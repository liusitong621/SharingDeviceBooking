#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
===============================================================================
共享设备借用 · 阶段 1 最小原型（单文件 / 无 Web / 无数据库）
===============================================================================

目标（只验证一件事）：
    本地 markdown 文档 -> 500 字切片 -> 向量化入 FAISS -> BM25 + 向量混合检索
    -> top3 拼进提示词 -> 调 LLM 生成带引用的回答。

不做（留给阶段 2+）：
    多轮会话、权限、数据库、Web/SSE、增量索引、重排模型、评测集。

运行方式：
    # 1) 零配置跑通检索链路（不调 LLM，用本地哈希向量兜底，验证切片与召回）
    python scripts/stage1_rag_prototype.py --no-llm

    # 2) 配置好模型后做真实问答
    export LLM_PROVIDER=qwen
    export LLM_API_KEY=sk-xxxx
    python scripts/stage1_rag_prototype.py

    # 3) 单次提问（不进交互循环）
    python scripts/stage1_rag_prototype.py --question "设备借用时段是怎么规定的？"

    # 4) 只索引手册/制度类文档（重要）
    python scripts/stage1_rag_prototype.py --pattern '一页纸*.md'

    关于语料：RAG 的召回上限由语料决定，不由算法决定。
    本项目 docs/ 下同时存在调研报告、FDE 训练记录和需求清单，它们通篇都在讨论
    "设备/预约/怎么"，会大量稀释真正的规程文档。真实使用时请把 500 字切片
    指向手册目录（如 docs/manuals/），或用 --pattern 只选手册类文件。

依赖：
    pip install numpy faiss-cpu jieba httpx
    （faiss / jieba 都有降级路径，装了才生效，不装也能跑）

代码分区（【核心】= 阶段 2 直接复用，【原型】= 阶段 2 丢弃重写）：
    【核心 C1】Chunk 数据模型 + 文档加载 + 500 字切片
    【核心 C2】中文分词（jieba，缺失时退化为字符二元组）
    【核心 C3】BM25 倒排索引（自实现，约 40 行，便于替换为 PG 全文检索）
    【核心 C4】向量化后端（API / 本地兜底）+ FAISS 索引
    【核心 C5】混合检索：RRF 融合两路召回
    【核心 C6】LLM 客户端（异步、OpenAI 兼容协议、供应商可配置）
    【核心 C7】提示词组装（强制引用、禁止编造）
    【原型 N1】索引缓存落盘（阶段 2 换成数据库 + 版本表）
    【原型 N2】命令行入口（阶段 2 换成 FastAPI 路由）
