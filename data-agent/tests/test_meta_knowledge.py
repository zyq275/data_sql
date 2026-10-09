"""阶段 4 验收：元数据知识库构建（《开发步骤文档》阶段 4 的四条验收标准）。

  1. 表/字段/指标完整入库
  2. 字段与指标向量索引建立
  3. 字段取值全文索引建立
  4. 召回能命中对应项

入库一项不读构建脚本的输出，而是直接查 information_schema 与四张知识表，
拿到的行数与 meta_config.yaml 的声明逐项比对——脚本说"成功"不算数，
存储里真的有才算。

整个模块标记 requires_stack：外部服务没起时跳过（见 conftest.py）。
跳过只针对"服务没监听"；服务在跑但数据不对，必须失败。
"""

from __future__ import annotations

from collections.abc import AsyncIterator

import pytest
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncEngine, create_async_engine

from app.clients import embedding, es, qdrant
from app.core.config import get_settings
from app.entities.meta_config import MetaConfigLoader, load_meta_config
from app.scripts.build_meta import VALUE_SAMPLE_LIMIT

pytestmark = pytest.mark.requires_stack

WRITE_PROBE_TABLE = "_pytest_write_probe"


@pytest.fixture(scope="session")
def loader() -> MetaConfigLoader:
    return load_meta_config()


@pytest.fixture(scope="session")
def settings():
    return get_settings()


@pytest.fixture(scope="session")
async def meta_engine(settings) -> AsyncIterator[AsyncEngine]:
    engine = create_async_engine(settings.db_meta.url, pool_pre_ping=True)
    yield engine
    await engine.dispose()


@pytest.fixture(scope="session")
async def dw_engine(settings) -> AsyncIterator[AsyncEngine]:
    engine = create_async_engine(settings.db_dw.url, pool_pre_ping=True)
    yield engine
    await engine.dispose()


@pytest.fixture(scope="session")
def qdrant_client():
    return qdrant.get_qdrant()


@pytest.fixture(scope="session")
def es_client():
    return es.get_es()


@pytest.fixture(scope="session")
def expected(loader: MetaConfigLoader) -> dict[str, int]:
    return {
        "tables": len(loader.tables()),
        "columns": sum(len(table.columns) for _db, table in loader.tables()),
        "metrics": len(loader.config.metrics),
        "links": sum(
            len(loader.resolve_metric_columns(metric)) for metric in loader.config.metrics
        ),
    }


# ==========================================================================
# 验收标准 1：表/字段/指标完整入库
# ==========================================================================


async def _count(engine: AsyncEngine, table: str) -> int:
    async with engine.connect() as conn:
        return (await conn.execute(text(f"SELECT COUNT(*) FROM {table}"))).scalar_one()


@pytest.mark.parametrize(
    ("table", "expected_key"),
    [("table_info", "tables"), ("column_info", "columns"), ("metric_info", "metrics")],
)
async def test_knowledge_table_row_count_matches_config(
    meta_engine: AsyncEngine, expected: dict[str, int], table: str, expected_key: str
) -> None:
    actual = await _count(meta_engine, table)
    assert actual == expected[expected_key], (
        f"{table} 有 {actual} 行，配置声明 {expected[expected_key]}"
    )


async def test_column_metric_link_count_matches_resolved_config(
    meta_engine: AsyncEngine, expected: dict[str, int]
) -> None:
    actual = await _count(meta_engine, "column_metric")
    assert actual == expected["links"], (
        f"column_metric 有 {actual} 条，指标相关字段解析出 {expected['links']} 条"
    )


async def test_every_declared_column_is_stored_with_matching_identity(
    meta_engine: AsyncEngine, loader: MetaConfigLoader
) -> None:
    """不只看行数：逐个字段核对表名、类型、主键标记是否真的落库。"""
    async with meta_engine.connect() as conn:
        rows = (
            await conn.execute(
                text(
                    "SELECT t.table_name, c.column_name, c.column_type, c.is_primary_key"
                    " FROM column_info c JOIN table_info t ON t.id = c.table_id"
                )
            )
        ).all()
    stored = {(r.table_name, r.column_name): r for r in rows}

    missing: list[str] = []
    mismatched: list[str] = []
    for _db, table, column in [(db, t, c) for db, t in loader.tables() for c in t.columns]:
        row = stored.get((table.table_name, column.column_name))
        if row is None:
            missing.append(f"{table.table_name}.{column.column_name}")
            continue
        if row.column_type != column.column_type:
            mismatched.append(f"{table.table_name}.{column.column_name} 类型")
        if bool(row.is_primary_key) != column.is_primary_key:
            mismatched.append(f"{table.table_name}.{column.column_name} 主键标记")

    assert not missing, f"配置里有但库里没有的字段：{missing}"
    assert not mismatched, f"落库内容与配置不一致：{mismatched}"


