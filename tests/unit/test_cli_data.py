"""`propbot data synth` and `propbot data check`: outputs, artifacts, refusals, exit codes."""

import datetime as dt
import hashlib
import json
from collections.abc import Callable
from pathlib import Path

import pytest

from propbot.backtest.artifacts import DataValidationArtifact, load_artifact
from propbot.cli import main
from support.paths import CONFIG_DIR, CONFIG_FIXTURES

START = dt.datetime(2026, 11, 18, 9, 0, tzinfo=dt.UTC)


def clock() -> Callable[[], dt.datetime]:
    """Advances one second per call, so every artifact gets its own file name."""
    moments = iter(START + dt.timedelta(seconds=step) for step in range(10_000))
    return lambda: next(moments)


def listing(root: Path) -> set[str]:
    return {str(path.relative_to(root)) for path in root.rglob("*")}


def synth(path: Path, *extra: str) -> int:
    return main(["data", "synth", "--out", str(path), "--sessions", "3", *extra])


def check(data: Path, reports: Path, *extra: str) -> int:
    arguments = ["data", "check", "--data", str(data), "--config-dir", str(CONFIG_DIR)]
    return main([*arguments, "--reports-dir", str(reports), *extra], clock=clock())


def write_bars(path: Path, skip: int | None = None, duplicate: int | None = None) -> Path:
    """Twenty 5-minute bars of one regular session; optionally one missing or repeated."""
    rows = ["time,open,high,low,close,volume"]
    first = dt.datetime(2026, 3, 2, 14, 30, tzinfo=dt.UTC)
    for index in range(20):
        if index == skip:
            continue
        stamp = (first + dt.timedelta(minutes=5 * index)).strftime("%Y-%m-%dT%H:%M:%SZ")
        row = f"{stamp},100,101,99,100.5,10"
        rows.append(row)
        if index == duplicate:
            rows.append(row)
    path.write_text("\n".join(rows) + "\n", encoding="utf-8")
    return path


# --- synth -------------------------------------------------------------------------------


