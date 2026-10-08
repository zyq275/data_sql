"""元数据知识库离线构建（阶段 4，TDD 第 5 章的知识构建层）。

    python -m app.scripts.build_meta                # 全量重建 MySQL + Qdrant + ES
    python -m app.scripts.build_meta --no-vector    # 只重建 MySQL 与 ES
    python -m app.scripts.build_meta --no-value-index   # 只重建 MySQL 与 Qdrant
    python -m app.scripts.build_meta --embed-only   # 只重建 Qdrant（配置未变时最快）

流程：
    1. 读 conf/meta_config.yaml，校验（外键指向、指标口径、重名）
    2. 解析外键与指标相关字段 -> 生成确定性 id -> 写入 MySQL 四张知识表
    3. 字段/指标的 name+alias+description 向量化 -> 写入 Qdrant 两个集合
    4. 标记 sync_values 的字段，从 dw 取不重复取值 -> 写入 ES 取值索引
    5. 核对「配置声明」与「实际入库」是否一致，不一致直接失败

设计取舍：
  - 中文检索文本拼装时把别名放在最前。用户问的是"订单金额"而不是"order_amount"，
    别名与描述才是语义主体，字段名只是补充信号。
  - 取值索引的文档按 (字段, 取值) 做 md5 作为 id，重复构建覆盖同一文档，不会产生副本。
  - Embedding 走 HTTP 独立服务（ADR-005），本脚本不加载任何本地模型。
"""

from __future__ import annotations

import argparse
import asyncio
import sys

from qdrant_client import models as qmodels
from sqlalchemy import text
from sqlalchemy.ext.asyncio import create_async_engine

from app.clients import embedding, es, qdrant
from app.clients.mysql import dispose_engine, get_session_factory
from app.core.config import get_settings
from app.entities.meta_config import (
    ColumnDef,
    MetaConfigLoader,
    MetricDef,
    TableDef,
    column_id,
    load_meta_config,
    metric_id,
    table_id,
)
from app.repositories.meta import MetaRepository

# 每个字段/指标取多少条取值进 ES。取值基数可能很大（如客户姓名），
# 全量同步既慢又没必要——取值召回只需要覆盖有代表性的常见取值。
VALUE_SAMPLE_LIMIT = 500
UPSERT_BATCH = 64


# --------------------------------------------------------------------------
# 文本拼装（决定向量检索的质量，改动前请先想清楚检索侧会怎么问）
# --------------------------------------------------------------------------


def column_text(db_name: str, table: TableDef, column: ColumnDef) -> str:
    """字段的向量化文本。"""
    head = f"{column.column_alias}（{column.column_name}）" if column.column_alias else column.column_name
    parts = [head, f"所属表：{table.table_alias or table.table_name}"]
    if column.description:
        parts.append(column.description)
    if column.is_foreign_key and column.ref_table:
        parts.append(f"外键，关联 {column.ref_table}.{column.ref_column}")
    return "。".join(parts)


def metric_text(metric: MetricDef) -> str:
    """指标的向量化文本。

    口径（calculation / filter_condition）一定要带上：口语问法"卖了多少"
    与该指标的语义关联，主要靠描述和口径撑起来，光靠指标名匹配不上。
    """
    head = f"{metric.metric_alias}（{metric.metric_name}）" if metric.metric_alias else metric.metric_name
    parts = [head]
    if metric.description:
        parts.append(metric.description)
    if metric.calculation:
        parts.append(f"计算方式：{metric.calculation}")
    if metric.filter_condition:
        parts.append(f"过滤条件：{metric.filter_condition}")
    return "。".join(parts)


# --------------------------------------------------------------------------
# 各步骤
# --------------------------------------------------------------------------


