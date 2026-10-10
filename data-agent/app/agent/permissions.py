"""数据权限（TDD 5.1 的上下文注入点，FR-PERM-001 / BR-PERM-001）。

**权限只有一层，就是那张 permission 表**：某角色有哪些 table / column 行，
就只用得了那些表与字段。没有第二套规则（没有"admin 天然全通"、没有角色继承），
这样"某个角色能看什么"永远只有一个地方能回答。

**没有配置的角色视为不设限**（``unrestricted``）：全新环境里 permission 表是空的，
若按白名单严格解读，则所有角色、包括管理员的每一次提问都取不到任何一张表——
一个刚部署完、还没人配过权限的系统不该是这个样子。一旦某角色有了哪怕一条记录，
就按白名单严格执行，所以"开始配置"这个动作本身就是启用的开关。这条取舍在
README 与 TDD 里都写明了，是**可配置语义下最不该被默默假设**的一处。

**它是只读网关，不是校验器**：这里回答"这个角色能不能看这张表这个字段"，
至于"生成的 SQL 里有没有越权引用"由 ``validate_sql`` 用这里给出的
``forbidden_tables`` / ``forbidden_columns`` 去判——判定所需的事实由本模块提供，
判定发生在离 SQL 最近的地方。
"""

from __future__ import annotations

import logging
from collections.abc import Sequence
from dataclasses import dataclass, replace

from sqlalchemy import select

from app.agent.knowledge import TableSpec
from app.clients.mysql import session_scope
from app.core.sql_security import referenced_names, referenced_tables
from app.models import ColumnInfo, ColumnMetric, MetricInfo, Permission, TableInfo

logger = logging.getLogger(__name__)

TABLE = "table"
COLUMN = "column"

#: 已经提醒过"未配置"的角色。同一条提醒只打一次（与 retrieval._warn_rerank_down 同一考虑）：
#: 未配置的角色每问一句都提醒一次，会把别的问题淹掉。
_warned_roles: set[str] = set()


def _warn_unconfigured(role: str) -> None:
    if role in _warned_roles:
        return
    _warned_roles.add(role)
    logger.warning("角色 %s 在 permission 表里没有任何配置，本次不限制可用表与字段", role)


@dataclass(slots=True, frozen=True)
class PermissionScope:
    """某个角色在**一次查询内**可见的表与字段。

    ``tables`` 把两件事压在一个映射里：键是可见表名，值是可见字段名集合，
    ``None`` 表示该表整个可用。用 ``None`` 而不是"列出全部字段名"，是因为
    "表级授权"与"字段级授权"在语义上确实不同——前者意味着这张表以后新增字段
    也自动可用（知识库重建后不必再配一遍），后者是明确圈定的若干列。

    ``forbidden_*`` 存的是**补集的名字**，供 ``validate_sql`` 做兜底校验用
    （那是唯一需要"反过来问"的地方：正着答"能不能看"在裁剪时用，
    反着答"这句话里有没有不该看的"在校验时用）。
    """

    role: str
    unrestricted: bool
    tables: dict[str, frozenset[str] | None]
    metrics: frozenset[str]
    forbidden_tables: frozenset[str]
    forbidden_columns: dict[str, frozenset[str]]
    """表名 → 该表里未授权的字段名（只含"整表授权但被字段级收窄"的那几张表）。

    按表分组而不是平铺成一个名字集合，因为**字段名在数仓里不唯一**：
    ``fact_order.product_id`` 与 ``dim_product.product_id`` 同名。平铺的话，
    "dim_product 只授了 brand"会让 ``SELECT product_id FROM fact_order`` 被判越权——
    而这句话碰的是一张已经整表授权的表，字段也确实属于它。模型改不掉这个问题
    （问题不在 SQL 上），连续校正几次之后整句失败，用户看到的是"这个问法不行"。

    未授权的整表不进这个映射：那种情况由 ``forbidden_tables`` 按表名直接拦下，
    再逐列报一遍只会让日志里同一件事写两遍。
    """

    @classmethod
    def everything(cls, role: str) -> PermissionScope:
        """不设限的范围。"""
        return cls(
            role=role,
            unrestricted=True,
            tables={},
            metrics=frozenset(),
            forbidden_tables=frozenset(),
            forbidden_columns={},
        )

    def allows_table(self, name: str) -> bool:
        return self.unrestricted or name in self.tables

    def allows_column(self, table: str, column: str) -> bool:
        if self.unrestricted:
            return True
        if table not in self.tables:
            return False
        allowed = self.tables[table]
        return allowed is None or column in allowed

    def allows_metric(self, name: str) -> bool:
        return self.unrestricted or name in self.metrics

    def filter_tables(self, tables: Sequence[TableSpec]) -> list[TableSpec]:
        """裁掉未授权的表与字段。

        **在提示词之前和之后各用一次**：前一次让模型根本看不到未授权的名字；
        后一次用来收口 ``filter_table._trim``——它无条件保留主外键（那是 JOIN 的
        结构性依赖），而权限的口径是"没授权的主外键也不给"，所以裁剪过的那一份
        必须再过一遍权限。两次调用是幂等的，第二次通常什么都不改。

        表被裁空时按"这张表不可用"处理，连表一起丢掉：留一张没有字段的表在
        提示词里，模型只会照着一个空壳去编字段名。
        """
        if self.unrestricted:
            return list(tables)
        kept: list[TableSpec] = []
        for table in tables:
            if table.name not in self.tables:
                continue
            allowed = self.tables[table.name]
            if allowed is None:
                kept.append(table)
                continue
            columns = [c for c in table.columns if c.name in allowed]
            if columns:
                kept.append(replace(table, columns=columns))
        return kept

    def violations(self, sql: str | None) -> list[str]:
        """指出 SQL 里引用了哪些越权的表与字段；没有越权时返回空列表。

        **先拿语句里出现的表名求交集**，而不是拿一份"全库表名"去比对：SQL 里的
        CTE 名、别名、写错的表名都不在已知表名里，于是自然不会被算成越权
        （见 ``sql_security.referenced_tables``）。

        **字段按表算**，见 ``forbidden_columns`` 的说明——这正是它不能平铺成一个
        名字集合的原因。整表就已经越权的，只报表名不报字段：那种情况下表名本身
        已经说明问题，再逐列列一遍只会让日志膨胀。

        返回的是名字本身，供日志使用——**对用户的那句话里不带名字**（见 validate_sql）。
        """
        if self.unrestricted or not sql:
            return []
        tables = referenced_tables(sql)
        banned = set(tables & self.forbidden_tables)
        names = referenced_names(sql)
        for table in tables - banned:
            banned |= names & self.forbidden_columns.get(table, frozenset())
        return sorted(banned)


