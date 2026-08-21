"""The three discovery channels, and the crawl's new/changed/pending decision."""
from __future__ import annotations

from datetime import UTC, datetime

import httpx
import respx

from nbkommune.crawl import _decide, _legacy_published_is_untrusted
from nbkommune.http import HttpClient
from nbkommune.records import ListedArticle
from nbkommune.settings import Settings
from nbkommune.sources.feed import FeedSource
from nbkommune.sources.listing import ListingSource
from nbkommune.sources.sitemap import SitemapSource
from nbkommune.targets import Target, registry


def _settings(**kw) -> Settings:
    return Settings(_env_file=None, respect_robots=False,
                    scrape_min_interval_s=0.0, http_max_retries=1, **kw)


def _target(**kw) -> Target:
    base = dict(key="testby", name="Testby", site_url="https://testby.dk",
                news_url="https://testby.dk/nyheder")
    return Target(**{**base, **kw}).normalised()


# ── feed ─────────────────────────────────────────────────────────────────────
RSS = """<?xml version="1.0" encoding="UTF-8"?>
<rss version="2.0"><channel><title>Nyheder</title>
<item><title>En rigtig nyhed</title>
  <link>https://testby.dk/nyheder/en-rigtig-nyhed</link>
  <description>Resume her.</description>
  <pubDate>Mon, 17 Aug 2026 14:46:00 +0200</pubDate></item>
<item><title>Budgetprocedure 2027-2030.pdf</title>
  <link>https://testby.dk/media/budget.pdf</link>
  <pubDate>Tue, 18 Aug 2026 06:00:01 +0200</pubDate></item>
<item><title>Postliste 30-07-2026.pdf</title>
  <link>https://testby.dk/da/node/9</link>
  <pubDate>Mon, 10 Aug 2026 06:45:01 +0200</pubDate></item>
</channel></rss>
"""


class TestFeedSource:
    @respx.mock
    def test_parses_entries_with_real_publication_dates(self):
        respx.get("https://testby.dk/rss.xml").mock(
            return_value=httpx.Response(200, text=RSS,
                                        headers={"content-type": "application/rss+xml"}))
        with HttpClient(_settings()) as http:
            found = FeedSource(_target(), http, "https://testby.dk/rss.xml").list_articles()
        assert len(found) == 1
        assert found[0].title == "En rigtig nyhed"
        assert found[0].published_at == "2026-08-17T12:46:00+00:00"
        assert found[0].channel == "feed"

    @respx.mock
    def test_pdf_entries_are_skipped(self):
        """Fredericia's Drupal RSS lists PDFs and postlists as news entries — one
        by URL, one only by title."""
        respx.get("https://testby.dk/rss.xml").mock(
            return_value=httpx.Response(200, text=RSS))
        with HttpClient(_settings()) as http:
            urls = [a.url for a in
                    FeedSource(_target(), http, "https://testby.dk/rss.xml").list_articles()]
        assert "https://testby.dk/media/budget.pdf" not in urls
        assert "https://testby.dk/da/node/9" not in urls

    @respx.mock
    def test_sitewide_feed_is_limited_to_configured_news_prefix(self):
        rss = """<?xml version="1.0"?><rss version="2.0"><channel>
        <title>Hele sitet</title>
        <item><title>Nyhed</title><link>https://testby.dk/nyheder/rigtig-nyhed</link>
          <pubDate>Mon, 17 Aug 2026 14:46:00 +0200</pubDate></item>
        <item><title>Gammel infoside</title><link>https://testby.dk/borger/infoside</link>
          <pubDate>Mon, 01 Jan 0001 00:00:00 +0000</pubDate></item>
        </channel></rss>"""
        respx.get("https://testby.dk/rss").mock(return_value=httpx.Response(200, text=rss))
        target = _target(config={"url_prefix": "/nyheder"})
        with HttpClient(_settings()) as http:
            found = FeedSource(target, http, "https://testby.dk/rss").list_articles()
        assert [article.title for article in found] == ["Nyhed"]


# ── sitemap ──────────────────────────────────────────────────────────────────
SITEMAP = """<?xml version="1.0" encoding="UTF-8"?>
<urlset xmlns="http://www.sitemaps.org/schemas/sitemap/0.9">
 <url><loc>https://testby.dk/nyheder/foerste-nyhed</loc>
      <lastmod>2026-08-18T10:00:00+02:00</lastmod></url>
 <url><loc>https://testby.dk/nyheder/anden-nyhed</loc>
      <lastmod>2026-08-17</lastmod></url>
 <url><loc>https://testby.dk/nyheder</loc></url>
 <url><loc>https://testby.dk/borger/affald</loc></url>
 <url><loc>https://testby.dk/nyheder/vedhaeftet-fil.pdf</loc></url>
</urlset>
"""

