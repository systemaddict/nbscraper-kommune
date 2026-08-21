"""Polite HTTP client: robots gate + per-host rate gate + retry/backoff.

One shared client for the whole scrape. The per-host gate guarantees at least
``scrape_min_interval_s`` between consecutive requests to the *same* host, so
concurrency across 98 kommune sites stays useful while no single kommune gets
hammered.

Three things here that a meeting-portal scraper does not need, and which are the
reason this file is its own rather than a shared import:

- **robots.txt** is honoured (``respect_robots``). We are an unannounced crawler
  on public-sector sites and have no reason to skip it.
- **Text decoding** is explicit: kommune CMSes routinely serve HTML with no
  charset header, or lie about it, and ``æøå`` mojibake silently corrupts every
  extracted body.
- **No download pipeline.** News is text; images are stored as URLs. Nothing
  here streams to disk.
"""
from __future__ import annotations

import logging
import re
import threading
import time
import urllib.robotparser
from urllib.parse import urlencode, urljoin, urlparse

import httpx
from tenacity import (
    retry,
    retry_if_exception_type,
    stop_after_attempt,
    wait_exponential,
)

from nbkommune.settings import Settings, get_settings

logger = logging.getLogger(__name__)


# Statuses that say "this URL will never work", as opposed to "not right now":
# the article was deleted (404/410), the host refuses us (401/403/451), or the
# request itself is wrong (400/405). Retrying these burns the task's whole
# attempt budget on a foregone conclusion, so the caller settles them instead.
PERMANENT_STATUSES = frozenset({400, 401, 403, 404, 405, 410, 451})

# Cloudflare serves its challenge as 403 (managed challenge / WAF block) or 503
# (legacy "checking your browser"), which PERMANENT_STATUSES would otherwise
# read as "the kommune deleted this" — the one misreading that lets a whole
# kommune go dark without a distinguishable error.
_CF_STATUSES = frozenset({403, 503})
_CF_BODY_MARKERS = (
    b"just a moment",
    b"__cf_chl",
    b"cf-browser-verification",
    b"enable javascript and cookies to continue",
    b"attention required",
    b"you have been blocked",
    b"cloudflare ray id",
)

# Some WAFs in front of these sites refuse a request with HTTP **200** and a
# tiny body ("Access Denied"). Left undetected that is the worst possible
# outcome: extraction succeeds, finds nothing, and stores an empty article that
# looks like a real one. So a short body carrying one of these markers is
# treated as a block, not as content. The length cap is what keeps a real
# article that happens to discuss access denial from tripping it.
_SOFT_BLOCK_MARKERS = (
    b"access denied",
    b"request rejected",
    b"request unsuccessful",
    b"you don't have permission",
    b"blocked by",
)
_SOFT_BLOCK_MAX_BYTES = 2048

# The proxy endpoint. Named because two places must recognise a proxied URL:
# `_public_url`, so the token never reaches the DB, and `_transient` so it never
# reaches the error log.
_SCRAPEDO_ENDPOINT = "https://api.scrape.do/"

# Charset declared inside the document, for the common case of a kommune CMS
# serving `Content-Type: text/html` with no charset at all.
_META_CHARSET = re.compile(
    rb"""<meta[^>]+charset=["']?\s*([a-zA-Z0-9_\-]+)""", re.I
)


def cloudflare_reason(status_code: int, headers, body: bytes | None = None) -> str | None:
    """Name the Cloudflare block behind this response, or None if it isn't one.

    ``cf-mitigated`` is Cloudflare saying so itself, so it is trusted alone.
    Otherwise we require the edge's fingerprint (``server: cloudflare`` /
    ``cf-ray``) *and* a challenge marker in the body: a CF-fronted site
    answering its own 403 on a restricted page must stay a plain permanent
    failure, or every access-controlled page starts costing proxy credits.
    """
    mitigated = headers.get("cf-mitigated")
    if mitigated:
        return f"cf-mitigated={mitigated}"
    if status_code not in _CF_STATUSES:
        return None
    server = (headers.get("server") or "").lower()
    ray = headers.get("cf-ray")
    if "cloudflare" not in server and not ray:
        return None
    blob = (body or b"")[:8192].lower()
    if not any(marker in blob for marker in _CF_BODY_MARKERS):
        return None
    return f"cloudflare challenge page (cf-ray={ray})" if ray else "cloudflare challenge page"


