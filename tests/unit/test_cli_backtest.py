"""`propbot backtest`, `parity`, `gonogo`, the Monte Carlo artifact and `config check`.

Every run reads the SYNTHETIC 5-minute fixture, so every output must say SENTETİK VERİ
and nothing may reach a Go/No-Go PASS (ADR-0007). The tests also pin that a command
writes only where its options point (the working directory stays empty).
"""

import dataclasses
import datetime as dt
import json
import shutil
from collections.abc import Callable, Iterator
from pathlib import Path
from zoneinfo import ZoneInfo

import pytest

from propbot.backtest import command
from propbot.backtest.artifacts import (
    BacktestArtifact,
    CostMatrixArtifact,
    GoNoGoArtifact,
    MonteCarloArtifact,
    ParityArtifact,
    WalkForwardArtifact,
    load_artifact,
)
from propbot.backtest.config import load_backtest_config
from propbot.backtest.model import FillModel
from propbot.backtest.runner import RunRequest, run
from propbot.backtest.trades import read_trades_csv
from propbot.cli import main
from propbot.data.ohlcv import load_ohlcv
from propbot.strategies.registry import STRATEGIES
from support.paths import CONFIG_DIR, FIXTURES

BARS = FIXTURES / "ohlcv" / "synthetic_5m.csv"
SAMPLE_TV = FIXTURES / "parity" / "sample_tv_basic_comma.csv"
VENUE = CONFIG_DIR / "venues" / "ftmo_2step_100k.yaml"
START = dt.datetime(2026, 11, 18, 9, 0, tzinfo=dt.UTC)
NY = ZoneInfo("America/New_York")
# The fixture spans 2026-10-19 → 2026-11-13: a split inside it gives both samples trades.
OOS = "2026-11-01"


def clock() -> Callable[[], dt.datetime]:
    """Advances one second per call: a test never depends on the real time."""
    moments = iter(START + dt.timedelta(seconds=step) for step in range(10_000))
    return lambda: next(moments)


def listing(root: Path) -> set[str]:
    return {str(path.relative_to(root)) for path in root.rglob("*")}


