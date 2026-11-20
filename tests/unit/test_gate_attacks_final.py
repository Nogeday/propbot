"""Gate attack, third round (2026-11-19): routes to a PASS the facts do not support.

Each route is something a user can do with the documented commands: edit config between
runs, pick Monte Carlo options, edit a trade list. Every test first rebuilt the route on
the code before its fix (where the gate saw nothing of the change), then checks that the
gate no longer passes and that the honest set still does. Routes:

- K: the venue's initial balance (sizing, so which trades exist) is not compared;
- L: the session settings (`session.min_gap_minutes`, the spread windows' zone) are not
  recorded, though strategies read `session.islastbar` from them;
- M: the instrument's `qty_step` / `min_qty` are not recorded, though they skip trades;
- N: the Monte Carlo seed, path count, horizon and skip probability are free choices
  (seed shopping, horizon shopping);
- P: a trade list whose times were edited keeps its count and its R / mae_r sums, so
  the day grouping the simulation depends on can be rewritten unseen.
"""

import dataclasses
import datetime as dt
import hashlib
import json
import shutil
from collections.abc import Callable
from pathlib import Path
from typing import Any

import pytest

from propbot.backtest.artifacts import (
    BacktestArtifact,
    LoadedArtifact,
    MonteCarloArtifact,
    artifact_file_name,
    load_artifact,
    trade_rows_digest,
)
from propbot.backtest.config import load_backtest_config
from propbot.backtest.gonogo import (
    evaluate,
    gate_data,
    gate_edge,
    gate_monte_carlo,
    gate_parity,
    gate_robustness,
)
from propbot.backtest.montecarlo import (
    DEFAULT_MAX_DAYS,
    DEFAULT_PATHS,
    DEFAULT_SEED,
    ChallengeRules,
    MonteCarloParams,
    simulate,
    summarize,
)
from propbot.backtest.runner import RunRequest, backtest_artifacts, run
from propbot.backtest.trades import IntratradeModel, Trade, build_day_blocks
from propbot.cli import main
from propbot.data.ohlcv import load_ohlcv
from support.paths import CONFIG_DIR, FIXTURES
from unit.test_gonogo import VENUE, inputs, passing_set, replaced

ORB_BT = "backtest_ny-orb-fvg_"
ORB_PARITY = "parity_ny-orb-fvg_"
MC = "monte_carlo_"
BARS = FIXTURES / "ohlcv" / "synthetic_5m.csv"
VENUE_FILE = CONFIG_DIR / "venues" / "ftmo_2step_100k.yaml"
START = dt.datetime(2026, 11, 18, 9, 0, tzinfo=dt.UTC)
DATA_GATES = (gate_data, gate_edge, gate_robustness, gate_monte_carlo)


def clock() -> Callable[[], dt.datetime]:
    moments = iter(START + dt.timedelta(seconds=step) for step in range(10_000))
    return lambda: next(moments)


def changed(item: LoadedArtifact, **changes: Any) -> LoadedArtifact:
    """The artifact with fields replaced ("a__b" reaches into a sub-object), re-validated.

    Through JSON, as a hand-edited file is read: the schema still applies.
    """
    raw = item.artifact.model_dump(mode="json")
    for key, value in changes.items():
        *path, last = key.split("__")
        target = raw
        for part in path:
            target = target[part]
        target[last] = value
    artifact = type(item.artifact).model_validate_json(json.dumps(raw))
    return LoadedArtifact(artifact_file_name(artifact), artifact)


def one(prefix: str) -> LoadedArtifact:
    return next(item for item in passing_set() if item.file_name.startswith(prefix))


def with_one(prefix: str, **changes: Any) -> list[LoadedArtifact]:
    return replaced(passing_set(), prefix, changed(one(prefix), **changes))


@pytest.fixture(scope="module")
def real_bars(tmp_path_factory: pytest.TempPathFactory) -> Path:
    """The synthetic fixture without its marker line: what a user could hand the tool as
    real bars (it cannot know). The tests only use it to see what a gate reads."""
    lines = BARS.read_text("utf-8").splitlines(keepends=True)
    assert lines[0].startswith("#propbot-synthetic")
    path = tmp_path_factory.mktemp("bars") / "mes_5m.csv"
    path.write_text("".join(lines[1:]), "utf-8")
    return path


