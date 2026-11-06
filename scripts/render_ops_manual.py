#!/usr/bin/env python3
"""把运维手册的 Markdown 渲染成可转 docx 的 HTML。

为什么自己写渲染器：项目不引入 markdown 依赖（requirements 里没有），
而手册需要能一条命令重新生成——否则改了 md、docx 还是旧的，两份内容迟早漂移。
只支持本手册实际用到的语法子集：标题、段落、GFM 表格、代码块、列表、引用、分隔线。

表格列宽按内容宽度自动分配并写成 inline style。这不是美化问题：docx 里
`<w:tblGrid>` 只认显式宽度，不给就均分，中文表头会被压成一字一行
（报价单那次「人天」竖排就是这么来的）。

用法：
    python3 scripts/render_ops_manual.py \
        --md docs/9.上线交付运行手册.md \
        --html output/ops-manual/manual.html
"""

from __future__ import annotations

import argparse
import html
import re
import unicodedata
from pathlib import Path

# ---------------------------------------------------------------- 行内元素

def _esc(text: str) -> str:
    return html.escape(text, quote=False)


def inline(text: str) -> str:
    """转义后处理 **粗体** 与 `代码`。先转义再替换，避免把标签当内容。"""
    out = _esc(text)
    out = re.sub(r"`([^`]+)`", r"<code>\1</code>", out)
    out = re.sub(r"\*\*([^*]+)\*\*", r"<strong>\1</strong>", out)
    return out


# ---------------------------------------------------------------- 表格列宽

def _disp_width(text: str) -> float:
    """按显示宽度算：中日韩字符 2，其余 1。"""
    w = 0.0
    for ch in text:
        w += 2.0 if unicodedata.east_asian_width(ch) in ("W", "F") else 1.0
    return w


def column_percent(rows: list[list[str]], lo: float = 10.0, hi: float = 52.0) -> list[int]:
    """按每列最长内容分配百分比，再夹到 [lo, hi] 并归一化到 100。"""
    ncol = max(len(r) for r in rows)
    weights = []
    for c in range(ncol):
        longest = max((_disp_width(r[c]) if c < len(r) else 1.0) for r in rows)
        weights.append(max(longest, 4.0))

    total = sum(weights) or 1.0
    pct = [w * 100.0 / total for w in weights]

    for _ in range(8):
        pct = [min(max(x, lo), hi) for x in pct]
        diff = 100.0 - sum(pct)
        if abs(diff) < 0.01:
            break
        room = [(hi - x) if diff > 0 else (x - lo) for x in pct]
        room_total = sum(room)
        if room_total <= 0:
            break
        pct = [x + diff * (r / room_total) for x, r in zip(pct, room)]

    out = [int(round(x)) for x in pct]
    out[-1] += 100 - sum(out)
    return out


# ---------------------------------------------------------------- 块级解析

def split_table_row(line: str) -> list[str]:
    body = line.strip()
    if body.startswith("|"):
        body = body[1:]
    if body.endswith("|"):
        body = body[:-1]
    return [c.strip() for c in body.split("|")]


def is_divider(line: str) -> bool:
    return bool(re.fullmatch(r"\|?[\s:|-]+\|?", line.strip())) and "-" in line


def render_table(block: list[str]) -> str:
    header = split_table_row(block[0])
    body = [split_table_row(r) for r in block[2:]]
    widths = column_percent([header] + body)

    def cells(tag: str, row: list[str]) -> str:
        parts = []
        for i, text in enumerate(row):
            w = widths[i] if i < len(widths) else 0
            parts.append(
                f'<{tag} style="width:{w}%">{inline(text)}</{tag}>'
            )
        return "".join(parts)

    out = ["<table>", "<thead>", f"<tr>{cells('th', header)}</tr>", "</thead>", "<tbody>"]
    for row in body:
        out.append(f"<tr>{cells('td', row)}</tr>")
    out += ["</tbody>", "</table>"]
    return "\n".join(out)


