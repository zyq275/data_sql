"""用户相关请求/响应模型（FR-AUTH-003、FR-ADMIN-001~004）。

注意：出参一律不含 ``password_hash``，哈希只存在于库内与进程内的短暂比较。
"""

from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, ConfigDict, Field

Role = Literal["viewer", "analyst", "admin"]

USERNAME_FIELD = Field(min_length=1, max_length=128, description="登录名，1-128 字符")
# PRD FR-AUTH-001 字段约束：密码 6-32 字符
PASSWORD_FIELD = Field(min_length=6, max_length=32, description="密码，6-32 字符")


class UserOut(BaseModel):
    """用户公开信息（FR-AUTH-003 要求的五个字段）。"""

    model_config = ConfigDict(from_attributes=True)

    id: str
    username: str
    role: str
    display_name: str | None = None
    is_active: bool | None = None


class UserCreateRequest(BaseModel):
    """管理员创建用户（FR-ADMIN-002）。"""

    username: str = USERNAME_FIELD
    password: str = PASSWORD_FIELD
    role: Role = "viewer"
    display_name: str | None = Field(default=None, max_length=128)


class UserRoleUpdateRequest(BaseModel):
    """管理员修改角色（FR-ADMIN-003）。"""

    role: Role


class UserStatusUpdateRequest(BaseModel):
    """管理员启用/禁用（FR-ADMIN-004）。"""

    is_active: bool
