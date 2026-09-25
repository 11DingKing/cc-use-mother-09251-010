"""并发测试：同版本并发签发、重复证据并发提交、并发建版。"""
import threading
import unittest
from datetime import datetime, timezone

from service_09251_010.application.security import Actor
from service_09251_010.application.services import ReviewService
from service_09251_010.domain.errors import ConflictError
from service_09251_010.persistence.sqlite_repo import SqliteRepository

SIGNER_A = Actor("signer_a", frozenset({"signer", "analyst"}))
SIGNER_B = Actor("signer_b", frozenset({"signer", "analyst"}))
ADMIN = Actor("admin", frozenset({"admin", "analyst", "signer"}))

SPECS = [{"spec_id": "wait", "metric_key": "queue_wait_minutes_avg", "bucket": "hour"}]


class ConcurrencyTests(unittest.TestCase):
    def setUp(self) -> None:
        self.repo = SqliteRepository(":memory:")
        self.svc = ReviewService(self.repo)
        self.review = self.svc.create_review(
            ADMIN,
            name="并发复盘",
            tz_name="Asia/Shanghai",
            window_start="2026-10-01T00:00:00",
            window_end="2026-10-02T00:00:00",
        )
        self.svc.add_evidence(
            ADMIN, self.review.id, kind="field_event", source_ref="feed",
            occurred_at="2026-10-01T09:00:00+08:00",
            payload={"event_type": "queue_wait", "wait_minutes": 20},
        )

    def test_concurrent_sign_exactly_one_wins(self) -> None:
        version = self.svc.create_version(ADMIN, self.review.id, SPECS)
        self.svc.compute_version(ADMIN, version.id)

        results: list[Exception | str] = []
        barrier = threading.Barrier(2)

        def sign(actor: Actor) -> None:
            barrier.wait()
            try:
                signed = self.svc.sign_version(actor, version.id)
                results.append(signed.signed_by or "ok")
            except ConflictError as exc:
                results.append(exc)
            except Exception as exc:  # noqa: BLE001
                results.append(exc)

        t1 = threading.Thread(target=sign, args=(SIGNER_A,))
        t2 = threading.Thread(target=sign, args=(SIGNER_B,))
        t1.start()
        t2.start()
        t1.join(timeout=10)
        t2.join(timeout=10)

        self.assertEqual(len(results), 2)
        successes = [r for r in results if not isinstance(r, Exception)]
        conflicts = [r for r in results if isinstance(r, ConflictError)]
        self.assertEqual(len(successes), 1)
        self.assertEqual(len(conflicts), 1)
        self.assertEqual(conflicts[0].code, "already_signed")

        final = self.svc.get_version(ADMIN, version.id)
        self.assertEqual(final.status, "signed")
        self.assertIn(final.signed_by, {"signer_a", "signer_b"})
        self.assertIsNotNone(final.signed_at)

    def test_concurrent_identical_evidence_deduped(self) -> None:
        outcomes = []
        lock = threading.Lock()
        barrier = threading.Barrier(5)

        def submit() -> None:
            barrier.wait()
            ev = self.svc.add_evidence(
                ADMIN, self.review.id, kind="field_event", source_ref="same-feed#42",
                occurred_at="2026-10-01T10:00:00+08:00",
                payload={"event_type": "queue_wait", "wait_minutes": 15},
            )
            with lock:
                outcomes.append(ev)

        threads = [threading.Thread(target=submit) for _ in range(5)]
        for t in threads:
            t.start()
        for t in threads:
            t.join(timeout=10)

        canonicals = [e for e in outcomes if e.duplicate_of is None]
        duplicates = [e for e in outcomes if e.duplicate_of is not None]
        self.assertEqual(len(canonicals), 1)
        self.assertEqual(len(duplicates), 4)
        self.assertTrue(all(d.duplicate_of == canonicals[0].id for d in duplicates))

        listing = self.svc.list_evidence(ADMIN, self.review.id)
        # setUp 1 条 + 新去重后 1 条
        self.assertEqual(len(listing), 2)

    def test_concurrent_version_creation_serialized(self) -> None:
        outcomes = []
        lock = threading.Lock()
        barrier = threading.Barrier(2)

        def create() -> None:
            barrier.wait()
            try:
                v = self.svc.create_version(ADMIN, self.review.id, SPECS)
                with lock:
                    outcomes.append(("ok", v.seq_no))
            except ConflictError as exc:
                with lock:
                    outcomes.append(("conflict", exc.code))

        t1 = threading.Thread(target=create)
        t2 = threading.Thread(target=create)
        t1.start()
        t2.start()
        t1.join(timeout=10)
        t2.join(timeout=10)

        self.assertEqual(len(outcomes), 2)
        self.assertEqual(sorted(o[0] for o in outcomes), ["conflict", "ok"])
        self.assertEqual(o_code := [o[1] for o in outcomes if o[0] == "conflict"][0],
                         "version_still_computing")
        versions = self.svc.list_versions(ADMIN, self.review.id)
        self.assertEqual(len(versions), 1)
        self.assertEqual(versions[0].status, "computing")


if __name__ == "__main__":
    unittest.main()
