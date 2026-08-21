"""Discovery + ingest — the work the queue's tasks execute.

Discovery updates the *world* (municipality and article rows) and files work
orders on ``scrape_task``; it never downloads an article body. Ingest executes
one article: fetch the page, extract it, store it. Both are invoked by the worker
loop, which owns scheduling, pacing, retries and error logging.

Article status is world-state only: ``listed`` (URL and listing metadata known) →
``ingested`` (body stored) → ``gone`` (URL now 404s). Everything that could be
mistaken for work-state — attempts, backoff, dead-lettering — is a task.
"""
from __future__ import annotations

import json
import logging
from dataclasses import dataclass

from nbkommune import repositories as repo
from nbkommune.dates import below_floor
from nbkommune.extract import classify_kind, extract_article
from nbkommune.http import HttpClient, PermanentHttpError, RobotsDenied
from nbkommune.records import ListedArticle
from nbkommune.settings import Settings
from nbkommune.sources import make_source
from nbkommune.targets import Target

logger = logging.getLogger(__name__)

# Bunny/libSQL interactive transactions have a short lifetime. A paginated
# listing can yield dozens of articles, and each sighting writes both the
# article and its source row. Commit in bounded chunks so a healthy large
# listing cannot time out after discovery has already succeeded.
_DISCOVERY_WRITE_BATCH = 20


def _json_object(value) -> dict:
    """Decode a DB JSON value defensively; corrupt diagnostics must not stop ingest."""
    if isinstance(value, dict):
        return value
    try:
        decoded = json.loads(value or "{}")
    except (TypeError, json.JSONDecodeError):
        return {}
    return decoded if isinstance(decoded, dict) else {}


def _legacy_published_is_untrusted(article: dict) -> bool:
    """Whether a stored date came from a pre-integrity guessing path.

    Old releases labelled every discovery date as ``listing`` — even RSS. A
    feed channel is therefore exempt and is migrated to ``feed`` on reingest.
    A real HTML listing or heuristic date is revalidated from the detail page;
    if no strict signal exists, unknown is preferable to a plausible bad date.
    """
    source = _json_object(article.get("provenance_json")).get("published_at")
    channel = article.get("channel")
    if channel == "feed":
        return False
    if source:
        return source == "heuristic" or source == "listing"
    listing_raw = _json_object(article.get("raw_json"))
    return bool(
        article.get("published_at")
        and channel == "listing"
        and listing_raw.get("mode") != "configured"
    )


@dataclass
class DiscoverStats:
    target: str
    seen: int = 0
    new: int = 0
    updated: int = 0
    # Known articles still awaiting a body — usually the overflow the enqueue cap
    # left behind on an earlier pass. Counted separately from `new` because they
    # are backlog, not news, and are queued at a priority that says so.
    pending: int = 0
    queued: int = 0
    skipped_old: int = 0


def _decide(existing: dict | None, listed: ListedArticle) -> str | None:
    """What this sighting means: ``'new'``, ``'changed'``, ``'pending'`` or None.

    Keyed on the listing fingerprint, never on a CMS id — these sites renumber
    nodes on migration, and the URL plus its stated metadata is what actually
    identifies an article.

    - ``new``     — never seen before.
    - ``changed`` — the fingerprint moved, so the listing says something differs.
    - ``pending`` — known, but no body was ever stored. Either the enqueue cap
      left it behind or an ingest died. It still needs fetching, but it is
      backlog rather than news, so the caller queues it at backfill priority.
      Without this distinction every pass re-reports the whole backlog as "new".
    - ``None``    — nothing to do; only ``checked_at`` moves.

    A ``gone`` article that reappears counts as changed, so a site that 404s
    during a migration recovers by itself instead of staying tombstoned.
    """
    if existing is None:
        return "new"
    if existing["status"] == "gone":
        return "changed"
    if existing["listing_hash"] != listed.fingerprint():
        return "changed"
    if existing["status"] == "listed" and existing["detail_hash"] is None:
        return "pending"
    return None


