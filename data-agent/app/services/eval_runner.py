"""批量评测的执行与判定（FR-EVAL-002，BR-EVAL-001）。

**评测跑的就是线上那条链路**：每条样本都经 ``QueryService`` 走一遍完整的图
（召回 → 筛选 → 生成 → 校验 → 执行），而不是另写一套"简化版"。另写一套的话，
评测出来的是那个简化版的成绩，与用户实际得到的回答无关。

**隔离靠"没有会话"而不是靠"记得别写"**（BR-EVAL-001）：
``QueryService(session=None)`` 在代码里就没有可写的库会话，图也走
``get_graph(persist=False)`` 那张不挂 checkpointer 的版本，于是
conversation / message / query_history / Redis 线程四样都不可能被写。
换成"用一个真会话、但要小心地不 commit"，隔离就成了纪律问题，早晚会破。

**准确率按结果集判**（阶段 10 定稿的口径）：样本给了 ``expected_result`` 就用它；
否则执行 ``expected_sql`` 拿它的结果来比；两条路都走不通（比如标准 SQL 在数仓上
根本跑不动）才退化成 SQL 文本比对。之所以不直接比 SQL 文本——同一份意图可以有
无数种写法（别名、JOIN 顺序、括号），文本不同不等于结果不同，用文本判会把
正确答案判成错的，而准确率是给人看的结论，宁可口径保守也不要它虚低。

**同一个道理对列名也成立**，这一点是实测踩出来的：标准 SQL 写 ``COUNT(*)``，数仓
给出的列名就是字面的 ``COUNT(*)``，而模型写 ``... AS 订单量`` 得到的列名是 ``订单量``，
两边算出来的是同一张表。所以比对时**列名只用来对齐、不用来判等**（见 ``rows_equal``）：
名字对得上就按名字对齐，对不上而列数相同就按位置比。列别名是写法，列数与取值才是答案。

**召回命中率**：以标准 SQL 用到的表为准，看它们有没有全部进入"生成 SQL 时看得到
的那份表清单"。它回答的是知识库维护者最关心的那个问题——这道题需要的表，召回
与筛选有没有把它漏掉。
"""

from __future__ import annotations

import logging
import math
import time
from collections import Counter
from collections.abc import Sequence
from dataclasses import dataclass
from decimal import Decimal

from app.agent.events import ERROR, RESULT, SQL
from app.agent.state import QueryState
from app.clients.mysql import session_scope
from app.core.exceptions import AppException
from app.core.sql_security import assert_readonly, referenced_tables
from app.models import EvalCase
from app.repositories.dw import DwRepository
from app.repositories.eval import EvalCaseRepository, EvalRunRepository
from app.repositories.meta import MetaRepository
from app.schemas.eval import EvalCaseOutcome, EvalRunDetail
from app.services.query_service import QueryService

logger = logging.getLogger(__name__)

#: 数值比对的容差位数。两侧分别来自 MySQL 与 JSON，同一个 ``Decimal(18,2)``
#: 经由 float 走一圈回来，末位可能有 1e-13 量级的差；比到小数点后 6 位
#: 既盖住了浮点噪声，又不会把真实差异（金额到分）抹平。
_VALUE_PRECISION = 6


# ==========================================================================
# 汇总与判定（纯函数，脱离数据库可测）
# ==========================================================================


@dataclass(frozen=True, slots=True)
class Metrics:
    """一次运行的四个指标；判不出来的指标是 None，不是 0。"""

    accuracy: Decimal | None
    recall_hit_rate: Decimal | None
    execute_success_rate: Decimal | None
    avg_duration_ms: int | None


def summarize(outcomes: Sequence[EvalCaseOutcome]) -> Metrics:
    """逐条结果 → 汇总指标。

    **召回命中率的分母是"判得出来的样本数"，不是总数**：没有 ``expected_sql`` 的
    样本无法知道该召回哪些表，把它算作未命中会让这个指标虚低，而虚低的指标会
    误导知识库维护者去调一个本来没坏的召回链路。
    """
    total = len(outcomes)
    if not total:
        return Metrics(None, None, None, None)

    judged = [o for o in outcomes if o.recall_hit is not None]
    return Metrics(
        accuracy=_rate(sum(1 for o in outcomes if o.correct), total),
        recall_hit_rate=_rate(sum(1 for o in judged if o.recall_hit), len(judged))
        if judged
        else None,
        execute_success_rate=_rate(sum(1 for o in outcomes if o.executed), total),
        avg_duration_ms=int(sum(o.duration_ms for o in outcomes) / total),
    )


