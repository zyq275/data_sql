"""字段召回（混合检索：向量 + 全文 → RRF → rerank）。"""

from __future__ import annotations

from langgraph.runtime import Runtime

from app.agent.context import QueryContext
from app.agent.events import step
from app.agent.state import QueryState


@step("recall_column")
async def recall_column(state: QueryState, runtime: Runtime[QueryContext]) -> dict:
    """按扩展出的检索词做字段召回。

    检索本身全在 `retrievers.retrieval` 里，节点只做"从 State 取词、把结果放回
    State"。阶段 6 把单路向量换成混合检索，这个文件只是多传了一个 ``query``——
    精排的输入是**整个问句**，不是扩展出来的关键词：关键词是"该找什么"，
    问句才带着"为什么找它"（"去年华东区销售额前三的省份"里的时间与排序意图）。

    传的是 ``resolved_query`` 而不是 ``query``：阶段 7 起前者才是消解了指代的
    完整问句，精排拿它才排得准。
    """
    candidates = await runtime.context.retrieval.columns(
        state.get("column_keywords") or [], query=state["resolved_query"]
    )
    return {"column_candidates": candidates}
