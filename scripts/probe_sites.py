"""Survey every registry target and report what each site actually offers.

This is the tool that turns the registry from guesses into measured config. For
each target it resolves the discovery channel, counts the articles found and how
many carry a real publication date, then extracts one article to see which
layer supplies the body.

    python scripts/probe_sites.py                 # every enabled target
    python scripts/probe_sites.py --keys aarhus,odder
    python scripts/probe_sites.py --json out.json # machine-readable

Output is deliberately blunt about failure: the point is to find the sites that
need hand-written selectors, not to produce a clean-looking report. Read the
`ATTENTION` section at the end first.
"""
from __future__ import annotations

import argparse
import json
import logging
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from nbkommune.extract import extract_article  # noqa: E402
from nbkommune.http import HttpClient  # noqa: E402
from nbkommune.settings import get_settings  # noqa: E402
from nbkommune.sources import make_source  # noqa: E402
from nbkommune.targets import registry  # noqa: E402


def probe_one(target, http, settings) -> dict:
    out: dict = {"key": target.key, "name": target.name,
                 "source_type": target.source_type, "enabled": target.enabled}
    if not target.enabled:
        out["status"] = "disabled"
        out["note"] = target.note
        return out
    try:
        source = make_source(target, http)
        out["channel"] = source.channel
        out["detail"] = source.detail
    except Exception as exc:
        out["status"] = "resolve_failed"
        out["error"] = f"{type(exc).__name__}: {exc}"
        return out
    try:
        found = source.list_articles()
    except Exception as exc:
        out["status"] = "list_failed"
        out["error"] = f"{type(exc).__name__}: {exc}"
        return out

    out["articles"] = len(found)
    out["with_date"] = sum(1 for a in found if a.published_at)
    out["with_title"] = sum(1 for a in found if a.title)
    if not found:
        out["status"] = "no_articles"
        return out

    # Extract the newest article to see whether the body is reachable at all.
    newest = max(found, key=lambda a: (a.published_at or a.updated_at or ""))
    out["sample_url"] = newest.url
    try:
        html, final = http.get_text(newest.url)
        detail = extract_article(html, final, listed=newest,
                                 body_selector=target.config.get("body_selector"),
                                 min_body_chars=settings.min_body_chars)
    except Exception as exc:
        out["status"] = "extract_failed"
        out["error"] = f"{type(exc).__name__}: {exc}"
        return out

    out["words"] = detail.word_count
    out["body_via"] = detail.provenance.get("body_text", "nothing")
    out["date_via"] = detail.provenance.get("published_at", "nothing")
    out["container"] = detail.raw.get("body_container")
    out["thin"] = len(detail.body_text or "") < settings.min_body_chars
    out["status"] = "thin" if out["thin"] else "ok"
    return out


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--keys", help="Comma-separated target keys (default: all).")
    parser.add_argument("--json", dest="json_path", help="Write full results here.")
    parser.add_argument("--verbose", action="store_true")
    args = parser.parse_args()

    logging.basicConfig(level=logging.INFO if args.verbose else logging.WARNING,
                        format="%(levelname)-7s %(name)s: %(message)s")
    logging.getLogger("httpx").setLevel(logging.WARNING)

    settings = get_settings()
    reg = registry(settings)
    targets = ([reg[k.strip()] for k in args.keys.split(",") if k.strip() in reg]
               if args.keys else sorted(reg.values(), key=lambda t: t.key))

    results: list[dict] = []
    with HttpClient(settings) as http:
        for i, target in enumerate(targets, 1):
            row = probe_one(target, http, settings)
            results.append(row)
            print(f"[{i:3d}/{len(targets)}] {row['key']:22s} "
                  f"{row.get('status','?'):14s} "
                  f"ch={row.get('channel','-'):8s} "
                  f"n={row.get('articles','-'):>4} "
                  f"dated={row.get('with_date','-'):>4} "
                  f"words={row.get('words','-'):>5} "
                  f"body={row.get('body_via','-'):9s} "
                  f"date={row.get('date_via','-')}")
            sys.stdout.flush()

    print("\n" + "=" * 78)
    by_status: dict[str, int] = {}
    by_channel: dict[str, int] = {}
    for row in results:
        by_status[row.get("status", "?")] = by_status.get(row.get("status", "?"), 0) + 1
        if row.get("channel"):
            by_channel[row["channel"]] = by_channel.get(row["channel"], 0) + 1
    print("status :", ", ".join(f"{k}={v}" for k, v in sorted(by_status.items())))
    print("channel:", ", ".join(f"{k}={v}" for k, v in sorted(by_channel.items())))
    dated = [r for r in results if r.get("articles")]
    if dated:
        no_date = sum(1 for r in dated if not r.get("with_date"))
        print(f"targets whose listing supplies no publication date: {no_date}/{len(dated)}")

    attention = [r for r in results
                 if r.get("status") not in ("ok", "disabled")]
    if attention:
        print(f"\nATTENTION — {len(attention)} target(s) need config or a browser:")
        for row in attention:
            print(f"  {row['key']:22s} {row.get('status','?'):14s} "
                  f"{row.get('error') or row.get('detail','')}"[:110])

    if args.json_path:
        Path(args.json_path).write_text(
            json.dumps(results, ensure_ascii=False, indent=2), encoding="utf-8")
        print(f"\nfull results → {args.json_path}")


if __name__ == "__main__":
    main()
