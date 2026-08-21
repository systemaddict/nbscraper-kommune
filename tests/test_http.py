"""HTTP client: block detection, decoding, and the UA rotation.

Every behaviour tested here was driven by something a real kommune site did.
"""
from __future__ import annotations

import httpx
import pytest
import respx

from nbkommune.http import (
    HttpClient,
    PermanentHttpError,
    WafBlocked,
    cloudflare_reason,
    decode_html,
    soft_block_marker,
)
from nbkommune.settings import Settings

URL = "https://kommune.dk/nyheder/en-nyhed"


def _settings(**kw) -> Settings:
    base = dict(
        _env_file=None, respect_robots=False, scrape_min_interval_s=0.0,
        http_max_retries=1, user_agent="UA-primary",
        user_agent_fallbacks=["UA-second"],
    )
    return Settings(**{**base, **kw})


class TestCloudflareDetection:
    def test_challenge_page_recognised(self):
        assert cloudflare_reason(
            403, {"server": "cloudflare", "cf-ray": "abc"},
            b"<h1>Just a moment...</h1>") == "cloudflare challenge page (cf-ray=abc)"

    def test_cf_mitigated_header_trusted_alone(self):
        assert cloudflare_reason(200, {"cf-mitigated": "challenge"}) == "cf-mitigated=challenge"

    def test_origins_own_403_is_not_cloudflare(self):
        """A CF-fronted site answering its own 403 must stay a plain permanent
        failure, or every access-controlled page starts costing proxy credits."""
        assert cloudflare_reason(403, {"server": "cloudflare", "cf-ray": "x"},
                                 b"<h1>Adgang nagtet</h1>") is None

    def test_no_cloudflare_fingerprint_is_not_cloudflare(self):
        assert cloudflare_reason(403, {"server": "nginx"}, b"just a moment") is None


class TestSoftBlockDetection:
    """A WAF that refuses with HTTP 200 and a tiny body is the worst case:
    extraction 'succeeds', finds nothing, and stores an empty article."""

    def test_short_access_denied_body_is_a_block(self):
        assert soft_block_marker(200, b"Access Denied") == "access denied"

    def test_long_body_mentioning_it_is_not(self):
        assert soft_block_marker(200, b"<p>" + b"x" * 3000 + b"access denied") is None

    def test_non_200_is_handled_elsewhere(self):
        assert soft_block_marker(403, b"Access Denied") is None


class TestDecoding:
    def test_header_charset_honoured(self):
        assert decode_html("Ærø æøå".encode(), "text/html; charset=utf-8") == "Ærø æøå"

    def test_meta_charset_used_when_header_is_silent(self):
        body = b'<meta charset="iso-8859-1">' + "Ærø".encode("latin-1")
        assert "Ærø" in decode_html(body, "text/html")

    def test_cp1252_last_resort_never_raises(self):
        """A legacy kommune page must not be lost to a UnicodeDecodeError."""
        assert decode_html("Ærø æøå".encode("cp1252"), None) == "Ærø æøå"


class TestUserAgentRotation:
    """Measured on lolland.dk: the tokens "bot" and "scraper" are refused, and a
    bare (non-Mozilla) UA gets a 13-byte "Access Denied". Which identifier a host
    accepts is opaque, so the client asks again as someone else."""

    @respx.mock
    def test_rotates_to_the_fallback_on_403(self):
        route = respx.get(URL)
        route.side_effect = [
            httpx.Response(403, content=b"Access Denied"),
            httpx.Response(200, html="<html><body>ok</body></html>"),
        ]
        with HttpClient(_settings()) as http:
            resp = http.get(URL)
        assert resp.status_code == 200
        assert route.calls[0].request.headers["user-agent"] == "UA-primary"
        assert route.calls[1].request.headers["user-agent"] == "UA-second"

    @respx.mock
    def test_remembers_the_accepted_agent_per_host(self):
        """The rotation must cost one extra request per host, not per fetch."""
        route = respx.get(URL)
        route.side_effect = [
            httpx.Response(403, content=b"Access Denied"),
            httpx.Response(200, html="ok"),
            httpx.Response(200, html="ok"),
        ]
        with HttpClient(_settings()) as http:
            http.get(URL)
            http.get(URL)
        assert len(route.calls) == 3
        assert route.calls[2].request.headers["user-agent"] == "UA-second"

    @respx.mock
    def test_404_is_not_retried_under_another_name(self):
        """A 404 is the site saying the article is gone. Re-asking as someone else
        just repeats the question and doubles the traffic."""
        route = respx.get(URL).mock(return_value=httpx.Response(404))
        with HttpClient(_settings()) as http, pytest.raises(PermanentHttpError) as exc:
            http.get(URL)
        assert exc.value.status_code == 404
        assert len(route.calls) == 1

    @respx.mock
    def test_refusal_by_every_agent_raises(self):
        respx.get(URL).mock(return_value=httpx.Response(200, content=b"Access Denied"))
        with HttpClient(_settings()) as http, pytest.raises(WafBlocked):
            http.get(URL)

    @respx.mock
    def test_rotation_can_be_disabled(self):
        route = respx.get(URL).mock(return_value=httpx.Response(403, content=b"nope"))
        with HttpClient(_settings(user_agent_fallbacks=[])) as http, \
                pytest.raises(PermanentHttpError):
            http.get(URL)
        assert len(route.calls) == 1


