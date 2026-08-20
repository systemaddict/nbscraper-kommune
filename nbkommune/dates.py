"""Danish date/time parsing, normalised to ISO 8601 UTC.

Its own module because publication dates are the single hardest field in this
scraper. The site survey found that most kommune CMSes expose no machine-readable
publication date at all: three of five sampled article pages carried only a
``cmspageupdated`` meta tag (a *modified* stamp, in the format
``2026-08-18 06.28``), one carried nothing, and only one emitted a clean
JSON-LD ``datePublished``. Everything else has to be read off rendered Danish
text like ``18. august 2026 kl. 14.30``.

Every value returned is ISO 8601 in **UTC**, so ordering and the publication
floor are comparable across sites. A date with no time is taken as midnight
Europe/Copenhagen — these are Danish municipal sites, and assuming UTC would
shift half of them to the previous day.
"""
from __future__ import annotations

import re
from datetime import UTC, date, datetime
from zoneinfo import ZoneInfo

DK = ZoneInfo("Europe/Copenhagen")

# .NET/Umbraco commonly serialises an unset DateTime as year 0001. Municipal
# news can be old, but nothing in this corpus predates the modern web; accepting
# a framework sentinel as a real publication date is silent data corruption.
_MIN_REAL_YEAR = 1900

_MONTHS = {
    "januar": 1, "jan": 1,
    "februar": 2, "feb": 2,
    "marts": 3, "mar": 3,
    "april": 4, "apr": 4,
    "maj": 5,
    "juni": 6, "jun": 6,
    "juli": 7, "jul": 7,
    "august": 8, "aug": 8,
    "september": 9, "sep": 9, "sept": 9,
    "oktober": 10, "okt": 10,
    "november": 11, "nov": 11,
    "december": 12, "dec": 12,
}

_WEEKDAYS = r"(?:mandag|tirsdag|onsdag|torsdag|fredag|lørdag|søndag)"

# "torsdag den 18. august 2026 kl. 14.30" — the weekday and "den" are noise, and
# the time separator is a period as often as a colon in Danish rendering.
_DK_LONG = re.compile(
    rf"(?:{_WEEKDAYS}\s+)?(?:den\s+)?(\d{{1,2}})\.?\s+"
    rf"([a-zæøå]+)\s+(\d{{4}})"
    rf"(?:\s*(?:kl\.?|,)?\s*(\d{{1,2}})[.:](\d{{2}})(?:[.:](\d{{2}}))?)?",
    re.I,
)

# "18-08-2026" / "18/08/2026" / "18.08.2026", optionally with a time.
_DK_NUMERIC = re.compile(
    r"\b(\d{1,2})[./-](\d{1,2})[./-](\d{4})"
    r"(?:\s+(\d{1,2})[.:](\d{2})(?:[.:](\d{2}))?)?\b"
)

# "2026-08-18 06.28" / "2026-08-18 12:59:14" — the Umbraco `cmspageupdated`
# shape. The dotted variant is a trap rather than a parse failure: Python's
# `fromisoformat` accepts "2026-08-18 06.28" and reads it as 06:00:00.28 —
# fractional *seconds*, silently discarding the minutes. So the dotted form is
# rewritten to colons before fromisoformat ever sees it (`_normalise_dotted`).
_ISO_LOOSE = re.compile(
    r"\b(\d{4})-(\d{2})-(\d{2})(?:[T\s]+(\d{1,2})[.:](\d{2})(?:[.:](\d{2}))?)?"
)

# A whole string that is a date followed by a period-separated time. Anchored and
# deliberately narrow: real ISO fractional seconds ("15:00:00.000+02:00") have a
# colon after the hour and must not be touched.
_DOTTED_TIME = re.compile(
    r"^(\d{4}-\d{2}-\d{2})[T\s]+(\d{1,2})\.(\d{2})(?:\.(\d{2}))?"
    r"(Z|[+-]\d{2}:?\d{2})?\s*$",
    re.I,
)


def _normalise_dotted(raw: str) -> str:
    """Rewrite a period-separated time to colons, or return ``raw`` unchanged."""
    m = _DOTTED_TIME.match(raw)
    if not m:
        return raw
    day, hh, mm, ss, zone = m.groups()
    return f"{day} {int(hh):02d}:{mm}:{ss or '00'}{zone or ''}"


