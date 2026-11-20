"""Bar and series invariants (`propbot.core.bars`)."""

import dataclasses

import pytest

from propbot.core.bars import (
    SYNTHETIC_MARKER,
    Bar,
    BarError,
    BarSeries,
    DataSource,
    check_ascending,
    check_bar,
)

SOURCE = DataSource(name="x.csv", format="generic", sha256="", synthetic=False, bar_tz="UTC")
NAN = float("nan")
INF = float("inf")


def bar(ts: int = 0, o: float = 10.0, h: float = 11.0, low: float = 9.0, c: float = 10.5) -> Bar:
    return Bar(ts_open_ms=ts, open=o, high=h, low=low, close=c, volume=1.0)


def test_valid_bar_passes() -> None:
    check_bar(bar())
    check_bar(Bar(0, 10.0, 10.0, 10.0, 10.0, 0.0))


@pytest.mark.parametrize(
    "candidate",
    [
        bar(o=NAN),
        bar(h=INF),
        bar(low=-INF),
        bar(c=NAN),
        bar(o=0.0, low=0.0),
        bar(o=-1.0, low=-2.0),
    ],
)
def test_non_finite_or_non_positive_price_is_rejected(candidate: Bar) -> None:
    with pytest.raises(BarError, match="sonlu ve pozitif"):
        check_bar(candidate)


@pytest.mark.parametrize(
    "candidate",
    [bar(h=10.25), bar(h=9.5, o=9.5, c=10.0), bar(low=10.25), bar(low=10.75, c=10.5)],
)
def test_high_low_not_containing_open_close_is_rejected(candidate: Bar) -> None:
    with pytest.raises(BarError, match="tutarsız"):
        check_bar(candidate)


@pytest.mark.parametrize("volume", [-1.0, NAN, INF])
def test_bad_volume_is_rejected(volume: float) -> None:
    with pytest.raises(BarError, match="hacmi"):
        check_bar(dataclasses.replace(bar(), volume=volume))


def test_series_requires_strictly_ascending_bars() -> None:
    with pytest.raises(BarError, match="kesin artan"):
        check_ascending([bar(1000), bar(1000)])
    with pytest.raises(BarError, match="kesin artan"):
        BarSeries("X", 60, (bar(2000), bar(1000)), SOURCE)


def test_series_rejects_an_invalid_bar_and_a_bad_length() -> None:
    with pytest.raises(BarError, match="tutarsız"):
        BarSeries("X", 60, (bar(0), bar(60_000, h=1.0)), SOURCE)
    with pytest.raises(BarError, match="bar süresi"):
        BarSeries("X", 0, (), SOURCE)


def test_series_exposes_length_and_first_last_times() -> None:
    series = BarSeries("X", 60, (bar(0), bar(60_000), bar(120_000)), SOURCE)
    assert len(series) == 3
    assert (series.first_ts_ms, series.last_ts_ms) == (0, 120_000)
    empty = BarSeries("X", 60, (), SOURCE)
    assert len(empty) == 0
    assert (empty.first_ts_ms, empty.last_ts_ms) == (None, None)


def test_bars_and_series_are_immutable() -> None:
    with pytest.raises(dataclasses.FrozenInstanceError):
        bar().close = 1.0  # type: ignore[misc]
    with pytest.raises(dataclasses.FrozenInstanceError):
        SOURCE.synthetic = True  # type: ignore[misc]


def test_synthetic_marker_is_a_comment_line() -> None:
    assert SYNTHETIC_MARKER.startswith("#")
