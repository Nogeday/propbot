"""Backtest engine: the order life cycle of TradingView's broker emulator (ADR-0008).

`run_backtest` walks a bar series once and drives one strategy through the contract of
`propbot.strategies.base`. Every bar, in this order:

1. The bar opens. Every working stop or limit the open has already passed fills at the
   open price (the gap rule: a stop gets the worse open, a limit the better one); then
   working market entries fill at the open, and the gap rule runs again for the exits
   they (or `on_fill`) brought.
2. The bar is walked open -> first extreme -> second extreme -> close. A leg is monotonic
   and orders trigger in the order price reaches them; when several trigger at the same
   price, stops go before targets and both before entries. An entry that fills mid-leg
   activates its exits from that price on.
3. The bar closes. The strategy sees the account at the close and its actions are applied
   in list order: `Close` / `CloseAll` fill at this close, everything else works from the
   next bar on.

Which extreme comes first is the fill model (`FillModel`). `tv_ohlc` is TradingView's
rule (high first when the open is nearer to the high). `pessimistic` walks against the
position: adverse extreme first while a position is open, and favourable extreme first
while the bar starts flat with a working limit entry, so that the move toward the target
is spent before the entry fills. It also calls `Strategy.on_fill` at the instant of a
fill; `tv_ohlc` never does.

Prices. Bars are bid prices and the ask is the bid plus the bar's spread (0 for futures):
buys trigger and fill on the ask, sells on the bid. Market and stop fills are worsened by
the slippage; a limit fills at its level once price has traded `limit_through_ticks`
through it, without slippage. Open profit marks a long at the bid and a short at the ask
and holds no commission; realized profit changes only when a row closes.

Invariant 2 (CLAUDE.md) holds in a backtest too: an entry that fills without a stop, a
batch that leaves open quantity without one, or a batch that moves a stop toward the loss
stops the run with `EngineError`. So does anything the engine would have to guess at: a
reversal, an entry id filled twice at once, an id shared by an entry and an exit order.
"""

import math
from collections.abc import Iterator, Sequence
from dataclasses import dataclass
from typing import Final, NamedTuple, overload

from propbot.backtest.model import EngineConfig, EngineResult, ExitReason, FillModel, TradeRecord
from propbot.core.bars import Bar, BarSeries
from propbot.core.session import LocalClock
from propbot.strategies.base import (
    Action,
    Cancel,
    Close,
    CloseAll,
    ClosedTrade,
    Entry,
    Exit,
    FillEvent,
    OpenTrade,
    Side,
    Strategy,
    StrategyContext,
)

# Float noise allowed when a price is compared with an order level, as a fraction of a
# tick: 4000.03 - 0.01 must still count as one tick through 4000.02.
_LEVEL_EPSILON_TICKS: Final = 1e-6
# A remainder below this fraction of the entry size is float dust, not open quantity.
_QTY_EPSILON: Final = 1e-9

# Orders that trigger at the same price fill in this order: a bar that reaches a stop and
# a target at once is a loss, and an entry never fills ahead of an older position's exit.
_RANK_STOP: Final = 0
_RANK_TARGET: Final = 1
_RANK_ENTRY: Final = 2


class EngineError(Exception):
    """A strategy broke the order contract (`propbot.strategies.base`). Developer-facing."""


@dataclass(slots=True, eq=False)
class _Lot:
    """One filled entry with quantity still open."""

    seq: int
    entry_id: str
    strategy_id: str
    setup: str
    side: Side
    sign: int
    qty: float  # filled size
    left: float  # still open
    entry_price: float
    entry_bar_index: int
    entry_ts_ms: int
    entry_commission: float  # for the whole filled size
    worst: float  # most adverse price since the fill: lowest bid (long) / highest ask (short)
    initial_stop: float = math.nan
    stop_locked: bool = False  # True once the initial stop is final: only tightening from here


