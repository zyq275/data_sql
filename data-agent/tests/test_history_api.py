"""阶段 7：查询历史接口（FR-HIST-001~003，TDD 7.2 history 组）。

需要 MySQL（``requires_stack``）。历史行直接落库构造，不走问答接口：
那条路要真调模型、真查数仓，而这里要验的是分页、筛选、详情字段与归属隔离。
"""

from __future__ import annotations

import uuid
from types import SimpleNamespace

import httpx
import pytest

from app.clients.mysql import session_scope
from app.core.security import get_current_user
from app.repositories.query_history import QueryHistoryRepository
from main import app

pytestmark = pytest.mark.requires_stack

BASE_URL = "http://test"

#: 每次运行换一批用户 id，见 User A/B 的说明。
RUN = uuid.uuid4().hex[:8]

#: 用户 id 里带上本次运行的随机后缀：这些用例会真的往库里写历史行，而"筛选命中几条"
#: 这类断言要求本用户名下**只有本用例写的那几条**。库是长期存在的开发库、用例之间
#: 也不共享清理逻辑，固定 id 会让上一次运行留下的行混进这一次——症状是"同一个用例，
#: 第一次跑绿、第二次跑红"，比一直红更难查。
USER_A = SimpleNamespace(id=f"u-hist-a-{RUN}", username="hist-a", role="analyst", is_active=True)
USER_B = SimpleNamespace(id=f"u-hist-b-{RUN}", username="hist-b", role="analyst", is_active=True)

current = [USER_A]


@pytest.fixture(autouse=True)
def _authenticated():
    current[0] = USER_A
    app.dependency_overrides[get_current_user] = lambda: current[0]
    yield
    app.dependency_overrides.pop(get_current_user, None)


def _client() -> httpx.AsyncClient:
    return httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url=BASE_URL)


async def add_history(
    *,
    user_id: str = USER_A.id,
    query_text: str = "各季度销售额",
    sql: str = "SELECT quarter, SUM(amount) FROM fact_order GROUP BY quarter",
    status: str = "success",
    duration_ms: int = 1200,
    session_id: str | None = None,
) -> str:
    async with session_scope() as session:
        record = await QueryHistoryRepository(session).create(
            user_id=user_id,
            session_id=session_id,
            query_text=query_text,
            generated_sql=sql,
            result_preview={"columns": ["quarter", "amount"], "rows": [["Q1", "1"]], "row_count": 1, "truncated": False},
            explain_text=None,
            status=status,
            duration_ms=duration_ms,
        )
        await session.commit()
        return record.id


async def get_list(query: str = "") -> dict:
    async with _client() as client:
        resp = await client.get(f"/api/v1/history{query}")
    assert resp.status_code == 200, resp.text
    return resp.json()["data"]


# ==========================================================================
# 列表：分页与筛选
# ==========================================================================


async def test_list_returns_page_metadata() -> None:
    await add_history(query_text="分页用")

    data = await get_list("?page=1&page_size=2")

    assert data["page"] == 1
    assert data["page_size"] == 2
    assert len(data["items"]) <= 2
    assert data["total"] >= 1


async def test_list_is_newest_first() -> None:
    for index in range(3):
        await add_history(query_text=f"第 {index} 条")

    data = await get_list("?page_size=100")

    # 主键是时间有序的，所以倒序的 id 一定严格递减；反过来若靠 created_at 排序，
    # 同一秒内写入的这几条谁前谁后是数据库说不上来的（见 repositories.ids）
    ids = [item["id"] for item in data["items"]]
    assert ids == sorted(ids, reverse=True)
    assert "第 2 条" in [item["query_text"] for item in data["items"]]


async def test_list_only_contains_the_callers_own_records() -> None:
    mine = await add_history(query_text="我的历史")
    theirs = await add_history(user_id=USER_B.id, query_text="别人的历史")

    data = await get_list("?page_size=100")

    ids = [item["id"] for item in data["items"]]
    assert mine in ids
    assert theirs not in ids


async def test_list_filters_by_status() -> None:
    await add_history(query_text="成功的那条", status="success")
    failed = await add_history(query_text="失败的那条", status="failed")

    data = await get_list("?status=failed&page_size=100")

    assert [item["id"] for item in data["items"]] == [failed]
    assert data["total"] == 1


