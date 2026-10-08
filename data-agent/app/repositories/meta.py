"""元数据知识库存取（TDD 8.1 的 table_info / column_info / metric_info / column_metric）。

只做读写，不做规则判断（TDD 4 分层约束）：配置校验在 entities.meta_config，
构建编排与存储间的一致性自检在 scripts.build_meta。

构建采用「全量重建」：先清空四张知识表再写入。这四张表完全由 meta_config.yaml
派生，逐条 upsert 反而要处理「配置里删掉的表怎么清理」这类边界，收益不大。
主键用 uuid5 由自然键确定性生成（见 entities.meta_config），因此重建后同一张表
/字段/指标的 id 不变——permission.resource_id 指向表字段时不会失效。
反过来，**从配置里彻底删除表或字段**会让指向它的 permission 变成悬空引用，
此时需要一并清理权限数据。
"""

from __future__ import annotations

from collections.abc import Sequence

from sqlalchemy import delete, func, insert, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models import ColumnInfo, ColumnMetric, MetricInfo, TableInfo

BATCH_SIZE = 500


class MetaRepository:
    """知识库存储访问。"""

    def __init__(self, session: AsyncSession) -> None:
        self.session = session

    async def clear(self) -> dict[str, int]:
        """清空四张知识表，返回各表被清掉的行数（按外键依赖顺序删）。"""
        removed: dict[str, int] = {}
        for model in (ColumnMetric, ColumnInfo, MetricInfo, TableInfo):
            result = await self.session.execute(delete(model))
            removed[model.__tablename__] = result.rowcount or 0
        return removed

    async def insert_tables(self, rows: Sequence[dict]) -> None:
        await self._bulk_insert(TableInfo, rows)

    async def insert_columns(self, rows: Sequence[dict]) -> None:
        await self._bulk_insert(ColumnInfo, rows)

    async def insert_metrics(self, rows: Sequence[dict]) -> None:
        await self._bulk_insert(MetricInfo, rows)

    async def insert_column_metrics(self, rows: Sequence[dict]) -> None:
        await self._bulk_insert(ColumnMetric, rows)

    async def _bulk_insert(self, model: type, rows: Sequence[dict]) -> None:
        if not rows:
            return
        for start in range(0, len(rows), BATCH_SIZE):
            chunk = rows[start:start + BATCH_SIZE]
            await self.session.execute(insert(model), list(chunk))

    async def counts(self) -> dict[str, int]:
        """各表行数，用于构建后的入库自检。"""
        out: dict[str, int] = {}
        for model in (TableInfo, ColumnInfo, MetricInfo, ColumnMetric):
            out[model.__tablename__] = (
                await self.session.execute(select(func.count()).select_from(model))
            ).scalar_one()
        return out
