"""Adversarial tests of the parity module (SPEC §6.6): no false PASS, no silently lost row.

Each test is an attack that must fail loudly (a ParityError naming the line) or be counted
against the verdict. The legitimate path is checked at the end (99/100 passes, 97/100 not).
"""

import dataclasses
import datetime as dt
import math
from pathlib import Path

import pytest
from hypothesis import given, settings
from hypothesis import strategies as st

from propbot.backtest.artifacts import MAX_PARITY_MISMATCHES
from propbot.backtest.model import ExitReason, FillModel, TradeRecord
from propbot.backtest.parity import (
    MAX_EXCLUDED_SHARE,
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
    write_golden,
)
from propbot.strategies.base import Side

UTC = "UTC"
NY = "America/New_York"
TICK = 0.25
MINUTE = 60_000
DAY = 86_400_000
T0 = 1_772_462_160_000  # 2026-03-02 14:36 UTC
CREATED = dt.datetime(2026, 11, 18, 12, 0, tzinfo=dt.UTC)
HEADER = "Trade #,Type,Signal,Date/Time,Price\n"
SEMI_HEADER = "Trade #;Type;Signal;Date/Time;Price\n"


def record(
    *,
    seq: int = 0,
    side: Side = Side.LONG,
    entry_ts_ms: int = T0,
    exit_ts_ms: int = T0 + 29 * MINUTE,
    entry_price: float = 4500.25,
    exit_price: float = 4510.75,
    exit_id: str = "Exit L",
    comment: str = "",
    reason: ExitReason = ExitReason.TARGET,
) -> TradeRecord:
    return TradeRecord(
        seq=seq,
        strategy_id="s",
        entry_id="Long",
        exit_id=exit_id,
        reason=reason,
        setup="Long",
        comment=comment,
        side=side,
        qty=1.0,
        entry_bar_index=0,
        exit_bar_index=29,
        entry_ts_ms=entry_ts_ms,
        exit_ts_ms=exit_ts_ms,
        entry_price=entry_price,
        exit_price=exit_price,
        gross_pnl=1.0,
        commission=0.0,
        pnl=1.0,
        initial_stop=entry_price - 5.0 * side.sign,
        risk_amount=5.0,
        r=0.2,
        mae_r=0.0,
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


def series(n: int) -> list[TradeRecord]:
    return [
        record(seq=i, entry_ts_ms=T0 + i * DAY, exit_ts_ms=T0 + i * DAY + 29 * MINUTE)
        for i in range(n)
    ]


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


def write(tmp_path: Path, text: str, name: str = "x.csv") -> Path:
    path = tmp_path / name
    path.write_bytes(text.encode("utf-8"))
    return path


def load(tmp_path: Path, text: str, tz: str = UTC) -> TvTradeList:
    return load_tradingview_trades(write(tmp_path, text), tz=tz)


def refused(tmp_path: Path, text: str, tz: str = UTC) -> str:
    with pytest.raises(ParityError) as caught:
        load(tmp_path, text, tz)
    return str(caught.value)


GOOD_TRADE = "1,Entry Long,L,2026-03-02 09:36,4500.25\n1,Exit Long,X,2026-03-02 09:40,4501\n"


# --- rows must never vanish -------------------------------------------------------------


def test_blank_lines_are_skipped_with_their_line_numbers_not_silently(tmp_path: Path) -> None:
    tv = load(tmp_path, HEADER + "\n" + GOOD_TRADE + "\n,,,,\n")
    assert len(tv.fills) == 1
    assert [(s.line, s.reason) for s in tv.skipped] == [
        (2, "boş satır"),
        (5, "boş satır"),
        (6, "boş satır"),
    ]


@pytest.mark.parametrize(
    ("tail", "expected"),
    [
        ("Total,,,,\n", "satır 4: 'Trade #'"),  # summary row with the full cell count
        ("Net profit: 12.5\n", "satır 4: 1 alan var"),  # footer with fewer cells
        ("1,Exit Long,X\n", "satır 4: 3 alan var"),  # truncated row
        (HEADER, "satır 4: 'Trade #'"),  # repeated header (two exports pasted together)
    ],
)
def test_footer_summary_header_and_short_rows_are_refused(
    tmp_path: Path, tail: str, expected: str
) -> None:
    assert expected in refused(tmp_path, HEADER + GOOD_TRADE + tail)


def test_non_numeric_price_unknown_type_and_orphan_exit_are_refused(tmp_path: Path) -> None:
    message = refused(
        tmp_path,
        HEADER
        + GOOD_TRADE
        + "2,Entry Long,L,2026-03-03 09:36,n/a\n"
        + "2,Exit Long,X,2026-03-03 09:40,4501\n"
        + "3,Entry Buy,L,2026-03-04 09:36,4500\n"
        + "4,Exit Long,X,2026-03-05 09:40,4501\n",
    )
    assert "satır 4: fiyat sayı olmalı" in message
    assert "satır 6: 'Type'" in message
    assert "işlem #4 (satır 7): giriş satırı yok" in message


def test_a_trade_number_with_two_entry_rows_is_refused_not_merged(tmp_path: Path) -> None:
    message = refused(tmp_path, HEADER + GOOD_TRADE + GOOD_TRADE)
    assert "birden fazla giriş satırı" in message


def test_crlf_export_reads_like_lf(tmp_path: Path) -> None:
    lf = load(tmp_path, HEADER + GOOD_TRADE)
    crlf = load_tradingview_trades(
        write(tmp_path, (HEADER + GOOD_TRADE).replace("\n", "\r\n"), "crlf.csv"), tz=UTC
    )
    assert [dataclasses.replace(f, line=0) for f in crlf.fills] == [
        dataclasses.replace(f, line=0) for f in lf.fills
    ]


# --- numbers -----------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("header", "cell", "value"),
    [
        (HEADER, '"1,234.50"', 1234.5),
        (SEMI_HEADER, "1.234,50", 1234.5),
        (SEMI_HEADER, "5 000,25", 5000.25),
        (SEMI_HEADER, "5 000,25", 5000.25),
        (SEMI_HEADER, "5 000,25", 5000.25),
        (HEADER, "4500", 4500.0),
    ],
)
def test_grouped_numbers_parse_to_their_true_value(
    tmp_path: Path, header: str, cell: str, value: float
) -> None:
    sep = header[7]
    text = (
        header
        + sep.join(("1", "Entry Long", "L", "2026-03-02 09:36", cell))
        + "\n"
        + sep.join(("1", "Exit Long", "X", "2026-03-02 09:40", cell))
        + "\n"
    )
    fill = load(tmp_path, text).fills[0]
    assert fill.entry_price == value
    assert fill.exit_price == value


