"""Backtest runner: "a bar series + a strategy id" -> results, a summary and report artifacts.

Pure library code for the `propbot backtest` command (SPEC §6, ADR-0008): nothing here
reads a clock, a global path or the environment, and nothing is written except by
`write_artifacts` and `write_trade_list`.

Statistics, artifacts and Go/No-Go gates are kept per LEG: a single-strategy port has one
leg (its own id), the Master portfolio has two and additionally gets a combined view
under its own id. Analytics does the counting (`analytics.split_is_oos`, `time_tables`,
`equity_curve`); this module only routes entries to legs and builds the artifacts.

Label policy (the Go/No-Go report relies on it): the decision run (pessimistic fills,
costs on, cost factor exactly 1.0, the configured OOS start, sized at the planned risk)
is labelled with the leg id alone; every other run carries a suffix naming what differs
("-tv", "-free", "-x1p5", "-oos20261020", "-pinerisk", combined in that order), so a
comparison run can neither overwrite nor pass for a decision run. The OOS suffix needs
the configured start (`RunRequest.configured_oos_start_ms`); without it the runner cannot
know, and the Go/No-Go report's own OOS check is the gate.

Risk (user decision of 2026-11-19, open finding 82): the account is planned at
config/risk.yaml's `risk_per_trade_pct`, the Pine scripts default to 0.5 %. R is not
size-free: sizes are floored to the instrument's step, a size below the minimum skips
the trade, and the scripts' own percent breakers trip after another number of losses.
A trade list made at the script's risk is therefore not the list the account would
have, and a decision run writes the planned risk into the port's risk input
(`RunRequest.planned_risk_pct`). Without it the run keeps the script's own risk and is
a comparison run ("-pinerisk"); that is also what a parity run is, on purpose.
The planned risk is a percent of the account's initial balance and never above the code
ceiling (SPEC §8.1): a run that is given it refuses another risk base and a larger value.
The trade list of a run at the script's own risk carries the mark in its rows' strategy
ids ("ny_orb_fvg-pinerisk"), because its rows can equal the decision run's (R is per
unit; only a floored-away or skipped trade changes a row) and nothing else in its
provenance line would tell the two lists apart.

ADR-0007: a run on a synthetic series says "SENTETİK VERİ" in its summary, its artifacts
(the artifact models enforce it) and its trade list's provenance line.
"""

import contextlib
import dataclasses
import datetime as dt
import itertools
import math
from collections.abc import Iterator, Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path
from types import MappingProxyType
from typing import Any, Final

from pydantic import ValidationError

from propbot.backtest.analytics import (
    AnalyticsError,
    EquityPoint,
    SampleSplit,
    TimeTables,
    TradeStats,
    equity_curve,
    render_stats_text,
    split_is_oos,
    time_tables,
    write_trades_csv,
)
from propbot.backtest.artifacts import (
    COMPARISON_RUN_TAG,
    COST_FREE_NOTE,
    PINE_RISK_SUFFIX,
    SYNTHETIC_NOTE,
    AnyArtifact,
    ArtifactError,
    BacktestArtifact,
    CostMatrixArtifact,
    CostRow,
    Scalar,
    WalkForwardArtifact,
    backtest_artifact,
    build_artifact,
    cost_cell,
    ms_stamp,
    utc_stamp,
    walk_forward_artifact,
    write_artifact,
)
from propbot.backtest.engine import EngineError, run_backtest
from propbot.backtest.model import (
    CostModel,
    EngineConfig,
    EngineResult,
    EntryResult,
    FillModel,
    group_by_entry,
)
from propbot.backtest.report import risk_shown, tr_int, tr_number, tr_plain
from propbot.backtest.trades import TradesProvenance
from propbot.backtest.walkforward import WalkForwardConfig, WalkForwardResult, walk_forward
from propbot.core.bars import Bar, BarSeries
from propbot.core.instrument import Instrument
from propbot.core.session import NEW_YORK
from propbot.data.sessions import DEFAULT_MIN_GAP_SECONDS, session_last_flags
from propbot.risk.config import MAX_RISK_PER_TRADE_PCT
from propbot.risk.errors import issues_from_validation_error
from propbot.strategies.base import Action, FillEvent, Strategy, StrategyContext
from propbot.strategies.registry import STRATEGIES, StrategyError, StrategySpec

DECISION_FACTOR: Final = 1.0
TV_SUFFIX: Final = "-tv"
FREE_SUFFIX: Final = "-free"
OOS_SUFFIX: Final = "-oos"
COST_MATRIX_SAMPLES: Final = ("all", "oos")
_EMPTY: Final[Mapping[str, object]] = MappingProxyType({})
_MAX_STAMP_MS: Final = 253_402_300_800_000  # 10000-01-01T00:00:00Z


class RunnerError(Exception):
    """A run cannot be made or reported. The message is user-facing (Turkish)."""


class StrategyDefectError(RunnerError):
    """The engine refused what a strategy port did: a bug in the port, never in the data."""


