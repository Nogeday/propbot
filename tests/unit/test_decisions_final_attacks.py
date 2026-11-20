"""Final attack on the user decisions of 2026-11-19, second round.

The first round (test_risk_rule_attacks.py) hardened the planned-risk rule itself. This
file attacks what came after or beside it: the Master portfolio judged by its legs or by
its combined id (decision 4), the risk chain through the real commands with the config
edited between the steps (decision 5), the `-pinerisk` mark, the panel's "Koşu türü" row
and the commands' behaviour on files a user edited or damaged. Every test goes through
the real entry point (`main([...])`) or the library the commands call.

Routes that were open and were written here as failing tests first:

- a recorded risk input that is an integer beyond the float range ended `propbot gonogo`
  in a traceback (and so did an integer longer than Python's own digit limit anywhere in
  a report file, a session gap or bar count beyond the float range, two totals that
  overflow when added and a standard deviation so small that the t-statistic's
  denominator is zero);
- with a planned risk equal to the script's, `--pine-risk` on an instrument profile
  without costs printed "made as the decision run" above "KARAR KOŞUSU DEĞİL";
- G4 wrote its "risk zinciri" sentence (decision runs, list and simulation at the same
  risk) for a simulation with experimental risk steps (the risk changes with the
  drawdown) and for a simulation risk that differs from the planned one within a
  relative tolerance;
- `propbot montecarlo` simulated a `-pinerisk` list at the planned risk without a word
  (only the ids carried the mark);
- the parity report's risk line put the clause about another risk base after its full
  stop.

The rest pins what held. Everything runs on the SYNTHETIC fixtures; where a gate check
could not be reached otherwise the bars are presented as real (the marker line removed).
Nothing here is evidence of an edge.
"""

import contextlib
import dataclasses
import io
import json
import shutil
from collections.abc import Callable, Sequence
from pathlib import Path
from typing import Any, Final

import pytest

from propbot.backtest.artifacts import (
    COMPARISON_RUN_TAG,
    PINE_RISK_SUFFIX,
    BacktestArtifact,
    Gate,
    GoNoGoArtifact,
    LoadedArtifact,
    MonteCarloArtifact,
    all_artifacts,
    artifact_file_name,
    load_artifact,
)
from propbot.backtest.command import expected_run_params
from propbot.backtest.gonogo import (
    GoNoGoInputs,
    evaluate,
    gate_edge,
    gate_monte_carlo,
    gate_parity,
)
from propbot.backtest.runner import effective_params
from propbot.cli import main
from propbot.dashboard.analysis import DECISION_RUN, backtest_body, run_kind
from propbot.strategies.registry import strategy_spec
from support.paths import CONFIG_DIR, FIXTURES
from unit.test_gonogo import (
    OTHER_SHA,
    PARAMS,
    PARITY_PARAMS,
    data_info,
    inputs,
    rows_digest,
    stats,
    status,
    trades_provenance,
    validation,
)
from unit.test_gonogo import backtest as backtest_report
from unit.test_gonogo import monte_carlo as monte_carlo_report
from unit.test_gonogo import parity as parity_report
from unit.test_risk_rule import MASTER, MASTER_LEGS, OOS, clock, part

BARS: Final = FIXTURES / "ohlcv" / "synthetic_5m.csv"
SAMPLE_TV: Final = FIXTURES / "parity" / "sample_tv_basic_comma.csv"
CHAIN: Final = "risk zinciri:"
ROWS_DIFFER: Final = "karar koşusununkilerle aynı değil"
NOT_THE_JUDGED: Final = "işlem listesinin stratejileri değerlendirilen stratejilerle aynı değil"
NO_DECISION_RUN: Final = "karar koşusu olmayan strateji"
TWICE: Final = "aynı işlemler birden fazla karar koşusunda"
PINE_LIST: Final = "betiğin kendi riskiyle (--pine-risk)"
CANNOT_PASS: Final = "G4'ü GEÇTİ yapamaz"
DATA_GATES: Final = ("G1", "G2", "G3", "G4")
REMOVED: Final = object()
# An integer no float can hold, and the digits of one Python itself refuses to read.
BEYOND_FLOAT: Final = "1" + "0" * 400
BEYOND_PYTHON: Final = "1" + "0" * 5000


# --- helpers: the pure layer ------------------------------------------------------------------


def edited(item: LoadedArtifact, **changes: Any) -> LoadedArtifact:
    """The artifact as a hand-edited file would be read: through JSON, schema applied.

    "a__b" reaches into a sub-object; the value REMOVED deletes the key.
    """
    raw = item.artifact.model_dump(mode="json")
    for key, value in changes.items():
        *path, last = key.split("__")
        target = raw
        for name in path:
            target = target[name]
        if value is REMOVED:
            del target[last]
        else:
            target[last] = value
    artifact = type(item.artifact).model_validate_json(json.dumps(raw))
    return LoadedArtifact(artifact_file_name(artifact), artifact)


def leg(sid: str, **options: Any) -> LoadedArtifact:
    """A Master leg's decision run: its rows carry its own id."""
    return edited(backtest_report(sid, **options), trades_digests={sid: rows_digest(sid)})


def combined(**changes: Any) -> LoadedArtifact:
    """The Master's combined decision run: 600 rows that carry the two leg ids."""
    digests = {sid: rows_digest(sid) for sid in MASTER_LEGS}
    return edited(backtest_report(MASTER, n_all=600), **{"trades_digests": digests, **changes})


def master_mc(strategies: Sequence[str] = MASTER_LEGS, **options: Any) -> LoadedArtifact:
    """The simulation of the Master's trade list: provenance and row digests name the legs."""
    item = monte_carlo_report(provenance=trades_provenance(strategies=strategies), **options)
    digests = {sid: rows_digest(sid) for sid in strategies}
    return edited(item, inputs__trades_digests=digests)