SITEMAP_INDEX = """<?xml version="1.0" encoding="UTF-8"?>
<sitemapindex xmlns="http://www.sitemaps.org/schemas/sitemap/0.9">
 <sitemap><loc>https://testby.dk/sitemap-borger.xml</loc></sitemap>
 <sitemap><loc>https://testby.dk/sitemap-nyheder.xml</loc></sitemap>
</sitemapindex>
"""


class TestSitemapSource:
    @respx.mock
    def test_filters_to_the_news_prefix(self):
        respx.get("https://testby.dk/sitemap.xml").mock(
            return_value=httpx.Response(200, text=SITEMAP))
        with HttpClient(_settings()) as http:
            found = SitemapSource(_target(), http, "https://testby.dk/sitemap.xml",
                                  "/nyheder").list_articles()
        urls = [a.url for a in found]
        assert "https://testby.dk/nyheder/foerste-nyhed" in urls
        assert "https://testby.dk/borger/affald" not in urls, "outside the prefix"
        assert "https://testby.dk/nyheder" not in urls, "the section index is not an article"
        assert "https://testby.dk/nyheder/vedhaeftet-fil.pdf" not in urls

    @respx.mock
    def test_lastmod_is_updated_not_published(self):
        """A sitemap states a *modification* time. Promoting it to published_at
        would date the whole corpus by when the CMS last touched it."""
        respx.get("https://testby.dk/sitemap.xml").mock(
            return_value=httpx.Response(200, text=SITEMAP))
        with HttpClient(_settings()) as http:
            found = SitemapSource(_target(), http, "https://testby.dk/sitemap.xml",
                                  "/nyheder").list_articles()
        first = next(a for a in found if a.url.endswith("foerste-nyhed"))
        assert first.updated_at == "2026-08-18T08:00:00+00:00"
        assert first.published_at is None

    @respx.mock
    def test_multiple_prefixes_cover_news_and_press_releases(self):
        sitemap = SITEMAP.replace(
            "https://testby.dk/borger/affald",
            "https://testby.dk/presse/foerste-pressemeddelelse",
        )
        respx.get("https://testby.dk/sitemap.xml").mock(
            return_value=httpx.Response(200, text=sitemap))
        with HttpClient(_settings()) as http:
            found = SitemapSource(
                _target(), http, "https://testby.dk/sitemap.xml",
                ["/nyheder", "/presse"],
            ).list_articles()
        urls = [article.url for article in found]
        assert "https://testby.dk/nyheder/foerste-nyhed" in urls
        assert "https://testby.dk/presse/foerste-pressemeddelelse" in urls

    @respx.mock
    def test_follows_a_sitemap_index_news_child_first(self):
        respx.get("https://testby.dk/sitemap.xml").mock(
            return_value=httpx.Response(200, text=SITEMAP_INDEX))
        news = respx.get("https://testby.dk/sitemap-nyheder.xml").mock(
            return_value=httpx.Response(200, text=SITEMAP))
        respx.get("https://testby.dk/sitemap-borger.xml").mock(
            return_value=httpx.Response(200, text=SITEMAP))
        with HttpClient(_settings()) as http:
            found = SitemapSource(_target(), http, "https://testby.dk/sitemap.xml",
                                  "/nyheder").list_articles()
        assert news.called
        assert found

    @respx.mock
    def test_malformed_xml_yields_nothing_rather_than_raising(self):
        respx.get("https://testby.dk/sitemap.xml").mock(
            return_value=httpx.Response(200, text="<urlset><url><loc>broken"))
        with HttpClient(_settings()) as http:
            assert SitemapSource(_target(), http, "https://testby.dk/sitemap.xml",
                                 "/nyheder").list_articles() == []


# ── listing ──────────────────────────────────────────────────────────────────
LISTING = """
<html><body><main>
 <article class="teaser">
   <a href="/nyheder/skole-er-aabnet">
     <img alt="Skole er åbnet"><h3>Skole er åbnet</h3>
   </a>
   <time datetime="2026-08-14T09:00:00+02:00">14. august 2026</time>
 </article>
 <article class="teaser">
   <a href="/nyheder/cykelsti-indviet"><img alt="Cykelsti indviet"></a>
   <span class="date">18. august 2026</span>
 </article>
 <a href="/nyheder">Alle nyheder</a>
 <a href="/nyheder/side/2">Næste side</a>
 <a href="/borger/affald-og-genbrug">Affald</a>
 <a href="https://facebook.com/testby/noget-her">Facebook</a>
</main></body></html>
"""


