"""Operating modes and their locks (CLAUDE.md invariant 1).

`signal` (default, no orders) -> `paper` -> `approve` -> `auto`. This build implements
`signal` only; every other mode is refused, whatever the config says. The order modes
arrive with the execution bridge (Faz 3), together with the remaining `auto`
conditions (config flag, CLI flag, Telegram PIN, Go/No-Go report).
"""

import datetime as dt
from enum import StrEnum

from propbot.risk.venue import RULES_MAX_AGE_DAYS, VenueProfile, rules_freshness


class Mode(StrEnum):
    SIGNAL = "signal"
    PAPER = "paper"
    APPROVE = "approve"
    AUTO = "auto"


def mode_refusal(mode: Mode, venue: VenueProfile, today: dt.date) -> str | None:
    """Return None when `mode` may run, otherwise the (Turkish) reason it is locked."""
    if mode is Mode.SIGNAL:
        return None
    reasons = [f"'{mode.value}' modu bu sürümde yok: yalnızca 'signal' (emir gönderilmez) çalışır"]
    if mode is not Mode.PAPER and rules_freshness(venue, today).order_modes_locked:
        reasons.append(
            f"venue kuralları taze değil (rules_verified_at boş, ileri tarihli ya da "
            f"{RULES_MAX_AGE_DAYS} günden eski)"
        )
    if mode is Mode.AUTO:
        reasons.append(
            "auto için ayrıca gerekir: config live.enabled, --i-accept-live-risk, Telegram PIN "
            "onayı ve PASS veren Go/No-Go raporu"
        )
    return "; ".join(reasons)
