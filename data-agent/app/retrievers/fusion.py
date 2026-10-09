"""RRF（Reciprocal Rank Fusion）融合（ADR-007）。

    score(d) = Σ_i 1 / (k + rank_i(d))        rank 从 1 起，k = 60

**为什么用名次而不是分数**：两路的分数根本不可比——向量是余弦相似度（0~1），
ES 是 BM25（无上界，同一份文档换个词表就能整体翻倍）。想把它们归一化到同一
尺度，需要知道各自的分布，而分布随语料与查询变化，没有稳定可用的估计。
名次则天然同尺度：第 1 名就是第 1 名。这就是 RRF"对异构分数鲁棒"的全部含义。

**为什么 k=60**：k 决定"多一路命中"值多少分。k=60 时第 1 名与第 2 名的分差
是 1/61 − 1/62 ≈ 0.0003，而"多一路命中"至少贡献 1/61 ≈ 0.0164——也就是
**任何两路都命中的项，一定压过只被一路命中的项**，哪怕后者在那一路上排第 1。
这正是融合想要的：共识优先于单路的极端名次。k 取小（比如 1）会让名次差
盖过共识，融合退化成"看谁在某一路里更靠前"，那还不如不融合。
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import replace

from app.retrievers.candidate import Candidate

RRF_K = 60

#: 两路（或更多）都命中时记的来源，便于下游与排查区分"融合产物"
HYBRID = "hybrid"

#: 只在两路之间补齐的字段。id / name / kind 是身份，不能从另一路覆盖
_FILLABLE = ("text", "description", "alias", "table_name", "column_name")


def reciprocal_rank_fusion(
    rankings: Sequence[Sequence[Candidate]], *, k: int = RRF_K
) -> list[Candidate]:
    """把多路有序候选按 RRF 合成一路。

    ``rankings`` 的顺序即**优先级**：同一个实体被多路命中时，取先出现的那一路
    的字段（并用后面的补齐空字段）。向量那一路的 payload 字段最全，所以调用方
    把它排在前面。
    """
    scores: dict[tuple[str, str, str], float] = {}
    merged: dict[tuple[str, str, str], Candidate] = {}
    sources: dict[tuple[str, str, str], list[str]] = {}

    for ranking in rankings:
        for rank, candidate in enumerate(ranking, start=1):
            key = candidate.identity()
            scores[key] = scores.get(key, 0.0) + 1.0 / (k + rank)
            if key in merged:
                merged[key] = _fill_missing(merged[key], candidate)
                sources[key].append(candidate.source)
            else:
                merged[key] = candidate
                sources[key] = [candidate.source]

    fused = [
        replace(
            candidate,
            score=scores[key],
            source=_label(sources[key]),
        )
        for key, candidate in merged.items()
    ]
    # 同分按 id 定序：RRF 分由名次算出，两路名次完全对称时确实可能相等
    return sorted(fused, key=lambda c: (-c.score, c.id))


def _fill_missing(base: Candidate, other: Candidate) -> Candidate:
    """用另一路的值补齐 ``base`` 的空字段。

    两路对同一个实体掌握的信息不一样：向量 payload 有 description 没有整段文本，
    全文索引的 ``_source`` 有整段文本却可能没有别名。取长补短之后，无论最终
    由哪一路"赢"下这条候选，下游看到的字段都是同一套。
    """
    patch = {
        field: value
        for field in _FILLABLE
        if not getattr(base, field) and (value := getattr(other, field))
    }
    return replace(base, **patch) if patch else base


def _label(sources: Sequence[str]) -> str:
    """来源标签：只被一路命中就沿用那一路的名字，两路都有则是 hybrid。"""
    unique = list(dict.fromkeys(sources))
    return unique[0] if len(unique) == 1 else HYBRID
