"""
检索装配层：把阶段 1 的核心模块（C1–C5）原样复用，装成可热替换的单例。

为什么不把这些代码复制进 app/：
    切片、分词、BM25、向量、RRF 这五块是阶段 1 已经调好的，复制一份就会出现两个版本，
    日后改切片策略必然只改一处，检索质量悄悄分叉。这里用 importlib 直接加载原文件，
    保证「命令行验证的排序」和「Web 里返回的排序」是同一套代码。

【批次 5 的关键变化】索引可以重建，不需要重启进程。
    此前 warmup() 只在 lifespan 里跑一次，`_kb` 一旦有值就再也不刷新——
    管理员改了制度文件，用户问到的还是旧条文，除非重启服务。
    制度每年都要改，这是唯一**业务上必然发生**的场景，所以必须解决。

    重建的做法是「先建新的，再整体替换」：
    整个过程在锁内进行，但 `_kb` 的赋值是最后一步的原子操作，
    重建期间进来的检索请求继续用旧索引，不会看到"建到一半"的索引。
    重建失败则保留旧索引——制度改了但文件写坏了，不能让问答直接挂掉。

语料有两个来源，都留在磁盘上：
    docs/manuals/     内置文档，随代码走 git，是制度正本
    data/kb/uploads/  管理员在后台上传的文件，不进 git

    为什么不把正文切好存进数据库：制度文件是要归档、盖章、交接的，
    文件本身必须是唯一真相。切碎了存库，日后要证明"当时那句原文是什么"
    就得从碎片里拼回去。数据库只登记元数据与启停状态。
"""

from __future__ import annotations

import asyncio
import hashlib
import importlib.util
import logging
import os
import shutil
import sys
import time
from collections import Counter
from datetime import datetime
from pathlib import Path
from typing import Any

from app import db
from app.db import PROJECT_ROOT
from app.errors import ApiError

logger = logging.getLogger("app.rag")

# 阶段 1 原型文件：核心模块的唯一定义处
STAGE1_FILE = PROJECT_ROOT / "scripts" / "stage1_rag_prototype.py"

# 默认语料目录。**单独一个 manuals/ 目录，不要和调研报告混在一起**——
# 阶段 1 已经验证过：223 个调研切片会把真正的规程文档挤出 top3。
DEFAULT_DOCS_DIR = PROJECT_ROOT / "docs" / "manuals"
DEFAULT_UPLOAD_DIR = PROJECT_ROOT / "data" / "kb" / "uploads"
DEFAULT_PATTERN = "*.md"

# 允许上传的扩展名。**不含 PDF**：PDF 需要额外解析库，且解析出来的排版
# 常常把表格拆散，制度里的"每天最多 2 格"这类关键数字最容易在拆表时丢。
# 与其给一个"上传成功但检索不到重点"的假功能，不如直接要求先转成 markdown。
UPLOAD_SUFFIXES = {".md", ".markdown", ".txt"}
MAX_UPLOAD_BYTES = 512 * 1024

# 上传文件在 doc_key 里的前缀。用它把两类文档的 id 空间分开，
# 避免内置的"设备借用制度.md"和上传的同名文件撞成一个 chunk_id。
UPLOAD_PREFIX = "uploads"

# 缓存文件保留份数。每份都含一个 .npy，制度改几十次会白占几十 MB。
CACHE_KEEP = 6

_stage1: Any = None
_kb: Any = None
_settings: Any = None
_error: str | None = None
_last: dict[str, Any] = {}
_lock = asyncio.Lock()


# =============================================================================
# 路径与环境
# =============================================================================


def builtin_dir() -> Path:
    """内置语料目录。可用 RAG_DOCS_DIR 覆盖（测试与离线部署都要用）。"""
    return Path(os.getenv("RAG_DOCS_DIR") or DEFAULT_DOCS_DIR)


