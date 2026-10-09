"""阶段 6：混合检索的纯逻辑测试（RRF 融合、精排编排、重排客户端协议）。

这一层用**桩**替换两路检索器与重排服务，不碰 .env、不碰任何外部服务：
融合的数学、精排失败时的降级、两种重排协议（TEI 形与 OpenAI 形）的解析
都是纯逻辑，必须能在没有 Qdrant / ES / 重排服务的机器上跑。
真实链路另由 test_stage6_acceptance.py 覆盖。
"""

from __future__ import annotations

import asyncio
import logging

import httpx
import pytest

from app.clients import rerank as rerank_client
from app.core.exceptions import ServiceError
from app.retrievers import retrieval as retrieval_module
from app.retrievers.candidate import Candidate, rank_by_score
from app.retrievers.dense import _from_payload
from app.retrievers.fulltext import _from_source
from app.retrievers.fusion import RRF_K, reciprocal_rank_fusion
from app.retrievers.retrieval import RetrievalGateway, document_text


def candidate(
    id: str = "x1",  # noqa: A002 —— 与 Candidate 的字段名一致更好读
    *,
    kind: str = "column",
    name: str | None = None,
    score: float = 1.0,
    source: str = "vector",
    **extra,
) -> Candidate:
    return Candidate(
        kind=kind,
        id=id,
        name=name or id,
        score=score,
        source=source,
        **extra,
    )


class StubRetriever:
    """按调用次数返回固定排序的桩检索器。"""

    def __init__(self, ranking: list[Candidate], *, on_call=None) -> None:
        self._ranking = ranking
        self._on_call = on_call
        self.calls: list[tuple] = []

    async def retrieve(self, keywords, *, collection=None, index=None, field=None, kind=None):
        self.calls.append((tuple(keywords), collection, index, field, kind))
        if self._on_call is not None:
            await self._on_call()
        return list(self._ranking)


def gateway(*, dense=None, fulltext=None, reranker=None) -> RetrievalGateway:
    return RetrievalGateway(
        dense=dense or StubRetriever([]),
        fulltext=fulltext or StubRetriever([]),
        reranker=reranker,
    )


# ==========================================================================
# RRF 融合
# ==========================================================================


def test_consensus_beats_a_single_path_top_rank() -> None:
    """两路都命中的项必须压过只被一路命中的项——这是 RRF 存在的理由。

    构造：``b`` 在两路里都排第 2，``a`` 只在第一路排第 1。若融合按"谁在
    某一路更靠前"来定，``a`` 会赢；按 RRF，``b`` 赢（1/62 + 1/61 vs 1/61）。
    """
    first = [candidate("a"), candidate("b")]
    second = [candidate("z", source="fulltext"), candidate("b", source="fulltext")]

    fused = reciprocal_rank_fusion([first, second])

    assert [c.id for c in fused][0] == "b"
    assert [c.id for c in fused] == ["b", "a", "z"]


def test_score_is_the_sum_of_reciprocal_ranks() -> None:
    first = [candidate("a"), candidate("b")]
    second = [candidate("b", source="fulltext")]

    fused = {c.id: c.score for c in reciprocal_rank_fusion([first, second])}

    assert fused["a"] == pytest.approx(1 / (RRF_K + 1))
    assert fused["b"] == pytest.approx(1 / (RRF_K + 2) + 1 / (RRF_K + 1))


def test_source_label_distinguishes_fused_from_single_path_hits() -> None:
    first = [candidate("a"), candidate("b")]
    second = [candidate("b", source="fulltext")]

    fused = {c.id: c.source for c in reciprocal_rank_fusion([first, second])}

    assert fused["a"] == "vector"
    assert fused["b"] == "hybrid"


