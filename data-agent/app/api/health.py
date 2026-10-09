"""健康探针（FR-SYS-001、FR-SYS-002）。

``/health`` 是存活探针：进程活着就返回 200，不碰任何外部依赖。
``/health/ready`` 是就绪探针：并发探测各依赖，任一必需依赖不可用即返回 503，
让编排器（TDD 14.1）把流量摘掉，而不是把请求放进来再 500。

探测范围是**存储**与检索链上的外部服务：MySQL（meta / dw）、Qdrant、
Elasticsearch 取值索引、Embedding 服务（ADR-005）。

每个探测有独立超时。探针挂起比探针失败更糟——编排器会一直等不到结论，
既不敢摘流量也不敢放流量，等于把故障从「确定的不可用」变成「不确定的卡死」。

Redis 在阶段 6 才接入（ADR-006），当前显式标记为 ``skipped`` 而不是伪报 ok：
探针谎报依赖健康，只是把这个依赖的故障推迟到线上才发现。
"""

from __future__ import annotations

import asyncio
import logging
import re
from collections.abc import Awaitable, Callable

from fastapi import APIRouter, Response
from sqlalchemy import text

from app.clients import embedding, es, qdrant
from app.clients.mysql import get_dw_engine, get_engine
from app.core.config import get_settings

logger = logging.getLogger(__name__)

router = APIRouter(tags=["system"])

DEFAULT_TIMEOUT_SECONDS = 3.0
# Embedding 服务冷启动要加载权重（bge-m3 约 1.07GiB），首次探测可能耗数秒
EMBEDDING_TIMEOUT_SECONDS = 10.0
MAX_DETAIL_CHARS = 300

CheckFn = Callable[[], Awaitable[str]]


# --------------------------------------------------------------------------
# 单项探测：成功返回一句可读的详情，失败直接抛异常（由 _run_check 收敛）
# --------------------------------------------------------------------------


async def _check_mysql_meta() -> str:
    async with get_engine().connect() as conn:
        await conn.execute(text("SELECT 1"))
    return "SELECT 1 正常"


async def _check_mysql_dw() -> str:
    """dw 库单独探测：它是独立库 + 只读账号，meta 通不代表 dw 通。

    用常驻连接池。阶段 4 时这里是一次性引擎（当时没有任何请求会查 dw，为探针常开
    一个池不划算）；阶段 5 起每次问答的执行环节都走这个池，探针再用一次性引擎就会
    探错对象——建连成功但池已耗尽时，探针报 ok 而真实查询在排队等待。
    复用同一个池，探到的才是请求实际会用的那条路。
    """
    async with get_dw_engine().connect() as conn:
        await conn.execute(text("SELECT 1"))
    return "SELECT 1 正常"


async def _check_qdrant() -> str:
    settings = get_settings()
    client = qdrant.get_qdrant()
    names = {c.name for c in (await client.get_collections()).collections}

    if settings.qdrant.is_local:
        where = f"嵌入式模式 {settings.qdrant.local_path}"
    else:
        where = f"{settings.qdrant.host}:{settings.qdrant.port}"

    missing = [
        name
        for name in (settings.qdrant.column_collection, settings.qdrant.metric_collection)
        if name not in names
    ]
    if missing:
        # 服务可达但集合没建：连通性没问题，不判失败，但把事实报出来。
        # 建库是独立的运维动作（build_meta），不该让探针代替它判生死。
        return f"{where} 可达，集合未建：{'、'.join(missing)}"
    return f"{where} 可达，集合 {len(names)} 个"


async def _check_es() -> str:
    settings = get_settings()
    client = es.get_es()
    if not await client.ping():
        raise RuntimeError("ping 返回 False")
    if await client.indices.exists(index=settings.es.index_name):
        return f"{settings.es.url} 可达，索引 {settings.es.index_name} 已建"
    return f"{settings.es.url} 可达，索引 {settings.es.index_name} 未建"


async def _check_embedding() -> str:
    """真调一次 embedding，而不是只探端口。

    Embedding 会出现「端口活着、模型加载失败」这种半死状态——我们实测过：
    提交内存不足时 Ollama 的 bge-m3 加载直接断言失败，服务进程却还在监听。
    只探端口会漏掉正是最需要被发现的那类故障。
    """
    vector = await embedding.embed_text("健康检查")
    return f"{get_settings().embedding.host} 正常，维度 {len(vector)}"