def config_copy(tmp_path: Path, old: str = "", new: str = "", name: str = "backtest.yaml") -> Path:
    target = tmp_path / "config"
    shutil.copytree(CONFIG_DIR, target)
    if old:
        path = target / name
        text = path.read_text("utf-8")
        assert old in text
        path.write_text(text.replace(old, new, 1), "utf-8")
    return target


def request(series_path: Path, sid: str = "ny_orb_fvg", **changes: Any) -> RunRequest:
    config = load_backtest_config(CONFIG_DIR / "backtest.yaml")
    base = RunRequest(
        series=load_ohlcv(series_path),
        strategy_id=sid,
        instrument=config.instrument("MES"),
        base_costs=config.cost_model("MES"),
        initial_capital=100_000.0,
        oos_start_ms=int(config.samples.oos_start_utc.timestamp()) * 1000,
        min_gap_seconds=config.session.min_gap_minutes * 60,
    )
    return dataclasses.replace(base, **changes)


def total_r(req: RunRequest) -> tuple[int, float]:
    result = run(req)
    return len(result.entries), round(sum(entry.r for entry in result.entries), 6)


def gonogo_json(reports: Path, config_dir: Path = CONFIG_DIR) -> dict[str, Any]:
    arguments = ["gonogo", "--strategies", "ny_orb_fvg", "--config-dir", str(config_dir)]
    # An hour after the runs (each run's clock starts at START), so no input is "later".
    later = START + dt.timedelta(hours=1)
    assert main([*arguments, "--reports-dir", str(reports)], clock=lambda: later) == 1
    (report,) = (
        p for p in reports.iterdir() if p.name.startswith("go_no_go_") and p.suffix == ".json"
    )
    return json.loads(report.read_text("utf-8"))


def gate_json(report: dict[str, Any], gate_id: str) -> dict[str, Any]:
    return next(gate for gate in report["gates"] if gate["id"] == gate_id)


def test_the_honest_set_still_passes_every_data_gate() -> None:
    by_id = {gate.id: gate.status for gate in evaluate(inputs()).gates}
    assert by_id == {
        "G1": "PASS",
        "G2": "PASS",
        "G3": "PASS",
        "G4": "PASS",
        "G5": "NOT_EVALUABLE",
        "G6": "PASS",
        "G7": "PASS",
    }


# --- K: initial balance -------------------------------------------------------------------


def test_k_the_initial_balance_changes_which_trades_exist(real_bars: Path) -> None:
    # Why it must be compared: sizes are whole contracts and a size below min_qty is
    # skipped, so R depends on the capital (not "R is size-free").
    assert total_r(request(real_bars, "mtf_trend_cont")) != total_r(
        request(real_bars, "mtf_trend_cont", initial_capital=5_000.0)
    )


def test_k_a_run_on_another_initial_balance_is_not_the_decision_run() -> None:
    items = with_one(ORB_BT, initial_capital=5_000.0)

    for gate in DATA_GATES:
        assert gate(inputs(items)).gate.status != "PASS", gate.__name__
    evidence = gate_edge(inputs(items)).gate.evidence
    assert "initial_balance" in evidence
    assert "5.000" in evidence


def test_k_a_venue_balance_edited_after_the_run_blocks_it_too() -> None:
    venue = VENUE.model_copy(update={"initial_balance": 200_000.0})
    assert gate_edge(inputs(venue=venue)).gate.status != "PASS"


def test_k_a_parity_report_on_another_balance_or_without_one_is_not_evaluable() -> None:
    for value in (5_000.0, None):
        items = with_one(ORB_PARITY, initial_capital=value)
        assert gate_parity(inputs(items)).gate.status == "NOT_EVALUABLE"


# --- L: session settings ------------------------------------------------------------------


def _holed(real_bars: Path, tmp_path: Path) -> Path:
    """The bars with 11:00-11:45 New York removed every day: a 45-minute data hole."""
    lines = real_bars.read_text("utf-8").splitlines(keepends=True)
    kept = [lines[0]]
    for line in lines[1:]:
        moment = dt.datetime.fromisoformat(line.split(",", 1)[0])
        local = moment.astimezone(dt.timezone(dt.timedelta(hours=-5))).time()
        if not dt.time(11, 0) <= local < dt.time(11, 45):
            kept.append(line)
    path = tmp_path / "holed.csv"
    path.write_text("".join(kept), "utf-8")
    return path