def discover_target(conn, target: Target, http: HttpClient,
                    settings: Settings) -> DiscoverStats:
    """Resolve the target's channel, list its articles, file the work.

    Enqueues are capped by ``discover_enqueue_cap``: a sitemap exposing a decade
    of archive under ``/nyheder/`` would otherwise flood the queue on first
    contact. Newest-first, so the cap keeps what matters, and the overflow is
    logged rather than silently dropped — it stays ``listed`` in the DB and an
    explicit backfill can pick it up.
    """
    stats = DiscoverStats(target=target.key)
    # Reuse the last resolution rather than re-probing. A fresh `auto` resolution
    # costs up to a dozen requests (feed autodiscovery, then eight conventional
    # feed paths, then sitemap probes); repeating that for 98 kommuner on every
    # pass is slow, wasteful and impolite — and through the scrape.do proxy it is
    # slow enough to time a discovery pass out entirely.
    source = make_source(target, http,
                         resolved=repo.stored_resolution(conn, target.key))
    repo.upsert_municipality(
        conn, target, channel=source.channel, channel_detail=source.detail,
        channel_config=json.dumps(source.resolved_config, ensure_ascii=False),
    )
    conn.commit()

    run_id = repo.start_crawl_run(conn, target.key, source.channel)
    try:
        listed_articles = source.list_articles()
    except Exception as exc:
        repo.touch_municipality(conn, target.key, ok=False)
        conn.commit()
        repo.finish_crawl_run(conn, run_id, stats, error=f"{type(exc).__name__}: {exc}")
        raise

    floor = settings.min_published_floor
    # Newest first so the enqueue cap keeps the newest, not an arbitrary slice.
    # Undated articles sort as newest: on a sitemap-only site nothing has a date
    # yet, and treating them as oldest would mean never ingesting any of them.
    ordered = sorted(
        listed_articles,
        key=lambda a: (a.published_at or a.updated_at or "9999"),
        reverse=True,
    )

    fresh: list[tuple[str, str]] = []      # new/changed → fast lane
    backlog: list[str] = []                # known but bodyless → paced lane
    writes_since_commit = 0
    for listed in ordered:
        stats.seen += 1
        if below_floor(listed.published_at, floor):
            stats.skipped_old += 1
            continue
        kind = classify_kind(
            listed.url, [], listed_kind=listed.kind, press_url=target.press_url
        )
        row = listed.as_row(municipality_key=target.key)
        row["kind"] = kind
        existing = repo.get_article(conn, target.key, listed.id)
        decision = _decide(existing, listed)
        repo.upsert_listed_article(conn, row)
        repo.upsert_article_source(
            conn,
            municipality_key=target.key,
            article_id=listed.id,
            source_type="website",
            external_id=row["canonical_url"],
            source_url=listed.url,
            title=listed.title,
            received_at=repo.now_iso(),
            metadata={"channel": listed.channel, "listing": listed.raw},
        )
        writes_since_commit += 1
        if decision == "new":
            stats.new += 1
            fresh.append((listed.id, "new"))
        elif decision == "changed":
            stats.updated += 1
            fresh.append((listed.id, "changed"))
        elif decision == "pending":
            stats.pending += 1
            backlog.append(listed.id)
        if writes_since_commit >= _DISCOVERY_WRITE_BATCH:
            conn.commit()
            writes_since_commit = 0

    # Close the article-write transaction before queue insertion. Apart from
    # keeping Bunny's transaction window short, this means enqueueing a large
    # first crawl never holds all article rows in the same remote transaction.
    if writes_since_commit:
        conn.commit()

    # Fresh work first and always: a press release published minutes ago must not
    # queue behind a week-old backlog item.
    cap = settings.discover_enqueue_cap
    if len(fresh) > cap:
        logger.warning(
            "%s: %d new/changed articles, enqueueing the %d newest (cap "
            "NBK_DISCOVER_ENQUEUE_CAP=%d); the rest stay 'listed' and are picked "
            "up by later passes or `nbk backfill`",
            target.key, len(fresh), cap, cap)
    for article_id, reason in fresh[:cap]:
        if repo.enqueue_ingest(conn, target.key, article_id, reason) is not None:
            stats.queued += 1

    # Whatever cap is left drains the backlog, at backfill priority so it runs in
    # the paced lane and cannot starve fresh news. This is what makes the cap
    # self-healing: the overflow leaks out over subsequent passes on its own,
    # instead of sitting there until someone remembers to run a backfill.
    remaining = max(cap - len(fresh), 0)
    if backlog and not remaining:
        logger.info("%s: %d article(s) still awaiting a body; no cap left this "
                    "pass", target.key, len(backlog))
    for article_id in backlog[:remaining]:
        if repo.enqueue_ingest(conn, target.key, article_id, "backfill") is not None:
            stats.queued += 1

    repo.touch_municipality(conn, target.key, ok=True)
    conn.commit()
    repo.finish_crawl_run(conn, run_id, stats)
    logger.info("%s [%s]: seen=%d new=%d changed=%d pending=%d queued=%d "
                "skipped_old=%d", target.key, source.channel, stats.seen,
                stats.new, stats.updated, stats.pending, stats.queued,
                stats.skipped_old)
    return stats


