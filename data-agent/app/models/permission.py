"""数据权限（TDD 8.1 permission）。"""

from __future__ import annotations

from datetime import datetime

from sqlalchemy import DateTime, Index, String
from sqlalchemy.orm import Mapped, mapped_column

from app.models.base import Base


class Permission(Base):
    """角色到表/字段的授权映射。

    - role 取值同 User.role
    - resource_type 取值 table / column
    - resource_id 指向 table_info.id 或 column_info.id

    生成的 SQL 只能使用授权范围内的表与字段（BR-PERM-001、FR-PERM-001）。
    """

    __tablename__ = "permission"
    __table_args__ = (Index("idx_role", "role"),)

    id: Mapped[str] = mapped_column(String(64), primary_key=True)
    role: Mapped[str] = mapped_column(String(32), nullable=False)
    resource_type: Mapped[str] = mapped_column(String(16), nullable=False)
    resource_id: Mapped[str] = mapped_column(String(64), nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime, nullable=False, default=datetime.now)
