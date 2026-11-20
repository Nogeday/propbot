"""Adversarial tests of the backtest runner: decision-run leaks, lost synthetic flags,
Master leg attribution, state between runs, factor keys, OOS boundary, degenerate data.

Every artifact a comparison run produces is fed to `gonogo.evaluate`, because the
Go/No-Go report trusts the artifact fields, never the label.
"""

import dataclasses
import datetime as dt
import functools
import math
import re
import time
from collections.abc import Sequence
from pathlib import Path
from types import MappingProxyType
from typing import Any
from zoneinfo import ZoneInfo

import pytest
from hypothesis import given, settings
from hypothesis import strategies as st

from propbot.backtest import gonogo
from propbot.backtest.artifacts import (
    SYNTHETIC_TAG,
    AnyArtifact,
    BacktestArtifact,
    LoadedArtifact,
    artifact_file_name,
    monte_carlo_artifact,
    ms_stamp,
)
from propbot.backtest.config import GoNoGoCriteria, load_backtest_config
from propbot.backtest.gonogo import GoNoGoInputs
from propbot.backtest.model import CostModel, FillModel, SpreadWindow
from propbot.backtest.montecarlo import ChallengeRules, MonteCarloParams, simulate, summarize
from propbot.backtest.runner import (
    RunnerError,
    RunRequest,
    all_artifacts_of,
    backtest_artifacts,
    cost_matrix_artifacts,
    effective_params,
    factor_shown,
    label_suffix,
    merge_runs,
    render_summary,
    run,
    run_cost_sensitivity,
    walk_forward_legs,
    write_trade_list,
)
from propbot.backtest.trades import IntratradeModel, build_day_blocks, read_trades_csv
from propbot.backtest.walkforward import WalkForwardConfig
from propbot.core.bars import Bar, BarSeries, DataSource
from propbot.core.session import NEW_YORK
from propbot.data.ohlcv import load_ohlcv
from propbot.data.sessions import DEFAULT_MIN_GAP_SECONDS
from propbot.risk import load_venue_profile
from propbot.risk.config import StrictModel
from propbot.strategies.base import Action, Entry, Exit, Side, StrategyContext
from propbot.strategies.registry import STRATEGIES, StrategySpec
from support.engine import START_MS, ScriptedStrategy, cfd, make_series, mes
from support.paths import CONFIG_DIR, FIXTURES

CREATED = dt.datetime(2026, 11, 18, 12, 0, tzinfo=dt.UTC)
CAPITAL = 100_000.0
OOS_5M_MS = 1_793_577_600_000  # 2026-11-02 00:00 UTC, inside the 5m fixture
MASTER_LEGS = ("master_ny_orb_fvg", "master_asian_sweep")
SHORT_WF = WalkForwardConfig(is_days=10, oos_days=5, step_days=5)
LABEL = re.compile(r"[a-z0-9_-]{1,64}")
# config/risk.yaml's risk_per_trade_pct. Since the user decision of 2026-11-19 (open
# finding 82) only a run sized at the planned risk is a decision run, so the requests
# here carry it; `planned_risk_pct=None` is the "--pine-risk" comparison run.
PLANNED_RISK_PCT = 0.25


@functools.cache
def fixture(name: str = "synthetic_5m.csv") -> BarSeries:
    return load_ohlcv(FIXTURES / "ohlcv" / name)


@functools.cache
def mes_costs() -> CostModel:
    return load_backtest_config(CONFIG_DIR / "backtest.yaml").cost_model("MES")


def request(
    strategy_id: str = "ny_orb_fvg", *, series: BarSeries | None = None, **changes: Any
) -> RunRequest:
    base = RunRequest(
        series=series if series is not None else fixture(),
        strategy_id=strategy_id,
        instrument=mes(),
        base_costs=mes_costs(),
        initial_capital=CAPITAL,
        oos_start_ms=OOS_5M_MS,
        planned_risk_pct=PLANNED_RISK_PCT,
    )
    return dataclasses.replace(base, **changes)


def assert_clean(text: str) -> None:
    """No Python spelling of a missing or non-finite value reaches the Turkish text."""
    assert re.search(r"\b(None|nan|inf|True|False)\b", text, re.IGNORECASE) is None, text


def unreal(series: BarSeries) -> BarSeries:
    """The same bars presented as real data with a file hash (only to probe the flags)."""
    source = dataclasses.replace(series.source, synthetic=False, sha256="ef" * 32)
    return dataclasses.replace(series, source=source)


# --- (1) no comparison run can pass for a decision run ------------------------------------

VENUE = load_venue_profile(CONFIG_DIR / "venues" / "ftmo_2step_100k.yaml")
CRITERIA = GoNoGoCriteria(
    min_data_years=2.0,
    required_tf_seconds=60,
    min_oos_t_stat=2.0,
    min_oos_to_is_ratio=0.5,
    min_p_pass_all_phases=0.60,
    max_p_breach=0.20,
    min_parity_rate=0.98,
)


def loaded(artifacts: Sequence[AnyArtifact]) -> list[LoadedArtifact]:
    return [LoadedArtifact(artifact_file_name(a), a) for a in artifacts]


