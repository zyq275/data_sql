"""接口层公共依赖别名。

集中声明，避免每个路由文件重复 ``Annotated[..., Depends(...)]``。
"""

from __future__ import annotations

from typing import Annotated

from fastapi import Depends
from sqlalchemy.ext.asyncio import AsyncSession

from app.clients.mysql import get_session
from app.core.security import get_current_user, require_admin, require_min_role
from app.models import User

SessionDep = Annotated[AsyncSession, Depends(get_session)]
"""每请求一个 meta 库会话。"""

AuthUser = Annotated[User, Depends(get_current_user)]
"""已认证用户（任意角色）；未认证或账号禁用返回 401（FR-AUTH-005）。"""

AdminUser = Annotated[User, Depends(require_admin)]
"""管理员；角色不足返回 403（FR-ADMIN-001~004）。"""

AnalystUser = Annotated[User, Depends(require_min_role("analyst"))]
"""analyst 及以上（admin 亦可），供阶段 10 数据看板使用。"""
