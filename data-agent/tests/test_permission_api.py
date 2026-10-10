"""阶段 9：数据权限配置接口（FR-PERM-002，TDD 7.2 permission 组）。

需要 MySQL（``requires_stack``）：这一组验的是"配得进去、取得出来、算得对"，
而"算得对"要拿真实的知识库（表/字段的 id 与名字）来验。

**隔离靠快照，不靠随机命名**。历史接口那组用例彼此隔离的办法是每次运行换一批
用户 id；权限这里不成立——角色只有 viewer / analyst / admin 三个固定取值，用例
必须往真实角色上配。于是只能清理，而**不能直接删**：开发库是长期存在的，
验收时管理员会亲手配几条权限，被测试删掉就等于"配置自己会消失"。所以进出两头
各做一次快照与还原，用例期间该角色的配置为空，用例结束后原样放回。
"""

from __future__ import annotations

import uuid
from types import SimpleNamespace

import httpx
import pytest
from sqlalchemy import delete, select

from app.clients.mysql import session_scope
from app.core.security import get_current_user
from app.models import Permission
from main import app

pytestmark = pytest.mark.requires_stack

BASE_URL = "http://test"
RUN = uuid.uuid4().hex[:8]

#: 用例会碰到的角色。admin 不在其中：整组接口本来就要求 admin，没有配置的余地。
TOUCHED_ROLES = ("viewer", "analyst")

ADMIN = SimpleNamespace(id=f"u-perm-{RUN}", username="perm-admin", role="admin", is_active=True)
current = [ADMIN]


@pytest.fixture(autouse=True)
def _authenticated():
    current[0] = ADMIN
    app.dependency_overrides[get_current_user] = lambda: current[0]
    yield
    app.dependency_overrides.pop(get_current_user, None)


async def _snapshot() -> list[dict]:
    """取走这两个角色现有的全部权限行（用例结束后还原）。"""
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


async def _clear() -> None:
    async with session_scope() as session:
        await session.execute(delete(Permission).where(Permission.role.in_(TOUCHED_ROLES)))
        await session.commit()


async def _restore(rows: list[dict]) -> None:
    if not rows:
        return
    async with session_scope() as session:
        session.add_all([Permission(**row) for row in rows])
        await session.commit()


@pytest.fixture(autouse=True)
async def _isolate():
    """用例期间这两个角色没有任何配置，用完把原来的放回去。

    还原放在 finally 里：用例失败（甚至是在夹具之后的断言里失败）时配置也必须回到
    原样，否则一次失败会顺手把开发者的权限配置吃掉。
    """
    saved = await _snapshot()
    await _clear()
    try:
        yield
    finally:
        await _clear()
        await _restore(saved)


def _client() -> httpx.AsyncClient:
    return httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url=BASE_URL)


async def catalog() -> dict:
    """可授权资源清单。用例要拿真实的表/字段 id 去配权限，只能从这儿取。"""
    async with _client() as client:
        resp = await client.get("/api/v1/permissions/resources")
    assert resp.status_code == 200, resp.text
    return resp.json()["data"]


async def pick_table() -> dict:
    return (await catalog())["tables"][0]


async def pick_column() -> dict:
    return (await catalog())["columns"][0]


def grant(role: str, resource: dict) -> dict:
    return {"role": role, "resource_type": resource["resource_type"], "resource_id": resource["id"]}


# ==========================================================================
# 可授权资源清单
# ==========================================================================


async def test_catalog_lists_tables_and_columns_with_their_names() -> None:
    """清单必须带名字：``resource_id`` 是 uuid5，界面上认不出来。"""
    data = await catalog()

    assert data["tables"] and data["columns"]
    table = data["tables"][0]
    assert table["id"] and table["name"] and table["resource_type"] == "table"
    column = data["columns"][0]
    assert column["id"] and column["name"] and column["resource_type"] == "column"
    # 字段要知道自己属于哪张表，否则界面上只能拉一个几百项的平铺列表
    assert column["table_name"] in {row["name"] for row in data["tables"]}


# ==========================================================================
# 增删改查
# ==========================================================================


async def test_an_admin_can_grant_and_then_see_the_mapping() -> None:
    table = await pick_table()

    async with _client() as client:
        created = await client.post("/api/v1/permissions", json=grant("viewer", table))
        assert created.status_code == 200, created.text
        body = created.json()["data"]
        assert body["role"] == "viewer"
        assert body["resource_name"] == table["name"], "响应里要带上资源的名字"

        listing = await client.get("/api/v1/permissions?role=viewer")
        items = listing.json()["data"]["items"]

    assert [row["id"] for row in items] == [body["id"]]


async def test_the_same_triple_cannot_be_granted_twice() -> None:
    """重复配置返回 400 而不是静默成功。

    静默成功会让"配了两次"与"配了一次"看起来一样，而界面上的勾选状态是从这份数据
    反推的——多出来的那条在列表里也看不出来，只会让分页数字对不上。
    """
    table = await pick_table()

    async with _client() as client:
        assert (await client.post("/api/v1/permissions", json=grant("viewer", table))).status_code == 200
        again = await client.post("/api/v1/permissions", json=grant("viewer", table))

    assert again.status_code == 400
    assert "已有" in again.json()["message"]


