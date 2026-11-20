"""Attacks on the Go/No-Go reporter: inputs that must never produce PASS or GO.

CLAUDE.md invariant 1 unlocks `auto` mode only after a Go/No-Go PASS, so every test
here tries to sneak a PASS (or a misleading report) through, and one test checks the
opposite direction: a legitimate set of inputs passes G1-G4, G6, G7 (the gates do not
fail vacuously) and stays NO-GO only because of G5.
"""

import dataclasses
import datetime as dt
import hashlib
import math
from collections.abc import Callable, Sequence
from pathlib import Path

import pytest
from pydantic import ValidationError

from propbot.backtest.artifacts import (
    SYNTHETIC_TAG,
    AnyArtifact,
    ArtifactError,
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
    GATE_TITLES,
    STATUS_TEXT,
    GateResult,
    GoNoGoError,
    GoNoGoInputs,
    evaluate,
    gate_data,
    gate_edge,
    gate_monte_carlo,
    gate_parity,
    gate_robustness,
    gate_rules,
    render_markdown,
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
ORB = "ny_orb_fvg"
ASIAN = "asian_sweep"
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
MIN_OOS_TRADES = CRITERIA.min_oos_trades
EXPECTED_COSTS = {"US500.cash": CostModel(commission_per_unit_side=0.5, slippage_ticks=1.0)}
# config/backtest.yaml session.min_gap_minutes (30) in seconds (gate attack L).
MIN_GAP_SECONDS = 1800
# Since the user decision of 2026-11-19 (open finding 82) a decision run carries the
# planned risk (config/risk.yaml, 0.25) in its risk input and Go/No-Go is told the input's
# name; a parity run keeps the script's own risk (0.5) and has its own expected set.
RUN_PARAMS: dict[str, Scalar] = {"risk_pct": 0.25}
PARITY_PARAMS: dict[str, Scalar] = {"risk_pct": 0.5}


def rows_digest(sid: str) -> str:
    """Stand-in trade-row digest shared by a decision run and its list (gate attack P)."""
    return hashlib.sha256(sid.encode("utf-8")).hexdigest()


EXPECTED_INSTRUMENTS = {
    "US500.cash": Instrument("US500.cash", InstrumentKind.CFD, 0.01, 1.0, 0.01, 0.01)
}
DATA_GATES: tuple[Callable[[GoNoGoInputs], GateResult], ...] = (
    gate_data,
    gate_edge,
    gate_robustness,
    gate_monte_carlo,
    gate_parity,
)


# --- builders ------------------------------------------------------------------------------


def stats(n: int, mean: float | None, stdev: float | None, t: float | None = None) -> StatsModel:
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
    tf: int = 60,
    first: str = "2024-01-02T14:30:00Z",
    last: str = "2026-11-13T21:00:00Z",
    bars: int = 700_000,
    synthetic: bool = False,
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
        bar_tz="UTC",
        synthetic=synthetic,
    )


def loaded(artifact: AnyArtifact) -> LoadedArtifact:
    return LoadedArtifact(artifact_file_name(artifact), artifact)


