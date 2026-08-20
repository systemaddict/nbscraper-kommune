"""Target registry — which kommune news sites to crawl, and how.

A *target* is one kommune plus its resolved **discovery channel**. Unlike a
meeting-portal scraper, where ~10 platforms cover all 98 kommuner, here every
kommune runs its own site: 98 hosts, no dominant CMS. So a target does not name
a platform module — it names a *channel* and carries the small amount of config
that channel needs:

- ``feed``    — an RSS/Atom URL. Cheapest and most reliable, but rare (~2 in 14
                sampled sites expose one).
- ``sitemap`` — ``sitemap.xml`` filtered to a URL prefix, using ``<lastmod>`` as
                the change signal. Near-universal (13/13 sampled sites had one).
- ``listing`` — the news page's HTML, with a CSS selector for item links.
- ``auto``    — try feed, then sitemap, then listing, and record what worked.

Built-in targets live in ``registry.json`` next to this module, generated from
the verified kommune URL list. Overrides go in ``config/targets.json`` so a
site whose markup changed can be corrected without touching code.

``selected_targets()`` = built-ins overlaid by the file, filtered to enabled
(unless explicit keys are requested via ``NBK_TARGETS``).
"""
from __future__ import annotations

import json
import logging
from dataclasses import dataclass, field
from pathlib import Path

from nbkommune.settings import Settings, get_settings

logger = logging.getLogger(__name__)

_REGISTRY_FILE = Path(__file__).with_name("registry.json")

CHANNELS = ("auto", "feed", "sitemap", "listing")

# What the kommune publishes where, from the verified URL survey.
#   faelles      — one page carries both news and press releases
#   separat      — distinct news and press-release pages
#   kun_nyheder  — news only; no press-release archive exists
#   tredjepart   — press releases live on an external service
SOURCE_TYPES = ("faelles", "separat", "kun_nyheder", "tredjepart")

# Proprietary publication-date markup is opt-in per target. Profiles keep the
# rule definition in one reviewed place while the registry explicitly states
# which municipalities are allowed to use it. None of these are fallbacks: if a
# selector changes or becomes ambiguous, extraction leaves published_at empty.
PUBLISHED_DATE_PROFILES: dict[str, list[dict]] = {
    "gopublic-data-date": [{
        "name": "gopublic-data-date",
        "selector": ".news-page > span.datetime.datetime-to-locale[data-date]",
        "attribute": "data-date",
    }],
    "moliri-created-meta": [{
        "name": "moliri-cmspagecreated",
        "selector": "meta[name='cmspagecreated']",
        "attribute": "content",
    }],
    "hvidovre-visible-date": [{
        "name": "hvidovre-visible-date",
        "selector": ".news-page > span.date",
    }],
    "middelfart-visible-date": [{
        "name": "middelfart-visible-date",
        "selector": ".news-page > span.date",
    }],
    "thisted-visible-date": [{
        "name": "thisted-visible-date",
        "selector": "span.date",
    }],
    "ishoej-visible-date": [{
        "name": "ishoej-visible-date",
        "selector": "#main .rte > h1 + p.small",
    }],
    "slagelse-nuxt-news-date": [{
        "name": "slagelse-nuxt-news-date",
        "pattern": r'newsDate:\s*"([^"]+)"',
    }],
    "favrskov-visible-date": [{
        "name": "favrskov-visible-date",
        "selector": ".news-page__publishing-date",
    }],
    "holstebro-visible-date": [{
        "name": "holstebro-visible-date",
        "selector": ".p-imagetext > h1 + small",
    }],
    "koebenhavn-publication-date": [{
        "name": "koebenhavn-publication-date",
        "selector": ".field--name-publication-date time[datetime]",
        "attribute": "datetime",
    }],
    "ringkoebing-skjern-visible-date": [{
        "name": "ringkoebing-skjern-visible-date",
        "selector": ".news__date",
    }],
    "rudersdal-visible-date": [{
        "name": "rudersdal-visible-date",
        "selector": "header.content-header h1 + span time[datetime]",
        "attribute": "datetime",
    }],
}


