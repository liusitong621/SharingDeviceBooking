"""
聊天接口：POST /chat 的 SSE 流式问答，以及会话历史读取、清空、探活。

整个文件只做三件事：校验、编排、收尾。检索在 app/rag.py，模型调用在 app/llm.py。

一次请求的时序（与接口设计里的图一致）：
    校验（同步，失败直接 4xx）
      -> 占会话锁 -> 取历史 -> 落用户消息
      -> 返回 200 + 开流
      -> meta / status(retrieving) / citations / status(generating) / delta... / done
      -> 落 assistant 消息（整段一次写入）

**开流之后的一切错误都走 event: error，不再有 HTTP 状态码可改。**
这不是风格选择，是 SSE 的物理限制：状态码在响应头发出时就已经定了。
"""

from __future__ import annotations

import asyncio
import hmac
import logging
import re
import time
import uuid
from dataclasses import dataclass
from typing import Any, AsyncIterator

from fastapi import APIRouter, Depends, Request
from fastapi.responses import StreamingResponse

from app import db, rag
from app.agent import confirm, harness
from app.agent.tools import ToolContext
from app.auth import current_user
from app.errors import ApiError, new_trace_id, trace_id_of
from app.llm import LLMStreamError, StreamLLM
from app.sse import SSE_HEADERS, frame, pump

logger = logging.getLogger("app.chat")

router = APIRouter(prefix="/api/v1", tags=["chat"])

# =============================================================================
# 常量
# =============================================================================
MAX_QUESTION_LEN = 500        # 与接口约定一致
DEFAULT_HISTORY_TURNS = 6     # 默认带最近 6 轮
MAX_HISTORY_TURNS = 12        # 上限，防止前端传 1000 把上下文打爆
HISTORY_BUDGET_CHARS = 2000   # 历史总字数预算（超过从最老的丢）
TOP_K = 3
SEARCH_TIMEOUT = 15.0         # 检索超时：超了说明索引/向量服务有问题，不是慢
LLM_TIMEOUT = 45.0            # 单次模型超时（降级路径与流式生成用）
# 整个智能体运行的总超时。5 轮决策 × 单轮 45s 太宽松了，用户等不了 4 分钟；
# 120s 足够跑完"查时段 → 校验 → 发起"的最长路径，超了就是真的卡住了。
AGENT_TIMEOUT = 120.0
COOKIE_NAME = "sdb_client"
COOKIE_MAX_AGE = 30 * 24 * 3600

UUID_RE = re.compile(r"^[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}$")
TOKEN_RE = re.compile(r"^[0-9a-f]{32}$")


# =============================================================================
# LLM 客户端单例
# -----------------------------------------------------------------------------
# 复用 httpx.AsyncClient 的连接池；每次请求新建客户端会多一轮 TCP + TLS 握手，
# 在首字延迟本来就慢的场景里是纯粹的浪费。
# =============================================================================

_llm: StreamLLM | None = None


def get_llm() -> StreamLLM | None:
    """返回可用的模型客户端；未配置密钥时返回 None，由调用方走降级路径。"""
    global _llm
    s = rag.settings()
    if s is None or not getattr(s, "llm_ready", False):
        return None
    if _llm is None:
        _llm = StreamLLM(s)
    return _llm


async def close_llm() -> None:
    """进程退出时释放连接池。"""
    global _llm
    if _llm is not None:
        await _llm.aclose()
        _llm = None


# =============================================================================
# 入参校验
# -----------------------------------------------------------------------------
# 手工校验而不是交给 pydantic，是为了拿到**精确的错误码**。
# 用 pydantic 的话，"问题为空"和"字段类型错了"都会变成同一个 422，
# 前端就没法只对"问题过长"做字数提示。
# =============================================================================


@dataclass
class ChatRequest:
    session_id: str
    question: str
    history_turns: int
    client_msg_id: str | None


