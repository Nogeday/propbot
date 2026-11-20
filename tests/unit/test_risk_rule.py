"""The planned-risk rule (user decision of 2026-11-19, open finding 82).

The Pine scripts default to 0.5 % risk per trade; the account is planned at
config/risk.yaml's `risk_per_trade_pct` (0.25 %). For live operation and every
portfolio-level judgement the planned risk holds, and the evidence is produced at it:

- `propbot backtest` sizes its runs at the planned risk (the decision run); `--pine-risk`
  keeps the script's own value and is a labelled comparison run;
- config/strategies.yaml may not set a port's risk input (one source for the risk);
- Go/No-Go judges decision runs against the parameters WITH the planned risk, parity
  reports against the parameters at the script's own risk, and G4 closes the chain
  "decision runs at the planned risk -> trade list -> Monte Carlo at the planned risk";
- `propbot parity` always runs at the script's own risk.

Everything here runs on the SYNTHETIC fixtures: nothing below is evidence of an edge.
"""

import dataclasses
import datetime as dt
import functools
import math
import shutil
from collections.abc import Callable
from pathlib import Path
from typing import Any

import pytest
from hypothesis import given, settings
from hypothesis import strategies as st

from propbot.backtest.artifacts import (
    BacktestArtifact,
    CostMatrixArtifact,
    GoNoGoArtifact,
    ParityArtifact,
    WalkForwardArtifact,
    load_artifact,
)
from propbot.backtest.command import expected_run_params
from propbot.backtest.config import load_backtest_config
from propbot.backtest.gonogo import (
    EXPECTED_PARITY_PARAMS_MISSING,
    PARITY_RISK_SENTENCE,
    RISK_PARAMS_MISSING,
    decision_risk_checks,
    evaluate,
    gate_data,
    gate_edge,
    gate_monte_carlo,
    gate_parity,
    gate_robustness,
)
from propbot.backtest.model import CostModel, FillModel
from propbot.backtest.report import risk_shown
from propbot.backtest.runner import (
    RunnerError,
    RunRequest,
    backtest_artifacts,
    cost_matrix_artifacts,
    effective_params,
    merge_runs,
    render_summary,
    run,
    run_cost_sensitivity,
    walk_forward_artifacts,
    walk_forward_legs,
)
from propbot.backtest.walkforward import WalkForwardConfig
from propbot.cli import main
from propbot.core.bars import BarSeries
from propbot.dashboard.analysis import run_kind
from propbot.data.ohlcv import load_ohlcv
from propbot.risk.errors import ConfigError
from propbot.strategies.config import load_strategy_overrides
from propbot.strategies.registry import STRATEGIES, StrategyError, strategy_spec
from support.engine import mes
from support.paths import CONFIG_DIR, FIXTURES
from unit.test_gonogo import (
    EXPECTED_PARAMS,
    ORB_BT,
    ORB_PARITY,
    PARAMS,
    PARITY_PARAMS,
    PINE_RISK_PCT,
    PLANNED_RISK_PCT,
    inputs,
    passing_set,
    replaced,
    status,
)
from unit.test_gonogo import backtest as backtest_report
from unit.test_gonogo import monte_carlo as monte_carlo_report
from unit.test_gonogo import parity as parity_report

BARS = FIXTURES / "ohlcv" / "synthetic_5m.csv"
SAMPLE_TV = FIXTURES / "parity" / "sample_tv_basic_comma.csv"
CREATED = dt.datetime(2026, 11, 18, 12, 0, tzinfo=dt.UTC)
START = dt.datetime(2026, 11, 18, 9, 0, tzinfo=dt.UTC)
MADE = dt.datetime(2026, 11, 17, 10, 0, 0, tzinfo=dt.UTC)
OOS = "2026-11-01"  # inside the fixture's span (2026-10-19 → 2026-11-13)
OOS_MS = int(dt.datetime(2026, 11, 1, tzinfo=dt.UTC).timestamp()) * 1000
# The venue profile's initial_balance: what the CLI sizes every run from.
CAPITAL = 100_000.0
MASTER = "master_portfolio"
MASTER_LEGS = ("master_ny_orb_fvg", "master_asian_sweep")


@functools.cache
def fixture() -> BarSeries:
    return load_ohlcv(BARS)


@functools.cache
def short_fixture() -> BarSeries:
    """The first days of the fixture: enough for a run to record its parameters."""
    series = fixture()
    return dataclasses.replace(series, bars=series.bars[:600])


@functools.cache
def mes_costs() -> CostModel:
    return load_backtest_config(CONFIG_DIR / "backtest.yaml").cost_model("MES")


def request(
    strategy_id: str,
    planned_risk_pct: float | None,
    *,
    series: BarSeries | None = None,
    capital: float = CAPITAL,
    **changes: Any,
) -> RunRequest:
    base = RunRequest(
        series=fixture() if series is None else series,
        strategy_id=strategy_id,
        instrument=mes(),
        base_costs=mes_costs(),
        initial_capital=capital,
        oos_start_ms=OOS_MS,
        min_gap_seconds=1800,
        planned_risk_pct=planned_risk_pct,
    )
    return dataclasses.replace(base, **changes)


# --- why the rule exists: R is not size-free ------------------------------------------------


