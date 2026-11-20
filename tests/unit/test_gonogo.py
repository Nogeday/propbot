"""Go/No-Go gate report (SPEC §9): every way a gate can fail or be not evaluable."""

import dataclasses
import datetime as dt
import hashlib
import json
import math
import re
from collections.abc import Callable, Sequence
from pathlib import Path

import pytest

from propbot.backtest import gonogo
from propbot.backtest.artifacts import (
    COST_FREE_TAG,
    SYNTHETIC_TAG,
    AnyArtifact,
    ArtifactError,
    ArtifactProblem,
    BacktestArtifact,
    BreachShareModel,
    CostInfo,
    DataInfo,
    DataValidationArtifact,
    DrawdownModel,
    GoNoGoArtifact,
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
    load_artifact,
)
from propbot.backtest.config import GoNoGoCriteria
from propbot.backtest.gonogo import (
    EXPECTED_PARAMS_MISSING,
    GateResult,
    GoNoGoError,
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
    render_markdown,
    report_file_name,
    write_report,
)
from propbot.backtest.model import CostModel
from propbot.core.instrument import Instrument, InstrumentKind
from propbot.risk import load_venue_profile
from propbot.risk.venue import RULES_MAX_AGE_DAYS, VenueProfile
from support.paths import CONFIG_DIR

TODAY = dt.date(2026, 11, 18)
REPORT_TIME = dt.datetime(2026, 11, 18, 12, 0, 0, tzinfo=dt.UTC)
MADE = dt.datetime(2026, 11, 17, 10, 0, 0, tzinfo=dt.UTC)
SHA = "ab" * 32
OTHER_SHA = "cd" * 32
DATA_FILE = "us500_1m.csv"
VENUE_NAME = "ftmo_2step_100k"
STRATEGIES = ("ny_orb_fvg", "asian_sweep")
VENUE = load_venue_profile(CONFIG_DIR / "venues" / "ftmo_2step_100k.yaml").model_copy(
    update={"rules_verified_at": TODAY - dt.timedelta(days=5), "automation_allowed": True}
)
CRITERIA = GoNoGoCriteria(
    min_data_years=2.0,
    required_tf_seconds=60,
    min_oos_t_stat=2.0,
    min_oos_to_is_ratio=0.5,
    min_p_pass_all_phases=0.60,
    max_p_breach=0.20,
    min_parity_rate=0.98,
)
MAX_ARTIFACT_AGE_DAYS = CRITERIA.max_artifact_age_days
MIN_MC_PATHS = CRITERIA.min_mc_paths
MIN_MC_TRADES = CRITERIA.min_mc_trades
SYMBOL = "US500.cash"
# What the CLI builds from config/backtest.yaml: the costs every decision run must carry.
EXPECTED_COSTS = {SYMBOL: CostModel(commission_per_unit_side=0.5, slippage_ticks=1.0)}
EXPECTED_INSTRUMENTS = {
    "US500.cash": Instrument("US500.cash", InstrumentKind.CFD, 0.01, 1.0, 0.01, 0.01)
}

# What the CLI builds with runner.effective_params: the parameters config/strategies.yaml
# gives today. Every decision run must have recorded exactly these.
# Since the user decision of 2026-11-19 (open finding 82) a decision run carries the
# PLANNED risk (config/risk.yaml, 0.25 here) in its risk input, and Go/No-Go must be told
# the input's name; a parity run keeps the script's own risk (0.5) and is judged against
# its own expected set.
PLANNED_RISK_PCT = 0.25
PINE_RISK_PCT = 0.5
PARAMS: dict[str, Scalar] = {
    "risk_reward": 2.0,
    "or_minutes": 15,
    "use_filter": True,
    "risk_pct": PLANNED_RISK_PCT,
}
PARITY_PARAMS: dict[str, Scalar] = {**PARAMS, "risk_pct": PINE_RISK_PCT}
EXPECTED_PARAMS = {sid: dict(PARAMS) for sid in STRATEGIES}
EXPECTED_PARITY_PARAMS = {sid: dict(PARITY_PARAMS) for sid in STRATEGIES}
RISK_PARAMS = dict.fromkeys(STRATEGIES, "risk_pct")
# config/backtest.yaml samples.oos_start as the CLI passes it: a decision run has this one.
OOS_START = "2025-01-01T00:00:00Z"
# The size grid is part of the instrument a decision run must match (gate attack M,
# 2026-11-19): the fixtures record the configured qty_step and min_qty.
INSTRUMENT_INFO = InstrumentInfo(
    symbol=SYMBOL, kind="cfd", tick_size=0.01, point_value=1.0, qty_step=0.01, min_qty=0.01
)
# config/backtest.yaml session.min_gap_minutes (30) in seconds, as the CLI passes it; a
# decision run and a parity report must have used it (gate attack L, 2026-11-19).
MIN_GAP_SECONDS = 1800


def rows_digest(sid: str) -> str:
    """A stand-in for `trade_rows_digest` of a strategy's rows: the fixtures' decision run
    of `sid` and the Monte Carlo list's rows of `sid` carry the same one (gate attack P)."""
    return hashlib.sha256(sid.encode("utf-8")).hexdigest()


# --- builders ------------------------------------------------------------------------------


def stats(n: int, mean: float | None, stdev: float | None, t: float | None = None) -> StatsModel:
    """Consistent statistics; `t` defaults to mean / (stdev / sqrt(n))."""
    if t is None and mean is not None and stdev is not None and stdev > 0 and n >= 2:
        t = mean / (stdev / math.sqrt(n))
    wins = n // 2
    return StatsModel(
        n=n,
        wins=wins,
        losses=n - wins,
        breakeven=0,
        win_rate=None if n == 0 else wins / n,
        mean_r=mean,
        median_r=mean,
        stdev_r=stdev,
        t_stat=t,
        profit_factor=None,
        total_r=0.0 if mean is None else mean * n,
        total_pnl=0.0,
        avg_win_r=None,
        avg_loss_r=None,
        max_drawdown_r=0.0,
        max_drawdown_pct=0.0,
        longest_losing_streak=0,
        incomplete=0,
    )


def data_info(
    *,
    sha: str = SHA,
    file_name: str = DATA_FILE,
    synthetic: bool = False,
    tf: int = 60,
    first: str | None = "2024-01-02T14:30:00Z",
    last: str | None = "2026-11-13T21:00:00Z",
    bars: int = 700_000,
    bar_tz: str = "UTC",
) -> DataInfo:
    return DataInfo(
        file_name=file_name,
        sha256=sha,
        format="mt5",
        symbol="US500.cash",
        tf_seconds=tf,
        bars=bars,
        first_utc=first,
        last_utc=last,
        bar_tz=bar_tz,
        synthetic=synthetic,
    )


def loaded(artifact: AnyArtifact) -> LoadedArtifact:
    return LoadedArtifact(artifact_file_name(artifact), artifact)


