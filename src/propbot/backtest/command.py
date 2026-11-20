"""`propbot backtest`, `propbot parity`, `propbot gonogo` and the Monte Carlo artifact.

The sub-parsers live in `propbot.cli`; this module does the work (ADR-0008, SPEC §6, §9).
Every function takes its output streams and its clock from the caller, so a test can run
a whole command without the real time or a terminal. All output is Turkish.

Exit codes:
- backtest: 0 when the runs were made and written, whatever the strategies' results;
  1 for every refusal.
- parity: 0 when the comparison passed, 1 when it did not, 2 when it could not be made.
- gonogo: 0 ONLY on GO, 1 on NO-GO, 2 when the report could not be made.

ADR-0007: a run on synthetic bars says "SENTETİK VERİ" on the terminal and in every file
it writes; the artifact models refuse a synthetic artifact without the tag.

Risk (user decision of 2026-11-19, open finding 82): `backtest` sizes its runs at
config/risk.yaml's `risk_per_trade_pct`, the risk the account is planned at; `--pine-risk`
keeps the script's own value and makes a labelled comparison run. `parity` always keeps
the script's own value (TradingView is compared at the script's inputs). `gonogo` builds
both parameter sets from the same runner function the runs used. The planned risk is a
percent of the account's initial balance: with another `risk_base` in
config/strategies.yaml there is no decision run (`backtest` and `gonogo` say so and
stop), only the comparisons at the script's inputs.
"""

import argparse
import datetime as dt
import errno
import math
import re
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Final, TextIO

from propbot.backtest.artifacts import (
    PINE_RISK_SUFFIX,
    SYNTHETIC_TAG,
    AnyArtifact,
    ArtifactError,
    Scalar,
    all_artifacts,
    monte_carlo_artifact,
    utc_stamp,
    write_artifact,
)
from propbot.backtest.config import (
    BACKTEST_FILE_NAME,
    BacktestConfig,
    BacktestConfigError,
    load_backtest_config,
    unresolved_instruments,
)
from propbot.backtest.gonogo import (
    GATE_TITLES,
    STATUS_TEXT,
    GoNoGoError,
    GoNoGoInputs,
    evaluate,
    write_report,
)
from propbot.backtest.model import CostModel, FillModel
from propbot.backtest.montecarlo import (
    ChallengeRules,
    MonteCarloParams,
    MonteCarloSummary,
    PathOutcomes,
)
from propbot.backtest.parity import (
    ParityError,
    compare_trades,
    load_tradingview_trades,
    parity_artifact,
    render_parity_report,
    warmup_cutoff_ms,
)
from propbot.backtest.report import risk_shown
from propbot.backtest.runner import (
    DECISION_FACTOR,
    LegWalkForward,
    RunnerError,
    RunRequest,
    RunResult,
    all_artifacts_of,
    effective_params,
    merge_runs,
    render_summary,
    risk_base_note,
    run,
    run_cost_sensitivity,
    walk_forward_legs,
    write_trade_list,
)
from propbot.backtest.trades import IntratradeModel, Trade, TradeListError, TradesProvenance
from propbot.backtest.walkforward import WalkForwardConfig, WalkForwardError
from propbot.core.bars import BarSeries
from propbot.core.instrument import Instrument
from propbot.data.ohlcv import DataError, load_ohlcv
from propbot.risk.errors import ConfigError
from propbot.risk.loader import LoadedConfig, load_config_dir
from propbot.risk.venue import VenueProfile
from propbot.strategies.config import STRATEGIES_FILE_NAME, load_strategy_overrides
from propbot.strategies.registry import STRATEGIES, StrategyError, StrategySpec

EXIT_OK: Final = 0
EXIT_FAILED: Final = 1
EXIT_ERROR: Final = 2
ALL_STRATEGIES: Final = "all"
DEFAULT_COMPLIANCE_DIR: Final = "docs/compliance"

type Clock = Callable[[], dt.datetime]

_LABEL_CHARS: Final = re.compile(r"[^a-z0-9_-]+")
_LABEL_MAX: Final = 64
# One gate line on the terminal; the Markdown report has the whole evidence.
_EVIDENCE_SHOWN: Final = 110
_SYNTHETIC_FILE: Final = re.compile(r"synthetic|sentetik", re.IGNORECASE)
_SYNTHETIC_FOOTER: Final = (
    f"!!! {SYNTHETIC_TAG}: bu sonuçlar hiçbir kenarı, pariteyi ya da veri kalitesini "
    "kanıtlamaz ve Go/No-Go'da hiçbir kapıyı geçemez (ADR-0007)."
)
# What `montecarlo` says of a trade list whose ids carry the mark of a run at the script's
# own risk: its terminal report and its artifact, in the words of the command that wrote
# the list and of the G4 refusal.
PINE_RISK_LIST_NOTE: Final = (
    "betiğin kendi riskiyle (--pine-risk) üretilmiş: planlanan hesabın işlemleri değildir, bu "
    "koşu Go/No-Go G4'ü GEÇTİ yapamaz"
)


class BacktestCommandError(Exception):
    """The command cannot run as asked. The message is user-facing (Turkish)."""


# Errors a user can cause (a wrong path, a VERIFY profile, a bad date): one Turkish line
# and an exit code, never a traceback.
USER_ERRORS: Final = (
    ConfigError,
    BacktestConfigError,
    BacktestCommandError,
    RunnerError,
    DataError,
    ParityError,
    ArtifactError,
    GoNoGoError,
    StrategyError,
    TradeListError,
    WalkForwardError,
)


