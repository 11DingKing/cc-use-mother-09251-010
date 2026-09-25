"""应用服务测试：去重、版本冻结、签发、续算、权限、复核。"""
import json
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path

from service_09251_010.application.security import Actor
from service_09251_010.application.services import ReviewService
from service_09251_010.domain.canonical import fingerprint
from service_09251_010.domain.enums import VersionStatus
from service_09251_010.domain.errors import (
    ConflictError,
    NotFoundError,
    PermissionDeniedError,
    ValidationError,
)
from service_09251_010.persistence.sqlite_repo import SqliteRepository

ADMIN = Actor("admin", frozenset({"admin", "analyst", "signer", "read_pii"}))
ANALYST = Actor("analyst", frozenset({"analyst"}))
SIGNER = Actor("signer", frozenset({"signer"}))
VIEWER = Actor("viewer", frozenset())
ANON = Actor.anonymous()

SPECS = [
    {"spec_id": "wait", "metric_key": "queue_wait_minutes_avg", "bucket": "hour"},
    {"spec_id": "dispatch", "metric_key": "mobile_charging_dispatch_count", "bucket": "hour"},
]


class FixedClock:
    def __init__(self, start: datetime) -> None:
        self.current = start

    def now(self) -> datetime:
        return self.current

    def tick(self, seconds: int = 1) -> None:
        self.current += timedelta(seconds=seconds)


class ServiceTestBase(unittest.TestCase):
    def setUp(self) -> None:
        self.clock = FixedClock(datetime(2026, 10, 9, 0, 0, tzinfo=timezone.utc))
        self.repo = SqliteRepository(":memory:")
        self.svc = ReviewService(self.repo, clock=self.clock)
        self.review = self.svc.create_review(
            ADMIN,
            name="国庆保供",
            tz_name="Asia/Shanghai",
            window_start="2026-10-01T00:00:00",
            window_end="2026-10-02T00:00:00",
        )

    def _ev(self, **overrides):
        params = {
            "kind": "field_event",
            "source_ref": "ops-feed",
            "occurred_at": "2026-10-01T08:30:00+08:00",
            "payload": {"event_type": "queue_wait", "wait_minutes": 30},
        }
        params.update(overrides)
        return self.svc.add_evidence(ADMIN, self.review.id, **params)

    def _computed_version(self, specs=None):
        version = self.svc.create_version(ADMIN, self.review.id, specs or SPECS)
        self.svc.compute_version(ADMIN, version.id)
        return self.svc.get_version(ADMIN, version.id)


class EvidenceDedupTests(ServiceTestBase):
    def test_identical_submission_is_marked_duplicate(self) -> None:
        first = self._ev(source_ref="feed#1")
        self.clock.tick()
        second = self._ev(source_ref="feed#1")
        self.assertIsNone(first.duplicate_of)
        self.assertEqual(second.duplicate_of, first.id)
        self.assertNotEqual(second.id, first.id)

    def test_dedup_fingerprint_ignores_receipt_time_and_id(self) -> None:
        first = self._ev(source_ref="feed#1")
        self.clock.tick(500)
        second = self._ev(source_ref="feed#1")
        self.assertEqual(first.fingerprint, second.fingerprint)

    def test_reordered_payload_keys_same_fingerprint(self) -> None:
        e1 = self._ev(payload={"wait_minutes": 30, "event_type": "queue_wait"})
        e2 = self._ev(payload={"event_type": "queue_wait", "wait_minutes": 30})
        self.assertEqual(e1.fingerprint, e2.fingerprint)
        self.assertEqual(e2.duplicate_of, e1.id)

    def test_duplicates_excluded_from_default_listing_and_computation(self) -> None:
        self._ev()
        dup = self._ev()
        version = self._computed_version()
        self.assertEqual(version.input_evidence_ids.count(dup.id), 0)
        listing = self.svc.list_evidence(ADMIN, self.review.id)
        self.assertEqual(len(listing), 1)
        with_dups = self.svc.list_evidence(ADMIN, self.review.id, include_duplicates=True)
        self.assertEqual(len(with_dups), 2)

    def test_same_content_in_different_review_not_deduped(self) -> None:
        other = self.svc.create_review(
            ADMIN,
            name="春节保供",
            tz_name="Asia/Shanghai",
            window_start="2027-02-01T00:00:00",
            window_end="2027-02-02T00:00:00",
        )
        first = self._ev()
        second = self.svc.add_evidence(
            ADMIN,
            other.id,
            kind="field_event",
            source_ref="ops-feed",
            occurred_at="2026-10-01T08:30:00+08:00",
            payload={"event_type": "queue_wait", "wait_minutes": 30},
        )
        self.assertIsNone(second.duplicate_of)
        self.assertEqual(first.fingerprint, second.fingerprint)

    def test_invalid_kind_rejected(self) -> None:
        with self.assertRaises(ValidationError):
            self._ev(kind="satellite_photo")