@pytest.mark.parametrize(
    ("header", "cell", "expected"),
    [
        (HEADER, "-12.5", "fiyat pozitif olmalı"),
        (HEADER, "−12.5", "fiyat pozitif olmalı"),  # Unicode minus is still a minus
        (HEADER, "1e3", "fiyat sayı olmalı"),
        (HEADER, "$4500", "fiyat sayı olmalı"),
        (HEADER, "4500 USD", "fiyat sayı olmalı"),
        (HEADER, "4500.25€", "fiyat sayı olmalı"),
        (HEADER, "inf", "fiyat sayı olmalı"),
        (SEMI_HEADER, "45 00,25", "fiyat sayı olmalı"),  # spaces only group thousands
        (SEMI_HEADER, "4 50 0,25", "fiyat sayı olmalı"),
        (SEMI_HEADER, "4500 ,25", "fiyat sayı olmalı"),
        (SEMI_HEADER, "12.34,5", "fiyat sayı olmalı"),
    ],
)
def test_numbers_that_could_be_misread_are_refused(
    tmp_path: Path, header: str, cell: str, expected: str
) -> None:
    sep = header[7]
    text = (
        header
        + sep.join(("1", "Entry Long", "L", "2026-03-02 09:36", "4500,5" if sep == ";" else "1"))
        + "\n"
        + sep.join(("1", "Exit Long", "X", "2026-03-02 09:40", cell))
        + "\n"
    )
    assert f"satır 3: {expected}" in refused(tmp_path, text)


