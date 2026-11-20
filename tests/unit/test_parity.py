"""Parity infrastructure (SPEC §6.6): TradingView export parser, matcher, report, golden files.

The `sample_*` fixtures are hand-written parser inputs, not TradingView evidence.
"""

import dataclasses
import datetime as dt
from pathlib import Path

import pytest

from propbot.backtest.artifacts import SYNTHETIC_TAG, load_artifact, write_artifact
from propbot.backtest.model import ExitReason, FillModel, TradeRecord
from propbot.backtest.parity import (
    EXPECTED_DEVIATION_HEADING,
    EXPECTED_DEVIATIONS,
    GOLDEN_MARKER,
    MAX_REPORTED_ERRORS,
    Field,
    ParityError,
    ParityResult,
    TvFill,
    TvTradeList,
    compare_trades,
    diff_golden,
    golden_rows,
    load_tradingview_trades,
    parity_artifact,
    read_golden,
    render_parity_report,
    warmup_cutoff_ms,
    write_golden,
)
from propbot.strategies.base import Side
from propbot.strategies.registry import STRATEGIES
from support.paths import FIXTURES

SAMPLES = FIXTURES / "parity"
NY = "America/New_York"
TICK = 0.25
MINUTE = 60_000
# 2026-03-02 09:36 New York (EST) = 14:36 UTC.
T0 = 1_772_462_160_000
CREATED = dt.datetime(2026, 11, 18, 12, 0, tzinfo=dt.UTC)


def ms(text: str) -> int:
    return int(dt.datetime.fromisoformat(text).timestamp() * 1000)


def record(
    *,
    seq: int = 0,
    side: Side = Side.LONG,
    entry_ts_ms: int = T0,
    exit_ts_ms: int = T0 + 29 * MINUTE,
    entry_price: float = 6812.25,
    exit_price: float = 6830.75,
    exit_id: str = "Exit L",
    comment: str = "",
    reason: ExitReason = ExitReason.TARGET,
    qty: float = 3.0,
) -> TradeRecord:
    return TradeRecord(
        seq=seq,
        strategy_id="ny_orb_fvg",
        entry_id="Long" if side is Side.LONG else "Short",
        exit_id=exit_id,
        reason=reason,
        setup="Long" if side is Side.LONG else "Short",
        comment=comment,
        side=side,
        qty=qty,
        entry_bar_index=6,
        exit_bar_index=35,
        entry_ts_ms=entry_ts_ms,
        exit_ts_ms=exit_ts_ms,
        entry_price=entry_price,
        exit_price=exit_price,
        gross_pnl=10.0,
        commission=1.0,
        pnl=9.0,
        initial_stop=entry_price - 5.0 * side.sign,
        risk_amount=15.0,
        r=0.6,
        mae_r=-0.2,
    )


def tv_of(row: TradeRecord, *, trade_no: int = 1, signal: str | None = None) -> TvFill:
    return TvFill(
        trade_no=trade_no,
        side=row.side,
        entry_ts_ms=row.entry_ts_ms,
        entry_price=row.entry_price,
        entry_signal=row.setup,
        exit_ts_ms=row.exit_ts_ms,
        exit_price=row.exit_price,
        exit_signal=(row.comment or row.exit_id) if signal is None else signal,
        qty=row.qty,
        line=0,
    )


def compare(
    python: list[TradeRecord], tv: list[TvFill] | TvTradeList, **kwargs: object
) -> ParityResult:
    options: dict[str, object] = {
        "fill_model": FillModel.TV_OHLC,
        "tick_size": TICK,
        "tf_seconds": 60,
        **kwargs,
    }
    return compare_trades(python, tv, **options)  # type: ignore[arg-type]


def series(n: int, *, step_days: int = 1) -> list[TradeRecord]:
    return [
        record(
            seq=i,
            entry_ts_ms=T0 + i * step_days * 86_400_000,
            exit_ts_ms=T0 + i * step_days * 86_400_000 + 29 * MINUTE,
        )
        for i in range(n)
    ]


def write(tmp_path: Path, name: str, text: str, encoding: str = "utf-8") -> Path:
    path = tmp_path / name
    path.write_bytes(text.encode(encoding))
    return path


# --- parser ------------------------------------------------------------------------------


def comparable(fills: tuple[TvFill, ...]) -> list[tuple[object, ...]]:
    return [
        (f.side, f.entry_ts_ms, f.entry_price, f.exit_ts_ms, f.exit_price, f.exit_signal, f.qty)
        for f in fills
    ]


