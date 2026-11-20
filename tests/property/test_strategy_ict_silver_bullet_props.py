"""Properties of the ICT Silver Bullet port on random walks and on the synthetic fixtures.

Each run goes through the real engine in both fill models and must hold:
- no `EngineError` (invariant 2 and the order contract are respected);
- the stop is on the loss side of the fill by at least one tick when it comes into force,
  unless the fill itself already sat at or beyond the stop (a gap, or the half-tick risk
  of a CE level): then the script's fixed stop takes the trade out in its fill bar;
- never more entries per New York calendar day than `max_tr_day`;
- no position outlives the force-flat minute or the session's last bar;
- the risk at the signal (|limit - stop| x qty x point value) stays within risk % of the
  risk base;
- the same input gives the same trades twice.
"""

from collections import Counter
from collections.abc import Sequence
from dataclasses import dataclass, field
from pathlib import Path

from hypothesis import given, settings
from hypothesis import strategies as st

from propbot.backtest.engine import run_backtest
from propbot.backtest.model import EngineResult, ExitReason, FillModel
from propbot.core.bars import Bar, BarSeries, DataSource
from propbot.core.session import LocalClock
from propbot.data.ohlcv import load_ohlcv
from propbot.data.sessions import session_last_flags
from propbot.strategies.base import Action, Entry, Exit, FillEvent, StrategyContext
from propbot.strategies.ict_silver_bullet import IctSilverBullet, IctSilverBulletParams
from support.engine import MINUTE_MS, START_MS, make_config, mes
from support.paths import REPO_ROOT

TICK = 0.25
POINT_VALUE = 5.0
CAPITAL = 100_000.0
DAY_MS = 24 * 60 * MINUTE_MS
BARS_PER_DAY = 100  # 09:30-11:09 New York: covers the AM window
FIXTURES = REPO_ROOT / "tests" / "fixtures" / "ohlcv"
NY = LocalClock("America/New_York")


@dataclass(eq=False)
class Recorder:
    inner: IctSilverBullet
    entries: list[tuple[Entry, Exit]] = field(default_factory=list)

    @property
    def strategy_id(self) -> str:
        return self.inner.strategy_id

    @property
    def pyramiding(self) -> int:
        return self.inner.pyramiding

    def on_bar(self, bar: Bar, ctx: StrategyContext) -> Sequence[Action]:
        out = list(self.inner.on_bar(bar, ctx))
        for index, action in enumerate(out):
            if isinstance(action, Entry):
                bracket = out[index + 1]
                assert isinstance(bracket, Exit)
                assert bracket.from_entry == action.id
                assert bracket.stop is not None
                self.entries.append((action, bracket))
        return out

    def on_fill(self, fill: FillEvent, ctx: StrategyContext) -> Sequence[Action]:
        return list(self.inner.on_fill(fill, ctx))


def run_and_check(
    series: BarSeries,
    last: Sequence[bool],
    params: IctSilverBulletParams,
    model: FillModel,
) -> EngineResult:
    recorder = Recorder(IctSilverBullet(params))
    config = make_config(instrument=mes(), fill_model=model, initial_capital=CAPITAL)
    result = run_backtest(series, recorder, config, session_last=last)

    # Determinism: a fresh instance on the same input trades the same way.
    again = run_backtest(series, IctSilverBullet(params), config, session_last=last)
    assert again.trades == result.trades

    flat_min = params.day_end_h * 60 + params.day_end_m
    entries_per_day: Counter[tuple[int, int, int]] = Counter()
    seen: set[int] = set()
    for trade in result.trades:
        assert trade.reason is not ExitReason.END_OF_DATA
        for b in range(trade.entry_bar_index, trade.exit_bar_index):
            minute = NY.parts(series.bars[b].ts_open_ms).minute_of_day
            assert not last[b], f"position held past the session's last bar {b}"
            if params.close_at_day_end:
                assert minute < flat_min, f"position held past the force-flat bar {b}"
        sign = trade.side.sign
        cushion = (trade.entry_price - trade.initial_stop) * sign
        if cushion < TICK - 1e-9:
            # The fill sat within a tick of (or beyond) the fixed stop: out in the fill bar.
            assert trade.reason is ExitReason.STOP
            assert trade.exit_bar_index == trade.entry_bar_index
        if trade.seq not in seen:
            seen.add(trade.seq)
            parts = NY.parts(trade.entry_ts_ms)
            entries_per_day[(parts.year, parts.month, parts.day)] += 1
    assert all(count <= params.max_tr_day for count in entries_per_day.values())

    if params.risk_base == "Initial Capital":
        cap = CAPITAL * params.risk_pct / 100.0
        for entry, bracket in recorder.entries:
            assert entry.limit is not None
            assert bracket.stop is not None
            risk = abs(entry.limit - bracket.stop) * entry.qty * POINT_VALUE
            assert risk <= cap * (1 + 1e-9)
            # The stop sits on the loss side of the planned entry level.
            assert (entry.limit - bracket.stop) * entry.side.sign > 0
    return result


