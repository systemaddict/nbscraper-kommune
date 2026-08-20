"""End-to-end Gmail collection, routing, and article promotion."""
from __future__ import annotations

import json
import logging
import re
from dataclasses import dataclass
from datetime import datetime
from urllib.parse import urlsplit

from nbkommune import repositories as repo
from nbkommune.email_classifier import (
    EmailDecision,
    OpenRouterClassifier,
    deterministic_decision,
    remember_decision,
)
from nbkommune.gmail import GmailClient, ParsedEmail, parse_gmail_message
from nbkommune.records import ArticleDetail, ListedArticle, normalise_url
from nbkommune.settings import Settings
from nbkommune.targets import Target, registry

logger = logging.getLogger(__name__)


@dataclass
class EmailCollectStats:
    seen: int = 0
    duplicates: int = 0
    ingested: int = 0
    ignored: int = 0
    review: int = 0
    failed: int = 0


def _text_date(value) -> str | None:
    if isinstance(value, datetime):
        return value.isoformat()
    return str(value) if value else None


def _parsed_from_row(row: dict) -> ParsedEmail:
    return ParsedEmail(
        gmail_message_id=row["gmail_message_id"],
        gmail_thread_id=row["gmail_thread_id"],
        sender_name=row["sender_name"] or "",
        sender_email=row["sender_email"],
        subject=row["subject"] or "",
        sent_at=_text_date(row["sent_at"]),
        received_at=_text_date(row["received_at"]) or repo.now_iso(),
        body_text=row["body_text"] or "",
        body_html=row["body_html"],
        links=json.loads(row["links_json"] or "[]"),
        raw=json.loads(row["raw_json"] or "{}"),
    )


def _clean_subject(subject: str) -> str:
    subject = re.sub(r"^(?:(?:re|fw|fwd)\s*:\s*)+", "", subject, flags=re.I)
    return re.sub(r"\s+", " ", subject).strip() or "Uden emne"


def _target_link(message: ParsedEmail, target: Target,
                 preferred: str | None) -> str | None:
    if preferred in message.links:
        return preferred
    site_host = (urlsplit(target.site_url).hostname or "").casefold().removeprefix("www.")
    for link in message.links:
        host = (urlsplit(link).hostname or "").casefold().removeprefix("www.")
        if site_host and host == site_host:
            return link
    # Third-party press rooms share a host. Require the configured path prefix,
    # rather than assigning every via.ritzau.dk link to the first municipality.
    for base in (target.news_url, target.press_url):
        if not base:
            continue
        prefix = normalise_url(base).rstrip("/")
        for link in message.links:
            if normalise_url(link).startswith(prefix):
                return link
    return None


def _promote(conn, settings: Settings, message: ParsedEmail,
             target: Target, decision: EmailDecision) -> str:
    """Create or enrich a canonical article and retain the email rendition."""
    public_url = _target_link(message, target, decision.canonical_url)
    identity_url = public_url or f"email://gmail/{message.gmail_message_id}"
    kind = {
        "press_release": "pressemeddelelse",
        "correction": "pressemeddelelse",
        "news": "nyhed",
    }.get(decision.classification, "ukendt")
    listed = ListedArticle(
        url=identity_url,
        title=_clean_subject(message.subject),
        published_at=message.sent_at or message.received_at,
        kind=kind,
        channel="email",
        raw={"gmail_message_id": message.gmail_message_id},
    )
    existing = repo.get_article(conn, target.key, listed.id)
    if existing is None:
        repo.upsert_listed_article(conn, listed.as_row(municipality_key=target.key))

    # An existing website body remains canonical; the email body is always
    # retained below in article_source and can supplement it downstream.
    if existing is None or not existing.get("detail_hash"):
        detail = ArticleDetail(
            url=identity_url,
            title=listed.title,
            summary=None,
            body_text=message.body_text,
            body_html=message.body_html,
            published_at=listed.published_at,
            updated_at=None,
            image_url=None,
            author=message.sender_name or message.sender_email,
            categories=["email", decision.classification],
            lang="da",
            canonical_url=public_url or identity_url,
            provenance={
                "title": "email_subject",
                "body_text": "email",
                "published_at": "email_date",
            },
            raw={"gmail_message_id": message.gmail_message_id},
        )
        row = detail.as_row(municipality_key=target.key)
        row["id"] = listed.id
        repo.save_article_detail(
            conn, row, thin=len(message.body_text) < settings.min_body_chars
        )
        repo.set_article_kind(conn, target.key, listed.id, kind)

    repo.upsert_article_source(
        conn,
        municipality_key=target.key,
        article_id=listed.id,
        source_type="email",
        external_id=message.gmail_message_id,
        source_url=public_url,
        title=listed.title,
        body_text=message.body_text,
        body_html=message.body_html,
        received_at=message.received_at,
        metadata={
            "sender_name": message.sender_name,
            "sender_email": message.sender_email,
            "subject": message.subject,
            "classification": decision.classification,
            "classification_reason": decision.reason,
        },
    )
    return listed.id