def test_european_decimal_in_a_comma_file_is_refused_not_read_as_thousands(
    tmp_path: Path,
) -> None:
    text = HEADER + '1,Entry Long,L,2026-03-02 09:36,"1.234,50"\n1,Exit Long,X,2026-03-02 09:40,2\n'
    assert "ondalık virgül yalnızca" in refused(tmp_path, text)


# --- times -------------------------------------------------------------------------------


@pytest.mark.parametrize(
    "moment",
    [
        "02/03/2026 09:36",  # DD/MM or MM/DD: must not be guessed
        "03/02/2026 09:36",
        "2026/03/02 09:36",
        "2026-03-02 9:36 AM",  # 12-hour clock
        "2026-03-02 09:36 PM",
        "2026-03-02",
        "2026-02-30 09:36",
        "2026-03-02 24:00",
    ],
)
def test_ambiguous_or_foreign_date_formats_are_refused(tmp_path: Path, moment: str) -> None:
    text = HEADER + f"1,Entry Long,L,{moment},1\n1,Exit Long,X,2026-03-03 09:40,2\n"
    message = refused(tmp_path, text)
    assert "satır 2:" in message


@pytest.mark.parametrize(
    ("tz", "moment"),
    [
        (NY, "2026-03-08 02:30"),  # spring gap
        (NY, "2026-11-01 01:30"),  # autumn fold
        ("Europe/Istanbul", None),  # no DST since 2016: nothing to refuse
        ("Europe/London", "2026-03-29 01:30"),
        ("Europe/London", "2026-10-25 01:30"),
    ],
)
def test_dst_gap_and_fold_wall_times_are_refused_in_the_chart_zone(
    tmp_path: Path, tz: str, moment: str | None
) -> None:
    if moment is None:
        tv = load(tmp_path, HEADER + GOOD_TRADE, tz)
        assert len(tv.fills) == 1
        return
    text = HEADER + f"1,Entry Long,L,{moment},1\n1,Exit Long,X,2026-12-01 09:40,2\n"
    assert "yaz saati" in refused(tmp_path, text, tz)


# --- matching ----------------------------------------------------------------------------


def test_repeated_python_rows_are_refused_instead_of_inflating_matches() -> None:
    row = record()
    with pytest.raises(ParityError, match="iki kez"):
        compare([row, row], [tv_of(row), tv_of(row, trade_no=2)])


def test_a_repeated_tradingview_fill_is_one_sided_not_matched_twice() -> None:
    row = record()
    result = compare([row], [tv_of(row), tv_of(row, trade_no=2)])
    assert len(result.matched) == 1
    assert len(result.tradingview_only) == 1
    assert result.match_rate == 0.5
    assert not result.passed


def test_one_python_row_is_never_paired_with_two_tradingview_rows() -> None:
    row = record()
    near = dataclasses.replace(tv_of(row, trade_no=2), exit_price=row.exit_price + 4 * TICK)
    result = compare([row], [tv_of(row), near])
    used = [p.python for p in result.matched] + [m.python for m in result.mismatches]
    assert used == [row]
    assert result.tradingview_only == (near,)


