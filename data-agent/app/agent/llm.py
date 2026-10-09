"""LLM 网关（TDD 3 的 ``agent/llm.py``；TDD 4 要求 LLM 逻辑集中在节点与提示词里）。

DeepSeek / Ollama / 任意 OpenAI 兼容端点都只靠配置切换，与 ADR-005 同一个思路：
模型外置，应用不做本地推理，换模型不动代码。

做成**类**而不是一堆模块级函数，是为了让节点能在测试里被喂假模型（依赖经
``runtime.context`` 注入，TDD 4），不必去 monkeypatch 全局单例。
"""

from __future__ import annotations

import logging

import httpx
import openai
from langchain_core.language_models.chat_models import BaseChatModel
from langchain_core.messages import AIMessage, BaseMessage, HumanMessage, SystemMessage
from langchain_openai import ChatOpenAI

from app.core.config import get_settings
from app.core.exceptions import LLMServiceError

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


class LLMGateway:
    """一次对话调用的封装。"""

    def __init__(self, model: BaseChatModel) -> None:
        self._model = model

    @classmethod
    def default(cls) -> LLMGateway:
        """按配置构造（进程内复用同一个底层模型）。"""
        return cls(get_chat_model())

    async def text(self, prompt: str, *, system: str | None = None) -> str:
        """发一次对话并返回纯文本；上游任何故障都抛 :class:`LLMServiceError`。

        空回复不算故障，原样返回空串——"模型没答"与"模型答得没用"是两回事，
        由调用方按业务判断（生成 SQL 时它是失败，扩展关键字时可以退回原问句）。
        """
        messages: list[BaseMessage] = []
        if system:
            messages.append(SystemMessage(content=system))
        messages.append(HumanMessage(content=prompt))
        try:
            response: AIMessage = await self._model.ainvoke(messages)
        except _UPSTREAM_ERRORS as exc:
            logger.warning("LLM 调用失败：%s", type(exc).__name__, exc_info=exc)
            raise LLMServiceError(f"LLM 调用失败：{type(exc).__name__}") from exc
        return content_to_text(response.content).strip()


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
