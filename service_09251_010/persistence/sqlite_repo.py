"""SQLite 仓储适配器。

时间以 UTC ISO 字符串落库，payload/规格/结果以 JSON 落库。
所有读改写流程必须在 begin_immediate() 事务内完成：立即获取写锁，
并发签发时后到者在拿到锁后会读到已变化的状态，由应用层判为冲突而非互相覆盖。
"""
from __future__ import annotations

import functools
import json
import sqlite3
import threading
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from ..application.ports import Repository
from ..domain.entities import Challenge, Evidence, Review, ReviewVersion


def _synchronized(cls):
    """类装饰器：所有公共方法在进程级可重入锁内执行。

    多线程 HTTP 服务共用一个连接；事务已持锁时，RLock 可重入，不受影响。
    """
    for name, fn in list(vars(cls).items()):
        if name.startswith("_") or not callable(fn):
            continue

        @functools.wraps(fn)
        def wrapper(self, *args, _fn=fn, **kwargs):
            with self._lock:
                return _fn(self, *args, **kwargs)

        setattr(cls, name, wrapper)
    return cls


_SCHEMA = """
CREATE TABLE IF NOT EXISTS reviews (
    id TEXT PRIMARY KEY,
    name TEXT NOT NULL,
    tz_name TEXT NOT NULL,
    window_start TEXT NOT NULL,
    window_end TEXT NOT NULL,
    created_by TEXT NOT NULL,
    created_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS evidence (
    seq INTEGER PRIMARY KEY AUTOINCREMENT,
    id TEXT UNIQUE NOT NULL,
    review_id TEXT NOT NULL,
    kind TEXT NOT NULL,
    source_ref TEXT NOT NULL,
    occurred_at TEXT NOT NULL,
    payload TEXT NOT NULL,
    fingerprint TEXT NOT NULL,
    received_at TEXT NOT NULL,
    duplicate_of TEXT
);
CREATE INDEX IF NOT EXISTS idx_evidence_review ON evidence(review_id, seq);
CREATE UNIQUE INDEX IF NOT EXISTS uq_evidence_review_fp
    ON evidence(review_id, fingerprint) WHERE duplicate_of IS NULL;
CREATE TABLE IF NOT EXISTS versions (
    id TEXT PRIMARY KEY,
    review_id TEXT NOT NULL,
    seq_no INTEGER NOT NULL,
    status TEXT NOT NULL,
    window TEXT NOT NULL,
    metric_specs TEXT NOT NULL,
    results TEXT,
    inputs_fingerprint TEXT,
    result_fingerprint TEXT,
    created_at TEXT NOT NULL,
    signed_at TEXT,
    signed_by TEXT,
    superseded_by TEXT,
    input_evidence TEXT NOT NULL DEFAULT '[]',
    computed_cursor INTEGER NOT NULL DEFAULT 0,
    UNIQUE(review_id, seq_no)
);
CREATE TABLE IF NOT EXISTS challenges (
    id TEXT PRIMARY KEY,
    review_id TEXT NOT NULL,
    version_id TEXT NOT NULL,
    metric_key TEXT,
    reason TEXT NOT NULL,
    raised_by TEXT NOT NULL,
    status TEXT NOT NULL,
    created_at TEXT NOT NULL,
    resolved_at TEXT,
    resolution TEXT
);
CREATE TABLE IF NOT EXISTS compute_states (
    version_id TEXT PRIMARY KEY,
    cursor INTEGER NOT NULL,
    states TEXT NOT NULL,
    updated_at TEXT NOT NULL
);
"""


def _parse_dt(value: str) -> datetime:
    return datetime.fromisoformat(value)


def _json_default(obj: Any) -> Any:
    if isinstance(obj, datetime):
        return obj.isoformat()
    raise TypeError(f"不可序列化的类型: {type(obj)!r}")


def dumps_json(obj: Any) -> str:
    return json.dumps(obj, ensure_ascii=False, default=_json_default)


def loads_json(value: str | None) -> Any:
    return json.loads(value) if value is not None else None


