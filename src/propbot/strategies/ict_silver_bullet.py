"""ICT Silver Bullet (Sweep -> MSS -> FVG): the port of `pine/ict_silver_bullet_strategy.pine`.

SPEC §3.4.2. Inside the Silver Bullet windows (default 10:00-11:00 and 14:00-15:00 New York)
a sweep of the last confirmed pivot high (short; tried first) or pivot low (long) starts a
hunt; a CLOSE through the opposing pivot within `max_bars_sweep_mss` bars is the market
structure shift; a fresh FVG in the hunt's direction within `max_bars_mss_fvg` bars of the
MSS arms a LIMIT entry together with its bracket (stop beyond the sweep extreme, target by
fixed R:R or the next opposing liquidity pool). An unfilled order is cancelled when the
close breaks the stop, when the order-placement gate closes or after `max_bars_to_fill`.
Prop guards: daily loss limit, consecutive losses per day, overall drawdown, day-end flat.

The bar-by-bar order of the Pine script is kept block by block in `on_bar`; the state
variables carry the snake_case names of the Pine `var`s. Visual-only state (labels, lines,
boxes, `oosDrawn`, the dashboard's `stopArr` / `dirArr`) and the alert() JSON are not
ported.
"""

import datetime as dt
import math
from collections import deque
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
from propbot.strategies.indicators import PivotHigh, PivotLow

STRATEGY_ID: Final = "ict_silver_bullet"
PINE_FILE: Final = "ict_silver_bullet_strategy.pine"
# strategy(pyramiding = 0)
PYRAMIDING: Final = 0

# Order ids and comments exactly as the Pine calls name them.
LONG_ID: Final = "Long"
SHORT_ID: Final = "Short"
EXIT_LONG_ID: Final = "Exit L"
EXIT_SHORT_ID: Final = "Exit S"
REASON_DAILY_DD: Final = "Daily DD halt"
REASON_OVERALL_DD: Final = "Overall DD halt"
REASON_EOD: Final = "EOD flat"
REASON_END_OF_RANGE: Final = "End of range"

# Pine: `if array.size(phArr) > 25 array.shift(phArr)` -> a FIFO of the latest 25 pivots.
LIQUIDITY_POOL_SIZE: Final = 25

_MS_PER_SECOND: Final = 1000
# high[2] / low[2] is the deepest look-back of the script.
_HISTORY_BARS: Final = 3

type EntryFill = Literal[
    "Proximal (first touch)", "50% (Consequent Encroachment)", "Distal (full gap fill)"
]
type RiskBase = Literal["Initial Capital", "Account Equity", "Fixed Balance"]
type TpMode = Literal["Fixed R:R", "Next opposing liquidity"]


