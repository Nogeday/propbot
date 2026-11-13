"""Number and time formatting for Telegram messages (user-facing, Turkish).

Prices, points and lots use a decimal POINT: the user types them into the trading
terminal as shown. Prose reports elsewhere (`backtest.report`) use Turkish separators.
"""

import datetime as dt
from typing import Final
from zoneinfo import ZoneInfo

from propbot.core.clock import utc_from_ms

NEW_YORK: Final = ZoneInfo("America/New_York")  # session logic (CLAUDE.md invariant 9)
ISTANBUL: Final = ZoneInfo("Europe/Istanbul")  # the user's wall clock (SPEC §8.2)

_MIN_DECIMALS: Final = 2
_MAX_DECIMALS: Final = 5


def fmt_price(value: float) -> str:
    """Price or point distance with 2 to 5 decimals: 5123.25 -> 5123.25, 12.5 -> 12.50."""
    text = f"{value:.{_MAX_DECIMALS}f}".rstrip("0")
    whole, _, fraction = text.partition(".")
    return f"{whole}.{fraction.ljust(_MIN_DECIMALS, '0')}"


def fmt_signed_points(value: float) -> str:
    """Signed point distance; negatives carry a typographic minus (U+2212), positives a plus."""
    sign = "−" if value < 0 else "+"
    return f"{sign}{fmt_price(abs(value))}"


def fmt_lots(value: float) -> str:
    """Lot size as the terminal shows it: 20 -> 20.00, 0.375 -> 0.375."""
    return fmt_price(value)


def fmt_money(amount: float, currency: str) -> str:
    """Account-currency amount: 250.0 -> $250, 249.5 -> $249.50; other currencies -> 250 EUR."""
    rounded = round(amount, 2)
    number = f"{rounded:.0f}" if rounded == int(rounded) else f"{rounded:.2f}"
    return f"${number}" if currency == "USD" else f"{number} {currency}"


def fmt_percent(percent: float) -> str:
    """A value that is already a percentage, without padding zeros: 0.25 -> %0.25."""
    return "%" + f"{percent:.4f}".rstrip("0").rstrip(".")


def fmt_clock(epoch_ms: int, zone: ZoneInfo, *, seconds: bool = False) -> str:
    """Wall-clock time of an instant in `zone`: 17:45 or 17:45:00."""
    local = utc_from_ms(epoch_ms).astimezone(zone)
    return local.strftime("%H:%M:%S" if seconds else "%H:%M")


def fmt_date(epoch_ms: int, zone: ZoneInfo) -> str:
    return utc_from_ms(epoch_ms).astimezone(zone).date().isoformat()


def fmt_duration(seconds: float) -> str:
    """Rough elapsed time for humans: 42 sn, 3 dk 20 sn, 2 sa 5 dk, 3 gün 4 sa."""
    total = max(0, round(seconds))
    if total < 60:
        return f"{total} sn"
    minutes, secs = divmod(total, 60)
    if minutes < 60:
        return f"{minutes} dk {secs} sn" if secs else f"{minutes} dk"
    hours, minutes = divmod(minutes, 60)
    if hours < 24:
        return f"{hours} sa {minutes} dk" if minutes else f"{hours} sa"
    days, hours = divmod(hours, 24)
    return f"{days} gün {hours} sa" if hours else f"{days} gün"


def local_date(epoch_ms: int, zone: ZoneInfo) -> dt.date:
    return utc_from_ms(epoch_ms).astimezone(zone).date()
