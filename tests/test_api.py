from __future__ import annotations

from types import SimpleNamespace

from fastapi.testclient import TestClient

from nbkommune import db
from nbkommune import repositories as repo
from nbkommune.api import create_app
from nbkommune.gmail import ParsedEmail
from nbkommune.settings import Settings


def _target() -> SimpleNamespace:
    return SimpleNamespace(
        key="test",
        name="Test Kommune",
        site_url="https://example.test",
        news_url="https://example.test/news",
        press_url=None,
        source_type="news",
        channel="listing",
        enabled=True,
        note=None,
    )


def _settings(tmp_path, **overrides) -> Settings:
    values = {
        "auth_enabled": False,
        "BUNNY_DATABASE_URL": f"file:{tmp_path / 'status.db'}",
    }
    values.update(overrides)
    return Settings(
        _env_file=None,
        **values,
    )


def _seed(settings: Settings) -> None:
    conn = db.connect(settings)
    db.init_schema(conn, settings)
    repo.upsert_municipality(conn, _target(), channel="listing")
    repo.upsert_listed_article(conn, {
        "municipality_key": "test",
        "id": "article-1",
        "url": "https://example.test/news/one",
        "canonical_url": "https://example.test/news/one",
        "title": "En nyhed",
        "summary": None,
        "published_at": "2026-08-19T08:00:00+00:00",
        "updated_at": None,
        "kind": "nyhed",
        "channel": "listing",
        "listing_hash": "hash",
        "raw_json": "{}",
    })
    conn.execute(
        "UPDATE article SET summary = %s, body_text = %s WHERE municipality_key = %s AND id = %s",
        ("Plan for grøn mobilitet", "Kommunen anlægger en ny cykelsti gennem byen.",
         "test", "article-1"),
    )
    repo.enqueue_ingest(conn, "test", "article-1", "new")
    repo.upsert_listed_article(conn, {
        "municipality_key": "test",
        "id": "article-2",
        "url": "https://example.test/press/two",
        "canonical_url": "https://example.test/press/two",
        "title": "En pressemeddelelse",
        "summary": None,
        "published_at": "2026-08-18T08:00:00Z",
        "updated_at": None,
        "kind": "pressemeddelelse",
        "channel": "listing",
        "listing_hash": "hash-2",
        "raw_json": "{}",
    })
    conn.commit()
    conn.close()


def test_health_and_dashboard_shell_are_public(tmp_path):
    settings = _settings(tmp_path)
    client = TestClient(create_app(settings))

    assert client.get("/healthz").json() == {"status": "ok"}
    page = client.get("/")
    assert page.status_code == 200
    assert "Kommune scraper" in page.text
    for navigation_item in (
        "Oversigt", "Scraperdrift", "Artikelsøgning", "E-mailhistorik", "Gmail-opsætning"
    ):
        assert navigation_item in page.text
    for email_status in ("review", "ingested", "ignored", "all"):
        assert f'data-email-status="{email_status}"' in page.text
    assert "NBK_DASHBOARD_TOKEN" not in page.text


def test_status_api_is_public_and_returns_snapshot(tmp_path):
    settings = _settings(tmp_path)
    _seed(settings)
    client = TestClient(create_app(settings))

    response = client.get("/api/status")
    assert response.status_code == 200
    payload = response.json()
    assert payload["state"] == "healthy"
    assert payload["totals"] == {
        "articles": 2,
        "ingested": 0,
        "listed": 2,
        "gone": 0,
        "thin": 0,
        "undated": 0,
        "municipalities": 1,
    }
    assert payload["queue"]["by_status"]["queued"] == 1
    assert payload["next_tasks"][0]["municipality_key"] == "test"
    assert payload["municipalities"][0]["channel"] == "listing"


def test_articles_api_is_public_filterable_and_paginated(tmp_path):
    settings = _settings(tmp_path)
    _seed(settings)
    client = TestClient(create_app(settings))

    response = client.get(
        "/api/articles",
        params={"kind": "pressemeddelelse", "status": "listed", "limit": 1},
    )
    assert response.status_code == 200
    payload = response.json()
    assert payload["total"] == 1
    assert payload["limit"] == 1
    assert payload["offset"] == 0
    assert payload["items"][0] == {
        "municipality_key": "test",
        "municipality_name": "Test Kommune",
        "id": "article-2",
        "url": "https://example.test/press/two",
        "title": "En pressemeddelelse",
        "kind": "pressemeddelelse",
        "status": "listed",
        "published_at": "2026-08-18T08:00:00Z",
        "word_count": None,
        "thin": 0,
        "ingested_at": None,
        "sources": "website",
    }

    assert client.get("/api/articles", params={"kind": "invalid"}).status_code == 422


