"""阶段 11 验收：可观测性（《开发步骤文档》阶段 11 的三条标准）。

  1. 每次请求有 request_id 贯穿日志
  2. LLM 调用在 Langfuse 可追踪
  3. 节点耗时、token 消耗、召回命中数可统计

**三条标准共用一次真实问答**：跑三遍就是三次大模型调用、三倍等待，而且三次
之间的差异还会让人怀疑"是不是哪一次没埋上"。所以这里跑一次，把这一轮留下的
日志与 span 摊开给三条标准各自断言。

**验收跑的是真的链路**：真的 HTTP 入口、真的 Agent 图、真的 MySQL 与检索服务、
真的大模型。桩能验"埋点代码写对了"，验不了"一次真实问答是不是真的留下了这些数据"
——比如某个节点被跳过、某次调用用的是另一条分支，桩里都不会发生。

**数据不出境**：这里不接 Langfuse 客户端，span 全部导出到内存（见 conftest 里
对 ``LANGFUSE__ENABLED`` 的说明）。所以"LLM 调用在 Langfuse 可追踪"验的是
**契约**——span 上带着 Langfuse 认识的那几个属性名、且 Langfuse 的处理器确实
挂在我们这个 provider 上（``test_observability`` 里那条用例）。真往账号里发一条
的端到端检查是手工做的，理由见本模块末尾的说明。

外部服务没起时整体跳过（见 conftest.py）；跳过只针对"服务没监听"，
服务在跑但结果不对，必须失败。
"""

from __future__ import annotations

import json
from types import SimpleNamespace

import httpx
import pytest
from loguru import logger
from opentelemetry import trace
from opentelemetry.sdk.trace import TracerProvider
from opentelemetry.sdk.trace.export import SimpleSpanProcessor
from opentelemetry.sdk.trace.export.in_memory_span_exporter import InMemorySpanExporter

from app.core.request_context import REQUEST_ID_HEADER
from app.core.security import get_current_user
from app.core.tracing import (
    ATTR_GEN_AI_INPUT_TOKENS,
    ATTR_GEN_AI_MODEL,
    ATTR_GEN_AI_OUTPUT_TOKENS,
    ATTR_NODE_DURATION_MS,
    ATTR_NODE_NAME,
    ATTR_OBSERVATION_METADATA_PREFIX,
    ATTR_OBSERVATION_TYPE,
    ATTR_OBSERVATION_USAGE,
    ATTR_RECALL_PREFIX,
    ATTR_REQUEST_ID,
    ATTR_TRACE_METADATA_PREFIX,
    reset_tracing,
)
from main import create_app

pytestmark = [pytest.mark.requires_stack, pytest.mark.requires_llm]

BASE_URL = "http://test"
QUERY_PATH = "/api/v1/query"
ACCEPTANCE_QUESTION = "去年华东区销售额前三的省份"

FAKE_USER = SimpleNamespace(id="u-obs", username="observer", role="analyst", is_active=True)

#: 一条 span 记录够不够用的下限。真实问答会打出十几条（召回、筛选、生成 SQL、
#: 执行、落库），取 5 只是"确实贯穿了整条链路"而不是"入口打了一条"。
MIN_TAGGED_LOGS = 5


@pytest.fixture(scope="module")
def exported_spans():
    """模块级的内存导出器：整个模块共用一个 provider，用来装这一次问答的 span。

    自己装而不复用 conftest 的 ``otel_spans``：那个是函数级的（每条用例一份干净
    的导出器），而这里三个用例要看的是**同一批** span。在夹具里装 provider 也
    正是为了赶在 ``create_app`` 之前——``setup_tracing`` 会复用已有的 provider，
    于是 FastAPI 原生 telemetry 与 Agent 节点写的是同一个（见 tracing 的模块说明）。
    """
    reset_tracing()
    exporter = InMemorySpanExporter()
    provider = TracerProvider()
    provider.add_span_processor(SimpleSpanProcessor(exporter))
    trace.set_tracer_provider(provider)
    try:
        yield exporter
    finally:
        reset_tracing()


