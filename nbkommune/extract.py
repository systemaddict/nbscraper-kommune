"""Article extraction — turn a fetched HTML page into an ``ArticleDetail``.

There is no common CMS across the 98 kommune sites, so there is no single
selector that works. Instead each field is resolved through an ordered set of
**layers**, best-quality first, and the layer that won is recorded in
``ArticleDetail.provenance``:

1. ``jsonld``    — schema.org ``NewsArticle``/``Article``. Clean and complete
                   when present, but the survey found it on only 1 of 5 sampled
                   article pages.
2. ``meta``      — ``og:*``, ``article:published_time``, and the Umbraco
                   ``cmspageupdated`` stamp. Good for title and image; for dates
                   it usually only offers *modified*, not published.
3. ``readability`` — Mozilla's Readability algorithm (``readability-lxml``).
                   Strong on conventional article markup, weak on these
                   municipal CMSes: measured against the density heuristic below
                   it won on 2 of 5 sampled pages and lost badly on Jammerbugt
                   (139 words vs 366 — it dropped over half the article). So it
                   is a *candidate*, not the authority.
4. ``heuristic``  — ``<h1>`` and the densest low-link container. DOM date
                   candidates are diagnostics only until a target opts into an
                   exact selector. On this corpus this layer is usually the
                   better of the two DOM body extractors.
5. ``listing``   — whatever the feed/sitemap/listing row already told us. Last
                   resort for title. A publication date is accepted only from
                   a feed or a reviewed listing selector, never from arbitrary
                   date-shaped card text.

For the **body** specifically the rule is borrowed from the SerpScraper4
extension, which solved the same problem for news articles: a schema.org
``articleBody`` wins when it is *substantial* (``SCHEMA_MIN_WORDS``), because
then it is the cleanest complete text available; otherwise the best of the two
DOM extractors wins. Running both and taking the longer beats trusting either,
since neither is reliably better across 98 hand-rolled CMSes. Which one won is
recorded in ``provenance['body_text']``, and every candidate's length is kept in
``raw['body_candidates']`` so the survey can show who is carrying which sites.

Provenance is not decoration. With this many heterogeneous sites, the only way
to notice that a site's extraction has silently degraded is to be able to ask
"which sites are still falling through to the heuristic layer?".
"""
from __future__ import annotations

import json
import logging
import re
from typing import Any
from urllib.parse import urljoin

from bs4 import BeautifulSoup
from bs4.element import Tag
from readability import Document as ReadabilityDocument
from readability.readability import Unparseable

from nbkommune.dates import parse_danish_datetime
from nbkommune.records import ArticleDetail, ListedArticle, normalise_url

logger = logging.getLogger(__name__)

# Elements that are never article content. Removed before any text extraction so
# a navigation menu can't win the "densest container" contest.
_STRIP_TAGS = (
    "script", "style", "noscript", "nav", "header", "footer", "aside",
    "iframe", "svg", "button", "template",
)

# Class/id fragments that mark chrome on these CMSes. Matched case-insensitively
# against the joined class list and the id. Kept conservative: over-stripping
# loses body text, which is worse than a stray cookie sentence.
_STRIP_PATTERN = re.compile(
    r"cookie|consent|samtykke|gdpr|menu|navigation|navbar|breadcrumb|brødkrumme"
    r"|sidebar|related|relateret|share|social|deling|skip-link|search|soegning"
    r"|newsletter|nyhedsbrev|subscribe|banner|toolbar|accessibility|tilgaengelig",
    re.I,
)

# Structured contact cards sometimes sit inside the page's ``<article>`` even
# though they are shared CMS chrome. Unlike a broad class match for "contact"
# (which could remove genuine prose), schema.org LocalBusiness is an explicit
# signal that the block describes the office, not the news item.
_STRIP_ITEMTYPE_PATTERN = re.compile(r"(?:schema\.org/)?LocalBusiness\b", re.I)

# Containers that plausibly hold the article body, best first.
_BODY_SELECTORS = (
    "[itemprop='articleBody']",
    "article .rich-text", "article .article-body", "article .body", "article",
    "main .rich-text", "main .article-body", "main .content", "main",
    ".article-body", ".articleBody", ".rich-text", ".news-article",
    ".page-content", ".content-body", "#content", ".content",
)

# Block elements whose text forms the body, in document order.
_BLOCK_TAGS = ("p", "h2", "h3", "h4", "li", "blockquote", "dd", "dt")

