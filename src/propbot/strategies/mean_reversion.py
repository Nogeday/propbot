"""Mean Reversion BB/KC + RSI(2) fade: port of `pine/mean_reversion_bb_kc_rsi2_strategy.pine`.

SPEC §3.4.3. A pure, deterministic, bar-based state machine (`propbot.strategies.base`):
it reads no clock, file or environment. `on_bar` mirrors the script's blocks in their
order: indicators -> time / date / day -> daily loss breaker -> entry conditions ->
trade management -> forced exits -> reset when flat -> entry. The state variables keep
the Pine names in snake_case so the two can be read side by side.

Not ported: plots, background colours, the OOS marker, the weekday stats table (computed
by `propbot.backtest.analytics`) and the alert() JSON (Faz 1 forwards TradingView's own
alerts).
"""

import datetime as dt
import math
from collections.abc import Sequence
from typing import Final, Literal
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from pydantic import Field, field_validator

from propbot.core.bars import Bar
from propbot.core.instrument import InstrumentKind
from propbot.core.session import NEW_YORK, LocalClock
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
from propbot.strategies.indicators import Atr, Ema, Rsi, Sma, Stdev

STRATEGY_ID: Final = "mr_bb_kc_rsi2"
PINE_FILE: Final = "mean_reversion_bb_kc_rsi2_strategy.pine"
# strategy(pyramiding = 0)
PYRAMIDING: Final = 0

# Order ids and comments exactly as in the script's strategy.entry / strategy.exit calls.
LONG_ID: Final = "Long"
SHORT_ID: Final = "Short"
EXIT_LONG_ID: Final = "Exit L"
EXIT_SHORT_ID: Final = "Exit S"
LONG_COMMENT: Final = "MR Long"
SHORT_COMMENT: Final = "MR Short"
# flatReason values of the FORCED EXITS block.
REASON_SESSION_END: Final = "Session end"
REASON_DAILY_HALT: Final = "Daily loss halt"
REASON_END_OF_RANGE: Final = "End of range"

type RiskBase = Literal["Initial Capital", "Account Equity", "Fixed Balance"]
type QtyRound = Literal["Auto", "Whole units", "Fractional"]

_MS_PER_SECOND: Final = 1000
_SECONDS_PER_MINUTE: Final = 60


class MeanReversionParams(StrictModel):
    """One field per order-affecting Pine input; defaults are the script's defaults."""

    # Bands & indicators
    bb_len: int = Field(default=20, ge=1)
    bb_mult: float = Field(default=2.5, ge=0.1)
    kc_len: int = Field(default=20, ge=1)
    kc_mult: float = Field(default=1.5, ge=0.1)
    rsi_len: int = Field(default=2, ge=1)
    rsi_ob: float = Field(default=90.0, ge=50, le=100)
    rsi_os: float = Field(default=10.0, ge=0, le=50)
    ema_len: int = Field(default=200, ge=1)
    atr_len: int = Field(default=14, ge=1)
    atr_mult: float = Field(default=2.0, ge=0.1)
    # Filters
    use_trend_filter: bool = True
    use_kc_filter: bool = True
    require_strong: bool = False
    allow_long: bool = True
    allow_short: bool = True
    # Risk management
    risk_pct: float = Field(default=0.5, ge=0.01)
    risk_base: RiskBase = "Initial Capital"
    fixed_bal: float = Field(default=10000.0, ge=1)
    max_daily_loss: float = Field(default=2.0, ge=0.1)
    close_on_halt: bool = True
    max_trades_day: int = Field(default=1, ge=1)
    qty_round: QtyRound = "Auto"
    # Time filter
    tz: str = NEW_YORK
    use_time_filter: bool = True
    start_hour: int = Field(default=10, ge=0, le=23)
    start_min: int = Field(default=0, ge=0, le=59)
    end_hour: int = Field(default=15, ge=0, le=23)
    end_min: int = Field(default=45, ge=0, le=59)
    close_at_end: bool = True
    # Day of week filter
    trade_mon: bool = True
    trade_tue: bool = True
    trade_wed: bool = True
    trade_thu: bool = True
    trade_fri: bool = True
    # Backtest date range. Pine sets no bounds on the years; Python dates need 1..9999.
    start_year: int = Field(default=2023, ge=1, le=9999)
    start_month: int = Field(default=1, ge=1, le=12)
    start_day: int = Field(default=1, ge=1, le=31)
    end_year: int = Field(default=2030, ge=1, le=9999)
    end_month: int = Field(default=12, ge=1, le=12)
    end_day: int = Field(default=31, ge=1, le=31)

    @field_validator("tz")
    @classmethod
    def _known_zone(cls, value: str) -> str:
        try:
            ZoneInfo(value)
        except (ZoneInfoNotFoundError, ValueError) as exc:
            raise ValueError(f"bilinmeyen saat dilimi: {value!r}") from exc
        return value