class TestListingSource:
    def _found(self, target=None):
        target = target or _target()
        source = ListingSource(target, None, urls=["https://testby.dk/nyheder"],
                               prefetched={"https://testby.dk/nyheder": LISTING})
        return source.list_articles()

    def test_finds_article_links_below_the_listing_path(self):
        urls = [a.url for a in self._found()]
        assert "https://testby.dk/nyheder/skole-er-aabnet" in urls
        assert "https://testby.dk/nyheder/cykelsti-indviet" in urls

    def test_excludes_pagination_sections_and_offsite(self):
        urls = [a.url for a in self._found()]
        assert "https://testby.dk/nyheder/side/2" not in urls
        assert "https://testby.dk/nyheder" not in urls
        assert "https://testby.dk/borger/affald-og-genbrug" not in urls
        assert not any("facebook.com" in u for u in urls)

    def test_title_is_not_doubled_by_an_image_alt(self):
        """An anchor wrapping both an <img alt> and a heading yields
        "Headline Headline" if the anchor's raw text is used."""
        article = next(a for a in self._found() if a.url.endswith("skole-er-aabnet"))
        assert article.title == "Skole er åbnet"

    def test_image_only_anchor_still_gets_a_title(self):
        article = next(a for a in self._found() if a.url.endswith("cykelsti-indviet"))
        assert article.title == "Cykelsti indviet"

    def test_unconfigured_time_is_not_assumed_to_be_publication_date(self):
        article = next(a for a in self._found() if a.url.endswith("skole-er-aabnet"))
        assert article.published_at is None

    def test_bare_date_text_is_not_assumed_to_be_publication_date(self):
        article = next(a for a in self._found() if a.url.endswith("cykelsti-indviet"))
        assert article.published_at is None

    def test_configured_date_selector_is_used(self):
        target = _target(config={"item_selector": "article.teaser",
                                 "link_selector": "a",
                                 "date_selector": "time"})
        article = next(a for a in self._found(target)
                       if a.url.endswith("skole-er-aabnet"))
        assert article.published_at == "2026-08-14T07:00:00+00:00"

    def test_configured_selectors_are_used(self):
        target = _target(config={"item_selector": "article.teaser",
                                 "link_selector": "a",
                                 "title_selector": "h3"})
        found = self._found(target)
        assert found[0].title == "Skole er åbnet"
        assert found[0].raw["mode"] == "configured"

    def test_nested_date_is_not_appended_to_configured_title(self):
        html = """<article class='teaser'><a href='/nyheder/en-rigtig-nyhed'>
          <h2>En rigtig nyhed<small>18.8.2026 | Pressemeddelelse</small></h2>
        </a></article>"""
        target = _target(config={
            "item_selector": "article.teaser",
            "link_selector": "a",
            "title_selector": "h2",
            "date_selector": "h2 > small",
        })
        source = ListingSource(
            target,
            None,
            urls=["https://testby.dk/nyheder"],
            prefetched={"https://testby.dk/nyheder": html},
        )
        article = source.list_articles()[0]
        assert article.title == "En rigtig nyhed"
        assert article.published_at == "2026-08-17T22:00:00+00:00"

    def test_a_selector_that_stops_matching_falls_back(self):
        """The most likely way this scraper goes blind is a site changing its
        markup, so a dead selector must fall back rather than return nothing."""
        target = _target(config={"item_selector": ".does-not-exist"})
        assert self._found(target)

    def test_current_year_archive_url_is_expanded(self):
        target = _target(config={
            "listing_urls": ["https://testby.dk/nyheder/{year}"],
            "listing_years": 2,
        })
        source = ListingSource(target, None, urls=target.config["listing_urls"])
        year = datetime.now(UTC).year
        assert source.urls == [
            f"https://testby.dk/nyheder/{year}",
            f"https://testby.dk/nyheder/{year - 1}",
        ]

    @respx.mock
    def test_configured_json_listing_is_paginated_and_mapped(self):
        first = {
            "success": True,
            "totalPages": 2,
            "news": [{
                "id": 21684,
                "title": "En ny børnehave er åbnet",
                "manchet": "Et kort resumé.",
                "url": "/nyheder/2026/en-ny-boernehave-er-aabnet/",
                "date": "19. august 2026",
            }],
        }
        second = {
            "success": True,
            "totalPages": 2,
            "news": [{
                "id": 21620,
                "title": "Ny cykelsti",
                "manchet": "Endnu et resumé.",
                "url": "/nyheder/2026/ny-cykelsti/",
                "date": "7. august 2026",
            }],
        }
        page_one = respx.get("https://testby.dk/api/news-list", params={
            "nodeId": "1125", "tagId": "0", "page": "1",
        }).mock(return_value=httpx.Response(200, json=first))
        page_two = respx.get("https://testby.dk/api/news-list", params={
            "nodeId": "1125", "tagId": "0", "page": "2",
        }).mock(return_value=httpx.Response(200, json=second))
        target = _target(config={"json_listing": {
            "url": "/api/news-list",
            "params": {"nodeId": "1125", "tagId": "0"},
            "max_pages": 5,
            "items_field": "news",
            "total_pages_field": "totalPages",
            "summary_field": "manchet",
        }})
        with HttpClient(_settings()) as http:
            found = ListingSource(target, http).list_articles()
        assert page_one.called and page_two.called
        assert [article.title for article in found] == [
            "En ny børnehave er åbnet", "Ny cykelsti",
        ]
        assert found[0].url == "https://testby.dk/nyheder/2026/en-ny-boernehave-er-aabnet/"
        assert found[0].summary == "Et kort resumé."
        assert found[0].published_at == "2026-08-18T22:00:00+00:00"
        assert found[0].raw["mode"] == "configured-json"

    def test_configured_listing_can_link_to_external_press_room(self):
        html = """<div class='press-releases__item'>
          <a href='https://press.example/message/123'>
            <p class='press-releases__item-date'>06.07.26</p>
            <h6 class='press-releases__item-header'>Ansvarlig brug af AI</h6>
            <div class='press-releases__item-description'>Et kort resume.</div>
          </a></div>"""
        target = _target(config={
            "item_selector": ".press-releases__item",
            "link_selector": "a[href]",
            "title_selector": ".press-releases__item-header",
            "summary_selector": ".press-releases__item-description",
            "date_selector": ".press-releases__item-date",
        })
        source = ListingSource(
            target, None, urls=["https://testby.dk/presse"],
            prefetched={"https://testby.dk/presse": html},
        )
        article = source.list_articles()[0]
        assert article.url == "https://press.example/message/123"
        assert article.title == "Ansvarlig brug af AI"
        assert article.summary == "Et kort resume."
        assert article.published_at == "2026-07-05T22:00:00+00:00"


