"""阶段 7：会话接口（FR-CONV-001~005，TDD 7.2 conversation 组）。

需要 MySQL（``requires_stack``）：会话与消息是真的落库再读回来的。
鉴权用依赖覆盖绕过——认证本身在阶段 3 已经单独覆盖过，这里要验的是
"归属校验"这一层，而那恰好要求能扮演**两个**不同的用户。
"""

from __future__ import annotations

import asyncio
import uuid
from types import SimpleNamespace

import httpx
import pytest

from app.clients.mysql import session_scope
from app.core.security import get_current_user
from app.repositories.conversation import MessageRepository
from main import app

pytestmark = pytest.mark.requires_stack

BASE_URL = "http://test"

#: 每次运行换一批用户 id，理由与 test_history_api 相同：会话是真的落库的，
#: 而"列表里有几条"这类断言要求本用户名下只有本次运行建的那些。固定 id 的话，
#: 第二次运行就会看见上一次的会话，用例只在干净库上成立。
RUN = uuid.uuid4().hex[:8]

USER_A = SimpleNamespace(id=f"u-conv-a-{RUN}", username="conv-a", role="analyst", is_active=True)
USER_B = SimpleNamespace(id=f"u-conv-b-{RUN}", username="conv-b", role="analyst", is_active=True)

#: 当前扮演的用户。改它比反复装卸依赖覆盖省事，也让"切换身份"在用例里一眼可见。
current = [USER_A]


@pytest.fixture(autouse=True)
def _authenticated():
    current[0] = USER_A
    app.dependency_overrides[get_current_user] = lambda: current[0]
    yield
    app.dependency_overrides.pop(get_current_user, None)


def _client() -> httpx.AsyncClient:
    return httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url=BASE_URL)


async def create_conversation(title: str | None = None) -> str:
    async with _client() as client:
        resp = await client.post("/api/v1/conversations", json={"title": title})
    assert resp.status_code == 200, resp.text
    return resp.json()["data"]["id"]


async def add_turn(conversation_id: str, *, user_id: str, query: str, sql: str) -> None:
    """直接往库里写一轮消息。

    不走问答接口：那条路要真调模型、真查数仓，而这里要验的是接口怎么读、
    怎么隔离，不是模型答得对不对（那在 test_stage7_acceptance 里）。
    """
    async with session_scope() as session:
        await MessageRepository(session).add_turn(
            conversation_id=conversation_id,
            user_id=user_id,
            role="assistant",
            query_text=query,
            generated_sql=sql,
            result_preview={"columns": ["q"], "rows": [[1]], "row_count": 1, "truncated": False},
        )
        await session.commit()


# ==========================================================================
# 创建
# ==========================================================================


async def test_create_returns_an_id_owned_by_the_caller() -> None:
    async with _client() as client:
        resp = await client.post("/api/v1/conversations", json={"title": "销售分析"})

    assert resp.status_code == 200
    data = resp.json()["data"]
    assert data["id"]
    assert data["title"] == "销售分析"
    assert data["created_at"]


async def test_create_without_a_title_is_allowed() -> None:
    """标题可以留空：前端通常是"用户先开口、再由首轮问题回填"。"""
    async with _client() as client:
        resp = await client.post("/api/v1/conversations", json={})

    assert resp.status_code == 200
    assert resp.json()["data"]["title"] is None


async def test_blank_title_is_normalised_to_null() -> None:
    """全是空白视同没填，不要在列表里显示一个空白的标题。"""
    async with _client() as client:
        resp = await client.post("/api/v1/conversations", json={"title": "   "})

    assert resp.json()["data"]["title"] is None


async def test_overlong_title_is_rejected_before_reaching_the_database() -> None:
    """超长标题要在入口拦下：宽松 sql_mode 下 MySQL 会静默截断，
    用户看到的标题与存下来的不是同一个，且没有任何提示。"""
    async with _client() as client:
        resp = await client.post("/api/v1/conversations", json={"title": "长" * 257})

    assert resp.status_code == 400


# ==========================================================================
# 列表与详情
# ==========================================================================


async def test_list_is_scoped_to_the_caller() -> None:
    mine = await create_conversation("我的会话")
    current[0] = USER_B
    theirs = await create_conversation("别人的会话")

    current[0] = USER_A
    async with _client() as client:
        ids = [c["id"] for c in (await client.get("/api/v1/conversations")).json()["data"]["items"]]

    assert mine in ids
    assert theirs not in ids


