"""
批次 4（对话审计）的单元测试。

只测"筛选和统计算得对不对"以及"标记会不会毁掉原始数据"。

这一类查询的 bug 有个共同特点：**看起来都对**。页面上少了当天的记录、
翻页数量对不上、状态筛选漏掉一类——都不会报错，只会让管理员得出
"最近没人提问"这种完全错误的结论。所以日期边界和状态映射必须钉住。
"""

from __future__ import annotations

import asyncio
import json
import shutil
import sqlite3
import tempfile
import unittest
from datetime import datetime, timedelta
from pathlib import Path

from app import db


def run_sync(coro):
    return asyncio.run(coro)


class AuditTestBase(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix="sdb-audit-"))
        self._old_dir, self._old_path = db.DB_DIR, db.DB_PATH
        db.DB_DIR = self.tmp
        db.DB_PATH = self.tmp / "t.db"
        db.init_db()
        self.uid = run_sync(db.upsert_user("1001", "张伟", "x", must_change_pwd=False))
        self.uid2 = run_sync(db.upsert_user("1002", "李娜", "x", must_change_pwd=False))
        self.admin = {"id": self.uid, "emp_no": "1001"}

    def tearDown(self):
        db.DB_DIR, db.DB_PATH = self._old_dir, self._old_path
        shutil.rmtree(self.tmp, ignore_errors=True)

    def _run_row(self, *, uid=None, question="设备怎么借", status="answered", created_at=None):
        rid = run_sync(db.start_run("s-1", uid or self.uid, question, "trace-1", "test-model"))
        run_sync(db.finish_run(rid, status, rounds=2, total_ms=1500))
        if created_at:
            conn = sqlite3.connect(db.DB_PATH)
            conn.execute("UPDATE agent_runs SET created_at = ? WHERE id = ?", (created_at, rid))
            conn.commit()
            conn.close()
        return rid


class RunQueryTest(AuditTestBase):
    def test_list_returns_user_info(self):
        self._run_row()
        rows = run_sync(db.list_runs())
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["emp_no"], "1001")
        self.assertEqual(rows[0]["user_name"], "张伟")
        self.assertEqual(rows[0]["status"], "answered")

    def test_keyword_matches_emp_no_name_and_question(self):
        self._run_row(question="频谱仪怎么约")
        self._run_row(uid=self.uid2, question="热成像仪归还在哪登记")

        self.assertEqual(len(run_sync(db.list_runs(keyword="1001"))), 1)
        self.assertEqual(len(run_sync(db.list_runs(keyword="李娜"))), 1)
        self.assertEqual(len(run_sync(db.list_runs(keyword="频谱"))), 1)

    def test_status_filter(self):
        self._run_row(status="answered")
        self._run_row(status="awaiting_confirm")
        rows = run_sync(db.list_runs(status="awaiting_confirm"))
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["status"], "awaiting_confirm")

    def test_date_to_includes_whole_day(self):
        """`to=2026-11-05` 必须包含 11-05 当天的记录。

        直接写 `created_at <= '2026-11-05'` 会把当天全漏掉，因为
        '2026-11-05T10:30:00' > '2026-11-05'。这个差一天的错误
        不会报错，只会让管理员以为当天没人用过——所以必须有回归用例。
        """
        self._run_row(created_at="2026-11-05T10:30:00+08:00")
        self._run_row(created_at="2026-11-04T23:00:00+08:00")

        rows = run_sync(db.list_runs(date_from="2026-11-05", date_to="2026-11-05"))
        self.assertEqual(len(rows), 1, "选了 '到 11-05' 应包含 11-05 当天")
        self.assertTrue(rows[0]["created_at"].startswith("2026-11-05"))

    def test_date_range_excludes_older(self):
        self._run_row(created_at="2026-11-01T09:00:00+08:00")
        self._run_row(created_at="2026-11-05T09:00:00+08:00")
        rows = run_sync(db.list_runs(date_from="2026-11-05"))
        self.assertEqual(len(rows), 1)

    def test_bad_date_ignored_not_crash(self):
        """筛选框里手打一个乱日期，应该退化成"不筛"，不是 500。"""
        self._run_row()
        rows = run_sync(db.list_runs(date_to="昨天"))
        self.assertEqual(len(rows), 1)

    def test_step_and_pending_counts(self):
        rid = self._run_row()
        run_sync(db.add_step(rid, 1, 1, "search_policy", {"query": "制度"}, "3 条片段"))
        run_sync(db.add_step(rid, 2, 1, "validate_booking", {"device_id": "d1"}, "校验失败",
                             status="error", error_code="SLOT_TAKEN"))
        run_sync(db.create_pending(
            session_id="s-1", run_id=rid, user_id=self.uid,
            action="request_booking", summary={"device": "EQ-0001"},
        ))
        row = run_sync(db.list_runs())[0]
        self.assertEqual(row["step_count"], 2)
        self.assertEqual(row["error_steps"], 1)
        self.assertEqual(row["pending_count"], 1)


