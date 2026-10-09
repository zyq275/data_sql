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

import sqlparse

from app.agent.knowledge import MetricSpec, TableSpec
from app.core.config import get_settings
from app.retrievers.retrieval import Candidate

#: 会话历史最多回放多少轮。
#: 取 5 而不是全部：多轮对话里"那 / 换成"指代的是**最近一轮**（BR-CONV-002），
#: 更早的轮次对改写几乎没有贡献，却会实打实地挤占提示词预算——表结构与指标口径
#: 才是生成 SQL 时真正需要篇幅的部分，被历史挤掉会让 SQL 质量悄悄下滑。
MAX_HISTORY_TURNS = 5
EMPTY = "（无）"

#: 没有会话历史时给模型的说明。刻意与 EMPTY 区分开：EMPTY 表达"这一类东西是空的"，
#: 而这里要表达的是"这是第一轮"，它直接决定 resolve_reference 该不该改写。
NO_HISTORY = "（这是本次会话的第一轮，没有上文）"


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


def where_clause(sql: str | None) -> str | None:
    """从 SQL 里抠出 WHERE 条件（取不到就返回 None）。

    会话历史要说清"上文问的是什么"，问题原文和 SQL 各自都只说了一半：
    "各季度销售额"看不出范围，"只看华东"之后 SQL 里的 ``WHERE region='华东'``
    才是真正的范围。把 WHERE 单独提一行，是因为它会被下一轮的"那 / 换成"直接指代，
    埋在整条 SQL 里时模型容易只看 SELECT 列而漏掉它。

    按 **token** 定位而不是按字符串找 "WHERE"：后者会被字符串字面量里的同名词
    （``WHERE note = 'where'``）或注释骗到，切出一段莫名其妙的条件，
    而这种错误在提示词里完全看不出来——模型会照着一个错的条件去改写问句。

    解析失败一律返回 None 而不是抛错：这只是给提示词锦上添花的一段文本，
    而这条 SQL 是模型自己生成的、语法偏怪是常有的事，不该让整轮问答失败。
    """
    if not sql:
        return None
    try:
        for statement in sqlparse.parse(sql):
            tokens = list(statement.flatten())
            for index, token in enumerate(tokens):
                if token.ttype is sqlparse.tokens.Keyword and token.value.upper() == "WHERE":
                    # 从 WHERE 起拼到末尾：GROUP BY / ORDER BY 混进来也无妨，
                    # 它们同样属于"这一轮查询的约束"，多给一点比漏掉安全
                    clause = "".join(t.value for t in tokens[index:])
                    normalized = " ".join(clause.split()).rstrip(";").strip()
                    return normalized or None
    except Exception:  # noqa: BLE001 —— 纯属尽力而为
        return None
    return None


def history_text(turns: Sequence[dict]) -> str:
    """把会话历史渲染成提示词片段（TDD 5.3：上文问题 / SQL / 过滤条件）。

    没有历史时返回一句明确的说明，而不是空串：空串在提示词里表现为这一段凭空消失，
    模型无从区分"这是第一轮"与"历史没取到"。这两者的处置完全相反——前者应当原样
    透传，后者若被当成前者，用户会看到"那华东呢"被当作一个完整问题直接去查。
    """
    if not turns:
        return NO_HISTORY

    blocks: list[str] = []
    for index, turn in enumerate(turns[-MAX_HISTORY_TURNS:], start=1):
        lines = [f"第 {index} 轮"]
        query = (turn.get("query") or "").strip()
        if query:
            lines.append(f"  问题：{query}")
        sql = (turn.get("sql") or "").strip()
        if sql:
            # 折成一行：SQL 里的换行在提示词中不承载语义，却会让"第 N 轮"的缩进错乱
            lines.append(f"  SQL：{' '.join(sql.split())}")
            condition = where_clause(sql)
            if condition:
                lines.append(f"  过滤条件：{condition}")
        blocks.append("\n".join(lines))
    return "\n".join(blocks)


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
