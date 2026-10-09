"""Redis 连接（ADR-006：会话 checkpointer）。

**为什么 ``decode_responses=False``**：会话 checkpointer 读写的是序列化后的图状态，
是**二进制**。若让客户端自动解码，取回一个含非 UTF-8 字节的检查点会直接抛
``UnicodeDecodeError``——而且只在"状态里恰好出现某个字节序列"时才抛，
属于最难复现的那类故障。二进制安全是唯一不会出错的选择，少数确实要文本的地方
（如 ``ping``）在调用点显式解码。

**惰性创建，与其它 client 一致**：连不上的依赖不该拖住启动。Redis 不可用时的
降级策略在 ``agent.checkpoint``，不在这一层——这一层只负责"给我一个客户端"，
返回的客户端能不能用由调用方判断。
"""

from __future__ import annotations

from urllib.parse import quote_plus

from redis.asyncio import Redis

from app.core.config import get_settings

_client: Redis | None = None

# 探活与建连的超时都压得很短：会话 checkpointer 位于每次问答的入口，
# 一个"连不上但也不报错、只是慢慢等"的 Redis 会把每次提问都拖慢几秒。
# 快速失败才能快速降级。
CONNECT_TIMEOUT_SECONDS = 2.0
SOCKET_TIMEOUT_SECONDS = 2.0


def redis_url() -> str:
    """连接串。口令做 URL 转义——与 ``DatabaseSettings.url`` 同一条理由：
    口令里出现 ``@`` / ``:`` 时不转义会把主机名解析到错误的地方。"""
    settings = get_settings().redis
    auth = f":{quote_plus(settings.password)}@" if settings.password else ""
    return f"redis://{auth}{settings.host}:{settings.port}/{settings.db}"


def safe_url() -> str:
    """日志用连接串，不含口令（NFR-SEC-003）。"""
    settings = get_settings().redis
    return f"redis://{settings.host}:{settings.port}/{settings.db}"


def get_redis() -> Redis:
    """应用侧客户端（首次调用时创建）。"""
    global _client
    if _client is None:
        _client = Redis.from_url(
            redis_url(),
            decode_responses=False,
            socket_connect_timeout=CONNECT_TIMEOUT_SECONDS,
            socket_timeout=SOCKET_TIMEOUT_SECONDS,
            health_check_interval=30,
        )
    return _client


async def dispose_redis() -> None:
    """关闭连接池（应用停机时调用）。"""
    global _client
    if _client is not None:
        await _client.aclose()
    _client = None


async def ping() -> str:
    """探活用：真发一次 PING，而不是只看端口在不在。

    "端口活着、服务半死"是 Redis 上真实存在的状态（加载中、持久化阻塞、
    内存满了被设为只读）。只探端口会把这类故障判成健康。
    """
    pong = await get_redis().ping()
    # 客户端是二进制安全的，PING 回来的是 b"PONG"；两种形态都接住，
    # 免得将来有人改了 decode_responses 就让探针自己崩掉
    return pong.decode() if isinstance(pong, bytes) else str(pong)
