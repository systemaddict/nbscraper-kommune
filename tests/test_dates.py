"""Danish date parsing — the field most likely to silently corrupt the corpus."""
from __future__ import annotations

from datetime import date

import pytest

from nbkommune.dates import below_floor, iso_date, parse_danish_datetime


class TestDottedTimeTrap:
    """The Umbraco `cmspageupdated` format, which stdlib parses *wrongly*.

    `datetime.fromisoformat("2026-08-18 06.28")` returns 06:00:00.28 — it reads
    the minutes as fractional seconds and discards them. Three of five sampled
    kommune article pages expose their timestamp in exactly this shape, so a
    regression here silently shifts most of the corpus to the top of the hour.
    """

    def test_dotted_time_keeps_its_minutes(self):
        # 06:28 Copenhagen (CEST, +02:00) == 04:28 UTC
        assert parse_danish_datetime("2026-08-18 06.28") == "2026-08-18T04:28:00+00:00"

    def test_dotted_time_with_seconds(self):
        assert parse_danish_datetime("2026-08-18 06.28.45") == "2026-08-18T04:28:45+00:00"

    def test_single_digit_hour(self):
        assert parse_danish_datetime("2026-08-18 6.05") == "2026-08-18T04:05:00+00:00"

    def test_real_iso_fractional_seconds_untouched(self):
        # Must NOT be mangled by the dotted-time repair: the hour is colon-separated.
        assert (parse_danish_datetime("2026-08-18T15:00:00.000+02:00")
                == "2026-08-18T13:00:00+00:00")


class TestFormats:
    @pytest.mark.parametrize("raw,expected", [
        ("2026-08-14 12:59:14", "2026-08-14T10:59:14+00:00"),
        ("18. august 2026", "2026-08-17T22:00:00+00:00"),
        ("torsdag den 18. august 2026 kl. 14.30", "2026-08-18T12:30:00+00:00"),
        ("18. aug 2026", "2026-08-17T22:00:00+00:00"),
        ("18-08-2026", "2026-08-17T22:00:00+00:00"),
        ("18/08/2026 09:15", "2026-08-18T07:15:00+00:00"),
        ("Opdateret 5. september 2026 kl. 08.00", "2026-09-05T06:00:00+00:00"),
    ])
    def test_parses(self, raw, expected):
        assert parse_danish_datetime(raw) == expected

    def test_date_only_is_danish_midnight_not_utc(self):
        """A bare date must land on the right Danish calendar day."""
        assert parse_danish_datetime("2026-08-18") == "2026-08-17T22:00:00+00:00"
        assert iso_date(parse_danish_datetime("2026-08-18")) == date(2026, 8, 17)

    @pytest.mark.parametrize("raw", ["", None, "ingen dato her", "31. februar 2026",
                                     "42. august 2026",
                                     "Mon, 01 Jan 0001 00:00:00 +0000"])
    def test_unparseable_returns_none(self, raw):
        assert parse_danish_datetime(raw) is None


class TestFloor:
    """The floor must fail OPEN — dropping current articles is far worse than
    keeping a few old ones."""

    def test_below_floor_skips_old(self):
        assert below_floor("2025-06-01T10:00:00+00:00", date(2026, 1, 1)) is True

    def test_at_or_after_floor_kept(self):
        assert below_floor("2026-06-01T10:00:00+00:00", date(2026, 1, 1)) is False

    def test_unknown_date_is_kept(self):
        assert below_floor(None, date(2026, 1, 1)) is False

    def test_unparseable_date_is_kept(self):
        assert below_floor("engang i fjor", date(2026, 1, 1)) is False

    def test_no_floor_keeps_everything(self):
        assert below_floor("1999-01-01T00:00:00+00:00", None) is False
