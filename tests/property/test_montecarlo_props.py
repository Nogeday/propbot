"""Property tests for the Monte Carlo simulator on random small trade lists."""

import numpy as np
from hypothesis import given, settings
from hypothesis import strategies as st

from propbot.backtest.montecarlo import (
    ChallengeRules,
    MonteCarloParams,
    PathOutcomes,
    PathStatus,
    simulate,
    summarize,
)
from propbot.backtest.trades import IntratradeModel
from support.mc import TradeSpec, make_blocks, rules

ANY_R = st.sampled_from([-1.5, -1.0, -0.5, -0.25, 0.0, 0.5, 1.0, 2.0, 3.0])
WINNING_R = st.sampled_from([0.25, 0.5, 1.0, 2.0, 3.0])
LOSING_R = st.sampled_from([-1.5, -1.0, -0.5, -0.25])
MAX_DAYS = 40


@st.composite
def trade_specs(draw: st.DrawFn, r_values: st.SearchStrategy[float]) -> TradeSpec:
    start = draw(st.integers(0, 500))
    length = draw(st.integers(0, 120))  # 0 = filled and closed on the same bar
    return (float(start), float(start + length), draw(r_values))


def pools(r_values: st.SearchStrategy[float] = ANY_R) -> st.SearchStrategy[list[list[TradeSpec]]]:
    """Pools of 2-6 days, each with up to three (possibly overlapping) trades or none."""
    busy = st.lists(trade_specs(r_values), min_size=1, max_size=3)
    inner = st.lists(st.one_of(busy, st.just([])), max_size=4)
    return st.tuples(busy, inner, busy).map(lambda parts: [parts[0], *parts[1], parts[2]])


CHALLENGES = st.builds(
    lambda min_days: rules(targets=(2.0, 1.0), daily=1.5, total=3.0, min_days=min_days),
    st.integers(0, 3),
)
SEEDS = st.integers(0, 2**32 - 1)
RISKS = st.sampled_from([0.25, 0.5])
SKIPS = st.sampled_from([0.0, 0.3])


def run(
    pool: list[list[TradeSpec]],
    challenge: ChallengeRules,
    *,
    seed: int,
    risk: float,
    skip: float = 0.0,
    model: IntratradeModel = IntratradeModel.STOP,
) -> PathOutcomes:
    params = MonteCarloParams(
        risk_pct=risk, n_paths=64, max_days=MAX_DAYS, dd_days=20, seed=seed, skip_prob=skip
    )
    return simulate(make_blocks(pool, intratrade=model), challenge, params)


@settings(max_examples=40, deadline=None)
@given(pool=pools(), challenge=CHALLENGES, seed=SEEDS, risk=RISKS, skip=SKIPS)
def test_outcomes_are_internally_consistent(
    pool: list[list[TradeSpec]], challenge: ChallengeRules, seed: int, risk: float, skip: float
) -> None:
    outcomes = run(pool, challenge, seed=seed, risk=risk, skip=skip)
    summary = summarize(outcomes)

    passed = outcomes.status == PathStatus.PASSED
    breached = outcomes.status == PathStatus.BREACHED
    undecided = (outcomes.status == PathStatus.RUNNING) | (outcomes.status == PathStatus.WAITING)
    phase_passed = outcomes.phase_days >= 0

    # Every path has exactly one outcome and the probabilities add up.
    assert int(passed.sum() + breached.sum() + undecided.sum()) == outcomes.n_paths
    assert abs(summary.p_pass_all + summary.p_breach + summary.p_undecided - 1.0) < 1e-12
    assert abs(sum(s.probability for s in summary.breach_shares) - summary.p_breach) < 1e-12
    assert summary.p_pass_phase[0] >= summary.p_pass_phase[1]
    # Phase 2 can only be passed after phase 1; "passed" means the last phase was passed.
    assert not (phase_passed[:, 1] & ~phase_passed[:, 0]).any()
    assert np.array_equal(passed, phase_passed[:, 1])
    # A breach is recorded with its phase, and only for breached paths.
    assert np.array_equal(breached, outcomes.breach_phase >= 0)
    assert np.array_equal(breached, outcomes.breach_reason > 0)
    assert not (breached & (outcomes.breach_phase == 1) & ~phase_passed[:, 0]).any()
    # Durations are positive and fit in the horizon.
    assert (outcomes.phase_days[phase_passed] >= 1).all()
    assert (np.where(phase_passed, outcomes.phase_days, 0).sum(axis=1) <= MAX_DAYS).all()
    assert (outcomes.max_drawdown_pct >= 0.0).all()


