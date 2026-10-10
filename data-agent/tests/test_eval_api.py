"""阶段 10：评测集与运行记录接口（FR-EVAL-001~003，TDD 7.2 eval 组）。

需要 MySQL：这一组验的是"导得进去、列得出来、删得掉、发起得了"，
而"发起得了"要往 eval_run 落一条真记录。

**发起运行的那条用例把后台任务换成了桩**：接口的契约是"立刻返回一条 running
记录 + 把批处理交出去"，而真跑一遍要调模型（几十秒）。后台任务本身另有验收用例
覆盖（test_stage10_acceptance），这里只验接口这一层的分工没有串位。
"""

from __future__ import annotations

import uuid
from types import SimpleNamespace

import httpx
import pytest
from sqlalchemy import delete, select

from app.api.v1 import eval as eval_api
from app.clients.mysql import session_scope
from app.core.security import get_current_user
from app.models import EvalCase, EvalRun
from main import app

pytestmark = pytest.mark.requires_stack

BASE_URL = "http://test"
RUN = uuid.uuid4().hex[:8]

ADMIN = SimpleNamespace(id=f"u-eval-{RUN}", username="eval-admin", role="admin", is_active=True)
current = [ADMIN]


@pytest.fixture(autouse=True)
def _authenticated():
    current[0] = ADMIN
    app.dependency_overrides[get_current_user] = lambda: current[0]
    yield
    app.dependency_overrides.pop(get_current_user, None)


@pytest.fixture
async def cases():
    """用例期间经接口导入的样本 id，用完逐个删掉。

    只删自己建的那些：开发库里的评测集可能是人手整理的，全表清空再还原虽然也能做，
    但那是"为了跑测试去动别人的数据"，能不动就不动。
    """
    created: list[str] = []
    try:
        yield created
    finally:
        for case_id in created:
            async with session_scope() as session:
                row = await session.get(EvalCase, case_id)
                if row is not None:
                    await session.delete(row)
                    await session.commit()


@pytest.fixture
async def runs():
    """用例期间落下的运行记录，用完删掉（接口没有删除端点，只能直接动库）。"""
    created: list[str] = []
    try:
        yield created
    finally:
        for run_id in created:
            async with session_scope() as session:
                row = await session.get(EvalRun, run_id)
                if row is not None:
                    await session.delete(row)
                    await session.commit()


def _client() -> httpx.AsyncClient:
    return httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url=BASE_URL)


async def import_one(client: httpx.AsyncClient, query_text: str, **extra) -> dict:
    payload = {"cases": [{"query_text": query_text, **extra}]}
    resp = await client.post("/api/v1/eval/cases", json=payload)
    assert resp.status_code == 200, resp.text
    return resp.json()["data"]["items"][0]


# ==========================================================================
# 评测集
# ==========================================================================


async def test_a_batch_of_cases_is_imported_in_one_call(cases: list[str]) -> None:
    """批量导入是真的批量（FR-EVAL-001）：一次请求进去多条。"""
    async with _client() as client:
        resp = await client.post(
            "/api/v1/eval/cases",
            json={
                "cases": [
                    {"query_text": f"阶段10-{RUN}-上面问的", "expected_sql": "SELECT 1"},
                    {"query_text": f"阶段10-{RUN}-另一问"},
                ]
            },
        )

    assert resp.status_code == 200, resp.text
    data = resp.json()["data"]
    assert data["imported"] == 2
    cases.extend(item["id"] for item in data["items"])
    assert len({item["id"] for item in data["items"]}) == 2


async def test_expected_result_round_trips_as_given(cases: list[str]) -> None:
    """预期结果存进去什么形状、读出来就是什么形状——比对时不做任何转换。"""
    expected = {"columns": ["gmv"], "rows": [[100]]}

    async with _client() as client:
        created = await import_one(
            client,
            f"阶段10-{RUN}-带预期结果",
            expected_sql="SELECT 100",
            expected_result=expected,
        )
        cases.append(created["id"])
        listed = await client.get("/api/v1/eval/cases?page_size=100")

    items = {item["id"]: item for item in listed.json()["data"]["items"]}
    assert items[created["id"]]["expected_result"] == expected
    assert items[created["id"]]["expected_sql"] == "SELECT 100"


async def test_a_duplicate_question_inside_one_batch_is_rejected(cases: list[str]) -> None:
    """同批重复会把准确率的分母撑大，而结果里看不出来。"""
    text = f"阶段10-{RUN}-同一句问两遍"

    async with _client() as client:
        resp = await client.post(
            "/api/v1/eval/cases",
            json={"cases": [{"query_text": text}, {"query_text": text}]},
        )

    assert resp.status_code == 400
    assert "重复" in resp.json()["message"]


async def test_an_empty_batch_is_rejected() -> None:
    async with _client() as client:
        resp = await client.post("/api/v1/eval/cases", json={"cases": []})

    assert resp.status_code == 400
    assert resp.json()["code"] == 40000


