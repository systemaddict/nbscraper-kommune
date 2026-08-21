"""HTML listing discovery — the universal fallback.

Used when a site has no feed and its sitemap is unusable. Two modes:

- **configured**: ``item_selector`` (a CSS selector for each row) plus optional
  ``link_selector`` / ``title_selector`` / ``date_selector`` inside it. This is
  where a site-specific fix goes, and it needs no code change — the selectors
  live in ``config/targets.json``.
- **heuristic**: links whose path sits *below* the listing page's own path and
  whose last segment looks like an article slug. In the survey this found article
  links on 5 of 8 listing pages unaided; the other 3 need selectors or a browser.

Publication dates from a reviewed ``date_selector`` are valuable: for sites
whose article pages expose only a "sidst opdateret" stamp, the listing can be
the sole publication source. Heuristic listing discovery never guesses dates.
"""
from __future__ import annotations

import json
import logging
import re
from datetime import UTC, datetime
from urllib.parse import urlencode, urljoin, urlparse

from bs4 import BeautifulSoup
from bs4.element import NavigableString, Tag

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

# A few reviewed municipal listings (currently Gentofte's press room) render a
# compact Danish date such as ``06.07.26``. Keep support scoped to configured
# date selectors: accepting two-digit years during generic page extraction
# would turn unrelated version numbers into publication dates.
_DK_SHORT_YEAR = re.compile(r"^\s*(\d{1,2})[./-](\d{1,2})[./-](\d{2})\s*$")

