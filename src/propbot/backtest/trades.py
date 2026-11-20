"""Trade list in R multiples: CSV loading, validation and day blocks for the bootstrap.

Generic CSV format (documented in docs/monte-carlo.md):

    entry_time,exit_time,r[,strategy_id][,mae_r]

Timestamps are ISO 8601 with a UTC offset or `Z`; `r` is the trade result in R
multiples; `mae_r` is the worst floating result during the trade (<= 0 and <= r).

A list written by `propbot backtest` starts with ONE provenance line before the header
(`TradesProvenance`): which run, data and strategies it came from. The Go/No-Go report
(G4) accepts a Monte Carlo run only on a list with that line; a hand-made list without it
still simulates. The line has a fixed key order and a canonical encoding, and the reader
re-encodes what it parsed and compares: anything that is not exactly what the writer
would produce (a reordered key, `1.00` for `1.0`, a lower-case escape) is an error, never
"no provenance".
"""

import csv
import datetime as dt
import math
import re
import urllib.parse
from collections import defaultdict
from collections.abc import Sequence
from dataclasses import dataclass
from enum import StrEnum
from pathlib import Path
from typing import Final
from zoneinfo import ZoneInfo

import numpy as np
import numpy.typing as npt

from propbot.backtest.model import FillModel
from propbot.core.propday import prop_day, weekdays_between

REQUIRED_COLUMNS: Final = ("entry_time", "exit_time", "r")
OPTIONAL_COLUMNS: Final = ("strategy_id", "mae_r")

EVENT_PAD: Final = 0
EVENT_ENTRY: Final = 1
EVENT_EXIT: Final = 2

_MAX_REPORTED_ERRORS: Final = 20
_MAE_TOLERANCE: Final = 1e-9
_TIMESTAMP = re.compile(r"\d{4}-\d{2}-\d{2}[T ]\d{2}:\d{2}")

PROVENANCE_PREFIX: Final = "#propbot-trades"
PROVENANCE_VERSION: Final = "1"
# Fixed order: the encoding is canonical, so a reordered line is a tampered line.
PROVENANCE_KEYS: Final = (
    "v",
    "synthetic",
    "cost_free",
    "fill_model",
    "cost_factor",
    "data_sha256",
    "data_file",
    "strategy_ids",
    "created_utc",
)
# A real line is a few hundred bytes; the cap keeps a garbage first line from being read whole.
MAX_PROVENANCE_LINE: Final = 16 * 1024
MAX_PROVENANCE_TEXT: Final = 256
_BOOL_TEXT: Final = {True: "true", False: "false"}
_SHA256 = re.compile(r"[0-9a-f]{64}")
_STAMP = re.compile(r"\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}Z")
_FACTOR = re.compile(r"[0-9]+\.[0-9]+(?:e[+-][0-9]+)?|[0-9]+e[+-][0-9]+")
# Percent-encoded UTF-8: unreserved characters stay, everything else (space, comma, "=",
# "%", newline, non-ASCII) is %XX with upper-case hex, so a value never holds a separator.
_ENCODED = re.compile(r"(?:[A-Za-z0-9._~-]|%[0-9A-F]{2})+")


class TradeListError(Exception):
    """The trade list cannot be used. The message is user-facing (Turkish)."""


class IntratradeModel(StrEnum):
    """How deep a trade is assumed to float against the account before it closes."""

    STOP = "stop"  # pessimistic: down to the stop (-1R), or the file's mae_r when given
    CLOSED = "closed"  # optimistic: only closed results move the equity


