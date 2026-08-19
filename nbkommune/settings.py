"""Configuration via pydantic-settings (.env-aware).

Read this through ``get_settings()`` — never touch ``os.environ`` directly.
Every knob is a ``Field`` with a one-line note on what it does and why the
default is what it is.

App knobs are namespaced ``NBK_``. Bunny Database's two platform-injected
variables retain their standard ``BUNNY_DATABASE_*`` names.
"""
from __future__ import annotations

from datetime import date
from functools import lru_cache
from pathlib import Path
from typing import Annotated

from pydantic import AliasChoices, Field, SecretStr, field_validator
from pydantic_settings import BaseSettings, NoDecode, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore",
        case_sensitive=False,
        env_prefix="NBK_",
    )

    # ── Scrape targets ──────────────────────────────────────────
    # Which kommuner to scrape. Empty = every enabled target in the registry.
    targets: Annotated[list[str], NoDecode] = Field(default_factory=list)
    # Optional JSON file with extra/override targets so a site's discovery
    # config can be corrected without a code change. Same shape as
    # registry.json. Missing file is fine.
    targets_file: Path = Field(default=Path("./config/targets.json"))
    # Never store an article published before this date (YYYY-MM-DD). News has
    # a long back-catalogue that sitemaps happily expose; without a floor the
    # first crawl of 98 sites would pull in a decade of archive. Articles with
    # no parseable date fail *open* (kept) — we would rather store an undated
    # current article than silently drop it.
    min_published_date: str = Field(default="2026-01-01")

    @field_validator("min_published_date")
    @classmethod
    def _validate_min_published(cls, v: str) -> str:
        v = v.strip()
        if v:
            date.fromisoformat(v)  # fail fast at startup on a malformed floor
        return v

    @property
    def min_published_floor(self) -> date | None:
        """Parsed publication floor, or None when unset."""
        return date.fromisoformat(self.min_published_date) if self.min_published_date else None

    # ── HTTP / politeness ───────────────────────────────────────
    # Identify ourselves honestly — a name and a contact URL, behind the
    # conventional `Mozilla/5.0 (compatible; …)` prefix.
    #
    # Two measured constraints shape this string, both from lolland.dk and
    # kerteminde.dk (Umbraco behind a WAF):
    #   1. The `Mozilla/5.0 (compatible; …)` prefix is required. Without it the
    #      request gets a 13-byte "Access Denied" (HTTP 403).
    #   2. The tokens **"bot"** and **"scraper"** are refused anywhere in the
    #      string. "nbmedier-nyhedsbot" and "nbscraper-nyheder" both 403;
    #      "nbmedier-nyheder" gets 200.
    # So the identifier says who we are and how to reach us without using either
    # blocked word. Verified 200 on lolland, kerteminde, jammerbugt, skanderborg.
    user_agent: str = Field(
        default="Mozilla/5.0 (compatible; nbmedier-nyheder/0.1; +https://nbmedier.dk)"
    )
    # Alternate identifiers tried, in order, when a host answers a WAF refusal
    # (403/soft-block) to the primary UA; the one that works is remembered per
    # host. Each is still an honest identifier with a contact URL — this rotates
    # *identifiers*, it does not impersonate a browser or forge TLS/header
    # fingerprints, and robots.txt stays authoritative either way. Empty list
    # disables the rotation and lets a refusal stand as a permanent failure.
    user_agent_fallbacks: Annotated[list[str], NoDecode] = Field(
        default_factory=lambda: [
            "Mozilla/5.0 (compatible; nbmedier/0.1; +https://nbmedier.dk)",
        ]
    )
    http_timeout_s: float = Field(default=30.0)
    # Short connect timeout so a down host fails in seconds rather than hanging
    # for the full read timeout.
    http_connect_timeout_s: float = Field(default=8.0)
    http_max_retries: int = Field(default=3)
    # Minimum seconds between consecutive requests to the SAME host. Higher
    # than a meeting-portal scraper would need: these are the kommuner's own
    # public websites serving real citizens, and we have no deadline.
    scrape_min_interval_s: float = Field(default=2.0)
    # Hard ceiling on one response body. News pages are small; 25 MB is already
    # far past any article and catches a runaway.
    max_response_bytes: int = Field(default=25_000_000)
    # Honour robots.txt. On by default: we are an unannounced crawler on 98
    # public-sector sites and have no reason not to.
    respect_robots: bool = Field(default=True)
    # How long a fetched robots.txt stays cached before we re-read it.
    robots_ttl_s: float = Field(default=86400.0)

    # ── scrape.do proxy (Cloudflare bypass) ─────────────────────
    # Some kommune sites answer a datacenter IP with a Cloudflare challenge
    # (Gentofte does today). Requests for those hosts go through scrape.do,
    # which solves the challenge from a residential IP and returns the origin
    # response. The origin URL is what the domain gate, error log and stored
    # article see, so the token never lands in the DB. Empty token → no
    # proxying at all.
    scrapedo_token: str = Field(default="")
    # Hosts always fetched through the proxy.
    scrapedo_hosts: Annotated[list[str], NoDecode] = Field(default_factory=list)
    # Hosts fetched directly until they start throttling us (5xx/429), then
    # routed through the proxy for `scrapedo_fallback_ttl_s`.
    scrapedo_fallback_hosts: Annotated[list[str], NoDecode] = Field(default_factory=list)
    scrapedo_fallback_ttl_s: float = Field(default=1800.0)
    # Escalate *any* host to the proxy the moment a Cloudflare challenge is
    # recognised, not just the opted-in ones. A challenge page is precisely
    # what the residential proxy exists to clear; the alternative is a kommune
    # going silently dark behind 403s.
    scrapedo_auto_cloudflare: bool = Field(default=True)
    # Same escalation for a *non-Cloudflare* refusal: a 401/403/451, or a WAF
    # answering 200 with a refusal body, that survives the whole User-Agent
    # rotation. Gentofte 403s a datacenter IP this way — no Cloudflare
    # fingerprint, just a WAF that has decided about our network. Without this
    # the kommune simply goes dark, which is the failure this scraper most wants
    # to avoid. Only fires once the UA rotation is exhausted, so it costs proxy
    # credits solely for hosts that genuinely refuse every identifier.
    scrapedo_auto_refusal: bool = Field(default=True)
    # Hosts fetched through scrape.do with JS rendering (`render=true`). Costs
    # more per request and roughly doubles latency, so it is opt-in per host
    # rather than a global default. Rendering is what makes a client-side news
    # list visible at all — measured on taarnby.dk, the HTML grows from 12 KB to
    # 111 KB with it. Note that rendering alone is not always enough: a list
    # populated by a later XHR still needs a wait selector or the site's own
    # JSON endpoint, so check with `nbk resolve` before enabling a site.
    scrapedo_render_hosts: Annotated[list[str], NoDecode] = Field(default_factory=list)
    # Read timeout for proxied requests. Separate from `http_timeout_s` because a
    # proxy fetch legitimately takes longer than a direct one — it is doing the
    # request on our behalf, sometimes rendering — and because scrape.do can take
    # the better part of a minute to report an upstream failure.
    scrapedo_timeout_s: float = Field(default=90.0)

    # ── Database ────────────────────────────────────────────────
    # Bunny injects these unprefixed names when a Database is attached to a
    # Magic Container. NBK_* aliases remain available for other environments.
    database_url: str = Field(
        default="",
        validation_alias=AliasChoices("BUNNY_DATABASE_URL", "NBK_DATABASE_URL"),
    )
    database_auth_token: str = Field(
        default="",
        validation_alias=AliasChoices(
            "BUNNY_DATABASE_AUTH_TOKEN", "NBK_DATABASE_AUTH_TOKEN"
        ),
    )
    # Timeout for one libSQL HTTP request. Database operations are small, but a
    # cold regional replica can take longer than an ordinary local query.
    database_timeout_s: float = Field(default=30.0)

    # ── Dashboard authentication ───────────────────────────────
    # Enabled by default so a newly deployed dashboard fails closed. The worker
    # ignores these settings, and local diagnostics can explicitly opt out.
    auth_enabled: bool = Field(default=True)
    # Public origin used for Better Auth's origin checks and secure cookies.
    auth_base_url: str = Field(default="")
    # High-entropy signing key; SecretStr keeps it out of settings repr/logs.
    auth_secret: SecretStr = Field(default=SecretStr(""))
    # Used exactly once when the auth tables contain no users. Signup remains
    # disabled on the public handler, so these are deployment-only credentials.
    auth_bootstrap_email: str = Field(default="")
    auth_bootstrap_password: SecretStr = Field(default=SecretStr(""))
    # Loopback port for FastAPI behind the public Better Auth gateway.
    auth_internal_port: int = Field(default=8001, ge=1, le=65535)

    # ── Scheduling / queue ──────────────────────────────────────
    # How often each target is re-discovered. News moves slower than an agenda
    # portal and 98 sites × 24/day is already plenty of traffic.
    discover_interval_min: float = Field(default=120.0)
    # Paced lane spacing: at most one backfill/recheck task per this many
    # seconds, so a bulk backfill drains gently and never starves fresh work.
    paced_task_interval_s: float = Field(default=10.0)
    # A claimed task holds a lease this long; if the worker dies the janitor
    # requeues it once the lease expires.
    task_lease_min: float = Field(default=15.0)
    task_backoff_base_s: float = Field(default=60.0)
    task_backoff_max_s: float = Field(default=21600.0)
    # Done/dead tasks older than this are purged so the queue table stays small.
    task_retention_days: float = Field(default=14.0)
    # Re-check an ingested article this many days after publication to catch
    # late edits, then stop. News is corrected within days, not months.
    recheck_settle_days: float = Field(default=7.0)
    recheck_interval_days: float = Field(default=2.0)
    # Max articles a single discovery pass may enqueue per target. Guards
    # against a sitemap-shaped surprise (a site exposing 10k URLs under
    # /nyheder/) flooding the queue on first contact.
    discover_enqueue_cap: int = Field(default=50)

    # ── Extraction ──────────────────────────────────────────────
    # Minimum extracted body length (characters) for an article to count as
    # successfully ingested. Below this we store it but flag `thin` so the
    # operator can see which sites need a real selector.
    min_body_chars: int = Field(default=200)

    @field_validator("targets", "scrapedo_hosts", "scrapedo_fallback_hosts",
                     "scrapedo_render_hosts", "user_agent_fallbacks", mode="before")
    @classmethod
    def _split_csv(cls, v):
        """Accept comma-separated env values (NBK_TARGETS=aarhus,odder)."""
        if isinstance(v, str):
            return [p.strip() for p in v.split(",") if p.strip()]
        return v

    def validate_auth(self) -> None:
        """Fail before binding a public port when auth configuration is unsafe."""
        if not self.auth_base_url.startswith(("http://", "https://")):
            raise ValueError("NBK_AUTH_BASE_URL must be an absolute http(s) URL")
        if len(self.auth_secret.get_secret_value()) < 32:
            raise ValueError("NBK_AUTH_SECRET must be at least 32 characters")
        password = self.auth_bootstrap_password.get_secret_value()
        if bool(self.auth_bootstrap_email) != bool(password):
            raise ValueError(
                "NBK_AUTH_BOOTSTRAP_EMAIL and NBK_AUTH_BOOTSTRAP_PASSWORD must be set together"
            )
        if password and len(password) < 8:
            raise ValueError("NBK_AUTH_BOOTSTRAP_PASSWORD must be at least 8 characters")


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    return Settings()
