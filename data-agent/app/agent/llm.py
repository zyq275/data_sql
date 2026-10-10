"""LLM 网关（TDD 3 的 ``agent/llm.py``；TDD 4 要求 LLM 逻辑集中在节点与提示词里）。

DeepSeek / Ollama / 任意 OpenAI 兼容端点都只靠配置切换，与 ADR-005 同一个思路：
模型外置，应用不做本地推理，换模型不动代码。

做成**类**而不是一堆模块级函数，是为了让节点能在测试里被喂假模型（依赖经
``runtime.context`` 注入，TDD 4），不必去 monkeypatch 全局单例。
"""

from __future__ import annotations

import logging
from dataclasses import dataclass

import httpx
import openai
from langchain_core.language_models.chat_models import BaseChatModel
from langchain_core.messages import AIMessage, BaseMessage, HumanMessage, SystemMessage
from langchain_openai import ChatOpenAI

from app.core.config import get_settings
from app.core.exceptions import LLMServiceError
from app.core.tracing import llm_generation, record_generation_result

logger = logging.getLogger(__name__)

# 上游故障统一收敛成 LLMServiceError（500xx），调用方只需处理一种异常。
# 不把 SDK 的异常原样抛出去：它的信息里可能带着请求细节，而这些异常最终会
# 出现在 SSE 的 error 事件里（TDD 12.2）。
#
# 两个基类就够了：APIConnectionError 覆盖超时与断连，APIStatusError 覆盖
# 限流(429)与鉴权(401/403)等一切带状态码的失败。httpx 的异常兜住传输层，
# 它们未必被 openai SDK 包过一层。
_UPSTREAM_ERRORS = (openai.APIConnectionError, openai.APIStatusError, httpx.HTTPError)


def content_to_text(content: object) -> str:
    """把 ``AIMessage.content`` 归一成纯文本。

    langchain-core 1.x 起 content 可能是字符串，也可能是内容块列表（多模态或
    带推理过程时是后者）。只取文本块，其余忽略——我们要的是 SQL 和 JSON，
    不是模型的旁白。
    """
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        parts: list[str] = []
        for block in content:
            if isinstance(block, str):
                parts.append(block)
            elif isinstance(block, dict) and block.get("type") == "text":
                parts.append(str(block.get("text", "")))
        return "".join(parts)
    return str(content)


@dataclass
class LLMUsage:
    """累计的 LLM 用量（阶段 11：token 消耗可统计）。

    **挂在网关实例上，不挂在进程上**：``QueryService`` 每轮问答建一个
    ``QueryContext``，也就建一个 ``LLMGateway``，所以这份累计天然是"这一轮的"。
    做成进程级计数器的话，要么并发请求互相污染，要么得靠 contextvar 去分——
    而 contextvar 在异步生成器（``stream``）里跨 ``yield`` 更麻烦：
    生成器被提前关闭时 ``reset`` 会在另一个上下文里执行，直接抛 ValueError。

    ``calls`` 与 ``unknown_usage_calls`` 分开记：有的 OpenAI 兼容端点不回 usage，
    这时 token 是**未知**而不是 0。把它们混在一起，平均值会被悄悄拉低，
    而"消耗变少了"这种结论一旦被信以为真，会把人引向完全错误的方向。
    """

    calls: int = 0
    input_tokens: int = 0
    output_tokens: int = 0
    unknown_usage_calls: int = 0

    @property
    def total_tokens(self) -> int:
        return self.input_tokens + self.output_tokens

    def add(self, *, input_tokens: int | None, output_tokens: int | None) -> None:
        self.calls += 1
        if input_tokens is None and output_tokens is None:
            self.unknown_usage_calls += 1
            return
        self.input_tokens += input_tokens or 0
        self.output_tokens += output_tokens or 0


