"""Position size suggestion: budget, exact flooring, skip reasons and input checks (SPEC §8.1)."""

import dataclasses
import inspect
import math
from typing import Any

import pytest

from propbot.risk import MAX_RISK_PER_TRADE_PCT, ContractSpec, RiskConfig, VenueProfile
from propbot.risk.loader import read_yaml_mapping
from propbot.risk.sizing import (
    SizeSkip,
    SizeSuggestion,
    risk_budget_amount,
    suggest_size,
    value_per_point_per_lot,
)
from support.paths import CONFIG_FIXTURES

RISK_FILE = CONFIG_FIXTURES / "valid" / "risk.yaml"
VENUE_FILE = CONFIG_FIXTURES / "valid" / "venues" / "sample_2step.yaml"

CONTRACT_FIELDS = ("tick_size", "tick_value_per_lot", "min_lot", "lot_step")
# Index CFD: one lot earns 1.0 per point, sizes in hundredths of a lot.
INDEX_CFD = {"tick_size": 0.01, "tick_value_per_lot": 0.01, "min_lot": 0.01, "lot_step": 0.01}
# Futures-like: 5.0 per point, whole contracts only.
WHOLE_LOTS = {"tick_size": 0.25, "tick_value_per_lot": 1.25, "min_lot": 1.0, "lot_step": 1.0}
# FX-like: five-digit quote, 100000 per 1.0 of price.
FIVE_DIGITS = {"tick_size": 0.00001, "tick_value_per_lot": 1.0, "min_lot": 0.01, "lot_step": 0.01}
TENTHS = {"tick_size": 0.1, "tick_value_per_lot": 0.1, "min_lot": 0.1, "lot_step": 0.1}
QUARTERS = {"tick_size": 0.01, "tick_value_per_lot": 0.01, "min_lot": 0.25, "lot_step": 0.25}
# 0.1 per point: the naive float formula floor(250 / (0.1 * 0.1) / 0.01) loses a step here.
CHEAP_POINT = {"tick_size": 0.1, "tick_value_per_lot": 0.01, "min_lot": 0.01, "lot_step": 0.01}
MIN_ABOVE_STEP = {"tick_size": 0.01, "tick_value_per_lot": 0.01, "min_lot": 0.1, "lot_step": 0.01}
MIN_OFF_GRID = {"tick_size": 0.01, "tick_value_per_lot": 0.01, "min_lot": 0.15, "lot_step": 0.1}

INVALID_NUMBERS = [0.0, -0.0, -1.0, -12.5, math.nan, math.inf, -math.inf]
INVALID_IDS = ["zero", "negative_zero", "minus_one", "negative", "nan", "inf", "minus_inf"]


def risk_config(pct: float = 0.25) -> RiskConfig:
    """The fixture risk file with the per-trade risk under test (validated)."""
    data = read_yaml_mapping(RISK_FILE)
    data["risk_per_trade_pct"] = pct
    return RiskConfig.model_validate(data)


def venue_profile(*, balance: float = 100_000.0, **contract: float | None) -> VenueProfile:
    """The fixture venue with the balance and contract under test (validated)."""
    data = read_yaml_mapping(VENUE_FILE)
    data["initial_balance"] = balance
    data["contract"] = INDEX_CFD | contract
    return VenueProfile.model_validate(data)


def unvalidated_risk(pct: float) -> RiskConfig:
    """A RiskConfig that skipped validation, as a careless caller could build it."""
    return risk_config().model_copy(update={"risk_per_trade_pct": pct})


def unvalidated_venue(**changes: Any) -> VenueProfile:
    """A VenueProfile that skipped validation; contract fields go into the nested model."""
    venue = venue_profile()
    contract = {name: changes.pop(name) for name in CONTRACT_FIELDS if name in changes}
    return venue.model_copy(
        update={**changes, "contract": venue.contract.model_copy(update=contract)}
    )


# --- risk budget ----------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("pct", "balance", "expected"),
    [
        (0.25, 100_000.0, 250.0),
        (0.5, 100_000.0, 500.0),
        (0.25, 10_000.0, 25.0),
        (0.1, 100_000.0, 100.0),
        (0.3, 99_999.99, 299.99997),  # exact decimal product, no binary noise
        (0.07, 25_000.0, 17.5),
    ],
)
def test_risk_budget_is_a_percentage_of_the_initial_balance(
    pct: float, balance: float, expected: float
) -> None:
    budget = risk_budget_amount(risk_config(pct), venue_profile(balance=balance))

    assert budget == expected
    assert isinstance(budget, float)


