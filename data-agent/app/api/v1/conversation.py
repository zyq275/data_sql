"""会话接口（TDD 7.2 conversation 组，FR-CONV-001~005）。

```
POST   /conversations        创建会话
GET    /conversations        会话列表
GET    /conversations/{id}   会话详情（含消息列表）
DELETE /conversations/{id}   删除会话
```

整组都只作用于本人：鉴权后按 ``user_id`` 过滤，他人的会话一律按"不存在"处理
（理由见 ``services.conversation_service``）。
"""

from __future__ import annotations

from typing import Annotated

from fastapi import APIRouter, Query

from app.api.deps import AuthUser, SessionDep
from app.core.response import ApiResponse, PageData
from app.schemas.conversation import (
    ConversationCreateRequest,
    ConversationDetailOut,
    ConversationOut,
)
from app.services.conversation_service import ConversationService

router = APIRouter(prefix="/conversations", tags=["会话"])

PageQuery = Annotated[int, Query(ge=1, description="页码，从 1 开始")]
PageSizeQuery = Annotated[int, Query(ge=1, le=100, description="每页条数，上限 100")]


@router.post("", response_model=ApiResponse[ConversationOut], summary="创建会话")
async def create_conversation(
    payload: ConversationCreateRequest, session: SessionDep, user: AuthUser
) -> ApiResponse[ConversationOut]:
    """创建会话（FR-CONV-001）。返回的 ``id`` 即问答接口要带的 ``session_id``。"""
    data = await ConversationService(session).create(user.id, payload.title)
    return ApiResponse(data=data)


@router.get("", response_model=ApiResponse[PageData[ConversationOut]], summary="会话列表")
async def list_conversations(
    session: SessionDep,
    user: AuthUser,
    page: PageQuery = 1,
    page_size: PageSizeQuery = 20,
) -> ApiResponse[PageData[ConversationOut]]:
    """按创建时间倒序分页列出本人会话（FR-CONV-005）。"""
    data = await ConversationService(session).list(user.id, page=page, page_size=page_size)
    return ApiResponse(data=data)


@router.get(
    "/{conversation_id}",
    response_model=ApiResponse[ConversationDetailOut],
    summary="会话详情",
)
async def get_conversation(
    conversation_id: str, session: SessionDep, user: AuthUser
) -> ApiResponse[ConversationDetailOut]:
    """会话详情，含该会话的消息列表（仅本人；非本人的会话返回 404）。"""
    data = await ConversationService(session).detail(user.id, conversation_id)
    return ApiResponse(data=data)


@router.delete("/{conversation_id}", response_model=ApiResponse[None], summary="删除会话")
async def delete_conversation(
    conversation_id: str, session: SessionDep, user: AuthUser
) -> ApiResponse[None]:
    """删除会话及其全部消息（仅本人）。图状态快照一并清理，清理失败只记日志。"""
    await ConversationService(session).delete(user.id, conversation_id)
    return ApiResponse(data=None)
