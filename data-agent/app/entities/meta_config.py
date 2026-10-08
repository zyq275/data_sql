"""meta_config.yaml 的领域实体与加载校验（TDD 8.2 / NFR-MAINT-001）。

知识库靠改配置扩展，不改代码。既然是配置驱动，最怕的就是配置写错却到运行期
才暴露——所以这里的校验刻意做得比较严：外键指向的表/字段、指标引用的字段、
重名冲突，全部在**动手写任何存储之前**一次性查出来并报出所有问题，
而不是写一半再 rollback。
"""

from __future__ import annotations

import uuid
from pathlib import Path

import yaml
from pydantic import BaseModel, Field, ValidationError

from app.core.config import BASE_DIR

META_CONFIG_FILE = BASE_DIR / "conf" / "meta_config.yaml"

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
        for db in config.databases:
            for table in db.tables:
                for column in table.columns:
                    key = (db.db_name, table.table_name, column.column_name)
                    self._columns[key] = column
                    self._column_by_name.setdefault(column.column_name, []).append(key)

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

        if problems:
            raise MetaConfigError(
                f"meta_config.yaml 校验失败，共 {len(problems)} 处问题：\n  - "
                + "\n  - ".join(problems)
            )


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