def print_error(exc: BaseException, err: TextIO) -> None:
    """A config error lists every issue; everything else is one `HATA:` line."""
    if isinstance(exc, ConfigError):
        print(f"HATA: config geçersiz ({len(exc.issues)} sorun)", file=err)
        for issue in exc.issues:
            print(f"  - {issue}", file=err)
        return
    print(f"HATA: {exc}", file=err)


# The operating system's own wording (strerror) is English: the cases a user can cause
# get a Turkish phrase, everything else at least its errno name.
_OS_ERRORS: Final = {
    errno.ENOENT: "dosya ya da dizin yok",
    errno.EACCES: "erişim izni yok",
    errno.EPERM: "işlem izni yok",
    errno.EEXIST: "aynı adda bir dosya var",
    errno.ENOTDIR: "yolun bir parçası dizin değil",
    errno.EISDIR: "bu bir dizin",
    errno.ENOSPC: "diskte yer kalmadı",
    errno.EDQUOT: "disk kotası doldu",
    errno.EROFS: "dosya sistemi salt okunur",
    errno.ENAMETOOLONG: "yol çok uzun",
}


def os_error_text(exc: OSError) -> str:
    """A Turkish phrase for an operating-system refusal (never its English strerror)."""
    if exc.errno is not None and exc.errno in _OS_ERRORS:
        return _OS_ERRORS[exc.errno]
    name = errno.errorcode.get(exc.errno, type(exc).__name__) if exc.errno else type(exc).__name__
    return f"işletim sistemi hatası ({name})"


def check_reports_dir(path: Path) -> None:
    """Refuse a reports folder that cannot be one, before any work is done.

    Otherwise a run of several minutes ends in a write error and its results are lost.
    """
    if path.exists() and not path.is_dir():
        raise BacktestCommandError(
            f"--reports-dir {path}: dizin değil (aynı adda bir dosya var); rapor klasörü ver"
        )


def label_from(text: str) -> str:
    """An artifact label (`[a-z0-9_-]{1,64}`) from a file stem or a name."""
    label = _LABEL_CHARS.sub("-", text.lower()).strip("-")[:_LABEL_MAX].strip("-")
    return label or "rapor"


# --- shared loading ----------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class _Setup:
    config: BacktestConfig
    instrument: Instrument
    costs: CostModel
    venue_name: str
    venue: VenueProfile
    overrides: Mapping[str, Mapping[str, object]]
    planned_risk_pct: float  # config/risk.yaml risk_per_trade_pct


def pick_venue(config: LoadedConfig, name: str | None) -> tuple[str, VenueProfile]:
    """Chosen like `serve` and `dashboard` do: by name, or the only profile there is."""
    names = ", ".join(sorted(config.venues))
    if name is None:
        if len(config.venues) != 1:
            raise BacktestCommandError(f"birden çok venue profili var; --venue ile seç: {names}")
        return next(iter(config.venues.items()))
    venue = config.venues.get(name)
    if venue is None:
        raise BacktestCommandError(f"venue profili bulunamadı: {name!r}; mevcut profiller: {names}")
    return name, venue


def _load_setup(args: argparse.Namespace) -> _Setup:
    config_dir = Path(args.config_dir)
    loaded = load_config_dir(config_dir)
    venue_name, venue = pick_venue(loaded, args.venue)
    config = load_backtest_config(config_dir / BACKTEST_FILE_NAME)
    # Both refuse a profile with VERIFY nulls, with the names of the empty values.
    instrument = config.instrument(args.instrument)
    costs = config.cost_model(args.instrument)
    overrides = load_strategy_overrides(config_dir / STRATEGIES_FILE_NAME)
    return _Setup(
        config, instrument, costs, venue_name, venue, overrides, loaded.risk.risk_per_trade_pct
    )


def _load_series(args: argparse.Namespace) -> BarSeries:
    return load_ohlcv(Path(args.data), symbol=args.symbol, tf=args.tf, bar_tz=args.bar_tz)


def _parse_day(text: str, option: str) -> dt.date:
    try:
        return dt.date.fromisoformat(text)
    except ValueError:
        raise BacktestCommandError(
            f"{option} YYYY-AA-GG biçiminde olmalı; okunan: {text!r}"
        ) from None


def _oos_start_ms(text: str | None, config: BacktestConfig) -> int:
    """--oos-start (UTC midnight of the date) or config/backtest.yaml's samples.oos_start."""
    if text is None:
        moment = config.samples.oos_start_utc
    else:
        day = _parse_day(text, "--oos-start")
        moment = dt.datetime.combine(day, dt.time(), tzinfo=dt.UTC)
    return int(moment.timestamp()) * 1000


def _strategy_ids(text: str) -> tuple[str, ...]:
    if text == ALL_STRATEGIES:
        # The stand-alone ports only: the Master portfolio's legs repeat two of them.
        return tuple(sid for sid, spec in STRATEGIES.items() if len(spec.leg_ids) == 1)
    if text not in STRATEGIES:
        known = ", ".join(sorted(STRATEGIES))
        raise BacktestCommandError(
            f"bilinmeyen strateji: {text} (tanımlılar: {known}; ya da '{ALL_STRATEGIES}')"
        )
    return (text,)


