"""接口层中间件。

阶段 1 仅注册 CORS 白名单；request_id 追踪中间件在阶段 11（可观测性）接入。
"""

from __future__ import annotations

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware

from app.core.config import Settings

ALLOWED_METHODS = ["GET", "POST", "PUT", "PATCH", "DELETE", "OPTIONS"]
ALLOWED_HEADERS = ["Authorization", "Content-Type", "Accept", "X-Request-Id"]


def register_cors(app: FastAPI, settings: Settings) -> None:
    """按白名单注册 CORS，生产环境禁止通配符（NFR-SEC-006）。"""
    origins = [o for o in settings.cors_origins if o]

    if "*" in origins and not settings.app.debug:
        raise RuntimeError(
            "cors_origins 含通配符 '*'，仅允许在 debug 模式下使用（NFR-SEC-006）"
        )

    if not origins:
        return

    allow_credentials = "*" not in origins
    app.add_middleware(
        CORSMiddleware,
        allow_origins=origins,
        allow_credentials=allow_credentials,
        allow_methods=ALLOWED_METHODS,
        allow_headers=ALLOWED_HEADERS,
    )
