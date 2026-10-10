"""数据权限的范围计算与裁剪（FR-PERM-001 / BR-PERM-001）。

纯逻辑，不碰数据库也不碰 LLM：``build_scope`` 与 ``PermissionScope`` 是这条特性
的判定核心——"谁看得到哪张表的哪一列"全部由它们决定，而这类判定错了**不会报错**，
只会静默地多给或少给。把入参做成简单元组（见 ``agent.permissions.build_scope``），
正是为了让这些用例能在任何机器上跑。

一个口径在这份文件里被反复钉住：**没有配置 = 不设限**。它是全新环境的默认语义，
也是"配置第一条权限会顺手收紧该角色全部范围"这件事的来源，两种理解都不算错，
所以必须由用例写死其中一个。
"""

from __future__ import annotations

from app.agent.knowledge import ColumnSpec, TableSpec
from app.agent.permissions import COLUMN, TABLE, PermissionScope, build_scope

# ---- 一份小号元数据：两张表，fact_order 三列、dim_region 两列 ----
TABLES = [("t1", "fact_order"), ("t2", "dim_region")]
COLUMNS = [
    ("c1", "t1", "region_id"),
    ("c2", "t1", "order_amount"),
    ("c3", "t1", "order_quantity"),
    ("c4", "t2", "region_name"),
    # 与 c1 同名，且分属两张表——照抄真实数仓（fact_order.region_id 与
    # dim_region.region_id）。判定必须按"它出现在哪张表上"算，见下面的用例。
    ("c5", "t2", "region_id"),
]
METRICS = [("m1", "销售额"), ("m2", "订单量")]
LINKS = [("c2", "m1"), ("c3", "m2")]


def scope_of(*granted: tuple[str, str], **overrides) -> PermissionScope:
    """按给定的 (resource_type, resource_id) 授权算出范围。"""
    return build_scope(
        "analyst",
        granted=list(granted),
        tables=overrides.get("tables", TABLES),
        columns=overrides.get("columns", COLUMNS),
        metrics=overrides.get("metrics", METRICS),
        links=overrides.get("links", LINKS),
    )


def spec(name: str, *columns: str) -> TableSpec:
    """造一张表定义；字段名直接充当它的字段清单。"""
    return TableSpec(
        db_name="dw",
        name=name,
        alias=None,
        description=None,
        is_dimension=False,
        columns=[
            ColumnSpec(
                name=column,
                type="varchar(64)",
                alias=None,
                description=None,
                is_primary_key=False,
                is_foreign_key=False,
                ref_table=None,
                ref_column=None,
            )
            for column in columns
        ],
    )


# ==========================================================================
# 表级授权
# ==========================================================================


def test_a_granted_table_keeps_all_of_its_columns() -> None:
    """只授了表、没授字段：整表可用，将来新增的字段也自动可用。

    这是"表级授权"与"字段级授权"语义上确实不同的地方——所以 ``tables`` 的值
    才用 ``None`` 表示"整表"，而不是把当前的全部字段名列出来。
    """
    scope = scope_of((TABLE, "t1"))

    assert scope.allows_table("fact_order")
    assert scope.allows_column("fact_order", "order_amount")
    assert scope.allows_column("fact_order", "以后新增的列")


def test_a_table_without_any_grant_is_not_visible() -> None:
    scope = scope_of((TABLE, "t1"))

    assert not scope.allows_table("dim_region")
    assert not scope.allows_column("dim_region", "region_name")


def test_a_table_is_also_authorized_by_its_granted_column() -> None:
    """只授了某张表的**一个字段**，这张表本身也是可用的。

    漏掉这一条的后果很隐蔽：权限配得没错，但那张表根本没进候选，表现为
    "配了权限还是查不到"——而管理员会以为是权限没生效，再去配一遍。
    """
    scope = scope_of((COLUMN, "c4"))

    assert scope.allows_table("dim_region")
    assert scope.allows_column("dim_region", "region_name")
    assert not scope.allows_column("dim_region", "别的列")


