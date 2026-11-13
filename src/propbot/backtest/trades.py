"""Trade list in R multiples: CSV loading, validation and day blocks for the bootstrap.

Generic CSV format (documented in docs/monte-carlo.md):

    entry_time,exit_time,r[,strategy_id][,mae_r]

Timestamps are ISO 8601 with a UTC offset or `Z`; `r` is the trade result in R
multiples; `mae_r` is the worst floating result during the trade (<= 0 and <= r).
"""

import csv
import datetime as dt
import math
import re
from collections import defaultdict
from collections.abc import Sequence
from dataclasses import dataclass
from enum import StrEnum
from pathlib import Path
from typing import Final
from zoneinfo import ZoneInfo

import numpy as np
import numpy.typing as npt

from propbot.core.propday import prop_day, weekdays_between

REQUIRED_COLUMNS: Final = ("entry_time", "exit_time", "r")
OPTIONAL_COLUMNS: Final = ("strategy_id", "mae_r")

EVENT_PAD: Final = 0
EVENT_ENTRY: Final = 1
EVENT_EXIT: Final = 2

_MAX_REPORTED_ERRORS: Final = 20
_MAE_TOLERANCE: Final = 1e-9
_TIMESTAMP = re.compile(r"\d{4}-\d{2}-\d{2}[T ]\d{2}:\d{2}")


class TradeListError(Exception):
    """The trade list cannot be used. The message is user-facing (Turkish)."""


class IntratradeModel(StrEnum):
    """How deep a trade is assumed to float against the account before it closes."""

    STOP = "stop"  # pessimistic: down to the stop (-1R), or the file's mae_r when given
    CLOSED = "closed"  # optimistic: only closed results move the equity


@dataclass(frozen=True, slots=True)
class Trade:
    entry_utc: dt.datetime
    exit_utc: dt.datetime
    r: float
    strategy_id: str | None = None
    mae_r: float | None = None


def load_trades_csv(path: Path, *, naive_tz: ZoneInfo | None = None) -> tuple[Trade, ...]:
    """Read a trade list. Timestamps without an offset are read in `naive_tz` if given."""
    try:
        with path.open(encoding="utf-8-sig", newline="") as handle:
            reader = csv.DictReader(handle)
            _check_header(reader.fieldnames, path)
            trades: list[Trade] = []
            errors: list[str] = []
            for row in reader:
                try:
                    trades.append(_parse_row(row, naive_tz))
                except ValueError as exc:
                    errors.append(f"satır {reader.line_num}: {exc}")
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
    return tuple(trades)


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
