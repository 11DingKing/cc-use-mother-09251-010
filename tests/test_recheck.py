"""复核：按冻结输入重算，校验证据哈希、指标结果与输入指纹。"""
import json
import unittest

from helpers import ADMIN, SH_DAY, make_service, queue_def, rescue_record, wait_event


class RecheckTests(unittest.TestCase):
    def setUp(self):
        self.service, self.store = make_service()
        def_id = self.service.create_metric_definition(ADMIN, queue_def())["id"]
        self.review_id = self.service.create_review(
            ADMIN, region="沪", name="国庆复盘", window=SH_DAY, metric_def_ids=[def_id]
        )["id"]
        self.service.add_evidence(ADMIN, self.review_id, [
            wait_event("w1", "2026-10-01T10:05:00+08:00", 30),
            wait_event("w2", "2026-10-01T10:35:00+08:00", 50),
            rescue_record("r1", "2026-10-01T11:00:00+08:00"),
        ])
        self.service.compute(ADMIN, self.review_id)
        self.service.issue_version(ADMIN, self.review_id, 1)

    def test_recheck_matches_untouched_version(self):
        report = self.service.recheck_version(ADMIN, self.review_id, 1)
        self.assertEqual(report["result"], "match")
        self.assertTrue(report["details"]["fingerprint"]["match"])
        self.assertTrue(all(m["result_match"] for m in report["details"]["metrics"]))
        self.assertEqual(report["details"]["problems"], [])

    def test_recheck_detects_tampered_evidence(self):
        evidence_id = self.service.list_evidence(ADMIN, self.review_id)[0]["id"]
        tampered = {"type": "queue_wait_observed", "wait_minutes": 999}
        with self.store._lock:  # 白盒：模拟库内数据被绕过服务层篡改
            self.store._conn.execute(
                "UPDATE evidence SET payload_json=? WHERE id=?",
                (json.dumps(tampered, ensure_ascii=False), evidence_id),
            )
        report = self.service.recheck_version(ADMIN, self.review_id, 1)
        self.assertEqual(report["result"], "mismatch")
        problems = report["details"]["problems"]
        self.assertEqual(problems, [{"type": "hash_mismatch", "evidence_id": evidence_id}])
        self.assertFalse(report["details"]["metrics"][0]["result_match"])

    def test_recheck_history_is_listed(self):
        self.service.recheck_version(ADMIN, self.review_id, 1)
        self.service.recheck_version(ADMIN, self.review_id, 1)
        history = self.service.list_rechecks(ADMIN, self.review_id, 1)
        self.assertEqual(len(history), 2)
        self.assertTrue(all(item["result"] == "match" for item in history))


if __name__ == "__main__":
    unittest.main()