async def test_granting_a_resource_that_does_not_exist_is_rejected() -> None:
    """配一条指向不存在资源的权限，比报错危险得多——界面上它会显示成"已授权"。"""
    async with _client() as client:
        resp = await client.post(
            "/api/v1/permissions",
            json={"role": "viewer", "resource_type": "table", "resource_id": "不存在的表id"},
        )

    assert resp.status_code == 400
    assert "不存在" in resp.json()["message"]


async def test_a_mapping_can_be_replaced_and_deleted() -> None:
    table = await pick_table()
    column = await pick_column()

    async with _client() as client:
        created = (
            await client.post("/api/v1/permissions", json=grant("viewer", table))
        ).json()["data"]

        replaced = await client.put(
            f"/api/v1/permissions/{created['id']}", json=grant("analyst", column)
        )
        assert replaced.status_code == 200, replaced.text
        assert replaced.json()["data"]["role"] == "analyst"
        assert replaced.json()["data"]["resource_id"] == column["id"]

        assert (await client.delete(f"/api/v1/permissions/{created['id']}")).status_code == 200
        assert (await client.get("/api/v1/permissions")).json()["data"]["total"] == 0

        # 改归属之后，旧角色名下不该还留着它——"改了角色"没生效是最难发现的一种错
        assert (await client.get("/api/v1/permissions?role=viewer")).json()["data"]["total"] == 0


async def test_unknown_mapping_returns_404() -> None:
    async with _client() as client:
        assert (await client.delete("/api/v1/permissions/没有这条")).status_code == 404


async def test_an_unknown_role_is_rejected_before_it_reaches_the_service() -> None:
    """角色取值由字面量约束，非法值挡在入参校验上（本项目的校验失败统一 400）。"""
    table = await pick_table()

    async with _client() as client:
        resp = await client.post("/api/v1/permissions", json=grant("root", table))

    assert resp.status_code == 400
    assert resp.json()["code"] == 40000


# ==========================================================================
# 实际生效范围
# ==========================================================================


async def test_a_role_without_configuration_is_unrestricted() -> None:
    """没配过 = 不设限。这是全新环境的默认语义，接口要如实说出来。

    接口返回 ``unrestricted=true`` 而不是"一张空表"：管理界面若只显示空列表，
    会把"不设限"看成"什么都还没配好"，而两者的实际行为正好相反。
    """
    async with _client() as client:
        scope = (await client.get("/api/v1/permissions/roles/viewer/scope")).json()["data"]

    assert scope["unrestricted"] is True
    assert scope["forbidden_tables"] == []
    assert scope["tables"] == {}


async def test_the_scope_reflects_what_was_configured() -> None:
    table = await pick_table()

    async with _client() as client:
        await client.post("/api/v1/permissions", json=grant("viewer", table))
        scope = (await client.get("/api/v1/permissions/roles/viewer/scope")).json()["data"]

    assert scope["unrestricted"] is False
    assert scope["tables"] == {table["name"]: None}, "整表授权要表现成「整表」（null），而不是列一堆字段名"
    assert table["name"] not in scope["forbidden_tables"]
    assert scope["forbidden_tables"], "除了这一张，其余的表现在都算越权"


async def test_a_column_grant_narrows_only_that_table() -> None:
    column = await pick_column()
    siblings = {
        row["name"]
        for row in (await catalog())["columns"]
        if row["table_name"] == column["table_name"] and row["name"] != column["name"]
    }

    async with _client() as client:
        await client.post("/api/v1/permissions", json=grant("viewer", column))
        scope = (await client.get("/api/v1/permissions/roles/viewer/scope")).json()["data"]

    assert scope["tables"] == {column["table_name"]: [column["name"]]}
    # 不许用的字段挂在表下面，而不是给一份平铺的名字清单：字段名在数仓里不唯一
    # （fact_order.product_id 与 dim_product.product_id 同名），平铺之后这一栏
    # 就没法回答"到底是哪个字段不许用"了
    assert set(scope["forbidden_columns"]) == {column["table_name"]}
    assert set(scope["forbidden_columns"][column["table_name"]]) == siblings


# ==========================================================================
# 权限本身要求 admin
# ==========================================================================


async def test_a_non_admin_cannot_touch_permissions() -> None:
    """整组接口要求 admin，角色不足直接 403（TDD 7.2）。"""
    current[0] = SimpleNamespace(id="u-perm-viewer", username="v", role="viewer", is_active=True)

    async with _client() as client:
        listing = await client.get("/api/v1/permissions")
        creating = await client.post("/api/v1/permissions", json={"role": "viewer", "resource_type": "table", "resource_id": "x"})

    assert listing.status_code == 403
    # 写接口尤其要挡住：读不到配置是信息泄露，改得动配置是越权
    assert creating.status_code == 403