# Explicit meta names/properties carrying a publication time, best first.
# Generic ``name=date`` is intentionally excluded: event pages use it for the
# event date, and that is not publication metadata.
_PUBLISHED_META = (
    ("property", "article:published_time"),
    ("name", "article:published_time"),
    ("property", "og:article:published_time"),
    ("property", "og:published_time"),
    ("property", "og:pageDate"),
    ("itemprop", "datePublished"),
    ("name", "datePublished"),
    ("name", "publish-date"),
    ("name", "publishdate"),
    ("name", "pubdate"),
    ("name", "page_date"),
    ("name", "dcterms.created"),
    ("name", "page:date"),
)
_MODIFIED_META = (
    ("property", "article:modified_time"),
    ("property", "og:updated_time"),
    ("property", "og:modified_time"),
    ("name", "article:modified_time"),
    ("name", "last-modified"),
    ("name", "dcterms.modified"),
    ("name", "page:lastUpdated"),
    ("name", "cmspageupdated"),
)

# JSON-LD @types that represent an article. WebPage is included because several
# Umbraco/Sitecore setups emit the publication dates on a WebPage node and
# nothing else.
_ARTICLE_TYPES = {"newsarticle", "article", "blogposting", "report", "webpage"}

# How many words a schema.org `articleBody` needs before it is trusted over the
# DOM extractors. Taken from the SerpScraper4 extension's SCHEMA_MIN_WORDS: below
# this a site is emitting a teaser or a stub in its structured data, and the
# rendered page holds the real article.
SCHEMA_MIN_WORDS = 100

# How much longer the best DOM extraction may be before it overrules an
# otherwise-substantial articleBody. Guards the case the extension does not
# handle: structured data carrying a genuine but truncated first section.
_LD_TRUNCATION_RATIO = 1.5

# Words in a URL path or section name that mark a press release rather than
# general news. Used only when the site does not label the item itself.
_PRESS_MARKERS = re.compile(r"presse|pressemeddel|press-release|pressrelease", re.I)

# DOM publication dates are not standardised, but their semantics repeat across
# CMSes. Candidates gain confidence from schema attributes, publication labels
# and proximity to the article heading. Event dates in the body lack those
# signals and stay below the acceptance threshold.
_PUBLISHED_LABEL = re.compile(
    r"\b(publiceret|udgivet|offentliggjort|oprettet|posted|published)\b", re.I
)
_MODIFIED_LABEL = re.compile(
    r"\b(opdateret|ændret|redigeret|modified|updated|lastmod)\b", re.I
)
_DATE_SEMANTIC = re.compile(r"date|dato|datetime|publish|udgiv|offentliggj", re.I)
_PUBLISHED_SEMANTIC = re.compile(r"datepublished|publish|udgiv|offentliggj", re.I)
_DOM_DATE_ATTRS = (
    "datetime", "data-date", "data-datetime", "data-published",
    "data-publish-date", "data-publication-date",
)
_DOM_DATE_MIN_SCORE = 100
# ── helpers ──────────────────────────────────────────────────────────────────
# A chrome-looking wrapper that holds most of the page's text is not chrome —
# it is a badly-named layout div wrapping the article. Skanderborg serves the
# whole page inside `<div class="navbar hidden-print">`; stripping that on the
# class match alone deleted 83% of the text and left an empty article. So a
# class/id match only strips an element holding less than this share of the
# document's text.
_STRIP_MAX_TEXT_SHARE = 0.4

# Never strip these on a class/id match, whatever they are called — removing one
# empties the document.
_NEVER_STRIP = frozenset({"html", "body", "main", "article"})


def _clean_soup(html: str) -> BeautifulSoup:
    """Parse and strip chrome. Returns a soup safe to score for text density.

    Only the heuristic layer may use this soup. It removes ``<script>``, which
    destroys the page's JSON-LD, so the ``jsonld`` and ``meta`` layers must read
    an unstripped parse instead — see ``extract_article``.

    Every loop re-checks ``tag.decomposed``: ``find_all`` returns a flat list
    captured up front, so decomposing a parent leaves its descendants in that
    list as detached tags whose ``attrs`` is ``None`` — touching one raises
    ``AttributeError`` mid-parse.
    """
    soup = BeautifulSoup(html, "lxml")
    for tag in soup.find_all(_STRIP_TAGS):
        if not tag.decomposed:
            tag.decompose()

    total = len(soup.get_text(" ", strip=True)) or 1
    limit = total * _STRIP_MAX_TEXT_SHARE

    def strip_matching(tags, value_of, pattern: re.Pattern = _STRIP_PATTERN) -> None:
        for tag in tags:
            if tag.decomposed or tag.name in _NEVER_STRIP:
                continue
            if not pattern.search(value_of(tag)):
                continue
            if len(tag.get_text(" ", strip=True)) > limit:
                continue          # too much of the page to be chrome
            tag.decompose()

    strip_matching(soup.find_all(attrs={"class": True}),
                   lambda t: " ".join(t.get("class") or []))
    strip_matching(soup.find_all(attrs={"id": True}),
                   lambda t: str(t.get("id") or ""))
    strip_matching(soup.find_all(attrs={"itemtype": True}),
                   lambda t: str(t.get("itemtype") or ""),
                   _STRIP_ITEMTYPE_PATTERN)

    # `hidden` and aria-hidden content is invisible to a reader, so it is not
    # body text either — several sites ship a hidden print variant of the page,
    # which would otherwise double every paragraph.
    for tag in soup.find_all(attrs={"aria-hidden": "true"}):
        if not tag.decomposed and tag.name not in _NEVER_STRIP:
            tag.decompose()
    for tag in soup.select("[hidden]"):
        if not tag.decomposed and tag.name not in _NEVER_STRIP:
            tag.decompose()
    return soup


