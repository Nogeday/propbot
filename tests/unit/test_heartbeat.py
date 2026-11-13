"""Alert-line monitor: silence, downtime and expiry reminders (SPEC §5.5, contract §6.1)."""

import asyncio
import datetime as dt
from collections.abc import Callable, Coroutine, Iterator, Sequence
from typing import Any

import pytest
import structlog

from propbot.core.clock import ms_from_datetime
from propbot.notify import messages
from propbot.signals.heartbeat import (
    DOWNTIME_THRESHOLD_MS,
    check_alert_line,
    run_heartbeat_monitor,
)
from propbot.signals.tvconfig import TvAlertsConfig
from propbot.store.db import NewSignal, OutboxKind, OutboxRow, StateKey, Store, StoreError

SECOND_MS = 1_000
MINUTE_MS = 60 * SECOND_MS
HOUR_MS = 60 * MINUTE_MS
DAY_MS = 24 * HOUR_MS
MAX_SILENCE_MS = 75 * MINUTE_MS

WEEKDAYS = ["mon", "tue", "wed", "thu", "fri"]
DEFAULT_WINDOWS = [(WEEKDAYS, "03:00", "16:00")]
SUNDAY_ONLY = [(["sun"], "03:00", "16:00")]

Window = tuple[list[str], str, str]
Alert = tuple[str, str | None]


def utc(text: str) -> int:
    return ms_from_datetime(dt.datetime.fromisoformat(text).replace(tzinfo=dt.UTC))


def tv_config(
    windows: Sequence[Window] = DEFAULT_WINDOWS,
    *,
    alerts: Sequence[Alert] = (),
    max_silence_minutes: int = 75,
    expiry_reminder_days: int = 7,
) -> TvAlertsConfig:
    return TvAlertsConfig.model_validate(
        {
            "webhook": {
                "rules_verified_at": dt.date(2026, 11, 13),
                "source_ips": ["52.89.214.238"],
                "allowed_ports": [80, 443],
                "response_timeout_seconds": 3,
                "mtls_supported": False,
            },
            "receiver": {
                "trusted_proxies": ["127.0.0.1"],
                "client_ip_header": "X-Forwarded-For",
                "rate_limit_per_minute": 30,
                "max_body_bytes": 4096,
            },
            "heartbeat": {
                "max_silence_minutes": max_silence_minutes,
                "watch_windows": [
                    {"days": days, "start": start, "end": end} for days, start, end in windows
                ],
            },
            "alerts": [{"name": name, "expires_on": expires_on} for name, expires_on in alerts],
            "expiry_reminder_days": expiry_reminder_days,
        }
    )


CONFIG = tv_config()

# Monday 2026-11-16, New York on winter time (UTC-5): the window 03:00-16:00 is 08:00-21:00 UTC.
WINDOW_OPEN_MS = utc("2026-11-16 08:00")
WINDOW_CLOSE_MS = utc("2026-11-16 21:00")
LONG_AGO_MS = utc("2026-11-01 00:00")


@pytest.fixture
def store() -> Iterator[Store]:
    structlog.reset_defaults()
    with Store(":memory:") as opened:
        yield opened


def tick(
    store: Store,
    now_ms: int,
    config: TvAlertsConfig = CONFIG,
    *,
    started_ms: int = LONG_AGO_MS,
    first_tick: bool = False,
) -> int:
    with store.transaction() as tx:
        return check_alert_line(
            tx, config, now_ms=now_ms, started_ms=started_ms, first_tick=first_tick
        )


def alert_received(store: Store, received_at_ms: int, *, action: str = "heartbeat") -> None:
    """Store a valid alert the way the receiver does (the monitor only looks at arrival time)."""
    is_heartbeat = action == "heartbeat"
    signal = NewSignal(
        received_at_ms=received_at_ms,
        strategy_id="heartbeat" if is_heartbeat else "ny_orb_fvg",
        symbol="CME_MINI:MES1!",
        tf="15",
        bar_time_ms=received_at_ms - 15 * MINUTE_MS,
        bar_close_ms=received_at_ms,
        action=action,
        side=None if is_heartbeat else "long",
        setup="",
        order_type=None,
        est_entry=None,
        stop=None,
        tp=None,
        rr=None,
        reason=None,
        payload_json="{}",
    )
    with store.transaction() as tx:
        assert tx.insert_signal(signal) is not None


def outbox(store: Store) -> list[OutboxRow]:
    with store.transaction() as tx:
        return tx.messages_created_between(0, 2**62)


def state(store: Store, key: StateKey) -> str | None:
    with store.transaction() as tx:
        return tx.get_state(key)


def set_state(store: Store, key: StateKey, value: str) -> None:
    with store.transaction() as tx:
        tx.set_state(key, value, 0)


# --- silence ------------------------------------------------------------------------------


