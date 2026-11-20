"""Pine-vs-Python parity (SPEC §6.6, ADR-0007 item 4, ADR-0008).

The user exports TradingView's Strategy Tester "List of Trades" and the chart's bars, runs
the Python engine on the same bars with `FillModel.TV_OHLC`, and this module says how
well the two trade lists agree. The unit of comparison is ONE EXIT FILL, the same unit as
`TradeRecord` (a partial exit is its own row).

The layout of TradingView's export could not be checked from this environment (VERIFY).
The parser is therefore header-driven and tolerant where the recalled layout is known to
vary (column names, currency suffixes, delimiter, decimal mark, row order), and loud
about everything it does not understand: a parity verdict on a half-read file would be
worse than none. Nothing produced from the hand-written `sample_*` test files is parity
evidence; their reports are marked synthetic.

Matching rules (decided here, documented in the report):

- A pair MATCHES when entry time (within `time_tolerance_bars`), side and exit reason
  agree and entry and exit price are each within `tolerance_ticks` ticks. The exit time
  is not a criterion of SPEC §6.6; a differing exit time is counted for information.
- Exit reason: TradingView's "Signal" text of the exit row is expected to show the exit
  order's comment when it has one and its id otherwise (`strategy.close_all` and
  `strategy.close` always pass a comment in the reference scripts). That is unverified,
  so either the Python row's comment or its exit id is accepted and the result counts
  which one matched.
- Pairing is one deterministic greedy assignment over candidate pairs whose entry times
  are at most `pair_window_bars` apart: pairs with fewer differing fields, then nearer
  in time, then nearer in price go first. Identical rows therefore always pair with each
  other, and a near-miss (one field off) is reported as one mismatch naming the field
  instead of two unexplained one-sided rows.
- Rows left out by the warm-up cut or the overlap window are counted; when they exceed
  MAX_EXCLUDED_SHARE of either list the comparison cannot pass (`blocked_by`), so a short
  export or a late cut-off cannot hide mismatches. Tolerances can only be tightened.
- The known TP difference of SPEC §3.1 does not exist in a `tv_ohlc` run (the strategy
  re-prices at the fill bar's close, as Pine does; ADR-0008), so no extra tolerance.
- A port may differ from its Pine script on purpose where the script would break an
  invariant of the bot. Such a difference is listed in `EXPECTED_DEVIATIONS` and printed
  in the report and the artifact of that strategy. Listing explains, it never tolerates:
  the differing fills are counted as mismatches like any other.
"""

import csv
import datetime as dt
import io
import math
import os
import re
import stat
from collections import Counter
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass
from enum import StrEnum
from pathlib import Path
from types import MappingProxyType
from typing import Final
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from pydantic import JsonValue

from propbot.backtest.artifacts import (
    MAX_PARITY_MISMATCHES,
    DataInfo,
    InstrumentInfo,
    ParityArtifact,
    Scalar,
    build_artifact,
)
from propbot.backtest.config import MIN_PARITY_RATE
from propbot.backtest.model import ExitReason, FillModel, TradeRecord
from propbot.core.bars import BarSeries
from propbot.core.instrument import Instrument
from propbot.strategies.base import Side

MAX_EXPORT_BYTES: Final = 16 * 1024 * 1024
MAX_GOLDEN_BYTES: Final = 16 * 1024 * 1024
MAX_REPORTED_ERRORS: Final = 20
DEFAULT_TOLERANCE_TICKS: Final = 1.0
# SPEC §6.6 sets the price tolerance at 1 tick and asks for matching entry times: a caller
# may tighten both, never loosen them past these (a 50-tick tolerance would pass anything).
MAX_TOLERANCE_TICKS: Final = 1.0
MAX_TIME_TOLERANCE_BARS: Final = 1
# Rows left out by the warm-up cut or the overlap window are not compared, so each could
# hide a mismatch. When more than this share of either list is left out, the comparison
# cannot pass: the exclusions alone would exceed the whole mismatch budget of SPEC §9.
MAX_EXCLUDED_SHARE: Final = 1.0 - MIN_PARITY_RATE
DEFAULT_PAIR_WINDOW_BARS: Final = 3
DEFAULT_REPORT_ROWS: Final = 20
SAMPLE_PREFIX: Final = "sample_"

# Float noise allowed on top of the price tolerance, in ticks: far above the noise of
# 4500.1 - 4500.0 (~1e-12) and far below any real price difference (0.01 tick).
PRICE_EPSILON_TICKS: Final = 1e-6

GOLDEN_MARKER: Final = "# python-engine regression pin, NOT TradingView evidence:"
GOLDEN_DECIMALS: Final = 6
GOLDEN_COLUMNS: Final = (
    "seq",
    "strategy_id",
    "entry_id",
    "exit_id",
    "reason",
    "setup",
    "comment",
    "side",
    "qty",
    "entry_time_utc",
    "exit_time_utc",
    "entry_price",
    "exit_price",
    "pnl",
    "r",
)

UNVERIFIED_NOTES: Final = (
    "TradingView 'List of Trades' dışa aktarım biçimi bu ortamdan doğrulanamadı (VERIFY): "
    "sütun adları, Date/Time'ın bar açılış saati olup olmadığı ve 'Signal' sütununda emir "
    "yorumu mu kimliği mi yazdığı gerçek bir dışa aktarımla kontrol edilmeli.",
    "Parite kanıtı yalnızca gerçek TradingView dışa aktarımı ve aynı barlarla sayılır "
    "(ADR-0007); örnek (sample_) ya da sentetik dosyalarla elde edilen oran kanıt değildir.",
)
# Wanted differences between a port and its Pine script, by strategy id (user decision of
# 2026-11-19). One table for the terminal report and the artifact's notes, so the guide
# and the report cannot drift apart. An entry changes no count and no verdict.
EXPECTED_DEVIATION_HEADING: Final = "Beklenen güvenli sapma"
EXPECTED_DEVIATIONS: Final[Mapping[str, tuple[str, ...]]] = MappingProxyType(
    {
        "mtf_trend_cont": (
            "İz göstergesi ısınırken (henüz değer üretmeden) Python portu runner'ın stop'unu "
            "başa başta tutar; Pine betiği o pencerede runner'ı stopsuz bırakır (CLAUDE.md "
            "değişmez kural 2: açık pozisyonun stop'u asla kaldırılmaz), bu yüzden o penceredeki "
            "dolumlar farklı olabilir.",
            "Bu farklar yine de uyuşmazlık olarak SAYILIR, hiçbiri sessizce hoş görülmez; "
            "ısınmayı karşılaştırmanın dışında tutmanın yolu --warmup-bars seçeneğidir.",
        ),
    }
)
SAMPLE_NOTE: Final = (
    "Bu karşılaştırma elle yazılmış örnek (sample_) dosyalarla yapıldı: ayrıştırıcı testi "
    "girdisidir, TradingView kanıtı DEĞİLDİR."
)

_MINUTE_MS: Final = 60_000
_EPOCH: Final = dt.datetime(1970, 1, 1, tzinfo=dt.UTC)
_DELIMITERS: Final = (",", ";", "\t")
_DELIMITER_NAMES: Final = {",": "virgül", ";": "noktalı virgül", "\t": "sekme"}
_SPACES: Final = " \u00a0\u202f"  # space, no-break space, narrow no-break space
_SPACE_GROUPING: Final = str.maketrans("", "", _SPACES)
_MINUS_SIGNS: Final = str.maketrans({"−": "-", "–": "-"})
_TIMESTAMP: Final = re.compile(
    r"(?P<date>\d{4}-\d{2}-\d{2})[ T](?P<time>\d{2}:\d{2}(?::\d{2})?)"
    r"(?P<offset>Z|[+-]\d{2}:?\d{2})?"
)
_CURRENCY_SUFFIX: Final = r"(?:\s+\(?[a-z]{3}\)?)?"


class ParityError(Exception):
    """The parity input cannot be used. The message is user-facing (Turkish)."""


class Column(StrEnum):
    TRADE_NO = "trade_no"
    TYPE = "type"
    SIGNAL = "signal"
    TIME = "time"
    PRICE = "price"
    QTY = "qty"


