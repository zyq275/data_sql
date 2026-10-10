"""链路追踪：OpenTelemetry 作骨、Langfuse 作 LLM 观测面（NFR-OBS）。

**只有一条管线，不是两套**。Langfuse 的 Python SDK 从 v3 起自身就建在
OpenTelemetry 之上：把 ``tracer_provider`` 交给它，它只是往这个 provider 上挂一个
自己的 span 处理器——实测 ``provider._active_span_processor._span_processors``
里多出一个 ``LangfuseSpanProcessor``，而 span 仍由我们创建、仍走 OTel 的 API。
所以"接入 OpenTelemetry"与"接入 Langfuse"在代码里是同一件事的两面。

反过来说，若各建一个 provider，同一个请求的 span 会分成两棵树：Langfuse 上
看到的节点是断的（每个节点各自成为一条 trace），而"这条链路花了多久、慢在哪"
正是它要回答的问题。

这条管线上的三方各管一段，边界要说清楚：

| 谁 | 埋什么 | 在哪儿配 |
| --- | --- | --- |
| FastAPI 原生 telemetry | 服务端 span、依赖/端点/序列化 span | ``telemetry_config()`` |
| 本模块 | ``node.*`` 节点 span、LLM generation、召回命中数 | ``node_span`` / ``llm_generation`` |
| Langfuse | 把上面两种 span 变成可检索的 trace，并算 LLM 费用 | ``setup_tracing()`` 挂的处理器 |

**接口层那一段不再自己挂中间件**：FastAPI 0.128 起自带 OTel 埋点，而且装在
``FastAPI.__call__`` 里，比任何 ``add_middleware`` 都外一层。自己再装一套
``opentelemetry-instrumentation-fastapi`` 不但重复，还会让原生那套主动让位
（实测），拿到的是属性更旧、粒度更粗的一份——详见 ``telemetry_config``。

**降级是设计的一部分**（与 rerank、Redis 同一取舍，见 config.py 的说明）：
没配 key、开关关掉、SDK 初始化报错，都只让 Langfuse 那一侧缺席——span 照常产生、
进程照常服务，理由是"观测不到"绝不该升级成"服务不可用"。缺 key 时打一条日志
说清楚，因为这种降级是无声的：页面正常、接口正常，只是 Langfuse 里空空如也。

**内容是否上报由配置决定，且有两道闸**（见 ``capture_content``）：
第一道是"根本不写进 span"（最彻底，内容连 exporter 的批次内存都进不去），
第二道是导出前按属性名擦一遍（``_mask_otel_spans``），兜住将来某个自动埋点
顺手把提示词塞进 span 的情况。第二道存在的理由是第一道只管得住我们自己写的代码。
"""

from __future__ import annotations

import json
import logging
import time
from collections.abc import Iterator, Mapping
from contextlib import contextmanager
from contextvars import ContextVar
from typing import Any

from langfuse import Langfuse
from langfuse.span_filter import is_default_export_span
from langfuse.types import MaskOtelSpansParams, MaskOtelSpansResult, OtelSpanPatch
from opentelemetry import trace
from opentelemetry.sdk.resources import Resource
from opentelemetry.sdk.trace import TracerProvider
from opentelemetry.sdk.trace.export import BatchSpanProcessor
from opentelemetry.trace import Span, SpanKind, Status, StatusCode

from app.core.config import Settings, get_settings
from app.core.request_context import current_request_id

logger = logging.getLogger(__name__)

