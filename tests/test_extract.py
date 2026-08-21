"""Layered extraction — each layer, the merge order, and the two real DOM traps.

The fixtures are deliberately shaped after pages actually observed in the survey
rather than invented markup: a JSON-LD site (Jammerbugt), an Umbraco site whose
only timestamp is `cmspageupdated` (Lolland/Kerteminde), and a site that wraps
its whole page in `<div class="navbar">` and renders the body in bare divs with
exactly one `<p>` in the document (Skanderborg).
"""
from __future__ import annotations

from nbkommune.extract import classify_kind, extract_article
from nbkommune.records import ListedArticle

BODY = ("Faglærte medarbejdere bliver en efterspurgt gruppe de kommende år, og "
        "kommunen sætter nu ind med en ny indsats for voksenlærlinge i hele "
        "området, så flere kan komme i gang med en uddannelse mens de arbejder.")

JSONLD_PAGE = f"""
<html lang="da"><head>
<title>En nyhed</title>
<script type="application/ld+json">
{{"@context":"https://schema.org","@type":"NewsArticle",
  "headline":"Et ekstra slag for voksenlærlinge",
  "datePublished":"2026-08-18T15:00:00.000+02:00",
  "dateModified":"2026-08-18T17:25:45.000+02:00",
  "description":"Kort resume.",
  "articleSection":["Erhverv"],
  "author":{{"@type":"Person","name":"Pressechefen"}},
  "image":"/media/billede.jpg"}}
</script></head>
<body><nav><a href="/a">Menu</a></nav>
<article><h1>Et ekstra slag for voksenlærlinge</h1><p>{BODY}</p></article>
<footer>Kontakt os</footer></body></html>
"""

UMBRACO_PAGE = f"""
<html lang="da"><head>
<meta property="og:title" content="National Flagdag 2026 - Lolland Kommune">
<meta property="og:description" content="Markering af flagdagen.">
<meta property="og:image" content="https://lolland.dk/media/flag.jpg">
<meta name="cmspageupdated" content="2026-08-18 06.28">
<link rel="canonical" href="https://lolland.dk/nyheder/national-flagdag-2026">
</head>
<body><article><h1>National Flagdag 2026</h1><p>{BODY}</p></article></body></html>
"""

# The whole page inside a chrome-named wrapper, body in bare divs, one <p> total.
NAVBAR_PAGE = f"""
<html lang="da"><head>
<meta property="og:title" content="Skole og Børnehus er åbnet">
<meta name="cmspageupdated" content="2026-08-14 12:59:14">
</head><body>
<div class="navbar hidden-print">
  <div class="menu"><a href="/x">Forside</a><a href="/y">Kontakt</a></div>
  <div class="body-wrapper">
    <h1>Skole og Børnehus er åbnet</h1>
    <div>{BODY}</div>
    <div>Der er indvielsesfest den 24. august for alle interesserede borgere i
    hele kommunen, og programmet offentliggøres i næste uge på hjemmesiden.</div>
  </div>
</div><p>cookie</p></body></html>
"""

GOPUBLIC_PAGE = f"""
<html lang="da"><head><meta property="og:title" content="Ny informationsside om ulve">
</head><body><main><div class="news-page"><h1>Ny informationsside om ulve</h1>
<span>Publiceret&nbsp;</span><span class="datetime datetime-to-locale"
data-date="2026-05-08T09:01:37Z" data-format="dd-MM-yyyy">08-05-2026</span>
<div class="rich-text"><p>{BODY}</p></div></div></main></body></html>
"""

VISIBLE_DATE_PAGE = f"""
<html><body><main><div class="news-page"><h1>Lønløft til pædagogisk personale</h1>
<span class="date">23-06-2026</span><div class="rich-text"><p>{BODY}</p></div>
</div></main></body></html>
"""

