"""阶段 6 验收：混合检索（《开发步骤文档》阶段 6 的三条验收标准）。

  1. 两路召回结果经 RRF 融合
  2. rerank 精排后相关项位于不相关项之前
  3. 召回命中率达标

前两条对着**真实的两路索引**（Qdrant + ES）验证，整个模块标记 requires_stack。

关于第 2 条的一个坦白：精排的**排序质量**取决于所用模型，本环境没有重排服务
（RERANK__HOST 指向的端口没起），所以这里用**本地桩服务**验证"接入与重排机制"：
桩按一条可复现的规则打分（文档含指定词得 1 分，否则 0 分），断言网关确实按
精排服务的分数重排了顺序。真实模型的精排效果由 requires_rerank 标记的用例覆盖，
最终质量由阶段 10 的评测集度量——用桩测出"模型排得对"是自欺。

第 3 条的检索词是写死的，不由 LLM 现场扩展：本阶段验收的是**检索层**，
关键词扩展的质量属于阶段 5 那一层，混进来会让"召回率不达标"无法归因。
"""

from __future__ import annotations

import json
import socket
import threading
from collections.abc import Iterator, Sequence
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import httpx
import pytest

from app.clients import es as es_client
from app.clients import rerank as rerank_client
from app.core.config import get_settings
from app.retrievers.candidate import Candidate
from app.retrievers.dense import DenseRetriever
from app.retrievers.fulltext import FullTextRetriever
from app.retrievers.fusion import RRF_K, reciprocal_rank_fusion
from app.retrievers.retrieval import RetrievalGateway

pytestmark = pytest.mark.requires_stack

#: 阶段 4 承诺过"正确项一定进候选集"。混合检索只能让这个承诺更强，不能更弱，
#: 所以这条门槛是硬性的 100%。
RECALL_AT_3_THRESHOLD = 1.0

#: top-1 精度门槛。阶段 4 明文写的是"保证召回@k，不保证 top-1"（单路向量分不开
#: 近义词），融合之后这个数字才有意义，门槛定在这里而不是阶段 4。
#:
#: 本机实测 100%（19 条语料全部第 1 名）。门槛仍留在 85% 而不是跟着实测拉满，
#: 是因为两者性质不同：召回@3 是对下游的承诺（少一条就少一个可选项），
#: top-1 是质量指标——本阶段的语料是手写的、关键词与库里的别名高度对齐，
#: 阶段 7 之后问法由模型现场扩展、语料还会扩大，用一个恰好等于当前观测值的门槛
#: 会把每次正常的质量波动都变成红灯。真正该盯住的是随语料扩大的趋势。
TOP1_THRESHOLD = 0.85


# ==========================================================================
# 验收标准 1：两路召回结果经 RRF 融合
# ==========================================================================


async def test_the_two_paths_are_fused_into_one_ranking() -> None:
    """字段/指标的两路各自召回，网关给出的是按 RRF 融合后的**一路**结果。

    逐个候选核对：分数必须等于它在两路里名次倒数之和——不是"看起来融合了"，
    而是与定义逐项相符。任何一路缺席、名次错位、去重出错都会让它对不上。
    """
    settings = get_settings()
    keywords = ["销售额"]
    dense_rank = await DenseRetriever().retrieve(
        keywords, collection=settings.qdrant.metric_collection, kind="metric"
    )
    text_rank = await FullTextRetriever().retrieve(
        keywords, index=settings.es.metric_index, field="text", kind="metric"
    )
    assert dense_rank and text_rank, "两路都必须有结果，否则谈不上融合"

    fused = await RetrievalGateway(reranker=None).metrics(keywords, query="去年的销售额是多少")

    dense_pos = {c.id: rank for rank, c in enumerate(dense_rank, start=1)}
    text_pos = {c.id: rank for rank, c in enumerate(text_rank, start=1)}
    assert {c.id for c in fused} == set(dense_pos) | set(text_pos), "融合结果必须覆盖两路的并集"

    for candidate in fused:
        expected = sum(
            1 / (RRF_K + positions[candidate.id])
            for positions in (dense_pos, text_pos)
            if candidate.id in positions
        )
        assert candidate.score == pytest.approx(expected), (
            f"{candidate.name} 的融合分 {candidate.score} 与两路名次算出的 {expected} 不符"
        )
        in_dense, in_text = candidate.id in dense_pos, candidate.id in text_pos
        expected_source = "hybrid" if in_dense and in_text else ("vector" if in_dense else "fulltext")
        assert candidate.source == expected_source, (
            f"{candidate.name} 的来源标成 {candidate.source}，按两路命中情况应为 {expected_source}"
        )


