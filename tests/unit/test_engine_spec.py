"""Black-box checks of the backtest engine, written from the contract alone.

Every expectation below was computed by hand from ADR-0008, the module docstring of
`propbot.strategies.base`, `propbot.backtest.model` and SPEC §3.1 / §6.2 / §6.3, before
the engine's code was read. Prices are on the MES grid (tick 0.25, point value 5) unless
a test builds a CFD; there the tick is 0.1 and some levels are deliberately chosen where
`level + tick` is not equal to the next grid price in binary floating point.
"""

import datetime as dt
import math
from collections.abc import Sequence

import pytest

from propbot.backtest.engine import EngineError
from propbot.backtest.model import (
    CostModel,
    EngineResult,
    ExitReason,
    FillModel,
    SpreadWindow,
    TradeRecord,
    group_by_entry,
)
from propbot.core.bars import Bar
from propbot.strategies.base import (
    Action,
    Cancel,
    Close,
    CloseAll,
    ClosedTrade,
    Entry,
    Exit,
    FillEvent,
    OpenTrade,
    Side,
    StrategyContext,
)
from support.engine import (
    FREE,
    MINUTE_MS,
    OHLC,
    ScriptedStrategy,
    bracket,
    cfd,
    make_config,
    make_series,
    run,
)

P = FillModel.PESSIMISTIC
TV = FillModel.TV_OHLC
BOTH = pytest.mark.parametrize("model", [P, TV])
FLAT: OHLC = (100.0, 100.0, 100.0, 100.0)
# Bar 1 of most scenarios: a market entry fills at its open, 100, and nothing triggers.
QUIET: OHLC = (100.0, 100.5, 99.5, 100.0)


def only(result: EngineResult) -> TradeRecord:
    assert len(result.trades) == 1, result.trades
    return result.trades[0]


def cfd_costs(*, spread: float = 0.5, slip: float = 1.0) -> CostModel:
    return CostModel(commission_per_unit_side=0.0, slippage_ticks=slip, spread_points=spread)


def long_at_100(
    *rest: OHLC, model: FillModel = P, stop: float = 98.0, target: float | None = 104.0
) -> EngineResult:
    """Long 1 MES: signal at bar 0, filled at 100 at bar 1's open, then `rest`."""
    strategy = ScriptedStrategy({0: bracket("L", Side.LONG, 1, stop=stop, target=target)})
    return run([FLAT, QUIET, *rest], strategy, make_config(fill_model=model))


def short_at_100(
    *rest: OHLC, model: FillModel = P, stop: float = 102.0, target: float | None = 96.0
) -> EngineResult:
    strategy = ScriptedStrategy({0: bracket("S", Side.SHORT, 1, stop=stop, target=target)})
    return run([FLAT, QUIET, *rest], strategy, make_config(fill_model=model))


# -- market entry, targets and the one-tick-through rule --------------------------------


@BOTH
def test_spec_market_long_to_target_exact_numbers(model: FillModel) -> None:
    trade = only(long_at_100((100.0, 104.25, 99.0, 104.0), FLAT, model=model))
    assert (trade.entry_price, trade.exit_price) == (100.0, 104.0)
    assert (trade.entry_bar_index, trade.exit_bar_index) == (1, 2)
    assert trade.reason is ExitReason.TARGET
    assert trade.exit_id == "x-L"
    assert trade.pnl == 20.0
    assert trade.initial_stop == 98.0
    assert trade.risk_amount == 10.0
    assert trade.r == 2.0
    # The worst floating point was bar 2's low (bar 2 walks O-L-H-C in both models).
    assert trade.mae_r == -0.5


@BOTH
def test_spec_target_exactly_touched_does_not_fill(model: FillModel) -> None:
    trade = only(
        long_at_100((100.0, 104.0, 99.5, 103.0), (103.0, 103.0, 103.0, 103.0), model=model)
    )
    assert trade.reason is ExitReason.END_OF_DATA
    assert trade.exit_id == ""
    assert trade.exit_price == 103.0
    assert trade.exit_bar_index == 3


@BOTH
def test_spec_target_gapped_through_fills_at_the_better_open(model: FillModel) -> None:
    trade = only(long_at_100((106.0, 107.0, 105.0, 106.5), FLAT, model=model))
    assert trade.exit_price == 106.0
    assert trade.reason is ExitReason.TARGET
    assert trade.r == 3.0


@BOTH
def test_spec_open_less_than_one_tick_through_a_target_is_no_gap_fill(model: FillModel) -> None:
    # The open (off the grid) is beyond 104 but not one tick through it; the high never
    # trades through either: no fill.
    trade = only(long_at_100((104.1, 104.2, 103.0, 103.5), (103.5,) * 4, model=model))
    assert trade.reason is ExitReason.END_OF_DATA


@BOTH
def test_spec_open_less_than_one_tick_through_then_through_fills_at_the_limit(
    model: FillModel,
) -> None:
    # Safest reading: no gap fill at the better open 104.1; the order fills at its own
    # level once price trades a full tick through it.
    trade = only(long_at_100((104.1, 104.5, 103.0, 103.5), FLAT, model=model))
    assert trade.reason is ExitReason.TARGET
    assert trade.exit_price == 104.0


@BOTH
def test_spec_stop_gapped_through_fills_at_the_worse_open_and_mae_stops_there(
    model: FillModel,
) -> None:
    trade = only(long_at_100((96.0, 97.0, 95.0, 96.5), FLAT, model=model))
    assert trade.exit_price == 96.0
    assert trade.reason is ExitReason.STOP
    assert trade.pnl == -20.0
    assert trade.r == -2.0
    # The bar's low 95 came after the exit: it is not part of the trade's excursion.
    assert trade.mae_r == -2.0


