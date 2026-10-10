"""评测集的增删查与评测运行的记录查询（FR-EVAL-001~003）。

**发起运行与实际执行是分开的**（``start_run`` 在这里，批量执行在 ``eval_runner``）：
一次几百条样本的评测要跑几十分钟，接口不可能挂在那儿等。所以这里只做一件
"很快就能做完"的事——落一条 ``running`` 记录并把 id 交出去，真正的执行交给
后台任务，客户端拿 id 轮询。这样"评测在跑"和"接口卡死"在前端是两种可区分的状态。
"""

from __future__ import annotations

import logging
from datetime import datetime

from sqlalchemy.ext.asyncio import AsyncSession

from app.core.exceptions import NotFoundError, ValidationError
from app.core.response import PageData
from app.repositories.eval import EvalCaseRepository, EvalRunRepository
from app.schemas.eval import (
    EvalCaseImportRequest,
    EvalCaseImportResult,
    EvalCaseOut,
    EvalRunDetail,
    EvalRunOut,
    EvalRunRequest,
)
from app.services.eval_runner import to_detail

logger = logging.getLogger(__name__)


class EvalService:
    """评测集与评测运行记录的读写。"""

    def __init__(self, session: AsyncSession) -> None:
        self.session = session
        self.cases = EvalCaseRepository(session)
        self.runs = EvalRunRepository(session)

    # ---- 评测集 ----------------------------------------------------------

    async def list_cases(self, *, page: int, page_size: int) -> PageData[EvalCaseOut]:
        """分页列出评测样本（FR-EVAL-001）。"""
        items, total = await self.cases.list(page=page, page_size=page_size)
        return PageData[EvalCaseOut](
            items=[EvalCaseOut.model_validate(item) for item in items],
            total=total,
            page=page,
            page_size=page_size,
        )

    async def import_cases(self, payload: EvalCaseImportRequest) -> EvalCaseImportResult:
        """批量导入（FR-EVAL-001）。

        **同批里的重复问题直接拒掉**：一次导入进来两条一样的题，会把准确率的分母
        凭空撑大（同一条被算了两次），而这在结果里看不出来——只表现为"这次成绩
        好像变差了"。跨批次重复不拦：删了重导是正常操作，判重得靠人。

        expected_result 存成 ``model_dump`` 而不是原样对象：存进去的形状就是比对时
        读出来的形状，中间不加任何转换（Pydantic 对象直接塞 JSON 列会在 commit 时抛错）。
        """
        seen: set[str] = set()
        duplicates: list[str] = []
        for case in payload.cases:
            text = case.query_text.strip()
            if text in seen:
                duplicates.append(text)
            seen.add(text)
        if duplicates:
            raise ValidationError(f"同一批里出现重复的评测问题：{duplicates[0]}")

        created = []
        for case in payload.cases:
            created.append(
                await self.cases.create(
                    query_text=case.query_text.strip(),
                    expected_sql=case.expected_sql,
                    expected_result=(
                        case.expected_result.model_dump() if case.expected_result else None
                    ),
                )
            )
        await self.session.commit()
        return EvalCaseImportResult(
            imported=len(created),
            items=[EvalCaseOut.model_validate(item) for item in created],
        )

    async def delete_case(self, case_id: str) -> None:
        """删除一条样本（FR-EVAL-001）。

        已跑过的运行记录不受影响：``eval_run.detail`` 里存的是**当时**那份快照，
        删掉题目不该让历史成绩跟着变形——那样趋势图会自己改自己。
        """
        case = await self.cases.get(case_id)
        if case is None:
            raise NotFoundError("评测样本不存在")
        await self.cases.delete(case)
        await self.session.commit()

    # ---- 评测运行 --------------------------------------------------------

    async def start_run(self, payload: EvalRunRequest) -> EvalRunOut:
        """落一条 ``running`` 记录，返回它的 id 供客户端轮询（FR-EVAL-002）。

        ``total_cases`` 在**建记录时就估出来**：客户端拿到 id 立刻就能显示
        "0/120"，而不用等后台任务第一次写回。它随后会被实际跑完的条数覆盖
        （见 ``EvalRunRepository.finish``），所以这里估错也不会留下错数。
        """
        if payload.case_ids:
            found = await self.cases.by_ids(payload.case_ids)
            missing = [case_id for case_id in payload.case_ids if case_id not in {c.id for c in found}]
            if missing:
                raise ValidationError(f"评测样本不存在：{missing[0]}")
            total = len(found)
        else:
            total = await self.cases.count()
        if not total:
            raise ValidationError("评测集为空，没有可运行的样本")

        run = await self.runs.create(name=payload.name or _default_name(), total_cases=total)
        await self.session.commit()
        return EvalRunOut.model_validate(run)

    async def list_runs(self, *, page: int, page_size: int) -> PageData[EvalRunOut]:
        """分页列出运行记录，时间倒序（FR-EVAL-003 的趋势数据）。"""
        items, total = await self.runs.list(page=page, page_size=page_size)
        return PageData[EvalRunOut](
            items=[EvalRunOut.model_validate(item) for item in items],
            total=total,
            page=page,
            page_size=page_size,
        )

    async def get_run(self, run_id: str) -> EvalRunDetail:
        """运行详情：汇总指标 + 逐条明细（FR-EVAL-003）。"""
        run = await self.runs.get(run_id)
        if run is None:
            raise NotFoundError("评测运行记录不存在")
        return to_detail(run)


def _default_name() -> str:
    """没给名字时按时间起一个。

    默认名而不是留空：看板的趋势图是一个列表，一排没有名字的记录等于一排时间戳，
    人对"哪次是调完召回之后跑的"这件事只能靠对时间。
    """
    return f"评测 {datetime.now():%Y-%m-%d %H:%M}"
