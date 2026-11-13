"""Prop day mapping, including the DST weeks SPEC §8.2 and §10 single out."""

import datetime as dt
from zoneinfo import ZoneInfo

import pytest

from propbot.core.propday import prop_day, prop_day_start, weekdays_between

PRAGUE = ZoneInfo("Europe/Prague")
NEW_YORK = ZoneInfo("America/New_York")
MIDNIGHT = dt.time(0, 0)


def utc(text: str) -> dt.datetime:
    return dt.datetime.fromisoformat(text).replace(tzinfo=dt.UTC)


def new_york(text: str) -> dt.datetime:
    return dt.datetime.fromisoformat(text).replace(tzinfo=NEW_YORK)


@pytest.mark.parametrize(
    ("moment", "expected"),
    [
        # Winter (CET = UTC+1): the day rolls at 23:00 UTC.
        (utc("2026-01-15 22:59:59"), dt.date(2026, 1, 15)),
        (utc("2026-01-15 23:00:00"), dt.date(2026, 1, 16)),
        # Summer (CEST = UTC+2): the day rolls at 22:00 UTC.
        (utc("2026-07-15 21:59:59"), dt.date(2026, 7, 15)),
        (utc("2026-07-15 22:00:00"), dt.date(2026, 7, 16)),
        # EU spring forward, 2026-03-29: a 23-hour prop day.
        (utc("2026-03-28 22:59:59"), dt.date(2026, 3, 28)),
        (utc("2026-03-28 23:00:00"), dt.date(2026, 3, 29)),
        (utc("2026-03-29 21:59:59"), dt.date(2026, 3, 29)),
        (utc("2026-03-29 22:00:00"), dt.date(2026, 3, 30)),
        # EU fall back, 2026-10-25: a 25-hour prop day.
        (utc("2026-10-24 21:59:59"), dt.date(2026, 10, 24)),
        (utc("2026-10-24 22:00:00"), dt.date(2026, 10, 25)),
        (utc("2026-10-25 22:59:59"), dt.date(2026, 10, 25)),
        (utc("2026-10-25 23:00:00"), dt.date(2026, 10, 26)),
    ],
)
def test_prague_midnight_reset(moment: dt.datetime, expected: dt.date) -> None:
    assert prop_day(moment, PRAGUE, MIDNIGHT) == expected


@pytest.mark.parametrize(
    ("moment", "expected"),
    [
        # Both on standard time: Prague midnight is 18:00 in New York.
        (new_york("2026-01-14 17:59:59"), dt.date(2026, 1, 14)),
        (new_york("2026-01-14 18:00:00"), dt.date(2026, 1, 15)),
        # US already on DST (from 2026-03-08), EU not yet (2026-03-29): 19:00 in New York.
        (new_york("2026-03-10 18:30:00"), dt.date(2026, 3, 10)),
        (new_york("2026-03-10 19:00:00"), dt.date(2026, 3, 11)),
        # Both on DST: back to 18:00.
        (new_york("2026-04-07 17:59:59"), dt.date(2026, 4, 7)),
        (new_york("2026-04-07 18:00:00"), dt.date(2026, 4, 8)),
        # EU back on standard time (2026-10-25), US still on DST (until 2026-11-01): 19:00.
        (new_york("2026-10-27 18:30:00"), dt.date(2026, 10, 27)),
        (new_york("2026-10-27 19:00:00"), dt.date(2026, 10, 28)),
        # Both on standard time again.
        (new_york("2026-11-03 17:59:59"), dt.date(2026, 11, 3)),
        (new_york("2026-11-03 18:00:00"), dt.date(2026, 11, 4)),
    ],
)
def test_prague_reset_seen_from_new_york(moment: dt.datetime, expected: dt.date) -> None:
    assert prop_day(moment, PRAGUE, MIDNIGHT) == expected


