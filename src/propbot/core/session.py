"""Wall-clock parts of a bar's open time in the session time zone (invariant 9).

Pine reads `hour(time, tz)`, `minute(time, tz)`, `dayofweek(time, tz)` and
`dayofmonth(time, tz)` on every bar; `LocalClock.parts` is the same thing for the Python
strategies. It is a pure function of the bar's timestamp: no strategy reads the system
clock. Session logic uses America/New_York; zoneinfo carries the DST rules.
"""

import datetime as dt
from dataclasses import dataclass
from typing import Final
from zoneinfo import ZoneInfo

NEW_YORK: Final = "America/New_York"
_MS_PER_SECOND: Final = 1000


@dataclass(frozen=True, slots=True)
class LocalParts:
    year: int
    month: int
    day: int  # Pine `dayofmonth`
    hour: int
    minute: int
    weekday: int  # Monday = 0 ... Sunday = 6 (Pine `dayofweek` is Sunday = 1 ... Saturday = 7)

    @property
    def minute_of_day(self) -> int:
        """Pine `curMin = hour * 60 + minute`."""
        return self.hour * 60 + self.minute

    @property
    def date(self) -> dt.date:
        return dt.date(self.year, self.month, self.day)


class LocalClock:
    """Converts UTC epoch milliseconds to wall-clock parts in one time zone."""

    def __init__(self, tz: str = NEW_YORK) -> None:
        self.tz: Final = tz
        self._zone: Final = ZoneInfo(tz)
        self._last_ms: int | None = None
        self._last_parts: LocalParts | None = None

    def parts(self, ts_ms: int) -> LocalParts:
        # A strategy asks several times for the same bar: one conversion per timestamp.
        if ts_ms == self._last_ms and self._last_parts is not None:
            return self._last_parts
        seconds, millis = divmod(ts_ms, _MS_PER_SECOND)
        moment = dt.datetime.fromtimestamp(seconds, tz=self._zone).replace(
            microsecond=millis * 1000
        )
        parts = LocalParts(
            year=moment.year,
            month=moment.month,
            day=moment.day,
            hour=moment.hour,
            minute=moment.minute,
            weekday=moment.weekday(),
        )
        self._last_ms = ts_ms
        self._last_parts = parts
        return parts
