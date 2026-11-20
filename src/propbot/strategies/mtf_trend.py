"""MTF Trend Continuation: port of `pine/mtf_trend_continuation_strategy.pine` (SPEC §3.4.4).

5-minute execution with a 1-hour trend filter: the last CLOSED hourly bar's EMA(20) vs
EMA(50) gives the bias, a pullback into the band between the 5-minute EMA(21) and EMA(50)
on drying volume "arms" the setup, and a momentum candle beyond the prior bar's extreme
triggers a market entry. Even sizes scale out at TP1 (limit) and trail the runner; odd
sizes (Plan B) keep the whole position and only move the stop once TP1 is touched.

The Pine script is the reference: `on_bar` runs its blocks in the script's order and the
state variables keep the script's names in snake_case (Pine `var` -> attributes, `na` ->
None). Plots, tables, the OOS shading and the alert() JSON are not ported; the table's
statistics come from `propbot.backtest.analytics`.

Fill-bar re-pricing (SPEC §3.1, ADR-0008): the script finalizes stop and TP1 from
`strategy.position_avg_price` on the fill bar's close. `_finalize` holds that arithmetic;
`on_fill` calls it at the fill instant (pessimistic model) and `on_bar` calls it when a
position is open but not finalized yet (tv_ohlc model, the script's own timing).
"""

import datetime as dt
import math
from collections.abc import Sequence
from typing import Final, Literal
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from pydantic import Field, field_validator

from propbot.core.bars import Bar
from propbot.core.session import LocalClock
from propbot.risk.config import StrictModel
from propbot.strategies.base import (
    Action,
    Cancel,
    CloseAll,
    Entry,
    Exit,
    FillEvent,
    Side,
    StrategyContext,
)
from propbot.strategies.history import BarHistory
from propbot.strategies.indicators import Atr, Ema, Highest, HtfEma, Lowest, Sma

STRATEGY_ID: Final = "mtf_trend_cont"
PINE_FILE: Final = "mtf_trend_continuation_strategy.pine"

# strategy(..., pyramiding = 0, ...)
PYRAMIDING: Final = 0

# Exit ids and comments exactly as the script's strategy.exit / close_all calls.
_LONG: Final = "Long"
_SHORT: Final = "Short"
_TP1: Final = "TP1"
_INIT: Final = "Init"
_RUN: Final = "Run"
_RUN_B: Final = "RunB"
_STOP: Final = "Stop"
_INIT_COMMENT: Final = "Init stop"
_RUN_COMMENT: Final = "Runner"
_RUN_B_COMMENT: Final = "Runner (Plan B)"
_DAILY_LOSS_HALT: Final = "Daily loss halt"
_SESSION_END: Final = "Session end"
_END_OF_RANGE: Final = "End of test range"

# `abs(strategy.position_size) < entryQty * 0.95`: the TP1 partial has filled.
_TP1_FILLED_FRACTION: Final = 0.95

_MS_PER_SECOND: Final = 1000
_SECONDS_PER_MINUTE: Final = 60
_EPOCH: Final = dt.datetime(1970, 1, 1, tzinfo=dt.UTC)

type RiskBase = Literal["Initial Capital", "Account Equity", "Fixed Balance"]
type TrailType = Literal["Chandelier Exit", "ATR Trailing"]


