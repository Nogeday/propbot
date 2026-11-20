"""Property tests: incremental indicators against naive O(n*length) recomputations."""

import math
import statistics
from collections.abc import Sequence
from itertools import pairwise

from hypothesis import given, settings
from hypothesis import strategies as st

from propbot.strategies.indicators import (
    Ema,
    Highest,
    HtfEma,
    Lowest,
    PivotHigh,
    PivotLow,
    PivotTies,
    Rma,
    Rsi,
    Sma,
    Stdev,
)

PRICES = st.lists(
    st.floats(min_value=1.0, max_value=10_000.0, allow_nan=False, allow_infinity=False),
    min_size=1,
    max_size=60,
)
LENGTHS = st.integers(min_value=1, max_value=12)
# Coarse values make equal highs/lows (pivot ties) frequent.
COARSE = st.lists(st.integers(min_value=0, max_value=5).map(float), min_size=1, max_size=40)
HOUR_MS = 3_600_000
STEP_MS = 5 * 60 * 1000


def close_to(a: float, b: float) -> bool:
    return math.isclose(a, b, rel_tol=1e-9, abs_tol=1e-9)


def naive_ema(xs: Sequence[float], length: int) -> list[float]:
    alpha = 2.0 / (length + 1)
    out: list[float] = []
    for x in xs:
        out.append(x if not out else alpha * x + (1 - alpha) * out[-1])
    return out


def naive_rma(xs: Sequence[float], length: int) -> list[float | None]:
    out: list[float | None] = []
    for i, x in enumerate(xs):
        prev = out[-1] if out else None
        if prev is None:
            out.append(statistics.fmean(xs[: i + 1]) if i + 1 == length else None)
        else:
            out.append(x / length + (1 - 1 / length) * prev)
    return out


@settings(max_examples=150)
@given(xs=PRICES, length=LENGTHS)
def test_sma_equals_fmean_of_window(xs: list[float], length: int) -> None:
    sma = Sma(length)
    for i, x in enumerate(xs):
        got = sma.update(x)
        if i + 1 < length:
            assert got is None
        else:
            assert got is not None
            assert close_to(got, statistics.fmean(xs[i + 1 - length : i + 1]))


@settings(max_examples=150)
@given(xs=PRICES, length=LENGTHS)
def test_ema_and_rma_match_naive_and_stay_within_input_range(xs: list[float], length: int) -> None:
    ema, rma = Ema(length), Rma(length)
    want_ema, want_rma = naive_ema(xs, length), naive_rma(xs, length)
    lo, hi = min(xs), max(xs)
    for i, x in enumerate(xs):
        e, r = ema.update(x), rma.update(x)
        assert e is not None
        assert close_to(e, want_ema[i])
        assert lo - 1e-9 <= e <= hi + 1e-9
        w = want_rma[i]
        if w is None:
            assert r is None
        else:
            assert r is not None
            assert close_to(r, w)
            assert lo - 1e-9 <= r <= hi + 1e-9


@settings(max_examples=150)
@given(xs=PRICES, length=LENGTHS)
def test_highest_lowest_match_window_extremes(xs: list[float], length: int) -> None:
    highest, lowest = Highest(length), Lowest(length)
    for i, x in enumerate(xs):
        h, lo = highest.update(x), lowest.update(x)
        window = xs[max(0, i + 1 - length) : i + 1]
        if i + 1 < length:
            assert h is None
            assert lo is None
            continue
        assert h is not None
        assert lo is not None
        assert all(h >= v for v in window)
        assert all(lo <= v for v in window)
        assert h in window
        assert lo in window


@settings(max_examples=150)
@given(xs=PRICES, length=LENGTHS)
def test_stdev_matches_pstdev_and_is_non_negative(xs: list[float], length: int) -> None:
    stdev = Stdev(length)
    for i, x in enumerate(xs):
        got = stdev.update(x)
        if got is None:
            assert i + 1 < length
            continue
        assert got >= 0.0
        want = statistics.pstdev(xs[i + 1 - length : i + 1])
        assert math.isclose(got, want, rel_tol=1e-9, abs_tol=1e-6)


@settings(max_examples=100)
@given(
    x=st.floats(min_value=0.01, max_value=10_000.0, allow_nan=False),
    n=st.integers(1, 30),
    length=LENGTHS,
)
def test_stdev_of_constant_input_is_zero(x: float, n: int, length: int) -> None:
    stdev = Stdev(length)
    for got in (stdev.update(x) for _ in range(n)):
        assert got is None or got == 0.0


def naive_rsi(xs: Sequence[float], length: int) -> list[float | None]:
    ups = [max(b - a, 0.0) for a, b in pairwise(xs)]
    downs = [max(a - b, 0.0) for a, b in pairwise(xs)]
    out: list[float | None] = [None]
    for up, down in zip(naive_rma(ups, length), naive_rma(downs, length), strict=True):
        if up is None or down is None:
            out.append(None)
        elif down == 0:
            out.append(100.0)
        elif up == 0:
            out.append(0.0)
        else:
            out.append(100 - 100 / (1 + up / down))
    return out[: len(xs)]


