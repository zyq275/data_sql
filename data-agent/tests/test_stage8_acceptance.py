"""阶段 8 验收：可解释性与可视化推荐（《开发步骤文档》阶段 8 的四条验收标准）。

  1. 结果页展示 SQL 与解释
  2. 解释与 SQL 实际逻辑一致
  3. 图表类型推荐符合规则（单维+度量→柱状、时间+度量→折线、占比→饼、多列明细→表格）
  4. 结果为空时不推荐图表，提示"无数据"

第 1、2 条走**真实的**问答链路（真模型、真检索、真数仓）：解释是模型写的，
"它有没有照着实际 SQL 说"只能由真模型的输出来回答。用桩模型测这一条，
测的是"我们把 SQL 拼进了提示词"，而拼错了的提示词照样能让桩给出预期输出。

第 3、4 条本可以用纯函数验（``test_visualization.py`` 已经逐条验过规则），
这里仍然**再走一遍真实链路**，因为要验收的不是"规则函数算得对"，而是
"一次真问答推出来的 viz 事件符合规则"——中间还隔着图有没有接对、
事件有没有推出、结果有没有传进节点。规则对而接线错，是这两处最容易漏的失配。

每个用例一个独占用户（同 test_stage7_acceptance 的理由：这些用例真往三张表里写，
而历史不会随会话删除，共用一个 user_id 会让"该有几条"取决于执行顺序）。
"""

from __future__ import annotations

import itertools
import json
import uuid
from types import SimpleNamespace

import httpx
import pytest

from app.core.security import get_current_user
from main import app

#: 整份文件都要真模型：图表推荐本身是纯计算，但每条用例都得先让模型把问句
#: 变成 SQL 才能走到那一步。标 requires_llm 而不是只标 requires_stack——
#: requires_stack 不检查 LLM Key，只标它会得到"没有 Key 时在这里报一串连接错误"
#: 而不是"跳过"（见 conftest 的 pytest_runtest_setup）。
pytestmark = [pytest.mark.requires_stack, pytest.mark.requires_llm]

BASE_URL = "http://test"
RUN = uuid.uuid4().hex[:8]
_serial = itertools.count()

#: 数仓里种子数据的区间是 2024-01-01 至今（scripts/seed_dw.py），2023 年必定为空。
#: 用"年份越界"而不是"编一个不存在的地区"来造空结果：后者依赖模型把那个字符串
#: 原样写进 WHERE，而它完全可能判断"这个地区不在表里"从而换一种写法。
EMPTY_QUESTION = "2023年各季度销售额"

#: 种子数据里的大区（scripts/seed_dw.py 的 REGIONS）。用来验"解释有没有编造
#: SQL 里不存在的过滤条件"——编造出来的多半就是同一层级的另一个大区。
ALL_REGIONS = ("华东", "华南", "华北", "华中", "西南", "西北", "东北")

#: SQL 里出现这些就说明模型没在"解释"，而是在复述语句。
SQL_SMELLS = ("SELECT", "GROUP BY", "ORDER BY", "JOIN", "WHERE", "FROM ")

current: list[SimpleNamespace] = []


def new_user(label: str = "t") -> SimpleNamespace:
    tag = f"{label}{next(_serial)}"
    return SimpleNamespace(
        id=f"u-acc8-{tag}-{RUN}", username=f"acc8-{tag}", role="analyst", is_active=True
    )


@pytest.fixture(autouse=True)
def _authenticated():
    current[:] = [new_user()]
    app.dependency_overrides[get_current_user] = lambda: current[0]
    yield
    app.dependency_overrides.pop(get_current_user, None)


def _client() -> httpx.AsyncClient:
    return httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url=BASE_URL, timeout=180.0
    )


def parse_sse(body: str) -> list[tuple[str, dict]]:
    frames: list[tuple[str, dict]] = []
    for block in body.strip().split("\n\n"):
        if not block.strip():
            continue
        lines = block.splitlines()
        name = next(line[len("event: ") :] for line in lines if line.startswith("event: "))
        data = next(line[len("data: ") :] for line in lines if line.startswith("data: "))
        frames.append((name, json.loads(data)))
    return frames


