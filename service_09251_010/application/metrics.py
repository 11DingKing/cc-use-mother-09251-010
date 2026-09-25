"""指标引擎：口径注册表 + 纯函数聚合。

指标口径 = 已注册的 metric_key（算法）+ 该复盘版本冻结的 config（参数）。
算法可通过 register_metric 扩展；同一份输入与口径在任何机器上重算结果一致。

约定的证据 payload 字段：
- field_event: {"event_type": "mobile_charging"|"queue_wait", "wait_minutes": float, ...}
- capacity_snapshot: {"station_id", "queue_length", "available_chargers", "total_chargers"}
- public_query: {"channel", "response_seconds", ...}（含个体字段，需授权）
- rescue_record: {"response_seconds", ...}（含个体字段，需授权）

config 中可放 {"where": {"键": "值"}} 对 payload 做精确匹配过滤。
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Callable

from ..domain import windows
from ..domain.entities import Evidence, MetricResult, MetricSpec
from ..domain.enums import EvidenceKind

ValueExtractor = Callable[[dict[str, Any], dict[str, Any]], float | None]
AggregatorKind = str  # count | sum | avg | max | p95


@dataclass(frozen=True)
class MetricDefinition:
    key: str
    label: str
    evidence_kind: str
    aggregator: AggregatorKind
    extract: ValueExtractor
    default_bucket: str = "hour"


_REGISTRY: dict[str, MetricDefinition] = {}


class MetricError(KeyError):
    """引用了未注册的指标算法。"""


def register_metric(definition: MetricDefinition) -> None:
    _REGISTRY[definition.key] = definition


def get_definition(key: str) -> MetricDefinition:
    try:
        return _REGISTRY[key]
    except KeyError:
        raise MetricError(f"未注册的指标口径: {key}") from None


def metric_keys() -> list[str]:
    return sorted(_REGISTRY)


# ---- 值提取器 -------------------------------------------------------------

def _field(name: str) -> ValueExtractor:
    def extract(payload: dict[str, Any], config: dict[str, Any]) -> float | None:
        value = payload.get(name)
        return float(value) if value is not None else None

    return extract


def _typed_event(type_name: str, value_field: str) -> ValueExtractor:
    def extract(payload: dict[str, Any], config: dict[str, Any]) -> float | None:
        if payload.get("event_type") != type_name:
            return None
        value = payload.get(value_field)
        return float(value) if value is not None else None

    return extract


def _event_count(type_name: str) -> ValueExtractor:
    def extract(payload: dict[str, Any], config: dict[str, Any]) -> float | None:
        return 1.0 if payload.get("event_type") == type_name else None

    return extract


def _availability(payload: dict[str, Any], config: dict[str, Any]) -> float | None:
    total = payload.get("total_chargers")
    available = payload.get("available_chargers")
    if total in (None, 0) or available is None:
        return None
    return float(available) / float(total)


def _rescue_minutes(payload: dict[str, Any], config: dict[str, Any]) -> float | None:
    seconds = payload.get("response_seconds", payload.get("arrival_seconds"))
    return float(seconds) / 60.0 if seconds is not None else None


# ---- 聚合 -----------------------------------------------------------------

def _aggregate(values: list[float], kind: AggregatorKind) -> float | None:
    if kind == "count":
        return float(len(values))
    if not values:
        return None
    if kind == "sum":
        return float(sum(values))
    if kind == "avg":
        return sum(values) / len(values)
    if kind == "max":
        return float(max(values))
    if kind == "p95":
        return _percentile(values, 95)
    raise MetricError(f"不支持的聚合方式: {kind}")


# 增量累加器：分批处理证据时保存中间态，任务中断后可从游标继续。
# 中间态统一为 {"<bucket_label>": [值...]}，可直接 JSON 持久化；
# p95 需要保留全部值，其他聚合最终只做一次归并。
def accumulator_init() -> dict[str, list[float]]:
    return {}


def accumulator_add(data: dict[str, list[float]], bucket: str, values: list[float]) -> None:
    data.setdefault(bucket, []).extend(float(v) for v in values)


def accumulator_value(values: list[float], kind: AggregatorKind) -> float | None:
    return _aggregate(values, kind)


def _percentile(values: list[float], pct: float) -> float:
    """最近秩法（nearest-rank）百分位，对输入顺序不敏感。"""
    import math

    ordered = sorted(values)
    index = max(1, math.ceil(pct / 100.0 * len(ordered)))
    return float(ordered[index - 1])


def _matches_where(payload: dict[str, Any], where: dict[str, Any]) -> bool:
    return all(payload.get(k) == v for k, v in where.items())


# ---- 计算 -----------------------------------------------------------------

def _resolve(spec: MetricSpec) -> tuple[MetricDefinition, dict[str, Any], str]:
    definition = get_definition(spec.metric_key)
    where = spec.config.get("where", {})
    bucket_unit = spec.bucket or definition.default_bucket
    return definition, where, bucket_unit


def collect_into(
    state: dict[str, list[float]],
    spec: MetricSpec,
    evidence: list[Evidence],
    window_start,
    window_end,
    tz_name: str,
) -> None:
    """把一批证据收集进可持久化的中间态（供续算）。"""
    definition, where, bucket_unit = _resolve(spec)
    for ev in evidence:
        if ev.kind != definition.evidence_kind:
            continue
        if not _in_range(ev.occurred_at, window_start, window_end):
            continue
        if where and not _matches_where(ev.payload, where):
            continue
        value = definition.extract(ev.payload, spec.config)
        if value is None:
            continue
        key = windows.bucket_key(ev.occurred_at, tz_name, bucket_unit)
        accumulator_add(state, key, [value])


def finalize(
    spec: MetricSpec,
    state: dict[str, list[float]],
    window_start,
    window_end,
    tz_name: str,
) -> MetricResult:
    definition, _where, bucket_unit = _resolve(spec)
    buckets: dict[str, float] = {}
    all_values: list[float] = []
    for label, _b_start, _b_end in windows.iter_buckets(
        window_start, window_end, tz_name, bucket_unit
    ):
        values = state.get(label, [])
        bucket_value = _aggregate(values, definition.aggregator)
        # 空桶：计数类补 0，其他类输出 None（序列化为 null）
        buckets[label] = (
            0.0 if bucket_value is None and definition.aggregator == "count" else bucket_value
        )
        all_values.extend(values)

    total = _aggregate(all_values, definition.aggregator)
    if total is None and definition.aggregator == "count":
        total = 0.0

    return MetricResult(
        spec_id=spec.spec_id,
        metric_key=definition.key,
        label=spec.label or definition.label,
        bucket=bucket_unit,
        total=total,
        buckets=buckets,
        window={
            "start": window_start.isoformat(),
            "end": window_end.isoformat(),
            "tz": tz_name,
        },
    )


def compute(
    spec: MetricSpec,
    evidence: list[Evidence],
    window_start,
    window_end,
    tz_name: str,
) -> MetricResult:
    state = accumulator_init()
    collect_into(state, spec, evidence, window_start, window_end, tz_name)
    return finalize(spec, state, window_start, window_end, tz_name)


def _in_range(instant, start, end) -> bool:
    return start <= instant < end


# ---- 内置口径 --------------------------------------------------------------

register_metric(
    MetricDefinition(
        key="mobile_charging_dispatch_count",
        label="移动充电派出次数",
        evidence_kind=EvidenceKind.FIELD_EVENT,
        aggregator="count",
        extract=_event_count("mobile_charging"),
    )
)
register_metric(
    MetricDefinition(
        key="queue_wait_minutes_avg",
        label="排队等待平均时长（分钟）",
        evidence_kind=EvidenceKind.FIELD_EVENT,
        aggregator="avg",
        extract=_typed_event("queue_wait", "wait_minutes"),
    )
)
register_metric(
    MetricDefinition(
        key="queue_length_max",
        label="排队长度峰值",
        evidence_kind=EvidenceKind.CAPACITY_SNAPSHOT,
        aggregator="max",
        extract=_field("queue_length"),
    )
)
register_metric(
    MetricDefinition(
        key="charger_availability_avg",
        label="充电桩可用率",
        evidence_kind=EvidenceKind.CAPACITY_SNAPSHOT,
        aggregator="avg",
        extract=_availability,
    )
)
register_metric(
    MetricDefinition(
        key="public_query_count",
        label="公众查询量",
        evidence_kind=EvidenceKind.PUBLIC_QUERY,
        aggregator="count",
        extract=lambda p, c: 1.0,
    )
)
register_metric(
    MetricDefinition(
        key="query_response_seconds_p95",
        label="查询响应时长 P95（秒）",
        evidence_kind=EvidenceKind.PUBLIC_QUERY,
        aggregator="p95",
        extract=_field("response_seconds"),
    )
)
register_metric(
    MetricDefinition(
        key="rescue_response_minutes_avg",
        label="救援平均到场时长（分钟）",
        evidence_kind=EvidenceKind.RESCUE_RECORD,
        aggregator="avg",
        extract=_rescue_minutes,
    )
)