def test_three_dialects_of_the_sample_export_parse_to_the_same_fills() -> None:
    comma = load_tradingview_trades(SAMPLES / "sample_tv_basic_comma.csv", tz=NY)
    semicolon = load_tradingview_trades(SAMPLES / "sample_tv_semicolon_decimal_comma.csv", tz=NY)
    tab = load_tradingview_trades(SAMPLES / "sample_tv_tab.tsv", tz=NY)
    assert (comma.delimiter, semicolon.delimiter, tab.delimiter) == (",", ";", "\t")
    assert (comma.decimal_mark, semicolon.decimal_mark, tab.decimal_mark) == (".", ",", ".")
    assert comparable(comma.fills) == comparable(semicolon.fills) == comparable(tab.fills)
    first, second = comma.fills
    assert first.side is Side.LONG
    assert first.entry_ts_ms == T0
    assert first.entry_price == 6812.25  # "6,812.25": thousands separator, not a decimal
    assert first.exit_signal == "Exit L"
    assert second.side is Side.SHORT
    assert second.exit_signal == "EOD close"
    assert second.exit_ts_ms == ms("2026-03-03T20:59:00+00:00")


def test_descending_export_is_read_regardless_of_row_order() -> None:
    tv = load_tradingview_trades(SAMPLES / "sample_tv_basic_comma.csv", tz=NY)
    assert [f.trade_no for f in tv.fills] == [1, 2]
    assert tv.sample


def test_open_trade_is_skipped_and_counted() -> None:
    comma = load_tradingview_trades(SAMPLES / "sample_tv_basic_comma.csv", tz=NY)
    assert comma.open_trades == 1
    assert any("açık işlem" in s.reason for s in comma.skipped)
    tab = load_tradingview_trades(SAMPLES / "sample_tv_tab.tsv", tz=NY)  # exit row says "Open"
    assert tab.open_trades == 1
    assert len(tab.fills) == 2


def test_column_aliases_and_ignored_columns_are_reported() -> None:
    semicolon = load_tradingview_trades(SAMPLES / "sample_tv_semicolon_decimal_comma.csv", tz=NY)
    assert dict(semicolon.columns)["time"] == "Date and time"
    assert dict(semicolon.columns)["qty"] == "Position size"
    assert semicolon.ignored_columns == ("Net P&L USD", "Net P&L %")
    tab = load_tradingview_trades(SAMPLES / "sample_tv_tab.tsv", tz=NY)
    assert dict(tab.columns)["price"] == "price usd"


def test_bom_is_accepted(tmp_path: Path) -> None:
    text = (
        "\ufeffTrade #,Type,Signal,Date/Time,Price\n1,Entry Long,L,2026-03-02 09:36,1\n"
        "1,Exit Long,X,2026-03-02 09:40,2\n"
    )
    tv = load_tradingview_trades(write(tmp_path, "x.csv", text), tz=NY)
    assert tv.fills[0].qty is None
    assert dict(tv.columns)["trade_no"] == "Trade #"


def test_partial_exits_on_one_trade_number_give_one_fill_each() -> None:
    tv = load_tradingview_trades(SAMPLES / "sample_tv_partial_same_trade.csv", tz=NY)
    assert [(f.trade_no, f.exit_signal, f.qty) for f in tv.fills] == [
        (1, "TP1", 2.0),
        (1, "Init stop", 2.0),
    ]


def test_partial_exits_split_into_trade_numbers_give_the_same_fills() -> None:
    same = load_tradingview_trades(SAMPLES / "sample_tv_partial_same_trade.csv", tz=NY)
    split = load_tradingview_trades(SAMPLES / "sample_tv_partial_split.csv", tz=NY)
    assert comparable(same.fills) == comparable(split.fills)


def test_bad_rows_are_listed_with_line_numbers() -> None:
    with pytest.raises(ParityError) as caught:
        load_tradingview_trades(SAMPLES / "sample_tv_bad_rows.csv", tz=NY)
    message = str(caught.value)
    assert "satır 4: 'Type'" in message
    assert "satır 5: Date/Time" in message
    assert "satır 6: fiyat sayı olmalı" in message
    assert "satır 7: 5 alan var" in message
    assert "satır 9: 'Trade #'" in message
    assert "işlem #5 (satır 8): giriş satırı yok" in message


