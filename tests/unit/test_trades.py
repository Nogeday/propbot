"""Trade list CSV loader and the day blocks the bootstrap resamples."""

import datetime as dt
import math
from pathlib import Path
from zoneinfo import ZoneInfo

import numpy as np
import pytest

from propbot.backtest.trades import (
    EVENT_ENTRY,
    EVENT_EXIT,
    EVENT_PAD,
    IntratradeModel,
    Trade,
    TradeListError,
    build_day_blocks,
    load_trades_csv,
    summarize_trades,
)

PRAGUE = ZoneInfo("Europe/Prague")
NEW_YORK = ZoneInfo("America/New_York")
MIDNIGHT = dt.time(0, 0)


def utc(text: str) -> dt.datetime:
    return dt.datetime.fromisoformat(text).replace(tzinfo=dt.UTC)


def trade(entry: str, exit_: str, r: float, mae_r: float | None = None) -> Trade:
    return Trade(entry_utc=utc(entry), exit_utc=utc(exit_), r=r, mae_r=mae_r)


def write_csv(tmp_path: Path, text: str) -> Path:
    path = tmp_path / "trades.csv"
    path.write_text(text, encoding="utf-8")
    return path


# --- CSV loader -------------------------------------------------------------------------


def test_loads_offsets_z_suffix_and_optional_columns(tmp_path: Path) -> None:
    path = write_csv(
        tmp_path,
        "entry_time,exit_time,r,strategy_id,mae_r\n"
        "2026-01-05T14:36:00Z,2026-01-05T15:10:00Z,2.0,ny_orb_fvg,-0.4\n"
        "2026-01-06T09:36:00-05:00,2026-01-06T10:02:00-05:00,-1,,\n"
        "2026-01-07 11:05:00+01:00,2026-01-07 12:00:00+01:00,0.35,asian_sweep,-0.9\n",
    )

    trades = load_trades_csv(path)

    assert trades == (
        Trade(utc("2026-01-05 14:36"), utc("2026-01-05 15:10"), 2.0, "ny_orb_fvg", -0.4),
        Trade(utc("2026-01-06 14:36"), utc("2026-01-06 15:02"), -1.0, None, None),
        Trade(utc("2026-01-07 10:05"), utc("2026-01-07 11:00"), 0.35, "asian_sweep", -0.9),
    )


def test_minimal_header_and_utf8_bom_are_accepted(tmp_path: Path) -> None:
    path = tmp_path / "trades.csv"
    path.write_bytes(
        b"\xef\xbb\xbfentry_time,exit_time,r\n2026-01-05T14:36:00Z,2026-01-05T15:10:00Z,2\n"
    )

    assert load_trades_csv(path) == (Trade(utc("2026-01-05 14:36"), utc("2026-01-05 15:10"), 2.0),)


def test_timestamps_without_offset_need_an_explicit_zone(tmp_path: Path) -> None:
    path = write_csv(
        tmp_path, "entry_time,exit_time,r\n2026-01-05 09:36:00,2026-01-05 10:10:00,2\n"
    )

    with pytest.raises(TradeListError, match="satır 2: entry_time saat dilimi içermiyor"):
        load_trades_csv(path)


def test_naive_timestamps_are_read_in_the_given_zone_across_dst(tmp_path: Path) -> None:
    path = write_csv(
        tmp_path,
        "entry_time,exit_time,r\n"
        "2026-01-05 09:36:00,2026-01-05 10:10:00,2\n"  # EST = UTC-5
        "2026-07-06 09:36:00,2026-07-06 10:10:00,-1\n",  # EDT = UTC-4
    )

    trades = load_trades_csv(path, naive_tz=NEW_YORK)

    assert [t.entry_utc for t in trades] == [utc("2026-01-05 14:36"), utc("2026-07-06 13:36")]


@pytest.mark.parametrize(
    ("text", "message"),
    [
        ("", "dosya boş"),
        ("entry_time,exit_time,r\n", "işlem yok"),
        ("entry_time,exit_time\n", "zorunlu sütun eksik: r"),
        ("entry,exit,r\n", "zorunlu sütun eksik: entry_time, exit_time"),
        ("entry_time,exit_time,r,mae\n", "bilinmeyen sütun: mae"),
        ("entry_time,exit_time,r,r\n", "yinelenen sütun: r"),
    ],
)
def test_bad_header_is_rejected(tmp_path: Path, text: str, message: str) -> None:
    with pytest.raises(TradeListError, match=message):
        load_trades_csv(write_csv(tmp_path, text))