class SummaryTest(AuditTestBase):
    def test_counts_by_status(self):
        self._run_row(status="answered")
        self._run_row(status="answered")
        self._run_row(status="awaiting_confirm")
        self._run_row(status="failed")
        self._run_row(status="max_rounds")

        s = run_sync(db.audit_summary(days=7))
        self.assertEqual(s["total"], 5)
        self.assertEqual(s["answered"], 2)
        self.assertEqual(s["awaiting_confirm"], 1)
        self.assertEqual(s["failed"], 1)
        self.assertEqual(s["max_rounds"], 1)
        self.assertEqual(s["avg_rounds"], 2.0)

    def test_empty_db_is_zero_not_error(self):
        s = run_sync(db.audit_summary(days=7))
        self.assertEqual(s["total"], 0)
        self.assertEqual(s["avg_rounds"], 0.0)
        self.assertEqual(s["avg_ms"], 0)

    def test_old_runs_excluded(self):
        old = (datetime.now().astimezone() - timedelta(days=30)).isoformat(timespec="seconds")
        self._run_row(created_at=old)
        self._run_row()
        self.assertEqual(run_sync(db.audit_summary(days=7))["total"], 1)


class FlagTest(AuditTestBase):
    def test_flag_and_unflag(self):
        rid = self._run_row()
        run_sync(db.flag_run(rid, "疑似代约", actor=self.admin))
        row = run_sync(db.fetch_run(rid))
        self.assertEqual(row["flag_reason"], "疑似代约")
        self.assertEqual(row["flagged_by_no"], "1001")

        run_sync(db.unflag_run(rid, actor=self.admin))
        self.assertIsNone(run_sync(db.fetch_run(rid))["flag_reason"])

    def test_reflag_updates_same_row(self):
        """第二次点标记是"补充说明"，不是"标两条"。"""
        rid = self._run_row()
        run_sync(db.flag_run(rid, "第一次", actor=self.admin))
        run_sync(db.flag_run(rid, "改主意了", actor=self.admin))
        conn = sqlite3.connect(db.DB_PATH)
        n = conn.execute("SELECT COUNT(*) FROM audit_flags").fetchone()[0]
        conn.close()
        self.assertEqual(n, 1)
        self.assertEqual(run_sync(db.fetch_run(rid))["flag_reason"], "改主意了")

    def test_flag_does_not_touch_run(self):
        """标记绝不能改动原始轨迹——这是审计可信的前提。"""
        rid = self._run_row(question="原始问题")
        before = run_sync(db.fetch_run(rid))
        run_sync(db.flag_run(rid, "标记", actor=self.admin))
        after = run_sync(db.fetch_run(rid))
        for key in ("question", "status", "rounds", "total_ms", "model", "created_at"):
            self.assertEqual(before[key], after[key], f"{key} 被标记改动了")

    def test_unflag_missing_returns_false(self):
        rid = self._run_row()
        self.assertFalse(run_sync(db.unflag_run(rid, actor=self.admin)))

    def test_flags_leave_audit_trail(self):
        rid = self._run_row()
        run_sync(db.flag_run(rid, "标记理由", actor=self.admin))
        run_sync(db.unflag_run(rid, actor=self.admin))
        logs = run_sync(db.list_op_logs(target_type="run", target_id=rid))
        actions = [l["action"] for l in logs]
        self.assertIn("audit.flag", actions)
        self.assertIn("audit.unflag", actions)
        self.assertEqual(logs[0]["actor_label"], "1001")

    def test_only_flagged_filter(self):
        a = self._run_row()
        self._run_row()
        run_sync(db.flag_run(a, "看看这条", actor=self.admin))
        rows = run_sync(db.list_runs(only_flagged=True))
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["id"], a)


