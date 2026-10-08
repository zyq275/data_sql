"""应用入口。

启动：uvicorn main:app --reload
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

from fastapi import FastAPI

from app.api.health import router as health_router
from app.api.middleware import register_cors
from app.api.v1.router import router as v1_router
from app.clients.mysql import dispose_engine
from app.core.config import Settings, get_settings
from app.core.handlers import register_exception_handlers


def check_security_settings(settings: Settings) -> None:
    """启动自检：缺失关键密钥直接拒绝启动，而不是运行期才 500（NFR-SEC-003）。"""
    if not settings.jwt.secret_key:
        raise RuntimeError(
            "jwt.secret_key 未配置，请在 .env 中设置 JWT__SECRET_KEY（NFR-SEC-003）"
        )


@asynccontextmanager
async def lifespan(app: FastAPI) -> AsyncIterator[None]:
    """优雅启停钩子。

    MySQL 引擎按需惰性创建（app.clients.mysql），此处只负责停机释放；
    Qdrant/ES/Redis 的生命周期在阶段 4/6 接入后一并纳入（TDD 第 10 章）。
    """
    settings = get_settings()
    print(f"[startup] {settings.app.name} v{settings.app.version} (debug={settings.app.debug})")
    yield
    await dispose_engine()
    print("[shutdown] bye")


def create_app() -> FastAPI:
    """组装 FastAPI 应用。"""
    settings = get_settings()
    check_security_settings(settings)

    app = FastAPI(
        title=settings.app.name,
        version=settings.app.version,
        debug=settings.app.debug,
        lifespan=lifespan,
    )

    register_cors(app, settings)
    # 异常处理器要在路由之前注册，保证所有端点（含 404/405）都走统一响应结构
    register_exception_handlers(app)

    # 探针挂在根路径（TDD 7.2），业务接口统一挂在 api_prefix 下
    app.include_router(health_router)
    app.include_router(v1_router, prefix=settings.app.api_prefix)

    return app


app = create_app()


if __name__ == "__main__":
    import uvicorn

    uvicorn.run("main:app", host="0.0.0.0", port=8000, reload=True)
