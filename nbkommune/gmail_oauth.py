"""Admin-driven Gmail OAuth with PKCE and encrypted refresh-token storage."""
from __future__ import annotations

import base64
import hashlib
import secrets
from datetime import UTC, datetime, timedelta
from urllib.parse import urlencode

import httpx
from cryptography.fernet import Fernet, InvalidToken

from nbkommune import repositories as repo
from nbkommune.db import Connection
from nbkommune.settings import Settings

GMAIL_READONLY_SCOPE = "https://www.googleapis.com/auth/gmail.readonly"
_AUTHORIZE_URL = "https://accounts.google.com/o/oauth2/v2/auth"
_TOKEN_URL = "https://oauth2.googleapis.com/token"
_REVOKE_URL = "https://oauth2.googleapis.com/revoke"
_PROFILE_URL = "https://gmail.googleapis.com/gmail/v1/users/me/profile"
_STATE_TTL_MINUTES = 10


class GmailOAuthError(RuntimeError):
    """A safe, operator-facing OAuth failure."""


class GmailNotConnectedError(GmailOAuthError):
    """The collector has credentials but no authorised inbox."""


def _state_hash(state: str) -> str:
    return hashlib.sha256(state.encode("utf-8")).hexdigest()


def _challenge(verifier: str) -> str:
    digest = hashlib.sha256(verifier.encode("ascii")).digest()
    return base64.urlsafe_b64encode(digest).rstrip(b"=").decode("ascii")


def _fernet(settings: Settings) -> Fernet:
    secret = settings.gmail_token_encryption_key.get_secret_value()
    if len(secret) < 32:
        raise GmailOAuthError(
            "NBK_GMAIL_TOKEN_ENCRYPTION_KEY must be at least 32 characters"
        )
    # Accept an ordinary high-entropy deployment secret instead of forcing the
    # operator to understand Fernet's base64 key representation.
    key = base64.urlsafe_b64encode(hashlib.sha256(secret.encode("utf-8")).digest())
    return Fernet(key)


def encrypt_refresh_token(settings: Settings, token: str) -> str:
    if not token:
        raise GmailOAuthError("Google did not return a refresh token")
    return _fernet(settings).encrypt(token.encode("utf-8")).decode("ascii")


def decrypt_refresh_token(settings: Settings, ciphertext: str) -> str:
    try:
        return _fernet(settings).decrypt(ciphertext.encode("ascii")).decode("utf-8")
    except (InvalidToken, ValueError) as exc:
        raise GmailOAuthError(
            "The stored Gmail token cannot be decrypted with the configured key"
        ) from exc


def gmail_connection_available(conn: Connection, settings: Settings) -> bool:
    return bool(
        repo.get_gmail_connection(conn)
        or settings.gmail_refresh_token.get_secret_value().strip()
    )


def gmail_refresh_token(conn: Connection, settings: Settings) -> str:
    """Resolve the admin-managed token, with the old env token as fallback."""
    connection = repo.get_gmail_connection(conn)
    if connection:
        return decrypt_refresh_token(settings, connection["refresh_token_enc"])
    legacy = settings.gmail_refresh_token.get_secret_value().strip()
    if legacy:
        return legacy
    raise GmailNotConnectedError("No Gmail inbox is connected")


def connection_status(conn: Connection, settings: Settings) -> dict:
    connection = repo.get_gmail_connection(conn)
    legacy = settings.gmail_refresh_token.get_secret_value().strip()
    return {
        "configured": settings.gmail_oauth_configured,
        "enabled": settings.gmail_enabled,
        "connected": bool(connection or legacy),
        "managed_by": "oauth" if connection else "environment" if legacy else None,
        "email_address": connection["email_address"] if connection else None,
        "connected_by": connection["connected_by"] if connection else None,
        "connected_at": connection["connected_at"] if connection else None,
        "last_sync_at": connection["last_sync_at"] if connection else None,
        "last_sync_error": connection["last_sync_error"] if connection else None,
        "query": settings.gmail_query,
        "redirect_uri": settings.gmail_oauth_redirect_uri,
    }