def test_missing_fields_are_filled_from_the_other_path() -> None:
    """两路掌握的信息不同，合起来才完整：向量那路有描述没原文，全文那路相反。"""
    dense_only = candidate("c1", description="订单成交金额", text="")
    same_entity = candidate("c1", source="fulltext", text="订单金额（order_amount）。订单成交金额")

    fused = reciprocal_rank_fusion([[dense_only], [same_entity]])[0]

    assert fused.text == same_entity.text, "全文那路的原文该补进来（精排要用）"
    assert fused.description == "订单成交金额", "向量那路的描述不该被空值覆盖"


def test_ties_are_broken_by_id_so_the_order_is_never_random() -> None:
    """两路名次完全对称时分数会相等（这里 a、b 互换名次），次序必须确定。"""
    first = [candidate("b1"), candidate("a1")]
    second = [candidate("a1", source="fulltext"), candidate("b1", source="fulltext")]

    assert [c.id for c in reciprocal_rank_fusion([first, second])] == ["a1", "b1"]


# ==========================================================================
# 网关：两路 → 融合 → 精排
# ==========================================================================


async def test_single_path_keeps_its_own_scores_instead_of_being_fused() -> None:
    """只有一路（取值）时不做融合：拿它去算 RRF 分等于把原始分换成名次分，
    白白丢掉"第一名有多确定"这条信息。"""
    only = StubRetriever([candidate("v1", source="fulltext", score=7.5)])

    result = await gateway(fulltext=only).values(["华东"], query="华东区卖了多少")

    assert [c.score for c in result] == [7.5]


async def test_both_paths_are_queried_concurrently() -> None:
    """两路没有先后依赖，串行会让每次召回白等一份网络延迟。

    桩的设计让串行实现必然超时：第一路要等到第二路开始才返回。
    """
    started = asyncio.Event()

    async def wait_for_other_path() -> None:
        await asyncio.wait_for(started.wait(), timeout=0.5)

    async def start_other_path() -> None:
        started.set()

    dense = StubRetriever([candidate("c1")], on_call=wait_for_other_path)
    fulltext = StubRetriever([candidate("c1", source="fulltext")], on_call=start_other_path)

    await gateway(dense=dense, fulltext=fulltext).columns(["订单金额"], query="订单金额是多少")

    assert dense.calls and fulltext.calls, "两路都必须被调用"


async def test_each_kind_queries_its_own_indexes() -> None:
    """字段走字段索引、指标走指标索引、取值走取值索引——配错了不会报错，
    只会安安静静地召回一堆不相关的东西。"""
    settings = retrieval_module.get_settings()
    dense = StubRetriever([])
    fulltext = StubRetriever([])
    gateway_ = gateway(dense=dense, fulltext=fulltext)

    await gateway_.columns(["金额"], query="q")
    await gateway_.metrics(["销售额"], query="q")
    await gateway_.values(["华东"], query="q")

    assert [call[1] for call in dense.calls] == [
        settings.qdrant.column_collection,
        settings.qdrant.metric_collection,
    ], "取值不该去查向量集合（它没有）"
    assert [call[2] for call in fulltext.calls] == [
        settings.es.column_index,
        settings.es.metric_index,
        settings.es.value_index,
    ]


async def test_empty_keywords_skip_both_paths() -> None:
    dense = StubRetriever([candidate("c1")])
    fulltext = StubRetriever([candidate("c1", source="fulltext")])
    gateway_ = gateway(dense=dense, fulltext=fulltext)

    assert await gateway_.columns(["", "   "], query="q") == []
    assert not dense.calls and not fulltext.calls