def test_silence_inside_a_window_raises_one_alarm(store: Store) -> None:
    last_alert_ms = utc("2026-11-16 14:00")
    alert_received(store, last_alert_ms)

    at_limit = tick(store, last_alert_ms + MAX_SILENCE_MS)
    beyond = tick(store, last_alert_ms + MAX_SILENCE_MS + 1)

    assert (at_limit, beyond) == (0, 1)
    (alarm,) = outbox(store)
    assert alarm.kind is OutboxKind.ALARM
    assert alarm.signal_id is None
    assert alarm.expires_at_ms is None
    assert alarm.created_at_ms == last_alert_ms + MAX_SILENCE_MS + 1
    assert alarm.text == (
        "[ALARM] TradingView alarm hattı sessiz: son 1 sa 15 dk içinde hiçbir alert gelmedi "
        "(sınır 75 dk).\n"
        "Sinyaller de GELMİYOR olabilir: TradingView'da alarmların aktif olduğunu (heartbeat "
        "göstergesi dahil) ve Alert Log'u kontrol et; açık pozisyonun varsa stop/çıkışı "
        "TradingView'dan izle.\n"
        "Tatil ya da erken kapanış günüyse yok say."
    )
    assert state(store, StateKey.HEARTBEAT_SILENT) == "1"
    assert state(store, StateKey.HEARTBEAT_ALARM_MS) == str(last_alert_ms + MAX_SILENCE_MS + 1)


def test_any_valid_alert_counts_as_a_sign_of_life(store: Store) -> None:
    alert_received(store, utc("2026-11-16 13:00"))
    alert_received(store, utc("2026-11-16 14:00"), action="entry")

    assert tick(store, utc("2026-11-16 15:15")) == 0
    assert tick(store, utc("2026-11-16 15:15:00.001")) == 1


@pytest.mark.parametrize(
    "now",
    [
        "2026-11-15 15:00",  # Sunday
        "2026-11-16 07:59:59.999",  # Monday, a millisecond before the window opens
        "2026-11-16 22:15",  # Monday, the grace after the close has just ended
        "2026-11-17 02:00",  # Monday evening in New York
        "2026-11-14 15:00",  # Saturday
    ],
)
def test_silence_outside_the_watch_windows_is_not_reported(store: Store, now: str) -> None:
    alert_received(store, utc("2026-11-10 12:00"))  # days of silence

    assert tick(store, utc(now)) == 0
    assert outbox(store) == []
    assert state(store, StateKey.HEARTBEAT_SILENT) is None


def test_silence_is_counted_from_the_window_start_when_the_last_alert_is_older(
    store: Store,
) -> None:
    alert_received(store, utc("2026-11-15 23:00"))  # Sunday evening: nobody was watching

    assert tick(store, WINDOW_OPEN_MS + MAX_SILENCE_MS) == 0
    assert tick(store, WINDOW_OPEN_MS + MAX_SILENCE_MS + 1) == 1
    assert "son 1 sa 15 dk içinde" in outbox(store)[0].text


def test_an_empty_database_counts_from_the_window_start_when_started_earlier(store: Store) -> None:
    assert tick(store, WINDOW_OPEN_MS + MAX_SILENCE_MS, started_ms=LONG_AGO_MS) == 0
    assert tick(store, WINDOW_OPEN_MS + MAX_SILENCE_MS + 1, started_ms=LONG_AGO_MS) == 1


def test_an_empty_database_counts_from_the_process_start_inside_a_window(store: Store) -> None:
    started_ms = utc("2026-11-16 10:00")

    assert tick(store, started_ms + MAX_SILENCE_MS, started_ms=started_ms) == 0
    assert tick(store, started_ms + MAX_SILENCE_MS + 1, started_ms=started_ms) == 1
    assert "son 1 sa 15 dk içinde" in outbox(store)[0].text


def test_a_restart_does_not_hide_silence(store: Store) -> None:
    alert_received(store, utc("2026-11-16 08:30"))
    restarted_ms = utc("2026-11-16 11:00")

    enqueued = tick(store, restarted_ms + 5 * MINUTE_MS, started_ms=restarted_ms, first_tick=True)

    assert enqueued == 1
    assert "son 2 sa 35 dk içinde" in outbox(store)[0].text


def test_a_line_that_dies_in_the_last_hour_is_reported_after_the_close(store: Store) -> None:
    last_alert_ms = utc("2026-11-16 20:10")  # 15:10 New York, 50 minutes before the close
    alert_received(store, last_alert_ms)

    assert tick(store, WINDOW_CLOSE_MS - 1) == 0
    assert tick(store, WINDOW_CLOSE_MS) == 0
    assert tick(store, last_alert_ms + MAX_SILENCE_MS) == 0
    assert tick(store, last_alert_ms + MAX_SILENCE_MS + 1) == 1  # 21:25 UTC, window closed
    (alarm,) = outbox(store)
    assert alarm.kind is OutboxKind.ALARM
    # After the grace nothing more is said until the next window.
    assert tick(store, WINDOW_CLOSE_MS + MAX_SILENCE_MS) == 0
    assert tick(store, utc("2026-11-17 07:59")) == 0
    assert len(outbox(store)) == 1


