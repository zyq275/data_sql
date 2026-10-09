"""阶段 7：自研 Redis checkpointer（ADR-006）。

用一个**与本项目无关的最小图**（两个节点、一个计数通道）来测：这里要验的是
checkpointer 本身的语义——写进去能读回来、父链接得上、重启（换一个 saver 实例）
后状态还在、删线程能删干净。掺进真实的问答图只会让失败原因变得含糊。

需要真 Redis（``requires_redis``）。没有它时会话降级到内存（agent.checkpoint），
整套用例跳过而不是失败——Redis 是可选的，见 conftest 的说明。
"""

from __future__ import annotations

import logging
import uuid
from dataclasses import dataclass
from typing import TypedDict

import pytest
from langgraph.checkpoint.serde.jsonplus import JsonPlusSerializer
from langgraph.graph import END, START, StateGraph

from app.agent.checkpoint import STATE_TYPES
from app.agent.knowledge import ColumnSpec, MetricSpec, TableSpec
from app.agent.redis_checkpointer import RedisCheckpointer
from app.clients.redis import get_redis
from app.retrievers.candidate import Candidate

pytestmark = pytest.mark.requires_redis


class Counter(TypedDict, total=False):
    n: int
    note: str


async def _increment(state: Counter) -> dict:
    return {"n": state.get("n", 0) + 1}


async def _annotate(state: Counter) -> dict:
    return {"note": f"n={state.get('n')}"}


def build(saver: RedisCheckpointer):
    builder = StateGraph(Counter)
    builder.add_node("increment", _increment)
    builder.add_node("annotate", _annotate)
    builder.add_edge(START, "increment")
    builder.add_edge("increment", "annotate")
    builder.add_edge("annotate", END)
    return builder.compile(checkpointer=saver)


def saver(prefix: str) -> RedisCheckpointer:
    """每次调用都造一个**新的** saver 实例。

    这正是"进程重启"的等价物：新实例与新连接，唯一共享的是 Redis 里的数据。
    """
    return RedisCheckpointer(get_redis(), prefix=prefix)


def config(thread_id: str) -> dict:
    return {"configurable": {"thread_id": thread_id}}


@pytest.fixture
async def prefix() -> str:
    """每个用例一个独立的键前缀，用完扫干净。

    用独立前缀而不是共用 ``checkpoint``：真实运行时 Redis 里可能正躺着别的数据，
    而清理必须是"删掉本次自己写的"——按前缀扫就能精确定位，也不会误删。
    """
    value = f"test-cp-{uuid.uuid4().hex[:8]}"
    yield value
    client = get_redis()
    keys = [key async for key in client.scan_iter(match=f"{value}:*", count=500)]
    if keys:
        await client.delete(*keys)


# ==========================================================================
# 往返
# ==========================================================================


async def test_state_survives_a_restart(prefix: str) -> None:
    """会话状态持久化，换一个进程（新实例）后仍能读回来（FR-CONV-002）。

    这是本文件最要紧的一条：自研 checkpointer 一旦语义有偏差，症状是
    "某几轮对话状态莫名其妙不对"，而不是报错。
    """
    thread_id = "t-restart"
    graph = build(saver(prefix))
    await graph.ainvoke({"n": 41}, config=config(thread_id))

    # —— 换一个 saver、换一张图，模拟进程重启 ——
    resumed = build(saver(prefix))
    snapshot = await resumed.aget_state(config(thread_id))

    assert snapshot.values["n"] == 42
    assert snapshot.values["note"] == "n=42"


async def test_unknown_thread_reads_back_empty_instead_of_raising(prefix: str) -> None:
    """首轮没有检查点是正常状态，不是错误。"""
    graph = build(saver(prefix))

    snapshot = await graph.aget_state(config("t-never-written"))

    assert snapshot.values == {}


async def test_second_turn_continues_from_the_state_stored_by_the_first(prefix: str) -> None:
    """第二轮只带本轮的新输入，上一轮的状态由 checkpointer 读回来。

    这正是多轮会话在存储层的语义：第二个问题的请求体里只有问题本身，
    "上文"必须来自持久化的状态。若 checkpointer 只写不读，这里的 n 会从 0 重新开始。
    """
    thread_id = "t-turns"
    graph = build(saver(prefix))

    first = await graph.ainvoke({"n": 41}, config=config(thread_id))
    assert first["n"] == 42

    second = await graph.ainvoke({"note": "第二轮"}, config=config(thread_id))

    assert second["n"] == 43, "第二轮必须从上一轮的状态继续，而不是从零开始"


# ==========================================================================
# 列出与父链
# ==========================================================================


async def test_list_returns_checkpoints_newest_first(prefix: str) -> None:
    thread_id = "t-list"
    graph = build(saver(prefix))
    await graph.ainvoke({"n": 0}, config=config(thread_id))

    ids = [t.config["configurable"]["checkpoint_id"] async for t in graph.checkpointer.alist(config(thread_id))]

    assert len(ids) >= 3, "输入 + 两个节点，至少三个检查点"
    assert ids == sorted(ids, reverse=True), "必须由新到旧"