def process_email(conn, settings: Settings, message: ParsedEmail,
                  *, classifier: OpenRouterClassifier | None = None) -> str:
    """Route and persist one parsed message. Returns its final status."""
    inserted = repo.insert_email_message(conn, message.as_row())
    conn.commit()
    if not inserted:
        existing_message = repo.get_email_message(conn, message.gmail_message_id)
        if existing_message and existing_message["status"] not in {"new", "error"}:
            return "duplicate"

    targets = list(registry(settings).values())
    decision = deterministic_decision(
        conn,
        sender_name=message.sender_name,
        sender_email=message.sender_email,
        subject=message.subject,
        targets=targets,
    )
    if decision is None:
        if classifier is None:
            raise RuntimeError("unknown sender requires an OpenRouter classifier")
        decision = classifier.classify(
            sender_name=message.sender_name,
            sender_email=message.sender_email,
            subject=message.subject,
            body_text=message.body_text,
            links=message.links,
            targets=targets,
        )

    threshold = settings.email_ai_confidence
    target = next(
        (item for item in targets if item.key == decision.municipality_key), None
    )
    if decision.municipality_key and target is None:
        raise ValueError(f"unknown municipality {decision.municipality_key!r}")
    if target is not None:
        # Sender and message decisions both reference municipality, including
        # ignored committee notices. Ensure the FK target exists before either
        # decision is persisted on a fresh database.
        repo.upsert_municipality(conn, target)
    remember_decision(conn, message.sender_email, decision, threshold=threshold)

    if decision.action == "ignore":
        repo.set_email_decision(
            conn, message.gmail_message_id,
            municipality_key=decision.municipality_key,
            classification=decision.classification,
            confidence=decision.confidence,
            source=decision.source,
            reason=decision.reason,
            sender_scope=decision.sender_scope,
            status="ignored",
        )
        conn.commit()
        return "ignored"

    if (decision.action != "ingest" or not decision.municipality_key
            or decision.confidence < threshold):
        repo.set_email_decision(
            conn, message.gmail_message_id,
            municipality_key=decision.municipality_key,
            classification=decision.classification,
            confidence=decision.confidence,
            source=decision.source,
            reason=decision.reason,
            sender_scope=decision.sender_scope,
            status="review",
        )
        conn.commit()
        return "review"

    assert target is not None  # guarded by the review branch above
    article_id = _promote(conn, settings, message, target, decision)
    repo.set_email_decision(
        conn, message.gmail_message_id,
        municipality_key=target.key,
        classification=decision.classification,
        confidence=decision.confidence,
        source=decision.source,
        reason=decision.reason,
        sender_scope=decision.sender_scope,
        status="ingested",
        article_id=article_id,
    )
    conn.commit()
    return "ingested"


