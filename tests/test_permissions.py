"""权限：密钥认证、角色范围、个体信息按 pii:read 脱敏。"""
import unittest

from helpers import SH_DAY, call_api, make_api, queue_def, rescue_record, wait_event


class PermissionTests(unittest.TestCase):
    def setUp(self):
        self.app, self.service = make_api()
        status, definition, _ = call_api(
            self.app, "POST", "/api/v1/metric-definitions", queue_def(), key="admin-key"
        )
        self.assertEqual(status, 201)
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
            key="admin-key",
        )
        self.assertEqual(status, 201)
        self.review_id = review["id"]
        call_api(
            self.app,
            "POST",
            f"/api/v1/reviews/{self.review_id}/evidence:batch",
            {
                "items": [
                    wait_event("w1", "2026-10-01T10:05:00+08:00", 30),
                    rescue_record("r1", "2026-10-01T11:00:00+08:00"),
                ]
            },
            key="admin-key",
        )

    def test_missing_key_is_401(self):
        status, body, _ = call_api(self.app, "GET", f"/api/v1/reviews/{self.review_id}")
        self.assertEqual(status, 401)
        self.assertEqual(body["error"]["code"], "unauthorized")

    def test_bad_key_is_401(self):
        status, _, _ = call_api(
            self.app, "GET", f"/api/v1/reviews/{self.review_id}", key="forged"
        )
        self.assertEqual(status, 401)

    def test_viewer_cannot_write(self):
        status, body, _ = call_api(
            self.app,
            "POST",
            "/api/v1/reviews",
            {"region": "苏", "name": "x", "window": SH_DAY},
            key="viewer-key",
        )
        self.assertEqual(status, 403)
        self.assertEqual(body["error"]["code"], "forbidden")

    def test_viewer_cannot_compute(self):
        status, _, _ = call_api(
            self.app, "POST", f"/api/v1/reviews/{self.review_id}/compute", key="viewer-key"
        )
        self.assertEqual(status, 403)

    def test_analyst_sees_redacted_pii(self):
        status, body, _ = call_api(
            self.app, "GET", f"/api/v1/reviews/{self.review_id}/evidence", key="analyst-key"
        )
        self.assertEqual(status, 200)
        rescue = next(item for item in body["items"] if item["kind"] == "rescue_record")
        self.assertEqual(rescue["payload"]["person_phone"], "***")
        self.assertEqual(rescue["payload"]["vehicle_plate"], "***")
        self.assertTrue(rescue["redacted"])

    def test_auditor_with_pii_scope_sees_full_payload(self):
        status, body, _ = call_api(
            self.app, "GET", f"/api/v1/reviews/{self.review_id}/evidence", key="auditor-key"
        )
        self.assertEqual(status, 200)
        rescue = next(item for item in body["items"] if item["kind"] == "rescue_record")
        self.assertEqual(rescue["payload"]["person_phone"], "13800000000")
        self.assertNotIn("redacted", rescue)

    def test_only_issuer_scope_can_issue(self):
        call_api(self.app, "POST", f"/api/v1/reviews/{self.review_id}/compute", key="analyst-key")
        status, _, _ = call_api(
            self.app, "POST", f"/api/v1/reviews/{self.review_id}/versions/1/issue",
            key="analyst-key",
        )
        self.assertEqual(status, 403)
        status, body, _ = call_api(
            self.app, "POST", f"/api/v1/reviews/{self.review_id}/versions/1/issue",
            key="issuer-key",
        )
        self.assertEqual(status, 200)
        self.assertEqual(body["status"], "issued")

    def test_viewer_can_export(self):
        call_api(self.app, "POST", f"/api/v1/reviews/{self.review_id}/compute", key="analyst-key")
        status, body, _ = call_api(
            self.app, "GET", f"/api/v1/reviews/{self.review_id}/versions/1/export",
            key="viewer-key",
        )
        self.assertEqual(status, 200)
        self.assertEqual(body["schema"], "service_09251_010.export/v1")


if __name__ == "__main__":
    unittest.main()