def _blocks(container: Tag) -> list[str]:
    """Text of the container's block elements, in order, de-duplicated.

    Nested blocks (a ``<p>`` inside an ``<li>``) would otherwise contribute
    their text twice; tracking seen strings is cruder than tree-walking but
    holds up better against the malformed markup these sites emit.
    """
    out: list[str] = []
    seen: set[str] = set()
    for el in container.find_all(_BLOCK_TAGS):
        text = re.sub(r"\s+", " ", el.get_text(" ", strip=True)).strip()
        if len(text) < 2 or text in seen:
            continue
        seen.add(text)
        out.append(text)
    return out


def _lines(container: Tag) -> list[str]:
    """Fallback text extraction for containers with no block markup.

    Not a rare case: Skanderborg's article pages carry the whole body in bare
    ``<div>``s and contain exactly one ``<p>`` in the entire document, so
    block-tag extraction returns nothing at all. Splitting the rendered text on
    newlines recovers the paragraphs a reader actually sees.
    """
    text = container.get_text("\n", strip=True)
    out: list[str] = []
    seen: set[str] = set()
    for line in text.split("\n"):
        line = re.sub(r"\s+", " ", line).strip()
        # One- and two-word lines are almost always stray labels or leftover
        # chrome; real body lines are sentences.
        if len(line) < 3 or line in seen:
            continue
        seen.add(line)
        out.append(line)
    return out


def _container_text(container: Tag) -> str:
    """Best available text for a container: block markup when it carries the
    body, rendered lines when it does not.

    The 60% test is the decision: if the block elements account for most of the
    container's visible text, the markup is trustworthy and its paragraph
    boundaries are better than newline-splitting. If they account for little of
    it, the body is in unmarked divs and the lines are all we have.
    """
    total = len(re.sub(r"\s+", " ", container.get_text(" ", strip=True)))
    blocks = _blocks(container)
    if total and sum(len(b) for b in blocks) >= total * 0.6:
        return "\n\n".join(blocks)
    lines = _lines(container)
    return "\n\n".join(lines) if lines else "\n\n".join(blocks)


def _score(container: Tag) -> int:
    """Total block-text length — the density signal for a marked-up container."""
    return sum(len(b) for b in _blocks(container))


def _link_ratio(container: Tag) -> float:
    """Share of the container's text that sits inside links.

    The classic readability signal for telling an article from a menu or a
    teaser list: prose is mostly unlinked, navigation is almost entirely linked.
    """
    text_len = len(container.get_text(" ", strip=True))
    if not text_len:
        return 1.0
    link_len = sum(len(a.get_text(" ", strip=True)) for a in container.find_all("a"))
    return min(link_len / text_len, 1.0)


def _density_score(container: Tag, *, min_chars: int) -> int:
    """Score any container by prose volume, discounted by link density.

    Used when no known selector matched — the generic last resort that lets an
    unrecognised CMS still yield a body.
    """
    text = re.sub(r"\s+", " ", container.get_text(" ", strip=True))
    if len(text) < min_chars:
        return 0
    ratio = _link_ratio(container)
    if ratio > 0.5:
        return 0                     # a link list, not an article
    return int(len(text) * (1.0 - ratio))


def _generic_body(soup: BeautifulSoup, *, min_chars: int) -> Tag | None:
    """Densest low-link container in the document, preferring the tightest one.

    Among near-ties the *deepest* candidate wins: a parent and its only
    content-bearing child score almost identically, and the child is the actual
    article body without the surrounding layout.
    """
    best: Tag | None = None
    best_score = 0
    for el in soup.find_all(("div", "section", "article", "main", "td")):
        score = _density_score(el, min_chars=min_chars)
        if score <= 0:
            continue
        if score > best_score or (
            best is not None and score >= best_score * 0.9
            and len(list(el.parents)) > len(list(best.parents))
        ):
            best, best_score = el, max(score, best_score)
    return best


