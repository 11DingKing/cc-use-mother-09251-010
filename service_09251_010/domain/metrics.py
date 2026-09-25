"""指标口径引擎：纯函数、可复算。

同一组输入（窗口、证据、口径版本）必然得到同一组输出；
引擎不读取时钟、随机源或任何外部状态。
"""
from __future__ import annotations

import math
import re
from statistics import fmean

from .errors import ValidationError
from .windows import HolidayWindow

ENGINES = ("count", "sum", "avg", "max", "min", "percentile", "ratio", "utilization")
DIRECTIONS = ("higher_better", "lower_better")
_VALUE_ENGINES = ("sum", "avg", "max", "min", "percentile")

_NAME_RE = re.compile(r"^[a-z][a-z0-9_]{1,40}$")


def metric_key(definition: dict) -> str:
    return f"{definition['name']}@v{definition['version']}"


def _is_text(value: object) -> bool:
    return isinstance(value, str) and bool(value.strip())


def validate_definition(raw: object, *, evidence_kinds: tuple[str, ...]) -> dict:
    """校验并归一化指标口径；归一化结果参与口径哈希与输入指纹。"""
    if not isinstance(raw, dict):
        raise ValidationError("指标口径必须是 JSON 对象")
    name = raw.get("name")
    if not isinstance(name, str) or not _NAME_RE.match(name):
        raise ValidationError("指标名称必须是小写字母开头的 snake_case（2-41 位）")
    engine = raw.get("engine")
    if engine not in ENGINES:
        raise ValidationError(f"不支持的计算引擎: {engine!r}", details={"allowed": list(ENGINES)})
    source_kind = raw.get("source_kind")
    if source_kind not in evidence_kinds:
        raise ValidationError(
            f"未知的证据类型: {source_kind!r}", details={"allowed": list(evidence_kinds)}
        )
    bucket_seconds = raw.get("bucket_seconds")
    if (
        not isinstance(bucket_seconds, int)
        or isinstance(bucket_seconds, bool)
        or not 60 <= bucket_seconds <= 86400
    ):
        raise ValidationError("bucket_seconds 必须是 60 到 86400 之间的整数秒")
    direction = raw.get("direction")
    if direction not in DIRECTIONS:
        raise ValidationError(
            "direction 必须是 higher_better 或 lower_better", details={"allowed": list(DIRECTIONS)}
        )
    version = raw.get("version")
    if version is not None and (
        not isinstance(version, int) or isinstance(version, bool) or version < 1
    ):
        raise ValidationError("version 必须是正整数")
    event_type = raw.get("event_type")
    if event_type is not None and not _is_text(event_type):
        raise ValidationError("event_type 必须是非空字符串")

    value_field = raw.get("value_field")
    percentile = raw.get("percentile")
    numerator_type = raw.get("numerator_type")
    denominator_type = raw.get("denominator_type")
    capacity_field = raw.get("capacity_field")
    used_field = raw.get("used_field")

    if engine in _VALUE_ENGINES and not _is_text(value_field):
        raise ValidationError(f"引擎 {engine} 需要 value_field 指向负载中的数值字段")
    if engine == "percentile":
        if (
            not isinstance(percentile, (int, float))
            or isinstance(percentile, bool)
            or not 0 < float(percentile) <= 100
        ):
            raise ValidationError("percentile 必须在 (0, 100] 区间")
        percentile = float(percentile)
    else:
        percentile = None
    if engine == "ratio":
        if not (_is_text(numerator_type) and _is_text(denominator_type)):
            raise ValidationError("ratio 引擎需要 numerator_type 与 denominator_type")
    if engine == "utilization":
        if not (_is_text(capacity_field) and _is_text(used_field)):
            raise ValidationError("utilization 引擎需要 capacity_field 与 used_field")

    return {
        "name": name,
        "version": version,
        "engine": engine,
        "source_kind": source_kind,
        "event_type": event_type,
        "value_field": value_field,
        "percentile": percentile,
        "numerator_type": numerator_type,
        "denominator_type": denominator_type,
        "capacity_field": capacity_field,
        "used_field": used_field,
        "bucket_seconds": bucket_seconds,
        "direction": direction,
    }


def percentile_value(values: list[float], p: float) -> float | None:
    """线性插值分位数（与 numpy 默认方法一致），确定性输出。"""
    ordered = sorted(values)
    count = len(ordered)
    if count == 0:
        return None
    rank = (p / 100.0) * (count - 1)
    low = math.floor(rank)
    high = math.ceil(rank)
    if low == high:
        return ordered[low]
    return ordered[low] + (ordered[high] - ordered[low]) * (rank - low)


def _matches(definition: dict, item: object) -> bool:
    if item.kind != definition["source_kind"]:
        return False
    event_type = definition.get("event_type")
    if event_type and item.payload.get("type") != event_type:
        return False
    return True


