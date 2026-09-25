"""SQLite 持久化：单连接 + 写锁，所有写操作串行化。

签发等关键状态迁移使用条件 UPDATE，保证并发下只有一次成功；
版本号分配在写锁内完成，保证单调不重号。
"""
from __future__ import annotations

import json
import sqlite3
import threading

from ..domain.models import (
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

SCHEMA = """
CREATE TABLE IF NOT EXISTS reviews (
    id TEXT PRIMARY KEY,
    region TEXT NOT NULL,
    name TEXT NOT NULL,
    window_json TEXT NOT NULL,
    baseline_json TEXT,
    metric_def_ids_json TEXT NOT NULL,
    created_at TEXT NOT NULL,
    created_by TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS metric_definitions (
    id TEXT PRIMARY KEY,
    name TEXT NOT NULL,
    version INTEGER NOT NULL,
    definition_json TEXT NOT NULL,
    definition_hash TEXT NOT NULL,
    created_at TEXT NOT NULL,
    created_by TEXT NOT NULL,
    UNIQUE(name, version)
);
CREATE TABLE IF NOT EXISTS evidence (
    id TEXT PRIMARY KEY,
    review_id TEXT NOT NULL REFERENCES reviews(id),
    kind TEXT NOT NULL,
    source TEXT NOT NULL,
    external_id TEXT NOT NULL,
    occurred_at TEXT NOT NULL,
    ingested_at TEXT NOT NULL,
    payload_json TEXT NOT NULL,
    content_hash TEXT NOT NULL,
    UNIQUE(review_id, kind, source, external_id)
);
CREATE INDEX IF NOT EXISTS idx_evidence_review ON evidence(review_id, occurred_at);
CREATE TABLE IF NOT EXISTS calculation_runs (
    id TEXT PRIMARY KEY,
    review_id TEXT NOT NULL REFERENCES reviews(id),
    status TEXT NOT NULL,
    fingerprint TEXT NOT NULL,
    manifest_json TEXT NOT NULL,
    metric_defs_json TEXT NOT NULL,
    window_json TEXT NOT NULL,
    baseline_json TEXT,
    error TEXT,
    created_at TEXT NOT NULL,
    finished_at TEXT
);
CREATE TABLE IF NOT EXISTS calculation_steps (
    run_id TEXT NOT NULL REFERENCES calculation_runs(id),
    metric_key TEXT NOT NULL,
    status TEXT NOT NULL,
    result_json TEXT NOT NULL,
    fingerprint TEXT NOT NULL,
    computed_at TEXT NOT NULL,
    PRIMARY KEY(run_id, metric_key)
);
CREATE TABLE IF NOT EXISTS review_versions (
    id TEXT PRIMARY KEY,
    review_id TEXT NOT NULL REFERENCES reviews(id),
    version_no INTEGER NOT NULL,
    run_id TEXT NOT NULL REFERENCES calculation_runs(id),
    status TEXT NOT NULL,
    fingerprint TEXT NOT NULL,
    manifest_json TEXT NOT NULL,
    metric_defs_json TEXT NOT NULL,
    results_json TEXT NOT NULL,
    window_json TEXT NOT NULL,
    baseline_json TEXT,
    created_at TEXT NOT NULL,
    issued_at TEXT,
    issued_by TEXT,
    issued_seq INTEGER,
    UNIQUE(review_id, version_no)
);
CREATE UNIQUE INDEX IF NOT EXISTS uq_versions_issued_seq
    ON review_versions(review_id, issued_seq) WHERE issued_seq IS NOT NULL;
CREATE TABLE IF NOT EXISTS rechecks (
    id TEXT PRIMARY KEY,
    version_id TEXT NOT NULL REFERENCES review_versions(id),
    rechecked_by TEXT NOT NULL,
    result TEXT NOT NULL,
    details_json TEXT NOT NULL,
    created_at TEXT NOT NULL
);
"""


class DuplicateKeyError(Exception):
    """唯一约束冲突（证据业务键重复等），由服务层翻译为去重或冲突。"""


def _dump(obj: object) -> str:
    return json.dumps(obj, ensure_ascii=False, sort_keys=True)


def _load(text: str) -> object:
    return json.loads(text)


class SQLiteStore:
    """线程安全的 SQLite 仓库：单连接，全部操作在写锁内执行。"""

    def __init__(self, path: str = ":memory:") -> None:
        self._conn = sqlite3.connect(path, check_same_thread=False, isolation_level=None)
        self._conn.row_factory = sqlite3.Row
        self._lock = threading.RLock()
        with self._lock:
            self._conn.execute("PRAGMA foreign_keys = ON")
            try:
                self._conn.execute("PRAGMA journal_mode = WAL")
            except sqlite3.DatabaseError:
                pass  # 内存库不支持 WAL，忽略
            self._conn.executescript(SCHEMA)

    def close(self) -> None:
        with self._lock:
            self._conn.close()

    # ---- 指标口径 ----

    def insert_metric_definition(self, record: MetricDefinition) -> None:
        with self._lock:
            self._conn.execute(
                "INSERT INTO metric_definitions "
                "(id, name, version, definition_json, definition_hash, created_at, created_by) "
                "VALUES (?,?,?,?,?,?,?)",
                (
                    record.id,
                    record.definition["name"],
                    record.definition["version"],
                    _dump(record.definition),
                    record.definition_hash,
                    record.created_at,
                    record.created_by,
                ),
            )

    def get_metric_definition(self, definition_id: str) -> MetricDefinition | None:
        with self._lock:
            row = self._conn.execute(
                "SELECT * FROM metric_definitions WHERE id=?", (definition_id,)
            ).fetchone()
        return self._row_to_metric_definition(row) if row else None

    def find_metric_definition(self, name: str, version: int) -> MetricDefinition | None:
        with self._lock:
            row = self._conn.execute(
                "SELECT * FROM metric_definitions WHERE name=? AND version=?", (name, version)
            ).fetchone()
        return self._row_to_metric_definition(row) if row else None

    def latest_metric_version(self, name: str) -> int:
        with self._lock:
            row = self._conn.execute(
                "SELECT COALESCE(MAX(version), 0) FROM metric_definitions WHERE name=?", (name,)
            ).fetchone()
        return int(row[0])

    def list_metric_definitions(self) -> list[MetricDefinition]:
        with self._lock:
            rows = self._conn.execute(
                "SELECT * FROM metric_definitions ORDER BY name, version"
            ).fetchall()
        return [self._row_to_metric_definition(row) for row in rows]

    @staticmethod
    def _row_to_metric_definition(row: sqlite3.Row) -> MetricDefinition:
        return MetricDefinition(
            id=row["id"],
            definition=_load(row["definition_json"]),
            definition_hash=row["definition_hash"],
            created_at=row["created_at"],
            created_by=row["created_by"],
        )

    # ---- 复盘 ----

    def insert_review(self, review: Review) -> None:
        with self._lock:
            self._conn.execute(
                "INSERT INTO reviews "
                "(id, region, name, window_json, baseline_json, metric_def_ids_json, created_at, created_by) "
                "VALUES (?,?,?,?,?,?,?,?)",
                (
                    review.id,
                    review.region,
                    review.name,
                    _dump(review.window.to_spec()),
                    _dump(review.baseline.to_spec()) if review.baseline else None,
                    _dump(review.metric_def_ids),
                    review.created_at,
                    review.created_by,
                ),
            )

    def get_review(self, review_id: str) -> Review | None:
        with self._lock:
            row = self._conn.execute("SELECT * FROM reviews WHERE id=?", (review_id,)).fetchone()
        return self._row_to_review(row) if row else None

    def update_review_metric_defs(self, review_id: str, metric_def_ids: list[str]) -> None:
        with self._lock:
            self._conn.execute(
                "UPDATE reviews SET metric_def_ids_json=? WHERE id=?",
                (_dump(metric_def_ids), review_id),
            )

    @staticmethod
    def _row_to_review(row: sqlite3.Row) -> Review:
        return Review(
            id=row["id"],
            region=row["region"],
            name=row["name"],
            window=HolidayWindow.from_spec(_load(row["window_json"])),
            baseline=HolidayWindow.from_spec(_load(row["baseline_json"]))
            if row["baseline_json"]
            else None,
            metric_def_ids=list(_load(row["metric_def_ids_json"])),
            created_at=row["created_at"],
            created_by=row["created_by"],
        )

    # ---- 证据 ----

    def insert_evidence(self, evidence: Evidence) -> None:
        with self._lock:
            try:
                self._conn.execute(
                    "INSERT INTO evidence "
                    "(id, review_id, kind, source, external_id, occurred_at, ingested_at, "
                    "payload_json, content_hash) VALUES (?,?,?,?,?,?,?,?,?)",
                    (
                        evidence.id,
                        evidence.review_id,
                        evidence.kind,
                        evidence.source,
                        evidence.external_id,
                        format_instant(evidence.occurred_at),
                        format_instant(evidence.ingested_at),
                        _dump(evidence.payload),
                        evidence.content_hash,
                    ),
                )
            except sqlite3.IntegrityError as exc:
                raise DuplicateKeyError(str(exc)) from exc

    def get_evidence_by_key(
        self, review_id: str, kind: str, source: str, external_id: str
    ) -> Evidence | None:
        with self._lock:
            row = self._conn.execute(
                "SELECT * FROM evidence WHERE review_id=? AND kind=? AND source=? AND external_id=?",
                (review_id, kind, source, external_id),
            ).fetchone()
        return self._row_to_evidence(row) if row else None

    def list_evidence(self, review_id: str, kind: str | None = None) -> list[Evidence]:
        with self._lock:
            if kind is None:
                rows = self._conn.execute(
                    "SELECT * FROM evidence WHERE review_id=? ORDER BY occurred_at, id",
                    (review_id,),
                ).fetchall()
            else:
                rows = self._conn.execute(
                    "SELECT * FROM evidence WHERE review_id=? AND kind=? ORDER BY occurred_at, id",
                    (review_id, kind),
                ).fetchall()
        return [self._row_to_evidence(row) for row in rows]

    def list_evidence_by_ids(self, ids: list[str]) -> list[Evidence]:
        if not ids:
            return []
        placeholders = ",".join("?" for _ in ids)
        with self._lock:
            rows = self._conn.execute(
                f"SELECT * FROM evidence WHERE id IN ({placeholders})", ids
            ).fetchall()
        return [self._row_to_evidence(row) for row in rows]

    def count_evidence(self, review_id: str) -> int:
        with self._lock:
            row = self._conn.execute(
                "SELECT COUNT(*) FROM evidence WHERE review_id=?", (review_id,)
            ).fetchone()
        return int(row[0])

    @staticmethod
    def _row_to_evidence(row: sqlite3.Row) -> Evidence:
        return Evidence(
            id=row["id"],
            review_id=row["review_id"],
            kind=row["kind"],
            source=row["source"],
            external_id=row["external_id"],
            occurred_at=parse_instant(row["occurred_at"]),
            ingested_at=parse_instant(row["ingested_at"]),
            payload=_load(row["payload_json"]),
            content_hash=row["content_hash"],
        )

    # ---- 计算运行与步骤 ----

    def insert_run(self, run: CalculationRun) -> None:
        with self._lock:
            self._conn.execute(
                "INSERT INTO calculation_runs "
                "(id, review_id, status, fingerprint, manifest_json, metric_defs_json, "
                "window_json, baseline_json, error, created_at, finished_at) "
                "VALUES (?,?,?,?,?,?,?,?,?,?,?)",
                (
                    run.id,
                    run.review_id,
                    run.status,
                    run.fingerprint,
                    _dump(run.manifest),
                    _dump(run.metric_defs),
                    _dump(run.window_spec),
                    _dump(run.baseline_spec) if run.baseline_spec else None,
                    run.error,
                    run.created_at,
                    run.finished_at,
                ),
            )

    def get_run(self, run_id: str) -> CalculationRun | None:
        with self._lock:
            row = self._conn.execute(
                "SELECT * FROM calculation_runs WHERE id=?", (run_id,)
            ).fetchone()
        return self._row_to_run(row) if row else None

    def update_run_status(
        self,
        run_id: str,
        status: str,
        *,
        error: str | None = None,
        finished_at: str | None = None,
    ) -> None:
        with self._lock:
            self._conn.execute(
                "UPDATE calculation_runs SET status=?, error=?, finished_at=? WHERE id=?",
                (status, error, finished_at, run_id),
            )

    def mark_running_runs_interrupted(self, reason: str) -> int:
        """服务重启后调用：遗留 running 运行一律标记为可续算的 interrupted。"""
        with self._lock:
            cursor = self._conn.execute(
                "UPDATE calculation_runs SET status=?, error=? WHERE status=?",
                ("interrupted", reason, RUN_RUNNING),
            )
            return cursor.rowcount

    @staticmethod
    def _row_to_run(row: sqlite3.Row) -> CalculationRun:
        return CalculationRun(
            id=row["id"],
            review_id=row["review_id"],
            status=row["status"],
            fingerprint=row["fingerprint"],
            manifest=list(_load(row["manifest_json"])),
            metric_defs=list(_load(row["metric_defs_json"])),
            window_spec=_load(row["window_json"]),
            baseline_spec=_load(row["baseline_json"]) if row["baseline_json"] else None,
            error=row["error"],
            created_at=row["created_at"],
            finished_at=row["finished_at"],
        )

    def upsert_step(self, step: CalculationStep) -> None:
        with self._lock:
            self._conn.execute(
                "INSERT INTO calculation_steps "
                "(run_id, metric_key, status, result_json, fingerprint, computed_at) "
                "VALUES (?,?,?,?,?,?) "
                "ON CONFLICT(run_id, metric_key) DO UPDATE SET "
                "status=excluded.status, result_json=excluded.result_json, "
                "fingerprint=excluded.fingerprint, computed_at=excluded.computed_at",
                (
                    step.run_id,
                    step.metric_key,
                    step.status,
                    _dump(step.result),
                    step.fingerprint,
                    step.computed_at,
                ),
            )

    def list_steps(self, run_id: str) -> list[CalculationStep]:
        with self._lock:
            rows = self._conn.execute(
                "SELECT * FROM calculation_steps WHERE run_id=? ORDER BY metric_key", (run_id,)
            ).fetchall()
        return [
            CalculationStep(
                run_id=row["run_id"],
                metric_key=row["metric_key"],
                status=row["status"],
                result=_load(row["result_json"]),
                fingerprint=row["fingerprint"],
                computed_at=row["computed_at"],
            )
            for row in rows
        ]

    # ---- 复盘版本 ----

    def insert_version(self, version: ReviewVersion) -> int:
        """在写锁内分配版本号并插入，保证版本号单调不重号。"""
        with self._lock:
            row = self._conn.execute(
                "SELECT COALESCE(MAX(version_no), 0) + 1 FROM review_versions WHERE review_id=?",
                (version.review_id,),
            ).fetchone()
            version_no = int(row[0])
            self._conn.execute(
                "INSERT INTO review_versions "
                "(id, review_id, version_no, run_id, status, fingerprint, manifest_json, "
                "metric_defs_json, results_json, window_json, baseline_json, created_at) "
                "VALUES (?,?,?,?,?,?,?,?,?,?,?,?)",
                (
                    version.id,
                    version.review_id,
                    version_no,
                    version.run_id,
                    version.status,
                    version.fingerprint,
                    _dump(version.manifest),
                    _dump(version.metric_defs),
                    _dump(version.results),
                    _dump(version.window_spec),
                    _dump(version.baseline_spec) if version.baseline_spec else None,
                    version.created_at,
                ),
            )
            return version_no

    def get_version(self, review_id: str, version_no: int) -> ReviewVersion | None:
        with self._lock:
            row = self._conn.execute(
                "SELECT * FROM review_versions WHERE review_id=? AND version_no=?",
                (review_id, version_no),
            ).fetchone()
        return self._row_to_version(row) if row else None

    def get_version_by_run(self, run_id: str) -> ReviewVersion | None:
        with self._lock:
            row = self._conn.execute(
                "SELECT * FROM review_versions WHERE run_id=?", (run_id,)
            ).fetchone()
        return self._row_to_version(row) if row else None

    def list_versions(self, review_id: str) -> list[ReviewVersion]:
        with self._lock:
            rows = self._conn.execute(
                "SELECT * FROM review_versions WHERE review_id=? ORDER BY version_no",
                (review_id,),
            ).fetchall()
        return [self._row_to_version(row) for row in rows]

    def latest_version(self, review_id: str) -> ReviewVersion | None:
        with self._lock:
            row = self._conn.execute(
                "SELECT * FROM review_versions WHERE review_id=? "
                "ORDER BY version_no DESC LIMIT 1",
                (review_id,),
            ).fetchone()
        return self._row_to_version(row) if row else None

    def issue_version(
        self, version_id: str, review_id: str, issued_by: str, issued_at: str
    ) -> bool:
        """条件更新：仅当版本仍是草稿时签发；并发下只有一个调用返回 True。"""
        with self._lock:
            cursor = self._conn.execute(
                "UPDATE review_versions "
                "SET status='issued', issued_at=?, issued_by=?, "
                "issued_seq=(SELECT COALESCE(MAX(issued_seq), 0) + 1 "
                "            FROM review_versions WHERE review_id=?) "
                "WHERE id=? AND status=?",
                (issued_at, issued_by, review_id, version_id, VERSION_DRAFT),
            )
            return cursor.rowcount == 1

    @staticmethod
    def _row_to_version(row: sqlite3.Row) -> ReviewVersion:
        return ReviewVersion(
            id=row["id"],
            review_id=row["review_id"],
            version_no=row["version_no"],
            run_id=row["run_id"],
            status=row["status"],
            fingerprint=row["fingerprint"],
            manifest=list(_load(row["manifest_json"])),
            metric_defs=list(_load(row["metric_defs_json"])),
            results=_load(row["results_json"]),
            window_spec=_load(row["window_json"]),
            baseline_spec=_load(row["baseline_json"]) if row["baseline_json"] else None,
            created_at=row["created_at"],
            issued_at=row["issued_at"],
            issued_by=row["issued_by"],
            issued_seq=row["issued_seq"],
        )

    # ---- 复核 ----

    def insert_recheck(self, recheck: Recheck) -> None:
        with self._lock:
            self._conn.execute(
                "INSERT INTO rechecks (id, version_id, rechecked_by, result, details_json, created_at) "
                "VALUES (?,?,?,?,?,?)",
                (
                    recheck.id,
                    recheck.version_id,
                    recheck.rechecked_by,
                    recheck.result,
                    _dump(recheck.details),
                    recheck.created_at,
                ),
            )

    def list_rechecks(self, version_id: str) -> list[Recheck]:
        with self._lock:
            rows = self._conn.execute(
                "SELECT * FROM rechecks WHERE version_id=? ORDER BY created_at, id", (version_id,)
            ).fetchall()
        return [
            Recheck(
                id=row["id"],
                version_id=row["version_id"],
                rechecked_by=row["rechecked_by"],
                result=row["result"],
                details=_load(row["details_json"]),
                created_at=row["created_at"],
            )
            for row in rows
        ]
