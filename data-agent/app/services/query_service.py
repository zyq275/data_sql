"""问答的服务编排（TDD 5.1 / 5.3 / 12.2）。

这一层做四件事：装配会话上下文、建初始状态、把图的进度转发出去、在结束时落库并收口。

**为什么分成 ``prepare`` 与 ``stream`` 两步**：SSE 的响应头在第一次迭代事件流时
就发出去了（``StreamingResponse`` 先发 ``http.response.start`` 再拉第一块内容），
此后 HTTP 状态码已经定型（TDD 12.2）。于是"会话不存在"和"这个会话不归你"
这两种失败，如果发生在流里，就只能表现为**连接被掐断**——没有状态码、没有原因，
而 TDD 7.1 要求它们是一个正常的 404 信封。所以凡是会失败、又必须给出状态码的事
（归属校验、取上文、选 checkpointer）都挪进 ``prepare``，在开流之前做完。

**为什么异常在这里被翻译成事件而不是往上抛**：同一条道理的另一半。流一旦开始，
再抛异常客户端只会看到连接被掐断，比一句明确的错误更糟。所以开流之后的所有失败
都走 ``type=error`` 事件。

**为什么 error 事件只在这里推**：图内部的 ``validate_sql`` 每次校验失败都会写
``error``，校正循环里可能失败三次。若每次都推事件，前端会连弹三条错误，而其中
两条马上就被修好了。这里的规则是"图停下来的时候，如果还带着错误，才报一次"。

**落库与终态事件的先后**：先落库、后推 done。反过来的话，客户端收到 done 就断开、
服务端还在写库，此刻进程被重启就会丢掉这一轮记录；先落库的代价只是进度条多转一下。
"""

from __future__ import annotations

import logging
import math
import time
from collections.abc import AsyncIterator
from dataclasses import dataclass, field
from typing import Any

from sqlalchemy.ext.asyncio import AsyncSession

from app.agent.context import QueryContext
from app.agent.events import DONE, ERROR, RESULT, SQL
from app.agent.graph import get_graph
from app.agent.rendering import MAX_HISTORY_TURNS
from app.agent.state import MAX_SQL_CORRECTIONS, QueryState, initial_state
from app.core.exceptions import AppException, ServiceError
from app.models import Conversation
from app.repositories.conversation import ConversationRepository, MessageRepository
from app.repositories.query_history import (
    STATUS_FAILED,
    STATUS_SUCCESS,
    QueryHistoryRepository,
)
from app.services.conversation_service import ConversationService

logger = logging.getLogger(__name__)

MAX_PREVIEW_ROWS = 20
"""落库的结果预览最多留多少行。

预览是给"我上次问了什么、返回了个什么"看的，不是用来重放数据的：真要那份数据
应当重跑一次（重跑拿到的还是**现在**的数，而库里存的是当时的快照，本来也不该
拿来当数据源）。不设上限的话，一次 1 万行的查询会往 ``message`` 与 ``query_history``
各塞一份上万行的 JSON，两处都只为展示那二十行。
"""

#: 会话消息的 role 取值。一轮问答只落**一行**（role=assistant），
#: 行内同时带问题与答案，理由见 ``_persist``。
ROLE_ASSISTANT = "assistant"


@dataclass
class TurnContext:
    """一轮问答在开流之前就该备齐的东西。

    这些字段的共同点是"取它们的过程可能失败"，而失败必须在开流前发生（见模块说明）。
    放在一个对象里传，是为了让接口层看见"要先把准备做完"这件事——
    散成三个参数时，漏传一个的后果是它在流里才被现取，而那正是要避免的。
    """

    graph: Any
    conversation: Conversation | None = None
    history: list[dict] = field(default_factory=list)


