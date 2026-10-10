"""阶段 10：数据看板接口（FR-DASH-001~002，TDD 7.2 dashboard 组）。

需要 MySQL 与数仓：这一组验的是"算出来的数对不对、用到的表越不越权"，
两件事都要真跑 SQL 才谈得上。

**隔离靠快照，不靠随机命名**：权限是按固定的三个角色配的，用例必须往真实角色上配。
安排与 ``test_permission_api`` 一致——进出两头各做一次快照与还原，
用例期间该角色的配置由用例自己掌控（见那里的详细说明）。
"""

from __future__ import annotations

import uuid
from types import SimpleNamespace

import httpx
import pytest
from sqlalchemy import delete, select

from app.clients.mysql import session_scope
from app.core.security import get_current_user
from app.core.sql_security import referenced_tables
from app.models import Permission
from main import app

pytestmark = pytest.mark.requires_stack

BASE_URL = "http://test"
RUN = uuid.uuid4().hex[:8]
ROLE = "analyst"

ADMIN = SimpleNamespace(id=f"u-dash-a-{RUN}", username="dash-admin", role="admin", is_active=True)
ANALYST = SimpleNamespace(id=f"u-dash-n-{RUN}", username="dash-analyst", role=ROLE, is_active=True)
VIEWER = SimpleNamespace(id=f"u-dash-v-{RUN}", username="dash-viewer", role="viewer", is_active=True)

current = [ADMIN]


@pytest.fixture(autouse=True)
def _authenticated():
    current[0] = ADMIN
    app.dependency_overrides[get_current_user] = lambda: current[0]
    yield
    app.dependency_overrides.pop(get_current_user, None)


@pytest.fixture(autouse=True)
async def _isolate_permissions():
    """用例期间 analyst 没有任何配置，用完原样放回。"""
    async with session_scope() as session:
        saved = [
            {
                "id": row.id,
                "role": row.role,
                "resource_type": row.resource_type,
                "resource_id": row.resource_id,
                "created_at": row.created_at,
            }
            for row in (
                await session.execute(select(Permission).where(Permission.role == ROLE))
            )
            .scalars()
            .all()
        ]
        await session.execute(delete(Permission).where(Permission.role == ROLE))
        await session.commit()
    try:
        yield
    finally:
        async with session_scope() as session:
            await session.execute(delete(Permission).where(Permission.role == ROLE))
            if saved:
                session.add_all([Permission(**row) for row in saved])
            await session.commit()


def _client() -> httpx.AsyncClient:
    return httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url=BASE_URL)


async def grant_whole(client: httpx.AsyncClient, table_name: str) -> None:
    """给 analyst 整表授权一张表（经 admin 身份调权限接口）。"""
    current[0] = ADMIN
    catalog = (await client.get("/api/v1/permissions/resources")).json()["data"]
    table = next(row for row in catalog["tables"] if row["name"] == table_name)
    resp = await client.post(
        "/api/v1/permissions",
        json={"role": ROLE, "resource_type": "table", "resource_id": table["id"]},
    )
    assert resp.status_code == 200, resp.text
    current[0] = ANALYST


# ==========================================================================
# 鉴权：analyst 及以上
# ==========================================================================


async def test_a_viewer_cannot_open_the_dashboard() -> None:
    """看板看的是全公司口径的聚合数，viewer 进来直接 403（FR-DASH-001）。"""
    current[0] = VIEWER

    async with _client() as client:
        dims = await client.get("/api/v1/dashboard/dimensions")
        metrics = await client.get("/api/v1/dashboard/metrics?dimension=month")

    assert dims.status_code == 403
    assert metrics.status_code == 403


async def test_an_analyst_can_list_what_it_may_use() -> None:
    current[0] = ANALYST

    async with _client() as client:
        resp = await client.get("/api/v1/dashboard/dimensions")

    assert resp.status_code == 200, resp.text
    data = resp.json()["data"]
    assert {row["name"] for row in data["dimensions"]} >= {"year", "month", "province"}
    assert "GMV" in {row["name"] for row in data["metrics"]}
    # 名字要带中文别名，否则下拉框里是一串英文列名
    assert all(row["alias"] for row in data["dimensions"])


# ==========================================================================
# 聚合取数
# ==========================================================================


async def test_metrics_are_returned_as_a_dimension_series() -> None:
    """看板要的是一条趋势线：维度列在前，其后每个指标一列。"""
    current[0] = ANALYST

    async with _client() as client:
        resp = await client.get(
            "/api/v1/dashboard/metrics?dimension=year,month&metrics=GMV,order_count"
        )

    assert resp.status_code == 200, resp.text
    data = resp.json()["data"]
    assert data["dimensions"] == ["year", "month"]
    assert data["columns"] == ["year", "month", "GMV", "order_count"]
    assert data["row_count"] == len(data["rows"]) > 0
    # 维度值必须有序：折线的连线顺序由它决定
    firsts = [row[:2] for row in data["rows"]]
    assert firsts == sorted(firsts)


async def test_the_response_carries_the_sql_it_ran() -> None:
    """一个数被质疑时，第一件要做的事是看它是怎么算出来的。"""
    current[0] = ANALYST

    async with _client() as client:
        data = (
            await client.get("/api/v1/dashboard/metrics?dimension=month&metrics=GMV")
        ).json()["data"]

    assert "SELECT" in data["sql"]
    assert "SUM(fact_order.order_amount)" in data["sql"]


