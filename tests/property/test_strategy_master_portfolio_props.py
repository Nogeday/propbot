"""Properties of the Master portfolio port over random walks and the committed synthetic fixtures.

The random series use compact windows so that one block of 1-minute bars per day (08:55-09:59
New York, then a gap to the next day) holds both engines:

    S2  Asian [09:00, 09:05), active [09:05, 09:50)
    S1  OR [09:30, 09:32), session end 09:50

The fixtures run with the script's defaults on an MES-like instrument.
"""

import datetime as dt
import math
from collections.abc import Sequence
from typing import Any
from zoneinfo import ZoneInfo

import pytest
from hypothesis import given, settings
from hypothesis import strategies as st

from propbot.backtest.engine import run_backtest
from propbot.backtest.model import EngineResult, FillModel
from propbot.core.bars import Bar, BarSeries, DataSource
from propbot.core.session import LocalClock
from propbot.data.ohlcv import load_ohlcv
from propbot.data.sessions import session_last_flags
from propbot.strategies.base import Action, Entry, Exit, FillEvent, StrategyContext
from propbot.strategies.master_portfolio import LEG_IDS, MasterPortfolio, MasterPortfolioParams
from support.engine import make_config, mes
from support.paths import REPO_ROOT

NY = ZoneInfo("America/New_York")
TICK = 0.25
POINT_VALUE = 5.0
FIXTURES = REPO_ROOT / "tests" / "fixtures" / "ohlcv"
S1_LEG, S2_LEG = LEG_IDS

COMPACT: dict[str, Any] = {
    "s1_or_minutes": 2,
    "s1_sess_end_hour": 9,
    "s1_sess_end_min": 50,
    "s2_asian_start_hour": 9,
    "s2_asian_start_min": 0,
    "s2_asian_end_hour": 9,
    "s2_asian_end_min": 5,
    "s2_active_start_hour": 9,
    "s2_active_start_min": 5,
    "s2_active_end_hour": 9,
    "s2_active_end_min": 50,
}
DAY_BARS = 65  # 08:55 .. 09:59
FIRST_DAY = dt.datetime(2026, 3, 2, 8, 55, tzinfo=NY)  # Monday


class Recorder:
    """Keeps every signal: the bar, the Entry, the leg's bracket in the same batch, the context."""

    def __init__(self, inner: MasterPortfolio) -> None:
        self.inner = inner
        self.signals: list[tuple[int, Entry, Exit, StrategyContext, float]] = []
        self.max_open = 0
        # (leg id, fill bar) -> (fill price, the stop the fill-bar re-pricing set): read right
        # after the call that re-prices (on_fill in the pessimistic model, the fill bar's
        # on_bar in tv_ohlc), before any later bar can trail it.
        self.repriced: dict[tuple[str, int], tuple[float, float]] = {}
        self._priced = {S1_LEG: False, S2_LEG: False}

    @property
    def strategy_id(self) -> str:
        return self.inner.strategy_id

    @property
    def pyramiding(self) -> int:
        return self.inner.pyramiding

    def _note_repricing(self, bar_index: int) -> None:
        legs = (
            (S1_LEG, self.inner.s1_fill_price, self.inner.s1_in_initial_stop),
            (S2_LEG, self.inner.s2_fill_price, self.inner.s2_in_initial_stop),
        )
        for leg, fill_price, stop in legs:
            if fill_price is None:
                self._priced[leg] = False
            elif not self._priced[leg]:
                self._priced[leg] = True
                assert stop is not None
                self.repriced[(leg, bar_index)] = (fill_price, stop)

    def on_bar(self, bar: Bar, ctx: StrategyContext) -> Sequence[Action]:
        out = list(self.inner.on_bar(bar, ctx))
        self._note_repricing(ctx.bar_index)
        legs = [t.entry_id[:2] for t in ctx.open_trades]
        assert len(legs) == len(set(legs)), legs  # at most one open trade per leg
        self.max_open = max(self.max_open, len(legs))
        for entry in (a for a in out if isinstance(a, Entry)):
            brackets = [a for a in out if isinstance(a, Exit) and a.from_entry == entry.id]
            assert len(brackets) == 1, out  # invariant 2: the stop goes with the entry
            assert brackets[0].stop is not None
            self.signals.append((ctx.bar_index, entry, brackets[0], ctx, bar.close))
        return out

    def on_fill(self, fill: FillEvent, ctx: StrategyContext) -> Sequence[Action]:
        out = list(self.inner.on_fill(fill, ctx))
        self._note_repricing(fill.bar_index)
        assert all(isinstance(a, Exit) and a.from_entry == fill.entry_id for a in out), out
        return out


def run(
    series: BarSeries, flags: Sequence[bool], params: MasterPortfolioParams, fill_model: FillModel
) -> tuple[EngineResult, Recorder]:
    recorder = Recorder(MasterPortfolio(params))
    config = make_config(
        instrument=mes(tick_size=TICK, point_value=POINT_VALUE), fill_model=fill_model
    )
    return run_backtest(series, recorder, config, session_last=flags), recorder


