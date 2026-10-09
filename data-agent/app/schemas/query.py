"""问答接口的请求模型（TDD 7.2 query 组）。"""

from __future__ import annotations

from pydantic import BaseModel, Field

MAX_QUERY_CHARS = 500
"""问题长度上限。

设上限而不是任由模型吞：超长输入会挤占上下文预算，把表结构和日期挤出去，
表现为"问题一长就生成错 SQL"，而错因藏在长度里、很难看出来。500 字对
自然语言问数足够，超出说明用户可能误把整段文档粘了进来。
"""


class QueryRequest(BaseModel):
    """单轮问答入参。"""

    query: str = Field(
        min_length=1,
        max_length=MAX_QUERY_CHARS,
        description="用户的自然语言问题",
        examples=["去年华东区销售额前三的省份"],
    )
    session_id: str | None = Field(
        default=None,
        description="会话标识（POST /conversations 返回的 id）；不传即为一次性提问",
    )