def backtest(
    sid: str = ORB,
    *,
    label: str | None = None,
    created: dt.datetime = MADE,
    data: DataInfo | None = None,
    fill_model: str = "pessimistic",
    cost_free: bool = False,
    oos: StatsModel | None = None,
    is_stats: StatsModel | None = None,
    g3_ratio: float | str | None = "auto",
    g3_note: str | None = None,
    synthetic: bool = False,
) -> LoadedArtifact:
    oos_stats = oos if oos is not None else stats(300, 0.25, 1.4)
    is_model = is_stats if is_stats is not None else stats(300, 0.4, 1.4)
    if g3_ratio == "auto":
        ratio: float | None = (
            None
            if oos_stats.mean_r is None or is_model.mean_r is None or is_model.mean_r <= 0
            else oos_stats.mean_r / is_model.mean_r
        )
    else:
        assert not isinstance(g3_ratio, str)
        ratio = g3_ratio
    free = cost_free
    return loaded(
        build_artifact(
            BacktestArtifact,
            label=label or sid.replace("_", "-"),
            created=created,
            synthetic=synthetic,
            data=data or data_info(synthetic=synthetic),
            cost_free=free,
            strategy_id=sid,
            params=dict(RUN_PARAMS),
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
            fill_model=fill_model,
            cost_factor=1.0,
            initial_capital=100_000.0,
            oos_start_utc="2025-01-01T00:00:00Z",
            # Two strategies x 300 complete trades = the 600 rows of the Monte Carlo list.
            stats_all=stats(300, 0.3, 1.4),
            mae_total_r=-300.0,
            stats_is=is_model,
            stats_oos=oos_stats,
            g3_ratio=ratio,
            g3_note=g3_note,
            tables=TablesModel(weekday=(), hour=(), month=()),
            equity_curve=(),
            incomplete_entries=0,
            dropped_entries=0,
            # Recorded since gate attacks L and P (2026-11-19).
            min_gap_seconds=MIN_GAP_SECONDS,
            trades_digest=rows_digest(sid),
        )
    )


def validation(
    *,
    sha: str = SHA,
    file_name: str = DATA_FILE,
    clean: bool = True,
    created: dt.datetime = MADE,
    label: str = "us500",
) -> LoadedArtifact:
    return loaded(
        build_artifact(
            DataValidationArtifact,
            label=label,
            created=created,
            synthetic=False,
            data=data_info(sha=sha, file_name=file_name),
            clean=clean,
            report={"duplicates": 0},
        )
    )


def monte_carlo(
    *,
    venue: str = VENUE_NAME,
    p_pass: float = 0.7,
    p_breach: float = 0.1,
    p_undecided: float | None = None,
    shares: float | None = None,
    phase1: float | None = None,
    n_paths: int = 10_000,
    trades_file: str = "oos_trades.csv",
    created: dt.datetime = MADE,
    synthetic: bool = False,
) -> LoadedArtifact:
    """A run whose probabilities add up (pass + breach + undecided = 1) unless overridden,
    on the trade list of the legitimate decision runs (provenance)."""
    undecided = max(0.0, 1.0 - p_pass - p_breach) if p_undecided is None else p_undecided
    share = p_breach if shares is None else shares
    return loaded(
        build_artifact(
            MonteCarloArtifact,
            label="ftmo",
            created=created,
            synthetic=synthetic,
            data=None,
            inputs=MonteCarloInputs(
                trades_file=trades_file,
                n_trades=600,
                risk_pct=0.25,
                cost_r=0.05,
                n_paths=n_paths,
                max_days=1000,
                dd_days=250,
                seed=20261113,
                skip_prob=0.0,
                experimental_risk_steps=False,
                venue=venue,
                rules=RulesModel(
                    targets_pct=(10.0, 5.0),
                    daily_loss_limit_pct=5.0,
                    max_loss_pct=10.0,
                    min_trading_days=4,
                    min_trading_days_assumed=False,
                    # The profile's other rules, recorded since review B (2026-11-19).
                    max_loss_type=VENUE.max_loss_type.value,  # type: ignore[arg-type]
                    daily_loss_basis=VENUE.daily_loss_basis.value,  # type: ignore[arg-type]
                    daily_reset_tz=VENUE.daily_reset.tz,
                    daily_reset_time=VENUE.daily_reset.time,
                ),
                # Since review C and F: the pessimistic model and the decision runs' sums.
                intratrade="stop",
                sum_r=math.fsum([0.3 * 300, 0.3 * 300]),
                sum_mae_r=-600.0,
                # The decision runs' rows (gate attack P, 2026-11-19).
                trades_digests={sid: rows_digest(sid) for sid in (ORB, ASIAN)},
            ),
            p_pass_phase=(min(1.0, p_pass + 0.1) if phase1 is None else phase1, p_pass),
            p_breach=p_breach,
            breach_shares=(BreachShareModel(phase=1, reason="max_loss", probability=share),),
            p_undecided=undecided,
            time_to_pass=(None, None),
            drawdown=DrawdownModel(median=1.0, q95=2.0, q99=3.0, es95=2.5, es99=3.5),
            drawdown_histogram=HistogramModel(edges=(0.0, 1.0), counts=(n_paths,)),
            provenance=TradesProvenanceModel(
                synthetic=False,
                cost_free=False,
                fill_model="pessimistic",
                cost_factor=1.0,
                data_sha256=SHA,
                data_file=DATA_FILE,
                strategy_ids=tuple(sorted((ORB, ASIAN))),
                created_utc=min(created, MADE).strftime("%Y-%m-%dT%H:%M:%SZ"),
            ),
        )
    )


