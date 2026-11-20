"""Golden trade lists: the Python engine pinned against ITSELF on SYNTHETIC data.

NOT TradingView evidence (ADR-0007 item 4). A failure here means the engine or a strategy
port changed what it trades; whether that change is wanted is a human decision, so the
test never rewrites a file (see tests/parity/golden/README.md).
"""

import io
from pathlib import Path

import pytest

from propbot.backtest.parity import GOLDEN_MARKER, diff_golden, golden_rows, read_golden
from propbot.strategies.registry import STRATEGIES
from support.golden import (
    FILL_MODELS,
    FIXTURE_FILES,
    GOLDEN_DIR,
    README,
    REGENERATE_COMMAND,
    GoldenCase,
    golden_cases,
    golden_instrument,
    golden_trades,
    header_note,
    main,
    readme_table,
    readme_table_section,
)

CASES = golden_cases()
SHOWN_DIFFERENCES = 15


def _failure(case: GoldenCase, differences: list[str]) -> str:
    shown = differences[:SHOWN_DIFFERENCES]
    more = len(differences) - len(shown)
    lines = [
        f"{case.file_name}: motorun işlem listesi altın dosyadan farklı "
        f"({len(differences)} fark; SENTETİK VERİ, TradingView kanıtı değil).",
        *shown,
    ]
    if more:
        lines.append(f"... ve {more} fark daha")
    lines.append(
        "Değişiklik AMAÇLANMIŞSA farkı inceleyip bilinçli olarak yeniden üretin: "
        f"{REGENERATE_COMMAND} (tests/parity/golden/README.md). Amaçlanmamışsa hatayı düzeltin."
    )
    return "\n".join(lines)


def test_cases_cover_every_strategy_fixture_and_fill_model() -> None:
    assert len(CASES) == len(STRATEGIES) * len(FIXTURE_FILES) * len(FILL_MODELS)
    assert {case.strategy_id for case in CASES} == set(STRATEGIES)
    assert len({case.file_name for case in CASES}) == len(CASES)


def test_golden_folder_holds_exactly_the_expected_files() -> None:
    present = {path.name for path in GOLDEN_DIR.iterdir() if path.is_file()}
    expected = {case.file_name for case in CASES} | {README.name}
    missing = sorted(expected - present)
    stale = sorted(present - expected)
    hint = f"Bilinçli yeniden üretme: {REGENERATE_COMMAND}"
    assert missing == [], f"eksik altın dosyalar: {missing}. {hint}"
    assert stale == [], f"beklenmeyen (eskimiş) dosyalar: {stale}. {hint}"


@pytest.mark.parametrize("case", CASES, ids=[case.id for case in CASES])
def test_engine_trades_match_the_committed_golden_file(case: GoldenCase) -> None:
    expected = read_golden(case.path)
    differences = diff_golden(
        expected, golden_trades(case), tick_size=golden_instrument().tick_size
    )
    assert differences == [], _failure(case, differences)


@pytest.mark.parametrize("case", CASES, ids=[case.id for case in CASES])
def test_golden_file_first_line_is_the_marker_and_names_the_run(case: GoldenCase) -> None:
    first = case.path.read_text(encoding="utf-8").partition("\n")[0]
    assert first.startswith(GOLDEN_MARKER)
    # Instrument, costs or the fixture changing must show up even when no trade moves.
    assert first == f"{GOLDEN_MARKER} {' '.join(header_note(case).split())}"
    assert "SENTETİK VERİ" in first
    assert "SYNTHETIC" in first
    assert f"strategy={case.strategy_id} " in first
    assert f"fill_model={case.fill_model.value} " in first


@pytest.mark.parametrize("fixture", FIXTURE_FILES, ids=[f.stem for f in FIXTURE_FILES])
def test_two_runs_of_the_same_case_are_identical(fixture: Path) -> None:
    for strategy_id in sorted(STRATEGIES):
        case = GoldenCase(strategy_id, fixture, FILL_MODELS[0])
        first, second = golden_rows(golden_trades(case)), golden_rows(golden_trades(case))
        assert first == second, case.file_name


def test_readme_table_matches_the_files_and_says_what_they_are_not() -> None:
    text = README.read_text(encoding="utf-8")
    assert readme_table_section(text) == readme_table(), (
        f"README tablosu dosyalarla uyuşmuyor; {REGENERATE_COMMAND} tabloyu yeniden yazar"
    )
    assert "SENTETİK VERİ" in text
    assert "TradingView kanıtı DEĞİLDİR" in text
    assert "docs/tv-verification-checklist.md" in text
    assert REGENERATE_COMMAND in text


def test_a_normal_test_run_never_writes_golden_files() -> None:
    # Only `main` writes; importing the module or calling the producers must not.
    before = {path: path.stat().st_mtime_ns for path in GOLDEN_DIR.iterdir()}
    golden_trades(CASES[0])
    header_note(CASES[0])
    readme_table()
    after = {path: path.stat().st_mtime_ns for path in GOLDEN_DIR.iterdir()}
    assert before == after


def test_regeneration_help_exits_cleanly_and_writes_nothing() -> None:
    out = io.StringIO()
    before = {path: path.stat().st_mtime_ns for path in GOLDEN_DIR.iterdir()}
    with pytest.raises(SystemExit) as exit_info:
        main(["--help"], out=out)
    assert exit_info.value.code == 0
    after = {path: path.stat().st_mtime_ns for path in GOLDEN_DIR.iterdir()}
    assert before == after
