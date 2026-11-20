"""Backtest runner: legs, labels, artifacts, cost matrix, walk-forward, trade list, summary."""

import datetime as dt
import functools
import math
from collections.abc import Sequence
from pathlib import Path
from types import MappingProxyType
from typing import Any

import pytest

from propbot.backtest.artifacts import (
    COST_FREE_TAG,
    SYNTHETIC_TAG,
    CostMatrixArtifact,
    WalkForwardArtifact,
    load_artifact,
)
from propbot.backtest.config import load_backtest_config
from propbot.backtest.model import CostModel, FillModel
from propbot.backtest.runner import (
    RunnerError,
    RunRequest,
    RunResult,
    StrategyDefectError,
    all_artifacts_of,
    backtest_artifacts,
    cost_matrix_artifacts,
    effective_params,
    label_suffix,
    merge_runs,
    render_summary,
    run,
    run_cost_sensitivity,
    run_label,
    walk_forward_artifacts,
    walk_forward_legs,
    write_artifacts,
    write_trade_list,
)
from propbot.backtest.trades import read_trades_csv
from propbot.backtest.walkforward import WalkForwardConfig
from propbot.core.bars import Bar, BarSeries, DataSource
from propbot.data.ohlcv import load_ohlcv
from propbot.risk.config import StrictModel
from propbot.strategies.base import Action, Entry, Exit, Side, StrategyContext
from propbot.strategies.registry import STRATEGIES, StrategySpec, strategy_spec
from support.engine import START_MS, ScriptedStrategy, make_series, mes
from support.paths import CONFIG_DIR, FIXTURES

CREATED = dt.datetime(2026, 11, 18, 12, 0, tzinfo=dt.UTC)
LATER = dt.datetime(2026, 11, 18, 13, 30, tzinfo=dt.UTC)
CAPITAL = 100_000.0
# 2026-11-02 00:00 UTC: inside the span of both fixtures' first weeks (5m) / before 1m.
OOS_5M_MS = 1_793_577_600_000
FIXTURE_FILES = ("synthetic_5m.csv", "synthetic_1m.csv")
MASTER_LEGS = ("master_ny_orb_fvg", "master_asian_sweep")
SHORT_WF = WalkForwardConfig(is_days=10, oos_days=5, step_days=5)
# config/risk.yaml's risk_per_trade_pct. Since the user decision of 2026-11-19 (open
# finding 82) a decision run is sized at the planned risk, so the requests of this file
# carry it by default; `planned_risk_pct=None` asks for the script's own risk (a
# comparison run labelled "-pinerisk", covered in test_risk_rule.py).
PLANNED_RISK_PCT = 0.25
DEFAULT_WF = WalkForwardConfig(is_days=180, oos_days=60, step_days=60)


@functools.cache
def fixture(name: str) -> BarSeries:
    return load_ohlcv(FIXTURES / "ohlcv" / name)


@functools.cache
def mes_costs() -> CostModel:
    return load_backtest_config(CONFIG_DIR / "backtest.yaml").cost_model("MES")


def request(
    strategy_id: str = "ny_orb_fvg",
    *,
    series: BarSeries | None = None,
    fill_model: FillModel = FillModel.PESSIMISTIC,
    cost_factor: float = 1.0,
    cost_free: bool = False,
    overrides: dict[str, object] | None = None,
    oos_start_ms: int = OOS_5M_MS,
    costs: CostModel | None = None,
    planned_risk_pct: float | None = PLANNED_RISK_PCT,
) -> RunRequest:
    return RunRequest(
        series=series if series is not None else fixture("synthetic_5m.csv"),
        strategy_id=strategy_id,
        instrument=mes(),
        base_costs=costs if costs is not None else mes_costs(),
        initial_capital=CAPITAL,
        oos_start_ms=oos_start_ms,
        overrides=MappingProxyType(overrides or {}),
        min_gap_seconds=1800,
        fill_model=fill_model,
        cost_factor=cost_factor,
        cost_free=cost_free,
        planned_risk_pct=planned_risk_pct,
    )


# --- scripted strategies through an injected registry -------------------------------------


class ScriptParams(StrictModel):
    every: int = 4  # bars between entry attempts
    points: float = 2.0  # stop / target distance
    leak_leg: str = ""  # a leg id the spec does not declare (for the refusal test)
    no_stop: bool = False  # break invariant 2 (for the defect test)
    trade: bool = True
    # Every registry entry names its risk input (StrategySpec.risk_param); the script
    # trades one unit whatever it says, so the planned risk changes no scripted trade.
    risk_pct: float = 0.5


