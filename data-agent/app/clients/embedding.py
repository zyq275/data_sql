"""Embedding 独立服务客户端（ADR-005）。

应用内不做本地 CPU 推理，一律走 HTTP。协议按 OpenAI 兼容的 ``POST /embeddings``
实现，因此 Ollama / Xinference / TEI / 云厂商服务都能只改配置直接替换：

    {"model": "bge-m3", "input": ["文本一", "文本二"]}
      -> {"data": [{"index": 0, "embedding": [...]}, ...]}

注意：Ollama 的 ``/v1/embeddings`` 虽兼容 OpenAI，但 ``data`` 数组**不保证**按
输入顺序返回（并发实现所致），因此这里一律按 ``index`` 回填，不依赖返回顺序。
"""

from __future__ import annotations

import asyncio

import httpx

from app.core.config import get_settings
from app.core.exceptions import ServiceError

# 单次请求的文本条数。太小则往返开销大，太大则服务端易超时/OOM。
BATCH_SIZE = 16
MAX_RETRIES = 3
RETRY_BACKOFF_SECONDS = 1.5

_client: httpx.AsyncClient | None = None


def get_client() -> httpx.AsyncClient:
    """共享 HTTP 客户端（连接池复用，首次调用时创建）。"""
    global _client
    if _client is None:
        settings = get_settings()
        _client = httpx.AsyncClient(
            base_url=settings.embedding.host.rstrip("/"),
            timeout=httpx.Timeout(settings.embedding.timeout),
        )
    return _client


async def dispose_client() -> None:
    """释放连接池（应用停机时调用）。"""
    global _client
    if _client is not None:
        await _client.aclose()
    _client = None


async def embed_texts(texts: list[str], *, model: str | None = None) -> list[list[float]]:
    """把一批文本转成向量，返回顺序与入参严格一致。

    空串会被服务端拒绝或返回无意义向量，调用方应先过滤；此处对空串做兜底，
    用单个空格代替，保证返回条数始终等于入参条数。
    """
    if not texts:
        return []

    settings = get_settings()
    model = model or settings.embedding.model
    client = get_client()
    vectors: list[list[float]] = []

    for start in range(0, len(texts), BATCH_SIZE):
        batch = texts[start:start + BATCH_SIZE]
        payload = {"model": model, "input": [t if t.strip() else " " for t in batch]}

        last_error: Exception | None = None
        for attempt in range(MAX_RETRIES):
            try:
                resp = await client.post("/embeddings", json=payload)
                resp.raise_for_status()
                body = resp.json()
                break
            except httpx.HTTPStatusError as exc:
                # 4xx 是请求本身的问题（模型名错、超长），重试无意义
                if 400 <= exc.response.status_code < 500:
                    raise ServiceError(
                        f"Embedding 服务拒绝请求（HTTP {exc.response.status_code}）："
                        f"{exc.response.text[:200]}"
                    ) from exc
                last_error = exc
            except httpx.HTTPError as exc:
                last_error = exc
            if attempt < MAX_RETRIES - 1:
                await asyncio.sleep(RETRY_BACKOFF_SECONDS * (attempt + 1))
        else:
            raise ServiceError(
                f"Embedding 服务不可用（{settings.embedding.host}，重试 {MAX_RETRIES} 次）：{last_error}"
            )

        items = body.get("data") or []
        if len(items) != len(batch):
            raise ServiceError(
                f"Embedding 服务返回条数不符：请求 {len(batch)} 条，返回 {len(items)} 条"
            )
        # 按 index 回填，不信任返回顺序
        ordered = [None] * len(batch)
        for pos, item in enumerate(items):
            ordered[item.get("index", pos)] = item["embedding"]
        vectors.extend(ordered)

    return vectors


async def embed_text(text: str, *, model: str | None = None) -> list[float]:
    """单条文本向量化。"""
    return (await embed_texts([text], model=model))[0]
