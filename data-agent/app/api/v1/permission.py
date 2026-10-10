"""数据权限管理接口（TDD 7.2 permission 组，FR-PERM-002）。

整组要求 admin 角色；角色不足由依赖直接抛 403。**这里管的是"配置"，
问答链路上的"执行"在 ``agent.permissions``**：改完配置到下一次提问之间没有缓存
需要失效——网关在一轮问答内缓存角色范围，跨请求各建各的实例，
所以下一次提问读到的就是刚改完的那一份。
"""

from __future__ import annotations

from typing import Annotated

from fastapi import APIRouter, Query

from app.api.deps import AdminUser, SessionDep
from app.core.response import ApiResponse, PageData
from app.schemas.permission import (
    PermissionCreateRequest,
    PermissionOut,
    PermissionUpdateRequest,
    ResourceCatalogResponse,
    RoleScopeOut,
)
from app.schemas.user import Role
from app.services.permission_service import PermissionService

router = APIRouter(prefix="/permissions", tags=["数据权限"])

PageQuery = Annotated[int, Query(ge=1, description="页码，从 1 开始")]
PageSizeQuery = Annotated[int, Query(ge=1, le=100, description="每页条数，上限 100")]


@router.get("", response_model=ApiResponse[PageData[PermissionOut]], summary="权限映射列表")
async def list_permissions(
    session: SessionDep,
    _admin: AdminUser,
    page: PageQuery = 1,
    page_size: PageSizeQuery = 20,
    role: Annotated[Role | None, Query(description="只看某个角色；不传则列出全部")] = None,
) -> ApiResponse[PageData[PermissionOut]]:
    """分页列出角色-表/字段映射。"""
    data = await PermissionService(session).list_mappings(
        role=role, page=page, page_size=page_size
    )
    return ApiResponse(data=data)


@router.get(
    "/resources",
    response_model=ApiResponse[ResourceCatalogResponse],
    summary="可授权资源清单",
)
async def list_resources(
    session: SessionDep, _admin: AdminUser
) -> ApiResponse[ResourceCatalogResponse]:
    """列出知识库里全部表与字段（id 与名字的对照），供配置界面渲染勾选项。

    单独一个端点而不是让界面自己去查知识库：``resource_id`` 是 uuid5，
    人认不出来，界面必须有一份 id↔名字的对照表才画得出勾选框。
    """
    data = await PermissionService(session).catalog()
    return ApiResponse(data=data)


@router.get(
    "/roles/{role}/scope",
    response_model=ApiResponse[RoleScopeOut],
    summary="某角色实际生效的范围",
)
async def get_role_scope(role: Role, session: SessionDep, _admin: AdminUser) -> ApiResponse[RoleScopeOut]:
    """按网关的口径算出该角色**实际**能看到的表与字段。

    与列表接口回答的问题不同：列表说"配了什么"，这里说"算出来是什么"。
    一个角色没有任何配置时这里会返回 ``unrestricted=true``——那才是它真实的行为，
    管理界面若只显示勾选列表，会把"不设限"错看成"什么都没配好"。
    """
    data = await PermissionService(session).scope(role)
    return ApiResponse(data=data)


@router.post("", response_model=ApiResponse[PermissionOut], summary="新增权限映射")
async def create_permission(
    payload: PermissionCreateRequest, session: SessionDep, _admin: AdminUser
) -> ApiResponse[PermissionOut]:
    """给某角色授权一张表或一个字段（FR-PERM-002）。

    ``resource_id`` 指向不存在的表/字段返回 400：配一条永远不生效的权限，
    比直接报错危险得多——界面上它会显示成"已授权"。
    """
    data = await PermissionService(session).create(payload)
    return ApiResponse(data=data)


@router.put("/{permission_id}", response_model=ApiResponse[PermissionOut], summary="修改权限映射")
async def update_permission(
    permission_id: str,
    payload: PermissionUpdateRequest,
    session: SessionDep,
    _admin: AdminUser,
) -> ApiResponse[PermissionOut]:
    """整条替换一条映射（三个字段都要传）。"""
    data = await PermissionService(session).update(permission_id, payload)
    return ApiResponse(data=data)


@router.delete("/{permission_id}", response_model=ApiResponse[None], summary="删除权限映射")
async def delete_permission(
    permission_id: str, session: SessionDep, _admin: AdminUser
) -> ApiResponse[None]:
    """删除一条映射。删掉某角色最后一条配置会把它退回"不设限"。"""
    await PermissionService(session).delete(permission_id)
    return ApiResponse(data=None)
