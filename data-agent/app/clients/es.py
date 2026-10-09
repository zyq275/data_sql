"""Elasticsearch 客户端与索引管理（TDD 8.3）。

三个索引，各对应一类召回的第二路（阶段 4 建取值索引，阶段 6 补字段与指标）：

    data-agent-value          取值全文 —— 取值召回的**唯一**来源
    data-agent-column-text    字段全文 —— 与 Qdrant data-agent-column 融合
    data-agent-metric-text    指标全文 —— 与 Qdrant data-agent-metric 融合

字段/指标为什么也要有全文索引：单路稠密向量分不开近义词。TDD 8.3 记着实测结论——
问"销售额"时 bge-m3 稳定把 ``sales_quantity``(销量) 排在 ``GMV``(成交总额) 之前，
分差只有 0.02。而指标的 description 里写着"口语常称销售额、营收、卖了多少钱"，
BM25 对这种字面共现极其敏感，正好补上向量那一路的短板。

分词用 ik：索引期 ``ik_max_word``（尽量细，多切出可命中的词），
检索期 ``ik_smart``（粗粒度，避免把用户输入切碎导致误召回）。
ik 是第三方插件，未安装时建索引会失败——此处显式报错而不是退回内置分词器，
因为中文用 standard 分词器基本等于单字匹配，静默降级会让召回质量悄悄变差。

每个索引都存一份 ``text``——字段/指标索引里它就是**被向量化的那段文本原样**。
两条路因此看到的是同一份内容，融合与精排的依据一致；重排（交叉编码器）也从
这里取文档，不必在检索侧重新拼一遍文本、避免"排的依据和召的依据不是一回事"。
"""

from __future__ import annotations

import hashlib

from elasticsearch import AsyncElasticsearch, BadRequestError

from app.core.config import get_settings
from app.core.exceptions import ServiceError

_client: AsyncElasticsearch | None = None

# 三个索引共用的分析器设置：ik_max_word 索引、ik_smart 检索
ANALYSIS_SETTINGS: dict = {
    "number_of_shards": 1,
    "number_of_replicas": 0,
    "analysis": {
        "analyzer": {
            "ik_index_analyzer": {"type": "custom", "tokenizer": "ik_max_word"},
            "ik_search_analyzer": {"type": "custom", "tokenizer": "ik_smart"},
        }
    },
}

#: 需要中文分词的文本字段
IK_TEXT = {
    "type": "text",
    "analyzer": "ik_index_analyzer",
    "search_analyzer": "ik_search_analyzer",
}

#: 只随 _source 返回、不建倒排的文本字段（重排用得上，检索用不上）
STORED_TEXT = {"type": "text", "index": False}

VALUE_MAPPING: dict = {
    "settings": ANALYSIS_SETTINGS,
    "mappings": {
        "properties": {
            "column_id": {"type": "keyword"},
            "db_name": {"type": "keyword"},
            "table_name": {"type": "keyword"},
            "column_name": {"type": "keyword"},
            "column_alias": IK_TEXT,
            "value": {
                **IK_TEXT,
                # raw 子字段做精确匹配：用户问"广东省"时不应该被 ik 切碎后模糊命中
                "fields": {"raw": {"type": "keyword", "ignore_above": 256}},
            },
            # 重排用的文档文本；取值本身很短，检索仍走 value 字段
            "text": STORED_TEXT,
        }
    },
}

COLUMN_MAPPING: dict = {
    "settings": ANALYSIS_SETTINGS,
    "mappings": {
        "properties": {
            "column_id": {"type": "keyword"},
            "db_name": {"type": "keyword"},
            "table_name": {"type": "keyword"},
            "table_alias": {"type": "keyword"},
            "column_name": {"type": "keyword"},
            "column_alias": {"type": "keyword"},
            "description": STORED_TEXT,
            # 全文检索走这里：内容与 Qdrant 里被向量化的文本逐字一致
            "text": IK_TEXT,
        }
    },
}

METRIC_MAPPING: dict = {
    "settings": ANALYSIS_SETTINGS,
    "mappings": {
        "properties": {
            "metric_id": {"type": "keyword"},
            "metric_name": {"type": "keyword"},
            "metric_alias": {"type": "keyword"},
            "description": STORED_TEXT,
            "text": IK_TEXT,
        }
    },
}


