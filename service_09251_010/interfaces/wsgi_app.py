"""WSGI 接口边界：仅依赖标准库，可在干净环境直接运行。

路由、JSON 编解码、认证与错误映射集中在此；业务规则全部在服务层。
"""
from __future__ import annotations

import json
import re
import traceback
from http import HTTPStatus
from urllib.parse import parse_qs

from ..domain.errors import BadRequestError, DomainError, NotFoundError, ValidationError
from ..services.auth import Authenticator
from ..services.review_service import ReviewService

HEALTH_PATH = "/api/v1/health"


class Request:
    def __init__(self, environ: dict) -> None:
        self._environ = environ
        self.method = environ.get("REQUEST_METHOD", "GET").upper()
        self.path = environ.get("PATH_INFO") or "/"
        self.query = {
            key: values[0]
            for key, values in parse_qs(environ.get("QUERY_STRING", "")).items()
        }
        self._headers = {
            key[5:].replace("_", "-").lower(): value
            for key, value in environ.items()
            if key.startswith("HTTP_")
        }
        self._body: object = ...
        self._body_loaded = False

    def header(self, name: str) -> str | None:
        return self._headers.get(name.lower())

    def json(self) -> object:
        if not self._body_loaded:
            self._body_loaded = True
            try:
                length = int(self._environ.get("CONTENT_LENGTH") or 0)
            except ValueError:
                length = 0
            raw = self._environ["wsgi.input"].read(length) if length > 0 else b""
            if not raw:
                self._body = {}
            else:
                try:
                    self._body = json.loads(raw.decode("utf-8"))
                except (UnicodeDecodeError, json.JSONDecodeError) as exc:
                    raise BadRequestError(f"请求体不是合法 JSON: {exc}") from exc
        return self._body


class Router:
    def __init__(self) -> None:
        self._routes: list[tuple[str, re.Pattern, object]] = []

    def add(self, method: str, pattern: str, handler) -> None:
        regex = re.compile("^" + re.sub(r"\{(\w+)\}", r"(?P<\1>[^/]+)", pattern) + "$")
        self._routes.append((method, regex, handler))

    def match(self, method: str, path: str):
        for route_method, regex, handler in self._routes:
            if route_method != method:
                continue
            matched = regex.match(path)
            if matched:
                return handler, matched.groupdict()
        return None, {}


def _to_int(value: object, field: str) -> int:
    try:
        return int(str(value))
    except (TypeError, ValueError) as exc:
        raise ValidationError(f"{field} 必须是整数，收到: {value!r}") from exc


