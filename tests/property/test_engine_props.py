"""Backtest engine properties over random bars and a random (but contract-abiding) strategy."""

import itertools
import math
from collections.abc import Sequence
from dataclasses import dataclass, field

import pytest
from hypothesis import given, settings
from hypothesis import strategies as st

from propbot.backtest.engine import EngineError
from propbot.backtest.model import CostModel, EngineResult, ExitReason, FillModel
from propbot.core.bars import Bar, BarSeries
from propbot.strategies.base import Action, Cancel, Close, Entry, Exit, Side, StrategyContext
from support.engine import ScriptedStrategy, make_config, make_series, run

TICK = 0.25  # the MES grid of `support.engine.mes`

# Per bar: gap from the previous close, close move, wick above, wick below (ticks).
_bar_steps = st.tuples(
    st.integers(-6, 6), st.integers(-12, 12), st.integers(0, 8), st.integers(0, 8)
)
_dice = st.lists(st.integers(0, 10_000), min_size=80, max_size=80)


def _series(steps: Sequence[tuple[int, int, int, int]]) -> BarSeries:
    rows = []
    close = 1000.0
    for gap, move, up, down in steps:
        open_ = close + gap * TICK
        close = open_ + move * TICK
        rows.append((open_, max(open_, close) + up * TICK, min(open_, close) - down * TICK, close))
    return make_series(rows)


@dataclass(eq=False)
class _RandomTrader:
    """Market and limit entries with a stop (and maybe a target), cancels, closes, trails.

    Decisions depend only on the dice, the bar and whether a position is open, never on
    fill prices, so two runs that differ only in commission and slippage take the same
    decisions on the same bars.
    """

    dice: Sequence[int]
    trail: bool
    stop: float = math.nan
    side: Side = Side.LONG
    stops: list[float] = field(default_factory=list)

    def __call__(self, index: int, bar: Bar, ctx: StrategyContext) -> Sequence[Action]:
        roll = self.dice[index % len(self.dice)]
        if ctx.position_size == 0.0:
            kind = roll % 9
            if kind >= 4:
                return [Cancel("E")] if kind == 8 else []
            self.side = Side.LONG if kind % 2 == 0 else Side.SHORT
            sign = self.side.sign
            limit = bar.close - sign * (roll % 5) * TICK if kind >= 2 else None
            reference = limit if limit is not None else bar.close
            self.stop = reference - sign * (2 + roll % 13) * TICK
            target = reference + sign * (3 + roll % 17) * TICK if roll % 3 else None
            return [
                Entry("E", self.side, 1 + roll % 3, limit=limit, comment="random"),
                Exit("X", "E", stop=self.stop, limit=target),
            ]
        if roll % 11 == 0:
            return [Close("E", "close")]
        if self.trail and roll % 4 == 0:
            sign = self.side.sign
            candidate = bar.close - sign * 4 * TICK
            if (candidate - self.stop) * sign > 0:
                self.stop = candidate
                return [Exit("X", "E", stop=self.stop)]
        return []


def _run(
    steps: Sequence[tuple[int, int, int, int]],
    dice: Sequence[int],
    *,
    model: FillModel,
    costs: CostModel,
    trail: bool = True,
) -> EngineResult:
    strategy = ScriptedStrategy(_RandomTrader(dice, trail))
    return run(_series(steps), strategy, make_config(costs=costs, fill_model=model))


_models = st.sampled_from([FillModel.PESSIMISTIC, FillModel.TV_OHLC])
_cost_models = st.builds(
    lambda commission, slip: CostModel(commission_per_unit_side=commission, slippage_ticks=slip),
    st.sampled_from([0.0, 0.62, 2.5]),
    st.sampled_from([0.0, 1.0, 2.0]),
)
_series_steps = st.lists(_bar_steps, min_size=1, max_size=120)
_FREE = CostModel(commission_per_unit_side=0.0, slippage_ticks=0.0)


