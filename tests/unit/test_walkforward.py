"""Walk-forward: calendar fold arithmetic, span cut, refusals and the summary."""

import datetime as dt
from zoneinfo import ZoneInfo

import pytest

from propbot.backtest.model import EntryResult, ExitReason
from propbot.backtest.walkforward import (
    NO_SEARCH_NOTE,
    WalkForwardConfig,
    WalkForwardError,
    walk_forward,
)
from propbot.strategies.base import Side

NY = ZoneInfo("America/New_York")
CAPITAL = 100_000.0


def ny_ms(day: dt.date, hour: int = 10, minute: int = 0) -> int:
    moment = dt.datetime(day.year, day.month, day.day, hour, minute, tzinfo=NY)
    return int(moment.timestamp()) * 1000


def entry(seq: int, ts_ms: int, r: float, *, complete: bool = True) -> EntryResult:
    return EntryResult(
        seq=seq,
        strategy_id="ny_orb_fvg",
        entry_id="L",
        setup="",
        side=Side.LONG,
        qty=1.0,
        entry_ts_ms=ts_ms,
        exit_ts_ms=ts_ms + 600_000,
        entry_price=5000.0,
        pnl=r * 500.0,
        risk_amount=500.0,
        r=r,
        mae_r=min(r, 0.0),
        reasons=(ExitReason.CLOSE,),
        complete=complete,
    )


def day_r(day: dt.date) -> float:
    if day <= dt.date(2026, 1, 14):
        return 1.0
    if day <= dt.date(2026, 1, 19):
        return 0.5
    if day <= dt.date(2026, 1, 24):
        return -1.0
    return 1.0


def daily_entries(last: dt.date) -> list[EntryResult]:
    days = [dt.date(2026, 1, 5) + dt.timedelta(days=i) for i in range(40)]
    return [entry(i, ny_ms(day), day_r(day)) for i, day in enumerate(d for d in days if d <= last)]


def test_folds_are_consecutive_calendar_windows_from_the_first_entry() -> None:
    result = walk_forward(
        daily_entries(dt.date(2026, 1, 31)),
        WalkForwardConfig(is_days=10, oos_days=5, step_days=5),
        first_bar_ms=ny_ms(dt.date(2026, 1, 5), 0, 0),
        last_bar_ms=ny_ms(dt.date(2026, 1, 31), 16, 59),
        initial_capital=CAPITAL,
    )

    windows = [
        (f.is_first_day, f.is_last_day, f.oos_first_day, f.oos_last_day) for f in result.folds
    ]
    d = dt.date
    assert windows == [
        (d(2026, 1, 5), d(2026, 1, 14), d(2026, 1, 15), d(2026, 1, 19)),
        (d(2026, 1, 10), d(2026, 1, 19), d(2026, 1, 20), d(2026, 1, 24)),
        (d(2026, 1, 15), d(2026, 1, 24), d(2026, 1, 25), d(2026, 1, 29)),
    ]
    assert [f.index for f in result.folds] == [0, 1, 2]
    assert [(f.is_stats.n, f.oos_stats.n) for f in result.folds] == [(10, 5)] * 3
    assert [f.is_stats.mean_r for f in result.folds] == pytest.approx([1.0, 0.75, -0.25])
    assert [f.oos_stats.mean_r for f in result.folds] == pytest.approx([0.5, -1.0, 1.0])
    assert result.folds[0].ratio == pytest.approx(0.5)
    assert result.folds[1].ratio == pytest.approx(-1.0 / 0.75)
    assert result.folds[2].ratio is None
    assert result.folds[2].ratio_note is not None
    assert (result.span_first_day, result.span_last_day) == (d(2026, 1, 5), d(2026, 1, 31))


def test_summary_pools_oos_entries_once_and_states_no_parameter_search() -> None:
    result = walk_forward(
        daily_entries(dt.date(2026, 1, 31)),
        WalkForwardConfig(is_days=10, oos_days=5, step_days=5),
        first_bar_ms=ny_ms(dt.date(2026, 1, 5), 0, 0),
        last_bar_ms=ny_ms(dt.date(2026, 1, 31), 16, 59),
        initial_capital=CAPITAL,
    )
    summary = result.summary

    assert summary.n_folds == 3
    assert summary.share_positive_oos == pytest.approx(2 / 3)
    assert summary.pooled_oos.n == 15
    assert summary.pooled_oos.total_r == pytest.approx(2.5)
    assert summary.efficiency == pytest.approx((1 / 6) / 0.5)
    assert summary.efficiency_note is None
    assert summary.parameter_search is False
    assert summary.note == NO_SEARCH_NOTE
    assert "Parametre araması yapılmadı" in summary.note


@pytest.mark.parametrize(
    ("last", "folds"),
    [
        (dt.date(2026, 1, 29), 3),  # the third OOS window ends exactly on the last data day
        (dt.date(2026, 1, 28), 2),  # one day short: the third fold is dropped
        (dt.date(2026, 1, 19), 1),
        (dt.date(2026, 1, 18), 0),
    ],
)
def test_a_fold_is_kept_only_when_its_oos_window_fits_in_the_data(
    last: dt.date, folds: int
) -> None:
    result = walk_forward(
        daily_entries(last),
        WalkForwardConfig(is_days=10, oos_days=5, step_days=5),
        first_bar_ms=ny_ms(dt.date(2026, 1, 5), 0, 0),
        last_bar_ms=ny_ms(last, 16, 59),
        initial_capital=CAPITAL,
    )

    assert result.summary.n_folds == folds


