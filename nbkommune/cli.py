"""The ``nbk`` CLI — init the DB, inspect targets, crawl, ingest, run the worker.

Control commands only ever *enqueue*; the worker executes. The one exception is
``crawl --now`` / ``ingest``, which run inline so an operator can debug a single
site without a worker running.
"""
from __future__ import annotations

import logging
from typing import Optional

import typer
from rich.console import Console
from rich.table import Table

from nbkommune import db
from nbkommune import repositories as repo
from nbkommune.crawl import discover_target, ingest_article
from nbkommune.http import HttpClient
from nbkommune.settings import get_settings
from nbkommune.sources import make_source
from nbkommune.targets import registry, selected_targets
from nbkommune.worker import run_worker

app = typer.Typer(add_completion=False, help="Kommune news + press-release scraper.")
console = Console()


def _setup_logging(verbose: bool) -> None:
    logging.basicConfig(
        level=logging.DEBUG if verbose else logging.INFO,
        format="%(asctime)s %(levelname)-7s %(name)s: %(message)s",
        datefmt="%H:%M:%S",
    )
    # httpx logs every request at INFO, which drowns our own lines.
    logging.getLogger("httpx").setLevel(logging.WARNING)


@app.command("init-db")
def init_db() -> None:
    """Create the schema and all tables (idempotent)."""
    _setup_logging(False)
    settings = get_settings()
    conn = db.connect(settings)
    db.init_schema(conn, settings)
    console.print("[green]Bunny Database schema ready[/green]")
    conn.close()


@app.command("targets")
def list_targets(
    enabled_only: bool = typer.Option(False, "--enabled", help="Only enabled targets."),
    channel: Optional[str] = typer.Option(None, help="Filter by configured channel."),
) -> None:
    """List the target registry."""
    reg = registry()
    table = Table("key", "name", "channel", "type", "on", "news url", "note")
    for target in sorted(reg.values(), key=lambda t: t.key):
        if enabled_only and not target.enabled:
            continue
        if channel and target.channel != channel:
            continue
        table.add_row(
            target.key, target.name, target.channel, target.source_type,
            "yes" if target.enabled else "no",
            (target.news_url or target.press_url or "")[:44],
            (target.note or "")[:34],
        )
    console.print(table)
    console.print(f"{len(reg)} targets, {sum(1 for t in reg.values() if t.enabled)} enabled")


@app.command("resolve")
def resolve(
    keys: list[str] = typer.Argument(..., help="Target keys to resolve."),
    verbose: bool = typer.Option(False, "--verbose", "-v"),
) -> None:
    """Show which discovery channel each target resolves to, without storing.

    The fastest way to see what a site actually offers — and, on a fresh
    registry, the way to find the sites that will need hand-written selectors.
    """
    _setup_logging(verbose)
    reg = registry()
    with HttpClient() as http:
        for key in keys:
            target = reg.get(key)
            if target is None:
                console.print(f"[red]unknown target {key}[/red]")
                continue
            try:
                source = make_source(target, http)
                found = source.list_articles()
                dated = sum(1 for a in found if a.published_at)
                console.print(
                    f"[green]{key}[/green]: channel=[bold]{source.channel}[/bold] "
                    f"articles={len(found)} with_date={dated}\n    {source.detail}"
                )
                for article in found[:3]:
                    console.print(f"      · {(article.title or '(no title)')[:60]!r} "
                                  f"{article.published_at or article.updated_at or '-'}")
            except Exception as exc:
                console.print(f"[red]{key}: {type(exc).__name__}: {exc}[/red]")


@app.command("reresolve")
def reresolve(
    keys: list[str] = typer.Argument(..., help="Target keys to re-probe."),
) -> None:
    """Forget the stored channel for these kommuner so the next crawl re-probes.

    Needed when a site gains a feed, moves its listing, or its markup changes —
    discovery otherwise keeps using what it resolved the first time.
    """
    _setup_logging(False)
    settings = get_settings()
    conn = db.connect(settings)
    cleared = repo.clear_resolution(conn, keys)
    conn.commit()
    console.print(f"[green]cleared the stored channel for {cleared} kommune(s)[/green]")
    conn.close()


