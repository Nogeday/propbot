"""JSON report artifacts under `reports/`: what the dashboard and the Go/No-Go report read.

Every artifact is one pydantic model with a common envelope (kind, schema version,
creation time, label, synthetic flag, data provenance, notes) and a kind-specific
payload. The files are git-ignored and can be edited by hand, so the reader trusts
nothing: size cap, strict UTF-8, strict JSON (no NaN, no duplicate keys), strict schema
(no coercion, no unknown fields). Every failure is an `ArtifactError` with one Turkish
line; the dashboard shows it instead of crashing.

Two rules live in the models themselves so no caller can break them (ADR-0007):
a synthetic artifact always carries the "SENTETİK VERİ" note, and a Go/No-Go artifact
can say GO only when it is not synthetic and all seven gates are PASS.
"""

import datetime as dt
import functools
import hashlib
import json
import math
import os
import re
import stat
import tempfile
from collections.abc import Callable, Iterable, Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Annotated, Final, Literal, Self

import numpy as np
import numpy.typing as npt
from pydantic import (
    AfterValidator,
    BaseModel,
    ConfigDict,
    Field,
    JsonValue,
    TypeAdapter,
    ValidationError,
    model_validator,
)
from pydantic_core import PydanticCustomError

from propbot.backtest.analytics import (
    DEFAULT_CURVE_POINTS,
    EquityPoint,
    SampleSplit,
    TableRow,
    TimeTables,
    TradeStats,
    downsample_curve,
    equity_curve,
    split_is_oos,
    time_tables,
    to_mc_trades,
)
from propbot.backtest.model import EngineConfig, EntryResult
from propbot.backtest.montecarlo import (
    BreachReason,
    ChallengeRules,
    DurationStats,
    MonteCarloParams,
    MonteCarloSummary,
    PathOutcomes,
)
from propbot.backtest.trades import IntratradeModel, Trade, TradesProvenance
from propbot.backtest.walkforward import WalkForwardResult
from propbot.core.bars import BarSeries
from propbot.risk.venue import VenueProfile

SCHEMA_VERSION: Final = 1
MAX_ARTIFACT_BYTES: Final = 8 * 1024 * 1024
# latest_artifacts and all_artifacts read at most this many files, newest names first.
MAX_EXAMINED_FILES: Final = 300
MAX_PARITY_MISMATCHES: Final = 200
# The longest integer a report file may hold, sign included (`_bounded_int`): far beyond
# every real value, and short enough that the product of two still fits a float.
_MAX_INTEGER_DIGITS: Final = 100
# match_rate must equal matched / compared within float rounding of one division.
_PARITY_RATE_TOL: Final = 1e-9
MAX_NOTES: Final = 100
DEFAULT_HISTOGRAM_BINS: Final = 30
DEFAULT_COST_FACTORS: Final = (1.0, 1.5, 2.0)
GATE_IDS: Final = ("G1", "G2", "G3", "G4", "G5", "G6", "G7")

SYNTHETIC_TAG: Final = "SENTETİK VERİ"
SYNTHETIC_NOTE: Final = (
    f"{SYNTHETIC_TAG}: bu rapor sentetik barlarla üretildi; hiçbir kenarı, pariteyi ya da "
    "veri kalitesini kanıtlamaz (ADR-0007)."
)
COST_FREE_TAG: Final = "MALİYETSİZ KOŞU"
COST_FREE_NOTE: Final = f"{COST_FREE_TAG}: yalnızca karşılaştırma içindir (SPEC §6.3)."
# The mark of a run sized at the script's own per-trade risk instead of the planned one
# (user decision of 2026-11-19): the last suffix of its labels, and the suffix of the
# strategy ids in its trade list. The runner writes it and Go/No-Go G4 reads it, so it
# lives here, where both look.
PINE_RISK_SUFFIX: Final = "-pinerisk"
# How the runner's note on a run that is not a decision run begins. The runner writes it
# with every reason (also the two no other field shows: another OOS start, the script's
# own risk); the dashboard reads it, so the wording lives here, where both look.
COMPARISON_RUN_TAG: Final = "KARŞILAŞTIRMA KOŞUSU (karar koşusu değil)"

KINDS: Final = (
    "backtest",
    "cost_matrix",
    "walk_forward",
    "monte_carlo",
    "go_no_go",
    "data_validation",
    "parity",
)
_LABEL = re.compile(r"[a-z0-9_-]{1,64}")
_UTC_STAMP = re.compile(r"\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}(\.\d{1,6})?Z")
_ISO_DATE = re.compile(r"\d{4}-\d{2}-\d{2}")
_SHA256 = re.compile(r"[0-9a-f]{64}")
_EPOCH: Final = dt.datetime(1970, 1, 1, tzinfo=dt.UTC)
_FILE_NAME = re.compile(
    r"(?P<kind>" + "|".join(KINDS) + r")_(?P<label>[a-z0-9_-]{1,64})_(?P<stamp>\d{8}T\d{6}Z)\.json"
)
_FILE_STAMP: Final = "%Y%m%dT%H%M%SZ"

_ERROR_TEXT: Final = {
    "missing": "eksik alan",
    "extra_forbidden": "bilinmeyen alan",
    "union_tag_invalid": "bilinmeyen rapor türü (kind)",
    "union_tag_not_found": "rapor türü (kind) yok",
    "literal_error": "izin verilmeyen değer",
    "finite_number": "sonlu bir sayı olmalı",
    "too_long": "liste fazla uzun",
    "too_short": "liste fazla kısa",
    "greater_than": "değer fazla küçük",
    "greater_than_equal": "değer fazla küçük",
    "less_than": "değer fazla büyük",
    "less_than_equal": "değer fazla büyük",
    "string_too_long": "metin fazla uzun",
    "model_type": "nesne olmalı",
    "model_attributes_type": "nesne olmalı",
    "dict_type": "nesne olmalı",
    "frozen_instance": "değiştirilemez",
}


class ArtifactError(Exception):
    """An artifact cannot be read or written. The message is one user-facing Turkish line."""


def _check_label(value: str) -> str:
    if _LABEL.fullmatch(value) is None:
        raise PydanticCustomError(
            "propbot_label", "etiket yalnızca a-z, 0-9, _ ve - içerebilir (1-64 karakter)"
        )
    return value


def _check_utc_stamp(value: str) -> str:
    if _UTC_STAMP.fullmatch(value) is None:
        raise PydanticCustomError(
            "propbot_utc", "UTC zaman damgası olmalı (ör. 2026-11-18T14:30:00Z)"
        )
    try:
        dt.datetime.fromisoformat(value)
    except ValueError as exc:
        raise PydanticCustomError("propbot_utc", "geçersiz tarih-saat") from exc
    return value


