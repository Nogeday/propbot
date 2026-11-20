"""Final cross-cutting review (2026-11-19): each finding rebuilt as a test before its fix.

A wrong PASS of any gate is the worst defect of the Go/No-Go report (CLAUDE.md
invariant 1), so every test here builds a set that a gate used to PASS on and checks
that it no longer does, then that the honest set still passes. Letters follow the
review: A (OOS boundary), B (G4 vs venue rules), C (intratrade model), D (G1 validation
link), E (G6 parity link), F (edited trade list), H (cost-free list read as net),
J (compliance directory).
"""

import dataclasses
import datetime as dt
import json
import math
from collections.abc import Callable
from pathlib import Path

import pytest
from pydantic import ValidationError

from propbot.backtest.artifacts import (
    BacktestArtifact,
    InstrumentInfo,
    MonteCarloArtifact,
    all_artifacts,
    load_artifact,
)
from propbot.backtest.config import MIN_PARITY_TRADES, GoNoGoCriteria, load_backtest_config
from propbot.backtest.gonogo import (
    evaluate,
    gate_data,
    gate_edge,
    gate_monte_carlo,
    gate_parity,
    gate_robustness,
    gate_rules,
)
from propbot.backtest.runner import RunRequest, render_summary, run
from propbot.cli import main
from propbot.data.ohlcv import load_ohlcv
from propbot.risk.venue import DailyLossBasis, DailyReset, MaxLossType
from support.paths import CONFIG_DIR, FIXTURES
from unit.test_gonogo import (
    CRITERIA,
    DATA_FILE,
    OTHER_SHA,
    PARITY_PARAMS,
    SHA,
    VENUE,
    backtest,
    data_info,
    inputs,
    monte_carlo,
    parity,
    passing_set,
    replaced,
    validation,
)

ORB_BT = "backtest_ny-orb-fvg_"
ORB_PARITY = "parity_ny-orb-fvg_"
MC = "monte_carlo_"
VALIDATION = "data_validation_"
LATER = dt.datetime(2026, 11, 17, 11, 0, 0, tzinfo=dt.UTC)
BARS = FIXTURES / "ohlcv" / "synthetic_5m.csv"
VENUE_FILE = CONFIG_DIR / "venues" / "ftmo_2step_100k.yaml"
START = dt.datetime(2026, 11, 18, 9, 0, tzinfo=dt.UTC)


def clock() -> Callable[[], dt.datetime]:
    moments = iter(START + dt.timedelta(seconds=step) for step in range(10_000))
    return lambda: next(moments)


def part_of(result_gate: object, subject: str) -> dict[str, object]:
    details = result_gate.gate.details  # type: ignore[attr-defined]
    parts = details["parts"]
    return next(part for part in parts if part["subject"] == subject)


def test_the_honest_set_still_passes_every_data_gate() -> None:
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


# --- A: OOS boundary cherry-picking ---------------------------------------------------------


def test_a_run_with_another_oos_start_cannot_decide_g2_or_g3() -> None:
    what_if = backtest(oos_start="2026-10-20T00:00:00Z")
    items = replaced(passing_set(), ORB_BT, what_if)

    for gate in (gate_edge(inputs(items)), gate_robustness(inputs(items))):
        part = part_of(gate, "ny_orb_fvg")
        assert part["status"] != "PASS"
        assert gate.gate.status != "PASS"
    g2 = gate_edge(inputs(items))
    assert "OOS başlangıcı" in g2.gate.evidence
    assert "2026-10-20" in g2.gate.evidence


def test_a_what_if_oos_run_neither_decides_nor_hides_an_older_decision_run() -> None:
    what_if = backtest(oos_start="2026-10-20T00:00:00Z", created=LATER, oos=None, label="orb-oos")
    items = [*passing_set(), what_if]

    g2 = gate_edge(inputs(items))
    assert g2.gate.status == "PASS"
    assert part_of(g2, "ny_orb_fvg")["values"]["backtest_file"].startswith(ORB_BT)  # type: ignore[index]


def test_a_unknown_configured_oos_start_confirms_no_decision_run() -> None:
    g2 = gate_edge(inputs(expected_oos_start_utc=None))
    assert g2.gate.status == "NOT_EVALUABLE"
    assert "OOS başlangıcı" in g2.gate.evidence