class VersionAndSigningTests(ServiceTestBase):
    def test_signed_version_is_immutable_even_with_late_evidence(self) -> None:
        self._ev(occurred_at="2026-10-01T08:30:00+08:00",
                 payload={"event_type": "queue_wait", "wait_minutes": 30})
        v1 = self._computed_version()
        self.svc.sign_version(SIGNER, v1.id)

        # 迟到资料到达：只能生成新版本
        self.clock.tick(60)
        self._ev(occurred_at="2026-10-01T18:30:00+08:00",
                 payload={"event_type": "queue_wait", "wait_minutes": 5})
        v2 = self._computed_version()
        self.svc.sign_version(SIGNER, v2.id)

        v1_reloaded = self.svc.get_version(ADMIN, v1.id)
        self.assertEqual(v1_reloaded.status, VersionStatus.SUPERSEDED.value)
        # v1 的结果、输入指纹、结果指纹完全不变
        self.assertEqual(v1_reloaded.results, v1.results)
        self.assertEqual(v1_reloaded.inputs_fingerprint, v1.inputs_fingerprint)
        self.assertEqual(v1_reloaded.result_fingerprint, v1.result_fingerprint)
        self.assertEqual(v1_reloaded.superseded_by, v2.id)
        self.assertEqual(len(v1_reloaded.input_evidence_ids), 1)
        self.assertEqual(len(v2.input_evidence_ids), 2)

    def test_diff_locates_late_evidence_and_changed_buckets(self) -> None:
        self._ev(occurred_at="2026-10-01T08:30:00+08:00",
                 payload={"event_type": "queue_wait", "wait_minutes": 30})
        v1 = self._computed_version()
        self.svc.sign_version(SIGNER, v1.id)

        self._ev(occurred_at="2026-10-01T18:30:00+08:00",
                 payload={"event_type": "queue_wait", "wait_minutes": 5})
        self._ev(occurred_at="2026-10-01T18:45:00+08:00",
                 payload={"event_type": "mobile_charging"})
        v2 = self._computed_version()

        diff = self.svc.diff_versions(ADMIN, self.review.id, 1, 2)
        self.assertEqual(diff["inputs"]["old_count"], 1)
        self.assertEqual(diff["inputs"]["new_count"], 3)
        self.assertEqual(len(diff["inputs"]["only_in_new"]), 2)
        self.assertFalse(diff["inputs"]["same_fingerprint"])
        changes = {c["spec_id"]: c for c in diff["metric_changes"]}
        wait_buckets = changes["wait"]["changed_buckets"]
        self.assertIn("2026-10-01T18:00:00+08:00", wait_buckets)
        self.assertEqual(
            changes["dispatch"]["changed_buckets"]["2026-10-01T18:00:00+08:00"]["new"], 1.0
        )

    def test_cannot_double_sign(self) -> None:
        v1 = self._computed_version()
        self.svc.sign_version(SIGNER, v1.id)
        with self.assertRaises(ConflictError) as ctx:
            self.svc.sign_version(SIGNER, v1.id)
        self.assertEqual(ctx.exception.code, "already_signed")

    def test_signing_older_version_after_newer_signed_is_conflict(self) -> None:
        v1 = self._computed_version()
        self.svc.sign_version(SIGNER, v1.id)
        self._ev(occurred_at="2026-10-01T20:00:00+08:00",
                 payload={"event_type": "queue_wait", "wait_minutes": 8})
        v2 = self._computed_version()
        self.svc.sign_version(SIGNER, v2.id)
        with self.assertRaises(ConflictError) as ctx:
            self.svc.sign_version(SIGNER, v1.id)
        self.assertEqual(ctx.exception.code, "newer_version_signed")

    def test_only_signer_role_can_sign(self) -> None:
        v1 = self._computed_version()
        with self.assertRaises(PermissionDeniedError):
            self.svc.sign_version(VIEWER, v1.id)
        with self.assertRaises(PermissionDeniedError):
            self.svc.sign_version(ANALYST, v1.id)

    def test_cannot_sign_uncomputed_version(self) -> None:
        v = self.svc.create_version(ADMIN, self.review.id, SPECS)
        with self.assertRaises(ConflictError):
            self.svc.sign_version(SIGNER, v.id)

    def test_cannot_create_version_while_one_computing(self) -> None:
        self._ev()
        self.svc.create_version(ADMIN, self.review.id, SPECS)
        with self.assertRaises(ConflictError) as ctx:
            self.svc.create_version(ADMIN, self.review.id, SPECS)
        self.assertEqual(ctx.exception.code, "version_still_computing")

    def test_unknown_metric_key_rejected(self) -> None:
        with self.assertRaises(ValidationError):
            self.svc.create_version(
                ADMIN, self.review.id, [{"spec_id": "x", "metric_key": "nope"}]
            )