async def test_rerank_can_overturn_the_fusion_order() -> None:
    """精排的职责就是在融合排序之上做最终裁决——两路都说 A 更相关，
    交叉编码器说 B 更相关时，最终结果听 B 的。

    （真实模型里这一步纠正的是"销售额 vs 销量"这类近义混淆；
    这里用桩把"排序确实会变"这件事钉死，与模型质量解耦。）
    """
    dense = StubRetriever([candidate("a", score=0.9), candidate("b", score=0.8)])
    fulltext = StubRetriever([candidate("a", source="fulltext", score=5.0)])

    async def reranker(query, documents, *, top_n=None):
        assert query == "去年销售额是多少"
        # 与 documents 的下标对应：b 才是相关的
        return [
            rerank_client.RankedDocument(index=1, score=0.97),
            rerank_client.RankedDocument(index=0, score=0.11),
        ]

    result = await gateway(dense=dense, fulltext=fulltext, reranker=reranker).metrics(
        ["销售额"], query="去年销售额是多少"
    )

    assert [c.id for c in result] == ["b", "a"], "精排后相关项必须排在前面"
    assert [c.score for c in result] == [0.97, 0.11], "分数要换成精排分"
    assert result[0].source == "vector", "source 记的是来源，不因精排而改变"


async def test_rerank_receives_the_indexed_text_not_a_rebuilt_one() -> None:
    """送进交叉编码器的必须是建库时那段原文，否则"排的依据"与"召的依据"分叉。"""
    seen: list[list[str]] = []

    async def reranker(query, documents, *, top_n=None):
        seen.append(list(documents))
        return None

    dense = StubRetriever([candidate("c1", text="订单金额（order_amount）。订单成交金额")])
    await gateway(dense=dense, reranker=reranker).columns(["订单金额"], query="q")

    assert seen == [["订单金额（order_amount）。订单成交金额"]]


async def test_rerank_returning_nothing_keeps_the_fusion_order() -> None:
    """重排器返回 None = 本次不精排（开关关闭或未配置），顺序原样保留。"""
    dense = StubRetriever([candidate("a", score=0.9), candidate("b", score=0.8)])

    async def reranker(query, documents, *, top_n=None):
        return None

    result = await gateway(dense=dense, reranker=reranker).columns(["金额"], query="q")

    assert [c.id for c in result] == ["a", "b"]
    assert [c.score for c in result] == [0.9, 0.8]


async def test_unavailable_rerank_service_degrades_to_fusion_order(caplog) -> None:
    """重排服务挂了不该让整个问答失败——降级到融合顺序，并提醒一次。"""
    retrieval_module._clear_rerank_warning()
    dense = StubRetriever([candidate("a", score=0.9), candidate("b", score=0.8)])

    async def broken_reranker(query, documents, *, top_n=None):
        raise ServiceError("Rerank 服务不可用（http://127.0.0.1:8002/rerank）")

    gateway_ = gateway(dense=dense, reranker=broken_reranker)
    with caplog.at_level(logging.WARNING, logger=retrieval_module.__name__):
        first = await gateway_.columns(["金额"], query="q")
        second = await gateway_.columns(["订单号"], query="q")

    assert [c.id for c in first] == ["a", "b"], "失败时保持融合顺序"
    assert [c.id for c in second] == ["a", "b"]
    warnings = [r for r in caplog.records if r.levelno == logging.WARNING]
    assert len(warnings) == 1, (
        f"一次问答有三条召回，同一个故障只该提醒一次，实际 {len(warnings)} 次："
        f"{[r.message for r in warnings]}"
    )


async def test_the_warning_comes_back_after_a_recovery(caplog) -> None:
    """服务恢复后再挂掉要能重新提醒——否则第一次抖动会永久静音这个告警。"""
    retrieval_module._clear_rerank_warning()
    calls = {"n": 0}

    async def flaky_reranker(query, documents, *, top_n=None):
        calls["n"] += 1
        if calls["n"] != 2:
            raise ServiceError("挂了")
        return [rerank_client.RankedDocument(index=0, score=1.0)]

    gateway_ = gateway(dense=StubRetriever([candidate("a")]), reranker=flaky_reranker)
    with caplog.at_level(logging.WARNING, logger=retrieval_module.__name__):
        await gateway_.columns(["金额"], query="q")  # 挂 -> 提醒
        await gateway_.columns(["金额"], query="q")  # 正常 -> 复位
        await gateway_.columns(["金额"], query="q")  # 又挂 -> 再提醒

    assert len([r for r in caplog.records if r.levelno == logging.WARNING]) == 2


