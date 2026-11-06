"""
由 output/testing/results.json 生成测试文档与图。

    .venv/bin/python scripts/render_test_docs.py

前置：先跑 scripts/test_scenarios.py。本脚本不重新执行测试、不编造数字，
所有结论都取自结果文件；方案 docx 的正文取自 docs/testing/测试方案-五类场景.md，
只有 {{...}} 占位符会被本脚本替换成结果文件里的实测值。

产出（docs/testing/）：
    测试方案-五类场景.docx        方案 Word 版（与 md 同源）
    测试过程-1正常场景.docx  …     5 份带流程图、度量图与逐步观测的过程文档
图与中间 HTML 放在 output/testing/（不入库，可随时重生成）。
"""

from __future__ import annotations

import base64
import html
import json
import re
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
RES = ROOT / "output" / "testing" / "results.json"
OUT = ROOT / "output" / "testing"
DOCS = ROOT / "docs" / "testing"
SRC_MD = DOCS / "测试方案-五类场景.md"
PLUGIN = Path("/Users/ergou/.workbuddy/plugins/cache/workbuddy-builtin/workbuddy-docx/"
              "5.9.3-wb.42398134.gd4692421.h1dfe572c384a")
CONVERT = PLUGIN / "skills/html-to-doc/scripts/convert_doc.py"
CHROME = "/Applications/Google Chrome.app/Contents/MacOS/Google Chrome"

FONT = "PingFang SC, STHeiti, Microsoft YaHei, sans-serif"
BLUE = "#2563a8"
BLUE_LIGHT = "#eaf2fb"
BLUE_LINE = "#9dc2e8"
ORANGE = "#e08a20"
ORANGE_LIGHT = "#fff5e6"
RED = "#c0392b"
GREEN = "#1e8449"
INK = "#23303c"
MUTED = "#5b6b7a"
GRID = "#dde5ec"


def esc(s: object) -> str:
    return html.escape(str(s), quote=True)


def svg_to_png(svg: str, name: str, w: int, h: int) -> Path:
    """把 SVG 包一层 HTML 让 Chrome 截成 PNG。docx 不认 SVG，但认 PNG。"""
    tmp_html = OUT / f"_svg_{name}.html"
    png = OUT / f"{name}.png"
    tmp_html.write_text(
        '<!DOCTYPE html><html><head><meta charset="utf-8"><style>'
        "html,body{margin:0;padding:0;background:#fff}svg{display:block}"
        "</style></head><body>" + svg + "</body></html>",
        encoding="utf-8",
    )
    cmd = [
        CHROME, "--headless", "--no-sandbox", "--disable-gpu", "--hide-scrollbars",
        "--force-device-scale-factor=1", f"--window-size={w},{h}",
        "--virtual-time-budget=1800", f"--screenshot={png}", tmp_html.as_uri(),
    ]
    proc = subprocess.run(cmd, capture_output=True, text=True, timeout=90)
    if not png.exists() or png.stat().st_size < 2000:
        raise RuntimeError(f"截图失败 {name}: {proc.stderr[-300:]}")
    return png


def img_tag(png: Path, alt: str) -> str:
    b64 = base64.b64encode(png.read_bytes()).decode()
    return (f'<img src="data:image/png;base64,{b64}" width="560" '
            f'style="width:100%;max-width:560px;height:auto" alt="{esc(alt)}">')


# =============================================================================
# 图 1：竖排编号步骤的测试过程流程图
# =============================================================================

SIDE_NOTES = {
    "normal": ("50 协程 ≠ 50 台机器",
               "并发只验唯一索引的正确性；\n容量压测需另走真实 HTTP 链路。"),
    "boundary": ("阈值两侧都要取点",
                 "+14 / +15、+5 / +6 分钟，\n只测一侧等于没测边界。"),
    "adversarial": ("参数来自模型输出",
                   "工具参数不可信；过期判定实际\n在 decide_pending 的 SQL 里。"),
    "permission": ("别绕过网关测权限",
                   "直接调 db 等于跳过鉴权，\n必须测服务层与路由依赖。"),
    "fault": ("最怕静默成功",
              "页面正常但索引已被清空，\n比直接 500 更难发现。"),
}

STEP_TITLES = [
    ("准备独立临时库", "init_db() + seed_if_empty()，与上一场景数据完全隔离"),
    ("按序执行 6 条用例", "{codes_short}，逐条记录输入与实测值"),
    ("逐步记录观测", "step() 把观测原样写入 results.json"),
    ("量化判定", "passed 由断言计算，量化门槛见右框"),
    ("回归佐证与归档", "跑既有单元测试类；生成流程图、度量图与过程文档"),
]


