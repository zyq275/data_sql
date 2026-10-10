"""阶段 10：评测判分与汇总（FR-EVAL-002）。

不依赖任何外部服务：判分是纯计算，正因如此它才该被逐条钉死——准确率这个数字
是要拿来做"这次改动到底有没有变好"的结论的，判分逻辑本身出错的话，
所有结论都会朝着同一个方向偏，而且偏得看不出来。
"""

from __future__ import annotations

from datetime import datetime
from decimal import Decimal
from typing import Any

import pytest

from app.agent.knowledge import TableSpec
from app.models import EvalCase
from app.repositories.dw import QueryResult
from app.schemas.eval import EvalCaseOutcome
from app.services.eval_runner import judge, normalize_sql, recall_hit, rows_equal, summarize


def case(**kwargs: Any) -> EvalCase:
    """内存里的样本行（不需要数据库）。"""
    defaults = {
        "id": "case-1",
        "query_text": "华东区销售额",
        "expected_sql": None,
        "expected_result": None,
        "created_at": datetime(2026, 10, 10, 10, 0, 0),
    }
    return EvalCase(**{**defaults, **kwargs})


def outcome(**kwargs: Any) -> EvalCaseOutcome:
    defaults = {"case_id": "c", "query_text": "q", "duration_ms": 100}
    return EvalCaseOutcome(**{**defaults, **kwargs})


# ==========================================================================
# 汇总
# ==========================================================================


def test_an_empty_run_has_no_metrics_rather_than_zeros() -> None:
    """没有样本 → 指标为 None。

    0 分与"没测"是两件事：把空运行的准确率写成 0，趋势图上会出现一根触底的折线，
    看起来像"某次改动把系统搞崩了"，而实际只是那次没跑成。
    """
    metrics = summarize([])

    assert metrics.accuracy is None
    assert metrics.recall_hit_rate is None
    assert metrics.execute_success_rate is None
    assert metrics.avg_duration_ms is None


def test_rates_are_computed_over_the_right_denominator() -> None:
    metrics = summarize(
        [
            outcome(correct=True, executed=True, recall_hit=True, duration_ms=1000),
            outcome(correct=False, executed=True, recall_hit=False, duration_ms=2000),
            outcome(correct=True, executed=False, recall_hit=None, duration_ms=3000),
            outcome(correct=False, executed=False, recall_hit=None, duration_ms=4000),
        ]
    )

    assert metrics.accuracy == Decimal("0.5"), "2/4 答对"
    assert metrics.execute_success_rate == Decimal("0.5"), "2/4 跑出了结果"
    # 召回只看判得出来的两条（另两条没有 expected_sql），1/2
    assert metrics.recall_hit_rate == Decimal("0.5")
    assert metrics.avg_duration_ms == 2500


def test_a_run_where_no_sample_can_be_judged_for_recall_leaves_it_empty() -> None:
    """判不出来就不能算 0——"不知道"与"没做到"给知识库维护者的动作完全不同。"""
    metrics = summarize([outcome(recall_hit=None), outcome(recall_hit=None)])

    assert metrics.recall_hit_rate is None
    assert metrics.execute_success_rate == Decimal("0.0")


def test_rates_fit_the_decimal_6_4_column() -> None:
    """1/3 这种无限小数必须先收敛到 4 位，否则严格模式下写库直接报错。"""
    metrics = summarize([outcome(correct=True) for _ in range(3)][:1] + [outcome() for _ in range(2)])

    assert metrics.accuracy == Decimal("0.3333")
    assert -metrics.accuracy.as_tuple().exponent == 4


# ==========================================================================
# 结果集比对
# ==========================================================================


def test_rows_are_compared_as_a_multiset_not_a_sequence() -> None:
    """行序不同不算错：同一个 GROUP BY 换个索引、换台机器跑，返回次序本来就不保证。"""
    ok, _ = rows_equal(
        {"columns": ["a"], "rows": [[2], [1]]},
        {"columns": ["a"], "rows": [[1], [2]]},
    )

    assert ok is True


def test_a_different_column_order_is_aligned_by_name_before_comparing() -> None:
    """``SELECT province, SUM(..)`` 与 ``SELECT SUM(..), province`` 是同一个答案。"""
    ok, _ = rows_equal(
        {"columns": ["province", "gmv"], "rows": [["浙江", 100]]},
        {"columns": ["gmv", "province"], "rows": [[100, "浙江"]]},
    )

    assert ok is True


def test_a_different_set_of_columns_is_not_the_same_result() -> None:
    ok, why = rows_equal(
        {"columns": ["province"], "rows": [["浙江"]]},
        {"columns": ["region_name"], "rows": [["华东"]]},
    )

    assert ok is False
    assert "列不一致" in why


def test_float_noise_does_not_make_a_correct_answer_look_wrong() -> None:
    """Decimal 经 JSON/float 走一圈会带上 1e-13 级的尾巴，比到小数点后 6 位足够。"""
    ok, _ = rows_equal(
        {"columns": ["gmv"], "rows": [[Decimal("1234.56")]]},
        {"columns": ["gmv"], "rows": [[1234.5600000000001]]},
    )

    assert ok is True