# 用列表而非元组：测试要能替换其中某一项来验证 503 分支
CHECKS: list[tuple[str, CheckFn, float]] = [
    ("mysql_meta", _check_mysql_meta, DEFAULT_TIMEOUT_SECONDS),
    ("mysql_dw", _check_mysql_dw, DEFAULT_TIMEOUT_SECONDS),
    ("qdrant", _check_qdrant, DEFAULT_TIMEOUT_SECONDS),
    ("es", _check_es, DEFAULT_TIMEOUT_SECONDS),
    ("embedding", _check_embedding, EMBEDDING_TIMEOUT_SECONDS),
]

# 尚未接入的依赖：如实报告未探测，不伪报 ok。阶段 6 接入后并入 CHECKS（ADR-006）
DEFERRED_CHECKS: dict[str, str] = {"redis": "阶段 6 接入（ADR-006）"}


# --------------------------------------------------------------------------
# 探测结果的收集与脱敏
# --------------------------------------------------------------------------

# 只捕获到口令之前的部分，保留用户名：用户名不是敏感信息，而"哪个账号连不上"
# 恰恰是排查时最需要的信息（阶段 4 就是靠 Access denied for user 'readonly' 定位到
# 用错了账号）。整段 user:pass 一起盖掉会把有用的线索也抹掉。
_CREDENTIAL_URL = re.compile(r"(://[^:/@\s]+):[^@/\s]+@")


def _redact(value: str) -> str:
    """抹掉可能出现在异常文本里的口令，并截断长度。

    探针响应体是给运维和编排器看的，不走业务接口那条「细节只进日志」的路
    （见 core.handlers），所以口令泄漏要在这里自己兜住：既盖掉连接串里的
    明文口令，也盖掉配置中所有已知密钥值（NFR-SEC-003）。
    """
    settings = get_settings()
    cleaned = _CREDENTIAL_URL.sub(r"\1:***@", value)
    for secret in (
        settings.db_meta.password,
        settings.db_dw.password,
        settings.es.password,
        settings.redis.password,
        settings.jwt.secret_key,
    ):
        if secret:
            cleaned = cleaned.replace(secret, "***")
    return cleaned[:MAX_DETAIL_CHARS]


async def _run_check(name: str, check: CheckFn, timeout: float) -> dict[str, str]:
    """跑单项探测，把异常与超时收敛成 fail 结果，绝不让异常冒出去。

    冒出去会被全局 handler 套上 ``{code, message, data}`` 信封（TDD 7.2 要求
    探针返回裸结构），也会让一项依赖故障掩盖住其余依赖的状态。
    """
    try:
        detail = await asyncio.wait_for(check(), timeout)
    except TimeoutError:
        logger.warning("就绪探测超时：%s（>%.3gs）", name, timeout)
        return {"status": "fail", "detail": f"超时（>{timeout:g}s）"}
    except Exception as exc:  # noqa: BLE001 —— 任何异常都只意味着"这项不可用"
        logger.warning("就绪探测失败：%s", name, exc_info=exc)
        return {"status": "fail", "detail": f"{type(exc).__name__}: {_redact(str(exc))}"}
    return {"status": "ok", "detail": detail}


# --------------------------------------------------------------------------
# 端点
# --------------------------------------------------------------------------


@router.get("/health", summary="存活探针")
async def health() -> dict:
    """进程存活即返回 200，不探测外部依赖。"""
    settings = get_settings()
    return {
        "status": "ok",
        "app": settings.app.name,
        "version": settings.app.version,
        "debug": settings.app.debug,
    }


@router.get("/health/ready", summary="就绪探针")
async def readiness(response: Response) -> dict:
    """就绪探针：任一必需依赖不可用即 503（FR-SYS-002）。

    并发探测（asyncio.gather），耗时取最慢的一项而不是各项之和。
    探测结果逐项返回，运维能一眼看出是哪一项拖住了，而不是只知道"没就绪"。
    """
    results = await asyncio.gather(
        *(_run_check(name, check, timeout) for name, check, timeout in CHECKS)
    )
    checks = {name: result for (name, _check, _timeout), result in zip(CHECKS, results)}
    for name, reason in DEFERRED_CHECKS.items():
        checks[name] = {"status": "skipped", "detail": reason}

    failed = [name for name, result in checks.items() if result["status"] == "fail"]
    if failed:
        # 用 response.status_code 而不是抛 HTTPException：抛异常会被全局 handler
        # 套上信封，破坏探针的裸结构约定（TDD 7.2）
        response.status_code = 503
    return {
        "status": "unavailable" if failed else "ok",
        "checks": checks,
    }
