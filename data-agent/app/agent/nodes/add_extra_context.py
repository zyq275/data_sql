"""上下文拼装：日期 / 数据库 / 表结构 / 指标口径。"""

from __future__ import annotations

from datetime import datetime

from langgraph.runtime import Runtime

from app.agent.context import QueryContext
from app.agent.events import step
from app.agent.rendering import date_info_text, db_info_text, metric_infos_text, table_infos_text
from app.agent.state import QueryState


@step("add_extra_context")
async def add_extra_context(state: QueryState, runtime: Runtime[QueryContext]) -> dict:
    """把生成 SQL 需要的四段上下文字符串拼齐。

    纯计算，不调 LLM：上下文是**事实**——今天几号、库是哪个、表有哪些字段，
    事实不该由模型生成。相对时间的换算同样在这里做死（见 ``rendering.date_info``），
    把不确定性从"推理"挪到"查表"。

    TDD 5.1 说这个节点还要注入会话历史（阶段 7）与数据权限（阶段 9）：
    到那时在这里多拼两段即可，生成节点一行不用改。
    """
    tables = state.get("table_infos") or []
    metrics = state.get("metric_infos") or []
    return {
        "table_text": table_infos_text(tables),
        "metric_text": metric_infos_text(metrics),
        "date_text": date_info_text(datetime.now()),
        "db_text": db_info_text(),
    }