def test_granting_a_column_narrows_the_whole_table() -> None:
    """表级与字段级同时存在时，**以字段级为准**。

    否则"先把整表授出去、再收窄到几列"这个动作就没法表达——而这正是管理界面上
    最自然的操作顺序。收窄只影响这张表。
    """
    scope = scope_of((TABLE, "t1"), (COLUMN, "c2"))

    assert scope.allows_column("fact_order", "order_amount")
    assert not scope.allows_column("fact_order", "order_quantity")
    assert not scope.allows_column("fact_order", "region_id")


def test_a_table_whose_columns_are_all_filtered_out_is_dropped() -> None:
    """表定义里一个可用字段都没有时，连表一起丢掉。

    留一张没有字段的空壳在提示词里，模型只会照着一个没有列的表去编列名。
    """
    scope = scope_of((TABLE, "t2"), (COLUMN, "c2"))  # fact_order 只剩 order_amount 可用

    # 这一份表定义里没有 order_amount，等于一个可用字段都没有
    kept = scope.filter_tables([spec("fact_order", "region_id", "order_quantity")])

    assert kept == []


# ==========================================================================
# filter_tables：进提示词之前的那道裁剪
# ==========================================================================


def test_filter_tables_drops_unauthorized_tables_and_columns() -> None:
    scope = scope_of((COLUMN, "c2"))  # 只给了 fact_order.order_amount
    tables = [spec("fact_order", "region_id", "order_amount"), spec("dim_region", "region_name")]

    kept = scope.filter_tables(tables)

    assert [table.name for table in kept] == ["fact_order"]
    assert [column.name for column in kept[0].columns] == ["order_amount"]


def test_filter_tables_keeps_a_wholly_granted_table_intact() -> None:
    scope = scope_of((TABLE, "t1"))
    tables = [spec("fact_order", "region_id", "order_amount", "order_quantity")]

    assert scope.filter_tables(tables) == tables


def test_filter_tables_is_idempotent() -> None:
    """裁两次与裁一次结果相同。

    filter_table 节点确实会调两次（模型筛选前后各一次，理由是 _trim 会把主外键
    加回来），"第二次调用"因此不是纸面上的可能性。不幂等的话，第二次调用会把
    第一次留下的东西又改一遍。
    """
    scope = scope_of((COLUMN, "c2"))
    tables = [spec("fact_order", "region_id", "order_amount")]

    once = scope.filter_tables(tables)
    assert scope.filter_tables(once) == once


def test_filter_tables_does_not_mutate_the_input() -> None:
    """裁剪是"返回一份新的"，不是"就地改"。

    同一份 TableSpec 会在 filter_table → add_extra_context → generate_sql 之间流转，
    就地改会让"谁把它裁掉的"变得不可追踪。
    """
    scope = scope_of((COLUMN, "c2"))
    tables = [spec("fact_order", "region_id", "order_amount")]
    original = [column.name for column in tables[0].columns]

    scope.filter_tables(tables)

    assert [column.name for column in tables[0].columns] == original


# ==========================================================================
# 不设限（角色还没有任何配置）
# ==========================================================================


def test_an_unrestricted_scope_lets_everything_through() -> None:
    scope = PermissionScope.everything("viewer")
    tables = [spec("fact_order", "region_id"), spec("dim_region", "region_name")]

    assert scope.filter_tables(tables) == tables
    assert scope.allows_table("任何表")
    assert scope.allows_column("任何表", "任何列")
    assert scope.allows_metric("任何指标")
    assert scope.violations("SELECT * FROM fact_order") == []


# ==========================================================================
# 指标可见性
# ==========================================================================


def test_a_metric_is_visible_only_when_all_of_its_columns_are() -> None:
    """指标按它关联的字段判可见——口径里写着 SUM(某字段)，它本身就是说明书。"""
    scope = scope_of((COLUMN, "c2"))  # 销售额(SUM(order_amount)) 可用

    assert scope.allows_metric("销售额")
    assert not scope.allows_metric("订单量"), "订单量关联的是没授权的 order_quantity"


def test_a_wholly_granted_table_makes_its_metrics_visible() -> None:
    scope = scope_of((TABLE, "t1"))

    assert scope.allows_metric("销售额")
    assert scope.allows_metric("订单量")