@settings(max_examples=40, deadline=None)
@given(pool=pools(), challenge=CHALLENGES, seed=SEEDS, risk=RISKS, skip=SKIPS)
def test_a_path_that_passes_pessimistically_also_passes_optimistically(
    pool: list[list[TradeSpec]], challenge: ChallengeRules, seed: int, risk: float, skip: float
) -> None:
    stop = run(pool, challenge, seed=seed, risk=risk, skip=skip, model=IntratradeModel.STOP)
    closed = run(pool, challenge, seed=seed, risk=risk, skip=skip, model=IntratradeModel.CLOSED)

    # Same draws, stricter breach check: a pass under "stop" is the same pass under "closed".
    for phase in range(2):
        passed_stop = stop.phase_days[:, phase] >= 0
        assert np.array_equal(
            stop.phase_days[passed_stop, phase], closed.phase_days[passed_stop, phase]
        )


@settings(max_examples=40, deadline=None)
@given(pool=pools(), challenge=CHALLENGES, seed=SEEDS, risk=RISKS)
def test_doubling_r_and_halving_risk_changes_nothing(
    pool: list[list[TradeSpec]], challenge: ChallengeRules, seed: int, risk: float
) -> None:
    doubled = [[(start, end, 2.0 * r) for start, end, r in day] for day in pool]

    base = run(pool, challenge, seed=seed, risk=risk, model=IntratradeModel.CLOSED)
    scaled = run(doubled, challenge, seed=seed, risk=risk / 2.0, model=IntratradeModel.CLOSED)

    assert np.array_equal(base.status, scaled.status)
    assert np.array_equal(base.phase_days, scaled.phase_days)
    assert np.array_equal(base.max_drawdown_pct, scaled.max_drawdown_pct)


@settings(max_examples=30, deadline=None)
@given(pool=pools(WINNING_R), challenge=CHALLENGES, seed=SEEDS, risk=RISKS, skip=SKIPS)
def test_only_winning_trades_never_breach_on_closed_results(
    pool: list[list[TradeSpec]], challenge: ChallengeRules, seed: int, risk: float, skip: float
) -> None:
    outcomes = run(pool, challenge, seed=seed, risk=risk, skip=skip, model=IntratradeModel.CLOSED)

    assert not (outcomes.status == PathStatus.BREACHED).any()
    assert not outcomes.max_drawdown_pct.any()


@settings(max_examples=30, deadline=None)
@given(pool=pools(LOSING_R), challenge=CHALLENGES, seed=SEEDS, risk=RISKS, skip=SKIPS)
def test_only_losing_trades_never_pass(
    pool: list[list[TradeSpec]], challenge: ChallengeRules, seed: int, risk: float, skip: float
) -> None:
    outcomes = run(pool, challenge, seed=seed, risk=risk, skip=skip)

    assert not (outcomes.phase_days >= 0).any()


@settings(max_examples=40, deadline=None)
@given(pool=pools(), challenge=CHALLENGES, seed=SEEDS, risk=RISKS)
def test_drawdown_statistics_are_ordered(
    pool: list[list[TradeSpec]], challenge: ChallengeRules, seed: int, risk: float
) -> None:
    drawdown = summarize(run(pool, challenge, seed=seed, risk=risk)).drawdown

    assert 0.0 <= drawdown.median <= drawdown.q95 <= drawdown.q99
    assert drawdown.es95 >= drawdown.q95
    assert drawdown.es99 >= drawdown.q99
    assert drawdown.es99 >= drawdown.es95


@settings(max_examples=25, deadline=None)
@given(pool=pools(), challenge=CHALLENGES, seed=SEEDS, risk=RISKS, skip=SKIPS)
def test_a_seed_determines_the_result(
    pool: list[list[TradeSpec]], challenge: ChallengeRules, seed: int, risk: float, skip: float
) -> None:
    first = run(pool, challenge, seed=seed, risk=risk, skip=skip)
    again = run(pool, challenge, seed=seed, risk=risk, skip=skip)

    assert summarize(first) == summarize(again)
    assert np.array_equal(first.max_drawdown_pct, again.max_drawdown_pct)
