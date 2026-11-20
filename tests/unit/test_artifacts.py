"""JSON report artifacts: builders, schema rules, atomic writing and defensive reading."""

import datetime as dt
import json
import os
from pathlib import Path

import numpy as np
import pytest

from propbot.backtest import artifacts
from propbot.backtest.analytics import trade_stats
from propbot.backtest.artifacts import (
    COST_FREE_NOTE,
    MAX_ARTIFACT_BYTES,
    SYNTHETIC_NOTE,
    AnyArtifact,
    ArtifactError,
    CostMatrixArtifact,
    CostRow,
    DataValidationArtifact,
    Gate,
    GoNoGoArtifact,
    ParityArtifact,
    artifact_file_name,
    backtest_artifact,
    build_artifact,
    cost_cell,
    data_info,
    latest_artifacts,
    load_artifact,
    monte_carlo_artifact,
    utc_stamp,
    walk_forward_artifact,
    write_artifact,
)
from propbot.backtest.model import CostModel, EngineConfig, EntryResult, ExitReason
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
from propbot.strategies.base import Side

CREATED = dt.datetime(2026, 11, 18, 14, 30, 5, tzinfo=dt.UTC)
DAY_MS = 86_400_000
START_MS = 1_767_623_400_000  # 2026-01-05T14:30:00Z, Monday 09:30 New York


def entry(seq: int, r: float, *, complete: bool = True) -> EntryResult:
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
        complete=complete,
    )


ENTRIES = [entry(i, r) for i, r in enumerate([2.0, -1.0, 1.0, -1.0, 2.0, 0.0])] + [
    entry(6, -1.0, complete=False)
]


def series(*, synthetic: bool = False) -> BarSeries:
    bars = tuple(Bar(START_MS + i * 60_000, 5000.0, 5001.0, 4999.0, 5000.5, 10.0) for i in range(3))
    source = DataSource(
        name="mes_1m.csv", format="generic", sha256="ab" * 32, synthetic=synthetic, bar_tz="UTC"
    )
    return BarSeries(symbol="MES1!", tf_seconds=60, bars=bars, source=source)


def engine_config(*, free: bool = False) -> EngineConfig:
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
    )


def make_backtest(
    *, synthetic: bool = False, free: bool = False, label: str = "orb"
) -> AnyArtifact:
    return backtest_artifact(
        label=label,
        created=CREATED,
        strategy_id="ny_orb_fvg",
        params={"orMinutes": 15, "useBias": True, "bias": "Close", "rr": 2.0, "none": None},
        config=engine_config(free=free),
        entries=ENTRIES,
        oos_start_ms=START_MS + 3 * DAY_MS,
        dropped_entries=1,
        data=series(synthetic=synthetic),
        synthetic=False,
    )


def make_cost_matrix() -> CostMatrixArtifact:
    stats = trade_stats(ENTRIES, initial_capital=50_000.0)
    return build_artifact(
        CostMatrixArtifact,
        label="all",
        created=CREATED,
        synthetic=False,
        data=None,
        sample="oos",
        factors=(1.0, 1.5, 2.0),
        rows=(
            CostRow(
                strategy_id="ny_orb_fvg",
                cells=tuple(cost_cell(f, stats) for f in (1.0, 1.5, 2.0)),
            ),
        ),
    )


def make_walk_forward() -> AnyArtifact:
    result = walk_forward(
        ENTRIES,
        WalkForwardConfig(is_days=2, oos_days=1, step_days=1),
        first_bar_ms=START_MS,
        last_bar_ms=START_MS + 8 * DAY_MS,
        initial_capital=50_000.0,
    )
    return walk_forward_artifact(
        result, label="orb", created=CREATED, strategy_id="ny_orb_fvg", data=None, synthetic=False
    )


def outcomes() -> PathOutcomes:
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
        max_drawdown_pct=np.arange(10, dtype=np.float64) * 0.75,
    )


def make_monte_carlo(*, synthetic: bool = False, bins: int = 30) -> AnyArtifact:
    paths = outcomes()
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
        venue="FTMO 2-Step 100k",
        synthetic=synthetic,
        bins=bins,
    )