def test_the_same_port_on_the_same_bars_makes_another_trade_list_at_half_the_risk() -> None:
    # MTF Trend on the 5-minute fixture at the venue's 100,000 balance, sized at the
    # script's 0.5 % and at the planned 0.25 %. The entries are the same 20 bars, but the
    # first one is 17 contracts at 0.5 % and 8 at 0.25 %: 8.5 is floored to the contract
    # step. The script scales half out at TP1 only when the size is even, so the odd 17
    # rides whole to its stop and the even 8 banks half at the target first. Same signal,
    # other exits, another R: a list made at 0.5 % is not the list a 0.25 % account has.
    at_pine = run(request("mtf_trend_cont", PINE_RISK_PCT))
    at_planned = run(request("mtf_trend_cont", PLANNED_RISK_PCT))

    assert [e.entry_ts_ms for e in at_pine.entries] == [e.entry_ts_ms for e in at_planned.entries]
    assert len(at_pine.entries) == 20
    first_pine, first_planned = at_pine.entries[0], at_planned.entries[0]
    assert (first_pine.qty, first_planned.qty) == (17.0, 8.0)
    assert first_planned.qty != first_pine.qty / 2  # floored, not halved
    assert [reason.value for reason in first_pine.reasons] == ["stop"]
    assert [reason.value for reason in first_planned.reasons] == ["target", "stop"]
    assert first_pine.r != first_planned.r
    # Not one odd trade: the lists differ in many rows, and in the number of exit fills.
    differing = [
        (a.r, b.r)
        for a, b in zip(at_pine.entries, at_planned.entries, strict=True)
        if not math.isclose(a.r, b.r, rel_tol=1e-9, abs_tol=1e-12)
    ]
    assert len(differing) >= 5
    assert len(at_pine.engine.trades) != len(at_planned.engine.trades)
    total_pine = math.fsum(e.r for e in at_pine.entries if e.complete)
    total_planned = math.fsum(e.r for e in at_planned.entries if e.complete)
    assert abs(total_pine - total_planned) > 0.5


def test_a_size_below_one_contract_skips_a_trade_the_larger_risk_takes() -> None:
    # The second way R is not size-free: on a 10,000 account 0.25 % is 25 units of risk,
    # and a stop wider than that per contract gives a size below the minimum, so the
    # script takes no trade where the 0.5 % run (50 units) still fits one contract.
    at_pine = run(request("mr_bb_kc_rsi2", PINE_RISK_PCT, capital=10_000.0))
    at_planned = run(request("mr_bb_kc_rsi2", PLANNED_RISK_PCT, capital=10_000.0))

    assert (len(at_pine.entries), len(at_planned.entries)) == (6, 5)
    skipped = {e.entry_ts_ms for e in at_pine.entries} - {e.entry_ts_ms for e in at_planned.entries}
    assert len(skipped) == 1


# --- the registry names the risk input, the runner writes the planned risk into it ---------


@pytest.mark.parametrize("strategy_id", sorted(STRATEGIES))
def test_a_run_with_a_planned_risk_records_it_and_is_the_decision_run(strategy_id: str) -> None:
    spec = strategy_spec(strategy_id)
    result = run(request(strategy_id, PLANNED_RISK_PCT, series=short_fixture()))

    assert result.decision
    assert result.label_suffix == ""
    assert result.params[spec.risk_param] == PLANNED_RISK_PCT
    # Every other parameter is the script's own.
    defaults = spec.params().model_dump(mode="json")
    assert {k: v for k, v in result.params.items() if k != spec.risk_param} == {
        k: v for k, v in defaults.items() if k != spec.risk_param
    }
    for artifact in backtest_artifacts(result, created=CREATED):
        assert artifact.params[spec.risk_param] == PLANNED_RISK_PCT
        assert artifact.label == artifact.strategy_id
        assert any("planlanan risk (config/risk.yaml" in note for note in artifact.notes)
    summary = render_summary(result)
    assert "KARAR KOŞUSU DEĞİL" not in summary
    assert (
        f"İşlem başı risk: %0,25 — planlanan risk (config/risk.yaml risk_per_trade_pct), betiğin "
        f"{spec.risk_param} girdisine yazıldı; Pine varsayılanı %0,5" in summary
    )


@pytest.mark.parametrize("strategy_id", sorted(STRATEGIES))
def test_a_run_without_a_planned_risk_keeps_the_pine_risk_and_is_never_a_decision_run(
    strategy_id: str,
) -> None:
    spec = strategy_spec(strategy_id)
    result = run(request(strategy_id, None, series=short_fixture()))

    assert not result.decision
    assert result.label_suffix == "-pinerisk"
    assert result.params == spec.params().model_dump(mode="json")
    assert result.params[spec.risk_param] == PINE_RISK_PCT
    assert result.not_decision_reasons == (
        "işlem başı risk Pine varsayılanı (%0,5); karar koşusu config/risk.yaml riskiyle yapılır",
    )
    for artifact in backtest_artifacts(result, created=CREATED):
        assert artifact.label == artifact.strategy_id + "-pinerisk"
        assert any("KARŞILAŞTIRMA KOŞUSU" in note for note in artifact.notes)
    summary = render_summary(result)
    assert "! KARAR KOŞUSU DEĞİL (Go/No-Go bu koşuyu kullanmaz): işlem başı risk Pine" in summary
    assert f"betiğin kendi değeri (Pine varsayılanı, {spec.risk_param})" in summary


def test_the_pine_risk_suffix_combines_with_every_other_comparison_suffix() -> None:
    configured = OOS_MS + 86_400_000
    result = run(
        request(
            "ny_orb_fvg",
            None,
            series=short_fixture(),
            fill_model=FillModel.TV_OHLC,
            cost_factor=1.5,
            configured_oos_start_ms=configured,
        )
    )

    assert result.label_suffix == "-tv-x1p5-oos20261101-pinerisk"
    assert len(result.not_decision_reasons) == 4
    (artifact,) = backtest_artifacts(result, created=CREATED)
    assert artifact.label == "ny_orb_fvg-tv-x1p5-oos20261101-pinerisk"


