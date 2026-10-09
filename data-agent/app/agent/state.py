"""Agent 图的 State 定义（TDD 5.1）。

字段名严格照 TDD 5.1 的表：``query`` / ``resolved_query`` / ``session_id`` /
``history`` / ``correction_count`` / ``error``。

**关于 resolved_query**：阶段 5 没有 resolve_reference 节点（阶段 7 才有），入口
直接把 ``resolved_query`` 填成 ``query``，而所有下游节点**一律只读
``resolved_query``**。这是给阶段 7 留的缝——那时只需把"原样透传"换成"指代消解"，
十二个节点一行都不用改。反过来，若阶段 5 图省事让下游读 ``query``，阶段 7 就得
挨个改，且极易漏掉某一处，表现为"多轮对话里只有某一两个节点没吃上改写结果"
这种极难定位的 bug。

**关于并行写入**：三路召回节点写在三个不同的 key（``column_candidates`` /
``value_candidates`` / ``metric_candidates``），filter 两个节点同理。并行分支写
同一个 key 需要 reducer，而这里最自然的做法是各写各的、由合并节点去汇聚——
少了 reducer 的隐式合并语义，数据流向在代码里看得见。
"""

from __future__ import annotations

from typing import Any, TypedDict

from app.agent.knowledge import MetricSpec, TableSpec
from app.retrievers.retrieval import Candidate

# SQL 校正次数上限（BR-SQL-005 / FR-QA-008）：连续校正满 3 次仍不通过就终止，
# 不无限重试。取值必须与《开发步骤文档》阶段 5 验收标准一致。
MAX_SQL_CORRECTIONS = 3


class QueryState(TypedDict, total=False):
    """单轮问答在图中流转的全部状态。"""

    # ---- 入口输入（services.query 构建）----
    query: str
    """用户本轮的原始问题。"""
    resolved_query: str
    """指代消解后的完整问题。阶段 5 恒等于 query；下游节点统一消费这个字段。"""
    session_id: str | None
    """会话标识。阶段 5 尚无会话，恒为 None（阶段 7 接入 checkpointer）。"""
    history: list[dict]
    """会话历史。阶段 5 恒为空列表（阶段 7 由 checkpointer 注入）。"""

    # ---- 控制流 ----
    correction_count: int
    """SQL 校正次数，初值 0，上限 MAX_SQL_CORRECTIONS。"""
    error: str | None
    """validate_sql 写回，驱动条件边分流；None 表示校验通过。"""
    error_code: int | None
    """与 error 配套的业务码（TDD 12.1）。TDD 5.1 的 State 表只有 error 一个字段，
    这里多带一个码，是为了让服务层推的 error 事件能带上正确的业务码，
    而不用去猜"这次失败算哪一个 402xx"。"""

    # ---- extract_keywords ----
    column_keywords: list[str]
    value_keywords: list[str]
    metric_keywords: list[str]

    # ---- 三路召回（并行，各写各的 key）----
    column_candidates: list[Candidate]
    value_candidates: list[Candidate]
    metric_candidates: list[Candidate]

    # ---- merge_retrieved_info ----
    retrieved: list[Candidate]

    # ---- filter_table / filter_metric（并行，各写各的 key）----
    table_infos: list[TableSpec]
    metric_infos: list[MetricSpec]

    # ---- add_extra_context ----
    table_text: str
    metric_text: str
    date_text: str
    db_text: str

    # ---- generate_sql / correct_sql / validate_sql / execute_sql ----
    sql: str | None
    explain_sql: str | None
    """预检用的 EXPLAIN 语句；不可预检的语句类型为 None。"""
    columns: list[str]
    rows: list[list[Any]]
    row_count: int
    truncated: bool
    """结果是否因 max_result_rows 被截断（BR-SQL-004）。截断了就要如实说，
    否则用户会把"前 10000 行"当成"全部"。"""


def initial_state(query: str, *, session_id: str | None = None, history: list[dict] | None = None) -> QueryState:
    """构建入口状态。

    集中在一处而不是散在服务层里拼 dict：初值一旦漏项（比如忘了 ``correction_count``），
    条件边会拿到 ``KeyError`` 或拿 None 比较，症状离原因很远。
    """
    return QueryState(
        query=query,
        resolved_query=query,  # 阶段 7 换成 resolve_reference 的输出
        session_id=session_id,
        history=history or [],
        correction_count=0,
        error=None,
        error_code=None,
        columns=[],
        rows=[],
        row_count=0,
        truncated=False,
    )
