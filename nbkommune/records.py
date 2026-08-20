"""Channel-independent scrape records.

These dataclasses are the **contract between any discovery channel and the
storage layer**: their ``as_row()`` output maps 1:1 onto the columns written by
``nbkommune.repositories``. A feed, a sitemap and a scraped HTML listing all
build the same ``ListedArticle``, so an article lands identically in the DB
regardless of how it was found.

Keep these free of channel specifics — nothing here should know about RSS
``<pubDate>`` or a site's CSS selectors.
"""
from __future__ import annotations

import hashlib
import json
import re
from dataclasses import dataclass, field
from typing import Any
from urllib.parse import urlsplit, urlunsplit

# Database TEXT values and JSON transport cannot safely carry NUL (0x00), and
# HTML scraped from the wild carries stray control characters often enough to
# matter. The remaining C0 range is never meaningful in an article body, so it
# all goes (keeping tab, newline and CR).
_C0_CONTROLS = {c: None for c in range(0x20) if c not in (0x09, 0x0A, 0x0D)}

# Tracking parameters that change the URL without changing the article. Stripped
# before the URL becomes an identity, or the same press release arrives twice
# because someone shared it with a campaign tag.
_TRACKING_PARAMS = re.compile(
    r"^(utm_[a-z_]+|fbclid|gclid|mc_cid|mc_eid|ref|source)$", re.I
)


# Files a news channel legitimately lists but which are not articles. Fredericia's
# Drupal RSS publishes "Budgetprocedure 2027-2030.pdf" and weekly postlists as
# feed entries; extracting those as articles yields rows with a filename for a
# title and no body.
_DOCUMENT_URL = re.compile(
    r"\.(pdf|docx?|xlsx?|pptx?|zip|csv|jpe?g|png|gif|svg|mp[34]|avi|mov)"
    r"(?:$|[?#])", re.I
)


def looks_like_document(url: str, title: str | None = None) -> bool:
    """Whether this listing entry points at a file rather than an article."""
    if _DOCUMENT_URL.search(url):
        return True
    return bool(title and _DOCUMENT_URL.search(title.strip()))


def scrub_text(value: str | None) -> str | None:
    """Strip control characters that can't be stored (or read) downstream.

    Identity for clean input — including the empty string, so scrubbing never
    shifts a ``""`` to ``None`` and perturbs a fingerprint.
    """
    return None if value is None else value.translate(_C0_CONTROLS)


def collapse_ws(value: str | None) -> str | None:
    """Collapse runs of whitespace to single spaces and trim.

    Applied to titles and summaries, never to bodies: a kommune CMS renders the
    same headline with different indentation on the listing and the detail page,
    and an uncollapsed title makes those look like two different articles.
    """
    if value is None:
        return None
    return re.sub(r"\s+", " ", value).strip()


def normalise_url(url: str) -> str:
    """Canonical form of an article URL, for use as identity.

    Lowercases the host (case-insensitive per RFC 3986) but never the path
    (many kommune CMSes serve case-sensitive slugs), drops the fragment and any
    tracking query parameters, and strips a trailing slash. Deliberately does
    NOT force https: if a site serves http we keep what it served, so identity
    survives a site's own scheme migration only via `canonical_url` on the row.
    """
    parts = urlsplit(url.strip())
    query = "&".join(
        q for q in parts.query.split("&")
        if q and not _TRACKING_PARAMS.match(q.split("=", 1)[0])
    )
    path = parts.path.rstrip("/") or "/"
    return urlunsplit((parts.scheme.lower(), parts.netloc.lower(), path, query, ""))


def article_id(url: str) -> str:
    """Stable article id — a hash of the normalised URL.

    Keyed on the URL, not on a CMS-assigned node id: the same article is
    reachable as ``/node/4711`` and ``/nyheder/some-slug`` on several of these
    sites, and the numeric id is the one that gets renumbered on migration. The
    slug URL is what a human and a search index both see, so it is identity.
    """
    return hashlib.sha256(normalise_url(url).encode("utf-8")).hexdigest()[:32]