===============================================================================
"""

from __future__ import annotations

# ---- 标准库 ----
import argparse
import asyncio
import hashlib
import json
import math
import os
import re
import sys
import time
from dataclasses import dataclass, asdict
from pathlib import Path
from typing import Sequence

# ---- 三方库（除 numpy 外全部允许缺失，缺失时自动降级）----
import numpy as np

try:
    import faiss  # type: ignore  # 向量检索；缺失时退化为 numpy 暴力内积
    HAS_FAISS = True
except ImportError:
    HAS_FAISS = False

import warnings

try:
    # jieba 在 Python 3.13 下会因正则转义产生 SyntaxWarning，与功能无关，屏蔽掉
    with warnings.catch_warnings():
        warnings.simplefilter("ignore", SyntaxWarning)
        import jieba  # type: ignore  # 中文分词；缺失时退化为字符二元组
    import logging as _logging

    jieba.setLogLevel(_logging.ERROR)  # 关掉首次加载词典时的冗余输出
    HAS_JIEBA = True
except ImportError:
    HAS_JIEBA = False

try:
    import httpx  # type: ignore  # 调 LLM / Embedding 的 HTTP 客户端
    HAS_HTTPX = True
except ImportError:
    HAS_HTTPX = False


# =============================================================================
# 0. 配置层：一切来自环境变量，换供应商不改代码
# =============================================================================
# 设计要点：把「供应商差异」全部收敛到下面这个预设表里。
# 业务代码只认 base_url + model 两个字符串，这就是阶段 2 的「防腐层」雏形。
# 注意：这里只列 OpenAI 兼容协议的供应商，因为它们的 /chat/completions 与
#       /embeddings 请求体格式一致，一个客户端就能覆盖绝大多数情况。
# =============================================================================

PROVIDER_PRESETS: dict[str, dict] = {
    # 供应商名: {对话地址, 默认对话模型, 向量地址, 默认向量模型, 向量维度}
    "openai": {
        "chat_base": "https://api.openai.com/v1",
        "chat_model": "gpt-4o-mini",
        "embed_base": "https://api.openai.com/v1",
        "embed_model": "text-embedding-3-small",
        "embed_dim": 1536,
    },
    "deepseek": {
        "chat_base": "https://api.deepseek.com/v1",
        "chat_model": "deepseek-chat",
        # DeepSeek 官方暂不提供 embedding 接口，所以向量后端退回本地兜底
        "embed_base": "",
        "embed_model": "",
        "embed_dim": 256,
    },
    "qwen": {
        # 阿里云百炼的 OpenAI 兼容模式
        "chat_base": "https://dashscope.aliyuncs.com/compatible-mode/v1",
        "chat_model": "qwen-plus",
        "embed_base": "https://dashscope.aliyuncs.com/compatible-mode/v1",
        "embed_model": "text-embedding-v3",
        "embed_dim": 1024,
    },
    "ollama": {
        # 本机模型，零成本调试用
        "chat_base": "http://127.0.0.1:11434/v1",
        "chat_model": "qwen2.5:7b",
        "embed_base": "http://127.0.0.1:11434/v1",
        "embed_model": "nomic-embed-text",
        "embed_dim": 768,
    },
}


@dataclass
class Settings:
    """运行时配置。阶段 2 用 pydantic-settings 重写，字段一一对应。"""

    provider: str = "openai"
    api_key: str = ""
    chat_base: str = ""
    chat_model: str = ""
    embed_base: str = ""
    embed_model: str = ""
    embed_dim: int = 256
    embed_backend: str = "auto"   # auto | api | hash
    temperature: float = 0.2      # 制度类问答必须低温，否则容易"自由发挥"
    max_tokens: int = 800
    chunk_size: int = 500         # 按需求：500 字
    chunk_overlap: int = 80       # 10%~15% 重叠，防止答案正好被切断
    top_k: int = 3                # 按需求：top3

    @classmethod
    def from_env(cls, provider: str | None = None) -> "Settings":
        """从环境变量装配配置；环境变量缺省时回落到供应商预设。"""
        name = (provider or os.getenv("LLM_PROVIDER") or "openai").lower()
        preset = PROVIDER_PRESETS.get(name, PROVIDER_PRESETS["openai"])

        s = cls(provider=name)
        s.api_key = os.getenv("LLM_API_KEY", "")
        # 允许单独覆盖，便于接自建网关 / vLLM / 中转站
        s.chat_base = os.getenv("LLM_BASE_URL") or preset["chat_base"]
        s.chat_model = os.getenv("LLM_MODEL") or preset["chat_model"]
        s.embed_base = os.getenv("EMBED_BASE_URL") or preset["embed_base"]
        s.embed_model = os.getenv("EMBED_MODEL") or preset["embed_model"]
        s.embed_dim = int(os.getenv("EMBED_DIM") or preset["embed_dim"])
        s.embed_backend = os.getenv("EMBED_BACKEND", "auto")
        return s

    @property
    def llm_ready(self) -> bool:
        """是否具备调 LLM 的最小条件（地址 + 模型 + 密钥）。"""
        return bool(self.chat_base and self.chat_model and self.api_key)


# =============================================================================
# 【核心 C1】Chunk 数据模型 + 文档加载 + 500 字切片
# -----------------------------------------------------------------------------
# 阶段 2 去向：app/agent/rag/chunker.py
# 复用理由：切片策略与引用元数据是全系统最稳定的部分，几乎不会变。
# 三个关键设计：
#   1) chunk 必须带 heading（章节路径）与 source（文件），否则回答里的引用没法落到位置；
#   2) 按段落聚合到 500 字，而不是机械地每 500 字符硬切——硬切会把表格和句子劈断；
#   3) 超长段落才硬切并保留 overlap，避免"答案横跨两个 chunk 两半都不完整"。
# =============================================================================


@dataclass
class Chunk:
    """一个可被检索的最小文本单元。"""

    chunk_id: str    # 稳定 ID：<文件名>#<序号>，用于去重、引用、增量更新
    source: str      # 相对路径，回答里要展示给用户
    heading: str     # 完整章节路径，如 "一页纸需求清单 > 四、时段规则"，用于展示出处
    title: str       # 叶子标题，如 "四、时段规则"，只用于检索（见 embed_text）
    text: str        # 正文
    start: int       # 在原文中的字符偏移，便于回溯原文（调试用）


HEADING_RE = re.compile(r"^(#{1,6})\s+(.+?)\s*$")


def split_text(text: str, size: int = 500, overlap: int = 80) -> list[str]:
    """
    把一段正文切成若干约 size 字的片段。

    策略（比"每 500 字符切一刀"好得多）：
      1) 先按空行拆成段落；
      2) 把段落依次塞进缓冲区，塞不下就落盘一个 chunk（尽量在段落边界切断）；
      3) 单段本身超过 size 时，才做硬切，并在相邻片段间保留 overlap。
    """
    text = text.strip()
    if not text:
        return []

    paragraphs = [p.strip() for p in re.split(r"\n\s*\n", text) if p.strip()]
    chunks: list[str] = []
    buf = ""

    for para in paragraphs:
        # 情况 A：单段超长，先把手里的缓冲区落盘，再单独硬切这一段
        if len(para) > size:
            if buf:
                chunks.append(buf)
                buf = ""
            step = max(1, size - overlap)
            for i in range(0, len(para), step):
                piece = para[i : i + size]
                if piece.strip():
                    chunks.append(piece)
                if i + size >= len(para):
                    break
            continue

        # 情况 B：塞得下就继续攒，塞不下就先落盘再开新的
        if len(buf) + len(para) + 2 <= size:
            buf = f"{buf}\n\n{para}" if buf else para
        else:
            if buf:
                chunks.append(buf)
            buf = para

    if buf:
        chunks.append(buf)
    return chunks


def load_markdown(path: Path, docs_root: Path, size: int, overlap: int) -> list[Chunk]:
    """
    读取单个 markdown，按标题层级切段，再对每段做 500 字切片。

    为什么要先按标题切：markdown 里的 ## 标题天然是"主题边界"。
    先切标题再切字数，能保证一个 chunk 不会横跨两个不相关的主题，
    这对后续 BM25 的区分度和 LLM 的回答质量都有直接好处。
    """
    raw = path.read_text(encoding="utf-8", errors="ignore")
    rel = str(path.relative_to(docs_root))

    lines = raw.split("\n")
    heading_stack: list[str] = []           # 用栈维护 "一级 > 二级 > 三级" 的路径
    # (章节完整路径, 叶标题, 正文, 起始偏移)
    sections: list[tuple[str, str, str, int]] = []
    buf: list[str] = []
    buf_start = 0
    offset = 0

    def flush() -> None:
        """把当前累积的正文落成一个 section。"""
        nonlocal buf, buf_start
        body = "\n".join(buf).strip()
        if body:
            sections.append(
                (
                    " > ".join(heading_stack) or rel,          # 完整路径：给人看
                    heading_stack[-1] if heading_stack else rel,  # 叶标题：给检索用
                    body,
                    buf_start,
                )
            )
        buf = []

    for line in lines:
        m = HEADING_RE.match(line)
        if m:
            flush()
            level = len(m.group(1))
            title = m.group(2).strip()
            # 同级或更高级标题出现时，先把栈弹到合适层级
            heading_stack = heading_stack[: level - 1]
            heading_stack.append(title)
            buf_start = offset + len(line) + 1
        else:
            if not buf:
                buf_start = offset
            buf.append(line)
        offset += len(line) + 1
    flush()

    chunks: list[Chunk] = []
    for heading, title, body, base in sections:
        for piece in split_text(body, size, overlap):
            chunks.append(
                Chunk(
                    chunk_id=f"{rel}#{len(chunks):04d}",
                    source=rel,
                    heading=heading,
                    title=title,
                    text=piece,
                    start=base,
                )
            )
    return chunks


def embed_text(c: Chunk) -> str:
    """
    索引侧使用的文本 = 叶标题 + 正文。

    这里要和「喂给模型的文本」严格区分：
      - 索引侧加标题：标题是主题的最强信号，能显著提升召回准确率；
      - 提示词侧只用正文与完整路径：标题已经单独作为「出处」展示，重复放进上下文只会浪费 token。

    为什么只用「叶标题」而不是完整路径：
      完整路径会把文档级前缀（如"共享设备借用"）重复写进每个切片，
      同一个词在所有切片里都出现，IDF 趋近于零，等于白加噪声。
      这是 RAG 调优里最容易忽略、收益又最大的一处细节。
    """
    return f"{c.title}\n{c.text}"


def load_corpus(docs_root: Path, size: int, overlap: int, pattern: str = "*.md") -> list[Chunk]:
    """递归加载目录下所有匹配的 markdown，拼成检索语料。"""
    files = sorted(docs_root.rglob(pattern))
    if not files:
        raise SystemExit(f"[错误] 在 {docs_root} 下没有找到匹配 {pattern} 的文件")

    all_chunks: list[Chunk] = []
    for f in files:
        got = load_markdown(f, docs_root, size, overlap)
        print(f"  - {f.relative_to(docs_root)}：{len(got)} 个切片")
        all_chunks.extend(got)
    return all_chunks


# =============================================================================
# 【核心 C2】中文分词
# -----------------------------------------------------------------------------
# 阶段 2 去向：app/agent/rag/tokenizer.py
# 为什么必须单独抽出来：BM25 的效果 80% 取决于分词。
# 中文没有空格，直接按字符切会让"借用"和"借 用"变成两个 token，召回质量崩塌。
# jieba 缺失时退回「单字 + 二元组」，虽然不是最优，但比按空格切强得多。
# =============================================================================

# 极简停用词：只去无信息量的虚词与标点，不做过度过滤（会误伤"不超过"这类关键否定词）
STOPWORDS = set("的了和是在与及或就都而及其为以于对从把被这那有")

# 纯中文词的正则，用于下面的「字词混合」处理
CJK_RE = re.compile(r"^[\u4e00-\u9fff]+$")


def tokenize(text: str) -> list[str]:
    """
    把文本切成 BM25 用的 token 序列（已转小写、去停用词）。

    采用「字词混合」索引：中文既入词、也入单字。原因是中文分词的未登录词问题——
    用户问"每天可以约几格"，jieba 会把"几格"当成一个词，而正文里写的是"6 格/天"，
    两边永远对不上。补上单字后，靠"格""天"这些字就能建立匹配，召回明显更稳。
    代价是倒排表变大、打分离散度略降，由 IDF 与 k1/b 参数兜住。
    """
    text = text.lower()
    if HAS_JIEBA:
        # lcut_for_search 会额外产出长词的子词，对短查询召回更友好
        words = jieba.lcut_for_search(text)
        # 中文词再补一遍单字（字词混合）
        uni = [ch for w in words if len(w) >= 2 and CJK_RE.match(w) for ch in w]
        raw = words + uni
    else:
        # 降级方案：中文取单字 + 相邻两字，英文/数字按词
        chars = [c for c in text if not c.isspace()]
        raw = re.findall(r"[a-z0-9]+", text) + chars + [
            a + b for a, b in zip(chars, chars[1:])
        ]

    tokens: list[str] = []
    for t in raw:
        t = t.strip()
        if not t or t in STOPWORDS:
            continue
        if re.fullmatch(r"[\W_]+", t):   # 纯标点丢弃
            continue
        tokens.append(t)
    return tokens


# =============================================================================
# 【核心 C3】BM25 索引
# -----------------------------------------------------------------------------
# 阶段 2 去向：app/agent/rag/bm25.py（或整体替换为 PG 的 tsvector + GIN 索引）
# 为什么自己实现而不引 rank_bm25：只有 40 行，依赖更少，且参数（k1/b）可调；
# 阶段 2 若走 PostgreSQL，只要把 search() 换成一条 SQL，接口不用动。
# 为什么必须有 BM25：向量检索对"专有名词、设备编号、精确数字"很不敏感，
# 而这类问题恰恰是设备借用场景的高频问题（"08:00 到 20:00"、"asset_no"）。
# =============================================================================


class BM25Index:
    """标准 BM25（Okapi）实现，稀疏检索那一路。"""

    def __init__(self, docs_tokens: Sequence[Sequence[str]], k1: float = 1.5, b: float = 0.75):
        self.k1 = k1          # 词频饱和系数
        self.b = b            # 文档长度归一化系数
        self.docs = [list(d) for d in docs_tokens]
        self.n_docs = len(self.docs)
        self.doc_len = [len(d) for d in self.docs]
        self.avg_len = (sum(self.doc_len) / self.n_docs) if self.n_docs else 0.0

        # 倒排表：token -> {doc_idx: 词频}
        self.inverted: dict[str, dict[int, int]] = {}
        for i, toks in enumerate(self.docs):
            for t in toks:
                self.inverted.setdefault(t, {})
                self.inverted[t][i] = self.inverted[t].get(i, 0) + 1

    def _idf(self, token: str) -> float:
        """IDF：包含该词的文档越少，权重越高。用 +0.5 平滑避免除零。"""
        df = len(self.inverted.get(token, {}))
        return math.log(1 + (self.n_docs - df + 0.5) / (df + 0.5))

    def search(self, query_tokens: Sequence[str], top_k: int = 20) -> list[tuple[int, float]]:
        """返回 [(文档下标, 得分)]，按得分降序。"""
        scores: dict[int, float] = {}
        for t in query_tokens:
            postings = self.inverted.get(t)
            if not postings:
                continue
            idf = self._idf(t)
            for doc_idx, tf in postings.items():
                dl = self.doc_len[doc_idx]
                denom = tf + self.k1 * (1 - self.b + self.b * dl / (self.avg_len or 1))
                scores[doc_idx] = scores.get(doc_idx, 0.0) + idf * tf * (self.k1 + 1) / denom
        ranked = sorted(scores.items(), key=lambda kv: kv[1], reverse=True)
        return ranked[:top_k]


# =============================================================================
# 【核心 C4】向量化后端 + FAISS 索引
# -----------------------------------------------------------------------------
# 阶段 2 去向：app/agent/rag/embedder.py + app/agent/rag/indexer.py
# 两个后端共存是刻意的：
#   - ApiEmbedder：真实语义向量，生产用；
#   - HashEmbedder：本地哈希向量，零配置冒烟测试用，保证"没有密钥也能跑通链路"。
# 这不是妥协，而是「降级路径」设计——阶段 2 里 LLM 挂了同样要能退回纯检索。
# =============================================================================


class HashEmbedder:
    """
    本地哈希向量（Hashing Vectorizer 思路）：把字符 n-gram 哈希到固定维度。

    它的能力边界必须说清：只捕捉字面重合，没有语义。
    用途仅限"没有 embedding API 时验证管道是否通畅"，绝不能用于生产。
    """

    name = "hash-local"

    def __init__(self, dim: int = 256):
        self.dim = dim

    def _one(self, text: str) -> np.ndarray:
        v = np.zeros(self.dim, dtype=np.float32)
        chars = [c for c in text if not c.isspace()]
        grams = chars + [a + b for a, b in zip(chars, chars[1:])]
        for g in grams:
            h = int.from_bytes(hashlib.blake2b(g.encode("utf-8"), digest_size=4).digest(), "big")
            idx = h % self.dim
            sign = 1.0 if (h >> 31) & 1 else -1.0   # 符号哈希，减少碰撞带来的系统性偏差
            v[idx] += sign
        norm = np.linalg.norm(v)
        return v / norm if norm > 0 else v

    async def embed_texts(self, texts: Sequence[str]) -> np.ndarray:
        return np.vstack([self._one(t) for t in texts]) if texts else np.zeros((0, self.dim), np.float32)


class ApiEmbedder:
    """通过 OpenAI 兼容的 /embeddings 接口取向量。"""

    name = "api"

    def __init__(self, client: "LLMClient"):
        self.client = client

    async def embed_texts(self, texts: Sequence[str]) -> np.ndarray:
        return await self.client.embed(texts)


class VectorIndex:
    """
    向量索引。装了 faiss 用 IndexFlatIP（精确内积），没装就用 numpy 暴力算。

    为什么用内积而不是 L2：向量事先做过 L2 归一化，此时内积 == 余弦相似度，
    排序结果与余弦完全一致，但少一次开方，且 FAISS 的 IP 索引更常用。
    """

    def __init__(self, dim: int):
        # dim 以实际向量维度为准（见 build），避免"配置维度"与"供应商真实维度"不一致
        self.dim = dim
        self.backend = "faiss" if HAS_FAISS else "numpy"
        self._index = faiss.IndexFlatIP(dim) if HAS_FAISS else None
        self._mat: np.ndarray | None = None

    def build(self, vectors: np.ndarray) -> None:
        vectors = np.ascontiguousarray(vectors.astype(np.float32))
        self.dim = int(vectors.shape[1])   # 以真实维度覆盖构造时的值
        # 归一化：这一步漏了，内积就不再等价于余弦，排序会出错
        norms = np.linalg.norm(vectors, axis=1, keepdims=True)
        vectors = vectors / np.where(norms == 0, 1.0, norms)
        if HAS_FAISS:
            self._index = faiss.IndexFlatIP(self.dim)
            self._index.add(vectors)
        else:
            self._mat = vectors

    def search(self, query: np.ndarray, top_k: int = 20) -> list[tuple[int, float]]:
        q = query.astype(np.float32).reshape(1, -1)
        n = np.linalg.norm(q)
        if n > 0:
            q = q / n
        if HAS_FAISS:
            sims, idxs = self._index.search(q, min(top_k, self._index.ntotal))
            return [(int(i), float(s)) for i, s in zip(idxs[0], sims[0]) if i >= 0]
        assert self._mat is not None
        sims = (self._mat @ q[0])
        order = np.argsort(-sims)[:top_k]
        return [(int(i), float(sims[i])) for i in order]


# =============================================================================
# 【核心 C6】LLM 客户端（先写它，因为 C4 的 ApiEmbedder 要依赖它）
# -----------------------------------------------------------------------------
# 阶段 2 去向：app/llm/client.py
# 三条设计原则，阶段 2 必须保持：
#   1) 只依赖 OpenAI 兼容协议，不 import 任何厂商 SDK——换供应商只改配置；
#   2) 全异步（httpx.AsyncClient），因为阶段 2 是 FastAPI，同步客户端会拖垮事件循环；
#   3) 错误统一成 LLMError 并带上可读原因，业务层不必知道 HTTP 细节。
# =============================================================================


class LLMError(RuntimeError):
    """LLM / Embedding 调用的统一异常。阶段 2 由全局异常处理器转成 502。"""


class LLMClient:
    """OpenAI 兼容协议的异步客户端，同时负责对话与向量化。"""

    def __init__(self, settings: Settings):
        if not HAS_HTTPX:
            raise SystemExit("[错误] 缺少 httpx，请先 pip install httpx")
        self.s = settings
        # 长超时：大模型首字延迟常在 3~10 秒，超时设短了会误杀
        self._http = httpx.AsyncClient(timeout=httpx.Timeout(90.0, connect=10.0))

    async def aclose(self) -> None:
        await self._http.aclose()

    # ------------------------------------------------------------------ 对话
    async def chat(
        self,
        messages: list[dict],
        temperature: float | None = None,
        max_tokens: int | None = None,
    ) -> str:
        """一次非流式对话，返回纯文本。阶段 2 的流式版本另加 stream_chat()。"""
        if not self.s.api_key:
            raise LLMError("未配置 LLM_API_KEY")

        payload = {
            "model": self.s.chat_model,
            "messages": messages,
            "temperature": self.s.temperature if temperature is None else temperature,
            "max_tokens": self.s.max_tokens if max_tokens is None else max_tokens,
        }
        url = f"{self.s.chat_base.rstrip('/')}/chat/completions"
        try:
            resp = await self._http.post(
                url,
                json=payload,
                headers={"Authorization": f"Bearer {self.s.api_key}"},
            )
        except httpx.HTTPError as e:
            raise LLMError(f"请求 {url} 失败：{e}") from e

        if resp.status_code >= 400:
            # 把响应体前 300 字带出来，否则排查只能靠猜
            raise LLMError(f"LLM 返回 {resp.status_code}：{resp.text[:300]}")

        data = resp.json()
        try:
            return data["choices"][0]["message"]["content"].strip()
        except (KeyError, IndexError) as e:
            raise LLMError(f"响应结构不符合 OpenAI 协议：{str(data)[:300]}") from e

    # ------------------------------------------------------------------ 向量
    async def embed(self, texts: Sequence[str]) -> np.ndarray:
        """批量取向量。失败的批次直接抛错，由上层决定是否降级。"""
        if not (self.s.embed_base and self.s.embed_model and self.s.api_key):
            raise LLMError("未配置 embedding 地址/模型/密钥")

        url = f"{self.s.embed_base.rstrip('/')}/embeddings"
        out: list[list[float]] = []
        # 分批：多数供应商单次上限 10~64 条，这里保守取 16
        for i in range(0, len(texts), 16):
            batch = list(texts[i : i + 16])
            try:
                resp = await self._http.post(
                    url,
                    json={"model": self.s.embed_model, "input": batch},
                    headers={"Authorization": f"Bearer {self.s.api_key}"},
                )
            except httpx.HTTPError as e:
                raise LLMError(f"向量化请求失败：{e}") from e
            if resp.status_code >= 400:
                raise LLMError(f"向量化返回 {resp.status_code}：{resp.text[:300]}")
            body = resp.json()
            # 按 index 排序，防止供应商乱序返回导致向量与文本错位
            items = sorted(body["data"], key=lambda d: d.get("index", 0))
            out.extend([d["embedding"] for d in items])
        return np.asarray(out, dtype=np.float32)


# =============================================================================
# 【核心 C5】混合检索：BM25 + 向量，用 RRF 融合
# -----------------------------------------------------------------------------
# 阶段 2 去向：app/agent/rag/retriever.py
# 为什么用 RRF（Reciprocal Rank Fusion）而不是加权平均分数：
#   BM25 的分数是无上界的，余弦相似度在 [-1,1]，两者量纲完全不同，
#   直接加权需要归一化，而归一化系数会随查询漂移。
#   RRF 只看名次不看绝对分值，一条公式就够，且被证明在混合检索里非常稳。
#   公式：score(d) = Σ 1 / (k + rank_i(d))，k 取 60 是论文常用值。
# =============================================================================


@dataclass
class Hit:
    """一条召回结果。阶段 2 会再挂上版本号与生效期。"""

    chunk: Chunk
    score: float          # 融合后的 RRF 分
    bm25_rank: int | None # 在 BM25 里的名次（None = 未召回）
    vec_rank: int | None  # 在向量路里的名次（None = 未召回）


async def hybrid_search(
    query: str,
    chunks: list[Chunk],
    bm25: BM25Index,
    vindex: VectorIndex,
    embedder,
    top_k: int = 3,
    pool: int = 20,
    rrf_k: int = 60,
) -> list[Hit]:
    """
    两路各取 pool 条，再 RRF 融合取 top_k。

    pool 的意义：单路召回 20 条是有意的扩大（recall 优先），
    最终 top3 交给 LLM 前才收窄（precision 优先）。
    这个「先粗后精」的两段式，是所有 RAG 系统的标准做法。
    """
    # ---- 第 1 路：稀疏（关键词）----
    bm25_hits = bm25.search(tokenize(query), top_k=pool)
    bm25_rank = {idx: r for r, (idx, _) in enumerate(bm25_hits, start=1)}

    # ---- 第 2 路：稠密（向量）----
    qvec = (await embedder.embed_texts([query]))[0]
    vec_hits = vindex.search(qvec, top_k=pool)
    vec_rank = {idx: r for r, (idx, _) in enumerate(vec_hits, start=1)}

    # ---- RRF 融合 ----
    fused: dict[int, float] = {}
    for idx, r in bm25_rank.items():
        fused[idx] = fused.get(idx, 0.0) + 1.0 / (rrf_k + r)
    for idx, r in vec_rank.items():
        fused[idx] = fused.get(idx, 0.0) + 1.0 / (rrf_k + r)

    ranked = sorted(fused.items(), key=lambda kv: kv[1], reverse=True)[:top_k]
    return [
        Hit(chunk=chunks[i], score=s, bm25_rank=bm25_rank.get(i), vec_rank=vec_rank.get(i))
        for i, s in ranked
    ]


# =============================================================================
# 【核心 C7】提示词组装
# -----------------------------------------------------------------------------
# 阶段 2 去向：app/agent/prompts/answer_with_citation.md + prompt_builder.py
# 三条硬约束（对应架构里的 guardrails，阶段 2 一条都不能松）：
#   1) 只能依据资料回答，资料没有就说没有——这是防幻觉的第一道闸；
#   2) 必须标注 [编号] 引用——让答案可追溯，也让用户能自己复核；
#   3) 数值必须原样照抄——「500 字」「08:00-20:00」这类数字被改写就是事故。
# =============================================================================

SYSTEM_PROMPT = """你是「共享设备借用系统」的制度与设备手册问答助手。