@pytest.mark.parametrize(
    ("row", "message"),
    [
        ("2026-01-05,2026-01-05T15:10:00Z,2", "entry_time ISO 8601 tarih-saat olmalı"),
        ("05.01.2026 14:36,2026-01-05T15:10:00Z,2", "entry_time ISO 8601 tarih-saat olmalı"),
        ("2026-01-05T14:36:00Z,2026-13-05T15:10:00Z,2", "exit_time okunamadı"),
        ("2026-01-05T15:10:00Z,2026-01-05T14:36:00Z,2", "exit_time, entry_time'dan önce"),
        ("2026-01-05T14:36:00Z,2026-01-05T15:10:00Z,iki", "r sayı olmalı"),
        ("2026-01-05T14:36:00Z,2026-01-05T15:10:00Z,", "r sayı olmalı"),
        ("2026-01-05T14:36:00Z,2026-01-05T15:10:00Z,nan", "r sonlu bir sayı olmalı"),
        ("2026-01-05T14:36:00Z,2026-01-05T15:10:00Z,inf", "r sonlu bir sayı olmalı"),
        ("2026-01-05T14:36:00Z,2026-01-05T15:10:00Z,0,5", "başlıktan fazla alan var"),
        ("2026-01-05T14:36:00Z,2026-01-05T15:10:00Z", "başlıktan az alan var"),
    ],
)
def test_bad_row_is_rejected_with_its_line_number(tmp_path: Path, row: str, message: str) -> None:
    path = write_csv(tmp_path, f"entry_time,exit_time,r\n{row}\n")

    with pytest.raises(TradeListError) as excinfo:
        load_trades_csv(path)

    assert f"satır 2: {message}" in str(excinfo.value)


@pytest.mark.parametrize(
    ("r", "mae_r", "message"),
    [
        ("2", "0.1", "mae_r sıfırdan büyük olamaz"),
        ("-1", "-0.5", "işlem sonucundan (-1) iyi olamaz"),
        ("2", "kötü", "mae_r sayı olmalı"),
    ],
)
def test_inconsistent_mae_is_rejected(tmp_path: Path, r: str, mae_r: str, message: str) -> None:
    path = write_csv(
        tmp_path,
        f"entry_time,exit_time,r,mae_r\n2026-01-05T14:36:00Z,2026-01-05T15:10:00Z,{r},{mae_r}\n",
    )

    with pytest.raises(TradeListError) as excinfo:
        load_trades_csv(path)

    assert message in str(excinfo.value)


def test_mae_equal_to_a_losing_result_is_consistent(tmp_path: Path) -> None:
    path = write_csv(
        tmp_path,
        "entry_time,exit_time,r,mae_r\n2026-01-05T14:36:00Z,2026-01-05T15:10:00Z,-1,-1\n",
    )

    assert load_trades_csv(path)[0].mae_r == -1.0


def test_every_bad_row_is_reported_up_to_a_limit(tmp_path: Path) -> None:
    rows = "".join("2026-01-05T14:36:00Z,2026-01-05T15:10:00Z,x\n" for _ in range(25))
    path = write_csv(tmp_path, "entry_time,exit_time,r\n" + rows)

    with pytest.raises(TradeListError) as excinfo:
        load_trades_csv(path)

    lines = str(excinfo.value).splitlines()
    assert lines[1].startswith("satır 2:")
    assert lines[20].startswith("satır 21:")
    assert lines[21] == "... ve 5 hata daha"


def test_missing_file_is_reported(tmp_path: Path) -> None:
    with pytest.raises(TradeListError, match="dosya bulunamadı"):
        load_trades_csv(tmp_path / "yok.csv")


# --- day blocks -------------------------------------------------------------------------


def test_pool_holds_empty_weekdays_and_any_day_with_a_trade() -> None:
    trades = [
        trade("2026-01-05 14:36", "2026-01-05 15:10", 2.0),  # Monday
        trade("2026-01-08 14:36", "2026-01-08 15:10", -1.0),  # Thursday
        trade("2026-01-10 14:36", "2026-01-10 15:10", -1.0),  # Saturday
    ]

    blocks = build_day_blocks(trades, zone=PRAGUE, reset=MIDNIGHT)

    assert blocks.days == (
        dt.date(2026, 1, 5),
        dt.date(2026, 1, 6),
        dt.date(2026, 1, 7),
        dt.date(2026, 1, 8),
        dt.date(2026, 1, 9),
        dt.date(2026, 1, 10),
    )
    assert blocks.trade_count.tolist() == [1, 0, 0, 1, 0, 1]
    assert blocks.n_days == 6
    assert blocks.max_trades == 1