def upload_dir() -> Path:
    """上传语料目录。可用 RAG_UPLOAD_DIR 覆盖。"""
    return Path(os.getenv("RAG_UPLOAD_DIR") or DEFAULT_UPLOAD_DIR)


def _builtin_pattern() -> str:
    return os.getenv("RAG_PATTERN") or DEFAULT_PATTERN


def trash_dir() -> Path:
    """被删除的上传文件移到这里，而不是 unlink。

    删除制度文件是个不可逆动作，而这个动作由人在网页上点出来。
    移到 .trash 至少留了一条"从磁盘捞回来"的路，代价是几百 KB 空间。
    """
    return upload_dir().parent / ".trash"


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


# =============================================================================
# 语料扫描
# =============================================================================


def _iter_files(root: Path, pattern: str) -> list[Path]:
    if not root.exists():
        return []
    out = [p for p in sorted(root.rglob(pattern)) if p.is_file() and p.suffix.lower() in UPLOAD_SUFFIXES]
    return out


def _scan_sync() -> list[dict[str, Any]]:
    """
    扫两个目录，返回文档清单（**不过滤停用**）。

    停用过滤放在数据库那一侧、由调用方做。原因：扫描结果同时要拿去同步
    元数据表，停用的文档也得把 on_disk 刷成 1——否则页面上会显示
    "文件已丢失"，而它其实好好躺在磁盘上。
    """
    docs: list[dict[str, Any]] = []

    for p in _iter_files(builtin_dir(), _builtin_pattern()):
        rel = p.relative_to(builtin_dir()).as_posix()
        docs.append(
            {
                "doc_key": rel,
                "display_name": p.name,
                "origin": "builtin",
                "path": str(p),
                "root": str(builtin_dir()),
                "size_bytes": p.stat().st_size,
                "file_mtime": datetime.fromtimestamp(p.stat().st_mtime).astimezone().isoformat(timespec="seconds"),
            }
        )

    for p in _iter_files(upload_dir(), "*"):
        rel = p.relative_to(upload_dir()).as_posix()
        docs.append(
            {
                "doc_key": f"{UPLOAD_PREFIX}/{rel}",
                "display_name": p.name,
                "origin": "uploaded",
                "path": str(p),
                "root": str(upload_dir()),
                "size_bytes": p.stat().st_size,
                "file_mtime": datetime.fromtimestamp(p.stat().st_mtime).astimezone().isoformat(timespec="seconds"),
            }
        )

    return docs


async def scan() -> list[dict[str, Any]]:
    return await asyncio.to_thread(_scan_sync)


def _fingerprint(docs: list[dict[str, Any]], s: Any) -> str:
    """
    语料指纹：内容变更 + 切片参数 + 向量模型。

    用 sha256 而不是 mtime，是因为这个值还决定"要不要重新向量化"。
    mtime 在 git checkout、rsync、解压时都会变，但内容没变，
    按 mtime 判缓存会白白烧掉一次 embedding 费用与几十秒等待。
    """
    h = hashlib.sha256()
    for d in docs:
        h.update(f"{d['doc_key']}|{d.get('sha256') or d['size_bytes']}".encode())
    h.update(f"v4|{s.chunk_size}|{s.chunk_overlap}|{s.embed_model}|{s.embed_backend}".encode())
    return h.hexdigest()[:16]


def _sha256_of(path: Path) -> str:
    h = hashlib.sha256()
    h.update(path.read_bytes())
    return h.hexdigest()


def _prune_cache(cache_dir: Path, keep: int = CACHE_KEEP) -> None:
    """只保留最近 keep 份缓存。失败无所谓，缓存清不掉不影响检索。"""
    try:
        files = sorted(cache_dir.glob("*.chunks.json"), key=lambda p: p.stat().st_mtime, reverse=True)
        for old in files[keep:]:
            old.unlink(missing_ok=True)
            old.with_name(old.name.replace(".chunks.json", ".vectors.npy")).unlink(missing_ok=True)
    except OSError as exc:  # noqa: BLE001 - 清理失败不是错误
        logger.warning("清理索引缓存失败：%s", exc)


