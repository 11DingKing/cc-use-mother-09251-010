"""跨时区节日窗口与 DST 分桶测试。"""
import unittest
from datetime import timezone

from service_09251_010.domain import windows
from service_09251_010.domain.errors import ValidationError


class WindowTests(unittest.TestCase):
    def test_local_window_converts_to_utc_shanghai(self) -> None:
        start, end = windows.local_window_to_utc(
            "2026-10-01T00:00:00", "2026-10-08T00:00:00", "Asia/Shanghai"
        )
        # 上海 UTC+8：本地午夜对应前一日 16:00 UTC
        self.assertEqual(start.isoformat(), "2026-09-30T16:00:00+00:00")
        self.assertEqual(end.isoformat(), "2026-10-07T16:00:00+00:00")

    def test_naive_and_zoned_inputs_agree(self) -> None:
        naive = windows.parse_instant("2026-10-01T08:00:00", default_tz=windows.get_zone("Asia/Shanghai"))
        zoned = windows.parse_instant("2026-10-01T08:00:00+08:00")
        self.assertEqual(naive, zoned)

    def test_naive_time_without_default_rejected(self) -> None:
        with self.assertRaises(ValidationError):
            windows.parse_instant("2026-10-01T08:00:00")

    def test_unknown_timezone_rejected(self) -> None:
        with self.assertRaises(ValidationError):
            windows.local_window_to_utc("2026-01-01T00:00:00", "2026-01-02T00:00:00", "Mars/Olympus")

    def test_spring_forward_day_has_23_hour_buckets(self) -> None:
        # 美东 2026-03-08 春令时，本地 02:00 直接跳到 03:00，当天只有 23 小时
        start, end = windows.local_window_to_utc(
            "2026-03-08T00:00:00", "2026-03-09T00:00:00", "America/New_York"
        )
        labels = [label for label, _s, _e in windows.iter_buckets(start, end, "America/New_York", "hour")]
        self.assertEqual(len(labels), 23)
        self.assertNotIn("2026-03-08T02:00:00-05:00", labels)
        self.assertIn("2026-03-08T01:00:00-05:00", labels)
        self.assertIn("2026-03-08T03:00:00-04:00", labels)

    def test_fall_back_day_has_25_hour_buckets(self) -> None:
        # 美东 2026-11-01 秋令时，当天 25 小时；UTC 轴步进保证不重桶
        start, end = windows.local_window_to_utc(
            "2026-11-01T00:00:00", "2026-11-02T00:00:00", "America/New_York"
        )
        labels = [label for label, _s, _e in windows.iter_buckets(start, end, "America/New_York", "hour")]
        self.assertEqual(len(labels), 25)
        self.assertEqual(len(labels), len(set(labels)))

    def test_event_bucketed_by_local_time_not_utc(self) -> None:
        # UTC 2026-10-01T02:30 = 上海 10:30，必须归入本地 10 点桶
        instant = windows.parse_instant("2026-10-01T02:30:00+00:00")
        self.assertEqual(
            windows.bucket_key(instant, "Asia/Shanghai", "hour"),
            "2026-10-01T10:00:00+08:00",
        )

    def test_buckets_cover_full_window_in_utc(self) -> None:
        start, end = windows.local_window_to_utc(
            "2026-03-08T00:00:00", "2026-03-09T00:00:00", "America/New_York"
        )
        spans = list(windows.iter_buckets(start, end, "America/New_York", "hour"))
        self.assertEqual(spans[0][1], start)
        self.assertEqual(spans[-1][2], end)
        for (_, s1, e1), (_, s2, _e2) in zip(spans, spans[1:]):
            self.assertEqual(e1, s2)
        self.assertTrue(all(s.tzinfo is not None for _, s, _ in spans))


if __name__ == "__main__":
    unittest.main()