def test_a_runner_labels_a_run_off_the_configured_oos_start_and_says_it_is_no_decision() -> None:
    series = load_ohlcv(BARS)
    config = load_backtest_config(CONFIG_DIR / "backtest.yaml")
    configured = int(config.samples.oos_start_utc.timestamp()) * 1000
    other = int(dt.datetime(2026, 10, 20, tzinfo=dt.UTC).timestamp()) * 1000
    base = RunRequest(
        series=series,
        strategy_id="ny_orb_fvg",
        instrument=config.instrument("MES"),
        base_costs=config.cost_model("MES"),
        initial_capital=100_000.0,
        oos_start_ms=other,
        configured_oos_start_ms=configured,
        # Sized at the planned risk (config/risk.yaml): since 2026-11-19 a run at the
        # script's own risk is a comparison run of its own ("-pinerisk"), and this test
        # is about the OOS boundary alone.
        planned_risk_pct=0.25,
    )

    result = run(base)

    assert result.label_suffix == "-oos20261020"
    assert not result.decision
    assert "KARAR KOŞUSU DEĞİL" in render_summary(result)
    same = run(dataclasses.replace(base, oos_start_ms=configured))
    assert same.label_suffix == ""
    assert same.decision
    # Without the configured start the runner cannot know; gonogo's own check is the gate.
    unknown = run(dataclasses.replace(base, configured_oos_start_ms=None))
    assert unknown.label_suffix == ""


