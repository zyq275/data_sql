"""全局异常处理（TDD 12.2）。

三类异常在此收敛，输出形状统一为 ``{"code", "message", "data": null}``：

1. ``AppException``  —— 业务异常，按自带的 code / http_status 返回；
2. ``RequestValidationError`` —— 请求体校验失败，压成 40000/400；
3. 其余未捕获异常 —— 兜底 50000/500，细节只进日志，不进响应。

另将 Starlette 自带的 ``HTTPException``（路由 404、方法 405 等）归一化为同样的形状，
避免前端既要解析 ``detail`` 又要解析 ``message``。

安全约束：校验失败时**不得**回显原始输入——pydantic 的 error 字典里带 ``input``
字段，对登录/改密请求就是明文口令，因此此处只取 ``loc`` 与 ``msg``。
"""

from __future__ import annotations

import logging

from fastapi import FastAPI, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from starlette.exceptions import HTTPException as StarletteHTTPException

from app.core.exceptions import AppException

logger = logging.getLogger(__name__)

VALIDATION_CODE = 40000
SERVICE_CODE = 50000

# Starlette 状态码 → 业务码。未列出的一律按 4xx→40000 / 5xx→50000 处理。
_STATUS_TO_CODE = {
    400: 40000,
    401: 40100,
    403: 40300,
    404: 40400,
    405: 40000,
}
_STATUS_MESSAGE = {
    404: "接口不存在",
    405: "请求方法不允许",
}


def _envelope(code: int, message: str) -> dict:
    return {"code": code, "message": message, "data": None}


def _safe_errors(exc: RequestValidationError) -> str:
    """把 pydantic 校验错误压成一行摘要，丢弃 input 以免回显口令。"""
    parts: list[str] = []
    for err in exc.errors()[:5]:
        loc = ".".join(str(x) for x in err.get("loc", ()) if x != "body")
        parts.append(f"{loc or 'body'}: {err.get('msg', '')}")
    summary = "；".join(parts) or "参数校验失败"
    total = len(exc.errors())
    if total > 5:
        summary += f"（共 {total} 处）"
    return summary


def register_exception_handlers(app: FastAPI) -> None:
    """注册全局异常处理器。"""

    @app.exception_handler(AppException)
    async def _handle_app_exception(request: Request, exc: AppException) -> JSONResponse:
        # 5xx 说明是服务端问题，记完整堆栈；4xx 是调用方问题，记一行即可
        if exc.http_status >= 500:
            logger.error(
                "业务异常(服务端) %s %s", request.method, request.url.path, exc_info=exc
            )
        else:
            logger.info(
                "业务异常 %s %s -> %s %s",
                request.method,
                request.url.path,
                exc.code,
                exc.message,
            )
        return JSONResponse(
            status_code=exc.http_status,
            content=_envelope(exc.code, exc.message),
        )

    @app.exception_handler(RequestValidationError)
    async def _handle_validation(request: Request, exc: RequestValidationError) -> JSONResponse:
        message = _safe_errors(exc)
        logger.info("参数校验失败 %s %s -> %s", request.method, request.url.path, message)
        return JSONResponse(
            status_code=400,
            content=_envelope(VALIDATION_CODE, message),
        )

    @app.exception_handler(StarletteHTTPException)
    async def _handle_http_exception(
        request: Request, exc: StarletteHTTPException
    ) -> JSONResponse:
        status = exc.status_code
        code = _STATUS_TO_CODE.get(status)
        if code is None:
            code = SERVICE_CODE if status >= 500 else VALIDATION_CODE
        message = _STATUS_MESSAGE.get(status) or (
            exc.detail if isinstance(exc.detail, str) and exc.detail else "请求失败"
        )
        return JSONResponse(status_code=status, content=_envelope(code, message))

    @app.exception_handler(Exception)
    async def _handle_unexpected(request: Request, exc: Exception) -> JSONResponse:
        logger.error("未捕获异常 %s %s", request.method, request.url.path, exc_info=exc)
        return JSONResponse(
            status_code=500,
            content=_envelope(SERVICE_CODE, "服务内部错误"),
        )