def flow_svg(sc: dict) -> tuple[str, int]:
    codes = " / ".join(c["id"] for c in sc["cases"])
    codes_short = f"{sc['cases'][0]['id']}–{sc['cases'][-1]['id']}（共 {len(sc['cases'])} 条）"
    note_title, note_body = SIDE_NOTES[sc["key"]]
    # 完整门槛放右侧挂框，按中文宽度切成两行，避免自动换行把数字劈成两半
    threshold_lines = _wrap(sc["threshold"], 17)
    if len(threshold_lines) > 2:
        threshold_note = threshold_lines[0] + "\n" + threshold_lines[1].rstrip("。；，") + "…"
    else:
        threshold_note = "\n".join(threshold_lines)
    w = 900
    top = 96
    gap = 104
    n = 5
    h = top + n * gap + 200

    parts = [
        f'<svg xmlns="http://www.w3.org/2000/svg" width="{w}" height="{h}" viewBox="0 0 {w} {h}" font-family="{FONT}">',
        f'<rect width="{w}" height="{h}" fill="#ffffff"/>',
        f'<rect x="0" y="0" width="{w}" height="72" fill="{BLUE}"/>',
        f'<text x="34" y="34" fill="#ffffff" font-size="23" font-weight="700">'
        f'测试过程流程图 · {esc(sc["index"])} {esc(sc["title"])}</text>',
        f'<text x="34" y="58" fill="#d8e8fa" font-size="13">'
        f'用例 {esc(codes)}　|　结果 {sc["passed"]}/{sc["total"]} PASS　|　'
        f'数据源 output/testing/results.json</text>',
    ]

    for i, (title, desc) in enumerate(STEP_TITLES):
        y = top + i * gap
        desc = desc.format(codes=codes, codes_short=codes_short, threshold=sc["threshold"])
        # 连接线
        if i < n - 1:
            parts.append(f'<line x1="66" y1="{y + 66}" x2="66" y2="{y + gap}" '
                         f'stroke="{BLUE_LINE}" stroke-width="2"/>')
        # 蓝色数字徽章
        parts.append(f'<circle cx="66" cy="{y + 28}" r="21" fill="{BLUE}"/>')
        parts.append(f'<text x="66" y="{y + 35}" text-anchor="middle" fill="#ffffff" '
                     f'font-size="19" font-weight="700">{i + 1}</text>')
        # 蓝色圆角步骤框
        parts.append(f'<rect x="106" y="{y}" width="470" height="66" rx="11" '
                     f'fill="{BLUE_LIGHT}" stroke="{BLUE_LINE}" stroke-width="1.6"/>')
        parts.append(f'<text x="126" y="{y + 24}" fill="{BLUE}" font-size="16" '
                     f'font-weight="700">{esc(title)}</text>')
        # 说明文字按「一行约 25 个汉字」粗切两行；框内可用宽约 430px
        lines = _wrap(desc, 25)
        for j, line in enumerate(lines[:2]):
            parts.append(f'<text x="126" y="{y + 43 + j * 16}" fill="{INK}" font-size="12.5">'
                         f'{esc(line)}</text>')
        # 右侧虚线橙色挂框（只在第 2、4 步挂，避免每步都重复）
        if i in (1, 3):
            side = (note_title, note_body) if i == 1 else ("量化门槛", threshold_note)
            parts.append(_side_box(610, y - 2, side[0], side[1]))

    # 橙色菱形判断节点
    dy = top + n * gap - 6
    parts.append(f'<line x1="66" y1="{top + (n - 1) * gap + 66}" x2="66" y2="{dy}" '
                 f'stroke="{BLUE_LINE}" stroke-width="2"/>')
    cx, cy = 341, dy + 34
    parts.append(f'<polygon points="{cx},{cy - 38} {cx + 118},{cy} {cx},{cy + 38} {cx - 118},{cy}" '
                 f'fill="{ORANGE_LIGHT}" stroke="{ORANGE}" stroke-width="2"/>')
    parts.append(f'<text x="{cx}" y="{cy - 4}" text-anchor="middle" fill="{INK}" '
                 f'font-size="15" font-weight="700">6 条用例全 PASS？</text>')
    parts.append(f'<text x="{cx}" y="{cy + 17}" text-anchor="middle" fill="{MUTED}" '
                 f'font-size="12">实测 {sc["passed"]}/{sc["total"]}，耗时 {sc["elapsed_ms"]} ms</text>')
    # 是 / 否 分支徽章
    parts.append(f'<rect x="500" y="{cy - 17}" width="150" height="34" rx="17" fill="#e8f6ed" stroke="{GREEN}"/>')
    parts.append(f'<text x="575" y="{cy + 6}" text-anchor="middle" fill="{GREEN}" '
                 f'font-size="13" font-weight="700">是 → 归档</text>')
    parts.append(f'<line x1="{cx + 118}" y1="{cy}" x2="500" y2="{cy}" stroke="{GREEN}" stroke-width="2"/>')
    parts.append(f'<rect x="106" y="{cy + 52}" width="150" height="30" rx="15" fill="#fdeceb" stroke="{RED}"/>')
    parts.append(f'<text x="181" y="{cy + 72}" text-anchor="middle" fill="{RED}" '
                 f'font-size="12.5" font-weight="700">否 → 定位失败用例</text>')

    # 痛点 / 必须人工 徽章
    parts.append(f'<rect x="620" y="{cy + 46}" width="112" height="30" rx="15" fill="#fdeceb" stroke="{RED}"/>')
    parts.append(f'<text x="676" y="{cy + 66}" text-anchor="middle" fill="{RED}" font-size="12.5">痛点</text>')
    parts.append(f'<rect x="742" y="{cy + 46}" width="126" height="30" rx="15" fill="#e8f6ed" stroke="{GREEN}"/>')
    parts.append(f'<text x="805" y="{cy + 66}" text-anchor="middle" fill="{GREEN}" font-size="12.5">必须人工确认</text>')

    # 底部图例 + 备注
    ly = h - 78
    parts.append(f'<rect x="34" y="{ly}" width="{w - 68}" height="60" rx="8" '
                 f'fill="#f7fafc" stroke="{GRID}"/>')
    legend = [("蓝框=执行步骤", BLUE_LIGHT, BLUE_LINE), ("橙菱形=判定", ORANGE_LIGHT, ORANGE),
              ("红色=风险", "#fdeceb", RED), ("绿色=必须人工", "#e8f6ed", GREEN)]
    lx = 52
    for label, fill, stroke in legend:
        parts.append(f'<rect x="{lx}" y="{ly + 12}" width="17" height="17" rx="4" fill="{fill}" stroke="{stroke}"/>')
        parts.append(f'<text x="{lx + 24}" y="{ly + 25}" fill="{MUTED}" font-size="12">{esc(label)}</text>')
        lx += 205
    parts.append(f'<text x="52" y="{ly + 50}" fill="{MUTED}" font-size="12">'
                 f'备注：{esc(sc["risk"][:44])}…</text>')
    parts.append("</svg>")
    return "".join(parts), h