def backtest(
    sid: str = "ny_orb_fvg",
    *,
    label: str | None = None,
    created: dt.datetime = MADE,
    synthetic: bool = False,
    data: DataInfo | None = None,
    no_data: bool = False,
    fill_model: str = "pessimistic",
    cost_free: bool = False,
    cost_factor: float | None = 1.0,
    oos: StatsModel | None = None,
    is_mean: float | None = 0.4,
    g3_ratio: float | str | None = "auto",
    g3_note: str | None = None,
    n_all: int = 300,
    params: dict[str, Scalar] | None = None,
    oos_start: str = OOS_START,
    mae_total: float | str | None = "auto",
    min_gap_seconds: int | None = MIN_GAP_SECONDS,
    trades_digest: str | None = "auto",
) -> LoadedArtifact:
    oos_stats = oos if oos is not None else stats(300, 0.25, 1.4)
    if g3_ratio == "auto":
        ratio: float | None = (
            None
            if oos_stats.mean_r is None or is_mean is None or is_mean <= 0
            else oos_stats.mean_r / is_mean
        )
    else:
        assert not isinstance(g3_ratio, str)
        ratio = g3_ratio
    costs = (
        CostInfo(
            commission_per_unit_side=0.0,
            slippage_ticks=0.0,
            spread_points=0.0,
            spread_windows=(),
            cost_free=True,
            spread_tz="America/New_York",
        )
        if cost_free
        else CostInfo(
            commission_per_unit_side=0.5,
            slippage_ticks=1.0,
            spread_points=0.0,
            spread_windows=(),
            cost_free=False,
            # The configured zone of the spread windows (gate attack L, 2026-11-19).
            spread_tz="America/New_York",
        )
    )
    info = None if no_data else (data or data_info(synthetic=synthetic))
    artifact = build_artifact(
        BacktestArtifact,
        label=label or sid.replace("_", "-"),
        created=created,
        synthetic=synthetic,
        data=info,
        cost_free=cost_free,
        strategy_id=sid,
        params=dict(PARAMS) if params is None else params,
        instrument=INSTRUMENT_INFO,
        costs=costs,
        fill_model=fill_model,
        cost_factor=cost_factor,
        initial_capital=100_000.0,
        oos_start_utc=oos_start,
        # Every complete trade floated to -1R: what the Monte Carlo list's mae_r adds up to.
        mae_total_r=-1.0 * n_all if mae_total == "auto" else mae_total,  # type: ignore[arg-type]
        # Two strategies x 300 complete trades = the 600 rows of the Monte Carlo list.
        stats_all=stats(n_all, 0.3, 1.4),
        stats_is=stats(300, is_mean, 1.4),
        stats_oos=oos_stats,
        g3_ratio=ratio,
        g3_note=g3_note,
        tables=TablesModel(weekday=(), hour=(), month=()),
        equity_curve=(),
        incomplete_entries=0,
        dropped_entries=0,
        # Recorded since gate attacks L and P (2026-11-19): the session gap the run used
        # and the digest of its trade-list rows (G4 ties the list to it).
        min_gap_seconds=min_gap_seconds,
        trades_digest=rows_digest(sid) if trades_digest == "auto" else trades_digest,
    )
    return loaded(artifact)


def validation(
    *,
    sha: str = SHA,
    file_name: str = DATA_FILE,
    clean: bool = True,
    created: dt.datetime = MADE,
    synthetic: bool = False,
    label: str = "us500",
    data: DataInfo | None = None,
) -> LoadedArtifact:
    return loaded(
        build_artifact(
            DataValidationArtifact,
            label=label,
            created=created,
            synthetic=synthetic,
            data=data or data_info(sha=sha, file_name=file_name, synthetic=synthetic),
            clean=clean,
            report={"duplicates": 0},
        )
    )


def monte_carlo(
    *,
    venue: str = VENUE_NAME,
    p_pass: float = 0.7,
    p_breach: float = 0.1,
    n_paths: int = 10_000,
    n_trades: int = 600,
    risk_pct: float = 0.25,
    created: dt.datetime = MADE,
    synthetic: bool = False,
    experimental: bool = False,
    trades_file: str = "oos_trades.csv",
    targets: tuple[float, ...] = (10.0, 5.0),
    min_days: int = 4,
    assumed: bool = False,
    label: str = "ftmo",
    provenance: TradesProvenanceModel | str | None = "legit",
    intratrade: str | None = "stop",
    sum_r: float | None = None,
    sum_mae_r: float | None = None,
    sums: bool = True,
    max_loss_type: str | None = "static",
    daily_loss_basis: str | None = VENUE.daily_loss_basis.value,
    reset_tz: str | None = VENUE.daily_reset.tz,
    reset_time: str | None = VENUE.daily_reset.time,
) -> LoadedArtifact:
    """`sum_r` / `sum_mae_r` default to the passing set's two decision runs (300 trades of
    0.3 R and -1 R mae each); `sums=False` records neither (an older file)."""
    phases = len(targets)
    if sums and sum_r is None:
        sum_r = math.fsum([0.3 * 300, 0.3 * 300])
    if sums and sum_mae_r is None:
        sum_mae_r = -600.0
    if provenance == "legit":
        provenance = trades_provenance(created=min(created, MADE))
    assert not isinstance(provenance, str)
    return loaded(
        build_artifact(
            MonteCarloArtifact,
            label=label,
            created=created,
            synthetic=synthetic,
            data=None,
            inputs=MonteCarloInputs(
                trades_file=trades_file,
                n_trades=n_trades,
                risk_pct=risk_pct,
                cost_r=0.05,
                n_paths=n_paths,
                max_days=1000,
                dd_days=250,
                seed=20261113,
                skip_prob=0.0,
                experimental_risk_steps=experimental,
                venue=venue,
                rules=RulesModel(
                    targets_pct=targets,
                    daily_loss_limit_pct=5.0,
                    max_loss_pct=10.0,
                    min_trading_days=min_days,
                    min_trading_days_assumed=assumed,
                    max_loss_type=max_loss_type,  # type: ignore[arg-type]
                    daily_loss_basis=daily_loss_basis,  # type: ignore[arg-type]
                    daily_reset_tz=reset_tz,
                    daily_reset_time=reset_time,
                ),
                intratrade=intratrade,  # type: ignore[arg-type]
                sum_r=sum_r if sums else None,
                sum_mae_r=sum_mae_r if sums else None,
                # The rows of the two decision runs (gate attack P, 2026-11-19).
                trades_digests={sid: rows_digest(sid) for sid in STRATEGIES} if sums else None,
            ),
            p_pass_phase=(min(1.0, p_pass + 0.1),) * (phases - 1) + (p_pass,),
            p_breach=p_breach,
            # Consistent like a real run: shares add up to P(breach), and passed +
            # breached + undecided = 1 (G4 checks both).
            breach_shares=(BreachShareModel(phase=1, reason="max_loss", probability=p_breach),),
            p_undecided=max(0.0, 1.0 - p_pass - p_breach),
            time_to_pass=(None,) * phases,
            drawdown=DrawdownModel(median=1.0, q95=2.0, q99=3.0, es95=2.5, es99=3.5),
            drawdown_histogram=HistogramModel(edges=(0.0, 1.0), counts=(n_paths,)),
            provenance=provenance,
        )
    )


def trades_provenance(
    *,
    created: dt.datetime = MADE,
    synthetic: bool = False,
    cost_free: bool = False,
    fill_model: str = "pessimistic",
    cost_factor: float = 1.0,
    sha: str = SHA,
    strategies: Sequence[str] = STRATEGIES,
) -> TradesProvenanceModel:
    """The trade list of the passing set's decision runs (same data, strategies, time)."""
    return TradesProvenanceModel(
        synthetic=synthetic,
        cost_free=cost_free,
        fill_model=fill_model,  # type: ignore[arg-type]
        cost_factor=cost_factor,
        data_sha256=sha,
        data_file=DATA_FILE,
        strategy_ids=tuple(sorted(set(strategies))),
        created_utc=created.strftime("%Y-%m-%dT%H:%M:%SZ"),
    )


