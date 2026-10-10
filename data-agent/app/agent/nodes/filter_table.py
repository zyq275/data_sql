"""表与字段筛选（LLM）。"""

from __future__ import annotations

import logging
from collections.abc import Sequence
from dataclasses import replace

from langgraph.runtime import Runtime

from app.agent.context import QueryContext
from app.agent.events import step
from app.agent.knowledge import TableSpec
from app.agent.llm import LLMGateway
from app.agent.parsing import parse_json_object
from app.agent.prompts import render
from app.agent.rendering import table_candidates_text
from app.agent.state import QueryState
from app.core.exceptions import LLMServiceError, QueryError

logger = logging.getLogger(__name__)


@step("filter_table")
async def filter_table(state: QueryState, runtime: Runtime[QueryContext]) -> dict:
    """由召回到的字段反查表，再让模型裁掉无关的表与字段。

    取值候选同样能定位表：它的 ``id`` 就是所属字段的 id（阶段 4 让 ES 文档带上了
    ``column_id``）。所以问句里只说"华东"、一个字段名都没提时，
    这一路是表线索的唯一来源。

    给出的是每张表的**完整**字段清单（见 knowledge.tables_for_columns），
    而不是只给召回命中的那几个——SQL 要分组、过滤、关联的字段未必在召回结果里。

    **数据权限在这一步收口**（阶段 9），而且要收两次：进模型之前裁一次，
    让它根本看不到未授权的表名与字段名；模型筛完再裁一次，因为 ``_trim``
    会无条件把主外键加回来，而"没授权的主外键也不给"是权限的口径
    （见 ``agent.permissions.PermissionScope.filter_tables``）。
    """
    candidates = state.get("retrieved") or []
    column_ids = [c.id for c in candidates if c.kind in ("column", "value")]
    tables = await runtime.context.knowledge.tables_for_columns(column_ids)
    if not tables:
        raise QueryError("没有召回到任何数据表，无法回答该问题")

    scope = await runtime.context.permissions.scope_for(runtime.context.role)
    tables = scope.filter_tables(tables)
    if not tables:
        # 召回有结果，但一条都不在授权范围内。这与"召回到不"是两回事，
        # 报错要能让人分辨——用户看到"没召回到表"会去改问题问法，其实是权限。
        raise QueryError("本次召回的数据表都不在当前角色的授权范围内")

    selected = await _select(runtime.context.llm, state["resolved_query"], tables)
    if selected is None:
        logger.warning("表结构筛选结果无法解析，退回全部候选表")
        selected = tables
    if not selected:
        # 模型明确判定"候选中没有能回答这个问题的表"。此时**绝不能放行空上下文**：
        # 模型拿不到任何表名，就会开始编造。一句明确的报错远比一段幻觉 SQL 有价值。
        raise QueryError("知识库的候选表里没有能回答该问题所需的表")
    return {"table_infos": scope.filter_tables(selected)}


async def _select(
    llm: LLMGateway, query: str, tables: Sequence[TableSpec]
) -> list[TableSpec] | None:
    """调模型筛选；输出无法解析时返回 None（由调用方决定怎么退）。

    这里不吞解析错误而是用 None 表达"没筛成"：调用方要区分"模型说没有相关表"
    （空列表，是结论）和"模型输出坏了"（None，是故障）——两者该有完全不同的处置。
    """
    prompt = render(
        "filter_table_info",
        query=query,
        table_candidates=table_candidates_text(tables),
    )
    raw = await llm.text(prompt)
    try:
        data = parse_json_object(raw, context="表结构筛选")
    except LLMServiceError as exc:
        logger.warning("表结构筛选结果无法解析：%s", exc)
        return None

    entries = data.get("tables")
    if not isinstance(entries, list):
        logger.warning("表结构筛选结果里没有 tables 数组")
        return None

    by_name = {table.name: table for table in tables}
    picked: list[TableSpec] = []
    for entry in entries:
        if not isinstance(entry, dict):
            continue
        table = by_name.get(str(entry.get("table_name", "")))
        if table is None:
            # 模型编了个不存在的表名。丢弃而不是整单失败：其余表可能仍然够用
            logger.warning("表结构筛选给出了不存在的表：%r", entry.get("table_name"))
            continue
        picked.append(_trim(table, entry.get("columns")))
    return picked


def _trim(table: TableSpec, wanted: object) -> TableSpec:
    """按模型的筛选结果裁剪字段。

    **主键与外键无条件保留**：它们是 JOIN 与去重的结构性依赖，而模型裁字段时
    最容易漏掉它们——问"各省份销售额"时它未必想到还得留下 ``region_id`` 去
    关联地区表。漏掉这类字段会让 SQL 直接跑不通，多留两三个必然相关的字段
    则毫无代价，所以这里只做保守裁剪。
    """
    if not isinstance(wanted, list):
        return table
    names = {str(name) for name in wanted if isinstance(name, (str, int))}
    if not names:
        return table
    columns = [
        column
        for column in table.columns
        if column.is_primary_key or column.is_foreign_key or column.name in names
    ]
    return replace(table, columns=columns) if columns else table
