"""Qdrant 向量库连接与集合管理（TDD 8.3）。

两种模式（见 QdrantSettings）：
  - server：连远程 Qdrant 服务，容器编排下的标准形态（TDD 14.1）；
  - local ：用 qdrant-client 自带的嵌入式引擎，数据落本地目录，不开独立进程。
            仅用于没有 Qdrant 服务的本地开发，API 与 server 模式完全一致。

集合：
  data-agent-column  字段向量（column_info）
  data-agent-metric  指标向量（metric_info）

点 id 直接用 MySQL 中的 36 位 UUID 主键，两个存储一一对应，
召回结果无需再做 id 映射即可回查元数据。
"""

from __future__ import annotations

from qdrant_client import AsyncQdrantClient, models

from app.core.config import get_settings

_client: AsyncQdrantClient | None = None


def get_qdrant() -> AsyncQdrantClient:
    """向量库客户端（首次调用时创建）。"""
    global _client
    if _client is None:
        settings = get_settings()
        q = settings.qdrant
        if q.is_local:
            if not q.local_path:
                raise ValueError("qdrant.mode=local 时必须配置 qdrant.local_path")
            _client = AsyncQdrantClient(path=q.local_path)
        else:
            _client = AsyncQdrantClient(host=q.host, port=q.port)
    return _client


async def dispose_qdrant() -> None:
    """关闭客户端（应用停机时调用）。"""
    global _client
    if _client is not None:
        await _client.close()
    _client = None


async def ensure_collection(name: str, *, recreate: bool = False) -> None:
    """建集合（不存在则建；``recreate=True`` 时先删后建，用于全量重建）。

    距离用余弦：bge-m3 的输出适合归一化后比角度，余弦对向量模长不敏感。
    同时给常用过滤字段建 payload 索引，否则按表/指标过滤会退化成全表扫描。
    """
    settings = get_settings()
    client = get_qdrant()

    exists = await client.collection_exists(name)
    if exists and recreate:
        await client.delete_collection(name)
        exists = False
    if exists:
        return

    await client.create_collection(
        collection_name=name,
        vectors_config=models.VectorParams(
            size=settings.qdrant.embedding_size,
            distance=models.Distance.COSINE,
        ),
    )

    # 嵌入式模式不支持 payload 索引（建了也不生效，qdrant-client 会告警）。
    # 只影响按字段过滤的查询性能，过滤语义本身一致，本地开发够用。
    if settings.qdrant.is_local:
        return

    for field in ("db_name", "table_name", "column_name", "metric_name"):
        await client.create_payload_index(
            collection_name=name,
            field_name=field,
            field_schema=models.PayloadSchemaType.KEYWORD,
        )


async def collection_count(name: str) -> int:
    """集合内点数，用于构建后自检。"""
    client = get_qdrant()
    if not await client.collection_exists(name):
        return 0
    return (await client.count(collection_name=name, exact=True)).count