def test_a_cli_backtest_with_its_own_oos_start_writes_a_labelled_comparison_run(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    reports = tmp_path / "reports"
    arguments = ["backtest", "--data", str(BARS), "--config-dir", str(CONFIG_DIR)]
    arguments += ["--reports-dir", str(reports), "--strategy", "ny_orb_fvg"]

    assert main([*arguments, "--oos-start", "2026-11-01"], clock=clock()) == 0

    (written,) = reports.iterdir()
    assert written.name.startswith("backtest_ny_orb_fvg-oos20261101_")
    assert "KARAR KOŞUSU DEĞİL" in capsys.readouterr().out


# --- B: G4 against every simulated venue rule -------------------------------------------------


def test_b_an_older_monte_carlo_without_the_rule_fields_is_not_evaluable() -> None:
    old = monte_carlo(max_loss_type=None, daily_loss_basis=None, reset_tz=None, reset_time=None)
    g4 = gate_monte_carlo(inputs(replaced(passing_set(), MC, old)))
    assert g4.gate.status == "NOT_EVALUABLE"
    assert "kaydetmiyor" in g4.gate.evidence


@pytest.mark.parametrize(
    ("change", "word"),
    [
        ({"max_loss_type": MaxLossType.TRAILING_EOD}, "max_loss_type"),
        ({"daily_reset": DailyReset(tz="Europe/London", time="00:00")}, "daily_reset"),
        ({"daily_loss_basis": DailyLossBasis.MAX_BALANCE_EQUITY_AT_RESET}, "daily_loss_basis"),
    ],
)
def test_b_a_profile_edited_after_the_simulation_makes_g4_not_evaluable(
    change: dict[str, object], word: str
) -> None:
    venue = VENUE.model_copy(update=change)
    g4 = gate_monte_carlo(inputs(venue=venue))
    assert g4.gate.status == "NOT_EVALUABLE"
    assert word in g4.gate.evidence


def test_b_a_trailing_profile_is_never_pass_even_if_the_artifact_says_trailing() -> None:
    # The simulator refuses trailing_eod; an artifact claiming it was hand-made.
    venue = VENUE.model_copy(update={"max_loss_type": MaxLossType.TRAILING_EOD})
    mc = monte_carlo(max_loss_type="trailing_eod")
    g4 = gate_monte_carlo(inputs(replaced(passing_set(), MC, mc), venue=venue))
    assert g4.gate.status == "NOT_EVALUABLE"
    assert "static" in g4.gate.evidence


def test_b_a_pass_cites_the_documented_daily_loss_basis_mapping() -> None:
    g4 = gate_monte_carlo(inputs())
    assert g4.gate.status == "PASS"
    assert "ADR-0002" in g4.gate.evidence


# --- C: intratrade model ---------------------------------------------------------------------


@pytest.mark.parametrize("model", ["closed", None])
def test_c_only_the_pessimistic_intratrade_model_can_pass_g4(model: str | None) -> None:
    mc = monte_carlo(intratrade=model)
    g4 = gate_monte_carlo(inputs(replaced(passing_set(), MC, mc)))
    assert g4.gate.status == "NOT_EVALUABLE"
    assert "işlem içi model" in g4.gate.evidence


# --- D: G1 validation link -------------------------------------------------------------------


def test_d_a_validation_of_the_same_file_read_in_another_zone_fails_g1() -> None:
    shifted = validation(
        data=data_info(
            bar_tz="Etc/GMT-7", first="2024-01-02T07:30:00Z", last="2026-11-13T14:00:00Z"
        )
    )
    g1 = gate_data(inputs(replaced(passing_set(), VALIDATION, shifted)))
    assert g1.gate.status == "FAIL"
    assert "Etc/GMT-7" in g1.gate.evidence
    assert "UTC" in g1.gate.evidence


@pytest.mark.parametrize(
    "change",
    [
        {"tf": 300},
        {"first": "2024-01-03T14:30:00Z"},
        {"last": "2026-11-12T21:00:00Z"},
    ],
)
def test_d_a_validation_of_other_bars_fails_g1(change: dict[str, object]) -> None:
    other = validation(data=data_info(**change))  # type: ignore[arg-type]
    g1 = gate_data(inputs(replaced(passing_set(), VALIDATION, other)))
    assert g1.gate.status == "FAIL"


def test_d_a_matching_validation_still_decides_next_to_another_reading() -> None:
    shifted = validation(data=data_info(bar_tz="Etc/GMT-7"), created=LATER, label="us500-ny7")
    g1 = gate_data(inputs([*passing_set(), shifted]))
    assert g1.gate.status == "PASS"


# --- E: G6 parity link -----------------------------------------------------------------------


def test_e_a_parity_report_with_other_parameters_is_not_evaluable() -> None:
    # Based on the parity set since 2026-11-19: G6 compares a parity report with the
    # configured parameters at the script's own risk, so only risk_reward differs here.
    other = parity(params={**PARITY_PARAMS, "risk_reward": 3.0})
    g6 = gate_parity(inputs(replaced(passing_set(), ORB_PARITY, other)))
    assert part_of(g6, "ny_orb_fvg")["status"] == "NOT_EVALUABLE"
    assert "aynı değil: risk_reward (" in g6.gate.evidence


def test_e_an_older_parity_report_without_parameters_or_instrument_is_not_evaluable() -> None:
    old = parity(params=None, instrument=None)
    g6 = gate_parity(inputs(replaced(passing_set(), ORB_PARITY, old)))
    assert part_of(g6, "ny_orb_fvg")["status"] == "NOT_EVALUABLE"


def test_e_a_parity_report_on_another_instrument_is_not_evaluable() -> None:
    other = parity(
        instrument=InstrumentInfo(symbol="US500.cash", kind="cfd", tick_size=0.25, point_value=1.0)
    )
    g6 = gate_parity(inputs(replaced(passing_set(), ORB_PARITY, other)))
    assert part_of(g6, "ny_orb_fvg")["status"] == "NOT_EVALUABLE"
    assert "tick_size" in g6.gate.evidence


def test_e_one_compared_trade_cannot_pass_g6() -> None:
    tiny = parity(counts=(1, 0))
    g6 = gate_parity(inputs(replaced(passing_set(), ORB_PARITY, tiny)))
    assert part_of(g6, "ny_orb_fvg")["status"] == "NOT_EVALUABLE"
    assert str(MIN_PARITY_TRADES) in g6.gate.evidence


def test_e_the_evidence_names_both_the_parity_data_and_the_decision_run_data() -> None:
    elsewhere = parity(data=data_info(sha=OTHER_SHA, file_name="tv_short.csv"))
    g6 = gate_parity(inputs(replaced(passing_set(), ORB_PARITY, elsewhere)))
    assert g6.gate.status == "PASS"
    assert "tv_short.csv" in g6.gate.evidence
    assert DATA_FILE in g6.gate.evidence


def test_e_min_parity_trades_is_a_floor_in_code() -> None:
    assert CRITERIA.min_parity_trades == MIN_PARITY_TRADES == 100
    with pytest.raises(ValidationError):
        GoNoGoCriteria.model_validate(
            {**CRITERIA.model_dump(), "min_parity_trades": MIN_PARITY_TRADES - 1}
        )
    yaml_value = load_backtest_config(CONFIG_DIR / "backtest.yaml").go_no_go.min_parity_trades
    assert yaml_value >= MIN_PARITY_TRADES


# --- F: edited trade list --------------------------------------------------------------------


def test_f_a_list_with_rewritten_r_values_is_not_evaluable() -> None:
    edited = monte_carlo(sum_r=600.0)  # same 600 rows, every R rewritten
    g4 = gate_monte_carlo(inputs(replaced(passing_set(), MC, edited)))
    assert g4.gate.status == "NOT_EVALUABLE"
    assert "R toplamı" in g4.gate.evidence


def test_f_a_list_with_rewritten_mae_values_is_not_evaluable() -> None:
    edited = monte_carlo(sum_mae_r=0.0)  # no floating loss any more
    g4 = gate_monte_carlo(inputs(replaced(passing_set(), MC, edited)))
    assert g4.gate.status == "NOT_EVALUABLE"
    assert "mae_r" in g4.gate.evidence


def test_f_an_older_monte_carlo_without_sums_is_not_evaluable() -> None:
    old = monte_carlo(sums=False)
    g4 = gate_monte_carlo(inputs(replaced(passing_set(), MC, old)))
    assert g4.gate.status == "NOT_EVALUABLE"


def test_f_the_sum_tolerance_is_float_rounding_only() -> None:
    # 180 is the exact total of the two decision runs; one part in 1e9 more is an edit.
    near = monte_carlo(sum_r=180.0 * (1 + 1e-9))
    g4 = gate_monte_carlo(inputs(replaced(passing_set(), MC, near)))
    assert g4.gate.status == "NOT_EVALUABLE"
    exact = monte_carlo(sum_r=math.fsum([90.0, 90.0]))
    assert gate_monte_carlo(inputs(replaced(passing_set(), MC, exact))).gate.status == "PASS"


# --- B, C, F through the real montecarlo command ----------------------------------------------


def _trade_list(tmp_path: Path, *extra: str) -> Path:
    trades = tmp_path / "trades.csv"
    arguments = ["backtest", "--data", str(BARS), "--config-dir", str(CONFIG_DIR)]
    arguments += ["--strategy", "ny_orb_fvg", "--no-artifact", "--trades-out", str(trades)]
    assert main([*arguments, *extra], clock=clock()) == 0
    return trades


def _montecarlo(trades: Path, reports: Path, *extra: str) -> int:
    arguments = ["montecarlo", "--trades", str(trades), "--venue", str(VENUE_FILE)]
    arguments += ["--risk-config", str(CONFIG_DIR / "risk.yaml")]
    arguments += ["--paths", "200", "--max-days", "60", "--dd-days", "30"]
    return main([*arguments, "--reports-dir", str(reports), *extra], clock=clock())


def test_bcf_the_montecarlo_artifact_records_rules_model_and_sums(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    trades = _trade_list(tmp_path)
    reports = tmp_path / "reports"

    assert _montecarlo(trades, reports, "--r-is-net", "--intratrade", "closed") == 0

    capsys.readouterr()
    (written,) = reports.iterdir()
    artifact = load_artifact(written)
    assert isinstance(artifact, MonteCarloArtifact)
    rules = artifact.inputs.rules
    assert rules.max_loss_type == VENUE.max_loss_type.value
    assert rules.daily_loss_basis == VENUE.daily_loss_basis.value
    assert (rules.daily_reset_tz, rules.daily_reset_time) == (
        VENUE.daily_reset.tz,
        VENUE.daily_reset.time,
    )
    assert artifact.inputs.intratrade == "closed"
    rows = [line.split(",") for line in trades.read_text(encoding="utf-8").splitlines()[2:]]
    assert artifact.inputs.sum_r == math.fsum(float(row[2]) for row in rows)
    assert artifact.inputs.sum_mae_r == math.fsum(float(row[4]) for row in rows)


def test_f_the_backtest_artifact_records_the_mae_total_the_list_must_match(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    reports = tmp_path / "reports"
    trades = tmp_path / "trades.csv"
    arguments = ["backtest", "--data", str(BARS), "--config-dir", str(CONFIG_DIR)]
    arguments += ["--strategy", "ny_orb_fvg", "--reports-dir", str(reports)]
    assert main([*arguments, "--trades-out", str(trades)], clock=clock()) == 0
    capsys.readouterr()
    (written,) = reports.iterdir()
    bt = load_artifact(written)
    assert isinstance(bt, BacktestArtifact)
    rows = [line.split(",") for line in trades.read_text(encoding="utf-8").splitlines()[2:]]
    assert bt.mae_total_r == math.fsum(float(row[4]) for row in rows)
    assert bt.stats_all.total_r == math.fsum(float(row[2]) for row in rows)


# --- H: a cost-free list read as net ---------------------------------------------------------


def test_h_a_cost_free_list_is_never_reported_as_after_costs(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    trades = _trade_list(tmp_path, "--cost-free")
    capsys.readouterr()

    assert _montecarlo(trades, tmp_path / "reports", "--r-is-net", "--no-artifact") == 0

    out = capsys.readouterr().out
    assert "MALİYETSİZ işlem listesi" in out
    assert "maliyet dahil" not in out
    assert "(maliyet sonrası)" not in out


# --- J: compliance directory -----------------------------------------------------------------


def test_j_a_missing_compliance_directory_is_named_with_its_resolved_path(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    work = tmp_path / "elsewhere"
    work.mkdir()
    monkeypatch.chdir(work)
    reports = tmp_path / "reports"
    arguments = ["gonogo", "--strategies", "ny_orb_fvg", "--config-dir", str(CONFIG_DIR)]

    assert main([*arguments, "--reports-dir", str(reports)], clock=clock()) == 1

    capsys.readouterr()
    (report,) = (path for path in reports.iterdir() if path.suffix == ".json")
    g7 = next(gate for gate in json.loads(report.read_text("utf-8"))["gates"] if gate["id"] == "G7")
    assert str(work / "docs" / "compliance") in g7["evidence"]
    assert "klasör yok" in g7["evidence"]
    assert "dosya yok" not in g7["evidence"]


def test_j_the_g7_evidence_shows_the_directory_it_listed() -> None:
    g7 = gate_rules(
        inputs(compliance_dir="/srv/propbot/docs/compliance", compliance_dir_found=True)
    )
    assert "/srv/propbot/docs/compliance" in g7.gate.evidence
    missing = gate_rules(
        inputs(compliance_files=(), compliance_dir="/x/docs/compliance", compliance_dir_found=False)
    )
    assert "/x/docs/compliance: klasör yok" in missing.gate.evidence


# SHA keeps the helpers' default data visible to a reader of this file.
assert SHA != OTHER_SHA


# --- older files: they still load, and they never pass ----------------------------------------

_NEW_FIELDS = {
    "backtest": ("mae_total_r",),
    "monte_carlo": ("intratrade", "sum_r", "sum_mae_r"),
    "parity": ("params", "instrument"),
}
_NEW_RULE_FIELDS = ("max_loss_type", "daily_loss_basis", "daily_reset_tz", "daily_reset_time")


def test_older_files_without_the_new_fields_load_and_block_their_gates(tmp_path: Path) -> None:
    for item in passing_set():
        raw = item.artifact.model_dump(mode="json")
        for name in _NEW_FIELDS.get(raw["kind"], ()):
            target = raw["inputs"] if raw["kind"] == "monte_carlo" else raw
            target.pop(name)
        if raw["kind"] == "monte_carlo":
            for name in _NEW_RULE_FIELDS:
                raw["inputs"]["rules"].pop(name)
        (tmp_path / item.file_name).write_text(json.dumps(raw), encoding="utf-8")

    scan = all_artifacts(tmp_path)

    assert scan.problems == ()
    assert len(scan.artifacts) == len(passing_set())
    report = evaluate(inputs(list(scan.artifacts)))
    by_id = {gate.id: gate for gate in report.gates}
    for gate_id in ("G4", "G6"):
        assert by_id[gate_id].status == "NOT_EVALUABLE", by_id[gate_id].evidence
        assert "eski rapor" in by_id[gate_id].evidence