def _overrides_text(overrides: Mapping[str, object]) -> str:
    return ", ".join(f"{name}={value}" for name, value in sorted(overrides.items()))


def _refuse_existing(path: Path, option: str) -> None:
    if path.is_dir():
        raise BacktestCommandError(f"{option} {path}: bu bir dizin; dosya yolu ver")
    if path.exists() or path.is_symlink():
        raise BacktestCommandError(
            f"{option} {path}: dosya zaten var, üzerine yazılmaz; başka bir yol ver ya da dosyayı "
            "kendin sil"
        )


def _write_all(artifacts: Sequence[AnyArtifact], reports_dir: Path, out: TextIO) -> None:
    for artifact in artifacts:
        try:
            path = write_artifact(artifact, reports_dir)
        except OSError as exc:
            raise BacktestCommandError(
                f"rapor yazılamadı ({reports_dir}): {os_error_text(exc)}"
            ) from exc
        print(f"Rapor yazıldı: {path}", file=out)


# --- backtest ----------------------------------------------------------------------------


def run_backtest_command(
    args: argparse.Namespace, out: TextIO, err: TextIO, *, clock: Clock
) -> int:
    try:
        return _backtest(args, out, clock)
    except USER_ERRORS as exc:
        print_error(exc, err)
        return EXIT_FAILED


def _backtest(args: argparse.Namespace, out: TextIO, clock: Clock) -> int:
    ids = _strategy_ids(args.strategy)
    factor: float = DECISION_FACTOR if args.cost_factor is None else args.cost_factor
    if not (math.isfinite(factor) and factor >= 0.0):
        raise BacktestCommandError(
            f"--cost-factor sonlu ve negatif olmayan bir sayı olmalı: {factor}"
        )
    if args.matrix and (args.cost_free or factor != DECISION_FACTOR):
        raise BacktestCommandError(
            "--matrix yalnızca maliyetli ve çarpanı 1,0 olan koşuyla kullanılır (--cost-free ve "
            "--cost-factor ile birlikte verilemez)"
        )
    trades_out = None if args.trades_out is None else Path(args.trades_out)
    if trades_out is not None:
        _refuse_existing(trades_out, "--trades-out")
    if not args.no_artifact:
        check_reports_dir(Path(args.reports_dir))
    setup = _load_setup(args)
    oos_start_ms = _oos_start_ms(args.oos_start, setup.config)
    series = _load_series(args)
    fill_model = FillModel(args.fill_model)
    wf_config = setup.config.walk_forward
    created = clock()

    results: list[RunResult] = []
    artifacts: list[AnyArtifact] = []
    # Whether everything other than the risk describes a decision run: only then may a
    # note about --pine-risk call the run one. An instrument profile without costs makes
    # every run a cost-free comparison run, whatever the options say.
    otherwise_decision = (
        fill_model is FillModel.PESSIMISTIC
        and not args.cost_free
        and not setup.costs.is_free
        and factor == DECISION_FACTOR
        and oos_start_ms == _oos_start_ms(None, setup.config)
    )
    for sid in ids:
        overrides = setup.overrides.get(sid, {})
        planned_risk_pct = _run_risk(
            args.pine_risk,
            setup.planned_risk_pct,
            STRATEGIES[sid],
            overrides,
            out,
            otherwise_decision=otherwise_decision,
        )
        if overrides:
            print(
                f"!!! NOT: {sid} config/{STRATEGIES_FILE_NAME} değişiklikleriyle koşuyor "
                f"(Pine varsayılanları değil): {_overrides_text(overrides)}",
                file=out,
            )
        request = RunRequest(
            series=series,
            strategy_id=sid,
            instrument=setup.instrument,
            base_costs=setup.costs,
            initial_capital=float(setup.venue.initial_balance),
            oos_start_ms=oos_start_ms,
            overrides=overrides,
            min_gap_seconds=setup.config.session.min_gap_minutes * 60,
            fill_model=fill_model,
            cost_factor=factor,
            cost_free=args.cost_free,
            # Known here, so a run on another boundary is labelled and noted (review A).
            configured_oos_start_ms=_oos_start_ms(None, setup.config),
            planned_risk_pct=planned_risk_pct,
        )
        result = run(request)
        sensitivity = (
            run_cost_sensitivity(result, setup.config.cost_factors) if args.matrix else None
        )
        folds: tuple[LegWalkForward, ...] = ()
        if args.walk_forward:
            folds = walk_forward_legs(
                result,
                WalkForwardConfig(
                    is_days=wf_config.is_days,
                    oos_days=wf_config.oos_days,
                    step_days=wf_config.step_days,
                ),
            )
        out.write(render_summary(result, sensitivity=sensitivity, walk_forward_results=folds))
        print(file=out)
        results.append(result)
        artifacts.extend(
            all_artifacts_of(
                result, created=created, sensitivity=sensitivity, walk_forward_results=folds
            )
        )

    print(f"Venue: {setup.venue_name} (başlangıç sermayesi = initial_balance)", file=out)
    if fill_model is FillModel.TV_OHLC and any(
        result.params[result.spec.risk_param] != result.spec.pine_risk_pct for result in results
    ):
        # TradingView runs the script at the script's own risk: a tv_ohlc run sized at the
        # planned risk is not what its Strategy Tester shows, and nobody should find that
        # out from a list of mismatches. Asked of the runs, not of the flag: at a planned
        # risk equal to the script's the sizes are TradingView's and the hint would send
        # the user to a flag that changes nothing.
        print(
            "NOT: bu tv_ohlc koşusu planlanan riskle (config/risk.yaml) boyutlandırıldı; "
            "TradingView ile aynı girdilerde karşılaştırma için --pine-risk ekleyin (propbot "
            "parity her zaman betiğin kendi riskini kullanır).",
            file=out,
        )
    if args.no_artifact:
        print("Rapor yazılmadı (--no-artifact).", file=out)
    else:
        _write_all(artifacts, Path(args.reports_dir), out)
    if trades_out is not None:
        _write_trades(results, trades_out, created, out)
    if series.source.synthetic:
        print(_SYNTHETIC_FOOTER, file=out)
    return EXIT_OK