def test_large_integers_are_compared_exactly() -> None:
    """订单号是 bigint，经 float 会在 2^53 之后丢精度——那会把不同的订单判成同一个。"""
    ok, _ = rows_equal(
        {"columns": ["order_id"], "rows": [[9007199254740993]]},
        {"columns": ["order_id"], "rows": [[9007199254740992]]},
    )

    assert ok is False


def test_an_empty_result_is_a_valid_expectation() -> None:
    ok, _ = rows_equal({"columns": ["a"], "rows": []}, {"columns": ["a"], "rows": []})

    assert ok is True


def test_no_result_at_all_is_not_equal_to_an_expected_result() -> None:
    """没跑出结果 ≠ 结果为空，前者是失败，后者可能是正确答案。"""
    ok, why = rows_equal(None, {"columns": ["a"], "rows": []})

    assert ok is False
    assert "没有产生结果集" in why


def test_sql_text_comparison_ignores_formatting() -> None:
    assert normalize_sql(" SELECT a\nFROM   t ; ") == normalize_sql("select a from t")


# ==========================================================================
# 判定
# ==========================================================================


class FakeDw:
    """只实现判分用到的执行入口。"""

    def __init__(self, result: QueryResult | Exception) -> None:
        self.result = result
        self.calls: list[str] = []

    async def execute(self, sql: str) -> QueryResult:
        self.calls.append(sql)
        if isinstance(self.result, Exception):
            raise self.result
        return self.result


async def test_the_samples_expected_result_wins_over_its_sql() -> None:
    """样本自带预期结果时不再去跑标准 SQL：那是样本作者已经算好的答案。"""
    dw = FakeDw(QueryResult(columns=["a"], rows=[[999]]))
    sample = case(
        expected_sql="SELECT a FROM t",
        expected_result={"columns": ["a"], "rows": [[1]]},
    )

    ok, why = await judge(sample, actual={"columns": ["a"], "rows": [[1]]}, actual_sql="x", dw=dw)

    assert ok is True
    assert dw.calls == [], "有预期结果就不该再去执行标准 SQL"
    assert "预期结果" in why


async def test_without_an_expected_result_the_standard_sql_is_executed() -> None:
    dw = FakeDw(QueryResult(columns=["a"], rows=[[1]]))
    sample = case(expected_sql="SELECT a FROM t")

    ok, why = await judge(sample, actual={"columns": ["a"], "rows": [[1]]}, actual_sql="x", dw=dw)

    assert ok is True
    assert dw.calls == ["SELECT a FROM t"]
    assert "标准 SQL" in why


async def test_an_unrunnable_standard_sql_falls_back_to_text_comparison() -> None:
    """标准 SQL 自己跑不动时，把退化的事实写进说明里。

    悄悄换掉判分依据是最坏的一种：这一次的准确率与上一次已经不是同一个口径了。
    """
    from app.core.exceptions import SQLExecutionError

    dw = FakeDw(SQLExecutionError("Unknown column 'a'"))
    sample = case(expected_sql="SELECT a FROM t")

    ok, why = await judge(sample, actual=None, actual_sql="SELECT a FROM t", dw=dw)

    assert ok is True
    assert "退化为 SQL 文本比对" in why


async def test_a_sample_with_neither_expectation_is_counted_as_wrong_with_a_reason() -> None:
    ok, why = await judge(case(), actual=None, actual_sql="SELECT 1", dw=FakeDw(QueryResult()))

    assert ok is False
    assert "无法判定" in why


# ==========================================================================
# 召回命中
# ==========================================================================


def spec(name: str) -> TableSpec:
    return TableSpec(db_name="dw", name=name, alias=None, description=None, is_dimension=True)


def test_recall_hit_requires_every_table_the_standard_sql_needs() -> None:
    sample = case(expected_sql="SELECT * FROM fact_order JOIN dim_date ON 1=1")
    known = {"fact_order", "dim_date", "dim_region"}

    assert recall_hit(sample, {"table_infos": [spec("fact_order"), spec("dim_date")]}, known) is True
    assert recall_hit(sample, {"table_infos": [spec("fact_order")]}, known) is False


def test_names_that_are_not_in_the_knowledge_base_are_ignored() -> None:
    """CTE 名、别名、写错的表名都不该被算进"该召回的"里。"""
    sample = case(expected_sql="WITH x AS (SELECT 1) SELECT * FROM x JOIN dim_region ON 1=1")
    known = {"fact_order", "dim_region"}

    assert recall_hit(sample, {"table_infos": [spec("dim_region")]}, known) is True


def test_recall_cannot_be_judged_without_a_standard_sql() -> None:
    assert recall_hit(case(), {"table_infos": [spec("fact_order")]}, {"fact_order"}) is None


def test_recall_cannot_be_judged_when_the_standard_sql_touches_no_known_table() -> None:
    sample = case(expected_sql="SELECT 1")

    assert recall_hit(sample, {"table_infos": []}, {"fact_order"}) is None


@pytest.mark.parametrize("value", ["2025-01-01", "20250101"])
def test_dashboard_accepts_both_date_spellings(value: str) -> None:
    """两种写法都要认：接口文档写 YYYY-MM-DD，而 date_id 的口径是 yyyymmdd。"""
    from app.services.dashboard_service import _date_id

    assert _date_id(value) == 20250101