class CloudflareBlocked(httpx.HTTPStatusError):
    """Cloudflare stopped the request — the origin never saw it.

    Subclasses ``HTTPStatusError`` so the retry predicate covers it (the retry
    after escalation goes through the proxy), and names itself in ``str()`` so
    the error log reads *Cloudflare*, not a bare "HTTP 403" that looks like any
    deleted page.
    """

    def __init__(self, url: str, reason: str, *, request, response) -> None:
        super().__init__(
            f"Cloudflare blocked {url} (HTTP {response.status_code}, {reason})",
            request=request,
            response=response,
        )
        self.reason = reason


class TransientHttpError(httpx.HTTPStatusError):
    """A retryable status (5xx/429), named with the **origin** URL.

    Raised instead of ``resp.raise_for_status()`` because that builds its message
    from ``resp.url`` — which for a proxied request is the scrape.do URL with the
    token in it, and that message is stored verbatim in ``scrape_error``.
    """

    def __init__(self, url: str, *, request, response) -> None:
        super().__init__(
            f"HTTP {response.status_code} for {url}", request=request, response=response
        )
        self.url = url
        self.status_code = response.status_code


class WafBlocked(Exception):
    """A WAF refused the request while claiming success (200 + refusal body).

    Retryable on purpose: the retry re-routes through the proxy when one is
    configured, which is exactly what clears an IP- or fingerprint-based block.
    """

    def __init__(self, url: str, marker: str) -> None:
        super().__init__(f"WAF blocked {url} (200 response, {marker!r})")
        self.url = url
        self.marker = marker


def soft_block_marker(status_code: int, content: bytes) -> str | None:
    """The refusal marker in a suspiciously short 200 body, or None."""
    if status_code != 200 or len(content) > _SOFT_BLOCK_MAX_BYTES:
        return None
    blob = content.lower()
    for marker in _SOFT_BLOCK_MARKERS:
        if marker in blob:
            return marker.decode("ascii")
    return None


class RobotsDenied(Exception):
    """robots.txt disallows this URL for our user-agent.

    Permanent by construction — retrying cannot change the answer — so the
    caller settles the task instead of spending its attempt budget.
    """

    def __init__(self, url: str) -> None:
        super().__init__(f"robots.txt disallows {url}")
        self.url = url


class PermanentHttpError(Exception):
    """A status a retry cannot fix (404/410/403/…). Carries the code."""

    def __init__(self, url: str, status_code: int) -> None:
        super().__init__(f"HTTP {status_code} for {url}")
        self.url = url
        self.status_code = status_code


def decode_html(content: bytes, content_type: str | None) -> str:
    """Decode a fetched HTML body, honouring what it actually declares.

    Order: charset in the Content-Type header, then ``<meta charset>`` in the
    document, then UTF-8, then cp1252 as the last resort — that last step is
    what keeps a legacy kommune page from arriving as ``ï¿½``. Decoding always
    succeeds: cp1252 with ``errors="replace"`` cannot raise, and a garbled
    article is still worth storing and flagging over losing it entirely.
    """
    charset = None
    if content_type and "charset=" in content_type.lower():
        charset = content_type.lower().split("charset=", 1)[1].split(";")[0].strip(" \"'")
    if not charset:
        m = _META_CHARSET.search(content[:4096])
        if m:
            charset = m.group(1).decode("ascii", "ignore")
    for candidate in (charset, "utf-8"):
        if not candidate:
            continue
        try:
            return content.decode(candidate)
        except (LookupError, UnicodeDecodeError):
            continue
    return content.decode("cp1252", errors="replace")


class _DomainGate:
    """Min-interval gate, tracked per host. Thread-safe."""

    def __init__(self, min_interval_s: float) -> None:
        self._min = min_interval_s
        self._lock = threading.Lock()
        self._last: dict[str, float] = {}

    def wait(self, host: str) -> None:
        if self._min <= 0:
            return
        with self._lock:
            now = time.monotonic()
            last = self._last.get(host, 0.0)
            wait_s = self._min - (now - last)
            if wait_s > 0:
                time.sleep(wait_s)
            self._last[host] = time.monotonic()


