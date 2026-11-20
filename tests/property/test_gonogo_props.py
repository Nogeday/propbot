"""Properties of the Go/No-Go gates: GO needs every gate, and a worse input never helps."""

import dataclasses
import datetime as dt
import hashlib
import math
from collections.abc import Callable

from hypothesis import example, given, settings
from hypothesis import strategies as st

from propbot.backtest.artifacts import (
    BacktestArtifact,
    BreachShareModel,
    CostInfo,
    DataInfo,
    DataValidationArtifact,
    DrawdownModel,
    HistogramModel,
    InstrumentInfo,
    LoadedArtifact,
    MonteCarloArtifact,
    MonteCarloInputs,
    ParityArtifact,
    RulesModel,
    Scalar,
    StatsModel,
    TablesModel,
    TradesProvenanceModel,
    artifact_file_name,
    build_artifact,
)
from propbot.backtest.config import GoNoGoCriteria
from propbot.backtest.gonogo import (
    GateResult,
    GoNoGoInputs,
    assemble,
    evaluate,
    gate_data,
    gate_demo_forward,
    gate_edge,
    gate_monte_carlo,
    gate_parity,
    gate_robustness,
    gate_rules,
)
from propbot.backtest.model import CostModel
from propbot.backtest.montecarlo import DEFAULT_SEED
from propbot.core.instrument import Instrument, InstrumentKind
from propbot.risk import load_venue_profile
from support.paths import CONFIG_DIR

TODAY = dt.date(2026, 11, 18)
REPORT_TIME = dt.datetime(2026, 11, 18, 12, 0, tzinfo=dt.UTC)
SID = "ny_orb_fvg"
VENUE_NAME = "ftmo_2step_100k"
SHA = "ab" * 32
FILE = "us500_1m.csv"
FIRST_BAR = dt.datetime(2023, 1, 2, 14, 30, tzinfo=dt.UTC)
BASE_VENUE = load_venue_profile(CONFIG_DIR / "venues" / "ftmo_2step_100k.yaml")
CRITERIA = GoNoGoCriteria(
    min_data_years=2.0,
    required_tf_seconds=60,
    min_oos_t_stat=2.0,
    min_oos_to_is_ratio=0.5,
    min_p_pass_all_phases=0.60,
    max_p_breach=0.20,
    min_parity_rate=0.98,
)
EXPECTED_COSTS = {"US500.cash": CostModel(commission_per_unit_side=0.5, slippage_ticks=1.0)}
# config/backtest.yaml session.min_gap_minutes (30) in seconds (gate attack L).
MIN_GAP_SECONDS = 1800
# Since the user decision of 2026-11-19 (open finding 82) a decision run carries the
# planned risk (config/risk.yaml, 0.25) in its risk input and Go/No-Go is told the input's
# name; a parity run keeps the script's own risk (0.5) and has its own expected set.
RUN_PARAMS: dict[str, Scalar] = {"risk_pct": 0.25}
PARITY_PARAMS: dict[str, Scalar] = {"risk_pct": 0.5}
# Stand-in trade-row digest shared by the decision run and its list (gate attack P).
ROWS_DIGEST = hashlib.sha256(SID.encode("utf-8")).hexdigest()
EXPECTED_INSTRUMENTS = {
    "US500.cash": Instrument("US500.cash", InstrumentKind.CFD, 0.01, 1.0, 0.01, 0.01)
}
# Defects of the Monte Carlo trade list's provenance ("ok" = the decision run's list).
PROVENANCE_DEFECTS = (
    "none",
    "synthetic",
    "cost_free",
    "tv_ohlc",
    "factor",
    "other_sha",
    "other_strategies",
)


