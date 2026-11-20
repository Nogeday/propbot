"""Walk-forward over one run's entries: rolling IS/OOS windows, no parameter search.

ADR-0008 item 9: the parameters are the Pine input defaults and stay fixed; walk-forward
only cuts one backtest's entry list into consecutive calendar windows and asks whether
the out-of-sample windows keep the in-sample expectancy. Nothing is fitted on IS.

Windows are New York calendar dates, counted from the NY date of the first complete
entry: fold k has IS = [d0 + k*step, + is_days) and OOS right after it for oos_days.
A fold is kept only when its whole OOS window lies inside the data span the caller
states (first and last bar). The span is not inferred from the trades: a quiet month at
the end of the data is still data.
"""

import datetime as dt
import statistics
from collections.abc import Sequence
from dataclasses import dataclass
from typing import Final

from propbot.backtest.analytics import TradeStats, expectancy_ratio, trade_stats
from propbot.backtest.model import EntryResult
from propbot.core.session import NEW_YORK, LocalClock

NO_SEARCH_NOTE: Final = (
    "Parametre araması yapılmadı: tüm pencereler aynı (Pine varsayılan) parametrelerle "
    "tek koşunun işlemleridir (ADR-0008 madde 9)."
)


class WalkForwardError(ValueError):
    """The walk-forward cannot run with these inputs. The message is user-facing (Turkish)."""


@dataclass(frozen=True, slots=True)
class WalkForwardConfig:
    is_days: int
    oos_days: int
    step_days: int

    def __post_init__(self) -> None:
        for name in ("is_days", "oos_days", "step_days"):
            value = getattr(self, name)
            if isinstance(value, bool) or not isinstance(value, int) or value < 1:
                raise WalkForwardError(f"{name} en az 1 gün olan bir tam sayı olmalı")
        if self.step_days < self.oos_days:
            # Overlapping OOS windows would count one entry in several folds of the pooled sample.
            raise WalkForwardError(
                f"adım ({self.step_days} gün) OOS penceresinden ({self.oos_days} gün) kısa "
                "olamaz: OOS pencereleri çakışır ve bir işlem birden fazla sayılır"
            )


@dataclass(frozen=True, slots=True)
class Fold:
    index: int  # from 0
    is_first_day: dt.date
    is_last_day: dt.date  # inclusive
    oos_first_day: dt.date
    oos_last_day: dt.date  # inclusive
    is_stats: TradeStats
    oos_stats: TradeStats
    ratio: float | None  # OOS / IS expectancy
    ratio_note: str | None


@dataclass(frozen=True, slots=True)
class WalkForwardSummary:
    n_folds: int
    # Folds whose OOS expectancy is > 0, over ALL folds (a fold without OOS trades counts as
    # not positive). None without folds.
    share_positive_oos: float | None
    pooled_oos: TradeStats  # every OOS entry of every fold, once
    # Mean OOS expectancy / mean IS expectancy over the folds that have trades in that sample.
    efficiency: float | None
    efficiency_note: str | None
    parameter_search: bool  # always False (ADR-0008 item 9)
    note: str


@dataclass(frozen=True, slots=True)
class WalkForwardResult:
    config: WalkForwardConfig
    span_first_day: dt.date
    span_last_day: dt.date
    folds: tuple[Fold, ...]
    summary: WalkForwardSummary


def walk_forward(
    entries: Sequence[EntryResult],
    config: WalkForwardConfig,
    *,
    first_bar_ms: int,
    last_bar_ms: int,
    initial_capital: float,
    tz: str = NEW_YORK,
) -> WalkForwardResult:
    if last_bar_ms < first_bar_ms:
        raise WalkForwardError("veri aralığı ters: son bar ilk bardan önce")
    clock = LocalClock(tz)
    span_first = clock.parts(first_bar_ms).date
    span_last = clock.parts(last_bar_ms).date
    dated = [(clock.parts(entry.entry_ts_ms).date, entry) for entry in entries if entry.complete]
    if any(day < span_first or day > span_last for day, _ in dated):
        raise WalkForwardError(
            "veri aralığının dışında girişi olan işlem var: aralık yanlış verildi"
        )

    folds: list[Fold] = []
    pooled: list[EntryResult] = []
    if dated:
        start = min(day for day, _ in dated)
        step = dt.timedelta(days=config.step_days)
        while True:
            is_end = start + dt.timedelta(days=config.is_days)  # exclusive
            oos_end = is_end + dt.timedelta(days=config.oos_days)  # exclusive
            if oos_end - dt.timedelta(days=1) > span_last:
                break
            is_part = [entry for day, entry in dated if start <= day < is_end]
            oos_part = [entry for day, entry in dated if is_end <= day < oos_end]
            folds.append(
                _fold(len(folds), start, is_end, oos_end, is_part, oos_part, initial_capital)
            )
            pooled.extend(oos_part)
            start += step

    return WalkForwardResult(
        config=config,
        span_first_day=span_first,
        span_last_day=span_last,
        folds=tuple(folds),
        summary=_summary(folds, pooled, initial_capital),
    )


def _fold(
    index: int,
    start: dt.date,
    is_end: dt.date,
    oos_end: dt.date,
    is_part: Sequence[EntryResult],
    oos_part: Sequence[EntryResult],
    initial_capital: float,
) -> Fold:
    is_stats = trade_stats(is_part, initial_capital=initial_capital)
    oos_stats = trade_stats(oos_part, initial_capital=initial_capital)
    ratio, note = expectancy_ratio(oos_stats.mean_r, is_stats.mean_r)
    one_day = dt.timedelta(days=1)
    return Fold(
        index=index,
        is_first_day=start,
        is_last_day=is_end - one_day,
        oos_first_day=is_end,
        oos_last_day=oos_end - one_day,
        is_stats=is_stats,
        oos_stats=oos_stats,
        ratio=ratio,
        ratio_note=note,
    )


def _summary(
    folds: Sequence[Fold], pooled: Sequence[EntryResult], initial_capital: float
) -> WalkForwardSummary:
    is_means = [fold.is_stats.mean_r for fold in folds if fold.is_stats.mean_r is not None]
    oos_means = [fold.oos_stats.mean_r for fold in folds if fold.oos_stats.mean_r is not None]
    positive = sum(1 for mean in oos_means if mean > 0.0)
    efficiency: float | None
    note: str | None
    if not folds:
        efficiency, note = None, "tam bir OOS penceresi veri aralığına sığmadı: kat yok"
    else:
        efficiency, note = expectancy_ratio(
            float(statistics.mean(oos_means)) if oos_means else None,
            float(statistics.mean(is_means)) if is_means else None,
        )
    return WalkForwardSummary(
        n_folds=len(folds),
        share_positive_oos=positive / len(folds) if folds else None,
        pooled_oos=trade_stats(pooled, initial_capital=initial_capital),
        efficiency=efficiency,
        efficiency_note=note,
        parameter_search=False,
        note=NO_SEARCH_NOTE,
    )
