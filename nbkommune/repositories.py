"""All SQL. Nothing else in the app writes the database.

Conventions, mirroring what the meeting scraper settled on the hard way:

- **Queue state transitions commit themselves** (``pop_due_task``,
  ``complete_task``, ``fail_task``, ``retry_task``, ``cancel_task``) and so does
  ``record_error`` — a claim or a failure record that is lost to a rollback is
  a task that runs twice or a failure nobody sees.
- **Plain writes follow callers-commit**, so a discovery pass either lands whole
  or not at all.
- Timestamps are stored as ISO 8601 UTC strings in Bunny Database.
"""
from __future__ import annotations

import json
import logging
import re
from datetime import UTC, datetime, timedelta
from typing import Any

from nbkommune.db import Connection

logger = logging.getLogger(__name__)

# ── priorities ───────────────────────────────────────────────────────────────
# Higher runs first. The fast lane is everything a human would expect to happen
# now; below it sits paced work that must never starve the fast lane.
PRIORITY_MANUAL = 100      # an operator asked for it
PRIORITY_NEW = 80          # a newly discovered article
PRIORITY_CHANGED = 70      # a known article whose listing changed
PRIORITY_DISCOVER = 60     # a target's scheduled discovery pass
PRIORITY_EMAIL = 60        # poll the shared municipal inbox
FAST_LANE_MIN = 50         # ── everything at or above here is the fast lane ──
PRIORITY_RECHECK = 30      # verify an ingested article for late edits
PRIORITY_BACKFILL = 10     # deliberate archive crawl

INGEST_PRIORITY = {
    "new": PRIORITY_NEW,
    "changed": PRIORITY_CHANGED,
    "manual": PRIORITY_MANUAL,
    "recheck": PRIORITY_RECHECK,
    "date-revalidation": PRIORITY_RECHECK,
    "backfill": PRIORITY_BACKFILL,
}


def now_iso() -> str:
    return datetime.now(UTC).isoformat(timespec="seconds")


def _ahead(seconds: float) -> str:
    return (datetime.now(UTC)
            + timedelta(seconds=seconds)).isoformat(timespec="seconds")


# ── municipality ─────────────────────────────────────────────────────────────
def upsert_municipality(conn: Connection, target, *,
                        channel: str | None = None,
                        channel_detail: str | None = None,
                        channel_config: str | None = None) -> None:
    """Record/refresh a kommune from its registry entry. Caller commits.

    ``channel`` is the channel that *resolved*, not the configured one — storing
    the literal "auto" for 93 of 98 targets would defeat the point of recording
    it. Pass it once discovery knows; omit it and any previously resolved value
    is kept rather than overwritten with the config placeholder.

    ``first_seen_at`` is written once and never moved — it is how we know when a
    kommune entered the corpus.
    """
    conn.execute(
        """
        INSERT INTO municipality (
            key, name, site_url, news_url, press_url, source_type, channel,
            channel_detail, channel_config, enabled, note, first_seen_at)
        VALUES (%(key)s, %(name)s, %(site)s, %(news)s, %(press)s, %(stype)s,
                %(channel)s, %(cdetail)s, %(cconfig)s, %(enabled)s, %(note)s, %(now)s)
        ON CONFLICT (key) DO UPDATE SET
            name = excluded.name,
            site_url = excluded.site_url,
            news_url = excluded.news_url,
            press_url = excluded.press_url,
            source_type = excluded.source_type,
            channel = COALESCE(excluded.channel, municipality.channel),
            channel_detail = COALESCE(excluded.channel_detail,
                                      municipality.channel_detail),
            channel_config = COALESCE(excluded.channel_config,
                                      municipality.channel_config),
            enabled = excluded.enabled,
            note = excluded.note
        """,
        {"key": target.key, "name": target.name, "site": target.site_url,
         "news": target.news_url, "press": target.press_url,
         "stype": target.source_type, "channel": channel or target.channel,
         "cdetail": channel_detail, "cconfig": channel_config,
         "enabled": target.enabled,
         "note": target.note, "now": now_iso()},
    )


def stored_resolution(conn: Connection, key: str) -> tuple[str, dict] | None:
    """A previously resolved ``(channel, config)`` for this kommune, if any.

    Discovery uses this to skip channel probing. Returns None when nothing has
    been resolved yet, or when the stored JSON is unreadable — in both cases the
    caller simply resolves again.
    """
    row = conn.execute(
        "SELECT channel, channel_config FROM municipality WHERE key = %s", (key,)
    ).fetchone()
    if not row or not row["channel"] or row["channel"] == "auto":
        return None
    try:
        config = json.loads(row["channel_config"]) if row["channel_config"] else {}
    except (TypeError, ValueError):
        return None
    return row["channel"], config if isinstance(config, dict) else {}


def clear_resolution(conn: Connection, keys: list[str]) -> int:
    """Forget the stored channel for these kommuner. Caller commits."""
    if not keys:
        return 0
    placeholders = ", ".join("?" for _ in keys)
    rows = conn.execute(
        "UPDATE municipality SET channel = 'auto', channel_config = NULL, "
        f"channel_detail = NULL WHERE key IN ({placeholders}) RETURNING key", keys,
    ).fetchall()
    return len(rows)


def touch_municipality(conn: Connection, key: str, *, ok: bool) -> None:
    """Stamp a crawl attempt. ``last_ok_at`` only moves on success, so the gap
    between the two columns is exactly how long a kommune has been failing."""
    conn.execute(
        "UPDATE municipality SET last_crawled_at = %s, "
        "last_ok_at = CASE WHEN %s THEN %s ELSE last_ok_at END WHERE key = %s",
        (now_iso(), ok, now_iso(), key),
    )