def master_inputs(
    items: Sequence[LoadedArtifact], ids: Sequence[str], **changes: Any
) -> GoNoGoInputs:
    """What the command passes for the Master: every judged id maps to the one script."""
    base = inputs(
        list(items),
        strategy_ids=tuple(ids),
        expected_params={sid: dict(PARAMS) for sid in ids},
        expected_parity_params={sid: dict(PARITY_PARAMS) for sid in ids},
        risk_params=dict.fromkeys(ids, "risk_pct"),
        script_ids=dict.fromkeys(ids, MASTER),
    )
    return dataclasses.replace(base, **changes)


def legs_set(*, second_leg: LoadedArtifact | None = None) -> list[LoadedArtifact]:
    other = leg(MASTER_LEGS[1]) if second_leg is None else second_leg
    return [validation(), master_mc(), leg(MASTER_LEGS[0]), other, parity_report(MASTER)]


def combined_set(**changes: Any) -> list[LoadedArtifact]:
    return [validation(), master_mc(), combined(**changes), parity_report(MASTER)]


def statuses(case: GoNoGoInputs) -> dict[str, str]:
    return {gate.id: gate.status for gate in evaluate(case).gates}


# --- helpers: the real commands ---------------------------------------------------------------


@pytest.fixture(scope="module")
def real_bars(tmp_path_factory: pytest.TempPathFactory) -> Path:
    """The synthetic fixture without its marker line: what a user could hand the tool as
    real bars (it cannot know). Only used to reach the checks behind "SENTETİK VERİ"."""
    lines = BARS.read_text("utf-8").splitlines(keepends=True)
    assert lines[0].startswith("#propbot-synthetic")
    path = tmp_path_factory.mktemp("bars") / "mes_5m.csv"
    path.write_text("".join(lines[1:]), "utf-8")
    return path


class World:
    """A copy of config/, a reports folder and one clock for a chain of real commands."""

    def __init__(self, root: Path, bars: Path) -> None:
        self.root = root
        self.bars = bars
        self.config = root / "config"
        self.reports = root / "reports"
        self._seconds = 0
        shutil.copytree(CONFIG_DIR, self.config)
        # An OOS start inside the fixture, so that a run can be the decision run.
        self.edit("backtest.yaml", 'oos_start: "2025-01-01"', f'oos_start: "{OOS}"')

    def edit(self, name: str, old: str, new: str) -> None:
        path = self.config / name
        text = path.read_text("utf-8")
        assert old in text, (name, old)
        path.write_text(text.replace(old, new, 1), "utf-8")

    def call(self, *argv: str) -> tuple[int, str, str]:
        """One command a minute after the last one; any exception fails the test."""
        self._seconds += 60
        out, err = io.StringIO(), io.StringIO()
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            code = main(list(argv), clock=clock(self._seconds))
        assert "Traceback" not in out.getvalue() + err.getvalue()
        return code, out.getvalue(), err.getvalue()

    def backtest(self, *extra: str) -> tuple[int, str, str]:
        arguments = ["backtest", "--data", str(self.bars), "--config-dir", str(self.config)]
        arguments += ["--reports-dir", str(self.reports)]
        if "--strategy" not in extra:
            arguments += ["--strategy", "ny_orb_fvg"]
        return self.call(*arguments, *extra)

    def trade_list(self, name: str, *extra: str) -> Path:
        path = self.root / name
        code, _, err = self.backtest("--trades-out", str(path), *extra)
        assert code == 0, err
        return path

    def montecarlo(self, trades: Path, *extra: str) -> tuple[int, str, str]:
        venue = self.config / "venues" / "ftmo_2step_100k.yaml"
        arguments = ["montecarlo", "--trades", str(trades), "--venue", str(venue)]
        arguments += ["--risk-config", str(self.config / "risk.yaml"), "--r-is-net"]
        return self.call(*arguments, "--reports-dir", str(self.reports), *extra)

    def gonogo_code(self, strategies: str) -> tuple[int, str]:
        arguments = ["gonogo", "--strategies", strategies, "--config-dir", str(self.config)]
        code, _, err = self.call(*arguments, "--reports-dir", str(self.reports))
        return code, err

    def gonogo(self, strategies: str) -> GoNoGoArtifact:
        """The newest Go/No-Go report (never GO in Faz 2: the exit code is 1)."""
        code, err = self.gonogo_code(strategies)
        assert code == 1, err
        newest = max(p for p in self.reports.iterdir() if p.name.startswith("go_no_go_g"))
        artifact = load_artifact(newest)
        assert isinstance(artifact, GoNoGoArtifact)
        assert artifact.overall == "NO_GO"
        return artifact

    def report_file(self, prefix: str) -> Path:
        (path,) = (p for p in self.reports.iterdir() if p.name.startswith(prefix))
        return path

    def rewrite(self, prefix: str, change: Callable[[dict[str, Any]], None]) -> None:
        """Edit one report file by hand (as JSON)."""
        path = self.report_file(prefix)
        raw = json.loads(path.read_text("utf-8"))
        change(raw)
        path.write_text(json.dumps(raw), "utf-8")


@pytest.fixture
def world(tmp_path: Path, real_bars: Path) -> World:
    return World(tmp_path, real_bars)


def gate(report: GoNoGoArtifact, gate_id: str) -> Gate:
    (found,) = (g for g in report.gates if g.id == gate_id)
    return found


def never_passes(report: GoNoGoArtifact) -> None:
    for gate_id in DATA_GATES:
        assert gate(report, gate_id).status != "PASS", gate_id
    assert CHAIN not in gate(report, "G4").evidence


# --- (1) the Master, by its legs or by its combined id (decision 4) ---------------------------