async def _read_json(request: Request) -> dict:
    """读请求体并确保是 JSON 对象。任何解析问题都算 INVALID_REQUEST。"""
    try:
        body = await request.json()
    except Exception as e:  # noqa: BLE001 - json 解析异常类型随版本变化，统一处理
        raise ApiError("INVALID_REQUEST", f"请求体不是合法 JSON：{e}") from e
    if not isinstance(body, dict):
        raise ApiError("INVALID_REQUEST", "请求体必须是 JSON 对象")
    return body


def _validate_session_id(value: Any) -> str:
    if not isinstance(value, str) or not UUID_RE.match(value):
        raise ApiError("INVALID_SESSION_ID", "session_id 必须是 UUID v4 字符串")
    return value


def _validate(body: dict) -> ChatRequest:
    """按「先格式、后内容」的顺序校验，保证错误码指向真正的第一处问题。"""
    session_id = _validate_session_id(body.get("session_id"))

    question = body.get("question")
    if not isinstance(question, str):
        raise ApiError("INVALID_REQUEST", "question 必须是字符串")
    question = question.strip()
    if not question:
        raise ApiError("QUESTION_EMPTY")
    if len(question) > MAX_QUESTION_LEN:
        raise ApiError("QUESTION_TOO_LONG", f"实际长度 {len(question)} 字")

    turns = body.get("history_turns", DEFAULT_HISTORY_TURNS)
    # 注意 bool 是 int 的子类，True 会被当成 1，必须显式排除
    if isinstance(turns, bool) or not isinstance(turns, int) or not (1 <= turns <= MAX_HISTORY_TURNS):
        raise ApiError("INVALID_HISTORY_TURNS", f"允许范围 1–{MAX_HISTORY_TURNS}")

    client_msg_id = body.get("client_msg_id")
    if client_msg_id is not None and not isinstance(client_msg_id, str):
        raise ApiError("INVALID_REQUEST", "client_msg_id 必须是字符串")

    return ChatRequest(session_id=session_id, question=question, history_turns=turns, client_msg_id=client_msg_id)


# =============================================================================
# 客户端绑定（无登录场景的最小防护）
# -----------------------------------------------------------------------------
# 任何人拿到 session_id 就能读历史，这对"制度问答"来说是不可接受的默认值。
# 方案：服务端下发一个 HttpOnly cookie 作为客户端凭据，与 session 一对一绑定。
# 拿到别人的 session_id 也读不到，因为 cookie 对不上。
# 局限要写清楚：换浏览器/清 cookie 会丢失历史，所以前端把 session_id 存 localStorage 也没用，
# 真正需要跨设备时，才值得引入登录。
# =============================================================================


def _new_token() -> str:
    return uuid.uuid4().hex


def _existing_token(request: Request) -> str | None:
    token = request.cookies.get(COOKIE_NAME)
    return token if token and TOKEN_RE.match(token) else None


def _client_token(request: Request) -> tuple[str, bool]:
    """返回 (token, 是否为新建)。新建时调用方要下发 Set-Cookie。"""
    token = _existing_token(request)
    if token:
        return token, False
    return _new_token(), True


def _assert_owner(session: dict, user: dict, token: str | None) -> None:
    """
    会话归属校验。批次 1 之后有两代规则并存：

      - 带 user_id 的会话（登录后创建的）：按 user_id 判，同一账号换设备也能看历史；
      - 不带 user_id 的历史会话（阶段 2 匿名产生）：仍按 cookie 判，行为不变。

    为什么不给管理员在这里开绿灯：审计页读会话有自己的入口，
    问答接口如果允许管理员读任意会话，就等于把"看别人聊天"变成常规能力，
    而这个能力应该只有审计流程（有留痕）才具备。
    """
    owner_id = session.get("user_id")
    if owner_id:
        if owner_id != user["id"]:
            raise ApiError("SESSION_FORBIDDEN")
        return
    if token is None or not hmac.compare_digest(str(session.get("client_token", "")), token):
        raise ApiError("SESSION_FORBIDDEN", "cookie 与会话不匹配")


