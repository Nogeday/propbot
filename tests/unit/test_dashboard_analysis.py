"""Analysis screen: every artifact kind as a card, honesty badges, robustness, the real route."""

import datetime as dt
import json
from collections.abc import Iterator
from html.parser import HTMLParser
from pathlib import Path

import numpy as np
import pytest
from fastapi.testclient import TestClient

from propbot.backtest.analytics import trade_stats
from propbot.backtest.artifacts import (
    AnyArtifact,
    BacktestArtifact,
    CostMatrixArtifact,
    CostRow,
    DataValidationArtifact,
    Gate,
    GoNoGoArtifact,
    LoadedArtifact,
    ParityArtifact,
    WalkForwardArtifact,
    backtest_artifact,
    build_artifact,
    cost_cell,
    monte_carlo_artifact,
    walk_forward_artifact,
    write_artifact,
)
from propbot.backtest.model import CostModel, EngineConfig, EntryResult, ExitReason, FillModel
from propbot.backtest.montecarlo import (
    BreachReason,
    ChallengeRules,
    MonteCarloParams,
    PathOutcomes,
    PathStatus,
    summarize,
)
from propbot.backtest.walkforward import WalkForwardConfig, walk_forward
from propbot.core.bars import Bar, BarSeries, DataSource
from propbot.core.instrument import Instrument, InstrumentKind
from propbot.dashboard import analysis, pages
from propbot.dashboard.analysis import MAX_CARDS, REPORT_COMMANDS, render_analysis, render_card
from propbot.dashboard.app import DashboardDeps, create_app
from propbot.dashboard.live import StoreSource
from propbot.dashboard.security import SECURITY_HEADERS
from propbot.risk.loader import load_risk_config, load_venue_profile
from propbot.store.db import Store
from propbot.strategies.base import Side
from support.paths import CONFIG_FIXTURES

CREATED = dt.datetime(2026, 11, 18, 14, 30, 5, tzinfo=dt.UTC)
DAY_MS = 86_400_000
START_MS = 1_767_623_400_000  # 2026-01-05T14:30:00Z, Monday 09:30 New York
HOSTILE = '<script>alert("x")</script>'
SYNTHETIC_BADGE = "SENTETİK VERİ — kanıt değildir"
PORT = 8787


# --- builders ------------------------------------------------------------------------------


def entry(seq: int, r: float) -> EntryResult:
    start = START_MS + seq * DAY_MS
    return EntryResult(
        seq=seq,
        strategy_id="ny_orb_fvg",
        entry_id="L",
        setup="",
        side=Side.LONG,
        qty=2.0,
        entry_ts_ms=start,
        exit_ts_ms=start + 900_000,
        entry_price=5000.0,
        pnl=r * 250.0,
        risk_amount=250.0,
        r=r,
        mae_r=min(r, 0.0),
        reasons=(ExitReason.STOP,),
        complete=True,
    )


ENTRIES = [entry(i, r) for i, r in enumerate([2.0, -1.0, 1.0, -1.0, 2.0, 0.0])]
LOSERS = [entry(i, r) for i, r in enumerate([-1.0, -1.0, 0.5])]


def series(*, synthetic: bool = False) -> BarSeries:
    bars = tuple(Bar(START_MS + i * 60_000, 5000.0, 5001.0, 4999.0, 5000.5, 10.0) for i in range(3))
    source = DataSource(
        name="mes_1m.csv", format="generic", sha256="ab" * 32, synthetic=synthetic, bar_tz="UTC"
    )
    return BarSeries(symbol="MES1!", tf_seconds=60, bars=bars, source=source)


def engine_config(
    *, free: bool = False, fill_model: FillModel = FillModel.PESSIMISTIC
) -> EngineConfig:
    return EngineConfig(
        instrument=Instrument(
            symbol="MES1!",
            kind=InstrumentKind.FUTURES,
            tick_size=0.25,
            point_value=5.0,
            qty_step=1.0,
            min_qty=1.0,
        ),
        costs=CostModel(0.0, 0.0) if free else CostModel(0.62, 1.0),
        initial_capital=50_000.0,
        fill_model=fill_model,
    )


