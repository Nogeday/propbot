"""Provenance of the Go/No-Go evidence: which runs decide and where a trade list came from.

The Go/No-Go report unlocks `auto` mode (CLAUDE.md invariant 1). These tests close the
holes two reviews found: a newer comparison run hiding an older decision run under the
same label (A), a decision run whose cost factor is unknown or wrong (B), a Monte Carlo
run on a trade list of unknown, synthetic or cost-free origin (C), parity counts that do
not add up (D); E (the thresholds in config) is covered in test_backtest_config.
"""

import dataclasses
import datetime as dt
import hashlib
import json
import math
from collections.abc import Sequence
from pathlib import Path

import pytest
from hypothesis import given, settings
from hypothesis import strategies as st
from pydantic import ValidationError

from propbot.backtest import artifacts
from propbot.backtest.analytics import AnalyticsError, to_mc_trades, write_trades_csv
from propbot.backtest.artifacts import (
    COST_FREE_TAG,
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
    SpreadWindowInfo,
    StatsModel,
    TablesModel,
    TradesProvenanceModel,
    all_artifacts,
    artifact_file_name,
    backtest_artifact,
    build_artifact,
    latest_artifacts,
    load_artifact,
    monte_carlo_artifact,
    trade_rows_digest,
    write_artifact,
)
from propbot.backtest.config import GoNoGoCriteria
from propbot.backtest.gonogo import (
    EXPECTED_COSTS_MISSING,
    UNKNOWN_TRADES_SOURCE,
    GateResult,
    GoNoGoInputs,
    evaluate,
    gate_data,
    gate_edge,
    gate_monte_carlo,
    gate_parity,
    gate_robustness,
)
from propbot.backtest.model import CostModel, EngineConfig, EntryResult, ExitReason, SpreadWindow
from propbot.backtest.montecarlo import ChallengeRules, MonteCarloParams, simulate, summarize
from propbot.backtest.trades import (
    PROVENANCE_PREFIX,
    IntratradeModel,
    TradeListError,
    TradesProvenance,
    build_day_blocks,
    load_trades_csv,
    read_trades_csv,
)
from propbot.core.instrument import Instrument, InstrumentKind
from propbot.risk import load_venue_profile
from propbot.strategies.base import Side
from support.paths import CONFIG_DIR

TODAY = dt.date(2026, 11, 18)
REPORT_TIME = dt.datetime(2026, 11, 18, 12, 0, 0, tzinfo=dt.UTC)
MADE = dt.datetime(2026, 11, 17, 10, 0, 0, tzinfo=dt.UTC)
HOUR = dt.timedelta(hours=1)
SHA = "ab" * 32
OTHER_SHA = "cd" * 32
DATA_FILE = "us500_1m.csv"
VENUE_NAME = "ftmo_2step_100k"
ORB = "ny_orb_fvg"
ASIAN = "asian_sweep"
STRATEGIES = (ORB, ASIAN)
SYMBOL = "US500.cash"
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
EXPECTED = CostModel(commission_per_unit_side=0.5, slippage_ticks=1.0)
EXPECTED_COSTS = {SYMBOL: EXPECTED}
EXPECTED_INSTRUMENTS = {
    "US500.cash": Instrument("US500.cash", InstrumentKind.CFD, 0.01, 1.0, 0.01, 0.01)
}
GOOD_OOS = (300, 0.25, 1.4)
BAD_OOS = (300, -0.2, 1.4)


# --- builders ------------------------------------------------------------------------------


def stamp(moment: dt.datetime) -> str:
    return moment.strftime("%Y-%m-%dT%H:%M:%SZ")


def stats(n: int, mean: float, stdev: float) -> StatsModel:
    wins = n // 2
    return StatsModel(
        n=n,
        wins=wins,
        losses=n - wins,
        breakeven=0,
        win_rate=wins / n,
        mean_r=mean,
        median_r=mean,
        stdev_r=stdev,
        t_stat=mean / (stdev / math.sqrt(n)),
        profit_factor=None,
        total_r=mean * n,
        total_pnl=0.0,
        avg_win_r=None,
        avg_loss_r=None,
        max_drawdown_r=0.0,
        max_drawdown_pct=0.0,
        longest_losing_streak=0,
        incomplete=0,
    )


def data_info(sha: str = SHA) -> DataInfo:
    return DataInfo(
        file_name=DATA_FILE,
        sha256=sha,
        format="mt5",
        symbol=SYMBOL,
        tf_seconds=60,
        bars=700_000,
        first_utc="2024-01-02T14:30:00Z",
        last_utc="2026-11-13T21:00:00Z",
        bar_tz="UTC",
        synthetic=False,
    )


def loaded(artifact: AnyArtifact) -> LoadedArtifact:
    return LoadedArtifact(artifact_file_name(artifact), artifact)


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


def instrument_info(symbol: str) -> InstrumentInfo:
    # The configured size grid is recorded since gate attack M (2026-11-19).
    return InstrumentInfo(
        symbol=symbol, kind="cfd", tick_size=0.01, point_value=1.0, qty_step=0.01, min_qty=0.01
    )


def cost_info(
    commission: float = 0.5,
    slippage: float = 1.0,
    spread: float = 0.0,
    windows: tuple[SpreadWindowInfo, ...] = (),
) -> CostInfo:
    free = commission == slippage == spread == 0.0 and all(w.spread_points == 0 for w in windows)
    return CostInfo(
        commission_per_unit_side=commission,
        slippage_ticks=slippage,
        spread_points=spread,
        spread_windows=windows,
        cost_free=free,
        spread_tz="America/New_York",  # gate attack L (2026-11-19)
    )