def create_app(service: ReviewService, authenticator: Authenticator):
    """装配 WSGI 应用：create_app(service, authenticator) -> callable(environ, start_response)。"""
    router = Router()

    def health(request, principal):
        return 200, {"status": "ok", "service": "service_09251_010"}

    def create_metric_definition(request, principal):
        return 201, service.create_metric_definition(principal, request.json())

    def list_metric_definitions(request, principal):
        return 200, {"items": service.list_metric_definitions(principal)}

    def create_review(request, principal):
        body = request.json()
        if not isinstance(body, dict):
            raise ValidationError("请求体必须是对象")
        return 201, service.create_review(
            principal,
            region=body.get("region"),
            name=body.get("name"),
            window=body.get("window"),
            baseline=body.get("baseline"),
            metric_def_ids=body.get("metric_def_ids") or [],
        )

    def get_review(request, principal, review_id):
        return 200, service.get_review_detail(principal, review_id)

    def attach_metrics(request, principal, review_id):
        body = request.json()
        if not isinstance(body, dict):
            raise ValidationError("请求体必须是对象")
        return 200, service.attach_metrics(principal, review_id, body.get("metric_def_ids") or [])

    def add_evidence(request, principal, review_id):
        body = request.json()
        items = body.get("items") if isinstance(body, dict) else None
        return 200, service.add_evidence(principal, review_id, items)

    def list_evidence(request, principal, review_id):
        return 200, {"items": service.list_evidence(principal, review_id, request.query.get("kind"))}

    def compute(request, principal, review_id):
        return 200, service.compute(principal, review_id)

    def list_versions(request, principal, review_id):
        return 200, {"items": service.list_versions(principal, review_id)}

    def get_version(request, principal, review_id, version_no):
        return 200, service.get_version(principal, review_id, _to_int(version_no, "version_no"))

    def issue_version(request, principal, review_id, version_no):
        return 200, service.issue_version(principal, review_id, _to_int(version_no, "version_no"))

    def recheck_version(request, principal, review_id, version_no):
        return 200, service.recheck_version(principal, review_id, _to_int(version_no, "version_no"))

    def list_rechecks(request, principal, review_id, version_no):
        return 200, {"items": service.list_rechecks(principal, review_id, _to_int(version_no, "version_no"))}

    def export_version(request, principal, review_id, version_no):
        payload = service.export_version(principal, review_id, _to_int(version_no, "version_no"))
        headers = [(
            "Content-Disposition",
            f'attachment; filename="review-{review_id}-v{version_no}.json"',
        )]
        return 200, payload, headers

    def diff_versions(request, principal, review_id):
        return 200, service.diff_versions(
            principal,
            review_id,
            _to_int(request.query.get("from"), "from"),
            _to_int(request.query.get("to"), "to"),
        )

    def get_run(request, principal, run_id):
        return 200, service.get_run(principal, run_id)

    def resume_run(request, principal, run_id):
        return 200, service.resume(principal, run_id)

    router.add("GET", HEALTH_PATH, health)
    router.add("POST", "/api/v1/metric-definitions", create_metric_definition)
    router.add("GET", "/api/v1/metric-definitions", list_metric_definitions)
    router.add("POST", "/api/v1/reviews", create_review)
    router.add("GET", "/api/v1/reviews/{review_id}", get_review)
    router.add("POST", "/api/v1/reviews/{review_id}/metrics", attach_metrics)
    router.add("POST", "/api/v1/reviews/{review_id}/evidence:batch", add_evidence)
    router.add("GET", "/api/v1/reviews/{review_id}/evidence", list_evidence)
    router.add("POST", "/api/v1/reviews/{review_id}/compute", compute)
    router.add("GET", "/api/v1/reviews/{review_id}/versions", list_versions)
    router.add("GET", "/api/v1/reviews/{review_id}/versions/{version_no}", get_version)
    router.add("POST", "/api/v1/reviews/{review_id}/versions/{version_no}/issue", issue_version)
    router.add("POST", "/api/v1/reviews/{review_id}/versions/{version_no}/recheck", recheck_version)
    router.add("GET", "/api/v1/reviews/{review_id}/versions/{version_no}/rechecks", list_rechecks)
    router.add("GET", "/api/v1/reviews/{review_id}/versions/{version_no}/export", export_version)
    router.add("GET", "/api/v1/reviews/{review_id}/diff", diff_versions)
    router.add("GET", "/api/v1/calculation-runs/{run_id}", get_run)
    router.add("POST", "/api/v1/calculation-runs/{run_id}/resume", resume_run)

    def app(environ, start_response):
        request = Request(environ)
        extra_headers: list[tuple[str, str]] = []
        try:
            handler, params = router.match(request.method, request.path)
            if handler is None:
                raise NotFoundError(f"路由不存在: {request.method} {request.path}")
            principal = (
                None
                if request.path == HEALTH_PATH
                else authenticator.authenticate(request.header("x-api-key"))
            )
            outcome = handler(request, principal, **params)
            status, payload = outcome[0], outcome[1]
            if len(outcome) > 2:
                extra_headers = list(outcome[2])
        except DomainError as exc:
            status = exc.http_status
            payload = {"error": {"code": exc.code, "message": exc.message, "details": exc.details}}
        except Exception:  # noqa: BLE001 - 边界兜底，避免泄露堆栈给客户端
            traceback.print_exc()
            status = 500
            payload = {"error": {"code": "internal_error", "message": "服务内部错误", "details": {}}}
        body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        headers = [("Content-Type", "application/json; charset=utf-8"), *extra_headers,
                   ("Content-Length", str(len(body)))]
        start_response(f"{status} {HTTPStatus(status).phrase}", headers)
        return [body]

    return app
