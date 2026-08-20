"""Bunny Database connection and SQLite-compatible schema.

Bunny Database speaks the libSQL Hrana protocol over HTTP. The application is
synchronous, so this module provides the small DB-API-shaped surface the
repositories need: ``execute()``, ``commit()``, ``rollback()`` and ``close()``.
Read-only statements use short, auto-closed requests; writes open an interactive
transaction and keep Bunny's baton until the caller commits or rolls back.

``file:`` URLs use Python's built-in SQLite driver. This keeps local development
and repository tests fast while exercising the same SQL dialect as production.
"""
from __future__ import annotations

import base64
import logging
import re
import sqlite3
from collections.abc import Mapping
from datetime import UTC, date, datetime
from pathlib import Path
from typing import Any, Protocol, Self
from urllib.parse import urljoin

import httpx

from nbkommune.settings import Settings, get_settings

logger = logging.getLogger(__name__)

_NAMED_PLACEHOLDER = re.compile(r"%\((\w+)\)s")
_TIMESTAMP_COLUMN = re.compile(r"(?:^latest$|(?:^|_)(?:at|time|date|after)$)")


class DatabaseError(RuntimeError):
    """A Bunny Database protocol or SQL error."""


class Result(Protocol):
    def fetchone(self) -> dict[str, Any] | None: ...
    def fetchall(self) -> list[dict[str, Any]]: ...


class Connection(Protocol):
    def execute(self, sql: str, params: Any = None) -> Result: ...
    def commit(self) -> None: ...
    def rollback(self) -> None: ...
    def close(self) -> None: ...


class _Result:
    def __init__(self, rows: list[dict[str, Any]], *, rowcount: int = 0,
                 lastrowid: int | None = None) -> None:
        self._rows = rows
        self._position = 0
        self.rowcount = rowcount
        self.lastrowid = lastrowid

    def fetchone(self) -> dict[str, Any] | None:
        if self._position >= len(self._rows):
            return None
        row = self._rows[self._position]
        self._position += 1
        return row

    def fetchall(self) -> list[dict[str, Any]]:
        rows = self._rows[self._position:]
        self._position = len(self._rows)
        return rows


def _translate_sql(sql: str) -> str:
    """Translate psycopg placeholders retained in repository SQL to SQLite."""
    sql = _NAMED_PLACEHOLDER.sub(r":\1", sql)
    return sql.replace("%s", "?")


def _normalise_param(value: Any) -> Any:
    if isinstance(value, bool):
        return int(value)
    if isinstance(value, (datetime, date)):
        return value.isoformat()
    return value


def _decode_value(column: str, value: Any) -> Any:
    if isinstance(value, str) and _TIMESTAMP_COLUMN.search(column):
        try:
            return datetime.fromisoformat(value)
        except ValueError:
            pass
    return value


class SQLiteConnection:
    """Local SQLite implementation used for development and tests."""

    def __init__(self, path: str) -> None:
        self._conn = sqlite3.connect(path)
        self._conn.row_factory = self._row_factory
        self._conn.execute("PRAGMA foreign_keys = ON")

    @staticmethod
    def _row_factory(cursor: sqlite3.Cursor, row: tuple[Any, ...]) -> dict[str, Any]:
        return {
            column[0]: _decode_value(column[0], value)
            for column, value in zip(cursor.description, row, strict=True)
        }

    def execute(self, sql: str, params: Any = None) -> sqlite3.Cursor:
        sql = _translate_sql(sql)
        if isinstance(params, Mapping):
            params = {key: _normalise_param(value) for key, value in params.items()}
        elif params is not None:
            params = tuple(_normalise_param(value) for value in params)
        return self._conn.execute(sql, params or ())

    def commit(self) -> None:
        self._conn.commit()

    def rollback(self) -> None:
        self._conn.rollback()

    def close(self) -> None:
        self._conn.close()


