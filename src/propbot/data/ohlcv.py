"""OHLCV bar files: format detection, reading and loading into a `BarSeries` (SPEC §6.1).

Two layouts are recognised from the header line, never from the file name:

- generic (propbot's own files, TradingView's chart export, vendor files): comma separated
  `time,open,high,low,close[,volume]`; column names in any case, extra columns ignored.
  `time` is ISO 8601 with a UTC offset or `Z`, ISO 8601 without an offset (a wall time in
  `bar_tz`), or an integer Unix time (seconds; 10^11 and above is milliseconds).
- MetaTrader 5 "Export Bars": tab separated `<DATE> <TIME> <OPEN> <HIGH> <LOW> <CLOSE>
  <TICKVOL> <VOL> <SPREAD>`, date `YYYY.MM.DD`, time in the broker's SERVER clock.

Both export layouts were written from the tools' documentation and could not be checked
against a real export here (VERIFY). The parser follows the header, so a different column
order still loads.

`bar_tz` says how a wall time without an offset is read: "UTC", an IANA zone, or "NY+7"
(New York wall time plus seven hours: the usual MT5 server clock, "EET with US DST").
A wall time that does not exist in the zone (the hour skipped when summer time begins)
is an error for its row. A wall time that exists twice (the hour repeated when summer
time ends) is read as the EARLIER instant, unless the previous row of the file is at or
after that instant and before the later one: then it is the later instant, which keeps
the file ascending.

Nothing is repaired: no sorting, no de-duplication, no price fixing. `read_ohlcv` returns
what the file says row by row; `load_ohlcv` refuses a file with any blocking problem.
"""

import csv
import datetime as dt
import hashlib
import itertools
import math
import re
from collections import Counter
from collections.abc import Iterator, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import BinaryIO, Final
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from propbot.core.bars import SYNTHETIC_MARKER, Bar, BarError, BarSeries, DataSource
from propbot.core.session import NEW_YORK
from propbot.core.timeframe import SECONDS_PER_MINUTE, TimeframeError, timeframe_seconds

FORMAT_GENERIC: Final = "generic"
FORMAT_MT5: Final = "mt5"

BAR_TZ_UTC: Final = "UTC"
BAR_TZ_NY_PLUS_7: Final = "NY+7"

# Two years of 1-minute bars are about 50 MiB; the cap only stops a wrong path (a disk
# image, a database) from being read into memory.
MAX_FILE_BYTES: Final = 2 * 1024**3
MAX_REPORTED_PROBLEMS: Final = 20
# Rows that cannot be read are counted in full but only this many are kept: a file with
# the wrong decimal separator would otherwise hold one message per row in memory.
MAX_KEPT_REJECTED: Final = 1000

_MAX_LINE_BYTES: Final = 1024 * 1024
_MAX_UNIX_DIGITS: Final = 16
# Unix times below this are seconds, from it on milliseconds (10^11 s is the year 5138).
_UNIX_MS_FROM: Final = 10**11
# Anything outside is a misread value (a compact date taken for Unix seconds, microseconds
# taken for milliseconds), not a bar.
_MIN_YEAR: Final = 1980
_MAX_YEAR: Final = 2099
_MS_PER_SECOND: Final = 1000
_ONE_MS: Final = dt.timedelta(milliseconds=1)
_ONE_DAY: Final = dt.timedelta(days=1)
_NY_PLUS_7_SHIFT: Final = dt.timedelta(hours=7)
_EPOCH_UTC: Final = dt.datetime(1970, 1, 1, tzinfo=dt.UTC)
_EPOCH_NAIVE: Final = dt.datetime(1970, 1, 1)
_MIN_TS_MS: Final = (dt.datetime(_MIN_YEAR, 1, 1, tzinfo=dt.UTC) - _EPOCH_UTC) // _ONE_MS
_MAX_TS_MS: Final = (dt.datetime(_MAX_YEAR + 1, 1, 1, tzinfo=dt.UTC) - _EPOCH_UTC) // _ONE_MS
_QUOTED_TEXT_LIMIT: Final = 40

_UTF8_BOM: Final = "\ufeff"
_UTF16_BOMS: Final = (b"\xff\xfe", b"\xfe\xff")
_ISO_START: Final = re.compile(r"[0-9]{4}-[0-9]{2}-[0-9]{2}[T ][0-9]{2}:[0-9]{2}")
_MT5_DATE: Final = re.compile(r"([0-9]{4})\.([0-9]{2})\.([0-9]{2})")
_MT5_TIME: Final = re.compile(r"[0-9]{2}:[0-9]{2}(:[0-9]{2})?")

