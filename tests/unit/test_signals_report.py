"""Signal report: daily counts, latency statistics and the CSV export (SPEC §5.7 evidence)."""

import csv
import datetime as dt
import io
from collections.abc import Iterator

import pytest

from propbot.core.clock import ms_from_datetime
from propbot.notify.format import ISTANBUL
from propbot.signals.report import (
    CSV_HEADER,
    OUTCOME_LABELS,
    LatencyStats,
    build_report,
    day_start_ms,
    latency_stats,
    signals_csv,
)
from propbot.store.db import NewSignal, OutboxKind, Outcome, Store

MINUTE_MS = 60_000
DAY_1 = dt.date(2026, 12, 1)
DAY_2 = dt.date(2026, 12, 2)
START_MS = day_start_ms(DAY_1)
END_MS = day_start_ms(dt.date(2026, 12, 3))


def tr(text: str) -> int:
    """Epoch ms of a Europe/Istanbul wall time."""
    return ms_from_datetime(dt.datetime.fromisoformat(text).replace(tzinfo=ISTANBUL))


@pytest.fixture
def store() -> Iterator[Store]:
    with Store(":memory:") as opened:
        yield opened


def add_signal(
    store: Store,
    *,
    bar_close_ms: int,
    receive_delay_ms: int,
    action: str = "entry",
    strategy_id: str = "ny_orb_fvg",
    outcome: Outcome | None = Outcome.FORWARDED,
    sent_delay_ms: int | None = None,
    alarm: str | None = None,
    undelivered: str | None = None,
) -> int:
    """Insert one signal as the receiver would, process it and optionally deliver its message.

    `undelivered`: write the message but leave it "pending", or close it as "expired" /
    "failed" (what the sender does when Telegram cannot be reached in time or refuses).
    """
    received_at_ms = bar_close_ms + receive_delay_ms
    heartbeat = action == "heartbeat"
    signal = NewSignal(
        received_at_ms=received_at_ms,
        strategy_id="heartbeat" if heartbeat else strategy_id,
        symbol="CME_MINI:MES1!",
        tf="1",
        bar_time_ms=bar_close_ms - MINUTE_MS,
        bar_close_ms=bar_close_ms,
        action=action,
        side=None if heartbeat else "long",
        setup="FVG" if action == "entry" else "",
        order_type="market" if action == "entry" else None,
        est_entry=None if heartbeat else 5123.25,
        stop=5110.75 if action == "entry" else None,
        tp=5148.25 if action == "entry" else None,
        rr=None,
        reason=None,
        payload_json="{}",
    )
    with store.transaction() as tx:
        signal_id = tx.insert_signal(signal)
        assert signal_id is not None
        if outcome is not None:
            tx.complete_signal(signal_id, outcome, received_at_ms + 5)
        if sent_delay_ms is not None:
            message_id = tx.enqueue_message(
                kind=OutboxKind.SIGNAL,
                text="[SİNYAL] …",
                now_ms=received_at_ms,
                signal_id=signal_id,
            )
            tx.mark_message_sent(message_id, bar_close_ms + sent_delay_ms)
        if undelivered is not None:
            message_id = tx.enqueue_message(
                kind=OutboxKind.SIGNAL,
                text="[SİNYAL] …",
                now_ms=received_at_ms,
                signal_id=signal_id,
                expires_at_ms=bar_close_ms + MINUTE_MS,
            )
            if undelivered == "expired":
                tx.mark_message_expired(message_id, bar_close_ms + MINUTE_MS + 1)
            elif undelivered == "failed":
                tx.mark_message_failed(message_id, error="Forbidden", now_ms=received_at_ms + 9)
            else:
                assert undelivered == "pending"
        if alarm is not None:
            tx.enqueue_message(kind=OutboxKind.ALARM, text=alarm, now_ms=received_at_ms)
    return signal_id


# --- latency statistics -------------------------------------------------------------------


def test_latency_stats_without_samples_is_none() -> None:
    assert latency_stats([]) is None


@pytest.mark.parametrize(
    ("values", "expected"),
    [
        ([1500], LatencyStats(count=1, median_ms=1500, p95_ms=1500, max_ms=1500)),
        ([3000, 1000], LatencyStats(count=2, median_ms=2000.0, p95_ms=3000, max_ms=3000)),
        ([5, 1, 3], LatencyStats(count=3, median_ms=3, p95_ms=5, max_ms=5)),
        (list(range(1, 101)), LatencyStats(count=100, median_ms=50.5, p95_ms=95, max_ms=100)),
        (list(range(1, 21)), LatencyStats(count=20, median_ms=10.5, p95_ms=19, max_ms=20)),
        ([-200, 100, 400], LatencyStats(count=3, median_ms=100, p95_ms=400, max_ms=400)),
    ],
)
def test_latency_stats(values: list[int], expected: LatencyStats) -> None:
    assert latency_stats(values) == expected


