"""Text-to-SQL 的图编排（TDD 5.1）。

```
START → resolve_reference → extract_keywords ─┬→ recall_column ─┐
                                              ├→ recall_value  ─┼→ merge_retrieved_info
                                              └→ recall_metric ─┘        │
                                                       ┌────────────────┴────────────────┐
                                                       ▼                                 ▼
                                                  filter_table                    filter_metric
                                                       └────────────────┬────────────────┘
                                                                        ▼
                                                              add_extra_context
                                                                        ↓
                                                                  generate_sql
                                                                        ↓
                                       ┌───────────────────────────→ validate_sql
                                       ↓                                    │
                                 correct_sql ←── 未通过且 correction_count < 3 ┤
                                                                            │
                                 execute_sql ←────────── 通过 ────────────────┤
                                       │                                    │
                                       ▼                                    │
                                generate_explain                            │
                                       │                                    │
                                       ▼                                    │
                            recommend_visualization                         │
                                       │                                    │
                                       ▼                                    │
                                      END ←─── 未通过且 correction_count ≥ 3 ─┘
```

**并行分支的合并语义**：三路召回、两个 filter 都用 ``add_edge([...], next)`` 这种
"多源一汇"的写法，langgraph 会等**所有**上游都跑完才推进下游。这不是语法糖而是
正确性要求——汇合节点读的是各路结果拼起来的完整视图，若某个分支先到就先跑，
它会看到半份候选集，选出半份表，最终 SQL 少一个 JOIN，且不报任何错。

**为什么 correct_sql 与 validate_sql 之间是**回边**而不是把校正塞进一个循环**：
图上的边即流程，循环以边的形式表达时，"最多三轮"这个约束由条件边显式持有；
塞进节点内部 while 循环的话，重试次数就藏在某个函数体里，而它恰恰是需要被
验收、被日志、被未来改动者看见的东西。

**阶段 7 起挂 checkpointer**：编译时带上它，运行时用 ``thread_id=会话 id``
指定线程，于是同一会话的每一轮都读写同一份状态快照（ADR-006）。

**阶段 8 在 execute_sql 之后接了两个节点**（解释与图表推荐）。它们放在执行之后
而不是之前，是因为两者都要看真实结果才谈得上"一致"；串行而不并行，
是因为一个要调模型、一个是纯计算，并行省下的时间抵不上事件次序变得不确定的代价
（见下方边的注释）。
"""

from __future__ import annotations

import asyncio

from langgraph.checkpoint.base import BaseCheckpointSaver
from langgraph.graph import END, START, StateGraph

from app.agent.context import QueryContext
from app.agent.nodes import (
    add_extra_context,
    correct_sql,
    execute_sql,
    extract_keywords,
    filter_metric,
    filter_table,
    generate_explain,
    generate_sql,
    merge_retrieved_info,
    recall_column,
    recall_metric,
    recall_value,
    recommend_visualization,
    resolve_reference,
    validate_sql,
)
from app.agent.state import MAX_SQL_CORRECTIONS, QueryState

GIVE_UP = "give_up"


def route_after_validate(state: QueryState) -> str:
    """validate_sql 之后往哪走。

    分三种，且顺序不能改：

    1. ``error`` 为 None —— 校验过了（含"只读安全通过但无法 EXPLAIN"的情况），去执行。
    2. 连续校正已达上限 —— 交给 END 终止。**必须在判定 correct_sql 之前**，
       否则第 4 轮校正会被放行，上限形同虚设。
    3. 其余 —— 还有额度，交给 correct_sql 再试一次。

    返回 ``error`` 而不是只加计数：校验结论是 validate_sql 的产物，分流只做判断。
    若在分流里也去调一次 ``assert_readonly``，同一件事就有了两处实现，
    两处将来必然有一处忘记更新。
    """
    if state.get("error") is None:
        return "execute_sql"
    if state.get("correction_count", 0) >= MAX_SQL_CORRECTIONS:
        return GIVE_UP
    return "correct_sql"


