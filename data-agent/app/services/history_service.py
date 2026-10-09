"""查询历史的业务编排（FR-HIST-001~003）。

**这里只负责"读与删"**。写入（一轮问答结束后落 query_history）在
``QueryService`` 里与本轮的消息一起提交——两行记录出自同一份数据、同一个时刻，
分开写只会得到"消息落了、历史没落"的半份记录：会话详情看得到这一轮，
历史列表里却凭空少一条，而两边都显示正常，没人会去对账。
"""

from __future__ import annotations

from sqlalchemy.ext.asyncio import AsyncSession

from app.core.exceptions import NotFoundError
from app.core.response import PageData
from app.models import QueryHistory
from app.repositories.query_history import QueryHistoryRepository
from app.schemas.history import HistoryDetail, HistoryItem


class HistoryService:
    """查询历史的列表、详情与删除，全部限定在本人范围内。"""

    def __init__(self, session: AsyncSession) -> None:
        self.session = session
        self.history = QueryHistoryRepository(session)

    async def list(
        self,
        user_id: str,
        *,
        page: int,
        page_size: int,
        status: str | None = None,
        keyword: str | None = None,
    ) -> PageData[HistoryItem]:
        """分页列出本人历史，可按状态与关键词筛选（FR-HIST-001）。"""
        items, total = await self.history.list_by_user(
            user_id, page=page, page_size=page_size, status=status, keyword=keyword
        )
        return PageData[HistoryItem](
            items=[HistoryItem.model_validate(item) for item in items],
            total=total,
            page=page,
            page_size=page_size,
        )

    async def detail(self, user_id: str, history_id: str) -> HistoryDetail:
        """历史详情：问题 / SQL / 结果预览 / 解释 / 耗时 / 状态（FR-HIST-002）。"""
        return HistoryDetail.model_validate(await self._owned(user_id, history_id))

    async def delete(self, user_id: str, history_id: str) -> None:
        """删除一条本人历史（FR-HIST-003）。"""
        record = await self._owned(user_id, history_id)
        await self.history.delete(record)
        await self.session.commit()

    async def _owned(self, user_id: str, history_id: str) -> QueryHistory:
        """取记录并确认归属，不属于本人时按"不存在"处理。

        与 ``ConversationService.owned`` 同一口径，理由见那里（越权不返回 403，
        是为了不让"存在与否"变成一个可以问出来的信息）。
        """
        record = await self.history.get(history_id)
        if record is None or record.user_id != user_id:
            raise NotFoundError("查询历史不存在")
        return record
