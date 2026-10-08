"""v1 业务路由汇总。

各模块路由按阶段接入：
  阶段 3  auth / admin
  阶段 7  conversation / query / history
  阶段 9  permission
  阶段 10 eval / dashboard
"""

from __future__ import annotations

from fastapi import APIRouter

router = APIRouter()