# Recalled header names (VERIFY on a real export). Matching is case-insensitive with
# collapsed spaces. Only the price may carry a currency suffix ("Price USD"): a size
# column with a currency ("Position size USD") is a VALUE, not a quantity.
COLUMN_ALIASES: Final[Mapping[Column, tuple[str, ...]]] = MappingProxyType(
    {
        Column.TRADE_NO: ("trade #", "trade no", "trade no.", "trade number"),
        Column.TYPE: ("type",),
        Column.SIGNAL: ("signal",),
        Column.TIME: ("date/time", "date and time", "date & time", "datetime", "date time"),
        Column.PRICE: ("price",),
        Column.QTY: ("contracts", "position size", "position size (qty)", "qty", "quantity"),
    }
)
REQUIRED: Final = (Column.TRADE_NO, Column.TYPE, Column.SIGNAL, Column.TIME, Column.PRICE)
_SUFFIXED: Final = frozenset({Column.PRICE})

_TYPES: Final[Mapping[str, tuple[bool, Side]]] = MappingProxyType(
    {
        "entry long": (True, Side.LONG),
        "exit long": (False, Side.LONG),
        "entry short": (True, Side.SHORT),
        "exit short": (False, Side.SHORT),
    }
)
# Recalled: an exit row of a trade still open at the end of the chart says "Open" (VERIFY).
_OPEN_SIGNAL: Final = "open"


class Field(StrEnum):
    ENTRY_TIME = "entry_time"
    SIDE = "side"
    EXIT_REASON = "exit_reason"
    ENTRY_PRICE = "entry_price"
    EXIT_PRICE = "exit_price"


FIELD_LABELS: Final[Mapping[Field, str]] = MappingProxyType(
    {
        Field.ENTRY_TIME: "giriş zamanı",
        Field.SIDE: "yön",
        Field.EXIT_REASON: "çıkış nedeni",
        Field.ENTRY_PRICE: "giriş fiyatı",
        Field.EXIT_PRICE: "çıkış fiyatı",
    }
)


# --- TradingView side --------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class TvFill:
    """One exit fill of TradingView's list: the exit row with its trade's entry row."""

    trade_no: int
    side: Side
    entry_ts_ms: int  # UTC; the chart-tz Date/Time of the entry row
    entry_price: float
    entry_signal: str
    exit_ts_ms: int
    exit_price: float
    exit_signal: str  # compared with the Python row's exit comment / exit id
    qty: float | None  # None when the export has no size column
    line: int  # line of the exit row in the file (0 for fills built in code)


@dataclass(frozen=True, slots=True)
class SkippedRow:
    line: int
    reason: str  # Turkish


@dataclass(frozen=True, slots=True)
class TvTradeList:
    source: str  # file name only
    tz: str
    fills: tuple[TvFill, ...]
    open_trades: int  # trades with an entry row and no closed exit row (skipped)
    skipped: tuple[SkippedRow, ...]
    columns: tuple[tuple[str, str], ...]  # (logical column, header found)
    ignored_columns: tuple[str, ...]
    delimiter: str
    decimal_mark: str

    @property
    def sample(self) -> bool:
        return self.source.startswith(SAMPLE_PREFIX)

    @classmethod
    def from_fills(
        cls, fills: Iterable[TvFill], *, source: str = "kod", tz: str = "UTC"
    ) -> "TvTradeList":
        """A list built in code (tests, conversions); nothing was skipped."""
        return cls(
            source=source,
            tz=tz,
            fills=tuple(fills),
            open_trades=0,
            skipped=(),
            columns=(),
            ignored_columns=(),
            delimiter=",",
            decimal_mark=".",
        )


@dataclass(frozen=True, slots=True)
class _Row:
    line: int
    trade_no: int
    is_entry: bool
    side: Side
    signal: str
    ts_ms: int | None  # None: an open trade's exit row
    price: float | None
    qty: float | None


def load_tradingview_trades(path: Path, *, tz: str) -> TvTradeList:
    """Read a "List of Trades" CSV export. Times without an offset are read in `tz`.

    `tz` is the CHART's time zone at export time (TradingView writes local times without
    an offset). Every problem is a `ParityError` listing at most 20 lines.
    """
    zone = _zone(tz)
    name = path.name
    text = _read_text(path, MAX_EXPORT_BYTES, "dışa aktarım")
    header_line = text.split("\n", 1)[0].rstrip("\r")
    if not header_line.strip():
        raise ParityError(f"{name}: dosya boş ya da ilk satır başlık değil")
    delimiter = _sniff_delimiter(header_line, name)
    try:
        records = list(csv.reader(io.StringIO(text, newline=""), delimiter=delimiter, strict=True))
    except csv.Error as exc:
        raise ParityError(f"{name}: CSV okunamadı: {exc}") from None
    # csv.reader does not report line numbers for a list; re-read with one for messages.
    lines = _record_lines(text, delimiter)
    header = records[0]
    mapping, ignored = _map_columns(header, name)
    index = {column: position for column, (position, _) in mapping.items()}

    numeric_cells: list[str] = []
    for record in records[1:]:
        if len(record) == len(header):
            numeric_cells.append(record[index[Column.PRICE]])
            if Column.QTY in index:
                numeric_cells.append(record[index[Column.QTY]])
    decimal_mark = _decimal_mark(numeric_cells, delimiter, name)

    rows: list[_Row] = []
    skipped: list[SkippedRow] = []
    errors: list[str] = []
    for record, line in zip(records[1:], lines[1:], strict=True):
        if not any(cell.strip() for cell in record):
            skipped.append(SkippedRow(line, "boş satır"))
            continue
        if len(record) != len(header):
            errors.append(f"satır {line}: {len(record)} alan var, başlıkta {len(header)} sütun var")
            continue
        try:
            rows.append(_parse_row(record, index, line, zone, decimal_mark))
        except ValueError as exc:
            errors.append(f"satır {line}: {exc}")

    fills, open_trades, trade_skips = _assemble(rows, errors)
    skipped.extend(trade_skips)
    if errors:
        shown = errors[:MAX_REPORTED_ERRORS]
        if len(errors) > len(shown):
            shown.append(f"... ve {len(errors) - len(shown)} hata daha")
        raise ParityError(f"{name}: işlem listesi geçersiz\n" + "\n".join(shown))
    if not fills and not open_trades:
        raise ParityError(f"{name}: işlem yok (yalnızca başlık satırı var)")
    return TvTradeList(
        source=name,
        tz=tz,
        fills=tuple(sorted(fills, key=_tv_key)),
        open_trades=open_trades,
        skipped=tuple(sorted(skipped, key=lambda s: s.line)),
        columns=tuple((column.value, header_name) for column, (_, header_name) in mapping.items()),
        ignored_columns=tuple(ignored),
        delimiter=delimiter,
        decimal_mark=decimal_mark,
    )


def _sniff_delimiter(header_line: str, name: str) -> str:
    counts = dict.fromkeys(_DELIMITERS, 0)
    quoted = False
    for char in header_line:
        if char == '"':
            quoted = not quoted
        elif not quoted and char in counts:
            counts[char] += 1
    best = max(counts.values())
    winners = [delim for delim, count in counts.items() if count == best]
    if best == 0:
        raise ParityError(
            f"{name}: başlık satırında ayırıcı bulunamadı (virgül, noktalı virgül ya da sekme)"
        )
    if len(winners) > 1:
        found = ", ".join(_DELIMITER_NAMES[d] for d in winners)
        raise ParityError(f"{name}: ayırıcı belirsiz ({found} aynı sayıda)")
    return winners[0]


def _record_lines(text: str, delimiter: str) -> list[int]:
    reader = csv.reader(io.StringIO(text, newline=""), delimiter=delimiter, strict=True)
    lines: list[int] = []
    for _ in reader:
        lines.append(reader.line_num)
    return lines


def _normalise_header(name: str) -> str:
    return " ".join(name.replace("﻿", "").split()).casefold()