def make_backtest(
    *,
    label: str = "orb",
    free: bool = False,
    synthetic: bool = False,
    entries: list[EntryResult] | None = None,
    strategy_id: str = "ny_orb_fvg",
    notes: tuple[str, ...] = (),
    fill_model: FillModel = FillModel.PESSIMISTIC,
    cost_factor: float | None = None,
) -> AnyArtifact:
    return backtest_artifact(
        label=label,
        created=CREATED,
        strategy_id=strategy_id,
        params={"orMinutes": 15, "useBias": True, "bias": "Close", "rr": 2.0, "none": None},
        config=engine_config(free=free, fill_model=fill_model),
        entries=ENTRIES if entries is None else entries,
        oos_start_ms=START_MS + 3 * DAY_MS,
        dropped_entries=1,
        data=series(synthetic=synthetic),
        synthetic=synthetic,
        notes=notes,
        cost_factor=cost_factor,
    )


def make_cost_matrix() -> CostMatrixArtifact:
    good = trade_stats(ENTRIES, initial_capital=50_000.0)
    bad = trade_stats(LOSERS, initial_capital=50_000.0)
    return build_artifact(
        CostMatrixArtifact,
        label="all",
        created=CREATED,
        synthetic=False,
        data=None,
        sample="oos",
        factors=(1.0, 1.5, 2.0),
        rows=(
            CostRow(strategy_id="ny_orb_fvg", cells=tuple(cost_cell(f, good) for f in (1, 1.5, 2))),
            CostRow(strategy_id="asian_sweep", cells=tuple(cost_cell(f, bad) for f in (1, 1.5, 2))),
        ),
    )


def make_walk_forward(*, oos_days: int = 1, last_day: int = 8) -> AnyArtifact:
    result = walk_forward(
        ENTRIES,
        WalkForwardConfig(is_days=2, oos_days=oos_days, step_days=oos_days),
        first_bar_ms=START_MS,
        last_bar_ms=START_MS + last_day * DAY_MS,
        initial_capital=50_000.0,
    )
    return walk_forward_artifact(
        result, label="orb", created=CREATED, strategy_id="ny_orb_fvg", data=None, synthetic=False
    )


def outcomes(drawdowns: np.ndarray | None = None) -> PathOutcomes:
    passed, breached, running = PathStatus.PASSED, PathStatus.BREACHED, PathStatus.RUNNING
    return PathOutcomes(
        n_phases=2,
        status=np.array([passed] * 4 + [breached] * 3 + [running] * 3, dtype=np.int8),
        phase_days=np.array(
            [[5, 7]] * 4 + [[6, -1], [-1, -1], [-1, -1]] + [[-1, -1]] * 3, dtype=np.int32
        ),
        breach_phase=np.array([-1] * 4 + [1, 0, 0] + [-1] * 3, dtype=np.intp),
        breach_reason=np.array(
            [0] * 4
            + [BreachReason.MAX_LOSS, BreachReason.DAILY_LOSS, BreachReason.DAILY_LOSS]
            + [0] * 3,
            dtype=np.int8,
        ),
        max_drawdown_pct=(
            np.arange(10, dtype=np.float64) * 0.75 if drawdowns is None else drawdowns
        ),
    )


def make_monte_carlo(
    *, synthetic: bool = False, venue: str = "FTMO 2-Step 100k", drawdowns: np.ndarray | None = None
) -> AnyArtifact:
    paths = outcomes(drawdowns)
    return monte_carlo_artifact(
        summarize(paths),
        paths,
        label="ftmo",
        created=CREATED,
        params=MonteCarloParams(risk_pct=0.5, n_paths=10, seed=7),
        rules=ChallengeRules(
            targets_pct=(10.0, 5.0), daily_loss_limit_pct=5.0, max_loss_pct=10.0, min_trading_days=4
        ),
        trades_file="/somewhere/reports/trades.csv",
        n_trades=6,
        cost_r=0.05,
        venue=venue,
        synthetic=synthetic,
    )


def gate(gate_id: str, status: str, evidence: str = "kanıt") -> Gate:
    return Gate(id=gate_id, title=f"Kapı {gate_id}", status=status, evidence=evidence, details={})  # type: ignore[arg-type]