def _window(minute: int, start: int, end: int) -> bool:
    # Written out again here, not imported: the test must not trust the port's own helper.
    return start <= minute < end if end > start else (minute >= start or minute < end)


def check_invariants(
    series: BarSeries,
    flags: Sequence[bool],
    params: MasterPortfolioParams,
    result: EngineResult,
    recorder: Recorder,
    fill_model: FillModel,
) -> None:
    clock = LocalClock(params.tz)
    local = [clock.parts(bar.ts_open_ms) for bar in series.bars]
    minutes = [parts.minute_of_day for parts in local]
    sess_end = params.s1_sess_end_hour * 60 + params.s1_sess_end_min
    a_start = params.s2_asian_start_hour * 60 + params.s2_asian_start_min
    a_end = params.s2_asian_end_hour * 60 + params.s2_asian_end_min
    x_start = params.s2_active_start_hour * 60 + params.s2_active_start_min
    x_end = params.s2_active_end_hour * 60 + params.s2_active_end_min
    asian = [_window(m, a_start, a_end) for m in minutes]
    active = [_window(m, x_start, x_end) for m in minutes]

    assert recorder.max_open <= 2
    assert {trade.strategy_id for trade in result.trades} <= set(LEG_IDS)
    for trade in result.trades:
        assert trade.entry_id.startswith("S1" if trade.strategy_id == S1_LEG else "S2")

    # One trade per engine per its own day: S1 the New York date, S2 the Asian cycle.
    cycle: list[int] = []
    current = 0
    for i, now in enumerate(asian):
        if now and not (asian[i - 1] if i > 0 else False):
            current += 1
        cycle.append(current)
    s1_days: dict[dt.date, int] = {}
    s2_cycles: dict[int, int] = {}
    for bar_index, entry, bracket, ctx, close in recorder.signals:
        if entry.strategy_id == S1_LEG:
            day = local[bar_index].date
            s1_days[day] = s1_days.get(day, 0) + 1
        else:
            assert entry.strategy_id == S2_LEG
            s2_cycles[cycle[bar_index]] = s2_cycles.get(cycle[bar_index], 0) + 1
        # Risk at the signal never exceeds risk % of the risk base.
        if params.risk_base == "Initial Capital":
            base = ctx.initial_capital
        elif params.risk_base == "Account Equity":
            base = ctx.equity
        else:
            base = params.fixed_bal
        assert bracket.stop is not None
        risk = abs(close - bracket.stop) * entry.qty * POINT_VALUE
        assert risk <= base * params.risk_pct / 100.0 * (1 + 1e-9)
        assert entry.qty >= 1.0
        assert entry.qty == math.floor(entry.qty)
    assert all(count <= 1 for count in s1_days.values()), s1_days
    assert all(count <= 1 for count in s2_cycles.values()), s2_cycles

    s1_forced = [m >= sess_end or flags[i] for i, m in enumerate(minutes)]
    s2_forced = [
        (not active[i] and i > 0 and active[i - 1]) or (active[i] and flags[i])
        for i in range(len(series))
    ]
    for trade in result.trades:
        # The fill-bar re-pricing puts the stop on the loss side of the fill by at least a
        # tick. In tv_ohlc a trade closed inside its fill bar never saw the re-pricing (Pine).
        repriced = (
            fill_model is FillModel.PESSIMISTIC or trade.exit_bar_index > trade.entry_bar_index
        )
        if repriced:
            sign = trade.side.sign
            fill_price, stop = recorder.repriced[(trade.strategy_id, trade.entry_bar_index)]
            assert fill_price == trade.entry_price, trade
            assert (trade.entry_price - stop) * sign >= TICK - 1e-9, (trade, stop)
            # The first stop the engine locked is that stop. One exception, and only towards
            # the fill: in tv_ohlc the lock follows the fill bar's whole on_bar, where S1's
            # trail may already have stepped (the script trails on the fill bar too once
            # `s1_trailActR` allows it, and 0 R does at once), so the stop can sit at or
            # beyond the fill there. It is never looser than the re-priced stop.
            tightened = (trade.initial_stop - stop) * sign
            if (
                fill_model is FillModel.TV_OHLC
                and trade.strategy_id == S1_LEG
                and params.s1_use_trail
            ):
                assert tightened >= -1e-9, (trade, stop)
            else:
                assert abs(tightened) <= 1e-9, (trade, stop)
        # No leg survives its forced-close bar (S1: session end or the session's last bar;
        # S2: the end of the active window or the session's last bar inside it).
        forced = s1_forced if trade.strategy_id == S1_LEG else s2_forced
        close_at_end = (
            params.s1_close_at_end if trade.strategy_id == S1_LEG else params.s2_close_at_end
        )
        if close_at_end:
            first = next((i for i in range(trade.entry_bar_index, len(series)) if forced[i]), None)
            if first is not None:
                assert trade.exit_bar_index <= first, trade