@BOTH
def test_spec_stop_exactly_touched_fills_at_the_stop(model: FillModel) -> None:
    trade = only(long_at_100((99.0, 99.5, 98.0, 99.0), FLAT, model=model))
    assert trade.exit_price == 98.0
    assert trade.reason is ExitReason.STOP
    assert trade.r == -1.0


# -- stop and target in one bar ---------------------------------------------------------


@pytest.mark.parametrize(
    ("model", "bar", "reason", "exit_price"),
    [
        (P, (103.0, 104.5, 97.5, 102.0), ExitReason.STOP, 98.0),
        (TV, (103.0, 104.5, 97.5, 102.0), ExitReason.TARGET, 104.0),  # open nearer high
        (TV, (99.0, 104.5, 97.5, 102.0), ExitReason.STOP, 98.0),  # open nearer low
        (TV, (101.0, 104.5, 97.5, 102.0), ExitReason.STOP, 98.0),  # equidistant: O-L-H-C
    ],
)
def test_spec_long_bar_touching_stop_and_target(
    model: FillModel, bar: OHLC, reason: ExitReason, exit_price: float
) -> None:
    trade = only(long_at_100(bar, FLAT, model=model))
    assert (trade.reason, trade.exit_price) == (reason, exit_price)


@pytest.mark.parametrize(
    ("model", "bar", "reason", "exit_price"),
    [
        (P, (99.0, 102.5, 95.5, 99.0), ExitReason.STOP, 102.0),  # short: high first
        (TV, (99.0, 102.5, 95.5, 99.0), ExitReason.TARGET, 96.0),  # equidistant: low first
        (TV, (101.5, 102.5, 95.5, 99.0), ExitReason.STOP, 102.0),  # nearer high
        (TV, (97.0, 102.5, 95.5, 99.0), ExitReason.TARGET, 96.0),  # nearer low
    ],
)
def test_spec_short_bar_touching_stop_and_target(
    model: FillModel, bar: OHLC, reason: ExitReason, exit_price: float
) -> None:
    trade = only(short_at_100(bar, FLAT, model=model))
    assert (trade.reason, trade.exit_price) == (reason, exit_price)
    assert trade.pnl == (100.0 - exit_price) * 5.0


@pytest.mark.parametrize(
    ("model", "bar", "reason", "exit_price"),
    [
        (P, (100.0, 104.5, 97.5, 101.0), ExitReason.STOP, 98.0),
        (TV, (100.0, 104.5, 97.5, 101.0), ExitReason.STOP, 98.0),  # nearer low
        (P, (103.0, 104.5, 97.5, 101.0), ExitReason.STOP, 98.0),
        (TV, (103.0, 104.5, 97.5, 101.0), ExitReason.TARGET, 104.0),  # nearer high
        (P, (100.0, 104.5, 99.0, 103.0), ExitReason.TARGET, 104.0),  # no stop inside
    ],
)
def test_spec_market_entry_bar_also_hits_its_bracket(
    model: FillModel, bar: OHLC, reason: ExitReason, exit_price: float
) -> None:
    strategy = ScriptedStrategy({0: bracket("L", Side.LONG, 1, stop=98.0, target=104.0)})
    result = run([FLAT, bar, FLAT], strategy, make_config(fill_model=model))
    trade = only(result)
    assert trade.entry_bar_index == trade.exit_bar_index == 1
    assert trade.entry_price == bar[0]
    assert (trade.reason, trade.exit_price) == (reason, exit_price)


# -- limit entries ----------------------------------------------------------------------


@BOTH
def test_spec_limit_entry_touched_is_no_fill_one_tick_through_fills(model: FillModel) -> None:
    strategy = ScriptedStrategy(
        {0: bracket("L", Side.LONG, 1, stop=97.0, target=103.0, limit=99.0)}
    )
    rows = [FLAT, (100.0, 100.5, 99.0, 99.5), (99.5, 100.0, 98.75, 99.0), (99.0,) * 4]
    trade = only(run(rows, strategy, make_config(fill_model=model)))
    assert (trade.entry_bar_index, trade.entry_price) == (2, 99.0)
    assert trade.reason is ExitReason.END_OF_DATA
    assert trade.initial_stop == 97.0


@BOTH
def test_spec_limit_entry_gapped_through_fills_at_the_better_open(model: FillModel) -> None:
    strategy = ScriptedStrategy(
        {0: bracket("L", Side.LONG, 1, stop=97.0, target=103.0, limit=99.0)}
    )
    trade = only(run([FLAT, (98.0, 98.5, 97.5, 98.0), (98.0,) * 4], strategy))
    assert (trade.entry_bar_index, trade.entry_price) == (1, 98.0)
    assert trade.risk_amount == 5.0


@pytest.mark.parametrize(
    ("model", "close", "reason", "exit_price"),
    [
        # Pessimistic, flat with a long limit: O-H-L-C. The fill is on the way down, the
        # target can only fill on the final leg to the close.
        (P, 104.5, ExitReason.TARGET, 104.0),
        (P, 104.0, ExitReason.END_OF_DATA, 104.0),
        # TV: open nearer the low, O-L-H-C: filled at 100, then the high reaches 105.
        (TV, 104.5, ExitReason.TARGET, 104.0),
        (TV, 104.0, ExitReason.TARGET, 104.0),
    ],
)
def test_spec_limit_entry_bar_favourable_first_and_final_leg(
    model: FillModel, close: float, reason: ExitReason, exit_price: float
) -> None:
    strategy = ScriptedStrategy(
        {0: bracket("L", Side.LONG, 1, stop=98.0, target=104.0, limit=100.0)}
    )
    rows = [(101.0,) * 4, (101.0, 105.0, 99.5, close), (close,) * 4]
    trade = only(run(rows, strategy, make_config(fill_model=model)))
    assert (trade.entry_bar_index, trade.entry_price) == (1, 100.0)
    assert (trade.reason, trade.exit_price) == (reason, exit_price)