@dataclasses.dataclass(frozen=True)
class Scenario:
    judged: bool
    bt_present: bool
    bt_synthetic: bool
    bt_tv_ohlc: bool
    bt_cost_free: bool
    bt_cost_factor: float | None
    bt_age: int
    oos_n: int
    oos_mean: float
    oos_stdev: float
    is_mean: float
    span_years: float
    tf: int
    val_present: bool
    val_clean: bool
    val_same_sha: bool
    val_synthetic: bool
    val_age: int
    mc_present: bool
    mc_venue_ok: bool
    mc_synthetic: bool
    mc_p_pass: float
    mc_p_breach: float
    mc_paths: int
    mc_trades: int
    mc_risk_ok: bool
    mc_age: int
    mc_provenance: str
    par_present: bool
    par_rate: float
    par_synthetic: bool
    par_age: int
    rules_age: int | None
    automation: bool
    # User decision of 2026-11-19 (open finding 82). The backtest was sized at the
    # script's own risk instead of the planned one (a "--pine-risk" comparison run), and
    # the parity run recorded the planned risk instead of the script's own.
    bt_pine_risk: bool = False
    par_planned_risk: bool = False


# Mostly fresh, so that passing gates are common enough for the monotonicity property.
ages = st.one_of(st.integers(min_value=0, max_value=20), st.integers(min_value=0, max_value=40))
probabilities = st.floats(min_value=0.0, max_value=1.0, allow_nan=False)
mostly_true = st.sampled_from([True, True, True, False])
mostly_false = st.sampled_from([False, False, False, True])

scenarios = st.builds(
    Scenario,
    judged=mostly_true,
    bt_present=mostly_true,
    bt_synthetic=mostly_false,
    bt_tv_ohlc=mostly_false,
    bt_cost_free=mostly_false,
    bt_cost_factor=st.sampled_from([1.0, 1.0, 1.0, 1.0, None, 0.99, 1.5]),
    bt_age=ages,
    oos_n=st.integers(min_value=0, max_value=600),
    oos_mean=st.floats(min_value=-0.5, max_value=0.8, allow_nan=False),
    oos_stdev=st.floats(min_value=0.5, max_value=2.0, allow_nan=False),
    # Away from zero: a tiny IS expectancy makes an infinite ratio no artifact can hold.
    is_mean=st.floats(min_value=-0.5, max_value=1.0, allow_nan=False).filter(
        lambda v: abs(v) >= 0.01
    ),
    span_years=st.floats(min_value=1.0, max_value=4.0, allow_nan=False),
    tf=st.sampled_from([60, 60, 60, 300]),
    val_present=mostly_true,
    val_clean=mostly_true,
    val_same_sha=mostly_true,
    val_synthetic=mostly_false,
    val_age=ages,
    mc_present=mostly_true,
    mc_venue_ok=mostly_true,
    mc_synthetic=mostly_false,
    mc_p_pass=st.one_of(probabilities, st.floats(min_value=0.5, max_value=1.0)),
    mc_p_breach=st.one_of(probabilities, st.floats(min_value=0.0, max_value=0.3)),
    mc_paths=st.sampled_from([1_000, 5_000, 10_000, 10_000]),
    mc_trades=st.sampled_from([20, 100, 600, 600]),
    mc_risk_ok=mostly_true,
    mc_age=ages,
    mc_provenance=st.sampled_from(("ok",) * 6 + PROVENANCE_DEFECTS),
    par_present=mostly_true,
    par_rate=st.floats(min_value=0.9, max_value=1.0, allow_nan=False),
    par_synthetic=mostly_false,
    par_age=ages,
    rules_age=st.one_of(ages, ages, st.none()),
    automation=mostly_true,
    bt_pine_risk=mostly_false,
    par_planned_risk=mostly_false,
)