def test_day_start_is_midnight_in_istanbul() -> None:
    # Istanbul is UTC+3 all year: the day starts at 21:00 UTC of the previous day.
    assert day_start_ms(DAY_1) == ms_from_datetime(dt.datetime(2026, 11, 30, 21, 0, tzinfo=dt.UTC))
    assert day_start_ms(DAY_2) - day_start_ms(DAY_1) == 24 * 60 * MINUTE_MS


def test_every_outcome_has_a_turkish_label() -> None:
    assert set(OUTCOME_LABELS) == set(Outcome)
    assert all(label.strip() for label in OUTCOME_LABELS.values())


# --- text report --------------------------------------------------------------------------


def populated(store: Store) -> None:
    # Day 1: a forwarded entry, its stop update and exit, two heartbeats, one stale entry.
    add_signal(
        store, bar_close_ms=tr("2026-12-01 17:45"), receive_delay_ms=1200, sent_delay_ms=2000
    )
    add_signal(
        store,
        bar_close_ms=tr("2026-12-01 18:02"),
        receive_delay_ms=900,
        action="stop_update",
        sent_delay_ms=1600,
    )
    add_signal(
        store,
        bar_close_ms=tr("2026-12-01 23:59"),
        receive_delay_ms=1500,
        action="exit",
        sent_delay_ms=2400,
    )
    add_signal(store, bar_close_ms=tr("2026-12-01 18:00"), receive_delay_ms=800, action="heartbeat")
    add_signal(store, bar_close_ms=tr("2026-12-01 19:00"), receive_delay_ms=700, action="heartbeat")
    add_signal(
        store,
        bar_close_ms=tr("2026-12-01 20:10"),
        receive_delay_ms=95_000,
        strategy_id="asian_sweep",
        outcome=Outcome.STALE,
        sent_delay_ms=96_000,
    )
    # Day 2: an entry skipped for the budget, one that failed to process (with its alarm),
    # and one still waiting in the queue.
    add_signal(
        store,
        bar_close_ms=tr("2026-12-02 17:40"),
        receive_delay_ms=1100,
        outcome=Outcome.SKIP_BUDGET,
        sent_delay_ms=1900,
    )
    add_signal(
        store,
        bar_close_ms=tr("2026-12-02 18:40"),
        receive_delay_ms=1000,
        strategy_id="mtf_trend_cont",
        outcome=Outcome.ERROR,
        alarm="[ALARM] Sinyal işlenemedi: id 8\nikinci satır",
    )
    add_signal(
        store,
        bar_close_ms=tr("2026-12-02 19:40"),
        receive_delay_ms=1300,
        strategy_id="mr_bb_kc_rsi2",
        outcome=None,
    )


def test_report_counts_days_outcomes_and_latency(store: Store) -> None:
    populated(store)

    report = build_report(store, start_ms=START_MS, end_ms=END_MS, source="data/propbot.db")

    assert report == (
        "Sinyal raporu · data/propbot.db\n"
        "Dönem (Türkiye saati): 2026-12-01 00:00 → 2026-12-03 00:00\n"
        "\n"
        "Gün (TR)    alert  heartbeat  giriş  iletilen giriş  bayat  atlanan   hata  ulaşmayan\n"
        "2026-12-01      4          2      2               1      1        0      0          0\n"
        "2026-12-02      3          0      3               0      0        1      1          0\n"
        "Toplam          7          2      5               1      1        1      1          0\n"
        "\n"
        "Sonuçlar (heartbeat hariç): iletildi 3 · bayat 1 · atla: günlük bütçe 1 · "
        "işlenemedi (hata) 1 · kuyrukta bekleyen 1\n"
        "Gecikme, bar kapanışı → alıcı: medyan 1.2 sn · %95 95.0 sn · en fazla 95.0 sn (7 alert)\n"
        "Gecikme, bar kapanışı → Telegram: medyan 2.0 sn · %95 96.0 sn · en fazla 96.0 sn "
        "(5 mesaj)\n"
        "Telegram teslimi (mesaj üreten 5 sinyal): gönderildi 5 · bekliyor 0 · süresi doldu 0 · "
        "başarısız 0\n"
        "  SPEC §5.7 ölçütü (ulaşmayan ya da işlenemeyen sinyal 0 ve uçtan uca medyan < 5 sn): "
        "SAĞLANMADI — 1 sinyal işlenemedi (hata)\n"
        "Telegram kutusu (tüm zamanlar): gönderildi 5 · bekliyor 1 · süresi doldu 0 · "
        "başarısız 0\n"
        "Dönemdeki alarmlar: 1\n"
        "  2026-12-02 18:40 TR  [ALARM] Sinyal işlenemedi: id 8\n"
        "\n"
        "Karşılaştırma: TradingView → Alerts → Log'daki kayıt sayısı, yukarıdaki 'alert' ve\n"
        "'heartbeat' sütunlarının toplamına eşit olmalı (kaçan sinyal 0). Aynı alert iki kez\n"
        "kaydedilemez: (strateji, sembol, bar zamanı, eylem) anahtarı veritabanında tekildir.\n"
    )


