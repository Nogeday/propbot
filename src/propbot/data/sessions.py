"""Session boundaries derived from the bars themselves (ADR-0008 item 6).

Pine's `session.islastbar` has no calendar behind it here: a bar is the last one of its
session when the file's next bar opens long after this bar has closed. Holidays and half
days are therefore whatever the data says they are. A live feed cannot see the next bar;
that decision belongs to Faz 3.
"""

import itertools
from collections.abc import Sequence
from typing import Final

from propbot.core.bars import Bar

# A break shorter than this between two bars is a hole in the data, not a session end. The
# daily maintenance break of the 23-hour index products is 60 minutes.
DEFAULT_MIN_GAP_SECONDS: Final = 1800

_MS_PER_SECOND: Final = 1000


def session_last_flags(
    bars: Sequence[Bar], tf_seconds: int, *, min_gap_seconds: int = DEFAULT_MIN_GAP_SECONDS
) -> tuple[bool, ...]:
    """Pine `session.islastbar` for every bar, in order.

    True when the next bar opens at least `min_gap_seconds` after this bar's CLOSE
    (its open time plus the bar length). The final bar is always True: nothing follows it.
    """
    _require_lengths(tf_seconds, min_gap_seconds)
    if not bars:
        return ()
    # Measured open to open: the next bar must open this long after this bar opened.
    threshold_ms = (tf_seconds + min_gap_seconds) * _MS_PER_SECOND
    flags = [
        following.ts_open_ms - bar.ts_open_ms >= threshold_ms
        for bar, following in itertools.pairwise(bars)
    ]
    flags.append(True)
    return tuple(flags)


def split_sessions(
    bars: Sequence[Bar], tf_seconds: int, *, min_gap_seconds: int = DEFAULT_MIN_GAP_SECONDS
) -> tuple[range, ...]:
    """Index ranges of the sessions: each ends at a bar `session_last_flags` marks."""
    sessions: list[range] = []
    start = 0
    flags = session_last_flags(bars, tf_seconds, min_gap_seconds=min_gap_seconds)
    for index, is_last in enumerate(flags):
        if is_last:
            sessions.append(range(start, index + 1))
            start = index + 1
    return tuple(sessions)


def _require_lengths(tf_seconds: int, min_gap_seconds: int) -> None:
    if tf_seconds <= 0:
        raise ValueError("tf_seconds must be positive")
    if min_gap_seconds <= 0:
        raise ValueError("min_gap_seconds must be positive")
