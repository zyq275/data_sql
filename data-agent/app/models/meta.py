"""元数据知识库（TDD 8.1 table_info / column_info / metric_info / column_metric）。

这四张表由离线构建脚本（阶段 4，app/scripts）从 conf/meta_config.yaml 灌入，
是混合检索与 SQL 生成的知识来源。
"""

from __future__ import annotations

from datetime import datetime

from sqlalchemy import Boolean, DateTime, Index, PrimaryKeyConstraint, String, Text, UniqueConstraint, text
from sqlalchemy.orm import Mapped, mapped_column

from app.models.base import Base


class TableInfo(Base):
    """表元数据。"""

    __tablename__ = "table_info"
    __table_args__ = (UniqueConstraint("db_name", "table_name", name="uk_db_table"),)

    id: Mapped[str] = mapped_column(String(64), primary_key=True)
    db_name: Mapped[str] = mapped_column(String(64), nullable=False)
    table_name: Mapped[str] = mapped_column(String(128), nullable=False)
    table_alias: Mapped[str | None] = mapped_column(String(128))
    description: Mapped[str | None] = mapped_column(Text)
    is_dimension: Mapped[bool | None] = mapped_column(Boolean, server_default=text("0"))
    created_at: Mapped[datetime] = mapped_column(DateTime, nullable=False, default=datetime.now)


class ColumnInfo(Base):
    """字段元数据。

    主外键信息用于召回后补齐关联关系（BR-META-001）：
    外键字段通过 ref_table_id / ref_column_id 指向被引用方。
    """

    __tablename__ = "column_info"
    __table_args__ = (Index("idx_table_id", "table_id"),)

    id: Mapped[str] = mapped_column(String(64), primary_key=True)
    table_id: Mapped[str] = mapped_column(String(64), nullable=False)
    column_name: Mapped[str] = mapped_column(String(128), nullable=False)
    column_type: Mapped[str | None] = mapped_column(String(64))
    column_alias: Mapped[str | None] = mapped_column(String(128))
    description: Mapped[str | None] = mapped_column(Text)
    is_primary_key: Mapped[bool | None] = mapped_column(Boolean, server_default=text("0"))
    is_foreign_key: Mapped[bool | None] = mapped_column(Boolean, server_default=text("0"))
    ref_table_id: Mapped[str | None] = mapped_column(String(64))
    ref_column_id: Mapped[str | None] = mapped_column(String(64))
    created_at: Mapped[datetime] = mapped_column(DateTime, nullable=False, default=datetime.now)


class MetricInfo(Base):
    """指标元数据。

    calculation / filter_condition / time_range 共同构成业务口径，
    生成 SQL 时必须严格遵循，不得偏离（BR-META-002、TDD 6 提示词设计原则）。
    relevant_columns 存 column_id 列表，用于指标相关字段一并召回。
    """

    __tablename__ = "metric_info"

    id: Mapped[str] = mapped_column(String(64), primary_key=True)
    metric_name: Mapped[str] = mapped_column(String(128), nullable=False)
    metric_alias: Mapped[str | None] = mapped_column(String(128))
    description: Mapped[str | None] = mapped_column(Text)
    calculation: Mapped[str | None] = mapped_column(Text)
    filter_condition: Mapped[str | None] = mapped_column(Text)
    filter_sql: Mapped[str | None] = mapped_column(Text)
    """过滤口径的 SQL 谓词形式（阶段 10 起）。

    与 ``filter_condition`` 并存：那条是给模型读的口径说明，这条是给看板拼进
    WHERE 子句的谓词。看板走模板 SQL，没有"读懂散文"这一步，所以两者不能合并。
    """
    time_range: Mapped[str | None] = mapped_column(String(128))
    relevant_columns: Mapped[str | None] = mapped_column(Text)
    created_at: Mapped[datetime] = mapped_column(DateTime, nullable=False, default=datetime.now)


class ColumnMetric(Base):
    """字段与指标的多对多关联。"""

    __tablename__ = "column_metric"
    __table_args__ = (
        PrimaryKeyConstraint("column_id", "metric_id"),
        Index("idx_metric_id", "metric_id"),
    )

    column_id: Mapped[str] = mapped_column(String(64))
    metric_id: Mapped[str] = mapped_column(String(64))