class ResumeTests(ServiceTestBase):
    def test_batched_computation_advances_cursor(self) -> None:
        for hour in (8, 9, 10, 11):
            self._ev(
                occurred_at=f"2026-10-01T{hour:02d}:30:00+08:00",
                payload={"event_type": "queue_wait", "wait_minutes": float(hour)},
            )
        version = self.svc.create_version(ADMIN, self.review.id, SPECS)

        p1 = self.svc.compute_version(ADMIN, version.id, batch_size=2)
        self.assertFalse(p1["done"])
        self.assertEqual(p1["processed"], 2)
        self.assertEqual(p1["cursor"], 2)

        p2 = self.svc.compute_version(ADMIN, version.id, batch_size=2)
        self.assertTrue(p2["done"])
        self.assertEqual(p2["status"], "ready")

        ready = self.svc.get_version(ADMIN, version.id)
        full = (8.0 + 9.0 + 10.0 + 11.0) / 4
        wait = next(r for r in ready.results if r["spec_id"] == "wait")
        self.assertAlmostEqual(wait["total"], full)

    def test_resume_after_process_restart_gives_same_result(self) -> None:
        for hour in (8, 9, 10):
            self._ev(
                occurred_at=f"2026-10-01T{hour:02d}:15:00+08:00",
                payload={"event_type": "queue_wait", "wait_minutes": 60.0},
            )
        version = self.svc.create_version(ADMIN, self.review.id, SPECS)
        self.svc.compute_version(ADMIN, version.id, batch_size=1)
        self.svc.compute_version(ADMIN, version.id, batch_size=1)

        # 模拟重启：新服务实例挂到同一仓储，recover 自动续算
        restarted = ReviewService(self.repo, clock=self.clock)
        recovered = restarted.recover_interrupted()
        self.assertEqual([r["version_id"] for r in recovered], [version.id])
        ready = restarted.get_version(ADMIN, version.id)
        self.assertEqual(ready.status, "ready")
        wait = next(r for r in ready.results if r["spec_id"] == "wait")
        self.assertEqual(wait["total"], 60.0)

    def test_checkpoint_state_cleared_after_completion(self) -> None:
        self._ev(occurred_at="2026-10-01T08:30:00+08:00")
        self._ev(occurred_at="2026-10-01T09:30:00+08:00")
        version = self.svc.create_version(ADMIN, self.review.id, SPECS)
        self.svc.compute_version(ADMIN, version.id, batch_size=1)
        # 首批之后仍有剩余证据，检查点保留
        self.assertIsNotNone(self.repo.get_compute_state(version.id))
        self.svc.compute_version(ADMIN, version.id)
        self.assertIsNone(self.repo.get_compute_state(version.id))

    def test_resume_with_file_backed_db(self) -> None:
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        db_path = str(Path(tmp.name) / "review.db")
        repo = SqliteRepository(db_path)
        svc = ReviewService(repo, clock=self.clock)
        review = svc.create_review(
            ADMIN,
            name="文件库",
            tz_name="Asia/Shanghai",
            window_start="2026-10-01T00:00:00",
            window_end="2026-10-02T00:00:00",
        )
        svc.add_evidence(
            ADMIN, review.id, kind="field_event", source_ref="s1",
            occurred_at="2026-10-01T09:00:00+08:00",
            payload={"event_type": "mobile_charging"},
        )
        svc.add_evidence(
            ADMIN, review.id, kind="field_event", source_ref="s2",
            occurred_at="2026-10-01T10:00:00+08:00",
            payload={"event_type": "mobile_charging"},
        )
        v = svc.create_version(ADMIN, review.id, SPECS)
        svc.compute_version(ADMIN, v.id, batch_size=1)
        repo.close()

        repo2 = SqliteRepository(db_path)
        svc2 = ReviewService(repo2, clock=self.clock)
        recovered = svc2.recover_interrupted()
        self.assertEqual(len(recovered), 1)
        ready = svc2.get_version(ADMIN, v.id)
        self.assertEqual(ready.status, "ready")
        repo2.close()


