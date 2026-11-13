"""Bar length of a TradingView timeframe string (Pine `timeframe.period`).

Only intraday bars of a fixed length are supported: the receiver computes the bar close
as `bar_time + timeframe_seconds(tf)` to judge how fresh an alert is. Daily and longer
bars close on the exchange's session calendar, so their close cannot be derived this way.
"""

import re
from typing import Final

SECONDS_PER_MINUTE: Final = 60
# 1440 minutes = 24 hours: the longest bar whose length does not depend on the session calendar.
MAX_MINUTES: Final = 1440
MAX_SECONDS: Final = 59

# ASCII digits only ([0-9], not \d) and `fullmatch`: no sign, no leading zero, no whitespace,
# no trailing newline, no Unicode digits.
_MINUTES: Final = re.compile(r"[1-9][0-9]{0,3}")
_SECONDS: Final = re.compile(r"([1-9][0-9]?)S")

# Value-free on purpose: the text may end up in a log line or a Telegram message.
_MESSAGE: Final = (
    "desteklenmeyen zaman dilimi: yalnızca dakikalık (1-1440, ör. 1, 5, 15, 60, 240) ve "
    "saniyelik (1S-59S) barlar kabul edilir; günlük ve daha uzun barlar seans takvimine "
    "göre kapandığı için sinyal tazeliği denetlenemez"
)


class TimeframeError(ValueError):
    """The timeframe string is not a supported fixed-length intraday bar."""


def timeframe_seconds(tf: str) -> int:
    """Return the bar length in seconds for a Pine `timeframe.period` value.

    Accepted: minutes as plain digits, "1" to "1440"; seconds as "1S" to "59S".
    Everything else (D, 1D, W, M, ticks, ranges, "0", "01", " 5", "5m", "") raises
    `TimeframeError` with a Turkish, value-free message.
    """
    if _MINUTES.fullmatch(tf) is not None:
        minutes = int(tf)
        if minutes <= MAX_MINUTES:
            return minutes * SECONDS_PER_MINUTE
    else:
        match = _SECONDS.fullmatch(tf)
        if match is not None:
            seconds = int(match.group(1))
            if seconds <= MAX_SECONDS:
                return seconds
    raise TimeframeError(_MESSAGE)
