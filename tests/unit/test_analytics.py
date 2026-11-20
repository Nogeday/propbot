"""Statistics, IS/OOS split, time tables, equity curve and trade-list export of a backtest."""

import datetime as dt
import math
from pathlib import Path
from zoneinfo import ZoneInfo

import pytest

from propbot.backtest.analytics import (
    AnalyticsError,
    EquityPoint,
    TableRow,
    downsample_curve,
    equity_curve,
    render_stats_text,
    split_is_oos,
    time_tables,
    to_mc_trades,
    trade_stats,
    write_trades_csv,
)
from propbot.backtest.model import EntryResult, ExitReason
from propbot.backtest.trades import Trade, load_trades_csv
from propbot.strategies.base import Side

NY = ZoneInfo("America/New_York")
CAPITAL = 10_000.0


def ny_ms(text: str) -> int:
    moment = dt.datetime.fromisoformat(text).replace(tzinfo=NY)
    return int(moment.timestamp()) * 1000


def utc_ms(text: str) -> int:
    moment = dt.datetime.fromisoformat(text).replace(tzinfo=dt.UTC)
    return int(moment.timestamp()) * 1000


def entry(
    seq: int,
    r: float,
    *,
    entry_ms: int | None = None,
    exit_ms: int | None = None,
    risk: float = 100.0,
    complete: bool = True,
    mae_r: float | None = None,
    strategy_id: str = "ny_orb_fvg",
) -> EntryResult:
    start = ny_ms("2026-01-05T09:30") + seq * 86_400_000 if entry_ms is None else entry_ms
    return EntryResult(
        seq=seq,
        strategy_id=strategy_id,
        entry_id="L",
        setup="",
        side=Side.LONG,
        qty=1.0,
        entry_ts_ms=start,
        exit_ts_ms=start + 600_000 if exit_ms is None else exit_ms,
        entry_price=5000.0,
        pnl=r * risk,
        risk_amount=risk,
        r=r,
        mae_r=min(r, 0.0, -0.5) if mae_r is None else mae_r,
        reasons=(ExitReason.TARGET if r > 0 else ExitReason.STOP,),
        complete=complete,
    )


def sample() -> list[EntryResult]:
    return [entry(i, r) for i, r in enumerate([2.0, -1.0, -1.0, 0.0, 1.5])]


# --- trade_stats ---------------------------------------------------------------------------


def test_stats_of_a_mixed_sample_match_hand_computed_values() -> None:
    stats = trade_stats(sample(), initial_capital=CAPITAL)

    assert (stats.n, stats.wins, stats.losses, stats.breakeven) == (5, 2, 2, 1)
    assert stats.win_rate == pytest.approx(0.4)
    assert stats.mean_r == pytest.approx(0.3)
    assert stats.median_r == 0.0
    assert stats.stdev_r == pytest.approx(math.sqrt(1.95))
    assert stats.t_stat == pytest.approx(0.3 / (math.sqrt(1.95) / math.sqrt(5)))
    assert stats.profit_factor == pytest.approx(350.0 / 200.0)
    assert stats.total_r == pytest.approx(1.5)
    assert stats.total_pnl == pytest.approx(150.0)
    assert stats.avg_win_r == pytest.approx(1.75)
    assert stats.avg_loss_r == pytest.approx(-1.0)
    assert stats.max_drawdown_r == pytest.approx(2.0)
    assert stats.max_drawdown_pct == pytest.approx(200.0 / 10_200.0 * 100.0)
    assert stats.longest_losing_streak == 2
    assert stats.incomplete == 0


def test_empty_input_gives_defined_empty_stats() -> None:
    stats = trade_stats([], initial_capital=CAPITAL)

    assert (stats.n, stats.wins, stats.losses, stats.breakeven) == (0, 0, 0, 0)
    assert stats.win_rate is None
    assert stats.mean_r is None
    assert stats.median_r is None
    assert stats.stdev_r is None
    assert stats.t_stat is None
    assert stats.profit_factor is None
    assert stats.avg_win_r is None
    assert stats.avg_loss_r is None
    assert stats.total_r == 0.0
    assert stats.total_pnl == 0.0
    assert stats.max_drawdown_r == 0.0
    assert stats.max_drawdown_pct == 0.0
    assert stats.longest_losing_streak == 0


def test_one_trade_has_no_spread_and_no_t_stat() -> None:
    stats = trade_stats([entry(0, -1.0)], initial_capital=CAPITAL)

    assert stats.n == 1
    assert stats.mean_r == -1.0
    assert stats.median_r == -1.0
    assert stats.stdev_r is None
    assert stats.t_stat is None
    assert stats.profit_factor == 0.0
    assert stats.max_drawdown_r == 1.0
    assert stats.max_drawdown_pct == pytest.approx(1.0)
    assert stats.longest_losing_streak == 1


