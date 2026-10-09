"""conversation / message 表存取（TDD 8.1）。

只做读写，不含业务规则（TDD 4 分层约束）："会话仅本人可见"这类判断在
``services.conversation_service``。
"""

from __future__ import annotations

from collections.abc import Sequence
from datetime import datetime

from sqlalchemy import Select, delete, func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models import Conversation, Message
from app.repositories.ids import new_id, new_ordered_id

__all__ = ["ConversationRepository", "MessageRepository", "new_id", "new_ordered_id"]


class ConversationRepository:
    """会话存储访问。"""

    def __init__(self, session: AsyncSession) -> None:
        self.session = session

    async def get(self, conversation_id: str) -> Conversation | None:
        return await self.session.get(Conversation, conversation_id)

    async def list_by_user(
        self, user_id: str, *, page: int, page_size: int
    ) -> tuple[Sequence[Conversation], int]:
        """按创建时间倒序列出本人会话（FR-CONV-005）。

        倒序 + 主键做次序稳定器：同一秒创建的两个会话靠 ``created_at`` 分不出先后，
        分页时会在两页之间来回漂——同一条记录既出现在上一页又出现在下一页，
        或者两边都没有。
        """
        condition = Conversation.user_id == user_id
        stmt: Select = (
            select(Conversation)
            .where(condition)
            .order_by(Conversation.created_at.desc(), Conversation.id.desc())
            .offset((page - 1) * page_size)
            .limit(page_size)
        )
        count_stmt = select(func.count()).select_from(Conversation).where(condition)

        total = (await self.session.execute(count_stmt)).scalar_one()
        items = (await self.session.execute(stmt)).scalars().all()
        return items, total

    async def create(self, *, user_id: str, title: str | None) -> Conversation:
        conversation = Conversation(id=new_id(), user_id=user_id, title=title)
        self.session.add(conversation)
        await self.session.flush()
        return conversation

    async def touch(self, conversation: Conversation) -> None:
        """标记会话刚刚有活动，让列表把它排到前面。

        赋的是 Python 侧的 ``datetime.now()`` 而不是 SQL 的 ``now()``：后者会把一个
        函数表达式塞进 ORM 属性，flush 之后对象上留着的仍是个表达式，
        紧接着若还要读 ``updated_at`` 就会拿到一个不是时间的对象。
        """
        conversation.updated_at = datetime.now()
        await self.session.flush()

    async def delete(self, conversation: Conversation) -> None:
        await self.session.delete(conversation)
        await self.session.flush()


class MessageRepository:
    """会话消息存取。"""

    def __init__(self, session: AsyncSession) -> None:
        self.session = session

    async def list_by_conversation(
        self, conversation_id: str, *, limit: int | None = None
    ) -> Sequence[Message]:
        """按发生顺序取某个会话的消息（新的在前，调用方可自行反转）。

        排序用主键而不是 ``created_at``：见 ``new_ordered_id`` 的说明。
        """
        stmt: Select = (
            select(Message)
            .where(Message.conversation_id == conversation_id)
            .order_by(Message.id.desc())
        )
        if limit is not None:
            stmt = stmt.limit(limit)
        return (await self.session.execute(stmt)).scalars().all()

    async def add_turn(
        self,
        *,
        conversation_id: str,
        user_id: str,
        role: str,
        query_text: str | None,
        generated_sql: str | None,
        result_preview: dict | None,
        explain_text: str | None = None,
    ) -> Message:
        message = Message(
            id=new_ordered_id(),
            conversation_id=conversation_id,
            user_id=user_id,
            role=role,
            query_text=query_text,
            generated_sql=generated_sql,
            result_preview=result_preview,
            explain_text=explain_text,
        )
        self.session.add(message)
        await self.session.flush()
        return message

    async def delete_by_conversation(self, conversation_id: str) -> None:
        """删会话时一并清掉它的消息。

        没有外键级联（TDD 8.1 的建表语句里没有 FOREIGN KEY），所以必须显式删——
        否则会话没了、消息还在，它们永远不会被读到，却会一直占着库，
        而且按 user_id 统计会话数时会与对话列表对不上。
        """
        await self.session.execute(
            delete(Message).where(Message.conversation_id == conversation_id)
        )
        await self.session.flush()