async def test_parent_chain_links_each_checkpoint_to_its_predecessor(prefix: str) -> None:
    """父链断了的话，回放（``alist`` 逐条重放）会从中间断掉。"""
    thread_id = "t-parent"
    graph = build(saver(prefix))
    await graph.ainvoke({"n": 0}, config=config(thread_id))

    tuples = [t async for t in graph.checkpointer.alist(config(thread_id))]
    newest, *rest = tuples

    assert newest.parent_config is not None
    assert newest.parent_config["configurable"]["checkpoint_id"] == rest[0].config["configurable"]["checkpoint_id"]
    # 最早那一条没有父
    assert tuples[-1].parent_config is None


async def test_list_can_be_limited(prefix: str) -> None:
    thread_id = "t-limit"
    graph = build(saver(prefix))
    await graph.ainvoke({"n": 0}, config=config(thread_id))

    tuples = [t async for t in graph.checkpointer.alist(config(thread_id), limit=2)]

    assert len(tuples) == 2


# ==========================================================================
# 删除
# ==========================================================================


async def test_delete_thread_removes_every_trace(prefix: str) -> None:
    """删会话时图状态必须一起清掉，否则 Redis 会随会话数无限增长。"""
    thread_id = "t-delete"
    graph = build(saver(prefix))
    await graph.ainvoke({"n": 0}, config=config(thread_id))

    await graph.checkpointer.adelete_thread(thread_id)

    assert [t async for t in graph.checkpointer.alist(config(thread_id))] == []
    # 比对 values 而不是整个 StateSnapshot：快照里的 config 带着请求的
    # thread_id，拿它跟另一个线程比必然不等，那不是"没删干净"
    assert (await graph.aget_state(config(thread_id))).values == {}


async def test_delete_thread_only_touches_that_thread(prefix: str) -> None:
    """删一个会话不能顺手动到别人。"""
    graph = build(saver(prefix))
    await graph.ainvoke({"n": 0}, config=config("t-keep"))
    await graph.ainvoke({"n": 0}, config=config("t-drop"))

    await graph.checkpointer.adelete_thread("t-drop")

    assert (await graph.aget_state(config("t-keep"))).values["n"] == 1


# ==========================================================================
# 去重与版本号
# ==========================================================================


async def test_rewriting_the_same_task_does_not_duplicate_writes(prefix: str) -> None:
    """同一任务重跑不能把写项写成两份。

    langgraph 在重放/重试时会重发同一批写项，去重规则照抄 ``InMemorySaver``
    （``WRITES_IDX_MAP`` 之外的通道按 (task_id, channel) 去重）。
    """
    thread_id = "t-dedupe"
    graph = build(saver(prefix))
    await graph.ainvoke({"n": 0}, config=config(thread_id))

    checkpoint = await graph.checkpointer.aget_tuple(config(thread_id))
    assert checkpoint is not None
    target = {**config(thread_id), "configurable": {**config(thread_id)["configurable"], "checkpoint_id": checkpoint.config["configurable"]["checkpoint_id"]}}

    await graph.checkpointer.aput_writes(target, [("n", 5)], task_id="task-1")
    await graph.checkpointer.aput_writes(target, [("n", 5)], task_id="task-1")

    reloaded = await graph.checkpointer.aget_tuple(target)
    assert reloaded is not None
    assert [w for w in reloaded.pending_writes if w[0] == "task-1"] == [("task-1", "n", 5)]


def test_next_version_counter_increases_with_each_call() -> None:
    """版本号的前缀是递增计数，所以"更新的一版"永远大于旧版。

    只取第一个 ``.`` 之前那段来比：整串里有两个点（计数 + 小数形式的随机数），
    按整串比较会变成按随机数比较，那正是这里不该依赖的东西。
    """
    checkpointer = RedisCheckpointer(get_redis(), prefix="test-cp-version")

    first = checkpointer.get_next_version(None, None)
    second = checkpointer.get_next_version(first, None)
    third = checkpointer.get_next_version(second, None)

    counters = [int(v.split(".")[0]) for v in (first, second, third)]
    assert counters == sorted(counters)
    assert len(set(counters)) == 3


def test_next_version_low_bits_differ_for_the_same_counter() -> None:
    """同一个计数下多次取值必须得到不同的版本串。

    同一轮里两个并行分支可能写同一个通道，计数相同；若版本串也相同，
    后写的那一版会把先写的覆盖掉，表现为"某个分支的结果莫名其妙丢了"。
    """
    checkpointer = RedisCheckpointer(get_redis(), prefix="test-cp-version")

    versions = [checkpointer.get_next_version("00000000000000000000000000000007", None) for _ in range(3)]

    assert len(set(versions)) == 3