# ── the discovery decision ───────────────────────────────────────────────────
class TestDecide:
    def _listed(self):
        return ListedArticle(url="https://testby.dk/nyheder/a", title="T",
                             published_at="2026-08-18T10:00:00+00:00")

    def _row(self, **kw):
        listed = self._listed()
        base = dict(status="ingested", listing_hash=listed.fingerprint(),
                    detail_hash="abc")
        return {**base, **kw}

    def test_unseen_is_new(self):
        assert _decide(None, self._listed()) == "new"

    def test_unchanged_is_nothing(self):
        assert _decide(self._row(), self._listed()) is None

    def test_moved_fingerprint_is_changed(self):
        assert _decide(self._row(listing_hash="stale"), self._listed()) == "changed"

    def test_bodyless_row_is_pending_not_new(self):
        """The enqueue cap deliberately leaves articles listed. Reporting those as
        "new" on every pass makes the counts meaningless."""
        assert _decide(self._row(status="listed", detail_hash=None),
                       self._listed()) == "pending"

    def test_tombstoned_article_that_reappears_is_changed(self):
        """A site that 404s during a migration must recover by itself."""
        assert _decide(self._row(status="gone"), self._listed()) == "changed"


class TestLegacyDateTrust:
    def test_old_html_listing_date_requires_revalidation(self):
        assert _legacy_published_is_untrusted({
            "channel": "listing",
            "published_at": "2026-08-18T10:00:00+00:00",
            "provenance_json": '{"published_at":"listing"}',
            "raw_json": "{}",
        })

    def test_old_feed_date_is_kept_and_migrated(self):
        assert not _legacy_published_is_untrusted({
            "channel": "feed",
            "published_at": "2026-08-18T10:00:00+00:00",
            "provenance_json": '{"published_at":"listing"}',
            "raw_json": "{}",
        })

    def test_reviewed_configured_listing_date_is_trusted(self):
        assert not _legacy_published_is_untrusted({
            "channel": "listing",
            "published_at": "2026-08-18T10:00:00+00:00",
            "provenance_json": '{"published_at":"listing:configured"}',
            "raw_json": "{}",
        })