def gonogo_inputs(items: Sequence[LoadedArtifact], strategy_ids: Sequence[str]) -> GoNoGoInputs:
    return GoNoGoInputs(
        artifacts=items,
        criteria=CRITERIA,
        venue_name="ftmo_2step_100k",
        venue=VENUE,
        strategy_ids=strategy_ids,
        planned_risk_pct=0.25,
        today=dt.date(2026, 11, 18),
        created=dt.datetime(2026, 11, 18, 18, 0, tzinfo=dt.UTC),
        compliance_files=(),
        expected_costs={"MES": mes_costs()},
        expected_instruments={"MES": mes()},
        # Leg ids of the Master portfolio are judged with the portfolio's parameters. A
        # decision run records the planned risk (2026-11-19), a parity run the script's.
        expected_params={
            sid: effective_params(_spec_of(sid), planned_risk_pct=PLANNED_RISK_PCT)
            for sid in strategy_ids
        },
        expected_parity_params={sid: effective_params(_spec_of(sid)) for sid in strategy_ids},
        risk_params={sid: _spec_of(sid).risk_param for sid in strategy_ids},
        artifacts_truncated=False,
        # The boundary these requests use is this world's configured one.
        expected_oos_start_utc=ms_stamp(OOS_5M_MS),
        # The requests' session gap is the configured one (gate attack L, 2026-11-19).
        expected_min_gap_seconds=DEFAULT_MIN_GAP_SECONDS,
    )


def _spec_of(sid: str) -> StrategySpec[Any]:
    return STRATEGIES.get(sid) or STRATEGIES["master_portfolio"]


def chosen_backtest(items: Sequence[LoadedArtifact], sid: str) -> str | None:
    """The file G2 judged for `sid` (None: no decision run was found)."""
    report = gonogo.evaluate(gonogo_inputs(items, [sid]))
    assert report.overall == "NO_GO"
    (g2,) = [gate for gate in report.gates if gate.id == "G2"]
    parts = g2.details["parts"]
    assert isinstance(parts, list)
    (part,) = [p for p in parts if isinstance(p, dict) and p["subject"] == sid]
    values = part["values"]
    assert isinstance(values, dict)
    if part["status"] == "NOT_EVALUABLE" and "olası karar" in str(part["reasons"]):
        return "BLOCKED"
    if g2.status == "FAIL" and "yalnızca karşılaştırma koşusu" in g2.evidence:
        return None
    name = values.get("backtest_file")
    return name if isinstance(name, str) else None


COMPARISONS = [
    {"fill_model": FillModel.TV_OHLC},
    {"cost_free": True},
    {"cost_factor": 0.0},
    {"cost_factor": 1.5},
    {"cost_factor": 0.999999},
    {"cost_factor": 1.0000000001},
    {"cost_factor": math.nextafter(1.0, 2.0)},
    {"fill_model": FillModel.TV_OHLC, "cost_factor": 2.0},
    {"base_costs": CostModel(commission_per_unit_side=0.0, slippage_ticks=0.0)},
    # Review A (2026-11-19): a run on another IS/OOS boundary than the configured one.
    {"oos_start_ms": OOS_5M_MS - 86_400_000, "configured_oos_start_ms": OOS_5M_MS},
    # User decision of 2026-11-19 (finding 82): a run at the script's own risk
    # ("--pine-risk"), and a run at any other risk than the planned one.
    {"planned_risk_pct": None},
    {"planned_risk_pct": None, "fill_model": FillModel.TV_OHLC},
]


@pytest.mark.parametrize("changes", COMPARISONS)
def test_no_comparison_run_gets_the_bare_label_or_is_chosen_by_gonogo(
    changes: dict[str, Any],
) -> None:
    result = run(request(series=unreal(fixture()), **changes))
    artifacts = backtest_artifacts(result, created=CREATED)

    assert not result.decision
    for artifact in artifacts:
        assert artifact.label != artifact.strategy_id
        assert LABEL.fullmatch(artifact.label)
        assert any("KARŞILAŞTIRMA KOŞUSU" in note for note in artifact.notes)
    # Alone it decides nothing; newer than a decision run it hides nothing.
    assert chosen_backtest(loaded(artifacts), "ny_orb_fvg") is None
    decision = backtest_artifacts(run(request(series=unreal(fixture()))), created=CREATED)
    later = backtest_artifacts(result, created=CREATED + dt.timedelta(hours=1))
    items = loaded([*decision, *later])
    assert chosen_backtest(items, "ny_orb_fvg") == artifact_file_name(decision[0])


def test_the_decision_run_is_chosen_by_gonogo_and_carries_factor_exactly_one() -> None:
    result = run(request(series=unreal(fixture())))
    (artifact,) = backtest_artifacts(result, created=CREATED)

    assert result.decision
    assert artifact.label == "ny_orb_fvg"
    assert artifact.cost_factor == 1.0
    assert artifact.fill_model == "pessimistic"
    assert chosen_backtest(loaded([artifact]), "ny_orb_fvg") == artifact_file_name(artifact)