def gates(status: str = "PASS", *, ids: tuple[str, ...] = artifacts.GATE_IDS) -> tuple[Gate, ...]:
    return tuple(
        Gate(id=gate_id, title=f"Kapı {gate_id}", status=status, evidence="kanıt", details={})  # type: ignore[arg-type]
        for gate_id in ids
    )


def make_go_no_go(
    overall: str = "NO_GO", *, gate_list: tuple[Gate, ...] | None = None, synthetic: bool = False
) -> GoNoGoArtifact:
    return build_artifact(
        GoNoGoArtifact,
        label="haftalik",
        created=CREATED,
        synthetic=synthetic,
        data=None,
        gates=gates("FAIL") if gate_list is None else gate_list,
        overall=overall,
        reasons=("G1 kanıt bekliyor",),
    )


def make_data_validation() -> AnyArtifact:
    return build_artifact(
        DataValidationArtifact,
        label="mes",
        created=CREATED,
        synthetic=False,
        data=series(),
        clean=False,
        report={"gaps": [{"from": "2026-01-05T15:00:00Z", "minutes": 3}], "duplicates": 0},
    )


def make_parity() -> AnyArtifact:
    return build_artifact(
        ParityArtifact,
        label="orb",
        created=CREATED,
        synthetic=False,
        data=None,
        strategy_id="ny_orb_fvg",
        matched=98,
        python_only=1,
        tradingview_only=1,
        match_rate=0.98,
        threshold=0.98,
        passed=True,
        mismatches=({"side": "python", "entry_utc": "2026-01-05T14:31:00Z"},),
        sources=("list_of_trades.csv", "mes_1m.csv"),
    )


ALL_BUILDERS = [
    make_backtest,
    make_cost_matrix,
    make_walk_forward,
    make_monte_carlo,
    make_go_no_go,
    make_data_validation,
    make_parity,
]


# --- round trip ----------------------------------------------------------------------------


@pytest.mark.parametrize("builder", ALL_BUILDERS)
def test_every_kind_round_trips_through_a_file(builder: object, tmp_path: Path) -> None:
    artifact = builder()  # type: ignore[operator]

    path = write_artifact(artifact, tmp_path / "reports")

    assert load_artifact(path) == artifact
    assert path.name == artifact_file_name(artifact)
    assert path.name.endswith("_20261118T143005Z.json")
    assert path.name.startswith(f"{artifact.kind}_{artifact.label}_")


def test_backtest_artifact_carries_stats_tables_and_provenance() -> None:
    artifact = make_backtest()

    assert artifact.kind == "backtest"
    assert artifact.schema_version == 1
    assert artifact.created_utc == "2026-11-18T14:30:05Z"
    assert artifact.stats_all.n == 6
    assert artifact.stats_is.n == 3
    assert artifact.stats_oos.n == 3
    assert artifact.incomplete_entries == 1
    assert artifact.dropped_entries == 1
    assert artifact.oos_start_utc == "2026-01-08T14:30:00Z"
    assert [row.key for row in artifact.tables.weekday] == [
        "Mon",
        "Tue",
        "Wed",
        "Thu",
        "Fri",
        "Sat",
    ]
    assert len(artifact.equity_curve) == 6
    assert artifact.data is not None
    assert artifact.data.file_name == "mes_1m.csv"
    assert artifact.data.first_utc == "2026-01-05T14:30:00Z"
    assert artifact.costs.cost_free is False
    assert artifact.notes == ()


def test_synthetic_series_makes_the_artifact_synthetic_with_the_warning() -> None:
    artifact = make_backtest(synthetic=True)

    assert artifact.synthetic is True
    assert artifact.notes[0] == SYNTHETIC_NOTE


def test_cost_free_run_carries_the_warning() -> None:
    artifact = make_backtest(free=True)

    assert artifact.costs.cost_free is True
    assert COST_FREE_NOTE in artifact.notes


