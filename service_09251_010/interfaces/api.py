"""HTTP 接口边界（仅依赖标准库）。

鉴权约定：请求头 X-Actor 为操作者名，X-Roles 为逗号分隔角色
（admin/analyst/signer/read_pii）。缺失即匿名。
所有响应为 JSON；领域错误映射为对应 HTTP 状态码与稳定错误码。
"""
from __future__ import annotations

import json
import logging
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any
from urllib.parse import parse_qs, urlsplit

from ..application.security import Actor
from ..domain.entities import Challenge, Review, ReviewVersion
from ..domain.errors import DomainError
from ..application import services as service_module

log = logging.getLogger("holiday_review.api")


def review_to_dict(review: Review) -> dict[str, Any]:
    return {
        "id": review.id,
        "name": review.name,
        "tz_name": review.tz_name,
        "window_start": review.window_start.isoformat(),
        "window_end": review.window_end.isoformat(),
        "created_by": review.created_by,
        "created_at": review.created_at.isoformat(),
    }


def version_to_dict(version: ReviewVersion) -> dict[str, Any]:
    return {
        "id": version.id,
        "review_id": version.review_id,
        "seq_no": version.seq_no,
        "status": version.status,
        "window": version.window,
        "metric_specs": version.metric_specs,
        "results": version.results,
        "inputs_fingerprint": version.inputs_fingerprint,
        "result_fingerprint": version.result_fingerprint,
        "input_evidence_count": len(version.input_evidence_ids),
        "computed_cursor": version.computed_cursor,
        "created_at": version.created_at.isoformat(),
        "signed_at": version.signed_at.isoformat() if version.signed_at else None,
        "signed_by": version.signed_by,
        "superseded_by": version.superseded_by,
        "immutable": version.is_immutable,
    }


def challenge_to_dict(challenge: Challenge) -> dict[str, Any]:
    return {
        "id": challenge.id,
        "review_id": challenge.review_id,
        "version_id": challenge.version_id,
        "metric_key": challenge.metric_key,
        "reason": challenge.reason,
        "raised_by": challenge.raised_by,
        "status": challenge.status,
        "created_at": challenge.created_at.isoformat(),
        "resolved_at": challenge.resolved_at.isoformat() if challenge.resolved_at else None,
        "resolution": challenge.resolution,
    }