GOOD = Scenario(
    judged=True,
    bt_present=True,
    bt_synthetic=False,
    bt_tv_ohlc=False,
    bt_cost_free=False,
    bt_cost_factor=1.0,
    bt_age=1,
    oos_n=400,
    oos_mean=0.3,
    oos_stdev=1.4,
    is_mean=0.4,
    span_years=3.0,
    tf=60,
    val_present=True,
    val_clean=True,
    val_same_sha=True,
    val_synthetic=False,
    val_age=1,
    mc_present=True,
    mc_venue_ok=True,
    mc_synthetic=False,
    mc_p_pass=0.7,
    mc_p_breach=0.1,
    mc_paths=10_000,
    mc_trades=600,
    mc_risk_ok=True,
    mc_age=1,
    mc_provenance="ok",
    par_present=True,
    par_rate=0.99,
    par_synthetic=False,
    par_age=1,
    rules_age=3,
    automation=True,
)


def made(age: int) -> dt.datetime:
    return REPORT_TIME - dt.timedelta(days=age, hours=1)


def stats(n: int, mean: float, stdev: float) -> StatsModel:
    mean_r = None if n == 0 else mean
    stdev_r = stdev if n >= 2 else None
    t = None if mean_r is None or stdev_r is None else mean_r / (stdev_r / math.sqrt(n))
    wins = n // 2
    return StatsModel(
        n=n,
        wins=wins,
        losses=n - wins,
        breakeven=0,
        win_rate=None if n == 0 else wins / n,
        mean_r=mean_r,
        median_r=mean_r,
        stdev_r=stdev_r,
        t_stat=t,
        profit_factor=None,
        total_r=0.0 if mean_r is None else mean_r * n,
        total_pnl=0.0,
        avg_win_r=None,
        avg_loss_r=None,
        max_drawdown_r=0.0,
        max_drawdown_pct=0.0,
        longest_losing_streak=0,
        incomplete=0,
    )


def info(*, sha: str, synthetic: bool, tf: int = 60, years: float = 3.0) -> DataInfo:
    last = FIRST_BAR + dt.timedelta(days=years * 365.25)
    return DataInfo(
        file_name=FILE,
        sha256=sha,
        format="mt5",
        symbol="US500.cash",
        tf_seconds=tf,
        bars=500_000,
        first_utc=FIRST_BAR.strftime("%Y-%m-%dT%H:%M:%SZ"),
        last_utc=last.strftime("%Y-%m-%dT%H:%M:%SZ"),
        bar_tz="UTC",
        synthetic=synthetic,
    )