def test_monte_carlo_artifact_has_a_histogram_of_path_drawdowns() -> None:
    artifact = make_monte_carlo(bins=5)

    histogram = artifact.drawdown_histogram
    assert histogram.counts == (2, 2, 2, 2, 2)
    assert histogram.edges[0] == 0.0
    assert histogram.edges[-1] == pytest.approx(6.75)
    assert artifact.p_pass_phase == (pytest.approx(0.5), pytest.approx(0.4))
    assert artifact.p_breach == pytest.approx(0.3)
    assert artifact.p_undecided == pytest.approx(0.3)
    shares = {(s.phase, s.reason): s.probability for s in artifact.breach_shares}
    assert shares[(1, "daily_loss")] == pytest.approx(0.2)
    assert shares[(2, "max_loss")] == pytest.approx(0.1)
    assert artifact.inputs.trades_file == "trades.csv"
    assert artifact.inputs.rules.targets_pct == (10.0, 5.0)
    assert artifact.time_to_pass[0] is not None
    assert artifact.time_to_pass[0].median == pytest.approx(5.0)


def test_histogram_of_identical_drawdowns_is_valid() -> None:
    paths = outcomes()
    flat = PathOutcomes(
        n_phases=paths.n_phases,
        status=paths.status,
        phase_days=paths.phase_days,
        breach_phase=paths.breach_phase,
        breach_reason=paths.breach_reason,
        max_drawdown_pct=np.zeros(10),
    )

    histogram = artifacts.drawdown_histogram(flat.max_drawdown_pct, 3)

    assert sum(histogram.counts) == 10
    assert len(histogram.edges) == 4


def test_builder_errors_are_turkish_artifact_errors() -> None:
    with pytest.raises(ArtifactError, match="rapor oluşturulamadı: label"):
        build_artifact(
            ParityArtifact,
            label="Bad Label",
            created=CREATED,
            synthetic=False,
            data=None,
            strategy_id="x",
            matched=0,
            python_only=0,
            tradingview_only=0,
            match_rate=None,
            threshold=0.98,
            passed=False,
            mismatches=(),
            sources=(),
        )


def test_naive_creation_time_is_refused() -> None:
    with pytest.raises(ArtifactError, match="saat dilimi"):
        utc_stamp(dt.datetime(2026, 11, 18, 14, 30))


def test_data_info_reads_the_series_source() -> None:
    info = data_info(series(synthetic=True))

    assert (info.symbol, info.tf_seconds, info.bars, info.synthetic) == ("MES1!", 60, 3, True)
    assert info.last_utc == "2026-01-05T14:32:00Z"


# --- schema rules --------------------------------------------------------------------------


def test_go_is_accepted_only_with_all_seven_gates_passing() -> None:
    artifact = make_go_no_go("GO", gate_list=gates("PASS"))

    assert artifact.overall == "GO"


@pytest.mark.parametrize("status", ["FAIL", "NOT_EVALUABLE"])
def test_go_with_any_non_passing_gate_is_refused(status: str) -> None:
    gate_list = (
        *gates("PASS", ids=("G1", "G2", "G3", "G4", "G5", "G6")),
        *gates(status, ids=("G7",)),
    )

    with pytest.raises(ArtifactError, match="PASS"):
        make_go_no_go("GO", gate_list=gate_list)


def test_go_with_a_missing_gate_is_refused() -> None:
    with pytest.raises(ArtifactError, match="PASS"):
        make_go_no_go("GO", gate_list=gates("PASS", ids=("G1", "G2", "G3")))


def test_go_with_no_gates_is_refused() -> None:
    with pytest.raises(ArtifactError):
        make_go_no_go("GO", gate_list=())


def test_go_on_synthetic_data_is_refused_even_with_all_gates_passing() -> None:
    with pytest.raises(ArtifactError, match="sentetik"):
        make_go_no_go("GO", gate_list=gates("PASS"), synthetic=True)


def test_no_go_is_always_allowed_and_duplicate_gates_are_not() -> None:
    assert make_go_no_go("NO_GO", gate_list=gates("PASS"), synthetic=True).overall == "NO_GO"
    with pytest.raises(ArtifactError, match="birden fazla"):
        make_go_no_go("NO_GO", gate_list=gates("FAIL", ids=("G1", "G1")))