class _Transaction:
    def __init__(self, conn: sqlite3.Connection, lock: threading.RLock):
        self._conn = conn
        self._lock = lock
        self._closed = False

    def __enter__(self) -> "_Transaction":
        return self

    def __exit__(self, exc_type, exc, tb) -> None:
        if self._closed:
            return
        if exc_type is None:
            self.commit()
        else:
            self.rollback()

    def commit(self) -> None:
        if self._closed:
            return
        self._conn.execute("COMMIT")
        self._closed = True
        self._lock.release()

    def rollback(self) -> None:
        if self._closed:
            return
        self._conn.execute("ROLLBACK")
        self._closed = True
        self._lock.release()


@_synchronized
class SqliteRepository(Repository):
    def __init__(self, path: str = ":memory:"):
        self._lock = threading.RLock()
        if path != ":memory:":
            Path(path).parent.mkdir(parents=True, exist_ok=True)
        self._conn = sqlite3.connect(
            path,
            check_same_thread=False,
            isolation_level=None,
            timeout=30.0,
        )
        self._conn.row_factory = sqlite3.Row
        self._conn.execute("PRAGMA journal_mode=WAL")
        self._conn.execute("PRAGMA foreign_keys=ON")
        self._conn.execute("PRAGMA busy_timeout=30000")
        with self._conn:
            self._conn.executescript(_SCHEMA)
            self._migrate()

    def close(self) -> None:
        with self._lock:
            self._conn.close()

    def _migrate(self) -> None:
        """对早于 input_evidence 列的库做轻量补列。"""
        cols = {r["name"] for r in self._conn.execute("PRAGMA table_info(versions)")}
        if "input_evidence" not in cols:
            self._conn.execute(
                "ALTER TABLE versions ADD COLUMN input_evidence TEXT NOT NULL DEFAULT '[]'"
            )

    # -- 事务 ---------------------------------------------------------------
    def begin_immediate(self) -> _Transaction:
        self._lock.acquire()
        try:
            self._conn.execute("BEGIN IMMEDIATE")
        except Exception:
            self._lock.release()
            raise
        return _Transaction(self._conn, self._lock)

    # -- review -------------------------------------------------------------
    def insert_review(self, review: Review) -> None:
        self._conn.execute(
            "INSERT INTO reviews (id, name, tz_name, window_start, window_end,"
            " created_by, created_at) VALUES (?,?,?,?,?,?,?)",
            (
                review.id,
                review.name,
                review.tz_name,
                review.window_start.isoformat(),
                review.window_end.isoformat(),
                review.created_by,
                review.created_at.isoformat(),
            ),
        )

    def get_review(self, review_id: str) -> Review | None:
        row = self._conn.execute("SELECT * FROM reviews WHERE id=?", (review_id,)).fetchone()
        return _row_to_review(row) if row else None

    def list_reviews(self) -> list[Review]:
        rows = self._conn.execute("SELECT * FROM reviews ORDER BY created_at").fetchall()
        return [_row_to_review(r) for r in rows]

    # -- evidence -----------------------------------------------------------
    def insert_evidence(self, evidence: Evidence) -> None:
        self._conn.execute(
            "INSERT INTO evidence (id, review_id, kind, source_ref, occurred_at,"
            " payload, fingerprint, received_at, duplicate_of)"
            " VALUES (?,?,?,?,?,?,?,?,?)",
            (
                evidence.id,
                evidence.review_id,
                evidence.kind,
                evidence.source_ref,
                evidence.occurred_at.isoformat(),
                dumps_json(evidence.payload),
                evidence.fingerprint,
                evidence.received_at.isoformat(),
                evidence.duplicate_of,
            ),
        )

    def get_evidence(self, evidence_id: str) -> Evidence | None:
        row = self._conn.execute("SELECT * FROM evidence WHERE id=?", (evidence_id,)).fetchone()
        return _row_to_evidence(row) if row else None

    def list_evidence(self, review_id: str) -> list[Evidence]:
        rows = self._conn.execute(
            "SELECT * FROM evidence WHERE review_id=? ORDER BY seq", (review_id,)
        ).fetchall()
        return [_row_to_evidence(r) for r in rows]

    def list_evidence_after(self, review_id: str, cursor: int) -> list[Evidence]:
        rows = self._conn.execute(
            "SELECT * FROM evidence WHERE review_id=? AND seq>? ORDER BY seq",
            (review_id, cursor),
        ).fetchall()
        return [_row_to_evidence(r) for r in rows]

    def find_evidence_by_fingerprint(self, review_id: str, fingerprint: str) -> Evidence | None:
        row = self._conn.execute(
            "SELECT * FROM evidence WHERE review_id=? AND fingerprint=? AND duplicate_of IS NULL",
            (review_id, fingerprint),
        ).fetchone()
        return _row_to_evidence(row) if row else None

    # -- version ------------------------------------------------------------
    def insert_version(self, version: ReviewVersion) -> None:
        self._conn.execute(
            "INSERT INTO versions (id, review_id, seq_no, status, window, metric_specs,"
            " results, inputs_fingerprint, result_fingerprint, created_at, signed_at,"
            " signed_by, superseded_by, input_evidence, computed_cursor)"
            " VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (
                version.id,
                version.review_id,
                version.seq_no,
                version.status,
                dumps_json(version.window),
                dumps_json(version.metric_specs),
                dumps_json(version.results) if version.results else None,
                version.inputs_fingerprint,
                version.result_fingerprint,
                version.created_at.isoformat(),
                version.signed_at.isoformat() if version.signed_at else None,
                version.signed_by,
                version.superseded_by,
                dumps_json(version.input_evidence_ids),
                version.computed_cursor,
            ),
        )

    def update_version(self, version: ReviewVersion) -> None:
        self._conn.execute(
            "UPDATE versions SET status=?, results=?, inputs_fingerprint=?,"
            " result_fingerprint=?, signed_at=?, signed_by=?, superseded_by=?,"
            " input_evidence=?, computed_cursor=? WHERE id=?",
            (
                version.status,
                dumps_json(version.results) if version.results else None,
                version.inputs_fingerprint,
                version.result_fingerprint,
                version.signed_at.isoformat() if version.signed_at else None,
                version.signed_by,
                version.superseded_by,
                dumps_json(version.input_evidence_ids),
                version.computed_cursor,
                version.id,
            ),
        )

    def get_version(self, version_id: str) -> ReviewVersion | None:
        row = self._conn.execute("SELECT * FROM versions WHERE id=?", (version_id,)).fetchone()
        return _row_to_version(row) if row else None

    def get_version_by_seq(self, review_id: str, seq_no: int) -> ReviewVersion | None:
        row = self._conn.execute(
            "SELECT * FROM versions WHERE review_id=? AND seq_no=?", (review_id, seq_no)
        ).fetchone()
        return _row_to_version(row) if row else None

    def list_versions(self, review_id: str) -> list[ReviewVersion]:
        rows = self._conn.execute(
            "SELECT * FROM versions WHERE review_id=? ORDER BY seq_no", (review_id,)
        ).fetchall()
        return [_row_to_version(r) for r in rows]

    def latest_version(self, review_id: str) -> ReviewVersion | None:
        row = self._conn.execute(
            "SELECT * FROM versions WHERE review_id=? ORDER BY seq_no DESC LIMIT 1",
            (review_id,),
        ).fetchone()
        return _row_to_version(row) if row else None

    def list_computing_versions(self) -> list[ReviewVersion]:
        rows = self._conn.execute("SELECT * FROM versions WHERE status=?", ("computing",)).fetchall()
        return [_row_to_version(r) for r in rows]

    # -- challenge ----------------------------------------------------------
    def insert_challenge(self, challenge: Challenge) -> None:
        self._conn.execute(
            "INSERT INTO challenges (id, review_id, version_id, metric_key, reason,"
            " raised_by, status, created_at, resolved_at, resolution)"
            " VALUES (?,?,?,?,?,?,?,?,?,?)",
            (
                challenge.id,
                challenge.review_id,
                challenge.version_id,
                challenge.metric_key,
                challenge.reason,
                challenge.raised_by,
                challenge.status,
                challenge.created_at.isoformat(),
                challenge.resolved_at.isoformat() if challenge.resolved_at else None,
                challenge.resolution,
            ),
        )

    def update_challenge(self, challenge: Challenge) -> None:
        self._conn.execute(
            "UPDATE challenges SET status=?, resolved_at=?, resolution=? WHERE id=?",
            (
                challenge.status,
                challenge.resolved_at.isoformat() if challenge.resolved_at else None,
                challenge.resolution,
                challenge.id,
            ),
        )

    def get_challenge(self, challenge_id: str) -> Challenge | None:
        row = self._conn.execute("SELECT * FROM challenges WHERE id=?", (challenge_id,)).fetchone()
        return _row_to_challenge(row) if row else None

    def list_challenges(self, review_id: str | None = None) -> list[Challenge]:
        if review_id is None:
            rows = self._conn.execute("SELECT * FROM challenges ORDER BY created_at").fetchall()
        else:
            rows = self._conn.execute(
                "SELECT * FROM challenges WHERE review_id=? ORDER BY created_at", (review_id,)
            ).fetchall()
        return [_row_to_challenge(r) for r in rows]

    # -- 续算状态 ------------------------------------------------------------
    def save_compute_state(self, version_id: str, cursor: int, states: dict) -> None:
        self._conn.execute(
            "INSERT INTO compute_states (version_id, cursor, states, updated_at)"
            " VALUES (?,?,?,?) ON CONFLICT(version_id) DO UPDATE SET"
            " cursor=excluded.cursor, states=excluded.states,"
            " updated_at=excluded.updated_at",
            (version_id, cursor, dumps_json(states), datetime.now(timezone.utc).isoformat()),
        )

    def get_compute_state(self, version_id: str) -> tuple[int, dict] | None:
        row = self._conn.execute(
            "SELECT * FROM compute_states WHERE version_id=?", (version_id,)
        ).fetchone()
        return (row["cursor"], loads_json(row["states"])) if row else None

    def delete_compute_state(self, version_id: str) -> None:
        self._conn.execute("DELETE FROM compute_states WHERE version_id=?", (version_id,))


