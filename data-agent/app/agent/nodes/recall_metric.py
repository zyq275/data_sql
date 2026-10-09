"""指标召回（单路稠密向量）。"""

from __future__ import annotations

from langgraph.runtime import Runtime

from app.agent.context import QueryContext
from app.agent.events import step
from app.agent.state import QueryState


@step("recall_metric")
async def recall_metric(state: QueryState, runtime: Runtime[QueryContext]) -> dict:
    """按扩展出的检索词做指标向量召回。

    指标召回是"口径"的来源：命中 GMV 才能拿到"仅统计已支付订单"这类过滤条件，
    否则生成的 SQL 会把已取消、已退款的订单也算进销售额。
    """
    candidates = await runtime.context.retrieval.metrics(state.get("metric_keywords") or [])
    return {"metric_candidates": candidates}
