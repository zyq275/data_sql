"""数据看板取数（FR-DASH-001~002，TDD 7.2 dashboard 组）。

**看板不走模型，按指标口径拼模板 SQL**。理由是同一条问题在两处必须有同一个答案：
看板上的人要的是"一个确定的数"，让它去经过一次模型生成，同一个问题两次刷新可能
给出两个数，而这类差异没有人能解释。所以这里做的是**确定性拼装**：聚合表达式取
``metric_info.calculation``、过滤谓词取 ``metric_info.filter_sql``、维度取下面那张
白名单表，全是模板填空。

**没有 filter_sql 的指标会被挡在门外**（而不是按不过滤算）：散文形式的
``filter_condition`` 程序读不懂，硬去解析必然有解析错的那一天，而错的那天给出的
是一个与问答链路口径不一致的数字——两个数字都"看着正常"，没有人会对账。
确实不需要过滤的指标写 ``filter_sql: ""`` 明说（``None`` 是漏写，``""`` 是想清楚了）。

**权限有两道**（与阶段 9 同构）：拼装前按角色可见范围挑维度、挑指标、挑事实表与
关联表；拼完再拿 ``PermissionScope.violations`` 兜底核一遍整句 SQL。前一道保证
正常路径下模型/配置看不到越权名字，后一道保证**知识库里写错的口径**（比如某个
指标的 calculation 引用了未登记的字段）也过不去。
"""

from __future__ import annotations

import logging
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import date

from sqlalchemy.ext.asyncio import AsyncSession

from app.agent.permissions import PermissionGateway, PermissionScope
from app.core.exceptions import PermissionDeniedError, ServiceError, ValidationError
from app.core.sql_security import SQLSecurityError, assert_readonly
from app.models import ColumnInfo, MetricInfo, TableInfo
from app.repositories.dw import DwRepository
from app.repositories.meta import MetaRepository
from app.schemas.dashboard import (
    DashboardDimensionOut,
    DashboardDimensionsResponse,
    DashboardMetricOut,
    DashboardMetricsResponse,
)

logger = logging.getLogger(__name__)

TIME = "time"
CATEGORY = "category"


@dataclass(frozen=True, slots=True)
class Dimension:
    """一个可用于聚合的维度。"""

    table: str
    column: str
    kind: str

    @property
    def expr(self) -> str:
        """写进 SQL 的表达式，始终带表名限定（字段名在数仓里不唯一）。"""
        return f"{self.table}.{self.column}"

    @property
    def alias(self) -> str:
        return self.column


#: 看板可用的维度白名单。
#:
#: **哪些字段算"看板维度"是判断，不是事实**，所以它写在代码里而不是知识库里：
#: 知识库回答"库里有什么字段"，而这里回答"哪些字段适合做业务分组"——每张维表都有
#: 主键，把 ``region_id`` 摆进下拉框对业务人员没有任何意义。要不要把它挪进配置，
#: 等真正有人需要按自己的口径定制时再谈；现在就做成配置项，只会多一张没人看的表。
#:
#: 名字（alias）仍从 ``column_info.column_alias`` 取，知识库依旧是"字段叫什么"的
#: 唯一来源，这里只圈范围。
DIMENSIONS: tuple[Dimension, ...] = (
    Dimension("dim_date", "year", TIME),
    Dimension("dim_date", "quarter", TIME),
    Dimension("dim_date", "month", TIME),
    Dimension("dim_date", "day", TIME),
    Dimension("dim_region", "province", CATEGORY),
    Dimension("dim_region", "region_name", CATEGORY),
    Dimension("dim_product", "category", CATEGORY),
    Dimension("dim_product", "brand", CATEGORY),
    Dimension("dim_customer", "gender", CATEGORY),
    Dimension("dim_customer", "member_level", CATEGORY),
)

#: 名 → 维度。上表里的列名不重复（这是它能被当成对外名字的前提），
#: 重复了会在这里直接暴露成"后一个把前一个顶掉"。
DIMENSION_BY_NAME: dict[str, Dimension] = {d.column: d for d in DIMENSIONS}


@dataclass(frozen=True, slots=True)
class Join:
    """一条 JOIN：目标表、ON 条件、以及目标表上参与关联的那个字段。"""

    table: str
    on: str
    dim_key: str