_GENERIC_HEADER: Final = "time,open,high,low,close[,volume]"
_MT5_HEADER: Final = "<DATE> <TIME> <OPEN> <HIGH> <LOW> <CLOSE> <TICKVOL> <VOL> <SPREAD>"


class DataError(Exception):
    """The price file cannot be used. The message is user-facing (Turkish)."""


@dataclass(frozen=True, slots=True)
class RowProblem:
    line: int  # 1-based line number in the file
    message: str  # Turkish, without the line-number prefix


@dataclass(frozen=True, slots=True)
class RawBars:
    """What a file says, row by row and in FILE order; nothing is checked or repaired.

    `bars` holds every row whose time and numbers could be read. Such a row can still be
    an impossible bar (see `bar_defect`) or be out of order (see `order_defects`).
    """

    symbol: str  # the file's stem; `load_ohlcv` lets the caller override it
    source: DataSource
    bars: tuple[Bar, ...]
    lines: tuple[int, ...]  # line number of each entry of `bars`
    rejected: tuple[RowProblem, ...]  # unreadable rows, the first `MAX_KEPT_REJECTED`
    rejected_total: int

    @property
    def rows(self) -> int:
        """Data rows in the file, readable or not."""
        return len(self.bars) + self.rejected_total


def read_ohlcv(path: Path, *, bar_tz: str = BAR_TZ_UTC) -> RawBars:
    """Read a bar file without judging it. Raises `DataError` when it cannot be read at all."""
    clock = _WallClock(bar_tz)
    try:
        if not path.exists():
            raise DataError(f"{path}: dosya bulunamadı")
        if path.is_dir():
            raise DataError(f"{path}: bu bir dizin; bar dosyasının yolunu ver")
        if not path.is_file():
            raise DataError(f"{path}: normal bir dosya değil")
        size = path.stat().st_size
        if size == 0:
            raise DataError(f"{path}: dosya boş")
        if size > MAX_FILE_BYTES:
            raise DataError(
                f"{path}: dosya çok büyük ({size / 1024**3:.1f} GiB; sınır "
                f"{MAX_FILE_BYTES / 1024**3:.0f} GiB). Bu bir bar dosyası mı?"
            )
        with path.open("rb") as handle:
            return _read(handle, path, clock)
    except UnicodeDecodeError as exc:
        raise DataError(
            f"{path}: dosya UTF-8 değil; UTF-8 olarak yeniden kaydet (BOM kabul edilir)"
        ) from exc
    except (OSError, csv.Error) as exc:
        raise DataError(f"{path}: dosya okunamadı: {exc}") from exc


def load_ohlcv(
    path: Path, *, symbol: str | None = None, tf: str | None = None, bar_tz: str = BAR_TZ_UTC
) -> BarSeries:
    """Read a bar file and build the series, or refuse it with every blocking problem listed.

    `tf` is a TradingView timeframe string ("1", "5", "60"). Left out, the bar length is
    the most common distance between consecutive bars and must be whole minutes. `symbol`
    left out is the file's stem.
    """
    raw = read_ohlcv(path, bar_tz=bar_tz)
    total, shown = _blocking_problems(raw)
    if total:
        raise _refusal(path, "veri dosyası yüklenemedi", total, shown, _descending_hint(raw))
    if not raw.bars:
        raise DataError(f"{path}: bar yok (yalnızca başlık satırı var)")

    tf_seconds = _bar_length(path, raw.bars, tf)
    tf_ms = tf_seconds * _MS_PER_SECOND
    overlaps = [
        (raw.lines[index + 1], _overlap_text(following.ts_open_ms - bar.ts_open_ms, tf_seconds))
        for index, (bar, following) in enumerate(itertools.pairwise(raw.bars))
        if following.ts_open_ms - bar.ts_open_ms < tf_ms
    ]
    if overlaps:
        raise _refusal(
            path,
            "barlar üst üste biniyor",
            len(overlaps),
            overlaps[:MAX_REPORTED_PROBLEMS],
            "",
        )
    try:
        return BarSeries(
            symbol=symbol if symbol is not None else raw.symbol,
            tf_seconds=tf_seconds,
            bars=raw.bars,
            source=raw.source,
        )
    except BarError as exc:  # unreachable after the checks above; never let it pass silently
        raise DataError(f"{path}: {exc}") from exc