def test_all_wins_have_no_profit_factor_and_no_drawdown() -> None:
    stats = trade_stats([entry(i, 1.0) for i in range(3)], initial_capital=CAPITAL)

    assert stats.profit_factor is None
    assert stats.losses == 0
    assert stats.avg_loss_r is None
    assert stats.win_rate == 1.0
    assert stats.max_drawdown_r == 0.0
    assert stats.max_drawdown_pct == 0.0
    assert stats.longest_losing_streak == 0


def test_all_losses_have_profit_factor_zero() -> None:
    stats = trade_stats([entry(i, -1.0) for i in range(4)], initial_capital=CAPITAL)

    assert stats.profit_factor == 0.0
    assert stats.win_rate == 0.0
    assert stats.avg_win_r is None
    assert stats.max_drawdown_r == pytest.approx(4.0)
    assert stats.max_drawdown_pct == pytest.approx(4.0)
    assert stats.longest_losing_streak == 4
    assert stats.stdev_r == 0.0
    assert stats.t_stat is None


def test_zero_variance_is_exact_so_there_is_no_huge_t_stat() -> None:
    # 0.1 is not representable; a float mean would leave a tiny stdev and a t of ~1e15.
    stats = trade_stats([entry(i, 0.1) for i in range(3)], initial_capital=CAPITAL)

    assert stats.stdev_r == 0.0
    assert stats.t_stat is None
    assert stats.mean_r == pytest.approx(0.1)


def test_incomplete_entries_are_excluded_and_counted() -> None:
    entries = [*sample(), entry(9, -5.0, complete=False)]

    stats = trade_stats(entries, initial_capital=CAPITAL)

    assert stats.n == 5
    assert stats.incomplete == 1
    assert stats.total_r == pytest.approx(1.5)


def test_drawdown_and_streak_follow_exit_order_not_entry_order() -> None:
    day = ny_ms("2026-01-05T09:30")
    entries = [
        entry(0, 1.0, entry_ms=day, exit_ms=day + 3_000_000),
        entry(1, -1.0, entry_ms=day + 60_000, exit_ms=day + 1_000_000),
        entry(2, -1.0, entry_ms=day + 120_000, exit_ms=day + 2_000_000),
    ]

    stats = trade_stats(entries, initial_capital=CAPITAL)

    # Exit order: -1, -1, +1 -> drawdown 2 from the start, two losses in a row.
    assert stats.max_drawdown_r == pytest.approx(2.0)
    assert stats.longest_losing_streak == 2


def test_breakeven_ends_a_losing_streak() -> None:
    stats = trade_stats([entry(0, -1.0), entry(1, 0.0), entry(2, -1.0)], initial_capital=CAPITAL)

    assert stats.longest_losing_streak == 1
    assert stats.breakeven == 1


def test_win_and_loss_follow_net_pnl() -> None:
    stats = trade_stats([entry(0, 0.0), entry(1, -0.01)], initial_capital=CAPITAL)

    assert (stats.wins, stats.losses, stats.breakeven) == (0, 1, 1)


@pytest.mark.parametrize("capital", [0.0, -1.0, math.inf, math.nan])
def test_invalid_capital_is_refused(capital: float) -> None:
    with pytest.raises(AnalyticsError, match="sermaye"):
        trade_stats(sample(), initial_capital=capital)


# --- IS / OOS ------------------------------------------------------------------------------


def test_entry_exactly_at_oos_start_is_out_of_sample() -> None:
    boundary = utc_ms("2025-01-01T00:00")
    entries = [
        entry(0, 1.0, entry_ms=boundary - 1, exit_ms=boundary + 60_000),
        entry(1, 0.5, entry_ms=boundary, exit_ms=boundary + 60_000),
    ]

    split = split_is_oos(entries, boundary, initial_capital=CAPITAL)

    assert split.in_sample.n == 1
    assert split.in_sample.mean_r == 1.0
    assert split.out_of_sample.n == 1
    assert split.out_of_sample.mean_r == 0.5
    assert split.all.n == 2
    assert split.g3_ratio == pytest.approx(0.5)
    assert split.g3_note is None


def test_g3_ratio_is_none_with_a_reason_when_is_expectancy_is_not_positive() -> None:
    boundary = utc_ms("2026-01-10T00:00")
    entries = [entry(0, -1.0), entry(1, 0.5, entry_ms=boundary)]

    split = split_is_oos(entries, boundary, initial_capital=CAPITAL)

    assert split.g3_ratio is None
    assert split.g3_note is not None
    assert "negatif" in split.g3_note


