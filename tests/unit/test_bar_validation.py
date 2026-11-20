"""Data quality report: what is blocking, what is only reported, and the wrong-bar_tz alarm."""

import dataclasses
import datetime as dt
import json
from pathlib import Path
from zoneinfo import ZoneInfo

import pytest

from propbot.core.bars import Bar, BarSeries
from propbot.data.ohlcv import DataError, load_ohlcv, read_ohlcv
from propbot.data.synthetic import generate_bars, write_ohlcv_csv
from propbot.data.validate import (
    OUTLIER_GAP,
    OUTLIER_RANGE,
    REGION_EU,
    REGION_US,
    render_validation_report,
    validate_bars,
)

NY = ZoneInfo("America/New_York")
MT5_HEADER = "<DATE>\t<TIME>\t<OPEN>\t<HIGH>\t<LOW>\t<CLOSE>\t<TICKVOL>\t<VOL>\t<SPREAD>"
# Mon 2026-10-19 .. Fri 2026-11-06: the EU (10-25) and US (11-01) DST ends inside.
AUTUMN = {"start": dt.date(2026, 10, 19), "sessions": 15}


def series(sessions: int = 5, start: dt.date = dt.date(2026, 3, 2), tf: int = 300) -> BarSeries:
    return generate_bars(tf_seconds=tf, start=start, sessions=sessions, seed=5)


def with_bars(base: BarSeries, bars: list[Bar]) -> BarSeries:
    return dataclasses.replace(base, bars=tuple(bars))


def csv_lines(base: BarSeries, tmp_path: Path) -> list[str]:
    path = tmp_path / "base.csv"
    write_ohlcv_csv(base, path)
    return path.read_text(encoding="utf-8").splitlines()


def write_lines(tmp_path: Path, lines: list[str], name: str = "edited.csv") -> Path:
    path = tmp_path / name
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    return path


def mt5_server_file(base: BarSeries, tmp_path: Path) -> Path:
    """The series as an MT5 export whose clock is New York + 7 hours."""
    lines = [MT5_HEADER]
    for bar in base.bars:
        server = dt.datetime.fromtimestamp(bar.ts_open_ms // 1000, NY).replace(
            tzinfo=None
        ) + dt.timedelta(hours=7)
        lines.append(
            f"{server:%Y.%m.%d}\t{server:%H:%M:%S}\t{bar.open:.2f}\t{bar.high:.2f}\t"
            f"{bar.low:.2f}\t{bar.close:.2f}\t{bar.volume:.0f}\t0\t50"
        )
    return write_lines(tmp_path, lines, "US500.cash_M5.csv")


def test_clean_series_report_has_source_span_and_sessions() -> None:
    base = series()
    report = validate_bars(base)
    assert report.clean
    assert not report.blocking
    assert report.tf_seconds == 300
    assert report.bar_count == report.rows == len(base)
    assert report.session_count == 5
    assert report.typical_session_bars == 276
    assert report.first_ts_ms == base.first_ts_ms
    assert report.last_ts_ms == base.last_ts_ms
    assert report.span_days == 6  # Sunday 2026-03-01 evening to Friday 2026-03-06
    assert report.source.synthetic
    text = render_validation_report(report)
    assert "SENTETİK VERİ" in text
    assert "TEMİZ" in text
    assert "2026-03-01 18:00 America/New_York" in text


def test_missing_bars_inside_a_session_are_an_intraday_gap() -> None:
    base = series()
    bars = list(base.bars)
    gone = bars[100:105]
    del bars[100:105]
    report = validate_bars(with_bars(base, bars))
    assert not report.clean
    assert not report.blocking
    assert report.gaps_total == 1
    assert report.missing_bars_total == 5
    assert report.gaps[0].first_missing_ms == gone[0].ts_open_ms
    assert report.gaps[0].missing_bars == 5
    assert "Gün içi boşluk: 1 (eksik bar toplamı 5)" in render_validation_report(report)


