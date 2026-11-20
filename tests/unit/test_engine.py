"""Backtest engine: one small scenario per order-life-cycle rule (ADR-0008, strategies.base).

Prices are on the MES grid (tick 0.25, point value 5) unless a test says otherwise, so
every expected number below is exact in binary floating point.
"""

import math
from collections.abc import Callable, Sequence

import pytest

from propbot.backtest.engine import EngineError
from propbot.backtest.model import (
    CostModel,
    ExitReason,
    FillModel,
    SpreadWindow,
    group_by_entry,
)
from propbot.core.bars import Bar
from propbot.strategies.base import (
    Action,
    Cancel,
    Close,
    CloseAll,
    Entry,
    Exit,
    FillEvent,
    Side,
    StrategyContext,
)
from support.engine import (
    FREE,
    MINUTE_MS,
    START_MS,
    ScriptedStrategy,
    bracket,
    cfd,
    make_config,
    make_series,
    run,
)

BOTH = pytest.mark.parametrize("model", [FillModel.PESSIMISTIC, FillModel.TV_OHLC])
FLAT = (100.0, 100.5, 99.5, 100.0)


def costs(*, commission: float = 0.0, slip: float = 0.0, spread: float = 0.0) -> CostModel:
    return CostModel(commission_per_unit_side=commission, slippage_ticks=slip, spread_points=spread)


# -- market fills, costs and what the strategy sees ------------------------------------


@BOTH
def test_market_entry_fills_at_next_open_with_slippage_and_commission(model: FillModel) -> None:
    strategy = ScriptedStrategy({0: bracket("L", Side.LONG, 2, stop=95.0, target=110.0)})
    config = make_config(costs=costs(commission=0.5, slip=1), fill_model=model)
    rows = [FLAT, (100.5, 101.0, 100.0, 100.75), (101.0, 111.0, 100.5, 110.5)]

    result = run(rows, strategy, config)

    (trade,) = result.trades
    assert trade.entry_price == 100.75  # open 100.5 + one tick
    assert trade.entry_bar_index == 1
    assert trade.exit_price == 110.0  # a limit fill: no slippage
    assert trade.reason is ExitReason.TARGET
    assert trade.exit_id == "x-L"
    assert trade.gross_pnl == (110.0 - 100.75) * 2 * 5
    assert trade.commission == 2.0
    assert trade.pnl == 90.5
    assert trade.initial_stop == 95.0
    assert trade.risk_amount == 57.5
    assert trade.r == pytest.approx(90.5 / 57.5)
    # Worst bid while open: bar 1's low 100; the paid entry commission counts.
    assert trade.mae_r == pytest.approx(-8.5 / 57.5)
    assert result.final_equity == 100_090.5

    at_fill_bar = strategy.bar_contexts[1]
    assert at_fill_bar.position_size == 2.0
    assert at_fill_bar.position_avg_price == 100.75
    assert at_fill_bar.net_profit == 0.0
    assert at_fill_bar.equity == 100_000.0  # close 100.75 = entry; commissions not marked
    (open_trade,) = at_fill_bar.open_trades
    assert (open_trade.entry_id, open_trade.qty, open_trade.entry_bar_index) == ("L", 2.0, 1)
    after_exit = strategy.bar_contexts[2]
    assert after_exit.position_size == 0.0
    assert after_exit.position_avg_price is None
    assert after_exit.open_trades == ()
    assert after_exit.net_profit == 90.5
    assert after_exit.equity == 100_090.5
    assert [t.profit for t in after_exit.closed_trades] == [90.5]
    assert after_exit.closed_trades[0].exit_id == "x-L"


def test_on_fill_context_reflects_the_position_right_after_the_fill() -> None:
    strategy = ScriptedStrategy({0: bracket("L", Side.LONG, 2, stop=95.0)})
    config = make_config(costs=costs(commission=0.5, slip=1))

    run([FLAT, (100.5, 101.0, 100.0, 100.75)], strategy, config)

    ((fill, ctx),) = strategy.fills
    assert fill == FillEvent("L", Side.LONG, 2.0, 100.75, 1, START_MS + MINUTE_MS)
    assert ctx.bar_index == 1
    assert ctx.position_size == 2.0
    assert ctx.position_avg_price == 100.75
    assert ctx.equity == 100_000.0 + (100.5 - 100.75) * 2 * 5  # marked at the open's bid


def test_tv_ohlc_never_calls_on_fill() -> None:
    strategy = ScriptedStrategy({0: bracket("L", Side.LONG, 1, stop=95.0)})
    run([FLAT, FLAT], strategy, make_config(fill_model=FillModel.TV_OHLC))
    assert strategy.fills == []


# -- stops and targets -------------------------------------------------------------------


@BOTH
@pytest.mark.parametrize(
    ("side", "stop", "gap_bar", "expected_exit"),
    [
        (Side.LONG, 95.0, (94.0, 96.0, 93.0, 95.0), 93.75),
        (Side.SHORT, 105.0, (106.0, 107.0, 104.0, 105.0), 106.25),
    ],
)
def test_stop_gapped_at_the_open_fills_at_the_worse_open(
    model: FillModel, side: Side, stop: float, gap_bar: tuple[float, ...], expected_exit: float
) -> None:
    strategy = ScriptedStrategy({0: bracket("E", side, 1, stop=stop)})
    config = make_config(costs=costs(slip=1), fill_model=model)

    result = run([FLAT, (100.0, 101.0, 99.0, 100.0), gap_bar], strategy, config)  # type: ignore[list-item]

    (trade,) = result.trades
    assert trade.reason is ExitReason.STOP
    assert trade.exit_bar_index == 2
    assert trade.exit_price == expected_exit
    assert trade.pnl == (expected_exit - trade.entry_price) * side.sign * 5


@BOTH
def test_stop_inside_the_bar_fills_at_the_stop_plus_slippage(model: FillModel) -> None:
    strategy = ScriptedStrategy({0: bracket("S", Side.SHORT, 1, stop=102.0)})
    config = make_config(costs=costs(slip=2), fill_model=model)

    result = run([FLAT, FLAT, (100.0, 102.0, 99.5, 101.0)], strategy, config)

    (trade,) = result.trades
    assert trade.entry_price == 99.5  # open 100 - two ticks
    assert trade.exit_price == 102.5
    assert trade.r == pytest.approx(-3.0 / 2.5)