async def test_list_filters_by_keyword_in_the_question() -> None:
    hit = await add_history(query_text="华东区销售额")
    await add_history(query_text="华南区销售额")

    data = await get_list("?keyword=华东&page_size=100")

    assert [item["id"] for item in data["items"]] == [hit]


async def test_list_filters_by_keyword_in_the_sql() -> None:
    """关键词也要匹配 SQL：用户可能只记得"那条用了 dim_region 的查询"。"""
    hit = await add_history(query_text="某个问题", sql="SELECT * FROM dim_region")
    await add_history(query_text="某个问题", sql="SELECT * FROM fact_order")

    data = await get_list("?keyword=dim_region&page_size=100")

    assert [item["id"] for item in data["items"]] == [hit]


async def test_list_filters_can_be_combined() -> None:
    hit = await add_history(query_text="华东区", status="failed")
    await add_history(query_text="华东区", status="success")

    data = await get_list("?keyword=华东&status=failed&page_size=100")

    assert [item["id"] for item in data["items"]] == [hit]


async def test_list_rejects_an_unknown_status() -> None:
    """未知状态的取值由接口层挡下（422），而不是让服务层去猜该查什么。"""
    async with _client() as client:
        resp = await client.get("/api/v1/history?status=whatever")

    assert resp.status_code in {400, 422}


async def test_list_omits_the_heavy_fields() -> None:
    """列表不带 SQL 与结果预览：一页 100 条都带上会让响应膨胀到几百 KB，
    而用户翻列表时并不看这两个字段（那是点进去之后的事）。"""
    await add_history(query_text="减肥用")

    data = await get_list("?keyword=减肥用")

    assert "generated_sql" not in data["items"][0]
    assert "result_preview" not in data["items"][0]


# ==========================================================================
# 详情
# ==========================================================================


async def test_detail_carries_the_six_required_fields() -> None:
    """FR-HIST-002：问题 / SQL / 结果预览 / 解释 / 耗时 / 状态。"""
    history_id = await add_history(query_text="看详情", status="success", duration_ms=2500)

    async with _client() as client:
        data = (await client.get(f"/api/v1/history/{history_id}")).json()["data"]

    assert data["query_text"] == "看详情"
    assert data["generated_sql"].startswith("SELECT quarter")
    assert data["result_preview"]["columns"] == ["quarter", "amount"]
    assert "explain_text" in data, "自然语言解释由阶段 8 产出，字段现在如实为空"
    assert data["duration_ms"] == 2500
    assert data["status"] == "success"
    assert data["created_at"]


async def test_detail_of_someone_elses_record_is_not_found() -> None:
    history_id = await add_history()
    current[0] = USER_B

    async with _client() as client:
        resp = await client.get(f"/api/v1/history/{history_id}")

    assert resp.status_code == 404
    assert resp.json()["code"] == 40400


async def test_detail_of_an_unknown_id_is_not_found() -> None:
    async with _client() as client:
        resp = await client.get("/api/v1/history/no-such-id")

    assert resp.status_code == 404


# ==========================================================================
# 删除
# ==========================================================================


async def test_delete_removes_the_record() -> None:
    history_id = await add_history(query_text="待删历史")

    async with _client() as client:
        resp = await client.delete(f"/api/v1/history/{history_id}")
        after = await client.get(f"/api/v1/history/{history_id}")

    assert resp.status_code == 200
    assert after.status_code == 404


async def test_delete_someone_elses_record_is_not_found_and_deletes_nothing() -> None:
    history_id = await add_history()
    current[0] = USER_B

    async with _client() as client:
        resp = await client.delete(f"/api/v1/history/{history_id}")

    current[0] = USER_A
    async with _client() as client:
        still_there = await client.get(f"/api/v1/history/{history_id}")

    assert resp.status_code == 404
    assert still_there.status_code == 200


# ==========================================================================
# 鉴权
# ==========================================================================


async def test_all_endpoints_require_authentication() -> None:
    history_id = await add_history()
    app.dependency_overrides.pop(get_current_user, None)

    async with _client() as client:
        responses = [
            await client.get("/api/v1/history"),
            await client.get(f"/api/v1/history/{history_id}"),
            await client.delete(f"/api/v1/history/{history_id}"),
        ]

    assert [r.status_code for r in responses] == [401, 401, 401]
