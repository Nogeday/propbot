"""Shared types of the backtest: costs, fill model, engine configuration and trade records.

`TradeRecord` is one exit fill, the same unit as a row pair in TradingView's "List of
Trades" (a partial exit is its own row). Rows of one entry share `seq`; analytics, the
Monte Carlo simulator and the Go/No-Go gates work on whole entries (`group_by_entry`).
Engine semantics: ADR-0008 and `propbot.strategies.base`.
"""

import math
from collections.abc import Sequence
from dataclasses import dataclass, replace
from enum import StrEnum
from typing import Final

from propbot.core.instrument import Instrument
from propbot.strategies.base import Side

_MINUTES_PER_DAY: Final = 1440


class FillModel(StrEnum):
    # Default. While a position is open the bar is walked adverse extreme first (a long
    # sees the low before the high), so a bar that touches both stop and target is a
    # loss. A bar that starts flat with a working limit entry is walked the other way
    # round: the move toward the target comes BEFORE the entry fills.
    PESSIMISTIC = "pessimistic"
    # TradingView's broker emulator, for parity runs only (SPEC §3.1): open -> high ->
    # low -> close when the open is nearer to the high, else open -> low -> high -> close;
    # brackets are re-priced at the fill bar's close instead of at the fill.
    TV_OHLC = "tv_ohlc"


class ExitReason(StrEnum):
    STOP = "stop"
    TARGET = "target"
    CLOSE = "close"  # the strategy closed at a bar's close (session end, breaker, ...)
    END_OF_DATA = "end_of_data"  # the series ended with the trade open: not a strategy exit


@dataclass(frozen=True, slots=True)
class SpreadWindow:
    """A wider (or narrower) spread between two wall-clock minutes of the session day."""

    start_minute: int  # minute of day in the cost model's time zone, inclusive
    end_minute: int  # exclusive; a window may wrap midnight (start > end)
    spread_points: float

    def __post_init__(self) -> None:
        for name in ("start_minute", "end_minute"):
            if not 0 <= getattr(self, name) < _MINUTES_PER_DAY:
                raise ValueError(f"{name} must be a minute of the day (0-1439)")
        if self.start_minute == self.end_minute:
            raise ValueError("a spread window must not be empty")
        _require_cost("spread_points", self.spread_points)

    def contains(self, minute_of_day: int) -> bool:
        if self.start_minute < self.end_minute:
            return self.start_minute <= minute_of_day < self.end_minute
        return minute_of_day >= self.start_minute or minute_of_day < self.end_minute


@dataclass(frozen=True, slots=True)
class CostModel:
    """Trading costs of one instrument at one venue (SPEC §6.3).

    Bars of a CFD are bid prices: every buy (long entry, short exit, short stop) happens
    at bid + spread, every sell at the bid. Futures bars are trade prices: spread is 0
    and the cost is commission plus slippage. Slippage worsens market and stop fills;
    limit fills get none.
    """

    commission_per_unit_side: float  # account currency per unit of size, per side
    slippage_ticks: float  # adverse ticks on every market and stop fill
    spread_points: float = 0.0  # bid/ask spread in price points outside the windows
    spread_windows: tuple[SpreadWindow, ...] = ()  # first matching window wins
    spread_tz: str = "America/New_York"

    def __post_init__(self) -> None:
        _require_cost("commission_per_unit_side", self.commission_per_unit_side)
        _require_cost("slippage_ticks", self.slippage_ticks)
        _require_cost("spread_points", self.spread_points)

    def scaled(self, factor: float) -> "CostModel":
        """Every cost multiplied by `factor` (cost sensitivity matrix: 1x, 1.5x, 2x)."""
        _require_cost("factor", factor)
        return replace(
            self,
            commission_per_unit_side=self.commission_per_unit_side * factor,
            slippage_ticks=self.slippage_ticks * factor,
            spread_points=self.spread_points * factor,
            spread_windows=tuple(
                replace(window, spread_points=window.spread_points * factor)
                for window in self.spread_windows
            ),
        )

    def spread_at(self, minute_of_day: int) -> float:
        for window in self.spread_windows:
            if window.contains(minute_of_day):
                return window.spread_points
        return self.spread_points

    @property
    def is_free(self) -> bool:
        """A cost-free run is for comparison only and is flagged in every report."""
        return (
            self.commission_per_unit_side == 0.0
            and self.slippage_ticks == 0.0
            and self.spread_points == 0.0
            and all(window.spread_points == 0.0 for window in self.spread_windows)
        )


