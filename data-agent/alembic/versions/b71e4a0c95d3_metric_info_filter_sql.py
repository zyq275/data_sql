"""metric_info 增加 filter_sql

阶段 10 的数据看板按指标口径拼模板 SQL，需要过滤口径的**可执行形式**。
``metric_info.filter_condition`` 里存的是给模型读的散文（"仅统计 order_status =
'已支付' 的订单，排除已取消与已退款"），程序读不懂，也不能去解析——解析散文
得到的谓词一旦解析错，看板会给出一个与问答链路口径不一致的数字，而两个数字
都"看着正常"，没人会对账。

于是两条并行：散文继续进提示词与向量文本，新增的 ``filter_sql`` 存
``fact_order.order_status = '已支付'`` 这样的谓词，进看板的 WHERE 子句。

纯增列、可空，对既有数据与既有链路没有任何影响；构建知识库
（``python -m app.scripts.build_meta``）后该列才有值，此前为空只意味着
"这些指标暂时不可用于数据看板"。

Revision ID: b71e4a0c95d3
Revises: f02dca8a316e
Create Date: 2026-10-10 15:20:11.000000

"""
from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op


revision: str = 'b71e4a0c95d3'
down_revision: str | None = 'f02dca8a316e'
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column('metric_info', sa.Column('filter_sql', sa.Text(), nullable=True))


def downgrade() -> None:
    op.drop_column('metric_info', 'filter_sql')
