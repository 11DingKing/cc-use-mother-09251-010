"""应用服务：复盘生命周期、证据补录、计算续算、签发、复核与导出。

关键语义：
- 证据按 (复盘, 类型, 来源, 外部编号) 去重，重放幂等，内容变化判冲突；
- 每次计算冻结输入清单与口径快照并记录输入指纹，同输入必同指纹；
- 资料迟到后重新计算产生新版本，已签发版本永不改变；
- 计算按指标分步落库，中断后可按原输入续算；
- 复核按版本冻结的输入重算并比对指纹与结果。
"""
from __future__ import annotations

from ..domain.errors import (
    ComputationInterrupted,
    ConflictError,
    NotFoundError,
    ValidationError,
)
from ..domain.fingerprint import (
    canonical_json,
    content_hash,
    input_fingerprint,
    manifest_hash,
    sha256_hex,
    step_fingerprint,
)
from ..domain.metrics import build_metric_result, metric_key, validate_definition
from ..domain.models import (
    EVIDENCE_KINDS,
    RECHECK_MATCH,
    RECHECK_MISMATCH,
    RUN_DONE,
    RUN_INTERRUPTED,
    RUN_RUNNING,
    VERSION_DRAFT,
    CalculationRun,
    CalculationStep,
    Evidence,
    MetricDefinition,
    Recheck,
    Review,
    ReviewVersion,
)
from ..domain.windows import HolidayWindow, format_instant, parse_instant
from ..persistence.sqlite_store import DuplicateKeyError, SQLiteStore
from ..ports import Clock, IdGenerator, SystemClock, SystemIds
from .auth import Principal, redact_payload, require


def _manifest_entry(evidence: Evidence) -> dict:
    return {
        "id": evidence.id,
        "kind": evidence.kind,
        "source": evidence.source,
        "external_id": evidence.external_id,
        "occurred_at": format_instant(evidence.occurred_at),
        "content_hash": evidence.content_hash,
    }


def _count_by(entries: list[dict], field: str) -> dict[str, int]:
    counts: dict[str, int] = {}
    for entry in entries:
        counts[entry[field]] = counts.get(entry[field], 0) + 1
    return counts