# =============================================================================
# 历史裁剪
# -----------------------------------------------------------------------------
# 提示词已迁到 app/agent/prompts.py —— 从"固定流程问答"升级为智能体之后，
# 提示词里必须包含工具用法、三步预约流程、日期上下文，这些只有编排层知道。
# 接口层不再拼提示词，只负责把历史裁到预算内。
# =============================================================================


def _trim_history(history: list[dict], budget: int = HISTORY_BUDGET_CHARS) -> list[dict]:
    """
    按字数预算裁剪历史，从最老的开始丢。

    为什么按字数而不是按条数：一条消息可能是 800 字的引用原文，另一条只有"那第二格呢"，
    按条数裁剪会一次砍掉太多有效上下文。
    """
    kept: list[dict] = []
    total = 0
    for m in reversed(history):
        size = len(m.get("content") or "")
        if kept and total + size > budget:
            break
        kept.append({"role": m["role"], "content": m["content"]})
        total += size
    return list(reversed(kept))


# =============================================================================
# 引用与降级
# =============================================================================


def _citation(hit: Any, index: int) -> dict:
    """
    把检索结果转成前端可直接渲染的结构。字段名就是前端契约。

    heading 用**叶标题**（"二、可预约时段"），不用完整路径
    （"共享设备借用制度（演示版 v1） > 二、可预约时段"）：
    界面上一行放不下，且前半截对每条引用都一样，是纯噪声。
    完整路径保留在 path 里，挂在 title 属性上供悬浮查看。
    """
    c = hit.chunk
    # 片段同样要压平 Markdown：原文里的 **加粗** 在界面上是纯噪音
    text = _plain((c.text or "").strip()).replace("\n", " ")
    return {
        "index": index,
        "chunk_id": c.chunk_id,
        "source": c.source,                     # 文件名
        "heading": getattr(c, "title", None) or c.heading,  # 章节（叶标题）
        "path": c.heading,                      # 完整章节路径
        "snippet": text[:180] + ("…" if len(text) > 180 else ""),
        "score": round(float(hit.score), 6),
    }


def _plain(text: str) -> str:
    """
    把制度原文里的 Markdown 标记压平成纯文本。

    降级模式是直接贴原文，原文里的 **加粗** 和 "- 列表" 在界面上会原样露出来，
    看起来像是程序出错。这里只做最小处理：去掉强调符、把短横线列表换成间隔点。
    """
    text = text.replace("**", "").replace("__", "")
    return re.sub(r"(?m)^\s*[-*]\s+", "· ", text)


def _fallback_pieces(hits: list) -> list[str]:
    """
    没有配置大模型密钥时的降级输出：直接把检索到的原文分片推给前端。

    这不是"回答问题"，是"把检索结果摆出来"，所以文案必须诚实说明是降级模式，
    绝不能伪造一段像是模型归纳过的结论。
    按 12 字一片切，是为了让打字机效果不至于一瞬间全部铺满。
    """
    if not hits:
        return ["当前没有配置大模型密钥（LLM_API_KEY），也没有检索到相关资料。\n"
                "请先在 .env 中配置密钥后重试。"]

    parts = ["【降级模式】未配置大模型密钥，以下直接列出检索到的制度原文：\n\n"]
    for i, h in enumerate(hits, start=1):
        c = h.chunk
        body = _plain((c.text or "").strip())[:300]
        parts.append(f"[{i}] {c.source} §{getattr(c, 'title', None) or c.heading}\n{body}\n\n")
    parts.append("配置 LLM_API_KEY 后，可获得归纳式回答。")
    text = "".join(parts)
    return [text[i : i + 12] for i in range(0, len(text), 12)]


# =============================================================================
# 路由
# =============================================================================


