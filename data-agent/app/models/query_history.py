"""查询历史（TDD 8.1 query_history）。"""

from __future__ import annotations

from datetime import datetime

from sqlalchemy import JSON, DateTime, Index, Integer, String, Text
from sqlalchemy.orm import Mapped, mapped_column

from app.models.base import Base


class QueryHistory(Base):
    """单次问答的落库记录，按 user_id 隔离（BR-HIST-001）。

    - status 取值 success / failed
    - 落库失败须隔离捕获，不影响主流程（NFR-STAB-003）
    """

    __tablename__ = "query_history"
    __table_args__ = (Index("idx_user_id", "user_id"),)

    id: Mapped[str] = mapped_column(String(64), primary_key=True)
    user_id: Mapped[str] = mapped_column(String(64), nullable=False)
    session_id: Mapped[str | None] = mapped_column(String(64))
    query_text: Mapped[str | None] = mapped_column(Text)
    generated_sql: Mapped[str | None] = mapped_column(Text)
    result_preview: Mapped[dict | None] = mapped_column(JSON)
    explain_text: Mapped[str | None] = mapped_column(Text)
    status: Mapped[str | None] = mapped_column(String(32))
    duration_ms: Mapped[int | None] = mapped_column(Integer)
    created_at: Mapped[datetime] = mapped_column(DateTime, nullable=False, default=datetime.now)