def parity_counts(rate: float | None) -> tuple[int, int]:
    """(matched, python_only) out of 10,000 so that the stored rate follows from them."""
    if rate is None:
        return 0, 0
    matched = round(rate * 10_000)
    return matched, 10_000 - matched


def parity(
    sid: str = "ny_orb_fvg",
    *,
    rate: float | None = 0.99,
    passed: bool | None = None,
    created: dt.datetime = MADE,
    synthetic: bool = False,
    label: str | None = None,
    params: dict[str, Scalar] | str | None = "configured",
    instrument: InstrumentInfo | str | None = "configured",
    data: DataInfo | str | None = "decision",
    counts: tuple[int, int] | None = None,
) -> LoadedArtifact:
    """Defaults: today's configured parameters and instrument on the decision run's data;
    `counts` = (matched, python_only) overrides the 10,000-trade comparison."""
    did_pass = passed if passed is not None else (rate is not None and rate >= 0.98)
    matched, python_only = parity_counts(rate) if counts is None else counts
    stored_rate = None if matched + python_only == 0 else matched / (matched + python_only)
    return loaded(
        build_artifact(
            ParityArtifact,
            label=label or sid.replace("_", "-"),
            created=created,
            synthetic=synthetic,
            data=data_info(synthetic=synthetic) if data == "decision" else data,
            params=dict(PARITY_PARAMS) if params == "configured" else params,
            instrument=INSTRUMENT_INFO if instrument == "configured" else instrument,
            # The Python side's session gap and capital (gate attacks K, L, 2026-11-19).
            min_gap_seconds=MIN_GAP_SECONDS,
            initial_capital=100_000.0,
            strategy_id=sid,
            matched=matched,
            python_only=python_only,
            tradingview_only=0,
            match_rate=stored_rate
            if counts is not None
            else (None if rate is None else matched / 10_000),
            threshold=0.98,
            passed=did_pass,
            mismatches=(),
            sources=("list_of_trades.csv",),
        )
    )


def passing_set(strategies: Sequence[str] = STRATEGIES) -> list[LoadedArtifact]:
    items = [validation(), monte_carlo()]
    for sid in strategies:
        items += [backtest(sid), parity(sid)]
    return items


def inputs(artifacts: Sequence[LoadedArtifact] | None = None, **changes: object) -> GoNoGoInputs:
    base = GoNoGoInputs(
        artifacts=passing_set() if artifacts is None else artifacts,
        criteria=CRITERIA,
        venue_name=VENUE_NAME,
        venue=VENUE,
        strategy_ids=STRATEGIES,
        planned_risk_pct=PLANNED_RISK_PCT,
        today=TODAY,
        created=REPORT_TIME,
        compliance_files=("2026-11-13-ftmo-kural-dogrulamasi.md",),
        expected_costs=EXPECTED_COSTS,
        expected_instruments=EXPECTED_INSTRUMENTS,
        expected_params=EXPECTED_PARAMS,
        # Explicit since 2026-11-19: unknown -> G6 / the decision run are not evaluable.
        expected_parity_params=EXPECTED_PARITY_PARAMS,
        risk_params=RISK_PARAMS,
        # Explicit since the default became None (unknown -> not evaluable).
        artifacts_truncated=False,
        expected_oos_start_utc=OOS_START,
        # Explicit since gate attack L (2026-11-19): unknown -> no decision run.
        expected_min_gap_seconds=MIN_GAP_SECONDS,
    )
    return dataclasses.replace(base, **changes)  # type: ignore[arg-type]


def replaced(
    items: list[LoadedArtifact], index_of: str, new: LoadedArtifact
) -> list[LoadedArtifact]:
    """The passing set with the artifact whose file name starts with `index_of` replaced."""
    result = [item for item in items if not item.file_name.startswith(index_of)]
    assert len(result) == len(items) - 1
    return [*result, new]


def without(items: list[LoadedArtifact], prefix: str) -> list[LoadedArtifact]:
    return [item for item in items if not item.file_name.startswith(prefix)]


def status(result: GateResult) -> str:
    return result.gate.status


ORB_BT = "backtest_ny-orb-fvg_"
ORB_PARITY = "parity_ny-orb-fvg_"


# --- the whole report ----------------------------------------------------------------------


def test_full_passing_set_passes_every_gate_except_demo_forward() -> None:
    report = evaluate(inputs())
    by_id = {gate.id: gate.status for gate in report.gates}
    assert by_id == {
        "G1": "PASS",
        "G2": "PASS",
        "G3": "PASS",
        "G4": "PASS",
        "G5": "NOT_EVALUABLE",
        "G6": "PASS",
        "G7": "PASS",
    }
    assert report.overall == "NO_GO"
    assert not report.synthetic
    assert any(reason.startswith("G5 DEĞERLENDİRİLEMEDİ") for reason in report.reasons)


def test_evaluate_can_never_return_go_in_faz_2() -> None:
    report = evaluate(inputs())
    assert report.overall == "NO_GO"
    g5 = next(gate for gate in report.gates if gate.id == "G5")
    assert g5.status == "NOT_EVALUABLE"
    assert "Faz 3" in g5.evidence


def test_g5_is_never_pass_whatever_the_inputs() -> None:
    for case in (inputs(), inputs(artifacts=[]), inputs(strategy_ids=())):
        assert status(gate_demo_forward(case)) == "NOT_EVALUABLE"


def g5_pass() -> GateResult:
    """A G5 PASS that evaluate never produces: only for testing the assembly."""
    result = gate_demo_forward(inputs())
    gate = result.gate.model_copy(update={"status": "PASS"})
    return GateResult(gate=gate, used=())


def all_results(case: GoNoGoInputs) -> list[GateResult]:
    return [
        gate_data(case),
        gate_edge(case),
        gate_robustness(case),
        gate_monte_carlo(case),
        g5_pass(),
        gate_parity(case),
        gate_rules(case),
    ]


def test_assemble_says_go_only_when_all_seven_pass_on_real_data() -> None:
    report = assemble(all_results(inputs()), label="gonogo", created=REPORT_TIME)
    assert report.overall == "GO"
    assert all(gate.status == "PASS" for gate in report.gates)


def test_assemble_with_one_gate_missing_is_no_go() -> None:
    results = all_results(inputs())[:-1]
    report = assemble(results, label="gonogo", created=REPORT_TIME)
    assert report.overall == "NO_GO"
    assert any("G7" in reason for reason in report.reasons)


def test_synthetic_input_anywhere_is_no_go_with_the_warning() -> None:
    items = replaced(passing_set(), "monte_carlo_", monte_carlo(synthetic=True))
    report = assemble(all_results(inputs(items)), label="gonogo", created=REPORT_TIME)
    assert report.synthetic
    assert report.overall == "NO_GO"
    assert any(SYNTHETIC_TAG in note for note in report.notes)
    assert SYNTHETIC_TAG in render_markdown(report)


def test_synthetic_backtest_fails_g1_g2_g3_with_the_words() -> None:
    items = replaced(passing_set(), ORB_BT, backtest(synthetic=True))
    case = inputs(items)
    for gate in (gate_data, gate_edge, gate_robustness):
        result = gate(case)
        assert status(result) == "FAIL"
        assert "SENTETİK VERİ" in result.gate.evidence
    report = evaluate(case)
    assert report.synthetic
    assert "SENTETİK VERİ" in render_markdown(report)


