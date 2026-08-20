from __future__ import annotations

import pytest

from nbkommune.settings import Settings


def test_auth_can_be_disabled_without_dashboard_secrets():
    settings = Settings(_env_file=None, auth_enabled=False)

    assert settings.auth_enabled is False


def test_enabled_auth_requires_public_url_and_strong_secret():
    with pytest.raises(ValueError, match="NBK_AUTH_BASE_URL"):
        Settings(_env_file=None, auth_enabled=True).validate_auth()

    with pytest.raises(ValueError, match="NBK_AUTH_SECRET"):
        Settings(
            _env_file=None,
            auth_enabled=True,
            auth_base_url="https://dashboard.example.com",
            auth_secret="too-short",
        ).validate_auth()


def test_auth_bootstrap_credentials_must_be_complete():
    with pytest.raises(ValueError, match="must be set together"):
        Settings(
            _env_file=None,
            auth_enabled=True,
            auth_base_url="https://dashboard.example.com",
            auth_secret="a" * 32,
            auth_bootstrap_email="admin@example.com",
        ).validate_auth()


def test_enabled_gmail_requires_oauth_and_openrouter_secrets():
    settings = Settings(_env_file=None, gmail_enabled=True)

    with pytest.raises(ValueError, match="NBK_GMAIL_CLIENT_ID"):
        settings.validate_gmail()

    Settings(
        _env_file=None,
        gmail_enabled=True,
        gmail_client_id="client",
        gmail_client_secret="secret",
        gmail_refresh_token="refresh",
        openrouter_api_key="router",
    ).validate_gmail()
