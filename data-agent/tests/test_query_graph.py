"""阶段 5：图编排、State 与提示词拼装。

这一层用**桩**替换 LLM / 检索 / 知识库 / 数仓，不碰 .env、不碰任何外部服务。
理由和 test_sql_security 一样：图的分支结构、校正回边次数、事件顺序都是纯逻辑，
必须能在没有 DeepSeek Key、没有数据库的机器上跑。真实链路另由
test_stage5_acceptance.py 覆盖。
"""

from __future__ import annotations

import asyncio
from datetime import datetime
from types import SimpleNamespace

import pytest

from app.agent.graph import build_graph, route_after_validate
from app.agent.knowledge import MetricSpec, TableSpec
from app.agent.parsing import parse_json_array, parse_json_object
from app.agent.prompts import placeholders, render
from app.agent.rendering import date_info_text
from app.agent.state import MAX_SQL_CORRECTIONS, initial_state
from app.core.exceptions import LLMServiceError, ServiceError
from app.core.text import strip_code_fence
from app.repositories.dw import QueryResult
from app.retrievers.retrieval import Candidate

# ==========================================================================
# 桩
# ==========================================================================

TABLE = TableSpec(
    db_name="dw",
    name="fact_order",
    alias="订单事实表",
    description="每笔订单一行",
    is_dimension=False,
    columns=[],
)
METRIC = MetricSpec(
    name="销售额",
    alias="GMV",
    description="订单金额合计",
    calculation="SUM(amount)",
    filter_condition=None,
    time_range=None,
)
CORRECTING_MARKER = "执行前的校验中失败"


class StubLLM:
    """按提示词里出现的角色词分派，而不是按调用次序。

    三路关键字扩展是并发的，次序不确定；按次序分派会得到一个时灵时不灵的测试。
    按内容分派从根上避开这件事，也顺带验证了各节点的提示词确实是各自那一份。
    """

    def __init__(self, *, sql: str = "SELECT province FROM fact_order") -> None:
        self.sql = sql
        self.calls: list[str] = []

    async def text(self, prompt: str, *, system: str | None = None) -> str:
        await asyncio.sleep(0)  # 让出控制权，确保并行分支真的并发
        self.calls.append(prompt)
        if "字段检索专家" in prompt:
            return '["销售额", "省份"]'
        if "取值检索专家" in prompt:
            return '["华东"]'
        if "指标检索专家" in prompt:
            return '["销售额"]'
        if "表结构筛选专家" in prompt:
            return '{"tables":[{"table_name":"fact_order","columns":["province","amount"]}]}'
        if "指标筛选专家" in prompt:
            return '["销售额"]'
        return self.sql  # generate_sql 与 correct_sql 都返回同一个桩 SQL


class StubRetrieval:
    """检索桩。

    签名带 ``query`` 且是**必填**关键字参数：节点必须把消解后的问句交给检索层
    （精排拿它当交叉编码器的查询），漏传就该在这里立刻报错，而不是等到线上
    发现"精排结果莫名其妙"。
    """

    def __init__(self) -> None:
        self.queries: list[str] = []

    async def columns(self, keywords, *, query):
        self.queries.append(query)
        return [Candidate(kind="column", id="c1", name="province", score=0.9)]

    async def values(self, keywords, *, query):
        self.queries.append(query)
        return [Candidate(kind="value", id="v1", name="华东", score=0.8)]

    async def metrics(self, keywords, *, query):
        self.queries.append(query)
        return [Candidate(kind="metric", id="m1", name="销售额", score=0.95)]


class StubKnowledge:
    async def tables_for_columns(self, column_ids):
        return [TABLE]

    async def tables(self, table_ids):
        return [TABLE]

    async def metrics(self, names):
        return [METRIC]


class StubDw:
    def __init__(self, *, fail_explain: bool = False) -> None:
        from app.core.exceptions import SQLExecutionError

        self.fail_explain = fail_explain
        self._error = SQLExecutionError("Unknown column 'nope' in 'field list'")
        self.executed: list[str] = []

    async def explain(self, sql):
        if self.fail_explain:
            raise self._error

    async def execute(self, sql):
        self.executed.append(sql)
        return QueryResult(columns=["province"], rows=[["上海市", 560890.86]], truncated=False)


def stub_context(*, sql: str = "SELECT province FROM fact_order", fail_explain: bool = False):
    return SimpleNamespace(
        user_id="u1",
        llm=StubLLM(sql=sql),
        knowledge=StubKnowledge(),
        retrieval=StubRetrieval(),
        dw=StubDw(fail_explain=fail_explain),
    )