async def test_a_case_can_be_deleted_and_an_unknown_one_returns_404() -> None:
    async with _client() as client:
        created = await import_one(client, f"阶段10-{RUN}-待删除")

        deleted = await client.delete(f"/api/v1/eval/cases/{created['id']}")
        assert deleted.status_code == 200, deleted.text

        gone = await client.delete(f"/api/v1/eval/cases/{created['id']}")
        assert gone.status_code == 404


# ==========================================================================
# 发起运行
# ==========================================================================


async def test_an_empty_eval_set_cannot_be_run() -> None:
    """空评测集返回 400，而不是建一条永远算不出指标的运行记录。

    先把现有的样本挪走再验证：开发库里可能有人手整理过评测集，这里不能假定它是空的。
    """
    async with session_scope() as session:
        saved = [
            {
                "id": row.id,
                "query_text": row.query_text,
                "expected_sql": row.expected_sql,
                "expected_result": row.expected_result,
                "created_at": row.created_at,
            }
            for row in (await session.execute(select(EvalCase))).scalars().all()
        ]
        await session.execute(delete(EvalCase))
        await session.commit()

    try:
        async with _client() as client:
            resp = await client.post("/api/v1/eval/run", json={})
        assert resp.status_code == 400
        assert "评测集为空" in resp.json()["message"]
    finally:
        if saved:
            async with session_scope() as session:
                session.add_all([EvalCase(**row) for row in saved])
                await session.commit()


async def test_running_an_unknown_case_is_rejected(cases: list[str]) -> None:
    async with _client() as client:
        resp = await client.post("/api/v1/eval/run", json={"case_ids": ["没有这条样本"]})

    assert resp.status_code == 400
    assert "不存在" in resp.json()["message"]


async def test_a_run_returns_immediately_and_hands_the_batch_to_a_background_task(
    cases: list[str], runs: list[str], monkeypatch: pytest.MonkeyPatch
) -> None:
    """接口只负责落一条 running 记录并把活交出去（FR-EVAL-002）。

    这一层的分工错了会很难受：接口等跑完再返回的话，一次几百条的评测会撞上网关
    超时，而客户端看到的只是"请求失败"，不知道评测其实正在跑。
    """
    scheduled: list[dict] = []

    async def fake_execute(run_id: str, **kwargs) -> None:
        scheduled.append({"run_id": run_id, **kwargs})

    monkeypatch.setattr(eval_api, "execute_run", fake_execute)

    async with _client() as client:
        created = await import_one(client, f"阶段10-{RUN}-后台跑的")
        cases.append(created["id"])

        resp = await client.post("/api/v1/eval/run", json={"name": f"阶段10-{RUN}"})
        assert resp.status_code == 200, resp.text
        data = resp.json()["data"]
        runs.append(data["id"])

        # 记录当场就能读到，客户端凭它开始轮询
        polled = await client.get(f"/api/v1/eval/runs/{data['id']}")

    assert data["status"] == "running"
    assert data["total_cases"] == 1
    assert data["accuracy"] is None, "还没跑完，指标必须留空而不是 0"
    assert polled.json()["data"]["outcomes"] == []
    assert scheduled == [
        {"run_id": data["id"], "user_id": ADMIN.id, "role": ADMIN.role, "case_ids": None}
    ]


# ==========================================================================
# 运行记录
# ==========================================================================


async def test_run_records_are_listed_and_can_be_read_back(
    cases: list[str], runs: list[str], monkeypatch: pytest.MonkeyPatch
) -> None:
    """看板的趋势数据来自这个列表（FR-EVAL-003）：新建的记录必须出现在最前面。"""
    monkeypatch.setattr(eval_api, "execute_run", _noop)

    async with _client() as client:
        created = await import_one(client, f"阶段10-{RUN}-趋势")
        cases.append(created["id"])
        run_id = (
            await client.post("/api/v1/eval/run", json={"name": f"趋势-{RUN}"})
        ).json()["data"]["id"]
        runs.append(run_id)

        listing = (await client.get("/api/v1/eval/runs?page_size=100")).json()["data"]

    assert run_id in [item["id"] for item in listing["items"]]
    assert listing["items"][0]["id"] == run_id, "最新的运行排在最前"


async def test_an_unknown_run_returns_404() -> None:
    async with _client() as client:
        resp = await client.get("/api/v1/eval/runs/没有这条")

    assert resp.status_code == 404


# ==========================================================================
# 整组要求 admin
# ==========================================================================


async def test_the_whole_group_is_admin_only() -> None:
    """谁能改题目谁就能改成绩，所以评测集属于管理面（TDD 7.2）。"""
    current[0] = SimpleNamespace(id="u-eval-analyst", username="a", role="analyst", is_active=True)

    async with _client() as client:
        listing = await client.get("/api/v1/eval/cases")
        creating = await client.post("/api/v1/eval/cases", json={"cases": [{"query_text": "x"}]})
        running = await client.post("/api/v1/eval/run", json={})
        records = await client.get("/api/v1/eval/runs")

    assert listing.status_code == 403
    assert creating.status_code == 403
    assert running.status_code == 403
    assert records.status_code == 403


async def _noop(run_id: str, **kwargs) -> None:
    """把批处理换成一个什么都不做的协程。"""
