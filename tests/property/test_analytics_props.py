"""Properties of the backtest statistics, the curve down-sampling and the trade-list export."""

import math
import tempfile
from pathlib import Path

from hypothesis import given, settings
from hypothesis import strategies as st

from propbot.backtest.analytics import (
    EquityPoint,
    downsample_curve,
    equity_curve,
    to_mc_trades,
    trade_stats,
    write_trades_csv,
)
from propbot.backtest.model import EntryResult, ExitReason
from propbot.backtest.trades import load_trades_csv
from propbot.strategies.base import Side

START_MS = 1_767_623_400_000  # 2026-01-05T14:30:00Z
CAPITAL = 10_000.0

results = st.floats(min_value=-3.0, max_value=5.0, allow_nan=False, allow_infinity=False)
risks = st.floats(min_value=10.0, max_value=500.0, allow_nan=False, allow_infinity=False)


@st.composite
def entry_lists(draw: st.DrawFn, min_size: int = 0, max_size: int = 40) -> list[EntryResult]:
    specs = draw(
        st.lists(
            st.tuples(
                results,
                risks,
                st.integers(min_value=0, max_value=50 * 86_400_000),  # entry offset
                st.integers(min_value=0, max_value=6 * 3_600_000),  # duration
                st.floats(min_value=0.0, max_value=2.0),  # extra adverse excursion
                st.booleans(),
            ),
            min_size=min_size,
            max_size=max_size,
        )
    )
    entries = []
    for seq, (r, risk, offset, duration, extra, complete) in enumerate(specs):
        entries.append(
            EntryResult(
                seq=seq,
                strategy_id="ny_orb_fvg",
                entry_id="L",
                setup="",
                side=Side.LONG,
                qty=1.0,
                entry_ts_ms=START_MS + offset,
                exit_ts_ms=START_MS + offset + duration,
                entry_price=5000.0,
                pnl=r * risk,
                risk_amount=risk,
                r=r,
                mae_r=min(r, 0.0) - extra,
                reasons=(ExitReason.CLOSE,),
                complete=complete or seq == 0,  # the export needs one complete entry
            )
        )
    return entries


@given(entry_lists())
def test_counts_and_totals_add_up(entries: list[EntryResult]) -> None:
    stats = trade_stats(entries, initial_capital=CAPITAL)
    complete = [e for e in entries if e.complete]

    assert stats.wins + stats.losses + stats.breakeven == stats.n == len(complete)
    assert stats.incomplete == len(entries) - len(complete)
    assert stats.total_r == math.fsum(e.r for e in complete)
    assert stats.total_pnl == math.fsum(e.pnl for e in complete)
    assert stats.longest_losing_streak <= stats.losses
    if stats.win_rate is not None:
        assert 0.0 <= stats.win_rate <= 1.0


@given(entry_lists())
def test_max_drawdown_is_non_negative_and_bounded_by_the_curve_range(
    entries: list[EntryResult],
) -> None:
    stats = trade_stats(entries, initial_capital=CAPITAL)
    levels = [0.0, *(p.cum_r for p in equity_curve(entries))]

    assert stats.max_drawdown_r >= 0.0
    assert stats.max_drawdown_r <= max(levels) - min(levels) + 1e-9
    assert stats.max_drawdown_pct >= 0.0
    if stats.n == 0:
        assert stats.max_drawdown_r == 0.0
    if all(e.pnl >= 0.0 for e in entries if e.complete):
        assert stats.max_drawdown_r == 0.0


@given(
    st.lists(
        st.floats(min_value=-100.0, max_value=100.0, allow_nan=False), min_size=1, max_size=300
    ),
    st.integers(min_value=6, max_value=60),
)
def test_downsampled_curve_keeps_first_last_peak_and_trough(
    values: list[float], max_points: int
) -> None:
    points = [EquityPoint(i, v, -v) for i, v in enumerate(values)]

    kept = downsample_curve(points, max_points)
    times = [p.exit_ts_ms for p in kept]

    assert len(kept) <= max_points
    assert times == sorted(set(times))
    assert kept[0] == points[0]
    assert kept[-1] == points[-1]
    assert max(p.cum_r for p in kept) == max(values)
    assert min(p.cum_r for p in kept) == min(values)
    assert set(kept) <= set(points)


@settings(max_examples=40, deadline=None)
@given(entry_lists(min_size=1, max_size=15))
def test_trade_list_round_trips_through_the_monte_carlo_loader(
    entries: list[EntryResult],
) -> None:
    with tempfile.TemporaryDirectory() as directory:
        path = Path(directory) / "trades.csv"

        written = write_trades_csv(entries, path)

        assert written == sum(1 for e in entries if e.complete)
        assert load_trades_csv(path) == to_mc_trades(entries)
