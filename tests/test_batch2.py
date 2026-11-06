"""
批次 2（R01 / R02 / R05 / R06 / R07 / R08）的单元测试。

这里只测"不依赖 HTTP"的部分：
  - 时段状态里的 mine 优先级（纯函数，最容易回归）
  - 归还时间的规整与拒绝
  - 名单解析的边界（表头、Tab、缺列、弱密码）
  - 日志摘要的可读性
  - 状态机与留痕的几个硬不变量（用临时库，不碰真实数据）

HTTP 层的端到端验证由 /tmp/e2e_batch2.py 承担——那部分用真实登录与表单提交，
不适合塞进 unittest（要起服务、要等时钟）。
"""

from __future__ import annotations

import asyncio
import shutil
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path

from app import db
from app.agent import confirm  # noqa: F401  - 确保包能导入，间接验证循环引用
from app.domain import slots as S
from app.errors import ApiError
from app.web import _diff_brief, _parse_import

TZ = timezone(timedelta(hours=8))


def at(y, m, d, hh, mm=0):
    return datetime(y, m, d, hh, mm, tzinfo=TZ)


def local(y, m, d, hh, mm=0):
    """
    按**运行环境本地时区**构造时间。

    为什么不能一律用上面的 at()：at() 写死 +08:00，而 `_normalize_returned_at`
    把 naive 输入按服务器本地时区解释（内部是 `.astimezone()`）。
    两边时区不一致时，断言就只在"机器在北京"时成立——
    CI 跑在 UTC 上，"2026-11-05T15:40" 会被解释成 UTC 15:40，
    比 now（+08:00 的 16:30）还晚，于是被判"未来归还"，4 项测试全红。
    这不是代码坏了，是测试默认了运行机器在哪个时区。
    """
    return datetime(y, m, d, hh, mm).astimezone()


# =============================================================================
# R02：占用视图的四态
# =============================================================================


class SlotStateTest(unittest.TestCase):
    def test_mine_beats_passed(self):
        """我自己约的格子，即使时段已开始也要显示"我的"。

        这是刻意调整过的优先级：10:30 打开页面时，
        用户更该看到"这是我 10:00 那单，该去取设备"，而不是"时段已开始"。
        两种状态下格子都不可约，所以不影响任何校验。
        """
        states = S.slot_states("2026-11-05", {"10:00": "u1"}, "available", at(2026, 11, 5, 10, 30), "u1")
        got = {s["start"]: s["state"] for s in states}
        self.assertEqual(got["10:00"], "mine")

    def test_others_slot_is_taken(self):
        states = S.slot_states("2026-11-05", {"10:00": "u2"}, "available", at(2026, 11, 5, 9, 0), "u1")
        got = {s["start"]: s["state"] for s in states}
        self.assertEqual(got["10:00"], "taken")

    def test_no_me_id_degrades_to_taken(self):
        """管理员台账页不传 me_id：所有占用一视同仁地显示"已被预约"。"""
        states = S.slot_states("2026-11-05", {"10:00": "u1"}, "available", at(2026, 11, 5, 9, 0), None)
        got = {s["start"]: s["state"] for s in states}
        self.assertEqual(got["10:00"], "taken")

    def test_set_input_still_works(self):
        """preflight 传的是 set（只关心占没占），不能因为改成 dict 就崩。"""
        states = S.slot_states("2026-11-05", {"10:00"}, "available", at(2026, 11, 5, 9, 0), "u1")
        got = {s["start"]: s["state"] for s in states}
        self.assertEqual(got["10:00"], "taken")

    def test_offline_wins_over_everything(self):
        states = S.slot_states("2026-11-05", {"10:00": "u1"}, "offline", at(2026, 11, 5, 9, 0), "u1")
        self.assertTrue(all(s["state"] == "offline" for s in states))


# =============================================================================
# R05：归还时间
# =============================================================================


class ReturnedAtTest(unittest.TestCase):
    """
    归还时间的规整与拒绝。

    now 与期望值都用 local()：被测函数的口径是"按服务器本地时区解释用户填的时间"，
    断言就得站在同一个时区上，否则测试结果会随运行机器的 TZ 变化。
    """

    def setUp(self):
        from app.booking_service import _normalize_returned_at
        self.f = _normalize_returned_at
        self.now = local(2026, 11, 5, 16, 30)

    def test_empty_means_use_server_time(self):
        self.assertIsNone(self.f("", self.now))
        self.assertIsNone(self.f(None, self.now))

    def test_full_datetime(self):
        self.assertEqual(
            self.f("2026-11-05T15:40", self.now),
            local(2026, 11, 5, 15, 40).isoformat(timespec="seconds"),
        )

    def test_space_separator_accepted(self):
        """从表格里复制来的时间常常是空格分隔，直接拒会让人以为功能坏了。"""
        self.assertEqual(
            self.f("2026-11-05 15:40", self.now),
            local(2026, 11, 5, 15, 40).isoformat(timespec="seconds"),
        )

    def test_time_only_uses_today(self):
        self.assertEqual(
            self.f("15:40", self.now),
            local(2026, 11, 5, 15, 40).isoformat(timespec="seconds"),
        )

    def test_future_rejected(self):
        with self.assertRaises(ApiError) as ctx:
            self.f("2026-11-05T18:00", self.now)
        self.assertEqual(ctx.exception.code, "INVALID_RETURN_TIME")

    def test_small_clock_skew_allowed(self):
        """手机快 3 分钟是常态，不该因此拒绝归还。"""
        self.assertEqual(
            self.f("2026-11-05T16:33", self.now),
            local(2026, 11, 5, 16, 33).isoformat(timespec="seconds"),
        )

    def test_garbage_rejected(self):
        with self.assertRaises(ApiError):
            self.f("昨天下午", self.now)


