"""Generate nbkommune/registry.json from the verified kommune URL survey CSV.

Run once at bootstrap, and again whenever the survey CSV is corrected:

    python scripts/build_registry.py path/to/kommuner_presse_nyheder_verificeret.csv

Deliberately a script, not import-time logic: the registry is data that gets
hand-corrected (a site changes its markup, a URL moves), and a generated file
that is then edited in place beats parsing a CSV on every boot.
"""
from __future__ import annotations

import csv
import json
import re
import sys
from pathlib import Path
from urllib.parse import urlparse

# Danish letters must fold to the conventional two-letter forms, not be stripped:
# "Ærø" → "aeroe", never "r". Order matters — do these before the catch-all.
_FOLD = {
    "æ": "ae", "ø": "oe", "å": "aa",
    "ä": "ae", "ö": "oe", "ü": "ue", "é": "e", "è": "e",
}

# Sites the survey flagged as rendering their news list client-side. They are
# shipped disabled: the plain-HTTP channels cannot see their content at all, so
# leaving them enabled would produce a permanent, meaningless error stream.
_JS_MARKER = re.compile(r"\bJS[- ]", re.I)

_SOURCE_TYPE = {
    "fælles": "faelles",
    "separat": "separat",
    "kun nyheder": "kun_nyheder",
    "tredjepart": "tredjepart",
}


def slugify(name: str) -> str:
    s = name.strip().lower()
    for src, dst in _FOLD.items():
        s = s.replace(src, dst)
    s = re.sub(r"[^a-z0-9]+", "-", s).strip("-")
    return s


def _url(value: str) -> str:
    value = (value or "").strip()
    return value if value.startswith("http") else ""


def build(csv_path: Path) -> list[dict]:
    rows = list(csv.DictReader(csv_path.open(encoding="utf-8-sig")))
    out: list[dict] = []
    seen: set[str] = set()
    for row in rows:
        name = (row.get("Kommune") or "").strip()
        if not name:
            continue
        key = slugify(name)
        if key in seen:
            raise SystemExit(f"duplicate slug {key!r} from {name!r} — fix the CSV")
        seen.add(key)

        site = _url(row.get("Officiel hjemmeside", ""))
        news = _url(row.get("Nyheder URL", ""))
        press = _url(row.get("Pressemeddelelser URL", ""))
        note = (row.get("Bemærkning") or "").strip()
        status = (row.get("Status") or "").strip().upper()
        src_type = _SOURCE_TYPE.get(
            (row.get("Kildetype") or "").strip().lower(), "faelles"
        )

        js_only = bool(_JS_MARKER.search(note))
        # A sitemap channel needs to know which URLs are articles. The news
        # path is the best available prefix; discovery widens it if it proves
        # too narrow. Derived here so every target ships with a usable guess.
        primary = news or press
        prefix = urlparse(primary).path.rstrip("/") if primary else ""

        entry = {
            "key": key,
            "name": name,
            "site_url": site,
            "news_url": news,
            "press_url": press,
            "channel": "auto",
            "source_type": src_type,
            "enabled": not js_only,
            "verified": status != "TJEK",
            "config": {k: v for k, v in {"url_prefix": prefix}.items() if v},
            "note": note,
        }
        if js_only:
            entry["note"] = (note + " [DISABLED: needs browser rendering]").strip()
        out.append(entry)
    out.sort(key=lambda e: e["key"])
    return out


def main() -> None:
    if len(sys.argv) != 2:
        raise SystemExit(__doc__)
    entries = build(Path(sys.argv[1]))
    dest = Path(__file__).resolve().parent.parent / "nbkommune" / "registry.json"
    dest.write_text(
        json.dumps(entries, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
    )
    enabled = sum(1 for e in entries if e["enabled"])
    print(f"wrote {len(entries)} targets ({enabled} enabled) → {dest}")
    for e in entries:
        if not e["enabled"]:
            print(f"  disabled: {e['key']:22s} {e['note'][:70]}")
        elif not e["verified"]:
            print(f"  unverified: {e['key']:20s} {e['note'][:70]}")


if __name__ == "__main__":
    main()
