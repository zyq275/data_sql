"""评测体系的请求/响应模型（TDD 7.2 eval 组，FR-EVAL-001~003）。

**导入用一个外层对象包住数组**（``{"cases": [...]}``）而不是直接收一个数组：
顶层是数组的请求体没法再加字段，而这个接口迟早要带"这批样本的批次名"之类的
元信息；更现实的是，顶层数组的入参校验错误在 FastAPI 里定位不到是第几条，
包一层之后 ``cases.3.query_text`` 这样的路径会直接出现在错误信息里。
"""

from __future__ import annotations

from datetime import datetime
from typing import Any

from pydantic import BaseModel, ConfigDict, Field

MAX_IMPORT_CASES = 500
"""单次批量导入的样本上限。评测集是人手整理的，一批几百条已经很激进；
不设上限的话，一个手滑的循环能把 eval_case 灌成几十万行，而列表接口还是分页的，
清理起来只能靠 SQL。"""

MAX_QUERY_CHARS = 500
MAX_SQL_CHARS = 8000
MAX_NAME_CHARS = 256


class ExpectedResult(BaseModel):
    """预期结果集（FR-EVAL-001 的"预期结果"）。

    与查询结果的形态一致（``columns`` + ``rows``），因为准确率是**按结果集判**的：
    存下来的是什么形状，比对时就拿同一种形状去比，中间不做任何转换。
    """

    columns: list[str] = Field(default_factory=list)
    rows: list[list[Any]] = Field(default_factory=list)


class EvalCaseCreate(BaseModel):
    """一条评测样本。"""

    query_text: str = Field(min_length=1, max_length=MAX_QUERY_CHARS, description="问题原文")
    expected_sql: str | None = Field(default=None, max_length=MAX_SQL_CHARS)
    expected_result: ExpectedResult | None = None


class EvalCaseImportRequest(BaseModel):
    """批量导入（FR-EVAL-001）。"""

    cases: list[EvalCaseCreate] = Field(min_length=1, max_length=MAX_IMPORT_CASES)


class EvalCaseOut(BaseModel):
    """评测样本（FR-EVAL-001）。"""

    model_config = ConfigDict(from_attributes=True)

    id: str
    query_text: str
    expected_sql: str | None = None
    # JSON 列，标 Any 而不是 ExpectedResult：早期数据可能是别的形状，
    # 用严格模型声明会让整页列表校验失败——一条老数据不该让列表打不开
    expected_result: Any = None
    created_at: datetime


class EvalCaseImportResult(BaseModel):
    """导入结果：告诉调用方到底进去几条。"""

    imported: int
    items: list[EvalCaseOut] = Field(default_factory=list)


class EvalRunRequest(BaseModel):
    """发起一次评测运行（FR-EVAL-002）。"""

    name: str | None = Field(default=None, max_length=MAX_NAME_CHARS)
    case_ids: list[str] | None = Field(
        default=None, max_length=MAX_IMPORT_CASES, description="只跑这些样本；不传则全量"
    )


class EvalCaseOutcome(BaseModel):
    """单条样本的评测结果（eval_run.detail 里的一项）。

    ``recall_hit`` 可以为 None：没有 ``expected_sql`` 的样本判不出"该召回哪些表"，
    此时它**不进召回命中率的分母**。用 False 表示"没命中"会把"不知道"说成"没做到"，
    而这两个结论对知识库维护者来说指向完全不同的动作。
    """

    case_id: str
    query_text: str
    generated_sql: str | None = None
    correct: bool = False
    recall_hit: bool | None = None
    executed: bool = False
    duration_ms: int = 0
    reason: str | None = None


class EvalRunOut(BaseModel):
    """评测运行记录（列表项，FR-EVAL-003 的趋势数据）。"""

    model_config = ConfigDict(from_attributes=True)

    id: str
    name: str | None = None
    total_cases: int = 0
    accuracy: float | None = None
    recall_hit_rate: float | None = None
    execute_success_rate: float | None = None
    avg_duration_ms: int | None = None
    status: str | None = None
    created_at: datetime


class EvalRunDetail(EvalRunOut):
    """运行详情：在列表项之上补出逐条明细。

    继承而不是另起一个类，理由同 ``schemas.history``：详情必须包含列表里的全部字段，
    否则会出现"列表里看得到准确率、点进去反而没了"。
    """

    outcomes: list[EvalCaseOutcome] = Field(default_factory=list)
    error: str | None = None