FORM_WRAPPED_PAGE = f"""
<html><body><form id="form1"><nav>Menu</nav><main><div class="news-page">
<h1>Nyhed i ASP.NET-side</h1><span class="date">23-06-2026</span>
<figure><img src="/media/news.jpg" alt="Nyhed"></figure>
<div class="rich-text"><p>{BODY}</p></div></div></main></form></body></html>
"""

PAGE_DATE_META = f"""
<html><head><meta name="page_date" content="2026-05-04T13:20:20Z">
<meta property="og:updated_time" content="2026-05-04T13.51.00Z"></head>
<body><main><h1>Kystnatur kortlægges</h1><p>{BODY}</p></main></body></html>
"""

EMBEDDED_MODEL_PAGE = f"""
<html><body><div class="js-page" data-model="{{&quot;Headline&quot;:&quot;Valgresultat&quot;,
&quot;MainContent&quot;:&quot;&lt;p&gt;{BODY}&lt;/p&gt;&quot;,
&quot;ListItemDate&quot;:&quot;2026-03-24&quot;}}"></div></body></html>
"""

ARTICLE_WITH_CONTACT_CARD = f"""
<html><body><main><article><h1>Stor færgestrategi</h1>
<section><h2>Baggrund</h2><p>{BODY}</p></section>
<section itemscope itemtype="https://schema.org/LocalBusiness">
<h2 itemprop="name">Kontakt</h2><p>Ejendomme og Faciliteter</p>
<p><a href="mailto:service@example.dk">Send sikker post</a></p></section>
</article></main></body></html>
"""

URL = "https://example.dk/nyheder/en-nyhed"
GOPUBLIC_DATE_RULE = [{
    "name": "gopublic-published",
    "selector": ".news-page > span.datetime.datetime-to-locale[data-date]",
    "attribute": "data-date",
}]
VISIBLE_DATE_RULE = [{
    "name": "visible-news-date",
    "selector": ".news-page > span.date",
}]
EMBEDDED_DATE_RULE = [{
    "name": "component-list-item-date",
    "selector": ".js-page[data-model]",
    "attribute": "data-model",
    "json_key": "ListItemDate",
}]


class TestJsonLdLayer:
    def test_prefers_jsonld_for_dates_and_author(self):
        d = extract_article(JSONLD_PAGE, URL)
        assert d.published_at == "2026-08-18T13:00:00+00:00"
        assert d.updated_at == "2026-08-18T15:25:45+00:00"
        assert d.author == "Pressechefen"
        assert d.categories == ["Erhverv"]
        assert d.provenance["published_at"] == "jsonld"

    def test_relative_image_is_absolutised(self):
        d = extract_article(JSONLD_PAGE, URL)
        assert d.image_url == "https://example.dk/media/billede.jpg"

    def test_chrome_is_not_body(self):
        d = extract_article(JSONLD_PAGE, URL)
        assert "Menu" not in (d.body_text or "")
        assert "Kontakt os" not in (d.body_text or "")
        assert BODY[:40] in (d.body_text or "")


class TestMetaLayer:
    def test_og_title_site_suffix_trimmed(self):
        """og:title routinely carries " - Kommune"; the h1 and JSON-LD do not, so
        titles would not compare across layers if the suffix survived."""
        d = extract_article(UMBRACO_PAGE, URL)
        assert d.title == "National Flagdag 2026"

    def test_dotted_cmspageupdated_becomes_updated_not_published(self):
        """`cmspageupdated` is a *modified* stamp. Promoting it to published_at
        would date every article on these sites wrongly."""
        d = extract_article(UMBRACO_PAGE, URL)
        assert d.updated_at == "2026-08-18T04:28:00+00:00"
        assert d.published_at is None

    def test_canonical_url_captured(self):
        d = extract_article(UMBRACO_PAGE, URL)
        assert d.canonical_url == "https://lolland.dk/nyheder/national-flagdag-2026"


