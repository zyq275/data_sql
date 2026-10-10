"""请求上下文：一个请求一个 ``request_id``，日志与链路共用同一个值（NFR-OBS）。

**为什么是 contextvar 而不是往函数签名上挂参数**：request_id 要出现在**每一条**
日志里，包括那些离入口十几层的模块（SQL 校验、重排降级、落库失败告警……）。
逐层传参意味着每个函数签名都得为它多一个参数，而且只要有一层忘了往下传，
那条日志就没有 id——症状是"大部分日志能串起来，偶尔几条串不上"，排查时
恰好缺的就是那几条。contextvar 是每个任务一份的隐式携带，加不加参数都不影响。

**为什么用 asyncio 的上下文而不是 threading.local**：整条链路是异步的，
``threading.local`` 在协程切换时不会隔离，两个并发请求会互相覆盖对方的 id。

**为什么客户端给的值要过一遍白名单**：它会进入日志行。带换行的值可以把一条日志
伪装成两条（日志伪造），超长的值可以把日志撑爆或者顶掉后面的字段。只接受
``[A-Za-z0-9._:-]`` 且不超过 64 字符的值，其余一律换成自己生成的——用户想用
自己的 id 串日志是可以满足的，但"照单全收"不行。
"""

from __future__ import annotations

import contextvars
import re
import uuid
from contextvars import Token

REQUEST_ID_HEADER = "X-Request-Id"
"""请求头名。阶段 1 的 CORS 白名单里已经留了这个头（见 api.middleware）。"""

#: 客户端自带的 request_id 只有长得像话才被采用，理由见模块说明。
SAFE_REQUEST_ID = re.compile(r"\A[A-Za-z0-9._:-]{1,64}\Z")

_request_id: contextvars.ContextVar[str] = contextvars.ContextVar("request_id", default="")


def new_request_id() -> str:
    """生成一个 request_id。

    用 uuid4 的十六进制前 16 位而不是完整的 36 字符：它要被打进每一条日志，
    长度直接乘上日志行数；16 位十六进制的碰撞概率在"单机一段时间的请求量"
    这个量级上可以忽略，而它仍然是从同一个 uuid4 里取的，没有引入新的随机性来源。
    """
    return uuid.uuid4().hex[:16]


def normalize_request_id(raw: str | None) -> str:
    """客户端给的值能用就用，不能用就自己生成一个（不报错）。

    这里**不拒绝请求**：request_id 是排障线索，不是安全边界，
    为了一个不合规的头拒掉一次问数，代价远大于换一个 id。
    """
    if raw:
        candidate = raw.strip()
        if SAFE_REQUEST_ID.match(candidate):
            return candidate
    return new_request_id()


def set_request_id(value: str) -> Token[str]:
    """设置当前上下文的 request_id，返回用于还原的 token。"""
    return _request_id.set(value)


def reset_request_id(token: Token[str]) -> None:
    _request_id.reset(token)


def current_request_id() -> str:
    """当前请求的 id；不在请求里（如命令行脚本）返回空串。

    返回空串而不是 None：调用方基本都是"塞进日志格式串"，空串直接得到
    ``req=``，而 None 会变成字面的 ``req=None``——后者看着像一个真的 id。
    需要"没有"这个语义的调用方自己判空。
    """
    return _request_id.get()
