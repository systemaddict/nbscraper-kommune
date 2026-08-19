# nbscraper-kommune

Scraper for **nyheder og pressemeddelelser** from all 98 Danish kommuner.
Discover article URLs, extract the article, store it in Bunny Database.

Independent of the byrådsmøde scraper: its own repo, its own Bunny Database, its
own `NBK_` config namespace, no shared code.

## Why it is built this way

The meeting scraper covers 98 kommuner with ~10 platform backends, because
kommuner all buy the same agenda portals. News is the opposite: **98 hosts, no
dominant CMS.** A module per site is not viable, so this is one generic pipeline
with layered fallbacks at both ends, and per-site **config** rather than per-site
code.

### Discovery: one channel per site, resolved automatically

| Channel | What it gives | How common |
|---|---|---|
| `feed` | title + a real `datePublished` | rare — 2 of 14 sampled sites |
| `listing` | title + often the only publication date that exists | works unaided on 5 of 8 sampled listings |
| `sitemap` | URL + `<lastmod>` only — no title, no publication date | every reachable site served one |

Resolution order is **feed → listing → sitemap**, by *metadata quality* rather
than reliability. That is deliberate: the sitemap looks like the safe default and
is not. Skanderborg's sitemap lists 7 URLs under `/nyheder` where its listing
page yields 12+, and a sitemap carries neither title nor publication date. It is
the fallback for sites whose markup defeats the listing heuristic — valuable
precisely there.

### Extraction: layered, with provenance

Each field resolves through `jsonld` → `meta` → `readability` → `heuristic` →
`listing`, and the layer that won is recorded per article in `provenance_json`.
That is not decoration: with 98 heterogeneous sites, the only way to notice a
site's extraction has silently degraded is to ask *which sites are still falling
through to the heuristic layer?*

For the **body** the rule follows the SerpScraper4 extension's Readability+schema
logic: a schema.org `articleBody` wins when it is substantial (`SCHEMA_MIN_WORDS
= 100`), otherwise the **longer** of two DOM extractors wins — Mozilla
Readability (`readability-lxml`) and the built-in density heuristic. Running both
beats trusting either, measured on five kommune article pages:

| page | heuristic | readability | defuddle |
|---|---|---|---|
| jammerbugt | **366** | 139 | 368 |
| lolland | **368** | 303 | 337 |
| skanderborg | 362 | **366** | 376 |
| kerteminde | **76** | 54 | 76 |
| frederikssund | 243 | **246** | 246 |

Every candidate's word count is kept in `raw.body_candidates`, so a survey can
answer "which extractor is carrying which sites?" without re-fetching.

