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


# ---- 500xx 服务 ----


class ServiceError(AppException):
    """服务端自身故障，对外不暴露内部细节。"""

    code = 50000
    http_status = 500
    message = "服务内部错误"


# 402xx 查询族（40200 QueryError / SQLGeneration / SQLExecution / SQLSecurity / Timeout）
# 随 SQL 生成与执行流程在阶段 5 引入——TDD 12.1 未给出 402xx 到 HTTP 状态码的映射，
# 待该阶段与文档一并确定后再补，避免此处臆造映射。
