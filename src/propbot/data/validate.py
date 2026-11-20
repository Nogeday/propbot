"""Data quality report of a bar file or series (SPEC §6.1).

The report is built from `RawBars` as well as from a `BarSeries`: its point is to explain
why `load_ohlcv` refused a file. Ordering problems and impossible bars are BLOCKING (the
loader refuses them); the other findings are reported and make the report not `clean`,
but they are a judgement for the user, not a refusal:

- intraday gaps: missing bars INSIDE a session (sessions come from the data, see
  `propbot.data.sessions`; a hole of `min_gap_seconds` or more looks like a session end
  and shows up as a short session instead);
- outliers: a bar range, or an open-versus-previous-close jump inside a session, beyond a
  multiple of the rolling median true range. Jumps at a session's first bar are normal
  (weekend, maintenance break) and are not tested;
- a time-zone shift: across a New York or an EU daylight-saving change inside the span,
  the session's first bar no longer opens at the same New York wall-clock time. Correct
  data keeps it; a shift means the file was read with the wrong `bar_tz`.

Short sessions and weekdays without a session are listed as "holiday / half day?" and do
not make the report unclean: the calendar is whatever the data says (ADR-0008 item 6).
"""

import bisect
import datetime as dt
import itertools
import math
from collections import Counter, deque
from collections.abc import Sequence
from dataclasses import dataclass
from typing import Final
from zoneinfo import ZoneInfo

from propbot.core.bars import Bar, BarSeries, DataSource
from propbot.core.session import NEW_YORK
from propbot.data.ohlcv import (
    MAX_REPORTED_PROBLEMS,
    RawBars,
    bar_defect,
    common_spacing_ms,
    order_defects,
    utc_text,
)
from propbot.data.sessions import DEFAULT_MIN_GAP_SECONDS, split_sessions

# A bar whose range exceeds this many rolling-median true ranges is listed. Real index
# futures print 09:30 and news bars of 10-20 quiet medians; the threshold sits above them.
RANGE_OUTLIER_MULTIPLE: Final = 25.0
# Inside a session a bar opens at (or ticks away from) the previous close; a jump of this
# many median true ranges is a data error (a spliced contract, a bad print).
GAP_OUTLIER_MULTIPLE: Final = 15.0
OUTLIER_MEDIAN_WINDOW: Final = 240
# Too few bars for a median to mean anything: the first bars of a file are not tested.
OUTLIER_MIN_HISTORY: Final = 30
# A session with fewer bars than this share of the typical (median) session is listed. A
# 23-hour index session that closes at 13:00 instead of 17:00 keeps 83 % of its bars.
SHORT_SESSION_RATIO: Final = 0.9
# Sessions on each side of a DST change whose first-bar wall time is compared.
DST_SESSIONS_COMPARED: Final = 5
# Any EU zone: the EU changes its clocks at the same UTC instant everywhere.
EU_DST_ZONE: Final = "Europe/Berlin"
REGION_US: Final = "US"
REGION_EU: Final = "EU"
OUTLIER_RANGE: Final = "range"
OUTLIER_GAP: Final = "gap"

_MS_PER_SECOND: Final = 1000
_ONE_HOUR: Final = dt.timedelta(hours=1)
_ONE_DAY: Final = dt.timedelta(days=1)
_SATURDAY: Final = 5
_REGION_NAMES: Final = {REGION_US: "ABD", REGION_EU: "AB"}


@dataclass(frozen=True, slots=True)
class RowIssue:
    line: int | None  # line in the file; None for a series built in memory
    ts_ms: int | None  # open time, when the row has one
    message: str  # Turkish


@dataclass(frozen=True, slots=True)
class Gap:
    first_missing_ms: int  # open time of the first missing bar
    missing_bars: int
    line: int | None  # line of the bar after the gap


@dataclass(frozen=True, slots=True)
class ShortSession:
    first_ts_ms: int
    last_ts_ms: int
    bars: int


