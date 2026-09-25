"""访问控制：API 密钥认证、权限范围与个体信息脱敏。

个体信息（救援记录中的当事人、公众查询中的用户标识）只在主体持有
``pii:read`` 范围时原样返回，否则递归脱敏为 ``***``。
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Mapping

from ..domain.errors import ForbiddenError, UnauthorizedError, ValidationError

ALL_SCOPES = frozenset({
    "review:read",
    "review:write",
    "evidence:write",
    "metrics:write",
    "compute:run",
    "review:issue",
    "recheck:run",
    "export:read",
    "pii:read",
})

ROLE_SCOPES: dict[str, frozenset[str]] = {
    "admin": ALL_SCOPES,
    "analyst": ALL_SCOPES - {"review:issue", "pii:read"},
    "issuer": frozenset({"review:read", "review:issue", "recheck:run", "export:read"}),
    "viewer": frozenset({"review:read", "export:read"}),
    "auditor": frozenset({"review:read", "export:read", "pii:read"}),
}

#: 各证据类型中属于个体信息的负载字段（任意深度命中即脱敏）
PII_FIELDS: dict[str, frozenset[str]] = {
    "rescue_record": frozenset(
        {"person_name", "person_phone", "id_number", "vehicle_plate", "contact"}
    ),
    "public_query": frozenset({"user_id", "contact", "phone", "openid", "account"}),
}

REDACTED = "***"


@dataclass(frozen=True)
class Principal:
    name: str
    role: str
    scopes: frozenset[str]


def require(principal: Principal, scope: str) -> None:
    """要求主体持有指定权限范围，否则拒绝。"""
    if scope not in principal.scopes:
        raise ForbiddenError(
            f"缺少权限范围 {scope}",
            details={"required": scope, "principal": principal.name, "role": principal.role},
        )


def redact_payload(kind: str, payload: dict) -> dict:
    """按证据类型递归脱敏个体信息字段，返回新对象。"""
    fields = PII_FIELDS.get(kind)
    if not fields:
        return payload

    def walk(node: object) -> object:
        if isinstance(node, dict):
            return {key: (REDACTED if key in fields else walk(value)) for key, value in node.items()}
        if isinstance(node, list):
            return [walk(item) for item in node]
        return node

    return walk(payload)


class Authenticator:
    """API 密钥认证：密钥 → 主体（角色 + 权限范围）。"""

    def __init__(self, keys: Mapping[str, Mapping[str, object]]) -> None:
        if not keys:
            raise ValidationError("至少需要配置一个 API 密钥")
        self._principals: dict[str, Principal] = {}
        for api_key, spec in keys.items():
            role = str(spec.get("role") or "viewer")
            if role not in ROLE_SCOPES:
                raise ValidationError(f"未知角色: {role}", details={"allowed": sorted(ROLE_SCOPES)})
            raw_scopes = spec.get("scopes")
            scopes = frozenset(raw_scopes) if raw_scopes else ROLE_SCOPES[role]
            unknown = scopes - ALL_SCOPES
            if unknown:
                raise ValidationError(f"未知权限范围: {sorted(unknown)}")
            self._principals[str(api_key)] = Principal(
                name=str(spec.get("name") or role), role=role, scopes=scopes
            )

    def authenticate(self, api_key: str | None) -> Principal:
        if not api_key:
            raise UnauthorizedError("缺少 API 密钥（X-API-Key）")
        principal = self._principals.get(api_key)
        if principal is None:
            raise UnauthorizedError("无效的 API 密钥")
        return principal


#: 内置开发密钥，仅供本地调试；生产部署必须通过密钥文件注入。
DEFAULT_DEV_KEYS: dict[str, dict[str, str]] = {
    "dev-admin-key": {"name": "admin", "role": "admin"},
    "dev-analyst-key": {"name": "analyst", "role": "analyst"},
    "dev-issuer-key": {"name": "issuer", "role": "issuer"},
    "dev-viewer-key": {"name": "viewer", "role": "viewer"},
    "dev-auditor-key": {"name": "auditor", "role": "auditor"},
}