@app.command("crawl")
def crawl(
    keys: Optional[list[str]] = typer.Argument(None, help="Targets (default: all enabled)."),
    now: bool = typer.Option(False, "--now", help="Run discovery inline instead of enqueueing."),
    verbose: bool = typer.Option(False, "--verbose", "-v"),
) -> None:
    """Discover articles: enqueue a discover task per target, or run it inline."""
    _setup_logging(verbose)
    settings = get_settings()
    conn = db.connect(settings)
    db.init_schema(conn, settings)
    reg = registry(settings)
    targets = ([reg[k] for k in keys if k in reg] if keys
               else selected_targets(settings))
    if keys:
        for key in keys:
            if key not in reg:
                console.print(f"[yellow]unknown target {key} — skipping[/yellow]")
    if not now:
        seeded = 0
        for target in targets:
            repo.upsert_municipality(conn, target)
            if repo.enqueue_task(conn, kind="discover", municipality_key=target.key,
                                 reason="manual", priority=repo.PRIORITY_MANUAL,
                                 max_attempts=0) is not None:
                seeded += 1
        conn.commit()
        console.print(f"[green]queued discovery for {seeded} target(s)[/green]")
        conn.close()
        return

    with HttpClient(settings) as http:
        for target in targets:
            try:
                stats = discover_target(conn, target, http, settings)
                console.print(
                    f"[green]{target.key}[/green]: seen={stats.seen} new={stats.new} "
                    f"changed={stats.updated} pending={stats.pending} "
                    f"queued={stats.queued} skipped_old={stats.skipped_old}")
            except Exception as exc:
                conn.rollback()
                repo.record_error(conn, phase="list", municipality_key=target.key, exc=exc)
                console.print(f"[red]{target.key}: {type(exc).__name__}: {exc}[/red]")
    conn.close()


@app.command("ingest")
def ingest(
    key: str = typer.Argument(..., help="Target key."),
    article_id: Optional[str] = typer.Option(None, help="One article id (default: all listed)."),
    limit: int = typer.Option(10, help="Max articles when ingesting all listed."),
    verbose: bool = typer.Option(False, "--verbose", "-v"),
) -> None:
    """Fetch and store article bodies inline (debugging aid)."""
    _setup_logging(verbose)
    settings = get_settings()
    conn = db.connect(settings)
    target = registry(settings).get(key)
    if target is None:
        console.print(f"[red]unknown target {key}[/red]")
        raise typer.Exit(1)
    ids = ([article_id] if article_id else
           [r["id"] for r in repo.list_articles(conn, municipality_key=key,
                                                status="listed", limit=limit)])
    if not ids:
        console.print("[yellow]nothing listed to ingest[/yellow]")
        conn.close()
        return
    with HttpClient(settings) as http:
        for aid in ids:
            try:
                changed = ingest_article(conn, target, http, aid, settings)
                console.print(f"  {aid} → {'changed' if changed else 'unchanged'}")
            except Exception as exc:
                conn.rollback()
                repo.record_error(conn, phase="detail", municipality_key=key,
                                  article_id=aid, exc=exc)
                console.print(f"  [red]{aid}: {type(exc).__name__}: {exc}[/red]")
    conn.close()


@app.command("worker")
def worker(
    once: bool = typer.Option(False, "--once", help="Drain what is due, then exit."),
    max_tasks: Optional[int] = typer.Option(None, help="Stop after this many tasks."),
    verbose: bool = typer.Option(False, "--verbose", "-v"),
) -> None:
    """Run the worker loop."""
    _setup_logging(verbose)
    executed = run_worker(get_settings(), once=once, max_tasks=max_tasks)
    console.print(f"[green]executed {executed} task(s)[/green]")