def test_cost_matrix_walk_forward_and_trade_list_follow_the_risk_of_their_run() -> None:
    pine = run(request("ny_orb_fvg", None))
    sensitivity = run_cost_sensitivity(pine, (1.0, 1.5))
    folds = walk_forward_legs(pine, WalkForwardConfig(is_days=10, oos_days=5, step_days=5))

    # Every factor of the matrix is the same run again: the risk does not change with it.
    assert [r.request.planned_risk_pct for r in sensitivity.runs] == [None, None]
    assert [a.label for a in cost_matrix_artifacts(sensitivity, created=CREATED)] == [
        "ny_orb_fvg-pinerisk-cost-all",
        "ny_orb_fvg-pinerisk-cost-oos",
    ]
    (walk,) = walk_forward_artifacts(pine, folds, created=CREATED)
    assert walk.label == "ny_orb_fvg-pinerisk"
    assert any("KARŞILAŞTIRMA KOŞUSU" in note for note in walk.notes)

    planned = run(request("asian_sweep", PLANNED_RISK_PCT))
    planned_matrix = run_cost_sensitivity(planned, (1.0, 1.5))
    assert [r.request.planned_risk_pct for r in planned_matrix.runs] == [PLANNED_RISK_PCT] * 2
    assert [r.params["risk_pct"] for r in planned_matrix.runs] == [PLANNED_RISK_PCT] * 2
    # One trade list is one account: runs sized at two risks are never merged into it.
    with pytest.raises(RunnerError, match="işlem başı riskleri farklı"):
        merge_runs([pine, planned], created=CREATED)
    other = run(request("asian_sweep", 0.3))
    with pytest.raises(RunnerError, match="işlem başı riskleri farklı"):
        merge_runs([run(request("ny_orb_fvg", PLANNED_RISK_PCT)), other], created=CREATED)


@pytest.mark.parametrize(
    ("strategy_id", "planned", "words"),
    [
        # 0.001 % is below every script's minimum for the input.
        ("ny_orb_fvg", 0.001, "en az 0.1 olmalı"),
        ("mtf_trend_cont", 0.001, "en az 0.01 olmalı"),
        (MASTER, 0.05, "en az 0.1 olmalı"),
    ],
)
def test_a_planned_risk_the_port_refuses_is_a_turkish_error_not_a_silent_clamp(
    strategy_id: str, planned: float, words: str
) -> None:
    spec = strategy_spec(strategy_id)

    with pytest.raises(RunnerError) as caught:
        run(request(strategy_id, planned, series=short_fixture()))

    message = str(caught.value)
    assert message.startswith(f"{strategy_id}: planlanan işlem başı risk {risk_shown(planned)}")
    assert "config/risk.yaml risk_per_trade_pct" in message
    assert spec.risk_param in message
    assert words in message
    assert "Değer sessizce değiştirilmez" in message
    # The same refusal when Go/No-Go asks what a decision run would have recorded.
    with pytest.raises(RunnerError, match="planlanan işlem başı risk"):
        effective_params(spec, planned_risk_pct=planned)


@pytest.mark.parametrize("planned", [0.0, -0.25, math.nan, math.inf, True])
def test_a_planned_risk_that_is_not_a_positive_finite_percent_is_refused(planned: float) -> None:
    with pytest.raises(RunnerError, match="planlanan işlem başı risk pozitif, sonlu"):
        run(request("ny_orb_fvg", planned, series=short_fixture()))


@pytest.mark.parametrize("strategy_id", sorted(STRATEGIES))
def test_the_risk_input_cannot_reach_a_run_as_an_override(strategy_id: str) -> None:
    # Otherwise "the script's own risk" (no planned risk) could be any number, and a
    # planned risk would silently lose against a second source.
    spec = strategy_spec(strategy_id)
    override = {spec.risk_param: 0.3}

    for planned in (None, PLANNED_RISK_PCT):
        with pytest.raises(RunnerError, match=r"tek kaynak config/risk\.yaml"):
            run(request(strategy_id, planned, series=short_fixture(), overrides=override))
    with pytest.raises(StrategyError, match="--pine-risk"):
        spec.with_planned_risk(override, None)


def test_the_master_portfolio_gets_the_planned_risk_once_and_keeps_its_own_cap() -> None:
    spec = strategy_spec(MASTER)
    planned = run(request(MASTER, PLANNED_RISK_PCT))
    pine = run(request(MASTER, None))

    # One shared risk input for both engines: every view of the run records the same value.
    artifacts = backtest_artifacts(planned, created=CREATED)
    assert [a.strategy_id for a in artifacts] == [*MASTER_LEGS, MASTER]
    assert [a.label for a in artifacts] == [*MASTER_LEGS, MASTER]
    for artifact in artifacts:
        assert artifact.params["risk_pct"] == PLANNED_RISK_PCT
        # The cap on both engines together is another input: not touched by the rule.
        assert artifact.params["max_concurrent_risk_pct"] == 1.0
    assert spec.risk_cap_param == "max_concurrent_risk_pct"
    assert (
        "eşzamanlı risk tavanı (max_concurrent_risk_pct, %1,0) betiğin kendi girdisidir, "
        "planlanan riskle değiştirilmedi" in render_summary(planned)
    )
    assert [a.label for a in backtest_artifacts(pine, created=CREATED)] == [
        "master_ny_orb_fvg-pinerisk",
        "master_asian_sweep-pinerisk",
        "master_portfolio-pinerisk",
    ]
    assert "eşzamanlı risk tavanı" in render_summary(pine)
    # A single-strategy script has no such cap, and its summary does not invent one.
    assert "eşzamanlı risk tavanı" not in render_summary(
        run(request("ny_orb_fvg", PLANNED_RISK_PCT, series=short_fixture()))
    )


# --- one source for the risk: config/strategies.yaml may not set it -------------------------


@pytest.mark.parametrize("strategy_id", sorted(STRATEGIES))
@pytest.mark.parametrize("value", [0.5, 0.25])
def test_strategies_yaml_may_not_set_a_ports_risk_input(
    tmp_path: Path, strategy_id: str, value: float
) -> None:
    spec = strategy_spec(strategy_id)
    path = tmp_path / "strategies.yaml"
    # Refused whatever the value, the Pine default and the planned risk included: the
    # key itself is the second source.
    path.write_text(f"strategies:\n  {strategy_id}:\n    {spec.risk_param}: {value}\n", "utf-8")

    with pytest.raises(ConfigError) as caught:
        load_strategy_overrides(path)

    (issue,) = caught.value.issues
    assert issue.location == f"strategies.{strategy_id}.{spec.risk_param}"
    assert "config/risk.yaml (risk_per_trade_pct)" in issue.message
    assert "--pine-risk" in issue.message