# Langfuse 认这批属性名（LangfuseOtelSpanAttributes 的取值）。写成字面量而不是
# 引入 SDK 的常量类，是为了让"我们往 span 上写了什么"在阅读时一眼可见——
# 这些字符串是跨系统契约，比一次 import 更值得留在代码里。
ATTR_OBSERVATION_TYPE = "langfuse.observation.type"
ATTR_OBSERVATION_INPUT = "langfuse.observation.input"
ATTR_OBSERVATION_OUTPUT = "langfuse.observation.output"
ATTR_OBSERVATION_MODEL = "langfuse.observation.model.name"
ATTR_OBSERVATION_USAGE = "langfuse.observation.usage_details"
ATTR_OBSERVATION_METADATA = "langfuse.observation.metadata"
ATTR_OBSERVATION_METADATA_PREFIX = "langfuse.observation.metadata."
ATTR_TRACE_METADATA_PREFIX = "langfuse.trace.metadata."

# OpenTelemetry 的 GenAI 语义约定（稳定字段）。用标准名而不是自定义名：
# 任何 OTLP 后端（不止 Langfuse）都认这几个。
ATTR_GEN_AI_MODEL = "gen_ai.request.model"
ATTR_GEN_AI_SYSTEM = "gen_ai.system"
ATTR_GEN_AI_OPERATION = "gen_ai.operation.name"
ATTR_GEN_AI_INPUT_TOKENS = "gen_ai.usage.input_tokens"
ATTR_GEN_AI_OUTPUT_TOKENS = "gen_ai.usage.output_tokens"

# 项目自己的命名空间。与上面两组分开：上面是"给别人看的"，这里是"给我们自己看的"。
ATTR_REQUEST_ID = "request.id"
ATTR_NODE_NAME = "agent.node.name"
ATTR_NODE_DURATION_MS = "agent.node.duration_ms"
ATTR_RECALL_PREFIX = "agent.recall."

#: 导出前一律抹掉的属性前缀（内容第二道闸，见模块说明）。
CONTENT_ATTRIBUTE_PREFIXES = (
    "langfuse.observation.input",
    "langfuse.observation.output",
    "langfuse.trace.input",
    "langfuse.trace.output",
    "gen_ai.prompt",
    "gen_ai.completion",
    "gen_ai.content",
)

SPAN_NAME_NODE = "node.{}"
SPAN_NAME_LLM = "llm.chat"

#: 探针路径（``/health``、``/health/ready``），见 ``telemetry_config``。
PROBE_PATH = "/health"

_langfuse: Langfuse | None = None
"""进程内一个 Langfuse 客户端。None 表示这一侧缺席（未配置或初始化失败）。"""

_node_name: ContextVar[str] = ContextVar("observability_node_name", default="")
"""当前正在执行的节点名。

用 contextvar 而不是去问 OTel span：并行分支（三路召回、两个 filter）各自跑在
拷贝出来的上下文里，这个变量天生就是分开的；而 span 上并没有"名字"这个可读字段
（``get_current_span()`` 返回的接口里只有 id）。LLM 的 generation 靠它取名字，
于是 Langfuse 里看到的是 ``generate_sql.llm`` 而不是一排分不清的 ``llm.chat``。
"""


# ==========================================================================
# 配置与生命周期
# ==========================================================================


def _capture_content() -> bool:
    """是否允许把提示词、SQL、结果等**内容**写进 span。

    每次现读配置而不是在 setup 时缓存一次：测试要能在同一个进程里来回切这个开关，
    而它是个安全开关——"改了配置但没生效"在这一项上的代价是数据出境。
    """
    return bool(get_settings().langfuse.capture_content)


def _resource(settings: Settings) -> Resource:
    # 用字面量而不是 semconv 的常量：``opentelemetry.sdk.resources.SERVICE_NAME``
    # 在 1.4x 里已经不再导出（迁去了 semconv 包），而这两个字符串本身是稳定的。
    return Resource.create(
        {
            "service.name": settings.app.name,
            "service.version": settings.app.version,
        }
    )