@dataclass(frozen=True, slots=True)
class RunRequest:
    """One backtest: everything comes from the caller (config files, venue profile, CLI)."""

    series: BarSeries
    strategy_id: str
    instrument: Instrument
    base_costs: CostModel  # config/backtest.yaml costs of the instrument (factor 1.0)
    initial_capital: float  # the venue profile's initial_balance; never a default here
    oos_start_ms: int
    overrides: Mapping[str, object] = _EMPTY  # config/strategies.yaml for this strategy
    min_gap_seconds: int = DEFAULT_MIN_GAP_SECONDS
    fill_model: FillModel = FillModel.PESSIMISTIC
    cost_factor: float = DECISION_FACTOR
    cost_free: bool = False
    # config/backtest.yaml's samples.oos_start (epoch ms). When `oos_start_ms` differs the
    # run is a what-if on another IS/OOS boundary (cross-cutting review A, 2026-11-19):
    # labelled and noted as a comparison run. None = the caller did not say.
    configured_oos_start_ms: int | None = None
    # config/risk.yaml's risk_per_trade_pct, in percent: the per-trade risk the account is
    # planned at. Written into the port's risk input for this run (module docstring).
    # None = the script's own risk, the Pine default: a comparison run, never a decision
    # run, so a caller that forgets it cannot produce Go/No-Go evidence by accident. (The
    # runner cannot know the planned risk when it is not given: a caller whose planned
    # risk EQUALS the script's passes it, as `propbot backtest --pine-risk` does.)
    planned_risk_pct: float | None = None

    @property
    def pine_risk(self) -> bool:
        """True when the run keeps the script's own per-trade risk (no planned risk)."""
        return self.planned_risk_pct is None

    @property
    def other_oos_start(self) -> bool:
        """True when the run's OOS start is known to differ from the configured one."""
        return (
            self.configured_oos_start_ms is not None
            and self.oos_start_ms != self.configured_oos_start_ms
        )

    @property
    def effective_factor(self) -> float:
        """The factor the base costs are scaled by: 0.0 for a cost-free run."""
        return 0.0 if self.cost_free else self.cost_factor

    def with_factor(self, factor: float) -> "RunRequest":
        return dataclasses.replace(self, cost_factor=factor)


@dataclass(frozen=True, slots=True)
class LegResult:
    """The entries of one leg (or the portfolio's combined view) and their statistics."""

    strategy_id: str  # the leg id; for the combined view the portfolio's id
    label: str
    entries: tuple[EntryResult, ...]  # in entry order
    split: SampleSplit
    tables: TimeTables
    curve: tuple[EquityPoint, ...]
    combined: bool = False


@dataclass(frozen=True, slots=True)
class RunResult:
    request: RunRequest
    spec: StrategySpec[Any]
    config: EngineConfig  # the costs actually used (base x effective factor)
    params: Mapping[str, Scalar]  # the effective parameters as JSON scalars
    engine: EngineResult
    entries: tuple[EntryResult, ...]  # every entry of the run, in entry order
    legs: tuple[LegResult, ...]  # in the spec's leg order
    combined: LegResult | None  # the portfolio view; None for a single-leg port
    label_suffix: str
    not_decision_reasons: tuple[str, ...]  # Turkish; empty for a decision run

    @property
    def decision(self) -> bool:
        return not self.not_decision_reasons

    @property
    def synthetic(self) -> bool:
        return self.request.series.source.synthetic

    @property
    def cost_free(self) -> bool:
        return self.config.costs.is_free

    @property
    def views(self) -> tuple[LegResult, ...]:
        """Legs first, then the combined view when there is one."""
        return self.legs if self.combined is None else (*self.legs, self.combined)


@dataclass(frozen=True, slots=True)
class CostSensitivity:
    """The same request at every factor; `runs[0]` IS the main run (factor 1.0)."""

    factors: tuple[float, ...]
    runs: tuple[RunResult, ...]


@dataclass(frozen=True, slots=True)
class LegWalkForward:
    strategy_id: str
    label: str
    result: WalkForwardResult
    note: str | None  # Turkish; set when no fold fits (not an error)


@dataclass(frozen=True, slots=True)
class MergedTrades:
    """Entries of one or more runs on the same data, with one provenance line."""

    entries: tuple[EntryResult, ...]  # renumbered by entry time
    provenance: TradesProvenance


# --- labels ------------------------------------------------------------------------------


def label_suffix(
    *,
    fill_model: FillModel,
    cost_free: bool,
    cost_factor: float,
    other_oos_start_ms: int | None = None,
    pine_risk: bool = False,
) -> str:
    """ "" for the decision run; otherwise names every way the run differs from it.

    `other_oos_start_ms`: the run's OOS start when it differs from the configured one.
    `pine_risk`: the run kept the script's own per-trade risk instead of the planned one.
    """
    suffix = TV_SUFFIX if fill_model is FillModel.TV_OHLC else ""
    if cost_free:
        # The factor of a run without costs changes nothing: one suffix covers it.
        suffix += FREE_SUFFIX
    elif cost_factor != DECISION_FACTOR:
        suffix += "-x" + _factor_text(cost_factor)
    if other_oos_start_ms is not None:
        suffix += OOS_SUFFIX + _oos_text(other_oos_start_ms)
    if pine_risk:
        suffix += PINE_RISK_SUFFIX
    return suffix


def run_label(
    leg_id: str,
    *,
    fill_model: FillModel,
    cost_free: bool,
    cost_factor: float,
    other_oos_start_ms: int | None = None,
    pine_risk: bool = False,
) -> str:
    return leg_id + label_suffix(
        fill_model=fill_model,
        cost_free=cost_free,
        cost_factor=cost_factor,
        other_oos_start_ms=other_oos_start_ms,
        pine_risk=pine_risk,
    )


def _oos_text(oos_start_ms: int) -> str:
    # "20261020" for a UTC midnight (what --oos-start writes), else down to the second, so
    # two different boundaries never share a label.
    moment = dt.datetime.fromtimestamp(oos_start_ms / 1000, tz=dt.UTC)
    text = moment.strftime("%Y%m%d")
    if moment.time() != dt.time():
        text += "t" + moment.strftime("%H%M%S")
    return text


def _factor_text(factor: float) -> str:
    # 1.5 -> "1p5", 2.0 -> "2"; the label pattern allows only [a-z0-9_-]. The shortest
    # round-trip repr, not a rounded format: 1.0000000001 must not read "x1" (the decision
    # factor) and 1.4999999999 must not share the label of 1.5.
    text = repr(float(factor))
    text = text.removesuffix(".0")
    return text.replace("+", "").replace(".", "p")


def factor_shown(factor: float) -> str:
    """A cost factor for people, exact (shortest round-trip): 1.5 -> "1,5", 1.0 -> "1,0".

    `tr_plain` rounds to four decimals, so a factor of 1.00001 would read "1" next to a
    "not a decision run" warning; this never rounds.
    """
    return repr(float(factor)).replace(".", ",")


