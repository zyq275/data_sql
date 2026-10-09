"""Redis checkpointer（ADR-006）：只用普通 Redis 命令持久化图状态。

**为什么不用 ``langgraph-checkpoint-redis``**：它建索引时执行 ``FT.INFO``、
写状态时执行 ``JSON.SET``，即硬依赖 RediSearch 与 RedisJSON 两个模块。
Redis 8 把这两个模块并进了内核，但** Windows 没有带模块的构建**——
redis-windows 各版本只编核心，官方也不发 Windows 二进制；Memurai 免费版是否含
模块无公开定论，且它每 10 天需重启一次，拿它当测试依赖只会让验收变得不稳定。
实测结论（2026-10-09）：``AsyncRedisSaver(...).asetup()`` 在本机 Redis 8.10.2 上
抛 ``RedisSearchError: unknown command 'FT.INFO'``。

**改为只依赖普通命令之后，反而更可移植**：``SET``/``GET``/``HSET``/``ZADD``
在任何版本的 Redis 上都在——包括 TDD 14.1 编排栈里那个真正的 Redis Stack。
换句话说，生产环境不会因为少了模块而换一条代码路径。

**语义照抄 ``InMemorySaver``**：本文件不发明存储语义，只把 ``InMemorySaver``
的三个内存字典（``storage`` / ``writes`` / ``blobs``）搬到 Redis 的键上，
键的划分与去重规则逐一对应。checkpointer 一旦语义出错，症状是"某几轮对话状态
莫名其妙不对"，而不是报错——这类错误在自研实现里极难定位，所以与其重新设计，
不如与参考实现保持一致，再用测试把"重启后能续上"钉死。

**只实现异步方法**：本应用全程 ``astream``，同步方法会阻塞事件循环，
因此显式抛 ``NotImplementedError`` 而不是给出一个"能跑但会拖慢服务"的实现。
基类的 ``aget_delta_channel_history`` 自带异步实现（只依赖 ``aget_tuple``），
所以异步侧只需要下面这五个方法。

键布局（``{p}`` = 前缀，默认 ``checkpoint``）::

    {p}:threads                       SET    全部线程，供 alist 无参遍历
    {p}:{tid}:namespaces              SET    该线程出现过的 checkpoint_ns
    {p}:{tid}:{ns}:idx                ZSET   该命名空间的 checkpoint_id（成员序即新旧序）
    {p}:{tid}:{ns}:c:{cid}            STRING (序列化后的 checkpoint, metadata, 父 id)
    {p}:{tid}:{ns}:w:{cid}            LIST   待写项，顺序即写入顺序
    {p}:{tid}:{ns}:blobs              HASH   字段 "{channel}\\x1f{version}" → 通道值
"""

from __future__ import annotations

import logging
import random
from collections.abc import AsyncIterator, Sequence
from typing import Any

from langgraph.checkpoint.base import (
    WRITES_IDX_MAP,
    BaseCheckpointSaver,
    ChannelVersions,
    Checkpoint,
    CheckpointMetadata,
    CheckpointTuple,
    RunnableConfig,
    SerializerProtocol,
    get_checkpoint_id,
    get_checkpoint_metadata,
)
from redis.asyncio import Redis

logger = logging.getLogger(__name__)

DEFAULT_PREFIX = "checkpoint"

# 字段分隔符。channel / version / task_id 里都不会出现 U+001F（单元分隔符），
# 而换行、冒号这些"看起来更自然"的分隔符都可能真的出现在这些值里。
FIELD_SEP = "\x1f"

# 序列化后的 (编解码名, 载荷) 之间的分隔。编解码名只可能是 msgpack / json 一类的
# 短标识符，绝不会含换行，所以只按第一个换行切开即可，载荷里的换行不受影响。
CODEC_SEP = b"\n"


