"""meta_config.yaml 的领域实体与加载校验（TDD 8.2 / NFR-MAINT-001）。

知识库靠改配置扩展，不改代码。既然是配置驱动，最怕的就是配置写错却到运行期
才暴露——所以这里的校验刻意做得比较严：外键指向的表/字段、指标引用的字段、
重名冲突，全部在**动手写任何存储之前**一次性查出来并报出所有问题，
而不是写一半再 rollback。
"""

from __future__ import annotations

import re
import uuid
from pathlib import Path

import yaml
from pydantic import BaseModel, Field, ValidationError

from app.core.config import BASE_DIR

META_CONFIG_FILE = BASE_DIR / "conf" / "meta_config.yaml"

#: 过滤谓词里一律禁止的字符。``;`` 能把一个谓词变成两条语句，``--`` 与 ``/*`` 能把
#: 后面的一切注释掉——而这段文本是**直接拼进 WHERE 子句**去执行的，不设闸门就等于
#: 让配置文件获得了任意 SQL 的能力。谓词里的单引号不在此列：它本来就是字面量的一部分。
_PREDICATE_FORBIDDEN = (";", "--", "/*", "*/")

#: 谓词里允许出现的 SQL 关键字（大小写不敏感）。除它们之外，剩下的每个标识符都必须是
#: ``表名.字段名`` 的形式——**强制限定表名**是为了让看板的权限校验有确定的答案：
#: 裸字段名在数仓里不唯一（fact_order.product_id 与 dim_product.product_id 同名），
#: 拿到一个裸名字，判定"它属于哪张表"就只能靠猜。
_PREDICATE_KEYWORDS = frozenset(
    {"and", "or", "not", "in", "between", "like", "is", "null", "true", "false"}
)

_QUALIFIED = re.compile(r"\b([A-Za-z_]\w*)\.([A-Za-z_]\w*)\b")
_LITERAL = re.compile(r"'(?:[^'\\]|\\.|'')*'")
_IDENTIFIER = re.compile(r"\b[A-Za-z_]\w*\b")

# 生成确定性 id 的命名空间：同一份配置反复构建得到同样的 id，
# MySQL 主键、Qdrant 点 id、ES 文档 id 三者因此都稳定，重建不会留下孤儿数据。
ID_NAMESPACE = uuid.UUID("6f9619ff-8b86-d011-b42d-00c04fc964ff")


def table_id(db_name: str, table_name: str) -> str:
    return str(uuid.uuid5(ID_NAMESPACE, f"table:{db_name}.{table_name}"))


def column_id(db_name: str, table_name: str, column_name: str) -> str:
    return str(uuid.uuid5(ID_NAMESPACE, f"column:{db_name}.{table_name}.{column_name}"))


def metric_id(metric_name: str) -> str:
    return str(uuid.uuid5(ID_NAMESPACE, f"metric:{metric_name}"))


class ColumnDef(BaseModel):
    """字段定义。"""

    column_name: str
    column_type: str
    column_alias: str | None = None
    description: str | None = None
    is_primary_key: bool = False
    is_foreign_key: bool = False
    ref_table: str | None = None
    ref_column: str | None = None
    # 该字段的取值是否需要同步进 ES（取值召回）
    sync_values: bool = False


class TableDef(BaseModel):
    """表定义。"""

    table_name: str
    table_alias: str | None = None
    description: str | None = None
    is_dimension: bool = False
    columns: list[ColumnDef] = Field(default_factory=list)


class DatabaseDef(BaseModel):
    """库定义。"""

    db_name: str
    tables: list[TableDef] = Field(default_factory=list)


class MetricDef(BaseModel):
    """指标定义。calculation / filter_condition / time_range 构成业务口径（BR-META-002）。"""

    metric_name: str
    metric_alias: str | None = None
    description: str | None = None
    calculation: str | None = None
    filter_condition: str | None = None
    filter_sql: str | None = None
    """过滤口径的 SQL 谓词形式（阶段 10）。

    与 ``filter_condition`` 是**同一件事的两种形态**，缺一不可：前者是给模型读的
    口径说明（进提示词与向量文本），后者是给程序执行的谓词（进看板的 WHERE 子句）。
    看板拼的是模板 SQL，它没有"读懂散文"这一步，所以 ``None``（没写）在看板上会被
    拒掉而不是按不过滤算——后者会给出一个与问答链路口径不一致的数字，而两个数字
    都"看着正常"，没人会对账。

    **写 ``""`` 与不写是两件事**：``""`` 是"这个指标确实不过滤"（比如累计注册用户数），
    看板照常出数、只是不加谓词；``None`` 是漏写，看板会拒绝这个指标并说明原因。
    区分它们不需要额外的字段——``str | None`` 本来就分得开。
    """

    time_range: str | None = None
    # 相关字段名，构建时解析为 column_id
    relevant_columns: list[str] = Field(default_factory=list)


