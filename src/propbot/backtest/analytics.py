"""Statistics over the entries of a backtest (SPEC §6.4).

Input unit is `EntryResult` (ADR-0008 item 5): every exit of one entry added up. An
entry the data closed (`complete=False`, the series ended with it open) did not end by
the strategy's own rules, so it is left out of every statistic and only counted.

Win / loss / breakeven is the sign of the net P&L (after costs). R is the entry's net
R. Time tables use the New York wall clock of the ENTRY, like the Pine statistics
tables (`dayofweek(strategy.closedtrades.entry_time(k), tz)`), and the IS/OOS split is
Pine's `isOOS = time >= oosStart` applied to the entry time.
"""

import csv
import datetime as dt
import math
import statistics
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Final

from propbot.backtest.model import EntryResult
from propbot.backtest.report import tr_int, tr_number, tr_percent, tr_r
from propbot.backtest.trades import OPTIONAL_COLUMNS, REQUIRED_COLUMNS, Trade, TradesProvenance
from propbot.core.session import NEW_YORK, LocalClock

# The dashboard draws at most this many points of an equity curve.
DEFAULT_CURVE_POINTS: Final = 500
# First, last, and the peak and trough of both the R and the money curve.
MIN_CURVE_POINTS: Final = 6

WEEKDAY_KEYS: Final = ("Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun")
WEEKDAY_LABELS: Final = ("Pazartesi", "Salı", "Çarşamba", "Perşembe", "Cuma", "Cumartesi", "Pazar")
_WORKDAYS: Final = 5

_EPOCH: Final = dt.datetime(1970, 1, 1, tzinfo=dt.UTC)
_CSV_COLUMNS: Final = (*REQUIRED_COLUMNS, *OPTIONAL_COLUMNS)


class AnalyticsError(ValueError):
    """The statistics cannot be produced from this input. The message is user-facing (Turkish)."""


@dataclass(frozen=True, slots=True)
class TradeStats:
    """Summary of one sample of complete entries. None where a value is undefined (no NaN)."""

    n: int
    wins: int
    losses: int
    breakeven: int
    win_rate: float | None  # wins / n
    mean_r: float | None  # expectancy in R
    median_r: float | None
    stdev_r: float | None  # sample standard deviation; None when n < 2
    t_stat: float | None  # mean / (stdev / sqrt(n)); None when n < 2 or stdev == 0
    profit_factor: float | None  # winning pnl / |losing pnl|; None when there is no loss
    total_r: float
    total_pnl: float
    avg_win_r: float | None
    avg_loss_r: float | None
    max_drawdown_r: float  # largest fall of cumulative R from a running peak (start = 0)
    max_drawdown_pct: float  # same on initial_capital + cumulative pnl, % of the peak
    longest_losing_streak: int  # consecutive losses in exit order; breakeven ends a streak
    incomplete: int  # entries left out because the data ended with them open


@dataclass(frozen=True, slots=True)
class SampleSplit:
    oos_start_ms: int
    all: TradeStats
    in_sample: TradeStats
    out_of_sample: TradeStats
    g3_ratio: float | None  # OOS expectancy / IS expectancy (SPEC §9 G3)
    g3_note: str | None  # why the ratio is None (Turkish)


@dataclass(frozen=True, slots=True)
class TableRow:
    key: str  # "Mon", "09", "2026-01"
    label: str  # Turkish display text
    trades: int
    wins: int
    win_rate: float | None
    total_r: float
    mean_r: float | None
    total_pnl: float


@dataclass(frozen=True, slots=True)
class TimeTables:
    weekday: tuple[TableRow, ...]
    hour: tuple[TableRow, ...]
    month: tuple[TableRow, ...]


@dataclass(frozen=True, slots=True)
class EquityPoint:
    exit_ts_ms: int
    cum_r: float
    cum_pnl: float


def complete_entries(entries: Sequence[EntryResult]) -> tuple[EntryResult, ...]:
    return tuple(entry for entry in entries if entry.complete)


def exit_order(entries: Sequence[EntryResult]) -> tuple[EntryResult, ...]:
    """Complete entries in the order their last exit filled (ties by entry number)."""
    return tuple(sorted(complete_entries(entries), key=lambda e: (e.exit_ts_ms, e.seq)))


