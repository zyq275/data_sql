"""permission 表存取（TDD 8.1 permission）。

只做读写，不做规则判断（TDD 4 分层约束）：角色名合不合法、resource_id 指向的表
/字段存不存在，都在 ``services.permission_service``。事务边界由服务层控制，
本层只 ``flush``。

**为什么不做 upsert**：``(role, resource_type, resource_id)`` 的重复与否是一条业务
规则（FR-PERM-002 的"增"应当是幂等的还是应当报错），而这里只提供原语。
服务层选择"重复即报错"，于是它需要一个能精确判重的查询——就是下面的 ``find``。
"""

from __future__ import annotations

from collections.abc import Sequence

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models import Permission


class PermissionRepository:
    """权限映射存储访问。"""

    def __init__(self, session: AsyncSession) -> None:
        self.session = session

    async def get(self, permission_id: str) -> Permission | None:
        return await self.session.get(Permission, permission_id)

    async def find(
        self, *, role: str, resource_type: str, resource_id: str
    ) -> Permission | None:
        """按三元组查一条映射（用于判重）。"""
        result = await self.session.execute(
            select(Permission).where(
                Permission.role == role,
                Permission.resource_type == resource_type,
                Permission.resource_id == resource_id,
            )
        )
        return result.scalars().first()

    async def list(
        self, *, role: str | None = None, page: int, page_size: int
    ) -> tuple[Sequence[Permission], int]:
        """分页列出映射；``role`` 为空时列出全部角色。"""
        condition = (Permission.role == role) if role else None

        stmt = select(Permission)
        count_stmt = select(func.count()).select_from(Permission)
        if condition is not None:
            stmt = stmt.where(condition)
            count_stmt = count_stmt.where(condition)

        # 先按角色后按资源：管理界面总是"某个角色配了哪些东西"这样看，
        # 而 id 是 uuid、按它排等于随机。加 id 做次级键只为让分页稳定。
        stmt = (
            stmt.order_by(Permission.role, Permission.resource_type, Permission.resource_id)
            .offset((page - 1) * page_size)
            .limit(page_size)
        )
        total = (await self.session.execute(count_stmt)).scalar_one()
        items = (await self.session.execute(stmt)).scalars().all()
        return items, total

    async def list_by_role(self, role: str) -> Sequence[Permission]:
        """某角色的全部映射（不分页）。"""
        result = await self.session.execute(
            select(Permission).where(Permission.role == role)
        )
        return result.scalars().all()

    async def create(
        self,
        *,
        permission_id: str,
        role: str,
        resource_type: str,
        resource_id: str,
    ) -> Permission:
        permission = Permission(
            id=permission_id,
            role=role,
            resource_type=resource_type,
            resource_id=resource_id,
        )
        self.session.add(permission)
        await self.session.flush()
        return permission

    async def update(
        self, permission: Permission, *, role: str, resource_type: str, resource_id: str
    ) -> Permission:
        permission.role = role
        permission.resource_type = resource_type
        permission.resource_id = resource_id
        await self.session.flush()
        return permission

    async def delete(self, permission: Permission) -> None:
        await self.session.delete(permission)
        await self.session.flush()
