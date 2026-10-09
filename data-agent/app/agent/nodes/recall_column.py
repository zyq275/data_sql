"""字段召回（单路稠密向量）。"""

from __future__ import annotations

from langgraph.runtime import Runtime

from app.agent.context import QueryContext
from app.agent.events import step
from app.agent.state import QueryState


@step("recall_column")
async def recall_column(state: QueryState, runtime: Runtime[QueryContext]) -> dict:
    """按扩展出的检索词做字段向量召回。

    检索本身全在 `retrievers.retrieval` 里，节点只做"从 State 取词、把结果放回
    State"——阶段 6 要换成混合检索时，改的是检索层，这个文件一行都不用动。
    """
    candidates = await runtime.context.retrieval.columns(state.get("column_keywords") or [])
    return {"column_candidates": candidates}