def _script(params: ScriptParams, legs: Sequence[str]) -> ScriptedStrategy:
    count = [0]

    def on_bar(index: int, bar: Bar, ctx: StrategyContext) -> list[Action]:
        if not params.trade or ctx.position_size != 0.0 or index % params.every:
            return []
        count[0] += 1
        leg = params.leak_leg or legs[count[0] % len(legs)]
        entry_id = f"e{index}"
        side = Side.LONG if count[0] % 3 else Side.SHORT
        sign = 1.0 if side is Side.LONG else -1.0
        entry = Entry(entry_id, side, 1.0, strategy_id=leg)
        if params.no_stop:
            return [entry]
        return [
            entry,
            Exit(
                f"x{index}",
                entry_id,
                stop=bar.close - sign * params.points,
                limit=bar.close + sign * params.points,
            ),
        ]

    return ScriptedStrategy(script=on_bar, strategy_id="scripted")


def scripted_spec(
    legs: Sequence[str] = ("leg_a", "leg_b"), strategy_id: str = "scripted"
) -> StrategySpec[Any]:
    return StrategySpec(
        strategy_id=strategy_id,
        pine_file="scripted.pine",
        params_model=ScriptParams,
        strategy_class=lambda p: _script(p or ScriptParams(), legs),
        leg_ids=tuple(legs),
        risk_param="risk_pct",
    )


def wave(n: int = 120) -> BarSeries:
    rows = []
    price = 4000.0
    for index in range(n):
        step = (1.25, -0.75, 2.5, -2.0, 0.5, -1.5, 3.0, -2.75)[index % 8]
        close = price + step
        rows.append((price, max(price, close) + 0.5, min(price, close) - 0.5, close))
        price = close
    return make_series(rows)


def scripted(
    series: BarSeries | None = None,
    *,
    legs: Sequence[str] = ("leg_a", "leg_b"),
    **kwargs: Any,
) -> RunResult:
    req = request("scripted", series=series if series is not None else wave(), **kwargs)
    return run(req, registry={"scripted": scripted_spec(legs)})


# --- every registered strategy ------------------------------------------------------------


@pytest.mark.parametrize("file_name", FIXTURE_FILES)
@pytest.mark.parametrize("fill_model", list(FillModel))
@pytest.mark.parametrize("strategy_id", sorted(STRATEGIES))
def test_every_registered_strategy_runs_through_the_runner_in_both_fill_models(
    strategy_id: str, fill_model: FillModel, file_name: str
) -> None:
    result = run(request(strategy_id, series=fixture(file_name), fill_model=fill_model))

    spec = strategy_spec(strategy_id)
    assert tuple(leg.strategy_id for leg in result.legs) == spec.leg_ids
    assert sum(len(leg.entries) for leg in result.legs) == len(result.entries)
    artifacts = backtest_artifacts(result, created=CREATED)
    assert len(artifacts) == len(result.views)
    for artifact in artifacts:
        assert artifact.synthetic is True
        assert any(SYNTHETIC_TAG in note for note in artifact.notes)
        assert artifact.fill_model == fill_model.value
    summary = render_summary(result)
    assert summary.startswith(f"!!! {SYNTHETIC_TAG}")


def test_master_portfolio_entries_are_split_into_its_two_legs_plus_a_combined_view() -> None:
    result = run(request("master_portfolio"))

    assert tuple(leg.strategy_id for leg in result.legs) == MASTER_LEGS
    for leg in result.legs:
        assert leg.entries, f"{leg.strategy_id} has no entry on the 5m fixture"
        assert {entry.strategy_id for entry in leg.entries} == {leg.strategy_id}
    assert result.combined is not None
    assert result.combined.strategy_id == "master_portfolio"
    assert result.combined.entries == result.entries
    seqs = sorted(entry.seq for leg in result.legs for entry in leg.entries)
    assert seqs == [entry.seq for entry in result.entries]

    artifacts = backtest_artifacts(result, created=CREATED)
    assert [(a.strategy_id, a.label) for a in artifacts] == [
        ("master_ny_orb_fvg", "master_ny_orb_fvg"),
        ("master_asian_sweep", "master_asian_sweep"),
        ("master_portfolio", "master_portfolio"),
    ]
    by_id = {a.strategy_id: a for a in artifacts}
    assert by_id["master_portfolio"].stats_all.n == sum(
        by_id[leg].stats_all.n for leg in MASTER_LEGS
    )


