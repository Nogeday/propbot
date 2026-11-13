"""Monte Carlo prop simulator: rule mechanics on scripted days, theory check, determinism."""

import math
from collections.abc import Sequence

import numpy as np
import pytest

from propbot.backtest.montecarlo import (
    DEFAULT_PATHS,
    BreachReason,
    ChallengeRules,
    MonteCarloParams,
    PathOutcomes,
    PathStatus,
    RiskStep,
    SimulationInputError,
    simulate,
    standard_error,
    summarize,
)
from propbot.backtest.trades import DayBlocks, IntratradeModel
from propbot.risk import MaxLossType, load_venue_profile
from support.mc import (
    EMPTY,
    LOSS,
    WIN,
    TradeSpec,
    barrier_probability,
    barrier_probability_by_linear_system,
    make_blocks,
    rules,
    scripted,
)
from support.paths import CONFIG_FIXTURES

CLOSED = IntratradeModel.CLOSED
STOP = IntratradeModel.STOP
BOTH_MODELS = pytest.mark.parametrize("model", [STOP, CLOSED])


def play(
    blocks: DayBlocks,
    sequence: Sequence[int],
    challenge: ChallengeRules | None = None,
    *,
    risk: float = 0.5,
    max_days: int | None = None,
    dd_days: int | None = None,
    risk_steps: tuple[RiskStep, ...] = (),
) -> PathOutcomes:
    """Run one path through a scripted sequence of pool days."""
    params = MonteCarloParams(
        risk_pct=risk,
        n_paths=1,
        max_days=len(sequence) if max_days is None else max_days,
        dd_days=len(sequence) if dd_days is None else dd_days,
        risk_steps=risk_steps,
    )
    return simulate(blocks, challenge or rules(), params, day_sampler=scripted(sequence))


def status(outcomes: PathOutcomes) -> PathStatus:
    return PathStatus(int(outcomes.status[0]))


def breach(outcomes: PathOutcomes) -> tuple[int, BreachReason]:
    return int(outcomes.breach_phase[0]), BreachReason(int(outcomes.breach_reason[0]))


# --- targets and phases -----------------------------------------------------------------


@BOTH_MODELS
def test_steady_wins_pass_both_phases_on_exact_days(model: IntratradeModel) -> None:
    # +2R at 0.5 % risk is +1 % per day: 10 days for the 10 % target, 5 for the 5 % target.
    blocks = make_blocks([WIN], intratrade=model)

    outcomes = play(blocks, [0] * 15)

    assert status(outcomes) is PathStatus.PASSED
    assert outcomes.phase_days.tolist() == [[10, 5]]


def test_one_day_short_of_the_second_target_is_undecided() -> None:
    outcomes = play(make_blocks([WIN]), [0] * 14)

    assert status(outcomes) is PathStatus.RUNNING
    assert outcomes.phase_days.tolist() == [[10, -1]]


def test_second_phase_starts_from_a_fresh_balance() -> None:
    # Ten wins pass phase 1. If the +10 % carried over, twenty losses would only reach 0 %.
    blocks = make_blocks([WIN, LOSS], intratrade=CLOSED)

    outcomes = play(blocks, [0] * 10 + [1] * 20)

    assert status(outcomes) is PathStatus.BREACHED
    assert breach(outcomes) == (1, BreachReason.MAX_LOSS)
    assert outcomes.phase_days.tolist() == [[10, -1]]


def test_target_counts_only_while_flat() -> None:
    # The winner closes at +1 % (the target) while the second trade is still open and then loses.
    day: list[TradeSpec] = [(0, 60, 2.0), (30, 120, -1.0)]
    blocks = make_blocks([day], intratrade=CLOSED)

    outcomes = play(blocks, [0], rules(targets=(1.0,)))

    assert status(outcomes) is PathStatus.RUNNING


def test_trading_stops_for_the_day_once_the_target_is_banked() -> None:
    # The later trade would wipe the account if it were taken.
    day: list[TradeSpec] = [(0, 60, 2.0), (120, 180, -30.0)]
    blocks = make_blocks([day], intratrade=CLOSED)

    outcomes = play(blocks, [0], rules(targets=(1.0,)))

    assert status(outcomes) is PathStatus.PASSED
    assert outcomes.phase_days.tolist() == [[1]]
    assert breach(outcomes) == (-1, BreachReason.NONE)