def _map_columns(
    header: Sequence[str], name: str
) -> tuple[dict[Column, tuple[int, str]], list[str]]:
    mapping: dict[Column, tuple[int, str]] = {}
    ignored: list[str] = []
    errors: list[str] = []
    for position, raw in enumerate(header):
        normal = _normalise_header(raw)
        found = _column_of(normal)
        if found is None:
            ignored.append(raw.strip())
        elif found in mapping:
            errors.append(
                f"'{mapping[found][1]}' ve '{raw.strip()}' aynı sütuna ({found.value}) "
                "karşılık geliyor"
            )
        else:
            mapping[found] = (position, raw.strip())
    if errors:
        raise ParityError(f"{name}: başlık belirsiz: " + "; ".join(errors))
    missing = [column for column in REQUIRED if column not in mapping]
    if missing:
        expected = "; ".join(
            f"{column.value}: "
            + " / ".join(repr(a) for a in COLUMN_ALIASES[column])
            + (" (+ para birimi, ör. 'Price USD')" if column in _SUFFIXED else "")
            for column in missing
        )
        found_names = ", ".join(repr(h.strip()) for h in header)
        raise ParityError(
            f"{name}: zorunlu sütun bulunamadı. Beklenen adlar (büyük/küçük harf fark etmez): "
            f"{expected}. Dosyadaki sütunlar: {found_names}"
        )
    return mapping, ignored


def _column_of(normal: str) -> Column | None:
    for column, aliases in COLUMN_ALIASES.items():
        suffix = _CURRENCY_SUFFIX if column in _SUFFIXED else ""
        for alias in aliases:
            if re.fullmatch(re.escape(alias) + suffix, normal):
                return column
    return None


def _clean_number(text: str) -> str:
    return text.strip().translate(_SPACE_GROUPING).translate(_MINUS_SIGNS)


def _decimal_mark(cells: Sequence[str], delimiter: str, name: str) -> str:
    """Decide the file's decimal mark once, from every price and size cell.

    Per value guessing would read "4,500" as 4.5 in one row and "4500,25" as 4500.25 in
    the next; one decision per file, refused when the cells disagree, cannot.
    """
    votes: set[str] = set()
    ambiguous = False
    for raw in cells:
        text = _clean_number(raw)
        dots, commas = text.count("."), text.count(",")
        if dots and commas:
            votes.add("." if text.rfind(".") > text.rfind(",") else ",")
        elif dots or commas:
            mark = "." if dots else ","
            if dots + commas > 1:
                votes.add("," if mark == "." else ".")  # repeated: a grouping mark
            elif len(text) - text.index(mark) - 1 != 3:
                votes.add(mark)
            else:
                ambiguous = True  # "4,500": thousands or decimals?
    if len(votes) > 1:
        raise ParityError(
            f"{name}: ondalık ayırıcı tutarsız (bazı sayılarda nokta, bazılarında virgül)"
        )
    if votes:
        mark = votes.pop()
    elif ambiguous and delimiter != ",":
        raise ParityError(
            f"{name}: ondalık ayırıcı belirlenemedi ('4,500' gibi değerler binlik de ondalık da "
            "olabilir); dışa aktarımı ondalıklı fiyatlarla ya da virgül ayırıcıyla alın"
        )
    else:
        mark = "."
    if mark == "," and delimiter == ",":
        raise ParityError(
            f"{name}: ondalık virgül yalnızca ayırıcı virgül değilken kabul edilir "
            "(noktalı virgül ya da sekme)"
        )
    return mark


def _parse_number(raw: str, decimal_mark: str, what: str) -> float:
    text = _clean_number(raw)
    group = "," if decimal_mark == "." else "."
    d, g = re.escape(decimal_mark), re.escape(group)
    pattern = rf"[+-]?(?:\d{{1,3}}(?:{g}\d{{3}})+|\d+)(?:{d}\d+)?"
    spaced = raw.strip().translate(_MINUS_SIGNS)
    if any(char in _SPACES for char in spaced):
        # Spaces are only a thousands grouping ("5 000,25"). Stripping them unchecked
        # would read "45 00,25" or "4500 ,25" as a price instead of refusing the cell.
        s = re.escape(_SPACES)
        if re.fullmatch(rf"[+-]?\d{{1,3}}(?:[{s}]\d{{3}})+(?:{d}\d+)?", spaced) is None:
            raise ValueError(f"{what} sayı olmalı; okunan: {raw.strip()!r}")
    if not text or re.fullmatch(pattern, text) is None:
        raise ValueError(f"{what} sayı olmalı; okunan: {raw.strip()!r}")
    value = float(text.replace(group, "").replace(decimal_mark, "."))
    if not math.isfinite(value):
        raise ValueError(f"{what} sonlu bir sayı olmalı; okunan: {raw.strip()!r}")
    return value


def _parse_time(raw: str, zone: ZoneInfo) -> int:
    text = raw.strip()
    match = _TIMESTAMP.fullmatch(text)
    if match is None:
        raise ValueError(
            "Date/Time 'YYYY-AA-GG SS:DD' (isteğe bağlı ':ss' ve 'T') biçiminde olmalı; "
            f"okunan: {text!r}"
        )
    try:
        moment = dt.datetime.fromisoformat(f"{match['date']}T{match['time']}")
    except ValueError:
        raise ValueError(f"geçersiz tarih-saat: {text!r}") from None
    if match["offset"] is not None:
        offset = match["offset"]
        parsed = dt.datetime.fromisoformat(
            moment.isoformat() + ("+00:00" if offset == "Z" else offset)
        )
        return _to_ms(parsed)
    first = moment.replace(tzinfo=zone, fold=0)
    second = moment.replace(tzinfo=zone, fold=1)
    if first.utcoffset() != second.utcoffset():
        # The repeated hour of the autumn change or the missing hour of the spring change:
        # guessing would shift a trade by an hour. A chart in UTC avoids both.
        raise ValueError(
            f"{text!r} {zone.key} diliminde yaz saati geçişinde belirsiz ya da var olmayan "
            "bir saat; grafiği UTC'ye alıp yeniden dışa aktarın"
        )
    return _to_ms(first)


def _parse_row(
    record: Sequence[str],
    index: Mapping[Column, int],
    line: int,
    zone: ZoneInfo,
    decimal_mark: str,
) -> _Row:
    trade_text = record[index[Column.TRADE_NO]].strip()
    if re.fullmatch(r"\d{1,9}", trade_text) is None:
        raise ValueError(f"'Trade #' pozitif tam sayı olmalı; okunan: {trade_text!r}")
    type_text = " ".join(record[index[Column.TYPE]].split()).casefold()
    kind = _TYPES.get(type_text)
    if kind is None:
        raise ValueError(
            "'Type' şunlardan biri olmalı: Entry Long, Exit Long, Entry Short, Exit Short; "
            f"okunan: {record[index[Column.TYPE]].strip()!r}"
        )
    is_entry, side = kind
    signal = record[index[Column.SIGNAL]].strip()
    time_text = record[index[Column.TIME]].strip()
    qty_text = record[index[Column.QTY]] if Column.QTY in index else ""
    if not is_entry and (not time_text or signal.casefold() == _OPEN_SIGNAL):
        return _Row(line, int(trade_text), False, side, signal, None, None, None)
    ts_ms = _parse_time(time_text, zone)
    price = _parse_number(record[index[Column.PRICE]], decimal_mark, "fiyat")
    if price <= 0.0:
        raise ValueError(f"fiyat pozitif olmalı; okunan: {record[index[Column.PRICE]]!r}")
    qty = _parse_number(qty_text, decimal_mark, "miktar") if qty_text.strip() else None
    if qty is not None and qty <= 0.0:
        raise ValueError(f"miktar pozitif olmalı; okunan: {qty_text.strip()!r}")
    return _Row(line, int(trade_text), is_entry, side, signal, ts_ms, price, qty)