def bar_defect(bar: Bar) -> str | None:
    """Why this row cannot be a bar (Turkish), or None. Mirrors `propbot.core.bars.check_bar`."""
    prices = (bar.open, bar.high, bar.low, bar.close)
    if not all(math.isfinite(price) and price > 0.0 for price in prices):
        return (
            "fiyatlar sonlu ve pozitif olmalı "
            f"(open={bar.open:g}, high={bar.high:g}, low={bar.low:g}, close={bar.close:g})"
        )
    if bar.high < max(bar.open, bar.close):
        return f"high ({bar.high:g}) open/close'un altında ({max(bar.open, bar.close):g})"
    if bar.low > min(bar.open, bar.close):
        return f"low ({bar.low:g}) open/close'un üstünde ({min(bar.open, bar.close):g})"
    if not (math.isfinite(bar.volume) and bar.volume >= 0.0):
        return f"hacim sonlu ve negatif olmayan bir sayı olmalı ({bar.volume:g})"
    return None


def order_defects(bars: Sequence[Bar]) -> tuple[list[int], list[int]]:
    """Indices of duplicate rows and of out-of-order rows, each in file order.

    A row is a duplicate when an earlier row has the same open time. It is out of order
    when it is older than a row before it and its time was not seen yet.
    """
    duplicates: list[int] = []
    out_of_order: list[int] = []
    newest: int | None = None
    seen: set[int] | None = None
    for index, bar in enumerate(bars):
        ts = bar.ts_open_ms
        if newest is None or ts > newest:
            newest = ts
            if seen is not None:
                seen.add(ts)
        elif ts == newest:
            duplicates.append(index)
        else:
            # Only a file that goes backwards pays for the set of every time seen so far.
            if seen is None:
                seen = {earlier.ts_open_ms for earlier in bars[:index]}
            if ts in seen:
                duplicates.append(index)
            else:
                out_of_order.append(index)
                seen.add(ts)
    return duplicates, out_of_order


def common_spacing_ms(bars: Sequence[Bar]) -> int | None:
    """The most common positive distance between consecutive rows, in milliseconds.

    None when no two consecutive rows are ascending. Of equally common distances the
    smallest wins: a bar length that is too short shows up as gaps, one that is too long
    would hide them.
    """
    counts = Counter(
        following.ts_open_ms - bar.ts_open_ms
        for bar, following in itertools.pairwise(bars)
        if following.ts_open_ms > bar.ts_open_ms
    )
    if not counts:
        return None
    best = max(counts.values())
    return min(spacing for spacing, count in counts.items() if count == best)


def utc_text(ts_ms: int) -> str:
    """ISO 8601 UTC text of an epoch-millisecond instant, with `Z`; milliseconds only if any."""
    seconds, millis = divmod(ts_ms, _MS_PER_SECOND)
    text = dt.datetime.fromtimestamp(seconds, dt.UTC).strftime("%Y-%m-%dT%H:%M:%S")
    return f"{text}.{millis:03d}Z" if millis else f"{text}Z"


class _RowError(ValueError):
    """One row cannot be read. The message is user-facing (Turkish)."""


