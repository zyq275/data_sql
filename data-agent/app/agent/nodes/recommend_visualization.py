"""图表类型推荐（FR-VIZ-001）。

按结果结构推一个 ``viz`` 事件（``chart_type`` + 一句人话 ``message``），
判定规则全在 ``agent/visualization.py``，本节点只负责"取结果、调规则、推事件"。

**为什么这个节点里没有一行判断逻辑**：规则是这个阶段的验收对象本身
（"图表类型推荐符合规则"）。把它写在节点里，测它就非得跑一遍图；抽成纯函数后，
四条规则各自的正例反例可以直接对着函数打，不必准备桩 LLM、桩数仓。
本文件因此薄到近乎多余——这正是想要的形状。

**它是图上唯一一个不会失败的节点**：纯计算，不调 LLM、不连库，没有上游可抖。
因此这里没有 try/except，也没有"失败就跳过"的降级分支——为不会发生的事写分支，
只会让下一个人以为这里真有失败路径。
"""

from __future__ import annotations

from langgraph.runtime import Runtime

from app.agent.context import QueryContext
from app.agent.events import VIZ, emit, step
from app.agent.state import QueryState
from app.agent.visualization import recommend


@step("recommend_visualization")
async def recommend_visualization(state: QueryState, runtime: Runtime[QueryContext]) -> dict:
    """按结果结构推荐图表类型，推 ``viz`` 事件。"""
    recommendation = recommend(
        state.get("columns") or [], state.get("rows") or []
    )
    emit(
        runtime.stream_writer,
        VIZ,
        chart_type=recommendation.chart_type,
        message=recommendation.message,
    )
    return {"chart_type": recommendation.chart_type}