class ListingSource:
    """Lists articles by scraping one or more listing pages."""

    channel = "listing"

    def __init__(self, target: Target, http: HttpClient,
                 urls: list[str] | None = None,
                 prefetched: dict[str, str] | None = None) -> None:
        self.target = target
        self.http = http
        self.urls = self._expand_urls(urls or target.listing_urls)
        # HTML the resolver already fetched while sniffing channels — reused so
        # channel resolution costs one request, not three.
        self.prefetched = prefetched or {}

    def _expand_urls(self, urls: list[str]) -> list[str]:
        """Expand a small current-year archive template.

        Some municipal archive roots expose only links to year sections. Herlev
        is one: the actual article cards live at ``/nyheder/2026`` while
        ``/nyheder`` contains no article links. ``{year}`` keeps that source
        working across New Year without a registry edit; ``listing_years`` can
        include the previous archive while a publication floor is settling.
        """
        current = datetime.now(UTC).year
        years = max(1, min(int(self.target.config.get("listing_years", 1)), 3))
        out: list[str] = []
        for url in urls:
            expanded = ([url.format(year=current - offset) for offset in range(years)]
                        if "{year}" in url else [url])
            for value in expanded:
                if value not in out:
                    out.append(value)
        return out

    @property
    def detail(self) -> str:
        if self.target.config.get("json_listing"):
            mode = "configured-json"
        elif self.target.config.get("html_post_listing"):
            mode = "configured-html-post"
        else:
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

    # ── configured JSON mode ───────────────────────────────────
    def _from_json_listing(self) -> list[ListedArticle]:
        """Read a paginated public JSON listing described by target config.

        Several kommune pages render an empty client-side shell while exposing
        the actual cards through a small same-origin endpoint. Field names and
        pagination differ, so the mapping stays in the registry rather than in
        site-specific Python.
        """
        cfg = self.target.config["json_listing"]
        endpoint = urljoin(self.target.site_url + "/", str(cfg["url"]))
        params = {str(key): str(value) for key, value in cfg.get("params", {}).items()}
        page_param = str(cfg.get("page_param", "page"))
        max_pages = max(1, min(int(cfg.get("max_pages", 1)), 50))
        items_field = str(cfg.get("items_field", "items"))
        total_pages_field = str(cfg.get("total_pages_field", "totalPages"))
        out: list[ListedArticle] = []

        for page in range(1, max_pages + 1):
            query = urlencode({**params, page_param: page})
            request_url = endpoint + ("&" if "?" in endpoint else "?") + query
            try:
                payload, final = self.http.get_text(request_url)
                data = json.loads(payload)
            except Exception as exc:
                logger.warning("%s: JSON listing page %d failed: %s",
                               self.target.key, page, exc)
                break
            if not isinstance(data, dict):
                logger.warning("%s: JSON listing returned a non-object on page %d",
                               self.target.key, page)
                break
            items = data.get(items_field)
            if not isinstance(items, list):
                logger.warning("%s: JSON listing field %r is not a list on page %d",
                               self.target.key, items_field, page)
                break
            for item in items:
                if not isinstance(item, dict):
                    continue
                raw_url = item.get(str(cfg.get("url_field", "url")))
                if not isinstance(raw_url, str) or not raw_url.strip():
                    continue
                url = urljoin(final, raw_url.strip())
                title = item.get(str(cfg.get("title_field", "title")))
                if looks_like_document(url, title if isinstance(title, str) else None):
                    continue
                summary = item.get(str(cfg.get("summary_field", "summary")))
                date_value = item.get(str(cfg.get("date_field", "date")))
                out.append(ListedArticle(
                    url=url,
                    title=title if isinstance(title, str) else None,
                    summary=summary if isinstance(summary, str) else None,
                    published_at=(parse_danish_datetime(date_value)
                                  if isinstance(date_value, str) else None),
                    channel=self.channel,
                    raw={"listing_url": endpoint, "mode": "configured-json",
                         "source_id": item.get(str(cfg.get("id_field", "id")))},
                ))
            total_pages = data.get(total_pages_field)
            if not items or not isinstance(total_pages, int) or page >= total_pages:
                break
        return out

    # ── configured HTML POST mode ──────────────────────────────
    def _from_html_post_listing(self) -> list[ListedArticle]:
        """Read HTML fragments from a CSRF-protected, paginated POST endpoint.

        The initial listing GET establishes the cookie and exposes the request
        token.  Card parsing then uses the same reviewed selectors as ordinary
        configured HTML listings, keeping this transport detail out of the
        extraction logic.
        """
        cfg = self.target.config["html_post_listing"]
        if not self.urls:
            return []
        shell, shell_url = self._html(self.urls[0])
        shell_soup = BeautifulSoup(shell, "lxml")
        token_selector = str(cfg.get(
            "csrf_selector", "input[name='__RequestVerificationToken']"
        ))
        token_node = shell_soup.select_one(token_selector)
        token = token_node.get("value") if token_node is not None else None
        if not isinstance(token, str) or not token.strip():
            logger.warning("%s: CSRF token selector %r matched nothing on %s",
                           self.target.key, token_selector, shell_url)
            return []

        endpoint = urljoin(shell_url, str(cfg["url"]))
        body = dict(cfg.get("body", {}))
        page_field = str(cfg.get("page_field", "page"))
        max_pages = max(1, min(int(cfg.get("max_pages", 1)), 50))
        last_selector = cfg.get("last_selector")
        csrf_header = str(cfg.get("csrf_header", "X-CSRF-Token"))
        out: list[ListedArticle] = []
        seen: set[str] = set()

        for page in range(1, max_pages + 1):
            payload = {**body, page_field: page}
            try:
                html, _ = self.http.post_json_text(
                    endpoint, payload, headers={csrf_header: token.strip()}
                )
            except Exception as exc:
                logger.warning("%s: HTML POST listing page %d failed: %s",
                               self.target.key, page, exc)
                break
            soup = BeautifulSoup(html, "lxml")
            found = self._from_selectors(soup, shell_url)
            for article in found:
                if article.id not in seen:
                    seen.add(article.id)
                    out.append(article)
            if not found or (isinstance(last_selector, str)
                             and soup.select_one(last_selector) is not None):
                break
        return out

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
            href = link["href"].strip()
            # Some XHR listings pad their last page with empty cards linked to
            # "#".  They are layout placeholders, not articles.
            if not href or href.startswith(("#", "javascript:")):
                continue
            url = urljoin(page_url, href)
            title = None
            if cfg.get("title_selector"):
                el = row.select_one(cfg["title_selector"])
                if el:
                    # Via Ritzau puts a timestamp/status in a nested <small>
                    # inside the <h2>. Prefer the heading's own text nodes when
                    # present so reviewed date markup cannot leak into title.
                    direct = " ".join(
                        str(child).strip()
                        for child in el.children
                        if isinstance(child, NavigableString) and str(child).strip()
                    )
                    title = direct or el.get_text(" ", strip=True)
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
        """A publication date from an explicitly configured selector.

        A generic listing card frequently mentions an event date or deadline.
        Treating any date-shaped text — or even an unlabelled ``<time>`` — as
        publication time silently corrupts the field, so unconfigured targets
        deliberately return no date here.
        """
        if not selector:
            return None
        candidates: list[str] = []
        el = row.select_one(selector)
        if el is not None:
            for attr in ("datetime", "content", "data-date"):
                value = el.get(attr)
                if isinstance(value, str) and value.strip():
                    candidates.append(value)
            candidates.append(el.get_text(" ", strip=True))
        for candidate in candidates:
            parsed = parse_danish_datetime(candidate)
            if parsed:
                return parsed
            short = _DK_SHORT_YEAR.match(candidate)
            if short:
                day, month, year = short.groups()
                parsed = parse_danish_datetime(f"{day}.{month}.20{year}")
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
        if self.target.config.get("json_listing"):
            return self._from_json_listing()
        if self.target.config.get("html_post_listing"):
            return self._from_html_post_listing()
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
