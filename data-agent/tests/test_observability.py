"""阶段 11 可观测性的单元测试（不依赖任何外部服务）。

覆盖的是"埋点本身对不对"，而不是"链路通不通"：request_id 的白名单与传播、
命中数怎么数、节点 span 的耗时与异常、generation 的内容闸与用量、导出前的擦除，
以及中间件把 id 回写到响应头。端到端的三条验收标准在 test_stage11_acceptance。

这里断言的是 span 的**属性名**（``agent.recall.columns`` 这类），不是某个后端
能看见的画面：这些名字是跨系统契约——Langfuse 与任何 OTLP 后端都按它们聚合。
写成对具体后端的断言，等于把"埋点对不对"绑死在那个后端的版本上。
"""

from __future__ import annotations

from contextvars import copy_context

import httpx
import pytest
from fastapi import FastAPI
from langfuse.types import MaskOtelSpansParams
from loguru import logger
from opentelemetry import trace
from opentelemetry.sdk.trace import TracerProvider
from opentelemetry.sdk.trace.export import SimpleSpanProcessor
from opentelemetry.sdk.trace.export.in_memory_span_exporter import InMemorySpanExporter
from opentelemetry.trace import StatusCode

from app.agent.events import count_hits
from app.api.middleware import register_request_id
from app.core.config import get_settings
from app.core.log_setup import REQUEST_ID_KEY, setup_logging
from app.core.request_context import (
    REQUEST_ID_HEADER,
    current_request_id,
    normalize_request_id,
    reset_request_id,
    set_request_id,
)
from app.core.tracing import (
    ATTR_GEN_AI_INPUT_TOKENS,
    ATTR_GEN_AI_MODEL,
    ATTR_GEN_AI_OUTPUT_TOKENS,
    ATTR_NODE_DURATION_MS,
    ATTR_NODE_NAME,
    ATTR_OBSERVATION_METADATA,
    ATTR_OBSERVATION_MODEL,
    ATTR_OBSERVATION_INPUT,
    ATTR_OBSERVATION_OUTPUT,
    ATTR_OBSERVATION_TYPE,
    ATTR_OBSERVATION_USAGE,
    ATTR_REQUEST_ID,
    ATTR_TRACE_METADATA_PREFIX,
    SPAN_NAME_LLM,
    _mask_otel_spans,
    llm_generation,
    node_span,
    recall_attributes,
    record_generation_result,
    reset_tracing,
    setup_tracing,
    telemetry_config,
)


@pytest.fixture
def spans(otel_spans):
    """本模块只关心"span 落在内存里"这一件事，夹具本体见 conftest.otel_spans。"""
    return otel_spans


def _span_named(exporter: InMemorySpanExporter, name: str):
    for span in exporter.get_finished_spans():
        if span.name == name:
            return span
    raise AssertionError(
        f"没有找到名为 {name} 的 span，实际有："
        f"{[s.name for s in exporter.get_finished_spans()]}"
    )


# ==========================================================================
# request_id
# ==========================================================================


@pytest.mark.parametrize(
    "raw",
    ["abc123", "cli-supplied-1", "a.b_c:d", "x" * 64, "  padded-id  "],
)
def test_normalize_accepts_client_value(raw: str) -> None:
    """合规的值原样采用——用户想用自己的 id 串日志，这个需求要能满足。"""
    assert normalize_request_id(raw) == raw.strip()


@pytest.mark.parametrize(
    "raw",
    [
        None,
        "",
        "   ",
        "with space",  # 空格不在白名单里
        "line\nbreak",  # 换行能伪造出第二条日志（日志伪造）
        "carriage\rreturn",
        "x" * 65,  # 超长会把日志行顶掉
        "中文",
    ],
)
def test_normalize_rejects_and_regenerates(raw: str | None) -> None:
    """不合规的值不报错，换一个自己生成的。

    **不拒绝请求**是刻意的：request_id 是排障线索，不是安全边界，
    为一个不合规的头拒掉一次问数，代价远大于换一个 id。
    """
    generated = normalize_request_id(raw)
    assert generated != raw
    assert len(generated) == 16
    assert generated.isalnum()