@settings(max_examples=150, deadline=None)
@given(
    st.lists(st.integers(0, 30), max_size=8, unique=True),
    st.lists(st.integers(0, 30), max_size=8, unique=True),
    st.integers(0, 30),
    st.integers(0, 4),
    st.booleans(),
)
def test_adding_an_unmatched_row_never_raises_the_rate(
    py_bars: list[int], tv_bars: list[int], extra_bar: int, extra_off: int, to_tv: bool
) -> None:
    def at(bar: int, seq: int, ticks: int = 0) -> TradeRecord:
        start = T0 + bar * MINUTE
        return record(
            seq=seq,
            entry_ts_ms=start,
            exit_ts_ms=start + 5 * MINUTE,
            exit_price=4510.75 + ticks * TICK,
        )

    python = [at(bar, i) for i, bar in enumerate(py_bars)]
    tv = [tv_of(at(bar, 0), trade_no=i) for i, bar in enumerate(tv_bars)]
    before = compare(python, tv, clip_to_overlap=False)
    extra = at(extra_bar, 99, extra_off)
    if to_tv:
        added = tv_of(extra, trade_no=99)
        after = compare(python, [*tv, added], clip_to_overlap=False)
        if any(p.tradingview is added for p in after.matched):
            return
    else:
        after = compare([*python, extra], tv, clip_to_overlap=False)
        if any(p.python is extra for p in after.matched):
            return
    assert (after.match_rate or 0.0) <= (before.match_rate or 0.0)
    assert after.compared >= before.compared


def test_a_short_export_cannot_pass_by_clipping_the_python_list() -> None:
    python = series(100)
    tv = [tv_of(row, trade_no=i) for i, row in enumerate(python[:10])]
    result = compare(python, tv)
    assert result.match_rate == 1.0  # the clipped rate looks perfect ...
    assert result.exclusions.python_outside_window == 90
    assert not result.passed  # ... but 90 hidden rows cannot pass
    assert "Python listesinin 90/100" in result.blocked_by[0]
    report = render_parity_report(result)
    assert "GEÇEMEZ: Python listesinin 90/100" in report


def test_a_tradingview_list_that_stops_trading_early_cannot_pass() -> None:
    # TradingView stopped producing trades half-way: a real divergence, not an edge effect.
    python = series(60)
    tv = [tv_of(row, trade_no=i) for i, row in enumerate(python[:30])]
    result = compare(python, tv)
    assert not result.passed
    assert result.blocked_by


def test_a_late_warmup_cutoff_cannot_hide_mismatches() -> None:
    python = series(50)
    tv = [
        dataclasses.replace(tv_of(row, trade_no=i), side=Side.SHORT) if i < 5 else tv_of(row)
        for i, row in enumerate(python)
    ]
    assert not compare(python, tv).passed
    result = compare(python, tv, warmup_cutoff_ms=python[5].entry_ts_ms)
    assert result.match_rate == 1.0
    assert (result.exclusions.python_warmup, result.exclusions.tradingview_warmup) == (5, 5)
    assert not result.passed
    assert len(result.blocked_by) == 2


def test_exclusions_within_the_named_share_still_allow_a_pass() -> None:
    assert math.isclose(MAX_EXCLUDED_SHARE, 0.02)
    python = series(100)
    tv = [tv_of(row, trade_no=i) for i, row in enumerate(python[2:])]
    result = compare(python, tv)  # 2 of 100 Python rows before the export starts
    assert result.exclusions.python_outside_window == 2
    assert result.blocked_by == ()
    assert result.passed
    tv = [tv_of(row, trade_no=i) for i, row in enumerate(python[3:])]
    assert not compare(python, tv).passed  # 3 %: beyond the bound


@pytest.mark.parametrize(
    ("tick", "base"),
    [(0.25, 4500.25), (0.25, 45123.75), (0.01, 4500.01), (0.01, 45000.07), (0.1, 4500.1)],
)
@pytest.mark.parametrize("ticks", [-3, -2, -1, 0, 1, 2, 3])
def test_price_tolerance_at_tick_boundaries(tick: float, base: float, ticks: int) -> None:
    # Prices are built the way an export prints them (rounded decimals), not base + k*tick,
    # so binary float noise of either construction is exercised.
    decimals = {0.25: 2, 0.01: 2, 0.1: 1}[tick]
    row = record(entry_price=base, exit_price=round(base + 40 * tick, decimals))
    moved = round(row.exit_price + ticks * tick, decimals)
    tv = dataclasses.replace(tv_of(row), exit_price=moved)
    result = compare([row], [tv], tick_size=tick)
    assert bool(result.matched) is (abs(ticks) <= 1)
    exact = compare([row], [tv], tick_size=tick, tolerance_ticks=0.0)
    assert bool(exact.matched) is (ticks == 0)