def test_single_strategy_port_has_one_leg_and_no_combined_view() -> None:
    result = run(request("asian_sweep"))

    assert [leg.strategy_id for leg in result.legs] == ["asian_sweep"]
    assert result.combined is None
    assert result.views == result.legs


# --- labels -------------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("fill_model", "cost_free", "factor", "suffix"),
    [
        (FillModel.PESSIMISTIC, False, 1.0, ""),
        (FillModel.PESSIMISTIC, False, 1.5, "-x1p5"),
        (FillModel.PESSIMISTIC, False, 2.0, "-x2"),
        (FillModel.PESSIMISTIC, False, 0.75, "-x0p75"),
        (FillModel.PESSIMISTIC, True, 1.0, "-free"),
        (FillModel.TV_OHLC, False, 1.0, "-tv"),
        (FillModel.TV_OHLC, False, 1.5, "-tv-x1p5"),
        (FillModel.TV_OHLC, True, 1.0, "-tv-free"),
    ],
)
def test_label_policy_names_every_way_a_run_differs_from_the_decision_run(
    fill_model: FillModel, cost_free: bool, factor: float, suffix: str
) -> None:
    assert label_suffix(fill_model=fill_model, cost_free=cost_free, cost_factor=factor) == suffix
    label = run_label("ny_orb_fvg", fill_model=fill_model, cost_free=cost_free, cost_factor=factor)
    assert label == "ny_orb_fvg" + suffix

    result = run(request(fill_model=fill_model, cost_free=cost_free, cost_factor=factor))
    assert result.label_suffix == suffix
    assert result.decision is (suffix == "")
    (artifact,) = backtest_artifacts(result, created=CREATED)
    assert artifact.label == label
    if suffix:
        assert any("KARŞILAŞTIRMA KOŞUSU" in note for note in artifact.notes)
        assert "KARAR KOŞUSU DEĞİL" in render_summary(result)
    else:
        assert not any("KARŞILAŞTIRMA KOŞUSU" in note for note in artifact.notes)
        assert "KARAR KOŞUSU DEĞİL" not in render_summary(result)


def test_master_leg_labels_carry_the_comparison_suffix_too() -> None:
    result = run(request("master_portfolio", fill_model=FillModel.TV_OHLC, cost_factor=1.5))

    assert [view.label for view in result.views] == [
        "master_ny_orb_fvg-tv-x1p5",
        "master_asian_sweep-tv-x1p5",
        "master_portfolio-tv-x1p5",
    ]


def test_zero_base_costs_make_a_cost_free_run_even_without_the_switch() -> None:
    free = CostModel(commission_per_unit_side=0.0, slippage_ticks=0.0)
    result = run(request(costs=free))

    assert result.cost_free
    assert result.label_suffix == "-free"
    assert not result.decision
    (artifact,) = backtest_artifacts(result, created=CREATED)
    assert artifact.costs.cost_free
    assert any(COST_FREE_TAG in note for note in artifact.notes)


def test_cost_free_switch_records_factor_zero_and_warns_loudly() -> None:
    result = run(request(cost_free=True))

    (artifact,) = backtest_artifacts(result, created=CREATED)
    assert artifact.cost_factor == 0.0
    assert artifact.costs.cost_free
    summary = render_summary(result)
    assert f"!!! {COST_FREE_TAG}" in summary
    assert summary.index(SYNTHETIC_TAG) < summary.index(COST_FREE_TAG)


# --- artifacts ----------------------------------------------------------------------------


def test_fixture_artifacts_are_synthetic_and_round_trip_through_files(tmp_path: Path) -> None:
    result = run(request("master_portfolio"))
    sensitivity = run_cost_sensitivity(result, (1.0, 1.5, 2.0))
    wf = walk_forward_legs(result, DEFAULT_WF)
    artifacts = all_artifacts_of(
        result, created=CREATED, sensitivity=sensitivity, walk_forward_results=wf
    )

    kinds = [artifact.kind for artifact in artifacts]
    assert kinds == ["backtest"] * 3 + ["cost_matrix"] * 2 + ["walk_forward"] * 3
    paths = write_artifacts(artifacts, tmp_path)
    assert len(set(paths)) == len(artifacts)
    for artifact, path in zip(artifacts, paths, strict=True):
        assert artifact.synthetic
        assert artifact.data is not None
        assert artifact.data.synthetic
        assert artifact.data.sha256 == fixture("synthetic_5m.csv").source.sha256
        assert artifact.data.file_name == "synthetic_5m.csv"
        assert any(SYNTHETIC_TAG in note for note in artifact.notes)
        assert load_artifact(path) == artifact