def test_l_the_session_gap_changes_the_trades_on_data_with_a_hole(
    real_bars: Path, tmp_path: Path
) -> None:
    holed = _holed(real_bars, tmp_path)
    assert total_r(request(holed)) != total_r(request(holed, min_gap_seconds=24 * 3600))


def test_l_the_runner_records_the_session_gap_and_the_spread_zone(real_bars: Path) -> None:
    (artifact,) = backtest_artifacts(run(request(real_bars, min_gap_seconds=3600)), created=START)
    assert artifact.min_gap_seconds == 3600
    assert artifact.costs.spread_tz == "America/New_York"


@pytest.mark.parametrize("gap", [3600, None])
def test_l_a_run_with_another_or_an_unrecorded_session_gap_is_not_the_decision_run(
    gap: int | None,
) -> None:
    items = with_one(ORB_BT, min_gap_seconds=gap)
    for gate in DATA_GATES:
        assert gate(inputs(items)).gate.status != "PASS", gate.__name__
    assert "seans sonu boşluğu" in gate_edge(inputs(items)).gate.evidence


def test_l_an_unknown_configured_session_gap_confirms_no_decision_run() -> None:
    result = gate_edge(inputs(expected_min_gap_seconds=None))
    assert result.gate.status == "NOT_EVALUABLE"
    assert "session.min_gap_minutes" in result.gate.evidence


def test_l_a_run_with_spread_windows_read_in_another_zone_is_not_the_decision_run() -> None:
    items = with_one(ORB_BT, costs__spread_tz="Europe/London")
    assert gate_edge(inputs(items)).gate.status != "PASS"
    assert "spread_tz" in gate_edge(inputs(items)).gate.evidence


@pytest.mark.parametrize("gap", [3600, None])
def test_l_a_parity_report_with_another_session_gap_is_not_evaluable(gap: int | None) -> None:
    items = with_one(ORB_PARITY, min_gap_seconds=gap)
    result = gate_parity(inputs(items))
    assert result.gate.status == "NOT_EVALUABLE"
    assert "seans sonu boşluğu" in result.gate.evidence