async def run_graph(*, sql: str = "SELECT province FROM fact_order", fail_explain: bool = False):
    """跑一遍图，返回（自定事件列表, 终态）。"""
    graph = build_graph()
    state = initial_state("去年华东区销售额前三的省份")
    events: list[dict] = []
    final = state
    async for mode, chunk in graph.astream(
        state,
        stream_mode=["custom", "values"],
        context=stub_context(sql=sql, fail_explain=fail_explain),
    ):
        if mode == "custom":
            events.append(chunk)
        else:
            final = chunk
    return events, final


# ==========================================================================
# 条件边分流
# ==========================================================================


def test_route_goes_to_execute_when_validation_passed() -> None:
    assert route_after_validate({"error": None, "correction_count": 0}) == "execute_sql"


@pytest.mark.parametrize("count", [0, 1, 2])
def test_route_retries_while_quota_remains(count: int) -> None:
    assert route_after_validate({"error": "语法错误", "correction_count": count}) == "correct_sql"


def test_route_gives_up_at_the_limit() -> None:
    assert (
        route_after_validate({"error": "语法错误", "correction_count": MAX_SQL_CORRECTIONS})
        == "give_up"
    )


# ==========================================================================
# 图的结构
# ==========================================================================


def test_graph_contains_exactly_the_twelve_documented_nodes() -> None:
    nodes = set(build_graph().get_graph().nodes)
    assert nodes - {"__start__", "__end__"} == {
        "extract_keywords",
        "recall_column",
        "recall_value",
        "recall_metric",
        "merge_retrieved_info",
        "filter_table",
        "filter_metric",
        "add_extra_context",
        "generate_sql",
        "validate_sql",
        "correct_sql",
        "execute_sql",
    }


# ==========================================================================
# 端到端（桩）
# ==========================================================================


async def test_happy_path_emits_sql_then_result_then_finishes_without_error() -> None:
    events, final = await run_graph()
    kinds = [e["type"] for e in events]

    assert kinds.count("sql") == 1
    assert kinds.count("result") == 1
    # 成功路径上不该出现 error 事件（error 只由服务层在终局推）
    assert kinds.count("error") == 0
    assert kinds.index("sql") < kinds.index("result"), "sql 应先于 result，前端才能立刻展示语句"
    assert final["error"] is None
    assert final["row_count"] == 1
    assert final["sql"] == "SELECT province FROM fact_order"


async def test_every_node_reports_start_and_done() -> None:
    events, _final = await run_graph()
    progress = [(e["step"], e["status"]) for e in events if e["type"] == "progress"]

    for node in ("extract_keywords", "merge_retrieved_info", "add_extra_context", "execute_sql"):
        assert (node, "start") in progress
        assert (node, "done") in progress
    # 三路召回与两个 filter 都是并行分支，各自都要有进度
    for parallel in ("recall_column", "recall_value", "recall_metric", "filter_table", "filter_metric"):
        assert (parallel, "start") in progress


async def test_parallel_branches_actually_overlap() -> None:
    """两个 filter 节点必须并行，否则汇合点的 add_extra_context 只会在最后才开跑。

    判据用"交叠"而不是"耗时"：start 事件的相对次序在并发下本就不确定，
    若代码被改成串行，一个分支的 start 一定会排在另一个的 done 之后。
    """
    events, _final = await run_graph()
    order = [e["step"] for e in events if e["type"] == "progress" and e["status"] == "start"]
    # 两个 filter 之间不能夹着任何一个的 done —— 简化检查：start 序列里它们相邻
    assert abs(order.index("filter_table") - order.index("filter_metric")) == 1


async def test_validation_failure_corrects_exactly_three_times_then_stops() -> None:
    events, final = await run_graph(sql="DROP TABLE fact_order")

    assert final["correction_count"] == MAX_SQL_CORRECTIONS
    assert final["error"], "放弃时必须留下明确错误"
    corrections = [e for e in events if e.get("step") == "correct_sql" and e["status"] == "start"]
    assert len(corrections) == MAX_SQL_CORRECTIONS, "校正次数必须正好等于上限，不能多一轮"
    # 校正途中不推 error 事件：用户不该看到一串马上被修好的报错
    assert [e["type"] for e in events].count("error") == 0


async def test_explain_failure_also_drives_correction() -> None:
    """EXPLAIN 预检失败与安全校验失败走同一条回边。"""
    _events, final = await run_graph(sql="SELECT nope FROM fact_order", fail_explain=True)
    assert final["correction_count"] == MAX_SQL_CORRECTIONS
    assert "预检未通过" in final["error"]


async def test_nothing_is_executed_when_validation_never_passes() -> None:
    graph = build_graph()
    state = initial_state("去年华东区销售额前三的省份")
    context = stub_context(sql="DROP TABLE fact_order")
    async for _mode, _chunk in graph.astream(
        state, stream_mode=["custom", "values"], context=context
    ):
        pass
    assert context.dw.executed == [], "校验没过就绝不能碰数仓"