class TestRobots:
    @respx.mock
    def test_disallowed_url_is_not_fetched(self):
        respx.get("https://kommune.dk/robots.txt").mock(
            return_value=httpx.Response(200, text="User-agent: *\nDisallow: /nyheder/"))
        page = respx.get(URL).mock(return_value=httpx.Response(200, html="ok"))
        from nbkommune.http import RobotsDenied
        with HttpClient(_settings(respect_robots=True)) as http, \
                pytest.raises(RobotsDenied):
            http.get(URL)
        assert not page.called

    @respx.mock
    def test_missing_robots_fails_open(self):
        """An unreachable robots.txt must not silently stop crawling a kommune
        that never objected."""
        respx.get("https://kommune.dk/robots.txt").mock(return_value=httpx.Response(404))
        respx.get(URL).mock(return_value=httpx.Response(200, html="ok"))
        with HttpClient(_settings(respect_robots=True)) as http:
            assert http.get(URL).status_code == 200


class TestScrapedoEscalation:
    """A host that refuses every identifier is escalated to the proxy.

    Gentofte does exactly this: a plain 403 with no Cloudflare fingerprint, from
    a WAF that has decided about our network rather than our User-Agent.
    """

    def _proxied(self, **kw) -> Settings:
        base = {"scrapedo_token": "SECRET-TOKEN", "scrapedo_auto_refusal": True}
        return _settings(**{**base, **kw})

    @respx.mock
    def test_refusal_surviving_the_rotation_escalates_to_the_proxy(self):
        respx.get(URL).mock(return_value=httpx.Response(403, content=b"Adgang nagtet"))
        proxied = respx.get(url__startswith="https://api.scrape.do/").mock(
            return_value=httpx.Response(200, html="<html><body>rigtig side</body></html>"))
        with HttpClient(self._proxied()) as http:
            resp = http.get(URL)
        assert resp.status_code == 200
        assert proxied.called

    @respx.mock
    def test_the_proxy_url_carries_the_origin_url_and_the_token(self):
        respx.get(URL).mock(return_value=httpx.Response(403))
        proxied = respx.get(url__startswith="https://api.scrape.do/").mock(
            return_value=httpx.Response(200, html="ok"))
        with HttpClient(self._proxied()) as http:
            http.get(URL)
        request_url = str(proxied.calls[0].request.url)
        assert "SECRET-TOKEN" in request_url
        assert "kommune.dk" in request_url

    @respx.mock
    def test_rendered_host_waits_for_xhr_backed_listing(self):
        proxied = respx.get(url__startswith="https://api.scrape.do/").mock(
            return_value=httpx.Response(200, html="ok"))
        settings = self._proxied(
            scrapedo_hosts=["kommune.dk"],
            scrapedo_render_hosts=["kommune.dk"],
            scrapedo_render_wait_ms=3000,
        )
        with HttpClient(settings) as http:
            http.get(URL)
        request_url = str(proxied.calls[0].request.url)
        assert "render=true" in request_url
        assert "customWait=3000" in request_url

    @respx.mock
    def test_super_host_uses_residential_rotation(self):
        proxied = respx.get(url__startswith="https://api.scrape.do/").mock(
            return_value=httpx.Response(200, html="ok"))
        settings = self._proxied(
            scrapedo_hosts=["kommune.dk"],
            scrapedo_super_hosts=["kommune.dk"],
        )
        with HttpClient(settings) as http:
            http.get(URL)
        request_url = str(proxied.calls[0].request.url)
        assert "super=true" in request_url

    @respx.mock
    def test_the_token_never_becomes_the_articles_url(self):
        """`get_text` returns the URL a caller stores. If that were the proxy URL,
        the token would be written into the database as the article's identity."""
        respx.get(URL).mock(return_value=httpx.Response(403))
        respx.get(url__startswith="https://api.scrape.do/").mock(
            return_value=httpx.Response(200, html="<html><body>ok</body></html>"))
        with HttpClient(self._proxied()) as http:
            _, final_url = http.get_text(URL)
        assert final_url == URL
        assert "SECRET-TOKEN" not in final_url
        assert "scrape.do" not in final_url

    @respx.mock
    def test_a_404_is_never_escalated(self):
        """No proxy can conjure up a deleted article; escalating would just burn
        credits on every removed page."""
        respx.get(URL).mock(return_value=httpx.Response(404))
        proxied = respx.get(url__startswith="https://api.scrape.do/").mock(
            return_value=httpx.Response(200, html="ok"))
        with HttpClient(self._proxied()) as http, pytest.raises(PermanentHttpError):
            http.get(URL)
        assert not proxied.called

    @respx.mock
    def test_no_token_means_the_refusal_simply_stands(self):
        respx.get(URL).mock(return_value=httpx.Response(403))
        with HttpClient(_settings(scrapedo_token="")) as http, \
                pytest.raises(PermanentHttpError):
            http.get(URL)

    @respx.mock
    def test_escalation_can_be_switched_off(self):
        respx.get(URL).mock(return_value=httpx.Response(403))
        proxied = respx.get(url__startswith="https://api.scrape.do/").mock(
            return_value=httpx.Response(200, html="ok"))
        with HttpClient(self._proxied(scrapedo_auto_refusal=False)) as http, \
                pytest.raises(PermanentHttpError):
            http.get(URL)
        assert not proxied.called

    @respx.mock
    def test_a_5xx_error_message_does_not_contain_the_token(self):
        """`raise_for_status` builds its message from the *proxied* URL, and that
        message is stored verbatim in scrape_error."""
        respx.get(URL).mock(return_value=httpx.Response(403))
        respx.get(url__startswith="https://api.scrape.do/").mock(
            return_value=httpx.Response(503, content=b"upstream down"))
        with HttpClient(self._proxied()) as http, pytest.raises(Exception) as exc:
            http.get(URL)
        assert "SECRET-TOKEN" not in str(exc.value)
