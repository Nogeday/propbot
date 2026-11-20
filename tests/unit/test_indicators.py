"""Hand-computed vectors for the Pine-compatible indicators (ADR-0008 item 8)."""

import datetime as dt
import math
from collections.abc import Callable, Sequence
from zoneinfo import ZoneInfo

import pytest

from propbot.strategies.indicators import (
    Atr,
    Change,
    Ema,
    Highest,
    HtfEma,
    IndicatorError,
    Lowest,
    PivotHigh,
    PivotLow,
    Rma,
    Rsi,
    Sma,
    Stdev,
    TrueRange,
)

NY = ZoneInfo("America/New_York")
HOUR_S = 3600


def feed(update: Callable[[float], float | None], xs: Sequence[float]) -> list[float | None]:
    return [update(x) for x in xs]


def assert_series(got: Sequence[float | None], want: Sequence[float | None]) -> None:
    assert len(got) == len(want)
    for g, w in zip(got, want, strict=True):
        if w is None:
            assert g is None
        else:
            assert g == pytest.approx(w, rel=1e-12, abs=1e-12)


def utc_ms(year: int, month: int, day: int, hour: int, minute: int = 0) -> int:
    return int(dt.datetime(year, month, day, hour, minute, tzinfo=dt.UTC).timestamp() * 1000)


def test_sma_is_mean_of_last_length_values_and_na_during_warmup() -> None:
    # (1+2+3)/3 = 2, (2+3+4)/3 = 3, (3+4+5)/3 = 4
    assert_series(feed(Sma(3).update, [1, 2, 3, 4, 5]), [None, None, 2, 3, 4])


def test_ema_is_seeded_with_first_value() -> None:
    # alpha = 2/(3+1) = 0.5: 2; 0.5*4+0.5*2 = 3; 0.5*6+0.5*3 = 4.5; 0.5*8+0.5*4.5 = 6.25
    assert_series(feed(Ema(3).update, [2, 4, 6, 8]), [2, 3, 4.5, 6.25])


def test_rma_is_seeded_with_sma_of_first_length_values() -> None:
    # seed (3+6+9)/3 = 6; then alpha = 1/3: 12/3 + 6*2/3 = 8; 15/3 + 8*2/3 = 31/3
    assert_series(feed(Rma(3).update, [3, 6, 9, 12, 15]), [None, None, 6, 8, 31 / 3])


BARS_HLC = [(10.0, 8.0, 9.0), (12.0, 9.0, 11.0), (11.0, 7.0, 8.0), (15.0, 13.0, 14.0)]


def test_true_range_uses_high_minus_low_on_first_bar() -> None:
    # 10-8 = 2; max(3, |12-9|=3, |9-9|=0) = 3; max(4, |11-11|=0, |7-11|=4) = 4;
    # max(2, |15-8|=7, |13-8|=5) = 7 (gap up)
    tr = TrueRange()
    assert [tr.update(*b) for b in BARS_HLC] == [2.0, 3.0, 4.0, 7.0]


def test_atr_is_rma_of_true_range() -> None:
    # TR 2, 3, 4, 7; seed (2+3)/2 = 2.5; 0.5*4+0.5*2.5 = 3.25; 0.5*7+0.5*3.25 = 5.125
    atr = Atr(2)
    assert_series([atr.update(*b) for b in BARS_HLC], [None, 2.5, 3.25, 5.125])
    assert atr.value == 5.125


def test_rsi_hand_vector() -> None:
    # x: 10, 12, 11, 14, 14 -> change +2, -1, +3, 0 -> u 2, 0, 3, 0; d 0, 1, 0, 0
    # bar 2: up = (2+0)/2 = 1, down = (0+1)/2 = 0.5, rs = 2 -> 100 - 100/3
    # bar 3: up = 0.5*3+0.5*1 = 2, down = 0.5*0+0.5*0.5 = 0.25, rs = 8 -> 100 - 100/9
    # bar 4: up = 1, down = 0.125, rs = 8 -> 100 - 100/9
    got = feed(Rsi(2).update, [10, 12, 11, 14, 14])
    assert_series(got, [None, None, 100 - 100 / 3, 100 - 100 / 9, 100 - 100 / 9])