def test_missing_session_is_listed_as_a_holiday_not_an_error() -> None:
    base = series()
    wednesday = dt.date(2026, 3, 4)
    bars = [
        bar
        for bar in base.bars
        if not (
            dt.datetime.fromtimestamp(bar.ts_open_ms // 1000, NY)
            >= dt.datetime(2026, 3, 3, 18, 0, tzinfo=NY)
            and dt.datetime.fromtimestamp(bar.ts_open_ms // 1000, NY)
            < dt.datetime(2026, 3, 4, 18, 0, tzinfo=NY)
        )
    ]
    report = validate_bars(with_bars(base, bars))
    assert report.session_count == 4
    assert report.missing_days == (wednesday,)
    assert report.gaps_total == 0
    assert report.clean
    assert "2026-03-04" in render_validation_report(report)


def test_half_days_are_short_sessions_not_errors() -> None:
    base = generate_bars(
        start=dt.date(2026, 11, 23),
        sessions=10,
        seed=5,
        early_closes={dt.date(2026, 11, 27): dt.time(13, 0), dt.date(2026, 12, 4): dt.time(13, 0)},
    )
    report = validate_bars(base)
    assert report.clean
    assert report.short_sessions_total == 2
    closes = [
        dt.datetime.fromtimestamp(item.last_ts_ms // 1000, NY) for item in report.short_sessions
    ]
    assert [(moment.date(), moment.hour, moment.minute) for moment in closes] == [
        (dt.date(2026, 11, 27), 12, 55),
        (dt.date(2026, 12, 4), 12, 55),
    ]
    assert "yarım gün" in render_validation_report(report)


def test_duplicates_and_out_of_order_rows_are_blocking(tmp_path: Path) -> None:
    lines = csv_lines(series(sessions=1), tmp_path)
    # lines[0] marker, lines[1] header, lines[2] first bar (file line 3).
    lines.insert(12, lines[11])  # duplicate of file line 12, now also at line 13
    lines[20], lines[21] = lines[21], lines[20]  # swap file lines 21 and 22
    path = write_lines(tmp_path, lines)
    report = validate_bars(read_ohlcv(path))
    assert report.blocking
    assert not report.clean
    assert report.duplicates_total == 1
    assert report.duplicates[0].line == 13
    assert report.out_of_order_total == 1
    assert report.out_of_order[0].line == 22
    # Timing checks run on the ascending rest: the out-of-order bar leaves an empty slot.
    assert report.gaps_total == 1
    with pytest.raises(DataError):
        load_ohlcv(path)
    text = render_validation_report(report)
    assert "YÜKLENEMEZ" in text
    assert "satır 13: yinelenen bar zamanı" in text


def test_lists_are_capped_with_a_total(tmp_path: Path) -> None:
    lines = csv_lines(series(sessions=1), tmp_path)
    duplicated = [line for index in range(2, 32) for line in (lines[index], lines[index])]
    path = write_lines(tmp_path, lines[:2] + duplicated)
    report = validate_bars(read_ohlcv(path))
    assert report.duplicates_total == 30
    assert len(report.duplicates) == 20
    assert "... ve 10 tane daha" in render_validation_report(report)


def test_unreadable_impossible_and_overlapping_rows_are_reported(tmp_path: Path) -> None:
    lines = [
        "time,open,high,low,close,volume",
        "2026-03-09T13:30:00Z,10,11,9,10.5,1",
        "2026-03-09T13:35:00Z,10,x,9,10.5,1",
        "2026-03-09T13:40:00Z,10,10.25,9,10.5,1",
        "2026-03-09T13:45:00Z,10,11,9,10.5,0",
        "2026-03-09T13:47:00Z,10,11,9,10.5,0",
    ]
    report = validate_bars(read_ohlcv(write_lines(tmp_path, lines)), tf_seconds=300)
    assert report.rows == 5
    assert report.bar_count == 4
    assert report.unreadable_total == 1
    assert report.unreadable[0].line == 3
    assert report.ohlc_inconsistent_total == 1
    assert report.ohlc_inconsistent[0].line == 4
    assert report.overlaps_total == 1
    assert report.overlaps[0].line == 6
    assert report.blocking_total == 3
    assert report.zero_volume_bars == 2
    assert report.zero_volume_share == pytest.approx(0.5)


def test_outlier_range_and_jump_are_reported() -> None:
    base = series()
    bars = list(base.bars)
    spike = bars[400]
    bars[400] = dataclasses.replace(spike, high=spike.high + 200.0)
    jump_at = 600
    previous, jumped = bars[jump_at - 1], bars[jump_at]
    shift = 150.0
    bars[jump_at] = dataclasses.replace(
        jumped,
        open=previous.close + shift,
        high=max(jumped.high, previous.close + shift),
    )
    report = validate_bars(with_bars(base, bars))
    assert not report.clean
    kinds = {(item.kind, item.ts_ms) for item in report.outliers}
    assert (OUTLIER_RANGE, spike.ts_open_ms) in kinds
    assert (OUTLIER_GAP, jumped.ts_open_ms) in kinds
    for item in report.outliers:
        assert item.multiple > 15.0
    assert "Aykırı sıçrama" in render_validation_report(report)


def test_dst_changes_inside_the_span_keep_the_session_open_time() -> None:
    report = validate_bars(generate_bars(seed=5, **AUTUMN))  # type: ignore[arg-type]
    assert [check.region for check in report.dst] == [REGION_EU, REGION_US]
    assert all(check.checked and not check.shifted for check in report.dst)
    assert all(check.before_minute == 18 * 60 for check in report.dst)
    assert report.clean
    eu, us = report.dst
    assert eu.change_ms == int(dt.datetime(2026, 10, 25, 1, tzinfo=dt.UTC).timestamp()) * 1000
    assert us.change_ms == int(dt.datetime(2026, 11, 1, 6, tzinfo=dt.UTC).timestamp()) * 1000


def test_mt5_file_read_with_the_right_bar_tz_is_clean(tmp_path: Path) -> None:
    base = generate_bars(seed=5, **AUTUMN)  # type: ignore[arg-type]
    loaded = load_ohlcv(mt5_server_file(base, tmp_path), bar_tz="NY+7")
    assert loaded.bars == base.bars
    report = validate_bars(loaded)
    assert report.clean
    assert not report.timezone_shift


def test_mt5_file_read_as_utc_shows_a_shift_at_the_us_change(tmp_path: Path) -> None:
    base = generate_bars(seed=5, **AUTUMN)  # type: ignore[arg-type]
    report = validate_bars(load_ohlcv(mt5_server_file(base, tmp_path), bar_tz="Etc/UTC"))
    assert report.timezone_shift
    assert not report.clean
    shifted = {check.region: check for check in report.dst}
    assert not shifted[REGION_EU].shifted
    assert shifted[REGION_US].shifted
    assert (shifted[REGION_US].before_minute, shifted[REGION_US].after_minute) == (
        21 * 60,
        20 * 60,
    )
    text = render_validation_report(report)
    assert text.startswith("!!! SAAT DİLİMİ KAYMASI")
    assert "bar_tz=Etc/UTC" in text


def test_mt5_file_read_in_an_eu_zone_shows_a_shift_at_the_eu_change(tmp_path: Path) -> None:
    base = generate_bars(seed=5, **AUTUMN)  # type: ignore[arg-type]
    report = validate_bars(load_ohlcv(mt5_server_file(base, tmp_path), bar_tz="Europe/Athens"))
    shifted = {check.region: check.shifted for check in report.dst}
    assert shifted == {REGION_EU: True, REGION_US: True}


def test_report_is_built_for_a_file_the_loader_refuses(tmp_path: Path) -> None:
    lines = ["time,open,high,low,close", "garbage,1,2,3,4", "2026-03-09T13:30:00Z,1,2,0.5,1.5"]
    path = write_lines(tmp_path, lines)
    with pytest.raises(DataError):
        load_ohlcv(path)
    report = validate_bars(read_ohlcv(path))
    assert report.blocking
    assert report.tf_seconds is None
    assert report.bar_count == 1
    assert "belirlenemedi" in render_validation_report(report)


def test_empty_raw_file_report() -> None:
    raw = read_ohlcv(Path(__file__).parents[1] / "fixtures" / "ohlcv" / "tv_export.csv")
    empty = dataclasses.replace(raw, bars=(), lines=())
    report = validate_bars(empty)
    assert report.blocking
    assert report.session_count == 0
    assert report.span_days == 0
    assert report.dst == ()
    render_validation_report(report)


def test_to_dict_is_json_safe() -> None:
    base = series()
    bars = list(base.bars)
    del bars[50:52]
    report = validate_bars(with_bars(base, bars))
    data = report.to_dict()
    restored = json.loads(json.dumps(data))
    assert restored == data
    assert data["clean"] is False
    assert data["source"] == {
        "name": base.source.name,
        "format": "synthetic",
        "sha256": "",
        "synthetic": True,
        "bar_tz": "UTC",
    }
    assert data["first_bar_local"] == "2026-03-01 18:00 America/New_York"
    gaps = data["gaps"]
    assert isinstance(gaps, dict)
    assert gaps["missing_bars_total"] == 2


def test_bad_thresholds_are_refused() -> None:
    with pytest.raises(ValueError, match="positive"):
        validate_bars(series(sessions=1), range_multiple=0.0)