def test_spec_pessimistic_limit_entry_bar_runs_on_into_the_stop() -> None:
    strategy = ScriptedStrategy(
        {0: bracket("L", Side.LONG, 1, stop=98.0, target=104.0, limit=100.0)}
    )
    rows = [(101.0,) * 4, (101.0, 105.0, 97.5, 99.0), FLAT]
    trade = only(run(rows, strategy))
    assert (trade.entry_price, trade.exit_price, trade.reason) == (100.0, 98.0, ExitReason.STOP)
    assert trade.entry_bar_index == trade.exit_bar_index == 1


def test_spec_pessimistic_short_limit_entry_walks_low_first_then_target_on_last_leg() -> None:
    strategy = ScriptedStrategy(
        {0: bracket("S", Side.SHORT, 1, stop=104.0, target=98.0, limit=102.0)}
    )
    rows = [(101.0,) * 4, (101.0, 103.0, 97.0, 97.5), (97.5,) * 4]
    trade = only(run(rows, strategy))
    assert (trade.entry_price, trade.exit_price, trade.reason) == (
        102.0,
        98.0,
        ExitReason.TARGET,
    )
    assert trade.pnl == 20.0


# -- several exits of one entry ---------------------------------------------------------


def _two_exit_bracket(qty: float, *, tp_pct: float = 50.0, init_pct: float = 50.0) -> list[Action]:
    return [
        Entry("L", Side.LONG, qty),
        Exit("TP1", "L", stop=98.0, limit=102.0, qty_percent=tp_pct),
        Exit("Init", "L", stop=98.0, qty_percent=init_pct),
    ]


@BOTH
def test_spec_two_exits_sharing_a_stop_close_half_each(model: FillModel) -> None:
    strategy = ScriptedStrategy({0: _two_exit_bracket(2)})
    result = run(
        [FLAT, QUIET, (100.0, 101.0, 97.5, 98.0), FLAT], strategy, make_config(fill_model=model)
    )
    assert len(result.trades) == 2
    assert sorted(t.exit_id for t in result.trades) == ["Init", "TP1"]
    for trade in result.trades:
        assert (trade.qty, trade.exit_price, trade.pnl, trade.r) == (1.0, 98.0, -10.0, -1.0)
    (entry,) = group_by_entry(result.trades)
    assert (entry.qty, entry.pnl, entry.r) == (2.0, -20.0, -1.0)


@BOTH
def test_spec_runner_exit_replaces_init_after_tp1(model: FillModel) -> None:
    runner = Exit("Init", "L", stop=100.0, limit=106.0, qty_percent=100.0)
    strategy = ScriptedStrategy({0: _two_exit_bracket(2), 2: [runner]})
    rows = [FLAT, QUIET, (100.0, 102.5, 99.5, 102.0), (102.0, 106.5, 101.0, 106.0), FLAT]
    result = run(rows, strategy, make_config(fill_model=model))
    tp1, init = result.trades
    assert (tp1.exit_id, tp1.qty, tp1.exit_price, tp1.pnl, tp1.r) == ("TP1", 1.0, 102.0, 10.0, 1.0)
    assert (init.exit_id, init.qty, init.exit_price, init.pnl, init.r) == (
        "Init",
        1.0,
        106.0,
        30.0,
        3.0,
    )
    assert init.initial_stop == 98.0  # moving the stop later does not change the risk unit
    (entry,) = group_by_entry(result.trades)
    assert (entry.pnl, entry.risk_amount, entry.r) == (40.0, 20.0, 2.0)


@BOTH
def test_spec_qty_percent_is_of_the_filled_size_capped_by_what_is_left(model: FillModel) -> None:
    strategy = ScriptedStrategy({0: _two_exit_bracket(4, tp_pct=50.0, init_pct=75.0)})
    rows = [FLAT, QUIET, (100.0, 102.5, 99.5, 101.0), (101.0, 101.0, 97.5, 98.0), FLAT]
    tp1, init = run(rows, strategy, make_config(fill_model=model)).trades
    assert (tp1.exit_id, tp1.qty, tp1.exit_price) == ("TP1", 2.0, 102.0)
    # 75 % of the filled 4 is 3, but only 2 are left.
    assert (init.exit_id, init.qty, init.exit_price) == ("Init", 2.0, 98.0)


@BOTH
def test_spec_commission_split_across_partial_exits_sums_to_the_total(model: FillModel) -> None:
    commission = 0.625  # exact in binary
    strategy = ScriptedStrategy({0: _two_exit_bracket(4, tp_pct=25.0, init_pct=100.0)})
    rows = [FLAT, QUIET, (100.0, 102.5, 99.5, 101.0), (101.0, 101.0, 97.5, 98.0), FLAT]
    config = make_config(
        fill_model=model,
        costs=CostModel(commission_per_unit_side=commission, slippage_ticks=0.0),
    )
    tp1, init = run(rows, strategy, config).trades
    assert (tp1.qty, tp1.commission, tp1.gross_pnl, tp1.pnl) == (1.0, 1.25, 10.0, 8.75)
    assert (init.qty, init.commission, init.gross_pnl, init.pnl) == (3.0, 3.75, -30.0, -33.75)
    assert tp1.commission + init.commission == 4 * commission * 2
    assert tp1.r == 8.75 / 10.0


