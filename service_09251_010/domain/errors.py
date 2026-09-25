"""领域错误，携带稳定的机器可读错误码与 HTTP 状态。"""
from __future__ import annotations


class DomainError(Exception):
    status = 400
    code = "bad_request"

    def __init__(self, message: str, *, code: str | None = None, status: int | None = None):
        super().__init__(message)
        self.message = message
        if code is not None:
            self.code = code
        if status is not None:
            self.status = status

    def to_dict(self) -> dict:
        return {"error": self.code, "message": self.message}


class NotFoundError(DomainError):
    status = 404
    code = "not_found"


class ConflictError(DomainError):
    status = 409
    code = "conflict"


class PermissionDeniedError(DomainError):
    status = 403
    code = "permission_denied"


class ValidationError(DomainError):
    status = 422
    code = "validation_error"
