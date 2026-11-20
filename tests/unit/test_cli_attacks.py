"""The Faz 2 commands driven as a careless and as a hostile user would drive them.

Each test pins one way a person can hold the tool wrong: a path that is a directory, a
report folder that is a file, an output pointed at the repository's fixtures, a broken
pipe, absurd sizes, argparse mistakes. The rules checked everywhere: no Python
traceback, one Turkish line per refusal, the documented exit code, nothing overwritten
and nothing synthetic that can reach a Go/No-Go PASS (ADR-0007).
"""

import datetime as dt
import errno
import hashlib
import os
import re
import subprocess
import sys
from collections.abc import Callable, Iterator
from pathlib import Path
from typing import Any

import pytest

from propbot.backtest.artifacts import BacktestArtifact, DataValidationArtifact, load_artifact
from propbot.backtest.trades import read_trades_csv
from propbot.cli import main
from support.paths import CONFIG_DIR, FIXTURES, REPO_ROOT

BARS = FIXTURES / "ohlcv" / "synthetic_5m.csv"
FIXTURE_TRADES = FIXTURES / "trades" / "synthetic_trades.csv"
SAMPLE_TV = FIXTURES / "parity" / "sample_tv_basic_comma.csv"
VENUE = CONFIG_DIR / "venues" / "ftmo_2step_100k.yaml"
RISK = CONFIG_DIR / "risk.yaml"
START = dt.datetime(2026, 11, 18, 9, 0, tzinfo=dt.UTC)
OOS = "2026-11-01"
# Fragments of argparse's own English that must never reach the user.
ENGLISH = ("usage:", "error:", "options:", "show this help", "invalid", "required", "argument ")


def clock(step_seconds: int = 1, *, offset: int = 0) -> Callable[[], dt.datetime]:
    """A test never depends on the real time; step 0 freezes it (same second)."""
    first = START + dt.timedelta(seconds=offset)
    moments = iter(first + dt.timedelta(seconds=step * step_seconds) for step in range(10_000))
    return lambda: next(moments)


def listing(root: Path) -> set[str]:
    return {str(path.relative_to(root)) for path in root.rglob("*")}