def _assemble(
    rows: Sequence[_Row], errors: list[str]
) -> tuple[list[TvFill], int, list[SkippedRow]]:
    """Join each trade's entry row with its exit row(s); errors are appended in place.

    Two layouts of a partial exit are supported: one trade number with several exit rows,
    and one trade number per exit fill (each with a copy of the entry row).
    """
    by_trade: dict[int, list[_Row]] = {}
    for row in rows:
        by_trade.setdefault(row.trade_no, []).append(row)
    fills: list[TvFill] = []
    skipped: list[SkippedRow] = []
    open_trades = 0
    for trade_no in sorted(by_trade):
        group = by_trade[trade_no]
        entries = [row for row in group if row.is_entry]
        exits = [row for row in group if not row.is_entry]
        if len(entries) != 1:
            where = ", ".join(str(row.line) for row in group)
            what = "giriş satırı yok" if not entries else "birden fazla giriş satırı var"
            errors.append(f"işlem #{trade_no} (satır {where}): {what}")
            continue
        entry = entries[0]
        conflicting = [row for row in exits if row.side is not entry.side]
        if conflicting:
            errors.append(
                f"işlem #{trade_no} (satır {entry.line}, {conflicting[0].line}): giriş ve çıkış "
                "satırlarının yönü çelişiyor; reddedildi"
            )
            continue
        closed = [row for row in exits if row.ts_ms is not None]
        for row in exits:
            if row.ts_ms is None:
                skipped.append(
                    SkippedRow(row.line, f"işlem #{trade_no}: açık işlemin çıkış satırı")
                )
        if not closed:
            open_trades += 1
            skipped.append(SkippedRow(entry.line, f"işlem #{trade_no}: açık işlem (çıkış yok)"))
            continue
        if entry.ts_ms is None or entry.price is None:  # pragma: no cover - entries always parse
            continue
        for row in closed:
            if row.ts_ms is None or row.price is None:  # pragma: no cover - filtered above
                continue
            if row.ts_ms < entry.ts_ms:
                errors.append(f"satır {row.line}: işlem #{trade_no} çıkışı girişten önce")
                continue
            fills.append(
                TvFill(
                    trade_no=trade_no,
                    side=entry.side,
                    entry_ts_ms=entry.ts_ms,
                    entry_price=entry.price,
                    entry_signal=entry.signal,
                    exit_ts_ms=row.ts_ms,
                    exit_price=row.price,
                    exit_signal=row.signal,
                    qty=row.qty if row.qty is not None else entry.qty,
                    line=row.line,
                )
            )
    return fills, open_trades, skipped


# --- comparison --------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class MatchedPair:
    python: TradeRecord
    tradingview: TvFill
    reason_matched_by: str  # "comment" or "id"


@dataclass(frozen=True, slots=True)
class Mismatch:
    python: TradeRecord
    tradingview: TvFill
    fields: tuple[Field, ...]  # every differing field, in `Field` order


@dataclass(frozen=True, slots=True)
class Exclusions:
    python_end_of_data: int  # closed by the end of the series: not a strategy exit
    python_warmup: int
    tradingview_warmup: int
    python_outside_window: int
    tradingview_outside_window: int


@dataclass(frozen=True, slots=True)
class ParityResult:
    matched: tuple[MatchedPair, ...]
    mismatches: tuple[Mismatch, ...]
    python_only: tuple[TradeRecord, ...]
    tradingview_only: tuple[TvFill, ...]
    match_rate: float | None  # None: nothing was compared
    threshold: float
    passed: bool
    field_counts: Mapping[str, int]  # Field value -> mismatches in which it differs
    reason_matched_by: Mapping[str, int]  # "comment" / "id" -> matched pairs
    time_tolerance_pairs: int  # matched pairs whose entry times differ within tolerance
    exit_time_differs: int  # matched pairs with different exit times (not a criterion)
    qty_differs: int  # matched pairs with different sizes (not a criterion)
    window: tuple[int, int] | None  # compared entry-time span (UTC ms), None: not clipped
    warmup_cutoff_ms: int | None
    exclusions: Exclusions
    tick_size: float
    tolerance_ticks: float
    tf_seconds: int
    time_tolerance_bars: int
    pair_window_bars: int
    tv_source: str
    tv_tz: str
    tv_open_trades: int
    tv_skipped: tuple[SkippedRow, ...]
    sample_input: bool
    # Turkish reasons that keep `passed` False whatever the rate (e.g. too many rows left
    # out of the comparison); empty when only the rate decides.
    blocked_by: tuple[str, ...] = ()

    @property
    def compared(self) -> int:
        return (
            len(self.matched)
            + len(self.mismatches)
            + len(self.python_only)
            + len(self.tradingview_only)
        )


def warmup_cutoff_ms(bar_open_ms: Sequence[int], warmup_bars: int) -> int | None:
    """Open time of the first bar after the warm-up, for `compare_trades(warmup_cutoff_ms=)`.

    Bars are counted, not minutes: a session gap does not shorten the warm-up.
    """
    if warmup_bars < 0:
        raise ParityError("ısınma bar sayısı negatif olamaz")
    if warmup_bars == 0:
        return None
    if warmup_bars >= len(bar_open_ms):
        raise ParityError(
            f"ısınma ({warmup_bars} bar) serinin tamamını ({len(bar_open_ms)} bar) kapsıyor"
        )
    return bar_open_ms[warmup_bars]


def compare_trades(
    python: Sequence[TradeRecord],
    tradingview: TvTradeList | Sequence[TvFill],
    *,
    fill_model: FillModel,
    tick_size: float,
    tf_seconds: int,
    threshold: float = MIN_PARITY_RATE,
    tolerance_ticks: float = DEFAULT_TOLERANCE_TICKS,
    time_tolerance_bars: int = 0,
    pair_window_bars: int = DEFAULT_PAIR_WINDOW_BARS,
    warmup_cutoff_ms: int | None = None,
    clip_to_overlap: bool = True,
) -> ParityResult:
    """Compare the exit fills of a `tv_ohlc` Python run with TradingView's list.

    `threshold` comes from `config/backtest.yaml` (`min_parity_rate`); it can be stricter
    than SPEC §9's 98 % but never looser. Every excluded row is counted in the result.
    """
    if fill_model is not FillModel.TV_OHLC:
        raise ParityError(
            f"parite yalnızca '{FillModel.TV_OHLC.value}' dolum modeliyle ölçülür; verilen koşu "
            f"'{fill_model.value}' (kötümser model TradingView'dan bilerek farklı dolar, "
            "karşılaştırma anlamsız olurdu)"
        )
    _check_settings(
        tick_size, tf_seconds, threshold, tolerance_ticks, time_tolerance_bars, pair_window_bars
    )
    tv_list = (
        tradingview if isinstance(tradingview, TvTradeList) else TvTradeList.from_fills(tradingview)
    )
    _refuse_duplicate_python_rows(python)
    py_rows = sorted(python, key=_py_key)
    tv_rows = sorted(tv_list.fills, key=_tv_key)

    end_of_data = sum(1 for row in py_rows if row.reason is ExitReason.END_OF_DATA)
    py_rows = [row for row in py_rows if row.reason is not ExitReason.END_OF_DATA]
    py_before, tv_before = len(py_rows), len(tv_rows)
    py_warm = tv_warm = 0
    if warmup_cutoff_ms is not None:
        py_warm = sum(1 for row in py_rows if row.entry_ts_ms < warmup_cutoff_ms)
        tv_warm = sum(1 for row in tv_rows if row.entry_ts_ms < warmup_cutoff_ms)
        py_rows = [row for row in py_rows if row.entry_ts_ms >= warmup_cutoff_ms]
        tv_rows = [row for row in tv_rows if row.entry_ts_ms >= warmup_cutoff_ms]

    window: tuple[int, int] | None = None
    py_out = tv_out = 0
    if clip_to_overlap and py_rows and tv_rows:
        # Only the span both lists cover is compared: an export that starts later or ends
        # earlier than the bars would otherwise show up as one-sided rows. The span is
        # widened by the pairing window so a near-miss at either end is still paired.
        reach = pair_window_bars * tf_seconds * 1000
        start = max(py_rows[0].entry_ts_ms, min(row.entry_ts_ms for row in tv_rows)) - reach
        end = (
            min(max(row.entry_ts_ms for row in py_rows), max(row.entry_ts_ms for row in tv_rows))
            + reach
        )
        window = (start, end)
        inside_py = [row for row in py_rows if start <= row.entry_ts_ms <= end]
        inside_tv = [row for row in tv_rows if start <= row.entry_ts_ms <= end]
        py_out, tv_out = len(py_rows) - len(inside_py), len(tv_rows) - len(inside_tv)
        py_rows, tv_rows = inside_py, inside_tv

    bar_ms = tf_seconds * 1000
    rules = _Rules(
        price_tol=tolerance_ticks * tick_size + PRICE_EPSILON_TICKS * tick_size,
        time_tol_ms=time_tolerance_bars * bar_ms,
    )
    pairs = _pair(py_rows, tv_rows, rules, pair_window_bars * bar_ms)

    matched: list[MatchedPair] = []
    mismatches: list[Mismatch] = []
    used_py: set[int] = set()
    used_tv: set[int] = set()
    for py_index, tv_index, fields, by in pairs:
        used_py.add(py_index)
        used_tv.add(tv_index)
        py_row, tv_row = py_rows[py_index], tv_rows[tv_index]
        if fields:
            mismatches.append(Mismatch(py_row, tv_row, fields))
        else:
            matched.append(MatchedPair(py_row, tv_row, by))
    python_only = tuple(row for i, row in enumerate(py_rows) if i not in used_py)
    tradingview_only = tuple(row for i, row in enumerate(tv_rows) if i not in used_tv)

    total = len(matched) + len(mismatches) + len(python_only) + len(tradingview_only)
    rate = len(matched) / total if total else None
    blocked_by = _excluded_share_blocks(
        (("Python", py_warm + py_out, py_before), ("TradingView", tv_warm + tv_out, tv_before))
    )
    field_counts = Counter(field.value for m in mismatches for field in m.fields)
    return ParityResult(
        matched=tuple(sorted(matched, key=lambda p: _py_key(p.python))),
        mismatches=tuple(sorted(mismatches, key=lambda m: _py_key(m.python))),
        python_only=python_only,
        tradingview_only=tradingview_only,
        match_rate=rate,
        threshold=threshold,
        passed=rate is not None and rate >= threshold and not blocked_by,
        field_counts=MappingProxyType({f.value: field_counts.get(f.value, 0) for f in Field}),
        reason_matched_by=MappingProxyType(
            {
                "comment": sum(1 for p in matched if p.reason_matched_by == "comment"),
                "id": sum(1 for p in matched if p.reason_matched_by == "id"),
            }
        ),
        time_tolerance_pairs=sum(
            1 for p in matched if p.python.entry_ts_ms != p.tradingview.entry_ts_ms
        ),
        exit_time_differs=sum(
            1 for p in matched if p.python.exit_ts_ms != p.tradingview.exit_ts_ms
        ),
        qty_differs=sum(
            1
            for p in matched
            if p.tradingview.qty is not None
            and not math.isclose(p.python.qty, p.tradingview.qty, rel_tol=1e-9, abs_tol=1e-9)
        ),
        window=window,
        warmup_cutoff_ms=warmup_cutoff_ms,
        exclusions=Exclusions(
            python_end_of_data=end_of_data,
            python_warmup=py_warm,
            tradingview_warmup=tv_warm,
            python_outside_window=py_out,
            tradingview_outside_window=tv_out,
        ),
        tick_size=tick_size,
        tolerance_ticks=tolerance_ticks,
        tf_seconds=tf_seconds,
        time_tolerance_bars=time_tolerance_bars,
        pair_window_bars=pair_window_bars,
        tv_source=tv_list.source,
        tv_tz=tv_list.tz,
        tv_open_trades=tv_list.open_trades,
        tv_skipped=tv_list.skipped,
        sample_input=tv_list.sample,
        blocked_by=blocked_by,
    )


