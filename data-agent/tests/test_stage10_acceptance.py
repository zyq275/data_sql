"""阶段 10 验收：评测体系与数据看板（《开发步骤文档》阶段 10 的五条验收标准）。

  1. 能批量导入、删除评测样本
  2. 能统计准确率、召回命中率、执行成功率、平均耗时并落 eval_run
  3. 评测看板可看到历史运行趋势
  4. 数据看板接口仅 analyst 及以上可访问，viewer 返回 403，
     且只使用授权范围内的表/字段
  5. 评测运行不污染线上数据

第 2 条走**真实的**评测链路（真模型、真检索、真数仓）。判分那部分是纯计算，
``test_eval_scoring`` 已经逐条钉死了；这里要验的是**这些数字真的能算出来并写进库**——
用桩模型跑出来的"准确率 100%"只说明桩答对了我们预设的题。

第 5 条是这五条里最要紧的一条，也是唯一一条"从外面看不出对错"的：隔离做漏了，
评测本身照样跑、指标照样好看，被污染的是**线上**——用户的会话列表里凭空多出
几十条他从未问过的问题。所以这里不比"评测结果对不对"，而是比**库里多没多出东西**。

评测样本用随机后缀命名，只删自己建的：开发库里的评测集可能是人手整理的。
"""

from __future__ import annotations

import asyncio
import itertools
import uuid
from types import SimpleNamespace

import httpx
import pytest
from sqlalchemy import delete, func, select

from app.clients.mysql import session_scope
from app.core.security import get_current_user
from app.models import Conversation, EvalCase, EvalRun, Message, Permission, QueryHistory
from main import app

#: 必须有真模型：第 2 条验的就是"跑一遍真链路，四个指标都落得下来"。
pytestmark = [pytest.mark.requires_stack, pytest.mark.requires_llm]

BASE_URL = "http://test"
RUN = uuid.uuid4().hex[:8]
_serial = itertools.count()

ROLE = "analyst"
TOUCHED_ROLES = ("analyst",)

ADMIN = SimpleNamespace(
    id=f"u-acc10-admin-{RUN}", username="acc10-admin", role="admin", is_active=True
)
VIEWER = SimpleNamespace(
    id=f"u-acc10-viewer-{RUN}", username="acc10-viewer", role="viewer", is_active=True
)

#: 轮询运行结果的上限。一次两条样本的真链路评测要调好几次模型，
#: 本机跑通一次大约十几秒，留足余量但**必须有上限**——否则模型服务挂了
#: 这个用例会一直挂着，表现为"卡住"而不是"失败"。
POLL_TIMEOUT_S = 240.0
POLL_INTERVAL_S = 1.0

current: list[SimpleNamespace] = [ADMIN]


@pytest.fixture(autouse=True)
def _authenticated():
    current[:] = [ADMIN]
    app.dependency_overrides[get_current_user] = lambda: current[0]
    yield
    app.dependency_overrides.pop(get_current_user, None)


async def _snapshot() -> list[dict]:
    async with session_scope() as session:
        rows = (
            (await session.execute(select(Permission).where(Permission.role.in_(TOUCHED_ROLES))))
            .scalars()
            .all()
        )
        return [
            {
                "id": row.id,
                "role": row.role,
                "resource_type": row.resource_type,
                "resource_id": row.resource_id,
                "created_at": row.created_at,
            }
            for row in rows
        ]


async def _clear_permissions() -> None:
    async with session_scope() as session:
        await session.execute(delete(Permission).where(Permission.role.in_(TOUCHED_ROLES)))
        await session.commit()


@pytest.fixture(autouse=True)
async def _isolate_permissions():
    """用例期间 analyst 没有任何配置，用完把原来的放回去（同 ``test_stage9_acceptance``）。

    评测以**发起人的角色**跑，所以这里把 analyst 清空是有意的：不受限的角色
    跑出来的正是"模型本身"的成绩，与"某个角色的系统表现"分开。
    还原放在夹具里而不是各用例自己管——评测用例失败的概率不低（它要调真模型），
    而一旦失败就顺手吃掉管理员的权限配置，是最不该有的副作用。
    """
    saved = await _snapshot()
    await _clear_permissions()
    try:
        yield
    finally:
        await _clear_permissions()
        if saved:
            async with session_scope() as session:
                session.add_all([Permission(**row) for row in saved])
                await session.commit()


@pytest.fixture
async def cases():
    """本用例经接口导入的样本 id，用完逐个删掉。"""
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
    """本用例落下的运行记录，用完删掉。"""
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
    return httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url=BASE_URL, timeout=180.0
    )