def build(s: Scenario) -> list[LoadedArtifact]:
    items = []
    if s.bt_present:
        oos = stats(s.oos_n, s.oos_mean, s.oos_stdev)
        ratio = None if oos.mean_r is None or s.is_mean <= 0.0 else oos.mean_r / s.is_mean
        free = s.bt_cost_free
        items.append(
            build_artifact(
                BacktestArtifact,
                label="orb",
                created=made(s.bt_age),
                synthetic=s.bt_synthetic,
                data=info(sha=SHA, synthetic=s.bt_synthetic, tf=s.tf, years=s.span_years),
                cost_free=free,
                strategy_id=SID,
                params=dict(PARITY_PARAMS if s.bt_pine_risk else RUN_PARAMS),
                # The configured size grid (gate attack M, 2026-11-19).
                instrument=InstrumentInfo(
                    symbol="US500.cash",
                    kind="cfd",
                    tick_size=0.01,
                    point_value=1.0,
                    qty_step=0.01,
                    min_qty=0.01,
                ),
                costs=CostInfo(
                    commission_per_unit_side=0.0 if free else 0.5,
                    slippage_ticks=0.0 if free else 1.0,
                    spread_points=0.0,
                    spread_windows=(),
                    cost_free=free,
                    spread_tz="America/New_York",  # gate attack L (2026-11-19)
                ),
                fill_model="tv_ohlc" if s.bt_tv_ohlc else "pessimistic",
                cost_factor=s.bt_cost_factor,
                initial_capital=100_000.0,
                oos_start_utc="2025-01-01T00:00:00Z",
                # Every complete trade is one row of the Monte Carlo list (G4 compares counts).
                stats_all=stats(s.mc_trades, 0.1, 1.0),
                mae_total_r=-1.0 * s.mc_trades,
                stats_is=stats(300, s.is_mean, 1.2),
                stats_oos=oos,
                g3_ratio=ratio,
                g3_note=None if ratio is not None else "oran yok",
                tables=TablesModel(weekday=(), hour=(), month=()),
                equity_curve=(),
                incomplete_entries=0,
                dropped_entries=0,
                # Recorded since gate attacks L and P (2026-11-19).
                min_gap_seconds=MIN_GAP_SECONDS,
                trades_digest=ROWS_DIGEST,
            )
        )
    if s.val_present:
        items.append(
            build_artifact(
                DataValidationArtifact,
                label="us500",
                created=made(s.val_age),
                synthetic=s.val_synthetic,
                # The same file read the same way (since review D the reading must match).
                data=info(
                    sha=SHA if s.val_same_sha else "cd" * 32,
                    synthetic=s.val_synthetic,
                    tf=s.tf,
                    years=s.span_years,
                ),
                clean=s.val_clean,
                report={},
            )
        )
    if s.mc_present:
        # A real run's probabilities add up (G4 checks it): breach is capped by what the
        # passing paths leave, which keeps "more breach" never better (monotone).
        breach = min(s.mc_p_breach, 1.0 - s.mc_p_pass)
        defect = s.mc_provenance
        provenance = (
            None
            if defect == "none"
            else TradesProvenanceModel(
                synthetic=defect == "synthetic",
                cost_free=defect == "cost_free",
                fill_model="tv_ohlc" if defect == "tv_ohlc" else "pessimistic",
                cost_factor=1.5 if defect == "factor" else 1.0,
                data_sha256="cd" * 32 if defect == "other_sha" else SHA,
                data_file=FILE,
                strategy_ids=(SID, "zz_other") if defect == "other_strategies" else (SID,),
                # The list is written with its backtest (same time, see made()).
                created_utc=made(s.bt_age).strftime("%Y-%m-%dT%H:%M:%SZ"),
            )
        )
        items.append(
            build_artifact(
                MonteCarloArtifact,
                label="ftmo",
                # A Monte Carlo run is never older than the backtest its list came from:
                # an older backtest therefore never makes the run fresher (monotone).
                created=made(min(s.mc_age, s.bt_age)),
                synthetic=s.mc_synthetic or defect == "synthetic",
                data=None,
                inputs=MonteCarloInputs(
                    trades_file="oos_trades.csv",
                    n_trades=s.mc_trades,
                    risk_pct=0.25 if s.mc_risk_ok else 0.5,
                    cost_r=0.05,
                    n_paths=s.mc_paths,
                    max_days=1000,
                    dd_days=250,
                    # The reproducible run's seed (gate attack N, 2026-11-19: another seed is
                    # a what-if and never passes G4).
                    seed=DEFAULT_SEED,
                    skip_prob=0.0,
                    experimental_risk_steps=False,
                    venue=VENUE_NAME if s.mc_venue_ok else "other_venue",
                    rules=RulesModel(
                        targets_pct=(10.0, 5.0),
                        daily_loss_limit_pct=5.0,
                        max_loss_pct=10.0,
                        min_trading_days=4,
                        min_trading_days_assumed=False,
                        # The profile's other rules, recorded since review B (2026-11-19).
                        max_loss_type=BASE_VENUE.max_loss_type.value,  # type: ignore[arg-type]
                        daily_loss_basis=BASE_VENUE.daily_loss_basis.value,  # type: ignore[arg-type]
                        daily_reset_tz=BASE_VENUE.daily_reset.tz,
                        daily_reset_time=BASE_VENUE.daily_reset.time,
                    ),
                    # Since review C and F: the pessimistic model and the backtest's sums.
                    intratrade="stop",
                    sum_r=stats(s.mc_trades, 0.1, 1.0).total_r,
                    sum_mae_r=-1.0 * s.mc_trades,
                    # The decision run's rows (gate attack P, 2026-11-19).
                    trades_digests={SID: ROWS_DIGEST},
                ),
                p_pass_phase=(max(s.mc_p_pass, 0.0), s.mc_p_pass),
                p_breach=breach,
                breach_shares=(BreachShareModel(phase=1, reason="max_loss", probability=breach),),
                p_undecided=max(0.0, 1.0 - s.mc_p_pass - breach),
                time_to_pass=(None, None),
                drawdown=DrawdownModel(median=1.0, q95=2.0, q99=3.0, es95=2.0, es99=3.0),
                drawdown_histogram=HistogramModel(edges=(0.0, 1.0), counts=(s.mc_paths,)),
                provenance=provenance,
            )
        )
    if s.par_present:
        items.append(
            build_artifact(
                ParityArtifact,
                label="orb",
                created=made(s.par_age),
                synthetic=s.par_synthetic,
                # Since review E: the compared bars, parameters and instrument are recorded.
                data=info(sha=SHA, synthetic=s.par_synthetic),
                params=dict(RUN_PARAMS if s.par_planned_risk else PARITY_PARAMS),
                # The configured size grid (gate attack M, 2026-11-19).
                instrument=InstrumentInfo(
                    symbol="US500.cash",
                    kind="cfd",
                    tick_size=0.01,
                    point_value=1.0,
                    qty_step=0.01,
                    min_qty=0.01,
                ),
                strategy_id=SID,
                # The Python side's session gap and capital (gate attacks K, L).
                min_gap_seconds=MIN_GAP_SECONDS,
                initial_capital=100_000.0,
                # Counts that give the rate (the model checks rate = matched / total).
                matched=round(s.par_rate * 10_000),
                python_only=10_000 - round(s.par_rate * 10_000),
                tradingview_only=0,
                match_rate=round(s.par_rate * 10_000) / 10_000,
                threshold=0.9,
                passed=True,
                mismatches=(),
                sources=(),
            )
        )
    return [LoadedArtifact(artifact_file_name(item), item) for item in items]