class QueryService:
    """把一次自然语言提问跑成一条事件流，并记录这一轮。"""

    def __init__(self, session: AsyncSession | None = None) -> None:
        """``session`` 为 None 即**不落库**，只产出事件流。

        这不是给接口用的（HTTP 入口总是带着请求级会话），而是给两类不需要数据库的
        调用方：验证事件收口逻辑的单测，以及评测脚本的干跑（只想看生成什么 SQL，
        不想在库里留下一堆评测噪声）。与 ``build_graph(checkpointer=None)`` 同一条
        取舍：把"要不要持久化"显式化，好过一次真连库的测试。
        """
        self.session = session
        self.conversations = ConversationService(session)
        self.conversation_repo = ConversationRepository(session)
        self.messages = MessageRepository(session)
        self.history = QueryHistoryRepository(session)

    async def prepare(self, user_id: str, session_id: str | None) -> TurnContext:
        """开流前的准备：校验会话归属、加载上文、选定 checkpointer。

        ``session_id`` 为 None 就是一次性的单轮提问——不校验、不取上文、
        也不给 checkpointer 线程，但**仍然会落查询历史**：它同样是一次
        "我问过什么"，而 query_history 的 session_id 本来就可空。
        """
        conversation: Conversation | None = None
        history: list[dict] = []
        if session_id:
            if self.session is None:
                # 无库模式没有上文可查、也校验不了归属。与其让它带着一个空历史
                # 跑下去（表现为"追问被当成新问题"），不如在这里明确拒绝
                raise RuntimeError(
                    "session=None 的无库模式不能用于多轮会话："
                    "会话归属校验与上文加载都要读库"
                )
            conversation = await self.conversations.owned(user_id, session_id)
            history = await self._load_history(session_id)
        return TurnContext(
            # 多轮用挂 checkpointer 的那张图；一次性提问用不挂的——
            # 挂了就强制要 thread_id，而没有会话 id 就只能现编一个，
            # 等于每问一句都往 Redis 留一个永不回收的线程（见 agent.graph.get_graph）
            graph=await get_graph(persist=conversation is not None),
            conversation=conversation,
            history=history,
        )

    async def stream(
        self, query: str, *, user_id: str, turn: TurnContext
    ) -> AsyncIterator[dict]:
        """跑一遍图，逐个产出事件字典（SSE 帧的编码交给接口层）。

        产出的是**裸事件字典**而不是拼好的 SSE 文本：SSE 是传输细节，
        让它沉在服务层会让这一层没法被别的调用方（评测脚本、阶段 10 看板）复用。
        """
        started = time.perf_counter()
        session_id = turn.conversation.id if turn.conversation else None
        state = initial_state(query, session_id=session_id, history=turn.history)
        context = QueryContext(user_id=user_id)
        final: QueryState = state
        trace = _Trace()
        failure: dict | None = None

        try:
            # 同时订阅 custom（节点推的进度/sql/result）与 values（整份状态快照）。
            # values 每个节点后都产一份，取最后一份即终态——这比再去 read 一次状态
            # 可靠：图跑完之后的读取要重新面对 checkpointer 语义，而这里拿到的
            # 就是图自己认定的终态。
            async for mode, chunk in turn.graph.astream(
                state,
                stream_mode=["custom", "values"],
                context=context,
                config=_thread_config(session_id),
            ):
                if mode == "custom":
                    trace.observe(chunk)
                    yield chunk
                else:
                    final = chunk
        except AppException as exc:
            # 业务异常：message 已经是给人看的（约定见 core.exceptions），原样透出
            logger.info("问答中断（%s）：%s", exc.code, exc)
            failure = {"type": ERROR, "code": exc.code, "message": exc.message}
        except Exception:  # noqa: BLE001 —— 流已开始，任何异常都只能变成事件
            # 与 core.handlers 同一条规矩：堆栈进日志，对外只给一句通用提示。
            # 否则上游异常串（可能带连接串、内网地址）会直接出现在前端。
            logger.exception("问答图执行失败")
            failure = {
                "type": ERROR,
                "code": ServiceError.code,
                "message": ServiceError.message,
            }

        if failure is None and final.get("error"):
            failure = self._gave_up(final)

        # 落库失败与"图跑挂"是两件事：前者不该变成后者那样的事件（用户已经看到答案了）
        await self._persist(
            user_id=user_id,
            conversation=turn.conversation,
            session_id=session_id,
            query=query,
            final=final,
            trace=trace,
            duration_ms=int((time.perf_counter() - started) * 1000),
            ok=failure is None,
        )

        if failure is not None:
            yield failure
            return

        yield {
            "type": DONE,
            "correction_count": final.get("correction_count", 0),
            "row_count": final.get("row_count", 0),
        }

    async def _load_history(self, conversation_id: str) -> list[dict]:
        """取最近若干轮上文，转成状态里 ``history`` 的形态（早的在前）。

        只取 ``MAX_HISTORY_TURNS`` 条：提示词模板也只渲染这么多（见
        ``agent.rendering.history_text``），多读的部分永远进不了上下文。

        取"最近 N 条"天然就是"本次提问之前的轮次"——本轮尚未落库，
        不必额外排除自己。
        """
        turns = await self.messages.list_by_conversation(
            conversation_id, limit=MAX_HISTORY_TURNS
        )
        return [
            {"query": turn.query_text, "sql": turn.generated_sql}
            for turn in reversed(list(turns))
        ]

    async def _persist(
        self,
        *,
        user_id: str,
        conversation: Conversation | None,
        session_id: str | None,
        query: str,
        final: QueryState,
        trace: "_Trace",
        duration_ms: int,
        ok: bool,
    ) -> None:
        """把这一轮落库：会话消息 + 会话活跃时间 + 查询历史。

        **一次提交**：三行出自同一份数据、同一个时刻，分开提交只会得到
        "会话详情里看得到这一轮、历史列表里却少一条"这种半份记录，
        而两边各自显示正常，没人会去对账。

        **失败隔离（NFR-STAB-003）**：用户的问答已经跑完、事件也快推完了，
        这里再抛异常只会让他看到一个完整的答案之后连接被掐断。所以失败只记日志
        （带用户 id 与会话 id，便于事后补数）后照常推 done。

        **一轮一行、role=assistant**：问题与它自己的答案写在同一行里。
        拆成 user / assistant 两行看起来更"规范"，但 MySQL 的 ``DATETIME`` 只到秒，
        同一秒内写入的两行无法可靠定序——一旦反过来，多轮历史就会把上一轮的问题
        配到这一轮的 SQL 上，而 resolve_reference 拿到错乱的上文会改写出一个
        看着合理、实则跑偏的问题。这里要的是"配得准"，不是"分得细"。
        """
        if self.session is None:
            return
        try:
            sql = trace.sql or final.get("sql")
            preview = _preview(trace.result)

            if conversation is not None:
                await self.messages.add_turn(
                    conversation_id=conversation.id,
                    user_id=user_id,
                    role=ROLE_ASSISTANT,
                    query_text=query,
                    generated_sql=sql,
                    result_preview=preview,
                    # 自然语言解释由阶段 8 的 explain 节点产出，此处如实留空，
                    # 不拿 EXPLAIN 语句充数（那是校验用的语句，不是解释）
                    explain_text=None,
                )
                await self.conversation_repo.touch(conversation)

            await self.history.create(
                user_id=user_id,
                session_id=session_id,
                query_text=query,
                generated_sql=sql,
                result_preview=preview,
                explain_text=None,
                status=STATUS_SUCCESS if ok else STATUS_FAILED,
                duration_ms=duration_ms,
            )
            await self.session.commit()
        except Exception as exc:  # noqa: BLE001 —— 见上，落库失败不影响主流程
            await self.session.rollback()
            logger.warning(
                "本轮问答落库失败（用户 %s，会话 %s）：%s",
                user_id,
                session_id or "无",
                exc,
                exc_info=True,
            )

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


