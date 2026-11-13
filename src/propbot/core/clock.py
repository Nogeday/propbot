"""Time source and conversions. Storage is UTC epoch milliseconds (CLAUDE.md invariant 9).

Components never read the wall clock themselves: they receive a `Clock`, so tests can
drive time and a clock problem has one place to show up.
"""

import datetime as dt
import time
from collections.abc import Callable

Clock = Callable[[], int]
"""Returns the current time as UTC epoch milliseconds."""


def system_clock_ms() -> int:
    return time.time_ns() // 1_000_000


def utc_from_ms(epoch_ms: int) -> dt.datetime:
    """Timezone-aware UTC datetime for an epoch-millisecond instant."""
    return dt.datetime.fromtimestamp(epoch_ms / 1000.0, tz=dt.UTC)


def ms_from_datetime(moment: dt.datetime) -> int:
    """Epoch milliseconds of a timezone-aware datetime (truncated to the millisecond)."""
    if moment.utcoffset() is None:
        raise ValueError("moment must be timezone-aware")
    delta = moment - dt.datetime(1970, 1, 1, tzinfo=dt.UTC)
    return delta // dt.timedelta(milliseconds=1)