def _to_utc(dt: datetime) -> str:
    """Render a datetime as ISO 8601 UTC to second precision.

    Naive input is read as Europe/Copenhagen — the only sane assumption for a
    Danish municipal site, and one that keeps a 00:30 publication on the right
    calendar day.
    """
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=DK)
    return dt.astimezone(UTC).replace(microsecond=0).isoformat()


def _build(year: int, month: int, day: int,
           hh: str | None = None, mm: str | None = None, ss: str | None = None) -> str | None:
    if year < _MIN_REAL_YEAR:
        return None
    try:
        # Built naive on purpose: a date scraped off a Danish municipal page has
        # no stated zone, and `_to_utc` attaches Europe/Copenhagen. Constructing
        # it as UTC here would silently shift midnight publications a day back.
        naive = datetime(  # noqa: DTZ001
            year, month, day,
            int(hh) if hh else 0, int(mm) if mm else 0, int(ss) if ss else 0,
        )
    except (ValueError, OverflowError):
        return None    # 31 February and friends — a real value on broken sites
    try:
        return _to_utc(naive)
    except (ValueError, OverflowError):
        # A timezone conversion can cross datetime's supported boundary. One
        # live RSS feed uses year 0001 as an "unknown" sentinel; Copenhagen's
        # historical UTC offset then moves it into year 0. It is unknown, not a
        # reason to abort discovery for every otherwise-valid feed entry.
        return None


def parse_danish_datetime(value: str | None) -> str | None:
    """Best-effort parse of any date string these sites produce → ISO 8601 UTC.

    Returns None when nothing date-shaped is found, which the caller must treat
    as "unknown", never as "old": an article whose date we cannot read is far
    more likely to be current than archival.
    """
    if not value:
        return None
    raw = value.strip()
    if not raw:
        return None

    # 1. Real ISO 8601 first — JSON-LD and `<time datetime="…">` emit it, and it
    #    is the only form that carries a trustworthy timezone. Dotted times are
    #    repaired first; see `_normalise_dotted` for why that is not optional.
    try:
        parsed = datetime.fromisoformat(
            _normalise_dotted(raw).replace("Z", "+00:00")
        )
        if parsed.year < _MIN_REAL_YEAR:
            return None
        return _to_utc(parsed)
    except (ValueError, OverflowError):
        pass

    # 2. ISO-shaped but not ISO-legal (period time separator, stray suffix).
    m = _ISO_LOOSE.search(raw)
    if m:
        y, mo, d, hh, mm, ss = m.groups()
        built = _build(int(y), int(mo), int(d), hh, mm, ss)
        if built:
            return built

    # 3. Danish long form, with or without a time.
    m = _DK_LONG.search(raw)
    if m:
        day, month_name, year, hh, mm, ss = m.groups()
        month = _MONTHS.get(month_name.lower().rstrip("."))
        if month:
            built = _build(int(year), month, int(day), hh, mm, ss)
            if built:
                return built

    # 4. Numeric day-first. Danish convention is unambiguous here (18-08-2026);
    #    a US-style month-first string would be misread, but these are .dk sites.
    m = _DK_NUMERIC.search(raw)
    if m:
        day, month, year, hh, mm, ss = m.groups()
        built = _build(int(year), int(month), int(day), hh, mm, ss)
        if built:
            return built

    return None


def iso_date(value: str | None) -> date | None:
    """The calendar date of an ISO timestamp, for floor comparisons."""
    if not value:
        return None
    try:
        return datetime.fromisoformat(value).date()
    except ValueError:
        return None


def below_floor(published_at: str | None, floor: date | None) -> bool:
    """Whether an article predates the publication floor and must not be stored.

    Fails **open** in both unknown cases — no floor, or no parseable date — so a
    site whose dates we cannot read is still scraped rather than silently
    skipped. Dropping current articles is a much worse failure than storing a
    few old ones.
    """
    if floor is None:
        return False
    published = iso_date(published_at)
    if published is None:
        return False
    return published < floor