def build_mysql_rows(loader: MetaConfigLoader) -> dict[str, list[dict]]:
    """把配置展开成四张知识表的待插入行。"""
    table_rows: list[dict] = []
    column_rows: list[dict] = []
    metric_rows: list[dict] = []
    link_rows: list[dict] = []

    for db, table in loader.tables():
        tid = table_id(db.db_name, table.table_name)
        table_rows.append(
            {
                "id": tid,
                "db_name": db.db_name,
                "table_name": table.table_name,
                "table_alias": table.table_alias,
                "description": table.description,
                "is_dimension": table.is_dimension,
            }
        )
        for column in table.columns:
            cid = column_id(db.db_name, table.table_name, column.column_name)
            ref_table_id = (
                table_id(db.db_name, column.ref_table)
                if column.is_foreign_key and column.ref_table
                else None
            )
            ref_column_id = (
                column_id(db.db_name, column.ref_table, column.ref_column)
                if column.is_foreign_key and column.ref_table and column.ref_column
                else None
            )
            column_rows.append(
                {
                    "id": cid,
                    "table_id": tid,
                    "column_name": column.column_name,
                    "column_type": column.column_type,
                    "column_alias": column.column_alias,
                    "description": column.description,
                    "is_primary_key": column.is_primary_key,
                    "is_foreign_key": column.is_foreign_key,
                    "ref_table_id": ref_table_id,
                    "ref_column_id": ref_column_id,
                }
            )

    for metric in loader.config.metrics:
        mid = metric_id(metric.metric_name)
        resolved = loader.resolve_metric_columns(metric)
        resolved_ids = [column_id(db, t, c) for db, t, c in resolved]
        metric_rows.append(
            {
                "id": mid,
                "metric_name": metric.metric_name,
                "metric_alias": metric.metric_alias,
                "description": metric.description,
                "calculation": metric.calculation,
                "filter_condition": metric.filter_condition,
                "time_range": metric.time_range,
                # Text 列存 id 列表，检索侧解析后回查 column_info
                "relevant_columns": ",".join(resolved_ids),
            }
        )
        for cid in resolved_ids:
            link_rows.append({"column_id": cid, "metric_id": mid})

    return {
        "table_info": table_rows,
        "column_info": column_rows,
        "metric_info": metric_rows,
        "column_metric": link_rows,
    }


async def write_mysql(rows: dict[str, list[dict]], *, embed_only: bool) -> dict[str, int]:
    """写 MySQL。``embed_only`` 时跳过（配置没变、只想重建向量时用）。"""
    factory = get_session_factory()
    async with factory() as session:
        repo = MetaRepository(session)
        if embed_only:
            return await repo.counts()
        removed = await repo.clear()
        await repo.insert_tables(rows["table_info"])
        await repo.insert_columns(rows["column_info"])
        await repo.insert_metrics(rows["metric_info"])
        await repo.insert_column_metrics(rows["column_metric"])
        # 清空与写入放在同一个事务里：中途失败则整体回滚，不会留下半套知识库
        await session.commit()
        counts = await repo.counts()
    print(
        f"[OK] MySQL 入库 table={counts['table_info']} column={counts['column_info']} "
        f"metric={counts['metric_info']} column_metric={counts['column_metric']}"
        f"（清理旧数据 {sum(removed.values())} 行）"
    )
    return counts