def test_the_honest_master_set_passes_by_its_legs_and_by_its_combined_id() -> None:
    # The control for everything below: on these sets every data gate passes, so a gate
    # that stops passing after one change stopped because of that change.
    by_legs = master_inputs(legs_set(), MASTER_LEGS)
    by_id = master_inputs(combined_set(), [MASTER])

    for case in (by_legs, by_id):
        assert statuses(case) == {
            "G1": "PASS",
            "G2": "PASS",
            "G3": "PASS",
            "G4": "PASS",
            "G5": "NOT_EVALUABLE",
            "G6": "PASS",
            "G7": "PASS",
        }
        assert CHAIN in gate_monte_carlo(case).gate.evidence
    # A leg borrows the script's parity report and says so; the combined id reads its own.
    assert "bir betik bacağıdır" in gate_parity(by_legs).gate.evidence
    assert "bir betik bacağıdır" not in gate_parity(by_id).gate.evidence


def test_a_combined_id_without_a_decision_run_is_not_reported_as_a_list_mismatch() -> None:
    # Which ids the Master's rows carry is known only from its decision run. Without one G4
    # used to fall back to the combined id itself and report the list's two legs as "not
    # judged", in front of the real cause.
    case = master_inputs([validation(), master_mc(), parity_report(MASTER)], [MASTER])
    result = gate_monte_carlo(case)

    assert result.gate.status == "NOT_EVALUABLE"
    assert f"{NO_DECISION_RUN}: {MASTER}" in result.gate.evidence
    assert "değerlendirilmeyen" not in result.gate.evidence
    assert "listede olmayan" not in result.gate.evidence


@pytest.mark.parametrize(
    "digests",
    [
        None,
        REMOVED,
        {},
        {MASTER_LEGS[0]: rows_digest(MASTER_LEGS[0])},
        {**{sid: rows_digest(sid) for sid in MASTER_LEGS}, "ny_orb_fvg": rows_digest("x")},
        dict.fromkeys(MASTER_LEGS, "1" * 64),
        {MASTER_LEGS[0]: rows_digest(MASTER_LEGS[1]), MASTER_LEGS[1]: rows_digest(MASTER_LEGS[0])},
        {MASTER: rows_digest(MASTER)},
    ],
    ids=["null", "absent", "empty", "one-leg", "extra-id", "other-run", "swapped", "own-id"],
)
def test_only_the_combined_runs_own_per_leg_digests_tie_it_to_the_list(digests: object) -> None:
    result = gate_monte_carlo(master_inputs(combined_set(trades_digests=digests), [MASTER]))

    assert status(result) == "NOT_EVALUABLE"
    assert CHAIN not in result.gate.evidence
    assert ROWS_DIFFER in result.gate.evidence


def test_the_combined_id_with_one_of_its_legs_is_refused_by_the_gate_and_by_the_command(
    world: World,
) -> None:
    items = [*legs_set(), combined()]
    for ids in ([MASTER, MASTER_LEGS[0]], [MASTER_LEGS[1], MASTER], [MASTER, *MASTER_LEGS]):
        result = gate_monte_carlo(master_inputs(items, ids))
        assert status(result) == "NOT_EVALUABLE", ids
        assert TWICE in result.gate.evidence
        assert CHAIN not in result.gate.evidence

    for ids in (f"{MASTER},{MASTER_LEGS[0]}", f"{MASTER_LEGS[1]}, {MASTER}"):
        code, err = world.gonogo_code(ids)
        assert code == 2
        assert err.startswith("HATA: --strategies: master_portfolio ile bacağı ")
    assert not world.reports.exists()  # refused before anything was read or written


def test_a_list_of_one_leg_is_not_the_portfolios_list_and_the_whole_list_is_not_one_legs() -> None:
    one_leg = MASTER_LEGS[:1]
    half = master_mc(one_leg, n_trades=300, sum_r=90.0, sum_mae_r=-300.0)
    backtests = [leg(MASTER_LEGS[0]), leg(MASTER_LEGS[1]), parity_report(MASTER)]

    by_legs = gate_monte_carlo(master_inputs([validation(), half, *backtests], MASTER_LEGS))
    by_id = gate_monte_carlo(master_inputs([validation(), half, combined()], [MASTER]))
    whole_for_one = gate_monte_carlo(master_inputs(legs_set(), one_leg))

    for result in (by_legs, by_id):
        assert status(result) == "NOT_EVALUABLE"
        assert f"listede olmayan: {MASTER_LEGS[1]}" in result.gate.evidence
        assert CHAIN not in result.gate.evidence
    assert status(whole_for_one) == "NOT_EVALUABLE"
    assert f"değerlendirilmeyen: {MASTER_LEGS[1]}" in whole_for_one.gate.evidence
    assert CHAIN not in whole_for_one.gate.evidence


@pytest.mark.parametrize(
    ("change", "reason"),
    [
        ({"data": data_info(sha=OTHER_SHA)}, "karar koşularının verisiyle aynı değil"),
        ({"params": dict(PARITY_PARAMS)}, NO_DECISION_RUN),
        ({"params": {**PARAMS, "use_filter": False}}, NO_DECISION_RUN),
        ({"oos_start": "2025-06-01T00:00:00Z"}, NO_DECISION_RUN),
        ({"cost_factor": 1.5}, NO_DECISION_RUN),
        ({"fill_model": "tv_ohlc"}, NO_DECISION_RUN),
        ({"min_gap_seconds": 3600}, NO_DECISION_RUN),
    ],
    ids=["data", "pine-risk", "params", "oos-start", "cost-factor", "fill-model", "session-gap"],
)
def test_a_leg_from_another_run_breaks_the_chain(change: dict[str, Any], reason: str) -> None:
    # One leg of the judged pair comes from a run that differs in one thing; the list is
    # the honest run's. Whatever differs, G4 does not pass and does not state the chain.
    other = leg(MASTER_LEGS[1], **change)

    result = gate_monte_carlo(master_inputs(legs_set(second_leg=other), MASTER_LEGS))

    assert status(result) == "NOT_EVALUABLE"
    assert reason in result.gate.evidence
    assert CHAIN not in result.gate.evidence