class ApiHandler(BaseHTTPRequestHandler):
    service: "service_module.ReviewService" = None  # 由 server 实例注入

    server_version = "HolidayReview/1.0"

    # ---- 框架 -------------------------------------------------------------
    def log_message(self, fmt: str, *args: Any) -> None:
        log.debug("%s - %s", self.address_string(), fmt % args)

    def _actor(self) -> Actor:
        name = self.headers.get("X-Actor", "").strip()
        if not name:
            return Actor.anonymous()
        roles_raw = self.headers.get("X-Roles", "")
        roles = frozenset(r.strip() for r in roles_raw.split(",") if r.strip())
        return Actor(name=name, roles=roles)

    def _read_json(self) -> dict[str, Any]:
        length = int(self.headers.get("Content-Length") or 0)
        if length <= 0:
            return {}
        raw = self.rfile.read(length)
        try:
            body = json.loads(raw.decode("utf-8"))
        except (json.JSONDecodeError, UnicodeDecodeError) as exc:
            raise DomainError("请求体不是合法 JSON", code="invalid_json", status=400) from exc
        if not isinstance(body, dict):
            raise DomainError("请求体必须是 JSON 对象", code="invalid_json", status=400)
        return body

    def _send_json(self, payload: Any, status: int = 200) -> None:
        data = json.dumps(payload, ensure_ascii=False, default=str).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def _handle(self, handler_fn) -> None:
        try:
            handler_fn()
        except DomainError as exc:
            self._send_json(exc.to_dict(), exc.status)
        except Exception:  # noqa: BLE001 - 边界统一兜底
            log.exception("未处理错误")
            self._send_json(
                {"error": "internal_error", "message": "服务内部错误"}, 500
            )

    # ---- 路由 -------------------------------------------------------------
    def do_GET(self) -> None:  # noqa: N802
        self._handle(self._route_get)

    def do_POST(self) -> None:  # noqa: N802
        self._handle(self._route_post)

    def _route_get(self) -> None:
        parts, query = self._split()
        svc = self.service
        actor = self._actor()

        if parts == ["api", "metrics"]:
            from ..application import metrics as metrics_engine

            return self._send_json({"metrics": metrics_engine.metric_keys()})
        if parts == ["api", "reviews"]:
            return self._send_json([review_to_dict(r) for r in svc.list_reviews(actor)])
        if len(parts) == 3 and parts[:2] == ["api", "reviews"]:
            return self._send_json(review_to_dict(svc.get_review(actor, parts[2])))
        if (
            len(parts) == 4
            and parts[0] == "api"
            and parts[1] == "reviews"
            and parts[3] == "evidence"
        ):
            include = query.get("include_duplicates", ["false"])[0] == "true"
            return self._send_json(svc.list_evidence(actor, parts[2], include_duplicates=include))
        if (
            len(parts) == 4
            and parts[0] == "api"
            and parts[1] == "reviews"
            and parts[3] == "versions"
        ):
            return self._send_json(
                [version_to_dict(v) for v in svc.list_versions(actor, parts[2])]
            )
        if (
            len(parts) == 4
            and parts[0] == "api"
            and parts[1] == "reviews"
            and parts[3] == "challenges"
        ):
            return self._send_json(
                [challenge_to_dict(c) for c in svc.list_challenges(actor, parts[2])]
            )
        if (
            len(parts) == 4
            and parts[0] == "api"
            and parts[1] == "reviews"
            and parts[3] == "diff"
        ):
            from_seq = self._int_query(query, "from")
            to_seq = self._int_query(query, "to")
            return self._send_json(svc.diff_versions(actor, parts[2], from_seq, to_seq))
        if len(parts) == 3 and parts[:2] == ["api", "versions"]:
            return self._send_json(version_to_dict(svc.get_version(actor, parts[2])))
        if len(parts) == 4 and parts[:2] == ["api", "versions"] and parts[3] == "export":
            return self._send_json(svc.export_version(actor, parts[2]))
        if parts == ["api", "challenges"]:
            return self._send_json(
                [challenge_to_dict(c) for c in svc.list_challenges(actor)]
            )
        self._not_found()

    def _route_post(self) -> None:
        parts, _query = self._split()
        svc = self.service
        actor = self._actor()
        body = self._read_json()

        if parts == ["api", "reviews"]:
            review = svc.create_review(
                actor,
                name=body.get("name", ""),
                tz_name=body.get("tz_name", ""),
                window_start=body.get("window_start", ""),
                window_end=body.get("window_end", ""),
            )
            return self._send_json(review_to_dict(review), 201)
        if (
            len(parts) == 4
            and parts[0] == "api"
            and parts[1] == "reviews"
            and parts[3] == "evidence"
        ):
            evidence = svc.add_evidence(
                actor,
                parts[2],
                kind=body.get("kind", ""),
                source_ref=body.get("source_ref", ""),
                occurred_at=body.get("occurred_at", ""),
                payload=body.get("payload", {}),
            )
            result = {"id": evidence.id, "fingerprint": evidence.fingerprint,
                      "duplicate_of": evidence.duplicate_of,
                      "duplicate": evidence.is_duplicate()}
            return self._send_json(result, 200 if evidence.is_duplicate() else 201)
        if (
            len(parts) == 4
            and parts[0] == "api"
            and parts[1] == "reviews"
            and parts[3] == "versions"
        ):
            version = svc.create_version(actor, parts[2], body.get("metric_specs"))
            return self._send_json(version_to_dict(version), 201)
        if len(parts) == 4 and parts[:2] == ["api", "versions"] and parts[3] == "compute":
            query = self._split()[1]
            batch_raw = query.get("batch_size", [None])[0]
            try:
                batch_size = int(batch_raw) if batch_raw else None
            except ValueError as exc:
                raise DomainError("batch_size 必须是正整数", code="invalid_query", status=400) from exc
            return self._send_json(svc.compute_version(actor, parts[2], batch_size))
        if len(parts) == 4 and parts[:2] == ["api", "versions"] and parts[3] == "sign":
            return self._send_json(version_to_dict(svc.sign_version(actor, parts[2])))
        if (
            len(parts) == 4
            and parts[0] == "api"
            and parts[1] == "reviews"
            and parts[3] == "challenges"
        ):
            challenge = svc.raise_challenge(
                actor,
                parts[2],
                body.get("version_id", ""),
                body.get("reason", ""),
                metric_key=body.get("metric_key"),
            )
            return self._send_json(challenge_to_dict(challenge), 201)
        if len(parts) == 4 and parts[:2] == ["api", "challenges"] and parts[3] == "resolve":
            challenge = svc.resolve_challenge(
                actor,
                parts[2],
                body.get("resolution", ""),
                bool(body.get("approve", False)),
            )
            return self._send_json(challenge_to_dict(challenge))
        if parts == ["api", "recover"]:
            if "admin" not in actor.roles:
                raise DomainError("仅管理员可触发续算恢复", code="permission_denied", status=403)
            return self._send_json({"recovered": svc.recover_interrupted()})
        self._not_found()

    # ---- 工具 -------------------------------------------------------------
    def _split(self):
        parsed = urlsplit(self.path)
        parts = [p for p in parsed.path.split("/") if p]
        query = parse_qs(parsed.query)
        return parts, query

    def _not_found(self) -> None:
        self._send_json({"error": "not_found", "message": "接口不存在"}, 404)

    @staticmethod
    def _int_query(query: dict, name: str) -> int:
        try:
            return int(query[name][0])
        except (KeyError, ValueError, TypeError) as exc:
            raise DomainError(f"查询参数 {name} 必须是整数", code="invalid_query", status=400) from exc


def create_server(host: str, port: int, svc: "service_module.ReviewService") -> ThreadingHTTPServer:
    handler = type("BoundApiHandler", (ApiHandler,), {"service": svc})
    httpd = ThreadingHTTPServer((host, port), handler)
    return httpd
