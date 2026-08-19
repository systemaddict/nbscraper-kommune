"""Read-only FastAPI status surface for the scraper.

The worker remains the only process that mutates queue state. This module opens
one short-lived database connection per request and exposes a single compact
snapshot for the dashboard to poll. ``/healthz`` and the HTML shell are public;
the snapshot is public too while the dashboard is an internal operational aid.
"""
from __future__ import annotations

from collections.abc import Iterator
from datetime import UTC, datetime
from importlib.resources import files
from typing import Annotated, Any, Literal

from fastapi import Depends, FastAPI, Query, Request
from fastapi.responses import HTMLResponse

from nbkommune import db
from nbkommune import repositories as repo
from nbkommune.db import Connection
from nbkommune.settings import Settings, get_settings


def get_conn(request: Request) -> Iterator[Connection]:
    conn = db.connect(request.app.state.settings)
    try:
        yield conn
    finally:
        conn.close()


def _queue_shape(rows: list[dict]) -> dict[str, Any]:
    by_status: dict[str, int] = {}
    by_kind: dict[str, dict[str, int]] = {}
    for row in rows:
        kind = str(row["kind"])
        status = str(row["status"])
        count = int(row["n"])
        by_status[status] = by_status.get(status, 0) + count
        by_kind.setdefault(kind, {})[status] = count
    return {"by_status": by_status, "by_kind": by_kind}


def _snapshot(conn: Connection) -> dict[str, Any]:
    municipalities = repo.article_stats(conn)
    queue = _queue_shape(repo.queue_summary(conn))
    errors = repo.error_summary(conn, hours=24.0)
    error_count = sum(int(row["n"]) for row in errors)
    dead_count = queue["by_status"].get("dead", 0)
    running = repo.list_tasks(conn, status="running", limit=10)

    if dead_count or error_count:
        state = "degraded"
    elif running:
        state = "working"
    else:
        state = "healthy"

    count_fields = ("articles", "ingested", "listed", "gone", "thin", "undated")
    totals = {field: sum(int(row[field]) for row in municipalities) for field in count_fields}
    totals["municipalities"] = len(municipalities)

    return {
        "generated_at": datetime.now(UTC),
        "state": state,
        "totals": totals,
        "queue": queue,
        "running_tasks": running,
        "next_tasks": repo.list_tasks(conn, status="queued", limit=12),
        "municipalities": municipalities,
        "recent_crawls": repo.recent_crawl_runs(conn, limit=12),
        "errors_24h": errors,
        "recent_errors": repo.recent_errors(conn, limit=12),
    }


def create_app(settings: Settings | None = None) -> FastAPI:
    app = FastAPI(
        title="nbscraper-kommune status",
        version="0.1.0",
        docs_url=None,
        redoc_url=None,
        openapi_url=None,
    )
    app.state.settings = settings or get_settings()

    @app.get("/healthz")
    def healthz() -> dict[str, str]:
        return {"status": "ok"}

    @app.get("/api/status")
    def status(conn: Annotated[Connection, Depends(get_conn)]) -> dict[str, Any]:
        return _snapshot(conn)

    @app.get("/api/articles")
    def articles(
        conn: Annotated[Connection, Depends(get_conn)],
        municipality: str | None = None,
        kind: Literal["nyhed", "pressemeddelelse"] | None = None,
        status: Literal["listed", "ingested", "gone"] | None = None,
        limit: int = Query(25, ge=1, le=100),
        offset: int = Query(0, ge=0),
    ) -> dict[str, Any]:
        """Filterable article metadata; content stays at the original URL."""
        filters = {
            "municipality_key": municipality,
            "kind": kind,
            "status": status,
        }
        return {
            "items": repo.list_articles(conn, **filters, limit=limit, offset=offset),
            "total": repo.count_articles(conn, **filters),
            "limit": limit,
            "offset": offset,
        }

    @app.get("/", response_class=HTMLResponse)
    def dashboard() -> str:
        return files("nbkommune").joinpath("static/dashboard.html").read_text("utf-8")

    return app
