"""数据看板的响应模型（TDD 7.2 dashboard 组，FR-DASH-001~002）。

**响应里带上拼好的 SQL**，理由与阶段 8 的可解释性一致：看板上的一个数一旦有人
质疑，第一件要做的事就是看它是怎么算出来的。而这句 SQL 只可能用到当前角色已授权
的表与字段（服务层对着权限范围拼、拼完再兜底校验一遍），所以回显它不构成泄露。
"""

from __future__ import annotations

from typing import Any, Literal

from pydantic import BaseModel, Field

DimensionKind = Literal["time", "category"]


class DashboardDimensionOut(BaseModel):
    """一个可用于聚合的维度。"""

    name: str
    alias: str | None = None
    table: str
    kind: DimensionKind


class DashboardMetricOut(BaseModel):
    """一个可用于出数的指标。"""

    name: str
    alias: str | None = None
    description: str | None = None


class DashboardDimensionsResponse(BaseModel):
    """当前角色可用的维度与指标（FR-DASH-001）。

    两个清单一起给：看板要先画筛选器，而"有哪些维度"与"有哪些指标"是同一屏上的
    两组下拉框。分成两个接口只会让页面多一次往返，且两次请求之间权限可能已经变了，
    出现"维度是新的、指标是旧的"这种自相矛盾的组合。
    """

    dimensions: list[DashboardDimensionOut] = Field(default_factory=list)
    metrics: list[DashboardMetricOut] = Field(default_factory=list)


class DashboardMetricsResponse(BaseModel):
    """按维度与时间范围聚合出的一组指标（FR-DASH-001）。

    ``columns`` / ``rows`` 的形态与查询结果一致（阶段 5 的 result 事件同形），
    前端画图表的那段代码因此可以直接复用。结果为空时 ``rows`` 为 ``[]``——
    "无数据"由前端按 FR-DASH-002 渲染成占位，服务端不编一个零行出来。
    """

    dimensions: list[str] = Field(default_factory=list)
    columns: list[str] = Field(default_factory=list)
    rows: list[list[Any]] = Field(default_factory=list)
    row_count: int = 0
    truncated: bool = False
    metrics: list[DashboardMetricOut] = Field(default_factory=list)
    sql: str = ""
    unrestricted: bool = True
    """本次取数是否在不受限的权限范围内算的。

    带上它是因为它会改变一个数的含义：受限角色看到的"华东区 GMV"可能只是它被授权
    的那部分表算出来的。前端据此决定要不要提示"当前为受限视角"。
    """