class RedisCheckpointer(BaseCheckpointSaver[str]):
    """把 LangGraph 的检查点存进 Redis 的 ``BaseCheckpointSaver`` 实现。"""

    def __init__(
        self,
        client: Redis,
        *,
        serde: SerializerProtocol | None = None,
        prefix: str = DEFAULT_PREFIX,
    ) -> None:
        super().__init__(serde=serde)
        self._redis = client
        self._prefix = prefix

    # ------------------------------------------------------------------
    # 键与序列化
    # ------------------------------------------------------------------

    def _key(self, *parts: str) -> str:
        return ":".join((self._prefix, *parts))

    def _index_key(self, thread_id: str, ns: str) -> str:
        return self._key(thread_id, ns, "idx")

    def _checkpoint_key(self, thread_id: str, ns: str, checkpoint_id: str) -> str:
        return self._key(thread_id, ns, "c", checkpoint_id)

    def _writes_key(self, thread_id: str, ns: str, checkpoint_id: str) -> str:
        return self._key(thread_id, ns, "w", checkpoint_id)

    def _blobs_key(self, thread_id: str, ns: str) -> str:
        return self._key(thread_id, ns, "blobs")

    def _threads_key(self) -> str:
        return self._key("threads")

    def _namespaces_key(self, thread_id: str) -> str:
        return self._key(thread_id, "namespaces")

    def _pack(self, value: Any) -> bytes:
        """序列化任意值：先按 serde 编成 (编解码名, 载荷)，再把两者合成一段字节。

        不是直接 ``ormsgpack.packb``：serde 是本 checkpointer 与应用共享的同一套
        序列化（含 ``with_allowlist`` 登记过的应用类型），绕开它就等于用两套规则
        读写同一份数据。
        """
        codec, payload = self.serde.dumps_typed(value)
        return codec.encode() + CODEC_SEP + payload

    def _unpack(self, raw: bytes) -> Any:
        codec, _, payload = raw.partition(CODEC_SEP)
        return self.serde.loads_typed((codec.decode(), payload))

    @staticmethod
    def _text(value: bytes | str) -> str:
        """把 Redis 返回的键名/成员还原成文本。

        客户端是二进制安全的（见 ``clients.redis``），凡是要当字符串比较或拼接的
        返回值都得在这里过一道；而检查点载荷**不能**过这一道，它是二进制。
        """
        return value.decode() if isinstance(value, bytes) else value

    # ------------------------------------------------------------------
    # 读
    # ------------------------------------------------------------------

    async def _latest_checkpoint_id(self, thread_id: str, ns: str) -> str | None:
        """该命名空间里最新（字典序最大）的 checkpoint_id。

        用字典序而不是写入时间：与 ``InMemorySaver`` 的 ``max(keys())`` 一致，
        而 langgraph 生成的 id 本身就是按时间递增、可直接比较的字符串。
        """
        ids = await self._redis.zrevrange(self._index_key(thread_id, ns), 0, 0)
        return self._text(ids[0]) if ids else None

    async def _load_blobs(
        self, thread_id: str, ns: str, versions: ChannelVersions
    ) -> dict[str, Any]:
        """按通道版本取回通道值。

        ``("empty", b"")`` 表示"这一版该通道没有值"（例如某分支没跑到），
        要原样跳过而不是去反序列化一段空载荷。
        """
        if not versions:
            return {}
        fields = [f"{channel}{FIELD_SEP}{version}" for channel, version in versions.items()]
        raw_values = await self._redis.hmget(self._blobs_key(thread_id, ns), fields)

        result: dict[str, Any] = {}
        for (channel, _version), raw in zip(versions.items(), raw_values):
            if raw is None:
                continue
            stored = self._unpack(raw)
            if stored[0] == "empty":
                continue
            result[channel] = self.serde.loads_typed(stored)
        return result

    async def _load_writes(
        self, thread_id: str, ns: str, checkpoint_id: str
    ) -> list[tuple[str, str, tuple[str, bytes], str]]:
        """取回某检查点的待写项，保持写入顺序。

        顺序有意义：``aget_delta_channel_history`` 会反向遍历这些写项来重建某个
        通道的历史。用 LIST 而不是 HASH 正是为了保住顺序——HASH 的遍历顺序是
        未定义的，换成 HASH 会让"同一批写项"在不同时刻重建出不同的状态，
        而且这种不确定性只在并发/重启后才暴露。
        """
        raws = await self._redis.lrange(self._writes_key(thread_id, ns, checkpoint_id), 0, -1)
        return [self._unpack(raw) for raw in raws]

    async def aget_tuple(self, config: RunnableConfig) -> CheckpointTuple | None:
        thread_id: str = config["configurable"]["thread_id"]
        ns: str = config["configurable"].get("checkpoint_ns", "")

        checkpoint_id = get_checkpoint_id(config)
        if checkpoint_id is None:
            checkpoint_id = await self._latest_checkpoint_id(thread_id, ns)
            if checkpoint_id is None:
                # 线程还没有任何检查点：这是首轮，不是错误
                return None

        raw = await self._redis.get(self._checkpoint_key(thread_id, ns, checkpoint_id))
        if raw is None:
            return None

        checkpoint_pair, metadata_pair, parent_id = self._unpack(raw)
        checkpoint: Checkpoint = self.serde.loads_typed(checkpoint_pair)
        checkpoint["channel_values"] = await self._load_blobs(
            thread_id, ns, checkpoint["channel_versions"]
        )

        writes = await self._load_writes(thread_id, ns, checkpoint_id)
        return CheckpointTuple(
            config={
                "configurable": {
                    "thread_id": thread_id,
                    "checkpoint_ns": ns,
                    "checkpoint_id": checkpoint_id,
                }
            },
            checkpoint=checkpoint,
            metadata=self.serde.loads_typed(metadata_pair),
            pending_writes=[
                # 写项落盘时是 (task_id, channel, 载荷, task_path, 序号) 五元组：
                # 序号只在去重时用得上，对外仍按 langgraph 的 (task_id, channel, value) 三元组给出
                (task_id, channel, self.serde.loads_typed(payload))
                for task_id, channel, payload, _path, _idx in writes
            ],
            parent_config=(
                {
                    "configurable": {
                        "thread_id": thread_id,
                        "checkpoint_ns": ns,
                        "checkpoint_id": parent_id,
                    }
                }
                if parent_id
                else None
            ),
        )

    async def alist(
        self,
        config: RunnableConfig | None,
        *,
        filter: dict[str, Any] | None = None,
        before: RunnableConfig | None = None,
        limit: int | None = None,
    ) -> AsyncIterator[CheckpointTuple]:
        """按线程倒序列出检查点。

        不需要 RediSearch：线程清单由一个 SET 维护，每个线程的命名空间与检查点
        序号各由一个 SET / ZSET 维护，因此"列出"退化成几次集合读取，
        而不是一次全库扫描。
        """
        if config is not None:
            thread_ids: list[str] = [config["configurable"]["thread_id"]]
            only_ns: str | None = config["configurable"].get("checkpoint_ns")
            only_id = get_checkpoint_id(config)
        else:
            thread_ids = sorted(
                self._text(t) for t in await self._redis.smembers(self._threads_key())
            )
            only_ns = None
            only_id = None

        before_id = get_checkpoint_id(before) if before else None
        remaining = limit

        for thread_id in thread_ids:
            if only_ns is not None:
                namespaces = [only_ns]
            else:
                namespaces = sorted(
                    self._text(t)
                    for t in await self._redis.smembers(self._namespaces_key(thread_id))
                )

            for ns in namespaces:
                # zrevrange 给出由新到旧的顺序，与 InMemorySaver 的倒序一致
                ids = [
                    self._text(i)
                    for i in await self._redis.zrevrange(self._index_key(thread_id, ns), 0, -1)
                ]
                for checkpoint_id in ids:
                    if only_id is not None and checkpoint_id != only_id:
                        continue
                    # ``before`` 是"严格早于"：id 更大（更新）的一律跳过
                    if before_id is not None and checkpoint_id >= before_id:
                        continue

                    tuple_ = await self.aget_tuple(
                        {
                            "configurable": {
                                "thread_id": thread_id,
                                "checkpoint_ns": ns,
                                "checkpoint_id": checkpoint_id,
                            }
                        }
                    )
                    if tuple_ is None:
                        continue

                    if filter and not all(
                        metadata_value == tuple_.metadata.get(metadata_key)
                        for metadata_key, metadata_value in filter.items()
                    ):
                        continue

                    if remaining is not None:
                        if remaining <= 0:
                            return
                        remaining -= 1

                    yield tuple_

    # ------------------------------------------------------------------
    # 写
    # ------------------------------------------------------------------

    async def aput(
        self,
        config: RunnableConfig,
        checkpoint: Checkpoint,
        metadata: CheckpointMetadata,
        new_versions: ChannelVersions,
    ) -> RunnableConfig:
        thread_id: str = config["configurable"]["thread_id"]
        ns: str = config["configurable"]["checkpoint_ns"]
        checkpoint_id: str = checkpoint["id"]
        parent_id: str | None = config["configurable"].get("checkpoint_id")

        # channel_values 单独按通道存：同一通道在多个检查点里往往一字不变，
        # 每个检查点都整份复制一遍会让 Redis 的占用随对话轮数平方增长。
        body = checkpoint.copy()
        values: dict[str, Any] = body.pop("channel_values")  # type: ignore[misc]

        pipe = self._redis.pipeline(transaction=True)
        if new_versions:
            pipe.hset(
                self._blobs_key(thread_id, ns),
                mapping={
                    f"{channel}{FIELD_SEP}{version}": self._pack(
                        self.serde.dumps_typed(values[channel])
                        if channel in values
                        else ("empty", b"")
                    )
                    for channel, version in new_versions.items()
                },
            )
        pipe.set(
            self._checkpoint_key(thread_id, ns, checkpoint_id),
            self._pack(
                (
                    self.serde.dumps_typed(body),
                    self.serde.dumps_typed(get_checkpoint_metadata(config, metadata)),
                    parent_id,
                )
            ),
        )
        pipe.zadd(self._index_key(thread_id, ns), {checkpoint_id: 0})
        pipe.sadd(self._namespaces_key(thread_id), ns)
        pipe.sadd(self._threads_key(), thread_id)
        await pipe.execute()

        return {
            "configurable": {
                "thread_id": thread_id,
                "checkpoint_ns": ns,
                "checkpoint_id": checkpoint_id,
            }
        }

    async def aput_writes(
        self,
        config: RunnableConfig,
        writes: Sequence[tuple[str, Any]],
        task_id: str,
        task_path: str = "",
    ) -> None:
        thread_id: str = config["configurable"]["thread_id"]
        ns: str = config["configurable"].get("checkpoint_ns", "")
        checkpoint_id: str = config["configurable"]["checkpoint_id"]
        key = self._writes_key(thread_id, ns, checkpoint_id)

        # 只有少量写项，整体读回来做去重比再维护一个索引集合更简单，
        # 也少一处可能与 LIST 不一致的状态。
        #
        # 转成 tuple 再入集合：解出来的 (task_id, channel) 是 **list**
        # （msgpack 的数组一律解成 list），而 list 不可哈希，直接塞进 set 会抛
        # ``TypeError: unhashable type: 'list'``。
        existing = {tuple(self._unpack(raw)[:2]) for raw in await self._redis.lrange(key, 0, -1)}

        entries: list[bytes] = []
        for idx, (channel, value) in enumerate(writes):
            # 与 InMemorySaver 同一条规则：``WRITES_IDX_MAP`` 之外（索引非负）
            # 的写项若已存在就跳过，避免同一任务重跑时把写项写成两份。
            write_idx = WRITES_IDX_MAP.get(channel, idx)
            if write_idx >= 0 and (task_id, channel) in existing:
                continue
            entries.append(
                self._pack(
                    (task_id, channel, self.serde.dumps_typed(value), task_path, write_idx)
                )
            )

        if entries:
            await self._redis.rpush(key, *entries)

    async def adelete_thread(self, thread_id: str) -> None:
        """删掉一个线程的全部痕迹（会话删除时调用）。

        用 SCAN 而不是 KEYS：KEYS 会一次性遍历整个键空间并阻塞 Redis，
        在共享实例上等于让别人的请求跟着一起卡住。
        """
        pattern = f"{self._prefix}:{thread_id}:*"
        batch: list[str] = []
        async for key in self._redis.scan_iter(match=pattern, count=500):
            batch.append(key)
            if len(batch) >= 500:
                await self._redis.delete(*batch)
                batch.clear()
        if batch:
            await self._redis.delete(*batch)
        await self._redis.srem(self._threads_key(), thread_id)

    # ------------------------------------------------------------------
    # 版本号
    # ------------------------------------------------------------------

    def get_next_version(self, current: str | None, channel: None) -> str:
        """通道版本号：高位是递增计数，低位是随机数。

        低位为什么带随机：同一轮里两个并行分支可能写同一个通道，计数相同而随机数
        不同，两版都能落盘、不会互相覆盖，融合时再按通道自己的规则合并。
        照抄 ``InMemorySaver``，不另行设计。
        """
        if current is None:
            current_v = 0
        elif isinstance(current, int):
            current_v = current
        else:
            current_v = int(current.split(".")[0])
        return f"{current_v + 1:032}.{random.random():016}"

    # ------------------------------------------------------------------
    # 同步接口
    # ------------------------------------------------------------------

    def _sync_unsupported(self) -> None:
        raise NotImplementedError(
            "RedisCheckpointer 只实现异步接口：本应用全程走 astream，"
            "同步方法会阻塞事件循环。需要同步调用的场景请改用 InMemorySaver。"
        )

    def get_tuple(self, config: RunnableConfig) -> CheckpointTuple | None:
        self._sync_unsupported()

    def list(self, config: RunnableConfig | None, **kwargs: Any) -> Any:  # noqa: A003
        self._sync_unsupported()

    def put(self, config: RunnableConfig, checkpoint: Checkpoint, metadata: CheckpointMetadata, new_versions: ChannelVersions) -> RunnableConfig:
        self._sync_unsupported()

    def put_writes(self, config: RunnableConfig, writes: Sequence[tuple[str, Any]], task_id: str, task_path: str = "") -> None:
        self._sync_unsupported()

    def delete_thread(self, thread_id: str) -> None:
        self._sync_unsupported()