# -- Close / CloseAll and entry ids -----------------------------------------------------


@BOTH
def test_spec_close_one_entry_while_another_stays_open(model: FillModel) -> None:
    actions = [*bracket("A", Side.LONG, 1, stop=98.0), *bracket("B", Side.LONG, 1, stop=98.0)]
    strategy = ScriptedStrategy({0: actions, 1: [Close("A", "flat A")]}, pyramiding=2)
    rows = [FLAT, (100.0, 101.5, 99.5, 101.0), (101.0, 101.5, 100.5, 101.0), (101.0,) * 4]
    result = run(rows, strategy, make_config(fill_model=model))
    first, last = result.trades
    assert (first.entry_id, first.exit_id, first.reason) == ("A", "", ExitReason.CLOSE)
    assert (first.exit_bar_index, first.exit_price, first.comment) == (1, 101.0, "flat A")
    assert (last.entry_id, last.reason, last.exit_bar_index) == ("B", ExitReason.END_OF_DATA, 3)
    ctx = strategy.bar_contexts[2]
    assert ctx.position_size == 1.0
    assert [t.entry_id for t in ctx.open_trades] == ["B"]
    assert [t.entry_id for t in ctx.closed_trades] == ["A"]


@BOTH
def test_spec_close_all_leaves_a_working_limit_entry_with_its_bracket(model: FillModel) -> None:
    later = bracket("B", Side.LONG, 1, stop=97.0, target=103.0, limit=99.0)
    strategy = ScriptedStrategy(
        {0: bracket("A", Side.LONG, 1, stop=98.0), 1: [*later, CloseAll("flat")]}, pyramiding=2
    )
    rows = [
        FLAT,
        (100.0, 100.5, 99.75, 100.25),
        (100.0, 100.0, 98.5, 99.0),
        (99.0, 99.0, 96.5, 97.0),
        FLAT,
    ]
    a, b = run(rows, strategy, make_config(fill_model=model)).trades
    assert (a.entry_id, a.reason, a.exit_price, a.exit_bar_index) == (
        "A",
        ExitReason.CLOSE,
        100.25,
        1,
    )
    assert (b.entry_id, b.entry_bar_index, b.entry_price) == ("B", 2, 99.0)
    assert (b.reason, b.exit_price, b.exit_bar_index) == (ExitReason.STOP, 97.0, 3)


@BOTH
def test_spec_limit_entry_without_any_exit_filling_after_close_all_is_an_error(
    model: FillModel,
) -> None:
    strategy = ScriptedStrategy(
        {
            0: bracket("A", Side.LONG, 1, stop=98.0),
            1: [Entry("B", Side.LONG, 1, limit=99.0), CloseAll("flat")],
        },
        pyramiding=2,
    )
    rows = [FLAT, (100.0, 100.5, 99.75, 100.25), (100.0, 100.0, 98.5, 99.0), FLAT]
    with pytest.raises(EngineError):
        run(rows, strategy, make_config(fill_model=model))


@BOTH
def test_spec_reused_entry_id_does_not_inherit_the_stale_exit(model: FillModel) -> None:
    strategy = ScriptedStrategy(
        {0: bracket("L", Side.LONG, 1, stop=98.0, target=101.0), 3: [Entry("L", Side.LONG, 1)]}
    )
    rows = [FLAT, QUIET, (100.0, 101.5, 99.5, 101.0), FLAT, FLAT, FLAT]
    with pytest.raises(EngineError):
        run(rows, strategy, make_config(fill_model=model))


@BOTH
def test_spec_reused_entry_id_with_a_fresh_exit_is_a_new_trade(model: FillModel) -> None:
    strategy = ScriptedStrategy(
        {
            0: bracket("L", Side.LONG, 1, stop=98.0, target=101.0),
            3: bracket("L", Side.LONG, 1, stop=99.0, target=101.0),
        }
    )
    rows = [
        FLAT,
        QUIET,
        (100.0, 101.5, 99.5, 101.0),
        FLAT,
        QUIET,
        (100.0, 101.5, 99.5, 101.0),
        FLAT,
    ]
    first, second = run(rows, strategy, make_config(fill_model=model)).trades
    assert (first.seq, first.initial_stop, first.r) == (0, 98.0, 0.5)
    assert (second.seq, second.initial_stop, second.r) == (1, 99.0, 1.0)
    assert second.entry_bar_index == 4


# -- invariant 2: stops ----------------------------------------------------------------


def test_spec_pessimistic_on_fill_may_widen_the_provisional_stop_then_it_is_locked() -> None:
    def refit(fill: FillEvent, ctx: StrategyContext) -> Sequence[Action]:
        return [Exit("x-L", "L", stop=97.0)]

    strategy = ScriptedStrategy({0: bracket("L", Side.LONG, 1, stop=98.0)}, fill_script=refit)
    trade = only(run([FLAT, QUIET, (100.0, 100.0, 96.5, 97.0), FLAT], strategy))
    assert (trade.initial_stop, trade.exit_price, trade.r) == (97.0, 97.0, -1.0)

    widen_later = ScriptedStrategy(
        {0: bracket("L", Side.LONG, 1, stop=98.0), 1: [Exit("x-L", "L", stop=96.5)]},
        fill_script=refit,
    )
    with pytest.raises(EngineError):
        run([FLAT, QUIET, QUIET, FLAT], widen_later)