@dataclass(frozen=True, slots=True)
class DstCheck:
    region: str  # REGION_US or REGION_EU
    change_ms: int  # the instant the clocks change
    before_minute: int | None  # typical first-bar minute of day (session tz) before the change
    after_minute: int | None

    @property
    def checked(self) -> bool:
        return self.before_minute is not None and self.after_minute is not None

    @property
    def shifted(self) -> bool:
        return self.checked and self.before_minute != self.after_minute


@dataclass(frozen=True, slots=True)
class Outlier:
    line: int | None
    ts_ms: int
    kind: str  # OUTLIER_RANGE or OUTLIER_GAP
    size: float  # the range or the jump, in price
    median_true_range: float

    @property
    def multiple(self) -> float:
        return self.size / self.median_true_range


@dataclass(frozen=True, slots=True)
class ValidationReport:
    source: DataSource
    symbol: str
    tf_seconds: int | None  # None when the bar length could not be determined
    tz: str  # the session time zone the wall-clock checks use
    rows: int  # data rows, readable or not
    bar_count: int  # readable rows
    first_ts_ms: int | None
    last_ts_ms: int | None
    span_days: int  # calendar days (session time zone) from the first to the last bar
    session_count: int
    unreadable: tuple[RowIssue, ...]
    unreadable_total: int
    ohlc_inconsistent: tuple[RowIssue, ...]
    ohlc_inconsistent_total: int
    duplicates: tuple[RowIssue, ...]
    duplicates_total: int
    out_of_order: tuple[RowIssue, ...]
    out_of_order_total: int
    overlaps: tuple[RowIssue, ...]  # a bar opening before the previous one has closed
    overlaps_total: int
    gaps: tuple[Gap, ...]
    gaps_total: int
    missing_bars_total: int
    typical_session_bars: int
    short_sessions: tuple[ShortSession, ...]
    short_sessions_total: int
    missing_days: tuple[dt.date, ...]  # weekdays without a session between two sessions
    missing_days_total: int
    dst: tuple[DstCheck, ...]
    outliers: tuple[Outlier, ...]
    outliers_total: int
    zero_volume_bars: int

    @property
    def blocking_total(self) -> int:
        return (
            self.unreadable_total
            + self.ohlc_inconsistent_total
            + self.duplicates_total
            + self.out_of_order_total
            + self.overlaps_total
        )

    @property
    def blocking(self) -> bool:
        return self.blocking_total > 0 or self.bar_count == 0

    @property
    def timezone_shift(self) -> bool:
        return any(check.shifted for check in self.dst)

    @property
    def zero_volume_share(self) -> float:
        return self.zero_volume_bars / self.bar_count if self.bar_count else 0.0

    @property
    def clean(self) -> bool:
        return (
            not self.blocking
            and self.gaps_total == 0
            and self.outliers_total == 0
            and not self.timezone_shift
        )

    def to_dict(self) -> dict[str, object]:
        """JSON-safe form (str, int, float, bool, None, lists and dicts only)."""
        return {
            "source": {
                "name": self.source.name,
                "format": self.source.format,
                "sha256": self.source.sha256,
                "synthetic": self.source.synthetic,
                "bar_tz": self.source.bar_tz,
            },
            "symbol": self.symbol,
            "tf_seconds": self.tf_seconds,
            "tz": self.tz,
            "rows": self.rows,
            "bar_count": self.bar_count,
            "first_bar_utc": _utc_or_none(self.first_ts_ms),
            "last_bar_utc": _utc_or_none(self.last_ts_ms),
            "first_bar_local": self._local_or_none(self.first_ts_ms),
            "last_bar_local": self._local_or_none(self.last_ts_ms),
            "span_days": self.span_days,
            "session_count": self.session_count,
            "clean": self.clean,
            "blocking": self.blocking,
            "blocking_total": self.blocking_total,
            "timezone_shift": self.timezone_shift,
            "unreadable": _issues(self.unreadable, self.unreadable_total),
            "ohlc_inconsistent": _issues(self.ohlc_inconsistent, self.ohlc_inconsistent_total),
            "duplicates": _issues(self.duplicates, self.duplicates_total),
            "out_of_order": _issues(self.out_of_order, self.out_of_order_total),
            "overlaps": _issues(self.overlaps, self.overlaps_total),
            "gaps": {
                "total": self.gaps_total,
                "missing_bars_total": self.missing_bars_total,
                "items": [
                    {
                        "first_missing_utc": utc_text(gap.first_missing_ms),
                        "missing_bars": gap.missing_bars,
                        "line": gap.line,
                    }
                    for gap in self.gaps
                ],
            },
            "sessions": {
                "count": self.session_count,
                "typical_bars": self.typical_session_bars,
                "short_total": self.short_sessions_total,
                "short": [
                    {
                        "first_utc": utc_text(item.first_ts_ms),
                        "last_utc": utc_text(item.last_ts_ms),
                        "bars": item.bars,
                    }
                    for item in self.short_sessions
                ],
                "missing_days_total": self.missing_days_total,
                "missing_days": [day.isoformat() for day in self.missing_days],
            },
            "dst": [
                {
                    "region": check.region,
                    "change_utc": utc_text(check.change_ms),
                    "before_first_bar": _hhmm_or_none(check.before_minute),
                    "after_first_bar": _hhmm_or_none(check.after_minute),
                    "checked": check.checked,
                    "shifted": check.shifted,
                }
                for check in self.dst
            ],
            "outliers": {
                "total": self.outliers_total,
                "items": [
                    {
                        "utc": utc_text(item.ts_ms),
                        "line": item.line,
                        "kind": item.kind,
                        "size": item.size,
                        "median_true_range": item.median_true_range,
                        "multiple": item.multiple,
                    }
                    for item in self.outliers
                ],
            },
            "zero_volume_bars": self.zero_volume_bars,
            "zero_volume_share": self.zero_volume_share,
        }

    def _local_or_none(self, ts_ms: int | None) -> str | None:
        return None if ts_ms is None else _local_text(ts_ms, ZoneInfo(self.tz))