# -- random walks -------------------------------------------------------------------------

_bar_steps = st.tuples(
    st.integers(-10, 10),  # close - open, ticks
    st.integers(0, 8),  # wick above max(open, close), ticks
    st.integers(0, 8),  # wick below min(open, close), ticks
    st.integers(-2, 2),  # gap of the next open, ticks
)


def _walk(steps: Sequence[tuple[int, int, int, int]]) -> tuple[BarSeries, list[bool]]:
    bars: list[Bar] = []
    price = 20_000  # in ticks: 5000.00
    times = [START_MS + i * MINUTE_MS for i in range(BARS_PER_DAY)]
    times += [START_MS + DAY_MS + i * MINUTE_MS for i in range(BARS_PER_DAY)]
    for ts, (body, up, down, gap) in zip(times, steps, strict=True):
        open_ = price
        close = open_ + body
        high = max(open_, close) + up
        low = min(open_, close) - down
        bars.append(
            Bar(
                ts_open_ms=ts,
                open=open_ * TICK,
                high=high * TICK,
                low=low * TICK,
                close=close * TICK,
                volume=1.0,
            )
        )
        price = close + gap
    source = DataSource(name="walk", format="generic", sha256="", synthetic=True, bar_tz="UTC")
    series = BarSeries(symbol="WALK", tf_seconds=60, bars=tuple(bars), source=source)
    last = [i in (BARS_PER_DAY - 1, 2 * BARS_PER_DAY - 1) for i in range(2 * BARS_PER_DAY)]
    return series, last


_params = st.builds(
    IctSilverBulletParams,
    pivot_len=st.integers(1, 3),
    entry_fill=st.sampled_from(
        ["Proximal (first touch)", "50% (Consequent Encroachment)", "Distal (full gap fill)"]
    ),
    tp_mode=st.sampled_from(["Fixed R:R", "Next opposing liquidity"]),
    max_tr_window=st.integers(1, 2),
    max_tr_day=st.integers(1, 2),
    max_consec=st.integers(1, 3),
    day_end_h=st.just(10),
    day_end_m=st.sampled_from([20, 40, 59]),
    max_daily_dd_pct=st.sampled_from([0.1, 2.0]),
    risk_pct=st.sampled_from([0.5, 2.0]),
)


@settings(deadline=None, max_examples=40)
@given(
    steps=st.lists(_bar_steps, min_size=2 * BARS_PER_DAY, max_size=2 * BARS_PER_DAY), params=_params
)
def test_random_walks_hold_the_properties_in_both_fill_models(
    steps: list[tuple[int, int, int, int]], params: IctSilverBulletParams
) -> None:
    series, last = _walk(steps)
    for model in FillModel:
        run_and_check(series, last, params, model)


@settings(deadline=None, max_examples=25)
@given(steps=st.lists(_bar_steps, min_size=2 * BARS_PER_DAY, max_size=2 * BARS_PER_DAY))
def test_random_walks_hold_the_properties_with_the_default_inputs(
    steps: list[tuple[int, int, int, int]],
) -> None:
    series, last = _walk(steps)
    for model in FillModel:
        run_and_check(series, last, IctSilverBulletParams(), model)


# -- committed synthetic fixtures (smoke run) -------------------------------------------


def _fixture(name: str) -> tuple[BarSeries, list[bool]]:
    series = load_ohlcv(Path(FIXTURES / name))
    return series, list(session_last_flags(series.bars, series.tf_seconds))


def test_synthetic_fixtures_run_clean_in_both_fill_models_with_the_default_inputs() -> None:
    params = IctSilverBulletParams()
    for name in ("synthetic_5m.csv", "synthetic_1m.csv"):
        series, last = _fixture(name)
        for model in FillModel:
            run_and_check(series, last, params, model)


def test_synthetic_fixtures_hold_the_properties_with_other_inputs() -> None:
    variants = [
        IctSilverBulletParams(pivot_len=2, entry_fill="Proximal (first touch)", max_tr_day=2),
        IctSilverBulletParams(
            pivot_len=3, tp_mode="Next opposing liquidity", max_tr_window=2, max_tr_day=2
        ),
        IctSilverBulletParams(entry_fill="Distal (full gap fill)", max_daily_dd_pct=0.1),
    ]
    for name in ("synthetic_5m.csv", "synthetic_1m.csv"):
        series, last = _fixture(name)
        for params in variants:
            for model in FillModel:
                run_and_check(series, last, params, model)