# --- running -----------------------------------------------------------------------------


def run(
    request: RunRequest, *, registry: Mapping[str, StrategySpec[Any]] = STRATEGIES
) -> RunResult:
    """Run the request once on a FRESH strategy instance and split the entries by leg."""
    spec = _spec(request.strategy_id, registry)
    _check_request(request)
    values = run_values(spec, request.overrides, request.planned_risk_pct)
    try:
        strategy = spec.build(values)
    except StrategyError as exc:
        raise RunnerError(str(exc)) from exc
    config = engine_config(request)
    series = request.series
    flags = session_last_flags(
        series.bars, series.tf_seconds, min_gap_seconds=request.min_gap_seconds
    )
    tracked = _Tracked(strategy)
    try:
        engine = run_backtest(series, tracked, config, session_last=flags)
    except EngineError as exc:
        seen = (
            "hiç bar işlenmeden"
            if tracked.last_bar_index is None
            else f"strateji en son {tracked.last_bar_index}. barı işledi"
        )
        raise StrategyDefectError(
            f"{spec.strategy_id}: strateji portu emir sözleşmesini bozdu ({seen}); bu bir "
            f"yazılım hatasıdır, veriyle ilgili değil. Motor: {exc}"
        ) from exc

    entries = group_by_entry(engine.trades)
    strays = sorted({entry.strategy_id for entry in entries} - set(spec.leg_ids))
    if strays:
        raise RunnerError(
            f"{spec.strategy_id}: işlem kaydında tanımsız bacak kimliği: {', '.join(strays)} "
            f"(beklenen: {', '.join(spec.leg_ids)})"
        )

    suffix = label_suffix(
        fill_model=request.fill_model,
        cost_free=config.costs.is_free,
        cost_factor=request.effective_factor,
        other_oos_start_ms=request.oos_start_ms if request.other_oos_start else None,
        pine_risk=request.pine_risk,
    )
    legs = tuple(
        _leg(
            leg_id,
            leg_id + suffix,
            tuple(entry for entry in entries if entry.strategy_id == leg_id),
            request,
        )
        for leg_id in spec.leg_ids
    )
    combined = None
    if len(spec.leg_ids) > 1:
        combined = _leg(spec.strategy_id, spec.strategy_id + suffix, entries, request, True)
    return RunResult(
        request=request,
        spec=spec,
        config=config,
        params=_dumped(spec, values),
        engine=engine,
        entries=entries,
        legs=legs,
        combined=combined,
        label_suffix=suffix,
        not_decision_reasons=_not_decision_reasons(request, config.costs, spec),
    )


def engine_config(request: RunRequest) -> EngineConfig:
    """The engine configuration of a request (costs scaled by the effective factor)."""
    _check_request(request)
    return EngineConfig(
        instrument=request.instrument,
        costs=request.base_costs.scaled(request.effective_factor),
        fill_model=request.fill_model,
        initial_capital=request.initial_capital,
    )


def run_values(
    spec: StrategySpec[Any],
    overrides: Mapping[str, object] | None,
    planned_risk_pct: float | None,
) -> Mapping[str, object]:
    """The parameter values a run is built from: the overrides plus the planned risk.

    Checked here, so a refusal is a Turkish sentence: the overrides alone first (their
    mistakes are named as theirs), then with the planned risk in the port's risk input.
    A planned risk the port refuses is refused, never moved to the nearest allowed value:
    a run at another risk than the account's would be evidence for another account. For
    the same reason a planned risk above the code ceiling is refused, and so is a planned
    risk on another risk base than the account's initial balance (the registry says so).
    """
    _check_planned_risk(planned_risk_pct)
    try:
        spec.with_planned_risk(overrides, None)  # the risk input is never an override
        spec.params(overrides)
        values = spec.with_planned_risk(overrides, planned_risk_pct)
    except StrategyError as exc:
        raise RunnerError(str(exc)) from exc
    if planned_risk_pct is not None:
        try:
            spec.params_model.model_validate(values)
        except ValidationError as exc:
            why = "; ".join(issue.message for issue in issues_from_validation_error(exc, ""))
            raise RunnerError(
                f"{spec.strategy_id}: planlanan işlem başı risk {risk_shown(planned_risk_pct)} "
                f"(config/risk.yaml risk_per_trade_pct) betiğin {spec.risk_param} girdisine "
                f"yazılamıyor: {why}. Değer sessizce değiştirilmez; betiğin kendi riskiyle "
                "karşılaştırma koşusu için --pine-risk"
            ) from exc
    return MappingProxyType(values)


def effective_params(
    spec: StrategySpec[Any],
    overrides: Mapping[str, object] | None = None,
    *,
    planned_risk_pct: float | None = None,
) -> Mapping[str, Scalar]:
    """The parameters a run records, as JSON scalars (datetimes as ISO strings).

    With `planned_risk_pct` these are what a decision run records (the Go/No-Go command
    builds its expectation from this very function); without it, what a run at the
    script's own risk records (a parity run, a "-pinerisk" comparison run).
    """
    return _dumped(spec, run_values(spec, overrides, planned_risk_pct))


def _dumped(spec: StrategySpec[Any], values: Mapping[str, object]) -> Mapping[str, Scalar]:
    try:
        dumped = spec.params(values).model_dump(mode="json")
    except StrategyError as exc:
        raise RunnerError(str(exc)) from exc
    result: dict[str, Scalar] = {}
    for name, value in dumped.items():
        if value is not None and not isinstance(value, str | int | float | bool):
            raise RunnerError(f"{spec.strategy_id}: parametre {name} tek bir değer değil")
        result[name] = value
    return MappingProxyType(result)


