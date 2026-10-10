"""数据权限请求/响应模型（FR-PERM-002）。

``resource_id`` 存的是 ``table_info.id`` / ``column_info.id``（uuid5，由自然键确定），
而不是表名/字段名——名字会随知识库重建而变，id 不会（TDD 8.1）。代价是接口的入参
对人不友好，所以：

- ``GET /permissions/resources`` 提供可授权的表/字段清单（id 与名字的对照表），
  管理界面据此渲染勾选项；
- 响应里带 ``resource_name`` / ``table_name``（由服务层联查元数据补上），
  让"这条权限指的是哪个字段"不必再查一次。
"""

from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, ConfigDict, Field

from app.schemas.user import Role

ResourceType = Literal["table", "column"]

RESOURCE_ID_FIELD = Field(min_length=1, max_length=64, description="table_info.id 或 column_info.id")


class PermissionCreateRequest(BaseModel):
    """新增一条角色-资源映射（FR-PERM-002）。"""

    role: Role
    resource_type: ResourceType
    resource_id: str = RESOURCE_ID_FIELD


class PermissionUpdateRequest(BaseModel):
    """修改一条映射。

    三个字段都必填而不是只改传上来的那些：一条映射就是一个三元组，
    "只改 role"与"换成另一个三元组"没有区别，全量提交比局部更新少一层"哪个字段没传"
    的歧义。
    """

    role: Role
    resource_type: ResourceType
    resource_id: str = RESOURCE_ID_FIELD


class PermissionOut(BaseModel):
    """一条权限映射。"""

    model_config = ConfigDict(from_attributes=True)

    id: str
    role: str
    resource_type: str
    resource_id: str
    resource_name: str | None = None
    """资源的业务名字；资源已从知识库消失时为 None（悬空引用）。"""
    table_name: str | None = None
    """字段所属的表；resource_type=table 时为该表名本身。"""


class ResourceOut(BaseModel):
    """一个可授权的资源（供管理界面渲染勾选项）。"""

    id: str
    resource_type: ResourceType
    name: str
    table_name: str
    table_alias: str | None = None
    column_alias: str | None = None
    description: str | None = None


class ResourceCatalogResponse(BaseModel):
    """可授权资源的全集。"""

    tables: list[ResourceOut]
    columns: list[ResourceOut]


class RoleScopeOut(BaseModel):
    """某角色当前**实际生效**的可见范围（按网关的口径算出来给人看）。

    它与管理界面上的勾选列表不是一回事，所以单独给一个接口：勾选列表回答
    "配了什么"，这里回答"算出来是什么"——两者相同时才是符合预期的。
    最容易出意外的正是它俩不一致的时候：某个角色一条都没配（于是不设限）、
    或者配的字段已被知识库重建删掉（于是那条权限是悬空的）。
    """

    role: str
    unrestricted: bool
    """为 True 表示该角色没有任何配置，当前**不限制**可用表与字段。"""
    tables: dict[str, list[str] | None]
    """表名 → 可见字段名；``None`` 表示整表可用（含以后新增的字段）。"""
    metrics: list[str]
    forbidden_tables: list[str]
    forbidden_columns: dict[str, list[str]]
    """表名 → 该表里未授权的字段名；只含"整表可用但被字段级收窄"的那几张表。

    按表分组而不是给一份平铺的字段名清单：字段名在数仓里不唯一
    （``fact_order.product_id`` 与 ``dim_product.product_id`` 同名），
    平铺之后"这个字段能不能用"就没法回答了。
    """