def validate_bars(
    data: RawBars | BarSeries,
    *,
    tf_seconds: int | None = None,
    tz: str = NEW_YORK,
    min_gap_seconds: int = DEFAULT_MIN_GAP_SECONDS,
    range_multiple: float = RANGE_OUTLIER_MULTIPLE,
    gap_multiple: float = GAP_OUTLIER_MULTIPLE,
    median_window: int = OUTLIER_MEDIAN_WINDOW,
    short_session_ratio: float = SHORT_SESSION_RATIO,
) -> ValidationReport:
    """Build the report. `tf_seconds` applies to `RawBars` only (a series knows its own);
    left out, it is the most common spacing between consecutive bars."""
    if not (range_multiple > 0.0 and gap_multiple > 0.0 and median_window > 0):
        raise ValueError("outlier thresholds must be positive")
    zone = ZoneInfo(tz)
    if isinstance(data, BarSeries):
        bars: Sequence[Bar] = data.bars
        lines: Sequence[int | None] = [None] * len(bars)
        unreadable: list[RowIssue] = []
        unreadable_total = 0
        symbol, source = data.symbol, data.source
        tf: int | None = data.tf_seconds
    else:
        bars = data.bars
        lines = data.lines
        unreadable = [
            RowIssue(problem.line, None, problem.message)
            for problem in data.rejected[:MAX_REPORTED_PROBLEMS]
        ]
        unreadable_total = data.rejected_total
        symbol, source = data.symbol, data.source
        tf = tf_seconds if tf_seconds is not None else _inferred_tf(bars)

    inconsistent = [
        RowIssue(lines[index], bar.ts_open_ms, defect)
        for index, bar in enumerate(bars)
        if (defect := bar_defect(bar)) is not None
    ]
    duplicate_idx, out_of_order_idx = order_defects(bars)
    duplicates = [
        RowIssue(
            lines[i], bars[i].ts_open_ms, "yinelenen bar zamanı: " + utc_text(bars[i].ts_open_ms)
        )
        for i in duplicate_idx
    ]
    out_of_order = [
        RowIssue(
            lines[i],
            bars[i].ts_open_ms,
            f"sıra dışı bar: {utc_text(bars[i].ts_open_ms)}, kendinden önceki bir bardan eski",
        )
        for i in out_of_order_idx
    ]

    # Timing checks run on the ascending part of the file: the rows the ordering checks
    # above did not flag.
    dropped = set(duplicate_idx) | set(out_of_order_idx)
    kept = [index for index in range(len(bars)) if index not in dropped]
    ordered = [bars[index] for index in kept]
    ordered_lines = [lines[index] for index in kept]

    overlaps: list[RowIssue] = []
    gaps: list[Gap] = []
    missing_total = 0
    sessions: tuple[range, ...] = (range(len(ordered)),) if ordered else ()
    if tf is not None:
        tf_ms = tf * _MS_PER_SECOND
        sessions = split_sessions(ordered, tf, min_gap_seconds=min_gap_seconds)
        for session in sessions:
            for index in session[1:]:
                spacing = ordered[index].ts_open_ms - ordered[index - 1].ts_open_ms
                if spacing < tf_ms:
                    overlaps.append(
                        RowIssue(
                            ordered_lines[index],
                            ordered[index].ts_open_ms,
                            f"bar bir öncekinden {spacing / _MS_PER_SECOND:g} sn sonra açılıyor; "
                            f"bar süresi {tf} sn",
                        )
                    )
                elif spacing > tf_ms:
                    missing = (spacing - 1) // tf_ms
                    missing_total += missing
                    gaps.append(
                        Gap(ordered[index - 1].ts_open_ms + tf_ms, missing, ordered_lines[index])
                    )

    counts = [len(session) for session in sessions]
    typical = _median_int(counts)
    short = [
        ShortSession(ordered[s.start].ts_open_ms, ordered[s.stop - 1].ts_open_ms, len(s))
        for s in sessions
        if len(s) < short_session_ratio * typical
    ]
    missing_days = _missing_weekdays(ordered, sessions, zone)
    dst = _dst_checks(ordered, sessions, zone)
    outliers = _outliers(
        ordered, ordered_lines, sessions, range_multiple, gap_multiple, median_window
    )

    first_ms = ordered[0].ts_open_ms if ordered else None
    last_ms = ordered[-1].ts_open_ms if ordered else None
    span_days = 0
    if first_ms is not None and last_ms is not None:
        span_days = (_local(last_ms, zone).date() - _local(first_ms, zone).date()).days + 1

    cap = MAX_REPORTED_PROBLEMS
    return ValidationReport(
        source=source,
        symbol=symbol,
        tf_seconds=tf,
        tz=tz,
        rows=len(bars) + unreadable_total,
        bar_count=len(bars),
        first_ts_ms=first_ms,
        last_ts_ms=last_ms,
        span_days=span_days,
        session_count=len(sessions),
        unreadable=tuple(unreadable),
        unreadable_total=unreadable_total,
        ohlc_inconsistent=tuple(inconsistent[:cap]),
        ohlc_inconsistent_total=len(inconsistent),
        duplicates=tuple(duplicates[:cap]),
        duplicates_total=len(duplicates),
        out_of_order=tuple(out_of_order[:cap]),
        out_of_order_total=len(out_of_order),
        overlaps=tuple(overlaps[:cap]),
        overlaps_total=len(overlaps),
        gaps=tuple(gaps[:cap]),
        gaps_total=len(gaps),
        missing_bars_total=missing_total,
        typical_session_bars=typical,
        short_sessions=tuple(short[:cap]),
        short_sessions_total=len(short),
        missing_days=tuple(missing_days[:cap]),
        missing_days_total=len(missing_days),
        dst=tuple(dst),
        outliers=tuple(outliers[:cap]),
        outliers_total=len(outliers),
        zero_volume_bars=sum(1 for bar in bars if bar.volume == 0.0),
    )