class BunnyConnection:
    """Synchronous libSQL/Hrana HTTP connection with caller-controlled writes."""

    def __init__(self, url: str, auth_token: str, *, timeout_s: float = 30.0,
                 transport: httpx.BaseTransport | None = None) -> None:
        host_url = url.replace("libsql://", "https://", 1).rstrip("/")
        if not host_url.startswith(("http://", "https://")):
            raise ValueError("BUNNY_DATABASE_URL must use libsql:// or https://")
        self._pipeline_url = f"{host_url}/v2/pipeline"
        self._request_url = self._pipeline_url
        self._baton: str | None = None
        self._client = httpx.Client(
            headers={"Authorization": f"Bearer {auth_token}"},
            timeout=timeout_s,
            transport=transport,
        )

    @staticmethod
    def _encode(value: Any) -> dict[str, str]:
        value = _normalise_param(value)
        if value is None:
            return {"type": "null"}
        if isinstance(value, int):
            return {"type": "integer", "value": str(value)}
        if isinstance(value, float):
            return {"type": "float", "value": str(value)}
        if isinstance(value, bytes):
            return {"type": "blob", "base64": base64.b64encode(value).decode("ascii")}
        return {"type": "text", "value": str(value)}

    @staticmethod
    def _decode(value: dict[str, Any]) -> Any:
        kind = value.get("type")
        if kind == "null":
            return None
        if kind == "integer":
            return int(value["value"])
        if kind == "float":
            return float(value["value"])
        if kind == "blob":
            payload = value.get("base64", value.get("value", ""))
            return base64.b64decode(payload)
        return value.get("value")

    def _statement(self, sql: str, params: Any) -> dict[str, Any]:
        translated = _translate_sql(sql)
        stmt: dict[str, Any] = {"sql": translated}
        if isinstance(params, Mapping):
            # psycopg tolerated unused mapping keys; Hrana rejects them. A few
            # repository queries build one parameter mapping for optional WHERE
            # clauses, so only transmit names that occur in the final SQL.
            used_names = set(re.findall(r"[:@$](\w+)", translated))
            stmt["named_args"] = [
                {"name": key, "value": self._encode(value)}
                for key, value in params.items() if key in used_names
            ]
        elif params is not None:
            stmt["args"] = [self._encode(value) for value in params]
        return stmt

    def _post(self, requests: list[dict[str, Any]], *, close: bool = False) -> list[Any]:
        if close:
            requests = [*requests, {"type": "close"}]
        payload: dict[str, Any] = {"requests": requests}
        if self._baton:
            payload["baton"] = self._baton
        response = self._client.post(self._request_url, json=payload)
        try:
            response.raise_for_status()
        except httpx.HTTPStatusError as exc:
            raise DatabaseError(
                f"Bunny Database HTTP {response.status_code}: {response.text[:500]}"
            ) from exc
        data = response.json()
        self._baton = data.get("baton")
        if data.get("base_url"):
            self._request_url = urljoin(self._request_url, data["base_url"])

        values: list[Any] = []
        for item in data.get("results", []):
            if item.get("type") == "error":
                error = item.get("error") or {}
                raise DatabaseError(
                    f"{error.get('code', 'SQL_ERROR')}: {error.get('message', error)}"
                )
            values.append(item.get("response"))
        return values

    @staticmethod
    def _is_read(sql: str) -> bool:
        first = sql.lstrip().split(None, 1)[0].upper() if sql.strip() else ""
        return first in {"SELECT", "PRAGMA", "EXPLAIN"}

    @classmethod
    def _result(cls, response: dict[str, Any] | None) -> _Result:
        result = (response or {}).get("result") or {}
        columns = [column.get("name", "") for column in result.get("cols", [])]
        rows = [
            {
                column: _decode_value(column, cls._decode(value))
                for column, value in zip(columns, row, strict=True)
            }
            for row in result.get("rows", [])
        ]
        last = result.get("last_insert_rowid")
        return _Result(
            rows,
            rowcount=int(result.get("affected_row_count") or 0),
            lastrowid=int(last) if last is not None else None,
        )

    def execute(self, sql: str, params: Any = None) -> _Result:
        request = {"type": "execute", "stmt": self._statement(sql, params)}
        if self._baton:
            responses = self._post([request])
            return self._result(responses[0])

        if self._is_read(sql):
            responses = self._post([request], close=True)
            return self._result(responses[0])

        # PRAGMA is scoped to the transaction's stream, so enable foreign-key
        # checks whenever a new write transaction is opened.
        responses = self._post([
            {"type": "execute", "stmt": {"sql": "PRAGMA foreign_keys = ON"}},
            {"type": "execute", "stmt": {"sql": "BEGIN IMMEDIATE"}},
            request,
        ])
        if not self._baton:
            raise DatabaseError("Bunny Database did not return a transaction baton")
        return self._result(responses[2])

    def commit(self) -> None:
        if not self._baton:
            return
        self._post([{"type": "execute", "stmt": {"sql": "COMMIT"}}], close=True)
        self._request_url = self._pipeline_url

    def rollback(self) -> None:
        if not self._baton:
            return
        try:
            self._post([{"type": "execute", "stmt": {"sql": "ROLLBACK"}}], close=True)
        finally:
            self._baton = None
            self._request_url = self._pipeline_url

    def close(self) -> None:
        try:
            self.rollback()
        finally:
            self._client.close()

    def __enter__(self) -> Self:
        return self

    def __exit__(self, *_exc: object) -> None:
        self.close()


