"""阶段 5 验收：单轮 Text-to-SQL Agent（《开发步骤文档》阶段 5 的四条标准）。

  1. 输入「去年华东区销售额前三的省份」返回正确结果
  2. SSE 流式推送进度 / 结果 / SQL
  3. SQL 只读校验生效、写操作被拒
  4. 校验失败连续校正 3 次仍不通过时终止并返回明确错误

第 1、2 条必须调真实大模型（``requires_llm``），否则验的是桩不是链路。
第 3、4 条反过来用桩喂进**恶意/错误**的 SQL：真实模型极少主动生成写语句，
指望它来触发安全闸门等于指望这条防线不被测试；把桩接在模型位置上，
才是对闸门本身的确定性考验，而且这两条同时用真的 EXPLAIN 与真的只读账号，
不是纯单元测试。

外部服务没起时整体跳过（见 conftest.py）；跳过只针对"服务没监听"，
服务在跑但结果不对，必须失败。
"""

from __future__ import annotations

import json
from types import SimpleNamespace

import httpx
import pytest
from sqlalchemy import text
from sqlalchemy.exc import SQLAlchemyError

from app.agent.context import QueryContext
from app.agent.graph import build_graph
from app.agent.state import MAX_SQL_CORRECTIONS
from app.clients.mysql import get_dw_engine
from app.core.security import get_current_user
from app.services.query_service import QueryService, TurnContext
from main import app

pytestmark = pytest.mark.requires_stack

BASE_URL = "http://test"
ACCEPTANCE_QUESTION = "去年华东区销售额前三的省份"
EXPECTED_PROVINCES = ["上海市", "浙江省", "江西省"]

FAKE_USER = SimpleNamespace(id="u-acceptance", username="acceptance", role="analyst", is_active=True)


@pytest.fixture(autouse=True)
def _authenticated():
    """绕过鉴权：本模块验的是查询链路，不是认证（认证由阶段 3 的用例覆盖）。

    这样也避免把测试绑到"库里恰好存在某个账号及其口令"上。
    """
    app.dependency_overrides[get_current_user] = lambda: FAKE_USER
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


# ==========================================================================
# 验收 1、2：真实链路
# ==========================================================================


@pytest.mark.requires_llm
async def test_acceptance_question_returns_the_top_three_provinces() -> None:
    """输入验收问句，前三名省份必须正确且有序。"""
    async with _client() as client:
        resp = await client.post("/api/v1/query", json={"query": ACCEPTANCE_QUESTION})

    assert resp.status_code == 200
    frames = parse_sse(resp.text)
    results = [data for name, data in frames if name == "result"]
    errors = [data for name, data in frames if name == "error"]
    assert not errors, f"链路报错：{errors}"

    assert len(results) == 1
    payload = results[0]["data"]
    rows = payload["rows"]
    # 打印出来，人也能一眼看到答案对不对（验收要的是"结果正确"，不只是断言通过）
    print(f"\n验收问句：{ACCEPTANCE_QUESTION}")
    print(f"生成 SQL：{[d['sql'] for n, d in frames if n == 'sql'][0]}")
    for row in rows:
        print(f"  {row}")

    assert len(rows) == 3, "「前三」必须正好三行"
    assert [row[0] for row in rows] == EXPECTED_PROVINCES
    amounts = [float(row[1]) for row in rows]
    assert amounts == sorted(amounts, reverse=True), "必须按销售额倒序"


@pytest.mark.requires_llm
async def test_sse_pushes_progress_sql_and_result_in_order() -> None:
    """进度、SQL、结果都要真实推出来，且 SQL 先于结果。"""
    async with _client() as client:
        resp = await client.post("/api/v1/query", json={"query": ACCEPTANCE_QUESTION})

    names = [name for name, _data in parse_sse(resp.text)]

    assert "progress" in names and "sql" in names and "result" in names
    assert names[-1] == "done"
    assert names.index("sql") < names.index("result")
    # 全程进度不只一条：至少要看到关键字抽取、召回、生成、执行这几个阶段
    steps = {data["step"] for name, data in parse_sse(resp.text) if name == "progress"}
    assert {"extract_keywords", "generate_sql", "validate_sql", "execute_sql"} <= steps

    body = resp.text
    assert body.startswith("event: progress"), "第一帧就该是进度，用户先看到的是它在动"


@pytest.mark.requires_llm
async def test_generated_sql_is_readonly() -> None:
    """真模型产出的语句本身也必须落在只读白名单里。"""
    from app.core.sql_security import assert_readonly

    async with _client() as client:
        resp = await client.post("/api/v1/query", json={"query": ACCEPTANCE_QUESTION})

    sql = next(data["sql"] for name, data in parse_sse(resp.text) if name == "sql")
    assert assert_readonly(sql) == sql