def make_go_no_go(gates: tuple[Gate, ...], overall: str = "NO_GO") -> GoNoGoArtifact:
    return build_artifact(
        GoNoGoArtifact,
        label="haftalik",
        created=CREATED,
        synthetic=False,
        data=None,
        gates=gates,
        overall=overall,
        reasons=("G1 kanıt bekliyor",),
    )


def make_data_validation(*, clean: bool = False) -> AnyArtifact:
    return build_artifact(
        DataValidationArtifact,
        label="mes",
        created=CREATED,
        synthetic=False,
        data=series(),
        clean=clean,
        report={"gaps": [{"from": "2026-01-05T15:00:00Z", "minutes": 3}], "duplicates": 0},
    )


def make_parity(*, passed: bool = True, rate: float = 0.98) -> AnyArtifact:
    # The artifact model refuses a rate that disagrees with its counts: derive them.
    matched = round(rate * 100)
    python_only = (100 - matched) // 2
    return build_artifact(
        ParityArtifact,
        label="orb",
        created=CREATED,
        synthetic=False,
        data=None,
        strategy_id="ny_orb_fvg",
        matched=matched,
        python_only=python_only,
        tradingview_only=100 - matched - python_only,
        match_rate=matched / 100,
        threshold=0.98,
        passed=passed,
        mismatches=({"side": "python", "entry_utc": "2026-01-05T14:31:00Z"},),
        sources=("list_of_trades.csv", "mes_1m.csv"),
    )


# --- page checks ---------------------------------------------------------------------------


class Collector(HTMLParser):
    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.tags: list[tuple[str, dict[str, str | None]]] = []
        self.text: list[str] = []

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        self.tags.append((tag, dict(attrs)))

    def handle_startendtag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        self.tags.append((tag, dict(attrs)))

    def handle_data(self, data: str) -> None:
        self.text.append(data)


def parse(document: str) -> Collector:
    collector = Collector()
    collector.feed(document)
    collector.close()
    return collector


def assert_clean(document: str) -> str:
    """CSP-safe markup; returns the visible text."""
    collector = parse(document)
    for tag, attrs in collector.tags:
        assert tag not in ("style", "iframe", "object", "img")
        assert "style" not in attrs
        assert not any(name.startswith("on") for name in attrs), (tag, attrs)
        if tag == "script":
            assert attrs.get("src"), "inline <script> is blocked by the CSP"
        for value in attrs.values():
            assert not (value or "").strip().lower().startswith("javascript:")
            assert "http://" not in (value or "").replace("http://www.w3.org/2000/svg", "")
    return "".join(collector.text)


def page(reports: Path) -> tuple[str, str]:
    document = pages.analysis_page(render_analysis(reports))
    return document, assert_clean(document)


def write(tmp_path: Path, *artifacts: AnyArtifact) -> Path:
    for artifact in artifacts:
        write_artifact(artifact, tmp_path)
    return tmp_path


def cards(document: str) -> int:
    return document.count('class="card an-card"')


# --- empty states --------------------------------------------------------------------------


@pytest.mark.parametrize("make_dir", [False, True])
def test_missing_or_empty_reports_dir_shows_how_to_produce_each_report(
    tmp_path: Path, make_dir: bool
) -> None:
    reports = tmp_path / "reports"
    if make_dir:
        reports.mkdir()
    _, text = page(reports)

    assert "Henüz rapor yok" in text
    for title in analysis.KIND_TITLES.values():
        assert title in text
    for command in REPORT_COMMANDS.values():
        assert command in text
    assert str(reports) in text


def test_reports_path_is_escaped(tmp_path: Path) -> None:
    document, text = page(tmp_path / HOSTILE)
    assert "<script>alert" not in document
    assert HOSTILE in text


def test_reports_path_that_is_a_file_renders_the_empty_state(tmp_path: Path) -> None:
    target = tmp_path / "reports"
    target.write_text("not a directory")
    _, text = page(target)
    assert "Henüz rapor yok" in text


# --- one card per kind ---------------------------------------------------------------------