# =============================================================================
# 重建
# =============================================================================


async def rebuild(reason: str = "manual") -> dict[str, Any]:
    """
    重建检索索引并**原地替换**，不重启进程。

    失败时保留上一次的索引：制度文件改坏了（比如整个文件被清空），
    正确的后果是"新内容检索不到"，而不是"问答整个不可用"。
    返回结构给页面用，调用方不需要自己拼状态。
    """
    global _kb, _settings, _error, _last

    async with _lock:
        started = time.perf_counter()
        try:
            module = load_stage1()
            if _settings is None:
                _settings = module.Settings.from_env()
            s = _settings

            all_docs = await asyncio.to_thread(_scan_sync)
            if not all_docs:
                raise RuntimeError(
                    f"没有找到任何制度文档（{builtin_dir()} 与 {upload_dir()} 均为空）"
                )

            disabled = await db.disabled_doc_keys()
            active = [d for d in all_docs if d["doc_key"] not in disabled]
            if not active:
                raise RuntimeError("所有制度文档都已停用，至少要保留一份才能检索")

            # 指纹只需要"参与检索的文档"，停用一份文档就等于换了一套语料；
            # 但哈希要给全部文档都算，否则停用的那份在元数据里会丢掉内容指纹。
            for d in all_docs:
                d["sha256"] = await asyncio.to_thread(_sha256_of, Path(d["path"]))
            fp = _fingerprint(active, s)

            embedder = await module._make_embedder(s, allow_fallback=True)  # noqa: SLF001 - 复用阶段 1 的降级策略
            cached = module.load_cache(fp)
            cache_hit = cached is not None

            if cached:
                chunks, vectors = cached
            else:
                chunks = []
                for d in active:
                    got = module.load_markdown(
                        Path(d["path"]), Path(d["root"]), s.chunk_size, s.chunk_overlap
                    )
                    # 统一改写 id 与来源：内置和上传两个根目录下可能有同名文件，
                    # 不统一就会出现两个 chunk_id 相同的切片，检索结果指向错误的出处。
                    for i, c in enumerate(got):
                        c.source = d["doc_key"]
                        c.chunk_id = f"{d['doc_key']}#{i:04d}"
                    chunks.extend(got)

                if not chunks:
                    raise RuntimeError("语料里没有可切片的正文（文件可能只有标题或全是空行）")

                vectors = await embedder.embed_texts([module.embed_text(c) for c in chunks])
                module.save_cache(fp, chunks, vectors)
                _prune_cache(module.CACHE_DIR)

            bm25 = module.BM25Index([module.tokenize(module.embed_text(c)) for c in chunks])
            vindex = module.VectorIndex(vectors.shape[1])
            vindex.build(vectors)

            # 最后一步才替换：此前所有异常都不会影响正在服务的旧索引
            _kb = module.KnowledgeBase(chunks, bm25, vindex, embedder, embedder.name)
            _error = None

            # 停用的文档没有切片，但页面上仍要显示"停用前占多少片"——
            # 管理员正是在决定"要不要停"的那一刻，需要知道会少掉多少内容。
            counts = Counter(c.source for c in chunks)
            existing = {r["doc_key"]: r for r in await db.list_kb_documents()}
            for d in all_docs:
                if d["doc_key"] in counts:
                    d["chunks"] = counts[d["doc_key"]]
                elif d["doc_key"] in disabled:
                    d["chunks"] = int((existing.get(d["doc_key"]) or {}).get("chunks") or 0)
                else:
                    d["chunks"] = 0
            await db.sync_kb_documents(all_docs)

            elapsed = int((time.perf_counter() - started) * 1000)
            _last = {
                "ok": True,
                "reason": reason,
                "at": datetime.now().astimezone().isoformat(timespec="seconds"),
                "chunks": len(chunks),
                "docs": len(active),
                "disabled": len(disabled),
                "embed_backend": embedder.name,
                "cache_hit": cache_hit,
                "ms": elapsed,
            }
            logger.info(
                "检索索引已重建（%s）：%s 片 / %s 份文档，向量后端 %s%s，耗时 %sms",
                reason, len(chunks), len(active), embedder.name,
                "（缓存命中）" if cache_hit else "", elapsed,
            )
            return dict(_last)

        except (Exception, SystemExit) as exc:  # noqa: BLE001 - 重建失败必须留住原因
            _error = f"{type(exc).__name__}: {exc}"
            elapsed = int((time.perf_counter() - started) * 1000)
            _last = {
                "ok": False,
                "reason": reason,
                "at": datetime.now().astimezone().isoformat(timespec="seconds"),
                "error": _error,
                "ms": elapsed,
                "kept_previous": _kb is not None,
            }
            logger.exception("重建检索索引失败（保留旧索引：%s）", _kb is not None)
            return dict(_last)


