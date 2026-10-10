"""评测体系接口（TDD 7.2 eval 组，FR-EVAL-001~003）。

整组要求 admin：评测集是**衡量系统本身的标尺**，谁能改题目谁就能改成绩，
所以它跟权限配置一样属于管理面，而不是 analyst 的日常工具。

``POST /eval/run`` **立刻返回**一条 ``running`` 记录：一次几百条的评测要跑几十分钟，
接口挂在那儿等既过不了网关超时，也让前端无法区分"在跑"与"卡死"。客户端拿返回的
id 去 ``GET /eval/runs/{id}`` 轮询状态与明细。
"""

from __future__ import annotations

from typing import Annotated

from fastapi import APIRouter, BackgroundTasks, Query

from app.api.deps import AdminUser, SessionDep
from app.core.response import ApiResponse, PageData
from app.schemas.eval import (
    EvalCaseImportRequest,
    EvalCaseImportResult,
    EvalCaseOut,
    EvalRunDetail,
    EvalRunOut,
    EvalRunRequest,
)
from app.services.eval_runner import execute_run
from app.services.eval_service import EvalService

router = APIRouter(prefix="/eval", tags=["评测体系"])

PageQuery = Annotated[int, Query(ge=1, description="页码，从 1 开始")]
PageSizeQuery = Annotated[int, Query(ge=1, le=100, description="每页条数，上限 100")]


@router.get("/cases", response_model=ApiResponse[PageData[EvalCaseOut]], summary="评测集列表")
async def list_cases(
    session: SessionDep,
    _admin: AdminUser,
    page: PageQuery = 1,
    page_size: PageSizeQuery = 20,
) -> ApiResponse[PageData[EvalCaseOut]]:
    """按时间倒序分页列出评测样本（FR-EVAL-001）。"""
    data = await EvalService(session).list_cases(page=page, page_size=page_size)
    return ApiResponse(data=data)


@router.post(
    "/cases", response_model=ApiResponse[EvalCaseImportResult], summary="批量导入评测样本"
)
async def import_cases(
    payload: EvalCaseImportRequest, session: SessionDep, _admin: AdminUser
) -> ApiResponse[EvalCaseImportResult]:
    """批量导入"问题 → 标准 SQL / 预期结果"样本（FR-EVAL-001）。

    同一批里出现重复问题返回 400：重复会把准确率的分母撑大，而在结果里看不出来。
    """
    data = await EvalService(session).import_cases(payload)
    return ApiResponse(data=data)


@router.delete("/cases/{case_id}", response_model=ApiResponse[None], summary="删除评测样本")
async def delete_case(
    case_id: str, session: SessionDep, _admin: AdminUser
) -> ApiResponse[None]:
    """删除一条样本。已跑过的运行记录不受影响（明细是当时的快照）。"""
    await EvalService(session).delete_case(case_id)
    return ApiResponse(data=None)


@router.post("/run", response_model=ApiResponse[EvalRunOut], summary="发起评测运行")
async def run_eval(
    payload: EvalRunRequest,
    background: BackgroundTasks,
    session: SessionDep,
    admin: AdminUser,
) -> ApiResponse[EvalRunOut]:
    """批量运行 Agent 处理评测集（FR-EVAL-002）。

    返回的是一条 ``running`` 记录；真正的执行交给后台任务，
    **跑的是与线上完全相同的图，但不落任何线上数据**（BR-EVAL-001）。

    评测以**发起人的角色**跑：权限是问答链路的一部分，按别人的角色跑出来的成绩
    不是这次调用者的系统表现。未配置任何权限的角色不受限，所以默认（全新环境）
    得到的正是模型本身的水平。
    """
    data = await EvalService(session).start_run(payload)
    background.add_task(
        execute_run, data.id, user_id=admin.id, role=admin.role, case_ids=payload.case_ids
    )
    return ApiResponse(data=data)


@router.get("/runs", response_model=ApiResponse[PageData[EvalRunOut]], summary="评测运行记录")
async def list_runs(
    session: SessionDep,
    _admin: AdminUser,
    page: PageQuery = 1,
    page_size: PageSizeQuery = 20,
) -> ApiResponse[PageData[EvalRunOut]]:
    """按时间倒序分页列出运行记录，供评测看板画历史趋势（FR-EVAL-003）。"""
    data = await EvalService(session).list_runs(page=page, page_size=page_size)
    return ApiResponse(data=data)


@router.get(
    "/runs/{run_id}", response_model=ApiResponse[EvalRunDetail], summary="评测运行详情"
)
async def get_run(run_id: str, session: SessionDep, _admin: AdminUser) -> ApiResponse[EvalRunDetail]:
    """运行详情：四个指标 + 逐条明细（含每条为什么被判对/判错）。

    ``status`` 为 ``running`` 时明细是空的（还没写回），这正是客户端轮询的依据。
    """
    data = await EvalService(session).get_run(run_id)
    return ApiResponse(data=data)
