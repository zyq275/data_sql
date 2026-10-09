"""v1 业务路由汇总。

各模块路由按阶段接入：
  阶段 3  auth / admin_users
  阶段 5  query
  阶段 7  conversation / history
  阶段 9  permission
  阶段 10 eval / dashboard
"""

from __future__ import annotations

from fastapi import APIRouter

from app.api.v1 import admin_users, auth, conversation, history, query

router = APIRouter()

router.include_router(auth.router)
router.include_router(admin_users.router)
router.include_router(query.router)
router.include_router(conversation.router)
router.include_router(history.router)