def test_report_is_validated_by_the_model_on_load(tmp_path: Path) -> None:
    report = evaluate(inputs())
    _md, json_path = write_report(report, tmp_path)
    again = load_artifact(json_path)
    assert isinstance(again, GoNoGoArtifact)
    assert again == report


def test_today_must_be_the_report_dates_utc_day() -> None:
    with pytest.raises(GoNoGoError, match="UTC tarihiyle"):
        evaluate(inputs(today=TODAY + dt.timedelta(days=1)))


def test_naive_report_time_is_refused() -> None:
    with pytest.raises(GoNoGoError, match="saat dilimi"):
        evaluate(inputs(created=REPORT_TIME.replace(tzinfo=None)))


@pytest.mark.parametrize("risk", [0.0, -0.25, math.nan, math.inf])
def test_planned_risk_must_be_positive_and_finite(risk: float) -> None:
    with pytest.raises(GoNoGoError, match="risk"):
        evaluate(inputs(planned_risk_pct=risk))


# --- which strategies ----------------------------------------------------------------------


def test_empty_strategy_list_makes_every_per_strategy_gate_not_evaluable() -> None:
    case = inputs(strategy_ids=())
    for gate in (gate_data, gate_edge, gate_robustness, gate_parity):
        result = gate(case)
        assert status(result) == "NOT_EVALUABLE"
        assert "strateji listesi boş" in result.gate.evidence


def test_listed_strategy_without_backtest_is_named_and_not_evaluable() -> None:
    case = inputs(strategy_ids=(*STRATEGIES, "silver_bullet"))
    for gate in (gate_data, gate_edge, gate_robustness):
        result = gate(case)
        assert status(result) == "NOT_EVALUABLE"
        assert "silver_bullet: DEĞERLENDİRİLEMEDİ — backtest raporu yok" in result.gate.evidence


def test_one_failing_strategy_fails_the_gate_and_is_named() -> None:
    items = replaced(
        passing_set(), "backtest_asian-sweep_", backtest("asian_sweep", oos=stats(300, -0.1, 1.4))
    )
    result = gate_edge(inputs(items))
    assert status(result) == "FAIL"
    assert "asian_sweep: KALDI" in result.gate.evidence
    assert "ny_orb_fvg: GEÇTİ" in result.gate.evidence


def test_fail_outranks_not_evaluable_in_a_gate() -> None:
    items = replaced(
        passing_set(), "backtest_asian-sweep_", backtest("asian_sweep", oos=stats(300, -0.1, 1.4))
    )
    result = gate_edge(inputs(items, strategy_ids=(*STRATEGIES, "missing")))
    assert status(result) == "FAIL"


def test_label_is_not_trusted_strategy_id_is() -> None:
    # A parity report labelled like ORB but carrying another strategy id does not count for ORB.
    items = without(passing_set(), ORB_PARITY)
    items.append(parity("other_strategy", label="ny-orb-fvg"))
    result = gate_parity(inputs(items))
    assert status(result) == "NOT_EVALUABLE"
    assert "ny_orb_fvg: DEĞERLENDİRİLEMEDİ — parite raporu yok" in result.gate.evidence


def test_newest_backtest_is_used_not_the_best() -> None:
    newer = backtest(
        label="orb-v2", created=MADE + dt.timedelta(hours=1), oos=stats(300, -0.2, 1.4)
    )
    result = gate_edge(inputs([*passing_set(), newer]))
    assert status(result) == "FAIL"
    assert newer.file_name in result.gate.evidence


def test_duplicate_strategy_ids_are_judged_once() -> None:
    result = gate_edge(inputs(strategy_ids=("ny_orb_fvg", "ny_orb_fvg")))
    assert result.gate.evidence.count("ny_orb_fvg: GEÇTİ") == 1


# --- G1 data -------------------------------------------------------------------------------


def test_g1_passes_on_long_real_clean_one_minute_data() -> None:
    result = gate_data(inputs())
    assert status(result) == "PASS"
    assert DATA_FILE in result.gate.evidence
    assert any(item.file_name.startswith("data_validation_") for item in result.used)


def test_g1_validation_missing_is_not_evaluable() -> None:
    result = gate_data(inputs(without(passing_set(), "data_validation_")))
    assert status(result) == "NOT_EVALUABLE"
    assert "doğrulama raporu yok" in result.gate.evidence


def test_g1_validation_not_clean_fails() -> None:
    items = replaced(passing_set(), "data_validation_", validation(clean=False))
    result = gate_data(inputs(items))
    assert status(result) == "FAIL"
    assert "temiz değil" in result.gate.evidence


def test_g1_validation_of_another_content_of_the_same_file_fails() -> None:
    items = replaced(passing_set(), "data_validation_", validation(sha=OTHER_SHA))
    result = gate_data(inputs(items))
    assert status(result) == "FAIL"
    assert "sha256 farklı" in result.gate.evidence


def test_g1_validation_of_an_unrelated_file_counts_as_missing() -> None:
    items = replaced(
        passing_set(), "data_validation_", validation(sha=OTHER_SHA, file_name="other.csv")
    )
    assert status(gate_data(inputs(items))) == "NOT_EVALUABLE"


def test_g1_sha_link_picks_the_validation_of_the_same_content() -> None:
    dirty_other = validation(
        sha=OTHER_SHA, clean=False, label="other", created=MADE + dt.timedelta(hours=2)
    )
    result = gate_data(inputs([*passing_set(), dirty_other]))
    assert status(result) == "PASS"


def test_g1_newest_validation_of_the_same_content_wins() -> None:
    dirty_newer = validation(clean=False, label="again", created=MADE + dt.timedelta(hours=2))
    assert status(gate_data(inputs([*passing_set(), dirty_newer]))) == "FAIL"


def test_g1_backtest_without_sha_fails() -> None:
    items = replaced(passing_set(), ORB_BT, backtest(data=data_info(sha="")))
    result = gate_data(inputs(items))
    assert status(result) == "FAIL"
    assert "sha256 yok" in result.gate.evidence


def test_g1_backtest_without_data_info_is_not_evaluable() -> None:
    items = replaced(passing_set(), ORB_BT, backtest(no_data=True))
    assert status(gate_data(inputs(items))) == "NOT_EVALUABLE"


def test_g1_bars_longer_than_required_fail() -> None:
    items = replaced(passing_set(), ORB_BT, backtest(data=data_info(tf=300)))
    result = gate_data(inputs(items))
    assert status(result) == "FAIL"
    assert "bar uzunluğu" in result.gate.evidence


def test_g1_empty_data_fails() -> None:
    items = replaced(passing_set(), ORB_BT, backtest(data=data_info(bars=0, first=None, last=None)))
    assert status(gate_data(inputs(items))) == "FAIL"


def span_end(years: float) -> str:
    first = dt.datetime(2024, 1, 2, 14, 30, tzinfo=dt.UTC)
    last = first + dt.timedelta(days=years * 365.25)
    return last.strftime("%Y-%m-%dT%H:%M:%SZ")


def test_g1_span_exactly_two_years_passes() -> None:
    # Stricter since review D (2026-11-19): the validation must have read the same bars
    # (first/last bar too), so the shorter reading gets its own validation report.
    bars = data_info(last=span_end(2.0))
    items = replaced(passing_set(), ORB_BT, backtest(data=bars))
    items.append(validation(data=bars, label="us500-short"))
    assert status(gate_data(inputs(items))) == "PASS"


