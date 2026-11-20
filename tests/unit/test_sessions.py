"""Session ends derived from the data (`session.islastbar`), half days included."""

import datetime as dt
from zoneinfo import ZoneInfo

import pytest

from propbot.core.bars import Bar
from propbot.data.sessions import DEFAULT_MIN_GAP_SECONDS, session_last_flags, split_sessions
from propbot.data.synthetic import generate_bars

NY = ZoneInfo("America/New_York")
MINUTE = 60_000


def bars_at(*minutes: int) -> list[Bar]:
    return [Bar(m * MINUTE, 10.0, 11.0, 9.0, 10.0, 1.0) for m in minutes]


def test_default_gap_is_thirty_minutes() -> None:
    assert DEFAULT_MIN_GAP_SECONDS == 1800


def test_bar_before_a_long_break_and_the_final_bar_are_last() -> None:
    # 1-minute bars; a 60-minute break after minute 2 and a 5-minute hole after minute 4.
    flags = session_last_flags(bars_at(0, 1, 2, 63, 64, 70, 71), 60)
    assert flags == (False, False, True, False, False, False, True)


def test_gap_is_measured_from_the_bar_close() -> None:
    # Bar at minute 0 closes at 5 (5-minute bars); the next opens 30 minutes later at 35.
    assert session_last_flags(bars_at(0, 35), 300) == (True, True)
    assert session_last_flags(bars_at(0, 34), 300) == (False, True)
    assert session_last_flags(bars_at(0, 10), 300, min_gap_seconds=300) == (True, True)
    assert session_last_flags(bars_at(0, 9), 300, min_gap_seconds=300) == (False, True)


def test_empty_and_single_bar_inputs() -> None:
    assert session_last_flags([], 60) == ()
    assert split_sessions([], 60) == ()
    assert session_last_flags(bars_at(5), 60) == (True,)
    assert split_sessions(bars_at(5), 60) == (range(1),)


def test_split_sessions_returns_consecutive_ranges() -> None:
    sessions = split_sessions(bars_at(0, 1, 2, 63, 64, 200), 60)
    assert sessions == (range(0, 3), range(3, 5), range(5, 6))


@pytest.mark.parametrize(("tf", "gap"), [(0, 1800), (-60, 1800), (60, 0), (60, -1)])
def test_non_positive_lengths_are_refused(tf: int, gap: int) -> None:
    with pytest.raises(ValueError, match="positive"):
        session_last_flags(bars_at(0), tf, min_gap_seconds=gap)


def local(ts_ms: int) -> dt.datetime:
    return dt.datetime.fromtimestamp(ts_ms // 1000, NY)


@pytest.mark.parametrize("tf_seconds", [60, 300])
@pytest.mark.parametrize("half_day", [dt.date(2026, 11, 27), dt.date(2026, 12, 24)])
def test_half_day_session_ends_at_the_early_close(tf_seconds: int, half_day: dt.date) -> None:
    series = generate_bars(
        tf_seconds=tf_seconds,
        start=half_day - dt.timedelta(days=1),
        sessions=3,
        seed=7,
        early_closes={half_day: dt.time(13, 0)},
    )
    flags = session_last_flags(series.bars, tf_seconds)
    sessions = split_sessions(series.bars, tf_seconds)
    assert len(sessions) == 3
    lasts = [local(series.bars[s.stop - 1].ts_open_ms) for s in sessions]
    assert all(flags[s.stop - 1] for s in sessions)
    assert sum(flags) == 3
    early = next(moment for moment in lasts if moment.date() == half_day)
    bar_minutes = tf_seconds // 60
    assert (early.hour, early.minute) == (12, 60 - bar_minutes)
    others = [moment for moment in lasts if moment.date() != half_day]
    assert all((moment.hour, moment.minute) == (16, 60 - bar_minutes) for moment in others)
    firsts = [local(series.bars[s.start].ts_open_ms) for s in sessions]
    assert all((moment.hour, moment.minute) == (18, 0) for moment in firsts)