def test_an_alert_after_the_close_ends_the_day_quietly(store: Store) -> None:
    alert_received(store, WINDOW_CLOSE_MS + 5 * MINUTE_MS)

    assert tick(store, WINDOW_CLOSE_MS + MAX_SILENCE_MS - 1) == 0
    assert outbox(store) == []


def test_a_start_after_the_close_does_not_report_the_closed_window(store: Store) -> None:
    started_ms = WINDOW_CLOSE_MS + MINUTE_MS

    assert tick(store, WINDOW_CLOSE_MS + MAX_SILENCE_MS - 1, started_ms=started_ms) == 0
    assert outbox(store) == []


def test_the_alarm_repeats_once_per_silence_limit(store: Store) -> None:
    first_ms = WINDOW_OPEN_MS + MAX_SILENCE_MS + 1

    assert tick(store, first_ms) == 1
    assert tick(store, first_ms + 30 * SECOND_MS) == 0
    assert tick(store, first_ms + 30 * MINUTE_MS) == 0
    assert tick(store, first_ms + MAX_SILENCE_MS - 1) == 0
    assert tick(store, first_ms + MAX_SILENCE_MS) == 1

    first, second = outbox(store)
    assert (first.kind, second.kind) == (OutboxKind.ALARM, OutboxKind.ALARM)
    assert "son 1 sa 15 dk içinde" in first.text
    assert "son 2 sa 30 dk içinde" in second.text
    assert state(store, StateKey.HEARTBEAT_SILENT) == "1"
    assert state(store, StateKey.HEARTBEAT_ALARM_MS) == str(first_ms + MAX_SILENCE_MS)


def test_the_silence_limit_comes_from_the_config(store: Store) -> None:
    config = tv_config(max_silence_minutes=20)

    assert tick(store, WINDOW_OPEN_MS + 20 * MINUTE_MS, config) == 0
    assert tick(store, WINDOW_OPEN_MS + 20 * MINUTE_MS + 1, config) == 1
    assert "son 20 dk içinde hiçbir alert gelmedi (sınır 20 dk)" in outbox(store)[0].text
    # The grace after the close is the same limit.
    assert tick(store, WINDOW_CLOSE_MS + 20 * MINUTE_MS - 1, config) == 1
    assert tick(store, WINDOW_CLOSE_MS + 20 * MINUTE_MS, config) == 0


@pytest.mark.parametrize("stored_value", ["garbage", "", str(utc("2026-11-16 12:00"))])
def test_an_unreadable_or_future_alarm_time_never_silences_the_alarm(
    store: Store, stored_value: str
) -> None:
    # "Future": the clock stepped back after the last alarm.
    set_state(store, StateKey.HEARTBEAT_ALARM_MS, stored_value)

    assert tick(store, WINDOW_OPEN_MS + MAX_SILENCE_MS + 1) == 1


# --- recovery -----------------------------------------------------------------------------


def test_an_alert_after_the_alarm_is_announced_once(store: Store) -> None:
    alarm_ms = WINDOW_OPEN_MS + MAX_SILENCE_MS + 1
    assert tick(store, alarm_ms) == 1
    alert_received(store, alarm_ms + 10 * MINUTE_MS)

    assert tick(store, alarm_ms + 10 * MINUTE_MS + 30 * SECOND_MS) == 1
    assert tick(store, alarm_ms + 11 * MINUTE_MS) == 0

    alarm, notice = outbox(store)
    assert alarm.kind is OutboxKind.ALARM
    assert notice.kind is OutboxKind.NOTICE
    assert notice.signal_id is None
    assert notice.text == "[BİLGİ] TradingView alarm hattı geri geldi: yeniden alert alınıyor."
    assert notice.text == messages.heartbeat_recovered_notice()
    assert state(store, StateKey.HEARTBEAT_SILENT) is None


def test_silence_after_a_recovery_is_alarmed_again(store: Store) -> None:
    alarm_ms = WINDOW_OPEN_MS + MAX_SILENCE_MS + 1
    tick(store, alarm_ms)
    back_ms = alarm_ms + 10 * MINUTE_MS
    alert_received(store, back_ms)
    tick(store, back_ms + 30 * SECOND_MS)

    assert tick(store, back_ms + MAX_SILENCE_MS) == 0
    assert tick(store, back_ms + MAX_SILENCE_MS + 1) == 1
    assert [row.kind for row in outbox(store)] == [
        OutboxKind.ALARM,
        OutboxKind.NOTICE,
        OutboxKind.ALARM,
    ]
    assert state(store, StateKey.HEARTBEAT_SILENT) == "1"