class MtfTrendParams(StrictModel):
    """The script's inputs that affect orders, with its defaults and minval / maxval / options.

    Not ported: `oosStart` (only the table and the shading use it), `showDateViz`,
    `showVA`, `showHtf`, `showLevels`, `showPerf`, `tableScope`, `tablePos`, `tableSize`,
    `alertToken`.
    """

    tz: str = "America/New_York"
    # input.timeframe: only minute timeframes ("60", "240") can be bucketed from the chart
    # bars; "D" / "W" are refused rather than guessed.
    htf_tf: str = Field(default="60", pattern=r"^[1-9][0-9]*$")
    htf_fast_len: int = Field(default=20, ge=1)
    htf_slow_len: int = Field(default=50, ge=1)
    ltf_fast_len: int = Field(default=21, ge=1)
    ltf_slow_len: int = Field(default=50, ge=1)
    use_vol_filter: bool = True
    vol_len: int = Field(default=20, ge=1)
    allow_long: bool = True
    allow_short: bool = True
    arm_validity_bars: int = Field(default=10, ge=1)
    risk_percent: float = Field(default=0.5, ge=0.01)
    risk_base: RiskBase = "Initial Capital"
    fixed_bal: float = Field(default=10000.0, ge=1)
    swing_len: int = Field(default=10, ge=1)
    swing_buf_tick: int = Field(default=4, ge=0)
    round_units: bool = True
    tp1_rr: float = Field(default=1.5, ge=0.1)
    scale_out_pct: float = Field(default=50.0, ge=1, le=99)
    trail_type: TrailType = "Chandelier Exit"
    ch_len: int = Field(default=22, ge=1)
    ch_mult: float = Field(default=3.0, ge=0.1)
    atr_trail_len: int = Field(default=14, ge=1)
    atr_trail_mult: float = Field(default=2.5, ge=0.1)
    trail_min_step_ticks: int = Field(default=4, ge=1)
    max_trades_day: int = Field(default=1, ge=1)
    use_daily_loss: bool = True
    daily_loss_pct: float = Field(default=2.0, ge=0.1)
    flat_on_daily_loss: bool = True
    use_consec: bool = False
    max_consec: int = Field(default=3, ge=1)
    use_session: bool = True
    sess_start_h: int = Field(default=9, ge=0, le=23)
    sess_start_m: int = Field(default=30, ge=0, le=59)
    sess_end_h: int = Field(default=15, ge=0, le=23)
    sess_end_m: int = Field(default=55, ge=0, le=59)
    flat_at_end: bool = True
    start_date: dt.datetime = dt.datetime(2024, 1, 1, tzinfo=dt.UTC)
    end_date: dt.datetime = dt.datetime(2030, 1, 1, tzinfo=dt.UTC)

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
    def _aware_utc(cls, value: dt.datetime) -> dt.datetime:
        # Pine input.time is an absolute instant; a naive datetime would be read in
        # whatever zone the machine has.
        if value.tzinfo is None or value.utcoffset() is None:
            raise ValueError("tarih saat dilimi bilgisi taşımalı (ör. UTC)")
        return value.astimezone(dt.UTC)

    @property
    def htf_seconds(self) -> int:
        return int(self.htf_tf) * _SECONDS_PER_MINUTE


def _epoch_ms(moment: dt.datetime) -> int:
    return (moment - _EPOCH) // dt.timedelta(milliseconds=1)