def _refuse_duplicate_python_rows(python: Sequence[TradeRecord]) -> None:
    # The engine never emits one exit fill twice (rows of an entry differ in exit order,
    # time or size). A repeated row means lists were concatenated, and it would pair with a
    # repeated TradingView row and inflate the matched count.
    seen: set[TradeRecord] = set()
    for row in python:
        if row in seen:
            raise ParityError(
                f"Python işlem listesinde aynı satır iki kez var (seq {row.seq}, giriş "
                f"{_ts(row.entry_ts_ms)}); liste iki kez eklenmiş olabilir"
            )
        seen.add(row)


def _excluded_share_blocks(sides: Iterable[tuple[str, int, int]]) -> tuple[str, ...]:
    blocks: list[str] = []
    for name, excluded, before in sides:
        if before and excluded / before > MAX_EXCLUDED_SHARE:
            blocks.append(
                f"{name} listesinin {excluded}/{before} satırı (%{excluded / before * 100:.2f}) "
                f"ısınma ya da ortak pencere dışında kaldı; sınır %{MAX_EXCLUDED_SHARE * 100:.2f}. "
                "Dışarıda kalan satırlar uyuşmazlık gizleyebilir: iki tarafı aynı bar aralığında "
                "üretin"
            )
    return tuple(blocks)


def _check_settings(
    tick_size: float,
    tf_seconds: int,
    threshold: float,
    tolerance_ticks: float,
    time_tolerance_bars: int,
    pair_window_bars: int,
) -> None:
    if not (math.isfinite(tick_size) and tick_size > 0.0):
        raise ParityError("tick büyüklüğü pozitif sonlu bir sayı olmalı")
    if tf_seconds < 1:
        raise ParityError("bar süresi en az 1 saniye olmalı")
    if not (math.isfinite(threshold) and threshold <= 1.0):
        raise ParityError("parite eşiği 0-1 arasında olmalı")
    if threshold < MIN_PARITY_RATE:
        raise ParityError(
            f"parite eşiği {threshold:.4f}, SPEC §9 tabanı {MIN_PARITY_RATE:.2f}'nin altında; "
            "eşik yalnızca sıkılaştırılabilir"
        )
    if not (math.isfinite(tolerance_ticks) and tolerance_ticks >= 0.0):
        raise ParityError("fiyat toleransı (tick) negatif olamaz")
    if tolerance_ticks > MAX_TOLERANCE_TICKS:
        raise ParityError(
            f"fiyat toleransı {_num(tolerance_ticks)} tick, SPEC §6.6 sınırı "
            f"{_num(MAX_TOLERANCE_TICKS)} tick; tolerans yalnızca daraltılabilir"
        )
    if time_tolerance_bars < 0:
        raise ParityError("zaman toleransı (bar) negatif olamaz")
    if time_tolerance_bars > MAX_TIME_TOLERANCE_BARS:
        raise ParityError(
            f"giriş zamanı toleransı {time_tolerance_bars} bar, sınır "
            f"{MAX_TIME_TOLERANCE_BARS} bar (SPEC §6.6 giriş zamanı eşleşmesi ister)"
        )
    if pair_window_bars < time_tolerance_bars:
        raise ParityError("eşleştirme penceresi zaman toleransından küçük olamaz")


@dataclass(frozen=True, slots=True)
class _Rules:
    price_tol: float
    time_tol_ms: int


def _labels(row: TradeRecord) -> tuple[tuple[str, str], ...]:
    """The Python row's candidate exit labels, the expected one (comment) first."""
    labels = []
    if row.comment.strip():
        labels.append(("comment", _norm(row.comment)))
    if row.exit_id.strip():
        labels.append(("id", _norm(row.exit_id)))
    return tuple(labels)


def _norm(text: str) -> str:
    return text.strip().casefold()


def _differences(
    py_row: TradeRecord, tv_row: TvFill, rules: _Rules
) -> tuple[tuple[Field, ...], str]:
    fields: list[Field] = []
    if abs(py_row.entry_ts_ms - tv_row.entry_ts_ms) > rules.time_tol_ms:
        fields.append(Field.ENTRY_TIME)
    if py_row.side is not tv_row.side:
        fields.append(Field.SIDE)
    signal = _norm(tv_row.exit_signal)
    by = next((kind for kind, label in _labels(py_row) if label == signal), "")
    if not by:
        fields.append(Field.EXIT_REASON)
    if abs(py_row.entry_price - tv_row.entry_price) > rules.price_tol:
        fields.append(Field.ENTRY_PRICE)
    if abs(py_row.exit_price - tv_row.exit_price) > rules.price_tol:
        fields.append(Field.EXIT_PRICE)
    return tuple(fields), by


