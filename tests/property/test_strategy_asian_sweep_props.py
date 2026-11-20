"""Properties of the Asian sweep port over random-walk bars and the committed synthetic fixtures.

The random series use compact windows so that every generated night holds a whole cycle:
Asian [23:50, 00:00), active [00:05, 00:50) New York, one block of 1-minute bars per night
(23:40-01:09) and a gap to the next night. The fixtures run with the script's defaults.
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
from propbot.strategies.asian_sweep import AsianSweep, AsianSweepParams
from propbot.strategies.base import Action, Entry, Exit, FillEvent, StrategyContext
from support.engine import make_config, mes
from support.paths import REPO_ROOT

NY = ZoneInfo("America/New_York")
TICK = 0.25
POINT_VALUE = 5.0
FIXTURES = REPO_ROOT / "tests" / "fixtures" / "ohlcv"

COMPACT: dict[str, Any] = {
    "asian_start_hour": 23,
    "asian_start_min": 50,
    "asian_end_hour": 0,
    "asian_end_min": 0,
    "active_start_hour": 0,
    "active_start_min": 5,
    "active_end_hour": 0,
    "active_end_min": 50,
}
NIGHT_BARS = 90  # 23:40 .. 01:09
FIRST_NIGHT = dt.datetime(2026, 3, 2, 23, 40, tzinfo=NY)  # Monday


class Recorder:
    """Keeps every signal (the Entry, its bracket and the context it was sent in)."""

    def __init__(self, inner: AsianSweep) -> None:
        self.inner = inner
        self.signals: list[tuple[int, Entry, Exit, StrategyContext, float]] = []

    @property
    def strategy_id(self) -> str:
        return self.inner.strategy_id

    @property
    def pyramiding(self) -> int:
        return self.inner.pyramiding

    def on_bar(self, bar: Bar, ctx: StrategyContext) -> Sequence[Action]:
        out = list(self.inner.on_bar(bar, ctx))
        entries = [a for a in out if isinstance(a, Entry)]
        if entries:
            (entry,) = entries
            bracket = out[out.index(entry) + 1]
            assert isinstance(bracket, Exit)
            assert bracket.from_entry == entry.id
            self.signals.append((ctx.bar_index, entry, bracket, ctx, bar.close))
        return out

    def on_fill(self, fill: FillEvent, ctx: StrategyContext) -> Sequence[Action]:
        return self.inner.on_fill(fill, ctx)


def run(
    series: BarSeries, flags: Sequence[bool], params: AsianSweepParams, fill_model: FillModel
) -> tuple[EngineResult, Recorder]:
    recorder = Recorder(AsianSweep(params))
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
    params: AsianSweepParams,
    result: EngineResult,
    recorder: Recorder,
    fill_model: FillModel,
) -> None:
    clock = LocalClock(params.tz)
    minutes = [clock.parts(bar.ts_open_ms).minute_of_day for bar in series.bars]
    a_start = params.asian_start_hour * 60 + params.asian_start_min
    a_end = params.asian_end_hour * 60 + params.asian_end_min
    x_start = params.active_start_hour * 60 + params.active_start_min
    x_end = params.active_end_hour * 60 + params.active_end_min
    asian = [_window(m, a_start, a_end) for m in minutes]
    active = [_window(m, x_start, x_end) for m in minutes]

    # The script's day: a cycle starts on the first bar of the Asian window.
    cycle = []
    current = 0
    for i, now in enumerate(asian):
        if now and not (asian[i - 1] if i > 0 else False):
            current += 1
        cycle.append(current)
    per_cycle: dict[int, int] = {}
    for bar_index, *_ in recorder.signals:
        per_cycle[cycle[bar_index]] = per_cycle.get(cycle[bar_index], 0) + 1
    assert all(count <= 1 for count in per_cycle.values()), per_cycle

    # Risk at the signal never exceeds risk % of the risk base.
    for _, entry, bracket, ctx, close in recorder.signals:
        assert bracket.stop is not None
        if params.risk_base == "Initial Capital":
            base = ctx.initial_capital
        elif params.risk_base == "Account Equity":
            base = ctx.equity
        else:
            base = params.fixed_bal
        risk = abs(close - bracket.stop) * entry.qty * POINT_VALUE
        assert risk <= base * params.risk_pct / 100.0 * (1 + 1e-9)
        assert entry.qty >= 1.0
        assert entry.qty == math.floor(entry.qty)

    forced = [
        (not active[i] and i > 0 and active[i - 1]) or (active[i] and flags[i])
        for i in range(len(series))
    ]
    for trade in result.trades:
        # The first stop in force is on the loss side of the fill by at least a tick. In
        # tv_ohlc a trade closed inside its fill bar never saw the re-pricing: its stop is
        # the signal bar's estimate, which a gapped open can leave on the wrong side.
        repriced = (
            fill_model is FillModel.PESSIMISTIC or trade.exit_bar_index > trade.entry_bar_index
        )
        if repriced:
            loss_side = (trade.entry_price - trade.initial_stop) * trade.side.sign
            assert loss_side >= TICK - 1e-9, trade
        # No position survives the forced-close bar (window end / session's last bar).
        if params.close_at_end:
            first_forced = next(
                (i for i in range(trade.entry_bar_index, len(series)) if forced[i]), None
            )
            if first_forced is not None:
                assert trade.exit_bar_index <= first_forced, trade


# -- hypothesis over random walks ------------------------------------------------------------


@st.composite
def nights(draw: st.DrawFn) -> BarSeries:
    count = draw(st.integers(min_value=1, max_value=3))
    price_ticks = 20_000  # 5000.00
    bars: list[Bar] = []
    for night in range(count):
        start = FIRST_NIGHT + dt.timedelta(days=night)
        first_ms = int(start.timestamp() * 1000)
        moves = draw(
            st.lists(
                st.tuples(
                    st.integers(-2, 2),  # gap from the previous close to this open
                    st.integers(-8, 8),  # close - open
                    st.integers(0, 6),  # wick above
                    st.integers(0, 6),  # wick below
                ),
                min_size=NIGHT_BARS,
                max_size=NIGHT_BARS,
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


params_strategy = st.builds(
    lambda stop_method, risk_base, sl_buf, max_bars, rr, min_fvg, dd, breakers, atr_len: (
        AsianSweepParams(
            **COMPACT,
            stop_method=stop_method,
            risk_base=risk_base,
            sl_buf=sl_buf,
            max_bars=max_bars,
            rr=rr,
            min_fvg_ticks=min_fvg,
            daily_dd_pct=dd,
            use_breakers=breakers,
            atr_len_init=atr_len,
        )
    ),
    st.sampled_from(["FVG Buffer", "ATR Based"]),
    st.sampled_from(["Initial Capital", "Account Equity", "Fixed Balance"]),
    st.integers(0, 4),
    st.integers(1, 20),
    st.sampled_from([0.5, 1.0, 2.0, 3.0]),
    st.integers(0, 2),
    st.sampled_from([0.1, 0.5, 2.0]),
    st.booleans(),
    st.integers(1, 20),
)


@settings(deadline=None, max_examples=60)
@given(
    series=nights(),
    params=params_strategy,
    fill_model=st.sampled_from([FillModel.PESSIMISTIC, FillModel.TV_OHLC]),
)
def test_random_walks_keep_every_invariant(
    series: BarSeries, params: AsianSweepParams, fill_model: FillModel
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
    params = AsianSweepParams()

    result, recorder = run(series, flags, params, fill_model)

    check_invariants(series, flags, params, result, recorder, fill_model)
    again, _ = run(series, flags, params, fill_model)
    assert again.trades == result.trades
    # Every filled entry came from a recorded signal.
    assert len({trade.seq for trade in result.trades}) <= len(recorder.signals)
