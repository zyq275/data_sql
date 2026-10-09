"""指标召回（混合检索：向量 + 全文 → RRF → rerank）。"""

from __future__ import annotations

from langgraph.runtime import Runtime

from app.agent.context import QueryContext
from app.agent.events import step
from app.agent.state import QueryState


@step("recall_metric")
async def recall_metric(state: QueryState, runtime: Runtime[QueryContext]) -> dict:
    """按扩展出的检索词做指标召回。

    指标召回是"口径"的来源：命中 GMV 才能拿到"仅统计已支付订单"这类过滤条件，
    否则生成的 SQL 会把已取消、已退款的订单也算进销售额。

    这一路是混合检索收益最大的一路：单路向量分不开近义词，问"销售额"时
    会稳定地把``sales_quantity``（销量）排在``GMV``（成交总额）之前（TDD 8.3
    有实测记录）。而指标的 description 里写着"口语常称销售额、营收、卖了多少钱"，
    全文那一路对这种字面共现极其敏感，两路融合正好把它提上来。
    """
    candidates = await runtime.context.retrieval.metrics(
        state.get("metric_keywords") or [], query=state["resolved_query"]
    )
    return {"metric_candidates": candidates}