def _pair(
    py_rows: Sequence[TradeRecord],
    tv_rows: Sequence[TvFill],
    rules: _Rules,
    window_ms: int,
) -> list[tuple[int, int, tuple[Field, ...], str]]:
    """Greedy assignment over candidate pairs, best candidates first (see module doc)."""
    tv_by_entry = sorted(range(len(tv_rows)), key=lambda i: (tv_rows[i].entry_ts_ms, i))
    tv_times = [tv_rows[i].entry_ts_ms for i in tv_by_entry]
    candidates: list[tuple[tuple[int, int, int, int, int, float, int, int], str, tuple[Field, ...]]]
    candidates = []
    for p, py_row in enumerate(py_rows):
        low = _bisect_left(tv_times, py_row.entry_ts_ms - window_ms)
        for position in range(low, len(tv_by_entry)):
            t = tv_by_entry[position]
            tv_row = tv_rows[t]
            if tv_row.entry_ts_ms > py_row.entry_ts_ms + window_ms:
                break
            fields, by = _differences(py_row, tv_row, rules)
            rank = (
                len(fields),
                int(py_row.side is not tv_row.side),
                abs(py_row.entry_ts_ms - tv_row.entry_ts_ms),
                abs(py_row.exit_ts_ms - tv_row.exit_ts_ms),
                int(by == "" and bool(fields)),
                abs(py_row.entry_price - tv_row.entry_price)
                + abs(py_row.exit_price - tv_row.exit_price),
                p,
                t,
            )
            candidates.append((rank, by, fields))
    candidates.sort(key=lambda c: c[0])
    used_py: set[int] = set()
    used_tv: set[int] = set()
    pairs: list[tuple[int, int, tuple[Field, ...], str]] = []
    for rank, by, fields in candidates:
        p, t = rank[6], rank[7]
        if p in used_py or t in used_tv:
            continue
        used_py.add(p)
        used_tv.add(t)
        pairs.append((p, t, fields, by))
    return pairs


def _bisect_left(values: Sequence[int], target: int) -> int:
    low, high = 0, len(values)
    while low < high:
        mid = (low + high) // 2
        if values[mid] < target:
            low = mid + 1
        else:
            high = mid
    return low


def _py_key(row: TradeRecord) -> tuple[object, ...]:
    # Every field: equal keys mean equal rows, so the input order cannot leak into the result.
    return (
        row.entry_ts_ms,
        row.exit_ts_ms,
        row.side.value,
        row.entry_price,
        row.exit_price,
        row.seq,
        row.strategy_id,
        row.entry_id,
        row.exit_id,
        row.reason.value,
        row.setup,
        row.comment,
        row.qty,
        row.entry_bar_index,
        row.exit_bar_index,
        row.gross_pnl,
        row.commission,
        row.pnl,
        row.initial_stop,
        row.risk_amount,
        row.r,
        row.mae_r,
    )


def _tv_key(row: TvFill) -> tuple[object, ...]:
    return (
        row.entry_ts_ms,
        row.exit_ts_ms,
        row.side.value,
        row.entry_price,
        row.exit_price,
        row.trade_no,
        row.line,
        row.entry_signal,
        row.exit_signal,
        -1.0 if row.qty is None else row.qty,
    )


# --- report ------------------------------------------------------------------------------


def expected_deviation_notes(strategy_id: str | None) -> tuple[str, ...]:
    """The expected-deviation sentences of a strategy, each under the heading; () for none."""
    if strategy_id is None:
        return ()
    return tuple(
        f"{EXPECTED_DEVIATION_HEADING}: {sentence}"
        for sentence in EXPECTED_DEVIATIONS.get(strategy_id, ())
    )


def render_parity_report(
    result: ParityResult,
    *,
    max_rows: int = DEFAULT_REPORT_ROWS,
    strategy_id: str | None = None,
) -> str:
    """Turkish plain-text report: verdict, counts, the first differences, the reminders.

    `strategy_id` adds the strategy's expected deviations (`EXPECTED_DEVIATIONS`), if any.
    """
    lines: list[str] = []
    if result.match_rate is None:
        verdict = "GEÇMEDİ: karşılaştırılacak işlem yok (boş karşılaştırma geçemez)"
    else:
        word = "GEÇTİ" if result.passed else "GEÇMEDİ"
        verdict = (
            f"{word}: eşleşme %{result.match_rate * 100:.2f} (eşik %{result.threshold * 100:.2f})"
        )
    lines.append(f"Parite (SPEC §6.6) — {verdict}")
    lines.extend(f"GEÇEMEZ: {block}" for block in result.blocked_by)
    if result.sample_input:
        lines.append(f"UYARI: {SAMPLE_NOTE}")
    lines.append("")
    lines.append(f"TradingView dosyası: {result.tv_source} (grafik saat dilimi {result.tv_tz})")
    lines.append(
        f"Tolerans: fiyat {_num(result.tolerance_ticks)} tick (tick {_num(result.tick_size)}), "
        f"giriş zamanı {result.time_tolerance_bars} bar, eşleştirme penceresi "
        f"{result.pair_window_bars} bar (bar {result.tf_seconds} sn)"
    )
    if result.window is not None:
        lines.append(
            f"Karşılaştırma penceresi (giriş zamanı, UTC): {_ts(result.window[0])} — "
            f"{_ts(result.window[1])}"
        )
    lines.append("")
    lines.append(f"Eşleşen: {len(result.matched)}")
    lines.append(f"Uyuşmayan: {len(result.mismatches)}")
    lines.append(f"Yalnızca Python: {len(result.python_only)}")
    lines.append(f"Yalnızca TradingView: {len(result.tradingview_only)}")
    lines.append(f"Toplam karşılaştırılan: {result.compared}")
    if result.mismatches:
        worst = sorted(
            ((count, name) for name, count in result.field_counts.items() if count),
            key=lambda item: (-item[0], item[1]),
        )
        parts = ", ".join(f"{FIELD_LABELS[Field(name)]} {count}" for count, name in worst)
        lines.append(f"Alan bazında uyuşmazlık: {parts}")
    by = result.reason_matched_by
    lines.append(
        f"Çıkış nedeni eşleşmesi: yorumla {by['comment']}, emir kimliğiyle {by['id']} "
        "(hangisini TradingView'ın yazdığı doğrulanmadı)"
    )
    lines.append(f"Zaman toleransıyla eşleşen: {result.time_tolerance_pairs}")
    lines.append(
        f"Bilgi (ölçüt değil): çıkış zamanı farklı {result.exit_time_differs}, "
        f"miktar farklı {result.qty_differs}"
    )
    ex = result.exclusions
    lines.append(
        "Dışarıda bırakılan (sayılarak): Python veri sonu kapanışı "
        f"{ex.python_end_of_data}; ısınma Python {ex.python_warmup} / TradingView "
        f"{ex.tradingview_warmup}; pencere dışı Python {ex.python_outside_window} / "
        f"TradingView {ex.tradingview_outside_window}; TradingView açık işlem "
        f"{result.tv_open_trades}; atlanan satır {len(result.tv_skipped)}"
    )
    if result.warmup_cutoff_ms is not None:
        lines.append(f"Isınma sınırı (UTC): {_ts(result.warmup_cutoff_ms)}")

    rows = _difference_rows(result)
    if rows:
        lines.append("")
        shown = rows[:max_rows]
        lines.append(f"İlk {len(shown)} fark (toplam {len(rows)}):")
        header = (
            "tür",
            "alanlar",
            "taraf",
            "giriş (UTC)",
            "yön",
            "giriş",
            "çıkış (UTC)",
            "çıkış",
            "neden",
        )
        table: list[tuple[str, ...]] = [header]
        for kind, fields, py_row, tv_row in shown:
            label = ", ".join(FIELD_LABELS[f] for f in fields) or "-"
            if py_row is not None:
                table.append((kind, label, "Python", *_py_cells(py_row)))
                kind, label = "", ""
            if tv_row is not None:
                table.append((kind, label, "TV", *_tv_cells(tv_row)))
        widths = [max(len(row[i]) for row in table) for i in range(len(header))]
        for row in table:
            lines.append(
                "  ".join(
                    cell.ljust(width) for cell, width in zip(row, widths, strict=True)
                ).rstrip()
            )
    deviations = () if strategy_id is None else EXPECTED_DEVIATIONS.get(strategy_id, ())
    if deviations:
        lines.append("")
        lines.append(f"{EXPECTED_DEVIATION_HEADING} ({strategy_id}):")
        lines.extend(f"- {sentence}" for sentence in deviations)
    lines.append("")
    lines.append("Hatırlatmalar:")
    lines.extend(f"- {note}" for note in UNVERIFIED_NOTES)
    return "\n".join(lines) + "\n"