def _best_body(
    soup: BeautifulSoup, *, extra_selector: str | None = None, min_chars: int = 200
) -> tuple[Tag | None, str]:
    """Pick the container most likely to be the article body.

    Order: an explicit per-site selector, then the generic selector list, then a
    link-density scan. The scan is what covers the sites that use none of the
    conventional wrappers — without it, a CMS that renders its body in unnamed
    divs yields an empty article that still looks successfully ingested.
    """
    if extra_selector:
        try:
            candidates = soup.select(extra_selector)
        except Exception:            # a bad selector from config must not crash
            logger.warning("ignoring invalid body selector %r", extra_selector)
            candidates = []
        best = max(candidates, key=_score, default=None)
        if best is not None and _score(best):
            return best, "config"    # trust an explicit override, don't second-guess it

    best: Tag | None = None
    best_score = 0
    for selector in _BODY_SELECTORS:
        try:
            candidates = soup.select(selector)
        except Exception:
            logger.warning("ignoring invalid body selector %r", selector)
            continue
        for candidate in candidates:
            score = _score(candidate)
            if score > best_score:
                best, best_score = candidate, score
    if best is not None and best_score >= min_chars:
        return best, "selector"

    generic = _generic_body(soup, min_chars=min_chars)
    if generic is not None:
        return generic, "density"
    if best is not None:
        return best, "selector"      # short, but it is what the page has
    return None, "none"


def _iter_jsonld(soup: BeautifulSoup):
    """Yield every dict node in every JSON-LD block, ``@graph`` included."""
    for script in soup.find_all("script", attrs={"type": re.compile("ld\\+json", re.I)}):
        raw = script.string or script.get_text() or ""
        try:
            data = json.loads(raw)
        except (json.JSONDecodeError, TypeError):
            continue                 # malformed JSON-LD is common; just skip it
        stack: list[Any] = [data]
        while stack:
            node = stack.pop()
            if isinstance(node, list):
                stack.extend(node)
            elif isinstance(node, dict):
                yield node
                stack.extend(v for v in node.values() if isinstance(v, (list, dict)))


def _node_types(node: dict) -> set[str]:
    t = node.get("@type")
    values = t if isinstance(t, list) else [t]
    return {str(v).lower() for v in values if v}


def _text_of(value: Any) -> str | None:
    """Flatten a JSON-LD value that may be a string, a dict or a list."""
    if isinstance(value, str):
        return value
    if isinstance(value, dict):
        for key in ("name", "@value", "headline", "url"):
            if isinstance(value.get(key), str):
                return value[key]
        return None
    if isinstance(value, list):
        for item in value:
            got = _text_of(item)
            if got:
                return got
    return None


def _categories_of(node: dict) -> list[str]:
    out: list[str] = []
    for key in ("articleSection", "keywords", "about", "genre"):
        value = node.get(key)
        if isinstance(value, str):
            # keywords are comma-separated by convention
            out.extend(p.strip() for p in value.split(",") if p.strip())
        elif isinstance(value, list):
            for item in value:
                got = _text_of(item)
                if got:
                    out.append(got.strip())
    seen: set[str] = set()
    return [c for c in out if not (c.lower() in seen or seen.add(c.lower()))]


# ── layers ───────────────────────────────────────────────────────────────────
def _from_jsonld(soup: BeautifulSoup, base_url: str) -> dict[str, Any]:
    """Fields from a schema.org Article node, when the site emits one."""
    best: dict[str, Any] = {}
    best_rank = -1
    for node in _iter_jsonld(soup):
        types = _node_types(node)
        matched = types & _ARTICLE_TYPES
        if not matched:
            continue
        # Prefer a real article type over the WebPage fallback.
        rank = 1 if matched - {"webpage"} else 0
        if rank < best_rank:
            continue
        found = {
            "title": _text_of(node.get("headline")) or _text_of(node.get("name")),
            "summary": _text_of(node.get("description")),
            "body_text": _text_of(node.get("articleBody")),
            "published_at": parse_danish_datetime(_text_of(node.get("datePublished"))),
            "updated_at": parse_danish_datetime(_text_of(node.get("dateModified"))),
            "author": _text_of(node.get("author")),
            "categories": _categories_of(node),
            "lang": _text_of(node.get("inLanguage")),
        }
        image = _text_of(node.get("image"))
        if image:
            found["image_url"] = urljoin(base_url, image)
        found = {k: v for k, v in found.items() if v}
        if found and (rank > best_rank or len(found) > len(best)):
            best, best_rank = found, rank
    return best