def test_recovery_is_announced_outside_a_window_too(store: Store) -> None:
    friday_alarm_ms = utc("2026-11-13 20:59")
    assert tick(store, friday_alarm_ms) == 1
    alert_received(store, utc("2026-11-14 10:00"))  # Saturday

    assert tick(store, utc("2026-11-14 10:00:30")) == 1
    assert [row.kind for row in outbox(store)] == [OutboxKind.ALARM, OutboxKind.NOTICE]


def test_an_alert_in_the_same_millisecond_as_the_alarm_counts_as_recovery(store: Store) -> None:
    alarm_ms = WINDOW_OPEN_MS + MAX_SILENCE_MS + 1
    tick(store, alarm_ms)
    alert_received(store, alarm_ms)  # stored right after the tick's transaction

    assert tick(store, alarm_ms + 30 * SECOND_MS) == 1
    assert outbox(store)[-1].kind is OutboxKind.NOTICE


def test_an_older_alert_is_no_recovery(store: Store) -> None:
    alert_received(store, utc("2026-11-16 09:00"))
    alarm_ms = utc("2026-11-16 10:15:00.001")
    assert tick(store, alarm_ms) == 1

    assert tick(store, alarm_ms + 30 * SECOND_MS) == 0
    assert state(store, StateKey.HEARTBEAT_SILENT) == "1"


def test_no_recovery_notice_without_an_alarm(store: Store) -> None:
    alert_received(store, utc("2026-11-16 09:00"))

    assert tick(store, utc("2026-11-16 09:01")) == 0
    assert outbox(store) == []


def test_recovery_and_new_silence_can_fall_into_one_tick(store: Store) -> None:
    alarm_ms = WINDOW_OPEN_MS + MAX_SILENCE_MS + 1
    tick(store, alarm_ms)
    alert_received(store, alarm_ms + MINUTE_MS)

    # The monitor did not tick for two hours (the event loop was blocked, say).
    assert tick(store, alarm_ms + 2 * HOUR_MS) == 2
    assert [row.kind for row in outbox(store)] == [
        OutboxKind.ALARM,
        OutboxKind.NOTICE,
        OutboxKind.ALARM,
    ]
    assert state(store, StateKey.HEARTBEAT_SILENT) == "1"


# --- daylight saving ------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("windows", "opens_utc", "closes_utc"),
    [
        # Mon-Fri 03:00-16:00 New York on both sides of each US change.
        (DEFAULT_WINDOWS, "2026-03-06 08:00", "2026-03-06 21:00"),  # Friday, EST
        (DEFAULT_WINDOWS, "2026-03-09 07:00", "2026-03-09 20:00"),  # Monday, EDT
        (DEFAULT_WINDOWS, "2026-10-30 07:00", "2026-10-30 20:00"),  # Friday, EDT
        (DEFAULT_WINDOWS, "2026-11-02 08:00", "2026-11-02 21:00"),  # Monday, EST
        # The change days themselves (Sundays): 01:00-05:00 is 3 and 5 hours long.
        ([(["sun"], "01:00", "05:00")], "2026-03-08 06:00", "2026-03-08 09:00"),
        ([(["sun"], "01:00", "05:00")], "2026-11-01 05:00", "2026-11-01 10:00"),
    ],
)
def test_windows_follow_new_york_time_across_daylight_saving_changes(
    windows: list[Window], opens_utc: str, closes_utc: str
) -> None:
    config = tv_config(windows)
    opens_ms, closes_ms = utc(opens_utc), utc(closes_utc)

    def enqueued_at(now_ms: int) -> int:
        with Store(":memory:") as fresh:
            return tick(fresh, now_ms, config, started_ms=opens_ms - DAY_MS)

    assert enqueued_at(opens_ms - 1) == 0  # not open yet: yesterday's window is long gone
    assert enqueued_at(opens_ms + MAX_SILENCE_MS) == 0
    assert enqueued_at(opens_ms + MAX_SILENCE_MS + 1) == 1
    assert enqueued_at(closes_ms + MAX_SILENCE_MS - 1) == 1
    assert enqueued_at(closes_ms + MAX_SILENCE_MS) == 0


# --- downtime -----------------------------------------------------------------------------


def alive_until(store: Store, last_tick_ms: int) -> None:
    """The previous process: an alert shortly before its last tick."""
    alert_received(store, last_tick_ms - MINUTE_MS)
    assert tick(store, last_tick_ms) == 0
    assert state(store, StateKey.LAST_ALIVE_MS) == str(last_tick_ms)