def test_generated_ids_differ() -> None:
    assert len({normalize_request_id(None) for _ in range(200)}) == 200


def test_current_request_id_default_and_isolation() -> None:
    """默认是空串（不是 None），且不同上下文互不干扰。"""
    assert current_request_id() == ""

    def other_context() -> str:
        token = set_request_id("only-in-this-context")
        try:
            return current_request_id()
        finally:
            reset_request_id(token)

    assert copy_context().run(other_context) == "only-in-this-context"
    # 另一个上下文里设的值不会漏回当前上下文
    assert current_request_id() == ""


def test_set_and_reset_restores_previous() -> None:
    outer = set_request_id("outer")
    inner = set_request_id("inner")
    assert current_request_id() == "inner"
    reset_request_id(inner)
    assert current_request_id() == "outer"
    reset_request_id(outer)
    assert current_request_id() == ""


# ==========================================================================
# 命中数
# ==========================================================================


def test_count_hits_counts_lists_and_ints() -> None:
    counts = count_hits(
        {
            "column_candidates": [1, 2, 3],
            "value_candidates": [],
            "metric_candidates": ["a"],
            "retrieved": [{"id": i} for i in range(12)],
            "table_infos": ["t1", "t2"],
            "metric_infos": ["m1"],
            "row_count": 37,
        }
    )
    assert counts == {
        "columns": 3,
        "values": 0,
        "metrics": 1,
        "retrieved": 12,
        "tables": 2,
        "selected_metrics": 1,
        "rows": 37,
    }


def test_count_hits_ignores_bool_and_unknown() -> None:
    """``truncated=True`` 不能被数成 1——bool 是 int 的子类，不挡住就会混进来。

    "这一路召回了 1 条"与"结果被截断了"在面板上是两件事，
    混在一起会让人以为召回质量出了问题。
    """
    counts = count_hits({"row_count": True, "some_other_key": [1, 2, 3]})
    assert counts == {}


def test_count_hits_tolerates_non_dict_and_missing() -> None:
    """节点返回值不一定含全部字段，甚至不是 dict（异常路径上可能是 None）。"""
    assert count_hits(None) == {}
    assert count_hits([1, 2]) == {}
    assert count_hits({"row_count": None}) == {}


def test_recall_attributes_uses_own_prefix() -> None:
    """每个命中数写两个名字：本项目中性的一个，加 Langfuse 认的一个。

    ``langfuse.observation.metadata.`` 那一份不是"顺便多写一遍"，也不是
    "不写就看不见"：实测 Langfuse 收普通属性，只是归到 ``attributes.`` 杂物桶里。
    它换来的是干净的一级元数据键。中性名字留着是因为它才是通用 OTLP 后端与
    本仓库断言的依据。
    """
    attributes = recall_attributes({"columns": 3, "rows": 37})
    assert attributes == {
        "agent.recall.columns": 3,
        "agent.recall.rows": 37,
        "langfuse.observation.metadata.agent.recall.columns": 3,
        "langfuse.observation.metadata.agent.recall.rows": 37,
    }


def test_recall_attributes_coerces_to_int() -> None:
    """值必须是 OTel 认的标量：``numpy.int64`` 之类的"看起来像 int"会让导出整批失败。"""
    attributes = recall_attributes({"columns": 3})
    assert isinstance(attributes["agent.recall.columns"], int)
    assert isinstance(attributes["langfuse.observation.metadata.agent.recall.columns"], int)


