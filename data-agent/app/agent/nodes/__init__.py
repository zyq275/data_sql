"""Agent 图的十五个节点（TDD 5）。

集中在这里导出，是为了让 ``graph.py`` 只依赖一个模块名：节点被挪到别处、
或被拆成子包时，改动只落在这一行。同时它也是一份清单——TDD 5.1 图里
十五个方框，这里数一遍就能对上。

阶段 7 起比阶段 5 多出的那一个是 ``resolve_reference``：它是图的入口，
先消解指代再抽关键字。加在入口而不是别处，是为了让"下游统一消费改写后的问句"
成为一条结构性事实——所有节点读的都是 ``resolved_query``，谁都不必知道
自己拿到的是原文还是改写结果。

阶段 8 多出的是末尾的两个：``generate_explain`` 与 ``recommend_visualization``。
它们排在 ``execute_sql`` **之后**，因为解释与图表都要看实际结果；而它们又都在
``END`` 之前，所以在图的终态里拿得到——解释要落库（``explain_text``），
图表类型要进 ``viz`` 事件。
"""

from __future__ import annotations

from app.agent.nodes.add_extra_context import add_extra_context
from app.agent.nodes.correct_sql import correct_sql
from app.agent.nodes.execute_sql import execute_sql
from app.agent.nodes.extract_keywords import extract_keywords
from app.agent.nodes.filter_metric import filter_metric
from app.agent.nodes.filter_table import filter_table
from app.agent.nodes.generate_explain import generate_explain
from app.agent.nodes.generate_sql import generate_sql
from app.agent.nodes.merge_retrieved_info import merge_retrieved_info
from app.agent.nodes.recall_column import recall_column
from app.agent.nodes.recall_metric import recall_metric
from app.agent.nodes.recall_value import recall_value
from app.agent.nodes.recommend_visualization import recommend_visualization
from app.agent.nodes.resolve_reference import resolve_reference
from app.agent.nodes.validate_sql import validate_sql

__all__ = [
    "add_extra_context",
    "correct_sql",
    "execute_sql",
    "extract_keywords",
    "filter_metric",
    "filter_table",
    "generate_explain",
    "generate_sql",
    "merge_retrieved_info",
    "recall_column",
    "recall_metric",
    "recall_value",
    "recommend_visualization",
    "resolve_reference",
    "validate_sql",
]
