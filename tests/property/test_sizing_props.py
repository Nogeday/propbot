"""Property tests for the size suggestion: the budget is a ceiling, the lot step a grid.

The oracle works on exact rationals and reads inputs the way the sizing module does:
every float is the decimal it prints as. Nothing here re-implements the floor; the
properties only state what a correct floor must satisfy.
"""

from decimal import Decimal
from fractions import Fraction

from hypothesis import given
from hypothesis import strategies as st

from propbot.risk import (
    MAX_RISK_PER_TRADE_PCT,
    ContractSpec,
    RiskConfig,
    VenueProfile,
    load_risk_config,
    load_venue_profile,
)
from propbot.risk.sizing import (
    SizeSkip,
    SizeSuggestion,
    risk_budget_amount,
    suggest_size,
    value_per_point_per_lot,
)
from support.paths import CONFIG_FIXTURES

BASE_RISK = load_risk_config(CONFIG_FIXTURES / "valid" / "risk.yaml")
BASE_VENUE = load_venue_profile(CONFIG_FIXTURES / "valid" / "venues" / "sample_2step.yaml")

# 0.01 % .. 0.50 %: everything RiskConfig accepts, in hundredths of a percent.
RISK_PCTS = st.integers(1, 50).map(lambda n: n / 100)
# Above the code ceiling: only reachable by skipping validation.
EXCESSIVE_RISK_PCTS = st.one_of(
    st.integers(51, 10_000).map(lambda n: n / 100), st.floats(0.5, 1e6, exclude_min=True)
)
# 1 000.00 .. 1 000 000.00 in cents.
BALANCES = st.integers(100_000, 100_000_000).map(lambda cents: cents / 100)
# Any float in a wide range of stop distances, not only whole ticks.
STOPS = st.floats(min_value=0.0001, max_value=50_000.0, allow_nan=False)
TICK_SIZES = st.sampled_from([0.00001, 0.0001, 0.01, 0.1, 0.25, 0.5, 1.0])
TICK_VALUES = st.sampled_from([0.001, 0.01, 0.1, 0.25, 0.5, 1.0, 1.25, 12.5])
# Independent draws: min_lot is not always a multiple of lot_step.
MIN_LOTS = st.sampled_from([0.001, 0.01, 0.1, 0.15, 0.25, 1.0, 2.0])
LOT_STEPS = st.sampled_from([0.001, 0.01, 0.1, 0.25, 1.0])
CONTRACTS = st.builds(
    ContractSpec,
    tick_size=TICK_SIZES,
    tick_value_per_lot=TICK_VALUES,
    min_lot=MIN_LOTS,
    lot_step=LOT_STEPS,
)
# At least one field null.
UNKNOWN_CONTRACTS = st.builds(
    ContractSpec,
    tick_size=st.none() | TICK_SIZES,
    tick_value_per_lot=st.none() | TICK_VALUES,
    min_lot=st.none() | MIN_LOTS,
    lot_step=st.none() | LOT_STEPS,
).filter(
    lambda c: None in (c.tick_size, c.tick_value_per_lot, c.min_lot, c.lot_step),
)


def exact(value: float) -> Fraction:
    return Fraction(Decimal(str(value)))


def risk_with(pct: float) -> RiskConfig:
    """Validated for pct <= 0.5; model_copy skips validation, which the cap tests rely on."""
    return BASE_RISK.model_copy(update={"risk_per_trade_pct": pct})


def venue_with(balance: float, contract: ContractSpec) -> VenueProfile:
    return BASE_VENUE.model_copy(update={"initial_balance": balance, "contract": contract})


def exact_budget(pct: float, balance: float) -> Fraction:
    return exact(pct) / 100 * exact(balance)


def exact_risk_per_lot(stop_points: float, contract: ContractSpec) -> Fraction:
    assert contract.tick_size is not None
    assert contract.tick_value_per_lot is not None
    return exact(stop_points) * exact(contract.tick_value_per_lot) / exact(contract.tick_size)


def lots_or_zero(suggestion: SizeSuggestion) -> float:
    return 0.0 if suggestion.lots is None else suggestion.lots


Case = tuple[float, float, float, ContractSpec]  # pct, balance, stop_points, contract

