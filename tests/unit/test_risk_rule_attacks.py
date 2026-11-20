"""Attacks on the planned-risk rule (user decision of 2026-11-19, open finding 82).

The rule: for live operation and every portfolio-level judgement the per-trade risk of
config/risk.yaml holds; the 0.5 % of the Pine scripts is the single-strategy assumption,
kept only for parity and the labelled `--pine-risk` comparison run. Every test here is a
route by which a run at ANOTHER risk could have been taken for the decision run, or by
which the notes about the rule could have said something untrue. Routes that were open
were written here as failing tests first:

- the risk BASE (`risk_base`, `fixed_bal`) is a second source for the risk: 0.25 % of a
  fixed 200,000 is 0.5 % of the 100,000 account, and Go/No-Go read the 0.25;
- a planned risk above the code ceiling (SPEC §8.1) was accepted by the runner and by
  the pure Go/No-Go layer (the config loader was the only guard);
- a `--pine-risk` trade list whose rows happen to equal the decision run's was tied to
  that run by G4, although the command said G4 could never pass on it;
- two different risks could read the same in G4's refusal (rounded to two decimals);
- at a planned risk equal to the script's, notes pointed to `--pine-risk` although
  nothing would change.

Everything runs on the SYNTHETIC fixtures (presented as real only where a gate check
could not be reached otherwise): nothing below is evidence of an edge.
"""

import dataclasses
import datetime as dt
import inspect
import itertools
import re
import shutil
from collections.abc import Sequence
from pathlib import Path
from typing import Any

import pytest

from propbot.backtest.artifacts import (
    AnyArtifact,
    BacktestArtifact,
    ParityArtifact,
    load_artifact,
    monte_carlo_artifact,
    write_artifact,
)
from propbot.backtest.gonogo import GoNoGoError, evaluate, gate_monte_carlo, gate_parity
from propbot.backtest.model import FillModel
from propbot.backtest.montecarlo import ChallengeRules, MonteCarloParams, simulate, summarize
from propbot.backtest.parity import (
    EXPECTED_DEVIATION_HEADING,
    compare_trades,
    parity_artifact,
    warmup_cutoff_ms,
)
from propbot.backtest.runner import (
    RunnerError,
    RunResult,
    backtest_artifacts,
    effective_params,
    label_suffix,
    merge_runs,
    render_summary,
    risk_line,
    run,
    write_trade_list,
)
from propbot.backtest.trades import IntratradeModel, build_day_blocks, read_trades_csv
from propbot.cli import main
from propbot.risk.config import MAX_RISK_PER_TRADE_PCT, StrictModel
from propbot.strategies.config import load_strategy_overrides
from propbot.strategies.registry import (
    ACCOUNT_RISK_BASE,
    STRATEGIES,
    StrategyError,
    StrategySpec,
    strategy_spec,
)
from support.paths import CONFIG_DIR, REPO_ROOT
from unit.test_gonogo import (
    ORB_PARITY,
    PINE_RISK_PCT,
    PLANNED_RISK_PCT,
    inputs,
    passing_set,
    replaced,
    status,
    trades_provenance,
)
from unit.test_gonogo import monte_carlo as monte_carlo_report
from unit.test_gonogo import parity as parity_report
from unit.test_parity import tv_of
from unit.test_risk_rule import (
    CREATED,
    MASTER,
    MASTER_LEGS,
    OOS,
    backtests,
    clock,
    fixture,
    part,
    request,
    short_fixture,
)
from unit.test_risk_rule import backtest as cli_backtest
from unit.test_risk_rule import gonogo as cli_gonogo
from unit.test_risk_rule import parity as cli_parity
from unit.test_runner_attacks import VENUE, gonogo_inputs, loaded, unreal
from unit.test_runner_attacks import request as real_request

LABEL = re.compile(r"[a-z0-9_-]{1,64}")
RISK_LINE = "risk_per_trade_pct: 0.25"
OTHER_BASES: list[dict[str, object]] = [
    {"risk_base": "Account Equity"},
    {"risk_base": "Fixed Balance", "fixed_bal": 200_000.0},
]
PINE_LIST = "betiğin kendi riskiyle (--pine-risk)"
ROWS_DIFFER = "karar koşusununkilerle aynı değil"
NO_DECISION_RUN = "karar koşusu olmayan strateji"
CHAIN = "risk zinciri:"


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


def set_risk(config: Path, spelling: str) -> None:
    """Rewrite risk.yaml's risk_per_trade_pct with the given YAML spelling."""
    path = config / "risk.yaml"
    text = path.read_text(encoding="utf-8")
    assert RISK_LINE in text
    path.write_text(text.replace(RISK_LINE, f"risk_per_trade_pct: {spelling}"), "utf-8")


def set_strategies(config: Path, body: str) -> None:
    (config / "strategies.yaml").write_text("strategies:\n" + body, "utf-8")


def gonogo_code(reports: Path, config: Path, strategies: str) -> int:
    """The exit code of `propbot gonogo` (for the cases where it writes no report)."""
    arguments = ["gonogo", "--strategies", strategies, "--config-dir", str(config)]
    return main([*arguments, "--reports-dir", str(reports)], clock=clock())


def quantities(result: RunResult) -> list[float]:
    return [trade.qty for trade in result.engine.trades]


def not_decision_line(text: str) -> str:
    """The one "this is not the decision run" warning of a summary."""
    (line,) = (row for row in text.splitlines() if row.startswith("! KARAR KOŞUSU DEĞİL"))
    return line


# --- (1) the risk base is a second source for the risk ---------------------------------------