def test_artifacts_of_a_real_series_are_not_synthetic() -> None:
    real = wave()
    source = DataSource(
        name="real.csv", format="generic", sha256="a" * 64, synthetic=False, bar_tz="UTC"
    )
    series = BarSeries(symbol="MES", tf_seconds=60, bars=real.bars, source=source)
    result = scripted(series)

    for artifact in backtest_artifacts(result, created=CREATED):
        assert not artifact.synthetic
        assert not any(SYNTHETIC_TAG in note for note in artifact.notes)
    assert SYNTHETIC_TAG not in render_summary(result)


def test_backtest_artifact_records_factor_fill_model_costs_and_counts() -> None:
    result = run(request(fill_model=FillModel.TV_OHLC, cost_factor=1.5))

    (artifact,) = backtest_artifacts(result, created=CREATED)
    assert artifact.cost_factor == 1.5
    assert artifact.fill_model == "tv_ohlc"
    assert artifact.costs.commission_per_unit_side == mes_costs().commission_per_unit_side * 1.5
    assert artifact.costs.slippage_ticks == mes_costs().slippage_ticks * 1.5
    assert artifact.initial_capital == CAPITAL
    assert artifact.dropped_entries == result.engine.dropped_entries
    assert artifact.incomplete_entries == sum(1 for e in result.entries if not e.complete)
    assert artifact.stats_all.n == result.legs[0].split.all.n


def test_effective_params_equal_pine_defaults_without_overrides() -> None:
    # Changed with the user decision of 2026-11-19: only a run at the script's own risk
    # records the Pine defaults unchanged; a decision run records the planned risk in
    # the risk input and the defaults everywhere else (second half).
    result = run(request(planned_risk_pct=None))

    (artifact,) = backtest_artifacts(result, created=CREATED)
    defaults = strategy_spec("ny_orb_fvg").params().model_dump(mode="json")
    assert artifact.params == defaults
    assert artifact.params["start_date"] == "2024-01-01T00:00:00Z"
    assert "Parametreler: Pine varsayılanları\n" in render_summary(result)

    planned = run(request())
    (decision,) = backtest_artifacts(planned, created=CREATED)
    assert defaults["risk_pct"] == 0.5
    assert decision.params == {**defaults, "risk_pct": PLANNED_RISK_PCT}
    assert "Parametreler: Pine varsayılanları (işlem başı risk dışında)\n" in render_summary(
        planned
    )


def test_effective_params_show_an_override() -> None:
    result = run(request(overrides={"or_minutes": 15}))

    (artifact,) = backtest_artifacts(result, created=CREATED)
    assert artifact.params["or_minutes"] == 15
    assert any("or_minutes=15" in note for note in artifact.notes)
    assert "or_minutes=15" in render_summary(result)
    assert effective_params(strategy_spec("ny_orb_fvg"))["or_minutes"] == 5


def test_two_runs_give_identical_artifacts_apart_from_the_creation_time() -> None:
    first = run(request("master_portfolio"))
    second = run(request("master_portfolio"))

    a = all_artifacts_of(
        first, created=CREATED, walk_forward_results=walk_forward_legs(first, SHORT_WF)
    )
    b = all_artifacts_of(
        second, created=LATER, walk_forward_results=walk_forward_legs(second, SHORT_WF)
    )
    assert len(a) == len(b)
    for left, right in zip(a, b, strict=True):
        assert left.created_utc != right.created_utc
        assert left.model_dump(exclude={"created_utc"}) == right.model_dump(exclude={"created_utc"})


# --- cost matrix --------------------------------------------------------------------------