def _check_date(value: str) -> str:
    if _ISO_DATE.fullmatch(value) is None:
        raise PydanticCustomError("propbot_date", "tarih YYYY-AA-GG olmalı")
    try:
        dt.date.fromisoformat(value)
    except ValueError as exc:
        raise PydanticCustomError("propbot_date", "geçersiz tarih") from exc
    return value


def _check_sha256(value: str) -> str:
    if value and _SHA256.fullmatch(value) is None:
        raise PydanticCustomError("propbot_sha256", "sha256 64 küçük onaltılık karakter olmalı")
    return value


Label = Annotated[str, AfterValidator(_check_label)]
UtcStamp = Annotated[str, AfterValidator(_check_utc_stamp)]
IsoDate = Annotated[str, AfterValidator(_check_date)]
Text = Annotated[str, Field(max_length=4000)]
Name = Annotated[str, Field(min_length=1, max_length=256)]
Count = Annotated[int, Field(ge=0)]
NonNegative = Annotated[float, Field(ge=0.0)]
Positive = Annotated[float, Field(gt=0.0)]
Probability = Annotated[float, Field(ge=0.0, le=1.0)]
Scalar = str | int | float | bool | None
Digest = Annotated[str, Field(pattern=r"^[0-9a-f]{64}$")]
GapSeconds = Annotated[int, Field(ge=1)]


class ArtifactModel(BaseModel):
    """No coercion, no unknown keys, no NaN/inf, immutable."""

    model_config = ConfigDict(strict=True, extra="forbid", frozen=True, allow_inf_nan=False)


class DataInfo(ArtifactModel):
    file_name: Name
    sha256: Annotated[str, AfterValidator(_check_sha256)]
    format: Name
    symbol: Name
    tf_seconds: Annotated[int, Field(ge=1)]
    bars: Count
    first_utc: UtcStamp | None
    last_utc: UtcStamp | None
    bar_tz: Name
    synthetic: bool


class ArtifactEnvelope(ArtifactModel):
    schema_version: Literal[1]
    created_utc: UtcStamp
    label: Label
    synthetic: bool
    data: DataInfo | None
    notes: Annotated[tuple[Text, ...], Field(max_length=MAX_NOTES)]

    @model_validator(mode="after")
    def _synthetic_is_visible(self) -> Self:
        if self.data is not None and self.data.synthetic and not self.synthetic:
            raise PydanticCustomError(
                "propbot_synthetic", "sentetik veriyle üretilen rapor 'synthetic: true' olmalı"
            )
        if self.synthetic and not any(SYNTHETIC_TAG in note for note in self.notes):
            raise PydanticCustomError(
                "propbot_synthetic", "sentetik rapor 'SENTETİK VERİ' notunu taşımalı"
            )
        return self


class StatsModel(ArtifactModel):
    n: Count
    wins: Count
    losses: Count
    breakeven: Count
    win_rate: Probability | None
    mean_r: float | None
    median_r: float | None
    stdev_r: NonNegative | None
    t_stat: float | None
    profit_factor: NonNegative | None
    total_r: float
    total_pnl: float
    avg_win_r: float | None
    avg_loss_r: float | None
    max_drawdown_r: NonNegative
    max_drawdown_pct: NonNegative
    longest_losing_streak: Count
    incomplete: Count

    @model_validator(mode="after")
    def _counts_add_up(self) -> Self:
        if self.wins + self.losses + self.breakeven != self.n:
            raise PydanticCustomError("propbot_counts", "kazanan + kaybeden + başa baş ≠ işlem")
        return self


class TableRowModel(ArtifactModel):
    key: Name
    label: Name
    trades: Count
    wins: Count
    win_rate: Probability | None
    total_r: float
    mean_r: float | None
    total_pnl: float


class TablesModel(ArtifactModel):
    weekday: tuple[TableRowModel, ...]
    hour: tuple[TableRowModel, ...]
    month: tuple[TableRowModel, ...]


class EquityPointModel(ArtifactModel):
    exit_ts_ms: int
    cum_r: float
    cum_pnl: float


class InstrumentInfo(ArtifactModel):
    symbol: Name
    kind: Literal["futures", "cfd"]
    tick_size: Positive
    point_value: Positive
    # The size grid: strategies floor sizes to qty_step and skip a size below min_qty, so
    # another grid is another set of trades. None = not recorded (an older file): such a
    # run is never a Go/No-Go decision run and such a parity report never passes G6.
    qty_step: Positive | None = None
    min_qty: Positive | None = None


class SpreadWindowInfo(ArtifactModel):
    start_minute: Annotated[int, Field(ge=0, le=1439)]
    end_minute: Annotated[int, Field(ge=0, le=1439)]
    spread_points: NonNegative


class CostInfo(ArtifactModel):
    commission_per_unit_side: NonNegative
    slippage_ticks: NonNegative
    spread_points: NonNegative
    spread_windows: tuple[SpreadWindowInfo, ...]
    cost_free: bool
    # The zone the spread windows' minutes are read in (config/backtest.yaml session.tz):
    # the same minutes in another zone are other hours. None = not recorded (older file).
    spread_tz: Name | None = None

    @model_validator(mode="after")
    def _flag_matches_values(self) -> Self:
        free = (
            self.commission_per_unit_side == 0.0
            and self.slippage_ticks == 0.0
            and self.spread_points == 0.0
            and all(window.spread_points == 0.0 for window in self.spread_windows)
        )
        if free != self.cost_free:
            raise PydanticCustomError(
                "propbot_cost_free", "cost_free bayrağı maliyetlerle çelişiyor"
            )
        return self


