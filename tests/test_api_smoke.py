"""端到端冒烟：建口径 → 建复盘 → 补证据 → 计算 → 签发 → 复核 → 迟到资料 → 差异 → 导出。"""
import unittest

from helpers import SH_DAY, call_api, make_api, queue_def, rescue_record, wait_event


class ApiSmokeTests(unittest.TestCase):
    def setUp(self):
        self.app, self.service = make_api()

    def test_health_needs_no_key(self):
        status, body, _ = call_api(self.app, "GET", "/api/v1/health")
        self.assertEqual(status, 200)
        self.assertEqual(body["status"], "ok")

    def test_unknown_route_is_404(self):
        status, body, _ = call_api(self.app, "GET", "/api/v1/nope", key="admin-key")
        self.assertEqual(status, 404)
        self.assertEqual(body["error"]["code"], "not_found")

    def test_full_lifecycle(self):
        key = "admin-key"
        status, definition, _ = call_api(
            self.app, "POST", "/api/v1/metric-definitions", queue_def(), key=key
        )
        self.assertEqual(status, 201)
        self.assertEqual(definition["key"], "queue_wait_p95@v1")

        status, review, _ = call_api(
            self.app,
            "POST",
            "/api/v1/reviews",
            {
                "region": "沪",
                "name": "国庆复盘",
                "window": SH_DAY,
                "metric_def_ids": [definition["id"]],
            },
            key=key,
        )
        self.assertEqual(status, 201)
        review_id = review["id"]

        status, batch, _ = call_api(
            self.app,
            "POST",
            f"/api/v1/reviews/{review_id}/evidence:batch",
            {
                "items": [
                    wait_event("w1", "2026-10-01T10:05:00+08:00", 30),
                    wait_event("w2", "2026-10-01T10:35:00+08:00", 50),
                ]
            },
            key=key,
        )
        self.assertEqual(status, 200)
        self.assertEqual(len(batch["added"]), 2)

        status, computed, _ = call_api(
            self.app, "POST", f"/api/v1/reviews/{review_id}/compute", key=key
        )
        self.assertEqual(status, 200)
        self.assertEqual(computed["version_no"], 1)
        self.assertFalse(computed["unchanged"])

        # 输入未变，再次计算不产生新版本
        status, again, _ = call_api(self.app, "POST", f"/api/v1/reviews/{review_id}/compute", key=key)
        self.assertTrue(again["unchanged"])

        status, issued, _ = call_api(
            self.app, "POST", f"/api/v1/reviews/{review_id}/versions/1/issue", key=key
        )
        self.assertEqual(status, 200)
        self.assertEqual(issued["status"], "issued")
        self.assertEqual(issued["issued_seq"], 1)

        status, recheck, _ = call_api(
            self.app, "POST", f"/api/v1/reviews/{review_id}/versions/1/recheck", key=key
        )
        self.assertEqual(status, 200)
        self.assertEqual(recheck["result"], "match")

        # 迟到资料 → 新版本 → 差异可解释
        call_api(
            self.app,
            "POST",
            f"/api/v1/reviews/{review_id}/evidence:batch",
            {"items": [rescue_record("r1", "2026-10-01T11:00:00+08:00")]},
            key=key,
        )
        status, second, _ = call_api(self.app, "POST", f"/api/v1/reviews/{review_id}/compute", key=key)
        self.assertEqual(second["version_no"], 2)

        status, diff, _ = call_api(
            self.app, "GET", f"/api/v1/reviews/{review_id}/diff", key=key,
            query={"from": 1, "to": 2},
        )
        self.assertEqual(status, 200)
        self.assertEqual(diff["evidence"]["added_by_kind"], {"rescue_record": 1})

        status, export, headers = call_api(
            self.app, "GET", f"/api/v1/reviews/{review_id}/versions/1/export", key=key
        )
        self.assertEqual(status, 200)
        self.assertEqual(export["schema"], "service_09251_010.export/v1")
        self.assertIn("attachment", headers.get("Content-Disposition", ""))

        status, versions, _ = call_api(self.app, "GET", f"/api/v1/reviews/{review_id}/versions", key=key)
        self.assertEqual([item["version_no"] for item in versions["items"]], [1, 2])

    def test_missing_version_is_404(self):
        status, review, _ = call_api(
            self.app,
            "POST",
            "/api/v1/reviews",
            {"region": "沪", "name": "x", "window": SH_DAY},
            key="admin-key",
        )
        status, body, _ = call_api(
            self.app, "GET", f"/api/v1/reviews/{review['id']}/versions/99", key="admin-key"
        )
        self.assertEqual(status, 404)


if __name__ == "__main__":
    unittest.main()