def test_sync_methods_refuse_loudly() -> None:
    """同步接口显式拒绝，而不是给出一个会阻塞事件循环的实现。"""
    checkpointer = RedisCheckpointer(get_redis(), prefix="test-cp-sync")

    with pytest.raises(NotImplementedError):
        checkpointer.get_tuple(config("t"))


# ==========================================================================
# 序列化白名单的登记
# ==========================================================================


@dataclass(slots=True)
class _Unregistered:
    """一个**故意不登记**的类型，用来给下面两条用例当尺子。

    写成 dataclass 是因为 serde 的编码侧只认 dataclass / pydantic 这类
    "有明确结构"的对象（``_msgpack_default``），普通类连**写**都写不进去，
    那样就测不到"读的时候被拒绝"这件事了。
    """

    value: int


def _strict_saver() -> RedisCheckpointer:
    """复现严格模式（``LANGGRAPH_STRICT_MSGPACK=true``）下的 serde 形态。

    必须显式传 ``allowed_msgpack_modules=None``：取默认值时它是 ``True``，
    而 ``with_allowlist`` 在那种情况下**不合并任何东西**（langgraph 源码
    ``JsonPlusSerializer.with_msgpack_allowlist`` 的 ``if base_allowlist is True:
    return self``）。用默认值测，测的是"什么都不拦"，那正是要避免的自欺。
    """
    return RedisCheckpointer(
        get_redis(), serde=JsonPlusSerializer(allowed_msgpack_modules=None)
    ).with_allowlist(STATE_TYPES)


def test_registered_state_types_round_trip_through_a_strict_serializer() -> None:
    """``STATE_TYPES`` 里每个名字都必须能解出自己的类。

    **这条用例不碰 Redis**，它验的是登记表的拼写对不对。宽松模式（默认）下
    ``with_allowlist`` 是空操作（见 ``agent.checkpoint`` 的说明），今天写错一个
    类名没有任何症状——症状要等到"将来某次 langgraph 升级把默认改成阻塞"或
    "我们主动打开严格模式"那天才出现，而且**不报错**（见下一条用例），
    表现为所有会话的候选/实体在状态里变成一堆 dict。
    这种失败模式没法靠"多跑几次"发现，只能靠这条用例。
    """
    strict = _strict_saver()

    samples = [
        Candidate(
            kind="column", id="c-1", name="order_amount", score=0.5, table_name="fact_order"
        ),
        ColumnSpec(
            name="order_amount",
            type="decimal",
            alias="订单金额",
            description=None,
            is_primary_key=False,
            is_foreign_key=False,
            ref_table=None,
            ref_column=None,
        ),
        TableSpec(
            db_name="data_agent_dw",
            name="fact_order",
            alias=None,
            description=None,
            is_dimension=False,
            columns=[],
        ),
        MetricSpec(
            name="GMV",
            alias="成交总额",
            description=None,
            calculation="SUM(order_amount)",
            filter_condition=None,
            time_range=None,
        ),
    ]

    for sample in samples:
        codec, payload = strict.serde.dumps_typed(sample)
        assert strict.serde.loads_typed((codec, payload)) == sample, type(sample).__name__


def test_a_strict_serializer_degrades_a_type_we_forgot_to_register(
    caplog: pytest.LogCaptureFixture,
) -> None:
    """反面对照：严格模式下未登记的类型**不会被还原成对象**。

    这是上一条的标尺。若哪天 ``with_allowlist`` 又变回空操作，上一条会因为
    "什么都放行"而照样变绿，而这一条会红——两条一起，才说明登记表真的在把关。

    **它的失败方式不是抛异常，这点必须写下来**，否则将来有人照着"拒绝"去写
    ``pytest.raises`` 会得到一个永远不成立的断言。langgraph 的 ext 钩子在拒绝
    一个模块时是 ``return tup[2]``——**退回原始构造参数**（源码注释："如果用在
    pydantic state 之类的地方，构造时还会再校验一遍"），只打一条 WARNING。
    所以漏登记的后果是：状态里躺着一个 dict，下游某个 ``candidate.identity()``
    以 AttributeError 的形式在对话中途炸掉，报错点离病根隔了整整一个节点。
    这里断言的正是这个"退化成原始数据且必须留痕"。
    """
    strict = _strict_saver()

    # 用宽松的 serde 写（写侧本来就不查白名单），再用严格的 serde 读
    codec, payload = JsonPlusSerializer().dumps_typed(_Unregistered(1))

    with caplog.at_level(logging.WARNING, logger="langgraph.checkpoint.serde.jsonplus"):
        restored = strict.serde.loads_typed((codec, payload))

    assert not isinstance(restored, _Unregistered), "未登记的类型不该被还原成对象"
    assert restored == {"value": 1}, "拒绝的落法是退回构造参数本身，而不是报错"
    assert "_Unregistered" in caplog.text, "拒绝必须留痕，否则就是静默丢数据"
