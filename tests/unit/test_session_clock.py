"""New York wall-clock parts of bar times (`propbot.core.session`), DST weeks included."""

import datetime as dt

import pytest

from propbot.core.session import NEW_YORK, LocalClock, LocalParts


def utc_ms(year: int, month: int, day: int, hour: int = 0, minute: int = 0) -> int:
    return int(dt.datetime(year, month, day, hour, minute, tzinfo=dt.UTC).timestamp()) * 1000


@pytest.mark.parametrize(
    ("utc", "hour"),
    [
        # US DST begins 2026-03-08: 09:30 EST is 14:30 UTC before, 09:30 EDT is 13:30 UTC after.
        ((2026, 3, 6, 14, 30), 9),
        ((2026, 3, 9, 13, 30), 9),
        ((2026, 3, 9, 14, 30), 10),
        # EU DST changes (2026-03-29, 2026-10-25) do not move New York.
        ((2026, 3, 27, 13, 30), 9),
        ((2026, 3, 30, 13, 30), 9),
        ((2026, 10, 23, 13, 30), 9),
        ((2026, 10, 26, 13, 30), 9),
        # US DST ends 2026-11-01.
        ((2026, 10, 30, 13, 30), 9),
        ((2026, 11, 2, 14, 30), 9),
        ((2026, 11, 2, 13, 30), 8),
    ],
)
def test_new_york_hour_follows_us_dst_only(utc: tuple[int, int, int, int, int], hour: int) -> None:
    parts = LocalClock().parts(utc_ms(*utc))
    assert (parts.hour, parts.minute) == (hour, 30)


def test_parts_carry_date_weekday_and_minute_of_day() -> None:
    parts = LocalClock(NEW_YORK).parts(utc_ms(2026, 11, 27, 18, 0))  # Friday 13:00 EST
    assert parts == LocalParts(year=2026, month=11, day=27, hour=13, minute=0, weekday=4)
    assert parts.minute_of_day == 13 * 60
    assert parts.date == dt.date(2026, 11, 27)


def test_utc_evening_is_the_previous_or_same_new_york_day() -> None:
    parts = LocalClock().parts(utc_ms(2026, 11, 30, 3, 0))  # 22:00 Sunday in New York
    assert (parts.date, parts.weekday, parts.hour) == (dt.date(2026, 11, 29), 6, 22)


def test_repeated_and_changing_timestamps_give_consistent_parts() -> None:
    clock = LocalClock()
    first = clock.parts(utc_ms(2026, 3, 9, 13, 30))
    assert clock.parts(utc_ms(2026, 3, 9, 13, 30)) == first
    later = clock.parts(utc_ms(2026, 3, 9, 13, 31))
    assert later.minute == 31
    assert clock.parts(utc_ms(2026, 3, 9, 13, 30)) == first


def test_milliseconds_do_not_change_the_minute() -> None:
    assert LocalClock().parts(utc_ms(2026, 3, 9, 13, 30) + 999).minute == 30


def test_other_zones_are_supported() -> None:
    parts = LocalClock("Europe/Prague").parts(utc_ms(2026, 10, 25, 0, 30))  # CEST, UTC+2
    assert (parts.hour, parts.minute) == (2, 30)
    parts = LocalClock("Europe/Prague").parts(utc_ms(2026, 10, 25, 1, 30))  # CET, UTC+1
    assert (parts.hour, parts.minute) == (2, 30)