# -- hypothesis over random walks ------------------------------------------------------------


@st.composite
def days(draw: st.DrawFn) -> BarSeries:
    count = draw(st.integers(min_value=1, max_value=3))
    price_ticks = 20_000  # 5000.00
    bars: list[Bar] = []
    for day in range(count):
        first_ms = int((FIRST_DAY + dt.timedelta(days=day)).timestamp() * 1000)
        moves = draw(
            st.lists(
                st.tuples(
                    st.integers(-2, 2),  # gap from the previous close to this open
                    st.integers(-8, 8),  # close - open
                    st.integers(0, 6),  # wick above
                    st.integers(0, 6),  # wick below
                ),
                min_size=DAY_BARS,
                max_size=DAY_BARS,
            )
        )
        for i, (gap, body, up, down) in enumerate(moves):
            o = price_ticks + gap
            c = o + body
            h = max(o, c) + up
            lo = min(o, c) - down
            price_ticks = c
            bars.append(
                Bar(
                    ts_open_ms=first_ms + i * 60_000,
                    open=o * TICK,
                    high=h * TICK,
                    low=lo * TICK,
                    close=c * TICK,
                    volume=1.0,
                )
            )
    source = DataSource(name="walk", format="generic", sha256="", synthetic=True, bar_tz="UTC")
    return BarSeries(symbol="WALK", tf_seconds=60, bars=tuple(bars), source=source)


@st.composite
def params_strategy(draw: st.DrawFn) -> MasterPortfolioParams:
    return MasterPortfolioParams(
        **COMPACT,
        risk_base=draw(st.sampled_from(["Initial Capital", "Account Equity", "Fixed Balance"])),
        rr=draw(st.sampled_from([0.5, 1.0, 2.0, 5.0, 10.0])),
        max_concurrent_risk_pct=draw(st.sampled_from([0.5, 1.0])),
        use_breakers=draw(st.booleans()),
        daily_dd_pct=draw(st.sampled_from([0.1, 0.5, 2.0])),
        max_consec_loss=draw(st.integers(1, 3)),
        s1_break_confirm=draw(st.sampled_from(["Close", "Wick"])),
        s1_stop_method=draw(st.sampled_from(["FVG Buffer", "ATR Based"])),
        s1_sl_buf=draw(st.integers(0, 4)),
        s1_atr_len_init=draw(st.integers(1, 20)),
        s1_use_trail=draw(st.booleans()),
        s1_trail_mode=draw(st.sampled_from(["Swing (1m structure)", "ATR step", "Percent step"])),
        s1_swing_len=draw(st.integers(1, 6)),
        s1_atr_len_t=draw(st.integers(1, 20)),
        s1_trail_pct=draw(st.sampled_from([0.01, 0.05, 0.3])),
        s1_trail_act_r=draw(st.sampled_from([0.0, 0.5, 1.0])),
        s1_trail_min_step_ticks=draw(st.integers(1, 4)),
        s1_min_fvg_ticks=draw(st.integers(0, 2)),
        s2_stop_method=draw(st.sampled_from(["FVG Buffer", "ATR Based"])),
        s2_sl_buf=draw(st.integers(0, 16)),
        s2_atr_len_init=draw(st.integers(1, 20)),
        s2_max_bars=draw(st.integers(1, 20)),
        s2_min_fvg_ticks=draw(st.integers(0, 2)),
    )


@settings(deadline=None, max_examples=60)
@given(
    series=days(),
    params=params_strategy(),
    fill_model=st.sampled_from([FillModel.PESSIMISTIC, FillModel.TV_OHLC]),
)
def test_random_walks_keep_every_invariant(
    series: BarSeries, params: MasterPortfolioParams, fill_model: FillModel
) -> None:
    flags = session_last_flags(series.bars, series.tf_seconds)

    result, recorder = run(series, flags, params, fill_model)  # no EngineError

    check_invariants(series, flags, params, result, recorder, fill_model)
    again, _ = run(series, flags, params, fill_model)
    assert again.trades == result.trades


# -- the committed synthetic fixtures (defaults) -----------------------------------------------


@pytest.mark.parametrize("file_name", ["synthetic_5m.csv", "synthetic_1m.csv"])
@pytest.mark.parametrize("fill_model", [FillModel.PESSIMISTIC, FillModel.TV_OHLC])
def test_synthetic_fixtures_run_clean_and_keep_every_invariant(
    file_name: str, fill_model: FillModel
) -> None:
    series = load_ohlcv(FIXTURES / file_name)
    flags = session_last_flags(series.bars, series.tf_seconds)
    params = MasterPortfolioParams()

    result, recorder = run(series, flags, params, fill_model)

    check_invariants(series, flags, params, result, recorder, fill_model)
    again, _ = run(series, flags, params, fill_model)
    assert again.trades == result.trades
    # Every filled entry came from a recorded signal.
    assert len({trade.seq for trade in result.trades}) <= len(recorder.signals)
