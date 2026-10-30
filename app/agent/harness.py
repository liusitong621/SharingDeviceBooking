"""
Harness：智能体执行循环。

四个阶段循环往复，最多 5 轮：

    模型决策 → 执行工具 → 结果回填 → 再决策

这里没有引入 LangGraph / OpenAI Agents SDK，原因不是"想自己造"，
而是本项目的约束恰好落在自研更划算的那一侧：

  1) 工具只有 7 个，主干是一条线（查 → 校验 → 发起），没有需要图调度的分支；
  2) 中断点只有一类（写操作前），而 LangGraph 的 interrupt 优势在多点状中断；
  3) 最要紧的一点：**中断后执行确认的必须是 REST 路径，不能回到模型**。
     图框架的 resume 语义是"从断点继续跑图"，而我们要的恰恰是"永远不要继续跑图"，
     用框架反而要和它的设计意图对抗。

三条循环内必须守住的规则（每条都对应一种真实故障）：

  1) 工具永远不抛异常，错误结构化回填。模型看到 SLOT_TAKEN 会自己换时段，
     抛出去就只能给用户 500。
  2) 每个 tool_call 都必须回填一条 tool 消息，哪怕它失败了。
     OpenAI 协议要求 tool 消息与 assistant 的 tool_calls 一一对应，
     漏一条，下一次请求直接 400。
  3) 同一工具、同一参数连续 3 次 → 判定为循环，回填提示并跳过。
     模型在失败后重复同样调用是最常见的死循环形态。
"""

from __future__ import annotations

import json
import time
from dataclasses import dataclass, field
from typing import Any, Awaitable, Callable

from app import db
from app.agent import confirm, prompts, tools
from app.agent.tools import ToolContext, ToolResult
from app.errors import ApiError
from app.llm import ChatResult, LLMStreamError, StreamLLM

# 轮次上限。5 轮的依据：一次完整预约需要 查时段 + 校验 + 发起 = 3 次工具调用，
# 留 2 轮给"校验失败后换时段重试"。超过 5 轮仍未收敛，几乎都是模型在打转，
# 继续下去只会烧 token 并让用户等更久。
MAX_ROUNDS = 5

# 同一工具同一参数重复调用的容忍次数
SAME_CALL_LIMIT = 3

Emit = Callable[[str, dict], Awaitable[None]]


@dataclass
class Outcome:
    """一次运行的结局。上层据此决定发 done 还是 confirm_required。"""

    answer: str = ""
    citations: list[dict[str, Any]] = field(default_factory=list)
    status: str = "answered"            # answered / awaiting_confirm / max_rounds / failed
    pending_id: str | None = None
    pending_summary: dict[str, Any] = field(default_factory=dict)
    rounds: int = 0
    run_id: str | None = None

    @property
    def interrupted(self) -> bool:
        return self.status == "awaiting_confirm"


