"""指标口径引擎：分桶聚合、分位数、利用率与时段改善对照。"""
import unittest

from helpers import mk_evidence, queue_def

from service_09251_010.domain.metrics import (
    build_metric_result,
    compute_series,
    percentile_value,
    validate_definition,
)
from service_09251_010.domain.errors import ValidationError
from service_09251_010.domain.windows import HolidayWindow

WINDOW = HolidayWindow.from_spec(
    {"tz": "Asia/Shanghai", "start": "2026-10-01", "end": "2026-10-02"}
)
BASELINE = HolidayWindow.from_spec(
    {"tz": "Asia/Shanghai", "start": "2026-09-24", "end": "2026-09-25"}
)


def wait_item(ext, occurred_at, minutes):
    return mk_evidence(
        "field_event", occurred_at, {"type": "queue_wait_observed", "wait_minutes": minutes}, ext
    )


class PercentileTests(unittest.TestCase):
    def test_linear_interpolation(self):
        self.assertEqual(percentile_value([30.0, 40.0, 50.0], 95), 49.0)
        self.assertEqual(percentile_value([10.0], 95), 10.0)

    def test_empty(self):
        self.assertIsNone(percentile_value([], 95))


class ComputeSeriesTests(unittest.TestCase):
    def test_bucket_boundaries(self):
        definition = validate_definition(
            queue_def(), evidence_kinds=("field_event",)
        )
        definition["version"] = 1
        items = [
            wait_item("a", "2026-10-01T00:00:00+08:00", 10),  # 桶 0（含端点）
            wait_item("b", "2026-10-01T10:59:59+08:00", 30),  # 桶 10
            wait_item("c", "2026-10-02T00:00:00+08:00", 99),  # 窗口结束，排除
            wait_item("d", "2026-09-30T23:00:00+08:00", 99),  # 窗口之前，排除
        ]
        series = compute_series(definition, items, WINDOW)
        self.assertEqual(series["buckets"][0]["value"], 10.0)
        self.assertEqual(series["buckets"][10]["value"], 30.0)
        self.assertEqual(series["total"]["samples"], 2)
        values = {b["index"]: b["value"] for b in series["buckets"] if b["value"] is not None}
        self.assertNotIn(99.0, values.values())

    def test_empty_bucket_is_none(self):
        definition = validate_definition(queue_def(), evidence_kinds=("field_event",))
        series = compute_series(definition, [], WINDOW)
        self.assertTrue(all(b["value"] is None and b["samples"] == 0 for b in series["buckets"]))

    def test_utilization_sums_then_divides(self):
        definition = validate_definition(
            {
                "name": "util",
                "engine": "utilization",
                "source_kind": "capacity_snapshot",
                "capacity_field": "capacity_kw",
                "used_field": "used_kw",
                "bucket_seconds": 3600,
                "direction": "higher_better",
            },
            evidence_kinds=("capacity_snapshot",),
        )
        items = [
            mk_evidence("capacity_snapshot", "2026-10-01T10:05:00+08:00",
                        {"capacity_kw": 100, "used_kw": 50}, "s1"),
            mk_evidence("capacity_snapshot", "2026-10-01T10:35:00+08:00",
                        {"capacity_kw": 100, "used_kw": 25}, "s2"),
        ]
        series = compute_series(definition, items, WINDOW)
        self.assertAlmostEqual(series["buckets"][10]["value"], 0.375)
        self.assertEqual(series["buckets"][10]["samples"], 2)

    def test_ratio_engine(self):
        definition = validate_definition(
            {
                "name": "answer_ratio",
                "engine": "ratio",
                "source_kind": "field_event",
                "numerator_type": "query_answered",
                "denominator_type": "query_received",
                "bucket_seconds": 3600,
                "direction": "higher_better",
            },
            evidence_kinds=("field_event",),
        )
        items = [
            mk_evidence("field_event", "2026-10-01T10:01:00+08:00", {"type": "query_received"}, "q1"),
            mk_evidence("field_event", "2026-10-01T10:02:00+08:00", {"type": "query_received"}, "q2"),
            mk_evidence("field_event", "2026-10-01T10:03:00+08:00", {"type": "query_answered"}, "q3"),
        ]
        series = compute_series(definition, items, WINDOW)
        self.assertAlmostEqual(series["buckets"][10]["value"], 0.5)

    def test_event_type_filter(self):
        definition = validate_definition(queue_def(), evidence_kinds=("field_event",))
        items = [
            wait_item("a", "2026-10-01T10:00:00+08:00", 30),
            mk_evidence("field_event", "2026-10-01T10:05:00+08:00",
                        {"type": "info_published", "wait_minutes": 999}, "b"),
        ]
        series = compute_series(definition, items, WINDOW)
        self.assertEqual(series["buckets"][10]["value"], 30.0)


class ComparisonTests(unittest.TestCase):
    def test_improvement_flagged_by_time_of_day(self):
        definition = validate_definition(queue_def(), evidence_kinds=("field_event",))
        definition["version"] = 1
        items = [
            wait_item("h1", "2026-10-01T10:10:00+08:00", 30),
            wait_item("b1", "2026-09-24T10:20:00+08:00", 60),
            wait_item("b2", "2026-09-24T10:40:00+08:00", 90),
        ]
        result = build_metric_result(definition, items, WINDOW, BASELINE)
        self.assertEqual(len(result["comparison"]), 1)
        entry = result["comparison"][0]
        self.assertEqual(entry["bucket"], 10)
        self.assertEqual(entry["holiday_value"], 30.0)
        self.assertEqual(entry["baseline_value"], 88.5)  # 基线桶同样按 p95 聚合
        self.assertTrue(entry["improved"])  # lower_better：30 < 88.5

    def test_no_baseline_no_comparison(self):
        definition = validate_definition(queue_def(), evidence_kinds=("field_event",))
        result = build_metric_result(definition, [], WINDOW, None)
        self.assertIsNone(result["baseline"])
        self.assertIsNone(result["comparison"])


class ValidationTests(unittest.TestCase):
    def test_unknown_engine_rejected(self):
        with self.assertRaises(ValidationError):
            validate_definition(
                {"name": "x", "engine": "magic", "source_kind": "field_event",
                 "bucket_seconds": 3600, "direction": "lower_better"},
                evidence_kinds=("field_event",),
            )

    def test_missing_value_field_rejected(self):
        with self.assertRaises(ValidationError):
            validate_definition(
                {"name": "x", "engine": "avg", "source_kind": "field_event",
                 "bucket_seconds": 3600, "direction": "lower_better"},
                evidence_kinds=("field_event",),
            )

    def test_bad_name_rejected(self):
        with self.assertRaises(ValidationError):
            validate_definition(
                {"name": "Bad Name", "engine": "count", "source_kind": "field_event",
                 "bucket_seconds": 3600, "direction": "lower_better"},
                evidence_kinds=("field_event",),
            )


if __name__ == "__main__":
    unittest.main()
