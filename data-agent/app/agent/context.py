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
from app.repositories.dw import DwRepository
from app.retrievers.retrieval import RetrievalGateway


@dataclass(slots=True)
class QueryContext:
    """一次查询运行期间节点可用的依赖。"""

    user_id: str
    """发起查询的用户（阶段 9 据此注入数据权限范围；阶段 5 仅用于日志）。"""

    llm: LLMGateway = field(default_factory=LLMGateway.default)
    knowledge: KnowledgeGateway = field(default_factory=KnowledgeGateway.default)
    retrieval: RetrievalGateway = field(default_factory=RetrievalGateway.default)
    dw: DwRepository = field(default_factory=DwRepository.default)