class ReviewService:
    def __init__(
        self,
        store: SQLiteStore,
        clock: Clock | None = None,
        ids: IdGenerator | None = None,
    ) -> None:
        self._store = store
        self._clock = clock or SystemClock()
        self._ids = ids or SystemIds()

    def _now(self) -> str:
        return format_instant(self._clock.now())

    def _must_review(self, review_id: str) -> Review:
        review = self._store.get_review(review_id)
        if review is None:
            raise NotFoundError(f"复盘不存在: {review_id}")
        return review

    def _must_version(self, review_id: str, version_no: int) -> ReviewVersion:
        version = self._store.get_version(review_id, version_no)
        if version is None:
            raise NotFoundError(f"复盘版本不存在: v{version_no}")
        return version

    def _load_defs(self, def_ids: object) -> list[MetricDefinition]:
        if not isinstance(def_ids, list) or not all(isinstance(item, str) for item in def_ids):
            raise ValidationError("metric_def_ids 必须是字符串数组")
        definitions = []
        for def_id in dict.fromkeys(def_ids):
            found = self._store.get_metric_definition(def_id)
            if found is None:
                raise ValidationError(f"指标口径不存在: {def_id}")
            definitions.append(found)
        return definitions

    @staticmethod
    def _parse_window(spec: object, field: str) -> HolidayWindow:
        try:
            return HolidayWindow.from_spec(spec)
        except ValueError as exc:
            raise ValidationError(f"{field} 无效: {exc}") from exc

    # ---- 指标口径 ----

    def create_metric_definition(self, principal: Principal, raw_definition: object) -> dict:
        require(principal, "metrics:write")
        definition = validate_definition(raw_definition, evidence_kinds=EVIDENCE_KINDS)
        version = definition["version"]
        if version is None:
            version = self._store.latest_metric_version(definition["name"]) + 1
        if self._store.find_metric_definition(definition["name"], version) is not None:
            raise ConflictError(f"指标口径 {definition['name']}@v{version} 已存在")
        definition["version"] = version
        digest = "sha256:" + sha256_hex(canonical_json(definition))
        record = MetricDefinition(
            id=self._ids.new_id("md"),
            definition=definition,
            definition_hash=digest,
            created_at=self._now(),
            created_by=principal.name,
        )
        self._store.insert_metric_definition(record)
        return record.to_dict()

    def list_metric_definitions(self, principal: Principal) -> list[dict]:
        require(principal, "review:read")
        return [record.to_dict() for record in self._store.list_metric_definitions()]

    # ---- 复盘 ----

    def create_review(
        self,
        principal: Principal,
        *,
        region: object,
        name: object,
        window: object,
        baseline: object = None,
        metric_def_ids: object = (),
    ) -> dict:
        require(principal, "review:write")
        if not isinstance(region, str) or not region.strip():
            raise ValidationError("region 不能为空")
        if not isinstance(name, str) or not name.strip():
            raise ValidationError("name 不能为空")
        holiday = self._parse_window(window, "window")
        base = self._parse_window(baseline, "baseline") if baseline is not None else None
        definitions = self._load_defs(list(metric_def_ids or []))
        review = Review(
            id=self._ids.new_id("rev"),
            region=region.strip(),
            name=name.strip(),
            window=holiday,
            baseline=base,
            metric_def_ids=[record.id for record in definitions],
            created_at=self._now(),
            created_by=principal.name,
        )
        self._store.insert_review(review)
        return review.to_dict()

    def attach_metrics(self, principal: Principal, review_id: str, metric_def_ids: object) -> dict:
        """追加绑定口径，从下一个计算版本起生效（旧版本仍按各自快照复核）。"""
        require(principal, "review:write")
        review = self._must_review(review_id)
        definitions = self._load_defs(metric_def_ids)
        merged = list(review.metric_def_ids)
        for record in definitions:
            if record.id not in merged:
                merged.append(record.id)
        self._store.update_review_metric_defs(review_id, merged)
        return self.get_review_detail(principal, review_id)

    def get_review_detail(self, principal: Principal, review_id: str) -> dict:
        require(principal, "review:read")
        review = self._must_review(review_id)
        versions = self._store.list_versions(review_id)
        evidence_count = self._store.count_evidence(review_id)
        latest = versions[-1] if versions else None
        if latest is None:
            uncomputed = evidence_count
        else:
            computed_ids = {entry["id"] for entry in latest.manifest}
            uncomputed = sum(
                1
                for evidence in self._store.list_evidence(review_id)
                if evidence.id not in computed_ids
            )
        payload = review.to_dict()
        payload.update({
            "versions": [version.summary_dict() for version in versions],
            "evidence_count": evidence_count,
            "uncomputed_evidence_count": uncomputed,
        })
        return payload

    # ---- 证据补录与去重 ----

    def add_evidence(self, principal: Principal, review_id: str, items: object) -> dict:
        require(principal, "evidence:write")
        self._must_review(review_id)
        if not isinstance(items, list) or not items:
            raise ValidationError("证据批次必须是非空数组")
        result: dict[str, list] = {"added": [], "duplicates": [], "conflicts": [], "errors": []}
        for position, item in enumerate(items):
            try:
                candidate = self._build_evidence(review_id, item)
            except ValidationError as exc:
                result["errors"].append({"position": position, "message": exc.message})
                continue
            existing = self._store.get_evidence_by_key(
                review_id, candidate.kind, candidate.source, candidate.external_id
            )
            if existing is not None:
                self._classify_duplicate(existing, candidate, result)
                continue
            try:
                self._store.insert_evidence(candidate)
            except DuplicateKeyError:
                # 并发补录由唯一约束兜底：重新读取后按去重/冲突归类
                existing = self._store.get_evidence_by_key(
                    review_id, candidate.kind, candidate.source, candidate.external_id
                )
                self._classify_duplicate(existing, candidate, result)
                continue
            result["added"].append(candidate.to_dict())
        return result

    @staticmethod
    def _classify_duplicate(existing: Evidence, candidate: Evidence, result: dict) -> None:
        if existing is not None and existing.content_hash == candidate.content_hash:
            result["duplicates"].append(existing.to_dict())
        else:
            result["conflicts"].append({
                "kind": candidate.kind,
                "source": candidate.source,
                "external_id": candidate.external_id,
                "existing_id": existing.id if existing else None,
                "message": "相同业务键的证据内容不一致，未覆盖原记录",
            })

    def _build_evidence(self, review_id: str, item: object) -> Evidence:
        if not isinstance(item, dict):
            raise ValidationError("证据必须是对象")
        kind = item.get("kind")
        if kind not in EVIDENCE_KINDS:
            raise ValidationError(
                f"未知证据类型: {kind!r}", details={"allowed": list(EVIDENCE_KINDS)}
            )
        source, external_id = item.get("source"), item.get("external_id")
        if not (isinstance(source, str) and source.strip()):
            raise ValidationError("source 不能为空")
        if not (isinstance(external_id, str) and external_id.strip()):
            raise ValidationError("external_id 不能为空")
        try:
            occurred = parse_instant(item.get("occurred_at"))
        except ValueError as exc:
            raise ValidationError(f"occurred_at 无效: {exc}") from exc
        payload = item.get("payload")
        if not isinstance(payload, dict):
            raise ValidationError("payload 必须是对象")
        occurred_text = format_instant(occurred)
        digest = content_hash(kind, source, external_id, occurred_text, payload)
        return Evidence(
            id=self._ids.new_id("ev"),
            review_id=review_id,
            kind=kind,
            source=source,
            external_id=external_id,
            occurred_at=occurred,
            ingested_at=self._clock.now(),
            payload=payload,
            content_hash=digest,
        )

    def list_evidence(self, principal: Principal, review_id: str, kind: str | None = None) -> list[dict]:
        require(principal, "review:read")
        self._must_review(review_id)
        if kind is not None and kind not in EVIDENCE_KINDS:
            raise ValidationError(
                f"未知证据类型: {kind!r}", details={"allowed": list(EVIDENCE_KINDS)}
            )
        can_see_pii = "pii:read" in principal.scopes
        output = []
        for evidence in self._store.list_evidence(review_id, kind):
            data = evidence.to_dict()
            if not can_see_pii:
                data["payload"] = redact_payload(evidence.kind, evidence.payload)
                data["redacted"] = True
            output.append(data)
        return output

    # ---- 计算与续算 ----

    def compute(self, principal: Principal, review_id: str, step_hook=None) -> dict:
        """按当前全部证据计算新版本；输入未变化时不产生新版本。"""
        require(principal, "compute:run")
        review = self._must_review(review_id)
        definitions = self._load_defs(review.metric_def_ids)
        if not definitions:
            raise ValidationError("复盘未绑定任何指标口径，无法计算")
        evidence = self._store.list_evidence(review_id)
        manifest = [_manifest_entry(item) for item in evidence]
        snapshots = [record.snapshot() for record in definitions]
        baseline_spec = review.baseline.to_spec() if review.baseline else None
        fingerprint = input_fingerprint(
            review.window.to_spec(), baseline_spec, snapshots, manifest
        )
        latest = self._store.latest_version(review_id)
        if latest is not None and latest.fingerprint == fingerprint:
            return {
                "unchanged": True,
                "run_id": None,
                "version_no": latest.version_no,
                "fingerprint": fingerprint,
            }
        run = CalculationRun(
            id=self._ids.new_id("run"),
            review_id=review_id,
            status=RUN_RUNNING,
            fingerprint=fingerprint,
            manifest=manifest,
            metric_defs=snapshots,
            window_spec=review.window.to_spec(),
            baseline_spec=baseline_spec,
            error=None,
            created_at=self._now(),
            finished_at=None,
        )
        self._store.insert_run(run)
        try:
            self._execute_steps(run, evidence, step_hook)
        except Exception as exc:
            self._store.update_run_status(run.id, RUN_INTERRUPTED, error=str(exc))
            raise ComputationInterrupted(run.id, str(exc)) from exc
        version = self._finalize_run(run)
        return {
            "unchanged": False,
            "run_id": run.id,
            "version_no": version.version_no,
            "fingerprint": fingerprint,
        }

    def resume(self, principal: Principal, run_id: str, step_hook=None) -> dict:
        """续算被中断的运行：沿用冻结的输入清单，跳过已完成步骤。"""
        require(principal, "compute:run")
        run = self._store.get_run(run_id)
        if run is None:
            raise NotFoundError(f"计算运行不存在: {run_id}")
        if run.status == RUN_DONE:
            version = self._store.get_version_by_run(run.id)
            return {
                "resumed": False,
                "run_id": run.id,
                "version_no": version.version_no if version else None,
                "fingerprint": run.fingerprint,
            }
        if run.status == RUN_RUNNING:
            raise ConflictError("计算运行仍在执行中，不能并发续算")
        evidence = self._store.list_evidence_by_ids([entry["id"] for entry in run.manifest])
        if len(evidence) != len(run.manifest):
            raise ConflictError("证据清单与库内记录不一致，无法续算")
        self._store.update_run_status(run.id, RUN_RUNNING)
        try:
            self._execute_steps(run, evidence, step_hook)
        except Exception as exc:
            self._store.update_run_status(run.id, RUN_INTERRUPTED, error=str(exc))
            raise ComputationInterrupted(run.id, str(exc)) from exc
        version = self._finalize_run(run)
        return {
            "resumed": True,
            "run_id": run.id,
            "version_no": version.version_no,
            "fingerprint": run.fingerprint,
        }

    def _execute_steps(self, run: CalculationRun, evidence: list[Evidence], step_hook) -> None:
        window = HolidayWindow.from_spec(run.window_spec)
        baseline = HolidayWindow.from_spec(run.baseline_spec) if run.baseline_spec else None
        done = {
            step.metric_key
            for step in self._store.list_steps(run.id)
            if step.status == "done"
        }
        for entry in run.metric_defs:
            definition = entry["definition"]
            key = metric_key(definition)
            if key in done:
                continue
            if step_hook is not None:
                step_hook(run.id, key)
            result = build_metric_result(definition, evidence, window, baseline)
            self._store.upsert_step(CalculationStep(
                run_id=run.id,
                metric_key=key,
                status="done",
                result=result,
                fingerprint=step_fingerprint(entry, run.window_spec, run.baseline_spec, run.manifest),
                computed_at=self._now(),
            ))

    def _finalize_run(self, run: CalculationRun) -> ReviewVersion:
        steps = self._store.list_steps(run.id)
        results = {step.metric_key: step.result for step in steps}
        version = ReviewVersion(
            id=self._ids.new_id("ver"),
            review_id=run.review_id,
            version_no=0,
            run_id=run.id,
            status=VERSION_DRAFT,
            fingerprint=run.fingerprint,
            manifest=run.manifest,
            metric_defs=run.metric_defs,
            results=results,
            window_spec=run.window_spec,
            baseline_spec=run.baseline_spec,
            created_at=self._now(),
            issued_at=None,
            issued_by=None,
            issued_seq=None,
        )
        version.version_no = self._store.insert_version(version)
        self._store.update_run_status(run.id, RUN_DONE, finished_at=self._now())
        return version

    def recover_interrupted(self) -> int:
        """服务启动时调用：把遗留的运行中任务标记为可续算。"""
        return self._store.mark_running_runs_interrupted("服务重启，运行被标记为可续算")

    def get_run(self, principal: Principal, run_id: str) -> dict:
        require(principal, "review:read")
        run = self._store.get_run(run_id)
        if run is None:
            raise NotFoundError(f"计算运行不存在: {run_id}")
        payload = run.to_dict()
        payload["steps"] = [
            {
                "metric_key": step.metric_key,
                "status": step.status,
                "fingerprint": step.fingerprint,
                "computed_at": step.computed_at,
            }
            for step in self._store.list_steps(run.id)
        ]
        return payload

    # ---- 版本、签发、差异 ----

    def list_versions(self, principal: Principal, review_id: str) -> list[dict]:
        require(principal, "review:read")
        self._must_review(review_id)
        return [version.summary_dict() for version in self._store.list_versions(review_id)]

    def get_version(self, principal: Principal, review_id: str, version_no: int) -> dict:
        require(principal, "review:read")
        self._must_review(review_id)
        return self._must_version(review_id, version_no).detail_dict()

    def issue_version(self, principal: Principal, review_id: str, version_no: int) -> dict:
        """签发版本：条件更新保证并发下只有一个调用成功，签发后不可更改。"""
        require(principal, "review:issue")
        self._must_review(review_id)
        version = self._must_version(review_id, version_no)
        issued = self._store.issue_version(version.id, review_id, principal.name, self._now())
        if not issued:
            raise ConflictError(f"版本 v{version_no} 已签发，签发结论不可更改")
        return self._must_version(review_id, version_no).detail_dict()

    def diff_versions(
        self, principal: Principal, review_id: str, from_no: int, to_no: int
    ) -> dict:
        """比较两个版本：指标分桶变化 + 证据增减（差异来源，含迟到资料）。"""
        require(principal, "review:read")
        self._must_review(review_id)
        source = self._must_version(review_id, from_no)
        target = self._must_version(review_id, to_no)
        source_ids = {entry["id"] for entry in source.manifest}
        target_ids = {entry["id"] for entry in target.manifest}
        added = [entry for entry in target.manifest if entry["id"] not in source_ids]
        removed = [entry for entry in source.manifest if entry["id"] not in target_ids]
        metric_changes = []
        for key in sorted(set(source.results) | set(target.results)):
            before = source.results.get(key)
            after = target.results.get(key)
            if before is None or after is None:
                metric_changes.append({"metric": key, "change": "added" if after else "removed"})
                continue
            before_buckets = {b["index"]: b for b in before["holiday"]["buckets"]}
            bucket_changes = []
            for bucket in after["holiday"]["buckets"]:
                previous = before_buckets.get(bucket["index"])
                if previous is None or previous["value"] != bucket["value"]:
                    bucket_changes.append({
                        "bucket": bucket["index"],
                        "label": bucket["label"],
                        "from": previous["value"] if previous else None,
                        "to": bucket["value"],
                    })
            total_change = None
            if before["holiday"]["total"]["value"] != after["holiday"]["total"]["value"]:
                total_change = {
                    "from": before["holiday"]["total"]["value"],
                    "to": after["holiday"]["total"]["value"],
                }
            if bucket_changes or total_change:
                metric_changes.append({
                    "metric": key,
                    "buckets": bucket_changes,
                    "total": total_change,
                })
        return {
            "review_id": review_id,
            "from": from_no,
            "to": to_no,
            "fingerprint": {"from": source.fingerprint, "to": target.fingerprint},
            "evidence": {
                "added": added,
                "removed": removed,
                "added_by_kind": _count_by(added, "kind"),
                "removed_by_kind": _count_by(removed, "kind"),
            },
            "metrics": metric_changes,
        }

    # ---- 复核 ----

    def recheck_version(self, principal: Principal, review_id: str, version_no: int) -> dict:
        """复核：按版本冻结的清单与口径重算，比对证据哈希、结果与输入指纹。"""
        require(principal, "recheck:run")
        self._must_review(review_id)
        version = self._must_version(review_id, version_no)
        problems = []
        current = self._store.list_evidence_by_ids(
            [entry["id"] for entry in version.manifest]
        )
        by_id = {evidence.id: evidence for evidence in current}
        for entry in version.manifest:
            evidence = by_id.get(entry["id"])
            if evidence is None:
                problems.append({"type": "missing_evidence", "evidence_id": entry["id"]})
                continue
            recomputed = content_hash(
                evidence.kind,
                evidence.source,
                evidence.external_id,
                format_instant(evidence.occurred_at),
                evidence.payload,
            )
            if recomputed != entry["content_hash"]:
                problems.append({"type": "hash_mismatch", "evidence_id": entry["id"]})
        window = HolidayWindow.from_spec(version.window_spec)
        baseline = (
            HolidayWindow.from_spec(version.baseline_spec) if version.baseline_spec else None
        )
        ordered = [by_id[entry["id"]] for entry in version.manifest if entry["id"] in by_id]
        metric_reports = []
        for entry in version.metric_defs:
            definition = entry["definition"]
            key = metric_key(definition)
            recomputed = build_metric_result(definition, ordered, window, baseline)
            metric_reports.append({
                "metric": key,
                "result_match": version.results.get(key) == recomputed,
            })
        fingerprint = input_fingerprint(
            version.window_spec, version.baseline_spec, version.metric_defs, version.manifest
        )
        fingerprint_match = fingerprint == version.fingerprint
        matched = (
            not problems
            and fingerprint_match
            and all(report["result_match"] for report in metric_reports)
        )
        record = Recheck(
            id=self._ids.new_id("chk"),
            version_id=version.id,
            rechecked_by=principal.name,
            result=RECHECK_MATCH if matched else RECHECK_MISMATCH,
            details={
                "fingerprint": {
                    "expected": version.fingerprint,
                    "actual": fingerprint,
                    "match": fingerprint_match,
                },
                "metrics": metric_reports,
                "problems": problems,
            },
            created_at=self._now(),
        )
        self._store.insert_recheck(record)
        return record.to_dict()

    def list_rechecks(self, principal: Principal, review_id: str, version_no: int) -> list[dict]:
        require(principal, "review:read")
        self._must_review(review_id)
        version = self._must_version(review_id, version_no)
        return [record.to_dict() for record in self._store.list_rechecks(version.id)]

    # ---- 导出 ----

    def export_version(self, principal: Principal, review_id: str, version_no: int) -> dict:
        """导出机器可读结果：口径快照、输入指纹、清单哈希、分桶结果与复核史。"""
        require(principal, "export:read")
        review = self._must_review(review_id)
        version = self._must_version(review_id, version_no)
        return {
            "schema": "service_09251_010.export/v1",
            "exported_at": self._now(),
            "review": review.to_dict(),
            "version": version.detail_dict(),
            "input_manifest_hash": manifest_hash(version.manifest),
            "rechecks": [
                record.to_dict() for record in self._store.list_rechecks(version.id)
            ],
        }
