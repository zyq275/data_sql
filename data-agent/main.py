"""应用入口。

启动：uvicorn main:app --reload
"""

from __future__ import annotations

from contextlib import asynccontextmanager
from collections.abc import AsyncIterator

from fastapi import FastAPI

from app.api.health import router as health_router
from app.api.middleware import register_cors
from app.api.v1.router import router as v1_router
from app.core.config import get_settings


@asynccontextmanager
async def lifespan(app: FastAPI) -> AsyncIterator[None]:
    """优雅启停钩子。

    外部连接（MySQL/Qdrant/ES/Redis）在 ClientManager 就绪后于此处 init/close，
    见 TDD 第 10 章；阶段 1 仅保留生命周期骨架。
    """
    settings = get_settings()
    print(f"[startup] {settings.app.name} v{settings.app.version} (debug={settings.app.debug})")
    yield
    print("[shutdown] bye")


def create_app() -> FastAPI:
    """组装 FastAPI 应用。"""
    settings = get_settings()

    app = FastAPI(
        title=settings.app.name,
        version=settings.app.version,
        debug=settings.app.debug,
        lifespan=lifespan,
    )

    register_cors(app, settings)

    # 探针挂在根路径（TDD 7.2），业务接口统一挂在 api_prefix 下
    app.include_router(health_router)
    app.include_router(v1_router, prefix=settings.app.api_prefix)

    return app


app = create_app()


if __name__ == "__main__":
    import uvicorn

    uvicorn.run("main:app", host="0.0.0.0", port=8000, reload=True)