def test_second_phase_does_not_trade_on_the_day_the_first_one_is_passed() -> None:
    # Day 1 banks the phase 1 target on its first trade; the second trade of that day is
    # not taken for phase 2, which starts on day 2 and needs two more winning days.
    day: list[TradeSpec] = [(0, 60, 2.0), (120, 180, 2.0)]
    blocks = make_blocks([day], intratrade=CLOSED)

    outcomes = play(blocks, [0, 0], rules(targets=(1.0, 2.0)))

    assert status(outcomes) is PathStatus.PASSED
    assert outcomes.phase_days.tolist() == [[1, 1]]  # day 2: +1 % then +1 % = phase 2 target


def test_single_phase_program_passes_after_its_only_target() -> None:
    outcomes = play(make_blocks([WIN]), [0] * 10, rules(targets=(10.0,)))

    assert status(outcomes) is PathStatus.PASSED
    assert outcomes.phase_days.tolist() == [[10]]


# --- minimum trading days ---------------------------------------------------------------


def test_minimum_trading_days_delay_the_pass() -> None:
    # Targets are reached on day 10 and day 5; twelve trading days are required in each phase.
    blocks = make_blocks([WIN])
    challenge = rules(min_days=12)

    assert play(blocks, [0] * 24, challenge).phase_days.tolist() == [[12, 12]]
    waiting = play(blocks, [0] * 23, challenge)
    assert status(waiting) is PathStatus.WAITING
    assert waiting.phase_days.tolist() == [[12, -1]]


def test_days_without_trades_do_not_count_as_trading_days() -> None:
    # Target (+2 %) is reached on day 4 with two trading days; one more is required.
    blocks = make_blocks([WIN, EMPTY, WIN])

    outcomes = play(blocks, [0, 1, 1, 0, 1], rules(targets=(2.0,), min_days=3))

    assert status(outcomes) is PathStatus.PASSED
    assert outcomes.phase_days.tolist() == [[5]]


def test_minimum_already_met_does_not_delay() -> None:
    outcomes = play(make_blocks([WIN]), [0] * 10, rules(targets=(10.0,), min_days=10))

    assert outcomes.phase_days.tolist() == [[10]]


# --- breaches ---------------------------------------------------------------------------


@BOTH_MODELS
def test_steady_losses_breach_the_max_loss_on_day_twenty(model: IntratradeModel) -> None:
    # -1R at 0.5 % risk: the balance touches -10 % exactly on day 20, and touching counts.
    blocks = make_blocks([LOSS], intratrade=model)

    assert status(play(blocks, [0] * 19)) is PathStatus.RUNNING
    breached = play(blocks, [0] * 20)
    assert status(breached) is PathStatus.BREACHED
    assert breach(breached) == (0, BreachReason.MAX_LOSS)


@BOTH_MODELS
def test_ten_losses_in_one_day_breach_the_daily_limit(model: IntratradeModel) -> None:
    nine: list[TradeSpec] = [(10 * i, 10 * i + 5, -1.0) for i in range(9)]
    eleven: list[TradeSpec] = [(10 * i, 10 * i + 5, -1.0) for i in range(11)]

    survived = play(make_blocks([nine], intratrade=model), [0])
    breached = play(make_blocks([eleven], intratrade=model), [0])

    assert status(survived) is PathStatus.RUNNING  # -4.5 % on the day
    assert status(breached) is PathStatus.BREACHED  # -5.0 % at the tenth trade
    assert breach(breached) == (0, BreachReason.DAILY_LOSS)


def test_daily_floor_moves_with_the_balance_at_the_start_of_the_day() -> None:
    # Two days of -4.5 % never breach the 5 % daily limit; day three hits the -10 % floor
    # after two more losses, long before that day's own floor (-9 % - 5 % = -14 %).
    nine: list[TradeSpec] = [(10 * i, 10 * i + 5, -1.0) for i in range(9)]
    blocks = make_blocks([nine], intratrade=CLOSED)

    assert status(play(blocks, [0, 0])) is PathStatus.RUNNING
    breached = play(blocks, [0, 0, 0])
    assert breach(breached) == (0, BreachReason.MAX_LOSS)