def test_our_langfuse_attribute_names_match_the_sdk() -> None:
    """我们手写的 Langfuse 属性名，与 SDK 自己认的名字逐字相同。

    这些字符串是**跨系统契约**（``tracing`` 的模块说明里解释了为什么写成字面量
    而不 import 常量）。字面量的代价就在这里补回来：改错一个字母不会有任何报错，
    只会在 Langfuse 界面上静默少一列——而那种缺失看上去像"业务本来就没这个数"。

    这里 import SDK 的私有路径是**故意**的：常量类搬家的那天，这条用例会直接
    报导入错误，而不是继续对着一个已经不存在的约定说"通过"。
    """
    from langfuse._client.attributes import LangfuseOtelSpanAttributes as Sdk

    assert ATTR_OBSERVATION_TYPE == Sdk.OBSERVATION_TYPE
    assert ATTR_OBSERVATION_INPUT == Sdk.OBSERVATION_INPUT
    assert ATTR_OBSERVATION_OUTPUT == Sdk.OBSERVATION_OUTPUT
    assert ATTR_OBSERVATION_MODEL == Sdk.OBSERVATION_MODEL
    assert ATTR_OBSERVATION_USAGE == Sdk.OBSERVATION_USAGE_DETAILS
    assert ATTR_OBSERVATION_METADATA == Sdk.OBSERVATION_METADATA
    assert ATTR_TRACE_METADATA_PREFIX == f"{Sdk.TRACE_METADATA}."


def test_metadata_prefix_is_how_the_sdk_flattens_it() -> None:
    """``langfuse.observation.metadata.<key>`` 这个拼法是 SDK 的规则，不是我们猜的。

    拿 SDK 自己那个把元数据摊平成属性的函数对一遍：同一个键，它给出的属性名
    必须与我们写上去的一模一样。这条比常量比对更有价值——常量只保证前缀对，
    这条保证"前缀 + 点 + 键"这个拼接方式也是对的。
    """
    from langfuse._client.attributes import _flatten_and_serialize_metadata

    sdk_attributes = _flatten_and_serialize_metadata(
        {"agent.recall.columns": 20}, "observation"
    )
    ours = recall_attributes({"columns": 20})
    key = "langfuse.observation.metadata.agent.recall.columns"

    assert key in sdk_attributes, sdk_attributes
    assert key in ours


# ==========================================================================
# 日志：request_id 贯穿
# ==========================================================================


@pytest.fixture
def log_lines():
    """抓 loguru 最终输出（含经 InterceptHandler 转过来的标准库记录）。"""
    lines: list[str] = []
    setup_logging("DEBUG")
    sink = logger.add(
        lambda message: lines.append(message),
        format="{extra[request_id]}|{name}|{function}:{line}|{message}",
    )
    try:
        yield lines
    finally:
        logger.remove(sink)


def test_request_id_reaches_stdlib_logging(log_lines) -> None:
    """标准库 logging 的记录经转接后也带上 request_id。

    这正是"接管而不是改用"的价值：项目里一百多处 ``logging.getLogger(__name__)``
    一个字没改，就都有 id 了。
    """
    import logging

    token = set_request_id("req-abc-1")
    try:
        logging.getLogger("app.some.module").warning("数据库连不上")
    finally:
        reset_request_id(token)

    assert any("req-abc-1" in line and "数据库连不上" in line for line in log_lines)


def test_request_id_is_dash_outside_request(log_lines) -> None:
    """不在请求里（命令行脚本、评测）时是 ``-``，而不是空串或 None。

    空串会让日志格式串里的分隔符挤在一起，``None`` 则看着像一个真的 id。
    """
    import logging

    logging.getLogger("app.cli").info("离线跑图")
    assert any(line.startswith("-|") for line in log_lines)


def test_explicit_bind_wins_over_context(log_lines) -> None:
    """显式 ``logger.bind`` 的值优先——后台任务要按自己的 id（如评测运行）聚合日志。"""
    logger.bind(request_id="eval-run-7").info("评测开始")
    assert any("eval-run-7" in line for line in log_lines)