@app.command("stats")
def stats(
    thin_only: bool = typer.Option(False, "--thin", help="Only kommuner with thin extractions."),
) -> None:
    """Per-kommune corpus and extraction health."""
    _setup_logging(False)
    conn = db.connect(get_settings())
    table = Table("key", "channel", "articles", "ingested", "listed", "gone",
                  "thin", "undated", "last ok")
    rows = repo.article_stats(conn)
    for row in rows:
        if thin_only and not row["thin"]:
            continue
        table.add_row(
            row["key"], row["channel"] or "-", str(row["articles"]),
            str(row["ingested"]), str(row["listed"]), str(row["gone"]),
            f"[yellow]{row['thin']}[/yellow]" if row["thin"] else "0",
            f"[yellow]{row['undated']}[/yellow]" if row["undated"] else "0",
            row["last_ok_at"].strftime("%m-%d %H:%M") if row["last_ok_at"] else "never",
        )
    console.print(table)
    totals = {k: sum(r[k] for r in rows) for k in ("articles", "ingested", "thin", "undated")}
    console.print(f"total: {totals['articles']} articles, {totals['ingested']} ingested, "
                  f"{totals['thin']} thin, {totals['undated']} undated")
    conn.close()


@app.command("queue")
def queue(
    status: Optional[str] = typer.Option(None, help="queued | running | done | dead | cancelled"),
    kind: Optional[str] = typer.Option(None, help="discover | ingest | recheck"),
    limit: int = typer.Option(30),
) -> None:
    """Show the task queue."""
    _setup_logging(False)
    conn = db.connect(get_settings())
    summary = Table("kind", "status", "n")
    for row in repo.queue_summary(conn):
        summary.add_row(row["kind"], row["status"], str(row["n"]))
    console.print(summary)
    table = Table("id", "kind", "kommune", "article", "prio", "status", "try", "run after", "error")
    for row in repo.list_tasks(conn, status=status, kind=kind, limit=limit):
        table.add_row(
            str(row["id"]), row["kind"], row["municipality_key"],
            (row["article_id"] or "-")[:10], str(row["priority"]), row["status"],
            f"{row['attempts']}/{row['max_attempts'] or '∞'}",
            row["run_after"].strftime("%m-%d %H:%M") if row["run_after"] else "-",
            (row["last_error"] or "")[:38],
        )
    console.print(table)
    conn.close()


@app.command("errors")
def errors(
    hours: float = typer.Option(24.0, help="Summary window."),
    key: Optional[str] = typer.Option(None, help="Only this kommune."),
    limit: int = typer.Option(20),
) -> None:
    """Recent failures, summarised and listed."""
    _setup_logging(False)
    conn = db.connect(get_settings())
    summary = Table("phase", "error", "n", "latest")
    for row in repo.error_summary(conn, hours=hours):
        summary.add_row(row["phase"], row["error_type"], str(row["n"]),
                        row["latest"].strftime("%m-%d %H:%M") if row["latest"] else "-")
    console.print(summary)
    table = Table("when", "phase", "kommune", "error", "message")
    for row in repo.recent_errors(conn, limit=limit, municipality_key=key):
        table.add_row(
            row["created_at"].strftime("%m-%d %H:%M") if row["created_at"] else "-",
            row["phase"], row["municipality_key"] or "-", row["error_type"] or "-",
            (row["message"] or "")[:60],
        )
    console.print(table)
    conn.close()


@app.command("retry")
def retry(task_id: int = typer.Argument(..., help="Task id to requeue.")) -> None:
    """Requeue a dead task with a fresh attempt budget."""
    _setup_logging(False)
    conn = db.connect(get_settings())
    row = repo.retry_task(conn, task_id)
    console.print(f"[green]requeued #{task_id}[/green]" if row
                  else f"[yellow]task #{task_id} is not dead or queued[/yellow]")
    conn.close()


@app.command("backfill")
def backfill(
    key: str = typer.Argument(..., help="Target key."),
    limit: int = typer.Option(200, help="Max articles to enqueue."),
) -> None:
    """Queue every still-``listed`` article for this kommune, at paced priority.

    This is how the overflow left behind by ``discover_enqueue_cap`` gets picked
    up — deliberately, at a priority that cannot starve fresh news.
    """
    _setup_logging(False)
    settings = get_settings()
    conn = db.connect(settings)
    rows = repo.list_articles(conn, municipality_key=key, status="listed", limit=limit)
    queued = 0
    for row in rows:
        if repo.enqueue_ingest(conn, key, row["id"], "backfill") is not None:
            queued += 1
    conn.commit()
    console.print(f"[green]queued {queued} backfill task(s) for {key}[/green]")
    conn.close()


if __name__ == "__main__":
    app()
