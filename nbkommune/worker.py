"""The worker loop — one process pops work orders and executes them.

Two lanes:

- **fast** (priority >= ``FAST_LANE_MIN``): new/changed articles, discovery,
  manual requeues — executed the moment they are due, so a press release is
  stored within seconds of being discovered. Only the per-host politeness gate
  throttles here.
- **paced** (below): rechecks and backfills — at most one per
  ``paced_task_interval_s``, so a bulk backfill drains gently in the background
  without ever starving fresh work.

Crash-safety is structural: a claimed task holds a lease; if the process dies the
janitor requeues it once the lease expires (the attempt was counted at claim
time, so crash loops still converge on 'dead'). Failures land in the append-only
``scrape_error`` log and are retried with exponential backoff until the attempts
run out. Discover tasks self-reschedule and never die; if a chain is ever lost,
``ensure_discover_tasks`` re-seeds it on the next janitor pass.
"""
from __future__ import annotations

import contextlib
import logging
import signal
import time
from datetime import UTC, datetime

from nbkommune import db
from nbkommune import repositories as repo
from nbkommune.crawl import discover_target, ingest_article
from nbkommune.email_ingest import collect_gmail
from nbkommune.gmail_oauth import gmail_connection_available
from nbkommune.http import HttpClient
from nbkommune.settings import Settings, get_settings
from nbkommune.targets import Target, registry, selected_targets

logger = logging.getLogger(__name__)

_PACED_GATE_KEY = "last_paced_task_at"
_JANITOR_INTERVAL_S = 60.0
_PURGE_INTERVAL_S = 3600.0
_IDLE_SLEEP_S = 2.0

# scrape_error phase for a task that failed as a whole.
_PHASE_BY_KIND = {
    "discover": "list", "ingest": "detail", "recheck": "detail",
    "collect_email": "email",
}


class WorkerContext:
    def __init__(self, conn, http: HttpClient, settings: Settings) -> None:
        self.conn = conn
        self.http = http
        self.settings = settings
        self._last_janitor = 0.0
        self._last_purge = 0.0
        self.stopping = False


def _resolve_target(ctx: WorkerContext, key: str) -> Target | None:
    """Registry first; fall back to the stored municipality row, so articles of a
    kommune since removed from config can still be worked."""
    target = registry(ctx.settings).get(key)
    if target is not None:
        return target
    row = repo.get_municipality(ctx.conn, key)
    if row is None:
        return None
    return Target(
        key=row["key"], name=row["name"], site_url=row["site_url"] or "",
        news_url=row["news_url"] or "", press_url=row["press_url"] or "",
        channel=row["channel"] or "auto", source_type=row["source_type"] or "faelles",
        enabled=bool(row["enabled"]), note=row["note"] or "",
    ).normalised()


# ── executors ────────────────────────────────────────────────────────────────
def _exec_discover(ctx: WorkerContext, task: dict) -> None:
    target = _resolve_target(ctx, task["municipality_key"])
    if target is None:
        raise ValueError(f"unknown target {task['municipality_key']!r}")
    discover_target(ctx.conn, target, ctx.http, ctx.settings)


def _exec_ingest(ctx: WorkerContext, task: dict) -> None:
    target = _resolve_target(ctx, task["municipality_key"])
    if target is None:
        raise ValueError(f"unknown target {task['municipality_key']!r}")
    if not task["article_id"]:
        raise ValueError(f"task #{task['id']} has kind {task['kind']!r} but no article_id")
    ingest_article(ctx.conn, target, ctx.http, task["article_id"], ctx.settings)


def _exec_collect_email(ctx: WorkerContext, _task: dict) -> None:
    if not ctx.settings.gmail_enabled:
        logger.info("Gmail collector is disabled; retiring queued collector task")
        return
    if not gmail_connection_available(ctx.conn, ctx.settings):
        logger.info("Gmail is not connected; retiring queued collector task")
        return
    stats = collect_gmail(ctx.conn, ctx.settings)
    logger.info(
        "Gmail: seen=%d ingested=%d ignored=%d review=%d duplicates=%d",
        stats.seen, stats.ingested, stats.ignored, stats.review, stats.duplicates,
    )