def trade_stats(entries: Sequence[EntryResult], *, initial_capital: float) -> TradeStats:
    _check_capital(initial_capital)
    ordered = exit_order(entries)
    incomplete = len(entries) - len(ordered)
    n = len(ordered)
    rs = [entry.r for entry in ordered]
    win_rs = [entry.r for entry in ordered if entry.pnl > 0.0]
    loss_rs = [entry.r for entry in ordered if entry.pnl < 0.0]
    win_pnl = math.fsum(entry.pnl for entry in ordered if entry.pnl > 0.0)
    loss_pnl = math.fsum(entry.pnl for entry in ordered if entry.pnl < 0.0)

    # statistics.mean/stdev are exact (fractions): identical values give a stdev of exactly 0.
    mean_r = float(statistics.mean(rs)) if rs else None
    stdev_r = float(statistics.stdev(rs)) if n >= 2 else None
    t_stat: float | None = None
    if mean_r is not None and stdev_r is not None and stdev_r > 0.0:
        t_stat = mean_r / (stdev_r / math.sqrt(n))

    return TradeStats(
        n=n,
        wins=len(win_rs),
        losses=len(loss_rs),
        breakeven=n - len(win_rs) - len(loss_rs),
        win_rate=len(win_rs) / n if n else None,
        mean_r=mean_r,
        median_r=float(statistics.median(rs)) if rs else None,
        stdev_r=stdev_r,
        t_stat=t_stat,
        profit_factor=win_pnl / -loss_pnl if loss_rs else None,
        total_r=math.fsum(rs),
        total_pnl=math.fsum(entry.pnl for entry in ordered),
        avg_win_r=float(statistics.mean(win_rs)) if win_rs else None,
        avg_loss_r=float(statistics.mean(loss_rs)) if loss_rs else None,
        max_drawdown_r=_max_drawdown(rs, start=0.0, as_percent=False),
        max_drawdown_pct=_max_drawdown(
            [entry.pnl for entry in ordered], start=initial_capital, as_percent=True
        ),
        longest_losing_streak=_longest_losing_streak(ordered),
        incomplete=incomplete,
    )


def split_is_oos(
    entries: Sequence[EntryResult], oos_start_ms: int, *, initial_capital: float
) -> SampleSplit:
    """Pine `isOOS = time >= oosStart` on the entry time; each sample starts from the capital."""
    in_sample = [entry for entry in entries if entry.entry_ts_ms < oos_start_ms]
    out_of_sample = [entry for entry in entries if entry.entry_ts_ms >= oos_start_ms]
    is_stats = trade_stats(in_sample, initial_capital=initial_capital)
    oos_stats = trade_stats(out_of_sample, initial_capital=initial_capital)
    ratio, note = expectancy_ratio(oos_stats.mean_r, is_stats.mean_r)
    return SampleSplit(
        oos_start_ms=oos_start_ms,
        all=trade_stats(entries, initial_capital=initial_capital),
        in_sample=is_stats,
        out_of_sample=oos_stats,
        g3_ratio=ratio,
        g3_note=note,
    )


def expectancy_ratio(
    oos_mean_r: float | None, is_mean_r: float | None
) -> tuple[float | None, str | None]:
    """OOS / IS expectancy, or None with the reason when the ratio means nothing."""
    if is_mean_r is None:
        return None, "IS örnekleminde tamamlanmış işlem yok: oran hesaplanamaz"
    if is_mean_r <= 0.0:
        # A negative denominator would turn a worse OOS into a "better" ratio.
        return None, "IS beklentisi sıfır ya da negatif: oran anlamsız"
    if oos_mean_r is None:
        return None, "OOS örnekleminde tamamlanmış işlem yok: oran hesaplanamaz"
    return oos_mean_r / is_mean_r, None


def time_tables(entries: Sequence[EntryResult], *, tz: str = NEW_YORK) -> TimeTables:
    clock = LocalClock(tz)
    complete = complete_entries(entries)
    by_weekday: dict[int, list[EntryResult]] = {day: [] for day in range(_WORKDAYS)}
    by_hour: dict[int, list[EntryResult]] = {}
    by_month: dict[tuple[int, int], list[EntryResult]] = {}
    for entry in complete:
        parts = clock.parts(entry.entry_ts_ms)
        by_weekday.setdefault(parts.weekday, []).append(entry)
        by_hour.setdefault(parts.hour, []).append(entry)
        by_month.setdefault((parts.year, parts.month), []).append(entry)

    weekday = tuple(
        _row(WEEKDAY_KEYS[day], WEEKDAY_LABELS[day], by_weekday[day]) for day in sorted(by_weekday)
    )
    hour = tuple(_row(f"{h:02d}", f"{h:02d}:00", by_hour[h]) for h in sorted(by_hour))
    month = tuple(
        _row(f"{year:04d}-{mon:02d}", f"{year:04d}-{mon:02d}", by_month.get((year, mon), []))
        for year, mon in _months_between(by_month)
    )
    return TimeTables(weekday=weekday, hour=hour, month=month)