def test_downtime_inside_a_window_is_reported_on_the_first_tick(store: Store) -> None:
    down_from_ms = utc("2026-11-16 14:10")
    back_ms = utc("2026-11-16 14:25:40")
    alive_until(store, down_from_ms)

    assert tick(store, back_ms, started_ms=back_ms, first_tick=True) == 1

    (alarm,) = outbox(store)
    assert alarm.kind is OutboxKind.ALARM
    assert alarm.text == (
        "[ALARM] Bot 17:10–17:25 TR arasında kapalıydı; bu aralıktaki TradingView alarmları "
        "alınmadı. Açık pozisyonun varsa stop/çıkışı TradingView'dan kontrol et."
    )
    assert alarm.text == messages.downtime_alarm(down_from_ms, back_ms)
    assert state(store, StateKey.LAST_ALIVE_MS) == str(back_ms)


def test_a_short_restart_inside_a_window_is_reported_as_a_restart(store: Store) -> None:
    # Every restart is a hole in which TradingView gets an error and the alert is lost.
    assert DOWNTIME_THRESHOLD_MS == 90 * SECOND_MS
    last_alive_ms = utc("2026-11-16 14:10")
    back_ms = last_alive_ms + 90 * SECOND_MS
    alive_until(store, last_alive_ms)

    assert tick(store, back_ms, first_tick=True) == 1

    (alarm,) = outbox(store)
    assert alarm.kind is OutboxKind.ALARM
    assert alarm.text == (
        "[ALARM] Bot 17:11:30 TR'de yeniden başladı (en fazla 90 sn kapalı kaldı); bu arada "
        "gelen bir TradingView alarmı alınmamış olabilir. Açık pozisyonun varsa stop/çıkışı "
        "TradingView'dan kontrol et."
    )
    assert alarm.text == messages.restart_alarm(last_alive_ms, back_ms, restarts=1)
    assert state(store, StateKey.RESTARTS_UNREPORTED) is None


def test_ninety_one_seconds_are_downtime(store: Store) -> None:
    down_from_ms = utc("2026-11-16 14:10")
    alive_until(store, down_from_ms)

    assert tick(store, down_from_ms + 90 * SECOND_MS + 1, first_tick=True) == 1

    (alarm,) = outbox(store)
    assert alarm.text.startswith("[ALARM] Bot 17:10–17:11 TR arasında kapalıydı;")


def test_a_crash_loop_is_reported_once_per_half_hour_with_its_restart_count(store: Store) -> None:
    started_ms = utc("2026-11-16 14:10")
    alive_until(store, started_ms)

    # A process that dies every 10 seconds: each start is the first tick of a new process.
    enqueued = [
        tick(store, started_ms + round_no * 10 * SECOND_MS, first_tick=True)
        for round_no in range(1, 181)  # 30 minutes
    ]

    assert enqueued == [1] + [0] * 179
    assert state(store, StateKey.RESTARTS_UNREPORTED) == "179"

    # 30 minutes after the first alarm the next restart is reported, with what was skipped.
    assert tick(store, started_ms + 181 * 10 * SECOND_MS, first_tick=True) == 1

    first, second = outbox(store)
    assert "yeniden başladı (en fazla 10 sn kapalı kaldı)" in first.text
    assert second.kind is OutboxKind.ALARM
    assert second.text == (
        "[ALARM] Bot 17:40:10 TR'de yeniden başladı: son yeniden başlama alarmından beri 180. "
        "kez (çökme döngüsü olabilir; sunucuda: journalctl -u propbot). Aralarda gelen "
        "TradingView alarmları alınmamış olabilir. Açık pozisyonun varsa stop/çıkışı "
        "TradingView'dan kontrol et."
    )
    assert state(store, StateKey.RESTARTS_UNREPORTED) is None


def test_a_restart_outside_the_watch_windows_is_silent(store: Store) -> None:
    # Monday 22:30 UTC = 17:30 New York: the window closed at 16:00.
    last_alive_ms = utc("2026-11-16 22:30")
    set_state(store, StateKey.LAST_ALIVE_MS, str(last_alive_ms))

    assert tick(store, last_alive_ms + 20 * SECOND_MS, first_tick=True) == 0
    assert outbox(store) == []
    assert state(store, StateKey.RESTARTS_UNREPORTED) is None


@pytest.mark.parametrize("stored_count", ["garbage", "", "1.5"])
def test_an_unreadable_restart_count_starts_again_at_one(store: Store, stored_count: str) -> None:
    last_alive_ms = utc("2026-11-16 14:10")
    alive_until(store, last_alive_ms)
    set_state(store, StateKey.RESTARTS_UNREPORTED, stored_count)

    assert tick(store, last_alive_ms + 5 * SECOND_MS, first_tick=True) == 1

    (alarm,) = outbox(store)
    assert alarm.text == messages.restart_alarm(
        last_alive_ms, last_alive_ms + 5 * SECOND_MS, restarts=1
    )


