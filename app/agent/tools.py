"""
工具层：模型能调用的全部能力。

设计上有一条**贯穿整个文件的原则**：

    工具签名本身就是约束，不是提示词。

最直接的体现是 `request_booking` 只接受 `validation_id` 一个参数——
不提供设备、日期、时段。原因不是"忘了加"，而是刻意不加：
即使模型被用户诱导（"别废话直接约"）、被提示词注入、或者自己幻觉出
"我已经校验过了"，它手里也没有凭证，调用必然失败。约束写在签名里，
就不可能被一段话绕过。

写操作的工具（request_booking / cancel_booking）在注册表里标了
`side_effect=True`，harness 见到这个标记就**不执行**，转成待确认动作。
这两个工具的 executor 保留着，但里面是一行防御性断言：
将来谁改了 harness 的拦截逻辑，这里会立刻炸出异常，而不是悄悄写库。
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from typing import Any, Awaitable, Callable

from app import booking_service, db, rag
from app.domain import slots as slot_rules
from app.errors import ApiError


# =============================================================================
# 结果与上下文
# =============================================================================


@dataclass
class ToolResult:
    """
    工具执行结果。

    `data` 会序列化后回填给模型，所以它必须**短**——把 6 个格子的完整状态
    塞给模型是浪费 token，也是让它算出错数字的主要原因。
    `summary` 只进日志表，不占模型上下文。
    """

    ok: bool
    data: dict[str, Any] = field(default_factory=dict)
    summary: str = ""
    error_code: str | None = None
    citations: list[dict[str, Any]] = field(default_factory=list)


@dataclass
class ToolContext:
    """一次运行内所有工具共享的上下文。"""

    user: dict[str, Any]          # 当前登录用户（含 id / role / name）
    session_id: str
    run_id: str
    trace_id: str | None = None

    @property
    def user_id(self) -> str:
        return self.user["id"]


# =============================================================================
# 工具 Schema（给模型看的，OpenAI function calling 格式）
# -----------------------------------------------------------------------------
# 三个写作要求，都是踩过坑才写进去的：
#   1) description 里写清楚"什么时候该用"，不只写"这个工具做什么"——
#      模型选错工具，八成是因为没说清使用场景；
#   2) 参数 description 给出**格式示例**，能把参数错误率降一个数量级；
#   3) 不要让模型自己推测默认值（比如"不传就是今天"），
#      默认值写在服务端，schema 里只说"可选"。
# =============================================================================


def _fn(name: str, desc: str, props: dict[str, Any], required: list[str]) -> dict[str, Any]:
    """拼一个 function 定义的样板，减少手写 JSON 的重复与括号错误。"""
    return {
        "type": "function",
        "function": {
            "name": name,
            "description": desc,
            "parameters": {
                "type": "object",
                "properties": props,
                "required": required,
                "additionalProperties": False,
            },
        },
    }


TOOL_SPECS: list[dict[str, Any]] = [
    _fn(
        "search_policy",
        "查询公司设备借用制度、操作规程、安全须知。回答任何关于'规定/标准/能不能/怎么办'"
        "的问题之前都必须先调用它。返回带章节号的原文片段，回答时必须据此标注引用。",
        {
            "query": {
                "type": "string",
                "description": "检索关键词，用名词短语而不是完整问句。"
                "例如用户问'每天能约几次'，写成'每日预约上限 格数'。",
            }
        },
        ["query"],
    ),
    _fn(
        "find_available_slots",
        "查询某一天有哪些设备、各自哪些时段还空着。"
        "回答'有没有空/能不能约/明天有位置吗'这类问题时使用。"
        "返回的 device_id 是后续校验预约的必填参数。",
        {
            "date": {
                "type": "string",
                "description": "日期，格式 YYYY-MM-DD。不传则默认今天。"
                "不要传'明天''下周一'这类相对说法，必须先换算成具体日期。",
            },
            "asset_no": {
                "type": "string",
                "description": "资产编号，如 'DEV-001'。只在用户明确指定某台设备时传，否则留空查全部。",
            },
        },
        [],
    ),
    _fn(
        "get_device_info",
        "查询单台设备的详细信息：状态（可借/已借出/已下架）、存放位置、型号、备注。"
        "用户问'某台设备在哪/是不是坏了/什么型号'时使用。",
        {
            "asset_no": {
                "type": "string",
                "description": "资产编号，如 'DEV-001'",
            }
        },
        ["asset_no"],
    ),
    _fn(
        "get_my_bookings",
        "查询当前登录用户自己的预约记录。用户问'我约了什么/我的预约/什么时候该还'时使用。"
        "只能查到自己名下的单，查不到别人的。",
        {
            "status": {
                "type": "string",
                "enum": ["booked", "picked", "returned", "cancelled", "expired"],
                "description": "按状态筛选。不传则返回全部未取消的记录。",
            }
        },
        [],
    ),
    _fn(
        "validate_booking",
        "校验一次预约是否可行，通过后返回一次性凭证 validation_id。"
        "**任何预约请求的第一步**。会检查设备状态、时段是否已被占、是否超出每日额度、"
        "日期是否合法。不通过时会返回具体原因和当天还空着的时段。",
        {
            "device_id": {
                "type": "string",
                "description": "设备 ID，必须来自 find_available_slots 的返回，不得自己编造",
            },
            "slot_date": {"type": "string", "description": "日期，格式 YYYY-MM-DD"},
            "slot_starts": {
                "type": "array",
                "items": {"type": "string"},
                "description": "时段起点数组，如 ['10:00','12:00']。每个元素必须是 08:00 到 18:00 的整点。",
            },
        },
        ["device_id", "slot_date", "slot_starts"],
    ),
    _fn(
        "request_booking",
        "提交预约。**唯一参数是 validate_booking 返回的 validation_id**，"
        "不接受设备与时段——所以不可能绕过校验。"
        "调用后系统会向用户展示确认卡片，由用户点击确认才真正生效。"
        "你不能声称预约已成功，只能说'已发起，请确认'。",
        {
            "validation_id": {
                "type": "string",
                "description": "validate_booking 返回的凭证 ID，5 分钟内有效，只能使用一次",
            }
        },
        ["validation_id"],
    ),
    _fn(
        "cancel_booking",
        "取消自己的一条预约。同样需要用户确认后才生效。",
        {
            "booking_id": {
                "type": "string",
                "description": "预约 ID，必须来自 get_my_bookings 的返回",
            }
        },
        ["booking_id"],
    ),
]

# 名字 → (执行函数, 是否有副作用)
# 有副作用的工具由 harness 拦截，不会执行 executor。
_REGISTRY: dict[str, tuple[Callable[..., Awaitable[ToolResult]], bool]] = {}


def tool(name: str, *, side_effect: bool = False):
    """
    注册装饰器。

    side_effect 是**唯一的写操作判据**，harness 只认这一个标记。
    这样新增工具时只需要想清楚"它会不会改数据"，不用同时改循环代码。
    """

    def deco(fn: Callable[..., Awaitable[ToolResult]]):
        _REGISTRY[name] = (fn, side_effect)
        return fn

    return deco


def is_side_effect(name: str) -> bool:
    entry = _REGISTRY.get(name)
    return bool(entry and entry[1])


def known(name: str) -> bool:
    return name in _REGISTRY


# =============================================================================
# 参数读取的小工具
# -----------------------------------------------------------------------------
# 模型给的参数类型经常不对（数字给字符串、数组给字符串），
# 每个工具里都写一遍 isinstance 判断会很啰嗦，集中在这里。
# 一律**宽松接受**而不是严格拒绝：能转换的就转换，转不了才报错。
# =============================================================================


def _str_arg(args: dict[str, Any], key: str, default: str | None = None) -> str | None:
    v = args.get(key)
    if v is None:
        return default
    if isinstance(v, str):
        v = v.strip()
        return v or default
    return str(v)


def _list_arg(args: dict[str, Any], key: str) -> list[str]:
    v = args.get(key)
    if v is None:
        return []
    if isinstance(v, str):
        # 模型偶尔把数组写成 "10:00,12:00" 或 "['10:00']"
        v = v.strip().strip("[]")
        parts = [p.strip().strip("'\"") for p in v.replace("，", ",").split(",")]
        return [p for p in parts if p]
    if isinstance(v, list):
        return [str(x).strip() for x in v if str(x).strip()]
    return []


def _normalize_date(value: str | None) -> str | None:
    """
    把模型给的日期整理成 YYYY-MM-DD。

    只做两种澄清：空值→今天；'2026/10/31' 或 '2026-10-31T00:00:00'→标准格式。
    刻意**不**解析"明天""下周一"：中文相对时间由模型换算，
    服务端猜一次就可能错一天，而错一天的预约比报错更难发现。
    """
    if not value:
        return booking_service.now().date().isoformat()
    text = value.strip().replace("/", "-")
    if "T" in text:
        text = text.split("T")[0]
    if " " in text:
        text = text.split(" ")[0]
    # parse_date 返回 None 而不抛异常，**必须检查返回值**。
    # 这里如果只写 try/except，非法日期会静默通过，最后在下单时才炸——
    # 那时错误信息已经离真正的原因很远了。
    if slot_rules.parse_date(text) is None:
        return None
    return text


# =============================================================================
# 读工具
# =============================================================================


@tool("search_policy")
async def search_policy(ctx: ToolContext, args: dict[str, Any]) -> ToolResult:
    """
    制度检索。

    返回给模型的是**带编号的片段**，编号与 citations 数组下标一一对应，
    这样模型写 [1][2] 时，前端能精确对上卡片。这个约定必须两边一致，
    否则会变成"回答里说 [2]，卡片却只有 3 条且对不上"。
    """
    query = _str_arg(args, "query")
    if not query:
        return ToolResult(ok=False, error_code="INVALID_ARGUMENT", data={"error": "query 不能为空"})

    hits = await rag.search(query, top_k=4)
    if not hits:
        # 检索不到不是错误，是事实。必须如实告诉模型"没有"，
        # 否则它会拿常识补完，这正是 RAG 最危险的失败模式。
        return ToolResult(
            ok=True,
            data={"found": 0, "note": "知识库中没有与该问题相关的条文，请如实告知用户'制度中未找到'"},
            summary="0 条命中",
        )

    snippets: list[dict[str, Any]] = []
    citations: list[dict[str, Any]] = []
    for i, h in enumerate(hits, start=1):
        c = h.chunk
        text = (c.text or "").strip().replace("\n", " ")
        heading = getattr(c, "title", None) or c.heading
        snippets.append({"no": i, "source": c.source, "heading": heading, "text": text[:400]})
        citations.append(
            {
                "index": i,
                "chunk_id": c.chunk_id,
                "source": c.source,
                "heading": heading,
                "path": c.heading,
                "snippet": text[:180] + ("…" if len(text) > 180 else ""),
                "score": round(float(h.score), 6),
            }
        )

    return ToolResult(
        ok=True,
        data={"found": len(snippets), "snippets": snippets},
        summary=f"{len(snippets)} 条命中：{snippets[0]['heading']}（{snippets[0]['source']}）",
        citations=citations,
    )


@tool("find_available_slots")
async def find_available_slots(ctx: ToolContext, args: dict[str, Any]) -> ToolResult:
    """查某天的空闲格。返回紧凑结构，只给 free 的格子。"""
    date = _normalize_date(_str_arg(args, "date"))
    if date is None:
        return ToolResult(
            ok=False,
            error_code="INVALID_ARGUMENT",
            data={"error": "日期格式不对，需要 YYYY-MM-DD（例如 2026-10-31）"},
        )

    asset_no = _str_arg(args, "asset_no")
    payload = await booking_service.list_devices_with_availability(date)

    devices: list[dict[str, Any]] = []
    for d in payload["devices"]:
        if asset_no and d["asset_no"].lower() != asset_no.lower():
            continue
        free = [s["start"] for s in d["slots"] if s["state"] == "free"]
        devices.append(
            {
                "device_id": d["id"],
                "asset_no": d["asset_no"],
                "name": d["name"],
                "location": d.get("location"),
                "status": d["status"],
                "free_slots": free,
            }
        )

    if not devices:
        note = f"没有找到资产编号为 {asset_no} 的设备" if asset_no else "知识库里没有设备"
        return ToolResult(ok=True, data={"date": date, "devices": [], "note": note}, summary="0 台设备")

    total_free = sum(len(d["free_slots"]) for d in devices)
    return ToolResult(
        ok=True,
        data={
            "date": date,
            "devices": devices,
            "note": f"共 {len(devices)} 台设备、{total_free} 个空闲时段"
            + ("；某台设备 free_slots 为空表示当天已约满" if total_free == 0 else ""),
        },
        summary=f"{date}：{len(devices)} 台设备 / {total_free} 个空闲格",
    )


@tool("get_device_info")
async def get_device_info(ctx: ToolContext, args: dict[str, Any]) -> ToolResult:
    asset_no = _str_arg(args, "asset_no")
    if not asset_no:
        return ToolResult(ok=False, error_code="INVALID_ARGUMENT", data={"error": "asset_no 不能为空"})

    device = await db.fetch_device_by_asset_no(asset_no)
    if device is None:
        return ToolResult(
            ok=False,
            error_code="DEVICE_NOT_FOUND",
            data={"error": f"没有资产编号为 {asset_no} 的设备。请先用 find_available_slots 查看有哪些设备。"},
        )

    status_text = {"available": "可借", "borrowed": "已借出", "offline": "已下架"}.get(device["status"], device["status"])
    return ToolResult(
        ok=True,
        data={
            "asset_no": device["asset_no"],
            "name": device["name"],
            "model": device.get("model"),
            "location": device.get("location"),
            "status": status_text,
            "note": device.get("note"),
        },
        summary=f"{device['asset_no']} {status_text}",
    )


@tool("get_my_bookings")
async def get_my_bookings(ctx: ToolContext, args: dict[str, Any]) -> ToolResult:
    status = _str_arg(args, "status")
    rows = await db.list_user_bookings(ctx.user_id, status, limit=20)
    if not rows:
        return ToolResult(ok=True, data={"bookings": [], "note": "该用户当前没有任何预约记录"}, summary="0 条")

    items = [
        {
            "booking_id": r["id"],
            "asset_no": r.get("asset_no"),
            "device_name": r.get("device_name"),
            "slot_date": r["slot_date"],
            "slot": f"{r['slot_start']}-{r['slot_end']}",
            "status": r["status"],
        }
        for r in rows
    ]
    return ToolResult(
        ok=True,
        data={"count": len(items), "bookings": items},
        summary=f"{len(items)} 条预约",
    )


@tool("validate_booking")
async def validate_booking(ctx: ToolContext, args: dict[str, Any]) -> ToolResult:
    """
    校验并签发凭证（T5）。

    凭证 5 分钟过期、用一次即废，且绑定 user_id——
    所以别人拿到 validation_id 也用不了，因为它只对签发者有效。
    """
    device_id = _str_arg(args, "device_id")
    date = _normalize_date(_str_arg(args, "slot_date"))
    starts = _list_arg(args, "slot_starts")

    if not device_id:
        return ToolResult(ok=False, error_code="INVALID_ARGUMENT", data={"error": "device_id 不能为空，需先调用 find_available_slots"})
    if date is None:
        return ToolResult(ok=False, error_code="INVALID_ARGUMENT", data={"error": "slot_date 格式不对，需要 YYYY-MM-DD"})
    if not starts:
        return ToolResult(ok=False, error_code="INVALID_ARGUMENT", data={"error": "slot_starts 不能为空，例如 ['10:00']"})

    result = await booking_service.preflight(ctx.user_id, device_id, date, starts)
    if not result["ok"]:
        # 把问题清单原样回填。模型据此要么换时段重试，要么如实转告用户。
        return ToolResult(
            ok=False,
            error_code=result["problems"][0]["code"],
            data={
                "valid": False,
                "problems": result["problems"],
                "free_alternatives": result.get("free_alternatives", []),
                "quota_left": result.get("quota_left"),
                "hint": "不要重复提交同样的参数。请改用 free_alternatives 里的时段，或告知用户原因。",
            },
            summary="校验不通过：" + "；".join(p["message"] for p in result["problems"])[:200],
        )

    # payload_hash 绑定"校验的到底是什么"，与 place_booking 时的参数做一致性核对
    payload = f"{device_id}|{date}|{','.join(sorted(starts))}"
    payload_hash = hashlib.sha256(payload.encode()).hexdigest()
    expires_at = (booking_service.now() + timedelta(minutes=5)).isoformat(timespec="seconds")
    vid = await db.create_validation(ctx.user_id, device_id, date, sorted(starts), payload_hash, expires_at)

    plan = {
        "validation_id": vid,
        "asset_no": result["device"]["asset_no"],
        "device_name": result["device"]["name"],
        "location": result["device"].get("location"),
        "slot_date": date,
        "slot_starts": sorted(starts),
        "quota_left_after": max(int(result.get("quota_left") or 0) - len(starts), 0),
        "expires_in_seconds": 300,
        "note": "校验通过。若要提交预约，调用 request_booking 并只传 validation_id。",
    }
    return ToolResult(ok=True, data=plan, summary=f"通过：{plan['asset_no']} {date} {','.join(sorted(starts))}")


# =============================================================================
# 写工具（harness 不会执行这两个函数）
# =============================================================================


@tool("request_booking", side_effect=True)
async def request_booking(ctx: ToolContext, args: dict[str, Any]) -> ToolResult:
    """
    提交预约——**这个函数永远不会被 harness 调用**。

    它存在的唯一价值是那条断言：
    如果哪天有人把 harness 里的拦截逻辑改坏了，或者新写了一个循环忘了拦截，
    这里会立刻抛异常（并被日志记下来），而不是静默地替模型把单下了。
    真实的下单发生在 `confirm.confirm_pending()`，由用户点击触发。

    参数只有 validation_id 这件事本身，也是同一层防御：
    模型手里没有设备和时段，就算绕过拦截也建不出单。
    """
    raise RuntimeError(
        "request_booking 不允许由模型直接执行：必须经用户确认后由 confirm_pending() 落单"
    )


@tool("cancel_booking", side_effect=True)
async def cancel_booking(ctx: ToolContext, args: dict[str, Any]) -> ToolResult:
    """取消预约，同样不会被 harness 直接执行。理由见 request_booking。"""
    raise RuntimeError(
        "cancel_booking 不允许由模型直接执行：必须经用户确认后由 confirm_pending() 执行"
    )


# =============================================================================
# 统一入口
# =============================================================================


async def execute(name: str, args: dict[str, Any], ctx: ToolContext) -> ToolResult:
    """
    执行一个工具，**永不抛异常**。

    这是 harness 能稳定跑完 5 轮的前提：任何一个工具抛出未捕获的异常，
    整个循环就断了，用户拿到的是 500。而绝大多数工具错误（设备不存在、
    日期非法、参数类型错）都是模型可以自己修正的——把错误结构化回填，
    它下一次调用往往就对了。

    ApiError 单独处理：它的 code 本来就是给模型看的有意义错误码
    （SLOT_TAKEN / QUOTA_EXCEEDED），原样透传是最好的提示。
    """
    entry = _REGISTRY.get(name)
    if entry is None:
        return ToolResult(
            ok=False,
            error_code="UNKNOWN_TOOL",
            data={"error": f"没有名为 {name} 的工具", "available": sorted(_REGISTRY)},
        )

    fn, _ = entry
    try:
        return await fn(ctx, args)
    except ApiError as e:
        return ToolResult(ok=False, error_code=e.code, data={"error": e.message, "detail": e.detail})
    except Exception as e:  # noqa: BLE001 - 工具层必须兜住一切
        return ToolResult(ok=False, error_code="TOOL_FAILED", data={"error": f"工具执行失败：{type(e).__name__}"})


def build_today_hint() -> str:
    """
    每天变化的上下文，拼进系统提示词。

    模型没有任何"今天是几号"的内部知识。不给它这一行，
    它算"明天"时就会从训练数据里的某个日期开始推，结果约到去年。
    这是工具调用类系统最常见、也最容易漏的一处。
    """
    now = booking_service.now()
    weekday = "一二三四五六日"[now.weekday()]
    return (
        f"今天是 {now.date().isoformat()}（星期{weekday}），当前时间 {now.strftime('%H:%M')}。"
        f"可预约范围是今天起 {slot_rules.BOOKING_WINDOW_DAYS} 天内。"
    )