def test_gap_through_both_floors_is_reported_as_the_floor_crossed_first() -> None:
    nine: list[TradeSpec] = [(10 * i, 10 * i + 5, -1.0) for i in range(9)]
    gap: list[TradeSpec] = [(0, 60, -30.0)]
    blocks = make_blocks([nine, gap], intratrade=CLOSED)

    # From 0 %: the daily floor (-5 %) lies above the max-loss floor (-10 %).
    assert breach(play(blocks, [1])) == (0, BreachReason.DAILY_LOSS)
    # From -9 %: the daily floor is -14 %, so the max-loss floor (-10 %) is crossed first.
    assert breach(play(blocks, [0, 0, 1])) == (0, BreachReason.MAX_LOSS)


def test_stop_model_counts_the_assumed_dip_of_a_winner() -> None:
    # At -9.5 % the next trade's stop distance (0.5 %) reaches the -10 % floor.
    sequence = [1] * 19 + [0]

    pessimistic = play(make_blocks([WIN, LOSS], intratrade=STOP), sequence)
    optimistic = play(make_blocks([WIN, LOSS], intratrade=CLOSED), sequence)

    assert breach(pessimistic) == (0, BreachReason.MAX_LOSS)
    assert status(optimistic) is PathStatus.RUNNING


def test_overlapping_trades_float_against_the_account_together() -> None:
    challenge = rules(targets=(50.0,), daily=1.0)
    overlapping: list[TradeSpec] = [(0, 120, 2.0), (60, 90, 2.0)]
    sequential: list[TradeSpec] = [(0, 50, 2.0), (60, 90, 2.0)]

    # Two open trades can both sit at their stops: 2 x 0.5 % reaches the 1 % daily limit.
    together = play(make_blocks([overlapping], intratrade=STOP), [0], challenge)
    one_by_one = play(make_blocks([sequential], intratrade=STOP), [0], challenge)
    closed_only = play(make_blocks([overlapping], intratrade=CLOSED), [0], challenge)

    assert breach(together) == (0, BreachReason.DAILY_LOSS)
    assert status(one_by_one) is PathStatus.RUNNING
    assert status(closed_only) is PathStatus.RUNNING


def test_closed_trade_no_longer_floats_against_the_account() -> None:
    # Three trades, never more than two open at once: the bound is 2 x 0.5 % = 1 %, not 1.5 %.
    challenge = rules(targets=(50.0,), daily=1.5)
    day: list[TradeSpec] = [(0, 60, 0.0), (30, 200, 2.0), (70, 110, 2.0)]

    outcomes = play(make_blocks([day], intratrade=STOP), [0], challenge)

    assert status(outcomes) is PathStatus.RUNNING


def test_measured_excursion_replaces_the_stop_assumption() -> None:
    challenge = rules(targets=(50.0,), daily=1.0)
    shallow: list[TradeSpec] = [(0, 120, 2.0, -0.2), (60, 90, 2.0, -0.2)]

    outcomes = play(make_blocks([shallow], intratrade=STOP), [0], challenge)

    assert status(outcomes) is PathStatus.RUNNING  # 2 x 0.2R x 0.5 % = 0.2 % < 1 %


def test_cost_assumption_turns_a_scratch_strategy_into_a_loser() -> None:
    scratch: list[TradeSpec] = [(0, 60, 0.0)]

    free = play(make_blocks([scratch], intratrade=CLOSED), [0] * 40)
    costly = play(make_blocks([scratch], cost_r=0.5, intratrade=CLOSED), [0] * 40)

    assert status(free) is PathStatus.RUNNING
    assert breach(costly) == (0, BreachReason.MAX_LOSS)  # 40 x 0.5R x 0.5 % = 10 %


# --- trade skipping ---------------------------------------------------------------------


def test_skipping_every_trade_leaves_all_paths_undecided() -> None:
    params = MonteCarloParams(risk_pct=0.5, n_paths=50, max_days=30, dd_days=30, skip_prob=1.0)

    outcomes = simulate(make_blocks([LOSS]), rules(), params)

    assert (outcomes.status == PathStatus.RUNNING).all()
    assert not outcomes.max_drawdown_pct.any()


