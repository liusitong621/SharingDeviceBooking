"""
端到端演示：一次 AI 预约从「查空档」到「审计可查」的完整闭环。

怎么用：

    .venv/bin/python examples/demo.py

它跑在**临时库**上（`tempfile.mkdtemp()`），跑完就丢，不碰 `data/chat.db`。
所以可以随时重跑，不需要先备份数据。

为什么不接大模型也能演示
------------------------
这个脚本不调用 LLM，而是**直接调用模型会调用的那一层**（`app/agent/tools.py`）
与用户点击确认后执行的那一层（`app/agent/confirm.py`）。这两层之间的边界
才是本项目的核心设计，而模型只是其中一个调用方：

    模型 → validate_booking（拿凭证） → request_booking（被拦截）
         → prepare_*(生成摘要) → 用户点确认 → confirm_pending（真正写库）

所以脚本演示的是"约束在哪里"，不是"模型有多聪明"。装了 LLM_API_KEY 之后，
同样的路径由模型驱动，边界一条不变。

六幕对应 README「一次 AI 预约发生了什么」的八个步骤，以及需求 R01/R02/R03/R05/R07。
"""

from __future__ import annotations

import asyncio
import json
import logging
import sys
import tempfile
import unicodedata
import uuid
from datetime import timedelta
from pathlib import Path
from typing import Any

# 允许 `python examples/demo.py` 直接跑（不用先设 PYTHONPATH）
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app import booking_service, db, seed  # noqa: E402
from app.agent import confirm, tools  # noqa: E402
from app.errors import ApiError  # noqa: E402

WIDTH = 72

# 演示只输出自己的内容：seed 的 "已写入 3 个演示账号" 之类的日志会打断阅读
logging.getLogger("app").setLevel(logging.ERROR)


# =============================================================================
# 输出小工具
# -----------------------------------------------------------------------------
# 刻意不用颜色，也不用第三方库：重定向到文件、在别人的终端里跑，都不该出乱码。
# =============================================================================


def rule(char: str = "─") -> None:
    print(char * WIDTH)


def act(no: int, title: str) -> None:
    print()
    rule("═")
    print(f" 第 {no} 幕  {title}")
    rule("═")


def pad(text: str, width: int) -> str:
    """
    按**显示宽度**补齐，而不是按字符数。

    中文字符占两格，f"{'校验结果':<16}" 只会补 12 个空格，
    下面几行的 value 就永远对不齐——这种错位在终端里特别明显。
    """
    shown = sum(2 if unicodedata.east_asian_width(c) in "WF" else 1 for c in text)
    return text + " " * max(0, width - shown)


def note(text: str) -> None:
    print(f"   {text}")


def fact(label: str, value: Any) -> None:
    print(f"   {pad(label, 18)}{value}")


def detail(text: str) -> None:
    print(f"     · {text}")


def short(text: str, limit: int = 46) -> str:
    text = " ".join(str(text).split())
    return text if len(text) <= limit else text[: limit - 1] + "…"


def slots_text(states: list[dict[str, Any]], want: str | None = None) -> str:
    """把时段状态压成 '08:00 自由 / 10:00 已占' 这种一眼能读的一行。"""
    picked = [s for s in states if want is None or s["state"] == want]
    return "、".join(f"{s['start']}({s['state']})" for s in picked) or "无"


# =============================================================================
# 主体
# =============================================================================


