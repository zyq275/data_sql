"""提示词装载（TDD 6：提示词独立存放 ``prompts/*.prompt``，支持版本管理）。

提示词与代码分开，改口径不必改代码、不必重新部署逻辑；也让 diff 里
"是提示词变了还是代码变了"一眼可辨。
"""

from __future__ import annotations

import re
from functools import lru_cache
from pathlib import Path

from app.core.config import BASE_DIR
from app.core.exceptions import ServiceError

PROMPTS_DIR = BASE_DIR / "prompts"

# 只认"小写标识符"形态的占位符。JSON 结构示例里的花括号（{"tables": [...]}）
# 因此原样保留——见 render 的说明。
_PLACEHOLDER = re.compile(r"\{([a-z_][a-z0-9_]*)\}")


@lru_cache(maxsize=None)
def load_prompt(name: str) -> str:
    """读一个提示词文件（UTF-8）。进程内缓存，文件不热更新。"""
    path = PROMPTS_DIR / f"{name}.prompt"
    if not path.exists():
        raise ServiceError(f"提示词文件不存在：{path.name}")
    return path.read_text(encoding="utf-8")


def placeholders(name: str) -> set[str]:
    """模板里声明的占位符集合。

    暴露出来是为了让测试能把"模板声明的键"和"节点实际传的键"对起来。``render``
    只校验"传了却没用到"，反过来（模板写了 ``{tabel_infos}`` 这类拼错的键）它
    无从判断——那串花括号会原样留在提示词里发给模型，模型看到一段未替换的模板变量，
    多半仍会猜着答，于是错得很安静。只有比对两个集合才能发现。
    """
    return set(_PLACEHOLDER.findall(load_prompt(name)))


def render(name: str, **values: object) -> str:
    """填提示词占位符。

    **不用 ``str.format``**：提示词里带着 JSON 结构示例，format 会把那些花括号
    当成未转义占位符直接报错，而要求作者写成 ``{{`` ``}}`` 又太容易被改坏。

    同时校验"传进来的值必须真的在模板里被用上"：代码给了 ``date_info`` 而模板里
    漏写 ``{date_info}``，这类错会静默生成一份缺上下文的提示词，
    模型照样能答，只是答得不对——最难查的那种错，所以在源头就炸掉。
    """
    text = load_prompt(name)
    unused = [key for key in values if f"{{{key}}}" not in text]
    if unused:
        raise ServiceError(
            f"提示词 {name} 未使用占位符：{'、'.join(sorted(unused))}"
        )
    return _PLACEHOLDER.sub(
        lambda m: str(values[m.group(1)]) if m.group(1) in values else m.group(0),
        text,
    )