@dataclass(frozen=True, slots=True, eq=False)
class _Row:
    """An exit fill. It becomes a `TradeRecord` at the end of the run, because the initial
    stop of its entry may still be re-priced after the row closed (a partial exit inside
    the fill bar of a `tv_ohlc` run)."""

    lot: _Lot
    exit_id: str
    reason: ExitReason
    comment: str
    qty: float
    exit_bar_index: int
    exit_ts_ms: int
    exit_price: float
    gross: float
    entry_commission: float  # this row's share
    commission: float  # entry share + exit commission
    pnl: float
    worst: float  # the lot's adverse extreme when the row closed


class _PrefixView(Sequence[ClosedTrade]):
    """The first `length` rows of an append-only list, frozen at that length.

    Handing the strategy a fresh tuple of every exit so far costs O(trades) per exit and
    O(trades^2) per run; this view costs O(1) and still never changes after the strategy
    got it, because the engine only ever appends to the list behind it.
    """

    __slots__ = ("_items", "_length")

    def __init__(self, items: list[ClosedTrade], length: int) -> None:
        self._items = items
        self._length = length

    def __len__(self) -> int:
        return self._length

    @overload
    def __getitem__(self, index: int) -> ClosedTrade: ...

    @overload
    def __getitem__(self, index: slice) -> tuple[ClosedTrade, ...]: ...

    def __getitem__(self, index: int | slice) -> ClosedTrade | tuple[ClosedTrade, ...]:
        if isinstance(index, slice):
            return tuple(self._items[: self._length][index])
        if not -self._length <= index < self._length:
            raise IndexError("closed trade index out of range")
        return self._items[index % self._length]

    def __iter__(self) -> Iterator[ClosedTrade]:
        items = self._items
        for position in range(self._length):
            yield items[position]

    def __eq__(self, other: object) -> bool:
        if not isinstance(other, Sequence) or isinstance(other, str):
            return NotImplemented
        return len(other) == self._length and all(a == b for a, b in zip(self, other, strict=True))

    __hash__ = None  # type: ignore[assignment]

    def __repr__(self) -> str:
        return f"{type(self).__name__}({tuple(self)!r})"


class _Trigger(NamedTuple):
    distance: float  # how far the bid travels before the order triggers; 0 = it already has
    rank: int
    order: Entry | Exit
    threshold: float  # the trigger level on the bid path
    level: float  # the order's own stop or limit price


def run_backtest(
    series: BarSeries,
    strategy: Strategy,
    config: EngineConfig,
    *,
    session_last: Sequence[bool],
) -> EngineResult:
    """Run `strategy` over `series`; `session_last[i]` is Pine's `session.islastbar` of bar i.

    Quantity still open after the last bar is closed at that bar's close with reason
    `END_OF_DATA`. Raises `EngineError` when the strategy breaks the order contract.
    """
    if len(session_last) != len(series):
        raise ValueError(
            f"session_last has {len(session_last)} flags for a series of {len(series)} bars"
        )
    broker = _Broker(series, strategy, config)
    for index, bar in enumerate(series.bars):
        broker.step(index, bar, bool(session_last[index]))
    return broker.finish(series)