class IctSilverBulletParams(StrictModel):
    """One field per order-relevant Pine input; defaults are the script's defaults."""

    # 1) Windows
    tz: str = "America/New_York"
    enable_am: bool = True
    am_start_h: int = Field(default=10, ge=0, le=23)
    am_start_m: int = Field(default=0, ge=0, le=59)
    am_end_h: int = Field(default=11, ge=0, le=23)
    am_end_m: int = Field(default=0, ge=0, le=59)
    enable_pm: bool = True
    pm_start_h: int = Field(default=14, ge=0, le=23)
    pm_start_m: int = Field(default=0, ge=0, le=59)
    pm_end_h: int = Field(default=15, ge=0, le=23)
    pm_end_m: int = Field(default=0, ge=0, le=59)
    # 2) Sweep & structure
    pivot_len: int = Field(default=5, ge=1)
    max_bars_sweep_mss: int = Field(default=10, ge=1)
    max_bars_mss_fvg: int = Field(default=5, ge=1)
    allow_long: bool = True
    allow_short: bool = True
    # 3) FVG / entry
    min_fvg_ticks: int = Field(default=0, ge=0)
    entry_fill: EntryFill = "50% (Consequent Encroachment)"
    max_bars_to_fill: int = Field(default=20, ge=1)
    # 4) Risk & sizing
    risk_pct: float = Field(default=0.5, ge=0.01)
    risk_base: RiskBase = "Initial Capital"
    fixed_bal: float = Field(default=10000.0, ge=1)
    tick_offset: int = Field(default=1, ge=0)
    round_to_int: bool = True
    # 5) Targets
    tp_mode: TpMode = "Fixed R:R"
    rr: float = Field(default=2.0, ge=0.1)
    min_rr_liq: float = Field(default=1.0, ge=0.1)
    liq_buf: int = Field(default=2, ge=0)
    # 6) Prop-firm guards
    use_daily_dd: bool = True
    max_daily_dd_pct: float = Field(default=2.0, ge=0.1)
    flatten_breach: bool = True
    max_consec: int = Field(default=3, ge=1)
    reset_consec_day: bool = True
    max_tr_window: int = Field(default=1, ge=1)
    max_tr_day: int = Field(default=1, ge=1)
    use_overall_dd: bool = True
    max_overall_pct: float = Field(default=8.0, ge=0.1)
    close_at_day_end: bool = True
    day_end_h: int = Field(default=15, ge=0, le=23)
    day_end_m: int = Field(default=55, ge=0, le=59)
    # 7) Date range (oosStart only drives visuals and the stats table: not ported)
    start_date: dt.datetime = dt.datetime(2024, 1, 1, tzinfo=dt.UTC)
    end_date: dt.datetime = dt.datetime(2030, 1, 1, tzinfo=dt.UTC)
    # 8) Day of week
    trade_mon: bool = True
    trade_tue: bool = True
    trade_wed: bool = True
    trade_thu: bool = True
    trade_fri: bool = True

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


def _ms(moment: dt.datetime) -> int:
    return int(moment.timestamp() * _MS_PER_SECOND)