def inputs(s: Scenario) -> GoNoGoInputs:
    venue = BASE_VENUE.model_copy(
        update={
            "rules_verified_at": None
            if s.rules_age is None
            else TODAY - dt.timedelta(days=s.rules_age),
            "automation_allowed": s.automation,
        }
    )
    return GoNoGoInputs(
        artifacts=build(s),
        criteria=CRITERIA,
        venue_name=VENUE_NAME,
        venue=venue,
        strategy_ids=(SID,) if s.judged else (),
        planned_risk_pct=0.25,
        today=TODAY,
        created=REPORT_TIME,
        compliance_files=(),
        expected_costs=EXPECTED_COSTS,
        expected_instruments=EXPECTED_INSTRUMENTS,
        # The scenario's backtest records only the planned risk; config says the same.
        expected_params={SID: dict(RUN_PARAMS)},
        expected_parity_params={SID: dict(PARITY_PARAMS)},
        risk_params={SID: "risk_pct"},
        # Explicit since the default became None (unknown -> not evaluable).
        artifacts_truncated=False,
        expected_oos_start_utc="2025-01-01T00:00:00Z",
        # Explicit since gate attack L (2026-11-19): unknown -> no decision run.
        expected_min_gap_seconds=MIN_GAP_SECONDS,
    )


GATES: tuple[Callable[[GoNoGoInputs], GateResult], ...] = (
    gate_data,
    gate_edge,
    gate_robustness,
    gate_monte_carlo,
    gate_demo_forward,
    gate_parity,
    gate_rules,
)


def statuses(s: Scenario) -> dict[str, str]:
    case = inputs(s)
    return {result.gate.id: result.gate.status for result in (gate(case) for gate in GATES)}