_EXECUTORS = {
    "discover": _exec_discover,
    "ingest": _exec_ingest,
    "recheck": _exec_ingest,   # a recheck IS a re-ingest; the hash decides if it changed
    "collect_email": _exec_collect_email,
}


def _after_success(ctx: WorkerContext, task: dict) -> None:
    """Post-completion chaining. Runs *after* complete_task so the live-task
    unique index cannot fold the follow-up into the just-finished row. Failures
    here are logged, not fatal: a lost discover chain is re-seeded by the
    janitor, a lost recheck by the next discovery that sees a change."""
    try:
        if task["kind"] == "discover":
            repo.enqueue_task(
                ctx.conn, kind="discover", municipality_key=task["municipality_key"],
                reason="schedule", priority=repo.PRIORITY_DISCOVER,
                run_after=repo._ahead(ctx.settings.discover_interval_min * 60.0),
                max_attempts=0,
            )
            ctx.conn.commit()
        elif (task["kind"] == "collect_email" and ctx.settings.gmail_enabled
              and gmail_connection_available(ctx.conn, ctx.settings)):
            repo.enqueue_task(
                ctx.conn, kind="collect_email", municipality_key="_gmail",
                reason="schedule", priority=repo.PRIORITY_EMAIL,
                run_after=repo._ahead(ctx.settings.gmail_poll_interval_min * 60.0),
                max_attempts=0,
            )
            ctx.conn.commit()
        elif task["kind"] in ("ingest", "recheck"):
            article = repo.get_article(ctx.conn, task["municipality_key"],
                                       task["article_id"])
            if article is not None and article["status"] == "ingested":
                published = article["published_at"]
                repo.schedule_recheck(
                    ctx.conn, task["municipality_key"], task["article_id"],
                    published_at=published.isoformat() if published else None,
                    settle_days=ctx.settings.recheck_settle_days,
                    interval_days=ctx.settings.recheck_interval_days,
                )
                ctx.conn.commit()
    except Exception:
        ctx.conn.rollback()
        logger.exception("post-task chaining failed for task #%s", task["id"])


def _run_task(ctx: WorkerContext, task: dict) -> bool:
    """Execute one claimed task; on failure record + backoff. Returns success."""
    conn = ctx.conn
    try:
        executor = _EXECUTORS.get(task["kind"])
        if executor is None:
            raise ValueError(f"no executor for task kind {task['kind']!r}")
        executor(ctx, task)
    except Exception as exc:
        conn.rollback()          # clear any aborted transaction before recording
        repo.record_error(
            conn, phase=_PHASE_BY_KIND.get(task["kind"], "task"),
            municipality_key=task["municipality_key"],
            article_id=task["article_id"], exc=exc, task=task,
        )
        outcome = repo.fail_task(
            conn, task, f"{type(exc).__name__}: {exc}",
            backoff_base_s=ctx.settings.task_backoff_base_s,
            backoff_max_s=ctx.settings.task_backoff_max_s,
        )
        logger.warning("task #%s (%s %s) failed → %s: %s", task["id"], task["kind"],
                       task["municipality_key"], outcome, exc)
        return False
    repo.complete_task(conn, task["id"])
    _after_success(ctx, task)
    return True


