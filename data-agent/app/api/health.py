"""健康探针（FR-SYS-001、FR-SYS-002）。"""

from __future__ import annotations

from fastapi import APIRouter

from app.core.config import get_settings

router = APIRouter(tags=["system"])


@router.get("/health", summary="存活探针")
async def health() -> dict:
    """进程存活即返回 200，不探测外部依赖。"""
    settings = get_settings()
    return {
        "status": "ok",
        "app": settings.app.name,
        "version": settings.app.version,
        "debug": settings.app.debug,
    }


@router.get("/health/ready", summary="就绪探针")
async def readiness() -> dict:
    """就绪探针。

    阶段 1 尚未接入外部依赖，恒为就绪；MySQL/Qdrant/Redis 检查与异常时返回 503
    在阶段 4/6/7 依赖接入后补齐（FR-SYS-002）。
    """
    return {"status": "ok", "checks": {}}