class _Broker:
    """Order book, position and ledger of one run."""

    def __init__(self, series: BarSeries, strategy: Strategy, config: EngineConfig) -> None:
        instrument = config.instrument
        costs = config.costs
        pyramiding = strategy.pyramiding
        if pyramiding < 0:
            raise EngineError(f"strategy {strategy.strategy_id!r}: pyramiding must not be negative")

        self._strategy: Final = strategy
        self._strategy_id: Final = strategy.strategy_id
        # Pine: `pyramiding` is the maximum number of same-direction entries, and 0 still
        # allows one (the Master script sets 2 for its two legs).
        self._max_lots: Final = max(1, pyramiding)
        self._pessimistic: Final = config.fill_model is FillModel.PESSIMISTIC
        self._instrument: Final = instrument
        self._tf_seconds: Final = series.tf_seconds
        self._capital: Final = config.initial_capital
        self._point_value: Final = instrument.point_value
        self._tick: Final = instrument.tick_size
        self._eps: Final = instrument.tick_size * _LEVEL_EPSILON_TICKS
        self._through: Final = config.limit_through_ticks * instrument.tick_size
        self._slip: Final = costs.slippage_ticks * instrument.tick_size
        self._commission_rate: Final = costs.commission_per_unit_side
        self._costs: Final = costs
        # Only a spread table needs the wall clock; a constant spread is read once.
        self._clock: Final = LocalClock(costs.spread_tz) if costs.spread_windows else None
        self._spread = costs.spread_points

        self._entries: dict[str, Entry] = {}
        self._exits: dict[str, Exit] = {}
        self._lots: list[_Lot] = []
        self._lot_by_id: dict[str, _Lot] = {}
        self._rows: list[_Row] = []
        self._closed: list[ClosedTrade] = []
        self._open_view: tuple[OpenTrade, ...] | None = ()
        self._net = 0.0
        self._next_seq = 0
        self._dropped = 0
        # Pessimistic walk: the side whose adverse extreme comes first in this bar.
        self._adverse_side: Side | None = None

        self._index = -1
        self._ts_ms = 0
        self._session_last = False

    def step(self, index: int, bar: Bar, session_last: bool) -> None:
        self._index = index
        self._ts_ms = bar.ts_open_ms
        self._session_last = session_last
        if self._clock is not None:
            minute = self._clock.parts(bar.ts_open_ms).minute_of_day
            self._spread = self._costs.spread_at(minute)
        if self._entries or self._lots:
            self._trade(bar)
        actions = self._strategy.on_bar(bar, self._context(bar.close))
        # Also with no actions: the invariant-2 checks run after every bar with open
        # quantity, and a lot filled in this bar of a `tv_ohlc` run gets its initial stop
        # fixed by this callback.
        if actions or self._lots:
            self._apply(actions or (), bar.close)

    def finish(self, series: BarSeries) -> EngineResult:
        if self._lots:
            close = series.bars[-1].close
            for lot in tuple(self._lots):
                self._close_at_market(lot, close, ExitReason.END_OF_DATA, "")
        trades = tuple(self._record(row) for row in self._rows)
        return EngineResult(
            trades=trades,
            bars=len(series),
            first_ts_ms=series.first_ts_ms,
            last_ts_ms=series.last_ts_ms,
            final_equity=self._capital + math.fsum(trade.pnl for trade in trades),
            dropped_entries=self._dropped,
        )

    # -- inside a bar -----------------------------------------------------------------

    def _trade(self, bar: Bar) -> None:
        open_ = bar.open
        self._adverse_side = self._lots[0].side if self._lots else None
        self._mark_adverse(open_)
        # Gapped exits of the position held overnight go first: they free pyramiding room
        # for a market entry at the same price.
        self._advance(open_, open_, at_open=True)
        for order in [order for order in self._entries.values() if order.limit is None]:
            # An earlier fill's `on_fill` may have cancelled or replaced this one.
            if self._entries.get(order.id) is order:
                long = order.side is Side.LONG
                price = open_ + self._spread + self._slip if long else open_ - self._slip
                self._fill_entry(order, price, open_)
                if self._adverse_side is None and order.id in self._lot_by_id:
                    self._adverse_side = order.side
        self._advance(open_, open_, at_open=True)
        if not (self._lots or self._entries):
            return
        first, second = (bar.high, bar.low) if self._high_first(bar) else (bar.low, bar.high)
        self._advance(open_, first)
        self._advance(first, second)
        self._advance(second, bar.close)

    def _high_first(self, bar: Bar) -> bool:
        if not self._pessimistic:
            # TradingView: the extreme nearer to the open is visited first; a tie goes down.
            # On a 0.1 grid two equal distances differ by float noise (4096.2 - 4096.1 is
            # not 4096.1 - 4096.0), so "nearer" must win by more than that noise.
            return abs(bar.open - bar.high) < abs(bar.open - bar.low) - self._eps
        # A position held at the open or opened by a market entry at the open sees its
        # adverse extreme first, even if a gap closed it at the open already.
        side = self._adverse_side
        if side is None and self._lots:
            side = self._lots[0].side
        if side is not None:
            return side is Side.SHORT
        # Flat with limit entries only: the oldest one decides, and its favourable extreme
        # is spent before it can fill.
        return next(iter(self._entries.values())).side is Side.LONG

    def _advance(self, start: float, target: float, *, at_open: bool = False) -> None:
        """Move the bid from `start` to `target`, filling what triggers on the way."""
        price = start
        while True:
            trigger = self._next_trigger(price, target)
            if trigger is None:
                break
            # Distance 0: the order's condition already holds where price stands (a gap at
            # the open, a level on the wrong side of the market, a level shared with the
            # order that has just filled).
            reached = trigger.distance > 0.0
            point = trigger.threshold if reached else price
            # Only the open may improve on a limit price; inside the bar price passed the
            # level on its way and the order is assumed filled there.
            improved = at_open and not reached
            order = trigger.order
            if isinstance(order, Entry):
                if order.side is Side.LONG:
                    fill = point + self._spread if improved else trigger.level
                else:
                    fill = point if improved else trigger.level
                self._fill_entry(order, fill, point)
            else:
                self._fill_exit(order, trigger, point, reached=reached, improved=improved)
            price = point
        self._mark_adverse(target)

    def _next_trigger(self, price: float, target: float) -> _Trigger | None:
        """The order price reaches first on its way from `price` to `target`, if any."""
        eps = self._eps
        spread = self._spread
        through = self._through
        found: list[_Trigger] = []
        for exit_ in self._exits.values():
            lot = self._lot_by_id.get(exit_.from_entry)
            if lot is None:
                continue  # its entry is still working
            long = lot.sign > 0
            if exit_.stop is not None:
                threshold = exit_.stop if long else exit_.stop - spread
                distance = _travel(price, target, threshold, rising=not long, eps=eps)
                if distance is not None:
                    found.append(_Trigger(distance, _RANK_STOP, exit_, threshold, exit_.stop))
            if exit_.limit is not None:
                threshold = exit_.limit + through if long else exit_.limit - through - spread
                distance = _travel(price, target, threshold, rising=long, eps=eps)
                if distance is not None:
                    found.append(_Trigger(distance, _RANK_TARGET, exit_, threshold, exit_.limit))
        for entry in self._entries.values():
            if entry.limit is None:
                continue
            long = entry.side is Side.LONG
            threshold = entry.limit - through - spread if long else entry.limit + through
            distance = _travel(price, target, threshold, rising=not long, eps=eps)
            if distance is not None:
                found.append(_Trigger(distance, _RANK_ENTRY, entry, threshold, entry.limit))
        if not found:
            return None
        # Levels at the same price can sit a few ulps apart once a spread or the through
        # distance is added (1024.2 - 0.1 > 1024.1), so distances within `eps` are a tie
        # and the rank decides. `min` keeps the first of equals: orders of one rank fill in
        # the order they were placed.
        nearest = min(trigger.distance for trigger in found)
        return min(
            (trigger for trigger in found if trigger.distance <= nearest + eps),
            key=_rank,
        )

    def _fill_entry(self, order: Entry, price: float, bid: float) -> None:
        del self._entries[order.id]
        lots = self._lots
        if lots and lots[0].side is not order.side:
            raise self._error(
                order.id, f"would reverse the {lots[0].side.value} position (not supported)"
            )
        if len(lots) >= self._max_lots:
            # Pine drops an entry beyond the pyramiding limit silently; the run counts it.
            self._dropped += 1
            self._discard_orphan_exits()
            return
        if order.id in self._lot_by_id:
            raise self._error(order.id, "fills while an entry with the same id is still open")
        # Two opposite limit entries may work while flat; once one fills the other would
        # be a reversal order, so the run stops here instead of when (or if) it fills.
        for other in self._entries.values():
            if other.side is not order.side:
                raise self._error(
                    other.id,
                    f"working entry opposite to the {order.side.value} position that just "
                    "opened (reversals are not supported)",
                )
        sign = order.side.sign
        lot = _Lot(
            seq=self._next_seq,
            entry_id=order.id,
            strategy_id=order.strategy_id or self._strategy_id,
            setup=order.comment,
            side=order.side,
            sign=sign,
            qty=order.qty,
            left=order.qty,
            entry_price=price,
            entry_bar_index=self._index,
            entry_ts_ms=self._ts_ms,
            entry_commission=self._commission_rate * order.qty,
            worst=bid if sign > 0 else bid + self._spread,
        )
        self._next_seq += 1
        lots.append(lot)
        self._lot_by_id[order.id] = lot
        self._open_view = None
        if self._pessimistic:
            fill = FillEvent(
                entry_id=order.id,
                side=order.side,
                qty=order.qty,
                price=price,
                bar_index=self._index,
                ts_ms=self._ts_ms,
            )
            actions = self._strategy.on_fill(fill, self._context(bid))
            self._apply(actions or (), bid, filled=order.id)
        else:
            # Provisional: the fill bar's `on_bar` may still re-price it (as Pine does).
            lot.initial_stop = self._protective_stop(lot)

    def _fill_exit(
        self, order: Exit, trigger: _Trigger, point: float, *, reached: bool, improved: bool
    ) -> None:
        lot = self._lot_by_id[order.from_entry]
        long = lot.sign > 0
        ask = point + self._spread
        if long:
            lot.worst = min(lot.worst, point)
        else:
            lot.worst = max(lot.worst, ask)
        if trigger.rank == _RANK_STOP:
            reason = ExitReason.STOP
            # Not reached = already passed: the stop fills at the (worse) market price. A
            # long's trigger level on the bid path is its stop, so `point` covers both cases.
            short_fill = trigger.level if reached else ask
            price = point - self._slip if long else short_fill + self._slip
        else:
            reason = ExitReason.TARGET
            # Only the open may fill a target better than its level.
            price = (point if long else ask) if improved else trigger.level
        del self._exits[order.id]
        share = order.qty_percent / 100.0 * lot.qty
        self._settle(lot, min(share, lot.left), price, order.id, reason, order.comment)

    def _close_at_market(self, lot: _Lot, bid: float, reason: ExitReason, comment: str) -> None:
        price = bid - self._slip if lot.sign > 0 else bid + self._spread + self._slip
        self._settle(lot, lot.left, price, "", reason, comment)

    def _settle(
        self, lot: _Lot, qty: float, price: float, exit_id: str, reason: ExitReason, comment: str
    ) -> None:
        if lot.left - qty <= lot.qty * _QTY_EPSILON:
            qty = lot.left
        entry_commission = lot.entry_commission * (qty / lot.qty)
        commission = entry_commission + self._commission_rate * qty
        gross = (price - lot.entry_price) * lot.sign * qty * self._point_value
        pnl = gross - commission
        self._net += pnl
        self._rows.append(
            _Row(
                lot=lot,
                exit_id=exit_id,
                reason=reason,
                comment=comment,
                qty=qty,
                exit_bar_index=self._index,
                exit_ts_ms=self._ts_ms,
                exit_price=price,
                gross=gross,
                entry_commission=entry_commission,
                commission=commission,
                pnl=pnl,
                worst=lot.worst,
            )
        )
        self._closed.append(
            ClosedTrade(
                entry_id=lot.entry_id,
                exit_id=exit_id,
                strategy_id=lot.strategy_id,
                side=lot.side,
                qty=qty,
                entry_price=lot.entry_price,
                exit_price=price,
                entry_bar_index=lot.entry_bar_index,
                exit_bar_index=self._index,
                entry_ts_ms=lot.entry_ts_ms,
                exit_ts_ms=self._ts_ms,
                profit=pnl,
            )
        )
        self._open_view = None
        lot.left -= qty
        if lot.left <= 0.0:
            self._lots.remove(lot)
            del self._lot_by_id[lot.entry_id]
            # The bracket goes with its entry even if an order re-using the entry's id is
            # working: a stale stop must never attach to a later fill.
            self._discard_exits_of(lot.entry_id)

    def _mark_adverse(self, bid: float) -> None:
        for lot in self._lots:
            if lot.sign > 0:
                if bid < lot.worst:
                    lot.worst = bid
            elif bid + self._spread > lot.worst:
                lot.worst = bid + self._spread

    # -- strategy actions -------------------------------------------------------------

    def _apply(self, actions: Sequence[Action], bid: float, *, filled: str | None = None) -> None:
        """Apply one batch in list order, then enforce invariant 2 on what is open.

        `filled` is the entry id an `on_fill` batch answers; None for an `on_bar` batch.
        """
        stops_before = {
            lot.seq: (self._protective_stop(lot), self._stop_shares(lot))
            for lot in self._lots
            if lot.stop_locked
        }
        for action in actions:
            if isinstance(action, Exit):
                self._place_exit(action)
            elif isinstance(action, Cancel):
                self._cancel(action.id)
            elif filled is not None:
                raise self._error(
                    filled,
                    f"on_fill returned {type(action).__name__}; only Exit and Cancel are allowed",
                )
            elif isinstance(action, Entry):
                self._place_entry(action)
            elif isinstance(action, Close):
                lot = self._lot_by_id.get(action.entry_id)
                if lot is not None:
                    self._close_at_market(lot, bid, ExitReason.CLOSE, action.comment)
            elif isinstance(action, CloseAll):
                for lot in tuple(self._lots):
                    self._close_at_market(lot, bid, ExitReason.CLOSE, action.comment)
            else:
                raise self._error("", f"not an order action: {action!r}")
        self._discard_orphan_exits()

        for lot in self._lots:
            stop = self._protective_stop(lot)
            if not lot.stop_locked:
                lot.initial_stop = stop
                lot.stop_locked = True
                continue
            locked = stops_before.get(lot.seq)
            if locked is None:
                continue
            before, shares_before = locked
            if (stop - before) * lot.sign < -self._eps:
                raise self._error(
                    lot.entry_id,
                    f"stop moved toward the loss, from {before} to {stop} (invariant 2)",
                )
            # The furthest stop alone misses a partial exit widened inside it (half of the
            # position from fill-10 to fill-19 while the other half sits at fill-20), so
            # the whole protection profile of the batch is compared as well.
            self._check_profile(lot, shares_before, self._stop_shares(lot))

        if filled is None and self._lots:
            side = self._lots[0].side
            for order in self._entries.values():
                if order.side is not side:
                    raise self._error(
                        order.id,
                        f"working entry opposite to the open {side.value} position "
                        "(reversals are not supported)",
                    )

    def _place_entry(self, action: Entry) -> None:
        if not (math.isfinite(action.qty) and action.qty > 0.0):
            raise self._error(action.id, f"entry qty must be finite and positive, got {action.qty}")
        if action.limit is not None and not (math.isfinite(action.limit) and action.limit > 0.0):
            raise self._error(
                action.id, f"entry limit must be a positive finite price, got {action.limit}"
            )
        if action.id in self._exits:
            raise self._error(action.id, "entry uses the id of a working exit order")
        if self._lots and self._lots[0].side is not action.side:
            raise self._error(
                action.id,
                f"entry opposite to the open {self._lots[0].side.value} position "
                "(reversals are not supported)",
            )
        self._entries[action.id] = action

    def _place_exit(self, action: Exit) -> None:
        if action.stop is None and action.limit is None:
            raise self._error(action.id, "exit has neither a stop nor a limit")
        for name, level in (("stop", action.stop), ("limit", action.limit)):
            if level is not None and not math.isfinite(level):
                raise self._error(action.id, f"exit {name} must be finite, got {level}")
        if not 0.0 < action.qty_percent <= 100.0:
            raise self._error(
                action.id, f"exit qty_percent must be in (0, 100], got {action.qty_percent}"
            )
        if action.id in self._entries:
            raise self._error(action.id, "exit uses the id of a working entry order")
        self._exits[action.id] = action

    def _cancel(self, order_id: str) -> None:
        if self._entries.pop(order_id, None) is not None:
            # The bracket of a cancelled entry goes with it, unless a filled entry of the
            # same id still owns it.
            if order_id not in self._lot_by_id:
                self._discard_exits_of(order_id)
            return
        self._exits.pop(order_id, None)

    def _discard_exits_of(self, entry_id: str) -> None:
        for exit_id in [o.id for o in self._exits.values() if o.from_entry == entry_id]:
            del self._exits[exit_id]

    def _discard_orphan_exits(self) -> None:
        orphans = [
            order.id
            for order in self._exits.values()
            if order.from_entry not in self._lot_by_id and order.from_entry not in self._entries
        ]
        for exit_id in orphans:
            del self._exits[exit_id]

    def _protective_stop(self, lot: _Lot) -> float:
        """The stop furthest toward the loss among the lot's exits; all of it must be covered."""
        stop: float | None = None
        covered_percent = 0.0
        for order in self._exits.values():
            if order.from_entry == lot.entry_id and order.stop is not None:
                covered_percent += order.qty_percent
                if stop is None or (order.stop - stop) * lot.sign < 0.0:
                    stop = order.stop
        if stop is None:
            raise self._error(lot.entry_id, "open entry has no exit with a stop (invariant 2)")
        if covered_percent / 100.0 * lot.qty < lot.left - lot.qty * _QTY_EPSILON:
            raise self._error(
                lot.entry_id,
                f"stops cover {covered_percent:g} % of the entry but {lot.left:g} of "
                f"{lot.qty:g} is open (invariant 2)",
            )
        return stop

    def _stop_shares(self, lot: _Lot) -> list[tuple[float, float]]:
        """(stop, quantity the exit would close) of every stop-carrying exit of `lot`."""
        return [
            (order.stop, order.qty_percent / 100.0 * lot.qty)
            for order in self._exits.values()
            if order.from_entry == lot.entry_id and order.stop is not None
        ]

    def _check_profile(
        self,
        lot: _Lot,
        before: Sequence[tuple[float, float]],
        after: Sequence[tuple[float, float]],
    ) -> None:
        """No price level may protect less of `lot` after the batch than before it.

        The protection at a level is the open quantity that a stop at that level or
        tighter would close, capped at what is open: shares that overlap or add up past
        100 % protect no more than the position. Comparing levels rather than exit ids
        lets a batch replace one exit by another ("Init" -> "Run") and only refuses
        moving quantity toward the loss. The protection only changes at a stop level, so
        those are the only levels that need a check.
        """
        sign = lot.sign
        eps = self._eps
        open_qty = lot.left
        slack = lot.qty * _QTY_EPSILON

        def protected(shares: Sequence[tuple[float, float]], level: float) -> float:
            # A stop protects at `level` when it is at `level` or on the profit side of it.
            total = math.fsum(qty for stop, qty in shares if (stop - level) * sign >= -eps)
            return min(open_qty, total)

        for level in sorted({stop for stop, _ in before}, key=lambda stop: -stop * sign):
            was = protected(before, level)
            now = protected(after, level)
            if now < was - slack:
                raise self._error(
                    lot.entry_id,
                    f"stop moved toward the loss: {was:g} of {open_qty:g} was protected at "
                    f"{level} or tighter, now {now:g} (invariant 2)",
                )

    # -- views and records ------------------------------------------------------------

    def _context(self, bid: float) -> StrategyContext:
        lots = self._lots
        if not lots:
            return StrategyContext(
                bar_index=self._index,
                instrument=self._instrument,
                tf_seconds=self._tf_seconds,
                session_last_bar=self._session_last,
                position_size=0.0,
                position_avg_price=None,
                open_trades=(),
                closed_trades=_PrefixView(self._closed, len(self._closed)),
                initial_capital=self._capital,
                net_profit=self._net,
                equity=self._capital + self._net,
            )
        ask = bid + self._spread
        size = 0.0
        cost = 0.0
        open_points = 0.0
        for lot in lots:
            size += lot.left
            cost += lot.left * lot.entry_price
            mark = bid if lot.sign > 0 else ask
            open_points += (mark - lot.entry_price) * lot.sign * lot.left
        if self._open_view is None:
            self._open_view = tuple(
                OpenTrade(
                    entry_id=lot.entry_id,
                    strategy_id=lot.strategy_id,
                    side=lot.side,
                    qty=lot.left,
                    entry_price=lot.entry_price,
                    entry_bar_index=lot.entry_bar_index,
                    entry_ts_ms=lot.entry_ts_ms,
                )
                for lot in lots
            )
        return StrategyContext(
            bar_index=self._index,
            instrument=self._instrument,
            tf_seconds=self._tf_seconds,
            session_last_bar=self._session_last,
            position_size=size * lots[0].sign,
            position_avg_price=cost / size,
            open_trades=self._open_view,
            closed_trades=_PrefixView(self._closed, len(self._closed)),
            initial_capital=self._capital,
            net_profit=self._net,
            equity=self._capital + self._net + open_points * self._point_value,
        )

    def _record(self, row: _Row) -> TradeRecord:
        lot = row.lot
        # At least one tick of risk: an entry that filled on its own stop has no distance.
        distance = max(abs(lot.entry_price - lot.initial_stop), self._tick)
        risk_amount = distance * row.qty * self._point_value
        r = row.pnl / risk_amount
        # The entry commission is already paid while the trade floats; the exit's is not.
        floating = (
            row.worst - lot.entry_price
        ) * lot.sign * row.qty * self._point_value - row.entry_commission
        return TradeRecord(
            seq=lot.seq,
            strategy_id=lot.strategy_id,
            entry_id=lot.entry_id,
            exit_id=row.exit_id,
            reason=row.reason,
            setup=lot.setup,
            comment=row.comment,
            side=lot.side,
            qty=row.qty,
            entry_bar_index=lot.entry_bar_index,
            exit_bar_index=row.exit_bar_index,
            entry_ts_ms=lot.entry_ts_ms,
            exit_ts_ms=row.exit_ts_ms,
            entry_price=lot.entry_price,
            exit_price=row.exit_price,
            gross_pnl=row.gross,
            commission=row.commission,
            pnl=row.pnl,
            initial_stop=lot.initial_stop,
            risk_amount=risk_amount,
            r=r,
            mae_r=min(floating / risk_amount, r, 0.0),
        )

    def _error(self, order_id: str, message: str) -> EngineError:
        return EngineError(f"bar {self._index}: order {order_id!r}: {message}")


def _travel(price: float, target: float, level: float, *, rising: bool, eps: float) -> float | None:
    """How far price moves from `price` toward `target` before `level` triggers.

    `rising` orders trigger at or above their level, the others at or below it. 0.0 means
    the condition already holds at `price`; None means this move does not trigger it.
    """
    if rising:
        if price >= level - eps:
            return 0.0
        if target >= level - eps:
            return level - price
    else:
        if price <= level + eps:
            return 0.0
        if target <= level + eps:
            return price - level
    return None


def _rank(trigger: _Trigger) -> int:
    return trigger.rank
