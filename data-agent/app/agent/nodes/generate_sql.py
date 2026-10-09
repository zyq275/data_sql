"""生成 SQL（LLM）。"""

from __future__ import annotations

from langgraph.runtime import Runtime

from app.agent.context import QueryContext
from app.agent.events import step
from app.agent.prompts import render
from app.agent.state import QueryState
from app.core.exceptions import SQLGenerationError
from app.core.text import strip_code_fence


@step("generate_sql")
async def generate_sql(state: QueryState, runtime: Runtime[QueryContext]) -> dict:
    """按上下文生成 SQL。

    提示词占位符与 TDD 6 的示例一一对应（``table_infos`` / ``metric_infos`` /
    ``date_info`` / ``db_info`` / ``query``），方便和文档对着看。

    仍然剥一次代码块围栏：提示词第 6 条已经禁止 Markdown 包裹，但模型不总是听。
    与其相信提示词，不如在执行处把它当"可能会犯规"来对待。
    """
    prompt = render(
        "generate_sql",
        table_infos=state.get("table_text", ""),
        metric_infos=state.get("metric_text", ""),
        date_info=state.get("date_text", ""),
        db_info=state.get("db_text", ""),
        conversation_history=state.get("history_text", ""),
        query=state["resolved_query"],
    )
    raw = await runtime.context.llm.text(prompt)
    sql = strip_code_fence(raw)
    if not sql:
        raise SQLGenerationError("模型没有返回 SQL 内容")

    # 清掉上一轮的 error / error_code：这是一次全新生成，旧错误不该再影响条件边分流。
    # 校正循环里 correct_sql 也会清，两处都清是为了让"任何一次新生成"都从干净状态开始。
    return {"sql": sql, "error": None, "error_code": None}