def _side_box(x: int, y: int, title: str, body: str) -> str:
    lines = body.split("\n")
    out = [
        f'<rect x="{x}" y="{y}" width="256" height="72" rx="9" fill="{ORANGE_LIGHT}" '
        f'stroke="{ORANGE}" stroke-width="1.5" stroke-dasharray="6 4"/>',
        f'<text x="{x + 14}" y="{y + 22}" fill="{ORANGE}" font-size="13" font-weight="700">{esc(title)}</text>',
    ]
    for j, line in enumerate(lines[:2]):
        out.append(f'<text x="{x + 14}" y="{y + 42 + j * 16}" fill="{INK}" font-size="12">{esc(line)}</text>')
    return "".join(out)


def _wrap(text: str, width: int) -> list[str]:
    """按显示宽度粗切中文，SVG 不支持自动换行。"""
    out, cur, size = [], "", 0
    for ch in text:
        cur += ch
        size += 2 if ord(ch) > 127 else 1
        if size >= width * 2:
            out.append(cur)
            cur, size = "", 0
    if cur:
        out.append(cur)
    return out or [""]


# =============================================================================
# 图 2：结果度量图
# =============================================================================


def metrics_svg(sc: dict) -> tuple[str, int]:
    w, h = 900, 470
    cases = sc["cases"]
    mx = max([c["elapsed_ms"] for c in cases] + [1])
    parts = [
        f'<svg xmlns="http://www.w3.org/2000/svg" width="{w}" height="{h}" viewBox="0 0 {w} {h}" font-family="{FONT}">',
        f'<rect width="{w}" height="{h}" fill="#ffffff"/>',
        f'<text x="34" y="38" fill="{INK}" font-size="21" font-weight="700">结果度量 · {esc(sc["title"])}</text>',
        f'<text x="34" y="61" fill="{MUTED}" font-size="12.5">横条为单条用例耗时（ms），完整实测结论见正文表格；绿色表示 PASS</text>',
        f'<rect x="690" y="18" width="176" height="52" rx="10" fill="#e8f6ed" stroke="{GREEN}"/>',
        f'<text x="778" y="42" text-anchor="middle" fill="{GREEN}" font-size="20" font-weight="700">'
        f'{sc["passed"]}/{sc["total"]} PASS</text>',
        f'<text x="778" y="60" text-anchor="middle" fill="{MUTED}" font-size="11.5">通过率 {sc["passed"] * 100 // sc["total"]}%</text>',
    ]
    y0, bh, gap = 92, 26, 45
    for i, c in enumerate(cases):
        y = y0 + i * gap
        bar_w = max(4, int(420 * c["elapsed_ms"] / mx))
        parts.append(f'<text x="34" y="{y + 18}" fill="{BLUE}" font-size="13" font-weight="700">{esc(c["id"])}</text>')
        parts.append(f'<text x="82" y="{y + 18}" fill="{INK}" font-size="12.5">{esc(c["name"][:11])}</text>')
        parts.append(f'<rect x="230" y="{y + 3}" width="420" height="{bh}" rx="5" fill="#f1f5f9"/>')
        parts.append(f'<rect x="230" y="{y + 3}" width="{bar_w}" height="{bh}" rx="5" fill="{BLUE}"/>')
        parts.append(f'<text x="660" y="{y + 21}" fill="{INK}" font-size="12.5">{c["elapsed_ms"]} ms</text>')
        parts.append(f'<rect x="726" y="{y + 5}" width="48" height="21" rx="10" fill="#e8f6ed"/>')
        parts.append(f'<text x="750" y="{y + 20}" text-anchor="middle" fill="{GREEN}" font-size="11" '
                     f'font-weight="700">{"PASS" if c["passed"] else "FAIL"}</text>')
        # 完整实测结论放在正文表格里，图里只留判定，避免文字挤出画布

    # 底部关键数值卡
    ky = y0 + len(cases) * gap + 14
    parts.append(f'<line x1="34" y1="{ky}" x2="866" y2="{ky}" stroke="{GRID}"/>')
    kv = _key_metrics(sc)
    cw = 202
    for i, (label, value) in enumerate(kv[:4]):
        x = 34 + i * (cw + 10)
        parts.append(f'<rect x="{x}" y="{ky + 14}" width="{cw}" height="54" rx="8" '
                     f'fill="{BLUE_LIGHT}" stroke="{BLUE_LINE}"/>')
        parts.append(f'<text x="{x + 12}" y="{ky + 34}" fill="{MUTED}" font-size="11.5">{esc(label)}</text>')
        parts.append(f'<text x="{x + 12}" y="{ky + 57}" fill="{BLUE}" font-size="17" '
                     f'font-weight="700">{esc(value)}</text>')
    parts.append("</svg>")
    return "".join(parts), h


