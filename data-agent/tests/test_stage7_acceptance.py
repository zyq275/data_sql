"""阶段 7 验收：多轮会话与查询历史（《开发步骤文档》阶段 7 的六条验收标准）。

  1. 先问"各季度销售额"，追问"只看华东"能正确理解上下文
  2. 指代消解把追问改写为独立完整的问句，首轮无历史时原样透传
  3. 会话状态持久化，重启后可继续
  4. 会话仅本人可见；会话详情返回该会话的消息列表
  5. 历史列表分页、可按状态/关键词筛选，仅本人可见
  6. 历史详情含问题/SQL/结果预览/解释/耗时/状态

第 1、3 条走**真实的**问答链路（真模型、真检索、真数仓），因为"上下文理解对不对"
只能由端到端结果回答：拿桩模型测多轮，测的是"我们把上文拼进了提示词"，
而拼错了的提示词照样能让桩给出预期输出。

第 4~6 条断言的是**真实查询产生出来的**那些行，而不是手工插进去的行——
那正是它们与 test_conversation_api / test_history_api 的区别：那边验接口的规则，
这边验"一轮真问答有没有被如实记下来"。

第 2 条单独直接用节点 + 真模型验，因为改写结果不出现在 SSE 事件里：
它只活在 state 里。要看见它，要么翻日志，要么直接调那个节点。这里选后者。
"""

from __future__ import annotations

import itertools
import json
import re
import uuid
from types import SimpleNamespace

import httpx
import pytest

from app.agent.checkpoint import STATE_TYPES
from app.agent.context import QueryContext
from app.agent.nodes.resolve_reference import resolve_reference
from app.agent.redis_checkpointer import RedisCheckpointer
from app.clients.redis import get_redis
from app.core.security import get_current_user
from main import app

pytestmark = pytest.mark.requires_stack

BASE_URL = "http://test"

FIRST_QUESTION = "各季度销售额"
FOLLOW_UP = "只看华东"

#: 本次运行的前缀。三张表里都会留下这些行的痕迹，跨运行时用不同的前缀区分开。
RUN = uuid.uuid4().hex[:8]
_serial = itertools.count()

#: 当前扮演的用户。每个用例开头都会被换成**一个全新的 id**（见 ``new_user``）。
current: list[SimpleNamespace] = []


def new_user(label: str = "t") -> SimpleNamespace:
    """一个本次运行内独占的用户 id。

    验收会真的往 conversation / message / query_history 三张表里写数据，而这些表
    没有"跑完自动清掉"这回事——历史尤其：删会话不会删历史，历史本来就是独立的。
    各用例共用同一个 user_id 的话，"列表里应该有几条""筛选该命中哪一条"就会被
    上一个用例的残留搅乱，而且表现取决于执行顺序。一个用例一个用户，把这件事从
    "记得清理"变成"结构上不可能串"。
    """
    tag = f"{label}{next(_serial)}"
    return SimpleNamespace(
        id=f"u-acc7-{tag}-{RUN}", username=f"acc7-{tag}", role="analyst", is_active=True
    )


@pytest.fixture(autouse=True)
def _authenticated():
    current[:] = [new_user()]
    app.dependency_overrides[get_current_user] = lambda: current[0]
    yield
    app.dependency_overrides.pop(get_current_user, None)


def _client() -> httpx.AsyncClient:
    return httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url=BASE_URL, timeout=180.0)


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
    return next(data for name, data in frames if name == kind)


def main_table(sql: str) -> str:
    """SQL 的主表名（第一个 FROM 后面的那个）。

    表名外面可能套着反引号——生成侧的表名一律带引号（``FROM `fact_order` f``），
    认不出来就会把"这张 SQL 写得对不对"误判成"这张 SQL 认不出主表"。
    """
    match = re.search(r"\bFROM\s+`?([A-Za-z_][A-Za-z0-9_]*)`?", sql, re.IGNORECASE)
    assert match, f"生成的 SQL 里认不出主表：{sql}"
    return match.group(1).lower()