class DashboardService:
    """看板的维度清单与聚合取数。"""

    def __init__(self, session: AsyncSession) -> None:
        self.session = session
        self.meta = MetaRepository(session)
        self.dw = DwRepository.default()

    # ---- 维度与指标清单 ---------------------------------------------------

    async def dimensions(self, role: str) -> DashboardDimensionsResponse:
        """当前角色可用的维度与指标（FR-DASH-001）。

        清单也要按权限裁：把这个角色的下拉框填满它根本查不了的维度，用户选一次
        撞一次 403，而"看得到但用不了"比"看不到"更让人困惑。
        """
        scope = await PermissionGateway.default().scope_for(role)
        columns = await self.meta.columns()
        metrics = await self.meta.metrics()
        table_names = {row.id: row.table_name for row in await self.meta.tables()}

        known = {
            (table_names.get(row.table_id, ""), row.column_name): row.column_alias
            for row in columns
        }
        return DashboardDimensionsResponse(
            dimensions=[
                DashboardDimensionOut(
                    name=d.column,
                    alias=known.get((d.table, d.column)),
                    table=d.table,
                    kind=d.kind,
                )
                for d in DIMENSIONS
                # 既要在白名单里，也要真的存在于知识库，还要在权限范围里
                if (d.table, d.column) in known and scope.allows_column(d.table, d.column)
            ],
            metrics=[
                DashboardMetricOut(
                    name=row.metric_name,
                    alias=row.metric_alias,
                    description=row.description,
                )
                for row in metrics
                if scope.allows_metric(row.metric_name)
            ],
        )

    # ---- 聚合取数 ---------------------------------------------------------

    async def metrics(
        self,
        role: str,
        *,
        dimension: str,
        metrics: str | None = None,
        start: str | None = None,
        end: str | None = None,
    ) -> DashboardMetricsResponse:
        """按维度与时间范围聚合出指标（FR-DASH-001）。"""
        scope = await PermissionGateway.default().scope_for(role)
        tables = await self.meta.tables()
        columns = await self.meta.columns()
        metric_rows = await self.meta.metrics()

        table_name = {row.id: row.table_name for row in tables}
        column_table = {row.id: table_name.get(row.table_id, "") for row in columns}
        metric_tables: dict[str, set[str]] = {}
        for link in await self.meta.column_metrics():
            touched = column_table.get(link.column_id)
            if touched:
                metric_tables.setdefault(link.metric_id, set()).add(touched)

        dims = _parse_dimensions(dimension)
        chosen = _select_metrics(metrics, metric_rows, scope)
        for d in dims:
            # 消息里点名用户**自己请求的**那个维度名：这不是泄露（名字是他给的），
            # 不点名则用户只能靠猜是哪一项被拒了——与阶段 9 的通用提示不同，
            # 那里被点名的名字是模型自己编出来的，用户从没见过
            if not scope.allows_column(d.table, d.column):
                raise PermissionDeniedError(f"当前角色无权使用维度：{d.column}")

        fact = _fact_table(chosen, tables, metric_tables)
        joins = self._joins(fact, dims, bool(start or end), tables, columns, scope)
        predicate = _predicate(chosen)

        assembled = _assemble(
            fact=fact,
            metrics=chosen,
            dims=dims,
            joins=joins,
            predicate=predicate,
            date_range=_date_range(joins, start, end),
        )
        # 拼出来的这句话马上要送去数仓，一样要过只读闸门：它由知识库里的
        # calculation / filter_sql 拼成，而"配置写歪了"是真实存在的一种输入
        try:
            sql = assert_readonly(assembled)
        except SQLSecurityError as exc:
            logger.warning("看板拼出的 SQL 未通过只读校验：%s（%s）", exc, assembled)
            raise ServiceError("看板查询语句不合法，请联系数据团队") from exc

        # 兜底：拼装前的挑拣只覆盖"表与字段"，覆盖不了知识库里写歪的口径
        # （比如某个指标的 calculation 引用了没登记的字段）。这句话马上要送去数仓，
        # 在它出门之前再核一遍，代价是一次正则扫描。
        violations = scope.violations(sql)
        if violations:
            logger.warning("看板 SQL 触及当前角色未授权的对象：%s", violations)
            raise PermissionDeniedError("当前角色的数据权限不足以完成这次看板查询")

        result = await self.dw.execute(sql)
        return DashboardMetricsResponse(
            dimensions=[d.alias for d in dims],
            columns=result.columns,
            rows=result.rows,
            row_count=len(result.rows),
            truncated=result.truncated,
            metrics=[
                DashboardMetricOut(
                    name=row.metric_name, alias=row.metric_alias, description=row.description
                )
                for row in chosen
            ],
            sql=sql,
            unrestricted=scope.unrestricted,
        )

    def _joins(
        self,
        fact: str,
        dims: list[Dimension],
        has_range: bool,
        tables: Sequence[TableInfo],
        columns: Sequence[ColumnInfo],
        scope: PermissionScope,
    ) -> list[Join]:
        """事实表到各维度表的关联，全部由 ``column_info`` 的外键元数据推出来。

        不写死 JOIN 条件：数仓加了张维表、或者把外键挪了个位置，写死的那份不会报错，
        只会悄悄 JOIN 错，然后给出一个看着正常的数。外键元数据是知识库的一部分，
        它错了会在别处（召回补齐关联）也一起错，一处错两处看得见。
        """
        table_name = {row.id: row.table_name for row in tables}
        column_name = {row.id: row.column_name for row in columns}
        fact_id = next((row.id for row in tables if row.table_name == fact), None)

        wanted = {d.table for d in dims} - {fact}
        if has_range:
            # 时间范围一律落在日期维度上，即使一个时间维度都没选——
            # "按地区看 GMV 的月度趋势"与"看某个时间段内各地区的 GMV"是两种问法
            wanted.add("dim_date")
        if "dim_date" in wanted and not scope.allows_table("dim_date"):
            raise PermissionDeniedError("当前角色无权使用日期维度，无法按时间范围筛选")

        joins: list[Join] = []
        for target in sorted(wanted):
            found = next(
                (
                    row
                    for row in columns
                    if row.table_id == fact_id and table_name.get(row.ref_table_id) == target
                ),
                None,
            )
            if found is None:
                raise ValidationError(f"无法把维度表 {target} 与事实表 {fact} 关联起来")
            key = column_name.get(found.ref_column_id, "")
            joins.append(
                Join(
                    table=target,
                    on=f"{fact}.{found.column_name} = {target}.{key}",
                    dim_key=f"{target}.{key}",
                )
            )
        return joins


