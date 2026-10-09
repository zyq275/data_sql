"""SQL 只读安全校验（BR-SQL-001、TDD 13、ADR-008）。

纯逻辑，不碰任何外部服务：这条闸门是"写操作进不了数仓"的最后一道保险，
它的用例必须能在任何机器上、任何 CI 里跑。放进 requires_stack 等于把保险的
测试绑在数据库可用性上，而数据库往往是最先不可用的那个。

用例分两半写：**该放行的**和**该拦下的**。只写后者会养出"越严越好"的倾向，
而把 UNION、字符串里的关键字、带注释的正常查询一并拦掉，用户看到的是
"这个问法不行"，且完全不知道为什么。
"""

from __future__ import annotations

import pytest

from app.core.exceptions import SQLSecurityError
from app.core.sql_security import assert_readonly, build_explain, leading_keyword

ACCEPTED = [
    "SELECT province FROM fact_order LIMIT 3",
    "select 1",
    "WITH t AS (SELECT 1 AS a) SELECT a FROM t",
    "SELECT * FROM `year`",  # 反引号包住 MySQL 保留字
    "SELECT region, SUM(amount) FROM fact_order GROUP BY region",
    "SELECT * FROM a JOIN b ON a.id = b.a_id",  # JOIN 里的 ON 不是关键字黑名单命中项
    "SELECT COUNT(*) FROM fact_order WHERE order_date >= 20250101",
    "EXPLAIN SELECT 1",
    "SHOW TABLES",
    "DESC fact_order",
    # 字符串字面量里的关键字不算：否则地区名、备注里出现 "delete" 就查不了
    "SELECT * FROM t WHERE note = 'DROP TABLE fact_order'",
    "SELECT * FROM t WHERE action = 'UPDATE'",
    # 注释里的关键字同上
    "SELECT 1 -- 这里想写 DROP 但只是注释",
    "SELECT 1 /* 只是注释，不是可执行注释 */",
    # WITH 开头、整体只读
    "WITH recent AS (SELECT * FROM fact_order) SELECT COUNT(*) FROM recent",
]

REJECTED = [
    "DROP TABLE fact_order",
    "INSERT INTO t VALUES (1)",
    "UPDATE t SET a = 1",
    "DELETE FROM t",
    "TRUNCATE TABLE t",
    "ALTER TABLE t ADD COLUMN c INT",
    "CREATE TABLE t (a INT)",
    "GRANT ALL ON *.* TO 'x'@'%'",
    "CALL some_proc()",
    "SET @x = 1",
    "USE dw",
    # 多语句：分开看每条都无害，合起来却是注入的经典形态
    "SELECT 1; DROP TABLE t",
    # SELECT 开头、实则写入（这就是不能用正则判定的原因）
    "WITH x AS (SELECT 1) DELETE FROM t",
    # 写文件
    "SELECT * FROM t INTO OUTFILE '/tmp/x'",
    # MySQL 可执行注释：注释里藏着能真执行的语句
    "SELECT 1 /*!50000 UNION SELECT user() */",
    # 只有空白 / 注释
    "",
    "   ",
    "-- 只有一行注释",
]


@pytest.mark.parametrize("sql", ACCEPTED)
def test_readonly_statements_are_accepted(sql: str) -> None:
    assert assert_readonly(sql)


@pytest.mark.parametrize("sql", REJECTED)
def test_writing_and_dangerous_statements_are_rejected(sql: str) -> None:
    with pytest.raises(SQLSecurityError):
        assert_readonly(sql)


def test_hash_comment_does_not_hide_a_write() -> None:
    """``#`` 是 MySQL 的注释符，但 sqlparse 不认它。

    于是 ``# DROP TABLE t`` 里的 DROP 在解析视角下**不是注释**，仍会落到关键字
    黑名单上——这里固化的正是这个"解析器与 MySQL 语义不一致"地带的实际行为。
    断言的是"最终不放行"，而不是"走哪条分支拦下的"：前者是对外承诺，后者是实现细节。
    """
    with pytest.raises(SQLSecurityError):
        assert_readonly("# 随便写点什么\nDROP TABLE t")


def test_code_fence_is_stripped_and_the_sql_is_returned() -> None:
    """模型爱把 SQL 包进 ``` 里；剥围栏是校验的一部分，返回值应是剥好的语句。"""
    assert assert_readonly("```sql\nSELECT 1\n```") == "SELECT 1"


def test_returned_sql_is_what_gets_validated_not_the_raw_text() -> None:
    """剥完围栏只剩空串时也必须拦下，不能因为"原文非空"就放行。"""
    with pytest.raises(SQLSecurityError):
        assert_readonly("```sql\n```")


@pytest.mark.parametrize(
    ("sql", "expected"),
    [
        ("SELECT 1", "SELECT"),
        ("  select 1", "SELECT"),
        ("-- 前导注释\nSELECT 1", "SELECT"),
        ("WITH t AS (SELECT 1) SELECT * FROM t", "WITH"),
        ("/* 块注释 */ SELECT 1", "SELECT"),
    ],
)
def test_leading_keyword_skips_comments_and_whitespace(sql: str, expected: str) -> None:
    assert leading_keyword(sql) == expected


@pytest.mark.parametrize(
    ("sql", "expected_prefix"),
    [
        ("SELECT 1", "EXPLAIN SELECT 1"),
        ("WITH t AS (SELECT 1) SELECT * FROM t", "EXPLAIN WITH t AS (SELECT 1) SELECT * FROM t"),
    ],
)
def test_explainable_statements_get_an_explain(sql: str, expected_prefix: str) -> None:
    assert build_explain(sql) == expected_prefix


@pytest.mark.parametrize("sql", ["SHOW TABLES", "DESC fact_order"])
def test_non_explainable_statements_are_not_prechecked(sql: str) -> None:
    """SHOW / DESC 本来就不取数，硬套 EXPLAIN 反而会造出语法错误。

    返回 None 表示"没有预检语句"，由调用方按"不预检但放行"处理。
    """
    assert build_explain(sql) is None