async def test_document_text_falls_back_when_the_index_has_no_text() -> None:
    """老索引（重建之前）没有 text 字段时按字段兜底拼一份，而不是发空文档。"""
    legacy = candidate("c1", alias="订单金额", description="订单成交金额", table_name="fact_order")

    assert document_text(legacy) == "订单金额。c1。fact_order。订单成交金额"
    assert document_text(candidate("c1", text="原文")) == "原文"


# ==========================================================================
# 检索器的字段映射（payload / _source -> Candidate）
# ==========================================================================


def test_dense_payload_maps_to_a_column_candidate() -> None:
    got = _from_payload(
        "column",
        "cid-1",
        0.83,
        {
            "table_name": "fact_order",
            "column_name": "order_amount",
            "column_alias": "订单金额",
            "description": "订单成交金额",
            "text": "订单金额（order_amount）",
        },
    )

    assert (got.kind, got.id, got.name, got.table_name, got.alias) == (
        "column",
        "cid-1",
        "order_amount",
        "fact_order",
        "订单金额",
    )
    assert got.score == 0.83 and got.source == "vector" and got.text.startswith("订单金额")


def test_fulltext_source_maps_to_a_value_candidate() -> None:
    got = _from_source(
        "value",
        {
            "column_id": "cid-9",
            "table_name": "dim_region",
            "column_name": "region_name",
            "column_alias": "大区",
            "value": "华东",
        },
        4.2,
    )

    assert (got.kind, got.id, got.name, got.column_name) == ("value", "cid-9", "华东", "region_name")
    assert got.source == "fulltext"
    assert got.text == "大区（region_name）：华东", "老索引没有 text 字段时按同样规则拼一份"


def test_fulltext_source_maps_to_a_metric_candidate() -> None:
    got = _from_source(
        "metric",
        {"metric_id": "mid-1", "metric_name": "GMV", "metric_alias": "成交总额", "text": "GMV（成交总额）"},
        3.1,
    )

    assert (got.kind, got.id, got.name, got.alias) == ("metric", "mid-1", "GMV", "成交总额")
    assert got.text == "GMV（成交总额）"


def test_rank_by_score_is_stable_on_ties() -> None:
    ranked = rank_by_score([candidate("b2", score=1.0), candidate("a1", score=1.0)])

    assert [c.id for c in ranked] == ["a1", "b2"]


# ==========================================================================
# 重排客户端：两种协议形状与失败路径
# ==========================================================================


@pytest.fixture
async def mock_rerank(monkeypatch):
    """把客户端单例换成走 ``MockTransport`` 的实例，并记录每次请求。"""
    clients: list[httpx.AsyncClient] = []

    def install(handler):
        calls: list[httpx.Request] = []

        def wrapped(request: httpx.Request) -> httpx.Response:
            calls.append(request)
            return handler(request, len(calls))

        client = httpx.AsyncClient(
            transport=httpx.MockTransport(wrapped),
            base_url="http://rerank.test",
            timeout=1,
        )
        clients.append(client)
        monkeypatch.setattr(rerank_client, "_client", client)
        return calls

    yield install
    for client in clients:
        await client.aclose()


async def test_tei_shaped_response_is_sorted_by_score(mock_rerank) -> None:
    def handler(request: httpx.Request, _attempt: int) -> httpx.Response:
        return httpx.Response(200, json=[{"index": 0, "score": 0.2}, {"index": 1, "score": 0.9}])

    mock_rerank(handler)
    ranked = await rerank_client.rerank("问句", ["低相关", "高相关"])

    assert [(r.index, r.score) for r in ranked] == [(1, 0.9), (0, 0.2)]