def test_hand_edited_go_file_is_refused_on_load(tmp_path: Path) -> None:
    path = write_artifact(make_go_no_go("NO_GO", gate_list=gates("FAIL")), tmp_path)
    document = json.loads(path.read_text(encoding="utf-8"))
    document["overall"] = "GO"
    path.write_text(json.dumps(document), encoding="utf-8")

    with pytest.raises(ArtifactError, match="GO için"):
        load_artifact(path)


def test_synthetic_flag_without_the_warning_is_refused(tmp_path: Path) -> None:
    path = write_artifact(make_monte_carlo(synthetic=True), tmp_path)
    document = json.loads(path.read_text(encoding="utf-8"))
    document["notes"] = []
    path.write_text(json.dumps(document), encoding="utf-8")

    with pytest.raises(ArtifactError, match="SENTETİK VERİ"):
        load_artifact(path)


def test_synthetic_data_cannot_be_declared_real(tmp_path: Path) -> None:
    path = write_artifact(make_backtest(synthetic=True), tmp_path)
    document = json.loads(path.read_text(encoding="utf-8"))
    document["synthetic"] = False
    path.write_text(json.dumps(document), encoding="utf-8")

    with pytest.raises(ArtifactError, match="synthetic"):
        load_artifact(path)


def test_parity_cannot_pass_below_its_threshold() -> None:
    with pytest.raises(ArtifactError, match="eşiğin altında"):
        build_artifact(
            ParityArtifact,
            label="orb",
            created=CREATED,
            synthetic=False,
            data=None,
            strategy_id="ny_orb_fvg",
            matched=9,
            python_only=1,
            tradingview_only=0,
            match_rate=0.9,
            threshold=0.98,
            passed=True,
            mismatches=(),
            sources=(),
        )


def test_cost_matrix_factors_must_ascend_and_match_the_cells() -> None:
    stats = trade_stats(ENTRIES, initial_capital=50_000.0)
    with pytest.raises(ArtifactError, match="artan"):
        build_artifact(
            CostMatrixArtifact,
            label="all",
            created=CREATED,
            synthetic=False,
            data=None,
            sample="all",
            factors=(1.0, 1.0),
            rows=(),
        )
    with pytest.raises(ArtifactError, match="hücre"):
        build_artifact(
            CostMatrixArtifact,
            label="all",
            created=CREATED,
            synthetic=False,
            data=None,
            sample="all",
            factors=(1.0, 2.0),
            rows=(CostRow(strategy_id="x", cells=(cost_cell(1.0, stats),)),),
        )


# --- load_artifact refusals ----------------------------------------------------------------


def written(tmp_path: Path) -> Path:
    return write_artifact(make_parity(), tmp_path)


def refused(path: Path, match: str) -> str:
    with pytest.raises(ArtifactError, match=match) as info:
        load_artifact(path)
    message = str(info.value)
    assert "\n" not in message
    assert message.startswith(path.name)
    return message


def test_too_large_file_is_refused(tmp_path: Path) -> None:
    path = tmp_path / "parity_orb_20261118T143005Z.json"
    with path.open("wb") as handle:
        handle.truncate(MAX_ARTIFACT_BYTES + 1)

    refused(path, "çok büyük")


def test_not_json_is_refused(tmp_path: Path) -> None:
    path = tmp_path / "x.json"
    path.write_text("kind: backtest", encoding="utf-8")

    refused(path, "geçerli JSON değil")