@pytest.mark.parametrize("step_ms", [0, -1, -5 * SECOND_MS])
def test_a_clock_that_did_not_move_forward_is_not_a_restart(store: Store, step_ms: int) -> None:
    last_alive_ms = utc("2026-11-16 14:10")
    alive_until(store, last_alive_ms)

    assert tick(store, last_alive_ms + step_ms, first_tick=True) == 0
    assert outbox(store) == []
    assert state(store, StateKey.RESTARTS_UNREPORTED) is None


def test_a_gap_between_later_ticks_is_not_reported_as_downtime(store: Store) -> None:
    down_from_ms = utc("2026-11-16 14:10")
    alive_until(store, down_from_ms)

    # Not the first tick: the process was alive, only slow.
    assert tick(store, down_from_ms + 30 * MINUTE_MS, first_tick=False) == 0
    assert state(store, StateKey.LAST_ALIVE_MS) == str(down_from_ms + 30 * MINUTE_MS)


def test_the_very_first_start_has_no_downtime(store: Store) -> None:
    now_ms = utc("2026-11-16 14:10")

    assert tick(store, now_ms, started_ms=now_ms, first_tick=True) == 0
    assert state(store, StateKey.LAST_ALIVE_MS) == str(now_ms)


@pytest.mark.parametrize(
    ("down_from", "back", "expected"),
    [
        ("2026-11-16 22:30", "2026-11-17 02:00", 0),  # Monday evening in New York
        ("2026-11-14 10:00", "2026-11-15 22:00", 0),  # the weekend
        ("2026-11-13 21:00", "2026-11-16 08:00", 0),  # from Friday's close to Monday's open
        ("2026-11-13 20:59:59.999", "2026-11-14 12:00", 1),  # the last millisecond of Friday
        ("2026-11-15 20:00", "2026-11-16 08:00:00.001", 1),  # the first millisecond of Monday
        ("2026-11-13 22:00", "2026-11-17 02:00", 1),  # the whole Monday window
        ("2026-11-01 00:00", "2026-11-14 12:00", 1),  # two weeks, restarted on a Saturday
        ("2026-11-07 10:00", "2026-11-15 23:00", 1),  # Saturday to the Sunday after next
    ],
)
def test_downtime_is_reported_only_when_it_overlaps_a_watch_window(
    store: Store, down_from: str, back: str, expected: int
) -> None:
    set_state(store, StateKey.LAST_ALIVE_MS, str(utc(down_from)))
    # An alert just before the restart keeps the silence rule out of the picture.
    alert_received(store, utc(back) - SECOND_MS)

    assert tick(store, utc(back), first_tick=True) == expected
    assert [row.kind for row in outbox(store)] == [OutboxKind.ALARM] * expected


@pytest.mark.parametrize("stored_value", ["garbage", "", "12.5"])
def test_an_unreadable_last_tick_is_ignored(store: Store, stored_value: str) -> None:
    set_state(store, StateKey.LAST_ALIVE_MS, stored_value)
    now_ms = utc("2026-11-16 14:10")
    alert_received(store, now_ms - SECOND_MS)

    assert tick(store, now_ms, first_tick=True) == 0
    assert state(store, StateKey.LAST_ALIVE_MS) == str(now_ms)


def test_a_clock_that_stepped_back_is_not_downtime(store: Store) -> None:
    now_ms = utc("2026-11-16 14:10")
    set_state(store, StateKey.LAST_ALIVE_MS, str(now_ms + HOUR_MS))
    alert_received(store, now_ms - SECOND_MS)

    assert tick(store, now_ms, first_tick=True) == 0


# --- alert expiry -------------------------------------------------------------------------

EXPIRING: list[Alert] = [
    ("NY ORB FVG", "2026-11-23"),
    ("Asian Sweep", "2026-11-24"),
    ("heartbeat", "2026-11-14"),
    ("MTF Trend", None),  # no expiry on this plan
]
EXPIRY_CONFIG = tv_config(SUNDAY_ONLY, alerts=EXPIRING)
# Monday 2026-11-16, 12:00 in Istanbul; no watch window is open in this config.
NOON_TR_MS = utc("2026-11-16 09:00")


def test_expiring_and_expired_alerts_are_listed_once_per_istanbul_day(store: Store) -> None:
    assert tick(store, NOON_TR_MS, EXPIRY_CONFIG) == 1
    assert tick(store, NOON_TR_MS + 30 * SECOND_MS, EXPIRY_CONFIG) == 0
    # 23:59:59.999 in Istanbul: still the same day.
    assert tick(store, utc("2026-11-16 20:59:59.999"), EXPIRY_CONFIG) == 0

    (notice,) = outbox(store)
    assert notice.kind is OutboxKind.NOTICE
    assert notice.signal_id is None
    assert notice.text == (
        "[HATIRLATMA] TradingView alarmlarının süresi doluyor:\n"
        "• heartbeat — 2026-11-14 (2 gün önce BİTTİ)\n"
        "• NY ORB FVG — 2026-11-23 (7 gün kaldı)\n"
        "Süresi biten alarm sinyal GÖNDERMEZ: TradingView'da alarmı yeniden kur ve "
        "config/tv_alerts.yaml'daki expires_on tarihini güncelle."
    )
    assert state(store, StateKey.EXPIRY_REMINDER_DAY) == "2026-11-16"