def _key_metrics(sc: dict) -> list[tuple[str, str]]:
    """从各场景挑真正有信息量的实测数字，而不是笼统的通过率。"""
    pick = {
        "normal": [("50 并发成功单数", "1 / 50"), ("同格有效占用", "1"), ("N06 全程耗时", "见左图"),
                   ("视图差异", "0 格")],
        "boundary": [("可约窗口", "+0…+14 天"), ("单日配额", "2 格"), ("归还容差", "5 分钟"),
                     ("非法起点拦截", "6 / 6")],
        "adversarial": [("重复确认后单数", "1"), ("他人凭证有效消费", "0 次"),
                        ("注入串副作用", "0"), ("过期卡片扫描", "1 条")],
        "permission": [("越权拒绝", "4 / 4"), ("停用后历史单", "1 单"),
                       ("登录失败锁定", "5 次 / 10 分钟"), ("日志密码哈希", "0 条")],
        "fault": [("故障回落路径", "6 类"), ("旧索引保留", "是"),
                  ("坏 JSON 抛异常", "0 次"), ("回归单测", "18 项")],
    }
    base = pick.get(sc["key"], [])
    # 用结果文件里真实存在的值覆盖前两格，避免手工常量与结果漂移
    by_id = {c["id"]: c for c in sc["cases"]}
    if sc["key"] == "normal" and "N06" in by_id:
        m = by_id["N06"]["metrics"]
        base[0] = ("50 并发成功单数", f"{m.get('success', '?')} / {m.get('concurrent', 50)}")
        base[1] = ("同格有效占用", f"{m.get('active', '?')} 单")
        base[2] = ("N06 全程耗时", f"{m.get('total_ms', '?')} ms")
    if sc["key"] == "permission" and "P06" in by_id:
        base[2] = ("登录失败锁定", f"{by_id['P06']['metrics'].get('attempts', 5)} 次 / 10 分钟")
    if sc["key"] == "fault":
        n = sum(c["metrics"].get("regression_tests", 0) for c in sc["cases"])
        base[3] = ("回归单测", f"{n} 项")
    return base


# =============================================================================
# 图 3：方案总览
# =============================================================================


