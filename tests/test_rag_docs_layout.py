"""语料目录回退：docs/ 被编号重排后，制度正本仍要能被索引找到。

这条用例是给「整理文档的人」兜底的：manuals/ 是运行依赖，目录一没，
索引建空、rag 变 degraded，但从启动日志之外看不出哪里坏了。
"""

from __future__ import annotations

import os
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from app import rag


class DocsLayoutTest(unittest.TestCase):
    def setUp(self):
        self._saved = {
            k: os.environ.get(k) for k in ("RAG_DOCS_DIR", "RAG_PATTERN")
        }
        os.environ.pop("RAG_DOCS_DIR", None)
        os.environ.pop("RAG_PATTERN", None)

    def tearDown(self):
        for k, v in self._saved.items():
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = v

    def test_manuals_dir_wins_when_present(self):
        """原布局优先：manuals/ 在就照旧全量 *.md，不改变既有行为。"""
        with tempfile.TemporaryDirectory() as d:
            manuals = Path(d) / "manuals"
            manuals.mkdir()
            (manuals / "制度.md").write_text("# 制度", encoding="utf-8")
            with mock.patch.object(rag, "DEFAULT_DOCS_DIR", manuals):
                self.assertEqual(rag.builtin_dir(), manuals)
                self.assertEqual(rag._builtin_pattern(), "*.md")

    def test_fallback_selects_only_policy_doc(self):
        """manuals/ 消失时退回 docs/，且 pattern 必须收窄到制度正本。"""
        with tempfile.TemporaryDirectory() as d:
            root = Path(d)
            (root / "4.设备借用制度.md").write_text("# 制度", encoding="utf-8")
            (root / "1.discovery-report.md").write_text("调研", encoding="utf-8")
            (root / "9.上线交付运行手册.md").write_text("手册", encoding="utf-8")
            missing = root / "manuals"
            with mock.patch.object(rag, "DEFAULT_DOCS_DIR", missing), \
                    mock.patch.object(rag, "FALLBACK_DOCS_DIR", root):
                self.assertEqual(rag.builtin_dir(), root)
                picked = sorted(p.name for p in root.glob(rag._builtin_pattern()))
                self.assertEqual(picked, ["4.设备借用制度.md"])

    def test_explicit_env_always_wins(self):
        """测试与离线部署靠 RAG_DOCS_DIR 指定语料，不能被回退逻辑劫持。"""
        with tempfile.TemporaryDirectory() as d:
            os.environ["RAG_DOCS_DIR"] = d
            os.environ["RAG_PATTERN"] = "custom-*.md"
            with mock.patch.object(rag, "DEFAULT_DOCS_DIR", Path(d) / "missing"):
                self.assertEqual(rag.builtin_dir(), Path(d))
                self.assertEqual(rag._builtin_pattern(), "custom-*.md")

    def test_current_repo_layout_finds_policy(self):
        """仓库当前形状（编号前缀、无 manuals/）必须能找到且只找到制度正本。"""
        picked = sorted(
            p.name for p in rag.builtin_dir().glob(rag._builtin_pattern())
        )
        self.assertEqual(picked, ["4.设备借用制度.md"])


if __name__ == "__main__":
    unittest.main()