def test_g1_span_one_second_short_of_two_years_fails() -> None:
    short = dt.datetime.fromisoformat(span_end(2.0)) - dt.timedelta(seconds=1)
    items = replaced(
        passing_set(), ORB_BT, backtest(data=data_info(last=short.strftime("%Y-%m-%dT%H:%M:%SZ")))
    )
    result = gate_data(inputs(items))
    assert status(result) == "FAIL"
    assert "veri kapsamı" in result.gate.evidence


def test_g1_stricter_criteria_from_config_apply() -> None:
    stricter = CRITERIA.model_copy(update={"min_data_years": 3.0})
    assert status(gate_data(inputs(criteria=stricter))) == "FAIL"


def test_g1_synthetic_validation_fails() -> None:
    items = replaced(passing_set(), "data_validation_", validation(synthetic=True))
    result = gate_data(inputs(items))
    assert status(result) == "FAIL"
    assert SYNTHETIC_TAG in result.gate.evidence


def test_g1_only_comparison_runs_are_not_evaluable_and_named() -> None:
    parity_run = backtest(fill_model="tv_ohlc")
    items = replaced(passing_set(), ORB_BT, parity_run)
    result = gate_data(inputs(items))
    assert status(result) == "NOT_EVALUABLE"
    assert parity_run.file_name in result.gate.evidence


def test_g1_only_synthetic_comparison_run_fails_as_synthetic() -> None:
    items = replaced(passing_set(), ORB_BT, backtest(fill_model="tv_ohlc", synthetic=True))
    result = gate_data(inputs(items))
    assert status(result) == "FAIL"
    assert SYNTHETIC_TAG in result.gate.evidence


# --- G2 edge -------------------------------------------------------------------------------


def test_g2_t_stat_exactly_at_the_floor_passes() -> None:
    oos = stats(100, 0.2, 1.0, t=2.0)
    items = replaced(passing_set(), ORB_BT, backtest(oos=oos))
    assert status(gate_edge(inputs(items))) == "PASS"


def test_g2_t_stat_below_the_floor_fails() -> None:
    items = replaced(passing_set(), ORB_BT, backtest(oos=stats(100, 0.19, 1.0)))
    result = gate_edge(inputs(items))
    assert status(result) == "FAIL"
    assert "t-istatistiği" in result.gate.evidence


def test_g2_stricter_t_floor_from_config_applies() -> None:
    stricter = CRITERIA.model_copy(update={"min_oos_t_stat": 3.0})
    items = replaced(passing_set(), ORB_BT, backtest(oos=stats(100, 0.25, 1.0)))
    assert status(gate_edge(inputs(items, criteria=stricter))) == "FAIL"


@pytest.mark.parametrize("mean", [0.0, -0.1])
def test_g2_non_positive_expectancy_fails(mean: float) -> None:
    items = replaced(passing_set(), ORB_BT, backtest(oos=stats(300, mean, 1.4), is_mean=0.4))
    assert status(gate_edge(inputs(items))) == "FAIL"


def test_g2_no_oos_trades_fails() -> None:
    items = replaced(passing_set(), ORB_BT, backtest(oos=stats(0, None, None)))
    result = gate_edge(inputs(items))
    assert status(result) == "FAIL"
    assert "OOS örnekleminde işlem yok" in result.gate.evidence


def test_g2_missing_t_stat_is_a_fail_never_a_pass() -> None:
    items = replaced(passing_set(), ORB_BT, backtest(oos=stats(1, 0.5, None)))
    assert status(gate_edge(inputs(items))) == "FAIL"


def test_g2_t_stat_inconsistent_with_its_inputs_fails() -> None:
    # A hand-edited t-stat: mean 0.05, stdev 1.4, n 300 gives ~0.62, not 5.
    items = replaced(passing_set(), ORB_BT, backtest(oos=stats(300, 0.05, 1.4, t=5.0)))
    result = gate_edge(inputs(items))
    assert status(result) == "FAIL"
    assert "tutarsız" in result.gate.evidence


def test_g2_tv_ohlc_run_cannot_satisfy_the_gate() -> None:
    items = replaced(passing_set(), ORB_BT, backtest(fill_model="tv_ohlc"))
    result = gate_edge(inputs(items))
    assert status(result) == "FAIL"
    assert "pessimistic" in result.gate.evidence


def test_g2_cost_free_run_cannot_satisfy_the_gate() -> None:
    run = backtest(cost_free=True)
    assert any(COST_FREE_TAG in note for note in run.artifact.notes)
    items = replaced(passing_set(), ORB_BT, run)
    assert status(gate_edge(inputs(items))) == "FAIL"


def test_g2_newer_comparison_run_does_not_hide_the_decision_run() -> None:
    newer_free = backtest(label="orb-free", cost_free=True, created=MADE + dt.timedelta(hours=1))
    result = gate_edge(inputs([*passing_set(), newer_free]))
    assert status(result) == "PASS"
    assert newer_free.file_name not in result.gate.evidence


# --- decision run: recorded parameters ---------------------------------------------------


def test_absent_expected_params_block_every_run_as_unconfirmable() -> None:
    case = inputs(expected_params=None)
    for gate in (gate_data, gate_edge, gate_robustness):
        result = gate(case)
        assert status(result) == "NOT_EVALUABLE"
        assert EXPECTED_PARAMS_MISSING in result.gate.evidence
        assert "daha eski bir koşuya geçilmez" in result.gate.evidence
    assert evaluate(case).overall == "NO_GO"


def test_strategy_without_expected_params_entry_is_not_evaluable_and_named() -> None:
    expected = {"asian_sweep": dict(PARAMS)}
    result = gate_edge(inputs(expected_params=expected))
    assert status(result) == "NOT_EVALUABLE"
    assert "'ny_orb_fvg' için beklenen parametreler verilmedi" in result.gate.evidence


def test_equal_params_keep_the_passing_path() -> None:
    # A fresh mapping with the same values (not the same object) must still match.
    expected = {sid: {**PARAMS} for sid in STRATEGIES}
    for gate in (gate_data, gate_edge, gate_robustness, gate_monte_carlo):
        assert status(gate(inputs(expected_params=expected))) == "PASS"


@pytest.mark.parametrize(
    "recorded",
    [
        {**PARAMS, "risk_reward": 2.5},  # one value differs
        {**PARAMS, "or_minutes": 15.0},  # 15 and 15.0 are different JSON scalars
        {**PARAMS, "use_filter": 1},  # True and 1 too
        {**PARAMS, "extra": 3},  # a key the configured strategy does not have
        {k: v for k, v in PARAMS.items() if k != "or_minutes"},  # a missing key
    ],
)
def test_newest_run_with_other_params_blocks_and_older_run_is_not_used(
    recorded: dict[str, Scalar],
) -> None:
    newer = backtest(label="orb-other", params=recorded, created=MADE + dt.timedelta(hours=1))
    result = gate_edge(inputs([*passing_set(), newer]))
    assert status(result) == "NOT_EVALUABLE"
    assert newer.file_name in result.gate.evidence
    assert "config/strategies.yaml" in result.gate.evidence
    assert "daha eski bir koşuya geçilmez" in result.gate.evidence


def test_differing_param_names_are_listed() -> None:
    newer = backtest(
        label="orb-other",
        params={**PARAMS, "risk_reward": 3.0},
        created=MADE + dt.timedelta(hours=1),
    )
    result = gate_robustness(inputs([*passing_set(), newer]))
    assert "aynı değil: risk_reward" in result.gate.evidence