def parity(
    sid: str = ORB,
    *,
    rate: float | None = 0.99,
    created: dt.datetime = MADE,
    label: str | None = None,
) -> LoadedArtifact:
    return loaded(
        build_artifact(
            ParityArtifact,
            label=label or sid.replace("_", "-"),
            created=created,
            synthetic=False,
            # Since review E: the compared bars, parameters and instrument are recorded.
            data=data_info(),
            params=dict(PARITY_PARAMS),
            # The configured size grid (gate attack M, 2026-11-19).
            instrument=InstrumentInfo(
                symbol="US500.cash",
                kind="cfd",
                tick_size=0.01,
                point_value=1.0,
                qty_step=0.01,
                min_qty=0.01,
            ),
            strategy_id=sid,
            # The Python side's session gap and capital (gate attacks K, L, 2026-11-19).
            min_gap_seconds=MIN_GAP_SECONDS,
            initial_capital=100_000.0,
            # Counts that give the rate exactly (the model checks rate = matched / total).
            matched=0 if rate is None else round(rate * 10_000),
            python_only=0 if rate is None else 10_000 - round(rate * 10_000),
            tradingview_only=0,
            match_rate=None if rate is None else round(rate * 10_000) / 10_000,
            threshold=0.98,
            passed=rate is not None and rate >= 0.98,
            mismatches=(),
            sources=("list_of_trades.csv",),
        )
    )


def legit(strategies: Sequence[str] = (ORB, ASIAN)) -> list[LoadedArtifact]:
    items = [validation(), monte_carlo()]
    for sid in strategies:
        items += [backtest(sid), parity(sid)]
    return items


def swap(items: list[LoadedArtifact], prefix: str, new: LoadedArtifact) -> list[LoadedArtifact]:
    kept = [item for item in items if not item.file_name.startswith(prefix)]
    assert len(kept) == len(items) - 1
    return [*kept, new]


def case(artifacts: Sequence[LoadedArtifact] | None = None, **changes: object) -> GoNoGoInputs:
    base = GoNoGoInputs(
        artifacts=legit() if artifacts is None else artifacts,
        criteria=CRITERIA,
        venue_name=VENUE_NAME,
        venue=VENUE,
        strategy_ids=(ORB, ASIAN),
        planned_risk_pct=0.25,
        today=TODAY,
        created=REPORT_TIME,
        compliance_files=("2026-11-13-ftmo-kural-dogrulamasi.md",),
        expected_costs=EXPECTED_COSTS,
        expected_instruments=EXPECTED_INSTRUMENTS,
        expected_params={ORB: dict(RUN_PARAMS), ASIAN: dict(RUN_PARAMS)},
        expected_parity_params={ORB: dict(PARITY_PARAMS), ASIAN: dict(PARITY_PARAMS)},
        risk_params={ORB: "risk_pct", ASIAN: "risk_pct"},
        # Explicit since the default became None (unknown -> not evaluable).
        artifacts_truncated=False,
        expected_oos_start_utc="2025-01-01T00:00:00Z",
        # Explicit since gate attack L (2026-11-19): unknown -> no decision run.
        expected_min_gap_seconds=MIN_GAP_SECONDS,
    )
    return dataclasses.replace(base, **changes)  # type: ignore[arg-type]