def test_error_list_is_capped(tmp_path: Path) -> None:
    rows = "".join(f"{i},Entry Nowhere,L,2026-03-02 09:36,1\n" for i in range(1, 40))
    path = write(tmp_path, "x.csv", "Trade #,Type,Signal,Date/Time,Price\n" + rows)
    with pytest.raises(ParityError) as caught:
        load_tradingview_trades(path, tz=NY)
    lines = str(caught.value).splitlines()
    assert len(lines) == 1 + MAX_REPORTED_ERRORS + 1
    assert lines[-1] == "... ve 19 hata daha"


def test_missing_required_column_lists_found_columns_and_aliases() -> None:
    with pytest.raises(ParityError) as caught:
        load_tradingview_trades(SAMPLES / "sample_tv_missing_column.csv", tz=NY)
    message = str(caught.value)
    assert "zorunlu sütun bulunamadı" in message
    assert "'price'" in message
    assert "'Price USD'" in message
    assert "'Contracts'" in message


def test_two_headers_for_one_column_are_refused(tmp_path: Path) -> None:
    path = write(tmp_path, "x.csv", "Trade #,Type,Signal,Date/Time,Price,Price USD\n")
    with pytest.raises(ParityError, match="aynı sütuna"):
        load_tradingview_trades(path, tz=NY)


def test_direction_conflict_is_refused() -> None:
    with pytest.raises(ParityError, match="yönü çelişiyor"):
        load_tradingview_trades(SAMPLES / "sample_tv_direction_conflict.csv", tz=NY)


def test_size_column_with_a_currency_is_not_taken_as_quantity(tmp_path: Path) -> None:
    text = (
        "Trade #,Type,Signal,Date/Time,Price,Position size USD\n"
        "1,Entry Long,L,2026-03-02 09:36,1,500\n1,Exit Long,X,2026-03-02 09:40,2,500\n"
    )
    tv = load_tradingview_trades(write(tmp_path, "x.csv", text), tz=NY)
    assert tv.fills[0].qty is None
    assert tv.ignored_columns == ("Position size USD",)


def test_decimal_comma_with_comma_delimiter_is_refused(tmp_path: Path) -> None:
    text = (
        'Trade #,Type,Signal,Date/Time,Price\n1,Entry Long,L,2026-03-02 09:36,"6812,25"\n'
        '1,Exit Long,X,2026-03-02 09:40,"6813,5"\n'
    )
    with pytest.raises(ParityError, match="ondalık virgül yalnızca"):
        load_tradingview_trades(write(tmp_path, "x.csv", text), tz=NY)


def test_mixed_decimal_marks_are_refused(tmp_path: Path) -> None:
    text = (
        "Trade #;Type;Signal;Date/Time;Price\n1;Entry Long;L;2026-03-02 09:36;6812,25\n"
        "1;Exit Long;X;2026-03-02 09:40;6813.5\n"
    )
    with pytest.raises(ParityError, match="tutarsız"):
        load_tradingview_trades(write(tmp_path, "x.csv", text), tz=NY)


def test_ambiguous_thousands_without_decimals_are_refused_outside_comma_files(
    tmp_path: Path,
) -> None:
    text = (
        "Trade #;Type;Signal;Date/Time;Price\n1;Entry Long;L;2026-03-02 09:36;6,812\n"
        "1;Exit Long;X;2026-03-02 09:40;6,813\n"
    )
    with pytest.raises(ParityError, match="belirlenemedi"):
        load_tradingview_trades(write(tmp_path, "x.csv", text), tz=NY)


def test_misplaced_thousands_separator_is_a_row_error(tmp_path: Path) -> None:
    text = (
        'Trade #,Type,Signal,Date/Time,Price\n1,Entry Long,L,2026-03-02 09:36,"68,12.25"\n'
        "1,Exit Long,X,2026-03-02 09:40,6813.50\n"
    )
    with pytest.raises(ParityError, match="satır 2: fiyat sayı olmalı"):
        load_tradingview_trades(write(tmp_path, "x.csv", text), tz=NY)


def test_ambiguous_delimiter_and_missing_delimiter_are_refused(tmp_path: Path) -> None:
    with pytest.raises(ParityError, match="ayırıcı belirsiz"):
        load_tradingview_trades(write(tmp_path, "a.csv", "a,b;c\n"), tz=NY)
    with pytest.raises(ParityError, match="ayırıcı bulunamadı"):
        load_tradingview_trades(write(tmp_path, "b.csv", "abc\n"), tz=NY)