async def as_current(user: SimpleNamespace, call):
    """换个身份调一次接口再换回来。"""
    previous = current[0]
    current[0] = user
    try:
        return await call()
    finally:
        current[0] = previous


async def wait_for(run_id: str, *, timeout: float = POLL_TIMEOUT_S) -> dict:
    """轮询到这次运行落定，返回详情。

    轮询是**接口契约的一部分**（``POST /eval/run`` 立刻返回 running），
    所以这里照着客户端的方式做，而不是绕过接口去查库。
    """
    deadline = asyncio.get_running_loop().time() + timeout
    while asyncio.get_running_loop().time() < deadline:
        async with _client() as client:
            resp = await client.get(f"/api/v1/eval/runs/{run_id}")
        assert resp.status_code == 200, resp.text
        data = resp.json()["data"]
        if data["status"] != "running":
            return data
        await asyncio.sleep(POLL_INTERVAL_S)
    raise AssertionError(f"评测运行 {run_id} 在 {timeout:.0f}s 内没有落定")


# ==========================================================================
# 验收 1：能批量导入、删除评测样本
# ==========================================================================


async def test_criterion_1_cases_can_be_bulk_imported_and_deleted(cases: list[str]) -> None:
    """一次请求导入多条、逐条删掉、列表随之变化。

    样本名带随机后缀：开发库里的评测集可能是人手整理的，不能假定它是空的，
    也不能把别人的样本算进自己的断言里。
    """
    first = f"阶段10验收-{RUN}-订单总数"
    second = f"阶段10验收-{RUN}-大区订单数"

    async with _client() as client:
        imported = await client.post(
            "/api/v1/eval/cases",
            json={
                "cases": [
                    {
                        "query_text": first,
                        "expected_sql": "SELECT COUNT(*) FROM fact_order",
                        "expected_result": {"rows": [[3974]]},
                    },
                    {
                        "query_text": second,
                        "expected_sql": (
                            "SELECT dim_region.region_name, COUNT(*) "
                            "FROM fact_order "
                            "JOIN dim_region ON fact_order.region_id = dim_region.region_id "
                            "GROUP BY dim_region.region_name"
                        ),
                    },
                ]
            },
        )
        assert imported.status_code == 200, imported.text
        items = imported.json()["data"]["items"]
        cases.extend(item["id"] for item in items)
        assert imported.json()["data"]["imported"] == 2

        listed = (await client.get("/api/v1/eval/cases?page_size=100")).json()["data"]["items"]
        assert {item["id"] for item in items} <= {item["id"] for item in listed}

        deleted = await client.delete(f"/api/v1/eval/cases/{items[0]['id']}")
        assert deleted.status_code == 200, deleted.text

        after = (await client.get("/api/v1/eval/cases?page_size=100")).json()["data"]["items"]

    assert items[0]["id"] not in {item["id"] for item in after}
    assert items[1]["id"] in {item["id"] for item in after}, "删一条不该动到另一条"


# ==========================================================================
# 验收 2：四个指标算得出来并且落了库
# ==========================================================================


async def test_criterion_2_a_real_run_lands_all_four_metrics(
    cases: list[str], runs: list[str]
) -> None:
    """跑一遍真链路，四个指标都要有值，并且写进了 eval_run 这一行。

    **一个都不许是 None**：每条样本都给了 ``expected_sql``，于是召回判得出来；
    问题都很直白，模型答不出结果的可能性很低。任何一个指标为空都说明那条链路
    （判分 / 召回判定 / 执行 / 计时）没接上——而看板上一个空值比一个难看的低分
    更难解释。
    """
    async with _client() as client:
        created = await client.post(
            "/api/v1/eval/cases",
            json={
                "cases": [
                    {
                        "query_text": "订单表里一共有多少条订单记录",
                        "expected_sql": "SELECT COUNT(*) FROM fact_order",
                        "expected_result": {"rows": [[3974]]},
                    },
                    {
                        "query_text": "每个大区各有多少条订单",
                        "expected_sql": (
                            "SELECT dim_region.region_name, COUNT(*) "
                            "FROM fact_order "
                            "JOIN dim_region ON fact_order.region_id = dim_region.region_id "
                            "GROUP BY dim_region.region_name"
                        ),
                    },
                ]
            },
        )
        cases.extend(item["id"] for item in created.json()["data"]["items"])

        started = await client.post("/api/v1/eval/run", json={"name": f"阶段10验收-{RUN}"})
        assert started.status_code == 200, started.text
        run_id = started.json()["data"]["id"]
        runs.append(run_id)

    detail = await wait_for(run_id)

    assert detail["status"] == "finished", detail.get("error")
    assert detail["total_cases"] == 2

    for field in ("accuracy", "recall_hit_rate", "execute_success_rate", "avg_duration_ms"):
        assert detail[field] is not None, f"{field} 没有算出来"

    # 指标不是接口现场算的：库里的那一行必须也是同一组数
    async with session_scope() as session:
        row = await session.get(EvalRun, run_id)
        assert row is not None
        assert float(row.accuracy) == float(detail["accuracy"])
        assert float(row.recall_hit_rate) == float(detail["recall_hit_rate"])
        assert float(row.execute_success_rate) == float(detail["execute_success_rate"])
        assert row.avg_duration_ms == detail["avg_duration_ms"] > 0

    # 逐条明细要能回答"这条为什么错"：光有一个准确率，看板上的人无从下手
    assert len(detail["outcomes"]) == 2
    for outcome in detail["outcomes"]:
        assert outcome["reason"], "每条都要有一句判分说明"
        assert outcome["duration_ms"] > 0