async def test_a_consensus_hit_outranks_a_single_path_hit_on_real_indexes() -> None:
    """真实索引上：两路都命中的项排在只被一路命中的项之前。

    这是 RRF 全部的意义，也是"融合"与"把两个列表拼起来"的分界线。
    """
    fused = await RetrievalGateway(reranker=None).metrics(
        ["销售额"], query="去年的销售额是多少"
    )

    consensus = [i for i, c in enumerate(fused) if c.source == "hybrid"]
    single = [i for i, c in enumerate(fused) if c.source != "hybrid"]
    assert consensus and single, (
        f"这条问法本该同时出现「两路都命中」与「只有一路命中」两类候选，"
        f"实际 consensus={consensus} single={single}："
        f"{[(c.name, c.source) for c in fused]}"
    )
    assert max(consensus) < min(single)


async def test_the_fulltext_path_fixes_the_near_synonym_case() -> None:
    """阶段 4 留下的那个已知限制——「销售额」被向量排到「销量」之前——应该被融合纠正。

    向量那一路至今仍把 ``sales_quantity`` 排在 ``GMV`` 前面（下面的断言会把这件事
    钉住），但指标的 description 里写着"口语常称销售额、营收、卖了多少钱"，
    全文那一路能命中 GMV，融合之后 GMV 必须回到第一。

    这条用例挂了，说明第二路没起作用、或者退化成了"两路等权拼接"，
    而不是"模型变差了"——这正是判据要写在用例里而不是写在提交说明里的原因。
    """
    settings = get_settings()
    vector_top = (
        await DenseRetriever().retrieve(
            ["销售额"], collection=settings.qdrant.metric_collection, kind="metric"
        )
    )[0].name
    assert vector_top == "sales_quantity", (
        f"单路向量的 top-1 变成了 {vector_top}，与 TDD 8.3 记录的已知限制不符——"
        "若确实换了 embedding 模型，请同步更新 TDD 与这条用例"
    )

    fused = await RetrievalGateway(reranker=None).metrics(["销售额"], query="去年的销售额是多少")

    assert fused[0].name == "GMV", (
        f"融合后 top-1 是 {fused[0].name}（{fused[0].source}），期望 GMV"
    )


# ==========================================================================
# 验收标准 2：rerank 精排后相关项位于不相关项之前
# ==========================================================================


class _StubRerankHandler(BaseHTTPRequestHandler):
    """桩重排服务：文档里含有 ``server.wanted`` 就得 1 分，否则 0 分。

    按内容打分而不是返回固定序列，是为了让它**必须真的读到文档**才能排对——
    一个不看文档就返回预设顺序的桩，测不出"文档有没有正确送到服务端"。
    """

    protocol_version = "HTTP/1.1"

    def do_POST(self) -> None:  # noqa: N802 —— BaseHTTPRequestHandler 的接口
        length = int(self.headers.get("Content-Length", 0))
        payload = json.loads(self.rfile.read(length))
        self.server.received.append(payload)  # type: ignore[attr-defined]
        wanted = self.server.wanted  # type: ignore[attr-defined]

        body = json.dumps(
            [
                {"index": index, "score": 1.0 if wanted in doc else 0.0}
                for index, doc in enumerate(payload["documents"])
            ],
            ensure_ascii=False,
        ).encode()
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, *args) -> None:
        """桩服务不打访问日志（会淹掉 pytest 的输出）。"""


def _free_port() -> int:
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return sock.getsockname()[1]


@pytest.fixture
async def stub_rerank(monkeypatch) -> Iterator[ThreadingHTTPServer]:
    """起一个桩重排服务，并把客户端指向它。"""
    server = ThreadingHTTPServer(("127.0.0.1", _free_port()), _StubRerankHandler)
    server.received = []  # type: ignore[attr-defined]
    server.wanted = ""  # type: ignore[attr-defined]
    threading.Thread(target=server.serve_forever, daemon=True).start()

    client = httpx.AsyncClient(base_url=f"http://127.0.0.1:{server.server_port}", timeout=5)
    monkeypatch.setattr(rerank_client, "_client", client)
    try:
        yield server
    finally:
        await client.aclose()
        server.shutdown()
        server.server_close()