def test_report_counts_and_names_the_automatic_skips(store: Store) -> None:
    # Different counts per outcome: swapped labels or a skip missing from the "atlanan"
    # column would show (open finding 58 tells the user to read these numbers).
    skips = [Outcome.SKIP_SYMBOL] + [Outcome.SKIP_STOP_TOO_TIGHT] * 2 + [Outcome.SKIP_STANDBY] * 3
    for minute, outcome in enumerate(skips):
        add_signal(
            store,
            bar_close_ms=tr(f"2026-12-01 17:{40 + minute}"),
            receive_delay_ms=1000,
            outcome=outcome,
            sent_delay_ms=1500,
        )

    report = build_report(store, start_ms=START_MS, end_ms=END_MS, source="db")

    assert (
        "Gün (TR)    alert  heartbeat  giriş  iletilen giriş  bayat  atlanan   hata  ulaşmayan\n"
        "2026-12-01      6          0      6               0      0        6      0          0\n"
    ) in report
    assert (
        "Sonuçlar (heartbeat hariç): atla: kopya beklemede 3 · atla: stop çok dar 2 · "
        "atla: sembol listede değil 1\n"
    ) in report


def test_report_is_limited_to_the_requested_days(store: Store) -> None:
    populated(store)

    report = build_report(store, start_ms=day_start_ms(DAY_2), end_ms=END_MS, source="db")

    assert "2026-12-01" not in report.split("\n", 3)[3]
    assert (
        "Toplam          3          0      3               0      0        1      1          0"
    ) in report
    assert "(3 alert)" in report
    assert "(1 mesaj)" in report


def test_report_without_signals_says_so(store: Store) -> None:
    report = build_report(store, start_ms=START_MS, end_ms=END_MS, source="db")

    assert report.splitlines()[-1] == "Bu dönemde alınmış alert yok."


def test_slow_delivery_fails_the_acceptance_line(store: Store) -> None:
    for minute in (40, 41, 42):
        add_signal(
            store,
            bar_close_ms=tr(f"2026-12-01 17:{minute}"),
            receive_delay_ms=1000,
            action="exit",
            sent_delay_ms=6000 + minute,
        )

    report = build_report(store, start_ms=START_MS, end_ms=END_MS, source="db")

    assert verdict(report) == "SAĞLANMADI"


def verdict(report: str) -> str:
    prefix = (
        "  SPEC §5.7 ölçütü (ulaşmayan ya da işlenemeyen sinyal 0 ve uçtan uca medyan < 5 sn): "
    )
    (line,) = (line for line in report.splitlines() if line.startswith(prefix))
    return line.removeprefix(prefix)


def test_without_any_message_there_is_no_verdict(store: Store) -> None:
    add_signal(
        store,
        bar_close_ms=tr("2026-12-01 17:45"),
        receive_delay_ms=1000,
        action="stop_update",
        outcome=Outcome.FILTERED_MIN_STEP,
    )

    report = build_report(store, start_ms=START_MS, end_ms=END_MS, source="db")

    assert "Gecikme, bar kapanışı → Telegram: örnek yok" in report
    assert "Telegram teslimi (mesaj üreten 0 sinyal): gönderildi 0 · bekliyor 0" in report
    assert verdict(report) == "veri yok"


