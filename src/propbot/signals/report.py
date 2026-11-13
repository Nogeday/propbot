"""Signal report: what arrived, what was forwarded and how late (SPEC §5.7 evidence).

Read-only view of the store for `propbot signals report`. The numbers are meant to be
compared with TradingView's own Alert Log: every alert in that log must appear here
(no missed signal), and the idempotency key guarantees none appears twice.
"""

import csv
import datetime as dt
import io
import math
from collections import Counter
from collections.abc import Sequence
from dataclasses import dataclass
from typing import Final

from propbot.core.clock import ms_from_datetime, utc_from_ms
from propbot.notify.format import ISTANBUL, fmt_clock, fmt_date
from propbot.store.db import (
    LatencySample,
    OutboxKind,
    OutboxRow,
    OutboxStatus,
    Outcome,
    SignalRow,
    Store,
)

# SPEC §5.7: median end-to-end latency (bar close -> Telegram) must stay below this.
ACCEPTANCE_MEDIAN_LATENCY_MS: Final = 5000
_HEARTBEAT_ACTION: Final = "heartbeat"
_ENTRY_ACTION: Final = "entry"
_MAX_LISTED_ALARMS: Final = 20

OUTCOME_LABELS: Final[dict[Outcome, str]] = {
    Outcome.FORWARDED: "iletildi",
    Outcome.STALE: "bayat",
    Outcome.SUPPRESSED_PAUSED: "duraklatıldığı için iletilmedi",
    Outcome.SUPPRESSED_KILLED: "KILL nedeniyle iletilmedi",
    Outcome.SKIP_RISK_TOO_WIDE: "atla: stop çok geniş",
    Outcome.SKIP_BUDGET: "atla: günlük bütçe",
    Outcome.SKIP_CLOCK: "atla: saat uyuşmazlığı",
    Outcome.FILTERED_MIN_STEP: "süzüldü: adım küçük",
    Outcome.REJECTED_WIDEN: "reddedildi: stop genişliyor",
    Outcome.SUPERSEDED: "eskidi: yenisi gönderilmişti",
    Outcome.NO_ACTIVE_TRADE: "ilgili işlem yok",
    Outcome.HEARTBEAT: "heartbeat",
    Outcome.ERROR: "işlenemedi (hata)",
}
_SKIPPED: Final = frozenset(
    {
        Outcome.SUPPRESSED_PAUSED,
        Outcome.SUPPRESSED_KILLED,
        Outcome.SKIP_RISK_TOO_WIDE,
        Outcome.SKIP_BUDGET,
        Outcome.SKIP_CLOCK,
    }
)
CSV_HEADER: Final = (
    "id",
    "received_at_utc",
    "strategy_id",
    "symbol",
    "tf",
    "action",
    "side",
    "bar_time_utc",
    "bar_close_utc",
    "outcome",
    "receive_latency_ms",
    "telegram_latency_ms",
)


class ReportError(Exception):
    """The report cannot be produced. The message is user-facing (Turkish)."""


@dataclass(frozen=True, slots=True)
class LatencyStats:
    count: int
    median_ms: float
    p95_ms: int
    max_ms: int


def latency_stats(values: Sequence[int]) -> LatencyStats | None:
    """Median, 95th percentile (nearest rank) and maximum; None without samples."""
    if not values:
        return None
    ordered = sorted(values)
    count = len(ordered)
    middle = count // 2
    median = ordered[middle] if count % 2 else (ordered[middle - 1] + ordered[middle]) / 2
    p95 = ordered[math.ceil(0.95 * count) - 1]
    return LatencyStats(count=count, median_ms=median, p95_ms=p95, max_ms=ordered[-1])


def day_start_ms(day: dt.date) -> int:
    """Start of a Europe/Istanbul calendar day as epoch milliseconds."""
    return ms_from_datetime(dt.datetime.combine(day, dt.time(0, 0), tzinfo=ISTANBUL))


