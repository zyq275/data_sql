"""阶段 7：指代消解节点（FR-CONV-004，TDD 5.1 的图入口）。

纯逻辑，全部用桩模型：这里要验的是"什么情况下改写、什么情况下退回原问句"，
以及模型输出里的残留（引号、代码块、"改写后："前缀）会不会被带进下游。
真实模型改写得对不对由 test_stage7_acceptance 覆盖。
"""

from __future__ import annotations

from types import SimpleNamespace

import pytest

from app.agent.nodes.resolve_reference import MAX_RESOLVED_CHARS, _clean, resolve_reference
from app.core.exceptions import LLMServiceError

QUESTION = "各季度销售额"
FOLLOW_UP = "只看华东"
REWRITTEN = "只看华东地区各季度销售额"

HISTORY = [
    {
        "query": QUESTION,
        "sql": (
            "SELECT quarter, SUM(amount) FROM fact_order "
            "WHERE region = '华东' GROUP BY quarter"
        ),
    }
]


class StubLLM:
    """只回一句话的模型桩，顺带记下它收到的提示词。"""

    def __init__(self, reply: str = REWRITTEN, *, error: Exception | None = None) -> None:
        self.reply = reply
        self.error = error
        self.prompts: list[str] = []

    async def text(self, prompt: str, *, system: str | None = None) -> str:
        self.prompts.append(prompt)
        if self.error is not None:
            raise self.error
        return self.reply


def runtime_with(llm: StubLLM):
    """最小 Runtime 替身：节点只用到 context.llm 与 stream_writer。"""
    events: list[dict] = []
    runtime = SimpleNamespace(
        context=SimpleNamespace(llm=llm),
        stream_writer=events.append,
    )
    return runtime, events


async def run(*, query: str = FOLLOW_UP, history=None, llm: StubLLM | None = None):
    llm = llm or StubLLM()
    runtime, events = runtime_with(llm)
    state = {"query": query, "history": history if history is not None else HISTORY}
    return await resolve_reference(state, runtime), llm, events


# ==========================================================================
# 首轮原样透传：不调模型
# ==========================================================================


async def test_first_turn_passes_the_question_through_untouched() -> None:
    """没有历史时原样透传（验收标准）。

    这里同时钉住"一次模型调用都不发"：让模型对着空历史判断要不要改写，
    它偶尔会凭空补出一个历史里没有的年份，而那种错误在结果里看不出来。
    """
    result, llm, _events = await run(query=QUESTION, history=[])

    assert result == {"resolved_query": QUESTION}
    assert llm.prompts == [], "首轮不该有任何模型调用"


@pytest.mark.parametrize("history", [None, [], ()])
async def test_missing_history_is_treated_as_the_first_turn(history) -> None:
    """history 字段缺失（键不存在）与空列表是同一件事，都要能跑。"""
    llm = StubLLM()
    runtime, _events = runtime_with(llm)

    result = await resolve_reference({"query": QUESTION, "history": history}, runtime)

    assert result["resolved_query"] == QUESTION
    assert llm.prompts == []


# ==========================================================================
# 有历史：改写
# ==========================================================================


async def test_follow_up_is_rewritten_into_a_standalone_question() -> None:
    result, llm, _events = await run()

    assert result == {"resolved_query": REWRITTEN}
    assert len(llm.prompts) == 1


async def test_prompt_carries_the_history_and_the_current_question() -> None:
    """提示词里必须真的带上上文——否则模型只能瞎补。

    断言渲染后的成文（"第 1 轮"、上一轮的问题、上一轮的过滤条件），而不是
    断言模板占位符：占位符对了但渲染时传空串，模型同样看不到上文。
    """
    _result, llm, _events = await run()
    prompt = llm.prompts[0]

    assert "第 1 轮" in prompt
    assert QUESTION in prompt
    assert FOLLOW_UP in prompt
    # 上文里的 SQL 与过滤条件（TDD 5.3 要求注入这三样）
    assert "GROUP BY quarter" in prompt
    assert "过滤条件" in prompt
    assert "WHERE region = '华东'" in prompt


async def test_first_line_wins_when_the_model_adds_an_explanation() -> None:
    """模型偶尔在问句后补一句解释，问句本身总在第一行。"""
    result, _llm, _events = await run(llm=StubLLM("只看华东地区各季度销售额\n（补全了'那'指代的地区）"))

    assert result["resolved_query"] == REWRITTEN


# ==========================================================================
# 退回原问句：任何"改不出来"的情况都不该让整轮问答失败
# ==========================================================================


async def test_llm_failure_falls_back_to_the_original_question() -> None:
    """消解是"让问题更清楚"，不是"没有它就没法答"。

    因一次上游抖动把整轮问答判死，等于把可用服务变成不可用。
    """
    llm = StubLLM(error=LLMServiceError("上游超时"))
    result, _llm, _events = await run(llm=llm)

    assert result == {"resolved_query": FOLLOW_UP}


@pytest.mark.parametrize("reply", ["", "   ", "\n\n"])
async def test_empty_reply_falls_back(reply: str) -> None:
    """空回复退回原问句。

    这一点格外要紧：resolved_query 空掉的话，下游每条提示词的 {query} 都变空，
    模型照样会"答"出一个看起来正常的 SQL，错误一路跑到结果里。
    """
    result, _llm, _events = await run(llm=StubLLM(reply))

    assert result["resolved_query"] == FOLLOW_UP


async def test_overlong_reply_falls_back() -> None:
    """失控的输出（自问自答、把历史全抄一遍）不能污染整轮问答。"""
    result, _llm, _events = await run(llm=StubLLM("华东" * (MAX_RESOLVED_CHARS // 2 + 10)))

    assert result["resolved_query"] == FOLLOW_UP


# ==========================================================================
# 输出清洗：残留会进入下游每一条提示词
# ==========================================================================


@pytest.mark.parametrize(
    "raw",
    [
        REWRITTEN,
        f'"{REWRITTEN}"',
        f"'{REWRITTEN}'",
        f"“{REWRITTEN}”",
        f"```\n{REWRITTEN}\n```",
        f"```text\n{REWRITTEN}\n```",
        f"改写后：{REWRITTEN}",
        f"完整问题：{REWRITTEN}",
        f"  {REWRITTEN}  ",
    ],
)
def test_clean_strips_the_usual_model_decorations(raw: str) -> None:
    assert _clean(raw) == REWRITTEN


def test_clean_keeps_a_legitimate_leading_quote() -> None:
    """只剥成对引号：`"去年"销售额` 是正常内容，削掉头一个引号就改错了。"""
    assert _clean('"去年"销售额') == '"去年"销售额'


def test_clean_never_returns_a_prefixed_counterpart_of_a_real_question() -> None:
    """剥前缀只该剥提示词里那几种，不该把内容里的冒号当成前缀。"""
    assert _clean("按月份：销售额") == "按月份：销售额"


# ==========================================================================
# 进度事件
# ==========================================================================


async def test_node_reports_start_and_done() -> None:
    _result, _llm, events = await run()

    assert [(e["step"], e["status"]) for e in events] == [
        ("resolve_reference", "start"),
        ("resolve_reference", "done"),
    ]
