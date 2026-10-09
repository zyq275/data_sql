"""单轮问答的服务编排（TDD 5.1 / 12.2）。

这一层只做三件事：建初始状态、把图的进度转发出去、在结束时收口。

**为什么异常在这里被翻译成事件而不是往上抛**：SSE 的响应头在第一个事件推送时
就发出去了，此后 HTTP 状态码已经定型（TDD 12.2）。图跑到一半出错时再抛异常，
客户端只会看到连接被掐断，既没有状态码也没有原因——比一句明确的错误更糟。
所以流一旦开始，所有失败都走 ``type=error`` 事件。

**为什么 error 事件只在这里推**：图内部的 ``validate_sql`` 每次校验失败都会写
``error``，校正循环里可能失败三次。若每次都推事件，前端会连弹三条错误，而其中
两条马上就被修好了。这里的规则是"图停下来的时候，如果还带着错误，才报一次"。
"""

from __future__ import annotations

import logging
from collections.abc import AsyncIterator

from app.agent.context import QueryContext
from app.agent.events import DONE, ERROR
from app.agent.graph import get_graph
from app.agent.state import MAX_SQL_CORRECTIONS, QueryState, initial_state
from app.core.exceptions import AppException, ServiceError

logger = logging.getLogger(__name__)


class QueryService:
    """把一次自然语言提问跑成一条事件流。"""

    def __init__(self, graph=None) -> None:
        # 图只持有拓扑，依赖全从 context 进来，因此进程内共用一个编译结果即可
        self._graph = graph or get_graph()

    async def stream(self, query: str, *, user_id: str) -> AsyncIterator[dict]:
        """跑一遍图，逐个产出事件字典（SSE 帧的编码交给接口层）。

        产出的是**裸事件字典**而不是拼好的 SSE 文本：SSE 是传输细节，
        让它沉在服务层会让这一层没法被别的调用方（评测脚本、阶段 10 看板）复用。
        """
        state = initial_state(query)
        context = QueryContext(user_id=user_id)
        final: QueryState = state

        try:
            # 同时订阅 custom（节点推的进度/sql/result）与 values（整份状态快照）。
            # values 每个节点后都产一份，取最后一份即终态——这比再去 read 一次状态
            # 可靠：图跑完之后的读取要重新面对 checkpointer 语义，而这里拿到的
            # 就是图自己认定的终态。
            async for mode, chunk in self._graph.astream(
                state, stream_mode=["custom", "values"], context=context
            ):
                if mode == "custom":
                    yield chunk
                else:
                    final = chunk
        except AppException as exc:
            # 业务异常：message 已经是给人看的（约定见 core.exceptions），原样透出
            logger.info("问答中断（%s）：%s", exc.code, exc)
            yield {"type": ERROR, "code": exc.code, "message": exc.message}
            return
        except Exception:  # noqa: BLE001 —— 流已开始，任何异常都只能变成事件
            # 与 core.handlers 同一条规矩：堆栈进日志，对外只给一句通用提示。
            # 否则上游异常串（可能带连接串、内网地址）会直接出现在前端。
            logger.exception("问答图执行失败")
            yield {"type": ERROR, "code": ServiceError.code, "message": ServiceError.message}
            return

        if final.get("error"):
            yield self._gave_up(final)
            return

        yield {
            "type": DONE,
            "correction_count": final.get("correction_count", 0),
            "row_count": final.get("row_count", 0),
        }

    @staticmethod
    def _gave_up(state: QueryState) -> dict:
        """把"图带着错误停下来"变成一条明确的 error 事件。

        带上最后一次的校验原因，而不是只说"失败了"：用户据此能判断是自己问得
        含糊，还是库里压根没有这个字段——前者改问法，后者找数据团队，
        两种处置完全不同，而笼统的失败提示把判断成本全部推给了用户。
        同时点明已重试次数，让"又失败"和"再试一次也许能过"区分得开。
        """
        return {
            "type": ERROR,
            "code": state.get("error_code") or ServiceError.code,
            "message": (
                f"SQL 连续校正 {MAX_SQL_CORRECTIONS} 次仍未通过：{state['error']}"
            ),
        }
