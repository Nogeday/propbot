"""NY Open ORB + FVG/IFVG: port of `pine/ny_open_fvg_strategy.pine` (SPEC §3.2).

The Pine script is the reference (as of commit 144abab). `NyOrbFvg.on_bar` runs the
script's blocks in the script's order, on the same state variables (snake_case of the
Pine names), so the two can be read side by side:

    TIME / DATE / DAY -> daily reset -> opening range -> master gate -> INDICATORS ->
    risk base -> CIRCUIT BREAKERS -> BREAKOUT -> BIAS -> DETECT FVGs -> UPDATE
    INVERSIONS / DEATHS -> ENTRY -> TRADE MANAGEMENT -> forced flats -> management reset

Not ported: plots, boxes, labels, tables, background colours and the alert() JSON (Faz 1
forwards TradingView's own alerts; the weekday table is `backtest/analytics.py`).

Fill-bar re-pricing (SPEC §3.1, ADR-0008): Pine finalizes the initial stop and the target
at the close of the fill bar from `strategy.position_avg_price`. `_finalize_bracket` is
that block; `on_fill` calls it at the fill instant (pessimistic model) and `on_bar` at the
fill bar's close when it has not run yet (tv_ohlc model, Pine's own timing). Both give the
same levels for the same fill price.
"""

import datetime as dt
import math
from collections.abc import Sequence
from dataclasses import dataclass
from typing import Annotated, Final, Literal
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from pydantic import AwareDatetime, Field, field_validator

from propbot.core.bars import Bar
from propbot.core.instrument import InstrumentKind
from propbot.core.session import LocalClock
from propbot.risk.config import StrictModel
from propbot.strategies.base import (
    Action,
    CloseAll,
    Entry,
    Exit,
    FillEvent,
    Side,
    StrategyContext,
)
from propbot.strategies.history import BarHistory
from propbot.strategies.indicators import Atr, Highest, Lowest

STRATEGY_ID: Final = "ny_orb_fvg"
PINE_FILE: Final = "ny_open_fvg_strategy.pine"

# Pine keeps the tracked-gap arrays bounded: `if array.size(aTop) > 60 -> array.shift`.
FVG_CAP: Final = 60

# Order ids and comments exactly as the Pine calls name them.
ENTRY_LONG: Final = "Long"
ENTRY_SHORT: Final = "Short"
EXIT_LONG: Final = "Exit L"
EXIT_SHORT: Final = "Exit S"
FLAT_EOD: Final = "EOD close"
FLAT_DAILY_DD: Final = "Daily DD halt"
FLAT_END_OF_RANGE: Final = "End of test range"

# aType: 1 = bullish FVG, -1 = bearish FVG. aState: 0 fresh, 1 inverted (IFVG), 2 dead.
BULLISH: Final = 1
BEARISH: Final = -1
FRESH: Final = 0
INVERTED: Final = 1
DEAD: Final = 2

_EPOCH: Final = dt.datetime(1970, 1, 1, tzinfo=dt.UTC)
_MS: Final = dt.timedelta(milliseconds=1)

type BreakConfirm = Literal["Close", "Wick"]
type RiskBase = Literal["Initial Capital", "Account Equity", "Fixed Balance"]
type StopMethod = Literal["FVG Buffer", "ATR Based"]
type QtyRound = Literal["Auto", "Whole units", "Fractional"]
type TrailMode = Literal["Swing (1m structure)", "ATR step", "Percent step"]


