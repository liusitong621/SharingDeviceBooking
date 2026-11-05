"""
批次 3（提示词模板入库）的单元测试。

只测三件真正会出事的事：

  1) **占位符写错会不会漏进去**。{max_slot}（少一个 s）不会报任何错，
     会原样出现在模型的系统提示词里，而模型很可能把它念给用户听。
     页面上还会显示"保存成功"——这种静默污染必须靠校验挡住。
  2) **模板坏了对话会不会挂**。数据库读不到、内容被手工改坏，
     都必须回落内置模板，而不是让提问返回 500。
  3) **版本只增不改**。归档、回滚、并发冲突，任何一条走偏，
     "上周回答变差是不是因为改了提示词"就再也回答不了。

真实 HTTP 流程（登录 → 编辑 → 预览 → 发布 → 回滚）由 /tmp/e2e_batch3.py 覆盖。
"""

from __future__ import annotations

import asyncio
import shutil
import sqlite3
import tempfile
import unittest
from pathlib import Path

from app import db
from app.agent import prompts
from app.errors import ApiError


# =============================================================================
# 纯函数：占位符与校验
# =============================================================================


class PlaceholderTest(unittest.TestCase):
    def test_extracts_identifier_only(self):
        text = "今天 {today_hint}，最多 {max_slots} 格"
        self.assertEqual(prompts.extract_placeholders(text), {"today_hint", "max_slots"})

    def test_json_examples_are_not_placeholders(self):
        """正文里的 JSON 示例必须原样保留，这是最容易踩的一脚。

        如果只按 `\\{.*?\\}` 粗暴匹配，{"device_id": "x"} 会被当成占位符，
        于是"提示词里不能写 JSON 示例"——而示例恰恰是最有效的约束手段。
        """
        text = '返回格式：{"device_id": "abc", "slot_date": "2026-11-05"}'
        self.assertEqual(prompts.extract_placeholders(text), set())

    def test_brace_with_space_ignored(self):
        self.assertEqual(prompts.extract_placeholders("{ today_hint }"), set())

    def test_empty_braces_ignored(self):
        self.assertEqual(prompts.extract_placeholders("空对象 {} 与 {{}}"), set())


class ValidateTest(unittest.TestCase):
    def test_unknown_placeholder_is_error(self):
        """少写一个字母是真实会发生的错，且后果是静默的。"""
        errors, _ = prompts.validate_template("每天最多 {max_slot} 格，今天是 {today_hint}")
        self.assertTrue(errors)
        self.assertIn("max_slot", errors[0])

    def test_missing_today_hint_is_only_warning(self):
        """缺日期只是排版不理想：渲染时会自动追加，功能不坏，不该拦保存。"""
        errors, warnings = prompts.validate_template("你是设备借用助手。每天最多 {max_slots} 格。")
        self.assertEqual(errors, [])
        self.assertTrue(any("today_hint" in w for w in warnings))

    def test_missing_max_slots_warns(self):
        _errors, warnings = prompts.validate_template("你是助手。{today_hint}")
        self.assertTrue(any("max_slots" in w for w in warnings))

    def test_empty_template_rejected(self):
        errors, _ = prompts.validate_template("   \n\t ")
        self.assertTrue(errors)

    def test_too_long_rejected(self):
        errors, _ = prompts.validate_template("啊" * (prompts.MAX_TEMPLATE_CHARS + 1))
        self.assertTrue(any("过长" in e for e in errors))

    def test_default_template_is_valid(self):
        """内置模板自己必须是合法的——否则一入库就报错，管理员会以为功能坏了。"""
        errors, _warnings = prompts.validate_template(prompts.DEFAULT_TEMPLATE)
        self.assertEqual(errors, [])