def test_the_mtf_port_is_refused_under_its_own_name_and_every_other_key_still_works(
    tmp_path: Path,
) -> None:
    assert strategy_spec("mtf_trend_cont").risk_param == "risk_percent"
    path = tmp_path / "strategies.yaml"
    path.write_text(
        "strategies:\n"
        "  mtf_trend_cont: {risk_percent: 0.25, tp1_rr: -1}\n"
        "  master_portfolio: {max_concurrent_risk_pct: 0.5, rr: 2.5}\n"
        "  ny_orb_fvg: {rr: 3.0}\n",
        "utf-8",
    )

    with pytest.raises(ConfigError) as caught:
        load_strategy_overrides(path)

    # Both mistakes of the entry are reported at once; the other entries are not touched.
    assert sorted(issue.location for issue in caught.value.issues) == [
        "strategies.mtf_trend_cont.risk_percent",
        "strategies.mtf_trend_cont.tp1_rr",
    ]
    path.write_text(
        "strategies:\n"
        "  master_portfolio: {max_concurrent_risk_pct: 0.5, rr: 2.5}\n"
        "  ny_orb_fvg: {rr: 3.0}\n",
        "utf-8",
    )
    overrides = load_strategy_overrides(path)
    assert dict(overrides[MASTER]) == {"max_concurrent_risk_pct": 0.5, "rr": 2.5}
    assert dict(overrides["ny_orb_fvg"]) == {"rr": 3.0}


# --- the Go/No-Go command and the runner use one function -----------------------------------


@pytest.mark.parametrize("strategy_id", sorted(STRATEGIES))
@settings(max_examples=8, deadline=None)
@given(planned=st.one_of(st.none(), st.floats(min_value=0.1, max_value=0.5)))
def test_what_gonogo_expects_is_what_the_runner_recorded(
    strategy_id: str, planned: float | None
) -> None:
    result = run(request(strategy_id, planned, series=short_fixture()))

    for artifact in backtest_artifacts(result, created=CREATED):
        # A Master leg's artifact is looked up by the leg id, as `propbot gonogo` does.
        sid = artifact.strategy_id
        expected = expected_run_params((sid,), {}, planned)[sid]
        assert artifact.params == expected
        # ...and the two sets Go/No-Go holds differ in the risk input alone.
        decision = expected_run_params((sid,), {}, PLANNED_RISK_PCT)[sid]
        parity = expected_run_params((sid,), {}, None)[sid]
        risk_param = result.spec.risk_param
        assert {k for k in decision if decision[k] != parity[k]} == {risk_param}
        assert (decision[risk_param], parity[risk_param]) == (PLANNED_RISK_PCT, PINE_RISK_PCT)


def test_expected_params_carry_the_configured_overrides_under_both_risks() -> None:
    overrides = {"ny_orb_fvg": {"rr": 3.0}}
    result = run(
        request("ny_orb_fvg", PLANNED_RISK_PCT, series=short_fixture(), overrides={"rr": 3.0})
    )

    decision = expected_run_params(("ny_orb_fvg",), overrides, PLANNED_RISK_PCT)["ny_orb_fvg"]
    parity = expected_run_params(("ny_orb_fvg",), overrides, None)["ny_orb_fvg"]
    assert result.params == decision
    assert (decision["rr"], decision["risk_pct"]) == (3.0, PLANNED_RISK_PCT)
    assert (parity["rr"], parity["risk_pct"]) == (3.0, PINE_RISK_PCT)


# --- Go/No-Go: decision runs at the planned risk -----------------------------------------------


def test_a_run_at_the_pine_risk_is_a_comparison_run_and_hides_no_older_decision_run() -> None:
    pine = backtest_report(
        label="orb-pinerisk",
        params={**PARAMS, "risk_pct": PINE_RISK_PCT},
        created=MADE + dt.timedelta(hours=1),
    )

    # Newer than the decision run: it is skipped, never a blocker.
    for gate in (gate_data, gate_edge, gate_robustness, gate_monte_carlo):
        result = gate(inputs([*passing_set(), pine]))
        assert status(result) == "PASS", result.gate.evidence
        assert pine.file_name not in result.gate.evidence
    # Alone it decides nothing, and the evidence names both risks.
    alone = gate_edge(inputs(replaced(passing_set(), ORB_BT, pine)))
    assert status(alone) == "FAIL"
    assert "yalnızca karşılaştırma koşusu var" in alone.gate.evidence
    assert "işlem başı risk %0,5 (planlanan risk %0,25, config/risk.yaml)" in alone.gate.evidence
    for gate in (gate_data, gate_robustness):
        assert status(gate(inputs(replaced(passing_set(), ORB_BT, pine)))) == "NOT_EVALUABLE"


def test_when_the_planned_risk_changes_the_old_decision_runs_stop_deciding() -> None:
    # config/risk.yaml moved from 0.25 to 0.2 after the runs were made: they were sized
    # for another account. Told as the CLI would tell it (expected params at the new risk).
    expected = {sid: {**PARAMS, "risk_pct": 0.2} for sid in EXPECTED_PARAMS}
    case = inputs(planned_risk_pct=0.2, expected_params=expected)

    report = evaluate(case)

    by_id = {gate.id: gate for gate in report.gates}
    assert report.overall == "NO_GO"
    for gate_id in ("G1", "G2", "G3", "G4"):
        assert by_id[gate_id].status != "PASS"
    assert "işlem başı risk %0,25 (planlanan risk %0,2, config/risk.yaml)" in by_id["G2"].evidence
    assert "karar koşusu olmayan strateji" in by_id["G4"].evidence