def render_validation_report(report: ValidationReport) -> str:
    """The report as Turkish text for the terminal."""
    zone = ZoneInfo(report.tz)
    out: list[str] = []
    source = report.source
    if source.synthetic:
        out.append("!!! SENTETİK VERİ: gerçek piyasa değildir, hiçbir sonucu kanıtlamaz !!!")
    if report.timezone_shift:
        out.append(
            "!!! SAAT DİLİMİ KAYMASI: yaz saati geçişinde seans açılış saati değişiyor; dosya "
            f"büyük olasılıkla yanlış bar_tz ile okundu (bar_tz={source.bar_tz}) !!!"
        )
    verdict = "TEMİZ" if report.clean else ("YÜKLENEMEZ" if report.blocking else "SORUNLU")
    out.append(f"Veri doğrulama raporu: {verdict}")
    out.append(
        f"Kaynak: {source.name} · biçim {source.format} · bar_tz {source.bar_tz} · "
        f"sha256 {source.sha256 or '-'}"
    )
    tf_text = f"{report.tf_seconds} sn" if report.tf_seconds is not None else "belirlenemedi"
    out.append(f"Sembol: {report.symbol} · bar süresi: {tf_text}")
    out.append(f"Satır: {report.rows} · okunan bar: {report.bar_count}")
    if report.first_ts_ms is not None and report.last_ts_ms is not None:
        out.append(
            f"İlk bar: {utc_text(report.first_ts_ms)} ({_local_text(report.first_ts_ms, zone)})"
        )
        out.append(
            f"Son bar: {utc_text(report.last_ts_ms)} ({_local_text(report.last_ts_ms, zone)})"
        )
    out.append(f"Kapsam: {report.span_days} takvim günü · {report.session_count} seans")

    out.append(f"Engelleyici sorun: {report.blocking_total}")
    for title, items, total in (
        ("Okunamayan satır", report.unreadable, report.unreadable_total),
        ("Tutarsız OHLC", report.ohlc_inconsistent, report.ohlc_inconsistent_total),
        ("Yinelenen bar", report.duplicates, report.duplicates_total),
        ("Sıra dışı bar", report.out_of_order, report.out_of_order_total),
        ("Üst üste binen bar", report.overlaps, report.overlaps_total),
    ):
        if total:
            out.append(f"  {title}: {total}")
            out.extend(f"    {_where(item.line, item.ts_ms)}: {item.message}" for item in items)
            out.extend(_more(total, len(items)))

    out.append(
        f"Gün içi boşluk: {report.gaps_total} (eksik bar toplamı {report.missing_bars_total})"
    )
    out.extend(
        f"    {_local_text(gap.first_missing_ms, zone)}: {gap.missing_bars} bar eksik"
        + (f" (satır {gap.line} öncesi)" if gap.line is not None else "")
        for gap in report.gaps
    )
    out.extend(_more(report.gaps_total, len(report.gaps)))

    out.append(f"Seans kapsaması: tipik seans {report.typical_session_bars} bar")
    if report.short_sessions_total:
        out.append(
            "  Kısa seans — tatil / yarım gün ya da dosya başı/sonu?: "
            f"{report.short_sessions_total}"
        )
        out.extend(
            f"    {_local_text(item.first_ts_ms, zone)} → {_local_text(item.last_ts_ms, zone)}: "
            f"{item.bars} bar"
            for item in report.short_sessions
        )
        out.extend(_more(report.short_sessions_total, len(report.short_sessions)))
    if report.missing_days_total:
        out.append(f"  Seansı olmayan hafta içi gün — tatil?: {report.missing_days_total}")
        out.extend(f"    {day.isoformat()}" for day in report.missing_days)
        out.extend(_more(report.missing_days_total, len(report.missing_days)))

    if report.dst:
        out.append("Yaz saati geçişleri (seansın ilk barı, yerel saat):")
        for check in report.dst:
            region = _REGION_NAMES.get(check.region, check.region)
            if not check.checked:
                state = "denetlenemedi (geçişin bir yanında seans yok)"
            elif check.shifted:
                state = (
                    f"KAYMA: {_hhmm_or_none(check.before_minute)} → "
                    f"{_hhmm_or_none(check.after_minute)} — bar_tz yanlış olabilir"
                )
            else:
                state = f"tutarlı ({_hhmm_or_none(check.before_minute)})"
            out.append(f"  {region} {_local_text(check.change_ms, zone)}: {state}")
    else:
        out.append("Yaz saati geçişi: kapsam içinde yok")

    out.append(f"Aykırı sıçrama: {report.outliers_total}")
    kinds = {OUTLIER_RANGE: "bar aralığı", OUTLIER_GAP: "açılış sıçraması"}
    out.extend(
        f"    {_where(item.line, item.ts_ms)}: {kinds[item.kind]} {item.size:g} "
        f"(medyan gerçek aralığın {item.multiple:.1f} katı)"
        for item in report.outliers
    )
    out.extend(_more(report.outliers_total, len(report.outliers)))
    out.append(
        f"Hacmi sıfır bar: {report.zero_volume_bars} (%{100.0 * report.zero_volume_share:.1f})"
    )
    return "\n".join(out)