async def ask(question: str, *, session_id: str | None = None) -> list[tuple[str, dict]]:
    body: dict = {"query": question}
    if session_id:
        body["session_id"] = session_id
    async with _client() as client:
        resp = await client.post("/api/v1/query", json=body)
    assert resp.status_code == 200, resp.text
    return parse_sse(resp.text)


def assert_succeeded(frames: list[tuple[str, dict]]) -> None:
    """问答成功：最后一条是 done 而不是 error。

    失败时把 error 事件的原文带进断言消息——否则只看到"没有 done"，
    还得回头翻日志才知道模型说了什么。
    """
    assert not any(name == "error" for name, _ in frames), frames
    last_name, last_data = frames[-1]
    assert last_name == "done", f"问答没有正常结束：{last_data}"


async def create_conversation(title: str | None = None) -> str:
    async with _client() as client:
        resp = await client.post("/api/v1/conversations", json={"title": title})
    assert resp.status_code == 200, resp.text
    return resp.json()["data"]["id"]


async def delete_conversation(conversation_id: str) -> None:
    async with _client() as client:
        await client.delete(f"/api/v1/conversations/{conversation_id}")


def fresh_reader() -> RedisCheckpointer:
    """装作"另一个进程"，直接去 Redis 读这份状态。

    ``with_allowlist`` 要与运行期一致（``agent.checkpoint`` 里也是这么装配的）：
    图状态里有 Candidate / TableSpec 这些应用自定义类型，未登记的 serde 解不出来。
    少这一步，读到的就不是"重启后能不能续上"，而是"这个读者自己配错了"。
    """
    return RedisCheckpointer(get_redis()).with_allowlist(STATE_TYPES)


async def read_thread_state(thread_id: str):
    return await fresh_reader().aget_tuple({"configurable": {"thread_id": thread_id}})


# ==========================================================================
# 验收标准 1：多轮上下文
# ==========================================================================


async def test_follow_up_understands_the_context_of_the_previous_turn() -> None:
    """先问"各季度销售额"，再追问"只看华东"（验收标准第 1 条）。

    判据落在**追问生成的 SQL**上，而不是"没有报错"：追问只说了"只看华东"，
    没说要算什么——它必须自己把上一轮问的那个量补上。上文没生效时，
    模型要么换个主题重答，要么答非所问；断"没报错"区分不出这两种。
    """
    conversation_id = await create_conversation("阶段 7 验收")

    first = await ask(FIRST_QUESTION, session_id=conversation_id)
    assert_succeeded(first)
    first_sql = payload_of(first, "sql")["sql"]
    subject = main_table(first_sql)

    follow = await ask(FOLLOW_UP, session_id=conversation_id)
    assert_succeeded(follow)
    follow_sql = payload_of(follow, "sql")["sql"]

    # 用"主表是否还在"而不是"两个 SQL 的主表是否完全相同"：追问多一个区域维度，
    # 写 SQL 时把 dim_region 提到 fact_order 前面 join 并不算理解错了。
    assert subject in follow_sql.lower(), (
        f"追问换了主表，说明上文没生效：\n第一轮 {first_sql}\n追问 {follow_sql}"
    )
    assert "华东" in follow_sql, f"追问新增的筛选条件没落进 SQL：{follow_sql}"

    # 会话里留下两轮，且顺序正确（早的在前）
    async with _client() as client:
        detail = (await client.get(f"/api/v1/conversations/{conversation_id}")).json()["data"]
    assert [m["query_text"] for m in detail["messages"]] == [FIRST_QUESTION, FOLLOW_UP]
    assert all(m["generated_sql"] for m in detail["messages"])

    await delete_conversation(conversation_id)


# ==========================================================================
# 验收标准 2：指代消解
# ==========================================================================


@pytest.mark.requires_llm
async def test_first_turn_passes_the_question_through_unchanged() -> None:
    """首轮无历史时原样透传（验收标准第 2 条）。

    用真模型跑这一条，正是为了证明"原样透传"不是因为模型刚好没改：
    没有历史时代码根本不发这次调用（``history`` 为空即直接返回）。
    """
    result = await resolve_reference(
        {"query": FIRST_QUESTION, "history": []},
        _runtime(),
    )

    assert result == {"resolved_query": FIRST_QUESTION}


