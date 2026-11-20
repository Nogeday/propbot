"""Deterministic synthetic bars: committed fixtures, session template and price shape."""

import datetime as dt
import statistics
from pathlib import Path
from zoneinfo import ZoneInfo

import pytest

from propbot.core.bars import SYNTHETIC_MARKER, BarSeries
from propbot.data.ohlcv import load_ohlcv
from propbot.data.synthetic import generate_bars, write_ohlcv_csv
from propbot.data.validate import validate_bars
from support.paths import FIXTURES

NY = ZoneInfo("America/New_York")
OHLCV = FIXTURES / "ohlcv"
SEED = 20261118
# The committed fixtures and the arguments that produce them.
FIXTURE_ARGS = {
    "synthetic_5m.csv": {"tf_seconds": 300, "start": dt.date(2026, 10, 19), "sessions": 20},
    "synthetic_1m.csv": {"tf_seconds": 60, "start": dt.date(2026, 12, 1), "sessions": 3},
}


def fixture_series(name: str) -> BarSeries:
    args = FIXTURE_ARGS[name]
    return generate_bars(
        tf_seconds=int(args["tf_seconds"]),  # type: ignore[call-overload]
        start=args["start"],  # type: ignore[arg-type]
        sessions=int(args["sessions"]),  # type: ignore[call-overload]
        seed=SEED,
    )