class _RobotsGate:
    """Per-host robots.txt, fetched once and cached for ``ttl_s``.

    Fetched with a bare client, not through ``HttpClient.get`` — routing
    robots.txt through the retry/Cloudflare/proxy machinery would recurse, and a
    missing robots.txt is the overwhelmingly common case that must stay cheap.
    A host we cannot reach fails **open** (allowed): treating a flaky
    robots.txt as a blanket disallow would silently stop crawling a kommune
    that never objected.
    """

    def __init__(self, *, user_agent: str, ttl_s: float, timeout_s: float) -> None:
        self._ua = user_agent
        self._ttl = ttl_s
        self._timeout = timeout_s
        self._lock = threading.Lock()
        self._cache: dict[str, tuple[float, urllib.robotparser.RobotFileParser | None]] = {}

    def _parser(self, url: str):
        parts = urlparse(url)
        origin = f"{parts.scheme}://{parts.netloc}"
        with self._lock:
            entry = self._cache.get(origin)
            if entry and entry[0] > time.monotonic():
                return entry[1]
        parser: urllib.robotparser.RobotFileParser | None = None
        try:
            resp = httpx.get(
                urljoin(origin, "/robots.txt"),
                headers={"User-Agent": self._ua},
                timeout=self._timeout,
                follow_redirects=True,
            )
            if resp.status_code == 200:
                parser = urllib.robotparser.RobotFileParser()
                parser.parse(decode_html(
                    resp.content, resp.headers.get("content-type")
                ).splitlines())
        except Exception as exc:   # unreachable / malformed → fail open
            logger.debug("robots.txt unavailable for %s: %s", origin, exc)
        with self._lock:
            self._cache[origin] = (time.monotonic() + self._ttl, parser)
        return parser

    def allowed(self, url: str) -> bool:
        parser = self._parser(url)
        if parser is None:
            return True
        try:
            return parser.can_fetch(self._ua, url)
        except Exception:
            return True

    def crawl_delay(self, url: str) -> float | None:
        """The host's requested delay, when it states one."""
        parser = self._parser(url)
        if parser is None:
            return None
        try:
            delay = parser.crawl_delay(self._ua)
            return float(delay) if delay is not None else None
        except Exception:
            return None