def get_municipality(conn: Connection, key: str) -> dict | None:
    return conn.execute("SELECT * FROM municipality WHERE key = %s", (key,)).fetchone()


def list_municipalities(conn: Connection) -> list[dict]:
    return conn.execute("SELECT * FROM municipality ORDER BY key").fetchall()


# ── articles ─────────────────────────────────────────────────────────────────
def get_article(conn: Connection, municipality_key: str,
                article_id: str) -> dict | None:
    return conn.execute(
        "SELECT * FROM article WHERE municipality_key = %s AND id = %s",
        (municipality_key, article_id),
    ).fetchone()


def upsert_listed_article(conn: Connection, row: dict[str, Any]) -> None:
    """Write listing-level metadata. Caller commits.

    Never touches ``body_text``/``detail_hash``/``ingested_at``: a listing pass
    knows nothing about content, and clobbering an ingested body with a listing's
    NULLs would silently empty the corpus. ``status`` is likewise left alone once
    set — only ingest and the gone-check move it.

    Title and summary are only *filled in*, never overwritten, once the article
    is ingested: the article page's own title is better than a listing teaser,
    and a rotating listing blurb must not keep overwriting it.
    """
    conn.execute(
        """
        INSERT INTO article (
            municipality_key, id, url, canonical_url, title, summary,
            published_at, updated_at, kind, channel, listing_hash, raw_json,
            status, first_seen_at, checked_at)
        VALUES (%(municipality_key)s, %(id)s, %(url)s, %(canonical_url)s,
                %(title)s, %(summary)s, %(published_at)s, %(updated_at)s,
                %(kind)s, %(channel)s, %(listing_hash)s, %(raw_json)s,
                'listed', %(now)s, %(now)s)
        ON CONFLICT (municipality_key, id) DO UPDATE SET
            url = excluded.url,
            canonical_url = excluded.canonical_url,
            title = CASE WHEN article.status = 'listed' OR article.title IS NULL
                         THEN excluded.title ELSE article.title END,
            summary = CASE WHEN article.status = 'listed' OR article.summary IS NULL
                           THEN excluded.summary ELSE article.summary END,
            published_at = COALESCE(excluded.published_at, article.published_at),
            updated_at = COALESCE(excluded.updated_at, article.updated_at),
            kind = COALESCE(excluded.kind, article.kind),
            channel = excluded.channel,
            listing_hash = excluded.listing_hash,
            raw_json = CASE WHEN article.status = 'listed'
                            THEN excluded.raw_json ELSE article.raw_json END,
            checked_at = excluded.checked_at
        """,
        {**row, "now": now_iso()},
    )


def save_article_detail(conn: Connection, row: dict[str, Any], *,
                        thin: bool, clear_published_at: bool = False) -> bool:
    """Store extracted content. Caller commits. Returns True on real change.

    ``ingested_at`` moves **only** when ``detail_hash`` changes — that timestamp
    is the signal a downstream consumer uses to decide what to re-index, so
    moving it on an unchanged re-fetch would re-index the whole corpus on every
    recheck. ``checked_at`` moves every time.
    """
    existing = conn.execute(
        "SELECT detail_hash, published_at, provenance_json FROM article "
        "WHERE municipality_key = %s AND id = %s",
        (row["municipality_key"], row["id"]),
    ).fetchone()
    changed = existing is None or existing["detail_hash"] != row["detail_hash"]

    # A recheck may find a page temporarily missing metadata that was previously
    # read from a trusted source. COALESCE already preserves the value; preserve
    # its provenance as well so it cannot silently turn into an unexplained date.
    # The repair path opts out for legacy guessed values and clears them if the
    # stricter extractor cannot recover a trustworthy replacement.
    if (existing and existing.get("published_at") and not row.get("published_at")
            and not clear_published_at):
        try:
            old_provenance = json.loads(existing.get("provenance_json") or "{}")
            new_provenance = json.loads(row.get("provenance_json") or "{}")
        except (TypeError, json.JSONDecodeError):
            old_provenance, new_provenance = {}, {}
        old_source = old_provenance.get("published_at")
        if old_source:
            new_provenance["published_at"] = old_source
            row = {**row, "provenance_json": json.dumps(
                new_provenance, ensure_ascii=False
            )}
    conn.execute(
        """
        UPDATE article SET
            url = %(url)s,
            canonical_url = %(canonical_url)s,
            title = COALESCE(%(title)s, title),
            summary = COALESCE(%(summary)s, summary),
            body_text = %(body_text)s,
            body_html = %(body_html)s,
            image_url = COALESCE(%(image_url)s, image_url),
            author = COALESCE(%(author)s, author),
            categories_json = %(categories_json)s,
            lang = COALESCE(%(lang)s, lang),
            word_count = %(word_count)s,
            published_at = CASE WHEN %(clear_published_at)s
                           THEN %(published_at)s
                           ELSE COALESCE(%(published_at)s, published_at) END,
            updated_at = COALESCE(%(updated_at)s, updated_at),
            detail_hash = %(detail_hash)s,
            provenance_json = %(provenance_json)s,
            raw_json = %(raw_json)s,
            thin = %(thin)s,
            status = 'ingested',
            gone_at = NULL,
            checked_at = %(now)s,
            ingested_at = CASE WHEN %(changed)s THEN %(now)s ELSE ingested_at END
        WHERE municipality_key = %(municipality_key)s AND id = %(id)s
        """,
        {**row, "thin": thin, "clear_published_at": clear_published_at,
         "changed": changed, "now": now_iso()},
    )
    return changed