def test_intercept_keeps_logger_name_and_real_call_site(log_lines) -> None:
    """转接后三样东西都要对：logger 名、函数名、行号。

    depth 算法的回归防线：早先那版会把每一行都写成 ``logging:callHandlers:1762``，
    看着像日志，实则把"哪里打的"整个丢了。logger 名则是另一条线——loguru 默认
    从调用帧的 ``__name__`` 推，uvicorn 那几个 logger 会被推成内部模块名，
    于是按 "uvicorn.access" 搜访问日志就搜不到了。
    """
    import logging

    logging.getLogger("app.some.module").info("定位我")
    hit = [line for line in log_lines if "定位我" in line]
    assert hit, "没有抓到这条日志"
    assert "|app.some.module|" in hit[0], hit[0]
    assert "test_intercept_keeps_logger_name_and_real_call_site" in hit[0]
    assert "callHandlers" not in hit[0]


def test_request_id_key_is_the_documented_one() -> None:
    """格式串里的 ``{extra[request_id]}`` 与 patcher 写入的键必须是同一个。"""
    assert REQUEST_ID_KEY == "request_id"


# ==========================================================================
# 节点 span
# ==========================================================================


def test_node_span_records_name_and_duration(spans) -> None:
    with node_span("recall_column") as span:
        span.set_attributes(recall_attributes(count_hits({"column_candidates": [1, 2]})))

    recorded = _span_named(spans, "node.recall_column")
    assert recorded.attributes[ATTR_NODE_NAME] == "recall_column"
    assert recorded.attributes[ATTR_OBSERVATION_TYPE] == "span"
    assert recorded.attributes[ATTR_NODE_DURATION_MS] >= 0
    assert recorded.attributes["agent.recall.columns"] == 2


def test_node_span_duration_is_measured_inside(spans) -> None:
    """耗时量的是"节点真正在跑"的那一段，得是整数毫秒。"""
    import time

    with node_span("filter_table"):
        time.sleep(0.03)

    recorded = _span_named(spans, "node.filter_table")
    assert recorded.attributes[ATTR_NODE_DURATION_MS] >= 25


def test_node_span_marks_error_and_reraises(spans) -> None:
    """节点崩了要记在链路里，但**不改业务语义**——异常原样抛出。"""
    with pytest.raises(ValueError, match="炸了"):
        with node_span("generate_sql"):
            raise ValueError("炸了")

    recorded = _span_named(spans, "node.generate_sql")
    assert recorded.status.status_code is StatusCode.ERROR
    # 耗时在异常路径上同样要记：耗时缺失的 span 在面板上就是一条断线
    assert recorded.attributes[ATTR_NODE_DURATION_MS] >= 0


def test_node_span_carries_request_id(spans) -> None:
    token = set_request_id("req-in-node")
    try:
        with node_span("execute_sql"):
            pass
    finally:
        reset_request_id(token)

    assert _span_named(spans, "node.execute_sql").attributes[ATTR_REQUEST_ID] == "req-in-node"


def test_node_spans_nest(spans) -> None:
    """异步分支各自嵌套在自己的父 span 下——trace 树裂成两棵就没法回答"慢在哪"。"""
    with node_span("outer") as outer:
        with node_span("inner"):
            pass

    inner_span = _span_named(spans, "node.inner")
    assert inner_span.parent is not None
    assert inner_span.parent.span_id == outer.get_span_context().span_id


def test_node_span_without_provider_does_not_blow_up() -> None:
    """没装 provider 的进程（命令行脚本、评测）里埋点必须是空操作。

    这条也在守 ``_tracer()`` 不缓存 tracer 的决定：缓存下来的 tracer 会指向一个
    已经 shutdown 的旧 provider，换了 provider 之后 span 就悄悄丢了。
    """
    reset_tracing()
    try:
        with node_span("no_provider"):
            pass
    finally:
        # 装回一个干净的 provider（不带导出器），免得给后续用例留下"全局无 provider"
        trace.set_tracer_provider(TracerProvider())


# ==========================================================================
# LLM generation
# ==========================================================================


def _capture(monkeypatch: pytest.MonkeyPatch, enabled: bool) -> None:
    monkeypatch.setattr(get_settings().langfuse, "capture_content", enabled)


