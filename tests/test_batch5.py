"""
批次 5 单测：制度知识库热更新。

这一组测的核心不是"函数返回值对不对"，而是三条**上线后会出事的硬约束**：

  1) 停用一份文档，重建后它的切片必须真的消失（否则"停用"是个假动作）；
  2) 重建失败时旧索引必须还活着（否则改错一个字，全站问答挂掉）；
  3) 元数据同步不能把"管理员停用"覆盖成"文件丢失"（否则事故原因被抹平）。

全部用临时目录 + 临时库，不碰真实 data/chat.db。
"""

from __future__ import annotations

import asyncio
import os
import shutil
import tempfile
import unittest
from pathlib import Path

from app import db, rag
from app.errors import ApiError

BUILTIN_DOC = """# 测试用制度

## 一、时段规则

每天 08:00 至 20:00 可以借用，共六个时段，每段两小时。

## 二、时长上限

单日最多预约两格，超出的部分不予受理。
"""

UPLOAD_DOC = """# 补充规定

## 一、贵重设备

频谱分析仪属于贵重设备，借用需要部门负责人签字确认。
"""


class KBTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.tmp = Path(tempfile.mkdtemp(prefix="kb-test-"))
        cls.builtin = cls.tmp / "builtin"
        cls.builtin.mkdir()
        (cls.builtin / "测试制度.md").write_text(BUILTIN_DOC, encoding="utf-8")
        cls.uploads = cls.tmp / "uploads"

        cls._env = {k: os.environ.get(k) for k in ("RAG_DOCS_DIR", "RAG_UPLOAD_DIR")}
        os.environ["RAG_DOCS_DIR"] = str(cls.builtin)
        os.environ["RAG_UPLOAD_DIR"] = str(cls.uploads)

        cls._db_path = db.DB_PATH
        db.DB_PATH = cls.tmp / "test.db"
        db.init_db()

        # 阶段 1 模块把缓存目录钉在项目根，测试里改到临时目录，避免污染 .rag_cache
        rag.load_stage1().CACHE_DIR = cls.tmp / "cache"

    @classmethod
    def tearDownClass(cls):
        db.DB_PATH = cls._db_path
        for key, value in cls._env.items():
            if value is None:
                os.environ.pop(key, None)
            else:
                os.environ[key] = value
        shutil.rmtree(cls.tmp, ignore_errors=True)

    def setUp(self):
        """每个用例都从"只有一份内置文档"的干净状态开始。"""
        with db._connect() as conn:
            conn.execute("DELETE FROM kb_documents")
        if self.uploads.exists():
            for p in self.uploads.iterdir():
                if p.is_file():
                    p.unlink()
        rag._kb = None  # noqa: SLF001 - 测试需要重置单例

    # ---------------------------------------------------------------- 工具函数

    def _build(self) -> dict:
        return asyncio.run(rag.rebuild("test"))

    def _status_of(self, doc_key: str) -> dict:
        row = asyncio.run(db.get_kb_document(doc_key))
        self.assertIsNotNone(row, f"{doc_key} 未登记")
        return row

    def _sources(self) -> set[str]:
        assert rag._kb is not None
        return {c.source for c in rag._kb.chunks}

    # ---------------------------------------------------------------- 文件名

    def test_safe_name_keeps_chinese_and_strips_path(self):
        self.assertEqual(rag._safe_name("设备借用制度-v1.md"), "设备借用制度-v1.md")
        # 路径穿越必须被拆掉，只留最后一段
        self.assertEqual(rag._safe_name("../../etc/passwd.md"), "passwd.md")
        self.assertEqual(rag._safe_name(r"..\..\windows\x.md"), "x.md")
        # 特殊字符被替换，但扩展名要留住（否则后面按后缀判断会失败）
        self.assertTrue(rag._safe_name('a b*c?.md').endswith(".md"))
        self.assertNotIn(" ", rag._safe_name("a b.md"))

    def test_decode_accepts_gbk(self):
        """企业里大量制度文档是 Windows 上另存的 GBK，不该让人先去转码。"""
        self.assertEqual(rag._decode("设备借用".encode("gbk")), "设备借用")
        self.assertEqual(rag._decode(UPLOAD_DOC.encode("utf-8")), UPLOAD_DOC)

    def test_decode_rejects_unknown_bytes(self):
        with self.assertRaises(ApiError) as ctx:
            rag._decode(b"\xff\xfe\x00\x00\x81\x00")
        self.assertEqual(ctx.exception.code, "KB_UPLOAD_INVALID")

    # ---------------------------------------------------------------- 上传

    def test_upload_rejects_pdf_and_empty(self):
        with self.assertRaises(ApiError) as ctx:
            asyncio.run(rag.save_upload("制度.pdf", b"%PDF-1.4"))
        self.assertEqual(ctx.exception.code, "KB_UPLOAD_INVALID")

        with self.assertRaises(ApiError) as ctx:
            asyncio.run(rag.save_upload("制度.md", b"   \n\n"))
        self.assertEqual(ctx.exception.code, "KB_UPLOAD_INVALID")

    def test_upload_rejects_duplicate_name(self):
        asyncio.run(rag.save_upload("补充规定.md", UPLOAD_DOC.encode("utf-8")))
        with self.assertRaises(ApiError) as ctx:
            asyncio.run(rag.save_upload("补充规定.md", UPLOAD_DOC.encode("utf-8")))
        self.assertEqual(ctx.exception.code, "KB_DOC_EXISTS")

    def test_upload_normalizes_to_utf8_on_disk(self):
        saved = asyncio.run(rag.save_upload("补充规定.md", UPLOAD_DOC.encode("gbk")))
        on_disk = (self.uploads / "补充规定.md").read_text(encoding="utf-8")
        self.assertIn("部门负责人签字", on_disk)
        self.assertEqual(saved["doc_key"], "uploads/补充规定.md")

    # ---------------------------------------------------------------- 重建

    def test_rebuild_registers_metadata(self):
        res = self._build()
        self.assertTrue(res["ok"], res)
        row = self._status_of("测试制度.md")
        self.assertEqual(row["origin"], "builtin")
        self.assertEqual(row["status"], "active")
        self.assertGreater(row["chunks"], 0)

    def test_upload_takes_effect_without_restart(self):
        """热更新的核心断言：同一个进程内，上传后立刻能检索到新内容。"""
        first = self._build()
        self.assertEqual(first["docs"], 1)

        asyncio.run(rag.save_upload("补充规定.md", UPLOAD_DOC.encode("utf-8")))
        second = self._build()

        self.assertTrue(second["ok"], second)
        self.assertEqual(second["docs"], 2)
        self.assertIn("uploads/补充规定.md", self._sources())

        hits = asyncio.run(rag.search("频谱分析仪借用需要谁签字", top_k=3))
        self.assertTrue(any(h.chunk.source == "uploads/补充规定.md" for h in hits))

    def test_rebuild_reuses_cache_when_nothing_changed(self):
        self._build()
        again = self._build()
        self.assertTrue(again["cache_hit"], again)

    def test_disabled_document_leaves_the_index(self):
        asyncio.run(rag.save_upload("补充规定.md", UPLOAD_DOC.encode("utf-8")))
        self._build()
        self.assertIn("uploads/补充规定.md", self._sources())

        actor = {"id": "u1", "emp_no": "T001"}
        asyncio.run(db.set_kb_document_status("uploads/补充规定.md", "disabled", actor))
        res = self._build()

        self.assertTrue(res["ok"], res)
        self.assertEqual(res["docs"], 1)
        self.assertNotIn("uploads/补充规定.md", self._sources())

        hits = asyncio.run(rag.search("频谱分析仪借用需要谁签字", top_k=3))
        self.assertFalse(any(h.chunk.source == "uploads/补充规定.md" for h in hits))

    def test_disabled_document_keeps_last_chunk_count(self):
        """停用后页面还要显示"停用前占多少片"，否则没法判断停掉会少多少内容。"""
        asyncio.run(rag.save_upload("补充规定.md", UPLOAD_DOC.encode("utf-8")))
        self._build()
        before = self._status_of("uploads/补充规定.md")["chunks"]
        self.assertGreater(before, 0)

        actor = {"id": "u1", "emp_no": "T001"}
        asyncio.run(db.set_kb_document_status("uploads/补充规定.md", "disabled", actor))
        self._build()
        self.assertEqual(self._status_of("uploads/补充规定.md")["chunks"], before)

    def test_rebuild_failure_keeps_previous_index(self):
        """把所有文档停用 → 重建必须失败，但**旧索引不能丢**。"""
        self._build()
        self.assertIsNotNone(rag._kb)
        chunks_before = len(rag._kb.chunks)

        actor = {"id": "u1", "emp_no": "T001"}
        asyncio.run(db.set_kb_document_status("测试制度.md", "disabled", actor))
        res = self._build()

        self.assertFalse(res["ok"])
        self.assertIn("停用", res["error"])
        self.assertTrue(res["kept_previous"])
        self.assertIsNotNone(rag._kb)
        self.assertEqual(len(rag._kb.chunks), chunks_before)

        # 失败原因要能通过 status() 暴露出来，否则页面上只有一个红条没有原因
        self.assertIn("停用", rag.status()["error"])

    def test_missing_file_marks_on_disk_zero_but_keeps_status(self):
        """
        文件被删掉时：on_disk=0，但 status 保持原样。

        这两列一旦混成一处，"管理员主动停用"就会被改写成"文件丢失"，
        事故复盘时看到的原因直接是错的。
        """
        asyncio.run(rag.save_upload("补充规定.md", UPLOAD_DOC.encode("utf-8")))
        self._build()
        (self.uploads / "补充规定.md").unlink()

        res = self._build()
        self.assertTrue(res["ok"], res)
        row = self._status_of("uploads/补充规定.md")
        self.assertEqual(int(row["on_disk"]), 0)
        self.assertEqual(row["status"], "active")

    def test_sync_does_not_reactivate_disabled_document(self):
        self._build()  # 先建一次索引，登记表里才有这一行
        actor = {"id": "u1", "emp_no": "T001"}
        asyncio.run(db.set_kb_document_status("测试制度.md", "disabled", actor))
        asyncio.run(db.sync_kb_documents([
            {
                "doc_key": "测试制度.md", "display_name": "测试制度.md", "origin": "builtin",
                "sha256": "x", "size_bytes": 10, "chunks": 1, "file_mtime": "2026-01-01T00:00:00+08:00",
            }
        ]))
        self.assertEqual(self._status_of("测试制度.md")["status"], "disabled")

    # ---------------------------------------------------------------- 删除

    def test_builtin_document_cannot_be_deleted(self):
        self._build()
        with self.assertRaises(ApiError) as ctx:
            asyncio.run(rag.trash_upload("测试制度.md"))
        self.assertEqual(ctx.exception.code, "KB_DOC_BUILTIN")

        with self.assertRaises(ApiError) as ctx:
            asyncio.run(db.delete_kb_document("测试制度.md", {"id": "u1", "emp_no": "T001"}))
        self.assertEqual(ctx.exception.code, "KB_DOC_BUILTIN")

    def test_delete_moves_file_to_trash(self):
        asyncio.run(rag.save_upload("补充规定.md", UPLOAD_DOC.encode("utf-8")))
        self._build()
        moved = asyncio.run(rag.trash_upload("uploads/补充规定.md"))

        self.assertFalse((self.uploads / "补充规定.md").exists())
        self.assertTrue(Path(moved).exists())
        self.assertIn(".trash", moved)

        asyncio.run(db.delete_kb_document("uploads/补充规定.md", {"id": "u1", "emp_no": "T001"}))
        self.assertIsNone(asyncio.run(db.get_kb_document("uploads/补充规定.md")))

    # ---------------------------------------------------------------- 预览

    def test_preview_shows_chunks_of_current_rules(self):
        self._build()
        chunks = asyncio.run(rag.preview("测试制度.md"))
        self.assertTrue(chunks)
        self.assertTrue(any("时段规则" in c["heading"] for c in chunks))
        self.assertTrue(all(c["chars"] > 0 for c in chunks))

    def test_preview_unknown_doc_is_empty(self):
        self._build()
        self.assertEqual(asyncio.run(rag.preview("不存在.md")), [])


if __name__ == "__main__":
    unittest.main()