def test_spec_pessimistic_widening_at_the_fill_bar_close_is_an_error() -> None:
    strategy = ScriptedStrategy(
        {0: bracket("L", Side.LONG, 1, stop=98.0), 1: [Exit("x-L", "L", stop=97.0)]}
    )
    with pytest.raises(EngineError):
        run([FLAT, QUIET, QUIET, FLAT], strategy)


def test_spec_tv_fill_bar_close_may_widen_the_stop_then_it_is_locked() -> None:
    config = make_config(fill_model=TV)
    ok = ScriptedStrategy(
        {0: bracket("L", Side.LONG, 1, stop=98.0), 1: [Exit("x-L", "L", stop=97.0)]}
    )
    trade = only(run([FLAT, QUIET, (100.0, 100.0, 96.5, 97.0), FLAT], ok, config))
    assert (trade.initial_stop, trade.exit_price, trade.r) == (97.0, 97.0, -1.0)
    assert ok.fills == []

    late = ScriptedStrategy(
        {
            0: bracket("L", Side.LONG, 1, stop=98.0),
            1: [Exit("x-L", "L", stop=97.0)],
            2: [Exit("x-L", "L", stop=96.5)],
        }
    )
    with pytest.raises(EngineError):
        run([FLAT, QUIET, QUIET, QUIET, FLAT], late, config)


@BOTH
def test_spec_widening_while_the_entry_still_works_is_allowed(model: FillModel) -> None:
    strategy = ScriptedStrategy(
        {
            0: bracket("L", Side.LONG, 1, stop=98.0, limit=99.0),
            1: [Exit("x-L", "L", stop=97.0)],
        }
    )
    rows = [FLAT, QUIET, (99.5, 99.5, 98.5, 99.0), (99.0,) * 4]
    trade = only(run(rows, strategy, make_config(fill_model=model)))
    assert (trade.entry_price, trade.initial_stop) == (99.0, 97.0)


@BOTH
def test_spec_cancelling_the_only_stop_is_an_error(model: FillModel) -> None:
    strategy = ScriptedStrategy({0: bracket("L", Side.LONG, 1, stop=98.0), 1: [Cancel("x-L")]})
    with pytest.raises(EngineError):
        run([FLAT, QUIET, QUIET, FLAT], strategy, make_config(fill_model=model))


@BOTH
def test_spec_long_stop_raised_above_the_market_fills_at_the_next_open(model: FillModel) -> None:
    strategy = ScriptedStrategy(
        {0: bracket("L", Side.LONG, 1, stop=98.0), 2: [Exit("x-L", "L", stop=101.0)]}
    )
    rows = [FLAT, QUIET, (100.0, 100.75, 99.5, 100.5), (100.5, 102.0, 100.25, 101.5), FLAT]
    trade = only(run(rows, strategy, make_config(fill_model=model)))
    assert (trade.exit_bar_index, trade.exit_price, trade.reason) == (3, 100.5, ExitReason.STOP)
    assert trade.initial_stop == 98.0
    assert trade.r == 0.25


def test_spec_tv_stop_moved_beyond_the_entry_at_the_fill_bar_close_is_the_initial_stop() -> None:
    # ADR-0008: in tv_ohlc the initial stop is the one in force after the fill bar's
    # on_bar, even when that call already moved it past the entry.
    strategy = ScriptedStrategy(
        {0: bracket("L", Side.LONG, 1, stop=98.0), 1: [Exit("x-L", "L", stop=101.0)]}
    )
    rows = [FLAT, (100.0, 100.75, 99.5, 100.5), (100.5, 102.0, 100.25, 101.5), FLAT]
    trade = only(run(rows, strategy, make_config(fill_model=TV)))
    assert (trade.exit_price, trade.initial_stop, trade.risk_amount) == (100.5, 101.0, 5.0)


@BOTH
def test_spec_reversal_entry_is_an_error(model: FillModel) -> None:
    strategy = ScriptedStrategy(
        {0: bracket("L", Side.LONG, 1, stop=98.0), 1: bracket("S", Side.SHORT, 1, stop=102.0)},
        pyramiding=2,
    )
    with pytest.raises(EngineError):
        run([FLAT, QUIET, QUIET, FLAT], strategy, make_config(fill_model=model))


def test_spec_entry_beyond_pyramiding_is_dropped_and_counted() -> None:
    strategy = ScriptedStrategy(
        {0: bracket("A", Side.LONG, 1, stop=98.0), 1: bracket("B", Side.LONG, 1, stop=98.0)}
    )
    result = run([FLAT, QUIET, QUIET, FLAT], strategy)
    assert result.dropped_entries == 1
    assert [t.entry_id for t in result.trades] == ["A"]


# -- re-pricing on fill -----------------------------------------------------------------


@pytest.mark.parametrize(
    ("model", "reason"), [(P, ExitReason.END_OF_DATA), (TV, ExitReason.TARGET)]
)
def test_spec_on_fill_reprices_the_target_inside_the_fill_bar_only_when_pessimistic(
    model: FillModel, reason: ExitReason
) -> None:
    def refit(fill: FillEvent, ctx: StrategyContext) -> Sequence[Action]:
        return [Exit("x-L", "L", stop=fill.price - 2.0, limit=fill.price + 4.0)]

    strategy = ScriptedStrategy(
        {0: bracket("L", Side.LONG, 1, stop=98.0, target=104.0)}, fill_script=refit
    )
    rows = [FLAT, (100.5, 104.5, 100.25, 104.0), (104.0,) * 4]
    trade = only(run(rows, strategy, make_config(fill_model=model)))
    assert trade.reason is reason
    assert trade.entry_price == 100.5
    if model is P:
        assert trade.initial_stop == 98.5
    else:
        assert trade.exit_price == 104.0