async def write_qdrant(loader: MetaConfigLoader) -> dict[str, int]:
    """字段与指标向量化后写入 Qdrant 两个集合。"""
    settings = get_settings()
    client = qdrant.get_qdrant()
    col_name = settings.qdrant.column_collection
    met_name = settings.qdrant.metric_collection

    await qdrant.ensure_collection(col_name, recreate=True)
    await qdrant.ensure_collection(met_name, recreate=True)

    # --- 字段 ---
    # 全部字段都进向量库（不只是 sync_values 的）：字段语义召回是主召回路径，
    # sync_values 控制的是「取值要不要进 ES」，两者是不同的事。
    points: list[qmodels.PointStruct] = []
    payloads: dict[str, dict] = {}
    texts: dict[str, str] = {}
    for db, table in loader.tables():
        for column in table.columns:
            cid = column_id(db.db_name, table.table_name, column.column_name)
            texts[cid] = column_text(db.db_name, table, column)
            payloads[cid] = {
                "db_name": db.db_name,
                "table_id": table_id(db.db_name, table.table_name),
                "table_name": table.table_name,
                "table_alias": table.table_alias,
                "column_name": column.column_name,
                "column_alias": column.column_alias,
                "column_type": column.column_type,
                "description": column.description,
                "is_dimension": table.is_dimension,
            }

    ids = list(texts)
    vectors = await embedding.embed_texts([texts[i] for i in ids])
    _check_dimension(vectors, col_name)
    for i, cid in enumerate(ids):
        points.append(qmodels.PointStruct(id=cid, vector=vectors[i], payload=payloads[cid]))
    for start in range(0, len(points), UPSERT_BATCH):
        await client.upsert(collection_name=col_name, points=points[start:start + UPSERT_BATCH])

    # --- 指标 ---
    m_points: list[qmodels.PointStruct] = []
    m_ids: list[str] = []
    m_texts: list[str] = []
    m_payloads: list[dict] = []
    for metric in loader.config.metrics:
        mid = metric_id(metric.metric_name)
        m_ids.append(mid)
        m_texts.append(metric_text(metric))
        m_payloads.append(
            {
                "metric_name": metric.metric_name,
                "metric_alias": metric.metric_alias,
                "description": metric.description,
                "calculation": metric.calculation,
                "filter_condition": metric.filter_condition,
                "time_range": metric.time_range,
                "relevant_columns": [
                    {"table": t, "column": c} for _db, t, c in loader.resolve_metric_columns(metric)
                ],
            }
        )
    m_vectors = await embedding.embed_texts(m_texts)
    _check_dimension(m_vectors, met_name)
    for i, mid in enumerate(m_ids):
        m_points.append(qmodels.PointStruct(id=mid, vector=m_vectors[i], payload=m_payloads[i]))
    for start in range(0, len(m_points), UPSERT_BATCH):
        await client.upsert(collection_name=met_name, points=m_points[start:start + UPSERT_BATCH])

    counts = {col_name: await qdrant.collection_count(col_name),
              met_name: await qdrant.collection_count(met_name)}
    print(f"[OK] Qdrant 向量入库 {col_name}={counts[col_name]} {met_name}={counts[met_name]}")
    return counts


def _check_dimension(vectors: list[list[float]], collection: str) -> None:
    """向量维度必须与集合配置一致，否则 upsert 会以晦涩的底层错误失败。"""
    if not vectors:
        return
    actual = len(vectors[0])
    expected = get_settings().qdrant.embedding_size
    if actual != expected:
        raise ValueError(
            f"Embedding 服务返回 {actual} 维向量，但 qdrant.embedding_size={expected}（集合 {collection}）。"
            f"请确认 embedding.model 与 embedding_size 配置匹配。"
        )


async def write_value_index(loader: MetaConfigLoader) -> int:
    """把需要同步的字段取值写入 ES 取值索引（ik 分词）。"""
    settings = get_settings()
    sync_columns = loader.sync_columns()
    await es.ensure_value_index(recreate=True)

    if not sync_columns:
        print("[WARN] 没有任何字段标记 sync_values，取值索引为空")
        return 0

    # 取值从 dw 只读账号读取：这是运行期查询用的账号，顺带验证它确实可用（ADR-008）
    dw = create_async_engine(settings.db_dw.url, pool_pre_ping=True)
    client = es.get_es()
    index = settings.es.index_name
    total = 0
    try:
        async with dw.connect() as conn:
            for db, table, column in sync_columns:
                # 列名来自本地配置文件而非用户输入，且用反引号包裹，不存在注入面
                sql = text(
                    f"SELECT DISTINCT `{column.column_name}` AS v FROM `{table.table_name}` "
                    f"WHERE `{column.column_name}` IS NOT NULL AND `{column.column_name}` <> '' "
                    f"LIMIT {VALUE_SAMPLE_LIMIT}"
                )
                values = [str(r.v) for r in (await conn.execute(sql)).all()]
                if not values:
                    print(f"[WARN] {table.table_name}.{column.column_name} 无取值可同步")
                    continue

                cid = column_id(db.db_name, table.table_name, column.column_name)
                ops: list[dict] = []
                for value in values:
                    ops.append({"index": {"_index": index, "_id": es.value_doc_id(cid, value)}})
                    ops.append(
                        {
                            "column_id": cid,
                            "db_name": db.db_name,
                            "table_name": table.table_name,
                            "column_name": column.column_name,
                            "column_alias": column.column_alias,
                            "value": value,
                        }
                    )
                resp = await client.bulk(operations=ops)
                if resp.get("errors"):
                    reasons = [
                        it["index"]["error"]["reason"]
                        for it in resp["items"]
                        if "error" in it.get("index", {})
                    ][:3]
                    raise RuntimeError(f"取值索引入库失败：{reasons}")
                total += len(values)
                print(f"[OK] 取值 {table.table_name}.{column.column_name}: {len(values)} 条")
    finally:
        await dw.dispose()

    # bulk 是近实时的，refresh 后才能立刻 count/search 到
    await client.indices.refresh(index=index)
    count = await es.index_count()
    print(f"[OK] ES 取值索引 {index} 共 {count} 篇文档")
    return count