class BacktestArtifact(ArtifactEnvelope):
    kind: Literal["backtest"] = "backtest"
    strategy_id: Name
    params: dict[str, Scalar]
    instrument: InstrumentInfo
    costs: CostInfo
    fill_model: Literal["pessimistic", "tv_ohlc"]
    # The cost sensitivity factor the costs above were scaled by (1.0 = the configured
    # costs). None = not recorded (written before the field existed, or by a caller that
    # did not say): such a run is never a Go/No-Go decision run (gonogo, G2/G3).
    cost_factor: NonNegative | None = None
    initial_capital: Positive
    oos_start_utc: UtcStamp
    stats_all: StatsModel
    stats_is: StatsModel
    stats_oos: StatsModel
    g3_ratio: float | None
    g3_note: Text | None
    tables: TablesModel
    equity_curve: tuple[EquityPointModel, ...]
    incomplete_entries: Count
    dropped_entries: Count
    # math.fsum of mae_r over the complete entries (the rows of the run's trade list). A
    # Monte Carlo run's list must add up to it, or its floating losses were edited (G4).
    # None = not recorded (an older file): no Monte Carlo run can be tied to this one.
    mae_total_r: float | None = None
    # config/backtest.yaml session.min_gap_minutes in seconds: where sessions end, and so
    # Pine's `session.islastbar` the strategies read. None = not recorded (an older file):
    # such a run is never a Go/No-Go decision run.
    min_gap_seconds: GapSeconds | None = None
    # `trade_rows_digest` of the rows this run writes to a trade list (its complete
    # entries). G4 compares it with the Monte Carlo list's rows: a list whose times were
    # moved keeps its count and sums, not its digest. None = not recorded (older file).
    trades_digest: Digest | None = None
    # The same rows grouped by the strategy id each row carries, as the Monte Carlo
    # artifact groups the list it read (`trades_digests`). A single-strategy run has one
    # key, its own id; the Master's combined view has one per leg, which is what lets G4
    # tie a trade list (whose rows name the legs) to the combined run when the portfolio
    # is judged by its combined id (user decision of 2026-11-19, open finding 72).
    # None = not recorded (an older file): G4 falls back to `trades_digest` under the
    # run's own id, which can never match a combined view.
    trades_digests: dict[Annotated[str, Field(max_length=256)], Digest] | None = None

    @model_validator(mode="after")
    def _cost_free_is_visible(self) -> Self:
        if self.costs.cost_free and not any(COST_FREE_TAG in note for note in self.notes):
            raise PydanticCustomError(
                "propbot_cost_free", "maliyetsiz koşu 'MALİYETSİZ KOŞU' notunu taşımalı"
            )
        return self


class CostCell(ArtifactModel):
    factor: NonNegative
    n: Count
    mean_r: float | None
    t_stat: float | None
    total_r: float
    profit_factor: NonNegative | None
    max_drawdown_r: NonNegative


class CostRow(ArtifactModel):
    strategy_id: Name
    cells: tuple[CostCell, ...]


class CostMatrixArtifact(ArtifactEnvelope):
    kind: Literal["cost_matrix"] = "cost_matrix"
    sample: Literal["all", "oos"]
    factors: Annotated[tuple[NonNegative, ...], Field(min_length=1)]
    rows: tuple[CostRow, ...]

    @model_validator(mode="after")
    def _grid_is_complete(self) -> Self:
        if any(b <= a for a, b in zip(self.factors, self.factors[1:], strict=False)):
            raise PydanticCustomError("propbot_factors", "maliyet çarpanları artan sırada olmalı")
        for row in self.rows:
            if tuple(cell.factor for cell in row.cells) != self.factors:
                raise PydanticCustomError(
                    "propbot_factors", "her satırın hücreleri çarpan listesine uymalı"
                )
        return self


class WalkForwardConfigModel(ArtifactModel):
    is_days: Annotated[int, Field(ge=1)]
    oos_days: Annotated[int, Field(ge=1)]
    step_days: Annotated[int, Field(ge=1)]

    @model_validator(mode="after")
    def _no_overlap(self) -> Self:
        if self.step_days < self.oos_days:
            raise PydanticCustomError("propbot_step", "adım OOS penceresinden kısa olamaz")
        return self


class FoldModel(ArtifactModel):
    index: Count
    is_first_day: IsoDate
    is_last_day: IsoDate
    oos_first_day: IsoDate
    oos_last_day: IsoDate
    is_stats: StatsModel
    oos_stats: StatsModel
    ratio: float | None
    ratio_note: Text | None


class WalkForwardSummaryModel(ArtifactModel):
    n_folds: Count
    share_positive_oos: Probability | None
    pooled_oos: StatsModel
    efficiency: float | None
    efficiency_note: Text | None
    parameter_search: Literal[False]
    note: Text


class WalkForwardArtifact(ArtifactEnvelope):
    kind: Literal["walk_forward"] = "walk_forward"
    strategy_id: Name
    config: WalkForwardConfigModel
    span_first_day: IsoDate
    span_last_day: IsoDate
    folds: tuple[FoldModel, ...]
    summary: WalkForwardSummaryModel

    @model_validator(mode="after")
    def _fold_count(self) -> Self:
        if self.summary.n_folds != len(self.folds):
            raise PydanticCustomError("propbot_folds", "kat sayısı kat listesine uymuyor")
        return self


class RulesModel(ArtifactModel):
    targets_pct: Annotated[tuple[Positive, ...], Field(min_length=1)]
    daily_loss_limit_pct: Positive
    max_loss_pct: Positive
    min_trading_days: Count
    min_trading_days_assumed: bool
    # The venue profile's other rule fields as read at simulation time. None = not
    # recorded (a file written before the fields existed): G4 cannot tell whether the
    # profile changed since, so it is NOT_EVALUABLE, never PASS.
    max_loss_type: Literal["static", "trailing_eod"] | None = None
    daily_loss_basis: Literal["max_balance_equity_at_reset", "balance_at_reset"] | None = None
    daily_reset_tz: Name | None = None
    daily_reset_time: Annotated[str, Field(pattern=r"^([01]\d|2[0-3]):[0-5]\d$")] | None = None


class MonteCarloInputs(ArtifactModel):
    trades_file: Name
    n_trades: Annotated[int, Field(ge=1)]
    risk_pct: Positive
    cost_r: NonNegative
    n_paths: Annotated[int, Field(ge=1)]
    max_days: Annotated[int, Field(ge=1)]
    dd_days: Annotated[int, Field(ge=1)]
    seed: Count
    skip_prob: Probability
    experimental_risk_steps: bool
    venue: Name
    rules: RulesModel
    # How deep an open trade floated (`--intratrade`). Only "stop" is pessimistic; None =
    # not recorded (an older file). G4 passes on "stop" only.
    intratrade: Literal["stop", "closed"] | None = None
    # math.fsum of the list's R values as read (before --cost-r) and of its mae_r values
    # (None when no row has one). G4 ties them to the decision runs: a list whose rows
    # were rewritten keeps its count but not its sums. None on sum_r = not recorded.
    sum_r: float | None = None
    sum_mae_r: float | None = None
    # Strategy id -> `trade_rows_digest` of the list's rows of that strategy, as read: the
    # times decide which trades share a prop day, which the sums cannot see. None = not
    # recorded (an older file); G4 then cannot tie the rows to the decision runs.
    trades_digests: dict[Annotated[str, Field(max_length=256)], Digest] | None = None


class BreachShareModel(ArtifactModel):
    phase: Annotated[int, Field(ge=1)]
    reason: Literal["daily_loss", "max_loss"]
    probability: Probability


class DurationModel(ArtifactModel):
    n: Annotated[int, Field(ge=1)]
    mean: NonNegative
    p25: NonNegative
    median: NonNegative
    p75: NonNegative
    p90: NonNegative
    p95: NonNegative