@BOTH
def test_target_needs_one_tick_through_and_a_touch_is_no_fill(model: FillModel) -> None:
    strategy = ScriptedStrategy({0: bracket("L", Side.LONG, 1, stop=90.0, target=110.0)})
    rows = [FLAT, FLAT, (101.0, 110.0, 100.0, 105.0), (105.0, 110.25, 104.0, 108.0)]

    result = run(rows, strategy, make_config(fill_model=model))

    (trade,) = result.trades
    assert trade.exit_bar_index == 3
    assert trade.exit_price == 110.0
    assert trade.reason is ExitReason.TARGET


def test_without_the_through_rule_a_touch_fills() -> None:
    strategy = ScriptedStrategy({0: bracket("L", Side.LONG, 1, stop=90.0, target=110.0)})
    rows = [FLAT, FLAT, (101.0, 110.0, 100.0, 105.0)]
    result = run(rows, strategy, make_config(limit_through_ticks=0))
    assert [(t.exit_bar_index, t.exit_price) for t in result.trades] == [(2, 110.0)]


@BOTH
def test_target_gapped_at_the_open_fills_at_the_better_open(model: FillModel) -> None:
    strategy = ScriptedStrategy({0: bracket("L", Side.LONG, 1, stop=90.0, target=110.0)})
    config = make_config(costs=costs(slip=4), fill_model=model)
    result = run([FLAT, FLAT, (112.0, 113.0, 111.0, 112.0)], strategy, config)
    assert [(t.exit_price, t.reason) for t in result.trades] == [(112.0, ExitReason.TARGET)]


@pytest.mark.parametrize(
    ("model", "bar", "reason", "exit_price"),
    [
        (FillModel.PESSIMISTIC, (109.0, 111.0, 94.0, 100.0), ExitReason.STOP, 95.0),
        # TradingView: the open is nearer to the high, so the high comes first.
        (FillModel.TV_OHLC, (109.0, 111.0, 94.0, 100.0), ExitReason.TARGET, 110.0),
        (FillModel.TV_OHLC, (96.0, 111.0, 94.0, 100.0), ExitReason.STOP, 95.0),
        # A tie goes to the low.
        (FillModel.TV_OHLC, (102.5, 111.0, 94.0, 100.0), ExitReason.STOP, 95.0),
    ],
)
def test_bar_touching_stop_and_target(
    model: FillModel, bar: tuple[float, ...], reason: ExitReason, exit_price: float
) -> None:
    strategy = ScriptedStrategy({0: bracket("L", Side.LONG, 1, stop=95.0, target=110.0)})
    result = run([FLAT, FLAT, bar], strategy, make_config(fill_model=model))  # type: ignore[list-item]
    assert [(t.reason, t.exit_price) for t in result.trades] == [(reason, exit_price)]


def test_pessimistic_short_sees_the_high_first() -> None:
    strategy = ScriptedStrategy({0: bracket("S", Side.SHORT, 1, stop=105.0, target=90.0)})
    result = run([FLAT, FLAT, (91.0, 106.0, 89.0, 95.0)], strategy, make_config())
    assert [(t.reason, t.exit_price) for t in result.trades] == [(ExitReason.STOP, 105.0)]


@BOTH
def test_stop_on_the_wrong_side_of_the_market_fills_at_the_next_open(model: FillModel) -> None:
    def script(index: int, bar: Bar, ctx: StrategyContext) -> Sequence[Action]:
        if index == 0:
            return bracket("L", Side.LONG, 1, stop=95.0)
        if index == 1:
            return [Exit("x-L", "L", stop=105.0)]  # above the close of 104
        return []

    strategy = ScriptedStrategy(script)
    config = make_config(costs=costs(slip=1), fill_model=model)
    rows = [FLAT, (100.0, 104.5, 99.5, 104.0), (103.5, 104.0, 103.0, 103.5)]

    result = run(rows, strategy, config)

    (trade,) = result.trades
    assert (trade.exit_bar_index, trade.exit_price, trade.reason) == (2, 103.25, ExitReason.STOP)


# -- limit entries -----------------------------------------------------------------------


def _limit_long_strategy() -> ScriptedStrategy:
    return ScriptedStrategy({0: bracket("L", Side.LONG, 1, limit=100.0, stop=98.0, target=102.0)})


@pytest.mark.parametrize(
    ("model", "expected"),
    [
        # Flat with a long limit: high first, the fill on the way down, and the close
        # (101) does not reach the target; bar 2 stays below 102.25 too.
        (FillModel.PESSIMISTIC, (1, 2, 102.0, ExitReason.END_OF_DATA)),
        # TradingView: the open is nearer to the low, so down (fill) then up through the
        # target inside the fill bar.
        (FillModel.TV_OHLC, (1, 1, 102.0, ExitReason.TARGET)),
    ],
)
def test_limit_entry_fill_bar_follows_the_fill_model(
    model: FillModel, expected: tuple[int, int, float, ExitReason]
) -> None:
    # Bar 0 trades through the level but the order is only issued at its close.
    rows = [(101.0, 101.5, 99.0, 101.0), (101.0, 103.0, 99.5, 101.0), (102.0, 102.0, 102.0, 102.0)]

    result = run(rows, _limit_long_strategy(), make_config(fill_model=model))

    (trade,) = result.trades
    assert (trade.entry_bar_index, trade.exit_bar_index, trade.exit_price, trade.reason) == expected
    assert trade.entry_price == 100.0  # a limit fills at its level


def test_pessimistic_limit_entry_target_only_on_the_final_leg_to_the_close() -> None:
    rows = [(101.0, 101.5, 100.5, 101.0), (101.0, 103.0, 99.5, 102.5)]
    result = run(rows, _limit_long_strategy(), make_config())
    got = [(t.entry_bar_index, t.exit_bar_index, t.exit_price, t.reason) for t in result.trades]
    assert got == [(1, 1, 102.0, ExitReason.TARGET)]


