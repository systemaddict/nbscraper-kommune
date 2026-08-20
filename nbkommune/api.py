"""FastAPI status surface plus authenticated inbox-review actions.

The worker remains the only process that mutates queue state. This module opens
one short-lived database connection per request and exposes a single compact
snapshot for the dashboard to poll. In production this server only binds to
loopback; the Better Auth gateway owns the public port and protects every route
except ``/healthz``.
"""
from __future__ import annotations

from collections.abc import Iterator
from datetime import UTC, datetime
from importlib.resources import files
from typing import Annotated, Any, Literal
from urllib.parse import urlencode

from fastapi import Depends, FastAPI, HTTPException, Query, Request
from fastapi.responses import HTMLResponse, RedirectResponse
from pydantic import BaseModel

from nbkommune import db
from nbkommune import repositories as repo
from nbkommune.db import Connection
from nbkommune.email_ingest import assign_email, ignore_email
from nbkommune.gmail_oauth import (
    GmailOAuthError,
    begin_oauth,
    complete_oauth,
    connection_status,
    disconnect_gmail,
)
from nbkommune.settings import Settings, get_settings
from nbkommune.targets import registry


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


class EmailAction(BaseModel):
    municipality_key: str | None = None
    remember_sender: bool = False


def _admin_actor(request: Request) -> str:
    # The public gateway authenticates the session and replaces this header.
    # The second header makes browser writes non-simple requests and prevents a
    # stray form POST from mutating the inbox review queue.
    if request.headers.get("x-nbk-admin-action") != "1":
        raise HTTPException(status_code=403, detail="Admin action header required")
    return request.headers.get("x-nbk-user-email") or "dashboard"


def _authenticated_actor(request: Request) -> str:
    """Identity injected by the public auth gateway for OAuth callbacks."""
    actor = request.headers.get("x-nbk-user-email")
    if not actor:
        raise HTTPException(status_code=401, detail="Authentication required")
    return actor


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
        source: Literal["website", "email"] | None = None,
        q: str | None = Query(None, min_length=1, max_length=200),
        limit: int = Query(25, ge=1, le=100),
        offset: int = Query(0, ge=0),
    ) -> dict[str, Any]:
        """Filterable article metadata; content stays at the original URL."""
        filters = {
            "municipality_key": municipality,
            "kind": kind,
            "status": status,
            "search": q,
            "source_type": source,
        }
        return {
            "items": repo.list_articles(conn, **filters, limit=limit, offset=offset),
            "total": repo.count_articles(conn, **filters),
            "limit": limit,
            "offset": offset,
        }

    @app.get("/api/admin/emails")
    def admin_emails(
        request: Request,
        conn: Annotated[Connection, Depends(get_conn)],
        status: Literal["new", "review", "error", "ignored", "ingested"] | None = "review",
        limit: int = Query(50, ge=1, le=200),
        offset: int = Query(0, ge=0),
    ) -> dict[str, Any]:
        rows = repo.list_email_messages(conn, status=status, limit=limit, offset=offset)
        fields = (
            "gmail_message_id", "sender_name", "sender_email", "subject",
            "received_at", "body_text", "municipality_key", "municipality_name",
            "classification", "confidence", "classification_source",
            "assignment_reason", "sender_scope", "status", "article_id",
        )
        items = [{field: row.get(field) for field in fields} for row in rows]
        for item in items:
            item["body_text"] = (item["body_text"] or "")[:1000]
        municipalities = sorted(
            ({"key": target.key, "name": target.name}
             for target in registry(request.app.state.settings).values()),
            key=lambda item: item["name"],
        )
        return {
            "items": items,
            "municipalities": municipalities,
            "total": repo.count_email_messages(conn, status=status),
            "limit": limit,
            "offset": offset,
        }

    @app.post("/api/admin/emails/{gmail_message_id}/assign")
    def admin_assign_email(
        gmail_message_id: str,
        action: EmailAction,
        request: Request,
        conn: Annotated[Connection, Depends(get_conn)],
    ) -> dict[str, str]:
        actor = _admin_actor(request)
        if not action.municipality_key:
            raise HTTPException(status_code=422, detail="municipality_key is required")
        try:
            article_id = assign_email(
                conn, request.app.state.settings, gmail_message_id,
                action.municipality_key, remember_sender=action.remember_sender,
                actor=actor,
            )
        except KeyError as exc:
            raise HTTPException(status_code=404, detail="Email not found") from exc
        except ValueError as exc:
            raise HTTPException(status_code=422, detail=str(exc)) from exc
        return {"status": "ingested", "article_id": article_id}

    @app.post("/api/admin/emails/{gmail_message_id}/ignore")
    def admin_ignore_email(
        gmail_message_id: str,
        action: EmailAction,
        request: Request,
        conn: Annotated[Connection, Depends(get_conn)],
    ) -> dict[str, str]:
        actor = _admin_actor(request)
        try:
            ignore_email(
                conn, gmail_message_id, remember_sender=action.remember_sender,
                actor=actor,
            )
        except KeyError as exc:
            raise HTTPException(status_code=404, detail="Email not found") from exc
        except ValueError as exc:
            raise HTTPException(status_code=409, detail=str(exc)) from exc
        return {"status": "ignored"}

    @app.get("/api/admin/gmail/status")
    def admin_gmail_status(
        conn: Annotated[Connection, Depends(get_conn)],
    ) -> dict[str, Any]:
        return connection_status(conn, app.state.settings)

    @app.post("/api/admin/gmail/connect")
    def admin_gmail_connect(
        request: Request,
        conn: Annotated[Connection, Depends(get_conn)],
    ) -> dict[str, str]:
        actor = _admin_actor(request)
        try:
            authorization_url = begin_oauth(
                conn, request.app.state.settings, actor=actor
            )
        except ValueError as exc:
            raise HTTPException(status_code=503, detail=str(exc)) from exc
        return {"authorization_url": authorization_url}

    @app.get("/api/admin/gmail/callback")
    def admin_gmail_callback(
        request: Request,
        conn: Annotated[Connection, Depends(get_conn)],
        state: str = "",
        code: str = "",
        error: str | None = None,
    ) -> RedirectResponse:
        actor = _authenticated_actor(request)
        if error:
            query = urlencode({"gmail": "error", "reason": f"Google: {error}"})
            return RedirectResponse(url=f"/?{query}", status_code=303)
        try:
            complete_oauth(
                conn,
                request.app.state.settings,
                actor=actor,
                state=state,
                code=code,
            )
        except (GmailOAuthError, ValueError) as exc:
            query = urlencode({"gmail": "error", "reason": str(exc)})
            return RedirectResponse(url=f"/?{query}", status_code=303)
        query = urlencode({"gmail": "connected"})
        return RedirectResponse(url=f"/?{query}", status_code=303)

    @app.post("/api/admin/gmail/disconnect")
    def admin_gmail_disconnect(
        request: Request,
        conn: Annotated[Connection, Depends(get_conn)],
    ) -> dict[str, Any]:
        _admin_actor(request)
        try:
            revoked = disconnect_gmail(conn, request.app.state.settings)
        except GmailOAuthError as exc:
            raise HTTPException(status_code=409, detail=str(exc)) from exc
        return {"status": "disconnected", "revoked": revoked}

    @app.get("/", response_class=HTMLResponse)
    def dashboard() -> str:
        return files("nbkommune").joinpath("static/dashboard.html").read_text("utf-8")

    return app
