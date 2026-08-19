from __future__ import annotations

from types import SimpleNamespace

from fastapi.testclient import TestClient

from nbkommune import db
from nbkommune import repositories as repo
from nbkommune.api import create_app
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


def _settings(tmp_path) -> Settings:
    return Settings(
        _env_file=None,
        BUNNY_DATABASE_URL=f"file:{tmp_path / 'status.db'}",
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
    }

    assert client.get("/api/articles", params={"kind": "invalid"}).status_code == 422