@settings(max_examples=150)
@given(xs=PRICES, length=LENGTHS)
def test_rsi_matches_naive_and_stays_in_0_100(xs: list[float], length: int) -> None:
    rsi = Rsi(length)
    for got, want in zip((rsi.update(x) for x in xs), naive_rsi(xs, length), strict=True):
        if want is None:
            assert got is None
        else:
            assert got is not None
            assert 0.0 <= got <= 100.0
            # rma(d) can be a tiny positive float in one model and exactly 0 in the other
            assert math.isclose(got, want, abs_tol=1e-6)


def naive_pivot(
    xs: Sequence[float], left: int, right: int, ties: PivotTies, sign: float
) -> list[float | None]:
    out: list[float | None] = []
    for t in range(len(xs)):
        c = t - right
        if c - left < 0:
            out.append(None)
            continue
        v = sign * xs[c]
        lefts = [sign * xs[j] for j in range(c - left, c)]
        rights = [sign * xs[j] for j in range(c + 1, t + 1)]
        ok_right = all(v > r for r in rights)
        ok_left = all(v > q for q in lefts) if ties == "strict" else all(v >= q for q in lefts)
        out.append(xs[c] if ok_left and ok_right else None)
    return out


@settings(max_examples=150)
@given(
    xs=COARSE,
    left=st.integers(1, 4),
    right=st.integers(1, 4),
    ties=st.sampled_from(["strict", "left_inclusive"]),
)
def test_pivots_match_naive(xs: list[float], left: int, right: int, ties: PivotTies) -> None:
    ph, pl = PivotHigh(left, right, ties), PivotLow(left, right, ties)
    assert [ph.update(x) for x in xs] == naive_pivot(xs, left, right, ties, 1.0)
    assert [pl.update(x) for x in xs] == naive_pivot(xs, left, right, ties, -1.0)


# Chart bars as (gap in 5-minute steps since the previous bar, close): gaps make missing bars.
CHART = st.lists(
    st.tuples(st.integers(1, 14), st.floats(1.0, 1000.0, allow_nan=False)),
    min_size=1,
    max_size=80,
)


def to_bars(spec: Sequence[tuple[int, float]], start: int) -> list[tuple[int, float]]:
    bars: list[tuple[int, float]] = []
    ts = start
    for gap, close in spec:
        ts += gap * STEP_MS
        bars.append((ts, close))
    return bars


def naive_htf(bars: Sequence[tuple[int, float]], length: int) -> list[float | None]:
    out: list[float | None] = []
    for ts, _ in bars:
        bucket = ts // HOUR_MS
        closes: dict[int, float] = {}
        for ts2, c2 in bars:
            if ts2 // HOUR_MS < bucket:
                closes[ts2 // HOUR_MS] = c2  # last bar of each earlier hour wins
        ordered = [closes[k] for k in sorted(closes)]
        out.append(naive_ema(ordered, length)[-1] if ordered else None)
    return out


@settings(max_examples=100)
@given(spec=CHART, length=LENGTHS, start_step=st.integers(0, 11))
def test_htf_ema_matches_naive_hour_bucketing(
    spec: list[tuple[int, float]], length: int, start_step: int
) -> None:
    bars = to_bars(spec, 1_700_000_000_000 // HOUR_MS * HOUR_MS + start_step * STEP_MS)
    htf = HtfEma(3600, length)
    got = [htf.update(ts, c) for ts, c in bars]
    for g, w in zip(got, naive_htf(bars, length), strict=True):
        assert (g is None) == (w is None)
        if g is not None and w is not None:
            assert close_to(g, w)


@settings(max_examples=100)
@given(
    spec=CHART,
    other=st.lists(st.floats(1.0, 1000.0, allow_nan=False), min_size=80, max_size=80),
    length=LENGTHS,
    data=st.data(),
)
def test_htf_ema_does_not_look_ahead(
    spec: list[tuple[int, float]], other: list[float], length: int, data: st.DataObject
) -> None:
    bars = to_bars(spec, 1_700_000_000_000)
    t = data.draw(st.integers(0, len(bars) - 1))
    first = HtfEma(3600, length)
    base = [first.update(ts, c) for ts, c in bars]

    # Change every close of bar t's own (still forming) hour and of every later bar.
    bucket_t = bars[t][0] // HOUR_MS
    altered = [(ts, other[i] if ts // HOUR_MS >= bucket_t else c) for i, (ts, c) in enumerate(bars)]
    second = HtfEma(3600, length)
    changed = [second.update(ts, c) for ts, c in altered]
    assert changed[: t + 1] == base[: t + 1]