def run_cost_sensitivity(
    main: RunResult,
    factors: Sequence[float],
    *,
    registry: Mapping[str, StrategySpec[Any]] = STRATEGIES,
) -> CostSensitivity:
    """`main` again at every other factor; the factor-1.0 run is `main` itself (not re-run)."""
    request = main.request
    # `main.cost_free`, not the switch: zero base costs scale to zero at every factor, and
    # a matrix of identical cells would read as "costs do not matter".
    if main.cost_free or request.cost_free or request.cost_factor != DECISION_FACTOR:
        raise RunnerError("maliyet matrisi yalnızca maliyetli ve çarpanı 1,0 olan koşudan kurulur")
    ordered = tuple(factors)
    if not ordered or ordered[0] != DECISION_FACTOR:
        raise RunnerError("maliyet çarpanları 1,0 ile başlamalı (temel maliyet)")
    if any(later <= earlier for earlier, later in itertools.pairwise(ordered)):
        raise RunnerError("maliyet çarpanları kesin artan sırada olmalı")
    if any(not math.isfinite(factor) for factor in ordered):
        raise RunnerError("maliyet çarpanları sonlu sayılar olmalı")
    runs = [main]
    runs.extend(run(request.with_factor(factor), registry=registry) for factor in ordered[1:])
    return CostSensitivity(factors=ordered, runs=tuple(runs))


def walk_forward_legs(result: RunResult, config: WalkForwardConfig) -> tuple[LegWalkForward, ...]:
    """Walk-forward of every view over the span of the series (not of the trades)."""
    series = result.request.series
    first, last = series.first_ts_ms, series.last_ts_ms
    # run() refused an empty series.
    assert first is not None
    assert last is not None
    output: list[LegWalkForward] = []
    for view in result.views:
        wf = walk_forward(
            view.entries,
            config,
            first_bar_ms=first,
            last_bar_ms=last,
            initial_capital=result.config.initial_capital,
            tz=NEW_YORK,
        )
        output.append(LegWalkForward(view.strategy_id, view.label, wf, _no_fold_note(wf, view)))
    return tuple(output)


def _no_fold_note(wf: WalkForwardResult, view: LegResult) -> str | None:
    if wf.folds:
        return None
    if not any(entry.complete for entry in view.entries):
        return "Walk-forward katı yok: tamamlanmış işlem yok (pencereler ilk işlemden başlar)."
    span_days = (wf.span_last_day - wf.span_first_day).days + 1
    need = wf.config.is_days + wf.config.oos_days
    return (
        f"Walk-forward katı yok: veri aralığı {span_days} gün; bir kat için ilk işlemden "
        f"sonra IS {wf.config.is_days} + OOS {wf.config.oos_days} = {need} gün gerekir. "
        "Bu bir hata değildir; daha uzun veriyle yeniden koşun."
    )


# --- artifacts ---------------------------------------------------------------------------


def backtest_artifacts(result: RunResult, *, created: dt.datetime) -> tuple[BacktestArtifact, ...]:
    """One artifact per leg, plus the combined view of a portfolio."""
    request = result.request
    notes = _run_notes(result)
    artifacts: list[BacktestArtifact] = []
    for view in result.views:
        view_notes = list(notes)
        if len(result.legs) > 1:
            view_notes.append(_portfolio_note(result, view))
        with _as_runner_error():
            artifacts.append(
                backtest_artifact(
                    label=view.label,
                    created=created,
                    strategy_id=view.strategy_id,
                    params=result.params,
                    config=result.config,
                    entries=view.entries,
                    oos_start_ms=request.oos_start_ms,
                    dropped_entries=result.engine.dropped_entries,
                    data=request.series,
                    synthetic=result.synthetic,
                    notes=view_notes,
                    cost_factor=request.effective_factor,
                    # Where sessions end decides session.islastbar for the strategies:
                    # recorded so Go/No-Go can compare it with config (gate attack L).
                    min_gap_seconds=request.min_gap_seconds,
                )
            )
    return tuple(artifacts)


def cost_matrix_artifacts(
    sensitivity: CostSensitivity, *, created: dt.datetime
) -> tuple[CostMatrixArtifact, ...]:
    """Two artifacts: the cells over all complete entries, and over the OOS entries only."""
    main = sensitivity.runs[0]
    # The matrix holds every factor, so its label names no factor; the rest is the main
    # run's suffix, composed where every other label is.
    suffix = label_suffix(
        fill_model=main.request.fill_model,
        cost_free=False,
        cost_factor=DECISION_FACTOR,
        other_oos_start_ms=main.request.oos_start_ms if main.request.other_oos_start else None,
        pine_risk=main.request.pine_risk,
    )
    artifacts: list[CostMatrixArtifact] = []
    for sample in COST_MATRIX_SAMPLES:
        rows = tuple(
            CostRow(
                strategy_id=view.strategy_id,
                cells=tuple(
                    cost_cell(factor, _sample_stats(run_.views[index].split, sample))
                    for factor, run_ in zip(sensitivity.factors, sensitivity.runs, strict=True)
                ),
            )
            for index, view in enumerate(main.views)
        )
        notes = [
            _sample_note(sample, main.request.oos_start_ms),
            "Her çarpanda aynı veri, aynı parametreler ve aynı dolum modeli "
            f"({main.request.fill_model.value}); yalnızca tüm maliyetler çarpanla çarpılır. "
            "Daha yüksek maliyet dolumları da değiştirebilir (kayma, spread, kötümser yürüyüş).",
            *_strategy_notes(main),
        ]
        with _as_runner_error():
            artifacts.append(
                build_artifact(
                    CostMatrixArtifact,
                    label=f"{main.spec.strategy_id}{suffix}-cost-{sample}",
                    created=created,
                    synthetic=main.synthetic,
                    data=main.request.series,
                    notes=notes,
                    sample=sample,
                    factors=sensitivity.factors,
                    rows=rows,
                )
            )
    return tuple(artifacts)


