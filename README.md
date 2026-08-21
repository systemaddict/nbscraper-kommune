# nbscraper-kommune

Scraper for **nyheder og pressemeddelelser** from all 98 Danish kommuner.
Discover article URLs and municipality inbox messages, extract the content,
and store it in Bunny Database.

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
  gmail.py         Gmail REST client and safe MIME parsing
  email_classifier.py  cached sender routing + constrained OpenRouter fallback
  email_ingest.py  inbox collection and article/source promotion
  worker.py        The task loop: fast and paced lanes, leases, janitor
  repositories.py  All SQLite-compatible SQL. Nothing else touches the DB.
  db.py            Bunny libSQL/HTTP adapter + schema (idempotent)
  api.py           Read-only status API; serves the dashboard from the same process
  mcp_server.py    Natural-language tool facade over the same article search
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
nbk gmail                          # queue the singleton inbox collector
nbk gmail --now                    # poll Gmail inline for diagnostics
nbk worker --once --max-tasks 20   # drain what is due, then exit
nbk serve                          # authenticated status API + dashboard on :8000
nbk mcp                            # local MCP over stdio
nbk mcp --http                     # remote MCP on :8766 with OAuth
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
SQLite's single-writer model. The web process opens a short database connection
for each request; only authenticated inbox-review actions write through it.

Bunny Database provides these variables to the container:

```text
BUNNY_DATABASE_URL
BUNNY_DATABASE_AUTH_TOKEN
```

The dashboard uses Better Auth email/password sessions stored in the same Bunny
Database. Only `/healthz`, `/login`, and Better Auth's `/api/auth/*` handler are
public. `/`, `/api/status`, and `/api/articles` require a valid session. The
article list additionally accepts the dedicated read-only `NBK_API_TOKEN` as a
Bearer token so another NB service can consume it without a browser session.
The token does not grant access to status or admin/write routes. Public signup
is disabled; the first user is created from deployment-only bootstrap credentials.

The article table includes native FTS5 search across title, summary and body.
Use the dashboard search box or `GET /api/articles?q=cykelsti`; search can be
combined with the municipality, type and status filters. Results are ranked by
relevance, with title matches weighted highest. The index is backfilled once on
upgrade and then maintained by database triggers.

For server-to-server access, set a random token on the dashboard container and
send it in the standard header:

```text
NBK_API_TOKEN=<at least 32 random characters>
Authorization: Bearer <the same value>
```

```bash
curl -H "Authorization: Bearer $NBK_API_TOKEN" \
  "$NBK_API_URL/api/articles?status=ingested&limit=100&offset=0"
```

### MCP access for Claude, ChatGPT and other clients

The MCP server exposes one model-facing tool, `search_articles`, backed by the
same FTS5 index and repository query as the dashboard search. The client model
turns a natural-language question into Danish keywords plus optional filters
for municipality, article type, ingestion status and source. No extra LLM or
search index runs in this service, and every result contains the original URL
for citations.

There are two transports:

- `nbk mcp` uses stdio for a local client. It has no network surface or auth;
  give the subprocess the database environment variables.
- `nbk mcp --http` uses remote streamable HTTP at `/mcp`. ChatGPT and Claude
  discover the OAuth 2.1 authorization server automatically and send the user
  through the existing dashboard login and a read-only consent screen.

For remote deployment, build [Dockerfile.mcp](Dockerfile.mcp) as its own
service and attach the same Bunny Database as the worker/dashboard:

```text
BUNNY_DATABASE_URL=libsql://...
BUNNY_DATABASE_AUTH_TOKEN=...
NBK_MCP_BASE_URL=https://<mcp-host>
NBK_MCP_OAUTH_ISSUER=https://<dashboard-host>/api/auth
NBK_MCP_OAUTH_JWKS_URL=https://<dashboard-host>/api/auth/jwks
```

