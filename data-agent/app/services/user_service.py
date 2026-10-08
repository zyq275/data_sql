"""用户管理业务编排（FR-ADMIN-001~004）。"""

from __future__ import annotations

from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.exceptions import NotFoundError, PermissionDeniedError, ValidationError
from app.core.response import PageData
from app.core.security import hash_password
from app.models import User
from app.repositories.user import UserRepository
from app.schemas.user import UserCreateRequest, UserOut


class UserService:
    """管理端的用户增改查。"""

    def __init__(self, session: AsyncSession) -> None:
        self.session = session
        self.users = UserRepository(session)

    async def list_users(
        self, *, page: int, page_size: int, keyword: str | None = None
    ) -> PageData[UserOut]:
        """分页列出用户（FR-ADMIN-001）。"""
        items, total = await self.users.list(page=page, page_size=page_size, keyword=keyword)
        return PageData[UserOut](
            items=[UserOut.model_validate(u) for u in items],
            total=total,
            page=page,
            page_size=page_size,
        )

    async def create_user(self, payload: UserCreateRequest) -> UserOut:
        """创建用户（FR-ADMIN-002）。用户名唯一，密码只以 bcrypt 哈希落库。"""
        if await self.users.get_by_username(payload.username) is not None:
            raise ValidationError("用户名已存在")

        try:
            user = await self.users.create(
                username=payload.username,
                password_hash=hash_password(payload.password),
                role=payload.role,
                display_name=payload.display_name,
            )
            await self.session.commit()
        except IntegrityError as exc:
            # 唯一键兜底：并发创建同名用户时先查后插仍可能撞 uk_username
            await self.session.rollback()
            raise ValidationError("用户名已存在") from exc
        return UserOut.model_validate(user)

    async def update_role(self, operator: User, user_id: str, role: str) -> UserOut:
        """修改他人角色（FR-ADMIN-003）；管理员不得修改本人（BR-AUTH-003）→ 403。"""
        target = await self._get_target(user_id)
        if target.id == operator.id:
            raise PermissionDeniedError("管理员不能修改自己的角色")
        await self.users.update_role(target, role)
        await self.session.commit()
        return UserOut.model_validate(target)

    async def update_status(self, operator: User, user_id: str, is_active: bool) -> UserOut:
        """启用/禁用他人（FR-ADMIN-004）；管理员不得禁用本人（BR-AUTH-003）→ 403。"""
        target = await self._get_target(user_id)
        if target.id == operator.id:
            raise PermissionDeniedError("管理员不能修改自己的启用状态")
        await self.users.update_status(target, is_active)
        await self.session.commit()
        return UserOut.model_validate(target)

    async def _get_target(self, user_id: str) -> User:
        user = await self.users.get(user_id)
        if user is None:
            raise NotFoundError("用户不存在")
        return user
