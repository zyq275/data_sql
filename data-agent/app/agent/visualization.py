"""图表类型推荐（FR-VIZ-001 / FR-VIZ-002）。

**这里是规则，不是提示词**。TDD 6 的提示词表里列过一份 ``recommend_visualization``，
实现时没有采用，理由：

1. FR-VIZ-001 的验收标准本身就是一张规则表（"单维分组+单度量→柱状图"……），
   而"符合规则"只有把规则写进代码才验得了——交给模型的话，同一份结果今天推柱状、
   明天推折线，验收时看到哪一次全凭运气。
2. "按结果结构推荐"（开发步骤文档阶段 8 第 2 条）本来就是**结构化**判断：
   数一数列、辨一辨类型就够了，不需要理解语义。为此加一次 LLM 调用，
   换来的是每轮问答多几百毫秒、多一份钱，以及一个不可复现的输出。
3. 它因此也是图上唯一一个**不会失败**的节点：纯计算，没有上游可抖。

规则与判定口径写在下面，尤其是"两条规则同时命中时谁赢"和"占比怎么认"——
这两处是最容易被后人改出偏差的地方。
"""

from __future__ import annotations

import re
from collections.abc import Sequence
from dataclasses import dataclass
from decimal import Decimal
from typing import Any

#: 推荐的图表类型。与 TDD 7.3 的 ``viz`` 事件字段取值一致。
BAR = "bar"
LINE = "line"
PIE = "pie"
TABLE = "table"

NO_DATA = "无数据"
"""结果为空时的提示语（FR-VIZ-001 异常分支）。原文如此，不要改写成"暂无数据"。"""

#: 列名里出现这些片段就当成时间列。
#:
#: **为什么必须靠列名而不是靠值**：本项目数仓的时间维度是 ``dim_date`` 的
#: ``year`` / ``quarter`` / ``month`` / ``day`` 四个**整数**列（见 scripts/seed_dw.py），
#: 光看值它们和度量一模一样。只按"值是不是数字"分类，会把 ``quarter`` 认成度量，
#: 于是"各季度销售额"变成"两个度量"→ 退回表格，"时间+度量→折线"这条规则
#: 在真实数据上永远命中不了。
_TIME_HINTS = (
    "date",
    "time",
    "year",
    "quarter",
    "month",
    "week",
    "day",
    "日期",
    "时间",
    "年",
    "季",
    "月",
    "周",
    "天",
)

#: 形如 ``2024-01-01`` / ``2024-01-01T00:00:00`` 的值——``DwRepository`` 把
#: ``date`` / ``datetime`` 转成 ISO 字符串（见 repositories/dw.py 的 ``_jsonable``），
#: 所以时间列在结果里长这样。列名没命中提示词时，这一条兜住。
_ISO_DATE = re.compile(r"^\d{4}-\d{2}-\d{2}([ T].*)?$")

#: 列名里出现这些片段就当成"占比型"度量。
#:
#: 刻意不含"率"：增长率、转化率、退货率都是率，但它们**不是占比**，
#: 画成饼图会误导（各月增长率加起来没有任何意义）。
_SHARE_HINTS = ("占比", "比例", "百分比", "比重", "份额", "ratio", "percent", "share", "pct")

#: 占比型数值的两种常见量纲：小数（0.32）与百分数（32.0）。
_SHARE_TOLERANCE = 0.02

_NUMERIC = (int, float, Decimal)


@dataclass(frozen=True, slots=True)
class Recommendation:
    """一次推荐的结果。

    ``chart_type`` 为 None 表示不推荐图表（当前只有"结果为空"一种情形）。
    ``message`` 是给人看的一句话，前端可直接展示或当作悬浮说明。
    """

    chart_type: str | None
    message: str


