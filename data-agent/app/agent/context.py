"""图运行上下文（TDD 4：依赖经 ``runtime.context`` 注入）。

节点拿到的是**可替换的实现**而不是模块级单例的直调，有两个好处：

1. 测试能把假模型、假检索塞进来，不必 monkeypatch 全局单例；
2. 阶段 7（会话历史）与阶段 9（数据权限）的输入有明确落点，
   加字段不影响任何节点签名。
"""

from __future__ import annotations

from dataclasses import dataclass, field

from app.agent.knowledge import KnowledgeGateway
from app.agent.llm import LLMGateway
from app.agent.permissions import PermissionGateway
from app.repositories.dw import DwRepository
from app.retrievers.retrieval import RetrievalGateway


@dataclass(slots=True)
class QueryContext:
    """一次查询运行期间节点可用的依赖。"""

    user_id: str
    """发起查询的用户（用于日志与落库归属）。"""

    role: str = "viewer"
    """发起查询的角色，数据权限按它算（阶段 9）。

    取的是**接口层已经认证过的那份角色**，不再拿 user_id 回库里查一遍：同一个事实
    在一条请求里读两次，只会多一次往返，还会多一个"两处不一致"的机会。
    默认值 ``viewer`` 只服务于单测与不连库的评测干跑——不设限的默认语义下，
    任何角色都等于不受限（见 ``agent.permissions`` 的模块说明），而真实入口
    一律显式传 ``user.role``。
    """

    llm: LLMGateway = field(default_factory=LLMGateway.default)
    knowledge: KnowledgeGateway = field(default_factory=KnowledgeGateway.default)
    retrieval: RetrievalGateway = field(default_factory=RetrievalGateway.default)
    dw: DwRepository = field(default_factory=DwRepository.default)
    permissions: PermissionGateway = field(default_factory=PermissionGateway.default)
    """角色 → 可见表/字段。实例内按角色缓存，生命周期正好是一轮问答。"""
