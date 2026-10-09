"""指代消解（TDD 5.1 的图入口节点，FR-CONV-004）。

把"那华东呢""换成按月看""跟去年对比一下"这类依赖上文的问句补全成独立完整的
问题，写进 ``state.resolved_query``。此后**所有**下游节点统一消费
``resolved_query``，不再看 ``query``。

**为什么改写要单独占一个节点，而不是并进 extract_keywords**：改写的产出同时被
三路召回、两个筛选、生成 SQL、校正六处消费，若它藏在关键字抽取里，那六处就得各自
判断"我现在拿到的是原文还是改写后的"。放在图的入口，这件事只发生一次、只有一个
出口，且"这一步到底改写了什么"在日志里是一条独立、可对照的记录。

**首轮为什么必须原样透传而不是"让模型自己判断"**：没有历史时任何改写都是凭空
发挥。让模型面对空历史去"决定要不要改写"，它偶尔会把一个本来完整的问题改坏
（补出一个历史里根本没有的年份）。分支写在代码里，判断成本为零且结果确定。
"""

from __future__ import annotations

import logging

from langgraph.runtime import Runtime

from app.agent.context import QueryContext
from app.agent.events import step
from app.agent.prompts import render
from app.agent.rendering import history_text
from app.agent.state import QueryState
from app.core.exceptions import LLMServiceError
from app.core.text import strip_code_fence

logger = logging.getLogger(__name__)

MAX_RESOLVED_CHARS = 500
"""改写结果的长度上限，与接口层 QueryRequest 的问题上限一致。

模型的改写有失控的可能（尤其提示词被改动之后）：它可能开始自问自答、把三轮历史
全抄一遍，甚至把 SQL 也写出来。改写结果是要喂回整条链路的，一旦膨胀，
后面每一次 LLM 调用都跟着变慢变贵，而症状只是"最近回答变慢了"。
超限就退回原问句——宁可少一次改写，也不要让一条失控的输出污染整轮问答。
"""


@step("resolve_reference")
async def resolve_reference(state: QueryState, runtime: Runtime[QueryContext]) -> dict:
    """结合会话历史把本轮问题补全为完整问句。"""
    query = state["query"]
    history = state.get("history") or []

    if not history:
        # 首轮（或历史取数失败时的空历史）：原样透传，一次模型调用都不发
        return {"resolved_query": query}

    prompt = render("resolve_reference", history=history_text(history), query=query)
    try:
        raw = await runtime.context.llm.text(prompt)
    except LLMServiceError as exc:
        # 与关键字扩展同一条取舍：消解是"让问题更清楚"，不是"没有它就没法答"。
        # 退回原问句，多轮体验会变差，但单轮问答照常可用；
        # 因一次上游抖动就把整轮问答判死，等于把可用服务变成不可用。
        logger.warning("指代消解失败，退回原始问句：%s", exc)
        return {"resolved_query": query}

    resolved = _clean(raw)
    if not resolved:
        # 空回复同上：模型没答，不是答错，不该让它把问题变成空串。
        # 这一点格外要紧——resolved_query 空掉的话，下游每一条提示词里的
        # {query} 都会变成空，模型照样会"答"出一个看起来正常的 SQL。
        logger.warning("指代消解返回空内容，退回原始问句")
        return {"resolved_query": query}

    if len(resolved) > MAX_RESOLVED_CHARS:
        logger.warning(
            "指代消解输出超长（%d 字 > %d），退回原始问句", len(resolved), MAX_RESOLVED_CHARS
        )
        return {"resolved_query": query}

    if resolved != query:
        logger.info("指代消解：%s → %s", query, resolved)
    return {"resolved_query": resolved}


def _clean(raw: str) -> str:
    """把模型输出收拾成一行问句。

    提示词已要求纯文本，但模型不总是听：实测常见的是加引号、加"改写后："前缀、
    或包一层代码块。这些残留会原样进入下游每一条提示词，模型看到问句外面套着引号，
    有时会把它当成"用户引用的一段文本"而改变理解。所以在入口统一剥掉。
    """
    text = strip_code_fence(raw).strip()
    # 只剥成对的引号，且剥完不能为空——避免把 `"去年"销售额` 这类正常内容削掉一块
    for quote in ('"', "'", "“”", "「」"):
        if len(text) >= 2 and text[0] == quote[0] and text[-1] == quote[-1]:
            text = text[1:-1].strip()
            break

    for prefix in ("改写后：", "改写结果：", "完整问题：", "问题："):
        if text.startswith(prefix):
            text = text[len(prefix) :].strip()
            break

    # 取第一行：模型偶尔在问句后补一句解释，而问句本身总在第一行
    first_line = text.splitlines()[0].strip() if text else ""
    return first_line