# ==========================================================================
# 验收 3：写操作被拒
# ==========================================================================


class ScriptedLLM:
    """把模型位置换成"永远输出这条 SQL"的桩。

    真实模型几乎不会主动生成 DROP，靠它触发安全闸门等于不测这道闸门。
    """

    def __init__(self, sql: str) -> None:
        self.sql = sql

    async def text(self, prompt: str, *, system: str | None = None) -> str:
        return self.sql


def stub_context(sql: str) -> QueryContext:
    """真实依赖 + 桩模型：除"模型输出什么"以外，其余都是真的。"""
    context = QueryContext(user_id="u-acceptance")
    context.llm = ScriptedLLM(sql)
    return context


def patch_context(monkeypatch: pytest.MonkeyPatch, sql: str) -> QueryContext:
    """把服务层要构造的 context 换成桩版本。

    注意这里替换的是 ``QueryService`` 里的那个名字，而不是冒充整张图——图、检索、
    知识库、EXPLAIN、数仓连接全都是真的。桩只落在"模型会说什么"这一个变量上，
    这正是验收第 3、4 条需要的确定性。
    """
    context = stub_context(sql)
    monkeypatch.setattr("app.services.query_service.QueryContext", lambda **_kwargs: context)
    return context


async def run_with_sql(monkeypatch: pytest.MonkeyPatch, sql: str) -> list[dict]:
    """走完整的服务层路径（含终局 error 事件的措辞），返回事件序列。

    无会话、不落库（``QueryService()`` 的 session 为 None）：这几条用例验的是
    安全闸门与错误措辞，与持久化无关。
    """
    patch_context(monkeypatch, sql)
    # 图不带 checkpointer：本用例没有会话，要的是一次性的单轮闭环
    turn = TurnContext(graph=build_graph())
    return [
        e
        async for e in QueryService().stream(
            ACCEPTANCE_QUESTION, user_id="u-acceptance", turn=turn
        )
    ]


@pytest.mark.parametrize(
    "write_sql",
    [
        "DROP TABLE fact_order",
        "DELETE FROM fact_order WHERE 1 = 1",
        "UPDATE fact_order SET amount = 0",
        "SELECT 1; DROP TABLE fact_order",
    ],
)
async def test_write_statements_are_refused_and_never_reach_the_dw(
    monkeypatch: pytest.MonkeyPatch, write_sql: str
) -> None:
    events = await run_with_sql(monkeypatch, write_sql)

    assert events[-1]["type"] == "error"
    assert events[-1]["code"] == 40203, "安全拒止应当是 40203（SQLSecurityError）"
    kinds = [e["type"] for e in events]
    # 既没有结果集，也没有把语句送出去执行——sql 事件只在 execute_sql 里推
    assert "result" not in kinds and "sql" not in kinds


async def test_the_dw_account_itself_cannot_write() -> None:
    """第二道防线：即便校验被绕过，只读账号也写不进去（ADR-008 / NFR-SEC-007）。

    连接池整个生命周期只配了只读账号，这里真的去建一张表，让 MySQL 自己拒绝。
    """
    async with get_dw_engine().connect() as conn:
        with pytest.raises(SQLAlchemyError) as excinfo:
            await conn.execute(text("CREATE TABLE _pytest_write_probe (a INT)"))

    assert "denied" in str(excinfo.value).lower() or "command" in str(excinfo.value).lower()


# ==========================================================================
# 验收 4：连续校正 3 次后终止
# ==========================================================================


async def test_unfixable_sql_terminates_after_exactly_three_corrections(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """喂一条"语法上只读、但列不存在"的 SQL：EXPLAIN 会失败，回边反复触发。

    用真实的 validate_sql 与真实的 EXPLAIN（只是模型位置是桩，它每次都原样返回
    同一条坏 SQL），因此验的是"真实预检失败时图怎么收敛"，而不是桩的行为。
    """
    events = await run_with_sql(monkeypatch, "SELECT no_such_column_zzz FROM fact_order")

    corrections = [e for e in events if e.get("step") == "correct_sql" and e["status"] == "start"]
    assert len(corrections) == MAX_SQL_CORRECTIONS, "校正次数必须正好等于上限"

    last = events[-1]
    assert last["type"] == "error"
    assert f"{MAX_SQL_CORRECTIONS} 次" in last["message"]
    assert "no_such_column_zzz" in last["message"], "错误里要带上根因，用户才知道改问法还是找数据团队"
    # 校正途中一次都不该推 error：中间那几次很快就被修掉了
    assert [e["type"] for e in events].count("error") == 1
    assert "result" not in [e["type"] for e in events], "始终跑不通的语句不该产出结果"
