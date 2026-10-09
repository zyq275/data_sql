"""把知识库实体渲染成提示词片段（TDD 6 的 ``{table_infos}`` / ``{metric_infos}`` /
``{date_info}`` / ``{db_info}``）。

渲染与节点分开，一是因为同一份文本被多个节点复用（filter 与 generate 都要表结构），
二是这些函数是**纯函数**——日期换算这类最容易算错的逻辑可以脱离 LLM、脱离数据库
直接测，不必等到跑通整条链路才发现"上个月"算错了。
"""

from __future__ import annotations

import calendar
from collections.abc import Sequence
from datetime import date, datetime, timedelta

from app.agent.knowledge import MetricSpec, TableSpec
from app.core.config import get_settings
from app.retrievers.retrieval import Candidate

EMPTY = "（无）"


def _shift_months(day: date, months: int) -> date:
    """按月平移；目标月没有这一天时取该月最后一天（3/31 往前一月 → 2/28）。"""
    total = day.year * 12 + (day.month - 1) + months
    year, month_index = divmod(total, 12)
    month = month_index + 1
    return date(year, month, min(day.day, calendar.monthrange(year, month)[1]))


def date_info_text(now: datetime) -> str:
    """当前时间与常用相对时间的换算表。

    **为什么要把"去年"这类词算给模型看**：相对时间的换算是死板的日期算术，
    交给模型做既容易错（算错一年、漏掉闰年边界）又没法测；算好摆在上下文里，
    模型只需要挑一行照抄。这是把不确定性从"推理"挪到"查表"。

    数据表的时间字段形状不写死：本项目既有形如 yyyymmdd 的整数主键，
    也有独立的年/季/月整数列，用哪一列由表结构决定。
    """
    today = now.date()
    month_start = today.replace(day=1)
    month_end = _shift_months(month_start, 1) - timedelta(days=1)
    last_month_start = _shift_months(month_start, -1)
    last_month_end = month_start - timedelta(days=1)
    quarter_start_month = (today.month - 1) // 3 * 3 + 1
    quarter_start = date(today.year, quarter_start_month, 1)
    last_quarter_end = quarter_start - timedelta(days=1)
    last_quarter_start = date(
        last_quarter_end.year, (last_quarter_end.month - 1) // 3 * 3 + 1, 1
    )

    rows = [
        ("今天", today, today),
        ("昨天", today - timedelta(days=1), today - timedelta(days=1)),
        ("近 7 天", today - timedelta(days=6), today),
        ("近 30 天", today - timedelta(days=29), today),
        ("近三个月", _shift_months(today, -3), today),
        ("本月", month_start, month_end),
        ("上月", last_month_start, last_month_end),
        ("本季度", quarter_start, _shift_months(quarter_start, 3) - timedelta(days=1)),
        ("上季度", last_quarter_start, last_quarter_end),
        ("今年", date(today.year, 1, 1), date(today.year, 12, 31)),
        ("去年", date(today.year - 1, 1, 1), date(today.year - 1, 12, 31)),
    ]

    lines = [f"当前日期：{today.isoformat()}（{now.strftime('%A')}）"]
    for label, start, end in rows:
        span = start.isoformat() if start == end else f"{start.isoformat()} ~ {end.isoformat()}"
        lines.append(f"- {label}：{span}")
    lines.append(
        "- 说明：数据表里的时间字段可能是形如 yyyymmdd 的整数日期主键，"
        "也可能是独立的年 / 季度 / 月整数列，请按表结构选用对应的列来过滤。"
    )
    return "\n".join(lines)


def db_info_text() -> str:
    """数据库环境说明。"""
    settings = get_settings()
    return (
        f"数据库类型：MySQL 8.0（InnoDB，字符集 utf8mb4）\n"
        f"数据仓库库名：{settings.db_dw.database}\n"
        "注意：`year`、`month`、`day`、`quarter` 是 MySQL 保留字，"
        "作为字段名使用时必须用反引号包裹。"
    )


def table_infos_text(tables: Sequence[TableSpec]) -> str:
    """给生成 SQL 用的完整表结构文本。"""
    if not tables:
        return EMPTY

    blocks: list[str] = []
    for table in tables:
        head = f"表名：{table.name}"
        if table.alias:
            head += f"（{table.alias}）"
        head += "，维度表" if table.is_dimension else "，事实表"

        lines = [head]
        if table.description:
            lines.append(f"  说明：{table.description}")
        lines.append("  字段：")
        for column in table.columns:
            parts = [f"    - {column.name} {column.type}"]
            flags: list[str] = []
            if column.is_primary_key:
                flags.append("主键")
            if column.is_foreign_key and column.ref_table and column.ref_column:
                flags.append(f"外键 → {column.ref_table}.{column.ref_column}")
            if column.alias:
                flags.append(column.alias)
            if flags:
                parts.append("（" + "；".join(flags) + "）")
            if column.description:
                parts.append(f" — {column.description}")
            lines.append("".join(parts))
        blocks.append("\n".join(lines))
    return "\n\n".join(blocks)


def metric_infos_text(metrics: Sequence[MetricSpec]) -> str:
    """给生成 SQL 用的指标口径文本。"""
    if not metrics:
        return EMPTY

    blocks: list[str] = []
    for metric in metrics:
        head = f"指标：{metric.name}"
        if metric.alias:
            head += f"（{metric.alias}）"
        lines = [head]
        if metric.description:
            lines.append(f"  说明：{metric.description}")
        if metric.calculation:
            lines.append(f"  计算口径：{metric.calculation}")
        if metric.filter_condition:
            lines.append(f"  过滤条件：{metric.filter_condition}")
        if metric.time_range:
            lines.append(f"  时间口径：{metric.time_range}")
        blocks.append("\n".join(lines))
    return "\n\n".join(blocks)


def table_candidates_text(tables: Sequence[TableSpec]) -> str:
    """给筛选节点用的候选表结构（比给生成节点的那份更紧凑）。"""
    if not tables:
        return EMPTY

    lines: list[str] = []
    for table in tables:
        head = f"表 {table.name}"
        if table.alias:
            head += f"（{table.alias}）"
        lines.append(head)
        if table.description:
            lines.append(f"  说明：{table.description}")
        for column in table.columns:
            alias = f"（{column.alias}）" if column.alias else ""
            description = f" — {column.description}" if column.description else ""
            lines.append(f"  - {column.name} {column.type}{alias}{description}")
    return "\n".join(lines)


def metric_candidates_text(candidates: Sequence[Candidate]) -> str:
    """给筛选节点用的候选指标（取自召回结果，带描述）。"""
    if not candidates:
        return EMPTY

    lines: list[str] = []
    for candidate in candidates:
        alias = f"（{candidate.alias}）" if candidate.alias else ""
        description = f"：{candidate.description}" if candidate.description else ""
        lines.append(f"- {candidate.name}{alias}{description}")
    return "\n".join(lines)