def _run_risk(
    pine_risk: bool,
    planned_risk_pct: float,
    spec: StrategySpec[Any],
    overrides: Mapping[str, object],
    out: TextIO,
    *,
    otherwise_decision: bool,
) -> float | None:
    """The risk a `backtest` run is sized at: the planned one, or None for the script's own.

    The decision run is sized at the planned risk; --pine-risk keeps the script's own
    (None), which the runner labels and reports as a comparison run. When the two risks
    are the same number there is nothing to compare: the run would record exactly what a
    decision run records, and Go/No-Go reads recorded fields, never labels. Writing
    "Go/No-Go will not use this run" on it would be untrue, so it is made as what it is.
    Not on another risk base: there the same percent is another risk, a decision run
    does not exist, and the run stays the comparison that was asked for.

    `otherwise_decision`: no other option (fill model, costs, OOS start) makes the run a
    comparison run. When one does, the note speaks of the risk only: the same output
    must not say "made as the decision run" and "not the decision run".
    """
    if not pine_risk:
        return planned_risk_pct
    if planned_risk_pct == spec.pine_risk_pct and spec.sized_on_account(overrides):
        made_as = (
            "karşılaştırılacak fark yok, koşu karar koşusu olarak yapılıyor."
            if otherwise_decision
            else "risk bakımından karşılaştırılacak fark yok, koşu planlanan riskle yapılıyor ve "
            f"'{PINE_RISK_SUFFIX}' etiketi almıyor."
        )
        print(
            f"NOT: --pine-risk: {spec.strategy_id} betiğinin kendi riski planlanan riskle aynı "
            f"({risk_shown(planned_risk_pct)}); {made_as}",
            file=out,
        )
        return planned_risk_pct
    return None


def _write_trades(
    results: Sequence[RunResult], path: Path, created: dt.datetime, out: TextIO
) -> None:
    usable: list[RunResult] = []
    for result in results:
        # The reader refuses a provenance line naming a leg without a row, so a run with
        # an empty leg cannot be in the list; it is left out loudly, never silently.
        empty = [leg.strategy_id for leg in result.legs if not any(e.complete for e in leg.entries)]
        if empty:
            print(
                f"NOT: {result.spec.strategy_id} işlem listesine alınmadı: tamamlanmış işlemi "
                f"olmayan bacak: {', '.join(empty)}",
                file=out,
            )
            continue
        usable.append(result)
    if not usable:
        raise BacktestCommandError(
            f"--trades-out {path}: yazılmadı, hiçbir stratejinin tamamlanmış işlemi yok"
        )
    merged = merge_runs(usable, created=created)
    if any(result.request.pine_risk for result in usable):
        # The list is written (it is what was asked for), but nobody should simulate the
        # account on it by mistake: its rows are sized at the script's risk. The runner
        # marks their strategy ids, which is what makes the second half of the note true
        # even when the rows equal the decision run's.
        print(
            "NOT: işlem listesi betiğin kendi riskiyle (--pine-risk) üretildi: planlanan "
            "hesabın işlemleri değildir, Go/No-Go G4 bu listeyle GEÇTİ olamaz (satır kimlikleri "
            f"'{PINE_RISK_SUFFIX}' taşır)",
            file=out,
        )
    _refuse_existing(path, "--trades-out")
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        rows = write_trade_list(merged, path)
    except OSError as exc:
        raise BacktestCommandError(
            f"--trades-out {path}: yazılamadı ({os_error_text(exc)})"
        ) from exc
    origin = provenance_text(merged.provenance)
    print(f"İşlem listesi yazıldı: {path} ({rows} işlem; köken: {origin})", file=out)


# --- parity ------------------------------------------------------------------------------


def run_parity_command(args: argparse.Namespace, out: TextIO, err: TextIO, *, clock: Clock) -> int:
    try:
        return _parity(args, out, clock)
    except USER_ERRORS as exc:
        print_error(exc, err)
        return EXIT_ERROR