def venue(**changes: object) -> VenueProfile:
    return VENUE.model_copy(update=changes)


ORB_BT = "backtest_ny-orb-fvg_"
ORB_PARITY = "parity_ny-orb-fvg_"


# --- the opposite direction: legitimate inputs are not failed vacuously --------------------


def test_legitimate_inputs_pass_every_gate_but_g5_and_stay_no_go_because_of_g5() -> None:
    report = evaluate(case())
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
    non_pass = [reason for reason in report.reasons if reason.startswith("G")]
    assert len(non_pass) == 1
    assert non_pass[0].startswith("G5 DEĞERLENDİRİLEMEDİ")


# --- strategy identity ---------------------------------------------------------------------


def test_backtest_of_another_strategy_sharing_the_label_does_not_count() -> None:
    items = [item for item in legit() if not item.file_name.startswith(ORB_BT)]
    items.append(backtest("mean_reversion", label="ny-orb-fvg"))
    for gate in (gate_data, gate_edge, gate_robustness):
        result = gate(case(items))
        assert result.gate.status == "NOT_EVALUABLE"
        assert "ny_orb_fvg: DEĞERLENDİRİLEMEDİ — backtest raporu yok" in result.gate.evidence


def test_artifacts_of_strategy_a_never_satisfy_strategy_b() -> None:
    items = legit(strategies=(ORB,))
    for gate in (gate_data, gate_edge, gate_robustness, gate_parity):
        result = gate(case(items, strategy_ids=(ORB, ASIAN)))
        assert result.gate.status == "NOT_EVALUABLE"
        assert "asian_sweep: DEĞERLENDİRİLEMEDİ" in result.gate.evidence


def test_parity_of_another_strategy_under_the_judged_label_does_not_count() -> None:
    items = [item for item in legit() if not item.file_name.startswith(ORB_PARITY)]
    items.append(parity("mean_reversion", label="ny-orb-fvg"))
    assert gate_parity(case(items)).gate.status == "NOT_EVALUABLE"


def test_duplicate_strategy_ids_do_not_double_count_a_strategy() -> None:
    items = swap(legit(), ORB_BT, backtest(oos=stats(300, -0.1, 1.4)))
    result = gate_edge(case(items, strategy_ids=(ASIAN, ASIAN, ORB, ASIAN)))
    assert result.gate.status == "FAIL"
    parts = result.gate.details["parts"]
    assert isinstance(parts, list)
    assert len(parts) == 2


def test_two_artifacts_with_one_file_name_are_refused() -> None:
    # Same kind, label and second: different strategies, one synthetic. Keyed by file name,
    # the synthetic one would vanish from the report's input list and its warning banner.
    real = backtest(ASIAN, label="base")
    fake = backtest(ORB, label="base", synthetic=True)
    assert real.file_name == fake.file_name
    items = [item for item in legit() if not item.file_name.startswith("backtest_")]
    with pytest.raises(GoNoGoError, match="birden fazla"):
        evaluate(case([*items, real, fake]))


def test_file_name_that_does_not_match_the_content_is_refused() -> None:
    real = backtest()
    disguised = LoadedArtifact("backtest_other_20261117T100000Z.json", real.artifact)
    with pytest.raises(GoNoGoError, match="uyuşmuyor"):
        evaluate(case(swap(legit(), ORB_BT, disguised)))


# --- G2 / G3 statistics --------------------------------------------------------------------


@pytest.mark.parametrize("n", [1, 2, MIN_OOS_TRADES - 1])
def test_tiny_oos_sample_with_a_huge_t_stat_never_passes(n: int) -> None:
    oos = stats(n, 1.0, 0.01) if n >= 2 else stats(1, 1.0, None, t=500.0)
    items = swap(legit(), ORB_BT, backtest(oos=oos))
    assert gate_edge(case(items)).gate.status != "PASS"
    assert gate_robustness(case(items)).gate.status != "PASS"