# ── janitor ──────────────────────────────────────────────────────────────────
def _janitor(ctx: WorkerContext) -> None:
    now = time.monotonic()
    if now - ctx._last_janitor < _JANITOR_INTERVAL_S:
        return
    ctx._last_janitor = now
    try:
        repo.expire_task_leases(ctx.conn)
        keys = [t.key for t in selected_targets(ctx.settings)]
        seeded = repo.ensure_discover_tasks(
            ctx.conn, keys, interval_min=ctx.settings.discover_interval_min)
        email_seeded = (
            ctx.settings.gmail_enabled
            and gmail_connection_available(ctx.conn, ctx.settings)
            and repo.ensure_email_task(ctx.conn)
        )
        ctx.conn.commit()
        if seeded:
            logger.info("seeded %d discover task(s)", seeded)
        if email_seeded:
            logger.info("seeded Gmail collector task")
    except Exception:
        ctx.conn.rollback()
        logger.exception("janitor pass failed")

    if now - ctx._last_purge >= _PURGE_INTERVAL_S:
        ctx._last_purge = now
        try:
            purged = repo.purge_finished_tasks(
                ctx.conn, retention_days=ctx.settings.task_retention_days)
            if purged:
                logger.info("purged %d finished task(s)", purged)
        except Exception:
            ctx.conn.rollback()
            logger.exception("purge failed")


def _paced_is_due(ctx: WorkerContext) -> bool:
    """Whether the paced lane may run a task now.

    The gate lives in the ``meta`` table, not in memory, so a worker restart does
    not reset the pacing and let a backfill burst through.
    """
    last = repo.get_meta(ctx.conn, _PACED_GATE_KEY)
    if not last:
        return True
    try:
        elapsed = (datetime.now(UTC) - datetime.fromisoformat(last)).total_seconds()
    except ValueError:
        return True     # an unreadable gate must not wedge the paced lane shut
    return elapsed >= ctx.settings.paced_task_interval_s


def _mark_paced(ctx: WorkerContext) -> None:
    repo.set_meta(ctx.conn, _PACED_GATE_KEY, repo.now_iso())
    ctx.conn.commit()


# ── loop ─────────────────────────────────────────────────────────────────────
def run_worker(settings: Settings | None = None, *, once: bool = False,
               max_tasks: int | None = None) -> int:
    """Pop and execute tasks until stopped. Returns the number executed.

    ``once=True`` drains only what is currently due and returns — used by the CLI
    and by tests. SIGTERM/SIGINT set a flag rather than killing mid-task, so a
    Magic Containers redeploy cannot leave a half-written article behind: the current task
    finishes, then the loop exits.
    """
    settings = settings or get_settings()
    settings.validate_gmail()
    conn = db.connect(settings)
    db.init_schema(conn, settings)
    executed = 0

    with HttpClient(settings) as http:
        ctx = WorkerContext(conn, http, settings)

        def _stop(signum, _frame) -> None:
            logger.info("signal %s received — finishing the current task then exiting",
                        signum)
            ctx.stopping = True

        for sig in (signal.SIGTERM, signal.SIGINT):
            # Not the main thread (tests) — there is no handler to install.
            with contextlib.suppress(ValueError):
                signal.signal(sig, _stop)

        logger.info("worker started (discover every %.0f min, paced 1/%.0fs)",
                    settings.discover_interval_min, settings.paced_task_interval_s)
        while not ctx.stopping:
            _janitor(ctx)

            task = repo.pop_due_task(conn, lane="fast",
                                     lease_min=settings.task_lease_min)
            lane = "fast"
            if task is None and _paced_is_due(ctx):
                task = repo.pop_due_task(conn, lane="paced",
                                         lease_min=settings.task_lease_min)
                lane = "paced"

            if task is None:
                if once:
                    break
                time.sleep(_IDLE_SLEEP_S)
                continue

            logger.debug("running #%s %s %s (%s lane, attempt %s)", task["id"],
                         task["kind"], task["municipality_key"], lane, task["attempts"])
            _run_task(ctx, task)
            if lane == "paced":
                _mark_paced(ctx)
            executed += 1
            if max_tasks is not None and executed >= max_tasks:
                break

    conn.close()
    logger.info("worker stopped after %d task(s)", executed)
    return executed