def _parity(args: argparse.Namespace, out: TextIO, clock: Clock) -> int:
    if args.strategy == ALL_STRATEGIES:
        raise BacktestCommandError("parite tek bir strateji için ölçülür; --strategy <kimlik> ver")
    (sid,) = _strategy_ids(args.strategy)
    if args.warmup_bars < 0:
        raise BacktestCommandError("--warmup-bars negatif olamaz")
    if not args.no_artifact:
        check_reports_dir(Path(args.reports_dir))
    setup = _load_setup(args)
    series = _load_series(args)
    tv_trades = load_tradingview_trades(Path(args.tv_trades), tz=args.tv_tz)
    overrides = setup.overrides.get(sid, {})
    if overrides:
        print(
            f"!!! NOT: {sid} config/{STRATEGIES_FILE_NAME} değişiklikleriyle koşuyor: "
            f"{_overrides_text(overrides)}; TradingView'da da aynısı ayarlı olmalı",
            file=out,
        )
    result = run(
        RunRequest(
            series=series,
            strategy_id=sid,
            instrument=setup.instrument,
            base_costs=setup.costs,
            initial_capital=float(setup.venue.initial_balance),
            # The OOS split plays no part in a parity comparison.
            oos_start_ms=_oos_start_ms(None, setup.config),
            overrides=overrides,
            min_gap_seconds=setup.config.session.min_gap_minutes * 60,
            fill_model=FillModel.TV_OHLC,
            # No planned risk, always: TradingView runs the script at the script's own
            # inputs, so the Python side must too (user decision of 2026-11-19).
            planned_risk_pct=None,
        )
    )
    risk_note = _parity_risk_line(result)
    cutoff = warmup_cutoff_ms([bar.ts_open_ms for bar in series.bars], args.warmup_bars)
    comparison = compare_trades(
        result.engine.trades,
        tv_trades,
        fill_model=FillModel.TV_OHLC,
        tick_size=setup.instrument.tick_size,
        tf_seconds=series.tf_seconds,
        threshold=setup.config.go_no_go.min_parity_rate,
        warmup_cutoff_ms=cutoff,
    )
    if series.source.synthetic:
        print(
            f"!!! {SYNTHETIC_TAG}: barlar sentetik; bu karşılaştırma pariteyi kanıtlamaz", file=out
        )
    print(risk_note, file=out)
    out.write(render_parity_report(comparison, strategy_id=sid))
    if args.no_artifact:
        print("Rapor yazılmadı (--no-artifact).", file=out)
    else:
        artifact = parity_artifact(
            comparison,
            label=label_from(sid),
            created=clock(),
            strategy_id=sid,
            data=series,
            synthetic=series.source.synthetic,
            python_source=series.source.name,
            notes=(
                f"Python tarafı: {sid}, dolum modeli tv_ohlc, venue {setup.venue_name}.",
                risk_note,
            ),
            # G6 ties the report to today's configured strategy and instrument (review E).
            params=result.params,
            instrument=setup.instrument,
            # Both change which trades the port makes (gate attack K, L).
            min_gap_seconds=result.request.min_gap_seconds,
            initial_capital=result.request.initial_capital,
        )
        _write_all((artifact,), Path(args.reports_dir), out)
    verdict = "GEÇTİ" if comparison.passed else "GEÇMEDİ"
    print(f"Parite: {verdict}", file=out)
    return EXIT_OK if comparison.passed else EXIT_FAILED


def _parity_risk_line(result: RunResult) -> str:
    """The one line saying which per-trade risk a parity run used, and why not the planned."""
    spec = result.spec
    used = result.params[spec.risk_param]
    assert isinstance(used, float)
    return (
        f"İşlem başı risk: {risk_shown(used)} — betiğin kendi değeri ({spec.risk_param}); parite "
        "TradingView ile betiğin girdilerinde karşılaştırılır, config/risk.yaml'daki planlanan "
        "risk burada kullanılmaz "
        # The user decided which risk holds; running parity at the script's own risk is
        # how that decision was implemented (ADR-0010), and the line says so.
        "(2026-11-19 kullanıcı kararının uygulaması, ADR-0010)"
        # On another risk base the percent above is not a percent of the account: a
        # clause of this sentence, so the full stop comes after it.
        + risk_base_note(spec, result.params)
        + "."
    )


# --- Monte Carlo ---------------------------------------------------------------------------


def provenance_text(provenance: TradesProvenance) -> str:
    """One Turkish phrase naming the backtest a trade list came from."""
    parts = [
        "propbot backtest",
        "stratejiler " + ", ".join(provenance.strategy_ids),
        f"veri {provenance.data_file}"
        + (f" (sha256 {provenance.data_sha256[:12]}…)" if provenance.data_sha256 else ""),
        f"dolum {provenance.fill_model}",
        f"maliyet çarpanı {provenance.cost_factor:g}".replace(".", ","),
        provenance.created_utc,
    ]
    if provenance.cost_free:
        parts.append("MALİYETSİZ KOŞU")
    if provenance.synthetic:
        parts.append(SYNTHETIC_TAG)
    return " · ".join(parts)


def trades_synthetic(trades_path: Path, provenance: TradesProvenance | None) -> bool:
    """Synthetic by the provenance line, or by a file name that says so (no line)."""
    if provenance is not None and provenance.synthetic:
        return True
    return _SYNTHETIC_FILE.search(trades_path.name) is not None


def trades_at_pine_risk(provenance: TradesProvenance | None) -> bool:
    """The list was written by a run at the script's own risk (`runner.trade_list_id`)."""
    return provenance is not None and any(
        sid.endswith(PINE_RISK_SUFFIX) for sid in provenance.strategy_ids
    )