async def test_foreign_keys_resolve_to_existing_ids(
    meta_engine: AsyncEngine, loader: MetaConfigLoader
) -> None:
    """外键必须真的解析成 id 指向，否则召回后补不齐关联（BR-META-001）。"""
    declared = sum(1 for _db, table in loader.tables() for c in table.columns if c.is_foreign_key)

    async with meta_engine.connect() as conn:
        foreign_keys = (
            await conn.execute(
                text(
                    "SELECT c.ref_table_id, c.ref_column_id FROM column_info c"
                    " WHERE c.is_foreign_key = 1"
                )
            )
        ).all()
        table_ids = {r[0] for r in (await conn.execute(text("SELECT id FROM table_info"))).all()}
        column_ids = {r[0] for r in (await conn.execute(text("SELECT id FROM column_info"))).all()}

    assert len(foreign_keys) == declared, (
        f"库里有 {len(foreign_keys)} 个外键，配置声明 {declared} 个"
    )
    assert foreign_keys, "配置里连一个外键都没有，知识库关联能力等于没有"
    for ref_table_id, ref_column_id in foreign_keys:
        assert ref_table_id in table_ids, "外键指向的表 id 悬空"
        assert ref_column_id in column_ids, "外键指向的字段 id 悬空"


async def test_every_metric_has_a_calculation(meta_engine: AsyncEngine) -> None:
    """指标口径必须完整（BR-META-002）：只有名字没有算法，生成的 SQL 就是猜的。"""
    async with meta_engine.connect() as conn:
        empty = (
            await conn.execute(
                text(
                    "SELECT metric_name FROM metric_info"
                    " WHERE calculation IS NULL OR calculation = ''"
                )
            )
        ).all()
    assert not empty, f"缺口径的指标：{[r[0] for r in empty]}"


# ==========================================================================
# 验收标准 2：字段与指标向量索引建立
# ==========================================================================


async def test_qdrant_collection_sizes_match_config(
    qdrant_client, settings, expected: dict[str, int]
) -> None:
    column_count = await qdrant.collection_count(settings.qdrant.column_collection)
    metric_count = await qdrant.collection_count(settings.qdrant.metric_collection)

    assert column_count == expected["columns"], (
        f"{settings.qdrant.column_collection} 有 {column_count} 个点，应为 {expected['columns']}"
    )
    assert metric_count == expected["metrics"], (
        f"{settings.qdrant.metric_collection} 有 {metric_count} 个点，应为 {expected['metrics']}"
    )


async def test_qdrant_vector_dimension_matches_config(qdrant_client, settings) -> None:
    info = await qdrant_client.get_collection(settings.qdrant.column_collection)
    size = info.config.params.vectors.size
    assert size == settings.qdrant.embedding_size, (
        f"集合维度 {size} 与配置 embedding_size={settings.qdrant.embedding_size} 不符——"
        "换过 embedding 模型但没重建集合时会这样"
    )


async def test_qdrant_point_ids_equal_mysql_primary_keys(
    qdrant_client, meta_engine: AsyncEngine, settings
) -> None:
    """点 id 与 MySQL 主键一致，是"召回后无需 id 映射即可回查元数据"的前提。"""
    async with meta_engine.connect() as conn:
        mysql_ids = {
            r[0] for r in (await conn.execute(text("SELECT id FROM column_info"))).all()
        }

    points, _ = await qdrant_client.scroll(
        collection_name=settings.qdrant.column_collection, limit=10_000, with_payload=False
    )
    qdrant_ids = {str(p.id) for p in points}

    assert qdrant_ids == mysql_ids, (
        f"仅 Qdrant 有：{qdrant_ids - mysql_ids}；仅 MySQL 有：{mysql_ids - qdrant_ids}"
    )


