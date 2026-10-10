"""阶段 9 验收：数据权限治理（《开发步骤文档》阶段 9 的两条验收标准）。

  1. 无权限的表/字段不出现在生成的 SQL 中
  2. admin 可配置角色-表/字段映射

第 1 条走**真实的**问答链路（真模型、真检索、真数仓）。这一条的实质是"模型会不会
越权"，而越权的典型形态正是"它自作主张补了个 JOIN"——用桩模型测等于测我们自己的
提示词拼接，而桩不会自作主张。第 2 条同样走真链路，因为它要的不是"接口能写进库"
（``test_permission_api`` 已经逐条验过），而是**改完配置下一句提问就按新的走**：
网关在一轮问答内缓存角色范围，跨请求各建各的实例，"没有缓存失效这一步"是个设计
决定，只有走一次真问答才验得到。

**判定方向是"不出现"，不是"等于什么"**。所以本文件的断言写成
``授权集合之外的名字一律不许出现``，而不是``SQL 应当等于某一句``：
后者要求模型每次都答得一模一样，会随着模型的措辞变化而红，而红的原因与权限无关。

权限配在 ``analyst`` 上（验收用户就是这个角色）。角色只有三个固定取值，隔离不了，
只能清理——而开发库里的配置是管理员亲手配的，删掉就等于"配置自己会消失"，
所以进出两头各快照/还原一次（同 ``test_permission_api`` 的 ``_isolate``）。
"""

from __future__ import annotations

import itertools
import json
import uuid
from types import SimpleNamespace

import httpx
import pytest
from sqlalchemy import delete, select

from app.clients.mysql import session_scope
from app.core.security import get_current_user
from app.core.sql_security import referenced_names, referenced_tables
from app.models import Permission
from main import app

#: 必须有真模型：整份文件验的都是"模型拿到裁剪后的上下文之后会不会越权"。
pytestmark = [pytest.mark.requires_stack, pytest.mark.requires_llm]

BASE_URL = "http://test"
RUN = uuid.uuid4().hex[:8]
_serial = itertools.count()

ROLE = "analyst"
TOUCHED_ROLES = ("analyst",)

ADMIN = SimpleNamespace(id=f"u-acc9-admin-{RUN}", username="acc9-admin", role="admin", is_active=True)


def new_user() -> SimpleNamespace:
    tag = f"u{next(_serial)}"
    return SimpleNamespace(id=f"u-acc9-{tag}-{RUN}", username=f"acc9-{tag}", role=ROLE, is_active=True)


current: list[SimpleNamespace] = [ADMIN]


@pytest.fixture(autouse=True)
def _authenticated():
    current[:] = [new_user()]
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


async def _clear() -> None:
    async with session_scope() as session:
        await session.execute(delete(Permission).where(Permission.role.in_(TOUCHED_ROLES)))
        await session.commit()


@pytest.fixture(autouse=True)
async def _isolate():
    """用例期间 analyst 没有任何配置，用完把原来的放回去。

    快照与还原都放在夹具里而不是各用例自己管：验收用例中途失败的可能性不低
    （它要调真模型），而一旦失败就顺手吃掉管理员的权限配置，是最不该有的副作用。
    """
    saved = await _snapshot()
    await _clear()
    try:
        yield
    finally:
        await _clear()
        if saved:
            async with session_scope() as session:
                session.add_all([Permission(**row) for row in saved])
                await session.commit()


def _client() -> httpx.AsyncClient:
    return httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url=BASE_URL, timeout=180.0
    )


async def as_admin(call):
    """以 admin 身份调一次接口再换回来。

    资源清单与权限配置都是 admin 接口，而验收用户是 analyst（受限的才是他）。
    身份在 ``current`` 这个单元素列表里换，换回来放 ``finally``——
    漏换回来的症状是"后面那句提问被当成 admin 问的"，而用例照样绿。
    """
    previous = current[0]
    current[0] = ADMIN
    try:
        return await call()
    finally:
        current[0] = previous


async def catalog() -> dict:
    """知识库里的表与字段（拿真实 id 去授权，拿真实名字去判越权）。"""

    async def fetch() -> dict:
        async with _client() as client:
            resp = await client.get("/api/v1/permissions/resources")
        assert resp.status_code == 200, resp.text
        return resp.json()["data"]

    return await as_admin(fetch)


