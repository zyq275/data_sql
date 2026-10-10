"""数据看板接口（TDD 7.2 dashboard 组，FR-DASH-001~002）。

**鉴权是 analyst 及以上**（``AnalystUser``）：看板看的是全公司口径的聚合数，
比"我自己的查询历史"敏感一档，所以 viewer 进来直接 403。

**权限是数据权限，不是接口权限**：analyst 能进来看板，但能看哪些维度、哪些指标
仍由它自己的角色配置决定（阶段 9 的同一套口径）。接口给不出越权的数，
而不是"先进得来、再看运气"。
"""

from __future__ import annotations

from typing import Annotated

from fastapi import APIRouter, Query

from app.api.deps import AnalystUser, SessionDep
from app.core.response import ApiResponse
from app.schemas.dashboard import DashboardDimensionsResponse, DashboardMetricsResponse
from app.services.dashboard_service import DashboardService

router = APIRouter(prefix="/dashboard", tags=["数据看板"])


@router.get(
    "/dimensions",
    response_model=ApiResponse[DashboardDimensionsResponse],
    summary="可用维度与指标",
)
async def list_dimensions(
    session: SessionDep, user: AnalystUser
) -> ApiResponse[DashboardDimensionsResponse]:
    """列出当前角色可用的聚合维度与指标，供看板渲染筛选器（FR-DASH-001）。

    已经按该角色的数据权限裁过：出现在清单里的，就一定查得动。
    """
    data = await DashboardService(session).dimensions(user.role)
    return ApiResponse(data=data)


@router.get(
    "/metrics", response_model=ApiResponse[DashboardMetricsResponse], summary="指标聚合查询"
)
async def query_metrics(
    session: SessionDep,
    user: AnalystUser,
    dimension: Annotated[
        str,
        Query(
            description="聚合维度，逗号分隔可多级（如 year,month），取值见 /dashboard/dimensions",
            examples=["month"],
        ),
    ],
    metrics: Annotated[
        str | None,
        Query(description="指标名，逗号分隔；不传则用当前角色的全部可用指标"),
    ] = None,
    start: Annotated[str | None, Query(description="起始日期，YYYY-MM-DD")] = None,
    end: Annotated[str | None, Query(description="结束日期，YYYY-MM-DD")] = None,
) -> ApiResponse[DashboardMetricsResponse]:
    """按维度与时间范围聚合出指标趋势（FR-DASH-001）。

    走的是**确定性的模板 SQL**，不经过大模型：看板上的一个数每次刷新都必须是同一个，
    而同一句 SQL 在数仓上的结果天然如此。响应里带上拼好的 SQL，便于复核。

    结果为空时返回空 ``rows``，由前端渲染成"无数据"占位（FR-DASH-002）。
    """
    data = await DashboardService(session).metrics(
        user.role, dimension=dimension, metrics=metrics, start=start, end=end
    )
    return ApiResponse(data=data)