def origin_line(trades_path: Path, provenance: TradesProvenance | None) -> str:
    """The report line saying where the trade list came from."""
    head = "Köken       : "
    if provenance is not None:
        text = provenance_text(provenance)
        if trades_at_pine_risk(provenance):
            # The simulation below runs at the planned risk on trades sized at another:
            # said where the result is read, not only by a suffix in the ids.
            text += f" → liste {PINE_RISK_LIST_NOTE}"
        return head + text
    text = (
        "bilinmiyor (dosyada köken satırı yok; listeyi propbot backtest --trades-out üretmeli) "
        "→ bu koşu Go/No-Go G4'ü GEÇTİ yapamaz"
    )
    if trades_synthetic(trades_path, None):
        text += f" · dosya adı sentetik: {SYNTHETIC_TAG}"
    return head + text


def with_origin_line(report: str, line: str) -> str:
    """Insert the origin line right after the input block (before the venue line)."""
    lines = report.split("\n")
    for index, text in enumerate(lines):
        if text.startswith("Venue       :"):
            return "\n".join([*lines[:index], line, *lines[index:]])
    return report.rstrip("\n") + "\n" + line + "\n"


@dataclass(frozen=True, slots=True)
class MonteCarloRun:
    """What the existing `montecarlo` command computed, for its artifact."""

    trades_path: Path
    venue_path: Path
    n_trades: int
    cost_r: float
    r_is_net: bool
    params: MonteCarloParams
    rules: ChallengeRules
    summary: MonteCarloSummary
    outcomes: PathOutcomes
    provenance: TradesProvenance | None
    # Recorded for Go/No-Go G4 (review B, C, F); left out, the artifact says "unknown"
    # and G4 cannot pass on it.
    venue: VenueProfile | None = None
    intratrade: IntratradeModel | None = None
    trades: Sequence[Trade] | None = None


def trades_cost_free(provenance: TradesProvenance | None) -> bool:
    """The list's provenance line says its run had no costs: it wins over --r-is-net."""
    return provenance is not None and provenance.cost_free


def write_monte_carlo_artifact(
    mc: MonteCarloRun, reports_dir: Path, *, created: dt.datetime, out: TextIO
) -> Path:
    venue_name = mc.venue_path.stem
    notes = []
    if mc.provenance is None:
        notes.append(
            "İşlem listesinin kaynağı bilinmiyor (köken satırı yok): bu koşu Go/No-Go G4'ü "
            "GEÇTİ yapamaz; listeyi propbot backtest --trades-out üretmeli."
        )
    else:
        notes.append(f"İşlem listesinin kökeni: {provenance_text(mc.provenance)}.")
    if trades_at_pine_risk(mc.provenance):
        notes.append(f"İşlem listesi {PINE_RISK_LIST_NOTE}.")
    if trades_cost_free(mc.provenance):
        notes.append(
            "MALİYETSİZ işlem listesi (köken satırı): R değerleri maliyet içermez; "
            "--r-is-net yok sayıldı."
        )
    elif mc.r_is_net:
        notes.append("R değerleri maliyet dahil (kullanıcı beyanı, --r-is-net).")
    artifact = monte_carlo_artifact(
        mc.summary,
        mc.outcomes,
        label=label_from(f"{venue_name}-{mc.trades_path.stem}"),
        created=created,
        params=mc.params,
        rules=mc.rules,
        trades_file=mc.trades_path.name,
        n_trades=mc.n_trades,
        cost_r=mc.cost_r,
        venue=venue_name,
        synthetic=trades_synthetic(mc.trades_path, mc.provenance),
        notes=notes,
        provenance=mc.provenance,
        venue_profile=mc.venue,
        intratrade=mc.intratrade,
        trades=mc.trades,
    )
    try:
        path = write_artifact(artifact, reports_dir)
    except OSError as exc:
        raise BacktestCommandError(
            f"rapor yazılamadı ({reports_dir}): {os_error_text(exc)}"
        ) from exc
    print(f"Rapor yazıldı: {path}", file=out)
    return path


# --- Go/No-Go ------------------------------------------------------------------------------


def run_gonogo_command(args: argparse.Namespace, out: TextIO, err: TextIO, *, clock: Clock) -> int:
    try:
        return _gonogo(args, out, clock)
    except USER_ERRORS as exc:
        print_error(exc, err)
    except BrokenPipeError:
        # The terminal went away (`… | head`); not a report error: `main` ends quietly.
        raise
    except OSError as exc:
        print(f"HATA: rapor yazılamadı: {os_error_text(exc)}", file=err)
    return EXIT_ERROR


def _judgeable_ids() -> dict[str, StrategySpec[Any]]:
    ids: dict[str, StrategySpec[Any]] = {}
    for spec in STRATEGIES.values():
        ids[spec.strategy_id] = spec
        for leg in spec.leg_ids:
            ids.setdefault(leg, spec)
    return ids