def test_monte_carlo_card(tmp_path: Path) -> None:
    document, text = page(write(tmp_path, make_monte_carlo()))

    assert "Monte Carlo prop simülasyonu" in text
    for label in ("Faz 1'i geçme", "İki fazı geçme", "İhlal", "Sonuçlanmayan (ufuk doldu)"):
        assert label in text
    assert "%40,0" in text  # P(both phases) = 4 / 10, written next to its bar
    assert "G4 eşiği ≥ %60" in text
    assert "G4 eşiği ≤ %20" in text
    assert "günlük kayıp" in text  # breach table
    assert "toplam kayıp" in text
    assert "Geçme süresi" in text
    assert "firma toplam kayıp sınırı %10,00" in text  # the histogram's max-loss line
    assert "medyan" in text
    assert "%99 dilim" in text
    for value in ("trades.csv", "FTMO 2-Step 100k", "%0,50", "0,050 R / işlem", "Tohum"):
        assert value in text
    assert "/somewhere" not in text  # only the file name is stored and shown
    assert document.count("<svg") >= 2
    assert SYNTHETIC_BADGE not in text
    # The page never says G4 passed: that is the Go/No-Go report's job.
    assert "GEÇTİ" not in text


def test_monte_carlo_with_identical_drawdowns_renders(tmp_path: Path) -> None:
    artifact = make_monte_carlo(drawdowns=np.full(10, 2.5))
    document, text = page(write(tmp_path, artifact))
    assert "kart çizilemedi" not in text
    assert cards(document) == 1


def test_backtest_card(tmp_path: Path) -> None:
    document, text = page(write(tmp_path, make_backtest()))

    assert "Backtest: ny_orb_fvg" in text
    for header in ("Tümü", "IS", "OOS", "t-istatistiği", "Profit factor", "Maks. düşüş (R)"):
        assert header in text
    assert "OOS/IS beklenti oranı (G3)" in text
    assert "OOS başlangıcı" in text  # the equity chart's boundary marker
    for table in ("Hafta günü", "Saat (NY)", "Ay"):
        assert table in text
    assert "mes_1m.csv" in text  # data provenance
    assert "18.11.2026 17:30 (TR)" in text  # when it was produced, on the user's clock
    assert "MALİYETSİZ KOŞU" not in text
    assert 'class="cellbar"' in document


def test_cost_free_backtest_carries_the_red_warning(tmp_path: Path) -> None:
    document, text = page(write(tmp_path, make_backtest(free=True)))
    assert 'class="banner bad"' in document
    assert "MALİYETSİZ KOŞU — maliyetler sıfır" in text


PESS = FillModel.PESSIMISTIC
TV = FillModel.TV_OHLC
COMPARISON = "KARŞILAŞTIRMA KOŞUSU (karar koşusu değil): "
DECISION = "karar koşusu"


@pytest.mark.parametrize(
    ("fill_model", "free", "factor", "kind", "reasons"),
    [
        (PESS, False, 1.0, DECISION, ()),
        (PESS, True, 1.0, COMPARISON, ("maliyetsiz koşu",)),
        (PESS, False, 1.5, COMPARISON, ("maliyet çarpanı 1,5× (1× değil)",)),
        (PESS, False, 0.0, COMPARISON, ("maliyet çarpanı 0× (1× değil)",)),
        (TV, False, 1.0, COMPARISON, ("dolum modeli kötümser değil",)),
        (
            TV,
            True,
            2.0,
            COMPARISON,
            ("dolum modeli kötümser değil", "maliyetsiz koşu", "maliyet çarpanı 2× (1× değil)"),
        ),
        (PESS, False, None, "bilinmiyor (", ("maliyet çarpanı rapor dosyasında kayıtlı değil",)),
        (PESS, True, None, COMPARISON, ("maliyetsiz koşu", "maliyet çarpanı kayıtlı değil")),
        (TV, False, None, COMPARISON, ("dolum modeli kötümser değil",)),
    ],
)
def test_backtest_run_kind_is_a_decision_run_only_when_every_condition_holds(
    tmp_path: Path,
    fill_model: FillModel,
    free: bool,
    factor: float | None,
    kind: str,
    reasons: tuple[str, ...],
) -> None:
    # The fill model alone used to label a cost-free or x1.5 run "karar koşusu".
    artifact = make_backtest(free=free, fill_model=fill_model, cost_factor=factor)
    document, text = page(write(tmp_path, artifact))
    expected_model = (
        "kötümser" if fill_model is PESS else "TradingView emülasyonu (yalnızca parite)"
    )
    assert f"<dt>Dolum modeli</dt><dd>{expected_model}</dd>" in document
    row = document.split("<dt>Koşu türü</dt><dd>", 1)[1].split("</dd>", 1)[0]
    assert row.startswith(kind)
    for reason in reasons:
        assert reason in row
    decision = row == DECISION
    assert decision is (kind == DECISION)
    # Nowhere else on the card may claim a decision run.
    assert ("karar koşusu" in text.replace("karar koşusu değil", "")) is decision
    # The page still grades nothing itself.
    assert "GEÇTİ" not in text
    assert "KALDI" not in text


