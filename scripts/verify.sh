#!/usr/bin/env bash
# 自检入口。真正的检查逻辑在 check_deps.py / verify.py —— shell 只负责找解释器、按顺序调。
#
# 用 .venv 里的 Python 而不是系统 python3：依赖（fastapi / faiss）只装在 venv 里，
# 系统解释器跑会直接 ImportError，而那个报错看起来很像"代码坏了"。
#
# 顺序是先依赖后业务：依赖没装对的时候，verify.py 会在 import 阶段崩，
# 堆栈几十行，看不出根因是"faiss 与 numpy 版本没配对"。
# check_deps.py 会先把这个话说清楚，再让 verify.py 跑。
set -euo pipefail

cd "$(dirname "$0")/.."

if [ -x .venv/bin/python ]; then
    PY=.venv/bin/python
elif command -v python3 >/dev/null 2>&1; then
    PY=python3
else
    echo "找不到 python，请先创建虚拟环境：python3 -m venv .venv" >&2
    exit 2
fi

"$PY" scripts/check_deps.py
echo
exec "$PY" scripts/verify.py

