"""阶段 5：/api/v1/query 的 SSE 契约与服务层收口。

不开流之前的部分（鉴权、入参校验）按统一信封返回；开流之后的一切失败都必须
变成 ``type=error`` 事件——响应头那时已经发出，改不了状态码了。这两条边界
分开测，是因为它们由完全不同的机制实现（handler vs. 服务层捕获）。
"""

from __future__ import annotations

import json
from collections.abc import AsyncIterator
from types import SimpleNamespace

import httpx
import pytest

from app.api.v1 import query as query_api
from app.core.exceptions import AppException, NotFoundError, QueryError, SQLSecurityError
from app.core.security import get_current_user
from app.services.query_service import QueryService, TurnContext
from main import app

BASE_URL = "http://test"
FAKE_USER = SimpleNamespace(id="u-1", username="tester", role="analyst", is_active=True)


@pytest.fixture(autouse=True)
def _authenticated():
    """绕过真实鉴权。

    阶段 5 验收的是查询链路，不是认证；认证本身在阶段 3 已单独覆盖，
    这里若也去真登录，就要依赖库里存在特定账号与口令，把一个纯逻辑测试
    绑到了环境上。
    """
    app.dependency_overrides[get_current_user] = lambda: FAKE_USER
    yield
    app.dependency_overrides.pop(get_current_user, None)


def _client() -> httpx.AsyncClient:
    return httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url=BASE_URL)


def parse_sse(body: str) -> list[tuple[str, dict]]:
    """把 SSE 响应体解析成 (事件名, 数据) 列表。

    自己解析而不是拿现成库：这里要断言的正是帧格式本身（event 行、data 行、
    帧间空行），用库解析会把"格式写错"这件事也一起容忍掉。
    """
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
# 开流之前：仍然走统一信封
# ==========================================================================


async def test_query_requires_authentication() -> None:
    app.dependency_overrides.pop(get_current_user, None)
    async with _client() as client:
        resp = await client.post("/api/v1/query", json={"query": "去年华东区销售额前三的省份"})

    assert resp.status_code == 401
    assert resp.json()["code"] == 40100


@pytest.mark.parametrize("payload", [{"query": ""}, {}, {"query": "x" * 501}])
async def test_invalid_payload_is_rejected_before_streaming(payload: dict) -> None:
    """入参不合法要在开流**之前**拦下，这样才是 400 而不是流里的一条 error。"""
    async with _client() as client:
        resp = await client.post("/api/v1/query", json=payload)

    assert resp.status_code == 400
    assert resp.json()["code"] == 40000


async def test_unknown_session_is_rejected_before_streaming(monkeypatch) -> None:
    """会话不存在（或不属于本人）也要在开流前拦下，给一个 404 信封。

    SSE 的响应头在第一次迭代事件流时就发出去了，此后改不了状态码。若把归属校验
    放进流里，越权访问只能表现为连接被掐断——没有状态码、没有原因，
    而 TDD 7.1 要求它是一条正常的 404。
    """
    StubService.SCRIPT = [{"type": "done", "correction_count": 0, "row_count": 0}]
    monkeypatch.setattr(query_api, "QueryService", StubService)

    async with _client() as client:
        resp = await client.post(
            "/api/v1/query", json={"query": "那华东呢", "session_id": "missing"}
        )

    assert resp.status_code == 404
    assert resp.json()["code"] == 40400
    assert resp.headers["content-type"].startswith("application/json")


# ==========================================================================
# 开流之后：SSE 帧
# ==========================================================================


class StubService:
    """按脚本吐事件的服务，用来单独校验接口层的封装。

    阶段 7 起 ``prepare`` 是接口层在开流前必须调的一步（会话归属校验要给出
    404 而不是断流），这里一并假掉。
    """

    SCRIPT: list[dict] = []

    def __init__(self, session=None) -> None:
        pass

    async def prepare(self, user_id: str, session_id: str | None):
        if session_id == "missing":
            raise NotFoundError("会话不存在")
        return None

    async def stream(
        self, query: str, *, user_id: str, role: str = "viewer", turn=None
    ) -> AsyncIterator[dict]:
        for event in self.SCRIPT:
            yield event


async def test_sse_frames_carry_event_name_and_self_describing_data(monkeypatch) -> None:
    StubService.SCRIPT = [
        {"type": "progress", "step": "extract_keywords", "status": "start"},
        {"type": "sql", "sql": "SELECT province FROM fact_order"},
        {"type": "result", "data": {"columns": ["province"], "rows": [["上海市"]], "row_count": 1}},
        {"type": "done", "correction_count": 0, "row_count": 1},
    ]
    monkeypatch.setattr(query_api, "QueryService", StubService)

    async with _client() as client:
        resp = await client.post("/api/v1/query", json={"query": "去年华东区销售额前三的省份"})

    assert resp.status_code == 200
    assert resp.headers["content-type"].startswith("text/event-stream")
    frames = parse_sse(resp.text)
    assert [name for name, _data in frames] == ["progress", "sql", "result", "done"]
    # data 里也留着 type，单帧捞出来看时不必回头看是哪一行 event
    assert frames[1][1]["type"] == "sql"
    assert frames[1][1]["sql"] == "SELECT province FROM fact_order"


