"""sitemap.xml discovery — the near-universal channel.

Every one of the 13 reachable sites in the survey served a ``sitemap.xml``, which
makes this the only channel that works almost everywhere. Two consequences shape
the implementation:

- A sitemap is **site-wide**, not news-scoped, so URLs must be filtered to a
  path prefix. Get that prefix wrong and you either miss everything or try to
  ingest the whole website.
- ``<lastmod>`` is the *only* change signal a sitemap-only site offers, so it is
  carried into ``ListedArticle.updated_at`` and folded into the listing
  fingerprint.

Sitemap *indexes* are followed one level, breadth-first, and every limit is
explicit: these files reach 725 KB (Holstebro) and a naive recursive crawl of a
mis-detected index is how a scraper ends up fetching a whole municipal website.
"""
from __future__ import annotations

import contextlib
import logging
import re
from urllib.parse import urljoin, urlparse
from xml.etree import ElementTree

from nbkommune.dates import parse_danish_datetime
from nbkommune.http import HttpClient
from nbkommune.records import ListedArticle, looks_like_document
from nbkommune.targets import Target

logger = logging.getLogger(__name__)

_SM_NS = "{http://www.sitemaps.org/schemas/sitemap/0.9}"

# How many child sitemaps of an index to follow, and how many URLs to keep in
# total. Both are hard caps, and exceeding either is logged rather than silently
# truncated — a scraper that quietly stops at N looks like it covered everything.
MAX_CHILD_SITEMAPS = 12
MAX_URLS = 5000

# A URL whose last path segment looks like an article slug rather than a section
# index: several words joined by hyphens. Used only to break ties when a prefix
# match alone would sweep in section pages.
_SLUG = re.compile(r"[a-z0-9æøå]+(?:-[a-z0-9æøå]+){1,}$", re.I)


