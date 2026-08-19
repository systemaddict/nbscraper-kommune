"""Source factory — pick (or discover) the discovery channel for a target.

A *source* is anything exposing ``channel``, ``detail`` and
``list_articles() -> list[ListedArticle]``. All three build the same records, so
an article lands identically in the DB regardless of how it was found.

``channel: "auto"`` resolves at crawl time and the winner is recorded on the
municipality row, so an operator can see what each of 98 sites actually resolved
to.

**Resolution order is feed → listing → sitemap**, which is by *metadata quality*
rather than by reliability, and the measurements say that is the right trade:

- A **feed** gives title, link and a real ``datePublished`` in one request.
- A **listing** gives the title and, on many sites, the only real publication
  date that exists anywhere — most of these CMSes expose merely a "sidst
  opdateret" stamp on the article page itself.
- A **sitemap** gives neither a title nor a publication date, only ``<lastmod>``.
  It is also not the coverage win it looks like: Skanderborg's sitemap lists 7
  URLs under ``/nyheder`` where its listing page yields 12+.

So the sitemap is the *fallback* for sites whose listing markup defeats the
heuristic, not the preferred channel. It stays valuable precisely there: every
reachable site in the survey served one.
"""
from __future__ import annotations

import logging
from typing import Union
from urllib.parse import urlparse

from nbkommune.http import HttpClient
from nbkommune.sources.feed import FeedSource, discover_feed_url, probe_feed_url
from nbkommune.sources.listing import ListingSource
from nbkommune.sources.sitemap import SitemapSource, probe_sitemap_url
from nbkommune.targets import CHANNELS, Target

logger = logging.getLogger(__name__)

Source = Union[FeedSource, SitemapSource, ListingSource]


def make_source(target: Target, http: HttpClient, *,
                resolved: tuple[str, dict] | None = None) -> Source:
    """Return the source for ``target``'s channel, resolving ``auto`` if needed.

    ``resolved`` is a previously stored ``(channel, config)`` from an earlier
    resolution. Passing it skips the probing entirely — see ``resolve_source``
    for why that matters: a fresh resolution costs up to a dozen requests, and
    re-paying that for 98 sites on every discovery pass is both slow and rude.
    An explicit channel in the registry always wins over a stored one.
    """
    channel = (target.channel or "auto").strip().lower()
    config = dict(target.config)
    if channel == "auto" and resolved:
        stored_channel, stored_config = resolved
        if stored_channel in CHANNELS and stored_channel != "auto":
            channel = stored_channel
            # Registry config still wins: a hand-written selector must not be
            # overruled by what an old automatic resolution happened to find.
            config = {**stored_config, **config}

    if channel == "feed":
        feed_url = config.get("feed_url")
        if not feed_url:
            raise ValueError(f"target {target.key!r} uses channel 'feed' "
                             "but config.feed_url is not set")
        return FeedSource(target, http, feed_url)

    if channel == "sitemap":
        sitemap_url = config.get("sitemap_url") or (
            (target.site_url or "").rstrip("/") + "/sitemap.xml")
        prefix = config.get("url_prefixes") or config.get("url_prefix")
        return SitemapSource(target, http, sitemap_url, prefix)

    if channel == "listing":
        return ListingSource(target, http, urls=config.get("listing_urls") or None)

    if channel != "auto":
        raise ValueError(f"unknown channel {target.channel!r} for target {target.key!r}")
    return resolve_source(target, http)


def resolve_source(target: Target, http: HttpClient) -> Source:
    """Sniff the best available channel for a target.

    Costs one listing fetch (reused across the feed and listing attempts) plus at
    most a couple of probes. Falling back to ``listing`` unconditionally at the
    end is deliberate: if its page cannot be fetched either, that must surface as
    a task failure with a real error rather than as a silent empty crawl.
    """
    listing_urls = target.listing_urls
    prefetched: dict[str, str] = {}
    listing_html: str | None = None
    listing_final: str | None = None

    if listing_urls:
        try:
            listing_html, listing_final = http.get_text(listing_urls[0])
            prefetched[listing_urls[0]] = listing_html
        except Exception as exc:
            logger.warning("%s: could not read listing %s while resolving: %s",
                           target.key, listing_urls[0], exc)

    # Speculative probing is cheap directly and ruinous through the proxy: eight
    # conventional feed paths plus three sitemap paths, each a proxied round trip
    # of tens of seconds, is enough to time a discovery pass out. For a proxied
    # host we resolve from the page already in hand and nothing else.
    host = urlparse(target.site_url or (listing_urls[0] if listing_urls else "")).netloc
    may_probe = not (host and http.is_proxied(host))
    if not may_probe:
        logger.info("%s: %s is proxied — resolving without speculative probing",
                    target.key, host)

    # 1. A feed — the only channel that states a real publication date.
    if listing_html and listing_final:
        feed_url = discover_feed_url(listing_html, listing_final)
        if feed_url:
            logger.info("%s: resolved to advertised feed %s", target.key, feed_url)
            return FeedSource(target, http, feed_url)
    if target.site_url and may_probe:
        feed_url = probe_feed_url(http, target.site_url)
        if feed_url:
            return FeedSource(target, http, feed_url)

    # 2. The listing page — titles, and usually the only publication dates going.
    if listing_html:
        candidate = ListingSource(target, http, prefetched=prefetched)
        try:
            found = candidate.list_articles()
        except Exception as exc:
            logger.warning("%s: listing scrape unusable: %s", target.key, exc)
            found = []
        if found:
            dated = sum(1 for a in found if a.published_at)
            logger.info("%s: resolved to listing scrape (%d articles, %d dated)",
                        target.key, len(found), dated)
            return candidate
        logger.info("%s: listing page yielded no articles — trying sitemap",
                    target.key)

    # 3. A sitemap, filtered to the news prefix. Only accepted if it actually
    #    yields articles: a sitemap matching nothing is worse than a listing,
    #    because it fails silently rather than loudly.
    if target.site_url and may_probe:
        sitemap_url = probe_sitemap_url(http, target.site_url)
        if sitemap_url:
            prefix = target.config.get("url_prefixes") or target.config.get("url_prefix")
            candidate = SitemapSource(target, http, sitemap_url, prefix)
            try:
                found = candidate.list_articles()
            except Exception as exc:
                logger.warning("%s: sitemap %s unusable: %s", target.key, sitemap_url, exc)
                found = []
            if found:
                logger.info("%s: resolved to sitemap %s (%d URLs under prefix)",
                            target.key, sitemap_url, len(found))
                return candidate
            logger.info("%s: sitemap %s matched no articles under prefix %r",
                        target.key, sitemap_url, candidate.url_prefix)

    logger.warning("%s: no channel yielded articles — falling back to listing so "
                   "the failure is visible", target.key)
    return ListingSource(target, http, prefetched=prefetched)