def assign_email(conn, settings: Settings, gmail_message_id: str,
                 municipality_key: str, *, remember_sender: bool,
                 actor: str = "dashboard") -> str:
    """Resolve a review item from the protected dashboard and promote it."""
    row = repo.get_email_message(conn, gmail_message_id)
    if row is None:
        raise KeyError(gmail_message_id)
    if row["status"] not in {"new", "review", "error"}:
        raise ValueError(f"email is already {row['status']}")
    target = registry(settings).get(municipality_key)
    if target is None:
        raise ValueError(f"unknown municipality {municipality_key!r}")
    message = _parsed_from_row(row)
    classification = row["classification"] or "news"
    if classification in {"other", "noise"}:
        classification = (
            "press_release" if "pressemeddelelse" in message.subject.casefold()
            else "news"
        )
    decision = EmailDecision(
        municipality_key=municipality_key,
        classification=classification,
        action="ingest",
        confidence=1.0,
        sender_scope="fixed" if remember_sender else "unknown",
        reason=f"assigned by {actor}",
        source="manual",
    )
    repo.upsert_municipality(conn, target)
    if remember_sender:
        remember_decision(conn, message.sender_email, decision, threshold=1.0)
    article_id = _promote(conn, settings, message, target, decision)
    repo.set_email_decision(
        conn, gmail_message_id, municipality_key=municipality_key,
        classification=decision.classification, confidence=1.0,
        source="manual", reason=decision.reason, sender_scope=decision.sender_scope,
        status="ingested", article_id=article_id,
    )
    conn.commit()
    return article_id


def ignore_email(conn, gmail_message_id: str, *, remember_sender: bool,
                 actor: str = "dashboard") -> None:
    row = repo.get_email_message(conn, gmail_message_id)
    if row is None:
        raise KeyError(gmail_message_id)
    if row["status"] not in {"new", "review", "error"}:
        raise ValueError(f"email is already {row['status']}")
    reason = f"ignored by {actor}"
    if remember_sender:
        repo.upsert_sender_resolution(
            conn, sender_email=row["sender_email"], mode="ignore",
            municipality_key=None, confidence=1.0, reason=reason, source="manual",
        )
    repo.set_email_decision(
        conn, gmail_message_id, municipality_key=None, classification="noise",
        confidence=1.0, source="manual", reason=reason,
        sender_scope="fixed" if remember_sender else "unknown", status="ignored",
    )
    conn.commit()


def collect_gmail(conn, settings: Settings) -> EmailCollectStats:
    """Collect the current Gmail query window, processing only new/error rows."""
    stats = EmailCollectStats()
    errors: list[str] = []
    attempted = 0
    with GmailClient(settings) as gmail, OpenRouterClassifier(settings) as classifier:
        for message_id in gmail.iter_message_ids():
            stats.seen += 1
            existing = repo.get_email_message(conn, message_id)
            if existing and existing["status"] not in {"new", "error"}:
                stats.duplicates += 1
                continue
            if attempted >= settings.gmail_batch_size:
                break
            attempted += 1
            try:
                message = parse_gmail_message(gmail.get_message(message_id))
                outcome = process_email(conn, settings, message, classifier=classifier)
                if outcome == "duplicate":
                    stats.duplicates += 1
                else:
                    setattr(stats, outcome, getattr(stats, outcome) + 1)
            except Exception as exc:
                conn.rollback()
                stats.failed += 1
                errors.append(f"{message_id}: {type(exc).__name__}: {exc}")
                if repo.get_email_message(conn, message_id):
                    repo.set_email_decision(
                        conn, message_id, municipality_key=None,
                        classification="other", confidence=0.0, source="error",
                        reason=str(exc)[:1000], sender_scope="unknown", status="error",
                    )
                    conn.commit()
                logger.exception("could not process Gmail message %s", message_id)
    if errors:
        raise RuntimeError(f"{len(errors)} Gmail message(s) failed; {errors[0]}")
    return stats