class FingerprintTests(ServiceTestBase):
    def test_inputs_fingerprint_changes_only_with_inputs(self) -> None:
        self._ev()
        v1 = self._computed_version()
        # 完全相同输入再建一版：输入指纹一致
        v2 = self._computed_version()
        self.assertEqual(v1.inputs_fingerprint, v2.inputs_fingerprint)
        self.assertEqual(v1.result_fingerprint, v2.result_fingerprint)

        self._ev(source_ref="other", payload={"event_type": "mobile_charging"})
        v3 = self._computed_version()
        self.assertNotEqual(v2.inputs_fingerprint, v3.inputs_fingerprint)

    def test_fingerprint_is_canonical_sha256(self) -> None:
        material = {"b": 2, "a": [1, {"c": 3}]}
        self.assertEqual(fingerprint(material), fingerprint({"a": [1, {"c": 3}], "b": 2}))
        self.assertEqual(len(fingerprint(material)), 64)

    def test_spec_change_changes_result_fingerprint(self) -> None:
        self._ev()
        v1 = self._computed_version([{"spec_id": "wait",
                                      "metric_key": "queue_wait_minutes_avg", "bucket": "hour"}])
        v2 = self._computed_version([{"spec_id": "wait",
                                      "metric_key": "queue_wait_minutes_avg", "bucket": "day"}])
        self.assertEqual(v1.inputs_fingerprint, v2.inputs_fingerprint)
        self.assertNotEqual(v1.result_fingerprint, v2.result_fingerprint)