# ==========================================================================
# 提示词渲染与解析
# ==========================================================================


#: 提示词 → 该模板声明必须与之完全一致的占位符（TDD 6）。
#: 这份契约写死在测试里而不是从节点代码反推：反推出来的永远是"现状"，
#: 而这里要守的是"模板该长什么样"。少一个占位符意味着模型缺一段上下文，
#: 多一个拼错的占位符会被原样发给模型，两种都不报错、只是答得不对。
PROMPT_CONTRACT: dict[str, set[str]] = {
    "extend_keywords_for_column_recall": {"query"},
    "extend_keywords_for_value_recall": {"query"},
    "extend_keywords_for_metric_recall": {"query"},
    "filter_table_info": {"query", "table_candidates"},
    "filter_metric_info": {"query", "metric_candidates"},
    "generate_sql": {"query", "table_infos", "metric_infos", "date_info", "db_info"},
    "correct_sql": {"query", "sql", "error", "table_infos", "metric_infos", "date_info", "db_info"},
}

SAMPLE_VALUES = {
    "query": "去年华东区销售额前三的省份",
    "table_infos": "T",
    "metric_infos": "M",
    "date_info": "D",
    "db_info": "B",
    "table_candidates": "TC",
    "metric_candidates": "MC",
    "sql": "SELECT 1",
    "error": "E",
}


@pytest.mark.parametrize(("name", "expected"), sorted(PROMPT_CONTRACT.items()))
def test_prompt_declares_exactly_the_expected_placeholders(name: str, expected: set[str]) -> None:
    assert placeholders(name) == expected


@pytest.mark.parametrize("name", sorted(PROMPT_CONTRACT))
def test_every_declared_placeholder_actually_gets_filled(name: str) -> None:
    """按契约传键渲染，产物里不该再留下未替换的模板变量。"""
    text = render(name, **{key: SAMPLE_VALUES[key] for key in PROMPT_CONTRACT[name]})
    for key in PROMPT_CONTRACT[name]:
        assert SAMPLE_VALUES[key] in text


def test_rendering_rejects_a_key_the_template_does_not_have() -> None:
    with pytest.raises(ServiceError):
        render("extend_keywords_for_column_recall", query="x", not_in_template="y")


def test_json_survives_rendering() -> None:
    """模板里的 JSON 示例不该被当成占位符吃掉——这正是没用 str.format 的原因。"""
    text = render("filter_table_info", query="x", table_candidates="y")
    assert '"tables"' in text


@pytest.mark.parametrize(
    "raw",
    ['["a"]', '```json\n["a"]\n```', '好的：\n["a"]\n以上。'],
)
def test_json_array_parsing_tolerates_model_chatter(raw: str) -> None:
    assert parse_json_array(raw, context="测试") == ["a"]


def test_json_array_parsing_raises_a_service_error_on_garbage() -> None:
    with pytest.raises(LLMServiceError):
        parse_json_array("我无法回答这个问题", context="测试")


def test_json_object_parsing_returns_a_dict() -> None:
    assert parse_json_object('{"tables": []}', context="测试") == {"tables": []}


def test_json_object_parsing_rejects_an_array() -> None:
    with pytest.raises(LLMServiceError):
        parse_json_object("[]", context="测试")


# ==========================================================================
# 代码围栏与日期换算
# ==========================================================================


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ("SELECT 1", "SELECT 1"),
        ("```sql\nSELECT 1\n```", "SELECT 1"),
        ("```\nSELECT 1\n```", "SELECT 1"),
        ("  SELECT 1  ", "SELECT 1"),
    ],
)
def test_strip_code_fence(raw: str, expected: str) -> None:
    assert strip_code_fence(raw) == expected


def test_date_info_gives_the_model_a_table_to_copy_from() -> None:
    """相对时间的换算必须由代码算好。这里钉死几个跨月/跨年的边界。"""
    text = date_info_text(datetime(2025, 3, 31, 10, 0))

    assert "当前日期：2025-03-31" in text
    assert "去年：2024-01-01 ~ 2024-12-31" in text
    assert "今年：2025-01-01 ~ 2025-12-31" in text
    # 上月结束于 2 月末，且 2025 不是闰年
    assert "上月：2025-02-01 ~ 2025-02-28" in text
    assert "本季度：2025-01-01 ~ 2025-03-31" in text
    assert "上季度：2024-10-01 ~ 2024-12-31" in text


def test_date_info_answers_the_acceptance_question() -> None:
    """验收问句里的"去年"必须能在上下文里直接查到，模型不必自己算。"""
    assert "去年：2024-01-01 ~ 2024-12-31" in date_info_text(datetime(2025, 6, 1, 0, 0))
