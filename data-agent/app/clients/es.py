"""Elasticsearch 客户端与取值索引管理（TDD 8.3 的 data-agent-value）。

索引存放「需要同步的字段取值」，供取值精确召回（ADR-002 的第三路）：
问题里出现"华东""金卡"这类取值时，靠全文检索定位到它属于哪个字段。

分词用 ik：索引期 ``ik_max_word``（尽量细，多切出可命中的词），
检索期 ``ik_smart``（粗粒度，避免把用户输入切碎导致误召回）。
ik 是第三方插件，未安装时建索引会失败——此处显式报错而不是退回内置分词器，
因为中文用 standard 分词器基本等于单字匹配，静默降级会让召回质量悄悄变差。
"""

from __future__ import annotations

import hashlib

from elasticsearch import AsyncElasticsearch, BadRequestError

from app.core.config import get_settings
from app.core.exceptions import ServiceError

_client: AsyncElasticsearch | None = None

# 取值字段的 mapping。列名/别名与取值都按中文检索需求配 ik。
INDEX_MAPPING: dict = {
    "settings": {
        "number_of_shards": 1,
        "number_of_replicas": 0,
        "analysis": {
            "analyzer": {
                "ik_index_analyzer": {"type": "custom", "tokenizer": "ik_max_word"},
                "ik_search_analyzer": {"type": "custom", "tokenizer": "ik_smart"},
            }
        },
    },
    "mappings": {
        "properties": {
            "column_id": {"type": "keyword"},
            "db_name": {"type": "keyword"},
            "table_name": {"type": "keyword"},
            "column_name": {"type": "keyword"},
            "column_alias": {
                "type": "text",
                "analyzer": "ik_index_analyzer",
                "search_analyzer": "ik_search_analyzer",
            },
            "value": {
                "type": "text",
                "analyzer": "ik_index_analyzer",
                "search_analyzer": "ik_search_analyzer",
                # raw 子字段做精确匹配：用户问"广东省"时不应该被 ik 切碎后模糊命中
                "fields": {"raw": {"type": "keyword", "ignore_above": 256}},
            },
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


async def ensure_value_index(*, recreate: bool = False) -> None:
    """建取值索引（不存在则建；``recreate=True`` 时先删后建）。"""
    settings = get_settings()
    index = settings.es.index_name
    client = get_es()

    if await client.indices.exists(index=index):
        if not recreate:
            return
        await client.indices.delete(index=index)

    try:
        await client.indices.create(index=index, **INDEX_MAPPING)
    except BadRequestError as exc:
        body = getattr(exc, "body", None) or {}
        reason = str(body)
        if "ik_max_word" in reason or "ik_smart" in reason:
            raise ServiceError(
                f"ES 缺少 ik 分词插件，无法创建索引 {index}。"
                "请把对应版本的 analysis-ik 解压到 ES 的 plugins/ 目录后重启 ES"
                "（版本必须与 ES 完全一致，否则插件加载失败）。"
            ) from exc
        raise


async def index_count() -> int:
    """索引内文档数，用于构建后自检。"""
    settings = get_settings()
    client = get_es()
    if not await client.indices.exists(index=settings.es.index_name):
        return 0
    return (await client.count(index=settings.es.index_name))["count"]