def test_factor_labels_never_round_onto_the_decision_label_or_onto_each_other() -> None:
    near = (1.0000000001, 0.9999999999, 1.4999999999, 1.5, 1.5000000001, 123456789.0, 1e-7)
    suffixes = [
        label_suffix(fill_model=FillModel.PESSIMISTIC, cost_free=False, cost_factor=f) for f in near
    ]
    assert "" not in suffixes
    assert "-x1" not in suffixes
    assert len(set(suffixes)) == len(near)
    for suffix in suffixes:
        assert LABEL.fullmatch("master_ny_orb_fvg-tv" + suffix)


@settings(max_examples=200, deadline=None)
@given(st.floats(min_value=0.0, max_value=1e6, allow_nan=False, allow_infinity=False))
def test_every_non_unit_factor_gets_a_distinct_valid_label(factor: float) -> None:
    suffix = label_suffix(fill_model=FillModel.PESSIMISTIC, cost_free=False, cost_factor=factor)
    assert (suffix == "") is (factor == 1.0)
    assert LABEL.fullmatch("master_ny_orb_fvg-tv" + suffix)
    if factor != 1.0:
        # The label names the factor exactly: it reads back to the same float.
        text = suffix.removeprefix("-x").replace("p", ".")
        assert float(text) == factor


def test_a_factor_near_one_is_shown_exactly_in_the_warning_and_the_cost_line() -> None:
    result = run(request(cost_factor=1.00001))
    text = render_summary(result)

    assert "maliyet çarpanı 1,00001 (1,0 değil)" in text
    assert "Maliyetler (çarpan 1,00001)" in text
    assert factor_shown(1.0) == "1,0"
    assert factor_shown(1.5) == "1,5"


def test_overrides_keep_the_decision_label_but_are_loud_in_artifact_and_summary() -> None:
    # Chosen behaviour: config/strategies.yaml overrides ARE the configured strategy, so
    # the run may still decide; but it is not the Pine default, so every reader is told.
    spec = STRATEGIES["ny_orb_fvg"]
    name, default = next(
        (k, v) for k, v in spec.params().model_dump().items() if isinstance(v, bool)
    )
    result = run(request(overrides=MappingProxyType({name: not default})))
    (artifact,) = backtest_artifacts(result, created=CREATED)
    text = render_summary(result)

    assert artifact.params[name] is (not default)
    assert any(name in note and "Pine varsayılanlarından" in note for note in artifact.notes)
    assert "PARAMETRELER PINE VARSAYILANLARINDAN FARKLI" in text.split("\n\n")[0]
    assert f"{name}={'true' if not default else 'false'}" in text
    assert "True" not in text
    assert "False" not in text
    plain = render_summary(run(request()))
    assert "PARAMETRELER PINE" not in plain
    # Since 2026-11-19 a decision run is sized at the planned risk, so its parameter line
    # names the one input that is not the script's; a "--pine-risk" run keeps the old line.
    assert "Parametreler: Pine varsayılanları (işlem başı risk dışında)\n" in plain
    assert "İşlem başı risk: %0,25 — planlanan risk (config/risk.yaml" in plain
    pine = render_summary(run(request(planned_risk_pct=None)))
    assert "Parametreler: Pine varsayılanları\n" in pine


# --- (2) the synthetic flag survives every step ------------------------------------------


def test_in_memory_synthetic_series_without_a_file_stays_synthetic_down_to_monte_carlo(
    tmp_path: Path,
) -> None:
    series = dataclasses.replace(
        fixture(),
        source=DataSource(name="bellek", format="generic", sha256="", synthetic=True, bar_tz="UTC"),
    )
    result = run(request(series=series))
    sensitivity = run_cost_sensitivity(result, (1.0, 2.0))
    wf = walk_forward_legs(result, SHORT_WF)
    artifacts = all_artifacts_of(
        result, created=CREATED, sensitivity=sensitivity, walk_forward_results=wf
    )

    assert len(artifacts) == 1 + 2 + 1
    for artifact in artifacts:
        assert artifact.synthetic
        assert any(SYNTHETIC_TAG in note for note in artifact.notes)
    assert render_summary(result).startswith(f"!!! {SYNTHETIC_TAG}")

    merged = merge_runs([result], created=CREATED)
    assert merged.provenance.synthetic
    assert merged.provenance.data_sha256 == ""
    path = tmp_path / "trades.csv"
    write_trade_list(merged, path)
    trades, provenance = read_trades_csv(path)
    assert provenance is not None
    assert provenance.synthetic
    rules = ChallengeRules.from_venue(VENUE)
    params = MonteCarloParams(risk_pct=0.25, n_paths=200, max_days=60, seed=7)
    blocks = build_day_blocks(
        trades, zone=VENUE.daily_reset.zone, reset=VENUE.daily_reset.time_of_day
    )
    outcomes = simulate(blocks, rules, params)
    # The caller forgets the flag: the provenance line alone must carry it.
    mc = monte_carlo_artifact(
        summarize(outcomes),
        outcomes,
        label="ftmo",
        created=CREATED,
        params=params,
        rules=rules,
        trades_file=str(path),
        n_trades=len(trades),
        cost_r=0.0,
        venue="ftmo_2step_100k",
        synthetic=False,
        provenance=provenance,
    )
    assert mc.synthetic
    assert any(SYNTHETIC_TAG in note for note in mc.notes)