def _mask_otel_spans(*, params: MaskOtelSpansParams) -> MaskOtelSpansResult:
    """导出前的内容擦除（第二道闸）。

    在**导出阶段**做而不是在写 span 时做：这样连"某个第三方自动埋点自己塞进去的"
    内容也盖得住，而写 span 时我们只管得住自己。
    """
    patches: dict[object, OtelSpanPatch] = {}
    for identifier, span in params.spans.items():
        drop = tuple(
            key for key in span.attributes if key.startswith(CONTENT_ATTRIBUTE_PREFIXES)
        )
        if drop:
            patches[identifier] = OtelSpanPatch(
                delete_attributes=drop,
                # 留个印记：事后看到这一条就知道内容被按策略抹掉了，
                # 而不是"当时恰好没有内容"
                set_attributes={"app.content_redacted": True},
            )
    return MaskOtelSpansResult(span_patches=patches)


def _ensure_provider(settings: Settings) -> TracerProvider:
    """取（或建）全局 TracerProvider。

    若进程里已经装好了一个（宿主应用自带的，或测试里塞进来的内存导出器），
    就复用它。**新建第二个是错的**：两个 provider 各自的 span 互相看不见，
    trace 树会裂成两棵，而症状是"Langfuse 里只有零散的节点，没有一次完整的问答"。
    """
    existing = trace.get_tracer_provider()
    if isinstance(existing, TracerProvider):
        return existing

    provider = TracerProvider(resource=_resource(settings))
    endpoint = get_settings().observability.otlp_endpoint
    if endpoint:
        # 通用 OTLP 后端是可选项：配了就往那儿也送一份。这是"接入 OpenTelemetry"
        # 里**不依赖 Langfuse** 的那一半——Langfuse 挂了、换掉了、或者只想接
        # 自建的 collector，这条路上的数据一点不少。
        provider.add_span_processor(
            BatchSpanProcessor(_otlp_exporter(endpoint))
        )
    trace.set_tracer_provider(provider)
    return provider


def _otlp_exporter(endpoint: str):
    # 延迟 import：HTTP 版导出器只在使用时才需要它的传输依赖，
    # 而没配 OTLP 的部署（多数）不必为它付一次 import 成本
    from opentelemetry.exporter.otlp.proto.http.trace_exporter import OTLPSpanExporter

    return OTLPSpanExporter(endpoint=endpoint)


def telemetry_config(settings: Settings) -> dict[str, Any]:
    """交给 ``FastAPI(telemetry=...)`` 的配置（NFR-OBS）。

    **接口层只填配置，不自己挂中间件**：FastAPI 从 0.128 起自带 OpenTelemetry
    埋点，且它装在 ``FastAPI.__call__`` 里——比任何 ``add_middleware`` 都外一层。
    请求进来时服务端 span 已经存在，我们的 request_id 中间件因此可以直接往上写
    属性（这一条是实测确认的，不是推出来的）。

    **为什么不用 opentelemetry-instrumentation-fastapi**：两套埋点同时在时，
    原生那套会检测到旧的包装并**让位**（``_legacy_otel``），于是我们拿到的是
    较次的一份 trace——属性退回到旧语义约定（``http.target`` 而不是 ``url.path``），
    且丢掉 ``fastapi.dependencies`` / ``fastapi.endpoint`` / ``fastapi.serialization``
    三个粒度 span，换来两个 ``GET /x http send`` 噪音。（三种组合都实测过，
    差别见阶段 11 的 TDD 记录。）一个能力只留一条实现路径。

    **``auto_configure=False`` 是安全要求，不是性能偏好**：默认值 True 会在启动时
    读环境里的 ``OTEL_EXPORTER_OTLP_ENDPOINT`` / ``OTEL_EXPORTER_OTLP_TRACES_ENDPOINT``
    并自动挂一个 OTLP 导出器。那等于"往哪儿发数据"由运行环境里一个我们不认识、
    也不会审的变量决定——本项目的规矩是每一处出境都要在配置里写得出来
    （对比 Langfuse 的 ``capture_content``）。要接自建 collector，
    用 ``observability.otlp_endpoint``，那是仓库里的配置。

    **``logs`` 与 ``metrics`` 关掉**：它们不属于本阶段的交付（NFR-OBS 要的是
    链路与 LLM 用量）。``logs=True`` 会把校验失败与未捕获异常的堆栈写进 OTel
    的 Logs 通道——那些我们在 loguru 那一条管线里已经记了，而且那边的
    ``diagnose=False`` 保证不打印栈帧局部变量（登录、连库的局部变量里有明文口令）。
    两份日志早晚会不一致，而多出来的那一份恰好是我们没设计过格式的那份。
    """
    enabled = bool(settings.observability.enabled)
    return {
        "tracer_provider": None,  # None = 用全局那一个，也就是 setup_tracing 装的那个
        "tracing": enabled,
        # 依赖解析、端点、序列化各一个 span。留着它们才能回答"时间花在 FastAPI 这边
        # 还是花在 Agent 里"——只看一个总的服务端 span 是分不出来的。
        "operation_spans": enabled,
        "metrics": False,
        "logs": False,
        "auto_configure": False,
        # 探针路径不埋点：它们被高频轮询，每次留一条 trace 会把真正要看的问答请求
        # 淹掉——而这类噪声最坏的地方在于它会让人放弃看链路，于是埋点等于白装。
        "exclude": _is_probe,
    }


