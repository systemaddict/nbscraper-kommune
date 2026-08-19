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

URL = "https://example.dk/nyheder/en-nyhed"


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