def _difference_rows(
    result: ParityResult,
) -> list[tuple[str, tuple[Field, ...], TradeRecord | None, TvFill | None]]:
    rows: list[tuple[int, str, tuple[Field, ...], TradeRecord | None, TvFill | None]] = []
    for m in result.mismatches:
        rows.append((m.python.entry_ts_ms, "uyuşmaz", m.fields, m.python, m.tradingview))
    for py_row in result.python_only:
        rows.append((py_row.entry_ts_ms, "yalnız Python", (), py_row, None))
    for tv_row in result.tradingview_only:
        rows.append((tv_row.entry_ts_ms, "yalnız TV", (), None, tv_row))
    rows.sort(key=lambda r: (r[0], r[1]))
    return [(kind, fields, py, tv) for _, kind, fields, py, tv in rows]


def _py_cells(row: TradeRecord) -> tuple[str, ...]:
    reason = row.comment or row.exit_id or "-"
    if row.comment and row.exit_id and row.comment != row.exit_id:
        reason = f"{row.comment} / {row.exit_id}"
    return (
        _ts(row.entry_ts_ms),
        _side(row.side),
        _num(row.entry_price),
        _ts(row.exit_ts_ms),
        _num(row.exit_price),
        reason,
    )


def _tv_cells(row: TvFill) -> tuple[str, ...]:
    return (
        _ts(row.entry_ts_ms),
        _side(row.side),
        _num(row.entry_price),
        _ts(row.exit_ts_ms),
        _num(row.exit_price),
        row.exit_signal or "-",
    )


def _side(side: Side) -> str:
    return "long" if side is Side.LONG else "short"


def _num(value: float) -> str:
    return f"{value:.6f}".rstrip("0").rstrip(".")


def _ts(ts_ms: int) -> str:
    moment = _EPOCH + dt.timedelta(milliseconds=ts_ms)
    if ts_ms % _MINUTE_MS:
        return moment.strftime("%Y-%m-%d %H:%M:%S")
    return moment.strftime("%Y-%m-%d %H:%M")


def parity_artifact(
    result: ParityResult,
    *,
    label: str,
    created: dt.datetime,
    strategy_id: str,
    data: BarSeries | DataInfo | None = None,
    synthetic: bool = False,
    python_source: str | None = None,
    notes: Iterable[str] = (),
    params: Mapping[str, Scalar] | None = None,
    instrument: Instrument | None = None,
    min_gap_seconds: int | None = None,
    initial_capital: float | None = None,
) -> ParityArtifact:
    """The JSON artifact for `reports/`. Sample inputs make it synthetic (never evidence).

    `params`, `instrument`, `min_gap_seconds` and `initial_capital` are the Python side's:
    Go/No-Go G6 accepts a report only when they equal today's configured ones (left at
    None they are recorded as unknown).
    """
    rows = _difference_rows(result)
    mismatches: list[dict[str, JsonValue]] = []
    for kind, fields, py_row, tv_row in rows[:MAX_PARITY_MISMATCHES]:
        mismatches.append(
            {
                "kind": {"uyuşmaz": "mismatch", "yalnız Python": "python_only"}.get(
                    kind, "tradingview_only"
                ),
                "fields": [f.value for f in fields],
                "python": None if py_row is None else _py_json(py_row),
                "tradingview": None if tv_row is None else _tv_json(tv_row),
            }
        )
    counts = (
        f"Eşleşen {len(result.matched)}, uyuşmayan {len(result.mismatches)}, yalnızca Python "
        f"{len(result.python_only)}, yalnızca TradingView {len(result.tradingview_only)}."
    )
    extra = [
        counts,
        *(f"Geçemez: {block}" for block in result.blocked_by),
        *expected_deviation_notes(strategy_id),
        *UNVERIFIED_NOTES,
    ]
    if len(rows) > MAX_PARITY_MISMATCHES:
        extra.append(
            f"Fark listesi ilk {MAX_PARITY_MISMATCHES} satırla kısaltıldı (toplam {len(rows)})."
        )
    if result.sample_input:
        extra.insert(0, SAMPLE_NOTE)
    sources = tuple(s for s in (result.tv_source, python_source) if s)
    return build_artifact(
        ParityArtifact,
        label=label,
        created=created,
        synthetic=synthetic or result.sample_input,
        data=data,
        notes=(*notes, *extra),
        strategy_id=strategy_id,
        matched=len(result.matched),
        mismatched=len(result.mismatches),
        python_only=len(result.python_only),
        tradingview_only=len(result.tradingview_only),
        match_rate=result.match_rate,
        threshold=result.threshold,
        passed=result.passed,
        mismatches=tuple(mismatches),
        sources=sources,
        params=None if params is None else dict(params),
        instrument=None
        if instrument is None
        else InstrumentInfo(
            symbol=instrument.symbol,
            kind=instrument.kind.value,
            tick_size=instrument.tick_size,
            point_value=instrument.point_value,
            qty_step=instrument.qty_step,
            min_qty=instrument.min_qty,
        ),
        min_gap_seconds=min_gap_seconds,
        initial_capital=initial_capital,
    )


def _py_json(row: TradeRecord) -> dict[str, JsonValue]:
    return {
        "entry_time_utc": _iso(row.entry_ts_ms),
        "exit_time_utc": _iso(row.exit_ts_ms),
        "side": row.side.value,
        "entry_price": row.entry_price,
        "exit_price": row.exit_price,
        "exit_comment": row.comment,
        "exit_id": row.exit_id,
        "qty": row.qty,
    }


def _tv_json(row: TvFill) -> dict[str, JsonValue]:
    return {
        "trade_no": row.trade_no,
        "line": row.line,
        "entry_time_utc": _iso(row.entry_ts_ms),
        "exit_time_utc": _iso(row.exit_ts_ms),
        "side": row.side.value,
        "entry_price": row.entry_price,
        "exit_price": row.exit_price,
        "signal": row.exit_signal,
        "qty": row.qty,
    }


# --- golden files ------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class GoldenRow:
    """The comparable fields of one `TradeRecord`, numbers rounded to GOLDEN_DECIMALS."""

    seq: int
    strategy_id: str
    entry_id: str
    exit_id: str
    reason: ExitReason
    setup: str
    comment: str
    side: Side
    qty: float
    entry_ts_ms: int
    exit_ts_ms: int
    entry_price: float
    exit_price: float
    pnl: float
    r: float


def golden_rows(trades: Iterable[TradeRecord]) -> tuple[GoldenRow, ...]:
    return tuple(
        GoldenRow(
            seq=t.seq,
            strategy_id=t.strategy_id,
            entry_id=t.entry_id,
            exit_id=t.exit_id,
            reason=t.reason,
            setup=t.setup,
            comment=t.comment,
            side=t.side,
            qty=_round(t.qty),
            entry_ts_ms=t.entry_ts_ms,
            exit_ts_ms=t.exit_ts_ms,
            entry_price=_round(t.entry_price),
            exit_price=_round(t.exit_price),
            pnl=_round(t.pnl),
            r=_round(t.r),
        )
        for t in trades
    )


def write_golden(trades: Iterable[TradeRecord], path: Path, *, header_note: str) -> None:
    """Pin the Python engine's own trade list. NOT TradingView evidence (ADR-0007 item 4)."""
    note = " ".join(header_note.split())
    if not note:
        raise ParityError("altın dosya notu boş olamaz (hangi koşunun sabitlendiği yazılmalı)")
    buffer = io.StringIO(newline="")
    buffer.write(f"{GOLDEN_MARKER} {note}\n")
    writer = csv.writer(buffer, lineterminator="\n")
    writer.writerow(GOLDEN_COLUMNS)
    for row in golden_rows(trades):
        writer.writerow(
            (
                row.seq,
                row.strategy_id,
                row.entry_id,
                row.exit_id,
                row.reason.value,
                row.setup,
                row.comment,
                row.side.value,
                _fixed(row.qty),
                _iso(row.entry_ts_ms),
                _iso(row.exit_ts_ms),
                _fixed(row.entry_price),
                _fixed(row.exit_price),
                _fixed(row.pnl),
                _fixed(row.r),
            )
        )
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(buffer.getvalue(), encoding="utf-8", newline="")


