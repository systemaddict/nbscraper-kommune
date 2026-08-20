from __future__ import annotations

import asyncio
import time

import pytest
import respx
from authlib.jose import JsonWebKey, jwt
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import rsa
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


def test_http_mcp_fails_closed_without_oauth_config(tmp_path):
    with pytest.raises(ValueError, match="NBK_MCP_BASE_URL"):
        build_mcp_server(auth=True, settings=_settings(tmp_path))


def test_stdio_mcp_does_not_require_oauth_config(tmp_path):
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


def test_http_transport_discovers_oauth_and_validates_resource_token(tmp_path):
    issuer = "https://dashboard.example.com/api/auth"
    resource = "https://mcp.example.com/mcp"
    jwks_url = f"{issuer}/jwks"
    server = build_mcp_server(
        auth=True,
        settings=_settings(
            tmp_path,
            mcp_base_url="https://mcp.example.com",
            mcp_oauth_issuer=issuer,
            mcp_oauth_jwks_url=jwks_url,
        ),
    )
    private_key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    public_pem = private_key.public_key().public_bytes(
        serialization.Encoding.PEM,
        serialization.PublicFormat.SubjectPublicKeyInfo,
    )
    private_pem = private_key.private_bytes(
        serialization.Encoding.PEM,
        serialization.PrivateFormat.PKCS8,
        serialization.NoEncryption(),
    )
    public_jwk = JsonWebKey.import_key(public_pem).as_dict()
    public_jwk.update({"alg": "RS256", "kid": "test-key", "use": "sig"})
    now = int(time.time())
    token = jwt.encode(
        {"alg": "RS256", "kid": "test-key"},
        {
            "iss": issuer,
            "aud": resource,
            "sub": "test-user",
            "client_id": "test-client",
            "scope": "search:articles",
            "iat": now,
            "exp": now + 300,
        },
        private_pem,
    ).decode()
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

    with respx.mock(assert_all_called=True) as router, TestClient(server.http_app()) as client:
        router.get(jwks_url).respond(json={"keys": [public_jwk]})
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
