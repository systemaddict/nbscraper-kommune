from __future__ import annotations

import asyncio

import pytest
from fastapi.testclient import TestClient
from fastmcp import Client

from nbkommune import db
from nbkommune import repositories as repo
from nbkommune.mcp_server import _search_articles, build_mcp_server
from nbkommune.settings import Settings


def _settings(tmp_path, **overrides) -> Settings:
    values = {"BUNNY_DATABASE_URL": f"file:{tmp_path / 'mcp.db'}"}
    values.update(overrides)
    return Settings(_env_file=None, **values)


def _seed(settings: Settings) -> None:
    conn = db.connect(settings)
    db.init_schema(conn, settings)
    target = type(
        "Target",
        (),
        {
            "key": "test",
            "name": "Test Kommune",
            "site_url": "https://example.test",
            "news_url": "https://example.test/news",
            "press_url": None,
            "source_type": "news",
            "channel": "listing",
            "enabled": True,
            "note": None,
        },
    )()
    repo.upsert_municipality(conn, target, channel="listing")
    repo.upsert_listed_article(
        conn,
        {
            "municipality_key": "test",
            "id": "article-1",
            "url": "https://example.test/news/one",
            "canonical_url": "https://example.test/news/one",
            "title": "Ny cykelsti åbner",
            "summary": "Grøn mobilitet i kommunen",
            "published_at": "2026-08-19T08:00:00+00:00",
            "updated_at": None,
            "kind": "nyhed",
            "channel": "listing",
            "listing_hash": "hash",
            "raw_json": "{}",
        },
    )
    conn.execute(
        "UPDATE article SET body_text = %s, status = 'ingested' "
        "WHERE municipality_key = %s AND id = %s",
        ("Kommunen anlægger en cykelsti gennem byen.", "test", "article-1"),
    )
    conn.commit()
    conn.close()


def test_search_articles_uses_existing_full_text_search(tmp_path):
    settings = _settings(tmp_path)
    _seed(settings)

    result = _search_articles(settings, query="cykelsti", status="ingested", limit=200)

    assert result["total"] == 1
    assert result["limit"] == 100
    assert result["offset"] == 0
    assert result["items"][0]["id"] == "article-1"
    assert result["items"][0]["municipality_name"] == "Test Kommune"
    assert "cykelsti" in result["items"][0]["excerpt"].casefold()
    assert result["items"][0]["url"] == "https://example.test/news/one"


def test_search_articles_validates_query_and_bounds_pagination(tmp_path):
    settings = _settings(tmp_path)
    _seed(settings)

    result = _search_articles(settings, limit=0, offset=-4)
    assert result["limit"] == 1
    assert result["offset"] == 0

    with pytest.raises(ValueError, match="at most 200"):
        _search_articles(settings, query="x" * 201)


def test_http_mcp_fails_closed_without_auth_tokens(tmp_path):
    with pytest.raises(ValueError, match="NBK_MCP_AUTH_TOKENS"):
        build_mcp_server(auth=True, settings=_settings(tmp_path))


def test_stdio_mcp_does_not_require_auth_tokens(tmp_path):
    server = build_mcp_server(auth=False, settings=_settings(tmp_path))

    assert server.name == "nb-kommune"


def test_mcp_protocol_exposes_read_only_search_tool(tmp_path):
    settings = _settings(tmp_path)
    _seed(settings)
    server = build_mcp_server(auth=False, settings=settings)

    async def call_tool():
        tools = await server.list_tools()
        assert [tool.name for tool in tools] == ["search_articles"]
        assert tools[0].annotations.readOnlyHint is True
        async with Client(server) as client:
            return await client.call_tool(
                "search_articles", {"query": "cykelsti", "status": "ingested"}
            )

    result = asyncio.run(call_tool())
    assert result.is_error is False
    assert result.structured_content["total"] == 1
    assert result.structured_content["items"][0]["id"] == "article-1"


def test_http_transport_rejects_missing_and_wrong_bearer_tokens(tmp_path):
    token = "mcp-client-token-" + "x" * 32
    server = build_mcp_server(
        auth=True,
        settings=_settings(tmp_path, mcp_auth_tokens=[token]),
    )
    payload = {
        "jsonrpc": "2.0",
        "id": 1,
        "method": "initialize",
        "params": {
            "protocolVersion": "2025-06-18",
            "capabilities": {},
            "clientInfo": {"name": "test", "version": "1"},
        },
    }
    headers = {
        "accept": "application/json, text/event-stream",
        "content-type": "application/json",
    }

    with TestClient(server.http_app()) as client:
        assert client.post("/mcp", headers=headers, json=payload).status_code == 401
        assert client.post(
            "/mcp",
            headers={**headers, "authorization": "Bearer wrong"},
            json=payload,
        ).status_code == 401
        accepted = client.post(
            "/mcp",
            headers={**headers, "authorization": f"Bearer {token}"},
            json=payload,
        )

    assert accepted.status_code == 200
    assert '"protocolVersion":"2025-06-18"' in accepted.text
