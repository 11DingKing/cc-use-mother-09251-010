"""组合根：组装仓储、应用服务与 HTTP 服务。"""
from __future__ import annotations

import os
from pathlib import Path

from .application.services import ReviewService
from .interfaces.api import create_server
from .persistence.sqlite_repo import SqliteRepository


def default_db_path() -> Path:
    env = os.environ.get("HOLIDAY_REVIEW_DB")
    if env:
        return Path(env)
    base = os.environ.get("XDG_DATA_HOME")
    if base:
        return Path(base) / "holiday-review" / "review.db"
    return Path("/tmp") / "holiday-review" / "review.db"


def build_service(db_path: str | Path | None = None) -> tuple[ReviewService, SqliteRepository]:
    path = str(db_path) if db_path is not None else str(default_db_path())
    repo = SqliteRepository(path)
    return ReviewService(repo), repo


def serve(host: str = "127.0.0.1", port: int = 8080, db_path: str | Path | None = None):
    svc, repo = build_service(db_path)
    httpd = create_server(host, port, svc)
    return httpd, repo
