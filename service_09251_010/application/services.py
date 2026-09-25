"""应用服务：用例编排。

所有读改写流程都在 begin_immediate() 事务内完成；证据指纹在建版时冻结，
计算过程支持按批提交检查点，进程中断后凭游标与中间态续算。
"""
from __future__ import annotations

import uuid
from datetime import datetime, timezone
from typing import Any

from . import metrics as metrics_engine
from ..domain import windows
from ..domain.canonical import fingerprint
from ..domain.entities import Challenge, Evidence, MetricSpec, Review, ReviewVersion
from ..domain.enums import ChallengeStatus, EvidenceKind, VersionStatus
from ..domain.errors import (
    ConflictError,
    NotFoundError,
    PermissionDeniedError,
    ValidationError,
)
from .ports import Clock, IdGenerator, Repository
from .security import Actor, evidence_payload_for, kind_has_pii

VALID_KINDS = {k.value for k in EvidenceKind}


class SystemClock:
    def now(self) -> datetime:
        return datetime.now(timezone.utc)


class UuidIdGenerator:
    def new_id(self, prefix: str = "") -> str:
        token = uuid.uuid4().hex
        return f"{prefix}_{token[:16]}" if prefix else token


def _unauthorized(message: str = "需要身份标识") -> PermissionDeniedError:
    return PermissionDeniedError(message, code="unauthorized", status=401)