def _from_meta(soup: BeautifulSoup, base_url: str) -> dict[str, Any]:
    """Fields from ``og:*`` / ``<meta name=…>`` / ``<link rel=canonical>``."""
    def meta(attr: str, value: str) -> str | None:
        tag = soup.find("meta", attrs={attr: re.compile(f"^{re.escape(value)}$", re.I)})
        if tag is None:
            return None
        content = tag.get("content")
        return content.strip() if isinstance(content, str) and content.strip() else None

    out: dict[str, Any] = {}
    title = meta("property", "og:title") or meta("name", "twitter:title")
    if title:
        # og:title routinely carries a " - Kommune" site suffix; the <h1> and
        # JSON-LD do not, so trim it to keep titles comparable across layers.
        out["title"] = re.sub(r"\s+[-–|]\s+[^-–|]{3,40}$", "", title).strip() or title
    summary = (meta("property", "og:description") or meta("name", "description")
               or meta("name", "twitter:description"))
    if summary:
        out["summary"] = summary
    image = meta("property", "og:image") or meta("name", "twitter:image")
    if image:
        out["image_url"] = urljoin(base_url, image)
    lang = soup.html.get("lang") if (soup.html and soup.html.attrs) else None
    if isinstance(lang, str) and lang.strip():
        out["lang"] = lang.strip()

    for attr, name in _PUBLISHED_META:
        parsed = parse_danish_datetime(meta(attr, name))
        if parsed:
            out["published_at"] = parsed
            out["_published_how"] = f"meta:{name}"
            break
    for attr, name in _MODIFIED_META:
        parsed = parse_danish_datetime(meta(attr, name))
        if parsed:
            out["updated_at"] = parsed
            out["_updated_how"] = f"meta:{name}"
            break

    canonical = soup.find("link", attrs={"rel": re.compile("^canonical$", re.I)})
    if canonical is not None and isinstance(canonical.get("href"), str):
        out["canonical_url"] = normalise_url(urljoin(base_url, canonical["href"]))

    tags = [t.get("content") for t in soup.find_all(
        "meta", attrs={"property": re.compile("^article:tag$", re.I)})]
    cats = [t.strip() for t in tags if isinstance(t, str) and t.strip()]
    if cats:
        out["categories"] = cats
    return out


def _json_values_for_key(value: Any, wanted: str) -> list[str]:
    """Find one explicitly configured key in a component's JSON model."""
    found: list[str] = []
    stack: list[Any] = [value]
    while stack:
        node = stack.pop()
        if isinstance(node, list):
            stack.extend(node)
        elif isinstance(node, dict):
            for key, child in node.items():
                if str(key).casefold() == wanted.casefold():
                    text = _text_of(child)
                    if text:
                        found.append(text)
                if isinstance(child, (dict, list)):
                    stack.append(child)
    return found


def _from_published_date_rules(
    soup: BeautifulSoup,
    html: str,
    rules: list[dict[str, Any]] | None,
) -> dict[str, Any]:
    """Apply target-owned publication-date rules without guessing.

    A rule is deliberately small and inspectable:

    - ``selector`` + optional ``attribute`` reads a DOM/meta value;
    - ``json_key`` decodes the selected attribute before reading that exact key;
    - ``pattern`` reads the first capture group from the raw document.

    A rule is accepted only when every parseable match agrees on one date. If
    markup changes and a selector starts matching two different dates, the field
    stays empty and the diagnostics explain why. That is safer than silently
    promoting an event or modification date to publication time.
    """
    diagnostics: list[dict[str, Any]] = []
    for position, rule in enumerate(rules or []):
        name = str(rule.get("name") or f"rule-{position + 1}")
        raw_values: list[str] = []
        selector = rule.get("selector")
        pattern = rule.get("pattern")
        if isinstance(selector, str) and selector:
            try:
                matches = soup.select(selector)
            except Exception as exc:
                diagnostics.append({"rule": name, "status": "invalid-selector",
                                    "error": type(exc).__name__})
                continue
            attribute = rule.get("attribute")
            json_key = rule.get("json_key")
            for match in matches:
                value: Any = (match.get(attribute) if isinstance(attribute, str)
                              else match.get_text(" ", strip=True))
                if not isinstance(value, str) or not value.strip():
                    continue
                if isinstance(json_key, str) and json_key:
                    try:
                        raw_values.extend(_json_values_for_key(json.loads(value), json_key))
                    except (json.JSONDecodeError, TypeError):
                        continue
                else:
                    raw_values.append(value.strip())
        elif isinstance(pattern, str) and pattern:
            try:
                raw_values.extend(re.findall(pattern, html, flags=re.I))
            except re.error as exc:
                diagnostics.append({"rule": name, "status": "invalid-pattern",
                                    "error": str(exc)[:120]})
                continue
        else:
            diagnostics.append({"rule": name, "status": "invalid-rule"})
            continue

        parsed = {date for value in raw_values
                  if (date := parse_danish_datetime(value))}
        status = "matched" if len(parsed) == 1 else ("ambiguous" if parsed else "no-date")
        diagnostics.append({
            "rule": name,
            "status": status,
            "matches": len(raw_values),
            "dates": sorted(parsed),
        })
        if len(parsed) == 1:
            return {
                "published_at": next(iter(parsed)),
                "_published_how": f"rule:{name}",
                "_diagnostics": diagnostics,
            }
    return {"_diagnostics": diagnostics} if diagnostics else {}


def _semantic_text(tag: Tag) -> str:
    """Class/id/schema strings that explain what a DOM element represents."""
    values: list[str] = [tag.name or ""]
    for name in ("class", "id", "itemprop", "property", "name", "aria-label"):
        value = tag.get(name)
        if isinstance(value, list):
            values.extend(str(part) for part in value)
        elif isinstance(value, str):
            values.append(value)
    return " ".join(values)


