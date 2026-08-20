from __future__ import annotations

from urllib.parse import parse_qs, urlsplit

import httpx
import pytest
from fastapi.testclient import TestClient

from nbkommune import db
from nbkommune import repositories as repo
from nbkommune.api import create_app
from nbkommune.gmail_oauth import (
    GmailOAuthError,
    begin_oauth,
    complete_oauth,
    connection_status,
    decrypt_refresh_token,
    disconnect_gmail,
    encrypt_refresh_token,
    gmail_refresh_token,
)
from nbkommune.settings import Settings


def _settings(tmp_path=None, **overrides) -> Settings:
    database_url = "file::memory:" if tmp_path is None else f"file:{tmp_path / 'oauth.db'}"
    values = {
        "auth_enabled": False,
        "auth_base_url": "https://dashboard.example.test",
        "BUNNY_DATABASE_URL": database_url,
        "gmail_enabled": True,
        "gmail_client_id": "client-id",
        "gmail_client_secret": "client-secret",
        "gmail_token_encryption_key": "token-key-" + "x" * 32,
        "openrouter_api_key": "router-key",
    }
    values.update(overrides)
    return Settings(_env_file=None, **values)


def _state_from(url: str) -> tuple[str, dict[str, list[str]]]:
    params = parse_qs(urlsplit(url).query)
    return params["state"][0], params


def test_oauth_flow_uses_pkce_encrypts_token_and_queues_collection():
    settings = _settings()
    conn = db.connect(settings)
    db.init_schema(conn, settings)

    authorization_url = begin_oauth(conn, settings, actor="Admin@Example.test")
    state, params = _state_from(authorization_url)
    stored_state = conn.execute("SELECT * FROM gmail_oauth_state").fetchone()

    assert params["scope"] == ["https://www.googleapis.com/auth/gmail.readonly"]
    assert params["access_type"] == ["offline"]
    assert params["prompt"] == ["consent"]
    assert params["code_challenge_method"] == ["S256"]
    assert params["redirect_uri"] == [settings.gmail_oauth_redirect_uri]
    assert stored_state["code_verifier_enc"].startswith("gAAAA")

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url == httpx.URL("https://oauth2.googleapis.com/token"):
            form = parse_qs(request.content.decode())
            assert form["code"] == ["authorization-code"]
            assert form["code_verifier"][0]
            return httpx.Response(200, json={
                "access_token": "short-lived-access",
                "refresh_token": "long-lived-refresh",
                "scope": "https://www.googleapis.com/auth/gmail.readonly",
            })
        assert request.url == httpx.URL(
            "https://gmail.googleapis.com/gmail/v1/users/me/profile"
        )
        assert request.headers["authorization"] == "Bearer short-lived-access"
        return httpx.Response(200, json={"emailAddress": "News@Example.test"})

    email = complete_oauth(
        conn,
        settings,
        actor="admin@example.test",
        state=state,
        code="authorization-code",
        transport=httpx.MockTransport(handler),
    )

    connection = repo.get_gmail_connection(conn)
    assert email == "News@Example.test"
    assert connection["email_address"] == "news@example.test"
    assert "long-lived-refresh" not in connection["refresh_token_enc"]
    assert decrypt_refresh_token(
        settings, connection["refresh_token_enc"]
    ) == "long-lived-refresh"
    assert gmail_refresh_token(conn, settings) == "long-lived-refresh"
    assert repo.ensure_email_task(conn) is False
    status = connection_status(conn, settings)
    assert status["connected"] is True
    assert status["managed_by"] == "oauth"

    with pytest.raises(GmailOAuthError, match="already been used"):
        complete_oauth(
            conn,
            settings,
            actor="admin@example.test",
            state=state,
            code="authorization-code",
            transport=httpx.MockTransport(handler),
        )


def test_oauth_state_is_bound_to_dashboard_user():
    settings = _settings()
    conn = db.connect(settings)
    db.init_schema(conn, settings)
    state, _params = _state_from(
        begin_oauth(conn, settings, actor="first@example.test")
    )

    with pytest.raises(GmailOAuthError, match="another dashboard user"):
        complete_oauth(
            conn,
            settings,
            actor="second@example.test",
            state=state,
            code="unused",
        )


def test_disconnect_revokes_and_removes_database_token():
    settings = _settings()
    conn = db.connect(settings)
    db.init_schema(conn, settings)
    repo.upsert_gmail_connection(
        conn,
        email_address="news@example.test",
        refresh_token_enc=encrypt_refresh_token(settings, "refresh-token"),
        scopes="https://www.googleapis.com/auth/gmail.readonly",
        connected_by="admin@example.test",
    )
    conn.commit()

    def handler(request: httpx.Request) -> httpx.Response:
        assert request.url == httpx.URL("https://oauth2.googleapis.com/revoke")
        assert parse_qs(request.content.decode())["token"] == ["refresh-token"]
        return httpx.Response(200)

    assert disconnect_gmail(
        conn, settings, transport=httpx.MockTransport(handler)
    ) is True
    assert repo.get_gmail_connection(conn) is None


def test_disconnect_still_removes_local_grant_when_google_is_unreachable():
    settings = _settings()
    conn = db.connect(settings)
    db.init_schema(conn, settings)
    repo.upsert_gmail_connection(
        conn,
        email_address="news@example.test",
        refresh_token_enc=encrypt_refresh_token(settings, "refresh-token"),
        scopes="https://www.googleapis.com/auth/gmail.readonly",
        connected_by="admin@example.test",
    )
    conn.commit()

    def handler(_request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("offline")

    assert disconnect_gmail(
        conn, settings, transport=httpx.MockTransport(handler)
    ) is False
    assert repo.get_gmail_connection(conn) is None


def test_admin_api_starts_oauth_and_callback_requires_gateway_identity(
    tmp_path, monkeypatch
):
    settings = _settings(tmp_path)
    conn = db.connect(settings)
    db.init_schema(conn, settings)
    conn.close()
    client = TestClient(create_app(settings))

    assert client.post("/api/admin/gmail/connect").status_code == 403
    started = client.post(
        "/api/admin/gmail/connect",
        headers={
            "X-NBK-Admin-Action": "1",
            "X-NBK-User-Email": "admin@example.test",
        },
        json={},
    )
    assert started.status_code == 200
    assert started.json()["authorization_url"].startswith(
        "https://accounts.google.com/o/oauth2/v2/auth?"
    )
    assert client.get(
        "/api/admin/gmail/callback", params={"state": "x", "code": "y"}
    ).status_code == 401

    monkeypatch.setattr(
        "nbkommune.api.complete_oauth",
        lambda *_args, **_kwargs: "news@example.test",
    )
    callback = client.get(
        "/api/admin/gmail/callback",
        params={"state": "x", "code": "y"},
        headers={"X-NBK-User-Email": "admin@example.test"},
        follow_redirects=False,
    )
    assert callback.status_code == 303
    assert callback.headers["location"] == "/?gmail=connected"
