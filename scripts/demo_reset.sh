#!/usr/bin/env bash
#
# 一条命令把演示库准备好：备份 → 灌数据 → 打印账号与下一步。
#
# 为什么备份放在这里而不是脚本里：seed_demo.py 可能被直接调用（CI、测试、
# 或指向 /tmp 的临时库），那种场景不需要备份；而人对 data/chat.db 动手时
# 一定要有退路。备份是「这个入口」的职责，不是种子脚本的职责。
#
# 用法：
#   ./scripts/demo_reset.sh                  # 在当前库上重灌（幂等）
#   ./scripts/demo_reset.sh --fresh          # 备份后从空库重建
#   ./scripts/demo_reset.sh --dry-run        # 只看计划，不写库
#   ./scripts/demo_reset.sh --history-days 30

set -euo pipefail

ROOT="$(cd "$(dirname "$0")/.." && pwd)"
cd "$ROOT"

PY=".venv/bin/python"
if [ ! -x "$PY" ]; then
    PY="$(command -v python3)"
fi

DB="data/chat.db"
DRY=0
for arg in "$@"; do
    [ "$arg" = "--dry-run" ] && DRY=1
done

# 试运行不备份：它根本不写库，凭空多出一个 .bak 只会让人以为改了什么。
if [ "$DRY" = 0 ] && [ -f "$DB" ]; then
    BACKUP="$DB.bak.$(date +%Y%m%d-%H%M%S)"
    cp "$DB" "$BACKUP"
    echo "已备份现有库：$BACKUP"
fi

exec "$PY" scripts/seed_demo.py "$@"
