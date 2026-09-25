"""操作者身份与个体信息（PII）访问控制。"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from ..domain.enums import PII_FIELDS, PII_KINDS, EvidenceKind


@dataclass(frozen=True)
class Actor:
    name: str
    roles: frozenset[str] = field(default_factory=frozenset)

    @classmethod
    def anonymous(cls) -> "Actor":
        return cls(name="anonymous", roles=frozenset())

    @property
    def is_authenticated(self) -> bool:
        return self.name != "anonymous"

    def can_view_pii(self) -> bool:
        return "admin" in self.roles or "read_pii" in self.roles

    def can_sign(self) -> bool:
        return "admin" in self.roles or "signer" in self.roles

    def can_manage_review(self) -> bool:
        return self.is_authenticated and ("admin" in self.roles or "analyst" in self.roles)

    def can_resolve_challenge(self) -> bool:
        return "admin" in self.roles or "analyst" in self.roles


def kind_has_pii(kind: str) -> bool:
    return kind in PII_KINDS


def redact_value(obj: Any, removed: list[str], path: str = "") -> Any:
    """递归剔除 PII 字段，记录被移除字段的路径。"""
    if isinstance(obj, dict):
        cleaned = {}
        for key, value in obj.items():
            key_path = f"{path}.{key}" if path else str(key)
            if key in PII_FIELDS:
                removed.append(key_path)
                continue
            cleaned[key] = redact_value(value, removed, key_path)
        return cleaned
    if isinstance(obj, list):
        return [redact_value(item, removed, f"{path}[]") for item in obj]
    return obj


def evidence_payload_for(kind: str, payload: dict[str, Any], actor: Actor):
    """按权限返回 (payload, 是否脱敏, 被剔除字段路径)。

    含个体信息的证据类别：
    - 有 read_pii/admin：返回完整原文；
    - 无授权：字段级剔除姓名、电话、车牌等标识字段，保留响应时长等服务指标，
      这样业务人员无需授权也能核对分时段数据，个体身份信息始终不外泄。
    """
    # 有个体信息授权：任何类别都返回完整原文
    if actor.can_view_pii():
        return payload, False, []
    # 无授权：字段级剔除标识字段；对个体类别标记为脱敏视图
    removed: list[str] = []
    cleaned = redact_value(payload, removed)
    return cleaned, kind in PII_KINDS or bool(removed), sorted(removed)
