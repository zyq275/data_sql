"""会话 checkpointer 的选型与降级（ADR-006）。

ADR-006 要的是"复用状态快照能力，Redis 保证多实例共享与低延迟，重启可恢复"，
所以**正常路径**是 ``RedisCheckpointer``：状态真正落在 Redis 上，进程重启、
多实例共用一个实例都能续上。

**为什么要有降级**：Redis 是本阶段新引入的外部依赖，而"没有 Redis"在开发机上
是常态（本机就一度完全没有）。若把它做成硬依赖，缺了它连"问一句数"都做不到——
而问答链路本身并不需要 Redis 才能工作。降级后的 ``InMemorySaver`` 单进程内
语义完全相同，只是进程一停状态就没了。

**降级不是无痕的，所以要说清楚代价**：会话消息（问题/SQL/结果）落在 MySQL 的
``message`` 表，**与这里无关**，所以"重启后能接着聊"在降级下依然成立——
丢掉的是图状态的精细快照（每个节点后的中间态），它影响的是状态回放与调试，
不影响用户看到的多轮对话。这个区分很重要：它决定了降级可以接受，
而不是"悄悄把功能做残了"。

**选型只在首次使用时做一次，之后不再翻转**：因为 checkpointer 是编译图时
烘进图对象里的，中途换一个就得重新编译图，而"正在跑的请求"与"新编译的图"
会落在两个不同的 state 上——这比"Redis 晚起了就一直用内存"更糟。所以
Redis 中途恢复不会自动切回去，需要重启进程；降级时那条告警会把这句话写在里面。
"""

from __future__ import annotations

import asyncio
import logging

from langgraph.checkpoint.base import BaseCheckpointSaver
from langgraph.checkpoint.memory import InMemorySaver

from app.agent.redis_checkpointer import RedisCheckpointer
from app.clients.redis import get_redis, safe_url
from app.core.config import get_settings

logger = logging.getLogger(__name__)

# 图状态里会出现应用自定义类型（召回候选项、表/指标实体）。langgraph 的 serde
# 对未登记的类型目前只打告警，并明确说未来版本会直接拒绝——与其等升级 langgraph
# 那天会话集体失效，不如现在就把它们登记进来。
#
# **但要说清楚：这份登记在当前配置下是"备好的答案"，并没有生效**。``with_allowlist``
# 最终落到 ``JsonPlusSerializer.with_msgpack_allowlist``，而它在宽松模式（默认，
# 即 ``allowed_msgpack_modules=True``）下**直接原样返回、不合并**（langgraph 源码
# ``checkpoint/serde/jsonplus.py`` 的 ``if base_allowlist is True ... return self``）：
# 宽松模式的含义本就是"全都放行、只对没登记的告警一次"，此时"哪些登记过"不影响行为。
# 它真正起作用是在严格模式（``LANGGRAPH_STRICT_MSGPACK=true``，或将来 langgraph 把
# 默认改成阻塞）——那时这份表就是白名单。但**"被拒"的样子不是抛异常**：serde 的 ext
# 钩子拒绝一个模块时是 ``return tup[2]``，即**退回原始构造参数**（对 dataclass 就是
# 一个 dict），只打一条 WARNING。所以漏登记的后果不是"启动即失败"，而是状态里躺着
# 一个 dict，下游拿它当对象用（``candidate.identity()``）时以 AttributeError 在对话
# 中途炸掉——报错点离病根隔了一个节点。知道这一点才明白这张表值得单独钉住。
#
# 所以：**不改默认模式**（打开严格模式会让任何我们没枚举到的类型都按上述方式降级，
# 那是另一件事，要单独验证过再定），但这张表本身必须是对的——
# 它出错的时刻是"升级那天所有会话一起坏"，那正是这个注释想避免的场景。
# 见 tests/test_redis_checkpointer.py 的两条用例（一条钉登记表本身，一条钉"未登记
# 会被降级成原始数据"，后者保证前者不是靠"什么都放行"变绿的）。
STATE_TYPES: tuple[tuple[str, str], ...] = (
    ("app.retrievers.candidate", "Candidate"),
    ("app.agent.knowledge", "ColumnSpec"),
    ("app.agent.knowledge", "TableSpec"),
    ("app.agent.knowledge", "MetricSpec"),
)

_saver: BaseCheckpointSaver | None = None
_lock = asyncio.Lock()
_warned = False


async def get_checkpointer() -> BaseCheckpointSaver:
    """取进程内共享的 checkpointer（首次调用时定选型）。"""
    global _saver
    if _saver is not None:
        return _saver
    async with _lock:
        # 双重检查：并发首次调用时只有一个真正去建连，其余等它建好直接用
        if _saver is None:
            _saver = await _create()
        return _saver


async def _create() -> BaseCheckpointSaver:
    global _warned
    settings = get_settings().redis

    if settings.enabled:
        try:
            saver = RedisCheckpointer(get_redis()).with_allowlist(STATE_TYPES)
            # 真发一次命令：只建客户端对象是不连的，等到第一次写检查点才发现
            # Redis 不可用，那时已经在一轮问答中间了
            await get_redis().ping()
            logger.info("会话 checkpointer：Redis %s", safe_url())
            return saver
        except Exception as exc:  # noqa: BLE001 —— 任何连接类故障都走同一条降级
            if not _warned:
                _warned = True
                logger.warning(
                    "Redis（%s）不可用：%s。会话状态降级为进程内存储，"
                    "进程重启后图状态不再保留（会话消息与查询历史仍在 MySQL，不受影响）。"
                    "Redis 恢复后需重启本进程才会切回 Redis。",
                    safe_url(),
                    exc,
                )
    elif not _warned:
        _warned = True
        logger.info("redis.enabled=false，会话状态使用进程内存储")

    return InMemorySaver()


def reset_checkpointer() -> None:
    """丢弃当前选型，下次调用重新判定。

    只给测试与显式重连用。应用运行期不要调用：换 checkpointer 意味着换一张图，
    见模块说明。
    """
    global _saver, _warned
    _saver = None
    _warned = False


def current_backend() -> str:
    """当前用的是哪一种（未选型时为 ``unresolved``），供探针与日志展示。"""
    if _saver is None:
        return "unresolved"
    return "redis" if isinstance(_saver, RedisCheckpointer) else "memory"


async def release_thread(thread_id: str) -> None:
    """丢弃某个会话的图状态快照（删除会话时调用）。

    **失败只告警、不抛出**：用户在界面上执行的是"删掉这个会话"，他看得见的东西
    （MySQL 里的会话与消息）此刻已经删掉了。Redis 里残留一份快照属于存储卫生问题——
    会话 id 是服务端生成的 uuid4，不会再有第二个会话复用它，所以这份残留既不会被
    读到、也不会串到别处。为它把整个删除操作报成失败，等于拿内部残留否掉用户
    已经完成的操作，而他重试多少次都会得到同一个结果。

    告警里带上 thread_id：残留是能被运维清理的，前提是知道要清哪一个。
    """
    saver = await get_checkpointer()
    try:
        await saver.adelete_thread(thread_id)
    except Exception as exc:  # noqa: BLE001 —— 见上，任何失败都不该改变删除的结论
        logger.warning("清理会话 %s 的图状态失败（会话本体已删除）：%s", thread_id, exc)
