"""接口层中间件：CORS 白名单（阶段 1）与 request_id 贯穿（阶段 11）。

**注册顺序是有讲究的**：Starlette 的 ``add_middleware`` 是"后加的在外层"，
而这两个中间件有一个偏序关系——

    OpenTelemetry（最外） → CORS → request_id → 应用

- request_id 必须在 OTel **里面**：它要把 id 写到当前的 span 上，
  而那个 span 是别人建的。反过来的话，写属性时 span 还不存在。
- CORS 在 request_id 外面：预检请求（OPTIONS）由 CORS 中间件直接短路返回，
  不该在它前面再插一层业务无关的中间件。
- OTel 在最外层：它的 span 要覆盖**整个**请求，包括 CORS 短路掉的那些——
  否则"预检请求为什么慢"在链路里是查不到的。

这一层里的 OTel **不在这个文件**：它由 FastAPI 原生 telemetry 提供，
装在 ``FastAPI.__call__`` 里，比整个 ``add_middleware`` 栈还外一层
（配置见 ``core.tracing.telemetry_config``）。所以 ``create_app`` 里只有
"先内后外"的两步：request_id → CORS。
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from typing import Any

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from starlette.datastructures import MutableHeaders

from app.core.config import Settings
from app.core.request_context import (
    REQUEST_ID_HEADER,
    normalize_request_id,
    reset_request_id,
    set_request_id,
)
from app.core.tracing import ATTR_REQUEST_ID, annotate_current_span

ALLOWED_METHODS = ["GET", "POST", "PUT", "PATCH", "DELETE", "OPTIONS"]
ALLOWED_HEADERS = ["Authorization", "Content-Type", "Accept", "X-Request-Id"]

#: Langfuse 从**根 span** 上读 ``langfuse.trace.metadata.*`` 并挂到整条 trace 上。
#: 把 request_id 放这里，Langfuse 里就能按 request_id 搜到那一次请求的完整链路——
#: 日志与链路于是可以互查：日志里拿到 id，去 Langfuse 搜；反过来也一样。
TRACE_METADATA_REQUEST_ID = "langfuse.trace.metadata.request_id"


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


class RequestIdMiddleware:
    """给每个请求定一个 request_id，并让它跟着这个请求走完。

    三件事，缺一不可：

    1. 设进 contextvar —— 此后本请求内的**任何**日志都自动带上它（见 log_setup）；
    2. 回写响应头 —— 用户报障时能把 id 给出来；只存在于服务端日志里的 id
       等于没有 id；
    3. 写到当前 span 上 —— 日志与链路由此可以互查。

    **写成裸 ASGI 中间件而不是 ``BaseHTTPMiddleware``**：后者会把响应包装成
    一个额外的 anyio task，而 SSE 这种长连接在包装里会走成"攒够一批再发"，
    流式就没了（本地小响应看不出差别，一上生产"进度条不动了"）。
    裸 ASGI 直接透传 ``send``，对 SSE 完全无感。顺带，contextvar 的传播也更直白：
    同一层调用里 set/reset，不必猜哪一个 task 拿到了哪一份上下文。

    异常安全：``reset`` 放在 ``finally`` 里。漏掉它的话，id 会留在这个上下文的
    变量里，同一连接上的下一个请求就顶着上一个请求的 id——比没有 id 更坏，
    因为它会把人引到一次无关的请求上去。
    """

    def __init__(self, app: Callable[..., Awaitable[None]]) -> None:
        self.app = app

    async def __call__(self, scope: dict, receive: Any, send: Any) -> None:
        if scope["type"] != "http":
            # websocket / lifespan 没有请求头也没有响应头，直接放行
            await self.app(scope, receive, send)
            return

        request_id = normalize_request_id(_header(scope, REQUEST_ID_HEADER))
        token = set_request_id(request_id)
        # 此刻 span 已经存在（OTel 中间件在更外层），把 id 写到它上面。
        # 两处都写是有意的：span 属性用于按 id 搜链路，
        # trace metadata 让 Langfuse 把它挂到整条 trace 上。
        annotate_current_span(
            {
                ATTR_REQUEST_ID: request_id,
                TRACE_METADATA_REQUEST_ID: request_id,
            }
        )

        async def send_with_request_id(message: dict) -> None:
            if message["type"] == "http.response.start":
                MutableHeaders(scope=message)[REQUEST_ID_HEADER] = request_id
            await send(message)

        try:
            await self.app(scope, receive, send_with_request_id)
        finally:
            reset_request_id(token)


def _header(scope: dict, name: str) -> str | None:
    """从 ASGI scope 里取一个请求头。

    ASGI 把头名统一小写、值统一 bytes，用 ``dict.get`` 拼一个头部名字典是最省事
    也最快的写法：请求头上的头通常不到十个。
    """
    wanted = name.lower().encode("latin-1")
    for key, value in scope.get("headers") or []:
        if key == wanted:
            return value.decode("latin-1")
    return None


def register_request_id(app: FastAPI) -> None:
    """注册 request_id 中间件（应在 CORS 之前调用，见模块说明）。"""
    app.add_middleware(RequestIdMiddleware)