@pytest.mark.parametrize(
    ("xs", "want"),
    [
        ([1, 2, 3], 100.0),  # no down moves: rma(d) == 0
        ([3, 2, 1], 0.0),  # no up moves: rma(u) == 0
        ([5, 5, 5], 100.0),  # both zero: rma(d) == 0 is checked first
    ],
)
def test_rsi_zero_denominator_edges(xs: list[float], want: float) -> None:
    assert feed(Rsi(2).update, xs)[-1] == want


def test_stdev_is_population_deviation() -> None:
    # mean 5, squared deviations 9,1,1,1,0,0,4,16 sum 32, 32/8 = 4, sqrt = 2
    got = feed(Stdev(8).update, [2, 4, 4, 4, 5, 5, 7, 9])
    assert_series(got, [None] * 7 + [2.0])
    # [1, 3]: mean 2, deviations ±1, sqrt(2/2) = 1 (the sample deviation would be sqrt 2)
    assert feed(Stdev(2).update, [1, 3]) == [None, 1.0]


def test_stdev_of_constant_input_is_exactly_zero() -> None:
    assert feed(Stdev(3).update, [0.1, 0.1, 0.1, 0.1])[2:] == [0.0, 0.0]


def test_highest_and_lowest_include_current_value() -> None:
    xs = [1.0, 3.0, 2.0, 1.0, 0.0, 5.0]
    assert feed(Highest(3).update, xs) == [None, None, 3.0, 3.0, 2.0, 5.0]
    assert feed(Lowest(3).update, xs) == [None, None, 1.0, 1.0, 0.0, 0.0]


def test_highest_length_one_is_the_value_itself() -> None:
    assert feed(Highest(1).update, [4.0, 2.0, 7.0]) == [4.0, 2.0, 7.0]


def test_change_is_difference_to_previous_value() -> None:
    assert feed(Change().update, [5, 7, 4]) == [None, 2, -3]


def test_pivot_high_reported_right_bars_later() -> None:
    # bar 2 (high 5) is above both neighbours on each side; confirmed on bar 2 + 2 = 4
    got = feed(PivotHigh(2, 2).update, [1, 2, 5, 3, 1, 0])
    assert got == [None, None, None, None, 5, None]


def test_pivot_low_reported_right_bars_later() -> None:
    got = feed(PivotLow(2, 2).update, [5, 4, 1, 3, 4, 6])
    assert got == [None, None, None, None, 1, None]


def test_pivot_left_and_right_may_differ() -> None:
    # left 1, right 2: bar 1 (high 4) vs bar 0 (1) and bars 2, 3 (3, 2): confirmed on bar 3
    assert feed(PivotHigh(1, 2).update, [1, 4, 3, 2]) == [None, None, None, 4]


EQUAL_HIGHS = [1.0, 5.0, 5.0, 2.0, 1.0]


def test_pivot_high_strict_rejects_equal_highs() -> None:
    # bar 1 has an equal high on its right, bar 2 one on its left: neither is a pivot
    assert feed(PivotHigh(1, 1).update, EQUAL_HIGHS) == [None] * 5


def test_pivot_high_left_inclusive_takes_the_later_of_equal_highs() -> None:
    # bar 2: left neighbour 5 >= is allowed, right neighbour 2 < 5 -> pivot, confirmed on bar 3
    got = feed(PivotHigh(1, 1, ties="left_inclusive").update, EQUAL_HIGHS)
    assert got == [None, None, None, 5.0, None]


def test_pivot_low_both_tie_rules_with_equal_lows() -> None:
    xs = [5.0, 1.0, 1.0, 3.0, 4.0]
    assert feed(PivotLow(1, 1).update, xs) == [None] * 5
    assert feed(PivotLow(1, 1, ties="left_inclusive").update, xs) == [None, None, None, 1.0, None]


def test_htf_ema_reads_only_closed_hour_across_boundary() -> None:
    # 30-minute chart, 1h EMA(2) (alpha 2/3). Hour 14 closes at 10, hour 15 at 13.
    htf = HtfEma(HOUR_S, 2)
    bars = [
        (utc_ms(2026, 11, 16, 14, 0), 1.0),  # hour 14 open: nothing closed -> na
        (utc_ms(2026, 11, 16, 14, 30), 10.0),  # still hour 14 -> na
        (utc_ms(2026, 11, 16, 15, 0), 20.0),  # hour 14 closed at 10 -> seed 10
        (utc_ms(2026, 11, 16, 15, 30), 13.0),  # the forming hour 15 is not read -> 10
        (utc_ms(2026, 11, 16, 16, 0), 7.0),  # hour 15 closed at 13: 2/3*13 + 1/3*10 = 12
    ]
    assert_series([htf.update(ts, c) for ts, c in bars], [None, None, 10.0, 10.0, 12.0])