class PermissionGateway:
    """按角色算出可见范围（元数据库只读）。

    每个方法自开短会话，理由同 ``KnowledgeGateway``：图里 filter_table 与
    filter_metric 是并行的，而 ``AsyncSession`` 不是并发安全的。

    算出来的范围在**实例内**按角色缓存：一个 ``QueryContext`` 一个实例、一轮问答
    一个实例，所以缓存的生命周期正好是"一轮问答"。权限是低频变更的配置，
    一轮问答里查两次表没有任何意义，而这一轮内三处调用（表、指标、兜底校验）
    必须看到同一个范围——否则会出现"表被拦了、校验却放行"这种自相矛盾的中间态。
    """

    def __init__(self) -> None:
        self._cache: dict[str, PermissionScope] = {}

    @classmethod
    def default(cls) -> PermissionGateway:
        return cls()

    async def scope_for(self, role: str) -> PermissionScope:
        """角色 → 可见范围。角色没有任何配置时返回不设限的范围。"""
        cached = self._cache.get(role)
        if cached is not None:
            return cached
        scope = await self._load(role)
        self._cache[role] = scope
        return scope

    async def _load(self, role: str) -> PermissionScope:
        async with session_scope() as session:
            granted = (
                (await session.execute(select(Permission).where(Permission.role == role)))
                .scalars()
                .all()
            )
            if not granted:
                _warn_unconfigured(role)
                return PermissionScope.everything(role)

            table_rows = (await session.execute(select(TableInfo))).scalars().all()
            column_rows = (await session.execute(select(ColumnInfo))).scalars().all()
            metric_rows = (await session.execute(select(MetricInfo))).scalars().all()
            links = (
                (await session.execute(select(ColumnMetric.column_id, ColumnMetric.metric_id))).all()
            )

        # 只取需要的列再交给纯函数：判范围的逻辑与 ORM 无关，才能脱离数据库测
        return build_scope(
            role,
            granted=[(row.resource_type, row.resource_id) for row in granted],
            tables=[(row.id, row.table_name) for row in table_rows],
            columns=[(row.id, row.table_id, row.column_name) for row in column_rows],
            metrics=[(row.id, row.metric_name) for row in metric_rows],
            links=[(column_id, metric_id) for column_id, metric_id in links],
        )


