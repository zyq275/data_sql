"""SSE 事件构造与节点进度上报（TDD 7.3）。

**谁推什么**：节点只推 ``progress``（进度）；``sql`` / ``result`` / ``explain`` /
``viz`` 由真正产出它们的那一个节点推（分别是 ``execute_sql`` / ``generate_explain``
/ ``recommend_visualization``）；``error`` **统一由服务层推**，节点不推。这样
"一次失败只报一次错"是结构性保证的——若节点也推 error，异常路径上就会出现
"节点推一次、服务层再推一次"的重复事件，而前端多半会把它渲染成两条错误提示。

**产出型事件一律"谁算出来谁推"**，不在服务层统一转发：服务层要转发就得把每个
事件的数据先存一遍，于是同一份数据在图上有一份、服务层再抄一份，两处一旦不一致，
症状是"页面上显示的 SQL 和结果对不上"。节点手里正握着那份数据，推出去即可。
"""

from __future__ import annotations

import functools
import logging
from collections.abc import Awaitable, Callable
from typing import Any, Protocol

from langgraph.runtime import Runtime

from app.core.tracing import node_span, recall_attributes

logger = logging.getLogger(__name__)

PROGRESS = "progress"
SQL = "sql"
RESULT = "result"
EXPLAIN = "explain"
VIZ = "viz"
ERROR = "error"
DONE = "done"

START = "start"
FINISHED = "done"


class StreamWriter(Protocol):
    """``runtime.stream_writer`` 的类型（langgraph 未导出对应协议）。"""

    def __call__(self, data: Any) -> None: ...


def emit(writer: StreamWriter, event_type: str, **fields: Any) -> None:
    """推一个事件。"""
    writer({"type": event_type, **fields})


def progress(writer: StreamWriter, step: str, status: str) -> None:
    """推进度事件（TDD 7.3 的 progress：step + status）。"""
    emit(writer, PROGRESS, step=step, status=status)


#: 各节点的产出里"命中多少条"能数出来的字段 → 指标名。
#:
#: 集中在这张表里而不是让每个节点自己报数：报数一旦分散，必然有人报有人忘，
#: 而"某个节点没有指标"在图上只表现为某个面板缺一条线，事后极难发现——
#: 与进度事件当初集中在这里是同一个理由。
#:
#: 覆盖的是"召回管道上的每一站"，从三路召回到筛选到最终结果：
#: 只记召回数的话，召回很好、筛选全裁掉了这种情况在面板上看不出来。
COUNTED_KEYS = {
    "column_candidates": "columns",  # 字段召回命中
    "value_candidates": "values",  # 取值召回命中
    "metric_candidates": "metrics",  # 指标召回命中
    "retrieved": "retrieved",  # 三路合并去重后
    "table_infos": "tables",  # 筛选后保留的表
    "metric_infos": "selected_metrics",  # 筛选后保留的指标
    "row_count": "rows",  # 最终结果行数
}


def count_hits(source: object) -> dict[str, int]:
    """从节点的返回值（或图终态）里数出各环节的命中数。

    数的是**返回值**而不是状态：返回值才是"这个节点这次产出了什么"，
    状态里可能还留着上一个节点写进去、本节点没碰的字段——在回边（correct_sql
    → validate_sql）上，把状态里所有候选数都数一遍会得到一张几乎不变的表。

    服务层拿图终态调它，得到的是"这一轮最终命中多少"；只要调用点自己清楚
    手里这份东西是"某个节点的产出"还是"整轮的终态"，同一个函数就够用，
    不必维护第二张对照表——两张表早晚会有一张忘了改。
    """
    if not isinstance(source, dict):
        return {}
    counts: dict[str, int] = {}
    for key, metric in COUNTED_KEYS.items():
        value = source.get(key)
        if value is None:
            continue
        if isinstance(value, bool):
            # bool 是 int 的子类，不挡住的话 truncated=True 会被数成 1
            continue
        if isinstance(value, int):
            counts[metric] = value
        elif isinstance(value, (list, tuple)):
            counts[metric] = len(value)
    return counts


def step(name: str) -> Callable:
    """节点装饰器：进入推 start、正常返回推 done，同时量耗时、数命中。

    统一在这里做而不是每个节点手写两行，是因为手写必然有人写、有人忘；
    而"某个节点没有进度事件"在前端只表现为进度条卡在上一格，
    事后极难定位到底是哪个节点漏了。阶段 11 的节点耗时与召回命中数
    搭在这同一个钩子上，正是为了吃到这份"不可能漏"。

    异常时**不在这里推 error 事件**（见模块说明），只记一条带步骤名的日志后原样抛出：
    步骤名是排查时最需要的线索，而服务层拿不到"崩在哪个节点"。
    """

    def decorator(func: Callable[..., Awaitable[dict]]) -> Callable[..., Awaitable[dict]]:
        @functools.wraps(func)
        async def wrapper(state, runtime: Runtime) -> dict:
            progress(runtime.stream_writer, name, START)
            # span 包住的正是"节点在跑"这一段，异常也由它记到链路里；
            # 埋点失败绝不影响节点——tracing 里的辅助函数对没有活跃 span 的
            # 情况是空操作，图也照常在没装 provider 的进程里跑
            with node_span(name) as span:
                try:
                    result = await func(state, runtime)
                except Exception:
                    logger.warning("节点 %s 执行失败", name, exc_info=True)
                    raise
                span.set_attributes(recall_attributes(count_hits(result)))
            progress(runtime.stream_writer, name, FINISHED)
            return result

        return wrapper

    return decorator
