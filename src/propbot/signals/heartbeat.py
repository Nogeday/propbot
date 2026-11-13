"""Alert-line monitor (SPEC §5.5): silence, downtime and alert-expiry reminders.

TradingView does not tell anybody when an alert stops firing, and neither a stop-loss nor a
take-profit fill is alerted. The bot therefore watches the line itself and tells the user
when it may be blind:

    silence    no valid alert for `max_silence_minutes` inside a watch window (the heartbeat
               indicator fires once an hour, so silence means the line is down)
    downtime   the bot itself was not running during a watch window
    expiry     a TradingView alert is about to expire, or already has

The monitor only reads store state and enqueues outbox rows; it never sends anything.
`check_alert_line` is one tick and runs inside the caller's transaction.
"""

import asyncio
import datetime as dt
from collections.abc import Callable
from contextlib import suppress
from typing import Final

import structlog

from propbot.core.clock import Clock, utc_from_ms
from propbot.notify import messages
from propbot.notify.format import ISTANBUL, NEW_YORK, local_date
from propbot.signals.tvconfig import HeartbeatConfig, TvAlertsConfig
from propbot.signals.watch import active_or_recent_window, window_instances
from propbot.store.db import OutboxKind, StateKey, Store, Tx

_MILLIS_PER_SECOND: Final = 1000
_MINUTE_MS: Final = 60 * _MILLIS_PER_SECOND
_ONE_DAY: Final = dt.timedelta(days=1)

# A gap between two ticks longer than this, seen on the first tick, means the bot was down
# (ticks are 30 s apart; a restart by systemd takes a few seconds).
DOWNTIME_THRESHOLD_MS: Final = 90 * _MILLIS_PER_SECOND
# Any gap this long contains a whole instance of every weekly watch window.
_ALWAYS_OVERLAPS_DAYS: Final = 8

_FLAG_SET: Final = "1"

_log = structlog.get_logger(__name__)


def _read_ms(tx: Tx, key: StateKey) -> int | None:
    """An epoch-ms state value; None when absent or unreadable."""
    value = tx.get_state(key)
    if value is None:
        return None
    try:
        return int(value)
    except ValueError:
        return None


def _overlaps_watch_window(heartbeat: HeartbeatConfig, start_ms: int, end_ms: int) -> bool:
    """True when `[start_ms, end_ms)` shares an instant with a watch window."""
    first_day = utc_from_ms(start_ms).astimezone(NEW_YORK).date()
    last_day = utc_from_ms(end_ms).astimezone(NEW_YORK).date()
    if (last_day - first_day).days > _ALWAYS_OVERLAPS_DAYS:
        return True
    day = first_day
    while day <= last_day:
        for window_start_ms, window_end_ms in window_instances(heartbeat, day):
            if window_start_ms < end_ms and start_ms < window_end_ms:
                return True
        day += _ONE_DAY
    return False


def _check_downtime(tx: Tx, heartbeat: HeartbeatConfig, now_ms: int) -> int:
    """First tick after a start: report the gap since the previous process's last tick."""
    previous_ms = _read_ms(tx, StateKey.LAST_ALIVE_MS)
    if previous_ms is None or now_ms - previous_ms <= DOWNTIME_THRESHOLD_MS:
        return 0
    if not _overlaps_watch_window(heartbeat, previous_ms, now_ms):
        return 0
    tx.enqueue_message(
        kind=OutboxKind.ALARM, text=messages.downtime_alarm(previous_ms, now_ms), now_ms=now_ms
    )
    return 1