def test_a_leg_sized_on_another_capital_blocks_like_any_other_run() -> None:
    other = edited(leg(MASTER_LEGS[1]), initial_capital=50_000.0)

    case = master_inputs(legs_set(second_leg=other), MASTER_LEGS)

    assert status(gate_monte_carlo(case)) == "NOT_EVALUABLE"
    assert status(gate_edge(case)) == "NOT_EVALUABLE"
    assert "initial_balance" in gate_edge(case).gate.evidence


def test_a_legs_parity_is_read_from_its_script_and_from_nothing_else() -> None:
    backtests = [validation(), master_mc(), leg(MASTER_LEGS[0]), leg(MASTER_LEGS[1])]
    single_scripts = [parity_report("ny_orb_fvg"), parity_report("asian_sweep")]

    # The script's report: every leg passes on it and the evidence says whose it is.
    honest = gate_parity(master_inputs([*backtests, parity_report(MASTER)], MASTER_LEGS))
    assert status(honest) == "PASS"
    assert "parite betiğin ('master_portfolio') raporundan okunur" in honest.gate.evidence
    # Reports of the stand-alone scripts the legs repeat are not the Master's report.
    stand_alone = gate_parity(master_inputs([*backtests, *single_scripts], MASTER_LEGS))
    assert status(stand_alone) == "NOT_EVALUABLE"
    assert "parite raporu yok ('master_ny_orb_fvg' bir betik bacağıdır" in (
        stand_alone.gate.evidence
    )
    # Without the mapping a leg has no script: nothing is borrowed.
    unmapped = master_inputs([*backtests, parity_report(MASTER)], MASTER_LEGS, script_ids=None)
    assert status(gate_parity(unmapped)) == "NOT_EVALUABLE"
    assert "parite raporu yok" in gate_parity(unmapped).gate.evidence


def test_a_leg_pointed_at_a_single_strategy_script_fails_on_that_scripts_parameters() -> None:
    # The pure layer believes the mapping it is given. With the real parameter sets a
    # wrong one still cannot pass: the Master's inputs are not the stand-alone script's.
    sid = MASTER_LEGS[0]
    wanted = expected_run_params([sid], {}, None)
    own = dict(effective_params(strategy_spec("ny_orb_fvg")))
    assert dict(wanted[sid]) != own
    report = parity_report("ny_orb_fvg", params=own)

    case = inputs(
        [report],
        strategy_ids=(sid,),
        expected_parity_params=wanted,
        script_ids={sid: "ny_orb_fvg"},
    )
    result = gate_parity(case)

    assert status(result) == "NOT_EVALUABLE"
    assert "parite raporunun parametreleri" in result.gate.evidence
    # The same report under its own id, judged against its own script's set, passes.
    own_case = inputs(
        [report],
        strategy_ids=("ny_orb_fvg",),
        expected_parity_params={"ny_orb_fvg": own},
    )
    assert status(gate_parity(own_case)) == "PASS"


def test_the_real_master_chain_holds_by_either_view_and_for_no_single_leg(world: World) -> None:
    trades = world.trade_list("master.csv", "--strategy", MASTER)
    assert world.montecarlo(trades)[0] == 0

    by_id = gate(world.gonogo(MASTER), "G4")
    by_legs = gate(world.gonogo(",".join(MASTER_LEGS)), "G4")

    # 39 trades and a losing fixture: G4 fails either way, but on its numbers, not on
    # the chain, which the real artifacts close under both views.
    for found, files in ((by_id, [MASTER]), (by_legs, MASTER_LEGS)):
        assert found.status == "FAIL"
        assert CHAIN in found.evidence
        for text in (NOT_THE_JUDGED, ROWS_DIFFER, NO_DECISION_RUN, TWICE):
            assert text not in found.evidence, text
        for sid in files:
            assert f"backtest_{sid}_" in found.evidence
    # The list the command writes always holds both legs: one leg alone is not its owner.
    # G4 said so ("değerlendirilmeyen: <other leg>", still pinned at the pure layer above);
    # since the user's decision of 2026-11-20 (open finding 87) the command refuses one leg
    # before any gate runs and writes no report.
    before = sorted(path.name for path in world.reports.iterdir())
    code, err = world.gonogo_code(MASTER_LEGS[0])
    assert code == 2
    assert f"--strategies: {MASTER_LEGS[0]}, {MASTER} betiğinin bacağıdır" in err
    assert f"eksik bacak: {MASTER_LEGS[1]};" in err
    assert sorted(path.name for path in world.reports.iterdir()) == before


# --- (2) the risk chain through the real commands, config edited between the steps ------------


def edit_risk(world: World) -> None:
    world.edit("risk.yaml", "risk_per_trade_pct: 0.25", "risk_per_trade_pct: 0.3")


def edit_strategy(world: World) -> None:
    (world.config / "strategies.yaml").write_text("strategies:\n  ny_orb_fvg: {rr: 3.0}\n", "utf-8")


def edit_venue(world: World) -> None:
    world.edit("venues/ftmo_2step_100k.yaml", "initial_balance: 100000", "initial_balance: 200000")


def edit_oos(world: World) -> None:
    world.edit("backtest.yaml", f'oos_start: "{OOS}"', 'oos_start: "2026-10-26"')


def edit_session(world: World) -> None:
    world.edit("backtest.yaml", "min_gap_minutes: 30", "min_gap_minutes: 45")


