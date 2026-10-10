"""数据权限配置的业务编排（FR-PERM-002）。

三条规则写在这一层（TDD 4：规则不进仓储）：

1. **角色必须是已知角色**——由 ``schemas.permission`` 的 ``Role`` 字面量挡住，
   这一层不再重复判断。
2. **resource_id 必须指向真实存在的表或字段**（PRD 9.3 数据一致性）。否则会配出
   一条永远不生效的权限：用户以为自己关了某张表，实际上什么都没发生，
   而这类"配了但不起作用"的问题在界面上看不出来。
3. **同一个三元组只有一条**。DDL 里没有唯一约束（阶段 2 定稿），所以判重在服务层做；
   重复创建返回 400 而不是静默成功——静默成功会让"我配了两次"和"我配了一次"
   看起来一样，而管理界面上的勾选状态是从这份数据反推的。

**这一层也是"配了什么"与"算出什么"的对账处**：``scope()`` 把网关算出来的
实际生效范围原样返回，让管理界面能同时看到两者。
"""

from __future__ import annotations

from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from app.agent.permissions import TABLE, PermissionGateway
from app.core.exceptions import NotFoundError, ValidationError
from app.core.response import PageData
from app.models import Permission
from app.repositories.ids import new_id
from app.repositories.meta import MetaRepository
from app.repositories.permission import PermissionRepository
from app.schemas.permission import (
    PermissionCreateRequest,
    PermissionOut,
    PermissionUpdateRequest,
    ResourceCatalogResponse,
    ResourceOut,
    RoleScopeOut,
)