def _parse_strategies(text: str) -> tuple[str, ...]:
    known = _judgeable_ids()
    chosen: list[str] = []
    for part in text.split(","):
        sid = part.strip()
        if not sid:
            raise BacktestCommandError(
                f"--strategies virgülle ayrılmış strateji kimlikleri olmalı; okunan: {text!r}"
            )
        if sid not in known:
            raise BacktestCommandError(
                f"bilinmeyen strateji: {sid} (değerlendirilebilenler: {', '.join(sorted(known))})"
            )
        if sid not in chosen:
            chosen.append(sid)
    for sid in chosen:
        script = known[sid].strategy_id
        if sid != script and script in chosen:
            # Both views are accepted, one at a time (user decision of 2026-11-19, open
            # finding 72): together the leg's trades would be in two judged runs and G4
            # would compare the trade list with them twice.
            raise BacktestCommandError(
                f"--strategies: {script} ile bacağı {sid} aynı raporda değerlendirilemez (aynı "
                f"işlemler iki kez sayılır); ya birleşik kimliği ({script}) ya da bacak "
                # Ids are joined the way they are typed (no space): the text in the
                # parentheses can be pasted after --strategies as it is.
                f"kimliklerini ({','.join(known[sid].leg_ids)}) verin"
            )
    for sid in chosen:
        spec = known[sid]
        missing = [leg for leg in spec.leg_ids if leg not in chosen]
        if sid != spec.strategy_id and missing:
            # A leg alone is refused here instead of being left to G4 (user decision of
            # 2026-11-20, open finding 87): the script's trade list always carries all its
            # legs, so G4 could never pass (at best NOT_EVALUABLE), after a report nobody
            # can use.
            raise BacktestCommandError(
                f"--strategies: {sid} kimliği {spec.strategy_id} betiğinin bir bacağıdır ve tek "
                f"başına değerlendirilemez (eksik bacak: {', '.join(missing)}; betiğin işlem "
                "listesi bütün bacakları taşır, G4 tek bacakla bağlanamaz); ya bütün bacakları "
                f"({','.join(spec.leg_ids)}) ya da birleşik kimliği ({spec.strategy_id}) verin"
                + _standalone_hint(sid)
            )
    return tuple(chosen)


def _standalone_hint(leg_id: str) -> str:
    """Where to look when the stand-alone strategy was meant, not the portfolio's leg.

    A leg id ends with the id of the stand-alone port of the same engine
    (master_ny_orb_fvg / ny_orb_fvg). That is a naming habit, not a rule, so the hint
    names one id only when exactly one fits and lists them all otherwise; with no
    stand-alone strategy at all there is nothing to point at.
    """
    alone = sorted(sid for sid, spec in STRATEGIES.items() if spec.leg_ids == (sid,))
    if not alone:
        return ""
    fits = [sid for sid in alone if leg_id.endswith("_" + sid)]
    if len(fits) == 1:
        return f". Aynı motorun bağımsız stratejisini değerlendirmek için: {fits[0]}"
    return f". Bağımsız stratejiler: {', '.join(alone)}"


def _compliance_files(directory: Path) -> tuple[str, ...] | None:
    """The file names in `directory`; None when it is not a directory (say so, review J)."""
    try:
        return tuple(
            sorted(
                entry.name
                for entry in directory.iterdir()
                if entry.is_file() and not entry.name.startswith(".")
            )
        )
    except (FileNotFoundError, NotADirectoryError):
        return None


def _expected(config: BacktestConfig) -> tuple[dict[str, CostModel], dict[str, Instrument]]:
    """The costs and instruments of every profile without VERIFY nulls, by symbol."""
    costs: dict[str, CostModel] = {}
    instruments: dict[str, Instrument] = {}
    for name, profile in config.instruments.items():
        if profile.unresolved_fields():
            continue
        instruments[name] = config.instrument(name)
        costs[name] = config.cost_model(name)
    return costs, instruments


def expected_run_params(
    strategy_ids: Sequence[str],
    overrides: Mapping[str, Mapping[str, object]],
    planned_risk_pct: float | None,
) -> dict[str, Mapping[str, Scalar]]:
    """What a run of each judged id must have recorded, from strategies.yaml today.

    With the planned risk (config/risk.yaml): the parameters of a decision run. With
    None: those of a run at the script's own risk, which is what a parity report records.
    Both come from `runner.effective_params`, the function the runs themselves record
    with, so the expectation cannot drift from what a run writes.

    A Master leg's artifact records the master_portfolio spec's parameters (the runner
    writes one parameter set per run), so a leg id is looked up through its spec.
    """
    known = _judgeable_ids()
    return {
        sid: effective_params(
            known[sid],
            overrides.get(known[sid].strategy_id, {}),
            planned_risk_pct=planned_risk_pct,
        )
        for sid in strategy_ids
    }