def equity_curve(entries: Sequence[EntryResult]) -> tuple[EquityPoint, ...]:
    points: list[EquityPoint] = []
    cum_r = 0.0
    cum_pnl = 0.0
    for entry in exit_order(entries):
        cum_r += entry.r
        cum_pnl += entry.pnl
        points.append(EquityPoint(entry.exit_ts_ms, cum_r, cum_pnl))
    return tuple(points)


def downsample_curve(
    points: Sequence[EquityPoint], max_points: int = DEFAULT_CURVE_POINTS
) -> tuple[EquityPoint, ...]:
    """At most `max_points` points; first, last, peak and trough (R and money) always kept."""
    if max_points < MIN_CURVE_POINTS:
        raise AnalyticsError(f"eğri en az {MIN_CURVE_POINTS} noktaya indirgenebilir")
    n = len(points)
    if n <= max_points:
        return tuple(points)
    keep = {
        0,
        n - 1,
        _first_index(points, max, lambda p: p.cum_r),
        _first_index(points, min, lambda p: p.cum_r),
        _first_index(points, max, lambda p: p.cum_pnl),
        _first_index(points, min, lambda p: p.cum_pnl),
    }
    spare = max_points - len(keep)
    keep.update(round(i * (n - 1) / (spare + 1)) for i in range(1, spare + 1))
    return tuple(points[i] for i in sorted(keep))


def to_mc_trades(entries: Sequence[EntryResult]) -> tuple[Trade, ...]:
    """Complete entries as the Monte Carlo simulator's trade list, in entry order."""
    return tuple(
        Trade(
            entry_utc=_utc(entry.entry_ts_ms),
            exit_utc=_utc(entry.exit_ts_ms),
            r=entry.r,
            strategy_id=entry.strategy_id or None,
            mae_r=entry.mae_r,
        )
        for entry in sorted(complete_entries(entries), key=lambda e: e.seq)
    )


def write_trades_csv(
    entries: Sequence[EntryResult], path: Path, *, provenance: TradesProvenance | None = None
) -> int:
    """Write the trade-list CSV `propbot montecarlo --trades` reads; returns the row count.

    With `provenance` the file starts with its "#propbot-trades" line (docs/monte-carlo.md):
    the Go/No-Go report trusts a Monte Carlo run only on a list that names its backtest.
    """
    trades = to_mc_trades(entries)
    if not trades:
        # The loader refuses a header-only file; refuse here so the error names the cause.
        raise AnalyticsError("işlem listesi yazılamadı: tamamlanmış işlem yok")
    if provenance is not None:
        # The reader refuses a row whose strategy the line does not name; refuse it here
        # first, so a wrong provenance never reaches the disk.
        strays = sorted({t.strategy_id or "(boş)" for t in trades} - set(provenance.strategy_ids))
        if strays:
            raise AnalyticsError(
                "işlem listesi yazılamadı: köken satırında olmayan strateji: " + ", ".join(strays)
            )
        absent = sorted(set(provenance.strategy_ids) - {t.strategy_id for t in trades})
        if absent:
            # The reader refuses this too: a named strategy without a row looks like a list
            # with rows removed.
            raise AnalyticsError(
                "işlem listesi yazılamadı: köken satırındaki stratejinin tamamlanmış işlemi yok: "
                + ", ".join(absent)
            )
    with path.open("w", encoding="utf-8", newline="") as handle:
        if provenance is not None:
            handle.write(provenance.to_line() + "\n")
        writer = csv.writer(handle, lineterminator="\n")
        writer.writerow(_CSV_COLUMNS)
        for trade in trades:
            writer.writerow(
                (
                    _iso(trade.entry_utc),
                    _iso(trade.exit_utc),
                    repr(trade.r),
                    trade.strategy_id or "",
                    "" if trade.mae_r is None else repr(trade.mae_r),
                )
            )
    return len(trades)