def test_pessimistic_walk_follows_the_position_held_at_the_open_even_if_it_gapped_out() -> None:
    # "A" is stopped out by the gap at the open; the bar still walks long-adverse (low
    # first), so "B" fills on the way down and its target on the way up.
    script: dict[int, Sequence[Action]] = {
        0: bracket("A", Side.LONG, 1, stop=95.0),
        1: bracket("B", Side.LONG, 1, limit=93.0, stop=90.0, target=96.0),
    }
    rows = [FLAT, FLAT, (94.0, 97.0, 92.0, 94.5)]

    result = run(rows, ScriptedStrategy(script, pyramiding=2))

    assert [(t.entry_id, t.reason, t.exit_price, t.exit_bar_index) for t in result.trades] == [
        ("A", ExitReason.STOP, 94.0, 2),
        ("B", ExitReason.TARGET, 96.0, 2),
    ]


@BOTH
def test_limit_entry_needs_one_tick_through(model: FillModel) -> None:
    rows = [(101.0, 101.5, 100.5, 101.0), (100.5, 101.0, 100.0, 100.5)]
    result = run(rows, _limit_long_strategy(), make_config(fill_model=model))
    assert result.trades == ()


@BOTH
def test_limit_entry_gapped_at_the_open_fills_at_the_better_open(model: FillModel) -> None:
    rows = [(101.0, 101.5, 100.5, 101.0), (99.0, 99.5, 98.5, 99.0)]
    result = run(rows, _limit_long_strategy(), make_config(fill_model=model))
    assert [(t.entry_price, t.reason) for t in result.trades] == [(99.0, ExitReason.END_OF_DATA)]


# -- re-pricing at the fill ----------------------------------------------------------------


def _bracket_from(price: float) -> Exit:
    """The strategies' shared re-pricing: stop 3 below, target 6 above the fill."""
    return Exit("x-L", "L", stop=price - 3.0, limit=price + 6.0)


@pytest.mark.parametrize("model", [FillModel.PESSIMISTIC, FillModel.TV_OHLC])
def test_bracket_is_repriced_on_fill_or_at_the_fill_bar_close(model: FillModel) -> None:
    def script(index: int, bar: Bar, ctx: StrategyContext) -> Sequence[Action]:
        if index == 0:
            return bracket("L", Side.LONG, 1, stop=95.0, target=110.0)
        if ctx.position_avg_price is not None:
            return [_bracket_from(ctx.position_avg_price)]
        return []

    strategy = ScriptedStrategy(script, fill_script=lambda fill, ctx: [_bracket_from(fill.price)])
    rows = [FLAT, (100.0, 106.5, 99.0, 106.0), (106.0, 106.5, 105.0, 106.0)]

    result = run(rows, strategy, make_config(fill_model=model))

    (trade,) = result.trades
    assert (trade.initial_stop, trade.exit_price, trade.r) == (97.0, 106.0, 2.0)
    if model is FillModel.PESSIMISTIC:
        assert len(strategy.fills) == 1
        assert trade.exit_bar_index == 1  # the re-priced target was live inside the fill bar
    else:
        assert strategy.fills == []
        assert trade.exit_bar_index == 2  # Pine: re-priced only at the fill bar's close


@pytest.mark.parametrize(
    ("model", "initial_stop", "r"),
    [(FillModel.PESSIMISTIC, 95.0, -0.4), (FillModel.TV_OHLC, 98.0, -1.0)],
)
def test_bracket_reissue_moves_the_stop(model: FillModel, initial_stop: float, r: float) -> None:
    script: dict[int, Sequence[Action]] = {
        0: bracket("L", Side.LONG, 1, stop=95.0),
        1: [Exit("x-L", "L", stop=98.0, comment="trail")],
    }
    strategy = ScriptedStrategy(script)
    rows = [FLAT, (100.0, 101.0, 99.0, 100.0), (99.0, 100.0, 97.5, 98.0)]

    result = run(rows, strategy, make_config(fill_model=model))

    (trade,) = result.trades
    assert (trade.exit_price, trade.reason) == (98.0, ExitReason.STOP)
    # The initial stop is the one in force after the first callback that follows the fill.
    assert trade.initial_stop == initial_stop
    assert trade.r == pytest.approx(r)


# -- partial exits -------------------------------------------------------------------------


def _scaled_script(index: int, bar: Bar, ctx: StrategyContext) -> Sequence[Action]:
    if index == 0:
        return [
            Entry("L", Side.LONG, 2, comment="Long"),
            Exit("TP1", "L", stop=95.0, limit=105.0, qty_percent=50, comment="TP1"),
            Exit("Init", "L", stop=95.0, qty_percent=50, comment="Init stop"),
        ]
    if ctx.position_size == 1.0:
        return [Cancel("Init"), Exit("Run", "L", stop=100.0, comment="Runner")]
    return []


@BOTH
def test_partial_exit_then_runner_at_breakeven(model: FillModel) -> None:
    strategy = ScriptedStrategy(_scaled_script)
    config = make_config(costs=costs(commission=1.0), fill_model=model)
    rows = [
        FLAT,
        (100.0, 101.0, 99.5, 100.5),
        (100.5, 105.5, 100.0, 105.0),
        (104.0, 104.5, 99.5, 100.0),
    ]

    result = run(rows, strategy, config)

    tp1, runner = result.trades
    assert (tp1.exit_id, tp1.qty, tp1.exit_price, tp1.reason) == ("TP1", 1.0, 105.0, "target")
    assert (tp1.commission, tp1.pnl, tp1.r) == (2.0, 23.0, 23.0 / 25.0)
    assert (runner.exit_id, runner.qty, runner.exit_price) == ("Run", 1.0, 100.0)
    assert (runner.comment, runner.reason, runner.pnl) == ("Runner", ExitReason.STOP, -2.0)
    assert tp1.seq == runner.seq == 0
    assert tp1.setup == runner.setup == "Long"
    (entry,) = group_by_entry(result.trades)
    assert (entry.qty, entry.pnl, entry.risk_amount) == (2.0, 21.0, 50.0)
    seen = strategy.bar_contexts[2]
    assert seen.position_size == 1.0
    assert [(t.exit_id, t.qty, t.profit) for t in seen.closed_trades] == [("TP1", 1.0, 23.0)]
    assert seen.open_trades[0].qty == 1.0