# ==========================================================================
# 验收 3：评测看板能看到历史运行趋势
# ==========================================================================


async def test_criterion_3_run_history_is_visible_to_the_trend_board(
    cases: list[str], runs: list[str]
) -> None:
    """两次运行都要出现在历史列表里，且最新的在最前。

    趋势图读的就是这个列表：只要有一次运行没被列出来，趋势上就会出现一个缺口，
    而缺口看着像"那天没跑"，不像"记录丢了"。
    """
    async with _client() as client:
        created = await client.post(
            "/api/v1/eval/cases",
            json={
                "cases": [
                    {
                        "query_text": "订单表里一共有多少条订单记录",
                        "expected_sql": "SELECT COUNT(*) FROM fact_order",
                        "expected_result": {"rows": [[3974]]},
                    }
                ]
            },
        )
        cases.extend(item["id"] for item in created.json()["data"]["items"])

        for tag in ("第一次", "第二次"):
            resp = await client.post(
                "/api/v1/eval/run", json={"name": f"阶段10验收-{RUN}-{tag}"}
            )
            assert resp.status_code == 200, resp.text
            run_id = resp.json()["data"]["id"]
            runs.append(run_id)
            await wait_for(run_id)

        listing = (await client.get("/api/v1/eval/runs?page_size=100")).json()["data"]["items"]

    mine = [item for item in listing if item["id"] in runs]
    assert [item["id"] for item in mine] == list(reversed(runs)), "最新的运行排在最前"
    # 两条历史都带着可画图的指标，趋势线上才有第二个点
    for item in mine:
        assert item["status"] == "finished"
        assert item["accuracy"] is not None


# ==========================================================================
# 验收 4：看板的鉴权与数据范围
# ==========================================================================


async def test_criterion_4_a_viewer_is_refused_by_the_dashboard() -> None:
    """viewer 两个端点都 403。看板给的是全公司口径的聚合数，不是他的日常工具。"""

    async def call() -> tuple[int, int]:
        async with _client() as client:
            dims = await client.get("/api/v1/dashboard/dimensions")
            metrics = await client.get("/api/v1/dashboard/metrics?dimension=month")
        return dims.status_code, metrics.status_code

    assert await as_current(VIEWER, call) == (403, 403)


async def test_criterion_4_the_dashboard_only_uses_authorized_tables_and_fields() -> None:
    """只授 ``fact_order`` 与 ``dim_date`` 时，出数的 SQL 里只该有这两张表。

    这条与阶段 9 的"生成的 SQL 不出现越权表"是同一条要求的两种表现：那里拦的是
    **模型会不会自作主张**，这里拦的是**模板会不会照着知识库全量拼**——
    后者更隐蔽，因为拼出来的 SQL 每次都一样，看着"很稳定"，只是多了一张表。
    """
    from app.core.sql_security import referenced_tables

    user = SimpleNamespace(
        id=f"u-acc10-analyst-{RUN}", username="acc10-analyst", role=ROLE, is_active=True
    )

    async def restricted():
        previous = current[0]
        current[0] = ADMIN
        try:
            async with _client() as client:
                catalog = (await client.get("/api/v1/permissions/resources")).json()["data"]
                for name in ("fact_order", "dim_date"):
                    table = next(row for row in catalog["tables"] if row["name"] == name)
                    granted = await client.post(
                        "/api/v1/permissions",
                        json={"role": ROLE, "resource_type": "table", "resource_id": table["id"]},
                    )
                    assert granted.status_code == 200, granted.text
        finally:
            current[0] = user

        async with _client() as client:
            allowed = await client.get(
                "/api/v1/dashboard/metrics?dimension=year,month&metrics=GMV,order_count"
            )
            denied = await client.get("/api/v1/dashboard/metrics?dimension=province&metrics=GMV")
        return allowed, denied

    allowed, denied = await restricted()

    assert allowed.status_code == 200, allowed.text
    data = allowed.json()["data"]
    assert data["unrestricted"] is False
    assert referenced_tables(data["sql"]) <= {"fact_order", "dim_date"}, data["sql"]

    assert denied.status_code == 403, "dim_region 没有授权，按省份看数应当被拒"
    assert "province" in denied.json()["message"]