async def test_list_reports_total_page_and_page_size() -> None:
    for index in range(3):
        await create_conversation(f"会话 {index}")

    async with _client() as client:
        data = (await client.get("/api/v1/conversations?page=1&page_size=2")).json()["data"]

    assert data["page"] == 1
    assert data["page_size"] == 2
    assert len(data["items"]) == 2
    assert data["total"] >= 3


async def test_detail_returns_the_message_list_in_reading_order() -> None:
    """详情按发生顺序返回（早的在前）。

    两轮之间隔开 2 毫秒：主键的顺序保证是"跨毫秒严格按时间先后"（见
    ``repositories.ids``），同一毫秒内只保证次序稳定、不保证是真实先后。
    真实链路上两轮相隔的是一次完整的问答（秒级），所以这里也不是把不可靠的
    行为测成可靠的——只是别让用例凭空造出"同一毫秒内写两条"这个现实中不会
    出现的场景，那会让它随机变红。
    """
    conversation_id = await create_conversation("多轮")
    await add_turn(conversation_id, user_id=USER_A.id, query="各季度销售额", sql="SELECT 1")
    await asyncio.sleep(0.002)
    await add_turn(conversation_id, user_id=USER_A.id, query="只看华东", sql="SELECT 2")

    async with _client() as client:
        data = (await client.get(f"/api/v1/conversations/{conversation_id}")).json()["data"]

    assert [m["query_text"] for m in data["messages"]] == ["各季度销售额", "只看华东"]
    assert [m["generated_sql"] for m in data["messages"]] == ["SELECT 1", "SELECT 2"]


async def test_detail_of_a_new_conversation_has_no_messages() -> None:
    conversation_id = await create_conversation()

    async with _client() as client:
        data = (await client.get(f"/api/v1/conversations/{conversation_id}")).json()["data"]

    assert data["messages"] == []


async def test_detail_of_someone_elses_conversation_is_not_found() -> None:
    """越权按"不存在"处理，不返回 403。

    403 等于承认"这个 id 确实存在、只是不归你"，那是一个可以问出来的信息。
    """
    conversation_id = await create_conversation("我的")
    current[0] = USER_B

    async with _client() as client:
        resp = await client.get(f"/api/v1/conversations/{conversation_id}")

    assert resp.status_code == 404
    assert resp.json()["code"] == 40400


async def test_detail_of_an_unknown_id_is_not_found() -> None:
    async with _client() as client:
        resp = await client.get("/api/v1/conversations/no-such-id")

    assert resp.status_code == 404


# ==========================================================================
# 删除
# ==========================================================================


async def test_delete_removes_the_conversation_and_its_messages() -> None:
    conversation_id = await create_conversation("待删")
    await add_turn(conversation_id, user_id=USER_A.id, query="q", sql="SELECT 1")

    async with _client() as client:
        resp = await client.delete(f"/api/v1/conversations/{conversation_id}")
        after = await client.get(f"/api/v1/conversations/{conversation_id}")

    assert resp.status_code == 200
    assert after.status_code == 404


async def test_delete_leaves_no_orphan_messages() -> None:
    """消息必须跟着会话一起走（TDD 8.1 的建表语句没有外键，不会级联）。

    留下孤儿消息的话，它们永远不会被读到，却会一直占着库。
    """
    conversation_id = await create_conversation("待删")
    await add_turn(conversation_id, user_id=USER_A.id, query="q", sql="SELECT 1")

    async with _client() as client:
        await client.delete(f"/api/v1/conversations/{conversation_id}")

    async with session_scope() as session:
        remaining = await MessageRepository(session).list_by_conversation(conversation_id)

    assert remaining == []


async def test_delete_someone_elses_conversation_is_not_found_and_deletes_nothing() -> None:
    conversation_id = await create_conversation("我的")
    current[0] = USER_B

    async with _client() as client:
        resp = await client.delete(f"/api/v1/conversations/{conversation_id}")

    current[0] = USER_A
    async with _client() as client:
        still_there = await client.get(f"/api/v1/conversations/{conversation_id}")

    assert resp.status_code == 404
    assert still_there.status_code == 200, "越权删除不能真的删掉别人的会话"


# ==========================================================================
# 鉴权
# ==========================================================================


async def test_all_endpoints_require_authentication() -> None:
    conversation_id = await create_conversation()
    app.dependency_overrides.pop(get_current_user, None)

    async with _client() as client:
        responses = [
            await client.post("/api/v1/conversations", json={}),
            await client.get("/api/v1/conversations"),
            await client.get(f"/api/v1/conversations/{conversation_id}"),
            await client.delete(f"/api/v1/conversations/{conversation_id}"),
        ]

    assert [r.status_code for r in responses] == [401, 401, 401, 401]