class DrawdownModel(ArtifactModel):
    median: NonNegative
    q95: NonNegative
    q99: NonNegative
    es95: NonNegative
    es99: NonNegative


class HistogramModel(ArtifactModel):
    edges: Annotated[tuple[float, ...], Field(min_length=2)]
    counts: Annotated[tuple[Count, ...], Field(min_length=1)]

    @model_validator(mode="after")
    def _shape(self) -> Self:
        if len(self.edges) != len(self.counts) + 1:
            raise PydanticCustomError("propbot_histogram", "kenar sayısı kutu sayısı + 1 olmalı")
        if any(b <= a for a, b in zip(self.edges, self.edges[1:], strict=False)):
            raise PydanticCustomError("propbot_histogram", "kutu kenarları artan sırada olmalı")
        return self


class TradesProvenanceModel(ArtifactModel):
    """`trades.TradesProvenance` as stored in a Monte Carlo artifact."""

    synthetic: bool
    cost_free: bool
    fill_model: Literal["pessimistic", "tv_ohlc"]
    cost_factor: NonNegative
    data_sha256: Annotated[str, AfterValidator(_check_sha256)]
    data_file: Name
    strategy_ids: Annotated[tuple[Name, ...], Field(min_length=1)]
    created_utc: UtcStamp

    @model_validator(mode="after")
    def _sorted_ids(self) -> Self:
        if list(self.strategy_ids) != sorted(set(self.strategy_ids)):
            raise PydanticCustomError(
                "propbot_provenance", "strategy_ids sıralı ve tekrarsız olmalı"
            )
        return self


class MonteCarloArtifact(ArtifactEnvelope):
    kind: Literal["monte_carlo"] = "monte_carlo"
    inputs: MonteCarloInputs
    # Which backtest the trade list came from (its "#propbot-trades" line). None = unknown
    # (a hand-made list): the run still shows, but it cannot support a Go/No-Go G4 PASS.
    provenance: TradesProvenanceModel | None = None
    p_pass_phase: Annotated[tuple[Probability, ...], Field(min_length=1)]
    p_breach: Probability
    breach_shares: tuple[BreachShareModel, ...]
    p_undecided: Probability
    time_to_pass: tuple[DurationModel | None, ...]
    drawdown: DrawdownModel
    drawdown_histogram: HistogramModel

    @model_validator(mode="after")
    def _consistent(self) -> Self:
        phases = len(self.inputs.rules.targets_pct)
        if len(self.p_pass_phase) != phases or len(self.time_to_pass) != phases:
            raise PydanticCustomError("propbot_phases", "faz sayısı kurallarla uyuşmuyor")
        if sum(self.drawdown_histogram.counts) != self.inputs.n_paths:
            raise PydanticCustomError(
                "propbot_histogram", "histogram toplamı yol sayısına eşit değil"
            )
        if self.provenance is not None and self.provenance.synthetic and not self.synthetic:
            raise PydanticCustomError(
                "propbot_synthetic",
                "sentetik işlem listesiyle üretilen rapor 'synthetic: true' olmalı",
            )
        return self


class Gate(ArtifactModel):
    id: Literal["G1", "G2", "G3", "G4", "G5", "G6", "G7"]
    title: Name
    status: Literal["PASS", "FAIL", "NOT_EVALUABLE"]
    evidence: Text
    details: dict[str, JsonValue]


class GoNoGoArtifact(ArtifactEnvelope):
    kind: Literal["go_no_go"] = "go_no_go"
    gates: tuple[Gate, ...]
    overall: Literal["GO", "NO_GO"]
    reasons: tuple[Text, ...]

    @model_validator(mode="after")
    def _go_needs_every_gate(self) -> Self:
        ids = [gate.id for gate in self.gates]
        if len(ids) != len(set(ids)):
            raise PydanticCustomError("propbot_gates", "bir kapı birden fazla kez yazılmış")
        if self.overall == "GO":
            # ADR-0007 item 3 / invariant 1: GO only on real data with all seven gates PASS.
            if self.synthetic:
                raise PydanticCustomError("propbot_go", "sentetik veriyle GO verilemez")
            if sorted(ids) != list(GATE_IDS) or any(g.status != "PASS" for g in self.gates):
                raise PydanticCustomError(
                    "propbot_go", "GO için G1-G7 kapılarının hepsi PASS olmalı"
                )
        return self


class DataValidationArtifact(ArtifactEnvelope):
    kind: Literal["data_validation"] = "data_validation"
    clean: bool
    report: dict[str, JsonValue]


class ParityArtifact(ArtifactEnvelope):
    kind: Literal["parity"] = "parity"
    strategy_id: Name
    matched: Count
    # Paired trades that differ in a compared field. Default 0 for files written before
    # the field existed: the rate check below then holds only if there were none.
    mismatched: Count = 0
    python_only: Count
    tradingview_only: Count
    match_rate: Probability | None  # None when neither side has a trade
    threshold: Annotated[float, Field(gt=0.0, le=1.0)]
    passed: bool
    mismatches: Annotated[tuple[dict[str, JsonValue], ...], Field(max_length=MAX_PARITY_MISMATCHES)]
    sources: tuple[Name, ...]
    # The Python side's effective parameters and instrument. None = not recorded (an older
    # file): G6 cannot tell which strategy was compared, so it is NOT_EVALUABLE.
    params: dict[str, Scalar] | None = None
    instrument: InstrumentInfo | None = None
    # The Python side's session gap and initial capital: both change which trades the
    # port makes (session.islastbar, whole-contract sizes). None = not recorded (older).
    min_gap_seconds: GapSeconds | None = None
    initial_capital: Positive | None = None

    @model_validator(mode="after")
    def _passed_needs_rate(self) -> Self:
        if self.passed and (self.match_rate is None or self.match_rate < self.threshold):
            raise PydanticCustomError(
                "propbot_parity", "eşleşme oranı eşiğin altındayken geçti denemez"
            )
        # The rate must follow from the counts: a hand-edited rate (or counts) is refused.
        total = self.matched + self.mismatched + self.python_only + self.tradingview_only
        if total > 0:
            expected = self.matched / total
            if self.match_rate is None or abs(self.match_rate - expected) > _PARITY_RATE_TOL:
                raise PydanticCustomError(
                    "propbot_parity",
                    "eşleşme oranı sayılarla tutarsız (eşleşen / karşılaştırılan toplam)",
                )
        elif self.match_rate is not None:
            raise PydanticCustomError(
                "propbot_parity", "hiç işlem karşılaştırılmadıysa eşleşme oranı olamaz"
            )
        return self


