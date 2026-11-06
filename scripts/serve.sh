#!/usr/bin/env bash
# 单端口启动入口：本地开发与沙盒部署共用同一条命令。
#
# 沙盒只暴露一个 HTTP 端口，并通过 PORT 环境变量告知端口号；服务必须绑定
# 0.0.0.0 才能被反向代理访问。本地不设 PORT 时退回 8000。
#
#   PORT=8000 bash scripts/serve.sh          # 本地
#   PORT=$PORT bash scripts/serve.sh         # 沙盒（工具自动注入 PORT）
#
# 刻意固定 --workers 1：会话锁与登录限流都是进程内 dict（见 app/db.py 顶部说明），
# 起多个 worker 会让「同一格只能有一单」以外的并发判定失效，而且失败是静默的。
set -euo pipefail

cd "$(dirname "$0")/.."

PORT="${PORT:-8000}"

if [ -n "${PYTHON:-}" ]; then
    # 显式指定优先：本地可以用一个干净 venv 复现沙盒的依赖组合
    PY="$PYTHON"
elif [ -x .venv/bin/python ]; then
    PY=.venv/bin/python
else
    # 沙盒里 .venv 不会随源码上传，依赖装在沙盒自己的解释器里
    PY=python3
fi

# 保留访问日志：沙盒里唯一能回答「刚才谁来过、哪个接口慢」的证据就是 stdout。
exec "$PY" -m uvicorn app.main:app \
    --host 0.0.0.0 \
    --port "$PORT" \
    --workers 1