def test_oos_sample_at_the_minimum_size_can_pass() -> None:
    items = swap(legit(), ORB_BT, backtest(oos=stats(MIN_OOS_TRADES, 0.5, 1.0)))
    assert gate_edge(case(items)).gate.status == "PASS"


def test_tiny_is_sample_cannot_support_the_robustness_ratio() -> None:
    items = swap(legit(), ORB_BT, backtest(is_stats=stats(3, 0.4, 0.1)))
    assert gate_robustness(case(items)).gate.status != "PASS"


@pytest.mark.parametrize("mean", [0.0, -0.0, -1e-12, -0.3])
def test_zero_or_negative_expectancy_never_passes_g2(mean: float) -> None:
    items = swap(legit(), ORB_BT, backtest(oos=stats(300, mean, 1.4)))
    assert gate_edge(case(items)).gate.status == "FAIL"


def test_missing_mean_with_a_stored_t_stat_fails_g2() -> None:
    items = swap(legit(), ORB_BT, backtest(oos=stats(300, None, 1.4, t=9.0)))
    assert gate_edge(case(items)).gate.status == "FAIL"


def test_negative_t_stat_with_positive_mean_is_inconsistent() -> None:
    items = swap(legit(), ORB_BT, backtest(oos=stats(300, 0.25, 1.4, t=-3.0)))
    assert gate_edge(case(items)).gate.status == "FAIL"


def test_negative_is_and_negative_oos_do_not_make_a_positive_ratio_pass() -> None:
    items = swap(
        legit(),
        ORB_BT,
        backtest(oos=stats(300, -0.3, 1.4), is_stats=stats(300, -0.4, 1.4), g3_ratio=0.75),
    )
    assert gate_robustness(case(items)).gate.status == "FAIL"


def test_t_stat_rounded_up_onto_the_floor_does_not_pass() -> None:
    # Stored t = 2.0, but mean / (stdev / sqrt(n)) is 1.9999999998: within the consistency
    # tolerance, below the floor. The smaller of the two decides.
    oos = stats(100, 0.2, 1.0000000001, t=2.0)
    items = swap(legit(), ORB_BT, backtest(oos=oos))
    assert gate_edge(case(items)).gate.status == "FAIL"


def test_ratio_rounded_up_onto_the_floor_does_not_pass() -> None:
    items = swap(
        legit(),
        ORB_BT,
        backtest(oos=stats(300, 0.2, 1.0), is_stats=stats(300, 0.4000000001, 1.0), g3_ratio=0.5),
    )
    assert gate_robustness(case(items)).gate.status == "FAIL"


# --- comparison runs -----------------------------------------------------------------------


def test_only_cost_free_and_tv_ohlc_runs_never_pass() -> None:
    items = [item for item in legit() if not item.file_name.startswith(ORB_BT)]
    items += [
        backtest(label="orb-free", cost_free=True),
        backtest(label="orb-tv", fill_model="tv_ohlc"),
        backtest(label="orb-free-tv", fill_model="tv_ohlc", cost_free=True),
    ]
    for gate in (gate_data, gate_edge, gate_robustness):
        assert gate(case(items)).gate.status != "PASS"


def test_better_newer_cost_free_run_does_not_replace_a_failing_decision_run() -> None:
    failing = backtest(oos=stats(300, -0.1, 1.4))
    shiny = backtest(label="orb-free", cost_free=True, created=MADE + dt.timedelta(hours=1))
    items = [*swap(legit(), ORB_BT, failing), shiny]
    assert gate_edge(case(items)).gate.status == "FAIL"


# --- envelope and model guards (the model refuses; confirmed here) -------------------------