def test_cost_matrix_factor_one_run_is_the_main_run_itself() -> None:
    main = run(request("master_portfolio"))
    sensitivity = run_cost_sensitivity(main, (1.0, 1.5, 2.0))

    assert sensitivity.runs[0] is main
    assert [r.request.cost_factor for r in sensitivity.runs] == [1.0, 1.5, 2.0]
    artifacts = cost_matrix_artifacts(sensitivity, created=CREATED)
    assert [(a.label, a.sample) for a in artifacts] == [
        ("master_portfolio-cost-all", "all"),
        ("master_portfolio-cost-oos", "oos"),
    ]
    for matrix in artifacts:
        assert isinstance(matrix, CostMatrixArtifact)
        assert matrix.factors == (1.0, 1.5, 2.0)
        assert [row.strategy_id for row in matrix.rows] == [*MASTER_LEGS, "master_portfolio"]
        for row, view in zip(matrix.rows, main.views, strict=True):
            stats = view.split.all if matrix.sample == "all" else view.split.out_of_sample
            cell = row.cells[0]
            assert cell.factor == 1.0
            assert (cell.n, cell.mean_r, cell.t_stat, cell.total_r) == (
                stats.n,
                stats.mean_r,
                stats.t_stat,
                stats.total_r,
            )
            assert cell.max_drawdown_r == stats.max_drawdown_r


def test_cost_matrix_of_a_tv_run_is_labelled_as_such() -> None:
    main = run(request(fill_model=FillModel.TV_OHLC))
    artifacts = cost_matrix_artifacts(run_cost_sensitivity(main, (1.0, 2.0)), created=CREATED)

    assert [a.label for a in artifacts] == ["ny_orb_fvg-tv-cost-all", "ny_orb_fvg-tv-cost-oos"]


def test_higher_costs_never_raise_any_entry_pnl_when_the_fills_are_unchanged() -> None:
    # Fixed stop/target levels on a futures contract without spread: slippage only worsens
    # the fill prices, the bars and levels that trigger stay the same. That is what is
    # guaranteed; R is NOT (the slipped entry price changes the risk it is divided by).
    main = scripted(oos_start_ms=START_MS)
    sensitivity = run_cost_sensitivity(
        main, (1.0, 1.5, 2.0, 3.0), registry={"scripted": scripted_spec()}
    )

    for cheaper, dearer in zip(sensitivity.runs, sensitivity.runs[1:], strict=False):
        assert len(cheaper.entries) == len(dearer.entries) > 3
        for low, high in zip(cheaper.entries, dearer.entries, strict=True):
            assert (low.entry_ts_ms, low.exit_ts_ms, low.reasons) == (
                high.entry_ts_ms,
                high.exit_ts_ms,
                high.reasons,
            )
            assert high.pnl <= low.pnl
        for low_view, high_view in zip(cheaper.views, dearer.views, strict=True):
            assert high_view.split.all.total_pnl <= low_view.split.all.total_pnl


def test_cost_matrix_row_of_a_strategy_without_trades_is_well_formed() -> None:
    main = scripted(overrides={"trade": False})
    sensitivity = run_cost_sensitivity(main, (1.0, 1.5), registry={"scripted": scripted_spec()})

    for matrix in cost_matrix_artifacts(sensitivity, created=CREATED):
        assert [row.strategy_id for row in matrix.rows] == ["leg_a", "leg_b", "scripted"]
        for row in matrix.rows:
            for cell in row.cells:
                assert (cell.n, cell.mean_r, cell.t_stat, cell.total_r) == (0, None, None, 0.0)
                assert cell.profit_factor is None
    for artifact in backtest_artifacts(main, created=CREATED):
        assert artifact.stats_all.n == 0


@pytest.mark.parametrize(
    "kwargs",
    [{"cost_factor": 1.5}, {"cost_free": True}],
)
def test_cost_matrix_refuses_a_main_run_that_is_not_at_the_base_costs(
    kwargs: dict[str, Any],
) -> None:
    with pytest.raises(RunnerError, match="maliyet matrisi"):
        run_cost_sensitivity(run(request(**kwargs)), (1.0, 1.5))


@pytest.mark.parametrize(
    ("factors", "message"),
    [((), "1,0 ile başlamalı"), ((1.5, 2.0), "1,0 ile başlamalı"), ((1.0, 2.0, 1.5), "artan")],
)
def test_cost_matrix_refuses_bad_factor_lists(factors: tuple[float, ...], message: str) -> None:
    with pytest.raises(RunnerError, match=message):
        run_cost_sensitivity(run(request()), factors)


def test_all_artifacts_refuses_a_cost_matrix_of_another_run() -> None:
    first = run(request())
    other = run_cost_sensitivity(run(request()), (1.0, 1.5))

    with pytest.raises(RunnerError, match="bu koşuya ait değil"):
        all_artifacts_of(first, created=CREATED, sensitivity=other)