@pytest.mark.parametrize(
    ("change", "reason"),
    [
        (edit_risk, "işlem başı risk %0,25 (planlanan risk %0,3, config/risk.yaml)"),
        (edit_strategy, "kayıtlı parametreler config/strategies.yaml'ın bugünkü değerleriyle"),
        (edit_venue, "venue profilinin initial_balance değeri 200.000,00"),
        (edit_oos, "OOS başlangıcı 2026-11-01T00:00:00Z (config/backtest.yaml: 2026-10-26"),
        (edit_session, "seans sonu boşluğu 30,00 dk, config/backtest.yaml"),
    ],
    ids=["risk.yaml", "strategies.yaml", "venue", "oos_start", "session"],
)
def test_a_config_edit_after_the_run_leaves_no_decision_run_and_no_chain(
    world: World, change: Callable[[World], None], reason: str
) -> None:
    # The run and its list are made under yesterday's config; the config is edited; the
    # simulation and the report run under today's. The run was not made for today's
    # account, whatever the file names say.
    trades = world.trade_list("orb.csv")
    assert gate(world.gonogo("ny_orb_fvg"), "G2").evidence.startswith("ny_orb_fvg: KALDI — OOS")
    change(world)
    assert world.montecarlo(trades)[0] == 0

    report = world.gonogo("ny_orb_fvg")

    never_passes(report)
    assert reason in gate(report, "G2").evidence
    assert NO_DECISION_RUN in gate(report, "G4").evidence
    # Not judged as a decision run: no gate reads its statistics.
    assert "oos_mean_r" not in part(report, "G2", "ny_orb_fvg")["values"]
    assert "g3_ratio" not in part(report, "G3", "ny_orb_fvg")["values"]


def test_a_list_made_while_risk_yaml_said_another_risk_is_not_the_decision_runs_list(
    world: World,
) -> None:
    # MTF Trend: other sizes at 0.5 %, other exits, other rows. The list is written while
    # risk.yaml says 0.5 (so it is unmarked: it was the planned risk then), the file is
    # set back, and the simulation runs at today's 0.25 on the 0.5 list.
    assert world.backtest("--strategy", "mtf_trend_cont")[0] == 0
    world.edit("risk.yaml", "risk_per_trade_pct: 0.25", "risk_per_trade_pct: 0.5")
    trades = world.trade_list("mtf.csv", "--strategy", "mtf_trend_cont", "--no-artifact")
    assert PINE_RISK_SUFFIX not in trades.read_text("utf-8")
    world.edit("risk.yaml", "risk_per_trade_pct: 0.5", "risk_per_trade_pct: 0.25")
    assert world.montecarlo(trades)[0] == 0

    found = gate(world.gonogo("mtf_trend_cont"), "G4")

    assert f"{ROWS_DIFFER}: mtf_trend_cont" in found.evidence
    assert CHAIN not in found.evidence


def as_before_the_decisions(world: World, prefix: str) -> None:
    """Make a real `--pine-risk` artifact look like a run made before 2026-11-19.

    Then the script's 0.5 % was the only risk: the label was the bare id, no note called
    the run a comparison run and the per-id row digests did not exist.
    """
    path = world.report_file(prefix)
    raw = json.loads(path.read_text("utf-8"))
    raw["label"] = raw["label"].removesuffix(PINE_RISK_SUFFIX)
    raw["notes"] = [
        note for note in raw["notes"] if not note.startswith((COMPARISON_RUN_TAG, "İşlem başı"))
    ]
    del raw["trades_digests"]
    path.unlink()
    artifact = load_artifact_text(json.dumps(raw), path.parent)
    assert artifact.label == "ny_orb_fvg"


def load_artifact_text(text: str, directory: Path) -> BacktestArtifact:
    """Write the edited JSON under the name its content asks for and read it back."""
    artifact = BacktestArtifact.model_validate_json(text)
    path = directory / artifact_file_name(artifact)
    path.write_text(text, "utf-8")
    loaded = load_artifact(path)
    assert isinstance(loaded, BacktestArtifact)
    return loaded


def test_a_run_made_before_the_decisions_is_a_comparison_run_today(world: World) -> None:
    trades = world.trade_list("old.csv", "--pine-risk")
    # Before the decisions the list carried no mark either.
    trades.write_text(trades.read_text("utf-8").replace(PINE_RISK_SUFFIX, ""), "utf-8")
    as_before_the_decisions(world, "backtest_ny_orb_fvg-pinerisk_")
    old = load_artifact(world.report_file("backtest_ny_orb_fvg_"))
    assert isinstance(old, BacktestArtifact)
    assert world.montecarlo(trades)[0] == 0

    report = world.gonogo("ny_orb_fvg")

    never_passes(report)
    assert "işlem başı risk %0,5 (planlanan risk %0,25, config/risk.yaml)" in (
        gate(report, "G2").evidence
    )
    assert NO_DECISION_RUN in gate(report, "G4").evidence
    # The panel's row is the run's own statement at the time (it reads no config): the
    # old file never said "comparison". Today's judgement is the report's, above.
    assert run_kind(old) == DECISION_RUN


