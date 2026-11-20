"""Attacks on the Go/No-Go evidence chain: try to win a PASS (or lose a FAIL) the rules forbid.

Each test names the hole it closes. The legitimate chain must still reach PASS for G1-G4,
G6 and G7, with the overall verdict NO_GO only because G5 (Faz 3) cannot be judged.
"""

import dataclasses
import datetime as dt
import hashlib
import json
import math
from collections.abc import Sequence
from pathlib import Path

import pytest
from pydantic import ValidationError

from propbot.backtest.analytics import AnalyticsError, write_trades_csv
from propbot.backtest.artifacts import (
    MAX_EXAMINED_FILES,
    AnyArtifact,
    ArtifactError,
    ArtifactProblem,
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
    build_artifact,
    load_artifact,
    write_artifact,
)
from propbot.backtest.config import GoNoGoCriteria, load_backtest_config
from propbot.backtest.gonogo import (
    EXPECTED_INSTRUMENTS_MISSING,
    TRUNCATION_UNKNOWN,
    GoNoGoInputs,
    evaluate,
    gate_data,
    gate_edge,
    gate_monte_carlo,
    gate_parity,
    gate_robustness,
)
from propbot.backtest.model import CostModel, EntryResult, ExitReason, SpreadWindow
from propbot.backtest.montecarlo import DEFAULT_SEED
from propbot.backtest.trades import TradeListError, TradesProvenance, read_trades_csv
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
PER_RUN = 300  # complete trades per decision run; the list holds 2 x 300 rows
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
EXPECTED = CostModel(
    commission_per_unit_side=0.5,
    slippage_ticks=1.0,
    spread_points=0.3,
    spread_windows=(SpreadWindow(start_minute=565, end_minute=580, spread_points=1.2),),
)
INSTRUMENT = Instrument(SYMBOL, InstrumentKind.CFD, 0.01, 1.0, 0.01, 0.01)
# The configured size grid is recorded since gate attack M (2026-11-19).
INSTRUMENT_INFO = InstrumentInfo(
    symbol=SYMBOL, kind="cfd", tick_size=0.01, point_value=1.0, qty_step=0.01, min_qty=0.01
)
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


def stamp(moment: dt.datetime) -> str:
    return moment.strftime("%Y-%m-%dT%H:%M:%SZ")


def stats(n: int, mean: float, stdev: float = 1.4) -> StatsModel:
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


def recorded_costs(costs: CostModel = EXPECTED) -> CostInfo:
    return CostInfo(
        commission_per_unit_side=costs.commission_per_unit_side,
        slippage_ticks=costs.slippage_ticks,
        spread_points=costs.spread_points,
        spread_windows=tuple(
            SpreadWindowInfo(
                start_minute=w.start_minute, end_minute=w.end_minute, spread_points=w.spread_points
            )
            for w in costs.spread_windows
        ),
        cost_free=costs.is_free,
        spread_tz=costs.spread_tz,  # gate attack L (2026-11-19)
    )


def backtest(
    sid: str = ORB,
    *,
    label: str | None = None,
    created: dt.datetime = MADE,
    oos_mean: float = 0.25,
    cost_factor: float | None = 1.0,
    costs: CostInfo | None = None,
    instrument: InstrumentInfo | None = None,
    n_all: int = PER_RUN,
) -> LoadedArtifact:
    recorded = costs if costs is not None else recorded_costs()
    return loaded(
        build_artifact(
            BacktestArtifact,
            label=label or sid.replace("_", "-"),
            created=created,
            synthetic=False,
            data=data_info(),
            cost_free=recorded.cost_free,
            strategy_id=sid,
            params=dict(RUN_PARAMS),
            instrument=instrument or INSTRUMENT_INFO,
            costs=recorded,
            fill_model="pessimistic",
            cost_factor=cost_factor,
            initial_capital=100_000.0,
            oos_start_utc="2025-01-01T00:00:00Z",
            stats_all=stats(n_all, 0.3),
            mae_total_r=-1.0 * n_all,
            stats_is=stats(300, 0.4),
            stats_oos=stats(300, oos_mean),
            g3_ratio=oos_mean / 0.4 if oos_mean > 0 else None,
            g3_note=None if oos_mean > 0 else "oran yok",
            tables=TablesModel(weekday=(), hour=(), month=()),
            equity_curve=(),
            incomplete_entries=0,
            dropped_entries=0,
            # Recorded since gate attacks L and P (2026-11-19).
            min_gap_seconds=MIN_GAP_SECONDS,
            trades_digest=rows_digest(sid),
        )
    )


