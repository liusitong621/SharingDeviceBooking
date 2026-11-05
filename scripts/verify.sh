#!/usr/bin/env bash
# 自检入口。真正的检查逻辑在 verify.py —— shell 只负责找到虚拟环境的解释器。
#
# 用 .venv 里的 Python 而不是系统 python3：依赖（fastapi / faiss）只装在 venv 里，
# 系统解释器跑会直接 ImportError，而那个报错看起来很像"代码坏了"。
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

exec "$PY" scripts/verify.py