def test_the_reminder_returns_on_the_next_istanbul_morning(store: Store) -> None:
    tick(store, NOON_TR_MS, EXPIRY_CONFIG)

    # Midnight in Istanbul is 21:00 UTC. A new day has begun, but nobody is woken for a
    # reminder: it waits until 09:00 Istanbul time (06:00 UTC).
    assert tick(store, utc("2026-11-16 21:00"), EXPIRY_CONFIG) == 0
    assert tick(store, utc("2026-11-17 05:59:59.999"), EXPIRY_CONFIG) == 0
    assert state(store, StateKey.EXPIRY_REMINDER_DAY) == "2026-11-16"
    assert tick(store, utc("2026-11-17 06:00"), EXPIRY_CONFIG) == 1

    assert outbox(store)[1].text.splitlines()[1:4] == [
        "• heartbeat — 2026-11-14 (3 gün önce BİTTİ)",
        "• NY ORB FVG — 2026-11-23 (6 gün kaldı)",
        "• Asian Sweep — 2026-11-24 (7 gün kaldı)",
    ]
    assert state(store, StateKey.EXPIRY_REMINDER_DAY) == "2026-11-17"


def test_the_reminder_window_comes_from_the_config(store: Store) -> None:
    config = tv_config(SUNDAY_ONLY, alerts=EXPIRING, expiry_reminder_days=30)

    assert tick(store, NOON_TR_MS, config) == 1
    assert outbox(store)[0].text.count("• ") == 3


@pytest.mark.parametrize(
    "alerts",
    [[], [("MTF Trend", None)], [("NY ORB FVG", "2026-11-24"), ("Asian Sweep", "2027-01-10")]],
)
def test_nothing_due_means_no_reminder_and_no_mark(store: Store, alerts: list[Alert]) -> None:
    config = tv_config(SUNDAY_ONLY, alerts=alerts)

    assert tick(store, NOON_TR_MS, config) == 0
    assert outbox(store) == []
    # Not marked: an alert that becomes due later the same day is still announced.
    assert state(store, StateKey.EXPIRY_REMINDER_DAY) is None


def test_a_long_reminder_is_cut_below_the_telegram_limit(store: Store) -> None:
    many = [(f"{index:03d} " + "n" * 60, "2026-11-17") for index in range(150)]
    config = tv_config(SUNDAY_ONLY, alerts=many)

    assert tick(store, NOON_TR_MS, config) == 1

    (notice,) = outbox(store)
    lines = notice.text.splitlines()
    shown = [line for line in lines if line.startswith("• ")]
    assert len(notice.text) < 3500
    assert shown[0] == "• 000 " + "n" * 60 + " — 2026-11-17 (1 gün kaldı)"
    assert lines[-2] == f"… ve {150 - len(shown)} alarm daha"
    assert 0 < len(shown) < 150


# --- one tick -----------------------------------------------------------------------------


def test_one_tick_can_report_downtime_silence_and_expiry_together(store: Store) -> None:
    config = tv_config(alerts=[("NY ORB FVG", "2026-11-17")])
    alert_received(store, utc("2026-11-16 08:30"))
    set_state(store, StateKey.LAST_ALIVE_MS, str(utc("2026-11-16 09:00")))
    now_ms = utc("2026-11-16 12:00")

    assert tick(store, now_ms, config, started_ms=now_ms, first_tick=True) == 3

    downtime, silence, reminder = outbox(store)
    assert (downtime.kind, silence.kind, reminder.kind) == (
        OutboxKind.ALARM,
        OutboxKind.ALARM,
        OutboxKind.NOTICE,
    )
    assert downtime.text.startswith("[ALARM] Bot 12:00–15:00 TR arasında kapalıydı;")
    assert silence.text.startswith("[ALARM] TradingView alarm hattı sessiz: son 3 sa 30 dk ")
    assert reminder.text.startswith("[HATIRLATMA] ")
    assert all(row.status.value == "pending" for row in (downtime, silence, reminder))


def test_a_quiet_tick_only_records_that_the_bot_is_alive(store: Store) -> None:
    now_ms = utc("2026-11-16 12:00")
    alert_received(store, now_ms - MINUTE_MS)

    assert tick(store, now_ms) == 0
    assert outbox(store) == []
    assert state(store, StateKey.LAST_ALIVE_MS) == str(now_ms)
    assert state(store, StateKey.HEARTBEAT_SILENT) is None
    assert state(store, StateKey.HEARTBEAT_ALARM_MS) is None


# --- the task -----------------------------------------------------------------------------