AnyArtifact = (
    BacktestArtifact
    | CostMatrixArtifact
    | WalkForwardArtifact
    | MonteCarloArtifact
    | GoNoGoArtifact
    | DataValidationArtifact
    | ParityArtifact
)
_ADAPTER: Final[TypeAdapter[AnyArtifact]] = TypeAdapter(
    Annotated[AnyArtifact, Field(discriminator="kind")]
)


@dataclass(frozen=True, slots=True)
class LoadedArtifact:
    file_name: str
    artifact: AnyArtifact


@dataclass(frozen=True, slots=True)
class ArtifactProblem:
    file_name: str
    message: str  # Turkish, one line


@dataclass(frozen=True, slots=True)
class LatestArtifacts:
    artifacts: tuple[LoadedArtifact, ...]  # newest per (kind, label), sorted by kind and label
    problems: tuple[ArtifactProblem, ...]


@dataclass(frozen=True, slots=True)
class AllArtifacts:
    """Every readable artifact among the newest `MAX_EXAMINED_FILES` file names.

    For the Go/No-Go report: "newest per label" would let a newer comparison run hide an
    older decision run under the same label. `truncated` says that more report files
    exist than were examined, so a decision may rest on an incomplete set.
    """

    artifacts: tuple[LoadedArtifact, ...]  # newest file name first
    problems: tuple[ArtifactProblem, ...]
    truncated: bool
    examined: int  # file names examined (readable or not)
    total: int  # report-like file names found


def _schema_errors[**P, R](builder: Callable[P, R]) -> Callable[P, R]:
    """A schema violation in a builder (a bug or an out-of-range number) as a Turkish line."""

    @functools.wraps(builder)
    def wrapper(*args: P.args, **kwargs: P.kwargs) -> R:
        try:
            return builder(*args, **kwargs)
        except ValidationError as exc:
            raise ArtifactError(f"rapor oluşturulamadı: {_describe(exc)}") from None

    return wrapper


# --- builders ----------------------------------------------------------------------------


def utc_stamp(moment: dt.datetime) -> str:
    """`2026-11-18T14:30:00Z`; the caller supplies the time (no clock is read here)."""
    if moment.utcoffset() is None:
        raise ArtifactError("rapor zamanı saat dilimi içermeli")
    return moment.astimezone(dt.UTC).replace(microsecond=0).strftime("%Y-%m-%dT%H:%M:%SZ")


def ms_stamp(ts_ms: int) -> str:
    epoch = dt.datetime(1970, 1, 1, tzinfo=dt.UTC)
    return utc_stamp(epoch + dt.timedelta(milliseconds=ts_ms))


def with_warnings(
    notes: Iterable[str], *, synthetic: bool, cost_free: bool = False
) -> tuple[str, ...]:
    """The notes with the mandatory warnings in front (each once)."""
    result = list(notes)
    if cost_free and not any(COST_FREE_TAG in note for note in result):
        result.insert(0, COST_FREE_NOTE)
    if synthetic and not any(SYNTHETIC_TAG in note for note in result):
        result.insert(0, SYNTHETIC_NOTE)
    return tuple(result)


@_schema_errors
def data_info(series: BarSeries) -> DataInfo:
    source = series.source
    return DataInfo(
        file_name=source.name,
        sha256=source.sha256,
        format=source.format,
        symbol=series.symbol,
        tf_seconds=series.tf_seconds,
        bars=len(series),
        first_utc=None if series.first_ts_ms is None else ms_stamp(series.first_ts_ms),
        last_utc=None if series.last_ts_ms is None else ms_stamp(series.last_ts_ms),
        bar_tz=source.bar_tz,
        synthetic=source.synthetic,
    )


@_schema_errors
def stats_model(stats: TradeStats) -> StatsModel:
    return StatsModel(
        n=stats.n,
        wins=stats.wins,
        losses=stats.losses,
        breakeven=stats.breakeven,
        win_rate=stats.win_rate,
        mean_r=stats.mean_r,
        median_r=stats.median_r,
        stdev_r=stats.stdev_r,
        t_stat=stats.t_stat,
        profit_factor=stats.profit_factor,
        total_r=stats.total_r,
        total_pnl=stats.total_pnl,
        avg_win_r=stats.avg_win_r,
        avg_loss_r=stats.avg_loss_r,
        max_drawdown_r=stats.max_drawdown_r,
        max_drawdown_pct=stats.max_drawdown_pct,
        longest_losing_streak=stats.longest_losing_streak,
        incomplete=stats.incomplete,
    )


@_schema_errors
def tables_model(tables: TimeTables) -> TablesModel:
    def rows(source: Sequence[TableRow]) -> tuple[TableRowModel, ...]:
        return tuple(
            TableRowModel(
                key=row.key,
                label=row.label,
                trades=row.trades,
                wins=row.wins,
                win_rate=row.win_rate,
                total_r=row.total_r,
                mean_r=row.mean_r,
                total_pnl=row.total_pnl,
            )
            for row in source
        )

    return TablesModel(
        weekday=rows(tables.weekday), hour=rows(tables.hour), month=rows(tables.month)
    )


@_schema_errors
def backtest_artifact(
    *,
    label: str,
    created: dt.datetime,
    strategy_id: str,
    params: Mapping[str, Scalar],
    config: EngineConfig,
    entries: Sequence[EntryResult],
    oos_start_ms: int,
    dropped_entries: int,
    data: BarSeries | None,
    synthetic: bool,
    notes: Iterable[str] = (),
    curve_points: int = DEFAULT_CURVE_POINTS,
    cost_factor: float | None = None,
    min_gap_seconds: int | None = None,
) -> BacktestArtifact:
    """`cost_factor`: the factor `config.costs` was scaled by (1.0 for the configured
    costs). Left at None the run is recorded as "factor unknown" and can never be a
    Go/No-Go decision run. `min_gap_seconds`: the session gap the run used; None is
    recorded as unknown, which is never a decision run either."""
    capital = config.initial_capital
    # The same entries `to_mc_trades` writes to the trade list (complete ones only).
    mae_total = math.fsum(entry.mae_r for entry in entries if entry.complete)
    split: SampleSplit = split_is_oos(entries, oos_start_ms, initial_capital=capital)
    costs = config.costs
    curve: tuple[EquityPoint, ...] = downsample_curve(equity_curve(entries), curve_points)
    return build_artifact(
        BacktestArtifact,
        label=label,
        created=created,
        synthetic=synthetic,
        data=data,
        notes=notes,
        cost_free=costs.is_free,
        strategy_id=strategy_id,
        params=dict(params),
        cost_factor=cost_factor,
        instrument=InstrumentInfo(
            symbol=config.instrument.symbol,
            kind=config.instrument.kind.value,
            tick_size=config.instrument.tick_size,
            point_value=config.instrument.point_value,
            qty_step=config.instrument.qty_step,
            min_qty=config.instrument.min_qty,
        ),
        costs=CostInfo(
            commission_per_unit_side=costs.commission_per_unit_side,
            slippage_ticks=costs.slippage_ticks,
            spread_points=costs.spread_points,
            spread_windows=tuple(
                SpreadWindowInfo(
                    start_minute=window.start_minute,
                    end_minute=window.end_minute,
                    spread_points=window.spread_points,
                )
                for window in costs.spread_windows
            ),
            cost_free=costs.is_free,
            spread_tz=costs.spread_tz,
        ),
        fill_model=config.fill_model.value,
        initial_capital=capital,
        oos_start_utc=ms_stamp(oos_start_ms),
        stats_all=stats_model(split.all),
        stats_is=stats_model(split.in_sample),
        stats_oos=stats_model(split.out_of_sample),
        g3_ratio=split.g3_ratio,
        g3_note=split.g3_note,
        tables=tables_model(time_tables(entries)),
        equity_curve=tuple(
            EquityPointModel(exit_ts_ms=p.exit_ts_ms, cum_r=p.cum_r, cum_pnl=p.cum_pnl)
            for p in curve
        ),
        incomplete_entries=split.all.incomplete,
        dropped_entries=dropped_entries,
        mae_total_r=mae_total,
        min_gap_seconds=min_gap_seconds,
        trades_digest=trade_rows_digest(to_mc_trades(entries)),
        trades_digests=trades_digests(to_mc_trades(entries)),
    )