def test_backtest_run_kind_repeats_the_runners_note_on_what_the_fields_cannot_show(
    tmp_path: Path,
) -> None:
    # Open finding 86: a run at another OOS start or at the script's own risk has the
    # pessimistic model, real costs and factor 1.0, so the fields alone said "karar koşusu"
    # right above the runner's note saying the opposite.
    note = COMPARISON + "işlem başı risk Pine varsayılanı (%0,5)"
    artifact = make_backtest(cost_factor=1.0, notes=(note,))
    document, text = page(write(tmp_path, artifact))
    row = document.split("<dt>Koşu türü</dt><dd>", 1)[1].split("</dd>", 1)[0]
    assert row == note
    assert "karar koşusu" not in text.replace("karar koşusu değil", "")


def test_backtest_run_kind_takes_only_the_runners_own_note_as_its_statement(
    tmp_path: Path,
) -> None:
    # A note that merely mentions the phrase is not the runner's statement: the row must
    # not turn a decision run into a comparison run because of free text.
    artifact = make_backtest(cost_factor=1.0, notes=("bkz. " + COMPARISON + "başka koşu",))
    document, _ = page(write(tmp_path, artifact))
    row = document.split("<dt>Koşu türü</dt><dd>", 1)[1].split("</dd>", 1)[0]
    assert row == DECISION


def test_backtest_with_zero_trades_renders(tmp_path: Path) -> None:
    document, text = page(write(tmp_path, make_backtest(entries=[])))
    assert "kart çizilemedi" not in text
    assert "çizilecek nokta yok" in document
    assert "İşlem yok." in text


def test_cost_matrix_card_marks_cells_below_zero(tmp_path: Path) -> None:
    document, text = page(write(tmp_path, make_cost_matrix()))

    assert "Maliyet matrisi" in text
    assert "yalnızca OOS" in text
    for factor in ("1×", "1,5×", "2×"):
        assert factor in text
    # asian_sweep loses: three cells below zero, ny_orb_fvg none.
    assert text.count("sıfırın altında") == 3
    assert document.count("<svg") == 2  # one small multiple per strategy
    assert "c-neg" in document


def test_walk_forward_card(tmp_path: Path) -> None:
    _, text = page(write(tmp_path, make_walk_forward()))

    assert "Walk-forward: ny_orb_fvg" in text
    assert "Parametre araması yapılmadı" in text
    assert "OOS'u pozitif katların payı" in text
    assert "Birleşik OOS" in text
    assert "Verimlilik oranı (OOS/IS)" in text
    assert "IS ortalama R" in text
    assert "OOS ortalama R" in text
    assert "K1" in text


def test_walk_forward_with_a_single_fold_renders(tmp_path: Path) -> None:
    artifact = make_walk_forward(oos_days=4, last_day=8)
    assert isinstance(artifact, WalkForwardArtifact)
    assert len(artifact.folds) == 1
    _, text = page(write(tmp_path, artifact))
    assert "kart çizilemedi" not in text
    assert "K1" in text


def test_go_no_go_card_shows_each_status_as_the_artifact_says(tmp_path: Path) -> None:
    gates = (
        gate("G1", "PASS"),
        gate("G2", "FAIL"),
        gate("G3", "NOT_EVALUABLE"),
        gate("G4", "PASS", evidence="P(tüm fazlar) %70"),
    )
    document, text = page(write(tmp_path, make_go_no_go(gates)))

    assert text.count("GEÇTİ") == 2
    assert text.count("KALDI") == 1
    assert text.count("DEĞERLENDİRİLEMEDİ") == 1
    assert text.count("RAPORDA YOK") == 3  # G5-G7 are not in the file
    assert "P(tüm fazlar) %70" in text
    assert "NO-GO" in text
    assert '<span class="badge ok">GO</span>' not in document