def _inferred_tf(bars: Sequence[Bar]) -> int | None:
    spacing = common_spacing_ms(bars)
    if spacing is None or spacing % _MS_PER_SECOND != 0:
        return None
    return spacing // _MS_PER_SECOND


def _median_int(values: Sequence[int]) -> int:
    if not values:
        return 0
    ordered = sorted(values)
    return ordered[len(ordered) // 2]


def _missing_weekdays(
    bars: Sequence[Bar], sessions: Sequence[range], zone: ZoneInfo
) -> list[dt.date]:
    # A session is dated by the local date of its last bar: a 23-hour session that opens
    # on Sunday evening is Monday's, and a day session is its own day.
    days = [_local(bars[s.stop - 1].ts_open_ms, zone).date() for s in sessions]
    missing: list[dt.date] = []
    for day, following in itertools.pairwise(days):
        current = day + _ONE_DAY
        while current < following:
            if current.weekday() < _SATURDAY:
                missing.append(current)
            current += _ONE_DAY
    return missing


def _dst_checks(bars: Sequence[Bar], sessions: Sequence[range], zone: ZoneInfo) -> list[DstCheck]:
    if not bars:
        return []
    first = dt.datetime.fromtimestamp(bars[0].ts_open_ms // _MS_PER_SECOND, dt.UTC)
    last = dt.datetime.fromtimestamp(bars[-1].ts_open_ms // _MS_PER_SECOND, dt.UTC)
    changes = [
        (change, region)
        for region, key in ((REGION_US, NEW_YORK), (REGION_EU, EU_DST_ZONE))
        for change in _clock_changes(ZoneInfo(key), first, last)
    ]
    changes.sort()
    starts = [bars[s.start].ts_open_ms for s in sessions]
    checks: list[DstCheck] = []
    for change, region in changes:
        change_ms = int(change.timestamp()) * _MS_PER_SECOND
        split = bisect.bisect_left(starts, change_ms)
        before = starts[max(0, split - DST_SESSIONS_COMPARED) : split]
        after = starts[split : split + DST_SESSIONS_COMPARED]
        checks.append(
            DstCheck(
                region=region,
                change_ms=change_ms,
                before_minute=_typical_minute(before, zone),
                after_minute=_typical_minute(after, zone),
            )
        )
    return checks


def _clock_changes(zone: ZoneInfo, first: dt.datetime, last: dt.datetime) -> list[dt.datetime]:
    """UTC instants in [first, last] at which the zone's UTC offset changes (on the hour)."""
    changes: list[dt.datetime] = []
    day = first.replace(hour=0, minute=0, second=0, microsecond=0)
    while day <= last:
        following = day + _ONE_DAY
        if day.astimezone(zone).utcoffset() != following.astimezone(zone).utcoffset():
            hour = day
            offset = day.astimezone(zone).utcoffset()
            while hour.astimezone(zone).utcoffset() == offset:
                hour += _ONE_HOUR
            if first <= hour <= last:
                changes.append(hour)
        day = following
    return changes


def _typical_minute(starts: Sequence[int], zone: ZoneInfo) -> int | None:
    if not starts:
        return None
    counts = Counter(_minute_of_day(ts, zone) for ts in starts)
    best = max(counts.values())
    # Ties (one partial session against one whole) go to the earliest minute: deterministic.
    return min(minute for minute, count in counts.items() if count == best)


def _outliers(
    bars: Sequence[Bar],
    lines: Sequence[int | None],
    sessions: Sequence[range],
    range_multiple: float,
    gap_multiple: float,
    window: int,
) -> list[Outlier]:
    session_starts = {s.start for s in sessions}
    recent: deque[float] = deque()
    ordered: list[float] = []
    found: list[Outlier] = []
    previous: Bar | None = None
    for index, bar in enumerate(bars):
        if bar_defect(bar) is not None:
            previous = None
            continue
        first_of_session = index in session_starts or previous is None
        median = ordered[len(ordered) // 2] if len(ordered) >= OUTLIER_MIN_HISTORY else 0.0
        # A zero median (a flat stretch) gives no scale: nothing is judged against it.
        if median > 0.0:
            size = bar.high - bar.low
            if size > range_multiple * median:
                found.append(Outlier(lines[index], bar.ts_open_ms, OUTLIER_RANGE, size, median))
            if not first_of_session and previous is not None:
                jump = abs(bar.open - previous.close)
                if jump > gap_multiple * median:
                    found.append(Outlier(lines[index], bar.ts_open_ms, OUTLIER_GAP, jump, median))
        if first_of_session or previous is None:
            true_range = bar.high - bar.low
        else:
            true_range = max(bar.high, previous.close) - min(bar.low, previous.close)
        if math.isfinite(true_range):
            recent.append(true_range)
            bisect.insort(ordered, true_range)
            if len(recent) > window:
                ordered.pop(bisect.bisect_left(ordered, recent.popleft()))
        previous = bar
    return found


def _local(ts_ms: int, zone: ZoneInfo) -> dt.datetime:
    return dt.datetime.fromtimestamp(ts_ms // _MS_PER_SECOND, zone)


def _minute_of_day(ts_ms: int, zone: ZoneInfo) -> int:
    moment = _local(ts_ms, zone)
    return moment.hour * 60 + moment.minute


def _local_text(ts_ms: int, zone: ZoneInfo) -> str:
    return f"{_local(ts_ms, zone):%Y-%m-%d %H:%M} {zone.key}"


def _hhmm_or_none(minute: int | None) -> str | None:
    return None if minute is None else f"{minute // 60:02d}:{minute % 60:02d}"


def _utc_or_none(ts_ms: int | None) -> str | None:
    return None if ts_ms is None else utc_text(ts_ms)


def _issues(items: Sequence[RowIssue], total: int) -> dict[str, object]:
    return {
        "total": total,
        "items": [
            {
                "line": item.line,
                "utc": _utc_or_none(item.ts_ms),
                "message": item.message,
            }
            for item in items
        ],
    }


def _where(line: int | None, ts_ms: int | None) -> str:
    if line is not None:
        return f"satır {line}"
    return utc_text(ts_ms) if ts_ms is not None else "?"


def _more(total: int, shown: int) -> list[str]:
    return [f"    ... ve {total - shown} tane daha"] if total > shown else []
