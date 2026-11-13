"""Position size suggestion (SPEC §8.1): fixed fractional risk, floored to the lot step.

`lots = floor_to_step(risk_budget / (stop_points x value_per_point_per_lot))`. The size
is never rounded up; a size below the minimum lot is skipped, never bumped to it.

Arithmetic is exact: every float is read as the decimal it prints as (`Decimal(str(x))`,
so 0.01 means exactly 0.01) and combined as a rational number. Binary floats would lose
or gain a lot step at exact boundaries (0.3 / 0.1 is 2.9999999999999996).

The functions are pure and stateless. They see the stop distance, the risk config and
the venue profile; never a balance, a P&L or a trade history. A loss therefore cannot
raise the next size (CLAUDE.md invariant 4).
"""

import math
from dataclasses import dataclass
from decimal import Decimal
from enum import StrEnum
from fractions import Fraction
from typing import Final

from propbot.risk.config import MAX_RISK_PER_TRADE_PCT, RiskConfig
from propbot.risk.venue import ContractSpec, VenueProfile


class SizeSkip(StrEnum):
    CONTRACT_UNKNOWN = "contract_unknown"  # a contract.* field is still null in the profile
    RISK_TOO_WIDE = "risk_too_wide"  # even the minimum lot would risk more than the budget


@dataclass(frozen=True, slots=True)
class SizeSuggestion:
    risk_budget: float  # risk_per_trade_pct % of initial_balance (account currency)
    lots: float | None  # None when nothing can be suggested
    risk_at_lots: float | None  # lots x stop_points x value_per_point_per_lot (<= risk_budget)
    skip: SizeSkip | None


def _exact(value: float, name: str) -> Fraction:
    """Read a positive finite float as the exact decimal it prints as.

    Config validation already guarantees this for config values; the check is a second
    layer for objects built without validation (`model_construct`, `model_copy`).
    """
    if not math.isfinite(value) or value <= 0:
        raise ValueError(f"{name} must be a finite number greater than zero; got {value!r}")
    return Fraction(Decimal(str(value)))


_MAX_RISK_PER_TRADE_PCT: Final = _exact(MAX_RISK_PER_TRADE_PCT, "MAX_RISK_PER_TRADE_PCT")


def _risk_budget(risk: RiskConfig, venue: VenueProfile) -> Fraction:
    # RiskConfig already rejects a value above the cap; the `min` keeps the budget capped
    # even if a caller bypassed config validation.
    pct = min(_exact(risk.risk_per_trade_pct, "risk_per_trade_pct"), _MAX_RISK_PER_TRADE_PCT)
    return pct / 100 * _exact(venue.initial_balance, "initial_balance")


def _value_per_point(contract: ContractSpec) -> Fraction | None:
    if contract.tick_size is None or contract.tick_value_per_lot is None:
        return None
    tick_value = _exact(contract.tick_value_per_lot, "contract.tick_value_per_lot")
    return tick_value / _exact(contract.tick_size, "contract.tick_size")


def risk_budget_amount(risk: RiskConfig, venue: VenueProfile) -> float:
    """Per-trade risk budget in account currency, on the initial balance (SPEC §8.1)."""
    return float(_risk_budget(risk, venue))


def value_per_point_per_lot(contract: ContractSpec) -> float | None:
    """Account-currency value of a 1.0 price move for one lot; None while unknown."""
    value = _value_per_point(contract)
    return None if value is None else float(value)


def suggest_size(*, stop_points: float, risk: RiskConfig, venue: VenueProfile) -> SizeSuggestion:
    """Largest lot size, in whole lot steps, whose loss at the stop fits the risk budget.

    `stop_points` is the distance between entry and stop in price units. Raises
    `ValueError` when it is not finite and positive.
    """
    stop = _exact(stop_points, "stop_points")
    budget = _risk_budget(risk, venue)
    risk_budget = float(budget)

    contract = venue.contract
    point_value = _value_per_point(contract)
    if point_value is None or contract.min_lot is None or contract.lot_step is None:
        return SizeSuggestion(risk_budget, None, None, SizeSkip.CONTRACT_UNKNOWN)

    lot_step = _exact(contract.lot_step, "contract.lot_step")
    min_lot = _exact(contract.min_lot, "contract.min_lot")
    risk_per_lot = stop * point_value
    lots = math.floor(budget / (risk_per_lot * lot_step)) * lot_step
    if lots < min_lot:
        return SizeSuggestion(risk_budget, None, None, SizeSkip.RISK_TOO_WIDE)
    # float() of a rational is correctly rounded: 1968/100 becomes the float that prints 19.68.
    return SizeSuggestion(risk_budget, float(lots), float(lots * risk_per_lot), None)
