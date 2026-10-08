"""会话与消息（TDD 8.1 conversation / message）。"""

from __future__ import annotations

from datetime import datetime

from sqlalchemy import JSON, DateTime, Index, String, Text
from sqlalchemy.orm import Mapped, mapped_column

from app.models.base import Base


class Conversation(Base):
    """多轮对话的上下文容器，按 user_id 隔离（BR-CONV-001）。"""

    __tablename__ = "conversation"
    __table_args__ = (Index("idx_user_id", "user_id"),)

    id: Mapped[str] = mapped_column(String(64), primary_key=True)
    user_id: Mapped[str] = mapped_column(String(64), nullable=False)
    title: Mapped[str | None] = mapped_column(String(256))
    created_at: Mapped[datetime] = mapped_column(DateTime, nullable=False, default=datetime.now)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime, nullable=False, default=datetime.now, onupdate=datetime.now
    )


class Message(Base):
    """会话内消息，按轮次同时留存问题、SQL、结果摘要与解释。"""

    __tablename__ = "message"
    __table_args__ = (
        Index("idx_conversation_id", "conversation_id"),
        Index("idx_user_id", "user_id"),
    )

    id: Mapped[str] = mapped_column(String(64), primary_key=True)
    conversation_id: Mapped[str] = mapped_column(String(64), nullable=False)
    user_id: Mapped[str] = mapped_column(String(64), nullable=False)
    role: Mapped[str] = mapped_column(String(16), nullable=False)
    query_text: Mapped[str | None] = mapped_column(Text)
    generated_sql: Mapped[str | None] = mapped_column(Text)
    result_preview: Mapped[dict | None] = mapped_column(JSON)
    explain_text: Mapped[str | None] = mapped_column(Text)
    created_at: Mapped[datetime] = mapped_column(DateTime, nullable=False, default=datetime.now)