def set_article_kind(conn: Connection, municipality_key: str,
                     article_id: str, kind: str) -> None:
    """Set the nyhed/pressemeddelelse classification. Caller commits."""
    conn.execute(
        "UPDATE article SET kind = %s WHERE municipality_key = %s AND id = %s",
        (kind, municipality_key, article_id),
    )


def mark_article_gone(conn: Connection, municipality_key: str,
                      article_id: str) -> None:
    """Tombstone an article whose URL no longer resolves. Caller commits.

    Kept rather than deleted: a consumer that indexed it needs to see that it
    went away, and a kommune that briefly 404s during a migration must not
    silently lose its history.
    """
    conn.execute(
        "UPDATE article SET status = 'gone', gone_at = %s, checked_at = %s "
        "WHERE municipality_key = %s AND id = %s AND status <> 'gone'",
        (now_iso(), now_iso(), municipality_key, article_id),
    )


def touch_article_checked(conn: Connection, municipality_key: str,
                          article_id: str) -> None:
    conn.execute(
        "UPDATE article SET checked_at = %s WHERE municipality_key = %s AND id = %s",
        (now_iso(), municipality_key, article_id),
    )


def upsert_article_source(conn: Connection, *, municipality_key: str,
                          article_id: str, source_type: str, external_id: str,
                          source_url: str | None = None,
                          title: str | None = None,
                          body_text: str | None = None,
                          body_html: str | None = None,
                          received_at: str | None = None,
                          metadata: dict[str, Any] | None = None) -> None:
    """Attach one concrete website/email occurrence to an article.

    The article row is the canonical, consumer-facing record. Sources retain
    each rendition independently, so a richer email does not overwrite a web
    page and an unchanged web recheck does not erase email provenance.
    Caller commits.
    """
    now = now_iso()
    conn.execute(
        """
        INSERT INTO article_source (
            municipality_key, article_id, source_type, external_id, source_url,
            title, body_text, body_html, received_at, metadata_json,
            first_seen_at, last_seen_at)
        VALUES (%(mk)s, %(aid)s, %(stype)s, %(external)s, %(url)s, %(title)s,
                %(text)s, %(html)s, %(received)s, %(metadata)s, %(now)s, %(now)s)
        ON CONFLICT (source_type, external_id) DO UPDATE SET
            municipality_key = excluded.municipality_key,
            article_id = excluded.article_id,
            source_url = COALESCE(excluded.source_url, article_source.source_url),
            title = COALESCE(excluded.title, article_source.title),
            body_text = COALESCE(excluded.body_text, article_source.body_text),
            body_html = COALESCE(excluded.body_html, article_source.body_html),
            received_at = COALESCE(excluded.received_at, article_source.received_at),
            metadata_json = COALESCE(excluded.metadata_json, article_source.metadata_json),
            last_seen_at = excluded.last_seen_at
        """,
        {"mk": municipality_key, "aid": article_id, "stype": source_type,
         "external": external_id, "url": source_url, "title": title,
         "text": body_text, "html": body_html, "received": received_at,
         "metadata": json.dumps(metadata, ensure_ascii=False) if metadata else None,
         "now": now},
    )


def article_sources(conn: Connection, municipality_key: str,
                    article_id: str) -> list[dict]:
    return conn.execute(
        "SELECT * FROM article_source WHERE municipality_key = %s AND article_id = %s "
        "ORDER BY first_seen_at",
        (municipality_key, article_id),
    ).fetchall()


# ── email inbox ───────────────────────────────────────────────────────────────────
def email_message_exists(conn: Connection, gmail_message_id: str) -> bool:
    return conn.execute(
        "SELECT 1 AS found FROM email_message WHERE gmail_message_id = %s",
        (gmail_message_id,),
    ).fetchone() is not None


def insert_email_message(conn: Connection, row: dict[str, Any]) -> bool:
    """Persist a parsed Gmail message exactly once. Caller commits."""
    result = conn.execute(
        """
        INSERT INTO email_message (
            gmail_message_id, gmail_thread_id, sender_name, sender_email,
            subject, sent_at, received_at, body_text, body_html, links_json,
            status, raw_json, created_at)
        VALUES (%(id)s, %(thread_id)s, %(sender_name)s, %(sender_email)s,
                %(subject)s, %(sent_at)s, %(received_at)s, %(body_text)s,
                %(body_html)s, %(links_json)s, 'new', %(raw_json)s, %(now)s)
        ON CONFLICT (gmail_message_id) DO NOTHING
        """,
        {**row, "now": now_iso()},
    )
    return bool(result.rowcount)


def get_email_message(conn: Connection, gmail_message_id: str) -> dict | None:
    return conn.execute(
        "SELECT * FROM email_message WHERE gmail_message_id = %s",
        (gmail_message_id,),
    ).fetchone()


def set_email_decision(conn: Connection, gmail_message_id: str, *,
                       municipality_key: str | None, classification: str,
                       confidence: float, source: str, reason: str,
                       sender_scope: str, status: str,
                       article_id: str | None = None) -> None:
    """Store the routing decision and processing outcome. Caller commits."""
    conn.execute(
        """
        UPDATE email_message SET
            municipality_key = %(mk)s,
            classification = %(classification)s,
            confidence = %(confidence)s,
            classification_source = %(source)s,
            assignment_reason = %(reason)s,
            sender_scope = %(scope)s,
            status = %(status)s,
            article_id = %(article_id)s,
            processed_at = %(now)s
        WHERE gmail_message_id = %(id)s
        """,
        {"id": gmail_message_id, "mk": municipality_key,
         "classification": classification, "confidence": confidence,
         "source": source, "reason": reason, "scope": sender_scope,
         "status": status, "article_id": article_id, "now": now_iso()},
    )