async def self_check(loader: MetaConfigLoader, mysql_counts: dict[str, int]) -> None:
    """核对配置声明与实际入库是否一致。"""
    expected_tables = len(loader.tables())
    expected_columns = sum(len(t.columns) for _db, t in loader.tables())
    expected_metrics = len(loader.config.metrics)
    expected_links = sum(len(loader.resolve_metric_columns(m)) for m in loader.config.metrics)

    problems = []
    for name, actual, expected in (
        ("table_info", mysql_counts.get("table_info", -1), expected_tables),
        ("column_info", mysql_counts.get("column_info", -1), expected_columns),
        ("metric_info", mysql_counts.get("metric_info", -1), expected_metrics),
        ("column_metric", mysql_counts.get("column_metric", -1), expected_links),
    ):
        if actual != expected:
            problems.append(f"{name}: 期望 {expected} 行，实际 {actual} 行")
    if problems:
        raise RuntimeError("入库自检未通过：\n  - " + "\n  - ".join(problems))

    print(
        f"[OK] 自检通过：{expected_tables} 表 / {expected_columns} 字段 / "
        f"{expected_metrics} 指标 / {expected_links} 条指标-字段关联"
    )


# --------------------------------------------------------------------------
# 入口
# --------------------------------------------------------------------------


async def build(*, with_vector: bool, with_value_index: bool, embed_only: bool) -> None:
    loader = load_meta_config()
    print(
        f"[..] 配置已加载并通过校验："
        f"{len(loader.tables())} 表 / "
        f"{sum(len(t.columns) for _db, t in loader.tables())} 字段 / "
        f"{len(loader.config.metrics)} 指标 / "
        f"{len(loader.sync_columns())} 个字段需同步取值"
    )

    mysql_counts = await write_mysql(build_mysql_rows(loader), embed_only=embed_only)
    if with_vector:
        await write_qdrant(loader)
    if with_value_index:
        await write_value_index(loader)
    await self_check(loader, mysql_counts)


async def main_async(args: argparse.Namespace) -> int:
    await build(
        with_vector=not args.no_vector,
        with_value_index=not args.no_value_index,
        embed_only=args.embed_only,
    )
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="构建元数据知识库（阶段 4）")
    parser.add_argument("--no-vector", action="store_true", help="跳过 Qdrant 向量构建")
    parser.add_argument("--no-value-index", action="store_true", help="跳过 ES 取值索引构建")
    parser.add_argument("--embed-only", action="store_true", help="跳过 MySQL 写入，只重建向量")
    args = parser.parse_args(argv)

    async def run() -> int:
        try:
            return await main_async(args)
        finally:
            # 无论成败都释放连接池，避免脚本挂住不退出
            await dispose_engine()
            await qdrant.dispose_qdrant()
            await es.dispose_es()
            await embedding.dispose_client()

    return asyncio.run(run())


if __name__ == "__main__":
    sys.exit(main())
