"""Properties of the parity matcher (SPEC §6.6)."""

import random

from hypothesis import given, settings
from hypothesis import strategies as st

from propbot.backtest.model import ExitReason, FillModel, TradeRecord
from propbot.backtest.parity import ParityResult, TvFill, compare_trades
from propbot.strategies.base import Side

TICK = 0.25
MINUTE = 60_000
T0 = 1_772_462_160_000  # 2026-03-02 14:36 UTC
LABELS = ("Exit L", "Exit S", "TP1", "Init stop", "EOD close", "")


@st.composite
def records(draw: st.DrawFn) -> TradeRecord:
    side = draw(st.sampled_from(Side))
    entry_bar = draw(st.integers(0, 400))
    exit_bar = entry_bar + draw(st.integers(0, 60))
    entry_price = 4000.0 + draw(st.integers(0, 400)) * TICK
    exit_price = 4000.0 + draw(st.integers(0, 400)) * TICK
    comment = draw(st.sampled_from(LABELS))
    exit_id = draw(st.sampled_from(LABELS[:4])) if not comment else ""
    return TradeRecord(
        seq=draw(st.integers(0, 50)),
        strategy_id="s",
        entry_id="Long" if side is Side.LONG else "Short",
        exit_id=exit_id,
        reason=draw(st.sampled_from((ExitReason.STOP, ExitReason.TARGET, ExitReason.CLOSE))),
        setup="",
        comment=comment,
        side=side,
        qty=float(draw(st.integers(1, 5))),
        entry_bar_index=entry_bar,
        exit_bar_index=exit_bar,
        entry_ts_ms=T0 + entry_bar * MINUTE,
        exit_ts_ms=T0 + exit_bar * MINUTE,
        entry_price=entry_price,
        exit_price=exit_price,
        gross_pnl=0.0,
        commission=0.0,
        pnl=0.0,
        initial_stop=entry_price - side.sign,
        risk_amount=1.0,
        r=0.0,
        mae_r=0.0,
    )


# The engine never emits one exit fill twice; compare_trades refuses repeated rows.
trade_lists = st.lists(records(), max_size=25, unique=True)


def tv_copy(row: TradeRecord, trade_no: int) -> TvFill:
    return TvFill(
        trade_no=trade_no,
        side=row.side,
        entry_ts_ms=row.entry_ts_ms,
        entry_price=row.entry_price,
        entry_signal=row.entry_id,
        exit_ts_ms=row.exit_ts_ms,
        exit_price=row.exit_price,
        exit_signal=row.comment or row.exit_id,
        qty=row.qty,
        line=0,
    )


def run(python: list[TradeRecord], tv: list[TvFill], *, clip: bool = True) -> ParityResult:
    return compare_trades(
        python,
        tv,
        fill_model=FillModel.TV_OHLC,
        tick_size=TICK,
        tf_seconds=60,
        clip_to_overlap=clip,
    )


def summary(result: ParityResult) -> tuple[object, ...]:
    return (
        result.matched,
        result.mismatches,
        result.python_only,
        result.tradingview_only,
        result.match_rate,
        result.passed,
        dict(result.field_counts),
        result.window,
        result.exclusions,
    )


@settings(max_examples=60, deadline=None)
@given(trade_lists)
def test_a_list_compared_with_a_copy_of_itself_always_matches_fully(
    python: list[TradeRecord],
) -> None:
    result = run(python, [tv_copy(row, i) for i, row in enumerate(python)])
    assert len(result.matched) == len(python)
    assert not result.mismatches
    assert not result.python_only
    assert not result.tradingview_only
    assert result.match_rate == (1.0 if python else None)
    assert result.passed is bool(python)


@settings(max_examples=60, deadline=None)
@given(trade_lists, st.data())
def test_removing_k_rows_from_one_side_gives_exactly_k_one_sided_rows(
    python: list[TradeRecord], data: st.DataObject
) -> None:
    tv = [tv_copy(row, i) for i, row in enumerate(python)]
    k = data.draw(st.integers(0, len(python)))
    removed = set(data.draw(st.permutations(range(len(python))))[:k])
    from_tv = data.draw(st.booleans())
    if from_tv:
        result = run(python, [row for i, row in enumerate(tv) if i not in removed], clip=False)
        assert len(result.python_only) == k
        assert not result.tradingview_only
    else:
        result = run([row for i, row in enumerate(python) if i not in removed], tv, clip=False)
        assert len(result.tradingview_only) == k
        assert not result.python_only
    assert not result.mismatches
    assert len(result.matched) == len(python) - k


@settings(max_examples=80, deadline=None)
@given(trade_lists, trade_lists, st.booleans())
def test_rate_is_a_probability_and_passing_implies_the_threshold(
    python: list[TradeRecord], other: list[TradeRecord], clip: bool
) -> None:
    result = run(python, [tv_copy(row, i) for i, row in enumerate(other)], clip=clip)
    if result.match_rate is None:
        assert result.compared == 0
        assert not result.passed
    else:
        assert 0.0 <= result.match_rate <= 1.0
        assert result.match_rate == len(result.matched) / result.compared
    if result.passed:
        assert result.match_rate is not None
        assert result.match_rate >= result.threshold


@settings(max_examples=60, deadline=None)
@given(trade_lists, trade_lists, st.randoms(use_true_random=False))
def test_shuffling_either_input_does_not_change_the_result(
    python: list[TradeRecord], other: list[TradeRecord], rnd: random.Random
) -> None:
    tv = [tv_copy(row, i) for i, row in enumerate(other)] + [
        tv_copy(row, 100 + i) for i, row in enumerate(python[::2])
    ]
    before = summary(run(python, tv))
    shuffled_py, shuffled_tv = list(python), list(tv)
    rnd.shuffle(shuffled_py)
    rnd.shuffle(shuffled_tv)
    assert summary(run(shuffled_py, shuffled_tv)) == before