def test_go_verdict_only_when_the_artifact_says_go(tmp_path: Path) -> None:
    gates = tuple(gate(f"G{i}", "PASS") for i in range(1, 8))
    document, text = page(write(tmp_path, make_go_no_go(gates, overall="GO")))
    assert '<span class="badge ok">GO</span>' in document
    assert "NO-GO" not in text


def test_parity_and_data_validation_cards(tmp_path: Path) -> None:
    write(tmp_path, make_parity(passed=False, rate=0.5), make_data_validation(clean=False))
    _, text = page(tmp_path)
    assert "Parite: ny_orb_fvg" in text
    assert "KALDI" in text
    assert "GEÇTİ" not in text
    assert "%50,0" in text
    assert "Uyuşmazlıklar (1; ilk 1 gösteriliyor)" in text
    assert "Veri doğrulama" in text
    assert "sorun var" in text
    assert '"minutes": 3' in text


def test_passed_parity_and_clean_data(tmp_path: Path) -> None:
    write(tmp_path, make_parity(), make_data_validation(clean=True))
    _, text = page(tmp_path)
    assert "GEÇTİ" in text
    assert "temiz" in text


def test_every_kind_on_one_page_in_decision_order(tmp_path: Path) -> None:
    write(
        tmp_path,
        make_parity(),
        make_data_validation(),
        make_walk_forward(),
        make_cost_matrix(),
        make_backtest(),
        make_monte_carlo(),
        make_go_no_go((gate("G1", "FAIL"),)),
    )
    document, text = page(tmp_path)
    assert cards(document) == 7
    positions = [text.index(title) for title in ("Go/No-Go", "Monte Carlo", "Backtest:")]
    assert positions == sorted(positions)
    assert "Henüz rapor yok" not in text
    assert "Eksik raporlar" not in text


# --- honesty -------------------------------------------------------------------------------


def test_synthetic_artifact_carries_the_badge_on_its_own_card(tmp_path: Path) -> None:
    write(tmp_path, make_monte_carlo(synthetic=True), make_parity())
    document, text = page(tmp_path)
    assert text.count(SYNTHETIC_BADGE) == 1
    synthetic_card = document.split('aria-label="Monte Carlo prop simülasyonu"')[1].split(
        "</section>"
    )[0]
    assert SYNTHETIC_BADGE in synthetic_card
    assert "1 tanesi sentetik veriyle" in text


def test_synthetic_backtest_badge(tmp_path: Path) -> None:
    _, text = page(write(tmp_path, make_backtest(synthetic=True)))
    assert SYNTHETIC_BADGE in text


# --- robustness ----------------------------------------------------------------------------


def test_broken_file_is_listed_and_the_rest_still_renders(tmp_path: Path) -> None:
    write(tmp_path, make_parity())
    (tmp_path / "backtest_bozuk_20260101T000000Z.json").write_text("{kesik", encoding="utf-8")
    duplicate = tmp_path / "monte_carlo_cift_20260101T000000Z.json"
    key = "<script>alert(1)</script>"
    duplicate.write_text(json.dumps({"a": 1})[:-1] + f', "{key}": 1, "{key}": 2}}')

    document, text = page(tmp_path)

    assert "Okunamayan raporlar" in text
    assert "backtest_bozuk_20260101T000000Z.json: geçerli JSON değil" in text
    assert f"yinelenen alan: '{key}'" in text
    assert "<script>alert" not in document
    assert "Parite: ny_orb_fvg" in text


def test_hostile_texts_are_escaped_everywhere(tmp_path: Path) -> None:
    write(
        tmp_path,
        make_backtest(strategy_id=HOSTILE, notes=(HOSTILE,)),
        make_monte_carlo(venue=HOSTILE),
        make_go_no_go((gate("G1", "FAIL", evidence=HOSTILE),)),
    )
    document, text = page(tmp_path)
    assert "<script>alert" not in document
    assert text.count(HOSTILE) >= 4


