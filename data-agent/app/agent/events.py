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


def step(name: str) -> Callable:
    """节点装饰器：进入推 start、正常返回推 done。

    统一在这里做而不是每个节点手写两行，是因为手写必然有人写、有人忘；
    而"某个节点没有进度事件"在前端只表现为进度条卡在上一格，
    事后极难定位到底是哪个节点漏了。

    异常时**不在这里推 error 事件**（见模块说明），只记一条带步骤名的日志后原样抛出：
    步骤名是排查时最需要的线索，而服务层拿不到"崩在哪个节点"。
    """

    def decorator(func: Callable[..., Awaitable[dict]]) -> Callable[..., Awaitable[dict]]:
        @functools.wraps(func)
        async def wrapper(state, runtime: Runtime) -> dict:
            progress(runtime.stream_writer, name, START)
            try:
                result = await func(state, runtime)
            except Exception:
                logger.warning("节点 %s 执行失败", name, exc_info=True)
                raise
            progress(runtime.stream_writer, name, FINISHED)
            return result

        return wrapper

    return decorator
