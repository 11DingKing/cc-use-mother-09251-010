"""领域枚举。"""
from __future__ import annotations

import enum


class StrEnum(str, enum.Enum):
    """取值即为字符串的枚举基类。"""

    def __str__(self) -> str:  # pragma: no cover - 简单委托
        return self.value


class EvidenceKind(StrEnum):
    """证据类别：计划版本、现场事件、容量快照、公众查询、救援记录。"""

    PLAN_VERSION = "plan_version"
    FIELD_EVENT = "field_event"
    CAPACITY_SNAPSHOT = "capacity_snapshot"
    PUBLIC_QUERY = "public_query"
    RESCUE_RECORD = "rescue_record"


class VersionStatus(StrEnum):
    DRAFT = "draft"
    COMPUTING = "computing"
    READY = "ready"
    SIGNED = "signed"
    SUPERSEDED = "superseded"


class ChallengeStatus(StrEnum):
    OPEN = "open"
    RESOLVED = "resolved"
    REJECTED = "rejected"


# 含个体信息、需要额外授权才能查看明细的证据类别
PII_KINDS = frozenset({EvidenceKind.RESCUE_RECORD, EvidenceKind.PUBLIC_QUERY})

# 各证据类别需要脱敏的字段名（递归按键名剔除）
PII_FIELDS = frozenset(
    {
        "contact_name",
        "contact_phone",
        "vehicle_plate",
        "user_name",
        "user_phone",
        "id_card",
    }
)