class SitemapSource:
    """Lists articles from a sitemap (or sitemap index), filtered by prefix."""

    channel = "sitemap"

    def __init__(self, target: Target, http: HttpClient, sitemap_url: str,
                 url_prefix: str | list[str] | None = None) -> None:
        self.target = target
        self.http = http
        self.sitemap_url = sitemap_url
        # Fall back to the news path from the registry; without any prefix we
        # would treat every page on the site as an article.
        configured = (url_prefix
                      or target.config.get("url_prefixes")
                      or target.config.get("url_prefix")
                      or urlparse(target.news_url or target.press_url or "").path
                      or "")
        values = [configured] if isinstance(configured, str) else list(configured)
        self.url_prefixes = [value.rstrip("/") for value in values if value]
        # Kept as a convenience for logs and callers written before multiple
        # prefixes were supported.
        self.url_prefix = self.url_prefixes[0] if self.url_prefixes else ""

    @property
    def detail(self) -> str:
        prefixes = ",".join(self.url_prefixes) or "/"
        return f"{self.sitemap_url} (prefix={prefixes})"

    @property
    def resolved_config(self) -> dict:
        """Everything needed to rebuild this source without probing again."""
        config: dict = {"sitemap_url": self.sitemap_url}
        if len(self.url_prefixes) > 1:
            config["url_prefixes"] = self.url_prefixes
        else:
            config["url_prefix"] = self.url_prefix
        return config

    def _parse(self, content: bytes) -> ElementTree.Element | None:
        try:
            return ElementTree.fromstring(content)
        except ElementTree.ParseError as exc:
            logger.warning("unparseable sitemap %s: %s", self.sitemap_url, exc)
            return None

    def _entries(self, root: ElementTree.Element) -> list[tuple[str, str | None]]:
        """``(loc, lastmod)`` pairs from a ``<urlset>``."""
        out: list[tuple[str, str | None]] = []
        for url_el in root.findall(f"{_SM_NS}url"):
            loc = url_el.findtext(f"{_SM_NS}loc")
            if not loc:
                continue
            out.append((loc.strip(), (url_el.findtext(f"{_SM_NS}lastmod") or "").strip() or None))
        return out

    def _child_sitemaps(self, root: ElementTree.Element) -> list[str]:
        out = []
        for sm in root.findall(f"{_SM_NS}sitemap"):
            loc = sm.findtext(f"{_SM_NS}loc")
            if loc:
                out.append(loc.strip())
        return out

    def _matches(self, url: str) -> bool:
        if not self.url_prefixes:
            return True
        path = urlparse(url).path.rstrip("/").lower()
        # A prefix matches complete path segments only, and the section index
        # itself is not an article.
        return any(path.startswith(prefix.lower() + "/") for prefix in self.url_prefixes)

    def list_articles(self) -> list[ListedArticle]:
        content, _ = self.http.get_bytes(self.sitemap_url)
        root = self._parse(content)
        if root is None:
            return []

        pairs = self._entries(root)
        children = self._child_sitemaps(root)
        if children:
            # A sitemap index. Prefer children whose own URL hints at news, so a
            # site splitting by section costs one fetch instead of twelve.
            ranked = sorted(
                children,
                key=lambda u: (0 if re.search(r"nyhed|news|presse|article", u, re.I) else 1, u),
            )
            if len(ranked) > MAX_CHILD_SITEMAPS:
                logger.warning(
                    "%s: sitemap index lists %d children, following the first %d "
                    "(news-looking first) — coverage may be incomplete",
                    self.target.key, len(ranked), MAX_CHILD_SITEMAPS)
            for child_url in ranked[:MAX_CHILD_SITEMAPS]:
                try:
                    child_content, _ = self.http.get_bytes(child_url)
                except Exception as exc:
                    logger.warning("%s: child sitemap %s failed: %s",
                                   self.target.key, child_url, exc)
                    continue
                child_root = self._parse(child_content)
                if child_root is not None:
                    pairs.extend(self._entries(child_root))
                if len(pairs) >= MAX_URLS:
                    logger.warning("%s: hit the %d-URL sitemap cap — stopping early",
                                   self.target.key, MAX_URLS)
                    break

        out: list[ListedArticle] = []
        for loc, lastmod in pairs:
            url = urljoin(self.sitemap_url, loc)
            if not self._matches(url) or looks_like_document(url):
                continue
            out.append(ListedArticle(
                url=url,
                # A sitemap states no title and no publication date. lastmod is a
                # *modification* time, so it must not become published_at —
                # extraction fills that in from the article page.
                updated_at=parse_danish_datetime(lastmod),
                channel=self.channel,
                raw={"sitemap": self.sitemap_url, "lastmod": lastmod},
            ))
        # Prefer slug-shaped URLs when the prefix match is broad: they are the
        # articles, the rest are usually section or pagination pages.
        slugged = [a for a in out if _SLUG.search(urlparse(a.url).path.rstrip("/").split("/")[-1])]
        return slugged or out


def probe_sitemap_url(http: HttpClient, site_url: str) -> str | None:
    """Find a usable sitemap: ``/sitemap.xml`` first, then robots.txt's own.

    robots.txt is checked second rather than first because it is often stale on
    these sites, while ``/sitemap.xml`` is served by every CMS in the survey.
    """
    root = site_url.rstrip("/") + "/"
    for path in ("sitemap.xml", "sitemap_index.xml", "sitemap-index.xml"):
        url = urljoin(root, path)
        try:
            content, final = http.get_bytes(url)
        except Exception:
            continue
        if b"<urlset" in content[:4096] or b"<sitemapindex" in content[:4096]:
            return final
    with contextlib.suppress(Exception):
        # A site with no reachable robots.txt simply has no sitemap we can find
        # this way; the caller falls back to another channel.
        content, _ = http.get_bytes(urljoin(root, "robots.txt"))
        for line in content.decode("utf-8", "replace").splitlines():
            if line.lower().startswith("sitemap:"):
                return line.split(":", 1)[1].strip()
    return None
