"""阶段 8：图表类型推荐的规则（FR-VIZ-001）。

纯函数对纯函数，不碰图、不碰 LLM、不碰任何外部服务：规则表就是这个阶段的验收
对象，它必须能在没有 DeepSeek Key、没有 Redis 的机器上逐条验证。

列名一律取本项目数仓的真实形状（``app/scripts/seed_dw.py``）：``dim_date`` 的
``year`` / ``quarter`` / ``month`` / ``day`` 是**整数**列，``fact_order`` 的
``order_amount`` 是 DECIMAL（取数时转 float）、``order_status`` 是中文枚举。
用 "col1/col2" 这种占位列名测，恰好会绕开这个项目最容易错的那一处——
整数时间列与整数量度长得一模一样。
"""

from __future__ import annotations

import pytest

from app.agent.visualization import BAR, LINE, PIE, TABLE, recommend

# ==========================================================================
# 异常分支：结果为空
# ==========================================================================


def test_empty_result_recommends_no_chart() -> None:
    """结果为空不推荐图表，提示"无数据"（FR-VIZ-001 异常分支）。

    ``chart_type`` 给 None 而不是 "table"：空结果的正确呈现是"没有数据"，
    不是"用表格展示零行"——后者会让用户以为查询有问题。
    """
    result = recommend(["province", "amount"], [])

    assert result.chart_type is None
    assert result.message == "无数据"


def test_columns_without_rows_are_still_empty() -> None:
    """有列无行同样是空。判空只看行，不看列。"""
    assert recommend(["province", "amount"], []).chart_type is None


# ==========================================================================
# 单维分组 + 单度量 → 柱状图
# ==========================================================================


def test_single_dimension_and_measure_is_a_bar_chart() -> None:
    result = recommend(
        ["province", "order_amount"],
        [["上海市", 560890.86], ["江苏省", 431200.0], ["浙江省", 398760.5]],
    )

    assert result.chart_type == BAR


def test_chinese_column_aliases_are_handled() -> None:
    """模型起的别名多半是中文，规则不能只认英文列名。"""
    result = recommend(
        ["大区", "销售额"],
        [["华东", 120300.0], ["华南", 98200.0]],
    )

    assert result.chart_type == BAR


# ==========================================================================
# 时间 + 单度量 → 折线图
# ==========================================================================


def test_integer_quarter_column_is_a_time_axis_not_a_measure() -> None:
    """``year`` / ``quarter`` 是整数，但它们是时间轴，不是度量。

    这是本文件最要紧的一条。只按"值是不是数字"分类的话，"各季度销售额"
    会被认成"两个度量"而退回表格——不报错，只是永远画不出折线。
    """
    result = recommend(
        ["year", "quarter", "order_amount"],
        [
            [2024, 1, 120300.0],
            [2024, 2, 138900.0],
            [2024, 3, 151200.0],
            [2024, 4, 176400.0],
        ],
    )

    assert result.chart_type == LINE


def test_year_and_month_integers_are_a_time_axis() -> None:
    result = recommend(
        ["year", "month", "order_amount"],
        [[2024, 1, 120.0], [2024, 2, 138.0]],
    )

    assert result.chart_type == LINE


def test_iso_date_strings_are_detected_as_time_even_without_a_hint() -> None:
    """列名没线索时靠值兜底：``DwRepository`` 把 date 转成 ISO 字符串。

    列名刻意取 ``biz``——它不含任何时间片段，所以这条用例走的是"看值"那条路。
    用 ``stat_day`` 之类会命中列名提示词的名字，测的就成了上一条，白测一场。
    """
    result = recommend(
        ["biz", "cnt"],
        [["2024-01-01", 12], ["2024-01-02", 15]],
    )

    assert result.chart_type == LINE


def test_a_dimension_named_like_a_date_is_still_time() -> None:
    result = recommend(
        ["下单日期", "金额"],
        [["2024-01-01", 120.0], ["2024-01-02", 138.0]],
    )

    assert result.chart_type == LINE


# ==========================================================================
# 占比 → 饼图
# ==========================================================================


def test_share_by_column_name_is_a_pie() -> None:
    result = recommend(
        ["region_name", "销售额占比"],
        [["华东", 0.42], ["华南", 0.31], ["华北", 0.27]],
    )

    assert result.chart_type == PIE


def test_share_by_decimal_magnitude_is_a_pie() -> None:
    """值的量纲也是证据：占比之和必然是 1，与模型起什么别名无关。

    这一条比列名那条更可靠——"各区域销售额占比"的 SQL 天然就是 ``SUM/total``，
    值必然加总为 1；而别名是模型自由发挥的。
    """
    result = recommend(
        ["region_name", "amount"],
        [["华东", 0.42], ["华南", 0.31], ["华北", 0.27]],
    )

    assert result.chart_type == PIE


def test_share_by_percentage_magnitude_is_a_pie() -> None:
    """百分数口径（42.0 而不是 0.42）同样要认。"""
    result = recommend(
        ["region_name", "amount"],
        [["华东", 42.0], ["华南", 31.0], ["华北", 27.0]],
    )

    assert result.chart_type == PIE


