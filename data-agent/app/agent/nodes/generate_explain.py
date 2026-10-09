"""生成自然语言解释（FR-EXPL-002）。

接在 ``execute_sql`` 之后，把"这条 SQL 干了什么、拿到了什么"讲成一段业务语言，
推 ``explain`` 事件，同时写进 ``state.explain_text`` 供服务层落库。

**为什么放在 execute_sql 之后而不是之前**：解释要"与 SQL 实际逻辑一致"
（阶段 8 验收标准 2）。SQL 生成阶段的意图与最终执行的语句是两回事——中间可能
经过校正，且只有执行完才知道筛出了多少行。拿着**实际执行的** SQL 与**实际返回的**
结果去解释，才谈得上"一致"；放在前面就只能解释一个也许被改写过的意图。
代价是它要多等一次取数，而这一步本来就在用户等待链路里。

**解释失败不判死整轮问答**：这是 P1 能力，而此刻结果已经推给用户了
（``execute_sql`` 先推 ``result``）。因为一段锦上添花的说明让用户看到一个完整的
答案之后连接被掐断，是拿次要的东西否掉主要的东西。所以失败只告警、写 None，
``done`` 照常推。与 ``resolve_reference`` 的降级是同一条取舍。

**但没有照抄它的长度上限**，两者的爆炸半径不一样：``resolve_reference`` 的输出会
被灌进此后**每一次** LLM 调用（三路召回、两个筛选、生成、校正），失控一行就拖慢
整条链路，所以它宁可退回原问句；``generate_explain`` 的输出是**终点**，
只推给前端、只写进一个 TEXT 列，不再流回任何提示词。为一个不再扩散的输出设上限，
代价是丢掉一段或许有用的说明，收益却接近于零。
"""

from __future__ import annotations

import logging

from langgraph.runtime import Runtime

from app.agent.context import QueryContext
from app.agent.events import EXPLAIN, emit, step
from app.agent.prompts import render
from app.agent.rendering import result_summary_text
from app.agent.state import QueryState
from app.core.exceptions import LLMServiceError
from app.core.text import strip_code_fence

logger = logging.getLogger(__name__)


@step("generate_explain")
async def generate_explain(state: QueryState, runtime: Runtime[QueryContext]) -> dict:
    """按实际执行的 SQL 与结果生成解释；失败即放弃解释，不影响本轮。"""
    sql = state.get("sql")
    if not sql:
        # 理论上传不到这里（execute_sql 必定写过 sql）。留着是因为"没有 SQL 却要解释
        # SQL"属于图被改坏的症状，此时返回一句没有依据的说明比返回空更糟。
        logger.warning("generate_explain 拿到空 SQL，跳过解释")
        return {"explain_text": None}

    prompt = render(
        "generate_explain",
        query=state.get("resolved_query") or state.get("query", ""),
        sql=sql,
        result=result_summary_text(
            state.get("columns") or [],
            state.get("rows") or [],
            row_count=state.get("row_count", 0),
            truncated=bool(state.get("truncated")),
        ),
    )

    try:
        raw = await runtime.context.llm.text(prompt)
    except LLMServiceError as exc:
        logger.warning("生成解释失败，本轮不附解释：%s", exc)
        return {"explain_text": None}

    text = strip_code_fence(raw).strip()
    if not text:
        # 空回复：模型没答，不是答错。解释为空时**不推事件**，
        # 前端据此就不渲染解释区；推一条空的反而要多写一段"空串怎么显示"的逻辑。
        logger.warning("生成解释返回空内容，本轮不附解释")
        return {"explain_text": None}

    emit(runtime.stream_writer, EXPLAIN, text=text)
    return {"explain_text": text}