def recommend(columns: Sequence[str], rows: Sequence[Sequence[Any]]) -> Recommendation:
    """按结果结构推荐图表类型。

    规则表（FR-VIZ-001），判定顺序即优先级：

    | 条件 | 推荐 |
    |---|---|
    | 结果为空 | 不推荐，提示"无数据" |
    | 单维分组，且度量里有占比型的 | 饼图 |
    | 单维分组 + 单度量，且该维是时间 | 折线图 |
    | 单维分组 + 单度量，且该维是普通维度 | 柱状图 |
    | 单度量，维度**全部**是时间（如 ``year`` + ``quarter`` 两列） | 折线图 |
    | 其余（多列明细） | 表格 |

    **为什么"维度全是时间"要单独列一条**：本项目的时间轴经常由两列拼成——
    ``SELECT year, quarter, SUM(amount) GROUP BY year, quarter``（``dim_date`` 的
    真实形状）。按"恰好一个维度"去判，这种最常见的季度走势会被当成两个维度而
    退回表格。时间轴可以是多列（年+季、年+月），只是粒度更细的同一条轴；
    而一旦掺进一个普通维度（各区域各季度），它就不再是一条轴，按多列明细处理。

    **饼图为什么排在折线之前**：PRD 的四条规则里，"时间+单度量→折线"与
    "占比→饼图"会同时命中"各月销售占比"这类问题，而 PRD 没说谁优先。
    这里让更具体的那条赢——占比说的是"度量是什么"，比"维度是什么"更特殊。
    这是**一处自定的裁决**，PRD 未规定；若将来数据显示时间序列的占比更该看走势，
    改这一处的顺序即可，别去改判定条件。

    **"多列明细→表格"不是一条独立判定，而是兜底**：两个维度、两个度量、
    只有一个聚合值（如"总销售额"）、一个度量都没有……这些形态都画不成图，
    统一落到表格。写成兜底而不是逐条枚举，是因为枚举必然漏——漏掉的那种形态
    会静默地什么都不返回。
    """
    if not rows:
        return Recommendation(None, NO_DATA)

    roles = _classify(columns, rows)
    measures = [index for index, role in enumerate(roles) if role == "measure"]
    # 时间列也是维度（它是分组依据），单独留一份是为了判"该维是不是时间"
    dimensions = [index for index, role in enumerate(roles) if role != "measure"]

    # 恰好一个维度时，这一维就是唯一的分组依据，度量有几个决定了画什么
    if len(dimensions) == 1:
        dimension = dimensions[0]
        # 占比优先，且**不要求"恰好一个度量"**：实测模型回答"各地区的销售额占比"
        # 时返回三列——大区、销售额、销售额占比。多出来的那列原始金额不是干扰，
        # 占比正是由它算出来的，两者是同一份信息。硬要求"恰好一个度量"会把
        # 这种最常见的占比查询判成表格，而那恰恰是 PRD 点名要画饼的场景。
        #
        # 仍然要求"恰好一个维度"：两个维度的占比是每个 (区域,品类) 各占多少，
        # 那已经是一张明细表，摊成饼图看不出东西。
        if any(_looks_like_share(columns[index], rows, index) for index in measures):
            return Recommendation(PIE, "占比型度量，用饼图看构成")
        if len(measures) == 1:
            if roles[dimension] == "time":
                return Recommendation(LINE, "时间 + 单度量，用折线图看走势")
            return Recommendation(BAR, "单维分组 + 单度量，用柱状图对比各分组大小")

    # 维度全是时间（如 year + quarter 两列拼成的时间轴）同样是走势图
    if len(measures) == 1 and dimensions and all(
        roles[index] == "time" for index in dimensions
    ):
        return Recommendation(LINE, "时间 + 单度量，用折线图看走势")

    return Recommendation(TABLE, "多列明细，用表格逐行展示")