def test_comparison_run_with_other_params_still_does_not_hide_the_decision_run() -> None:
    newer_tv = backtest(
        label="orb-tv",
        fill_model="tv_ohlc",
        params={**PARAMS, "risk_reward": 9.0},
        created=MADE + dt.timedelta(hours=1),
    )
    assert status(gate_edge(inputs([*passing_set(), newer_tv]))) == "PASS"


# --- G3 robustness -------------------------------------------------------------------------


def test_g3_ratio_exactly_at_the_floor_passes() -> None:
    items = replaced(passing_set(), ORB_BT, backtest(oos=stats(300, 0.2, 1.0), is_mean=0.4))
    result = gate_robustness(inputs(items))
    assert status(result) == "PASS"
    assert "yalnızca oran" in result.gate.evidence


def test_g3_ratio_below_the_floor_fails() -> None:
    items = replaced(passing_set(), ORB_BT, backtest(oos=stats(300, 0.19, 1.0), is_mean=0.4))
    assert status(gate_robustness(inputs(items))) == "FAIL"


def test_g3_stricter_ratio_from_config_applies() -> None:
    stricter = CRITERIA.model_copy(update={"min_oos_to_is_ratio": 0.8})
    items = replaced(passing_set(), ORB_BT, backtest(oos=stats(300, 0.25, 1.0), is_mean=0.4))
    assert status(gate_robustness(inputs(items, criteria=stricter))) == "FAIL"


def test_g3_missing_ratio_fails_with_its_note() -> None:
    note = "IS beklentisi sıfır ya da negatif: oran anlamsız"
    items = replaced(passing_set(), ORB_BT, backtest(is_mean=-0.1, g3_note=note))
    result = gate_robustness(inputs(items))
    assert status(result) == "FAIL"
    assert note in result.gate.evidence


def test_g3_ratio_inconsistent_with_the_means_fails() -> None:
    items = replaced(passing_set(), ORB_BT, backtest(is_mean=-0.1, g3_ratio=0.9))
    result = gate_robustness(inputs(items))
    assert status(result) == "FAIL"
    assert "tutarsız" in result.gate.evidence


def test_g3_filter_choice_is_a_manual_item_that_does_not_decide_the_gate() -> None:
    result = gate_robustness(inputs())
    assert status(result) == "PASS"
    assert result.gate.details["manual"] == [gonogo.G3_MANUAL_ITEM]
    assert "elle teyit" in result.gate.evidence


# --- G4 Monte Carlo ------------------------------------------------------------------------


def g4_with(**changes: object) -> GateResult:
    items = replaced(passing_set(), "monte_carlo_", monte_carlo(**changes))  # type: ignore[arg-type]
    return gate_monte_carlo(inputs(items))


def test_g4_passes_with_its_inputs_in_the_evidence() -> None:
    result = gate_monte_carlo(inputs())
    assert status(result) == "PASS"
    evidence = result.gate.evidence
    for text in ("oos_trades.csv", "600 işlem", "%0,25", "10.000 yol", "tohum 20261113"):
        assert text in evidence


def test_g4_boundaries_exactly_at_the_criteria_pass() -> None:
    assert status(g4_with(p_pass=0.60, p_breach=0.20)) == "PASS"


def test_g4_p_pass_below_the_floor_fails() -> None:
    assert status(g4_with(p_pass=0.59)) == "FAIL"


def test_g4_p_breach_above_the_ceiling_fails() -> None:
    assert status(g4_with(p_breach=0.21)) == "FAIL"


def test_g4_stricter_criteria_from_config_apply() -> None:
    stricter = CRITERIA.model_copy(update={"min_p_pass_all_phases": 0.8, "max_p_breach": 0.05})
    assert status(gate_monte_carlo(inputs(criteria=stricter))) == "FAIL"


def test_g4_missing_is_not_evaluable() -> None:
    result = gate_monte_carlo(inputs(without(passing_set(), "monte_carlo_")))
    assert status(result) == "NOT_EVALUABLE"
    assert "Monte Carlo raporu yok" in result.gate.evidence


def test_g4_other_venue_is_not_evaluable_and_named() -> None:
    result = g4_with(venue="funding_pips")
    assert status(result) == "NOT_EVALUABLE"
    assert "funding_pips" in result.gate.evidence


def test_g4_newest_run_for_the_venue_is_used() -> None:
    newer = monte_carlo(p_pass=0.3, label="ftmo-2", created=MADE + dt.timedelta(hours=1))
    assert status(gate_monte_carlo(inputs([*passing_set(), newer]))) == "FAIL"


def test_g4_synthetic_fails() -> None:
    result = g4_with(synthetic=True)
    assert status(result) == "FAIL"
    assert SYNTHETIC_TAG in result.gate.evidence


def test_g4_synthetic_trade_file_name_fails_even_without_the_flag() -> None:
    result = g4_with(trades_file="synthetic_trades.csv")
    assert status(result) == "FAIL"
    assert SYNTHETIC_TAG in result.gate.evidence


def test_g4_too_few_paths_is_not_evaluable() -> None:
    assert status(g4_with(n_paths=MIN_MC_PATHS - 1)) == "NOT_EVALUABLE"
    # Stricter since gate attack N (2026-11-19): G4 judges only the decision path count
    # (the simulator's default, or a stricter configured minimum); the floor alone no
    # longer passes, because picking among path counts would pick among results.
    assert status(g4_with(n_paths=MIN_MC_PATHS)) == "NOT_EVALUABLE"
    assert status(g4_with(n_paths=gonogo.decision_paths(CRITERIA))) == "PASS"


def test_g4_too_few_trades_is_not_evaluable() -> None:
    # G4 now also requires the list's count to equal the decision runs' complete trades,
    # so the boundary is tested with decision runs that hold exactly that many.
    for n, expected in ((MIN_MC_TRADES - 1, "NOT_EVALUABLE"), (MIN_MC_TRADES, "PASS")):
        first = n // 2
        items = [
            validation(),
            # Since review F the list's R and mae_r sums must follow from these runs too.
            monte_carlo(
                n_trades=n,
                sum_r=math.fsum([0.3 * first, 0.3 * (n - first)]),
                sum_mae_r=-float(n),
            ),
            backtest(STRATEGIES[0], n_all=first),
            backtest(STRATEGIES[1], n_all=n - first),
            *(parity(sid) for sid in STRATEGIES),
        ]
        result = gate_monte_carlo(inputs(items))
        assert status(result) == expected, result.gate.evidence
    assert (
        "girdi çok küçük"
        in gate_monte_carlo(
            inputs(
                [
                    validation(),
                    monte_carlo(n_trades=MIN_MC_TRADES - 1),
                    backtest(STRATEGIES[0], n_all=49),
                    backtest(STRATEGIES[1], n_all=50),
                    *(parity(sid) for sid in STRATEGIES),
                ]
            )
        ).gate.evidence
    )


def test_g4_other_risk_than_planned_is_not_evaluable() -> None:
    result = g4_with(risk_pct=0.1)
    assert status(result) == "NOT_EVALUABLE"
    assert "seçilen riskle" in result.gate.evidence


def test_g4_experimental_risk_steps_are_not_evaluable() -> None:
    assert status(g4_with(experimental=True)) == "NOT_EVALUABLE"