def test_synth_writes_a_marked_synthetic_file_and_says_so(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    path = tmp_path / "bars" / "synth.csv"

    code = synth(path, "--start", "2024-12-02", "--seed", "7")

    captured = capsys.readouterr()
    assert code == 0
    assert captured.err == ""
    assert "SENTETİK VERİ" in captured.out
    assert "hiçbir stratejiyi" in captured.out
    assert f"Yazıldı: {path}" in captured.out
    assert "3 seans" in captured.out
    assert hashlib.sha256(path.read_bytes()).hexdigest() in captured.out
    first_line = path.read_text(encoding="utf-8").splitlines()[0]
    assert first_line.startswith("#propbot-synthetic")
    assert "SENTETİK VERİ" in first_line
    assert listing(tmp_path) == {"bars", "bars/synth.csv"}


def test_synth_is_deterministic_and_seed_dependent(tmp_path: Path) -> None:
    synth(tmp_path / "a.csv", "--seed", "3")
    synth(tmp_path / "b.csv", "--seed", "3")
    synth(tmp_path / "c.csv", "--seed", "4")

    assert (tmp_path / "a.csv").read_bytes() == (tmp_path / "b.csv").read_bytes()
    assert (tmp_path / "a.csv").read_bytes() != (tmp_path / "c.csv").read_bytes()


def test_synth_refuses_to_overwrite_without_force(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    path = tmp_path / "synth.csv"
    path.write_text("gerçek veri\n", encoding="utf-8")

    refused = synth(path)
    refusal = capsys.readouterr()
    forced = synth(path, "--force")

    assert refused == 1
    assert refusal.out == ""
    assert refusal.err.startswith("HATA: ")
    assert "dosya zaten var" in refusal.err
    assert "--force" in refusal.err
    assert forced == 0
    assert path.read_text(encoding="utf-8").startswith("#propbot-synthetic")
    # No temporary file is left behind by either attempt.
    assert listing(tmp_path) == {"synth.csv"}


@pytest.mark.parametrize(
    ("extra", "message"),
    [
        (["--start", "2024-13-01"], "--start YYYY-AA-GG"),
        (["--start", "dün"], "--start YYYY-AA-GG"),
        (["--tf", "7"], "sentetik veri üretilemedi"),
        (["--sessions", "0"], "seans sayısı en az 1"),
        (["--symbol", " "], "--symbol boş olamaz"),
    ],
)
def test_synth_refuses_bad_arguments(
    tmp_path: Path, capsys: pytest.CaptureFixture[str], extra: list[str], message: str
) -> None:
    code = main(["data", "synth", "--out", str(tmp_path / "s.csv"), *extra])

    captured = capsys.readouterr()
    assert code == 1
    assert captured.out == ""
    assert message in captured.err
    assert "Traceback" not in captured.err
    assert listing(tmp_path) == set()


def test_synth_refuses_a_directory_as_output(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    assert synth(tmp_path) == 1
    assert "bu bir dizin" in capsys.readouterr().err


# --- check -------------------------------------------------------------------------------


def test_check_of_synthetic_bars_is_clean_and_writes_a_synthetic_artifact(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    data = tmp_path / "my synth.csv"
    synth(data, "--start", "2024-12-02")
    capsys.readouterr()
    reports = tmp_path / "reports"

    code = check(data, reports)

    out = capsys.readouterr().out
    assert code == 0
    assert "SENTETİK VERİ" in out
    assert "Veri doğrulama raporu: TEMİZ" in out
    assert out.rstrip().endswith("Sonuç: TEMİZ")
    (written,) = reports.iterdir()
    assert written.name == "data_validation_my-synth_20261118T090000Z.json"
    assert f"Rapor yazıldı: {written}" in out
    artifact = load_artifact(written)
    assert isinstance(artifact, DataValidationArtifact)
    assert artifact.synthetic
    assert artifact.clean
    assert artifact.data is not None
    assert artifact.data.sha256 == hashlib.sha256(data.read_bytes()).hexdigest()
    assert artifact.data.file_name == "my synth.csv"
    assert artifact.data.tf_seconds == 300
    assert any("SENTETİK VERİ" in note for note in artifact.notes)


def test_check_of_a_file_with_a_gap_is_not_clean(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    data = write_bars(tmp_path / "gap.csv", skip=10)

    code = check(data, tmp_path / "reports", "--tf", "5")

    out = capsys.readouterr().out
    assert code == 1
    assert "SORUNLU" in out
    assert "SENTETİK" not in out
    assert out.rstrip().endswith("Sonuç: TEMİZ DEĞİL")
    (written,) = (tmp_path / "reports").iterdir()
    artifact = load_artifact(written)
    assert isinstance(artifact, DataValidationArtifact)
    assert not artifact.clean
    assert not artifact.synthetic


def test_check_of_a_file_the_loader_refuses_still_names_the_file(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    data = write_bars(tmp_path / "dup.csv", duplicate=4)

    code = check(data, tmp_path / "reports")

    out = capsys.readouterr().out
    assert code == 1
    assert "YÜKLENEMEZ" in out
    (written,) = (tmp_path / "reports").iterdir()
    artifact = load_artifact(written)
    assert isinstance(artifact, DataValidationArtifact)
    assert artifact.data is not None
    assert artifact.data.sha256 == hashlib.sha256(data.read_bytes()).hexdigest()
    report = json.loads(written.read_text(encoding="utf-8"))["report"]
    assert report["source"]["name"] == "dup.csv"


def test_check_without_artifact_writes_nothing(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    data = write_bars(tmp_path / "bars.csv")

    code = check(data, tmp_path / "reports", "--no-artifact")

    assert code == 0
    assert "Rapor yazılmadı (--no-artifact)." in capsys.readouterr().out
    assert listing(tmp_path) == {"bars.csv"}


def test_check_symbol_override_is_reported(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    data = write_bars(tmp_path / "bars.csv")

    check(data, tmp_path / "reports", "--symbol", "MES")

    capsys.readouterr()
    (written,) = (tmp_path / "reports").iterdir()
    artifact = load_artifact(written)
    assert artifact.data is not None
    assert artifact.data.symbol == "MES"


@pytest.mark.parametrize(
    ("make", "extra", "message"),
    [
        (lambda tmp: tmp / "yok.csv", [], "dosya bulunamadı"),
        (lambda tmp: _empty(tmp / "bos.csv"), [], "dosya boş"),
        (lambda tmp: tmp, [], "bu bir dizin"),
        (lambda tmp: write_bars(tmp / "b.csv"), ["--tf", "1D"], "--tf: desteklenmeyen"),
        (lambda tmp: write_bars(tmp / "b.csv"), ["--bar-tz", "Mars/Olympus"], "bilinmeyen bar_tz"),
    ],
)
def test_check_exits_2_when_the_input_cannot_be_read(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
    make: Callable[[Path], Path],
    extra: list[str],
    message: str,
) -> None:
    data = make(tmp_path)
    before = listing(tmp_path)

    code = check(data, tmp_path / "reports", *extra)

    captured = capsys.readouterr()
    assert code == 2
    assert captured.out == ""
    assert captured.err.startswith("HATA: ")
    assert message in captured.err
    assert listing(tmp_path) == before


def test_check_needs_the_backtest_config(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    data = write_bars(tmp_path / "b.csv")
    arguments = ["data", "check", "--data", str(data)]
    arguments += ["--config-dir", str(CONFIG_FIXTURES / "valid")]

    code = main([*arguments, "--reports-dir", str(tmp_path / "r")], clock=clock())

    captured = capsys.readouterr()
    assert code == 2
    assert "HATA: config geçersiz (1 sorun)" in captured.err
    assert "backtest.yaml" in captured.err
    assert "dosya bulunamadı" in captured.err


def _empty(path: Path) -> Path:
    path.write_bytes(b"")
    return path