def _classify(columns: Sequence[str], rows: Sequence[Sequence[Any]]) -> list[str]:
    """给每一列判一个角色：``time`` / ``measure`` / ``dimension``。

    **判定顺序不能换**：先看是不是时间列（``year`` 是整数但不是度量），
    再看是不是标识列（``region_id`` 是整数但也不是度量），最后才按值的类型定。
    顺序换了，``quarter`` 和 ``region_id`` 都会被当成度量，
    表现为"该出折线图的问题退回了表格"——不报错，只是推荐得不对。

    全列都是空值（整列 NULL）时按维度处理：没有值就没有"可被聚合的数量"这回事，
    把它当度量会让一个全空的结果集凭空多出一个度量、从而退化成表格。
    """
    roles: list[str] = []
    for index, name in enumerate(columns):
        values = [row[index] for row in rows if index < len(row) and row[index] is not None]
        if _is_time(name, values):
            roles.append("time")
        elif _is_identifier(name) or not _all_numeric(values):
            roles.append("dimension")
        else:
            roles.append("measure")
    return roles


def _is_time(name: str, values: Sequence[Any]) -> bool:
    lowered = name.lower()
    if any(hint in lowered for hint in _TIME_HINTS):
        return True
    # 列名没线索时看值：全是 ISO 日期样式的字符串，那它就是时间轴
    return bool(values) and all(
        isinstance(value, str) and _ISO_DATE.match(value) for value in values
    )


def _is_identifier(name: str) -> bool:
    """``id`` / ``order_id`` 这类标识列。

    **标识列永远不是度量**，哪怕它是整数。不这么判的话，``SELECT region_id,
    province, SUM(amount)`` 会得到"两个度量 + 一个维度"→ 退回表格，
    而它其实是一份正常的单维分组。更要紧的是明细类查询（``order_id,
    customer_id, amount``）：把 id 当度量，二维度一度量同样退回表格——
    结论碰巧对，但理由全错，下一个人改规则时就会改坏。

    只认 ``id`` / ``*_id`` 两种写法，覆盖本项目的 schema。中文别名（"订单编号"）
    不认——列名是模型生成的 SQL 里的别名，用它猜标识不可靠，且猜错的代价
    （把一个真度量当维度）比猜不中更大。
    """
    lowered = name.lower()
    return lowered == "id" or lowered.endswith("_id")


def _all_numeric(values: Sequence[Any]) -> bool:
    """整列都是数字才算度量。

    ``bool`` 不算：``True``/``False`` 在 Python 里是 int 的子类，
    不排除的话，"是否已支付"会被当成可以求和的度量。
    """
    return bool(values) and all(
        isinstance(value, _NUMERIC) and not isinstance(value, bool) for value in values
    )


def _looks_like_share(name: str, rows: Sequence[Sequence[Any]], index: int) -> bool:
    """判断这个度量是不是"占比"。

    两条线索，命中其一即可：

    1. **列名**（``占比`` / ``比例`` / ``ratio``……）—— 模型算占比时多半会这么起别名。
    2. **值的量纲**：非负、且加总约等于 1 或 100。

    **为什么要留第 2 条**：只认列名的话，能不能推对全看模型起什么别名；
    而"各区域销售额占比"这类问题的 SQL 天然就是 ``SUM(x)/total``，
    值必然加总为 1，这条比列名稳得多。实测中也确实靠它命中。

    **它会有误判，这里如实记下**：某个恰好加总为 1 或 100 的普通度量
    （"各区域销售额"恰好是 0.5/0.3/0.2 万元）会被认成占比而推荐饼图。
    从结果结构出发做到这一步已经到头了——要彻底避免只能看语义，
    而那正是这一段刻意不引入 LLM 的原因。误判的代价是把柱状图画成饼图，
    两者都能读，不构成错误答案。
    """
    lowered = name.lower()
    if any(hint in lowered for hint in _SHARE_HINTS):
        return True

    values = [
        float(row[index])
        for row in rows
        if index < len(row) and isinstance(row[index], _NUMERIC) and not isinstance(row[index], bool)
    ]
    if len(values) < 2 or any(value < 0 for value in values):
        return False
    total = sum(values)
    if total <= 0:
        return False
    # 小数口径（0.32）与百分数口径（32.0）都要认，容差按各自量纲缩放
    return (
        abs(total - 1) <= _SHARE_TOLERANCE
        or abs(total - 100) <= _SHARE_TOLERANCE * 100
    )
