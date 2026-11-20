"""OHLCV file loader: formats, time forms, bar_tz (NY+7, DST) and every refusal path."""

import datetime as dt
from pathlib import Path
from zoneinfo import ZoneInfo

import pytest

from propbot.core.bars import SYNTHETIC_MARKER
from propbot.data import ohlcv
from propbot.data.ohlcv import (
    FORMAT_GENERIC,
    FORMAT_MT5,
    DataError,
    load_ohlcv,
    read_ohlcv,
)
from support.paths import FIXTURES

OHLCV = FIXTURES / "ohlcv"
NY = ZoneInfo("America/New_York")
HEADER = "time,open,high,low,close,volume"
MT5_HEADER = "<DATE>\t<TIME>\t<OPEN>\t<HIGH>\t<LOW>\t<CLOSE>\t<TICKVOL>\t<VOL>\t<SPREAD>"


def utc_ms(year: int, month: int, day: int, hour: int = 0, minute: int = 0) -> int:
    return int(dt.datetime(year, month, day, hour, minute, tzinfo=dt.UTC).timestamp()) * 1000


def write(tmp_path: Path, text: str, name: str = "MES_5.csv", encoding: str = "utf-8") -> Path:
    path = tmp_path / name
    path.write_bytes(text.encode(encoding))
    return path


def rows(*times: str, price: str = "10,11,9,10.5,1") -> str:
    return "\n".join([HEADER, *(f"{time},{price}" for time in times)]) + "\n"


def mt5_rows(*wall: str) -> str:
    return "\n".join([MT5_HEADER, *(f"{w}\t10\t11\t9\t10.5\t7\t0\t2" for w in wall)]) + "\n"


