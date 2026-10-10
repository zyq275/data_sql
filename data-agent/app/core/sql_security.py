"""SQL 只读校验（BR-SQL-001、TDD 13、ADR-008）。

**为什么不用正则**：三条都能骗过"看开头几个字"的检查。

- ``WITH t AS (...) DELETE FROM x`` —— 以 WITH 开头，MySQL 8 真支持这种写法，会删数据；
- ``SELECT ... INTO OUTFILE '/tmp/x'`` —— 以 SELECT 开头，实际在写文件系统；
- ``SELECT 1; DROP TABLE t`` —— 两条语句，只查第一条前缀等于没查。

所以按 sqlparse 的 token 类型判定，分三层：**先剥噪声 → 再看首个关键字 → 最后全文扫危险词**。

剥噪声（注释与字符串字面量）是必需的：不剥，``WHERE s = '求购成功'`` 里的字面量会造成
误判；剥了，才能按"语句结构"而不是"文本碰巧包含"来判断——而恶意者本来也爱把关键字
藏在字符串或注释里，剥掉正好把这条路也堵上。
"""

from __future__ import annotations

import re

import sqlparse
from sqlparse import tokens as T
from sqlparse.sql import Statement

from app.core.config import get_settings
from app.core.exceptions import SQLSecurityError
from app.core.text import strip_code_fence

# 出现即拒绝的关键字（在剥掉注释与字符串之后按整词匹配）。
#
# 白名单管"语句以什么开头"，这张表管"语句里还藏了什么"，两者缺一不可：
# `SELECT ... INTO OUTFILE` 与 `WITH ... DELETE` 都能骗过前缀检查。
#
# 刻意不收 REPLACE：它既是写语句（REPLACE INTO），也是常用的字符串函数
# （SELECT REPLACE(name,'a','b')），收了会误伤正常查询；而写用法里的 INTO 已被拦下。
# INTO / OUTFILE / DUMPFILE 一并拦：只读分析用不到它们，而它们都能用来落盘。
# SET / USE / EXECUTE / PREPARE 属于会话级操作，同样不在只读分析的射程内。
FORBIDDEN_KEYWORDS: tuple[str, ...] = (
    "INSERT",
    "UPDATE",
    "DELETE",
    "DROP",
    "ALTER",
    "CREATE",
    "TRUNCATE",
    "RENAME",
    "GRANT",
    "REVOKE",
    "MERGE",
    "CALL",
    "LOAD",
    "LOCK",
    "UNLOCK",
    "SET",
    "USE",
    "EXECUTE",
    "PREPARE",
    "DEALLOCATE",
    "HANDLER",
    "INTO",
    "OUTFILE",
    "DUMPFILE",
)

_FORBIDDEN_PATTERN = re.compile(r"\b(?:" + "|".join(FORBIDDEN_KEYWORDS) + r")\b")

# 可以套 EXPLAIN 预检的语句类型。EXPLAIN 不能解释 EXPLAIN / DESC / SHOW 自身。
_EXPLAINABLE = ("SELECT", "WITH")


def _has_content(statement: Statement) -> bool:
    """语句里是否有实质 token——``;`` 或纯注释不算一条语句。"""
    for token in statement.flatten():
        if token.is_whitespace or token.ttype in T.Comment or token.ttype in T.Punctuation:
            continue
        return True
    return False


def _leading_keyword(statement: Statement) -> str | None:
    """语句的首个关键字（大写）；首个实质 token 不是关键字时返回 None。"""
    for token in statement.flatten():
        if token.is_whitespace or token.ttype in T.Comment:
            continue
        if token.ttype in T.Keyword:
            return token.value.upper()
        return None
    return None


def _effective_text(statement: Statement) -> str:
    """剥掉注释与字符串字面量后的语句文本，用于危险词扫描。"""
    parts: list[str] = []
    for token in statement.flatten():
        if token.ttype in T.Comment or token.ttype in T.Literal.String:
            continue
        parts.append(token.value)
    return "".join(parts)


def leading_keyword(sql: str) -> str | None:
    """公开给调用方判断语句类型（如决定能否 EXPLAIN 预检）。"""
    statements = [s for s in sqlparse.parse(strip_code_fence(sql)) if _has_content(s)]
    if not statements:
        return None
    return _leading_keyword(statements[0])


def assert_readonly(sql: str) -> str:
    """校验 SQL 只读，通过则返回清洗后的 SQL，否则抛 :class:`SQLSecurityError`。

    这是**执行前的唯一入口**：任何要送去数仓执行的 SQL 都必须先过这里，
    不能因为"提示词已经要求只生成 SELECT"就跳过——提示词是请求，不是保证。
    """
    cleaned = strip_code_fence(sql)
    if not cleaned:
        raise SQLSecurityError("生成的 SQL 为空")

    # MySQL 会执行 ``/*! ... */`` 里的内容，但它长得像注释。先剥注释再判定的做法
    # 恰好会把它整段跳过，等于留了一条绕过通道，所以见到就直接拒。
    if "/*!" in cleaned:
        raise SQLSecurityError("不支持 MySQL 可执行注释 /*! ... */")

    statements = [s for s in sqlparse.parse(cleaned) if _has_content(s)]
    if not statements:
        raise SQLSecurityError("SQL 里没有有效语句（只有空白或注释）")
    if len(statements) > 1:
        # 多条语句要么是模型不听话，要么是想把写操作夹带在合法查询后面。
        raise SQLSecurityError(f"只允许单条语句，实际有 {len(statements)} 条")

    statement = statements[0]
    allowed = {p.upper() for p in get_settings().sql_security.allowed_statement_prefixes}
    leading = _leading_keyword(statement)
    if leading is None:
        raise SQLSecurityError("无法识别语句类型，只接受以只读语句开头的 SQL")
    if leading not in allowed:
        raise SQLSecurityError(
            f"只允许 {'/'.join(sorted(allowed))} 开头的只读语句，实际以 {leading} 开头"
        )

    hits = sorted(set(_FORBIDDEN_PATTERN.findall(_effective_text(statement).upper())))
    if hits:
        raise SQLSecurityError(f"SQL 含写入或危险关键字：{'、'.join(hits)}")

    return cleaned