@pytest.mark.requires_llm
async def test_reference_resolution_rewrites_a_follow_up_into_a_standalone_question() -> None:
    """带着上一轮的真实上下文，"只看华东"要被补全成一句独立完整的问题。

    断言的是"补上了上一轮的主题、又保留了本轮的新条件"，而不是某个具体的句子：
    改写由模型现场生成，钉死措辞等于把一次正常的措辞变化变成红灯。
    但两件事必须同时成立——只补主题会丢掉"只看华东"，只留"只看华东"则根本没改写。
    """
    history = [
        {
            "query": FIRST_QUESTION,
            "sql": (
                "SELECT quarter, SUM(order_amount) AS amount FROM fact_order "
                "GROUP BY quarter ORDER BY quarter"
            ),
        }
    ]

    result = await resolve_reference({"query": FOLLOW_UP, "history": history}, _runtime())

    resolved = result["resolved_query"]
    assert resolved != FOLLOW_UP, f"没有发生改写：{resolved}"
    assert "华东" in resolved, f"改写丢掉了本轮的新条件：{resolved}"
    assert any(word in resolved for word in ("季度", "销售额", "销售")), (
        f"改写没有补上上一轮的主题：{resolved}"
    )


def _runtime() -> SimpleNamespace:
    """节点的运行上下文：真模型 + 一个吃掉进度事件的 writer。

    进度事件在这里没有接收方（不经 SSE 出去），但仍要给一个可调用的 writer：
    节点外面套着 ``@step``，它无条件推 start/done。
    """
    return SimpleNamespace(
        context=QueryContext(user_id=current[0].id),
        stream_writer=lambda _event: None,
    )


# ==========================================================================
# 验收标准 3：状态持久化
# ==========================================================================


async def test_session_state_is_readable_from_a_fresh_process() -> None:
    """会话状态持久化，重启后可继续（验收标准第 3 条）。

    "重启"在这里的等价物是**换一个 checkpointer 实例去读**：新对象、新连接，
    唯一共享的是 Redis 里的那份数据。状态若只存在进程内（Redis 不可用时会降级成
    内存存储），这里读到的就是空。
    """
    conversation_id = await create_conversation("持久化验收")
    await ask(FIRST_QUESTION, session_id=conversation_id)

    restored = await read_thread_state(conversation_id)

    assert restored is not None, "Redis 里没有这个会话的状态，说明它只存在进程内"
    assert restored.checkpoint["channel_values"]["query"] == FIRST_QUESTION

    await delete_conversation(conversation_id)


async def test_deleting_a_conversation_releases_its_state() -> None:
    """删会话时要顺手清掉图状态，否则 Redis 里的线程只增不减。"""
    conversation_id = await create_conversation("待清理")
    await ask(FIRST_QUESTION, session_id=conversation_id)

    await delete_conversation(conversation_id)

    assert await read_thread_state(conversation_id) is None


# ==========================================================================
# 验收标准 4~6：会话与历史（对着真实查询产生的数据）
# ==========================================================================