class Monitor:
    """`run_heartbeat_monitor` as a task, with its clock and `on_messages` calls recorded."""

    def __init__(
        self,
        store: Store,
        *,
        now_ms: int,
        config: TvAlertsConfig = CONFIG,
        interval_seconds: float = 0.005,
        step_ms: int = 0,
    ) -> None:
        self.store = store
        self.now_ms = now_ms
        self.step_ms = step_ms
        self.ticks = 0
        self.message_calls = 0
        self.stop = asyncio.Event()
        self.task = asyncio.create_task(
            run_heartbeat_monitor(
                store=store,
                config=config,
                clock=self._clock,
                started_ms=now_ms,
                on_messages=self._on_messages,
                stop=self.stop,
                interval_seconds=interval_seconds,
            )
        )

    def _clock(self) -> int:
        value = self.now_ms
        self.now_ms += self.step_ms
        self.ticks += 1
        return value

    def _on_messages(self) -> None:
        self.message_calls += 1

    async def until(self, condition: Callable[[], bool], timeout: float = 5.0) -> None:
        deadline = asyncio.get_running_loop().time() + timeout
        while not condition():
            if self.task.done():
                self.task.result()
                raise AssertionError("the monitor stopped before the condition was met")
            assert asyncio.get_running_loop().time() < deadline, "condition not reached in time"
            await asyncio.sleep(0.002)

    async def finish(self, timeout: float = 2.0) -> None:
        self.stop.set()
        await asyncio.wait_for(self.task, timeout=timeout)


def run(scenario: Callable[[], Coroutine[Any, Any, None]]) -> None:
    asyncio.run(scenario())


def test_the_monitor_ticks_at_once_and_checks_downtime_on_its_first_tick_only(
    store: Store,
) -> None:
    now_ms = utc("2026-11-16 14:30")
    set_state(store, StateKey.LAST_ALIVE_MS, str(now_ms - HOUR_MS))
    alert_received(store, now_ms - MINUTE_MS)

    async def scenario() -> None:
        # Every tick is two minutes after the previous one: more than the downtime
        # threshold, so only the first-tick rule keeps the later ticks quiet.
        monitor = Monitor(store, now_ms=now_ms, step_ms=2 * MINUTE_MS)
        await monitor.until(lambda: monitor.ticks >= 5)
        await monitor.finish()
        assert monitor.message_calls == 1

    run(scenario)
    (alarm,) = outbox(store)
    assert alarm.text == messages.downtime_alarm(now_ms - HOUR_MS, now_ms)
    assert alarm.created_at_ms == now_ms


def test_the_monitor_calls_back_only_for_ticks_that_enqueued_something(store: Store) -> None:
    alert_received(store, WINDOW_OPEN_MS)
    start_ms = WINDOW_OPEN_MS + MAX_SILENCE_MS - 2 * MINUTE_MS

    async def scenario() -> None:
        # Minute by minute: the silence limit is crossed on the fourth tick.
        monitor = Monitor(store, now_ms=start_ms, step_ms=MINUTE_MS)
        await monitor.until(lambda: monitor.ticks >= 8)
        await monitor.finish()
        assert monitor.message_calls == 1

    run(scenario)
    (alarm,) = outbox(store)
    assert alarm.kind is OutboxKind.ALARM
    assert alarm.created_at_ms == start_ms + 3 * MINUTE_MS


def test_the_monitor_stops_promptly_while_waiting_for_its_next_tick(store: Store) -> None:
    now_ms = utc("2026-11-15 12:00")  # Sunday: nothing to report

    async def scenario() -> None:
        monitor = Monitor(store, now_ms=now_ms, interval_seconds=60.0)
        await monitor.until(lambda: state(store, StateKey.LAST_ALIVE_MS) == str(now_ms))
        started = asyncio.get_running_loop().time()
        await monitor.finish(timeout=1.0)
        assert asyncio.get_running_loop().time() - started < 1.0
        assert monitor.ticks == 1
        assert monitor.message_calls == 0

    run(scenario)
    assert outbox(store) == []


def test_the_monitor_does_not_tick_once_stop_is_set(store: Store) -> None:
    async def scenario() -> None:
        monitor = Monitor(store, now_ms=utc("2026-11-16 14:30"))
        monitor.stop.set()
        await asyncio.wait_for(monitor.task, timeout=2.0)
        assert monitor.ticks == 0

    run(scenario)
    assert state(store, StateKey.LAST_ALIVE_MS) is None


def test_a_store_failure_ends_the_monitor() -> None:
    closed = Store(":memory:")
    closed.close()

    async def scenario() -> None:
        monitor = Monitor(closed, now_ms=utc("2026-11-16 14:30"))
        with pytest.raises(StoreError, match="veritabanı kapalı"):
            await asyncio.wait_for(monitor.task, timeout=2.0)
        assert monitor.message_calls == 0

    run(scenario)