class TestOverStrippingGuard:
    """A chrome-named wrapper holding most of the page is a layout div, not chrome.

    Stripping `<div class="navbar hidden-print">` on the class match alone deleted
    83% of Skanderborg's text and produced an empty article that still looked
    like a successful ingest.
    """

    def test_body_survives_a_navbar_named_wrapper(self):
        d = extract_article(NAVBAR_PAGE, URL)
        assert d.body_text and len(d.body_text) > 200
        assert BODY[:40] in d.body_text

    def test_small_chrome_inside_it_is_still_stripped(self):
        d = extract_article(NAVBAR_PAGE, URL)
        assert "Forside" not in (d.body_text or "")
        assert "cookie" not in (d.body_text or "")

    def test_structured_office_contact_card_is_not_article_body(self):
        d = extract_article(ARTICLE_WITH_CONTACT_CARD, URL)
        assert BODY[:40] in (d.body_text or "")
        assert "Ejendomme og Faciliteter" not in (d.body_text or "")
        assert "Send sikker post" not in (d.body_text or "")


class TestDensityFallback:
    """Skanderborg's article pages contain exactly one <p> in the whole document,
    so block-tag extraction alone returns nothing."""

    def test_body_in_bare_divs_is_found(self):
        d = extract_article(NAVBAR_PAGE, URL)
        assert "indvielsesfest" in (d.body_text or "")

    def test_records_which_container_strategy_won(self):
        d = extract_article(NAVBAR_PAGE, URL)
        assert d.raw["body_container"] in ("selector", "density", "config")


class TestListingFallback:
    def test_listing_date_beats_a_modified_only_page(self):
        """For the many sites exposing only "sidst opdateret", the listing row is
        the sole source of a real publication date."""
        listed = ListedArticle(url=URL, title="Fra listen",
                               published_at="2026-08-10T06:00:00+00:00")
        d = extract_article(UMBRACO_PAGE, URL, listed=listed)
        assert d.published_at == "2026-08-10T06:00:00+00:00"
        assert d.provenance["published_at"] == "listing"

    def test_jsonld_still_beats_the_listing(self):
        listed = ListedArticle(url=URL, published_at="2020-01-01T00:00:00+00:00")
        d = extract_article(JSONLD_PAGE, URL, listed=listed)
        assert d.published_at == "2026-08-18T13:00:00+00:00"
        assert d.provenance["published_at"] == "jsonld"

    def test_feed_date_is_not_mislabelled_as_listing(self):
        listed = ListedArticle(
            url=URL,
            published_at="2026-08-10T06:00:00+00:00",
            channel="feed",
        )
        d = extract_article(UMBRACO_PAGE, URL, listed=listed)
        assert d.published_at == "2026-08-10T06:00:00+00:00"
        assert d.provenance["published_at"] == "feed"

    def test_reviewed_listing_selector_is_identifiable(self):
        listed = ListedArticle(
            url=URL,
            published_at="2026-08-10T06:00:00+00:00",
            channel="listing",
            raw={"mode": "configured"},
        )
        d = extract_article(UMBRACO_PAGE, URL, listed=listed)
        assert d.provenance["published_at"] == "listing:configured"

    def test_reviewed_json_listing_date_is_identifiable(self):
        listed = ListedArticle(
            url=URL,
            published_at="2026-08-10T06:00:00+00:00",
            channel="listing",
            raw={"mode": "configured-json"},
        )
        d = extract_article(UMBRACO_PAGE, URL, listed=listed)
        assert d.published_at == "2026-08-10T06:00:00+00:00"
        assert d.provenance["published_at"] == "listing:configured"