def walk_forward_artifacts(
    result: RunResult, legs: Sequence[LegWalkForward], *, created: dt.datetime
) -> tuple[WalkForwardArtifact, ...]:
    base_notes = [*_strategy_notes(result), *_comparison_notes(result)]
    artifacts: list[WalkForwardArtifact] = []
    for leg in legs:
        notes = [*base_notes, *([leg.note] if leg.note else [])]
        with _as_runner_error():
            artifacts.append(
                walk_forward_artifact(
                    leg.result,
                    label=leg.label,
                    created=created,
                    strategy_id=leg.strategy_id,
                    data=result.request.series,
                    synthetic=result.synthetic,
                    notes=notes,
                )
            )
    return tuple(artifacts)


def all_artifacts_of(
    result: RunResult,
    *,
    created: dt.datetime,
    sensitivity: CostSensitivity | None = None,
    walk_forward_results: Sequence[LegWalkForward] = (),
) -> tuple[AnyArtifact, ...]:
    """Every artifact of a run, in a fixed order (backtests, cost matrix, walk-forward)."""
    if sensitivity is not None and sensitivity.runs[0] is not result:
        raise RunnerError("maliyet matrisi bu koşuya ait değil")
    artifacts: list[AnyArtifact] = list(backtest_artifacts(result, created=created))
    if sensitivity is not None:
        artifacts.extend(cost_matrix_artifacts(sensitivity, created=created))
    artifacts.extend(walk_forward_artifacts(result, walk_forward_results, created=created))
    return tuple(artifacts)


def write_artifacts(artifacts: Sequence[AnyArtifact], reports_dir: Path) -> tuple[Path, ...]:
    """Write each artifact (never overwriting); the paths in the same order."""
    return tuple(write_artifact(artifact, reports_dir) for artifact in artifacts)


# --- trade list --------------------------------------------------------------------------


def merge_runs(runs: Sequence[RunResult], *, created: dt.datetime) -> MergedTrades:
    """The entries of several runs ON THE SAME DATA as one trade list with one provenance.

    Different data (file hash, or bars for a series built in memory), instrument, costs,
    fill model or cost factor are refused: a portfolio simulation over runs that do not
    share them would mix experiments. A strategy may appear once.

    The rows of runs at the script's own risk carry the marked ids (`trade_list_id`).
    """
    if not runs:
        raise RunnerError("birleştirilecek koşu yok")
    first = runs[0]
    seen: set[str] = set()
    for item in runs:
        if item.spec.strategy_id in seen:
            raise RunnerError(f"{item.spec.strategy_id}: aynı strateji iki kez birleştirilemez")
        seen.add(item.spec.strategy_id)
        problem = _mismatch(first, item)
        if problem is not None:
            raise RunnerError(
                f"koşular birleştirilemez ({first.spec.strategy_id} ↔ "
                f"{item.spec.strategy_id}): {problem}"
            )
    ordered = sorted(
        (entry for item in runs for entry in item.entries),
        key=lambda e: (e.entry_ts_ms, e.exit_ts_ms, e.strategy_id, e.seq),
    )
    # `_mismatch` refused a mix of risks: the runs are all at the script's own, or none is.
    pine_risk = first.request.pine_risk
    # Renumbered so the list's order is the entry order across runs (to_mc_trades sorts by seq).
    entries = tuple(
        dataclasses.replace(
            entry, seq=index, strategy_id=trade_list_id(entry.strategy_id, pine_risk=pine_risk)
        )
        for index, entry in enumerate(ordered)
    )
    source = first.request.series.source
    try:
        provenance = TradesProvenance(
            synthetic=any(item.synthetic for item in runs),
            cost_free=first.cost_free,
            fill_model=first.request.fill_model.value,
            cost_factor=float(first.request.effective_factor),
            data_sha256=source.sha256,
            data_file=source.name,
            strategy_ids=tuple(
                sorted(
                    {
                        trade_list_id(sid, pine_risk=pine_risk)
                        for item in runs
                        for sid in item.spec.leg_ids
                    }
                )
            ),
            created_utc=utc_stamp(created),
        )
    except ValueError as exc:
        raise RunnerError(f"işlem listesi kökeni oluşturulamadı: {exc}") from exc
    return MergedTrades(entries=entries, provenance=provenance)


def trade_list_id(leg_id: str, *, pine_risk: bool) -> str:
    """The strategy id a trade-list row carries: the leg id, marked for the script's risk.

    Go/No-Go G4 ties a list to the decision runs by the ids and digests of its rows, and
    refuses a marked id outright. The mark is in the rows (and so in the provenance
    line's `strategy_ids`) because the line has no field for the risk and the rows of
    the two lists can be equal: without it a list written by `--pine-risk` would pass
    for the planned account's whenever no trade happened to be floored away.
    """
    return leg_id + PINE_RISK_SUFFIX if pine_risk else leg_id


def write_trade_list(merged: MergedTrades, path: Path) -> int:
    """The Monte Carlo trade list with its "#propbot-trades" line; returns the row count."""
    try:
        return write_trades_csv(merged.entries, path, provenance=merged.provenance)
    except AnalyticsError as exc:
        raise RunnerError(str(exc)) from exc


