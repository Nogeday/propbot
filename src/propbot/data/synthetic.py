"""Deterministic SYNTHETIC OHLCV bars for developing and testing without market data.

The bars are made up and prove nothing about any strategy (ADR-0007): a written file
starts with the `#propbot-synthetic` marker, the loader carries it as
`DataSource.synthetic`, and every report built on it says "SENTETİK VERİ".

Session template, an ASSUMPTION for test data and not exchange-calendar knowledge: a
23-hour futures-like day in New York time that opens at 18:00 and closes at 17:00 the
next day; sessions run from Sunday evening to Friday afternoon (they are dated by their
closing day, Monday to Friday). A session whose closing date is in `early_closes` ends at
that New York time instead. No holiday is skipped: a test drops the bars it needs gone.

The price process is shaped so that the strategies in `pine/` find their patterns:
volatility clustering; a quiet range 20:00-00:00 New York that is swept in 03:00-09:00 and
followed by a reversal; an opening burst at 09:30 with a range breakout; displacement runs
of three strong bars that leave fair value gaps, and pullbacks into them; trending and
mean-reverting stretches. Prices move in whole ticks; volume is higher 09:30-16:00.

Byte-identical output on every run and platform: `random.Random(seed)` is the only source
of randomness, normal draws are Irwin-Hall sums (additions only, no libm `log`/`cos`
whose last bit may differ between platforms), and no set or dict order is relied on.
"""

import datetime as dt
import math
import random
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Final
from zoneinfo import ZoneInfo

from propbot.core.bars import SYNTHETIC_MARKER, Bar, BarSeries, DataSource
from propbot.core.session import NEW_YORK
from propbot.data.ohlcv import utc_text

FORMAT_SYNTHETIC: Final = "synthetic"
DEFAULT_SYMBOL: Final = "SYNTH-MES"
SESSION_OPEN: Final = dt.time(18, 0)
SESSION_CLOSE: Final = dt.time(17, 0)

_NY: Final = ZoneInfo(NEW_YORK)
_MS_PER_SECOND: Final = 1000
_SECONDS_PER_HOUR: Final = 3600
_REFERENCE_TF: Final = 300  # the process is specified per 5-minute bar and scaled from it
# Base volatility as a share of price per 5-minute bar (MES at 5000: 3 ticks, 0.75 points).
_BASE_VOL_SHARE: Final = 0.00015
_MIN_START_TICKS: Final = 1000
_MAX_DECIMALS: Final = 12
# Bar size caps in base-sigma units: keeps every bar below the validator's outlier line.
_MAX_BODY_UNITS: Final = 7.0
_MAX_WICK_UNITS: Final = 2.0
_SESSION_GAP_UNITS: Final = 3.0

# (start minute of day, volatility multiplier); each applies until the next start.
_TIME_OF_DAY: Final = (
    (0, 0.6),
    (3 * 60, 1.0),
    (8 * 60 + 30, 1.2),
    (9 * 60 + 30, 2.4),
    (10 * 60, 1.7),
    (11 * 60 + 30, 1.2),
    (14 * 60, 1.5),
    (16 * 60, 0.8),
    (20 * 60, 0.45),
)
_ASIA_START: Final = 20 * 60
_ASIA_END: Final = 24 * 60
_SWEEP_FIRST: Final = 3 * 60
_SWEEP_LAST: Final = 9 * 60
_RTH_OPEN: Final = 9 * 60 + 30
_RTH_CLOSE: Final = 16 * 60
_ACTIVE_FIRST: Final = 3 * 60
_SILVER_BULLET: Final = ((10 * 60, 11 * 60), (14 * 60, 15 * 60))
_SWEEP_CHANCE: Final = 0.85
_BREAKOUT_CHANCE: Final = 0.75
_DISPLACEMENT_CHANCE: Final = 0.025  # per 5-minute bar in active hours
_SMALL_GAP_CHANCE: Final = 0.04
_TREND_DRIFT: Final = 0.25  # base sigmas per bar


@dataclass(frozen=True, slots=True)
class _Push:
    """One forced bar: toward `target` by `fraction`, or `strength` sigmas in `direction`."""

    direction: int
    strength: float
    wick: float
    target: int | None = None
    fraction: float = 0.0
    pullback_after: bool = False