def test_generation_has_model_and_usage(spans, monkeypatch) -> None:
    _capture(monkeypatch, False)
    with llm_generation(model="deepseek-chat", prompt="列一下华东的销售额", system="你是助手") as span:
        record_generation_result(
            span, text="SELECT ...", input_tokens=120, output_tokens=30
        )

    recorded = _span_named(spans, SPAN_NAME_LLM)
    assert recorded.attributes[ATTR_OBSERVATION_TYPE] == "generation"
    assert recorded.attributes[ATTR_GEN_AI_MODEL] == "deepseek-chat"
    # 用量写两处：OTel 的通用字段给任何后端，usage_details 给 Langfuse 算费用
    assert recorded.attributes[ATTR_GEN_AI_INPUT_TOKENS] == 120
    assert recorded.attributes[ATTR_GEN_AI_OUTPUT_TOKENS] == 30
    assert '"input": 120' in recorded.attributes[ATTR_OBSERVATION_USAGE]
    assert '"total": 150' in recorded.attributes[ATTR_OBSERVATION_USAGE]


def test_generation_name_carries_node(spans, monkeypatch) -> None:
    """``generate_sql.llm`` 而不是一排分不清的 ``llm.chat``。"""
    _capture(monkeypatch, False)
    with node_span("generate_sql"):
        with llm_generation(model="m", prompt="p", system=None):
            pass

    _span_named(spans, "generate_sql.llm")


def test_content_is_not_written_when_capture_off(spans, monkeypatch) -> None:
    """默认这一侧是**根本不写进 span**（第一道闸），而不只是导出时擦掉。

    内容连 exporter 的批次内存都进不去，泄出的路径就少一条。
    """
    _capture(monkeypatch, False)
    with llm_generation(model="m", prompt="华东的销售额是多少", system="你是助手") as span:
        record_generation_result(span, text="SELECT SUM(x)", input_tokens=1, output_tokens=1)

    recorded = _span_named(spans, SPAN_NAME_LLM)
    assert ATTR_OBSERVATION_INPUT not in recorded.attributes
    assert ATTR_OBSERVATION_OUTPUT not in recorded.attributes
    assert "华东" not in str(recorded.attributes)


def test_content_is_written_when_capture_on(spans, monkeypatch) -> None:
    _capture(monkeypatch, True)
    with llm_generation(model="m", prompt="华东的销售额是多少", system="你是助手") as span:
        record_generation_result(span, text="SELECT SUM(x)", input_tokens=1, output_tokens=1)

    recorded = _span_named(spans, SPAN_NAME_LLM)
    assert recorded.attributes[ATTR_OBSERVATION_INPUT] == "华东的销售额是多少"
    assert recorded.attributes[ATTR_OBSERVATION_OUTPUT] == "SELECT SUM(x)"


def test_unknown_usage_stays_empty(spans, monkeypatch) -> None:
    """上游没回用量时**留空**，不写 0。

    0 会被聚合成"这次调用不花 token"，把平均值往下拉，而真相是"不知道"。
    """
    _capture(monkeypatch, False)
    with llm_generation(model="m", prompt="p", system=None) as span:
        record_generation_result(span, text="t", input_tokens=None, output_tokens=None)

    recorded = _span_named(spans, SPAN_NAME_LLM)
    assert ATTR_OBSERVATION_USAGE not in recorded.attributes
    assert ATTR_GEN_AI_INPUT_TOKENS not in recorded.attributes


def test_generation_marks_error(spans, monkeypatch) -> None:
    _capture(monkeypatch, False)
    with pytest.raises(TimeoutError):
        with llm_generation(model="m", prompt="p", system=None):
            raise TimeoutError("上游超时")

    assert _span_named(spans, SPAN_NAME_LLM).status.status_code is StatusCode.ERROR


# ==========================================================================
# 导出前的内容擦除（第二道闸）
# ==========================================================================


