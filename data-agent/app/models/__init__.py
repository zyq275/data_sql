"""meta 库 ORM 模型（TDD 第 8 章，11 张表）。

此处集中导入，保证 Base.metadata 在 Alembic autogenerate 时能看到全部模型；
新增模型必须在此登记，否则迁移会漏表。
"""

from __future__ import annotations

from app.models.base import Base
from app.models.conversation import Conversation, Message
from app.models.eval import EvalCase, EvalRun
from app.models.meta import ColumnInfo, ColumnMetric, MetricInfo, TableInfo
from app.models.permission import Permission
from app.models.query_history import QueryHistory
from app.models.user import User

__all__ = [
    "Base",
    "ColumnInfo",
    "ColumnMetric",
    "Conversation",
    "EvalCase",
    "EvalRun",
    "Message",
    "MetricInfo",
    "Permission",
    "QueryHistory",
    "TableInfo",
    "User",
]
