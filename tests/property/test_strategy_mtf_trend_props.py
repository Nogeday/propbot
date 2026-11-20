"""Properties of the MTF Trend Continuation port over random and fixture bar series.

Random walks on the MES tick grid run through the real engine in both fill models; the
committed synthetic fixtures (`tests/fixtures/ohlcv`) are a fixed smoke run. Each property
is one of the script's own guarantees: the stop is on the loss side of the fill, no more
entries per New York day than `maxTradesDay`, nothing open past the forced-close time or
the session's last bar, the cash risk at the signal within `riskPercent` of the risk base,
and the same input gives the same trades.
"""

import datetime as dt
import functools
import math
from collections.abc import Sequence
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import pytest
from hypothesis import given, settings
from hypothesis import strategies as st

from propbot.backtest.engine import run_backtest
from propbot.backtest.model import CostModel, EngineResult, ExitReason, FillModel, group_by_entry
from propbot.core.bars import Bar, BarSeries, DataSource
from propbot.core.session import LocalClock
from propbot.data.ohlcv import load_ohlcv
from propbot.data.sessions import session_last_flags
from propbot.strategies.base import Action, Entry, Exit, FillEvent, StrategyContext
from propbot.strategies.mtf_trend import MtfTrend, MtfTrendParams
from support.engine import MINUTE_MS, make_config, mes
from support.paths import REPO_ROOT

TICK = 0.25
POINT_VALUE = 5.0
FIXTURES = REPO_ROOT / "tests" / "fixtures" / "ohlcv"
NEW_YORK = LocalClock("America/New_York")
# Pine header defaults for MES: 0.50 $ per contract and side, 1 tick slippage.
MES_COSTS = CostModel(commission_per_unit_side=0.5, slippage_ticks=1.0)
# 2026-03-02 08:00 New York (EST): random sessions start here, a day apart.
SESSION_START_MS = 1_772_456_400_000
DAY_MS = 24 * 60 * MINUTE_MS


@dataclass(eq=False)
class Observer:
    """Delegates to the port and records what the properties need."""

    inner: MtfTrend
    signals: list[tuple[int, int, float, float, float]] = field(default_factory=list)
    finalized: list[tuple[int, float, float]] = field(default_factory=list)  # side, avg, init

    @property
    def strategy_id(self) -> str:
        return self.inner.strategy_id

    @property
    def pyramiding(self) -> int:
        return self.inner.pyramiding

    def on_bar(self, bar: Bar, ctx: StrategyContext) -> Sequence[Action]:
        was_final = self.inner.active_init_stop is not None
        actions = list(self.inner.on_bar(bar, ctx))
        entry = next((a for a in actions if isinstance(a, Entry)), None)
        if entry is not None:
            stop = next(a.stop for a in actions if isinstance(a, Exit) and a.stop is not None)
            self.signals.append((ctx.bar_index, bar.ts_open_ms, bar.close, stop, entry.qty))
        init = self.inner.active_init_stop
        if not was_final and init is not None and ctx.position_avg_price is not None:
            sign = 1 if ctx.position_size > 0 else -1
            self.finalized.append((sign, ctx.position_avg_price, init))
        return actions

    def on_fill(self, fill: FillEvent, ctx: StrategyContext) -> Sequence[Action]:
        actions = list(self.inner.on_fill(fill, ctx))
        init = self.inner.active_init_stop
        assert init is not None
        self.finalized.append((fill.side.sign, fill.price, init))
        return actions


def run(
    data: BarSeries,
    flags: Sequence[bool],
    fill_model: FillModel,
    params: MtfTrendParams,
    costs: CostModel = MES_COSTS,
) -> tuple[EngineResult, Observer]:
    observer = Observer(MtfTrend(params))
    config = make_config(instrument=mes(), costs=costs, fill_model=fill_model)
    return run_backtest(data, observer, config, session_last=flags), observer


def check_properties(
    data: BarSeries,
    flags: Sequence[bool],
    params: MtfTrendParams,
    result: EngineResult,
    observer: Observer,
    fill_model: FillModel,
) -> None:
    bars = data.bars
    end_minute = params.sess_end_h * 60 + params.sess_end_m

    # The finalized initial stop is at least one tick on the loss side of the fill.
    for sign, avg, init in observer.finalized:
        assert (avg - init) * sign >= TICK - 1e-9
    if fill_model is FillModel.PESSIMISTIC:
        # ... and it is the first stop in force (on_fill re-prices at the fill instant).
        for trade in result.trades:
            sign = 1 if trade.side.value == "long" else -1
            assert (trade.entry_price - trade.initial_stop) * sign >= TICK - 1e-9

    # No more entries per New York calendar day (the script's dayofmonth) than allowed.
    per_day: dict[dt.date, int] = {}
    for _, ts_ms, *_ in observer.signals:
        day = NEW_YORK.parts(ts_ms).date
        per_day[day] = per_day.get(day, 0) + 1
    assert all(count <= params.max_trades_day for count in per_day.values())

    # Cash risk at the signal never exceeds riskPercent of the risk base (initial capital).
    budget = 100_000.0 * params.risk_percent / 100.0
    for _, _, close, stop, qty in observer.signals:
        assert qty >= 1.0
        assert qty == math.floor(qty)
        assert abs(close - stop) * qty * POINT_VALUE <= budget * (1 + 1e-12)

    # Nothing stays open at the close of a forced-close bar.
    for trade in result.trades:
        assert trade.reason is not ExitReason.END_OF_DATA
        for index in range(trade.entry_bar_index, trade.exit_bar_index):
            minute = NEW_YORK.parts(bars[index].ts_open_ms).minute_of_day
            assert not flags[index]
            assert not (params.use_session and minute >= end_minute)