def test_an_entry_whose_message_never_went_out_is_not_a_delivered_entry(store: Store) -> None:
    # Telegram answers the first entry and then times out: the next two messages expire.
    # "Forwarded" is only the processor's decision; nothing reached the trader.
    add_signal(
        store, bar_close_ms=tr("2026-12-01 17:45"), receive_delay_ms=1000, sent_delay_ms=1500
    )
    for minute in (50, 55):
        add_signal(
            store,
            bar_close_ms=tr(f"2026-12-01 17:{minute}"),
            receive_delay_ms=1000,
            strategy_id="asian_sweep",
            undelivered="expired",
        )

    report = build_report(store, start_ms=START_MS, end_ms=END_MS, source="db")

    lines = report.splitlines()
    assert lines[3:6] == [
        "Gün (TR)    alert  heartbeat  giriş  iletilen giriş  bayat  atlanan   hata  ulaşmayan",
        "2026-12-01      3          0      3               1      0        0      0          2",
        "Toplam          3          0      3               1      0        0      0          2",
    ]
    assert "Sonuçlar (heartbeat hariç): iletildi 3" in lines  # the decision, not the delivery
    assert (
        "Gecikme, bar kapanışı → Telegram: medyan 1.5 sn · %95 1.5 sn · en fazla 1.5 sn (1 mesaj)"
    ) in lines
    assert (
        "Telegram teslimi (mesaj üreten 3 sinyal): gönderildi 1 · bekliyor 0 · süresi doldu 2 · "
        "başarısız 0"
    ) in lines
    # The median alone (1.5 s over the one survivor) would pass.
    assert verdict(report) == "SAĞLANMADI — 2 sinyalin mesajı Telegram'a ulaşmadı"


@pytest.mark.parametrize(
    ("undelivered", "expected"),
    [
        ("failed", "SAĞLANMADI — 1 sinyalin mesajı Telegram'a ulaşmadı"),
        ("pending", "EKSİK — 1 mesaj hâlâ bekliyor; raporu sonra yeniden al"),
    ],
)
def test_one_message_that_is_not_out_keeps_the_criterion_from_being_met(
    store: Store, undelivered: str, expected: str
) -> None:
    for minute in (40, 41, 42):
        add_signal(
            store,
            bar_close_ms=tr(f"2026-12-01 17:{minute}"),
            receive_delay_ms=1000,
            action="exit",
            sent_delay_ms=1200,
        )
    add_signal(
        store,
        bar_close_ms=tr("2026-12-01 17:50"),
        receive_delay_ms=1000,
        undelivered=undelivered,
    )

    report = build_report(store, start_ms=START_MS, end_ms=END_MS, source="db")

    assert verdict(report) == expected


def delivered_exits(store: Store) -> None:
    """Three exits that reached Telegram within 1.2 s: on their own the criterion is met."""
    for minute in (40, 41, 42):
        add_signal(
            store,
            bar_close_ms=tr(f"2026-12-01 17:{minute}"),
            receive_delay_ms=1000,
            action="exit",
            sent_delay_ms=1200,
        )


def test_everything_delivered_in_time_meets_the_criterion(store: Store) -> None:
    delivered_exits(store)

    report = build_report(store, start_ms=START_MS, end_ms=END_MS, source="db")

    assert verdict(report) == "SAĞLANDI"


@pytest.mark.parametrize(
    ("outcome", "expected"),
    [
        # Processing failed: no message was ever written, the trader only got an alarm.
        (Outcome.ERROR, "SAĞLANMADI — 1 sinyal işlenemedi (hata)"),
        # Not processed yet: nothing can be said about it.
        (None, "EKSİK — 1 sinyal kuyrukta bekliyor; raporu sonra yeniden al"),
    ],
)
def test_a_signal_without_any_message_keeps_the_criterion_from_being_met(
    store: Store, outcome: Outcome | None, expected: str
) -> None:
    delivered_exits(store)
    add_signal(store, bar_close_ms=tr("2026-12-01 17:50"), receive_delay_ms=1000, outcome=outcome)

    report = build_report(store, start_ms=START_MS, end_ms=END_MS, source="db")

    assert verdict(report) == expected


def test_the_verdict_names_everything_that_was_lost_or_is_still_open(store: Store) -> None:
    delivered_exits(store)
    add_signal(
        store, bar_close_ms=tr("2026-12-01 17:50"), receive_delay_ms=1000, undelivered="expired"
    )
    add_signal(
        store, bar_close_ms=tr("2026-12-01 17:51"), receive_delay_ms=1000, outcome=Outcome.ERROR
    )
    add_signal(
        store, bar_close_ms=tr("2026-12-01 17:52"), receive_delay_ms=1000, undelivered="pending"
    )
    add_signal(store, bar_close_ms=tr("2026-12-01 17:53"), receive_delay_ms=1000, outcome=None)

    lost = verdict(build_report(store, start_ms=START_MS, end_ms=END_MS, source="db"))

    assert lost == (
        "SAĞLANMADI — 1 sinyalin mesajı Telegram'a ulaşmadı · 1 sinyal işlenemedi (hata)"
    )

    # Without the two lost ones, the two open ones remain.
    with Store(":memory:") as other:
        delivered_exits(other)
        add_signal(
            other,
            bar_close_ms=tr("2026-12-01 17:52"),
            receive_delay_ms=1000,
            undelivered="pending",
        )
        add_signal(other, bar_close_ms=tr("2026-12-01 17:53"), receive_delay_ms=1000, outcome=None)
        still_open = verdict(build_report(other, start_ms=START_MS, end_ms=END_MS, source="db"))

    assert still_open == (
        "EKSİK — 1 mesaj hâlâ bekliyor · 1 sinyal kuyrukta bekliyor; raporu sonra yeniden al"
    )