def minute_of_day(ts_ms: int) -> int:
    moment = dt.datetime.fromtimestamp(ts_ms // 1000, NY)
    return moment.hour * 60 + moment.minute


@pytest.mark.parametrize("name", sorted(FIXTURE_ARGS))
def test_committed_fixture_is_the_generator_output_byte_for_byte(name: str, tmp_path: Path) -> None:
    path = tmp_path / name
    write_ohlcv_csv(fixture_series(name), path)
    assert path.read_bytes() == (OHLCV / name).read_bytes()
    assert (OHLCV / name).stat().st_size < 350 * 1024


@pytest.mark.parametrize("name", sorted(FIXTURE_ARGS))
def test_committed_fixture_loads_as_synthetic_and_validates_clean(name: str) -> None:
    series = load_ohlcv(OHLCV / name, symbol="SYNTH-MES")
    assert series.source.synthetic
    assert series.bars == fixture_series(name).bars
    report = validate_bars(series)
    assert report.clean, report.to_dict()
    assert report.session_count == FIXTURE_ARGS[name]["sessions"]


def test_same_seed_same_bars_other_seed_other_bars() -> None:
    first = generate_bars(start=dt.date(2026, 3, 2), sessions=2, seed=1)
    assert generate_bars(start=dt.date(2026, 3, 2), sessions=2, seed=1) == first
    assert generate_bars(start=dt.date(2026, 3, 2), sessions=2, seed=2).bars != first.bars


def test_series_is_marked_synthetic() -> None:
    series = generate_bars(start=dt.date(2026, 3, 2), sessions=1, seed=1, symbol="SYN")
    assert series.symbol == "SYN"
    assert series.source.synthetic
    assert series.source.format == "synthetic"


def test_written_file_starts_with_the_marker(tmp_path: Path) -> None:
    path = tmp_path / "s.csv"
    write_ohlcv_csv(generate_bars(start=dt.date(2026, 3, 2), sessions=1, seed=1), path)
    first, header = path.read_text(encoding="utf-8").splitlines()[:2]
    assert first.startswith(SYNTHETIC_MARKER)
    assert "SENTETİK VERİ" in first
    assert header == "time,open,high,low,close,volume"


@pytest.mark.parametrize("tf_seconds", [60, 300, 900])
def test_session_template_sunday_evening_to_friday_afternoon(tf_seconds: int) -> None:
    # Starts on a Saturday: the first session closes on Monday 2026-03-09.
    series = generate_bars(tf_seconds=tf_seconds, start=dt.date(2026, 3, 7), sessions=6, seed=3)
    tf_ms = tf_seconds * 1000
    starts = [0] + [
        index
        for index in range(1, len(series.bars))
        if series.bars[index].ts_open_ms - series.bars[index - 1].ts_open_ms != tf_ms
    ]
    assert len(starts) == 6
    bars_per_session = 23 * 3600 // tf_seconds
    assert len(series.bars) == 6 * bars_per_session
    opens = [dt.datetime.fromtimestamp(series.bars[i].ts_open_ms // 1000, NY) for i in starts]
    assert [moment.date() for moment in opens] == [
        dt.date(2026, 3, 8),
        dt.date(2026, 3, 9),
        dt.date(2026, 3, 10),
        dt.date(2026, 3, 11),
        dt.date(2026, 3, 12),
        dt.date(2026, 3, 15),
    ]
    assert all((moment.hour, moment.minute) == (18, 0) for moment in opens)


def test_every_bar_is_valid_on_the_tick_grid_with_positive_volume() -> None:
    series = generate_bars(tf_seconds=60, start=dt.date(2026, 6, 1), sessions=2, seed=11, tick=0.1)
    for bar in series.bars:
        for price in (bar.open, bar.high, bar.low, bar.close):
            assert round(price / 0.1) * 0.1 == pytest.approx(price, abs=1e-9)
            assert price == round(price, 1)
        assert bar.low <= min(bar.open, bar.close) <= max(bar.open, bar.close) <= bar.high
        assert bar.volume >= 1.0
        assert bar.volume == int(bar.volume)


@pytest.mark.parametrize("seed", [1, 2, 3, 20261118])
@pytest.mark.parametrize("tf_seconds", [60, 300])
def test_generated_series_validates_clean(seed: int, tf_seconds: int) -> None:
    sessions = 3 if tf_seconds == 60 else 10
    series = generate_bars(
        tf_seconds=tf_seconds, start=dt.date(2026, 3, 2), sessions=sessions, seed=seed
    )
    report = validate_bars(series)
    assert report.clean, report.to_dict()
    assert report.zero_volume_bars == 0


def test_price_shape_carries_the_patterns_the_strategies_look_for() -> None:
    series = fixture_series("synthetic_5m.csv")
    bars = series.bars
    minutes = [minute_of_day(bar.ts_open_ms) for bar in bars]

    bull = sum(1 for i in range(2, len(bars)) if bars[i].low > bars[i - 2].high)
    bear = sum(1 for i in range(2, len(bars)) if bars[i].high < bars[i - 2].low)
    assert bull > 100
    assert bear > 100

    # Consecutive bars normally open at the previous close.
    same = sum(
        1
        for i in range(1, len(bars))
        if minutes[i] != 18 * 60 and bars[i].open == bars[i - 1].close
    )
    assert same / len(bars) > 0.9

    # The 20:00-00:00 range is swept between 03:00 and 11:00 in most sessions.
    swept = cycles = 0
    high = low = 0.0
    done = True
    for bar, minute in zip(bars, minutes, strict=True):
        if minute == 20 * 60:
            high, low, done = bar.high, bar.low, False
            cycles += 1
        elif minute > 20 * 60:
            high, low = max(high, bar.high), min(low, bar.low)
        elif 3 * 60 <= minute < 11 * 60 and not done and (bar.high > high or bar.low < low):
            swept += 1
            done = True
    assert cycles == 20
    assert swept >= 15

    def mean_range(first: int, last: int) -> float:
        return statistics.fmean(
            bar.high - bar.low
            for bar, minute in zip(bars, minutes, strict=True)
            if first <= minute < last
        )

    def mean_volume(first: int, last: int) -> float:
        return statistics.fmean(
            bar.volume for bar, minute in zip(bars, minutes, strict=True) if first <= minute < last
        )

    assert mean_range(9 * 60 + 30, 10 * 60) > 2 * mean_range(20 * 60, 24 * 60)
    assert mean_volume(9 * 60 + 30, 16 * 60) > 3 * mean_volume(0, 9 * 60)


@pytest.mark.parametrize(
    ("kwargs", "message"),
    [
        ({"tf_seconds": 90}, "bar süresi"),
        ({"tf_seconds": 420}, "bar süresi"),
        ({"tf_seconds": 0}, "bar süresi"),
        ({"sessions": 0}, "seans sayısı"),
        ({"tick": 0.0}, "tick"),
        ({"tick": float("nan")}, "tick"),
        ({"start_price": 100.0}, "başlangıç fiyatı"),
        ({"early_closes": {dt.date(2026, 3, 3): dt.time(17, 30)}}, "erken kapanış"),
        ({"early_closes": {dt.date(2026, 3, 3): dt.time(13, 2)}}, "erken kapanış"),
    ],
)
def test_bad_arguments_are_refused(kwargs: dict[str, object], message: str) -> None:
    args: dict[str, object] = {"start": dt.date(2026, 3, 2), "sessions": 1, "seed": 1}
    args.update(kwargs)
    with pytest.raises(ValueError, match=message):
        generate_bars(**args)  # type: ignore[arg-type]