class _FakeSnapshot:
    """``MaskOtelSpansParams.spans`` 里的 span 快照（只需要 ``attributes``）。"""

    def __init__(self, **attributes: object) -> None:
        self.attributes = attributes


def test_mask_drops_content_attributes() -> None:
    """第一道闸只管得住我们自己写的代码；自动埋点顺手塞进来的内容由这一道兜住。"""
    params = MaskOtelSpansParams(
        spans={
            "span-a": _FakeSnapshot(
                **{
                    "langfuse.observation.input": "华东的销售额",
                    "langfuse.observation.output": "SELECT 1",
                    "langfuse.trace.input": "整轮的问题",
                    "gen_ai.prompt.0.content": "系统提示词",
                    "gen_ai.completion.0.content": "补全",
                    ATTR_GEN_AI_MODEL: "deepseek-chat",
                }
            )
        }
    )

    patch = _mask_otel_spans(params=params).span_patches["span-a"]
    assert set(patch.delete_attributes) == {
        "langfuse.observation.input",
        "langfuse.observation.output",
        "langfuse.trace.input",
        "gen_ai.prompt.0.content",
        "gen_ai.completion.0.content",
    }
    # 留个印记，事后能区分"被按策略抹掉"与"当时恰好没有内容"
    assert patch.set_attributes == {"app.content_redacted": True}
    # 结构信息不受影响：抹内容不等于放弃观测
    assert ATTR_GEN_AI_MODEL not in patch.delete_attributes


def test_mask_leaves_clean_spans_alone() -> None:
    params = MaskOtelSpansParams(
        spans={"span-b": _FakeSnapshot(**{ATTR_GEN_AI_MODEL: "deepseek-chat"})}
    )
    assert _mask_otel_spans(params=params).span_patches == {}


def test_mask_handles_empty_batch() -> None:
    """导出批次可能是空的（按时刷但期间没有新 span），不该因此报错。"""
    assert _mask_otel_spans(params=MaskOtelSpansParams(spans={})).span_patches == {}


# ==========================================================================
# 中间件与装配
# ==========================================================================


def _build_app(settings=None) -> FastAPI:
    """按 ``main.create_app`` 的方式组装一个最小应用（少掉路由与异常处理器）。

    接口层的埋点走 ``FastAPI(telemetry=...)``，**不在 add_middleware 里**：
    原生 telemetry 装在 ``__call__`` 中，比整个中间件栈还外一层。这里就是要
    验证这个偏序——request_id 中间件写属性时，服务端 span 必须已经存在。
    """
    app = FastAPI(telemetry=telemetry_config(settings or get_settings()))

    @app.get("/ping")
    async def ping() -> dict[str, bool]:
        return {"ok": True}

    register_request_id(app)
    return app


@pytest.fixture
def minimal_app() -> FastAPI:
    return _build_app()


async def _call(app: FastAPI, headers: dict[str, str] | None = None) -> httpx.Response:
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        return await client.get("/ping", headers=headers or {})


async def test_response_carries_request_id_header(spans, minimal_app) -> None:
    """用户报障时能把 id 给出来；只存在于服务端日志里的 id 等于没有 id。"""
    response = await _call(minimal_app, {REQUEST_ID_HEADER: "from-client-1"})

    assert response.headers[REQUEST_ID_HEADER] == "from-client-1"
    # 同时落到服务端 span 上：日志与链路由此可以互查
    assert _span_named(spans, "GET /ping").attributes[ATTR_REQUEST_ID] == "from-client-1"


async def test_unsafe_header_is_replaced(spans, minimal_app) -> None:
    response = await _call(minimal_app, {REQUEST_ID_HEADER: "bad\nvalue"})

    echoed = response.headers[REQUEST_ID_HEADER]
    assert echoed not in ("bad\nvalue", "")
    assert "\n" not in echoed
    assert len(echoed) == 16


async def test_request_id_generated_when_absent(spans, minimal_app) -> None:
    first = await _call(minimal_app)
    second = await _call(minimal_app)

    assert len(first.headers[REQUEST_ID_HEADER]) == 16
    assert first.headers[REQUEST_ID_HEADER] != second.headers[REQUEST_ID_HEADER]