def usage_of(message: AIMessage) -> tuple[int | None, int | None]:
    """从回复里取 (输入 token, 输出 token)；取不到返回 (None, None)。

    读两个地方是因为它们来自不同层：``usage_metadata`` 是 langchain-core 归一化
    之后的字段（跨厂商都有），``response_metadata["token_usage"]`` 是 OpenAI 协议
    的原始字段。前者缺失就退回后者——不同版本的 langchain-openai 这两个的填充
    情况并不一致，只认一个的话，换个版本用量就整片变成"未知"。
    """
    usage = getattr(message, "usage_metadata", None)
    if isinstance(usage, dict):
        inputs, outputs = usage.get("input_tokens"), usage.get("output_tokens")
        if inputs is not None or outputs is not None:
            return inputs, outputs

    metadata = getattr(message, "response_metadata", None) or {}
    raw = metadata.get("token_usage") or metadata.get("usage")
    if isinstance(raw, dict):
        return raw.get("prompt_tokens"), raw.get("completion_tokens")
    return None, None


class LLMGateway:
    """一次对话调用的封装。

    每次 ``text`` 都在一条 Langfuse generation 里跑（阶段 11）：模型名、token 用量、
    耗时都记在那上面。放在这里而不是各节点里，因为**这里是唯一的一个出口**——
    十五个节点共用它，埋一次就全覆盖；反过来，某个节点私自直调模型，
    它就会在链路里凭空消失，而"漏了一处埋点"从面板上完全看不出来。
    """

    def __init__(self, model: BaseChatModel) -> None:
        self._model = model
        self.usage = LLMUsage()

    @classmethod
    def default(cls) -> LLMGateway:
        """按配置构造（进程内复用同一个底层模型）。"""
        return cls(get_chat_model())

    @property
    def model_name(self) -> str:
        """底层模型名，用于链路与日志；取不到就给空串（假模型常见）。"""
        return getattr(self._model, "model_name", "") or getattr(self._model, "model", "") or ""

    async def text(self, prompt: str, *, system: str | None = None) -> str:
        """发一次对话并返回纯文本；上游任何故障都抛 :class:`LLMServiceError`。

        空回复不算故障，原样返回空串——"模型没答"与"模型答得没用"是两回事，
        由调用方按业务判断（生成 SQL 时它是失败，扩展关键字时可以退回原问句）。
        """
        messages: list[BaseMessage] = []
        if system:
            messages.append(SystemMessage(content=system))
        messages.append(HumanMessage(content=prompt))

        # 生成器的名字取自"当前节点"（见 tracing.current_node_name），
        # 于是 Langfuse 上看到的是 generate_sql.llm、filter_table.llm……
        # 不必让每个节点多传一个参数，也就不会有"新节点忘了传"这种漏
        with llm_generation(model=self.model_name, prompt=prompt, system=system) as span:
            try:
                response: AIMessage = await self._model.ainvoke(messages)
            except _UPSTREAM_ERRORS as exc:
                logger.warning("LLM 调用失败：%s", type(exc).__name__, exc_info=exc)
                raise LLMServiceError(f"LLM 调用失败：{type(exc).__name__}") from exc

            text = content_to_text(response.content).strip()
            input_tokens, output_tokens = usage_of(response)
            self.usage.add(input_tokens=input_tokens, output_tokens=output_tokens)
            record_generation_result(
                span,
                text=text,
                input_tokens=input_tokens,
                output_tokens=output_tokens,
            )
        return text


_chat_model: BaseChatModel | None = None


def get_chat_model() -> BaseChatModel:
    """进程级对话模型单例（首次调用时创建）。"""
    global _chat_model
    if _chat_model is None:
        settings = get_settings().llm
        if not settings.api_key:
            # ChatOpenAI 缺 key 时抛的是 OpenAIError("Missing credentials")，
            # 那句话不告诉人该改哪个变量。这里补一句指路的（NFR-SEC-003）。
            raise LLMServiceError(
                "llm.api_key 未配置，请在 .env 中设置 LLM__API_KEY"
            )
        _chat_model = ChatOpenAI(
            model=settings.model_name,
            api_key=settings.api_key,
            base_url=settings.base_url,
            temperature=settings.temperature,
            timeout=settings.timeout,
            max_retries=settings.max_retries,
        )
    return _chat_model


async def dispose_llm() -> None:
    """关闭底层 HTTP 连接池（应用停机时调用）。"""
    global _chat_model
    if _chat_model is not None:
        await _chat_model.root_async_client.close()
    _chat_model = None