def test_skipping_half_the_trades_doubles_the_time_to_target() -> None:
    # Ten wins are needed; with half the trades skipped the wait is negative-binomial, mean 20.
    params = MonteCarloParams(risk_pct=0.5, n_paths=4000, max_days=200, dd_days=1, skip_prob=0.5)

    outcomes = simulate(make_blocks([WIN]), rules(targets=(10.0,)), params)

    assert (outcomes.status == PathStatus.PASSED).all()
    assert outcomes.phase_days[:, 0].mean() == pytest.approx(20.0, abs=0.3)
    assert outcomes.phase_days[:, 0].min() >= 10


def test_skip_probability_is_the_share_of_trades_left_out() -> None:
    # Skipping a quarter keeps three quarters: ten wins then take 10 / 0.75 days on average.
    params = MonteCarloParams(risk_pct=0.5, n_paths=4000, max_days=200, dd_days=1, skip_prob=0.25)

    outcomes = simulate(make_blocks([WIN]), rules(targets=(10.0,)), params)

    assert outcomes.phase_days[:, 0].mean() == pytest.approx(10 / 0.75, abs=0.2)


def test_skip_draws_do_not_disturb_the_day_draws() -> None:
    # A vanishing skip probability switches the skip stream on without skipping anything.
    blocks = make_blocks([WIN, LOSS, LOSS])
    base = MonteCarloParams(risk_pct=0.5, n_paths=500, max_days=400, dd_days=50)
    with_stream = MonteCarloParams(
        risk_pct=0.5, n_paths=500, max_days=400, dd_days=50, skip_prob=1e-300
    )

    plain = simulate(blocks, rules(), base)
    streamed = simulate(blocks, rules(), with_stream)

    assert np.array_equal(plain.phase_days, streamed.phase_days)
    assert np.array_equal(plain.status, streamed.status)


# --- maximum drawdown -------------------------------------------------------------------


def test_max_drawdown_of_a_known_sequence() -> None:
    sequence = [0, 1, 1, 1, 0]  # closed balance: +1.0, +0.5, 0.0, -0.5, +0.5

    closed = play(make_blocks([WIN, LOSS], intratrade=CLOSED), sequence)
    stop = play(make_blocks([WIN, LOSS], intratrade=STOP), sequence)

    assert closed.max_drawdown_pct.tolist() == [1.5]  # peak +1.0 to trough -0.5
    assert stop.max_drawdown_pct.tolist() == [2.0]  # last entry may float from -0.5 to -1.0


def test_max_drawdown_window_is_limited_to_dd_days() -> None:
    outcomes = play(make_blocks([WIN, LOSS], intratrade=CLOSED), [0, 1, 1, 1, 0], dd_days=3)

    assert outcomes.max_drawdown_pct.tolist() == [1.0]


def test_max_drawdown_ignores_rule_stops() -> None:
    # The challenge ends on day 20; the drawdown track keeps going to day 50.
    outcomes = play(make_blocks([LOSS], intratrade=CLOSED), [0] * 50)

    assert status(outcomes) is PathStatus.BREACHED
    assert outcomes.max_drawdown_pct.tolist() == [25.0]


def test_challenge_stops_at_max_days_while_the_drawdown_window_runs_on() -> None:
    # The breach would come on day 20; the challenge horizon ends on day 19.
    outcomes = play(make_blocks([LOSS], intratrade=CLOSED), [0] * 30, max_days=19, dd_days=30)

    assert status(outcomes) is PathStatus.RUNNING
    assert outcomes.max_drawdown_pct.tolist() == [15.0]


# --- experimental risk steps ------------------------------------------------------------

STEPS = (RiskStep(loss_pct=3.0, multiplier=0.5), RiskStep(loss_pct=6.0, multiplier=0.25))


def test_risk_steps_slow_the_approach_to_the_max_loss() -> None:
    # 6 losses at 0.5 % reach -3 %, 12 at 0.25 % reach -6 %, 32 at 0.125 % reach -10 %.
    blocks = make_blocks([LOSS], intratrade=CLOSED)

    assert status(play(blocks, [0] * 49, risk_steps=STEPS)) is PathStatus.RUNNING
    breached = play(blocks, [0] * 50, risk_steps=STEPS)
    assert breach(breached) == (0, BreachReason.MAX_LOSS)
    assert breached.max_drawdown_pct.tolist() == [10.0]
    assert play(blocks, [0] * 50).max_drawdown_pct.tolist() == [25.0]


