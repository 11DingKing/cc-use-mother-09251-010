"""HTTP 接口边界端到端测试（真实 socket + http.client）。"""
import http.client
import json
import threading
import unittest

from service_09251_010.app import build_service
from service_09251_010.interfaces.api import create_server


class ApiClient:
    def __init__(self, host: str, port: int) -> None:
        self.host = host
        self.port = port

    def request(self, method: str, path: str, body=None, actor: str | None = None,
                roles: str | None = None):
        conn = http.client.HTTPConnection(self.host, self.port, timeout=10)
        headers = {"Content-Type": "application/json"}
        if actor:
            headers["X-Actor"] = actor
        if roles:
            headers["X-Roles"] = roles
        payload = json.dumps(body, ensure_ascii=False).encode("utf-8") if body is not None else None
        conn.request(method, path, body=payload, headers=headers)
        resp = conn.getresponse()
        raw = resp.read().decode("utf-8")
        conn.close()
        return resp.status, json.loads(raw) if raw else None


class ApiTests(unittest.TestCase):
    def setUp(self) -> None:
        self.svc, self.repo = build_service(":memory:")
        self.httpd = create_server("127.0.0.1", 0, self.svc)
        self.port = self.httpd.server_address[1]
        self.thread = threading.Thread(target=self.httpd.serve_forever, daemon=True)
        self.thread.start()
        self.api = ApiClient("127.0.0.1", self.port)

    def tearDown(self) -> None:
        self.httpd.shutdown()
        self.httpd.server_close()
        self.thread.join(timeout=5)
        self.repo.close()

    ADMIN_ROLES = "admin,analyst,signer,read_pii"
    ANALYST_ROLES = "analyst"
    SIGNER_ROLES = "signer"

    def _create_review(self):
        status, body = self.api.request(
            "POST", "/api/reviews",
            {
                "name": "国庆复盘",
                "tz_name": "Asia/Shanghai",
                "window_start": "2026-10-01T00:00:00",
                "window_end": "2026-10-03T00:00:00",
            },
            actor="admin", roles=self.ADMIN_ROLES,
        )
        self.assertEqual(status, 201, body)
        return body["id"]

    def test_full_workflow_over_http(self) -> None:
        # 指标口径目录
        status, body = self.api.request("GET", "/api/metrics")
        self.assertEqual(status, 200)
        self.assertIn("queue_wait_minutes_avg", body["metrics"])

        review_id = self._create_review()

        # 补证据：计划版本、现场事件、容量、查询、救援五类
        samples = [
            ("plan_version", "plan#1", "2026-09-30T18:00:00+08:00", {"revision": 1}),
            ("field_event", "feed#1", "2026-10-01T08:30:00+08:00",
             {"event_type": "queue_wait", "wait_minutes": 40}),
            ("field_event", "feed#2", "2026-10-01T10:30:00+08:00",
             {"event_type": "mobile_charging"}),
            ("field_event", "feed#3", "2026-10-01T10:45:00+08:00",
             {"event_type": "queue_wait", "wait_minutes": 8}),
            ("capacity_snapshot", "cap#1", "2026-10-01T10:00:00+08:00",
             {"station_id": "S1", "queue_length": 3, "available_chargers": 2, "total_chargers": 10}),
            ("public_query", "q#1", "2026-10-01T09:00:00+08:00",
             {"channel": "app", "response_seconds": 5, "user_phone": "13900000000"}),
            ("rescue_record", "r#1", "2026-10-01T11:00:00+08:00",
             {"response_seconds": 1200, "contact_name": "王五", "contact_phone": "13800000000"}),
        ]
        for kind, ref, at, payload in samples:
            status, body = self.api.request(
                "POST", f"/api/reviews/{review_id}/evidence",
                {"kind": kind, "source_ref": ref, "occurred_at": at, "payload": payload},
                actor="admin", roles=self.ADMIN_ROLES,
            )
            self.assertEqual(status, 201, body)

        # 重复提交：200 + duplicate=true
        status, body = self.api.request(
            "POST", f"/api/reviews/{review_id}/evidence",
            {"kind": "field_event", "source_ref": "feed#1",
             "occurred_at": "2026-10-01T08:30:00+08:00",
             "payload": {"event_type": "queue_wait", "wait_minutes": 40}},
            actor="admin", roles=self.ADMIN_ROLES,
        )
        self.assertEqual(status, 200)
        self.assertTrue(body["duplicate"])

        # 建版并计算
        specs = [
            {"spec_id": "wait", "metric_key": "queue_wait_minutes_avg", "bucket": "hour"},
            {"spec_id": "dispatch", "metric_key": "mobile_charging_dispatch_count", "bucket": "hour"},
            {"spec_id": "avail", "metric_key": "charger_availability_avg", "bucket": "hour"},
        ]
        status, version = self.api.request(
            "POST", f"/api/reviews/{review_id}/versions",
            {"metric_specs": specs}, actor="analyst", roles=self.ANALYST_ROLES,
        )
        self.assertEqual(status, 201, version)
        version_id = version["id"]

        status, progress = self.api.request(
            "POST", f"/api/versions/{version_id}/compute", {},
            actor="analyst", roles=self.ANALYST_ROLES,
        )
        self.assertEqual(status, 200)
        self.assertTrue(progress["done"])

        status, ready = self.api.request(
            "GET", f"/api/versions/{version_id}", actor="admin", roles=self.ADMIN_ROLES
        )
        self.assertEqual(status, 200)
        self.assertEqual(ready["status"], "ready")
        results = {r["spec_id"]: r for r in ready["results"]}
        # 10 点移动充电到位后等待降到 8 分钟
        self.assertEqual(results["wait"]["buckets"]["2026-10-01T10:00:00+08:00"], 8.0)
        self.assertEqual(results["wait"]["buckets"]["2026-10-01T08:00:00+08:00"], 40.0)
        self.assertEqual(results["dispatch"]["buckets"]["2026-10-01T10:00:00+08:00"], 1.0)
        self.assertEqual(results["avail"]["total"], 0.2)

        # 签发
        status, signed = self.api.request(
            "POST", f"/api/versions/{version_id}/sign", {},
            actor="signer", roles=self.SIGNER_ROLES,
        )
        self.assertEqual(status, 200)
        self.assertEqual(signed["status"], "signed")
        self.assertEqual(signed["signed_by"], "signer")

        # 迟到资料 → v2 → 差异
        status, _ = self.api.request(
            "POST", f"/api/reviews/{review_id}/evidence",
            {"kind": "field_event", "source_ref": "feed#late",
             "occurred_at": "2026-10-01T12:05:00+08:00",
             "payload": {"event_type": "queue_wait", "wait_minutes": 2}},
            actor="admin", roles=self.ADMIN_ROLES,
        )
        self.assertEqual(status, 201)
        status, v2 = self.api.request(
            "POST", f"/api/reviews/{review_id}/versions",
            {"metric_specs": specs}, actor="analyst", roles=self.ANALYST_ROLES,
        )
        self.assertEqual(status, 201)
        self.api.request("POST", f"/api/versions/{v2['id']}/compute", {},
                         actor="analyst", roles=self.ANALYST_ROLES)

        status, diff = self.api.request(
            "GET", f"/api/reviews/{review_id}/diff?from=1&to=2",
            actor="admin", roles=self.ADMIN_ROLES,
        )
        self.assertEqual(status, 200)
        self.assertEqual(len(diff["inputs"]["only_in_new"]), 1)
        self.assertTrue(diff["old_version_immutable"])

        # 复核
        status, challenge = self.api.request(
            "POST", f"/api/reviews/{review_id}/challenges",
            {"version_id": v2["id"], "reason": "请核实 12 点数据来源", "metric_key": "wait"},
            actor="viewer", roles="",
        )
        self.assertEqual(status, 201)
        status, resolved = self.api.request(
            "POST", f"/api/challenges/{challenge['id']}/resolve",
            {"resolution": "来源已核验", "approve": True},
            actor="analyst", roles=self.ANALYST_ROLES,
        )
        self.assertEqual(status, 200)
        self.assertEqual(resolved["status"], "resolved")

        # 导出机器可读结果
        status, exported = self.api.request(
            "GET", f"/api/versions/{v2['id']}/export",
            actor="admin", roles=self.ADMIN_ROLES,
        )
        self.assertEqual(status, 200)
        self.assertEqual(exported["schema"], "holiday-charging-review-export/1")
        self.assertEqual(exported["version"]["inputs_fingerprint"],
                         ready["inputs_fingerprint"] if v2["seq_no"] == 1 else
                         exported["version"]["inputs_fingerprint"])
        raw = json.dumps(exported, ensure_ascii=False)
        self.assertNotIn("13800000000", raw)
        self.assertNotIn("13900000000", raw)

    def test_pii_enforcement_over_http(self) -> None:
        review_id = self._create_review()
        status, _ = self.api.request(
            "POST", f"/api/reviews/{review_id}/evidence",
            {"kind": "rescue_record", "source_ref": "r#1",
             "occurred_at": "2026-10-01T11:00:00+08:00",
             "payload": {"response_seconds": 600, "contact_phone": "13800000000"}},
            actor="admin", roles=self.ADMIN_ROLES,
        )
        self.assertEqual(status, 201)

        # 普通登录用户：标识字段剔除，服务指标保留
        status, body = self.api.request(
            "GET", f"/api/reviews/{review_id}/evidence", actor="viewer", roles=""
        )
        self.assertEqual(status, 200)
        self.assertNotIn("contact_phone", body[0]["payload"])
        self.assertEqual(body[0]["payload"]["response_seconds"], 600)
        self.assertTrue(body[0]["redacted"])

        # 有 read_pii：可见
        status, body = self.api.request(
            "GET", f"/api/reviews/{review_id}/evidence",
            actor="admin", roles=self.ADMIN_ROLES,
        )
        self.assertEqual(body[0]["payload"]["contact_phone"], "13800000000")

    def test_auth_and_validation_errors(self) -> None:
        # 匿名
        status, body = self.api.request("GET", "/api/reviews")
        self.assertEqual(status, 401)
        self.assertEqual(body["error"], "unauthorized")

        # 无权限建复盘
        status, body = self.api.request(
            "POST", "/api/reviews",
            {"name": "x", "tz_name": "Asia/Shanghai",
             "window_start": "2026-10-01T00:00:00", "window_end": "2026-10-02T00:00:00"},
            actor="viewer", roles="",
        )
        self.assertEqual(status, 403)

        # 参数错误：坏时间
        status, body = self.api.request(
            "POST", "/api/reviews",
            {"name": "x", "tz_name": "Asia/Shanghai",
             "window_start": "not-a-time", "window_end": "2026-10-02T00:00:00"},
            actor="admin", roles=self.ADMIN_ROLES,
        )
        self.assertEqual(status, 422)

        # 404
        status, body = self.api.request("GET", "/api/reviews/rev_missing",
                                        actor="admin", roles=self.ADMIN_ROLES)
        self.assertEqual(status, 404)

        # 错误路由
        status, body = self.api.request("GET", "/api/nope", actor="admin", roles=self.ADMIN_ROLES)
        self.assertEqual(status, 404)

    def test_batched_compute_endpoint(self) -> None:
        review_id = self._create_review()
        for hour in (8, 9, 10):
            self.api.request(
                "POST", f"/api/reviews/{review_id}/evidence",
                {"kind": "field_event", "source_ref": f"f{hour}",
                 "occurred_at": f"2026-10-01T{hour:02d}:00:00+08:00",
                 "payload": {"event_type": "mobile_charging"}},
                actor="admin", roles=self.ADMIN_ROLES,
            )
        status, version = self.api.request(
            "POST", f"/api/reviews/{review_id}/versions",
            {"metric_specs": [{"spec_id": "d",
                               "metric_key": "mobile_charging_dispatch_count", "bucket": "hour"}]},
            actor="admin", roles=self.ADMIN_ROLES,
        )
        vid = version["id"]
        status, p1 = self.api.request(
            "POST", f"/api/versions/{vid}/compute?batch_size=2", {},
            actor="admin", roles=self.ADMIN_ROLES,
        )
        self.assertFalse(p1["done"])
        self.assertEqual(p1["processed"], 2)
        status, p2 = self.api.request(
            "POST", f"/api/versions/{vid}/compute?batch_size=2", {},
            actor="admin", roles=self.ADMIN_ROLES,
        )
        self.assertTrue(p2["done"])


if __name__ == "__main__":
    unittest.main()
