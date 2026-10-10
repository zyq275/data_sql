"""阶段 10：看板拼模板 SQL 的纯函数部分（FR-DASH-002）。

不需要任何外部服务：拼装是字符串操作，而"该不该拼"这类判断正是最该逐条钉死的——
它错了不会报错，只会给出一个看着正常的数字。整条链路（取数、聚合、权限）另有
``test_dashboard_api`` 与 ``test_stage10_acceptance`` 走真实数仓覆盖。
"""

from __future__ import annotations

from datetime import datetime

import pytest

from app.core.exceptions import ValidationError
from app.models import MetricInfo
from app.services.dashboard_service import Dimension, Join, _assemble, _parse_dimensions, _predicate


def metric(name: str, *, calculation: str = "SUM(t.a)", filter_sql: str | None = "t.ok = 1"):
    """内存里的指标行（不需要数据库）。

    ``filter_sql`` 默认给一个值而不是 None：绝大多数用例要验的是拼装本身。
    """
    return MetricInfo(
        id=f"m-{name}",
        metric_name=name,
        metric_alias=None,
        description=None,
        calculation=calculation,
        filter_condition=None,
        filter_sql=filter_sql,
        time_range=None,
        relevant_columns=None,
        created_at=datetime(2026, 10, 10),
    )


# ==========================================================================
# 过滤谓词
# ==========================================================================


def test_metrics_sharing_one_predicate_use_it() -> None:
    assert _predicate([metric("GMV"), metric("order_count")]) == "t.ok = 1"


def test_metrics_with_different_predicates_are_refused() -> None:
    """一条 WHERE 对所有指标生效，混着用会让不该过滤的被打上，而看着只是"数小了点"。"""
    with pytest.raises(ValidationError, match="口径不一致"):
        _predicate([metric("GMV"), metric("order_count", filter_sql="t.status = '已签收'")])


def test_a_metric_without_a_filter_is_refused_rather_than_run_unfiltered() -> None:
    """没写 ``filter_sql`` 的指标不上看板，而不是按不过滤算。

    按不过滤算给出的是一个**与问答链路口径不一致**的数字，而它看着完全正常——
    两个数都"正常"时没有人会去对账，于是这次的错会一直留着。拒掉至少会有人来问一句。
    """
    with pytest.raises(ValidationError, match="没有配置过滤口径"):
        _predicate([metric("GMV"), metric("新指标", filter_sql=None)])


def test_an_empty_predicate_is_an_explicit_no_filter() -> None:
    """``""`` 与 ``None`` 是两件事：前者是"确实不过滤"，后者是"忘了写"。

    区分它们不需要额外字段——``str | None`` 本来就分得开，而合并成一个"空"的话，
    "累计注册用户数"这类真的不用过滤的指标就永远上不了看板。
    """
    assert _predicate([metric("累计注册数", filter_sql="")]) is None


def test_an_explicit_no_filter_cannot_be_mixed_with_a_real_one() -> None:
    """"不过滤"与"只看已支付"放进同一条 WHERE，等于把后者也放开了。"""
    with pytest.raises(ValidationError, match="口径不一致"):
        _predicate([metric("GMV"), metric("累计注册数", filter_sql="")])


# ==========================================================================
# 维度
# ==========================================================================


def test_dimensions_are_kept_in_the_order_requested() -> None:
    """列的先后由请求决定：``SELECT year, month`` 与 ``SELECT month, year`` 是两张表。"""
    assert [d.alias for d in _parse_dimensions("year,month")] == ["year", "month"]
    assert [d.alias for d in _parse_dimensions("month,year")] == ["month", "year"]


def test_a_repeated_dimension_is_collapsed() -> None:
    assert [d.alias for d in _parse_dimensions("year,year,month")] == ["year", "month"]


def test_an_unknown_dimension_is_refused_by_name() -> None:
    """点名是必要的：说"不支持"而不说是哪一个，用户只能逐个试。"""
    with pytest.raises(ValidationError, match="customer_name"):
        _parse_dimensions("year,customer_name")


def test_an_empty_dimension_is_refused() -> None:
    with pytest.raises(ValidationError, match="至少一个聚合维度"):
        _parse_dimensions(" , ")


# ==========================================================================
# 拼装
# ==========================================================================


def assemble(predicate: str | None = "fact_order.order_status = '已支付'", **overrides) -> str:
    kwargs = {
        "fact": "fact_order",
        "metrics": [metric("GMV", calculation="SUM(fact_order.order_amount)")],
        "dims": [Dimension("dim_date", "year", "time")],
        "joins": [Join("dim_date", "fact_order.date_id = dim_date.date_id", "dim_date.date_id")],
        "predicate": predicate,
        "date_range": None,
    }
    return _assemble(**{**kwargs, **overrides})


def test_aliases_are_backticked() -> None:
    """``year`` / ``month`` 同时也是 MySQL 的函数名，``AS month`` 会被当成函数调用位置。"""
    sql = assemble()

    assert "dim_date.year AS `year`" in sql
    assert "SUM(fact_order.order_amount) AS `GMV`" in sql


def test_the_join_comes_from_the_given_condition() -> None:
    assert "JOIN dim_date ON fact_order.date_id = dim_date.date_id" in assemble()


def test_both_the_predicate_and_the_date_range_land_in_the_where_clause() -> None:
    sql = assemble(date_range="dim_date.date_id >= 20250101")

    assert "WHERE fact_order.order_status = '已支付' AND dim_date.date_id >= 20250101" in sql


def test_an_empty_predicate_leaves_no_where_clause_at_all() -> None:
    """不过滤 + 无时间范围时不该留下一个空的 ``WHERE``。"""
    sql = assemble(predicate=None)

    assert "WHERE" not in sql


def test_the_date_range_alone_still_produces_a_where_clause() -> None:
    sql = assemble(predicate=None, date_range="dim_date.date_id >= 20250101")

    assert "WHERE dim_date.date_id >= 20250101" in sql


def test_grouping_and_ordering_use_the_same_expression() -> None:
    """按维度值排序：折线的连线顺序由它决定，交给数据库自己发挥会画出两张不同的图。"""
    sql = assemble(dims=[Dimension("dim_date", "year", "time"), Dimension("dim_date", "month", "time")])

    assert "GROUP BY dim_date.year, dim_date.month" in sql
    assert "ORDER BY dim_date.year, dim_date.month" in sql
