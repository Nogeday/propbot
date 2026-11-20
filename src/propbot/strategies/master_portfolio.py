"""Master Prop Firm Portfolio: the port of `pine/master_prop_firm_portfolio.pine`.

SPEC §3.4.1. Two engines in one script, behind one global risk shield:

- S1 = NY Open ORB + FVG/IFVG with a trailing stop (the logic of SPEC §3.2),
- S2 = Asian Range Sweep & Reversal with a fixed bracket (the logic of SPEC §3.3).

A Pine strategy has ONE net position, so both legs may be open together only in the same
direction (`pyramiding = 2`); a leg is told apart from the other by its entry id ("S1 ..." /
"S2 ..."), and every Entry names its leg in `Entry.strategy_id` (`LEG_IDS`) so the trade
records are attributed per leg. The daily loss / loss streak breakers act on both engines
over the New York calendar day.

`on_bar` keeps the script's order block by block: (1) open-leg scan and reset of a leg that
left the book, (2) fill-bar re-pricing per leg, (3) global breakers, (4) S1, (5) S2,
(6) portfolio-level forced close. State variables carry the snake_case names of the Pine
`var`s. Not ported: plots, boxes, lines, labels, tables, bgcolor, the alert() JSON and the
stats-only state (`s1_stopQueue`, `s2_stopQueue`, `s2_sweepHiLvl`, `s2_sweepLoLvl`,
`s2_asianStartIdx`, `oosDrawn`).

This module was written from the Master script itself, not from the stand-alone ports:
the two differ (e.g. the Master's arm invalidation runs outside the entry gates and a leg's
state resets from the open-trade scan, not from the closed-trade count).
"""

import datetime as dt
import math
from collections.abc import Sequence
from dataclasses import dataclass
from typing import Annotated, Final, Literal
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from pydantic import Field, field_validator

from propbot.core.bars import Bar
from propbot.core.instrument import InstrumentKind
from propbot.core.session import LocalClock
from propbot.risk.config import StrictModel
from propbot.strategies.base import (
    Action,
    Close,
    CloseAll,
    Entry,
    Exit,
    FillEvent,
    Side,
    StrategyContext,
)
from propbot.strategies.history import BarHistory
from propbot.strategies.indicators import Atr, Highest, Lowest

STRATEGY_ID: Final = "master_portfolio"
PINE_FILE: Final = "master_prop_firm_portfolio.pine"
# The alert ids of the two legs (S1, S2); keys of propbot.signals.schema.STRATEGY_NAMES.
LEG_IDS: Final = ("master_ny_orb_fvg", "master_asian_sweep")
# strategy(pyramiding = 2): S1 and S2 may be open at once (same direction only).
PYRAMIDING: Final = 2

# Order ids and comments exactly as the Pine calls name them.
S1_LONG_ID: Final = "S1 Long"
S1_SHORT_ID: Final = "S1 Short"
S1_EXIT_LONG_ID: Final = "S1 ExitL"
S1_EXIT_SHORT_ID: Final = "S1 ExitS"
S2_LONG_ID: Final = "S2 Long"
S2_SHORT_ID: Final = "S2 Short"
S2_EXIT_LONG_ID: Final = "S2 ExitL"
S2_EXIT_SHORT_ID: Final = "S2 ExitS"
S1_REASON_EOD: Final = "S1 EOD"
S2_REASON_SESSION_END: Final = "S2 session end"
REASON_DAILY_DD: Final = "Daily DD halt"
REASON_END_OF_RANGE: Final = "End of test range"

# aType / aState of the S1 FVG arrays.
BULLISH: Final = 1
BEARISH: Final = -1
FRESH: Final = 0
INVERTED: Final = 1
DEAD: Final = 2
# `if array.size(s1_aTop) > 60` -> one array.shift per bar.
MAX_FVGS: Final = 60

_MINUTES_PER_DAY: Final = 1440
_EPOCH: Final = dt.datetime(1970, 1, 1, tzinfo=dt.UTC)
_MS: Final = dt.timedelta(milliseconds=1)
# high[2] / low[2] is the deepest look-back of the script.
_HISTORY_BARS: Final = 3
# `<= maxConcurrentRiskPct + 0.000001` in both entry gates.
_RISK_CAP_EPS: Final = 0.000001

type RiskBase = Literal["Initial Capital", "Account Equity", "Fixed Balance"]
type QtyRound = Literal["Auto", "Whole units", "Fractional"]
type BreakConfirm = Literal["Close", "Wick"]
type StopMethod = Literal["FVG Buffer", "ATR Based"]
type TrailMode = Literal["Swing (1m structure)", "ATR step", "Percent step"]

type Hour = Annotated[int, Field(ge=0, le=23)]
type Minute = Annotated[int, Field(ge=0, le=59)]


