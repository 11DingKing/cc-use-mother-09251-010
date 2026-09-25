"""领域实体（纯数据结构，不依赖数据库）。"""
from __future__ import annotations

from dataclasses import asdict, dataclass, field
from datetime import datetime
from typing import Any

from .enums import EvidenceKind


@dataclass(frozen=True)
class HolidayWindow:
    name: str
    tz_name: str
    start_utc: datetime
    end_utc: datetime

    def contains(self, instant_utc: datetime) -> bool:
        return self.start_utc <= instant_utc < self.end_utc


@dataclass
class Review:
    id: str
    name: str
    tz_name: str
    window_start: datetime
    window_end: datetime
    created_by: str
    created_at: datetime


@dataclass
class Evidence:
    id: str
    review_id: str
    kind: str
    source_ref: str
    occurred_at: datetime
    payload: dict[str, Any]
    fingerprint: str
    received_at: datetime
    # 同一复盘内若提交重复内容，指向首次入库的证据
    duplicate_of: str | None = None
    # 库内单调序号，续算时作为游标
    seq: int = 0

    def is_duplicate(self) -> bool:
        return self.duplicate_of is not None


@dataclass(frozen=True)
class MetricSpec:
    """可复算的指标口径定义。

    metric_key 选择指标算法，config 承载口径参数（如事件类型、移动资源标识）。
    """

    spec_id: str
    metric_key: str
    label: str
    bucket: str = "hour"
    config: dict[str, Any] = field(default_factory=dict)


@dataclass
class MetricResult:
    spec_id: str
    metric_key: str
    label: str
    bucket: str
    total: float
    buckets: dict[str, float]
    window: dict[str, str]

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass
class ReviewVersion:
    id: str
    review_id: str
    seq_no: int
    status: str
    window: dict[str, Any]
    metric_specs: list[dict[str, Any]]
    created_at: datetime
    results: list[dict[str, Any]] = field(default_factory=list)
    inputs_fingerprint: str | None = None
    result_fingerprint: str | None = None
    signed_at: datetime | None = None
    signed_by: str | None = None
    superseded_by: str | None = None
    # 建版时冻结的证据 id 列表（按库内序号排序）；计算只使用这批输入
    input_evidence_ids: list[str] = field(default_factory=list)
    # 续算支持：快照证据中已纳入计算的最大序号
    computed_cursor: int = 0

    @property
    def is_immutable(self) -> bool:
        # 已签发或已被新版替代的版本，结果与指纹均永久冻结
        return self.status in ("signed", "superseded")


@dataclass
class Challenge:
    id: str
    review_id: str
    version_id: str
    metric_key: str | None
    reason: str
    raised_by: str
    status: str
    created_at: datetime
    resolved_at: datetime | None = None
    resolution: str | None = None