@dataclass(frozen=True, slots=True)
class TradesProvenance:
    """Where a trade list came from: the backtest run that wrote it.

    `data_sha256` is "" for a series built in memory (synthetic data has no file).
    """

    synthetic: bool
    cost_free: bool
    fill_model: str  # a `FillModel` value
    cost_factor: float  # costs of the run = configured costs x this factor
    data_sha256: str
    data_file: str
    strategy_ids: tuple[str, ...]  # sorted, unique, non-empty
    created_utc: str  # YYYY-MM-DDTHH:MM:SSZ

    def __post_init__(self) -> None:
        for name in ("synthetic", "cost_free"):
            if not isinstance(getattr(self, name), bool):
                raise ValueError(f"{name} true ya da false olmalı")
        if self.fill_model not in {model.value for model in FillModel}:
            raise ValueError(f"bilinmeyen dolum modeli: {self.fill_model[:32]!r}")
        if (
            isinstance(self.cost_factor, bool)
            or not isinstance(self.cost_factor, float)
            or not (math.isfinite(self.cost_factor) and self.cost_factor >= 0.0)
        ):
            raise ValueError("cost_factor sonlu, negatif olmayan bir ondalık sayı olmalı")
        if self.data_sha256 and _SHA256.fullmatch(self.data_sha256) is None:
            raise ValueError("data_sha256 64 küçük onaltılık karakter ya da boş olmalı")
        _check_text("data_file", self.data_file)
        if not isinstance(self.strategy_ids, tuple) or not self.strategy_ids:
            raise ValueError("strategy_ids boş olmayan bir demet olmalı")
        for sid in self.strategy_ids:
            _check_text("strategy_id", sid)
            if sid != sid.strip():
                # The CSV reader strips every field: such an id could never match its rows.
                raise ValueError("strategy_id başında ya da sonunda boşluk olamaz")
        if list(self.strategy_ids) != sorted(set(self.strategy_ids)):
            raise ValueError("strategy_ids sıralı ve tekrarsız olmalı")
        if _STAMP.fullmatch(self.created_utc) is None:
            raise ValueError("created_utc 2026-11-18T14:30:00Z biçiminde olmalı")
        try:
            dt.datetime.fromisoformat(self.created_utc)  # a real date and time
        except ValueError:
            raise ValueError("created_utc geçersiz tarih-saat") from None

    def to_line(self) -> str:
        """The single provenance line, without a line terminator."""
        values = (
            PROVENANCE_VERSION,
            _BOOL_TEXT[self.synthetic],
            _BOOL_TEXT[self.cost_free],
            self.fill_model,
            repr(self.cost_factor),
            self.data_sha256,
            _encode(self.data_file),
            ",".join(_encode(sid) for sid in self.strategy_ids),
            self.created_utc,
        )
        pairs = " ".join(
            f"{key}={value}" for key, value in zip(PROVENANCE_KEYS, values, strict=True)
        )
        return f"{PROVENANCE_PREFIX} {pairs}"

    @classmethod
    def from_line(cls, line: str) -> "TradesProvenance":
        """Strict parse of `to_line`'s output; anything else raises ValueError."""
        tokens = line.split(" ")
        if tokens[0] != PROVENANCE_PREFIX or len(tokens) != len(PROVENANCE_KEYS) + 1:
            raise ValueError(f"{len(PROVENANCE_KEYS)} alan bekleniyordu")
        raw: dict[str, str] = {}
        for key, token in zip(PROVENANCE_KEYS, tokens[1:], strict=True):
            name, sep, value = token.partition("=")
            if name != key or not sep:
                raise ValueError(f"beklenen alan {key!r}, okunan {name[:32]!r}")
            raw[key] = value
        if raw["v"] != PROVENANCE_VERSION:
            raise ValueError(f"bilinmeyen sürüm {raw['v'][:16]!r}")
        flags = {text: flag for flag, text in _BOOL_TEXT.items()}
        if raw["synthetic"] not in flags or raw["cost_free"] not in flags:
            raise ValueError("synthetic ve cost_free 'true' ya da 'false' olmalı")
        if _FACTOR.fullmatch(raw["cost_factor"]) is None:
            raise ValueError("cost_factor sayı olmalı (ör. 1.0)")
        if raw["data_sha256"] and _SHA256.fullmatch(raw["data_sha256"]) is None:
            raise ValueError("data_sha256 64 küçük onaltılık karakter olmalı")
        if not raw["strategy_ids"]:
            raise ValueError("strategy_ids boş olamaz")
        provenance = cls(
            synthetic=flags[raw["synthetic"]],
            cost_free=flags[raw["cost_free"]],
            fill_model=raw["fill_model"],
            cost_factor=float(raw["cost_factor"]),
            data_sha256=raw["data_sha256"],
            data_file=_decode(raw["data_file"]),
            strategy_ids=tuple(_decode(part) for part in raw["strategy_ids"].split(",")),
            created_utc=raw["created_utc"],
        )
        # Canonical form: only the writer's exact bytes are accepted (no second spelling
        # of the same line that a hand edit could hide behind).
        if provenance.to_line() != line:
            raise ValueError("satır kanonik biçimde değil (elle düzenlenmiş olabilir)")
        return provenance