async def warmup() -> None:
    """
    启动时建索引。幂等，重复调用不会重建。

    失败**不抛异常**：服务要能起来，前端要能拿到明确的 503（而不是 502 白屏），
    管理员才能看到 /health 里的具体原因。
    """
    if _kb is not None:
        return
    await rebuild(reason="startup")


# =============================================================================
# 查询与状态
# =============================================================================


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
    """/health 与知识库页共用的状态快照。ready=False 时 error 一定有值。"""
    return {
        "ready": _kb is not None,
        "chunks": len(_kb.chunks) if _kb is not None else 0,
        "embed_backend": getattr(_kb, "backend_name", None) if _kb is not None else None,
        "llm_ready": bool(getattr(_settings, "llm_ready", False)) if _settings else False,
        "llm_model": getattr(_settings, "chat_model", None) if _settings else None,
        "error": _error,
        "last_build": dict(_last),
        "builtin_dir": str(builtin_dir()),
        "upload_dir": str(upload_dir()),
    }


# =============================================================================
# 切片预览
# =============================================================================


async def preview(doc_key: str, limit: int = 40) -> list[dict[str, Any]]:
    """
    按当前的切片参数，把某份文档切一遍给管理员看。

    这比"显示字数"有用得多：制度里写了一段 2000 字的长条款，
    切出来若是一片横跨三个主题的巨型 chunk，检索命中率就会掉，
    而这件事只有在切片层面才看得见。
    """
    docs = await asyncio.to_thread(_scan_sync)
    target = next((d for d in docs if d["doc_key"] == doc_key), None)
    if target is None:
        return []

    module = load_stage1()
    s = _settings or module.Settings.from_env()
    got = await asyncio.to_thread(
        module.load_markdown, Path(target["path"]), Path(target["root"]), s.chunk_size, s.chunk_overlap
    )
    out: list[dict[str, Any]] = []
    for i, c in enumerate(got[:limit]):
        out.append(
            {
                # 与 rebuild 里同一套改写：否则上传文档在预览里显示 "x.md#0000"，
                # 在检索结果里却是 "uploads/x.md#0000"，对不上号。
                "chunk_id": f"{doc_key}#{i:04d}",
                "heading": c.heading,
                "chars": len(c.text),
                "text": c.text[:160] + ("…" if len(c.text) > 160 else ""),
            }
        )
    return out


# =============================================================================
# 上传与删除
# =============================================================================


def _safe_name(raw: str) -> str:
    """
    清洗上传文件名。

    只保留中英文、数字、点、横线、下划线：文件名会进 doc_key、进 URL、
    进日志，任何 `../` 或控制字符都是在给自己挖坑。
    中文必须保留——叫"设备借用制度-v1.md"比叫"a1b2c3.md"有用得多。
    """
    name = Path(raw.replace("\\", "/")).name.strip()
    cleaned = "".join(
        ch if (ch.isalnum() or ch in "._-" or "\u4e00" <= ch <= "\u9fff") else "_" for ch in name
    ).strip("._")
    return cleaned[:80]