class _WallClock:
    """Turns a wall time without an offset into UTC epoch milliseconds for one `bar_tz`."""

    def __init__(self, bar_tz: str) -> None:
        self.bar_tz: Final = bar_tz
        key = NEW_YORK if bar_tz == BAR_TZ_NY_PLUS_7 else bar_tz
        # "localtime" is whatever the machine is set to: the same file would load differently
        # on the VPS and on the laptop.
        if key == "localtime":
            raise DataError(_unknown_zone(bar_tz))
        try:
            self._zone: Final = ZoneInfo(key)
        except (ZoneInfoNotFoundError, ValueError, OSError) as exc:
            raise DataError(_unknown_zone(bar_tz)) from exc
        self._shift: Final = _NY_PLUS_7_SHIFT if bar_tz == BAR_TZ_NY_PLUS_7 else dt.timedelta(0)
        # Local date (ordinal) -> UTC offset in ms when it is the same all day, else None.
        self._day_offsets: dict[int, int | None] = {}

    def to_ms(self, wall: dt.datetime, previous_ms: int | None) -> int:
        local = wall - self._shift
        ordinal = local.toordinal()
        try:
            offset = self._day_offsets[ordinal]
        except KeyError:
            offset = self._day_offsets[ordinal] = self._whole_day_offset(local)
        naive_ms = (local - _EPOCH_NAIVE) // _ONE_MS
        if offset is not None:
            return naive_ms - offset
        return self._on_change_day(wall, local, naive_ms, previous_ms)

    def _whole_day_offset(self, local: dt.datetime) -> int | None:
        # One zone lookup per day instead of one per row: only the day a clock change
        # falls on takes the slow path below.
        midnight = local.replace(hour=0, minute=0, second=0, microsecond=0)
        start = _offset_ms(midnight.replace(tzinfo=self._zone))
        end = _offset_ms((midnight + _ONE_DAY).replace(tzinfo=self._zone))
        return start if start == end else None

    def _on_change_day(
        self, wall: dt.datetime, local: dt.datetime, naive_ms: int, previous_ms: int | None
    ) -> int:
        # PEP 495: `fold` picks the offset before (0) or after (1) a clock change for a
        # wall time that falls into it. Equal offsets: the time is ordinary.
        before = _offset_ms(local.replace(tzinfo=self._zone, fold=0))
        after = _offset_ms(local.replace(tzinfo=self._zone, fold=1))
        if before == after:
            return naive_ms - before
        if before < after:
            raise _RowError(
                f"{wall:%Y-%m-%d %H:%M:%S} saati {self.bar_tz} diliminde yok (yaz saatine "
                "geçişte atlanan saat); bar_tz yanlış olabilir"
            )
        earlier, later = naive_ms - before, naive_ms - after
        if previous_ms is not None and earlier <= previous_ms < later:
            return later
        return earlier


@dataclass(frozen=True, slots=True)
class _Layout:
    format: str
    delimiter: str
    fields: int  # columns in the header; every row must have as many
    time: int
    date: int | None  # MT5 only
    open: int
    high: int
    low: int
    close: int
    volume: int | None  # generic `volume`, MT5 `<VOL>`
    tick_volume: int | None  # MT5 `<TICKVOL>`

    def label(self, name: str) -> str:
        """The column as the user sees it in the header, for messages."""
        return f"<{name.upper()}>" if self.format == FORMAT_MT5 else name


def _read(handle: BinaryIO, path: Path, clock: _WallClock) -> RawBars:
    digest = hashlib.sha256()
    lines = _decoded_lines(handle, digest, path)
    header_line = 0
    header: str | None = None
    synthetic = False
    for text in lines:
        header_line += 1
        stripped = text.strip()
        if not stripped:
            continue
        if stripped.startswith("#"):
            # Any comment line above the header counts, in any case: a synthetic file must
            # not pass for real data because a note was put above the marker (ADR-0007).
            if stripped.lower().startswith(SYNTHETIC_MARKER):
                synthetic = True
            continue
        header = text.rstrip("\r\n")
        break
    if header is None:
        raise DataError(f"{path}: başlık satırı yok (dosyada yalnızca boş ya da # satırları var)")

    layout = _detect_layout(header, path)
    if layout.format == FORMAT_MT5 and clock.bar_tz == BAR_TZ_UTC:
        raise DataError(
            f"{path}: MetaTrader 5 dışa aktarımındaki bar saatleri brokerin SUNUCU saatidir, "
            "UTC değildir; yanlış okunursa her seans saati kayar. bar_tz açıkça verilmeli "
            f'(New York + 7 saat kullanan sunucular için "{BAR_TZ_NY_PLUS_7}", başka sunucular '
            'için IANA dilimi; sunucu gerçekten UTC ise "Etc/UTC")'
        )

    bars: list[Bar] = []
    line_numbers: list[int] = []
    rejected: list[RowProblem] = []
    rejected_total = 0
    previous_ms: int | None = None
    reader = csv.reader(lines, delimiter=layout.delimiter)
    for row in reader:
        if not row:
            continue
        try:
            bar = _parse_row(row, layout, clock, previous_ms)
        except _RowError as exc:
            rejected_total += 1
            if len(rejected) < MAX_KEPT_REJECTED:
                rejected.append(RowProblem(header_line + reader.line_num, str(exc)))
            continue
        bars.append(bar)
        line_numbers.append(header_line + reader.line_num)
        previous_ms = bar.ts_open_ms

    return RawBars(
        symbol=path.stem,
        source=DataSource(
            name=path.name,
            format=layout.format,
            sha256=digest.hexdigest(),
            synthetic=synthetic,
            bar_tz=clock.bar_tz,
        ),
        bars=tuple(bars),
        lines=tuple(line_numbers),
        rejected=tuple(rejected),
        rejected_total=rejected_total,
    )