def _check_text(name: str, value: str) -> None:
    if not isinstance(value, str) or not 1 <= len(value) <= MAX_PROVENANCE_TEXT:
        raise ValueError(f"{name} 1-{MAX_PROVENANCE_TEXT} karakter olmalı")
    try:
        value.encode("utf-8")
    except UnicodeEncodeError as exc:
        raise ValueError(f"{name} geçerli UTF-8 metin olmalı") from exc


def _encode(value: str) -> str:
    return urllib.parse.quote(value, safe="", encoding="utf-8", errors="strict")


def _decode(value: str) -> str:
    if _ENCODED.fullmatch(value) is None:
        raise ValueError("geçersiz yüzde kodlaması")
    try:
        return urllib.parse.unquote_to_bytes(value).decode("utf-8")
    except UnicodeDecodeError as exc:
        raise ValueError("yüzde kodlaması UTF-8 değil") from exc


@dataclass(frozen=True, slots=True)
class Trade:
    entry_utc: dt.datetime
    exit_utc: dt.datetime
    r: float
    strategy_id: str | None = None
    mae_r: float | None = None


def load_trades_csv(path: Path, *, naive_tz: ZoneInfo | None = None) -> tuple[Trade, ...]:
    """Read a trade list. Timestamps without an offset are read in `naive_tz` if given."""
    return read_trades_csv(path, naive_tz=naive_tz)[0]


def read_trades_csv(
    path: Path, *, naive_tz: ZoneInfo | None = None
) -> tuple[tuple[Trade, ...], TradesProvenance | None]:
    """The trades and the provenance line (None when the file has none).

    Only the FIRST line may be a provenance line; a malformed one is an error. Any other
    line starting with "#" is read as CSV like before (as a header it lacks the required
    columns, as a row it is not a timestamp), so it stays an error.
    """
    provenance: TradesProvenance | None = None
    try:
        with path.open(encoding="utf-8-sig", newline="") as handle:
            first = handle.readline(MAX_PROVENANCE_LINE + 1)
            skipped = 0
            if first.startswith(PROVENANCE_PREFIX):
                provenance = _parse_provenance(first, path)
                skipped = 1
            else:
                handle.seek(0)
            reader = csv.DictReader(handle)
            _check_header(reader.fieldnames, path)
            trades: list[Trade] = []
            errors: list[str] = []
            for row in reader:
                try:
                    trade = _parse_row(row, naive_tz)
                    if provenance is not None and trade.strategy_id not in provenance.strategy_ids:
                        raise ValueError(
                            f"strategy_id {trade.strategy_id!r} köken satırındaki stratejiler "
                            "arasında değil"
                        )
                    trades.append(trade)
                except ValueError as exc:
                    errors.append(f"satır {reader.line_num + skipped}: {exc}")
    except FileNotFoundError as exc:
        raise TradeListError(f"{path}: dosya bulunamadı") from exc
    except (OSError, UnicodeDecodeError, csv.Error) as exc:
        raise TradeListError(f"{path}: dosya okunamadı: {exc}") from exc

    if errors:
        shown = errors[:_MAX_REPORTED_ERRORS]
        if len(errors) > len(shown):
            shown.append(f"... ve {len(errors) - len(shown)} hata daha")
        raise TradeListError(f"{path}: işlem listesi geçersiz\n" + "\n".join(shown))
    if not trades:
        raise TradeListError(f"{path}: işlem yok (yalnızca başlık satırı var)")
    if provenance is not None:
        # The line and the rows must agree both ways: a strategy the line names without a
        # single row means rows were removed (or the line names a run it does not hold).
        present = {trade.strategy_id for trade in trades}
        absent = [sid for sid in provenance.strategy_ids if sid not in present]
        if absent:
            raise TradeListError(
                f"{path}: köken satırındaki stratejinin listede işlemi yok: "
                + ", ".join(repr(sid) for sid in absent[:_MAX_REPORTED_ERRORS])
            )
    return tuple(trades), provenance


def _parse_provenance(first: str, path: Path) -> TradesProvenance:
    if first.endswith("\r\n"):
        line = first[:-2]
    elif first.endswith("\n"):
        line = first[:-1]
    else:
        # Too long for the cap, or the file has nothing after it.
        raise TradeListError(f"{path}: köken satırı (satır 1) bozuk: satır sonu yok ya da çok uzun")
    try:
        return TradesProvenance.from_line(line)
    except ValueError as exc:
        raise TradeListError(f"{path}: köken satırı (satır 1) bozuk: {exc}") from None