def _decode(data: bytes) -> str:
    """
    解码上传内容。UTF-8 优先，失败再试 GB18030。

    为什么不是"统一要求 UTF-8"：企业里大量制度文档是 Windows 上另存的 GBK，
    让管理员为此去转码，实际结果就是他把文件丢进聊天框说"你帮我弄一下"。
    这里顺手转掉，入库统一成 UTF-8，后续环节就不用再关心编码。
    """
    for enc in ("utf-8-sig", "utf-8", "gb18030"):
        try:
            return data.decode(enc)
        except UnicodeDecodeError:
            continue
    raise ApiError("KB_UPLOAD_INVALID", "文件编码无法识别，请另存为 UTF-8 后重试")


async def save_upload(filename: str, data: bytes) -> dict[str, Any]:
    """保存上传的制度文档，返回 doc_key。不做重建，重建由调用方决定时机。"""
    if not filename:
        raise ApiError("KB_UPLOAD_INVALID", "请选择要上传的文件")
    if Path(filename).suffix.lower() not in UPLOAD_SUFFIXES:
        raise ApiError("KB_UPLOAD_INVALID", "只支持 .md / .markdown / .txt；PDF 请先转换成 markdown")
    if not data:
        raise ApiError("KB_UPLOAD_INVALID", "文件是空的")
    if len(data) > MAX_UPLOAD_BYTES:
        raise ApiError("KB_UPLOAD_TOO_LARGE", f"文件 {len(data) // 1024}KB，上限 {MAX_UPLOAD_BYTES // 1024}KB")

    text = _decode(data)
    if not text.strip():
        raise ApiError("KB_UPLOAD_INVALID", "文件里没有任何正文")

    safe = _safe_name(filename)
    if not safe or Path(safe).suffix.lower() not in UPLOAD_SUFFIXES:
        raise ApiError("KB_UPLOAD_INVALID", "文件名清洗后不可用，请改名后重试")

    doc_key = f"{UPLOAD_PREFIX}/{safe}"
    if await db.get_kb_document(doc_key) is not None or (upload_dir() / safe).exists():
        # 直接覆盖是危险的：同名往往意味着"新版替换旧版"，但同名也意味着
        # 两份不同制度被叫成了一个名字。让人自己决定删还是改名，成本更低。
        raise ApiError("KB_DOC_EXISTS", f"已存在同名文档《{safe}》，请先删除或改名")

    def _write() -> None:
        upload_dir().mkdir(parents=True, exist_ok=True)
        tmp = upload_dir() / f".{safe}.tmp"
        tmp.write_text(text, encoding="utf-8")
        os.replace(tmp, upload_dir() / safe)  # 原子替换，避免半截文件被扫到

    await asyncio.to_thread(_write)

    stat = (upload_dir() / safe).stat()
    return {
        "doc_key": doc_key,
        "display_name": safe,
        "origin": "uploaded",
        "size_bytes": stat.st_size,
        "chars": len(text),
    }


async def trash_upload(doc_key: str) -> str:
    """把上传的文件移到 .trash/，返回移到的路径。内置文档不允许删除。"""
    if not doc_key.startswith(f"{UPLOAD_PREFIX}/"):
        raise ApiError("KB_DOC_BUILTIN", "内置制度文档只能停用，不能删除")

    name = doc_key[len(UPLOAD_PREFIX) + 1 :]
    src = upload_dir() / name
    if not src.exists():
        raise ApiError("KB_DOC_NOT_FOUND", "磁盘上已经没有这个文件了")

    def _move() -> str:
        trash_dir().mkdir(parents=True, exist_ok=True)
        stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
        dest = trash_dir() / f"{stamp}-{name}"
        shutil.move(str(src), str(dest))
        return str(dest)

    return await asyncio.to_thread(_move)
