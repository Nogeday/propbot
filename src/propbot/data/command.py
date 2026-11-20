"""`propbot data synth` and `propbot data check` (SPEC §6.1, ADR-0007).

The sub-parsers live in `propbot.cli`; the work is here, with the output streams and the
clock passed in.

`synth` writes deterministic SYNTHETIC bars (`propbot.data.synthetic`). It never replaces
a file unless told to (--force): a made-up series silently overwriting a real export
would be the worst way to lose data.

`check` prints the validation report and writes a `data_validation` artifact. The
Go/No-Go report (G1) matches it to a backtest by the file's sha256, so the artifact
carries the file's identity even when the file cannot be loaded. Exit codes: 0 clean,
1 not clean, 2 when the file (or the config) cannot be read at all.
"""

import argparse
import datetime as dt
import hashlib
import os
import tempfile
from collections.abc import Callable
from pathlib import Path
from typing import Final, TextIO

from pydantic import ValidationError

from propbot.backtest.artifacts import (
    SYNTHETIC_TAG,
    ArtifactError,
    DataInfo,
    DataValidationArtifact,
    build_artifact,
    ms_stamp,
    write_artifact,
)
from propbot.backtest.command import (
    BacktestCommandError,
    Clock,
    check_reports_dir,
    label_from,
    os_error_text,
    print_error,
)
from propbot.backtest.config import BACKTEST_FILE_NAME, load_backtest_config
from propbot.core.timeframe import TimeframeError, timeframe_seconds
from propbot.data.ohlcv import DataError, RawBars, read_ohlcv
from propbot.data.synthetic import generate_bars, write_ohlcv_csv
from propbot.data.validate import ValidationReport, render_validation_report, validate_bars
from propbot.risk.errors import ConfigError

EXIT_OK: Final = 0
EXIT_FAILED: Final = 1
EXIT_ERROR: Final = 2

DEFAULT_TF_MINUTES: Final = 5
DEFAULT_SESSIONS: Final = 250
DEFAULT_START: Final = "2024-01-02"
DEFAULT_SEED: Final = 1
# About ten years of sessions. The generator holds every bar in memory: a typo such as
# --sessions 1000000 would otherwise run for hours and exhaust the machine.
MAX_SESSIONS: Final = 2520

SYNTH_WARNING: Final = (
    f"!!! {SYNTHETIC_TAG}: bu barlar uydurmadır; gerçek piyasa değildir ve hiçbir stratejiyi, "
    "pariteyi ya da veri kalitesini kanıtlamaz (ADR-0007). Dosyanın ilk satırı bunu söyler; "
    "ondan üretilen her rapor da söyler."
)


class DataCommandError(Exception):
    """The data command cannot run as asked. The message is user-facing (Turkish)."""


def _exists(path: Path) -> str:
    return f"--out {path}: dosya zaten var, üzerine yazılmaz (bilerek değiştirmek için --force)"


# --- synth -------------------------------------------------------------------------------


def run_data_synth(args: argparse.Namespace, out: TextIO, err: TextIO) -> int:
    try:
        return _synth(args, out)
    except DataCommandError as exc:
        print_error(exc, err)
        return EXIT_FAILED


def _synth(args: argparse.Namespace, out: TextIO) -> int:
    path = Path(args.out)
    try:
        start = dt.date.fromisoformat(args.start)
    except ValueError:
        raise DataCommandError(
            f"--start YYYY-AA-GG biçiminde olmalı; okunan: {args.start!r}"
        ) from None
    if path.is_dir():
        raise DataCommandError(f"--out {path}: bu bir dizin; dosya yolu ver")
    if (path.exists() or path.is_symlink()) and not args.force:
        raise DataCommandError(_exists(path))
    if not args.symbol.strip():
        raise DataCommandError("--symbol boş olamaz")
    if args.sessions > MAX_SESSIONS:
        raise DataCommandError(
            f"--sessions en fazla {MAX_SESSIONS} olabilir (yaklaşık 10 yıl); verilen: "
            f"{args.sessions}"
        )
    try:
        series = generate_bars(
            symbol=args.symbol,
            tf_seconds=args.tf * 60,
            start=start,
            sessions=args.sessions,
            seed=args.seed,
        )
    except ValueError as exc:
        # generate_bars' own messages are Turkish.
        raise DataCommandError(f"sentetik veri üretilemedi: {exc}") from None
    except OverflowError:
        raise DataCommandError(
            f"--start {start} ile {args.sessions} seans takvimin sonunu (9999-12-31) aşıyor; "
            "daha erken bir --start ver"
        ) from None
    try:
        _write_new(path, writer=lambda target: write_ohlcv_csv(series, target), force=args.force)
    except OSError as exc:
        raise DataCommandError(f"--out {path}: yazılamadı ({os_error_text(exc)})") from exc
    digest = hashlib.sha256(path.read_bytes()).hexdigest()
    first, last = series.first_ts_ms, series.last_ts_ms
    span = "—" if first is None or last is None else f"{ms_stamp(first)} → {ms_stamp(last)}"
    print(SYNTH_WARNING, file=out)
    print(f"Yazıldı: {path}", file=out)
    print(
        f"  sembol {series.symbol} · bar {args.tf} dk · {args.sessions} seans · "
        f"{len(series)} bar · tohum {args.seed}",
        file=out,
    )
    print(f"  {span} (UTC) · sha256 {digest}", file=out)
    return EXIT_OK


