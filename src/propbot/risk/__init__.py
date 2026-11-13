"""Risk configuration: validated `config/risk.yaml` and venue profiles."""

from propbot.risk.config import (
    DAILY_CAP_MAX_FRACTION_OF_DAILY_LOSS_LIMIT,
    MAX_POSITIONS_PER_STRATEGY,
    MAX_RISK_PER_TRADE_PCT,
    MAX_SELF_REQUESTS_PER_DAY,
    RiskConfig,
)
from propbot.risk.errors import ConfigError, ConfigIssue
from propbot.risk.loader import (
    LoadedConfig,
    load_config_dir,
    load_risk_config,
    load_venue_profile,
)
from propbot.risk.rules import (
    cross_check,
    daily_budget_amount,
    daily_budget_pct,
    daily_cap_ceiling_pct,
)
from propbot.risk.venue import (
    RULES_MAX_AGE_DAYS,
    ContractSpec,
    DailyLossBasis,
    DailyReset,
    MaxLossType,
    NewsRule,
    Platform,
    ProfitTargets,
    RulesFreshness,
    RulesState,
    UnresolvedField,
    UnresolvedKind,
    VenueProfile,
    rules_freshness,
    unresolved_fields,
)

__all__ = [
    "DAILY_CAP_MAX_FRACTION_OF_DAILY_LOSS_LIMIT",
    "MAX_POSITIONS_PER_STRATEGY",
    "MAX_RISK_PER_TRADE_PCT",
    "MAX_SELF_REQUESTS_PER_DAY",
    "RULES_MAX_AGE_DAYS",
    "ConfigError",
    "ConfigIssue",
    "ContractSpec",
    "DailyLossBasis",
    "DailyReset",
    "LoadedConfig",
    "MaxLossType",
    "NewsRule",
    "Platform",
    "ProfitTargets",
    "RiskConfig",
    "RulesFreshness",
    "RulesState",
    "UnresolvedField",
    "UnresolvedKind",
    "VenueProfile",
    "cross_check",
    "daily_budget_amount",
    "daily_budget_pct",
    "daily_cap_ceiling_pct",
    "load_config_dir",
    "load_risk_config",
    "load_venue_profile",
    "rules_freshness",
    "unresolved_fields",
]