@BOTH
def test_two_exits_of_one_entry_on_the_same_stop_both_fill(model: FillModel) -> None:
    strategy = ScriptedStrategy(_scaled_script)
    rows = [FLAT, (100.0, 101.0, 99.5, 100.5), (99.0, 100.0, 94.0, 95.0)]

    result = run(rows, strategy, make_config(fill_model=model))

    assert [(t.exit_id, t.qty, t.exit_price, t.reason) for t in result.trades] == [
        ("TP1", 1.0, 95.0, ExitReason.STOP),
        ("Init", 1.0, 95.0, ExitReason.STOP),
    ]
    assert [t.r for t in result.trades] == [-1.0, -1.0]


# -- Close / CloseAll / Cancel / pyramiding ------------------------------------------------


def _two_legs() -> list[Action]:
    return [
        *bracket("S1 Long", Side.LONG, 1, stop=90.0, comment="S1"),
        *bracket("S2 Long", Side.LONG, 1, stop=90.0, comment="S2"),
    ]


@BOTH
def test_close_one_of_two_entries_then_close_all(model: FillModel) -> None:
    script: dict[int, Sequence[Action]] = {
        0: _two_legs(),
        1: [Close("S1 Long", "S1 EOD")],
        2: [CloseAll("flat")],
    }
    strategy = ScriptedStrategy(script, pyramiding=2)
    config = make_config(costs=costs(slip=1), fill_model=model)
    rows = [FLAT, (100.0, 101.0, 99.0, 100.5), (100.5, 102.0, 100.0, 101.0), FLAT]

    result = run(rows, strategy, config)

    assert [(t.entry_id, t.reason, t.comment, t.exit_bar_index) for t in result.trades] == [
        ("S1 Long", ExitReason.CLOSE, "S1 EOD", 1),
        ("S2 Long", ExitReason.CLOSE, "flat", 2),
    ]
    assert [t.entry_price for t in result.trades] == [100.25, 100.25]
    assert [t.exit_price for t in result.trades] == [100.25, 100.75]  # close - one tick
    assert [t.seq for t in result.trades] == [0, 1]
    before_close = strategy.bar_contexts[1]
    assert before_close.position_size == 2.0
    assert [t.entry_id for t in before_close.open_trades] == ["S1 Long", "S2 Long"]
    after_close = strategy.bar_contexts[2]
    assert [t.entry_id for t in after_close.open_trades] == ["S2 Long"]
    assert [t.entry_id for t in after_close.closed_trades] == ["S1 Long"]
    assert result.dropped_entries == 0


def test_entry_beyond_the_pyramiding_limit_is_dropped() -> None:
    strategy = ScriptedStrategy({0: _two_legs()}, pyramiding=0)
    result = run([FLAT, FLAT, FLAT], strategy)
    assert result.dropped_entries == 1
    assert [t.entry_id for t in result.trades] == ["S1 Long"]


@BOTH
def test_close_all_leaves_a_working_limit_entry(model: FillModel) -> None:
    script: dict[int, Sequence[Action]] = {
        0: [
            *bracket("A", Side.LONG, 1, stop=90.0),
            *bracket("B", Side.LONG, 1, limit=95.0, stop=90.0),
        ],
        1: [CloseAll("halt")],
    }
    strategy = ScriptedStrategy(script, pyramiding=2)
    rows = [FLAT, (100.0, 101.0, 99.0, 100.0), (99.0, 99.5, 94.0, 95.0)]

    result = run(rows, strategy, make_config(fill_model=model))

    assert [(t.entry_id, t.reason, t.entry_price, t.exit_price) for t in result.trades] == [
        ("A", ExitReason.CLOSE, 100.0, 100.0),
        ("B", ExitReason.END_OF_DATA, 95.0, 95.0),
    ]


def test_cancel_removes_a_working_entry_and_its_bracket() -> None:
    script: dict[int, Sequence[Action]] = {
        0: bracket("B", Side.LONG, 1, limit=95.0, stop=90.0),
        1: [Cancel("B"), Cancel("unknown")],
    }
    rows = [FLAT, (100.0, 101.0, 99.0, 100.0), (99.0, 99.5, 90.0, 91.0)]
    result = run(rows, ScriptedStrategy(script))
    assert result.trades == ()


def test_reissued_entry_replaces_the_working_one() -> None:
    script: dict[int, Sequence[Action]] = {
        0: bracket("B", Side.LONG, 1, limit=95.0, stop=90.0),
        1: [Entry("B", Side.LONG, 3, limit=99.0)],
    }
    rows = [FLAT, (100.0, 101.0, 99.5, 100.0), (99.5, 99.5, 98.5, 99.0)]
    result = run(rows, ScriptedStrategy(script))
    assert [(t.qty, t.entry_price) for t in result.trades] == [(3.0, 99.0)]


def test_exit_without_its_entry_is_discarded_after_the_batch() -> None:
    # The stale exit must not protect an entry that re-uses the id later.
    script: dict[int, Sequence[Action]] = {
        0: [Exit("x-L", "L", stop=95.0)],
        1: [Entry("L", Side.LONG, 1)],
    }
    with pytest.raises(EngineError, match=r"bar 2: order 'L': .*no exit with a stop"):
        run([FLAT, FLAT, FLAT], ScriptedStrategy(script))


# -- CFD: bid bars, ask side, spread windows -------------------------------------------------


@BOTH
def test_cfd_long_entry_pays_the_ask_and_is_marked_at_the_bid(model: FillModel) -> None:
    strategy = ScriptedStrategy({0: bracket("L", Side.LONG, 1, stop=90.0)})
    config = make_config(instrument=cfd(), costs=costs(spread=0.5), fill_model=model)

    result = run([FLAT, FLAT], strategy, config)

    (trade,) = result.trades
    assert trade.entry_price == 100.5
    assert trade.exit_price == 100.0  # end of data sells at the bid
    assert trade.pnl == pytest.approx(-0.5)
    assert strategy.bar_contexts[1].equity == pytest.approx(99_999.5)


