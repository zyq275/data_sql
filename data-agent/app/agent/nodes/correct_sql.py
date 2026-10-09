"""修正 SQL（LLM），校正计数加一后回边重校。"""

from __future__ import annotations

from langgraph.runtime import Runtime

from app.agent.context import QueryContext
from app.agent.events import step
from app.agent.prompts import render
from app.agent.state import QueryState
from app.core.exceptions import SQLGenerationError
from app.core.text import strip_code_fence


@step("correct_sql")
async def correct_sql(state: QueryState, runtime: Runtime[QueryContext]) -> dict:
    """带着错误信息让模型重写 SQL，并把 ``correction_count`` 加 1。

    **计数在这里加，而不是在 validate_sql 里加**：加法必须和"确实又产出，
    了一版新 SQL"绑定。若在发现校验失败时就加，模型调用随后失败（超时、限流）
    也已经把额度耗掉了，等于凭空少一次改正机会——而失败的那次根本没产生新 SQL。

    带上完整上下文（表结构、指标口径、日期）而不只是错误信息：模型改 SQL 时
    同样需要知道有哪些字段可写，只给错误信息会促使它编一个字段来绕开报错。
    """
    prompt = render(
        "correct_sql",
        table_infos=state.get("table_text", ""),
        metric_infos=state.get("metric_text", ""),
        date_info=state.get("date_text", ""),
        db_info=state.get("db_text", ""),
        sql=state.get("sql") or "",
        error=state.get("error") or "未知错误",
        query=state["resolved_query"],
    )
    raw = await runtime.context.llm.text(prompt)
    sql = strip_code_fence(raw)
    if not sql:
        raise SQLGenerationError("模型没有返回修正后的 SQL")

    count = state.get("correction_count", 0) + 1
    return {
        "sql": sql,
        "correction_count": count,
        # 清掉上一轮的错误：新的 SQL 还没校验过，旧的错误结论不能带到下一次分流
        "error": None,
        "error_code": None,
    }
