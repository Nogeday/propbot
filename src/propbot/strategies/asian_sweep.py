"""Asian Range Liquidity Sweep & Reversal: the port of `pine/asian_sweep_reversal_strategy.pine`.

SPEC §3.3. The Asian window (default 20:00-00:00 New York, may wrap midnight) builds a
High/Low range; its first bar starts a new cycle (range, sweeps, arming and the daily trade
right reset). Inside the active window (default 03:00-11:00) a sweep of the High followed
within `max_bars` by a bearish FVG that closes back below the High arms a short (the Low
mirrored arms a long); a later bar that touches the FVG enters at the next bar's open with
a fixed stop (FVG buffer or ATR) and a fixed `rr` target. The open trade is closed when the
active window ends, on the session's last bar, on a daily-loss breach and after the end date.

The bar-by-bar order of the Pine script is kept block by block in `on_bar`; the state
variables carry the snake_case names of the Pine `var`s. Visual-only state (lines, boxes,
labels, the stats arrays, `sweepHiLvl` / `sweepLoLvl`, `asianStartIdx`) is not ported.
"""

import datetime as dt
import math
from collections.abc import Sequence
from typing import Final, Literal
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from pydantic import Field, field_validator

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
from propbot.strategies.indicators import Atr

STRATEGY_ID: Final = "asian_sweep"
PINE_FILE: Final = "asian_sweep_reversal_strategy.pine"
# strategy(pyramiding = 0)
PYRAMIDING: Final = 0

# Order ids and comments exactly as the Pine calls name them.
LONG_ID: Final = "Long"
SHORT_ID: Final = "Short"
EXIT_LONG_ID: Final = "Exit L"
EXIT_SHORT_ID: Final = "Exit S"
REASON_SESSION_END: Final = "Session end"
REASON_DAILY_DD: Final = "Daily DD halt"
REASON_END_OF_RANGE: Final = "End of test range"

_MINUTES_PER_DAY: Final = 1440
_MS_PER_SECOND: Final = 1000
# high[2] / low[2] is the deepest look-back of the script.
_HISTORY_BARS: Final = 3

type RiskBase = Literal["Initial Capital", "Account Equity", "Fixed Balance"]
type StopMethod = Literal["FVG Buffer", "ATR Based"]
type QtyRounding = Literal["Auto", "Whole units", "Fractional"]


class AsianSweepParams(StrictModel):
    """One field per order-relevant Pine input; defaults are the script's defaults."""

    # Sessions
    tz: str = "America/New_York"
    asian_start_hour: int = Field(default=20, ge=0, le=23)
    asian_start_min: int = Field(default=0, ge=0, le=59)
    asian_end_hour: int = Field(default=0, ge=0, le=23)
    asian_end_min: int = Field(default=0, ge=0, le=59)
    active_start_hour: int = Field(default=3, ge=0, le=23)
    active_start_min: int = Field(default=0, ge=0, le=59)
    active_end_hour: int = Field(default=11, ge=0, le=23)
    active_end_min: int = Field(default=0, ge=0, le=59)
    close_at_end: bool = True
    # Backtest date range (oosStart only drives visuals and the stats table: not ported)
    start_date: dt.datetime = dt.datetime(2024, 1, 1, tzinfo=dt.UTC)
    end_date: dt.datetime = dt.datetime(2030, 1, 1, tzinfo=dt.UTC)
    # Day of week filter (applies to the active-window bar's own day)
    trade_mon: bool = True
    trade_tue: bool = True
    trade_wed: bool = True
    trade_thu: bool = True
    trade_fri: bool = True
    # Sweep / entry
    max_bars: int = Field(default=15, ge=1)
    min_fvg_ticks: int = Field(default=0, ge=0)
    allow_long: bool = True
    allow_short: bool = True
    # Risk
    risk_pct: float = Field(default=0.5, ge=0.1)
    risk_base: RiskBase = "Initial Capital"
    fixed_bal: float = Field(default=10000.0, ge=1)
    rr: float = Field(default=2.0, ge=0.1)
    stop_method: StopMethod = "FVG Buffer"
    sl_buf: int = Field(default=2, ge=0)
    atr_len_init: int = Field(default=14, ge=1)
    atr_mult_init: float = Field(default=1.5, ge=0.1)
    qty_round: QtyRounding = "Auto"
    # Circuit breakers
    use_breakers: bool = True
    daily_dd_pct: float = Field(default=2.0, ge=0.1)
    flatten_breach: bool = True
    max_consec_loss: int = Field(default=2, ge=1)

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
        if value.tzinfo is None or value.utcoffset() is None:
            raise ValueError("tarih saat dilimi içermeli (UTC)")
        return value.astimezone(dt.UTC)


