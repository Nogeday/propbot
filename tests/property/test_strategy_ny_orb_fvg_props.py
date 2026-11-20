"""Properties of the NY ORB + FVG port over random-walk days and the committed fixtures.

The random walks are gapless (each bar opens at the previous close) and lie on the MES
tick grid, over several New York days that include the 2026-03-08 DST switch. The costs
are the Pine header's MES profile ($0.50 per side, 1 tick slippage).
"""

import datetime as dt
import random
from collections.abc import Sequence
from dataclasses import dataclass, field
from typing import Final
from zoneinfo import ZoneInfo

import pytest
from hypothesis import given, settings
from hypothesis import strategies as st

from propbot.backtest.engine import run_backtest
from propbot.backtest.model import CostModel, EngineResult, ExitReason, FillModel
from propbot.core.bars import Bar, BarSeries, DataSource
from propbot.core.session import LocalClock
from propbot.data.ohlcv import load_ohlcv
from propbot.data.sessions import session_last_flags
from propbot.strategies.base import Action, Entry, Exit, FillEvent, StrategyContext
from propbot.strategies.ny_orb_fvg import NyOrbFvg, NyOrbFvgParams
from support.engine import make_config, mes
from support.paths import REPO_ROOT

NY: Final = ZoneInfo("America/New_York")
TICK: Final = 0.25
POINT_VALUE: Final = 5.0
MES_COSTS: Final = CostModel(commission_per_unit_side=0.5, slippage_ticks=1.0)
FIXTURES: Final = REPO_ROOT / "tests" / "fixtures" / "ohlcv"
# Thursday, Friday and the Monday after the DST switch.
DAYS: Final = (dt.date(2026, 3, 5), dt.date(2026, 3, 6), dt.date(2026, 3, 9))
FIRST_MINUTE: Final = 9 * 60 + 20
LAST_MINUTE: Final = 16 * 60 + 5


@dataclass(eq=False)
class Recorder:
    """Delegates to the port and keeps every (bar, ctx, actions) of `on_bar`."""

    inner: NyOrbFvg
    calls: list[tuple[Bar, StrategyContext, list[Action]]] = field(default_factory=list)

    @property
    def strategy_id(self) -> str:
        return self.inner.strategy_id

    @property
    def pyramiding(self) -> int:
        return self.inner.pyramiding

    def on_bar(self, bar: Bar, ctx: StrategyContext) -> Sequence[Action]:
        actions = list(self.inner.on_bar(bar, ctx))
        self.calls.append((bar, ctx, actions))
        return actions

    def on_fill(self, fill: FillEvent, ctx: StrategyContext) -> Sequence[Action]:
        return self.inner.on_fill(fill, ctx)