def _check_header(fieldnames: Sequence[str] | None, path: Path) -> None:
    if not fieldnames:
        raise TradeListError(f"{path}: dosya boş (başlık satırı yok)")
    duplicates = sorted({name for name in fieldnames if fieldnames.count(name) > 1})
    if duplicates:
        raise TradeListError(f"{path}: yinelenen sütun: {', '.join(duplicates)}")
    missing = [name for name in REQUIRED_COLUMNS if name not in fieldnames]
    if missing:
        raise TradeListError(
            f"{path}: zorunlu sütun eksik: {', '.join(missing)} "
            f"(beklenen başlık: {','.join(REQUIRED_COLUMNS)} ve isteğe bağlı "
            f"{','.join(OPTIONAL_COLUMNS)})"
        )
    known = {*REQUIRED_COLUMNS, *OPTIONAL_COLUMNS}
    unknown = [name for name in fieldnames if name not in known]
    if unknown:
        raise TradeListError(
            f"{path}: bilinmeyen sütun: {', '.join(unknown)} "
            f"(izin verilenler: {', '.join(sorted(known))})"
        )


def _parse_row(row: dict[str | None, str | list[str] | None], naive_tz: ZoneInfo | None) -> Trade:
    if None in row:
        raise ValueError("başlıktan fazla alan var (ondalık ayırıcı nokta olmalı, virgül değil)")
    fields: dict[str, str] = {}
    for name, value in row.items():
        if not isinstance(name, str) or not isinstance(value, str):
            raise ValueError("başlıktan az alan var")
        fields[name] = value.strip()

    entry = _parse_timestamp(fields["entry_time"], "entry_time", naive_tz)
    exit_ = _parse_timestamp(fields["exit_time"], "exit_time", naive_tz)
    if exit_ < entry:
        raise ValueError("exit_time, entry_time'dan önce")

    r = _parse_number(fields["r"], "r")
    mae_text = fields.get("mae_r", "")
    mae_r: float | None = None
    if mae_text:
        mae_r = _parse_number(mae_text, "mae_r")
        if mae_r > _MAE_TOLERANCE:
            raise ValueError(f"mae_r sıfırdan büyük olamaz; okunan: {mae_text}")
        if mae_r > r + _MAE_TOLERANCE:
            raise ValueError(
                f"mae_r ({mae_text}) işlem sonucundan ({fields['r']}) iyi olamaz: "
                "en kötü yüzen sonuç kapanıştan kötü ya da ona eşit olmalı"
            )
    return Trade(
        entry_utc=entry,
        exit_utc=exit_,
        r=r,
        strategy_id=fields.get("strategy_id") or None,
        mae_r=mae_r,
    )


def _parse_timestamp(text: str, column: str, naive_tz: ZoneInfo | None) -> dt.datetime:
    if _TIMESTAMP.match(text) is None:
        raise ValueError(
            f"{column} ISO 8601 tarih-saat olmalı (ör. 2026-01-05T14:36:00Z); okunan: {text!r}"
        )
    try:
        moment = dt.datetime.fromisoformat(text)
    except ValueError as exc:
        raise ValueError(f"{column} okunamadı: {text!r}") from exc
    if moment.utcoffset() is None:
        if naive_tz is None:
            raise ValueError(
                f"{column} saat dilimi içermiyor ({text!r}); UTC ofseti ekle "
                "(ör. Z ya da -05:00) ya da --trades-tz ver"
            )
        moment = moment.replace(tzinfo=naive_tz)
    return moment.astimezone(dt.UTC)


def _parse_number(text: str, column: str) -> float:
    try:
        value = float(text)
    except ValueError as exc:
        raise ValueError(f"{column} sayı olmalı (ondalık ayırıcı nokta); okunan: {text!r}") from exc
    if not math.isfinite(value):
        raise ValueError(f"{column} sonlu bir sayı olmalı; okunan: {text!r}")
    return value


@dataclass(frozen=True, slots=True)
class DayBlocks:
    """Bootstrap population: one row per prop day, padded to the busiest day.

    Trade arrays are (n_days, max_trades); event arrays are (n_days, 2 * max_trades)
    and list each day's entries and exits in time order. Padding is zero.
    """

    days: tuple[dt.date, ...]
    r: npt.NDArray[np.float64]  # result per trade in R, after the cost assumption
    mae: npt.NDArray[np.float64]  # worst floating result per trade in R (<= 0)
    event_kind: npt.NDArray[np.int8]  # EVENT_PAD / EVENT_ENTRY / EVENT_EXIT
    event_trade: npt.NDArray[np.intp]  # trade slot the event belongs to
    trade_count: npt.NDArray[np.intp]
    trades_crossing_reset: int

    @property
    def n_days(self) -> int:
        return len(self.days)

    @property
    def max_trades(self) -> int:
        return int(self.r.shape[1])