def render_blocks(md: str) -> list[str]:
    lines = md.splitlines()
    out: list[str] = []
    i = 0
    n = len(lines)

    while i < n:
        line = lines[i]
        stripped = line.strip()

        if not stripped:
            i += 1
            continue

        # 代码块：不用 <pre>，转换器会把 pre 压成 1×1 表格（边框里塞代码，很难看）。
        # 改成单个段落 + 硬换行，行首空格转 &nbsp; 保住缩进。
        if stripped.startswith("```"):
            i += 1
            buf = []
            while i < n and not lines[i].strip().startswith("```"):
                buf.append(lines[i])
                i += 1
            i += 1
            rendered = []
            for ln in buf:
                esc = html.escape(ln)
                esc = re.sub(r"^ +", lambda m: "&nbsp;" * len(m.group(0)), esc)
                rendered.append(esc)

            # 一个 <p> 塞整段代码会触发「段落过长」（实测 581 字被判超限）。
            # 按 360 字或 12 行分组，视觉上仍是连续的行块。
            groups: list[list[str]] = []
            cur: list[str] = []
            size = 0
            for ln in rendered:
                cur.append(ln)
                size += len(ln)
                if size > 360 or len(cur) >= 12:
                    groups.append(cur)
                    cur, size = [], 0
            if cur:
                groups.append(cur)

            for g in groups:
                out.append('<p class="code-block">' + "<br>".join(g) + "</p>")
            continue

        # 分隔线
        if re.fullmatch(r"-{3,}|\*{3,}", stripped):
            out.append("<hr>")
            i += 1
            continue

        # 标题
        m = re.match(r"^(#{1,4})\s+(.*)$", stripped)
        if m:
            level = len(m.group(1))
            out.append(f"<h{level}>{inline(m.group(2))}</h{level}>")
            i += 1
            continue

        # 表格：首行含 |，第二行是分隔行
        if "|" in stripped and i + 1 < n and is_divider(lines[i + 1]):
            block = []
            while i < n and lines[i].strip().startswith("|"):
                block.append(lines[i])
                i += 1
            out.append(render_table(block))
            continue

        # 引用：同样避开 blockquote（转换器会把它变成 1 列表格）
        if stripped.startswith(">"):
            buf = []
            while i < n and lines[i].strip().startswith(">"):
                buf.append(lines[i].strip().lstrip(">").strip())
                i += 1
            out.append('<p class="quote">' + inline(" ".join(buf)) + "</p>")
            continue

        # 列表（有序 / 无序，单层）
        if re.match(r"^([-*]|\d+\.)\s+", stripped):
            ordered = bool(re.match(r"^\d+\.\s+", stripped))
            tag = "ol" if ordered else "ul"
            items = []
            while i < n:
                cur = lines[i].strip()
                if not re.match(r"^([-*]|\d+\.)\s+", cur):
                    break
                items.append(inline(re.sub(r"^([-*]|\d+\.)\s+", "", cur)))
                i += 1
            body = "".join(f"<li>{x}</li>" for x in items)
            out.append(f"<{tag}>{body}</{tag}>")
            continue

        # 段落：吸收到空行为止
        buf = [stripped]
        i += 1
        while i < n and lines[i].strip() and not re.match(
            r"^(#{1,4}\s|```|>|\||[-*]\s|\d+\.\s)", lines[i].strip()
        ):
            buf.append(lines[i].strip())
            i += 1
        out.append("<p>" + inline("".join(buf)) + "</p>")

    return out


CSS = (Path(__file__).with_name("ops_manual.css")).read_text(encoding="utf-8")


def render(md: str, title: str = "上线交付运行手册") -> str:
    body = "\n".join(render_blocks(md))
    return (
        "<!DOCTYPE html>\n<html lang=\"zh-CN\">\n<head>\n<meta charset=\"utf-8\">\n"
        "<meta name=\"docx-page-size\" content=\"A4\">\n"
        f"<title>{_esc(title)}</title>\n<style>{CSS}</style>\n</head>\n"
        f"<body>\n{body}\n</body>\n</html>\n"
    )


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--md", required=True)
    ap.add_argument("--html", required=True)
    ap.add_argument("--title", default="共享设备借用助手 · 上线交付运行手册")
    args = ap.parse_args()

    md_path = Path(args.md)
    html_path = Path(args.html)
    html_path.parent.mkdir(parents=True, exist_ok=True)

    text = md_path.read_text(encoding="utf-8")
    html_path.write_text(render(text, args.title), encoding="utf-8")

    tables = text.count("\n|")
    print(f"已生成 {html_path}（{html_path.stat().st_size} 字节，约 {tables} 行表格）")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