def _mismatch(a: RunResult, b: RunResult) -> str | None:
    sa, sb = a.request.series, b.request.series
    if sa.source.sha256 != sb.source.sha256:
        return "veri dosyaları farklı (sha256)"
    if not sa.source.sha256 and sa != sb:
        return "bellekte kurulmuş seriler farklı"
    if (sa.symbol, sa.tf_seconds, sa.source.bar_tz) != (sb.symbol, sb.tf_seconds, sb.source.bar_tz):
        return "sembol, bar süresi ya da bar saat dilimi farklı"
    if sa is not sb and sa.bars != sb.bars:
        # Same file, other bars (a slice, a resample): the hash names the file, not the bars.
        return "aynı dosyadan farklı barlar (kesit ya da yeniden örnekleme)"
    if a.request.min_gap_seconds != b.request.min_gap_seconds:
        # The session-end gap decides the forced end-of-session exits: another experiment.
        return "seans sonu boşlukları farklı"
    if a.request.initial_capital != b.request.initial_capital:
        return "başlangıç sermayeleri farklı"
    if a.request.planned_risk_pct != b.request.planned_risk_pct:
        # One list is one account: rows sized at two risks would mix two experiments.
        return "işlem başı riskleri farklı (planlanan risk ya da betiğin kendi riski)"
    if a.request.fill_model is not b.request.fill_model:
        return "dolum modelleri farklı"
    if a.request.effective_factor != b.request.effective_factor:
        return "maliyet çarpanları farklı"
    if a.config.instrument != b.config.instrument or a.config.costs != b.config.costs:
        return "enstrüman ya da maliyetler farklı"
    return None


# --- summary -----------------------------------------------------------------------------


def render_summary(
    result: RunResult,
    *,
    sensitivity: CostSensitivity | None = None,
    walk_forward_results: Sequence[LegWalkForward] = (),
) -> str:
    """Turkish text summary; the warnings come first and loud."""
    request = result.request
    series = request.series
    source = series.source
    costs = result.config.costs
    lines: list[str] = []
    if result.synthetic:
        lines.append(f"!!! {SYNTHETIC_NOTE}")
    if result.cost_free:
        lines.append(f"!!! {COST_FREE_NOTE}")
    if not result.decision:
        lines.append(
            "! KARAR KOŞUSU DEĞİL (Go/No-Go bu koşuyu kullanmaz): "
            + "; ".join(result.not_decision_reasons)
        )
    if request.overrides:
        # Overrides come from config/strategies.yaml (the strategy as configured), so the
        # run may still decide; but it is not the Pine script's default strategy, and
        # parity holds only if TradingView is set the same way. Said first, not buried.
        lines.append(
            "! PARAMETRELER PINE VARSAYILANLARINDAN FARKLI (config/strategies.yaml): "
            + _overrides_text(result)
            + "; parite için TradingView'da da aynısı ayarlanmalı"
        )
    oos_warning = _oos_outside_note(request)
    if oos_warning is not None:
        lines.append(f"! {oos_warning}")
    if lines:
        lines.append("")

    span = (
        f"{ms_stamp(series.first_ts_ms)} → {ms_stamp(series.last_ts_ms)}"
        if series.first_ts_ms is not None and series.last_ts_ms is not None
        else "—"
    )
    instrument = result.config.instrument
    overridden = bool(request.overrides)
    # The risk input is the one parameter a decision run changes: said on its own line.
    defaults = "Pine varsayılanları" + ("" if request.pine_risk else " (işlem başı risk dışında)")
    lines += [
        f"Backtest: {result.spec.strategy_id} ({result.spec.pine_file})",
        f"Bacaklar: {', '.join(result.spec.leg_ids)}",
        "Parametreler: "
        + (defaults if not overridden else f"{defaults} + değişiklik: " + _overrides_text(result)),
        risk_line(result),
        f"Veri: {source.name} ({source.format}), sha256 {source.sha256 or '— (bellekte)'}",
        f"  sembol {series.symbol}, bar {tr_int(series.tf_seconds)} sn, "
        f"{tr_int(len(series))} bar, {span}",
        f"  bar saat dilimi {source.bar_tz}; sentetik: {'EVET' if source.synthetic else 'hayır'}",
        f"Enstrüman: {instrument.symbol} ({instrument.kind.value}), tick "
        f"{tr_plain(instrument.tick_size)}, puan değeri {tr_plain(instrument.point_value)}",
        f"Maliyetler (çarpan {factor_shown(request.effective_factor)}): komisyon "
        f"{tr_number(costs.commission_per_unit_side, 2)}/birim/yön, kayma "
        f"{tr_plain(costs.slippage_ticks)} tick, spread {tr_plain(costs.spread_points)} puan"
        + (f", {len(costs.spread_windows)} spread penceresi" if costs.spread_windows else ""),
        f"Dolum modeli: {request.fill_model.value}",
        f"Başlangıç sermayesi: {tr_number(request.initial_capital, 2)}",
        f"Seans sonu boşluğu: {tr_plain(request.min_gap_seconds / 60)} dk",
        f"Giriş: {tr_int(len(result.entries))}; veri bittiğinde açık (istatistik dışı): "
        f"{tr_int(sum(1 for e in result.entries if not e.complete))}; piramit sınırıyla "
        f"düşürülen giriş: {tr_int(result.engine.dropped_entries)}",
        "",
    ]
    for view in result.views:
        title = (
            f"{view.strategy_id} — portföy toplamı ({view.label})"
            if view.combined
            else f"{view.strategy_id} ({view.label})"
        )
        lines.append(render_stats_text(view.split, title=title))
    if sensitivity is not None:
        lines += _cost_lines(sensitivity)
    if walk_forward_results:
        lines += _walk_forward_lines(walk_forward_results)
    return "\n".join(lines).rstrip("\n") + "\n"


def risk_line(result: RunResult) -> str:
    """Which per-trade risk the run was sized at and where the value came from."""
    spec = result.spec
    used = result.params[spec.risk_param]
    assert isinstance(used, float)
    if result.request.pine_risk:
        text = (
            f"İşlem başı risk: {risk_shown(used)} — betiğin kendi değeri (Pine varsayılanı, "
            f"{spec.risk_param}); planlanan risk kullanılmadı, karar koşusu değil"
        )
    else:
        text = (
            f"İşlem başı risk: {risk_shown(used)} — planlanan risk (config/risk.yaml "
            f"risk_per_trade_pct), betiğin {spec.risk_param} girdisine yazıldı; Pine varsayılanı "
            f"{risk_shown(spec.pine_risk_pct)}"
        )
    cap = spec.risk_cap_param
    if cap is not None:
        value = result.params[cap]
        assert isinstance(value, float)
        # The planned risk replaces one input only; the cap on both engines together is
        # another input of the script and stays whatever the script (or its override) says.
        text += (
            f"; eşzamanlı risk tavanı ({cap}, {risk_shown(value)}) betiğin kendi girdisidir, "
            "planlanan riskle değiştirilmedi"
        )
    return text + risk_base_note(spec, result.params)