# ==========================================================================
# 验收 5：评测不污染线上数据
# ==========================================================================


async def _online_counts() -> dict[str, int]:
    async with session_scope() as session:
        return {
            model.__tablename__: (
                await session.execute(select(func.count()).select_from(model))
            ).scalar_one()
            for model in (Conversation, Message, QueryHistory)
        }


async def test_criterion_5_a_run_writes_nothing_into_the_online_tables(
    cases: list[str], runs: list[str]
) -> None:
    """跑一次评测前后，线上三张表的行数一行不差。

    **这条只能这样验**：隔离做漏时评测照样跑通、指标照样好看，唯一不对的是
    用户在会话列表里看到几十条自己没问过的问题——而那要等到有人打开页面才发现。
    所以这里不比评测结果，只比"库里多没多出东西"。

    顺带钉住"评测发起人也一样不落数据"：发起人是 admin，最容易发生的漏法就是
    "给发起人建一条会话吧，免得流程里某个地方空指针"。这里顺手把 admin 的
    会话行数也记下来，因为它最可能是那个被写进去的人。
    """
    before = await _online_counts()
    before_admin_conversations = await _count_conversations_of(ADMIN.id)

    async with _client() as client:
        created = await client.post(
            "/api/v1/eval/cases",
            json={
                "cases": [
                    {
                        "query_text": "订单表里一共有多少条订单记录",
                        "expected_sql": "SELECT COUNT(*) FROM fact_order",
                        "expected_result": {"rows": [[3974]]},
                    }
                ]
            },
        )
        cases.extend(item["id"] for item in created.json()["data"]["items"])

        started = await client.post("/api/v1/eval/run", json={"name": f"阶段10验收-{RUN}-隔离"})
        run_id = started.json()["data"]["id"]
        runs.append(run_id)

    detail = await wait_for(run_id)

    assert detail["status"] == "finished", detail.get("error")
    # 先确认它真的跑了：一条都没执行的话，"没写脏数据"是废话
    assert detail["outcomes"][0]["duration_ms"] > 0

    assert await _online_counts() == before
    assert await _count_conversations_of(ADMIN.id) == before_admin_conversations


async def _count_conversations_of(user_id: str) -> int:
    async with session_scope() as session:
        return (
            await session.execute(
                select(func.count()).select_from(Conversation).where(Conversation.user_id == user_id)
            )
        ).scalar_one()


async def test_a_crashed_run_is_recorded_as_failed_rather_than_left_running(
    cases: list[str], runs: list[str], monkeypatch: pytest.MonkeyPatch
) -> None:
    """跑挂的评测要留下 ``failed`` 与原因，而不是永远停在 running。

    客户端靠轮询看状态，于是"永远 running"在界面上与"还在跑"长得一模一样，
    而它其实早就死了。这条路径从接口那一侧不好造（接口的校验都很严），
    所以直接调后台任务本体，把中间那一步换成会抛的桩。
    """
    from app.services import eval_runner

    async with _client() as client:
        created = await client.post(
            "/api/v1/eval/cases",
            json={"cases": [{"query_text": f"阶段10验收-{RUN}-注定失败"}]},
        )
        cases.extend(item["id"] for item in created.json()["data"]["items"])

        started = await client.post("/api/v1/eval/run", json={"name": f"阶段10验收-{RUN}-失败"})
        run_id = started.json()["data"]["id"]
        runs.append(run_id)

    async def boom(*args, **kwargs):
        raise RuntimeError("数仓连不上了")

    monkeypatch.setattr(eval_runner, "_run_case", boom)
    await eval_runner.execute_run(run_id, user_id=ADMIN.id, role=ADMIN.role)

    async with _client() as client:
        detail = (await client.get(f"/api/v1/eval/runs/{run_id}")).json()["data"]

    assert detail["status"] == "failed"
    assert "数仓连不上了" in (detail["error"] or "")
