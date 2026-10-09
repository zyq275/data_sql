"""元数据知识库读取（节点用的只读网关）。

放在 agent 层而不是 repositories：repository 只做单表存取（TDD 4 分层约束），
而这里要把 ``table_info`` 与 ``column_info`` 拼成"带外键指向的表结构"，还要把
召回出来的字段 id 反查回它所属的表——这是**取数逻辑**，不是存储逻辑。

每个方法自开一个短会话（``clients.mysql.session_scope``），不复用请求级会话：
图里三路召回是并行的，而 SQLAlchemy 的 ``AsyncSession`` 不是并发安全的。
"""

from __future__ import annotations

import logging
from collections.abc import Sequence
from dataclasses import dataclass, field

from sqlalchemy import select

from app.clients.mysql import session_scope
from app.models import ColumnInfo, MetricInfo, TableInfo

logger = logging.getLogger(__name__)


@dataclass(slots=True)
class ColumnSpec:
    """字段定义（含外键指向的**名字**，直接可用在提示词里）。"""

    name: str
    type: str
    alias: str | None
    description: str | None
    is_primary_key: bool
    is_foreign_key: bool
    ref_table: str | None
    ref_column: str | None


@dataclass(slots=True)
class TableSpec:
    """表定义。"""

    db_name: str
    name: str
    alias: str | None
    description: str | None
    is_dimension: bool
    columns: list[ColumnSpec] = field(default_factory=list)


@dataclass(slots=True)
class MetricSpec:
    """指标口径。"""

    name: str
    alias: str | None
    description: str | None
    calculation: str | None
    filter_condition: str | None
    time_range: str | None


def _dedupe(values: Sequence[str]) -> list[str]:
    """去重且保持原顺序——顺序即相关度，不能靠 set 打乱。"""
    return list(dict.fromkeys(value for value in values if value))


class KnowledgeGateway:
    """知识库只读网关。"""

    @classmethod
    def default(cls) -> KnowledgeGateway:
        return cls()

    async def tables_for_columns(self, column_ids: Sequence[str]) -> list[TableSpec]:
        """由召回到的字段反查它所属的表，返回这些表的**完整**字段清单。

        给完整字段而不是只给召回命中的那几个：SQL 里 JOIN / GROUP BY 需要的字段
        未必出现在召回结果里——问"华东区销售额前三的省份"时召回会命中
        ``region_name``，而 SQL 真正要分组的是 ``province``。只给命中字段，
        模型就只能编一个出来。
        """
        ids = _dedupe(column_ids)
        if not ids:
            return []
        async with session_scope() as session:
            table_ids = (
                (await session.execute(select(ColumnInfo.table_id).where(ColumnInfo.id.in_(ids))))
                .scalars()
                .all()
            )
        return await self.tables(table_ids)

    async def tables(self, table_ids: Sequence[str]) -> list[TableSpec]:
        """按 id 取表结构，附带字段的外键指向。"""
        ids = _dedupe(table_ids)
        if not ids:
            return []

        async with session_scope() as session:
            table_rows = (
                (await session.execute(select(TableInfo).where(TableInfo.id.in_(ids))))
                .scalars()
                .all()
            )
            column_rows = (
                (
                    await session.execute(
                        select(ColumnInfo)
                        .where(ColumnInfo.table_id.in_(ids))
                        # 主键优先，其后按 id——库里没有声明顺序可依，这个次序
                        # 至少保证两次运行一致，且把主键摆在最显眼处
                        .order_by(
                            ColumnInfo.table_id,
                            ColumnInfo.is_primary_key.desc(),
                            ColumnInfo.id,
                        )
                    )
                )
                .scalars()
                .all()
            )
            ref_tables = await self._ref_table_names(session, column_rows)
            ref_columns = await self._ref_column_names(session, column_rows)

        by_id = {row.id: row for row in table_rows}
        grouped: dict[str, list[ColumnSpec]] = {tid: [] for tid in ids}
        for row in column_rows:
            grouped.setdefault(row.table_id, []).append(
                ColumnSpec(
                    name=row.column_name,
                    type=row.column_type or "",
                    alias=row.column_alias,
                    description=row.description,
                    is_primary_key=bool(row.is_primary_key),
                    is_foreign_key=bool(row.is_foreign_key),
                    ref_table=ref_tables.get(row.ref_table_id),
                    ref_column=ref_columns.get(row.ref_column_id),
                )
            )

        # 按传入顺序返回：调用方（filter_table）会把最相关的排在前面
        return [
            TableSpec(
                db_name=by_id[tid].db_name,
                name=by_id[tid].table_name,
                alias=by_id[tid].table_alias,
                description=by_id[tid].description,
                is_dimension=bool(by_id[tid].is_dimension),
                columns=grouped.get(tid, []),
            )
            for tid in ids
            if tid in by_id
        ]

    async def metrics(self, names: Sequence[str]) -> list[MetricSpec]:
        """按**指标名**取完整口径。

        filter_metric 的输出是指标名数组（TDD 6），所以这里按名字查；
        查不到的名字记日志——静默丢弃会让"指标筛选选了个不存在的名字"
        表现为"生成的 SQL 少了指标口径"，而看不出原因。
        """
        wanted = _dedupe(names)
        if not wanted:
            return []

        async with session_scope() as session:
            rows = (
                (await session.execute(select(MetricInfo).where(MetricInfo.metric_name.in_(wanted))))
                .scalars()
                .all()
            )

        by_name = {row.metric_name: row for row in rows}
        missing = [name for name in wanted if name not in by_name]
        if missing:
            logger.warning("指标筛选结果不在知识库中，已忽略：%s", missing)

        return [
            MetricSpec(
                name=row.metric_name,
                alias=row.metric_alias,
                description=row.description,
                calculation=row.calculation,
                filter_condition=row.filter_condition,
                time_range=row.time_range,
            )
            for row in (by_name[name] for name in wanted if name in by_name)
        ]

    @staticmethod
    async def _ref_table_names(session, column_rows) -> dict[str, str]:
        """把字段上的 ``ref_table_id`` 解析成表名。

        外键指向的表可能不在本次选中的表里（``fact_order`` 指向 ``dim_region``，
        而召回只选中了 ``fact_order``），所以必须单独查一次，不能从已取的行里找。

        入参一律是**字段行**：外键信息长在 ``ColumnInfo`` 上，``TableInfo``
        没有 ``ref_*`` 字段。这两个解析方法曾经共用一份按 model 分支的实现，
        调用方在"解析表名"那一支顺手传了表行——表行上没有那个属性，
        直到真的取到一张带外键的表才炸。拆成两个各自只收字段行的函数，
        调用方就没得选错。
        """
        wanted = {row.ref_table_id for row in column_rows if row.ref_table_id}
        if not wanted:
            return {}
        result = await session.execute(
            select(TableInfo.id, TableInfo.table_name).where(TableInfo.id.in_(wanted))
        )
        return dict(result.all())

    @staticmethod
    async def _ref_column_names(session, column_rows) -> dict[str, str]:
        """把字段上的 ``ref_column_id`` 解析成字段名。"""
        wanted = {row.ref_column_id for row in column_rows if row.ref_column_id}
        if not wanted:
            return {}
        result = await session.execute(
            select(ColumnInfo.id, ColumnInfo.column_name).where(ColumnInfo.id.in_(wanted))
        )
        return dict(result.all())