def get_sender_resolution(conn: Connection, sender_email: str) -> dict | None:
    return conn.execute(
        "SELECT * FROM email_sender_resolution WHERE sender_email = %s",
        (sender_email.casefold().strip(),),
    ).fetchone()


def upsert_sender_resolution(conn: Connection, *, sender_email: str, mode: str,
                             municipality_key: str | None, confidence: float,
                             reason: str, source: str) -> None:
    now = now_iso()
    conn.execute(
        """
        INSERT INTO email_sender_resolution (
            sender_email, mode, municipality_key, confidence, reason,
            resolution_source, first_seen_at, last_seen_at)
        VALUES (%(email)s, %(mode)s, %(mk)s, %(confidence)s, %(reason)s,
                %(source)s, %(now)s, %(now)s)
        ON CONFLICT (sender_email) DO UPDATE SET
            mode = excluded.mode,
            municipality_key = excluded.municipality_key,
            confidence = excluded.confidence,
            reason = excluded.reason,
            resolution_source = excluded.resolution_source,
            last_seen_at = excluded.last_seen_at
        """,
        {"email": sender_email.casefold().strip(), "mode": mode,
         "mk": municipality_key, "confidence": confidence, "reason": reason,
         "source": source, "now": now},
    )


def list_email_messages(conn: Connection, *, status: str | None = None,
                        limit: int = 100, offset: int = 0) -> list[dict]:
    where = "WHERE e.status = %(status)s" if status else ""
    return conn.execute(
        f"""SELECT e.*, m.name AS municipality_name
            FROM email_message e
            LEFT JOIN municipality m ON m.key = e.municipality_key
            {where}
            ORDER BY e.received_at DESC, e.created_at DESC
            LIMIT %(limit)s OFFSET %(offset)s""",
        {"status": status, "limit": limit, "offset": offset},
    ).fetchall()


def count_email_messages(conn: Connection, *, status: str | None = None) -> int:
    where = "WHERE status = %s" if status else ""
    row = conn.execute(
        f"SELECT COUNT(*) AS n FROM email_message {where}",
        (status,) if status else None,
    ).fetchone()
    return int(row["n"] if row else 0)


# ── Gmail OAuth connection ──────────────────────────────────────────────────
def get_gmail_connection(conn: Connection) -> dict | None:
    return conn.execute(
        "SELECT * FROM gmail_connection WHERE singleton_id = 1"
    ).fetchone()


def upsert_gmail_connection(conn: Connection, *, email_address: str,
                            refresh_token_enc: str, scopes: str,
                            connected_by: str) -> None:
    """Replace the singleton inbox connection. Caller commits."""
    now = now_iso()
    conn.execute(
        """
        INSERT INTO gmail_connection (
            singleton_id, email_address, refresh_token_enc, scopes,
            connected_by, connected_at, updated_at)
        VALUES (1, %(email)s, %(token)s, %(scopes)s, %(actor)s, %(now)s, %(now)s)
        ON CONFLICT (singleton_id) DO UPDATE SET
            email_address = excluded.email_address,
            refresh_token_enc = excluded.refresh_token_enc,
            scopes = excluded.scopes,
            connected_by = excluded.connected_by,
            connected_at = excluded.connected_at,
            updated_at = excluded.updated_at,
            last_sync_at = NULL,
            last_sync_error = NULL
        """,
        {"email": email_address.casefold().strip(), "token": refresh_token_enc,
         "scopes": scopes, "actor": connected_by, "now": now},
    )


def delete_gmail_connection(conn: Connection) -> bool:
    """Remove the local OAuth grant. Caller commits."""
    result = conn.execute("DELETE FROM gmail_connection WHERE singleton_id = 1")
    return bool(result.rowcount)


def set_gmail_sync_result(conn: Connection, *, error: str | None = None) -> None:
    """Record collector health without ever touching encrypted credentials."""
    conn.execute(
        """
        UPDATE gmail_connection SET
            last_sync_at = CASE WHEN %(error)s IS NULL THEN %(now)s ELSE last_sync_at END,
            last_sync_error = %(error)s,
            updated_at = %(now)s
        WHERE singleton_id = 1
        """,
        {"error": error[:1000] if error else None, "now": now_iso()},
    )


def create_gmail_oauth_state(conn: Connection, *, state_hash: str,
                             code_verifier_enc: str, actor: str,
                             expires_at: str) -> None:
    """Persist one short-lived OAuth/PKCE transaction. Caller commits."""
    now = now_iso()
    conn.execute("DELETE FROM gmail_oauth_state WHERE expires_at < %s", (now,))
    conn.execute(
        """
        INSERT INTO gmail_oauth_state (
            state_hash, code_verifier_enc, actor, created_at, expires_at)
        VALUES (%s, %s, %s, %s, %s)
        """,
        (state_hash, code_verifier_enc, actor, now, expires_at),
    )


def consume_gmail_oauth_state(conn: Connection, state_hash: str) -> dict | None:
    """Fetch and delete an OAuth state atomically from the caller's transaction."""
    return conn.execute(
        "DELETE FROM gmail_oauth_state WHERE state_hash = %s RETURNING *",
        (state_hash,),
    ).fetchone()


_SEARCH_TERM = re.compile(r"[^\W_]+", re.UNICODE)


