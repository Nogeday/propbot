"""Runner properties: legs partition the engine's entries; leg pnl adds up to the equity."""

import math
from collections.abc import Sequence
from typing import Any

from hypothesis import given, settings
from hypothesis import strategies as st
from pydantic import TypeAdapter

from propbot.backtest.artifacts import Label
from propbot.backtest.model import CostModel, FillModel, group_by_entry
from propbot.backtest.runner import RunRequest, label_suffix, run, run_label
from propbot.core.bars import Bar
from propbot.risk.config import StrictModel
from propbot.strategies.base import Action, Entry, Exit, Side, StrategyContext
from propbot.strategies.registry import StrategySpec
from support.engine import ScriptedStrategy, make_series, mes

LEGS = ("leg_a", "leg_b", "leg_c")
COSTS = CostModel(commission_per_unit_side=0.5, slippage_ticks=1.0)


class Params(StrictModel):
    every: int = 3
    points: float = 1.5
    pattern: str = "0"  # leg index per entry (digits), cycled
    risk_pct: float = 0.5  # the risk input every registry entry names (unused: 1 unit)


def _strategy(params: Params, legs: Sequence[str]) -> ScriptedStrategy:
    count = [0]

    def on_bar(index: int, bar: Bar, ctx: StrategyContext) -> list[Action]:
        if ctx.position_size != 0.0 or index % params.every:
            return []
        leg = legs[int(params.pattern[count[0] % len(params.pattern)]) % len(legs)]
        count[0] += 1
        side = Side.LONG if count[0] % 2 else Side.SHORT
        sign = 1.0 if side is Side.LONG else -1.0
        return [
            Entry(f"e{index}", side, 1.0, strategy_id=leg),
            Exit(
                f"x{index}",
                f"e{index}",
                stop=bar.close - sign * params.points,
                limit=bar.close + sign * params.points,
            ),
        ]

    return ScriptedStrategy(script=on_bar, strategy_id="scripted")


def _spec(legs: Sequence[str]) -> StrategySpec[Any]:
    return StrategySpec(
        strategy_id="scripted",
        pine_file="scripted.pine",
        params_model=Params,
        strategy_class=lambda p: _strategy(p or Params(), legs),
        leg_ids=tuple(legs),
        risk_param="risk_pct",
    )


@st.composite
def bar_rows(draw: st.DrawFn) -> list[tuple[float, float, float, float]]:
    steps = draw(st.lists(st.integers(-8, 8), min_size=2, max_size=60))
    wicks = draw(st.lists(st.integers(0, 4), min_size=len(steps), max_size=len(steps)))
    rows = []
    price = 4000.0
    for step, wick in zip(steps, wicks, strict=True):
        close = price + step * 0.25
        rows.append(
            (price, max(price, close) + wick * 0.25, min(price, close) - wick * 0.25, close)
        )
        price = close
    return rows


@settings(max_examples=60, deadline=None)
@given(
    rows=bar_rows(),
    n_legs=st.integers(1, 3),
    every=st.integers(1, 6),
    points=st.sampled_from([0.75, 1.5, 2.5]),
    pattern=st.lists(st.integers(0, 2), min_size=1, max_size=5),
    fill_model=st.sampled_from(list(FillModel)),
)
def test_leg_entries_partition_the_engine_entries_and_pnl_adds_up_to_equity(
    rows: list[tuple[float, float, float, float]],
    n_legs: int,
    every: int,
    points: float,
    pattern: list[int],
    fill_model: FillModel,
) -> None:
    legs = LEGS[:n_legs]
    request = RunRequest(
        series=make_series(rows),
        strategy_id="scripted",
        instrument=mes(),
        base_costs=COSTS,
        initial_capital=50_000.0,
        oos_start_ms=0,
        overrides={"every": every, "points": points, "pattern": "".join(map(str, pattern))},
        fill_model=fill_model,
    )
    result = run(request, registry={"scripted": _spec(legs)})

    engine_entries = group_by_entry(result.engine.trades)
    assert result.entries == engine_entries
    leg_entries = [entry for leg in result.legs for entry in leg.entries]
    assert sorted(leg_entries, key=lambda e: e.seq) == list(engine_entries)
    assert len({entry.seq for entry in leg_entries}) == len(leg_entries)
    for leg in result.legs:
        assert all(entry.strategy_id == leg.strategy_id for entry in leg.entries)

    leg_pnl = math.fsum(entry.pnl for leg in result.legs for entry in leg.entries)
    expected = result.engine.final_equity - request.initial_capital
    assert math.isclose(leg_pnl, expected, rel_tol=1e-9, abs_tol=1e-6)
    if result.combined is not None:
        assert result.combined.entries == result.entries


@given(
    leg=st.sampled_from(["ny_orb_fvg", "master_asian_sweep", "mtf_trend_cont"]),
    fill_model=st.sampled_from(list(FillModel)),
    cost_free=st.booleans(),
    factor=st.floats(min_value=0.0, max_value=1e6, allow_nan=False, allow_infinity=False),
    pine_risk=st.booleans(),
)
def test_only_the_decision_run_has_the_bare_leg_label_and_every_label_is_valid(
    leg: str, fill_model: FillModel, cost_free: bool, factor: float, pine_risk: bool
) -> None:
    label = run_label(
        leg, fill_model=fill_model, cost_free=cost_free, cost_factor=factor, pine_risk=pine_risk
    )
    # Since 2026-11-19 a run at the script's own risk is a comparison run too (finding 82).
    decision = (
        fill_model is FillModel.PESSIMISTIC and not cost_free and factor == 1.0 and not pine_risk
    )

    assert TypeAdapter(Label).validate_python(label) == label
    assert (label == leg) is decision
    assert label.startswith(leg)
    suffix = label_suffix(
        fill_model=fill_model, cost_free=cost_free, cost_factor=factor, pine_risk=pine_risk
    )
    assert suffix.endswith("-pinerisk") is pine_risk
    assert ("-tv" in suffix) is (fill_model is FillModel.TV_OHLC)
    assert ("-free" in suffix) is cost_free
