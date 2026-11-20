"""Property tests: synthetic write/load round trip and `session_last_flags` invariants."""

import datetime as dt
import tempfile
from pathlib import Path

from hypothesis import given, settings
from hypothesis import strategies as st

from propbot.core.bars import Bar
from propbot.data.ohlcv import load_ohlcv
from propbot.data.sessions import session_last_flags, split_sessions
from propbot.data.synthetic import generate_bars, write_ohlcv_csv


@settings(max_examples=25, deadline=None)
@given(
    seed=st.integers(0, 2**32),
    tf_minutes=st.sampled_from([1, 5, 15]),
    sessions=st.integers(1, 2),
    start=st.dates(dt.date(2026, 1, 1), dt.date(2027, 12, 31)),
    tick_and_price=st.sampled_from([(0.25, 5000.0), (0.1, 6500.0), (0.01, 450.0), (1.0, 20000.0)]),
    early=st.booleans(),
)
def test_written_series_loads_back_exactly(
    seed: int,
    tf_minutes: int,
    sessions: int,
    start: dt.date,
    tick_and_price: tuple[float, float],
    early: bool,
) -> None:
    tick, price = tick_and_price
    early_closes = {start + dt.timedelta(days=offset): dt.time(13, 0) for offset in range(3)}
    series = generate_bars(
        tf_seconds=tf_minutes * 60,
        start=start,
        sessions=sessions,
        seed=seed,
        tick=tick,
        start_price=price,
        early_closes=early_closes if early else None,
    )
    with tempfile.TemporaryDirectory() as directory:
        path = Path(directory) / "synthetic.csv"
        write_ohlcv_csv(series, path)
        loaded = load_ohlcv(path, symbol=series.symbol, tf=str(tf_minutes))
    assert loaded.bars == series.bars
    assert loaded.tf_seconds == series.tf_seconds
    assert loaded.source.synthetic


@st.composite
def ascending_bars(draw: st.DrawFn) -> tuple[list[Bar], int, int]:
    tf_seconds = draw(st.sampled_from([60, 300]))
    steps = draw(st.lists(st.integers(1, 200), min_size=0, max_size=60))
    times = [0]
    for step in steps:
        times.append(times[-1] + step * 60_000)
    bars = [Bar(ts, 10.0, 11.0, 9.0, 10.0, 1.0) for ts in times]
    gap = draw(st.sampled_from([60, 1800, 3600]))
    return bars, tf_seconds, gap


@given(case=ascending_bars())
def test_session_flags_follow_the_gap_rule(case: tuple[list[Bar], int, int]) -> None:
    bars, tf_seconds, gap = case
    flags = session_last_flags(bars, tf_seconds, min_gap_seconds=gap)
    assert len(flags) == len(bars)
    assert flags[-1]
    for index in range(len(bars) - 1):
        close_ms = bars[index].ts_open_ms + tf_seconds * 1000
        expected = bars[index + 1].ts_open_ms - close_ms >= gap * 1000
        assert flags[index] == expected

    sessions = split_sessions(bars, tf_seconds, min_gap_seconds=gap)
    assert len(sessions) == sum(flags)
    covered = [index for session in sessions for index in session]
    assert covered == list(range(len(bars)))
    assert all(len(session) > 0 and flags[session.stop - 1] for session in sessions)


@given(case=ascending_bars(), extra=st.integers(1, 3600))
def test_a_longer_minimum_gap_never_adds_session_ends(
    case: tuple[list[Bar], int, int], extra: int
) -> None:
    bars, tf_seconds, gap = case
    short = session_last_flags(bars, tf_seconds, min_gap_seconds=gap)
    long = session_last_flags(bars, tf_seconds, min_gap_seconds=gap + extra)
    assert all(short[i] or not long[i] for i in range(len(bars)))
