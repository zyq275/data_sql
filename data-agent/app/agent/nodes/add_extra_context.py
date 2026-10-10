"""上下文拼装：日期 / 数据库 / 表结构 / 指标口径 / 会话历史。"""

from __future__ import annotations

import logging
from datetime import datetime

from langgraph.runtime import Runtime

from app.agent.context import QueryContext
from app.agent.events import step
from app.agent.permissions import PermissionScope
from app.agent.rendering import (
    date_info_text,
    db_info_text,
    history_text,
    metric_infos_text,
    table_infos_text,
)
from app.agent.state import QueryState

logger = logging.getLogger(__name__)


@step("add_extra_context")
async def add_extra_context(state: QueryState, runtime: Runtime[QueryContext]) -> dict:
    """把生成 SQL 需要的五段上下文字符串拼齐。

    不调 LLM：上下文是**事实**——今天几号、库是哪个、表有哪些字段、上文问过什么，
    事实不该由模型生成。相对时间的换算同样在这里做死（见 ``rendering.date_info``），
    把不确定性从"推理"挪到"查表"。

    会话历史（阶段 7，TDD 5.3）走的是 ``state["history"]``，由服务层从
    ``message`` 表取好放进状态——**不在这里查库**。节点保持"只读状态"有一条实际
    好处：它可以在没有数据库的环境里被单独测，而多轮上下文是否正确是阶段 7
    最需要被反复验证的东西。

    **历史里的 SQL 要过一遍权限**（阶段 9）：``table_text`` 与 ``metric_text``
    已经是被裁过的，但历史是上一轮 SQL 的原文，里面可能引用着**当时能用、现在
    不能**的表——权限是被改过的，而会话比权限活得久。不裁的话，越权的表名会从
    这条旁路原样进提示词，前面所有的裁剪都白做。裁掉的轮次记 WARNING：
    上下文少一轮会让追问的指代变模糊，得留下线索。
    """
    tables = state.get("table_infos") or []
    metrics = state.get("metric_infos") or []
    scope = await runtime.context.permissions.scope_for(runtime.context.role)
    return {
        "table_text": table_infos_text(tables),
        "metric_text": metric_infos_text(metrics),
        "date_text": date_info_text(datetime.now()),
        "db_text": db_info_text(),
        "history_text": history_text(_authorized_turns(state.get("history") or [], scope)),
    }


def _authorized_turns(history: list[dict], scope: PermissionScope) -> list[dict]:
    """丢掉引用了当前角色不可用表的那些历史轮次。"""
    if scope.unrestricted:
        return history
    kept: list[dict] = []
    for turn in history:
        violations = scope.violations(turn.get("sql"))
        if violations:
            logger.warning("历史轮次引用了当前角色未授权的对象 %s，已从提示词中剔除", violations)
            continue
        kept.append(turn)
    return kept