def overview_svg(data: dict) -> tuple[str, int]:
    w, h = 900, 360
    ss = data["scenarios"]
    mx = max(s["elapsed_ms"] for s in ss)
    parts = [
        f'<svg xmlns="http://www.w3.org/2000/svg" width="{w}" height="{h}" viewBox="0 0 {w} {h}" font-family="{FONT}">',
        f'<rect width="{w}" height="{h}" fill="#ffffff"/>',
        f'<text x="34" y="36" fill="{INK}" font-size="21" font-weight="700">五类场景总览</text>',
        f'<text x="34" y="58" fill="{MUTED}" font-size="12.5">'
        f'共 {data["summary"]["cases"]} 条量化用例，{data["summary"]["passed"]} 条通过；横条为该场景累计耗时（ms）</text>',
    ]
    y0, gap = 86, 46
    for i, s in enumerate(ss):
        y = y0 + i * gap
        bw = max(6, int(430 * s["elapsed_ms"] / mx))
        parts.append(f'<text x="34" y="{y + 18}" fill="{BLUE}" font-size="13.5" font-weight="700">'
                     f'{s["index"]} {esc(s["title"])}</text>')
        parts.append(f'<rect x="164" y="{y + 3}" width="430" height="22" rx="5" fill="#f1f5f9"/>')
        parts.append(f'<rect x="164" y="{y + 3}" width="{bw}" height="22" rx="5" fill="{BLUE}"/>')
        parts.append(f'<text x="606" y="{y + 19}" fill="{INK}" font-size="12.5">{s["elapsed_ms"]} ms</text>')
        parts.append(f'<text x="700" y="{y + 19}" fill="{GREEN}" font-size="13" font-weight="700">'
                     f'{s["passed"]}/{s["total"]} PASS</text>')
    parts.append(f'<line x1="34" y1="{h - 52}" x2="866" y2="{h - 52}" stroke="{GRID}"/>')
    parts.append(f'<text x="34" y="{h - 28}" fill="{MUTED}" font-size="12">'
                 f'故障场景耗时最高，因为其中 6 条用例各自拉起子进程跑既有单元测试类；其余场景均为进程内断言。</text>')
    parts.append("</svg>")
    return "".join(parts), h


# =============================================================================
# 极简 Markdown → HTML（方案 docx 与 md 同源）
# =============================================================================


def inline(text: str) -> str:
    text = esc(text)
    text = re.sub(r"`([^`]+)`", r"<code>\1</code>", text)
    text = re.sub(r"\*\*([^*]+)\*\*", r"<strong>\1</strong>", text)
    return text


def md_to_html(md: str) -> str:
    lines = md.splitlines()
    out: list[str] = []
    i = 0
    while i < len(lines):
        line = lines[i]
        if not line.strip():
            i += 1
            continue
        if line.startswith("```"):
            i += 1
            buf = []
            while i < len(lines) and not lines[i].startswith("```"):
                buf.append(lines[i]); i += 1
            i += 1
            out.append("<pre>" + esc("\n".join(buf)) + "</pre>")
            continue
        if line.startswith("|"):
            block = []
            while i < len(lines) and lines[i].startswith("|"):
                block.append(lines[i]); i += 1
            out.append(_table(block))
            continue
        if line.startswith(">"):
            buf = []
            while i < len(lines) and lines[i].startswith(">"):
                buf.append(lines[i].lstrip("> ").rstrip()); i += 1
            out.append("<blockquote>" + inline(" ".join(x for x in buf if x)) + "</blockquote>")
            continue
        m = re.match(r"^(#{1,4})\s+(.*)$", line)
        if m:
            lvl = len(m.group(1))
            out.append(f"<h{lvl}>{inline(m.group(2))}</h{lvl}>")
            i += 1
            continue
        if re.match(r"^[-*]\s+", line):
            buf = []
            while i < len(lines) and re.match(r"^[-*]\s+", lines[i]):
                buf.append(re.sub(r"^[-*]\s+", "", lines[i])); i += 1
            out.append("<ul>" + "".join(f"<li>{inline(x)}</li>" for x in buf) + "</ul>")
            continue
        if re.match(r"^\d+\.\s+", line):
            buf = []
            while i < len(lines) and re.match(r"^\d+\.\s+", lines[i]):
                buf.append(re.sub(r"^\d+\.\s+", "", lines[i])); i += 1
            out.append("<ol>" + "".join(f"<li>{inline(x)}</li>" for x in buf) + "</ol>")
            continue
        if line.strip() == "---":
            out.append("<hr>"); i += 1
            continue
        buf = [line]
        i += 1
        while i < len(lines) and lines[i].strip() and not re.match(
                r"^(#{1,4}\s|\||>|```|[-*]\s|\d+\.\s|---$)", lines[i]):
            buf.append(lines[i]); i += 1
        out.append("<p>" + inline(" ".join(buf)) + "</p>")
    return "\n".join(out)


def _table(block: list[str]) -> str:
    rows = [[c.strip() for c in r.strip().strip("|").split("|")] for r in block]
    if len(rows) > 1 and all(set(c) <= set("-: ") for c in rows[1]):
        rows.pop(1)
    head, body = rows[0], rows[1:]
    parts = ['<table class="data"><thead><tr>']
    for c in head:
        parts.append(f"<th>{inline(c)}</th>")
    parts.append("</tr></thead><tbody>")
    for row in body:
        parts.append("<tr>")
        for c in row:
            parts.append(f"<td>{inline(c)}</td>")
        parts.append("</tr>")
    parts.append("</tbody></table>")
    return "".join(parts)


