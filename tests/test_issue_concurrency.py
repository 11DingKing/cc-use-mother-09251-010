"""并发签发：同一版本只有一个赢家；不同版本序号单调不重号。"""
import unittest
from concurrent.futures import ThreadPoolExecutor

from helpers import ADMIN, SH_DAY, make_service, queue_def, wait_event

from service_09251_010.domain.errors import ConflictError


class ConcurrentIssueTests(unittest.TestCase):
    def setUp(self):
        self.service, self.store = make_service()
        def_id = self.service.create_metric_definition(ADMIN, queue_def())["id"]
        self.review_id = self.service.create_review(
            ADMIN, region="沪", name="国庆复盘", window=SH_DAY, metric_def_ids=[def_id]
        )["id"]
        self.service.add_evidence(ADMIN, self.review_id, [
            wait_event("w1", "2026-10-01T10:05:00+08:00", 30),
        ])
        self.service.compute(ADMIN, self.review_id)

    def test_concurrent_issue_of_same_version_has_single_winner(self):
        outcomes = {"issued": 0, "conflict": 0, "other": []}

        def attempt():
            try:
                self.service.issue_version(ADMIN, self.review_id, 1)
                return "issued"
            except ConflictError:
                return "conflict"
            except Exception as exc:  # noqa: BLE001 - 测试需要捕获一切意外
                return exc

        with ThreadPoolExecutor(max_workers=8) as pool:
            for outcome in pool.map(lambda _: attempt(), range(8)):
                if isinstance(outcome, str):
                    outcomes[outcome] += 1
                else:
                    outcomes["other"].append(outcome)

        self.assertEqual(outcomes["issued"], 1)
        self.assertEqual(outcomes["conflict"], 7)
        self.assertEqual(outcomes["other"], [])
        version = self.service.get_version(ADMIN, self.review_id, 1)
        self.assertEqual(version["status"], "issued")
        self.assertEqual(version["issued_seq"], 1)

    def test_concurrent_issue_of_distinct_versions_gets_distinct_sequences(self):
        # 再补两批迟到资料，制造 v2、v3 两个草稿
        self.service.add_evidence(ADMIN, self.review_id, [
            wait_event("w2", "2026-10-01T11:05:00+08:00", 25),
        ])
        self.service.compute(ADMIN, self.review_id)
        self.service.add_evidence(ADMIN, self.review_id, [
            wait_event("w3", "2026-10-01T12:05:00+08:00", 20),
        ])
        self.service.compute(ADMIN, self.review_id)

        with ThreadPoolExecutor(max_workers=3) as pool:
            results = list(pool.map(
                lambda no: self.service.issue_version(ADMIN, self.review_id, no), [1, 2, 3]
            ))
        sequences = sorted(item["issued_seq"] for item in results)
        self.assertEqual(sequences, [1, 2, 3])
        self.assertTrue(all(item["status"] == "issued" for item in results))


if __name__ == "__main__":
    unittest.main()
