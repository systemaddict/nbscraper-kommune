from __future__ import annotations

import base64
import json
from dataclasses import replace
from datetime import UTC, datetime

import httpx

from nbkommune import db
from nbkommune import repositories as repo
from nbkommune.email_classifier import (
    EmailDecision,
    OpenRouterClassifier,
    deterministic_decision,
)
from nbkommune.email_ingest import process_email
from nbkommune.gmail import GmailClient, ParsedEmail, parse_gmail_message
from nbkommune.records import ArticleDetail, ListedArticle
from nbkommune.settings import Settings
from nbkommune.targets import registry


def _b64(value: str) -> str:
    return base64.urlsafe_b64encode(value.encode()).decode().rstrip("=")


def _settings(**overrides) -> Settings:
    return Settings(
        _env_file=None,
        auth_enabled=False,
        BUNNY_DATABASE_URL="file::memory:",
        openrouter_api_key="test-key",
        **overrides,
    )


def _message(message_id: str, sender: str, subject: str, *,
             sender_name: str = "", body: str = "Kommunal nyhed", links=None) -> ParsedEmail:
    return ParsedEmail(
        gmail_message_id=message_id,
        gmail_thread_id=f"thread-{message_id}",
        sender_name=sender_name,
        sender_email=sender,
        subject=subject,
        sent_at="2026-08-19T08:00:00+00:00",
        received_at="2026-08-19T08:01:00+00:00",
        body_text=body,
        body_html=f"<p>{body}</p>",
        links=list(links or []),
        raw={},
    )


def test_gmail_parser_prefers_plain_text_extracts_links_and_drops_reply():
    data = {
        "id": "gmail-1",
        "threadId": "thread-1",
        "internalDate": "1787126460000",
        "labelIds": ["INBOX"],
        "payload": {
            "mimeType": "multipart/alternative",
            "headers": [
                {"name": "From", "value": "Margit Kjellquist <margit.kjellquist@koege.dk>"},
                {"name": "Subject", "value": "Pressemeddelelse: En ny forestilling"},
                {"name": "Date", "value": "Wed, 19 Aug 2026 10:00:00 +0200"},
            ],
            "parts": [
                {
                    "mimeType": "text/plain",
                    "headers": [{"name": "Content-Type", "value": "text/plain; charset=utf-8"}],
                    "body": {"data": _b64(
                        "Selve pressemeddelelsen\nhttps://www.koege.dk/nyhed\n"
                        "On Tuesday someone wrote:\n> gammel tekst"
                    )},
                },
                {
                    "mimeType": "text/html",
                    "body": {"data": _b64(
                        '<p>Selve pressemeddelelsen</p><a href="https://www.koege.dk/nyhed">Læs</a>'
                    )},
                },
            ],
        },
    }

    parsed = parse_gmail_message(data)

    assert parsed.sender_name == "Margit Kjellquist"
    assert parsed.sender_email == "margit.kjellquist@koege.dk"
    assert parsed.sent_at == "2026-08-19T08:00:00+00:00"
    assert "gammel tekst" not in parsed.body_text
    assert parsed.links == ["https://www.koege.dk/nyhed"]


def test_gmail_parser_decodes_rfc2047_sender_and_subject_headers():
    data = {
        "id": "gmail-encoded",
        "internalDate": "1787126460000",
        "payload": {
            "mimeType": "text/plain",
            "headers": [
                {
                    "name": "From",
                    "value": "=?utf-8?Q?K=C3=B8ge_Kommune?= <presse@koege.dk>",
                },
                {
                    "name": "Subject",
                    "value": "=?utf-8?Q?Pressemeddelelse_fra_K=C3=B8ge?=",
                },
            ],
            "body": {"data": _b64("Indhold")},
        },
    }

    parsed = parse_gmail_message(data)

    assert parsed.sender_name == "Køge Kommune"
    assert parsed.subject == "Pressemeddelelse fra Køge"


def test_gmail_parser_uses_original_sender_for_mailing_lists():
    data = {
        "id": "gmail-list",
        "internalDate": "1787126460000",
        "payload": {
            "mimeType": "text/plain",
            "headers": [
                {
                    "name": "From",
                    "value": (
                        '"\'Lotte Holle Schneider\' via Pressemeddelelser fra kommuner" '
                        "<pmkom@nb-medier.dk>"
                    ),
                },
                {"name": "X-Original-From", "value": (
                    "Lotte Holle Schneider <lotte.schneider@koege.dk>"
                )},
                {"name": "Reply-To", "value": (
                    "Lotte Holle Schneider <lotte.schneider@koege.dk>"
                )},
                {"name": "List-ID", "value": "<pmkom.nb-medier.dk>"},
            ],
            "body": {"data": _b64("Indhold")},
        },
    }

    parsed = parse_gmail_message(data)

    assert parsed.sender_name == "Lotte Holle Schneider"
    assert parsed.sender_email == "lotte.schneider@koege.dk"
    assert parsed.raw["envelope_sender"] == "pmkom@nb-medier.dk"
    assert parsed.raw["list_id"] == "<pmkom.nb-medier.dk>"


