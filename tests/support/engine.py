"""Builders for backtest-engine tests: bars, instruments, configs and a scripted strategy.

`ScriptedStrategy` returns prepared actions per bar index (or computes them with a
callable) and records every context the engine showed it, so a test can assert both the
fills and what the strategy saw. Strategy-port tests reuse it to drive the engine by hand.
"""

from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass, field
from typing import Final

from propbot.backtest.engine import run_backtest
from propbot.backtest.model import CostModel, EngineConfig, EngineResult, FillModel
from propbot.core.bars import Bar, BarSeries, DataSource
from propbot.core.instrument import Instrument, InstrumentKind
from propbot.strategies.base import Action, Entry, Exit, FillEvent, Side, StrategyContext

# 2026-03-02 14:30 UTC = 09:30 New York (EST): the first bar of a regular session.
START_MS: Final = 1_772_461_800_000
MINUTE_MS: Final = 60_000

# (open, high, low, close); volume is irrelevant to the engine.
type OHLC = tuple[float, float, float, float]
type BarScript = Callable[[int, Bar, StrategyContext], Sequence[Action]]
type FillScript = Callable[[FillEvent, StrategyContext], Sequence[Action]]

FREE: Final = CostModel(commission_per_unit_side=0.0, slippage_ticks=0.0)


def mes(*, tick_size: float = 0.25, point_value: float = 5.0) -> Instrument:
    """A micro E-mini-like futures contract (the Pine header's default)."""
    return Instrument(
        symbol="MES",
        kind=InstrumentKind.FUTURES,
        tick_size=tick_size,
        point_value=point_value,
        qty_step=1.0,
        min_qty=1.0,
    )


def cfd(*, tick_size: float = 0.1, point_value: float = 1.0) -> Instrument:
    return Instrument(
        symbol="US500.cash",
        kind=InstrumentKind.CFD,
        tick_size=tick_size,
        point_value=point_value,
        qty_step=0.01,
        min_qty=0.01,
    )


def make_series(
    rows: Sequence[OHLC], *, start_ms: int = START_MS, tf_seconds: int = 60
) -> BarSeries:
    """Consecutive bars from `start_ms`, one every `tf_seconds`."""
    bars = tuple(
        Bar(
            ts_open_ms=start_ms + index * tf_seconds * 1000,
            open=o,
            high=h,
            low=lo,
            close=c,
            volume=1.0,
        )
        for index, (o, h, lo, c) in enumerate(rows)
    )
    source = DataSource(name="test", format="generic", sha256="", synthetic=True, bar_tz="UTC")
    return BarSeries(symbol="TEST", tf_seconds=tf_seconds, bars=bars, source=source)


def make_config(
    *,
    instrument: Instrument | None = None,
    costs: CostModel = FREE,
    fill_model: FillModel = FillModel.PESSIMISTIC,
    initial_capital: float = 100_000.0,
    limit_through_ticks: int = 1,
) -> EngineConfig:
    return EngineConfig(
        instrument=instrument or mes(),
        costs=costs,
        fill_model=fill_model,
        initial_capital=initial_capital,
        limit_through_ticks=limit_through_ticks,
    )


def run(
    rows: Sequence[OHLC] | BarSeries,
    strategy: "ScriptedStrategy",
    config: EngineConfig | None = None,
    *,
    session_last: Sequence[bool] | None = None,
) -> EngineResult:
    """Run the engine; by default only the last bar ends a session."""
    series = rows if isinstance(rows, BarSeries) else make_series(rows)
    if session_last is None:
        session_last = [index == len(series) - 1 for index in range(len(series))]
    return run_backtest(series, strategy, config or make_config(), session_last=session_last)


def bracket(
    entry_id: str,
    side: Side,
    qty: float,
    *,
    stop: float,
    target: float | None = None,
    limit: float | None = None,
    exit_id: str | None = None,
    comment: str = "",
) -> list[Action]:
    """Pine's usual pair: `strategy.entry` and one `strategy.exit` with stop (and target)."""
    return [
        Entry(entry_id, side, qty, limit=limit, comment=comment),
        Exit(exit_id or f"x-{entry_id}", entry_id, stop=stop, limit=target),
    ]


@dataclass(eq=False)
class ScriptedStrategy:
    """A strategy that plays a script.

    `script` maps a bar index to the actions returned at that bar's close, or is a
    callable `(index, bar, ctx) -> actions`. `fill_script` answers `on_fill`; without it
    `on_fill` returns nothing. `bar_contexts[i]` is the context of bar i's `on_bar`.
    """

    script: Mapping[int, Sequence[Action]] | BarScript = field(default_factory=dict)
    fill_script: FillScript | None = None
    pyramiding: int = 0
    strategy_id: str = "test"
    bar_contexts: list[StrategyContext] = field(default_factory=list)
    fills: list[tuple[FillEvent, StrategyContext]] = field(default_factory=list)

    def on_bar(self, bar: Bar, ctx: StrategyContext) -> Sequence[Action]:
        self.bar_contexts.append(ctx)
        if callable(self.script):
            return self.script(ctx.bar_index, bar, ctx)
        return self.script.get(ctx.bar_index, ())

    def on_fill(self, fill: FillEvent, ctx: StrategyContext) -> Sequence[Action]:
        self.fills.append((fill, ctx))
        if self.fill_script is None:
            return ()
        return self.fill_script(fill, ctx)