class RenderTest(unittest.TestCase):
    def test_placeholder_replaced(self):
        out = prompts.system_prompt("今天是 2026-11-05", "日期：{today_hint}；上限 {max_slots} 格")
        self.assertIn("今天是 2026-11-05", out)
        self.assertIn("上限 2 格", out)

    def test_missing_today_hint_appended(self):
        """没有占位符也不能让模型不知道今天几号：它一算错日期，后面全错。"""
        out = prompts.system_prompt("今天是 2026-11-05", "你是助手。")
        self.assertTrue(out.startswith("你是助手。"))
        self.assertIn("今天是 2026-11-05", out)

    def test_json_braces_survive(self):
        out = prompts.system_prompt("D", '格式 {"a": 1}，日期 {today_hint}')
        self.assertIn('{"a": 1}', out)
        self.assertNotIn("{today_hint}", out)

    def test_max_slots_is_configurable(self):
        self.assertIn("3 格", prompts.system_prompt("D", "最多 {max_slots} 格", max_slots=3))


# =============================================================================
# 入库：版本、归档、回滚、回落
# =============================================================================


class TemplateDBTest(unittest.TestCase):
    """用临时库测硬不变量，不碰 data/chat.db。"""

    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix="sdb-tpl-"))
        self._old_dir, self._old_path = db.DB_DIR, db.DB_PATH
        db.DB_DIR = self.tmp
        db.DB_PATH = self.tmp / "t.db"
        db.init_db()
        self.actor = {"id": "u-admin", "emp_no": "admin"}

    def tearDown(self):
        db.DB_DIR, db.DB_PATH = self._old_dir, self._old_path
        shutil.rmtree(self.tmp, ignore_errors=True)

    def _publish(self, content="你是助手。{today_hint}", note=None):
        return asyncio.run(
            db.publish_prompt(
                tpl_key=prompts.PROMPT_KEY, content=content, note=note, actor=self.actor
            )
        )

    def test_nothing_in_db_means_builtin(self):
        """空库是正常初始状态，不是错误。"""
        self.assertIsNone(asyncio.run(db.active_prompt(prompts.PROMPT_KEY)))

    def test_version_increments_and_only_one_active(self):
        v1 = self._publish("第一版 {today_hint}")
        v2 = self._publish("第二版 {today_hint}")
        self.assertEqual((v1["version"], v2["version"]), (1, 2))

        active = asyncio.run(db.active_prompt(prompts.PROMPT_KEY))
        self.assertEqual(active["version"], 2)
        self.assertIn("第二版", active["content"])

        # 旧版必须转成 archived，否则历史列表里会出现两个"当前"
        old = asyncio.run(db.fetch_prompt_version(prompts.PROMPT_KEY, 1))
        self.assertEqual(old["status"], "archived")

    def test_publish_writes_op_log(self):
        self._publish("第一版 {today_hint}", note="初始化")
        logs = asyncio.run(db.list_op_logs(target_type="prompt", target_id=prompts.PROMPT_KEY))
        self.assertEqual(logs[0]["action"], "prompt.publish")
        self.assertEqual(logs[0]["actor_label"], "admin")
        # 正文不进日志：日志记事实，全文留给模板表按版本查
        self.assertNotIn("第一版", logs[0]["after_json"] or "")

    def test_rollback_appends_new_version(self):
        """回滚是"用旧内容发新版"，版本号只增不减。"""
        self._publish("v1 内容 {today_hint}")
        self._publish("v2 内容 {today_hint}")
        result = asyncio.run(
            db.rollback_prompt(tpl_key=prompts.PROMPT_KEY, version=1, actor=self.actor)
        )
        self.assertEqual(result["version"], 3)

        active = asyncio.run(db.active_prompt(prompts.PROMPT_KEY))
        self.assertEqual(active["version"], 3)
        self.assertIn("v1 内容", active["content"], "回滚后启用内容必须等于被回滚版本")
        self.assertIn("回滚自 v1", active["note"])

        # 被回滚的历史版本本身不能被改动
        v1 = asyncio.run(db.fetch_prompt_version(prompts.PROMPT_KEY, 1))
        self.assertEqual(v1["status"], "archived")

    def test_rollback_missing_version(self):
        with self.assertRaises(ApiError) as ctx:
            asyncio.run(db.rollback_prompt(tpl_key=prompts.PROMPT_KEY, version=99, actor=self.actor))
        self.assertEqual(ctx.exception.code, "PROMPT_VERSION_NOT_FOUND")

    def test_content_cannot_be_overwritten(self):
        """同一个版本号插两次必须失败——否则"v3 到底是什么"就没有答案了。"""
        self._publish("内容 {today_hint}")
        conn = sqlite3.connect(db.DB_PATH)
        try:
            with self.assertRaises(sqlite3.IntegrityError):
                conn.execute(
                    "INSERT INTO prompt_templates (id, tpl_key, version, content, status, created_at) "
                    "VALUES ('dup', ?, 1, 'x', 'archived', 'now')",
                    (prompts.PROMPT_KEY,),
                )
        finally:
            conn.close()

    def test_two_active_versions_rejected_by_db(self):
        """应用层"先归档再插入"漏一步时，数据库必须兜住。"""
        self._publish("内容 {today_hint}")
        conn = sqlite3.connect(db.DB_PATH)
        try:
            with self.assertRaises(sqlite3.IntegrityError):
                conn.execute(
                    "INSERT INTO prompt_templates (id, tpl_key, version, content, status, created_at) "
                    "VALUES ('dup2', ?, 2, 'x', 'active', 'now')",
                    (prompts.PROMPT_KEY,),
                )
        finally:
            conn.close()