def _is_probe(scope: Mapping[str, Any]) -> bool:
    """FastAPI 的 ``telemetry.exclude`` 谓词：scope 是探针路径吗。

    比对的是"``/health`` 本身"或"``/health/`` 底下的子路径"，不是裸的
    ``startswith("/health")``——那样会把将来某个叫 ``/healthz`` 的业务接口
    一起静音掉，而"某个接口查不到链路"这种故障极难被发现（接口本身一切正常）。
    """
    path = str(scope.get("path", ""))
    return path == PROBE_PATH or path.startswith(f"{PROBE_PATH}/")


#: 我们自己往 span 上写的属性前缀。见 ``should_export_span``——按属性判而不按
#: tracer 名判，是因为前者是跨系统契约，后者改一次名字就会静默失效。
_OWN_ATTRIBUTE_PREFIXES = ("langfuse.", "agent.", "request.")


def should_export_span(span: Span) -> bool:
    """哪些 span 送到 Langfuse（``LangfuseSpanProcessor`` 的 ``should_export_span``）。

    **必须显式给**：默认谓词 ``is_default_export_span`` 只放行三类 span——Langfuse
    SDK 自己建的、带 ``gen_ai.*`` 属性的、来自已知 LLM 埋点 scope 的。我们的节点
    span 三类都不占，实测的后果是：Langfuse 上只有一排 generation，父节点一个不见，
    每条 generation 都被当成**独立的一条 trace**（``isRootObservation=true``）——
    "这次问答慢在哪"恰是它要回答的问题，却正好被它挡掉了。

    补上的两类，各自的理由：

    - **HTTP 服务端 span（``SpanKind.SERVER``）**：它是一次请求的根，而且整轮汇总
      （总耗时、token 用量、各环节命中数）就写在它上面（见 query_service._report）。
      少了它，Langfuse 上根本不存在"一次问答"这个整体。
    - **带本项目命名空间的属性**（``langfuse.`` / ``agent.`` / ``request.``）：
      节点 span 与 generation 走这一条。

    **不无脑放行全部**：qdrant / elasticsearch 客户端埋点会为一次召回产生几十条
    ``search`` span，全放进来会让一条 trace 从十几条涨到四十多条，把真正要看的那几段
    埋掉。它们被跳过不会造成孤立节点——孤立的成因是"父节点被丢掉、子节点还留着"，
    而这些第三方 span 全都是叶子（它们的父节点是我们的节点 span，照常导出）。
    """
    if is_default_export_span(span):
        # gen_ai.* 那一类：`llm_generation` 写的 model / usage 都在这里，
        # 所以即便不加下面的规则，generation 也是能到的（实测确实到了）
        return True
    if span.kind is SpanKind.SERVER:
        return True
    attributes = span.attributes or {}
    return any(
        isinstance(key, str) and key.startswith(_OWN_ATTRIBUTE_PREFIXES)
        for key in attributes
    )