@pytest.mark.parametrize(
    ("changes", "words"),
    [
        ({"risk_params": None}, RISK_PARAMS_MISSING),
        (
            {"risk_params": {"asian_sweep": "risk_pct"}},
            "'ny_orb_fvg' için işlem başı risk girdisinin adı verilmedi",
        ),
        # The name is known but the run does not record it: unknown is not "planned".
        (
            {"risk_params": {"ny_orb_fvg": "risk_percent", "asian_sweep": "risk_pct"}},
            "kayıtlı parametrelerde işlem başı risk (risk_percent) yok ya da sayı değil",
        ),
    ],
)
def test_an_unknown_risk_input_confirms_no_decision_run(
    changes: dict[str, object], words: str
) -> None:
    case = inputs(**changes)

    for gate in (gate_data, gate_edge, gate_robustness, gate_monte_carlo):
        result = gate(case)
        assert status(result) == "NOT_EVALUABLE"
    edge = gate_edge(case)
    assert words in edge.gate.evidence
    assert "daha eski bir koşuya geçilmez" in edge.gate.evidence
    assert evaluate(case).overall == "NO_GO"


@pytest.mark.parametrize("recorded", ["0.25", True, None])
def test_a_recorded_risk_that_is_not_a_number_blocks(recorded: object) -> None:
    # (The artifact model itself refuses a non-finite number in `params`.)
    odd = backtest_report(params={**PARAMS, "risk_pct": recorded})  # type: ignore[dict-item]

    result = gate_edge(inputs(replaced(passing_set(), ORB_BT, odd)))

    assert status(result) == "NOT_EVALUABLE"
    assert "işlem başı risk (risk_pct) yok ya da sayı değil" in result.gate.evidence


# --- G4: the risk chain in one place ------------------------------------------------------------


def test_g4_states_the_closed_risk_chain_when_it_holds() -> None:
    result = gate_monte_carlo(inputs())

    assert status(result) == "PASS"
    assert (
        "risk zinciri: karar koşuları planlanan riskle (%0,25, config/risk.yaml) boyutlandırıldı, "
        "işlem listesi o koşulardan, simülasyon aynı riskte" in result.gate.evidence
    )


def test_g4_needs_the_simulation_at_the_planned_risk() -> None:
    result = gate_monte_carlo(
        inputs(replaced(passing_set(), "monte_carlo_", monte_carlo_report(risk_pct=PINE_RISK_PCT)))
    )

    assert status(result) == "NOT_EVALUABLE"
    # Both risks are written exactly since the attack of 2026-11-19 ("%0,5", no longer
    # rounded to "%0,50"): at two decimals 0.25 and 0.250000001 read as the same number.
    assert "simülasyon riski %0,5, planlanan risk %0,25" in result.gate.evidence
    assert "risk zinciri:" not in result.gate.evidence


def test_g4_needs_every_decision_run_at_the_planned_risk() -> None:
    pine = backtest_report(params={**PARAMS, "risk_pct": PINE_RISK_PCT})
    case = inputs(replaced(passing_set(), ORB_BT, pine))

    result = gate_monte_carlo(case)

    # Through the gate: such a run is no decision run, so the list hangs on nothing.
    assert status(result) == "NOT_EVALUABLE"
    assert "karar koşusu olmayan strateji: ny_orb_fvg" in result.gate.evidence
    assert "risk zinciri:" not in result.gate.evidence
    # The chain's own check, on the runs handed to it: both values are named.
    (check,) = decision_risk_checks(case, [pine])
    assert check[0] == "NOT_EVALUABLE"
    assert pine.file_name in check[1]
    assert "karar koşusunun işlem başı riski %0,5, planlanan risk %0,25" in check[1]
    assert decision_risk_checks(case, [backtest_report("asian_sweep")]) == []
    (unknown,) = decision_risk_checks(inputs(risk_params=None), [backtest_report()])
    assert unknown[0] == "NOT_EVALUABLE"
    assert RISK_PARAMS_MISSING in unknown[1]


# --- G6: parity is judged at the script's own risk ----------------------------------------------


def test_g6_passes_a_parity_report_at_the_pine_risk_and_says_why() -> None:
    result = gate_parity(inputs())

    assert status(result) == "PASS"
    assert PARITY_RISK_SENTENCE in result.gate.evidence
    assert "2026-11-19 kullanıcı kararı" in PARITY_RISK_SENTENCE


def test_g6_judged_against_the_decision_set_would_refuse_every_honest_parity_report() -> None:
    # The decision set carries the planned risk; a parity run never does.
    result = gate_parity(inputs(expected_parity_params=EXPECTED_PARAMS))

    assert status(result) == "NOT_EVALUABLE"
    assert "aynı değil: risk_pct (parite betiğin kendi riskiyle koşulur)" in result.gate.evidence


def test_g6_refuses_a_parity_report_made_at_the_planned_risk() -> None:
    at_planned = parity_report(params=dict(PARAMS))

    result = gate_parity(inputs(replaced(passing_set(), ORB_PARITY, at_planned)))

    assert status(result) == "NOT_EVALUABLE"
    assert "aynı değil: risk_pct" in result.gate.evidence


def test_g6_without_its_own_expected_set_is_not_evaluable() -> None:
    absent = gate_parity(inputs(expected_parity_params=None))
    assert status(absent) == "NOT_EVALUABLE"
    assert EXPECTED_PARITY_PARAMS_MISSING in absent.gate.evidence
    # The decision set being known does not help: G6 never falls back to it.
    assert inputs().expected_params is not None

    partial = gate_parity(inputs(expected_parity_params={"asian_sweep": dict(PARITY_PARAMS)}))
    assert status(partial) == "NOT_EVALUABLE"
    assert "'ny_orb_fvg' için beklenen parite parametreleri verilmedi" in partial.gate.evidence


# --- the commands ---------------------------------------------------------------------------


GONOGO_ORB = ("gonogo", "--strategies", "ny_orb_fvg")


def clock(offset_s: int = 0) -> Callable[[], dt.datetime]:
    moments = iter(START + dt.timedelta(seconds=offset_s + step) for step in range(10_000))
    return lambda: next(moments)