def _decoded_lines(handle: BinaryIO, digest: "hashlib._Hash", path: Path) -> Iterator[str]:
    """Lines of the file as text, hashing the raw bytes on the way (one pass over the file)."""
    first = True
    while True:
        raw = handle.readline(_MAX_LINE_BYTES + 1)
        if not raw:
            return
        if len(raw) > _MAX_LINE_BYTES:
            raise DataError(f"{path}: satır çok uzun; bu bir metin (CSV) dosyası değil")
        digest.update(raw)
        if first:
            first = False
            if raw.startswith(_UTF16_BOMS):
                raise DataError(
                    f"{path}: dosya UTF-16 kodlanmış; UTF-8 olarak yeniden kaydet "
                    "(ör. iconv -f UTF-16 -t UTF-8)"
                )
            yield raw.decode("utf-8").removeprefix(_UTF8_BOM)
        else:
            yield raw.decode("utf-8")


def _detect_layout(header: str, path: Path) -> _Layout:
    delimiter = "\t" if "\t" in header else ","
    names = [name.strip().lower() for name in next(csv.reader([header], delimiter=delimiter))]

    def column(name: str, *, required: bool = True) -> int | None:
        hits = [index for index, candidate in enumerate(names) if candidate == name]
        if len(hits) > 1:
            raise DataError(f"{path}: yinelenen sütun: {name} (büyük/küçük harf ayrımı yapılmaz)")
        if not hits:
            if required:
                raise DataError(f"{path}: zorunlu sütun eksik: {name}. {_expected_headers()}")
            return None
        return hits[0]

    def required(name: str) -> int:
        index = column(name)
        assert index is not None
        return index

    if "<date>" in names:
        if "<time>" not in names:
            raise DataError(
                f"{path}: MetaTrader 5 dosyasında <TIME> sütunu yok; günlük ve daha uzun barlar "
                "desteklenmez (gün içi barları dışa aktar)"
            )
        return _Layout(
            format=FORMAT_MT5,
            delimiter=delimiter,
            fields=len(names),
            time=required("<time>"),
            date=required("<date>"),
            open=required("<open>"),
            high=required("<high>"),
            low=required("<low>"),
            close=required("<close>"),
            volume=column("<vol>", required=False),
            tick_volume=column("<tickvol>", required=False),
        )
    if "time" in names:
        return _Layout(
            format=FORMAT_GENERIC,
            delimiter=delimiter,
            fields=len(names),
            time=required("time"),
            date=None,
            open=required("open"),
            high=required("high"),
            low=required("low"),
            close=required("close"),
            volume=column("volume", required=False),
            tick_volume=None,
        )
    hint = (
        " Noktalı virgülle ayrılmış dosyalar desteklenmez: ayırıcı virgül ya da sekme, "
        "ondalık ayırıcı nokta olmalı."
        if ";" in header
        else ""
    )
    raise DataError(
        f"{path}: başlık satırı tanınmadı: {_quoted(header)}. {_expected_headers()}{hint}"
    )


def _expected_headers() -> str:
    return (
        f"Beklenen başlık: {_GENERIC_HEADER} (virgülle ayrılmış) ya da MetaTrader 5'in "
        f"{_MT5_HEADER} başlığı (sekmeyle ayrılmış)"
    )


def _parse_row(row: list[str], layout: _Layout, clock: _WallClock, previous_ms: int | None) -> Bar:
    if len(row) != layout.fields:
        hint = " (ondalık ayırıcı nokta olmalı, virgül değil)" if len(row) > layout.fields else ""
        raise _RowError(f"{len(row)} alan var, başlıkta {layout.fields} sütun var{hint}")
    time_text = row[layout.time].strip()
    if layout.date is None:
        ts_ms = _parse_time(time_text, clock, previous_ms)
    else:
        ts_ms = _parse_mt5_time(row[layout.date].strip(), time_text, clock, previous_ms)
    return Bar(
        ts_open_ms=ts_ms,
        open=_number(row[layout.open], layout.label("open")),
        high=_number(row[layout.high], layout.label("high")),
        low=_number(row[layout.low], layout.label("low")),
        close=_number(row[layout.close], layout.label("close")),
        volume=_volume(row, layout),
    )


