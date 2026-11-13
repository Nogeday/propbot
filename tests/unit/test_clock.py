"""Epoch-millisecond clock helpers."""

import datetime as dt
from zoneinfo import ZoneInfo

import pytest

from propbot.core.clock import ms_from_datetime, system_clock_ms, utc_from_ms


def test_round_trip_keeps_the_millisecond() -> None:
    moment = dt.datetime(2026, 11, 13, 14, 30, 0, 123000, tzinfo=dt.UTC)

    assert ms_from_datetime(moment) == 1_794_580_200_123
    assert utc_from_ms(1_794_580_200_123) == moment


def test_conversion_is_exact_where_float_seconds_would_round() -> None:
    # 1784647800001 ms / 1000 is not representable as a float second; integer math is.
    moment = dt.datetime(2026, 7, 21, 15, 30, 0, 1999, tzinfo=dt.UTC)

    assert ms_from_datetime(moment) % 1000 == 1


def test_other_zones_map_to_the_same_instant() -> None:
    new_york = dt.datetime(2026, 11, 13, 9, 30, tzinfo=ZoneInfo("America/New_York"))

    assert ms_from_datetime(new_york) == ms_from_datetime(new_york.astimezone(dt.UTC))
    assert utc_from_ms(ms_from_datetime(new_york)).utcoffset() == dt.timedelta(0)


def test_naive_datetime_is_rejected() -> None:
    with pytest.raises(ValueError, match="timezone-aware"):
        ms_from_datetime(dt.datetime(2026, 11, 13, 9, 30))


def test_system_clock_is_utc_epoch_milliseconds() -> None:
    before = dt.datetime.now(dt.UTC)
    now = utc_from_ms(system_clock_ms())

    assert abs((now - before).total_seconds()) < 5