class HttpClient:
    """Thin wrapper over httpx with politeness, robots and retry baked in."""

    def __init__(self, settings: Settings | None = None) -> None:
        self.settings = settings or get_settings()
        self._gate = _DomainGate(self.settings.scrape_min_interval_s)
        self._robots = _RobotsGate(
            user_agent=self.settings.user_agent,
            ttl_s=self.settings.robots_ttl_s,
            timeout_s=self.settings.http_connect_timeout_s,
        ) if self.settings.respect_robots else None
        self._scrapedo_token = self.settings.scrapedo_token
        self._scrapedo_hosts = set(self.settings.scrapedo_hosts)
        self._scrapedo_fallback_hosts = set(self.settings.scrapedo_fallback_hosts)
        self._scrapedo_render_hosts = set(self.settings.scrapedo_render_hosts)
        self._scrapedo_super_hosts = set(self.settings.scrapedo_super_hosts)
        # host → monotonic deadline until which we prefer the proxy for it.
        self._degraded: dict[str, float] = {}
        self._degraded_lock = threading.Lock()
        # host → the identifier that host actually accepted, learned at runtime.
        self._ua_by_host: dict[str, str] = {}
        self._ua_lock = threading.Lock()
        self._client = httpx.Client(
            headers={
                # User-Agent is set per request, not here: it is chosen per host
                # by the fallback rotation in `get`.
                # Danish first: several of these sites content-negotiate and
                # would otherwise hand us an English stub.
                "Accept-Language": "da,en;q=0.5",
                "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
            },
            timeout=httpx.Timeout(
                self.settings.http_timeout_s,
                connect=self.settings.http_connect_timeout_s,
            ),
            follow_redirects=True,
        )

    # ── proxy routing ───────────────────────────────────────────
    def _use_proxy(self, host: str) -> bool:
        """True when this host should be fetched through scrape.do — either
        always (``scrapedo_hosts``) or because it is currently degraded
        (throttling us, or Cloudflare-blocked, within the TTL).

        The opt-in check lives in ``_mark_degraded``: a host only ever *enters*
        the degraded map if allowed to, so reading it needs no second gate.
        """
        if not self._scrapedo_token:
            return False
        if host in self._scrapedo_hosts:
            return True
        with self._degraded_lock:
            return self._degraded.get(host, 0.0) > time.monotonic()

    def is_proxied(self, host: str) -> bool:
        """Whether requests to this host currently go through scrape.do.

        Public so channel resolution can skip its speculative probing: through
        the proxy each probe costs tens of seconds, and a dozen of them times a
        discovery pass out entirely.
        """
        return self._use_proxy(host)

    def _degrade(self, host: str) -> bool:
        """Route ``host`` through the proxy for the fallback TTL. Returns True
        if it was being fetched directly until now (a switch, not a refresh) —
        so the caller logs the transition once, not per request."""
        with self._degraded_lock:
            was_direct = self._degraded.get(host, 0.0) <= time.monotonic()
            self._degraded[host] = time.monotonic() + self.settings.scrapedo_fallback_ttl_s
        return was_direct

    def _mark_degraded(self, host: str) -> None:
        """The origin just throttled us (5xx/429) — prefer the proxy for a
        while. Only hosts opted into the fallback are switched, so a genuinely
        broken site doesn't silently start costing proxy credits."""
        if not self._scrapedo_token or host not in self._scrapedo_fallback_hosts:
            return
        if self._degrade(host):
            logger.info("%s is throttling — routing via scrape.do for %.0f s",
                        host, self.settings.scrapedo_fallback_ttl_s)

    def _mark_cloudflare(self, host: str, reason: str) -> None:
        """Cloudflare is challenging us — escalate to the proxy immediately.

        Unlike a 5xx, a challenge is unambiguous and is exactly what a
        residential proxy clears, so *every* host escalates rather than only the
        opted-in ones (``scrapedo_auto_cloudflare`` turns that off). Always
        warns, even when we can't act: a site that starts challenging us is news
        whether or not a token is configured — and a challenge that survives the
        proxy is louder still.
        """
        if self._use_proxy(host):
            logger.warning("Cloudflare is blocking %s even via scrape.do (%s)", host, reason)
            return
        logger.warning("Cloudflare is blocking %s (%s)", host, reason)
        if not self._scrapedo_token or not self.settings.scrapedo_auto_cloudflare:
            return
        if self._degrade(host):
            logger.warning("routing %s via scrape.do for %.0f s to clear the challenge",
                           host, self.settings.scrapedo_fallback_ttl_s)

    def _route(self, url: str) -> tuple[str, str]:
        """Map a request URL to ``(request_url, gate_host)``.

        When proxying, the politeness gate still keys on the *origin* host (so we
        stay polite to the kommune, not to scrape.do), and callers only ever pass
        origin URLs — the token never reaches the DB, the error log or our logs.
        Resolved per attempt, so a request that trips a challenge switches to the
        proxy on its very next retry.
        """
        host = urlparse(url).netloc
        if self._use_proxy(host):
            params = {"token": self._scrapedo_token, "url": url}
            if host in self._scrapedo_super_hosts:
                params["super"] = "true"
            if host in self._scrapedo_render_hosts:
                params["render"] = "true"
                if self.settings.scrapedo_render_wait_ms:
                    params["customWait"] = str(self.settings.scrapedo_render_wait_ms)
            return _SCRAPEDO_ENDPOINT + "?" + urlencode(params), host
        return url, host

    # ── fetching ────────────────────────────────────────────────
    def _agents_for(self, host: str, override: str | None) -> list[str]:
        """The User-Agents to try for this host, best first.

        A host that has already refused one identifier keeps using the one that
        worked, so the rotation costs a single extra request per host per
        process, not per fetch.
        """
        with self._ua_lock:
            learned = self._ua_by_host.get(host)
        primary = override or learned or self.settings.user_agent
        chain = [primary]
        for candidate in [self.settings.user_agent] + list(self.settings.user_agent_fallbacks):
            if candidate and candidate not in chain:
                chain.append(candidate)
        return chain

    def get(self, url: str, *, user_agent: str | None = None) -> httpx.Response:
        """GET with robots check, per-host throttling and retry on transients.

        Raises ``RobotsDenied`` / ``PermanentHttpError`` for outcomes a retry
        cannot fix, and ``CloudflareBlocked`` / ``HTTPStatusError`` once the
        retries are spent on ones it might.

        On a WAF refusal (403 or a 200 soft-block) the request is retried with
        the next honest identifier in the fallback chain before the failure is
        allowed to stand; the identifier that worked is remembered per host.
        """
        if self._robots is not None and not self._robots.allowed(url):
            raise RobotsDenied(url)

        host = urlparse(url).netloc
        try:
            return self._try_agents(url, host, user_agent)
        except (PermanentHttpError, WafBlocked) as exc:
            if not self._escalate_refusal(host, exc):
                raise
            # `_route` re-resolves per attempt, so this pass goes through the
            # proxy without the caller or the URL changing.
            return self._try_agents(url, host, user_agent)

    @staticmethod
    def _is_refusal(exc: Exception) -> bool:
        """Whether this failure is the host refusing *us*, rather than the page
        being absent. A 404 means the article is gone and no proxy will help."""
        if isinstance(exc, WafBlocked):
            return True
        return isinstance(exc, PermanentHttpError) and exc.status_code in (401, 403, 451)

    def _escalate_refusal(self, host: str, exc: Exception) -> bool:
        """Route a refusing host through the proxy. True if a retry is worth it.

        Fires only after every identifier in the rotation has been refused, so a
        host that merely dislikes one User-Agent never costs proxy credits.
        """
        if not self._is_refusal(exc):
            return False
        if not self._scrapedo_token or not self.settings.scrapedo_auto_refusal:
            logger.warning("%s refuses every configured identifier (%s) and no "
                           "scrape.do fallback is available", host, exc)
            return False
        if self._use_proxy(host):
            logger.warning("%s refuses us even via scrape.do (%s)", host, exc)
            return False
        self._degrade(host)
        logger.warning("%s refuses every identifier (%s) — retrying via scrape.do "
                       "and routing it there for %.0f s",
                       host, exc, self.settings.scrapedo_fallback_ttl_s)
        return True

    def _try_agents(self, url: str, host: str,
                    user_agent: str | None) -> httpx.Response:
        """Attempt the request under each identifier until one is accepted."""
        agents = self._agents_for(host, user_agent)
        last_exc: Exception | None = None
        for index, agent in enumerate(agents):
            try:
                resp = self._get_once(url, agent)
            except (PermanentHttpError, WafBlocked) as exc:
                # Only a refusal is worth re-asking as someone else. A 404 is
                # the site telling us the article is gone, and re-asking under a
                # different name would just repeat the question.
                refused = isinstance(exc, WafBlocked) or (
                    isinstance(exc, PermanentHttpError)
                    and exc.status_code in (401, 403, 451)
                )
                last_exc = exc
                if not refused or index == len(agents) - 1:
                    raise
                logger.info("%s refused UA %r — retrying as %r",
                            host, agent[:40], agents[index + 1][:40])
                continue
            if index > 0:
                with self._ua_lock:
                    self._ua_by_host[host] = agent
                logger.info("%s accepts UA %r — using it for this host", host, agent[:40])
            return resp
        raise last_exc if last_exc else RuntimeError(f"no user agent available for {url}")

    def _get_once(self, url: str, agent: str) -> httpx.Response:
        """One GET under one identifier, with the politeness gate and retries."""

        @retry(
            stop=stop_after_attempt(self.settings.http_max_retries),
            wait=wait_exponential(multiplier=1, max=20),
            retry=retry_if_exception_type(
                (httpx.TransportError, httpx.HTTPStatusError, WafBlocked)
            ),
            reraise=True,
        )
        def _do() -> httpx.Response:
            request_url, host = self._route(url)
            self._gate.wait(host)
            if self._robots is not None:
                # A site asking for more space than our floor gets it. Applied
                # on top of the gate, not instead of it, so the configured
                # minimum is always also respected.
                delay = self._robots.crawl_delay(url)
                if delay and delay > self.settings.scrape_min_interval_s:
                    time.sleep(delay - self.settings.scrape_min_interval_s)
            timeout = (
                httpx.Timeout(self.settings.scrapedo_timeout_s,
                              connect=self.settings.http_connect_timeout_s)
                if request_url.startswith(_SCRAPEDO_ENDPOINT)
                else None
            )
            resp = (self._client.get(request_url, headers={"User-Agent": agent},
                                     timeout=timeout)
                    if timeout is not None
                    else self._client.get(request_url, headers={"User-Agent": agent}))
            # A Cloudflare challenge is transient *if* we change how we ask — so
            # escalate to the proxy and let the retry go through it, rather than
            # letting the 403 fail fast as the origin's own answer.
            reason = cloudflare_reason(resp.status_code, resp.headers, resp.content)
            if reason:
                self._mark_cloudflare(host, reason)
                raise CloudflareBlocked(url, reason, request=resp.request, response=resp)
            if resp.status_code >= 500 or resp.status_code == 429:
                self._mark_degraded(host)
                raise TransientHttpError(       # transient → retry
                    url, request=resp.request, response=resp)
            if resp.status_code in PERMANENT_STATUSES:
                raise PermanentHttpError(url, resp.status_code)
            if resp.status_code >= 400:
                raise TransientHttpError(url, request=resp.request, response=resp)
            if len(resp.content) > self.settings.max_response_bytes:
                raise PermanentHttpError(url, resp.status_code)
            marker = soft_block_marker(resp.status_code, resp.content)
            if marker:
                self._mark_degraded(host)
                raise WafBlocked(url, marker)
            return resp

        return _do()

    @staticmethod
    def _public_url(requested: str, resp: httpx.Response) -> str:
        """The origin URL for a response — never the proxy URL.

        Critical: when a request is proxied, ``resp.url`` is
        ``https://api.scrape.do/?token=…&url=…``. That string is what callers
        store as the article's ``url`` and ``canonical_url``, so returning it
        would write the **scrape.do token into the database** and give the
        article an identity pointing at the proxy instead of the kommune.

        For a proxied request the origin URL we asked for is returned as-is. A
        redirect the origin performed is invisible through the proxy, so a
        proxied article keeps its pre-redirect URL — a small, deliberate loss of
        fidelity in exchange for never leaking the token.
        """
        final = str(resp.url)
        if final.startswith(_SCRAPEDO_ENDPOINT):
            return requested
        return final

    def get_text(self, url: str, *, user_agent: str | None = None) -> tuple[str, str]:
        """GET and decode. Returns ``(text, final_url)``.

        The final URL matters: kommune CMSes redirect ``/nyheder/123`` to a slug,
        and the slug is the URL we want as the article's identity.
        """
        resp = self.get(url, user_agent=user_agent)
        return (
            decode_html(resp.content, resp.headers.get("content-type")),
            self._public_url(url, resp),
        )

    def post_json_text(self, url: str, payload: dict,
                       *, headers: dict[str, str] | None = None,
                       user_agent: str | None = None) -> tuple[str, str]:
        """POST JSON to a public same-origin listing endpoint and decode it.

        A few municipal CMSes ship an empty HTML listing shell and populate it
        through a CSRF-protected JSON POST.  The shared client is important here:
        it preserves the cookie established while fetching that shell.  Unlike
        GET discovery this deliberately does not proxy; replaying an origin
        cookie and CSRF token through a third party would both be fragile and
        unnecessary for the reviewed endpoints using this method.
        """
        if self._robots is not None and not self._robots.allowed(url):
            raise RobotsDenied(url)

        host = urlparse(url).netloc
        agent = self._agents_for(host, user_agent)[0]

        @retry(
            stop=stop_after_attempt(self.settings.http_max_retries),
            wait=wait_exponential(multiplier=1, max=20),
            retry=retry_if_exception_type(
                (httpx.TransportError, httpx.HTTPStatusError, WafBlocked)
            ),
            reraise=True,
        )
        def _do() -> httpx.Response:
            self._gate.wait(host)
            request_headers = {"User-Agent": agent, **(headers or {})}
            resp = self._client.post(url, json=payload, headers=request_headers)
            reason = cloudflare_reason(resp.status_code, resp.headers, resp.content)
            if reason:
                raise CloudflareBlocked(url, reason, request=resp.request, response=resp)
            if resp.status_code >= 500 or resp.status_code == 429:
                raise TransientHttpError(url, request=resp.request, response=resp)
            if resp.status_code in PERMANENT_STATUSES:
                raise PermanentHttpError(url, resp.status_code)
            if resp.status_code >= 400:
                raise TransientHttpError(url, request=resp.request, response=resp)
            if len(resp.content) > self.settings.max_response_bytes:
                raise PermanentHttpError(url, resp.status_code)
            marker = soft_block_marker(resp.status_code, resp.content)
            if marker:
                raise WafBlocked(url, marker)
            return resp

        resp = _do()
        return decode_html(resp.content, resp.headers.get("content-type")), str(resp.url)

    def get_bytes(self, url: str, *, user_agent: str | None = None) -> tuple[bytes, str]:
        """GET without decoding — for XML (sitemaps, feeds), where the parser
        wants the raw bytes and its own declared encoding."""
        resp = self.get(url, user_agent=user_agent)
        return resp.content, self._public_url(url, resp)

    def close(self) -> None:
        self._client.close()

    def __enter__(self) -> HttpClient:
        return self

    def __exit__(self, *exc) -> None:
        self.close()