def test_control_characters_never_reach_the_svg(tmp_path: Path) -> None:
    _, text = page(write(tmp_path, make_backtest(strategy_id="a\x1bb\x00c")))
    assert "\x1b" not in text
    assert "\x00" not in text


def test_very_long_strategy_name_is_clipped_in_charts(tmp_path: Path) -> None:
    long_name = "x" * 256
    document, text = page(write(tmp_path, make_backtest(strategy_id=long_name, label="u" * 64)))
    assert long_name in text  # the table / heading keep it (CSS wraps it)
    svg = document[document.index("<svg") : document.index("</svg>")]
    assert long_name not in svg.split("<title>")[0]
    assert "kart çizilemedi" not in text


def test_extreme_and_missing_numbers_render_without_breaking(tmp_path: Path) -> None:
    artifact = make_backtest()
    assert isinstance(artifact, BacktestArtifact)
    stats = artifact.stats_all.model_copy(
        update={"mean_r": 1e300, "t_stat": None, "profit_factor": None, "total_r": -1e300}
    )
    curve = tuple(
        point.model_copy(update={"cum_r": value})
        for point, value in zip(artifact.equity_curve, (1e300, -1e300, 0.0), strict=False)
    )
    extreme = artifact.model_copy(
        update={"stats_all": stats, "g3_ratio": None, "equity_curve": curve}
    )
    html = render_card(LoadedArtifact("backtest_orb_20261118T143005Z.json", extreme))
    text = assert_clean(html)
    assert "kart çizilemedi" not in text
    assert "1,00e+300" in text
    assert "hesaplanamadı" in text
    assert max(len(token) for token in text.split()) < 300


def test_number_of_cards_is_bounded(tmp_path: Path) -> None:
    for index in range(MAX_CARDS + 15):
        write(tmp_path, make_parity().model_copy(update={"label": f"p{index:03d}"}))
    document, text = page(tmp_path)
    assert cards(document) == MAX_CARDS
    assert "+15 rapor daha gösterilmedi" in text


def test_a_card_that_fails_to_render_becomes_an_error_card(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    def boom(_artifact: object) -> str:
        raise ZeroDivisionError

    monkeypatch.setattr(analysis, "parity_body", boom)
    write(tmp_path, make_parity(), make_data_validation())
    _, text = page(tmp_path)
    assert "kart çizilemedi (ZeroDivisionError)" in text
    assert "Veri doğrulama" in text


def test_missing_kinds_are_listed_with_their_commands(tmp_path: Path) -> None:
    _, text = page(write(tmp_path, make_parity()))
    assert "Eksik raporlar" in text
    assert REPORT_COMMANDS["monte_carlo"] in text
    assert "Henüz rapor yok" not in text


# --- through the real app ------------------------------------------------------------------


@pytest.fixture
def store() -> Iterator[Store]:
    with Store(":memory:") as opened:
        yield opened


def test_analysis_route_serves_the_cards_with_the_security_headers(
    tmp_path: Path, store: Store
) -> None:
    write(tmp_path, make_monte_carlo(synthetic=True), make_backtest(free=True))
    deps = DashboardDeps(
        source=StoreSource(None, store=store),
        risk=load_risk_config(CONFIG_FIXTURES / "valid" / "risk.yaml"),
        venue=load_venue_profile(CONFIG_FIXTURES / "valid" / "venues" / "sample_2step.yaml"),
        venue_name="sample_2step",
        commands=None,
        unavailable_reason="test: salt-okunur",
        clock=lambda: START_MS,
        reports_dir=tmp_path,
        port=PORT,
        csrf_token="fixed-test-csrf-token_0123456789abcdefghijkl",
    )
    client = TestClient(create_app(deps), base_url=f"http://127.0.0.1:{PORT}")

    response = client.get("/analiz")

    assert response.status_code == 200
    assert response.headers["content-type"].startswith("text/html")
    for name, value in SECURITY_HEADERS.items():
        assert response.headers[name] == value
    text = assert_clean(response.text)
    assert SYNTHETIC_BADGE in text
    assert "MALİYETSİZ KOŞU" in text
    assert client.get("/analiz", headers={"Host": "evil.example:8787"}).status_code != 200