async def test_qdrant_vectors_are_not_degenerate(qdrant_client, settings) -> None:
    """抽查一次真实检索：分数要落在合理区间，且排序有区分度。

    全零向量或未归一化的占位向量能骗过所有"点数对不对"的检查，
    只有真检索一次才看得出来。
    """
    vector = await embedding.embed_text("订单金额")
    hits = (
        await qdrant_client.query_points(
            collection_name=settings.qdrant.column_collection, query=vector, limit=2
        )
    ).points

    assert len(hits) == 2
    assert 0.0 < hits[0].score <= 1.0, f"top1 分数异常：{hits[0].score}"
    assert hits[0].score > hits[1].score, "前两名分数相同，排序没有区分度"


# ==========================================================================
# 验收标准 3：字段取值全文索引建立
# ==========================================================================


async def test_value_index_exists(es_client, settings) -> None:
    assert await es_client.indices.exists(index=settings.es.value_index)


async def test_value_index_document_count_matches_distinct_values(
    es_client, dw_engine: AsyncEngine, loader: MetaConfigLoader
) -> None:
    total = 0
    async with dw_engine.connect() as conn:
        for _db, table, column in loader.sync_columns():
            distinct = (
                await conn.execute(
                    text(f"SELECT COUNT(DISTINCT `{column.column_name}`) FROM `{table.table_name}`")
                )
            ).scalar_one()
            total += min(distinct, VALUE_SAMPLE_LIMIT)

    actual = await es.index_count()
    assert actual == total, (
        f"索引 {actual} 篇文档，库中去重取值累计 {total} 条"
        f"（{len(loader.sync_columns())} 个字段，每个最多取 {VALUE_SAMPLE_LIMIT} 条）"
    )


async def test_ik_analyzer_tokenizes_chinese(es_client, settings) -> None:
    """ik 是否真的生效：中文应被切成词，而不是逐字拆开。

    用"ik 切出的词数明显少于 standard"来判定，比硬编码期望词表稳：
    ik 的词典版本会变，但"比逐字切分粗"这一点不会变。
    """
    probe = "华东区销售额"
    ik = await es_client.indices.analyze(index=settings.es.value_index, analyzer="ik_smart", text=probe)
    standard = await es_client.indices.analyze(
        index=settings.es.value_index, analyzer="standard", text=probe
    )
    ik_tokens = [t["token"] for t in ik["tokens"]]
    standard_tokens = [t["token"] for t in standard["tokens"]]

    assert len(ik_tokens) < len(standard_tokens), (
        f"ik_smart 切出 {ik_tokens}，standard 切出 {standard_tokens}——"
        "数量相同说明 ik 没生效，退回了内置分词器"
    )
    assert any(len(token) > 1 for token in ik_tokens), f"ik 全是单字：{ik_tokens}"


async def test_value_field_has_raw_keyword_subfield(es_client, settings) -> None:
    """value 带 raw 子字段才能做精确匹配，避免"广东省"被切碎后模糊命中。"""
    mapping = await es_client.indices.get_mapping(index=settings.es.value_index)
    properties = mapping[settings.es.value_index]["mappings"]["properties"]

    assert properties["value"]["fields"]["raw"]["type"] == "keyword"


# ==========================================================================
# 验收标准 3b：字段 / 指标的全文索引（阶段 6 的混合检索第二路）
#
# 阶段 4 只建了取值索引；阶段 6 的"两路召回"要求字段与指标也有全文这一路，
# 所以构建脚本把它们一起建了，验收也一并落在这里——索引是构建的产物，
# 它的完整性属于阶段 4 那份契约的延伸，不该等检索测试去发现"少了半个索引"。
# ==========================================================================


async def test_column_fulltext_index_covers_every_column(
    es_client, settings, expected: dict[str, int]
) -> None:
    assert await es_client.indices.exists(index=settings.es.column_index)
    actual = await es.index_count(settings.es.column_index)
    assert actual == expected["columns"], (
        f"字段全文索引有 {actual} 篇，配置声明 {expected['columns']} 个字段——"
        "少一篇就意味着那个字段只剩向量一路可召回"
    )


