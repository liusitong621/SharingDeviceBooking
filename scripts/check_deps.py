"""
装完依赖后的冒烟自检：解释器版本够不够、每个包能不能真的 import、faiss 与 numpy 的 ABI 对不对。

    .venv/bin/python scripts/check_deps.py

为什么"pip install 成功"不等于"依赖可用"
-----------------------------------------
requirements.txt 把 numpy 和 faiss-cpu 的版本死死锁住，防的是 C ABI 漂移：
faiss-cpu 是编译期对着某个 numpy 的 C 接口生成的，numpy 一旦被单独升级，
`import faiss` 会抛 "undefined symbol: ..."，报错点在 faiss 内部，
从堆栈看不出是版本漂移，很容易被误判成"装的时候网络坏了"。
pip 装得干干净净，问题要到第一次 import 才炸——所以这里主动 import 一次。

另一个坑是解释器版本：numpy 2.5.x 的 Requires-Python 是 >=3.12。
在 3.11 上 pip 不会提示"版本太低"，而是吐一屏 "Could not find a version
that satisfies the requirement numpy==2.5.4 (from versions: ...)"，
把版本不兼容伪装成"找不到包"。第一版 CI 就是红在这里，所以这一步先查版本。

退出码：全部通过 0，任一项失败 1。
"""

from __future__ import annotations

import sys

# requirements.txt 声明的下限。两边必须一致：改这里就要改 requirements.txt 的注释，
# 反过来也一样。用常量而不是在代码里散落字符串，是为了只有一处可改。
MIN_PYTHON = (3, 12)

# 导入名 → 包名（pip 名）。两者不同的那几个必须显式写出来，
# 否则报错信息里会出现"rank_bm25 没装"，而用户可能压根不知道它来自 rank-bm25。
IMPORTS: list[tuple[str, str]] = [
    ("fastapi", "fastapi"),
    ("starlette", "starlette"),
    ("uvicorn", "uvicorn"),
    ("jinja2", "jinja2"),
    ("multipart", "python-multipart"),
    ("pydantic", "pydantic"),
    ("jwt", "pyjwt"),
    ("numpy", "numpy"),
    ("faiss", "faiss-cpu"),
    ("rank_bm25", "rank-bm25"),
    ("jieba", "jieba"),
    ("httpx", "httpx"),
]

# 可选：缺了只是检索召回变差，脚本会自己降级，不该让 CI 红。
# 这里不放进 IMPORTS，但装了就打出版本，方便看日志时对账。
OPTIONAL_IMPORTS: list[tuple[str, str]] = []


def check_python() -> list[str]:
    major, minor = sys.version_info[:2]
    if (major, minor) < MIN_PYTHON:
        return [
            f"解释器版本过低：当前 {major}.{minor}，要求 >={MIN_PYTHON[0]}.{MIN_PYTHON[1]}。"
            f"  numpy 2.5.x 的 Requires-Python 就是 >={MIN_PYTHON[0]}.{MIN_PYTHON[1]}，"
            "低于它会报成 'Could not find a version that satisfies numpy==2.5.4'，"
            "看着像包不存在，实际是版本不够。"
        ]
    return []


def check_imports() -> list[str]:
    failures: list[str] = []
    lines: list[str] = []

    for mod_name, pkg_name in IMPORTS:
        try:
            mod = __import__(mod_name)
        except Exception as exc:  # noqa: BLE001 - import 阶段的异常类型不可枚举
            hint = ""
            detail = f"{type(exc).__name__}: {exc}"
            if "undefined symbol" in detail or "cannot import name" in detail:
                hint = (
                    "  ← 这是典型的 C 扩展 ABI 不匹配，不是没装。"
                    " 处理办法：按 requirements.txt 把 numpy + faiss-cpu 整组重装，"
                    " 不要只升其中一个。"
                )
            failures.append(f"import {mod_name}（pip 包 {pkg_name}）失败：{detail}{hint}")
            continue

        version = getattr(mod, "__version__", None) or getattr(mod, "VERSION", None)
        shown = f" {version}" if isinstance(version, str | tuple) else ""
        lines.append(f"  {pkg_name:<18}{mod_name:<10}{shown}")

    for mod_name, pkg_name in OPTIONAL_IMPORTS:
        try:
            __import__(mod_name)
            lines.append(f"  {pkg_name:<18}{mod_name:<10}（可选）")
        except Exception:  # noqa: BLE001
            lines.append(f"  {pkg_name:<18}{mod_name:<10}（可选，未装）")

    if lines:
        print("已导入：")
        print("\n".join(lines))

    return failures


def check_faiss_numpy_abi() -> list[str]:
    """真的建一个索引、写进两条向量、查一次。

    import 成功只说明符号解析过了动态链接这一关；真正的 ABI 问题是
    在把 numpy array 传进 faiss 的 C++ 接口时才会暴露（比如 dtype/stride 假设不一致）。
    所以这里跑一次最小的 add + search，代价是几毫秒。
    """
    try:
        import faiss
        import numpy as np
    except Exception:  # noqa: BLE001 - 前面已经报过，这里不重复报
        return []

    try:
        index = faiss.IndexFlatL2(4)
        # float32 是 faiss 唯一接受的类型；用 float64 会直接抛异常，
        # 这本身也是一条值得留在检查里的约定。
        vecs = np.array([[0.0, 0.0, 0.0, 0.0], [1.0, 1.0, 1.0, 1.0]], dtype="float32")
        index.add(vecs)
        distances, ids = index.search(np.array([[0.1, 0.1, 0.1, 0.1]], dtype="float32"), 1)
    except Exception as exc:  # noqa: BLE001
        return [f"faiss 冒烟检索失败：{type(exc).__name__}: {exc}（多为 numpy/faiss 版本不配对）"]

    if int(ids[0][0]) != 0:
        return [f"faiss 检索结果异常：期望命中 0 号向量，实际 {int(ids[0][0])}"]

    print(f"faiss 冒烟检索通过（最近邻距离 {float(distances[0][0]):.4f}，numpy {np.__version__}）")
    return []


def main() -> int:
    print(f"解释器：{sys.executable}")
    print(f"版本：{sys.version.split()[0]}\n")

    problems = check_python()
    if problems:
        print("\n".join(f"[FAIL] {p}" for p in problems))
        return 1

    problems = check_imports()
    problems += check_faiss_numpy_abi()

    print()
    if problems:
        print("\n".join(f"[FAIL] {p}" for p in problems))
        print(f"\n共 {len(problems)} 项失败。")
        return 1

    print("[OK] 依赖自检通过。")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