def _nearby_text(tag: Tag) -> str:
    """Small local context only; never scan the whole article for a label."""
    parts: list[str] = []
    sibling = tag.find_previous_sibling()
    for _ in range(2):
        if sibling is None:
            break
        text = sibling.get_text(" ", strip=True)
        if text:
            parts.insert(0, text)
        sibling = sibling.find_previous_sibling()
    own = tag.get_text(" ", strip=True)
    if own:
        parts.append(own)
    parent = tag.parent
    if isinstance(parent, Tag):
        parent_text = parent.get_text(" ", strip=True)
        if len(parent_text) <= 180:
            parts.append(parent_text)
    return re.sub(r"\s+", " ", " ".join(parts)).strip()


def _dom_published_date(soup: BeautifulSoup) -> tuple[str | None, str | None, list[dict]]:
    """Find a publication date from generic, scored semantic evidence.

    Parsing every date-shaped string is unsafe: articles routinely contain
    meeting dates, deadlines and event schedules. Explicit datePublished
    semantics, a nearby publication label or placement beside the h1 are the
    evidence that turns a date-shaped value into a publication date.
    """
    h1 = soup.find("h1")
    near_heading: set[int] = set()
    if h1 is not None:
        # The publication stamp belongs to the article header, before prose
        # starts. A fixed "next N tags" window also includes short article
        # bodies and can mistake an event date for publication.
        for tag in h1.find_all_next(limit=60):
            if tag.name in {"p", "h2", "h3", "h4", "blockquote"}:
                break
            near_heading.add(id(tag))

    candidates: list[dict[str, Any]] = []
    for tag in soup.find_all(True):
        if tag.name in {"html", "head", "body", "script", "style"}:
            continue
        semantic = _semantic_text(tag)
        context = _nearby_text(tag)
        values: list[tuple[str, str]] = []
        for attr in _DOM_DATE_ATTRS:
            value = tag.get(attr)
            if isinstance(value, str) and value.strip():
                values.append((attr, value.strip()))

        itemprop = str(tag.get("itemprop") or "")
        if _PUBLISHED_SEMANTIC.search(itemprop):
            value = tag.get("content")
            if isinstance(value, str) and value.strip():
                values.append(("content", value.strip()))

        # A visible date is only eligible on a date-named element or close to
        # the article heading. This captures `<span class="date">` while a date
        # in an ordinary article paragraph remains ineligible.
        visible = tag.get_text(" ", strip=True)
        if visible and (_DATE_SEMANTIC.search(semantic) or id(tag) in near_heading):
            values.append(("text", visible))

        seen_values: set[tuple[str, str]] = set()
        for source, value in values:
            if (source, value) in seen_values:
                continue
            seen_values.add((source, value))
            parsed = parse_danish_datetime(value)
            if not parsed:
                continue

            score = 0
            evidence: list[str] = []
            if _PUBLISHED_SEMANTIC.search(itemprop):
                score += 140
                evidence.append("datePublished")
            if _PUBLISHED_SEMANTIC.search(semantic):
                score += 90
                evidence.append("published-semantic")
            if _PUBLISHED_LABEL.search(context):
                score += 100
                evidence.append("published-label")
            if source.startswith("data-publish") or source == "data-publication-date":
                score += 90
                evidence.append(source)
            elif source in {"data-date", "data-datetime", "datetime"}:
                score += 55
                evidence.append(source)
            if tag.name == "time":
                score += 20
                evidence.append("time")
            if _DATE_SEMANTIC.search(semantic):
                score += 50
                evidence.append("date-semantic")
            if id(tag) in near_heading:
                score += 60
                evidence.append("near-h1")
            if _MODIFIED_LABEL.search(f"{semantic} {context}"):
                score -= 160
                evidence.append("modified-label")

            candidates.append({
                "value": parsed,
                "score": score,
                "source": source,
                "tag": tag.name,
                "evidence": evidence,
            })

    candidates.sort(key=lambda item: item["score"], reverse=True)
    compact = candidates[:8]
    if not compact or compact[0]["score"] < _DOM_DATE_MIN_SCORE:
        return None, None, compact
    winner = compact[0]
    return winner["value"], f"dom:{winner['source']}", compact