def get_es() -> AsyncElasticsearch:
    """ES 客户端（首次调用时创建）。"""
    global _client
    if _client is None:
        es = get_settings().es
        auth = (es.username, es.password) if es.username else None
        _client = AsyncElasticsearch(
            hosts=[es.url],
            basic_auth=auth,
            request_timeout=30,
        )
    return _client


async def dispose_es() -> None:
    """关闭客户端（应用停机时调用）。"""
    global _client
    if _client is not None:
        await _client.close()
    _client = None


def value_doc_id(column_id: str, value: str) -> str:
    """取值文档的确定性 id。

    同一 (字段, 取值) 反复构建落到同一个文档，重建知识库不会产生重复；
    取值可能很长，用 md5 压到 32 字符，避免超出 ES 的 _id 长度上限。
    """
    return hashlib.md5(f"{column_id}|{value}".encode()).hexdigest()


def value_text(column_name: str, column_alias: str | None, value: str) -> str:
    """取值的重排文档文本。

    只给一个"华东"太单薄：重排要判断的是"这个取值与问句是否相关"，
    带上它属于哪个字段（"大区：华东"）才有足够信息与问句里的其他词竞争。
    """
    head = f"{column_alias}（{column_name}）" if column_alias else column_name
    return f"{head}：{value}"


async def ensure_index(
    index: str, mapping: dict, *, recreate: bool = False, what: str = "索引"
) -> None:
    """建索引（已存在则跳过；``recreate=True`` 时先删后建）。"""
    client = get_es()
    if await client.indices.exists(index=index):
        if not recreate:
            return
        await client.indices.delete(index=index)

    try:
        await client.indices.create(index=index, **mapping)
    except BadRequestError as exc:
        reason = str(getattr(exc, "body", None) or {})
        if "ik_max_word" in reason or "ik_smart" in reason:
            raise ServiceError(
                f"ES 缺少 ik 分词插件，无法创建{what} {index}。"
                "请把对应版本的 analysis-ik 解压到 ES 的 plugins/ 目录后重启 ES"
                "（版本必须与 ES 完全一致，否则插件加载失败）。"
            ) from exc
        raise


async def ensure_value_index(*, recreate: bool = False) -> None:
    """建取值索引。"""
    await ensure_index(
        get_settings().es.value_index, VALUE_MAPPING, recreate=recreate, what="取值索引"
    )


async def ensure_column_index(*, recreate: bool = False) -> None:
    """建字段全文索引。"""
    await ensure_index(
        get_settings().es.column_index, COLUMN_MAPPING, recreate=recreate, what="字段索引"
    )


async def ensure_metric_index(*, recreate: bool = False) -> None:
    """建指标全文索引。"""
    await ensure_index(
        get_settings().es.metric_index, METRIC_MAPPING, recreate=recreate, what="指标索引"
    )


async def bulk_write(index: str, operations: list[dict]) -> None:
    """批量写文档。

    用 ES 的 ``bulk`` 而不是逐条 ``index``：字段/指标各只有几十条，但取值索引
    动辄上千条（每个同步字段最多 500 个取值），逐条往返的耗时是数量级差异。
    bulk 是近实时的，写完要 ``refresh`` 才能立刻搜到。
    """
    if not operations:
        return
    resp = await get_es().bulk(operations=operations)
    if resp.get("errors"):
        reasons = [
            item["index"]["error"]["reason"]
            for item in resp["items"]
            if "error" in item.get("index", {})
        ][:3]
        raise ServiceError(f"ES 索引入库失败（{index}）：{reasons}")


async def refresh(index: str) -> None:
    """让刚写入的文档立刻可搜。"""
    await get_es().indices.refresh(index=index)


async def index_count(index: str | None = None) -> int:
    """索引内文档数，用于构建后自检。默认取值索引（阶段 4 起的既有调用点）。"""
    index = index or get_settings().es.value_index
    client = get_es()
    if not await client.indices.exists(index=index):
        return 0
    return (await client.count(index=index))["count"]
