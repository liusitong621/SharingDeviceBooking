"""
演示数据：用脚本化的假模型跑一次 harness，造出一个真实的待确认动作。

用法：
    .venv/bin/python scripts/demo_pending.py

会打印一个 URL，形如 http://127.0.0.1:8011/?session=<id>。
登录后打开该 URL 就能看到待确认卡片，可以真的点一下确认。

注意：它把 1001 的 must_change_pwd 置 0（否则页面会被强制改密挡住），
**只应在开发/演示环境跑**。

为什么需要它：本地没有配置大模型密钥，聊天入口走的是降级路径（直接贴原文），
不会触发智能体循环。但确认卡片是这一阶段最需要肉眼验收的东西，
所以用"假模型 + 真库"把状态造出来，前端看到的就是真实链路的产物。

它调用的是生产代码（harness.run → 真实 validate_booking → 真实 pending 落库），
唯一假的是"模型怎么决策"这一步。
"""

from __future__ import annotations

import asyncio
import json
import sys
import uuid
from datetime import timedelta

sys.path.insert(0, str(__import__("pathlib").Path(__file__).resolve().parent.parent))

from app import auth, booking_service, db  # noqa: E402
from app.agent import harness  # noqa: E402
from app.agent import tools as agent_tools  # noqa: E402
from app.agent.tools import ToolContext  # noqa: E402
from app.chat_api import _interrupt_text  # noqa: E402
from app.llm import ChatResult, ToolCall  # noqa: E402


class ScriptedLLM:
    """按脚本返回决策；脚本项可以是 ChatResult，也可以是协程函数。"""

    model_name = "scripted-demo"

    def __init__(self, script):
        self.script = list(script)

    async def chat_with_tools(self, messages, tools=None, *, tool_choice=None, **kw):
        if tool_choice == "none" or not self.script:
            return ChatResult(content="好的。", tool_calls=[], finish_reason="stop", usage={})
        item = self.script.pop(0)
        if callable(item):
            return await item(messages, tool_choice)
        return item


def tc(name, **args):
    return ToolCall(id=f"call_{name}", name=name, args_raw=json.dumps(args, ensure_ascii=False), args=args)


async def main() -> None:
    user = await db.fetch_user_by_emp_no("1001")
    if user is None:
        raise SystemExit("找不到 1001，请先启动一次服务完成种子数据")

    # 演示用：跳过首次改密，否则页面会被强制跳到 /change-password
    with db._connect() as conn:
        conn.execute("UPDATE users SET must_change_pwd = 0 WHERE id = ?", (user["id"],))

    devices = await db.list_devices()
    device = devices[0]
    tomorrow = (booking_service.now().date() + timedelta(days=1)).isoformat()

    # 挑一格当天真正空闲的
    plan = await booking_service.preflight(user["id"], device["id"], tomorrow, ["10:00"])
    free = plan.get("free_alternatives") or []
    slot = "10:00" if "10:00" in free else (free[0] if free else "10:00")

    sid = str(uuid.uuid4())
    await db.create_session(sid, "seed", "帮我约明天的频谱分析仪", user["id"])
    ctx = ToolContext(user=user, session_id=sid, run_id="", trace_id="demo")
    events = []

    async def emit(e, d):
        events.append((e, d))

    async def validate_then_request(messages, tool_choice=None):
        """第二轮：先用真工具签一张凭证，再用它发起预约（这一步会被中断）。"""
        r = await agent_tools.execute(
            "validate_booking",
            {"device_id": device["id"], "slot_date": tomorrow, "slot_starts": [slot]},
            ctx,
        )
        if not r.ok:
            raise SystemExit(f"校验未通过：{r.data}")
        return ChatResult(
            content="",
            tool_calls=[tc("request_booking", validation_id=r.data["validation_id"])],
            finish_reason="tool_calls",
            usage={},
        )

    llm = ScriptedLLM([validate_then_request])

    outcome = await harness.run(
        ctx=ctx,
        question="帮我约明天 10 点的频谱分析仪",
        history=[],
        llm=llm,
        emit=emit,
    )

    if not outcome.interrupted:
        raise SystemExit(f"预期中断，实际 status={outcome.status}")

    # 复刻 chat_api 中断分支的落库行为（用户消息 + 待确认的助手消息）
    with db._connect() as conn:
        conn.execute(
            "INSERT INTO chat_messages (id, session_id, role, content, status, created_at) "
            "VALUES (?, ?, 'user', ?, 'ok', ?)",
            (str(uuid.uuid4()), sid, "帮我约明天 10 点的频谱分析仪", db._now()),
        )
    await db.append_message(
        sid,
        "assistant",
        _interrupt_text(outcome.pending_summary),
        citations=[{"kind": "pending_action", "pending_id": outcome.pending_id, "summary": outcome.pending_summary}],
        status="ok",
    )

    print(json.dumps({
        "url": f"http://127.0.0.1:8011/?session={sid}",
        "session_id": sid,
        "pending_id": outcome.pending_id,
        "token": auth.create_token(user),
        "slot_date": tomorrow,
        "slot": slot,
        "events": [e for e, _ in events],
    }, ensure_ascii=False))


asyncio.run(main())