class _Process:
    def __init__(self, rng: random.Random, start_ticks: int, tf_seconds: int) -> None:
        self.rng = rng
        self.start_ticks = start_ticks
        self.scale = math.sqrt(tf_seconds / _REFERENCE_TF)
        self.bars_per_5m = _REFERENCE_TF / tf_seconds
        self.unit = max(1.0, start_ticks * _BASE_VOL_SHARE) * self.scale
        self.tf_minutes = tf_seconds // 60
        self.close = start_ticks
        self.vol = 1.0
        self.drift = 0.0
        self.regime_left = 0
        self.anchor: float | None = None
        self.pull = 0.0
        self.anchor_left = 0
        self.queue: list[_Push] = []
        self.asia_high: int | None = None
        self.asia_low: int | None = None
        self.sweep_minute: int | None = None
        self.breakout_minute: int | None = None
        self.history: list[tuple[int, int, int, int]] = []  # open, high, low, close in ticks

    def normal(self) -> float:
        return sum(self.rng.random() for _ in range(12)) - 6.0

    def bar(self, minute: int, first_of_session: bool) -> tuple[int, int, int, int, int]:
        tod = _time_of_day(minute)
        self.vol = min(1.8, max(0.6, 1.0 + 0.94 * (self.vol - 1.0) + 0.08 * self.normal()))
        sigma = self.unit * tod * self.vol
        self._schedule(minute)
        self._update_regime()

        open_ = self.close
        if first_of_session:
            open_ += round(self.normal() * self.unit * _SESSION_GAP_UNITS)
        elif self.rng.random() < _SMALL_GAP_CHANCE:
            open_ += self.rng.choice((-2, -1, 1, 2))
        open_ = max(open_, self.start_ticks // 4)

        wick_scale = 1.0
        noise = 1.0
        push = self.queue.pop(0) if self.queue else None
        if push is not None:
            noise = 0.25
            wick_scale = push.wick
            if push.target is not None:
                mu = push.fraction * (push.target - open_)
            else:
                mu = push.direction * push.strength * self.unit * max(tod, 1.0) * self.vol
        else:
            mu = self.drift * sigma
            if self.anchor is not None:
                mu += self.pull * (self.anchor - open_)
            if open_ < self.start_ticks // 2:
                mu += sigma  # keep prices far from zero over long runs
        max_body = round(_MAX_BODY_UNITS * self.unit)
        body = round(mu + noise * sigma * self.normal())
        body = max(-max_body, min(max_body, body))
        close = open_ + body
        max_wick = round(_MAX_WICK_UNITS * self.unit)
        up = min(max_wick, round(abs(self.normal()) * 0.5 * sigma * wick_scale))
        down = min(max_wick, round(abs(self.normal()) * 0.5 * sigma * wick_scale))
        high = max(open_, close) + up
        low = max(1, min(open_, close) - down)
        self.close = close
        self.history.append((open_, high, low, close))
        if len(self.history) > 3:
            self.history.pop(0)
        if push is not None and push.pullback_after:
            self._pullback()
        self._track_asia(minute, high, low)
        return open_, high, low, close, self._volume(minute, body, sigma)

    def _schedule(self, minute: int) -> None:
        if minute == 0 and self.asia_high is not None and self.asia_low is not None:
            self.anchor = (self.asia_high + self.asia_low) / 2
            self.pull = 0.1
            self.anchor_left = (_SWEEP_FIRST // self.tf_minutes) or 1
            self.sweep_minute = None
            if self.rng.random() < _SWEEP_CHANCE:
                slots = (_SWEEP_LAST - _SWEEP_FIRST) // self.tf_minutes
                self.sweep_minute = _SWEEP_FIRST + self.rng.randrange(slots) * self.tf_minutes
        if minute == _ASIA_START:
            self.asia_high = self.asia_low = None
            self.anchor = float(self.close)
            self.pull = 0.15
            self.anchor_left = (_ASIA_END - _ASIA_START) // self.tf_minutes
            self.queue.clear()
        if minute == _RTH_OPEN:
            self.breakout_minute = None
            if self.rng.random() < _BREAKOUT_CHANCE:
                self.breakout_minute = _RTH_OPEN + self.rng.choice((5, 10, 15, 20))
        if self.queue:
            return
        if minute == self.sweep_minute:
            self.sweep_minute = None
            self._sweep()
        elif self.breakout_minute is not None and minute >= self.breakout_minute:
            self.breakout_minute = None
            self._displacement(self.rng.choice((-1, 1)))
        elif _ACTIVE_FIRST <= minute < _RTH_CLOSE:
            chance = _DISPLACEMENT_CHANCE / self.bars_per_5m
            if any(start <= minute < end for start, end in _SILVER_BULLET):
                chance *= 2.0
            if self.rng.random() < chance:
                direction = (
                    (1 if self.drift > 0 else -1)
                    if self.drift != 0.0 and self.rng.random() < 0.7
                    else self.rng.choice((-1, 1))
                )
                self._displacement(direction)

    def _sweep(self) -> None:
        if self.asia_high is None or self.asia_low is None:
            return
        up = self.rng.random() < 0.5
        extra = self.rng.randint(2, 6)
        target = self.asia_high + extra if up else self.asia_low - extra
        if abs(target - self.close) > 3 * _MAX_BODY_UNITS * self.unit:
            return  # price wandered off the range: no sweep this session
        self.anchor = None
        self.queue.extend(
            _Push(0, 0.0, 0.6, target=target, fraction=fraction) for fraction in (0.5, 0.7, 1.0)
        )
        self._displacement(-1 if up else 1)

    def _displacement(self, direction: int) -> None:
        self.anchor = None
        self.queue.extend(
            (
                _Push(direction, 3.0, 0.2),
                _Push(direction, 3.5, 0.15),
                _Push(direction, 2.0, 0.3, pullback_after=True),
            )
        )

    def _pullback(self) -> None:
        (_, high_2, low_2, _), _, (_, high_0, low_0, _) = self.history
        if low_0 > high_2:
            self.anchor = (low_0 + high_2) / 2
        elif high_0 < low_2:
            self.anchor = (high_0 + low_2) / 2
        else:
            return
        self.pull = 0.3
        self.anchor_left = self.rng.randint(4, 10)
        self.drift = 0.0

    def _update_regime(self) -> None:
        if self.anchor is not None:
            self.anchor_left -= 1
            if self.anchor_left <= 0:
                self.anchor = None
        self.regime_left -= 1
        if self.regime_left > 0:
            return
        self.regime_left = round(self.rng.randint(60, 300) * self.bars_per_5m / 5) or 1
        kind = self.rng.randrange(4)
        if kind == 0:
            self.drift = _TREND_DRIFT
        elif kind == 1:
            self.drift = -_TREND_DRIFT
        else:
            self.drift = 0.0
            if kind == 2 and self.anchor is None:
                self.anchor = float(self.close)
                self.pull = 0.08
                self.anchor_left = self.regime_left

    def _track_asia(self, minute: int, high: int, low: int) -> None:
        if minute >= _ASIA_START:
            self.asia_high = high if self.asia_high is None else max(self.asia_high, high)
            self.asia_low = low if self.asia_low is None else min(self.asia_low, low)

    def _volume(self, minute: int, body: int, sigma: float) -> int:
        base = 2500.0 if _RTH_OPEN <= minute < _RTH_CLOSE else 400.0
        activity = (0.6 + 0.4 * self.vol) * (1.0 + abs(body) / (sigma + 1.0))
        volume = base / self.bars_per_5m * activity * self.rng.uniform(0.7, 1.3)
        return max(1, round(volume))


def generate_bars(
    *,
    symbol: str = DEFAULT_SYMBOL,
    tf_seconds: int = 300,
    start: dt.date,
    sessions: int,
    seed: int,
    tick: float = 0.25,
    start_price: float = 5000.0,
    early_closes: Mapping[dt.date, dt.time] | None = None,
) -> BarSeries:
    """`sessions` synthetic sessions, the first closing on `start` (or the next weekday)."""
    _check_arguments(tf_seconds, sessions, tick, start_price)
    early = dict(early_closes or {})
    tf_minutes = tf_seconds // 60
    for day, close in early.items():
        if close > SESSION_CLOSE or (close.hour * 60 + close.minute) % tf_minutes or close.second:
            raise ValueError(
                f"{day}: erken kapanış {SESSION_CLOSE:%H:%M}'den sonra olamaz ve bar ızgarasında "
                f"({tf_minutes} dk) olmalı"
            )
    decimals = _tick_decimals(tick)
    start_ticks = round(start_price / tick)
    process = _Process(random.Random(seed), start_ticks, tf_seconds)
    tf_ms = tf_seconds * _MS_PER_SECOND

    bars: list[Bar] = []
    for closing_day in _closing_days(start, sessions):
        open_local = dt.datetime.combine(closing_day - dt.timedelta(days=1), SESSION_OPEN, _NY)
        close_local = dt.datetime.combine(closing_day, early.get(closing_day, SESSION_CLOSE), _NY)
        open_ms = _epoch_ms(open_local)
        close_ms = _epoch_ms(close_local)
        open_minute = SESSION_OPEN.hour * 60 + SESSION_OPEN.minute
        # The session spans no DST change (those happen early on Sunday, between sessions),
        # so the New York minute of day advances with the bar index.
        for index, ts_ms in enumerate(range(open_ms, close_ms, tf_ms)):
            minute = (open_minute + index * tf_minutes) % 1440
            o, h, low, c, volume = process.bar(minute, first_of_session=index == 0)
            bars.append(
                Bar(
                    ts_open_ms=ts_ms,
                    open=round(o * tick, decimals),
                    high=round(h * tick, decimals),
                    low=round(low * tick, decimals),
                    close=round(c * tick, decimals),
                    volume=float(volume),
                )
            )
    return BarSeries(
        symbol=symbol,
        tf_seconds=tf_seconds,
        bars=tuple(bars),
        source=DataSource(
            name=f"{symbol}-seed{seed}",
            format=FORMAT_SYNTHETIC,
            sha256="",
            synthetic=True,
            bar_tz="UTC",
        ),
    )


def write_ohlcv_csv(series: BarSeries, path: Path) -> None:
    """Write the series in the generic format `load_ohlcv` reads back exactly.

    A synthetic series gets the marker line first. Numbers use the fewest fixed decimals
    that reproduce every value of their column, so the bytes depend only on the bars.
    """
    price_decimals = _fixed_decimals(
        [price for bar in series.bars for price in (bar.open, bar.high, bar.low, bar.close)]
    )
    volume_decimals = _fixed_decimals([bar.volume for bar in series.bars])
    lines: list[str] = []
    if series.source.synthetic:
        lines.append(
            f"{SYNTHETIC_MARKER} {series.source.name} tf={series.tf_seconds}s — SENTETİK VERİ: "
            "gerçek piyasa değildir, hiçbir stratejiyi kanıtlamaz"
        )
    lines.append("time,open,high,low,close,volume")
    p, v = price_decimals, volume_decimals
    lines.extend(
        f"{utc_text(bar.ts_open_ms)},{bar.open:.{p}f},{bar.high:.{p}f},{bar.low:.{p}f},"
        f"{bar.close:.{p}f},{bar.volume:.{v}f}"
        for bar in series.bars
    )
    with path.open("w", encoding="utf-8", newline="\n") as handle:
        handle.write("\n".join(lines) + "\n")


def _check_arguments(tf_seconds: int, sessions: int, tick: float, start_price: float) -> None:
    if tf_seconds <= 0 or tf_seconds % 60 or _SECONDS_PER_HOUR % tf_seconds:
        raise ValueError("bar süresi 60 dakikayı tam bölen bir dakika sayısı olmalı (1, 5, 15 …)")
    if sessions < 1:
        raise ValueError("seans sayısı en az 1 olmalı")
    if not (math.isfinite(tick) and tick > 0.0):
        raise ValueError("tick pozitif bir sayı olmalı")
    if not (math.isfinite(start_price) and start_price / tick >= _MIN_START_TICKS):
        raise ValueError(f"başlangıç fiyatı en az {_MIN_START_TICKS} tick olmalı")


def _closing_days(start: dt.date, sessions: int) -> list[dt.date]:
    days: list[dt.date] = []
    day = start
    while len(days) < sessions:
        if day.weekday() < 5:
            days.append(day)
        day += dt.timedelta(days=1)
    return days


def _epoch_ms(moment: dt.datetime) -> int:
    return int(moment.timestamp()) * _MS_PER_SECOND


def _time_of_day(minute: int) -> float:
    multiplier = _TIME_OF_DAY[0][1]
    for start, value in _TIME_OF_DAY:
        if minute >= start:
            multiplier = value
    return multiplier


def _tick_decimals(tick: float) -> int:
    for decimals in range(_MAX_DECIMALS + 1):
        if abs(round(tick, decimals) - tick) <= tick * 1e-9:
            return decimals
    raise ValueError("tick en fazla 12 ondalık basamaklı olmalı")


def _fixed_decimals(values: list[float]) -> int:
    for decimals in range(18):
        if all(float(f"{value:.{decimals}f}") == value for value in values):
            return decimals
    raise ValueError("değerler sabit ondalıkla yazılamıyor")