def test_a_real_series_artifacts_are_not_synthetic_and_carry_no_synthetic_note() -> None:
    result = run(request(series=unreal(fixture())))
    for artifact in backtest_artifacts(result, created=CREATED):
        assert not artifact.synthetic
        assert not any(SYNTHETIC_TAG in note for note in artifact.notes)
    assert SYNTHETIC_TAG not in render_summary(result)
    assert not merge_runs([result], created=CREATED).provenance.synthetic


# --- (3) Master portfolio ------------------------------------------------------------------


def test_master_entries_go_to_the_leg_named_in_the_trade_record_and_add_up() -> None:
    result = run(request("master_portfolio"))
    assert result.combined is not None

    for leg in result.legs:
        assert all(entry.strategy_id == leg.strategy_id for entry in leg.entries)
    leg_seqs = [entry.seq for leg in result.legs for entry in leg.entries]
    assert len(leg_seqs) == len(set(leg_seqs)) == len(result.combined.entries)
    combined_stats = result.combined.split.all
    leg_stats = [leg.split.all for leg in result.legs]
    assert combined_stats.n == sum(s.n for s in leg_stats)
    assert math.isclose(combined_stats.total_r, sum(s.total_r for s in leg_stats))
    assert math.isclose(combined_stats.total_pnl, sum(s.total_pnl for s in leg_stats))
    # The legs' standalone runs are other strategies (ids differ): no row is shared.
    alone = run(request("ny_orb_fvg"))
    assert not {e.strategy_id for e in alone.entries} & set(MASTER_LEGS)


# User decision of 2026-11-19 (open finding 72): the Master may be judged by its two legs
# or by its combined run, each from its own artifact. What Go/No-Go G4 needs for that is
# pinned here on real runner output (the bars are presented as real only to reach the
# trade-list checks; G4 still cannot pass on 39 trades and a 200-path simulation).
MASTER = "master_portfolio"
NOT_THE_JUDGED = "işlem listesinin stratejileri değerlendirilen stratejilerle aynı değil"
ROWS_DIFFER = "karar koşusununkilerle aynı değil"
COUNT_DIFFERS = "karar koşularının tamamlanmış işlem sayısı"
TWICE = "aynı işlemler birden fazla karar koşusunda"


def master_chain_artifacts(directory: Path) -> tuple[AnyArtifact, ...]:
    """The three artifacts of one Master decision run and the Monte Carlo run on its list."""
    result = run(request(MASTER, series=unreal(fixture())))
    path = directory / "master_trades.csv"
    write_trade_list(merge_runs([result], created=CREATED), path)
    trades, provenance = read_trades_csv(path)
    assert provenance is not None
    assert provenance.strategy_ids == tuple(sorted(MASTER_LEGS))  # the rows name the legs
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
    return (*backtest_artifacts(result, created=CREATED), mc)


def g4_of(artifacts: Sequence[AnyArtifact], strategy_ids: Sequence[str]) -> gonogo.GateResult:
    return gonogo.gate_monte_carlo(gonogo_inputs(loaded(artifacts), strategy_ids))


def used_backtests(result: gonogo.GateResult) -> list[str]:
    return sorted(
        item.artifact.strategy_id
        for item in result.used
        if isinstance(item.artifact, BacktestArtifact)
    )


def test_master_artifacts_record_the_row_digests_per_leg(tmp_path: Path) -> None:
    leg_a, leg_b, combined, _ = master_chain_artifacts(tmp_path)
    assert isinstance(leg_a, BacktestArtifact)
    assert isinstance(leg_b, BacktestArtifact)
    assert isinstance(combined, BacktestArtifact)

    # A leg's rows carry its own id; the combined view's rows carry the two leg ids.
    assert leg_a.trades_digests == {MASTER_LEGS[0]: leg_a.trades_digest}
    assert leg_b.trades_digests == {MASTER_LEGS[1]: leg_b.trades_digest}
    assert combined.trades_digests == {
        MASTER_LEGS[0]: leg_a.trades_digest,
        MASTER_LEGS[1]: leg_b.trades_digest,
    }
    assert combined.strategy_id == MASTER
    assert combined.stats_all.n == leg_a.stats_all.n + leg_b.stats_all.n


def test_g4_ties_the_master_trade_list_to_the_two_leg_runs(tmp_path: Path) -> None:
    leg_a, leg_b, _, mc = master_chain_artifacts(tmp_path)

    # Only the leg artifacts are given: each leg is judged from its own file.
    result = g4_of([leg_a, leg_b, mc], MASTER_LEGS)

    assert used_backtests(result) == sorted(MASTER_LEGS)
    for text in (NOT_THE_JUDGED, ROWS_DIFFER, COUNT_DIFFERS, TWICE, "karar koşusu olmayan"):
        assert text not in result.gate.evidence, text


def test_g4_ties_the_master_trade_list_to_the_combined_run_alone(tmp_path: Path) -> None:
    _, _, combined, mc = master_chain_artifacts(tmp_path)

    # Only the combined artifact is given: it is judged from its own file, although the
    # list's rows (and its provenance line) name the legs, never "master_portfolio".
    result = g4_of([combined, mc], [MASTER])

    assert used_backtests(result) == [MASTER]
    for text in (NOT_THE_JUDGED, ROWS_DIFFER, COUNT_DIFFERS, TWICE, "karar koşusu olmayan"):
        assert text not in result.gate.evidence, text