def payload_of(frames: list[tuple[str, dict]], kind: str) -> dict:
    """取第一帧该类事件。没有就直接失败——缺事件是本阶段的主要故障形态。"""
    for name, data in frames:
        if name == kind:
            return data
    raise AssertionError(f"事件流里没有 {kind} 帧：{[n for n, _ in frames]}")


async def ask(question: str) -> list[tuple[str, dict]]:
    async with _client() as client:
        resp = await client.post("/api/v1/query", json={"query": question})
    assert resp.status_code == 200, resp.text
    frames = parse_sse(resp.text)
    assert not any(name == "error" for name, _ in frames), frames
    assert frames[-1][0] == "done", f"问答没有正常结束：{frames[-1][1]}"
    return frames


def describe(frames: list[tuple[str, dict]]) -> str:
    """失败时把结果长什么样带进断言消息，省得回头翻日志。"""
    result = payload_of(frames, "result")["data"]
    return f"列={result['columns']} 行数={result['row_count']} 首行={result['rows'][:1]}"


# ==========================================================================
# 验收标准 1：结果页展示 SQL 与解释
# ==========================================================================


async def test_sql_and_explanation_both_reach_the_client() -> None:
    """一次成功问答要同时推出 sql 与 explain 两条事件（验收标准第 1 条）。

    判据是"两条事件都在且解释非空"，而不是"没有报错"：解释是 P1 且允许降级
    （见 nodes/generate_explain），降级时事件流依旧完整、依旧 done，
    唯一区别就是少一条 explain——那种情况下这个用例必须红。
    """
    frames = await ask("华东地区各季度销售额")

    sql = payload_of(frames, "sql")["sql"]
    text = payload_of(frames, "explain")["text"]

    assert sql.strip(), "SQL 不能为空"
    assert text.strip(), "解释不能为空"
    # 解释要在结果之后：它得看着真实返回的行数才写得出来
    kinds = [name for name, _ in frames]
    assert kinds.index("result") < kinds.index("explain")


async def test_explanation_is_natural_language_not_the_sql_again() -> None:
    """解释得是"人话"，不是把 SQL 换个说法抄一遍（FR-EXPL-002）。

    这条最容易在实现走偏时静默通过：只要把 SQL 原样塞进 explain 事件，
    "解释非空"的断言照样绿，而用户看到的是同一段看不懂的东西。
    """
    frames = await ask("华东地区各季度销售额")
    sql = payload_of(frames, "sql")["sql"]
    text = payload_of(frames, "explain")["text"]

    assert text != sql
    for smell in SQL_SMELLS:
        assert smell not in text.upper(), f"解释里出现了 SQL 语句成分 {smell!r}：{text}"
    assert any("一" <= char <= "鿿" for char in text), f"解释应当是中文：{text}"


# ==========================================================================
# 验收标准 2：解释与 SQL 实际逻辑一致
# ==========================================================================


async def test_explanation_matches_what_the_sql_actually_does() -> None:
    """"解释与 SQL 实际逻辑一致"（验收标准第 2 条）。

    怎么把"一致"变成可判定的：挑一个带**显著过滤条件**的问题，然后
      - SQL 里确实筛了华东（前提，先自证用例本身成立），
      - 解释里必须提到华东——没提说明它没照着这条 SQL 说，
      - 解释里不能出现别的大区名字——出现了就是编造了 SQL 里没有的条件。

    反面那条比正面那条更能说明问题：模型复述 SQL 时几乎不会提错地区，
    但让它"补充解释一下"时，它很容易顺手写出一个上下文里没有的范围。
    """
    frames = await ask("华东地区各季度销售额")
    sql = payload_of(frames, "sql")["sql"]
    text = payload_of(frames, "explain")["text"]

    assert "华东" in sql, f"用例前提不成立——这条 SQL 并没有筛华东：{sql}"
    assert "华东" in text, f"解释没有提到 SQL 里真实的过滤条件：{text}"

    invented = [region for region in ALL_REGIONS if region != "华东" and region in text]
    assert not invented, f"解释里出现了 SQL 里没有的地区 {invented}：{text}"


