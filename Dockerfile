# 共享设备借用助手 · CloudBase 云托管容器镜像
#
# 单端口模型：平台注入 PORT（本项目固定 8080），uvicorn 必须绑 0.0.0.0。
# scripts/serve.sh 是本地 / 沙盒 / 云托管共用的唯一启动入口，别在这里另写一条
# uvicorn 命令：--workers 1 是会话锁与登录限流（进程内 dict）的硬约束，
# 多开一条启动路径就多一个地方会把它漏掉，而且失败是静默的。
FROM python:3.12-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PORT=8080 \
    TZ=Asia/Shanghai

WORKDIR /app

# 制度规定「时间以 Asia/Shanghai 为准」，归还时间在 db 里按服务器本地时区解释。
# 容器默认 UTC 会让 08:00–20:00 的可预约格子和「今天」整体偏 8 小时，
# 这种错不会报错，只会让预约判定悄悄算错。
RUN if [ ! -f /usr/share/zoneinfo/Asia/Shanghai ]; then \
        apt-get update && apt-get install -y --no-install-recommends tzdata \
        && rm -rf /var/lib/apt/lists/*; \
    fi \
    && ln -snf /usr/share/zoneinfo/Asia/Shanghai /etc/localtime \
    && echo Asia/Shanghai > /etc/timezone

COPY requirements.txt ./
RUN pip install --no-cache-dir -r requirements.txt

COPY app/ ./app/
COPY static/ ./static/
COPY templates/ ./templates/
COPY scripts/serve.sh ./scripts/serve.sh
# app/rag.py 用 importlib 按路径加载阶段 1 原型，不是 import 包。少这一个文件不会
# 让进程起不来——warmup() 刻意吞掉索引异常——只会静默退化成 rag=degraded/chunks=0，
# 问答功能等于废了但从探针之外看不出来。
COPY scripts/stage1_rag_prototype.py ./scripts/stage1_rag_prototype.py
COPY docs/ ./docs/

# 运行期可写目录：SQLite、上传语料、回收站、RAG 索引缓存。
# 容器文件系统不持久，重启或重新部署后由 seed 重建演示数据——这是演示实例的
# 明确取舍，不是遗漏；正式环境要把 data/ 换成持久卷或托管数据库。
RUN mkdir -p /app/data/kb/uploads /app/data/kb/trash /app/.rag_cache

EXPOSE 8080

CMD ["bash", "scripts/serve.sh"]