async def test_trace_metadata_carries_request_id(spans, minimal_app) -> None:
    """Langfuse 从根 span 读 ``langfuse.trace.metadata.*`` 并挂到整条 trace 上。

    这一条是"在 Langfuse 里按 request_id 搜到那次请求"的全部依据。
    """
    response = await _call(minimal_app)

    server_span = _span_named(spans, "GET /ping")
    assert server_span.attributes["langfuse.trace.metadata.request_id"] == (
        response.headers[REQUEST_ID_HEADER]
    )


async def test_context_is_clean_after_request(spans, minimal_app) -> None:
    """漏掉 reset 的话，同一连接上的下一个请求会顶着上一个请求的 id。"""
    await _call(minimal_app, {REQUEST_ID_HEADER: "first"})

    assert current_request_id() == ""


async def test_node_spans_hang_under_the_server_span(spans) -> None:
    """节点 span 长在服务端 span 底下——"一次问答一棵树"就是靠这一条成立的。

    断言的是**祖先关系**，不是"父亲恰好是服务端 span"：中间还隔着 FastAPI 原生
    telemetry 建的 ``fastapi.endpoint``（端点 span），这是它的正常粒度。
    写死父亲是谁的测试会在 FastAPI 调一次埋点粒度时就红，而那种红没有意义。
    """
    app = _build_app()

    @app.get("/q")
    async def q() -> dict[str, bool]:
        with node_span("generate_sql"):
            pass
        return {"ok": True}

    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        await client.get("/q")

    server = _span_named(spans, "GET /q")
    node = _span_named(spans, "node.generate_sql")
    server_id = server.get_span_context().span_id

    # 同一个 trace id 是"在一棵树里"的前提；不在一棵树的表现是节点各自成一条 trace
    assert node.get_span_context().trace_id == server.get_span_context().trace_id

    # 顺着 parent 一路往上，必须走到服务端 span
    by_id = {s.get_span_context().span_id: s for s in spans.get_finished_spans()}
    current, hops = node, 0
    while current.get_span_context().span_id != server_id:
        assert current.parent is not None, f"{node.name} 的祖先链断在 {current.name}"
        current = by_id[current.parent.span_id]
        hops += 1
        assert hops <= 10, "span 嵌套过深，疑似成环"


# ==========================================================================
# 交给 FastAPI 原生 telemetry 的配置
# ==========================================================================


def test_telemetry_config_turns_off_environment_export() -> None:
    """``auto_configure`` 必须关掉。

    它默认为 True，会在启动时读环境里的 ``OTEL_EXPORTER_OTLP_ENDPOINT`` 之类
    自动挂一个导出器——等于"数据往哪儿发"由运行环境里一个我们不认识、也不会审的
    变量决定。本项目每一处出境都要在配置里写得出来（对比 Langfuse 的
    ``capture_content``）。要接自建 collector 走 ``observability.otlp_endpoint``。
    """
    config = telemetry_config(get_settings())
    assert config["auto_configure"] is False
    assert config["metrics"] is False
    assert config["logs"] is False


def test_telemetry_config_providers_come_from_setup_tracing() -> None:
    """传 None = 用全局 provider，也就是 ``setup_tracing`` 装的那个。

    写死一个 provider 会让接口层的 span 与 Agent 节点的 span 分成两棵树。
    """
    assert telemetry_config(get_settings())["tracer_provider"] is None


def test_telemetry_config_follows_global_switch() -> None:
    settings = get_settings().model_copy(deep=True)
    settings.observability.enabled = False
    config = telemetry_config(settings)
    assert config["tracing"] is False
    assert config["operation_spans"] is False


