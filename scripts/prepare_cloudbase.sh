#!/usr/bin/env bash
# 生成 CloudBase 云托管的「干净构建上下文」。
#
# 为什么不能直接把项目根当 targetPath：云端源码构建会把整个目录打成 zip 上传，
# 实测它不读 .dockerignore 也不读 .gitignore —— .venv/（109M）和 data/chat.db
# （含演示账号的口令哈希）都会被送进构建机。Dockerfile 并没有 COPY 它们，
# 镜像是干净的，但上传这一步既慢又不该发生。这里显式列出构建真正需要的文件。
#
#   bash scripts/prepare_cloudbase.sh                  # 默认 build/cloudbase/
#   bash scripts/prepare_cloudbase.sh /tmp/cb-ctx     # 指定目录
#
# 输出的目录路径会打印到 stdout，可直接作为 manageCloudRun 的 targetPath。
set -euo pipefail

ROOT="$(cd "$(dirname "$0")/.." && pwd)"
OUT="${1:-$ROOT/build/cloudbase}"

rm -rf "$OUT"
mkdir -p "$OUT/scripts" "$OUT/docs"

cp "$ROOT/Dockerfile" "$ROOT/.dockerignore" "$ROOT/requirements.txt" "$OUT/"
cp -R "$ROOT/app" "$ROOT/static" "$ROOT/templates" "$OUT/"

# 启动入口 + RAG 的单文件原型（app/rag.py 用 importlib 动态加载它）。
cp "$ROOT/scripts/serve.sh" "$ROOT/scripts/stage1_rag_prototype.py" "$OUT/scripts/"

# 制度正本：rag.py 在没有 docs/manuals/ 时按 *设备借用制度*.md 回退挑选，
# 多带几份 md 不影响索引，但改名后不至于让 COPY 直接失败。
cp "$ROOT"/docs/*.md "$OUT/docs/"

# 环境与服务名，让云端工具在上下文目录里读到和仓库一致的配置。
if [ -f "$ROOT/cloudbaserc.json" ]; then
    cp "$ROOT/cloudbaserc.json" "$OUT/"
fi

echo "$OUT"
