"""用户与角色（TDD 8.1 user）。"""

from __future__ import annotations

from datetime import datetime

from sqlalchemy import Boolean, DateTime, String, UniqueConstraint, text
from sqlalchemy.orm import Mapped, mapped_column

from app.models.base import Base


class User(Base):
    """平台用户。

    - role 取值 viewer / analyst / admin，权限顺序 viewer < analyst < admin（BR-AUTH-002）
    - password_hash 只存 bcrypt 哈希，禁止明文（BR-AUTH-001）
    """

    __tablename__ = "user"
    __table_args__ = (UniqueConstraint("username", name="uk_username"),)

    id: Mapped[str] = mapped_column(String(64), primary_key=True)
    username: Mapped[str] = mapped_column(String(128), nullable=False)
    password_hash: Mapped[str] = mapped_column(String(256), nullable=False)
    role: Mapped[str] = mapped_column(String(32), nullable=False)
    display_name: Mapped[str | None] = mapped_column(String(128))
    is_active: Mapped[bool | None] = mapped_column(Boolean, server_default=text("1"))
    created_at: Mapped[datetime] = mapped_column(DateTime, nullable=False, default=datetime.now)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime, nullable=False, default=datetime.now, onupdate=datetime.now
    )