def test_a_metric_with_no_linked_columns_is_hidden_when_restricted() -> None:
    """没有任何字段关联的指标，在受限范围内一律不可见。

    没有依据能证明它只碰已授权的字段，宁可不给。这条同时是一条知识库数据问题
    （relevant_columns 没配上），所以实现里记 WARNING 而不是静默。
    """
    scope = scope_of((TABLE, "t1"), metrics=[("m9", "来路不明的指标")], links=[])

    assert not scope.allows_metric("来路不明的指标")


# ==========================================================================
# 越权判定（validate_sql 的兜底校验用）
# ==========================================================================


def test_a_reference_to_an_unauthorized_table_is_a_violation() -> None:
    scope = scope_of((TABLE, "t1"))

    assert scope.violations("SELECT region_name FROM dim_region") == ["dim_region"]


def test_a_reference_to_an_unauthorized_column_is_a_violation() -> None:
    scope = scope_of((COLUMN, "c2"))

    assert scope.violations("SELECT order_quantity FROM fact_order") == ["order_quantity"]


def test_a_column_name_borrowed_from_another_table_is_not_a_violation() -> None:
    """同名不同表：用一张整表授权里的字段，不能因为别处有同名字段就被判越权。

    这是走阶段 9 验收时**实测到的**：``fact_order.region_id`` 与
    ``dim_region.region_id`` 同名。若把"未授权字段名"平铺成一个集合来比对，
    ``SELECT region_id FROM fact_order`` 就会命中 dim_region 那一侧的同名字段
    而被判越权——而这句话碰的是一张整表授权的表，字段也确实属于它。

    后果不是"少给"，是**模型改不掉**：问题不在 SQL 上，correct_sql 拿着同一份
    被裁剪的表结构重写多少次都还是这句话，最后整句失败，用户看到的是"这个问法不行"。
    所以字段的判定必须落到"它出现在哪张表上"（见 PermissionScope.forbidden_columns）。
    """
    scope = scope_of((TABLE, "t1"), (COLUMN, "c4"))  # t1 整表可用；t2 只剩 region_name

    assert scope.violations("SELECT region_id FROM fact_order") == []
    assert scope.forbidden_columns == {"dim_region": frozenset({"region_id"})}
    # 同一句话换成 t2 就是越权——别把这条用例写成"判定被关掉了"
    assert scope.violations("SELECT region_id FROM dim_region") == ["region_id"]


def test_a_clean_statement_has_no_violations() -> None:
    scope = scope_of((COLUMN, "c2"))

    assert scope.violations("SELECT order_amount FROM fact_order WHERE order_amount > 0") == []


def test_cte_and_table_aliases_are_not_mistaken_for_unauthorized_tables() -> None:
    """CTE 名与别名不是元数据里的表，不该被算成越权。

    提取器按"已知表名"求交集，所以这里天然不会被误判——这条用例钉住这个设计，
    免得将来有人给提取器加上"排除 CTE"的语法分析（那是更复杂也更脆的一条路）。
    """
    scope = scope_of((TABLE, "t1"))

    sql = "WITH 汇总 AS (SELECT region_id FROM fact_order) SELECT o.region_id FROM 汇总 o"
    assert scope.violations(sql) == []


def test_violations_name_only_the_offending_objects() -> None:
    """返回的是名字本身，且只含有问题的那几个——日志靠它定位。"""
    scope = scope_of((TABLE, "t1"))

    assert scope.violations("SELECT dim_region.region_name FROM dim_region") == ["dim_region"]


def test_an_empty_or_missing_sql_is_not_a_violation() -> None:
    """SQL 还没生成（或为空）时不算越权——校验是针对语句的，不是针对"没有语句"。"""
    scope = scope_of((TABLE, "t1"))

    assert scope.violations(None) == []
    assert scope.violations("") == []


# ==========================================================================
# 悬空引用
# ==========================================================================


def test_a_permission_pointing_at_a_vanished_table_is_ignored() -> None:
    """权限行指向的表已从知识库删掉时，忽略它而不是崩。

    重建知识库时若把某张表从配置里删了，那几条 permission 就成了悬空引用。
    让整个权限计算炸掉，等于"某个管理员删了一张表"波及所有角色的每一次提问。
    """
    scope = scope_of((TABLE, "t1"), (TABLE, "已经删掉的表"))

    assert scope.allows_table("fact_order")
    assert not scope.allows_table("已经删掉的表")
    assert scope.tables == {"fact_order": None}