async def run(
    *,
    ctx: ToolContext,
    question: str,
    history: list[dict],
    llm: StreamLLM,
    emit: Emit,
) -> Outcome:
    """
    跑完一次完整的决策循环。

    emit 是外层的 SSE 推送回调。循环本身不碰 HTTP，也不碰队列——
    这样它能在 CLI 里跑、能在测试里跑、也能换一个传输层（WebSocket）复用。
    """
    started = time.perf_counter()
    run_id = await db.start_run(
        ctx.session_id, ctx.user_id, question, ctx.trace_id, llm.model_name
    )

    messages: list[dict[str, Any]] = [
        {"role": "system", "content": prompts.system_prompt(tools.build_today_hint())}
    ]
    # 历史里只保留成功消息（db.recent_messages 已过滤），否则模型会模仿半截话
    messages.extend({"role": m["role"], "content": m["content"]} for m in history)
    messages.append({"role": "user", "content": question})

    citations: list[dict[str, Any]] = []
    seen_calls: dict[str, int] = {}
    last_round = 0

    try:
        for round_no in range(1, MAX_ROUNDS + 1):
            last_round = round_no
            # 最后一轮禁止再调工具，强制模型基于已有信息作答。
            # 这一刀保证"5 轮内必有回答"：否则第 5 轮它又想查东西，
            # 查完循环就结束了，用户拿到的是一句"轮次超限"。
            force_answer = round_no == MAX_ROUNDS

            await emit(
                "status",
                {
                    "stage": "thinking",
                    "text": "正在整理回答…" if force_answer else f"正在分析（第 {round_no} 轮）…",
                },
            )

            resp: ChatResult = await llm.chat_with_tools(
                messages,
                # 注意：即使强制应答也要**继续传 tools 定义**。
                # 历史消息里已经带了 tool_calls，此时若请求里没有 tools 字段，
                # 部分 OpenAI 兼容实现会直接报 400（"tool_calls 需要 tools 定义"）。
                # 真正禁止调用靠的是 tool_choice="none"，而不是抽掉定义。
                tools=tools.TOOL_SPECS,
                tool_choice="none" if force_answer else None,
            )

            # ---- 模型决定直接回答：循环结束 ----
            if not resp.wants_tools:
                answer = resp.content or _empty_answer_hint(round_no)
                await _stream_text(answer, emit)
                await db.finish_run(
                    run_id, "answered", round_no, _ms(started)
                )
                return Outcome(
                    answer=answer,
                    citations=citations,
                    status="answered",
                    rounds=round_no,
                    run_id=run_id,
                )

            # ---- 模型要求调工具：先把 assistant 的这一条原样回填 ----
            # 必须带 tool_calls，且 id 要与后面 tool 消息里的 tool_call_id 对应，
            # 否则下一次请求会被上游以 400 拒绝（协议硬要求）。
            messages.append(
                {
                    "role": "assistant",
                    "content": resp.content or None,
                    "tool_calls": [
                        {
                            "id": c.id,
                            "type": "function",
                            "function": {"name": c.name, "arguments": c.args_raw},
                        }
                        for c in resp.tool_calls
                    ],
                }
            )

            for step_no, call in enumerate(resp.tool_calls, start=1):
                # ① 参数不是合法 JSON：回填错误，让模型自己改（通常第二次就对）
                if call.parse_error or call.args is None:
                    await db.add_step(
                        run_id, round_no, step_no, call.name, None,
                        call.parse_error or "参数解析失败",
                        status="error", error_code="INVALID_ARGUMENT",
                    )
                    messages.append(
                        _tool_msg(
                            call.id,
                            {"ok": False, "error_code": "INVALID_ARGUMENT",
                             "error": f"参数格式不对：{call.parse_error}。请重新生成合法的 JSON 对象。"},
                        )
                    )
                    continue

                # ② 未知工具：把可用清单告诉它，比单纯报错有效得多
                if not tools.known(call.name):
                    await db.add_step(
                        run_id, round_no, step_no, call.name, call.args,
                        "未知工具", status="error", error_code="UNKNOWN_TOOL",
                    )
                    messages.append(
                        _tool_msg(
                            call.id,
                            {"ok": False, "error_code": "UNKNOWN_TOOL",
                             "error": f"没有名为 {call.name} 的工具",
                             "available": [s["function"]["name"] for s in tools.TOOL_SPECS]},
                        )
                    )
                    continue

                # ③ 写操作：不执行，转成待确认并**中断整个循环**
                if tools.is_side_effect(call.name):
                    outcome = await _interrupt(
                        ctx=ctx, run_id=run_id, call=call, round_no=round_no,
                        step_no=step_no, messages=messages, citations=citations,
                        emit=emit, started=started,
                    )
                    if outcome is not None:
                        return outcome
                    # 返回 None 表示"凭证无效，已回填错误"，继续循环让模型重新校验
                    continue

                # ④ 重复调用保护：同工具同参数第 3 次直接跳过
                key = f"{call.name}::{json.dumps(call.args, sort_keys=True, ensure_ascii=False)}"
                seen_calls[key] = seen_calls.get(key, 0) + 1
                if seen_calls[key] >= SAME_CALL_LIMIT:
                    await db.add_step(
                        run_id, round_no, step_no, call.name, call.args,
                        f"重复调用第 {seen_calls[key]} 次，已跳过",
                        status="skipped", error_code="REPEATED_CALL",
                    )
                    messages.append(
                        _tool_msg(
                            call.id,
                            {"ok": False, "error_code": "REPEATED_CALL",
                             "error": "同样的参数已经试过两次且没有成功，再试一次结果不会变。"
                                      "请换参数，或直接告知用户当前无法完成。"},
                        )
                    )
                    continue

                # ⑤ 正常执行
                t0 = time.perf_counter()
                result = await tools.execute(call.name, call.args, ctx)
                elapsed = int((time.perf_counter() - t0) * 1000)

                await db.add_step(
                    run_id, round_no, step_no, call.name, call.args,
                    result.summary or ("成功" if result.ok else "失败"),
                    status="ok" if result.ok else "error",
                    error_code=result.error_code,
                    elapsed_ms=elapsed,
                )

                # 引用在工具返回时立刻推给前端——用户等待模型组织语言时就能看到依据
                if result.citations:
                    citations = _merge_citations(citations, result.citations)
                    await emit("citations", {"hits": citations})

                await emit("status", {"stage": "tool_done", "text": _progress_text(call.name, result)})
                messages.append(_tool_msg(call.id, _payload(result)))

        # ---- 理论不可达：最后一轮被 tool_choice=none 强制应答 ----
        return await _finish_max_rounds(ctx, run_id, last_round, citations, emit, started)

    except LLMStreamError as e:
        code = "LLM_TIMEOUT" if e.kind == "timeout" else "LLM_UPSTREAM_ERROR"
        await db.finish_run(run_id, "failed", last_round, _ms(started), code)
        raise
    except ApiError as e:
        await db.finish_run(run_id, "failed", last_round, _ms(started), e.code)
        raise
    except Exception as e:  # noqa: BLE001
        await db.finish_run(run_id, "failed", last_round, _ms(started), type(e).__name__)
        raise


