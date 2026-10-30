"""
Harness 执行循环的测试。

关键取舍：**不用真实大模型**，用一个按脚本返回决策的 FakeLLM。

理由很实际：这个循环要验证的是"5 轮上限、中断时机、凭证校验、重复调用保护"——
这些全是**控制流**问题，与模型聪明与否无关。用真实 API 测控制流，
会得到三个坏结果：慢、不确定（同一输入两次决策可能不同）、贵。

反过来，模型"答得好不好"这类问题，测试也答不了，那是阶段 5 的评估集要干的事。
分清楚哪些能用测试锁住、哪些不能，比多写测试更重要。
"""

from __future__ import annotations

import asyncio
import json
import shutil
import tempfile
import unittest
from datetime import timedelta
from pathlib import Path

from app import booking_service, db
from app.agent import confirm, harness
from app.agent.tools import ToolContext
from app.errors import ApiError
from app.llm import ChatResult, ToolCall

DEVICE_ID = "22222222-2222-2222-2222-222222222222"
SESSION_ID = "33333333-3333-3333-3333-333333333333"


def tc(name: str, **args) -> ToolCall:
    """构造一个工具调用。args_raw 要和 args 一致，模拟真实协议。"""
    return ToolCall(
        id=f"call_{name}",
        name=name,
        args_raw=json.dumps(args, ensure_ascii=False),
        args=args,
    )


def say(text: str) -> ChatResult:
    return ChatResult(content=text, tool_calls=[], finish_reason="stop", usage={})


def use(*calls: ToolCall) -> ChatResult:
    return ChatResult(content="", tool_calls=list(calls), finish_reason="tool_calls", usage={})


class FakeLLM:
    """
    按脚本依次返回决策。

    脚本项可以是 ChatResult，也可以是一个接收 (messages, tool_choice) 的函数
    ——后者用于"要根据上一轮工具结果动态决定"的场景。
    """

    model_name = "fake-model"

    def __init__(self, script: list):
        self.script = list(script)
        self.requests: list[dict] = []

    async def chat_with_tools(
        self, messages, tools=None, *, temperature=None, max_tokens=None, tool_choice=None
    ) -> ChatResult:
        self.requests.append(
            {
                "tool_choice": tool_choice,
                "has_tools": bool(tools),
                "messages": [m["role"] for m in messages],
                "last_tool_content": (
                    messages[-1]["content"] if messages and messages[-1]["role"] == "tool" else None
                ),
            }
        )
        # 强制应答时必须真的给文本，否则测不到"5 轮内必有回答"这条保证
        if tool_choice == "none":
            return say("根据目前查到的信息，明天没有完全空闲的时段。")
        if not self.script:
            return say("脚本已用尽。")
        item = self.script.pop(0)
        if callable(item):
            return item(messages, tool_choice)
        return item