def risk_base_note(spec: StrategySpec[Any], params: Mapping[str, object]) -> str:
    """ "" on the account's base; otherwise the clause saying the percent is of another.

    Only a run at the script's own inputs can be on another base (a run with a planned
    risk refuses it). Its "%0,5" is then not half a percent of the account, and a
    reader comparing it with the planned risk must be told.
    """
    if spec.sized_on_account(params):
        return ""
    return (
        f"; risk bazı '{spec.risk_base(params)}' (config/strategies.yaml): bu yüzde hesabın "
        "başlangıç bakiyesinin yüzdesi DEĞİLDİR"
    )


def _cost_lines(sensitivity: CostSensitivity) -> list[str]:
    main = sensitivity.runs[0]
    titles = ["x" + factor_shown(f) for f in sensitivity.factors]
    # Columns as wide as the longest exact factor (plus a gap), so they never run together.
    width = max(12, *(len(title) + 2 for title in titles))
    name_width = max(24, *(len(view.strategy_id) + 2 for view in main.views))
    header = "".join(f"{title:>{width}}" for title in titles)
    lines = ["Maliyet duyarlılığı (OOS beklentisi, ortalama R):", f"  {'':<{name_width}}{header}"]
    for index, view in enumerate(main.views):
        cells = "".join(
            f"{_opt_r(run_.views[index].split.out_of_sample.mean_r):>{width}}"
            for run_ in sensitivity.runs
        )
        lines.append(f"  {view.strategy_id:<{name_width}}{cells}")
    lines.append("")
    return lines


def _walk_forward_lines(legs: Sequence[LegWalkForward]) -> list[str]:
    lines = ["Walk-forward:"]
    for leg in legs:
        summary = leg.result.summary
        if leg.note:
            lines.append(f"  {leg.strategy_id}: {leg.note}")
            continue
        share = "—" if summary.share_positive_oos is None else tr_plain(summary.share_positive_oos)
        efficiency = (
            f"yok ({summary.efficiency_note})"
            if summary.efficiency is None
            else tr_number(summary.efficiency, 2)
        )
        lines.append(
            f"  {leg.strategy_id}: {tr_int(summary.n_folds)} kat, pozitif OOS payı {share}, "
            f"verim {efficiency}"
        )
    lines.append("")
    return lines


def _opt_r(value: float | None) -> str:
    return "—" if value is None else f"{value:+.3f}".replace(".", ",") + "R"


# --- internals ---------------------------------------------------------------------------


class _Tracked:
    """Delegates to the strategy and remembers the last bar it saw (for defect messages)."""

    def __init__(self, inner: Strategy) -> None:
        self._inner = inner
        self.last_bar_index: int | None = None

    @property
    def strategy_id(self) -> str:
        return self._inner.strategy_id

    @property
    def pyramiding(self) -> int:
        return self._inner.pyramiding

    def on_bar(self, bar: Bar, ctx: StrategyContext) -> Sequence[Action]:
        self.last_bar_index = ctx.bar_index
        return self._inner.on_bar(bar, ctx)

    def on_fill(self, fill: FillEvent, ctx: StrategyContext) -> Sequence[Action]:
        self.last_bar_index = ctx.bar_index
        return self._inner.on_fill(fill, ctx)


def _spec(strategy_id: str, registry: Mapping[str, StrategySpec[Any]]) -> StrategySpec[Any]:
    spec = registry.get(strategy_id)
    if spec is None:
        raise RunnerError(
            f"bilinmeyen strateji: {strategy_id} (tanımlılar: {', '.join(sorted(registry))})"
        )
    return spec


def _check_request(request: RunRequest) -> None:
    if not request.series.bars:
        raise RunnerError("veri serisi boş: backtest için en az bir bar gerekli")
    capital = request.initial_capital
    if isinstance(capital, bool) or not (math.isfinite(capital) and capital > 0.0):
        raise RunnerError("başlangıç sermayesi pozitif, sonlu bir sayı olmalı")
    factor = request.cost_factor
    if isinstance(factor, bool) or not (math.isfinite(factor) and factor >= 0.0):
        raise RunnerError("maliyet çarpanı sonlu, negatif olmayan bir sayı olmalı")
    if request.cost_free and factor != DECISION_FACTOR:
        raise RunnerError("maliyetsiz koşuya maliyet çarpanı verilemez")
    if request.min_gap_seconds <= 0:
        raise RunnerError("seans sonu boşluğu pozitif olmalı")
    _check_planned_risk(request.planned_risk_pct)
    if isinstance(request.oos_start_ms, bool) or not isinstance(request.oos_start_ms, int):
        raise RunnerError("OOS başlangıcı epoch milisaniye (tam sayı) olmalı")
    if not 0 <= request.oos_start_ms < _MAX_STAMP_MS:
        # The artifacts store it as a UTC stamp; out of range it would crash there instead.
        raise RunnerError("OOS başlangıcı 1970-01-01 ile 9999-12-31 arasında olmalı")
    configured = request.configured_oos_start_ms
    if configured is not None and (
        isinstance(configured, bool)
        or not isinstance(configured, int)
        or not 0 <= configured < _MAX_STAMP_MS
    ):
        raise RunnerError("config OOS başlangıcı 1970-01-01 ile 9999-12-31 arasında olmalı")