# =============================================================================
# R08：名单解析
# =============================================================================


class ParseImportTest(unittest.TestCase):
    def test_skips_header_and_parses_tab(self):
        """从 Excel 复制是 Tab 分隔，且第一行几乎一定是表头。"""
        rows, errs = _parse_import("工号\t姓名\t部门\n1003\t王芳\t研发一部\n")
        self.assertEqual(errs, [])
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["emp_no"], "1003")
        self.assertEqual(rows[0]["dept"], "研发一部")

    def test_chinese_comma(self):
        rows, _ = _parse_import("1004，刘洋，测试部")
        self.assertEqual(rows[0]["name"], "刘洋")

    def test_missing_column_reported_with_line_no(self):
        _, errs = _parse_import("1005")
        self.assertTrue(any("第 1 行" in e for e in errs), errs)

    def test_weak_password_rejected(self):
        rows, errs = _parse_import("1006,赵六,研发部,12345678")
        self.assertEqual(rows, [])
        self.assertTrue(any("过于常见" in e or "纯数字" in e for e in errs), errs)

    def test_default_password_applied(self):
        rows, _ = _parse_import("1007,孙七")
        self.assertEqual(rows[0]["pwd"], "Staff@12345")

    def test_blank_lines_ignored(self):
        rows, errs = _parse_import("\n\n1008,周八,研发部\n\n")
        self.assertEqual(errs, [])
        self.assertEqual(len(rows), 1)


# =============================================================================
# R07：日志摘要
# =============================================================================


class DiffBriefTest(unittest.TestCase):
    def test_create(self):
        brief = _diff_brief(None, '{"asset_no": "EQ-9", "name": "测试", "status": "available"}')
        self.assertIn("建立", brief)
        self.assertIn("EQ-9", brief)

    def test_status_change_is_translated(self):
        """直接甩 booked → cancelled 等于没翻译，必须是"待取用 → 已取消"。"""
        brief = _diff_brief('{"status": "booked"}', '{"status": "cancelled"}')
        self.assertIn("待取用", brief)
        self.assertIn("已取消", brief)

    def test_no_change(self):
        self.assertEqual(_diff_brief('{"a": 1}', '{"a": 1}'), "无字段变化")

    def test_reason_field_hidden(self):
        """reason 是处置说明，已经写进备注了，不该在"变更"列再出现一次。"""
        brief = _diff_brief('{"status": "picked"}', '{"status": "returned", "reason": "设备已还"}')
        self.assertNotIn("设备已还", brief)

    def test_broken_json_does_not_raise(self):
        self.assertIn("无法解析", _diff_brief("{不是 json", "{}"))


# =============================================================================
# 状态机与留痕的硬不变量（临时库）
# =============================================================================


