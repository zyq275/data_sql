"""健康探针（FR-SYS-001、FR-SYS-002）。

前半部分不依赖任何外部服务：探针的骨架结构、失败判 503、超时兜底、口令脱敏
都是纯逻辑，必须能在没有 MySQL/Qdrant/ES 的机器上跑。
后半部分（真实依赖连通性）由 test_meta_knowledge.py 覆盖。
"""

from __future__ import annotations

import asyncio

import httpx
import pytest

from main import app

BASE_URL = "http://test"


def _client() -> httpx.AsyncClient:
    return httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url=BASE_URL)


# --------------------------------------------------------------------------
# /health 存活探针
# --------------------------------------------------------------------------


async def test_health_returns_200_without_touching_dependencies() -> None:
    async with _client() as client:
        resp = await client.get("/health")

    assert resp.status_code == 200
    body = resp.json()
    assert body["status"] == "ok"
    assert body["app"]
    assert body["version"]


async def test_health_is_not_wrapped_in_the_response_envelope() -> None:
    """探针按约定返回裸结构（TDD 7.2），前端与编排器都不该看到 code/message/data。"""
    async with _client() as client:
        body = (await client.get("/health")).json()

    assert set(body).isdisjoint({"code", "message", "data"})


# --------------------------------------------------------------------------
# /health/ready 就绪探针的结构
# --------------------------------------------------------------------------


async def test_readiness_reports_every_check_with_status_and_detail() -> None:
    async with _client() as client:
        resp = await client.get("/health/ready")

    body = resp.json()
    assert body["status"] in {"ok", "unavailable"}
    assert body["checks"], "就绪探针必须逐项报告依赖状态"
    for name, result in body["checks"].items():
        assert set(result) >= {"status", "detail"}, f"{name} 缺少状态或详情"
        assert result["status"] in {"ok", "fail", "skipped"}


async def test_readiness_marks_redis_as_skipped_not_ok() -> None:
    """Redis 到阶段 6 才接入，必须如实报 skipped。

    伪报 ok 就等于把这个依赖的故障推迟到线上才发现——探针存在的意义就是
    让未探测与探测通过两者可区分。
    """
    async with _client() as client:
        checks = (await client.get("/health/ready")).json()["checks"]

    assert checks["redis"]["status"] == "skipped"
    assert "阶段 6" in checks["redis"]["detail"]


# --------------------------------------------------------------------------
# 失败与超时：探针的核心职责
# --------------------------------------------------------------------------


async def test_readiness_returns_503_when_a_check_fails(monkeypatch: pytest.MonkeyPatch) -> None:
    from app.api import health

    async def _boom() -> str:
        raise RuntimeError("模拟依赖故障")

    monkeypatch.setattr(health, "CHECKS", [("failing_dep", _boom, 3.0)])

    async with _client() as client:
        resp = await client.get("/health/ready")

    assert resp.status_code == 503, "依赖不可用时必须 503，否则编排器不会摘流量"
    body = resp.json()
    assert body["status"] == "unavailable"
    assert body["checks"]["failing_dep"]["status"] == "fail"
    # 故障细节要能看出是哪种失败，否则运维只能盲猜
    assert "模拟依赖故障" in body["checks"]["failing_dep"]["detail"]


async def test_readiness_returns_503_on_a_hung_dependency(monkeypatch: pytest.MonkeyPatch) -> None:
    """卡死的依赖必须被超时兜住。

    探针挂起比探针失败更糟：编排器等不到结论，既不敢摘流量也不敢放流量。
    """
    from app.api import health

    async def _hang() -> str:
        await asyncio.sleep(30)
        return "永远不会到这里"

    monkeypatch.setattr(health, "CHECKS", [("hung_dep", _hang, 0.05)])

    async with _client() as client:
        resp = await client.get("/health/ready")

    assert resp.status_code == 503
    assert "超时" in resp.json()["checks"]["hung_dep"]["detail"]


async def test_one_failing_check_does_not_hide_the_others(monkeypatch: pytest.MonkeyPatch) -> None:
    """单项故障不能掩盖其余依赖的状态——运维需要一次看全，而不是修一个冒一个。"""
    from app.api import health

    async def _ok() -> str:
        return "正常"

    async def _boom() -> str:
        raise RuntimeError("坏了")

    monkeypatch.setattr(health, "CHECKS", [("good_dep", _ok, 3.0), ("bad_dep", _boom, 3.0)])

    async with _client() as client:
        checks = (await client.get("/health/ready")).json()["checks"]

    assert checks["good_dep"]["status"] == "ok"
    assert checks["bad_dep"]["status"] == "fail"


# --------------------------------------------------------------------------
# 口令脱敏：探针响应不走「细节只进日志」那条路，必须自己兜住（NFR-SEC-003）
# --------------------------------------------------------------------------


def test_redact_masks_credentials_embedded_in_a_connection_string() -> None:
    from app.api.health import _redact

    cleaned = _redact("mysql+asyncmy://readonly:sup3rsecret@127.0.0.1:3306/data_agent_dw 连接失败")

    assert "sup3rsecret" not in cleaned
    assert "readonly" in cleaned, "用户名不是敏感信息，保留才便于排查"
    assert "127.0.0.1" in cleaned


def test_redact_truncates_long_detail() -> None:
    from app.api.health import MAX_DETAIL_CHARS, _redact

    assert len(_redact("x" * 5000)) == MAX_DETAIL_CHARS


async def test_failing_check_detail_never_leaks_the_password(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """端到端确认脱敏真的接在响应上，而不只是 _redact 单测通过。"""
    from app.api import health

    async def _leaky() -> str:
        raise RuntimeError("连接 mysql://readonly:hunter2@127.0.0.1:3306/dw 被拒绝")

    monkeypatch.setattr(health, "CHECKS", [("leaky_dep", _leaky, 3.0)])

    async with _client() as client:
        resp = await client.get("/health/ready")

    assert "hunter2" not in resp.text
    assert "***" in resp.json()["checks"]["leaky_dep"]["detail"]