async def grant(role: str, resource: dict) -> None:
    """以 admin 身份配一条权限（走真实接口，不是直接写库）。

    直接写库会绕过"资源必须存在"等校验，验出来的就不是同一条路径了。
    """
    async def post() -> None:
        async with _client() as client:
            resp = await client.post(
                "/api/v1/permissions",
                json={
                    "role": role,
                    "resource_type": resource["resource_type"],
                    "resource_id": resource["id"],
                },
            )
        assert resp.status_code == 200, resp.text

    await as_admin(post)


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


async def run(question: str) -> list[tuple[str, dict]]:
    async with _client() as client:
        resp = await client.post("/api/v1/query", json={"query": question})
    assert resp.status_code == 200, resp.text
    return parse_sse(resp.text)


def payload_of(frames: list[tuple[str, dict]], kind: str) -> dict | None:
    for name, data in frames:
        if name == kind:
            return data
    return None


def sql_of(frames: list[tuple[str, dict]]) -> str | None:
    frame = payload_of(frames, "sql")
    return frame["sql"] if frame else None


def forbidden(facts: dict, authorized: dict[str, set[str] | None]) -> tuple[set[str], dict[str, set[str]]]:
    """按"应该有哪些权限"算出不许出现的表名，与各表里不许出现的字段名。

    ``authorized`` 是"表 → 可见字段；``None`` 表示整表"。在**测试侧**重新算一遍，
    不复用被测代码的 ``PermissionScope``：拿被测实现的判定结果去当期望值，
    接错线也照样绿（它算错了，期望值就跟着错）。

    字段按表分组而不是给一份平铺的名字清单，理由与被测实现相同：
    ``fact_order.product_id`` 与 ``dim_product.product_id`` 同名，平铺之后
    "这个字段在这一句里能不能用"就没法回答——第一版就是栽在这上面。
    """
    tables = {row["name"] for row in facts["tables"]}
    banned_tables = tables - set(authorized)
    banned_columns = {
        name: {
            row["name"]
            for row in facts["columns"]
            if row["table_name"] == name and row["name"] not in (columns or set())
        }
        for name, columns in authorized.items()
        if columns is not None
    }
    return banned_tables, banned_columns


def assert_clean(
    frames: list[tuple[str, dict]], banned_tables: set[str], banned_columns: dict[str, set[str]]
) -> None:
    """无权限的表/字段，一个都不许出现在用户看得见的任何地方。

    三处都查：
      - ``sql`` 事件 —— 验收标准第 1 条的原文；
      - ``explain`` 事件 —— 解释是写给用户看的，里面点名一张他没权限的表，
        等于告诉他"有这么一张表、里面大概是这些数据"；
      - ``error`` 事件 —— 同理。这也是实现里"越权提示不点名"那条取舍的判据。

    字段只在 SQL 里查、且按它出现的表查：表名进不了解释文本的概率低、字段名则不然
    （"订单金额"对应的 ``order_amount`` 完全可能被模型写进解释里），
    而不分表地查字段名会把同名的那几个（``region_id`` 在事实表与维表里各有一份）
    一律算成泄漏。
    """
    sql = sql_of(frames)
    if sql:
        hits = referenced_tables(sql) & banned_tables
        assert not hits, f"生成的 SQL 引用了未授权的表：{hits}\n{sql}"
        names = referenced_names(sql)
        leaked = {
            f"{table}.{column}"
            for table, columns in banned_columns.items()
            for column in names & columns
        }
        assert not leaked, f"生成的 SQL 引用了未授权的字段：{sorted(leaked)}\n{sql}"

    for kind in ("explain", "error"):
        text = json.dumps(payload_of(frames, kind) or {}, ensure_ascii=False)
        leaked_tables = sorted(name for name in banned_tables if name in text)
        assert not leaked_tables, f"{kind} 事件里出现了未授权的表名 {leaked_tables}：{text}"


# ==========================================================================
# 验收标准 1：无权限的表 / 字段不出现在生成的 SQL 中
# ==========================================================================


async def test_a_restricted_role_only_ever_gets_sql_over_the_tables_it_may_use() -> None:
    """给一个角色开三张表，它问出来的 SQL 就只能用这三张表。

    这是**正向**那一半：受限不等于不可用。少了这一半，"永远报权限错误"也能让
    反向的断言全绿，而那显然不是我们要的东西。
    """
    facts = await catalog()
    by_name = {row["name"]: row for row in facts["tables"]}
    allowed = {"fact_order", "dim_date", "dim_region"}
    for name in allowed:
        await grant(ROLE, by_name[name])

    frames = await run("华东地区各季度销售额")
    sql = sql_of(frames)

    assert sql, f"授权了这三张表却没能生成 SQL：{frames}"
    banned_tables, banned_columns = forbidden(facts, dict.fromkeys(allowed))
    assert_clean(frames, banned_tables, banned_columns)
    # 提示词里只有这三张表，模型仍要能答出这个问题的正经写法
    assert referenced_tables(sql) & allowed, f"SQL 没用到任何授权的表：{sql}"