def test_g4_refuses_the_combined_id_together_with_one_of_its_legs(tmp_path: Path) -> None:
    leg_a, leg_b, combined, mc = master_chain_artifacts(tmp_path)

    result = g4_of([leg_a, leg_b, combined, mc], [MASTER, MASTER_LEGS[0]])

    # The leg's trades are in two judged runs: counted twice against one list. (The gate
    # is not PASS for other reasons too, see above; the reasons are what is pinned.)
    assert result.gate.status != "PASS"
    assert COUNT_DIFFERS in result.gate.evidence
    assert f"{TWICE}: {MASTER_LEGS[0]}" in result.gate.evidence
    assert "ya birleşik kimliğiyle ya da bacak kimlikleriyle" in result.gate.evidence


def test_g4_cannot_tie_an_older_combined_artifact_without_per_leg_digests(
    tmp_path: Path,
) -> None:
    _, _, combined, mc = master_chain_artifacts(tmp_path)
    assert isinstance(combined, BacktestArtifact)
    older = combined.model_copy(update={"trades_digests": None})

    result = g4_of([older, mc], [MASTER])

    # Fail closed: one digest under "master_portfolio" matches no row group of the list.
    assert result.gate.status != "PASS"
    assert NOT_THE_JUDGED in result.gate.evidence
    assert ROWS_DIFFER in result.gate.evidence


def test_g4_notices_a_master_list_with_one_legs_rows_from_another_run(tmp_path: Path) -> None:
    leg_a, leg_b, combined, mc = master_chain_artifacts(tmp_path)
    assert isinstance(combined, BacktestArtifact)
    assert isinstance(leg_b, BacktestArtifact)
    forged = dict(combined.trades_digests or {})
    forged[MASTER_LEGS[1]] = "0" * 64

    other_leg = leg_b.model_copy(update={"trades_digests": {MASTER_LEGS[1]: "0" * 64}})
    for artifacts, ids in (
        ([combined.model_copy(update={"trades_digests": forged}), mc], [MASTER]),
        ([leg_a, other_leg, mc], MASTER_LEGS),
    ):
        result = g4_of(artifacts, ids)
        assert result.gate.status != "PASS"
        assert f"{ROWS_DIFFER}: {MASTER_LEGS[1]}" in result.gate.evidence


class LegParams(StrictModel):
    every: int = 6
    risk_pct: float = 0.5  # the risk input every registry entry names (unused: 1 unit)


def _one_leg_trader(params: LegParams, leg: str) -> ScriptedStrategy:
    def on_bar(index: int, bar: Bar, ctx: StrategyContext) -> list[Action]:
        if ctx.position_size != 0.0 or index % params.every:
            return []
        entry_id = f"e{index}"
        return [
            Entry(entry_id, Side.LONG, 1.0, strategy_id=leg),
            Exit(f"x{index}", entry_id, stop=bar.close - 2.0, limit=bar.close + 2.0),
        ]

    return ScriptedStrategy(script=on_bar, strategy_id="pair")


def _pair_spec(trading_leg: str = "leg_a") -> StrategySpec[Any]:
    return StrategySpec(
        strategy_id="pair",
        pine_file="pair.pine",
        params_model=LegParams,
        strategy_class=lambda p: _one_leg_trader(p or LegParams(), trading_leg),
        leg_ids=("leg_a", "leg_b"),
        risk_param="risk_pct",
    )


def wave(n: int = 240) -> BarSeries:
    rows = []
    price = 4000.0
    for index in range(n):
        step = (1.25, -0.75, 2.5, -2.0, 0.5, -1.5, 3.0, -2.75)[index % 8]
        close = price + step
        rows.append((price, max(price, close) + 0.5, min(price, close) - 0.5, close))
        price = close
    return make_series(rows)


def test_a_portfolio_leg_without_trades_is_reported_not_dropped(tmp_path: Path) -> None:
    series = wave()
    result = run(
        request("pair", series=series, oos_start_ms=START_MS + 60 * 60_000),
        registry={"pair": _pair_spec()},
    )

    a, b = result.legs
    assert a.entries
    assert not b.entries
    assert b.split.all.n == 0
    assert result.combined is not None
    assert result.combined.entries == a.entries
    artifacts = backtest_artifacts(result, created=CREATED)
    assert [x.strategy_id for x in artifacts] == ["leg_a", "leg_b", "pair"]
    assert artifacts[1].stats_all.n == 0
    (_, matrix_oos) = cost_matrix_artifacts(
        run_cost_sensitivity(result, (1.0, 2.0), registry={"pair": _pair_spec()}),
        created=CREATED,
    )
    assert [row.strategy_id for row in matrix_oos.rows] == ["leg_a", "leg_b", "pair"]
    assert all(cell.n == 0 for cell in matrix_oos.rows[1].cells)
    text = render_summary(result)
    assert "leg_b (leg_b)" in text
    assert "None" not in text
    assert_clean(text)
    # A trade list naming a leg with no row would look like a list with rows removed.
    with pytest.raises(RunnerError, match="tamamlanmış işlemi yok: leg_b"):
        write_trade_list(merge_runs([result], created=CREATED), tmp_path / "t.csv")


# --- (4) every run starts fresh -------------------------------------------------------------