def _bare(value: str) -> str:
    """把 token 原文整理成一个裸标识符。

    去反引号（``\\`order_amount\\``）、去库名前缀（``dw.fact_order`` → ``fact_order``）：
    调用方拿它去和元数据里的表名/字段名比对，而元数据存的是不带引号、不带库名的名字。
    """
    name = value.strip().strip("`")
    if "." in name:
        name = name.rsplit(".", 1)[-1].strip().strip("`")
    return name


def _significant(sql: str) -> list:
    """解析出第一条有内容的语句的 token，并剥掉空白与注释。"""
    statements = [s for s in sqlparse.parse(strip_code_fence(sql)) if _has_content(s)]
    if not statements:
        return []
    return [
        token
        for token in statements[0].flatten()
        if not token.is_whitespace and token.ttype not in T.Comment
    ]


def referenced_tables(sql: str) -> set[str]:
    """从 FROM / JOIN 子句里提取被引用的表名（阶段 9 的权限兜底校验用）。

    **它不负责也不试图判断这些名字是不是真表**：调用方拿到的名字去和**元数据里
    已知的表名**求交集，交集之外的（CTE 名、派生表别名、写错的表名）自然被忽略。
    这样就不必去区分 ``WITH x AS (...) SELECT * FROM x`` 里的 ``x``——它不是
    元数据里的表，压根不会被当成越权。按"已知表名"来判，比按"语法上是不是表"
    来判简单得多，也少了一大类误判。

    ``FROM a, b`` 的逗号列表、``AS`` 别名、``LEFT OUTER JOIN`` 的多词关键字
    都要处理：漏掉其中任何一个，都会让"这条 SQL 引用了什么"低估，而漏掉的正是
    越权表时，兜底校验就形同虚设。
    """
    tokens = _significant(sql)
    found: set[str] = set()
    index = 0
    while index < len(tokens):
        token = tokens[index]
        upper = token.value.upper() if token.ttype in T.Keyword else ""
        if token.ttype in T.Keyword and (upper == "FROM" or upper.endswith("JOIN")):
            index += 1
            while index < len(tokens):
                nxt = tokens[index]
                if nxt.ttype in T.Punctuation and nxt.value == ",":
                    index += 1
                    continue
                if nxt.ttype in T.Punctuation and nxt.value == "(":
                    # 派生表：FROM (SELECT ...) t。括号里的表由它自己的 FROM 收走
                    break
                if nxt.ttype in T.Name or nxt.ttype is None:
                    # 库名限定 ``dw.fact_order`` 会被切成 ``dw`` ``.`` ``fact_order``
                    # 三个 token，取到家的是最后一段
                    parts = [_bare(nxt.value)]
                    index += 1
                    while (
                        index + 1 < len(tokens)
                        and tokens[index].ttype in T.Punctuation
                        and tokens[index].value == "."
                        and (tokens[index + 1].ttype in T.Name or tokens[index + 1].ttype is None)
                    ):
                        parts.append(_bare(tokens[index + 1].value))
                        index += 2
                    found.add(parts[-1])
                    # 吃掉 ``AS x`` 与 ``t x`` 两种别名写法，否则别名会被当成下一张表
                    if index < len(tokens) and tokens[index].value.upper() == "AS":
                        index += 1
                    if index < len(tokens) and (tokens[index].ttype in T.Name or tokens[index].ttype is None):
                        index += 1
                    continue
                break
            continue
        index += 1
    return found


def referenced_names(sql: str) -> set[str]:
    """提取 SQL 里出现的全部标识符（表名、字段名、别名、函数名一视同仁）。

    供阶段 9 的字段级兜底校验使用。同样不区分"这是字段还是别名"——调用方只拿它
    与"已知但未授权的字段名"求交集。别名或函数名恰好撞上某个未授权字段名时会误判，
    但那要两个名字一模一样才可能，且代价只是一次重写重试，比漏放一个未授权字段小得多。
    """
    return {
        _bare(token.value)
        for token in _significant(sql)
        if token.ttype in T.Name or token.ttype is None
    }


def build_explain(sql: str) -> str | None:
    """把只读语句包成 EXPLAIN 供执行前预检（FR-QA-007）；不可预检时返回 None。

    EXPLAIN 不取数，但会走完整的解析与优化流程——表名写错、字段不存在、聚合不合法
    都会在这里暴露，正好用来驱动 correct_sql。比真正执行一遍便宜得多，
    也是"校验"与"执行"能拆成两个节点的前提。
    """
    if leading_keyword(sql) in _EXPLAINABLE:
        return f"EXPLAIN {sql}"
    return None