@BOTH
def test_cfd_short_stop_triggers_on_the_ask(model: FillModel) -> None:
    rows = [FLAT, FLAT, (100.0, 100.6, 99.8, 100.2)]

    def strategy() -> ScriptedStrategy:
        return ScriptedStrategy({0: bracket("S", Side.SHORT, 1, stop=101.0)})

    with_spread = run(rows, strategy(), make_config(instrument=cfd(), costs=costs(spread=0.5)))
    without = run(rows, strategy(), make_config(instrument=cfd(), costs=FREE, fill_model=model))

    (trade,) = with_spread.trades
    assert (trade.entry_price, trade.exit_price, trade.reason) == (100.0, 101.0, ExitReason.STOP)
    assert [t.reason for t in without.trades] == [ExitReason.END_OF_DATA]


def test_cfd_short_is_marked_at_the_ask() -> None:
    strategy = ScriptedStrategy({0: bracket("S", Side.SHORT, 1, stop=110.0)})
    config = make_config(instrument=cfd(), costs=costs(spread=0.5))
    run([FLAT, FLAT], strategy, config)
    assert strategy.bar_contexts[1].equity == pytest.approx(99_999.5)


@pytest.mark.parametrize(("low", "filled"), [(98.5, False), (98.4, True)])
def test_cfd_buy_limit_fills_only_when_the_ask_trades_through(low: float, filled: bool) -> None:
    strategy = ScriptedStrategy({0: bracket("L", Side.LONG, 1, limit=99.0, stop=90.0)})
    config = make_config(instrument=cfd(), costs=costs(spread=0.5))
    result = run([FLAT, (99.5, 99.6, low, 99.0)], strategy, config)
    assert [t.entry_price for t in result.trades] == ([99.0] if filled else [])


def test_cfd_buy_limit_without_spread_fills_on_the_bid() -> None:
    strategy = ScriptedStrategy({0: bracket("L", Side.LONG, 1, limit=99.0, stop=90.0)})
    result = run([FLAT, (99.5, 99.6, 98.5, 99.0)], strategy, make_config(instrument=cfd()))
    assert [t.entry_price for t in result.trades] == [99.0]


@pytest.mark.parametrize(("offset_minutes", "entry_price"), [(0, 102.0), (4, 100.5)])
def test_spread_window_by_new_york_minute(offset_minutes: int, entry_price: float) -> None:
    # 09:30-09:35 New York: 2.0 points; otherwise 0.5. The fill bar opens one minute later.
    windows = (SpreadWindow(start_minute=570, end_minute=575, spread_points=2.0),)
    cost_model = CostModel(
        commission_per_unit_side=0.0,
        slippage_ticks=0.0,
        spread_points=0.5,
        spread_windows=windows,
    )
    series = make_series([FLAT, FLAT], start_ms=START_MS + offset_minutes * MINUTE_MS)
    strategy = ScriptedStrategy({0: bracket("L", Side.LONG, 1, stop=90.0)})

    result = run(series, strategy, make_config(instrument=cfd(), costs=cost_model))

    assert [t.entry_price for t in result.trades] == [entry_price]


# -- end of data, empty input ---------------------------------------------------------------


def test_end_of_data_closes_at_the_last_close_like_a_close() -> None:
    strategy = ScriptedStrategy({0: bracket("L", Side.LONG, 1, stop=90.0)})
    config = make_config(costs=costs(commission=0.5, slip=1))

    result = run([FLAT, FLAT, (101.0, 102.5, 100.5, 102.0)], strategy, config)

    (trade,) = result.trades
    assert (trade.reason, trade.exit_id, trade.comment) == (ExitReason.END_OF_DATA, "", "")
    assert (trade.exit_bar_index, trade.exit_price) == (2, 101.75)
    assert trade.pnl == (101.75 - 100.25) * 5 - 1.0
    assert (entry := group_by_entry(result.trades)[0]).complete is False
    assert entry.reasons == (ExitReason.END_OF_DATA,)


def test_empty_series_gives_an_empty_result() -> None:
    result = run([], ScriptedStrategy())
    assert result.trades == ()
    assert (result.bars, result.first_ts_ms, result.last_ts_ms) == (0, None, None)
    assert (result.final_equity, result.dropped_entries) == (100_000.0, 0)


def test_session_last_must_match_the_series() -> None:
    with pytest.raises(ValueError, match="session_last"):
        run([FLAT, FLAT], ScriptedStrategy(), session_last=[True])


def test_session_last_flag_reaches_the_strategy() -> None:
    strategy = ScriptedStrategy()
    run([FLAT, FLAT, FLAT], strategy, session_last=[False, True, False])
    assert [ctx.session_last_bar for ctx in strategy.bar_contexts] == [False, True, False]
    assert [ctx.bar_index for ctx in strategy.bar_contexts] == [0, 1, 2]


# -- contract violations ----------------------------------------------------------------------


@BOTH
def test_reversal_entry_is_an_error(model: FillModel) -> None:
    script: dict[int, Sequence[Action]] = {
        0: bracket("L", Side.LONG, 1, stop=90.0),
        1: bracket("S", Side.SHORT, 1, stop=110.0),
    }
    with pytest.raises(EngineError, match=r"bar 1: order 'S': .*revers"):
        run([FLAT, FLAT, FLAT], ScriptedStrategy(script), make_config(fill_model=model))


def test_opposite_working_entry_is_an_error_once_a_position_opens() -> None:
    script: dict[int, Sequence[Action]] = {
        0: [
            *bracket("L", Side.LONG, 1, limit=99.0, stop=95.0),
            *bracket("S", Side.SHORT, 1, limit=101.0, stop=105.0),
        ]
    }
    # Flat with a long limit first: high first, so the short fills while the long works.
    with pytest.raises(EngineError, match=r"bar 1: order 'L': .*opposite"):
        run([FLAT, (100.0, 101.5, 98.5, 100.0)], ScriptedStrategy(script))


