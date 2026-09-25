"""指标口径注册、复算确定性与分时段聚合测试。"""
import unittest

from service_09251_010.application import metrics
from service_09251_010.application.metrics import MetricDefinition
from service_09251_010.domain import windows
from service_09251_010.domain.entities import Evidence, MetricSpec
from service_09251_010.domain.enums import EvidenceKind


def _ev(kind: str, at: str, payload: dict, eid: str = "e") -> Evidence:
    return Evidence(
        id=eid,
        review_id="r1",
        kind=kind,
        source_ref="src",
        occurred_at=windows.parse_instant(at),
        payload=payload,
        fingerprint="fp",
        received_at=windows.parse_instant("2026-10-09T00:00:00+00:00"),
    )


class MetricEngineTests(unittest.TestCase):
    def setUp(self) -> None:
        self.start, self.end = windows.local_window_to_utc(
            "2026-10-01T00:00:00", "2026-10-02T00:00:00", "Asia/Shanghai"
        )

    def test_builtin_metrics_registered(self) -> None:
        for key in (
            "mobile_charging_dispatch_count",
            "queue_wait_minutes_avg",
            "queue_length_max",
            "charger_availability_avg",
            "public_query_count",
            "query_response_seconds_p95",
            "rescue_response_minutes_avg",
        ):
            self.assertIn(key, metrics.metric_keys())

    def test_hourly_buckets_show_which_slots_changed(self) -> None:
        evidence = [
            _ev(EvidenceKind.FIELD_EVENT, "2026-10-01T08:10:00+08:00",
                {"event_type": "queue_wait", "wait_minutes": 60}, "a"),
            _ev(EvidenceKind.FIELD_EVENT, "2026-10-01T08:50:00+08:00",
                {"event_type": "queue_wait", "wait_minutes": 40}, "b"),
            # 移动充电到位后，10 点时段等待降到 10 分钟
            _ev(EvidenceKind.FIELD_EVENT, "2026-10-01T10:05:00+08:00",
                {"event_type": "mobile_charging"}, "c"),
            _ev(EvidenceKind.FIELD_EVENT, "2026-10-01T10:20:00+08:00",
                {"event_type": "queue_wait", "wait_minutes": 10}, "d"),
        ]
        wait_spec = MetricSpec("w", "queue_wait_minutes_avg", "排队", "hour", {})
        dispatch_spec = MetricSpec("d", "mobile_charging_dispatch_count", "派出", "hour", {})
        wait_result = metrics.compute(wait_spec, evidence, self.start, self.end, "Asia/Shanghai")
        dispatch_result = metrics.compute(dispatch_spec, evidence, self.start, self.end, "Asia/Shanghai")

        self.assertEqual(wait_result.buckets["2026-10-01T08:00:00+08:00"], 50.0)
        self.assertEqual(wait_result.buckets["2026-10-01T10:00:00+08:00"], 10.0)
        # 没有数据的小时：avg 输出 None，count 补 0
        self.assertIsNone(wait_result.buckets["2026-10-01T09:00:00+08:00"])
        self.assertEqual(dispatch_result.buckets["2026-10-01T10:00:00+08:00"], 1.0)
        self.assertEqual(dispatch_result.buckets["2026-10-01T09:00:00+08:00"], 0.0)

    def test_recompute_is_deterministic_regardless_of_order(self) -> None:
        spec = MetricSpec("q", "query_response_seconds_p95", "P95", "hour", {})
        payloads = [
            _ev(EvidenceKind.PUBLIC_QUERY, f"2026-10-01T09:{m:02d}:00+08:00",
                {"response_seconds": v, "user_phone": "13800000000"}, f"q{m}")
            for m, v in enumerate([3, 7, 5, 9, 20, 1, 100, 4])
        ]
        r1 = metrics.compute(spec, payloads, self.start, self.end, "Asia/Shanghai")
        r2 = metrics.compute(spec, list(reversed(payloads)), self.start, self.end, "Asia/Shanghai")
        self.assertEqual(r1.buckets, r2.buckets)
        self.assertEqual(r1.total, r2.total)

    def test_incremental_collect_then_finalize_equals_full_compute(self) -> None:
        spec = MetricSpec("w", "queue_wait_minutes_avg", "排队", "hour", {})
        evidence = [
            _ev(EvidenceKind.FIELD_EVENT, "2026-10-01T08:00:00+08:00",
                {"event_type": "queue_wait", "wait_minutes": 30}, "a"),
            _ev(EvidenceKind.FIELD_EVENT, "2026-10-01T09:00:00+08:00",
                {"event_type": "queue_wait", "wait_minutes": 20}, "b"),
            _ev(EvidenceKind.FIELD_EVENT, "2026-10-01T09:30:00+08:00",
                {"event_type": "queue_wait", "wait_minutes": 40}, "c"),
        ]
        full = metrics.compute(spec, evidence, self.start, self.end, "Asia/Shanghai")

        state = metrics.accumulator_init()
        metrics.collect_into(state, spec, evidence[:1], self.start, self.end, "Asia/Shanghai")
        # 模拟中断后从游标续算
        metrics.collect_into(state, spec, evidence[1:], self.start, self.end, "Asia/Shanghai")
        resumed = metrics.finalize(spec, state, self.start, self.end, "Asia/Shanghai")
        self.assertEqual(resumed.buckets, full.buckets)
        self.assertEqual(resumed.total, full.total)

    def test_where_filter_in_config(self) -> None:
        evidence = [
            _ev(EvidenceKind.CAPACITY_SNAPSHOT, "2026-10-01T08:00:00+08:00",
                {"station_id": "A", "queue_length": 5}, "a"),
            _ev(EvidenceKind.CAPACITY_SNAPSHOT, "2026-10-01T08:00:00+08:00",
                {"station_id": "B", "queue_length": 9}, "b"),
        ]
        spec = MetricSpec(
            "qa", "queue_length_max", "A站峰值", "hour", {"where": {"station_id": "A"}}
        )
        result = metrics.compute(spec, evidence, self.start, self.end, "Asia/Shanghai")
        self.assertEqual(result.total, 5.0)

    def test_events_outside_window_excluded(self) -> None:
        evidence = [
            _ev(EvidenceKind.FIELD_EVENT, "2026-09-30T23:59:00+08:00",
                {"event_type": "queue_wait", "wait_minutes": 99}, "before"),
            _ev(EvidenceKind.FIELD_EVENT, "2026-10-02T00:00:00+08:00",
                {"event_type": "queue_wait", "wait_minutes": 99}, "after"),
            _ev(EvidenceKind.FIELD_EVENT, "2026-10-01T23:59:00+08:00",
                {"event_type": "queue_wait", "wait_minutes": 12}, "inside"),
        ]
        spec = MetricSpec("w", "queue_wait_minutes_avg", "排队", "hour", {})
        result = metrics.compute(spec, evidence, self.start, self.end, "Asia/Shanghai")
        self.assertEqual(result.total, 12.0)

    def test_extensible_custom_metric(self) -> None:
        metrics.register_metric(
            MetricDefinition(
                key="test_plan_documents",
                label="计划版本数",
                evidence_kind=EvidenceKind.PLAN_VERSION,
                aggregator="count",
                extract=lambda payload, config: 1.0,
            )
        )
        evidence = [_ev(EvidenceKind.PLAN_VERSION, "2026-10-01T07:00:00+08:00", {"rev": 1}, "p1")]
        spec = MetricSpec("p", "test_plan_documents", "计划", "day", {})
        result = metrics.compute(spec, evidence, self.start, self.end, "Asia/Shanghai")
        self.assertEqual(result.total, 1.0)


if __name__ == "__main__":
    unittest.main()