class _Trace:
    """从事件流里捡出这一轮最终生效的 SQL 与结果集。

    落库写的是**用户实际看到的那一份**，而不是图终态里的那一份：两者在正常情况下
    相同，但当图在 generate_sql 之后、execute_sql 之前失败时，终态里没有 ``sql``
    而事件已经推出去过——这时该记的是推出去的那条，否则历史里会留下
    "用户看到过 SQL、历史里却空空如也"的记录。
    """

    def __init__(self) -> None:
        self.sql: str | None = None
        self.result: dict | None = None

    def observe(self, event: dict) -> None:
        kind = event.get("type")
        if kind == SQL:
            self.sql = event.get("sql")
        elif kind == RESULT:
            data = event.get("data")
            if isinstance(data, dict):
                self.result = data


def _thread_config(session_id: str | None) -> dict | None:
    """会话 id → checkpointer 的 thread_id（ADR-006）。

    没有会话时返回 None：不带 thread_id 时 langgraph 不落检查点，
    正是一次性提问想要的——否则每一句无会话的提问都会在 Redis 里留下一个
    永不回收的线程。
    """
    if not session_id:
        return None
    return {"configurable": {"thread_id": session_id}}


def _preview(result: dict | None) -> dict | None:
    """把结果事件裁成落库用的预览。

    行值可能是 ``Decimal`` / ``date`` / ``datetime``，它们不是 JSON 原生类型，
    而 ``JSON`` 列的序列化不做兜底——直接塞进去会在 commit 时抛 ``TypeError``，
    表现为"查询成功、历史却永远写不进去"。所以在入口处统一转成字符串。
    """
    if not result:
        return None
    rows = result.get("rows") or []
    return {
        "columns": list(result.get("columns") or []),
        "rows": [[_scalar(value) for value in row] for row in rows[:MAX_PREVIEW_ROWS]],
        "row_count": result.get("row_count", len(rows)),
        "truncated": bool(result.get("truncated", False)),
    }


def _scalar(value: Any) -> Any:
    """能进 JSON 列的原样保留，其余转字符串。

    NaN / Infinity 也算"进不去"：``json`` 会把它们写成裸的 ``NaN``，那不是合法
    JSON，MySQL 的 JSON 列会直接拒收。
    """
    if value is None or isinstance(value, (bool, int, str)):
        return value
    if isinstance(value, float):
        return value if math.isfinite(value) else str(value)
    return str(value)