# --- walk-forward -------------------------------------------------------------------------


def test_walk_forward_span_shorter_than_one_fold_is_explicit_not_an_error() -> None:
    result = run(request("master_portfolio"))
    legs = walk_forward_legs(result, DEFAULT_WF)

    assert [leg.strategy_id for leg in legs] == [*MASTER_LEGS, "master_portfolio"]
    for leg in legs:
        assert leg.result.folds == ()
        assert leg.note is not None
        assert "katı yok" in leg.note
        assert "240 gün" in leg.note
    artifacts = walk_forward_artifacts(result, legs, created=CREATED)
    for artifact in artifacts:
        assert isinstance(artifact, WalkForwardArtifact)
        assert artifact.summary.n_folds == 0
        assert any("katı yok" in note for note in artifact.notes)
    assert "katı yok" in render_summary(result, walk_forward_results=legs)


def test_walk_forward_with_folds_has_no_note_and_uses_the_series_span() -> None:
    result = run(request())
    (leg,) = walk_forward_legs(result, SHORT_WF)

    assert leg.result.folds
    assert leg.note is None
    assert leg.label == "ny_orb_fvg"
    assert leg.result.span_first_day == dt.date(2026, 10, 18)
    assert leg.result.span_last_day == dt.date(2026, 11, 13)


def test_walk_forward_without_trades_says_so() -> None:
    result = scripted(overrides={"trade": False})

    for leg in walk_forward_legs(result, SHORT_WF):
        assert leg.note is not None
        assert "tamamlanmış işlem yok" in leg.note


# --- trade list ---------------------------------------------------------------------------


def test_trade_list_round_trips_with_its_provenance(tmp_path: Path) -> None:
    result = run(request("master_portfolio"))
    merged = merge_runs([result], created=CREATED)
    path = tmp_path / "trades.csv"

    rows = write_trade_list(merged, path)
    trades, provenance = read_trades_csv(path)
    assert rows == len(trades) == sum(1 for e in result.entries if e.complete)
    assert provenance == merged.provenance
    assert provenance is not None
    assert provenance.synthetic is True
    assert provenance.cost_free is False
    assert provenance.fill_model == "pessimistic"
    assert provenance.cost_factor == 1.0
    assert provenance.data_sha256 == fixture("synthetic_5m.csv").source.sha256
    assert provenance.data_file == "synthetic_5m.csv"
    assert provenance.strategy_ids == tuple(sorted(MASTER_LEGS))
    assert provenance.created_utc == "2026-11-18T12:00:00Z"
    assert {trade.strategy_id for trade in trades} == set(MASTER_LEGS)


def test_merge_of_two_strategies_on_the_same_data_orders_entries_by_time(tmp_path: Path) -> None:
    a = run(request("ny_orb_fvg"))
    b = run(request("asian_sweep"))
    merged = merge_runs([a, b], created=CREATED)

    assert merged.provenance.strategy_ids == ("asian_sweep", "ny_orb_fvg")
    assert len(merged.entries) == len(a.entries) + len(b.entries)
    assert [e.seq for e in merged.entries] == list(range(len(merged.entries)))
    times = [e.entry_ts_ms for e in merged.entries]
    assert times == sorted(times)
    path = tmp_path / "portfolio.csv"
    write_trade_list(merged, path)
    trades, provenance = read_trades_csv(path)
    assert provenance == merged.provenance
    assert {t.strategy_id for t in trades} == {"asian_sweep", "ny_orb_fvg"}


@pytest.mark.parametrize(
    ("other", "message"),
    [
        ({"strategy_id": "asian_sweep", "series": "synthetic_1m.csv"}, "sha256"),
        ({"strategy_id": "asian_sweep", "fill_model": FillModel.TV_OHLC}, "dolum"),
        ({"strategy_id": "asian_sweep", "cost_factor": 1.5}, "çarpan"),
        ({"strategy_id": "asian_sweep", "cost_free": True}, "çarpan"),
        ({"strategy_id": "ny_orb_fvg"}, "iki kez"),
    ],
)
def test_merge_refuses_runs_that_do_not_share_data_fill_model_and_factor(
    other: dict[str, Any], message: str
) -> None:
    base = run(request("ny_orb_fvg"))
    kwargs = dict(other)
    strategy_id = kwargs.pop("strategy_id")
    if "series" in kwargs:
        kwargs["series"] = fixture(kwargs["series"])
    with pytest.raises(RunnerError, match=message):
        merge_runs([base, run(request(strategy_id, **kwargs))], created=CREATED)


