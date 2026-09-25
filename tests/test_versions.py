"""版本语义：迟到资料产生新版本、已签发结论不变、差异来源可解释。"""
import unittest

from helpers import (
    ADMIN,
    SH_BASELINE_DAY,
    SH_DAY,
    make_service,
    queue_def,
    rescue_record,
    wait_event,
)

from service_09251_010.domain.errors import ConflictError


def build_review(service):
    def_id = service.create_metric_definition(ADMIN, queue_def())["id"]
    return service.create_review(
        ADMIN,
        region="沪",
        name="国庆复盘",
        window=SH_DAY,
        baseline=SH_BASELINE_DAY,
        metric_def_ids=[def_id],
    )["id"]


class LateDataVersionTests(unittest.TestCase):
    def setUp(self):
        self.service, self.store = make_service()
        self.review_id = build_review(self.service)
        self.service.add_evidence(ADMIN, self.review_id, [
            wait_event("w1", "2026-10-01T10:05:00+08:00", 30),
            wait_event("w2", "2026-10-01T10:25:00+08:00", 40),
            wait_event("w3", "2026-10-01T10:45:00+08:00", 50),
            wait_event("b1", "2026-09-24T10:15:00+08:00", 70),
        ])

    def compute(self):
        return self.service.compute(ADMIN, self.review_id)

    def test_late_evidence_creates_new_version_issued_stays_frozen(self):
        first = self.compute()
        self.assertEqual(first["version_no"], 1)
        self.service.issue_version(ADMIN, self.review_id, 1)
        issued_snapshot = self.service.get_version(ADMIN, self.review_id, 1)
        self.assertEqual(
            issued_snapshot["results"]["queue_wait_p95@v1"]["holiday"]["buckets"][10]["value"],
            49.0,
        )

        # 资料迟到：两条更短的排队记录 + 一条迟到的救援记录
        self.service.add_evidence(ADMIN, self.review_id, [
            wait_event("w4", "2026-10-01T10:50:00+08:00", 5),
            wait_event("w5", "2026-10-01T10:55:00+08:00", 10),
            rescue_record("r1", "2026-10-01T11:30:00+08:00"),
        ])
        detail = self.service.get_review_detail(ADMIN, self.review_id)
        self.assertEqual(detail["uncomputed_evidence_count"], 3)

        second = self.compute()
        self.assertEqual(second["version_no"], 2)
        self.assertNotEqual(first["fingerprint"], second["fingerprint"])

        # 已签发的 v1 结论保持不变
        reloaded = self.service.get_version(ADMIN, self.review_id, 1)
        self.assertEqual(reloaded["status"], "issued")
        self.assertEqual(reloaded["results"], issued_snapshot["results"])
        self.assertEqual(reloaded["fingerprint"], issued_snapshot["fingerprint"])

        # v2 反映迟到资料：p95 从 49 降到 48
        v2 = self.service.get_version(ADMIN, self.review_id, 2)
        self.assertEqual(v2["status"], "draft")
        self.assertEqual(
            v2["results"]["queue_wait_p95@v1"]["holiday"]["buckets"][10]["value"], 48.0
        )

    def test_diff_attributes_changes_to_late_evidence(self):
        self.compute()
        self.service.issue_version(ADMIN, self.review_id, 1)
        self.service.add_evidence(ADMIN, self.review_id, [
            wait_event("w4", "2026-10-01T10:50:00+08:00", 5),
            wait_event("w5", "2026-10-01T10:55:00+08:00", 10),
            rescue_record("r1", "2026-10-01T11:30:00+08:00"),
        ])
        self.compute()
        diff = self.service.diff_versions(ADMIN, self.review_id, 1, 2)

        # 差异来源：迟到的 2 条现场事件 + 1 条救援记录
        self.assertEqual(diff["evidence"]["added_by_kind"], {"field_event": 2, "rescue_record": 1})
        self.assertEqual(diff["evidence"]["removed"], [])
        self.assertEqual(len(diff["evidence"]["added"]), 3)

        metric = diff["metrics"][0]
        self.assertEqual(metric["metric"], "queue_wait_p95@v1")
        change = metric["buckets"][0]
        self.assertEqual(change["bucket"], 10)
        self.assertEqual(change["label"], "2026-10-01T10:00+08:00")
        self.assertEqual(change["from"], 49.0)
        self.assertEqual(change["to"], 48.0)

    def test_recompute_without_new_evidence_is_noop(self):
        self.compute()
        again = self.compute()
        self.assertTrue(again["unchanged"])
        self.assertEqual(again["version_no"], 1)
        self.assertEqual(len(self.service.list_versions(ADMIN, self.review_id)), 1)

    def test_issued_version_cannot_be_reissued(self):
        self.compute()
        self.service.issue_version(ADMIN, self.review_id, 1)
        with self.assertRaises(ConflictError):
            self.service.issue_version(ADMIN, self.review_id, 1)

    def test_comparison_marks_improved_time_buckets(self):
        self.compute()
        v1 = self.service.get_version(ADMIN, self.review_id, 1)
        comparison = v1["results"]["queue_wait_p95@v1"]["comparison"]
        self.assertEqual(len(comparison), 1)
        self.assertEqual(comparison[0]["label"], "2026-10-01T10:00+08:00")
        self.assertTrue(comparison[0]["improved"])

    def test_export_is_machine_readable_and_self_contained(self):
        self.compute()
        self.service.issue_version(ADMIN, self.review_id, 1)
        self.service.recheck_version(ADMIN, self.review_id, 1)
        export = self.service.export_version(ADMIN, self.review_id, 1)
        self.assertEqual(export["schema"], "service_09251_010.export/v1")
        self.assertTrue(export["input_manifest_hash"].startswith("sha256:"))
        self.assertEqual(export["version"]["status"], "issued")
        self.assertEqual(export["version"]["issued_seq"], 1)
        self.assertEqual(len(export["version"]["manifest"]), 4)
        self.assertEqual(export["rechecks"][0]["result"], "match")


if __name__ == "__main__":
    unittest.main()
