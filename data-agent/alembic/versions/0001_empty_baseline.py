"""empty baseline

建立迁移基线，不建任何表；meta 库的 11 张表在阶段 2 的迁移中创建（TDD 第 8 章）。

Revision ID: 0001
Revises:
Create Date: 2026-10-08

"""
from __future__ import annotations

from collections.abc import Sequence

revision: str = "0001"
down_revision: str | None = None
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    pass


def downgrade() -> None:
    pass