def setup_tracing(settings: Settings) -> None:
    """装好全局 provider，并把 Langfuse 挂在它上面。可重复调用（幂等）。"""
    global _langfuse

    if not settings.observability.enabled:
        logger.info("可观测性已关闭（OBSERVABILITY__ENABLED=false），本次不产生链路数据")
        return

    provider = _ensure_provider(settings)

    if _langfuse is not None:
        return

    langfuse_settings = settings.langfuse
    if not langfuse_settings.configured:
        logger.info(
            "Langfuse 未启用或未配置（需要 LANGFUSE_PUBLIC_KEY 与 LANGFUSE_SECRET_KEY），"
            "链路数据只走 OpenTelemetry"
        )
        return

    try:
        _langfuse = Langfuse(
            public_key=langfuse_settings.public_key,
            secret_key=langfuse_settings.secret_key,
            base_url=langfuse_settings.host,
            timeout=langfuse_settings.timeout,
            tracer_provider=provider,
            # 第二道闸；第一道是"不往 span 里写内容"，见模块说明
            mask_otel_spans=_mask_otel_spans,
            # 默认只放行 LLM 那几类 span，节点与汇总会被静默丢掉，见 should_export_span
            should_export_span=should_export_span,
            environment="debug" if settings.app.debug else "production",
            release=settings.app.version,
        )
    except Exception:  # noqa: BLE001 —— 观测不到不该让服务起不来
        logger.warning("Langfuse 初始化失败，降级为纯 OpenTelemetry 追踪", exc_info=True)
        _langfuse = None
        return

    logger.info(
        "Langfuse 已接入：%s（内容上报：%s）",
        langfuse_settings.host,
        "开" if langfuse_settings.capture_content else "关",
    )


def dispose_tracing() -> None:
    """停机收尾：把缓冲区里的 span 刷出去。

    不刷就消失——``BatchSpanProcessor`` 是攒够一批或到时间才发，进程退出时
    缓冲区里那些"最后几秒的 trace"恰好是最想看的（崩溃前的请求）。
    """
    global _langfuse

    if _langfuse is not None:
        try:
            _langfuse.shutdown()
        except Exception:  # noqa: BLE001 —— 停机失败不该阻断其余资源的释放
            logger.warning("关闭 Langfuse 客户端失败", exc_info=True)
        _langfuse = None

    provider = trace.get_tracer_provider()
    if isinstance(provider, TracerProvider):
        try:
            provider.shutdown()
        except Exception:  # noqa: BLE001
            logger.warning("关闭 TracerProvider 失败", exc_info=True)


def reset_tracing() -> None:
    """丢弃已配置的 provider，让 ``setup_tracing`` 可以重新装。

    **只给测试用**：生产进程只配置一次。OpenTelemetry 没有提供重置全局 provider
    的公开 API——``set_tracer_provider`` 只生效一次，第二次调用会被静默忽略
    （只打一条 warning，不报错）。同一个进程里要换配置（测试的常态），
    只能清掉这两个私有字段。
    """
    global _langfuse
    _langfuse = None
    trace._TRACER_PROVIDER = None  # noqa: SLF001 —— 见上，没有公开 API
    trace._TRACER_PROVIDER_SET_ONCE._done = False  # noqa: SLF001


# ==========================================================================
# span 构造
# ==========================================================================


def _tracer() -> trace.Tracer:
    # 每次现取而不是模块级缓存：``reset_tracing`` 换掉 provider 之后，
    # 缓存下来的 tracer 还指着一个已 shutdown 的旧 provider，span 就丢了
    return trace.get_tracer("data-agent")