def _fts_query(value: str) -> str:
    """Turn plain user text into a safe prefix query for FTS5.

    Dashboard search is deliberately not an FTS query-language endpoint. That
    avoids syntax errors from punctuation and prevents operators such as NEAR
    or column filters from changing the meaning of an ordinary search box.
    Prefix matching makes Danish compounds and partially typed words useful.
    """
    terms = _SEARCH_TERM.findall(value.casefold())[:12]
    return " AND ".join(f'"{term}"*' for term in terms)


def list_articles(conn: Connection, *, municipality_key: str | None = None,
                  status: str | None = None, kind: str | None = None,
                  thin: bool | None = None, search: str | None = None,
                  source_type: str | None = None,
                  limit: int = 100,
                  offset: int = 0) -> list[dict]:
    where, params = [], {}
    if municipality_key:
        where.append("a.municipality_key = %(mk)s")
        params["mk"] = municipality_key
    if status:
        where.append("a.status = %(status)s")
        params["status"] = status
    if kind:
        where.append("a.kind = %(kind)s")
        params["kind"] = kind
    if thin is not None:
        where.append("a.thin = %(thin)s")
        params["thin"] = thin
    if source_type:
        where.append(
            "(EXISTS (SELECT 1 FROM article_source sf WHERE "
            "sf.municipality_key = a.municipality_key AND sf.article_id = a.id "
            "AND sf.source_type = %(source_type)s) OR "
            "(NOT EXISTS (SELECT 1 FROM article_source sx WHERE "
            "sx.municipality_key = a.municipality_key AND sx.article_id = a.id) "
            "AND CASE WHEN a.channel = 'email' THEN 'email' ELSE 'website' END "
            "= %(source_type)s))"
        )
        params["source_type"] = source_type
    fts = _fts_query(search or "")
    if fts:
        where.append("article_fts MATCH %(search)s")
        params["search"] = fts
    elif search:
        where.append("0")
    clause = f"WHERE {' AND '.join(where)}" if where else ""
    fts_join = "JOIN article_fts ON article_fts.rowid = a.rowid" if fts else ""
    excerpt = (
        ", snippet(article_fts, 2, '', '', ' … ', 28) AS excerpt" if fts else ""
    )
    ordering = (
        "bm25(article_fts, 8.0, 3.0, 1.0), " if fts else ""
    )
    return conn.execute(
        f"""SELECT a.municipality_key, m.name AS municipality_name, a.id, a.url,
                   a.title, a.kind, a.status, a.published_at, a.word_count,
                   a.thin, a.ingested_at,
                   COALESCE((
                       SELECT GROUP_CONCAT(DISTINCT source_type)
                       FROM article_source src
                       WHERE src.municipality_key = a.municipality_key
                         AND src.article_id = a.id
                   ), CASE WHEN a.channel = 'email' THEN 'email' ELSE 'website' END)
                   AS sources{excerpt}
            FROM article a JOIN municipality m ON m.key = a.municipality_key
            {fts_join}
            {clause}
            ORDER BY {ordering}a.published_at DESC NULLS LAST, a.first_seen_at DESC
            LIMIT %(limit)s OFFSET %(offset)s""",
        {**params, "limit": limit, "offset": offset},
    ).fetchall()


def count_articles(conn: Connection, *, municipality_key: str | None = None,
                   status: str | None = None, kind: str | None = None,
                   search: str | None = None,
                   source_type: str | None = None) -> int:
    """Count articles using the public dashboard's list filters."""
    where, params = [], {}
    if municipality_key:
        where.append("a.municipality_key = %(mk)s")
        params["mk"] = municipality_key
    if status:
        where.append("a.status = %(status)s")
        params["status"] = status
    if kind:
        where.append("a.kind = %(kind)s")
        params["kind"] = kind
    if source_type:
        where.append(
            "(EXISTS (SELECT 1 FROM article_source sf WHERE "
            "sf.municipality_key = a.municipality_key AND sf.article_id = a.id "
            "AND sf.source_type = %(source_type)s) OR "
            "(NOT EXISTS (SELECT 1 FROM article_source sx WHERE "
            "sx.municipality_key = a.municipality_key AND sx.article_id = a.id) "
            "AND CASE WHEN a.channel = 'email' THEN 'email' ELSE 'website' END "
            "= %(source_type)s))"
        )
        params["source_type"] = source_type
    fts = _fts_query(search or "")
    if fts:
        where.append("article_fts MATCH %(search)s")
        params["search"] = fts
    elif search:
        where.append("0")
    clause = f"WHERE {' AND '.join(where)}" if where else ""
    fts_join = "JOIN article_fts ON article_fts.rowid = a.rowid" if fts else ""
    row = conn.execute(
        f"SELECT COUNT(*) AS n FROM article a {fts_join} {clause}", params
    ).fetchone()
    return int(row["n"]) if row else 0


def article_stats(conn: Connection) -> list[dict]:
    """Per-kommune counts, plus the extraction-health signals that matter:
    how many articles are thin, and how many have no publication date."""
    return conn.execute(
        """
        SELECT m.key, m.name, m.channel, m.last_ok_at,
               COUNT(a.id) AS articles,
               COUNT(*) FILTER (WHERE a.status = 'ingested') AS ingested,
               COUNT(*) FILTER (WHERE a.status = 'listed')   AS listed,
               COUNT(*) FILTER (WHERE a.status = 'gone')     AS gone,
               COUNT(*) FILTER (WHERE a.thin)                AS thin,
               COUNT(*) FILTER (WHERE a.published_at IS NULL AND a.status = 'ingested')
                   AS undated,
               MAX(a.ingested_at) AS last_ingested_at
        FROM municipality m LEFT JOIN article a ON a.municipality_key = m.key
        GROUP BY m.key, m.name, m.channel, m.last_ok_at
        ORDER BY m.key
        """
    ).fetchall()