**On defuddle** ([kepano/defuddle](https://github.com/kepano/defuddle)): evaluated
and **not adopted**. Its body extraction is marginally better on 2 of 6 pages
(+2 and +10 words) and worse on one (−31), which does not pay for a Node runtime
inside a Python service. More decisively, its metadata is unsafe here: it
reported `published: 2026-08-14` for the Skanderborg article, whose page contains
**no publication date at all** — only `cmspageupdated` and the visible text
"Opdateret: 14 august 2026" (*Updated:*). Promoting a modification stamp to a
publication date is precisely the error this pipeline exists to prevent.

Publication date is the hardest field. Of five sampled article pages, **one** had
a JSON-LD `datePublished`, **three** exposed only a `cmspageupdated` modification
stamp, and **one** had nothing at all. So a listing's date outranks a page-level
modification stamp, and `published_at` and `updated_at` are never conflated.

## Layout

```
nbkommune/
  settings.py      NBK_* plus Bunny Database config. Read via get_settings().
  targets.py       Target registry: which kommune, which channel, which config.
  registry.json    98 targets, generated from the verified URL survey.
  http.py          Polite client: robots, per-host gate, retry, Cloudflare/WAF,
                   per-host User-Agent rotation, explicit charset handling.
  dates.py         Danish date parsing → ISO 8601 UTC. Read the docstring.
  records.py       ListedArticle / ArticleDetail — the storage contract.
  extract.py       The layered article extractor.
  sources/
    feed.py        RSS/Atom
    sitemap.py     sitemap.xml (+ index), prefix-filtered
    listing.py     HTML listing: configured selectors or heuristic
    __init__.py    make_source / resolve_source
  crawl.py         discover_target + ingest_article
  worker.py        The task loop: fast and paced lanes, leases, janitor
  repositories.py  All SQLite-compatible SQL. Nothing else touches the DB.
  db.py            Bunny libSQL/HTTP adapter + schema (idempotent)
  api.py           Read-only status API; serves the dashboard from the same process
  static/          Dependency-free, shadcn-inspired operational dashboard
  serve.py         Supervises FastAPI behind the Better Auth gateway
  cli.py           The nbk CLI
auth/
  src/             Better Auth + Hono gateway backed by Bunny Database/libSQL
  static/          Login page
scripts/
  build_registry.py  Regenerate registry.json from the survey CSV
  probe_sites.py     Survey every site: channel, dates, extraction health
config/
  targets.json     Per-site overrides, merged over registry.json (gitignored)
```

## Running it

```bash
python3 -m venv .venv && .venv/bin/pip install -e ".[dev]"
cp .env.example .env
# For local-only development, set BUNNY_DATABASE_URL=file:./nbkommune.db
.venv/bin/nbk init-db
```

```bash
nbk targets --enabled              # the registry
nbk resolve aarhus odder           # what channel does a site resolve to? (no writes)
nbk crawl --now                    # discovery inline
nbk crawl                          # or: queue discovery for the worker
nbk worker                         # the service: pops and executes tasks
nbk worker --once --max-tasks 20   # drain what is due, then exit
nbk serve                          # authenticated status API + dashboard on :8000
nbk stats                          # per-kommune corpus + extraction health
nbk queue / nbk errors             # what is pending, what is failing
nbk backfill aarhus --limit 200    # ingest the still-listed backlog
nbk retry 1234                     # requeue a dead task
```

Survey every site and find the ones needing config:

```bash
python scripts/probe_sites.py --json survey.json
```

## Bunny deployment

Production uses the same image for two Magic Containers: a private worker that
starts with `nbk worker`, and a small public web container that starts with
`nbk serve --port 8000`. The worker keeps exactly one replica. That is deliberate
because the queue and per-host pacing gate are global and Bunny Database uses
SQLite's single-writer model. The web process is read-only and opens a short
database connection for each status poll.

Bunny Database provides these variables to the container:

```text
BUNNY_DATABASE_URL
BUNNY_DATABASE_AUTH_TOKEN
```

The dashboard uses Better Auth email/password sessions stored in the same Bunny
Database. Only `/healthz`, `/login`, and Better Auth's `/api/auth/*` handler are
public. `/`, `/api/status`, and `/api/articles` require a valid session. Public
signup is disabled; the first user is created from deployment-only bootstrap
credentials.

The article table includes native FTS5 search across title, summary and body.
Use the dashboard search box or `GET /api/articles?q=cykelsti`; search can be
combined with the municipality, type and status filters. Results are ranked by
relevance, with title matches weighted highest. The index is backfilled once on
upgrade and then maintained by database triggers.

Set these variables on the dashboard container (not the worker):

```text
NBK_AUTH_ENABLED=true
NBK_AUTH_BASE_URL=https://your-dashboard.example.com
NBK_AUTH_SECRET=<at least 32 random characters>
NBK_AUTH_BOOTSTRAP_EMAIL=<initial login email>
NBK_AUTH_BOOTSTRAP_PASSWORD=<initial password, at least 8 characters>
```

After the first successful login, the two `NBK_AUTH_BOOTSTRAP_*` values can be
removed. Existing users and sessions remain in Bunny Database.

Create and link a database with Bunny's CLI, or attach an existing database from
its **Access > Add Secrets to Magic Container App** screen:

```bash
bunny db create --name nbkommune --primary DK --link --token --save-env
```

Magic Containers accepts `linux/amd64` images only. For a manual release:

```bash
docker buildx build --platform linux/amd64 \
  -t ghcr.io/systemaddict/nbscraper-kommune:latest --push .
```

The GitHub Actions workflow publishes both `latest` and the commit SHA. Once the
Magic Container app exists, set repository variable `BUNNY_APP_ID` and secret
`BUNNYNET_API_KEY`; pushes to `main` then update the worker image automatically.

## How work flows

Discovery updates the world and files work orders; the worker executes them.

```
discover  →  article rows (status 'listed')  →  ingest tasks
                                                    ↓
                                          status 'ingested', body stored
```

- `article.status` is **world-state only**: `listed` → `ingested` → `gone`.
- All work-state — attempts, backoff, dead-lettering — lives on `scrape_task`.
- Every failure is an append-only `scrape_error` row.
- To make something happen, **enqueue**. Never set a status to signal work.
- `ingested_at` moves only on real content change (`detail_hash`); `checked_at`
  moves on every verification.

Two lanes: **fast** (new/changed articles, discovery, manual) runs the moment it
is due; **paced** (rechecks, backlog) runs at most one per
`NBK_PACED_TASK_INTERVAL_S` so a backfill can never starve fresh news.

`NBK_DISCOVER_ENQUEUE_CAP` bounds what one discovery pass queues per site — a
sitemap exposing a decade of archive would otherwise flood the queue on first
contact. The overflow stays `listed` and drains over subsequent passes at
backfill priority, so it is self-healing; `nbk backfill` forces it.

## Being a polite scraper

These are the kommuner's own public websites, serving real citizens, and we have
no deadline.

- `robots.txt` is honoured (`NBK_RESPECT_ROBOTS`, on by default), including
  `Crawl-delay`. An unreachable robots.txt fails **open**.
- `NBK_SCRAPE_MIN_INTERVAL_S` defaults to 2 s per host. Don't lower it to rush.
- The User-Agent identifies us with a contact URL.

### The User-Agent problem

Some of these WAFs refuse a request unless the UA begins with
`Mozilla/5.0 (compatible; …)`, **and** refuse any UA containing the tokens `bot`
or `scraper`. Measured on lolland.dk and kerteminde.dk: a bare
`nbscraper-kommune/0.1 (…)` UA gets a 13-byte "Access Denied" (HTTP 403), while
`Mozilla/5.0 (compatible; nbmedier-nyheder/0.1; +https://nbmedier.dk)` gets 200.

Since which identifier a host accepts is opaque and per-host, `http.py` rotates
through `NBK_USER_AGENT_FALLBACKS` on a refusal and remembers what worked per
host. This rotates **honest identifiers only** — each names us and carries a
contact URL. It is not browser impersonation, there is no fingerprint forgery,
and robots.txt stays authoritative regardless of which identifier is in use. Set
`NBK_USER_AGENT_FALLBACKS=` to disable it and let refusals stand.

A separate case: a WAF that refuses with **HTTP 200** and a short "Access Denied"
body. That is detected too — undetected it would store an empty article that
looks like a successful ingest.

### When a host refuses everything: scrape.do

If a host refuses *every* identifier in the rotation (401/403/451, or a 200
soft-block), and `NBK_SCRAPEDO_TOKEN` is set, the host is escalated to the
**scrape.do** proxy and the request is retried through it; the host then stays
routed there for `NBK_SCRAPEDO_FALLBACK_TTL_S`. Gentofte needs exactly this — a
plain 403 with no Cloudflare fingerprint, from a WAF that has decided about our
network rather than our User-Agent. Escalation fires only *after* the rotation is
exhausted, so it costs credits solely for hosts that genuinely refuse everyone,
and a 404 is never escalated (no proxy conjures up a deleted article).

The proxy URL carries the token, so it is never allowed to escape: `get_text`
returns the **origin** URL, not `resp.url`, and 5xx failures raise a
`TransientHttpError` naming the origin. Without both, the token would be written
into the database as the article's identity and into `scrape_error` as its
message. There are tests asserting exactly that.

`NBK_SCRAPEDO_RENDER_HOSTS` additionally requests JS rendering for named hosts —
it grows taarnby.dk's HTML from 12 KB to 111 KB. It is opt-in because it costs
more and doubles latency, and because it is not always sufficient: a list
populated by a later XHR still needs a wait selector or the site's own JSON
endpoint.

**Channel resolution is sticky.** A fresh `auto` resolution costs up to a dozen
requests, so the resolved channel and its parameters are stored on the
municipality row and reused. Re-probing every pass is slow, wasteful and impolite
— and through the proxy it timed a discovery pass out entirely (8m49s → 11s once
cached). Use `nbk reresolve <key>` when a site changes.

## Known gaps

- **All 98 kommuner are enabled.** Tårnby, Hvidovre, Middelfart, Skive and
  Thisted render their visible lists client-side, but their official sitemaps
  provide stable discovery without paying for browser rendering on every pass.
- **3 sites are flagged `verified: false`** in the registry (Esbjerg, Ringsted,
  Skanderborg) — the URL survey could not confirm their press archive. They still
  crawl; the flag records that the source is shaky.
- **Gentofte** 403s every identifier and is escalated to scrape.do
  automatically, which returns its pages — but its news list is client-side and
  is *not* recovered by `render=true` either (the list arrives in a later XHR).
  It still yields 0 articles and needs its JSON endpoint or a wait selector.
- **Many sites supply no publication date at all** — see `nbk stats`, `undated`
  column. Those articles are stored with `published_at = NULL` rather than being
  dated by a modification stamp. Fixing a site means finding a `date_selector`
  for its listing.

## Tests

```bash
.venv/bin/python -m pytest -q
```

106 tests, no network and no database required. They encode the real traps: the
`fromisoformat` dotted-time misparse, the over-stripping guard, the block
detectors, the UA rotation, and the discovery decision table.