def test_risk_budget_does_not_need_the_contract() -> None:
    venue = venue_profile(tick_size=None, tick_value_per_lot=None, min_lot=None, lot_step=None)

    assert risk_budget_amount(risk_config(), venue) == 250.0


@pytest.mark.parametrize("pct", [0.500001, 0.6, 2.0, 50.0, 100.0, 1e9])
def test_risk_above_the_code_ceiling_is_capped_even_without_validation(pct: float) -> None:
    risk = unvalidated_risk(pct)
    venue = venue_profile()
    at_ceiling = suggest_size(
        stop_points=12.5, risk=risk_config(MAX_RISK_PER_TRADE_PCT), venue=venue
    )

    assert risk_budget_amount(risk, venue) == MAX_RISK_PER_TRADE_PCT / 100.0 * 100_000.0 == 500.0
    assert suggest_size(stop_points=12.5, risk=risk, venue=venue) == at_ceiling
    assert at_ceiling == SizeSuggestion(500.0, 40.0, 500.0, None)


@pytest.mark.parametrize("pct", INVALID_NUMBERS, ids=INVALID_IDS)
def test_unvalidated_nonsense_risk_is_refused(pct: float) -> None:
    risk = unvalidated_risk(pct)

    with pytest.raises(ValueError, match="risk_per_trade_pct"):
        risk_budget_amount(risk, venue_profile())
    with pytest.raises(ValueError, match="risk_per_trade_pct"):
        suggest_size(stop_points=12.5, risk=risk, venue=venue_profile())


@pytest.mark.parametrize("balance", INVALID_NUMBERS, ids=INVALID_IDS)
def test_unvalidated_nonsense_balance_is_refused(balance: float) -> None:
    venue = unvalidated_venue(initial_balance=balance)

    with pytest.raises(ValueError, match="initial_balance"):
        risk_budget_amount(risk_config(), venue)
    with pytest.raises(ValueError, match="initial_balance"):
        suggest_size(stop_points=12.5, risk=risk_config(), venue=venue)


# --- value per point ------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("tick_size", "tick_value", "expected"),
    [
        (0.01, 0.01, 1.0),
        (0.25, 1.25, 5.0),
        (0.25, 12.5, 50.0),
        (0.1, 0.01, 0.1),
        (0.01, 0.03, 3.0),
        (0.00001, 1.0, 100_000.0),  # float division gives 99999.99999999999
    ],
)
def test_value_per_point_is_tick_value_over_tick_size(
    tick_size: float, tick_value: float, expected: float
) -> None:
    contract = ContractSpec(
        tick_size=tick_size, tick_value_per_lot=tick_value, min_lot=0.01, lot_step=0.01
    )

    assert value_per_point_per_lot(contract) == expected


@pytest.mark.parametrize(("tick_size", "tick_value"), [(None, 0.01), (0.01, None), (None, None)])
def test_value_per_point_is_unknown_while_a_tick_field_is_null(
    tick_size: float | None, tick_value: float | None
) -> None:
    contract = ContractSpec(
        tick_size=tick_size, tick_value_per_lot=tick_value, min_lot=0.01, lot_step=0.01
    )

    assert value_per_point_per_lot(contract) is None


def test_value_per_point_does_not_need_the_lot_fields() -> None:
    contract = ContractSpec(tick_size=0.25, tick_value_per_lot=1.25, min_lot=None, lot_step=None)

    assert value_per_point_per_lot(contract) == 5.0


# --- suggested size -------------------------------------------------------------------------


def test_worked_example_stop_divides_the_budget_exactly() -> None:
    suggestion = suggest_size(stop_points=12.5, risk=risk_config(0.25), venue=venue_profile())

    assert suggestion == SizeSuggestion(risk_budget=250.0, lots=20.0, risk_at_lots=250.0, skip=None)


