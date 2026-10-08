"""ORM 基类。

阶段 1 仅提供基类与命名约定，meta 库的 11 张表在阶段 2 定义（见 TDD 第 8 章）。
统一命名约定可让 Alembic autogenerate 的索引/约束名稳定、可回滚。
"""

from __future__ import annotations

from sqlalchemy import MetaData
from sqlalchemy.orm import DeclarativeBase

NAMING_CONVENTION = {
    "ix": "ix_%(table_name)s_%(column_0_N_name)s",
    "uq": "uq_%(table_name)s_%(column_0_N_name)s",
    "ck": "ck_%(table_name)s_%(constraint_name)s",
    "fk": "fk_%(table_name)s_%(column_0_N_name)s_%(referred_table_name)s",
    "pk": "pk_%(table_name)s",
}


class Base(DeclarativeBase):
    """所有 ORM 模型的基类（对应 meta 库）。"""

    metadata = MetaData(naming_convention=NAMING_CONVENTION)