def test_g3_ratio_is_none_when_a_sample_is_empty() -> None:
    oos_only = split_is_oos([entry(0, 1.0)], 0, initial_capital=CAPITAL)
    is_only = split_is_oos([entry(0, 1.0)], utc_ms("2030-01-01T00:00"), initial_capital=CAPITAL)

    assert oos_only.g3_ratio is None
    assert oos_only.g3_note is not None
    assert "IS" in oos_only.g3_note
    assert is_only.g3_ratio is None
    assert is_only.g3_note is not None
    assert "OOS" in is_only.g3_note


# --- tables --------------------------------------------------------------------------------


def row(rows: tuple[TableRow, ...], key: str) -> TableRow:
    return next(r for r in rows if r.key == key)


def test_weekday_table_has_monday_to_friday_rows_even_when_empty() -> None:
    tables = time_tables([entry(0, 1.0, entry_ms=ny_ms("2026-01-07T09:45"))])  # Wednesday

    assert [r.key for r in tables.weekday] == ["Mon", "Tue", "Wed", "Thu", "Fri"]
    assert row(tables.weekday, "Wed").trades == 1
    assert row(tables.weekday, "Mon").trades == 0
    assert row(tables.weekday, "Mon").win_rate is None
    assert row(tables.weekday, "Mon").mean_r is None


def test_weekday_and_hour_use_new_york_time_across_dst() -> None:
    # Friday before the 2026-03-08 change (EST, 14:30Z) and Monday after it (EDT, 13:30Z).
    friday = utc_ms("2026-03-06T14:30")
    monday = utc_ms("2026-03-09T13:30")
    tables = time_tables([entry(0, 1.0, entry_ms=friday), entry(1, -1.0, entry_ms=monday)])

    assert row(tables.weekday, "Fri").trades == 1
    assert row(tables.weekday, "Mon").trades == 1
    assert [r.key for r in tables.hour] == ["09"]
    hour = tables.hour[0]
    assert (hour.trades, hour.wins, hour.label) == (2, 1, "09:00")
    assert hour.total_r == 0.0
    assert hour.win_rate == 0.5


def test_entry_after_midnight_utc_belongs_to_the_new_york_evening_before() -> None:
    # 2026-01-06T02:00Z is Monday 21:00 in New York.
    tables = time_tables([entry(0, 2.0, entry_ms=utc_ms("2026-01-06T02:00"))])

    assert row(tables.weekday, "Mon").trades == 1
    assert row(tables.weekday, "Tue").trades == 0
    assert [r.key for r in tables.hour] == ["21"]


def test_weekend_rows_appear_only_when_present() -> None:
    sunday = ny_ms("2026-01-04T18:00")
    tables = time_tables([entry(0, 1.0, entry_ms=sunday)])

    assert [r.key for r in tables.weekday] == ["Mon", "Tue", "Wed", "Thu", "Fri", "Sun"]
    assert row(tables.weekday, "Sun").label == "Pazar"


def test_month_table_uses_new_york_month_and_keeps_empty_months() -> None:
    entries = [
        entry(0, 1.0, entry_ms=utc_ms("2026-02-01T03:00")),  # 2026-01-31 22:00 New York
        entry(1, -1.0, entry_ms=ny_ms("2026-03-02T10:00")),
        entry(2, 0.5, entry_ms=ny_ms("2026-03-03T10:00")),
    ]

    months = time_tables(entries).month

    assert [r.key for r in months] == ["2026-01", "2026-02", "2026-03"]
    assert (months[0].trades, months[0].total_r, months[0].total_pnl) == (1, 1.0, 100.0)
    assert months[1].trades == 0
    assert months[1].total_r == 0.0
    assert (months[2].trades, months[2].wins) == (2, 1)
    assert months[2].mean_r == pytest.approx(-0.25)
    assert months[2].total_pnl == pytest.approx(-50.0)


def test_month_table_crosses_a_year_end() -> None:
    entries = [
        entry(0, 1.0, entry_ms=ny_ms("2025-11-03T10:00")),
        entry(1, 1.0, entry_ms=ny_ms("2026-01-05T10:00")),
    ]

    assert [r.key for r in time_tables(entries).month] == ["2025-11", "2025-12", "2026-01"]


def test_tables_skip_incomplete_entries_and_handle_empty_input() -> None:
    tables = time_tables([entry(0, 1.0, complete=False)])

    assert all(r.trades == 0 for r in tables.weekday)
    assert tables.hour == ()
    assert tables.month == ()


# --- equity curve --------------------------------------------------------------------------


