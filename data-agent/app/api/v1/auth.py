"""认证接口（TDD 7.2 auth 组）。

POST /auth/login     登录，无鉴权
POST /auth/refresh   刷新令牌，无鉴权（令牌走请求体）
GET  /auth/me        当前用户，Bearer
PUT  /auth/password  修改密码，Bearer
"""

from __future__ import annotations

from fastapi import APIRouter

from app.api.deps import AuthUser, SessionDep
from app.core.response import ApiResponse
from app.schemas.auth import ChangePasswordRequest, LoginRequest, RefreshRequest, TokenOut
from app.schemas.user import UserOut
from app.services.auth_service import AuthService

router = APIRouter(prefix="/auth", tags=["认证"])


@router.post("/login", response_model=ApiResponse[TokenOut], summary="登录")
async def login(payload: LoginRequest, session: SessionDep) -> ApiResponse[TokenOut]:
    """用户名 + 密码换取双令牌（FR-AUTH-001）。密码错 401，账号禁用 403。"""
    tokens = await AuthService(session).login(payload.username, payload.password)
    return ApiResponse(data=tokens)


@router.post("/refresh", response_model=ApiResponse[TokenOut], summary="刷新令牌")
async def refresh(payload: RefreshRequest, session: SessionDep) -> ApiResponse[TokenOut]:
    """用刷新令牌换新令牌（FR-AUTH-002）。非 refresh 类型、无效令牌或用户禁用均返回 401。"""
    tokens = await AuthService(session).refresh(payload.refresh_token)
    return ApiResponse(data=tokens)


@router.get("/me", response_model=ApiResponse[UserOut], summary="当前用户")
async def me(user: AuthUser) -> ApiResponse[UserOut]:
    """返回 id/username/role/display_name/is_active（FR-AUTH-003）。"""
    return ApiResponse(data=UserOut.model_validate(user))


@router.put("/password", response_model=ApiResponse[None], summary="修改密码")
async def change_password(
    payload: ChangePasswordRequest, user: AuthUser, session: SessionDep
) -> ApiResponse[None]:
    """修改本人密码（FR-AUTH-004）。原密码错误返回 400，成功后旧密码失效。"""
    await AuthService(session).change_password(user, payload.old_password, payload.new_password)
    return ApiResponse(data=None)
