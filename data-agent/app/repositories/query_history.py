"""query_history 表存取（TDD 8.1）。

与 ``repositories.conversation`` 分开两个模块，是因为它们的读法不同：会话消息按
"某会话内的顺序"读，查询历史按"某用户的倒序分页 + 状态/关键词筛选"读。
合成一个仓储会得到一个既有会话维度又有用户维度的类，而两条查询路径其实毫无交集。
"""

from __future__ import annotations

from collections.abc import Sequence

from sqlalchemy import Select, func, or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models import QueryHistory
from app.repositories.ids import new_ordered_id

#: 历史列表的两种终态，与落库时写进 status 的值一一对应。
STATUS_SUCCESS = "success"
STATUS_FAILED = "failed"

MAX_KEYWORD_CHARS = 128
"""关键词筛选的长度上限。与 admin_users 的关键词参数同值——

超长关键词进不了索引、只能全表扫，而且 ``LIKE '%...%'`` 本身就用不上索引，
再叠加一个超长模式串只是白白让每行都做一次长文本匹配。
"""


class QueryHistoryRepository:
    """查询历史存储访问。"""

    def __init__(self, session: AsyncSession) -> None:
        self.session = session

    async def get(self, history_id: str) -> QueryHistory | None:
        return await self.session.get(QueryHistory, history_id)

    async def list_by_user(
        self,
        user_id: str,
        *,
        page: int,
        page_size: int,
        status: str | None = None,
        keyword: str | None = None,
    ) -> tuple[Sequence[QueryHistory], int]:
        """按时间倒序分页列出本人历史（FR-HIST-001）。

        关键词同时匹配问题原文与生成的 SQL：用户找回一次查询时，记得的可能是
        "我当时问了什么"，也可能是"那条 SQL 里有个 fact_order"，
        只让其中一种能搜到，另一种就只能靠翻页。
        """
        conditions = [QueryHistory.user_id == user_id]
        if status:
            conditions.append(QueryHistory.status == status)
        if keyword:
            pattern = f"%{keyword.strip()}%"
            conditions.append(
                or_(
                    QueryHistory.query_text.like(pattern),
                    QueryHistory.generated_sql.like(pattern),
                )
            )

        stmt: Select = (
            select(QueryHistory)
            .where(*conditions)
            # 主键是时间有序的（见 repositories.ids.new_ordered_id），拿它当次序稳定器，
            # 同一秒内的多条记录在翻页时不会来回漂
            .order_by(QueryHistory.created_at.desc(), QueryHistory.id.desc())
            .offset((page - 1) * page_size)
            .limit(page_size)
        )
        count_stmt = select(func.count()).select_from(QueryHistory).where(*conditions)

        total = (await self.session.execute(count_stmt)).scalar_one()
        items = (await self.session.execute(stmt)).scalars().all()
        return items, total

    async def create(
        self,
        *,
        user_id: str,
        session_id: str | None,
        query_text: str | None,
        generated_sql: str | None,
        result_preview: dict | None,
        explain_text: str | None,
        status: str,
        duration_ms: int | None,
    ) -> QueryHistory:
        """新增一条历史。主键在内部生成（时间有序，见 ``repositories.ids``）。"""
        record = QueryHistory(
            id=new_ordered_id(),
            user_id=user_id,
            session_id=session_id,
            query_text=query_text,
            generated_sql=generated_sql,
            result_preview=result_preview,
            explain_text=explain_text,
            status=status,
            duration_ms=duration_ms,
        )
        self.session.add(record)
        await self.session.flush()
        return record

    async def delete(self, record: QueryHistory) -> None:
        await self.session.delete(record)
        await self.session.flush()