def random_walk(seed: int, tf_seconds: int) -> BarSeries:
    """Gapless days from 09:20 to 16:05 New York; the walk also runs across the nights."""
    rng = random.Random(seed)
    step_ms = tf_seconds * 1000
    scale = 1 if tf_seconds == 60 else 2
    close = 5000.0
    bars: list[Bar] = []
    for day in DAYS:
        start = dt.datetime(day.year, day.month, day.day, tzinfo=NY)
        first_ms = int(start.timestamp()) * 1000 + FIRST_MINUTE * 60_000
        for i in range((LAST_MINUTE - FIRST_MINUTE) * 60 // tf_seconds):
            open_ = close
            close = open_ + rng.randint(-8 * scale, 8 * scale) * TICK
            high = max(open_, close) + rng.randint(0, 5 * scale) * TICK
            low = min(open_, close) - rng.randint(0, 5 * scale) * TICK
            bars.append(Bar(first_ms + i * step_ms, open_, high, low, close, 1.0))
    source = DataSource(name="walk", format="generic", sha256="", synthetic=True, bar_tz="UTC")
    return BarSeries(symbol="WALK", tf_seconds=tf_seconds, bars=tuple(bars), source=source)


def run_port(
    series: BarSeries, params: NyOrbFvgParams, fill_model: FillModel
) -> tuple[EngineResult, Recorder, tuple[bool, ...]]:
    recorder = Recorder(NyOrbFvg(params))
    flags = session_last_flags(series.bars, series.tf_seconds)
    config = make_config(instrument=mes(), costs=MES_COSTS, fill_model=fill_model)
    return run_backtest(series, recorder, config, session_last=flags), recorder, flags


def check_invariants(
    series: BarSeries,
    params: NyOrbFvgParams,
    result: EngineResult,
    recorder: Recorder,
    flags: Sequence[bool],
) -> int:
    """Assert the port's properties on one run; return the number of entry signals."""
    clock = LocalClock(params.tz)
    sess_end = params.sess_end_hour * 60 + params.sess_end_min

    # A stop that first comes into force sits >= 1 tick on the loss side of the fill.
    for trade in result.trades:
        loss_side = (trade.entry_price - trade.initial_stop) * trade.side.sign
        assert loss_side >= TICK - 1e-9, trade

    # At most one entry signal per New York calendar day (the script's `newDay`).
    signals: list[tuple[dt.date, Bar, StrategyContext, Entry, Exit]] = []
    for bar, ctx, actions in recorder.calls:
        entries = [a for a in actions if isinstance(a, Entry)]
        if not entries:
            continue
        (entry,) = entries
        exits = [a for a in actions if isinstance(a, Exit) and a.from_entry == entry.id]
        assert exits, "every entry is returned with its bracket"
        assert exits[0].stop is not None
        signals.append((clock.parts(bar.ts_open_ms).date, bar, ctx, entry, exits[0]))
    days = [signal[0] for signal in signals]
    assert len(days) == len(set(days))
    fills_per_day: dict[dt.date, int] = {}
    for record in {(t.seq, t.entry_ts_ms) for t in result.trades}:
        date = clock.parts(record[1]).date
        fills_per_day[date] = fills_per_day.get(date, 0) + 1
    assert all(count <= 1 for count in fills_per_day.values())

    # Risk at the signal never exceeds risk % of the risk base.
    for _, bar, ctx, entry, exit_ in signals:
        assert exit_.stop is not None
        if params.risk_base == "Fixed Balance":
            base = params.fixed_bal
        elif params.risk_base == "Account Equity":
            base = ctx.equity
        else:
            base = ctx.initial_capital
        risk = abs(bar.close - exit_.stop) * entry.qty * POINT_VALUE
        assert risk <= params.risk_pct / 100.0 * base + 1e-6
        assert entry.qty == int(entry.qty)  # whole MES contracts

    # No position survives the forced-close time or the session's last bar.
    if params.close_at_end:
        forced = [
            flags[i] or clock.parts(bar.ts_open_ms).minute_of_day >= sess_end
            for i, bar in enumerate(series.bars)
        ]
        for trade in result.trades:
            assert trade.reason is not ExitReason.END_OF_DATA
            deadline = next(i for i in range(trade.entry_bar_index, len(forced)) if forced[i])
            assert trade.exit_bar_index <= deadline, trade
    return len(signals)


_params = st.builds(
    NyOrbFvgParams,
    break_confirm=st.sampled_from(["Close", "Wick"]),
    risk_base=st.sampled_from(["Initial Capital", "Account Equity", "Fixed Balance"]),
    stop_method=st.sampled_from(["FVG Buffer", "ATR Based"]),
    atr_len_init=st.sampled_from([1, 5, 14]),
    trail_mode=st.sampled_from(["Swing (1m structure)", "ATR step", "Percent step"]),
    trail_pct=st.sampled_from([0.05, 0.3]),
    risk_pct=st.sampled_from([0.5, 2.0]),
    daily_dd_pct=st.sampled_from([0.5, 2.0]),
    min_fvg_ticks=st.integers(0, 4),
    use_ifvg=st.booleans(),
)


@settings(deadline=None, max_examples=12)
@given(
    seed=st.integers(0, 2**32 - 1),
    tf_seconds=st.sampled_from([60, 300]),
    params=_params,
    fill_model=st.sampled_from(list(FillModel)),
)
def test_random_walk_days_keep_every_property(
    seed: int, tf_seconds: int, params: NyOrbFvgParams, fill_model: FillModel
) -> None:
    series = random_walk(seed, tf_seconds)
    result, recorder, flags = run_port(series, params, fill_model)  # no EngineError
    check_invariants(series, params, result, recorder, flags)


@settings(deadline=None, max_examples=6)
@given(seed=st.integers(0, 2**32 - 1), fill_model=st.sampled_from(list(FillModel)))
def test_the_same_input_gives_the_same_trades(seed: int, fill_model: FillModel) -> None:
    series = random_walk(seed, 60)
    first, _, _ = run_port(series, NyOrbFvgParams(), fill_model)
    second, _, _ = run_port(series, NyOrbFvgParams(), fill_model)
    assert first == second


@pytest.mark.parametrize("name", ["synthetic_5m.csv", "synthetic_1m.csv"])
@pytest.mark.parametrize("fill_model", list(FillModel))
def test_synthetic_fixtures_run_clean_in_both_fill_models(name: str, fill_model: FillModel) -> None:
    series = load_ohlcv(FIXTURES / name)
    params = NyOrbFvgParams()
    result, recorder, flags = run_port(series, params, fill_model)
    signals = check_invariants(series, params, result, recorder, flags)
    # 5m: 20 entries in 20 sessions; 1m: 3 in 3 (at Pine defaults, both fill models).
    assert signals == {"synthetic_5m.csv": 20, "synthetic_1m.csv": 3}[name]
    assert len({trade.seq for trade in result.trades}) == signals
    again, _, _ = run_port(series, params, fill_model)
    assert again == result
