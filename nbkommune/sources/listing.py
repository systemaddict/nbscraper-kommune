"""HTML listing discovery — the universal fallback.

Used when a site has no feed and its sitemap is unusable. Two modes:

- **configured**: ``item_selector`` (a CSS selector for each row) plus optional
  ``link_selector`` / ``title_selector`` / ``date_selector`` inside it. This is
  where a site-specific fix goes, and it needs no code change — the selectors
  live in ``config/targets.json``.
- **heuristic**: links whose path sits *below* the listing page's own path and
  whose last segment looks like an article slug. In the survey this found article
  links on 5 of 8 listing pages unaided; the other 3 need selectors or a browser.

Publication dates found here are valuable out of proportion to their looks: for
the many sites whose article pages expose only a "sidst opdateret" stamp, the
listing row is the only place a real publication date appears at all.
"""
from __future__ import annotations

import logging
import re
from urllib.parse import urljoin, urlparse

from bs4 import BeautifulSoup
from bs4.element import Tag

from nbkommune.dates import parse_danish_datetime
from nbkommune.http import HttpClient
from nbkommune.records import ListedArticle, looks_like_document, normalise_url
from nbkommune.targets import Target

logger = logging.getLogger(__name__)

# An article slug: at least two hyphen-joined words. Section pages ("/nyheder",
# "/presse") and pagination ("/side/2") do not look like this.
_SLUG = re.compile(r"[a-z0-9æøå]+(?:-[a-z0-9æøå]+){1,}$", re.I)

# Paths that are never articles even when they sit under the listing path.
_NOT_ARTICLE = re.compile(
    r"/(side|page|p)/\d+/?$|\.(pdf|jpg|jpeg|png|docx?|xlsx?)$|[?&]page=", re.I
)

# Nearby text that is a date. Danish listings render these as bare text next to
# the headline, so the row's whole text is searched when no date selector is set.
_DATE_IN_TEXT = re.compile(
    r"\d{1,2}\.\s*(?:jan|feb|mar|apr|maj|jun|jul|aug|sep|okt|nov|dec)[a-zæøå]*\.?\s*\d{4}"
    r"|\d{4}-\d{2}-\d{2}"
    r"|\d{1,2}[./-]\d{1,2}[./-]\d{4}",
    re.I,
)