@pytest.mark.parametrize(
    "day", ["2026-01-14", "2026-03-10", "2026-07-15", "2026-10-27", "2026-11-12"]
)
def test_asian_active_window_and_new_york_session_share_a_prop_day(day: str) -> None:
    # SPEC §8.2: 03:00-11:00 ET and 09:30-15:59 ET trades count against the same daily budget.
    asian_entry = prop_day(new_york(f"{day} 03:00:00"), PRAGUE, MIDNIGHT)
    session_end = prop_day(new_york(f"{day} 15:59:00"), PRAGUE, MIDNIGHT)

    assert asian_entry == session_end == dt.date.fromisoformat(day)


def test_asian_range_window_belongs_to_the_next_prop_day() -> None:
    # 20:00 ET is after the Prague midnight reset, so it already counts for tomorrow.
    assert prop_day(new_york("2026-11-11 20:00:00"), PRAGUE, MIDNIGHT) == dt.date(2026, 11, 12)


def test_non_midnight_reset_labels_the_day_by_its_start() -> None:
    reset = dt.time(17, 0)

    assert prop_day(new_york("2026-01-15 16:59:59"), NEW_YORK, reset) == dt.date(2026, 1, 14)
    assert prop_day(new_york("2026-01-15 17:00:00"), NEW_YORK, reset) == dt.date(2026, 1, 15)
    assert prop_day(new_york("2026-01-16 09:30:00"), NEW_YORK, reset) == dt.date(2026, 1, 15)


def test_utc_reset_is_the_utc_calendar_date() -> None:
    zone = ZoneInfo("UTC")

    assert prop_day(utc("2026-06-30 23:59:59"), zone, MIDNIGHT) == dt.date(2026, 6, 30)
    assert prop_day(utc("2026-07-01 00:00:00"), zone, MIDNIGHT) == dt.date(2026, 7, 1)


def test_any_utc_offset_is_accepted() -> None:
    istanbul = dt.datetime(2026, 11, 13, 1, 30, tzinfo=dt.timezone(dt.timedelta(hours=3)))

    # 01:30 in Istanbul is 23:30 in Prague on the previous date.
    assert prop_day(istanbul, PRAGUE, MIDNIGHT) == dt.date(2026, 11, 12)


def test_naive_datetime_is_rejected() -> None:
    with pytest.raises(ValueError, match="timezone-aware"):
        prop_day(dt.datetime(2026, 1, 15, 12, 0), PRAGUE, MIDNIGHT)


def test_prop_day_start_is_the_reset_instant_in_utc() -> None:
    assert prop_day_start(dt.date(2026, 1, 15), PRAGUE, MIDNIGHT) == utc("2026-01-14 23:00:00")
    assert prop_day_start(dt.date(2026, 7, 15), PRAGUE, MIDNIGHT) == utc("2026-07-14 22:00:00")


def test_reset_inside_the_repeated_hour_stays_monotonic() -> None:
    # 2026-11-01 01:00-02:00 happens twice in New York. With a 01:30 reset, the second
    # pass through 01:15 is later than the first 01:45 and must not fall back a day.
    reset = dt.time(1, 30)
    first_0145 = utc("2026-11-01 05:45:00")  # 01:45 EDT
    second_0115 = utc("2026-11-01 06:15:00")  # 01:15 EST

    assert prop_day(first_0145, NEW_YORK, reset) == dt.date(2026, 11, 1)
    assert prop_day(second_0115, NEW_YORK, reset) == dt.date(2026, 11, 1)


def test_weekdays_between_skips_weekends_and_includes_both_ends() -> None:
    days = weekdays_between(dt.date(2026, 11, 12), dt.date(2026, 11, 17))  # Thu .. Tue

    assert days == [
        dt.date(2026, 11, 12),
        dt.date(2026, 11, 13),
        dt.date(2026, 11, 16),
        dt.date(2026, 11, 17),
    ]
    assert weekdays_between(dt.date(2026, 11, 14), dt.date(2026, 11, 15)) == []  # Sat, Sun
    assert weekdays_between(dt.date(2026, 11, 13), dt.date(2026, 11, 12)) == []
