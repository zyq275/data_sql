"""认证业务编排（FR-AUTH-001/002/004）。"""

from __future__ import annotations

from sqlalchemy.ext.asyncio import AsyncSession

from app.core.exceptions import AuthError, PermissionDeniedError, ValidationError
from app.core.security import (
    TOKEN_TYPE_ACCESS,
    TOKEN_TYPE_REFRESH,
    burn_password_check,
    create_token,
    decode_token,
    hash_password,
    verify_password,
)
from app.models import User
from app.repositories.user import UserRepository
from app.schemas.auth import TokenOut


class AuthService:
    """登录、刷新令牌、修改密码。"""

    def __init__(self, session: AsyncSession) -> None:
        self.session = session
        self.users = UserRepository(session)

    async def login(self, username: str, password: str) -> TokenOut:
        """凭据校验并签发双令牌。

        用户不存在与口令错误返回同一个错误，避免通过错误码枚举账号；
        用户不存在时也走一次等价的哈希比对，抹平响应时间差。
        """
        user = await self.users.get_by_username(username)
        if user is None:
            burn_password_check(password)
            raise AuthError("用户名或密码错误")
        if not verify_password(password, user.password_hash):
            raise AuthError("用户名或密码错误")
        # 先验密码再看启用状态：避免未通过认证的调用方探测账号是否被禁用
        if not user.is_active:
            raise PermissionDeniedError("账号已被禁用")
        return self._issue_tokens(user)

    async def refresh(self, refresh_token: str) -> TokenOut:
        """用刷新令牌换一组新令牌（FR-AUTH-002）。

        只接受 type=refresh 的令牌；令牌无效或用户已禁用返回 401。
        """
        payload = decode_token(refresh_token, TOKEN_TYPE_REFRESH)
        user = await self.users.get(payload["sub"])
        if user is None or not user.is_active:
            raise AuthError("令牌无效或账号已被禁用")
        return self._issue_tokens(user)

    async def change_password(self, user: User, old_password: str, new_password: str) -> None:
        """修改本人密码；原密码错误返回 400（FR-AUTH-004）。

        更换哈希后旧密码即无法再登录。已签发的访问令牌在有效期内仍可用——
        JWT 无状态，主动失效需要黑名单（Redis），见阶段 6 与 FR-AUTH-006 的登出。
        """
        if not verify_password(old_password, user.password_hash):
            raise ValidationError("原密码错误")
        await self.users.update_password(user, hash_password(new_password))
        await self.session.commit()

    @staticmethod
    def _issue_tokens(user: User) -> TokenOut:
        access_token, expires_in = create_token(user.id, user.role, TOKEN_TYPE_ACCESS)
        refresh_token, _ = create_token(user.id, user.role, TOKEN_TYPE_REFRESH)
        return TokenOut(
            access_token=access_token,
            refresh_token=refresh_token,
            expires_in=expires_in,
        )