@pytest.fixture
def config_dir(tmp_path: Path) -> Path:
    """A copy of config/ whose OOS start lies inside the fixture (so runs can decide)."""
    target = tmp_path / "config"
    shutil.copytree(CONFIG_DIR, target)
    path = target / "backtest.yaml"
    text = path.read_text(encoding="utf-8")
    assert 'oos_start: "2025-01-01"' in text
    path.write_text(text.replace('oos_start: "2025-01-01"', f'oos_start: "{OOS}"'), "utf-8")
    return target


def backtest(
    reports: Path, config: Path, *extra: str, now: Callable[[], dt.datetime] | None = None
) -> int:
    arguments = ["backtest", "--data", str(BARS), "--config-dir", str(config)]
    arguments += ["--reports-dir", str(reports)]
    if "--strategy" not in extra:
        arguments += ["--strategy", "ny_orb_fvg"]
    return main([*arguments, *extra], clock=now or clock())


def gonogo(reports: Path, config: Path, strategies: str = "ny_orb_fvg") -> GoNoGoArtifact:
    arguments = ["gonogo", "--strategies", strategies, "--config-dir", str(config)]
    code = main([*arguments, "--reports-dir", str(reports)], clock=clock(3_600))
    assert code == 1  # synthetic bars: never GO
    (path,) = (
        p for p in reports.iterdir() if p.name.startswith("go_no_go_") and p.suffix == ".json"
    )
    artifact = load_artifact(path)
    assert isinstance(artifact, GoNoGoArtifact)
    assert artifact.overall == "NO_GO"
    return artifact


def part(report: GoNoGoArtifact, gate_id: str, subject: str) -> dict[str, Any]:
    (gate,) = (g for g in report.gates if g.id == gate_id)
    parts = gate.details["parts"]
    assert isinstance(parts, list)
    (found,) = (p for p in parts if isinstance(p, dict) and p["subject"] == subject)
    return found


def backtests(reports: Path) -> dict[str, BacktestArtifact]:
    found = {}
    for path in sorted(reports.iterdir()):
        if path.name.startswith("backtest_"):
            artifact = load_artifact(path)
            assert isinstance(artifact, BacktestArtifact)
            found[path.name] = artifact
    return found