@pytest.mark.parametrize(
    ("change", "reason"),
    [
        (lambda raw: raw["params"].pop("risk_pct"), "işlem başı risk (risk_pct) yok ya da sayı"),
        (lambda raw: raw["params"].update(risk_pct=None), "işlem başı risk (risk_pct) yok ya da"),
        (lambda raw: raw["params"].update(risk_pct="0.25"), "işlem başı risk (risk_pct) yok ya"),
        (lambda raw: raw.pop("cost_factor"), "maliyet çarpanı kaydedilmemiş"),
        (lambda raw: raw.pop("min_gap_seconds"), "seans sonu boşluğu kaydedilmemiş"),
    ],
    ids=["no-risk-input", "null-risk", "text-risk", "no-factor", "no-session-gap"],
)
def test_a_real_run_with_a_field_removed_blocks_and_is_never_replaced_by_an_older_run(
    world: World, change: Callable[[dict[str, Any]], Any], reason: str
) -> None:
    # Two real decision runs; the newer one loses a field (an older file format, or a
    # hand edit). Unknown is never "at the planned risk": the gates stop there.
    assert world.backtest()[0] == 0
    trades = world.trade_list("orb.csv")
    newer = max(p.name for p in world.reports.iterdir())
    world.rewrite(newer, change)
    assert world.montecarlo(trades)[0] == 0

    report = world.gonogo("ny_orb_fvg")

    never_passes(report)
    for gate_id in ("G1", "G2", "G3"):
        found = gate(report, gate_id)
        assert found.status == "NOT_EVALUABLE"
        assert f"en yeni olası karar koşusu {newer}" in found.evidence
        assert "daha eski bir koşuya geçilmez" in found.evidence
    assert reason in gate(report, "G2").evidence
    assert NO_DECISION_RUN in gate(report, "G4").evidence


def test_the_real_combined_run_without_its_per_leg_digests_cannot_be_tied(world: World) -> None:
    trades = world.trade_list("master.csv", "--strategy", MASTER)
    assert world.montecarlo(trades)[0] == 0
    assert CHAIN in gate(world.gonogo(MASTER), "G4").evidence
    world.rewrite(f"backtest_{MASTER}_", lambda raw: raw.pop("trades_digests"))

    found = gate(world.gonogo(MASTER), "G4")

    assert CHAIN not in found.evidence
    assert ROWS_DIFFER in found.evidence
    # The legs' own files are untouched: judged by them the chain still closes.
    assert CHAIN in gate(world.gonogo(",".join(MASTER_LEGS)), "G4").evidence


def test_a_simulation_with_experimental_risk_steps_is_not_at_the_same_risk() -> None:
    # With --dd-risk-steps the risk shrinks in a drawdown: the simulation did not run at
    # the planned risk throughout, so the sentence that says it did is not written.
    stepped = edited(master_mc(), inputs__experimental_risk_steps=True)

    result = gate_monte_carlo(master_inputs([validation(), stepped, combined()], [MASTER]))

    assert status(result) == "NOT_EVALUABLE"
    assert "deneysel risk basamaklarıyla" in result.gate.evidence
    assert CHAIN not in result.gate.evidence


@pytest.mark.parametrize("simulated", [0.2500000001, 0.24999999999, 0.25000000000000006])
def test_a_simulation_at_almost_the_planned_risk_is_at_another_risk(simulated: float) -> None:
    # Decision runs are compared exactly (the runner writes the planned value itself);
    # the simulation records the value it was given, so it is compared exactly too.
    near = edited(master_mc(), inputs__risk_pct=simulated)

    result = gate_monte_carlo(master_inputs([validation(), near, combined()], [MASTER]))

    assert status(result) == "NOT_EVALUABLE"
    assert "planlanan risk %0,25: seçilen riskle koşulmamış" in result.gate.evidence
    assert CHAIN not in result.gate.evidence


def test_the_real_simulation_at_almost_the_planned_risk_is_named_exactly(world: World) -> None:
    trades = world.trade_list("orb.csv")
    assert world.montecarlo(trades, "--risk-pct", "0.2500000001")[0] == 0

    found = gate(world.gonogo("ny_orb_fvg"), "G4")

    assert "simülasyon riski %0,2500000001, planlanan risk %0,25" in found.evidence
    assert CHAIN not in found.evidence


def test_pine_risk_on_a_profile_without_costs_is_never_called_the_decision_run(
    world: World,
) -> None:
    # Planned risk = the script's risk, so --pine-risk changes nothing about the risk;
    # but the instrument profile has no costs, which makes every run a comparison run.
    world.edit("risk.yaml", "risk_per_trade_pct: 0.25", "risk_per_trade_pct: 0.5")
    world.edit("backtest.yaml", "commission_per_unit_side: 0.50", "commission_per_unit_side: 0")
    world.edit("backtest.yaml", "slippage_ticks: 1 ", "slippage_ticks: 0 ")

    code, out, _ = world.backtest("--pine-risk")

    assert code == 0
    assert "! KARAR KOŞUSU DEĞİL (Go/No-Go bu koşuyu kullanmaz): maliyetsiz koşu" in out
    assert "karar koşusu olarak yapılıyor" not in out
    assert "NOT: --pine-risk: ny_orb_fvg betiğinin kendi riski planlanan riskle aynı" in out
    assert f"'{PINE_RISK_SUFFIX}' etiketi almıyor" in out
    artifact = load_artifact(world.report_file("backtest_ny_orb_fvg-free_"))
    assert isinstance(artifact, BacktestArtifact)
    assert run_kind(artifact).startswith(COMPARISON_RUN_TAG)


def test_the_parity_risk_line_stays_one_sentence_on_another_risk_base(world: World) -> None:
    # The clause about the base belongs to the sentence: it used to follow its full stop.
    body = "strategies:\n  ny_orb_fvg: {risk_base: Fixed Balance, fixed_bal: 50000}\n"
    (world.config / "strategies.yaml").write_text(body, "utf-8")
    arguments = ["parity", "--data", str(world.bars), "--strategy", "ny_orb_fvg"]
    arguments += ["--tv-trades", str(SAMPLE_TV), "--tv-tz", "UTC", "--no-artifact"]

    code, out, _ = world.call(*arguments, "--config-dir", str(world.config))

    assert code == 1  # the sample list is not this run's: the comparison itself fails
    (line,) = (row for row in out.splitlines() if row.startswith("İşlem başı risk:"))
    assert ".;" not in line
    assert (
        "(2026-11-19 kullanıcı kararının uygulaması, ADR-0010); risk bazı 'Fixed Balance'" in line
    )
    assert line.endswith("yüzdesi DEĞİLDİR.")