def test_articles_api_accepts_only_valid_service_bearer_or_gateway_session(tmp_path):
    token = "service-token-" + "x" * 32
    settings = _settings(tmp_path, auth_enabled=True, api_token=token)
    _seed(settings)
    client = TestClient(create_app(settings))

    missing = client.get("/api/articles")
    wrong = client.get(
        "/api/articles", headers={"Authorization": "Bearer wrong-token"}
    )
    service = client.get(
        "/api/articles", headers={"Authorization": f"Bearer {token}"}
    )
    dashboard = client.get(
        "/api/articles", headers={"X-NBK-User-Email": "admin@example.test"}
    )

    assert missing.status_code == 401
    assert missing.headers["www-authenticate"] == "Bearer"
    assert wrong.status_code == 401
    assert service.status_code == 200
    assert service.json()["total"] == 2
    assert dashboard.status_code == 200


def test_articles_api_full_text_searches_body_and_combines_filters(tmp_path):
    settings = _settings(tmp_path)
    _seed(settings)
    client = TestClient(create_app(settings))

    response = client.get("/api/articles", params={"q": "cykel", "kind": "nyhed"})

    assert response.status_code == 200
    payload = response.json()
    assert payload["total"] == 1
    assert payload["items"][0]["id"] == "article-1"
    assert "cykelsti" in payload["items"][0]["excerpt"]


def test_articles_api_search_treats_fts_syntax_as_plain_text(tmp_path):
    settings = _settings(tmp_path)
    _seed(settings)
    client = TestClient(create_app(settings))

    response = client.get("/api/articles", params={"q": 'cykel OR "unterminated'})

    assert response.status_code == 200
    assert response.json()["total"] == 0


def test_article_source_filter_and_admin_email_assignment(tmp_path):
    settings = _settings(tmp_path)
    _seed(settings)
    conn = db.connect(settings)
    message = ParsedEmail(
        gmail_message_id="review-1",
        gmail_thread_id="thread-1",
        sender_name="Ukendt afsender",
        sender_email="unknown@example.org",
        subject="Pressemeddelelse: Test",
        sent_at="2026-08-19T08:00:00+00:00",
        received_at="2026-08-19T08:01:00+00:00",
        body_text="En pressemeddelelse som skal tildeles.",
        body_html="<p>En pressemeddelelse som skal tildeles.</p>",
        links=[],
        raw={},
    )
    repo.insert_email_message(conn, message.as_row())
    repo.set_email_decision(
        conn, "review-1", municipality_key=None,
        classification="press_release", confidence=0.6, source="ai",
        reason="insufficient evidence", sender_scope="unknown", status="review",
    )
    conn.commit()
    conn.close()
    client = TestClient(create_app(settings))

    website = client.get("/api/articles", params={"source": "website"})
    email = client.get("/api/articles", params={"source": "email"})
    review = client.get("/api/admin/emails")

    assert website.json()["total"] == 2
    assert email.json()["total"] == 0
    assert review.json()["items"][0]["gmail_message_id"] == "review-1"
    assert client.post(
        "/api/admin/emails/review-1/assign",
        json={"municipality_key": "koege", "remember_sender": True},
    ).status_code == 403

    assigned = client.post(
        "/api/admin/emails/review-1/assign",
        headers={"X-NBK-Admin-Action": "1", "X-NBK-User-Email": "admin@example.test"},
        json={"municipality_key": "koege", "remember_sender": True},
    )

    assert assigned.status_code == 200
    assert assigned.json()["status"] == "ingested"
    assert client.get("/api/articles", params={"source": "email"}).json()["total"] == 1

    ingested = client.get("/api/admin/emails", params={"status": "ingested"})
    history = client.get("/api/admin/emails", params={"status": "all"})
    assert ingested.json()["items"][0]["gmail_message_id"] == "review-1"
    assert ingested.json()["status_counts"] == {"ingested": 1}
    assert history.json()["total"] == 1

    ignored = client.post(
        "/api/admin/emails/review-1/ignore",
        headers={"X-NBK-Admin-Action": "1", "X-NBK-User-Email": "admin@example.test"},
        json={"remember_sender": False},
    )
    assert ignored.status_code == 200
    assert client.get("/api/articles", params={"source": "email"}).json()["total"] == 0
    ignored_list = client.get("/api/admin/emails", params={"status": "ignored"})
    assert ignored_list.json()["items"][0]["status"] == "ignored"

    reassigned = client.post(
        "/api/admin/emails/review-1/assign",
        headers={"X-NBK-Admin-Action": "1", "X-NBK-User-Email": "admin@example.test"},
        json={"municipality_key": "koege", "remember_sender": False},
    )
    assert reassigned.status_code == 200
    assert client.get("/api/articles", params={"source": "email"}).json()["total"] == 1

    conn = db.connect(settings)
    repo.upsert_article_source(
        conn,
        municipality_key="koege",
        article_id=reassigned.json()["article_id"],
        source_type="website",
        external_id="website-copy-1",
        source_url="https://koege.dk/news/test",
    )
    conn.commit()
    conn.close()
    assert client.post(
        "/api/admin/emails/review-1/ignore",
        headers={"X-NBK-Admin-Action": "1", "X-NBK-User-Email": "admin@example.test"},
        json={"remember_sender": False},
    ).status_code == 200
    assert client.get("/api/articles", params={"source": "email"}).json()["total"] == 0
    assert client.get("/api/articles", params={"source": "website"}).json()["total"] == 3
    assert client.get("/api/admin/emails", params={"status": "invalid"}).status_code == 422