async def test_a_role_with_one_column_cannot_reach_the_rest_of_the_warehouse() -> None:
    """只授一个字段时，模型拿着一个答不了的问题也编不出越权的 SQL。

    这是**反向**那一半，也是这一条验收最吃劲的地方：问题（地区 × 季度 × 销售额）
    需要三张表、若干字段，而上下文里只有 ``dim_product.category`` 一个字段。
    模型此时要么拿这点东西硬凑，要么答不上来，两种都符合规格——
    所以用例不预设它走哪条路，只钉死"用户侧一处都不许出现未授权名字"。

    出错时那条 error 文案尤其要看：它必须**不点名**。点了一张表的名字，
    就是向一个没权限的人确认了"这张表存在、它大概长这样"。
    """
    facts = await catalog()
    column = next(row for row in facts["columns"] if row["table_name"] == "dim_product")
    await grant(ROLE, column)

    frames = await run("华东地区各季度销售额")

    banned_tables, banned_columns = forbidden(facts, {"dim_product": {column["name"]}})
    assert_clean(frames, banned_tables, banned_columns)

    # 被拦下时要是权限，不是"没找到表"——两种处置完全不同（找管理员 vs 换问法）
    error = payload_of(frames, "error")
    if error and not sql_of(frames):
        assert "权限" in error["message"] or "授权" in error["message"], error


# ==========================================================================
# 验收标准 2：admin 可配置角色-表/字段映射
# ==========================================================================


async def test_a_configuration_change_takes_effect_on_the_very_next_question() -> None:
    """管理员配一条权限，**下一句**提问就按新的走（验收标准第 2 条）。

    差分着断言：同一句问题、同一个用户，配置前后必须问出不一样的东西。
    只断言"配完之后不越权"是不够的——配之前就不越权（那时压根没限制），
    整个用例会在"配置根本没生效"的情况下全绿。

    要验的正是这一点：网关在一轮问答内缓存角色范围、跨请求各建各的实例，
    所以**没有"改完配置要清缓存"这一步**。这个决定错了的症状是"配了没反应，
    重启一下就好了"——在现场看起来像是偶发。
    """
    user = current[0]
    facts = await catalog()
    by_name = {row["name"]: row for row in facts["tables"]}

    # 未配置 = 不设限：这一句必须能正常查出来，且确实用到了订单事实表
    before = await run("订单金额总和是多少")
    sql_before = sql_of(before)
    assert sql_before, f"未配置权限时应当不受限制：{before}"
    assert "fact_order" in referenced_tables(sql_before), f"用例前提不成立：{sql_before}"

    # 管理员配一条权限：该角色从此只用得了 dim_product
    await grant(ROLE, by_name["dim_product"])
    assert current[0] is user, "授权只是换了个身份去调接口，不该把当前用户换掉"

    after = await run("订单金额总和是多少")
    sql_after = sql_of(after)

    assert not sql_after or "fact_order" not in referenced_tables(sql_after), (
        f"配置生效后仍然查到了未授权的表——权限配置没有在下一句生效：{sql_after}"
    )
    banned_tables, banned_columns = forbidden(facts, {"dim_product": None})
    assert_clean(after, banned_tables, banned_columns)


async def test_an_admin_can_read_back_the_effective_scope_of_a_role() -> None:
    """配完能立刻读回"算出来是什么"（验收标准第 2 条的闭环）。

    与列表接口回答的问题不同：列表说"配了什么"，这里说"算出来是什么"。
    管理员需要后者才能发现"配的字段已被知识库重建删掉"这类悬空配置——
    那时列表看着一条不少，实际一条都不生效。
    """
    facts = await catalog()
    column = next(row for row in facts["columns"] if row["table_name"] == "dim_product")
    await grant(ROLE, column)

    async def fetch() -> dict:
        async with _client() as client:
            resp = await client.get(f"/api/v1/permissions/roles/{ROLE}/scope")
        assert resp.status_code == 200, resp.text
        return resp.json()["data"]

    scope = await as_admin(fetch)

    assert scope["unrestricted"] is False
    assert scope["tables"] == {"dim_product": [column["name"]]}
    banned_tables, _ = forbidden(facts, {"dim_product": None})
    assert set(scope["forbidden_tables"]) == banned_tables