def test_l_cli_a_backtest_made_under_another_session_gap_cannot_decide_after_the_edit_is_undone(
    real_bars: Path, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    edited = config_copy(tmp_path, "min_gap_minutes: 30", "min_gap_minutes: 60")
    reports = tmp_path / "reports"
    arguments = ["backtest", "--data", str(real_bars), "--config-dir", str(edited)]
    assert (
        main([*arguments, "--strategy", "ny_orb_fvg", "--reports-dir", str(reports)], clock=clock())
        == 0
    )
    (written,) = reports.iterdir()
    bt = load_artifact(written)
    assert isinstance(bt, BacktestArtifact)
    assert bt.min_gap_seconds == 3600

    # The config is back to 30 minutes when the report is made.
    g2 = gate_json(gonogo_json(reports), "G2")
    capsys.readouterr()
    assert g2["status"] == "NOT_EVALUABLE"
    assert "seans sonu boşluğu" in g2["evidence"]


# --- M: qty_step / min_qty ----------------------------------------------------------------


def test_m_min_qty_skips_trades(real_bars: Path) -> None:
    instrument = request(real_bars).instrument
    larger = dataclasses.replace(instrument, min_qty=4.0)
    req = request(real_bars, "mtf_trend_cont", initial_capital=5_000.0)
    assert total_r(req) != total_r(dataclasses.replace(req, instrument=larger))


def test_m_the_runner_records_qty_step_and_min_qty(real_bars: Path) -> None:
    (artifact,) = backtest_artifacts(run(request(real_bars)), created=START)
    assert (artifact.instrument.qty_step, artifact.instrument.min_qty) == (1, 1)


@pytest.mark.parametrize("field", ["qty_step", "min_qty"])
@pytest.mark.parametrize("value", [4.0, None])
def test_m_a_run_or_parity_with_another_size_grid_is_not_the_decision_run(
    field: str, value: float | None
) -> None:
    items = with_one(ORB_BT, **{f"instrument__{field}": value})
    assert gate_edge(inputs(items)).gate.status != "PASS"
    assert field in gate_edge(inputs(items)).gate.evidence
    items = with_one(ORB_PARITY, **{f"instrument__{field}": value})
    assert gate_parity(inputs(items)).gate.status == "NOT_EVALUABLE"


# --- N: Monte Carlo options -----------------------------------------------------------------


def _blocks() -> Any:
    start = dt.datetime(2024, 1, 2, 15, 0, tzinfo=dt.UTC)
    trades = [
        Trade(
            entry_utc=start + dt.timedelta(days=index * 1.4),
            exit_utc=start + dt.timedelta(days=index * 1.4, hours=1),
            r=2.0 if index % 3 == 0 else -1.0,
            mae_r=-1.0,
        )
        for index in range(200)
    ]
    return build_day_blocks(
        trades,
        zone=VENUE.daily_reset.zone,
        reset=VENUE.daily_reset.time_of_day,
        cost_r=0.0,
        intratrade=IntratradeModel.STOP,
    )


def test_n_the_seed_horizon_and_skip_probability_move_the_probabilities() -> None:
    blocks, rules = _blocks(), ChallengeRules.from_venue(VENUE)

    def p_pass(**changes: Any) -> float:
        params = MonteCarloParams(risk_pct=0.5, n_paths=2_000, **changes)
        return summarize(simulate(blocks, rules, params)).p_pass_phase[-1]

    base = p_pass()
    assert {p_pass(seed=seed) for seed in range(5)} != {base}
    assert p_pass(max_days=100) != base
    assert p_pass(skip_prob=0.5) != base


@pytest.mark.parametrize(
    ("change", "word"),
    [
        ({"inputs__seed": DEFAULT_SEED + 1}, "tohum"),
        ({"inputs__n_paths": DEFAULT_PATHS * 2}, "yol sayısı"),
        ({"inputs__max_days": DEFAULT_MAX_DAYS * 5}, "ufuk"),
        ({"inputs__max_days": 300}, "ufuk"),
        ({"inputs__skip_prob": 0.3}, "atlama"),
    ],
)
def test_n_a_monte_carlo_with_a_chosen_seed_size_horizon_or_skip_cannot_pass_g4(
    change: dict[str, Any], word: str
) -> None:
    if "inputs__n_paths" in change:
        # The histogram must count every path (schema); the counts are not judged.
        change = {**change, "drawdown_histogram__counts": [change["inputs__n_paths"]]}
    items = with_one(MC, **change)
    result = gate_monte_carlo(inputs(items))
    assert result.gate.status == "NOT_EVALUABLE"
    # The summary always names seed, horizon and skip: the reason is the knob clause.
    clause = result.gate.evidence.split("tekrar üretilebilir ayarlarla koşulmamış (", 1)[1]
    assert word in clause.split(")", 1)[0]


def test_n_the_decision_path_count_follows_a_stricter_configured_minimum() -> None:
    stricter = inputs().criteria.model_copy(update={"min_mc_paths": DEFAULT_PATHS * 2})
    result = gate_monte_carlo(inputs(criteria=stricter))
    assert result.gate.status == "NOT_EVALUABLE"
    items = with_one(
        MC,
        inputs__n_paths=DEFAULT_PATHS * 2,
        drawdown_histogram__counts=[DEFAULT_PATHS * 2],
    )
    assert gate_monte_carlo(inputs(items, criteria=stricter)).gate.status == "PASS"


# --- P: edited trade-list times -----------------------------------------------------------


def test_p_the_row_digest_ignores_order_and_sees_a_moved_time() -> None:
    first = dt.datetime(2025, 3, 3, 15, 0, tzinfo=dt.UTC)
    rows = [
        Trade(first, first + dt.timedelta(minutes=5), -1.0, "a", -1.0),
        Trade(first + dt.timedelta(hours=1), first + dt.timedelta(hours=2), 2.0, "a", -0.5),
    ]
    assert trade_rows_digest(rows) == trade_rows_digest(rows[::-1])
    moved = [
        rows[0],
        dataclasses.replace(rows[1], entry_utc=rows[1].entry_utc + dt.timedelta(days=1)),
    ]
    assert trade_rows_digest(moved) != trade_rows_digest(rows)
    assert len(trade_rows_digest(rows)) == 64


@pytest.mark.parametrize(
    "change",
    [
        {
            "inputs__trades_digests": {
                "ny_orb_fvg": "0" * 64,
                "asian_sweep": hashlib.sha256(b"asian_sweep").hexdigest(),
            }
        },
        {"inputs__trades_digests": None},
    ],
)
def test_p_a_monte_carlo_whose_rows_differ_from_the_decision_runs_is_not_evaluable(
    change: dict[str, Any],
) -> None:
    items = with_one(MC, **change)
    result = gate_monte_carlo(inputs(items))
    assert result.gate.status == "NOT_EVALUABLE"
    assert "satır" in result.gate.evidence


def test_p_a_decision_run_without_a_row_digest_cannot_tie_a_list() -> None:
    items = with_one(ORB_BT, trades_digest=None)
    result = gate_monte_carlo(inputs(items))
    assert result.gate.status == "NOT_EVALUABLE"
    assert "satır" in result.gate.evidence


def test_p_cli_a_list_with_moved_times_keeps_count_and_sums_but_not_its_digest(
    real_bars: Path, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    reports = tmp_path / "reports"
    trades = tmp_path / "trades.csv"
    arguments = ["backtest", "--data", str(real_bars), "--config-dir", str(CONFIG_DIR)]
    arguments += ["--strategy", "ny_orb_fvg", "--reports-dir", str(reports)]
    assert main([*arguments, "--trades-out", str(trades)], clock=clock()) == 0
    lines = trades.read_text("utf-8").splitlines(keepends=True)
    # The last row one day later: same rows, same R, same mae_r, another trading day.
    cells = lines[-1].split(",")
    for index in (0, 1):
        moment = dt.datetime.fromisoformat(cells[index]) + dt.timedelta(days=1)
        cells[index] = moment.strftime("%Y-%m-%dT%H:%M:%SZ")
    edited = tmp_path / "trades_edited.csv"
    edited.write_text("".join([*lines[:-1], ",".join(cells)]), "utf-8")

    runs = []
    for path in (trades, edited):
        command = ["montecarlo", "--trades", str(path), "--venue", str(VENUE_FILE)]
        command += ["--risk-config", str(CONFIG_DIR / "risk.yaml"), "--r-is-net"]
        assert main([*command, "--reports-dir", str(reports)], clock=clock()) == 0
    for path in sorted(reports.glob("monte_carlo_*.json")):
        artifact = load_artifact(path)
        assert isinstance(artifact, MonteCarloArtifact)
        runs.append(artifact)
    honest, moved = runs
    assert moved.inputs.n_trades == honest.inputs.n_trades
    assert moved.inputs.sum_r == honest.inputs.sum_r
    assert moved.inputs.sum_mae_r == honest.inputs.sum_mae_r
    assert moved.inputs.trades_digests != honest.inputs.trades_digests

    g4 = gate_json(gonogo_json(reports), "G4")
    capsys.readouterr()
    # Never PASS; this list also fails the probabilities, so the status itself is FAIL.
    assert g4["status"] != "PASS"
    assert "satırları" in g4["evidence"]


# --- older files ---------------------------------------------------------------------------

_NEW = {
    "backtest": ("min_gap_seconds", "trades_digest"),
    "parity": ("min_gap_seconds", "initial_capital"),
}


def test_older_files_without_this_rounds_fields_load_and_block_their_gates() -> None:
    items = []
    for item in passing_set():
        raw = item.artifact.model_dump(mode="json")
        for name in _NEW.get(raw["kind"], ()):
            raw.pop(name)
        if raw["kind"] in ("backtest", "parity"):
            raw["instrument"].pop("qty_step")
            raw["instrument"].pop("min_qty")
        if raw["kind"] == "backtest":
            raw["costs"].pop("spread_tz")
        if raw["kind"] == "monte_carlo":
            raw["inputs"].pop("trades_digests")
        artifact = type(item.artifact).model_validate_json(json.dumps(raw))
        items.append(LoadedArtifact(artifact_file_name(artifact), artifact))

    by_id = {gate.id: gate for gate in evaluate(inputs(items)).gates}
    for gate_id in ("G1", "G2", "G3", "G4", "G6"):
        assert by_id[gate_id].status == "NOT_EVALUABLE", gate_id
    assert "eski rapor" in by_id["G2"].evidence
    assert "eski rapor" in by_id["G6"].evidence