class NyOrbFvgParams(StrictModel):
    """Every Pine input that affects orders; defaults and limits exactly as in the script.

    Option strings are the Pine option strings verbatim, so a config can be compared with
    the TradingView settings dialog. Not ported (visual or table only): oosStart,
    showDateViz, showTable, tableScope, tablePos, tableSize, showAllFVG, boxProj, fvgProj,
    alertToken.
    """

    # Session & Opening Range
    tz: str = "America/New_York"
    or_start_hour: Annotated[int, Field(ge=0, le=23)] = 9
    or_start_min: Annotated[int, Field(ge=0, le=59)] = 30
    or_minutes: Annotated[int, Field(ge=1, le=60)] = 5
    sess_end_hour: Annotated[int, Field(ge=0, le=23)] = 15
    sess_end_min: Annotated[int, Field(ge=0, le=59)] = 59
    close_at_end: bool = True
    # Backtest Date Range (Pine `time` is compared with these, both inclusive)
    start_date: AwareDatetime = dt.datetime(2024, 1, 1, tzinfo=dt.UTC)
    end_date: AwareDatetime = dt.datetime(2030, 1, 1, tzinfo=dt.UTC)
    # Day of Week Filter
    trade_mon: bool = True
    trade_tue: bool = True
    trade_wed: bool = True
    trade_thu: bool = True
    trade_fri: bool = True
    # Entry
    break_confirm: BreakConfirm = "Close"
    allow_long: bool = True
    allow_short: bool = True
    # FVG / IFVG
    use_fvg: bool = True
    use_ifvg: bool = True
    min_fvg_ticks: Annotated[int, Field(ge=0)] = 0
    # Risk Management
    risk_pct: Annotated[float, Field(ge=0.1)] = 0.5
    risk_base: RiskBase = "Initial Capital"
    fixed_bal: Annotated[float, Field(ge=1)] = 10000.0
    rr: Annotated[float, Field(ge=0.1)] = 2.0
    stop_method: StopMethod = "FVG Buffer"
    sl_buf: Annotated[int, Field(ge=0)] = 2
    atr_len_init: Annotated[int, Field(ge=1)] = 14
    atr_mult_init: Annotated[float, Field(ge=0.1)] = 1.5
    qty_round: QtyRound = "Auto"
    # Circuit Breakers (Daily Lockout)
    use_breakers: bool = True
    daily_dd_pct: Annotated[float, Field(ge=0.1)] = 2.0
    flatten_breach: bool = True
    max_consec_loss: Annotated[int, Field(ge=1)] = 2
    # Trailing Stop
    use_trail: bool = True
    trail_mode: TrailMode = "Swing (1m structure)"
    swing_len: Annotated[int, Field(ge=1)] = 5
    atr_len_t: Annotated[int, Field(ge=1)] = 14
    atr_mult_t: Annotated[float, Field(ge=0.1)] = 1.5
    trail_pct: Annotated[float, Field(ge=0.01)] = 0.3
    trail_act_r: Annotated[float, Field(ge=0.0)] = 1.0
    trail_min_step_ticks: Annotated[int, Field(ge=1)] = 4

    @field_validator("tz")
    @classmethod
    def _known_zone(cls, value: str) -> str:
        try:
            ZoneInfo(value)
        except (ZoneInfoNotFoundError, ValueError) as exc:
            raise ValueError(f"bilinmeyen saat dilimi: {value!r}") from exc
        return value

    @field_validator("start_date", "end_date")
    @classmethod
    def _as_utc(cls, value: dt.datetime) -> dt.datetime:
        # Pine `input.time` is a UTC epoch; any aware datetime names one instant.
        return value.astimezone(dt.UTC)


@dataclass(slots=True)
class Fvg:
    """One tracked gap: a row of Pine's parallel arrays aTop/aBot/aType/aState/aFormBar/aFlipBar."""

    top: float
    bot: float
    kind: int  # aType: BULLISH / BEARISH
    state: int  # aState: FRESH / INVERTED / DEAD
    form_bar: int
    flip_bar: int  # -1 until inverted


def _epoch_ms(moment: dt.datetime) -> int:
    return (moment - _EPOCH) // _MS