def build_graph(checkpointer: BaseCheckpointSaver | None = None):
    """构建并编译问答图。

    ``compile()`` 出来的对象是**可复用**的：它只持有图的拓扑与 checkpointer，
    所有会话相关的依赖都从 ``context`` 进来，因此进程内建一次即可，
    不必每次请求重建。

    ``checkpointer`` 取默认值 None 时图不带任何持久化——发一条消息、跑完就丢，
    这正是测试要的：绝大多数图行为（分支、回边、事件顺序）与会话无关，
    给它们配上 Redis 只会让测试依赖外部服务。真正带会话的入口是 ``get_graph``。
    """
    builder = StateGraph(QueryState, context_schema=QueryContext)

    builder.add_node("resolve_reference", resolve_reference)
    builder.add_node("extract_keywords", extract_keywords)
    builder.add_node("recall_column", recall_column)
    builder.add_node("recall_value", recall_value)
    builder.add_node("recall_metric", recall_metric)
    builder.add_node("merge_retrieved_info", merge_retrieved_info)
    builder.add_node("filter_table", filter_table)
    builder.add_node("filter_metric", filter_metric)
    builder.add_node("add_extra_context", add_extra_context)
    builder.add_node("generate_sql", generate_sql)
    builder.add_node("validate_sql", validate_sql)
    builder.add_node("correct_sql", correct_sql)
    builder.add_node("execute_sql", execute_sql)
    builder.add_node("generate_explain", generate_explain)
    builder.add_node("recommend_visualization", recommend_visualization)

    builder.add_edge(START, "resolve_reference")
    builder.add_edge("resolve_reference", "extract_keywords")

    # 三路召回并行；写法是"扇出 + 汇合"两步，汇合点等齐三个分支
    for node in ("recall_column", "recall_value", "recall_metric"):
        builder.add_edge("extract_keywords", node)
    builder.add_edge(["recall_column", "recall_value", "recall_metric"], "merge_retrieved_info")

    # 取表与取指标互不依赖（各自只读候选集），同样并行
    for node in ("filter_table", "filter_metric"):
        builder.add_edge("merge_retrieved_info", node)
    builder.add_edge(["filter_table", "filter_metric"], "add_extra_context")

    builder.add_edge("add_extra_context", "generate_sql")
    builder.add_edge("generate_sql", "validate_sql")

    builder.add_conditional_edges(
        "validate_sql",
        route_after_validate,
        {
            "execute_sql": "execute_sql",
            "correct_sql": "correct_sql",
            GIVE_UP: END,
        },
    )
    # 回边：改完重新校验，计数器在 correct_sql 里增长，由上面的条件边兜底
    builder.add_edge("correct_sql", "validate_sql")
    # 执行之后依次补解释与图表推荐（阶段 8）。两者串行而非并行：
    # generate_explain 是 LLM 调用、recommend_visualization 是纯计算，
    # 并行省下的那点时间抵不上"两个节点同时往一个流里写事件"引入的次序不确定。
    # 顺序固定为解释在前、图表在后，前端因此可以假定 viz 到达时解释已经到手。
    builder.add_edge("execute_sql", "generate_explain")
    builder.add_edge("generate_explain", "recommend_visualization")
    builder.add_edge("recommend_visualization", END)

    return builder.compile(checkpointer=checkpointer)


_graphs: dict[bool, object] = {}
_graph_lock = asyncio.Lock()


async def get_graph(*, persist: bool = True):
    """取进程内共享的编译结果。

    **为什么两张图**：``persist=True`` 的那张挂着 checkpointer，而 langgraph 对
    挂了 checkpointer 的图**强制要求**每次调用都给 ``thread_id``——给不出来就直接
    抛错（实测：``Checkpointer requires one or more of the following 'configurable'
    keys: thread_id, ...``）。一次性提问（不带 session_id）没有线程可归属，只能
    现编一个，而那样每问一句都会在 Redis 里留下一个永不回收的线程。
    另编一张不带 checkpointer 的图，既省掉这份垃圾，也让"这次问答不留状态"
    在调用处一眼可见。

    编译有实际开销（校验拓扑、准备各分支），而编译产物只依赖图的静态结构；
    每次请求重建等于白交一遍这份开销。缓存住也顺带保证了"全进程只有一张图"——
    图一旦被误建成可变的（比如往节点里塞状态），多份实例会让问题只在部分请求上出现。

    **为什么是 async**：选 checkpointer 要真连一次 Redis（见 ``agent.checkpoint``），
    那是 I/O。硬要在同步函数里做，就只能"先假定 Redis 可用、失败了再说"，
    而失败点会落到某一轮问答的中间。
    """
    if persist in _graphs:
        return _graphs[persist]
    async with _graph_lock:
        if persist not in _graphs:
            checkpointer = None
            if persist:
                from app.agent.checkpoint import get_checkpointer

                checkpointer = await get_checkpointer()
            _graphs[persist] = build_graph(checkpointer)
        return _graphs[persist]


def reset_graph() -> None:
    """丢弃编译结果，下次重新构建。只给测试与显式重连用。"""
    _graphs.clear()