def test_half_a_tick_beyond_the_tolerance_is_a_mismatch_for_every_tick_size() -> None:
    for tick in (0.25, 0.01, 0.1):
        row = record(entry_price=4500.0, exit_price=4510.0)
        tv = dataclasses.replace(tv_of(row), entry_price=4500.0 + 1.5 * tick)
        assert compare([row], [tv], tick_size=tick).mismatches[0].fields == (Field.ENTRY_PRICE,)


def test_tolerances_looser_than_the_spec_are_refused() -> None:
    row = record()
    with pytest.raises(ParityError, match=r"SPEC §6\.6 sınırı"):
        compare([row], [tv_of(row)], tolerance_ticks=1.0001)
    with pytest.raises(ParityError, match="giriş zamanı toleransı"):
        compare([row], [tv_of(row)], time_tolerance_bars=2, pair_window_bars=5)
    with pytest.raises(ParityError):
        compare([row], [tv_of(row)], tolerance_ticks=math.nan)


@pytest.mark.parametrize(
    ("comment", "exit_id", "signal"),
    [
        ("", "", ""),
        ("  ", "", "   "),
        ("", "", "Exit L"),
        ("", "Exit L", ""),
        ("EOD close", "", " "),
        ("EOD close", "Exit L", "EOD"),
        ("Init stop", "", "Init stop2"),
    ],
)
def test_empty_or_different_exit_reasons_never_count_as_agreement(
    comment: str, exit_id: str, signal: str
) -> None:
    row = record(comment=comment, exit_id=exit_id)
    result = compare([row], [tv_of(row, signal=signal)])
    assert not result.matched
    assert result.mismatches[0].fields == (Field.EXIT_REASON,)


@pytest.mark.parametrize("model", [m for m in FillModel if m is not FillModel.TV_OHLC])
def test_every_fill_model_but_tv_ohlc_is_refused(model: FillModel) -> None:
    row = record()
    with pytest.raises(ParityError, match="tv_ohlc"):
        compare([row], [tv_of(row)], fill_model=model)


@pytest.mark.parametrize("threshold", [0.9799999, 0.0, -1.0, 1.0000001, 2.0, math.nan, math.inf])
def test_thresholds_outside_floor_to_one_are_refused(threshold: float) -> None:
    with pytest.raises(ParityError):
        compare([], [], threshold=threshold)


def test_threshold_exactly_098_and_exactly_one() -> None:
    python = series(100)
    tv = [tv_of(row, trade_no=i) for i, row in enumerate(python)]
    tv[10] = dataclasses.replace(tv[10], side=Side.SHORT)
    tv[20] = dataclasses.replace(tv[20], exit_signal="other")
    result = compare(python, tv, threshold=0.98)
    assert result.match_rate == 0.98
    assert result.passed
    assert not compare(python, tv, threshold=1.0).passed
    assert compare(python, [tv_of(row) for row in python], threshold=1.0).passed


# --- legitimate path ---------------------------------------------------------------------


def test_99_of_100_agreeing_fills_pass_and_97_fail() -> None:
    python = series(100)
    tv = [tv_of(row, trade_no=i) for i, row in enumerate(python)]
    one_off = list(tv)
    one_off[42] = dataclasses.replace(tv[42], exit_price=tv[42].exit_price + 2 * TICK)
    result = compare(python, one_off)
    assert (len(result.matched), len(result.mismatches), result.compared) == (99, 1, 100)
    assert result.match_rate == 0.99
    assert result.passed
    three_off = list(tv)
    for i in (5, 50, 95):
        three_off[i] = dataclasses.replace(tv[i], entry_ts_ms=tv[i].entry_ts_ms + MINUTE)
    result = compare(python, three_off)
    assert result.match_rate == 0.97
    assert result.field_counts[Field.ENTRY_TIME.value] == 3
    assert not result.passed


