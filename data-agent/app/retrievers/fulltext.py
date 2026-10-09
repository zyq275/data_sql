"""全文检索器（Elasticsearch）——混合检索的第二路。

与向量那一路是互补的两种"相关"：向量问的是"语义像不像"，BM25 问的是"词
有没有真的出现过"。近义词（销售额 vs 成交总额）靠前者，字面共现
（description 里写着"口语常称销售额"）靠后者。实测里正是这一路把
阶段 4 遗留的"销售额被排到销量之前"纠正过来的。

三个索引共用这一个检索器：它们的文档形状不同，但"逐词 match、按身份去重取最高分"
的行为完全一致，差别只在索引名与字段名——由调用方传入，不在这里分支。
"""

from __future__ import annotations

from collections.abc import Sequence

from app.clients import es
from app.retrievers.candidate import Candidate, rank_by_score

# 每个检索词取回的候选数。取值索引里重复取值多，与向量那一路取同一个量级，
# 保证两路在 RRF 里的"投票基数"相当——一路 10 条、另一路 100 条会让后者
# 靠数量而不是靠质量左右融合结果。
FULLTEXT_LIMIT = 10


class FullTextRetriever:
    """ES 全文检索（取值 / 字段 / 指标三个索引）。"""

    def __init__(self, *, limit: int = FULLTEXT_LIMIT) -> None:
        self._limit = limit

    async def retrieve(
        self, keywords: Sequence[str], *, index: str, field: str, kind: str
    ) -> list[Candidate]:
        """逐词全文检索，按候选身份去重后取每项的最高分。"""
        words = [word for word in keywords if word.strip()]
        if not words:
            return []

        client = es.get_es()
        best: dict[tuple[str, str, str], Candidate] = {}

        for word in words:
            response = await client.search(
                index=index,
                query={"match": {field: word}},
                size=self._limit,
            )
            for hit in response["hits"]["hits"]:
                candidate = _from_source(kind, hit["_source"], float(hit.get("_score") or 0.0))
                key = candidate.identity()
                current = best.get(key)
                if current is None or candidate.score > current.score:
                    best[key] = candidate

        return rank_by_score(best.values())


def _from_source(kind: str, source: dict, score: float) -> Candidate:
    """ES ``_source`` -> Candidate。"""
    if kind == "value":
        return Candidate(
            kind=kind,
            id=str(source["column_id"]),
            name=str(source["value"]),
            score=score,
            table_name=source.get("table_name"),
            column_name=source.get("column_name"),
            alias=source.get("column_alias"),
            source="fulltext",
            # 老索引里没有 text 字段时按同样的规则拼一份（见 es.value_text）
            text=source.get("text")
            or es.value_text(
                str(source.get("column_name", "")), source.get("column_alias"), str(source["value"])
            ),
        )

    if kind == "metric":
        return Candidate(
            kind=kind,
            id=str(source["metric_id"]),
            name=str(source["metric_name"]),
            score=score,
            alias=source.get("metric_alias"),
            description=source.get("description"),
            source="fulltext",
            text=source.get("text") or "",
        )

    return Candidate(
        kind=kind,
        id=str(source["column_id"]),
        name=str(source["column_name"]),
        score=score,
        table_name=source.get("table_name"),
        alias=source.get("column_alias"),
        description=source.get("description"),
        source="fulltext",
        text=source.get("text") or "",
    )