def digest(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def flat(text: str) -> str:
    """argparse wraps help text; compare with single spaces."""
    return " ".join(text.split())


@pytest.fixture
def cwd(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Iterator[Path]:
    work = tmp_path / "cwd"
    work.mkdir()
    monkeypatch.chdir(work)
    yield work
    assert listing(work) == set(), "a command wrote into the working directory"


def backtest(reports: Path, *extra: str, now: Callable[[], dt.datetime] | None = None) -> int:
    arguments = ["backtest", "--data", str(BARS), "--config-dir", str(CONFIG_DIR)]
    arguments += ["--reports-dir", str(reports), "--oos-start", OOS]
    if "--strategy" not in extra:
        arguments += ["--strategy", "ny_orb_fvg"]
    if "--data" in extra:
        index = arguments.index("--data")
        del arguments[index : index + 2]
    return main([*arguments, *extra], clock=now or clock())


def montecarlo(trades: Path, reports: Path, *extra: str) -> int:
    arguments = ["montecarlo", "--trades", str(trades), "--venue", str(VENUE)]
    arguments += ["--risk-config", str(RISK), "--paths", "200", "--max-days", "100"]
    arguments += ["--dd-days", "30", "--reports-dir", str(reports)]
    return main([*arguments, *extra], clock=clock())


def gonogo(reports: Path, strategies: str = "ny_orb_fvg", *, offset: int = 60) -> int:
    """Later than every run the test made (the report must see them as older)."""
    arguments = ["gonogo", "--strategies", strategies, "--config-dir", str(CONFIG_DIR)]
    arguments += ["--reports-dir", str(reports), "--compliance-dir", str(reports / "yok")]
    return main(arguments, clock=clock(offset=offset))


def no_traceback(captured: Any) -> None:
    assert "Traceback" not in captured.out
    assert "Traceback" not in captured.err


# --- data synth: sizes and the calendar --------------------------------------------------


@pytest.mark.parametrize(
    ("extra", "message"),
    [
        (["--sessions", "100000000"], "--sessions en fazla"),
        (["--sessions", "1000000"], "--sessions en fazla"),
        (["--start", "9999-12-28", "--sessions", "5"], "takvim"),
    ],
)
def test_synth_refuses_absurd_sizes_with_a_turkish_line(
    tmp_path: Path, capsys: pytest.CaptureFixture[str], extra: list[str], message: str
) -> None:
    out = tmp_path / "bars.csv"

    code = main(["data", "synth", "--out", str(out), *extra])

    captured = capsys.readouterr()
    no_traceback(captured)
    assert code == 1
    assert captured.err.startswith("HATA: ")
    assert message in captured.err
    assert listing(tmp_path) == set()


def test_synth_never_overwrites_a_repository_fixture(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    before = digest(BARS)

    code = main(["data", "synth", "--out", str(BARS), "--sessions", "2"])

    assert code == 1
    assert "dosya zaten var, üzerine yazılmaz" in capsys.readouterr().err
    assert digest(BARS) == before


def test_synth_refuses_a_dangling_symlink_without_force(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    link = tmp_path / "bars.csv"
    link.symlink_to(tmp_path / "hedef.csv")

    code = main(["data", "synth", "--out", str(link), "--sessions", "2"])

    assert code == 1
    assert "dosya zaten var" in capsys.readouterr().err
    assert not (tmp_path / "hedef.csv").exists()


def test_paths_with_spaces_and_turkish_letters_go_through_the_whole_chain(
    tmp_path: Path, cwd: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    folder = tmp_path / "ş klasör ğüı"
    bars = folder / "çubuk barlar.csv"
    reports = tmp_path / "rapor ç"

    assert main(["data", "synth", "--out", str(bars), "--sessions", "3"]) == 0
    check = ["data", "check", "--data", str(bars), "--config-dir", str(CONFIG_DIR)]
    assert main([*check, "--reports-dir", str(reports)], clock=clock()) == 0
    assert backtest(reports, "--data", str(bars)) == 0

    no_traceback(capsys.readouterr())
    kinds = sorted(type(load_artifact(path)).__name__ for path in reports.iterdir())
    assert kinds == ["BacktestArtifact", "DataValidationArtifact"]


# --- reports folders that are not folders -------------------------------------------------


def _reports_is_a_file(tmp_path: Path) -> Path:
    blocker = tmp_path / "reports"
    blocker.write_text("bu bir dosya\n", encoding="utf-8")
    return blocker


@pytest.mark.parametrize("command", ["backtest", "parity", "data check", "montecarlo", "gonogo"])
def test_a_reports_dir_that_is_a_file_is_refused_before_any_work(
    tmp_path: Path, cwd: Path, capsys: pytest.CaptureFixture[str], command: str
) -> None:
    reports = _reports_is_a_file(tmp_path)
    expected_code = {"backtest": 1, "montecarlo": 1, "parity": 2, "data check": 2, "gonogo": 2}

    if command == "backtest":
        code = backtest(reports)
    elif command == "montecarlo":
        code = montecarlo(FIXTURE_TRADES, reports, "--cost-r", "0.05")
    elif command == "gonogo":
        code = gonogo(reports)
    elif command == "parity":
        arguments = ["parity", "--strategy", "ny_orb_fvg", "--tv-trades", str(SAMPLE_TV)]
        arguments += ["--tv-tz", "America/New_York", "--data", str(BARS)]
        arguments += ["--config-dir", str(CONFIG_DIR), "--reports-dir", str(reports)]
        code = main(arguments, clock=clock())
    else:
        arguments = ["data", "check", "--data", str(BARS), "--config-dir", str(CONFIG_DIR)]
        code = main([*arguments, "--reports-dir", str(reports)], clock=clock())

    captured = capsys.readouterr()
    no_traceback(captured)
    assert code == expected_code[command]
    assert f"--reports-dir {reports}: dizin değil" in captured.err
    for english in ("File exists", "Not a directory"):
        assert english not in captured.err
    # Refused before the work: no strategy summary, no simulation, no gate table.
    for heading in ("Backtest:", "SONUÇLAR", "Karar:", "Parite (SPEC", "Veri doğrulama raporu"):
        assert heading not in captured.out
    assert reports.read_text(encoding="utf-8") == "bu bir dosya\n"


def test_an_unwritable_reports_dir_is_explained_in_turkish(
    tmp_path: Path, cwd: Path, capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch
) -> None:
    # The tests may run as root, where chmod cannot make a folder unwritable: the
    # operating system's refusal is simulated at the one place that writes.
    def refuse(*_args: object, **_kwargs: object) -> Path:
        raise PermissionError(errno.EACCES, "Permission denied")

    monkeypatch.setattr("propbot.backtest.command.write_artifact", refuse)
    monkeypatch.setattr("propbot.data.command.write_artifact", refuse)

    code = backtest(tmp_path / "reports")
    bt = capsys.readouterr()
    arguments = ["data", "check", "--data", str(BARS), "--config-dir", str(CONFIG_DIR)]
    check_code = main([*arguments, "--reports-dir", str(tmp_path / "r")], clock=clock())
    dc = capsys.readouterr()

    assert (code, check_code) == (1, 2)
    for captured in (bt, dc):
        no_traceback(captured)
        assert "rapor yazılamadı" in captured.err
        assert "erişim izni yok" in captured.err
        assert "Permission denied" not in captured.err


def test_a_trade_list_path_that_is_a_directory_is_called_a_directory(
    tmp_path: Path, cwd: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    folder = tmp_path / "liste"
    folder.mkdir()

    code = backtest(tmp_path / "reports", "--trades-out", str(folder))

    captured = capsys.readouterr()
    assert code == 1
    assert f"--trades-out {folder}: bu bir dizin" in captured.err
    assert "dosya zaten var" not in captured.err
    assert listing(folder) == set()


def test_a_trade_list_never_overwrites_a_repository_fixture(
    tmp_path: Path, cwd: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    before = digest(FIXTURE_TRADES)

    code = backtest(tmp_path / "reports", "--trades-out", str(FIXTURE_TRADES))

    assert code == 1
    assert "üzerine yazılmaz" in capsys.readouterr().err
    assert digest(FIXTURE_TRADES) == before


# --- the same second ----------------------------------------------------------------------


def test_backtest_twice_in_the_same_second_is_a_clean_refusal_not_an_overwrite(
    tmp_path: Path, cwd: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    reports = tmp_path / "reports"
    frozen = clock(step_seconds=0)

    assert backtest(reports, now=frozen) == 0
    (first,) = reports.iterdir()
    before = digest(first)
    capsys.readouterr()

    again = backtest(reports, now=frozen)
    captured = capsys.readouterr()
    comparison = backtest(reports, "--cost-free", now=frozen)

    no_traceback(captured)
    assert again == 1
    assert "dosya zaten var, üzerine yazılmadı" in captured.err
    assert digest(first) == before
    # A comparison run in the same second has its own label, so its own name.
    assert comparison == 0
    assert len(list(reports.iterdir())) == 2


# --- synthetic data can never pass ---------------------------------------------------------


def test_no_flag_combination_on_synthetic_bars_reaches_go_or_a_passed_g1(
    tmp_path: Path, cwd: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    reports = tmp_path / "reports"
    trades = tmp_path / "trades.csv"
    check = ["data", "check", "--data", str(BARS), "--config-dir", str(CONFIG_DIR)]
    assert main([*check, "--reports-dir", str(reports)], clock=clock()) == 0
    runs: list[tuple[str, ...]] = [
        ("--matrix", "--walk-forward", "--trades-out", str(trades)),
        ("--cost-free",),
        ("--fill-model", "tv_ohlc"),
        ("--cost-factor", "0.5"),
        ("--cost-factor", "1.0000001"),
    ]
    for step, extra in enumerate(runs):
        # Each run in its own second: the newest run is a comparison run, not the decision.
        assert backtest(reports, *extra, now=clock(offset=step)) == 0, extra
    assert montecarlo(trades, reports, "--r-is-net") == 0
    capsys.readouterr()

    # The third choice held one Master leg; one leg alone is refused since 2026-11-20 (open
    # finding 87), so it now gives both: still strategies without any report of their own.
    choices = (
        "ny_orb_fvg",
        "ny_orb_fvg,ny_orb_fvg",
        "master_ny_orb_fvg,master_asian_sweep,ny_orb_fvg",
    )
    for index, strategies in enumerate(choices):
        code = gonogo(reports, strategies, offset=60 + index)
        out = capsys.readouterr().out
        assert code == 1, strategies
        assert "Karar: NO-GO" in out
        g1 = next(line for line in out.splitlines() if line.strip().startswith("G1"))
        assert "GEÇTİ" not in g1
        assert "KALDI" in g1


def test_comparison_runs_alone_are_never_decision_runs(
    tmp_path: Path, cwd: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    reports = tmp_path / "reports"
    for extra in (("--cost-free",), ("--fill-model", "tv_ohlc"), ("--cost-factor", "0.5")):
        assert backtest(reports, *extra) == 0
    capsys.readouterr()

    code = gonogo(reports)

    out = capsys.readouterr().out
    assert code == 1
    for gate in ("G2", "G3"):
        line = next(text for text in out.splitlines() if text.strip().startswith(gate))
        assert "GEÇTİ" not in line, line


def test_a_synthetic_file_without_its_marker_is_consistent_along_the_chain(
    tmp_path: Path, cwd: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    # The tool cannot know such a file is made up; what it can do is name the same file
    # everywhere, so that a person can trace any report back to the bytes it came from.
    stripped = tmp_path / "es_5m.csv"
    lines = BARS.read_text(encoding="utf-8").splitlines(keepends=True)
    stripped.write_text("".join(line for line in lines if not line.startswith("#")))
    reports = tmp_path / "reports"
    trades = tmp_path / "trades.csv"

    check = ["data", "check", "--data", str(stripped), "--config-dir", str(CONFIG_DIR)]
    assert main([*check, "--reports-dir", str(reports)], clock=clock()) == 0
    assert backtest(reports, "--data", str(stripped), "--trades-out", str(trades)) == 0

    out = capsys.readouterr().out
    assert "SENTETİK VERİ" not in out
    artifacts = [load_artifact(path) for path in sorted(reports.iterdir())]
    (validation,) = [a for a in artifacts if isinstance(a, DataValidationArtifact)]
    (run,) = [a for a in artifacts if isinstance(a, BacktestArtifact)]
    _, provenance = read_trades_csv(trades)
    assert provenance is not None
    assert validation.data is not None
    sha = digest(stripped)
    assert validation.data.sha256 == run.data.sha256 == provenance.data_sha256 == sha
    assert not validation.synthetic
    assert not run.synthetic
    assert not provenance.synthetic
    assert provenance.data_file == stripped.name


def test_montecarlo_on_a_synthetic_list_says_so_on_the_terminal(
    tmp_path: Path, cwd: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    trades = tmp_path / "liste.csv"
    assert backtest(tmp_path / "reports", "--no-artifact", "--trades-out", str(trades)) == 0
    capsys.readouterr()

    code = montecarlo(trades, tmp_path / "reports", "--no-artifact")

    out = capsys.readouterr().out
    assert code == 0
    assert out.startswith("!!! SENTETİK VERİ")
    # The list carries its costs (provenance line): the bare run must say how to declare it.
    assert "--r-is-net" in out.split("UYARILAR")[0]


# --- argparse speaks Turkish ---------------------------------------------------------------


@pytest.mark.parametrize(
    ("argv", "fragment"),
    [
        (["data", "synth"], "şu argümanlar zorunlu: --out"),
        (["data", "synth", "--out", "x.csv", "--tf", "abc"], "geçersiz tam sayı değeri: 'abc'"),
        (["backtest", "--strategy", "x", "--data", "y", "--fill-model", "z"], "geçersiz seçim"),
        (["gonogo", "--strategies", "a", "--fazla"], "tanınmayan argümanlar: --fazla"),
        (["data", "check", "--data"], "bir değer bekleniyor"),
        (["nope"], "geçersiz seçim: 'nope'"),
        (
            ["backtest", "--strategy", "x", "--data", "y", "--cost-free", "--cost-factor", "2"],
            "birlikte verilemez",
        ),
        (["montecarlo", "--trades", "t", "--venue", "v", "--paths", "1.5"], "tam sayı"),
    ],
)
def test_usage_errors_are_turkish_with_exit_code_2(
    capsys: pytest.CaptureFixture[str], argv: list[str], fragment: str
) -> None:
    with pytest.raises(SystemExit) as excinfo:
        main(argv)

    err = capsys.readouterr().err
    assert excinfo.value.code == 2
    assert "HATA: " in err
    assert fragment in err
    assert "kullanım: propbot" in err
    for english in ENGLISH:
        assert english not in err, english


@pytest.mark.parametrize(
    ("argv", "writes"),
    [
        ([], "KOMUT"),
        (["data", "synth"], "--out"),
        (["data", "check"], "data_validation_"),
        (["backtest"], "backtest_<kimlik>_<zaman>.json"),
        (["parity"], "parity_<kimlik>_<zaman>.json"),
        (["montecarlo"], "monte_carlo_"),
        (["gonogo"], "go_no_go_YYYYAAGG.md"),
        (["config", "check"], "Dosya yazmaz"),
        (["serve"], "Emir göndermez"),
        (["dashboard"], "ADR-0009"),
    ],
)
def test_help_is_turkish_and_names_what_the_command_writes(
    capsys: pytest.CaptureFixture[str], argv: list[str], writes: str
) -> None:
    with pytest.raises(SystemExit) as excinfo:
        main([*argv, "--help"])

    out = flat(capsys.readouterr().out)
    assert excinfo.value.code == 0
    assert out.startswith("kullanım: propbot")
    assert writes in out
    assert "bu yardımı göster ve çık" in out
    for english in ENGLISH:
        assert english not in out, english
    if argv and argv[0] in {"data", "backtest", "parity", "montecarlo", "gonogo", "config"}:
        assert "Çıkış kodu" in out


# --- a closed pipe ---------------------------------------------------------------------------


class _ClosedPipe:
    """A stdout whose reader went away (`propbot … | head -1`)."""

    def write(self, _text: str) -> int:
        raise BrokenPipeError(errno.EPIPE, "Broken pipe")

    def flush(self) -> None:
        raise BrokenPipeError(errno.EPIPE, "Broken pipe")


@pytest.mark.parametrize("command", ["data check", "gonogo"])
def test_a_closed_stdout_ends_the_command_quietly(
    tmp_path: Path,
    cwd: Path,
    capsys: pytest.CaptureFixture[str],
    monkeypatch: pytest.MonkeyPatch,
    command: str,
) -> None:
    reports = tmp_path / "reports"
    monkeypatch.setattr(sys, "stdout", _ClosedPipe())

    if command == "gonogo":
        code = gonogo(reports)
    else:
        arguments = ["data", "check", "--data", str(BARS), "--config-dir", str(CONFIG_DIR)]
        code = main([*arguments, "--reports-dir", str(reports)], clock=clock())

    err = capsys.readouterr().err
    assert code == 141
    assert err == ""


def test_the_real_entry_point_survives_a_closed_pipe(tmp_path: Path) -> None:
    read_end, write_end = os.pipe()
    os.close(read_end)  # nobody will ever read: the first write fails
    script = "import sys; from propbot.cli import main; sys.exit(main())"
    argv = [sys.executable, "-c", script, "data", "synth", "--sessions", "1"]
    env = {**os.environ, "PYTHONPATH": str(REPO_ROOT / "src")}
    try:
        done = subprocess.run(
            [*argv, "--out", str(tmp_path / "b.csv")],
            stdout=write_end,
            stderr=subprocess.PIPE,
            env=env,
            cwd=tmp_path,
            timeout=60,
            check=False,
        )
    finally:
        os.close(write_end)

    err = done.stderr.decode("utf-8")
    assert "Traceback" not in err
    assert "BrokenPipeError" not in err
    assert done.returncode == 141
    # The file itself was written before the terminal output failed.
    assert (tmp_path / "b.csv").is_file()
    assert re.search(r"\S", err) is None