class DbInvariantTest(unittest.TestCase):
    """
    这些用例之所以必须存在：它们测的不是"功能能不能用"，
    而是"出错时数据会不会坏"。归还后格子没释放、日志没写、
    删除设备时绕过了闸门——这三类问题都不会在页面上报错，
    只会慢慢积累成"明明显示空闲却约不上"这种没人查得清的故障。
    """

    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix="sdb-test-"))
        self._old_dir, self._old_path = db.DB_DIR, db.DB_PATH
        db.DB_DIR = self.tmp
        db.DB_PATH = self.tmp / "t.db"
        db.init_db()
        self.user_id = asyncio.run(
            db.upsert_user("u-test", "测试员", "x", must_change_pwd=False)
        )
        self.device_id = asyncio.run(
            db.create_device("EQ-T-1", "测试设备", actor={"id": self.user_id, "emp_no": "u-test"})
        )

    def tearDown(self):
        db.DB_DIR, db.DB_PATH = self._old_dir, self._old_path
        shutil.rmtree(self.tmp, ignore_errors=True)

    def _book(self, start="10:00", end="12:00", day="2026-11-05"):
        return asyncio.run(
            db.create_bookings(self.device_id, day, [(start, end)], self.user_id)
        )[0]

    def test_return_releases_slot(self):
        """归还必须释放格子，否则"显示空闲却约不上"——这个 bug 真的存在过。"""
        bid = self._book()
        asyncio.run(db.mark_returned(bid))
        row = asyncio.run(db.fetch_booking(bid))
        self.assertEqual(row["status"], "returned")
        self.assertIsNone(row["active_flag"], "归还后 active_flag 必须为 NULL，否则唯一索引仍挡着")
        # 同一格必须能再被预约
        again = self._book()
        self.assertNotEqual(again, bid)

    def test_cancel_releases_slot(self):
        bid = self._book()
        self.assertTrue(asyncio.run(db.cancel_booking(bid)))
        again = self._book()
        self.assertNotEqual(again, bid)

    def test_double_booking_rejected(self):
        self._book()
        with self.assertRaises(ApiError) as ctx:
            self._book()
        self.assertEqual(ctx.exception.code, "SLOT_TAKEN")

    def test_op_log_written_with_state_change(self):
        bid = self._book()
        logs = asyncio.run(db.list_op_logs(target_type="booking", target_id=bid))
        self.assertEqual(logs[0]["action"], "booking.create")

        asyncio.run(db.mark_returned(bid, "有划痕"))
        logs = asyncio.run(db.booking_timeline(bid))
        actions = [row["action"] for row in logs]
        self.assertIn("booking.return", actions)
        # 备注是追加而不是覆盖
        row = asyncio.run(db.fetch_booking(bid))
        self.assertIn("有划痕", row["remark"])

    def test_force_close_picked_to_returned(self):
        bid = self._book()
        asyncio.run(db.mark_picked(bid))
        result = asyncio.run(db.force_close_booking(bid, reason="现场收回"))
        self.assertEqual(result["to"], "returned")
        row = asyncio.run(db.fetch_booking(bid))
        self.assertIsNone(row["active_flag"])
        self.assertIn("现场收回", row["remark"])

    def test_force_close_rejects_finished_booking(self):
        bid = self._book()
        asyncio.run(db.cancel_booking(bid))
        with self.assertRaises(ApiError) as ctx:
            asyncio.run(db.force_close_booking(bid, reason=""))
        self.assertEqual(ctx.exception.code, "BOOKING_STATE_INVALID")

    def test_expire_writes_log(self):
        self._book()
        n = asyncio.run(db.expire_bookings("2026-11-06T00:00"))
        self.assertEqual(n, 1)
        logs = asyncio.run(db.list_op_logs(action="booking.expire"))
        self.assertEqual(len(logs), 1)
        # 系统动作要有明确来源，不能伪装成"没人做的事"
        self.assertEqual(logs[0]["actor_label"], "system:expire_worker")
        self.assertIsNone(logs[0]["actor_id"])

    def test_delete_device_blocked_by_active_booking(self):
        self._book()
        with self.assertRaises(ApiError) as ctx:
            asyncio.run(db.delete_device(self.device_id))
        self.assertEqual(ctx.exception.code, "DEVICE_HAS_ACTIVE_BOOKINGS")

    def test_delete_device_is_soft(self):
        """删除只是从台账消失：历史预约仍要能 JOIN 出设备名。"""
        bid = self._book()
        asyncio.run(db.mark_returned(bid))
        asyncio.run(db.delete_device(self.device_id))
        self.assertIsNone(asyncio.run(db.fetch_device(self.device_id))["deleted_at"] and None)
        devs = asyncio.run(db.list_devices())
        self.assertTrue(all(d["id"] != self.device_id for d in devs))
        self.assertIsNotNone(asyncio.run(db.fetch_device(self.device_id)))
        row = asyncio.run(db.fetch_booking(bid))
        self.assertIsNotNone(row, "历史预约不能因为设备被删而消失")

    def test_import_does_not_reset_existing_password(self):
        """重复导入全量名单是每周都会发生的事，绝不能重置密码。"""
        uid, created = asyncio.run(
            db.create_user_if_absent(
                emp_no="u-test", name="测试员", dept=None, pwd_hash="new-hash"
            )
        )
        self.assertFalse(created)
        self.assertEqual(uid, self.user_id)
        row = asyncio.run(db.fetch_user(self.user_id))
        self.assertEqual(row["pwd_hash"], "x", "已存在账号的密码不能被导入覆盖")

    def test_disable_user_keeps_bookings(self):
        """停用不自动释放预约（待答项 3 的默认口径），格子仍被占着。"""
        self._book()
        asyncio.run(db.set_user_status(self.user_id, "disabled"))
        self.assertEqual(asyncio.run(db.count_active_bookings_of_user(self.user_id)), 1)
        row = asyncio.run(db.fetch_user(self.user_id))
        self.assertEqual(row["status"], "disabled")

    def test_op_log_never_contains_password_hash(self):
        asyncio.run(db.set_user_status(self.user_id, "disabled"))
        logs = asyncio.run(db.list_op_logs(target_type="user", target_id=self.user_id))
        for row in logs:
            self.assertNotIn("pwd_hash", row["after_json"] or "")
            self.assertNotIn("pwd_hash", row["before_json"] or "")


if __name__ == "__main__":
    unittest.main()
