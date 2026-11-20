"""Pine-compatible incremental indicators (ADR-0008 item 8).

Each indicator is fed once per bar, in bar order, and `update` returns its value for that
bar; `None` is Pine's `na` (not enough data yet). The formulas are those of the Pine v5
reference manual (`pine_sma`, `pine_ema`, `pine_rma`, `pine_atr`, `pine_rsi`,
`pine_stdev`, ...). TradingView is not reachable from this environment, so numerical
equality with the real `ta.*` functions is checked in the parity run (VERIFY, ADR-0007).

Inputs must be finite numbers. The ported strategies only feed real prices and volumes,
so a NaN or infinity means corrupt data upstream: it raises `IndicatorError` instead of
being treated as Pine's `na`, which would silently shift every value that follows.
"""

import math
from collections import deque
from typing import Final, Literal

# `pine_stdev` in the reference manual zeroes a deviation this close to zero, so that a
# constant input yields exactly 0 despite float rounding of the mean.
STDEV_ZERO_EPS: Final = 1e-10

type PivotTies = Literal["strict", "left_inclusive"]
PIVOT_TIES: Final[tuple[PivotTies, ...]] = ("strict", "left_inclusive")

_MS_PER_SECOND: Final = 1000


class IndicatorError(ValueError):
    """An indicator got a bad parameter or input. The message is user-facing (Turkish)."""


def _check_length(name: str, length: int) -> int:
    if isinstance(length, bool) or not isinstance(length, int) or length < 1:
        raise IndicatorError(f"{name} uzunluğu 1 veya daha büyük bir tam sayı olmalı: {length!r}")
    return length


def _check_value(name: str, value: float) -> float:
    if not math.isfinite(value):
        raise IndicatorError(f"{name} girdisi sonlu bir sayı olmalı: {value!r}")
    return value


class Sma:
    """`ta.sma(src, length)`: mean of the last `length` values."""

    __slots__ = ("_length", "_value", "_window")

    def __init__(self, length: int) -> None:
        self._length = _check_length("SMA", length)
        self._window: deque[float] = deque(maxlen=length)
        self._value: float | None = None

    @property
    def value(self) -> float | None:
        return self._value

    def update(self, x: float) -> float | None:
        self._window.append(_check_value("SMA", x))
        if len(self._window) < self._length:
            return None
        # fsum over the window instead of a running sum: no drift over millions of bars.
        self._value = math.fsum(self._window) / self._length
        return self._value


class Ema:
    """`ta.ema(src, length)`: alpha = 2 / (length + 1), seeded with the first value.

    Reference manual: `sum := na(sum[1]) ? src : alpha * src + (1 - alpha) * nz(sum[1])`,
    so the EMA has a value from the first bar on.
    """

    __slots__ = ("_alpha", "_value")

    def __init__(self, length: int) -> None:
        self._alpha = 2.0 / (_check_length("EMA", length) + 1)
        self._value: float | None = None

    @property
    def value(self) -> float | None:
        return self._value

    def update(self, x: float) -> float | None:
        _check_value("EMA", x)
        if self._value is None:
            self._value = x
        else:
            self._value = self._alpha * x + (1.0 - self._alpha) * self._value
        return self._value


class Rma:
    """`ta.rma(src, length)`: alpha = 1 / length, seeded with the SMA of the first `length`."""

    __slots__ = ("_alpha", "_length", "_seed", "_value")

    def __init__(self, length: int) -> None:
        self._length = _check_length("RMA", length)
        self._alpha = 1.0 / length
        self._seed: list[float] = []
        self._value: float | None = None

    @property
    def value(self) -> float | None:
        return self._value

    def update(self, x: float) -> float | None:
        _check_value("RMA", x)
        if self._value is not None:
            self._value = self._alpha * x + (1.0 - self._alpha) * self._value
            return self._value
        self._seed.append(x)
        if len(self._seed) == self._length:
            self._value = math.fsum(self._seed) / self._length
            self._seed = []
        return self._value


class TrueRange:
    """`ta.tr(true)`: high - low on the first bar, then the classic true range."""

    __slots__ = ("_prev_close",)

    def __init__(self) -> None:
        self._prev_close: float | None = None

    def update(self, high: float, low: float, close: float) -> float:
        for price in (high, low, close):
            _check_value("TR", price)
        prev = self._prev_close
        self._prev_close = close
        if prev is None:
            return high - low
        return max(high - low, abs(high - prev), abs(low - prev))