def test_worked_example_floors_to_the_lot_step() -> None:
    # 250 / 12.7 = 19.685...: 19.68 lots, never 19.69.
    suggestion = suggest_size(stop_points=12.7, risk=risk_config(0.25), venue=venue_profile())

    assert suggestion == SizeSuggestion(
        risk_budget=250.0, lots=19.68, risk_at_lots=249.936, skip=None
    )
    assert repr(suggestion.lots) == "19.68"
    assert repr(suggestion.risk_at_lots) == "249.936"


@pytest.mark.parametrize(
    ("contract", "stop_points", "lots", "risk_at_lots"),
    [
        pytest.param(INDEX_CFD, 12.501, 19.99, 249.89499, id="just_below_a_step_19.9984"),
        pytest.param(INDEX_CFD, 12.499, 20.0, 249.98, id="just_above_a_step_20.0016"),
        pytest.param(INDEX_CFD, 0.01, 25_000.0, 250.0, id="one_tick_stop"),
        pytest.param(WHOLE_LOTS, 12.5, 4.0, 250.0, id="whole_lots_exact"),
        pytest.param(WHOLE_LOTS, 12.75, 3.0, 191.25, id="whole_lots_3.92_floors_to_3"),
        pytest.param(WHOLE_LOTS, 10.25, 4.0, 205.0, id="whole_lots_4.88_floors_to_4"),
        pytest.param(TENTHS, 7.3, 34.2, 249.66, id="tenths_34.2465"),
        pytest.param(QUARTERS, 100.0, 2.5, 250.0, id="quarter_step_exact"),
        pytest.param(QUARTERS, 110.0, 2.25, 247.5, id="quarter_step_2.27_floors_to_2.25"),
        pytest.param(FIVE_DIGITS, 0.00125, 2.0, 250.0, id="five_digit_quote_exact"),
        pytest.param(FIVE_DIGITS, 0.0013, 1.92, 249.6, id="five_digit_quote_1.923"),
        pytest.param(CHEAP_POINT, 0.1, 25_000.0, 250.0, id="float_formula_would_lose_a_step"),
        pytest.param(CHEAP_POINT, 0.8, 3125.0, 250.0, id="float_formula_would_print_noise"),
    ],
)
def test_lots_are_floored_to_the_lot_step(
    contract: dict[str, float], stop_points: float, lots: float, risk_at_lots: float
) -> None:
    suggestion = suggest_size(
        stop_points=stop_points, risk=risk_config(0.25), venue=venue_profile(**contract)
    )

    assert suggestion == SizeSuggestion(250.0, lots, risk_at_lots, None)
    assert repr(suggestion.lots) == repr(lots)
    assert repr(suggestion.risk_at_lots) == repr(risk_at_lots)


@pytest.mark.parametrize(
    ("pct", "balance", "lots", "risk_at_lots"),
    [
        (0.25, 100_000.0, 19.68, 249.936),
        (0.5, 100_000.0, 39.37, 499.999),
        (0.1, 100_000.0, 7.87, 99.949),
        (0.25, 10_000.0, 1.96, 24.892),
        (0.25, 200_000.0, 39.37, 499.999),
    ],
)
def test_size_scales_with_the_budget_only(
    pct: float, balance: float, lots: float, risk_at_lots: float
) -> None:
    suggestion = suggest_size(
        stop_points=12.7, risk=risk_config(pct), venue=venue_profile(balance=balance)
    )

    assert (suggestion.lots, suggestion.risk_at_lots, suggestion.skip) == (
        lots,
        risk_at_lots,
        None,
    )


@pytest.mark.parametrize(
    ("contract", "stop_points", "floor_lots"),
    [
        (INDEX_CFD, 12.7, 19.68),
        (INDEX_CFD, 12.501, 19.99),
        (INDEX_CFD, 12.500001, 19.99),  # 19.9999984: a hair below 20.00 is still 19.99
        (WHOLE_LOTS, 12.75, 3.0),
        (WHOLE_LOTS, 12.500001, 3.0),  # 3.99999968 contracts: 3, not 4
        (TENTHS, 7.3, 34.2),
    ],
)
def test_size_is_never_rounded_up(
    contract: dict[str, float], stop_points: float, floor_lots: float
) -> None:
    risk, venue = risk_config(0.25), venue_profile(**contract)
    suggestion = suggest_size(stop_points=stop_points, risk=risk, venue=venue)
    point_value = value_per_point_per_lot(venue.contract)

    assert point_value is not None
    assert suggestion.lots == floor_lots
    assert suggestion.risk_at_lots is not None
    assert suggestion.risk_at_lots < suggestion.risk_budget
    # One more step would not fit the budget (wide margin, so float noise cannot decide it).
    next_step = floor_lots + contract["lot_step"]
    assert next_step * stop_points * point_value > 250.0 * (1 + 1e-9)