```bash
docker build -f Dockerfile.mcp -t nbscraper-kommune-mcp .
# Client URL: https://<mcp-host>/mcp
```

The dashboard's Better Auth service provides dynamic client registration,
authorization-code + PKCE, consent, refresh tokens, discovery metadata and a
public JWKS. The MCP service validates resource-bound JWTs and requires the
`search:articles` scope. It is read-only and does not expose dashboard status,
Gmail or admin actions.

### Gmail inbox ingestion

Gmail is opt-in. The worker polls a dedicated label, parses MIME content, and
stores each Gmail message id exactly once. Municipality assignment uses this
order:

1. a remembered exact sender decision;
2. an unambiguous municipality domain from `registry.json`;
3. an exact display name such as `Egedal Kommune`;
4. a schema-constrained OpenRouter classification over the cleaned message and
   the canonical 98-municipality list.

High-confidence fixed senders are remembered, so OpenRouter is called once.
Shared services such as FirstAgenda are stored as `classify_each` and resolved
from each message's subject/body instead. Low-confidence results enter the
authenticated dashboard review queue rather than being guessed.

Create one Google Cloud OAuth client of type **Web application**, enable the
Gmail API, and register this exact redirect URI:

```text
https://your-dashboard.example.com/api/admin/gmail/callback
```

Set these values, identically, on both the dashboard and worker containers. The
encryption key can be generated with `openssl rand -hex 32`:

```text
NBK_GMAIL_ENABLED=true
NBK_GMAIL_CLIENT_ID=...
NBK_GMAIL_CLIENT_SECRET=...
NBK_GMAIL_TOKEN_ENCRYPTION_KEY=<at least 32 random characters>
NBK_GMAIL_QUERY=label:kommune-news
```

Set the classifier secret on the worker only:

```text
NBK_OPENROUTER_API_KEY=...
```

Then log into the dashboard and click **Forbind Gmail**. Google presents the
read-only consent screen and returns to the dashboard. The refresh token is
stored encrypted in Bunny Database; admins never copy a user token. The OAuth
transaction uses one-use state bound to the signed-in dashboard account plus
PKCE. **Genforbind Gmail** rotates the grant, and **Afbryd** revokes it at Google
and removes it locally.

For a Google Workspace-owned project, configure the OAuth audience as Internal.
If Workspace API access is restricted, add the client id in Admin Console under
Security → Access and data control → API controls, preferably allowing only the
Gmail read-only data requested by this app.

The OpenRouter key can use the same secret value as the sibling nbscraper
deployment, but it is configured independently as `NBK_OPENROUTER_API_KEY` and
is never stored in the repository or database. Only sender, subject, cleaned
body text, and extracted links are sent for unknown/shared senders. Quoted
threads, MIME attachments, and raw message data are excluded.

`article_source` records website and email renditions separately. A matching
email therefore adds provenance and supplemental text without overwriting the
canonical website body. Existing website articles are backfilled into this
table when the schema upgrade first runs.

Set these variables on the dashboard container (not the worker):

```text
NBK_AUTH_ENABLED=true
NBK_AUTH_BASE_URL=https://your-dashboard.example.com
NBK_AUTH_SECRET=<at least 32 random characters>
NBK_AUTH_BOOTSTRAP_EMAIL=<initial login email>
NBK_AUTH_BOOTSTRAP_PASSWORD=<initial password, at least 8 characters>
NBK_API_TOKEN=<at least 32 random characters; dashboard container only>
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
endpoint. `NBK_SCRAPEDO_RENDER_WAIT_MS` controls that post-load wait (3 seconds
by default); Gentofte's news cards are present after the wait but absent from an
immediate rendered response.

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
- **Gentofte and Herlev** require scrape.do rendering. Gentofte's XHR-backed
  listing additionally needs `NBK_SCRAPEDO_RENDER_WAIT_MS`; Herlev's article
  cards live on the year archive rather than the `/nyheder` root. Both are
  configured and covered by live source checks.
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
