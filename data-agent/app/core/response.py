"""统一响应结构（TDD 7.1）。

成功：``{"code": 0, "message": "ok", "data": ...}``
分页：``data`` 为 ``{"items": [], "total": 0, "page": 1, "page_size": 20}``

失败响应由 core.handlers 按同一形状产出，``code`` 为错误码、``data`` 为 null。
"""

from __future__ import annotations

from typing import Generic, TypeVar

from pydantic import BaseModel, Field

T = TypeVar("T")

OK_CODE = 0
OK_MESSAGE = "ok"


class ApiResponse(BaseModel, Generic[T]):
    """成功响应外壳。端点的 ``response_model`` 用 ``ApiResponse[XxxOut]`` 声明。"""

    code: int = OK_CODE
    message: str = OK_MESSAGE
    data: T | None = None


class PageData(BaseModel, Generic[T]):
    """分页数据体，作为 ``ApiResponse.data`` 使用。"""

    items: list[T] = Field(default_factory=list)
    total: int = 0
    page: int = 1
    page_size: int = 20
