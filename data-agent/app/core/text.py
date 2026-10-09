"""LLM 输出文本的清洗（各节点共用）。

LLM 常把 SQL / JSON 包在 Markdown 代码块里，尽管提示词里三令五申禁止。这是
"上游不守约定"而不是"我方逻辑错"，所以在入口统一剥掉，而不是指望把提示词写得更严厉
—— 叮嘱是不可靠的，剥壳是可靠的。剥不掉的部分（真不是 SQL / JSON）由下游各自报错。
"""

from __future__ import annotations

import re

# 只匹配"整段被围栏包住"的情况：首尾都是 ``` 才剥。语句中间的 ``` 不动它，
# 否则一句正常的反引号标识符都可能被误伤。
_FENCED = re.compile(
    r"^\s*```[a-zA-Z0-9_+-]*\s*\n?(?P<body>.*?)\n?\s*```\s*$",
    re.DOTALL,
)


def strip_code_fence(text: str) -> str:
    """剥掉整体包裹的 Markdown 代码块围栏，并去掉首尾空白。"""
    match = _FENCED.match(text)
    if match:
        return match.group("body").strip()
    return text.strip()
