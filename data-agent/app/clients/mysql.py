"""MySQL 连接生命周期（meta 库读写）。

dw 库为只读数仓，其连接在阶段 5 执行环节引入，此处只管理 meta。
引擎按进程持有单例，由 main.lifespan 在停机时释放。
"""

from __future__ import annotations

from collections.abc import AsyncIterator

from sqlalchemy.ext.asyncio import (
    AsyncEngine,
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)

from app.core.config import get_settings

_engine: AsyncEngine | None = None
_session_factory: async_sessionmaker[AsyncSession] | None = None


def get_engine() -> AsyncEngine:
    """meta 库引擎（首次调用时创建）。"""
    global _engine
    if _engine is None:
        settings = get_settings()
        db = settings.db_meta
        _engine = create_async_engine(
            db.url,
            echo=db.echo,
            pool_size=db.pool_size,
            max_overflow=db.max_overflow,
            pool_pre_ping=True,  # 长连接被 MySQL 掐断后自动重连
            pool_recycle=3600,
        )
    return _engine


def get_session_factory() -> async_sessionmaker[AsyncSession]:
    """会话工厂（首次调用时创建）。"""
    global _session_factory
    if _session_factory is None:
        _session_factory = async_sessionmaker(
            get_engine(),
            expire_on_commit=False,  # commit 后仍可读属性，便于直接序列化返回
        )
    return _session_factory


async def dispose_engine() -> None:
    """释放连接池（应用停机时调用）。"""
    global _engine, _session_factory
    if _engine is not None:
        await _engine.dispose()
    _engine = None
    _session_factory = None


async def get_session() -> AsyncIterator[AsyncSession]:
    """FastAPI 依赖：每请求一个会话。

    只负责开闭，事务边界由服务层显式 ``commit`` —— 这样一次业务操作里的多次写入
    是一个原子单元，也不会出现「响应已发出、提交却在依赖清理阶段」的时序歧义。
    """
    factory = get_session_factory()
    async with factory() as session:
        try:
            yield session
        except Exception:
            await session.rollback()
            raise
