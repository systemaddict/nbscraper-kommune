# CLAUDE.md

Guidance for AI agents (Claude Code) working in this repository.

## What this is

A scraper for **nyheder og pressemeddelelser** from all 98 Danish kommuner:
discover article URLs, extract the article, store it. Independent of the
byrådsmøde scraper (`../nbscraper`) — its own repo, its own Bunny Database, its
own `NBK_` config namespace, no shared code. Design *shapes* were borrowed
(task queue, world-state/work-state split, append-only error log); nothing is
imported.

The philosophy in one line: **discovery updates the world and files work orders;
one worker executes work orders by priority; every failure is a structured,
append-only log row, retried with backoff until dead.**

### Why this is not the meeting scraper

The problem shape is inverted. The meeting scraper covers 98 kommuner with ~10
platform backends, because they all buy the same agenda portals. Here there are
**98 hosts and no dominant CMS**, so a module per site is not viable. Instead
there is **one generic pipeline driven by per-site config**, with layered
fallbacks at both ends:

- **Discovery** (`nbkommune/sources/`) resolves a channel per site:
  `feed` → `listing` → `sitemap` (see below for why that order).
- **Extraction** (`nbkommune/extract.py`) resolves each field through layers:
  `jsonld` → `meta` → `heuristic` → `listing`, recording which layer won in
  `article.provenance_json`.

A site needing a fix gets **config**, not code: selectors and channel overrides
go in `config/targets.json`, which merges over `nbkommune/registry.json`.

## Measured facts that shape the code

These were established by probing the real sites. Do not "simplify" them away.

1. **User-Agent is load-bearing.** Some of these WAFs (lolland.dk,
   kerteminde.dk) refuse a request unless the UA starts with
   `Mozilla/5.0 (compatible; …)`, **and** they refuse any UA containing the
   tokens `bot` or `scraper`. A bare honest UA gets a 13-byte "Access Denied".
   Hence the default UA and the per-host fallback rotation in `http.py`.
2. **A WAF can refuse with HTTP 200** and a tiny "Access Denied" body. Undetected,
   extraction "succeeds" and stores an empty article — so `soft_block_marker`
   treats a short refusal body as a block.
3. **Publication dates are the scarcest field.** Of five sampled article pages,
   one had JSON-LD `datePublished`, three had only `cmspageupdated` (a
   *modification* stamp), one had nothing. This is why the **listing** channel
   outranks the sitemap: a listing row often carries the only real publication
   date in existence.
4. **`datetime.fromisoformat` silently misparses `"2026-08-18 06.28"`** as
   06:00:00.28 — fractional seconds, minutes discarded. That is the single most
   common timestamp format in this corpus (Umbraco `cmspageupdated`). See
   `_normalise_dotted` in `dates.py`; there is a test class guarding it.
5. **Chrome-named wrappers are not always chrome.** Skanderborg serves its whole
   page inside `<div class="navbar hidden-print">`; stripping on the class match
   alone deleted 83% of the text. Hence `_STRIP_MAX_TEXT_SHARE`.
6. **Some sites have no block markup.** Skanderborg's article pages contain
   exactly one `<p>` in the document; the body is in bare divs. Hence the
   link-density fallback in `_best_body`.
7. **News feeds list non-articles.** Fredericia's Drupal RSS publishes
   `Budgetprocedure 2027-2030.pdf` and weekly postlists as entries. Hence
   `looks_like_document`.
8. **5 of 98 sites render their visible news list client-side** (Tårnby,
   Hvidovre, Middelfart, Skive, Thisted). Their official sitemaps expose the
   article URLs, so they use an explicit `sitemap` channel rather than paying
   for browser rendering on every discovery pass.

## Workflow rules

- **ALWAYS create a git branch before making any file changes.** Never edit
  files directly on `main`. Branch first (`git checkout -b <type>/<slug>`), then
  change files. This applies to **every** file change — code, config, or docs.
  If you realize you've started on `main`, branch immediately (uncommitted
  changes carry over).
- Committing and pushing require an explicit request from the user. Branching is
  mandatory and automatic; committing is not.

## Conventions

- Python ≥ 3.12, `from __future__ import annotations` at the top of every module.
- Config goes through `nbkommune.settings.Settings` (pydantic-settings) — read it
  via `get_settings()`, never `os.environ`. New knobs get a `Field(...)` with a
  one-line comment on what it does and why the default is what it is.
- Be a polite scraper. These are the kommuner's own public websites serving real
  citizens, and we have no deadline: `robots.txt` is honoured, and
  `NBK_SCRAPE_MIN_INTERVAL_S` defaults to 2 s per host. Never lower it to rush a
  backfill.
- The UA rotation rotates **honest identifiers only**. Do not add
  browser-impersonation, TLS/header fingerprint forgery, or CAPTCHA solving; and
  robots.txt stays authoritative regardless of which identifier is in use.
- Change detection is keyed on a **content hash**, never on a CMS-assigned id —
  these sites renumber nodes on migration. Listing-level:
  `ListedArticle.fingerprint`; content-level: `ArticleDetail.fingerprint`.
- `article.status` is **world-state only** (`listed` → `ingested` → `gone`). All
  work-state (attempts, backoff, dead-letter) lives on `scrape_task`, and every
  failure is an append-only `scrape_error` row. To make something happen,
  **enqueue** — never set a status to signal work.
- `ingested_at` moves **only** on real content change (`detail_hash`);
  `checked_at` moves on every verification. A consumer keys re-indexing off
  `ingested_at`, so moving it on an unchanged re-fetch re-indexes the corpus.
- All SQLite-compatible SQL lives in `nbkommune.repositories`; nothing else
  touches the DB. `nbkommune.db` owns the Bunny libSQL/HTTP protocol adapter.
- Queue transitions and `record_error` **commit themselves**; plain writes follow
  callers-commit.
- When extraction degrades, it must be **loud**: a dead selector logs and falls
  back, a short body is stored with `thin = true` and surfaces in `nbk stats`.
  Silent empty results are the failure mode this codebase is built to avoid.

See [README.md](README.md) for layout and how to run it.