def _pine_timestamp_ms(
    zone: ZoneInfo, year: int, month: int, day: int, hour: int, minute: int
) -> int:
    """`timestamp(tz, year, month, day, hour, minute)`.

    A day past the month's end rolls into the next month (31 June -> 1 July) instead of
    raising, which is how the script's "End day = 31" default works for every month.
    """
    local = dt.datetime(year, month, 1, hour, minute) + dt.timedelta(days=day - 1)
    return int(local.replace(tzinfo=zone).timestamp() * _MS_PER_SECOND)


class MeanReversion:
    """`strategy("Mean Reversion BB/KC + RSI(2) Fade", pyramiding = 0, ...)`."""

    def __init__(self, params: MeanReversionParams | None = None) -> None:
        p = params if params is not None else MeanReversionParams()
        self.params: Final = p
        self._clock = LocalClock(p.tz)
        zone = ZoneInfo(p.tz)
        # BACKTEST DATE RANGE: startTime / endTime
        self._start_time = _pine_timestamp_ms(zone, p.start_year, p.start_month, p.start_day, 0, 0)
        self._end_time = _pine_timestamp_ms(zone, p.end_year, p.end_month, p.end_day, 23, 59)
        self._s_min = p.start_hour * 60 + p.start_min
        self._e_min = p.end_hour * 60 + p.end_min
        self._trade_day = (p.trade_mon, p.trade_tue, p.trade_wed, p.trade_thu, p.trade_fri)

        # INDICATORS
        self._sma_bb = Sma(p.bb_len)
        self._stdev_bb = Stdev(p.bb_len)
        self._ema_kc = Ema(p.kc_len)
        self._atr_kc = Atr(p.kc_len)
        self._rsi = Rsi(p.rsi_len)
        self._ema_tr = Ema(p.ema_len)
        self._atr = Atr(p.atr_len)
        # The latest `basis` (Pine na -> None); on_fill re-prices the target from it.
        self._basis: float | None = None

        # ta.change(dayofmonth(time, tz)): the previous bar's day of month.
        self._prev_dom: int | None = None
        # longSetup[1] / shortSetup[1]
        self._prev_long_setup = False
        self._prev_short_setup = False

        # var state of the DAILY LOSS CIRCUIT BREAKER and the trade counter
        self.day_start_equity: float | None = None
        self.daily_halt = False
        self.trades_today = 0
        # var state of OPEN-TRADE STATE
        self.in_dir = 0
        self.in_stop: float | None = None
        self.atr_stored: float | None = None

    @property
    def strategy_id(self) -> str:
        return STRATEGY_ID

    @property
    def pyramiding(self) -> int:
        return PYRAMIDING

    def on_bar(self, bar: Bar, ctx: StrategyContext) -> Sequence[Action]:
        p = self.params
        tick = ctx.instrument.tick_size
        actions: list[Action] = []

        # ── INDICATORS (every bar) ──
        basis = self._sma_bb.update(bar.close)
        stdev = self._stdev_bb.update(bar.close)
        kc_basis = self._ema_kc.update(bar.close)
        kc_range = self._atr_kc.update(bar.high, bar.low, bar.close)
        rsi2 = self._rsi.update(bar.close)
        ema_tr = self._ema_tr.update(bar.close)
        atr_val = self._atr.update(bar.high, bar.low, bar.close)
        self._basis = basis

        bb_upper: float | None = None
        bb_lower: float | None = None
        if basis is not None and stdev is not None:
            bb_dev = p.bb_mult * stdev
            bb_upper = basis + bb_dev
            bb_lower = basis - bb_dev
        kc_upper: float | None = None
        kc_lower: float | None = None
        if kc_basis is not None and kc_range is not None:
            kc_upper = kc_basis + p.kc_mult * kc_range
            kc_lower = kc_basis - p.kc_mult * kc_range

        # isSqueeze: a comparison with na is false in Pine, so an na band is "not a squeeze".
        is_squeeze = (
            bb_upper is not None
            and bb_lower is not None
            and kc_upper is not None
            and kc_lower is not None
            and bb_upper < kc_upper
            and bb_lower > kc_lower
        )
        vol_expansion = not is_squeeze
        kc_ok = not p.use_kc_filter or vol_expansion

        # ── TIME / DATE / DAY ──
        parts = self._clock.parts(bar.ts_open_ms)
        cur_min = parts.minute_of_day
        in_window = self._s_min <= cur_min < self._e_min
        sess_ok = not p.use_time_filter or in_window
        at_or_past_end = p.use_time_filter and cur_min >= self._e_min
        bar_min = max(1, int(ctx.tf_seconds / _SECONDS_PER_MINUTE))
        entry_time_ok = not ctx.session_last_bar and (
            not p.use_time_filter or cur_min + bar_min < self._e_min
        )
        time = bar.ts_open_ms
        in_date_range = self._start_time <= time <= self._end_time
        # ta.change(...) is na on the first bar and `na != 0` is false: no new day there.
        is_new_day = self._prev_dom is not None and parts.day != self._prev_dom
        self._prev_dom = parts.day
        # dayofweek Monday..Friday only; Saturday / Sunday never pass.
        day_ok = parts.weekday < len(self._trade_day) and self._trade_day[parts.weekday]

        # ── DAILY LOSS CIRCUIT BREAKER ──
        dse = self.day_start_equity
        if is_new_day or dse is None:
            dse = ctx.equity
            self.day_start_equity = dse
        day_dd_pct = 0.0 if dse <= 0 else (dse - ctx.equity) / dse * 100.0
        if is_new_day:
            self.daily_halt = False
        if day_dd_pct >= p.max_daily_loss:
            self.daily_halt = True
        if is_new_day:
            self.trades_today = 0

        # ── ENTRY CONDITIONS ──
        close = bar.close
        trend_ok_long = not p.use_trend_filter or (ema_tr is not None and close >= ema_tr)
        trend_ok_short = not p.use_trend_filter or (ema_tr is not None and close <= ema_tr)
        rng = max(bar.high - bar.low, tick)
        strong_long = close > bar.open and (close - bar.low) / rng >= 0.5
        strong_short = close < bar.open and (bar.high - close) / rng >= 0.5
        conf_long_ok = not p.require_strong or strong_long
        conf_short_ok = not p.require_strong or strong_short

        long_setup = (
            p.allow_long
            and bb_lower is not None
            and close < bb_lower
            and rsi2 is not None
            and rsi2 < p.rsi_os
            and kc_ok
        )
        short_setup = (
            p.allow_short
            and bb_upper is not None
            and close > bb_upper
            and rsi2 is not None
            and rsi2 > p.rsi_ob
            and kc_ok
        )
        # The confirmation must be the very next bar and uses its own bands and EMA.
        long_confirm = (
            self._prev_long_setup
            and bb_lower is not None
            and close > bb_lower
            and trend_ok_long
            and conf_long_ok
        )
        short_confirm = (
            self._prev_short_setup
            and bb_upper is not None
            and close < bb_upper
            and trend_ok_short
            and conf_short_ok
        )
        self._prev_long_setup = long_setup
        self._prev_short_setup = short_setup

        trading_allowed = (
            in_date_range
            and sess_ok
            and entry_time_ok
            and day_ok
            and not self.daily_halt
            and self.trades_today < p.max_trades_day
            and ctx.position_size == 0
        )
        go_long = trading_allowed and long_confirm
        go_short = trading_allowed and short_confirm

        # ── TRADE MANAGEMENT (stop + dynamic target) ──
        avg = ctx.position_avg_price
        if ctx.position_size != 0 and avg is not None:
            sign = 1 if ctx.position_size > 0 else -1
            if self.in_stop is None:
                # tv_ohlc: the fill bar's close is where Pine finalizes the stop.
                self._finalize_stop(avg, sign)
            if self.in_dir == sign and self.in_stop is not None:
                actions.append(self._exit(sign, self.in_stop, self._target(basis, avg, sign, tick)))

        # ── FORCED EXITS ── one reason per bar, filled at this bar's close
        if p.close_at_end and (at_or_past_end or ctx.session_last_bar):
            flat_reason = REASON_SESSION_END
        elif p.close_on_halt and self.daily_halt:
            flat_reason = REASON_DAILY_HALT
        elif time > self._end_time:
            flat_reason = REASON_END_OF_RANGE
        else:
            flat_reason = ""
        if flat_reason and ctx.position_size != 0:
            actions.append(CloseAll(comment=flat_reason))

        # ── RESET STATE WHEN FLAT ── strategy.position_size is the bar-close value: a
        # close_all above shows up on the next bar, which resets there before any entry.
        if ctx.position_size == 0 and self.in_dir != 0:
            self.in_dir = 0
            self.in_stop = None
            self.atr_stored = None

        # ── ENTRY ── submitted at the confirmation close, fills at the next open
        if go_long:
            actions.extend(self._entry(Side.LONG, close, basis, atr_val, ctx))
        if go_short:
            actions.extend(self._entry(Side.SHORT, close, basis, atr_val, ctx))
        return actions

    def on_fill(self, fill: FillEvent, ctx: StrategyContext) -> Sequence[Action]:
        """Pessimistic model: finalize the stop at the fill instead of at the fill bar's close.

        The stop is the same number the fill bar's close would give (`_finalize_stop`); the
        target uses the latest known basis (the signal bar's), clamped against the fill.
        The fill bar's `on_bar` then re-issues the bracket with its own basis, as Pine does.
        """
        if fill.entry_id not in (LONG_ID, SHORT_ID) or self.in_stop is not None:
            return ()
        sign = fill.side.sign
        self._finalize_stop(fill.price, sign)
        if self.in_stop is None:
            return ()
        tick = ctx.instrument.tick_size
        return (self._exit(sign, self.in_stop, self._target(self._basis, fill.price, sign, tick)),)

    # -- the script's helpers ------------------------------------------------------------

    def _finalize_stop(self, avg_price: float, sign: int) -> None:
        """`inStop := strategy.position_avg_price -+ atrMult * atrStored` (the fixed stop).

        The single place of the fill-bar arithmetic: on_fill (pessimistic) and the fill
        bar's on_bar (tv_ohlc) both call it, so the same fill gives the same stop.
        """
        if self.atr_stored is None:
            return  # never happens: an entry is only sent after atrStored is set
        self.in_dir = sign
        self.in_stop = avg_price - sign * self.params.atr_mult * self.atr_stored

    @staticmethod
    def _target(basis: float | None, avg_price: float, sign: int, tick: float) -> float | None:
        """tpL = max(basis, avg + mintick) / tpS = min(basis, avg - mintick).

        Pine's math.max / math.min return na when the basis is na; the exit then has no limit.
        """
        if basis is None:
            return None
        if sign > 0:
            return max(basis, avg_price + tick)
        return min(basis, avg_price - tick)

    @staticmethod
    def _exit(sign: int, stop: float, limit: float | None) -> Exit:
        if sign > 0:
            return Exit(EXIT_LONG_ID, LONG_ID, stop=stop, limit=limit)
        return Exit(EXIT_SHORT_ID, SHORT_ID, stop=stop, limit=limit)

    def _qty_from_dist(self, dist_points: float | None, ctx: StrategyContext) -> float:
        """`f_qtyFromDist`, then rounded down to the instrument's size step."""
        p = self.params
        if p.risk_base == "Fixed Balance":
            risk_base_amt = p.fixed_bal
        elif p.risk_base == "Account Equity":
            risk_base_amt = ctx.equity
        else:
            risk_base_amt = ctx.initial_capital
        risk_capital = risk_base_amt * (p.risk_pct / 100.0)
        # `_perUnit > 0` is false for na: no size.
        if dist_points is None:
            return 0.0
        per_unit = dist_points * ctx.instrument.point_value
        raw = risk_capital / per_unit if per_unit > 0 else 0.0
        whole = p.qty_round == "Whole units" or (
            p.qty_round == "Auto" and ctx.instrument.kind is InstrumentKind.FUTURES
        )
        qty = float(math.floor(raw)) if whole else raw
        return ctx.instrument.floor_qty(qty)

    def _entry(
        self,
        side: Side,
        close: float,
        basis: float | None,
        atr_val: float | None,
        ctx: StrategyContext,
    ) -> list[Action]:
        p = self.params
        dist = None if atr_val is None else p.atr_mult * atr_val
        qty = self._qty_from_dist(dist, ctx)
        if not qty > 0 or atr_val is None or dist is None:
            return []  # the script skips the trade and leaves its state untouched
        self.atr_stored = atr_val
        sign = side.sign
        tick = ctx.instrument.tick_size
        est_stop = close - sign * dist
        est_tp = self._target(basis, close, sign, tick)
        self.trades_today += 1
        if side is Side.LONG:
            entry = Entry(LONG_ID, Side.LONG, qty, comment=LONG_COMMENT)
        else:
            entry = Entry(SHORT_ID, Side.SHORT, qty, comment=SHORT_COMMENT)
        return [entry, self._exit(sign, est_stop, est_tp)]