@pytest.fixture(scope="module")
async def run(exported_spans):
    """跑一次真实问答，留下这一轮的日志、span 与响应。

    写成 async 夹具是为了跑在**会话级**的事件循环里（配置见 pyproject 的
    ``asyncio_default_fixture_loop_scope``）：app.clients 下的引擎与客户端是按
    进程持有的单例、绑在创建它的那个循环上，自己新建一个循环会让它们变成
    "绑在别的循环上"的僵尸——症状是连接超时，而不是报错。

    日志 sink 在 ``create_app()`` **之后**才挂：``setup_logging`` 会先
    ``logger.remove()`` 清空所有 sink（那是它保证"重复调用只得到一份配置"的办法），
    早挂的 sink 会被它一并清掉，症状是"日志一条都没抓到"。
    """
    app = create_app()
    app.dependency_overrides[get_current_user] = lambda: FAKE_USER

    lines: list[str] = []
    sink = logger.add(
        lambda message: lines.append(message),
        format="{extra[request_id]}|{name}|{message}",
        level="INFO",
    )

    transport = httpx.ASGITransport(app=app)
    try:
        async with httpx.AsyncClient(
            transport=transport, base_url=BASE_URL, timeout=180.0
        ) as client:
            response = await client.post(QUERY_PATH, json={"query": ACCEPTANCE_QUESTION})
    finally:
        logger.remove(sink)
        app.dependency_overrides.pop(get_current_user, None)

    return SimpleNamespace(
        response=response,
        frames=_parse_sse(response.text),
        spans=exported_spans.get_finished_spans(),
        logs=lines,
    )


def _parse_sse(body: str) -> list[tuple[str, dict]]:
    frames: list[tuple[str, dict]] = []
    for block in body.strip().split("\n\n"):
        if not block.strip():
            continue
        lines = block.splitlines()
        name = next(line[len("event: ") :] for line in lines if line.startswith("event: "))
        data = next(line[len("data: ") :] for line in lines if line.startswith("data: "))
        frames.append((name, json.loads(data)))
    return frames


def _spans_named(spans, prefix: str) -> list:
    return [s for s in spans if s.name.startswith(prefix)]


def _find(spans, predicate):
    hits = [s for s in spans if predicate(s)]
    assert len(hits) == 1, f"期望恰好一条，实际 {len(hits)} 条：{[s.name for s in hits]}"
    return hits[0]


def _ancestors(span, by_id):
    seen = []
    current = span
    while current.parent is not None:
        current = by_id[current.parent.span_id]
        seen.append(current)
    return seen


# ==========================================================================
# 验收 1：每次请求有 request_id 贯穿日志
# ==========================================================================


def test_response_echoes_request_id(run) -> None:
    """id 要给得出来。只存在于服务端日志里的 id 等于没有 id。"""
    assert run.response.status_code == 200, run.response.text
    assert not [data for name, data in run.frames if name == "error"]
    assert len(run.response.headers[REQUEST_ID_HEADER]) == 16


def test_request_id_threads_through_every_log_of_the_request(run) -> None:
    """这一轮请求打出的日志，凡是带 id 的必须是同一个 id。

    断言"非 ``-`` 的值集合只有一个元素"，而不是"每条日志都有 id"：
    模块级 sink 也会收到夹具与别的用例打出的日志，那些本就不在请求里
    （id 是 ``-`` ），不该算进这条标准。
    """
    request_id = run.response.headers[REQUEST_ID_HEADER]
    tagged = [line.split("|", 1)[0] for line in run.logs]
    in_request = {value for value in tagged if value != "-"}

    assert in_request == {request_id}, f"日志里出现了别的 id：{in_request - {request_id}}"
    assert tagged.count(request_id) >= MIN_TAGGED_LOGS, (
        f"只有 {tagged.count(request_id)} 条日志带 id，链路大概没串起来"
    )


def test_request_id_is_searchable_in_the_trace(run) -> None:
    """id 同时落在服务端 span 与 trace metadata 上——日志与链路由此可以互查。

    metadata 那一条是"在 Langfuse 里按 request_id 搜到那次请求"的依据：
    它挂在根 span 上，Langfuse 会把整个请求的 span 归到同一个 trace。
    """
    request_id = run.response.headers[REQUEST_ID_HEADER]
    server = _find(run.spans, lambda s: s.name == f"POST {QUERY_PATH}")

    assert server.attributes[ATTR_REQUEST_ID] == request_id
    assert server.attributes["langfuse.trace.metadata.request_id"] == request_id


# ==========================================================================
# 验收 3：节点耗时、token 消耗、召回命中数可统计
# ==========================================================================


