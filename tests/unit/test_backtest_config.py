"""`config/backtest.yaml`: schema, VERIFY profiles and the Go/No-Go floors (SPEC §9)."""

import copy
import datetime as dt
from pathlib import Path
from typing import Any

import pytest
import yaml

from propbot.backtest.config import (
    MAX_ARTIFACT_AGE_DAYS,
    MAX_P_BREACH,
    MIN_BAR_HOURS_PER_YEAR,
    MIN_DATA_YEARS,
    MIN_MC_PATHS,
    MIN_MC_TRADES,
    MIN_OOS_T_STAT,
    MIN_OOS_TO_IS_RATIO,
    MIN_OOS_TRADES,
    MIN_P_PASS_ALL_PHASES,
    MIN_PARITY_RATE,
    REQUIRED_TF_SECONDS,
    BacktestConfig,
    BacktestConfigError,
    GoNoGoCriteria,
    load_backtest_config,
    unresolved_instruments,
)
from propbot.core.instrument import InstrumentKind
from propbot.risk.errors import ConfigError
from support.paths import REPO_ROOT

REAL_FILE = REPO_ROOT / "config" / "backtest.yaml"


def base() -> dict[str, Any]:
    data = yaml.safe_load(REAL_FILE.read_text(encoding="utf-8"))
    assert isinstance(data, dict)
    return data


def load(tmp_path: Path, data: dict[str, Any]) -> BacktestConfig:
    path = tmp_path / "backtest.yaml"
    path.write_text(yaml.safe_dump(data), encoding="utf-8")
    return load_backtest_config(path)


def issues(tmp_path: Path, data: dict[str, Any]) -> str:
    with pytest.raises(ConfigError) as caught:
        load(tmp_path, data)
    return str(caught.value)


def test_the_real_file_loads_and_its_default_instrument_is_the_pine_header_profile() -> None:
    config = load_backtest_config(REAL_FILE)

    instrument = config.instrument()
    costs = config.cost_model()

    # pine/*.pine header: MES, 1 tick = 0.25 point = 1.25 USD; 0.50 USD per side; 1 tick slippage.
    assert instrument.symbol == "MES"
    assert instrument.kind is InstrumentKind.FUTURES
    assert instrument.tick_size == 0.25
    assert instrument.point_value == 5.0
    assert instrument.tick_size * instrument.point_value == 1.25
    assert (instrument.qty_step, instrument.min_qty) == (1.0, 1.0)
    assert costs.commission_per_unit_side == 0.50
    assert costs.slippage_ticks == 1.0
    assert costs.spread_points == 0.0
    assert costs.spread_tz == "America/New_York"
    assert not costs.is_free


def test_the_real_files_thresholds_are_exactly_the_spec_values() -> None:
    criteria = load_backtest_config(REAL_FILE).go_no_go

    assert criteria.min_data_years == MIN_DATA_YEARS == 2.0
    assert criteria.required_tf_seconds == REQUIRED_TF_SECONDS == 60
    assert criteria.min_oos_t_stat == MIN_OOS_T_STAT == 2.0
    assert criteria.min_oos_to_is_ratio == MIN_OOS_TO_IS_RATIO == 0.5
    assert criteria.min_p_pass_all_phases == MIN_P_PASS_ALL_PHASES == 0.60
    assert criteria.max_p_breach == MAX_P_BREACH == 0.20
    assert criteria.min_parity_rate == MIN_PARITY_RATE == 0.98


def test_the_real_files_engineering_thresholds_are_exactly_the_code_floors() -> None:
    criteria = load_backtest_config(REAL_FILE).go_no_go

    assert criteria.min_oos_trades == MIN_OOS_TRADES == 30
    assert criteria.min_bar_hours_per_year == MIN_BAR_HOURS_PER_YEAR == 1500.0
    assert criteria.min_mc_paths == MIN_MC_PATHS == 5000
    assert criteria.min_mc_trades == MIN_MC_TRADES == 100
    assert criteria.max_artifact_age_days == MAX_ARTIFACT_AGE_DAYS == 30


def test_the_engineering_thresholds_are_marked_as_approved_by_the_user() -> None:
    # Until 2026-11-19 these lines said "awaiting user confirmation"; the user then approved
    # the six thresholds unchanged (Faz 2 delivery, decision 3). The marker stays pinned, and
    # now on all six keys, so nobody reads them as SPEC §9 values and a later change to one
    # is seen to need a new decision.
    lines = REAL_FILE.read_text(encoding="utf-8").splitlines()
    for key in (
        "min_oos_trades",
        "min_bar_hours_per_year",
        "min_mc_paths",
        "min_mc_trades",
        "max_artifact_age_days",
        "min_parity_trades",
    ):
        line = next(line for line in lines if line.strip().startswith(f"{key}:"))
        assert "engineering choice; approved by the user on 2026-11-19" in line, key
        assert "awaiting" not in line, key