class NyOrbFvg:
    """The Pine script as a bar-by-bar state machine (`propbot.strategies.base.Strategy`)."""

    def __init__(self, params: NyOrbFvgParams | None = None) -> None:
        p = params if params is not None else NyOrbFvgParams()
        self.params: Final = p
        self._clock: Final = LocalClock(p.tz)
        self._or_start: Final = p.or_start_hour * 60 + p.or_start_min
        self._or_end: Final = self._or_start + p.or_minutes
        self._sess_end: Final = p.sess_end_hour * 60 + p.sess_end_min
        self._start_ms: Final = _epoch_ms(p.start_date)
        self._end_ms: Final = _epoch_ms(p.end_date)
        # Indexed by LocalParts.weekday (Monday = 0); the script never trades Sat / Sun.
        self._trade_days: Final = (
            p.trade_mon,
            p.trade_tue,
            p.trade_wed,
            p.trade_thu,
            p.trade_fri,
            False,
            False,
        )
        # high[2] / low[2] for the gap test.
        self._history: Final = BarHistory(3)
        self._atr_t: Final = Atr(p.atr_len_t)
        self._atr_init: Final = Atr(p.atr_len_init)
        self._sw_low: Final = Lowest(p.swing_len)
        self._sw_high: Final = Highest(p.swing_len)
        self._bar_count = 0

        # ---- Pine `var` state -------------------------------------------------------
        self.or_high: float | None = None
        self.or_low: float | None = None
        self.or_ready = False
        self.traded_today = False
        self.bias = 0  # 1 long bias, -1 short bias, 0 none
        self.fvgs: list[Fvg] = []
        # Open-trade management
        self.in_dir = 0
        self.in_fvg_stop: float | None = None
        self.in_initial_stop: float | None = None
        self.in_take_profit: float | None = None
        self.in_trail_stop: float | None = None
        self.in_atr_dist: float | None = None
        # Circuit breakers
        self.day_start_equity: float | None = None
        self.day_start_net: float | None = None
        self.consec_loss_today = 0
        self.last_closed_count = 0
        self.halted_dd = False
        self.halted_streak = False
        # Series values of the last bar, kept for inspection (not `var` in Pine).
        self.halted = False
        self.day_base_eq: float | None = None
        # `dayofmonth(time, tz)[1]` and `strategy.closedtrades[1]`: None = na (first bar).
        self._prev_dayofmonth: int | None = None
        self._prev_closed_count: int | None = None

    @property
    def strategy_id(self) -> str:
        return STRATEGY_ID

    @property
    def pyramiding(self) -> int:
        return 0  # strategy(pyramiding = 0)

    # -- the bar ----------------------------------------------------------------------

    def on_bar(self, bar: Bar, ctx: StrategyContext) -> Sequence[Action]:
        p = self.params
        mintick = ctx.instrument.tick_size
        actions: list[Action] = []
        self._history.push(bar)
        self._bar_count += 1

        # ---- TIME / DATE / DAY
        local = self._clock.parts(bar.ts_open_ms)
        cur_min = local.minute_of_day
        is_in_or = self._or_start <= cur_min < self._or_end
        at_session_end = cur_min >= self._sess_end or ctx.session_last_bar
        bar_min = max(1, int(ctx.tf_seconds / 60))
        entry_time_ok = not ctx.session_last_bar and cur_min + bar_min < self._sess_end
        in_date_range = self._start_ms <= bar.ts_open_ms <= self._end_ms
        day_ok = self._trade_days[local.weekday]

        # newDay = ta.change(dayofmonth(time, tz)) != 0: on the first bar the change is na
        # and `na != 0` is false, so the first bar is NOT a new day (the `var` defaults hold).
        new_day = self._prev_dayofmonth is not None and local.day != self._prev_dayofmonth
        self._prev_dayofmonth = local.day
        if new_day:
            self.or_high = None
            self.or_low = None
            self.or_ready = False
            self.traded_today = False
            self.bias = 0
            self.fvgs.clear()
            if ctx.position_size == 0:
                self._reset_management()

        # ---- Build the opening range
        if is_in_or:
            self.or_high = bar.high if self.or_high is None else max(self.or_high, bar.high)
            self.or_low = bar.low if self.or_low is None else min(self.or_low, bar.low)
        # Finalize the range right after the window
        if (
            not self.or_ready
            and self.or_high is not None
            and cur_min >= self._or_end
            and cur_min <= self._sess_end
        ):
            self.or_ready = True
        can_scan = (
            self.or_ready
            and day_ok
            and in_date_range
            and cur_min >= self._or_end
            and cur_min < self._sess_end
        )

        # ---- INDICATORS (every bar)
        buffer = p.sl_buf * mintick  # trailMinStep is computed where it is used (_manage)
        atr_t = self._atr_t.update(bar.high, bar.low, bar.close)
        atr_init = self._atr_init.update(bar.high, bar.low, bar.close)
        sw_low = self._sw_low.update(bar.low)
        sw_high = self._sw_high.update(bar.high)

        # ---- POSITION SIZING: risk base
        if p.risk_base == "Fixed Balance":
            risk_base_amt = p.fixed_bal
        elif p.risk_base == "Account Equity":
            risk_base_amt = ctx.equity
        else:
            risk_base_amt = ctx.initial_capital

        # ---- CIRCUIT BREAKERS
        if new_day:
            self.day_start_equity = ctx.equity
            self.day_start_net = ctx.net_profit
            self.consec_loss_today = 0
            self.halted_dd = False
            self.halted_streak = False
        closed_count = len(ctx.closed_trades)
        if closed_count > self.last_closed_count:
            for j in range(self.last_closed_count, closed_count):
                if ctx.closed_trades[j].profit < 0:
                    self.consec_loss_today += 1
                else:
                    self.consec_loss_today = 0
            self.last_closed_count = closed_count
        # Base capped at initial capital -> the more conservative cash limit.
        if self.day_start_equity is None:
            day_base_eq = ctx.initial_capital
        else:
            day_base_eq = min(self.day_start_equity, ctx.initial_capital)
        self.day_base_eq = day_base_eq
        daily_loss_cap = day_base_eq * (p.daily_dd_pct / 100.0)
        daily_real_pnl = 0.0 if self.day_start_net is None else ctx.net_profit - self.day_start_net
        daily_eq_pnl = 0.0 if self.day_start_equity is None else ctx.equity - self.day_start_equity
        if min(daily_real_pnl, daily_eq_pnl) <= -daily_loss_cap:
            self.halted_dd = True
        if self.consec_loss_today >= p.max_consec_loss:
            self.halted_streak = True
        halted = p.use_breakers and (self.halted_dd or self.halted_streak)
        self.halted = halted

        # ---- BREAKOUT -> BIAS (or_high / or_low are set whenever or_ready is)
        if can_scan and self.bias == 0 and self.or_high is not None and self.or_low is not None:
            if p.break_confirm == "Close":
                broke_up = bar.close > self.or_high
                broke_dn = bar.close < self.or_low
            else:
                broke_up = bar.high > self.or_high
                broke_dn = bar.low < self.or_low
            if broke_up and p.allow_long:
                self.bias = 1
            elif broke_dn and p.allow_short:
                self.bias = -1

        # ---- DETECT FVGs (after breakout)
        if can_scan and self.bias != 0:
            self._detect_fvgs(bar, ctx.bar_index, p.min_fvg_ticks * mintick)

        # ---- UPDATE INVERSIONS / DEATHS (before the entry check)
        if can_scan and self.fvgs:
            for fvg in self.fvgs:
                if fvg.state == DEAD:
                    continue
                if fvg.kind == BULLISH:
                    if fvg.state == FRESH and bar.close < fvg.bot:
                        fvg.state = INVERTED
                        fvg.flip_bar = ctx.bar_index
                    elif fvg.state == INVERTED and bar.close > fvg.top:
                        fvg.state = DEAD
                elif fvg.state == FRESH and bar.close > fvg.top:
                    fvg.state = INVERTED
                    fvg.flip_bar = ctx.bar_index
                elif fvg.state == INVERTED and bar.close < fvg.bot:
                    fvg.state = DEAD

        # ---- ENTRY (pullback into the gap)
        if (
            can_scan
            and entry_time_ok
            and not self.traded_today
            and not halted
            and ctx.position_size == 0
            and self.fvgs
        ):
            if self.bias == 1:
                for fvg in reversed(self.fvgs):
                    if fvg.state == DEAD:
                        continue
                    long_zone = (fvg.kind == BULLISH and fvg.state == FRESH and p.use_fvg) or (
                        fvg.kind == BEARISH and fvg.state == INVERTED and p.use_ifvg
                    )
                    if not long_zone:
                        continue
                    act_bar = fvg.flip_bar if fvg.state == INVERTED else fvg.form_bar
                    if ctx.bar_index > act_bar and bar.low <= fvg.top:
                        actions.extend(
                            self._enter(Side.LONG, fvg, bar, ctx, atr_init, buffer, risk_base_amt)
                        )
                        break  # Pine breaks on the first touched zone, also when qty is 0
            elif self.bias == -1:
                for fvg in reversed(self.fvgs):
                    if fvg.state == DEAD:
                        continue
                    short_zone = (fvg.kind == BEARISH and fvg.state == FRESH and p.use_fvg) or (
                        fvg.kind == BULLISH and fvg.state == INVERTED and p.use_ifvg
                    )
                    if not short_zone:
                        continue
                    act_bar = fvg.flip_bar if fvg.state == INVERTED else fvg.form_bar
                    if ctx.bar_index > act_bar and bar.high >= fvg.bot:
                        actions.extend(
                            self._enter(Side.SHORT, fvg, bar, ctx, atr_init, buffer, risk_base_amt)
                        )
                        break

        # ---- TRADE MANAGEMENT (initial stop + TP + trailing)
        avg = ctx.position_avg_price
        if ctx.position_size > 0 and self.in_dir == 1 and avg is not None:
            actions.append(self._manage(Side.LONG, avg, bar, mintick, buffer, atr_t, sw_low))
        if ctx.position_size < 0 and self.in_dir == -1 and avg is not None:
            actions.append(self._manage(Side.SHORT, avg, bar, mintick, buffer, atr_t, sw_high))

        # ---- Forced flats: one reason per bar, filled at this bar's close
        if p.close_at_end and at_session_end:
            flat_reason = FLAT_EOD
        elif p.use_breakers and p.flatten_breach and self.halted_dd:
            flat_reason = FLAT_DAILY_DD
        elif bar.ts_open_ms > self._end_ms:
            flat_reason = FLAT_END_OF_RANGE
        else:
            flat_reason = ""
        if flat_reason and ctx.position_size != 0:
            actions.append(CloseAll(comment=flat_reason))

        # ---- Reset management vars only after a trade has actually closed. Pine reads
        # `strategy.closedtrades > strategy.closedtrades[1]` (na on the first bar -> false).
        if (
            ctx.position_size == 0
            and self._prev_closed_count is not None
            and closed_count > self._prev_closed_count
            and self.in_dir != 0
        ):
            self._reset_management()
        self._prev_closed_count = closed_count
        return actions

    def on_fill(self, fill: FillEvent, ctx: StrategyContext) -> Sequence[Action]:
        """Pessimistic model: the fill-bar block of TRADE MANAGEMENT, run at the fill instant."""
        avg = ctx.position_avg_price
        if avg is None or fill.side.sign != self.in_dir or self.in_take_profit is not None:
            # Pine manages only a position whose inDir matches; anything else keeps the
            # bracket placed with the entry.
            return ()
        # ATR has a value on the fill bar once that bar is the atrLenInit-th one; the
        # fill bar's own `on_bar` has not run yet, so count it in.
        self._finalize_bracket(
            fill.side,
            avg,
            ctx.instrument.tick_size,
            atr_ready=self._bar_count + 1 >= self.params.atr_len_init,
        )
        return (self._exit_order(fill.side),)

    # -- blocks -----------------------------------------------------------------------

    def _detect_fvgs(self, bar: Bar, bar_index: int, min_size: float) -> None:
        high2 = self._history.high(2)
        low2 = self._history.low(2)
        # Bullish FVG: gap between high[2] (bottom) and the current low (top); na -> false.
        if high2 is not None and bar.low > high2 and (bar.low - high2) >= min_size:
            self.fvgs.append(Fvg(bar.low, high2, BULLISH, FRESH, bar_index, -1))
        # Bearish FVG: gap between the current high (bottom) and low[2] (top).
        if low2 is not None and bar.high < low2 and (low2 - bar.high) >= min_size:
            self.fvgs.append(Fvg(low2, bar.high, BEARISH, FRESH, bar_index, -1))
        # One shift per bar, as in Pine (both gap kinds cannot form on the same bar).
        if len(self.fvgs) > FVG_CAP:
            del self.fvgs[0]

    def _enter(
        self,
        side: Side,
        fvg: Fvg,
        bar: Bar,
        ctx: StrategyContext,
        atr_init: float | None,
        buffer: float,
        risk_base_amt: float,
    ) -> list[Action]:
        p = self.params
        mintick = ctx.instrument.tick_size
        close = bar.close
        # Estimate the fill at the signal-bar close; the stop at least one tick beyond it.
        # An na ATR (too few bars yet) falls back to the FVG level, as in Pine.
        atr_dist = (
            atr_init * p.atr_mult_init
            if p.stop_method == "ATR Based" and atr_init is not None
            else None
        )
        if side is Side.LONG:
            fvg_stop = fvg.bot - buffer
            est_stop = fvg_stop if atr_dist is None else close - atr_dist
            est_stop = min(est_stop, close - mintick)
        else:
            fvg_stop = fvg.top + buffer
            est_stop = fvg_stop if atr_dist is None else close + atr_dist
            est_stop = max(est_stop, close + mintick)
        qty = self._round_qty(_f_qty(close, est_stop, p.risk_pct, risk_base_amt, mintick, ctx), ctx)
        if qty <= 0:
            return []  # the script skips the trade and keeps scanning on later bars
        if side is Side.LONG:
            est_tp = close + p.rr * (close - est_stop)
            entry_id, exit_id = ENTRY_LONG, EXIT_LONG
            self.in_atr_dist = close - est_stop
        else:
            est_tp = close - p.rr * (est_stop - close)
            entry_id, exit_id = ENTRY_SHORT, EXIT_SHORT
            self.in_atr_dist = est_stop - close
        self.in_dir = side.sign
        self.in_fvg_stop = fvg_stop  # captured now; finalized on the fill bar
        self.in_initial_stop = None
        self.in_trail_stop = None
        self.in_take_profit = None
        self.traded_today = True
        # Bracket in the SAME bar as the entry so the fill bar is protected.
        return [
            Entry(entry_id, side, qty, comment=entry_id),
            Exit(exit_id, from_entry=entry_id, stop=est_stop, limit=est_tp),
        ]

    def _round_qty(self, qty: float, ctx: StrategyContext) -> float:
        """Pine f_roundQty, then the instrument's own step (always DOWN; 0 = skip)."""
        rounding = self.params.qty_round
        whole = rounding == "Whole units" or (
            rounding == "Auto" and ctx.instrument.kind is InstrumentKind.FUTURES
        )
        if whole:
            qty = math.floor(qty)
        return ctx.instrument.floor_qty(qty)

    def _finalize_bracket(self, side: Side, avg: float, mintick: float, *, atr_ready: bool) -> None:
        """Pine `if na(inTakeProfit)`: the initial stop and the rr target from the real fill.

        FVG Buffer re-uses the structural level captured at the signal; ATR Based re-applies
        the signal-bar distance to the fill (SPEC §3.1). The stop stays >= 1 tick on the
        loss side of the fill.
        """
        use_atr = self.params.stop_method == "ATR Based" and atr_ready
        atr_dist = self.in_atr_dist
        fvg_stop = self.in_fvg_stop
        if atr_dist is None or fvg_stop is None:
            raise RuntimeError("bracket finalized without a captured signal")
        rr = self.params.rr
        if side is Side.LONG:
            raw_stop = avg - atr_dist if use_atr else fvg_stop
            initial = min(raw_stop, avg - mintick)
            take_profit = avg + rr * (avg - initial)
        else:
            raw_stop = avg + atr_dist if use_atr else fvg_stop
            initial = max(raw_stop, avg + mintick)
            take_profit = avg - rr * (initial - avg)
        self.in_initial_stop = initial
        self.in_trail_stop = initial  # seed the trail
        self.in_take_profit = take_profit

    def _manage(
        self,
        side: Side,
        avg: float,
        bar: Bar,
        mintick: float,
        buffer: float,
        atr_t: float | None,
        swing: float | None,
    ) -> Exit:
        p = self.params
        trail_min_step = p.trail_min_step_ticks * mintick
        if self.in_take_profit is None:
            # tv_ohlc: the fill bar's close (Pine's own timing).
            self._finalize_bracket(side, avg, mintick, atr_ready=self._atr_init.value is not None)
        initial = self.in_initial_stop
        trail = self.in_trail_stop
        if initial is None or trail is None:
            raise RuntimeError("management without a finalized bracket")
        close = bar.close
        long = side is Side.LONG
        risk = max(avg - initial, mintick) if long else max(initial - avg, mintick)
        if p.use_trail:
            sign = 1.0 if long else -1.0
            cand: float | None
            if p.trail_mode == "Swing (1m structure)":
                cand = None if swing is None else swing - sign * buffer
            elif p.trail_mode == "ATR step":
                cand = None if atr_t is None else close - sign * atr_t * p.atr_mult_t
            else:
                cand = close * (1 - sign * p.trail_pct / 100)
            # Only toward profit, and only in steps of at least trailMinStepTicks; an na
            # candidate compares false in Pine. Profit is measured from the real fill.
            if cand is not None:
                profit = (close - avg) if long else (avg - close)
                improvement = (cand - trail) if long else (trail - cand)
                if profit >= p.trail_act_r * risk and improvement >= trail_min_step:
                    self.in_trail_stop = cand
        return self._exit_order(side)

    def _exit_order(self, side: Side) -> Exit:
        if side is Side.LONG:
            return Exit(
                EXIT_LONG, from_entry=ENTRY_LONG, stop=self.in_trail_stop, limit=self.in_take_profit
            )
        return Exit(
            EXIT_SHORT, from_entry=ENTRY_SHORT, stop=self.in_trail_stop, limit=self.in_take_profit
        )

    def _reset_management(self) -> None:
        self.in_dir = 0
        self.in_fvg_stop = None
        self.in_initial_stop = None
        self.in_take_profit = None
        self.in_trail_stop = None
        self.in_atr_dist = None


def _f_qty(
    est_entry: float,
    est_stop: float,
    risk: float,
    base_amt: float,
    mintick: float,
    ctx: StrategyContext,
) -> float:
    """Pine f_qty: the size at which the initial stop loses `risk` % of the risk base."""
    risk_amt = base_amt * (risk / 100.0)
    # The one-tick floor prevents a size blow-up on a degenerate stop.
    dist = max(abs(est_entry - est_stop), mintick)
    per_unit = dist * ctx.instrument.point_value
    return risk_amt / per_unit if per_unit > 0 else 0.0
