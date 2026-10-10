"""eval_case / eval_run 两张表的存取（TDD 8.1，FR-EVAL-001~003）。

两个类放一个模块，理由与 ``repositories.conversation`` 相同：它们的读法不同
（题目按时间倒序列出，运行记录按时间倒序看趋势），但服务层永远是成对使用——
一次评测运行必然是"读一批题目 → 写一条运行记录"。

**主键用时间有序的形态**（``repositories.ids.new_ordered_id``）：两张表都要按
"发生顺序"读回来，而它们的 ``created_at`` 是 MySQL 的 ``DATETIME``，精度只到秒。
批量导入几十条样本、或同一秒内跑完两次评测，按 ``created_at`` 排出来的次序是
数据库说不上来的；而看板的趋势图按这个次序连线，错序的折线看起来只是"抖动"，
不会有人怀疑它排错了。
"""

from __future__ import annotations

from collections.abc import Sequence
from decimal import Decimal

from sqlalchemy import Select, func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models import EvalCase, EvalRun
from app.repositories.ids import new_ordered_id

#: 一次评测运行的三种状态，与 DDL 注释一致。
STATUS_RUNNING = "running"
STATUS_FINISHED = "finished"
STATUS_FAILED = "failed"


class EvalCaseRepository:
    """评测样本存取。"""

    def __init__(self, session: AsyncSession) -> None:
        self.session = session

    async def get(self, case_id: str) -> EvalCase | None:
        return await self.session.get(EvalCase, case_id)

    async def list(self, *, page: int, page_size: int) -> tuple[Sequence[EvalCase], int]:
        """按时间倒序分页列出评测样本（FR-EVAL-001）。"""
        stmt: Select = (
            select(EvalCase)
            # 主键时间有序，拿它当同一秒内的次序稳定器（见模块说明）
            .order_by(EvalCase.created_at.desc(), EvalCase.id.desc())
            .offset((page - 1) * page_size)
            .limit(page_size)
        )
        total = (
            await self.session.execute(select(func.count()).select_from(EvalCase))
        ).scalar_one()
        items = (await self.session.execute(stmt)).scalars().all()
        return items, total

    async def all(self) -> Sequence[EvalCase]:
        """全部样本，按导入顺序（评测要一条不落地跑完，不适用分页）。"""
        stmt = select(EvalCase).order_by(EvalCase.created_at.asc(), EvalCase.id.asc())
        return (await self.session.execute(stmt)).scalars().all()

    async def by_ids(self, case_ids: Sequence[str]) -> Sequence[EvalCase]:
        """按 id 取一批样本，返回顺序与传入一致。

        顺序要自己排回去：``IN`` 查询的返回顺序由数据库决定。而调用方（评测运行）
        传入的是一个有意义的次序，丢掉它就等于让"这次评测跑了哪几条、按什么顺序"
        无从复现。
        """
        if not case_ids:
            return []
        stmt = select(EvalCase).where(EvalCase.id.in_(list(case_ids)))
        found = {row.id: row for row in (await self.session.execute(stmt)).scalars().all()}
        return [found[case_id] for case_id in case_ids if case_id in found]

    async def create(
        self,
        *,
        query_text: str,
        expected_sql: str | None,
        expected_result: dict | None,
    ) -> EvalCase:
        record = EvalCase(
            id=new_ordered_id(),
            query_text=query_text,
            expected_sql=expected_sql,
            expected_result=expected_result,
        )
        self.session.add(record)
        await self.session.flush()
        return record

    async def delete(self, case: EvalCase) -> None:
        await self.session.delete(case)
        await self.session.flush()

    async def count(self) -> int:
        return (await self.session.execute(select(func.count()).select_from(EvalCase))).scalar_one()


class EvalRunRepository:
    """评测运行记录存取。"""

    def __init__(self, session: AsyncSession) -> None:
        self.session = session

    async def get(self, run_id: str) -> EvalRun | None:
        return await self.session.get(EvalRun, run_id)

    async def list(self, *, page: int, page_size: int) -> tuple[Sequence[EvalRun], int]:
        """按时间**倒序**分页列出运行记录（FR-EVAL-003 的趋势数据）。"""
        stmt: Select = (
            select(EvalRun)
            .order_by(EvalRun.created_at.desc(), EvalRun.id.desc())
            .offset((page - 1) * page_size)
            .limit(page_size)
        )
        total = (await self.session.execute(select(func.count()).select_from(EvalRun))).scalar_one()
        items = (await self.session.execute(stmt)).scalars().all()
        return items, total

    async def create(self, *, name: str | None, total_cases: int) -> EvalRun:
        """建一条 ``running`` 的运行记录。

        **先落记录再跑**：接口要在"还没跑完"的这一刻就把 id 交出去，客户端拿着它
        轮询进度。反过来（跑完再落）会让一次几百条样本的评测在前端看来是"请求卡死"，
        而且进程一旦重启，这次运行就彻底没有痕迹了。
        """
        record = EvalRun(
            id=new_ordered_id(),
            name=name,
            total_cases=total_cases,
            status=STATUS_RUNNING,
            detail={"cases": []},
        )
        self.session.add(record)
        await self.session.flush()
        return record

    async def finish(
        self,
        run: EvalRun,
        *,
        total_cases: int,
        accuracy: Decimal | None,
        recall_hit_rate: Decimal | None,
        execute_success_rate: Decimal | None,
        avg_duration_ms: int | None,
        detail: dict,
    ) -> None:
        """把汇总指标与逐条明细一次性写回。

        ``total_cases`` 在这里再写一遍，是因为它可能与建记录时估的那个数不同：
        样本在运行过程中被删掉是可能的，而一条"总数 50、实际跑了 48 条"的运行记录
        会让分母对不上——准确率到底除的是哪个数，只能靠人去数明细。
        """
        run.total_cases = total_cases
        run.accuracy = accuracy
        run.recall_hit_rate = recall_hit_rate
        run.execute_success_rate = execute_success_rate
        run.avg_duration_ms = avg_duration_ms
        run.detail = detail
        run.status = STATUS_FINISHED
        await self.session.flush()

    async def fail(self, run: EvalRun, *, message: str) -> None:
        """标记为失败。

        失败也留逐条明细：能跑到哪一条就记到哪一条，比只留一句"失败了"有用得多——
        它同时说明"跑了几条才出的事"，而这决定了这次运行的结论还剩多少参考价值。
        """
        run.status = STATUS_FAILED
        detail = dict(run.detail or {})
        detail["error"] = message
        run.detail = detail
        await self.session.flush()