def test_gmail_parser_does_not_replace_ordinary_sender_with_reply_to():
    data = {
        "id": "gmail-reply-to",
        "internalDate": "1787126460000",
        "payload": {
            "mimeType": "text/plain",
            "headers": [
                {"name": "From", "value": "Nyhedsbrev <news@example.com>"},
                {"name": "Reply-To", "value": "Support <support@example.com>"},
            ],
            "body": {"data": _b64("Indhold")},
        },
    }

    parsed = parse_gmail_message(data)

    assert parsed.sender_email == "news@example.com"
    assert parsed.raw["envelope_sender"] is None


def test_gmail_client_refreshes_token_and_paginates_scan_window():
    requests: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        if request.url.host == "oauth2.googleapis.com":
            return httpx.Response(200, json={"access_token": "access"})
        assert request.headers["authorization"] == "Bearer access"
        if "pageToken=next" in str(request.url):
            return httpx.Response(200, json={"messages": [{"id": "two"}]})
        return httpx.Response(200, json={
            "messages": [{"id": "one"}], "nextPageToken": "next",
        })

    settings = _settings(
        gmail_client_id="client", gmail_client_secret="secret",
        gmail_refresh_token="refresh", gmail_scan_limit=10,
    )
    with GmailClient(settings, transport=httpx.MockTransport(handler)) as client:
        ids = list(client.iter_message_ids())

    assert ids == ["one", "two"]
    assert len([request for request in requests if request.url.host == "oauth2.googleapis.com"]) == 1


def test_deterministic_resolution_uses_official_domain_and_display_name_alias():
    settings = _settings()
    conn = db.connect(settings)
    db.init_schema(conn, settings)
    targets = list(registry(settings).values())

    koege = deterministic_decision(
        conn, sender_name="Margit Kjellquist",
        sender_email="margit.kjellquist@koege.dk",
        subject="Pressemeddelelse: Teater", targets=targets,
    )
    egedal = deterministic_decision(
        conn, sender_name="Egedal Kommune", sender_email="no-reply@egekom.dk",
        subject="Klovneløb i Smørum", targets=targets,
    )

    assert koege and (koege.municipality_key, koege.classification) == (
        "koege", "press_release"
    )
    assert egedal and egedal.municipality_key == "egedal"


def test_openrouter_classifier_enforces_canonical_keys_and_source_link():
    def handler(request: httpx.Request) -> httpx.Response:
        body = json.loads(request.content)
        assert body["response_format"]["type"] == "json_schema"
        return httpx.Response(200, json={
            "choices": [{"message": {"content": json.dumps({
                "municipality_key": "gribskov",
                "classification": "committee_notice",
                "action": "ignore",
                "confidence": 0.98,
                "sender_scope": "shared",
                "reason": "The subject names Gribskov Kommune",
                "canonical_url": None,
            })}}],
        })

    settings = _settings()
    with OpenRouterClassifier(settings, transport=httpx.MockTransport(handler)) as classifier:
        decision = classifier.classify(
            sender_name="FirstAgenda", sender_email="no-reply@firstagenda.com",
            subject="Nyt fra udvalg i Gribskov Kommune", body_text="",
            links=[], targets=list(registry(settings).values()),
        )

    assert decision.municipality_key == "gribskov"
    assert decision.sender_scope == "shared"
    assert decision.action == "ignore"


def test_known_sender_is_cached_and_second_message_needs_no_ai():
    settings = _settings()
    conn = db.connect(settings)
    db.init_schema(conn, settings)

    first = process_email(
        conn, settings,
        _message("one", "margit.kjellquist@koege.dk", "Pressemeddelelse: Første"),
    )
    second = process_email(
        conn, settings,
        _message("two", "margit.kjellquist@koege.dk", "Pressemeddelelse: Anden"),
    )

    assert first == second == "ingested"
    assert repo.get_sender_resolution(conn, "margit.kjellquist@koege.dk")["mode"] == "fixed"
    assert repo.count_email_messages(conn, status="ingested") == 2


def test_ignored_committee_message_does_not_poison_fixed_sender_mapping():
    settings = _settings()
    conn = db.connect(settings)
    db.init_schema(conn, settings)
    sender = "margit.kjellquist@koege.dk"

    assert process_email(
        conn, settings, _message("press-1", sender, "Pressemeddelelse: Første")
    ) == "ingested"
    assert process_email(
        conn, settings, _message("committee", sender, "Nyt fra udvalg i Køge")
    ) == "ignored"
    assert process_email(
        conn, settings, _message("press-2", sender, "Pressemeddelelse: Anden")
    ) == "ingested"

    resolution = repo.get_sender_resolution(conn, sender)
    assert resolution["mode"] == "fixed"
    assert resolution["municipality_key"] == "koege"