# --- artifact ----------------------------------------------------------------------------


def test_artifact_passed_follows_the_result_even_when_the_rate_is_perfect() -> None:
    python = series(100)
    result = compare(python, [tv_of(row) for row in python[:10]])
    assert result.match_rate == 1.0
    assert not result.passed
    artifact = parity_artifact(result, label="x", created=CREATED, strategy_id="s")
    assert artifact.passed is False
    assert artifact.match_rate == 1.0
    assert any(note.startswith("Geçemez: Python listesinin 90/100") for note in artifact.notes)


def test_capped_artifact_list_still_reports_the_true_counts() -> None:
    python = series(MAX_PARITY_MISMATCHES + 50)
    tv = [dataclasses.replace(tv_of(row), side=Side.SHORT) for row in python[:30]]
    result = compare(python, tv, clip_to_overlap=False)
    artifact = parity_artifact(result, label="x", created=CREATED, strategy_id="s")
    assert len(artifact.mismatches) == MAX_PARITY_MISMATCHES
    assert artifact.python_only == MAX_PARITY_MISMATCHES + 20
    assert artifact.matched == 0
    assert artifact.passed is result.passed is False
    assert any(
        f"uyuşmayan 30, yalnızca Python {MAX_PARITY_MISMATCHES + 20}" in note
        for note in artifact.notes
    )
    assert any(f"toplam {MAX_PARITY_MISMATCHES + 50})" in note for note in artifact.notes)


# --- golden files ------------------------------------------------------------------------


def golden(tmp_path: Path, rows: list[TradeRecord]) -> Path:
    path = tmp_path / "golden.csv"
    write_golden(rows, path, header_note="pin")
    return path


def test_golden_prices_that_do_not_round_trip_at_fixed_decimals_compare_equal(
    tmp_path: Path,
) -> None:
    rows = [record(entry_price=4500.1234567, exit_price=0.1 + 0.2), record(seq=1)]
    path = golden(tmp_path, rows)
    assert read_golden(path) == golden_rows(rows)
    assert diff_golden(read_golden(path), rows, tick_size=TICK) == []


def test_reordered_golden_file_is_a_difference(tmp_path: Path) -> None:
    rows = series(3)
    path = golden(tmp_path, rows)
    marker, header, *body = path.read_text(encoding="utf-8").splitlines(keepends=True)
    path.write_text(marker + header + "".join(reversed(body)), encoding="utf-8")
    assert diff_golden(read_golden(path), rows, tick_size=TICK)


def test_truncated_golden_file_is_refused_or_a_difference(tmp_path: Path) -> None:
    rows = series(3)
    path = golden(tmp_path, rows)
    text = path.read_text(encoding="utf-8")
    lines = text.splitlines(keepends=True)
    path.write_text("".join(lines[:-1]), encoding="utf-8")  # cut at a row boundary
    assert "satır sayısı farklı: beklenen 2, gelen 3" in diff_golden(
        read_golden(path), rows, tick_size=TICK
    )
    for cut in (len(text) - 3, len(text) - 12, len(text) - 40):
        path.write_text(text[:cut], encoding="utf-8")
        with pytest.raises(ParityError, match="satır 5"):
            read_golden(path)


def test_crlf_golden_file_reads_the_same(tmp_path: Path) -> None:
    rows = series(2)
    path = golden(tmp_path, rows)
    expected = read_golden(path)
    path.write_bytes(path.read_bytes().replace(b"\n", b"\r\n"))
    assert read_golden(path) == expected
