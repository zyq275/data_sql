"""SQL 校验：只读安全 + EXPLAIN 预检（FR-QA-007）。"""

from __future__ import annotations

import logging

from langgraph.runtime import Runtime

from app.agent.context import QueryContext
from app.agent.events import step
from app.agent.state import QueryState
from app.core.exceptions import SQLExecutionError, SQLGenerationError, SQLSecurityError
from app.core.sql_security import assert_readonly, build_explain

logger = logging.getLogger(__name__)


@step("validate_sql")
async def validate_sql(state: QueryState, runtime: Runtime[QueryContext]) -> dict:
    """校验生成的 SQL，把结论写回 ``error`` / ``error_code`` 驱动条件边分流。

    两道关，顺序不能颠倒：

    1. **只读安全校验** —— 纯解析，不碰数据库。不允许执行的语句根本不该被送到
       数仓面前，哪怕只是 EXPLAIN。
    2. **EXPLAIN 预检** —— 让 MySQL 自己说这条语句能不能跑。表名写错、字段不存在、
       聚合不合法都会在这里暴露，正好拿去驱动 correct_sql。EXPLAIN 不取数，
       比真跑一遍便宜得多，这也是"校验"与"执行"能拆成两个节点的前提。
    """
    sql = state.get("sql") or ""
    try:
        safe_sql = assert_readonly(sql)
    except SQLSecurityError as exc:
        return _rejected(exc.message, exc.code)

    explain = build_explain(safe_sql)
    if explain is None:
        # 语句类型不支持 EXPLAIN（只读白名单里的 SHOW / DESC 等）；安全校验已过，
        # 直接放行，不因为"没法预检"就把合法查询拒掉。
        return {"sql": safe_sql, "explain_sql": None, "error": None, "error_code": None}

    try:
        await runtime.context.dw.explain(explain)
    except SQLExecutionError as exc:
        # 预检失败归到"生成"而不是"执行"：SQL 还没真正跑过，问题出在语句本身，
        # 应该交给 correct_sql 去修。用 40202 会让前端以为数仓出故障了。
        return _rejected(f"SQL 预检未通过：{exc.message}", SQLGenerationError.code)

    return {"sql": safe_sql, "explain_sql": explain, "error": None, "error_code": None}


def _rejected(message: str, code: int) -> dict:
    """记录一次校验失败。

    **只写错误，不推 error 事件**：校正循环里会失败多次，逐个推事件会让前端弹出
    一串错误提示，而其中大部分马上就被修好了。真正的 error 事件由服务层在
    "图跑完但仍然带着错误"时推一次（TDD 12.2）。
    """
    logger.info("SQL 校验未通过：%s", message)
    return {"error": message, "error_code": code}