def test_risk_returns_to_the_base_after_recovery_and_never_above_it() -> None:
    # Six losses reach -3 %. The first win is taken at half risk (+0.5 %), later ones at full.
    blocks = make_blocks([WIN, LOSS], intratrade=CLOSED)
    challenge = rules(targets=(2.0,))
    sequence = [1] * 6 + [0] * 6

    stepped = play(blocks, sequence, challenge, risk_steps=STEPS)
    fixed = play(blocks, sequence, challenge)

    assert fixed.phase_days.tolist() == [[11]]  # -3 % + 5 x 1 % = +2 %
    assert stepped.phase_days.tolist() == [[12]]  # -3 % + 0.5 % + 5 x 1 % = +2.5 %


@pytest.mark.parametrize(
    "steps",
    [
        (RiskStep(3.0, 1.5),),  # would increase risk
        (RiskStep(3.0, 0.0),),
        (RiskStep(3.0, 0.25), RiskStep(6.0, 0.5)),  # multiplier grows deeper in the loss
        (RiskStep(6.0, 0.5), RiskStep(3.0, 0.25)),  # thresholds not increasing
        (RiskStep(0.0, 0.5),),
        (RiskStep(-1.0, 0.5),),
    ],
)
def test_risk_steps_may_only_reduce_risk(steps: tuple[RiskStep, ...]) -> None:
    with pytest.raises(SimulationInputError, match="risk kademesi"):
        MonteCarloParams(risk_pct=0.5, risk_steps=steps)


def test_risk_step_at_or_beyond_the_max_loss_is_rejected() -> None:
    params = MonteCarloParams(risk_pct=0.5, n_paths=1, risk_steps=(RiskStep(10.0, 0.5),))

    with pytest.raises(SimulationInputError, match="max_loss_pct"):
        simulate(make_blocks([WIN]), rules(), params)


# --- inputs -----------------------------------------------------------------------------


@pytest.mark.parametrize("risk", [0.0, -0.25, 0.5000001, 1.0, math.nan, math.inf])
def test_risk_outside_the_code_cap_is_rejected(risk: float) -> None:
    with pytest.raises(SimulationInputError, match="risk_pct"):
        MonteCarloParams(risk_pct=risk)


@pytest.mark.parametrize(
    "overrides",
    [
        {"n_paths": 0},
        {"max_days": 0},
        {"dd_days": 0},
        {"skip_prob": -0.1},
        {"skip_prob": 1.1},
        {"skip_prob": math.nan},
        {"seed": -1},
    ],
)
def test_invalid_parameters_are_rejected(overrides: dict[str, float]) -> None:
    with pytest.raises(SimulationInputError):
        MonteCarloParams(risk_pct=0.25, **overrides)  # type: ignore[arg-type]


def test_default_path_count_is_ten_thousand() -> None:
    assert MonteCarloParams(risk_pct=0.25).n_paths == DEFAULT_PATHS == 10_000


def test_rules_come_from_the_venue_profile() -> None:
    venue = load_venue_profile(CONFIG_FIXTURES / "valid" / "venues" / "sample_2step.yaml")

    challenge = ChallengeRules.from_venue(venue)

    assert challenge == ChallengeRules(
        targets_pct=(10.0, 5.0),
        daily_loss_limit_pct=5.0,
        max_loss_pct=10.0,
        min_trading_days=4,
        min_trading_days_assumed=False,
    )


def test_null_min_trading_days_needs_an_explicit_assumption() -> None:
    venue = load_venue_profile(
        CONFIG_FIXTURES / "valid_unverified" / "venues" / "sample_unverified.yaml"
    )
    assert venue.min_trading_days is None

    with pytest.raises(SimulationInputError, match="--assume-min-trading-days"):
        ChallengeRules.from_venue(venue)

    assumed = ChallengeRules.from_venue(venue, assume_min_trading_days=4)
    assert assumed.min_trading_days == 4
    assert assumed.min_trading_days_assumed