def _from_heuristic(soup: BeautifulSoup, base_url: str, *,
                    body_selector: str | None = None,
                    min_body_chars: int = 200) -> dict[str, Any]:
    """Fields read off the rendered page: ``<h1>``, ``<time>``, densest text."""
    out: dict[str, Any] = {}
    h1 = soup.find("h1")
    if h1 is not None:
        text = h1.get_text(" ", strip=True)
        if text:
            out["title"] = text

    container, how = _best_body(
        soup, extra_selector=body_selector, min_chars=min_body_chars
    )
    if container is not None:
        text = _container_text(container)
        if text:
            out["body_text"] = text
            out["body_html"] = str(container)
            out["_body_how"] = how
        # An image inside the body beats og:image, which is often a generic
        # social-card fallback — but only when og:image is absent, decided by
        # the merge order, not here.
        img = container.find("img")
        if img is None:
            # Hero images are often a sibling of `.rich-text`, not inside it.
            # Walk only as far as the closest ancestor that also owns the h1;
            # this reaches the article shell without drifting into site chrome.
            for parent in container.parents:
                if not isinstance(parent, Tag) or parent.name in {"body", "html"}:
                    break
                if parent.find("h1") is not None:
                    img = parent.find("img")
                    break
        if img is not None and isinstance(img.get("src"), str):
            out["image_url"] = urljoin(base_url, img["src"])

    # DOM date scoring is diagnostic only. A target must opt into the exact
    # selector/attribute through `published_date_rules` before a DOM value can
    # be stored as a publication date.
    _published, _how, candidates = _dom_published_date(soup)
    if candidates:
        out["_date_candidates"] = candidates
    return out


def _from_readability(html: str, base_url: str) -> dict[str, Any]:
    """Body and title via Mozilla's Readability algorithm.

    Never raises: ``readability-lxml`` throws ``Unparseable`` on documents it
    cannot score, and on this corpus that is an ordinary Tuesday, not an error —
    the other layers still have to produce an article.
    """
    out: dict[str, Any] = {}
    try:
        document = ReadabilityDocument(html)
        summary_html = document.summary(html_partial=True)
    except (Unparseable, Exception):  # noqa: B014 - Unparseable subclasses Exception
        return out
    if not summary_html:
        return out
    soup = BeautifulSoup(summary_html, "lxml")
    blocks = _blocks(soup)
    text = "\n\n".join(blocks) if blocks else _lines_text(soup)
    if text:
        out["body_text"] = text
        out["body_html"] = summary_html
    try:
        title = document.short_title()
        if title and title.strip():
            out["title"] = title.strip()
    except Exception:
        pass
    return out


def _lines_text(container: Tag) -> str:
    """Newline-split text for a container with no block markup."""
    return "\n\n".join(_lines(container))


def _merge(field: str, layers: list[tuple[str, dict[str, Any]]],
           provenance: dict[str, str]) -> Any:
    """First layer that has a truthy value for ``field`` wins; record which."""
    for name, data in layers:
        value = data.get(field)
        if value:
            provenance[field] = name
            return value
    return None


def _choose_body(
    jsonld: dict[str, Any], heuristic: dict[str, Any], readability: dict[str, Any]
) -> tuple[str | None, str | None, str | None, dict[str, int]]:
    """Pick the article body from the three candidates.

    The rule, after the SerpScraper4 extension's Readability+schema logic:

    1. A schema.org ``articleBody`` of at least ``SCHEMA_MIN_WORDS`` wins — it is
       the cleanest complete text a page can offer.
    2. Unless the best DOM extraction is more than ``_LD_TRUNCATION_RATIO``
       longer, which means the structured data was a genuine-looking stub.
    3. Otherwise the **longer** of Readability and the density heuristic wins.
       Neither is reliably better here: measured across five kommune article
       pages the heuristic won three and Readability two, and Readability's
       failures are large (it returned 139 of 366 words on Jammerbugt). Taking
       the longer of the two is what makes the pair beat either alone.

    Returns ``(text, html, provenance_label, candidate_word_counts)``.
    """
    def _words(text: str | None) -> int:
        return len((text or "").split())

    ld_body = jsonld.get("body_text")
    heur_body = heuristic.get("body_text")
    read_body = readability.get("body_text")
    candidates = {
        "jsonld": _words(ld_body),
        "heuristic": _words(heur_body),
        "readability": _words(read_body),
    }

    # Best of the two DOM extractors.
    dom_label, dom_text, dom_html = None, None, None
    if _words(heur_body) >= _words(read_body) and heur_body:
        dom_label, dom_text, dom_html = ("heuristic", heur_body,
                                         heuristic.get("body_html"))
    elif read_body:
        dom_label, dom_text, dom_html = ("readability", read_body,
                                         readability.get("body_html"))

    if (ld_body and candidates["jsonld"] >= SCHEMA_MIN_WORDS
            and (not dom_text or len(dom_text) <= len(ld_body) * _LD_TRUNCATION_RATIO)):
            # `body_html` deliberately comes from the DOM even when the text does
            # not: an articleBody is plain text, and the rendered markup is what
            # preserves paragraphs, links and emphasis for a consumer.
            return ld_body, dom_html, "jsonld", candidates

    if dom_text:
        return dom_text, dom_html, dom_label, candidates
    if ld_body:
        return ld_body, None, "jsonld", candidates
    return None, None, None, candidates


