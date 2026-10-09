"""检索网关（阶段 6：混合检索 = 两路召回 → RRF 融合 → rerank 精排）。

三条路各自的情况：

    kind      第一路                第二路                融合  精排
    column    Qdrant 字段向量       ES 字段全文索引        ✓     ✓
    metric    Qdrant 指标向量       ES 指标全文索引        ✓     ✓
    value     ——（无向量集合）      ES 取值全文索引        ——    ✓

**取值为什么只有一路**：取值不是独立实体，它是某个字段的一个取值，语义空间里
没有"取值向量"这种东西可查；它的召回本来就是字面匹配，BM25 已经是对的工具。
阶段 4 也只给字段与指标建了向量集合。缺第二路的后果是"少了名次投票"，
不是"没有结果"，所以这里不硬凑一路来对称。

**为什么精排要让"服务不可用"变成降级而不是报错**：检索是问答链路的必经环节，
一个可选的重排服务挂掉不该让整个问答失败。降级顺序是"RRF 融合顺序"，
它本身就是可用的召回结果（真模型的精排质量由阶段 10 的评测集度量）。
提醒只打一次（见 ``_warn_rerank_down``），否则一次问答三条日志会把别的问题淹掉。

**分数的量纲**：融合后是 RRF 分（≈1/61 量级），精排后是交叉编码器分，
未启用精排时是各自原始分（余弦或 BM25）。跨路比较没有意义，下游只能比大小。
"""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Awaitable, Sequence
from dataclasses import replace
from typing import Protocol

from app.clients import rerank as rerank_client
from app.core.config import get_settings
from app.core.exceptions import ServiceError
from app.retrievers.candidate import Candidate, rank_by_score
from app.retrievers.dense import DenseRetriever
from app.retrievers.fulltext import FullTextRetriever
from app.retrievers.fusion import RRF_K, reciprocal_rank_fusion

logger = logging.getLogger(__name__)

# 每条召回最多交给下游多少条候选。与 merge_retrieved_info 的 MAX_PER_KIND 同值：
# 交叉编码器的开销与文档数成正比，20 条是"够用"与"不慢"之间的取舍；
# 再多也不会进提示词（合并节点同样按 20 截断）。
RERANK_INPUT_LIMIT = 20


class Reranker(Protocol):
    """重排器接口。返回 ``None`` 表示"这次不精排"，由网关保持融合顺序。"""

    async def __call__(
        self, query: str, documents: Sequence[str], *, top_n: int | None = None
    ) -> list[rerank_client.RankedDocument] | None: ...


async def configured_reranker(
    query: str, documents: Sequence[str], *, top_n: int | None = None
) -> list[rerank_client.RankedDocument] | None:
    """默认重排器：读配置决定是否调用。

    开关放在配置里而不是"配一个连不上的地址来表达不要精排"：
    后者会让每次问答都白等一轮超时，且日志里全是连接失败——
    一个刻意的选择不该长得像一次故障。
    """
    settings = get_settings().rerank
    if not settings.enabled or not settings.host:
        return None
    return await rerank_client.rerank(query, documents, top_n=top_n or None)


