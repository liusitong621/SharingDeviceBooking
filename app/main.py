"""
FastAPI 应用装配：建表、预热索引、挂路由、挂静态资源。

启动方式（必须在项目根目录，.rag_cache 与 docs 都按根目录解析）：
    .venv/bin/uvicorn app.main:app --reload --port 8000

一个刻意的取舍：**不使用 BaseHTTPMiddleware 生成 trace_id**。
Starlette 的 BaseHTTPMiddleware 会包一层 body 迭代器，历史上多次导致 SSE 被缓冲、
首字延迟从 300ms 变成整段回答生成完才一次吐出。trace_id 因此在路由里直接生成，
少一个中间件，少一类玄学问题。
"""

from __future__ import annotations

import asyncio
import logging
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import FastAPI, Request
from fastapi.responses import FileResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles

from app import agent_api, auth, booking_api, chat_api, db, rag, seed, web
from app.errors import install_error_handlers

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)-7s %(name)s | %(message)s",
)
logger = logging.getLogger("app.main")

PROJECT_ROOT = Path(__file__).resolve().parent.parent
STATIC_DIR = PROJECT_ROOT / "static"


@asynccontextmanager
async def lifespan(app: FastAPI):
    """启动时建表 + 种子数据 + 建索引 + 起过期扫描；关闭时释放连接池。"""
    db.init_db()
    logger.info("SQLite 就绪：%s", db.DB_PATH)
    await seed.seed_if_empty()

    # 索引构建可能要几十秒（首次要向量化），但失败不阻塞启动：
    # 服务照常起来，前端能拿到明确的 503 而不是连不上。
    await rag.warmup()
    st = rag.status()
    if st["ready"]:
        logger.info("检索就绪：%s 个切片，向量后端 %s，LLM %s",
                    st["chunks"], st["embed_backend"],
                    "已配置" if st["llm_ready"] else "未配置（降级模式）")
    else:
        logger.error("检索未就绪：%s", st["error"])

    # 过期扫描：常驻任务。放在 lifespan 里而不是请求路径上，
    # 否则没人访问的时段就永远不会被释放。
    expire_task = asyncio.create_task(booking_api.expire_worker())

    yield

    expire_task.cancel()
    try:
        await expire_task
    except asyncio.CancelledError:
        pass
    await chat_api.close_llm()


def create_app() -> FastAPI:
    app = FastAPI(
        title="共享设备借用助手",
        version="0.3.0",
        description="制度问答（SSE 流式 + 混合检索）+ 预约 REST + 管理后台",
        lifespan=lifespan,
    )
    install_error_handlers(app)
    app.include_router(chat_api.router)
    app.include_router(booking_api.router)
    app.include_router(agent_api.router)
    app.include_router(web.router)

    # 静态资源与首页。单页应用，不做前端路由，所以 "/" 直接返回 index.html
    app.mount("/static", StaticFiles(directory=STATIC_DIR), name="static")

    # 两条路径指向同一个处理函数：/api/healthz 是给托管平台用的——平台自己会占用
    # 根路径下的 /healthz 返回 "Pod alive"，应用的路由在那里访问不到，看起来像
    # "探针没生效"。/healthz 保留给自有服务器与本地脚本，避免已有巡检命令失效。
    @app.get("/api/healthz", include_in_schema=False)
    @app.get("/healthz", include_in_schema=False)
    async def healthz():
        """
        存活探针：不鉴权，供沙盒健康检查与运维巡检使用。

        rag.status() 的文档字符串里早就写了「/health 与知识库页共用的状态快照」，
        但那个端点一直没实现——巡检时只能靠 /login 是否 200 来判断服务活着，
        而登录页是静态渲染的，数据库和检索都挂了它照样 200。这里补上真正的探针。

        只回报「能不能干活」和「降级到哪一档」，不返回用户数、预约数等业务数据：
        分享链接是公开的，多一个数字就多一条信息泄露路径。
        """
        st = rag.status()
        db_ok = db.DB_PATH.exists()
        return {
            "status": "ok" if db_ok else "starting",
            "version": app.version,
            "db": "ok" if db_ok else "missing",
            "rag": "ready" if st.get("ready") else "degraded",
            "llm": "configured" if st.get("llm_ready") else "fallback",
            "chunks": st.get("chunks", 0),
        }

    @app.get("/", include_in_schema=False)
    async def index(request: Request):
        """
        首页必须登录后才能看。

        这一处校验与 API 层的 current_user 是同一份逻辑（都走 auth.user_from_request），
        区别只在失败时的表现：页面跳登录，接口返回 401 JSON。
        """
        user = await auth.user_from_request(request)
        if user is None:
            # 带上完整来路（含 query）：`/?session=<id>` 这类链接登录后要能回到原处，
            # 否则用户点开链接、登录、然后看到一个空白新会话。
            from urllib.parse import quote

            full = request.url.path
            if request.url.query:
                full = f"{full}?{request.url.query}"
            return RedirectResponse(f"/login?next={quote(full, safe='/?=&-_.~')}", status_code=302)
        if user.get("must_change_pwd"):
            return RedirectResponse("/change-password", status_code=302)
        return FileResponse(STATIC_DIR / "index.html")

    return app


app = create_app()
