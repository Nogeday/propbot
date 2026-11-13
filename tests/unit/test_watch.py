"""Watch windows as instants, including the weeks around the US daylight-saving changes."""

import datetime as dt

import pytest

from propbot.core.clock import ms_from_datetime
from propbot.notify.format import NEW_YORK
from propbot.signals.tvconfig import HeartbeatConfig
from propbot.signals.watch import active_or_recent_window, watch_window_open, window_instances

MINUTE_MS = 60_000
HOUR_MS = 60 * MINUTE_MS
WEEKDAYS = ["mon", "tue", "wed", "thu", "fri"]


def heartbeat(*windows: tuple[list[str], str, str]) -> HeartbeatConfig:
    return HeartbeatConfig.model_validate(
        {
            "max_silence_minutes": 75,
            "watch_windows": [
                {"days": days, "start": start, "end": end} for days, start, end in windows
            ],
        }
    )


DEFAULT = heartbeat((WEEKDAYS, "03:00", "16:00"))


def new_york(text: str) -> int:
    return ms_from_datetime(dt.datetime.fromisoformat(text).replace(tzinfo=NEW_YORK))


def utc(text: str) -> int:
    return ms_from_datetime(dt.datetime.fromisoformat(text).replace(tzinfo=dt.UTC))


def test_a_weekday_window_becomes_one_interval_of_that_day() -> None:
    monday = dt.date(2026, 11, 16)

    assert window_instances(DEFAULT, monday) == [
        (new_york("2026-11-16 03:00"), new_york("2026-11-16 16:00"))
    ]
    assert window_instances(DEFAULT, dt.date(2026, 11, 14)) == []  # Saturday
    assert window_instances(DEFAULT, dt.date(2026, 11, 15)) == []  # Sunday


def test_several_windows_of_one_day_come_back_in_start_order() -> None:
    config = heartbeat((["mon"], "09:30", "16:00"), (["mon", "sun"], "03:00", "08:00"))

    assert window_instances(config, dt.date(2026, 11, 16)) == [
        (new_york("2026-11-16 03:00"), new_york("2026-11-16 08:00")),
        (new_york("2026-11-16 09:30"), new_york("2026-11-16 16:00")),
    ]
    assert window_instances(config, dt.date(2026, 11, 15)) == [
        (new_york("2026-11-15 03:00"), new_york("2026-11-15 08:00"))
    ]


@pytest.mark.parametrize(
    ("moment", "expected"),
    [
        ("2026-11-16 02:59:59", False),
        ("2026-11-16 03:00:00", True),  # the start belongs to the window
        ("2026-11-16 15:59:59", True),
        ("2026-11-16 16:00:00", False),  # the end does not
        ("2026-11-14 10:00:00", False),  # Saturday
        ("2026-11-20 15:00:00", True),  # Friday
    ],
)
def test_a_window_is_open_from_its_start_up_to_but_not_including_its_end(
    moment: str, expected: bool
) -> None:
    assert watch_window_open(DEFAULT, new_york(moment)) is expected


@pytest.mark.parametrize(
    ("day", "start_utc", "end_utc"),
    [
        # Winter (EST = UTC-5).
        ("2026-01-15", "2026-01-15 08:00", "2026-01-15 21:00"),
        # Friday before the US change (2026-03-08): still EST.
        ("2026-03-06", "2026-03-06 08:00", "2026-03-06 21:00"),
        # Monday after it: EDT = UTC-4, while Europe is still on winter time.
        ("2026-03-09", "2026-03-09 07:00", "2026-03-09 20:00"),
        # Summer.
        ("2026-07-14", "2026-07-14 07:00", "2026-07-14 20:00"),
        # Europe went back on 2026-10-25, the US has not yet: still EDT.
        ("2026-10-27", "2026-10-27 07:00", "2026-10-27 20:00"),
        # Monday after the US change (2026-11-01): EST again.
        ("2026-11-02", "2026-11-02 08:00", "2026-11-02 21:00"),
    ],
)
def test_windows_follow_new_york_wall_time_across_daylight_saving(
    day: str, start_utc: str, end_utc: str
) -> None:
    assert window_instances(DEFAULT, dt.date.fromisoformat(day)) == [(utc(start_utc), utc(end_utc))]
    assert watch_window_open(DEFAULT, utc(start_utc))
    assert not watch_window_open(DEFAULT, utc(start_utc) - 1)
    assert not watch_window_open(DEFAULT, utc(end_utc))


def test_a_sunday_window_on_the_change_days_keeps_its_wall_times() -> None:
    config = heartbeat((["sun"], "01:00", "05:00"))

    spring = window_instances(config, dt.date(2026, 3, 8))  # 02:00 -> 03:00
    autumn = window_instances(config, dt.date(2026, 11, 1))  # 02:00 -> 01:00

    assert spring == [(utc("2026-03-08 06:00"), utc("2026-03-08 09:00"))]  # three real hours
    assert autumn == [(utc("2026-11-01 05:00"), utc("2026-11-01 10:00"))]  # five real hours


def test_the_active_window_is_returned_with_its_bounds() -> None:
    window = (new_york("2026-11-16 03:00"), new_york("2026-11-16 16:00"))

    assert active_or_recent_window(DEFAULT, new_york("2026-11-16 09:30")) == window
    assert active_or_recent_window(DEFAULT, new_york("2026-11-16 16:00")) is None


def test_a_window_that_just_closed_is_still_found_within_the_grace() -> None:
    window = (new_york("2026-11-16 03:00"), new_york("2026-11-16 16:00"))
    grace = 75 * MINUTE_MS

    assert active_or_recent_window(DEFAULT, window[1], grace_ms=grace) == window
    assert active_or_recent_window(DEFAULT, window[1] + grace - 1, grace_ms=grace) == window
    assert active_or_recent_window(DEFAULT, window[1] + grace, grace_ms=grace) is None
    assert active_or_recent_window(DEFAULT, window[0] - 1, grace_ms=grace) is None


def test_the_grace_reaches_back_over_midnight() -> None:
    config = heartbeat((["mon"], "20:00", "23:59"))
    window = (new_york("2026-11-16 20:00"), new_york("2026-11-16 23:59"))

    after_midnight = new_york("2026-11-17 00:30")

    assert active_or_recent_window(config, after_midnight) is None
    assert active_or_recent_window(config, after_midnight, grace_ms=HOUR_MS) == window


def test_with_overlapping_candidates_the_latest_start_wins() -> None:
    config = heartbeat((["mon"], "03:00", "11:00"), (["mon"], "09:30", "16:00"))
    early = (new_york("2026-11-16 03:00"), new_york("2026-11-16 11:00"))
    late = (new_york("2026-11-16 09:30"), new_york("2026-11-16 16:00"))

    assert active_or_recent_window(config, new_york("2026-11-16 09:00")) == early
    assert active_or_recent_window(config, new_york("2026-11-16 10:00")) == late
    assert active_or_recent_window(config, new_york("2026-11-16 11:30"), grace_ms=HOUR_MS) == late


def test_a_negative_grace_is_a_programming_error() -> None:
    with pytest.raises(ValueError, match="grace_ms"):
        active_or_recent_window(DEFAULT, new_york("2026-11-16 09:30"), grace_ms=-1)