@router.post("/chat")
async def chat(request: Request, user: dict = Depends(current_user)) -> StreamingResponse:
    trace_id = trace_id_of(request)
    req = _validate(await _read_json(request))
    token, is_new_token = _client_token(request)

    # ---- 会话：不存在就建，存在就校验归属 ----
    session = await db.fetch_session(req.session_id)
    if session is None:
        # user_id 落库后，审计页才能把会话归到人；这条是"按员工筛选对话"的前提
        await db.create_session(req.session_id, token, _make_title(req.question), user["id"])
        session = await db.fetch_session(req.session_id)
    else:
        _assert_owner(session, user, _existing_token(request))

    # ---- 检索服务必须就绪，否则开流后没法发 citations ----
    rag_state = rag.status()
    if not rag_state["ready"]:
        raise ApiError("RETRIEVAL_UNAVAILABLE", rag_state["error"])

    # ---- 同会话互斥：抢不到立即 409，不排队 ----
    if not db.try_lock(req.session_id):
        raise ApiError("SESSION_BUSY")

    # 准备阶段一旦失败必须释放锁，否则这个会话会永久 409
    try:
        history = await db.recent_messages(req.session_id, req.history_turns * 2)
        history = _trim_history(history)
        user_message_id = str(uuid.uuid4())
        await db.append_message(
            req.session_id,
            "user",
            req.question,
            trace_id=trace_id,
            message_id=user_message_id,
        )
    except BaseException:
        db.unlock(req.session_id)
        raise

    headers = dict(SSE_HEADERS)
    if is_new_token:
        # 内网 http 部署先不加 Secure；将来上 HTTPS 时必须补上
        headers["Set-Cookie"] = (
            f"{COOKIE_NAME}={token}; Path=/; HttpOnly; SameSite=Lax; Max-Age={COOKIE_MAX_AGE}"
        )

    stream = _event_stream(
        req=req,
        history=history,
        trace_id=trace_id,
        user_message_id=user_message_id,
        user=user,
    )
    # media_type 只写 text/event-stream，charset 由 Starlette 自动补成 utf-8
    return StreamingResponse(stream, media_type="text/event-stream", headers=headers)