def test_truncated_file_is_refused(tmp_path: Path) -> None:
    path = written(tmp_path)
    text = path.read_text(encoding="utf-8")
    path.write_text(text[: len(text) // 2], encoding="utf-8")

    refused(path, "kesik")


def test_non_utf8_file_is_refused(tmp_path: Path) -> None:
    path = tmp_path / "x.json"
    path.write_bytes(b'{"kind": "parity", "label": "\xff"}')

    refused(path, "UTF-8")


def test_nan_is_refused(tmp_path: Path) -> None:
    path = written(tmp_path)
    document = path.read_text(encoding="utf-8").replace('"match_rate": 0.98', '"match_rate": NaN')
    path.write_text(document, encoding="utf-8")

    refused(path, "NaN")


def test_duplicate_key_is_refused(tmp_path: Path) -> None:
    path = written(tmp_path)
    document = path.read_text(encoding="utf-8").replace(
        '"matched": 98,', '"matched": 98,\n  "matched": 99,'
    )
    path.write_text(document, encoding="utf-8")

    refused(path, "yinelenen alan")


def edit(path: Path, change: object) -> None:
    document = json.loads(path.read_text(encoding="utf-8"))
    change(document)  # type: ignore[operator]
    path.write_text(json.dumps(document, ensure_ascii=False), encoding="utf-8")


def test_unknown_kind_is_refused(tmp_path: Path) -> None:
    path = written(tmp_path)
    edit(path, lambda d: d.update(kind="optimizer"))

    refused(path, "bilinmeyen rapor türü")


def test_missing_kind_is_refused(tmp_path: Path) -> None:
    path = written(tmp_path)
    edit(path, lambda d: d.pop("kind"))

    refused(path, "rapor türü")


def test_extra_field_is_refused(tmp_path: Path) -> None:
    path = written(tmp_path)
    edit(path, lambda d: d.update(override_gate=True))

    refused(path, "override_gate: bilinmeyen alan")


def test_wrong_type_is_refused_without_coercion(tmp_path: Path) -> None:
    path = written(tmp_path)
    edit(path, lambda d: d.update(matched="98"))

    refused(path, "matched: yanlış tür")


def test_missing_field_is_refused(tmp_path: Path) -> None:
    path = written(tmp_path)
    edit(path, lambda d: d.pop("threshold"))

    refused(path, "threshold: eksik alan")


def test_wrong_schema_version_is_refused(tmp_path: Path) -> None:
    path = written(tmp_path)
    edit(path, lambda d: d.update(schema_version=2))

    refused(path, "schema_version")


def test_json_array_root_is_refused(tmp_path: Path) -> None:
    path = tmp_path / "x.json"
    path.write_text("[1, 2]", encoding="utf-8")

    refused(path, "şema")


def test_missing_file_symlink_and_directory_are_refused(tmp_path: Path) -> None:
    real = written(tmp_path)
    link = tmp_path / "link.json"
    link.symlink_to(real)
    directory = tmp_path / "dir.json"
    directory.mkdir()

    refused(tmp_path / "absent.json", "bulunamadı")
    refused(link, "açılamadı")
    refused(directory, "normal bir dosya değil")
    fifo = tmp_path / "fifo.json"
    os.mkfifo(fifo)
    refused(fifo, "normal bir dosya değil")


# --- write_artifact ------------------------------------------------------------------------


def test_write_refuses_to_overwrite_and_keeps_the_first_file(tmp_path: Path) -> None:
    first = write_artifact(make_parity(), tmp_path)
    before = first.read_bytes()

    with pytest.raises(ArtifactError, match="zaten var"):
        write_artifact(make_parity(), tmp_path)

    assert first.read_bytes() == before
    assert sorted(p.name for p in tmp_path.iterdir()) == [first.name]


def test_failed_write_leaves_neither_target_nor_temp_file(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    def broken_fsync(fd: int) -> None:
        raise OSError(28, "No space left on device")

    monkeypatch.setattr(artifacts.os, "fsync", broken_fsync)

    with pytest.raises(OSError, match="No space"):
        write_artifact(make_parity(), tmp_path)

    assert list(tmp_path.iterdir()) == []


def test_write_creates_the_directory_and_valid_utf8_json(tmp_path: Path) -> None:
    path = write_artifact(make_backtest(synthetic=True), tmp_path / "a" / "reports")

    text = path.read_text(encoding="utf-8")
    assert "SENTETİK VERİ" in text  # not escaped as \\u0130
    assert json.loads(text)["kind"] == "backtest"


def test_write_falls_back_when_hard_links_are_unsupported(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    def no_links(source: object, target: object) -> None:
        raise OSError(1, "Operation not permitted")

    monkeypatch.setattr(artifacts.os, "link", no_links)

    path = write_artifact(make_parity(), tmp_path)
    assert load_artifact(path) == make_parity()
    with pytest.raises(ArtifactError, match="zaten var"):
        write_artifact(make_parity(), tmp_path)
    assert [p.name for p in tmp_path.iterdir()] == [path.name]


# --- latest_artifacts ----------------------------------------------------------------------


def at(artifact: AnyArtifact, moment: dt.datetime) -> AnyArtifact:
    return artifact.model_copy(update={"created_utc": utc_stamp(moment)})


def test_latest_picks_the_newest_per_kind_and_label(tmp_path: Path) -> None:
    old = at(make_parity(), CREATED - dt.timedelta(days=1))
    new = make_parity()
    other_label = build_artifact(
        DataValidationArtifact,
        label="es",
        created=CREATED,
        synthetic=False,
        data=None,
        clean=True,
        report={},
    )
    for artifact in (old, new, other_label, make_monte_carlo()):
        write_artifact(artifact, tmp_path)

    latest = latest_artifacts(tmp_path)

    assert latest.problems == ()
    keys = [(item.artifact.kind, item.artifact.label) for item in latest.artifacts]
    assert keys == [("data_validation", "es"), ("monte_carlo", "ftmo"), ("parity", "orb")]
    parity = latest.artifacts[2]
    assert parity.artifact == new
    assert parity.file_name == artifact_file_name(new)


def test_latest_reports_broken_files_and_keeps_the_readable_ones(tmp_path: Path) -> None:
    good = write_artifact(at(make_parity(), CREATED - dt.timedelta(hours=1)), tmp_path)
    broken = tmp_path / "parity_orb_20261118T143005Z.json"
    broken.write_text('{"kind": "parity", ', encoding="utf-8")

    latest = latest_artifacts(tmp_path)

    assert [item.file_name for item in latest.artifacts] == [good.name]
    assert [problem.file_name for problem in latest.problems] == [broken.name]
    assert "JSON" in latest.problems[0].message


def test_latest_ignores_unrelated_files_and_temp_files(tmp_path: Path) -> None:
    (tmp_path / "notes.txt").write_text("x", encoding="utf-8")
    (tmp_path / ".tmp-abc.json").write_text("{", encoding="utf-8")
    (tmp_path / "go_no_go_20261118.md").write_text("# rapor", encoding="utf-8")

    latest = latest_artifacts(tmp_path)

    assert latest.artifacts == ()
    assert latest.problems == ()


def test_latest_of_a_missing_directory_is_empty(tmp_path: Path) -> None:
    latest = latest_artifacts(tmp_path / "absent")

    assert (latest.artifacts, latest.problems) == ((), ())


def test_latest_does_not_follow_symlinks(tmp_path: Path) -> None:
    outside = tmp_path / "outside"
    reports = tmp_path / "reports"
    target = write_artifact(make_parity(), outside)
    reports.mkdir()
    (reports / target.name).symlink_to(target)

    latest = latest_artifacts(reports)

    assert latest.artifacts == ()
    assert [p.file_name for p in latest.problems] == [target.name]
    assert "sembolik" in latest.problems[0].message


def test_latest_refuses_a_file_whose_name_does_not_match_its_content(tmp_path: Path) -> None:
    path = write_artifact(make_parity(), tmp_path)
    renamed = path.with_name("parity_other_20261118T143005Z.json")
    os.rename(path, renamed)

    latest = latest_artifacts(tmp_path)

    assert latest.artifacts == ()
    assert "uyuşmuyor" in latest.problems[0].message


def test_latest_examines_a_bounded_number_of_newest_files(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    for hours in range(4):
        write_artifact(at(make_parity(), CREATED - dt.timedelta(hours=hours)), tmp_path)
    oldest_broken = tmp_path / "parity_orb_20200101T000000Z.json"
    oldest_broken.write_text("{", encoding="utf-8")
    monkeypatch.setattr(artifacts, "MAX_EXAMINED_FILES", 2)

    latest = latest_artifacts(tmp_path)

    assert latest.problems == ()  # the broken file is too old to be examined
    assert latest.artifacts[0].artifact.created_utc == "2026-11-18T14:30:05Z"