# ==========================================================================
# 纯函数：解析与拼装（脱离数据库可测的部分）
# ==========================================================================


def _parse_dimensions(raw: str) -> list[Dimension]:
    """``year,month`` → 维度列表（保序去重）。"""
    names = [part.strip() for part in (raw or "").split(",") if part.strip()]
    if not names:
        raise ValidationError("必须指定至少一个聚合维度")
    dims: list[Dimension] = []
    for name in names:
        found = DIMENSION_BY_NAME.get(name)
        if found is None:
            raise ValidationError(f"不支持的聚合维度：{name}")
        if found not in dims:
            dims.append(found)
    aliases = [d.alias for d in dims]
    if len(set(aliases)) != len(aliases):
        # 两个维度撞了同一个列名，结果列就分不清哪个是哪个了
        raise ValidationError(f"所选维度的列名重复：{aliases}")
    return dims


def _select_metrics(
    raw: str | None, rows: Sequence[MetricInfo], scope: PermissionScope
) -> list[MetricInfo]:
    """请求的指标名 → 指标行；没请求就给全部可见指标。"""
    by_name = {row.metric_name: row for row in rows}
    if raw is None:
        chosen = [row for row in rows if scope.allows_metric(row.metric_name)]
    else:
        names = [part.strip() for part in raw.split(",") if part.strip()]
        unknown = [name for name in names if name not in by_name]
        if unknown:
            raise ValidationError(f"未知的指标：{unknown[0]}")
        chosen = [by_name[name] for name in dict.fromkeys(names)]

    denied = [row.metric_name for row in chosen if not scope.allows_metric(row.metric_name)]
    if denied:
        raise PermissionDeniedError(f"当前角色无权使用指标：{'、'.join(denied)}")
    if not chosen:
        raise ValidationError("没有可用的指标")
    return chosen


def _fact_table(
    chosen: Sequence[MetricInfo],
    tables: Sequence[TableInfo],
    metric_tables: dict[str, set[str]],
) -> str:
    """所选指标共用的事实表。

    事实表由 ``column_metric`` 关联出来，**不去解析 ``calculation`` 的文本**：
    ``SUM(fact_order.order_amount)`` 里的 fact_order 出现在字段限定位置而不是 FROM
    后面，文本解析只认得后者，于是"这张表的字段要算进去"这条事实在解析那条路上
    直接丢了。关联表是知识库自己的声明，既不依赖写法，也不会因为有人给口径加个
    括号就变样。

    只接受**一张**事实表：指标 A 在订单上、指标 B 在浏览上时，把它们放进同一条
    SELECT 需要先把两张事实表按维度对齐，那是另一件事（且有口径风险）——
    与其悄悄按其中一张算，不如直接说不支持。
    """
    dimensions = {row.table_name for row in tables if row.is_dimension}
    facts: set[str] = set()
    for row in chosen:
        facts |= metric_tables.get(row.id, set()) - dimensions
    if not facts:
        raise ValidationError("所选指标的关联字段里没有可识别的事实表")
    if len(facts) > 1:
        raise ValidationError(f"所选指标不属于同一张事实表：{'、'.join(sorted(facts))}")
    return facts.pop()