def test_the_default_backtest_reads_risk_yaml_and_writes_the_decision_run(
    tmp_path: Path, config_dir: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    reports = tmp_path / "reports"

    assert backtest(reports, config_dir) == 0

    out = capsys.readouterr().out
    ((name, artifact),) = backtests(reports).items()
    assert name == "backtest_ny_orb_fvg_20261118T090000Z.json"
    assert artifact.params["risk_pct"] == PLANNED_RISK_PCT  # config/risk.yaml, not Pine's 0.5
    assert "KARAR KOŞUSU DEĞİL" not in out
    assert "İşlem başı risk: %0,25 — planlanan risk (config/risk.yaml risk_per_trade_pct)" in out
    # Go/No-Go judges it as the decision run: its parameters are what the command expects.
    report = gonogo(reports, config_dir)
    g2 = part(report, "G2", "ny_orb_fvg")
    assert g2["values"]["backtest_file"] == name
    assert "SENTETİK VERİ" in str(g2["reasons"])
    assert "aynı değil" not in str(g2["reasons"])
    assert "karşılaştırma koşusu" not in str(g2["reasons"])


def test_the_default_backtest_follows_a_changed_risk_yaml(
    tmp_path: Path, config_dir: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    path = config_dir / "risk.yaml"
    text = path.read_text(encoding="utf-8")
    assert "risk_per_trade_pct: 0.25" in text
    path.write_text(text.replace("risk_per_trade_pct: 0.25", "risk_per_trade_pct: 0.2"), "utf-8")
    reports = tmp_path / "reports"

    assert backtest(reports, config_dir, "--strategy", "mtf_trend_cont") == 0

    ((_, artifact),) = backtests(reports).items()
    assert artifact.params["risk_percent"] == 0.2
    assert "İşlem başı risk: %0,2 — planlanan risk" in capsys.readouterr().out


def test_pine_risk_is_labelled_warned_and_never_the_decision_run(
    tmp_path: Path, config_dir: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    reports = tmp_path / "reports"
    trades = tmp_path / "trades.csv"

    code = backtest(
        reports,
        config_dir,
        "--pine-risk",
        "--matrix",
        "--walk-forward",
        "--trades-out",
        str(trades),
    )

    out = capsys.readouterr().out
    assert code == 0
    assert (
        "! KARAR KOŞUSU DEĞİL (Go/No-Go bu koşuyu kullanmaz): işlem başı risk Pine varsayılanı "
        "(%0,5); karar koşusu config/risk.yaml riskiyle yapılır" in out
    )
    assert "NOT: işlem listesi betiğin kendi riskiyle (--pine-risk) üretildi" in out
    names = sorted(path.name for path in reports.iterdir())
    assert names == [
        "backtest_ny_orb_fvg-pinerisk_20261118T090000Z.json",
        "cost_matrix_ny_orb_fvg-pinerisk-cost-all_20261118T090000Z.json",
        "cost_matrix_ny_orb_fvg-pinerisk-cost-oos_20261118T090000Z.json",
        "walk_forward_ny_orb_fvg-pinerisk_20261118T090000Z.json",
    ]
    loaded = [load_artifact(reports / name) for name in names]
    assert isinstance(loaded[0], BacktestArtifact)
    assert loaded[0].params["risk_pct"] == PINE_RISK_PCT
    assert isinstance(loaded[1], CostMatrixArtifact)
    assert isinstance(loaded[3], WalkForwardArtifact)
    # Alone it is no decision run: G2 says only a comparison run exists, and why.
    g2 = part(gonogo(reports, config_dir), "G2", "ny_orb_fvg")
    assert g2["status"] == "FAIL"
    assert "yalnızca karşılaştırma koşusu var" in str(g2["reasons"])
    assert "işlem başı risk %0,5 (planlanan risk %0,25, config/risk.yaml)" in str(g2["reasons"])


def test_a_newer_pine_risk_run_does_not_hide_the_older_decision_run(
    tmp_path: Path, config_dir: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    reports = tmp_path / "reports"
    assert backtest(reports, config_dir) == 0
    assert backtest(reports, config_dir, "--pine-risk", now=clock(600)) == 0
    capsys.readouterr()

    report = gonogo(reports, config_dir)

    names = sorted(backtests(reports))
    assert names == [
        "backtest_ny_orb_fvg-pinerisk_20261118T091000Z.json",
        "backtest_ny_orb_fvg_20261118T090000Z.json",
    ]
    for gate_id in ("G1", "G2", "G3"):
        values = part(report, gate_id, "ny_orb_fvg")["values"]
        assert values["backtest_file"] == "backtest_ny_orb_fvg_20261118T090000Z.json"


def test_every_registered_strategy_made_by_the_cli_is_confirmed_by_the_cli(
    tmp_path: Path, config_dir: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    # The six registry entries through both commands: what `backtest` recorded is exactly
    # what `gonogo` expects, so no run is refused for its parameters (MTF's input is
    # named risk_percent, the Master's legs record the portfolio's set).
    reports = tmp_path / "reports"
    assert backtest(reports, config_dir, "--strategy", "all") == 0
    assert backtest(reports, config_dir, "--strategy", MASTER, now=clock(60)) == 0
    capsys.readouterr()
    singles = [sid for sid, spec in STRATEGIES.items() if len(spec.leg_ids) == 1]

    report = gonogo(reports, config_dir, ",".join([*singles, *MASTER_LEGS]))

    for sid in [*singles, *MASTER_LEGS]:
        g2 = part(report, "G2", sid)
        assert g2["values"]["backtest_file"].startswith(f"backtest_{sid}_2026"), sid
        assert "aynı değil" not in str(g2["reasons"]), sid
        assert "karşılaştırma koşusu" not in str(g2["reasons"]), sid
    for name, artifact in backtests(reports).items():
        risk_param = "risk_percent" if artifact.strategy_id == "mtf_trend_cont" else "risk_pct"
        assert artifact.params[risk_param] == PLANNED_RISK_PCT, name


def test_a_risk_override_in_strategies_yaml_stops_every_command(
    tmp_path: Path, config_dir: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    (config_dir / "strategies.yaml").write_text(
        "strategies:\n  ny_orb_fvg:\n    risk_pct: 0.25\n", "utf-8"
    )
    reports = tmp_path / "reports"

    code = backtest(reports, config_dir)

    err = capsys.readouterr().err
    assert code == 1
    assert "strategies.ny_orb_fvg.risk_pct" in err
    assert "tek kaynak config/risk.yaml" in err
    assert not reports.exists()
    assert main(["config", "check", "--config-dir", str(config_dir)], clock=clock()) == 1
    assert "strategies.yaml: HATA" in capsys.readouterr().out
    gonogo_code = main(
        [*GONOGO_ORB, "--config-dir", str(config_dir), "--reports-dir", str(reports)],
        clock=clock(),
    )
    assert gonogo_code == 2
    assert not reports.exists()


def test_a_planned_risk_below_a_scripts_minimum_stops_the_cli_in_one_turkish_line(
    tmp_path: Path, config_dir: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    path = config_dir / "risk.yaml"
    text = path.read_text(encoding="utf-8")
    # A valid risk.yaml value (0 < x <= 0.5) that the NY ORB script's input refuses
    # (its minimum is 0.1) and the ICT script's accepts (minimum 0.01).
    path.write_text(text.replace("risk_per_trade_pct: 0.25", "risk_per_trade_pct: 0.05"), "utf-8")
    reports = tmp_path / "reports"

    code = backtest(reports, config_dir)

    captured = capsys.readouterr()
    assert code == 1
    assert captured.err.startswith("HATA: ny_orb_fvg: planlanan işlem başı risk %0,05")
    assert "en az 0.1 olmalı" in captured.err
    assert len(captured.err.strip().splitlines()) == 1
    assert "Traceback" not in captured.err
    assert not reports.exists()
    # The script's own risk is still runnable, and a script that accepts 0.05 runs at it.
    assert backtest(reports, config_dir, "--pine-risk", "--no-artifact") == 0
    assert backtest(reports, config_dir, "--strategy", "ict_silver_bullet") == 0
    ((_, artifact),) = backtests(reports).items()
    assert artifact.params["risk_pct"] == 0.05
    capsys.readouterr()
    # Go/No-Go cannot say what a decision run of NY ORB would be at that risk: an error.
    code = main(
        [*GONOGO_ORB, "--config-dir", str(config_dir), "--reports-dir", str(reports)],
        clock=clock(),
    )
    assert code == 2
    assert "planlanan işlem başı risk %0,05" in capsys.readouterr().err


def test_pine_risk_at_a_planned_risk_equal_to_the_scripts_is_the_decision_run_and_says_so(
    tmp_path: Path, config_dir: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    # risk.yaml at the code's hard maximum, which is also every script's default: the two
    # runs are the same run. Go/No-Go reads recorded fields (never the label), so a file
    # saying "Go/No-Go will not use this run" would be judged as the decision run all the
    # same; the command writes what the run is instead.
    path = config_dir / "risk.yaml"
    text = path.read_text(encoding="utf-8")
    path.write_text(text.replace("risk_per_trade_pct: 0.25", "risk_per_trade_pct: 0.5"), "utf-8")
    reports = tmp_path / "reports"

    assert backtest(reports, config_dir, "--pine-risk") == 0

    out = capsys.readouterr().out
    assert (
        "NOT: --pine-risk: ny_orb_fvg betiğinin kendi riski planlanan riskle aynı (%0,5); "
        "karşılaştırılacak fark yok, koşu karar koşusu olarak yapılıyor." in out
    )
    assert "KARAR KOŞUSU DEĞİL" not in out
    ((name, artifact),) = backtests(reports).items()
    assert name == "backtest_ny_orb_fvg_20261118T090000Z.json"
    assert artifact.params["risk_pct"] == PINE_RISK_PCT
    assert not any("KARŞILAŞTIRMA KOŞUSU" in note for note in artifact.notes)
    g2 = part(gonogo(reports, config_dir), "G2", "ny_orb_fvg")
    assert g2["values"]["backtest_file"] == name
    assert "karşılaştırma koşusu" not in str(g2["reasons"])


def test_a_tv_ohlc_backtest_at_the_planned_risk_points_to_pine_risk(
    tmp_path: Path, config_dir: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    hint = "NOT: bu tv_ohlc koşusu planlanan riskle (config/risk.yaml) boyutlandırıldı"
    reports = tmp_path / "reports"

    assert backtest(reports, config_dir, "--fill-model", "tv_ohlc", "--no-artifact") == 0
    planned_out = capsys.readouterr().out
    assert backtest(reports, config_dir, "--fill-model", "tv_ohlc", "--pine-risk") == 0
    pine_out = capsys.readouterr().out
    assert backtest(reports, config_dir, "--no-artifact") == 0
    decision_out = capsys.readouterr().out

    assert hint in planned_out
    assert "--pine-risk ekleyin" in planned_out
    assert hint not in pine_out
    assert hint not in decision_out
    ((name, _),) = backtests(reports).items()
    assert name == "backtest_ny_orb_fvg-tv-pinerisk_20261118T090000Z.json"


def parity(reports: Path, config: Path, strategy: str, *extra: str) -> int:
    arguments = ["parity", "--strategy", strategy, "--data", str(BARS)]
    arguments += ["--tv-trades", str(SAMPLE_TV), "--tv-tz", "America/New_York"]
    arguments += ["--config-dir", str(config), "--reports-dir", str(reports)]
    return main([*arguments, *extra], clock=clock())


@pytest.mark.parametrize(
    ("strategy_id", "risk_param"), [("ny_orb_fvg", "risk_pct"), ("mtf_trend_cont", "risk_percent")]
)
def test_parity_always_runs_at_the_scripts_own_risk_and_says_so_in_one_line(
    tmp_path: Path,
    config_dir: Path,
    capsys: pytest.CaptureFixture[str],
    strategy_id: str,
    risk_param: str,
) -> None:
    reports = tmp_path / "reports"

    code = parity(reports, config_dir, strategy_id)

    out = capsys.readouterr().out
    assert code == 1  # the hand-written sample list is not this run's
    (line,) = (text for text in out.splitlines() if text.startswith("İşlem başı risk:"))
    assert line == (
        f"İşlem başı risk: %0,5 — betiğin kendi değeri ({risk_param}); parite TradingView ile "
        "betiğin girdilerinde karşılaştırılır, config/risk.yaml'daki planlanan risk burada "
        "kullanılmaz (2026-11-19 kullanıcı kararının uygulaması, ADR-0010)."
    )
    (written,) = reports.iterdir()
    artifact = load_artifact(written)
    assert isinstance(artifact, ParityArtifact)
    assert artifact.params is not None
    assert artifact.params[risk_param] == PINE_RISK_PCT
    assert line in artifact.notes
    # It is the parity set Go/No-Go holds, and not the decision set.
    assert artifact.params == expected_run_params((strategy_id,), {}, None)[strategy_id]
    assert artifact.params != expected_run_params((strategy_id,), {}, PLANNED_RISK_PCT)[strategy_id]
    # There is no way to ask parity for the planned risk.
    with pytest.raises(SystemExit):
        parity(reports, config_dir, strategy_id, "--pine-risk")
    capsys.readouterr()


def test_the_cli_parity_report_is_judged_by_g6_against_the_parity_set(
    tmp_path: Path, config_dir: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    reports = tmp_path / "reports"
    assert parity(reports, config_dir, "ny_orb_fvg") == 1
    capsys.readouterr()

    g6 = part(gonogo(reports, config_dir), "G6", "ny_orb_fvg")

    reasons = " | ".join(str(reason) for reason in g6["reasons"])
    # Synthetic sample input: never PASS. But not for its parameters: they are the
    # script's own, which is what G6 expects of a parity report.
    assert g6["status"] == "FAIL"
    assert "config/strategies.yaml'ın bugünkü değerleriyle aynı değil" not in reasons


def test_the_help_text_names_the_new_option_and_the_rule(
    capsys: pytest.CaptureFixture[str],
) -> None:
    with pytest.raises(SystemExit):
        main(["backtest", "--help"])
    text = " ".join(capsys.readouterr().out.split())

    assert "--pine-risk" in text
    assert "config/risk.yaml'daki işlem başı risk (risk_per_trade_pct" in text
    assert "etiketi -pinerisk" in text


# --- the panel repeats what the runner said (open finding 86) -------------------------------


@pytest.mark.parametrize(
    ("changes", "planned", "label", "decision"),
    [
        ({}, 0.25, "ny_orb_fvg", True),
        ({}, None, "ny_orb_fvg-pinerisk", False),
        ({"oos_start_ms": OOS_MS + 86_400_000}, 0.25, "ny_orb_fvg-oos20261102", False),
    ],
)
def test_the_panel_calls_a_run_a_decision_run_only_when_the_runner_did(
    changes: dict[str, Any], planned: float | None, label: str, decision: bool
) -> None:
    # Real artifacts from the real runner: the card's "Koşu türü" row used to read the fill
    # model and the costs only, so a -pinerisk and a -oos… run were shown as decision runs.
    result = run(
        request(
            "ny_orb_fvg",
            planned,
            series=short_fixture(),
            configured_oos_start_ms=OOS_MS,
            **changes,
        )
    )
    (artifact,) = backtest_artifacts(result, created=CREATED)
    assert artifact.label == label
    assert result.decision is decision
    row = run_kind(artifact)
    if decision:
        assert row == "karar koşusu"
    else:
        assert row.startswith("KARŞILAŞTIRMA KOŞUSU (karar koşusu değil): ")
        assert row in artifact.notes
