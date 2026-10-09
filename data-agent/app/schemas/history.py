"""查询历史的响应模型（FR-HIST-001~003，TDD 7.2 history 组）。

**列表项与详情分成两个模型**，不是重复。列表一页最多 100 条，每条都带上
完整 SQL 与结果预览的话，单次响应会到几百 KB，而用户翻列表时并不看这两个字段——
那是点进去之后的事。分开之后，"列表为什么这么快"和"详情为什么必须点开"
都有了明确答案，而不是靠调小 page_size 去掩盖。
"""

from __future__ import annotations

from datetime import datetime
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict

HistoryStatus = Literal["success", "failed"]
"""历史状态（FR-HIST-001 的筛选维度）。取值与 ``QueryHistoryRepository`` 的常量一致。"""


class HistoryItem(BaseModel):
    """历史列表项（FR-HIST-001）。"""

    model_config = ConfigDict(from_attributes=True)

    id: str
    session_id: str | None = None
    query_text: str | None = None
    status: str | None = None
    duration_ms: int | None = None
    created_at: datetime


class HistoryDetail(HistoryItem):
    """历史详情：在列表项之上补出问题/SQL/结果预览/解释（FR-HIST-002）。

    继承而不是另起一个类：详情必须包含列表里的**全部**字段，否则会出现
    "列表里看得到耗时、点进去反而没了"这种自相矛盾。继承让这条约束由类型系统
    守着——将来往列表项加字段，详情自动跟上。

    ``result_preview`` 是 ``JSON`` 列，取出来已是 dict/list；标成 ``Any`` 而不是
    ``dict``，因为早期数据可能是列表形态，用 dict 声明会让整条响应校验失败——
    一条老数据不该让整个详情打不开。``HistoryItem`` 不含它，同一个理由也适用于
    ``session_id`` 等历史遗留的空值，故这些字段都给了默认值。
    """

    generated_sql: str | None = None
    result_preview: Any = None
    explain_text: str | None = None