def _gonogo(args: argparse.Namespace, out: TextIO, clock: Clock) -> int:
    strategy_ids = _parse_strategies(args.strategies)
    check_reports_dir(Path(args.reports_dir))
    config_dir = Path(args.config_dir)
    loaded = load_config_dir(config_dir)
    venue_name, venue = pick_venue(loaded, args.venue)
    config = load_backtest_config(config_dir / BACKTEST_FILE_NAME)
    costs, instruments = _expected(config)
    overrides = load_strategy_overrides(config_dir / STRATEGIES_FILE_NAME)
    planned_risk_pct = loaded.risk.risk_per_trade_pct
    params = expected_run_params(strategy_ids, overrides, planned_risk_pct)
    parity_params = expected_run_params(strategy_ids, overrides, None)
    known = _judgeable_ids()
    reports_dir = Path(args.reports_dir)
    scan = all_artifacts(reports_dir)
    # A relative default is read from the working directory: the report names the
    # directory it actually listed, so "no files" is never said of a folder not found.
    compliance_dir = Path(args.compliance_dir).absolute()
    compliance = _compliance_files(compliance_dir)
    created = clock()
    inputs = GoNoGoInputs(
        artifacts=scan.artifacts,
        criteria=config.go_no_go,
        venue_name=venue_name,
        venue=venue,
        strategy_ids=strategy_ids,
        planned_risk_pct=planned_risk_pct,
        today=created.astimezone(dt.UTC).date(),
        created=created,
        compliance_files=() if compliance is None else compliance,
        compliance_dir=str(compliance_dir),
        compliance_dir_found=compliance is not None,
        problems=scan.problems,
        expected_costs=costs,
        artifacts_truncated=scan.truncated,
        expected_instruments=instruments,
        expected_params=params,
        expected_parity_params=parity_params,
        risk_params={sid: known[sid].risk_param for sid in strategy_ids},
        script_ids={sid: known[sid].strategy_id for sid in strategy_ids},
        expected_oos_start_utc=utc_stamp(config.samples.oos_start_utc),
        expected_min_gap_seconds=config.session.min_gap_minutes * 60,
    )
    artifact = evaluate(inputs)
    markdown_path, json_path = write_report(artifact, reports_dir)

    verdict = "GO" if artifact.overall == "GO" else "NO-GO"
    print(
        f"Go/No-Go · venue {venue_name} · stratejiler: {', '.join(strategy_ids)}",
        file=out,
    )
    if artifact.synthetic:
        print(
            f"!!! {SYNTHETIC_TAG}: girdilerden en az biri sentetik; bu rapor GO veremez (ADR-0007)",
            file=out,
        )
    print(f"Karar: {verdict}", file=out)
    width = max(len(title) for title in GATE_TITLES.values())
    for gate in artifact.gates:
        first = gate.evidence.splitlines()[0] if gate.evidence else ""
        if len(first) > _EVIDENCE_SHOWN:
            first = first[: _EVIDENCE_SHOWN - 1] + "…"
        print(
            f"  {gate.id}  {gate.title:<{width}}  {STATUS_TEXT[gate.status]:<18}  {first}",
            file=out,
        )
    print(f"Rapor yazıldı: {markdown_path}", file=out)
    print(f"Rapor yazıldı: {json_path}", file=out)
    return EXIT_OK if artifact.overall == "GO" else EXIT_FAILED


# --- config check ------------------------------------------------------------------------


def check_backtest_files(config_dir: Path, out: TextIO) -> bool:
    """`config check` lines for backtest.yaml and strategies.yaml; False when one is invalid.

    Both are optional here, like tv_alerts.yaml: only the Faz 2 commands need them, and
    those refuse to run without them. VERIFY instruments and overrides are notes.
    """
    valid = True
    path = config_dir / BACKTEST_FILE_NAME
    if not path.exists():
        print(
            f"{BACKTEST_FILE_NAME}: yok (yalnızca `propbot backtest`, `parity` ve `gonogo` için "
            "gerekir)",
            file=out,
        )
    else:
        try:
            config = load_backtest_config(path)
        except ConfigError as exc:
            valid = False
            print(f"{BACKTEST_FILE_NAME}: HATA, {len(exc.issues)} sorun bulundu", file=out)
            for issue in exc.issues:
                print(f"  - {issue}", file=out)
        else:
            print(f"{BACKTEST_FILE_NAME}: GEÇERLİ", file=out)
            print(
                f"  varsayılan enstrüman: {config.default_instrument} · OOS başlangıcı "
                f"{config.samples.oos_start} · maliyet çarpanları "
                + ", ".join(f"{f:g}".replace(".", ",") for f in config.cost_factors),
                file=out,
            )
            for issue in unresolved_instruments(config):
                print(
                    f"  NOT: {issue.location} {issue.message} → bu profille backtest koşulamaz "
                    "(değerleri işlem terminalinden oku)",
                    file=out,
                )
    path = config_dir / STRATEGIES_FILE_NAME
    if not path.exists():
        print(
            f"{STRATEGIES_FILE_NAME}: yok (yalnızca `propbot backtest` ve `parity` için gerekir)",
            file=out,
        )
        return valid
    try:
        overrides = load_strategy_overrides(path)
    except ConfigError as exc:
        print(f"{STRATEGIES_FILE_NAME}: HATA, {len(exc.issues)} sorun bulundu", file=out)
        for issue in exc.issues:
            print(f"  - {issue}", file=out)
        return False
    print(f"{STRATEGIES_FILE_NAME}: GEÇERLİ", file=out)
    changed = {sid: values for sid, values in overrides.items() if values}
    if not changed:
        # The one input a decision run does not take from the script (2026-11-19).
        print(
            "  tüm stratejiler Pine varsayılanlarıyla koşar (işlem başı risk dışında: karar "
            "koşularında config/risk.yaml riski)",
            file=out,
        )
    for sid, values in sorted(changed.items()):
        print(
            f"  NOT: {sid} Pine varsayılanlarından farklı parametrelerle koşar: "
            f"{_overrides_text(values)} (parite için TradingView'da da aynısı ayarlanmalı)",
            file=out,
        )
        spec = STRATEGIES[sid]
        if not spec.sized_on_account(values):
            print(
                f"  NOT: {sid}: {spec.risk_base_param}={spec.risk_base(values)} iken karar koşusu "
                "yapılamaz (planlanan risk hesabın başlangıç bakiyesinin yüzdesidir); yalnızca "
                "--pine-risk karşılaştırma koşusu ve parity çalışır",
                file=out,
            )
    return valid