def test_trades_are_grouped_by_the_prop_day_of_their_entry() -> None:
    trades = [
        # 20:30 in New York on the 14th is 02:30 in Prague on the 15th.
        trade("2026-01-15 01:30", "2026-01-15 02:00", 2.0),
        trade("2026-01-15 14:36", "2026-01-15 15:10", -1.0),
        trade("2026-01-14 14:36", "2026-01-14 15:10", 0.5),
    ]

    blocks = build_day_blocks(trades, zone=PRAGUE, reset=MIDNIGHT)

    assert blocks.days == (dt.date(2026, 1, 14), dt.date(2026, 1, 15))
    assert blocks.trade_count.tolist() == [1, 2]
    assert blocks.r.tolist() == [[0.5, 0.0], [2.0, -1.0]]  # slots follow entry time
    assert blocks.trades_crossing_reset == 0


def test_trade_open_across_the_reset_is_counted() -> None:
    # 22:30 UTC is before the Prague midnight (23:00 UTC in winter); 23:30 UTC is after it.
    trades = [trade("2026-01-14 22:30", "2026-01-14 23:30", 1.0)]

    blocks = build_day_blocks(trades, zone=PRAGUE, reset=MIDNIGHT)

    assert blocks.days == (dt.date(2026, 1, 14),)
    assert blocks.trades_crossing_reset == 1


def test_events_are_time_ordered_and_padded() -> None:
    trades = [
        trade("2026-01-05 10:00", "2026-01-05 12:00", 2.0),  # slot 0, spans slot 1
        trade("2026-01-05 11:00", "2026-01-05 11:30", -1.0),  # slot 1
        trade("2026-01-06 10:00", "2026-01-06 10:00", 0.5),  # filled and closed on one bar
    ]

    blocks = build_day_blocks(trades, zone=PRAGUE, reset=MIDNIGHT)

    assert blocks.event_kind.tolist() == [
        [EVENT_ENTRY, EVENT_ENTRY, EVENT_EXIT, EVENT_EXIT],
        [EVENT_ENTRY, EVENT_EXIT, EVENT_PAD, EVENT_PAD],
    ]
    assert blocks.event_trade.tolist() == [[0, 1, 1, 0], [0, 0, 0, 0]]


def test_entry_sorts_before_an_exit_at_the_same_timestamp() -> None:
    # Pessimistic tie-break: the two trades are treated as open together.
    trades = [
        trade("2026-01-05 10:00", "2026-01-05 11:00", 2.0),
        trade("2026-01-05 11:00", "2026-01-05 12:00", -1.0),
    ]

    blocks = build_day_blocks(trades, zone=PRAGUE, reset=MIDNIGHT)

    assert blocks.event_kind.tolist() == [[EVENT_ENTRY, EVENT_ENTRY, EVENT_EXIT, EVENT_EXIT]]
    assert blocks.event_trade.tolist() == [[0, 1, 0, 1]]


@pytest.mark.parametrize(
    ("r", "mae_r", "cost_r", "expected_r", "expected_mae"),
    [
        (2.0, None, 0.0, 2.0, -1.0),  # a winner is assumed to have dipped to its stop
        (-1.0, None, 0.0, -1.0, -1.0),
        (-0.4, None, 0.0, -0.4, -1.0),  # early exit: the stop was still 1R away
        (-1.0, None, 0.05, -1.05, -1.05),  # costs push the realised loss beyond 1R
        (-1.3, None, 0.0, -1.3, -1.3),  # slipped stop
        (2.0, -0.3, 0.0, 2.0, -0.3),  # measured excursion replaces the assumption
        (-0.3, -0.3, 0.05, -0.35, -0.35),  # never better than the net result
        (0.02, -0.01, 0.05, -0.03, -0.03),
    ],
)
def test_worst_floating_result_in_the_stop_model(
    r: float, mae_r: float | None, cost_r: float, expected_r: float, expected_mae: float
) -> None:
    trades = [trade("2026-01-05 10:00", "2026-01-05 11:00", r, mae_r)]

    blocks = build_day_blocks(trades, zone=PRAGUE, reset=MIDNIGHT, cost_r=cost_r)

    assert blocks.r[0, 0] == pytest.approx(expected_r)
    assert blocks.mae[0, 0] == pytest.approx(expected_mae)
    assert blocks.mae[0, 0] <= min(0.0, blocks.r[0, 0])