class TestDomPublicationDates:
    def test_data_date_beside_published_label(self):
        d = extract_article(GOPUBLIC_PAGE, URL,
                            published_date_rules=GOPUBLIC_DATE_RULE)
        assert d.published_at == "2026-05-08T09:01:37+00:00"
        assert d.provenance["published_at"] == "rule:gopublic-published"

    def test_visible_date_immediately_after_heading(self):
        d = extract_article(VISIBLE_DATE_PAGE, URL,
                            published_date_rules=VISIBLE_DATE_RULE)
        assert d.published_at == "2026-06-22T22:00:00+00:00"
        assert d.provenance["published_at"] == "rule:visible-news-date"

    def test_dom_candidate_is_diagnostic_without_explicit_rule(self):
        d = extract_article(GOPUBLIC_PAGE, URL)
        assert d.published_at is None
        assert d.raw["date_candidates"]

    def test_body_event_date_is_not_publication_date(self):
        html = ("<html><body><main><h1>Invitation</h1>"
                f"<article><p>{BODY}</p><p class='date'>Mødet er 23-06-2026</p>"
                "</article></main></body></html>")
        assert extract_article(html, URL).published_at is None

    def test_page_date_meta_and_updated_time_are_classified(self):
        d = extract_article(PAGE_DATE_META, URL)
        assert d.published_at == "2026-05-04T13:20:20+00:00"
        assert d.updated_at == "2026-05-04T13:51:00+00:00"
        assert d.provenance["published_at"] == "meta:page_date"
        assert d.provenance["updated_at"] == "meta:og:updated_time"

    def test_page_wide_form_does_not_delete_article_or_date(self):
        d = extract_article(FORM_WRAPPED_PAGE, URL,
                            published_date_rules=VISIBLE_DATE_RULE)
        assert d.published_at == "2026-06-22T22:00:00+00:00"
        assert BODY[:40] in (d.body_text or "")
        assert d.image_url == "https://example.dk/media/news.jpg"

    def test_embedded_component_model_publication_date(self):
        d = extract_article(EMBEDDED_MODEL_PAGE, URL,
                            published_date_rules=EMBEDDED_DATE_RULE)
        assert d.published_at == "2026-03-23T23:00:00+00:00"
        assert d.provenance["published_at"] == "rule:component-list-item-date"

    def test_ambiguous_explicit_rule_is_rejected(self):
        html = ("<main><h1>Nyhed</h1><span class='date'>01-02-2026</span>"
                "<span class='date'>03-04-2026</span><p>" + BODY + "</p></main>")
        d = extract_article(
            html, URL,
            published_date_rules=[{"name": "dates", "selector": "span.date"}],
        )
        assert d.published_at is None
        assert d.raw["date_rule_diagnostics"][0]["status"] == "ambiguous"

    def test_configured_script_field_is_supported(self):
        html = ("<main><h1>Nyhed</h1><p>" + BODY + "</p></main>"
                '<script>window.__PAGE__={newsDate:"2026-08-18T08:09:45Z"}</script>')
        d = extract_article(
            html, URL,
            published_date_rules=[{
                "name": "nuxt-news-date",
                "pattern": r'newsDate:\s*"([^"]+)"',
            }],
        )
        assert d.published_at == "2026-08-18T08:09:45+00:00"
        assert d.provenance["published_at"] == "rule:nuxt-news-date"


class TestRobustness:
    def test_malformed_jsonld_does_not_raise(self):
        html = ('<html><head><script type="application/ld+json">{not json,,</script>'
                f'</head><body><h1>T</h1><p>{BODY}</p></body></html>')
        d = extract_article(html, URL)
        assert d.title == "T"

    def test_empty_document_yields_empty_detail(self):
        d = extract_article("", URL)
        assert d.title is None and not d.body_text

    def test_fragment_without_html_element_does_not_raise(self):
        d = extract_article("<p>hej</p>", URL)
        assert d is not None


class TestKindClassification:
    def test_listing_label_wins(self):
        assert classify_kind("https://x.dk/nyheder/a", [], listed_kind="pressemeddelelse") \
            == "pressemeddelelse"

    def test_press_path_detected(self):
        assert classify_kind("https://x.dk/presse/pressemeddelelser/a", []) \
            == "pressemeddelelse"

    def test_press_category_detected(self):
        assert classify_kind("https://x.dk/n/a", ["Pressemeddelelse"]) == "pressemeddelelse"

    def test_plain_news_default(self):
        assert classify_kind("https://x.dk/nyheder/a", ["Kultur"]) == "nyhed"
