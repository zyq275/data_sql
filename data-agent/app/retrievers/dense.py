"""稠密向量检索器（Qdrant）——混合检索的第一路。

独立的检索器而不是塞在网关里的一个私有方法：融合需要"两路各自给出**有序**结果"，
把每路做成同样的形状（``retrieve(...) -> list[Candidate]``，内部已排序），
RRF 才不至于要反过来猜"这个列表是按什么排的"。
"""

from __future__ import annotations

from collections.abc import Sequence

from app.clients import embedding, qdrant
from app.retrievers.candidate import Candidate, rank_by_score

# 每个检索词取回多少条候选。多词取回后按 id 去重取最高分。
DENSE_LIMIT = 10


class DenseRetriever:
    """Qdrant 向量检索（字段 / 指标两个集合）。"""

    def __init__(self, *, limit: int = DENSE_LIMIT) -> None:
        self._limit = limit

    async def retrieve(
        self, keywords: Sequence[str], *, collection: str, kind: str
    ) -> list[Candidate]:
        """逐词向量检索，按点 id 取每项的最高分。

        逐词而不是把多个词拼成一句去查：拼接会把"销售额"和"华东"混成一个
        既不是指标也不是地区的中间语义，两边都不像。分开查各自命中，
        再由 RRF 用名次把两批结果合起来。
        """
        words = [word for word in keywords if word.strip()]
        if not words:
            return []

        vectors = await embedding.embed_texts(words)
        client = qdrant.get_qdrant()
        best: dict[str, Candidate] = {}

        for vector in vectors:
            response = await client.query_points(
                collection_name=collection,
                query=vector,
                limit=self._limit,
                with_payload=True,
            )
            for hit in response.points:
                point_id = str(hit.id)
                candidate = _from_payload(kind, point_id, float(hit.score), hit.payload or {})
                current = best.get(point_id)
                if current is None or candidate.score > current.score:
                    best[point_id] = candidate

        return rank_by_score(best.values())


def _from_payload(kind: str, point_id: str, score: float, payload: dict) -> Candidate:
    """Qdrant payload -> Candidate。字段名在两处必须保持一致（见 build_meta）。"""
    common = {
        "kind": kind,
        "id": point_id,
        "score": score,
        "alias": payload.get("metric_alias") if kind == "metric" else payload.get("column_alias"),
        "description": payload.get("description"),
        "source": "vector",
        "text": payload.get("text") or "",
    }
    if kind == "metric":
        return Candidate(name=str(payload.get("metric_name", "")), **common)
    return Candidate(
        name=str(payload.get("column_name", "")),
        table_name=payload.get("table_name"),
        **common,
    )
