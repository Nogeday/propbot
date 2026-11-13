"""Generator of the SYNTHETIC trade list used for demos and CLI tests.

The numbers are made up: they describe no real strategy and prove nothing about one.
`tests/fixtures/trades/synthetic_trades.csv` is this generator's output for the default
seed; a test keeps the two in sync.
"""

import datetime as dt
import random
from zoneinfo import ZoneInfo

from propbot.core.propday import weekdays_between

NEW_YORK = ZoneInfo("America/New_York")
HEADER = "entry_time,exit_time,r,strategy_id,mae_r"

# strategy id -> (chance of a trade per weekday, earliest entry, latest entry, forced exit)
_STRATEGIES = {
    "asian_sweep": (0.45, dt.time(3, 30), dt.time(10, 0), dt.time(11, 0)),
    "ny_orb_fvg": (0.60, dt.time(9, 40), dt.time(14, 30), dt.time(15, 59)),
}
_WIN_PROBABILITY = 0.33  # +2R target
_LOSS_PROBABILITY = 0.52  # -1R stop; the rest are early exits between -0.6R and +1.2R


def synthetic_trades_csv(seed: int = 20261113) -> str:
    """Return the CSV text: one year of weekdays, two strategies, at most one trade each per day."""
    rng = random.Random(seed)
    rows = [HEADER]
    for day in weekdays_between(dt.date(2025, 1, 2), dt.date(2025, 12, 31)):
        for strategy_id, (chance, earliest, latest, forced_exit) in _STRATEGIES.items():
            if rng.random() >= chance:
                continue
            first_minute = earliest.hour * 60 + earliest.minute
            last_minute = latest.hour * 60 + latest.minute
            entry_minute = rng.randrange(first_minute, last_minute + 1)
            exit_minute = min(
                entry_minute + rng.randrange(5, 121), forced_exit.hour * 60 + forced_exit.minute
            )
            outcome = rng.random()
            if outcome < _WIN_PROBABILITY:
                r, mae_r = 2.0, round(rng.uniform(-0.9, -0.05), 2)
            elif outcome < _WIN_PROBABILITY + _LOSS_PROBABILITY:
                r, mae_r = -1.0, -1.0
            else:
                r = round(rng.uniform(-0.6, 1.2), 2)
                mae_r = round(min(r, rng.uniform(-0.9, 0.0)), 2)
            # Only one strategy reports its excursion, so both code paths are exercised.
            mae_text = f"{mae_r:.2f}" if strategy_id == "ny_orb_fvg" else ""
            rows.append(
                f"{_utc_text(day, entry_minute)},{_utc_text(day, exit_minute)},"
                f"{r:.2f},{strategy_id},{mae_text}"
            )
    return "\n".join(rows) + "\n"


def _utc_text(day: dt.date, minute_of_day: int) -> str:
    local = dt.datetime.combine(day, dt.time(minute_of_day // 60, minute_of_day % 60), NEW_YORK)
    return local.astimezone(dt.UTC).strftime("%Y-%m-%dT%H:%M:%SZ")
