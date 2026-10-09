"""取值召回（ES 全文）。"""

from __future__ import annotations

from langgraph.runtime import Runtime

from app.agent.context import QueryContext
from app.agent.events import step
from app.agent.state import QueryState


@step("recall_value")
async def recall_value(state: QueryState, runtime: Runtime[QueryContext]) -> dict:
    """按扩展出的检索词做取值全文召回（ik 分词）。

    这一路是问句里"只有取值、没有字段名"时唯一的线索来源：问"华东区卖了多少"
    时问句里没有任何字段名，但"华东"能命中 ``region_name`` 的取值索引，
    从而反查出地区表。
    """
    candidates = await runtime.context.retrieval.values(state.get("value_keywords") or [])
    return {"value_candidates": candidates}
