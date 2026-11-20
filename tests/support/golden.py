"""Golden trade lists: regression pins of the Python engine against ITSELF (SPEC §6.6).

These files are NOT TradingView evidence (ADR-0007 item 4). They are computed on the
committed SYNTHETIC fixtures, so they prove nothing about any strategy; they only make an
unintended change in the engine or a strategy port visible as a diff. Real parity needs a
TradingView "List of Trades" export (docs/tv-verification-checklist.md).

`golden_trades` is the one function that produces a pinned list; the test and the
regeneration entry point both call it, so they cannot drift apart. Regeneration is an
explicit act only:

    PYTHONPATH=tests .venv/bin/python -m support.golden

A normal test run never writes into tests/parity/golden/.
"""

import argparse
import sys
from collections.abc import Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Final, TextIO

from propbot.backtest.config import BacktestConfig, load_backtest_config
from propbot.backtest.engine import run_backtest
from propbot.backtest.model import CostModel, EngineConfig, FillModel, TradeRecord
from propbot.backtest.parity import read_golden, write_golden
from propbot.core.bars import BarSeries
from propbot.core.instrument import Instrument
from propbot.data.ohlcv import load_ohlcv
from propbot.data.sessions import session_last_flags
from propbot.strategies.registry import STRATEGIES
from support.paths import CONFIG_DIR, FIXTURES, REPO_ROOT

GOLDEN_DIR: Final = REPO_ROOT / "tests" / "parity" / "golden"
README: Final = GOLDEN_DIR / "README.md"
BACKTEST_CONFIG: Final = CONFIG_DIR / "backtest.yaml"
FIXTURE_FILES: Final = (
    FIXTURES / "ohlcv" / "synthetic_5m.csv",
    FIXTURES / "ohlcv" / "synthetic_1m.csv",
)
FILL_MODELS: Final = (FillModel.PESSIMISTIC, FillModel.TV_OHLC)
# The Pine scripts' `strategy(initial_capital = 100000)`.
INITIAL_CAPITAL: Final = 100_000.0
REGENERATE_COMMAND: Final = "PYTHONPATH=tests .venv/bin/python -m support.golden"
# The README's file table sits between these lines and is rewritten by `regenerate`, so the
# counts a reader sees cannot silently disagree with the files (a test compares them).
TABLE_START: Final = "<!-- golden-table:start (support.golden yazar; elle düzenlemeyin) -->"
TABLE_END: Final = "<!-- golden-table:end -->"


@dataclass(frozen=True, slots=True)
class GoldenCase:
    strategy_id: str
    fixture: Path
    fill_model: FillModel

    @property
    def file_name(self) -> str:
        return f"{self.strategy_id}__{self.fixture.stem}__{self.fill_model.value}.csv"

    @property
    def path(self) -> Path:
        return GOLDEN_DIR / self.file_name

    @property
    def id(self) -> str:
        return self.file_name.removesuffix(".csv")


def golden_cases() -> tuple[GoldenCase, ...]:
    """Every strategy in the registry x every committed fixture x every fill model."""
    return tuple(
        GoldenCase(strategy_id, fixture, fill_model)
        for strategy_id in sorted(STRATEGIES)
        for fixture in FIXTURE_FILES
        for fill_model in FILL_MODELS
    )


def _backtest_config() -> BacktestConfig:
    return load_backtest_config(BACKTEST_CONFIG)


def _instrument_and_costs(config: BacktestConfig) -> tuple[Instrument, CostModel]:
    # The default instrument (MES): the profile the Pine headers document. US500.cash still
    # has VERIFY nulls and is refused by the loader.
    return config.instrument(), config.cost_model()


def golden_instrument() -> Instrument:
    return _instrument_and_costs(_backtest_config())[0]


def _load_fixture(path: Path) -> BarSeries:
    series = load_ohlcv(path)
    # A pin on real data would need a provenance story this folder does not have.
    if not series.source.synthetic:
        raise ValueError(f"{path.name} is not a synthetic fixture")
    return series


def golden_trades(case: GoldenCase) -> tuple[TradeRecord, ...]:
    """The engine's trade list for one case: Pine default parameters, MES, config costs."""
    config = _backtest_config()
    instrument, costs = _instrument_and_costs(config)
    series = _load_fixture(case.fixture)
    flags = session_last_flags(
        series.bars, series.tf_seconds, min_gap_seconds=config.session.min_gap_minutes * 60
    )
    engine_config = EngineConfig(
        instrument=instrument,
        costs=costs,
        fill_model=case.fill_model,
        initial_capital=INITIAL_CAPITAL,
    )
    strategy = STRATEGIES[case.strategy_id].build(None)
    result = run_backtest(series, strategy, engine_config, session_last=flags)
    return tuple(result.trades)