def _rate(hit: int, total: int) -> Decimal:
    """比例 → ``DECIMAL(6,4)``。

    先 ``round`` 再 ``Decimal(str(...))``：直接 ``Decimal(hit / total)`` 会把二进制
    浮点的那一串尾巴（0.6666666666666666）塞进只留 4 位小数的列，MySQL 在严格模式下
    直接拒收，报的还是个看不懂的截断错误。
    """
    return Decimal(str(round(hit / total, 4)))


def normalize_sql(sql: str | None) -> str:
    """SQL 文本比对的归一化：去首尾空白、去结尾分号、折叠空白、统一小写。"""
    return " ".join((sql or "").strip().rstrip(";").lower().split())


def _normalize_value(value: object) -> object:
    """行值的归一化。

    整数原样保留：订单号是 bigint，经 float 走一圈会掉精度（``2**53`` 之后
    相邻整数不再可分辨），而"金额对得上、订单号对不上"这种差异最容易被当成噪声。
    ``1`` 与 ``1.0`` 归一化到同一个值——MySQL 的 ``SUM`` 有时返回 Decimal、
    有时返回整数，它们指的是同一个数。
    """
    if value is None or isinstance(value, bool):
        return value
    if isinstance(value, int):
        return value
    if isinstance(value, (float, Decimal)):
        number = float(value)
        if not math.isfinite(number):
            return str(value)
        return int(number) if number.is_integer() else round(number, _VALUE_PRECISION)
    return str(value).strip()


def _normalize_row(row: Sequence[object]) -> tuple:
    return tuple(_normalize_value(value) for value in row)


def rows_equal(actual: dict | None, expected: dict | None) -> tuple[bool, str]:
    """结果集比对（顺序无关的多重集比较）。返回 (是否一致, 说明)。

    **列名只用来对齐，不用来判等。** 列别名是写法而不是答案的一部分，而且模型
    无从知道标准 SQL 会取什么列名：标准 SQL 写 ``COUNT(*)``，数仓给出的列名就是
    字面的 ``COUNT(*)``，而模型写 ``COUNT(DISTINCT order_id) AS 订单量`` 得到的列名
    是 ``订单量``。两边算出来的是同一张表，按列名判等会把这份正确答案判成错的——
    这与"不比 SQL 文本"是同一个道理，只是往下一层：文本、别名都是表达，结果才是答案。

    于是分三种情况：

    - 两边的列名集合对得上 → 按列名对齐（同一份结果换个列顺序是同一个答案）；
    - 对不上但列数相同 → **按列位置比对**，并在说明里写明这是退化比对；
    - 列数都不同 → 判不等（那确实是两份不同的结果）。

    行序无关，同理：``ORDER BY`` 本来就不保证跨索引、跨机器返回同一次序。
    """
    if actual is None:
        return False, "本次没有产生结果集，无法比对"
    if expected is None:
        return False, "样本没有可比的预期结果"

    actual_rows = list(actual.get("rows") or [])
    actual_columns = [str(name) for name in (actual.get("columns") or [])]
    expected_columns = [str(name) for name in (expected.get("columns") or [])]
    expected_rows = list(expected.get("rows") or [])

    aligned_by_name = False
    if expected_columns:
        if sorted(actual_columns) == sorted(expected_columns):
            order = [actual_columns.index(name) for name in expected_columns]
            actual_rows = [[row[index] for index in order] for row in actual_rows]
            aligned_by_name = True
        elif len(actual_columns) != len(expected_columns):
            return False, (
                f"结果列数不一致：实际 {len(actual_columns)} 列 {actual_columns or '无'}，"
                f"预期 {len(expected_columns)} 列 {expected_columns}"
            )

    # Counter 而不是排序后逐个比：行里混着 None 与数字，排序在类型不可比时直接抛错，
    # 而 Counter 只要求元素可哈希——元组正好满足，且天然就是"多重集相等"的语义
    left = Counter(_normalize_row(row) for row in actual_rows)
    right = Counter(_normalize_row(row) for row in expected_rows)
    if left == right:
        note = "按列名对齐" if aligned_by_name else "按列位置比对"
        return True, f"结果集一致（{len(actual_rows)} 行，{note}）"

    reason = f"结果集不一致：实际 {len(actual_rows)} 行，预期 {len(expected_rows)} 行"
    if expected_columns and not aligned_by_name:
        # 退化比对这件事要说出来：不然看板上的人会以为"列名不一样所以判错"
        reason += (
            f"；两边列名不同（实际 {actual_columns}，预期 {expected_columns}），"
            "已改按列位置比对"
        )
    return False, reason