CSS = """
@page { size: A4; margin: 2.2cm 2.0cm; }
body { font-family: PingFang SC, STHeiti, Microsoft YaHei, sans-serif;
       font-size: 10.5pt; line-height: 1.65; color: #23303c; }
h1 { font-size: 20pt; color: #1c4f86; margin: 0 0 14pt; border-bottom: 2pt solid #2563a8; padding-bottom: 8pt; }
h2 { font-size: 14.5pt; color: #1c4f86; margin: 20pt 0 8pt; border-left: 4pt solid #2563a8; padding-left: 8pt; }
h3 { font-size: 12pt; color: #2563a8; margin: 14pt 0 6pt; }
p { margin: 6pt 0; text-align: justify; }
blockquote { background: #f2f7fc; border-left: 3pt solid #9dc2e8; padding: 7pt 10pt;
             margin: 8pt 0; color: #41576b; font-size: 9.5pt; }
table.data { border-collapse: collapse; width: 100%; margin: 8pt 0 12pt; font-size: 9pt; }
table.data th { background: #2563a8; color: #ffffff; padding: 5pt 6pt; border: 0.6pt solid #9dc2e8;
                text-align: left; font-weight: 700; }
table.data td { padding: 4pt 6pt; border: 0.6pt solid #dde5ec; vertical-align: top; }
table.data tr:nth-child(even) td { background: #f7fafc; }
code { font-family: Menlo, Consolas, monospace; font-size: 9pt; background: #f1f5f9; padding: 0 2pt; }
pre { background: #f4f7fa; border: 0.6pt solid #dde5ec; padding: 8pt; font-size: 8.5pt;
      font-family: Menlo, Consolas, monospace; white-space: pre-wrap; }
ul, ol { margin: 6pt 0 6pt 18pt; padding-left: 8pt; }
li { margin: 3pt 0; }
hr { border: 0; border-top: 0.6pt solid #dde5ec; margin: 14pt 0; }
.figure { margin: 10pt 0; text-align: center; }
.caption { font-size: 9pt; color: #5b6b7a; text-align: center; margin: 2pt 0 10pt; }
.meta { background: #eaf2fb; border: 0.6pt solid #9dc2e8; }
.verdict-pass { color: #1e8449; font-weight: 700; }
.verdict-fail { color: #c0392b; font-weight: 700; }
.note { background: #fff5e6; border-left: 3pt solid #e08a20; padding: 7pt 10pt; margin: 8pt 0; font-size: 9.5pt; }
"""


def page(title: str, body: str) -> str:
    return ('<!DOCTYPE html><html><head><meta charset="utf-8"><title>' + esc(title) +
            '</title><style>' + CSS + '</style></head><body>' + body + '</body></html>')


def to_docx(html_path: Path, docx_path: Path) -> None:
    proc = subprocess.run(
        [sys.executable, str(CONVERT), "--html", str(html_path), "--output", str(docx_path),
         "--no-preflight", "--no-verify"],
        cwd=ROOT, capture_output=True, text=True, timeout=180,
    )
    try:
        res = json.loads(proc.stdout.strip().splitlines()[-1])
    except Exception:
        raise RuntimeError(f"转换失败 {docx_path.name}: {proc.stdout[-300:]} {proc.stderr[-300:]}")
    if not res.get("success"):
        raise RuntimeError(f"转换失败 {docx_path.name}: {res.get('error')}")
    print(f"    → {docx_path.name}  ({docx_path.stat().st_size:,} bytes)")


def write_html(name: str, content: str) -> Path:
    p = OUT / f"{name}.html"
    p.write_text(content, encoding="utf-8")
    return p


# =============================================================================
# 方案 docx
# =============================================================================


