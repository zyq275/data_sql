"""数仓（dw）只读执行（阶段 5 的执行环节，ADR-008 / NFR-SEC-007）。

与 ``repositories/meta.py`` 的 MetaRepository 不同，这里**自己开连接**，而不是由
调用方传入会话：图里 ``validate_sql`` 与 ``execute_sql`` 会和别的分支并发，
共用一个 ``AsyncSession`` 不是并发安全的。请求级的 MetaRepository 仍是注入式，
两边差异由各自调用场景决定，不是风格不一致。

三重执行限制（TDD 13）：

1. 会话级 ``MAX_EXECUTION_TIME`` —— 让 MySQL 自己掐断，比在客户端干等可靠；
2. ``max_result_rows`` —— 用流式游标边取边停，不会把整表拉进内存再截断；
3. 墙钟超时兜底 —— 万一第 1 条没生效（只读账号 SET 失败、语句类型不受该参数管辖），
   请求也不会无限挂起。
"""

from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass, field
from datetime import date, datetime
from decimal import Decimal
from typing import Any

from sqlalchemy import text
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.ext.asyncio import AsyncConnection

from app.clients.mysql import get_dw_engine
from app.core.config import get_settings
from app.core.exceptions import QueryTimeoutError, SQLExecutionError

logger = logging.getLogger(__name__)

EXPLAIN_TIMEOUT_SECONDS = 10.0
# 墙钟超时在 MySQL 的 max_execution_time 之上再宽限一点：先让 MySQL 优雅地掐断，
# 客户端超时只用来兜底，两者同时到点会掩盖掉"到底是谁掐的"这个信息
GRACE_SECONDS = 5.0


@dataclass(slots=True)
class QueryResult:
    """一次查询的结果集。"""

    columns: list[str] = field(default_factory=list)
    rows: list[list[Any]] = field(default_factory=list)
    truncated: bool = False


def _message(exc: Exception) -> str:
    """取一句可读的错误。

    SQLAlchemy 的异常串里带着完整 SQL 与参数（``[SQL: ...] [parameters: ...]``），
    而这句话会进 SSE 的 error 事件给前端看。口令不会出现在这里（连接串在 DBAPI
    层已脱敏），但整条 SQL 塞进提示语既冗长又难读，因此只取首行。
    """
    raw = str(exc).strip()
    return (raw.splitlines()[0] if raw else type(exc).__name__)[:500]


def _jsonable(value: Any) -> Any:
    """把 MySQL 返回的 Python 值转成可 JSON 序列化的形式。

    Decimal 与 date/datetime 都不是 JSON 原生类型，而 SSE 事件要 ``json.dumps``，
    不转就会在推送那一刻抛 TypeError——那时 SQL 已经跑完，等于白跑一趟。
    Decimal 转 float：金额精度到分，float 足够，前端还要拿它算图表。
    末尾的 str() 兜底是为"想不到的类型"准备的：宁可显示成字符串，
    也不要让整个查询在最后一步炸掉。
    """
    if isinstance(value, Decimal):
        return float(value)
    if isinstance(value, (datetime, date)):
        return value.isoformat()
    if isinstance(value, (bytes, bytearray)):
        return value.decode("utf-8", errors="replace")
    if value is None or isinstance(value, (str, int, float)):
        return value
    return str(value)


class DwRepository:
    """数仓只读访问。"""

    def __init__(
        self,
        *,
        max_rows: int | None = None,
        timeout_seconds: float | None = None,
    ) -> None:
        security = get_settings().sql_security
        self._max_rows = security.max_result_rows if max_rows is None else max_rows
        self._timeout_ms = security.max_execution_time_ms
        self._timeout_seconds = (
            max(self._timeout_ms / 1000, 1.0) + GRACE_SECONDS
            if timeout_seconds is None
            else timeout_seconds
        )

    @classmethod
    def default(cls) -> DwRepository:
        return cls()

    async def explain(self, sql: str) -> None:
        """跑一次 EXPLAIN 做语法与语义预检；失败抛 :class:`SQLExecutionError`。"""
        try:
            async with asyncio.timeout(EXPLAIN_TIMEOUT_SECONDS):
                async with get_dw_engine().connect() as connection:
                    await connection.execute(text(sql))
        except TimeoutError as exc:
            raise SQLExecutionError(f"SQL 预检超过 {EXPLAIN_TIMEOUT_SECONDS:g}s 未返回") from exc
        except SQLAlchemyError as exc:
            raise SQLExecutionError(_message(exc)) from exc

    async def execute(self, sql: str) -> QueryResult:
        """执行只读查询。

        ``MAX_EXECUTION_TIME`` 是只作用于 SELECT 的限制，正合我们的用途；
        设置失败不终止查询——记日志，墙钟超时仍然兜着。只读账号能否 SET 会话变量
        取决于 MySQL 版本与授权，为这个把查询整个拒掉不划算。
        """
        rows: list[list[Any]] = []
        columns: list[str] = []
        try:
            async with asyncio.timeout(self._timeout_seconds):
                async with get_dw_engine().connect() as connection:
                    await self._apply_timeout(connection)
                    # 流式游标：取够 max_rows + 1 行就停，多余的既不从 MySQL 拉过来，
                    # 也不在内存里堆着——缓冲式执行是"先全拉回来再截断"，等于没限制
                    result = await connection.stream(text(sql))
                    columns = list(result.keys())
                    async for row in result:
                        rows.append([_jsonable(value) for value in row])
                        if len(rows) > self._max_rows:
                            break
        except TimeoutError as exc:
            raise QueryTimeoutError(f"查询超过 {self._timeout_seconds:g}s 未返回") from exc
        except SQLAlchemyError as exc:
            raise SQLExecutionError(_message(exc)) from exc

        truncated = len(rows) > self._max_rows
        return QueryResult(columns=columns, rows=rows[: self._max_rows], truncated=truncated)

    async def _apply_timeout(self, connection: AsyncConnection) -> None:
        try:
            await connection.execute(
                text(f"SET SESSION MAX_EXECUTION_TIME={int(self._timeout_ms)}")
            )
        except SQLAlchemyError as exc:
            logger.warning(
                "设置 MAX_EXECUTION_TIME 失败，仅靠墙钟超时（%.0fs）兜底：%s",
                self._timeout_seconds,
                _message(exc),
            )