class MasterPortfolioParams(StrictModel):
    """One field per order-relevant Pine input; defaults are the script's defaults."""

    # Master — Toggles & Global Risk
    s1_enable: bool = True
    s2_enable: bool = True
    risk_pct: Annotated[float, Field(ge=0.1)] = 0.5
    risk_base: RiskBase = "Initial Capital"
    fixed_bal: Annotated[float, Field(ge=1)] = 10000.0
    rr: Annotated[float, Field(ge=0.1)] = 2.0
    max_concurrent_risk_pct: Annotated[float, Field(ge=0.1)] = 1.0
    tz: str = "America/New_York"
    qty_round: QtyRound = "Auto"
    # Global Circuit Breakers (Daily Lockout)
    use_breakers: bool = True
    daily_dd_pct: Annotated[float, Field(ge=0.1)] = 2.0
    flatten_breach: bool = True
    max_consec_loss: Annotated[int, Field(ge=1)] = 2
    # Backtest Date Range (oosStart drives only visuals and the table: not ported)
    start_date: dt.datetime = dt.datetime(2024, 1, 1, tzinfo=dt.UTC)
    end_date: dt.datetime = dt.datetime(2030, 1, 1, tzinfo=dt.UTC)
    # S1 · Session & Entry
    s1_or_start_hour: Hour = 9
    s1_or_start_min: Minute = 30
    s1_or_minutes: Annotated[int, Field(ge=1, le=60)] = 5
    s1_sess_end_hour: Hour = 15
    s1_sess_end_min: Minute = 59
    s1_close_at_end: bool = True
    s1_break_confirm: BreakConfirm = "Close"
    s1_allow_long: bool = True
    s1_allow_short: bool = True
    s1_use_fvg: bool = True
    s1_use_ifvg: bool = True
    s1_min_fvg_ticks: Annotated[int, Field(ge=0)] = 0
    # S1 · Risk & Stop
    s1_stop_method: StopMethod = "FVG Buffer"
    s1_sl_buf: Annotated[int, Field(ge=0)] = 2
    s1_atr_len_init: Annotated[int, Field(ge=1)] = 14
    s1_atr_mult_init: Annotated[float, Field(ge=0.1)] = 1.5
    # S1 · Trailing Stop
    s1_use_trail: bool = True
    s1_trail_mode: TrailMode = "Swing (1m structure)"
    s1_swing_len: Annotated[int, Field(ge=1)] = 5
    s1_atr_len_t: Annotated[int, Field(ge=1)] = 14
    s1_atr_mult_t: Annotated[float, Field(ge=0.1)] = 1.5
    s1_trail_pct: Annotated[float, Field(ge=0.01)] = 0.3
    s1_trail_act_r: Annotated[float, Field(ge=0.0)] = 1.0
    s1_trail_min_step_ticks: Annotated[int, Field(ge=1)] = 4
    # S1 · Day Filter
    s1_trade_mon: bool = True
    s1_trade_tue: bool = True
    s1_trade_wed: bool = True
    s1_trade_thu: bool = True
    s1_trade_fri: bool = True
    # S2 · Sessions & Entry
    s2_asian_start_hour: Hour = 20
    s2_asian_start_min: Minute = 0
    s2_asian_end_hour: Hour = 0
    s2_asian_end_min: Minute = 0
    s2_active_start_hour: Hour = 3
    s2_active_start_min: Minute = 0
    s2_active_end_hour: Hour = 11
    s2_active_end_min: Minute = 0
    s2_close_at_end: bool = True
    s2_max_bars: Annotated[int, Field(ge=1)] = 15
    s2_min_fvg_ticks: Annotated[int, Field(ge=0)] = 0
    s2_allow_long: bool = True
    s2_allow_short: bool = True
    # S2 · Risk & Stop
    s2_stop_method: StopMethod = "FVG Buffer"
    s2_sl_buf: Annotated[int, Field(ge=0)] = 2
    s2_atr_len_init: Annotated[int, Field(ge=1)] = 14
    s2_atr_mult_init: Annotated[float, Field(ge=0.1)] = 1.5
    # S2 · Day Filter
    s2_trade_mon: bool = True
    s2_trade_tue: bool = True
    s2_trade_wed: bool = True
    s2_trade_thu: bool = True
    s2_trade_fri: bool = True

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
        # Pine `input.time` is a UTC epoch; a naive datetime names no instant.
        if value.tzinfo is None or value.utcoffset() is None:
            raise ValueError("tarih saat dilimi içermeli (UTC)")
        return value.astimezone(dt.UTC)


@dataclass(slots=True)
class Fvg:
    """One row of the S1 parallel arrays s1_aTop/aBot/aType/aState/aFormBar/aFlipBar."""

    top: float
    bot: float
    kind: int  # aType: BULLISH / BEARISH
    state: int  # aState: FRESH / INVERTED / DEAD
    form_bar: int
    flip_bar: int  # -1 until inverted


def _in_window(minute: int, start: int, end: int) -> bool:
    """Pine `s2_asianNow` / `s2_activeNow`: [start, end) in minutes of day, may wrap midnight."""
    if end > start:
        return start <= minute < end
    return minute >= start or minute < end


def _epoch_ms(moment: dt.datetime) -> int:
    return (moment - _EPOCH) // _MS