# How many min_lots fit the budget at the generated stop; 1 is the RISK_TOO_WIDE boundary.
MIN_LOT_RATIOS = st.sampled_from(
    [Fraction(n, 100) for n in (50, 99, 100, 101, 150, 199, 200, 201, 250, 1000)]
)


@st.composite
def cases_near_min_lot(draw: st.DrawFn) -> Case:
    """Stops at and around the distance where only the minimum lot still fits the budget."""
    pct, balance, contract = draw(RISK_PCTS), draw(BALANCES), draw(CONTRACTS)
    assert contract.min_lot is not None
    risk_at_min_lot_per_point = exact(contract.min_lot) * exact_risk_per_lot(1.0, contract)
    stop = exact_budget(pct, balance) / (risk_at_min_lot_per_point * draw(MIN_LOT_RATIOS))
    return pct, balance, float(stop), contract


CASES = st.one_of(st.tuples(RISK_PCTS, BALANCES, STOPS, CONTRACTS), cases_near_min_lot())


@given(case=CASES)
def test_suggested_size_never_risks_more_than_the_budget(case: Case) -> None:
    pct, balance, stop_points, contract = case
    suggestion = suggest_size(
        stop_points=stop_points, risk=risk_with(pct), venue=venue_with(balance, contract)
    )
    budget = exact_budget(pct, balance)

    assert suggestion.risk_budget == float(budget)
    assert budget <= exact(MAX_RISK_PER_TRADE_PCT) / 100 * exact(balance)
    if suggestion.lots is not None:
        assert suggestion.risk_at_lots is not None
        assert exact(suggestion.lots) * exact_risk_per_lot(stop_points, contract) <= budget
        assert suggestion.risk_at_lots <= suggestion.risk_budget


@given(case=CASES)
def test_suggested_size_is_a_positive_multiple_of_the_lot_step(case: Case) -> None:
    pct, balance, stop_points, contract = case
    suggestion = suggest_size(
        stop_points=stop_points, risk=risk_with(pct), venue=venue_with(balance, contract)
    )

    if suggestion.lots is not None:
        assert contract.lot_step is not None
        # `exact` reads the float as it prints, so this also proves the size prints as a
        # plain lot value: 0.37000000000000005 would not be a whole number of 0.01 steps.
        steps = exact(suggestion.lots) / exact(contract.lot_step)
        assert steps.denominator == 1
        assert steps >= 1


@given(case=CASES)
def test_size_is_at_least_min_lot_or_the_signal_is_skipped(case: Case) -> None:
    pct, balance, stop_points, contract = case
    suggestion = suggest_size(
        stop_points=stop_points, risk=risk_with(pct), venue=venue_with(balance, contract)
    )
    assert contract.min_lot is not None

    if suggestion.skip is None:
        assert suggestion.lots is not None
        assert suggestion.risk_at_lots is not None
        assert exact(suggestion.lots) >= exact(contract.min_lot)
    else:
        assert suggestion.skip is SizeSkip.RISK_TOO_WIDE
        assert suggestion.lots is None
        assert suggestion.risk_at_lots is None