def _parse_time(text: str, clock: _WallClock, previous_ms: int | None) -> int:
    if text.isascii() and text.isdigit():
        if len(text) > _MAX_UNIX_DIGITS:
            raise _RowError(f"time okunamadı: {_quoted(text)}")
        value = int(text)
        ts_ms = value if value >= _UNIX_MS_FROM else value * _MS_PER_SECOND
        if not _MIN_TS_MS <= ts_ms < _MAX_TS_MS:
            raise _RowError(
                f"time {_MIN_YEAR}-{_MAX_YEAR} aralığının dışında bir Unix zamanı: {_quoted(text)} "
                "(saniye ya da milisaniye olmalı)"
            )
        return ts_ms
    if _ISO_START.match(text) is None:
        raise _RowError(
            "time ISO 8601 tarih-saat (ör. 2026-01-05T14:30:00Z) ya da tam sayı Unix zamanı "
            f"olmalı; okunan: {_quoted(text)}"
        )
    try:
        moment = dt.datetime.fromisoformat(text)
    except ValueError:
        raise _RowError(f"time okunamadı: {_quoted(text)}") from None
    return _moment_ms(moment, clock, previous_ms, text)


def _parse_mt5_time(
    date_text: str, time_text: str, clock: _WallClock, previous_ms: int | None
) -> int:
    date = _MT5_DATE.fullmatch(date_text)
    if date is None or _MT5_TIME.fullmatch(time_text) is None:
        raise _RowError(
            "<DATE> YYYY.MM.DD ve <TIME> HH:MM:SS olmalı; okunan: "
            f"{_quoted(date_text)} {_quoted(time_text)}"
        )
    text = f"{date.group(1)}-{date.group(2)}-{date.group(3)}T{time_text}"
    try:
        moment = dt.datetime.fromisoformat(text)
    except ValueError:
        raise _RowError(
            f"<DATE> <TIME> geçerli bir tarih-saat değil: {_quoted(date_text)} {_quoted(time_text)}"
        ) from None
    return _moment_ms(moment, clock, previous_ms, f"{date_text} {time_text}")


def _moment_ms(moment: dt.datetime, clock: _WallClock, previous_ms: int | None, text: str) -> int:
    if not _MIN_YEAR <= moment.year <= _MAX_YEAR:
        raise _RowError(f"tarih {_MIN_YEAR}-{_MAX_YEAR} aralığının dışında: {_quoted(text)}")
    if moment.tzinfo is None:
        return clock.to_ms(moment, previous_ms)
    return (moment - _EPOCH_UTC) // _ONE_MS


def _number(text: str, column: str) -> float:
    try:
        value = float(text)
    except ValueError:
        if not text.strip():
            raise _RowError(f"{column} boş") from None
        raise _RowError(
            f"{column} sayı olmalı (ondalık ayırıcı nokta); okunan: {_quoted(text)}"
        ) from None
    if not math.isfinite(value):
        raise _RowError(f"{column} sonlu bir sayı olmalı; okunan: {_quoted(text)}")
    return value


def _volume(row: list[str], layout: _Layout) -> float:
    if layout.format == FORMAT_MT5:
        # <VOL> is the exchange volume and stays 0 on instruments that have none (CFDs);
        # the tick count is then the only activity measure the file carries.
        real = 0.0 if layout.volume is None else _number(row[layout.volume], "<VOL>")
        if real > 0.0 or layout.tick_volume is None:
            return real
        return _number(row[layout.tick_volume], "<TICKVOL>")
    if layout.volume is None:
        return 0.0
    text = row[layout.volume]
    # An empty cell is "no volume reported"; it shows up as a zero-volume bar in the report.
    return _number(text, "volume") if text.strip() else 0.0