def test_retry_refreshes_sender_metadata_before_reclassification():
    settings = _settings()
    conn = db.connect(settings)
    db.init_schema(conn, settings)
    stale = _message(
        "retry", "pmkom@nb-medier.dk", "Pressemeddelelse: Budget",
        sender_name="Lotte via Pressemeddelelser fra kommuner",
    )
    repo.insert_email_message(conn, stale.as_row())
    repo.set_email_decision(
        conn, stale.gmail_message_id, municipality_key=None,
        classification="noise", confidence=0.9, source="sender",
        reason="stale forwarding rule", sender_scope="fixed", status="error",
    )
    conn.commit()

    refreshed = replace(
        stale,
        sender_name="Lotte Holle Schneider",
        sender_email="lotte.schneider@koege.dk",
        raw={"envelope_sender": "pmkom@nb-medier.dk"},
    )
    assert process_email(conn, settings, refreshed) == "ingested"

    stored = repo.get_email_message(conn, stale.gmail_message_id)
    assert stored["sender_name"] == "Lotte Holle Schneider"
    assert stored["sender_email"] == "lotte.schneider@koege.dk"
    assert stored["status"] == "ingested"


def test_email_before_publication_floor_is_ignored_without_ai_or_sender_cache():
    settings = _settings(min_published_date="2026-01-01")
    conn = db.connect(settings)
    db.init_schema(conn, settings)
    message = replace(
        _message("old", "unknown@example.com", "En gammel pressemeddelelse"),
        sent_at="2025-12-31T23:59:59+00:00",
        received_at="2026-01-01T00:00:01+00:00",
    )

    outcome = process_email(conn, settings, message)

    stored = repo.get_email_message(conn, message.gmail_message_id)
    assert outcome == "ignored"
    assert stored["classification_source"] == "date_floor"
    assert repo.get_sender_resolution(conn, message.sender_email) is None
    assert repo.count_email_messages(conn, status="ignored") == 1


def test_shared_sender_is_classified_for_every_message():
    class Classifier:
        calls = 0

        def classify(self, **_kwargs):
            self.calls += 1
            return EmailDecision(
                "gribskov", "committee_notice", "ignore", 0.98,
                "shared", "subject names Gribskov", "ai",
            )

    settings = _settings()
    conn = db.connect(settings)
    db.init_schema(conn, settings)
    classifier = Classifier()

    process_email(conn, settings, _message(
        "firstagenda-1", "no-reply@firstagenda.com",
        "Nyt fra udvalg i Gribskov Kommune", sender_name="FirstAgenda",
    ), classifier=classifier)
    process_email(conn, settings, _message(
        "firstagenda-2", "no-reply@firstagenda.com",
        "Nyt fra udvalg i Helsingør Kommune", sender_name="FirstAgenda",
    ), classifier=classifier)

    assert classifier.calls == 2
    assert repo.get_sender_resolution(conn, "no-reply@firstagenda.com")["mode"] == "classify_each"


def test_matching_email_keeps_website_body_and_adds_source():
    settings = _settings()
    conn = db.connect(settings)
    db.init_schema(conn, settings)
    target = registry(settings)["koege"]
    repo.upsert_municipality(conn, target)
    listed = ListedArticle(
        url="https://www.koege.dk/nyhed", title="En nyhed", channel="listing"
    )
    repo.upsert_listed_article(conn, listed.as_row(municipality_key="koege"))
    detail = ArticleDetail(
        url=listed.url, title=listed.title, summary=None,
        body_text="Website-versionen", body_html="<p>Website-versionen</p>",
        published_at="2026-08-19T08:00:00+00:00", updated_at=None,
        image_url=None, author=None, canonical_url=listed.url,
    )
    repo.save_article_detail(conn, detail.as_row(municipality_key="koege"), thin=False)
    repo.upsert_article_source(
        conn, municipality_key="koege", article_id=listed.id,
        source_type="website", external_id=listed.url, source_url=listed.url,
        body_text="Website-versionen",
    )
    conn.commit()

    outcome = process_email(
        conn, settings,
        _message(
            "gmail-supplement", "margit.kjellquist@koege.dk", "En nyhed",
            body="E-mail-version med ekstra afsnit", links=[listed.url],
        ),
    )

    article = repo.get_article(conn, "koege", listed.id)
    sources = repo.article_sources(conn, "koege", listed.id)
    assert outcome == "ingested"
    assert article["body_text"] == "Website-versionen"
    assert {source["source_type"] for source in sources} == {"website", "email"}


def test_gmail_timestamp_fixture_is_currently_timezone_aware():
    # Guard the timestamp conversion used by the MIME fixture above.
    assert datetime.fromtimestamp(1787126460000 / 1000, UTC).tzinfo is UTC