def test_htf_ema_survives_a_missing_hour_and_missing_bars() -> None:
    htf = HtfEma(HOUR_S, 2)
    bars = [
        (utc_ms(2026, 11, 16, 14, 0), 10.0),
        (utc_ms(2026, 11, 16, 14, 30), 11.0),
        # hour 15 has no bars at all and hour 16 misses its first bar
        (utc_ms(2026, 11, 16, 16, 30), 5.0),  # hour 14 closed at 11 -> 11
        (utc_ms(2026, 11, 16, 17, 0), 6.0),  # hour 16 closed at 5: 2/3*5 + 1/3*11 = 7
    ]
    assert_series([htf.update(ts, c) for ts, c in bars], [None, None, 11.0, 7.0])


def _ny_hour_key(ts_ms: int) -> tuple[dt.date, int, int]:
    # Wall-clock hour in New York; `fold` separates the repeated 01:00 hour of a fall-back day.
    local = dt.datetime.fromtimestamp(ts_ms / 1000, tz=dt.UTC).astimezone(NY)
    return (local.date(), local.hour, local.fold)


@pytest.mark.parametrize(
    "start",
    [
        utc_ms(2026, 3, 5, 0),  # spring forward on Sunday 2026-03-08
        utc_ms(2026, 10, 29, 0),  # fall back on Sunday 2026-11-01
    ],
)
def test_htf_hour_buckets_match_new_york_hours_through_a_dst_week(start: int) -> None:
    step_ms = 5 * 60 * 1000
    n_bars = 7 * 24 * 12
    bars = [(start + i * step_ms, 100.0 + math.sin(i / 7.0) * 3 + i * 0.01) for i in range(n_bars)]
    htf = HtfEma(HOUR_S, 3)
    got = [htf.update(ts, c) for ts, c in bars]

    # Independent model: group by New York wall-clock hour, EMA the closed hours' closes.
    ref = Ema(3)
    want: list[float | None] = []
    value: float | None = None
    key = None
    last_close = 0.0
    for ts, c in bars:
        k = _ny_hour_key(ts)
        if key is not None and k != key:
            value = ref.update(last_close)
        key = k
        last_close = c
        want.append(value)
    assert got == want


def test_htf_ema_rejects_non_ascending_bars() -> None:
    htf = HtfEma(HOUR_S, 2)
    htf.update(utc_ms(2026, 11, 16, 14), 1.0)
    with pytest.raises(IndicatorError, match="artan"):
        htf.update(utc_ms(2026, 11, 16, 14), 1.0)


@pytest.mark.parametrize(
    "make",
    [
        lambda: Sma(0),
        lambda: Ema(-1),
        lambda: Rma(0),
        lambda: Atr(0),
        lambda: Rsi(0),
        lambda: Stdev(0),
        lambda: Highest(0),
        lambda: Lowest(0),
        lambda: PivotHigh(0, 1),
        lambda: PivotLow(1, 0),
        lambda: HtfEma(0, 5),
        lambda: HtfEma(HOUR_S, 0),
        lambda: Sma(True),
    ],
)
def test_bad_parameters_are_rejected(make: Callable[[], object]) -> None:
    with pytest.raises(IndicatorError):
        make()


def test_unknown_pivot_tie_rule_is_rejected() -> None:
    with pytest.raises(IndicatorError, match="eşitlik"):
        PivotHigh(2, 2, ties="loose")  # type: ignore[arg-type]


@pytest.mark.parametrize("bad", [math.nan, math.inf, -math.inf])
def test_non_finite_input_raises_instead_of_becoming_na(bad: float) -> None:
    for update in (
        Sma(2).update,
        Ema(2).update,
        Rma(2).update,
        Rsi(2).update,
        Stdev(2).update,
        Highest(2).update,
        Lowest(2).update,
        Change().update,
        PivotHigh(1, 1).update,
        PivotLow(1, 1).update,
    ):
        with pytest.raises(IndicatorError, match="sonlu"):
            update(bad)
    with pytest.raises(IndicatorError, match="sonlu"):
        Atr(2).update(1.0, bad, 1.0)
    with pytest.raises(IndicatorError, match="sonlu"):
        HtfEma(HOUR_S, 2).update(0, bad)