def _to_attribute(value: object) -> object:
    """把值压成 OTel 认的类型。

    OTel 的属性只接受标量或**同构**标量序列，塞一个 dict 进去会抛
    ``TypeError``，而这个异常发生在埋点里——它会把正常业务代码带崩。
    埋点必须比业务代码更"不挑食"，所以这里兜一层：认不出的就转成 JSON 字符串。
    """
    if value is None or isinstance(value, (bool, int, float, str)):
        return value
    if isinstance(value, (list, tuple)) and all(
        isinstance(item, (bool, int, float, str)) for item in value
    ):
        return list(value)
    return json.dumps(value, ensure_ascii=False, default=str)


def annotate_current_span(attributes: Mapping[str, object]) -> None:
    """给当前 span 补属性；没有活跃 span 时是空操作。

    空操作这件事很重要：评测（阶段 10）与命令行脚本跑图时没有 HTTP span，
    这些调用点不该因此判空——埋点写起来要像不存在一样。

    收一个映射而不是 ``**kwargs``：属性名里带点（``agent.recall.columns``），
    不是合法的 Python 标识符，写成关键字参数只能靠 ``**{...}`` 绕过去，
    读的人还得停下来想一下它为什么能跑。
    """
    span = trace.get_current_span()
    for key, value in attributes.items():
        span.set_attribute(key, _to_attribute(value))


def recall_attributes(counts: Mapping[str, int]) -> dict[str, int]:
    """各召回/筛选环节的命中数 → span 属性名。

    单独一个前缀（``agent.recall.``）而不是混进别的属性里：面板上要按前缀聚合，
    而"名字里恰好带 count 的属性"这种筛法迟早会把别的计数器也捞进来。

    **每个数写两遍，两个名字、一份来源**：

    - ``agent.recall.<kind>``：我们自己的中性名字。配了通用 OTLP 后端
      （``observability.otlp_endpoint``）时起作用的是它，测试断言的也是它。
    - ``langfuse.observation.metadata.agent.recall.<kind>``：Langfuse 认作元数据，
      在它那边落成干净的一级键 ``agent.recall.<kind>``。

    写这一份的**理由不是"不写就看不见"**——实测（发一条只带普通属性的 span，
    再拿 ``fields=metadata`` 读回来）Langfuse 是收的，只是把它归进
    ``attributes.`` 那个杂物桶：``agent.recall.probe`` 落成
    ``metadata.attributes.agent.recall.probe``。同一个桶里还有 ``http.route``、
     ``resourceAttributes.*`` 这些框架自己倒进来的东西。

    所以区别在于**那一列数叫什么名字**：``attributes.agent.recall.columns`` 是
    "不认识的属性的倾倒口"的副产品，``agent.recall.columns`` 才是一级元数据。
    面板上要按前者筛也做得到，但那意味着把查询绑在一个明说了是兜底的命名上。

    重复是有意的，两处都由这一个函数产出，所以不可能只改了一边而对不上。
    """
    attributes: dict[str, int] = {}
    for kind, count in counts.items():
        value = int(count)
        attributes[f"{ATTR_RECALL_PREFIX}{kind}"] = value
        attributes[f"{ATTR_OBSERVATION_METADATA_PREFIX}{ATTR_RECALL_PREFIX}{kind}"] = value
    return attributes


def current_trace_id() -> str:
    """当前 trace 的 id（32 位十六进制）；没有活跃 trace 时返回空串。"""
    context = trace.get_current_span().get_span_context()
    return format(context.trace_id, "032x") if context.is_valid else ""


def current_node_name() -> str:
    """当前节点名；不在节点里时返回空串。"""
    return _node_name.get()


