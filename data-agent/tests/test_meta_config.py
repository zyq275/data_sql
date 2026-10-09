"""meta_config.yaml 的加载、校验与确定性 id。

不依赖任何外部服务：配置解析是纯计算，应当随时可跑。
"""

from __future__ import annotations

from pathlib import Path

import pytest

from app.entities.meta_config import (
    ColumnDef,
    DatabaseDef,
    MetaConfig,
    MetaConfigError,
    MetaConfigLoader,
    MetricDef,
    TableDef,
    column_id,
    load_meta_config,
    metric_id,
    table_id,
)

DB = "data_agent_dw"


# --------------------------------------------------------------------------
# 确定性 id
#
# 这三个函数生成的 id 直接就是 MySQL 主键、Qdrant 点 id、ES 文档 id。
# 一旦命名空间或自然键格式被改动，库里所有既有 id 全部失效，
# permission.resource_id 指的关系也跟着断。所以这里把实际值钉死成黄金值：
# 有意变更必须同步改测试并规划数据迁移，不能悄悄改。
# --------------------------------------------------------------------------


def test_ids_are_stable_golden_values() -> None:
    assert table_id(DB, "fact_order") == "80edd3a8-9c21-5e6d-a926-e3ebc80e00d4"
    assert (
        column_id(DB, "fact_order", "order_amount")
        == "abe75aca-4494-5a48-b4b7-d0213bbfa463"
    )
    assert metric_id("GMV") == "5ec3e1c6-a203-57e2-88e7-e751dff3f883"


def test_ids_are_reproducible() -> None:
    """同一自然键反复调用必须得到同一个 id——这是重建幂等的前提。"""
    assert table_id(DB, "fact_order") == table_id(DB, "fact_order")
    assert column_id(DB, "fact_order", "order_amount") == column_id(
        DB, "fact_order", "order_amount"
    )


def test_ids_differ_by_natural_key() -> None:
    assert table_id(DB, "fact_order") != table_id(DB, "dim_region")
    assert column_id(DB, "fact_order", "order_amount") != column_id(
        DB, "fact_order", "order_quantity"
    )
    assert metric_id("GMV") != metric_id("order_count")


def test_id_namespaces_do_not_collide() -> None:
    """表、字段、指标三类 id 不能撞车，否则三类对象会在同一个 id 空间里互相覆盖。"""
    ids = {
        table_id(DB, "x"),
        column_id(DB, "x", "x"),
        metric_id("x"),
    }
    assert len(ids) == 3


def test_same_table_name_in_different_databases_gets_different_id() -> None:
    """id 里带 db_name：不同库的同名表必须区分开，否则跨库场景会静默串数据。"""
    assert table_id("db_a", "orders") != table_id("db_b", "orders")


# --------------------------------------------------------------------------
# 校验：一次性收集全部问题
# --------------------------------------------------------------------------


def _column(name: str, **overrides: object) -> ColumnDef:
    data: dict[str, object] = {"column_name": name, "column_type": "varchar(32)", "is_primary_key": True}
    data.update(overrides)
    return ColumnDef(**data)


def _loader(
    tables: list[TableDef] | None = None, metrics: list[MetricDef] | None = None
) -> MetaConfigLoader:
    return MetaConfigLoader(
        MetaConfig(
            databases=[DatabaseDef(db_name=DB, tables=tables or [_table("t1")])],
            metrics=metrics if metrics is not None else [_metric()],
        )
    )


def _table(name: str, columns: list[ColumnDef] | None = None) -> TableDef:
    return TableDef(table_name=name, columns=columns or [_column("id")])


def _metric(**overrides: object) -> MetricDef:
    data: dict[str, object] = {
        "metric_name": "m1",
        "calculation": "SUM(t1.id)",
        "relevant_columns": ["id"],
    }
    data.update(overrides)
    return MetricDef(**data)


def test_valid_minimal_config_passes() -> None:
    _loader().validate()


def test_empty_databases_is_rejected() -> None:
    loader = MetaConfigLoader(MetaConfig(databases=[], metrics=[_metric()]))
    with pytest.raises(MetaConfigError, match="databases 为空"):
        loader.validate()


def test_empty_metrics_is_rejected() -> None:
    loader = MetaConfigLoader(MetaConfig(databases=[DatabaseDef(db_name=DB, tables=[_table("t1")])]))
    with pytest.raises(MetaConfigError, match="metrics 为空"):
        loader.validate()


def test_duplicate_column_is_rejected() -> None:
    loader = _loader(tables=[_table("t1", [_column("id"), _column("id")])])
    with pytest.raises(MetaConfigError, match="字段重复定义"):
        loader.validate()


def test_table_without_primary_key_is_rejected() -> None:
    loader = _loader(tables=[_table("t1", [_column("id", is_primary_key=False)])])
    with pytest.raises(MetaConfigError, match="没有主键字段"):
        loader.validate()


def test_table_without_columns_is_rejected() -> None:
    loader = _loader(tables=[TableDef(table_name="t1", columns=[])])
    with pytest.raises(MetaConfigError, match="没有任何字段"):
        loader.validate()