class PermissionService:
    """权限映射的增删改查 + 生效范围查询。"""

    def __init__(self, session: AsyncSession) -> None:
        self.session = session
        self.permissions = PermissionRepository(session)
        self.meta = MetaRepository(session)

    async def list_mappings(
        self, *, role: str | None, page: int, page_size: int
    ) -> PageData[PermissionOut]:
        """分页列出映射（FR-PERM-002）。"""
        items, total = await self.permissions.list(role=role, page=page, page_size=page_size)
        names = await self._name_maps()
        return PageData[PermissionOut](
            items=[self._to_out(item, names) for item in items],
            total=total,
            page=page,
            page_size=page_size,
        )

    async def create(self, payload: PermissionCreateRequest) -> PermissionOut:
        """新增一条映射。"""
        await self._assert_resource_exists(payload.resource_type, payload.resource_id)
        if await self._duplicate(payload.role, payload.resource_type, payload.resource_id):
            raise ValidationError("该角色已有这条权限配置")

        try:
            permission = await self.permissions.create(
                permission_id=new_id(),
                role=payload.role,
                resource_type=payload.resource_type,
                resource_id=payload.resource_id,
            )
            await self.session.commit()
        except IntegrityError as exc:  # pragma: no cover —— 判重之后仍撞主键只可能是并发
            await self.session.rollback()
            raise ValidationError("该角色已有这条权限配置") from exc

        # 提交后再取名字映射：``_name_maps`` 另起一次查询，未提交时看不到本行之外
        # 的一致性；而这行本身已经在对象上，不依赖查询。
        return self._to_out(permission, await self._name_maps())

    async def update(self, permission_id: str, payload: PermissionUpdateRequest) -> PermissionOut:
        """整条替换一条映射。"""
        permission = await self._get(permission_id)
        await self._assert_resource_exists(payload.resource_type, payload.resource_id)
        if await self._duplicate(
            payload.role, payload.resource_type, payload.resource_id, exclude=permission_id
        ):
            raise ValidationError("该角色已有这条权限配置")

        await self.permissions.update(
            permission,
            role=payload.role,
            resource_type=payload.resource_type,
            resource_id=payload.resource_id,
        )
        await self.session.commit()
        return self._to_out(permission, await self._name_maps())

    async def delete(self, permission_id: str) -> None:
        """删除一条映射。

        删掉某角色的**最后一条**映射会把它退回"不设限"（见 agent.permissions 的
        模块说明）。这在界面上可能反直觉，所以不在这里拦：语义只有一个，
        想"清空某角色的全部权限"就该配一条这个角色至少要有的表，
        而不是把配置删光——删光等于没配过。
        """
        permission = await self._get(permission_id)
        await self.permissions.delete(permission)
        await self.session.commit()

    async def catalog(self) -> ResourceCatalogResponse:
        """可授权的表与字段全集（管理界面用来渲染勾选项）。"""
        tables = await self.meta.tables()
        columns = await self.meta.columns()
        table_names = {row.id: row.table_name for row in tables}
        table_aliases = {row.id: row.table_alias for row in tables}

        return ResourceCatalogResponse(
            tables=[
                ResourceOut(
                    id=row.id,
                    resource_type=TABLE,
                    name=row.table_name,
                    table_name=row.table_name,
                    table_alias=row.table_alias,
                    description=row.description,
                )
                for row in tables
            ],
            columns=[
                ResourceOut(
                    id=row.id,
                    resource_type="column",
                    name=row.column_name,
                    table_name=table_names.get(row.table_id, ""),
                    table_alias=table_aliases.get(row.table_id),
                    column_alias=row.column_alias,
                    description=row.description,
                )
                for row in columns
            ],
        )

    async def scope(self, role: str) -> RoleScopeOut:
        """某角色**实际生效**的可见范围（复用问答链路上的同一个网关）。"""
        scope = await PermissionGateway.default().scope_for(role)
        return RoleScopeOut(
            role=scope.role,
            unrestricted=scope.unrestricted,
            tables={
                name: None if allowed is None else sorted(allowed)
                for name, allowed in sorted(scope.tables.items())
            },
            metrics=sorted(scope.metrics),
            forbidden_tables=sorted(scope.forbidden_tables),
            forbidden_columns={
                name: sorted(columns) for name, columns in sorted(scope.forbidden_columns.items())
            },
        )

    async def _get(self, permission_id: str) -> Permission:
        permission = await self.permissions.get(permission_id)
        if permission is None:
            raise NotFoundError("权限配置不存在")
        return permission

    async def _duplicate(
        self, role: str, resource_type: str, resource_id: str, *, exclude: str | None = None
    ) -> bool:
        found = await self.permissions.find(
            role=role, resource_type=resource_type, resource_id=resource_id
        )
        return found is not None and found.id != exclude

    async def _assert_resource_exists(self, resource_type: str, resource_id: str) -> None:
        """资源必须真实存在，否则这条权限永远不会生效。"""
        if resource_type == TABLE:
            exists = any(row.id == resource_id for row in await self.meta.tables())
        else:
            exists = any(row.id == resource_id for row in await self.meta.columns())
        if not exists:
            raise ValidationError(f"资源不存在：{resource_type} {resource_id}")

    async def _name_maps(
        self,
    ) -> tuple[dict[str, str], dict[str, str], dict[str, str]]:
        """(表 id→名, 字段 id→名, 字段 id→所属表名)。"""
        tables = await self.meta.tables()
        columns = await self.meta.columns()
        table_names = {row.id: row.table_name for row in tables}
        return (
            table_names,
            {row.id: row.column_name for row in columns},
            {row.id: table_names.get(row.table_id, "") for row in columns},
        )

    def _to_out(
        self,
        permission: Permission,
        names: tuple[dict[str, str], dict[str, str], dict[str, str]],
    ) -> PermissionOut:
        table_names, column_names, column_tables = names
        if permission.resource_type == TABLE:
            resource_name = table_names.get(permission.resource_id)
            table_name = resource_name
        else:
            resource_name = column_names.get(permission.resource_id)
            table_name = column_tables.get(permission.resource_id)
        return PermissionOut(
            id=permission.id,
            role=permission.role,
            resource_type=permission.resource_type,
            resource_id=permission.resource_id,
            resource_name=resource_name,
            table_name=table_name,
        )