@dataclass(frozen=True, slots=True)
class _Data:
    signals: list[SignalRow]
    samples: list[LatencySample]
    messages: list[OutboxRow]
    counts: dict[OutboxStatus, int]


def _read(store: Store, start_ms: int, end_ms: int) -> _Data:
    with store.transaction() as tx:
        signals = tx.signals_received_between(start_ms, end_ms)
        samples = [s for s in tx.latency_samples(start_ms) if s.received_at_ms < end_ms]
        return _Data(
            signals=signals,
            samples=samples,
            messages=tx.messages_created_between(start_ms, end_ms),
            counts=tx.outbox_counts(),
        )


def build_report(store: Store, *, start_ms: int, end_ms: int, source: str) -> str:
    """Render the Turkish text report for signals received in [start_ms, end_ms)."""
    data = _read(store, start_ms, end_ms)
    lines = [
        f"Sinyal raporu · {source}",
        f"Dönem (Türkiye saati): {_stamp(start_ms)} → {_stamp(end_ms)}",
        "",
    ]
    if not data.signals:
        lines.append("Bu dönemde alınmış alert yok.")
        return "\n".join(lines) + "\n"

    lines += _daily_table(data.signals)
    lines.append("")
    lines += _outcome_lines(data.signals)
    lines += _latency_lines(data.samples)
    lines += _outbox_lines(data)
    lines += [
        "",
        "Karşılaştırma: TradingView → Alerts → Log'daki kayıt sayısı, yukarıdaki 'alert' ve",
        "'heartbeat' sütunlarının toplamına eşit olmalı (kaçan sinyal 0). Aynı alert iki kez",
        "kaydedilemez: (strateji, sembol, bar zamanı, eylem) anahtarı veritabanında tekildir.",
    ]
    return "\n".join(lines) + "\n"


def _stamp(epoch_ms: int) -> str:
    return f"{fmt_date(epoch_ms, ISTANBUL)} {fmt_clock(epoch_ms, ISTANBUL)}"


def _daily_table(signals: Sequence[SignalRow]) -> list[str]:
    header = (
        "Gün (TR)",
        "alert",
        "heartbeat",
        "giriş",
        "iletilen giriş",
        "bayat",
        "atlanan",
        "hata",
    )
    rows: dict[str, Counter[str]] = {}
    for signal in signals:
        counter = rows.setdefault(fmt_date(signal.received_at_ms, ISTANBUL), Counter())
        if signal.action == _HEARTBEAT_ACTION:
            counter["heartbeat"] += 1
            continue
        counter["alert"] += 1
        if signal.action == _ENTRY_ACTION:
            counter["giriş"] += 1
            if signal.outcome is Outcome.FORWARDED:
                counter["iletilen giriş"] += 1
        if signal.outcome is Outcome.STALE:
            counter["bayat"] += 1
        elif signal.outcome in _SKIPPED:
            counter["atlanan"] += 1
        elif signal.outcome is Outcome.ERROR:
            counter["hata"] += 1

    total: Counter[str] = Counter()
    for counter in rows.values():
        total.update(counter)
    widths = [max(10, len(header[0]))] + [max(5, len(name)) for name in header[1:]]

    def render(label: str, counter: Counter[str]) -> str:
        cells = [label.ljust(widths[0])]
        cells += [
            str(counter[name]).rjust(width)
            for name, width in zip(header[1:], widths[1:], strict=True)
        ]
        return "  ".join(cells).rstrip()

    titles = [header[0].ljust(widths[0])]
    titles += [name.rjust(width) for name, width in zip(header[1:], widths[1:], strict=True)]
    table = ["  ".join(titles)]
    table += [render(day, rows[day]) for day in sorted(rows)]
    table.append(render("Toplam", total))
    return table