def test_g4_rules_other_than_the_venue_are_not_evaluable() -> None:
    assert status(g4_with(targets=(8.0, 5.0))) == "NOT_EVALUABLE"
    assert status(g4_with(targets=(10.0,))) == "NOT_EVALUABLE"
    assert status(g4_with(min_days=0)) == "NOT_EVALUABLE"


def test_g4_assumed_minimum_trading_days_are_not_evaluable() -> None:
    assert status(g4_with(assumed=True)) == "NOT_EVALUABLE"


# --- G6 parity -----------------------------------------------------------------------------


def test_g6_rate_exactly_at_the_floor_passes() -> None:
    items = replaced(passing_set(), ORB_PARITY, parity(rate=0.98))
    assert status(gate_parity(inputs(items))) == "PASS"


def test_g6_rate_below_the_floor_fails() -> None:
    items = replaced(passing_set(), ORB_PARITY, parity(rate=0.97))
    result = gate_parity(inputs(items))
    assert status(result) == "FAIL"
    assert "eşleşme" in result.gate.evidence


def test_g6_report_that_did_not_pass_fails() -> None:
    items = replaced(passing_set(), ORB_PARITY, parity(rate=0.99, passed=False))
    assert status(gate_parity(inputs(items))) == "FAIL"


def test_g6_no_rate_fails() -> None:
    items = replaced(passing_set(), ORB_PARITY, parity(rate=None))
    assert status(gate_parity(inputs(items))) == "FAIL"


def test_g6_missing_is_not_evaluable() -> None:
    assert status(gate_parity(inputs(without(passing_set(), ORB_PARITY)))) == "NOT_EVALUABLE"


def test_g6_stricter_floor_from_config_applies() -> None:
    stricter = CRITERIA.model_copy(update={"min_parity_rate": 0.995})
    assert status(gate_parity(inputs(criteria=stricter))) == "FAIL"


def test_g6_synthetic_parity_fails() -> None:
    items = replaced(passing_set(), ORB_PARITY, parity(synthetic=True))
    assert status(gate_parity(inputs(items))) == "FAIL"


# --- G6: the Master portfolio (user decision of 2026-11-19, open finding 72) ---------------

MASTER = "master_portfolio"
MASTER_LEGS = ("master_ny_orb_fvg", "master_asian_sweep")


def master_case(strategy_ids: Sequence[str]) -> GoNoGoInputs:
    """One parity report, the script's (TradingView has one trade list per script)."""
    return inputs(
        [parity(MASTER)],
        strategy_ids=strategy_ids,
        expected_parity_params={sid: dict(PARITY_PARAMS) for sid in strategy_ids},
        # What the CLI passes: a leg is produced by the portfolio's script.
        script_ids=dict.fromkeys(strategy_ids, MASTER),
    )


def test_g6_of_a_master_leg_reads_its_scripts_parity_report_and_says_so() -> None:
    result = gate_parity(master_case(MASTER_LEGS))

    assert status(result) == "PASS"
    for leg in MASTER_LEGS:
        assert f"'{leg}' bir betik bacağıdır: parite betiğin ('{MASTER}') raporundan okunur" in (
            result.gate.evidence
        )
    assert "bacak başına ayrı oran yok" in result.gate.evidence
    assert [item.file_name for item in result.used] == [parity(MASTER).file_name]


def test_g6_of_the_combined_id_reads_the_same_report_as_its_own() -> None:
    result = gate_parity(master_case((MASTER,)))

    assert status(result) == "PASS"
    assert "bir betik bacağıdır" not in result.gate.evidence
    assert [item.file_name for item in result.used] == [parity(MASTER).file_name]


def test_g6_of_a_leg_without_the_script_map_finds_no_report() -> None:
    # A caller that does not say which script a leg belongs to gets no borrowed evidence.
    result = gate_parity(dataclasses.replace(master_case(MASTER_LEGS), script_ids=None))

    assert status(result) == "NOT_EVALUABLE"
    assert "parite raporu yok" in result.gate.evidence


def test_g6_of_a_leg_names_the_script_when_its_report_is_missing() -> None:
    result = gate_parity(dataclasses.replace(master_case(MASTER_LEGS), artifacts=[parity()]))

    assert status(result) == "NOT_EVALUABLE"
    assert "parite betiğin, 'master_portfolio', raporundan okunur" in result.gate.evidence


def test_g6_of_a_leg_still_checks_the_scripts_report_like_any_other() -> None:
    failed = parity(MASTER, rate=0.9)
    result = gate_parity(dataclasses.replace(master_case(MASTER_LEGS), artifacts=[failed]))

    assert status(result) == "FAIL"


# --- G7 rules ------------------------------------------------------------------------------


def venue(**changes: object) -> VenueProfile:
    return VENUE.model_copy(update=changes)


def test_g7_passes_with_fresh_rules_and_lists_compliance_files() -> None:
    result = gate_rules(inputs())
    assert status(result) == "PASS"
    assert "2026-11-13-ftmo-kural-dogrulamasi.md" in result.gate.evidence
    assert "yazılı otomasyon onayı" in result.gate.evidence


def test_g7_rules_exactly_at_the_maximum_age_pass() -> None:
    edge = venue(rules_verified_at=TODAY - dt.timedelta(days=RULES_MAX_AGE_DAYS))
    assert status(gate_rules(inputs(venue=edge))) == "PASS"


def test_g7_stale_rules_fail() -> None:
    stale = venue(rules_verified_at=TODAY - dt.timedelta(days=RULES_MAX_AGE_DAYS + 1))
    assert status(gate_rules(inputs(venue=stale))) == "FAIL"


def test_g7_missing_rules_date_fails() -> None:
    assert status(gate_rules(inputs(venue=venue(rules_verified_at=None)))) == "FAIL"


def test_g7_future_rules_date_is_not_evaluable() -> None:
    future = venue(rules_verified_at=TODAY + dt.timedelta(days=5))
    assert status(gate_rules(inputs(venue=future))) == "NOT_EVALUABLE"


def test_g7_automation_not_allowed_fails() -> None:
    result = gate_rules(inputs(venue=venue(automation_allowed=False)))
    assert status(result) == "FAIL"
    assert "automation_allowed: false" in result.gate.evidence


def test_g7_no_compliance_files_is_stated() -> None:
    result = gate_rules(inputs(compliance_files=()))
    assert "dosya yok" in result.gate.evidence


# --- staleness and broken files ------------------------------------------------------------


def mc_at(when: dt.datetime) -> list[LoadedArtifact]:
    """The passing set with the Monte Carlo run (and its trade list) made at `when`.

    The backtests are moved too when `when` is earlier: since G4 checks the provenance, a
    Monte Carlo run older than the decision runs its trade list came from is inconsistent
    on its own (that rule is tested separately); here only the age decides.
    """
    items = without(without(passing_set(), "monte_carlo_"), "backtest_")
    runs = when if when < MADE else MADE
    items += [backtest(sid, created=runs) for sid in STRATEGIES]
    return [*items, monte_carlo(created=when, provenance=trades_provenance(created=runs))]