async def test_metric_fulltext_index_covers_every_metric(
    es_client, settings, expected: dict[str, int]
) -> None:
    assert await es_client.indices.exists(index=settings.es.metric_index)
    actual = await es.index_count(settings.es.metric_index)
    assert actual == expected["metrics"], (
        f"指标全文索引有 {actual} 篇，配置声明 {expected['metrics']} 个指标"
    )


async def test_both_paths_see_the_identical_text(
    es_client, qdrant_client, settings
) -> None:
    """两条路必须看到**逐字相同**的文本。

    向量那一路把 ``text`` 向量化，全文那一路把同一段 ``text`` 做 ik 索引；
    它们一旦分叉（比如只改了一边的拼装逻辑），融合与精排的依据就不再是同一份
    内容，名次会对不上而**没有任何报错**——这是最难查的一类问题，所以钉死它。
    """
    column_id = (await es_client.search(index=settings.es.column_index, size=1))["hits"][
        "hits"
    ][0]["_source"]["column_id"]

    es_doc = (await es_client.get(index=settings.es.column_index, id=column_id))["_source"]
    points = (
        await qdrant_client.retrieve(
            collection_name=settings.qdrant.column_collection,
            ids=[column_id],
            with_payload=True,
        )
    )
    assert points, f"Qdrant 字段集合里没有 {column_id}——两路的 id 必须一一对应"

    assert es_doc["text"] == points[0].payload["text"], (
        f"字段 {es_doc['column_name']} 的两路文本不一致：\n"
        f"  ES    : {es_doc['text']}\n"
        f"  Qdrant: {points[0].payload['text']}"
    )


COLUMN_TEXT_CASES = [
    ("订单金额", "order_amount"),
    ("省份", "province"),
    ("商品类目", "category"),
]

METRIC_TEXT_CASES = [
    # 口语词："销售额"只出现在 GMV 的描述里，全文这一路必须把它挑出来。
    # 这正是单路向量做不到的那件事（TDD 8.3 记录的已知限制）。
    ("销售额", "GMV"),
    ("订单量", "order_count"),
    ("活跃客户", "active_customer_count"),
]


@pytest.mark.parametrize(("keyword", "expected_column"), COLUMN_TEXT_CASES)
async def test_column_fulltext_recall_hits_expected_column(
    es_client, settings, keyword: str, expected_column: str
) -> None:
    resp = await es_client.search(
        index=settings.es.column_index, query={"match": {"text": keyword}}, size=5
    )
    names = [hit["_source"]["column_name"] for hit in resp["hits"]["hits"]]
    assert expected_column in names, f"字段全文检索「{keyword}」命中 {names}，期望含 {expected_column}"


@pytest.mark.parametrize(("keyword", "expected_metric"), METRIC_TEXT_CASES)
async def test_metric_fulltext_recall_hits_expected_metric(
    es_client, settings, keyword: str, expected_metric: str
) -> None:
    resp = await es_client.search(
        index=settings.es.metric_index, query={"match": {"text": keyword}}, size=3
    )
    names = [hit["_source"]["metric_name"] for hit in resp["hits"]["hits"]]
    assert names and names[0] == expected_metric, (
        f"指标全文检索「{keyword}」top3={names}，期望 top1={expected_metric}"
    )


async def test_dw_readonly_account_cannot_write(
    dw_engine: AsyncEngine, meta_engine: AsyncEngine, settings
) -> None:
    """ADR-008 / NFR-SEC-007：写操作在机制上不可能发生，不能只靠代码自觉。"""
    try:
        async with dw_engine.begin() as conn:
            await conn.execute(text(f"CREATE TABLE {WRITE_PROBE_TABLE} (id INT)"))
    except Exception as exc:  # noqa: BLE001 —— 任何失败都是期望结果
        assert "1142" in str(exc) or "denied" in str(exc).lower(), (
            f"建表失败的原因不是权限不足，需要确认：{exc!r}"
        )
        return

    # 只读账号竟然建表成功了——先把痕迹清掉再用测试失败报出来
    async with meta_engine.begin() as conn:
        await conn.execute(
            text(f"DROP TABLE IF EXISTS {settings.db_dw.database}.{WRITE_PROBE_TABLE}")
        )
    pytest.fail(
        f"db_dw 账号 {settings.db_dw.user} 竟然能在 {settings.db_dw.database} 建表，"
        "违反 ADR-008 的最小权限要求（已清理探针表）"
    )