@pytest.mark.parametrize("qty", [0.0, -1.0, math.nan, math.inf])
def test_entry_qty_must_be_finite_and_positive(qty: float) -> None:
    with pytest.raises(EngineError, match=r"bar 0: order 'L': entry qty"):
        run([FLAT], ScriptedStrategy({0: bracket("L", Side.LONG, qty, stop=90.0)}))


def test_entry_limit_must_be_a_positive_price() -> None:
    with pytest.raises(EngineError, match="entry limit"):
        run([FLAT], ScriptedStrategy({0: [Entry("L", Side.LONG, 1, limit=math.nan)]}))


@BOTH
@pytest.mark.parametrize(
    "exits",
    [
        [],
        [Exit("tp", "L", limit=110.0)],
        [Exit("half", "L", stop=90.0, qty_percent=50)],
    ],
)
def test_entry_must_fill_fully_covered_by_stops(model: FillModel, exits: list[Action]) -> None:
    strategy = ScriptedStrategy({0: [Entry("L", Side.LONG, 2), *exits]})
    with pytest.raises(EngineError, match=r"bar 1: order 'L': .*invariant 2"):
        run([FLAT, FLAT], strategy, make_config(fill_model=model))


def test_cancelling_the_only_stop_of_an_open_entry_is_an_error() -> None:
    script: dict[int, Sequence[Action]] = {
        0: bracket("L", Side.LONG, 1, stop=90.0),
        1: [Cancel("x-L")],
    }
    with pytest.raises(EngineError, match=r"bar 1: order 'L': .*invariant 2"):
        run([FLAT, FLAT, FLAT], ScriptedStrategy(script))


@pytest.mark.parametrize(
    ("model", "error_bar"), [(FillModel.PESSIMISTIC, 1), (FillModel.TV_OHLC, 2)]
)
def test_moving_the_stop_toward_the_loss_is_an_error(model: FillModel, error_bar: int) -> None:
    script: dict[int, Sequence[Action]] = {
        0: bracket("L", Side.LONG, 1, stop=95.0),
        1: [Exit("x-L", "L", stop=94.0)],  # TV: still the fill bar's re-pricing
        2: [Exit("x-L", "L", stop=93.0)],
    }
    with pytest.raises(EngineError, match=rf"bar {error_bar}: order 'L': .*toward the loss"):
        run([FLAT, FLAT, FLAT, FLAT], ScriptedStrategy(script), make_config(fill_model=model))


def test_short_stop_may_only_move_down() -> None:
    script: dict[int, Sequence[Action]] = {
        0: bracket("S", Side.SHORT, 1, stop=105.0),
        1: [Exit("x-S", "S", stop=103.0)],
        2: [Exit("x-S", "S", stop=104.0)],
    }
    with pytest.raises(EngineError, match=r"bar 2: order 'S': .*toward the loss"):
        run([FLAT, FLAT, FLAT, FLAT], ScriptedStrategy(script))


# Two stop-carrying halves of a 2-lot entry filled at 100: "A" at 97.5 (fill - 10 ticks)
# and "B" at 95 (fill - 20 ticks); the short mirrors them around 100.
_HALVES_FLAT = [FLAT] * 5


def _halves(side: Side, *, tight: float = 2.5, loose: float = 5.0) -> list[Action]:
    sign = side.sign
    return [
        Entry("E", side, 2),
        Exit("A", "E", stop=100.0 - sign * tight, qty_percent=50),
        Exit("B", "E", stop=100.0 - sign * loose, qty_percent=50),
    ]


def _at(side: Side, distance: float) -> float:
    """The stop `distance` points on the loss side of the 100 fill."""
    return 100.0 - side.sign * distance


@BOTH
@pytest.mark.parametrize("side", [Side.LONG, Side.SHORT])
def test_widening_one_partial_stop_inside_the_furthest_is_an_error(
    model: FillModel, side: Side
) -> None:
    # The reviewer's case: half of the position goes from fill-10 to fill-19 while the
    # furthest stop (fill-20) stays put; the furthest-stop check alone accepted it.
    script: dict[int, Sequence[Action]] = {
        0: _halves(side),
        2: [Exit("A", "E", stop=_at(side, 4.75), qty_percent=50)],
    }
    with pytest.raises(EngineError, match=r"bar 2: order 'E': .*toward the loss.*invariant 2"):
        run(_HALVES_FLAT, ScriptedStrategy(script), make_config(fill_model=model))


@BOTH
@pytest.mark.parametrize("side", [Side.LONG, Side.SHORT])
@pytest.mark.parametrize(
    "batch",
    [
        # Quantity moved from the tight stop to the loose one, same ids.
        lambda s: [
            Exit("A", "E", stop=_at(s, 2.5), qty_percent=25),
            Exit("B", "E", stop=_at(s, 5.0), qty_percent=75),
        ],
        # The tight exit replaced by a looser one under another id.
        lambda s: [Cancel("A"), Exit("C", "E", stop=_at(s, 4.0), qty_percent=50)],
        # Both exits swapped for one full-size exit at the furthest level.
        lambda s: [Cancel("A"), Cancel("B"), Exit("R", "E", stop=_at(s, 5.0))],
    ],
)
def test_moving_a_share_from_a_tight_stop_to_a_looser_one_is_an_error(
    model: FillModel, side: Side, batch: Callable[[Side], list[Action]]
) -> None:
    script: dict[int, Sequence[Action]] = {0: _halves(side), 2: batch(side)}
    with pytest.raises(EngineError, match=r"bar 2: order 'E': .*toward the loss.*invariant 2"):
        run(_HALVES_FLAT, ScriptedStrategy(script), make_config(fill_model=model))