def test_criteria_without_the_engineering_keys_get_exactly_the_floors() -> None:
    criteria = GoNoGoCriteria(
        min_data_years=2.0,
        required_tf_seconds=60,
        min_oos_t_stat=2.0,
        min_oos_to_is_ratio=0.5,
        min_p_pass_all_phases=0.60,
        max_p_breach=0.20,
        min_parity_rate=0.98,
    )

    assert (
        criteria.min_oos_trades,
        criteria.min_bar_hours_per_year,
        criteria.min_mc_paths,
        criteria.min_mc_trades,
        criteria.max_artifact_age_days,
    ) == (30, 1500.0, 5000, 100, 30)


def test_the_oos_boundary_is_utc_midnight_like_the_pine_input() -> None:
    config = load_backtest_config(REAL_FILE)

    assert config.samples.oos_start_utc == dt.datetime(2025, 1, 1, tzinfo=dt.UTC)
    assert config.cost_factors == (1.0, 1.5, 2.0)
    assert config.session.min_gap_minutes == 30


def test_a_profile_with_verify_values_loads_but_cannot_be_used() -> None:
    config = load_backtest_config(REAL_FILE)

    notes = unresolved_instruments(config)
    assert [note.location for note in notes] == ["instruments.US500.cash"]
    assert "tick_size" in notes[0].message
    assert "costs.spread_points" in notes[0].message
    for build in (config.instrument, config.cost_model):
        with pytest.raises(BacktestConfigError) as caught:
            build("US500.cash")
        assert "VERIFY" in str(caught.value)
        assert "tick_size" in str(caught.value)


def test_an_unknown_instrument_names_the_known_ones() -> None:
    config = load_backtest_config(REAL_FILE)

    with pytest.raises(BacktestConfigError) as caught:
        config.instrument("NQ")

    assert "NQ" in str(caught.value)
    assert "MES" in str(caught.value)


@pytest.mark.parametrize(
    ("key", "looser"),
    [
        ("min_data_years", 1.9),
        ("required_tf_seconds", 300),
        ("min_oos_t_stat", 1.99),
        ("min_oos_to_is_ratio", 0.49),
        ("min_p_pass_all_phases", 0.59),
        ("max_p_breach", 0.21),
        ("min_parity_rate", 0.979),
        ("min_oos_trades", 29),
        ("min_bar_hours_per_year", 1499.9),
        ("min_mc_paths", 4999),
        ("min_mc_trades", 99),
        ("max_artifact_age_days", 31),
        ("max_artifact_age_days", -1),
        ("min_oos_trades", 30.0),
        ("min_mc_paths", True),
    ],
)
def test_a_threshold_looser_than_the_spec_is_a_load_error(
    tmp_path: Path, key: str, looser: float
) -> None:
    data = base()
    data["go_no_go"][key] = looser

    assert f"go_no_go.{key}" in issues(tmp_path, data)


def test_stricter_thresholds_are_accepted(tmp_path: Path) -> None:
    data = base()
    data["go_no_go"] = {
        "min_data_years": 3.0,
        "required_tf_seconds": 60,
        "min_oos_t_stat": 2.5,
        "min_oos_to_is_ratio": 0.7,
        "min_p_pass_all_phases": 0.75,
        "max_p_breach": 0.10,
        "min_parity_rate": 0.99,
        "min_oos_trades": 50,
        "min_bar_hours_per_year": 1600,
        "min_mc_paths": 20000,
        "min_mc_trades": 200,
        "max_artifact_age_days": 7,
    }

    criteria = load(tmp_path, data).go_no_go

    assert criteria.min_oos_t_stat == 2.5
    assert criteria.max_p_breach == 0.10
    assert criteria.min_oos_trades == 50
    assert criteria.min_bar_hours_per_year == 1600.0
    assert criteria.min_mc_paths == 20000
    assert criteria.min_mc_trades == 200
    assert criteria.max_artifact_age_days == 7


@pytest.mark.parametrize("value", [1.5, -0.1, "0.2", True])
def test_probabilities_outside_their_range_or_of_the_wrong_type_are_refused(
    tmp_path: Path, value: object
) -> None:
    data = base()
    data["go_no_go"]["min_p_pass_all_phases"] = value

    assert "go_no_go.min_p_pass_all_phases" in issues(tmp_path, data)


def test_unknown_keys_are_refused_at_every_level(tmp_path: Path) -> None:
    for path in ([], ["session"], ["instruments", "MES"], ["instruments", "MES", "costs"]):
        data = base()
        target = data
        for part in path:
            target = target[part]
        target["surprise"] = 1

        assert "surprise" in issues(tmp_path, data)


def test_a_missing_section_is_reported(tmp_path: Path) -> None:
    data = base()
    del data["walk_forward"]

    assert "walk_forward" in issues(tmp_path, data)