class RetrievalGateway:
    """三条路的混合检索。"""

    def __init__(
        self,
        *,
        dense: DenseRetriever | None = None,
        fulltext: FullTextRetriever | None = None,
        reranker: Reranker | None = configured_reranker,
        rrf_k: int = RRF_K,
        limit: int = RERANK_INPUT_LIMIT,
    ) -> None:
        self._dense = dense or DenseRetriever()
        self._fulltext = fulltext or FullTextRetriever()
        self._reranker = reranker
        self._rrf_k = rrf_k
        self._limit = limit

    @classmethod
    def default(cls) -> RetrievalGateway:
        return cls()

    async def columns(self, keywords: Sequence[str], *, query: str) -> list[Candidate]:
        """字段召回：向量 + 全文 → RRF → 精排。"""
        settings = get_settings()
        return await self._hybrid(
            query=query,
            keywords=keywords,
            kind="column",
            collection=settings.qdrant.column_collection,
            index=settings.es.column_index,
        )

    async def metrics(self, keywords: Sequence[str], *, query: str) -> list[Candidate]:
        """指标召回：向量 + 全文 → RRF → 精排。"""
        settings = get_settings()
        return await self._hybrid(
            query=query,
            keywords=keywords,
            kind="metric",
            collection=settings.qdrant.metric_collection,
            index=settings.es.metric_index,
        )

    async def values(self, keywords: Sequence[str], *, query: str) -> list[Candidate]:
        """取值召回：全文 → 精排（只有一路，没有可融合的对象）。

        ``query`` 在这里是有用的：取值候选要按"与整个问句的相关度"重排，
        而不是按其中某一个词。签名与另两路保持一致，节点代码才不必分支。
        """
        settings = get_settings()
        return await self._hybrid(
            query=query,
            keywords=keywords,
            kind="value",
            collection=None,
            index=settings.es.value_index,
            field="value",
        )

    async def _hybrid(
        self,
        *,
        query: str,
        keywords: Sequence[str],
        kind: str,
        collection: str | None,
        index: str | None,
        field: str = "text",
    ) -> list[Candidate]:
        """两路并行召回 → 融合 → 精排。

        两路各自独立，没有先后依赖，所以并发发出：它们的耗时都是网络往返，
        串行会让每次召回白等一份延迟。``return_exceptions`` 不用——任何一路
        失败都该如实抛出（知识库没建好是配置问题，不该被另一路掩盖）。
        """
        words = [word for word in keywords if word.strip()]
        if not words:
            return []

        tasks: list[Awaitable[list[Candidate]]] = []
        if collection:
            tasks.append(self._dense.retrieve(words, collection=collection, kind=kind))
        if index:
            tasks.append(self._fulltext.retrieve(words, index=index, field=field, kind=kind))

        rankings = [ranking for ranking in await asyncio.gather(*tasks) if ranking]
        if not rankings:
            return []
        fused = (
            reciprocal_rank_fusion(rankings, k=self._rrf_k)
            if len(rankings) > 1
            else rankings[0]
        )
        return await self._rerank(query, fused[: self._limit])

    async def _rerank(self, query: str, candidates: list[Candidate]) -> list[Candidate]:
        """按与问句的相关度重排；未启用或服务不可用时保持融合顺序。"""
        if not candidates or self._reranker is None:
            return candidates

        documents = [document_text(candidate) for candidate in candidates]
        try:
            ranked = await self._reranker(query, documents, top_n=get_settings().rerank.top_n)
        except ServiceError as exc:
            _warn_rerank_down(exc)
            return candidates
        if not ranked:
            return candidates

        _clear_rerank_warning()
        ordered = [replace(candidates[item.index], score=item.score) for item in ranked]
        # 服务可能只返回前 top_n 条，剩下的按融合顺序跟在后面。
        # 丢弃它们等于让一次"只想看前几名"的配置把召回结果凭空变少。
        returned = {item.index for item in ranked}
        ordered.extend(
            candidate for position, candidate in enumerate(candidates) if position not in returned
        )
        return ordered


def document_text(candidate: Candidate) -> str:
    """重排用的文档文本。

    优先取建库时写入的 ``text``——那是被向量化、被全文索引的**同一段文本**。
    精排如果用另一段拼出来的字，就会出现"召回的依据"和"排序的依据"不是一回事，
    名次对不上时无从解释。老索引没有这个字段时才按候选自带的字段兜底拼一份。
    """
    if candidate.text:
        return candidate.text
    parts = [
        value
        for value in (candidate.alias, candidate.name, candidate.column_name, candidate.table_name,
                      candidate.description)
        if value
    ]
    return "。".join(dict.fromkeys(parts)) if parts else candidate.name


# --------------------------------------------------------------------------
# 降级提醒：只提醒一次
# --------------------------------------------------------------------------

_rerank_warning_shown = False


def _warn_rerank_down(exc: Exception) -> None:
    """重排服务不可用时提醒一次。

    一次问答会有三条召回同时失败，每次都打 WARNING 会让日志被同一件事刷满，
    而它的处置方式（起服务，或把开关关掉）在整个进程里是同一个。
    服务恢复后再挂掉会重新提醒，见 ``_clear_rerank_warning``。
    """
    global _rerank_warning_shown
    if _rerank_warning_shown:
        logger.debug("Rerank 仍不可用：%s", exc)
        return
    _rerank_warning_shown = True
    logger.warning(
        "Rerank 服务不可用，检索退回 RRF 融合顺序（%s）。"
        "需要精排请启动重排服务；确定不用则设 RERANK__ENABLED=false 明确关闭。原因：%s",
        get_settings().rerank.host,
        exc,
    )


def _clear_rerank_warning() -> None:
    """服务恢复正常后复位提醒状态，将来再挂掉还能提醒一次。"""
    global _rerank_warning_shown
    _rerank_warning_shown = False


__all__ = [
    "Candidate",
    "Reranker",
    "RetrievalGateway",
    "configured_reranker",
    "document_text",
    "rank_by_score",
]