@given(case=CASES)
def test_size_is_the_largest_step_multiple_within_the_budget(case: Case) -> None:
    pct, balance, stop_points, contract = case
    suggestion = suggest_size(
        stop_points=stop_points, risk=risk_with(pct), venue=venue_with(balance, contract)
    )
    assert contract.lot_step is not None
    assert contract.min_lot is not None
    budget = exact_budget(pct, balance)
    risk_per_lot = exact_risk_per_lot(stop_points, contract)
    step = exact(contract.lot_step)

    if suggestion.lots is not None:
        # Not needlessly small: one more step would break the budget.
        assert (exact(suggestion.lots) + step) * risk_per_lot > budget
    else:
        # Skipped for a reason: the smallest tradable size (first step multiple at or
        # above min_lot) does not fit the budget.
        smallest = -(-exact(contract.min_lot) // step) * step
        assert smallest * risk_per_lot > budget


@given(case=CASES)
def test_reported_risk_is_lots_times_stop_times_point_value(case: Case) -> None:
    pct, balance, stop_points, contract = case
    suggestion = suggest_size(
        stop_points=stop_points, risk=risk_with(pct), venue=venue_with(balance, contract)
    )

    if suggestion.lots is not None:
        expected = exact(suggestion.lots) * exact_risk_per_lot(stop_points, contract)
        assert suggestion.risk_at_lots == float(expected)


@given(
    pct=RISK_PCTS,
    balance=BALANCES,
    stops=st.tuples(STOPS, STOPS).map(sorted),
    contract=CONTRACTS,
)
def test_size_never_grows_when_the_stop_gets_wider(
    pct: float, balance: float, stops: list[float], contract: ContractSpec
) -> None:
    risk, venue = risk_with(pct), venue_with(balance, contract)
    tighter = suggest_size(stop_points=stops[0], risk=risk, venue=venue)
    wider = suggest_size(stop_points=stops[1], risk=risk, venue=venue)

    assert lots_or_zero(wider) <= lots_or_zero(tighter)
    if tighter.skip is SizeSkip.RISK_TOO_WIDE:
        assert wider.skip is SizeSkip.RISK_TOO_WIDE


@given(
    pcts=st.tuples(RISK_PCTS, RISK_PCTS).map(sorted),
    balance=BALANCES,
    stop_points=STOPS,
    contract=CONTRACTS,
)
def test_size_never_grows_when_the_risk_setting_gets_smaller(
    pcts: list[float], balance: float, stop_points: float, contract: ContractSpec
) -> None:
    venue = venue_with(balance, contract)
    smaller = suggest_size(stop_points=stop_points, risk=risk_with(pcts[0]), venue=venue)
    larger = suggest_size(stop_points=stop_points, risk=risk_with(pcts[1]), venue=venue)

    assert smaller.risk_budget <= larger.risk_budget
    assert lots_or_zero(smaller) <= lots_or_zero(larger)


@given(pct=EXCESSIVE_RISK_PCTS, balance=BALANCES, stop_points=STOPS, contract=CONTRACTS)
def test_unvalidated_excessive_risk_sizes_like_the_code_ceiling(
    pct: float, balance: float, stop_points: float, contract: ContractSpec
) -> None:
    venue = venue_with(balance, contract)
    capped = suggest_size(stop_points=stop_points, risk=risk_with(pct), venue=venue)
    at_ceiling = suggest_size(
        stop_points=stop_points, risk=risk_with(MAX_RISK_PER_TRADE_PCT), venue=venue
    )

    assert capped == at_ceiling
    assert risk_budget_amount(risk_with(pct), venue) == float(
        exact_budget(MAX_RISK_PER_TRADE_PCT, balance)
    )


@given(pct=RISK_PCTS, balance=BALANCES, stop_points=STOPS, contract=UNKNOWN_CONTRACTS)
def test_incomplete_contract_never_gets_a_size_and_never_raises(
    pct: float, balance: float, stop_points: float, contract: ContractSpec
) -> None:
    risk, venue = risk_with(pct), venue_with(balance, contract)

    suggestion = suggest_size(stop_points=stop_points, risk=risk, venue=venue)

    assert suggestion == SizeSuggestion(
        risk_budget=float(exact_budget(pct, balance)),
        lots=None,
        risk_at_lots=None,
        skip=SizeSkip.CONTRACT_UNKNOWN,
    )


@given(contract=CONTRACTS)
def test_point_value_is_the_exact_tick_ratio(contract: ContractSpec) -> None:
    assert contract.tick_size is not None
    assert contract.tick_value_per_lot is not None

    assert value_per_point_per_lot(contract) == float(
        exact(contract.tick_value_per_lot) / exact(contract.tick_size)
    )


@given(
    pct=RISK_PCTS,
    balance=BALANCES,
    stop_points=STOPS,
    contract=CONTRACTS,
    history=st.lists(STOPS, max_size=8),
)
def test_earlier_signals_never_change_a_suggestion(
    pct: float, balance: float, stop_points: float, contract: ContractSpec, history: list[float]
) -> None:
    # No state and no P&L input: whatever was sized (and lost) before, the answer is the
    # same, so risk cannot be raised after a loss (CLAUDE.md invariant 4).
    risk, venue = risk_with(pct), venue_with(balance, contract)
    fresh = suggest_size(stop_points=stop_points, risk=risk, venue=venue)

    for earlier in history:
        suggest_size(stop_points=earlier, risk=risk, venue=venue)

    assert suggest_size(stop_points=stop_points, risk=risk, venue=venue) == fresh