@settings(max_examples=60, deadline=None)
@given(_series_steps, _dice, _models, _cost_models)
def test_ledger_adds_up_and_records_are_sane(
    steps: list[tuple[int, int, int, int]],
    dice: list[int],
    model: FillModel,
    costs: CostModel,
) -> None:
    result = _run(steps, dice, model=model, costs=costs)

    total = math.fsum(trade.pnl for trade in result.trades)
    assert math.isclose(result.final_equity, 100_000.0 + total, rel_tol=1e-6)
    for trade in result.trades:
        assert trade.exit_bar_index >= trade.entry_bar_index
        assert trade.exit_ts_ms >= trade.entry_ts_ms
        assert trade.qty > 0.0
        assert math.isclose(trade.pnl, trade.gross_pnl - trade.commission, abs_tol=1e-9)
        assert trade.risk_amount > 0.0
        assert trade.mae_r <= min(0.0, trade.r) + 1e-12
    assert [trade.seq for trade in result.trades] == sorted(t.seq for t in result.trades)


@settings(max_examples=40, deadline=None)
@given(_series_steps, _dice, _models)
def test_untouched_stop_loses_exactly_one_r_without_costs(
    steps: list[tuple[int, int, int, int]], dice: list[int], model: FillModel
) -> None:
    result = _run(steps, dice, model=model, costs=_FREE, trail=False)

    for trade in result.trades:
        if trade.reason is not ExitReason.STOP:
            continue
        sign = trade.side.sign
        entry_above_stop = (trade.entry_price - trade.initial_stop) * sign >= TICK
        if entry_above_stop and trade.exit_price == trade.initial_stop:
            assert math.isclose(trade.r, -1.0, abs_tol=1e-9)
        else:
            # The open gapped to or through the stop, at the exit bar or already at the
            # entry's own fill: the stop filled at that open, never better than the stop.
            assert (trade.exit_price - trade.initial_stop) * sign <= 0.0
            assert trade.r <= 0.0


@settings(max_examples=30, deadline=None)
@given(_series_steps, _dice, _models, _cost_models)
def test_same_inputs_give_the_identical_result(
    steps: list[tuple[int, int, int, int]],
    dice: list[int],
    model: FillModel,
    costs: CostModel,
) -> None:
    first = _run(steps, dice, model=model, costs=costs)
    second = _run(steps, dice, model=model, costs=costs)
    assert first == second


@settings(max_examples=40, deadline=None)
@given(
    _series_steps,
    _dice,
    _models,
    _cost_models,
    st.sampled_from([1.5, 2.0, 3.0]),
)
def test_scaled_commission_and_slippage_never_raise_final_equity(
    steps: list[tuple[int, int, int, int]],
    dice: list[int],
    model: FillModel,
    costs: CostModel,
    factor: float,
) -> None:
    # Without a spread, commission and slippage change fill prices but no trigger (the
    # bid path decides), so the same orders fill on the same bars, each at a price and a
    # fee no better than before. With a spread this does NOT hold: a wider spread can
    # keep a buy limit from filling and so avoid a losing trade.
    base = _run(steps, dice, model=model, costs=costs)
    scaled = _run(steps, dice, model=model, costs=costs.scaled(factor))

    def fills(result: EngineResult) -> list[tuple[int, int, int, ExitReason, float]]:
        return [
            (t.seq, t.entry_bar_index, t.exit_bar_index, t.reason, t.qty) for t in result.trades
        ]

    assert fills(scaled) == fills(base)
    assert scaled.final_equity <= base.final_equity + 1e-9
    for before, after in zip(base.trades, scaled.trades, strict=True):
        assert after.pnl <= before.pnl + 1e-9


# -- invariant 2: the protection profile of a batch ------------------------------------

_QUIET = (1000.0, 1000.0 + TICK, 1000.0 - TICK, 1000.0)
_FILL = 1000.0  # a market entry fills at the next open; costs are free


@st.composite
def _bracket(draw: st.DrawFn) -> tuple[Side, list[tuple[int, int]]]:
    """A side and its exits as (stop distance in ticks, qty_percent) summing to exactly 100.

    Exactly 100 % and no overlap: then every share is the only protection at its level,
    so loosening any of it must show in the profile.
    """
    side = draw(st.sampled_from([Side.LONG, Side.SHORT]))
    count = draw(st.integers(1, 4))
    cuts = sorted(draw(st.lists(st.integers(1, 99), min_size=count - 1, max_size=count - 1)))
    bounds = [0, *cuts, 100]
    percents = [high - low for low, high in itertools.pairwise(bounds)]
    distances = draw(st.lists(st.integers(8, 40), min_size=count, max_size=count))
    return side, [(d, p) for d, p in zip(distances, percents, strict=True) if p > 0]