# ==========================================================================
# 验收标准 4：召回能命中对应项
#
# 口径是召回@k，不是 top-1 精度：阶段 4 交付的是索引，能保证正确项进入候选集；
# 单路稠密向量分不开近义词（实测"销售额"会被排到"销量"之前），精排是阶段 6 的
# RRF + rerank 的职责。详见 TDD 8.3 的"已知限制"。
# ==========================================================================

FIELD_CASES = [
    ("订单卖了多少钱", "order_amount"),
    ("哪个省", "province"),
    ("商品属于什么类目", "category"),
    ("客户姓名", "customer_name"),
    ("会员等级", "member_level"),
    ("下单日期", "date_id"),
    ("买了多少件", "order_quantity"),
]

METRIC_CASES = [
    ("一共卖了多少件", "sales_quantity"),
    ("平均每单花多少钱", "avg_order_amount"),
    ("有多少活跃用户", "active_customer_count"),
    ("成交总额", "GMV"),
    ("订单量", "order_count"),
]

VALUE_CASES = [
    ("华东", "region_name"),
    ("浙江省", "province"),
    ("金卡", "member_level"),
    ("手机数码", "category"),
    ("已支付", "order_status"),
]


async def _top_column_names(qdrant_client, settings, question: str, limit: int) -> list[str]:
    vector = await embedding.embed_text(question)
    hits = (
        await qdrant_client.query_points(
            collection_name=settings.qdrant.column_collection, query=vector, limit=limit
        )
    ).points
    return [hit.payload["column_name"] for hit in hits]


@pytest.mark.parametrize(("question", "expected_column"), FIELD_CASES)
async def test_field_vector_recall_hits_expected_column(
    qdrant_client, settings, question: str, expected_column: str
) -> None:
    names = await _top_column_names(qdrant_client, settings, question, limit=3)
    assert names[0] == expected_column, f"「{question}」top3={names}，期望 top1={expected_column}"


@pytest.mark.parametrize(("question", "expected_metric"), METRIC_CASES)
async def test_metric_vector_recall_hits_expected_metric(
    qdrant_client, settings, question: str, expected_metric: str
) -> None:
    vector = await embedding.embed_text(question)
    hits = (
        await qdrant_client.query_points(
            collection_name=settings.qdrant.metric_collection, query=vector, limit=3
        )
    ).points
    names = [hit.payload["metric_name"] for hit in hits]
    assert names[0] == expected_metric, f"「{question}」top3={names}，期望 top1={expected_metric}"


@pytest.mark.parametrize(("keyword", "expected_column"), VALUE_CASES)
async def test_value_fulltext_recall_finds_owning_column(
    es_client, settings, keyword: str, expected_column: str
) -> None:
    resp = await es_client.search(
        index=settings.es.value_index, query={"match": {"value": keyword}}, size=5
    )
    columns = [hit["_source"]["column_name"] for hit in resp["hits"]["hits"]]
    assert expected_column in columns, f"取值「{keyword}」命中字段 {columns}，期望含 {expected_column}"


async def test_end_to_end_recall_covers_metric_field_and_value(
    qdrant_client, es_client, settings
) -> None:
    """一个真实问法应该同时召回指标、字段与取值三路的结果。"""
    question = "去年华东区销售额前三的省份"
    vector = await embedding.embed_text(question)

    metric_hits = (
        await qdrant_client.query_points(
            collection_name=settings.qdrant.metric_collection, query=vector, limit=2
        )
    ).points
    column_hits = await _top_column_names(qdrant_client, settings, question, limit=3)
    value_hits = await es_client.search(
        index=settings.es.value_index, query={"match": {"value": "华东"}}, size=1
    )

    metric_names = [hit.payload["metric_name"] for hit in metric_hits]
    assert "GMV" in metric_names, (
        f"「{question}」的指标候选集 {metric_names} 里没有 GMV——"
        "阶段 4 只保证召回@k，但正确项必须进候选集，否则阶段 6 的 rerank 无从补救"
    )
    assert column_hits[0] == "region_name", f"字段 top3={column_hits}，期望 region_name 居首"

    top_value = value_hits["hits"]["hits"][0]["_source"]
    assert top_value["value"] == "华东"
    assert top_value["column_name"] == "region_name"