def test_synthetic_data_behind_a_non_synthetic_envelope_is_refused_by_the_model() -> None:
    real = backtest().artifact
    raw = real.model_dump()
    raw["data"] = data_info(synthetic=True).model_dump()
    with pytest.raises(ValidationError, match="synthetic"):
        BacktestArtifact.model_validate(raw)


def test_parity_passed_below_its_threshold_is_refused_by_the_model() -> None:
    raw = parity().artifact.model_dump()
    raw.update(match_rate=0.5, passed=True)
    with pytest.raises(ValidationError):
        ParityArtifact.model_validate(raw)


def test_parity_with_a_low_own_threshold_still_needs_the_criteria_rate() -> None:
    raw = parity().artifact.model_dump()
    # Counts move with the rate: since the model checks rate = matched / total, a lone
    # hand-edited rate is refused before G6 sees it (that is tested in test_provenance).
    raw.update(matched=9_000, python_only=1_000, match_rate=0.9, threshold=0.5, passed=True)
    lax = loaded(ParityArtifact.model_validate(raw))
    assert gate_parity(case(swap(legit(), ORB_PARITY, lax))).gate.status == "FAIL"


# --- G1 data -------------------------------------------------------------------------------


def test_validation_of_another_content_of_the_same_file_fails() -> None:
    items = swap(legit(), "data_validation_", validation(sha=OTHER_SHA))
    assert gate_data(case(items)).gate.status == "FAIL"


def test_validation_of_another_file_is_not_a_validation() -> None:
    items = swap(legit(), "data_validation_", validation(sha=OTHER_SHA, file_name="es_1m.csv"))
    assert gate_data(case(items)).gate.status == "NOT_EVALUABLE"


def test_two_year_span_made_of_two_bars_fails() -> None:
    sparse = data_info(bars=2, first="2024-01-02T14:30:00Z", last="2026-11-13T21:00:00Z")
    items = swap(legit(), ORB_BT, backtest(data=sparse))
    result = gate_data(case(items))
    assert result.gate.status == "FAIL"
    assert "bar sayısı" in result.gate.evidence


def test_five_minute_data_fails_g1() -> None:
    items = swap(legit(), ORB_BT, backtest(data=data_info(tf=300)))
    assert gate_data(case(items)).gate.status == "FAIL"


# --- G4 Monte Carlo ------------------------------------------------------------------------


def g4(mc: LoadedArtifact) -> str:
    return gate_monte_carlo(case(swap(legit(), "monte_carlo_", mc))).gate.status


def test_monte_carlo_for_another_venue_does_not_count() -> None:
    assert g4(monte_carlo(venue="funding_pips_2step")) == "NOT_EVALUABLE"


def test_monte_carlo_with_ten_paths_does_not_count() -> None:
    assert g4(monte_carlo(n_paths=10)) == "NOT_EVALUABLE"


@pytest.mark.parametrize("name", ["synthetic_trades.csv", "SENTETIK_islemler.csv"])
def test_monte_carlo_from_a_synthetic_trade_list_fails(name: str) -> None:
    assert g4(monte_carlo(trades_file=name)) == "FAIL"


def test_monte_carlo_marked_synthetic_fails() -> None:
    assert g4(monte_carlo(synthetic=True)) == "FAIL"


def test_hand_lowered_breach_probability_is_inconsistent() -> None:
    # Real run: pass 0.55, breach 0.40, undecided 0.05. Breach edited down to 0.10.
    assert g4(monte_carlo(p_pass=0.65, p_breach=0.10, p_undecided=0.05, shares=0.40)) == "FAIL"


def test_breach_shares_that_do_not_add_up_are_inconsistent() -> None:
    assert g4(monte_carlo(p_pass=0.7, p_breach=0.1, shares=0.3)) == "FAIL"


def test_all_phases_more_likely_than_phase_one_is_inconsistent() -> None:
    assert g4(monte_carlo(p_pass=0.7, phase1=0.5)) == "FAIL"