def _stop(side: Side, ticks: int) -> float:
    return _FILL - side.sign * ticks * TICK


def _guard_run(side: Side, initial: list[tuple[int, int]], batch: list[Action]) -> None:
    script: dict[int, Sequence[Action]] = {
        0: [
            Entry("E", side, 4),
            *(
                Exit(f"x{i}", "E", stop=_stop(side, d), qty_percent=p)
                for i, (d, p) in enumerate(initial)
            ),
        ],
        2: batch,
    }
    run(make_series([_QUIET] * 4), ScriptedStrategy(script), make_config())


@settings(max_examples=80, deadline=None)
@given(_bracket(), st.data())
def test_batches_that_loosen_no_share_never_trip_the_stop_guard(
    bracket: tuple[Side, list[tuple[int, int]]], data: st.DataObject
) -> None:
    side, initial = bracket
    exits = {f"x{i}": [d, p] for i, (d, p) in enumerate(initial)}
    batch: list[Action] = []
    # Tighten stops (never closer than 4 ticks to the market, so nothing fills).
    for exit_id, share in exits.items():
        share[0] -= data.draw(st.integers(0, share[0] - 4), label=f"tighten {exit_id}")
    # Move quantity from a looser exit to a tighter (or equal) one.
    ordered = sorted(exits, key=lambda exit_id: exits[exit_id][0])
    if len(ordered) > 1 and data.draw(st.booleans(), label="shift"):
        tight, loose = ordered[0], ordered[-1]
        moved = data.draw(st.integers(0, exits[loose][1] - 1), label="moved")
        exits[tight][1] += moved
        exits[loose][1] -= moved
    # Replace one exit by another id at the same or a tighter stop.
    replaced = data.draw(st.sampled_from(sorted(exits)), label="replaced")
    if data.draw(st.booleans(), label="replace"):
        batch.append(Cancel(replaced))
        exits[f"r-{replaced}"] = exits.pop(replaced)
    # Maybe an extra overlapping exit: more protection, never less.
    if data.draw(st.booleans(), label="extra"):
        batch.append(Exit("extra", "E", stop=_stop(side, 6), qty_percent=50))
    batch.extend(
        Exit(exit_id, "E", stop=_stop(side, d), qty_percent=p) for exit_id, (d, p) in exits.items()
    )
    _guard_run(side, initial, batch)


@settings(max_examples=80, deadline=None)
@given(_bracket(), st.data())
def test_a_batch_that_loosens_any_share_always_trips_the_stop_guard(
    bracket: tuple[Side, list[tuple[int, int]]], data: st.DataObject
) -> None:
    side, initial = bracket
    index = data.draw(st.integers(0, len(initial) - 1), label="loosened exit")
    distance, percent = initial[index]
    widen = data.draw(st.integers(1, 20), label="ticks")
    part = data.draw(st.integers(1, percent), label="loosened percent")
    batch: list[Action] = []
    if part == percent and data.draw(st.booleans(), label="new id"):
        # The whole exit replaced by a looser one under another id (Init -> Run).
        batch += [
            Cancel(f"x{index}"),
            Exit("moved", "E", stop=_stop(side, distance + widen), qty_percent=part),
        ]
    else:
        # Part (or all) of the share goes to a looser stop; the rest stays.
        if part < percent:
            batch.append(
                Exit(f"x{index}", "E", stop=_stop(side, distance), qty_percent=percent - part)
            )
        else:
            batch.append(Cancel(f"x{index}"))
        batch.append(Exit("moved", "E", stop=_stop(side, distance + widen), qty_percent=part))
    with pytest.raises(EngineError, match=r"bar 2: order 'E': .*invariant 2"):
        _guard_run(side, initial, batch)