def _check_silence(tx: Tx, heartbeat: HeartbeatConfig, now_ms: int, started_ms: int) -> int:
    max_silence_ms = heartbeat.max_silence_minutes * _MINUTE_MS
    last_seen_ms = tx.last_received_ms()  # any valid alert, heartbeat or strategy
    alarm_ms = _read_ms(tx, StateKey.HEARTBEAT_ALARM_MS)
    enqueued = 0

    # An alert that arrived after the last alarm: the line is back, say so once.
    if (
        tx.get_state(StateKey.HEARTBEAT_SILENT) == _FLAG_SET
        and last_seen_ms is not None
        and (alarm_ms is None or last_seen_ms >= alarm_ms)
    ):
        tx.enqueue_message(
            kind=OutboxKind.NOTICE, text=messages.heartbeat_recovered_notice(), now_ms=now_ms
        )
        tx.delete_state(StateKey.HEARTBEAT_SILENT)
        enqueued += 1

    # The grace keeps a window that has just closed in view: a line that died in its last
    # hour is still reported.
    window = active_or_recent_window(heartbeat, now_ms, grace_ms=max_silence_ms)
    if window is None:
        return enqueued
    window_start_ms, window_end_ms = window
    if last_seen_ms is None:
        # A database that never saw an alert: count from when this process started watching.
        baseline_ms = max(window_start_ms, started_ms)
    else:
        # A restart does not reset the baseline: the line has been silent since the last alert.
        baseline_ms = max(last_seen_ms, window_start_ms)
    silent = baseline_ms < window_end_ms and now_ms - baseline_ms > max_silence_ms
    if not silent:
        return enqueued
    # At most one alarm per `max_silence_ms`. A clock that stepped back never silences it.
    if alarm_ms is not None and 0 <= now_ms - alarm_ms < max_silence_ms:
        return enqueued
    text = messages.heartbeat_silence_alarm(
        (now_ms - baseline_ms) / _MILLIS_PER_SECOND, heartbeat.max_silence_minutes
    )
    tx.enqueue_message(kind=OutboxKind.ALARM, text=text, now_ms=now_ms)
    tx.set_state(StateKey.HEARTBEAT_ALARM_MS, str(now_ms), now_ms)
    tx.set_state(StateKey.HEARTBEAT_SILENT, _FLAG_SET, now_ms)
    return enqueued + 1


def _check_expiry(tx: Tx, config: TvAlertsConfig, now_ms: int) -> int:
    """Once per Istanbul calendar day: list the alerts that expire soon or already have."""
    today = local_date(now_ms, ISTANBUL)
    if tx.get_state(StateKey.EXPIRY_REMINDER_DAY) == today.isoformat():
        return 0
    due = sorted(
        (alert.expires_on, alert.name, (alert.expires_on - today).days)
        for alert in config.alerts
        if alert.expires_on is not None
        and (alert.expires_on - today).days <= config.expiry_reminder_days
    )
    if not due:
        return 0
    items = [(name, expires_on, days_left) for expires_on, name, days_left in due]
    tx.enqueue_message(
        kind=OutboxKind.NOTICE, text=messages.alert_expiry_reminder(items), now_ms=now_ms
    )
    tx.set_state(StateKey.EXPIRY_REMINDER_DAY, today.isoformat(), now_ms)
    return 1


def check_alert_line(
    tx: Tx, config: TvAlertsConfig, *, now_ms: int, started_ms: int, first_tick: bool
) -> int:
    """One monitor tick inside `tx`; return the number of outbox rows it enqueued.

    `started_ms` is when this process started; `first_tick` is True for its first tick only
    (the downtime check compares against the previous process's last tick).
    """
    enqueued = 0
    if first_tick:
        enqueued += _check_downtime(tx, config.heartbeat, now_ms)
    tx.set_state(StateKey.LAST_ALIVE_MS, str(now_ms), now_ms)
    enqueued += _check_silence(tx, config.heartbeat, now_ms, started_ms)
    enqueued += _check_expiry(tx, config, now_ms)
    return enqueued


def _tick(
    store: Store, config: TvAlertsConfig, now_ms: int, started_ms: int, first_tick: bool
) -> int:
    with store.transaction() as tx:
        return check_alert_line(
            tx, config, now_ms=now_ms, started_ms=started_ms, first_tick=first_tick
        )


async def run_heartbeat_monitor(
    *,
    store: Store,
    config: TvAlertsConfig,
    clock: Clock,
    started_ms: int,
    on_messages: Callable[[], None],
    stop: asyncio.Event,
    interval_seconds: float = 30.0,
) -> None:
    """Tick every `interval_seconds` until `stop` is set; the first tick runs at once.

    `on_messages` is called after a tick that enqueued outbox rows. A store failure ends the
    task (the service then exits and is restarted): a monitor that cannot read its state
    must not look alive.
    """
    first_tick = True
    while not stop.is_set():
        enqueued = await asyncio.to_thread(_tick, store, config, clock(), started_ms, first_tick)
        first_tick = False
        if enqueued:
            _log.info("alert_line_messages_enqueued", count=enqueued)
            on_messages()
        with suppress(TimeoutError):
            await asyncio.wait_for(stop.wait(), timeout=interval_seconds)