@dataclass
class ListedArticle:
    """An article as it appears in a feed / sitemap / listing page.

    Metadata only — enough to decide whether the article is new or changed and
    therefore worth fetching. Every field except ``url`` is optional, because
    what a channel exposes varies wildly: an RSS feed gives title + pubDate, a
    sitemap gives lastmod and nothing else.
    """

    url: str
    title: str | None = None
    summary: str | None = None
    published_at: str | None = None   # ISO 8601, when the channel states it
    updated_at: str | None = None     # ISO 8601 (sitemap <lastmod>, feed <updated>)
    kind: str = "ukendt"              # nyhed | pressemeddelelse | ukendt
    channel: str = "listing"          # feed | sitemap | listing
    raw: dict[str, Any] = field(default_factory=dict)

    def __post_init__(self) -> None:
        self.title = collapse_ws(scrub_text(self.title))
        self.summary = collapse_ws(scrub_text(self.summary))

    @property
    def id(self) -> str:
        return article_id(self.url)

    def fingerprint(self) -> str:
        """Stable hash of the fields that signal the listing changed.

        Includes ``updated_at`` so a sitemap ``<lastmod>`` bump alone re-queues
        the article — for a sitemap-only site that timestamp is the *only*
        change signal there is. Excludes ``summary``: several sites render a
        rotating teaser, which would otherwise mark every article changed on
        every pass.
        """
        parts = "|".join(
            str(p) for p in (
                normalise_url(self.url), self.title,
                self.published_at, self.updated_at, self.kind,
            )
        )
        return hashlib.sha256(parts.encode("utf-8")).hexdigest()

    def as_row(self, *, municipality_key: str) -> dict[str, Any]:
        return {
            "municipality_key": municipality_key,
            "id": self.id,
            "url": self.url,
            "canonical_url": normalise_url(self.url),
            "title": self.title,
            "summary": self.summary,
            "published_at": self.published_at,
            "updated_at": self.updated_at,
            "kind": self.kind,
            "channel": self.channel,
            "listing_hash": self.fingerprint(),
            "raw_json": json.dumps(self.raw, ensure_ascii=False),
        }


@dataclass
class ArticleDetail:
    """The extracted article itself.

    ``provenance`` records which extraction layer produced each field
    (``jsonld`` / ``meta:*`` / ``rule:*`` / ``feed`` /
    ``listing:configured`` / ``heuristic``). That is not decoration: with 98
    heterogeneous sites and no common CMS, the only way to find the ones whose
    extraction has silently degraded is to ask which layer supplied each field.
    """

    url: str
    title: str | None
    summary: str | None
    body_text: str | None
    body_html: str | None
    published_at: str | None
    updated_at: str | None
    image_url: str | None
    author: str | None
    categories: list[str] = field(default_factory=list)
    lang: str | None = None
    canonical_url: str | None = None
    provenance: dict[str, str] = field(default_factory=dict)
    raw: dict[str, Any] = field(default_factory=dict)

    def __post_init__(self) -> None:
        # Scrub on the way in, not in as_row(), so fingerprint() hashes exactly
        # what the DB will hold. `raw` needs no scrubbing: as_row() json.dumps()
        # it, and json escapes control chars to \uXXXX.
        self.title = collapse_ws(scrub_text(self.title))
        self.summary = collapse_ws(scrub_text(self.summary))
        self.body_text = scrub_text(self.body_text)
        self.body_html = scrub_text(self.body_html)

    @property
    def id(self) -> str:
        return article_id(self.url)

    @property
    def word_count(self) -> int:
        return len((self.body_text or "").split())

    def fingerprint(self) -> str:
        """Stable hash of the *content* — what a consumer would re-index.

        Excludes ``updated_at``: several of these CMSes stamp a fresh
        "sidst opdateret" on every publish cycle whether or not a word changed,
        and treating that as content change would re-index the whole corpus
        nightly. A real edit moves ``body_text`` or ``title``, which are here.
        """
        parts = json.dumps(
            [self.title, self.summary, self.body_text, self.published_at,
             self.image_url, self.author, sorted(self.categories)],
            ensure_ascii=False, default=str,
        )
        return hashlib.sha256(parts.encode("utf-8")).hexdigest()

    def as_row(self, *, municipality_key: str) -> dict[str, Any]:
        return {
            "municipality_key": municipality_key,
            "id": self.id,
            "url": self.url,
            "canonical_url": self.canonical_url or normalise_url(self.url),
            "title": self.title,
            "summary": self.summary,
            "body_text": self.body_text,
            "body_html": self.body_html,
            "published_at": self.published_at,
            "updated_at": self.updated_at,
            "image_url": self.image_url,
            "author": self.author,
            "categories_json": json.dumps(self.categories, ensure_ascii=False),
            "lang": self.lang,
            "word_count": self.word_count,
            "detail_hash": self.fingerprint(),
            "provenance_json": json.dumps(self.provenance, ensure_ascii=False),
            "raw_json": json.dumps(self.raw, ensure_ascii=False),
        }