class PrivacyTests(ServiceTestBase):
    def _rescue(self, phone="13800001111", name="李四"):
        return self.svc.add_evidence(
            ADMIN, self.review.id,
            kind="rescue_record",
            source_ref="rescue-1",
            occurred_at="2026-10-01T09:00:00+08:00",
            payload={"response_seconds": 900, "contact_name": name, "contact_phone": phone},
        )

    def test_unprivileged_actor_sees_redacted_payload_without_identity(self) -> None:
        self._rescue()
        rows = self.svc.list_evidence(VIEWER, self.review.id)
        # 个体标识字段剔除，服务指标字段保留
        self.assertNotIn("contact_phone", rows[0]["payload"])
        self.assertNotIn("contact_name", rows[0]["payload"])
        self.assertEqual(rows[0]["payload"]["response_seconds"], 900)
        self.assertTrue(rows[0]["redacted"])
        self.assertIn("contact_phone", rows[0]["redacted_fields"])

    def test_privileged_actor_sees_payload(self) -> None:
        self._rescue()
        rows = self.svc.list_evidence(ADMIN, self.review.id)
        self.assertEqual(rows[0]["payload"]["contact_phone"], "13800001111")

    def test_public_query_redacts_user_fields_but_keeps_metric_fields(self) -> None:
        self.svc.add_evidence(
            ADMIN, self.review.id,
            kind="public_query",
            source_ref="q-1",
            occurred_at="2026-10-01T09:00:00+08:00",
            payload={"channel": "app", "response_seconds": 12, "user_phone": "13900000000"},
        )
        rows = self.svc.list_evidence(ANALYST, self.review.id)
        self.assertNotIn("user_phone", rows[0]["payload"])
        self.assertEqual(rows[0]["payload"]["response_seconds"], 12)
        self.assertTrue(rows[0]["redacted"])

    def test_anonymous_blocked(self) -> None:
        with self.assertRaises(PermissionDeniedError):
            self.svc.list_reviews(ANON)
        with self.assertRaises(PermissionDeniedError):
            self.svc.create_review(
                ANON, name="x", tz_name="Asia/Shanghai",
                window_start="2026-10-01T00:00:00", window_end="2026-10-02T00:00:00",
            )

    def test_export_never_contains_raw_pii(self) -> None:
        self._rescue()
        v1 = self._computed_version([{"spec_id": "rescue",
                                      "metric_key": "rescue_response_minutes_avg",
                                      "bucket": "hour"}])
        exported = self.svc.export_version(VIEWER, v1.id)
        raw = json.dumps(exported, ensure_ascii=False)
        self.assertNotIn("13800001111", raw)
        self.assertNotIn("李四", raw)
        self.assertTrue(exported["privacy"]["pii_evidence_present"])
        self.assertFalse(exported["privacy"]["payload_included"])
        # 聚合数值仍可导出
        self.assertEqual(exported["version"]["results"][0]["total"], 15.0)


class ChallengeTests(ServiceTestBase):
    def test_challenge_lifecycle(self) -> None:
        v1 = self._computed_version()
        ch = self.svc.raise_challenge(
            VIEWER, self.review.id, v1.id, "18 点时段数据疑似缺失", metric_key="wait"
        )
        self.assertEqual(ch.status, "open")
        resolved = self.svc.resolve_challenge(
            ANALYST, ch.id, "确认缺失，补传后已在 v2 复核", approve=True
        )
        self.assertEqual(resolved.status, "resolved")
        with self.assertRaises(ConflictError):
            self.svc.resolve_challenge(ANALYST, ch.id, "再次处理", approve=False)

    def test_viewer_cannot_resolve(self) -> None:
        v1 = self._computed_version()
        ch = self.svc.raise_challenge(VIEWER, self.review.id, v1.id, "有疑问")
        with self.assertRaises(PermissionDeniedError):
            self.svc.resolve_challenge(VIEWER, ch.id, "处理", approve=True)

    def test_challenge_requires_reason(self) -> None:
        v1 = self._computed_version()
        with self.assertRaises(ValidationError):
            self.svc.raise_challenge(VIEWER, self.review.id, v1.id, "  ")

    def test_missing_entities_404(self) -> None:
        with self.assertRaises(NotFoundError):
            self.svc.get_review(ADMIN, "rev_nonexistent")
        with self.assertRaises(NotFoundError):
            self.svc.get_version(ADMIN, "ver_nonexistent")


if __name__ == "__main__":
    unittest.main()
