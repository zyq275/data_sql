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

    三道关，顺序不能颠倒：

    1. **只读安全校验** —— 纯解析，不碰数据库。不允许执行的语句根本不该被送到
       数仓面前，哪怕只是 EXPLAIN。
    2. **数据权限校验**（阶段 9）—— 同样纯解析。上下文里已经没有越权的表与字段了
       （见 filter_table / filter_metric / add_extra_context），所以这一关是对
       "模型自己编了一个表名"与"历史里带进来的旧表名"的兜底。它必须挡在 EXPLAIN
       之前：越权的语句连预检都不该做——预检要把它发给数仓。
    3. **EXPLAIN 预检** —— 让 MySQL 自己说这条语句能不能跑。表名写错、字段不存在、
       聚合不合法都会在这里暴露，正好拿去驱动 correct_sql。EXPLAIN 不取数，
       比真跑一遍便宜得多，这也是"校验"与"执行"能拆成两个节点的前提。

    **越权报错不带表名字段名**：``error`` 最终会被服务层推成 ``error`` 事件给用户看，
    而"你引用了一张你无权访问的表"里若带着表名，就等于向无权者确认了那张表存在。
    名字只进日志（``violations`` 的返回值），给用户的那句话只说结果。
    重写仍然做得成：correct_sql 手里有**裁剪过的**表结构，照着重写即可。
    """
    sql = state.get("sql") or ""
    try:
        safe_sql = assert_readonly(sql)
    except SQLSecurityError as exc:
        return _rejected(exc.message, exc.code)

    scope = await runtime.context.permissions.scope_for(runtime.context.role)
    violations = scope.violations(safe_sql)
    if violations:
        logger.warning("SQL 引用了角色 %s 未授权的对象：%s", scope.role, violations)
        return _rejected("生成的 SQL 引用了当前角色未授权的数据表或字段", SQLSecurityError.code)

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
