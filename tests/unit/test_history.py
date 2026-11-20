"""BarHistory: Pine-style `x[n]` indexing over a ring of recent bars."""

import pytest

from propbot.core.bars import Bar
from propbot.strategies.history import BarHistory, HistoryError


def make_bar(i: int) -> Bar:
    return Bar(
        ts_open_ms=1_000 * i,
        open=100.0 + i,
        high=101.0 + i,
        low=99.0 + i,
        close=100.5 + i,
        volume=10.0 * i,
    )


def test_offset_zero_is_latest_bar_and_n_is_n_bars_ago() -> None:
    h = BarHistory(5)
    for i in range(3):
        h.push(make_bar(i))
    assert len(h) == 3
    assert h.high() == 103.0  # bar 2
    assert h.high(2) == 101.0  # bar 0
    assert h.open(1) == 101.0
    assert h.low(1) == 100.0
    assert h.close(0) == 102.5
    assert h.volume(2) == 0.0
    assert h.time(1) == 1_000
    assert h.bar(0) == make_bar(2)


def test_beyond_pushed_bars_is_na() -> None:
    h = BarHistory(5)
    assert len(h) == 0
    assert not h.has(0)
    assert h.close() is None
    h.push(make_bar(0))
    assert h.has(0)
    assert not h.has(1)
    assert h.high(1) is None
    assert h.bar(1) is None
    assert h.time(1) is None


def test_ring_keeps_only_the_most_recent_maxlen_bars() -> None:
    h = BarHistory(3)
    for i in range(10):
        h.push(make_bar(i))
    assert len(h) == 3
    assert h.maxlen == 3
    assert h.close(0) == 109.5
    assert h.close(2) == 107.5


def test_offset_beyond_maxlen_is_a_configuration_error() -> None:
    h = BarHistory(3)
    h.push(make_bar(0))
    with pytest.raises(HistoryError, match="aşıyor"):
        h.has(3)
    with pytest.raises(HistoryError, match="aşıyor"):
        h.high(3)


@pytest.mark.parametrize("bad", [-1, True, 1.0])
def test_bad_offset_is_rejected(bad: object) -> None:
    h = BarHistory(3)
    with pytest.raises(HistoryError):
        h.has(bad)  # type: ignore[arg-type]


@pytest.mark.parametrize("bad", [0, -2, True])
def test_bad_maxlen_is_rejected(bad: object) -> None:
    with pytest.raises(HistoryError):
        BarHistory(bad)  # type: ignore[arg-type]


def test_push_requires_strictly_ascending_open_times() -> None:
    h = BarHistory(3)
    h.push(make_bar(2))
    with pytest.raises(HistoryError, match="artan"):
        h.push(make_bar(2))
    with pytest.raises(HistoryError, match="artan"):
        h.push(make_bar(1))
    assert len(h) == 1
