"""用户管理接口（TDD 7.2 admin 组，FR-ADMIN-001~004）。

整组要求 admin 角色；角色不足由依赖直接抛 403。
"""

from __future__ import annotations

from typing import Annotated

from fastapi import APIRouter, Query

from app.api.deps import AdminUser, SessionDep
from app.core.response import ApiResponse, PageData
from app.schemas.user import (
    UserCreateRequest,
    UserOut,
    UserRoleUpdateRequest,
    UserStatusUpdateRequest,
)
from app.services.user_service import UserService

router = APIRouter(prefix="/admin/users", tags=["用户管理"])

PageQuery = Annotated[int, Query(ge=1, description="页码，从 1 开始")]
PageSizeQuery = Annotated[int, Query(ge=1, le=100, description="每页条数，上限 100")]


@router.get("", response_model=ApiResponse[PageData[UserOut]], summary="用户列表")
async def list_users(
    session: SessionDep,
    _admin: AdminUser,
    page: PageQuery = 1,
    page_size: PageSizeQuery = 20,
    keyword: Annotated[str | None, Query(max_length=128, description="按用户名/显示名模糊匹配")] = None,
) -> ApiResponse[PageData[UserOut]]:
    """分页列出用户（FR-ADMIN-001），按创建时间倒序。"""
    data = await UserService(session).list_users(page=page, page_size=page_size, keyword=keyword)
    return ApiResponse(data=data)


@router.post("", response_model=ApiResponse[UserOut], summary="创建用户")
async def create_user(
    payload: UserCreateRequest, session: SessionDep, _admin: AdminUser
) -> ApiResponse[UserOut]:
    """创建用户（FR-ADMIN-002）。用户名重复返回 400，密码以 bcrypt 哈希落库。"""
    data = await UserService(session).create_user(payload)
    return ApiResponse(data=data)


@router.put("/{user_id}/role", response_model=ApiResponse[UserOut], summary="修改角色")
async def update_role(
    user_id: str,
    payload: UserRoleUpdateRequest,
    session: SessionDep,
    admin: AdminUser,
) -> ApiResponse[UserOut]:
    """修改指定用户角色（FR-ADMIN-003）。目标不存在 404，改本人 400。"""
    data = await UserService(session).update_role(admin, user_id, payload.role)
    return ApiResponse(data=data)


@router.put("/{user_id}/status", response_model=ApiResponse[UserOut], summary="启用/禁用")
async def update_status(
    user_id: str,
    payload: UserStatusUpdateRequest,
    session: SessionDep,
    admin: AdminUser,
) -> ApiResponse[UserOut]:
    """启用或禁用指定用户（FR-ADMIN-004）。禁用后其既有令牌立即失效。禁本人 400。"""
    data = await UserService(session).update_status(admin, user_id, payload.is_active)
    return ApiResponse(data=data)