def _check_planned_risk(planned: float | None) -> None:
    """A planned risk is a positive, finite percent, never above the code ceiling."""
    if planned is None:
        return
    if (
        isinstance(planned, bool)
        or not isinstance(planned, int | float)
        or not (math.isfinite(planned) and planned > 0.0)
    ):
        raise RunnerError("planlanan işlem başı risk pozitif, sonlu bir yüzde olmalı")
    if planned > MAX_RISK_PER_TRADE_PCT:
        # config/risk.yaml cannot say this (its schema has the same ceiling); a caller of
        # the library can, and a run above the ceiling must not exist as a decision run.
        raise RunnerError(
            f"planlanan işlem başı risk {risk_shown(planned)} kod tavanını "
            f"({risk_shown(MAX_RISK_PER_TRADE_PCT)}, SPEC §8.1) aşıyor"
        )


def _leg(
    strategy_id: str,
    label: str,
    entries: tuple[EntryResult, ...],
    request: RunRequest,
    combined: bool = False,
) -> LegResult:
    return LegResult(
        strategy_id=strategy_id,
        label=label,
        entries=entries,
        split=split_is_oos(entries, request.oos_start_ms, initial_capital=request.initial_capital),
        tables=time_tables(entries, tz=NEW_YORK),
        curve=equity_curve(entries),
        combined=combined,
    )


def _not_decision_reasons(
    request: RunRequest, costs: CostModel, spec: StrategySpec[Any]
) -> tuple[str, ...]:
    reasons: list[str] = []
    if request.fill_model is not FillModel.PESSIMISTIC:
        reasons.append(
            f"dolum modeli '{request.fill_model.value}' (yalnızca parite karşılaştırması)"
        )
    if costs.is_free:
        reasons.append("maliyetsiz koşu")
    elif request.cost_factor != DECISION_FACTOR:
        reasons.append(f"maliyet çarpanı {factor_shown(request.cost_factor)} (1,0 değil)")
    if request.other_oos_start:
        assert request.configured_oos_start_ms is not None
        # A boundary chosen after seeing results would let G2/G3 be judged on a
        # cherry-picked split: a what-if, like another cost factor.
        reasons.append(
            f"OOS başlangıcı {ms_stamp(request.oos_start_ms)} (config/backtest.yaml: "
            f"{ms_stamp(request.configured_oos_start_ms)})"
        )
    if request.pine_risk:
        # Sized at the script's risk, the trades are not the planned account's (module
        # docstring): a comparison with TradingView, never Go/No-Go evidence.
        base = (
            ""
            if spec.sized_on_account(request.overrides)
            else f", risk bazı '{spec.risk_base(request.overrides)}'"
        )
        reasons.append(
            f"işlem başı risk Pine varsayılanı ({risk_shown(spec.pine_risk_pct)}{base}); karar "
            "koşusu config/risk.yaml riskiyle yapılır"
        )
    return tuple(reasons)


def _strategy_notes(result: RunResult) -> list[str]:
    notes = [f"Strateji {result.spec.strategy_id} ({result.spec.pine_file}).", risk_line(result)]
    if result.request.overrides:
        notes.append(
            "Pine varsayılanlarından sapan parametreler (config/strategies.yaml): "
            f"{_overrides_text(result)}; parite için aynısı TradingView'da da ayarlanmalı."
        )
    oos_warning = _oos_outside_note(result.request)
    if oos_warning is not None:
        notes.append(oos_warning)
    return notes


def _overrides_text(result: RunResult) -> str:
    return ", ".join(
        f"{name}={_scalar_text(result.params[name])}" for name in sorted(result.request.overrides)
    )


def _scalar_text(value: Scalar) -> str:
    # Python's "None"/"True" never reach a Turkish report.
    if value is None:
        return "—"
    if isinstance(value, bool):
        return "true" if value else "false"
    return str(value)


def _oos_outside_note(request: RunRequest) -> str | None:
    """A warning when the OOS start leaves one sample empty by construction.

    Usually a unit slip (seconds for milliseconds, a date outside the file); the split
    itself stays as asked.
    """
    series = request.series
    first, last = series.first_ts_ms, series.last_ts_ms
    if first is None or last is None:
        return None
    stamp = ms_stamp(request.oos_start_ms)
    if request.oos_start_ms <= first:
        return f"OOS başlangıcı ({stamp}) ilk bardan önce ya da ona eşit: IS örneklemi boş"
    if request.oos_start_ms > last:
        return f"OOS başlangıcı ({stamp}) son bardan sonra: OOS örneklemi boş"
    return None


def _comparison_notes(result: RunResult) -> list[str]:
    if result.decision:
        return []
    return [f"{COMPARISON_RUN_TAG}: " + "; ".join(result.not_decision_reasons)]


def _run_notes(result: RunResult) -> list[str]:
    return [*_strategy_notes(result), *_comparison_notes(result)]


def _portfolio_note(result: RunResult, view: LegResult) -> str:
    spec_id = result.spec.strategy_id
    if view.combined:
        return (
            f"{spec_id} portföyünün birleşik görünümü: bacakların "
            f"({', '.join(result.spec.leg_ids)}) tüm girişleri birlikte."
        )
    return (
        f"{spec_id} portföyünün bir bacağı. Düşürülen giriş sayısı bacaklara ayrılamaz: "
        "portföy toplamıdır."
    )


def _sample_stats(split: SampleSplit, sample: str) -> TradeStats:
    return split.all if sample == "all" else split.out_of_sample


def _sample_note(sample: str, oos_start_ms: int) -> str:
    if sample == "all":
        return "Hücreler tüm örneklemin (IS + OOS) tamamlanmış girişlerini özetler."
    return f"Hücreler yalnızca OOS girişlerini özetler (giriş ≥ {ms_stamp(oos_start_ms)})."


@contextlib.contextmanager
def _as_runner_error() -> Iterator[None]:
    # The builders raise ArtifactError (already Turkish); surfaced as a RunnerError so the
    # CLI catches one type.
    try:
        yield
    except ArtifactError as exc:
        raise RunnerError(str(exc)) from exc