def test_assumption_may_not_contradict_the_profile() -> None:
    venue = load_venue_profile(CONFIG_FIXTURES / "valid" / "venues" / "sample_2step.yaml")

    assert ChallengeRules.from_venue(venue, assume_min_trading_days=4).min_trading_days == 4
    with pytest.raises(SimulationInputError, match="farklı bir varsayım"):
        ChallengeRules.from_venue(venue, assume_min_trading_days=0)


def test_trailing_max_loss_is_not_simulated() -> None:
    venue = load_venue_profile(CONFIG_FIXTURES / "valid" / "venues" / "sample_2step.yaml")
    trailing = venue.model_copy(update={"max_loss_type": MaxLossType.TRAILING_EOD})

    with pytest.raises(SimulationInputError, match="trailing_eod"):
        ChallengeRules.from_venue(trailing)


def test_single_phase_profile_yields_one_target() -> None:
    venue = load_venue_profile(CONFIG_FIXTURES / "valid" / "venues" / "sample_2step.yaml")
    single = venue.model_copy(
        update={"profit_target_pct": venue.profit_target_pct.model_copy(update={"phase2": None})}
    )

    assert ChallengeRules.from_venue(single).targets_pct == (10.0,)


@pytest.mark.parametrize(
    "kwargs",
    [
        {"targets_pct": (), "daily_loss_limit_pct": 5.0, "max_loss_pct": 10.0},
        {"targets_pct": (0.0,), "daily_loss_limit_pct": 5.0, "max_loss_pct": 10.0},
        {"targets_pct": (10.0,), "daily_loss_limit_pct": 0.0, "max_loss_pct": 10.0},
        {"targets_pct": (10.0,), "daily_loss_limit_pct": 5.0, "max_loss_pct": -1.0},
    ],
)
def test_nonsensical_rules_are_rejected(kwargs: dict[str, object]) -> None:
    with pytest.raises(SimulationInputError):
        ChallengeRules(min_trading_days=0, **kwargs)  # type: ignore[arg-type]


def test_negative_min_trading_days_are_rejected() -> None:
    with pytest.raises(SimulationInputError, match="negatif"):
        rules(min_days=-1)


def test_day_sampler_must_cover_every_path() -> None:
    params = MonteCarloParams(risk_pct=0.5, n_paths=3, max_days=1, dd_days=1)

    with pytest.raises(ValueError, match="one source-day index per path"):
        simulate(make_blocks([WIN]), rules(), params, day_sampler=scripted([0], n_paths=2))


# --- sanity check against theory (SPEC §6.5, §10) ----------------------------------------


@pytest.mark.parametrize(("lower", "upper"), [(20, 20), (20, 10), (19, 20), (19, 10), (3, 2)])
def test_closed_form_barrier_probability_matches_the_linear_system(lower: int, upper: int) -> None:
    assert barrier_probability(lower, upper) == pytest.approx(
        barrier_probability_by_linear_system(lower, upper), abs=1e-12
    )


@pytest.mark.parametrize(
    ("model", "lower"),
    [
        (CLOSED, 20),  # -10 % is 20 losses of 0.5 % away
        (STOP, 19),  # the assumed dip of the next trade ends the path one step earlier
    ],
)
def test_zero_expectancy_rr2_matches_the_barrier_probability(
    model: IntratradeModel, lower: int
) -> None:
    # E = 0 at RR 1:2: +2R with probability 1/3, -1R with 2/3, one trade per day, 0.5 % risk.
    blocks = make_blocks([WIN, LOSS, LOSS], intratrade=model)
    n_paths = 20_000
    params = MonteCarloParams(risk_pct=0.5, n_paths=n_paths, max_days=6_000, dd_days=1)

    summary = summarize(simulate(blocks, rules(), params))

    phase_1 = barrier_probability(lower, 20)  # +10 % target = 20 steps
    both = phase_1 * barrier_probability(lower, 10)  # then +5 % = 10 steps, fresh account
    tolerance = 4.0  # standard errors
    assert summary.p_undecided == 0.0
    assert abs(summary.p_pass_phase[0] - phase_1) < tolerance * standard_error(phase_1, n_paths)
    assert abs(summary.p_pass_all - both) < tolerance * standard_error(both, n_paths)
    assert summary.p_breach == pytest.approx(1.0 - summary.p_pass_all)
    # One trade of 0.5 % per day can never lose 5 % in a day.
    assert all(
        share.probability == 0.0
        for share in summary.breach_shares
        if share.reason is BreachReason.DAILY_LOSS
    )