async def test_rerank_puts_the_relevant_candidate_first(stub_rerank) -> None:
    """精排服务说"这条相关"，它就必须排在前面——即使融合顺序不这么认为。"""
    stub_rerank.wanted = "成交总额"  # type: ignore[attr-defined]

    fused = await RetrievalGateway(reranker=None).metrics(["销售额", "订单量"], query="去年的销售额")
    reranked = await RetrievalGateway().metrics(["销售额", "订单量"], query="去年的销售额")

    assert fused[0].name != "GMV", (
        f"融合顺序已经把它排在第一了（{[(c.name, round(c.score, 4)) for c in fused]}），"
        "这条用例就测不出精排做了什么——请改换检索词"
    )
    assert reranked[0].name == "GMV", (
        f"精排后 top-1 应为被判为相关的 GMV，实际是 {reranked[0].name}"
    )
    assert reranked[0].score == 1.0, "分数必须换成精排服务给的分数"
    assert [c.name for c in reranked] == ["GMV"] + [c.name for c in fused if c.name != "GMV"], (
        "未被判为相关的候选应保持融合顺序跟在后面"
    )


async def test_rerank_receives_the_text_the_indexes_saw(stub_rerank) -> None:
    """送到精排服务的文档必须是建库时那段原文。

    否则"排的依据"与"召的依据"分叉：名次对不上，而且无从解释。
    """
    stub_rerank.wanted = "成交总额"  # type: ignore[attr-defined]
    await RetrievalGateway().metrics(["销售额"], query="去年的销售额是多少")

    sent = stub_rerank.received[0]["documents"]  # type: ignore[attr-defined]
    assert sent, "网关没有向精排服务送出任何文档"

    indexed = {
        hit["_source"]["text"]
        for hit in (
            await es_client.get_es().search(
                index=get_settings().es.metric_index, query={"match_all": {}}, size=10
            )
        )["hits"]["hits"]
    }
    assert set(sent) <= indexed, f"送出的文档里有不属于索引原文的文本：{set(sent) - indexed}"
    assert all(len(text) > 5 for text in sent), f"送出的文档太单薄，不像拼好的整段原文：{sent}"


@pytest.mark.requires_rerank
async def test_a_real_rerank_service_ranks_the_relevant_item_first() -> None:
    """配了真实重排服务时：问"销售额"，GMV 必须排在销量之前。

    本环境通常跳过（没有重排服务）。它是给"接了真模型之后"准备的验收——
    桩服务只能证明机制通了，证明不了模型排得对。
    """
    result = await RetrievalGateway().metrics(["销售额"], query="去年的销售额是多少")

    assert result[0].name == "GMV", (
        f"真实精排后 top-1 是 {result[0].name}，期望 GMV："
        f"{[(c.name, round(c.score, 3)) for c in result]}"
    )


# ==========================================================================
# 验收标准 3：召回命中率达标
# ==========================================================================

METRIC_CASES = [
    ("去年销售额是多少", ["销售额"], "GMV"),
    ("一共卖了多少件", ["销量"], "sales_quantity"),
    ("订单量是多少", ["订单量"], "order_count"),
    ("平均每单花多少钱", ["客单价"], "avg_order_amount"),
    ("有多少活跃客户", ["活跃客户数"], "active_customer_count"),
]

COLUMN_CASES = [
    ("订单金额合计", ["订单金额"], "order_amount"),
    ("各省份的销售额", ["省份"], "province"),
    ("商品类目有哪些", ["商品类目"], "category"),
    ("按会员等级分组", ["会员等级"], "member_level"),
    ("下单日期范围", ["下单日期"], "date_id"),
    ("客户姓名列表", ["客户姓名"], "customer_name"),
    ("买了多少件", ["订单数量"], "order_quantity"),
]

VALUE_CASES = [
    ("华东区卖了多少", ["华东"], "region_name"),
    ("浙江省的订单", ["浙江省"], "province"),
    ("金卡会员有多少", ["金卡"], "member_level"),
    ("手机数码类目", ["手机数码"], "category"),
    ("已支付的订单", ["已支付"], "order_status"),
]