def test_every_node_span_carries_its_duration(run) -> None:
    """"慢在哪"的全部依据：每个节点一条 span，每条都带耗时。

    断言的是"每一个节点都有"，不是"某些节点有"——漏一个节点的耗时在面板上
    表现为某一段凭空消失，而那不是"这一段很快"，是"这一段没量到"。
    """
    nodes = _spans_named(run.spans, "node.")
    assert len(nodes) >= 5, f"节点 span 只有 {len(nodes)} 条，埋点大概没装全"

    missing = [
        s.attributes.get(ATTR_NODE_NAME)
        for s in nodes
        if ATTR_NODE_DURATION_MS not in s.attributes
    ]
    assert not missing, f"这些节点没有耗时：{missing}"
    assert all(isinstance(s.attributes[ATTR_NODE_DURATION_MS], int) for s in nodes)


def test_recall_counts_are_recorded_on_node_spans(run) -> None:
    """召回管道的每一站都要留下命中数，不能只有"最终召回了几条"。

    只看最终召回数的话，"召回了 200 条、筛完剩 2 条"与"本来就只召回 2 条"
    在面板上是同一条线，而这两件事该改的地方完全不同。
    """
    recorded: set[str] = set()
    for span in _spans_named(run.spans, "node."):
        recorded.update(k for k in span.attributes if k.startswith(ATTR_RECALL_PREFIX))

    # 三路召回 + 合并 + 两个筛选 + 结果行数，见 agent.events.COUNTED_KEYS
    expected = {
        f"{ATTR_RECALL_PREFIX}{kind}"
        for kind in (
            "columns",
            "values",
            "metrics",
            "retrieved",
            "tables",
            "selected_metrics",
            "rows",
        )
    }
    assert expected <= recorded, f"这些指标没记上：{sorted(expected - recorded)}"


def test_recall_counts_are_readable_as_langfuse_metadata(run) -> None:
    """每个命中数在 Langfuse 侧也有一份，名字是 Langfuse 认的那一个。

    中性名（``agent.recall.*``）Langfuse 也收，但只当作"不认识的属性"归进
    ``attributes.`` 杂物桶（实测：``agent.recall.probe`` 落成
    ``metadata.attributes.agent.recall.probe``）。缺了元数据那一份，面板上
    这一列就叫 ``attributes.agent.recall.columns``——与 ``attributes.http.route``、
    ``resourceAttributes.*`` 这些框架自己的东西挤在同一个兜底命名空间里。
    名字对不上还是小事，把查询绑在一个明说是兜底的命名上才是问题。

    这里断言"中性名出现的地方必有孪生名"，而不是数一数有几个：写在哪个节点上
    是埋点实现的事，这份契约只关心两个名字成对出现。
    """
    nodes = _spans_named(run.spans, "node.")
    neutral = {
        key for span in nodes for key in span.attributes if key.startswith(ATTR_RECALL_PREFIX)
    }
    # 先确认真的有数——否则下面的循环在"一个都没埋"时也会空转通过
    assert len(neutral) >= 7, f"节点上只记了 {len(neutral)} 个命中数：{sorted(neutral)}"

    missing = [
        (span.name, key)
        for span in nodes
        for key in span.attributes
        if key.startswith(ATTR_RECALL_PREFIX)
        and f"{ATTR_OBSERVATION_METADATA_PREFIX}{key}" not in span.attributes
    ]
    assert not missing, f"这些命中数在 Langfuse 侧看不到：{missing}"


def test_turn_summary_is_readable_as_langfuse_trace_metadata(run) -> None:
    """整轮汇总同时写在 Langfuse 的 trace metadata 上，键名是一级的。

    与命中数同理：不写这一份不是"看不见"——普通属性会被归进 ``attributes.``
    杂物桶（实测）。写 ``langfuse.trace.metadata.*`` 才会落成 ``agent.duration_ms``
    这样的一级元数据键，这也是 SDK 自己在 ``propagate_attributes`` 里往 trace 上
    挂元数据时用的那套键。

    顺带钉住"汇总落在根 span 上"。实测（父 span 与子 span 各写一个 trace metadata
    键再读回）：**只有根 span 上写的那个还在**，子 span 上写的被静默丢弃。
    所以位置落错时，OTel 一侧照旧读得到属性、Langfuse 一侧什么都不剩——
    只看本仓库的断言发现不了，必须靠这条把它钉死。
    """
    summary = _find(run.spans, lambda s: "agent.duration_ms" in s.attributes)
    assert summary.parent is None, f"汇总落在了 {summary.name}，Langfuse 不会读它的 trace metadata"

    for key in ("agent.duration_ms", "agent.outcome", "agent.llm.input_tokens"):
        assert summary.attributes[f"{ATTR_TRACE_METADATA_PREFIX}{key}"] == summary.attributes[key]


