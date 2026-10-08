"""JWT 与密码哈希、角色依赖（TDD 4 core.security）。

- 密码：bcrypt 直连（``hashpw`` / ``checkpw``），库存标准 ``$2b$`` 哈希（BR-AUTH-001）。
  不经 passlib：其最新版仍为 2020 年的 1.7.4，后端自检在 bcrypt 5.x 下会直接抛错，
  详见 TDD 13 安全设计一节的说明。
- 令牌：PyJWT HS256，访问/刷新分离，靠 ``type`` 声明校验用途（NFR-SEC-001）。
- 角色：viewer < analyst < admin（BR-AUTH-002），越权抛 403、未认证抛 401（FR-AUTH-005）。
"""

from __future__ import annotations

import uuid
from datetime import datetime, timedelta, timezone
from typing import Annotated, Any, Literal

import bcrypt
import jwt
from fastapi import Depends
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer
from sqlalchemy.ext.asyncio import AsyncSession

from app.clients.mysql import get_session
from app.core.config import get_settings
from app.core.exceptions import (
    AuthError,
    InvalidTokenError,
    PermissionDeniedError,
    ServiceError,
    TokenExpiredError,
    ValidationError,
)
from app.models import User

TokenType = Literal["access", "refresh"]

TOKEN_TYPE_ACCESS: TokenType = "access"
TOKEN_TYPE_REFRESH: TokenType = "refresh"

# bcrypt 只取前 72 字节；超长输入在新版会直接抛 ValueError，故在入口显式拦截。
BCRYPT_MAX_BYTES = 72

ROLE_RANK: dict[str, int] = {"viewer": 0, "analyst": 1, "admin": 2}
ROLES: tuple[str, ...] = ("viewer", "analyst", "admin")

# auto_error=False：自行抛 AuthError，保证 401 也走统一响应结构
_bearer = HTTPBearer(auto_error=False, description="Bearer <access_token>")

# 用户不存在时用来「陪跑」一次哈希比对，抹平响应时间差，避免账号枚举
_dummy_hash: bytes | None = None


# ---- 密码 ----


def hash_password(raw_password: str) -> str:
    """生成 bcrypt 哈希（每次调用使用新的随机 salt）。"""
    data = raw_password.encode("utf-8")
    if len(data) > BCRYPT_MAX_BYTES:
        # 显式报错而非静默截断：截断会让「前 72 字节相同的两个口令」互相可登录
        raise ValidationError(f"密码过长（最多 {BCRYPT_MAX_BYTES} 字节）")
    return bcrypt.hashpw(data, bcrypt.gensalt()).decode("utf-8")


def verify_password(raw_password: str, password_hash: str) -> bool:
    """校验明文与哈希是否匹配；哈希损坏时返回 False 而不是抛异常。"""
    try:
        return bcrypt.checkpw(raw_password.encode("utf-8"), password_hash.encode("utf-8"))
    except (ValueError, TypeError):
        return False


def burn_password_check(raw_password: str) -> None:
    """对不存在的用户执行一次等价的哈希比对，抵消「用户名是否存在」的时间差。"""
    global _dummy_hash
    if _dummy_hash is None:
        _dummy_hash = bcrypt.hashpw(b"dummy-password", bcrypt.gensalt())
    verify_password(raw_password, _dummy_hash.decode("utf-8"))


# ---- 令牌 ----


def _secret_key() -> str:
    secret = get_settings().jwt.secret_key
    if not secret:
        raise ServiceError("JWT 密钥未配置，请设置 JWT__SECRET_KEY")
    return secret


def create_token(subject: str, role: str, token_type: TokenType) -> tuple[str, int]:
    """签发令牌，返回 ``(token, 有效期秒数)``。"""
    settings = get_settings().jwt
    now = datetime.now(timezone.utc)
    expires_in = (
        settings.access_token_expire_minutes * 60
        if token_type == TOKEN_TYPE_ACCESS
        else settings.refresh_token_expire_days * 24 * 3600
    )
    payload: dict[str, Any] = {
        "sub": subject,
        "role": role,
        "type": token_type,
        "iat": now,
        "exp": now + timedelta(seconds=expires_in),
        "jti": uuid.uuid4().hex,
    }
    token = jwt.encode(payload, _secret_key(), algorithm=settings.algorithm)
    return token, expires_in


def decode_token(token: str, expected_type: TokenType) -> dict[str, Any]:
    """校验签名、有效期与用途，返回载荷；失败抛 401 系异常。"""
    settings = get_settings().jwt
    try:
        payload = jwt.decode(
            token,
            _secret_key(),
            algorithms=[settings.algorithm],
            options={"require": ["exp", "sub", "type"]},
        )
    except jwt.ExpiredSignatureError as exc:
        raise TokenExpiredError() from exc
    except jwt.PyJWTError as exc:
        raise InvalidTokenError() from exc

    if payload.get("type") != expected_type:
        # 用刷新令牌换访问令牌不行，反之亦然（NFR-SEC-001「令牌滥用」防护）
        raise InvalidTokenError(f"令牌用途不符，需要 {expected_type}")
    return payload


# ---- 依赖 ----


async def get_current_user(
    credentials: Annotated[HTTPAuthorizationCredentials | None, Depends(_bearer)],
    session: Annotated[AsyncSession, Depends(get_session)],
) -> User:
    """从 Bearer 令牌解析当前用户；未认证或账号禁用一律 401。"""
    if credentials is None or not credentials.credentials:
        raise AuthError("缺少访问令牌")

    payload = decode_token(credentials.credentials, TOKEN_TYPE_ACCESS)

    user = await session.get(User, payload["sub"])
    if user is None:
        raise InvalidTokenError("令牌对应的用户不存在")
    if not user.is_active:
        # 禁用后既有令牌立即失效（FR-AUTH-002）
        raise AuthError("账号已被禁用")
    return user


CurrentUser = Annotated[User, Depends(get_current_user)]


def require_min_role(min_role: str) -> Any:
    """要求角色不低于 ``min_role``（按 ROLE_RANK 比较，BR-AUTH-002）。"""
    if min_role not in ROLE_RANK:
        raise ValueError(f"未知角色：{min_role}")

    async def _dependency(user: CurrentUser) -> User:
        if ROLE_RANK.get(user.role, -1) < ROLE_RANK[min_role]:
            raise PermissionDeniedError(f"需要 {min_role} 及以上角色")
        return user

    return _dependency


def require_roles(*roles: str) -> Any:
    """要求角色在给定集合内（不做层级推导）。"""
    allowed = set(roles)

    async def _dependency(user: CurrentUser) -> User:
        if user.role not in allowed:
            raise PermissionDeniedError("当前角色无权访问该资源")
        return user

    return _dependency


# 管理端接口统一依赖（TDD 7.2 标注为 admin）
require_admin = require_min_role("admin")