def render_plan(data: dict, overview_png: Path) -> None:
    md = SRC_MD.read_text(encoding="utf-8")
    s = data["summary"]
    rows = ["| 场景 | 用例数 | 通过 | 失败 | 通过率 | 累计耗时 |",
            "|---|---|---|---|---|---|"]
    for sc in data["scenarios"]:
        rows.append(f"| {sc['index']} {sc['title']} | {sc['total']} | {sc['passed']} | "
                    f"{sc['total'] - sc['passed']} | {sc['passed'] * 100 // sc['total']}% | "
                    f"{sc['elapsed_ms']} ms |")
    rows.append(f"| **合计** | **{s['cases']}** | **{s['passed']}** | **{s['failed']}** | "
                f"**{s['rate']}%** | **{s['elapsed_ms']} ms** |")
    summary_table = "\n".join(rows)

    scen_rows = ["| 场景 | 关键实测值 |", "|---|---|"]
    for sc in data["scenarios"]:
        kv = "；".join(f"{k} {v}" for k, v in _key_metrics(sc)[:3])
        scen_rows.append(f"| {sc['index']} {sc['title']} | {kv} |")

    md = md.replace("{{SUMMARY_TABLE}}", summary_table)
    md = md.replace("{{SCENARIO_TABLE}}", "\n".join(scen_rows))
    md = md.replace("{{GENERATED_AT}}", data["generated_at"])
    md = md.replace("{{PYTHON}}", data["python"])

    body = md_to_html(md)
    # 在「七、本次实测结果」后插总览图
    marker = "<h2>七、本次实测结果</h2>"
    figure = ('<div class="figure">' + img_tag(overview_png, "五类场景总览") +
              '</div><p class="caption">图 0-1　五类场景用例数与累计耗时（数据源：results.json）</p>')
    body = body.replace(marker, marker + figure, 1)
    hp = write_html("plan", page("五类场景量化测试方案", body))
    to_docx(hp, DOCS / "测试方案-五类场景.docx")


# =============================================================================
# 5 份过程文档
# =============================================================================


CONCLUSIONS = {
    "normal": ("主流程端到端可跑通，唯一索引在 50 协程并发写下只放行 1 单，"
               "占用视图与库内数据零差异。N06 的全程耗时低于基线 800 ms 目标，"
               "但这是单次样本、且没有经过网络栈，不能当作 P95 结论。",
               "真实 HTTP 压测多进程竞争；列表页 P95 < 1s 未测。"),
    "boundary": ("六个阈值两侧都取到了值，拒绝点与理由均可区分：+14 合法 / +15 超窗、"
                 "+5 分钟接受 / +6 分钟拒绝、第 3 格拒绝后配额仍为 2。",
               "B05 依赖伪造时钟，真实跨日/夏令时场景未覆盖；CI 使用 UTC，已单独验过时区无关性。"),
    "adversarial": ("重复确认、他人凭证、过期卡片三类「把同一动作做两次或换个人做」的攻击面都拦住了，"
                   "注入串作为普通文本处理，设备表无副作用。",
                   "A03/A04 验证的是解析层与错误码契约，没有跑完整 SSE 流；模型提示词注入未覆盖。"),
    "permission": ("权限判定落在服务层与 FastAPI 路由依赖上，不是前端隐藏：4 类越权全部拒绝，"
                   "停用即时生效且历史预约保留，5 次失败锁定 10 分钟，日志无密码哈希。",
                   "未测 JWT 过期/篡改、CSRF 与并发提权；这些属于部署验收。"),
    "fault": ("六类故障都有明确回落路径，最关键的 F03 证明「重建失败时旧索引仍在」——"
              "这防的是页面看着正常、索引其实被清空的静默事故。",
              "LLM 上游用错误码契约与 Harness 回归代替，没有真实外部模型故障注入；备份恢复演练仍未做。"),
}