class LoadSystemPromptTest(unittest.TestCase):
    """加载路径的行为决定了"模板坏了会不会拖垮对话"。"""

    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix="sdb-load-"))
        self._old_dir, self._old_path = db.DB_DIR, db.DB_PATH
        db.DB_DIR = self.tmp
        db.DB_PATH = self.tmp / "t.db"
        db.init_db()
        self.actor = {"id": "u-admin", "emp_no": "admin"}

    def tearDown(self):
        db.DB_DIR, db.DB_PATH = self._old_dir, self._old_path
        shutil.rmtree(self.tmp, ignore_errors=True)

    def test_builtin_when_empty(self):
        text, source = asyncio.run(prompts.load_system_prompt("今天是 2026-11-05"))
        self.assertEqual(source["source"], "builtin")
        self.assertIsNone(source["version"])
        self.assertIn("今天是 2026-11-05", text)
        self.assertIn("search_policy", text, "内置模板必须含检索约束")

    def test_db_version_used(self):
        asyncio.run(
            db.publish_prompt(
                tpl_key=prompts.PROMPT_KEY, content="自定义正文 {today_hint}", note=None, actor=self.actor
            )
        )
        text, source = asyncio.run(prompts.load_system_prompt("今天是 2026-11-05"))
        self.assertEqual(source["source"], "db")
        self.assertEqual(source["version"], 1)
        self.assertIn("自定义正文", text)

    def test_broken_template_falls_back(self):
        """库里存了带未知占位符的模板（多半是直接改库），本轮必须回落。

        注意不是删掉它——那是管理员的数据；也不报错——用户不该为此看到 500。
        """
        asyncio.run(
            db.publish_prompt(
                tpl_key=prompts.PROMPT_KEY,
                content="坏模板 {max_slot} {today_hint}",
                note="direct sql",
                actor=self.actor,
            )
        )
        text, source = asyncio.run(prompts.load_system_prompt("今天是 2026-11-05"))
        self.assertEqual(source["source"], "builtin")
        self.assertEqual(source["broken_version"], 1)
        self.assertIn("search_policy", text)
        # 坏模板必须仍留在库里，等管理员自己看见并修
        self.assertIsNotNone(asyncio.run(db.active_prompt(prompts.PROMPT_KEY)))

    def test_db_error_falls_back(self):
        """数据库整个不可用（路径指向不存在的目录）也不能让提问挂掉。"""
        db.DB_PATH = self.tmp / "no-such-dir" / "x.db"
        text, source = asyncio.run(prompts.load_system_prompt("今天是 2026-11-05"))
        self.assertEqual(source["source"], "builtin")
        self.assertIn("search_policy", text)


if __name__ == "__main__":
    unittest.main()