@BOTH
@pytest.mark.parametrize("side", [Side.LONG, Side.SHORT])
def test_overlapping_shares_still_count_a_widened_tight_stop(model: FillModel, side: Side) -> None:
    # Both exits cover the whole entry: "A" alone protects all of it at fill-10, so moving
    # it to fill-16 widens the stop of the whole position although "B" stays furthest.
    script: dict[int, Sequence[Action]] = {
        0: [
            Entry("E", side, 2),
            Exit("A", "E", stop=_at(side, 2.5)),
            Exit("B", "E", stop=_at(side, 5.0)),
        ],
        2: [Exit("A", "E", stop=_at(side, 4.0))],
    }
    with pytest.raises(EngineError, match=r"bar 2: order 'E': .*invariant 2"):
        run(_HALVES_FLAT, ScriptedStrategy(script), make_config(fill_model=model))


@BOTH
@pytest.mark.parametrize("side", [Side.LONG, Side.SHORT])
@pytest.mark.parametrize(
    "batch",
    [
        # Both tightened.
        lambda s: [
            Exit("A", "E", stop=_at(s, 2.0), qty_percent=50),
            Exit("B", "E", stop=_at(s, 4.0), qty_percent=50),
        ],
        # Quantity moved from the loose stop to the tight one.
        lambda s: [
            Exit("A", "E", stop=_at(s, 2.5), qty_percent=75),
            Exit("B", "E", stop=_at(s, 5.0), qty_percent=25),
        ],
        # The loose exit replaced by a tighter one under another id.
        lambda s: [Cancel("B"), Exit("C", "E", stop=_at(s, 3.0), qty_percent=50)],
        # An extra overlapping exit adds protection.
        lambda s: [Exit("D", "E", stop=_at(s, 1.0), qty_percent=100)],
    ],
)
def test_batches_that_tighten_and_loosen_nothing_are_accepted(
    model: FillModel, side: Side, batch: Callable[[Side], list[Action]]
) -> None:
    script: dict[int, Sequence[Action]] = {0: _halves(side), 2: batch(side)}
    result = run(_HALVES_FLAT, ScriptedStrategy(script), make_config(fill_model=model))
    assert sum(trade.qty for trade in result.trades) == 2.0
    assert {trade.reason for trade in result.trades} == {ExitReason.END_OF_DATA}


def test_tv_fill_bar_may_still_reprice_a_partial_stop_before_the_lock() -> None:
    # tv_ohlc: the fill bar's on_bar re-prices the bracket (ADR-0008), so widening one half
    # there is the initial stop, not a move; the pessimistic run locks it in on_fill.
    script: dict[int, Sequence[Action]] = {
        0: _halves(Side.LONG),
        1: [Exit("A", "E", stop=96.0, qty_percent=50)],
    }
    result = run(_HALVES_FLAT, ScriptedStrategy(script), make_config(fill_model=FillModel.TV_OHLC))
    assert {trade.initial_stop for trade in result.trades} == {95.0}
    with pytest.raises(EngineError, match=r"bar 1: order 'E': .*invariant 2"):
        run(_HALVES_FLAT, ScriptedStrategy(script))


@BOTH
@pytest.mark.parametrize("side", [Side.LONG, Side.SHORT])
def test_mtf_scale_out_then_runner_at_breakeven_then_trailing_is_accepted(
    model: FillModel, side: Side
) -> None:
    # The MTF port: "TP1" (50 %, stop + target) and "Init" (50 %); once TP1 fills, "Init"
    # is cancelled and "Run" (100 % of the entry, i.e. the rest) sits at breakeven, then
    # trails. The capped profile lets the 100 % runner replace the half-size "Init".
    sign = side.sign
    stop = _at(side, 5.0)
    tp1 = 100.0 + sign * 1.0
    script: dict[int, Sequence[Action]] = {
        0: [
            Entry("E", side, 2),
            Exit("TP1", "E", stop=stop, limit=tp1, qty_percent=50),
            Exit("Init", "E", stop=stop, qty_percent=50),
        ],
        3: [Cancel("Init"), Exit("Run", "E", stop=100.0)],
        4: [Exit("Run", "E", stop=100.0 + sign * 0.5)],
    }
    tp1_bar = (100.0, 101.5, 99.75, 101.0) if side is Side.LONG else (100.0, 100.25, 98.5, 99.0)
    calm = (101.0, 101.25, 100.75, 101.0) if side is Side.LONG else (99.0, 99.25, 98.75, 99.0)
    rows = [FLAT, FLAT, tp1_bar, calm, calm, calm]
    result = run(rows, ScriptedStrategy(script), make_config(fill_model=model))
    reasons = [(trade.exit_id, trade.reason) for trade in result.trades]
    assert reasons == [("TP1", ExitReason.TARGET), ("", ExitReason.END_OF_DATA)]


@BOTH
@pytest.mark.parametrize("side", [Side.LONG, Side.SHORT])
def test_mtf_plan_b_stop_replaced_by_runner_is_accepted(model: FillModel, side: Side) -> None:
    script: dict[int, Sequence[Action]] = {
        0: [Entry("E", side, 1), Exit("Stop", "E", stop=_at(side, 5.0))],
        2: [Cancel("Stop"), Exit("RunB", "E", stop=_at(side, 1.0))],
        3: [Cancel("Stop"), Exit("RunB", "E", stop=_at(side, 0.75))],
    }
    result = run(_HALVES_FLAT, ScriptedStrategy(script), make_config(fill_model=model))
    (trade,) = result.trades
    assert trade.initial_stop == _at(side, 5.0)


@BOTH
def test_after_a_partial_fill_the_rest_may_not_move_to_a_looser_stop(model: FillModel) -> None:
    # TP1 filled half; "Init" still protects the other half at 97.5. A 100 % "Run" at 96
    # replacing it keeps the furthest stop but widens what is left by six ticks.
    script: dict[int, Sequence[Action]] = {
        0: [
            Entry("E", Side.LONG, 2),
            Exit("TP1", "E", stop=95.0, limit=101.0, qty_percent=50),
            Exit("Init", "E", stop=97.5, qty_percent=50),
            Exit("Far", "E", stop=95.0, qty_percent=50),
        ],
        3: [Cancel("Init"), Exit("Run", "E", stop=96.0)],
    }
    rows = [FLAT, FLAT, (100.0, 101.5, 99.75, 101.0), (101.0,) * 4, (101.0,) * 4]
    with pytest.raises(EngineError, match=r"bar 3: order 'E': .*invariant 2"):
        run(rows, ScriptedStrategy(script), make_config(fill_model=model))