async def judge(
    case: EvalCase,
    *,
    actual: dict | None,
    actual_sql: str | None,
    dw: DwRepository,
) -> tuple[bool, str]:
    """判定一条样本答对没有，并给出一句人话的说明。

    说明是给看板上的人看的：只写"错了"帮不上忙，得让他知道是**结果对不上**
    还是**压根没跑出来**，这两种情况分别指向模型与数仓/权限两条排查路径。
    """
    expected = case.expected_result
    if isinstance(expected, dict) and expected.get("rows") is not None:
        ok, why = rows_equal(actual, expected)
        return ok, f"与样本预期结果比对：{why}"

    if case.expected_sql:
        try:
            expected_query = await dw.execute(assert_readonly(case.expected_sql))
        except AppException as exc:
            # 标准 SQL 自己跑不动（数仓改过表、样本写错了），结果集比对这条路的
            # 前提就没了；退回到文本比对，并且**把原因说出来**——
            # 否则这一次的判分依据悄悄换了，准确率的含义也跟着变了
            return (
                normalize_sql(actual_sql) == normalize_sql(case.expected_sql),
                f"标准 SQL 无法执行（{exc.message}），退化为 SQL 文本比对",
            )
        ok, why = rows_equal(
            actual, {"columns": expected_query.columns, "rows": expected_query.rows}
        )
        return ok, f"与标准 SQL 的实际结果比对：{why}"

    return False, "样本既没有预期结果也没有标准 SQL，无法判定"


# ==========================================================================
# 执行
# ==========================================================================


async def execute_run(
    run_id: str,
    *,
    user_id: str,
    role: str,
    case_ids: Sequence[str] | None = None,
) -> None:
    """跑完一次评测并把结果写回 ``eval_run``。

    **自己开库会话**：它作为后台任务运行，脱离了请求上下文，请求级那个会话
    在响应发出去时就关掉了。跑在请求的会话上，写回时会撞上"会话已关闭"，
    而症状是"评测跑完了、记录还是 running"。

    失败不抛出（后台任务的异常没人接，只会变成一条无人看的堆栈）：把
    ``status`` 置为 failed、把已经跑出来的明细与原因留下，客户端轮询时看得到。
    """
    async with session_scope() as session:
        runs = EvalRunRepository(session)
        run = await runs.get(run_id)
        if run is None:
            logger.warning("评测运行记录 %s 已不存在，放弃这次运行", run_id)
            return

        outcomes: list[EvalCaseOutcome] = []
        try:
            cases = await _load_cases(session, case_ids)
            known_tables = {row.table_name for row in await MetaRepository(session).tables()}
            for case in cases:
                outcomes.append(
                    await _run_case(case, user_id=user_id, role=role, known_tables=known_tables)
                )

            metrics = summarize(outcomes)
            await runs.finish(
                run,
                total_cases=len(outcomes),
                accuracy=metrics.accuracy,
                recall_hit_rate=metrics.recall_hit_rate,
                execute_success_rate=metrics.execute_success_rate,
                avg_duration_ms=metrics.avg_duration_ms,
                detail=_detail(outcomes),
            )
        except Exception as exc:  # noqa: BLE001 —— 后台任务的异常必须自己收口
            logger.exception("评测运行 %s 失败", run_id)
            # 回滚后再取一次：失败可能就发生在 flush 上，此时会话里的对象已经不可用，
            # 直接拿着旧对象改属性会在 commit 时再炸一次，连"失败"都记不下来
            await session.rollback()
            run = await runs.get(run_id)
            if run is not None:
                run.detail = _detail(outcomes)
                await runs.fail(run, message=f"评测运行失败：{exc}"[:500])
        await session.commit()


