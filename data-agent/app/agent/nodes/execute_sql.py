"""执行 SQL 并推送 sql / result 事件。"""

from __future__ import annotations

import logging

from langgraph.runtime import Runtime

from app.agent.context import QueryContext
from app.agent.events import RESULT, SQL, emit, step
from app.agent.state import QueryState
from app.core.sql_security import assert_readonly

logger = logging.getLogger(__name__)


@step("execute_sql")
async def execute_sql(state: QueryState, runtime: Runtime[QueryContext]) -> dict:
    """在数仓上执行只读查询，推送 ``sql`` 与 ``result`` 事件。

    **执行前再校验一次**。这不是重复劳动：安全闸门应当装在闸门自己身上，
    而不是指望上一个节点一定会先跑。这个节点手里握着唯一一个真会连数仓执行
    SQL 的句柄，被单独调用或重放时也必须自证安全。校验是纯解析，代价可忽略。

    先推 ``sql`` 再执行：前端能立刻把 SQL 显示出来，而不必等结果集回来
    （大结果集的取数可能要好几秒），TDD 7.3 里 sql 是独立于 result 的事件。
    """
    sql = assert_readonly(state.get("sql") or "")
    writer = runtime.stream_writer
    emit(writer, SQL, sql=sql)

    result = await runtime.context.dw.execute(sql)
    if result.truncated:
        logger.info("结果因 max_result_rows 被截断，返回前 %d 行", len(result.rows))

    payload = {
        "columns": result.columns,
        "rows": result.rows,
        "row_count": len(result.rows),
        # 截断了就要如实说：否则用户会把"前一万行"当成"全部"，
        # 而这类误读往往在拿去做决策时才被发现（BR-SQL-004）
        "truncated": result.truncated,
    }
    emit(writer, RESULT, data=payload)

    return {
        "sql": sql,
        "columns": result.columns,
        "rows": result.rows,
        "row_count": len(result.rows),
        "truncated": result.truncated,
    }
