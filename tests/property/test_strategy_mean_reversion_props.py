"""Properties of the Mean Reversion port on random bar series and on the synthetic fixtures.

Random series: 5-minute bars of whole New York regular sessions (09:30-15:55, the last
bar of each day ends the session) on the MES tick grid, with large random swings so the
2.5-sigma setups actually occur. Every bar spans at least one tick, as real bars do.
"""

import math
from collections import Counter
from collections.abc import Sequence
from typing import Final

import pytest
from hypothesis import given, settings
from hypothesis import strategies as st

from propbot.backtest.engine import run_backtest
from propbot.backtest.model import EngineResult, ExitReason, FillModel
from propbot.core.bars import Bar, BarSeries, DataSource
from propbot.core.session import LocalClock
from propbot.data.ohlcv import load_ohlcv
from propbot.data.sessions import session_last_flags
from propbot.strategies.base import Action, Entry, Exit, FillEvent, StrategyContext
from propbot.strategies.mean_reversion import MeanReversion, MeanReversionParams
from support.engine import MINUTE_MS, START_MS, make_config, mes
from support.paths import REPO_ROOT

TICK: Final = 0.25
BARS_PER_DAY: Final = 78  # 09:30 .. 15:55
DAY_MS: Final = 24 * 60 * MINUTE_MS
FIXTURES: Final = REPO_ROOT / "tests" / "fixtures" / "ohlcv"
MODELS: Final = (FillModel.PESSIMISTIC, FillModel.TV_OHLC)
CLOCK: Final = LocalClock()

# (close move, upper wick, lower wick, gap at the next open), all in ticks.
_moves = st.tuples(
    st.integers(-24, 24),
    st.integers(0, 6),
    st.integers(0, 6),
    st.sampled_from((0, 0, 0, 0, -8, 8)),
)


class Recorder:
    """Keeps every bar's actions with the bar, to check the orders at the signal."""

    def __init__(self, params: MeanReversionParams) -> None:
        self.inner = MeanReversion(params)
        self.signals: list[tuple[Bar, Entry, Exit]] = []

    @property
    def strategy_id(self) -> str:
        return self.inner.strategy_id

    @property
    def pyramiding(self) -> int:
        return self.inner.pyramiding

    def on_bar(self, bar: Bar, ctx: StrategyContext) -> Sequence[Action]:
        actions = list(self.inner.on_bar(bar, ctx))
        entries = [a for a in actions if isinstance(a, Entry)]
        if entries:
            (entry,) = entries
            (bracket,) = [a for a in actions if isinstance(a, Exit)]
            self.signals.append((bar, entry, bracket))
        return actions

    def on_fill(self, fill: FillEvent, ctx: StrategyContext) -> Sequence[Action]:
        return self.inner.on_fill(fill, ctx)


def build_series(days: int, moves: Sequence[tuple[int, int, int, int]]) -> BarSeries:
    bars: list[Bar] = []
    price = 5000.0
    for index in range(days * BARS_PER_DAY):
        day, slot = divmod(index, BARS_PER_DAY)
        move, up, down, gap = moves[index % len(moves)]
        open_ = price
        close = max(open_ + move * TICK, 100.0)
        high = max(open_, close) + up * TICK
        low = min(open_, close) - down * TICK
        if high == low:
            high += TICK  # a real bar spans at least one tick
        bars.append(
            Bar(
                ts_open_ms=START_MS + day * DAY_MS + slot * 5 * MINUTE_MS,
                open=open_,
                high=high,
                low=low,
                close=close,
                volume=1.0,
            )
        )
        price = max(close + gap * TICK, 100.0)
    source = DataSource(name="random", format="generic", sha256="", synthetic=True, bar_tz="UTC")
    return BarSeries(symbol="RW", tf_seconds=300, bars=tuple(bars), source=source)


def run(
    series: BarSeries, params: MeanReversionParams, model: FillModel, flags: Sequence[bool]
) -> tuple[EngineResult, Recorder]:
    recorder = Recorder(params)
    config = make_config(instrument=mes(), fill_model=model)
    return run_backtest(series, recorder, config, session_last=flags), recorder