# -- what the strategy sees -------------------------------------------------------------


def test_spec_equity_around_a_fill_and_a_partial_exit() -> None:
    strategy = ScriptedStrategy({0: _two_exit_bracket(2)})
    rows = [FLAT, (100.0, 101.0, 99.5, 101.0), (101.0, 102.5, 100.5, 101.5), (101.5,) * 4]
    result = run(rows, strategy)
    ctx0, ctx1, ctx2, ctx3 = strategy.bar_contexts
    assert (ctx0.equity, ctx0.position_size, ctx0.position_avg_price) == (100_000.0, 0.0, None)
    assert (ctx1.equity, ctx1.position_size, ctx1.position_avg_price) == (100_010.0, 2.0, 100.0)
    assert (ctx2.net_profit, ctx2.equity, ctx2.position_size) == (10.0, 100_017.5, 1.0)
    fill_ts = make_series(rows).bars[1].ts_open_ms
    assert list(ctx2.open_trades) == [OpenTrade("L", "test", Side.LONG, 1.0, 100.0, 1, fill_ts)]
    assert len(ctx2.closed_trades) == 1
    closed: ClosedTrade = ctx2.closed_trades[0]
    assert (closed.exit_id, closed.qty, closed.exit_price, closed.profit) == (
        "TP1",
        1.0,
        102.0,
        10.0,
    )
    assert ctx3.session_last_bar is True
    assert ctx2.session_last_bar is False
    (fill, fill_ctx) = strategy.fills[0]
    assert (fill.entry_id, fill.qty, fill.price, fill.bar_index) == ("L", 2.0, 100.0, 1)
    assert (fill_ctx.equity, fill_ctx.position_size) == (100_000.0, 2.0)
    assert result.final_equity == 100_017.5


def test_spec_end_of_data_closes_at_the_last_close_outside_the_statistics() -> None:
    result = long_at_100((100.0, 101.0, 99.5, 100.75))
    trade = only(result)
    (entry,) = group_by_entry(result.trades)
    assert (trade.reason, trade.exit_price, trade.exit_bar_index) == (
        ExitReason.END_OF_DATA,
        100.75,
        2,
    )
    assert entry.complete is False
    assert result.final_equity == 100_003.75


def test_spec_empty_series() -> None:
    strategy = ScriptedStrategy()
    result = run([], strategy)
    assert (result.trades, result.bars, result.first_ts_ms, result.last_ts_ms) == (
        (),
        0,
        None,
        None,
    )
    assert result.final_equity == 100_000.0
    assert strategy.bar_contexts == []


def test_spec_one_bar_series_never_fills_what_it_asks_for() -> None:
    strategy = ScriptedStrategy({0: bracket("L", Side.LONG, 1, stop=98.0)})
    result = run([FLAT], strategy)
    assert (result.trades, result.bars, result.final_equity) == ((), 1, 100_000.0)
    assert len(strategy.bar_contexts) == 1
    assert strategy.bar_contexts[0].session_last_bar is True


def test_spec_session_last_length_mismatch_is_refused() -> None:
    with pytest.raises((ValueError, EngineError)):
        run([FLAT, FLAT], ScriptedStrategy(), session_last=[True])


# -- CFD bid/ask: the eight execution kinds --------------------------------------------


def _cfd_run(
    rows: Sequence[OHLC], script: dict[int, Sequence[Action]], spread: float = 0.5
) -> EngineResult:
    config = make_config(instrument=cfd(), costs=cfd_costs(spread=spread))
    return run(rows, ScriptedStrategy(script), config)


def test_spec_cfd_buy_market_and_sell_close() -> None:
    script = {0: bracket("L", Side.LONG, 1, stop=95.0), 1: [CloseAll("c")]}
    trade = only(_cfd_run([FLAT, (100.0, 101.0, 100.0, 101.0), FLAT], script))
    assert trade.entry_price == pytest.approx(100.6)  # open + spread + 1 tick
    assert trade.exit_price == pytest.approx(100.9)  # bid close - 1 tick


def test_spec_cfd_sell_market_and_buy_close() -> None:
    script = {0: bracket("S", Side.SHORT, 1, stop=105.0), 1: [CloseAll("c")]}
    trade = only(_cfd_run([FLAT, (100.0, 100.0, 99.0, 99.0), FLAT], script))
    assert trade.entry_price == pytest.approx(99.9)
    assert trade.exit_price == pytest.approx(99.6)  # 99 + 0.5 + 0.1


@pytest.mark.parametrize(("high", "filled"), [(101.5, True), (101.4, False)])
def test_spec_cfd_buy_stop_triggers_on_the_ask(high: float, filled: bool) -> None:
    script = {0: bracket("S", Side.SHORT, 1, stop=102.0)}
    trade = only(_cfd_run([FLAT, QUIET, (100.0, high, 99.8, 100.0), FLAT], script))
    if filled:
        assert (trade.reason, trade.exit_price) == (ExitReason.STOP, pytest.approx(102.1))
    else:
        assert trade.reason is ExitReason.END_OF_DATA


def test_spec_cfd_buy_stop_gapped_fills_at_the_ask_open() -> None:
    script = {0: bracket("S", Side.SHORT, 1, stop=102.0)}
    trade = only(_cfd_run([FLAT, QUIET, (103.0, 103.0, 102.5, 103.0), FLAT], script))
    assert trade.exit_price == pytest.approx(103.6)


