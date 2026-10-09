"""关键字抽取与扩展：产出三路召回各自的检索词。"""

from __future__ import annotations

import asyncio
import logging

from langgraph.runtime import Runtime

from app.agent.context import QueryContext
from app.agent.events import step
from app.agent.parsing import parse_json_array
from app.agent.prompts import render
from app.agent.state import QueryState
from app.core.exceptions import LLMServiceError

logger = logging.getLogger(__name__)

MAX_EXPANDED_KEYWORDS = 8

# 三路各一份提示词：字段路要"金额 / 日期 / 等级"这类字段概念，取值路要"华东 / 金卡"
# 这类会直接出现在数据里的值，指标路要"销售额 / 销量"这类可度量口径。
# 用一份提示词产三套词只会互相干扰（同一个词在三路里的相关性完全相反），
# 所以分三次调用，与 TDD 6 的三份 extend_keywords_for_* 提示词一一对应。
_ROUTES: tuple[tuple[str, str, str], ...] = (
    ("column", "extend_keywords_for_column_recall", "字段"),
    ("value", "extend_keywords_for_value_recall", "取值"),
    ("metric", "extend_keywords_for_metric_recall", "指标"),
)


@step("extract_keywords")
async def extract_keywords(state: QueryState, runtime: Runtime[QueryContext]) -> dict:
    """把问句扩展成三路检索词。

    三次调用彼此无依赖，并发发出：串行会让这一步耗时等于三次 LLM 往返之和，
    而它正好在整条链路最前面，用户等待时间看得最明显。
    """
    query = state["resolved_query"]
    expanded = await asyncio.gather(
        *(_expand(runtime.context, prompt_name, label, query) for _kind, prompt_name, label in _ROUTES)
    )
    return {
        f"{kind}_keywords": words
        for (kind, _prompt, _label), words in zip(_ROUTES, expanded)
    }


async def _expand(
    context: QueryContext, prompt_name: str, label: str, query: str
) -> list[str]:
    """扩展一路检索词；失败时退回原始问句。

    扩展是锦上添花：问句本身已含关键信息，ik 分词也认得出里面的实词
    （阶段 4 的验收就是这么跑的）。因为一次扩展失败就把整条链路判死，
    等于把"可用但稍差"的服务变成"完全不可用"。

    但只吞**上游类**错误（LLMServiceError）。提示词文件缺失、模板与代码的占位符
    对不上属于部署/配置问题——那种错会每次都错，静默降级等于永远发现不了。
    """
    try:
        raw = await context.llm.text(render(prompt_name, query=query))
        words = parse_json_array(raw, context=f"{label}关键字扩展")
    except LLMServiceError as exc:
        logger.warning("%s关键字扩展失败，退回原始问句检索：%s", label, exc)
        words = []

    merged = [query, *words]
    unique = list(dict.fromkeys(word.strip() for word in merged if word.strip()))
    return unique[: MAX_EXPANDED_KEYWORDS + 1]