# =============================================================================
# 写操作的中断
# =============================================================================


async def _interrupt(
    *,
    ctx: ToolContext,
    run_id: str,
    call: Any,
    round_no: int,
    step_no: int,
    messages: list[dict],
    citations: list[dict],
    emit: Emit,
    started: float,
) -> Outcome | None:
    """
    写操作前的强制中断。

    返回 Outcome  = 已中断（凭证有效，卡片已发给用户，循环到此为止）
    返回 None     = 未中断（凭证是假的/别人的/过期的，已把错误回填，让模型重新校验）

    第二种情况很容易被忽略但必须处理：
    模型会幻觉出一个格式合法但根本不存在的 validation_id。如果照样发确认卡片，
    用户看到"确认预约"点下去必然失败——那比直接告诉模型"凭证无效"糟糕得多。
    """
    args = call.args or {}

    try:
        if call.name == "request_booking":
            summary = await confirm.prepare_request_booking(ctx, str(args.get("validation_id") or ""))
            action = "request_booking"
            validation_id = str(args.get("validation_id") or "")
            booking_id = None
        else:  # cancel_booking
            summary = await confirm.prepare_cancel(ctx, str(args.get("booking_id") or ""))
            action = "cancel_booking"
            validation_id = None
            booking_id = str(args.get("booking_id") or "")
    except ApiError as e:
        # 凭证不成立：回填错误，继续循环。模型重新走 validate_booking 就能拿到新凭证。
        await db.add_step(
            run_id, round_no, step_no, call.name, args,
            f"凭证校验失败：{e.message}", status="error", error_code=e.code,
        )
        messages.append(
            _tool_msg(
                call.id,
                {"ok": False, "error_code": e.code, "error": e.message,
                 "hint": "这个凭证不可用。请先调用 validate_booking 获取新的 validation_id，"
                         "不要重复使用旧凭证或自己编造。"},
            )
        )
        await emit("status", {"stage": "tool_done", "text": "校验凭证已失效，正在重新校验…"})
        return None

    pending_id = await db.create_pending(
        session_id=ctx.session_id,
        run_id=run_id,
        user_id=ctx.user_id,
        action=action,
        summary=summary,
        validation_id=validation_id,
        booking_id=booking_id,
    )

    await db.add_step(
        run_id, round_no, step_no, call.name, args,
        f"已中断，等待用户确认（pending={pending_id[:8]}）",
        status="interrupted", error_code=None,
    )
    await db.finish_run(run_id, "awaiting_confirm", round_no, _ms(started))

    await emit(
        "confirm_required",
        {
            "pending_id": pending_id,
            "summary": summary,
            "expires_at": summary.get("expires_at"),
            "note": "这是一次待确认的操作，需要你点击确认后才会真正执行。",
        },
    )
    return Outcome(
        status="awaiting_confirm",
        pending_id=pending_id,
        pending_summary=summary,
        rounds=round_no,
        citations=citations,
        run_id=run_id,
        answer="",
    )