@pytest.mark.parametrize("strategy_id", ["ny_orb_fvg", "master_portfolio", "mr_bb_kc_rsi2"])
def test_each_factor_run_starts_fresh_whatever_ran_before(strategy_id: str) -> None:
    main = run(request(strategy_id))
    many = run_cost_sensitivity(main, (1.0, 1.5, 2.0, 3.0))
    few = run_cost_sensitivity(run(request(strategy_id)), (1.0, 3.0))
    alone = run(request(strategy_id, cost_factor=3.0))

    assert many.runs[-1].entries == few.runs[-1].entries == alone.entries
    assert run(request(strategy_id)).entries == main.entries
    # The registry builds a new instance per call: two builds are never the same object.
    spec = STRATEGIES[strategy_id]
    assert spec.build() is not spec.build()


# --- (5) the cost matrix's factor-1.0 cell is the main run ----------------------------------


def test_cost_matrix_unit_cell_equals_the_main_backtest_artifact_for_every_view() -> None:
    main = run(request("master_portfolio"))
    sensitivity = run_cost_sensitivity(main, (1.0, 1.4999999999, 1.5))
    by_id = {a.strategy_id: a for a in backtest_artifacts(main, created=CREATED)}
    matrix_all, matrix_oos = cost_matrix_artifacts(sensitivity, created=CREATED)

    assert matrix_all.factors == (1.0, 1.4999999999, 1.5)
    for matrix, sample in ((matrix_all, "stats_all"), (matrix_oos, "stats_oos")):
        for row in matrix.rows:
            unit = row.cells[0]
            stats = getattr(by_id[row.strategy_id], sample)
            assert unit.factor == 1.0
            assert (unit.n, unit.mean_r, unit.total_r) == (stats.n, stats.mean_r, stats.total_r)
    lines = render_summary(main, sensitivity=sensitivity).splitlines()
    header = lines[lines.index("Maliyet duyarlılığı (OOS beklentisi, ortalama R):") + 1]
    # Two factors apart by 1e-10 must not read the same column title.
    assert header.split() == ["x1,0", "x1,4999999999", "x1,5"]


def test_cost_matrix_refuses_a_main_run_whose_base_costs_are_zero() -> None:
    free = CostModel(commission_per_unit_side=0.0, slippage_ticks=0.0)
    main = run(request(base_costs=free))

    assert main.cost_free
    with pytest.raises(RunnerError, match="maliyetli"):
        run_cost_sensitivity(main, (1.0, 2.0))


def test_summary_shows_a_session_gap_that_is_not_whole_minutes() -> None:
    text = render_summary(run(request(min_gap_seconds=90)))
    assert "Seans sonu boşluğu: 1,5 dk" in text


# --- (6) IS/OOS boundary --------------------------------------------------------------------