class HarnessTest(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self) -> None:
        # 每个用例一个独立库文件，避免用例之间互相污染。
        self.tmp = Path(tempfile.mkdtemp(prefix="sdb_test_"))
        self._orig_path = db.DB_PATH
        self._orig_dir = db.DB_DIR
        db.DB_DIR = self.tmp
        db.DB_PATH = self.tmp / "test.db"
        db.init_db()

        # upsert_user 自己生成 uuid，必须用它返回的 id 建会话与做生意，
        # 否则外键对不上——这也是真实代码里最容易踩的一处（seed 脚本就踩过）。
        self.uid = await db.upsert_user("1001", "张三", "x", role="employee", must_change_pwd=False)
        await db.upsert_device(DEVICE_ID, "DEV-001", "工业内窥镜", "BO-200", "A 栋 3 层工具间")
        await db.create_session(SESSION_ID, "tok", "测试会话", self.uid)

        self.user = {
            "id": self.uid, "emp_no": "1001", "name": "张三",
            "role": "employee", "dept": "研发",
        }
        self.ctx = ToolContext(user=self.user, session_id=SESSION_ID, run_id="", trace_id="t1")
        self.events: list[tuple[str, dict]] = []

    async def asyncTearDown(self) -> None:
        db.DB_DIR = self._orig_dir
        db.DB_PATH = self._orig_path
        db._busy.clear()
        shutil.rmtree(self.tmp, ignore_errors=True)

    async def emit(self, event: str, data: dict) -> None:
        self.events.append((event, data))

    # ------------------------------------------------------------------ 工具

    async def _make_validation(self, slots: list[str] | None = None) -> str:
        """真实走一次 validate_booking，拿到可用凭证（不 mock，要验证的就是它）。"""
        date = (booking_service.now().date() + timedelta(days=1)).isoformat()
        from app.agent import tools

        result = await tools.execute(
            "validate_booking",
            {"device_id": DEVICE_ID, "slot_date": date, "slot_starts": slots or ["10:00"]},
            self.ctx,
        )
        self.assertTrue(result.ok, f"校验应通过，实际：{result.data}")
        return result.data["validation_id"]

    # ------------------------------------------------------------- 基本循环

    async def test_direct_answer_no_tool(self):
        """不需要工具时，一轮就结束，且只调了一次模型。"""
        llm = FakeLLM([say("你好，我是设备借用助手。")])
        outcome = await harness.run(
            ctx=self.ctx, question="你好", history=[], llm=llm, emit=self.emit
        )
        self.assertEqual(outcome.status, "answered")
        self.assertEqual(outcome.rounds, 1)
        self.assertIn("设备借用助手", outcome.answer)
        self.assertEqual(len(llm.requests), 1)

    async def test_tool_then_answer(self):
        """模型先查时段、再回答：两轮，且工具结果确实回填给了模型。"""
        tomorrow = (booking_service.now().date() + timedelta(days=1)).isoformat()
        llm = FakeLLM([use(tc("find_available_slots", date=tomorrow)), say("明天有 6 个空闲时段。")])
        outcome = await harness.run(
            ctx=self.ctx, question="明天有空的吗", history=[], llm=llm, emit=self.emit
        )
        self.assertEqual(outcome.rounds, 2)
        self.assertIn("6 个空闲", outcome.answer)
        # 第二次请求的最后一条必须是 tool 消息，否则模型看不到工具结果
        self.assertIsNotNone(llm.requests[1]["last_tool_content"])
        self.assertIn("DEV-001", llm.requests[1]["last_tool_content"])

        steps = await db.list_steps(outcome.run_id)
        self.assertEqual(len(steps), 1)
        self.assertEqual(steps[0]["tool_name"], "find_available_slots")
        self.assertEqual(steps[0]["status"], "ok")

    # ------------------------------------------------------------- 中断

    async def test_interrupt_before_write(self):
        """
        核心用例：模型调 request_booking 时**不能执行**。

        断言三件事：循环停了、pending 落库了、bookings 表仍然是空的。
        最后一条是关键——如果它建了单，前面两条断言都通过也没意义。
        """
        vid = await self._make_validation()
        llm = FakeLLM([use(tc("request_booking", validation_id=vid)), say("不该走到这里")])

        outcome = await harness.run(
            ctx=self.ctx, question="帮我约明天 10 点", history=[], llm=llm, emit=self.emit
        )

        self.assertTrue(outcome.interrupted)
        self.assertEqual(outcome.status, "awaiting_confirm")
        self.assertIsNotNone(outcome.pending_id)
        # 只调了一次模型：中断后不再继续决策
        self.assertEqual(len(llm.requests), 1)

        pending = await db.fetch_pending(outcome.pending_id)
        self.assertEqual(pending["status"], "pending")
        self.assertEqual(pending["action"], "request_booking")

        rows = await db.list_user_bookings(self.uid)
        self.assertEqual(rows, [], "中断时绝不能落单")

        # 前端必须收到 confirm_required 事件，否则用户看不到确认卡片
        kinds = [e for e, _ in self.events]
        self.assertIn("confirm_required", kinds)

    async def test_hallucinated_validation_does_not_interrupt(self):
        """
        模型编一个不存在的凭证：不能给用户发确认卡片，要把错误回填让它重新校验。

        这是很容易漏的一条——如果照样中断，用户看到"确认预约"点下去必然失败。
        """
        llm = FakeLLM(
            [
                use(tc("request_booking", validation_id="99999999-0000-0000-0000-000000000000")),
                say("凭证无效，我重新帮你查一下。"),
            ]
        )
        outcome = await harness.run(
            ctx=self.ctx, question="帮我约", history=[], llm=llm, emit=self.emit
        )

        self.assertFalse(outcome.interrupted)
        self.assertEqual(outcome.status, "answered")
        self.assertEqual(len(llm.requests), 2, "应继续第二轮，而不是中断")
        # 第二轮请求里应能看到凭证错误的回填
        self.assertIn("VALIDATION_NOT_FOUND", llm.requests[1]["last_tool_content"] or "")

        steps = await db.list_steps(outcome.run_id)
        self.assertEqual(steps[0]["status"], "error")
        self.assertEqual(steps[0]["error_code"], "VALIDATION_NOT_FOUND")

    async def test_other_users_validation_rejected(self):
        """别人的凭证不能用。这条防的是"猜到别人 id"的情形。"""
        vid = await self._make_validation()
        # 造一个真用户，再把凭证改到它名下：模拟"猜到别人凭证 id"的情形。
        # 直接塞一个不存在的 uuid 会先被外键拦住，测不到想测的那一层。
        other = await db.upsert_user("1002", "李四", "x", must_change_pwd=False)
        with db._connect() as conn:  # noqa: SLF001 - 测试里直接改库比造一整套流程快
            conn.execute("UPDATE booking_validations SET user_id = ? WHERE id = ?", (other, vid))

        with self.assertRaises(ApiError) as ctx:
            await confirm.prepare_request_booking(self.ctx, vid)
        self.assertEqual(ctx.exception.code, "VALIDATION_MISMATCH")

    # ------------------------------------------------------------- 轮次与保护

    async def test_max_rounds_forces_answer(self):
        """
        5 轮上限。

        模型每轮都换参数调工具（避免触发重复保护），验证：
        第 5 轮被强制 tool_choice=none，且总调用次数恰好等于 MAX_ROUNDS。
        """
        tomorrow = (booking_service.now().date() + timedelta(days=1)).isoformat()
        script = [
            use(tc("find_available_slots", date=tomorrow, asset_no=f"DEV-00{i}")) for i in range(1, 6)
        ]
        llm = FakeLLM(script)

        outcome = await harness.run(
            ctx=self.ctx, question="一直查", history=[], llm=llm, emit=self.emit
        )

        self.assertEqual(len(llm.requests), harness.MAX_ROUNDS)
        self.assertEqual(llm.requests[-1]["tool_choice"], "none", "最后一轮必须强制应答")
        self.assertEqual(outcome.status, "answered")

    async def test_repeated_call_is_skipped(self):
        """
        同工具同参数第 3 次调用被跳过。

        模型失败后重复同样调用是最常见的死循环形态，
        没有这道闸门，5 轮会被一次徒劳重试全部吃掉。
        """
        tomorrow = (booking_service.now().date() + timedelta(days=1)).isoformat()
        same = tc("find_available_slots", date=tomorrow)
        llm = FakeLLM([use(same), use(same), use(same)])
        outcome = await harness.run(
            ctx=self.ctx, question="重复", history=[], llm=llm, emit=self.emit
        )

        steps = await db.list_steps(outcome.run_id)
        self.assertEqual([s["status"] for s in steps], ["ok", "ok", "skipped"])

    async def test_bad_json_args_fed_back(self):
        """参数不是合法 JSON：回填错误而不是崩掉整个循环。"""
        bad = ToolCall(id="c1", name="find_available_slots", args_raw='{"date": "2026-10-31",}', args=None, parse_error="尾逗号")
        llm = FakeLLM([use(bad), say("我换个方式。")])
        outcome = await harness.run(
            ctx=self.ctx, question="坏参数", history=[], llm=llm, emit=self.emit
        )
        self.assertEqual(outcome.status, "answered")
        steps = await db.list_steps(outcome.run_id)
        self.assertEqual(steps[0]["error_code"], "INVALID_ARGUMENT")

    # ------------------------------------------------------------- 确认执行

    async def test_confirm_creates_booking(self):
        """用户点确认后才真正建单，且状态、消息都落库。"""
        vid = await self._make_validation(["10:00"])
        llm = FakeLLM([use(tc("request_booking", validation_id=vid))])
        outcome = await harness.run(
            ctx=self.ctx, question="帮我约", history=[], llm=llm, emit=self.emit
        )

        result = await confirm.confirm_pending(outcome.pending_id, self.user)
        self.assertTrue(result["ok"])
        self.assertIn("预约成功", result["message"])

        rows = await db.list_user_bookings(self.uid)
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["slot_start"], "10:00")

        p = await db.fetch_pending(outcome.pending_id)
        self.assertEqual(p["status"], "confirmed")

        # 确认结果必须进对话历史，否则刷新页面就查不到这单的由来
        msgs = await db.list_messages(SESSION_ID)
        self.assertTrue(any("预约成功" in m["content"] for m in msgs))

    async def test_confirm_twice_second_time_rejected(self):
        """
        连点两下不能建两单。

        第一道闸门是 pending 的原子 claim，第二道是凭证的原子 consume。
        这里验证第一道：第二次确认直接被拒。
        """
        vid = await self._make_validation(["10:00"])
        llm = FakeLLM([use(tc("request_booking", validation_id=vid))])
        outcome = await harness.run(
            ctx=self.ctx, question="帮我约", history=[], llm=llm, emit=self.emit
        )

        await confirm.confirm_pending(outcome.pending_id, self.user)
        with self.assertRaises(ApiError) as ctx:
            await confirm.confirm_pending(outcome.pending_id, self.user)
        self.assertEqual(ctx.exception.code, "PENDING_ALREADY_DECIDED")

        rows = await db.list_user_bookings(self.uid)
        self.assertEqual(len(rows), 1, "绝不能出现第二条")

    async def test_reject_creates_nothing(self):
        """用户点取消：什么都不执行，但状态要记下来。"""
        vid = await self._make_validation(["12:00"])
        llm = FakeLLM([use(tc("request_booking", validation_id=vid))])
        outcome = await harness.run(
            ctx=self.ctx, question="帮我约", history=[], llm=llm, emit=self.emit
        )

        await confirm.reject_pending(outcome.pending_id, self.user)
        self.assertEqual(await db.list_user_bookings(self.uid), [])
        p = await db.fetch_pending(outcome.pending_id)
        self.assertEqual(p["status"], "rejected")

    async def test_confirm_after_slot_taken_marks_failed(self):
        """
        确认前那一格被别人抢走：要如实报错并把 pending 标成 failed。

        不能回滚成 pending——用户手里的凭证已消费，卡片又能点也只会再失败一次。
        """
        vid = await self._make_validation(["14:00"])
        llm = FakeLLM([use(tc("request_booking", validation_id=vid))])
        outcome = await harness.run(
            ctx=self.ctx, question="帮我约", history=[], llm=llm, emit=self.emit
        )

        # 模拟"另一个人先约走了"。要用真实存在的用户 id，否则撞的是外键而不是唯一索引。
        tomorrow = (booking_service.now().date() + timedelta(days=1)).isoformat()
        other = await db.upsert_user("1003", "王五", "x", must_change_pwd=False)
        await db.create_bookings(DEVICE_ID, tomorrow, [("14:00", "16:00")], other)

        with self.assertRaises(ApiError) as ctx:
            await confirm.confirm_pending(outcome.pending_id, self.user)
        self.assertEqual(ctx.exception.code, "SLOT_TAKEN")
        p = await db.fetch_pending(outcome.pending_id)
        self.assertEqual(p["status"], "failed")

    async def test_expired_pending_rejected(self):
        """过期的确认卡片不能再执行。"""
        vid = await self._make_validation(["16:00"])
        llm = FakeLLM([use(tc("request_booking", validation_id=vid))])
        outcome = await harness.run(
            ctx=self.ctx, question="帮我约", history=[], llm=llm, emit=self.emit
        )

        with db._connect() as conn:  # noqa: SLF001
            conn.execute(
                "UPDATE pending_actions SET expires_at = '2020-01-01T00:00:00+08:00' WHERE id = ?",
                (outcome.pending_id,),
            )

        with self.assertRaises(ApiError) as ctx:
            await confirm.confirm_pending(outcome.pending_id, self.user)
        self.assertEqual(ctx.exception.code, "PENDING_EXPIRED")

    # ------------------------------------------------------- SSE 接线（集成）

    async def test_sse_interrupt_frames(self):
        """
        验证 chat_api 把 harness 的事件翻译成 SSE 帧这一层。

        这是 harness 与 HTTP 之间唯一的一处接缝，也是最容易出错的地方：
        harness 发 "confirm_required"，接口层必须原样转成同名 SSE 事件，
        否则前端永远等不到卡片。单元测试覆盖不到这里，只能真跑一遍生成器。
        """
        from unittest.mock import patch

        from app import chat_api
        from app.chat_api import ChatRequest, _event_stream

        vid = await self._make_validation(["18:00"])
        fake = FakeLLM([use(tc("request_booking", validation_id=vid))])

        req = ChatRequest(
            session_id=SESSION_ID, question="帮我约 18 点", history_turns=6, client_msg_id=None
        )
        db.try_lock(SESSION_ID)
        with patch.object(chat_api, "get_llm", lambda: fake):
            frames = [
                f
                async for f in _event_stream(
                    req=req, history=[], trace_id="t1", user_message_id="u1", user=self.user
                )
            ]

        events = []
        payloads = {}
        for f in frames:
            for line in f.split("\n"):
                if line.startswith("event: "):
                    events.append(line[7:].strip())
                elif line.startswith("data: "):
                    payloads.setdefault(events[-1], []).append(json.loads(line[6:]))

        # 中断必须让前端收到这三类事件，缺一个界面就是死的
        self.assertIn("confirm_required", events)
        self.assertIn("done", events)
        self.assertNotIn("error", events)

        done = payloads["done"][-1]
        self.assertTrue(done["interrupted"])
        self.assertIsNotNone(done.get("pending_id"))

        # 落库的助手消息要带 pending 元信息，否则刷新后卡片消失
        msgs = await db.list_messages(SESSION_ID)
        last = [m for m in msgs if m["role"] == "assistant"][-1]
        self.assertEqual(last["citations"][0]["kind"], "pending_action")
        self.assertIn("还没有生效", last["content"])

    async def test_sse_normal_answer(self):
        """普通问答：不应出现 confirm_required，done 里 interrupted=false。"""
        from unittest.mock import patch

        from app import chat_api
        from app.chat_api import ChatRequest, _event_stream

        fake = FakeLLM([say("每天最多可以预约 2 格。")])
        req = ChatRequest(session_id=SESSION_ID, question="几格", history_turns=6, client_msg_id=None)
        db.try_lock(SESSION_ID)
        with patch.object(chat_api, "get_llm", lambda: fake):
            frames = [
                f
                async for f in _event_stream(
                    req=req, history=[], trace_id="t2", user_message_id="u2", user=self.user
                )
            ]

        events = []
        done_payload = None
        for f in frames:
            for line in f.split("\n"):
                if line.startswith("event: "):
                    events.append(line[7:].strip())
                elif line.startswith("data: ") and events and events[-1] == "done":
                    done_payload = json.loads(line[6:])

        self.assertIn("delta", events)
        self.assertNotIn("confirm_required", events)
        self.assertFalse(done_payload["interrupted"])

        msgs = await db.list_messages(SESSION_ID)
        last = [m for m in msgs if m["role"] == "assistant"][-1]
        self.assertIn("2 格", last["content"])
        self.assertEqual(last["status"], "ok")


if __name__ == "__main__":
    unittest.main()
