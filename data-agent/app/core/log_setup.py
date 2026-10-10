"""日志初始化：loguru 接管全部日志，并按请求补上 request_id（NFR-OBS）。

**为什么是"接管"而不是"改用"**：项目里已经有一百多处 ``logging.getLogger(__name__)``
（阶段 1 起就是这个写法），全量改成 ``from loguru import logger`` 是一场纯粹的
文字替换，改完既没有新能力，还会让中间那些 ``record.xxx`` 风格的自定义 handler
失效。``InterceptHandler`` 把标准库 logging 的记录原样转交给 loguru，两种写法
从此走同一条输出管线——**新代码可以按 loguru 写，老代码一个字不用动**。

**request_id 用 patcher 补，不用 filter**：loguru 的 ``logger.configure(patcher=...)``
对**每一条**记录（含经 intercept 过来的）生效，而 filter 是"sink 级"的——多个 sink
就得挂多个 filter，漏挂一个，那个 sink 的日志就没有 id。补在 record 上，
所有下游（任何 sink、任何格式串）都能用。

**diagnose 必须关掉**：loguru 默认 ``diagnose=True``，异常回溯里会**打印出栈帧的
局部变量值**。登录与改密接口的局部变量里就有明文口令，数据库连接的局部变量里
有连接串——这些一旦写进日志文件就是长期留存（NFR-SEC-003）。``backtrace`` 只影响
显示多少层调用栈，不含变量值，保留着有助于排查。
"""

from __future__ import annotations

import inspect
import logging
import sys
from typing import Any

from loguru import logger

from app.core.request_context import current_request_id

REQUEST_ID_KEY = "request_id"
"""``record["extra"]`` 里的字段名，与 ``LOG_FORMAT`` 中的 ``{extra[request_id]}`` 对应。"""

LOG_FORMAT = (
    "<green>{time:YYYY-MM-DD HH:mm:ss.SSS}</green> | "
    "<level>{level: <8}</level> | "
    "<cyan>req={extra[request_id]}</cyan> | "
    "<dim>{name}:{function}:{line}</dim> - <level>{message}</level>"
)

#: uvicorn 的三个 logger 都有私有 handler 且 ``propagate=False``，
#: 不显式接管的话，启动日志与访问日志会绕过 loguru——而访问日志恰恰是
#: "哪个请求慢、哪个请求 4xx"的第一手材料，它没有 request_id 就串不起来。
UVICORN_LOGGERS = ("uvicorn", "uvicorn.error", "uvicorn.access")


class InterceptHandler(logging.Handler):
    """标准库 ``logging`` → loguru 的转接器。

    三个细节都按 loguru 官方推荐来，各有原因：

    - ``logger.opt(depth=...)``：不修正 depth 的话，每条日志的"哪一行打的"
      都会指到本文件（转接器自己）。
      **depth 的起算与循环条件是对的，不是随便写的**：起点是 emit 自己的帧
      （depth=0），先把 emit 这一层让出去，然后一路跳过 logging 内部的若干帧
      （``Handler.handle`` → ``Logger.callHandlers`` → ``Logger.handle`` …
      它们都在 ``logging.__file__`` 里），直到踩到真正调用 ``logger.xxx`` 的那一行。
      实测过只跳过 logging 帧、起算写死的版本：那样会停在 ``callHandlers``，
      日志里整齐地写着 ``logging:callHandlers:1762``——看着像日志，其实是把
      "哪里打的"这个信息整个丢了。
    - ``logger.patch`` 把 ``{name}`` 换成标准库那边的 logger 名字：不换的话
      loguru 会从调用帧的 ``__name__`` 推一个，多数时候与 ``getLogger(__name__)``
      恰好相同，但 uvicorn 那几个（``logging.getLogger("uvicorn.access")``、
      ``propagate=False`` 的私有 logger）会推成 ``uvicorn.protocols.http.h11_impl``
      之类的内部模块名——而访问日志恰恰是靠 "uvicorn.access" 这个词被搜出来的。
    - ``logger.level(record.levelname)`` 取不到时退回数字 level：第三方库可能
      用了 loguru 不认识的级别名。
    - 传 ``exception=record.exc_info``：不传的话 ``logger.exception`` 打出来的
      堆栈会在转接处断掉。
    """

    def emit(self, record: logging.LogRecord) -> None:
        try:
            level: str | int = logger.level(record.levelname).name
        except ValueError:
            level = record.levelno

        frame, depth = inspect.currentframe(), 0
        while frame is not None and (depth == 0 or frame.f_code.co_filename == logging.__file__):
            frame = frame.f_back
            depth += 1

        logger.opt(depth=depth, exception=record.exc_info).patch(
            lambda patched: patched.update(name=record.name)
        ).log(level, record.getMessage())


def _patch(record: dict[str, Any]) -> None:
    """给每条记录补上 request_id。

    用 ``setdefault``：调用方若显式 ``logger.bind(request_id=...)``（比如后台任务
    想把评测运行的 id 放进来），应当以它为准，而不是被上下文里的空值顶掉。
    """
    record["extra"].setdefault(REQUEST_ID_KEY, current_request_id() or "-")


def setup_logging(level: str = "INFO") -> None:
    """配置 loguru，并把标准库 logging 接到它上面。

    **可以重复调用**：``logger.remove()`` 先清空已有 sink，所以重复执行得到的是
    同一份配置，而不是每调一次多一个 sink（多 sink 的表现是每条日志打印 N 遍，
    每加一个应用实例多一遍——测试里最容易撞上）。
    """
    logger.remove()
    logger.add(
        sys.stderr,
        level=(level or "INFO").upper(),
        format=LOG_FORMAT,
        backtrace=True,
        # 见模块说明：这一项开着会把局部变量（口令、连接串）写进日志
        diagnose=False,
    )
    logger.configure(patcher=_patch)

    # force=True 才替换掉已有的 root handler（uvicorn 启动时已经装过一套）。
    #
    # 级别取与 sink 相同的值，而不是教科书里的 level=0：
    # root 的级别决定的是"第三方库的 DEBUG 记录要不要被创建"，而它们一旦被创建，
    # 就会顺着传播链进到**所有** root handler 里——包括 pytest 的 caplog。
    # 用 0 的后果是测试失败时报表里多出整屏 httpx / elasticsearch 的 DEBUG 报文，
    # 把真正的断言埋掉（这场面在阶段 11 验收时真的遇到了）。反正 loguru 的 sink
    # 自己也会按级别再滤一遍，让 root 提前拦住更省事。
    level_name = (level or "INFO").upper()
    logging.basicConfig(handlers=[InterceptHandler()], level=level_name, force=True)

    for name in UVICORN_LOGGERS:
        uvicorn_logger = logging.getLogger(name)
        # 换掉它自己的 handler 并把 propagate 打开，让记录走到 root 的转接器；
        # uvicorn 默认 propagate=False，光换 handler 是不够的
        uvicorn_logger.handlers = [InterceptHandler()]
        uvicorn_logger.propagate = False
