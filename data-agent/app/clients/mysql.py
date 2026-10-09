"""MySQL 连接生命周期（meta 可写库 + dw 只读库，ADR-004）。

两个库各自持有进程级引擎单例，由 main.lifespan 在停机时释放。

分开的不只是"两个库"，而是**两套权限**：meta 用可写账号，dw 用只读账号
（ADR-008 / NFR-SEC-007）。引擎分开，连接池就不会串用凭据；只读账号在机制上
写不了数仓，于是即使 SQL 安全校验被绕过，最后一道闸门仍然关着。

dw 的池从阶段 5 的 execute_sql 开始才真正被用上，此前只有就绪探针用一次性引擎探活。
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

from sqlalchemy.ext.asyncio import (
    AsyncEngine,
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)

from app.core.config import DatabaseSettings, get_settings

META = "meta"
DW = "dw"

_engines: dict[str, AsyncEngine] = {}
_session_factories: dict[str, async_sessionmaker[AsyncSession]] = {}


def _database_for(key: str) -> DatabaseSettings:
    settings = get_settings()
    return settings.db_meta if key == META else settings.db_dw


def _engine_for(key: str) -> AsyncEngine:
    """按库取引擎，首次调用时创建。"""
    if key not in _engines:
        db = _database_for(key)
        _engines[key] = create_async_engine(
            db.url,
            echo=db.echo,
            pool_size=db.pool_size,
            max_overflow=db.max_overflow,
            pool_pre_ping=True,  # 长连接被 MySQL 掐断后自动重连
            pool_recycle=3600,
        )
    return _engines[key]


def _session_factory_for(key: str) -> async_sessionmaker[AsyncSession]:
    if key not in _session_factories:
        _session_factories[key] = async_sessionmaker(
            _engine_for(key),
            expire_on_commit=False,  # commit 后仍可读属性，便于直接序列化返回
        )
    return _session_factories[key]


def get_engine() -> AsyncEngine:
    """meta 库引擎（可写账号）。"""
    return _engine_for(META)


def get_session_factory() -> async_sessionmaker[AsyncSession]:
    """meta 库会话工厂。"""
    return _session_factory_for(META)


def get_dw_engine() -> AsyncEngine:
    """dw 库引擎（只读账号，ADR-008）。"""
    return _engine_for(DW)


def get_dw_session_factory() -> async_sessionmaker[AsyncSession]:
    """dw 库会话工厂。"""
    return _session_factory_for(DW)


async def dispose_engine() -> None:
    """释放全部 MySQL 连接池（应用停机时调用）。"""
    for engine in _engines.values():
        await engine.dispose()
    _engines.clear()
    _session_factories.clear()


@asynccontextmanager
async def session_scope() -> AsyncIterator[AsyncSession]:
    """meta 库会话作用域，供不经过 FastAPI 依赖注入的调用方使用。

    图节点在并行分支里各开各的会话，不复用请求级那一个：SQLAlchemy 的
    ``AsyncSession`` 不是并发安全的，三路召回公用一个会话会直接串数据。
    """
    async with _session_factory_for(META)() as session:
        try:
            yield session
        except Exception:
            await session.rollback()
            raise


async def get_session() -> AsyncIterator[AsyncSession]:
    """FastAPI 依赖：每请求一个会话。

    只负责开闭，事务边界由服务层显式 ``commit`` —— 这样一次业务操作里的多次写入
    是一个原子单元，也不会出现「响应已发出、提交却在依赖清理阶段」的时序歧义。
    """
    async with session_scope() as session:
        yield session
