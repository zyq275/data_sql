"""指标筛选（LLM）。"""

from __future__ import annotations

import logging
from collections.abc import Sequence

from langgraph.runtime import Runtime

from app.agent.context import QueryContext
from app.agent.events import step
from app.agent.llm import LLMGateway
from app.agent.parsing import parse_json_array
from app.agent.prompts import render
from app.agent.rendering import metric_candidates_text
from app.agent.state import QueryState
from app.core.exceptions import LLMServiceError
from app.retrievers.retrieval import Candidate

logger = logging.getLogger(__name__)


@step("filter_metric")
async def filter_metric(state: QueryState, runtime: Runtime[QueryContext]) -> dict:
    """从指标候选里挑出真正要用的，并取回它们的完整口径。

    取回的是 ``metric_info`` 里的完整定义（计算方式、过滤条件、时间口径），
    而不是召回结果里那份摘要：召回带的是 Qdrant payload，够用来"选"，
    但生成 SQL 需要的是"仅统计已支付订单"这种**过滤口径**——少了它，
    销售额会把已取消、已退款的订单一起算进去，数字看着合理却是错的。
    """
    candidates = [c for c in (state.get("retrieved") or []) if c.kind == "metric"]
    if not candidates:
        return {"metric_infos": []}

    names = [c.name for c in candidates]
    known = set(names)
    picked = await _select(runtime.context.llm, state["resolved_query"], candidates)

    if picked is None:
        logger.warning("指标筛选结果无法解析，保留全部候选")
        picked = names
    elif picked and not [name for name in picked if name in known]:
        # 返回了非空数组，却没有一个名字在候选里——模型改写或编造了指标名。
        # 这与"返回空数组"是两回事：空数组是"这是明细类问法、不需要指标"，
        # 是合法结论；全是编造的名字则说明模型跑偏了，此时保留全部候选才安全。
        logger.warning("指标筛选给出的名字都不在候选中：%s", picked)
        picked = names

    valid = [name for name in dict.fromkeys(picked) if name in known]
    return {"metric_infos": await runtime.context.knowledge.metrics(valid)}


async def _select(
    llm: LLMGateway, query: str, candidates: Sequence[Candidate]
) -> list[str] | None:
    """调模型筛指标；输出无法解析时返回 None。"""
    prompt = render(
        "filter_metric_info",
        query=query,
        metric_candidates=metric_candidates_text(candidates),
    )
    raw = await llm.text(prompt)
    try:
        return parse_json_array(raw, context="指标筛选")
    except LLMServiceError as exc:
        logger.warning("指标筛选结果无法解析：%s", exc)
        return None