# --- (3) the -pinerisk mark -------------------------------------------------------------------


def test_montecarlo_says_that_a_marked_list_is_not_the_planned_accounts(world: World) -> None:
    marked = world.trade_list("pine.csv", "--pine-risk", "--no-artifact")
    plain = world.trade_list("plain.csv", "--no-artifact")

    code, out, _ = world.montecarlo(marked)
    plain_code, plain_out, _ = world.montecarlo(plain)

    assert (code, plain_code) == (0, 0)
    (origin,) = (line for line in out.splitlines() if line.startswith("Köken"))
    assert "stratejiler ny_orb_fvg-pinerisk" in origin
    assert PINE_LIST in origin
    assert CANNOT_PASS in origin
    assert PINE_LIST not in plain_out
    assert CANNOT_PASS not in plain_out
    by_list = {}
    for path in world.reports.iterdir():
        artifact = load_artifact(path)
        assert isinstance(artifact, MonteCarloArtifact)
        by_list[artifact.inputs.trades_file] = " ".join(artifact.notes)
    assert PINE_LIST in by_list["pine.csv"]
    assert CANNOT_PASS in by_list["pine.csv"]
    assert PINE_LIST not in by_list["plain.csv"]


def test_every_file_of_a_pine_risk_run_carries_the_mark_in_its_name(world: World) -> None:
    options = ["--strategy", MASTER, "--pine-risk", "--matrix", "--walk-forward"]
    trades = world.trade_list("pine.csv", *options)

    names = sorted(path.name for path in world.reports.iterdir())

    # Three backtests, two cost matrices and three walk-forward reports: none under a
    # label a decision run could have written.
    assert len(names) == 8
    for name in names:
        assert PINE_RISK_SUFFIX in name, name
    assert f"cost_matrix_{MASTER}{PINE_RISK_SUFFIX}-cost-oos_" in " ".join(names)
    for path in world.reports.iterdir():
        notes = " ".join(load_artifact(path).notes)
        assert "planlanan risk kullanılmadı, karar koşusu değil" in notes, path.name
    rows = trades.read_text("utf-8").splitlines()[2:]
    assert rows
    assert all(PINE_RISK_SUFFIX in row for row in rows)


def test_a_half_removed_mark_is_refused_by_the_reader_in_one_turkish_message(
    world: World,
) -> None:
    marked = world.trade_list("pine.csv", "--pine-risk", "--no-artifact")
    line, *rows = marked.read_text("utf-8").splitlines()
    unmarked_rows = [row.replace(PINE_RISK_SUFFIX, "") for row in rows]
    half = {
        "rows.csv": [line, *unmarked_rows],
        "line.csv": [line.replace(PINE_RISK_SUFFIX, ""), *rows],
    }

    for name, lines in half.items():
        path = world.root / name
        path.write_text("\n".join(lines) + "\n", "utf-8")
        code, _, err = world.montecarlo(path)
        assert code == 1, name
        assert err.startswith(f"HATA: {path}: işlem listesi geçersiz"), name
        assert "köken satırındaki stratejiler arasında değil" in err
    assert not world.reports.exists()


def test_a_wholly_removed_mark_does_not_tie_a_list_whose_rows_differ(world: World) -> None:
    # MTF Trend at the script's risk makes other rows than at the planned risk. With the
    # mark edited out of the line and the rows the list still is not the decision run's.
    assert world.backtest("--strategy", "mtf_trend_cont")[0] == 0
    marked = world.trade_list("pine.csv", "--strategy", "mtf_trend_cont", "--pine-risk")
    marked.write_text(marked.read_text("utf-8").replace(PINE_RISK_SUFFIX, ""), "utf-8")
    assert world.montecarlo(marked)[0] == 0

    found = gate(world.gonogo("mtf_trend_cont"), "G4")

    assert PINE_LIST not in found.evidence  # nothing left to read the mark from
    assert f"{ROWS_DIFFER}: mtf_trend_cont" in found.evidence
    assert CHAIN not in found.evidence


def test_a_mark_edited_out_of_the_simulations_line_but_not_its_digests_is_noticed() -> None:
    # The simulation's artifact edited by hand: the provenance names the bare ids, the
    # row digests (which were not updated) still sit under the marked ones.
    marked = {f"{sid}{PINE_RISK_SUFFIX}": rows_digest(sid) for sid in MASTER_LEGS}
    half = edited(master_mc(), inputs__trades_digests=marked)

    result = gate_monte_carlo(master_inputs([validation(), half, combined()], [MASTER]))

    assert status(result) == "NOT_EVALUABLE"
    assert ROWS_DIFFER in result.gate.evidence
    assert CHAIN not in result.gate.evidence


# --- (4) the panel's "Koşu türü" row ----------------------------------------------------------


def test_the_panel_row_follows_the_runner_for_every_view_of_every_kind_of_run(
    world: World,
) -> None:
    options: list[list[str]] = [
        [],
        ["--pine-risk"],
        ["--cost-free"],
        ["--cost-factor", "1.5"],
        ["--cost-factor", "1.0000000001"],
        ["--fill-model", "tv_ohlc"],
        ["--oos-start", "2026-10-25"],
        ["--pine-risk", "--oos-start", "2026-10-25", "--fill-model", "tv_ohlc"],
    ]
    for extra in options:
        assert world.backtest("--strategy", MASTER, *extra)[0] == 0

    shown: dict[str, str] = {}
    for path in world.reports.iterdir():
        artifact = load_artifact(path)
        assert isinstance(artifact, BacktestArtifact)
        shown[artifact.label] = run_kind(artifact)

    assert len(shown) == 3 * len(options)
    decision = {label for label, text in shown.items() if text == DECISION_RUN}
    assert decision == {MASTER, *MASTER_LEGS}
    for label, text in shown.items():
        if label not in decision:
            assert text.startswith(f"{COMPARISON_RUN_TAG}: "), label
    assert "işlem başı risk Pine varsayılanı (%0,5)" in shown[f"{MASTER}-pinerisk"]
    assert "OOS başlangıcı 2026-10-25T00:00:00Z" in shown[f"{MASTER_LEGS[0]}-oos20261025"]