def ny(ts_ms: int) -> tuple[int, int]:
    moment = dt.datetime.fromtimestamp(ts_ms // 1000, NY)
    return moment.hour, moment.minute


# --- formats -------------------------------------------------------------------------


def test_tradingview_export_with_extra_columns_loads() -> None:
    series = load_ohlcv(OHLCV / "tv_export.csv")
    assert series.symbol == "tv_export"
    assert series.tf_seconds == 300
    assert series.source.format == FORMAT_GENERIC
    assert not series.source.synthetic
    first = series.bars[0]
    assert first.ts_open_ms == utc_ms(2026, 3, 9, 13, 30)
    assert ny(first.ts_open_ms) == (9, 30)
    assert (first.open, first.high, first.low, first.close, first.volume) == (
        6702.25,
        6705.50,
        6700.75,
        6704.00,
        18234.0,
    )
    assert len(series.source.sha256) == 64


def test_mt5_export_in_ny_plus_7_server_time_loads() -> None:
    series = load_ohlcv(OHLCV / "mt5_export.csv", bar_tz="NY+7", symbol="US500.cash")
    assert series.symbol == "US500.cash"
    assert series.source.format == FORMAT_MT5
    assert series.source.bar_tz == "NY+7"
    times = [bar.ts_open_ms for bar in series.bars]
    # 16:30 server is 09:30 New York on both sides of the US DST end (2026-11-01).
    assert times[0] == utc_ms(2026, 10, 30, 13, 30)
    assert times[3] == utc_ms(2026, 11, 2, 14, 30)
    assert all(ny(ts)[0] == 9 for ts in times)
    # <VOL> is 0 on a CFD: the tick volume stands in; a positive <VOL> wins.
    assert series.bars[0].volume == 412.0
    assert series.bars[3].volume == 1200.0


def test_mt5_export_needs_an_explicit_bar_tz() -> None:
    with pytest.raises(DataError, match="SUNUCU saatidir"):
        load_ohlcv(OHLCV / "mt5_export.csv")
    series = load_ohlcv(OHLCV / "mt5_export.csv", bar_tz="Etc/UTC")
    assert series.bars[0].ts_open_ms == utc_ms(2026, 10, 30, 16, 30)


def test_mt5_columns_in_another_order_still_load(tmp_path: Path) -> None:
    text = (
        "<TIME>\t<VOL>\t<DATE>\t<CLOSE>\t<LOW>\t<HIGH>\t<OPEN>\n"
        "16:30:00\t5\t2026.03.09\t10.5\t9\t11\t10\n"
    )
    raw = read_ohlcv(write(tmp_path, text), bar_tz="NY+7")
    assert raw.source.format == FORMAT_MT5
    assert raw.bars[0].ts_open_ms == utc_ms(2026, 3, 9, 13, 30)
    assert (raw.bars[0].open, raw.bars[0].close, raw.bars[0].volume) == (10.0, 10.5, 5.0)


def test_mt5_date_and_time_must_have_the_export_shape(tmp_path: Path) -> None:
    path = write(tmp_path, MT5_HEADER + "\n2026-03-09\t16:30:00\t10\t11\t9\t10.5\t1\t0\t2\n")
    with pytest.raises(DataError, match=r"YYYY\.MM\.DD"):
        load_ohlcv(path, bar_tz="NY+7")
    path = write(tmp_path, MT5_HEADER + "\n2026.02.30\t16:30:00\t10\t11\t9\t10.5\t1\t0\t2\n")
    with pytest.raises(DataError, match="geçerli bir tarih-saat değil"):
        load_ohlcv(path, bar_tz="NY+7")


def test_mt5_daily_export_without_time_is_refused(tmp_path: Path) -> None:
    path = write(tmp_path, "<DATE>\t<OPEN>\t<HIGH>\t<LOW>\t<CLOSE>\n2026.03.09\t10\t11\t9\t10\n")
    with pytest.raises(DataError, match="<TIME> sütunu yok"):
        read_ohlcv(path, bar_tz="NY+7")


def test_header_is_case_insensitive_and_volume_is_optional(tmp_path: Path) -> None:
    path = write(tmp_path, "Close,TIME,Open,LOW,High\n10.5,2026-03-09T13:30:00Z,10,9,11\n")
    raw = read_ohlcv(path)
    assert raw.bars[0].close == 10.5
    assert raw.bars[0].volume == 0.0


def test_empty_volume_cell_reads_as_zero(tmp_path: Path) -> None:
    raw = read_ohlcv(write(tmp_path, HEADER + "\n2026-03-09T13:30:00Z,10,11,9,10.5,\n"))
    assert raw.bars[0].volume == 0.0


def test_utf8_bom_and_crlf_are_accepted(tmp_path: Path) -> None:
    text = "﻿" + rows("2026-03-09T13:30:00Z", "2026-03-09T13:35:00Z").replace("\n", "\r\n")
    series = load_ohlcv(write(tmp_path, text))
    assert len(series) == 2


# --- time forms ----------------------------------------------------------------------


@pytest.mark.parametrize(
    ("text", "bar_tz"),
    [
        ("2026-03-09T13:30:00Z", "UTC"),
        ("2026-03-09T13:30:00+00:00", "UTC"),
        ("2026-03-09T09:30:00-04:00", "UTC"),
        ("2026-03-09 13:30:00Z", "UTC"),
        ("2026-03-09T13:30:00", "UTC"),
        ("2026-03-09T09:30:00", "America/New_York"),
        ("2026-03-09T16:30:00", "NY+7"),
        # London is still on GMT until the EU change on 2026-03-29; Istanbul is UTC+3.
        ("2026-03-09T13:30:00", "Europe/London"),
        ("2026-03-09T16:30:00", "Europe/Istanbul"),
        ("1773063000", "UTC"),
        ("1773063000000", "UTC"),
        # An offset in the text wins over bar_tz.
        ("2026-03-09T13:30:00Z", "Asia/Tokyo"),
    ],
)
def test_every_time_form_gives_the_same_instant(tmp_path: Path, text: str, bar_tz: str) -> None:
    raw = read_ohlcv(write(tmp_path, rows(text)), bar_tz=bar_tz)
    assert raw.rejected_total == 0
    assert raw.bars[0].ts_open_ms == utc_ms(2026, 3, 9, 13, 30)


@pytest.mark.parametrize(
    ("text", "message"),
    [
        ("09.03.2026 13:30", "ISO 8601"),
        ("2026-03-09T25:30:00Z", "okunamadı"),
        ("1900-01-01T00:00:00Z", "aralığının dışında"),
        ("20260309", "Unix"),
        ("12345678901234567", "okunamadı"),
        ("", "ISO 8601"),
    ],
)
def test_bad_times_are_row_errors(tmp_path: Path, text: str, message: str) -> None:
    raw = read_ohlcv(write(tmp_path, rows(text)))
    assert raw.rejected_total == 1
    assert raw.rejected[0].line == 2
    assert message in raw.rejected[0].message


def test_ny_plus_7_maps_server_1630_to_ny_0930_on_every_day_of_2026(tmp_path: Path) -> None:
    days = [dt.date(2026, 1, 1) + dt.timedelta(days=offset) for offset in range(365)]
    path = write(tmp_path, mt5_rows(*(f"{day:%Y.%m.%d}\t16:30:00" for day in days)))
    raw = read_ohlcv(path, bar_tz="NY+7")
    assert raw.rejected_total == 0
    for day, bar in zip(days, raw.bars, strict=True):
        local = dt.datetime.fromtimestamp(bar.ts_open_ms // 1000, NY)
        assert (local.date(), local.hour, local.minute) == (day, 9, 30)


@pytest.mark.parametrize(
    ("bar_tz", "wall"),
    [
        ("America/New_York", "2026-03-08T02:30:00"),
        ("NY+7", "2026-03-08T09:30:00"),
        ("Europe/Berlin", "2026-03-29T02:30:00"),
    ],
)
def test_wall_time_skipped_by_dst_is_a_row_error(tmp_path: Path, bar_tz: str, wall: str) -> None:
    raw = read_ohlcv(write(tmp_path, rows(wall)), bar_tz=bar_tz)
    assert raw.rejected_total == 1
    assert "atlanan saat" in raw.rejected[0].message
    with pytest.raises(DataError, match=r"satır 2: .*diliminde yok"):
        load_ohlcv(write(tmp_path, rows(wall)), bar_tz=bar_tz, tf="5")


@pytest.mark.parametrize(
    ("bar_tz", "day", "hours", "first_utc"),
    [
        # 01:00-01:59 New York happens twice on 2026-11-01 (EDT, then EST).
        ("America/New_York", "2026-11-01", (0, 1, 1, 2), (2026, 11, 1, 4, 30)),
        ("NY+7", "2026-11-01", (7, 8, 8, 9), (2026, 11, 1, 4, 30)),
        # 02:00-02:59 Berlin happens twice on 2026-10-25 (CEST, then CET).
        ("Europe/Berlin", "2026-10-25", (1, 2, 2, 3), (2026, 10, 24, 23, 30)),
    ],
)
def test_repeated_wall_hour_is_read_so_the_file_stays_ascending(
    tmp_path: Path,
    bar_tz: str,
    day: str,
    hours: tuple[int, int, int, int],
    first_utc: tuple[int, int, int, int, int],
) -> None:
    walls = [f"{day}T{hour:02d}:{minute}:00" for hour in hours for minute in ("00", "30")][1:]
    series = load_ohlcv(write(tmp_path, rows(*walls)), bar_tz=bar_tz)
    start = utc_ms(*first_utc)
    assert [bar.ts_open_ms for bar in series.bars] == [
        start + index * 30 * 60_000 for index in range(len(walls))
    ]


def test_lone_repeated_wall_time_is_the_earlier_instant(tmp_path: Path) -> None:
    raw = read_ohlcv(write(tmp_path, rows("2026-11-01T01:30:00")), bar_tz="America/New_York")
    assert raw.bars[0].ts_open_ms == utc_ms(2026, 11, 1, 5, 30)


@pytest.mark.parametrize("bar_tz", ["Mars/Base", "localtime", "", "NY+8", "../etc/passwd"])
def test_unknown_bar_tz_is_refused(tmp_path: Path, bar_tz: str) -> None:
    with pytest.raises(DataError, match="bilinmeyen bar_tz"):
        read_ohlcv(write(tmp_path, rows("2026-03-09T13:30:00")), bar_tz=bar_tz)


# --- marker, comments, symbol, timeframe ---------------------------------------------


def test_synthetic_marker_sets_the_flag_and_comments_are_skipped(tmp_path: Path) -> None:
    text = f"# exported by hand\n{SYNTHETIC_MARKER} anything here\n\n" + rows(
        "2026-03-09T13:30:00Z", "2026-03-09T13:35:00Z"
    )
    raw = read_ohlcv(write(tmp_path, text))
    assert raw.source.synthetic
    assert raw.lines == (5, 6)
    assert load_ohlcv(write(tmp_path, text)).source.synthetic


def test_file_without_marker_is_not_synthetic(tmp_path: Path) -> None:
    text = "# propbot data\n" + rows("2026-03-09T13:30:00Z", "2026-03-09T13:35:00Z")
    assert not read_ohlcv(write(tmp_path, text)).source.synthetic


def test_marker_after_the_header_is_just_a_bad_row(tmp_path: Path) -> None:
    text = rows("2026-03-09T13:30:00Z") + SYNTHETIC_MARKER + "\n"
    raw = read_ohlcv(write(tmp_path, text))
    assert not raw.source.synthetic
    assert raw.rejected_total == 1


def test_timeframe_is_the_most_common_spacing(tmp_path: Path) -> None:
    text = rows(
        "2026-03-09T13:30:00Z",
        "2026-03-09T13:35:00Z",
        "2026-03-09T13:40:00Z",
        "2026-03-09T13:55:00Z",
    )
    series = load_ohlcv(write(tmp_path, text))
    assert series.tf_seconds == 300
    assert load_ohlcv(write(tmp_path, text), tf="5").tf_seconds == 300


def test_timeframe_that_disagrees_with_the_file_is_refused(tmp_path: Path) -> None:
    path = write(tmp_path, rows("2026-03-09T13:30:00Z", "2026-03-09T13:35:00Z"))
    with pytest.raises(DataError, match="uyuşmuyor"):
        load_ohlcv(path, tf="1")
    with pytest.raises(DataError, match="desteklenmeyen zaman dilimi"):
        load_ohlcv(path, tf="D")


def test_spacing_that_is_not_whole_minutes_needs_an_explicit_timeframe(tmp_path: Path) -> None:
    path = write(tmp_path, rows("2026-03-09T13:30:00Z", "2026-03-09T13:30:30Z"))
    with pytest.raises(DataError, match="tam dakika değil"):
        load_ohlcv(path)
    assert load_ohlcv(path, tf="30S").tf_seconds == 30


def test_single_bar_needs_an_explicit_timeframe(tmp_path: Path) -> None:
    path = write(tmp_path, rows("2026-03-09T13:30:00Z"))
    with pytest.raises(DataError, match="tek bar"):
        load_ohlcv(path)
    assert len(load_ohlcv(path, tf="5")) == 1


def test_overlapping_bars_are_refused(tmp_path: Path) -> None:
    text = rows(*(f"2026-03-09T13:{minute:02d}:00Z" for minute in (30, 35, 40, 45, 47)))
    with pytest.raises(DataError, match="üst üste biniyor"):
        load_ohlcv(write(tmp_path, text))


# --- refusals ------------------------------------------------------------------------


def test_missing_file_directory_and_empty_file_are_clean_errors(tmp_path: Path) -> None:
    with pytest.raises(DataError, match="bulunamadı"):
        load_ohlcv(tmp_path / "nope.csv")
    with pytest.raises(DataError, match="dizin"):
        load_ohlcv(tmp_path)
    with pytest.raises(DataError, match="boş"):
        load_ohlcv(write(tmp_path, ""))


def test_file_over_the_size_cap_is_refused(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    assert ohlcv.MAX_FILE_BYTES >= 2 * 1024**3
    monkeypatch.setattr(ohlcv, "MAX_FILE_BYTES", 10)
    with pytest.raises(DataError, match="çok büyük"):
        load_ohlcv(write(tmp_path, rows("2026-03-09T13:30:00Z")))


def test_header_only_and_comment_only_files_are_refused(tmp_path: Path) -> None:
    with pytest.raises(DataError, match="bar yok"):
        load_ohlcv(write(tmp_path, HEADER + "\n"))
    with pytest.raises(DataError, match="başlık satırı yok"):
        load_ohlcv(write(tmp_path, f"{SYNTHETIC_MARKER}\n# nothing\n"))


@pytest.mark.parametrize(
    ("header", "message"),
    [
        ("date,o,h,l,c", "başlık satırı tanınmadı"),
        ("time;open;high;low;close", "Noktalı virgül"),
        ("time,open,high,close", "zorunlu sütun eksik: low"),
        ("time,open,high,low,close,Close", "yinelenen sütun: close"),
    ],
)
def test_unusable_headers_are_refused(tmp_path: Path, header: str, message: str) -> None:
    with pytest.raises(DataError, match=message):
        read_ohlcv(write(tmp_path, header + "\n"))


def test_non_utf8_files_are_refused(tmp_path: Path) -> None:
    with pytest.raises(DataError, match="UTF-16"):
        read_ohlcv(write(tmp_path, rows("2026-03-09T13:30:00Z"), encoding="utf-16"))
    with pytest.raises(DataError, match="UTF-8 değil"):
        read_ohlcv(write(tmp_path, "time,open,high,low,close\n\xff\n", encoding="latin-1"))


def test_binary_file_with_a_huge_line_is_refused(tmp_path: Path) -> None:
    path = tmp_path / "blob.csv"
    path.write_bytes(b"time,open,high,low,close\n" + b"x" * (1024 * 1024 + 10))
    with pytest.raises(DataError, match="satır çok uzun"):
        read_ohlcv(path)


@pytest.mark.parametrize(
    ("row", "message"),
    [
        ("2026-03-09T13:30:00Z,10,11,9", "4 alan var"),
        ("2026-03-09T13:30:00Z,10,11,9,10,5,6", "ondalık ayırıcı"),
        ("2026-03-09T13:30:00Z,10,x,9,10.5,1", "high sayı olmalı"),
        ("2026-03-09T13:30:00Z,10,11,,10.5,1", "low boş"),
        ("2026-03-09T13:30:00Z,10,inf,9,10.5,1", "sonlu"),
        ("2026-03-09T13:30:00Z,nan,11,9,10.5,1", "sonlu"),
        ("2026-03-09T13:30:00Z,10,11,9,10.5,abc", "volume sayı olmalı"),
    ],
)
def test_unreadable_rows_block_the_load(tmp_path: Path, row: str, message: str) -> None:
    path = write(tmp_path, f"{HEADER}\n2026-03-09T13:25:00Z,10,11,9,10.5,1\n{row}\n")
    raw = read_ohlcv(path)
    assert raw.rejected_total == 1
    assert raw.rows == 2
    with pytest.raises(DataError, match=f"satır 3: .*{message}"):
        load_ohlcv(path)


@pytest.mark.parametrize(
    ("prices", "message"),
    [
        ("0,11,0,10.5,1", "sonlu ve pozitif"),
        ("-1,11,-2,10.5,1", "sonlu ve pozitif"),
        ("10,10.25,9,10.5,1", "high"),
        ("10,11,10.75,10.5,1", "low"),
        ("10,11,9,10.5,-3", "hacim"),
    ],
)
def test_impossible_bars_block_the_load(tmp_path: Path, prices: str, message: str) -> None:
    path = write(tmp_path, rows("2026-03-09T13:30:00Z", "2026-03-09T13:35:00Z"))
    path.write_text(path.read_text() + f"2026-03-09T13:40:00Z,{prices}\n")
    assert len(read_ohlcv(path).bars) == 3
    with pytest.raises(DataError, match=f"satır 4: .*{message}"):
        load_ohlcv(path)


def test_duplicate_and_out_of_order_rows_block_the_load(tmp_path: Path) -> None:
    text = rows(
        "2026-03-09T13:30:00Z",
        "2026-03-09T13:35:00Z",
        "2026-03-09T13:35:00Z",
        "2026-03-09T13:45:00Z",
        "2026-03-09T13:40:00Z",
    )
    with pytest.raises(DataError) as caught:
        load_ohlcv(write(tmp_path, text))
    message = str(caught.value)
    assert "(2 hata)" in message
    assert "satır 4: yinelenen bar zamanı: 2026-03-09T13:35:00Z" in message
    assert "satır 6: sıra dışı bar: 2026-03-09T13:40:00Z" in message


def test_newest_first_file_gets_a_sorting_hint_but_is_not_sorted(tmp_path: Path) -> None:
    text = rows("2026-03-09T13:40:00Z", "2026-03-09T13:35:00Z", "2026-03-09T13:30:00Z")
    with pytest.raises(DataError, match="yeniden eskiye sıralı"):
        load_ohlcv(write(tmp_path, text))
    raw = read_ohlcv(write(tmp_path, text))
    assert [bar.ts_open_ms for bar in raw.bars] == sorted(
        (bar.ts_open_ms for bar in raw.bars), reverse=True
    )


def test_more_than_twenty_problems_are_summarised(tmp_path: Path) -> None:
    lines = [HEADER] + [f"2026-03-09T13:{m:02d}:00Z,10,x,9,10,1" for m in range(25)]
    path = write(tmp_path, "\n".join(lines) + "\n")
    with pytest.raises(DataError) as caught:
        load_ohlcv(path)
    message = str(caught.value)
    assert "(25 hata)" in message
    assert message.count("\nsatır ") == 20
    assert "satır 2: " in message
    assert "satır 21: " in message
    assert "satır 22: " not in message
    assert message.endswith("... ve 5 hata daha")


def test_errors_are_data_errors_without_a_traceback_of_other_types(tmp_path: Path) -> None:
    assert issubclass(DataError, Exception)
    assert not issubclass(DataError, ValueError | OSError)