@dataclass(frozen=True, slots=True)
class EngineConfig:
    instrument: Instrument
    costs: CostModel
    fill_model: FillModel = FillModel.PESSIMISTIC
    initial_capital: float = 100_000.0
    # Pine `backtest_fill_limits_assumption`: a limit order fills only after price has
    # traded this many ticks THROUGH its level (a touch is not a fill).
    limit_through_ticks: int = 1

    def __post_init__(self) -> None:
        if not (math.isfinite(self.initial_capital) and self.initial_capital > 0.0):
            raise ValueError("initial_capital must be a positive finite number")
        if self.limit_through_ticks < 0:
            raise ValueError("limit_through_ticks must not be negative")


@dataclass(frozen=True, slots=True)
class TradeRecord:
    seq: int  # number of the entry fill, from 0; rows of one entry share it
    strategy_id: str
    entry_id: str
    exit_id: str  # the Exit order's id; "" for Close / CloseAll / end of data
    reason: ExitReason
    setup: str  # the Entry's comment
    comment: str  # the exit's comment ("EOD close", ...)
    side: Side
    qty: float
    entry_bar_index: int
    exit_bar_index: int
    entry_ts_ms: int  # open time of the bar the entry filled in
    exit_ts_ms: int  # open time of the bar the exit filled in
    entry_price: float
    exit_price: float
    gross_pnl: float
    commission: float  # this row's share of the entry commission plus its exit commission
    pnl: float  # gross_pnl - commission
    initial_stop: float  # the first stop in force for the entry (see ADR-0008)
    risk_amount: float  # |entry_price - initial_stop| * qty * point value
    r: float  # pnl / risk_amount
    mae_r: float  # worst floating result of this quantity while open, in R; <= 0 and <= r


@dataclass(frozen=True, slots=True)
class EntryResult:
    """Every exit of one entry added up: the unit of the statistics and of Monte Carlo."""

    seq: int
    strategy_id: str
    entry_id: str
    setup: str
    side: Side
    qty: float
    entry_ts_ms: int
    exit_ts_ms: int  # the last exit
    entry_price: float
    pnl: float
    risk_amount: float
    r: float
    mae_r: float
    reasons: tuple[ExitReason, ...]  # in fill order
    complete: bool  # False when any part was closed by the end of the data


@dataclass(frozen=True, slots=True)
class EngineResult:
    trades: tuple[TradeRecord, ...]  # in exit-fill order
    bars: int
    first_ts_ms: int | None
    last_ts_ms: int | None
    final_equity: float
    dropped_entries: int  # entries refused by the pyramiding limit (Pine drops them silently)


def group_by_entry(trades: Sequence[TradeRecord]) -> tuple[EntryResult, ...]:
    """Add the rows of each entry up; the result is ordered by entry (`seq`)."""
    rows_by_seq: dict[int, list[TradeRecord]] = {}
    for trade in trades:
        rows_by_seq.setdefault(trade.seq, []).append(trade)
    results: list[EntryResult] = []
    for seq in sorted(rows_by_seq):
        rows = rows_by_seq[seq]
        first = rows[0]
        pnl = math.fsum(row.pnl for row in rows)
        risk_amount = math.fsum(row.risk_amount for row in rows)
        # Size-weighted: each row's mae_r is relative to its own share of the risk.
        mae = math.fsum(row.mae_r * row.risk_amount for row in rows)
        results.append(
            EntryResult(
                seq=seq,
                strategy_id=first.strategy_id,
                entry_id=first.entry_id,
                setup=first.setup,
                side=first.side,
                qty=math.fsum(row.qty for row in rows),
                entry_ts_ms=first.entry_ts_ms,
                exit_ts_ms=max(row.exit_ts_ms for row in rows),
                entry_price=first.entry_price,
                pnl=pnl,
                risk_amount=risk_amount,
                r=pnl / risk_amount,
                mae_r=min(mae / risk_amount, pnl / risk_amount, 0.0),
                reasons=tuple(row.reason for row in rows),
                complete=all(row.reason is not ExitReason.END_OF_DATA for row in rows),
            )
        )
    return tuple(results)


def _require_cost(name: str, value: float) -> None:
    if not (math.isfinite(value) and value >= 0.0):
        raise ValueError(f"{name} must be a non-negative finite number")
