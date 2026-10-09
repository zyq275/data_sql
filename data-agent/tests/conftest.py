"""测试夹具，以及「外部依赖是否就绪」的判定。

阶段 4 起的验收依赖一整套外部服务（MySQL / Qdrant / Elasticsearch / Embedding）。
这套服务并非每个环境都有，所以带 ``requires_stack`` 标记的测试在服务没起时
**跳过并说明缺什么**，而不是失败——失败会掩盖真正的问题，跳过才是诚实的状态描述。

但跳过的边界要说清楚：只判断「进程有没有在监听」。服务在监听而口令错、
知识库没建、字段对不上，那是真问题，必须失败，不能被跳过掩盖。

会话级事件循环：app.clients 下的 Qdrant / ES 客户端是按进程持有的单例，
绑在创建它的那个事件循环上。若每个测试各起一个循环，第一个测试创建的客户端
到第二个测试就成了「绑在已关闭循环上」的僵尸。因此这里让夹具与测试共用
一个会话级循环（配置见 pyproject 的 asyncio_default_*_loop_scope）。
"""

from __future__ import annotations

import os
import socket
from urllib.parse import urlparse

import pytest

from app.core.config import Settings

# main.py 在导入时就执行 create_app()，而 create_app 会做启动自检：
# 缺 JWT__SECRET_KEY 直接抛 RuntimeError（NFR-SEC-003）。测试进程不一定有 .env，
# 这里在导入应用之前先兜一个测试专用密钥。
# 用 setdefault 之外的行为是刻意的：环境变量优先级高于 .env，所以测试始终用这个
# 密钥而不是开发者机器上的真实密钥，结果不随个人环境漂移。
os.environ.setdefault("JWT__SECRET_KEY", "test-only-secret-do-not-use-in-production")

# 启动自检从阶段 5 起也要求 LLM__API_KEY。这里**不能**照抄上面的 setdefault：
# 环境变量优先于 .env，无脑设一个占位值会把开发者 .env 里的真实 Key 顶掉，
# 于是阶段 5 的端到端验收会拿着假 Key 去调 DeepSeek，全部报鉴权失败。
# 所以先看 .env 里到底有没有（此刻环境变量还没被污染，读到的就是 .env 的真值），
# 有就一个字节都不动；没有才补占位值，让"不依赖真实 LLM"的那些用例仍能导入应用。
LLM_KEY_PLACEHOLDER = "test-only-key-do-not-use"
_REAL_LLM_KEY = Settings().llm.api_key
HAS_REAL_LLM = bool(_REAL_LLM_KEY and _REAL_LLM_KEY != LLM_KEY_PLACEHOLDER)
if not HAS_REAL_LLM:
    os.environ["LLM__API_KEY"] = LLM_KEY_PLACEHOLDER

TCP_PROBE_TIMEOUT_SECONDS = 1.0


def _tcp_reachable(host: str, port: int) -> bool:
    try:
        with socket.create_connection((host, port), timeout=TCP_PROBE_TIMEOUT_SECONDS):
            return True
    except OSError:
        return False


def missing_dependencies() -> dict[str, str]:
    """返回 {依赖名: 不可用原因}；空字典表示全部就绪。

    **不含重排服务**：它是可选的。没有它，检索会降级到 RRF 融合顺序
    （见 retrievers.retrieval 的说明），整条问答链路照常工作，
    所以它不该出现在"外部依赖未就绪就跳过"的名单里——那会把所有
    requires_stack 的用例一并跳掉，比故障本身更糟。
    需要真实精排模型的用例单独用 requires_rerank 标记（见 rerank_available）。

    **同样不含 Redis**（阶段 7 起）：它也是可选的，没有它会话状态降级到进程内
    （见 agent.checkpoint），问答链路照常工作。需要真实 Redis 的用例单独用
    requires_redis 标记（见 redis_available）。
    """
    from app.core.config import get_settings

    settings = get_settings()
    missing: dict[str, str] = {}

    for label, db in (("MySQL(meta)", settings.db_meta), ("MySQL(dw)", settings.db_dw)):
        if not _tcp_reachable(db.host, db.port):
            missing[label] = f"{db.host}:{db.port} 未监听"

    qdrant_settings = settings.qdrant
    if qdrant_settings.is_local:
        pass  # 嵌入式模式不开端口，没有服务可探
    elif not _tcp_reachable(qdrant_settings.host, qdrant_settings.port):
        missing["Qdrant"] = f"{qdrant_settings.host}:{qdrant_settings.port} 未监听"

    if not _tcp_reachable(settings.es.host, settings.es.port):
        missing["Elasticsearch"] = f"{settings.es.host}:{settings.es.port} 未监听"

    embedding_url = urlparse(settings.embedding.host)
    hostname = embedding_url.hostname or "127.0.0.1"
    port = embedding_url.port or (443 if embedding_url.scheme == "https" else 80)
    if not _tcp_reachable(hostname, port):
        missing["Embedding"] = f"{settings.embedding.host} 未监听"

    return missing


def pytest_collection_modifyitems(config: pytest.Config, items: list[pytest.Item]) -> None:
    """外部依赖没起时，跳过（而非失败）需要真实服务栈的用例。"""
    missing = missing_dependencies()
    if not missing:
        return
    reason = "外部依赖未就绪，跳过：" + "、".join(
        f"{name}（{detail}）" for name, detail in missing.items()
    )
    skip_marker = pytest.mark.skip(reason=reason)
    for item in items:
        if "requires_stack" in item.keywords:
            item.add_marker(skip_marker)


def rerank_available() -> bool:
    """重排服务是否可用（开关打开、配了地址、端口在监听）。"""
    from app.core.config import get_settings

    settings = get_settings().rerank
    if not settings.enabled or not settings.host:
        return False
    url = urlparse(settings.host)
    hostname = url.hostname or "127.0.0.1"
    port = url.port or (443 if url.scheme == "https" else 80)
    return _tcp_reachable(hostname, port)


def redis_available() -> bool:
    """Redis 是否可用（开关打开且端口在监听）。"""
    from app.core.config import get_settings

    settings = get_settings().redis
    if not settings.enabled:
        return False
    return _tcp_reachable(settings.host, settings.port)


def pytest_runtest_setup(item: pytest.Item) -> None:
    """没配真实 LLM Key / Redis 时，各自跳过需要它们的用例。

    与 ``requires_stack`` 分开判定、也分开打标：知识库通了不等于模型能调，
    两个条件各自可能单独不满足，合成一个标记会让"为什么跳过"说不清到底缺哪一项。
    """
    if "requires_llm" in item.keywords and not HAS_REAL_LLM:
        pytest.skip("未配置真实的 LLM__API_KEY（.env），跳过需要调用大模型的用例")
    if "requires_rerank" in item.keywords and not rerank_available():
        pytest.skip(
            "没有可用的 Rerank 服务（RERANK__HOST 未监听或已关闭），"
            "跳过需要真实精排模型的用例——精排机制的用例另在 test_hybrid_retrieval "
            "与 test_stage6_acceptance 里用桩服务覆盖"
        )
    if "requires_redis" in item.keywords and not redis_available():
        pytest.skip(
            "没有可用的 Redis（redis.enabled=false 或 host:port 未监听），"
            "跳过会话持久化相关的用例——降级到进程内存储的路径另在 "
            "test_stage7_acceptance 里覆盖"
        )