@_schema_errors
def cost_cell(factor: float, stats: TradeStats) -> CostCell:
    return CostCell(
        factor=factor,
        n=stats.n,
        mean_r=stats.mean_r,
        t_stat=stats.t_stat,
        total_r=stats.total_r,
        profit_factor=stats.profit_factor,
        max_drawdown_r=stats.max_drawdown_r,
    )


@_schema_errors
def walk_forward_artifact(
    result: WalkForwardResult,
    *,
    label: str,
    created: dt.datetime,
    strategy_id: str,
    data: BarSeries | None,
    synthetic: bool,
    notes: Iterable[str] = (),
) -> WalkForwardArtifact:
    summary = result.summary
    return build_artifact(
        WalkForwardArtifact,
        label=label,
        created=created,
        synthetic=synthetic,
        data=data,
        notes=notes,
        strategy_id=strategy_id,
        config=WalkForwardConfigModel(
            is_days=result.config.is_days,
            oos_days=result.config.oos_days,
            step_days=result.config.step_days,
        ),
        span_first_day=result.span_first_day.isoformat(),
        span_last_day=result.span_last_day.isoformat(),
        folds=tuple(
            FoldModel(
                index=fold.index,
                is_first_day=fold.is_first_day.isoformat(),
                is_last_day=fold.is_last_day.isoformat(),
                oos_first_day=fold.oos_first_day.isoformat(),
                oos_last_day=fold.oos_last_day.isoformat(),
                is_stats=stats_model(fold.is_stats),
                oos_stats=stats_model(fold.oos_stats),
                ratio=fold.ratio,
                ratio_note=fold.ratio_note,
            )
            for fold in result.folds
        ),
        summary=WalkForwardSummaryModel(
            n_folds=summary.n_folds,
            share_positive_oos=summary.share_positive_oos,
            pooled_oos=stats_model(summary.pooled_oos),
            efficiency=summary.efficiency,
            efficiency_note=summary.efficiency_note,
            parameter_search=False,
            note=summary.note,
        ),
    )


@_schema_errors
def drawdown_histogram(
    values: npt.NDArray[np.float64], bins: int = DEFAULT_HISTOGRAM_BINS
) -> HistogramModel:
    data = np.asarray(values, dtype=np.float64)
    if data.size == 0 or not np.all(np.isfinite(data)):
        raise ArtifactError("düşüş histogramı için sonlu değerler gerekli")
    if bins < 1:
        raise ArtifactError("histogram en az bir kutu içermeli")
    counts, edges = np.histogram(data, bins=bins)
    return HistogramModel(
        edges=tuple(float(edge) for edge in edges), counts=tuple(int(c) for c in counts)
    )


@_schema_errors
def monte_carlo_artifact(
    summary: MonteCarloSummary,
    outcomes: PathOutcomes,
    *,
    label: str,
    created: dt.datetime,
    params: MonteCarloParams,
    rules: ChallengeRules,
    trades_file: str,
    n_trades: int,
    cost_r: float,
    venue: str,
    synthetic: bool,
    data: BarSeries | None = None,
    notes: Iterable[str] = (),
    bins: int = DEFAULT_HISTOGRAM_BINS,
    provenance: TradesProvenance | None = None,
    venue_profile: VenueProfile | None = None,
    intratrade: IntratradeModel | None = None,
    trades: Sequence[Trade] | None = None,
) -> MonteCarloArtifact:
    """`provenance`: the trade list's "#propbot-trades" line (`trades.read_trades_csv`).
    None = unknown origin; the run cannot support a Go/No-Go G4 PASS. A synthetic list
    makes the artifact synthetic whatever `synthetic` says.

    `venue_profile`, `intratrade` and `trades` (the list as read, before --cost-r) are
    recorded so G4 can compare them with today's profile and the decision runs; each
    left at None is recorded as unknown, and an unknown never passes G4."""
    synthetic = synthetic or (provenance is not None and provenance.synthetic)
    sum_r = None if trades is None else math.fsum(trade.r for trade in trades)
    maes = [] if trades is None else [t.mae_r for t in trades if t.mae_r is not None]
    sum_mae_r = math.fsum(maes) if maes else None
    reason_names: dict[BreachReason, Literal["daily_loss", "max_loss"]] = {
        BreachReason.DAILY_LOSS: "daily_loss",
        BreachReason.MAX_LOSS: "max_loss",
    }
    return build_artifact(
        MonteCarloArtifact,
        label=label,
        created=created,
        synthetic=synthetic,
        data=data,
        notes=notes,
        inputs=MonteCarloInputs(
            trades_file=Path(trades_file).name,
            n_trades=n_trades,
            risk_pct=params.risk_pct,
            cost_r=cost_r,
            n_paths=params.n_paths,
            max_days=params.max_days,
            dd_days=params.dd_days,
            seed=params.seed,
            skip_prob=params.skip_prob,
            experimental_risk_steps=bool(params.risk_steps),
            venue=venue,
            rules=RulesModel(
                targets_pct=rules.targets_pct,
                daily_loss_limit_pct=rules.daily_loss_limit_pct,
                max_loss_pct=rules.max_loss_pct,
                min_trading_days=rules.min_trading_days,
                min_trading_days_assumed=rules.min_trading_days_assumed,
                max_loss_type=None if venue_profile is None else venue_profile.max_loss_type.value,
                daily_loss_basis=None
                if venue_profile is None
                else venue_profile.daily_loss_basis.value,
                daily_reset_tz=None if venue_profile is None else venue_profile.daily_reset.tz,
                daily_reset_time=None if venue_profile is None else venue_profile.daily_reset.time,
            ),
            intratrade=None if intratrade is None else intratrade.value,
            sum_r=sum_r,
            sum_mae_r=sum_mae_r,
            trades_digests=None if trades is None else trades_digests(trades),
        ),
        p_pass_phase=summary.p_pass_phase,
        p_breach=summary.p_breach,
        breach_shares=tuple(
            BreachShareModel(
                phase=share.phase,
                reason=reason_names[share.reason],
                probability=share.probability,
            )
            for share in summary.breach_shares
        ),
        p_undecided=summary.p_undecided,
        time_to_pass=tuple(_duration(stats) for stats in summary.time_to_pass),
        drawdown=DrawdownModel(
            median=summary.drawdown.median,
            q95=summary.drawdown.q95,
            q99=summary.drawdown.q99,
            es95=summary.drawdown.es95,
            es99=summary.drawdown.es99,
        ),
        drawdown_histogram=drawdown_histogram(outcomes.max_drawdown_pct, bins),
        provenance=None if provenance is None else _provenance_model(provenance),
    )