def begin_oauth(conn: Connection, settings: Settings, *, actor: str) -> str:
    """Create a one-use OAuth transaction and return Google's consent URL."""
    settings.validate_gmail_oauth()
    state = secrets.token_urlsafe(32)
    verifier = secrets.token_urlsafe(64)
    expires_at = (datetime.now(UTC) + timedelta(minutes=_STATE_TTL_MINUTES)).isoformat(
        timespec="seconds"
    )
    repo.create_gmail_oauth_state(
        conn,
        state_hash=_state_hash(state),
        code_verifier_enc=encrypt_refresh_token(settings, verifier),
        actor=actor.casefold().strip(),
        expires_at=expires_at,
    )
    conn.commit()
    params = {
        "client_id": settings.gmail_client_id,
        "redirect_uri": settings.gmail_oauth_redirect_uri,
        "response_type": "code",
        "scope": GMAIL_READONLY_SCOPE,
        "access_type": "offline",
        "prompt": "consent",
        "include_granted_scopes": "true",
        "state": state,
        "code_challenge": _challenge(verifier),
        "code_challenge_method": "S256",
    }
    return f"{_AUTHORIZE_URL}?{urlencode(params)}"


def _as_utc(value: datetime | str) -> datetime:
    parsed = value if isinstance(value, datetime) else datetime.fromisoformat(value)
    return parsed if parsed.tzinfo else parsed.replace(tzinfo=UTC)


def complete_oauth(conn: Connection, settings: Settings, *, actor: str,
                   state: str, code: str,
                   transport: httpx.BaseTransport | None = None) -> str:
    """Consume a callback, persist the encrypted grant, and return the inbox."""
    settings.validate_gmail_oauth()
    transaction = repo.consume_gmail_oauth_state(conn, _state_hash(state))
    conn.commit()  # state is one-use even when Google rejects the code
    if transaction is None:
        raise GmailOAuthError("OAuth session is missing or has already been used")
    if transaction["actor"].casefold() != actor.casefold().strip():
        raise GmailOAuthError("OAuth session belongs to another dashboard user")
    if _as_utc(transaction["expires_at"]) < datetime.now(UTC):
        raise GmailOAuthError("OAuth session has expired; start the connection again")
    if not code:
        raise GmailOAuthError("Google did not return an authorization code")

    try:
        with httpx.Client(timeout=30.0, transport=transport) as client:
            token_response = client.post(_TOKEN_URL, data={
                "client_id": settings.gmail_client_id,
                "client_secret": settings.gmail_client_secret.get_secret_value(),
                "code": code,
                "code_verifier": decrypt_refresh_token(
                    settings, transaction["code_verifier_enc"]
                ),
                "grant_type": "authorization_code",
                "redirect_uri": settings.gmail_oauth_redirect_uri,
            })
            if not token_response.is_success:
                raise GmailOAuthError(
                    f"Google rejected the authorization code ({token_response.status_code})"
                )
            token_data = token_response.json()
            access_token = str(token_data.get("access_token") or "")
            refresh_token = str(token_data.get("refresh_token") or "")
            if not access_token:
                raise GmailOAuthError("Google did not return an access token")

            profile_response = client.get(
                _PROFILE_URL,
                headers={"Authorization": f"Bearer {access_token}"},
            )
            if not profile_response.is_success:
                raise GmailOAuthError(
                    f"Gmail profile check failed ({profile_response.status_code})"
                )
            email_address = str(
                profile_response.json().get("emailAddress") or ""
            ).strip()
            if not email_address:
                raise GmailOAuthError("Gmail did not identify the connected mailbox")
    except httpx.HTTPError as exc:
        raise GmailOAuthError("Could not contact Google OAuth/Gmail") from exc

    if refresh_token:
        encrypted = encrypt_refresh_token(settings, refresh_token)
    else:
        existing = repo.get_gmail_connection(conn)
        if (not existing
                or existing["email_address"].casefold() != email_address.casefold()):
            raise GmailOAuthError(
                "Google did not return offline access; revoke access and connect again"
            )
        encrypted = existing["refresh_token_enc"]
    scopes = str(token_data.get("scope") or GMAIL_READONLY_SCOPE)
    repo.upsert_gmail_connection(
        conn,
        email_address=email_address,
        refresh_token_enc=encrypted,
        scopes=scopes,
        connected_by=actor,
    )
    if settings.gmail_enabled:
        repo.ensure_email_task(conn)
    conn.commit()
    return email_address


def disconnect_gmail(conn: Connection, settings: Settings, *,
                     transport: httpx.BaseTransport | None = None) -> bool:
    """Revoke the admin-managed grant, then always remove it locally."""
    connection = repo.get_gmail_connection(conn)
    if not connection:
        return False
    token = decrypt_refresh_token(settings, connection["refresh_token_enc"])
    revoked = False
    try:
        with httpx.Client(timeout=15.0, transport=transport) as client:
            response = client.post(_REVOKE_URL, data={"token": token})
            revoked = response.is_success
    except httpx.HTTPError:
        # Local disconnect must still work if Google is temporarily
        # unreachable. A revoked/expired token is harmless at this point.
        revoked = False
    finally:
        repo.delete_gmail_connection(conn)
        conn.commit()
    return revoked