def test_g4_float_boundaries_are_strict() -> None:
    stricter_pass = CRITERIA.model_copy(update={"min_p_pass_all_phases": 0.6000000000000001})
    items = swap(legit(), "monte_carlo_", monte_carlo(p_pass=0.6, p_breach=0.2))
    assert gate_monte_carlo(case(items)).gate.status == "PASS"
    assert gate_monte_carlo(case(items, criteria=stricter_pass)).gate.status == "FAIL"
    above = swap(legit(), "monte_carlo_", monte_carlo(p_pass=0.6, p_breach=0.2000000000000001))
    assert gate_monte_carlo(case(above)).gate.status == "FAIL"


# --- G6 parity -----------------------------------------------------------------------------


def test_parity_rate_a_hair_below_the_floor_fails() -> None:
    raw = parity().artifact.model_dump()
    # 9,800 / 10,000 = 0.98: the hair is within the model's 1e-9 rate tolerance, so the
    # artifact is valid and only G6's strict comparison can refuse it.
    raw.update(matched=9_800, python_only=200, match_rate=0.98 - 1e-12, threshold=0.5, passed=True)
    hair = loaded(ParityArtifact.model_validate(raw))
    assert gate_parity(case(swap(legit(), ORB_PARITY, hair))).gate.status == "FAIL"


# --- G7 rules ------------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("verified", "expected"),
    [
        (TODAY + dt.timedelta(days=10), "NOT_EVALUABLE"),
        (TODAY - dt.timedelta(days=RULES_MAX_AGE_DAYS + 1), "FAIL"),
        (None, "FAIL"),
    ],
)
def test_rules_date_in_the_future_stale_or_missing_never_passes(
    verified: dt.date | None, expected: str
) -> None:
    result = gate_rules(case(venue=venue(rules_verified_at=verified)))
    assert result.gate.status == expected


def test_automation_not_allowed_fails_even_with_fresh_rules() -> None:
    result = gate_rules(case(venue=venue(automation_allowed=False)))
    assert result.gate.status == "FAIL"


# --- time ----------------------------------------------------------------------------------


def test_every_artifact_one_day_past_the_age_limit_blocks_every_data_gate() -> None:
    old = dt.datetime.combine(
        TODAY - dt.timedelta(days=MAX_ARTIFACT_AGE_DAYS + 1), dt.time(23, 59), tzinfo=dt.UTC
    )
    items = [validation(created=old), monte_carlo(created=old)]
    for sid in (ORB, ASIAN):
        items += [backtest(sid, created=old), parity(sid, created=old)]
    report = evaluate(case(items))
    for gate in report.gates:
        if gate.id in {"G1", "G2", "G3", "G4", "G6"}:
            assert gate.status == "NOT_EVALUABLE", gate.id
            assert "gün eski" in gate.evidence
    assert report.overall == "NO_GO"


def test_report_dated_before_its_artifacts_cannot_pass_anything() -> None:
    earlier = REPORT_TIME - dt.timedelta(days=2)
    report = evaluate(case(today=earlier.date(), created=earlier))
    for gate in report.gates:
        if gate.id in {"G1", "G2", "G3", "G4", "G6"}:
            assert gate.status == "NOT_EVALUABLE", gate.id
            assert "ileri tarihli" in gate.evidence


def test_today_that_disagrees_with_the_report_time_is_refused() -> None:
    with pytest.raises(GoNoGoError):
        evaluate(case(today=TODAY - dt.timedelta(days=3)))


def test_empty_reports_set_passes_no_data_gate() -> None:
    report = evaluate(case([]))
    for gate in report.gates:
        if gate.id in {"G1", "G2", "G3", "G4", "G6"}:
            assert gate.status == "NOT_EVALUABLE", gate.id
    assert report.overall == "NO_GO"


