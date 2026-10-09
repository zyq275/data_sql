"""LLM 输出的解析（JSON 部分）。

"只输出 JSON" 是**请求**不是**保证**：模型会习惯性裹代码块、会在 JSON 前后
补一句解释。这里只做能可靠做对的那部分——剥围栏、抠出最外层 JSON——
解析不出来的一律报错，由调用方按各自的降级策略决定怎么办。

**不在这里替调用方兜底**：关键字扩展失败退回原问句是合理的，筛选表失败
退回"全部保留"也是合理的，但生成 SQL 失败没有合理的兜底。把兜底策略
放在各自的节点里，比在这里猜一个通用答案诚实。
"""

from __future__ import annotations

import json

from app.core.exceptions import LLMServiceError
from app.core.text import strip_code_fence

# 从夹杂说明文字的输出里抠最外层 JSON：第一个 { 或 [ 到最后一个 } 或 ]。
_JSON_START = "{["
_JSON_END = "}]"


def _load(text: str) -> object:
    cleaned = strip_code_fence(text)
    if not cleaned:
        raise LLMServiceError("LLM 返回空内容")
    try:
        return json.loads(cleaned)
    except json.JSONDecodeError:
        pass

    start = min(
        (pos for pos in (cleaned.find(c) for c in _JSON_START) if pos >= 0),
        default=-1,
    )
    end = max(cleaned.rfind(c) for c in _JSON_END)
    if start < 0 or end <= start:
        raise LLMServiceError("LLM 输出里找不到 JSON")
    try:
        return json.loads(cleaned[start : end + 1])
    except json.JSONDecodeError as exc:
        raise LLMServiceError(f"LLM 输出的 JSON 无法解析：{exc.msg}") from exc


def parse_json_array(text: str, *, context: str) -> list[str]:
    """解析字符串数组（关键字扩展用）。非字符串元素转成字符串后一并保留。"""
    data = _load(text)
    if not isinstance(data, list):
        raise LLMServiceError(f"{context}：期望 JSON 数组，实际是 {type(data).__name__}")
    items = [
        str(item).strip() for item in data if isinstance(item, (str, int, float)) and not isinstance(item, bool)
    ]
    return [item for item in items if item]


def parse_json_object(text: str, *, context: str) -> dict:
    """解析 JSON 对象（表结构筛选用）。"""
    data = _load(text)
    if not isinstance(data, dict):
        raise LLMServiceError(f"{context}：期望 JSON 对象，实际是 {type(data).__name__}")
    return data