def test_closed_model_ignores_floating_results() -> None:
    trades = [
        trade("2026-01-05 10:00", "2026-01-05 11:00", 2.0, -0.8),
        trade("2026-01-05 12:00", "2026-01-05 13:00", -1.0),
    ]

    blocks = build_day_blocks(
        trades, zone=PRAGUE, reset=MIDNIGHT, intratrade=IntratradeModel.CLOSED
    )

    assert not blocks.mae.any()
    assert blocks.r.tolist() == [[2.0, -1.0]]


def test_reset_time_and_zone_come_from_the_caller() -> None:
    trades = [
        trade("2026-01-15 21:30", "2026-01-15 21:45", 1.0),  # 16:30 New York
        trade("2026-01-15 22:30", "2026-01-15 22:45", 1.0),  # 17:30 New York
    ]

    blocks = build_day_blocks(trades, zone=NEW_YORK, reset=dt.time(17, 0))

    assert blocks.days == (dt.date(2026, 1, 14), dt.date(2026, 1, 15))


@pytest.mark.parametrize("cost_r", [-0.01, math.nan, math.inf])
def test_invalid_cost_is_rejected(cost_r: float) -> None:
    trades = [trade("2026-01-05 10:00", "2026-01-05 11:00", 2.0)]

    with pytest.raises(ValueError, match="cost_r"):
        build_day_blocks(trades, zone=PRAGUE, reset=MIDNIGHT, cost_r=cost_r)


def test_empty_trade_list_is_rejected() -> None:
    with pytest.raises(TradeListError, match="işlem listesi boş"):
        build_day_blocks([], zone=PRAGUE, reset=MIDNIGHT)


def test_trade_statistics_use_the_net_results() -> None:
    trades = [
        trade("2026-01-05 10:00", "2026-01-05 11:00", 2.1),
        trade("2026-01-05 12:00", "2026-01-05 13:00", -0.9),
        trade("2026-01-07 10:00", "2026-01-07 11:00", -0.9),
        trade("2026-01-08 10:00", "2026-01-08 11:00", 2.1),
    ]

    stats = summarize_trades(build_day_blocks(trades, zone=PRAGUE, reset=MIDNIGHT, cost_r=0.1))

    assert stats.n_trades == 4
    assert stats.n_days == 4  # Mon..Thu, Tuesday is empty
    assert stats.n_days_with_trades == 3
    assert (stats.first_day, stats.last_day) == (dt.date(2026, 1, 5), dt.date(2026, 1, 8))
    assert stats.mean_r == pytest.approx(0.5)
    assert stats.std_r == pytest.approx(math.sqrt(3.0))
    assert stats.win_rate == 0.5
    assert stats.t_stat == pytest.approx(0.5 / (math.sqrt(3.0) / 2.0))


def test_statistics_of_a_single_trade_have_no_t_statistic() -> None:
    trades = [trade("2026-01-05 10:00", "2026-01-05 11:00", 2.0)]

    stats = summarize_trades(build_day_blocks(trades, zone=PRAGUE, reset=MIDNIGHT))

    assert stats.std_r == 0.0
    assert math.isnan(stats.t_stat)


def test_block_arrays_have_consistent_shapes_and_types() -> None:
    trades = [
        trade("2026-01-05 10:00", "2026-01-05 11:00", 2.0),
        trade("2026-01-05 12:00", "2026-01-05 13:00", -1.0),
        trade("2026-01-06 10:00", "2026-01-06 11:00", -1.0),
    ]

    blocks = build_day_blocks(trades, zone=PRAGUE, reset=MIDNIGHT)

    assert blocks.r.shape == blocks.mae.shape == (2, 2)
    assert blocks.event_kind.shape == blocks.event_trade.shape == (2, 4)
    assert blocks.r.dtype == blocks.mae.dtype == np.float64
    assert (blocks.event_kind != EVENT_PAD).sum(axis=1).tolist() == [4, 2]
