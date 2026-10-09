"""Rerank 独立服务客户端（ADR-005 / ADR-007）。

重排用交叉编码器：把"问句 + 候选文档"拼在一起过一次模型，直接输出相关度。
它与双塔向量（问句与文档分别编码、只比向量夹角）的本质差别在于**能看见两者的
交互**，所以对"销售额 / 销量"这类近义干扰的判别力远强于向量相似度——这正是
TDD 8.3 记下的那个阶段 4 遗留问题（单路向量把销量排在成交总额之前）的解法。

重排不属于 OpenAI 兼容协议的一部分，各家形状不一，这里一并吃掉：

    TEI / Xinference / vLLM :  POST {host}{path}  ->  [{"index": 0, "score": 0.93}, ...]
    SiliconFlow / Jina 等   :  POST {host}{path}  ->  {"results": [{"index": 0,
                                                        "relevance_score": 0.93}, ...]}

请求体统一用 ``{"model", "query", "documents", "top_n"}``（两层协议都接受），
响应按上面两种形状归一化。返回结果**按分数降序**，且同分时按下标升序——
保留确定性的次序，否则下游的名次会随服务端的返回顺序漂移。
"""

from __future__ import annotations

import asyncio
from collections.abc import Sequence
from dataclasses import dataclass

import httpx

from app.core.config import get_settings
from app.core.exceptions import ServiceError

# 重排在问答链路的必经路径上，重试次数比 embedding 更保守：
# 服务不可用时，用户宁可拿到"融合顺序的召回"，也不要多等 4 秒再拿到同一份结果。
MAX_RETRIES = 2
RETRY_BACKOFF_SECONDS = 0.5

_client: httpx.AsyncClient | None = None


@dataclass(slots=True)
class RankedDocument:
    """一条精排结果：``index`` 是入参 ``documents`` 中的下标，不是文档 id。

    用下标而不是 id 回指，是因为调用方给的文档可能根本不是实体（比如两路融合后
    临时拼出来的文本），外部服务无从知道它们的标识；下标是双方唯一确定的共同语言。
    """

    index: int
    score: float


def get_client() -> httpx.AsyncClient:
    """共享 HTTP 客户端（连接池复用，首次调用时创建）。"""
    global _client
    if _client is None:
        settings = get_settings().rerank
        headers = {"Authorization": f"Bearer {settings.api_key}"} if settings.api_key else None
        _client = httpx.AsyncClient(
            base_url=settings.host.rstrip("/"),
            timeout=httpx.Timeout(settings.timeout),
            headers=headers,
        )
    return _client


async def dispose_client() -> None:
    """释放连接池（应用停机时调用）。"""
    global _client
    if _client is not None:
        await _client.aclose()
    _client = None


async def rerank(
    query: str, documents: Sequence[str], *, top_n: int | None = None
) -> list[RankedDocument]:
    """按与 ``query`` 的相关度重排 ``documents``。

    ``documents`` 为空或问句为空时直接返回空列表而不发请求：空查询在多数服务端
    会 400，而"没有候选可排"在调用方看来等价于"不需要精排"，不该升级成故障。
    """
    if not documents or not query.strip():
        return []

    settings = get_settings().rerank
    payload: dict = {
        "model": settings.model,
        "query": query,
        "documents": list(documents),
    }
    if top_n:
        payload["top_n"] = top_n

    client = get_client()
    last_error: Exception | None = None
    for attempt in range(MAX_RETRIES):
        try:
            resp = await client.post(settings.path, json=payload)
            resp.raise_for_status()
        except httpx.HTTPStatusError as exc:
            # 4xx 是请求本身的问题（模型名错、超长、鉴权失败），重试无意义
            if 400 <= exc.response.status_code < 500:
                raise ServiceError(
                    f"Rerank 服务拒绝请求（HTTP {exc.response.status_code}）："
                    f"{exc.response.text[:200]}"
                ) from exc
            last_error = exc
        except httpx.HTTPError as exc:
            last_error = exc
        else:
            try:
                body = resp.json()
            except ValueError as exc:
                # 返回的不是 JSON，多半是 path 配错了（指到了某个网页或代理兜底页）。
                # 重试不会让它变成 JSON，直接给出可排查的信息。
                raise ServiceError(
                    f"Rerank 服务的响应不是 JSON（{settings.host}{settings.path}）："
                    f"{resp.text[:200]}"
                ) from exc
            return _parse(body, len(documents))

        if attempt < MAX_RETRIES - 1:
            await asyncio.sleep(RETRY_BACKOFF_SECONDS * (attempt + 1))

    raise ServiceError(
        f"Rerank 服务不可用（{settings.host}{settings.path}，"
        f"重试 {MAX_RETRIES} 次）：{last_error}"
    )


def _parse(body: object, count: int) -> list[RankedDocument]:
    """把两种响应形状归一成 ``RankedDocument`` 列表。"""
    if isinstance(body, dict):
        items = body.get("results")
        if items is None:
            items = body.get("data")
    else:
        items = body
    if not isinstance(items, list):
        raise ServiceError(f"Rerank 服务返回了无法识别的结构：{str(body)[:200]}")

    ranked: list[RankedDocument] = []
    for item in items:
        if not isinstance(item, dict):
            continue
        index = item.get("index")
        score = item.get("relevance_score", item.get("score"))
        if not isinstance(index, int) or not isinstance(score, (int, float)):
            continue
        if not 0 <= index < count:
            # 下标越界说明双方对 documents 的理解不一致，宁可报错也不猜
            raise ServiceError(f"Rerank 服务返回了越界下标 {index}（共 {count} 条文档）")
        ranked.append(RankedDocument(index=index, score=float(score)))

    if not ranked:
        raise ServiceError(f"Rerank 服务没有返回任何有效结果：{str(body)[:200]}")
    # 同分按下标升序：让"分数相同"时的先后完全由入参顺序决定，不取决于服务端怎么排
    ranked.sort(key=lambda item: (-item.score, item.index))
    return ranked