def backtest(
    sid: str = ORB,
    *,
    label: str | None = None,
    created: dt.datetime = MADE,
    oos: tuple[int, float, float] = GOOD_OOS,
    fill_model: str = "pessimistic",
    cost_factor: float | None = 1.0,
    costs: CostInfo | None = None,
    symbol: str = SYMBOL,
    sha: str = SHA,
) -> LoadedArtifact:
    oos_stats = stats(*oos)
    is_stats = stats(300, 0.4, 1.4)
    assert oos_stats.mean_r is not None
    recorded = costs if costs is not None else cost_info()
    return loaded(
        build_artifact(
            BacktestArtifact,
            label=label or sid.replace("_", "-"),
            created=created,
            synthetic=False,
            data=data_info(sha),
            cost_free=recorded.cost_free,
            strategy_id=sid,
            params=dict(RUN_PARAMS),
            instrument=instrument_info(symbol),
            costs=recorded,
            fill_model=fill_model,
            cost_factor=cost_factor,
            initial_capital=100_000.0,
            oos_start_utc="2025-01-01T00:00:00Z",
            # Two strategies x 300 complete trades = the 600 rows of the Monte Carlo list.
            stats_all=stats(300, 0.3, 1.4),
            mae_total_r=-300.0,
            stats_is=is_stats,
            stats_oos=oos_stats,
            g3_ratio=None if oos_stats.mean_r <= 0 else oos_stats.mean_r / 0.4,
            g3_note=None if oos_stats.mean_r > 0 else "oran yok",
            tables=TablesModel(weekday=(), hour=(), month=()),
            equity_curve=(),
            incomplete_entries=0,
            dropped_entries=0,
            # Recorded since gate attacks L and P (2026-11-19).
            min_gap_seconds=MIN_GAP_SECONDS,
            trades_digest=rows_digest(sid),
        )
    )


def validation() -> LoadedArtifact:
    return loaded(
        build_artifact(
            DataValidationArtifact,
            label="us500",
            created=MADE,
            synthetic=False,
            data=data_info(),
            clean=True,
            report={},
        )
    )


def provenance_model(**changes: object) -> TradesProvenanceModel:
    base = TradesProvenanceModel(
        synthetic=False,
        cost_free=False,
        fill_model="pessimistic",
        cost_factor=1.0,
        data_sha256=SHA,
        data_file=DATA_FILE,
        strategy_ids=tuple(sorted(STRATEGIES)),
        created_utc=stamp(MADE),
    )
    return base.model_copy(update=changes)


def monte_carlo(
    provenance: TradesProvenanceModel | None,
    *,
    created: dt.datetime = MADE + HOUR,
    synthetic: bool = False,
    max_days: int = 1000,
    skip_prob: float = 0.0,
) -> LoadedArtifact:
    return loaded(
        build_artifact(
            MonteCarloArtifact,
            label="ftmo",
            created=created,
            synthetic=synthetic,
            data=None,
            inputs=MonteCarloInputs(
                trades_file="trades.csv",
                n_trades=600,
                risk_pct=0.25,
                cost_r=0.0,
                n_paths=10_000,
                max_days=max_days,
                dd_days=250,
                seed=20261113,
                skip_prob=skip_prob,
                experimental_risk_steps=False,
                venue=VENUE_NAME,
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
                trades_digests={sid: rows_digest(sid) for sid in STRATEGIES},
            ),
            p_pass_phase=(0.8, 0.7),
            p_breach=0.1,
            breach_shares=(BreachShareModel(phase=1, reason="max_loss", probability=0.1),),
            p_undecided=0.2,
            time_to_pass=(None, None),
            drawdown=DrawdownModel(median=1.0, q95=2.0, q99=3.0, es95=2.5, es99=3.5),
            drawdown_histogram=HistogramModel(edges=(0.0, 1.0), counts=(10_000,)),
            provenance=provenance,
        )
    )


def parity(sid: str) -> LoadedArtifact:
    return loaded(
        build_artifact(
            ParityArtifact,
            label=sid.replace("_", "-"),
            created=MADE,
            synthetic=False,
            # Since review E: the compared bars, parameters and instrument are recorded.
            data=data_info(),
            params=dict(PARITY_PARAMS),
            instrument=instrument_info(SYMBOL),
            # The Python side's session gap and capital (gate attacks K, L, 2026-11-19).
            min_gap_seconds=MIN_GAP_SECONDS,
            initial_capital=100_000.0,
            strategy_id=sid,
            matched=99,
            python_only=1,
            tradingview_only=0,
            match_rate=0.99,
            threshold=0.98,
            passed=True,
            mismatches=(),
            sources=(),
        )
    )


def legit(
    *, runs: Sequence[LoadedArtifact] | None = None, mc: LoadedArtifact | None = None
) -> list[LoadedArtifact]:
    items = [validation(), mc if mc is not None else monte_carlo(provenance_model())]
    items += [parity(sid) for sid in STRATEGIES]
    items += list(runs) if runs is not None else [backtest(sid) for sid in STRATEGIES]
    return items


