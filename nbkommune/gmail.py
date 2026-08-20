"""Small Gmail REST client and MIME parser for the municipality inbox."""
from __future__ import annotations

import base64
import json
import re
from collections.abc import Iterator
from dataclasses import dataclass
from datetime import UTC, datetime
from email.header import decode_header, make_header
from email.utils import parseaddr, parsedate_to_datetime
from urllib.parse import urlsplit

import httpx
from bs4 import BeautifulSoup

from nbkommune.records import collapse_ws, scrub_text
from nbkommune.settings import Settings

_URL = re.compile(r"https?://[^\s<>'\"]+", re.I)
_REPLY_MARKER = re.compile(
    r"^(?:On .+ wrote:|Den .+ skrev .+:|Fra:\s|From:\s|-{2,}\s*Original Message\s*-{2,})",
    re.I,
)


@dataclass(frozen=True)
class ParsedEmail:
    gmail_message_id: str
    gmail_thread_id: str | None
    sender_name: str
    sender_email: str
    subject: str
    sent_at: str | None
    received_at: str
    body_text: str
    body_html: str | None
    links: list[str]
    raw: dict

    def as_row(self) -> dict:
        return {
            "id": self.gmail_message_id,
            "thread_id": self.gmail_thread_id,
            "sender_name": self.sender_name,
            "sender_email": self.sender_email,
            "subject": self.subject,
            "sent_at": self.sent_at,
            "received_at": self.received_at,
            "body_text": self.body_text,
            "body_html": self.body_html,
            "links_json": json.dumps(self.links, ensure_ascii=False),
            "raw_json": json.dumps(self.raw, ensure_ascii=False),
        }


def _decode(data: str, content_type: str = "") -> str:
    raw = base64.urlsafe_b64decode(data + "=" * (-len(data) % 4))
    charset_match = re.search(r"charset=[\"']?([^;\"']+)", content_type, re.I)
    charset = charset_match.group(1) if charset_match else "utf-8"
    try:
        return raw.decode(charset, errors="replace")
    except LookupError:
        return raw.decode("utf-8", errors="replace")


def _decode_header(value: str) -> str:
    """Decode RFC 2047 names/subjects while failing open on malformed mail."""
    try:
        return str(make_header(decode_header(value)))
    except (LookupError, UnicodeError):
        return value


def _parts(payload: dict) -> tuple[list[str], list[str]]:
    plain: list[str] = []
    html: list[str] = []
    mime = str(payload.get("mimeType") or "").casefold()
    headers = {str(h.get("name", "")).casefold(): str(h.get("value", ""))
               for h in payload.get("headers", [])}
    data = (payload.get("body") or {}).get("data")
    if data and mime == "text/plain":
        plain.append(_decode(data, headers.get("content-type", "")))
    elif data and mime == "text/html":
        html.append(_decode(data, headers.get("content-type", "")))
    for child in payload.get("parts") or []:
        child_plain, child_html = _parts(child)
        plain.extend(child_plain)
        html.extend(child_html)
    return plain, html


def _clean_text(value: str) -> str:
    lines: list[str] = []
    for line in value.replace("\r\n", "\n").replace("\r", "\n").splitlines():
        if _REPLY_MARKER.match(line.strip()):
            break
        if line.lstrip().startswith(">"):
            continue
        lines.append(line.rstrip())
    text = "\n".join(lines)
    text = re.sub(r"\n{3,}", "\n\n", text)
    return text.strip()