def check_properties(
    series: BarSeries,
    params: MeanReversionParams,
    model: FillModel,
    flags: Sequence[bool],
) -> EngineResult:
    result, recorder = run(series, params, model, flags)  # no EngineError
    bars = series.bars
    end_minute = params.end_hour * 60 + params.end_min

    # The same input gives the same trades twice (fresh instance).
    again, _ = run(series, params, model, flags)
    assert again.trades == result.trades

    # Entries per New York calendar day of the signal bar (the script's day).
    per_day = Counter(CLOCK.parts(bar.ts_open_ms).date for bar, _, _ in recorder.signals)
    assert all(count <= params.max_trades_day for count in per_day.values())

    # Risk at the signal: |estimated entry - estimated stop| x size x point value.
    budget = 100_000.0 * params.risk_pct / 100.0
    for bar, entry, bracket in recorder.signals:
        assert bracket.stop is not None
        risk = abs(bar.close - bracket.stop) * entry.qty * 5.0
        assert risk <= budget * (1 + 1e-9)
        assert entry.qty >= 1.0
        assert entry.qty == math.floor(entry.qty)

    for trade in result.trades:
        # Never left to the end of the data: every session's last bar flattens.
        assert trade.reason is not ExitReason.END_OF_DATA
        # No position is open at the close of a forced-close bar.
        for index in range(trade.entry_bar_index, trade.exit_bar_index):
            minute = CLOCK.parts(bars[index].ts_open_ms).minute_of_day
            assert not flags[index]
            assert minute < end_minute
        # The stop that first comes into force is on the loss side by at least a tick. In
        # the TradingView model a trade closed inside its fill bar never got the re-priced
        # stop (the signal bar's estimate was in force: see the Pine finding in the port).
        finalized = model is FillModel.PESSIMISTIC or trade.exit_bar_index > trade.entry_bar_index
        if finalized:
            loss_side = (trade.entry_price - trade.initial_stop) * trade.side.sign
            assert loss_side >= TICK - 1e-9
    return result


@settings(deadline=None, max_examples=60)
@given(
    days=st.integers(1, 3),
    moves=st.lists(_moves, min_size=20, max_size=120),
    model=st.sampled_from(MODELS),
    max_trades=st.sampled_from((1, 2, 3)),
    trend=st.booleans(),
)
def test_random_series_keep_the_invariants(
    days: int,
    moves: list[tuple[int, int, int, int]],
    model: FillModel,
    max_trades: int,
    trend: bool,
) -> None:
    series = build_series(days, moves)
    flags = [(i + 1) % BARS_PER_DAY == 0 for i in range(len(series))]
    params = MeanReversionParams(max_trades_day=max_trades, use_trend_filter=trend)
    check_properties(series, params, model, flags)


@settings(deadline=None, max_examples=25)
@given(
    moves=st.lists(_moves, min_size=20, max_size=60),
    max_daily_loss=st.sampled_from((0.1, 0.3, 2.0)),
    close_on_halt=st.booleans(),
)
def test_daily_breaker_variants_keep_the_invariants(
    moves: list[tuple[int, int, int, int]], max_daily_loss: float, close_on_halt: bool
) -> None:
    series = build_series(2, moves)
    flags = [(i + 1) % BARS_PER_DAY == 0 for i in range(len(series))]
    params = MeanReversionParams(
        max_daily_loss=max_daily_loss,
        close_on_halt=close_on_halt,
        max_trades_day=3,
        use_trend_filter=False,
    )
    for model in MODELS:
        check_properties(series, params, model, flags)


@pytest.mark.parametrize("name", ["synthetic_5m.csv", "synthetic_1m.csv"])
@pytest.mark.parametrize("model", MODELS)
def test_synthetic_fixtures_run_clean(name: str, model: FillModel) -> None:
    series = load_ohlcv(FIXTURES / name)
    flags = session_last_flags(series.bars, series.tf_seconds)
    result = check_properties(series, MeanReversionParams(), model, flags)
    entries = {trade.seq for trade in result.trades}
    print(f"{name} {model.value}: {len(entries)} entries")