async def test_explanation_does_not_claim_the_result_is_partial() -> None:
    """没被截断的结果，解释不能说成"只显示了部分数据"。

    这条是走查里**实测到的**瑕疵：给模型的样本只有几行，措辞一旦写成"前几行数据"，
    它就会照猫画虎地写出"上面展示的只是前几行，并非全部数据"——而用户那边 12 行
    是全的。一句话把一次完整的查询说成了截断的，而用户无从分辨。

    只在「结果确实没被截断」时断言：截断时要求它说明数据不完整（BR-SQL-004），
    两件事方向相反，混在一个断言里必然自相矛盾。
    """
    frames = await ask("华东地区各季度销售额")
    text = payload_of(frames, "explain")["text"]
    result = payload_of(frames, "result")["data"]

    if result["truncated"]:  # pragma: no cover —— 这条查询远达不到行数上限
        pytest.skip("这一轮结果被截断了，'数据不完整'的说法在这个用例里是正确表述")

    misleading = [phrase for phrase in ("部分数据", "只显示", "前几行", "并非全部", "不完整") if phrase in text]
    assert not misleading, f"结果完整，但解释暗示了数据不完整（{misleading}）：{text}"


# ==========================================================================
# 验收标准 3：图表类型推荐符合规则
# ==========================================================================


@pytest.mark.parametrize(
    ("question", "expected"),
    [
        ("各地区的销售额", "bar"),
        ("各季度销售额", "line"),
        ("各地区的销售额占比", "pie"),
        ("订单明细", "table"),
    ],
)
async def test_chart_type_follows_the_documented_rules(question: str, expected: str) -> None:
    """四条规则各走一遍真链路（FR-VIZ-001）。

    这四条问题都是**结构被问题本身钉死**的：问"各地区"必然是一维分组，
    问"各季度"必然落在 dim_date 的时间列上，问"占比"必然是 SUM/total，
    问"明细"必然是宽表。所以这里可以断言具体的类型，而不是"和规则函数算的一样"——
    后者是拿规则验规则，接错线也照样绿。
    """
    frames = await ask(question)
    viz = payload_of(frames, "viz")

    assert viz["chart_type"] == expected, f"{question} → {describe(frames)}"
    assert viz["message"], "viz 事件要带一句给人看的说明"


# ==========================================================================
# 验收标准 4：结果为空
# ==========================================================================


async def test_empty_result_recommends_no_chart_and_says_so() -> None:
    """结果为空不推荐图表，提示"无数据"（FR-VIZ-001 异常分支）。

    两件事都要验：不给图表类型（None，不是 "table"），且把话说出来。
    只验前者的话，前端拿到 None 却不知道该显示什么，只能自己编一句。
    """
    frames = await ask(EMPTY_QUESTION)
    result = payload_of(frames, "result")["data"]
    viz = payload_of(frames, "viz")

    assert result["row_count"] == 0, f"用例前提不成立——这条查询居然有数据：{describe(frames)}"
    assert viz["chart_type"] is None, f"空结果不该推荐图表：{viz}"
    assert viz["message"] == "无数据", f"提示语要与 PRD 原文一致：{viz}"


async def test_an_empty_result_is_still_explained() -> None:
    """没有数据 ≠ 没有解释。

    解释说的是"这条 SQL 干了什么"，0 行也是它的一个结果。此时不解释，
    用户面对一个空白页，连"我是不是问错了"都判断不了。
    """
    frames = await ask(EMPTY_QUESTION)
    text = payload_of(frames, "explain")["text"]

    assert text.strip(), "空结果同样要有解释"


# ==========================================================================
# 解释的落库（阶段 7 留下的 NULL 占位，到此关闭）
# ==========================================================================


async def test_the_explanation_is_persisted_into_the_history_row() -> None:
    """解释要跟着这一轮一起落库，历史详情里能再看到它。

    阶段 7 的 ``_persist`` 把 ``explain_text`` 如实写成 NULL 并注明"由阶段 8 产出"，
    这条用例就是那句话的兑现。钉在**历史详情接口**上而不是直接查库：
    用户看到的那一份才是要保证的那一份。
    """
    frames = await ask("华东地区各季度销售额")
    text = payload_of(frames, "explain")["text"]

    async with _client() as client:
        listing = (await client.get("/api/v1/history?page_size=1")).json()["data"]
        assert listing["total"] == 1, "这个用户只问过一次，历史里就该只有一条"
        detail = (await client.get(f"/api/v1/history/{listing['items'][0]['id']}")).json()["data"]

    assert detail["explain_text"] == text, "历史里存的解释要与这次推给用户的一致"