def render_stats_text(split: SampleSplit, *, title: str) -> str:
    """Turkish text block: all / IS / OOS columns in the style of `backtest.report`."""
    columns = (split.all, split.in_sample, split.out_of_sample)
    oos_day = _utc(split.oos_start_ms).isoformat().replace("+00:00", "Z")

    def row(label: str, cell: Callable[[TradeStats], str]) -> str:
        return f"  {label:<26}" + "".join(f"{cell(stats):>14}" for stats in columns)

    lines = [
        title,
        "=" * 70,
        f"OOS başlangıcı: {oos_day} (giriş anına göre; giriş = sınır → OOS)",
        f"  {'':<26}{'tümü':>14}{'IS':>14}{'OOS':>14}",
        row("işlem", lambda s: tr_int(s.n)),
        row("kazanan / kaybeden / başa baş", lambda s: f"{s.wins}/{s.losses}/{s.breakeven}"),
        row("kazanma oranı", lambda s: _opt(s.win_rate, tr_percent)),
        row("beklenti (ortalama R)", lambda s: _opt(s.mean_r, tr_r)),
        row("medyan R", lambda s: _opt(s.median_r, tr_r)),
        row("standart sapma (R)", lambda s: _opt(s.stdev_r, lambda v: tr_number(v, 3))),
        row("t-istatistiği", lambda s: _opt(s.t_stat, lambda v: tr_number(v, 2))),
        row("profit factor", lambda s: _opt(s.profit_factor, lambda v: tr_number(v, 2))),
        row("toplam R", lambda s: tr_r(s.total_r, 2)),
        row("toplam K/Z", lambda s: tr_number(s.total_pnl, 2)),
        row("ortalama kazanç", lambda s: _opt(s.avg_win_r, tr_r)),
        row("ortalama kayıp", lambda s: _opt(s.avg_loss_r, tr_r)),
        row("maks. düşüş (R)", lambda s: tr_number(s.max_drawdown_r, 2) + "R"),
        row("maks. düşüş (%)", lambda s: "%" + tr_number(s.max_drawdown_pct, 2)),
        row("en uzun kayıp serisi", lambda s: tr_int(s.longest_losing_streak)),
    ]
    if split.g3_ratio is None:
        lines.append(f"G3 oranı (OOS / IS beklenti): yok: {split.g3_note}")
    else:
        lines.append(f"G3 oranı (OOS / IS beklenti): {tr_number(split.g3_ratio, 2)}")
    if split.all.incomplete:
        lines.append(
            f"! {tr_int(split.all.incomplete)} işlem veri bittiğinde açıktı: "
            "istatistiklerin dışında bırakıldı."
        )
    lines.append("R ve K/Z maliyet sonrasıdır; kazanç/kayıp net K/Z'nin işaretine göredir.")
    return "\n".join(lines) + "\n"


def _check_capital(initial_capital: float) -> None:
    if not (math.isfinite(initial_capital) and initial_capital > 0.0):
        raise AnalyticsError("başlangıç sermayesi pozitif, sonlu bir sayı olmalı")


def _max_drawdown(changes: Sequence[float], *, start: float, as_percent: bool) -> float:
    level = peak = start
    worst = 0.0
    for change in changes:
        level += change
        peak = max(peak, level)
        fall = (peak - level) / peak * 100.0 if as_percent else peak - level
        worst = max(worst, fall)
    return worst


def _longest_losing_streak(ordered: Sequence[EntryResult]) -> int:
    longest = current = 0
    for entry in ordered:
        current = current + 1 if entry.pnl < 0.0 else 0
        longest = max(longest, current)
    return longest


def _row(key: str, label: str, entries: Sequence[EntryResult]) -> TableRow:
    n = len(entries)
    wins = sum(1 for entry in entries if entry.pnl > 0.0)
    total_r = math.fsum(entry.r for entry in entries)
    return TableRow(
        key=key,
        label=label,
        trades=n,
        wins=wins,
        win_rate=wins / n if n else None,
        total_r=total_r,
        mean_r=float(statistics.mean(entry.r for entry in entries)) if n else None,
        total_pnl=math.fsum(entry.pnl for entry in entries),
    )


def _months_between(months: dict[tuple[int, int], list[EntryResult]]) -> list[tuple[int, int]]:
    # Months without a trade stay in the table: a gap is information, not noise.
    if not months:
        return []
    year, month = min(months)
    last = max(months)
    result: list[tuple[int, int]] = []
    while (year, month) <= last:
        result.append((year, month))
        year, month = (year + 1, 1) if month == 12 else (year, month + 1)
    return result


def _first_index(
    points: Sequence[EquityPoint],
    pick: Callable[[Sequence[float]], float],
    value: Callable[[EquityPoint], float],
) -> int:
    values = [value(point) for point in points]
    return values.index(pick(values))


def _utc(ts_ms: int) -> dt.datetime:
    # Integer arithmetic: a float division would round some millisecond stamps.
    return _EPOCH + dt.timedelta(milliseconds=ts_ms)


def _iso(moment: dt.datetime) -> str:
    spec = "milliseconds" if moment.microsecond else "seconds"
    return moment.astimezone(dt.UTC).isoformat(timespec=spec).replace("+00:00", "Z")


def _opt(value: float | None, formatter: Callable[[float], str]) -> str:
    return "—" if value is None else formatter(value)