@dataclass(frozen=True)
class Target:
    key: str                       # stable slug, e.g. "koebenhavn" — DB primary key
    name: str                      # display name, e.g. "København"
    site_url: str = ""             # kommune root, no trailing slash
    news_url: str = ""             # news listing
    press_url: str = ""            # press-release listing (may equal news_url)
    channel: str = "auto"          # auto | feed | sitemap | listing
    source_type: str = "faelles"
    enabled: bool = True
    # Channel config. `feed_url` for feed; `url_prefix`/`url_prefixes` (+ optional
    # `sitemap_url`) for sitemap; `item_selector` / `link_selector` and optional
    # `date_selector` / `body_selector` for listing, plus strict
    # `published_date_rules` for detail-page dates that use proprietary markup.
    # Kept as a loose dict so a site can be fixed in config/targets.json without
    # a schema migration.
    config: dict = field(default_factory=dict)
    # Why this site needs attention — carried through from the URL survey
    # ("JS-rendered", "verify archive", …). Operational, never behavioural.
    note: str = ""
    # Set when the survey could not confirm the URLs. Such targets still crawl;
    # the flag exists so a bad extraction can be traced back to a shaky source.
    verified: bool = True

    def normalised(self) -> Target:
        """Tidy the target without rewriting any URL a human verified.

        Only ``site_url`` loses its trailing slash — it is a root that gets
        ``urljoin``-ed, so the slash is noise there. The listing URLs are kept
        **exactly** as surveyed: on these sites a trailing slash is part of the
        address, not decoration. 20 of the 98 registry entries end in one, and
        stripping it is not cosmetic — gentofte.dk serves the slashed path but
        502s on the slashless one, which cost a nine-minute discovery pass and
        zero articles before this was tracked down.
        """
        return Target(
            key=self.key,
            name=self.name,
            site_url=self.site_url.rstrip("/"),
            news_url=self.news_url,
            press_url=self.press_url,
            channel=(self.channel or "auto").strip().lower(),
            source_type=self.source_type,
            enabled=self.enabled,
            config=dict(self.config or {}),
            note=self.note,
            verified=self.verified,
        )

    @property
    def listing_urls(self) -> list[str]:
        """Distinct pages to crawl for this kommune.

        A ``faelles`` site has one page under both names; returning it twice
        would double every request, so identical URLs collapse to one.
        """
        urls = [u for u in (self.news_url, self.press_url) if u]
        out: list[str] = []
        for u in urls:
            if u not in out:
                out.append(u)
        return out

    @property
    def published_date_rules(self) -> list[dict]:
        """Reviewed detail-date rules explicitly enabled for this target."""
        inline = self.config.get("published_date_rules")
        if isinstance(inline, list):
            return inline
        profile = self.config.get("published_date_profile")
        if not profile:
            return []
        rules = PUBLISHED_DATE_PROFILES.get(str(profile))
        if rules is None:
            logger.warning("target %r has unknown published date profile %r",
                           self.key, profile)
            return []
        return [dict(rule) for rule in rules]


def _coerce(entry: dict) -> Target | None:
    try:
        target = Target(
            key=entry["key"],
            name=entry["name"],
            site_url=entry.get("site_url", ""),
            news_url=entry.get("news_url", ""),
            press_url=entry.get("press_url", ""),
            channel=entry.get("channel", "auto"),
            source_type=entry.get("source_type", "faelles"),
            enabled=bool(entry.get("enabled", True)),
            config=entry.get("config") or {},
            note=entry.get("note", ""),
            verified=bool(entry.get("verified", True)),
        ).normalised()
    except (KeyError, TypeError) as exc:
        logger.warning("skipping malformed target entry %r: %s", entry, exc)
        return None
    if target.channel not in CHANNELS:
        logger.warning("target %r has unknown channel %r — treating as 'auto'",
                       target.key, target.channel)
        object.__setattr__(target, "channel", "auto")
    return target


def _load_json(path: Path, *, required: bool) -> list[dict]:
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError:
        if required:
            logger.error("cannot read built-in registry %s", path)
        return []
    except (OSError, json.JSONDecodeError) as exc:
        level = logger.error if required else logger.warning
        level("ignoring %s: %s", path, exc)
        return []


def registry(settings: Settings | None = None) -> dict[str, Target]:
    """All known targets (built-ins overlaid by file entries), keyed by slug."""
    settings = settings or get_settings()
    reg: dict[str, Target] = {}
    for entry in _load_json(_REGISTRY_FILE, required=True):
        t = _coerce(entry)
        if t is not None:
            reg[t.key] = t
    for entry in _load_json(settings.targets_file, required=False):
        t = _coerce(entry)
        if t is not None:
            # A file entry *merges* into the built-in: a targets.json that only
            # fixes `item_selector` must not blank out the site's URLs.
            base = reg.get(t.key)
            if base is not None:
                merged = {**base.__dict__, **{
                    k: v for k, v in t.__dict__.items()
                    if v not in ("", {}, None) or k in ("enabled", "verified")
                }}
                merged["config"] = {**base.config, **t.config}
                t = Target(**merged).normalised()
            reg[t.key] = t
    return reg


def selected_targets(settings: Settings | None = None) -> list[Target]:
    """Targets to crawl this run.

    With ``NBK_TARGETS`` set, exactly those keys are used (including disabled
    ones — an explicit request overrides the flag). Empty = all *enabled*
    targets that have at least one listing URL.
    """
    settings = settings or get_settings()
    reg = registry(settings)
    if settings.targets:
        out: list[Target] = []
        for key in settings.targets:
            if key in reg:
                out.append(reg[key])
            else:
                logger.warning("unknown target %r (not in registry) — skipping", key)
        return out
    return [t for t in reg.values() if t.enabled and t.listing_urls]
