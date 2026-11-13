"""Rules that tie `config/risk.yaml` to a venue profile (SPEC §8.1)."""

from propbot.risk.config import DAILY_CAP_MAX_FRACTION_OF_DAILY_LOSS_LIMIT, RiskConfig
from propbot.risk.errors import ConfigIssue
from propbot.risk.venue import VenueProfile


def daily_cap_ceiling_pct(venue: VenueProfile) -> float:
    """Highest daily cap the venue allows: half of the firm's daily loss limit."""
    return DAILY_CAP_MAX_FRACTION_OF_DAILY_LOSS_LIMIT * venue.daily_loss_limit_pct


def daily_budget_pct(risk: RiskConfig, venue: VenueProfile) -> float:
    """Daily loss budget in % of initial balance.

    `cross_check` already rejects a `daily_cap_pct` above the ceiling; the `min` is a
    second layer so the budget stays capped even if a caller skips the check.
    """
    return min(risk.daily_cap_pct, daily_cap_ceiling_pct(venue))


def daily_budget_amount(risk: RiskConfig, venue: VenueProfile) -> float:
    """Daily loss budget in account currency."""
    return daily_budget_pct(risk, venue) / 100.0 * venue.initial_balance


def cross_check(
    risk: RiskConfig, venue: VenueProfile, *, risk_source: str, venue_source: str
) -> list[ConfigIssue]:
    """Return the cross-file rule violations (empty list when the pair is consistent)."""
    issues: list[ConfigIssue] = []
    ceiling = daily_cap_ceiling_pct(venue)
    if risk.daily_cap_pct > ceiling:
        issues.append(
            ConfigIssue(
                source=risk_source,
                location="daily_cap_pct",
                message=(
                    f"{risk.daily_cap_pct} değeri {venue_source} için tavanı aşıyor: en fazla "
                    f"{DAILY_CAP_MAX_FRACTION_OF_DAILY_LOSS_LIMIT} × daily_loss_limit_pct "
                    f"({venue.daily_loss_limit_pct}) = {ceiling}"
                ),
            )
        )
    if risk.self_request_budget_per_day > venue.firm_request_threshold_per_day:
        issues.append(
            ConfigIssue(
                source=risk_source,
                location="self_request_budget_per_day",
                message=(
                    f"{risk.self_request_budget_per_day} değeri {venue_source} içindeki "
                    f"firm_request_threshold_per_day ({venue.firm_request_threshold_per_day}) "
                    "değerini aşıyor"
                ),
            )
        )
    return issues
