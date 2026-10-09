"""检索网关（阶段 5 为单路检索；阶段 6 在此换成混合检索 + RRF + rerank）。

三条路的分工由阶段 4 已建好的索引决定，不是随意选的：

- **字段 / 指标** —— Qdrant 稠密向量（阶段 4 建的 ``data-agent-column`` / ``data-agent-metric``）
- **取值** —— Elasticsearch 全文（阶段 4 建的 ik 索引；取值本来就没有向量集合）

三路收在一个网关里，是为了让阶段 6 有**唯一的替换点**：内部换成
"向量 + 全文 → RRF → rerank" 时节点代码一行不用改——这正是《开发步骤文档》
阶段 6 说的"在 recall_column/recall_metric 节点中替换原单一向量检索"。

分数不可跨路比较：向量是余弦相似度（0~1），ES 是 BM25（无上界）。
合并只按各路内部排序拼接，不做跨路归一——没有依据的归一化比不归一化更误导。
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass

from app.clients import embedding, es, qdrant
from app.core.config import get_settings

# 每个检索词取回多少条候选。取值召回走 ES，重复取值多，给得宽一些。
VECTOR_LIMIT = 10
FULLTEXT_LIMIT = 10


@dataclass(slots=True)
class Candidate:
    """一路召回的候选项。

    ``kind`` 决定哪些字段有意义：

    - ``column``：``name`` 是字段名，``table_name`` 是所属表
    - ``value`` ：``name`` 是取值本身，``column_name`` 是它属于哪个字段，
      此时 ``id`` 是**该字段**的 ``column_info.id``——取值不是独立实体，跟着字段走
    - ``metric``：``name`` 是指标名

    三种都带 ``id``，且都是 MySQL 里的主键（阶段 4 让 Qdrant 点 id 与 ES
    ``column_id`` 直接复用了同一批 uuid），所以召回结果不必再做 id 映射即可回查。
    """

    kind: str
    id: str
    name: str
    score: float
    table_name: str | None = None
    column_name: str | None = None
    alias: str | None = None
    description: str | None = None
    source: str = "vector"


class RetrievalGateway:
    """三路检索。"""

    def __init__(self, *, vector_limit: int = VECTOR_LIMIT, fulltext_limit: int = FULLTEXT_LIMIT) -> None:
        self._vector_limit = vector_limit
        self._fulltext_limit = fulltext_limit

    @classmethod
    def default(cls) -> RetrievalGateway:
        return cls()

    async def columns(self, keywords: Sequence[str]) -> list[Candidate]:
        """字段召回：逐词向量检索，按 id 去重后取每项的最高分。"""
        return await self._vector_recall(
            collection=get_settings().qdrant.column_collection,
            keywords=keywords,
            kind="column",
        )

    async def metrics(self, keywords: Sequence[str]) -> list[Candidate]:
        """指标召回：同上，走指标集合。"""
        return await self._vector_recall(
            collection=get_settings().qdrant.metric_collection,
            keywords=keywords,
            kind="metric",
        )

    async def values(self, keywords: Sequence[str]) -> list[Candidate]:
        """取值召回：走 ES 全文索引。

        去重键是 ``字段 + 取值`` 而不是字段：同一个取值可能合法地属于多个字段
        （比如 ``2025`` 既可能是年份也可能是编号），按字段去重会把其中一个丢掉，
        而丢掉的那个恰好可能是问句指向的那个。
        """
        words = [word for word in keywords if word.strip()]
        if not words:
            return []

        client = es.get_es()
        index = get_settings().es.index_name
        best: dict[str, Candidate] = {}

        for word in words:
            response = await client.search(
                index=index,
                query={"match": {"value": word}},
                size=self._fulltext_limit,
            )
            for hit in response["hits"]["hits"]:
                source = hit["_source"]
                column_id = str(source["column_id"])
                value = str(source["value"])
                key = f"{column_id}|{value}"
                score = float(hit.get("_score") or 0.0)
                current = best.get(key)
                if current is None or score > current.score:
                    best[key] = Candidate(
                        kind="value",
                        id=column_id,
                        name=value,
                        score=score,
                        table_name=source.get("table_name"),
                        column_name=source.get("column_name"),
                        alias=source.get("column_alias"),
                        source="fulltext",
                    )

        return _rank(best.values())

    async def _vector_recall(
        self, *, collection: str, keywords: Sequence[str], kind: str
    ) -> list[Candidate]:
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
                limit=self._vector_limit,
                with_payload=True,
            )
            for hit in response.points:
                point_id = str(hit.id)
                payload = hit.payload or {}
                candidate = _from_payload(kind, point_id, float(hit.score), payload)
                current = best.get(point_id)
                if current is None or candidate.score > current.score:
                    best[point_id] = candidate

        return _rank(best.values())


def _rank(candidates) -> list[Candidate]:
    """按分数降序；同分按 id 升序。

    加 id 这个次级键是为了让排序**完全确定**——否则同分项的顺序取决于字典的
    插入顺序，也就取决于上游返回顺序，测试会偶发失败、线上结果会莫名抖动。
    """
    return sorted(candidates, key=lambda c: (-c.score, c.id))


def _from_payload(kind: str, point_id: str, score: float, payload: dict) -> Candidate:
    if kind == "metric":
        return Candidate(
            kind=kind,
            id=point_id,
            name=str(payload.get("metric_name", "")),
            score=score,
            alias=payload.get("metric_alias"),
            description=payload.get("description"),
        )
    return Candidate(
        kind=kind,
        id=point_id,
        name=str(payload.get("column_name", "")),
        score=score,
        table_name=payload.get("table_name"),
        alias=payload.get("column_alias"),
        description=payload.get("description"),
    )