@contextmanager
def node_span(name: str) -> Iterator[Span]:
    """包住一个节点的执行，产出该节点的 span。

    **async 里的 ``with`` 是安全的**：进入与退出都发生在同一个协程帧内
    （节点的包装函数不是生成器），contextvar 的 attach/detach 因此天然配对。

    节点耗时在这里量、并以属性落到 span 上，而不是靠外部掐表：``with`` 圈的正是
    "节点真正在跑"的那一段，中间没有排队与调度时间——那部分属于调用方的责任。
    """
    started = time.perf_counter()
    attributes: dict[str, object] = {
        ATTR_OBSERVATION_TYPE: "span",
        ATTR_NODE_NAME: name,
    }
    request_id = current_request_id()
    if request_id:
        attributes[ATTR_REQUEST_ID] = request_id

    with _tracer().start_as_current_span(SPAN_NAME_NODE.format(name), attributes=attributes) as span:
        token = _node_name.set(name)
        try:
            yield span
        except Exception as exc:  # noqa: BLE001 —— 记录后原样抛出，不改业务语义
            span.set_status(Status(StatusCode.ERROR, str(exc)))
            raise
        finally:
            span.set_attribute(
                ATTR_NODE_DURATION_MS, int((time.perf_counter() - started) * 1000)
            )
            _node_name.reset(token)


@contextmanager
def llm_generation(*, model: str, prompt: str, system: str | None) -> Iterator[Span]:
    """包住一次 LLM 调用，产出 Langfuse 的 generation。

    名字带上当前节点（``generate_sql.llm``）：Langfuse 的调用列表按名字分组，
    全叫 ``llm.chat`` 的话，第一眼看到的是一排同名的行，还得逐个点开才知道
    哪次是生成 SQL、哪次是在消解指代。
    """
    node = current_node_name()
    name = f"{node}.llm" if node else SPAN_NAME_LLM
    attributes: dict[str, object] = {
        ATTR_OBSERVATION_TYPE: "generation",
        ATTR_OBSERVATION_MODEL: model,
        ATTR_GEN_AI_MODEL: model,
        ATTR_GEN_AI_SYSTEM: "openai",
        ATTR_GEN_AI_OPERATION: "chat",
    }
    request_id = current_request_id()
    if request_id:
        attributes[ATTR_REQUEST_ID] = request_id

    with _tracer().start_as_current_span(name, attributes=attributes) as span:
        if _capture_content():
            if system:
                span.set_attribute("langfuse.observation.metadata.system_prompt", system)
            span.set_attribute(ATTR_OBSERVATION_INPUT, prompt)
        try:
            yield span
        except Exception as exc:  # noqa: BLE001
            span.set_status(Status(StatusCode.ERROR, f"{type(exc).__name__}: {exc}"))
            raise


def record_generation_result(
    span: Span,
    *,
    text: str,
    input_tokens: int | None,
    output_tokens: int | None,
) -> None:
    """把一次 LLM 调用的产出与用量写回它的 generation。

    用量写两处：``gen_ai.usage.*`` 是 OTel 的通用字段（任何 OTLP 后端都认），
    ``langfuse.observation.usage_details`` 是 Langfuse 算费用的依据。
    只写其中一处的话，要么 Langfuse 面板上费用是空的，要么离开 Langfuse 就没了用量。
    """
    if _capture_content():
        span.set_attribute(ATTR_OBSERVATION_OUTPUT, text)

    if input_tokens is None and output_tokens is None:
        # 上游没回用量（有的兼容端点不返回 usage）。**留空而不是写 0**：
        # 0 会被聚合成"这次调用不花 token"，把平均值往下拉，而真相是"不知道"。
        return

    usage = {
        "input": input_tokens or 0,
        "output": output_tokens or 0,
        "total": (input_tokens or 0) + (output_tokens or 0),
    }
    span.set_attribute(ATTR_OBSERVATION_USAGE, json.dumps(usage))
    span.set_attribute(ATTR_GEN_AI_INPUT_TOKENS, usage["input"])
    span.set_attribute(ATTR_GEN_AI_OUTPUT_TOKENS, usage["output"])