def legacy_date_articles(
    conn: Connection, *, limit: int = 1000
) -> list[dict]:
    """Ingested rows carrying a legacy, imprecise provenance label.

    Old releases used ``listing`` for both guessed HTML-card dates and real RSS
    pubDate values. Reingest distinguishes them using the stored channel: HTML
    guesses are revalidated or cleared, while feed dates are preserved and
    relabelled ``feed``.
    """
    return conn.execute(
        """
        SELECT a.municipality_key, a.id, a.url, a.published_at,
               json_extract(a.provenance_json, '$.published_at') AS date_source,
               m.channel
        FROM article a
        JOIN municipality m ON m.key = a.municipality_key
        WHERE a.status = 'ingested'
          AND a.published_at IS NOT NULL
          AND json_extract(a.provenance_json, '$.published_at')
              IN ('listing', 'heuristic')
        ORDER BY a.published_at DESC
        LIMIT %s
        """,
        (limit,),
    ).fetchall()


# ── task queue ───────────────────────────────────────────────────────────────
def enqueue_task(conn: Connection, *, kind: str, municipality_key: str,
                 article_id: str | None = None, reason: str, priority: int,
                 run_after: str | None = None, max_attempts: int = 5,
                 payload: str | None = None) -> int | None:
    """Queue one unit of work; escalate in place if it is already queued.

    The partial unique index turns a duplicate enqueue into an UPDATE: priority
    only ever goes up (and the reason follows it), ``run_after`` only ever moves
    earlier — so a queued backfill jumps the queue the moment discovery sees the
    same article change. ``max_attempts=0`` means unlimited, used by discover
    tasks, which must never die. Caller commits.
    """
    row = conn.execute(
        """
        INSERT INTO scrape_task (
            kind, municipality_key, article_id, reason, priority, status,
            run_after, attempts, max_attempts, payload, created_at)
        VALUES (%(kind)s, %(mk)s, %(aid)s, %(reason)s, %(priority)s, 'queued',
                %(run_after)s, 0, %(max_attempts)s, %(payload)s, %(now)s)
        ON CONFLICT (kind, municipality_key, (COALESCE(article_id, '')))
            WHERE status IN ('queued', 'running')
        DO UPDATE SET
            priority = MAX(scrape_task.priority, excluded.priority),
            reason = CASE WHEN excluded.priority > scrape_task.priority
                          THEN excluded.reason ELSE scrape_task.reason END,
            run_after = MIN(scrape_task.run_after, excluded.run_after)
        RETURNING id
        """,
        {"kind": kind, "mk": municipality_key, "aid": article_id, "reason": reason,
         "priority": priority, "run_after": run_after or now_iso(),
         "max_attempts": max_attempts, "payload": payload, "now": now_iso()},
    ).fetchone()
    return int(row["id"]) if row else None


def enqueue_ingest(conn: Connection, municipality_key: str, article_id: str,
                   reason: str, *, max_attempts: int = 5,
                   run_after: str | None = None) -> int | None:
    """Queue an article ingest at the priority its reason dictates.

    An unrecognised reason falls back to manual priority rather than raising — a
    reason typo must never take down a kommune's whole discovery pass.
    """
    return enqueue_task(
        conn, kind="ingest", municipality_key=municipality_key, article_id=article_id,
        reason=reason, priority=INGEST_PRIORITY.get(reason, PRIORITY_MANUAL),
        max_attempts=max_attempts, run_after=run_after,
    )


def ensure_discover_tasks(conn: Connection, target_keys: list[str], *,
                          interval_min: float) -> int:
    """Seed a discover task for every target without a live one. Caller commits.

    Discover tasks are self-rescheduling, so this fires only on first boot, for
    targets added while running, or after an operator cancels one. Seeds are
    staggered across the interval so a cold start of 98 sites drips instead of
    bursting. Returns the number seeded.
    """
    if not target_keys:
        return 0
    live = {r["municipality_key"] for r in conn.execute(
        "SELECT municipality_key FROM scrape_task "
        "WHERE kind = 'discover' AND status IN ('queued', 'running')"
    ).fetchall()}
    pending = [k for k in target_keys if k not in live]
    if not pending:
        return 0
    gap_s = max(1.0, interval_min * 60.0) / len(pending)
    for i, key in enumerate(pending):
        enqueue_task(conn, kind="discover", municipality_key=key, reason="schedule",
                     priority=PRIORITY_DISCOVER, run_after=_ahead(i * gap_s),
                     max_attempts=0)
    return len(pending)


def ensure_email_task(conn: Connection) -> bool:
    """Seed the singleton Gmail collector task when no live one exists."""
    existing = conn.execute(
        "SELECT id FROM scrape_task WHERE kind = 'collect_email' "
        "AND status IN ('queued', 'running') LIMIT 1"
    ).fetchone()
    if existing:
        return False
    enqueue_task(
        conn, kind="collect_email", municipality_key="_gmail",
        reason="schedule", priority=PRIORITY_EMAIL, max_attempts=0,
    )
    return True