def same_trades(first: EngineResult, second: EngineResult) -> bool:
    return first.trades == second.trades


# -- random walks -----------------------------------------------------------------------------

# Per bar: gap from the previous close, close move, wick above, wick below, volume (ticks).
_bar = st.tuples(
    st.integers(-2, 2),
    st.integers(-10, 10),
    st.integers(0, 6),
    st.integers(0, 6),
    st.integers(1, 1000),
)


def random_series(sessions: Sequence[Sequence[tuple[int, int, int, int, int]]]) -> BarSeries:
    bars: list[Bar] = []
    close = 5000.0
    for day, steps in enumerate(sessions):
        start = SESSION_START_MS + day * DAY_MS
        for index, (gap, move, up, down, volume) in enumerate(steps):
            open_ = close + gap * TICK
            close = open_ + move * TICK
            bars.append(
                Bar(
                    ts_open_ms=start + index * 5 * MINUTE_MS,
                    open=open_,
                    high=max(open_, close) + up * TICK,
                    low=min(open_, close) - down * TICK,
                    close=close,
                    volume=float(volume),
                )
            )
    source = DataSource(name="random", format="generic", sha256="", synthetic=True, bar_tz="UTC")
    return BarSeries(symbol="MES", tf_seconds=300, bars=tuple(bars), source=source)


# 08:00 + 110 bars = 17:10: crosses 09:30 and 15:55 every session.
_sessions = st.lists(st.lists(_bar, min_size=110, max_size=110), min_size=1, max_size=2)
# Short lengths so a 110-bar session gets armed and triggered often.
_params = st.fixed_dictionaries(
    {
        "htf_tf": st.sampled_from(["15", "30", "60"]),
        "htf_fast_len": st.integers(1, 3),
        "htf_slow_len": st.integers(3, 6),
        "ltf_fast_len": st.integers(2, 8),
        "ltf_slow_len": st.integers(5, 15),
        "use_vol_filter": st.booleans(),
        "swing_len": st.integers(2, 10),
        "risk_percent": st.sampled_from([0.05, 0.1, 0.5]),
        "trail_type": st.sampled_from(["Chandelier Exit", "ATR Trailing"]),
        "ch_len": st.integers(2, 22),
        "atr_trail_len": st.integers(2, 14),
        "max_trades_day": st.integers(1, 3),
        "daily_loss_pct": st.sampled_from([0.1, 2.0]),
        "use_consec": st.booleans(),
    }
)


@settings(deadline=None, max_examples=25)
@given(sessions=_sessions, overrides=_params, fill_model=st.sampled_from(list(FillModel)))
def test_random_walks_keep_the_scripts_guarantees(
    sessions: list[list[tuple[int, int, int, int, int]]],
    overrides: dict[str, Any],
    fill_model: FillModel,
) -> None:
    data = random_series(sessions)
    flags = session_last_flags(data.bars, data.tf_seconds)
    params = MtfTrendParams(**overrides)

    result, observer = run(data, flags, fill_model, params)
    check_properties(data, flags, params, result, observer, fill_model)
    again, _ = run(data, flags, fill_model, params)
    assert same_trades(result, again)


@settings(deadline=None, max_examples=15)
@given(sessions=_sessions, overrides=_params)
def test_both_fill_models_take_the_same_signals_until_their_trades_diverge(
    sessions: list[list[tuple[int, int, int, int, int]]], overrides: dict[str, Any]
) -> None:
    # Signals depend on the bars and the account; with no costs and the first trade the
    # same in both models, the first signal bar must agree.
    data = random_series(sessions)
    flags = session_last_flags(data.bars, data.tf_seconds)
    params = MtfTrendParams(**overrides)
    free = CostModel(commission_per_unit_side=0.0, slippage_ticks=0.0)

    _, pess = run(data, flags, FillModel.PESSIMISTIC, params, free)
    _, tv = run(data, flags, FillModel.TV_OHLC, params, free)
    if pess.signals and tv.signals:
        assert pess.signals[0] == tv.signals[0]
    else:
        assert pess.signals == tv.signals == []


# -- the committed synthetic fixtures ---------------------------------------------------------


@functools.cache
def fixture(name: str) -> tuple[BarSeries, tuple[bool, ...]]:
    data = load_ohlcv(Path(FIXTURES / name))
    return data, session_last_flags(data.bars, data.tf_seconds)


@pytest.mark.parametrize("fill_model", list(FillModel))
@pytest.mark.parametrize(
    ("name", "expected_entries"), [("synthetic_5m.csv", 20), ("synthetic_1m.csv", 3)]
)
def test_the_synthetic_fixtures_run_clean_with_the_pine_defaults(
    name: str, expected_entries: int, fill_model: FillModel
) -> None:
    data, flags = fixture(name)
    params = MtfTrendParams()

    result, observer = run(data, flags, fill_model, params)
    check_properties(data, flags, params, result, observer, fill_model)
    # Smoke figures, not evidence (SYNTHETIC data): the count is pinned so a change in the
    # port or the fixture is noticed.
    assert len(group_by_entry(result.trades)) == len(observer.signals) == expected_entries
    again, _ = run(data, flags, fill_model, params)
    assert same_trades(result, again)