async def test_a_real_query_is_recorded_into_both_the_conversation_and_the_history() -> None:
    """一轮真问答要同时落进 message 与 query_history，且历史详情六要素齐全（验收标准 6）。

    两处都断言，是因为它们由同一次提交写出（``services.query_service._persist``）：
    只看其中一处的话，"另一半没写进去"这种缺陷会漏掉，而它在界面上表现为
    "会话里有这一轮、历史里没有"。
    """
    conversation_id = await create_conversation("落库验收")
    frames = await ask(FIRST_QUESTION, session_id=conversation_id)
    assert_succeeded(frames)
    sql = payload_of(frames, "sql")["sql"]
    result = payload_of(frames, "result")["data"]

    async with _client() as client:
        history = (await client.get("/api/v1/history?page_size=1")).json()["data"]

    assert history["total"] == 1, "这个用户只问过一次，历史里就该只有一条"
    item = history["items"][0]
    assert item["query_text"] == FIRST_QUESTION
    assert item["status"] == "success"
    assert item["duration_ms"] is not None and item["duration_ms"] >= 0
    assert item["session_id"] == conversation_id

    async with _client() as client:
        detail = (await client.get(f"/api/v1/history/{item['id']}")).json()["data"]

    # FR-HIST-002 的六要素：问题 / SQL / 结果预览 / 解释 / 耗时 / 状态
    assert detail["query_text"] == FIRST_QUESTION
    assert detail["generated_sql"] == sql
    assert detail["status"] == "success"
    assert detail["duration_ms"] is not None
    # 解释由阶段 8 的 generate_explain 节点写。这里只验字段存在：
    # 该节点是 P1 能力且允许降级，值为 None 表示"这次没解释"，不是缺陷
    assert "explain_text" in detail
    preview = detail["result_preview"]
    assert preview["columns"] == result["columns"], "结果预览要与用户看到的结果对得上"
    assert preview["row_count"] == result["row_count"]
    assert len(preview["rows"]) <= len(result["rows"])

    await delete_conversation(conversation_id)


async def test_a_query_without_a_session_is_still_recorded() -> None:
    """不带 session_id 的一次性提问同样落历史。

    它也回答了一次"我问过什么"，而 query_history 的 session_id 本来就可空。
    """
    frames = await ask(FIRST_QUESTION)
    assert_succeeded(frames)

    async with _client() as client:
        history = (await client.get("/api/v1/history?keyword=各季度销售额&page_size=5")).json()["data"]

    assert len(history["items"]) == 1
    assert history["items"][0]["session_id"] is None


async def test_history_is_isolated_pageable_and_filterable() -> None:
    """历史列表分页、可按状态/关键词筛选、仅本人可见（验收标准第 5 条）。"""
    me = current[0]
    conversation_id = await create_conversation("筛选验收")
    await ask(FIRST_QUESTION, session_id=conversation_id)
    await ask(FOLLOW_UP, session_id=conversation_id)

    async with _client() as client:
        first_page = (await client.get("/api/v1/history?page_size=1")).json()["data"]
        filtered = (await client.get("/api/v1/history?keyword=只看华东&page_size=10")).json()["data"]
        succeeded = (await client.get("/api/v1/history?status=success&page_size=10")).json()["data"]
        failed = (await client.get("/api/v1/history?status=failed&page_size=10")).json()["data"]

        current[0] = new_user("other")
        theirs = (await client.get("/api/v1/history?page_size=10")).json()["data"]
        current[0] = me

    assert len(first_page["items"]) == 1, "page_size=1 只该返回一条"
    assert first_page["total"] == 2, "这个用户只问过这两次"

    assert [i["query_text"] for i in filtered["items"]] == [FOLLOW_UP]
    assert all(i["status"] == "success" for i in succeeded["items"])
    assert failed["total"] == 0, "这两次都成功了，failed 里不该有本用户的数据"
    assert theirs["total"] == 0, "另一个用户不该看到我的历史"

    await delete_conversation(conversation_id)


async def test_a_conversation_is_invisible_to_another_user() -> None:
    """会话仅本人可见（验收标准第 4 条）。"""
    me = current[0]
    conversation_id = await create_conversation("隐私验收")
    await ask(FIRST_QUESTION, session_id=conversation_id)

    current[0] = new_user("other")
    async with _client() as client:
        detail = await client.get(f"/api/v1/conversations/{conversation_id}")
        listing = (await client.get("/api/v1/conversations?page_size=100")).json()["data"]
        asking = await client.post(
            "/api/v1/query", json={"query": FOLLOW_UP, "session_id": conversation_id}
        )

    assert detail.status_code == 404
    assert conversation_id not in [c["id"] for c in listing["items"]]
    # 问数接口也要挡住：否则"看得见但用不了"变成"看不见但能接着用"，等于没隔离
    assert asking.status_code == 404

    current[0] = me
    await delete_conversation(conversation_id)