def inputs(artifacts_: Sequence[LoadedArtifact], **changes: object) -> GoNoGoInputs:
    base = GoNoGoInputs(
        artifacts=artifacts_,
        criteria=CRITERIA,
        venue_name=VENUE_NAME,
        venue=VENUE,
        strategy_ids=STRATEGIES,
        planned_risk_pct=0.25,
        today=TODAY,
        created=REPORT_TIME,
        compliance_files=(),
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


def status(result: GateResult) -> str:
    return result.gate.status


def from_disk(tmp_path: Path, items: Sequence[LoadedArtifact]) -> GoNoGoInputs:
    """Write every artifact, read them back with all_artifacts, as the CLI must."""
    for item in items:
        write_artifact(item.artifact, tmp_path)
    scan = all_artifacts(tmp_path)
    assert scan.problems == ()
    return inputs(scan.artifacts, problems=scan.problems, artifacts_truncated=scan.truncated)


def test_the_legitimate_set_passes_g1_to_g4_and_g6() -> None:
    report = evaluate(inputs(legit()))
    assert {gate.id: gate.status for gate in report.gates} == {
        "G1": "PASS",
        "G2": "PASS",
        "G3": "PASS",
        "G4": "PASS",
        "G5": "NOT_EVALUABLE",
        "G6": "PASS",
        "G7": "PASS",
    }


# --- A: hidden runs ------------------------------------------------------------------------


def test_cherry_pick_sequence_ends_not_pass_with_all_artifacts(tmp_path: Path) -> None:
    # T1: good decision run under label a. T2: bad decision run under label b (newer).
    # T3: comparison run under label b (newest): newest-per-label would show T1 and T3.
    t1 = backtest(label="a", created=MADE - 2 * HOUR)
    t2 = backtest(label="b", created=MADE - HOUR, oos=BAD_OOS)
    t3 = backtest(label="b", created=MADE, fill_model="tv_ohlc")
    items = legit(runs=[t1, t2, t3, backtest(ASIAN)])
    for item in items:
        write_artifact(item.artifact, tmp_path)

    hidden = {item.file_name for item in latest_artifacts(tmp_path).artifacts}
    assert t2.file_name not in hidden  # why the report must not use latest_artifacts

    case = from_disk(tmp_path / "again", items)
    result = gate_edge(case)
    assert status(result) == "FAIL"
    assert t2.file_name in result.gate.evidence
    assert evaluate(case).overall == "NO_GO"
    assert status(gate_robustness(case)) == "FAIL"


@pytest.mark.parametrize(
    "comparison",
    [
        {"fill_model": "tv_ohlc"},
        {"costs": cost_info(0.0, 0.0)},
        {"cost_factor": 1.5},
        {"cost_factor": 0.01},
    ],
)
def test_no_newer_comparison_run_hides_the_newest_decision_run(
    tmp_path: Path, comparison: dict[str, object]
) -> None:
    good_old = backtest(label="a", created=MADE - 2 * HOUR)
    bad = backtest(label="b", created=MADE - HOUR, oos=BAD_OOS)
    shiny = backtest(label="b", created=MADE, oos=(300, 0.6, 1.0), **comparison)  # type: ignore[arg-type]
    case = from_disk(tmp_path, legit(runs=[good_old, bad, shiny, backtest(ASIAN)]))
    assert status(gate_edge(case)) == "FAIL"


def test_shared_label_across_strategies_cannot_hide_a_bad_run(tmp_path: Path) -> None:
    orb_good = backtest(ORB, label="orb", created=MADE - 2 * HOUR)
    orb_bad = backtest(ORB, label="shared", created=MADE - HOUR, oos=BAD_OOS)
    asian = backtest(ASIAN, label="shared", created=MADE)
    items = legit(runs=[orb_good, orb_bad, asian])
    for item in items:
        write_artifact(item.artifact, tmp_path)
    assert orb_bad.file_name not in {i.file_name for i in latest_artifacts(tmp_path).artifacts}

    result = gate_edge(from_disk(tmp_path / "again", items))
    assert status(result) == "FAIL"
    assert "ny_orb_fvg: KALDI" in result.gate.evidence
    assert "asian_sweep: GEÇTİ" in result.gate.evidence


def test_shared_label_with_a_good_newer_run_of_each_strategy_passes(tmp_path: Path) -> None:
    items = legit(
        runs=[
            backtest(ORB, label="shared", created=MADE - HOUR, oos=BAD_OOS),
            backtest(ORB, label="orb2", created=MADE),
            backtest(ASIAN, label="shared", created=MADE),
        ]
    )
    assert status(gate_edge(from_disk(tmp_path, items))) == "PASS"


def test_an_older_better_run_never_replaces_a_newer_unconfirmed_one() -> None:
    newer_unknown = backtest(label="new", created=MADE + HOUR, oos=BAD_OOS, cost_factor=None)
    case = inputs([*legit(), newer_unknown])
    for gate in (gate_data, gate_edge, gate_robustness):
        result = gate(case)
        assert status(result) == "NOT_EVALUABLE"
        assert newer_unknown.file_name in result.gate.evidence
        assert "daha eski bir koşuya geçilmez" in result.gate.evidence


def test_all_artifacts_returns_every_readable_artifact_and_the_problems(tmp_path: Path) -> None:
    runs = [backtest(label="x", created=MADE - k * HOUR) for k in range(3)]
    for item in runs:
        write_artifact(item.artifact, tmp_path)
    broken = tmp_path / "backtest_x_20261117T120000Z.json"
    broken.write_text("{", encoding="utf-8")
    renamed = tmp_path / "backtest_y_20261117T130000Z.json"
    renamed.write_bytes((tmp_path / runs[0].file_name).read_bytes())
    (tmp_path / "notes.txt").write_text("x", encoding="utf-8")

    scan = all_artifacts(tmp_path)

    assert [item.file_name for item in scan.artifacts] == [item.file_name for item in runs]
    assert sorted(p.file_name for p in scan.problems) == [broken.name, renamed.name]
    assert (scan.truncated, scan.examined, scan.total) == (False, 5, 5)
    assert len(latest_artifacts(tmp_path).artifacts) == 1


def test_all_artifacts_of_a_missing_directory_is_empty(tmp_path: Path) -> None:
    scan = all_artifacts(tmp_path / "absent")
    assert (scan.artifacts, scan.problems, scan.truncated) == ((), (), False)


def test_all_artifacts_does_not_follow_symlinks(tmp_path: Path) -> None:
    outside = write_artifact(backtest().artifact, tmp_path / "outside")
    reports = tmp_path / "reports"
    reports.mkdir()
    (reports / outside.name).symlink_to(outside)
    scan = all_artifacts(reports)
    assert scan.artifacts == ()
    assert "sembolik" in scan.problems[0].message


def test_hitting_the_examined_files_bound_blocks_every_data_gate(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    items = legit()
    for item in items:
        write_artifact(item.artifact, tmp_path)
    monkeypatch.setattr(artifacts, "MAX_EXAMINED_FILES", len(items) - 1)

    scan = all_artifacts(tmp_path)
    assert scan.truncated
    assert (scan.examined, scan.total) == (len(items) - 1, len(items))

    report = evaluate(inputs(scan.artifacts, artifacts_truncated=scan.truncated))
    for gate in report.gates:
        if gate.id in {"G1", "G2", "G3", "G4", "G6"}:
            assert gate.status == "NOT_EVALUABLE", gate.id
            assert "rapor dizininde" in gate.evidence
    # The flag alone blocks, whatever was read.
    case = inputs(legit(), artifacts_truncated=True)
    assert status(gate_parity(case)) == "NOT_EVALUABLE"
    assert status(gate_parity(inputs(legit()))) == "PASS"


# --- B: cost factor and expected costs -----------------------------------------------------


@pytest.mark.parametrize(
    ("factor", "g2", "g13"),
    [
        (None, "NOT_EVALUABLE", "NOT_EVALUABLE"),
        (0.99, "FAIL", "NOT_EVALUABLE"),
        (1.0, "PASS", "PASS"),
        (1.5, "FAIL", "NOT_EVALUABLE"),
    ],
)
def test_only_a_run_at_cost_factor_exactly_one_is_a_decision_run(
    factor: float | None, g2: str, g13: str
) -> None:
    run = backtest(cost_factor=factor)
    items = [*legit(runs=[backtest(ASIAN)]), run]
    case = inputs(items)
    result = gate_edge(case)
    assert status(result) == g2
    assert status(gate_data(case)) == g13
    assert status(gate_robustness(case)) == g13
    if factor is None:
        assert "maliyet çarpanı kaydedilmemiş" in result.gate.evidence
    elif factor != 1.0:
        assert "1,0 değil" in result.gate.evidence


def test_a_good_older_decision_run_is_used_past_a_newer_sensitivity_run() -> None:
    stress = backtest(label="stress", created=MADE + HOUR, cost_factor=2.0, oos=BAD_OOS)
    assert status(gate_edge(inputs([*legit(), stress]))) == "PASS"


def test_backtest_artifact_builder_records_the_cost_factor() -> None:
    config = EngineConfig(
        instrument=Instrument(
            symbol=SYMBOL,
            kind=InstrumentKind.CFD,
            tick_size=0.01,
            point_value=1.0,
            qty_step=0.01,
            min_qty=0.01,
        ),
        costs=EXPECTED.scaled(1.5),
    )
    common: dict[str, object] = {
        "label": "orb",
        "created": MADE,
        "strategy_id": ORB,
        "params": {},
        "config": config,
        "entries": entries(4),
        "oos_start_ms": 0,
        "dropped_entries": 0,
        "data": None,
        "synthetic": False,
    }
    assert backtest_artifact(**common).cost_factor is None  # type: ignore[arg-type]
    assert backtest_artifact(**common, cost_factor=1.5).cost_factor == 1.5  # type: ignore[arg-type]
    with pytest.raises(ArtifactError):
        backtest_artifact(**common, cost_factor=-1.0)  # type: ignore[arg-type]


def test_an_old_backtest_file_without_the_cost_factor_loads_as_unknown(tmp_path: Path) -> None:
    path = write_artifact(backtest().artifact, tmp_path)
    raw = json.loads(path.read_text(encoding="utf-8"))
    del raw["cost_factor"]
    path.write_text(json.dumps(raw), encoding="utf-8")
    again = load_artifact(path)
    assert isinstance(again, BacktestArtifact)
    assert again.cost_factor is None


def test_without_expected_costs_g1_to_g4_are_not_evaluable_with_one_sentence() -> None:
    report = evaluate(inputs(legit(), expected_costs=None))
    for gate in report.gates:
        if gate.id in {"G1", "G2", "G3", "G4"}:
            assert gate.status == "NOT_EVALUABLE", gate.id
            assert EXPECTED_COSTS_MISSING in gate.evidence or gate.id == "G4", gate.id
    g4 = next(gate for gate in report.gates if gate.id == "G4")
    assert "karar koşusu olmayan strateji" in g4.evidence


def test_an_instrument_without_an_expected_profile_is_not_a_decision_run() -> None:
    case = inputs(legit(), expected_costs={"MES": EXPECTED})
    result = gate_edge(case)
    assert status(result) == "NOT_EVALUABLE"
    assert f"'{SYMBOL}' enstrümanı için beklenen maliyet profili yok" in result.gate.evidence


WINDOW = SpreadWindowInfo(start_minute=565, end_minute=580, spread_points=1.2)


@pytest.mark.parametrize(
    ("recorded", "field"),
    [
        (cost_info(commission=0.25), "commission_per_unit_side"),
        (cost_info(commission=0.5 + 1e-15), "commission_per_unit_side"),
        (cost_info(slippage=0.5), "slippage_ticks"),
        (cost_info(spread=0.1), "spread_points"),
        (cost_info(windows=(WINDOW,)), "spread_windows"),
    ],
)
def test_recorded_costs_must_equal_the_configured_ones_exactly(
    recorded: CostInfo, field: str
) -> None:
    run = backtest(ORB, costs=recorded)
    result = gate_edge(inputs(legit(runs=[run, backtest(ASIAN)])))
    assert status(result) == "NOT_EVALUABLE"
    assert field in result.gate.evidence


def test_matching_spread_windows_are_a_decision_run() -> None:
    expected = CostModel(
        commission_per_unit_side=0.5,
        slippage_ticks=1.0,
        spread_windows=(SpreadWindow(start_minute=565, end_minute=580, spread_points=1.2),),
    )
    runs = [backtest(sid, costs=cost_info(windows=(WINDOW,))) for sid in STRATEGIES]
    case = inputs(legit(runs=runs), expected_costs={SYMBOL: expected})
    assert status(gate_edge(case)) == "PASS"


# --- C: trade-list provenance --------------------------------------------------------------


def provenance(**changes: object) -> TradesProvenance:
    base = TradesProvenance(
        synthetic=False,
        cost_free=False,
        fill_model="pessimistic",
        cost_factor=1.0,
        data_sha256=SHA,
        data_file=DATA_FILE,
        strategy_ids=tuple(sorted(STRATEGIES)),
        created_utc=stamp(MADE),
    )
    return dataclasses.replace(base, **changes)


def test_provenance_line_has_the_documented_shape() -> None:
    assert provenance().to_line() == (
        "#propbot-trades v=1 synthetic=false cost_free=false fill_model=pessimistic "
        f"cost_factor=1.0 data_sha256={SHA} data_file=us500_1m.csv "
        "strategy_ids=asian_sweep,ny_orb_fvg created_utc=2026-11-17T10:00:00Z"
    )


@pytest.mark.parametrize(
    "changes",
    [
        {"synthetic": 1},
        {"fill_model": "optimistic"},
        {"cost_factor": -1.0},
        {"cost_factor": math.inf},
        {"cost_factor": 1},
        {"data_sha256": "AB" * 32},
        {"data_sha256": "ab"},
        {"data_file": ""},
        {"data_file": "x" * 257},
        {"data_file": "\ud800"},
        {"strategy_ids": ()},
        {"strategy_ids": ("b", "a")},
        {"strategy_ids": ("a", "a")},
        {"strategy_ids": ("",)},
        {"strategy_ids": (" a",)},
        {"created_utc": "2026-11-17 10:00:00Z"},
        {"created_utc": "2026-02-30T10:00:00Z"},
    ],
)
def test_invalid_provenance_values_are_refused(changes: dict[str, object]) -> None:
    with pytest.raises(ValueError, match=r"olmalı|olamaz|geçersiz|bilinmeyen"):
        provenance(**changes)


odd_text = st.text(alphabet=st.characters(blacklist_categories=("Cs",)), min_size=1, max_size=40)
# The CSV reader strips fields, so a strategy id is never padded (the provenance refuses one).
odd_ids = odd_text.filter(lambda text: text == text.strip())


@settings(max_examples=150, deadline=None)
@given(
    synthetic=st.booleans(),
    cost_free=st.booleans(),
    fill_model=st.sampled_from(["pessimistic", "tv_ohlc"]),
    cost_factor=st.floats(min_value=0.0, max_value=1e6, allow_nan=False),
    sha=st.one_of(st.just(""), st.text(alphabet="0123456789abcdef", min_size=64, max_size=64)),
    data_file=odd_text,
    ids=st.sets(odd_ids, min_size=1, max_size=4),
    moment=st.datetimes(min_value=dt.datetime(2000, 1, 1), max_value=dt.datetime(2099, 12, 31)),
)
def test_provenance_line_round_trips_exactly(
    synthetic: bool,
    cost_free: bool,
    fill_model: str,
    cost_factor: float,
    sha: str,
    data_file: str,
    ids: set[str],
    moment: dt.datetime,
) -> None:
    original = TradesProvenance(
        synthetic=synthetic,
        cost_free=cost_free,
        fill_model=fill_model,
        cost_factor=cost_factor,
        data_sha256=sha,
        data_file=data_file,
        strategy_ids=tuple(sorted(ids)),
        created_utc=moment.strftime("%Y-%m-%dT%H:%M:%SZ"),
    )
    line = original.to_line()
    assert "\n" not in line
    assert "\r" not in line
    assert TradesProvenance.from_line(line) == original


@settings(max_examples=60, deadline=None)
@given(data_file=odd_text, ids=st.sets(odd_ids, min_size=1, max_size=3))
def test_provenance_survives_the_csv_file_with_odd_names(
    tmp_path_factory: pytest.TempPathFactory, data_file: str, ids: set[str]
) -> None:
    path = tmp_path_factory.mktemp("odd") / "trades.csv"
    names = tuple(sorted(ids))
    rows = [entry(i, 1.0, sid=names[i % len(names)]) for i in range(len(names) * 2)]
    original = provenance(data_file=data_file, strategy_ids=names)
    write_trades_csv(rows, path, provenance=original)
    trades, again = read_trades_csv(path)
    assert again == original
    assert trades == to_mc_trades(rows)
    assert load_trades_csv(path) == trades


def entry(seq: int, r: float, *, sid: str = ORB) -> EntryResult:
    start = 1_767_623_400_000 + seq * 86_400_000  # 2026-01-05T14:30:00Z + seq days
    return EntryResult(
        seq=seq,
        strategy_id=sid,
        entry_id="L",
        setup="",
        side=Side.LONG,
        qty=1.0,
        entry_ts_ms=start,
        exit_ts_ms=start + 900_000,
        entry_price=5000.0,
        pnl=r * 250.0,
        risk_amount=250.0,
        r=r,
        mae_r=min(r, 0.0),
        reasons=(ExitReason.TARGET if r > 0 else ExitReason.STOP,),
        complete=True,
    )


def entries(n: int) -> list[EntryResult]:
    return [entry(i, 2.0 if i % 10 < 7 else -1.0, sid=STRATEGIES[i % 2]) for i in range(n)]


def test_old_files_without_the_line_still_load_with_no_provenance(tmp_path: Path) -> None:
    path = tmp_path / "trades.csv"
    write_trades_csv(entries(4), path)
    trades, found = read_trades_csv(path)
    assert found is None
    assert trades == load_trades_csv(path) == to_mc_trades(entries(4))


def test_the_line_is_skipped_by_the_old_loader_and_row_numbers_stay_true(tmp_path: Path) -> None:
    path = tmp_path / "trades.csv"
    write_trades_csv(entries(4), path, provenance=provenance())
    text = path.read_text(encoding="utf-8")
    assert text.startswith(PROVENANCE_PREFIX + " ")
    assert load_trades_csv(path) == to_mc_trades(entries(4))
    lines = text.splitlines()
    lines[3] = lines[3].replace(",2.0,", ",abc,")
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    with pytest.raises(TradeListError, match="satır 4"):
        load_trades_csv(path)


def test_a_bom_and_crlf_endings_are_accepted(tmp_path: Path) -> None:
    path = tmp_path / "trades.csv"
    write_trades_csv(entries(4), path, provenance=provenance())
    text = path.read_text(encoding="utf-8").replace("\n", "\r\n")
    path.write_bytes(b"\xef\xbb\xbf" + text.encode("utf-8"))
    assert read_trades_csv(path)[1] == provenance()


def good_line() -> str:
    return provenance().to_line()


MALFORMED = [
    good_line() + " ",
    good_line().replace(" v=1 ", "  v=1 "),
    good_line().replace("v=1", "v=2"),
    good_line().replace("synthetic=false", "synthetic=False"),
    good_line().replace("synthetic=false", "synthetic=0"),
    good_line().replace("cost_factor=1.0", "cost_factor=1.00"),
    good_line().replace("cost_factor=1.0", "cost_factor=1"),
    good_line().replace("cost_factor=1.0", "cost_factor=nan"),
    good_line().replace("cost_factor=1.0", "cost_factor=-1.0"),
    good_line().replace("fill_model=pessimistic", "fill_model=optimistic"),
    good_line().replace(f"data_sha256={SHA}", "data_sha256=" + "AB" * 32),
    good_line().replace("data_file=us500_1m.csv", "data_file=us500%5f1m.csv"),
    good_line().replace("data_file=us500_1m.csv", "data_file=%2"),
    good_line().replace("data_file=us500_1m.csv", "data_file=%FF"),
    good_line().replace("data_file=us500_1m.csv", "data_file="),
    good_line().replace("asian_sweep,ny_orb_fvg", "ny_orb_fvg,asian_sweep"),
    good_line().replace("asian_sweep,ny_orb_fvg", "asian_sweep,,ny_orb_fvg"),
    good_line().replace("strategy_ids=asian_sweep,ny_orb_fvg", "strategy_ids="),
    good_line().replace("created_utc=2026-11-17T10:00:00Z", "created_utc=2026-11-17T10:00Z"),
    good_line().replace(" synthetic=false cost_free=false", " cost_free=false synthetic=false"),
    good_line().replace(" cost_free=false", ""),
    good_line() + " extra=1",
    good_line().replace("#propbot-trades", "#propbot-tradesX"),
    "#propbot-trades",
]


@pytest.mark.parametrize("line", MALFORMED)
def test_a_malformed_or_tampered_line_is_an_error_not_no_provenance(
    tmp_path: Path, line: str
) -> None:
    path = tmp_path / "trades.csv"
    write_trades_csv(entries(4), path)
    path.write_text(line + "\n" + path.read_text(encoding="utf-8"), encoding="utf-8")
    with pytest.raises(TradeListError, match="köken satırı"):
        read_trades_csv(path)
    with pytest.raises(TradeListError, match="köken satırı"):
        load_trades_csv(path)


def test_a_provenance_line_without_a_line_end_is_malformed(tmp_path: Path) -> None:
    path = tmp_path / "trades.csv"
    path.write_text(good_line(), encoding="utf-8")
    with pytest.raises(TradeListError, match="satır sonu"):
        read_trades_csv(path)


@pytest.mark.parametrize(
    "text",
    [
        "# a comment\nentry_time,exit_time,r\n2026-01-05T14:36:00Z,2026-01-05T15:10:00Z,1.0\n",
        "entry_time,exit_time,r\n# a comment\n2026-01-05T14:36:00Z,2026-01-05T15:10:00Z,1.0\n",
        "entry_time,exit_time,r\n" + good_line() + "\n",
    ],
)
def test_other_hash_lines_stay_errors_as_before(tmp_path: Path, text: str) -> None:
    path = tmp_path / "trades.csv"
    path.write_text(text, encoding="utf-8")
    with pytest.raises(TradeListError):
        read_trades_csv(path)


def test_a_row_of_a_strategy_the_line_does_not_name_is_refused(tmp_path: Path) -> None:
    path = tmp_path / "trades.csv"
    write_trades_csv(entries(4), path, provenance=provenance())
    text = path.read_text(encoding="utf-8").replace(",asian_sweep,", ",silver_bullet,", 1)
    path.write_text(text, encoding="utf-8")
    with pytest.raises(TradeListError, match="silver_bullet"):
        read_trades_csv(path)


def test_writer_refuses_a_provenance_that_does_not_name_the_strategies(tmp_path: Path) -> None:
    path = tmp_path / "trades.csv"
    with pytest.raises(AnalyticsError, match="asian_sweep"):
        write_trades_csv(entries(4), path, provenance=provenance(strategy_ids=(ORB,)))
    assert not path.exists()


def test_monte_carlo_artifact_with_a_synthetic_list_must_be_synthetic() -> None:
    raw = monte_carlo(provenance_model()).artifact.model_dump()
    raw["provenance"] = provenance_model(synthetic=True).model_dump()
    with pytest.raises(ValidationError, match="synthetic"):
        MonteCarloArtifact.model_validate(raw)


def test_provenance_model_needs_sorted_unique_ids() -> None:
    with pytest.raises(ValidationError, match="sıralı"):
        provenance_model(strategy_ids=(ORB, ASIAN)).model_validate(
            provenance_model(strategy_ids=(ORB, ASIAN)).model_dump()
        )


def test_an_old_monte_carlo_file_without_provenance_loads(tmp_path: Path) -> None:
    path = write_artifact(monte_carlo(provenance_model()).artifact, tmp_path)
    raw = json.loads(path.read_text(encoding="utf-8"))
    del raw["provenance"]
    path.write_text(json.dumps(raw), encoding="utf-8")
    again = load_artifact(path)
    assert isinstance(again, MonteCarloArtifact)
    assert again.provenance is None


def g4(provenance_: TradesProvenanceModel | None, **changes: object) -> GateResult:
    mc = monte_carlo(provenance_)
    return gate_monte_carlo(inputs(legit(mc=mc), **changes))


def test_g4_passes_on_the_decision_runs_list_and_shows_horizon_and_skip() -> None:
    result = g4(provenance_model())
    assert status(result) == "PASS"
    assert "ufuk 1.000 piyasa günü" in result.gate.evidence
    assert "işlem atlama olasılığı 0" in result.gate.evidence
    # Since gate attack N (2026-11-19) the two are judged, no longer "not checked".
    assert "firma kuralı değil" in result.gate.evidence
    names = {item.file_name for item in result.used}
    assert backtest(ORB).file_name in names  # the decision runs it was chained to


def test_g4_without_provenance_is_not_evaluable_with_the_sentence() -> None:
    result = g4(None)
    assert status(result) == "NOT_EVALUABLE"
    assert UNKNOWN_TRADES_SOURCE in result.gate.evidence


def test_g4_synthetic_list_fails_with_the_word() -> None:
    mc = monte_carlo(provenance_model(synthetic=True), synthetic=True)
    result = gate_monte_carlo(inputs(legit(mc=mc)))
    assert status(result) == "FAIL"
    assert "SENTETİK" in result.gate.evidence


@pytest.mark.parametrize(
    ("changes", "words"),
    [
        ({"cost_free": True}, COST_FREE_TAG),
        ({"fill_model": "tv_ohlc"}, "'tv_ohlc' dolum modeliyle"),
        ({"cost_factor": 1.5}, "maliyet çarpanı 1,5"),
        ({"cost_factor": 0.99}, "maliyet çarpanı 0,99"),
        ({"data_sha256": OTHER_SHA}, "karar koşularının verisiyle aynı değil"),
        ({"data_sha256": ""}, "sha256'sı yok"),
        ({"strategy_ids": (ORB,)}, "listede olmayan: asian_sweep"),
        ({"strategy_ids": tuple(sorted((*STRATEGIES, "zz")))}, "değerlendirilmeyen: zz"),
        ({"created_utc": stamp(MADE - HOUR)}, "en yeni karar koşusundan"),
        ({"created_utc": stamp(MADE + 2 * HOUR)}, "Monte Carlo koşusundan sonra"),
    ],
)
def test_g4_provenance_defects_are_not_evaluable_with_the_reason(
    changes: dict[str, object], words: str
) -> None:
    result = g4(provenance_model(**changes))
    assert status(result) == "NOT_EVALUABLE"
    assert words in result.gate.evidence


def test_g4_list_of_runs_that_are_not_decision_runs_is_not_evaluable() -> None:
    runs = [backtest(ORB, cost_factor=None), backtest(ASIAN)]
    result = gate_monte_carlo(inputs(legit(runs=runs)))
    assert status(result) == "NOT_EVALUABLE"
    assert "karar koşusu olmayan strateji: ny_orb_fvg" in result.gate.evidence


def test_g4_list_of_decision_runs_on_other_data_is_not_evaluable() -> None:
    runs = [backtest(ORB, sha=OTHER_SHA), backtest(ASIAN)]
    result = gate_monte_carlo(inputs(legit(runs=runs)))
    assert status(result) == "NOT_EVALUABLE"
    assert "karar koşularının verisiyle aynı değil" in result.gate.evidence


def test_g4_empty_strategy_list_is_not_evaluable() -> None:
    assert status(g4(provenance_model(), strategy_ids=())) == "NOT_EVALUABLE"


def test_g4_horizon_and_skip_probability_are_shown_and_judged() -> None:
    # Stricter since gate attack N (2026-11-19): shown AND judged. A chosen horizon or
    # skip probability moves both probabilities, so only the defaults (1,000 market days,
    # no skipped trade) can pass G4; this run used to PASS here.
    result = gate_monte_carlo(
        inputs(legit(mc=monte_carlo(provenance_model(), max_days=40, skip_prob=0.35)))
    )
    assert status(result) == "NOT_EVALUABLE"
    assert "ufuk 40 piyasa günü" in result.gate.evidence
    assert "işlem atlama olasılığı 0,35" in result.gate.evidence


# --- C end to end: entries -> CSV -> read -> simulate -> artifact -> G4 --------------------


def end_to_end(
    tmp_path: Path, prov: TradesProvenance | None, *, synthetic: bool = False
) -> GateResult:
    path = tmp_path / "trades.csv"
    # 600 complete trades, 300 per strategy: the decision runs' stats_all (G4 compares).
    write_trades_csv(entries(600), path, provenance=prov)
    trades, read_back = read_trades_csv(path)
    assert read_back == prov
    rules = ChallengeRules.from_venue(VENUE)
    # The reproducible run (default seed, paths, horizon): since gate attack N
    # (2026-11-19) G4 passes on no other.
    params = MonteCarloParams(risk_pct=0.25)
    blocks = build_day_blocks(
        trades, zone=VENUE.daily_reset.zone, reset=VENUE.daily_reset.time_of_day
    )
    outcomes = simulate(blocks, rules, params)
    mc = monte_carlo_artifact(
        summarize(outcomes),
        outcomes,
        label="ftmo",
        created=MADE + HOUR,
        params=params,
        rules=rules,
        trades_file=str(path),
        n_trades=len(trades),
        cost_r=0.0,
        venue=VENUE_NAME,
        synthetic=synthetic,
        provenance=read_back,
        # Since review B, C and F (2026-11-19) the run records what G4 compares with.
        venue_profile=VENUE,
        intratrade=IntratradeModel.STOP,
        trades=trades,
    )
    written = write_artifact(mc, tmp_path / "reports")
    again = load_artifact(written)
    # The decision runs these entries came from: their totals are the list's sums.
    runs = []
    for sid in STRATEGIES:
        own = [e for e in entries(600) if e.strategy_id == sid]
        item = backtest(sid).artifact
        assert isinstance(item, BacktestArtifact)
        runs.append(
            loaded(
                item.model_copy(
                    update={
                        "stats_all": item.stats_all.model_copy(
                            update={"total_r": math.fsum(e.r for e in own)}
                        ),
                        "mae_total_r": math.fsum(e.mae_r for e in own),
                        # Gate attack P (2026-11-19): the rows the run writes, hashed.
                        "trades_digest": trade_rows_digest(to_mc_trades(own)),
                    }
                )
            )
        )
    return gate_monte_carlo(inputs(legit(runs=runs, mc=loaded(again))))


def test_end_to_end_legitimate_chain_passes_g4(tmp_path: Path) -> None:
    result = end_to_end(tmp_path, provenance())
    assert status(result) == "PASS", result.gate.evidence


@pytest.mark.parametrize(
    ("prov", "expected"),
    [
        (None, "NOT_EVALUABLE"),
        (provenance(synthetic=True), "FAIL"),
        (provenance(cost_free=True), "NOT_EVALUABLE"),
        (provenance(fill_model="tv_ohlc"), "NOT_EVALUABLE"),
        (provenance(cost_factor=1.5), "NOT_EVALUABLE"),
        (provenance(data_sha256=OTHER_SHA), "NOT_EVALUABLE"),
    ],
)
def test_end_to_end_any_defect_keeps_g4_from_passing(
    tmp_path: Path, prov: TradesProvenance | None, expected: str
) -> None:
    result = end_to_end(tmp_path, prov)
    assert status(result) == expected
    if prov is not None and prov.synthetic:
        assert SYNTHETIC_TAG in result.gate.evidence


def test_end_to_end_a_line_naming_a_strategy_without_rows_is_refused(tmp_path: Path) -> None:
    # Stricter than before (this case used to reach G4 as NOT_EVALUABLE): the writer and
    # the reader both refuse a line that names a strategy the rows do not hold.
    prov = provenance(strategy_ids=tuple(sorted((*STRATEGIES, "zz"))))
    with pytest.raises(AnalyticsError, match="zz"):
        write_trades_csv(entries(600), tmp_path / "trades.csv", provenance=prov)


def test_the_builder_makes_a_run_on_a_synthetic_list_synthetic(tmp_path: Path) -> None:
    result = end_to_end(tmp_path, provenance(synthetic=True), synthetic=False)
    assert all(
        item.artifact.synthetic for item in result.used if item.artifact.kind == "monte_carlo"
    )


# --- D: parity counts ----------------------------------------------------------------------


def parity_raw(**changes: object) -> dict[str, object]:
    raw = parity(ORB).artifact.model_dump()
    raw.update(changes)
    return raw


@pytest.mark.parametrize(
    "changes",
    [
        {"matched": 98, "mismatched": 1, "python_only": 1, "match_rate": 0.98},
        {"matched": 0, "mismatched": 0, "python_only": 0, "match_rate": None, "passed": False},
        {"matched": 1, "mismatched": 2, "python_only": 0, "match_rate": 1 / 3, "passed": False},
    ],
)
def test_consistent_parity_counts_are_accepted(changes: dict[str, object]) -> None:
    ParityArtifact.model_validate(parity_raw(**changes))


@pytest.mark.parametrize(
    "changes",
    [
        {"match_rate": 0.995},  # 99 / 100 stored as 0.995
        {"mismatched": 1},  # 99 / 101 != 0.99
        {"match_rate": None, "passed": False},  # trades compared but no rate
        {"match_rate": None},  # passed with no rate
        {"matched": 0, "python_only": 0, "match_rate": 0.0, "passed": False},  # rate of nothing
        {"matched": 99, "python_only": 1, "match_rate": 0.99 + 2e-9},
    ],
)
def test_inconsistent_parity_counts_are_refused(changes: dict[str, object]) -> None:
    with pytest.raises(ValidationError):
        ParityArtifact.model_validate(parity_raw(**changes))


def test_an_old_parity_file_without_mismatched_loads_when_the_rate_adds_up(tmp_path: Path) -> None:
    path = write_artifact(parity(ORB).artifact, tmp_path)
    raw = json.loads(path.read_text(encoding="utf-8"))
    del raw["mismatched"]
    path.write_text(json.dumps(raw), encoding="utf-8")
    again = load_artifact(path)
    assert isinstance(again, ParityArtifact)
    assert again.mismatched == 0