@pytest.fixture
def cwd(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Iterator[Path]:
    """An empty working directory: no command may write into it (default paths)."""
    work = tmp_path / "cwd"
    work.mkdir()
    monkeypatch.chdir(work)
    yield work
    assert listing(work) == set(), "a command wrote into the working directory"


def with_fixture_oos(config_dir: Path) -> None:
    """Set samples.oos_start to the fixture's split.

    Since the cross-cutting review (A, 2026-11-19) a run whose --oos-start differs from
    config/backtest.yaml is a labelled comparison run; these tests exercise decision
    runs on a fixture that spans only Oct-Nov 2026, so the configured boundary is moved.
    """
    path = config_dir / "backtest.yaml"
    text = path.read_text(encoding="utf-8")
    assert 'oos_start: "2025-01-01"' in text
    path.write_text(text.replace('oos_start: "2025-01-01"', f'oos_start: "{OOS}"'), "utf-8")


@pytest.fixture(scope="module", autouse=True)
def fixture_config(tmp_path_factory: pytest.TempPathFactory) -> None:
    global FIXTURE_CONFIG
    target = tmp_path_factory.mktemp("config-oos") / "config"
    shutil.copytree(CONFIG_DIR, target)
    with_fixture_oos(target)
    FIXTURE_CONFIG = target


FIXTURE_CONFIG = CONFIG_DIR  # replaced by the module fixture above


def backtest(reports: Path, *extra: str, config_dir: Path | None = None) -> int:
    chosen = FIXTURE_CONFIG if config_dir is None else config_dir
    arguments = ["backtest", "--data", str(BARS), "--config-dir", str(chosen)]
    arguments += ["--reports-dir", str(reports), "--oos-start", OOS]
    if "--strategy" not in extra:
        arguments += ["--strategy", "ny_orb_fvg"]
    return main([*arguments, *extra], clock=clock())


def config_copy(tmp_path: Path, *, strategies: str | None = None) -> Path:
    target = tmp_path / "config"
    shutil.copytree(CONFIG_DIR, target)
    with_fixture_oos(target)
    if strategies is not None:
        (target / "strategies.yaml").write_text(strategies, encoding="utf-8")
    return target


def loaded(reports: Path) -> list[object]:
    return [load_artifact(path) for path in sorted(reports.iterdir())]


# --- backtest ----------------------------------------------------------------------------


def test_backtest_writes_the_decision_run_and_says_synthetic(
    tmp_path: Path, cwd: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    reports = tmp_path / "reports"

    code = backtest(reports)

    captured = capsys.readouterr()
    assert code == 0
    assert captured.err == ""
    assert captured.out.startswith("!!! SENTETİK VERİ")
    assert "Backtest: ny_orb_fvg" in captured.out
    assert "Venue: ftmo_2step_100k (başlangıç sermayesi = initial_balance)" in captured.out
    assert "Go/No-Go'da hiçbir kapıyı geçemez" in captured.out
    (written,) = reports.iterdir()
    assert written.name == "backtest_ny_orb_fvg_20261118T090000Z.json"
    assert f"Rapor yazıldı: {written}" in captured.out
    artifact = load_artifact(written)
    assert isinstance(artifact, BacktestArtifact)
    assert artifact.synthetic
    assert artifact.strategy_id == "ny_orb_fvg"
    assert artifact.fill_model == "pessimistic"
    assert artifact.cost_factor == 1.0
    assert not artifact.costs.cost_free
    assert artifact.instrument.symbol == "MES"
    assert artifact.initial_capital == 100_000.0
    assert artifact.oos_start_utc == "2026-11-01T00:00:00Z"


def test_backtest_all_runs_the_five_stand_alone_ports_with_matrix_folds_and_trade_list(
    tmp_path: Path, cwd: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    reports = tmp_path / "reports"
    trades = tmp_path / "out" / "trades.csv"

    code = backtest(
        reports, "--strategy", "all", "--matrix", "--walk-forward", "--trades-out", str(trades)
    )

    out = capsys.readouterr().out
    assert code == 0
    artifacts = loaded(reports)
    backtests = {a.strategy_id for a in artifacts if isinstance(a, BacktestArtifact)}
    assert backtests == {
        "ny_orb_fvg",
        "asian_sweep",
        "ict_silver_bullet",
        "mr_bb_kc_rsi2",
        "mtf_trend_cont",
    }
    assert sum(isinstance(a, CostMatrixArtifact) for a in artifacts) == 10
    assert sum(isinstance(a, WalkForwardArtifact) for a in artifacts) == 5
    assert all(a.synthetic for a in artifacts)  # type: ignore[attr-defined]
    rows, provenance = read_trades_csv(trades)
    assert provenance is not None
    assert provenance.synthetic
    assert provenance.fill_model == "pessimistic"
    assert provenance.cost_factor == 1.0
    assert rows
    assert f"İşlem listesi yazıldı: {trades}" in out
    assert {p for p in listing(tmp_path) if not p.startswith(("reports", "cwd"))} == {
        "out",
        "out/trades.csv",
    }


def test_backtest_comparison_runs_carry_their_label(
    tmp_path: Path, cwd: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    reports = tmp_path / "reports"

    assert backtest(reports, "--cost-free") == 0
    assert backtest(reports, "--cost-factor", "1.5", "--fill-model", "tv_ohlc") == 0

    out = capsys.readouterr().out
    assert "MALİYETSİZ KOŞU" in out
    names = sorted(path.name for path in reports.iterdir())
    assert names == [
        "backtest_ny_orb_fvg-free_20261118T090000Z.json",
        "backtest_ny_orb_fvg-tv-x1p5_20261118T090000Z.json",
    ]


def test_backtest_without_artifact_writes_nothing(
    tmp_path: Path, cwd: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    code = backtest(tmp_path / "reports", "--no-artifact")

    assert code == 0
    assert "Rapor yazılmadı (--no-artifact)." in capsys.readouterr().out
    assert listing(tmp_path) == {"cwd"}


def test_backtest_announces_overrides_loudly(
    tmp_path: Path, cwd: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    config = config_copy(tmp_path, strategies="strategies:\n  ny_orb_fvg:\n    rr: 3.0\n")

    code = backtest(tmp_path / "reports", "--no-artifact", config_dir=config)

    out = capsys.readouterr().out
    assert code == 0
    assert "!!! NOT: ny_orb_fvg config/strategies.yaml değişiklikleriyle koşuyor" in out
    assert "rr=3.0" in out


@pytest.mark.parametrize(
    ("extra", "message"),
    [
        (["--strategy", "yok"], "bilinmeyen strateji: yok"),
        (["--oos-start", "2026-02-30"], "--oos-start YYYY-AA-GG"),
        (["--instrument", "US500.cash"], "VERIFY"),
        (["--instrument", "ES"], "ES"),
        (["--venue", "yok"], "venue profili bulunamadı: 'yok'"),
        (["--cost-factor", "-1"], "--cost-factor sonlu ve negatif olmayan"),
        (["--cost-factor", "nan"], "--cost-factor sonlu ve negatif olmayan"),
        (["--matrix", "--cost-free"], "--matrix yalnızca maliyetli"),
        (["--matrix", "--cost-factor", "2"], "--matrix yalnızca maliyetli"),
        (["--tf", "1D"], "desteklenmeyen zaman dilimi"),
    ],
)
def test_backtest_refusals_are_one_turkish_line_and_write_nothing(
    tmp_path: Path,
    cwd: Path,
    capsys: pytest.CaptureFixture[str],
    extra: list[str],
    message: str,
) -> None:
    code = backtest(tmp_path / "reports", *extra)

    captured = capsys.readouterr()
    assert code == 1
    assert captured.err.startswith("HATA: ")
    assert message in captured.err
    assert "Traceback" not in captured.err
    assert listing(tmp_path) == {"cwd"}


def test_backtest_refuses_missing_data(
    tmp_path: Path, cwd: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    arguments = ["backtest", "--strategy", "ny_orb_fvg", "--data", str(tmp_path / "yok.csv")]
    code = main([*arguments, "--reports-dir", str(tmp_path / "r")], clock=clock())

    assert code == 1
    assert "dosya bulunamadı" in capsys.readouterr().err
    assert listing(tmp_path) == {"cwd"}


def test_backtest_refuses_an_existing_trade_list_before_running(
    tmp_path: Path, cwd: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    trades = tmp_path / "trades.csv"
    trades.write_text("elle yazılmış\n", encoding="utf-8")

    code = backtest(tmp_path / "reports", "--trades-out", str(trades))

    captured = capsys.readouterr()
    assert code == 1
    assert "dosya zaten var, üzerine yazılmaz" in captured.err
    assert captured.out == ""
    assert trades.read_text(encoding="utf-8") == "elle yazılmış\n"
    assert listing(tmp_path) == {"cwd", "trades.csv"}


def test_backtest_refuses_a_config_without_strategies_yaml(
    tmp_path: Path, cwd: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    config = config_copy(tmp_path)
    (config / "strategies.yaml").unlink()

    code = backtest(tmp_path / "reports", config_dir=config)

    assert code == 1
    assert "strategies.yaml" in capsys.readouterr().err


def test_cost_factor_and_cost_free_are_mutually_exclusive(
    tmp_path: Path, cwd: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    with pytest.raises(SystemExit) as excinfo:
        backtest(tmp_path / "reports", "--cost-free", "--cost-factor", "2")

    assert excinfo.value.code == 2
    assert "--cost-factor" in capsys.readouterr().err


# --- parity ------------------------------------------------------------------------------


def tradingview_export(path: Path) -> Path:
    """A 'List of Trades' CSV written from the Python tv_ohlc run itself.

    A round trip, not parity evidence: it only shows the command wires the comparison
    (and exit 0) correctly. The real export's layout is VERIFY (parity.py).
    """
    config = load_backtest_config(CONFIG_DIR / "backtest.yaml")
    series = load_ohlcv(BARS)
    result = run(
        RunRequest(
            series=series,
            strategy_id="ny_orb_fvg",
            instrument=config.instrument(),
            base_costs=config.cost_model(),
            initial_capital=100_000.0,
            oos_start_ms=0,
            fill_model=FillModel.TV_OHLC,
        )
    )

    def stamp(ms: int) -> str:
        return dt.datetime.fromtimestamp(ms / 1000, NY).strftime("%Y-%m-%d %H:%M")

    rows = [
        "Trade #,Type,Signal,Date/Time,Price USD,Contracts,Profit USD,Profit %,"
        "Cum. Profit USD,Cum. Profit %,Run-up USD,Run-up %,Drawdown USD,Drawdown %"
    ]
    for number, trade in enumerate(result.engine.trades, 1):
        side = "Long" if trade.side.sign > 0 else "Short"
        signal = trade.comment or trade.exit_id
        rows.append(
            f"{number},Exit {side},{signal},{stamp(trade.exit_ts_ms)},{trade.exit_price:.2f},"
            f"{trade.qty:g},0,0,0,0,0,0,0,0"
        )
        rows.append(
            f"{number},Entry {side},{trade.setup},{stamp(trade.entry_ts_ms)},"
            f"{trade.entry_price:.2f},{trade.qty:g},,,,,,,,"
        )
    assert len(rows) > 2
    path.write_text("\n".join(rows) + "\n", encoding="utf-8")
    return path


def parity(reports: Path, tv: Path, *extra: str) -> int:
    arguments = ["parity", "--strategy", "ny_orb_fvg", "--data", str(BARS)]
    arguments += ["--tv-trades", str(tv), "--tv-tz", "America/New_York"]
    arguments += ["--config-dir", str(CONFIG_DIR), "--reports-dir", str(reports)]
    return main([*arguments, *extra], clock=clock())


def test_parity_passes_on_an_identical_list_and_writes_a_synthetic_artifact(
    tmp_path: Path, cwd: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    tv = tradingview_export(tmp_path / "tv.csv")
    reports = tmp_path / "reports"

    code = parity(reports, tv)

    out = capsys.readouterr().out
    assert code == 0
    assert "SENTETİK VERİ" in out
    assert out.rstrip().endswith("Parite: GEÇTİ")
    (written,) = reports.iterdir()
    artifact = load_artifact(written)
    assert isinstance(artifact, ParityArtifact)
    assert artifact.passed
    assert artifact.synthetic
    assert artifact.mismatched == 0


def test_parity_that_does_not_pass_exits_1(
    tmp_path: Path, cwd: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    code = parity(tmp_path / "reports", SAMPLE_TV)

    out = capsys.readouterr().out
    assert code == 1
    assert out.rstrip().endswith("Parite: GEÇMEDİ")
    (written,) = (tmp_path / "reports").iterdir()
    artifact = load_artifact(written)
    assert isinstance(artifact, ParityArtifact)
    assert not artifact.passed


@pytest.mark.parametrize(
    ("change", "message"),
    [
        (["--strategy", "all"], "parite tek bir strateji için"),
        (["--strategy", "yok"], "bilinmeyen strateji"),
        (["--warmup-bars", "-1"], "--warmup-bars negatif olamaz"),
        (["--tv-tz", "Mars/Olympus"], "Mars/Olympus"),
        (["--instrument", "US500.cash"], "VERIFY"),
    ],
)
def test_parity_errors_exit_2_and_write_nothing(
    tmp_path: Path,
    cwd: Path,
    capsys: pytest.CaptureFixture[str],
    change: list[str],
    message: str,
) -> None:
    code = parity(tmp_path / "reports", SAMPLE_TV, *change)

    captured = capsys.readouterr()
    assert code == 2
    assert captured.err.startswith("HATA: ")
    assert message in captured.err
    assert listing(tmp_path) == {"cwd"}


def test_parity_with_a_missing_export_exits_2(
    tmp_path: Path, cwd: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    assert parity(tmp_path / "reports", tmp_path / "yok.csv") == 2
    assert capsys.readouterr().err.startswith("HATA: ")


# --- montecarlo artifact -----------------------------------------------------------------


def montecarlo(trades: Path, reports: Path, *extra: str) -> int:
    arguments = ["montecarlo", "--trades", str(trades), "--venue", str(VENUE)]
    arguments += ["--risk-config", str(CONFIG_DIR / "risk.yaml"), "--r-is-net"]
    arguments += ["--paths", "200", "--max-days", "60", "--dd-days", "30"]
    return main([*arguments, "--reports-dir", str(reports), *extra], clock=clock())


def test_montecarlo_names_the_trade_lists_origin_and_writes_its_artifact(
    tmp_path: Path, cwd: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    trades = tmp_path / "trades.csv"
    assert backtest(tmp_path / "bt", "--no-artifact", "--trades-out", str(trades)) == 0
    capsys.readouterr()
    reports = tmp_path / "reports"

    code = montecarlo(trades, reports)

    out = capsys.readouterr().out
    assert code == 0
    origin = next(line for line in out.splitlines() if line.startswith("Köken       : "))
    assert "propbot backtest" in origin
    assert "ny_orb_fvg" in origin
    assert "SENTETİK VERİ" in origin
    # Placed in the input block, right before the venue line.
    lines = out.splitlines()
    assert lines[lines.index(origin) + 1].startswith("Venue       :")
    (written,) = reports.iterdir()
    assert f"Rapor yazıldı: {written}" in out
    artifact = load_artifact(written)
    assert isinstance(artifact, MonteCarloArtifact)
    assert artifact.synthetic
    assert artifact.provenance is not None


def test_montecarlo_of_a_list_without_origin_says_it_cannot_pass_g4(
    tmp_path: Path, cwd: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    trades = tmp_path / "eski.csv"
    trades.write_text(
        "entry_time,exit_time,r\n"
        "2026-01-05T14:36:00Z,2026-01-05T15:10:00Z,2\n"
        "2026-01-06T14:36:00Z,2026-01-06T15:10:00Z,-1\n",
        encoding="utf-8",
    )
    reports = tmp_path / "reports"

    code = montecarlo(trades, reports)

    out = capsys.readouterr().out
    assert code == 0
    assert "Köken       : bilinmiyor" in out
    assert "G4'ü GEÇTİ yapamaz" in out
    (written,) = reports.iterdir()
    artifact = load_artifact(written)
    assert isinstance(artifact, MonteCarloArtifact)
    assert artifact.provenance is None
    assert not artifact.synthetic


def test_montecarlo_without_artifact_writes_nothing(
    tmp_path: Path, cwd: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    trades = FIXTURES / "trades" / "synthetic_trades.csv"

    code = montecarlo(trades, tmp_path / "reports", "--no-artifact")

    out = capsys.readouterr().out
    assert code == 0
    assert "Köken       : bilinmiyor" in out
    assert "SENTETİK VERİ" in out  # the file name says so
    assert listing(tmp_path) == {"cwd"}


# --- gonogo ------------------------------------------------------------------------------


def gonogo(
    reports: Path,
    *extra: str,
    strategies: str = "ny_orb_fvg",
    now: Callable[[], dt.datetime] | None = None,
) -> int:
    arguments = ["gonogo", "--strategies", strategies, "--config-dir", str(FIXTURE_CONFIG)]
    arguments += ["--reports-dir", str(reports)]
    return main([*arguments, *extra], clock=clock() if now is None else now)


def test_gonogo_on_synthetic_runs_is_no_go_with_exit_1(
    tmp_path: Path, cwd: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    reports = tmp_path / "reports"
    assert backtest(reports) == 0
    capsys.readouterr()

    code = gonogo(reports)

    out = capsys.readouterr().out
    assert code == 1
    assert "Karar: NO-GO" in out
    assert "SENTETİK VERİ" in out
    for gate in ("G1", "G2", "G3", "G4", "G5", "G6", "G7"):
        assert f"  {gate}  " in out
    names = {path.name for path in reports.iterdir()}
    assert "go_no_go_20261118.md" in names
    (json_name,) = (n for n in names if n.startswith("go_no_go_") and n.endswith(".json"))
    artifact = load_artifact(reports / json_name)
    assert isinstance(artifact, GoNoGoArtifact)
    assert artifact.overall == "NO_GO"
    gates = {gate.id: gate.status for gate in artifact.gates}
    assert gates["G1"] == "FAIL"
    assert gates["G5"] == "NOT_EVALUABLE"


def test_gonogo_confirms_the_decision_run_by_its_configured_params(
    tmp_path: Path, cwd: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    reports = tmp_path / "reports"
    assert backtest(reports) == 0
    changed = config_copy(tmp_path, strategies="strategies:\n  ny_orb_fvg:\n    rr: 3.0\n")
    capsys.readouterr()

    # One clock for both: the second report replaces the first only when it is newer.
    shared = clock()
    same = gonogo(reports, now=shared)
    capsys.readouterr()
    same_md = (reports / "go_no_go_20261118.md").read_text(encoding="utf-8")
    other = gonogo(reports, "--config-dir", str(changed), now=shared)
    other_out = capsys.readouterr().out

    assert (same, other) == (1, 1)
    assert "config/strategies.yaml'ın bugünkü değerleriyle aynı değil" not in same_md
    # The run used rr=2.0; config now says 3.0: it can no longer be the decision run.
    md = (reports / "go_no_go_20261118.md").read_text(encoding="utf-8")
    assert "aynı değil: rr" in md
    assert "G2" in other_out


def test_gonogo_with_no_reports_is_no_go_not_an_error(
    tmp_path: Path, cwd: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    code = gonogo(tmp_path / "reports")

    assert code == 1
    assert "Karar: NO-GO" in capsys.readouterr().out


@pytest.mark.parametrize(
    ("extra", "strategies", "message"),
    [
        ([], "yok", "bilinmeyen strateji: yok"),
        ([], "ny_orb_fvg,,asian_sweep", "--strategies virgülle"),
        (["--venue", "yok"], "ny_orb_fvg", "venue profili bulunamadı"),
        (["--config-dir", "/yok/config"], "ny_orb_fvg", "config geçersiz"),
        # User decision of 2026-11-19 (open finding 72): the Master is judged by its legs
        # OR by its combined id, never both in one report (the same trades twice).
        (
            [],
            "master_portfolio,master_ny_orb_fvg",
            "master_portfolio ile bacağı master_ny_orb_fvg aynı raporda değerlendirilemez",
        ),
        (
            [],
            "ny_orb_fvg,master_asian_sweep,master_portfolio",
            "master_portfolio ile bacağı master_asian_sweep aynı raporda değerlendirilemez",
        ),
        # User decision of 2026-11-20 (open finding 87): one leg alone is refused here, not
        # left to G4, which could never pass on the script's two-leg list.
        (
            [],
            "master_ny_orb_fvg",
            "master_ny_orb_fvg kimliği master_portfolio betiğinin bir bacağıdır ve tek başına "
            "değerlendirilemez",
        ),
        ([], "ny_orb_fvg,master_asian_sweep", "eksik bacak: master_ny_orb_fvg"),
    ],
)
def test_gonogo_errors_exit_2_and_write_nothing(
    tmp_path: Path,
    cwd: Path,
    capsys: pytest.CaptureFixture[str],
    extra: list[str],
    strategies: str,
    message: str,
) -> None:
    code = gonogo(tmp_path / "reports", *extra, strategies=strategies)

    captured = capsys.readouterr()
    assert code == 2
    assert captured.err.startswith("HATA: ")
    assert message in captured.err
    assert listing(tmp_path) == {"cwd"}


def test_gonogo_accepts_master_leg_ids(
    tmp_path: Path, cwd: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    code = gonogo(tmp_path / "reports", strategies="master_ny_orb_fvg,master_asian_sweep")

    assert code == 1
    assert "master_ny_orb_fvg, master_asian_sweep" in capsys.readouterr().out


def test_gonogo_accepts_the_master_combined_id(
    tmp_path: Path, cwd: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    # User decision of 2026-11-19 (open finding 72): the combined run is accepted too.
    code = gonogo(tmp_path / "reports", strategies="master_portfolio")

    out = capsys.readouterr().out
    assert code == 1
    assert "stratejiler: master_portfolio" in out


def test_the_mix_refusal_says_which_ids_to_give_instead(
    tmp_path: Path, cwd: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    code = gonogo(tmp_path / "reports", strategies="master_ny_orb_fvg,master_portfolio")

    err = capsys.readouterr().err
    assert code == 2
    assert "aynı işlemler iki kez sayılır" in err
    assert "ya birleşik kimliği (master_portfolio)" in err
    # Written the way it is typed after --strategies (no space after the comma).
    assert "bacak kimliklerini (master_ny_orb_fvg,master_asian_sweep) verin" in err
    # A leg next to the stand-alone port is no mix (that port is another script), but it is
    # one leg alone: since the user's decision of 2026-11-20 (open finding 87) the command
    # refuses that too, with its own message, where it used to write a report (exit 1).
    assert gonogo(tmp_path / "reports", strategies="master_ny_orb_fvg,ny_orb_fvg") == 2
    err = capsys.readouterr().err
    assert "tek başına değerlendirilemez" in err
    assert "aynı raporda" not in err
    assert not (tmp_path / "reports").exists()


@pytest.mark.parametrize(
    ("strategies", "leg", "missing", "alone"),
    [
        ("master_ny_orb_fvg", "master_ny_orb_fvg", "master_asian_sweep", "ny_orb_fvg"),
        ("master_asian_sweep", "master_asian_sweep", "master_ny_orb_fvg", "asian_sweep"),
        # Next to other strategies, repeated or spaced, it is still one leg alone.
        ("ny_orb_fvg,master_asian_sweep", "master_asian_sweep", "master_ny_orb_fvg", "asian_sweep"),
        (
            "master_ny_orb_fvg, master_ny_orb_fvg",
            "master_ny_orb_fvg",
            "master_asian_sweep",
            "ny_orb_fvg",
        ),
        (
            "mtf_trend_cont , master_ny_orb_fvg,asian_sweep",
            "master_ny_orb_fvg",
            "master_asian_sweep",
            "ny_orb_fvg",
        ),
    ],
)
def test_a_single_master_leg_is_refused_before_any_gate_and_told_what_to_give(
    tmp_path: Path,
    cwd: Path,
    capsys: pytest.CaptureFixture[str],
    strategies: str,
    leg: str,
    missing: str,
    alone: str,
) -> None:
    # User decision of 2026-11-20 (open finding 87). Before it the command wrote a report
    # in which G4 could never pass (at best NOT_EVALUABLE, "değerlendirilmeyen: <other
    # leg>"): right, but a report nobody could use and a message that named neither way out.
    reports = tmp_path / "reports"
    assert backtest(reports, "--strategy", "master_portfolio") == 0
    before = sorted(path.name for path in reports.iterdir())
    capsys.readouterr()

    code = gonogo(reports, strategies=strategies)

    captured = capsys.readouterr()
    assert code == 2
    assert captured.out == ""
    assert sorted(path.name for path in reports.iterdir()) == before  # no report written
    err = captured.err.strip()
    assert len(err.splitlines()) == 1
    assert "Traceback" not in err
    assert err.startswith(
        f"HATA: --strategies: {leg} kimliği master_portfolio betiğinin bir bacağıdır ve tek "
        "başına değerlendirilemez"
    )
    assert f"eksik bacak: {missing};" in err
    # Both ways to judge the Master, and the stand-alone strategy if that was meant.
    # The first way is written as it is typed: pasted after --strategies it is accepted.
    assert "ya bütün bacakları (master_ny_orb_fvg,master_asian_sweep)" in err
    assert "ya da birleşik kimliği (master_portfolio) verin" in err
    assert err.endswith(f"Aynı motorun bağımsız stratejisini değerlendirmek için: {alone}")
    assert gonogo(reports, strategies="master_ny_orb_fvg,master_asian_sweep") == 1


@pytest.mark.parametrize(
    ("strategies", "shown"),
    [
        ("master_ny_orb_fvg,master_asian_sweep", "master_ny_orb_fvg, master_asian_sweep"),
        ("master_asian_sweep , master_ny_orb_fvg", "master_asian_sweep, master_ny_orb_fvg"),
        (
            "master_asian_sweep,ny_orb_fvg,master_ny_orb_fvg",
            "master_asian_sweep, ny_orb_fvg, master_ny_orb_fvg",
        ),
        ("master_portfolio", "master_portfolio"),
        ("master_portfolio,ny_orb_fvg", "master_portfolio, ny_orb_fvg"),
    ],
)
def test_both_legs_together_and_the_combined_id_are_still_judged(
    tmp_path: Path, cwd: Path, capsys: pytest.CaptureFixture[str], strategies: str, shown: str
) -> None:
    # The single-leg refusal must not reach the two accepted views (finding 72), in any
    # order and next to other strategies.
    code = gonogo(tmp_path / "reports", strategies=strategies)

    out = capsys.readouterr().out
    assert code == 1
    assert f"stratejiler: {shown}" in out


def test_a_leg_whose_name_points_at_no_stand_alone_strategy_gets_the_whole_list(
    tmp_path: Path, cwd: Path, capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch
) -> None:
    # The hint guesses the stand-alone id from the leg's name; where no id fits it must not
    # invent one. A second two-leg script whose legs follow no naming habit:
    pair = dataclasses.replace(
        STRATEGIES["master_portfolio"], strategy_id="pair", leg_ids=("pair_a", "pair_b")
    )
    monkeypatch.setattr(command, "STRATEGIES", {**STRATEGIES, "pair": pair})

    code = gonogo(tmp_path / "reports", strategies="pair_b")

    err = capsys.readouterr().err.strip()
    assert code == 2
    assert err.startswith("HATA: --strategies: pair_b kimliği pair betiğinin bir bacağıdır")
    assert "eksik bacak: pair_a;" in err
    assert "ya bütün bacakları (pair_a,pair_b) ya da birleşik kimliği (pair) verin" in err
    assert err.endswith(
        "Bağımsız stratejiler: asian_sweep, ict_silver_bullet, mr_bb_kc_rsi2, mtf_trend_cont, "
        "ny_orb_fvg"
    )


def test_a_leg_whose_name_fits_two_stand_alone_strategies_is_not_given_a_guess(
    tmp_path: Path, cwd: Path, capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch
) -> None:
    # "duo_ny_orb_fvg" ends with two stand-alone ids here (ny_orb_fvg and a shorter "fvg"):
    # naming either as "the same engine" would be a guess, so the list is given instead.
    duo = dataclasses.replace(
        STRATEGIES["master_portfolio"],
        strategy_id="duo",
        leg_ids=("duo_ny_orb_fvg", "duo_other"),
    )
    fvg = dataclasses.replace(STRATEGIES["ny_orb_fvg"], strategy_id="fvg", leg_ids=("fvg",))
    monkeypatch.setattr(command, "STRATEGIES", {**STRATEGIES, "duo": duo, "fvg": fvg})

    code = gonogo(tmp_path / "reports", strategies="duo_ny_orb_fvg")

    err = capsys.readouterr().err.strip()
    assert code == 2
    assert "Aynı motorun" not in err
    assert err.endswith(
        "Bağımsız stratejiler: asian_sweep, fvg, ict_silver_bullet, mr_bb_kc_rsi2, "
        "mtf_trend_cont, ny_orb_fvg"
    )


def test_without_any_stand_alone_strategy_the_refusal_ends_with_the_two_ways(
    tmp_path: Path, cwd: Path, capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch
) -> None:
    # Nothing to point at: the message must not end in an empty list.
    monkeypatch.setattr(command, "STRATEGIES", {"master_portfolio": STRATEGIES["master_portfolio"]})

    code = gonogo(tmp_path / "reports", strategies="master_asian_sweep")

    err = capsys.readouterr().err.strip()
    assert code == 2
    assert err.endswith("ya da birleşik kimliği (master_portfolio) verin")


def judged_files(reports: Path, gate_id: str) -> dict[str, object]:
    """Subject -> the file the newest Go/No-Go report judged it from, for one gate."""
    newest = max(
        p for p in reports.iterdir() if p.name.startswith("go_no_go_") and p.suffix == ".json"
    )
    artifact = load_artifact(newest)
    assert isinstance(artifact, GoNoGoArtifact)
    (gate,) = (g for g in artifact.gates if g.id == gate_id)
    parts = gate.details["parts"]
    assert isinstance(parts, list)
    found: dict[str, object] = {}
    for part in parts:
        assert isinstance(part, dict)
        values = part["values"]
        assert isinstance(values, dict)
        found[str(part["subject"])] = values.get("backtest_file", values.get("file"))
    return found


def test_the_master_is_judged_by_its_legs_or_by_its_combined_run_each_from_its_own_file(
    tmp_path: Path, cwd: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    reports = tmp_path / "reports"
    assert backtest(reports, "--strategy", "master_portfolio") == 0
    assert parity(reports, SAMPLE_TV, "--strategy", "master_portfolio") == 1
    stamp = "20261118T090000Z"
    shared = clock()

    assert gonogo(reports, strategies="master_ny_orb_fvg,master_asian_sweep", now=shared) == 1
    for gate_id in ("G1", "G2", "G3"):
        assert judged_files(reports, gate_id) == {
            "master_ny_orb_fvg": f"backtest_master_ny_orb_fvg_{stamp}.json",
            "master_asian_sweep": f"backtest_master_asian_sweep_{stamp}.json",
        }
    # A leg has no TradingView list of its own: G6 reads the script's parity report.
    assert judged_files(reports, "G6") == {
        "master_ny_orb_fvg": f"parity_master_portfolio_{stamp}.json",
        "master_asian_sweep": f"parity_master_portfolio_{stamp}.json",
    }

    assert gonogo(reports, strategies="master_portfolio", now=shared) == 1
    for gate_id in ("G1", "G2", "G3"):
        assert judged_files(reports, gate_id) == {
            "master_portfolio": f"backtest_master_portfolio_{stamp}.json"
        }
    assert judged_files(reports, "G6") == {
        "master_portfolio": f"parity_master_portfolio_{stamp}.json"
    }
    capsys.readouterr()


# --- config check ------------------------------------------------------------------------


def test_config_check_validates_backtest_and_strategies_yaml(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    config = config_copy(tmp_path, strategies="strategies:\n  ny_orb_fvg:\n    rr: 3.0\n")

    code = main(["config", "check", "--config-dir", str(config)], clock=clock())

    out = capsys.readouterr().out
    assert code == 0
    assert "backtest.yaml: GEÇERLİ" in out
    assert "NOT: instruments.US500.cash" in out
    assert "strategies.yaml: GEÇERLİ" in out
    assert "NOT: ny_orb_fvg Pine varsayılanlarından farklı parametrelerle koşar: rr=3.0" in out
    assert out.rstrip().endswith("Sonuç: GEÇERLİ")


def test_config_check_treats_missing_backtest_files_like_tv_alerts(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    config = config_copy(tmp_path)
    (config / "backtest.yaml").unlink()
    (config / "strategies.yaml").unlink()

    code = main(["config", "check", "--config-dir", str(config)], clock=clock())

    out = capsys.readouterr().out
    assert code == 0
    assert "backtest.yaml: yok" in out
    assert "strategies.yaml: yok" in out


@pytest.mark.parametrize(
    ("name", "text"),
    [
        ("strategies.yaml", "strategies:\n  yok: {}\n"),
        ("strategies.yaml", "strategies:\n  ny_orb_fvg:\n    rr: dört\n"),
        ("backtest.yaml", "default_instrument: MES\n"),
    ],
)
def test_config_check_rejects_a_broken_backtest_file(
    tmp_path: Path, capsys: pytest.CaptureFixture[str], name: str, text: str
) -> None:
    config = config_copy(tmp_path)
    (config / name).write_text(text, encoding="utf-8")

    code = main(["config", "check", "--config-dir", str(config)], clock=clock())

    out = capsys.readouterr().out
    assert code == 1
    assert f"{name}: HATA" in out
    assert out.rstrip().endswith("Sonuç: GEÇERSİZ (backtest.yaml / strategies.yaml)")


def test_backtest_artifacts_are_plain_json(tmp_path: Path, cwd: Path) -> None:
    reports = tmp_path / "reports"
    assert backtest(reports) == 0
    (written,) = reports.iterdir()
    payload = json.loads(written.read_text(encoding="utf-8"))
    assert payload["kind"] == "backtest"
    assert payload["params"]["rr"] == 2.0
