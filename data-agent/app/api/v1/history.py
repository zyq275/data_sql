"""查询历史接口（TDD 7.2 history 组，FR-HIST-001~003）。

```
GET    /history        历史列表（分页 + 状态/关键词筛选）
GET    /history/{id}   历史详情
DELETE /history/{id}   删除历史
```

整组只作用于本人；``status`` 用 ``Literal`` 声明，取值不合法时由 FastAPI 挡在
进入业务之前（422），而不是让服务层去猜一个未知状态该查什么。
"""

from __future__ import annotations

from typing import Annotated, Literal

from fastapi import APIRouter, Query

from app.api.deps import AuthUser, SessionDep
from app.core.response import ApiResponse, PageData
from app.schemas.history import HistoryDetail, HistoryItem
from app.services.history_service import HistoryService

router = APIRouter(prefix="/history", tags=["查询历史"])

PageQuery = Annotated[int, Query(ge=1, description="页码，从 1 开始")]
PageSizeQuery = Annotated[int, Query(ge=1, le=100, description="每页条数，上限 100")]


@router.get("", response_model=ApiResponse[PageData[HistoryItem]], summary="历史列表")
async def list_history(
    session: SessionDep,
    user: AuthUser,
    page: PageQuery = 1,
    page_size: PageSizeQuery = 20,
    status: Annotated[
        Literal["success", "failed"] | None, Query(description="按状态筛选")
    ] = None,
    keyword: Annotated[
        str | None, Query(max_length=128, description="按问题或 SQL 模糊匹配")
    ] = None,
) -> ApiResponse[PageData[HistoryItem]]:
    """按时间倒序分页列出本人历史（FR-HIST-001）。"""
    data = await HistoryService(session).list(
        user.id, page=page, page_size=page_size, status=status, keyword=keyword
    )
    return ApiResponse(data=data)


@router.get("/{history_id}", response_model=ApiResponse[HistoryDetail], summary="历史详情")
async def get_history(
    history_id: str, session: SessionDep, user: AuthUser
) -> ApiResponse[HistoryDetail]:
    """历史详情：问题 / SQL / 结果预览 / 解释 / 耗时 / 状态（仅本人，非本人 404）。"""
    data = await HistoryService(session).detail(user.id, history_id)
    return ApiResponse(data=data)


@router.delete("/{history_id}", response_model=ApiResponse[None], summary="删除历史")
async def delete_history(
    history_id: str, session: SessionDep, user: AuthUser
) -> ApiResponse[None]:
    """删除一条本人历史（FR-HIST-003）。"""
    await HistoryService(session).delete(user.id, history_id)
    return ApiResponse(data=None)