def test_unreadable_inputs_have_clean_errors(tmp_path: Path) -> None:
    with pytest.raises(ParityError, match="dosya bulunamadı"):
        load_tradingview_trades(tmp_path / "none.csv", tz=NY)
    with pytest.raises(ParityError, match="UTF-8 değil"):
        load_tradingview_trades(write(tmp_path, "l.csv", "Trade #,Type\nü\n", "latin-1"), tz=NY)
    with pytest.raises(ParityError, match="bilinmeyen saat dilimi"):
        load_tradingview_trades(SAMPLES / "sample_tv_basic_comma.csv", tz="Mars/Base")
    with pytest.raises(ParityError, match="işlem yok"):
        load_tradingview_trades(
            write(tmp_path, "h.csv", "Trade #,Type,Signal,Date/Time,Price\n"), tz=NY
        )


def test_size_cap(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    import propbot.backtest.parity as parity

    monkeypatch.setattr(parity, "MAX_EXPORT_BYTES", 64)
    path = write(tmp_path, "big.csv", "Trade #,Type,Signal,Date/Time,Price\n" + "x" * 100)
    with pytest.raises(ParityError, match="çok büyük"):
        load_tradingview_trades(path, tz=NY)


def test_chart_time_zone_across_both_2026_dst_changes() -> None:
    new_york = load_tradingview_trades(SAMPLES / "sample_tv_dst_new_york.csv", tz=NY)
    utc = load_tradingview_trades(SAMPLES / "sample_tv_dst_utc.csv", tz="UTC")
    assert comparable(new_york.fills) == comparable(utc.fills)
    entries = [f.entry_ts_ms for f in new_york.fills]
    assert entries == [
        ms("2026-03-06T14:36:00+00:00"),  # EST, before 8 March
        ms("2026-03-09T13:36:00+00:00"),  # EDT
        ms("2026-10-30T13:36:00+00:00"),  # EDT, before 1 November
        ms("2026-11-02T14:36:00+00:00"),  # EST
    ]


def test_ambiguous_and_missing_local_times_are_refused(tmp_path: Path) -> None:
    for moment in ("2026-11-01 01:30", "2026-03-08 02:30"):
        text = (
            f"Trade #,Type,Signal,Date/Time,Price\n1,Entry Long,L,{moment},1\n"
            "1,Exit Long,X,2026-11-02 09:40,2\n"
        )
        with pytest.raises(ParityError, match="yaz saati"):
            load_tradingview_trades(write(tmp_path, "x.csv", text), tz=NY)


def test_explicit_offset_is_honoured(tmp_path: Path) -> None:
    text = (
        "Trade #,Type,Signal,Date/Time,Price\n1,Entry Long,L,2026-03-02T14:36:00Z,1\n"
        "1,Exit Long,X,2026-03-02 09:40:00-05:00,2\n"
    )
    tv = load_tradingview_trades(write(tmp_path, "x.csv", text), tz=NY)
    assert tv.fills[0].entry_ts_ms == T0
    assert tv.fills[0].exit_ts_ms == T0 + 4 * MINUTE


# --- matching ----------------------------------------------------------------------------


def test_identical_lists_match_fully() -> None:
    python = series(5)
    result = compare(python, [tv_of(row, trade_no=i + 1) for i, row in enumerate(python)])
    assert len(result.matched) == 5
    assert result.match_rate == 1.0
    assert result.passed
    assert result.reason_matched_by == {"comment": 0, "id": 5}


def test_exit_reason_matches_comment_or_id_case_and_space_insensitive() -> None:
    by_comment = record(exit_id="", comment="EOD close", reason=ExitReason.CLOSE)
    by_id = record(exit_id="Exit L", comment="Init stop")
    result = compare(
        [by_comment, by_id],
        [tv_of(by_comment, signal="  eod CLOSE "), tv_of(by_id, signal="exit l")],
    )
    assert len(result.matched) == 2
    assert result.reason_matched_by == {"comment": 1, "id": 1}


@pytest.mark.parametrize(
    ("change", "field"),
    [
        ({"entry_ts_ms": T0 + MINUTE}, Field.ENTRY_TIME),
        ({"side": Side.SHORT}, Field.SIDE),
        ({"exit_signal": "EOD close"}, Field.EXIT_REASON),
        ({"entry_price": 6812.25 + 2 * TICK}, Field.ENTRY_PRICE),
        ({"exit_price": 6830.75 - 2 * TICK}, Field.EXIT_PRICE),
    ],
)
def test_each_single_field_difference_is_caught_and_named(
    change: dict[str, object], field: Field
) -> None:
    row = record()
    tv = dataclasses.replace(tv_of(row), **change)  # type: ignore[arg-type]
    result = compare([row], [tv])
    assert len(result.mismatches) == 1
    assert result.mismatches[0].fields == (field,)
    assert result.field_counts[field.value] == 1
    assert result.match_rate == 0.0
    assert not result.passed


def test_exit_time_alone_is_information_not_a_mismatch() -> None:
    row = record()
    result = compare([row], [dataclasses.replace(tv_of(row), exit_ts_ms=row.exit_ts_ms + MINUTE)])
    assert len(result.matched) == 1
    assert result.exit_time_differs == 1


@pytest.mark.parametrize("tick", [0.25, 0.1, 0.01])
def test_price_exactly_one_tick_off_matches_and_one_tick_plus_epsilon_does_not(
    tick: float,
) -> None:
    row = record(entry_price=4500.1, exit_price=4510.3)
    one_tick = dataclasses.replace(
        tv_of(row), entry_price=row.entry_price + tick, exit_price=row.exit_price - tick
    )
    result = compare([row], [one_tick], tick_size=tick)
    assert len(result.matched) == 1
    beyond = dataclasses.replace(tv_of(row), exit_price=row.exit_price + tick * 1.0001)
    result = compare([row], [beyond], tick_size=tick)
    assert result.mismatches[0].fields == (Field.EXIT_PRICE,)


def test_time_tolerance_is_opt_in_and_counted() -> None:
    row = record()
    late = dataclasses.replace(tv_of(row), entry_ts_ms=row.entry_ts_ms + MINUTE)
    assert compare([row], [late]).mismatches
    result = compare([row], [late], time_tolerance_bars=1)
    assert len(result.matched) == 1
    assert result.time_tolerance_pairs == 1


def test_duplicate_keys_are_paired_in_exit_order() -> None:
    tp1 = record(
        seq=0, exit_id="TP1", comment="TP1", exit_ts_ms=T0 + 25 * MINUTE, exit_price=6825.0
    )
    rest = record(
        seq=0,
        exit_id="Init",
        comment="Init stop",
        exit_ts_ms=T0 + 44 * MINUTE,
        exit_price=6815.25,
        reason=ExitReason.STOP,
    )
    tv_rest = dataclasses.replace(tv_of(rest), exit_price=6816.25)
    result = compare([rest, tp1], [tv_rest, tv_of(tp1)])
    assert [p.python.exit_id for p in result.matched] == ["TP1"]
    (mismatch,) = result.mismatches
    assert mismatch.python.exit_id == "Init"
    assert mismatch.fields == (Field.EXIT_PRICE,)


def test_partial_exit_sample_matches_python_partial_rows() -> None:
    tv = load_tradingview_trades(SAMPLES / "sample_tv_partial_split.csv", tz=NY)
    tp1 = record(
        exit_id="TP1", comment="TP1", exit_ts_ms=T0 + 25 * MINUTE, exit_price=6825.0, qty=2.0
    )
    stop = record(
        exit_id="Init",
        comment="Init stop",
        exit_ts_ms=T0 + 44 * MINUTE,
        exit_price=6815.25,
        qty=2.0,
        reason=ExitReason.STOP,
    )
    result = compare([tp1, stop], tv)
    assert len(result.matched) == 2
    assert result.sample_input


def test_one_sided_rows_are_listed() -> None:
    python = series(3)
    tv = [tv_of(python[0]), tv_of(python[1])]
    extra_tv = dataclasses.replace(
        tv_of(python[1]), entry_ts_ms=T0 + 10 * 86_400_000, exit_ts_ms=T0 + 10 * 86_400_000 + MINUTE
    )
    result = compare(python, [*tv, extra_tv], clip_to_overlap=False)
    assert [r.seq for r in result.python_only] == [2]
    assert result.tradingview_only == (extra_tv,)
    assert result.match_rate == 2 / 4


def test_threshold_boundary_is_inclusive() -> None:
    python = series(50)
    tv = [tv_of(row) for row in python[:49]]
    result = compare(python, tv, clip_to_overlap=False)
    assert result.match_rate == 0.98
    assert result.passed
    result = compare([*python[:49], python[49]], tv[:48], clip_to_overlap=False)
    assert result.match_rate == 48 / 50
    assert not result.passed


def test_threshold_below_the_floor_is_refused_and_stricter_is_used() -> None:
    with pytest.raises(ParityError, match="tabanı"):
        compare(series(1), [], threshold=0.97)
    python = series(50)
    result = compare(
        python, [tv_of(row) for row in python[:49]], threshold=0.99, clip_to_overlap=False
    )
    assert not result.passed


def test_empty_comparison_does_not_pass() -> None:
    result = compare([], [])
    assert result.match_rate is None
    assert not result.passed
    assert "boş karşılaştırma geçemez" in render_parity_report(result)


def test_pessimistic_run_is_refused() -> None:
    with pytest.raises(ParityError, match="tv_ohlc"):
        compare(series(1), [], fill_model=FillModel.PESSIMISTIC)


def test_end_of_data_rows_and_warmup_are_excluded_and_counted() -> None:
    python = series(4)
    tv = [tv_of(row) for row in python[:3]]
    python[3] = dataclasses.replace(python[3], reason=ExitReason.END_OF_DATA)
    cutoff = python[1].entry_ts_ms
    result = compare(python, tv, warmup_cutoff_ms=cutoff)
    assert result.exclusions.python_end_of_data == 1
    assert result.exclusions.python_warmup == 1
    assert result.exclusions.tradingview_warmup == 1
    assert len(result.matched) == 2
    assert result.compared == 2


def test_warmup_cutoff_counts_bars_not_minutes() -> None:
    opens = [0, MINUTE, 10 * MINUTE, 11 * MINUTE]
    assert warmup_cutoff_ms(opens, 0) is None
    assert warmup_cutoff_ms(opens, 2) == 10 * MINUTE
    with pytest.raises(ParityError):
        warmup_cutoff_ms(opens, 4)


def test_only_the_overlap_of_both_spans_is_compared() -> None:
    python = series(5)
    tv = [tv_of(row) for row in python[1:4]]
    result = compare(python, tv)
    assert len(result.matched) == 3
    assert result.exclusions.python_outside_window == 2
    reach = 3 * MINUTE  # widened by the pairing window
    assert result.window == (python[1].entry_ts_ms - reach, python[3].entry_ts_ms + reach)
    # 2 of 5 Python rows left out is far beyond the excluded-share bound: no pass.
    assert result.match_rate == 1.0
    assert not result.passed
    assert len(result.blocked_by) == 1
    assert result.blocked_by[0].startswith("Python listesinin 2/5 satırı")


def test_invalid_settings_are_refused() -> None:
    for kwargs in (
        {"tick_size": 0.0},
        {"tf_seconds": 0},
        {"tolerance_ticks": -1.0},
        {"time_tolerance_bars": -1},
        {"time_tolerance_bars": 5, "pair_window_bars": 2},
        {"threshold": 1.5},
        {"tolerance_ticks": 1.5},
        {"time_tolerance_bars": 2, "pair_window_bars": 3},
    ):
        with pytest.raises(ParityError):
            compare([], [], **kwargs)


# --- report and artifact -----------------------------------------------------------------


def test_report_shows_verdict_counts_table_and_reminders() -> None:
    python = series(3)
    tv = [tv_of(python[0]), dataclasses.replace(tv_of(python[1]), exit_signal="EOD close")]
    result = compare(python, tv, clip_to_overlap=False)
    text = render_parity_report(result)
    assert text.startswith("Parite (SPEC §6.6) — GEÇMEDİ: eşleşme %33.33 (eşik %98.00)")
    assert "Uyuşmayan: 1" in text
    assert "Yalnızca Python: 1" in text
    assert "Alan bazında uyuşmazlık: çıkış nedeni 1" in text
    assert "uyuşmaz" in text
    assert "EOD close" in text
    assert "Exit L" in text
    assert "doğrulanamadı (VERIFY)" in text


def test_artifact_from_sample_input_is_synthetic_and_round_trips(tmp_path: Path) -> None:
    tv = load_tradingview_trades(SAMPLES / "sample_tv_basic_comma.csv", tz=NY)
    long_row = record()
    short_row = record(
        seq=1,
        side=Side.SHORT,
        entry_ts_ms=ms("2026-03-03T14:41:00+00:00"),
        exit_ts_ms=ms("2026-03-03T20:59:00+00:00"),
        entry_price=6790.5,
        exit_price=6785.0,
        exit_id="",
        comment="EOD close",
        reason=ExitReason.CLOSE,
        qty=2.0,
    )
    result = compare([long_row, short_row], tv)
    assert result.passed
    artifact = parity_artifact(result, label="sample", created=CREATED, strategy_id="ny_orb_fvg")
    assert artifact.synthetic
    assert any(SYNTHETIC_TAG in note for note in artifact.notes)
    assert artifact.sources == ("sample_tv_basic_comma.csv",)
    loaded = load_artifact(write_artifact(artifact, tmp_path))
    assert loaded == artifact


def test_artifact_lists_mismatches_and_one_sided_rows() -> None:
    python = series(2)
    tv = [dataclasses.replace(tv_of(python[0]), side=Side.SHORT)]
    result = compare(python, tv, clip_to_overlap=False)
    artifact = parity_artifact(result, label="x", created=CREATED, strategy_id="s")
    assert not artifact.synthetic
    assert [m["kind"] for m in artifact.mismatches] == ["mismatch", "python_only"]
    assert artifact.mismatches[0]["fields"] == ["side"]
    assert not artifact.passed
    assert (artifact.matched, artifact.mismatched, artifact.python_only) == (0, 1, 1)


def test_the_artifact_counts_mismatched_fills_so_its_rate_agrees_with_its_counts() -> None:
    # The artifact model refuses a rate that is not matched / (all compared fills). The
    # rate's denominator includes paired-but-differing fills, so the artifact must carry
    # their count: without it a real run with one mismatch could not be written at all.
    python = series(3)
    tv = [
        tv_of(python[0]),
        dataclasses.replace(tv_of(python[1]), side=Side.SHORT),
        tv_of(python[2]),
    ]
    result = compare(python, tv, clip_to_overlap=False)

    artifact = parity_artifact(result, label="x", created=CREATED, strategy_id="s")

    assert (artifact.matched, artifact.mismatched) == (2, 1)
    assert (artifact.python_only, artifact.tradingview_only) == (0, 0)
    assert artifact.match_rate == pytest.approx(2 / 3)
    assert not artifact.passed


# --- expected safe deviations (user decision of 2026-11-19) ------------------------------


def one_mismatch() -> ParityResult:
    python = series(3)
    tv = [
        tv_of(python[0]),
        dataclasses.replace(tv_of(python[1]), exit_price=python[1].exit_price + 5.0),
        tv_of(python[2]),
    ]
    return compare(python, tv, clip_to_overlap=False)


def test_the_expected_deviation_table_names_the_mtf_runner_stop_and_nothing_else() -> None:
    assert set(EXPECTED_DEVIATIONS) == {"mtf_trend_cont"}
    assert set(EXPECTED_DEVIATIONS) <= set(STRATEGIES)
    first, second = EXPECTED_DEVIATIONS["mtf_trend_cont"]
    # What differs and why it is wanted: the Python side never leaves a position
    # without a stop (CLAUDE.md invariant 2), the Pine script does while the trail warms up.
    assert "İz göstergesi ısınırken" in first
    assert "başa başta tutar" in first
    assert "stopsuz bırakır" in first
    assert "değişmez kural 2" in first
    assert "dolumlar farklı olabilir" in first
    # What the note does NOT do: it tolerates nothing; the way out is --warmup-bars.
    assert "uyuşmazlık olarak SAYILIR" in second
    assert "sessizce hoş görülmez" in second
    assert "--warmup-bars" in second


def test_the_mtf_report_and_artifact_carry_the_expected_safe_deviation() -> None:
    result = one_mismatch()

    text = render_parity_report(result, strategy_id="mtf_trend_cont")
    artifact = parity_artifact(result, label="mtf", created=CREATED, strategy_id="mtf_trend_cont")

    assert f"{EXPECTED_DEVIATION_HEADING} (mtf_trend_cont):" in text
    assert EXPECTED_DEVIATION_HEADING == "Beklenen güvenli sapma"
    notes = [note for note in artifact.notes if note.startswith(EXPECTED_DEVIATION_HEADING)]
    for sentence in EXPECTED_DEVIATIONS["mtf_trend_cont"]:
        # One table feeds both: the terminal report and the stored report cannot drift.
        assert f"- {sentence}" in text
        assert f"{EXPECTED_DEVIATION_HEADING}: {sentence}" in notes
    assert len(notes) == 2
    # Before the generic reminders, after the differences.
    assert text.index(EXPECTED_DEVIATION_HEADING) < text.index("Hatırlatmalar:")


def test_the_note_explains_and_tolerates_nothing() -> None:
    result = one_mismatch()

    with_note = parity_artifact(result, label="m", created=CREATED, strategy_id="mtf_trend_cont")
    without = parity_artifact(result, label="o", created=CREATED, strategy_id="ny_orb_fvg")

    # The same comparison under either id: the mismatch is counted, the verdict is the same.
    assert (result.passed, len(result.mismatches)) == (False, 1)
    for artifact in (with_note, without):
        assert (artifact.matched, artifact.mismatched, artifact.passed) == (2, 1, False)
        assert artifact.match_rate == pytest.approx(2 / 3)
    plain = render_parity_report(result)
    noted = render_parity_report(result, strategy_id="mtf_trend_cont")
    assert plain.splitlines()[0] == noted.splitlines()[0]
    assert "Uyuşmayan: 1" in noted


@pytest.mark.parametrize("strategy_id", sorted(set(STRATEGIES) - {"mtf_trend_cont"}))
def test_no_other_strategy_gets_an_expected_deviation_note(strategy_id: str) -> None:
    result = one_mismatch()

    text = render_parity_report(result, strategy_id=strategy_id)
    artifact = parity_artifact(result, label="x", created=CREATED, strategy_id=strategy_id)

    assert EXPECTED_DEVIATION_HEADING not in text
    assert text == render_parity_report(result)
    assert not any(EXPECTED_DEVIATION_HEADING in note for note in artifact.notes)


# --- golden files ------------------------------------------------------------------------


def test_golden_round_trip_is_exact(tmp_path: Path) -> None:
    rows = [
        record(entry_price=4500.1, exit_price=4512.300000001, comment='a, "quoted" note'),
        record(seq=1, side=Side.SHORT, exit_id="", comment="EOD close", reason=ExitReason.CLOSE),
    ]
    path = tmp_path / "golden" / "ny.csv"
    write_golden(rows, path, header_note="ny_orb_fvg on synthetic bars, seed 7")
    assert path.read_text(encoding="utf-8").startswith(GOLDEN_MARKER + " ny_orb_fvg")
    loaded = read_golden(path)
    assert loaded == golden_rows(rows)
    assert diff_golden(loaded, rows, tick_size=TICK) == []


def test_golden_diff_names_the_differences(tmp_path: Path) -> None:
    rows = series(2)
    path = tmp_path / "g.csv"
    write_golden(rows, path, header_note="pin")
    changed = [
        dataclasses.replace(rows[0], exit_price=rows[0].exit_price + 2 * TICK, exit_id="Stop")
    ]
    diffs = diff_golden(read_golden(path), changed, tick_size=TICK)
    assert "satır sayısı farklı: beklenen 2, gelen 1" in diffs
    assert any("exit_price" in d and "+2.00 tick" in d for d in diffs)
    assert any("exit_id beklenen 'Exit L', gelen 'Stop'" in d for d in diffs)
    assert any(d.startswith("satır 2: beklenen işlem yok") for d in diffs)


def test_golden_reader_refuses_foreign_files(tmp_path: Path) -> None:
    with pytest.raises(ParityError, match="işareti"):
        read_golden(write(tmp_path, "a.csv", "seq,strategy_id\n"))
    with pytest.raises(ParityError, match="başlık"):
        read_golden(write(tmp_path, "b.csv", f"{GOLDEN_MARKER} x\nseq\n"))
    with pytest.raises(ParityError, match="not"):
        write_golden([], tmp_path / "c.csv", header_note="  ")
    path = tmp_path / "d.csv"
    write_golden(series(1), path, header_note="pin")
    text = path.read_text(encoding="utf-8").replace("6812.250000", "6812.25")
    path.write_text(text, encoding="utf-8")
    with pytest.raises(ParityError, match="satır 3: entry_price"):
        read_golden(path)


def test_tv_trade_list_from_fills_is_not_a_sample() -> None:
    assert not TvTradeList.from_fills([]).sample
