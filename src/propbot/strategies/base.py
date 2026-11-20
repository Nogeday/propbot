"""The contract between a strategy and whatever runs it (ADR-0008).

A strategy is a pure, deterministic, bar-based state machine: it gets one CLOSED bar and
a read-only view of its account, and returns order actions. It reads no clock, file,
network or environment variable (SPEC §2.3). The actions mirror the Pine order calls the
reference scripts in `pine/` use, so a port stays line-by-line comparable:

    strategy.entry(id, direction, qty[, limit])          -> Entry
    strategy.exit(id, from_entry[, qty_percent][, stop][, limit]) -> Exit
    strategy.cancel(id)                                  -> Cancel
    strategy.close(id, immediately = true)               -> Close
    strategy.close_all(immediately = true)               -> CloseAll

Order life cycle (the backtest engine implements it; a live adapter must match it):

- Actions returned for bar N take effect after bar N's close. `Close` / `CloseAll` fill
  at bar N's close; everything else works from bar N+1 on. A market `Entry` fills at
  bar N+1's open; a limit `Entry` fills once price trades through its level.
- An order id names one working order. Returning an `Entry` or `Exit` with the id of a
  working order replaces that order's levels and size (Pine re-issues its brackets on
  every bar). `Cancel` removes a working, unfilled order.
- An `Exit` belongs to the entry named by `from_entry`. It waits while that entry is
  still working, becomes active the moment the entry fills (also inside the fill bar),
  and disappears when the entry's quantity is fully closed or the entry order is gone.
  When it triggers it closes `min(qty_percent % of the filled entry size, what is left
  of that entry)`.
- Every filled entry must have at least one `Exit` with a stop (CLAUDE.md invariant 2);
  the engine refuses to run a strategy that lets an entry fill without one. Once the
  first stop of an entry is in force it may only move toward profit.
- `on_fill` is called at the instant an entry fills so the strategy can re-price its
  bracket from the real fill price (SPEC §3.1). It may return `Exit` and `Cancel` only.
  In the TradingView-emulating fill model (parity runs) the engine does not call it: the
  strategy then re-prices at the fill bar's close from `on_bar`, exactly as Pine does. A
  strategy must therefore do the same arithmetic in both places (one shared method).
"""

from collections.abc import Sequence
from dataclasses import dataclass
from enum import StrEnum
from typing import Protocol

from propbot.core.bars import Bar
from propbot.core.instrument import Instrument


class Side(StrEnum):
    LONG = "long"
    SHORT = "short"

    @property
    def sign(self) -> int:
        """+1 for long, -1 for short."""
        return 1 if self is Side.LONG else -1


@dataclass(frozen=True, slots=True)
class Entry:
    id: str
    side: Side
    qty: float
    limit: float | None = None  # None: market order, fills at the next bar's open
    comment: str = ""  # the setup ("FVG", "IFVG", ...); copied to the trade record
    strategy_id: str = ""  # "" = the running strategy's id (the Master script names its leg)


@dataclass(frozen=True, slots=True)
class Exit:
    id: str
    from_entry: str
    stop: float | None = None
    limit: float | None = None
    qty_percent: float = 100.0
    comment: str = ""


@dataclass(frozen=True, slots=True)
class Cancel:
    id: str


@dataclass(frozen=True, slots=True)
class Close:
    """Close what is left of one entry at this bar's close (Pine `immediately = true`)."""

    entry_id: str
    comment: str


@dataclass(frozen=True, slots=True)
class CloseAll:
    """Close the whole position at this bar's close (Pine `immediately = true`).

    As in Pine, working ENTRY orders are not cancelled by it: a strategy with a working
    limit entry returns a `Cancel` as well.
    """

    comment: str


type Action = Entry | Exit | Cancel | Close | CloseAll


@dataclass(frozen=True, slots=True)
class OpenTrade:
    """Pine `strategy.opentrades.*`: one filled entry that still has quantity open."""

    entry_id: str
    strategy_id: str
    side: Side
    qty: float  # still open
    entry_price: float
    entry_bar_index: int
    entry_ts_ms: int


@dataclass(frozen=True, slots=True)
class ClosedTrade:
    """Pine `strategy.closedtrades.*`: one exit fill (a partial exit is its own row)."""

    entry_id: str
    exit_id: str  # the Exit order's id; "" for Close / CloseAll / end of data
    strategy_id: str
    side: Side
    qty: float
    entry_price: float
    exit_price: float
    entry_bar_index: int
    exit_bar_index: int
    entry_ts_ms: int
    exit_ts_ms: int
    profit: float  # net: after this row's share of both commissions


@dataclass(frozen=True, slots=True)
class FillEvent:
    """An entry order filled. Passed to `Strategy.on_fill`."""

    entry_id: str
    side: Side
    qty: float
    price: float
    bar_index: int
    ts_ms: int  # open time of the bar the fill happened in


@dataclass(frozen=True, slots=True)
class StrategyContext:
    """Read-only account view. In `on_bar` it describes the just-closed bar's close.

    The sequences are the engine's own; a strategy must not mutate them.
    """

    bar_index: int  # 0-based index of the bar in the series (Pine `bar_index`)
    instrument: Instrument
    tf_seconds: int
    session_last_bar: bool  # Pine `session.islastbar`: no bar follows in this session
    position_size: float  # signed: > 0 long, < 0 short, 0 flat (Pine `strategy.position_size`)
    position_avg_price: float | None  # None when flat
    open_trades: Sequence[OpenTrade]
    closed_trades: Sequence[ClosedTrade]  # every exit fill so far, in fill order
    initial_capital: float
    net_profit: float  # realized, after commissions (Pine `strategy.netprofit`)
    equity: float  # initial capital + net profit + open profit at this price


class Strategy(Protocol):
    @property
    def strategy_id(self) -> str:
        """The id the Pine script sends in its alerts (`propbot.signals.schema`)."""
        ...

    @property
    def pyramiding(self) -> int:
        """Most same-direction entries open at once (Pine `pyramiding`; 0 and 1 both mean one)."""
        ...

    def on_bar(self, bar: Bar, ctx: StrategyContext) -> Sequence[Action]:
        """Decide on a closed bar. Called exactly once per bar, in order."""
        ...

    def on_fill(self, fill: FillEvent, ctx: StrategyContext) -> Sequence[Action]:
        """Re-price the bracket of the entry that just filled. `Exit` and `Cancel` only."""
        ...
