"""user 表存取（TDD 8.1 user）。

只做读写，不做规则判断（TDD 4 分层约束）：用户名唯一性、本人不可改等业务规则
在 services.user_service 与 services.auth_service 中。
事务边界由服务层控制，本层只 ``flush`` 让主键/默认值回到对象上。
"""

from __future__ import annotations

import uuid
from collections.abc import Sequence

from sqlalchemy import Select, func, or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models import User


def new_id() -> str:
    """user.id 为 VARCHAR(64)，用 32 位十六进制 UUID，避免自增带来的可枚举。"""
    return uuid.uuid4().hex


class UserRepository:
    """用户存储访问。"""

    def __init__(self, session: AsyncSession) -> None:
        self.session = session

    async def get(self, user_id: str) -> User | None:
        return await self.session.get(User, user_id)

    async def get_by_username(self, username: str) -> User | None:
        result = await self.session.execute(select(User).where(User.username == username))
        return result.scalar_one_or_none()

    async def list(
        self,
        *,
        page: int,
        page_size: int,
        keyword: str | None = None,
    ) -> tuple[Sequence[User], int]:
        """分页查询；``keyword`` 模糊匹配用户名或显示名。"""
        stmt: Select = select(User)
        count_stmt = select(func.count()).select_from(User)

        if keyword:
            pattern = f"%{keyword.strip()}%"
            condition = or_(User.username.like(pattern), User.display_name.like(pattern))
            stmt = stmt.where(condition)
            count_stmt = count_stmt.where(condition)

        stmt = stmt.order_by(User.created_at.desc(), User.id).offset((page - 1) * page_size).limit(page_size)

        total = (await self.session.execute(count_stmt)).scalar_one()
        items = (await self.session.execute(stmt)).scalars().all()
        return items, total

    async def create(
        self,
        *,
        username: str,
        password_hash: str,
        role: str,
        display_name: str | None,
        is_active: bool = True,
    ) -> User:
        user = User(
            id=new_id(),
            username=username,
            password_hash=password_hash,
            role=role,
            display_name=display_name,
            is_active=is_active,
        )
        self.session.add(user)
        await self.session.flush()
        return user

    async def update_role(self, user: User, role: str) -> User:
        user.role = role
        await self.session.flush()
        return user

    async def update_status(self, user: User, is_active: bool) -> User:
        user.is_active = is_active
        await self.session.flush()
        return user

    async def update_password(self, user: User, password_hash: str) -> User:
        user.password_hash = password_hash
        await self.session.flush()
        return user
