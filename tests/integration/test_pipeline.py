"""The whole Faz 2 chain through `main([...])`, on a synthetic series it makes itself.

data synth -> data check -> backtest all (matrix, walk-forward, trade list) -> parity ->
montecarlo on that list -> gonogo -> the dashboard's analysis page. Everything is
SYNTHETIC, so the chain must end in NO-GO with every card carrying the badge (ADR-0007).
"""

import datetime as dt
import re
from collections.abc import Callable
from pathlib import Path

import pytest

from propbot.backtest.artifacts import (
    KINDS,
    BacktestArtifact,
    GoNoGoArtifact,
    ParityArtifact,
    all_artifacts,
)
from propbot.cli import main
from propbot.dashboard.analysis import KIND_TITLES, SYNTHETIC_BADGE, render_analysis
from support.paths import CONFIG_DIR, FIXTURES

START = dt.datetime(2026, 11, 18, 9, 0, tzinfo=dt.UTC)
# A hand-written parser sample: its comparison cannot pass, but it gives the page a card.
SAMPLE_TV = FIXTURES / "parity" / "sample_tv_basic_comma.csv"


def clock() -> Callable[[], dt.datetime]:
    """One clock for the whole chain: every step is a few seconds after the previous."""
    moments = iter(START + dt.timedelta(seconds=step) for step in range(100_000))
    return lambda: next(moments)


def listing(root: Path) -> set[str]:
    return {str(path.relative_to(root)) for path in root.rglob("*")}


def test_synthetic_chain_ends_in_no_go_and_every_card_says_synthetic(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    work = tmp_path / "cwd"
    work.mkdir()
    monkeypatch.chdir(work)
    now = clock()
    bars = tmp_path / "data" / "bars.csv"
    reports = tmp_path / "reports"
    trades = tmp_path / "trades.csv"
    config = ["--config-dir", str(CONFIG_DIR)]
    target = ["--reports-dir", str(reports)]

    synth = main(["data", "synth", "--out", str(bars), "--sessions", "12", "--start", "2026-03-02"])
    assert synth == 0
    assert "SENTETİK VERİ" in capsys.readouterr().out

    check = main(["data", "check", "--data", str(bars), *config, *target], clock=now)
    assert check == 0
    assert "Sonuç: TEMİZ" in capsys.readouterr().out

    backtest = main(
        [
            "backtest",
            "--strategy",
            "all",
            "--data",
            str(bars),
            "--oos-start",
            "2026-03-10",
            "--matrix",
            "--walk-forward",
            "--trades-out",
            str(trades),
            *config,
            *target,
        ],
        clock=now,
    )
    out = capsys.readouterr().out
    assert backtest == 0
    assert "SENTETİK VERİ" in out
    assert f"İşlem listesi yazıldı: {trades}" in out
    # User decision of 2026-11-19 (finding 82): the runs are sized at config/risk.yaml's
    # risk. (They are comparison runs all the same, for their own --oos-start.)
    assert "planlanan risk (config/risk.yaml risk_per_trade_pct)" in out
    assert "işlem başı risk Pine varsayılanı" not in out

    parity = main(
        [
            "parity",
            "--strategy",
            "ny_orb_fvg",
            "--data",
            str(bars),
            "--tv-trades",
            str(SAMPLE_TV),
            "--tv-tz",
            "America/New_York",
            *config,
            *target,
        ],
        clock=now,
    )
    out = capsys.readouterr().out
    assert parity == 1
    assert "Parite: GEÇMEDİ" in out
    # Parity alone keeps the script's own risk, and says so.
    assert "İşlem başı risk: %0,5 — betiğin kendi değeri (risk_pct)" in out

    montecarlo = main(
        [
            "montecarlo",
            "--trades",
            str(trades),
            "--venue",
            str(CONFIG_DIR / "venues" / "ftmo_2step_100k.yaml"),
            "--risk-config",
            str(CONFIG_DIR / "risk.yaml"),
            "--r-is-net",
            "--paths",
            "100",
            "--max-days",
            "60",
            "--dd-days",
            "20",
            *target,
        ],
        clock=now,
    )
    out = capsys.readouterr().out
    assert montecarlo == 0
    origin = next(line for line in out.splitlines() if line.startswith("Köken       : "))
    assert "propbot backtest" in origin
    assert "SENTETİK VERİ" in origin

    gonogo = main(["gonogo", "--strategies", "ny_orb_fvg,asian_sweep", *config, *target], clock=now)
    out = capsys.readouterr().out
    assert gonogo == 1
    assert "Karar: NO-GO" in out

    # Nothing outside the paths the commands were given.
    assert listing(work) == set()
    assert {p for p in listing(tmp_path) if not p.startswith(("reports/", "cwd"))} == {
        "data",
        "data/bars.csv",
        "reports",
        "trades.csv",
    }

    scan = all_artifacts(reports)
    assert scan.problems == ()
    kinds = {item.artifact.kind for item in scan.artifacts}
    assert kinds == set(KINDS)
    assert all(item.artifact.synthetic for item in scan.artifacts)
    runs = [i.artifact for i in scan.artifacts if isinstance(i.artifact, BacktestArtifact)]
    assert len(runs) == 5
    for run in runs:
        assert run.label == f"{run.strategy_id}-oos20260310"  # nothing about the risk
        risk_param = "risk_percent" if run.strategy_id == "mtf_trend_cont" else "risk_pct"
        assert run.params[risk_param] == 0.25  # config/risk.yaml, not the Pine 0.5
    (compared,) = (i.artifact for i in scan.artifacts if isinstance(i.artifact, ParityArtifact))
    assert compared.params is not None
    assert compared.params["risk_pct"] == 0.5
    (decision,) = (i.artifact for i in scan.artifacts if isinstance(i.artifact, GoNoGoArtifact))
    assert decision.overall == "NO_GO"
    gates = {gate.id: gate for gate in decision.gates}
    assert gates["G1"].status == "FAIL"
    assert "SENTETİK VERİ" in gates["G1"].evidence
    # Set aside for their OOS boundary alone: the risk is not what Go/No-Go objects to.
    assert "OOS başlangıcı 2026-03-10" in gates["G2"].evidence
    assert "işlem başı risk" not in gates["G2"].evidence
    assert gates["G5"].status == "NOT_EVALUABLE"
    # 100 paths is below the configured minimum and the list is synthetic: never PASS.
    assert gates["G4"].status != "PASS"
    assert (reports / "go_no_go_20261118.md").is_file()

    page = render_analysis(reports)
    for kind in KINDS:
        title = KIND_TITLES[kind]
        # "Backtest: ny_orb_fvg" for kinds that carry a strategy id.
        assert re.search(rf'aria-label="{re.escape(title)}(: [a-z_]+)?"', page), kind
    cards = re.findall(r'<section class="card an-card".*?</section>', page, flags=re.DOTALL)
    assert cards
    assert all(SYNTHETIC_BADGE in card for card in cards)
    assert "Eksik raporlar" not in page