class MtfTrend:
    """The script as a bar-based state machine (`propbot.strategies.base.Strategy`)."""

    def __init__(self, params: MtfTrendParams | None = None) -> None:
        p = params if params is not None else MtfTrendParams()
        self._p: Final = p
        self._clock: Final = LocalClock(p.tz)
        self._s_start: Final = p.sess_start_h * 60 + p.sess_start_m
        self._s_end: Final = p.sess_end_h * 60 + p.sess_end_m
        self._start_ms: Final = _epoch_ms(p.start_date)
        self._end_ms: Final = _epoch_ms(p.end_date)

        # ---- indicators (every ta.* runs on every bar, as at the script's global scope)
        self._htf_fast: Final = HtfEma(p.htf_seconds, p.htf_fast_len)
        self._htf_slow: Final = HtfEma(p.htf_seconds, p.htf_slow_len)
        self._ema_fast: Final = Ema(p.ltf_fast_len)
        self._ema_slow: Final = Ema(p.ltf_slow_len)
        self._vol_avg: Final = Sma(p.vol_len)
        self._swing_low: Final = Lowest(p.swing_len)
        self._swing_high: Final = Highest(p.swing_len)
        self._atr_ch: Final = Atr(p.ch_len)
        self._atr_tr: Final = Atr(p.atr_trail_len)
        self._ch_highest: Final = Highest(p.ch_len)
        self._ch_lowest: Final = Lowest(p.ch_len)
        self._history: Final = BarHistory(2)  # high[1], low[1]
        self._prev_day: int | None = None  # dayofmonth(time, tz)[1]
        self._tf_checked = False

        # ---- arm (pullback) state
        self._arm_dir = 0  # 0 none, 1 long-armed, -1 short-armed
        self._arm_bar = 0

        # ---- open-trade state
        self._pending_dir = 0
        self._pending_stop: float | None = None
        self._pending_qty: float | None = None
        self._pending_bar: int | None = None
        self._pending_no_scale = False
        self._active_init_stop: float | None = None
        self._active_tp1: float | None = None
        self._runner_stop: float | None = None
        self._entry_qty: float | None = None
        self._tp1_filled = False
        self._no_scale_trade = False

        # ---- daily / streak state
        self._trades_today = 0
        self._total_entries = 0
        self._consec_losses = 0
        self._daily_loss_hit = False
        self._consec_halt_hit = False
        self._day_start_equity: float | None = None
        self._eq_at_entry: float | None = None
        self._closed_seen = 0

    # -- Strategy protocol -------------------------------------------------------------

    @property
    def strategy_id(self) -> str:
        return STRATEGY_ID

    @property
    def pyramiding(self) -> int:
        return PYRAMIDING

    @property
    def params(self) -> MtfTrendParams:
        return self._p

    @property
    def active_init_stop(self) -> float | None:
        """Pine `activeInitStop`: the initial stop finalized from the real fill (observability)."""
        return self._active_init_stop

    @property
    def total_entries(self) -> int:
        """Pine `totalEntries`: entry orders submitted so far."""
        return self._total_entries

    def on_fill(self, fill: FillEvent, ctx: StrategyContext) -> Sequence[Action]:
        avg = ctx.position_avg_price if ctx.position_avg_price is not None else fill.price
        self._finalize(fill.side, avg, ctx.instrument.tick_size)
        return self._bracket(fill.side)

    def on_bar(self, bar: Bar, ctx: StrategyContext) -> Sequence[Action]:
        p = self._p
        if not self._tf_checked:
            if p.htf_seconds < ctx.tf_seconds:
                raise ValueError(
                    f"üst zaman dilimi ({p.htf_tf} dk) grafik zaman diliminden "
                    f"({ctx.tf_seconds // _SECONDS_PER_MINUTE} dk) küçük olamaz"
                )
            self._tf_checked = True
        tick = ctx.instrument.tick_size
        close = bar.close
        self._history.push(bar)

        # ---- MTF data: request.security(..., [ema[1], ema[1]], lookahead_on)
        htf_fast = self._htf_fast.update(bar.ts_open_ms, close)
        htf_slow = self._htf_slow.update(bar.ts_open_ms, close)
        htf_bull = htf_fast is not None and htf_slow is not None and htf_fast > htf_slow
        htf_bear = htf_fast is not None and htf_slow is not None and htf_fast < htf_slow

        # ---- time / date / day
        parts = self._clock.parts(bar.ts_open_ms)
        cur_min = parts.minute_of_day
        session_ok = not p.use_session or (self._s_start <= cur_min < self._s_end)
        bar_min = max(1, ctx.tf_seconds // _SECONDS_PER_MINUTE)
        entry_time_ok = not ctx.session_last_bar and (
            not p.use_session or cur_min + bar_min < self._s_end
        )
        # newDay = ta.change(dayofmonth(time, tz)) != 0: on the first bar the change is na
        # and `na != 0` is false, so the first bar is not a new day.
        new_day = self._prev_day is not None and parts.day != self._prev_day
        self._prev_day = parts.day
        in_date_range = self._start_ms <= bar.ts_open_ms <= self._end_ms

        # ---- LTF indicators
        ema_fast = self._ema_fast.update(close)
        ema_slow = self._ema_slow.update(close)
        in_va = False
        if ema_fast is not None and ema_slow is not None:
            va_top = max(ema_fast, ema_slow)
            va_bot = min(ema_fast, ema_slow)
            in_va = bar.low <= va_top and bar.high >= va_bot
        vol_avg = self._vol_avg.update(bar.volume)
        vol_ok = (vol_avg is not None and bar.volume < vol_avg) if p.use_vol_filter else True

        swing_low = self._swing_low.update(bar.low)
        swing_high = self._swing_high.update(bar.high)
        buf = p.swing_buf_tick * tick
        trail_min_step = p.trail_min_step_ticks * tick

        atr_ch = self._atr_ch.update(bar.high, bar.low, close)
        atr_tr = self._atr_tr.update(bar.high, bar.low, close)
        ch_highest = self._ch_highest.update(bar.high)
        ch_lowest = self._ch_lowest.update(bar.low)
        trail_long: float | None
        trail_short: float | None
        if p.trail_type == "Chandelier Exit":
            trail_long = (
                None if ch_highest is None or atr_ch is None else ch_highest - atr_ch * p.ch_mult
            )
            trail_short = (
                None if ch_lowest is None or atr_ch is None else ch_lowest + atr_ch * p.ch_mult
            )
        else:
            trail_long = None if atr_tr is None else close - atr_tr * p.atr_trail_mult
            trail_short = None if atr_tr is None else close + atr_tr * p.atr_trail_mult

        # ---- arm (pullback) + momentum trigger
        if new_day:
            self._arm_dir = 0  # no overnight setup into a new session
        long_pullback = htf_bull and in_va and vol_ok and p.allow_long
        short_pullback = htf_bear and in_va and vol_ok and p.allow_short
        if long_pullback:
            self._arm_dir = 1
            self._arm_bar = ctx.bar_index
        elif short_pullback:
            self._arm_dir = -1
            self._arm_bar = ctx.bar_index
        stale = ctx.bar_index - self._arm_bar > p.arm_validity_bars
        if self._arm_dir == 1 and (not htf_bull or stale):
            self._arm_dir = 0
        if self._arm_dir == -1 and (not htf_bear or stale):
            self._arm_dir = 0

        high_1 = self._history.high(1)
        low_1 = self._history.low(1)
        bull_trigger = close > bar.open and high_1 is not None and close > high_1
        bear_trigger = close < bar.open and low_1 is not None and close < low_1
        long_signal = self._arm_dir == 1 and bull_trigger
        short_signal = self._arm_dir == -1 and bear_trigger

        # ---- new-day reset
        if new_day or self._day_start_equity is None:
            self._day_start_equity = ctx.equity
            self._trades_today = 0
            self._daily_loss_hit = False
            self._consec_halt_hit = False
            self._consec_losses = 0

        # ---- trade-outcome tracking (count-based: also a trade closed in its fill bar)
        flat = ctx.position_size == 0.0
        closed_count = len(ctx.closed_trades)
        if flat and closed_count > self._closed_seen:
            self._closed_seen = closed_count
            if self._eq_at_entry is not None:
                trade_net = ctx.equity - self._eq_at_entry
                if trade_net > 0:
                    self._consec_losses = 0
                else:
                    self._consec_losses += 1
                if p.use_consec and self._consec_losses >= p.max_consec:
                    self._consec_halt_hit = True
                self._eq_at_entry = None

        actions: list[Action] = []

        # ---- reset state once the previous trade is over (BEFORE new entries)
        pending_over = (
            self._pending_dir != 0
            and self._pending_bar is not None
            and ctx.bar_index > self._pending_bar
        )
        if flat and (self._active_tp1 is not None or pending_over):
            self._pending_dir = 0
            self._pending_stop = None
            self._pending_qty = None
            self._active_init_stop = None
            self._active_tp1 = None
            self._runner_stop = None
            self._entry_qty = None
            self._tp1_filled = False
            self._no_scale_trade = False
            actions.extend(Cancel(order_id) for order_id in (_TP1, _INIT, _RUN, _RUN_B, _STOP))

        # ---- daily-loss circuit breaker (bar-close equity, incl. open P&L)
        day_start = self._day_start_equity
        if (
            p.use_daily_loss
            and day_start is not None
            and (day_start - ctx.equity) >= day_start * p.daily_loss_pct / 100.0
        ):
            self._daily_loss_hit = True

        halted_today = self._daily_loss_hit or (p.use_consec and self._consec_halt_hit)
        can_enter_base = (
            in_date_range
            and session_ok
            and entry_time_ok
            and not halted_today
            and self._trades_today < p.max_trades_day
            and flat
        )

        if p.risk_base == "Fixed Balance":
            risk_base_amt = p.fixed_bal
        elif p.risk_base == "Account Equity":
            risk_base_amt = ctx.equity
        else:
            risk_base_amt = ctx.initial_capital

        # ---- entries (fixed-% sizing). A swing extreme still na makes stopDist na and
        # `stopDist > 0` false: no entry.
        if long_signal and can_enter_base and swing_low is not None:
            stop_lvl = min(swing_low - buf, close - tick)  # stop sits below price
            self._enter(Side.LONG, close, stop_lvl, close - stop_lvl, risk_base_amt, ctx, actions)
        if short_signal and can_enter_base and swing_high is not None:
            stop_lvl = max(swing_high + buf, close + tick)
            self._enter(Side.SHORT, close, stop_lvl, stop_lvl - close, risk_base_amt, ctx, actions)

        # ---- finalize stop / TP on the fill bar, then manage exits
        if ctx.position_size != 0.0:
            side = Side.LONG if ctx.position_size > 0 else Side.SHORT
            avg = ctx.position_avg_price
            assert avg is not None  # an open position always has an average price
            if self._active_tp1 is None:  # first bar of the position (tv_ohlc timing)
                self._finalize(side, avg, tick)
            trail = trail_long if side is Side.LONG else trail_short
            actions.extend(self._manage(side, bar, ctx, avg, trail, trail_min_step))

        # ---- forced exits: one reason per bar, filled at this bar's close
        if p.use_daily_loss and p.flat_on_daily_loss and self._daily_loss_hit:
            flat_reason = _DAILY_LOSS_HALT
        elif p.flat_at_end and ((p.use_session and cur_min >= self._s_end) or ctx.session_last_bar):
            flat_reason = _SESSION_END
        elif bar.ts_open_ms > self._end_ms:
            flat_reason = _END_OF_RANGE
        else:
            flat_reason = ""
        if flat_reason and ctx.position_size != 0.0:
            actions.append(CloseAll(comment=flat_reason))
        return actions

    # -- the script's blocks -----------------------------------------------------------

    def _enter(
        self,
        side: Side,
        close: float,
        stop_lvl: float,
        stop_dist: float,
        risk_base_amt: float,
        ctx: StrategyContext,
        actions: list[Action],
    ) -> None:
        """The `if longSignal and canEnterBase` / `if shortSignal ...` block."""
        p = self._p
        if not stop_dist > 0:
            return
        risk_cash = risk_base_amt * p.risk_percent / 100.0
        raw_qty = risk_cash / (stop_dist * ctx.instrument.point_value)
        qty = math.floor(raw_qty) if p.round_units else raw_qty
        # Never above the script's size: down to the instrument's step (SPEC §3.1).
        qty = ctx.instrument.floor_qty(qty)
        if not qty > 0:
            return  # skipped: the arm stays as it is, exactly as in the script
        entry_id = _LONG if side is Side.LONG else _SHORT
        if side is Side.LONG:
            est_tp1 = close + p.tp1_rr * stop_dist
        else:
            est_tp1 = close - p.tp1_rr * stop_dist
        actions.append(Entry(entry_id, side, qty, comment=entry_id))
        # Bracket in the same bar as the entry, so the fill bar is protected; the fill-bar
        # block re-prices it from the real fill (same ids -> modified in place).
        self._pending_no_scale = p.round_units and _is_odd(qty)
        if self._pending_no_scale:
            actions.append(Exit(_STOP, entry_id, stop=stop_lvl, comment=_INIT_COMMENT))
        else:
            actions.append(
                Exit(
                    _TP1,
                    entry_id,
                    stop=stop_lvl,
                    limit=est_tp1,
                    qty_percent=p.scale_out_pct,
                    comment=_TP1,
                )
            )
            actions.append(
                Exit(
                    _INIT,
                    entry_id,
                    stop=stop_lvl,
                    qty_percent=100 - p.scale_out_pct,
                    comment=_INIT_COMMENT,
                )
            )
        self._pending_dir = side.sign
        self._pending_stop = stop_lvl
        self._pending_qty = qty
        self._pending_bar = ctx.bar_index
        self._eq_at_entry = ctx.equity
        self._trades_today += 1
        self._total_entries += 1
        self._arm_dir = 0  # a fresh pullback is required for the next trade

    def _finalize(self, side: Side, avg: float, tick: float) -> None:
        """The `if na(activeTP1)` block: initial stop and TP1 from the real average price.

        The structural stop is kept, but pulled at least one tick to the loss side of the
        fill (a gap through it would otherwise leave no risk distance).
        """
        p = self._p
        pending_stop = self._pending_stop
        assert pending_stop is not None, "a fill without a submitted entry"
        if side is Side.LONG:
            init_stop = min(pending_stop, avg - tick)
            risk_pts = avg - init_stop
            self._active_tp1 = avg + p.tp1_rr * risk_pts
        else:
            init_stop = max(pending_stop, avg + tick)
            risk_pts = init_stop - avg
            self._active_tp1 = avg - p.tp1_rr * risk_pts
        self._active_init_stop = init_stop
        self._runner_stop = init_stop
        # The submitted qty, not the current position: TP1 may already have filled.
        self._entry_qty = self._pending_qty
        # Plan B: an odd / single-contract size cannot be split 50/50 into whole contracts.
        self._no_scale_trade = (
            p.round_units and self._entry_qty is not None and _is_odd(self._entry_qty)
        )
        self._tp1_filled = False

    def _bracket(self, side: Side) -> list[Action]:
        """The exits re-issued every bar while TP1 has not filled (or 1.5R not reached)."""
        p = self._p
        entry_id = _LONG if side is Side.LONG else _SHORT
        stop = self._active_init_stop
        if self._no_scale_trade:
            return [Exit(_STOP, entry_id, stop=stop, comment=_INIT_COMMENT)]
        return [
            Exit(
                _TP1,
                entry_id,
                stop=stop,
                limit=self._active_tp1,
                qty_percent=p.scale_out_pct,
                comment=_TP1,
            ),
            Exit(
                _INIT, entry_id, stop=stop, qty_percent=100 - p.scale_out_pct, comment=_INIT_COMMENT
            ),
        ]

    def _manage(
        self,
        side: Side,
        bar: Bar,
        ctx: StrategyContext,
        avg: float,
        trail: float | None,
        trail_min_step: float,
    ) -> list[Action]:
        """The LONG / SHORT management blocks after the fill-bar finalization."""
        long = side is Side.LONG
        entry_id = _LONG if long else _SHORT
        active_tp1 = self._active_tp1
        assert active_tp1 is not None
        if self._no_scale_trade:
            # Plan B: the whole position runs; 1.5R is a touch of the bar's extreme.
            if not self._tp1_filled and (bar.high >= active_tp1 if long else bar.low <= active_tp1):
                self._tp1_filled = True
        elif (
            not self._tp1_filled
            and self._entry_qty is not None
            and abs(ctx.position_size) < self._entry_qty * _TP1_FILLED_FRACTION
        ):
            self._tp1_filled = True  # the TP1 partial filled
        if self._tp1_filled:
            self._trail(long, avg, trail, trail_min_step)
        if not self._tp1_filled:
            return self._bracket(side)
        runner = self._runner_stop
        if self._no_scale_trade:
            return [Cancel(_STOP), Exit(_RUN_B, entry_id, stop=runner, comment=_RUN_B_COMMENT)]
        return [Cancel(_INIT), Exit(_RUN, entry_id, stop=runner, comment=_RUN_COMMENT)]

    def _trail(self, long: bool, be: float, trail: float | None, trail_min_step: float) -> None:
        """First move straight to breakeven (or the trail, if better); afterwards only in
        steps of at least `trailMinStepTicks`, and only toward profit."""
        runner = self._runner_stop
        assert runner is not None
        # Pine: math.max(be, na) is na, which would leave the runner without a stop while
        # the trail is still warming up (fewer than chLen / atrTrailLen bars). The engine
        # refuses an unprotected position, so a missing trail counts as breakeven only.
        if long:
            cand = be if trail is None else max(be, trail)
            # Pine: `if runnerStop < be` (first move) `else if cand - runnerStop >= trailMinStep`
            if runner < be or cand - runner >= trail_min_step:
                self._runner_stop = cand
        else:
            cand = be if trail is None else min(be, trail)
            if runner > be or runner - cand >= trail_min_step:
                self._runner_stop = cand


def _is_odd(qty: float) -> bool:
    """`int(math.round(qty)) % 2 == 1`; Pine rounds half away from zero."""
    return math.floor(qty + 0.5) % 2 == 1
