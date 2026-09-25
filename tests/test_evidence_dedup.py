"""证据去重：业务键幂等、内容冲突识别、批内重复与字段校验。"""
import unittest

from helpers import (
    ADMIN,
    SH_DAY,
    make_service,
    public_query,
    rescue_record,
    wait_event,
)


class EvidenceDedupTests(unittest.TestCase):
    def setUp(self):
        self.service, self.store = make_service()
        self.review_id = self.service.create_review(
            ADMIN, region="沪", name="国庆复盘", window=SH_DAY, metric_def_ids=[]
        )["id"]

    def add(self, items):
        return self.service.add_evidence(ADMIN, self.review_id, items)

    def test_replay_is_idempotent(self):
        batch = [
            wait_event("w1", "2026-10-01T10:05:00+08:00", 30),
            rescue_record("r1", "2026-10-01T11:00:00+08:00"),
            public_query("q1", "2026-10-01T12:00:00+08:00"),
        ]
        first = self.add(batch)
        self.assertEqual(len(first["added"]), 3)

        second = self.add(batch + [wait_event("w2", "2026-10-01T13:00:00+08:00", 20)])
        self.assertEqual(len(second["duplicates"]), 3)
        self.assertEqual(len(second["added"]), 1)
        self.assertEqual(self.store.count_evidence(self.review_id), 4)

    def test_same_key_different_content_is_conflict_not_overwrite(self):
        self.add([wait_event("w1", "2026-10-01T10:05:00+08:00", 30)])
        result = self.add([wait_event("w1", "2026-10-01T10:05:00+08:00", 45)])
        self.assertEqual(len(result["conflicts"]), 1)
        self.assertEqual(len(result["added"]), 0)
        self.assertEqual(self.store.count_evidence(self.review_id), 1)
        stored = self.service.list_evidence(ADMIN, self.review_id, kind="field_event")
        self.assertEqual(stored[0]["payload"]["wait_minutes"], 30)

    def test_duplicates_within_single_batch(self):
        result = self.add([
            wait_event("w1", "2026-10-01T10:05:00+08:00", 30),
            wait_event("w1", "2026-10-01T10:05:00+08:00", 30),
        ])
        self.assertEqual(len(result["added"]), 1)
        self.assertEqual(len(result["duplicates"]), 1)
        self.assertEqual(self.store.count_evidence(self.review_id), 1)

    def test_invalid_items_reported_per_position(self):
        result = self.add([
            {"kind": "mystery", "source": "s", "external_id": "e",
             "occurred_at": "2026-10-01T10:00:00+08:00", "payload": {}},
            {"kind": "field_event", "source": "s", "external_id": "e2",
             "occurred_at": "2026-10-01T10:00:00", "payload": {}},  # 无时区
            wait_event("w9", "2026-10-01T10:05:00+08:00", 12),
        ])
        self.assertEqual(len(result["errors"]), 2)
        self.assertEqual([e["position"] for e in result["errors"]], [0, 1])
        self.assertEqual(len(result["added"]), 1)

    def test_empty_batch_rejected(self):
        with self.assertRaises(Exception) as ctx:
            self.add([])
        self.assertEqual(getattr(ctx.exception, "code", None), "validation_error")


if __name__ == "__main__":
    unittest.main()