def test_criteria_stricter_than_the_numbers_fail_each_gate() -> None:
    stricter = GoNoGoCriteria(
        min_data_years=5.0,
        required_tf_seconds=30,
        min_oos_t_stat=10.0,
        min_oos_to_is_ratio=0.9,
        min_p_pass_all_phases=0.95,
        max_p_breach=0.01,
        min_parity_rate=0.999,
    )
    for gate in DATA_GATES:
        assert gate(case(criteria=stricter)).gate.status == "FAIL", gate.__name__


# --- Markdown injection --------------------------------------------------------------------


def gfm_cells(row: str) -> list[str]:
    """Split a table row the way GitHub does: a backslash escapes the next character."""
    cells, current, index = [], "", 0
    while index < len(row):
        char = row[index]
        if char == "\\" and index + 1 < len(row):
            current += row[index : index + 2]
            index += 2
            continue
        if char == "|":
            cells.append(current)
            current = ""
        else:
            current += char
        index += 1
    cells.append(current)
    return cells[1:-1]


PAYLOADS = (
    "x\n| G2 | Sahte | GEÇTİ | sahte |\n",
    "a\\| GEÇTİ \\| b",
    "<!-- gizle",
    "<script>alert(1)</script>",
    "![izle](http://example.invalid/x.png)",
    "`kod | kaçış`",
    "x | G4 | Sahte | GEÇTİ | y |",
)


def injected_report(payload: str) -> str:
    note_run = backtest(
        is_stats=stats(300, -0.1, 1.4),
        g3_note=payload,
        data=data_info(file_name=f"veri{payload}.csv"),
    )
    items = swap(legit(), ORB_BT, note_run)
    items = swap(items, "monte_carlo_", monte_carlo(venue=payload, trades_file=f"t{payload}.csv"))
    report = evaluate(case(items, venue_name=f"ftmo{payload}"))
    return render_markdown(report)


@pytest.mark.parametrize("payload", PAYLOADS)
def test_artifact_text_cannot_forge_or_break_the_gate_table(payload: str) -> None:
    text = injected_report(payload)
    rows = [line for line in text.splitlines() if line.startswith("| G")]
    assert [row.split("|")[1].strip() for row in rows] == [f"G{i}" for i in range(1, 8)]
    for row in rows:
        assert len(gfm_cells(row)) == 4, row
    statuses = {}
    for row in rows:
        gate_id, title, shown, _evidence = (cell.strip() for cell in gfm_cells(row))
        assert title == GATE_TITLES[gate_id]
        assert shown in STATUS_TEXT.values()
        statuses[gate_id] = shown
    assert statuses["G3"] == "KALDI"
    assert statuses["G4"] == "DEĞERLENDİRİLEMEDİ"


@pytest.mark.parametrize("payload", PAYLOADS)
def test_artifact_text_cannot_inject_html_or_images(payload: str) -> None:
    lines = injected_report(payload).splitlines()
    body = "\n".join(lines[1:])
    assert "<!--" not in body
    assert "<script" not in body
    assert "![" not in body


# --- files ---------------------------------------------------------------------------------


def test_report_of_another_label_in_the_same_second_is_refused(tmp_path: Path) -> None:
    first = evaluate(case())
    second = evaluate(case(label="ikinci"))
    md_path, _ = write_report(first, tmp_path)
    with pytest.raises(ArtifactError, match="daha yeni"):
        write_report(second, tmp_path)
    assert md_path.read_text(encoding="utf-8") == render_markdown(first)
    assert len(list(tmp_path.glob("go_no_go_*.json"))) == 1


def test_synthetic_input_used_by_a_gate_marks_the_written_report(tmp_path: Path) -> None:
    items = swap(legit(), ORB_PARITY, parity())
    items = swap(items, "monte_carlo_", monte_carlo(synthetic=True))
    report = evaluate(case(items))
    md_path, _ = write_report(report, tmp_path)
    assert report.synthetic
    assert SYNTHETIC_TAG in md_path.read_text(encoding="utf-8")
