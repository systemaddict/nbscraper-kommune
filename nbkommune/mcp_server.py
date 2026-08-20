"""MCP facade over the kommune-news full-text article search.

The caller's model supplies the natural-language layer: it maps a user's
question to concrete Danish search terms and optional filters, then reasons
over the ranked results. This server does not call an LLM and does not maintain
a second index; it uses the same repository function and FTS5 index as the
dashboard's article search.

Two transports share this definition:

* stdio is a local subprocess with no network surface and no auth;
* streamable HTTP is protected by configured static bearer tokens.
"""
from __future__ import annotations

from typing import Any, Literal

from fastmcp import FastMCP
from fastmcp.server.auth.providers.jwt import StaticTokenVerifier

from nbkommune import db
from nbkommune import repositories as repo
from nbkommune.settings import Settings, get_settings

_INSTRUCTIONS = """\
NB Kommune — søgning i nyheder og pressemeddelelser fra danske kommuner.

Brug `search_articles` til både emnesøgning og seneste nyt. Korpusset er dansk:
omsæt brugerens spørgsmål til få, konkrete danske nøgleord i `query` frem for
at sende en lang sætning. En tom `query` viser de nyeste artikler. Kombinér med
kommune, type, status og kilde, når spørgsmålet gør det muligt.

Resultaterne kommer fra samme fuldtekstsøgning som dashboardet og rangeres med
titelmatches højest. Hvert resultat har et `url`-felt; brug det til kildehenvisning.
"""


def _build_auth(settings: Settings) -> StaticTokenVerifier:
    """Build the same fail-closed static bearer verifier as NBSubstans MCP."""
    settings.require_mcp_auth()
    tokens = {
        token: {"client_id": f"nb-kommune-mcp-{index + 1}", "scopes": []}
        for index, token in enumerate(settings.mcp_auth_tokens)
    }
    return StaticTokenVerifier(tokens=tokens)


def _search_articles(
    settings: Settings,
    *,
    query: str = "",
    municipality: str | None = None,
    kind: Literal["nyhed", "pressemeddelelse"] | None = None,
    status: Literal["listed", "ingested", "gone"] | None = None,
    source: Literal["website", "email"] | None = None,
    limit: int = 25,
    offset: int = 0,
) -> dict[str, Any]:
    """Run one bounded search using the dashboard's repository surface."""
    if len(query) > 200:
        raise ValueError("query must be at most 200 characters")
    bounded_limit = max(1, min(int(limit), 100))
    bounded_offset = max(0, int(offset))
    conn = db.connect(settings)
    try:
        search = query or None
        return {
            "items": repo.list_articles(
                conn,
                municipality_key=municipality,
                kind=kind,
                status=status,
                search=search,
                source_type=source,
                limit=bounded_limit,
                offset=bounded_offset,
            ),
            "total": repo.count_articles(
                conn,
                municipality_key=municipality,
                kind=kind,
                status=status,
                search=search,
                source_type=source,
            ),
            "limit": bounded_limit,
            "offset": bounded_offset,
        }
    finally:
        conn.close()


def build_mcp_server(*, auth: bool = False, settings: Settings | None = None) -> FastMCP:
    """Build the MCP server for local stdio or authenticated remote HTTP."""
    settings = settings or get_settings()
    mcp: FastMCP = FastMCP(
        name="nb-kommune",
        version="0.1.0",
        instructions=_INSTRUCTIONS,
        auth=_build_auth(settings) if auth else None,
    )

    @mcp.tool(
        title="Søg i kommunale nyheder og pressemeddelelser",
        annotations={
            "readOnlyHint": True,
            "destructiveHint": False,
            "idempotentHint": True,
            "openWorldHint": False,
        },
    )
    def search_articles(
        query: str = "",
        municipality: str | None = None,
        kind: Literal["nyhed", "pressemeddelelse"] | None = None,
        status: Literal["listed", "ingested", "gone"] | None = None,
        source: Literal["website", "email"] | None = None,
        limit: int = 25,
        offset: int = 0,
    ) -> dict[str, Any]:
        """Search Danish municipality news and press releases.

        `query` is ordinary full text, not query syntax. Prefer a few concrete
        Danish keywords; use `query=""` for the newest matching articles.
        `municipality` is the lowercase municipality key, for example
        `aarhus`, `koebenhavn` or `alleroed`. `kind` selects news versus press
        releases, `status="ingested"` limits results to fully fetched articles,
        and `source` selects municipality websites versus received emails.
        Results are relevance-ranked when `query` is set and newest-first when
        it is empty. `limit` is clamped to 1–100; use `offset` to fetch the
        next page when `total` is larger than the returned item count.
        """
        return _search_articles(
            settings,
            query=query,
            municipality=municipality,
            kind=kind,
            status=status,
            source=source,
            limit=limit,
            offset=offset,
        )

    return mcp