async def test_chinese_is_not_escaped_into_codepoints(monkeypatch) -> None:
    """默认的 ensure_ascii 会把中文写成 \\uXXXX，字节翻几倍且人不可读。"""
    StubService.SCRIPT = [{"type": "result", "data": {"rows": [["上海市"]]}}]
    monkeypatch.setattr(query_api, "QueryService", StubService)

    async with _client() as client:
        resp = await client.post("/api/v1/query", json={"query": "q"})

    assert "上海市" in resp.text
    assert "\\u4e0a" not in resp.text


async def test_the_current_users_role_is_handed_to_the_service(monkeypatch) -> None:
    """当前用户的角色要一路传到服务层——阶段 9 的数据权限按它算。

    ``QueryService.stream`` 的 ``role`` 有一个 ``"viewer"`` 默认值，那是给单测与
    评测干跑用的。接口层若忘了传，问答照样跑完、事件流照样完整，只是**权限一律按
    viewer 算**：一个 admin 或 data_owner 提问会莫名其妙地受限，而没有任何一处报错。
    这类"漏传一个参数、行为静默变错"的失配，只能靠在边界上钉一次。
    """
    seen: list[str] = []

    class Recording(StubService):
        async def stream(
            self, query: str, *, user_id: str, role: str = "viewer", turn=None
        ) -> AsyncIterator[dict]:
            seen.append(role)
            yield {"type": "done", "correction_count": 0, "row_count": 0}

    monkeypatch.setattr(query_api, "QueryService", Recording)

    async with _client() as client:
        await client.post("/api/v1/query", json={"query": "各季度销售额"})

    assert seen == [FAKE_USER.role]


# ==========================================================================
# 服务层收口：异常怎么变成事件
# ==========================================================================


class ScriptedGraph:
    """假图：按脚本产出 (mode, chunk)，或在中途抛异常。"""

    def __init__(self, *, events: list[dict] | None = None, final: dict | None = None, raises=None):
        self.events = events or []
        self.final = final or {}
        self.raises = raises

    async def astream(self, state, stream_mode=None, context=None, config=None):
        for event in self.events:
            yield ("custom", event)
        if self.raises is not None:
            raise self.raises
        yield ("values", {**state, **self.final})


async def collect(graph) -> list[dict]:
    """跑一遍假图，只看事件。

    ``QueryService(session=None)`` 即不落库（见那个构造器的说明）：这条用例验证的是
    "异常怎么变成事件"，与持久化无关，真连库只会把一个纯逻辑用例绑到环境上。
    """
    turn = TurnContext(graph=graph)
    return [e async for e in QueryService().stream("问题", user_id="u-1", turn=turn)]


async def test_business_exception_becomes_a_single_error_event() -> None:
    """流已开始，业务异常只能变成事件——message 直接给用户看。"""
    events = await collect(ScriptedGraph(raises=SQLSecurityError("SQL 未通过安全校验")))

    assert events[-1]["type"] == "error"
    assert events[-1]["code"] == 40203
    assert [e["type"] for e in events].count("error") == 1


async def test_unexpected_exception_does_not_leak_internals() -> None:
    """未预期异常对外只给通用提示（与 core.handlers 同一条规矩）。"""
    events = await collect(ScriptedGraph(raises=RuntimeError("mysql://user:pw@10.0.0.1 refused")))

    assert events[-1]["code"] == 50000
    assert "10.0.0.1" not in events[-1]["message"]
    assert "pw" not in events[-1]["message"]


async def test_giving_up_after_three_corrections_reports_the_last_reason() -> None:
    """图带着错误停下时，报一次错并说清楚"试了几次、最后卡在哪"。"""
    events = await collect(
        ScriptedGraph(final={"error": "SQL 预检未通过：Unknown column 'nope'", "error_code": 40201})
    )

    assert events[-1]["type"] == "error"
    assert events[-1]["code"] == 40201
    assert "3 次" in events[-1]["message"]
    assert "Unknown column 'nope'" in events[-1]["message"]


async def test_successful_run_ends_with_done_not_error() -> None:
    events = await collect(ScriptedGraph(final={"error": None, "correction_count": 1, "row_count": 3}))

    assert events == [{"type": "done", "correction_count": 1, "row_count": 3}]


async def test_intermediate_validation_failures_are_not_reported_as_errors() -> None:
    """校正循环里的中间失败不该推 error——用户会看到一串马上被修好的报错。"""
    events = await collect(
        ScriptedGraph(
            events=[{"type": "progress", "step": "validate_sql", "status": "start"}],
            final={"error": None, "correction_count": 2, "row_count": 1},
        )
    )

    assert [e["type"] for e in events] == ["progress", "done"]


def test_app_exception_subclasses_all_expose_a_message() -> None:
    """收口逻辑依赖 exc.message / exc.code，这里把契约钉住，防止将来有人只改其一。"""
    for exc in (QueryError(), SQLSecurityError(), AppException("自定义")):
        assert isinstance(exc.message, str) and exc.message
        assert isinstance(exc.code, int)