def classify_kind(url: str, categories: list[str], *,
                  listed_kind: str = "ukendt", press_url: str | None = None) -> str:
    """Whether this is a pressemeddelelse or a general nyhed.

    The listing already knows on a site with separate pages, so that wins. Only
    when the site publishes both under one page (46 of 98 do) is the item's own
    URL and section inspected.
    """
    if listed_kind in ("nyhed", "pressemeddelelse"):
        return listed_kind
    if press_url and _PRESS_MARKERS.search(press_url) and _PRESS_MARKERS.search(url):
        return "pressemeddelelse"
    if _PRESS_MARKERS.search(url):
        return "pressemeddelelse"
    if any(_PRESS_MARKERS.search(c) for c in categories):
        return "pressemeddelelse"
    return "nyhed"


def extract_article(
    html: str,
    url: str,
    *,
    listed: ListedArticle | None = None,
    body_selector: str | None = None,
    published_date_rules: list[dict[str, Any]] | None = None,
    min_body_chars: int = 200,
) -> ArticleDetail:
    """Extract one article, resolving each field through the layer order.

    ``url`` must be the URL the response actually came from (post-redirect), so
    identity keys on the slug the site itself considers canonical.
    """
    # Parsed twice, deliberately. `_clean_soup` decomposes <script>, which is
    # where JSON-LD lives — reading the metadata layers off the cleaned soup
    # silently disables schema.org extraction entirely and quietly degrades every
    # site to whatever its <meta> tags happen to say.
    document = BeautifulSoup(html, "lxml")
    jsonld = _from_jsonld(document, url)
    meta = _from_meta(document, url)
    date_rule = _from_published_date_rules(document, html, published_date_rules)
    heuristic = _from_heuristic(
        _clean_soup(html), url,
        body_selector=body_selector, min_body_chars=min_body_chars,
    )
    readability = _from_readability(html, url)
    from_listing: dict[str, Any] = {}
    if listed is not None:
        from_listing = {
            "title": listed.title,
            "summary": listed.summary,
            "published_at": listed.published_at,
            "updated_at": listed.updated_at,
        }

    layers = [
        ("jsonld", jsonld), ("meta", meta), ("date_rule", date_rule),
        ("heuristic", heuristic),
        ("readability", readability), ("listing", from_listing),
    ]
    provenance: dict[str, str] = {}

    body_text, body_html, body_via, candidates = _choose_body(
        jsonld, heuristic, readability
    )
    if body_via:
        provenance["body_text"] = body_via

    # Published date: standard detail metadata wins, followed by a target-owned
    # proprietary detail rule, then a date the discovery channel explicitly
    # stated (RSS or a reviewed listing selector). Generic DOM candidates never
    # enter this list.
    published_layers = [("jsonld", jsonld), ("meta", meta), ("date_rule", date_rule)]
    if listed is not None and listed.published_at:
        # Keep the discovery contract visible in provenance. Historically every
        # ListedArticle was labelled "listing", including RSS pubDate values;
        # that made it impossible to distinguish an authoritative feed date
        # from a guessed HTML-card date during repair work.
        if listed.channel == "feed":
            listed_date_source = "feed"
        elif listed.raw.get("mode") in ("configured", "configured-json"):
            listed_date_source = "listing:configured"
        else:
            listed_date_source = "listing"
        published_layers.append((listed_date_source, from_listing))
    categories = (_merge("categories", layers, provenance) or [])
    published_at = _merge("published_at", published_layers, provenance)
    updated_at = _merge("updated_at", layers, provenance)
    published_source = provenance.get("published_at")
    if published_source == "meta" and meta.get("_published_how"):
        provenance["published_at"] = meta["_published_how"]
    elif published_source == "date_rule" and date_rule.get("_published_how"):
        provenance["published_at"] = date_rule["_published_how"]
    updated_source = provenance.get("updated_at")
    if updated_source == "meta" and meta.get("_updated_how"):
        provenance["updated_at"] = meta["_updated_how"]
    detail = ArticleDetail(
        url=url,
        title=_merge("title", layers, provenance),
        summary=_merge("summary", layers, provenance),
        body_text=body_text,
        body_html=body_html,
        published_at=published_at,
        updated_at=updated_at,
        image_url=_merge("image_url", [("meta", meta), ("jsonld", jsonld),
                                       ("heuristic", heuristic)], provenance),
        author=_merge("author", layers, provenance),
        categories=list(categories) if isinstance(categories, list) else [],
        lang=_merge("lang", layers, provenance),
        canonical_url=meta.get("canonical_url"),
        provenance=provenance,
        raw={
            "layers_present": [n for n, d in layers if d],
            "body_container": heuristic.get("_body_how", "none"),
            # Every candidate's word count, so a survey can answer "which
            # extractor is carrying which sites?" without re-fetching them.
            "body_candidates": candidates,
            "date_candidates": heuristic.get("_date_candidates", []),
            "date_rule_diagnostics": date_rule.get("_diagnostics", []),
        },
    )
    return detail