async def _event_stream(
    *,
    req: ChatRequest,
    history: list[dict],
    trace_id: str,
    user_message_id: str,
    user: dict,
) -> AsyncIterator[str]:
    """
    真正的 SSE 生成器。

    结构上分成「生产者」和「消费者」两条协程，中间用队列解耦：
      - 生产者：检索 -> 调模型 -> 产出 delta；
      - 消费者：从队列取事件 -> 掺心跳 -> yield 给客户端。
    解耦的唯一目的是心跳：模型 20 秒不吐字时，消费者仍然能每 15 秒发一次 ping。
    """
    started = time.perf_counter()
    assistant_id = str(uuid.uuid4())
    queue: asyncio.Queue[tuple[str, dict] | None] = asyncio.Queue()

    # 可变状态：生产者和收尾逻辑共享，事件循环单线程，不需要加锁
    st: dict[str, Any] = {
        "text": "",
        "citations": [],
        "model": None,
        "degraded": False,
        "persisted": False,
        "pending": None,          # 中断时的待确认信息，落库后刷新页面仍能看到卡片
    }

    async def persist(status: str) -> None:
        """整段落库。逐 token 写库会让 SQLite 每字一次 fsync，这是必须避免的写法。"""
        if st["persisted"]:
            return
        st["persisted"] = True
        try:
            await db.append_message(
                req.session_id,
                "assistant",
                st["text"],
                status=status,
                citations=st["citations"] or None,
                model=st["model"],
                latency_ms=int((time.perf_counter() - started) * 1000),
                trace_id=trace_id,
                message_id=assistant_id,
            )
        except Exception:  # noqa: BLE001 - 落库失败不能影响已经把内容推给用户这件事
            logger.exception("[%s] assistant 消息落库失败", trace_id)

    async def fail(code: str, message: str) -> None:
        """流内失败：先把已有内容落库（标记 error），再发事件，最后关流。"""
        await persist("error")
        await queue.put(("error", {"code": code, "message": message, "recoverable": bool(st["text"])}))
        await queue.put(None)

    async def producer() -> None:
        try:
            llm = get_llm()

            # =============================================================
            # 路径 A：未配置模型密钥 —— 降级为"预检索 + 直接贴原文"
            # -------------------------------------------------------------
            # 这条路径刻意保留：没有密钥时也要能演示完整链路，
            # 但它**不是智能体**，所以文案里必须诚实标注"降级模式"。
            # 用贴出来的原文冒充"模型总结"，是这个项目最容易犯的错。
            # =============================================================
            if llm is None:
                hits = await asyncio.wait_for(
                    rag.search(req.question, top_k=TOP_K), timeout=SEARCH_TIMEOUT
                )
                st["citations"] = [_citation(h, i) for i, h in enumerate(hits, start=1)]
                await queue.put(("citations", {"hits": st["citations"]}))
                await queue.put(("status", {"stage": "generating", "text": "正在生成回答…"}))
                st["degraded"] = True
                for piece in _fallback_pieces(hits):
                    st["text"] += piece
                    await queue.put(("delta", {"text": piece}))
                    await asyncio.sleep(0.01)
                await persist("ok")
                await queue.put((
                    "done",
                    {
                        "assistant_message_id": assistant_id,
                        "elapsed_ms": int((time.perf_counter() - started) * 1000),
                        "degraded": True,
                        "interrupted": False,
                        "usage": None,
                    },
                ))
                await queue.put(None)
                return

            # =============================================================
            # 路径 B：智能体循环
            # -------------------------------------------------------------
            # 检索不再由接口层预先发起，而是**由模型决定要不要检索**。
            # 这是从"固定流程问答"升级为"自主决策"的实质区别：
            # 问"你好"不该触发检索，问"每天几格"才该。
            # 引用因此也由工具返回时推送，而不是开流时预推。
            # =============================================================
            st["model"] = llm.model_name
            ctx = ToolContext(
                user=user,
                session_id=req.session_id,
                run_id="",                 # 真实 run_id 由 harness 内部生成并落库
                trace_id=trace_id,
            )

            async def emit(event: str, data: dict) -> None:
                """把 harness 的事件翻译成队列里的 SSE 事件。"""
                await queue.put((event, data))

            async with asyncio.timeout(AGENT_TIMEOUT):
                outcome = await harness.run(
                    ctx=ctx,
                    question=req.question,
                    history=history,
                    llm=llm,
                    emit=emit,
                )

            st["citations"] = outcome.citations

            if outcome.interrupted:
                # 中断：模型想写库，但**没有执行**。这里落一条"待确认"消息，
                # 目的是刷新页面后卡片还在——否则用户一刷新就以为已经约上了。
                st["pending"] = {
                    "pending_id": outcome.pending_id,
                    "summary": outcome.pending_summary,
                }
                st["text"] = _interrupt_text(outcome.pending_summary)
                # citations 里放一条带 kind 的条目，前端据此渲染成确认卡片而不是引用
                st["citations"] = [
                    {
                        "kind": "pending_action",
                        "pending_id": outcome.pending_id,
                        "summary": outcome.pending_summary,
                    }
                ]
                await persist("ok")
                await queue.put((
                    "done",
                    {
                        "assistant_message_id": assistant_id,
                        "elapsed_ms": int((time.perf_counter() - started) * 1000),
                        "degraded": False,
                        "interrupted": True,
                        "pending_id": outcome.pending_id,
                        "rounds": outcome.rounds,
                        "usage": None,
                    },
                ))
                await queue.put(None)
                return

            st["text"] = outcome.answer
            await persist("ok")
            await queue.put((
                "done",
                {
                    "assistant_message_id": assistant_id,
                    "elapsed_ms": int((time.perf_counter() - started) * 1000),
                    "degraded": False,
                    "interrupted": False,
                    "rounds": outcome.rounds,
                    "status": outcome.status,
                    "usage": None,
                },
            ))
            await queue.put(None)
        except asyncio.CancelledError:
            # 客户端断开：不吞掉，交给外层 finally 落 aborted
            raise
        except asyncio.TimeoutError:
            await fail("LLM_TIMEOUT", "这次分析用时不正常，已保留已生成的内容，请重试")
        except LLMStreamError as e:
            code = "LLM_TIMEOUT" if e.kind == "timeout" else "LLM_UPSTREAM_ERROR"
            logger.warning("[%s] 模型调用失败：%s", trace_id, e)
            await fail(code, "大模型服务暂时不可用，请稍后重试")
        except Exception:  # noqa: BLE001
            logger.exception("[%s] 流式生成异常", trace_id)
            await fail("INTERNAL_ERROR", "服务内部错误，请稍后重试")

    # ---- 开流：先发 meta 与状态 ----
    # 状态文案从"正在检索制度"改成"正在分析"：现在检索与否由模型决定，
    # 一上来就说"正在检索"，等于向用户承诺了一件可能不会发生的事。
    yield frame("meta", {"session_id": req.session_id, "user_message_id": user_message_id, "trace_id": trace_id})
    yield frame("status", {"stage": "thinking", "text": "正在分析你的问题…"})

    task = asyncio.create_task(producer())
    try:
        async for chunk in pump(queue):
            yield chunk
    finally:
        # 这里覆盖三种结局：正常结束、客户端断开、任务被取消。
        # 正常结束时 st["persisted"] 已为 True，persist("aborted") 会直接返回。
        if not task.done():
            task.cancel()
        try:
            # shield：即使当前协程已被取消，也要让落库任务跑完
            await asyncio.shield(asyncio.create_task(persist("aborted")))
        except (asyncio.CancelledError, Exception):  # noqa: BLE001
            pass
        db.unlock(req.session_id)