严格遵守以下规则：
1. 只能依据【资料】中的内容回答，不得使用资料之外的知识，不得推测。
2. 如果【资料】无法回答，直接回答「资料中未收录该内容」，并说明建议联系设备管理员。
   绝不允许编造制度条款、时间、数值或设备参数。
3. 每个结论后面用 [编号] 标注来源，编号必须来自资料前的方括号，可同时标多个，如 [1][2]。
4. 涉及时间、数量、金额、设备编号、型号等事实时，必须与原文完全一致，不得换算或改写。
5. 用简体中文，先给结论，再给依据，不要复述问题，不要输出与问题无关的章节。"""


def build_messages(question: str, hits: list[Hit]) -> list[dict]:
    """把召回片段拼成带编号的上下文，再和问题一起组消息。"""
    blocks: list[str] = []
    for i, h in enumerate(hits, start=1):
        # 编号 + 出处 + 章节，这个顺序是给模型看的，也是给用户看的
        blocks.append(
            f"[{i}] 来源：{h.chunk.source} §{h.chunk.heading}\n{h.chunk.text.strip()}"
        )
    context = "\n\n---\n\n".join(blocks) if blocks else "（无检索结果）"

    user_content = (
        f"【资料】\n{context}\n\n"
        f"【问题】\n{question}\n\n"
        f"请依据上面的资料回答，并用 [编号] 标注引用。"
    )
    return [
        {"role": "system", "content": SYSTEM_PROMPT},
        {"role": "user", "content": user_content},
    ]


# =============================================================================
# 【原型 N1】索引缓存落盘
# -----------------------------------------------------------------------------
# 阶段 2 会被彻底替换：chunk 进数据库（manual_chunks 表），
# 向量进 pgvector，版本与生效期挂在外键上。
# 这里落盘只有一个目的：避免每次运行都重新花 token 做 embedding。
# 用「文件指纹 + 模型名 + 切片参数」做缓存键，任何一个变了就重建。
# =============================================================================


CACHE_DIR = Path(".rag_cache")


def corpus_fingerprint(docs_root: Path, s: Settings, pattern: str = "*.md") -> str:
    """把"语料内容 + 切片参数 + 向量模型"揉成一个指纹，用于判断缓存是否失效。"""
    h = hashlib.sha256()
    for f in sorted(docs_root.rglob(pattern)):
        h.update(f.name.encode())
        h.update(str(f.stat().st_size).encode())
        h.update(str(int(f.stat().st_mtime)).encode())
    h.update(f"v3|{pattern}|{s.chunk_size}|{s.chunk_overlap}|{s.embed_model}|{s.embed_backend}".encode())
    return h.hexdigest()[:16]


def save_cache(fp: str, chunks: list[Chunk], vectors: np.ndarray) -> None:
    if not CACHE_DIR.exists():          # 先判断再创建，避免某些受限环境下 exist_ok 失效
        CACHE_DIR.mkdir(parents=True, exist_ok=True)
    (CACHE_DIR / f"{fp}.chunks.json").write_text(
        json.dumps([asdict(c) for c in chunks], ensure_ascii=False), encoding="utf-8"
    )
    np.save(CACHE_DIR / f"{fp}.vectors.npy", vectors)


def load_cache(fp: str) -> tuple[list[Chunk], np.ndarray] | None:
    cpath = CACHE_DIR / f"{fp}.chunks.json"
    vpath = CACHE_DIR / f"{fp}.vectors.npy"
    if not (cpath.exists() and vpath.exists()):
        return None
    chunks = [Chunk(**d) for d in json.loads(cpath.read_text(encoding="utf-8"))]
    return chunks, np.load(vpath)


# =============================================================================
# 装配层：把上面这些零件组装成可用的知识库
# =============================================================================


@dataclass
class KnowledgeBase:
    """检索所需的全部构件。阶段 2 会由 FastAPI 的依赖注入统一构造一次。"""

    chunks: list[Chunk]
    bm25: BM25Index
    vindex: VectorIndex
    embedder: object
    backend_name: str


async def build_knowledge_base(
    docs_root: Path, s: Settings, rebuild: bool = False, pattern: str = "*.md"
) -> KnowledgeBase:
    """加载语料 -> 向量化 -> 建两套索引。有缓存就直接读。"""
    fp = corpus_fingerprint(docs_root, s, pattern)

    cached = None if rebuild else load_cache(fp)
    if cached:
        chunks, vectors = cached
        print(f"[缓存命中] {fp}：{len(chunks)} 个切片，跳过向量化（省掉 token 与等待）")
    else:
        print(f"[建索引] 扫描 {docs_root}（匹配 {pattern}）...")
        chunks = load_corpus(docs_root, s.chunk_size, s.chunk_overlap, pattern)
        print(f"[建索引] 共 {len(chunks)} 个切片，开始向量化 ...")

        embedder = await _make_embedder(s, allow_fallback=True)
        t0 = time.time()
        # 关键：把「章节标题」一起喂给向量化。
        # 标题往往比正文更能概括主题（"四、时段规则" 对"时段怎么规定"这类问题的区分度极高），
        # 只索引正文会让正确答案被一堆同样提到"设备/预约"的长文本挤下去。
        vectors = await embedder.embed_texts([embed_text(c) for c in chunks])
        print(f"[建索引] 向量化完成：{vectors.shape}，耗时 {time.time() - t0:.1f}s，后端 {embedder.name}")
        save_cache(fp, chunks, vectors)

    # 向量后端：重建时按配置，读缓存时也用同一套规则，保证查询侧与索引侧一致
    embedder = await _make_embedder(s, allow_fallback=True)
    # 索引维度以真实向量为准，不信任配置里的 embed_dim
    vindex = VectorIndex(vectors.shape[1])
    vindex.build(vectors)
    # 同理，BM25 侧也要把标题作为可检索文本，否则两路的信号不对齐
    bm25 = BM25Index([tokenize(embed_text(c)) for c in chunks])

    print(
        f"[就绪] 切片 {len(chunks)} | 向量后端 {embedder.name} | 索引后端 {vindex.backend} "
        f"| 分词 {'jieba' if HAS_JIEBA else '字符二元组'}"
    )
    return KnowledgeBase(chunks, bm25, vindex, embedder, embedder.name)


async def _make_embedder(s: Settings, allow_fallback: bool = True):
    """
    选择向量后端。这个函数是「降级策略」的落点，阶段 2 复用同样的思路：
      配置了 api 且能通 -> ApiEmbedder
      否则 -> HashEmbedder，并打印明确的警告（绝不静默降级）
    """
    want_api = s.embed_backend in ("auto", "api")
    if want_api and s.embed_base and s.embed_model and s.api_key and HAS_HTTPX:
        if s.embed_backend == "api":
            return ApiEmbedder(LLMClient(s))
        # auto 模式下先探测一次，避免跑到一半才失败
        try:
            probe = LLMClient(s)
            await probe.embed(["连接测试"])
            await probe.aclose()
            return ApiEmbedder(LLMClient(s))
        except Exception as e:  # noqa: BLE001 - 探测失败即降级，原因打印出来
            if not allow_fallback:
                raise
            print(f"[警告] Embedding 接口不可用（{e}），降级为本地哈希向量。")
            print("        此时向量路只能捕捉字面重合，语义检索能力相当于没有。")
    return HashEmbedder(256)


# =============================================================================
# 【原型 N2】命令行入口
# -----------------------------------------------------------------------------
# 阶段 2 替换为：POST /api/v1/chat（SSE 流式）+ 会话落库。
# 这里的交互循环只负责验证"检索质量 + 提示词效果"，不做任何会话状态管理。
# =============================================================================


def print_hits(hits: list[Hit]) -> None:
    """调试用：打印召回结果与两路名次，方便判断是哪一路起了作用。"""
    if not hits:
        print("  （无召回结果）")
        return
    for i, h in enumerate(hits, start=1):
        bm = f"BM25#{h.bm25_rank}" if h.bm25_rank else "BM25-"
        ve = f"向量#{h.vec_rank}" if h.vec_rank else "向量-"
        print(f"  [{i}] rrf={h.score:.4f} | {bm} | {ve} | {h.chunk.source} §{h.chunk.heading}")
        preview = h.chunk.text.replace("\n", " ")[:80]
        print(f"      {preview}...")


async def answer_once(question: str, kb: KnowledgeBase, s: Settings) -> None:
    """一次完整的问答：检索 -> 拼提示词 -> 调模型 -> 输出。"""
    print(f"\n[问题] {question}")

    hits = await hybrid_search(question, kb.chunks, kb.bm25, kb.vindex, kb.embedder, top_k=s.top_k)
    print(f"[检索] top{s.top_k} 召回如下：")
    print_hits(hits)

    if not hits:
        print("[回答] 资料中未收录该内容。")
        return

    if not s.llm_ready:
        # 没有密钥时不报错退出，而是把检索结果交出来——这正是阶段 2 的降级态
        print("\n[回答] （未配置 LLM_API_KEY，仅返回检索结果，以下为上面前 3 段的原文）")
        for i, h in enumerate(hits, start=1):
            body = h.chunk.text.strip()
            print(f"\n--- [{i}] {h.chunk.source} §{h.chunk.heading} ---\n{body[:300]}")
        return

    messages = build_messages(question, hits)
    client = LLMClient(s)
    try:
        t0 = time.time()
        text = await client.chat(messages)
        print(f"\n[回答]（{time.time() - t0:.1f}s，模型 {s.chat_model}）\n{text}")
    except LLMError as e:
        # 失败也要给出可用结果，而不是让用户看到一个空屏
        print(f"\n[回答] LLM 调用失败：{e}")
        print("       已降级为直接返回检索原文：")
        for i, h in enumerate(hits, start=1):
            print(f"\n--- [{i}] {h.chunk.source} §{h.chunk.heading} ---\n{h.chunk.text.strip()[:300]}")
    finally:
        await client.aclose()


async def amain() -> None:
    parser = argparse.ArgumentParser(description="共享设备借用 · 阶段1 RAG 原型")
    parser.add_argument("--docs", default="docs", help="markdown 目录，默认 docs")
    parser.add_argument("--pattern", default="*.md", help="文件匹配模式，如 '一页纸*.md' 可只索引单个文件")
    parser.add_argument("--question", default=None, help="单次提问，不给则进交互模式")
    parser.add_argument("--no-llm", action="store_true", help="只做检索，不调大模型")
    parser.add_argument("--rebuild", action="store_true", help="忽略缓存，重建索引")
    parser.add_argument("--topk", type=int, default=3, help="最终送给模型的片段数")
    parser.add_argument("--provider", default=None, help="openai | deepseek | qwen | ollama")
    parser.add_argument("--show-chunks", action="store_true", help="打印召回片段的完整正文")
    args = parser.parse_args()

    s = Settings.from_env(args.provider)
    s.top_k = args.topk

    # 顶部一眼看清当前配置，避免"以为在用 A 其实在跑 B"
    print("=" * 72)
    print(f"供应商 {s.provider} | 对话模型 {s.chat_model or '(未配置)'} | 密钥 {'已配置' if s.api_key else '未配置'}")
    print(f"切片 {s.chunk_size} 字 / 重叠 {s.chunk_overlap} | 最终 top{s.top_k}")
    print("=" * 72)

    docs_root = Path(args.docs)
    if not docs_root.exists():
        raise SystemExit(f"[错误] 目录不存在：{docs_root.resolve()}")

    kb = await build_knowledge_base(docs_root, s, rebuild=args.rebuild, pattern=args.pattern)

    if args.show_chunks:
        print("\n[片段全文]")
        for c in kb.chunks[:5]:
            print(f"\n--- {c.chunk_id} | {c.heading} ---\n{c.text}")

    if args.no_llm:
        s.api_key = ""   # 强制走"只检索"分支

    if args.question:
        await answer_once(args.question, kb, s)
        return

    print("\n进入交互模式：直接输入问题，输入 q 退出。")
    while True:
        try:
            q = input("\n> ").strip()
        except (EOFError, KeyboardInterrupt):
            print()
            break
        if q.lower() in ("q", "quit", "exit"):
            break
        if q:
            await answer_once(q, kb, s)


if __name__ == "__main__":
    # Windows 控制台默认不是 UTF-8，中文会乱码；显式转一次
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8")
    asyncio.run(amain())