# =============================================================================
# 收尾与工具函数
# =============================================================================


async def _finish_max_rounds(
    ctx: ToolContext,
    run_id: str,
    rounds: int,
    citations: list[dict],
    emit: Emit,
    started: float,
) -> Outcome:
    """
    轮次用尽的兜底。

    文案刻意不写"系统繁忙"这类含糊话：用户需要知道下一步能做什么。
    同时如实说明"没能完成"，不假装已经查清。
    """
    answer = (
        f"这个问题我查了 {rounds} 轮还是没能得出结论。"
        "可以试着把设备编号和具体日期说清楚一些，比如「明天下午 DEV-001 还有空吗」；"
        "如果问题涉及具体预约，也可以直接在「我的预约」里操作，或联系设备管理员。"
    )
    await _stream_text(answer, emit)
    await db.finish_run(run_id, "max_rounds", rounds, _ms(started), "MAX_ROUNDS_EXCEEDED")
    return Outcome(
        answer=answer, citations=citations, status="max_rounds",
        rounds=rounds, run_id=run_id,
    )


async def _stream_text(text: str, emit: Emit, chunk: int = 14) -> None:
    """
    把整段文本切成小块推给前端，模拟流式。

    这是一个**已知的体验折衷**：因为决策用的是非流式调用（要拿完整的 tool_calls），
    最终回答也只能一次性拿到，所以首字出现的时刻 = 回答生成完毕的时刻。
    对制度问答（100–300 字）而言，等待约 2–4 秒，可接受。

    要真正流式，需要改成两段式：决策非流式 → 收起 tools 再发一次流式请求。
    代价是每轮多一次调用、多一份 token，且模型两次回答可能不一致。
    在第一版里，一致性比首字延迟更重要——回答内容错了，快也没有意义。
    """
    for i in range(0, len(text), chunk):
        await emit("delta", {"text": text[i : i + chunk]})


def _tool_msg(call_id: str, payload: dict[str, Any]) -> dict[str, Any]:
    """构造回填给模型的 tool 消息。"""
    return {
        "role": "tool",
        "tool_call_id": call_id,
        "content": json.dumps(payload, ensure_ascii=False),
    }


def _payload(result: ToolResult) -> dict[str, Any]:
    payload: dict[str, Any] = {"ok": result.ok}
    if result.error_code:
        payload["error_code"] = result.error_code
    payload.update(result.data)
    return payload


def _merge_citations(existing: list[dict], new: list[dict]) -> list[dict]:
    """
    合并多次检索的引用，按 chunk_id 去重后**重新编号**。

    重新编号是关键：模型在回答里写 [1][2] 参照的是**最后一次**检索的编号，
    如果直接把两次结果拼起来（编号 1,2 + 1,2），前端会出现两组 [1]，
    用户点引用时对不上。这里以最新一次为准，旧结果降级保留在后面。
    """
    seen: set[str] = set()
    merged: list[dict] = []
    for item in list(new) + list(existing):
        cid = item.get("chunk_id")
        if cid in seen:
            continue
        seen.add(cid)
        merged.append(dict(item))
    for i, item in enumerate(merged, start=1):
        item["index"] = i
    return merged


def _progress_text(name: str, result: ToolResult) -> str:
    """
    给用户看的进度提示。

    不写"正在调用 search_policy"——用户不关心工具名，只关心"系统在干什么"。
    """
    if not result.ok:
        return "这一步没查到，正在换一种方式…"
    return {
        "search_policy": "已找到相关制度条文…",
        "find_available_slots": "已查到空闲时段…",
        "get_device_info": "已查到设备信息…",
        "get_my_bookings": "已查到你的预约记录…",
        "validate_booking": "校验通过，正在准备确认…",
    }.get(name, "正在处理…")


def _empty_answer_hint(round_no: int) -> str:
    return (
        "我没能组织出有效的回答。请把问题说得更具体一些，"
        "比如要查哪台设备、哪一天，或者直接问某条规定。"
    )


def _ms(started: float) -> int:
    return int((time.perf_counter() - started) * 1000)