def test_spec_cfd_sell_stop_triggers_on_the_bid() -> None:
    script = {0: bracket("L", Side.LONG, 1, stop=98.0)}
    trade = only(_cfd_run([FLAT, QUIET, (100.0, 100.2, 98.0, 99.0), FLAT], script))
    assert (trade.reason, trade.exit_price) == (ExitReason.STOP, pytest.approx(97.9))


@pytest.mark.parametrize(("low", "filled"), [(98.5, False), (98.4, True)])
def test_spec_cfd_buy_limit_entry_fills_when_the_ask_is_a_tick_through(
    low: float, filled: bool
) -> None:
    script = {0: bracket("L", Side.LONG, 1, stop=97.0, limit=99.0)}
    result = _cfd_run([FLAT, (100.0, 100.2, low, 99.0), (99.0,) * 4], script)
    if filled:
        assert only(result).entry_price == 99.0  # a limit pays no spread, no slippage
    else:
        assert result.trades == ()


@pytest.mark.parametrize(("high", "filled"), [(102.0, False), (102.1, True)])
def test_spec_cfd_sell_limit_target_on_the_bid(high: float, filled: bool) -> None:
    script = {0: bracket("L", Side.LONG, 1, stop=95.0, target=102.0)}
    trade = only(_cfd_run([FLAT, QUIET, (100.0, high, 99.8, 100.0), FLAT], script))
    if filled:
        assert (trade.reason, trade.exit_price) == (ExitReason.TARGET, 102.0)
    else:
        assert trade.reason is ExitReason.END_OF_DATA


@pytest.mark.parametrize(("low", "filled"), [(97.5, False), (97.4, True)])
def test_spec_cfd_buy_limit_target_of_a_short_on_the_ask(low: float, filled: bool) -> None:
    script = {0: bracket("S", Side.SHORT, 1, stop=105.0, target=98.0)}
    trade = only(_cfd_run([FLAT, QUIET, (100.0, 100.2, low, 99.0), FLAT], script))
    if filled:
        assert (trade.reason, trade.exit_price) == (ExitReason.TARGET, 98.0)
    else:
        assert trade.reason is ExitReason.END_OF_DATA


def test_spec_cfd_sell_limit_entry_on_the_bid() -> None:
    script = {0: bracket("S", Side.SHORT, 1, stop=103.0, limit=101.0)}
    touched = _cfd_run([FLAT, (100.0, 101.0, 99.8, 100.0), FLAT], script)
    assert touched.trades == ()
    through = _cfd_run([FLAT, (100.0, 101.1, 99.8, 100.0), (100.0,) * 4], script)
    assert only(through).entry_price == 101.0


@pytest.mark.parametrize(
    ("day", "expected"),
    [
        # 13:30 UTC is 08:30 New York before the DST change (EST) ...
        (dt.date(2026, 3, 6), 100.5 + 0.1),
        # ... and 09:30 after it (EDT): the opening window applies.
        (dt.date(2026, 3, 9), 102.0 + 0.1),
    ],
)
def test_spec_spread_window_by_new_york_minute_across_dst(day: dt.date, expected: float) -> None:
    start = dt.datetime(day.year, day.month, day.day, 13, 29, tzinfo=dt.UTC)
    start_ms = int(start.timestamp()) * 1000
    costs = CostModel(
        commission_per_unit_side=0.0,
        slippage_ticks=1.0,
        spread_points=0.5,
        spread_windows=(SpreadWindow(start_minute=570, end_minute=575, spread_points=2.0),),
    )
    series = make_series([FLAT, FLAT, FLAT], start_ms=start_ms)
    strategy = ScriptedStrategy({0: bracket("L", Side.LONG, 1, stop=90.0)})
    trade = only(run(series, strategy, make_config(instrument=cfd(), costs=costs)))
    assert trade.entry_ts_ms == start_ms + MINUTE_MS
    assert trade.entry_price == pytest.approx(expected)


# -- float robustness on the tick grid --------------------------------------------------


@BOTH
def test_spec_one_tick_through_is_robust_to_float_error(model: FillModel) -> None:
    # 4096.1 + 0.1 > 4096.2 in binary floating point; the high IS one tick through.
    base: OHLC = (4096.0, 4096.0, 4096.0, 4096.0)
    script = {0: bracket("L", Side.LONG, 1, stop=4090.0, target=4096.1)}
    config = make_config(instrument=cfd(), costs=FREE, fill_model=model)
    rows = [base, base, (4096.0, 4096.2, 4095.9, 4096.0), base]
    trade = only(run(rows, ScriptedStrategy(script), config))
    assert (trade.reason, trade.exit_price) == (ExitReason.TARGET, 4096.1)


@BOTH
def test_spec_short_target_one_tick_through_is_robust_to_float_error(model: FillModel) -> None:
    base: OHLC = (4097.0, 4097.0, 4097.0, 4097.0)
    script = {0: bracket("S", Side.SHORT, 1, stop=4100.0, target=4096.2)}
    config = make_config(instrument=cfd(), costs=FREE, fill_model=model)
    rows = [base, base, (4097.0, 4097.0, 4096.1, 4096.5), base]
    trade = only(run(rows, ScriptedStrategy(script), config))
    assert (trade.reason, trade.exit_price) == (ExitReason.TARGET, 4096.2)