def test_equity_curve_is_cumulative_in_exit_order() -> None:
    curve = equity_curve([*sample(), entry(7, 3.0, complete=False)])

    assert [p.cum_r for p in curve] == pytest.approx([2.0, 1.0, 0.0, 0.0, 1.5])
    assert [p.cum_pnl for p in curve] == pytest.approx([200.0, 100.0, 0.0, 0.0, 150.0])
    assert [p.exit_ts_ms for p in curve] == sorted(p.exit_ts_ms for p in curve)


def test_downsampling_keeps_first_last_peak_and_trough() -> None:
    values = [0.0, 1.0, 5.0, 2.0, 1.0, -4.0, -1.0, 0.0, 1.0, 2.0, 1.0, 0.5]
    points = [EquityPoint(i, v, v * 10.0) for i, v in enumerate(values)]

    kept = downsample_curve(points, 6)

    assert len(kept) <= 6
    assert [p.exit_ts_ms for p in kept][:1] == [0]
    assert kept[-1].exit_ts_ms == 11
    assert {2, 5} <= {p.exit_ts_ms for p in kept}
    assert [p.exit_ts_ms for p in kept] == sorted(p.exit_ts_ms for p in kept)


def test_downsampling_is_deterministic_and_bounded() -> None:
    points = [EquityPoint(i, math.sin(i / 7.0), float(i % 13)) for i in range(1000)]

    first = downsample_curve(points, 50)

    assert first == downsample_curve(points, 50)
    assert 6 <= len(first) <= 50


def test_short_curve_is_returned_unchanged_and_a_tiny_limit_is_refused() -> None:
    points = tuple(EquityPoint(i, float(i), float(i)) for i in range(4))

    assert downsample_curve(points, 6) == points
    with pytest.raises(AnalyticsError):
        downsample_curve(points, 5)


# --- Monte Carlo trade list ----------------------------------------------------------------


def test_to_mc_trades_converts_complete_entries_in_entry_order() -> None:
    entries = [
        entry(1, -1.0, mae_r=-1.0),
        entry(0, 2.0, mae_r=-0.25),
        entry(2, 1.0, complete=False),
    ]

    trades = to_mc_trades(entries)

    assert trades == (
        Trade(
            entry_utc=dt.datetime(2026, 1, 5, 14, 30, tzinfo=dt.UTC),
            exit_utc=dt.datetime(2026, 1, 5, 14, 40, tzinfo=dt.UTC),
            r=2.0,
            strategy_id="ny_orb_fvg",
            mae_r=-0.25,
        ),
        Trade(
            entry_utc=dt.datetime(2026, 1, 6, 14, 30, tzinfo=dt.UTC),
            exit_utc=dt.datetime(2026, 1, 6, 14, 40, tzinfo=dt.UTC),
            r=-1.0,
            strategy_id="ny_orb_fvg",
            mae_r=-1.0,
        ),
    )


def test_trade_csv_round_trips_through_the_monte_carlo_loader(tmp_path: Path) -> None:
    entries = [
        entry(0, 1.0 / 3.0, mae_r=-0.7),
        entry(1, -0.8123456789, mae_r=-0.9),
        # Millisecond stamps survive the round trip.
        entry(
            2,
            0.0,
            entry_ms=utc_ms("2026-02-02T15:00") + 123,
            exit_ms=utc_ms("2026-02-02T15:30") + 7,
        ),
    ]
    path = tmp_path / "trades.csv"

    written = write_trades_csv(entries, path)

    assert written == 3
    assert load_trades_csv(path) == to_mc_trades(entries)
    assert path.read_text(encoding="utf-8").splitlines()[0] == (
        "entry_time,exit_time,r,strategy_id,mae_r"
    )


def test_trade_csv_with_nothing_complete_is_refused(tmp_path: Path) -> None:
    path = tmp_path / "trades.csv"

    with pytest.raises(AnalyticsError, match="tamamlanmış işlem yok"):
        write_trades_csv([entry(0, 1.0, complete=False)], path)
    assert not path.exists()


# --- text ----------------------------------------------------------------------------------


def test_render_stats_text_is_turkish_and_shows_missing_values_as_dash() -> None:
    boundary = utc_ms("2026-01-08T00:00")
    split = split_is_oos(
        [*sample(), entry(9, 1.0, complete=False)], boundary, initial_capital=CAPITAL
    )

    text = render_stats_text(split, title="ny_orb_fvg · backtest")

    assert text.startswith("ny_orb_fvg · backtest\n")
    assert "OOS başlangıcı: 2026-01-08T00:00:00Z" in text
    assert "beklenti (ortalama R)" in text
    assert "+0,300R" in text  # all-sample expectancy
    assert "1,75" in text  # profit factor
    assert "—" in text  # OOS has no loss: no profit factor
    assert "1 işlem veri bittiğinde açıktı" in text
    assert "G3 oranı" in text
    assert text.endswith("\n")
