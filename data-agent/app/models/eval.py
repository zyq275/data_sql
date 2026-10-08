"""评测体系（TDD 8.1 eval_case / eval_run）。"""

from __future__ import annotations

from datetime import datetime
from decimal import Decimal

from sqlalchemy import JSON, DateTime, Index, Integer, Numeric, String, Text, text
from sqlalchemy.orm import Mapped, mapped_column

from app.models.base import Base


class EvalCase(Base):
    """评测集样本：问题 + 标准 SQL / 预期结果（FR-EVAL-001）。"""

    __tablename__ = "eval_case"

    id: Mapped[str] = mapped_column(String(64), primary_key=True)
    query_text: Mapped[str] = mapped_column(Text, nullable=False)
    expected_sql: Mapped[str | None] = mapped_column(Text)
    expected_result: Mapped[dict | None] = mapped_column(JSON)
    created_at: Mapped[datetime] = mapped_column(DateTime, nullable=False, default=datetime.now)


class EvalRun(Base):
    """单次评测运行的汇总指标与逐条明细。

    与 EvalCase 分表：EvalCase 是题目，EvalRun 是答题记录。
    评测看板的历史趋势读取本表按 created_at 的记录（FR-EVAL-003）；
    评测过程不写入 conversation / message / query_history（BR-EVAL-001）。
    """

    __tablename__ = "eval_run"
    __table_args__ = (Index("idx_created_at", "created_at"),)

    id: Mapped[str] = mapped_column(String(64), primary_key=True)
    name: Mapped[str | None] = mapped_column(String(256))
    total_cases: Mapped[int] = mapped_column(Integer, nullable=False, server_default=text("0"))
    accuracy: Mapped[Decimal | None] = mapped_column(Numeric(6, 4))
    recall_hit_rate: Mapped[Decimal | None] = mapped_column(Numeric(6, 4))
    execute_success_rate: Mapped[Decimal | None] = mapped_column(Numeric(6, 4))
    avg_duration_ms: Mapped[int | None] = mapped_column(Integer)
    detail: Mapped[dict | None] = mapped_column(JSON)
    status: Mapped[str | None] = mapped_column(String(32))
    created_at: Mapped[datetime] = mapped_column(DateTime, nullable=False, default=datetime.now)
