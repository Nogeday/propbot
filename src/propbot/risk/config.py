"""Risk defaults schema (`config/risk.yaml`) and the hard caps enforced in code.

The caps below hold no matter what the YAML says: a value above a cap is a load
error, never silently clamped (SPEC §8.1, ADR-0003).
"""

from typing import Annotated, Final

from pydantic import BaseModel, ConfigDict, Field

# SPEC §8.1: per-trade risk default 0.25, code ceiling 0.5 (% of initial balance).
MAX_RISK_PER_TRADE_PCT: Final = 0.5
# SPEC §8.1: daily budget = min(daily_cap_pct, 0.5 x venue daily loss limit).
DAILY_CAP_MAX_FRACTION_OF_DAILY_LOSS_LIMIT: Final = 0.5
# SPEC §8.1: one concurrent position per strategy.
MAX_POSITIONS_PER_STRATEGY: Final = 1
# CLAUDE.md invariant 8: at most 300 server requests per day per account.
MAX_SELF_REQUESTS_PER_DAY: Final = 300


class StrictModel(BaseModel):
    """Base for config models: no type coercion, no unknown keys, no NaN/inf, immutable."""

    model_config = ConfigDict(strict=True, extra="forbid", frozen=True, allow_inf_nan=False)


Percent = Annotated[float, Field(gt=0, lt=100)]
PositiveInt = Annotated[int, Field(ge=1)]


class RiskConfig(StrictModel):
    """Every key is required: defaults live in `config/risk.yaml`, not in code."""

    risk_per_trade_pct: float = Field(gt=0, le=MAX_RISK_PER_TRADE_PCT)
    daily_cap_pct: Percent
    weekly_cap_pct: Percent
    flatten_when_daily_buffer_below_pct: float = Field(ge=0, lt=100)
    max_consecutive_losses: PositiveInt
    max_positions_total: PositiveInt
    max_positions_per_strategy: int = Field(ge=1, le=MAX_POSITIONS_PER_STRATEGY)
    self_request_budget_per_day: int = Field(ge=1, le=MAX_SELF_REQUESTS_PER_DAY)
    trail_min_step_ticks: PositiveInt
    signal_validity_seconds: PositiveInt
    signal_max_drift_frac_of_stop: float = Field(gt=0, le=1)