def ingest_article(conn, target: Target, http: HttpClient, article_id: str,
                   settings: Settings) -> bool:
    """Fetch and store one article. Returns True if the content changed.

    A 404/410 tombstones the article rather than failing the task: the site
    telling us an article is gone is a successful answer, and retrying it four
    more times would only burn the attempt budget. A robots.txt denial settles
    the same way — the answer will not change on a retry.
    """
    article = repo.get_article(conn, target.key, article_id)
    if article is None:
        raise ValueError(f"{target.key}: no article row for id {article_id!r}")

    url = article["url"]
    try:
        html, final_url = http.get_text(url)
    except PermanentHttpError as exc:
        if exc.status_code in (404, 410):
            repo.mark_article_gone(conn, target.key, article_id)
            conn.commit()
            logger.info("%s: %s is gone (HTTP %d) — tombstoned",
                        target.key, url, exc.status_code)
            return False
        raise
    except RobotsDenied:
        repo.touch_article_checked(conn, target.key, article_id)
        conn.commit()
        logger.info("%s: robots.txt disallows %s — leaving it listed", target.key, url)
        return False

    provenance = _json_object(article.get("provenance_json"))
    listing_raw = _json_object(article.get("raw_json"))
    published_source = provenance.get("published_at")
    # New rows still contain the actual discovery record. On later rechecks,
    # pass a stored date back as discovery evidence only when its channel is
    # independently trustworthy; detail-page JSON-LD/meta/rules are re-read.
    use_discovery_date = (
        article["channel"] == "feed"
        or published_source == "listing:configured"
        or (article["status"] == "listed"
            and listing_raw.get("mode") == "configured")
    )
    listed = ListedArticle(
        url=url,
        title=article["title"],
        summary=article["summary"],
        published_at=(article["published_at"].isoformat()
                      if article["published_at"] and use_discovery_date else None),
        updated_at=article["updated_at"].isoformat() if article["updated_at"] else None,
        kind=article["kind"] or "ukendt",
        channel=article["channel"] or "listing",
        raw=listing_raw if article["status"] == "listed" else {},
    )
    detail = extract_article(
        html, final_url, listed=listed,
        body_selector=target.config.get("body_selector"),
        published_date_rules=target.published_date_rules,
        min_body_chars=settings.min_body_chars,
    )
    # The article's own categories can reclassify it — on a `faelles` site the
    # section is often the only thing distinguishing a press release from a news
    # item, and the listing could not see it.
    kind = classify_kind(final_url, detail.categories,
                         listed_kind=article["kind"] or "ukendt",
                         press_url=target.press_url)

    body_len = len(detail.body_text or "")
    thin = body_len < settings.min_body_chars
    if thin:
        # Not an error: stored and flagged, so `nbk stats` can show which sites
        # need a real selector instead of this failing invisibly.
        logger.warning("%s: thin extraction for %s (%d chars, body via %s)",
                       target.key, final_url, body_len,
                       detail.provenance.get("body_text", "nothing"))

    row = detail.as_row(municipality_key=target.key)
    # Pin the row id to the one discovery created. `ArticleDetail.id` is derived
    # from the URL the response *came from*, which differs whenever the site
    # redirects /nyheder/123 to a slug — and an UPDATE keyed on that derived id
    # would match no row and silently store nothing while reporting success.
    # The redirected URL is still recorded, as `url` and `canonical_url`.
    row["id"] = article_id
    changed = repo.save_article_detail(
        conn, row, thin=thin,
        clear_published_at=_legacy_published_is_untrusted(article),
    )
    repo.set_article_kind(conn, target.key, article_id, kind)
    repo.upsert_article_source(
        conn,
        municipality_key=target.key,
        article_id=article_id,
        source_type="website",
        external_id=article["canonical_url"] or article["url"],
        source_url=final_url,
        title=detail.title,
        body_text=detail.body_text,
        body_html=detail.body_html,
        received_at=repo.now_iso(),
        metadata={"provenance": detail.provenance},
    )
    conn.commit()

    if changed:
        logger.info("%s: ingested %s (%d words, published=%s)",
                    target.key, final_url, detail.word_count, detail.published_at)
    return changed