class ReviewService:
    def __init__(
        self,
        repo: Repository,
        clock: Clock | None = None,
        id_gen: IdGenerator | None = None,
    ):
        self.repo = repo
        self.clock = clock or SystemClock()
        self.ids = id_gen or UuidIdGenerator()

    # ---- 鉴权辅助 ---------------------------------------------------------
    @staticmethod
    def _require_auth(actor: Actor) -> None:
        if not actor.is_authenticated:
            raise _unauthorized()

    def _require_manage(self, actor: Actor) -> None:
        self._require_auth(actor)
        if not actor.can_manage_review():
            raise PermissionDeniedError("无权创建复盘或定义指标口径")

    def _require_review(self, review_id: str) -> Review:
        review = self.repo.get_review(review_id)
        if review is None:
            raise NotFoundError(f"复盘不存在: {review_id}")
        return review

    def _require_version(self, version_id: str) -> ReviewVersion:
        version = self.repo.get_version(version_id)
        if version is None:
            raise NotFoundError(f"复盘版本不存在: {version_id}")
        return version

    # ---- 复盘 -------------------------------------------------------------
    def create_review(
        self,
        actor: Actor,
        *,
        name: str,
        tz_name: str,
        window_start: str,
        window_end: str,
    ) -> Review:
        self._require_manage(actor)
        if not name or not str(name).strip():
            raise ValidationError("复盘名称不能为空")
        start_utc, end_utc = windows.local_window_to_utc(window_start, window_end, tz_name)
        review = Review(
            id=self.ids.new_id("rev"),
            name=str(name).strip(),
            tz_name=tz_name,
            window_start=start_utc,
            window_end=end_utc,
            created_by=actor.name,
            created_at=self.clock.now(),
        )
        with self.repo.begin_immediate():
            self.repo.insert_review(review)
        return review

    def get_review(self, actor: Actor, review_id: str) -> Review:
        self._require_auth(actor)
        return self._require_review(review_id)

    def list_reviews(self, actor: Actor) -> list[Review]:
        self._require_auth(actor)
        return self.repo.list_reviews()

    # ---- 证据 -------------------------------------------------------------
    def add_evidence(
        self,
        actor: Actor,
        review_id: str,
        *,
        kind: str,
        source_ref: str,
        occurred_at: str,
        payload: dict[str, Any],
    ) -> Evidence:
        self._require_auth(actor)
        if kind not in VALID_KINDS:
            raise ValidationError(f"未知证据类别: {kind}")
        if not isinstance(payload, dict):
            raise ValidationError("payload 必须是对象")
        if not source_ref:
            raise ValidationError("source_ref 不能为空")
        review = self._require_review(review_id)
        occurred = windows.parse_instant(occurred_at, default_tz=windows.get_zone(review.tz_name))

        fp_material = {
            "kind": kind,
            "source_ref": source_ref,
            "occurred_at": occurred,
            "payload": payload,
        }
        fp = fingerprint(fp_material)
        now = self.clock.now()

        with self.repo.begin_immediate():
            existing = self.repo.find_evidence_by_fingerprint(review_id, fp)
            if existing is not None:
                # 保留上报痕迹，但标记为重复，不进入任何计算快照
                duplicate = Evidence(
                    id=self.ids.new_id("ev"),
                    review_id=review_id,
                    kind=kind,
                    source_ref=source_ref,
                    occurred_at=occurred,
                    payload=payload,
                    fingerprint=fp,
                    received_at=now,
                    duplicate_of=existing.id,
                )
                self.repo.insert_evidence(duplicate)
                return duplicate

            evidence = Evidence(
                id=self.ids.new_id("ev"),
                review_id=review_id,
                kind=kind,
                source_ref=source_ref,
                occurred_at=occurred,
                payload=payload,
                fingerprint=fp,
                received_at=now,
            )
            self.repo.insert_evidence(evidence)
        return evidence

    def list_evidence(
        self, actor: Actor, review_id: str, *, include_duplicates: bool = False
    ) -> list[dict[str, Any]]:
        self._require_auth(actor)
        self._require_review(review_id)
        rows = self.repo.list_evidence(review_id)
        views: list[dict[str, Any]] = []
        for ev in rows:
            if ev.is_duplicate() and not include_duplicates:
                continue
            views.append(self._evidence_view(ev, actor))
        return views

    def _evidence_view(self, ev: Evidence, actor: Actor) -> dict[str, Any]:
        payload, redacted, removed = evidence_payload_for(ev.kind, ev.payload, actor)
        return {
            "id": ev.id,
            "kind": ev.kind,
            "source_ref": ev.source_ref,
            "occurred_at": ev.occurred_at.isoformat(),
            "received_at": ev.received_at.isoformat(),
            "fingerprint": ev.fingerprint,
            "duplicate_of": ev.duplicate_of,
            "payload": payload,
            "redacted": redacted,
            "redacted_fields": removed,
        }

    # ---- 版本 -------------------------------------------------------------
    def _parse_specs(self, raw: Any) -> list[MetricSpec]:
        if not isinstance(raw, list) or not raw:
            raise ValidationError("metric_specs 必须是非空数组")
        specs: list[MetricSpec] = []
        seen: set[str] = set()
        for item in raw:
            if not isinstance(item, dict):
                raise ValidationError("指标口径必须是对象")
            spec_id = item.get("spec_id")
            metric_key = item.get("metric_key")
            if not spec_id or not isinstance(spec_id, str):
                raise ValidationError("spec_id 不能为空")
            if spec_id in seen:
                raise ValidationError(f"spec_id 重复: {spec_id}")
            seen.add(spec_id)
            try:
                definition = metrics_engine.get_definition(metric_key)
            except metrics_engine.MetricError as exc:
                raise ValidationError(str(exc)) from exc
            bucket = item.get("bucket") or definition.default_bucket
            if bucket not in ("hour", "day"):
                raise ValidationError(f"不支持的分桶粒度: {bucket}")
            config = item.get("config") or {}
            if not isinstance(config, dict):
                raise ValidationError("config 必须是对象")
            specs.append(
                MetricSpec(
                    spec_id=spec_id,
                    metric_key=metric_key,
                    label=item.get("label") or definition.label,
                    bucket=bucket,
                    config=config,
                )
            )
        return specs

    @staticmethod
    def _spec_to_dict(spec: MetricSpec) -> dict[str, Any]:
        return {
            "spec_id": spec.spec_id,
            "metric_key": spec.metric_key,
            "label": spec.label,
            "bucket": spec.bucket,
            "config": spec.config,
        }

    def create_version(self, actor: Actor, review_id: str, metric_specs: Any) -> ReviewVersion:
        self._require_manage(actor)
        specs = self._parse_specs(metric_specs)
        with self.repo.begin_immediate():
            review = self._require_review(review_id)
            latest = self.repo.latest_version(review_id)
            if latest is not None and latest.status in (
                VersionStatus.DRAFT.value,
                VersionStatus.COMPUTING.value,
            ):
                raise ConflictError(
                    f"版本 v{latest.seq_no} 尚未完成计算，请先续算或等待其完成",
                    code="version_still_computing",
                )
            seq_no = (latest.seq_no + 1) if latest else 1
            snapshot = [e for e in self.repo.list_evidence(review_id) if e.duplicate_of is None]
            inputs_fp = fingerprint([e.fingerprint for e in snapshot])
            version = ReviewVersion(
                id=self.ids.new_id("ver"),
                review_id=review_id,
                seq_no=seq_no,
                status=VersionStatus.COMPUTING.value,
                window={
                    "tz": review.tz_name,
                    "start": review.window_start.isoformat(),
                    "end": review.window_end.isoformat(),
                },
                metric_specs=[self._spec_to_dict(s) for s in specs],
                created_at=self.clock.now(),
                input_evidence_ids=[e.id for e in snapshot],
            )
            version.inputs_fingerprint = inputs_fp
            self.repo.insert_version(version)
        return version

    def list_versions(self, actor: Actor, review_id: str) -> list[ReviewVersion]:
        self._require_auth(actor)
        self._require_review(review_id)
        return self.repo.list_versions(review_id)

    def get_version(self, actor: Actor, version_id: str) -> ReviewVersion:
        self._require_auth(actor)
        return self._require_version(version_id)

    # ---- 计算与续算 --------------------------------------------------------
    def _load_specs(self, version: ReviewVersion) -> list[MetricSpec]:
        return [
            MetricSpec(
                spec_id=s["spec_id"],
                metric_key=s["metric_key"],
                label=s.get("label", ""),
                bucket=s.get("bucket", "hour"),
                config=s.get("config", {}),
            )
            for s in version.metric_specs
        ]

    def compute_version(
        self, actor: Actor, version_id: str, batch_size: int | None = None
    ) -> dict[str, Any]:
        """推进计算。batch_size=None 时一次跑到完成；否则每调用处理一批并落检查点。"""
        self._require_manage(actor)
        if batch_size is not None and batch_size <= 0:
            raise ValidationError("batch_size 必须为正整数")
        version = self.repo.get_version(version_id)
        if version is None:
            raise NotFoundError(f"复盘版本不存在: {version_id}")
        total = len(version.input_evidence_ids)
        processed = 0
        done = False
        while True:
            done, batch_count = self._advance(version_id, batch_size)
            processed += batch_count
            if done or batch_size is not None:
                break
        version = self.repo.get_version(version_id)
        if not done:
            state = self.repo.get_compute_state(version_id)
            processed = _processed_count(version, self.repo, state[0] if state else 0)
        return {
            "done": done,
            "status": version.status,
            "total": total,
            "processed": total if done else processed,
            "cursor": version.computed_cursor,
        }

    def _advance(self, version_id: str, batch_size: int | None) -> tuple[bool, int]:
        """在单个立即事务中处理一批证据并提交检查点。返回 (是否完成, 本批条数)。"""
        with self.repo.begin_immediate():
            version = self.repo.get_version(version_id)
            if version is None:
                raise NotFoundError(f"复盘版本不存在: {version_id}")
            if version.status == VersionStatus.READY.value:
                return True, 0
            if version.status != VersionStatus.COMPUTING.value:
                raise ConflictError(
                    f"版本状态为 {version.status}，不可计算", code="version_not_computing"
                )

            snapshot = set(version.input_evidence_ids)
            saved = self.repo.get_compute_state(version_id)
            cursor = saved[0] if saved else 0
            states: dict[str, dict] = (
                saved[1] if saved else {s["spec_id"]: {} for s in version.metric_specs}
            )

            pending = [
                e
                for e in self.repo.list_evidence_after(version.review_id, cursor)
                if e.id in snapshot
            ]
            batch = pending if batch_size is None else pending[:batch_size]

            if not batch:
                self._finalize(version, states)
                self.repo.delete_compute_state(version_id)
                return True, 0

            specs = self._load_specs(version)
            start = _parse_window_dt(version.window["start"])
            end = _parse_window_dt(version.window["end"])
            tz_name = version.window["tz"]
            for spec in specs:
                state = states.setdefault(spec.spec_id, {})
                metrics_engine.collect_into(state, spec, batch, start, end, tz_name)

            new_cursor = max(e.seq for e in batch)
            version.computed_cursor = new_cursor
            self.repo.update_version(version)
            self.repo.save_compute_state(version_id, new_cursor, states)

            # 快照内已无剩余证据：同事务内直接定稿，避免末批还要再调用一次
            remaining = [
                e
                for e in self.repo.list_evidence_after(version.review_id, new_cursor)
                if e.id in snapshot
            ]
            if not remaining:
                self._finalize(version, states)
                self.repo.delete_compute_state(version_id)
                return True, len(batch)
            return False, len(batch)

    def _finalize(self, version: ReviewVersion, states: dict[str, dict]) -> None:
        specs = self._load_specs(version)
        start = _parse_window_dt(version.window["start"])
        end = _parse_window_dt(version.window["end"])
        tz_name = version.window["tz"]
        results = []
        for spec in specs:
            result = metrics_engine.finalize(spec, states.get(spec.spec_id, {}), start, end, tz_name)
            results.append(result.to_dict())
        version.results = results
        version.status = VersionStatus.READY.value
        version.computed_cursor = max(
            (self.repo.get_evidence(eid).seq for eid in version.input_evidence_ids),
            default=0,
        )
        version.result_fingerprint = fingerprint(
            {"specs": version.metric_specs, "results": results}
        )
        self.repo.update_version(version)

    def recover_interrupted(self) -> list[dict[str, Any]]:
        """进程重启后把所有 computing 版本续算到完成（输入指纹不变，结果一致）。"""
        recovered = []
        for version in self.repo.list_computing_versions():
            while True:
                done, _ = self._advance(version.id, None)
                if done:
                    break
            recovered.append({"version_id": version.id, "review_id": version.review_id})
        return recovered

    # ---- 签发（并发安全）---------------------------------------------------
    def sign_version(self, actor: Actor, version_id: str) -> ReviewVersion:
        self._require_auth(actor)
        if not actor.can_sign():
            raise PermissionDeniedError("无权签发复盘结论")
        with self.repo.begin_immediate():
            version = self.repo.get_version(version_id)
            if version is None:
                raise NotFoundError(f"复盘版本不存在: {version_id}")
            if version.status == VersionStatus.SIGNED.value:
                raise ConflictError("该版本已签发，结论不可修改", code="already_signed")
            siblings = self.repo.list_versions(version.review_id)
            newer_signed = [
                v
                for v in siblings
                if v.seq_no > version.seq_no
                and v.status in (VersionStatus.SIGNED.value, VersionStatus.SUPERSEDED.value)
                and v.signed_at is not None
            ]
            if newer_signed:
                raise ConflictError(
                    "已有更高版本签发，旧版本不能再签发", code="newer_version_signed"
                )
            if version.status == VersionStatus.SUPERSEDED.value:
                raise ConflictError("该版本已被新版替代", code="version_superseded")
            if version.status != VersionStatus.READY.value:
                raise ConflictError(
                    f"版本状态为 {version.status}，计算完成前不可签发",
                    code="version_not_ready",
                )
            version.status = VersionStatus.SIGNED.value
            version.signed_at = self.clock.now()
            version.signed_by = actor.name
            self.repo.update_version(version)

            # 同复盘更早的已签发版本转为被替代，但其结果与指纹原样保留
            for older in siblings:
                if older.seq_no < version.seq_no and older.status == VersionStatus.SIGNED.value:
                    older.status = VersionStatus.SUPERSEDED.value
                    older.superseded_by = version.id
                    self.repo.update_version(older)
            return version

    # ---- 版本差异 ----------------------------------------------------------
    def diff_versions(
        self, actor: Actor, review_id: str, from_seq: int, to_seq: int
    ) -> dict[str, Any]:
        self._require_auth(actor)
        old = self.repo.get_version_by_seq(review_id, from_seq)
        new = self.repo.get_version_by_seq(review_id, to_seq)
        if old is None or new is None:
            raise NotFoundError("指定的版本序号不存在")
        return self._diff(old, new)

    def _diff(self, old: ReviewVersion, new: ReviewVersion) -> dict[str, Any]:
        old_ev = self._evidence_index(old)
        new_ev = self._evidence_index(new)
        only_new = [
            self._evidence_ref(e)
            for eid, e in new_ev.items()
            if eid not in old_ev
        ]
        only_old = [
            self._evidence_ref(e)
            for eid, e in old_ev.items()
            if eid not in new_ev
        ]

        old_specs = {s["spec_id"]: s for s in old.metric_specs}
        new_specs = {s["spec_id"]: s for s in new.metric_specs}
        old_results = {r["spec_id"]: r for r in old.results}
        new_results = {r["spec_id"]: r for r in new.results}

        metric_changes = []
        for spec_id in sorted(set(old_specs) | set(new_specs)):
            if spec_id not in old_specs:
                metric_changes.append({"spec_id": spec_id, "change": "added", "new": new_results.get(spec_id)})
                continue
            if spec_id not in new_specs:
                metric_changes.append({"spec_id": spec_id, "change": "removed", "old": old_results.get(spec_id)})
                continue
            old_r = old_results.get(spec_id)
            new_r = new_results.get(spec_id)
            if old_r is None or new_r is None:
                metric_changes.append(
                    {"spec_id": spec_id, "change": "added" if old_r is None else "removed"}
                )
                continue
            changed_buckets = {}
            for label in sorted(set(old_r["buckets"]) | set(new_r["buckets"])):
                ov = old_r["buckets"].get(label)
                nv = new_r["buckets"].get(label)
                if ov != nv:
                    changed_buckets[label] = {
                        "old": ov,
                        "new": nv,
                        "delta": _delta(nv, ov),
                    }
            metric_changes.append(
                {
                    "spec_id": spec_id,
                    "metric_key": new_r["metric_key"],
                    "change": "unchanged"
                    if not changed_buckets and old_r["total"] == new_r["total"]
                    else "changed",
                    "total": {"old": old_r["total"], "new": new_r["total"],
                              "delta": _delta(new_r["total"], old_r["total"])},
                    "changed_buckets": changed_buckets,
                }
            )

        return {
            "review_id": new.review_id,
            "from": self._version_ref(old),
            "to": self._version_ref(new),
            "inputs": {
                "old_count": len(old_ev),
                "new_count": len(new_ev),
                "only_in_new": only_new,
                "only_in_old": only_old,
                "same_fingerprint": old.inputs_fingerprint == new.inputs_fingerprint,
            },
            "metric_changes": metric_changes,
            "old_version_immutable": old.is_immutable
            or old.status == VersionStatus.SUPERSEDED.value,
        }

    def _evidence_index(self, version: ReviewVersion) -> dict[str, Evidence]:
        index = {}
        for eid in version.input_evidence_ids:
            ev = self.repo.get_evidence(eid)
            if ev is not None:
                index[eid] = ev
        return index

    @staticmethod
    def _evidence_ref(ev: Evidence) -> dict[str, Any]:
        return {
            "id": ev.id,
            "kind": ev.kind,
            "source_ref": ev.source_ref,
            "occurred_at": ev.occurred_at.isoformat(),
            "fingerprint": ev.fingerprint,
        }

    @staticmethod
    def _version_ref(version: ReviewVersion) -> dict[str, Any]:
        return {
            "version_id": version.id,
            "seq_no": version.seq_no,
            "status": version.status,
            "inputs_fingerprint": version.inputs_fingerprint,
            "result_fingerprint": version.result_fingerprint,
            "signed_by": version.signed_by,
        }

    # ---- 复核 -------------------------------------------------------------
    def raise_challenge(
        self,
        actor: Actor,
        review_id: str,
        version_id: str,
        reason: str,
        metric_key: str | None = None,
    ) -> Challenge:
        self._require_auth(actor)
        if not reason or not reason.strip():
            raise ValidationError("复核理由不能为空")
        version = self._require_version(version_id)
        if version.review_id != review_id:
            raise ValidationError("复核版本与复盘不匹配")
        challenge = Challenge(
            id=self.ids.new_id("ch"),
            review_id=review_id,
            version_id=version_id,
            metric_key=metric_key,
            reason=reason.strip(),
            raised_by=actor.name,
            status=ChallengeStatus.OPEN.value,
            created_at=self.clock.now(),
        )
        with self.repo.begin_immediate():
            self.repo.insert_challenge(challenge)
        return challenge

    def resolve_challenge(
        self, actor: Actor, challenge_id: str, resolution: str, approve: bool
    ) -> Challenge:
        self._require_auth(actor)
        if not actor.can_resolve_challenge():
            raise PermissionDeniedError("无权处理复核")
        if not resolution or not resolution.strip():
            raise ValidationError("处理结论不能为空")
        with self.repo.begin_immediate():
            challenge = self.repo.get_challenge(challenge_id)
            if challenge is None:
                raise NotFoundError(f"复核不存在: {challenge_id}")
            if challenge.status != ChallengeStatus.OPEN.value:
                raise ConflictError("该复核已处理", code="challenge_closed")
            challenge.status = (
                ChallengeStatus.RESOLVED.value if approve else ChallengeStatus.REJECTED.value
            )
            challenge.resolved_at = self.clock.now()
            challenge.resolution = resolution.strip()
            self.repo.update_challenge(challenge)
            return challenge

    def list_challenges(self, actor: Actor, review_id: str | None = None) -> list[Challenge]:
        self._require_auth(actor)
        return self.repo.list_challenges(review_id)

    # ---- 导出 -------------------------------------------------------------
    def export_version(self, actor: Actor, version_id: str) -> dict[str, Any]:
        """机器可读导出：结果为聚合数值；证据只出非个体元数据与输入指纹。"""
        self._require_auth(actor)
        version = self._require_version(version_id)
        review = self._require_review(version.review_id)
        evidence_refs = []
        pii_included = False
        for eid in version.input_evidence_ids:
            ev = self.repo.get_evidence(eid)
            if ev is None:
                continue
            ref = self._evidence_ref(ev)
            ref["contains_pii"] = kind_has_pii(ev.kind)
            if ref["contains_pii"]:
                pii_included = True
            evidence_refs.append(ref)

        return {
            "schema": "holiday-charging-review-export/1",
            "exported_at": self.clock.now().isoformat(),
            "exported_by": actor.name,
            "review": {
                "id": review.id,
                "name": review.name,
                "tz": review.tz_name,
                "window_start": review.window_start.isoformat(),
                "window_end": review.window_end.isoformat(),
            },
            "version": {
                "version_id": version.id,
                "seq_no": version.seq_no,
                "status": version.status,
                "created_at": version.created_at.isoformat(),
                "window": version.window,
                "metric_specs": version.metric_specs,
                "results": version.results,
                "input_evidence": evidence_refs,
                "inputs_fingerprint": version.inputs_fingerprint,
                "result_fingerprint": version.result_fingerprint,
                "signed_at": version.signed_at.isoformat() if version.signed_at else None,
                "signed_by": version.signed_by,
                "superseded_by": version.superseded_by,
            },
            "privacy": {
                "pii_evidence_present": pii_included,
                "payload_included": False,
                "note": "导出仅含聚合结果与证据指纹；个体明细需 read_pii 权限走证据接口",
            },
        }


def _parse_window_dt(value: str) -> datetime:
    return windows.parse_instant(value)


def _delta(new: float | None, old: float | None) -> float | None:
    if new is None or old is None:
        return None
    return new - old


def _processed_count(version: ReviewVersion, repo: Repository, cursor: int) -> int:
    snapshot = set(version.input_evidence_ids)
    remaining = sum(
        1
        for e in repo.list_evidence_after(version.review_id, cursor)
        if e.id in snapshot
    )
    return len(snapshot) - remaining