def pop_due_task(conn: Connection, *, lane: str = "any",
                 lease_min: float = 15.0) -> dict | None:
    """Claim the next due task, or None. Commits (the claim must be visible).

    ``lane`` filters by priority class: 'fast' (>= FAST_LANE_MIN), 'paced'
    (below), or 'any'. Ordering is priority first; within a priority the
    tiebreak is the kommune served **least recently** — not enqueue order. The
    worker pops one task at a time and re-runs this query, so an age tiebreak
    lets whichever kommune holds the oldest queue head win every pop and starve
    the other 97 during a recheck wave.

    The claim is one ``UPDATE ... RETURNING`` statement inside Bunny's write
    transaction. Magic Containers runs one replica for this worker, while
    SQLite's single-writer lock still serialises an accidental second process.
    """
    lane_sql = {
        "fast": f"AND t.priority >= {FAST_LANE_MIN}",
        "paced": f"AND t.priority < {FAST_LANE_MIN}",
        "any": "",
    }[lane]
    row = conn.execute(
        f"""
        WITH last_serve AS (
            SELECT municipality_key, MAX(finished_at) AS served_at
            FROM scrape_task WHERE status = 'done' AND kind <> 'discover'
            GROUP BY municipality_key
        ),
        next_task AS (
            SELECT t.id FROM scrape_task t
            LEFT JOIN last_serve ls USING (municipality_key)
            WHERE t.status = 'queued' AND t.run_after <= %(now)s {lane_sql}
            ORDER BY t.priority DESC, ls.served_at ASC NULLS FIRST,
                     t.run_after, t.id
            LIMIT 1
        )
        UPDATE scrape_task
        SET status = 'running', attempts = attempts + 1, lease_expires_at = %(lease)s
        WHERE id = (SELECT id FROM next_task)
        RETURNING *
        """,
        {"now": now_iso(), "lease": _ahead(lease_min * 60.0)},
    ).fetchone()
    conn.commit()
    return row


def complete_task(conn: Connection, task_id: int) -> None:
    """Mark a task done. Commits."""
    conn.execute(
        "UPDATE scrape_task SET status = 'done', finished_at = %s, "
        "lease_expires_at = NULL WHERE id = %s",
        (now_iso(), task_id),
    )
    conn.commit()


def fail_task(conn: Connection, task: dict, error: str, *,
              backoff_base_s: float, backoff_max_s: float) -> str:
    """Requeue a failed task with exponential backoff, or kill it. Commits.

    The attempt was counted at pop time. Backoff = base * 2^(attempt-1), capped;
    after ``max_attempts`` the task goes ``dead`` (kept, listable, retryable).
    ``max_attempts=0`` never dies — a discover task keeps probing a broken site
    at the capped backoff. Returns ``'dead'`` or ``'requeued'``.
    """
    attempts = int(task["attempts"])
    max_attempts = int(task["max_attempts"])
    if max_attempts > 0 and attempts >= max_attempts:
        conn.execute(
            "UPDATE scrape_task SET status = 'dead', finished_at = %s, "
            "lease_expires_at = NULL, last_error = %s WHERE id = %s",
            (now_iso(), error[:2000], task["id"]),
        )
        conn.commit()
        return "dead"
    delay = min(backoff_base_s * (2 ** max(attempts - 1, 0)), backoff_max_s)
    conn.execute(
        "UPDATE scrape_task SET status = 'queued', run_after = %s, "
        "lease_expires_at = NULL, last_error = %s WHERE id = %s",
        (_ahead(delay), error[:2000], task["id"]),
    )
    conn.commit()
    return "requeued"


def expire_task_leases(conn: Connection) -> int:
    """Requeue tasks whose worker died mid-flight. Commits. Returns the count."""
    rows = conn.execute(
        "UPDATE scrape_task SET status = 'queued', lease_expires_at = NULL, "
        "last_error = 'lease expired (worker died)' "
        "WHERE status = 'running' AND lease_expires_at < %s RETURNING id",
        (now_iso(),),
    ).fetchall()
    conn.commit()
    if rows:
        logger.warning("requeued %d task(s) with an expired lease", len(rows))
    return len(rows)


def retry_task(conn: Connection, task_id: int) -> dict | None:
    """Put a dead task back in the queue with a fresh attempt budget. Commits."""
    row = conn.execute(
        "UPDATE scrape_task SET status = 'queued', attempts = 0, run_after = %s, "
        "finished_at = NULL, lease_expires_at = NULL, last_error = NULL "
        "WHERE id = %s AND status IN ('dead', 'queued') RETURNING *",
        (now_iso(), task_id),
    ).fetchone()
    conn.commit()
    return row


def cancel_task(conn: Connection, task_id: int) -> dict | None:
    """Cancel a queued or dead task. Commits."""
    row = conn.execute(
        "UPDATE scrape_task SET status = 'cancelled', finished_at = %s, "
        "lease_expires_at = NULL WHERE id = %s AND status IN ('queued', 'dead') "
        "RETURNING *",
        (now_iso(), task_id),
    ).fetchone()
    conn.commit()
    return row


def purge_finished_tasks(conn: Connection, *, retention_days: float) -> int:
    """Drop old done/cancelled rows. Commits. Dead tasks are always kept —
    they are the record of what is still broken."""
    rows = conn.execute(
        "DELETE FROM scrape_task WHERE status IN ('done', 'cancelled') "
        "AND finished_at < %s RETURNING id",
        ((datetime.now(UTC) - timedelta(days=retention_days))
         .isoformat(timespec="seconds"),),
    ).fetchall()
    conn.commit()
    return len(rows)


