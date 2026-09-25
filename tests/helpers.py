"""测试公共工具：服务装配、样例口径与证据、WSGI 调用器。"""
from __future__ import annotations

import io
import json
from datetime import timedelta
from urllib.parse import urlencode
from wsgiref.util import setup_testing_defaults

from service_09251_010.domain.windows import parse_instant
from service_09251_010.persistence.sqlite_store import SQLiteStore
from service_09251_010.ports import SequentialIds
from service_09251_010.services.auth import ALL_SCOPES, Principal
from service_09251_010.services.review_service import ReviewService

ADMIN = Principal(name="admin", role="admin", scopes=ALL_SCOPES)

SH_WINDOW = {"tz": "Asia/Shanghai", "start": "2026-10-01", "end": "2026-10-08"}
SH_DAY = {"tz": "Asia/Shanghai", "start": "2026-10-01", "end": "2026-10-02"}
SH_BASELINE_DAY = {"tz": "Asia/Shanghai", "start": "2026-09-24", "end": "2026-09-25"}
LA_DAY = {"tz": "America/Los_Angeles", "start": "2026-10-01", "end": "2026-10-02"}


class FixedClock:
    """每次调用前进固定秒数的测试时钟。"""

    def __init__(self, start: str = "2026-10-10T00:00:00Z", step_seconds: int = 1) -> None:
        self._current = parse_instant(start)
        self._step = timedelta(seconds=step_seconds)

    def now(self):
        moment = self._current
        self._current = self._current + self._step
        return moment


def make_service() -> tuple[ReviewService, SQLiteStore]:
    store = SQLiteStore(":memory:")
    service = ReviewService(store, clock=FixedClock(), ids=SequentialIds())
    return service, store


# ---- 指标口径样例 ----

def queue_def(name: str = "queue_wait_p95", bucket_seconds: int = 3600) -> dict:
    return {
        "name": name,
        "engine": "percentile",
        "source_kind": "field_event",
        "event_type": "queue_wait_observed",
        "value_field": "wait_minutes",
        "percentile": 95,
        "bucket_seconds": bucket_seconds,
        "direction": "lower_better",
    }


def utilization_def(name: str = "mobile_charger_utilization") -> dict:
    return {
        "name": name,
        "engine": "utilization",
        "source_kind": "capacity_snapshot",
        "capacity_field": "capacity_kw",
        "used_field": "used_kw",
        "bucket_seconds": 3600,
        "direction": "higher_better",
    }


def info_latency_def(name: str = "info_publish_latency_avg") -> dict:
    return {
        "name": name,
        "engine": "avg",
        "source_kind": "field_event",
        "event_type": "info_published",
        "value_field": "latency_minutes",
        "bucket_seconds": 3600,
        "direction": "lower_better",
    }


def rescue_count_def(name: str = "rescue_events_count") -> dict:
    return {
        "name": name,
        "engine": "count",
        "source_kind": "rescue_record",
        "bucket_seconds": 3600,
        "direction": "lower_better",
    }


# ---- 证据样例 ----

def field_event(ext: str, occurred_at: str, event_type: str, **payload) -> dict:
    return {
        "kind": "field_event",
        "source": "field-ops",
        "external_id": ext,
        "occurred_at": occurred_at,
        "payload": {"type": event_type, **payload},
    }


def wait_event(ext: str, occurred_at: str, minutes: float) -> dict:
    return field_event(ext, occurred_at, "queue_wait_observed", wait_minutes=minutes)


def rescue_record(ext: str, occurred_at: str, **payload) -> dict:
    base = {"person_name": "张三", "person_phone": "13800000000", "vehicle_plate": "沪A12345"}
    base.update(payload)
    return {
        "kind": "rescue_record",
        "source": "rescue-center",
        "external_id": ext,
        "occurred_at": occurred_at,
        "payload": base,
    }


def capacity_snapshot(ext: str, occurred_at: str, capacity_kw: float, used_kw: float) -> dict:
    return {
        "kind": "capacity_snapshot",
        "source": "telemetry",
        "external_id": ext,
        "occurred_at": occurred_at,
        "payload": {"capacity_kw": capacity_kw, "used_kw": used_kw},
    }


def public_query(ext: str, occurred_at: str, **payload) -> dict:
    base = {"user_id": "u-1001", "channel": "app", "query_text": "排队还要多久"}
    base.update(payload)
    return {
        "kind": "public_query",
        "source": "app-gateway",
        "external_id": ext,
        "occurred_at": occurred_at,
        "payload": base,
    }


def mk_evidence(kind: str, occurred_at: str, payload: dict, ext: str = "x"):
    """直接构造领域 Evidence，用于引擎单元测试。"""
    from service_09251_010.domain.models import Evidence

    instant = parse_instant(occurred_at)
    return Evidence(
        id=f"ev_{ext}",
        review_id="rev_test",
        kind=kind,
        source="test",
        external_id=ext,
        occurred_at=instant,
        ingested_at=instant,
        payload=payload,
        content_hash="sha256:test",
    )


# ---- WSGI 调用 ----

def call_api(app, method: str, path: str, payload=None, key: str | None = None, query=None):
    body = json.dumps(payload).encode("utf-8") if payload is not None else b""
    environ: dict = {}
    setup_testing_defaults(environ)
    environ["REQUEST_METHOD"] = method
    environ["PATH_INFO"] = path
    environ["QUERY_STRING"] = urlencode(query or {})
    environ["wsgi.input"] = io.BytesIO(body)
    environ["CONTENT_LENGTH"] = str(len(body))
    if payload is not None:
        environ["CONTENT_TYPE"] = "application/json"
    if key is not None:
        environ["HTTP_X_API_KEY"] = key
    captured: dict = {}

    def start_response(status, headers, exc_info=None):
        captured["status"] = status
        captured["headers"] = dict(headers)

    chunks = app(environ, start_response)
    raw = b"".join(chunks)
    return int(captured["status"].split(" ", 1)[0]), json.loads(raw.decode("utf-8")), captured["headers"]


def make_api(keys=None):
    from service_09251_010.interfaces.wsgi_app import create_app
    from service_09251_010.services.auth import Authenticator

    service, _store = make_service()
    api_keys = keys or {
        "admin-key": {"name": "admin", "role": "admin"},
        "analyst-key": {"name": "analyst", "role": "analyst"},
        "issuer-key": {"name": "issuer", "role": "issuer"},
        "viewer-key": {"name": "viewer", "role": "viewer"},
        "auditor-key": {"name": "auditor", "role": "auditor"},
    }
    return create_app(service, Authenticator(api_keys)), service