ATOM_UPDATED_ONLY = """<?xml version="1.0" encoding="utf-8"?>
<feed xmlns="http://www.w3.org/2005/Atom"><title>N</title>
<entry><title>Kun opdateret</title>
  <link href="https://testby.dk/nyheder/kun-opdateret"/>
  <updated>2026-08-18T12:00:00+02:00</updated></entry>
</feed>
"""


class TestFeedUpdatedOnly:
    @respx.mock
    def test_atom_updated_does_not_become_a_publication_date(self):
        """feedparser aliases a missing `published` to `updated`. Accepting that
        would date the article by when the CMS last touched it."""
        respx.get("https://testby.dk/atom.xml").mock(
            return_value=httpx.Response(200, text=ATOM_UPDATED_ONLY))
        with HttpClient(_settings()) as http:
            found = FeedSource(_target(), http, "https://testby.dk/atom.xml").list_articles()
        assert len(found) == 1
        assert found[0].published_at is None
        assert found[0].updated_at == "2026-08-18T10:00:00+00:00"


class TestTargetUrlPreservation:
    """A trailing slash is part of the address on these sites, not decoration."""

    def test_listing_urls_keep_their_trailing_slash(self):
        t = Target(key="k", name="K", site_url="https://k.dk/",
                   news_url="https://k.dk/nyheder/",
                   press_url="https://k.dk/presse/").normalised()
        assert t.news_url == "https://k.dk/nyheder/"
        assert t.press_url == "https://k.dk/presse/"

    def test_site_url_root_is_still_tidied(self):
        t = Target(key="k", name="K", site_url="https://k.dk/").normalised()
        assert t.site_url == "https://k.dk"

    def test_faelles_site_collapses_duplicate_listing_urls(self):
        t = Target(key="k", name="K", news_url="https://k.dk/nyheder/",
                   press_url="https://k.dk/nyheder/").normalised()
        assert t.listing_urls == ["https://k.dk/nyheder/"]


class TestPublishedDateProfiles:
    def test_reviewed_profile_expands_to_rules(self):
        t = _target(config={"published_date_profile": "gopublic-data-date"})
        assert t.published_date_rules == [{
            "name": "gopublic-data-date",
            "selector": ".news-page > span.datetime.datetime-to-locale[data-date]",
            "attribute": "data-date",
        }]

    def test_unknown_profile_fails_closed(self):
        t = _target(config={"published_date_profile": "does-not-exist"})
        assert t.published_date_rules == []

    def test_registry_profiles_are_assigned_only_to_reviewed_sites(self, tmp_path):
        settings = _settings(targets_file=tmp_path / "no-overrides.json")
        reg = registry(settings)
        expected = {
            "gopublic-data-date": {
                "alleroed", "bornholm", "glostrup", "gribskov", "halsnaes",
                "helsingoer", "herning", "hjoerring", "holbaek", "kalundborg",
                "lemvig", "norddjurs", "odder", "roedovre", "solroed", "soroe",
                "stevns", "struer", "vallensbaek", "vesthimmerland", "vordingborg",
            },
            "moliri-created-meta": {
                "aeroe", "assens", "broenderslev", "egedal", "fredensborg",
                "furesoe", "hoeje-taastrup", "hoersholm", "kerteminde", "koege",
                "kolding", "laesoe", "langeland", "lolland", "silkeborg",
                "skanderborg", "vejen",
            },
            "favrskov-visible-date": {"favrskov"},
            "holstebro-visible-date": {"holstebro"},
            "hvidovre-visible-date": {"hvidovre"},
            "ishoej-visible-date": {"ishoej"},
            "koebenhavn-publication-date": {"koebenhavn"},
            "middelfart-visible-date": {"middelfart"},
            "ringkoebing-skjern-visible-date": {"ringkoebing-skjern"},
            "rudersdal-visible-date": {"rudersdal"},
            "slagelse-nuxt-news-date": {"slagelse"},
            "thisted-visible-date": {"thisted"},
        }
        actual: dict[str, set[str]] = {}
        for key, target in reg.items():
            profile = target.config.get("published_date_profile")
            if profile:
                actual.setdefault(profile, set()).add(key)
        assert actual == expected