@pytest.mark.parametrize(
    "factors", [[], [1.5, 2.0], [1.0, 1.0], [1.0, 2.0, 1.5], [1.0, 0.0], [1.0, 11.0]]
)
def test_cost_factors_start_at_one_and_strictly_increase(
    tmp_path: Path, factors: list[float]
) -> None:
    data = base()
    data["cost_factors"] = factors

    assert "cost_factors" in issues(tmp_path, data)


def test_a_walk_forward_step_shorter_than_the_oos_window_is_refused(tmp_path: Path) -> None:
    data = base()
    data["walk_forward"] = {"is_days": 180, "oos_days": 60, "step_days": 59}

    assert "step_days" in issues(tmp_path, data)


def test_the_default_instrument_must_be_listed(tmp_path: Path) -> None:
    data = base()
    data["default_instrument"] = "ES"

    assert "default_instrument" in issues(tmp_path, data)


@pytest.mark.parametrize("zone", ["Mars/Olympus", "", "New York"])
def test_an_unknown_time_zone_is_refused(tmp_path: Path, zone: str) -> None:
    data = base()
    data["session"]["tz"] = zone

    assert "session.tz" in issues(tmp_path, data)


@pytest.mark.parametrize("day", ["2025-1-1", "2025-02-30", "01.01.2025", 20250101])
def test_the_oos_date_must_be_a_real_iso_date(tmp_path: Path, day: object) -> None:
    data = base()
    data["samples"]["oos_start"] = day

    assert "samples.oos_start" in issues(tmp_path, data)


@pytest.mark.parametrize(
    ("field", "value"),
    [("tick_size", 0), ("tick_size", -0.25), ("point_value", 0), ("qty_step", "1")],
)
def test_contract_values_must_be_positive_numbers(
    tmp_path: Path, field: str, value: object
) -> None:
    data = base()
    data["instruments"]["MES"][field] = value

    assert f"instruments.MES.{field}" in issues(tmp_path, data)


def test_a_negative_cost_is_refused(tmp_path: Path) -> None:
    data = base()
    data["instruments"]["MES"]["costs"]["slippage_ticks"] = -1

    assert "instruments.MES.costs.slippage_ticks" in issues(tmp_path, data)


def test_spread_windows_become_minutes_of_the_session_day_and_may_wrap(tmp_path: Path) -> None:
    data = base()
    cfd = copy.deepcopy(data["instruments"]["MES"])
    cfd["kind"] = "cfd"
    cfd["costs"]["spread_points"] = 0.4
    cfd["costs"]["spread_windows"] = [
        {"start": "09:25", "end": "09:40", "spread_points": 1.2},
        {"start": "16:55", "end": "18:05", "spread_points": 3.0},
        {"start": "23:50", "end": "00:10", "spread_points": 0.9},
    ]
    data["instruments"]["CFD"] = cfd

    costs = load(tmp_path, data).cost_model("CFD")

    assert costs.spread_at(9 * 60 + 24) == 0.4
    assert costs.spread_at(9 * 60 + 25) == 1.2
    assert costs.spread_at(9 * 60 + 39) == 1.2
    assert costs.spread_at(9 * 60 + 40) == 0.4
    assert costs.spread_at(17 * 60) == 3.0
    assert costs.spread_at(23 * 60 + 55) == 0.9
    assert costs.spread_at(5) == 0.9
    assert costs.spread_at(10) == 0.4


@pytest.mark.parametrize(
    "window",
    [
        {"start": "9:25", "end": "09:40", "spread_points": 1.0},
        {"start": "09:25", "end": "24:00", "spread_points": 1.0},
        {"start": "09:25", "end": "09:25", "spread_points": 1.0},
        {"start": "09:25", "end": "09:40", "spread_points": -1.0},
        {"start": "09:25", "end": "09:40"},
    ],
)
def test_a_malformed_spread_window_is_refused(tmp_path: Path, window: dict[str, object]) -> None:
    data = base()
    data["instruments"]["MES"]["costs"]["spread_windows"] = [window]

    assert "spread_windows" in issues(tmp_path, data)


def test_an_instrument_name_with_odd_characters_is_refused(tmp_path: Path) -> None:
    data = base()
    data["instruments"]["bad name"] = copy.deepcopy(data["instruments"]["MES"])

    assert "instruments" in issues(tmp_path, data)


def test_a_duplicate_key_is_refused(tmp_path: Path) -> None:
    path = tmp_path / "backtest.yaml"
    path.write_text(REAL_FILE.read_text(encoding="utf-8") + "\ncost_factors: [1.0]\n", "utf-8")

    with pytest.raises(ConfigError) as caught:
        load_backtest_config(path)

    assert "yinelenen anahtar" in str(caught.value)
