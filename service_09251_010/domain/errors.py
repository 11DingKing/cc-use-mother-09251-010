"""领域错误：携带稳定错误码与建议的 HTTP 状态，供接口边界统一映射。"""
from __future__ import annotations


class DomainError(Exception):
    code = "domain_error"
    http_status = 400

    def __init__(self, message: str, *, details: dict | None = None) -> None:
        super().__init__(message)
        self.message = message
        self.details = details or {}


class BadRequestError(DomainError):
    code = "bad_request"
    http_status = 400


class UnauthorizedError(DomainError):
    code = "unauthorized"
    http_status = 401


class ForbiddenError(DomainError):
    code = "forbidden"
    http_status = 403


class NotFoundError(DomainError):
    code = "not_found"
    http_status = 404


class ConflictError(DomainError):
    code = "conflict"
    http_status = 409


class ValidationError(DomainError):
    code = "validation_error"
    http_status = 422


class ComputationInterrupted(DomainError):
    """计算被中断：运行已落库，可通过续算完成，指纹保持不变。"""

    code = "computation_interrupted"
    http_status = 500

    def __init__(self, run_id: str, cause: str) -> None:
        super().__init__(
            f"计算运行 {run_id} 被中断: {cause}",
            details={"run_id": run_id, "cause": cause},
        )
        self.run_id = run_id