def _table_statements() -> list[str]:
    return [
        """
        CREATE TABLE IF NOT EXISTS municipality (
            key             TEXT PRIMARY KEY,
            name            TEXT NOT NULL,
            site_url        TEXT,
            news_url        TEXT,
            press_url       TEXT,
            source_type     TEXT,
            channel         TEXT,
            channel_detail  TEXT,
            channel_config  TEXT,
            enabled         INTEGER NOT NULL DEFAULT 1,
            note            TEXT,
            first_seen_at   TEXT,
            last_crawled_at TEXT,
            last_ok_at      TEXT
        )
        """,
        """
        CREATE TABLE IF NOT EXISTS article (
            municipality_key TEXT NOT NULL REFERENCES municipality(key),
            id               TEXT NOT NULL,
            url              TEXT NOT NULL,
            canonical_url    TEXT,
            kind             TEXT,
            title            TEXT,
            summary          TEXT,
            body_text        TEXT,
            body_html        TEXT,
            image_url        TEXT,
            author           TEXT,
            categories_json  TEXT,
            lang             TEXT,
            word_count       INTEGER,
            published_at     TEXT,
            updated_at       TEXT,
            channel          TEXT,
            status           TEXT NOT NULL DEFAULT 'listed',
            listing_hash     TEXT,
            detail_hash      TEXT,
            provenance_json  TEXT,
            raw_json         TEXT,
            thin             INTEGER NOT NULL DEFAULT 0,
            first_seen_at    TEXT,
            checked_at       TEXT,
            ingested_at      TEXT,
            gone_at          TEXT,
            PRIMARY KEY (municipality_key, id)
        )
        """,
        "CREATE INDEX IF NOT EXISTS ix_article_status ON article(municipality_key, status)",
        "CREATE INDEX IF NOT EXISTS ix_article_published ON article(published_at DESC)",
        "CREATE INDEX IF NOT EXISTS ix_article_ingested ON article(ingested_at)",
        "CREATE INDEX IF NOT EXISTS ix_article_kind ON article(municipality_key, kind)",
        "CREATE INDEX IF NOT EXISTS ix_article_canonical "
        "ON article(municipality_key, canonical_url)",
        """
        CREATE TABLE IF NOT EXISTS article_source (
            municipality_key TEXT NOT NULL,
            article_id       TEXT NOT NULL,
            source_type      TEXT NOT NULL,
            external_id      TEXT NOT NULL,
            source_url       TEXT,
            title            TEXT,
            body_text        TEXT,
            body_html        TEXT,
            received_at      TEXT,
            metadata_json    TEXT,
            first_seen_at    TEXT NOT NULL,
            last_seen_at     TEXT NOT NULL,
            PRIMARY KEY (source_type, external_id),
            FOREIGN KEY (municipality_key, article_id)
                REFERENCES article(municipality_key, id)
        )
        """,
        "CREATE INDEX IF NOT EXISTS ix_article_source_article "
        "ON article_source(municipality_key, article_id)",
        """
        CREATE TABLE IF NOT EXISTS email_sender_resolution (
            sender_email     TEXT PRIMARY KEY,
            mode             TEXT NOT NULL,
            municipality_key TEXT REFERENCES municipality(key),
            confidence       REAL,
            reason           TEXT,
            resolution_source TEXT NOT NULL,
            first_seen_at    TEXT NOT NULL,
            last_seen_at     TEXT NOT NULL
        )
        """,
        """
        CREATE TABLE IF NOT EXISTS email_message (
            gmail_message_id TEXT PRIMARY KEY,
            gmail_thread_id  TEXT,
            sender_name      TEXT,
            sender_email     TEXT NOT NULL,
            subject          TEXT,
            sent_at          TEXT,
            received_at      TEXT,
            body_text        TEXT,
            body_html        TEXT,
            links_json       TEXT,
            municipality_key TEXT REFERENCES municipality(key),
            classification   TEXT,
            confidence       REAL,
            classification_source TEXT,
            assignment_reason TEXT,
            sender_scope     TEXT,
            status           TEXT NOT NULL DEFAULT 'new',
            article_id       TEXT,
            raw_json         TEXT,
            created_at       TEXT NOT NULL,
            processed_at     TEXT
        )
        """,
        "CREATE INDEX IF NOT EXISTS ix_email_message_status "
        "ON email_message(status, received_at DESC)",
        "CREATE INDEX IF NOT EXISTS ix_email_message_sender "
        "ON email_message(sender_email, received_at DESC)",
        """
        CREATE VIRTUAL TABLE IF NOT EXISTS article_fts USING fts5(
            title,
            summary,
            body_text,
            content = 'article',
            content_rowid = 'rowid',
            tokenize = 'unicode61 remove_diacritics 2'
        )
        """,
        """
        CREATE TRIGGER IF NOT EXISTS article_fts_insert AFTER INSERT ON article BEGIN
            INSERT INTO article_fts(rowid, title, summary, body_text)
            VALUES (new.rowid, new.title, new.summary, new.body_text);
        END
        """,
        """
        CREATE TRIGGER IF NOT EXISTS article_fts_delete AFTER DELETE ON article BEGIN
            INSERT INTO article_fts(article_fts, rowid, title, summary, body_text)
            VALUES ('delete', old.rowid, old.title, old.summary, old.body_text);
        END
        """,
        """
        CREATE TRIGGER IF NOT EXISTS article_fts_update
        AFTER UPDATE OF title, summary, body_text ON article
        WHEN old.title IS NOT new.title
          OR old.summary IS NOT new.summary
          OR old.body_text IS NOT new.body_text
        BEGIN
            INSERT INTO article_fts(article_fts, rowid, title, summary, body_text)
            VALUES ('delete', old.rowid, old.title, old.summary, old.body_text);
            INSERT INTO article_fts(rowid, title, summary, body_text)
            VALUES (new.rowid, new.title, new.summary, new.body_text);
        END
        """,
        """
        CREATE TABLE IF NOT EXISTS scrape_task (
            id               INTEGER PRIMARY KEY AUTOINCREMENT,
            kind             TEXT NOT NULL,
            municipality_key TEXT NOT NULL,
            article_id       TEXT,
            reason           TEXT,
            priority         INTEGER NOT NULL DEFAULT 0,
            status           TEXT NOT NULL DEFAULT 'queued',
            run_after        TEXT NOT NULL,
            attempts         INTEGER NOT NULL DEFAULT 0,
            max_attempts     INTEGER NOT NULL DEFAULT 5,
            lease_expires_at TEXT,
            payload          TEXT,
            last_error       TEXT,
            created_at       TEXT,
            finished_at      TEXT
        )
        """,
        "CREATE INDEX IF NOT EXISTS ix_task_pop ON scrape_task(status, run_after)",
        "CREATE INDEX IF NOT EXISTS ix_task_muni ON scrape_task(municipality_key, status)",
        "CREATE UNIQUE INDEX IF NOT EXISTS ux_task_live ON scrape_task "
        "(kind, municipality_key, (COALESCE(article_id, ''))) "
        "WHERE status IN ('queued', 'running')",
        """
        CREATE TABLE IF NOT EXISTS scrape_error (
            id               INTEGER PRIMARY KEY AUTOINCREMENT,
            phase            TEXT NOT NULL,
            municipality_key TEXT,
            article_id       TEXT,
            url              TEXT,
            error_type       TEXT,
            message          TEXT,
            task_id          INTEGER,
            task_kind        TEXT,
            attempts         INTEGER,
            created_at       TEXT
        )
        """,
        "CREATE INDEX IF NOT EXISTS ix_error_muni_time "
        "ON scrape_error(municipality_key, created_at DESC)",
        "CREATE INDEX IF NOT EXISTS ix_error_phase_time "
        "ON scrape_error(phase, created_at DESC)",
        """
        CREATE TABLE IF NOT EXISTS crawl_run (
            id               INTEGER PRIMARY KEY AUTOINCREMENT,
            municipality_key TEXT,
            channel          TEXT,
            started_at       TEXT,
            finished_at      TEXT,
            seen             INTEGER DEFAULT 0,
            new              INTEGER DEFAULT 0,
            updated          INTEGER DEFAULT 0,
            pending          INTEGER DEFAULT 0,
            queued           INTEGER DEFAULT 0,
            skipped_old      INTEGER DEFAULT 0,
            error            TEXT
        )
        """,
        "CREATE INDEX IF NOT EXISTS ix_crawl_run_muni "
        "ON crawl_run(municipality_key, started_at DESC)",
        "CREATE TABLE IF NOT EXISTS meta (key TEXT PRIMARY KEY, value TEXT)",
    ]