# ---- 行映射 ---------------------------------------------------------------

def _row_to_review(row: sqlite3.Row) -> Review:
    return Review(
        id=row["id"],
        name=row["name"],
        tz_name=row["tz_name"],
        window_start=_parse_dt(row["window_start"]),
        window_end=_parse_dt(row["window_end"]),
        created_by=row["created_by"],
        created_at=_parse_dt(row["created_at"]),
    )


def _row_to_evidence(row: sqlite3.Row) -> Evidence:
    return Evidence(
        seq=row["seq"],
        id=row["id"],
        review_id=row["review_id"],
        kind=row["kind"],
        source_ref=row["source_ref"],
        occurred_at=_parse_dt(row["occurred_at"]),
        payload=loads_json(row["payload"]),
        fingerprint=row["fingerprint"],
        received_at=_parse_dt(row["received_at"]),
        duplicate_of=row["duplicate_of"],
    )


def _row_to_version(row: sqlite3.Row) -> ReviewVersion:
    return ReviewVersion(
        id=row["id"],
        review_id=row["review_id"],
        seq_no=row["seq_no"],
        status=row["status"],
        window=loads_json(row["window"]),
        metric_specs=loads_json(row["metric_specs"]),
        results=loads_json(row["results"]) or [],
        inputs_fingerprint=row["inputs_fingerprint"],
        result_fingerprint=row["result_fingerprint"],
        created_at=_parse_dt(row["created_at"]),
        signed_at=_parse_dt(row["signed_at"]) if row["signed_at"] else None,
        signed_by=row["signed_by"],
        superseded_by=row["superseded_by"],
        input_evidence_ids=loads_json(row["input_evidence"]) or [],
        computed_cursor=row["computed_cursor"],
    )


def _row_to_challenge(row: sqlite3.Row) -> Challenge:
    return Challenge(
        id=row["id"],
        review_id=row["review_id"],
        version_id=row["version_id"],
        metric_key=row["metric_key"],
        reason=row["reason"],
        raised_by=row["raised_by"],
        status=row["status"],
        created_at=_parse_dt(row["created_at"]),
        resolved_at=_parse_dt(row["resolved_at"]) if row["resolved_at"] else None,
        resolution=row["resolution"],
    )