def test_an_entry_exactly_at_the_oos_start_is_out_of_sample() -> None:
    base = run(request())
    target = base.entries[len(base.entries) // 2]
    result = run(request(oos_start_ms=target.entry_ts_ms))
    split = result.legs[0].split

    assert target.complete
    assert split.out_of_sample.n == sum(
        1 for e in result.entries if e.complete and e.entry_ts_ms >= target.entry_ts_ms
    )
    assert split.in_sample.n == sum(
        1 for e in result.entries if e.complete and e.entry_ts_ms < target.entry_ts_ms
    )
    (artifact,) = backtest_artifacts(result, created=CREATED)
    assert artifact.oos_start_utc == dt.datetime.fromtimestamp(
        target.entry_ts_ms / 1000, dt.UTC
    ).strftime("%Y-%m-%dT%H:%M:%SZ")


@pytest.mark.parametrize(
    "bad",
    [
        dt.datetime(2026, 11, 2),  # a naive datetime
        dt.datetime(2026, 11, 2, tzinfo=dt.UTC),
        1_793_577_600.0,  # float seconds
        True,
        -1,
        10**18,
    ],
)
def test_oos_start_must_be_integer_milliseconds_in_range(bad: object) -> None:
    with pytest.raises(RunnerError, match="OOS başlangıcı"):
        run(request(oos_start_ms=bad))


def test_oos_start_in_seconds_instead_of_milliseconds_is_flagged_loudly() -> None:
    result = run(request(oos_start_ms=OOS_5M_MS // 1000))
    text = render_summary(result)
    head = text.split("\n\n")[0]

    assert "IS örneklemi boş" in head
    (artifact,) = backtest_artifacts(result, created=CREATED)
    assert any("IS örneklemi boş" in note for note in artifact.notes)
    last = fixture().last_ts_ms
    assert last is not None
    late = render_summary(run(request(oos_start_ms=last + 1)))
    assert "OOS örneklemi boş" in late.split("\n\n")[0]
    assert "örneklemi boş" not in render_summary(run(request()))


# --- (7) walk-forward spans the bars, not the trades ----------------------------------------


def test_walk_forward_span_is_the_series_span_in_new_york_days() -> None:
    result = run(request("asian_sweep"))
    (leg,) = walk_forward_legs(result, SHORT_WF)
    series = fixture()
    assert series.first_ts_ms is not None
    assert series.last_ts_ms is not None

    first_day = dt.datetime.fromtimestamp(series.first_ts_ms / 1000, ZoneInfo(NEW_YORK)).date()
    last_day = dt.datetime.fromtimestamp(series.last_ts_ms / 1000, ZoneInfo(NEW_YORK)).date()
    assert leg.result.span_last_day == last_day
    first_entry_day = dt.datetime.fromtimestamp(
        result.entries[0].entry_ts_ms / 1000, ZoneInfo(NEW_YORK)
    )
    # The first bar's day comes before the first entry's day: the span starts at the bars.
    assert first_day < first_entry_day.date()
    assert leg.result.span_first_day == first_day


# --- (8) the trade list -----------------------------------------------------------------------


def test_trade_list_rows_are_the_complete_entries_net_of_costs_with_matching_ids(
    tmp_path: Path,
) -> None:
    result = run(request("master_portfolio"))
    merged = merge_runs([result], created=CREATED)
    path = tmp_path / "trades.csv"
    rows = write_trade_list(merged, path)
    trades, provenance = read_trades_csv(path)

    complete = [e for e in result.entries if e.complete]
    assert rows == len(trades) == len(complete) == result.combined.split.all.n  # type: ignore[union-attr]
    assert provenance is not None
    assert provenance.strategy_ids == tuple(sorted(MASTER_LEGS))
    assert {t.strategy_id for t in trades} == set(MASTER_LEGS)
    assert provenance.cost_factor == 1.0
    assert not provenance.cost_free
    assert provenance.fill_model == "pessimistic"
    free = {
        (e.entry_ts_ms, e.strategy_id): e.r
        for e in run(request("master_portfolio", cost_free=True)).entries
        if e.complete
    }
    by_key = {(e.entry_ts_ms, e.strategy_id): e for e in complete}
    for trade in trades:
        key = (int(trade.entry_utc.timestamp() * 1000), trade.strategy_id or "")
        entry = by_key[key]
        assert trade.r == entry.r
        if key in free:
            assert trade.r < free[key]  # costs are in it
        if trade.mae_r is not None:
            assert trade.mae_r <= 0.0


def _holds_to_the_end(params: LegParams | None) -> ScriptedStrategy:
    del params

    def on_bar(index: int, bar: Bar, ctx: StrategyContext) -> list[Action]:
        if ctx.position_size != 0.0 or index % 10:
            return []
        # Every tenth bar a trade; from bar 200 on the bracket is too wide to ever fill.
        width = 1_000.0 if index >= 200 else 2.0
        entry_id = f"e{index}"
        return [
            Entry(entry_id, Side.LONG, 1.0, strategy_id="hold"),
            Exit(f"x{index}", entry_id, stop=bar.close - width, limit=bar.close + width),
        ]

    return ScriptedStrategy(script=on_bar, strategy_id="hold")


def test_entries_open_at_the_end_of_data_are_counted_but_not_listed(tmp_path: Path) -> None:
    spec = StrategySpec(
        strategy_id="hold",
        pine_file="hold.pine",
        params_model=LegParams,
        strategy_class=_holds_to_the_end,
        leg_ids=("hold",),
        risk_param="risk_pct",
    )
    result = run(
        request("hold", series=wave(240), oos_start_ms=START_MS + 120 * 60_000),
        registry={"hold": spec},
    )
    open_n = sum(1 for e in result.entries if not e.complete)

    assert open_n == 1
    text = render_summary(result)
    assert f"veri bittiğinde açık (istatistik dışı): {open_n}" in text
    (leg,) = result.legs
    assert leg.split.all.incomplete == open_n
    assert leg.split.all.n == len(result.entries) - open_n
    (artifact,) = backtest_artifacts(result, created=CREATED)
    assert artifact.incomplete_entries == open_n
    rows = write_trade_list(merge_runs([result], created=CREATED), tmp_path / "t.csv")
    assert rows == len(result.entries) - open_n


def test_merge_refuses_a_slice_of_the_same_file_another_gap_and_another_capital() -> None:
    series = fixture()
    full = run(request("ny_orb_fvg"))
    sliced = dataclasses.replace(series, bars=series.bars[: len(series.bars) // 2])
    other = run(request("asian_sweep", series=sliced))
    assert other.request.series.source.sha256 == full.request.series.source.sha256

    with pytest.raises(RunnerError, match="farklı barlar"):
        merge_runs([full, other], created=CREATED)
    with pytest.raises(RunnerError, match="seans sonu"):
        merge_runs([full, run(request("asian_sweep", min_gap_seconds=3600))], created=CREATED)
    with pytest.raises(RunnerError, match="sermaye"):
        merge_runs([full, run(request("asian_sweep", initial_capital=50_000.0))], created=CREATED)
    with pytest.raises(RunnerError, match="dolum"):
        merge_runs(
            [full, run(request("asian_sweep", fill_model=FillModel.TV_OHLC))], created=CREATED
        )
    merged = merge_runs([full, run(request("asian_sweep"))], created=CREATED)
    assert merged.provenance.strategy_ids == ("asian_sweep", "ny_orb_fvg")
    assert [e.seq for e in merged.entries] == list(range(len(merged.entries)))
    assert [e.entry_ts_ms for e in merged.entries] == sorted(e.entry_ts_ms for e in merged.entries)


# --- (9) degenerate and large inputs ---------------------------------------------------------


@pytest.mark.parametrize("strategy_id", sorted(STRATEGIES))
def test_one_bar_and_a_series_without_a_session_gap_run_cleanly(strategy_id: str) -> None:
    one = make_series([(4000.0, 4001.0, 3999.0, 4000.5)])
    result = run(request(strategy_id, series=one, oos_start_ms=START_MS + 60_000))
    assert result.entries == ()
    wf = walk_forward_legs(result, SHORT_WF)
    assert all(leg.note and "tamamlanmış işlem yok" in leg.note for leg in wf)
    text = render_summary(result, walk_forward_results=wf)
    assert "1 bar" in text
    assert "None" not in text
    assert_clean(text)

    no_gap = wave(600)
    result = run(request(strategy_id, series=no_gap, oos_start_ms=START_MS + 300 * 60_000))
    backtest_artifacts(result, created=CREATED)
    assert "None" not in render_summary(result)


class ManyParams(StrictModel):
    risk_pct: float = 0.5  # the risk input every registry entry names (unused: 1 unit)


def _every_bar(params: ManyParams | None) -> ScriptedStrategy:
    del params

    def on_bar(index: int, bar: Bar, ctx: StrategyContext) -> list[Action]:
        if ctx.position_size != 0.0:
            return []
        entry_id = f"e{index}"
        return [
            Entry(entry_id, Side.LONG, 1.0, strategy_id="many"),
            Exit(f"x{index}", entry_id, stop=bar.close - 0.75, limit=bar.close + 0.75),
        ]

    return ScriptedStrategy(script=on_bar, strategy_id="many")


def test_thousands_of_trades_run_report_and_merge_quickly(tmp_path: Path) -> None:
    spec = StrategySpec(
        strategy_id="many",
        pine_file="many.pine",
        params_model=ManyParams,
        strategy_class=_every_bar,
        leg_ids=("many",),
        risk_param="risk_pct",
    )
    registry = {"many": spec}
    series = wave(12_000)
    start = time.perf_counter()
    result = run(
        request("many", series=series, oos_start_ms=START_MS + 6_000 * 60_000), registry=registry
    )
    sensitivity = run_cost_sensitivity(result, (1.0, 2.0), registry=registry)
    all_artifacts_of(result, created=CREATED, sensitivity=sensitivity)
    rows = write_trade_list(merge_runs([result], created=CREATED), tmp_path / "t.csv")
    render_summary(result, sensitivity=sensitivity)
    elapsed = time.perf_counter() - start

    assert len(result.entries) >= 2_000
    assert rows == result.legs[0].split.all.n
    assert elapsed < 10.0, f"{elapsed:.1f} s"


def test_cfd_instrument_with_a_spread_window_runs_and_is_shown() -> None:
    costs = CostModel(
        commission_per_unit_side=0.0,
        slippage_ticks=1.0,
        spread_points=0.5,
        spread_windows=(
            SpreadWindow(start_minute=9 * 60 + 25, end_minute=9 * 60 + 40, spread_points=1.2),
        ),
    )
    req = request("ny_orb_fvg", instrument=cfd(), base_costs=costs)
    result = run(req)
    double = run(dataclasses.replace(req, cost_factor=2.0))
    (artifact,) = backtest_artifacts(result, created=CREATED)
    text = render_summary(result)

    assert artifact.instrument.kind == "cfd"
    assert artifact.costs.spread_windows[0].spread_points == 1.2
    assert double.config.costs.spread_windows[0].spread_points == pytest.approx(2.4)
    assert "1 spread penceresi" in text
    assert "Enstrüman: US500.cash (cfd)" in text


# --- (10) the Turkish summary ------------------------------------------------------------------


@pytest.mark.parametrize(
    ("changes", "synthetic", "free", "comparison"),
    [
        ({}, True, False, False),
        ({"series": "real"}, False, False, False),
        ({"cost_free": True}, True, True, True),
        ({"series": "real", "fill_model": FillModel.TV_OHLC}, False, False, True),
        ({"series": "real", "cost_factor": 2.0}, False, False, True),
    ],
)
def test_summary_warnings_appear_exactly_when_they_apply(
    changes: dict[str, Any], synthetic: bool, free: bool, comparison: bool
) -> None:
    if changes.get("series") == "real":
        changes = {**changes, "series": unreal(fixture())}
    result = run(request("master_portfolio", **changes))
    sensitivity = run_cost_sensitivity(result, (1.0, 2.0)) if result.decision else None
    wf = walk_forward_legs(result, SHORT_WF)
    text = render_summary(result, sensitivity=sensitivity, walk_forward_results=wf)
    head = text.split("\n\n")[0] if text.startswith("!") else ""

    assert (SYNTHETIC_TAG in head) is synthetic
    assert (SYNTHETIC_TAG in text) is synthetic
    assert ("MALİYETSİZ KOŞU" in head) is free
    assert ("KARAR KOŞUSU DEĞİL" in head) is comparison
    assert "None" not in text
    assert_clean(text)
    assert "sentetik: " + ("EVET" if synthetic else "hayır") in text
    for artifact in backtest_artifacts(result, created=CREATED):
        assert isinstance(artifact, BacktestArtifact)
        assert artifact.synthetic is synthetic