def test_telemetry_config_excludes_probes_only() -> None:
    """探针排除掉，业务路径一个都不能误伤。"""
    exclude = telemetry_config(get_settings())["exclude"]
    assert exclude({"path": "/health"})
    assert exclude({"path": "/health/ready"})
    assert not exclude({"path": "/api/v1/query"})
    assert not exclude({"path": "/healthz"})  # 前缀比对，不做子串匹配
    assert not exclude({})  # 没有 path 的 scope 不排除


def test_setup_tracing_reuses_existing_provider(spans) -> None:
    """已经装好 provider 的进程（宿主应用、测试）里必须复用。

    新建第二个是错的：两个 provider 各自的 span 互相看不见，trace 树会裂成两棵，
    症状是"Langfuse 里只有零散的节点，没有一次完整的问答"。
    """
    existing = trace.get_tracer_provider()
    setup_tracing(get_settings())
    assert trace.get_tracer_provider() is existing


def test_setup_tracing_is_idempotent(spans) -> None:
    settings = get_settings()
    setup_tracing(settings)
    before = list(trace.get_tracer_provider()._active_span_processor._span_processors)
    setup_tracing(settings)
    after = list(trace.get_tracer_provider()._active_span_processor._span_processors)
    assert len(after) == len(before)


def test_setup_tracing_skips_langfuse_when_unconfigured(spans, log_lines) -> None:
    """缺 key 只降级、不报错——但要说清楚，因为这种降级是无声的。

    页面正常、接口正常，只是 Langfuse 里空空如也；没有这条日志，
    排查方向会先跑到埋点代码上去。
    """
    import app.core.tracing as tracing_module

    settings = get_settings().model_copy(deep=True)
    settings.langfuse.public_key = ""
    settings.langfuse.secret_key = ""
    tracing_module._langfuse = None

    tracing_module.setup_tracing(settings)

    assert tracing_module._langfuse is None
    assert any("Langfuse 未启用或未配置" in line for line in log_lines)
    # 降级是"少一侧数据"，不是"什么都不埋"：OTel 那一半照常工作
    assert isinstance(trace.get_tracer_provider(), TracerProvider)


async def test_observability_disabled_produces_no_spans(spans) -> None:
    """``observability.enabled=false`` 时接口层一字节都不产生。

    这是给"只想跑测试"或"不想要观测"的部署留的开关，所以它必须是**不埋**，
    而不是"埋了但不上报"——后者在进程里仍然留着埋点的开销。
    """
    settings = get_settings().model_copy(deep=True)
    settings.observability.enabled = False
    app = _build_app(settings)

    response = await _call(app)

    # request_id 不属于 OTel：它照常回写响应头，日志也照常带 id
    assert len(response.headers[REQUEST_ID_HEADER]) == 16
    assert spans.get_finished_spans() == ()


# ==========================================================================
# Langfuse 与 OTel 是一条管线（本阶段最重要的结构决定）
# ==========================================================================


def test_langfuse_attaches_to_our_provider() -> None:
    """把 tracer_provider 交给 Langfuse，它只是往上面挂一个自己的处理器。

    若各建一个 provider，同一个请求的 span 会分成两棵树：Langfuse 上看到的节点
    是断的（每个节点各自成一条 trace），而"这条链路花了多久、慢在哪"正是它要
    回答的问题。这条测试是那个决定的回归防线——Langfuse 升级若改了行为，
    这里先红，而不是等到线上发现链路断了。

    用假 key 且当场 shutdown：这个测试不产生任何 span，因此不会有数据发往
    任何地方（Langfuse 是攒批发送的，没有 span 就没有批次）。
    """
    from langfuse import Langfuse

    provider = TracerProvider()
    client = Langfuse(
        public_key="pk-lf-test-not-real",
        secret_key="sk-lf-test-not-real",
        host="http://127.0.0.1:1",  # 不通的地址：即便意外发送也发不出去
        tracer_provider=provider,
        timeout=1,
    )
    try:
        processors = provider._active_span_processor._span_processors
        assert [type(p).__name__ for p in processors] == ["LangfuseSpanProcessor"]
    finally:
        client.shutdown()
