"""统一异常体系（TDD 12.1 错误码）。

业务代码只抛 AppException 子类，全局 handler（core.handlers）统一转成
``{"code": ..., "message": ..., "data": null}``，HTTP 状态码按类别映射：
400→400xx、401→401xx、403→403xx、404→404xx、500→500xx。

约定：``message`` 是面向调用方的中文提示，禁止写入口令、令牌、SQL 原文等敏感内容；
排查细节走日志（见 core.handlers），不进响应体。
"""

from __future__ import annotations


class AppException(Exception):
    """业务异常基类。

    子类通过类属性声明 ``code`` 与 ``http_status``，实例化时可覆盖 message。
    """

    code: int = 50000
    http_status: int = 500
    message: str = "服务内部错误"

    def __init__(self, message: str | None = None, *, code: int | None = None) -> None:
        if message is not None:
            self.message = message
        if code is not None:
            self.code = code
        super().__init__(self.message)


# ---- 400xx 参数校验 ----


class ValidationError(AppException):
    """入参不合法：缺字段、超长、类型不符、非法枚举值等。"""

    code = 40000
    http_status = 400
    message = "参数校验失败"


# ---- 401xx 认证 ----


class AuthError(AppException):
    """未认证或凭据不正确。"""

    code = 40100
    http_status = 401
    message = "认证失败"


class TokenExpiredError(AuthError):
    """令牌已过期，前端应凭 refresh_token 换新（FR-AUTH-002）。"""

    code = 40101
    message = "令牌已过期"


class InvalidTokenError(AuthError):
    """令牌签名错误、类型不符、被篡改或对应用户已不存在。"""

    code = 40102
    message = "令牌无效"


# ---- 403xx 权限 ----


class PermissionDeniedError(AppException):
    """已认证但操作被拒。

    三种情形共用 403/40300：

    - 角色不足（FR-AUTH-005，如 viewer 访问 admin 接口）；
    - 对自身执行被禁止的操作（BR-AUTH-003，管理员不得修改或禁用本人）——
      语义上是「该主体无权对自身施加此操作」，而非入参有误，故不归入 400xx；
    - 账号被禁用（FR-AUTH-001）。
    """

    code = 40300
    http_status = 403
    message = "无权访问该资源"


# ---- 404xx 资源 ----


class NotFoundError(AppException):
    """资源不存在。"""

    code = 40400
    http_status = 404
    message = "资源不存在"


# ---- 402xx 查询（阶段 5：SQL 生成与执行流程） ----
#
# TDD 12.1 给出了这一族的业务码，却没给 HTTP 映射——400/401/403/404/500 五族都有映射
# 规则，唯独 402xx 缺席。这里按同样的原则补齐，并已同步登记进 TDD 12.1：
# 调用方自己能纠正的 → 4xx；服务端或上游故障 → 5xx。
#
# 注意 SSE 场景下的分工（TDD 12.2）：流一旦开始推送，这些异常就不再变成 HTTP 状态码
# （响应头早已发出、改不了了），而是转成 type=error 事件；HTTP 码只对"开流之前"的失败生效。


class QueryError(AppException):
    """查询请求本身不可用：问题为空、超长、无法解析。"""

    code = 40200
    http_status = 400
    message = "查询请求不合法"


class SQLGenerationError(AppException):
    """生成 SQL 失败：模型答复了，但答复的不是能用的 SQL。

    与 LLMServiceError 的分工：本条是"模型答了、但答的不是我们能用的"（空串、
    纯解释文字、解析不出东西）；LLMServiceError 是"模型根本没答上来"（超时、
    限流、连接失败、鉴权失败），后者归 500xx。
    """

    code = 40201
    http_status = 500
    message = "SQL 生成失败"


class SQLExecutionError(AppException):
    """数仓执行失败：语法预检通过，但真正跑起来报错（缺表、类型不符等）。"""

    code = 40202
    http_status = 500
    message = "SQL 执行失败"


class SQLSecurityError(AppException):
    """SQL 未通过只读安全校验（BR-SQL-001、TDD 13、ADR-008）。

    与 40300 PermissionDeniedError 的区别：那条是"你这个角色不许做这件事"，
    这条是"这条语句本身就不该被执行"。两者都可能落 HTTP 403，业务码不同。
    """

    code = 40203
    http_status = 403
    message = "SQL 未通过安全校验"


class QueryTimeoutError(AppException):
    """查询超过 max_execution_time_ms（BR-SQL-003）。"""

    code = 40204
    http_status = 504
    message = "查询超时"


# ---- 500xx 服务 ----


class ServiceError(AppException):
    """服务端自身故障，对外不暴露内部细节。"""

    code = 50000
    http_status = 500
    message = "服务内部错误"


class LLMServiceError(ServiceError):
    """LLM 上游不可用：超时、限流、连接失败、鉴权失败（TDD 12.1）。

    单独立一个子类而不是复用 ServiceError，因为它指向的是**外部**故障：
    排查要先看模型服务的配额与网络，而不是看本服务。业务码区分开，
    日志与告警才能按类归因。
    """

    code = 50001
    message = "LLM 服务不可用"