def _outcome_lines(signals: Sequence[SignalRow]) -> list[str]:
    outcomes = Counter(
        signal.outcome
        for signal in signals
        if signal.action != _HEARTBEAT_ACTION and signal.outcome is not None
    )
    queued = sum(1 for signal in signals if signal.outcome is None)
    parts = [f"{OUTCOME_LABELS[outcome]} {count}" for outcome, count in outcomes.most_common()]
    if queued:
        parts.append(f"kuyrukta bekleyen {queued}")
    return ["Sonuçlar (heartbeat hariç): " + (" · ".join(parts) if parts else "yok")]


def _latency_lines(samples: Sequence[LatencySample]) -> list[str]:
    receive = latency_stats([s.received_at_ms - s.bar_close_ms for s in samples])
    telegram = latency_stats(
        [s.sent_at_ms - s.bar_close_ms for s in samples if s.sent_at_ms is not None]
    )
    lines = [
        "Gecikme, bar kapanışı → alıcı: " + _latency_text(receive, "alert"),
        "Gecikme, bar kapanışı → Telegram: " + _latency_text(telegram, "mesaj"),
    ]
    if telegram is None:
        verdict = "veri yok"
    elif telegram.median_ms < ACCEPTANCE_MEDIAN_LATENCY_MS:
        verdict = "SAĞLANDI"
    else:
        verdict = "SAĞLANMADI"
    seconds = ACCEPTANCE_MEDIAN_LATENCY_MS // 1000
    lines.append(f"  SPEC §5.7 ölçütü (uçtan uca medyan < {seconds} sn): {verdict}")
    return lines


def _latency_text(stats: LatencyStats | None, unit: str) -> str:
    if stats is None:
        return "örnek yok"
    return (
        f"medyan {stats.median_ms / 1000:.1f} sn · %95 {stats.p95_ms / 1000:.1f} sn · "
        f"en fazla {stats.max_ms / 1000:.1f} sn ({stats.count} {unit})"
    )


def _outbox_lines(data: _Data) -> list[str]:
    counts = data.counts
    lines = [
        "Telegram kutusu (tüm zamanlar): "
        f"gönderildi {counts[OutboxStatus.SENT]} · bekliyor {counts[OutboxStatus.PENDING]} · "
        f"süresi doldu {counts[OutboxStatus.EXPIRED]} · başarısız {counts[OutboxStatus.FAILED]}"
    ]
    alarms = [message for message in data.messages if message.kind is OutboxKind.ALARM]
    lines.append(f"Dönemdeki alarmlar: {len(alarms)}")
    for alarm in alarms[:_MAX_LISTED_ALARMS]:
        first_line = alarm.text.splitlines()[0] if alarm.text else ""
        lines.append(f"  {_stamp(alarm.created_at_ms)} TR  {first_line}")
    if len(alarms) > _MAX_LISTED_ALARMS:
        lines.append(f"  … ve {len(alarms) - _MAX_LISTED_ALARMS} alarm daha")
    return lines


def signals_csv(store: Store, *, start_ms: int, end_ms: int) -> str:
    """One CSV row per signal received in [start_ms, end_ms), for spreadsheet comparison."""
    data = _read(store, start_ms, end_ms)
    sent_at = {sample.signal_id: sample.sent_at_ms for sample in data.samples}
    buffer = io.StringIO()
    writer = csv.writer(buffer, lineterminator="\n")
    writer.writerow(CSV_HEADER)
    for signal in data.signals:
        sent = sent_at.get(signal.id)
        writer.writerow(
            [
                signal.id,
                _iso(signal.received_at_ms),
                signal.strategy_id,
                signal.symbol,
                signal.tf,
                signal.action,
                signal.side or "",
                _iso(signal.bar_time_ms),
                _iso(signal.bar_close_ms),
                "" if signal.outcome is None else signal.outcome.value,
                signal.received_at_ms - signal.bar_close_ms,
                "" if sent is None else sent - signal.bar_close_ms,
            ]
        )
    return buffer.getvalue()


def _iso(epoch_ms: int) -> str:
    return utc_from_ms(epoch_ms).isoformat(timespec="milliseconds").replace("+00:00", "Z")