def _blocking_problems(raw: RawBars) -> tuple[int, list[tuple[int, str]]]:
    """Total count and the first `MAX_REPORTED_PROBLEMS` (line, message) of what stops a load."""
    cap = MAX_REPORTED_PROBLEMS
    found = [(problem.line, problem.message) for problem in raw.rejected[:cap]]
    total = raw.rejected_total

    kept = 0
    for index, bar in enumerate(raw.bars):
        defect = bar_defect(bar)
        if defect is not None:
            total += 1
            if kept < cap:
                kept += 1
                found.append((raw.lines[index], defect))

    duplicates, out_of_order = order_defects(raw.bars)
    total += len(duplicates) + len(out_of_order)
    found.extend(
        (raw.lines[index], f"yinelenen bar zamanı: {utc_text(raw.bars[index].ts_open_ms)}")
        for index in duplicates[:cap]
    )
    found.extend(
        (
            raw.lines[index],
            f"sıra dışı bar: {utc_text(raw.bars[index].ts_open_ms)}, dosyada kendinden önceki "
            "bir bardan eski",
        )
        for index in out_of_order[:cap]
    )
    # Each kind is in file order, so the first `cap` of each hold the first `cap` overall.
    found.sort()
    return total, found[:cap]


def _descending_hint(raw: RawBars) -> str:
    descending = sum(
        1
        for bar, following in itertools.pairwise(raw.bars)
        if following.ts_open_ms < bar.ts_open_ms
    )
    if len(raw.bars) > 1 and descending == len(raw.bars) - 1:
        return (
            "Dosya yeniden eskiye sıralı görünüyor. Yükleyici sıralama yapmaz: dosyayı eskiden "
            "yeniye sıralayıp yeniden dene."
        )
    return ""


def _refusal(
    path: Path, headline: str, total: int, shown: Sequence[tuple[int, str]], hint: str
) -> DataError:
    lines = [f"{path}: {headline} ({total} hata)"]
    lines.extend(f"satır {line}: {message}" for line, message in shown)
    if total > len(shown):
        lines.append(f"... ve {total - len(shown)} hata daha")
    if hint:
        lines.append(hint)
    return DataError("\n".join(lines))


def _bar_length(path: Path, bars: Sequence[Bar], tf: str | None) -> int:
    spacing_ms = common_spacing_ms(bars)
    if tf is not None:
        try:
            tf_seconds = timeframe_seconds(tf)
        except TimeframeError as exc:
            raise DataError(f"{path}: {exc}") from exc
        if spacing_ms is not None and spacing_ms != tf_seconds * _MS_PER_SECOND:
            raise DataError(
                f"{path}: verilen zaman dilimi ({tf_seconds} sn) dosyadaki en yaygın bar "
                f"aralığıyla ({spacing_ms / _MS_PER_SECOND:g} sn) uyuşmuyor"
            )
        return tf_seconds
    if spacing_ms is None:
        raise DataError(
            f"{path}: bar süresi dosyadan çıkarılamadı (tek bar var); zaman dilimini açıkça ver"
        )
    minute_ms = SECONDS_PER_MINUTE * _MS_PER_SECOND
    if spacing_ms % minute_ms != 0:
        raise DataError(
            f"{path}: en yaygın bar aralığı ({spacing_ms / _MS_PER_SECOND:g} sn) tam dakika "
            "değil; zaman dilimini açıkça ver"
        )
    try:
        return timeframe_seconds(str(spacing_ms // minute_ms))
    except TimeframeError as exc:
        raise DataError(f"{path}: {exc}") from exc


def _overlap_text(spacing_ms: int, tf_seconds: int) -> str:
    return (
        f"bar bir öncekinden {spacing_ms / _MS_PER_SECOND:g} sn sonra açılıyor; "
        f"bar süresi {tf_seconds} sn"
    )


def _offset_ms(moment: dt.datetime) -> int:
    offset = moment.utcoffset()
    assert offset is not None
    return offset // _ONE_MS


def _unknown_zone(bar_tz: str) -> str:
    return (
        f"bilinmeyen bar_tz: {_quoted(bar_tz)} ({BAR_TZ_UTC}, bir IANA dilimi — ör. "
        f'Europe/Prague — ya da "{BAR_TZ_NY_PLUS_7}" olmalı)'
    )


def _quoted(text: str) -> str:
    """The file's own text for a message, shortened: a broken row can be megabytes long."""
    if len(text) > _QUOTED_TEXT_LIMIT:
        return repr(text[:_QUOTED_TEXT_LIMIT] + "…")
    return repr(text)