def _in_window(minute: int, start: int, end: int) -> bool:
    """Pine `asianNow` / `activeNow`: [start, end) in minutes of the day, may wrap midnight."""
    if end > start:
        return start <= minute < end
    return minute >= start or minute < end


def _ms(moment: dt.datetime) -> int:
    return int(moment.timestamp() * _MS_PER_SECOND)


class AsianSweep:
    """`propbot.strategies.base.Strategy` for the Asian sweep script. One instance per run."""

    def __init__(self, params: AsianSweepParams | None = None) -> None:
        p = params or AsianSweepParams()
        self.params: Final = p
        self._clock = LocalClock(p.tz)
        self._history = BarHistory(_HISTORY_BARS)
        self._atr = Atr(p.atr_len_init)
        self._bar_count = 0  # bars seen so far; decides whether ta.atr is still na
        self._asian_start_m = p.asian_start_hour * 60 + p.asian_start_min
        self._asian_end_m = p.asian_end_hour * 60 + p.asian_end_min
        self._active_start_m = p.active_start_hour * 60 + p.active_start_min
        self._active_end_m = p.active_end_hour * 60 + p.active_end_min
        self._start_ms = _ms(p.start_date)
        self._end_ms = _ms(p.end_date)
        # Pine dayofweek.monday .. friday by Python weekday (Monday = 0); weekend: never.
        self._day_ok = (p.trade_mon, p.trade_tue, p.trade_wed, p.trade_thu, p.trade_fri)

        # -- Pine `var` state --
        self.asian_high: float | None = None
        self.asian_low: float | None = None
        self.asian_ready = False
        self.traded_today = False
        self.sweep_hi_bar = -1
        self.sweep_lo_bar = -1
        self.arm_dir = 0  # 0 none, 1 long-armed, -1 short-armed
        self.arm_top: float | None = None
        self.arm_bot: float | None = None
        self.arm_bar: int | None = None
        self.in_dir = 0
        self.in_fvg_stop: float | None = None
        self.in_initial_stop: float | None = None
        self.in_take_profit: float | None = None
        self.in_atr_dist: float | None = None
        self.day_start_equity: float | None = None
        self.day_start_net: float | None = None
        self.consec_loss_today = 0
        self.last_closed_count = 0
        self.halted_dd = False
        self.halted_streak = False

        # -- previous-bar values of series the script reads with [1] --
        # On the first bar Pine's `asianNow[1]` is na; an na bool counts as false, so a
        # chart that opens inside a window starts a cycle there (VERIFY in TradingView).
        self._asian_now_prev = False
        self._active_now_prev = False
        self._closed_count_prev: int | None = None  # strategy.closedtrades[1]; na on bar 0

    # -- Strategy protocol ---------------------------------------------------------------

    @property
    def strategy_id(self) -> str:
        return STRATEGY_ID

    @property
    def pyramiding(self) -> int:
        return PYRAMIDING

    def on_bar(self, bar: Bar, ctx: StrategyContext) -> Sequence[Action]:
        p = self.params
        h = self._history
        h.push(bar)
        self._bar_count += 1
        bar_index = ctx.bar_index
        tick = ctx.instrument.tick_size
        actions: list[Action] = []

        # ── TIME / DATE / DAY ──
        parts = self._clock.parts(bar.ts_open_ms)
        cur_min = parts.minute_of_day
        asian_now = _in_window(cur_min, self._asian_start_m, self._asian_end_m)
        active_now = _in_window(cur_min, self._active_start_m, self._active_end_m)
        asian_start_bar = asian_now and not self._asian_now_prev
        asian_end_bar = not asian_now and self._asian_now_prev
        active_first = active_now and not self._active_now_prev
        # Entry-time gate: the market entry fills at the NEXT bar's open (SPEC §3.1).
        bar_min = max(1, int(ctx.tf_seconds / 60))
        next_min = (cur_min + bar_min) % _MINUTES_PER_DAY
        next_active = _in_window(next_min, self._active_start_m, self._active_end_m)
        entry_time_ok = next_active and not ctx.session_last_bar
        in_date_range = self._start_ms <= bar.ts_open_ms <= self._end_ms
        day_ok = parts.weekday < len(self._day_ok) and self._day_ok[parts.weekday]

        # ── CYCLE RESET (Asian session open) ──
        if asian_start_bar:
            self.asian_high = bar.high
            self.asian_low = bar.low
            self.asian_ready = False
            self.traded_today = False
            self.sweep_hi_bar = -1
            self.sweep_lo_bar = -1
            self.arm_dir = 0
            if ctx.position_size == 0:
                self._reset_trade_vars()

        # Build the Asian range
        if asian_now:
            hi, lo = self.asian_high, self.asian_low
            self.asian_high = bar.high if hi is None else max(hi, bar.high)
            self.asian_low = bar.low if lo is None else min(lo, bar.low)

        # Finalize the range when the Asian session ends
        if asian_end_bar and self.asian_high is not None:
            self.asian_ready = True

        can_trade = self.asian_ready and day_ok and in_date_range and active_now

        # ── INDICATORS ── (ta.atr runs on every bar, at global scope)
        buffer = p.sl_buf * tick
        atr_init = self._atr.update(bar.high, bar.low, bar.close)

        # ── POSITION SIZING ──
        if p.risk_base == "Fixed Balance":
            risk_base_amt = p.fixed_bal
        elif p.risk_base == "Account Equity":
            risk_base_amt = ctx.equity
        else:
            risk_base_amt = ctx.initial_capital

        # ── CIRCUIT BREAKERS ──
        if asian_start_bar:
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
        if self.day_start_equity is None:
            day_base_eq = ctx.initial_capital
        else:
            day_base_eq = min(self.day_start_equity, ctx.initial_capital)
        daily_loss_cap = day_base_eq * (p.daily_dd_pct / 100.0)
        daily_real_pnl = 0.0 if self.day_start_net is None else ctx.net_profit - self.day_start_net
        daily_eq_pnl = 0.0 if self.day_start_equity is None else ctx.equity - self.day_start_equity
        if min(daily_real_pnl, daily_eq_pnl) <= -daily_loss_cap:
            self.halted_dd = True
        if self.consec_loss_today >= p.max_consec_loss:
            self.halted_streak = True
        halted = p.use_breakers and (self.halted_dd or self.halted_streak)

        # ── SWEEP CONFIRMATION (arming) ── runs BEFORE sweep detection, so the FVG is always
        # judged against a sweep of an earlier bar.
        if can_trade:
            min_size = p.min_fvg_ticks * tick
            low2 = h.low(2)
            high2 = h.high(2)
            short_window = (
                p.allow_short
                and self.sweep_hi_bar >= 0
                and (bar_index - self.sweep_hi_bar) <= p.max_bars
            )
            # high < low[2] is false while low[2] is na (first bars of the chart).
            if (
                short_window
                and low2 is not None
                and bar.high < low2
                and bar.close < _known(self.asian_high)
                and (low2 - bar.high) >= min_size
            ):
                self.arm_dir = -1
                self.arm_top = low2
                self.arm_bot = bar.high
                self.arm_bar = bar_index
                self.sweep_hi_bar = -1  # the sweep is consumed
            # LONG second: when both arm on one bar the long overwrites the short.
            long_window = (
                p.allow_long
                and self.sweep_lo_bar >= 0
                and (bar_index - self.sweep_lo_bar) <= p.max_bars
            )
            if (
                long_window
                and high2 is not None
                and bar.low > high2
                and bar.close > _known(self.asian_low)
                and (bar.low - high2) >= min_size
            ):
                self.arm_dir = 1
                self.arm_top = bar.low
                self.arm_bot = high2
                self.arm_bar = bar_index
                self.sweep_lo_bar = -1

        # ── SWEEP DETECTION ──
        if can_trade:
            asian_high = _known(self.asian_high)  # asian_ready implies a range
            asian_low = _known(self.asian_low)
            high1 = h.high(1)
            low1 = h.low(1)
            # high[1] na on the first bar: the comparison is false in Pine.
            crossed_up = bar.high > asian_high and high1 is not None and high1 <= asian_high
            if crossed_up or (active_first and bar.high > asian_high):
                self.sweep_hi_bar = bar_index
            crossed_dn = bar.low < asian_low and low1 is not None and low1 >= asian_low
            if crossed_dn or (active_first and bar.low < asian_low):
                self.sweep_lo_bar = bar_index

        # ── ENTRY (pullback touches the FVG) ──
        if (
            can_trade
            and not self.traded_today
            and not halted
            and ctx.position_size == 0
            and self.arm_dir != 0
        ):
            arm_top = _known(self.arm_top)
            arm_bot = _known(self.arm_bot)
            arm_bar = _known(self.arm_bar)
            # Invalidate first: a close through the gap kills the setup.
            long_dead = self.arm_dir == 1 and bar.close < arm_bot
            short_dead = self.arm_dir == -1 and bar.close > arm_top
            if long_dead or short_dead:
                self.arm_dir = 0
            atr_based = p.stop_method == "ATR Based"
            close = bar.close
            if self.arm_dir == 1 and entry_time_ok and bar_index > arm_bar and bar.low <= arm_top:
                if atr_based and atr_init is not None:
                    est_stop = close - atr_init * p.atr_mult_init
                else:
                    est_stop = arm_bot - buffer
                est_stop = min(est_stop, close - tick)
                qty = self._round_qty(self._f_qty(close, est_stop, risk_base_amt, ctx), ctx)
                # qty == 0: nothing is sent and the arming stays for the next bar (Pine).
                if qty > 0:
                    est_tp = close + p.rr * (close - est_stop)
                    actions.append(Entry(LONG_ID, Side.LONG, qty, comment=LONG_ID))
                    # Bracket on the signal bar: the fill bar is protected (invariant 2).
                    actions.append(Exit(EXIT_LONG_ID, LONG_ID, stop=est_stop, limit=est_tp))
                    self.in_dir = 1
                    self.in_fvg_stop = arm_bot - buffer
                    self.in_atr_dist = close - est_stop
                    self.in_initial_stop = None
                    self.in_take_profit = None
                    self.traded_today = True
                    self.arm_dir = 0
            elif (
                self.arm_dir == -1 and entry_time_ok and bar_index > arm_bar and bar.high >= arm_bot
            ):
                if atr_based and atr_init is not None:
                    est_stop = close + atr_init * p.atr_mult_init
                else:
                    est_stop = arm_top + buffer
                est_stop = max(est_stop, close + tick)
                qty = self._round_qty(self._f_qty(close, est_stop, risk_base_amt, ctx), ctx)
                if qty > 0:
                    est_tp = close - p.rr * (est_stop - close)
                    actions.append(Entry(SHORT_ID, Side.SHORT, qty, comment=SHORT_ID))
                    actions.append(Exit(EXIT_SHORT_ID, SHORT_ID, stop=est_stop, limit=est_tp))
                    self.in_dir = -1
                    self.in_fvg_stop = arm_top + buffer
                    self.in_atr_dist = est_stop - close
                    self.in_initial_stop = None
                    self.in_take_profit = None
                    self.traded_today = True
                    self.arm_dir = 0

        # ── TRADE MANAGEMENT (initial stop + fixed rr target) ──
        # tv_ohlc: the fill bar's close finalizes the bracket here, as Pine does; pessimistic:
        # `on_fill` already did, and this only re-issues the same levels.
        if (ctx.position_size > 0 and self.in_dir == 1) or (
            ctx.position_size < 0 and self.in_dir == -1
        ):
            avg = _known(ctx.position_avg_price)
            if self.in_take_profit is None:
                self._finalize_bracket(avg, self._bar_count, tick)
            actions.append(self._bracket_exit())

        # ── FORCED FLATS ── one reason per bar, filled at this bar's close
        active_exit = (not active_now and self._active_now_prev) or (
            active_now and ctx.session_last_bar
        )
        if p.close_at_end and active_exit:
            flat_reason = REASON_SESSION_END
        elif p.use_breakers and p.flatten_breach and self.halted_dd:
            flat_reason = REASON_DAILY_DD
        elif bar.ts_open_ms > self._end_ms:
            flat_reason = REASON_END_OF_RANGE
        else:
            flat_reason = ""
        if flat_reason and ctx.position_size != 0:
            actions.append(CloseAll(flat_reason))

        # Reset the management vars only once a trade has actually closed: on the signal bar
        # the entry is still pending (flat), and a plain flat test would wipe in_dir.
        if (
            ctx.position_size == 0
            and self._closed_count_prev is not None
            and closed_count > self._closed_count_prev
            and self.in_dir != 0
        ):
            self._reset_trade_vars()

        self._asian_now_prev = asian_now
        self._active_now_prev = active_now
        self._closed_count_prev = closed_count
        return actions

    def on_fill(self, fill: FillEvent, ctx: StrategyContext) -> Sequence[Action]:
        """Pessimistic model: finalize the bracket at the fill instant (SPEC §3.1)."""
        expected = LONG_ID if self.in_dir == 1 else SHORT_ID if self.in_dir == -1 else None
        if fill.entry_id != expected or self.in_take_profit is not None:
            return ()
        # The fill bar has not been seen yet: Pine would read ta.atr on it.
        self._finalize_bracket(fill.price, self._bar_count + 1, ctx.instrument.tick_size)
        return (self._bracket_exit(),)

    # -- shared arithmetic -----------------------------------------------------------------

    def _finalize_bracket(self, avg: float, bars_at_fill_bar: int, tick: float) -> None:
        """Pine's `if na(inTakeProfit)` block: initial stop and target from the real fill.

        Both fill models call this with the same fill price and get the same levels.
        `bars_at_fill_bar` is the number of bars up to and including the fill bar: ta.atr
        is na until `atr_len_init` bars have been seen, and the script tests `na(atrInit)`
        on the fill bar (the distance itself was captured on the signal bar).
        """
        p = self.params
        atr_known = bars_at_fill_bar >= p.atr_len_init
        use_atr = p.stop_method == "ATR Based" and atr_known
        if self.in_dir == 1:
            raw_stop = avg - _known(self.in_atr_dist) if use_atr else _known(self.in_fvg_stop)
            stop = min(raw_stop, avg - tick)  # keep the stop below the entry
            self.in_initial_stop = stop
            self.in_take_profit = avg + p.rr * (avg - stop)
        elif self.in_dir == -1:
            raw_stop = avg + _known(self.in_atr_dist) if use_atr else _known(self.in_fvg_stop)
            stop = max(raw_stop, avg + tick)  # keep the stop above the entry
            self.in_initial_stop = stop
            self.in_take_profit = avg - p.rr * (stop - avg)

    def _bracket_exit(self) -> Exit:
        stop = _known(self.in_initial_stop)
        target = _known(self.in_take_profit)
        if self.in_dir == 1:
            return Exit(EXIT_LONG_ID, LONG_ID, stop=stop, limit=target)
        return Exit(EXIT_SHORT_ID, SHORT_ID, stop=stop, limit=target)

    def _f_qty(
        self, est_entry: float, est_stop: float, base_amt: float, ctx: StrategyContext
    ) -> float:
        """Pine `f_qty`: the size that loses `risk_pct` of the risk base at the stop."""
        risk_amt = base_amt * (self.params.risk_pct / 100.0)
        # The tick floor prevents a size blow-up on a degenerate stop.
        dist = max(abs(est_entry - est_stop), ctx.instrument.tick_size)
        per_unit = dist * ctx.instrument.point_value
        return risk_amt / per_unit if per_unit > 0 else 0.0

    def _round_qty(self, q: float, ctx: StrategyContext) -> float:
        """Pine `f_roundQty`, then the instrument's own step (always DOWN, never up)."""
        rounding = self.params.qty_round
        whole = rounding == "Whole units" or (
            rounding == "Auto" and ctx.instrument.kind is InstrumentKind.FUTURES
        )
        if whole:
            q = float(math.floor(q))
        return ctx.instrument.floor_qty(q)

    def _reset_trade_vars(self) -> None:
        self.in_dir = 0
        self.in_fvg_stop = None
        self.in_initial_stop = None
        self.in_take_profit = None
        self.in_atr_dist = None


def _known[T](value: T | None) -> T:
    """A value the script's own flow guarantees is not na here; None is a port bug."""
    if value is None:
        raise RuntimeError("asian_sweep: internal state is na where the script defines it")
    return value