@_schema_errors
def build_artifact[T: ArtifactEnvelope](
    model: type[T],
    *,
    label: str,
    created: dt.datetime,
    synthetic: bool,
    data: BarSeries | DataInfo | None,
    notes: Iterable[str] = (),
    cost_free: bool = False,
    **payload: object,
) -> T:
    """Fill the envelope (kind, version, time, mandatory warnings) and validate.

    A series marked synthetic makes the artifact synthetic whatever the caller says, and
    a schema violation (a bug or an out-of-range number) becomes a Turkish `ArtifactError`.
    """
    info = data_info(data) if isinstance(data, BarSeries) else data
    synthetic = synthetic or (info is not None and info.synthetic)
    fields: dict[str, object] = {
        "kind": model.model_fields["kind"].default,
        "schema_version": SCHEMA_VERSION,
        "created_utc": utc_stamp(created),
        "label": label,
        "synthetic": synthetic,
        "data": info,
        "notes": with_warnings(notes, synthetic=synthetic, cost_free=cost_free),
        **payload,
    }
    return model.model_validate(fields)


def trade_rows_digest(trades: Iterable[Trade]) -> str:
    """sha256 of the rows, sorted: the same rows in any order give the same digest.

    One row is entry and exit (epoch ms), strategy id, R and mae_r (shortest round-trip
    repr, as the trade-list CSV writes them), so a list read back from its CSV has the
    digest of the run that wrote it, and a moved time, an edited value or a swapped row
    does not. A sorted hash, not a sum of row hashes: a sum of hashes can be matched by
    a chosen set of other rows (generalised birthday), a hash of the sorted rows cannot.
    """
    rows = sorted(
        f"{_epoch_ms(t.entry_utc)}|{_epoch_ms(t.exit_utc)}|{t.strategy_id or ''}|"
        f"{float(t.r)!r}|{None if t.mae_r is None else float(t.mae_r)!r}"
        for t in trades
    )
    return hashlib.sha256("\n".join(rows).encode("utf-8")).hexdigest()


def trades_digests(trades: Sequence[Trade]) -> dict[str, str]:
    """`trade_rows_digest` per strategy id ("" for rows without one)."""
    groups: dict[str, list[Trade]] = {}
    for trade in trades:
        groups.setdefault(trade.strategy_id or "", []).append(trade)
    return {sid: trade_rows_digest(rows) for sid, rows in sorted(groups.items())}


def _epoch_ms(moment: dt.datetime) -> int:
    return (moment - _EPOCH) // dt.timedelta(milliseconds=1)


# --- files -------------------------------------------------------------------------------


def artifact_file_name(artifact: AnyArtifact) -> str:
    created = dt.datetime.fromisoformat(artifact.created_utc)
    return f"{artifact.kind}_{artifact.label}_{created.strftime(_FILE_STAMP)}.json"


def write_artifact(artifact: AnyArtifact, reports_dir: Path) -> Path:
    """Write atomically; an existing file of the same name is never overwritten."""
    reports_dir.mkdir(parents=True, exist_ok=True)
    target = reports_dir / artifact_file_name(artifact)
    payload = artifact.model_dump_json(indent=2).encode("utf-8") + b"\n"
    if len(payload) > MAX_ARTIFACT_BYTES:
        raise ArtifactError(f"{target.name}: rapor çok büyük ({len(payload)} bayt)")
    fd, temp_name = tempfile.mkstemp(prefix=".tmp-", suffix=".json", dir=reports_dir)
    temp = Path(temp_name)
    try:
        with os.fdopen(fd, "wb") as handle:
            handle.write(payload)
            handle.flush()
            os.fsync(handle.fileno())
        # A hard link fails if the name exists, so creation and the no-overwrite check are
        # one atomic step (os.replace would silently overwrite).
        try:
            os.link(temp, target)
        except FileExistsError as exc:
            raise ArtifactError(f"{target.name}: dosya zaten var, üzerine yazılmadı") from exc
        except OSError:
            # Filesystems without hard links: checked replace (a race here needs two writers
            # of the same kind, label and second).
            if target.exists():
                raise ArtifactError(f"{target.name}: dosya zaten var, üzerine yazılmadı") from None
            os.replace(temp, target)
    finally:
        temp.unlink(missing_ok=True)
    _fsync_dir(reports_dir)
    return target


def load_artifact(path: Path) -> AnyArtifact:
    name = path.name
    text = _read_text(path)
    try:
        json.loads(
            text,
            parse_constant=_reject_constant,
            parse_int=_bounded_int,
            object_pairs_hook=_reject_duplicates,
        )
    except _JsonRefused as exc:
        raise ArtifactError(f"{name}: {exc}") from None
    except json.JSONDecodeError as exc:
        raise ArtifactError(
            f"{name}: geçerli JSON değil (satır {exc.lineno}, sütun {exc.colno}); dosya kesik ya "
            "da elle bozulmuş olabilir"
        ) from None
    try:
        return _ADAPTER.validate_json(text)
    except ValidationError as exc:
        raise ArtifactError(f"{name}: rapor şeması geçersiz: {_describe(exc)}") from None