def test_fair_coin_at_rr1_passes_half_the_time() -> None:
    # Steps of +-1 with equal probability: P(+20 before -20) = 1/2, then P(+10 before -20) = 2/3.
    blocks = make_blocks([[(0, 60, 1.0)], [(0, 60, -1.0)]], intratrade=CLOSED)
    n_paths = 10_000
    params = MonteCarloParams(risk_pct=0.5, n_paths=n_paths, max_days=8_000, dd_days=1)

    summary = summarize(simulate(blocks, rules(), params))

    assert abs(summary.p_pass_phase[0] - 0.5) < 4.0 * standard_error(0.5, n_paths)
    assert abs(summary.p_pass_all - 1.0 / 3.0) < 4.0 * standard_error(1.0 / 3.0, n_paths)


# --- determinism ------------------------------------------------------------------------


def test_same_seed_reproduces_every_path_and_other_seeds_differ() -> None:
    blocks = make_blocks([WIN, LOSS, LOSS, [(0, 60, 2.0), (30, 90, -1.0)]])

    def run(seed: int) -> PathOutcomes:
        params = MonteCarloParams(
            risk_pct=0.5, n_paths=2000, max_days=600, dd_days=100, seed=seed, skip_prob=0.1
        )
        return simulate(blocks, rules(), params)

    first, again, other = run(7), run(7), run(8)

    for name in ("status", "phase_days", "breach_phase", "breach_reason", "max_drawdown_pct"):
        assert np.array_equal(getattr(first, name), getattr(again, name)), name
    assert summarize(first) == summarize(again)
    assert not np.array_equal(first.phase_days, other.phase_days)


# --- summary ----------------------------------------------------------------------------


def test_summary_of_hand_made_outcomes() -> None:
    outcomes = PathOutcomes(
        n_phases=2,
        status=np.array(
            [PathStatus.PASSED, PathStatus.BREACHED, PathStatus.BREACHED, PathStatus.RUNNING],
            dtype=np.int8,
        ),
        phase_days=np.array([[10, 5], [20, -1], [-1, -1], [-1, -1]], dtype=np.int32),
        breach_phase=np.array([-1, 1, 0, -1], dtype=np.intp),
        breach_reason=np.array(
            [BreachReason.NONE, BreachReason.MAX_LOSS, BreachReason.DAILY_LOSS, BreachReason.NONE],
            dtype=np.int8,
        ),
        max_drawdown_pct=np.array([1.0, 2.0, 3.0, 4.0]),
    )

    summary = summarize(outcomes)

    assert summary.n_paths == 4
    assert summary.p_pass_phase == (0.5, 0.25)
    assert summary.p_pass_all == 0.25
    assert summary.p_breach == 0.5
    assert summary.p_undecided == 0.25
    assert {(s.phase, s.reason): s.probability for s in summary.breach_shares} == {
        (1, BreachReason.DAILY_LOSS): 0.25,
        (1, BreachReason.MAX_LOSS): 0.0,
        (2, BreachReason.DAILY_LOSS): 0.0,
        (2, BreachReason.MAX_LOSS): 0.25,
    }
    phase_1, both = summary.time_to_pass
    assert phase_1 is not None
    assert both is not None
    assert (phase_1.n, phase_1.mean, phase_1.median) == (2, 15.0, 15.0)
    assert (both.n, both.median, both.p95) == (1, 15.0, 15.0)  # 10 + 5 market days
    assert summary.drawdown.median == 2.5
    assert summary.drawdown.q95 == pytest.approx(3.85)
    assert summary.drawdown.es95 == 4.0  # mean of the paths at or beyond the 95 % quantile
    assert summary.drawdown.es99 == 4.0


def test_summary_without_any_pass_has_no_duration() -> None:
    summary = summarize(play(make_blocks([LOSS]), [0] * 20))

    assert summary.p_pass_phase == (0.0, 0.0)
    assert summary.time_to_pass == (None, None)
    assert summary.p_breach == 1.0


def test_standard_error_of_a_proportion() -> None:
    assert standard_error(0.5, 10_000) == pytest.approx(0.005)
    assert standard_error(0.0, 10_000) == 0.0
