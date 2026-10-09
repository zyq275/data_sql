"""问答接口（TDD 7.2 query 组 / 7.3 SSE 事件）。

POST /query   自然语言问数，SSE 流式返回（Bearer）

**为什么是 POST 而不是 GET**：问题原文要进请求体。GET 得把它塞进 URL，
中文要编码、长者受浏览器与代理的长度限制，而且问题原文会落进访问日志与
浏览器历史——用户在问数时贴进来的往往不止一句话（有时带客户名、金额），
这些不该出现在日志里。
"""

from __future__ import annotations

import json
import logging
from collections.abc import AsyncIterator

from fastapi import APIRouter
from fastapi.responses import StreamingResponse

from app.api.deps import AuthUser, SessionDep
from app.schemas.query import QueryRequest
from app.services.query_service import QueryService

logger = logging.getLogger(__name__)

router = APIRouter(tags=["问答"])

SSE_MEDIA_TYPE = "text/event-stream"
"""SSE 的 media type。

不用 ``ApiResponse`` 信封包一层：SSE 是一条长连接上的事件序列，没有"一个响应体"
可以套信封，每个事件自成一个 JSON（TDD 7.3）。
"""


async def _encode(events: AsyncIterator[dict]) -> AsyncIterator[str]:
    """把事件字典编码成 SSE 帧。

    ``event:`` 带上事件类型，前端 ``EventSource`` 才能按类型分发；``data:`` 里
    保留完整的 ``type`` 字段，使每一帧自身可读——日志里捞出来的单帧、
    ``curl`` 手撸的一段输出，都不必回头看是哪一行 event 才知道这是什么。

    ``ensure_ascii=False``：默认会把中文转成 ``\\uXXXX``，字节数翻几倍且人不可读。
    帧之间必须留空行，否则客户端会把相邻两帧当成同一帧的数据继续拼接。
    """
    async for event in events:
        data = json.dumps(event, ensure_ascii=False, default=str)
        yield f"event: {event.get('type', 'message')}\ndata: {data}\n\n"


@router.post("/query", summary="自然语言问数（SSE）")
async def query(payload: QueryRequest, session: SessionDep, user: AuthUser) -> StreamingResponse:
    """Text-to-SQL 问答，以 SSE 推送进度、SQL、结果与错误（FR-QA-001~008）。

    每步进度一条 ``progress``，SQL 一条 ``sql``，结果一条 ``result``，
    结束时一条 ``done``（出错则为 ``error``，且只报一次）。事件定义见 TDD 7.3。

    带 ``session_id`` 即为多轮（FR-CONV-002）：会话必须归本人，上文由服务层从
    ``message`` 表取，会话 id 同时用作 checkpointer 的 thread_id。不带则是一次性
    提问，同样会落一条查询历史。

    **准备必须在构造响应之前完成**：SSE 的响应头在第一次迭代事件流时就发出去了，
    之后改不了状态码。会话不存在（或不属于本人）时要给的是一个正常的 404 信封
    （TDD 7.1），而不是一条被掐断的连接——所以 ``prepare`` 在开流前调用。
    """
    service = QueryService(session)
    turn = await service.prepare(user.id, payload.session_id)
    events = service.stream(payload.query, user_id=user.id, turn=turn)
    return StreamingResponse(
        _encode(events),
        media_type=SSE_MEDIA_TYPE,
        headers={
            # 关掉各级反向代理的缓冲：被缓冲住的话，事件会攒到连接结束才一次性吐出，
            # 流式的意义（让用户先看到进度和 SQL）就没了，而且在本地一切正常、
            # 一上生产就"不流式了"，属于最难查的那类差异
            "Cache-Control": "no-cache",
            "X-Accel-Buffering": "no",
            "Connection": "keep-alive",
        },
    )
