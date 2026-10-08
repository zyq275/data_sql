"""认证相关请求/响应模型（FR-AUTH-001~004）。"""

from __future__ import annotations

from pydantic import BaseModel, Field

from app.schemas.user import PASSWORD_FIELD, USERNAME_FIELD


class LoginRequest(BaseModel):
    """登录入参（FR-AUTH-001）。"""

    username: str = USERNAME_FIELD
    password: str = PASSWORD_FIELD


class RefreshRequest(BaseModel):
    """刷新令牌入参（FR-AUTH-002）。

    刷新令牌放请求体而非 Authorization 头：TDD 7.2 标注该端点「鉴权：无」，
    头部的 Bearer 语义留给访问令牌，避免二者混用。
    """

    refresh_token: str = Field(min_length=1, description="登录返回的 refresh_token")


class TokenOut(BaseModel):
    """双令牌响应。"""

    access_token: str
    refresh_token: str
    token_type: str = "bearer"
    expires_in: int = Field(description="access_token 剩余有效秒数")


class ChangePasswordRequest(BaseModel):
    """修改密码入参（FR-AUTH-004）。"""

    old_password: str = PASSWORD_FIELD
    new_password: str = PASSWORD_FIELD