def parse_gmail_message(data: dict) -> ParsedEmail:
    payload = data.get("payload") or {}
    headers = {str(h.get("name", "")).casefold(): str(h.get("value", ""))
               for h in payload.get("headers", [])}
    sender_name, sender_email = parseaddr(_decode_header(headers.get("from", "")))
    sender_email = sender_email.casefold().strip()
    if not sender_email:
        raise ValueError(f"Gmail message {data.get('id')!r} has no sender address")

    plain_parts, html_parts = _parts(payload)
    body_html = "\n".join(part for part in html_parts if part).strip() or None
    if plain_parts:
        body_text = "\n".join(part for part in plain_parts if part)
    elif body_html:
        body_text = BeautifulSoup(body_html, "html.parser").get_text("\n")
    else:
        body_text = str(data.get("snippet") or "")
    body_text = _clean_text(body_text)

    links: list[str] = []
    if body_html:
        for tag in BeautifulSoup(body_html, "html.parser").find_all("a", href=True):
            href = str(tag["href"]).strip().rstrip(".,;)")
            if urlsplit(href).scheme in {"http", "https"} and href not in links:
                links.append(href)
    for match in _URL.findall(body_text):
        link = match.rstrip(".,;)")
        if link not in links:
            links.append(link)

    internal_ms = int(data.get("internalDate") or 0)
    received = datetime.fromtimestamp(internal_ms / 1000, UTC) if internal_ms else datetime.now(UTC)
    sent_at: str | None = None
    if headers.get("date"):
        try:
            sent = parsedate_to_datetime(headers["date"])
            if sent.tzinfo is None:
                sent = sent.replace(tzinfo=UTC)
            sent_at = sent.astimezone(UTC).isoformat(timespec="seconds")
        except (TypeError, ValueError, OverflowError):
            pass

    return ParsedEmail(
        gmail_message_id=str(data["id"]),
        gmail_thread_id=str(data["threadId"]) if data.get("threadId") else None,
        sender_name=collapse_ws(scrub_text(sender_name)) or "",
        sender_email=sender_email,
        subject=collapse_ws(scrub_text(_decode_header(headers.get("subject", "")))) or "",
        sent_at=sent_at,
        received_at=received.isoformat(timespec="seconds"),
        body_text=scrub_text(body_text) or "",
        body_html=scrub_text(body_html),
        links=links,
        raw={
            "history_id": data.get("historyId"),
            "label_ids": data.get("labelIds") or [],
            "size_estimate": data.get("sizeEstimate"),
            "rfc_message_id": headers.get("message-id"),
        },
    )


class GmailClient:
    def __init__(self, settings: Settings, *,
                 transport: httpx.BaseTransport | None = None) -> None:
        self.settings = settings
        self._client = httpx.Client(timeout=30.0, transport=transport)
        self._access_token: str | None = None

    def close(self) -> None:
        self._client.close()

    def __enter__(self):
        return self

    def __exit__(self, *_exc: object) -> None:
        self.close()

    def _token(self) -> str:
        if self._access_token:
            return self._access_token
        response = self._client.post("https://oauth2.googleapis.com/token", data={
            "client_id": self.settings.gmail_client_id,
            "client_secret": self.settings.gmail_client_secret.get_secret_value(),
            "refresh_token": self.settings.gmail_refresh_token.get_secret_value(),
            "grant_type": "refresh_token",
        })
        response.raise_for_status()
        self._access_token = str(response.json()["access_token"])
        return self._access_token

    def _headers(self) -> dict[str, str]:
        return {"Authorization": f"Bearer {self._token()}"}

    def iter_message_ids(self) -> Iterator[str]:
        """Yield query matches across pages, bounded by gmail_scan_limit."""
        page_token: str | None = None
        scanned = 0
        while scanned < self.settings.gmail_scan_limit:
            params = {
                "q": self.settings.gmail_query,
                "maxResults": min(500, self.settings.gmail_scan_limit - scanned),
            }
            if page_token:
                params["pageToken"] = page_token
            response = self._client.get(
                "https://gmail.googleapis.com/gmail/v1/users/me/messages",
                headers=self._headers(), params=params,
            )
            response.raise_for_status()
            data = response.json()
            messages = data.get("messages", [])
            for item in messages:
                scanned += 1
                yield str(item["id"])
                if scanned >= self.settings.gmail_scan_limit:
                    return
            page_token = data.get("nextPageToken")
            if not page_token or not messages:
                return

    def get_message(self, message_id: str) -> dict:
        response = self._client.get(
            f"https://gmail.googleapis.com/gmail/v1/users/me/messages/{message_id}",
            headers=self._headers(), params={"format": "full"},
        )
        response.raise_for_status()
        return response.json()