def test_foreign_key_without_target_is_rejected() -> None:
    """外键缺指向会让召回结果补不齐关联表（BR-META-001）。"""
    loader = _loader(
        tables=[
            _table("t1"),  # 提供 id 供指标引用
            _table("t2", [_column("id"), _column("t1_id", is_primary_key=False, is_foreign_key=True)]),
        ]
    )
    with pytest.raises(MetaConfigError, match="缺少 ref_table/ref_column"):
        loader.validate()


def test_foreign_key_pointing_to_missing_column_is_rejected() -> None:
    loader = _loader(
        tables=[
            _table("t1"),
            _table(
                "t2",
                [
                    _column("id"),
                    _column(
                        "t1_id",
                        is_primary_key=False,
                        is_foreign_key=True,
                        ref_table="t1",
                        ref_column="not_there",
                    ),
                ],
            ),
        ]
    )
    with pytest.raises(MetaConfigError, match="指向不存在的字段"):
        loader.validate()


def test_ref_target_without_foreign_key_flag_is_rejected() -> None:
    """标了 ref_table 却没标 is_foreign_key，属自相矛盾，必须挡住。"""
    loader = _loader(
        tables=[_table("t1"), _table("t2", [_column("id"), _column("t1_id", is_primary_key=False, ref_table="t1", ref_column="id")])]
    )
    with pytest.raises(MetaConfigError, match="未标记 is_foreign_key"):
        loader.validate()


def test_metric_without_calculation_is_rejected() -> None:
    """指标口径不完整（BR-META-002）——只有名字没有算法，生成的 SQL 会是猜的。"""
    loader = _loader(metrics=[_metric(calculation=None)])
    with pytest.raises(MetaConfigError, match="缺少 calculation"):
        loader.validate()


def test_metric_referencing_unknown_column_is_rejected() -> None:
    loader = _loader(metrics=[_metric(relevant_columns=["ghost_column"])])
    with pytest.raises(MetaConfigError, match="在任何表中都不存在"):
        loader.validate()


def test_all_problems_are_reported_at_once() -> None:
    """一次报全部问题，避免配置改一处、跑一次、再发现下一处的来回试错。"""
    loader = _loader(
        tables=[_table("t1", [_column("id", is_primary_key=False)])],
        metrics=[_metric(calculation=None, relevant_columns=["ghost"])],
    )
    with pytest.raises(MetaConfigError) as excinfo:
        loader.validate()

    message = str(excinfo.value)
    assert "没有主键字段" in message
    assert "缺少 calculation" in message
    assert "在任何表中都不存在" in message
    assert "共 3 处问题" in message


# --------------------------------------------------------------------------
# 相关字段解析
# --------------------------------------------------------------------------


def test_ambiguous_column_name_resolves_to_every_match() -> None:
    """同名字段出现在多张表时全部返回。

    宁可多给一个候选，也不要因为配置少写限定而漏掉真正相关的字段——
    date_id 同时存在于 dim_date 与 fact_order，只取其一就会漏。
    """
    loader = _loader(
        tables=[
            _table("dim_date", [_column("id"), _column("date_id", is_primary_key=False)]),
            _table("fact_order", [_column("id"), _column("date_id", is_primary_key=False)]),
        ]
    )
    matches = loader.resolve_metric_columns(
        MetricDef(metric_name="m", calculation="COUNT(*)", relevant_columns=["date_id"])
    )
    assert {table for _db, table, _column_name in matches} == {"dim_date", "fact_order"}


def test_unknown_column_name_resolves_to_nothing() -> None:
    assert _loader().resolve_metric_columns(
        MetricDef(metric_name="m", calculation="COUNT(*)", relevant_columns=["ghost"])
    ) == []


def test_sync_columns_selects_only_flagged_columns() -> None:
    loader = _loader(
        tables=[_table("t1", [_column("id"), _column("province", is_primary_key=False, sync_values=True)])]
    )
    assert [c.column_name for _db, _table_def, c in loader.sync_columns()] == ["province"]


# --------------------------------------------------------------------------
# 真实配置文件：这是阶段 4 交付的知识库声明，坏掉必须立刻发现
# --------------------------------------------------------------------------


def test_shipped_meta_config_is_valid() -> None:
    loader = load_meta_config()
    loader.validate()


def test_shipped_meta_config_shape() -> None:
    loader = load_meta_config()
    tables = loader.tables()
    columns = [c for _db, table in tables for c in table.columns]

    assert len(tables) == 5
    assert len(columns) == 25
    assert len(loader.config.metrics) == 5
    assert len(loader.sync_columns()) == 8


def test_every_foreign_key_in_shipped_config_points_at_a_real_column() -> None:
    """validate() 已经查了，这里再独立数一遍：外键数量掉到 0 说明配置被改坏了。"""
    loader = load_meta_config()
    foreign_keys = [
        (table.table_name, c.column_name)
        for _db, table in loader.tables()
        for c in table.columns
        if c.is_foreign_key
    ]
    assert len(foreign_keys) == 4


def test_missing_config_file_raises_with_path(tmp_path: Path) -> None:
    with pytest.raises(MetaConfigError, match="配置文件不存在"):
        load_meta_config(Path(tmp_path) / "nope.yaml")