def read_golden(path: Path) -> tuple[GoldenRow, ...]:
    name = path.name
    text = _read_text(path, MAX_GOLDEN_BYTES, "altın dosya")
    first, _, rest = text.partition("\n")
    if not first.startswith(GOLDEN_MARKER):
        raise ParityError(f"{name}: ilk satır altın dosya işareti değil ({GOLDEN_MARKER!r})")
    reader = csv.reader(io.StringIO(rest, newline=""), strict=True)
    try:
        header = next(reader, None)
        if header is None or tuple(header) != GOLDEN_COLUMNS:
            raise ParityError(f"{name}: başlık beklenen değil ({','.join(GOLDEN_COLUMNS)})")
        rows: list[GoldenRow] = []
        errors: list[str] = []
        for record in reader:
            line = reader.line_num + 1  # + the marker line
            try:
                rows.append(_golden_row(record))
            except ValueError as exc:
                errors.append(f"satır {line}: {exc}")
    except csv.Error as exc:
        raise ParityError(f"{name}: CSV okunamadı: {exc}") from None
    if errors:
        shown = errors[:MAX_REPORTED_ERRORS]
        if len(errors) > len(shown):
            shown.append(f"... ve {len(errors) - len(shown)} hata daha")
        raise ParityError(f"{name}: altın dosya geçersiz\n" + "\n".join(shown))
    return tuple(rows)


def _golden_row(record: Sequence[str]) -> GoldenRow:
    if len(record) != len(GOLDEN_COLUMNS):
        raise ValueError(f"{len(record)} alan var, {len(GOLDEN_COLUMNS)} bekleniyor")
    fields = dict(zip(GOLDEN_COLUMNS, record, strict=True))
    try:
        reason = ExitReason(fields["reason"])
        side = Side(fields["side"])
    except ValueError:
        raise ValueError(f"geçersiz reason/side: {fields['reason']!r}/{fields['side']!r}") from None
    if re.fullmatch(r"\d+", fields["seq"]) is None:
        raise ValueError(f"seq tam sayı olmalı: {fields['seq']!r}")
    return GoldenRow(
        seq=int(fields["seq"]),
        strategy_id=fields["strategy_id"],
        entry_id=fields["entry_id"],
        exit_id=fields["exit_id"],
        reason=reason,
        setup=fields["setup"],
        comment=fields["comment"],
        side=side,
        qty=_golden_number(fields["qty"], "qty"),
        entry_ts_ms=_from_iso(fields["entry_time_utc"]),
        exit_ts_ms=_from_iso(fields["exit_time_utc"]),
        entry_price=_golden_number(fields["entry_price"], "entry_price"),
        exit_price=_golden_number(fields["exit_price"], "exit_price"),
        pnl=_golden_number(fields["pnl"], "pnl"),
        r=_golden_number(fields["r"], "r"),
    )


def diff_golden(
    expected: Sequence[GoldenRow],
    actual: Sequence[GoldenRow | TradeRecord],
    *,
    tick_size: float,
) -> list[str]:
    """Readable differences between a pinned list and a new run; empty when identical."""
    if not (math.isfinite(tick_size) and tick_size > 0.0):
        raise ParityError("tick büyüklüğü pozitif sonlu bir sayı olmalı")
    current = tuple(row if isinstance(row, GoldenRow) else golden_rows((row,))[0] for row in actual)
    out: list[str] = []
    if len(expected) != len(current):
        out.append(f"satır sayısı farklı: beklenen {len(expected)}, gelen {len(current)}")
    for number, (want, got) in enumerate(zip(expected, current, strict=False), start=1):
        for name in GoldenRow.__slots__:
            a, b = getattr(want, name), getattr(got, name)
            if a == b:
                continue
            if name in ("entry_price", "exit_price"):
                ticks = (b - a) / tick_size
                out.append(
                    f"satır {number} ({want.entry_id} {_iso(want.entry_ts_ms)}): {name} "
                    f"beklenen {_fixed(a)}, gelen {_fixed(b)} ({ticks:+.2f} tick)"
                )
            elif name in ("entry_ts_ms", "exit_ts_ms"):
                out.append(
                    f"satır {number} ({want.entry_id}): {name.removesuffix('_ts_ms')} zamanı "
                    f"beklenen {_iso(a)}, gelen {_iso(b)}"
                )
            else:
                shown_a = _fixed(a) if isinstance(a, float) else str(a)
                shown_b = _fixed(b) if isinstance(b, float) else str(b)
                out.append(
                    f"satır {number} ({want.entry_id} {_iso(want.entry_ts_ms)}): {name} "
                    f"beklenen {shown_a!r}, gelen {shown_b!r}"
                )
    for number, row in enumerate(expected[len(current) :], start=len(current) + 1):
        out.append(f"satır {number}: beklenen işlem yok ({row.entry_id} {_iso(row.entry_ts_ms)})")
    for number, row in enumerate(current[len(expected) :], start=len(expected) + 1):
        out.append(f"satır {number}: fazladan işlem ({row.entry_id} {_iso(row.entry_ts_ms)})")
    return out


def _round(value: float) -> float:
    return round(value, GOLDEN_DECIMALS)


def _fixed(value: float) -> str:
    text = f"{value:.{GOLDEN_DECIMALS}f}"
    return "0.000000" if text == "-0.000000" else text


def _golden_number(text: str, what: str) -> float:
    if re.fullmatch(rf"-?\d+\.\d{{{GOLDEN_DECIMALS}}}", text) is None:
        raise ValueError(f"{what} {GOLDEN_DECIMALS} ondalıklı sayı olmalı: {text!r}")
    return float(text)


def _iso(ts_ms: int) -> str:
    moment = _EPOCH + dt.timedelta(milliseconds=ts_ms)
    return moment.strftime("%Y-%m-%dT%H:%M:%S.") + f"{ts_ms % 1000:03d}Z"


def _from_iso(text: str) -> int:
    if re.fullmatch(r"\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}\.\d{3}Z", text) is None:
        raise ValueError(f"zaman ISO UTC olmalı (ör. 2026-03-02T14:30:00.000Z): {text!r}")
    try:
        return _to_ms(dt.datetime.fromisoformat(text.replace("Z", "+00:00")))
    except ValueError:
        raise ValueError(f"geçersiz zaman: {text!r}") from None


# --- helpers -----------------------------------------------------------------------------


def _to_ms(moment: dt.datetime) -> int:
    delta = moment - _EPOCH
    return (delta.days * 86_400 + delta.seconds) * 1000 + delta.microseconds // 1000


def _zone(tz: str) -> ZoneInfo:
    try:
        return ZoneInfo(tz)
    except (ZoneInfoNotFoundError, ValueError):
        raise ParityError(f"bilinmeyen saat dilimi: {tz!r} (IANA adı olmalı)") from None


def _read_text(path: Path, limit: int, what: str) -> str:
    name = path.name
    too_large = f"{name}: {what} çok büyük (sınır {limit} bayt)"
    # O_NONBLOCK: a FIFO must not hang the reader; no effect on regular files.
    flags = os.O_RDONLY | getattr(os, "O_NONBLOCK", 0)
    try:
        fd = os.open(path, flags)
    except FileNotFoundError:
        raise ParityError(f"{name}: dosya bulunamadı") from None
    except OSError as exc:
        raise ParityError(f"{name}: dosya açılamadı ({exc.strerror})") from None
    try:
        info = os.fstat(fd)
        if not stat.S_ISREG(info.st_mode):
            raise ParityError(f"{name}: normal bir dosya değil")
        if info.st_size > limit:
            raise ParityError(too_large)
        with os.fdopen(os.dup(fd), "rb") as handle:
            raw = handle.read(limit + 1)
    except OSError as exc:
        raise ParityError(f"{name}: dosya okunamadı ({exc.strerror})") from None
    finally:
        os.close(fd)
    if len(raw) > limit:
        raise ParityError(too_large)
    try:
        return raw.decode("utf-8-sig")
    except UnicodeDecodeError as exc:
        raise ParityError(
            f"{name}: UTF-8 değil (bayt {exc.start}); dışa aktarımı UTF-8 olarak kaydedin"
        ) from None