def test_the_repeated_note_is_escaped_in_the_row(world: World) -> None:
    assert world.backtest("--pine-risk")[0] == 0
    hostile = ' <script>alert(1)</script> "a" & <b>'
    world.rewrite(
        "backtest_",
        lambda raw: raw.update(
            notes=[n + hostile if n.startswith(COMPARISON_RUN_TAG) else n for n in raw["notes"]]
        ),
    )
    artifact = load_artifact(world.report_file("backtest_"))
    assert isinstance(artifact, BacktestArtifact)
    assert run_kind(artifact).endswith(hostile)

    html = backtest_body(artifact)

    assert "<script>" not in html
    assert "&lt;script&gt;alert(1)&lt;/script&gt; &quot;a&quot; &amp; &lt;b&gt;" in html


# --- (5) no traceback, whatever a file says ---------------------------------------------------


def set_number(world: World, prefix: str, field: str, digits: str) -> None:
    """Write a number too long for `json.dumps` into a report file, as text."""
    path = world.report_file(prefix)
    sentinel = 987_654_321_987_654_321

    def place(raw: dict[str, Any]) -> None:
        *parents, last = field.split(".")
        target = raw
        for name in parents:
            target = target[name]
        assert last in target, field
        target[last] = sentinel

    world.rewrite(prefix, place)
    text = path.read_text("utf-8")
    assert text.count(str(sentinel)) == 1
    path.write_text(text.replace(str(sentinel), digits), "utf-8")


def test_a_recorded_risk_beyond_the_float_range_is_unknown_not_a_crash() -> None:
    # The pure layer, whoever built the artifact: such a value is no risk a run could
    # have been sized at, so the run is unconfirmable (never "another risk, skip it").
    huge = edited(leg(MASTER_LEGS[0]), params={**PARAMS, "risk_pct": int(BEYOND_FLOAT)})

    case = master_inputs([*legs_set()[:2], huge, leg(MASTER_LEGS[1])], MASTER_LEGS)

    assert status(gate_edge(case)) == "NOT_EVALUABLE"
    assert "işlem başı risk (risk_pct) yok ya da sayı değil" in gate_edge(case).gate.evidence
    assert status(gate_monte_carlo(case)) == "NOT_EVALUABLE"


@pytest.mark.parametrize(
    ("field", "digits"),
    [
        ("params.risk_pct", BEYOND_FLOAT),
        ("params.risk_pct", "-" + BEYOND_FLOAT),
        ("params.risk_pct", BEYOND_PYTHON),
        ("params.or_minutes", BEYOND_PYTHON),
        ("data.bars", BEYOND_FLOAT),
        ("data.tf_seconds", BEYOND_FLOAT),
        ("min_gap_seconds", BEYOND_FLOAT),
        ("stats_all.n", BEYOND_PYTHON),
    ],
    ids=[
        "risk-400-digits",
        "risk-negative-400-digits",
        "risk-5000-digits",
        "param-5000-digits",
        "bars-400-digits",
        "bar-length-400-digits",
        "session-gap-400-digits",
        "count-5000-digits",
    ],
)
def test_a_report_file_with_an_absurd_number_ends_gonogo_in_a_report_not_a_traceback(
    world: World, field: str, digits: str
) -> None:
    trades = world.trade_list("orb.csv")
    assert world.montecarlo(trades)[0] == 0
    set_number(world, "backtest_", field, digits)
    name = world.report_file("backtest_").name

    report = world.gonogo("ny_orb_fvg")  # `call` fails the test on any exception

    never_passes(report)
    for gate_id in ("G1", "G2", "G3"):
        assert gate(report, gate_id).status == "NOT_EVALUABLE", gate_id
        assert name in gate(report, gate_id).evidence
    # The other commands that read the folder do not stop on the file either.
    scan = all_artifacts(world.reports)
    files = [path for path in world.reports.iterdir() if path.suffix == ".json"]
    assert len(scan.artifacts) + len(scan.problems) == len(files)


def test_a_standard_deviation_too_small_to_divide_by_fails_g2_instead_of_crashing() -> None:
    # 5e-324 / sqrt(300) underflows to zero: the recomputed t-statistic does not exist.
    tiny = stats(300, 0.25, 5e-324, t=2.5)

    result = gate_edge(inputs([backtest_report(oos=tiny, g3_ratio=0.625)]))

    assert status(result) == "FAIL"
    assert "OOS t-istatistiği ortalama, sapma ve işlem sayısıyla tutarsız" in result.gate.evidence


@pytest.mark.parametrize(
    ("change", "reason"),
    [
        ({"stats_all__total_r": 1e308}, "işlem listesinin R toplamı"),
        ({"mae_total_r": -1e308}, "işlem listesinin mae_r toplamı"),
    ],
    ids=["R", "mae_r"],
)
def test_totals_that_overflow_when_added_are_not_the_lists_sum_and_not_a_crash(
    change: dict[str, float], reason: str
) -> None:
    # Both legs' files edited to a total near the float limit: their sum is no float.
    edited_legs = [edited(leg(sid), **change) for sid in MASTER_LEGS]

    result = gate_monte_carlo(master_inputs([*legs_set()[:2], *edited_legs], MASTER_LEGS))

    assert status(result) == "NOT_EVALUABLE"
    assert reason in result.gate.evidence
    assert "sayı sınırının dışında" in result.gate.evidence
    assert CHAIN not in result.gate.evidence