def g5_forced_pass(case: GoNoGoInputs) -> GateResult:
    result = gate_demo_forward(case)
    return GateResult(gate=result.gate.model_copy(update={"status": "PASS"}), used=())


def older(age: int) -> int:
    return age + 10


# Each transform makes exactly one input worse (or removes it).
WORSE: tuple[Callable[[Scenario], Scenario], ...] = (
    lambda s: dataclasses.replace(s, judged=False),
    lambda s: dataclasses.replace(s, bt_present=False),
    lambda s: dataclasses.replace(s, bt_synthetic=True),
    lambda s: dataclasses.replace(s, bt_tv_ohlc=True),
    lambda s: dataclasses.replace(s, bt_cost_free=True),
    lambda s: dataclasses.replace(s, bt_cost_factor=None),
    lambda s: dataclasses.replace(s, bt_cost_factor=1.5),
    lambda s: dataclasses.replace(s, bt_age=older(s.bt_age)),
    lambda s: dataclasses.replace(s, oos_mean=s.oos_mean - 0.05),
    lambda s: dataclasses.replace(s, oos_stdev=s.oos_stdev * 1.5),
    lambda s: dataclasses.replace(s, oos_n=s.oos_n // 2),
    lambda s: dataclasses.replace(s, is_mean=s.is_mean + 0.2 if s.is_mean > 0 else s.is_mean),
    lambda s: dataclasses.replace(s, span_years=s.span_years * 0.7),
    lambda s: dataclasses.replace(s, tf=300),
    lambda s: dataclasses.replace(s, val_present=False),
    lambda s: dataclasses.replace(s, val_clean=False),
    lambda s: dataclasses.replace(s, val_same_sha=False),
    lambda s: dataclasses.replace(s, val_synthetic=True),
    lambda s: dataclasses.replace(s, val_age=older(s.val_age)),
    lambda s: dataclasses.replace(s, mc_present=False),
    lambda s: dataclasses.replace(s, mc_venue_ok=False),
    lambda s: dataclasses.replace(s, mc_synthetic=True),
    lambda s: dataclasses.replace(s, mc_p_pass=max(0.0, s.mc_p_pass - 0.05)),
    lambda s: dataclasses.replace(s, mc_p_breach=min(1.0, s.mc_p_breach + 0.05)),
    lambda s: dataclasses.replace(s, mc_paths=1_000),
    lambda s: dataclasses.replace(s, mc_trades=20),
    lambda s: dataclasses.replace(s, mc_risk_ok=False),
    lambda s: dataclasses.replace(s, mc_age=older(s.mc_age)),
    *(
        (lambda s, d=defect: dataclasses.replace(s, mc_provenance=d))
        for defect in PROVENANCE_DEFECTS
    ),
    lambda s: dataclasses.replace(s, par_present=False),
    lambda s: dataclasses.replace(s, par_rate=max(0.9, s.par_rate - 0.01)),
    lambda s: dataclasses.replace(s, par_synthetic=True),
    lambda s: dataclasses.replace(s, par_age=older(s.par_age)),
    lambda s: dataclasses.replace(s, rules_age=None if s.rules_age is None else older(s.rules_age)),
    lambda s: dataclasses.replace(s, rules_age=None),
    lambda s: dataclasses.replace(s, automation=False),
    lambda s: dataclasses.replace(s, bt_pine_risk=True),
    lambda s: dataclasses.replace(s, par_planned_risk=True),
)


@settings(max_examples=150, deadline=None)
@given(scenario=scenarios)
@example(scenario=GOOD)
def test_evaluate_never_says_go_and_go_needs_every_gate(scenario: Scenario) -> None:
    case = inputs(scenario)
    report = evaluate(case)
    assert report.overall == "NO_GO"
    forced = assemble(
        [*(gate(case) for gate in GATES if gate is not gate_demo_forward), g5_forced_pass(case)],
        label="gonogo",
        created=REPORT_TIME,
    )
    if forced.overall == "GO":
        assert all(gate.status == "PASS" for gate in forced.gates)
        assert sorted(gate.id for gate in forced.gates) == [
            "G1",
            "G2",
            "G3",
            "G4",
            "G5",
            "G6",
            "G7",
        ]
        assert not forced.synthetic
    else:
        assert forced.synthetic or any(gate.status != "PASS" for gate in forced.gates)


@settings(max_examples=150, deadline=None)
@given(scenario=scenarios)
@example(scenario=GOOD)
def test_synthetic_input_used_anywhere_marks_the_report(scenario: Scenario) -> None:
    case = inputs(scenario)
    results = [gate(case) for gate in GATES]
    report = assemble(results, label="gonogo", created=REPORT_TIME)
    used_synthetic = any(item.artifact.synthetic for r in results for item in r.used)
    assert report.synthetic == used_synthetic
    if used_synthetic:
        assert report.overall == "NO_GO"
        assert any("SENTETİK VERİ" in reason for reason in report.reasons)


@settings(max_examples=200, deadline=None)
@given(scenario=scenarios, which=st.integers(min_value=0, max_value=len(WORSE) - 1))
def test_a_worse_input_never_turns_a_gate_into_pass(scenario: Scenario, which: int) -> None:
    before = statuses(scenario)
    after = statuses(WORSE[which](scenario))
    for gate_id, status in after.items():
        if status == "PASS":
            assert before[gate_id] == "PASS", (gate_id, which)


@settings(max_examples=60, deadline=None)
@given(scenario=scenarios)
@example(scenario=GOOD)
def test_every_gate_passing_is_reachable_only_on_good_inputs(scenario: Scenario) -> None:
    passing = {gate_id for gate_id, status in statuses(scenario).items() if status == "PASS"}
    if "G1" in passing:
        assert scenario.bt_present
        assert scenario.val_present
        assert scenario.val_clean
        assert not scenario.bt_synthetic
        assert not scenario.val_synthetic
        assert scenario.span_years >= CRITERIA.min_data_years
    if passing & {"G1", "G2", "G3"}:
        assert scenario.bt_cost_factor == 1.0
        # Only a run sized at the planned risk decides (2026-11-19, finding 82).
        assert not scenario.bt_pine_risk
    if "G4" in passing:
        assert scenario.mc_provenance == "ok"
        assert scenario.bt_present
        assert scenario.bt_cost_factor == 1.0
        # The risk chain: decision run and simulation both at the planned risk.
        assert not scenario.bt_pine_risk
        assert scenario.mc_risk_ok
        assert scenario.mc_p_pass >= CRITERIA.min_p_pass_all_phases
        # The artifact carries the capped breach (build(): a real run's probabilities add
        # up), and that is what G4 judged. Asserting on the raw draw failed for a draw such
        # as pass 0.9 / breach 0.25, whose artifact holds 0.1 and rightly passes.
        assert min(scenario.mc_p_breach, 1.0 - scenario.mc_p_pass) <= CRITERIA.max_p_breach
        assert not scenario.mc_synthetic
    if "G6" in passing:
        # A parity report is judged at the script's own risk, never the planned one.
        assert not scenario.par_planned_risk
    assert "G5" not in passing


def test_good_scenario_passes_every_gate_but_demo_forward() -> None:
    assert statuses(GOOD) == {
        "G1": "PASS",
        "G2": "PASS",
        "G3": "PASS",
        "G4": "PASS",
        "G5": "NOT_EVALUABLE",
        "G6": "PASS",
        "G7": "PASS",
    }


def test_every_single_worsening_of_the_good_scenario_keeps_the_monotonicity() -> None:
    before = statuses(GOOD)
    for index, worse in enumerate(WORSE):
        after = statuses(worse(GOOD))
        for gate_id, status in after.items():
            if status == "PASS":
                assert before[gate_id] == "PASS", (gate_id, index)
