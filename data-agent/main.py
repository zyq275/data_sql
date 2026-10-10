"""应用入口。

启动：uvicorn main:app --reload
"""

from __future__ import annotations

import logging
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

from fastapi import FastAPI

from app.api.health import router as health_router
from app.api.middleware import register_cors, register_request_id
from app.api.v1.router import router as v1_router
from app.agent.llm import dispose_llm
from app.clients.embedding import dispose_client as dispose_embedding
from app.clients.es import dispose_es
from app.clients.mysql import dispose_engine
from app.clients.qdrant import dispose_qdrant
from app.clients.redis import dispose_redis
from app.clients.rerank import dispose_client as dispose_rerank
from app.core.config import Settings, get_settings
from app.core.handlers import register_exception_handlers
from app.core.log_setup import setup_logging
from app.core.tracing import dispose_tracing, setup_tracing, telemetry_config

logger = logging.getLogger(__name__)


def check_security_settings(settings: Settings) -> None:
    """启动自检：缺失关键密钥直接拒绝启动，而不是运行期才 500（NFR-SEC-003）。"""
    if not settings.jwt.secret_key:
        raise RuntimeError(
            "jwt.secret_key 未配置，请在 .env 中设置 JWT__SECRET_KEY（NFR-SEC-003）"
        )
    if not settings.llm.api_key:
        # 和 JWT 密钥同样处理：没有它，整个问答链路一次都跑不通，而失败会以
        # 「每个请求都报 50001」的形式出现——现象在业务接口，原因在配置，
        # 排查要绕一大圈。启动即拒、并把要设的键名写在提示里，代价是一次重启。
        #
        # 反方观点是"管理接口不依赖 LLM，不该被拖住"；但一个问数服务起得来、
        # 问不了数，对使用者的误导更大，不一致的可用状态更难被察觉。
        raise RuntimeError(
            "llm.api_key 未配置，请在 .env 中设置 LLM__API_KEY（阶段 5 起问答接口必需）"
        )


@asynccontextmanager
async def lifespan(app: FastAPI) -> AsyncIterator[None]:
    """优雅启停钩子。

    各客户端都按需惰性创建（app.clients.*），此处只负责停机释放——连不上的
    依赖不该拖住启动。Redis 自阶段 7 起纳入（会话 checkpointer，ADR-006）。
    """
    settings = get_settings()
    # 这里是标准库 logging，占位符必须是 %s：写成 {} 不会当场报错，
    # 而是在 emit 时才炸（`msg % args` 找不到 % 占位符）——也就是在启动那一刻炸。
    logger.info("%s v%s 启动（debug=%s）", settings.app.name, settings.app.version, settings.app.debug)
    yield
    # 追踪要在其余资源之前收尾：它要把缓冲区里的 span 刷出去，而刷的动作本身
    # 可能触发一次 HTTP 上报（Langfuse）——趁进程还在、连接池还没关的时候做
    dispose_tracing()
    await dispose_engine()  # 含 meta 与 dw 两个引擎
    await dispose_qdrant()
    await dispose_es()
    await dispose_embedding()
    # checkpointer 持有 Redis 连接；不关会在测试与热重载里累积成"连接未关闭"告警
    await dispose_redis()
    # 重排客户端只在真的启用精排时才会被创建（惰性），dispose 对未创建的客户端是空操作
    await dispose_rerank()
    # LLM 客户端持有 httpx 连接池，不关会在测试与热重载里累积成"连接未关闭"告警
    await dispose_llm()
    logger.info("%s 已停机", settings.app.name)


def create_app() -> FastAPI:
    """组装 FastAPI 应用。"""
    settings = get_settings()

    # 日志要排在启动自检**之前**：自检失败会直接抛出去（NFR-SEC-003），
    # 而那正是最需要看见的一条日志——排在后面的话，它恰好是唯一没经过 loguru 的消息。
    setup_logging(settings.observability.log_level)
    check_security_settings(settings)
    # 追踪排在自检之后：一个注定起不来的进程不该先把数据发去第三方。
    setup_tracing(settings)

    app = FastAPI(
        title=settings.app.name,
        version=settings.app.version,
        # 恒为 False，不透传 settings.app.debug（TDD 7.1 要求响应结构恒定统一）。
        # Starlette 的 ServerErrorMiddleware 在 debug=True 时会让调试页优先于我们注册的
        # 兜底 handler，未捕获异常将返回裸 traceback 文本而非 {code, message, data}，
        # 前端的 code 解析恰好会在开发环境被打破；且调试页会渲染帧局部变量，
        # 对登录/改密这类请求有泄明文口令的风险。
        # 排查所需的堆栈由 core.handlers 的 _handle_unexpected 写进日志，信息不丢；
        # 本地热重载用 uvicorn --reload，与 debug 开关无关。
        # settings.app.debug 仍用于别处：CORS 通配符放行、启动日志与 /health 输出。
        debug=False,
        lifespan=lifespan,
        # 接口层的链路埋点走 FastAPI 自带的 OpenTelemetry 支持（0.128 起），
        # 它装在 __call__ 里、比整个中间件栈还外一层（见 core.tracing.telemetry_config）。
        # 传进来的 provider 是 None：用 setup_tracing 刚装好的那个全局 provider，
        # 于是接口层的 span 与 Agent 节点的 span 落在同一棵树上。
        telemetry=telemetry_config(settings),
    )

    # 中间件的注册顺序 = 由内到外（见 api.middleware 的模块说明）：
    # request_id → CORS。再往外是 FastAPI 原生 telemetry，它不在这个栈里。
    register_request_id(app)
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

    uvicorn.run("main:app", host="127.0.0.1", port=8000, reload=True)