class IctSilverBullet:
    """`propbot.strategies.base.Strategy` for the Silver Bullet script. One instance per run."""

    def __init__(self, params: IctSilverBulletParams | None = None) -> None:
        p = params or IctSilverBulletParams()
        self.params: Final = p
        self._clock = LocalClock(p.tz)
        self._history = BarHistory(_HISTORY_BARS)
        self._pivot_high = PivotHigh(p.pivot_len, p.pivot_len)
        self._pivot_low = PivotLow(p.pivot_len, p.pivot_len)
        self._am_start = p.am_start_h * 60 + p.am_start_m
        self._am_end = p.am_end_h * 60 + p.am_end_m
        self._pm_start = p.pm_start_h * 60 + p.pm_start_m
        self._pm_end = p.pm_end_h * 60 + p.pm_end_m
        self._flat_min = p.day_end_h * 60 + p.day_end_m
        self._start_ms = _ms(p.start_date)
        self._end_ms = _ms(p.end_date)
        # Pine dayofweek.monday .. friday by Python weekday (Monday = 0); weekend: never.
        self._day_ok = (p.trade_mon, p.trade_tue, p.trade_wed, p.trade_thu, p.trade_fri)

        # -- Pine `var` state --
        self.phase = 0  # 0 idle, 1 swept (awaiting MSS/FVG), 2 armed (limit), 3 in-trade
        self.dir = 0  # 1 long, -1 short for the current hunt
        self.mss_done = False
        self.sweep_bar: int | None = None
        self.mss_bar: int | None = None
        self.sweep_extreme: float | None = None
        self.mss_level: float | None = None
        self.entry_level: float | None = None
        self.stop_level: float | None = None
        self.tp_level: float | None = None
        self.arm_bar: int | None = None
        self.bull_fvg_top: float | None = None
        self.bull_fvg_bot: float | None = None
        self.bull_fvg_bar: int | None = None
        self.bear_fvg_top: float | None = None
        self.bear_fvg_bot: float | None = None
        self.bear_fvg_bar: int | None = None
        self.ph_arr: deque[float] = deque(maxlen=LIQUIDITY_POOL_SIZE)
        self.pl_arr: deque[float] = deque(maxlen=LIQUIDITY_POOL_SIZE)
        self.last_ph: float | None = None
        self.last_pl: float | None = None
        self.day_start_eq: float | None = None
        self.peak_eq: float | None = None
        self.trades_today = 0
        self.trades_window = 0
        self.consec_losses = 0
        self.halted_today = False
        self.halted_perma = False
        self.halt_reason = ""

        # -- previous-bar values of series the script reads with [1] --
        self._window_id_prev = 0  # nz(windowId[1], 0)
        self._dom_prev: int | None = None  # dayofmonth(time, tz)[1]; na on bar 0
        self._trade_count_prev = 0  # nz(opentrades[1] + closedtrades[1], 0)
        self._closed_count_prev = 0  # nz(closedtrades[1], 0)

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
        bar_index = ctx.bar_index
        tick = ctx.instrument.tick_size
        position_size = ctx.position_size
        actions: list[Action] = []

        # -- TIME / DATE / DAY --
        parts = self._clock.parts(bar.ts_open_ms)
        cur_min = parts.minute_of_day
        in_am = p.enable_am and self._am_start <= cur_min < self._am_end
        in_pm = p.enable_pm and self._pm_start <= cur_min < self._pm_end
        in_window = in_am or in_pm
        window_id = 1 if in_am else 2 if in_pm else 0
        new_window = window_id != 0 and window_id != self._window_id_prev
        # Order-placement gate: the NEXT bar must still open inside the same window and this
        # must not be the session's last bar; the same test expires a resting limit.
        bar_min = max(1, int(ctx.tf_seconds / 60))
        win_end = self._am_end if in_am else self._pm_end
        entry_time_ok = in_window and not ctx.session_last_bar and cur_min + bar_min < win_end
        in_date_range = self._start_ms <= bar.ts_open_ms <= self._end_ms
        day_ok = parts.weekday < len(self._day_ok) and self._day_ok[parts.weekday]

        # -- PIVOTS / LIQUIDITY POOLS (ta.pivothigh/low run on every bar) --
        ph = self._pivot_high.update(bar.high)
        pl = self._pivot_low.update(bar.low)
        if ph is not None:
            self.last_ph = ph
            self.ph_arr.append(ph)
        if pl is not None:
            self.last_pl = pl
            self.pl_arr.append(pl)

        # -- DAILY / WINDOW RESETS --
        if self.day_start_eq is None:
            self.day_start_eq = ctx.equity
        if self.peak_eq is None:
            self.peak_eq = ctx.equity

        # ta.change(dayofmonth) is na on the first bar and `na != 0` is false: no new day.
        new_day = self._dom_prev is not None and parts.day != self._dom_prev
        self._dom_prev = parts.day
        if new_day:
            self.day_start_eq = ctx.equity
            self.trades_today = 0
            self.halted_today = False
            self.halt_reason = self.halt_reason if self.halted_perma else ""
            if p.reset_consec_day:
                self.consec_losses = 0
            if position_size == 0:
                # Pine finding (reported): a resting limit is not cancelled here.
                self._reset_hunt()

        if new_window:
            self.trades_window = 0
            self.bear_fvg_bar = None  # do not arm off a stale gap from a previous window
            self.bull_fvg_bar = None
            if position_size == 0 and self.phase != 3:
                self._reset_hunt()

        # -- FVG TRACKERS (in-window) --
        if in_window:
            min_size = p.min_fvg_ticks * tick
            high2 = h.high(2)
            low2 = h.low(2)
            if high2 is not None and bar.low > high2 and (bar.low - high2) >= min_size:
                self.bull_fvg_top = bar.low
                self.bull_fvg_bot = high2
                self.bull_fvg_bar = bar_index
            if low2 is not None and bar.high < low2 and (low2 - bar.high) >= min_size:
                self.bear_fvg_top = low2
                self.bear_fvg_bot = bar.high
                self.bear_fvg_bar = bar_index

        # -- ELIGIBILITY --
        eligible = (
            entry_time_ok
            and in_date_range
            and day_ok
            and not self.halted_today
            and not self.halted_perma
            and position_size == 0
        )
        start_new_hunt = (
            eligible
            and self.phase == 0
            and self.trades_window < p.max_tr_window
            and self.trades_today < p.max_tr_day
        )

        # -- 1) SWEEP -> SET BIAS (short first) --
        if start_new_hunt and self.last_ph is not None and self.last_pl is not None:
            if p.allow_short and bar.high > self.last_ph:
                self._start_hunt(-1, bar_index, bar.high, self.last_pl)
            elif p.allow_long and bar.low < self.last_pl:
                self._start_hunt(1, bar_index, bar.low, self.last_ph)

        # -- 2) MSS + 3) FVG -> ARM LIMIT --
        if self.phase == 1 and in_window:
            self._phase_one(bar, ctx, entry_time_ok, actions)

        # Count-based fill detection: also catches a limit entry that fills AND exits on the
        # same bar; computed before the cancel block so such a bar is not taken as unfilled.
        trade_count = len(ctx.open_trades) + len(ctx.closed_trades)
        just_entered = trade_count > self._trade_count_prev
        self._trade_count_prev = trade_count

        # -- 4) CANCEL / INVALIDATE (armed, unfilled) --
        if self.phase == 2 and position_size == 0 and not just_entered:
            stop = self.stop_level
            broke_stop = stop is not None and (
                (self.dir == -1 and bar.close > stop) or (self.dir == 1 and bar.close < stop)
            )
            expired = not entry_time_ok or (
                self.arm_bar is not None and bar_index - self.arm_bar > p.max_bars_to_fill
            )
            if broke_stop or expired:
                actions += [
                    Cancel(LONG_ID),
                    Cancel(SHORT_ID),
                    Cancel(EXIT_LONG_ID),  # flat here: only the pending bracket goes
                    Cancel(EXIT_SHORT_ID),
                ]
                self._reset_hunt()

        # -- 5) FILL DETECTION --
        if just_entered:
            self.phase = 3
            self.trades_window += 1
            self.trades_today += 1

        # -- TRADE MANAGEMENT (fixed stop + target, re-issued every bar like Pine) --
        if position_size > 0:
            actions += self._fill_bracket(Side.LONG)
        if position_size < 0:
            actions += self._fill_bracket(Side.SHORT)

        # -- CLOSE DETECTION -> consecutive-loss guard --
        closed_count = len(ctx.closed_trades)
        just_closed = closed_count != self._closed_count_prev
        self._closed_count_prev = closed_count
        if just_closed and closed_count > 0:
            last_profit = ctx.closed_trades[closed_count - 1].profit
            if last_profit < 0:
                self.consec_losses += 1
            elif last_profit > 0:
                self.consec_losses = 0
            if self.consec_losses >= p.max_consec:
                self.halted_today = True
                self.halt_reason = "Max consec losses"

        # Reset the hunt once flat after a trade.
        if position_size == 0 and self.phase == 3:
            self._reset_hunt()

        # -- DRAWDOWN GUARDS --
        equity = ctx.equity
        day_start_eq = self.day_start_eq
        daily_pnl = equity - day_start_eq
        daily_pnl_pct = daily_pnl / day_start_eq * 100.0 if day_start_eq > 0 else 0.0
        # One forced close per bar, filled at THIS bar's close.
        flat_sent = False

        if p.use_daily_dd and not self.halted_today and daily_pnl_pct <= -p.max_daily_dd_pct:
            self.halted_today = True
            self.halt_reason = "Daily DD limit"
            if p.flatten_breach and position_size != 0:
                actions.append(CloseAll(REASON_DAILY_DD))
                flat_sent = True
            actions += [Cancel(LONG_ID), Cancel(SHORT_ID)]
            # strategy.position_size is the bar-close value: close_all(immediately) does not
            # change it inside this execution, so an open position keeps its bracket here.
            if position_size == 0:  # never pull the stop of an open position
                actions += [Cancel(EXIT_LONG_ID), Cancel(EXIT_SHORT_ID)]
            if self.phase != 3:
                self.phase = 0

        self.peak_eq = max(self.peak_eq, equity)
        dd_peak_pct = (equity - self.peak_eq) / self.peak_eq * 100.0 if self.peak_eq > 0 else 0.0
        if p.use_overall_dd and not self.halted_perma and dd_peak_pct <= -p.max_overall_pct:
            self.halted_perma = True
            self.halt_reason = "Overall DD limit"
            if p.flatten_breach and position_size != 0 and not flat_sent:
                actions.append(CloseAll(REASON_OVERALL_DD))
                flat_sent = True
            actions += [Cancel(LONG_ID), Cancel(SHORT_ID)]
            if position_size == 0:
                actions += [Cancel(EXIT_LONG_ID), Cancel(EXIT_SHORT_ID)]

        # -- INTRADAY / END-OF-RANGE FLATTEN (one reason per bar, at this bar's close) --
        if p.close_at_day_end and (cur_min >= self._flat_min or ctx.session_last_bar):
            flat_reason = REASON_EOD
        elif bar.ts_open_ms > self._end_ms:
            flat_reason = REASON_END_OF_RANGE
        else:
            flat_reason = ""
        if flat_reason and position_size != 0 and not flat_sent:
            actions.append(CloseAll(flat_reason))

        self._window_id_prev = window_id
        return actions

    def on_fill(self, fill: FillEvent, ctx: StrategyContext) -> Sequence[Action]:
        """Pessimistic fill model: the same re-pricing the script does at the fill bar's close."""
        return self._fill_bracket(fill.side)

    # -- the script's blocks ------------------------------------------------------------

    def _fill_bracket(self, side: Side) -> list[Action]:
        """The bracket the script (re-)issues once its entry has filled (SPEC §3.1).

        Silver Bullet re-prices nothing at the fill: the stop is structural (sweep extreme
        ± `tick_offset`) and the target was priced from the planned limit level, so the
        fill price moves neither (`strategy.exit(..., stop = stopLevel, limit = tpLevel)`).
        Called from `on_fill` (pessimistic) and from `on_bar` while a position is open
        (tv_ohlc: the script's own timing), so both paths give the same levels.
        """
        if self.stop_level is None or self.tp_level is None:
            # Unreachable: a position only comes from an armed entry. Returning no stop lets
            # the engine refuse the run (fail closed) instead of guessing a level.
            return []
        if side is Side.LONG:
            return [Exit(EXIT_LONG_ID, LONG_ID, stop=self.stop_level, limit=self.tp_level)]
        return [Exit(EXIT_SHORT_ID, SHORT_ID, stop=self.stop_level, limit=self.tp_level)]

    def _reset_hunt(self) -> None:
        self.phase = 0
        self.dir = 0
        self.mss_done = False

    def _start_hunt(self, direction: int, bar_index: int, extreme: float, mss: float) -> None:
        self.phase = 1
        self.dir = direction
        self.sweep_bar = bar_index
        self.sweep_extreme = extreme
        self.mss_level = mss
        self.mss_done = False

    def _phase_one(
        self, bar: Bar, ctx: StrategyContext, entry_time_ok: bool, actions: list[Action]
    ) -> None:
        """Pine block "2) MSS + 3) FVG -> ARM LIMIT" (phase 1, in-window bars only)."""
        p = self.params
        bar_index = ctx.bar_index
        sweep_bar = self.sweep_bar
        sweep_extreme = self.sweep_extreme
        mss_level = self.mss_level
        if sweep_bar is None or sweep_extreme is None or mss_level is None:
            return  # unreachable: phase 1 is only entered through _start_hunt
        # Track the running sweep extreme so the stop sits beyond the furthest poke.
        if self.dir == -1:
            sweep_extreme = max(sweep_extreme, bar.high)
        else:
            sweep_extreme = min(sweep_extreme, bar.low)
        self.sweep_extreme = sweep_extreme

        if not self.mss_done and bar_index - sweep_bar > p.max_bars_sweep_mss:
            # Void if the MSS never arrives in time (mssDone stays false already).
            self.phase = 0
            self.dir = 0
            return
        # Confirm MSS (close-based break of the opposing pivot); possible on the sweep bar.
        if not self.mss_done and (
            (self.dir == -1 and bar.close < mss_level) or (self.dir == 1 and bar.close > mss_level)
        ):
            self.mss_done = True
            self.mss_bar = bar_index
        if not (self.mss_done and self.phase == 1) or self.mss_bar is None:
            return
        if bar_index - self.mss_bar > p.max_bars_mss_fvg:
            self._reset_hunt()  # displacement FVG never appeared -> void
            return
        if self.dir == -1:
            fvg_bar, f_top, f_bot = self.bear_fvg_bar, self.bear_fvg_top, self.bear_fvg_bot
        else:
            fvg_bar, f_top, f_bot = self.bull_fvg_bar, self.bull_fvg_top, self.bull_fvg_bot
        fvg_ok = entry_time_ok and fvg_bar is not None and fvg_bar >= sweep_bar
        if not fvg_ok or f_top is None or f_bot is None:
            return
        tick = ctx.instrument.tick_size
        if p.entry_fill == "Distal (full gap fill)":
            ent = f_top if self.dir == -1 else f_bot
        elif p.entry_fill == "Proximal (first touch)":
            ent = f_bot if self.dir == -1 else f_top
        else:
            ent = (f_top + f_bot) / 2.0
        if self.dir == -1:
            stp = sweep_extreme + p.tick_offset * tick
            rsk = stp - ent
        else:
            stp = sweep_extreme - p.tick_offset * tick
            rsk = ent - stp
        if not rsk > 0:
            return
        qty = ctx.instrument.floor_qty(self._f_qty(ent, stp, ctx))
        if not qty > 0:
            return  # too small for the risk: no arming, the hunt keeps looking (as Pine)
        self.entry_level = ent
        self.stop_level = stp
        self.tp_level = self._f_target(self.dir, ent, rsk, tick)
        self.arm_bar = bar_index
        # The bracket goes in with the limit entry: it only activates once the entry fills,
        # so the fill bar is protected; it is cancelled together with the entry.
        if self.dir == -1:
            actions += [
                Entry(SHORT_ID, Side.SHORT, qty, limit=ent),
                Exit(EXIT_SHORT_ID, SHORT_ID, stop=stp, limit=self.tp_level),
            ]
        else:
            actions += [
                Entry(LONG_ID, Side.LONG, qty, limit=ent),
                Exit(EXIT_LONG_ID, LONG_ID, stop=stp, limit=self.tp_level),
            ]
        self.phase = 2

    def _f_qty(self, entry: float, stop: float, ctx: StrategyContext) -> float:
        """Pine `f_qty`: true fixed-fractional size from the risk base and the stop distance."""
        p = self.params
        if p.risk_base == "Fixed Balance":
            risk_base_amt = p.fixed_bal
        elif p.risk_base == "Account Equity":
            risk_base_amt = ctx.equity
        else:
            risk_base_amt = ctx.initial_capital
        risk_per_contract = abs(entry - stop) * ctx.instrument.point_value
        cap = risk_base_amt * p.risk_pct / 100.0
        raw = cap / risk_per_contract if risk_per_contract > 0 else 0.0
        return float(math.floor(raw)) if p.round_to_int else raw

    def _f_target(self, direction: int, entry: float, risk: float, tick: float) -> float:
        """Pine `f_target`: fixed R:R, or the nearest opposing pool with the min-R fallback."""
        p = self.params
        tp = entry + p.rr * risk if direction == 1 else entry - p.rr * risk
        if p.tp_mode != "Next opposing liquidity":
            return tp
        if direction == 1 and self.ph_arr:
            above = [v for v in self.ph_arr if v > entry]  # nearest high ABOVE entry
            if above:
                cand = min(above) - p.liq_buf * tick
                if cand - entry >= p.min_rr_liq * risk:
                    tp = cand
        elif direction == -1 and self.pl_arr:
            below = [v for v in self.pl_arr if v < entry]  # nearest low BELOW entry
            if below:
                cand = max(below) + p.liq_buf * tick
                if entry - cand >= p.min_rr_liq * risk:
                    tp = cand
        return tp