def _interrupt_text(summary: dict) -> str:
    """
    中断时给用户的正文。

    这段文字是**服务端生成**的，不是模型生成的——因为它要确保说清一件事：
    "还没有生效"。模型很容易写成"已为您预约成功"，那是这个系统最不能出的错。
    由服务端用结构化数据拼出来，就不可能写错。
    """
    if summary.get("kind") == "cancel":
        return (
            f"准备取消 {summary.get('asset_no', '')} 在 {summary.get('slot_date', '')} "
            f"{summary.get('slot_text', '')} 的预约。确认后才会真正执行。"
        )
    return (
        f"已准备好这次预约：{summary.get('asset_no', '')} {summary.get('device_name', '')}，"
        f"{summary.get('slot_date', '')} 的 {summary.get('slot_text', '')}，"
        f"共 {summary.get('count', 0)} 个时段。"
        "请在下方卡片里确认——在你点击之前，这一单还没有生效。"
    )


def _make_title(question: str) -> str:
    """会话标题：取首条问题前 20 字，管理员在审计页一眼能认出是哪次对话。"""
    return question[:20] + ("…" if len(question) > 20 else "")


# =============================================================================
# 历史与清空
# =============================================================================


@router.get("/chat/sessions/{session_id}/messages")
async def get_messages(session_id: str, request: Request, user: dict = Depends(current_user)) -> dict:
    """刷新页面后恢复历史。无 cookie 直接拒，不生成新 token（否则永远对不上）。"""
    _validate_session_id(session_id)
    session = await db.fetch_session(session_id)
    if session is None:
        raise ApiError("SESSION_NOT_FOUND")
    _assert_owner(session, user, _existing_token(request))
    return {
        "session_id": session_id,
        "title": session["title"],
        "messages": await db.list_messages(session_id),
    }


@router.delete("/chat/sessions/{session_id}")
async def clear_session(session_id: str, request: Request, user: dict = Depends(current_user)) -> dict:
    """前端「新对话」：清空当前会话。只删自己的，删不到就是 403/404。"""
    _validate_session_id(session_id)
    session = await db.fetch_session(session_id)
    if session is None:
        raise ApiError("SESSION_NOT_FOUND")
    _assert_owner(session, user, _existing_token(request))
    removed = await db.delete_session(session_id)
    return {"deleted": True, "session_id": session_id, "removed_messages": removed}


@router.get("/health")
async def health() -> dict:
    """探活 + 索引就绪状态。前端加载时先打这个，决定是否禁用输入框。"""
    return {"status": "ok", "trace_id": new_trace_id(), "rag": rag.status()}
