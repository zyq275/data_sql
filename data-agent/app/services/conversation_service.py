"""会话与消息的业务编排（FR-CONV-001~005，TDD 5.3）。

**归属校验只有一处实现**：``owned()`` 同时给本模块的详情/删除与
``QueryService`` 的多轮问答用。这条规则若在两处各写一遍，将来改一处忘一处，
症状是"某一类入口能读到别人的会话"——而读接口和问数接口由不同的人看，
很难被联系起来。
"""

from __future__ import annotations

import logging

from sqlalchemy.ext.asyncio import AsyncSession

from app.agent.checkpoint import release_thread
from app.core.exceptions import NotFoundError
from app.core.response import PageData
from app.models import Conversation
from app.repositories.conversation import ConversationRepository, MessageRepository
from app.schemas.conversation import (
    MAX_TITLE_CHARS,
    ConversationDetailOut,
    ConversationOut,
    MessageOut,
)

logger = logging.getLogger(__name__)


class ConversationService:
    """会话的增删查，全部限定在本人范围内。"""

    def __init__(self, session: AsyncSession) -> None:
        self.session = session
        self.conversations = ConversationRepository(session)
        self.messages = MessageRepository(session)

    async def create(self, user_id: str, title: str | None = None) -> ConversationOut:
        """创建会话（FR-CONV-001）。返回的 id 即 ``session_id``。

        标题允许留空：前端通常是"用户先开口、再由首轮问题回填标题"，
        强制先填标题等于让他在还不知道要问什么的时候先给这次对话起个名字。
        回填由前端调删除/列表之外的接口完成则不必——标题只是列表里的一行字，
        空着显示"未命名会话"即可，不必为它加一个 PATCH 端点。
        """
        conversation = await self.conversations.create(
            user_id=user_id, title=self._clean_title(title)
        )
        await self.session.commit()
        return ConversationOut.model_validate(conversation)

    async def list(
        self, user_id: str, *, page: int, page_size: int
    ) -> PageData[ConversationOut]:
        """按时间倒序列出本人会话（FR-CONV-005）。"""
        items, total = await self.conversations.list_by_user(
            user_id, page=page, page_size=page_size
        )
        return PageData[ConversationOut](
            items=[ConversationOut.model_validate(item) for item in items],
            total=total,
            page=page,
            page_size=page_size,
        )

    async def detail(self, user_id: str, conversation_id: str) -> ConversationDetailOut:
        """会话详情，含该会话的消息列表（TDD 7.2，仅本人）。

        消息按发生顺序返回（早的在前）。仓储按倒序取，这里翻回来：
        "取哪几条"是存储层的取舍，"按什么顺序看"是接口的契约。
        """
        conversation = await self.owned(user_id, conversation_id)
        messages = await self.messages.list_by_conversation(conversation_id)
        return ConversationDetailOut(
            id=conversation.id,
            title=conversation.title,
            created_at=conversation.created_at,
            updated_at=conversation.updated_at,
            messages=[MessageOut.model_validate(m) for m in reversed(list(messages))],
        )

    async def delete(self, user_id: str, conversation_id: str) -> None:
        """删除会话及其消息（FR-CONV-005，仅本人）。

        消息显式删除：TDD 8.1 的建表语句里没有外键，删除不会级联。
        """
        conversation = await self.owned(user_id, conversation_id)
        await self.messages.delete_by_conversation(conversation_id)
        await self.conversations.delete(conversation)
        # 先落 MySQL 再清 Redis：库里这两张表才是用户认知中的"这个会话"，
        # 而下面的清理失败只告警（见 agent.checkpoint.release_thread）
        await self.session.commit()
        await release_thread(conversation_id)
        logger.info("会话 %s 已删除（用户 %s）", conversation_id, user_id)

    async def owned(self, user_id: str, conversation_id: str) -> Conversation:
        """取会话并确认归属，不满足时按"不存在"处理。

        **为什么是不存在而不是无权**：403 等于承认"这个 id 确实存在，只是不归你"，
        这就成了一个探测他人会话是否存在的口径。会话 id 是 uuid4、本来猜不出来，
        但"该用什么口径回答越权访问"这件事，不该建立在"id 恰好猜不到"之上——
        换了 id 生成方式（比如将来改成时间有序的），这个依赖就悄悄断了。
        """
        conversation = await self.conversations.get(conversation_id)
        if conversation is None or conversation.user_id != user_id:
            raise NotFoundError("会话不存在")
        return conversation

    @staticmethod
    def _clean_title(title: str | None) -> str | None:
        """标题去空白；全是空白视同未填。"""
        if title is None:
            return None
        cleaned = title.strip()
        return cleaned[:MAX_TITLE_CHARS] or None