async def _load_cases(session, case_ids: Sequence[str] | None) -> Sequence[EvalCase]:
    repo = EvalCaseRepository(session)
    return await repo.by_ids(case_ids) if case_ids else await repo.all()


async def _run_case(
    case: EvalCase, *, user_id: str, role: str, known_tables: set[str]
) -> EvalCaseOutcome:
    """跑一条样本并判定。

    ``states`` 只用来取最后一份状态快照——图的终态里有 ``table_infos``
    （这次生成 SQL 时看得到哪些表），那是召回命中率唯一的事实来源，
    而它不在事件流里（见 ``QueryService.stream`` 的 ``on_state``）。
    """
    service = QueryService(session=None)
    turn = await service.prepare(user_id, None)

    states: list[QueryState] = []
    sql: str | None = None
    result: dict | None = None
    failure: str | None = None
    started = time.perf_counter()

    async for event in service.stream(
        case.query_text,
        user_id=user_id,
        role=role,
        turn=turn,
        on_state=states.append,
    ):
        kind = event.get("type")
        if kind == SQL:
            sql = event.get("sql")
        elif kind == RESULT:
            data = event.get("data")
            result = data if isinstance(data, dict) else None
        elif kind == ERROR:
            failure = event.get("message")

    duration_ms = int((time.perf_counter() - started) * 1000)
    final: QueryState = states[-1] if states else {}

    correct, reason = await judge(
        case, actual=result, actual_sql=sql, dw=DwRepository.default()
    )
    if failure:
        # 失败原因要留在明细里：只写"没答对"，看板上分不出是模型答歪了
        # 还是这条链路本身没跑通
        reason = f"{reason}；执行失败：{failure}"

    return EvalCaseOutcome(
        case_id=case.id,
        query_text=case.query_text,
        generated_sql=sql,
        correct=correct,
        recall_hit=recall_hit(case, final, known_tables),
        executed=result is not None,
        duration_ms=duration_ms,
        reason=reason,
    )


def recall_hit(case: EvalCase, final: QueryState, known_tables: set[str]) -> bool | None:
    """标准 SQL 用到的表，是否都出现在"生成 SQL 时看得到的那份表清单"里。

    期望的表取自 ``expected_sql`` 里出现的、**且确实存在于知识库**的表名：
    CTE 名、别名、写错的表名都不算数（同 ``sql_security.referenced_tables`` 的处置）。
    取不到任何已知表时返回 None——判不了，而不是判错。
    """
    if not case.expected_sql:
        return None
    expected = referenced_tables(case.expected_sql) & known_tables
    if not expected:
        return None
    visible = {table.name for table in final.get("table_infos") or []}
    return expected <= visible


def _detail(outcomes: Sequence[EvalCaseOutcome]) -> dict:
    """逐条明细的落库形态。

    存成对象而不是裸数组：``detail`` 是 ``JSON`` 列，将来要加"本次运行的配置/参数"
    这类字段时，对象能就地扩，数组只能整体改形——而已经落库的旧记录不会跟着变。
    """
    return {"cases": [outcome.model_dump() for outcome in outcomes]}


def to_detail(run) -> EvalRunDetail:
    """``eval_run`` 行 → 详情响应。

    ``detail`` 里的 ``cases`` 走 ``EvalCaseOutcome`` 校验：写的和读的是同一套规则，
    但读的一侧更宽松——旧记录里多出来的字段不该让详情打不开。
    """
    detail = run.detail or {}
    outcomes: list[EvalCaseOutcome] = []
    for item in detail.get("cases") or []:
        try:
            outcomes.append(EvalCaseOutcome.model_validate(item))
        except Exception:  # noqa: BLE001 —— 一条坏明细不该毁掉整份详情
            logger.warning("评测明细中有一条无法解析，已跳过：%s", item, exc_info=True)
    return EvalRunDetail(
        **{
            key: getattr(run, key)
            for key in (
                "id",
                "name",
                "total_cases",
                "accuracy",
                "recall_hit_rate",
                "execute_success_rate",
                "avg_duration_ms",
                "status",
                "created_at",
            )
        },
        outcomes=outcomes,
        error=detail.get("error"),
    )