def test_per_request_summary_is_recorded(run) -> None:
    """整轮汇总落在一条 span 上：总耗时、结论、token 用量、命中数。

    单看节点看不出"这一轮一共花了多少、烧了多少 token"，而这两个数正是
    面板首页要显示的东西。断言"恰好一条"，多于一条说明埋点被重复触发过一次
    （比如重试路径上又汇总了一遍），那会让总数翻倍。
    """
    summary = _find(run.spans, lambda s: "agent.duration_ms" in s.attributes)

    assert summary.attributes["agent.duration_ms"] >= 0
    assert summary.attributes["agent.outcome"] == "ok"
    assert summary.attributes["agent.llm.calls"] >= 1
    assert summary.attributes["agent.llm.input_tokens"] > 0
    assert summary.attributes["agent.llm.output_tokens"] > 0
    # 0 次未采集用量时这条属性**不写**（留空而不是写 0 是同一条规矩），所以用 get
    assert summary.attributes.get("agent.llm.unknown_usage_calls", 0) == 0
    # 命中数与节点上那批是同一张表算出来的，这里能拿到说明"整轮终态"也数了一遍
    assert summary.attributes[f"{ATTR_RECALL_PREFIX}rows"] >= 1


# ==========================================================================
# 验收 2：LLM 调用在 Langfuse 可追踪
# ==========================================================================


def test_llm_call_is_recorded_as_a_langfuse_generation(run) -> None:
    """每次 LLM 调用在链路上是一条 generation，带模型名与 token 用量。

    ``langfuse.observation.type=generation`` 是 Langfuse 把一个 span 认成
    "模型调用"的唯一依据（因此它才会去算费用、才会出现在调用列表里）；
    usage_details 是它算费用的输入。名字带上节点（``generate_sql.llm``）
    是为了在调用列表里分得清哪次在做什么。
    """
    generations = [
        s for s in run.spans if s.attributes.get(ATTR_OBSERVATION_TYPE) == "generation"
    ]
    assert generations, "一条 generation 都没有，LLM 调用没有埋点"

    for span in generations:
        assert span.name.endswith(".llm"), f"{span.name} 没带上节点名"
        assert span.attributes[ATTR_GEN_AI_MODEL], "没有模型名，Langfuse 上认不出模型"
        assert ATTR_OBSERVATION_USAGE in span.attributes, "没有用量，Langfuse 算不出费用"
        assert span.attributes[ATTR_GEN_AI_INPUT_TOKENS] > 0
        assert span.attributes[ATTR_GEN_AI_OUTPUT_TOKENS] > 0


def test_generation_is_in_the_same_tree_as_the_request(run) -> None:
    """generation 长在这次请求的 span 树下，而不是自成一条 trace。

    这正是"只建一个 provider、Langfuse 挂在它上面"换来的东西：各建一个 provider
    的话，Langfuse 上看到的是一个个孤立的节点，而"这次问答慢在哪"就答不出来了。
    """
    server = _find(run.spans, lambda s: s.name == f"POST {QUERY_PATH}")
    by_id = {s.get_span_context().span_id: s for s in run.spans}

    for generation in run.spans:
        if generation.attributes.get(ATTR_OBSERVATION_TYPE) != "generation":
            continue
        assert generation.get_span_context().trace_id == server.get_span_context().trace_id, (
            f"{generation.name} 自成一条 trace"
        )
        chain = _ancestors(generation, by_id)
        assert server in chain, (
            f"{generation.name} 不在服务端 span 底下：" + " <- ".join(s.name for s in chain)
        )


def test_prompt_and_result_stay_off_the_span_by_default(run) -> None:
    """``capture_content`` 默认关着：业务问题原文与生成的 SQL 一个字节都不出境。

    这条验收是**反向**的——它验的不是"能看见"，而是"默认看不见"。
    没有它，"默认不传内容"这个决定就只存在于配置文件的注释里。
    """
    for span in run.spans:
        for key in span.attributes:
            assert not key.startswith("langfuse.observation.input"), (
                f"{span.name} 上出现了提示词，capture_content 应当是关的"
            )
            assert not key.startswith("langfuse.observation.output"), (
                f"{span.name} 上出现了模型输出，capture_content 应当是关的"
            )
    # 结构信息照旧：关的是内容，不是观测
    assert any(
        s.attributes.get(ATTR_OBSERVATION_TYPE) == "generation" for s in run.spans
    )