def build_day_blocks(
    trades: Sequence[Trade],
    *,
    zone: ZoneInfo,
    reset: dt.time,
    cost_r: float = 0.0,
    intratrade: IntratradeModel = IntratradeModel.STOP,
) -> DayBlocks:
    """Group trades by the prop day of their entry and lay them out as arrays.

    The population holds every weekday between the first and the last trade (days
    without trades included, so the trade frequency survives resampling) plus any
    other day that has a trade. `cost_r` is subtracted from every trade.
    """
    if not trades:
        raise TradeListError("işlem listesi boş")
    if not (math.isfinite(cost_r) and cost_r >= 0.0):
        raise ValueError("cost_r must be a finite, non-negative number")

    by_day: defaultdict[dt.date, list[Trade]] = defaultdict(list)
    crossing = 0
    for trade in trades:
        day = prop_day(trade.entry_utc, zone, reset)
        by_day[day].append(trade)
        if prop_day(trade.exit_utc, zone, reset) != day:
            crossing += 1

    days = sorted({*weekdays_between(min(by_day), max(by_day)), *by_day})
    max_trades = max(len(day_trades) for day_trades in by_day.values())
    shape = (len(days), max_trades)
    r = np.zeros(shape, dtype=np.float64)
    mae = np.zeros(shape, dtype=np.float64)
    event_kind = np.zeros((len(days), 2 * max_trades), dtype=np.int8)
    event_trade = np.zeros((len(days), 2 * max_trades), dtype=np.intp)
    trade_count = np.zeros(len(days), dtype=np.intp)

    for row, day in enumerate(days):
        day_trades = sorted(by_day.get(day, []), key=lambda t: (t.entry_utc, t.exit_utc))
        trade_count[row] = len(day_trades)
        events: list[tuple[dt.datetime, int, int]] = []
        for slot, trade in enumerate(day_trades):
            net = trade.r - cost_r
            r[row, slot] = net
            mae[row, slot] = _worst_floating(trade, net, intratrade)
            events.append((trade.entry_utc, EVENT_ENTRY, slot))
            events.append((trade.exit_utc, EVENT_EXIT, slot))
        # At equal timestamps entries sort before exits: trades are treated as overlapping.
        events.sort()
        for position, (_moment, kind, slot) in enumerate(events):
            event_kind[row, position] = kind
            event_trade[row, position] = slot

    return DayBlocks(
        days=tuple(days),
        r=r,
        mae=mae,
        event_kind=event_kind,
        event_trade=event_trade,
        trade_count=trade_count,
        trades_crossing_reset=crossing,
    )


def _worst_floating(trade: Trade, net_r: float, intratrade: IntratradeModel) -> float:
    if intratrade is IntratradeModel.CLOSED:
        return 0.0
    assumed = trade.mae_r if trade.mae_r is not None else -1.0
    return min(assumed, net_r, 0.0)


@dataclass(frozen=True, slots=True)
class TradeStats:
    """Summary of the resampled population, in R after the cost assumption."""

    n_trades: int
    n_days: int
    n_days_with_trades: int
    first_day: dt.date
    last_day: dt.date
    mean_r: float
    std_r: float
    win_rate: float
    t_stat: float


def summarize_trades(blocks: DayBlocks) -> TradeStats:
    taken = np.arange(blocks.max_trades)[None, :] < blocks.trade_count[:, None]
    results = blocks.r[taken]
    n_trades = int(results.size)
    mean_r = float(results.mean())
    std_r = float(results.std(ddof=1)) if n_trades > 1 else 0.0
    t_stat = mean_r / (std_r / math.sqrt(n_trades)) if std_r > 0.0 else math.nan
    return TradeStats(
        n_trades=n_trades,
        n_days=blocks.n_days,
        n_days_with_trades=int((blocks.trade_count > 0).sum()),
        first_day=blocks.days[0],
        last_day=blocks.days[-1],
        mean_r=mean_r,
        std_r=std_r,
        win_rate=float((results > 0.0).mean()),
        t_stat=t_stat,
    )