def header_note(case: GoldenCase) -> str:
    """What the file pins; written after parity.GOLDEN_MARKER on the first line."""
    config = _backtest_config()
    instrument, costs = _instrument_and_costs(config)
    series = _load_fixture(case.fixture)
    return (
        f"SENTETİK VERİ — python-engine regression pin on SYNTHETIC data, not evidence; "
        f"strategy={case.strategy_id} params=pine-defaults "
        f"fixture={case.fixture.name} sha256={series.source.sha256} "
        f"fill_model={case.fill_model.value} "
        f"instrument={instrument.symbol} tick_size={instrument.tick_size:g} "
        f"point_value={instrument.point_value:g} "
        f"costs=commission_per_unit_side:{costs.commission_per_unit_side:g},"
        f"slippage_ticks:{costs.slippage_ticks:g},spread_points:{costs.spread_points:g} "
        f"initial_capital={INITIAL_CAPITAL:g}"
    )


def write_case(case: GoldenCase) -> int:
    trades = golden_trades(case)
    write_golden(trades, case.path, header_note=header_note(case))
    return len(trades)


def stale_files() -> tuple[Path, ...]:
    expected = {case.file_name for case in golden_cases()}
    return tuple(path for path in sorted(GOLDEN_DIR.glob("*.csv")) if path.name not in expected)


def readme_table() -> str:
    """The README's file table, built from the committed files (not from a fresh run)."""
    lines = [
        "| Dosya | Strateji | Fikstür | Dolum modeli | Satır (çıkış dolumu) |",
        "|---|---|---|---|---:|",
    ]
    for case in golden_cases():
        count = len(read_golden(case.path)) if case.path.is_file() else 0
        shown = f"{count}" if count else "0 (yalnızca başlık: bu koşuda işlem yok)"
        lines.append(
            f"| `{case.file_name}` | `{case.strategy_id}` | `{case.fixture.name}` "
            f"| `{case.fill_model.value}` | {shown} |"
        )
    return "\n".join(lines)


def _split_readme(text: str) -> tuple[str, str, str]:
    head, start, rest = text.partition(TABLE_START)
    table, end, tail = rest.partition(TABLE_END)
    if not (start and end):
        raise ValueError(f"{README.name}: table markers are missing")
    return head, table, tail


def readme_table_section(text: str) -> str:
    """The text between the table markers of the README, without surrounding blank lines."""
    return _split_readme(text)[1].strip("\n")


def _write_readme_table() -> None:
    head, _, tail = _split_readme(README.read_text(encoding="utf-8"))
    body = f"{head}{TABLE_START}\n{readme_table()}\n{TABLE_END}{tail}"
    README.write_text(body, encoding="utf-8")


def regenerate(out: TextIO) -> int:
    """Rewrite every golden file and remove stale ones; prints the trade count per file."""
    GOLDEN_DIR.mkdir(parents=True, exist_ok=True)
    for path in stale_files():
        path.unlink()
        out.write(f"silindi (artık beklenmiyor): {path.name}\n")
    for case in golden_cases():
        count = write_case(case)
        # Read back: a file the reader refuses must fail here, not in the next test run.
        if len(read_golden(case.path)) != count:
            raise RuntimeError(f"{case.file_name}: read-back row count differs")
        out.write(f"{case.file_name}: {count} satır\n")
    _write_readme_table()
    out.write(
        "Altın dosyalar ve README tablosu yeniden üretildi "
        "(SENTETİK VERİ; Python motorunun kendine karşı sabiti, TradingView kanıtı DEĞİL).\n"
    )
    return 0


def main(argv: Sequence[str] | None = None, out: TextIO = sys.stdout) -> int:
    parser = argparse.ArgumentParser(
        prog="support.golden",
        description=(
            "tests/parity/golden/ altın dosyalarını bilinçli olarak yeniden üretir "
            "(Python motorunun kendine karşı regresyon sabiti; TradingView kanıtı değildir)."
        ),
    )
    parser.parse_args(argv)
    return regenerate(out)


if __name__ == "__main__":
    raise SystemExit(main())
