"""OHLCV bars and bar series (SPEC §2.2).

A bar is stamped with the instant it OPENS, in UTC epoch milliseconds: the same value
TradingView calls `time`. Symbol and bar length belong to the series, not to each bar.
Prices are floats; strategies round to the instrument's tick where a level is an order.
"""

from collections.abc import Sequence
from dataclasses import dataclass
from typing import Final

# Marker line a propbot-generated synthetic file starts with (`propbot data synth`). The
# loader sets `DataSource.synthetic` from it, and every report built on such a series
# carries a "SENTETİK VERİ" warning and can never pass a Go/No-Go gate (ADR-0007).
SYNTHETIC_MARKER: Final = "#propbot-synthetic"


class BarError(ValueError):
    """A bar or a series breaks an invariant. The message is user-facing (Turkish)."""


@dataclass(frozen=True, slots=True)
class Bar:
    ts_open_ms: int
    open: float
    high: float
    low: float
    close: float
    volume: float


@dataclass(frozen=True, slots=True)
class DataSource:
    """Where a series came from; written into every report built on it (SPEC §6.1)."""

    name: str  # file name (no directory: reports may be shared)
    format: str  # "tradingview", "mt5", "generic"
    sha256: str  # of the file's bytes; "" for a series built in memory
    synthetic: bool
    bar_tz: str  # how the file's wall times were read ("UTC", an IANA zone, "NY+7")


@dataclass(frozen=True, slots=True)
class BarSeries:
    """Bars of one symbol and one bar length, strictly ascending by open time."""

    symbol: str
    tf_seconds: int
    bars: tuple[Bar, ...]
    source: DataSource

    def __post_init__(self) -> None:
        if self.tf_seconds <= 0:
            raise BarError("bar süresi pozitif olmalı")
        check_ascending(self.bars)

    def __len__(self) -> int:
        return len(self.bars)

    @property
    def first_ts_ms(self) -> int | None:
        return self.bars[0].ts_open_ms if self.bars else None

    @property
    def last_ts_ms(self) -> int | None:
        return self.bars[-1].ts_open_ms if self.bars else None


def check_bar(bar: Bar) -> None:
    """Raise `BarError` unless the bar's prices are finite, positive and consistent."""
    prices = (bar.open, bar.high, bar.low, bar.close)
    # `x == x` is False only for NaN; the range test rejects both infinities.
    if not all(price == price and 0.0 < price < float("inf") for price in prices):
        raise BarError(f"bar fiyatları sonlu ve pozitif olmalı (açılış anı {bar.ts_open_ms})")
    if bar.high < max(bar.open, bar.close) or bar.low > min(bar.open, bar.close):
        raise BarError(f"bar tutarsız: high/low, open/close'u kapsamıyor ({bar.ts_open_ms})")
    if not (bar.volume == bar.volume and 0.0 <= bar.volume < float("inf")):
        raise BarError(f"bar hacmi sonlu ve negatif olmayan bir sayı olmalı ({bar.ts_open_ms})")


def check_ascending(bars: Sequence[Bar]) -> None:
    """Raise `BarError` unless every bar is valid and open times strictly increase."""
    previous: int | None = None
    for bar in bars:
        check_bar(bar)
        if previous is not None and bar.ts_open_ms <= previous:
            raise BarError(
                "barlar açılış anına göre kesin artan sırada olmalı "
                f"({previous} → {bar.ts_open_ms})"
            )
        previous = bar.ts_open_ms