class PendingOfRunTest(AuditTestBase):
    def test_summary_json_parsed(self):
        rid = self._run_row()
        run_sync(db.create_pending(
            session_id="s-1", run_id=rid, user_id=self.uid,
            action="request_booking",
            summary={"asset_no": "EQ-0001", "slot_date": "2026-11-06", "slot_starts": ["10:00"]},
        ))
        rows = run_sync(db.list_pendings_by_run(rid))
        self.assertEqual(len(rows), 1)
        self.assertIsInstance(rows[0]["summary"], dict)
        self.assertEqual(rows[0]["summary"]["asset_no"], "EQ-0001")
        self.assertEqual(rows[0]["status"], "pending")

    def test_broken_summary_does_not_crash(self):
        """手工改库把 summary_json 写坏，页面不能因此 500。"""
        rid = self._run_row()
        run_sync(db.create_pending(
            session_id="s-1", run_id=rid, user_id=self.uid,
            action="request_booking", summary={"a": 1},
        ))
        conn = sqlite3.connect(db.DB_PATH)
        conn.execute("UPDATE pending_actions SET summary_json = '{坏掉的' WHERE run_id = ?", (rid,))
        conn.commit()
        conn.close()

        rows = run_sync(db.list_pendings_by_run(rid))
        self.assertEqual(rows[0]["summary"], {})

    def test_only_this_run(self):
        r1 = self._run_row()
        r2 = self._run_row()
        run_sync(db.create_pending(
            session_id="s-1", run_id=r1, user_id=self.uid,
            action="request_booking", summary={"n": 1},
        ))
        self.assertEqual(len(run_sync(db.list_pendings_by_run(r1))), 1)
        self.assertEqual(len(run_sync(db.list_pendings_by_run(r2))), 0)


class StepTraceTest(AuditTestBase):
    def test_steps_ordered_by_round_then_step(self):
        rid = self._run_row()
        run_sync(db.add_step(rid, 2, 1, "validate_booking", {}, "ok"))
        run_sync(db.add_step(rid, 1, 2, "get_device_info", {}, "ok"))
        run_sync(db.add_step(rid, 1, 1, "search_policy", {}, "ok"))
        steps = run_sync(db.list_steps(rid))
        self.assertEqual(
            [(s["round_no"], s["step_no"]) for s in steps],
            [(1, 1), (1, 2), (2, 1)],
        )

    def test_args_preserved_raw(self):
        """原始参数必须原样保留，包括非法值——那才是复盘时最有价值的信息。"""
        rid = self._run_row()
        run_sync(db.add_step(rid, 1, 1, "validate_booking",
                             {"slot_date": "2026-13-45", "slot_starts": ["10:30"]}, "日期非法",
                             status="error", error_code="INVALID_ARGUMENT"))
        step = run_sync(db.list_steps(rid))[0]
        args = json.loads(step["args_json"])
        self.assertEqual(args["slot_date"], "2026-13-45")


if __name__ == "__main__":
    unittest.main()