@pytest.mark.parametrize(
    ("contract", "stop_points", "lots"),
    [
        pytest.param(INDEX_CFD, 25_000.0, 0.01, id="exactly_min_lot"),
        pytest.param(INDEX_CFD, 24_999.99, 0.01, id="a_tick_tighter"),
        pytest.param(INDEX_CFD, 12_500.01, 0.01, id="1.99_steps_floor_to_min_lot"),
        pytest.param(WHOLE_LOTS, 50.0, 1.0, id="whole_lots_exactly_one"),
        pytest.param(MIN_ABOVE_STEP, 2_500.0, 0.1, id="min_above_step_exactly_min"),
        pytest.param(MIN_OFF_GRID, 1_250.0, 0.2, id="min_off_grid_next_step_fits"),
    ],
)
def test_stop_at_the_min_lot_boundary_still_gets_a_size(
    contract: dict[str, float], stop_points: float, lots: float
) -> None:
    suggestion = suggest_size(
        stop_points=stop_points, risk=risk_config(0.25), venue=venue_profile(**contract)
    )

    assert suggestion.lots == lots
    assert suggestion.skip is None
    assert suggestion.risk_at_lots is not None
    assert suggestion.risk_at_lots <= 250.0


@pytest.mark.parametrize(
    ("contract", "stop_points"),
    [
        pytest.param(INDEX_CFD, 25_000.01, id="a_tick_past_min_lot"),
        pytest.param(INDEX_CFD, 25_000.000001, id="a_hair_past_min_lot"),
        pytest.param(INDEX_CFD, 1e9, id="absurdly_wide"),
        pytest.param(WHOLE_LOTS, 50.25, id="whole_lots_0.995"),
        pytest.param(WHOLE_LOTS, 50.000001, id="whole_lots_a_hair_below_one"),
        # 0.09 lots would fit the budget and is a whole number of steps, but is below min_lot.
        pytest.param(MIN_ABOVE_STEP, 2_500.01, id="min_above_step_0.09"),
        # Floor first, then compare: 0.1 lots is below min_lot 0.15 and is not bumped up to it.
        pytest.param(MIN_OFF_GRID, 1_250.01, id="min_off_grid_0.1"),
    ],
)
def test_stop_too_wide_for_the_min_lot_is_skipped(
    contract: dict[str, float], stop_points: float
) -> None:
    suggestion = suggest_size(
        stop_points=stop_points, risk=risk_config(0.25), venue=venue_profile(**contract)
    )

    assert suggestion == SizeSuggestion(
        risk_budget=250.0, lots=None, risk_at_lots=None, skip=SizeSkip.RISK_TOO_WIDE
    )


# --- unknown contract -----------------------------------------------------------------------


@pytest.mark.parametrize("field", CONTRACT_FIELDS)
def test_any_single_null_contract_field_means_contract_unknown(field: str) -> None:
    venue = venue_profile(**{field: None})

    suggestion = suggest_size(stop_points=12.5, risk=risk_config(0.25), venue=venue)

    assert suggestion == SizeSuggestion(
        risk_budget=250.0, lots=None, risk_at_lots=None, skip=SizeSkip.CONTRACT_UNKNOWN
    )


def test_fully_null_contract_means_contract_unknown() -> None:
    venue = venue_profile(tick_size=None, tick_value_per_lot=None, min_lot=None, lot_step=None)

    suggestion = suggest_size(stop_points=12.5, risk=risk_config(0.5), venue=venue)

    assert suggestion == SizeSuggestion(500.0, None, None, SizeSkip.CONTRACT_UNKNOWN)


@pytest.mark.parametrize("field", CONTRACT_FIELDS)
def test_contract_unknown_wins_over_risk_too_wide(field: str) -> None:
    # With a null field nothing can be said about the size, however wide the stop is.
    venue = venue_profile(**{field: None})

    suggestion = suggest_size(stop_points=1e9, risk=risk_config(0.25), venue=venue)

    assert suggestion.skip is SizeSkip.CONTRACT_UNKNOWN