def test_a_heartbeat_that_was_not_processed_yet_does_not_count(store: Store) -> None:
    delivered_exits(store)
    add_signal(
        store,
        bar_close_ms=tr("2026-12-01 18:00"),
        receive_delay_ms=800,
        action="heartbeat",
        outcome=None,
    )

    report = build_report(store, start_ms=START_MS, end_ms=END_MS, source="db")

    assert verdict(report) == "SAĞLANDI"


def test_a_refused_message_outweighs_a_pending_one(store: Store) -> None:
    add_signal(
        store, bar_close_ms=tr("2026-12-01 17:40"), receive_delay_ms=1000, undelivered="pending"
    )
    add_signal(
        store, bar_close_ms=tr("2026-12-01 17:41"), receive_delay_ms=1000, undelivered="failed"
    )

    report = build_report(store, start_ms=START_MS, end_ms=END_MS, source="db")

    assert verdict(report) == "SAĞLANMADI — 1 sinyalin mesajı Telegram'a ulaşmadı"


def test_long_alarm_lists_are_cut(store: Store) -> None:
    for index in range(25):
        add_signal(
            store,
            bar_close_ms=tr("2026-12-01 10:00") + index * MINUTE_MS,
            receive_delay_ms=500,
            action="exit",
            alarm=f"[ALARM] {index}",
        )

    lines = build_report(store, start_ms=START_MS, end_ms=END_MS, source="db").splitlines()

    assert "Dönemdeki alarmlar: 25" in lines
    assert "  … ve 5 alarm daha" in lines
    assert sum(line.endswith(tuple(f"[ALARM] {i}" for i in range(25))) for line in lines) == 20


# --- CSV ----------------------------------------------------------------------------------


def test_csv_has_one_row_per_signal_with_latencies(store: Store) -> None:
    populated(store)

    rows = list(csv.reader(io.StringIO(signals_csv(store, start_ms=START_MS, end_ms=END_MS))))

    assert tuple(rows[0]) == CSV_HEADER
    assert len(rows) == 1 + 9
    first = dict(zip(CSV_HEADER, rows[1], strict=True))
    assert first == {
        "id": "1",
        "received_at_utc": "2026-12-01T14:45:01.200Z",
        "strategy_id": "ny_orb_fvg",
        "symbol": "CME_MINI:MES1!",
        "tf": "1",
        "action": "entry",
        "side": "long",
        "bar_time_utc": "2026-12-01T14:44:00.000Z",
        "bar_close_utc": "2026-12-01T14:45:00.000Z",
        "outcome": "forwarded",
        "receive_latency_ms": "1200",
        "telegram_latency_ms": "2000",
        "delivery": "sent",
    }
    heartbeat = dict(zip(CSV_HEADER, rows[4], strict=True))
    assert (heartbeat["action"], heartbeat["side"], heartbeat["telegram_latency_ms"]) == (
        "heartbeat",
        "",
        "",
    )
    queued = dict(zip(CSV_HEADER, rows[9], strict=True))
    assert (queued["strategy_id"], queued["outcome"]) == ("mr_bb_kc_rsi2", "")
    assert [row[-1] for row in rows[1:]] == ["sent", "sent", "sent", "", "", "sent", "sent", "", ""]


@pytest.mark.parametrize("undelivered", ["pending", "expired", "failed"])
def test_csv_says_what_became_of_the_message(store: Store, undelivered: str) -> None:
    add_signal(
        store,
        bar_close_ms=tr("2026-12-01 17:45"),
        receive_delay_ms=1000,
        undelivered=undelivered,
    )

    rows = list(csv.reader(io.StringIO(signals_csv(store, start_ms=START_MS, end_ms=END_MS))))

    row = dict(zip(CSV_HEADER, rows[1], strict=True))
    assert (row["outcome"], row["telegram_latency_ms"], row["delivery"]) == (
        "forwarded",
        "",
        undelivered,
    )


def test_csv_of_an_empty_period_is_only_the_header(store: Store) -> None:
    assert signals_csv(store, start_ms=START_MS, end_ms=END_MS) == ",".join(CSV_HEADER) + "\n"