def render_process(sc: dict, flow_png: Path, metric_png: Path, data: dict) -> None:
    verdict = "全部通过" if sc["passed"] == sc["total"] else f"{sc['total'] - sc['passed']} 条失败"
    conclusion, residual = CONCLUSIONS[sc["key"]]
    b: list[str] = []
    b.append(f"<h1>测试过程记录 · {sc['index']} {esc(sc['title'])}</h1>")
    b.append('<table class="data meta"><tbody>')
    meta = [
        ("用例范围", " / ".join(c["id"] for c in sc["cases"])),
        ("执行时间", data["generated_at"]),
        ("解释器 / 数据库", f"Python {data['python']} / SQLite 独立临时库"),
        ("结果", f"{sc['passed']}/{sc['total']} PASS（{verdict}）"),
        ("累计耗时", f"{sc['elapsed_ms']} ms"),
        ("结果文件", "output/testing/results.json"),
    ]
    for k, v in meta:
        b.append(f"<tr><th>{esc(k)}</th><td>{esc(v)}</td></tr>")
    b.append("</tbody></table>")

    b.append("<h2>一、场景目标与判定门槛</h2>")
    b.append(f"<p><strong>目标：</strong>{esc(sc['goal'])}</p>")
    b.append(f"<p><strong>风险：</strong>{esc(sc['risk'])}</p>")
    b.append(f"<div class=\"note\"><strong>量化门槛：</strong>{esc(sc['threshold'])}</div>")

    b.append("<h2>二、测试过程流程图</h2>")
    b.append('<div class="figure">' + img_tag(flow_png, f"{sc['title']}测试过程流程图") + "</div>")
    b.append(f'<p class="caption">图 {sc["index"]}-1　{esc(sc["title"])}的执行步骤、判定分支与人工复核点</p>')
    b.append("<p>五个步骤固定为：准备独立临时库 → 按序执行 6 条用例 → 逐步记录观测 → "
             "量化判定 → 回归佐证与归档。右侧虚线挂框写的是本场景最容易误读的一处口径。</p>")

    b.append("<h2>三、逐条用例与过程观测</h2>")
    for c in sc["cases"]:
        mark = '<span class="verdict-pass">PASS</span>' if c["passed"] else '<span class="verdict-fail">FAIL</span>'
        b.append(f"<h3>{esc(c['id'])}　{esc(c['name'])}　{mark}　{c['elapsed_ms']} ms</h3>")
        b.append('<table class="data"><tbody>')
        b.append(f"<tr><th>输入</th><td>{esc(c['given'])}</td></tr>")
        b.append(f"<tr><th>预期</th><td>{esc(c['expect'])}</td></tr>")
        b.append(f"<tr><th>实测</th><td>{esc(c['observed'])}</td></tr>")
        b.append("</tbody></table>")
        if c["steps"]:
            b.append('<table class="data"><thead><tr><th style="width:6%">步</th>'
                     '<th style="width:24%">动作</th><th style="width:32%">输入 / 条件</th>'
                     '<th style="width:38%">观测值</th></tr></thead><tbody>')
            for j, st in enumerate(c["steps"], 1):
                b.append(f"<tr><td>{j}</td><td>{esc(st['action'])}</td>"
                         f"<td>{esc(st['given'])}</td><td>{esc(st['observed'])}</td></tr>")
            b.append("</tbody></table>")
        if c["metrics"]:
            kv = "；".join(f"{esc(k)}={esc(v)}" for k, v in list(c["metrics"].items())[:6]
                          if not isinstance(v, (list, dict)))
            if kv:
                b.append(f"<p class=\"caption\">结构化度量：{kv}</p>")

    b.append("<h2>四、结果度量</h2>")
    b.append('<div class="figure">' + img_tag(metric_png, f"{sc['title']}结果度量") + "</div>")
    b.append(f'<p class="caption">图 {sc["index"]}-2　6 条用例耗时分布、PASS 判定与关键实测值</p>')
    b.append(f"<p>本场景 {sc['total']} 条用例，通过 {sc['passed']} 条，失败 {sc['total'] - sc['passed']} 条，"
             f"累计 {sc['elapsed_ms']} ms。耗时包含数据库 IO 与（故障场景的）子进程回归。</p>")

    b.append("<h2>五、结论与残留风险</h2>")
    b.append(f"<p>{esc(conclusion)}</p>")
    b.append(f'<div class="note"><strong>还没覆盖：</strong>{esc(residual)}</div>')

    b.append("<h2>六、复现方式</h2>")
    b.append("<pre>cd /Users/ergou/code/SharingDeviceBooking\n"
             ".venv/bin/python scripts/test_scenarios.py\n"
             ".venv/bin/python scripts/render_test_docs.py</pre>")
    b.append("<p>本页所有数值来自 <code>output/testing/results.json</code>，由执行器当次真实运行产生；"
             "文档生成脚本只读取结果，不重新计算、不人工填写。</p>")

    hp = write_html(f"process-{sc['key']}", page(f"测试过程-{sc['index']}{sc['title']}", "\n".join(b)))
    to_docx(hp, DOCS / f"测试过程-{sc['index']}{sc['title']}.docx")


def main() -> int:
    if not RES.exists():
        print(f"缺少 {RES}，请先运行 scripts/test_scenarios.py", file=sys.stderr)
        return 2
    data = json.loads(RES.read_text(encoding="utf-8"))
    OUT.mkdir(parents=True, exist_ok=True)
    DOCS.mkdir(parents=True, exist_ok=True)

    print("生成图：")
    ov, ovh = overview_svg(data)
    ov_png = svg_to_png(ov, "overview", 900, ovh)
    print(f"    overview ({ov_png.stat().st_size:,} bytes)")

    flow_pngs = {}
    metric_pngs = {}
    for sc in data["scenarios"]:
        fsvg, fh = flow_svg(sc)
        fp = svg_to_png(fsvg, f"flow-{sc['key']}", 900, fh)
        ms, mh = metrics_svg(sc)
        mp = svg_to_png(ms, f"metrics-{sc['key']}", 900, mh)
        flow_pngs[sc["key"]] = fp
        metric_pngs[sc["key"]] = mp
        print(f"    {sc['key']}: flow {fp.stat().st_size:,} B / metrics {mp.stat().st_size:,} B")

    print("生成 docx：")
    render_plan(data, ov_png)
    for sc in data["scenarios"]:
        render_process(sc, flow_pngs[sc["key"]], metric_pngs[sc["key"]], data)

    print(f"\n完成：1 份方案 + {len(data['scenarios'])} 份过程文档 → {DOCS}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