def list_tasks(conn: Connection, *, status: str | None = None,
               kind: str | None = None, municipality_key: str | None = None,
               limit: int = 100) -> list[dict]:
    where, params = [], {}
    if status:
        where.append("status = %(status)s")
        params["status"] = status
    if kind:
        where.append("kind = %(kind)s")
        params["kind"] = kind
    if municipality_key:
        where.append("municipality_key = %(mk)s")
        params["mk"] = municipality_key
    clause = f"WHERE {' AND '.join(where)}" if where else ""
    return conn.execute(
        f"SELECT * FROM scrape_task {clause} "
        "ORDER BY priority DESC, run_after LIMIT %(limit)s",
        {**params, "limit": limit},
    ).fetchall()


def queue_summary(conn: Connection) -> list[dict]:
    return conn.execute(
        "SELECT kind, status, COUNT(*) AS n FROM scrape_task "
        "GROUP BY kind, status ORDER BY kind, status"
    ).fetchall()


def schedule_recheck(conn: Connection, municipality_key: str, article_id: str,
                     *, published_at: str | None, settle_days: float,
                     interval_days: float) -> int | None:
    """Queue a recheck, unless the article has settled. Caller commits.

    News is corrected within days of publication, not months, so rechecking
    stops once an article is older than ``settle_days``. Without that stop, 98
    kommuner × a growing archive would grind the paced lane forever on articles
    nobody will ever touch again.
    """
    if published_at:
        try:
            age_days = (datetime.now(UTC)
                        - datetime.fromisoformat(published_at)).days
        except ValueError:
            age_days = 0
        if age_days > settle_days:
            return None
    return enqueue_task(
        conn, kind="recheck", municipality_key=municipality_key, article_id=article_id,
        reason="recheck", priority=PRIORITY_RECHECK,
        run_after=_ahead(interval_days * 86400.0), max_attempts=3,
    )


# ── error log ────────────────────────────────────────────────────────────────
def record_error(conn: Connection, *, phase: str,
                 municipality_key: str | None, exc: BaseException,
                 article_id: str | None = None, url: str | None = None,
                 task: dict | None = None) -> None:
    """Append one structured failure. Commits — a failure nobody can see is
    worse than the failure itself, so this must survive the caller's rollback."""
    try:
        conn.execute(
            """
            INSERT INTO scrape_error (
                phase, municipality_key, article_id, url, error_type, message,
                task_id, task_kind, attempts, created_at)
            VALUES (%(phase)s, %(mk)s, %(aid)s, %(url)s, %(etype)s, %(msg)s,
                    %(tid)s, %(tkind)s, %(attempts)s, %(now)s)
            """,
            {"phase": phase, "mk": municipality_key, "aid": article_id, "url": url,
             "etype": type(exc).__name__, "msg": str(exc)[:2000],
             "tid": task["id"] if task else None,
             "tkind": task["kind"] if task else None,
             "attempts": task["attempts"] if task else None, "now": now_iso()},
        )
        conn.commit()
    except Exception:
        conn.rollback()
        logger.exception("could not record error for %s/%s", municipality_key, article_id)


def recent_errors(conn: Connection, *, limit: int = 100,
                  municipality_key: str | None = None) -> list[dict]:
    clause = "WHERE municipality_key = %(mk)s" if municipality_key else ""
    return conn.execute(
        f"SELECT * FROM scrape_error {clause} ORDER BY created_at DESC LIMIT %(limit)s",
        {"mk": municipality_key, "limit": limit},
    ).fetchall()


def error_summary(conn: Connection, *, hours: float = 24.0) -> list[dict]:
    return conn.execute(
        "SELECT phase, error_type, COUNT(*) AS n, MAX(created_at) AS latest "
        "FROM scrape_error WHERE created_at > %s "
        "GROUP BY phase, error_type ORDER BY n DESC",
        ((datetime.now(UTC) - timedelta(hours=hours))
         .isoformat(timespec="seconds"),),
    ).fetchall()


# ── crawl runs ───────────────────────────────────────────────────────────────
def start_crawl_run(conn: Connection, municipality_key: str,
                    channel: str) -> int:
    row = conn.execute(
        "INSERT INTO crawl_run (municipality_key, channel, started_at) "
        "VALUES (%s, %s, %s) RETURNING id",
        (municipality_key, channel, now_iso()),
    ).fetchone()
    conn.commit()
    return int(row["id"])


def finish_crawl_run(conn: Connection, run_id: int, stats,
                     error: str | None = None) -> None:
    conn.execute(
        "UPDATE crawl_run SET finished_at = %s, seen = %s, new = %s, updated = %s, "
        "pending = %s, queued = %s, skipped_old = %s, error = %s WHERE id = %s",
        (now_iso(), stats.seen, stats.new, stats.updated, stats.pending,
         stats.queued, stats.skipped_old, error, run_id),
    )
    conn.commit()


def recent_crawl_runs(conn: Connection, *, limit: int = 20) -> list[dict]:
    """Latest discovery runs, newest first, for operational monitoring."""
    return conn.execute(
        "SELECT * FROM crawl_run ORDER BY started_at DESC LIMIT %s", (limit,)
    ).fetchall()


# ── meta ─────────────────────────────────────────────────────────────────────
def get_meta(conn: Connection, key: str) -> str | None:
    row = conn.execute("SELECT value FROM meta WHERE key = %s", (key,)).fetchone()
    return row["value"] if row else None


def set_meta(conn: Connection, key: str, value: str) -> None:
    conn.execute(
        "INSERT INTO meta (key, value) VALUES (%s, %s) "
        "ON CONFLICT (key) DO UPDATE SET value = excluded.value",
        (key, value),
    )