async def test_openai_shaped_response_is_understood_too(mock_rerank) -> None:
    """SiliconFlow / Jina 这类云服务返回 ``{"results": [{"relevance_score": ...}]}``。"""
    mock_rerank(
        lambda request, attempt: httpx.Response(
            200, json={"results": [{"index": 1, "relevance_score": 0.8}]}
        )
    )

    ranked = await rerank_client.rerank("问句", ["一", "二"])

    assert [(r.index, r.score) for r in ranked] == [(1, 0.8)]


async def test_request_carries_model_query_and_documents(mock_rerank) -> None:
    calls = mock_rerank(
        lambda request, attempt: httpx.Response(200, json={"results": [{"index": 0, "score": 1.0}]})
    )

    await rerank_client.rerank("去年销售额", ["文档一"], top_n=3)

    body = calls[0].content.decode()
    assert calls[0].url.path == "/rerank"
    assert "bge-reranker-v2-m3" in body and "去年销售额" in body and "文档一" in body
    assert '"top_n":3' in body


async def test_top_n_is_omitted_when_not_asked_for(mock_rerank) -> None:
    calls = mock_rerank(
        lambda request, attempt: httpx.Response(200, json={"results": [{"index": 0, "score": 1.0}]})
    )

    await rerank_client.rerank("q", ["d"])

    assert "top_n" not in calls[0].content.decode()


async def test_a_4xx_is_reported_immediately_without_retrying(mock_rerank) -> None:
    """模型名写错、超长、鉴权失败都不该重试——重试只是把同一个错误等三遍。"""
    calls = mock_rerank(lambda request, attempt: httpx.Response(422, json={"detail": "bad model"}))

    with pytest.raises(ServiceError, match="422"):
        await rerank_client.rerank("q", ["d"])

    assert len(calls) == 1


async def test_a_5xx_is_retried_once(mock_rerank, monkeypatch) -> None:
    monkeypatch.setattr(rerank_client, "RETRY_BACKOFF_SECONDS", 0)

    def handler(request: httpx.Request, attempt: int) -> httpx.Response:
        if attempt == 1:
            return httpx.Response(503, text="overloaded")
        return httpx.Response(200, json=[{"index": 0, "score": 1.0}])

    calls = mock_rerank(handler)
    ranked = await rerank_client.rerank("q", ["d"])

    assert len(calls) == 2 and ranked[0].score == 1.0


async def test_an_unreachable_service_raises_a_service_error(mock_rerank, monkeypatch) -> None:
    monkeypatch.setattr(rerank_client, "RETRY_BACKOFF_SECONDS", 0)

    def handler(request: httpx.Request, _attempt: int) -> httpx.Response:
        raise httpx.ConnectError("connection refused", request=request)

    mock_rerank(handler)

    with pytest.raises(ServiceError, match="不可用"):
        await rerank_client.rerank("q", ["d"])


async def test_empty_input_short_circuits(mock_rerank) -> None:
    calls = mock_rerank(lambda request, attempt: httpx.Response(500))

    assert await rerank_client.rerank("q", []) == []
    assert await rerank_client.rerank("   ", ["d"]) == []
    assert calls == [], "没有候选可排时不该打扰服务"


async def test_a_non_json_response_says_so(mock_rerank) -> None:
    """多半是 path 配错了（指到了网页或代理兜底页），报错要说清是响应形状问题。"""
    mock_rerank(lambda request, attempt: httpx.Response(200, text="<html>hello</html>"))

    with pytest.raises(ServiceError, match="不是 JSON"):
        await rerank_client.rerank("q", ["d"])


async def test_an_out_of_range_index_is_refused(mock_rerank) -> None:
    """下标越界说明双方对 documents 的理解不一致，宁可报错也不猜。"""
    mock_rerank(
        lambda request, attempt: httpx.Response(200, json=[{"index": 5, "score": 1.0}])
    )

    with pytest.raises(ServiceError, match="越界"):
        await rerank_client.rerank("q", ["只有一条"])