@pytest.mark.parametrize("field", CONTRACT_FIELDS)
@pytest.mark.parametrize("value", INVALID_NUMBERS, ids=INVALID_IDS)
def test_unvalidated_nonsense_contract_value_is_refused(field: str, value: float) -> None:
    venue = unvalidated_venue(**{field: value})

    with pytest.raises(ValueError, match=f"contract.{field}"):
        suggest_size(stop_points=12.5, risk=risk_config(0.25), venue=venue)


@pytest.mark.parametrize("field", ["tick_size", "tick_value_per_lot"])
@pytest.mark.parametrize("value", INVALID_NUMBERS, ids=INVALID_IDS)
def test_unvalidated_nonsense_tick_value_has_no_point_value(field: str, value: float) -> None:
    contract = unvalidated_venue(**{field: value}).contract

    with pytest.raises(ValueError, match=f"contract.{field}"):
        value_per_point_per_lot(contract)


# --- stop distance --------------------------------------------------------------------------


@pytest.mark.parametrize("stop_points", INVALID_NUMBERS, ids=INVALID_IDS)
def test_invalid_stop_distance_is_an_error(stop_points: float) -> None:
    with pytest.raises(ValueError, match="stop_points"):
        suggest_size(stop_points=stop_points, risk=risk_config(), venue=venue_profile())


@pytest.mark.parametrize("stop_points", INVALID_NUMBERS, ids=INVALID_IDS)
def test_invalid_stop_distance_is_an_error_even_when_the_contract_is_unknown(
    stop_points: float,
) -> None:
    venue = venue_profile(tick_size=None, tick_value_per_lot=None, min_lot=None, lot_step=None)

    with pytest.raises(ValueError, match="stop_points"):
        suggest_size(stop_points=stop_points, risk=risk_config(), venue=venue)


def test_integer_stop_distance_is_accepted() -> None:
    # JSON numbers such as 25 arrive as int; the distance is the same.
    as_int = suggest_size(stop_points=25, risk=risk_config(), venue=venue_profile())

    assert as_int == suggest_size(stop_points=25.0, risk=risk_config(), venue=venue_profile())
    assert as_int.lots == 10.0


# --- shape: no state, no P&L input (CLAUDE.md invariant 4) ----------------------------------


def test_suggest_size_takes_only_stop_risk_config_and_venue() -> None:
    parameters = inspect.signature(suggest_size).parameters

    assert list(parameters) == ["stop_points", "risk", "venue"]
    assert all(p.kind is inspect.Parameter.KEYWORD_ONLY for p in parameters.values())


def test_suggestion_does_not_depend_on_earlier_calls() -> None:
    risk, venue = risk_config(0.25), venue_profile()
    first = suggest_size(stop_points=12.7, risk=risk, venue=venue)

    # A run of skipped and wide-stop signals in between changes nothing.
    for stop_points in (25_000.01, 1e9, 0.01, 3.3, 25_000.01):
        suggest_size(stop_points=stop_points, risk=risk, venue=venue)

    assert suggest_size(stop_points=12.7, risk=risk, venue=venue) == first


def test_size_ignores_every_other_risk_setting() -> None:
    venue = venue_profile()
    base = risk_config(0.25)
    other = base.model_copy(
        update={
            "daily_cap_pct": 2.5,
            "weekly_cap_pct": 9.0,
            "max_consecutive_losses": 1,
            "max_positions_total": 7,
            "trail_min_step_ticks": 1,
        }
    )

    assert suggest_size(stop_points=12.7, risk=other, venue=venue) == suggest_size(
        stop_points=12.7, risk=base, venue=venue
    )


def test_suggestion_is_immutable() -> None:
    suggestion = suggest_size(stop_points=12.5, risk=risk_config(), venue=venue_profile())

    with pytest.raises(dataclasses.FrozenInstanceError):
        suggestion.lots = 40.0  # type: ignore[misc]
    assert not hasattr(suggestion, "__dict__")


def test_skip_reasons_have_stable_wire_values() -> None:
    assert {member.name: member.value for member in SizeSkip} == {
        "CONTRACT_UNKNOWN": "contract_unknown",
        "RISK_TOO_WIDE": "risk_too_wide",
    }
