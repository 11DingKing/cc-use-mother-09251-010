"""领域模型：复盘、证据、指标口径、计算运行、复盘版本与复核记录。"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime

from .windows import HolidayWindow, format_instant

#: 证据类型：计划版本 / 现场事件 / 容量快照 / 公众查询 / 救援记录
EVIDENCE_KINDS = (
    "plan_version",
    "field_event",
    "capacity_snapshot",
    "public_query",
    "rescue_record",
)

RUN_RUNNING = "running"
RUN_INTERRUPTED = "interrupted"
RUN_DONE = "done"

VERSION_DRAFT = "draft"
VERSION_ISSUED = "issued"

RECHECK_MATCH = "match"
RECHECK_MISMATCH = "mismatch"


@dataclass
class MetricDefinition:
    """一条带版本的指标口径；definition 为归一化后的完整口径文本。"""

    id: str
    definition: dict
    definition_hash: str
    created_at: str
    created_by: str

    @property
    def key(self) -> str:
        return f"{self.definition['name']}@v{self.definition['version']}"

    def snapshot(self) -> dict:
        """随计算运行冻结的口径快照，复核时按快照重算而非按现值。"""
        return {
            "id": self.id,
            "definition": self.definition,
            "definition_hash": self.definition_hash,
        }

    def to_dict(self) -> dict:
        return {
            "id": self.id,
            "key": self.key,
            "definition": self.definition,
            "definition_hash": self.definition_hash,
            "created_at": self.created_at,
            "created_by": self.created_by,
        }


@dataclass
class Review:
    id: str
    region: str
    name: str
    window: HolidayWindow
    baseline: HolidayWindow | None
    metric_def_ids: list[str]
    created_at: str
    created_by: str

    def to_dict(self) -> dict:
        return {
            "id": self.id,
            "region": self.region,
            "name": self.name,
            "window": self.window.to_spec(),
            "baseline": self.baseline.to_spec() if self.baseline else None,
            "metric_def_ids": list(self.metric_def_ids),
            "created_at": self.created_at,
            "created_by": self.created_by,
        }


@dataclass
class Evidence:
    id: str
    review_id: str
    kind: str
    source: str
    external_id: str
    occurred_at: datetime
    ingested_at: datetime
    payload: dict
    content_hash: str

    def to_dict(self) -> dict:
        return {
            "id": self.id,
            "review_id": self.review_id,
            "kind": self.kind,
            "source": self.source,
            "external_id": self.external_id,
            "occurred_at": format_instant(self.occurred_at),
            "ingested_at": format_instant(self.ingested_at),
            "payload": self.payload,
            "content_hash": self.content_hash,
        }


@dataclass
class CalculationRun:
    """一次计算运行：输入清单与口径在创建时冻结，中断后按原输入续算。"""

    id: str
    review_id: str
    status: str
    fingerprint: str
    manifest: list[dict]
    metric_defs: list[dict]
    window_spec: dict
    baseline_spec: dict | None
    error: str | None
    created_at: str
    finished_at: str | None

    def to_dict(self) -> dict:
        return {
            "id": self.id,
            "review_id": self.review_id,
            "status": self.status,
            "fingerprint": self.fingerprint,
            "error": self.error,
            "created_at": self.created_at,
            "finished_at": self.finished_at,
            "manifest_count": len(self.manifest),
            "metric_keys": [
                f"{entry['definition']['name']}@v{entry['definition']['version']}"
                for entry in self.metric_defs
            ],
        }


@dataclass
class CalculationStep:
    """一个指标一步：落库即checkpoint，续算时跳过已完成步骤。"""

    run_id: str
    metric_key: str
    status: str
    result: dict
    fingerprint: str
    computed_at: str


@dataclass
class ReviewVersion:
    """一个复盘版本：计算结果的不可变快照；签发后结论冻结。"""

    id: str
    review_id: str
    version_no: int
    run_id: str
    status: str
    fingerprint: str
    manifest: list[dict]
    metric_defs: list[dict]
    results: dict
    window_spec: dict
    baseline_spec: dict | None
    created_at: str
    issued_at: str | None
    issued_by: str | None
    issued_seq: int | None

    def summary_dict(self) -> dict:
        return {
            "version_no": self.version_no,
            "status": self.status,
            "fingerprint": self.fingerprint,
            "run_id": self.run_id,
            "created_at": self.created_at,
            "issued_at": self.issued_at,
            "issued_by": self.issued_by,
            "issued_seq": self.issued_seq,
            "result_metrics": sorted(self.results.keys()),
        }

    def detail_dict(self) -> dict:
        detail = self.summary_dict()
        detail.update({
            "id": self.id,
            "review_id": self.review_id,
            "window": self.window_spec,
            "baseline": self.baseline_spec,
            "manifest": self.manifest,
            "metric_defs": self.metric_defs,
            "results": self.results,
        })
        return detail


@dataclass
class Recheck:
    """一次复核：按版本冻结的输入重算并比对指纹与结果。"""

    id: str
    version_id: str
    rechecked_by: str
    result: str
    details: dict
    created_at: str

    def to_dict(self) -> dict:
        return {
            "id": self.id,
            "version_id": self.version_id,
            "rechecked_by": self.rechecked_by,
            "result": self.result,
            "details": self.details,
            "created_at": self.created_at,
        }