def test_the_risk_base_sizes_the_trades_exactly_like_another_risk_percent() -> None:
    # Why the base cannot be left to config/strategies.yaml in a decision run: 0.5 % of a
    # fixed 50,000 is the planned 0.25 % of the 100,000 account, trade for trade. The
    # other direction (0.25 % of a fixed 200,000 = the script's 0.5 %) is the route that
    # was open: the recorded risk input read 0.25 and Go/No-Go confirmed the run.
    planned = run(request("mtf_trend_cont", PLANNED_RISK_PCT))
    pine = run(request("mtf_trend_cont", None))
    on_half_the_base = run(
        request(
            "mtf_trend_cont", None, overrides={"risk_base": "Fixed Balance", "fixed_bal": 50_000.0}
        )
    )

    assert quantities(planned) != quantities(pine)
    assert quantities(on_half_the_base) == quantities(planned)
    assert on_half_the_base.params["risk_percent"] == PINE_RISK_PCT  # yet it reads 0.5


@pytest.mark.parametrize("overrides", OTHER_BASES)
@pytest.mark.parametrize("strategy_id", sorted(STRATEGIES))
def test_a_decision_run_is_never_sized_on_another_risk_base(
    strategy_id: str, overrides: dict[str, object]
) -> None:
    spec = strategy_spec(strategy_id)

    with pytest.raises(RunnerError) as caught:
        run(request(strategy_id, PLANNED_RISK_PCT, series=short_fixture(), overrides=overrides))

    message = str(caught.value)
    assert message.startswith(f"{strategy_id}: planlanan işlem başı risk")
    assert "başlangıç bakiyesinin yüzdesidir" in message
    assert f"risk_base={overrides['risk_base']!r}" in message
    assert "--pine-risk" in message
    # Go/No-Go asks the same function what a decision run would have recorded: there is
    # no such run, so there is no expectation a run could be confirmed against.
    with pytest.raises(RunnerError, match="başlangıç bakiyesinin yüzdesidir"):
        effective_params(spec, overrides, planned_risk_pct=PLANNED_RISK_PCT)
    with pytest.raises(StrategyError, match="karar koşusu yapılamaz"):
        spec.with_planned_risk(overrides, PLANNED_RISK_PCT)


@pytest.mark.parametrize("overrides", OTHER_BASES)
@pytest.mark.parametrize("strategy_id", sorted(STRATEGIES))
def test_the_scripts_own_inputs_still_run_on_another_base_as_a_comparison(
    strategy_id: str, overrides: dict[str, object]
) -> None:
    # Parity and --pine-risk compare with TradingView at the script's inputs as
    # configured; the base is one of them. Such a run says what its percent is a percent of.
    result = run(request(strategy_id, None, series=short_fixture(), overrides=overrides))

    assert not result.decision
    assert result.label_suffix == "-pinerisk"
    assert result.params["risk_base"] == overrides["risk_base"]
    line = risk_line(result)
    assert f"risk bazı '{overrides['risk_base']}'" in line
    assert "hesabın başlangıç bakiyesinin yüzdesi DEĞİLDİR" in line
    assert f"risk bazı '{overrides['risk_base']}'" in not_decision_line(render_summary(result))
    # What parity records is still computable, with the base in it.
    assert effective_params(strategy_spec(strategy_id), overrides) == result.params


def test_naming_the_accounts_base_or_an_unused_fixed_balance_changes_nothing() -> None:
    # `risk_base: Initial Capital` is the account's base, and `fixed_bal` is read only
    # under "Fixed Balance": neither is another risk, so neither stops a decision run.
    plain = run(request("ny_orb_fvg", PLANNED_RISK_PCT))
    named = run(
        request(
            "ny_orb_fvg",
            PLANNED_RISK_PCT,
            overrides={"risk_base": "Initial Capital", "fixed_bal": 123_456.0},
        )
    )

    assert named.decision
    assert quantities(named) == quantities(plain) != []
    assert "risk bazı" not in risk_line(named)
    assert "risk bazı" not in risk_line(plain)


class _EquityBased(StrictModel):
    """A parameter model whose script sizes on the equity by default."""

    risk_pct: float = 0.5
    risk_base: str = "Account Equity"


def test_a_port_whose_own_base_is_not_the_accounts_cannot_be_registered() -> None:
    # "The script's own risk" is quoted everywhere as a percent of the account: a port
    # that defaults to another base is refused when its registry entry is built.
    assert ACCOUNT_RISK_BASE == "Initial Capital"
    orb = strategy_spec("ny_orb_fvg")
    fields: dict[str, Any] = {
        "strategy_id": "equity_based",
        "pine_file": "equity_based.pine",
        "params_model": _EquityBased,
        "strategy_class": orb.strategy_class,
        "leg_ids": ("equity_based",),
        "risk_param": "risk_pct",
    }

    with pytest.raises(ValueError, match="must default to 'Initial Capital'"):
        StrategySpec(**fields, risk_base_param="risk_base")
    with pytest.raises(ValueError, match="has no field 'riskBase'"):
        StrategySpec(**fields, risk_base_param="riskBase")
    # A script without the input always sizes on the initial capital.
    without = StrategySpec(**fields)
    assert without.sized_on_account({"risk_base": "Account Equity"})
    for spec in STRATEGIES.values():
        assert spec.risk_base_param == "risk_base"
        assert spec.sized_on_account(None)
        assert spec.sized_on_account({})
        assert not spec.sized_on_account({"risk_base": "Account Equity"})