def init_schema(conn: Connection, settings: Settings | None = None) -> None:
    """Create every table and index idempotently.

    Each DDL statement gets its own short transaction. This avoids holding an
    interactive remote transaction across many network round trips during boot.
    """
    del settings  # retained for a stable public signature
    for statement in _table_statements():
        conn.execute(statement)
        conn.commit()
    # Creating an external-content FTS table does not index pre-existing rows.
    # Rebuild once when this migration first reaches a database; the triggers
    # above keep every later insert/update/delete in sync without application
    # code having to remember a second write.
    marker = conn.execute(
        "SELECT value FROM meta WHERE key = %s", ("schema:article_fts:v1",)
    ).fetchone()
    if marker is None:
        conn.execute("INSERT INTO article_fts(article_fts) VALUES ('rebuild')")
        conn.execute(
            "INSERT INTO meta (key, value) VALUES (%s, %s)",
            ("schema:article_fts:v1", "complete"),
        )
        conn.commit()
    source_marker = conn.execute(
        "SELECT value FROM meta WHERE key = %s", ("schema:article_source:v1",)
    ).fetchone()
    if source_marker is None:
        now = datetime.now(UTC).isoformat(timespec="seconds")
        conn.execute(
            """
            INSERT OR IGNORE INTO article_source (
                municipality_key, article_id, source_type, external_id,
                source_url, title, body_text, body_html, received_at,
                metadata_json, first_seen_at, last_seen_at)
            SELECT municipality_key, id, 'website', COALESCE(canonical_url, url),
                   url, title, body_text, body_html, ingested_at,
                   NULL, COALESCE(first_seen_at, ?), COALESCE(checked_at, ?)
            FROM article
            """,
            (now, now),
        )
        conn.execute(
            "INSERT INTO meta (key, value) VALUES (%s, %s)",
            ("schema:article_source:v1", "complete"),
        )
        conn.commit()
    logger.info("Bunny/SQLite schema ready")


def connect(settings: Settings | None = None) -> Connection:
    settings = settings or get_settings()
    if not settings.database_url:
        raise RuntimeError("BUNNY_DATABASE_URL is not set")
    if settings.database_url.startswith("file:"):
        raw_path = settings.database_url.removeprefix("file:")
        path = ":memory:" if raw_path in {"", ":memory:"} else str(Path(raw_path))
        return SQLiteConnection(path)
    if not settings.database_auth_token:
        raise RuntimeError("BUNNY_DATABASE_AUTH_TOKEN is not set")
    return BunnyConnection(
        settings.database_url,
        settings.database_auth_token,
        timeout_s=settings.database_timeout_s,
    )