def test_share_beside_its_own_base_measure_is_still_a_pie() -> None:
    """占比列与它算出来的原始金额列同时出现时，仍然画饼。

    这是**真链路里实测到的形态**：问"各地区的销售额占比"，模型返回三列
    ``大区 / 销售额 / 销售额占比``。原始金额不是干扰，占比正是由它算出来的，
    两者是同一份信息。要求"恰好一个度量"会把这种最典型的占比查询判成表格——
    而那正是 PRD 点名要画饼的场景。
    """
    result = recommend(
        ["大区", "销售额", "销售额占比"],
        [["华东", 8269805.0, 50.05], ["华南", 3201400.0, 19.37], ["华北", 2810300.0, 17.0]],
    )

    assert result.chart_type == PIE


def test_a_growth_rate_is_not_a_share() -> None:
    """增长率不是占比——各月增长率加起来没有任何意义，画成饼图会误导。

    刻意没把"率"放进占比线索里，这条用例钉住这个决定。
    """
    result = recommend(
        ["month", "增长率"],
        [["1月", 0.05], ["2月", 0.08], ["3月", 0.03]],
    )

    assert result.chart_type == LINE, "月份是时间轴，且不该被判成占比"


def test_pie_wins_over_line_when_both_rules_match() -> None:
    """两条规则同时命中时谁赢：占比赢（PRD 未规定，这是一处自定裁决）。

    "各月销售占比"既满足"时间+度量"，也满足"占比"。这里让更具体的那条赢——
    占比说的是"度量是什么"，比"维度是什么"更特殊。改这条裁决只需调
    ``recommend`` 里的判断顺序，别去动判定条件。
    """
    result = recommend(
        ["month", "占比"],
        [["1月", 0.3], ["2月", 0.35], ["3月", 0.35]],
    )

    assert result.chart_type == PIE


# ==========================================================================
# 多列明细 → 表格
# ==========================================================================


def test_time_plus_a_categorical_dimension_is_not_a_single_time_axis() -> None:
    """"各区域各季度销售额"不是一条时间轴，是多列明细。

    与上面"年+季"的差别只在于多掺了一个普通维度——它一旦掺进来，
    每一个时间点都不再只有一个值，折线画不出来。
    """
    result = recommend(
        ["year", "quarter", "region_name", "order_amount"],
        [[2024, 1, "华东", 120.0], [2024, 1, "华南", 98.0]],
    )

    assert result.chart_type == TABLE


def test_two_dimensions_and_one_measure_is_a_table() -> None:
    result = recommend(
        ["region_name", "category", "order_amount"],
        [["华东", "手机", 120.0], ["华东", "电脑", 98.0]],
    )

    assert result.chart_type == TABLE


def test_one_dimension_and_two_measures_is_a_table() -> None:
    """两个**普通**度量画不成图。

    与 ``test_share_beside_its_own_base_measure_is_still_a_pie`` 成对：那条证明
    "多一个占比列"不该退回表格，这条证明放宽的只是占比那一处、不是把
    "单维 + 单度量"整个门槛拆了。只有两条都在，才说明改动是有来由的。
    """
    result = recommend(
        ["province", "order_amount", "order_quantity"],
        [["上海市", 120.0, 3], ["江苏省", 98.0, 2]],
    )

    assert result.chart_type == TABLE


def test_a_single_aggregate_without_any_dimension_is_a_table() -> None:
    """``SELECT SUM(amount)`` 只有一个数，没有可画的东西。"""
    result = recommend(["order_amount"], [[1234567.89]])

    assert result.chart_type == TABLE


def test_identifier_columns_are_never_measures() -> None:
    """``order_id`` / ``customer_id`` 是整数，但不是度量。

    不这么判的话，明细查询会被算成"三维度两度量"——结论碰巧也是表格，
    但理由全错，下一个人改规则时就会改坏。
    """
    result = recommend(
        ["order_id", "customer_id", "order_amount"],
        [[1001, 7, 120.0], [1002, 8, 98.0]],
    )

    assert result.chart_type == TABLE


def test_identifiers_do_not_turn_a_grouping_into_a_two_dimension_table() -> None:
    """反过来也要对：``region_id`` 陪着 ``province`` 出现时，仍是一次单维分组。

    这一条与上一条一起，才说明"标识列算维度"是双向正确的。
    """
    result = recommend(
        ["region_id", "order_amount"],
        [[1, 120.0], [2, 98.0], [3, 76.0]],
    )

    assert result.chart_type == BAR


def test_booleans_do_not_count_as_measures() -> None:
    """``True``/``False`` 在 Python 里是 int 的子类，不能当成可求和的度量。"""
    result = recommend(
        ["province", "is_paid"],
        [["上海市", True], ["江苏省", False]],
    )

    assert result.chart_type == TABLE


def test_an_all_null_measure_column_is_a_dimension() -> None:
    """整列 NULL 时按维度处理，不让一个全空结果无端多出一个度量。"""
    result = recommend(["province", "amount"], [["上海市", None], ["江苏省", None]])

    assert result.chart_type == TABLE


@pytest.mark.parametrize("rows", [[], [[]], [[None]]])
def test_degenerate_rows_never_raise(rows: list) -> None:
    """规则是每轮问答都要跑的，任何形态都不该在这里抛异常。

    空行、长度对不上列数的行都算进来：结果集来自数据库，形状不完全受我们控制，
    而这里抛出去就是一个已经跑完的查询被一条推荐规则搞挂。
    """
    recommend(["province", "amount"], rows)  # 不抛即通过
