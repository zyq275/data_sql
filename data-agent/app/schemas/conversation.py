"""会话与消息的请求/响应模型（FR-CONV-001~005，TDD 7.2 conversation 组）。"""

from __future__ import annotations

from datetime import datetime
from typing import Any

from pydantic import BaseModel, ConfigDict, Field

MAX_TITLE_CHARS = 256
"""会话标题长度上限，与 ``conversation.title`` 的列宽一致。

在这里卡住而不是入库时才炸：MySQL 对超长字符串的处理取决于 sql_mode，
严格模式下报错、宽松模式下**静默截断**。后者更危险——用户看到的标题
和存下来的不是同一个，而且没有任何提示。
"""


class ConversationCreateRequest(BaseModel):
    """创建会话入参（FR-CONV-001）。标题可留空，由首轮问题回填。"""

    title: str | None = Field(default=None, max_length=MAX_TITLE_CHARS)


class ConversationOut(BaseModel):
    """会话概要，用于列表（FR-CONV-005）。"""

    model_config = ConfigDict(from_attributes=True)

    id: str
    title: str | None = None
    created_at: datetime
    updated_at: datetime


class MessageOut(BaseModel):
    """会话内一轮消息。

    ``result_preview`` 是 ``JSON`` 列，取出来已是 dict/list；标成 ``Any`` 而不是
    ``dict``，因为历史遗留数据可能是列表形态，用 dict 声明会让整条响应校验失败——
    一条老数据不该让整个会话详情打不开。
    """

    model_config = ConfigDict(from_attributes=True)

    id: str
    role: str
    query_text: str | None = None
    generated_sql: str | None = None
    result_preview: Any = None
    explain_text: str | None = None
    created_at: datetime


class ConversationDetailOut(ConversationOut):
    """会话详情：概要 + 该会话的消息列表（FR-CONV-002、TDD 7.2）。

    ``messages`` 按发生顺序（早的在前）——与阅读顺序一致。
    仓储层为了能取"最近 N 轮"而按倒序查，翻转放在服务层做：
    "取哪几条"是存储的优化，"按什么顺序看"是接口的契约，两者混在一起时，
    改一个会顺手改坏另一个。
    """

    messages: list[MessageOut] = Field(default_factory=list)