@pytest.mark.parametrize(
    ("prefix", "gate", "build"),
    [
        (ORB_BT, gate_edge, lambda when: replaced(passing_set(), ORB_BT, backtest(created=when))),
        (
            "data_validation_",
            gate_data,
            lambda when: replaced(passing_set(), "data_validation_", validation(created=when)),
        ),
        ("monte_carlo_", gate_monte_carlo, mc_at),
        (
            ORB_PARITY,
            gate_parity,
            lambda when: replaced(passing_set(), ORB_PARITY, parity(created=when)),
        ),
    ],
)
def test_stale_and_future_artifacts_cannot_support_a_pass(
    prefix: str,
    gate: Callable[[GoNoGoInputs], GateResult],
    build: Callable[[dt.datetime], list[LoadedArtifact]],
) -> None:
    del prefix  # names the case in the test id
    edge = dt.datetime.combine(
        TODAY - dt.timedelta(days=MAX_ARTIFACT_AGE_DAYS), dt.time(), tzinfo=dt.UTC
    )
    old = edge - dt.timedelta(days=1)
    future = REPORT_TIME + dt.timedelta(minutes=1)
    assert status(gate(inputs(build(edge)))) == "PASS"
    stale = gate(inputs(build(old)))
    assert status(stale) == "NOT_EVALUABLE"
    assert "gün eski" in stale.gate.evidence
    ahead = gate(inputs(build(future)))
    assert status(ahead) == "NOT_EVALUABLE"
    assert "ileri tarihli" in ahead.gate.evidence


@pytest.mark.parametrize(
    ("file_name", "affected"),
    [
        # G4 too (stricter since its trade list chains to the newest decision runs: an
        # unreadable backtest may be the newer run the list should have come from).
        ("backtest_orb_20261117T100000Z.json", {"G1", "G2", "G3", "G4"}),
        ("data_validation_x_20261117T100000Z.json", {"G1"}),
        ("monte_carlo_ftmo_20261117T100000Z.json", {"G4"}),
        ("parity_orb_20261117T100000Z.json", {"G6"}),
        ("cost_matrix_all_20261117T100000Z.json", set()),
        ("reports", {"G1", "G2", "G3", "G4", "G6"}),
    ],
)
def test_unreadable_report_files_block_the_gates_they_may_hide(
    file_name: str, affected: set[str]
) -> None:
    problem = ArtifactProblem(file_name, f"{file_name}: geçerli JSON değil")
    report = evaluate(inputs(problems=(problem,)))
    for gate in report.gates:
        if gate.id in affected:
            assert gate.status == "NOT_EVALUABLE"
            assert file_name in gate.evidence
        elif gate.id != "G5":
            assert gate.status == "PASS"
    if affected:
        assert file_name in render_markdown(report)


# --- Markdown ------------------------------------------------------------------------------


def failing_report() -> GoNoGoArtifact:
    items = replaced(passing_set(), ORB_BT, backtest(oos=stats(1, 0.5, None), is_mean=None))
    items = without(items, "monte_carlo_")
    return evaluate(inputs(items, strategy_ids=(*STRATEGIES, "missing")))


def test_markdown_has_title_verdict_table_details_and_reminder() -> None:
    report = evaluate(inputs())
    text = render_markdown(report)
    assert text.startswith("<!-- propbot-go-no-go created_utc=2026-11-18T12:00:00Z -->")
    assert "# Go/No-Go raporu — 2026-11-18 — ftmo_2step_100k" in text
    assert "**Karar: NO-GO**" in text
    for gate_id in ("G1", "G2", "G3", "G4", "G5", "G6", "G7"):
        assert re.search(rf"^\| {gate_id} \|", text, re.MULTILINE)
        assert f"### {gate_id} — " in text
    assert "GEÇTİ" in text
    assert "DEĞERLENDİRİLEMEDİ" in text
    assert "## Elle teyit edilecekler" in text
    assert "gün/saat filtrelerinin" in text
    assert "beslemesinden geldiği" in text
    assert "## Girdi dosyaları" in text
    for item in passing_set():
        assert item.file_name in text
    assert "2026-11-17T10:00:00Z" in text
    assert "Faz 4" in text
    assert "kullanıcının kendi" in text


def test_markdown_shows_fail_and_never_raw_none_or_nan() -> None:
    text = render_markdown(failing_report())
    assert "KALDI" in text
    assert "DEĞERLENDİRİLEMEDİ" in text
    for raw in ("None", "nan", "NaN", "null", "True", "False"):
        assert re.search(rf"\b{raw}\b", text) is None, raw


def test_markdown_table_rows_stay_on_one_line() -> None:
    text = render_markdown(failing_report())
    rows = [line for line in text.splitlines() if line.startswith("| G")]
    assert len(rows) == 7
    assert all(line.endswith("|") for line in rows)


def test_markdown_go_verdict_line() -> None:
    report = assemble(all_results(inputs()), label="gonogo", created=REPORT_TIME)
    assert "**Karar: GO**" in render_markdown(report)


# --- files ---------------------------------------------------------------------------------


def test_write_report_writes_the_spec_named_markdown_and_the_json(tmp_path: Path) -> None:
    report = evaluate(inputs())
    md_path, json_path = write_report(report, tmp_path)
    assert md_path.name == "go_no_go_20261118.md" == report_file_name(report)
    assert json_path.name == "go_no_go_gonogo_20261118T120000Z.json"
    assert md_path.read_text(encoding="utf-8") == render_markdown(report)
    assert json.loads(json_path.read_text(encoding="utf-8"))["overall"] == "NO_GO"
    assert not list(tmp_path.glob(".tmp-*"))


def test_later_run_on_the_same_day_replaces_the_markdown_and_keeps_both_json(
    tmp_path: Path,
) -> None:
    first = evaluate(inputs())
    later_time = REPORT_TIME + dt.timedelta(hours=2)
    later = evaluate(inputs(failing_report_items(), created=later_time))
    write_report(first, tmp_path)
    md_path, _ = write_report(later, tmp_path)
    assert md_path.read_text(encoding="utf-8") == render_markdown(later)
    assert len(list(tmp_path.glob("go_no_go_*.json"))) == 2


def failing_report_items() -> list[LoadedArtifact]:
    return without(passing_set(), "monte_carlo_")


def test_older_run_never_replaces_a_newer_markdown(tmp_path: Path) -> None:
    newer = evaluate(inputs(created=REPORT_TIME + dt.timedelta(hours=2)))
    older = evaluate(inputs())
    md_path, _ = write_report(newer, tmp_path)
    with pytest.raises(ArtifactError, match="daha yeni"):
        write_report(older, tmp_path)
    assert md_path.read_text(encoding="utf-8") == render_markdown(newer)
    assert len(list(tmp_path.glob("go_no_go_*.json"))) == 1


def test_same_run_twice_is_refused(tmp_path: Path) -> None:
    report = evaluate(inputs())
    write_report(report, tmp_path)
    with pytest.raises(ArtifactError):
        write_report(report, tmp_path)


def test_foreign_markdown_of_the_same_name_is_never_replaced(tmp_path: Path) -> None:
    foreign = tmp_path / "go_no_go_20261118.md"
    foreign.write_text("# elle yazılmış not\n", encoding="utf-8")
    with pytest.raises(ArtifactError, match="yazmadığı"):
        write_report(evaluate(inputs()), tmp_path)
    assert foreign.read_text(encoding="utf-8") == "# elle yazılmış not\n"
    assert not list(tmp_path.glob("go_no_go_*.json"))


def test_symlinked_markdown_is_never_followed(tmp_path: Path) -> None:
    target = tmp_path / "elsewhere.md"
    target.write_text("hedef\n", encoding="utf-8")
    (tmp_path / "go_no_go_20261118.md").symlink_to(target)
    with pytest.raises(ArtifactError):
        write_report(evaluate(inputs()), tmp_path)
    assert target.read_text(encoding="utf-8") == "hedef\n"