class MetaConfig(BaseModel):
    """meta_config.yaml 的结构。"""

    databases: list[DatabaseDef] = Field(default_factory=list)
    metrics: list[MetricDef] = Field(default_factory=list)


class MetaConfigError(ValueError):
    """配置校验失败，消息内含全部问题。"""


class MetaConfigLoader:
    """加载 + 校验 meta_config.yaml。"""

    def __init__(self, config: MetaConfig) -> None:
        self.config = config
        # (db_name, table_name, column_name) -> ColumnDef
        self._columns: dict[tuple[str, str, str], ColumnDef] = {}
        # 字段名 -> [(db_name, table_name, column_name)]，用于指标的相关字段解析
        self._column_by_name: dict[str, list[tuple[str, str, str]]] = {}
        # (表名, 字段名) 全集，用于校验 filter_sql 里的 ``表.字段`` 引用
        self._column_pairs: set[tuple[str, str]] = set()
        for db in config.databases:
            for table in db.tables:
                for column in table.columns:
                    key = (db.db_name, table.table_name, column.column_name)
                    self._columns[key] = column
                    self._column_by_name.setdefault(column.column_name, []).append(key)
                    self._column_pairs.add((table.table_name, column.column_name))

    # -- 查询辅助 ---------------------------------------------------------

    def tables(self) -> list[tuple[DatabaseDef, TableDef]]:
        return [(db, t) for db in self.config.databases for t in db.tables]

    def sync_columns(self) -> list[tuple[DatabaseDef, TableDef, ColumnDef]]:
        """所有标记了 sync_values 的字段。"""
        return [
            (db, t, c)
            for db in self.config.databases
            for t in db.tables
            for c in t.columns
            if c.sync_values
        ]

    def resolve_metric_columns(self, metric: MetricDef) -> list[tuple[str, str, str]]:
        """把指标的相关字段名解析成 (db, table, column) 列表。

        同名字段出现在多张表时全部返回——宁可多召回一个候选字段，
        也不要因为配置里少写了限定而漏掉真正相关的那个。
        """
        found: list[tuple[str, str, str]] = []
        for name in metric.relevant_columns:
            found.extend(self._column_by_name.get(name, []))
        return found

    # -- 校验 -------------------------------------------------------------

    def validate(self) -> None:
        """一次性收集全部配置问题后抛出。"""
        problems: list[str] = []

        if not self.config.databases:
            problems.append("databases 为空，没有可构建的知识")
        if not self.config.metrics:
            problems.append("metrics 为空，没有可构建的指标")

        seen_tables: set[tuple[str, str]] = set()
        for db, table in self.tables():
            tkey = (db.db_name, table.table_name)
            if tkey in seen_tables:
                problems.append(f"表重复定义：{db.db_name}.{table.table_name}")
            seen_tables.add(tkey)

            if not table.columns:
                problems.append(f"表 {tkey[1]} 没有任何字段")
            seen_columns: set[str] = set()
            pk_count = 0
            for column in table.columns:
                if column.column_name in seen_columns:
                    problems.append(f"字段重复定义：{tkey[1]}.{column.column_name}")
                seen_columns.add(column.column_name)
                if column.is_primary_key:
                    pk_count += 1

                # 外键必须给出确切的指向，否则召回后无法补齐关联（BR-META-001）
                if column.is_foreign_key:
                    if not (column.ref_table and column.ref_column):
                        problems.append(
                            f"外键 {tkey[1]}.{column.column_name} 缺少 ref_table/ref_column"
                        )
                    elif (db.db_name, column.ref_table, column.ref_column) not in self._columns:
                        problems.append(
                            f"外键 {tkey[1]}.{column.column_name} 指向不存在的字段 "
                            f"{column.ref_table}.{column.ref_column}"
                        )
                elif column.ref_table or column.ref_column:
                    problems.append(
                        f"{tkey[1]}.{column.column_name} 填了 ref_table/ref_column 但未标记 is_foreign_key"
                    )
            if pk_count == 0:
                problems.append(f"表 {tkey[1]} 没有主键字段")

        seen_metrics: set[str] = set()
        for metric in self.config.metrics:
            if metric.metric_name in seen_metrics:
                problems.append(f"指标重复定义：{metric.metric_name}")
            seen_metrics.add(metric.metric_name)

            if not metric.calculation:
                problems.append(f"指标 {metric.metric_name} 缺少 calculation（口径不完整，BR-META-002）")
            for name in metric.relevant_columns:
                if name not in self._column_by_name:
                    problems.append(
                        f"指标 {metric.metric_name} 的相关字段 {name} 在任何表中都不存在"
                    )
            problems.extend(self._check_filter_sql(metric))

        if problems:
            raise MetaConfigError(
                f"meta_config.yaml 校验失败，共 {len(problems)} 处问题：\n  - "
                + "\n  - ".join(problems)
            )

    def _check_filter_sql(self, metric: MetricDef) -> list[str]:
        """校验过滤谓词：无危险字符、字段必须 ``表名.字段名`` 限定且真实存在。

        **这里是唯一能拦住它的时候**：这条谓词在运行期会被原样拼进看板的 WHERE 子句，
        校验漏掉的每一处都会直接变成一条在数仓上执行的语句。所以宁可在这里把格式
        管严一点——谓词写歪了本就该在构建知识库时就报出来，而不是等看板算出个错数字。

        ``None``（没写）与 ``""``（写了空）都不校验：前者是"这个指标只给问答用"，
        后者是"这个指标不过滤"，两种都不是格式错误。**不写不是错误**——一个指标
        完全可以先服务于问答链路，看板那边等口径想清楚了再补。
        """
        sql = metric.filter_sql
        if not sql:
            return []
        name = metric.metric_name
        problems: list[str] = []

        hits = [bad for bad in _PREDICATE_FORBIDDEN if bad in sql]
        if hits:
            problems.append(
                f"指标 {name} 的 filter_sql 含 {('、'.join(hits))}：只接受单个谓词表达式"
            )
            # 已经含有语句分隔/注释标记，后面的标识符检查没有意义了
            return problems

        refs = _QUALIFIED.findall(sql)
        if not refs:
            problems.append(
                f"指标 {name} 的 filter_sql 没有以「表名.字段名」的形式引用字段：{sql}"
            )
        for table, column in refs:
            if (table, column) not in self._column_pairs:
                problems.append(
                    f"指标 {name} 的 filter_sql 引用了不存在的字段 {table}.{column}"
                )

        # 剥掉字面量与限定引用之后，剩下的标识符只允许是 SQL 关键字；
        # 还剩下的就是没限定表名的裸字段名——它在数仓里不唯一，判定归属只能靠猜
        leftover = [
            token
            for token in _IDENTIFIER.findall(_QUALIFIED.sub(" ", _LITERAL.sub(" ", sql)))
            if token.lower() not in _PREDICATE_KEYWORDS
        ]
        if leftover:
            problems.append(
                f"指标 {name} 的 filter_sql 里有未限定表名的标识符 {leftover}："
                "字段名在数仓里不唯一，必须写成「表名.字段名」"
            )
        return problems


def load_meta_config(path: Path | None = None) -> MetaConfigLoader:
    """读取并校验 meta_config.yaml。

    显式 UTF-8：Windows 中文环境默认用 GBK 打开文本文件，文件里的中文会解码失败。
    """
    path = path or META_CONFIG_FILE
    if not path.exists():
        raise MetaConfigError(f"元数据配置文件不存在：{path}")

    with path.open("r", encoding="utf-8") as f:
        raw = yaml.safe_load(f) or {}

    try:
        config = MetaConfig.model_validate(raw)
    except ValidationError as exc:
        raise MetaConfigError(f"meta_config.yaml 结构不合法：{exc}") from exc

    loader = MetaConfigLoader(config)
    loader.validate()
    return loader