@pytest.mark.parametrize("action", [Entry("L2", Side.LONG, 1), Close("L", ""), CloseAll("")])
def test_on_fill_may_only_return_exit_and_cancel(action: Action) -> None:
    strategy = ScriptedStrategy(
        {0: bracket("L", Side.LONG, 1, stop=90.0)}, fill_script=lambda fill, ctx: [action]
    )
    with pytest.raises(EngineError, match=r"bar 1: order 'L': on_fill returned"):
        run([FLAT, FLAT], strategy)


@pytest.mark.parametrize(
    ("exit_", "message"),
    [
        (Exit("x", "L"), "neither a stop nor a limit"),
        (Exit("x", "L", stop=90.0, qty_percent=0.0), "qty_percent"),
        (Exit("x", "L", stop=90.0, qty_percent=150.0), "qty_percent"),
        (Exit("x", "L", stop=math.inf), "stop must be finite"),
    ],
)
def test_malformed_exit_is_an_error(exit_: Exit, message: str) -> None:
    with pytest.raises(EngineError, match=rf"bar 0: order 'x': .*{message}"):
        run([FLAT], ScriptedStrategy({0: [Entry("L", Side.LONG, 1), exit_]}))


def test_entry_and_exit_must_not_share_an_id() -> None:
    script: dict[int, Sequence[Action]] = {
        0: [Entry("L", Side.LONG, 1, limit=90.0), Exit("x", "L", stop=85.0)],
        1: [Entry("x", Side.LONG, 1)],
    }
    with pytest.raises(EngineError, match=r"bar 1: order 'x': .*id of a working exit"):
        run([FLAT, FLAT], ScriptedStrategy(script))


def test_negative_pyramiding_is_an_error() -> None:
    with pytest.raises(EngineError, match="pyramiding"):
        run([FLAT], ScriptedStrategy(pyramiding=-1))


def test_pyramiding_counts_entries_as_pine_does_zero_and_one_both_mean_one() -> None:
    # Pine: `pyramiding` is the maximum number of same-direction entries; 0 still allows
    # one. The Master script sets 2 for its two legs: with 1 the second leg is dropped.
    for limit, dropped in ((0, 1), (1, 1), (2, 0)):
        result = run([FLAT, FLAT, FLAT], ScriptedStrategy({0: _two_legs()}, pyramiding=limit))
        assert result.dropped_entries == dropped, limit


# -- gaps a mutation run of the engine found in these tests -----------------------------------


@BOTH
def test_two_stops_crossed_in_one_bar_each_fill_at_their_own_level(model: FillModel) -> None:
    # The exit placed FIRST carries the farther stop: orders must trigger in the order price
    # reaches them, not in the order they were placed. Filling the far one first would hand
    # the near one the far price.
    script: dict[int, Sequence[Action]] = {
        0: [*bracket("B", Side.LONG, 1, stop=96.0), *bracket("A", Side.LONG, 1, stop=98.0)]
    }
    strategy = ScriptedStrategy(script, pyramiding=2)
    rows = [FLAT, FLAT, (100.0, 100.5, 95.0, 95.5)]

    result = run(rows, strategy, make_config(fill_model=model))

    assert sorted((t.entry_id, t.exit_price, t.reason) for t in result.trades) == [
        ("A", 98.0, ExitReason.STOP),
        ("B", 96.0, ExitReason.STOP),
    ]


@BOTH
def test_a_target_already_passed_at_the_fill_bars_open_fills_at_the_open(
    model: FillModel,
) -> None:
    # The bar gaps above the target of the entry that fills at this very open. A sell limit
    # below the market fills at the market: selling at its own level would give 2 points away.
    strategy = ScriptedStrategy({0: bracket("L", Side.LONG, 1, stop=95.0, target=101.0)})
    rows = [FLAT, (103.0, 103.5, 102.5, 103.0)]

    result = run(rows, strategy, make_config(fill_model=model))

    (trade,) = result.trades
    assert (trade.entry_price, trade.exit_price, trade.reason) == (103.0, 103.0, ExitReason.TARGET)
    assert trade.pnl == 0.0


def test_cfd_buy_limit_gapped_at_the_open_fills_at_the_opening_ask() -> None:
    # Bid opens at 97.0 below a buy limit at 99.0: the fill is the better opening price, and
    # for a buy that is the ASK (bid + spread), not the bid minus the spread.
    strategy = ScriptedStrategy({0: bracket("L", Side.LONG, 1, stop=90.0, limit=99.0)})
    config = make_config(instrument=cfd(), costs=costs(spread=0.5))

    result = run([FLAT, (97.0, 97.5, 96.5, 97.0)], strategy, config)

    (trade,) = result.trades
    assert trade.entry_price == 97.5
    assert trade.pnl == pytest.approx(-0.5)  # closed at the bid at the end of the data


@BOTH
def test_cfd_short_adverse_excursion_is_measured_on_the_ask(model: FillModel) -> None:
    # Short at the bid 100.0, stop 110.0: risk is 10 points. The bid peaks at 102.0, so the
    # price the short would be bought back at peaked at 102.5: 2.5 points against it.
    strategy = ScriptedStrategy({0: bracket("S", Side.SHORT, 1, stop=110.0)})
    config = make_config(instrument=cfd(), costs=costs(spread=0.5), fill_model=model)

    result = run([FLAT, (100.0, 102.0, 99.5, 100.0)], strategy, config)

    (trade,) = result.trades
    assert trade.entry_price == 100.0
    assert trade.mae_r == pytest.approx(-0.25)
    assert trade.r == pytest.approx(-0.05)  # bought back at the closing ask 100.5


@pytest.mark.parametrize("limit", [0.0, -5.0, math.inf, math.nan])
def test_a_limit_entry_needs_a_positive_finite_price(limit: float) -> None:
    strategy = ScriptedStrategy({0: bracket("L", Side.LONG, 1, stop=90.0, limit=limit)})

    with pytest.raises(EngineError, match="L"):
        run([FLAT, FLAT], strategy)