def test_a_risk_base_in_strategies_yaml_stops_the_decision_run_and_gonogo_only(
    tmp_path: Path, config_dir: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    set_strategies(
        config_dir, '  ny_orb_fvg:\n    risk_base: "Fixed Balance"\n    fixed_bal: 200000\n'
    )
    reports = tmp_path / "reports"
    # The file itself is valid (the comparison commands need it), and `config check` says
    # what it stops.
    assert dict(load_strategy_overrides(config_dir / "strategies.yaml")["ny_orb_fvg"]) == {
        "risk_base": "Fixed Balance",
        "fixed_bal": 200000,
    }
    assert main(["config", "check", "--config-dir", str(config_dir)], clock=clock()) == 0
    check = capsys.readouterr().out
    assert "NOT: ny_orb_fvg: risk_base=Fixed Balance iken karar koşusu yapılamaz" in check

    code = cli_backtest(reports, config_dir)

    captured = capsys.readouterr()
    assert code == 1
    assert captured.err.startswith("HATA: ny_orb_fvg: planlanan işlem başı risk")
    assert "risk_base='Fixed Balance'" in captured.err
    assert len(captured.err.strip().splitlines()) == 1
    assert not reports.exists()
    # Go/No-Go cannot say what a decision run of this strategy would be: an error, no report.
    code = gonogo_code(reports, config_dir, "ny_orb_fvg")
    assert code == 2
    assert "karar koşusu yapılamaz" in capsys.readouterr().err
    assert not reports.exists()
    # The comparisons at the script's inputs still run, and name the base.
    assert cli_backtest(reports, config_dir, "--pine-risk") == 0
    out = capsys.readouterr().out
    assert "risk bazı 'Fixed Balance'" in out
    ((name, artifact),) = backtests(reports).items()
    assert name == "backtest_ny_orb_fvg-pinerisk_20261118T090000Z.json"
    assert artifact.params["risk_base"] == "Fixed Balance"
    assert cli_parity(reports, config_dir, "ny_orb_fvg") == 1  # the sample list is not this run's
    assert "risk bazı 'Fixed Balance'" in capsys.readouterr().out
    # A strategy the override does not touch is not held back by it.
    assert cli_backtest(reports, config_dir, "--strategy", "asian_sweep", "--no-artifact") == 0
    capsys.readouterr()


def test_at_equal_risks_another_base_keeps_pine_risk_a_comparison_run(
    tmp_path: Path, config_dir: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    # risk.yaml at the scripts' own 0.5: --pine-risk is normally the decision run then
    # (nothing differs). With another base something does differ, so the run stays what
    # it is, and the reason names the base instead of two equal percentages.
    set_risk(config_dir, "0.5")
    set_strategies(config_dir, "  ny_orb_fvg:\n    risk_base: Account Equity\n")
    reports = tmp_path / "reports"

    assert cli_backtest(reports, config_dir, "--pine-risk") == 0

    out = capsys.readouterr().out
    assert "karar koşusu olarak yapılıyor" not in out
    assert "risk bazı 'Account Equity'" in not_decision_line(out)
    ((name, _),) = backtests(reports).items()
    assert name == "backtest_ny_orb_fvg-pinerisk_20261118T090000Z.json"
    assert cli_backtest(reports, config_dir, now=clock(60)) == 1
    assert "karar koşusu yapılamaz" in capsys.readouterr().err


# --- (2) the code ceiling holds below the config loader too ---------------------------------


@pytest.mark.parametrize("planned", [0.5000001, 0.6, 1.0, 5])
def test_a_planned_risk_above_the_code_ceiling_is_refused_at_every_layer(planned: float) -> None:
    # config/risk.yaml cannot say it (the schema caps the field), but a caller of the
    # library could: SPEC §8.1's ceiling is enforced where the value is used as well.
    assert planned > MAX_RISK_PER_TRADE_PCT

    with pytest.raises(RunnerError, match="kod tavanını"):
        run(request("mtf_trend_cont", planned, series=short_fixture()))
    with pytest.raises(RunnerError, match="kod tavanını"):
        effective_params(strategy_spec("mtf_trend_cont"), planned_risk_pct=planned)
    with pytest.raises(GoNoGoError, match="kod tavanını"):
        evaluate(inputs(planned_risk_pct=planned))


def test_the_ceiling_itself_is_a_valid_planned_risk() -> None:
    result = run(request("ny_orb_fvg", MAX_RISK_PER_TRADE_PCT, series=short_fixture()))

    assert result.decision
    assert result.params["risk_pct"] == MAX_RISK_PER_TRADE_PCT
    assert evaluate(inputs(planned_risk_pct=MAX_RISK_PER_TRADE_PCT)).overall == "NO_GO"


@pytest.mark.parametrize("strategy_id", ["asian_sweep", MASTER])
def test_a_planned_risk_below_a_scripts_minimum_is_one_line_and_an_exit_code(
    tmp_path: Path, config_dir: Path, capsys: pytest.CaptureFixture[str], strategy_id: str
) -> None:
    # 0.05 % is a valid risk.yaml value; these scripts' inputs start at 0.1.
    set_risk(config_dir, "0.05")
    reports = tmp_path / "reports"

    code = cli_backtest(reports, config_dir, "--strategy", strategy_id)

    captured = capsys.readouterr()
    assert code == 1
    assert captured.err.startswith(f"HATA: {strategy_id}: planlanan işlem başı risk %0,05")
    assert len(captured.err.strip().splitlines()) == 1
    assert "Traceback" not in captured.err + captured.out
    assert not reports.exists()
    # A leg is judged with its script's parameters: the same refusal, exit code 2. Both
    # legs are given: one leg alone is refused earlier, for another reason (finding 87).
    judged = ",".join(MASTER_LEGS) if strategy_id == MASTER else strategy_id
    code = gonogo_code(reports, config_dir, judged)
    assert code == 2
    assert f"HATA: {strategy_id}: planlanan işlem başı risk %0,05" in capsys.readouterr().err


# --- (3) risk.yaml changes between the backtest and the report -------------------------------


@pytest.mark.parametrize(
    ("spelling", "shown"), [("0.3", "%0,3"), ("0.250000001", "%0,250000001"), ("0.5", "%0,5")]
)
def test_a_run_made_before_risk_yaml_changed_is_no_longer_the_decision_run(
    tmp_path: Path,
    config_dir: Path,
    capsys: pytest.CaptureFixture[str],
    spelling: str,
    shown: str,
) -> None:
    reports = tmp_path / "reports"
    assert cli_backtest(reports, config_dir) == 0
    set_risk(config_dir, spelling)
    capsys.readouterr()

    g2 = part(cli_gonogo(reports, config_dir), "G2", "ny_orb_fvg")

    reasons = str(g2["reasons"])
    assert g2["status"] == "FAIL"
    assert "yalnızca karşılaştırma koşusu var" in reasons
    # Exact, never rounded: 0.25 and 0.250000001 must not read the same.
    assert f"işlem başı risk %0,25 (planlanan risk {shown}, config/risk.yaml)" in reasons


@pytest.mark.parametrize("spelling", ["2.5e-1", ".25", "0.250", "+0.25"])
def test_another_spelling_of_the_same_number_changes_nothing(
    tmp_path: Path, config_dir: Path, capsys: pytest.CaptureFixture[str], spelling: str
) -> None:
    reports = tmp_path / "reports"
    assert cli_backtest(reports, config_dir) == 0
    set_risk(config_dir, spelling)
    capsys.readouterr()

    g2 = part(cli_gonogo(reports, config_dir), "G2", "ny_orb_fvg")

    assert g2["values"]["backtest_file"] == "backtest_ny_orb_fvg_20261118T090000Z.json"
    assert "karşılaştırma koşusu" not in str(g2["reasons"])


def test_a_quoted_risk_is_a_config_error_for_every_command_not_a_guess(
    tmp_path: Path, config_dir: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    set_risk(config_dir, '"0.25"')
    reports = tmp_path / "reports"

    assert cli_backtest(reports, config_dir) == 1
    assert "risk_per_trade_pct" in capsys.readouterr().err
    assert cli_backtest(reports, config_dir, "--pine-risk") == 1
    assert cli_parity(reports, config_dir, "ny_orb_fvg") == 2
    code = gonogo_code(reports, config_dir, "ny_orb_fvg")
    assert code == 2
    assert "Traceback" not in capsys.readouterr().err
    assert not reports.exists()


def test_two_different_risks_never_read_the_same_in_g4() -> None:
    # The simulation ran at 0.25, risk.yaml now says 0.250000001: both must be legible.
    result = gate_monte_carlo(inputs(planned_risk_pct=0.250000001))

    assert status(result) == "NOT_EVALUABLE"
    assert "simülasyon riski %0,25, planlanan risk %0,250000001" in result.gate.evidence
    assert CHAIN not in result.gate.evidence


# --- (4) a planned risk equal to the script's own ---------------------------------------------


def test_at_equal_risks_the_recorded_fields_decide_and_every_note_agrees(
    tmp_path: Path, config_dir: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    # Made while risk.yaml said 0.25: a decision run and a "-pinerisk" comparison run.
    reports = tmp_path / "reports"
    assert cli_backtest(reports, config_dir) == 0
    assert cli_backtest(reports, config_dir, "--pine-risk", now=clock(600)) == 0
    set_risk(config_dir, "0.5")
    # Made after: a default run and a --pine-risk run. Nothing differs between them, so
    # both are written as what they are, the decision run, under the bare label.
    assert cli_backtest(reports, config_dir, now=clock(1200)) == 0
    capsys.readouterr()
    assert cli_backtest(reports, config_dir, "--pine-risk", now=clock(1800)) == 0
    out = capsys.readouterr().out
    assert "karşılaştırılacak fark yok, koşu karar koşusu olarak yapılıyor" in out
    assert "KARAR KOŞUSU DEĞİL" not in out
    made = backtests(reports)
    assert sorted(made) == [
        "backtest_ny_orb_fvg-pinerisk_20261118T091000Z.json",
        "backtest_ny_orb_fvg_20261118T090000Z.json",
        "backtest_ny_orb_fvg_20261118T092000Z.json",
        "backtest_ny_orb_fvg_20261118T093000Z.json",
    ]
    default, with_flag = (made[f"backtest_ny_orb_fvg_20261118T09{m}00Z.json"] for m in ("20", "30"))
    assert default.params == with_flag.params
    assert default.notes == with_flag.notes

    g2 = part(cli_gonogo(reports, config_dir), "G2", "ny_orb_fvg")

    # The newest run at today's planned risk decides. Labels describe a run against the
    # configuration of its day (like "-oos…"); Go/No-Go reads recorded fields, so the old
    # "-pinerisk" file would decide too if it were the newest, and the old bare-label run
    # at 0.25 is a comparison run now.
    assert g2["values"]["backtest_file"] == "backtest_ny_orb_fvg_20261118T093000Z.json"
    old_pine = made["backtest_ny_orb_fvg-pinerisk_20261118T091000Z.json"]
    assert old_pine.params == default.params


def test_at_equal_risks_a_tv_ohlc_run_is_not_told_to_add_pine_risk(
    tmp_path: Path, config_dir: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    # The hint exists because a tv_ohlc run at the planned risk is not what TradingView
    # shows. At equal risks it is, and "--pine-risk ekleyin" would change nothing.
    set_risk(config_dir, "0.5")

    assert cli_backtest(tmp_path / "r", config_dir, "--fill-model", "tv_ohlc", "--no-artifact") == 0

    out = capsys.readouterr().out
    assert "--pine-risk ekleyin" not in out
    assert "İşlem başı risk: %0,5 — planlanan risk" in out


@pytest.mark.parametrize(
    ("options", "label"),
    [
        (("--fill-model", "tv_ohlc"), "ny_orb_fvg-tv"),
        (("--cost-free",), "ny_orb_fvg-free"),
        (("--cost-factor", "1.5"), "ny_orb_fvg-x1p5"),
        (("--oos-start", "2026-11-05"), "ny_orb_fvg-oos20261105"),
    ],
)
def test_at_equal_risks_the_note_never_calls_another_comparison_the_decision_run(
    tmp_path: Path,
    config_dir: Path,
    capsys: pytest.CaptureFixture[str],
    options: tuple[str, ...],
    label: str,
) -> None:
    # --pine-risk leaves no difference in the RISK; the run is still a comparison run for
    # its other option, and one output must not say both "made as the decision run" and
    # "not the decision run".
    set_risk(config_dir, "0.5")
    reports = tmp_path / "reports"

    assert cli_backtest(reports, config_dir, "--pine-risk", *options) == 0

    out = capsys.readouterr().out
    assert "! KARAR KOŞUSU DEĞİL" in out
    assert "karar koşusu olarak yapılıyor" not in out
    assert (
        "NOT: --pine-risk: ny_orb_fvg betiğinin kendi riski planlanan riskle aynı (%0,5); risk "
        "bakımından karşılaştırılacak fark yok, koşu planlanan riskle yapılıyor ve '-pinerisk' "
        "etiketi almıyor." in out
    )
    ((name, _),) = backtests(reports).items()
    assert name == f"backtest_{label}_20261118T090000Z.json"


# --- (5) --pine-risk with every other option: labels ------------------------------------------


def test_no_combination_of_options_shares_a_label_or_borrows_the_decision_label() -> None:
    midnight = int(dt.datetime(2026, 10, 20, tzinfo=dt.UTC).timestamp()) * 1000
    costs: list[tuple[bool, float]] = [(False, 1.0), (False, 1.5), (False, 2.0), (True, 1.0)]
    combos = itertools.product(
        FillModel, costs, (None, midnight, midnight + 3_600_000), (False, True)
    )
    ids = sorted({leg for spec in STRATEGIES.values() for leg in (spec.strategy_id, *spec.leg_ids)})
    # The mark means "at the script's own risk" in labels and in trade lists: no id has it.
    assert not any("pinerisk" in sid for sid in ids)
    seen: dict[str, object] = {}
    for fill_model, (cost_free, factor), other_oos, pine_risk in combos:
        suffix = label_suffix(
            fill_model=fill_model,
            cost_free=cost_free,
            cost_factor=0.0 if cost_free else factor,
            other_oos_start_ms=other_oos,
            pine_risk=pine_risk,
        )
        decision = (fill_model, cost_free, factor, other_oos, pine_risk) == (
            FillModel.PESSIMISTIC,
            False,
            1.0,
            None,
            False,
        )
        assert (suffix == "") is decision
        assert suffix.endswith("-pinerisk") is pine_risk
        for sid in ids:
            # Backtest and walk-forward labels, and the two cost-matrix labels.
            for label in (sid + suffix, f"{sid}{suffix}-cost-all", f"{sid}{suffix}-cost-oos"):
                assert LABEL.fullmatch(label), label
                assert label not in seen, label
                seen[label] = (sid, fill_model, cost_free, factor, other_oos, pine_risk)
    # 2 fill models x 4 cost settings x 3 OOS settings x 2 risks, three labels per id.
    assert len(seen) == 48 * 3 * len(ids)


def test_a_label_the_suffix_pushes_past_the_limit_is_refused_before_anything_is_written(
    tmp_path: Path, config_dir: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    # The only way past 64 characters: a cost factor typed in exponent notation with 17
    # digits, on the longest leg id, with every other suffix. A refusal, not a clipped
    # label (a clipped label could collide with another run's).
    reports = tmp_path / "reports"
    options = ["--strategy", MASTER, "--pine-risk", "--fill-model", "tv_ohlc"]
    options += ["--oos-start", "2026-11-05", "--cost-factor", "1.2345678901234567e-05"]

    code = cli_backtest(reports, config_dir, *options)

    captured = capsys.readouterr()
    assert code == 1
    assert captured.err.startswith("HATA: rapor oluşturulamadı: label:")
    assert "Traceback" not in captured.err + captured.out
    assert not reports.exists()


# --- (6) a --pine-risk trade list can never feed G4 -----------------------------------------


def chain(
    directory: Path, strategy_id: str, *, list_from: str, decision_run: bool = True
) -> tuple[list[AnyArtifact], bool]:
    """A decision run, a pine-risk run, a trade list from one of them and its Monte Carlo.

    Returns the artifacts and whether the two runs' trade-list rows are the same rows.
    The bars are presented as real only to reach G4's trade-list checks; G4 still cannot
    pass on a few dozen trades and a 200-path simulation.
    """
    series = unreal(fixture())
    decision = run(real_request(strategy_id, series=series))
    pine = run(real_request(strategy_id, series=series, planned_risk_pct=None))
    assert decision.decision
    assert not pine.decision
    source = {"decision": decision, "pine": pine}[list_from]
    path = directory / f"{strategy_id}_{list_from}.csv"
    write_trade_list(merge_runs([source], created=CREATED), path)
    trades, provenance = read_trades_csv(path)
    assert provenance is not None
    rules = ChallengeRules.from_venue(VENUE)
    params = MonteCarloParams(risk_pct=PLANNED_RISK_PCT, n_paths=200, max_days=60)
    blocks = build_day_blocks(
        trades, zone=VENUE.daily_reset.zone, reset=VENUE.daily_reset.time_of_day
    )
    outcomes = simulate(blocks, rules, params)
    mc = monte_carlo_artifact(
        summarize(outcomes),
        outcomes,
        label="ftmo",
        created=CREATED,
        params=params,
        rules=rules,
        trades_file=path.name,
        n_trades=len(trades),
        cost_r=0.0,
        venue="ftmo_2step_100k",
        synthetic=False,
        provenance=provenance,
        venue_profile=VENUE,
        intratrade=IntratradeModel.STOP,
        trades=trades,
    )
    artifacts: list[AnyArtifact] = [mc, *backtest_artifacts(pine, created=CREATED)]
    if decision_run:
        artifacts += backtest_artifacts(decision, created=CREATED)
    same_rows = [(e.entry_ts_ms, e.exit_ts_ms, e.r, e.mae_r) for e in decision.entries] == [
        (e.entry_ts_ms, e.exit_ts_ms, e.r, e.mae_r) for e in pine.entries
    ]
    return artifacts, same_rows


def g4(artifacts: Sequence[AnyArtifact], strategy_id: str) -> str:
    result = gate_monte_carlo(gonogo_inputs(loaded(artifacts), [strategy_id]))
    assert result.gate.status != "PASS"
    return result.gate.evidence


def test_the_decision_runs_own_trade_list_closes_the_chain(tmp_path: Path) -> None:
    # The control: the honest list is tied to its run, on the same fixtures as below.
    for strategy_id in ("ny_orb_fvg", "mtf_trend_cont"):
        artifacts, _ = chain(tmp_path, strategy_id, list_from="decision")

        evidence = g4(artifacts, strategy_id)

        assert CHAIN in evidence, strategy_id
        for text in (PINE_LIST, ROWS_DIFFER, NO_DECISION_RUN, "değerlendirilen stratejilerle"):
            assert text not in evidence, (strategy_id, text)


def test_a_pine_risk_list_with_the_decision_runs_very_rows_is_still_not_its_list(
    tmp_path: Path,
) -> None:
    # NY ORB on this fixture: every trade is taken at both risks and R is per unit, so
    # the two lists have the same rows. Tied by rows alone G4 took the pine-risk list
    # for the decision run's, while the command that wrote it said G4 could not pass.
    artifacts, same_rows = chain(tmp_path, "ny_orb_fvg", list_from="pine")
    assert same_rows

    evidence = g4(artifacts, "ny_orb_fvg")

    assert PINE_LIST in evidence
    assert "ny_orb_fvg-pinerisk" in evidence
    assert CHAIN not in evidence


def test_a_pine_risk_list_with_other_rows_is_refused_as_well(tmp_path: Path) -> None:
    # MTF Trend on the same fixture: 17 contracts against 8, other exits, other R.
    artifacts, same_rows = chain(tmp_path, "mtf_trend_cont", list_from="pine")
    assert not same_rows

    evidence = g4(artifacts, "mtf_trend_cont")

    assert PINE_LIST in evidence
    assert CHAIN not in evidence


def test_a_pine_risk_list_without_any_decision_run_hangs_on_nothing(tmp_path: Path) -> None:
    artifacts, _ = chain(tmp_path, "ny_orb_fvg", list_from="pine", decision_run=False)

    evidence = g4(artifacts, "ny_orb_fvg")

    assert PINE_LIST in evidence
    assert NO_DECISION_RUN in evidence
    assert CHAIN not in evidence


def test_g4_refuses_a_list_marked_pine_risk_whatever_else_matches() -> None:
    # The pure layer, on the set that otherwise passes every gate: the mark alone decides,
    # also when the simulation's row digests are (wrongly) filed under the bare ids.
    marked = trades_provenance(strategies=["asian_sweep-pinerisk", "ny_orb_fvg-pinerisk"])
    items = replaced(passing_set(), "monte_carlo_", monte_carlo_report(provenance=marked))

    result = gate_monte_carlo(inputs(items))

    assert status(result) == "NOT_EVALUABLE"
    assert PINE_LIST in result.gate.evidence
    assert "asian_sweep-pinerisk, ny_orb_fvg-pinerisk" in result.gate.evidence
    assert CHAIN not in result.gate.evidence
    assert status(gate_monte_carlo(inputs())) == "PASS"


@pytest.mark.parametrize(
    "changes",
    [
        {},
        {"cost_free": True},
        {"cost_factor": 1.5},
        {"fill_model": FillModel.TV_OHLC},
        {"fill_model": FillModel.TV_OHLC, "cost_factor": 2.0},
    ],
)
def test_every_trade_list_of_a_run_at_the_scripts_risk_is_marked(changes: dict[str, Any]) -> None:
    pine = run(real_request(MASTER, planned_risk_pct=None, **changes))
    planned = run(real_request(MASTER, **changes))

    marked = merge_runs([pine], created=CREATED)
    plain = merge_runs([planned], created=CREATED)

    wanted = tuple(sorted(f"{leg}-pinerisk" for leg in MASTER_LEGS))
    assert marked.provenance.strategy_ids == wanted
    assert {entry.strategy_id for entry in marked.entries} == set(wanted)
    assert plain.provenance.strategy_ids == tuple(sorted(MASTER_LEGS))
    assert {entry.strategy_id for entry in plain.entries} == set(MASTER_LEGS)
    # The run itself is not renamed: its statistics and artifacts keep the leg ids.
    assert {entry.strategy_id for entry in pine.entries} == set(MASTER_LEGS)
    assert [a.strategy_id for a in backtest_artifacts(pine, created=CREATED)] == [
        *MASTER_LEGS,
        MASTER,
    ]


def test_the_cli_marks_a_pine_risk_trade_list_in_its_rows_and_its_provenance_line(
    tmp_path: Path, config_dir: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    reports = tmp_path / "reports"
    pine_path, plain_path = tmp_path / "pine.csv", tmp_path / "plain.csv"
    pine_options = ["--pine-risk", "--strategy", MASTER, "--trades-out", str(pine_path)]

    assert cli_backtest(reports, config_dir, *pine_options, "--no-artifact") == 0
    pine_out = capsys.readouterr().out
    options = ["--strategy", MASTER, "--trades-out", str(plain_path), "--no-artifact"]
    assert cli_backtest(reports, config_dir, *options) == 0
    plain_out = capsys.readouterr().out

    note = (
        "NOT: işlem listesi betiğin kendi riskiyle (--pine-risk) üretildi: planlanan hesabın "
        "işlemleri değildir, Go/No-Go G4 bu listeyle GEÇTİ olamaz"
    )
    assert note in pine_out
    assert "satır kimlikleri '-pinerisk' taşır" in pine_out
    assert note not in plain_out
    pine_rows, pine_line = read_trades_csv(pine_path)
    plain_rows, plain_line = read_trades_csv(plain_path)
    assert pine_line is not None
    assert plain_line is not None
    marked = tuple(sorted(f"{leg}-pinerisk" for leg in MASTER_LEGS))
    assert pine_line.strategy_ids == marked
    assert {row.strategy_id for row in pine_rows} == set(marked)
    assert "stratejiler master_asian_sweep-pinerisk, master_ny_orb_fvg-pinerisk" in pine_out
    # The decision list is what it always was.
    assert plain_line.strategy_ids == tuple(sorted(MASTER_LEGS))
    assert {row.strategy_id for row in plain_rows} == set(MASTER_LEGS)
    # Nothing else in the line tells the two lists apart (which is why the mark is needed).
    assert dataclasses.replace(pine_line, strategy_ids=plain_line.strategy_ids) == plain_line
    # A marked id cannot be asked for either: Go/No-Go judges registered ids only.
    assert gonogo_code(reports, config_dir, "master_ny_orb_fvg-pinerisk") == 2
    assert "bilinmeyen strateji: master_ny_orb_fvg-pinerisk" in capsys.readouterr().err


def test_at_equal_risks_the_pine_risk_flag_writes_the_decision_list_unmarked(
    tmp_path: Path, config_dir: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    set_risk(config_dir, "0.5")
    path = tmp_path / "trades.csv"

    code = cli_backtest(
        tmp_path / "r", config_dir, "--pine-risk", "--trades-out", str(path), "--no-artifact"
    )

    assert code == 0
    assert "GEÇTİ olamaz" not in capsys.readouterr().out
    _, line = read_trades_csv(path)
    assert line is not None
    assert line.strategy_ids == ("ny_orb_fvg",)


# --- (7) parity: the script's own risk, and a note that tolerates nothing --------------------


def test_the_cli_parity_report_carries_the_expected_deviation_for_mtf_only(
    tmp_path: Path, config_dir: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    reports = tmp_path / "reports"
    # risk.yaml at another value still: parity never reads it.
    set_risk(config_dir, "0.1")

    assert cli_parity(reports / "mtf", config_dir, "mtf_trend_cont") == 1
    mtf_out = capsys.readouterr().out
    assert cli_parity(reports / "orb", config_dir, "ny_orb_fvg") == 1
    orb_out = capsys.readouterr().out

    assert f"{EXPECTED_DEVIATION_HEADING} (mtf_trend_cont):" in mtf_out
    assert EXPECTED_DEVIATION_HEADING not in orb_out
    (mtf_file,) = (reports / "mtf").iterdir()
    (orb_file,) = (reports / "orb").iterdir()
    mtf, orb = load_artifact(mtf_file), load_artifact(orb_file)
    assert isinstance(mtf, ParityArtifact)
    assert isinstance(orb, ParityArtifact)
    assert sum(note.startswith(EXPECTED_DEVIATION_HEADING) for note in mtf.notes) == 2
    assert not any(EXPECTED_DEVIATION_HEADING in note for note in orb.notes)
    assert mtf.params is not None
    assert orb.params is not None
    assert (mtf.params["risk_percent"], orb.params["risk_pct"]) == (PINE_RISK_PCT, PINE_RISK_PCT)
    # The note changed no verdict: the hand-written sample list is not these runs'.
    assert not mtf.passed
    assert not orb.passed


def test_a_differing_runner_exit_of_the_real_mtf_port_is_counted_note_or_no_note() -> None:
    # The Python side of a parity run of MTF Trend, and a TradingView list that agrees
    # with it except for the first runner exit (what the Pine script's missing stop
    # during the trail's warm-up would look like in an export).
    result = run(
        real_request("mtf_trend_cont", planned_risk_pct=None, fill_model=FillModel.TV_OHLC)
    )
    python = list(result.engine.trades)
    first_runner = next(i for i, row in enumerate(python) if "Runner" in row.comment)
    agreeing = [tv_of(row, trade_no=i + 1) for i, row in enumerate(python)]
    differing = list(agreeing)
    differing[first_runner] = dataclasses.replace(
        agreeing[first_runner], exit_price=agreeing[first_runner].exit_price - 6.0
    )
    settings: dict[str, Any] = {
        "fill_model": FillModel.TV_OHLC,
        "tick_size": 0.25,
        "tf_seconds": 300,
    }

    same = compare_trades(python, agreeing, **settings)
    moved = compare_trades(python, differing, **settings)

    assert (len(same.mismatches), same.passed) == (0, True)
    assert (len(moved.matched), len(moved.mismatches)) == (len(python) - 1, 1)
    assert not moved.passed
    # The comparison is not even told which strategy it compares: nothing in it could
    # tolerate a row because of the note. The note is added to the texts afterwards.
    assert "strategy_id" not in inspect.signature(compare_trades).parameters
    noted = parity_artifact(moved, label="mtf", created=CREATED, strategy_id="mtf_trend_cont")
    plain = parity_artifact(moved, label="orb", created=CREATED, strategy_id="ny_orb_fvg")
    for artifact in (noted, plain):
        assert (artifact.matched, artifact.mismatched, artifact.passed) == (
            len(python) - 1,
            1,
            False,
        )
    assert any(note.startswith(EXPECTED_DEVIATION_HEADING) for note in noted.notes)
    # The one way to leave the warm-up out is --warmup-bars, and it is counted and capped:
    # here the excluded share is above the limit, so the comparison cannot pass either.
    bars = [bar.ts_open_ms for bar in fixture().bars]
    cutoff = warmup_cutoff_ms(bars, python[first_runner].entry_bar_index + 1)
    excluded = compare_trades(python, differing, warmup_cutoff_ms=cutoff, **settings)
    assert excluded.exclusions.python_warmup >= 1
    assert not excluded.passed
    assert excluded.blocked_by


def test_an_older_parity_report_still_loads_and_is_not_evaluable(tmp_path: Path) -> None:
    # Written before parameters and the instrument were recorded (and before the risk
    # and deviation notes existed): readable, shown, never a G6 PASS.
    for index, changes in enumerate(({"params": None}, {"instrument": None})):
        old = parity_report(**changes)  # type: ignore[arg-type]
        path = write_artifact(old.artifact, tmp_path / str(index))
        again = load_artifact(path)
        assert again == old.artifact

        result = gate_parity(inputs(replaced(passing_set(), ORB_PARITY, old)))

        assert status(result) == "NOT_EVALUABLE"
        assert "eski rapor" in result.gate.evidence


# --- (8) what must not have moved ---------------------------------------------------------------


def test_config_check_passes_on_the_repositorys_config_and_names_the_rule(
    capsys: pytest.CaptureFixture[str],
) -> None:
    assert main(["config", "check", "--config-dir", str(CONFIG_DIR)], clock=clock()) == 0

    out = capsys.readouterr().out
    assert "risk_per_trade_pct 0.25 ≤ 0.5 (kod tavanı)" in out
    # "All strategies run on the Pine defaults" stopped being true with the rule: the
    # risk input of a decision run is config/risk.yaml's.
    assert (
        "tüm stratejiler Pine varsayılanlarıyla koşar (işlem başı risk dışında: karar "
        "koşularında config/risk.yaml riski)" in out
    )
    assert "Sonuç: GEÇERLİ" in out


def test_no_strategy_of_the_repositorys_config_changes_a_risk_input() -> None:
    overrides = load_strategy_overrides(CONFIG_DIR / "strategies.yaml")

    assert REPO_ROOT.joinpath("config", "risk.yaml").read_text("utf-8").count(RISK_LINE) == 1
    for strategy_id, spec in STRATEGIES.items():
        assert dict(overrides[strategy_id]) == {}
        # Every port: the script's own risk is the pinned 0.5 % of the initial capital.
        assert spec.pine_risk_pct == PINE_RISK_PCT
        assert spec.params().model_dump()["risk_base"] == "Initial Capital"


def test_a_backtest_report_never_calls_a_pine_risk_run_the_decision_run(
    tmp_path: Path, config_dir: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    reports = tmp_path / "reports"
    options = ["--pine-risk", "--matrix", "--walk-forward", "--cost-factor", "1.0"]

    assert cli_backtest(reports, config_dir, "--strategy", MASTER, *options) == 0

    capsys.readouterr()
    names = sorted(path.name for path in reports.iterdir())
    assert len(names) == 3 + 2 + 3  # backtests and walk-forwards per view, two matrices
    for name in names:
        artifact = load_artifact(reports / name)
        assert "-pinerisk" in artifact.label
        assert LABEL.fullmatch(artifact.label)
        assert any("karar koşusu değil" in note for note in artifact.notes), name
        if isinstance(artifact, BacktestArtifact):
            assert artifact.params["risk_pct"] == PINE_RISK_PCT
            assert any(note.startswith("KARŞILAŞTIRMA KOŞUSU") for note in artifact.notes)
    report = cli_gonogo(reports, config_dir, MASTER)
    g2 = part(report, "G2", MASTER)
    assert g2["status"] == "FAIL"
    assert "yalnızca karşılaştırma koşusu var" in str(g2["reasons"])