def validation(*, label: str = "us500", clean: bool = True) -> LoadedArtifact:
    return loaded(
        build_artifact(
            DataValidationArtifact,
            label=label,
            created=MADE,
            synthetic=False,
            data=data_info(),
            clean=clean,
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
    *,
    label: str = "ftmo",
    created: dt.datetime = MADE + HOUR,
    n_trades: int = 2 * PER_RUN,
    p_pass: float = 0.7,
    provenance: TradesProvenanceModel | None = None,
) -> LoadedArtifact:
    return loaded(
        build_artifact(
            MonteCarloArtifact,
            label=label,
            created=created,
            synthetic=False,
            data=None,
            inputs=MonteCarloInputs(
                trades_file="trades.csv",
                n_trades=n_trades,
                risk_pct=0.25,
                cost_r=0.0,
                n_paths=10_000,
                max_days=1000,
                dd_days=250,
                # The reproducible run's seed (gate attack N, 2026-11-19).
                seed=DEFAULT_SEED,
                skip_prob=0.0,
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
                sum_r=math.fsum([0.3 * PER_RUN, 0.3 * PER_RUN]),
                sum_mae_r=-2.0 * PER_RUN,
                # The decision runs' rows (gate attack P, 2026-11-19).
                trades_digests={sid: rows_digest(sid) for sid in STRATEGIES},
            ),
            p_pass_phase=(max(p_pass, 0.8), p_pass),
            p_breach=0.1,
            breach_shares=(BreachShareModel(phase=1, reason="max_loss", probability=0.1),),
            p_undecided=0.9 - p_pass,
            time_to_pass=(None, None),
            drawdown=DrawdownModel(median=1.0, q95=2.0, q99=3.0, es95=2.5, es99=3.5),
            drawdown_histogram=HistogramModel(edges=(0.0, 1.0), counts=(10_000,)),
            provenance=provenance if provenance is not None else provenance_model(),
        )
    )


def parity(
    sid: str, *, label: str | None = None, matched: int = 99, python_only: int = 1
) -> LoadedArtifact:
    total = matched + python_only
    return loaded(
        build_artifact(
            ParityArtifact,
            label=label or sid.replace("_", "-"),
            created=MADE,
            synthetic=False,
            # Since review E: the compared bars, parameters and instrument are recorded.
            data=data_info(),
            params=dict(PARITY_PARAMS),
            instrument=INSTRUMENT_INFO,
            # The Python side's session gap and capital (gate attacks K, L, 2026-11-19).
            min_gap_seconds=MIN_GAP_SECONDS,
            initial_capital=100_000.0,
            strategy_id=sid,
            matched=matched,
            python_only=python_only,
            tradingview_only=0,
            match_rate=matched / total,
            threshold=0.5,
            passed=True,
            mismatches=(),
            sources=(),
        )
    )


def legit(
    *, runs: Sequence[LoadedArtifact] | None = None, extra: Sequence[LoadedArtifact] = ()
) -> list[LoadedArtifact]:
    items = [validation(), monte_carlo(), *(parity(sid) for sid in STRATEGIES)]
    items += list(runs) if runs is not None else [backtest(sid) for sid in STRATEGIES]
    return [*items, *extra]


def inputs(items: Sequence[LoadedArtifact], **changes: object) -> GoNoGoInputs:
    base = GoNoGoInputs(
        artifacts=items,
        criteria=CRITERIA,
        venue_name=VENUE_NAME,
        venue=VENUE,
        strategy_ids=STRATEGIES,
        planned_risk_pct=0.25,
        today=TODAY,
        created=REPORT_TIME,
        compliance_files=(),
        expected_costs={SYMBOL: EXPECTED},
        expected_instruments={SYMBOL: INSTRUMENT},
        expected_params={ORB: dict(RUN_PARAMS), ASIAN: dict(RUN_PARAMS)},
        expected_parity_params={ORB: dict(PARITY_PARAMS), ASIAN: dict(PARITY_PARAMS)},
        risk_params={ORB: "risk_pct", ASIAN: "risk_pct"},
        artifacts_truncated=False,
        expected_oos_start_utc="2025-01-01T00:00:00Z",
        # Explicit since gate attack L (2026-11-19): unknown -> no decision run.
        expected_min_gap_seconds=MIN_GAP_SECONDS,
    )
    return dataclasses.replace(base, **changes)  # type: ignore[arg-type]


def statuses(case: GoNoGoInputs) -> dict[str, str]:
    return {gate.id: gate.status for gate in evaluate(case).gates}


LEGIT_STATUSES = {
    "G1": "PASS",
    "G2": "PASS",
    "G3": "PASS",
    "G4": "PASS",
    "G5": "NOT_EVALUABLE",
    "G6": "PASS",
    "G7": "PASS",
}


# --- (7) the legitimate path ---------------------------------------------------------------


def test_the_legitimate_chain_passes_every_gate_but_g5(tmp_path: Path) -> None:
    for item in legit():
        write_artifact(item.artifact, tmp_path)
    scan = all_artifacts(tmp_path)
    case = inputs(scan.artifacts, problems=scan.problems, artifacts_truncated=scan.truncated)
    report = evaluate(case)
    assert {gate.id: gate.status for gate in report.gates} == LEGIT_STATUSES
    assert report.overall == "NO_GO"
    assert [r for r in report.reasons if r.startswith("G")] == [
        r for r in report.reasons if "G5" in r
    ]


# --- (1) selection: ties, defaults, unreadable files --------------------------------------


def test_two_possible_decision_runs_in_the_same_second_decide_nothing() -> None:
    # Without the tie rule the file name picked the run: label "zz" sorts first newest-first.
    bad = backtest(label="aa", oos_mean=-0.2)
    good = backtest(label="zz")
    case = inputs(legit(runs=[bad, good, backtest(ASIAN)]))
    for gate in (gate_data, gate_edge, gate_robustness, gate_monte_carlo):
        result = gate(case)
        assert result.gate.status == "NOT_EVALUABLE", gate.__name__
        assert "aynı saniyede" in result.gate.evidence or "karar koşusu olmayan" in (
            result.gate.evidence
        )


def test_a_same_second_comparison_run_does_not_tie() -> None:
    sensitivity = backtest(label="zz", cost_factor=1.5, oos_mean=0.6)
    assert gate_edge(inputs(legit(extra=[sensitivity]))).gate.status == "PASS"


def test_two_monte_carlo_runs_in_the_same_second_decide_nothing() -> None:
    worse = monte_carlo(label="aa", p_pass=0.3)
    better = monte_carlo(label="zz")
    result = gate_monte_carlo(inputs(legit(extra=[worse, better])))
    assert result.gate.status != "PASS"


def test_two_parity_reports_in_the_same_second_decide_nothing() -> None:
    poor = parity(ORB, label="aa", matched=90, python_only=10)
    fine = parity(ORB, label="zz")
    items = [item for item in legit() if not item.file_name.startswith("parity_ny-orb")]
    result = gate_parity(inputs([*items, poor, fine]))
    assert result.gate.status == "NOT_EVALUABLE"
    assert "aynı saniyede" in result.gate.evidence


def test_two_validations_of_one_file_in_the_same_second_decide_nothing() -> None:
    dirty = validation(label="aa", clean=False)
    clean = validation(label="zz")
    items = [item for item in legit() if item.artifact.kind != "data_validation"]
    assert gate_data(inputs([*items, dirty, clean])).gate.status == "NOT_EVALUABLE"


def test_unknown_truncation_is_not_read_as_complete() -> None:
    case = inputs(legit(), artifacts_truncated=None)
    for gate in (gate_data, gate_edge, gate_robustness, gate_monte_carlo, gate_parity):
        result = gate(case)
        assert result.gate.status == "NOT_EVALUABLE"
        assert TRUNCATION_UNKNOWN in result.gate.evidence
    # The default is that unknown state: a caller that forgets it never gets PASS.
    plain = GoNoGoInputs(
        artifacts=legit(),
        criteria=CRITERIA,
        venue_name=VENUE_NAME,
        venue=VENUE,
        strategy_ids=STRATEGIES,
        planned_risk_pct=0.25,
        today=TODAY,
        created=REPORT_TIME,
        compliance_files=(),
    )
    assert all(statuses(plain)[g] != "PASS" for g in ("G1", "G2", "G3", "G4", "G6"))


def test_an_unreadable_backtest_also_blocks_g4() -> None:
    # It may be the newer decision run the Monte Carlo list should have come from.
    problem = ArtifactProblem("backtest_orb_20261117T110000Z.json", "geçerli JSON değil")
    result = gate_monte_carlo(inputs(legit(), problems=(problem,)))
    assert result.gate.status == "NOT_EVALUABLE"


def test_pushing_the_bad_run_out_of_the_examined_window_never_passes(tmp_path: Path) -> None:
    bad = backtest(label="bad", created=MADE + HOUR, oos_mean=-0.2)
    for item in [*legit(), bad]:
        write_artifact(item.artifact, tmp_path)
    # Future-dated junk sorts first and pushes everything real out of the window.
    for k in range(MAX_EXAMINED_FILES):
        name = f"cost_matrix_junk_{(MADE + dt.timedelta(days=30, seconds=k)):%Y%m%dT%H%M%SZ}.json"
        (tmp_path / name).write_text("{}", encoding="utf-8")
    scan = all_artifacts(tmp_path)
    assert scan.truncated
    case = inputs(scan.artifacts, problems=scan.problems, artifacts_truncated=scan.truncated)
    assert all(statuses(case)[g] != "PASS" for g in ("G1", "G2", "G3", "G4", "G6"))


def test_a_future_dated_decision_run_blocks_instead_of_being_skipped() -> None:
    ahead = backtest(label="ahead", created=REPORT_TIME + HOUR, oos_mean=-0.2)
    assert gate_edge(inputs(legit(extra=[ahead]))).gate.status == "NOT_EVALUABLE"


# --- (2) cost factor, expected costs and instruments --------------------------------------


@pytest.mark.parametrize("factor", [1.0000000001, 0.9999999999])
def test_a_factor_a_hair_from_one_is_a_comparison_run(factor: float) -> None:
    only = backtest(cost_factor=factor)
    result = gate_edge(inputs(legit(runs=[only, backtest(ASIAN)])))
    assert result.gate.status == "FAIL"
    assert "1,0 değil" in result.gate.evidence


@pytest.mark.parametrize("token", ["NaN", "Infinity", "-Infinity", '"1.0"', "true", "-1.0"])
def test_a_hand_edited_cost_factor_is_refused(tmp_path: Path, token: str) -> None:
    path = write_artifact(backtest().artifact, tmp_path)
    raw = path.read_text(encoding="utf-8").replace('"cost_factor": 1.0', f'"cost_factor": {token}')
    path.write_text(raw, encoding="utf-8")
    with pytest.raises(ArtifactError):
        load_artifact(path)


def test_an_integer_one_cost_factor_is_exactly_one(tmp_path: Path) -> None:
    path = write_artifact(backtest().artifact, tmp_path)
    raw = path.read_text(encoding="utf-8").replace('"cost_factor": 1.0', '"cost_factor": 1')
    path.write_text(raw, encoding="utf-8")
    try:
        artifact = load_artifact(path)
    except ArtifactError:
        return  # refusing it is fine too
    assert isinstance(artifact, BacktestArtifact)
    assert artifact.cost_factor == 1.0


def test_scaled_by_one_keeps_every_cost_bit_for_bit() -> None:
    scaled = EXPECTED.scaled(1.0)
    run = backtest(costs=recorded_costs(scaled))
    assert gate_edge(inputs(legit(runs=[run, backtest(ASIAN)]))).gate.status == "PASS"


@pytest.mark.parametrize(
    "costs",
    [
        dataclasses.replace(EXPECTED, spread_windows=()),
        dataclasses.replace(
            EXPECTED,
            spread_windows=(SpreadWindow(start_minute=565, end_minute=580, spread_points=1.1),),
        ),
        dataclasses.replace(EXPECTED, spread_points=0.3 - 1e-12),
        dataclasses.replace(EXPECTED, slippage_ticks=0.0),
    ],
)
def test_a_cheaper_run_at_factor_one_is_never_a_decision_run(costs: CostModel) -> None:
    run = backtest(costs=recorded_costs(costs), oos_mean=0.6)
    bad_old = backtest(label="old", created=MADE - HOUR, oos_mean=-0.2)
    result = gate_edge(inputs(legit(runs=[run, bad_old, backtest(ASIAN)])))
    assert result.gate.status == "NOT_EVALUABLE"
    assert "daha eski bir koşuya geçilmez" in result.gate.evidence


@pytest.mark.parametrize(
    "instrument",
    [
        # Each differs in one field only: the size grid is the configured one (since gate
        # attack M it is compared too, and an unrecorded one would hide what is tested).
        INSTRUMENT_INFO.model_copy(update={"tick_size": 0.001}),
        INSTRUMENT_INFO.model_copy(update={"point_value": 10.0}),
        INSTRUMENT_INFO.model_copy(update={"kind": "futures"}),
    ],
)
def test_equal_cost_numbers_on_another_instrument_are_not_a_decision_run(
    instrument: InstrumentInfo,
) -> None:
    # 1 tick of slippage on a 10x smaller tick is 10x cheaper in R.
    run = backtest(instrument=instrument, oos_mean=0.6)
    result = gate_edge(inputs(legit(runs=[run, backtest(ASIAN)])))
    assert result.gate.status == "NOT_EVALUABLE"
    assert "enstrüman" in result.gate.evidence


def test_a_profile_for_another_symbol_does_not_confirm() -> None:
    case = inputs(legit(), expected_costs={"MES": EXPECTED})
    assert statuses(case)["G2"] == "NOT_EVALUABLE"
    case = inputs(legit(), expected_instruments={"MES": INSTRUMENT})
    assert statuses(case)["G2"] == "NOT_EVALUABLE"


def test_missing_instruments_block_g1_to_g4() -> None:
    case = inputs(legit(), expected_instruments=None)
    result = statuses(case)
    assert all(result[g] == "NOT_EVALUABLE" for g in ("G1", "G2", "G3", "G4"))
    assert EXPECTED_INSTRUMENTS_MISSING in gate_edge(case).gate.evidence


# --- (3) the provenance line ---------------------------------------------------------------


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


def entry(seq: int, r: float, sid: str) -> EntryResult:
    start = 1_767_623_400_000 + seq * 86_400_000
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
    return [entry(i, 2.0 if i % 10 < 7 else -1.0, STRATEGIES[i % 2]) for i in range(n)]


def written(tmp_path: Path, prov: TradesProvenance | None = None, n: int = 20) -> Path:
    path = tmp_path / "trades.csv"
    write_trades_csv(entries(n), path, provenance=prov or provenance())
    return path


def edit_line(path: Path, old: str, new: str) -> None:
    raw = path.read_bytes().decode("utf-8")
    assert old in raw
    path.write_bytes(raw.replace(old, new, 1).encode("utf-8"))


@pytest.mark.parametrize(
    ("old", "new"),
    [
        (" v=1 ", " v=1  "),  # double space
        ("created_utc=2026-11-17T10:00:00Z", "created_utc=2026-11-17T10:00:00Z "),  # trailing
        (" v=1 ", " v=1 v=1 "),  # duplicated key
        (" v=1 ", " v=1 x=1 "),  # unknown key
        (" synthetic=false", ""),  # missing key
        ("synthetic=false", "synthetic=False"),  # case
        ("cost_factor=1.0", "cost_factor=1"),
        ("cost_factor=1.0", "cost_factor=1.00"),
        ("cost_factor=1.0", "cost_factor=nan"),
        ("cost_factor=1.0", "cost_factor=inf"),
        ("fill_model=pessimistic", "fill_model=PESSIMISTIC"),
        ("strategy_ids=asian_sweep,ny_orb_fvg", "strategy_ids=ny_orb_fvg,asian_sweep"),
        ("strategy_ids=asian_sweep,ny_orb_fvg", "strategy_ids=asian_sweep,ny_orb_fvg,ny_orb_fvg"),
        ("strategy_ids=asian_sweep,ny_orb_fvg", "strategy_ids=asian_sweep,ny_orb_fvg#x"),
        ("strategy_ids=asian_sweep,ny_orb_fvg", "strategy_ids=asian_sweep,ny_orb_fvg%2c"),
        ("data_file=us500_1m.csv", "data_file=us500 1m.csv"),
        ("data_file=us500_1m.csv", "data_file=us500=1m.csv"),
        ("#propbot-trades", "#Propbot-trades"),
        ("#propbot-trades", " #propbot-trades"),
        ("#propbot-trades", "﻿﻿#propbot-trades"),  # a second BOM
    ],
)
def test_a_tampered_line_is_an_error_not_missing_provenance(
    tmp_path: Path, old: str, new: str
) -> None:
    path = written(tmp_path)
    edit_line(path, old, new)
    with pytest.raises(TradeListError):
        read_trades_csv(path)


def test_one_bom_and_crlf_endings_still_read(tmp_path: Path) -> None:
    path = written(tmp_path)
    raw = path.read_bytes().decode("utf-8").replace("\n", "\r\n")
    path.write_bytes(b"\xef\xbb\xbf" + raw.encode("utf-8"))
    assert read_trades_csv(path)[1] == provenance()


def test_a_second_provenance_line_or_one_after_the_header_is_an_error(tmp_path: Path) -> None:
    path = written(tmp_path)
    line = provenance().to_line()
    lines = path.read_text(encoding="utf-8").splitlines()
    for variant in (
        [lines[0], line, *lines[1:]],  # a second line before the header
        [lines[1], line, *lines[2:]],  # the line after the header
        [*lines, line],  # at the end
    ):
        path.write_text("\n".join(variant) + "\n", encoding="utf-8")
        with pytest.raises(TradeListError):
            read_trades_csv(path)


@pytest.mark.parametrize(
    "name",
    [
        "a b.csv",
        "a,b.csv",
        "a=b.csv",
        "a\nb.csv",
        "#x.csv",
        "a%41.csv",
        "a b.csv",
        "üs500.csv",
        "a strategy_ids=x.csv",
    ],
)
def test_file_names_with_separators_round_trip_and_cannot_inject(tmp_path: Path, name: str) -> None:
    prov = provenance(data_file=name)
    line = prov.to_line()
    assert "\n" not in line
    assert line.count("=") == line.count(" ") == 9
    path = written(tmp_path, prov)
    assert read_trades_csv(path)[1] == prov


def test_strategy_ids_with_separators_round_trip(tmp_path: Path) -> None:
    ids = ("a,b", "c d", "e=f", "g\nh", "#i")
    prov = provenance(strategy_ids=tuple(sorted(ids)))
    path = tmp_path / "trades.csv"
    write_trades_csv([entry(i, 1.0, ids[i % len(ids)]) for i in range(10)], path, provenance=prov)
    trades, read_back = read_trades_csv(path)
    assert read_back == prov
    assert {t.strategy_id for t in trades} == set(ids)


def test_a_look_alike_strategy_id_in_the_rows_is_refused(tmp_path: Path) -> None:
    path = written(tmp_path)
    # Cyrillic small o (U+043E) in place of the Latin "o" in every ny_orb_fvg row.
    raw = path.read_text(encoding="utf-8")
    head, rest = raw.split("\n", 1)
    path.write_text(head + "\n" + rest.replace("ny_orb_fvg", "ny_оrb_fvg"), encoding="utf-8")
    with pytest.raises(TradeListError, match="köken satırındaki stratejiler arasında değil"):
        read_trades_csv(path)


def test_rows_of_only_one_named_strategy_are_refused(tmp_path: Path) -> None:
    path = written(tmp_path)
    lines = path.read_text(encoding="utf-8").splitlines()
    kept = [lines[0], lines[1], *(row for row in lines[2:] if ",asian_sweep," not in row)]
    path.write_text("\n".join(kept) + "\n", encoding="utf-8")
    with pytest.raises(TradeListError, match="işlemi yok"):
        read_trades_csv(path)


def test_the_writer_refuses_a_line_naming_a_strategy_without_rows(tmp_path: Path) -> None:
    prov = provenance(strategy_ids=tuple(sorted((*STRATEGIES, "zz"))))
    with pytest.raises(AnalyticsError, match="zz"):
        write_trades_csv(entries(20), tmp_path / "t.csv", provenance=prov)


def test_a_list_with_rows_removed_does_not_pass_g4() -> None:
    # The losers dropped by hand: the line still names both strategies, the count does not.
    assert gate_monte_carlo(inputs(legit())).gate.status == "PASS"
    shrunk = monte_carlo(n_trades=2 * PER_RUN - 60)
    items = [i for i in legit() if i.artifact.kind != "monte_carlo"] + [shrunk]
    result = gate_monte_carlo(inputs(items))
    assert result.gate.status == "NOT_EVALUABLE"
    assert "liste eksik ya da fazla" in result.gate.evidence


@pytest.mark.parametrize(
    "changes",
    [
        {"data_sha256": OTHER_SHA},
        {"strategy_ids": (ORB,)},
        {"strategy_ids": tuple(sorted((*STRATEGIES, "zz")))},
        {"created_utc": stamp(MADE - HOUR)},
    ],
)
def test_g4_list_for_other_data_or_strategies_is_not_pass(changes: dict[str, object]) -> None:
    mc = monte_carlo(provenance=provenance_model(**changes))
    items = [i for i in legit() if i.artifact.kind != "monte_carlo"] + [mc]
    assert gate_monte_carlo(inputs(items)).gate.status == "NOT_EVALUABLE"


def test_judged_strategies_a_subset_of_the_lines_is_not_pass() -> None:
    assert gate_monte_carlo(inputs(legit(), strategy_ids=(ORB,))).gate.status == "NOT_EVALUABLE"


# --- (4) the Monte Carlo artifact model ----------------------------------------------------


def test_a_synthetic_list_in_a_non_synthetic_artifact_is_refused() -> None:
    raw = monte_carlo().artifact.model_dump()
    raw["provenance"] = provenance_model(synthetic=True).model_dump()
    with pytest.raises(ValidationError, match="synthetic"):
        MonteCarloArtifact.model_validate(raw)


def test_dropping_the_provenance_by_hand_is_not_evaluable(tmp_path: Path) -> None:
    path = write_artifact(monte_carlo().artifact, tmp_path)
    data = json.loads(path.read_text(encoding="utf-8"))
    del data["provenance"]
    path.write_text(json.dumps(data), encoding="utf-8")
    edited = load_artifact(path)
    items = [i for i in legit() if i.artifact.kind != "monte_carlo"] + [loaded(edited)]
    assert gate_monte_carlo(inputs(items)).gate.status == "NOT_EVALUABLE"


# --- (5) parity ----------------------------------------------------------------------------


def test_a_rate_rounded_onto_the_floor_from_huge_counts_does_not_pass() -> None:
    # 9,799,999,999 / 10^10 is below 0.98 by 1e-10: within the model's rate tolerance, so
    # the artifact is valid with 0.98 stored; G6 must judge the real ratio.
    matched = 9_799_999_999
    raw = parity(ORB).artifact.model_dump()
    raw.update(matched=matched, python_only=10**10 - matched, match_rate=0.98)
    hair = loaded(ParityArtifact.model_validate(raw))
    items = [i for i in legit() if not i.file_name.startswith("parity_ny-orb")] + [hair]
    assert gate_parity(inputs(items)).gate.status == "FAIL"


@pytest.mark.parametrize(
    "changes",
    [
        {"match_rate": None, "passed": True},
        {"match_rate": 1.0},
        {"mismatched": 5},
        {"matched": 0, "python_only": 0, "match_rate": 0.99},
    ],
)
def test_inconsistent_parity_counts_are_refused(changes: dict[str, object]) -> None:
    raw = parity(ORB).artifact.model_dump()
    raw.update(changes)
    with pytest.raises(ValidationError):
        ParityArtifact.model_validate(raw)


# --- (6) config floors ---------------------------------------------------------------------


SPEC = {
    "min_data_years": 2.0,
    "required_tf_seconds": 60,
    "min_oos_t_stat": 2.0,
    "min_oos_to_is_ratio": 0.5,
    "min_p_pass_all_phases": 0.6,
    "max_p_breach": 0.2,
    "min_parity_rate": 0.98,
}


@pytest.mark.parametrize(
    ("key", "looser"),
    [
        ("min_data_years", 1.99),
        ("required_tf_seconds", 61),
        ("min_oos_t_stat", 1.99),
        ("min_oos_to_is_ratio", 0.49),
        ("min_p_pass_all_phases", 0.59),
        ("max_p_breach", 0.21),
        ("min_parity_rate", 0.97),
        ("min_oos_trades", 29),
        ("min_bar_hours_per_year", 1499),
        ("min_mc_paths", 4999),
        ("min_mc_trades", 99),
        ("max_artifact_age_days", 31),
        ("min_data_years", math.nan),
        ("max_p_breach", math.nan),
        ("min_bar_hours_per_year", math.inf),
    ],
)
def test_every_threshold_one_unit_looser_is_refused(key: str, looser: float) -> None:
    with pytest.raises(ValidationError):
        GoNoGoCriteria(**{**SPEC, key: looser})  # type: ignore[arg-type]


def test_the_real_config_loads_with_the_floors() -> None:
    config = load_backtest_config(CONFIG_DIR / "backtest.yaml")
    assert config.go_no_go == GoNoGoCriteria(**SPEC)  # type: ignore[arg-type]