def _predicate(chosen: Sequence[MetricInfo]) -> str | None:
    """过滤谓词：所有所选指标必须是同一口径，且都已声明口径。

    混着用是不行的——一条 WHERE 对所有指标生效，本该过滤的会多过滤一次、
    不该过滤的会被一起过滤掉，而结果看起来只是"数小了点"。

    **没声明口径的指标直接拒掉，而不是按不过滤算**。按不过滤算会给出一个与问答链路
    口径不一致的数字，而那个数字看着完全正常——两个数都"正常"时，没有人会去对账，
    于是这次的错会一直留着。拒掉至少会有人来问一句"为什么看板上没有这个指标"。
    确实不需要过滤的指标（比如累计注册用户数）写 ``filter_sql: ""`` 明说，
    与"忘了写"区分开：``None`` 是漏写，``""`` 是想清楚了。
    """
    missing = [row.metric_name for row in chosen if row.filter_sql is None]
    if missing:
        raise ValidationError(
            f"指标 {'、'.join(missing)} 没有配置过滤口径（filter_sql），"
            "看板无法保证与问答链路报出来的是同一个数"
        )
    values = {row.filter_sql for row in chosen}
    if len(values) > 1:
        raise ValidationError("所选指标的过滤口径不一致，无法在同一次查询里聚合")
    return values.pop() or None


def _date_range(joins: list[Join], start: str | None, end: str | None) -> str | None:
    """时间范围 → 谓词；同时给定时用 BETWEEN。"""
    if not start and not end:
        return None
    key = next((join.dim_key for join in joins if join.table == "dim_date"), "dim_date.date_id")
    if start and end:
        return f"{key} BETWEEN {_date_id(start)} AND {_date_id(end)}"
    if start:
        return f"{key} >= {_date_id(start)}"
    return f"{key} <= {_date_id(end)}"


def _date_id(value: str) -> int:
    """``YYYY-MM-DD`` / ``YYYYMMDD`` → ``dim_date.date_id`` 的整数形式。

    自己解析而不是 ``date.fromisoformat``：要同时收两种写法，而 ``date_id`` 的
    口径（``yyyymmdd``）在知识库里写得很清楚，转成整数才能和它对上。
    """
    raw = value.strip().replace("-", "").replace("/", "")
    if len(raw) != 8 or not raw.isdigit():
        raise ValidationError(f"时间范围格式应为 YYYY-MM-DD：{value}")
    try:
        date(int(raw[:4]), int(raw[4:6]), int(raw[6:]))
    except ValueError as exc:
        raise ValidationError(f"时间范围不是合法日期：{value}") from exc
    return int(raw)


def _assemble(
    *,
    fact: str,
    metrics: Sequence[MetricInfo],
    dims: Sequence[Dimension],
    joins: Sequence[Join],
    predicate: str | None,
    date_range: str | None,
) -> str:
    """把各部分拼成一句 SELECT。

    别名用反引号括起来：``year`` / ``month`` 这些名字同时也是 MySQL 的函数名，
    不括起来时 ``AS month`` 会被解析成函数调用的位置，报的还是个语法错误。
    """
    selects = [f"{d.expr} AS `{d.alias}`" for d in dims] + [
        f"{row.calculation} AS `{row.metric_name}`" for row in metrics
    ]
    lines = [f"SELECT {', '.join(selects)}", f"FROM {fact}"]
    lines += [f"JOIN {join.table} ON {join.on}" for join in joins]

    conditions = [part for part in (predicate, date_range) if part]
    if conditions:
        lines.append("WHERE " + " AND ".join(conditions))
    grouping = ", ".join(d.expr for d in dims)
    lines.append(f"GROUP BY {grouping}")
    # 按维度的值排序：折线图的连线顺序由它决定，让数据库按自己的喜好返回，
    # 同一个查询两次刷新可能画出走势完全不同的两张图
    lines.append(f"ORDER BY {grouping}")
    return "\n".join(lines)


__all__ = [
    "DIMENSIONS",
    "DashboardService",
    "Dimension",
]