async def test_the_business_filter_is_part_of_the_template() -> None:
    """过滤口径要跟着看板一起下去（BR-META-002）。

    少了它，GMV 会把已取消、已退款的订单一起算进去——数字看着正常，只是偏大，
    而两个地方（问答与看板）报出来的数不一样时，没有人能说清哪个对。
    """
    current[0] = ANALYST

    async with _client() as client:
        data = (
            await client.get("/api/v1/dashboard/metrics?dimension=month&metrics=GMV")
        ).json()["data"]

    assert "order_status = '已支付'" in data["sql"]


async def test_a_time_range_narrows_the_result() -> None:
    """时间范围要真的收窄结果，而不只是写进 SQL 里好看。

    这里用 ``year,month`` 而不是 ``month``：``dim_date.month`` 是月份序号（1~12），
    单看它每年长得一模一样，圈不圈年份都是 12 行，断言不出任何东西。
    """
    current[0] = ANALYST

    async with _client() as client:
        everything = (
            await client.get("/api/v1/dashboard/metrics?dimension=year,month&metrics=GMV")
        ).json()["data"]
        one_year = (
            await client.get(
                "/api/v1/dashboard/metrics?dimension=year,month&metrics=GMV"
                "&start=2025-01-01&end=2025-12-31"
            )
        ).json()["data"]

    assert 0 < one_year["row_count"] < everything["row_count"]
    assert {row[0] for row in one_year["rows"]} == {2025}
    assert "BETWEEN 20250101 AND 20251231" in one_year["sql"]


async def test_a_single_day_range_is_a_valid_edge() -> None:
    """只给一端也要能用：`start` 与 `end` 各自可省。"""
    current[0] = ANALYST

    async with _client() as client:
        data = (
            await client.get(
                "/api/v1/dashboard/metrics?dimension=year&metrics=order_count&start=2025-01-01"
            )
        ).json()["data"]

    assert ">= 20250101" in data["sql"]


# ==========================================================================
# 参数校验
# ==========================================================================


async def test_an_unknown_dimension_or_metric_is_rejected() -> None:
    current[0] = ANALYST

    async with _client() as client:
        bad_dimension = await client.get("/api/v1/dashboard/metrics?dimension=customer_name")
        bad_metric = await client.get("/api/v1/dashboard/metrics?dimension=month&metrics=利润")

    assert bad_dimension.status_code == 400
    assert "不支持的聚合维度" in bad_dimension.json()["message"]
    assert bad_metric.status_code == 400
    assert "未知的指标" in bad_metric.json()["message"]


async def test_a_malformed_date_is_rejected() -> None:
    current[0] = ANALYST

    async with _client() as client:
        resp = await client.get(
            "/api/v1/dashboard/metrics?dimension=month&metrics=GMV&start=2025-13-99"
        )

    assert resp.status_code == 400
    assert "日期" in resp.json()["message"]


async def test_the_dimension_is_required() -> None:
    """不指定维度就没有可聚合的东西，请求本身不成立。"""
    current[0] = ANALYST

    async with _client() as client:
        resp = await client.get("/api/v1/dashboard/metrics?metrics=GMV")

    assert resp.status_code == 400
    assert resp.json()["code"] == 40000


# ==========================================================================
# 只使用授权范围内的表与字段
# ==========================================================================


async def test_the_dashboard_never_touches_a_table_outside_the_role_scope() -> None:
    """只授 fact_order 与 dim_date 时，出数的 SQL 里只该有这两张表。

    这是 FR-DASH-001 的核心：接口不是"进得来就行"，而是**每个角色只看得见自己那些表**，
    连拼 SQL 的模板都只由已授权的部分拼出来。
    """
    async with _client() as client:
        await grant_whole(client, "fact_order")
        await grant_whole(client, "dim_date")

        resp = await client.get(
            "/api/v1/dashboard/metrics?dimension=year,month&metrics=GMV,order_count"
        )

    assert resp.status_code == 200, resp.text
    data = resp.json()["data"]
    assert data["unrestricted"] is False
    assert referenced_tables(data["sql"]) <= {"fact_order", "dim_date"}


async def test_a_dimension_on_an_unauthorized_table_is_rejected() -> None:
    """维度落在没授权的维表上时直接 403，而不是绕过权限去查。"""
    async with _client() as client:
        await grant_whole(client, "fact_order")
        await grant_whole(client, "dim_date")

        denied = await client.get("/api/v1/dashboard/metrics?dimension=province&metrics=GMV")
        allowed = await client.get("/api/v1/dashboard/metrics?dimension=year&metrics=GMV")

    assert denied.status_code == 403
    assert allowed.status_code == 200


async def test_the_dimension_list_only_offers_what_the_role_may_use() -> None:
    """下拉框里出现一个选了就 403 的维度，比它干脆不出现更让人困惑。"""
    async with _client() as client:
        await grant_whole(client, "fact_order")
        await grant_whole(client, "dim_date")

        data = (await client.get("/api/v1/dashboard/dimensions")).json()["data"]

    names = {row["name"] for row in data["dimensions"]}
    assert {"year", "month"} <= names
    assert "province" not in names, "dim_region 没有授权"
    assert "brand" not in names, "dim_product 没有授权"


async def test_an_unconfigured_role_is_reported_as_unrestricted() -> None:
    """没配过权限 = 不设限（阶段 9 的口径），响应要如实说出来。"""
    current[0] = ANALYST

    async with _client() as client:
        data = (
            await client.get("/api/v1/dashboard/metrics?dimension=month&metrics=GMV")
        ).json()["data"]

    assert data["unrestricted"] is True