def test_span_comes_from_the_bars_not_from_the_trades() -> None:
    # No trade after Jan 19, but the data runs to Jan 31: the quiet days still count.
    entries = daily_entries(dt.date(2026, 1, 19))
    result = walk_forward(
        entries,
        WalkForwardConfig(is_days=10, oos_days=5, step_days=5),
        first_bar_ms=ny_ms(dt.date(2026, 1, 5), 0, 0),
        last_bar_ms=ny_ms(dt.date(2026, 1, 31), 16, 59),
        initial_capital=CAPITAL,
    )

    assert result.summary.n_folds == 3
    assert result.folds[2].oos_stats.n == 0
    assert result.folds[2].ratio is None
    # A fold without OOS trades is not a positive fold.
    assert result.summary.share_positive_oos == pytest.approx(1 / 3)


def test_step_longer_than_oos_leaves_gaps_and_counts_each_entry_once() -> None:
    result = walk_forward(
        daily_entries(dt.date(2026, 1, 31)),
        WalkForwardConfig(is_days=5, oos_days=3, step_days=7),
        first_bar_ms=ny_ms(dt.date(2026, 1, 5), 0, 0),
        last_bar_ms=ny_ms(dt.date(2026, 1, 31), 16, 59),
        initial_capital=CAPITAL,
    )

    oos_days = [(f.oos_first_day, f.oos_last_day) for f in result.folds]
    d = dt.date
    assert oos_days == [
        (d(2026, 1, 10), d(2026, 1, 12)),
        (d(2026, 1, 17), d(2026, 1, 19)),
        (d(2026, 1, 24), d(2026, 1, 26)),
    ]
    assert result.summary.pooled_oos.n == 9


def test_windows_use_the_new_york_date_of_the_entry() -> None:
    # 2026-01-15T00:30Z is Jan 14 19:30 in New York: the last IS day of fold 0, not OOS.
    late = int(dt.datetime(2026, 1, 15, 0, 30, tzinfo=dt.UTC).timestamp()) * 1000
    entries = [entry(0, ny_ms(dt.date(2026, 1, 5)), 1.0), entry(1, late, -1.0)]

    result = walk_forward(
        entries,
        WalkForwardConfig(is_days=10, oos_days=5, step_days=5),
        first_bar_ms=ny_ms(dt.date(2026, 1, 5), 0, 0),
        last_bar_ms=ny_ms(dt.date(2026, 1, 19), 16, 59),
        initial_capital=CAPITAL,
    )

    assert result.folds[0].is_stats.n == 2
    assert result.folds[0].oos_stats.n == 0


def test_incomplete_entries_are_left_out() -> None:
    entries = [
        *daily_entries(dt.date(2026, 1, 19)),
        entry(99, ny_ms(dt.date(2026, 1, 16)), 3.0, complete=False),
    ]

    result = walk_forward(
        entries,
        WalkForwardConfig(is_days=10, oos_days=5, step_days=5),
        first_bar_ms=ny_ms(dt.date(2026, 1, 5), 0, 0),
        last_bar_ms=ny_ms(dt.date(2026, 1, 19), 16, 59),
        initial_capital=CAPITAL,
    )

    assert result.folds[0].oos_stats.n == 5
    assert result.folds[0].oos_stats.mean_r == pytest.approx(0.5)


def test_no_entries_gives_no_folds_and_no_efficiency() -> None:
    result = walk_forward(
        [],
        WalkForwardConfig(is_days=10, oos_days=5, step_days=5),
        first_bar_ms=ny_ms(dt.date(2026, 1, 5), 0, 0),
        last_bar_ms=ny_ms(dt.date(2026, 3, 31), 16, 59),
        initial_capital=CAPITAL,
    )

    assert result.folds == ()
    assert result.summary.n_folds == 0
    assert result.summary.share_positive_oos is None
    assert result.summary.efficiency is None
    assert result.summary.efficiency_note is not None
    assert result.summary.pooled_oos.n == 0


@pytest.mark.parametrize(
    ("is_days", "oos_days", "step_days", "message"),
    [
        (10, 5, 4, "çakışır"),
        (0, 5, 5, "is_days"),
        (10, 0, 5, "oos_days"),
        (10, 5, -1, "step_days"),
        (True, 5, 5, "is_days"),
    ],
)
def test_invalid_config_is_refused(
    is_days: int, oos_days: int, step_days: int, message: str
) -> None:
    with pytest.raises(WalkForwardError, match=message):
        WalkForwardConfig(is_days=is_days, oos_days=oos_days, step_days=step_days)


def test_reversed_span_is_refused() -> None:
    with pytest.raises(WalkForwardError, match="ters"):
        walk_forward(
            [],
            WalkForwardConfig(is_days=10, oos_days=5, step_days=5),
            first_bar_ms=ny_ms(dt.date(2026, 2, 1)),
            last_bar_ms=ny_ms(dt.date(2026, 1, 1)),
            initial_capital=CAPITAL,
        )


def test_entry_outside_the_stated_span_is_refused() -> None:
    with pytest.raises(WalkForwardError, match="aralığının dışında"):
        walk_forward(
            daily_entries(dt.date(2026, 1, 31)),
            WalkForwardConfig(is_days=10, oos_days=5, step_days=5),
            first_bar_ms=ny_ms(dt.date(2026, 1, 5), 0, 0),
            last_bar_ms=ny_ms(dt.date(2026, 1, 20), 16, 59),
            initial_capital=CAPITAL,
        )
