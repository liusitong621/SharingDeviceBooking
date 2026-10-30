"""
时段规则的单元测试。

这些用例存在的理由很简单：时段边界是这类系统最容易回归的地方，
而"现在几点"如果不注入，就只能靠人在特定时间点手工点页面。
所以 slots.py 里每个函数都接收 now 参数，这里可以把时间当参数随便造。
"""

from __future__ import annotations

import unittest
from datetime import datetime, timedelta, timezone

from app.domain import slots as S

TZ = timezone(timedelta(hours=8))


def at(y, m, d, hh, mm=0):
    return datetime(y, m, d, hh, mm, tzinfo=TZ)


class TestSlotDefinition(unittest.TestCase):
    def test_six_slots_between_8_and_20(self):
        self.assertEqual(len(S.SLOTS), 6)
        self.assertEqual(S.SLOTS[0], ("08:00", "10:00"))
        self.assertEqual(S.SLOTS[-1], ("18:00", "20:00"))

    def test_slot_end_lookup(self):
        self.assertEqual(S.slot_end_of("08:00"), "10:00")
        self.assertEqual(S.slot_end_of("18:00"), "20:00")
        self.assertIsNone(S.slot_end_of("09:00"))   # 非整点起点不合法
        self.assertIsNone(S.slot_end_of("20:00"))   # 20:00 之后没有格子

    def test_boundary_adjacent_are_different_starts(self):
        """边界相接不算冲突的本质：10:00 和 12:00 是两个不同的 key。"""
        starts = [s for s, _ in S.SLOTS]
        self.assertIn("10:00", starts)
        self.assertIn("12:00", starts)
        self.assertEqual(len(set(starts)), 6)


class TestDateRange(unittest.TestCase):
    now = at(2026, 10, 30, 10, 17)

    def test_today_ok(self):
        ok, _ = S.check_date("2026-10-30", self.now)
        self.assertTrue(ok)

    def test_yesterday_rejected(self):
        ok, reason = S.check_date("2026-10-29", self.now)
        self.assertFalse(ok)
        self.assertIn("过去", reason)

    def test_within_window(self):
        ok, _ = S.check_date("2026-11-13", self.now)   # 第 14 天
        self.assertTrue(ok)

    def test_beyond_window(self):
        ok, reason = S.check_date("2026-11-14", self.now)  # 第 15 天
        self.assertFalse(ok)
        self.assertIn("14", reason)

    def test_bad_format(self):
        ok, reason = S.check_date("2026/10/30", self.now)
        self.assertFalse(ok)
        self.assertIn("YYYY-MM-DD", reason)


class TestSlotStates(unittest.TestCase):
    def test_morning_before_work(self):
        """08:00 前：6 格全部可约。"""
        states = S.slot_states("2026-10-30", set(), "available", at(2026, 10, 30, 7, 30))
        self.assertTrue(all(s["state"] == "free" for s in states))

    def test_slot_started_cannot_be_booked(self):
        """
        10:17 时，10:00–12:00 必须不可约。

        这条是改过的规则：早先按"结束时刻"判断，10:00 这格在 10:17 仍显示可约，
        用户会约到只剩 1 小时 43 分的半格。现在按"起始时刻"判断。
        """
        states = {s["start"]: s for s in S.slot_states("2026-10-30", set(), "available", at(2026, 10, 30, 10, 17))}
        self.assertEqual(states["08:00"]["state"], "passed")
        self.assertEqual(states["10:00"]["state"], "passed")
        self.assertEqual(states["12:00"]["state"], "free")
        self.assertEqual(states["18:00"]["state"], "free")

    def test_exactly_on_start_is_passed(self):
        """恰好在 10:00:00 时，10:00 这格已经算开始（>= 而非 >）。"""
        states = {s["start"]: s for s in S.slot_states("2026-10-30", set(), "available", at(2026, 10, 30, 10, 0))}
        self.assertEqual(states["10:00"]["state"], "passed")
        self.assertEqual(states["12:00"]["state"], "free")

    def test_future_date_not_affected_by_clock(self):
        """明天的 08:00 当然可约，不能被"今天是 10:17"影响。"""
        states = S.slot_states("2026-10-31", set(), "available", at(2026, 10, 30, 10, 17))
        self.assertTrue(all(s["state"] == "free" for s in states))

    def test_booked_becomes_taken(self):
        states = {s["start"]: s for s in S.slot_states("2026-10-31", {"12:00"}, "available", at(2026, 10, 30, 10, 17))}
        self.assertEqual(states["12:00"]["state"], "taken")
        self.assertEqual(states["10:00"]["state"], "free")

    def test_passed_takes_priority_over_taken(self):
        """
        已完成且被约过的格子，展示为 passed 而不是 taken。

        理由：用户关心的是"现在能不能约"，不是"历史上有人约过"。
        优先级写错会让"已过去的格子"看起来像"被别人抢了"。
        """
        states = {s["start"]: s for s in S.slot_states("2026-10-30", {"08:00"}, "available", at(2026, 10, 30, 10, 17))}
        self.assertEqual(states["08:00"]["state"], "passed")

    def test_offline_device_all_offline(self):
        states = S.slot_states("2026-10-31", set(), "offline", at(2026, 10, 30, 10, 17))
        self.assertTrue(all(s["state"] == "offline" for s in states))

    def test_next_days(self):
        days = S.next_days(at(2026, 10, 30, 23, 59), count=3)
        self.assertEqual(days, ["2026-10-30", "2026-10-31", "2026-11-01"])


if __name__ == "__main__":
    unittest.main(verbosity=2)