def test_merge_refuses_different_in_memory_series_and_an_empty_list() -> None:
    first = scripted(wave(120))
    second = run(
        request("other", series=wave(80)),
        registry={"other": scripted_spec(("leg_c",), "other")},
    )

    with pytest.raises(RunnerError, match="bellekte"):
        merge_runs([first, second], created=CREATED)
    with pytest.raises(RunnerError, match="koşu yok"):
        merge_runs([], created=CREATED)


def test_trade_list_of_a_run_without_trades_is_refused_in_turkish(tmp_path: Path) -> None:
    merged = merge_runs([scripted(overrides={"trade": False})], created=CREATED)

    with pytest.raises(RunnerError, match="tamamlanmış işlem yok"):
        write_trade_list(merged, tmp_path / "empty.csv")
    assert not (tmp_path / "empty.csv").exists()


# --- refusals -----------------------------------------------------------------------------


def test_unknown_strategy_id_is_refused_in_turkish() -> None:
    with pytest.raises(RunnerError, match="bilinmeyen strateji: nope"):
        run(request("nope"))


def test_bad_overrides_are_refused_in_turkish() -> None:
    with pytest.raises(RunnerError, match="geçersiz parametre"):
        run(request(overrides={"no_such_param": 1}))
    with pytest.raises(RunnerError, match="geçersiz parametre"):
        run(request(overrides={"or_minutes": "five"}))


def test_trade_record_with_an_undeclared_leg_id_is_refused() -> None:
    with pytest.raises(RunnerError, match="tanımsız bacak kimliği: leg_x"):
        scripted(overrides={"leak_leg": "leg_x"})


def test_engine_error_surfaces_as_a_strategy_defect_with_id_bar_and_original_text() -> None:
    with pytest.raises(StrategyDefectError) as caught:
        scripted(overrides={"no_stop": True})

    message = str(caught.value)
    assert message.startswith("scripted: strateji portu emir sözleşmesini bozdu")
    assert "en son" in message
    assert "bar" in message
    assert str(caught.value.__cause__) in message


def test_empty_series_is_refused() -> None:
    empty = BarSeries(
        symbol="X",
        tf_seconds=60,
        bars=(),
        source=DataSource(name="e", format="generic", sha256="", synthetic=True, bar_tz="UTC"),
    )
    with pytest.raises(RunnerError, match="boş"):
        run(request(series=empty))


@pytest.mark.parametrize(
    ("changes", "message"),
    [
        ({"initial_capital": 0.0}, "sermaye"),
        ({"initial_capital": math.inf}, "sermaye"),
        ({"cost_factor": -1.0}, "çarpanı"),
        ({"cost_factor": math.nan}, "çarpanı"),
        ({"cost_free": True, "cost_factor": 1.5}, "maliyetsiz"),
        ({"min_gap_seconds": 0}, "boşluğu"),
    ],
)
def test_invalid_requests_are_refused_in_turkish(changes: dict[str, Any], message: str) -> None:
    import dataclasses

    bad = dataclasses.replace(request(), **changes)
    with pytest.raises(RunnerError, match=message):
        run(bad)


# --- summary ------------------------------------------------------------------------------


def test_summary_of_a_decision_run_states_source_costs_and_counts() -> None:
    result = run(request("master_portfolio"))
    sensitivity = run_cost_sensitivity(result, (1.0, 1.5))
    text = render_summary(result, sensitivity=sensitivity)

    first_line = text.splitlines()[0]
    assert SYNTHETIC_TAG in first_line
    assert "KARAR KOŞUSU DEĞİL" not in text
    assert COST_FREE_TAG not in text
    for expected in (
        "synthetic_5m.csv",
        fixture("synthetic_5m.csv").source.sha256,
        "5.520 bar",
        "bar saat dilimi UTC",
        "sentetik: EVET",
        "Enstrüman: MES (futures)",
        "komisyon 0,50/birim/yön",
        "Dolum modeli: pessimistic",
        "veri bittiğinde açık",
        "düşürülen giriş",
        "master_portfolio — portföy toplamı",
        "Maliyet duyarlılığı",
    ):
        assert expected in text, expected
    for leg in MASTER_LEGS:
        assert f"{leg} ({leg})" in text
