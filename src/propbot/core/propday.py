"""Prop trading day: the firm's day rolls over at a reset time in the firm's time zone.

Storage is UTC; session logic is America/New_York; the prop day follows the venue
profile's `daily_reset` (CLAUDE.md invariant 9). Day boundaries are real instants, so
the mapping stays monotonic across DST changes.
"""

import datetime as dt
from zoneinfo import ZoneInfo


def prop_day_start(day: dt.date, zone: ZoneInfo, reset: dt.time) -> dt.datetime:
    """Return the UTC instant at which prop day `day` begins.

    Prop day D runs from D at `reset` (wall time in `zone`) up to, but not including,
    D+1 at `reset`. If the wall time does not exist on that date (spring-forward gap),
    zoneinfo resolves it with the offset in force before the change.
    """
    return dt.datetime.combine(day, reset, tzinfo=zone).astimezone(dt.UTC)


def prop_day(moment: dt.datetime, zone: ZoneInfo, reset: dt.time) -> dt.date:
    """Return the prop day that contains `moment` (which must be timezone-aware)."""
    if moment.utcoffset() is None:
        raise ValueError("moment must be timezone-aware")
    local_date = moment.astimezone(zone).date()
    # Compare instants in UTC: datetimes sharing a tzinfo compare by wall clock otherwise.
    if moment.astimezone(dt.UTC) < prop_day_start(local_date, zone, reset):
        return local_date - dt.timedelta(days=1)
    return local_date


def weekdays_between(first: dt.date, last: dt.date) -> list[dt.date]:
    """Return every Monday-to-Friday date from `first` to `last`, both inclusive."""
    days: list[dt.date] = []
    day = first
    while day <= last:
        if day.weekday() < 5:
            days.append(day)
        day += dt.timedelta(days=1)
    return days
