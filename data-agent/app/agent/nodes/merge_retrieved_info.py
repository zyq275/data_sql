"""三路召回结果汇聚。"""

from __future__ import annotations

from langgraph.runtime import Runtime

from app.agent.context import QueryContext
from app.agent.events import step
from app.agent.state import QueryState
from app.retrievers.retrieval import Candidate

# 每一路各留多少条给下游。分批限量而不是全局取前 N：全局排序会让量纲决定谁被留下
# ——向量分数是 0~1 的余弦相似度，ES 是上不封顶的 BM25，混在一起排等于让
# "哪种分数的数值大"代替"哪个更相关"来做决定。
MAX_PER_KIND = 20

_KIND_ORDER = ("column", "value", "metric")
_SOURCE_KEYS = ("column_candidates", "value_candidates", "metric_candidates")


@step("merge_retrieved_info")
async def merge_retrieved_info(state: QueryState, runtime: Runtime[QueryContext]) -> dict:
    """把三路候选合成一个去重、限量的列表。

    ``runtime`` 这里用不到，但 TDD 4 要求所有节点签名统一为 ``(state, runtime)``：
    签名一致才能套同一个进度装饰器，也才能在阶段 6/7/9 平滑地加依赖而不用改图。
    """
    merged: list[Candidate] = []
    for key in _SOURCE_KEYS:
        merged.extend(state.get(key) or [])

    seen: set[tuple[str, str, str]] = set()
    grouped: dict[str, list[Candidate]] = {kind: [] for kind in _KIND_ORDER}
    for candidate in merged:
        identity = (candidate.kind, candidate.id, candidate.name)
        if identity in seen:
            continue
        seen.add(identity)
        if candidate.kind in grouped:
            grouped[candidate.kind].append(candidate)

    # 各路内部已按分数降序（见 retrievers.retrieval._rank），截断即"留下每路最好的"
    retrieved: list[Candidate] = []
    for kind in _KIND_ORDER:
        retrieved.extend(grouped[kind][:MAX_PER_KIND])
    return {"retrieved": retrieved}
