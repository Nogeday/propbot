"""Watch windows of the alert line as real instants (config `heartbeat.watch_windows`).

A window is written in New York wall time for some weekdays ("mon-fri 03:00-16:00").
Here it becomes concrete `[start_ms, end_ms)` intervals in UTC epoch milliseconds, so the
silence monitor and `/saglik` agree on when the line is being watched, also across the
weeks in which daylight saving changes (CLAUDE.md invariant 9).
"""

import datetime as dt
from typing import Final

from propbot.core.clock import ms_from_datetime, utc_from_ms
from propbot.notify.format import NEW_YORK
from propbot.signals.tvconfig import HeartbeatConfig

_ONE_DAY: Final = dt.timedelta(days=1)

type Interval = tuple[int, int]
"""`(start_ms, end_ms)`: the window is open for `start_ms <= t < end_ms`."""


def window_instances(heartbeat: HeartbeatConfig, day: dt.date) -> list[Interval]:
    """Watch windows that open on New York calendar day `day`, ordered by start.

    Windows never cross midnight (the config demands start < end), so each one lies
    inside `day`. A wall time that does not exist on that date (spring-forward gap) is
    resolved by zoneinfo with the offset in force before the change.
    """
    instances = [
        (
            ms_from_datetime(dt.datetime.combine(day, window.start_time, tzinfo=NEW_YORK)),
            ms_from_datetime(dt.datetime.combine(day, window.end_time, tzinfo=NEW_YORK)),
        )
        for window in heartbeat.watch_windows
        if day.weekday() in window.weekdays
    ]
    return sorted(instances)


def active_or_recent_window(
    heartbeat: HeartbeatConfig, now_ms: int, *, grace_ms: int = 0
) -> Interval | None:
    """The window that contains `now_ms` or ended at most `grace_ms` ago.

    With several candidates the one that started last wins. The grace lets the silence
    rule look back at a window that has just closed: a line that died in the window's
    last hour is still reported.
    """
    if grace_ms < 0:
        raise ValueError("grace_ms must not be negative")
    today = utc_from_ms(now_ms).astimezone(NEW_YORK).date()
    candidates = [
        (start_ms, end_ms)
        # Yesterday's windows matter only through the grace period after a late end.
        for day in (today - _ONE_DAY, today)
        for start_ms, end_ms in window_instances(heartbeat, day)
        if start_ms <= now_ms < end_ms + grace_ms
    ]
    return max(candidates, default=None)


def watch_window_open(heartbeat: HeartbeatConfig, now_ms: int) -> bool:
    """True while `now_ms` lies inside a watch window."""
    return active_or_recent_window(heartbeat, now_ms) is not None