async def _measure(gateway: RetrievalGateway, cases: Sequence, *, kind: str) -> list[tuple[str, int]]:
    """跑一遍语料，返回 [(案由, 命中名次)]；未命中记 0。"""
    measured: list[tuple[str, int]] = []
    for question, keywords, expected in cases:
        candidates = await getattr(gateway, {"metric": "metrics", "column": "columns", "value": "values"}[kind])(
            keywords, query=question
        )
        names = [c.name for c in candidates] if kind != "value" else [c.column_name or "" for c in candidates]
        measured.append(
            (f"{question}→{expected}", names.index(expected) + 1 if expected in names else 0)
        )
    return measured


def _report(measured: Sequence[tuple[str, int]]) -> str:
    return " | ".join(f"{label}:{'第' + str(rank) if rank else '未命中'}" for label, rank in measured)


async def test_recall_hit_rate_meets_the_bar_on_all_three_paths() -> None:
    """三条路的召回率。不达标时报出逐条明细——只知道"不达标"没法修。"""
    gateway = RetrievalGateway(reranker=None)  # 只看融合本身，不掺精排

    for kind, cases in (("metric", METRIC_CASES), ("column", COLUMN_CASES), ("value", VALUE_CASES)):
        measured = await _measure(gateway, cases, kind=kind)
        hit_at_3 = sum(1 for _label, rank in measured if 0 < rank <= 3) / len(measured)
        print(f"\n{kind} 召回@3 {hit_at_3:.0%}：" + _report(measured))
        assert hit_at_3 >= RECALL_AT_3_THRESHOLD, (
            f"{kind} 的召回@3 只有 {hit_at_3:.0%}，低于阶段 4 承诺的 "
            f"{RECALL_AT_3_THRESHOLD:.0%}：{_report(measured)}"
        )


@pytest.mark.parametrize(
    ("kind", "cases"),
    [("metric", METRIC_CASES), ("column", COLUMN_CASES), ("value", VALUE_CASES)],
)
async def test_top1_accuracy_meets_the_bar(kind: str, cases) -> None:
    """top-1 精度：阶段 4 明确不承诺这一项，融合之后它才有意义。"""
    gateway = RetrievalGateway(reranker=None)
    measured = await _measure(gateway, cases, kind=kind)
    top1 = sum(1 for _label, rank in measured if rank == 1) / len(measured)

    print(f"\n{kind} top-1 {top1:.0%}：" + _report(measured))
    assert top1 >= TOP1_THRESHOLD, (
        f"{kind} 的 top-1 命中率 {top1:.0%} 低于 {TOP1_THRESHOLD:.0%}：{_report(measured)}"
    )


async def test_fusion_never_loses_a_candidate_the_vector_path_found() -> None:
    """"加了一路"不该让召回变差：向量那一路原先能命中的项，融合后仍须在候选集里。

    这是混合检索最容易出的事故——第二路在名次上挤掉了位置，看起来"更先进了"，
    实际候选集变小。逐条比对单路与融合后的结果。
    """
    settings = get_settings()
    gateway = RetrievalGateway(reranker=None)
    dense = DenseRetriever()

    for kind, cases, collection in (
        ("column", COLUMN_CASES, settings.qdrant.column_collection),
        ("metric", METRIC_CASES, settings.qdrant.metric_collection),
    ):
        call = gateway.columns if kind == "column" else gateway.metrics
        for question, keywords, expected in cases:
            vector_only = await dense.retrieve(keywords, collection=collection, kind=kind)
            if expected not in [c.name for c in vector_only][:3]:
                continue  # 单路本来就召不回，不构成"融合把它弄丢了"
            fused = await call(keywords, query=question)
            assert expected in [c.name for c in fused], (
                f"「{question}」单路向量能召回 {expected}，融合后却丢了："
                f"{[(c.name, c.source) for c in fused]}"
            )


# ==========================================================================
# 融合只许改分数与顺序，不许改身份
# ==========================================================================


def test_candidate_identity_survives_fusion_and_reranking() -> None:
    """下游靠 ``id`` 回查元数据，融合与精排一旦动了 kind/id/name，回查就会查错东西。"""
    merged = reciprocal_rank_fusion(
        [
            [Candidate(kind="column", id="cid-1", name="province", score=0.9)],
            [Candidate(kind="column", id="cid-1", name="province", score=5.0, source="fulltext")],
        ]
    )

    assert [(c.kind, c.id, c.name) for c in merged] == [("column", "cid-1", "province")]