class Atr:
    """`ta.atr(length)` = `ta.rma(ta.tr(true), length)`."""

    __slots__ = ("_rma", "_tr")

    def __init__(self, length: int) -> None:
        self._tr = TrueRange()
        self._rma = Rma(_check_length("ATR", length))

    @property
    def value(self) -> float | None:
        return self._rma.value

    def update(self, high: float, low: float, close: float) -> float | None:
        return self._rma.update(self._tr.update(high, low, close))


class Change:
    """`ta.change(src)` = `src - src[1]`; na on the first bar."""

    __slots__ = ("_prev",)

    def __init__(self) -> None:
        self._prev: float | None = None

    def update(self, x: float) -> float | None:
        _check_value("change", x)
        prev = self._prev
        self._prev = x
        return None if prev is None else x - prev


class Rsi:
    """`ta.rsi(src, length)`.

    u = max(x - x[1], 0), d = max(x[1] - x, 0), both na on the first bar; RMA seeding
    therefore starts on the second bar and the first RSI value is on bar index `length`.
    As in the reference manual, rma(d) == 0 gives 100 (checked first, so a flat input is
    100) and rma(u) == 0 gives 0.
    """

    __slots__ = ("_change", "_down", "_up", "_value")

    def __init__(self, length: int) -> None:
        _check_length("RSI", length)
        self._change = Change()
        self._up = Rma(length)
        self._down = Rma(length)
        self._value: float | None = None

    @property
    def value(self) -> float | None:
        return self._value

    def update(self, x: float) -> float | None:
        diff = self._change.update(x)
        if diff is None:
            return None
        up = self._up.update(max(diff, 0.0))
        down = self._down.update(max(-diff, 0.0))
        if up is None or down is None:
            return None
        if down == 0.0:
            self._value = 100.0
        elif up == 0.0:
            self._value = 0.0
        else:
            self._value = 100.0 - 100.0 / (1.0 + up / down)
        return self._value


class Stdev:
    """`ta.stdev(src, length)` (biased): population deviation around the SMA, / length."""

    __slots__ = ("_length", "_value", "_window")

    def __init__(self, length: int) -> None:
        self._length = _check_length("stdev", length)
        self._window: deque[float] = deque(maxlen=length)
        self._value: float | None = None

    @property
    def value(self) -> float | None:
        return self._value

    def update(self, x: float) -> float | None:
        self._window.append(_check_value("stdev", x))
        if len(self._window) < self._length:
            return None
        mean = math.fsum(self._window) / self._length
        squares = []
        for v in self._window:
            dev = v - mean
            if abs(dev) <= STDEV_ZERO_EPS:
                dev = 0.0
            squares.append(dev * dev)
        self._value = math.sqrt(math.fsum(squares) / self._length)
        return self._value


class _Extreme:
    """Sliding-window max (or min) over the last `length` values, amortized O(1)."""

    __slots__ = ("_count", "_deque", "_length", "_name", "_sign", "_value")

    def __init__(self, name: str, length: int, sign: float) -> None:
        self._name = name
        self._length = _check_length(name, length)
        self._sign = sign  # +1 keeps the maximum; -1 keeps the minimum
        # (index, value) pairs whose values are monotonic in the kept direction.
        self._deque: deque[tuple[int, float]] = deque()
        self._count = 0
        self._value: float | None = None

    @property
    def value(self) -> float | None:
        return self._value

    def update(self, x: float) -> float | None:
        _check_value(self._name, x)
        index = self._count
        self._count += 1
        while self._deque and self._sign * self._deque[-1][1] <= self._sign * x:
            self._deque.pop()
        self._deque.append((index, x))
        if self._deque[0][0] <= index - self._length:
            self._deque.popleft()
        if self._count < self._length:
            return None
        self._value = self._deque[0][1]
        return self._value


class Highest(_Extreme):
    """`ta.highest(src, length)`: the last `length` values INCLUDING the current one."""

    __slots__ = ()

    def __init__(self, length: int) -> None:
        super().__init__("highest", length, 1.0)


class Lowest(_Extreme):
    """`ta.lowest(src, length)`: the last `length` values INCLUDING the current one."""

    __slots__ = ()

    def __init__(self, length: int) -> None:
        super().__init__("lowest", length, -1.0)