def _write_new(path: Path, *, writer: Callable[[Path], None], force: bool) -> None:
    """Write through a temporary file; without `force` an existing name is never replaced."""
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, temp_name = tempfile.mkstemp(prefix=".tmp-", suffix=path.suffix, dir=path.parent)
    os.close(fd)
    temp = Path(temp_name)
    try:
        writer(temp)
        if force:
            os.replace(temp, path)
            return
        try:
            # A hard link fails if the name exists: the existence check and the creation
            # are one step, so a file that appeared meanwhile is not overwritten either.
            os.link(temp, path)
        except FileExistsError:
            raise DataCommandError(_exists(path)) from None
        except OSError:
            if path.exists():
                raise DataCommandError(_exists(path)) from None
            os.replace(temp, path)
    finally:
        temp.unlink(missing_ok=True)


# --- check -------------------------------------------------------------------------------


def run_data_check(args: argparse.Namespace, out: TextIO, err: TextIO, *, clock: Clock) -> int:
    try:
        return _check(args, out, clock)
    except (DataCommandError, BacktestCommandError, DataError, ConfigError) as exc:
        print_error(exc, err)
        return EXIT_ERROR
    except ArtifactError as exc:
        print_error(exc, err)
        return EXIT_FAILED


def _check(args: argparse.Namespace, out: TextIO, clock: Clock) -> int:
    if not args.no_artifact:
        check_reports_dir(Path(args.reports_dir))
    config = load_backtest_config(Path(args.config_dir) / BACKTEST_FILE_NAME)
    tf_seconds = None
    if args.tf is not None:
        try:
            tf_seconds = timeframe_seconds(args.tf)
        except TimeframeError as exc:
            raise DataCommandError(f"--tf: {exc}") from None
    raw = read_ohlcv(Path(args.data), bar_tz=args.bar_tz)
    if args.symbol is not None:
        raw = RawBars(
            symbol=args.symbol,
            source=raw.source,
            bars=raw.bars,
            lines=raw.lines,
            rejected=raw.rejected,
            rejected_total=raw.rejected_total,
        )
    report = validate_bars(
        raw,
        tf_seconds=tf_seconds,
        tz=config.session.tz,
        min_gap_seconds=config.session.min_gap_minutes * 60,
    )
    text = render_validation_report(report)
    out.write(text if text.endswith("\n") else text + "\n")
    if args.no_artifact:
        print("Rapor yazılmadı (--no-artifact).", file=out)
    else:
        artifact = _artifact(report, Path(args.data), clock())
        reports_dir = Path(args.reports_dir)
        try:
            path = write_artifact(artifact, reports_dir)
        except OSError as exc:
            raise DataCommandError(
                f"rapor yazılamadı ({reports_dir}): {os_error_text(exc)}"
            ) from exc
        print(f"Rapor yazıldı: {path}", file=out)
    print(f"Sonuç: {'TEMİZ' if report.clean else 'TEMİZ DEĞİL'}", file=out)
    return EXIT_OK if report.clean else EXIT_FAILED


def _artifact(report: ValidationReport, path: Path, created: dt.datetime) -> DataValidationArtifact:
    source = report.source
    info: DataInfo | None = None
    if report.tf_seconds is not None and report.tf_seconds >= 1:
        # Built from the report, not from a loaded series: a file the loader refuses still
        # needs its identity (name, sha256) in the artifact, for G1's "same file" check.
        try:
            info = DataInfo(
                file_name=source.name,
                sha256=source.sha256,
                format=source.format,
                symbol=report.symbol or source.name,
                tf_seconds=report.tf_seconds,
                bars=report.bar_count,
                first_utc=None if report.first_ts_ms is None else ms_stamp(report.first_ts_ms),
                last_utc=None if report.last_ts_ms is None else ms_stamp(report.last_ts_ms),
                bar_tz=source.bar_tz,
                synthetic=source.synthetic,
            )
        except ValidationError as exc:
            raise ArtifactError(
                f"rapor oluşturulamadı: veri bilgisi geçersiz ({exc.error_count()} hata)"
            ) from None
    notes = (
        []
        if info is not None
        else ["Bar süresi belirlenemedi: rapor dosyanın kimliğini taşımıyor."]
    )
    return build_artifact(
        DataValidationArtifact,
        label=label_from(path.stem),
        created=created,
        synthetic=source.synthetic,
        data=info,
        notes=notes,
        clean=report.clean,
        report=report.to_dict(),
    )