def _number(value: object) -> float | None:
    if isinstance(value, bool):
        return None
    if isinstance(value, (int, float)):
        return float(value)
    return None


class _BucketAcc:
    """单个时间桶的累加器。"""

    __slots__ = ("values", "count", "numerator", "denominator", "used", "capacity", "snapshots")

    def __init__(self) -> None:
        self.values: list[float] = []
        self.count = 0
        self.numerator = 0
        self.denominator = 0
        self.used = 0.0
        self.capacity = 0.0
        self.snapshots = 0

    def aggregate(self, definition: dict) -> tuple[float | int | None, int]:
        engine = definition["engine"]
        if engine == "count":
            return self.count, self.count
        if engine == "ratio":
            samples = self.numerator + self.denominator
            return (self.numerator / self.denominator if self.denominator else None), samples
        if engine == "utilization":
            return (
                self.used / self.capacity if self.capacity > 0 else None
            ), self.snapshots
        if not self.values:
            return None, 0
        if engine == "sum":
            return sum(self.values), len(self.values)
        if engine == "avg":
            return fmean(self.values), len(self.values)
        if engine == "max":
            return max(self.values), len(self.values)
        if engine == "min":
            return min(self.values), len(self.values)
        return percentile_value(self.values, definition["percentile"]), len(self.values)


def compute_series(definition: dict, items: list, window: HolidayWindow) -> dict:
    """在单个窗口内按时间桶聚合，返回分桶序列与窗口总量。"""
    bucket_seconds = definition["bucket_seconds"]
    buckets = window.buckets(bucket_seconds)
    engine = definition["engine"]
    start = window.start_utc()
    accumulators = [_BucketAcc() for _ in buckets]

    for item in items:
        if not _matches(definition, item):
            continue
        offset = (item.occurred_at - start).total_seconds()
        index = int(offset // bucket_seconds)
        if index < 0 or index >= len(accumulators):
            continue
        acc = accumulators[index]
        payload = item.payload
        if engine == "count":
            acc.count += 1
        elif engine == "ratio":
            event = payload.get("type")
            if event == definition["numerator_type"]:
                acc.numerator += 1
            if event == definition["denominator_type"]:
                acc.denominator += 1
        elif engine == "utilization":
            used = _number(payload.get(definition["used_field"]))
            capacity = _number(payload.get(definition["capacity_field"]))
            if used is not None and capacity is not None:
                acc.used += used
                acc.capacity += capacity
                acc.snapshots += 1
        else:
            number = _number(payload.get(definition["value_field"]))
            if number is not None:
                acc.values.append(number)

    series_buckets = []
    total = _BucketAcc()
    for bucket, acc in zip(buckets, accumulators):
        value, samples = acc.aggregate(definition)
        series_buckets.append({**bucket.to_dict(), "value": value, "samples": samples})
        total.values.extend(acc.values)
        total.count += acc.count
        total.numerator += acc.numerator
        total.denominator += acc.denominator
        total.used += acc.used
        total.capacity += acc.capacity
        total.snapshots += acc.snapshots
    total_value, total_samples = total.aggregate(definition)
    return {"buckets": series_buckets, "total": {"value": total_value, "samples": total_samples}}


def compare_series(holiday: dict, baseline: dict, direction: str) -> list[dict]:
    """按“本地时刻”对齐假期与基线桶，标注哪些时段改善——回答“改善了哪些时段”。"""
    by_time_of_day: dict[str, list[float]] = {}
    for bucket in baseline["buckets"]:
        if bucket["value"] is not None:
            by_time_of_day.setdefault(bucket["label"][11:16], []).append(bucket["value"])
    entries = []
    for bucket in holiday["buckets"]:
        holiday_value = bucket["value"]
        if holiday_value is None:
            continue
        candidates = by_time_of_day.get(bucket["label"][11:16])
        if not candidates:
            continue
        baseline_value = fmean(candidates)
        delta = holiday_value - baseline_value
        improved = delta < 0 if direction == "lower_better" else delta > 0
        entries.append({
            "bucket": bucket["index"],
            "label": bucket["label"],
            "holiday_value": holiday_value,
            "baseline_value": baseline_value,
            "delta": delta,
            "improved": improved,
        })
    return entries


def build_metric_result(
    definition: dict,
    items: list,
    window: HolidayWindow,
    baseline: HolidayWindow | None,
) -> dict:
    """单个口径的完整结果：假期分桶 + 基线分桶 + 时段改善对照。"""
    result = {
        "metric": metric_key(definition),
        "engine": definition["engine"],
        "direction": definition["direction"],
        "bucket_seconds": definition["bucket_seconds"],
        "holiday": compute_series(definition, items, window),
        "baseline": None,
        "comparison": None,
    }
    if baseline is not None:
        baseline_series = compute_series(definition, items, baseline)
        result["baseline"] = baseline_series
        result["comparison"] = compare_series(result["holiday"], baseline_series, definition["direction"])
    return result