class _Pivot:
    __slots__ = ("_left", "_name", "_right", "_sign", "_ties", "_window")

    def __init__(self, name: str, left: int, right: int, ties: PivotTies, sign: float) -> None:
        if ties not in PIVOT_TIES:
            raise IndicatorError(f"{name} eşitlik kuralı bilinmiyor: {ties!r}")
        self._name = name
        self._left = _check_length(f"{name} sol", left)
        self._right = _check_length(f"{name} sağ", right)
        self._ties = ties
        self._sign = sign  # +1 pivot high, -1 pivot low
        self._window: deque[float] = deque(maxlen=left + right + 1)

    def update(self, x: float) -> float | None:
        self._window.append(_check_value(self._name, x))
        if len(self._window) < self._left + self._right + 1:
            return None
        values = list(self._window)
        candidate = self._sign * values[self._left]
        left_side = [self._sign * v for v in values[: self._left]]
        right_side = [self._sign * v for v in values[self._left + 1 :]]
        if any(v >= candidate for v in right_side):
            return None
        if self._ties == "strict":
            if any(v >= candidate for v in left_side):
                return None
        elif any(v > candidate for v in left_side):
            return None
        return values[self._left]


class PivotHigh(_Pivot):
    """`ta.pivothigh(src, left, right)`: on bar t, src[right] if bar t - right is a pivot.

    `ties` decides equal highs: "strict" (default) wants the pivot strictly above every
    bar on both sides; "left_inclusive" wants it strictly above the right side and above
    or equal to the left side. TradingView's actual tie rule is NOT verified (VERIFY):
    compare on a chart with two equal adjacent highs.
    """

    __slots__ = ()

    def __init__(self, left: int, right: int, ties: PivotTies = "strict") -> None:
        super().__init__("pivothigh", left, right, ties, 1.0)


class PivotLow(_Pivot):
    """`ta.pivotlow(src, left, right)`: on bar t, src[right] if bar t - right is a pivot.

    `ties` mirrors `PivotHigh` with "below" for "above". TradingView's actual tie rule is
    NOT verified (VERIFY).
    """

    __slots__ = ()

    def __init__(self, left: int, right: int, ties: PivotTies = "strict") -> None:
        super().__init__("pivotlow", left, right, ties, -1.0)


class HtfEma:
    """Higher-timeframe EMA of close, as the MTF script reads it.

    Pine: `request.security(syminfo.tickerid, "60", ta.ema(close, n)[1],
    lookahead = barmerge.lookahead_on)`. On every chart bar the value is the EMA as of
    the last higher-timeframe bar that had CLOSED before this chart bar opened; na until
    one has closed.

    Higher-timeframe bars are built from the chart bars by bucketing the open time:
    `floor(ts / htf)` in UTC (hour buckets are whole hours in New York as well, DST or
    not). A bucket closes when a chart bar of a later bucket arrives, and its close is the
    close of its last chart bar; a bucket with no chart bars simply does not exist, as on
    TradingView. The value on a chart bar depends only on bars of earlier buckets, so it
    cannot look ahead.
    """

    __slots__ = ("_bucket", "_ema", "_htf_ms", "_last_ts", "_pending_close", "_value")

    def __init__(self, htf_seconds: int, length: int) -> None:
        if isinstance(htf_seconds, bool) or not isinstance(htf_seconds, int) or htf_seconds < 1:
            raise IndicatorError(f"üst zaman dilimi saniyesi pozitif olmalı: {htf_seconds!r}")
        self._htf_ms = htf_seconds * _MS_PER_SECOND
        self._ema = Ema(_check_length("HTF EMA", length))
        self._bucket: int | None = None
        self._last_ts: int | None = None
        self._pending_close = 0.0
        self._value: float | None = None

    @property
    def value(self) -> float | None:
        return self._value

    def update(self, ts_open_ms: int, close: float) -> float | None:
        _check_value("HTF EMA", close)
        if self._last_ts is not None and ts_open_ms <= self._last_ts:
            raise IndicatorError(
                f"HTF EMA barları açılış anına göre kesin artan olmalı "
                f"({self._last_ts} → {ts_open_ms})"
            )
        self._last_ts = ts_open_ms
        bucket = ts_open_ms // self._htf_ms
        if self._bucket is not None and bucket != self._bucket:
            self._value = self._ema.update(self._pending_close)
        self._bucket = bucket
        self._pending_close = close
        return self._value
