"""RSS/Atom discovery — the cheapest and most reliable channel, when it exists.

Rare in this corpus: of 14 sampled kommune sites, only Frederikssund advertised a
feed via ``<link rel=alternate>`` and only Fredericia answered ``/rss.xml``. But
where a feed exists it gives title, link and a real *publication* date in one
request, which no other channel here reliably does — so it is always tried first.
"""
from __future__ import annotations

import logging
from typing import Any
from urllib.parse import urljoin

import feedparser

from nbkommune.dates import parse_danish_datetime
from nbkommune.http import HttpClient
from nbkommune.records import ListedArticle, looks_like_document
from nbkommune.targets import Target

logger = logging.getLogger(__name__)

# Paths worth probing when a page advertises no feed. Ordered by how often they
# hit in the survey; `/rss.xml` is the Drupal default and found Fredericia.
COMMON_FEED_PATHS = (
    "/rss.xml", "/rss", "/feed", "/feed.xml", "/atom.xml",
    "/nyheder/rss", "/rss/nyheder", "/?feed=rss2",
)


class FeedSource:
    """Lists articles from one RSS/Atom feed."""

    channel = "feed"

    def __init__(self, target: Target, http: HttpClient, feed_url: str) -> None:
        self.target = target
        self.http = http
        self.feed_url = feed_url

    @property
    def detail(self) -> str:
        return self.feed_url

    @property
    def resolved_config(self) -> dict:
        """Everything needed to rebuild this source without probing again."""
        return {"feed_url": self.feed_url}

    def list_articles(self) -> list[ListedArticle]:
        content, _ = self.http.get_bytes(self.feed_url)
        # feedparser is handed bytes deliberately: it honours the XML declaration's
        # own encoding, which is more often right than any header on these hosts.
        parsed = feedparser.parse(content)
        if parsed.bozo and not parsed.entries:
            raise ValueError(
                f"unparseable feed {self.feed_url}: "
                f"{getattr(parsed, 'bozo_exception', 'unknown error')}"
            )
        out: list[ListedArticle] = []
        for entry in parsed.entries:
            link = entry.get("link") or ""
            if not link:
                continue
            url = urljoin(self.feed_url, link)
            if looks_like_document(url, entry.get("title")):
                # A Drupal news feed happily lists PDFs and postlists alongside
                # articles; ingesting those stores a filename with no body.
                logger.debug("%s: skipping document entry %s", self.target.key, url)
                continue
            raw: dict[str, Any] = {
                "feed_url": self.feed_url,
                "id": entry.get("id"),
                "categories": [t.get("term") for t in entry.get("tags", []) if t.get("term")],
            }
            # Read through `dict.get`, NOT `entry.get`: feedparser aliases a
            # missing `published` to `updated`, so an Atom feed carrying only
            # <updated> would hand us a modification time disguised as a
            # publication date — exactly the conflation this scraper separates.
            published = (parse_danish_datetime(dict.get(entry, "published"))
                         or parse_danish_datetime(dict.get(entry, "created")))
            updated = parse_danish_datetime(dict.get(entry, "updated"))
            out.append(ListedArticle(
                url=url,
                title=entry.get("title"),
                summary=entry.get("summary"),
                published_at=published,
                # An Atom feed with only <updated> is common; using it as the
                # publication date would be wrong, so it stays in updated_at and
                # extraction decides.
                updated_at=updated,
                channel=self.channel,
                raw={k: v for k, v in raw.items() if v},
            ))
        return out


def discover_feed_url(html: str, base_url: str) -> str | None:
    """The feed advertised by a page's ``<link rel=alternate>``, if any."""
    from bs4 import BeautifulSoup

    soup = BeautifulSoup(html, "lxml")
    for link in soup.find_all("link"):
        rel = " ".join(link.get("rel") or []).lower()
        ctype = (link.get("type") or "").lower()
        if "alternate" not in rel:
            continue
        if "rss" in ctype or "atom" in ctype:
            href = link.get("href")
            if isinstance(href, str) and href.strip():
                return urljoin(base_url, href.strip())
    return None


def probe_feed_url(http: HttpClient, site_url: str) -> str | None:
    """Try the conventional feed paths. Returns the first that parses.

    A 200 is not enough: several of these sites answer every unknown path with
    their 200 HTML error page, which feedparser would happily accept as an empty
    feed. Requiring at least one entry is what separates a real feed from that.
    """
    for path in COMMON_FEED_PATHS:
        url = urljoin(site_url.rstrip("/") + "/", path.lstrip("/"))
        try:
            content, final = http.get_bytes(url)
        except Exception:
            continue
        parsed = feedparser.parse(content)
        if parsed.entries:
            logger.info("found feed for %s at %s (%d entries)",
                        site_url, final, len(parsed.entries))
            # The *final* URL, not the probed one: several of these sites redirect
            # /rss.xml to a language-prefixed path, and storing the pre-redirect
            # URL means re-paying that redirect on every crawl.
            return final
    return None