class ListingSource:
    """Lists articles by scraping one or more listing pages."""

    channel = "listing"

    def __init__(self, target: Target, http: HttpClient,
                 urls: list[str] | None = None,
                 prefetched: dict[str, str] | None = None) -> None:
        self.target = target
        self.http = http
        self.urls = urls or target.listing_urls
        # HTML the resolver already fetched while sniffing channels — reused so
        # channel resolution costs one request, not three.
        self.prefetched = prefetched or {}

    @property
    def detail(self) -> str:
        mode = "configured" if self.target.config.get("item_selector") else "heuristic"
        return f"{mode}: {', '.join(self.urls)}"

    @property
    def resolved_config(self) -> dict:
        """Everything needed to rebuild this source without probing again."""
        return {"listing_urls": self.urls}

    def _html(self, url: str) -> tuple[str, str]:
        cached = self.prefetched.get(url)
        if cached is not None:
            return cached, url
        return self.http.get_text(url)

    # ── configured mode ─────────────────────────────────────────
    def _from_selectors(self, soup: BeautifulSoup, page_url: str) -> list[ListedArticle]:
        cfg = self.target.config
        item_selector = cfg["item_selector"]
        link_selector = cfg.get("link_selector", "a")
        out: list[ListedArticle] = []
        for row in soup.select(item_selector):
            link = row.select_one(link_selector) if link_selector else None
            if link is None or not isinstance(link.get("href"), str):
                continue
            url = urljoin(page_url, link["href"].strip())
            title = None
            if cfg.get("title_selector"):
                el = row.select_one(cfg["title_selector"])
                title = el.get_text(" ", strip=True) if el else None
            title = title or self._title_from(link, row)
            out.append(ListedArticle(
                url=url, title=title,
                summary=self._text(row, cfg.get("summary_selector")),
                published_at=self._date(row, cfg.get("date_selector")),
                channel=self.channel,
                raw={"listing_url": page_url, "mode": "configured"},
            ))
        return out

    @staticmethod
    def _text(row: Tag, selector: str | None) -> str | None:
        if not selector:
            return None
        el = row.select_one(selector)
        return el.get_text(" ", strip=True) if el else None

    @staticmethod
    def _date(row: Tag, selector: str | None) -> str | None:
        """A date from the configured selector, else any date-shaped text in the row.

        Checks ``datetime`` and ``content`` attributes before the rendered text:
        a ``<time datetime>`` is unambiguous where "18/08" is not.
        """
        candidates: list[str] = []
        if selector:
            el = row.select_one(selector)
            if el is not None:
                for attr in ("datetime", "content", "data-date"):
                    value = el.get(attr)
                    if isinstance(value, str) and value.strip():
                        candidates.append(value)
                candidates.append(el.get_text(" ", strip=True))
        else:
            for time_el in row.find_all("time"):
                value = time_el.get("datetime")
                candidates.append(value if isinstance(value, str)
                                  else time_el.get_text(" ", strip=True))
            match = _DATE_IN_TEXT.search(row.get_text(" ", strip=True))
            if match:
                candidates.append(match.group(0))
        for candidate in candidates:
            parsed = parse_danish_datetime(candidate)
            if parsed:
                return parsed
        return None

    # ── titles ──────────────────────────────────────────────────
    @staticmethod
    def _dedupe_repeat(text: str) -> str:
        """Collapse a title that is the same phrase twice.

        Common on these listings: the anchor wraps both an image (whose ``alt``
        repeats the headline) and the heading, so the anchor's combined text is
        "Headline Headline".
        """
        words = text.split()
        if len(words) >= 2 and len(words) % 2 == 0:
            half = len(words) // 2
            if words[:half] == words[half:]:
                return " ".join(words[:half])
        return text

    @classmethod
    def _title_from(cls, anchor: Tag, row: Tag) -> str | None:
        """Best title for a listing row, structure first.

        Anchor text is the *last* resort, not the first: on these sites an anchor
        routinely wraps an image and a heading (giving a doubled title) or only an
        image (giving none at all). A heading element inside the row is both more
        reliable and cleaner.
        """
        for scope in (anchor, row):
            for selector in ("h1", "h2", "h3", "h4", "h5",
                             "[class*='title']", "[class*='titel']",
                             "[class*='heading']", "[class*='overskrift']"):
                el = scope.select_one(selector)
                if el is not None:
                    text = el.get_text(" ", strip=True)
                    if text:
                        return cls._dedupe_repeat(text)
        for attr in ("title", "aria-label"):
            value = anchor.get(attr)
            if isinstance(value, str) and value.strip():
                return value.strip()
        text = anchor.get_text(" ", strip=True)
        if text:
            return cls._dedupe_repeat(text)
        # An image-only anchor still names the article in its alt text.
        img = anchor.find("img")
        if img is not None and isinstance(img.get("alt"), str) and img["alt"].strip():
            return img["alt"].strip()
        return None

    # ── heuristic mode ──────────────────────────────────────────
    def _from_heuristic(self, soup: BeautifulSoup, page_url: str) -> list[ListedArticle]:
        page = urlparse(page_url)
        base_path = page.path.rstrip("/")
        seen: set[str] = set()
        out: list[ListedArticle] = []
        for anchor in soup.find_all("a"):
            href = anchor.get("href")
            if not isinstance(href, str) or not href.strip():
                continue
            url = urljoin(page_url, href.strip())
            parts = urlparse(url)
            if parts.netloc != page.netloc or parts.scheme not in ("http", "https"):
                continue
            path = parts.path.rstrip("/")
            if base_path and not path.lower().startswith(base_path.lower() + "/"):
                continue
            if path.lower() == base_path.lower() or _NOT_ARTICLE.search(url):
                continue
            if looks_like_document(url):
                continue
            if not _SLUG.search(path.split("/")[-1]):
                continue
            key = normalise_url(url)
            if key in seen:
                continue
            seen.add(key)
            # The row is the anchor's nearest block ancestor — that is where a
            # listing puts the date and teaser, not on the anchor itself.
            row = anchor.find_parent(["article", "li", "div"]) or anchor
            out.append(ListedArticle(
                url=url,
                title=self._title_from(anchor, row),
                published_at=self._date(row, None),
                channel=self.channel,
                raw={"listing_url": page_url, "mode": "heuristic"},
            ))
        return out

    def list_articles(self) -> list[ListedArticle]:
        configured = bool(self.target.config.get("item_selector"))
        out: list[ListedArticle] = []
        seen: set[str] = set()
        for url in self.urls:
            try:
                html, final = self._html(url)
            except Exception as exc:
                # One dead listing page must not lose the other: a `separat`
                # kommune has two, and the press page failing is not a reason to
                # skip its news.
                logger.warning("%s: listing %s failed: %s", self.target.key, url, exc)
                continue
            soup = BeautifulSoup(html, "lxml")
            found = (self._from_selectors(soup, final) if configured
                     else self._from_heuristic(soup, final))
            if configured and not found:
                # A selector that stops matching is the most likely way this
                # scraper silently goes blind, so it is loud and it falls back.
                logger.warning("%s: item_selector %r matched nothing on %s — "
                               "falling back to heuristic",
                               self.target.key, self.target.config["item_selector"], final)
                found = self._from_heuristic(soup, final)
            # A `faelles` site lists the same article under both URLs; keep the
            # first sighting so its kind/date are not overwritten by the second.
            for article in found:
                key = article.id
                if key not in seen:
                    seen.add(key)
                    out.append(article)
        return out
