"""跨时区节日窗口：同一物理时刻在不同地区窗口中的归属。"""
import unittest

from service_09251_010.domain.windows import HolidayWindow, format_instant, parse_instant


class ParseInstantTests(unittest.TestCase):
    def test_accepts_zulu_and_offset(self):
        self.assertEqual(
            parse_instant("2026-10-01T10:00:00+08:00"),
            parse_instant("2026-10-01T02:00:00Z"),
        )

    def test_rejects_naive_instant(self):
        with self.assertRaises(ValueError):
            parse_instant("2026-10-01T10:00:00")

    def test_rejects_garbage(self):
        with self.assertRaises(ValueError):
            parse_instant("not-a-time")

    def test_format_roundtrip(self):
        self.assertEqual(
            format_instant(parse_instant("2026-10-01T10:00:00+08:00")),
            "2026-10-01T02:00:00.000Z",
        )


class HolidayWindowTests(unittest.TestCase):
    def test_same_instant_different_membership_across_timezones(self):
        shanghai = HolidayWindow.from_spec(
            {"tz": "Asia/Shanghai", "start": "2026-10-01", "end": "2026-10-02"}
        )
        los_angeles = HolidayWindow.from_spec(
            {"tz": "America/Los_Angeles", "start": "2026-10-01", "end": "2026-10-02"}
        )
        # 2026-10-01T02:00Z = 上海 10:00（节内） = 洛杉矶 09-30 19:00（节外）
        instant = parse_instant("2026-10-01T02:00:00Z")
        self.assertTrue(shanghai.contains(instant))
        self.assertFalse(los_angeles.contains(instant))

    def test_window_bounds_are_utc_midnights_in_local_zone(self):
        shanghai = HolidayWindow.from_spec(
            {"tz": "Asia/Shanghai", "start": "2026-10-01", "end": "2026-10-02"}
        )
        self.assertEqual(format_instant(shanghai.start_utc()), "2026-09-30T16:00:00.000Z")
        self.assertEqual(format_instant(shanghai.end_utc()), "2026-10-01T16:00:00.000Z")

    def test_end_is_exclusive(self):
        window = HolidayWindow.from_spec(
            {"tz": "Asia/Shanghai", "start": "2026-10-01", "end": "2026-10-02"}
        )
        self.assertFalse(window.contains(parse_instant("2026-10-01T16:00:00Z")))
        self.assertTrue(window.contains(parse_instant("2026-10-01T15:59:59Z")))

    def test_buckets_carry_local_labels(self):
        window = HolidayWindow.from_spec(
            {"tz": "Asia/Shanghai", "start": "2026-10-01", "end": "2026-10-02"}
        )
        buckets = window.buckets(3600)
        self.assertEqual(len(buckets), 24)
        self.assertEqual(buckets[0].label, "2026-10-01T00:00+08:00")
        self.assertEqual(buckets[10].label, "2026-10-01T10:00+08:00")

    def test_dst_transition_yields_25_hourly_buckets(self):
        # 英国 2026-10-25 夏令时结束，当地这一天有 25 个小时
        london = HolidayWindow.from_spec(
            {"tz": "Europe/London", "start": "2026-10-25", "end": "2026-10-26"}
        )
        buckets = london.buckets(3600)
        self.assertEqual(len(buckets), 25)
        labels = {bucket.label for bucket in buckets}
        self.assertIn("2026-10-25T01:00+01:00", labels)
        self.assertIn("2026-10-25T01:00+00:00", labels)

    def test_rejects_end_before_start(self):
        with self.assertRaises(ValueError):
            HolidayWindow.from_spec(
                {"tz": "Asia/Shanghai", "start": "2026-10-02", "end": "2026-10-01"}
            )

    def test_rejects_tz_aware_boundary(self):
        with self.assertRaises(ValueError):
            HolidayWindow.from_spec(
                {
                    "tz": "Asia/Shanghai",
                    "start": "2026-10-01T00:00:00+08:00",
                    "end": "2026-10-02",
                }
            )

    def test_rejects_unknown_timezone(self):
        with self.assertRaises(ValueError):
            HolidayWindow.from_spec(
                {"tz": "Mars/Olympus", "start": "2026-10-01", "end": "2026-10-02"}
            )


if __name__ == "__main__":
    unittest.main()