@BOTH
def test_spec_limit_entry_one_tick_through_is_robust_to_float_error(model: FillModel) -> None:
    base: OHLC = (4097.0, 4097.0, 4097.0, 4097.0)
    script = {0: bracket("L", Side.LONG, 1, stop=4090.0, limit=4096.2)}
    config = make_config(instrument=cfd(), costs=FREE, fill_model=model)
    rows = [base, (4097.0, 4097.0, 4096.1, 4096.5), (4096.5,) * 4]
    trade = only(run(rows, ScriptedStrategy(script), config))
    assert trade.entry_price == 4096.2


# -- engine hygiene ---------------------------------------------------------------------


def test_spec_context_sequences_cannot_be_mutated_by_the_strategy() -> None:
    def vandal(index: int, bar: Bar, ctx: StrategyContext) -> Sequence[Action]:
        for seq in (ctx.open_trades, ctx.closed_trades):
            assert not isinstance(seq, list), "the engine hands out its own mutable list"
        return bracket("L", Side.LONG, 1, stop=98.0, target=101.0) if index == 0 else ()

    run([FLAT, QUIET, (100.0, 101.5, 99.5, 101.0), FLAT], ScriptedStrategy(vandal))


def test_spec_two_runs_are_independent_and_identical() -> None:
    def script() -> ScriptedStrategy:
        return ScriptedStrategy(
            {
                0: bracket("L", Side.LONG, 1, stop=98.0, target=101.0),
                3: bracket("M", Side.LONG, 1, stop=98.0),
            }
        )

    rows = [FLAT, QUIET, (100.0, 101.5, 99.5, 101.0), FLAT, QUIET, FLAT]
    config = make_config()
    first = run(rows, script(), config)
    second = run(rows, script(), config)
    assert first == second
    assert [t.seq for t in second.trades] == [0, 1]


def test_spec_closed_trades_seen_by_the_strategy_grow_in_fill_order() -> None:
    def script(index: int, bar: Bar, ctx: StrategyContext) -> Sequence[Action]:
        if index % 3 == 0:
            return bracket(f"L{index}", Side.LONG, 1, stop=98.0, target=101.0)
        return ()

    hit: OHLC = (100.0, 101.5, 99.5, 101.0)
    rows = [FLAT, QUIET, hit] * 5 + [FLAT]
    strategy = ScriptedStrategy(script)
    result = run(rows, strategy)
    assert len(result.trades) == 5
    last = strategy.bar_contexts[-1].closed_trades
    assert [t.entry_id for t in last] == [f"L{3 * k}" for k in range(5)]
    assert math.fsum(t.profit for t in last) == 25.0


def test_spec_closed_trades_handed_out_earlier_never_change() -> None:
    def script(index: int, bar: Bar, ctx: StrategyContext) -> Sequence[Action]:
        if index % 3 == 0:
            return bracket(f"L{index}", Side.LONG, 1, stop=98.0, target=101.0)
        return ()

    hit: OHLC = (100.0, 101.5, 99.5, 101.0)
    strategy = ScriptedStrategy(script)
    run([FLAT, QUIET, hit] * 3 + [FLAT], strategy)
    early = strategy.bar_contexts[2].closed_trades
    final = strategy.bar_contexts[-1].closed_trades
    assert len(early) == 1
    assert len(final) == 3
    assert early == tuple(final[:1])
    assert final[-1].entry_id == "L6"
    assert final[-3] == final[0] == early[0]
    assert [t.entry_id for t in final[1:]] == ["L3", "L6"]
    with pytest.raises(IndexError):
        early[1]
    with pytest.raises(IndexError):
        final[-4]


# -- same price, float noise --------------------------------------------------------------


@pytest.mark.parametrize(
    ("instrument_kind", "stop", "limit", "base", "low"),
    [
        ("mes", 98.0, 98.25, 100.0, 97.5),
        # 1024.2 - 0.1 > 1024.1 in binary: the entry's trigger would come a few ulps
        # before the stop if distances were compared exactly.
        ("cfd", 1024.1, 1024.2, 1025.0, 1024.0),
    ],
)
def test_spec_stop_fills_before_an_entry_at_the_same_price(
    instrument_kind: str, stop: float, limit: float, base: float, low: float
) -> None:
    flat: OHLC = (base,) * 4
    strategy = ScriptedStrategy(
        {
            0: bracket("A", Side.LONG, 1, stop=stop),
            1: bracket("B", Side.LONG, 1, stop=low - 1.0, limit=limit),
        }
    )
    instrument = cfd() if instrument_kind == "cfd" else None
    config = make_config(instrument=instrument)
    rows = [flat, flat, (base, base, low, low), (low,) * 4]
    result = run(rows, strategy, config)
    assert result.dropped_entries == 0
    a, b = result.trades
    assert (a.entry_id, a.reason, a.exit_price, a.exit_bar_index) == ("A", ExitReason.STOP, stop, 2)
    assert (b.entry_id, b.entry_price, b.entry_bar_index) == ("B", limit, 2)


def test_spec_tv_equidistant_open_is_a_tie_despite_float_noise() -> None:
    # 4096.2 - 4096.1 < 4096.1 - 4096.0 in binary; the distances are equal, so the
    # tie rule (low first) applies and the long is stopped.
    config = make_config(instrument=cfd(), fill_model=TV, limit_through_ticks=0)
    strategy = ScriptedStrategy({0: bracket("L", Side.LONG, 1, stop=4096.0, target=4096.2)})
    rows = [(4096.1,) * 4, (4096.1, 4096.2, 4096.0, 4096.1), (4096.1,) * 4]
    trade = only(run(rows, strategy, config))
    assert (trade.reason, trade.exit_price) == (ExitReason.STOP, 4096.0)