async def main() -> int:
    # —— 临时库：与真实数据完全隔离 ——
    tmp_dir = Path(tempfile.mkdtemp(prefix="sdb-demo-"))
    db.DB_PATH = tmp_dir / "chat.db"
    db.init_db()
    await seed.seed_if_empty()

    print()
    rule("═")
    print(" 共享设备借用助手 · 端到端演示")
    fact("临时库", db.DB_PATH)
    fact("真实库", "data/chat.db（本次不读写）")
    rule("═")

    users = {u["emp_no"]: u for u in await db.list_users()}
    zhang = users["1001"]        # 张伟：本演示的"我"
    li = users["1002"]           # 李娜：用来演示越权
    tomorrow = (booking_service.now() + timedelta(days=1)).date().isoformat()

    # -------------------------------------------------------------------------
    act(1, "查空档：员工只关心「哪天哪台设备还有位置」")
    # -------------------------------------------------------------------------
    avail = await booking_service.list_devices_with_availability(tomorrow, me_id=zhang["id"])
    note(f"查询日期 {tomorrow}（明天），视角 = 张伟（1001）")
    print()
    for d in avail["devices"]:
        free = [s["start"] for s in d["slots"] if s["state"] == "free"]
        print(f"   {d['asset_no']}  {pad(d['name'], 16)}{pad(d['location'], 20)}可约 {len(free)} 格")
        detail("空闲时段：" + ("、".join(free) if free else "无"))
    device = avail["devices"][0]
    detail(f"格数上限：每人每天 {booking_service.slot_rules.MAX_SLOTS_PER_DAY} 格（R01）")

    # -------------------------------------------------------------------------
    act(2, "模型只能「校验」，不能「下单」")
    # -------------------------------------------------------------------------
    sid = str(uuid.uuid4())
    await db.create_session(sid, "demo-client-token", "演示会话", user_id=zhang["id"])
    trace_id = uuid.uuid4().hex[:12]
    run_id = await db.start_run(
        sid, zhang["id"], f"帮我约明天 {device['asset_no']} 的 08:00 和 10:00 两个时段",
        trace_id, model="(demo · 未接模型)",
    )
    ctx = tools.ToolContext(user=zhang, session_id=sid, run_id=run_id, trace_id=trace_id)

    # 2.1 先看一眼 request_booking 的参数：只有 validation_id
    spec = next(s for s in tools.TOOL_SPECS if s["function"]["name"] == "request_booking")
    params = spec["function"]["parameters"]
    note("request_booking 的入参（模型能填的只有这几项）：")
    detail("properties = " + json.dumps(list(params["properties"]), ensure_ascii=False))
    detail(f"required   = {params['required']}   additionalProperties = {params['additionalProperties']}")
    detail("没有 device_id、没有时段、没有日期 —— 不是忘了加，是刻意不加。")
    print()

    # 2.2 校验通过 → 签发一次性凭证
    v = await tools.execute(
        "validate_booking",
        {"device_id": device["id"], "slot_date": tomorrow, "slot_starts": ["08:00", "10:00"]},
        ctx,
    )
    if not v.ok:
        note(f"校验未通过：{v.error_code} {v.data}")
        return 1
    validation_id = v.data["validation_id"]
    fact("校验结果", "通过")
    fact("凭证", validation_id)
    fact("有效期", f"{v.data['expires_in_seconds']} 秒，用一次即废")
    fact("确认后剩余额度", f"{v.data['quota_left_after']} 格")

    # 2.3 绕过拦截直接调 request_booking：执行器本身就会炸
    print()
    note("假设模型无视提示词，直接调用 request_booking 想立刻建单：")
    r = await tools.execute("request_booking", {"validation_id": validation_id}, ctx)
    fact("工具返回", f"ok={r.ok}  error_code={r.error_code}")
    detail("执行器里是一行防御性断言：模型永远不该走到这里。")
    detail(f"harness 判定 is_side_effect('request_booking') = {tools.is_side_effect('request_booking')}")
    detail("→ 真实运行时它在 harness 里就被拦住，转成「待确认」，不会调用执行器。")

    # 2.4 拦截时做的事：校验凭证、生成给人看的摘要（不写业务数据）
    plan = await confirm.prepare_request_booking(ctx, validation_id)
    pending_id = await db.create_pending(
        session_id=sid, run_id=run_id, user_id=zhang["id"],
        action="request_booking", summary=plan, validation_id=validation_id,
    )
    print()
    note("拦截后生成确认卡片（此时数据库里还没有这单）：")
    fact("卡片标题", plan["title"])
    fact("设备", f"{plan['asset_no']} {plan['device_name']}")
    fact("时段", f"{plan['slot_date']} {plan['slot_text']}（{plan['count']} 格）")
    fact("后果提示", plan["effect"])

    # -------------------------------------------------------------------------
    act(3, "用户点「确认」：唯一真正写库的入口")
    # -------------------------------------------------------------------------
    res = await confirm.confirm_pending(pending_id, zhang)
    fact("执行结果", res["message"])
    booking_id = res["bookings"][0]["id"]
    for b in res["bookings"]:
        detail(f"{b['id'][:8]}…  {b['asset_no']} {b['slot_date']} {b['slot_start']}–{b['slot_end']}")
    detail("这一步走 booking_service.place_booking，与 REST 下单是同一条路径。")

    # -------------------------------------------------------------------------
    act(4, "连点两下：幂等闸门")
    # -------------------------------------------------------------------------
    try:
        await confirm.confirm_pending(pending_id, zhang)
        note("第二次确认竟然成功了 —— 这是 bug")
        return 1
    except ApiError as e:
        fact("第二次确认", f"{e.code}  {e.message}")
    detail("同一张卡片点两次不会建出两单：pending 的原子 claim + 凭证的一次性消费，两道闸门。")

    # -------------------------------------------------------------------------
    act(5, "越权：换个人来取消张伟的单")
    # -------------------------------------------------------------------------
    ctx_li = tools.ToolContext(
        user=li, session_id=sid, run_id=str(uuid.uuid4()), trace_id=trace_id
    )
    note("李娜（1002）走 AI 路径，想取消张伟的预约：")
    r = await tools.execute("cancel_booking", {"booking_id": booking_id}, ctx_li)
    fact("工具层", f"ok={r.ok}  error_code={r.error_code}")
    detail("写工具的执行体是防御性断言，直接调也建不出单。")
    detail("正常运行时 harness 见到 side_effect 标记就转「待确认」，根本走不到执行器。")

    try:
        await confirm.prepare_cancel(ctx_li, booking_id)
        note("prepare_cancel 竟然通过了 —— 这是 bug")
        return 1
    except ApiError as e:
        fact("确认卡片生成", f"{e.code}  {e.message}")
        detail("摘要都生成不出来，李娜根本看不到「确认」按钮。")

    try:
        await booking_service.cancel(li, booking_id)
        note("服务层竟然允许了 —— 这是 bug")
        return 1
    except ApiError as e:
        fact("服务层（REST 直接调）", f"{e.code}  {e.message}")
    detail("越权判定在服务层，所以工具层和 REST 两条路都挡得住，不靠提示词求模型。")

    # -------------------------------------------------------------------------
    act(6, "本人取消 → 时段立刻释放")
    # -------------------------------------------------------------------------
    before = await booking_service.device_slots(device["id"], tomorrow, zhang["id"])
    fact("取消前 08:00", next(s["state"] for s in before["slots"] if s["start"] == "08:00"))

    await booking_service.cancel(zhang, booking_id, ip="127.0.0.1")

    after = await booking_service.device_slots(device["id"], tomorrow, zhang["id"])
    fact("取消后 08:00", next(s["state"] for s in after["slots"] if s["start"] == "08:00"))
    fact("另一格 10:00", next(s["state"] for s in after["slots"] if s["start"] == "10:00"))
    detail("取消只释放这一格；同一单的其它格不受影响。")

    # -------------------------------------------------------------------------
    act(7, "审计：这单发生过什么，可以完整还原")
    # -------------------------------------------------------------------------
    timeline = await db.booking_timeline(booking_id)
    note(f"按 target 查日志，共 {len(timeline)} 条（新→旧）：")
    print()
    for row in timeline:
        after_json = json.loads(row["after_json"]) if row.get("after_json") else {}
        label = short(json.dumps(after_json, ensure_ascii=False), 44)
        print(f"   {row['created_at'][11:19]}  {row['action']:<18} {row['actor_label']:<10} {label}")
    detail("op_logs 只有 INSERT 与 SELECT，代码里没有 UPDATE / DELETE —— 日志不可改（R07）。")

    pendings = await db.list_pendings_by_run(run_id)
    print()
    fact("本次运行的确认动作", f"{len(pendings)} 条")
    for p in pendings:
        detail(f"{p['action']}  status={p['status']}")

    # -------------------------------------------------------------------------
    print()
    rule("═")
    print(" 演示结束。以上每一步都没有绕过服务层，也没有让模型自己写库。")
    print(" 网页版的同一批数据出现在：员工端 /bookings、管理端 /admin/logs、/admin/audit")
    rule("═")
    print()
    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