def latest_artifacts(reports_dir: Path) -> LatestArtifacts:
    """The newest artifact per (kind, label), for the dashboard (not for Go/No-Go)."""
    scan = all_artifacts(reports_dir)
    newest: dict[tuple[str, str], tuple[dt.datetime, LoadedArtifact]] = {}
    for item in scan.artifacts:
        artifact = item.artifact
        key = (artifact.kind, artifact.label)
        created = dt.datetime.fromisoformat(artifact.created_utc)
        held = newest.get(key)
        if held is None or created > held[0]:
            newest[key] = (created, item)
    return LatestArtifacts(
        artifacts=tuple(newest[key][1] for key in sorted(newest)),
        problems=scan.problems,
    )


def all_artifacts(reports_dir: Path) -> AllArtifacts:
    """Every readable artifact among the newest `MAX_EXAMINED_FILES` file names.

    Same defensive reading as `latest_artifacts` (no symlinks, strict schema, the file
    name must follow from the content); an unreadable file is a problem, not a crash.
    """
    try:
        with os.scandir(reports_dir) as scan:
            candidates = []
            for entry in scan:
                match = _FILE_NAME.fullmatch(entry.name)
                if match is not None:
                    candidates.append((match.group("stamp"), entry.name, entry.is_symlink()))
    except (FileNotFoundError, NotADirectoryError):
        return AllArtifacts(artifacts=(), problems=(), truncated=False, examined=0, total=0)
    except OSError as exc:
        problem = ArtifactProblem(reports_dir.name, f"rapor dizini okunamadı: {exc.strerror}")
        return AllArtifacts(artifacts=(), problems=(problem,), truncated=False, examined=0, total=0)

    candidates.sort(reverse=True)
    examined = candidates[:MAX_EXAMINED_FILES]
    loaded: list[LoadedArtifact] = []
    problems: list[ArtifactProblem] = []
    for _stamp, file_name, is_link in examined:
        if is_link:
            problems.append(ArtifactProblem(file_name, "sembolik bağlantı: okunmadı"))
            continue
        try:
            artifact = load_artifact(reports_dir / file_name)
        except ArtifactError as exc:
            problems.append(ArtifactProblem(file_name, str(exc)))
            continue
        if artifact_file_name(artifact) != file_name:
            problems.append(
                ArtifactProblem(
                    file_name, f"{file_name}: dosya adı içerikle (tür/etiket/zaman) uyuşmuyor"
                )
            )
            continue
        loaded.append(LoadedArtifact(file_name, artifact))
    return AllArtifacts(
        artifacts=tuple(loaded),
        problems=tuple(sorted(problems, key=lambda p: p.file_name)),
        truncated=len(candidates) > len(examined),
        examined=len(examined),
        total=len(candidates),
    )


# --- helpers -----------------------------------------------------------------------------


def _provenance_model(provenance: TradesProvenance) -> TradesProvenanceModel:
    return TradesProvenanceModel(
        synthetic=provenance.synthetic,
        cost_free=provenance.cost_free,
        fill_model=provenance.fill_model,  # type: ignore[arg-type]  # validated by the model
        cost_factor=provenance.cost_factor,
        data_sha256=provenance.data_sha256,
        data_file=provenance.data_file,
        strategy_ids=provenance.strategy_ids,
        created_utc=provenance.created_utc,
    )


def _duration(stats: DurationStats | None) -> DurationModel | None:
    if stats is None:
        return None
    return DurationModel(
        n=stats.n,
        mean=stats.mean,
        p25=stats.p25,
        median=stats.median,
        p75=stats.p75,
        p90=stats.p90,
        p95=stats.p95,
    )


class _JsonRefused(ValueError):
    pass


def _reject_constant(token: str) -> object:
    raise _JsonRefused(f"JSON'da {token} olamaz (NaN/sonsuz değer)")


def _bounded_int(token: str) -> int:
    # A JSON integer has no upper bound and a float has: an integer beyond the float range
    # ends the readers' arithmetic (bar hours, a recorded risk) in an OverflowError, and
    # one past Python's own digit limit makes the decoder itself raise. No report holds
    # one (the largest real integers are epoch milliseconds), so such a file is damaged or
    # hand-edited and is refused here, as an unreadable file, never as a traceback.
    if len(token) > _MAX_INTEGER_DIGITS:
        raise _JsonRefused(f"tam sayı çok uzun (en çok {_MAX_INTEGER_DIGITS} basamak)")
    return int(token)


def _reject_duplicates(pairs: list[tuple[str, object]]) -> dict[str, object]:
    result: dict[str, object] = {}
    for key, value in pairs:
        if key in result:
            raise _JsonRefused(f"yinelenen alan: {key[:64]!r}")
        result[key] = value
    return result


def _read_text(path: Path) -> str:
    name = path.name
    too_large = f"{name}: dosya çok büyük (sınır {MAX_ARTIFACT_BYTES} bayt)"
    # O_NONBLOCK: a FIFO named like a report must not hang the reader; no effect on files.
    flags = os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0) | getattr(os, "O_NONBLOCK", 0)
    try:
        fd = os.open(path, flags)
    except FileNotFoundError:
        raise ArtifactError(f"{name}: dosya bulunamadı") from None
    except OSError as exc:
        raise ArtifactError(f"{name}: dosya açılamadı ({exc.strerror})") from None
    try:
        info = os.fstat(fd)
        if not stat.S_ISREG(info.st_mode):
            raise ArtifactError(f"{name}: normal bir dosya değil")
        if info.st_size > MAX_ARTIFACT_BYTES:
            raise ArtifactError(too_large)
        with os.fdopen(os.dup(fd), "rb") as handle:
            raw = handle.read(MAX_ARTIFACT_BYTES + 1)
    except OSError as exc:
        raise ArtifactError(f"{name}: dosya okunamadı ({exc.strerror})") from None
    finally:
        os.close(fd)
    if len(raw) > MAX_ARTIFACT_BYTES:
        raise ArtifactError(too_large)
    try:
        return raw.decode("utf-8")
    except UnicodeDecodeError:
        raise ArtifactError(f"{name}: UTF-8 değil") from None


def _describe(exc: ValidationError) -> str:
    errors = exc.errors(include_url=False)
    first = errors[0]
    where = ".".join(str(part) for part in first["loc"]) or "kök"
    kind = first["type"]
    if kind.startswith("propbot_"):
        what = first["msg"]
    elif kind.endswith("_type") or kind.endswith("_parsing"):
        what = "yanlış tür"
    else:
        what = _ERROR_TEXT.get(kind, "geçersiz değer")
    more = f" (+{len(errors) - 1} hata daha)" if len(errors) > 1 else ""
    return " ".join(f"{where}: {what}{more}".split())


def _fsync_dir(directory: Path) -> None:
    try:
        fd = os.open(directory, os.O_RDONLY)
    except OSError:
        return
    try:
        os.fsync(fd)
    except OSError:
        pass
    finally:
        os.close(fd)
