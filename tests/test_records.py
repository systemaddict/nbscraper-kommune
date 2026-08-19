"""Article identity and change detection."""
from __future__ import annotations

from nbkommune.records import (
    ArticleDetail,
    ListedArticle,
    article_id,
    collapse_ws,
    looks_like_document,
    normalise_url,
    scrub_text,
)


class TestUrlIdentity:
    def test_tracking_params_stripped(self):
        assert (normalise_url("https://kk.dk/nyheder/slug?utm_source=x&a=1")
                == "https://kk.dk/nyheder/slug?a=1")

    def test_fragment_and_trailing_slash_dropped(self):
        assert (normalise_url("https://kk.dk/nyheder/slug/#top")
                == "https://kk.dk/nyheder/slug")

    def test_host_lowercased_but_path_preserved(self):
        """Several of these CMSes serve case-sensitive slugs, so the path must
        survive verbatim while the host is folded."""
        assert (normalise_url("HTTPS://WWW.KK.DK/Nyheder/Slug")
                == "https://www.kk.dk/Nyheder/Slug")

    def test_same_article_shared_with_a_campaign_tag_is_one_id(self):
        a = article_id("https://kk.dk/nyheder/slug/")
        b = article_id("https://kk.dk/nyheder/slug?utm_medium=social&fbclid=xyz")
        assert a == b

    def test_different_articles_differ(self):
        assert article_id("https://kk.dk/n/a") != article_id("https://kk.dk/n/b")


class TestListingFingerprint:
    def _listed(self, **kw):
        base = dict(url="https://kk.dk/n/slug", title="Titel",
                    published_at="2026-08-18T10:00:00+00:00")
        return ListedArticle(**{**base, **kw})

    def test_whitespace_only_title_change_is_not_a_change(self):
        """The same headline rendered with different indentation on the listing
        and the detail page must not read as an edit."""
        assert (self._listed(title="  Titel   her ").fingerprint()
                == self._listed(title="Titel her").fingerprint())

    def test_title_change_is_a_change(self):
        assert self._listed(title="Ny titel").fingerprint() != self._listed().fingerprint()

    def test_lastmod_bump_is_a_change(self):
        """For a sitemap-only site, <lastmod> is the only change signal there is."""
        assert (self._listed(updated_at="2026-08-19T00:00:00+00:00").fingerprint()
                != self._listed().fingerprint())

    def test_rotating_teaser_is_not_a_change(self):
        """Summary is excluded on purpose: some sites rotate the teaser text,
        which would otherwise mark every article changed on every pass."""
        assert (self._listed(summary="et teaser").fingerprint()
                == self._listed(summary="et helt andet teaser").fingerprint())


class TestDetailFingerprint:
    def _detail(self, **kw):
        base = dict(url="https://kk.dk/n/s", title="T", summary="S", body_text="Krop",
                    body_html="<p>Krop</p>", published_at="2026-08-18T10:00:00+00:00",
                    updated_at=None, image_url=None, author=None)
        return ArticleDetail(**{**base, **kw})

    def test_body_change_is_content_change(self):
        assert self._detail(body_text="Andet").fingerprint() != self._detail().fingerprint()

    def test_modified_stamp_alone_is_not_content_change(self):
        """Several of these CMSes restamp "sidst opdateret" on every publish
        cycle. Treating that as content change would re-index the whole corpus."""
        assert (self._detail(updated_at="2026-09-01T00:00:00+00:00").fingerprint()
                == self._detail().fingerprint())

    def test_word_count_counts_body_words(self):
        assert self._detail(body_text="en to tre").word_count == 3


class TestScrubbing:
    def test_nul_and_c0_removed(self):
        """Database text and JSON transport must never receive NUL bytes."""
        assert scrub_text("a\x00b\x07c") == "abc"

    def test_tab_newline_cr_kept(self):
        assert scrub_text("a\tb\nc\rd") == "a\tb\nc\rd"

    def test_empty_string_stays_empty_not_none(self):
        """Must not shift "" to None, or a fingerprint would move."""
        assert scrub_text("") == ""
        assert collapse_ws("") == ""


class TestDocumentDetection:
    def test_pdf_url_is_a_document(self):
        assert looks_like_document("https://fredericia.dk/da/postliste.pdf")

    def test_pdf_title_is_a_document(self):
        """Fredericia's Drupal RSS lists "Budgetprocedure 2027-2030.pdf" as a
        news entry; the URL alone does not always give it away."""
        assert looks_like_document("https://fredericia.dk/da/node/123",
                                   "Budgetprocedure 2027-2030.pdf")

    def test_ordinary_article_is_not(self):
        assert not looks_like_document("https://fredericia.dk/da/nyheder/en-nyhed")
