"""
检索装配层：把阶段 1 的核心模块（C1–C5）原样复用，只做一件事——装成单例。

为什么不把这些代码复制进 app/：
    切片、分词、BM25、向量、RRF 这五块是阶段 1 已经调好的，复制一份就会出现两个版本，
    日后改切片策略必然只改一处，检索质量悄悄分叉。这里用 importlib 直接加载原文件，
    保证「命令行验证的排序」和「Web 里返回的排序」是同一套代码。

阶段 3 的替换点（写在这里免得忘）：
    - chunks 从 .rag_cache 搬进 manual_chunks 表，向量进 pgvector；
    - BM25 换成 PostgreSQL 全文检索（tsvector + GIN）；
    - 检索前加 effective_from <= as_of 的版本过滤。
    对外只暴露 search()，上面的替换不会影响调用方。
"""

from __future__ import annotations

import asyncio
import importlib.util
import logging
import os
import sys
from pathlib import Path
from typing import Any

from app.db import PROJECT_ROOT

logger = logging.getLogger("app.rag")

# 阶段 1 原型文件：核心模块的唯一定义处
STAGE1_FILE = PROJECT_ROOT / "scripts" / "stage1_rag_prototype.py"

# 默认语料目录。**单独一个 manuals/ 目录，不要和调研报告混在一起**——
# 阶段 1 已经验证过：223 个调研切片会把真正的规程文档挤出 top3。
DEFAULT_DOCS_DIR = PROJECT_ROOT / "docs" / "manuals"
DEFAULT_PATTERN = "*.md"

_stage1: Any = None
_kb: Any = None
_settings: Any = None
_error: str | None = None
_warm_lock = asyncio.Lock()


def load_stage1() -> Any:
    """用模块名加载阶段 1 脚本（带 __main__ 保护，导入不会触发 CLI）。"""
    global _stage1
    if _stage1 is not None:
        return _stage1

    if not STAGE1_FILE.exists():
        raise RuntimeError(f"找不到阶段 1 模块：{STAGE1_FILE}")

    spec = importlib.util.spec_from_file_location("stage1_rag_prototype", STAGE1_FILE)
    if spec is None or spec.loader is None:
        raise RuntimeError(f"无法加载阶段 1 模块：{STAGE1_FILE}")
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)

    # 脚本里 CACHE_DIR 是相对路径（依赖启动时的工作目录）。
    # Web 服务可能从任意目录启动，这里钉死到项目根，避免索引缓存"找不到就重建"。
    module.CACHE_DIR = PROJECT_ROOT / ".rag_cache"
    _stage1 = module
    return module


def settings() -> Any | None:
    """当前 LLM 配置。warmup 之前为 None。"""
    return _settings


async def warmup() -> None:
    """
    构建检索索引。幂等，重复调用只扫第一遍。

    失败**不抛异常**：服务要能起来，前端要能拿到明确的 503（而不是 502 白屏），
    管理员才能看到 /health 里的具体原因。
    """
    global _kb, _settings, _error

    async with _warm_lock:
        if _kb is not None:
            return

        try:
            module = load_stage1()
            _settings = module.Settings.from_env()

            docs_dir = Path(os.getenv("RAG_DOCS_DIR") or DEFAULT_DOCS_DIR)
            pattern = os.getenv("RAG_PATTERN") or DEFAULT_PATTERN

            if not docs_dir.exists():
                raise RuntimeError(f"语料目录不存在：{docs_dir}")
            if not list(docs_dir.rglob(pattern)):
                raise RuntimeError(f"语料目录 {docs_dir} 下没有匹配 {pattern} 的文件")

            logger.info("开始构建检索索引：%s（%s）", docs_dir, pattern)
            _kb = await module.build_knowledge_base(docs_dir, _settings, pattern=pattern)
            _error = None
            logger.info("检索索引就绪：%s 个切片，向量后端 %s", len(_kb.chunks), _kb.backend_name)
        except (Exception, SystemExit) as e:  # noqa: BLE001 - 启动失败要留住原因，不能让进程挂掉
            _error = f"{type(e).__name__}: {e}"
            logger.exception("构建检索索引失败")


async def search(question: str, top_k: int = 3) -> list:
    """混合检索 top_k。未就绪时抛 RuntimeError，由路由层转成 503。"""
    if _kb is None or _stage1 is None:
        raise RuntimeError(_error or "检索索引尚未就绪，请稍后重试")
    return await _stage1.hybrid_search(
        question,
        _kb.chunks,
        _kb.bm25,
        _kb.vindex,
        _kb.embedder,
        top_k=top_k,
    )


def status() -> dict[str, Any]:
    """/health 用的状态快照。ready=False 时 error 一定有值。"""
    return {
        "ready": _kb is not None,
        "chunks": len(_kb.chunks) if _kb is not None else 0,
        "embed_backend": getattr(_kb, "backend_name", None) if _kb is not None else None,
        "llm_ready": bool(getattr(_settings, "llm_ready", False)) if _settings else False,
        "llm_model": getattr(_settings, "chat_model", None) if _settings else None,
        "error": _error,
    }