class MasterPortfolio:
    """`propbot.strategies.base.Strategy` for the Master script. One instance per run."""

    def __init__(self, params: MasterPortfolioParams | None = None) -> None:
        p = params if params is not None else MasterPortfolioParams()
        self.params: Final = p
        self._clock: Final = LocalClock(p.tz)
        self._start_ms: Final = _epoch_ms(p.start_date)
        self._end_ms: Final = _epoch_ms(p.end_date)
        self._s1_or_start: Final = p.s1_or_start_hour * 60 + p.s1_or_start_min
        self._s1_or_end: Final = self._s1_or_start + p.s1_or_minutes
        self._s1_sess_end: Final = p.s1_sess_end_hour * 60 + p.s1_sess_end_min
        self._s2_asian_start_m: Final = p.s2_asian_start_hour * 60 + p.s2_asian_start_min
        self._s2_asian_end_m: Final = p.s2_asian_end_hour * 60 + p.s2_asian_end_min
        self._s2_active_start_m: Final = p.s2_active_start_hour * 60 + p.s2_active_start_min
        self._s2_active_end_m: Final = p.s2_active_end_hour * 60 + p.s2_active_end_min
        # Indexed by LocalParts.weekday (Monday = 0); Saturday / Sunday never match.
        self._s1_days: Final = (
            p.s1_trade_mon,
            p.s1_trade_tue,
            p.s1_trade_wed,
            p.s1_trade_thu,
            p.s1_trade_fri,
            False,
            False,
        )
        self._s2_days: Final = (
            p.s2_trade_mon,
            p.s2_trade_tue,
            p.s2_trade_wed,
            p.s2_trade_thu,
            p.s2_trade_fri,
            False,
            False,
        )
        self._history: Final = BarHistory(_HISTORY_BARS)
        # Global-scope ta.* calls run on every bar, like the script's INDICATORS block.
        self._s1_atr_t: Final = Atr(p.s1_atr_len_t)
        self._s1_atr_init: Final = Atr(p.s1_atr_len_init)
        self._s2_atr_init: Final = Atr(p.s2_atr_len_init)
        self._s1_sw_low: Final = Lowest(p.s1_swing_len)
        self._s1_sw_high: Final = Highest(p.s1_swing_len)
        self._bar_count = 0  # bars seen so far; decides whether ta.atr is still na

        # ---- S1 `var` state ----
        self.s1_or_high: float | None = None
        self.s1_or_low: float | None = None
        self.s1_or_ready = False
        self.s1_traded_today = False
        self.s1_bias = 0
        self.s1_fvgs: list[Fvg] = []
        self.s1_in_dir = 0
        self.s1_fill_price: float | None = None  # real fill of the S1 leg; na until its fill bar
        self.s1_in_fvg_stop: float | None = None  # structural FVG-buffer stop (signal bar)
        self.s1_in_atr_dist: float | None = None  # ATR-method distance (signal bar)
        self.s1_in_initial_stop: float | None = None
        self.s1_in_take_profit: float | None = None
        self.s1_in_trail_stop: float | None = None
        self.s1_entry_bar: int | None = None

        # ---- S2 `var` state ----
        self.s2_asian_high: float | None = None
        self.s2_asian_low: float | None = None
        self.s2_asian_ready = False
        self.s2_traded_today = False
        self.s2_sweep_hi_bar = -1
        self.s2_sweep_lo_bar = -1
        self.s2_arm_dir = 0
        self.s2_arm_top: float | None = None
        self.s2_arm_bot: float | None = None
        self.s2_arm_bar: int | None = None
        self.s2_in_dir = 0
        self.s2_fill_price: float | None = None
        self.s2_in_fvg_stop: float | None = None
        self.s2_in_atr_dist: float | None = None
        self.s2_in_initial_stop: float | None = None
        self.s2_in_take_profit: float | None = None
        self.s2_entry_bar: int | None = None

        # ---- global breaker `var` state ----
        self.g_day_start_equity: float | None = None
        self.g_day_start_net: float | None = None
        self.g_consec_loss = 0
        self.g_last_closed = 0
        self.g_halted_dd = False
        self.g_halted_streak = False
        # Series values of the last bar, kept for inspection (not `var` in Pine).
        self.halted = False
        self.g_force_flat = False

        # ---- previous-bar values of the series the script reads with [1] ----
        # `dayofmonth(time, tz)[1]`: na on the first bar, where `na != 0` is false.
        self._prev_dayofmonth: int | None = None
        # `s2_asianNow[1]` / `s2_activeNow[1]`: na on the first bar; an na bool counts as
        # false, so a chart that opens inside a window starts there (VERIFY in TradingView).
        self._s2_asian_now_prev = False
        self._s2_active_now_prev = False

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
        high, low, close = bar.high, bar.low, bar.close
        actions: list[Action] = []

        # ── TIME / DATE / DAY (shared) ──
        parts = self._clock.parts(bar.ts_open_ms)
        cur_min = parts.minute_of_day
        in_date_range = self._start_ms <= bar.ts_open_ms <= self._end_ms
        # newDay = ta.change(dayofmonth(time, tz)) != 0 — false on the first bar (na != 0).
        new_day = self._prev_dayofmonth is not None and parts.day != self._prev_dayofmonth
        self._prev_dayofmonth = parts.day

        # S1 session math
        s1_or_start, s1_or_end, s1_sess_end = self._s1_or_start, self._s1_or_end, self._s1_sess_end
        s1_is_in_or = s1_or_start <= cur_min < s1_or_end
        # ">=" + session.islastbar: also flattens on half days / a missing sessEnd bar.
        s1_at_session_end = cur_min >= s1_sess_end or ctx.session_last_bar
        s1_day_ok = self._s1_days[parts.weekday]

        # S2 session math (windows may wrap past midnight)
        s2_asian_now = _in_window(cur_min, self._s2_asian_start_m, self._s2_asian_end_m)
        s2_active_now = _in_window(cur_min, self._s2_active_start_m, self._s2_active_end_m)
        s2_asian_start_bar = s2_asian_now and not self._s2_asian_now_prev
        s2_asian_end_bar = not s2_asian_now and self._s2_asian_now_prev
        s2_active_first = s2_active_now and not self._s2_active_now_prev
        s2_active_exit = (not s2_active_now and self._s2_active_now_prev) or (
            s2_active_now and ctx.session_last_bar
        )
        s2_day_ok = self._s2_days[parts.weekday]
        self._s2_asian_now_prev = s2_asian_now
        self._s2_active_now_prev = s2_active_now

        # Entry-time gates: a market entry fills at the NEXT bar's open (SPEC §3.1).
        bar_min = max(1, int(ctx.tf_seconds / 60))
        s1_entry_time_ok = not ctx.session_last_bar and cur_min + bar_min < s1_sess_end
        s2_next_min = (cur_min + bar_min) % _MINUTES_PER_DAY
        s2_next_active = _in_window(s2_next_min, self._s2_active_start_m, self._s2_active_end_m)
        s2_entry_time_ok = s2_next_active and not ctx.session_last_bar

        # ── DAILY / CYCLE RESETS ──
        if new_day:  # S1: New York calendar day
            self.s1_or_high = None
            self.s1_or_low = None
            self.s1_or_ready = False
            self.s1_traded_today = False
            self.s1_bias = 0
            self.s1_fvgs.clear()
        if s2_asian_start_bar:  # S2: Asian session open
            self.s2_asian_high = high
            self.s2_asian_low = low
            self.s2_asian_ready = False
            self.s2_traded_today = False
            self.s2_sweep_hi_bar = -1
            self.s2_sweep_lo_bar = -1
            self.s2_arm_dir = 0

        # ── BUILD RANGES ──
        if s1_is_in_or:
            hi, lo = self.s1_or_high, self.s1_or_low
            self.s1_or_high = high if hi is None else max(hi, high)
            self.s1_or_low = low if lo is None else min(lo, low)
        if (
            not self.s1_or_ready
            and self.s1_or_high is not None
            and s1_or_end <= cur_min <= s1_sess_end
        ):
            self.s1_or_ready = True
        if s2_asian_now:
            hi, lo = self.s2_asian_high, self.s2_asian_low
            self.s2_asian_high = high if hi is None else max(hi, high)
            self.s2_asian_low = low if lo is None else min(lo, low)
        if s2_asian_end_bar and self.s2_asian_high is not None:
            self.s2_asian_ready = True

        # ── INDICATORS ──
        s1_buffer = p.s1_sl_buf * tick
        s2_buffer = p.s2_sl_buf * tick
        s1_trail_min_step = p.s1_trail_min_step_ticks * tick
        s1_atr_t = self._s1_atr_t.update(high, low, close)
        s1_atr_init = self._s1_atr_init.update(high, low, close)
        s2_atr_init = self._s2_atr_init.update(high, low, close)
        s1_sw_low = self._s1_sw_low.update(low)
        s1_sw_high = self._s1_sw_high.update(high)

        # ── POSITION SIZING ──
        if p.risk_base == "Fixed Balance":
            risk_base_amt = p.fixed_bal
        elif p.risk_base == "Account Equity":
            risk_base_amt = ctx.equity
        else:
            risk_base_amt = ctx.initial_capital

        # ── (1) OPEN-STATE SCAN + PER-LEG CLOSE DETECTION ──
        s1_is_open = False
        s2_is_open = False
        s1_open_price: float | None = None
        s2_open_price: float | None = None
        for trade in ctx.open_trades:
            if trade.entry_id.startswith("S1"):
                s1_is_open = True
                s1_open_price = trade.entry_price
            elif trade.entry_id.startswith("S2"):
                s2_is_open = True
                s2_open_price = trade.entry_price
        # The 1-bar guard: on the signal bar the entry is still working, not on the book.
        if self.s1_in_dir != 0 and bar_index > _known(self.s1_entry_bar) and not s1_is_open:
            self._reset_s1_leg()
        if self.s2_in_dir != 0 and bar_index > _known(self.s2_entry_bar) and not s2_is_open:
            self._reset_s2_leg()

        # ── (2) FILL-BAR RE-PRICING ── tv_ohlc: here, at the fill bar's close (Pine's own
        # timing); pessimistic: `on_fill` already did it and s*_fill_price is set.
        if self.s1_in_dir != 0 and s1_is_open and self.s1_fill_price is None:
            self._reprice_s1(_known(s1_open_price), self._bar_count, tick)
        if self.s2_in_dir != 0 and s2_is_open and self.s2_fill_price is None:
            self._reprice_s2(_known(s2_open_price), self._bar_count, tick)

        # ── (3) GLOBAL CIRCUIT BREAKERS ── closedtrades / netprofit aggregate both engines.
        if new_day:
            self.g_day_start_equity = ctx.equity
            self.g_day_start_net = ctx.net_profit
            self.g_consec_loss = 0
            self.g_halted_dd = False
            self.g_halted_streak = False
        closed_count = len(ctx.closed_trades)
        if closed_count > self.g_last_closed:
            for j in range(self.g_last_closed, closed_count):
                if ctx.closed_trades[j].profit < 0:
                    self.g_consec_loss += 1
                else:
                    self.g_consec_loss = 0
            self.g_last_closed = closed_count
        if self.g_day_start_equity is None:
            g_base_eq = ctx.initial_capital
        else:
            g_base_eq = min(self.g_day_start_equity, ctx.initial_capital)
        g_loss_cap = g_base_eq * (p.daily_dd_pct / 100.0)
        g_real_pnl = 0.0 if self.g_day_start_net is None else ctx.net_profit - self.g_day_start_net
        # Floating check: bar-close equity holds the open legs' P&L.
        g_eq_pnl = 0.0 if self.g_day_start_equity is None else ctx.equity - self.g_day_start_equity
        if min(g_real_pnl, g_eq_pnl) <= -g_loss_cap:
            self.g_halted_dd = True
        if self.g_consec_loss >= p.max_consec_loss:
            self.g_halted_streak = True
        halted = p.use_breakers and (self.g_halted_dd or self.g_halted_streak)
        self.halted = halted
        # Portfolio-level forced flat (executed at the bottom); on such a bar the per-engine
        # closes stand down, so every leg gets exactly one close order.
        g_flat_dd = p.use_breakers and p.flatten_breach and self.g_halted_dd
        g_force_flat = (g_flat_dd or bar.ts_open_ms > self._end_ms) and ctx.position_size != 0
        self.g_force_flat = g_force_flat

        # ═══ (4) S1 · NY BREAKOUT ENGINE ═══
        s1_can_scan = (
            p.s1_enable
            and self.s1_or_ready
            and s1_day_ok
            and in_date_range
            and s1_or_end <= cur_min < s1_sess_end
        )
        # Breakout -> bias (fixed for the day)
        if s1_can_scan and self.s1_bias == 0:
            or_high, or_low = _known(self.s1_or_high), _known(self.s1_or_low)
            if p.s1_break_confirm == "Close":
                broke_up, broke_dn = close > or_high, close < or_low
            else:
                broke_up, broke_dn = high > or_high, low < or_low
            if broke_up and p.s1_allow_long:
                self.s1_bias = 1
            elif broke_dn and p.s1_allow_short:
                self.s1_bias = -1

        # Detect FVGs after the breakout (high[2] / low[2] na on the first bars: false)
        if s1_can_scan and self.s1_bias != 0:
            min_size = p.s1_min_fvg_ticks * tick
            high2, low2 = h.high(2), h.low(2)
            if high2 is not None and low > high2 and (low - high2) >= min_size:
                self.s1_fvgs.append(Fvg(low, high2, BULLISH, FRESH, bar_index, -1))
            if low2 is not None and high < low2 and (low2 - high) >= min_size:
                self.s1_fvgs.append(Fvg(low2, high, BEARISH, FRESH, bar_index, -1))
            if len(self.s1_fvgs) > MAX_FVGS:
                self.s1_fvgs.pop(0)

        # Update inversions / deaths (before the entry check)
        if s1_can_scan and self.s1_fvgs:
            for fvg in self.s1_fvgs:
                if fvg.state == DEAD:
                    continue
                if fvg.kind == BULLISH:
                    if fvg.state == FRESH and close < fvg.bot:
                        fvg.state = INVERTED
                        fvg.flip_bar = bar_index
                    elif fvg.state == INVERTED and close > fvg.top:
                        fvg.state = DEAD
                elif fvg.state == FRESH and close > fvg.top:
                    fvg.state = INVERTED
                    fvg.flip_bar = bar_index
                elif fvg.state == INVERTED and close < fvg.bot:
                    fvg.state = DEAD

        # S1 entry gates: entry time + global halt + concurrency cap + opposite-direction block.
        # The per-leg s*_in_dir flags are same-bar accurate, so S1 (run first) wins a conflict.
        s1_committed_other = p.risk_pct if self.s2_in_dir != 0 else 0.0
        s1_risk_ok = (s1_committed_other + p.risk_pct) <= p.max_concurrent_risk_pct + _RISK_CAP_EPS
        s1_dir_ok = self.s2_in_dir == 0 or self.s2_in_dir == self.s1_bias
        s1_can_enter = (
            s1_can_scan
            and s1_entry_time_ok
            and not self.s1_traded_today
            and not halted
            and self.s1_in_dir == 0
            and s1_risk_ok
            and s1_dir_ok
            and self.s1_bias != 0
            and len(self.s1_fvgs) > 0
        )
        if s1_can_enter:
            actions.extend(
                self._s1_entry(bar, bar_index, tick, s1_buffer, s1_atr_init, risk_base_amt, ctx)
            )

        # S1 trade management (re-priced bracket + rr target + optional trailing). Trailing
        # starts only once the leg has its real fill price and is measured from it.
        if self.s1_in_dir == 1:
            fill = self.s1_fill_price
            trail = _known(self.s1_in_trail_stop)
            if p.s1_use_trail and fill is not None:
                risk_l = max(fill - _known(self.s1_in_initial_stop), tick)
                cand: float | None
                if p.s1_trail_mode == "Swing (1m structure)":
                    cand = None if s1_sw_low is None else s1_sw_low - s1_buffer
                elif p.s1_trail_mode == "ATR step":
                    cand = None if s1_atr_t is None else close - s1_atr_t * p.s1_atr_mult_t
                else:
                    cand = close * (1 - p.s1_trail_pct / 100)
                # only ever moves up, in steps of at least s1_trailMinStepTicks (na: false)
                if (
                    cand is not None
                    and (close - fill) >= p.s1_trail_act_r * risk_l
                    and cand - trail >= s1_trail_min_step
                ):
                    self.s1_in_trail_stop = cand
            actions.append(self._s1_exit())
        elif self.s1_in_dir == -1:
            fill = self.s1_fill_price
            trail = _known(self.s1_in_trail_stop)
            if p.s1_use_trail and fill is not None:
                risk_s = max(_known(self.s1_in_initial_stop) - fill, tick)
                cand_s: float | None
                if p.s1_trail_mode == "Swing (1m structure)":
                    cand_s = None if s1_sw_high is None else s1_sw_high + s1_buffer
                elif p.s1_trail_mode == "ATR step":
                    cand_s = None if s1_atr_t is None else close + s1_atr_t * p.s1_atr_mult_t
                else:
                    cand_s = close * (1 + p.s1_trail_pct / 100)
                if (
                    cand_s is not None
                    and (fill - close) >= p.s1_trail_act_r * risk_s
                    and trail - cand_s >= s1_trail_min_step
                ):
                    self.s1_in_trail_stop = cand_s
            actions.append(self._s1_exit())

        # S1 force flat at its session end (immediately = this bar's close)
        if p.s1_close_at_end and s1_at_session_end and self.s1_in_dir != 0 and not g_force_flat:
            leg = S1_LONG_ID if self.s1_in_dir == 1 else S1_SHORT_ID
            actions.append(Close(leg, S1_REASON_EOD))

        # ═══ (5) S2 · ASIAN SWEEP ENGINE ═══
        s2_can_trade = (
            p.s2_enable and self.s2_asian_ready and s2_day_ok and in_date_range and s2_active_now
        )
        # Sweep confirmation (arming) runs BEFORE detection: the FVG follows an earlier sweep.
        if s2_can_trade:
            min_size = p.s2_min_fvg_ticks * tick
            high2, low2 = h.high(2), h.low(2)
            if (
                p.s2_allow_short
                and self.s2_sweep_hi_bar >= 0
                and (bar_index - self.s2_sweep_hi_bar) <= p.s2_max_bars
            ) and (
                low2 is not None
                and high < low2
                and close < _known(self.s2_asian_high)
                and (low2 - high) >= min_size
            ):
                self.s2_arm_dir = -1
                self.s2_arm_top = low2
                self.s2_arm_bot = high
                self.s2_arm_bar = bar_index
                self.s2_sweep_hi_bar = -1
            # Long second: when both arm on one bar the long overwrites the short.
            if (
                p.s2_allow_long
                and self.s2_sweep_lo_bar >= 0
                and (bar_index - self.s2_sweep_lo_bar) <= p.s2_max_bars
            ) and (
                high2 is not None
                and low > high2
                and close > _known(self.s2_asian_low)
                and (low - high2) >= min_size
            ):
                self.s2_arm_dir = 1
                self.s2_arm_top = low
                self.s2_arm_bot = high2
                self.s2_arm_bar = bar_index
                self.s2_sweep_lo_bar = -1

        # Sweep detection (marks sweeps for later bars; high[1] na on the first bar: false)
        if s2_can_trade:
            asian_high, asian_low = _known(self.s2_asian_high), _known(self.s2_asian_low)
            high1, low1 = h.high(1), h.low(1)
            crossed_up = high > asian_high and high1 is not None and high1 <= asian_high
            if crossed_up or (s2_active_first and high > asian_high):
                self.s2_sweep_hi_bar = bar_index
            crossed_dn = low < asian_low and low1 is not None and low1 >= asian_low
            if crossed_dn or (s2_active_first and low < asian_low):
                self.s2_sweep_lo_bar = bar_index

        # Invalidate a stale arm once price has closed through the gap (no entry gates here)
        if s2_can_trade and self.s2_arm_dir == 1 and close < _known(self.s2_arm_bot):
            self.s2_arm_dir = 0
        if s2_can_trade and self.s2_arm_dir == -1 and close > _known(self.s2_arm_top):
            self.s2_arm_dir = 0

        # S2 entry gates (mirror of S1; S1 has already run on this bar)
        s2_committed_other = p.risk_pct if self.s1_in_dir != 0 else 0.0
        s2_risk_ok = (s2_committed_other + p.risk_pct) <= p.max_concurrent_risk_pct + _RISK_CAP_EPS
        s2_dir_ok = self.s1_in_dir == 0 or self.s1_in_dir == self.s2_arm_dir
        s2_can_enter = (
            s2_can_trade
            and s2_entry_time_ok
            and not self.s2_traded_today
            and not halted
            and self.s2_in_dir == 0
            and self.s2_arm_dir != 0
            and s2_risk_ok
            and s2_dir_ok
        )
        if s2_can_enter:
            actions.extend(
                self._s2_entry(bar, bar_index, tick, s2_buffer, s2_atr_init, risk_base_amt, ctx)
            )

        # S2 trade management (re-priced bracket + fixed rr target; no trailing)
        if self.s2_in_dir != 0:
            actions.append(self._s2_exit())

        # S2 force flat at its active-window end (immediately = this bar's close)
        if p.s2_close_at_end and s2_active_exit and self.s2_in_dir != 0 and not g_force_flat:
            leg = S2_LONG_ID if self.s2_in_dir == 1 else S2_SHORT_ID
            actions.append(Close(leg, S2_REASON_SESSION_END))

        # ── (6) FORCED FLAT: DAILY-LOSS BREACH / END OF TEST RANGE ── both legs at this close
        if g_force_flat:
            actions.append(CloseAll(REASON_DAILY_DD if g_flat_dd else REASON_END_OF_RANGE))
        return actions

    def on_fill(self, fill: FillEvent, ctx: StrategyContext) -> Sequence[Action]:
        """Pessimistic model: re-price the leg that just filled, from its own fill price."""
        tick = ctx.instrument.tick_size
        # The fill bar has not been seen yet: Pine reads ta.atr's na-ness on it.
        bars_at_fill_bar = self._bar_count + 1
        if (
            self.s1_in_dir != 0
            and fill.entry_id == _leg_id(self.s1_in_dir, S1_LONG_ID, S1_SHORT_ID)
            and self.s1_fill_price is None
        ):
            self._reprice_s1(fill.price, bars_at_fill_bar, tick)
            return (self._s1_exit(),)
        if (
            self.s2_in_dir != 0
            and fill.entry_id == _leg_id(self.s2_in_dir, S2_LONG_ID, S2_SHORT_ID)
            and self.s2_fill_price is None
        ):
            self._reprice_s2(fill.price, bars_at_fill_bar, tick)
            return (self._s2_exit(),)
        return ()

    # -- entries ---------------------------------------------------------------------------

    def _s1_entry(
        self,
        bar: Bar,
        bar_index: int,
        tick: float,
        buffer: float,
        atr_init: float | None,
        risk_base_amt: float,
        ctx: StrategyContext,
    ) -> list[Action]:
        """The S1 `if s1_canEnter` block: newest zone first, the first touched zone decides."""
        p = self.params
        long = self.s1_bias == 1
        atr_based = p.s1_stop_method == "ATR Based" and atr_init is not None
        for fvg in reversed(self.s1_fvgs):
            if fvg.state == DEAD:
                continue
            if long:
                zone = (fvg.kind == BULLISH and fvg.state == FRESH and p.s1_use_fvg) or (
                    fvg.kind == BEARISH and fvg.state == INVERTED and p.s1_use_ifvg
                )
            else:
                zone = (fvg.kind == BEARISH and fvg.state == FRESH and p.s1_use_fvg) or (
                    fvg.kind == BULLISH and fvg.state == INVERTED and p.s1_use_ifvg
                )
            if not zone:
                continue
            act_bar = fvg.flip_bar if fvg.state == INVERTED else fvg.form_bar
            touched = bar.low <= fvg.top if long else bar.high >= fvg.bot
            if not (bar_index > act_bar and touched):
                continue
            est_entry = bar.close
            if long:
                est_stop = (
                    est_entry - _known(atr_init) * p.s1_atr_mult_init
                    if atr_based
                    else fvg.bot - buffer
                )
                est_stop = min(est_stop, est_entry - tick)
                s1_risk = max(est_entry - est_stop, tick)
            else:
                est_stop = (
                    est_entry + _known(atr_init) * p.s1_atr_mult_init
                    if atr_based
                    else fvg.top + buffer
                )
                est_stop = max(est_stop, est_entry + tick)
                s1_risk = max(est_stop - est_entry, tick)
            qty = self._round_qty(self._f_qty(est_entry, est_stop, risk_base_amt, ctx), ctx)
            # `break` follows `if qty > 0`: a zero size skips the trade and ends the scan.
            if qty <= 0:
                return []
            self.s1_in_dir = 1 if long else -1
            self.s1_fill_price = None  # set on the fill bar
            self.s1_in_fvg_stop = fvg.bot - buffer if long else fvg.top + buffer
            self.s1_in_atr_dist = est_entry - est_stop if long else est_stop - est_entry
            self.s1_in_initial_stop = est_stop
            self.s1_in_trail_stop = est_stop
            self.s1_in_take_profit = est_entry + p.rr * s1_risk * (1 if long else -1)
            self.s1_entry_bar = bar_index
            self.s1_traded_today = True
            leg = S1_LONG_ID if long else S1_SHORT_ID
            side = Side.LONG if long else Side.SHORT
            # The bracket follows from the management block on this same bar.
            return [Entry(leg, side, qty, comment=leg, strategy_id=LEG_IDS[0])]
        return []

    def _s2_entry(
        self,
        bar: Bar,
        bar_index: int,
        tick: float,
        buffer: float,
        atr_init: float | None,
        risk_base_amt: float,
        ctx: StrategyContext,
    ) -> list[Action]:
        """The S2 `if s2_canEnter` block: a later bar touches the armed FVG."""
        p = self.params
        arm_top, arm_bot = _known(self.s2_arm_top), _known(self.s2_arm_bot)
        arm_bar = _known(self.s2_arm_bar)
        atr_based = p.s2_stop_method == "ATR Based" and atr_init is not None
        est_entry = bar.close
        if self.s2_arm_dir == 1 and bar_index > arm_bar and bar.low <= arm_top:
            if atr_based:
                est_stop = est_entry - _known(atr_init) * p.s2_atr_mult_init
            else:
                est_stop = arm_bot - buffer
            est_stop = min(est_stop, est_entry - tick)
            s2_risk = max(est_entry - est_stop, tick)
            qty = self._round_qty(self._f_qty(est_entry, est_stop, risk_base_amt, ctx), ctx)
            # qty == 0: nothing is sent and the arming stays (Pine).
            if qty <= 0:
                return []
            self.s2_in_dir = 1
            self.s2_fill_price = None
            self.s2_in_fvg_stop = arm_bot - buffer
            self.s2_in_atr_dist = est_entry - est_stop
            self.s2_in_initial_stop = est_stop
            self.s2_in_take_profit = est_entry + p.rr * s2_risk
            self.s2_entry_bar = bar_index
            self.s2_traded_today = True
            self.s2_arm_dir = 0
            return [Entry(S2_LONG_ID, Side.LONG, qty, comment=S2_LONG_ID, strategy_id=LEG_IDS[1])]
        if self.s2_arm_dir == -1 and bar_index > arm_bar and bar.high >= arm_bot:
            if atr_based:
                est_stop = est_entry + _known(atr_init) * p.s2_atr_mult_init
            else:
                est_stop = arm_top + buffer
            est_stop = max(est_stop, est_entry + tick)
            s2_risk = max(est_stop - est_entry, tick)
            qty = self._round_qty(self._f_qty(est_entry, est_stop, risk_base_amt, ctx), ctx)
            if qty <= 0:
                return []
            self.s2_in_dir = -1
            self.s2_fill_price = None
            self.s2_in_fvg_stop = arm_top + buffer
            self.s2_in_atr_dist = est_stop - est_entry
            self.s2_in_initial_stop = est_stop
            self.s2_in_take_profit = est_entry - p.rr * s2_risk
            self.s2_entry_bar = bar_index
            self.s2_traded_today = True
            self.s2_arm_dir = 0
            return [
                Entry(S2_SHORT_ID, Side.SHORT, qty, comment=S2_SHORT_ID, strategy_id=LEG_IDS[1])
            ]
        return []

    # -- shared arithmetic (both fill models) -----------------------------------------------

    def _reprice_s1(self, fill_price: float, bars_at_fill_bar: int, tick: float) -> None:
        """FILL-BAR RE-PRICING of the S1 leg from ITS OWN fill price (never the blend).

        `bars_at_fill_bar` counts the bars up to and including the fill bar: the script tests
        `na(s1_atrInit)` on the fill bar, and ta.atr is na until `s1_atr_len_init` bars.
        """
        p = self.params
        use_atr = p.s1_stop_method == "ATR Based" and bars_at_fill_bar >= p.s1_atr_len_init
        self.s1_fill_price = fill_price
        if self.s1_in_dir == 1:
            raw = fill_price - _known(self.s1_in_atr_dist) if use_atr else self.s1_in_fvg_stop
            stop = min(_known(raw), fill_price - tick)
            self.s1_in_initial_stop = stop
            self.s1_in_take_profit = fill_price + p.rr * (fill_price - stop)
        else:
            raw = fill_price + _known(self.s1_in_atr_dist) if use_atr else self.s1_in_fvg_stop
            stop = max(_known(raw), fill_price + tick)
            self.s1_in_initial_stop = stop
            self.s1_in_take_profit = fill_price - p.rr * (stop - fill_price)
        self.s1_in_trail_stop = stop  # seed the trail

    def _reprice_s2(self, fill_price: float, bars_at_fill_bar: int, tick: float) -> None:
        """FILL-BAR RE-PRICING of the S2 leg (same rules as S1, without the trail)."""
        p = self.params
        use_atr = p.s2_stop_method == "ATR Based" and bars_at_fill_bar >= p.s2_atr_len_init
        self.s2_fill_price = fill_price
        if self.s2_in_dir == 1:
            raw = fill_price - _known(self.s2_in_atr_dist) if use_atr else self.s2_in_fvg_stop
            stop = min(_known(raw), fill_price - tick)
            self.s2_in_initial_stop = stop
            self.s2_in_take_profit = fill_price + p.rr * (fill_price - stop)
        else:
            raw = fill_price + _known(self.s2_in_atr_dist) if use_atr else self.s2_in_fvg_stop
            stop = max(_known(raw), fill_price + tick)
            self.s2_in_initial_stop = stop
            self.s2_in_take_profit = fill_price - p.rr * (stop - fill_price)

    def _s1_exit(self) -> Exit:
        stop = _known(self.s1_in_trail_stop)
        target = _known(self.s1_in_take_profit)
        if self.s1_in_dir == 1:
            return Exit(S1_EXIT_LONG_ID, S1_LONG_ID, stop=stop, limit=target)
        return Exit(S1_EXIT_SHORT_ID, S1_SHORT_ID, stop=stop, limit=target)

    def _s2_exit(self) -> Exit:
        stop = _known(self.s2_in_initial_stop)
        target = _known(self.s2_in_take_profit)
        if self.s2_in_dir == 1:
            return Exit(S2_EXIT_LONG_ID, S2_LONG_ID, stop=stop, limit=target)
        return Exit(S2_EXIT_SHORT_ID, S2_SHORT_ID, stop=stop, limit=target)

    def _f_qty(
        self, est_entry: float, est_stop: float, base_amt: float, ctx: StrategyContext
    ) -> float:
        """Pine `f_qty`: the size that loses `risk_pct` of the risk base at the stop."""
        risk_amt = base_amt * (self.params.risk_pct / 100.0)
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

    def _reset_s1_leg(self) -> None:
        self.s1_in_dir = 0
        self.s1_fill_price = None
        self.s1_in_fvg_stop = None
        self.s1_in_atr_dist = None
        self.s1_in_initial_stop = None
        self.s1_in_take_profit = None
        self.s1_in_trail_stop = None

    def _reset_s2_leg(self) -> None:
        self.s2_in_dir = 0
        self.s2_fill_price = None
        self.s2_in_fvg_stop = None
        self.s2_in_atr_dist = None
        self.s2_in_initial_stop = None
        self.s2_in_take_profit = None


def _leg_id(direction: int, long_id: str, short_id: str) -> str:
    return long_id if direction == 1 else short_id


def _known[T](value: T | None) -> T:
    """A value the script's own flow guarantees is not na here; None is a port bug."""
    if value is None:
        raise RuntimeError("master_portfolio: internal state is na where the script defines it")
    return value