def build_scope(
    role: str,
    *,
    granted: Sequence[tuple[str, str]],
    tables: Sequence[tuple[str, str]],
    columns: Sequence[tuple[str, str, str]],
    metrics: Sequence[tuple[str, str]],
    links: Sequence[tuple[str, str]],
) -> PermissionScope:
    """由权限行与元数据算出可见范围（纯函数，不碰数据库）。

    四个入参都是 ``(id, ...)`` 形式的简单元组而不是 ORM 行：这段逻辑要能在没有
    数据库的机器上逐条验（它是整个权限特性的判定核心，最不该只靠"起了 MySQL 才能测"）。
    网关那边的适配只有一行列表推导。

    参数含义：``granted`` 是 ``(resource_type, resource_id)``；
    ``tables`` 是 ``(id, name)``；``columns`` 是 ``(id, table_id, name)``；
    ``metrics`` 是 ``(id, name)``；``links`` 是 ``(column_id, metric_id)``。
    """
    granted_tables = {resource_id for kind, resource_id in granted if kind == TABLE}
    granted_columns = {resource_id for kind, resource_id in granted if kind == COLUMN}

    table_names = dict(tables)
    column_names = {column_id: name for column_id, _table_id, name in columns}
    column_tables = {column_id: table_id for column_id, table_id, _name in columns}

    # 授权表 = 显式授权的表 ∪ 有被授权字段的表。后者不能漏：只授了
    # dim_region.region_name 而没授整表时，dim_region 仍然是可用的表。
    authorized = granted_tables | {
        column_tables[column_id] for column_id in granted_columns if column_id in column_tables
    }

    # 一张表只要有字段级授权，就以字段级为准（收窄）；一条字段行都没有的授权表，
    # 视为整表可用。这样"先授整表、再收窄到几列"是能表达出来的。
    columns_by_table: dict[str, set[str]] = {}
    for column_id in granted_columns:
        table_id = column_tables.get(column_id)
        if table_id is not None:
            columns_by_table.setdefault(table_id, set()).add(column_id)

    scope_tables: dict[str, frozenset[str] | None] = {}
    for table_id in authorized:
        name = table_names.get(table_id)
        if name is None:
            # 悬空引用：权限行指向的表已不在元数据里（重建知识库时删过表）。
            # 丢掉而不是崩：下一次按元数据重新计算即可，记一条日志足够定位。
            logger.warning("权限引用了元数据里不存在的表 id=%s，已忽略", table_id)
            continue
        ids = columns_by_table.get(table_id)
        scope_tables[name] = (
            None if not ids else frozenset(column_names[cid] for cid in ids if cid in column_names)
        )

    # 已授权字段 = 显式授的列 ∪ 整表授权那些表的全部列（后者用于判定指标是否可见）
    authorized_columns = set(granted_columns)
    for table_id in authorized:
        if table_id not in columns_by_table:
            authorized_columns.update(
                column_id for column_id, tid, _name in columns if tid == table_id
            )

    # 收窄过的表里，哪些字段不许用。只算这几张表：整表可用的没有不许的字段，
    # 整表不可用的由 forbidden_tables 按表名拦下（见 violations）。
    forbidden_columns: dict[str, frozenset[str]] = {}
    for table_id, name in tables:
        allowed = scope_tables.get(name)
        if allowed is None:  # 整表可用，或这张表压根没被授权
            continue
        forbidden_columns[name] = frozenset(
            column_names[column_id]
            for column_id, tid, _name in columns
            if tid == table_id and column_id in column_names
        ) - allowed

    return PermissionScope(
        role=role,
        unrestricted=False,
        tables=scope_tables,
        metrics=_visible_metrics(metrics, links, authorized_columns),
        forbidden_tables=frozenset(
            name for table_id, name in tables if table_id not in authorized
        ),
        forbidden_columns=forbidden_columns,
    )


def _visible_metrics(
    metrics: Sequence[tuple[str, str]],
    links: Sequence[tuple[str, str]],
    authorized_columns: set[str],
) -> frozenset[str]:
    """指标可见的条件：它关联的每一个字段都被授权。

    口径按 ``column_metric``（构建期由 metric_info.relevant_columns 解析而来），
    不解析 ``calculation`` 里的表达式——那串文本是给模型看的业务口径，
    不是可解析的 SQL，从里面抠字段名只会抠错。后果是"计算口径里提到了某个越权
    字段、但没登记进 relevant_columns"的指标会被放行；这是知识库维护的责任，
    在 build_meta 的自检里管（TDD 8.1）。

    **一个字段都没关联的指标，在受限范围内一律不可见**：没有任何依据能证明它
    只碰已授权的字段，宁可不给。它同时是一条需要修的数据问题（知识库构建漏了
    relevant_columns），所以记 WARNING 而不是静默——指标凭空消失是最难查的一类故障。
    """
    by_metric: dict[str, list[str]] = {}
    for column_id, metric_id in links:
        by_metric.setdefault(metric_id, []).append(column_id)

    visible: set[str] = set()
    for metric_id, name in metrics:
        linked = by_metric.get(metric_id)
        if not linked:
            logger.warning("指标 %s 没有关联任何字段，在受限权限下一律不可见", name)
            continue
        if all(column_id in authorized_columns for column_id in linked):
            visible.add(name)
    return frozenset(visible)
