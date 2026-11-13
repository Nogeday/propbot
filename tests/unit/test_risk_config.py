"""RiskConfig: range rules and hard caps, with files that must load and files that must fail."""

from typing import Any

import pytest
from pydantic import ValidationError

from propbot.risk import (
    MAX_POSITIONS_PER_STRATEGY,
    MAX_RISK_PER_TRADE_PCT,
    MAX_SELF_REQUESTS_PER_DAY,
    ConfigError,
    RiskConfig,
    load_risk_config,
)
from propbot.risk.loader import read_yaml_mapping
from support.paths import CONFIG_DIR, CONFIG_FIXTURES

VALID = CONFIG_FIXTURES / "valid" / "risk.yaml"
INVALID_DIR = CONFIG_FIXTURES / "invalid" / "risk"

# fixture stem -> (issue location, fragment of the Turkish message)
MUST_FAIL: dict[str, tuple[str, str]] = {
    "risk_per_trade_above_cap": ("risk_per_trade_pct", "en fazla 0.5 olabilir; okunan: 0.6"),
    "unknown_key": ("monthly_cap_pct", "bilinmeyen anahtar"),
    "missing_key": ("weekly_cap_pct", "zorunlu alan eksik"),
    "duplicate_key": ("", "yinelenen anahtar 'risk_per_trade_pct' (satır 4)"),
    "bool_as_number": ("risk_per_trade_pct", "sayı olmalı"),
    "quoted_number": ("risk_per_trade_pct", "sayı olmalı"),
    "two_positions_per_strategy": ("max_positions_per_strategy", "en fazla 1 olabilir"),
    "request_budget_above_cap": ("self_request_budget_per_day", "en fazla 300 olabilir"),
    "float_where_int_expected": ("max_consecutive_losses", "tam sayı olmalı"),
    "null_value": ("daily_cap_pct", "boş (null) bırakılamaz"),
    "not_a_mapping": ("", "eşlemesi olmalı"),
    "empty": ("", "eşlemesi olmalı"),
    "syntax_error": ("", "YAML okunamadı (satır"),
}


def _valid_data() -> dict[Any, Any]:
    return read_yaml_mapping(VALID)


def test_real_risk_config_loads_within_the_caps() -> None:
    risk = load_risk_config(CONFIG_DIR / "risk.yaml")

    assert 0 < risk.risk_per_trade_pct <= MAX_RISK_PER_TRADE_PCT
    assert risk.max_positions_per_strategy == MAX_POSITIONS_PER_STRATEGY
    assert risk.self_request_budget_per_day <= MAX_SELF_REQUESTS_PER_DAY


def test_valid_fixture_loads_with_exact_values() -> None:
    risk = load_risk_config(VALID)

    assert risk.model_dump() == {
        "risk_per_trade_pct": 0.25,
        "daily_cap_pct": 1.0,
        "weekly_cap_pct": 3.0,
        "flatten_when_daily_buffer_below_pct": 40.0,
        "max_consecutive_losses": 5,
        "max_positions_total": 2,
        "max_positions_per_strategy": 1,
        "self_request_budget_per_day": 300,
        "trail_min_step_ticks": 4,
        "signal_validity_seconds": 60,
        "signal_max_drift_frac_of_stop": 0.25,
    }


def test_the_code_caps_match_the_spec() -> None:
    # SPEC §8.1 and CLAUDE.md invariant 8. A change here needs an ADR, not a test edit.
    assert MAX_RISK_PER_TRADE_PCT == 0.5
    assert MAX_POSITIONS_PER_STRATEGY == 1
    assert MAX_SELF_REQUESTS_PER_DAY == 300


def test_every_must_fail_fixture_is_listed() -> None:
    assert {path.stem for path in INVALID_DIR.glob("*.yaml")} == set(MUST_FAIL)


@pytest.mark.parametrize(
    ("stem", "location", "fragment"),
    [(stem, *expected) for stem, expected in MUST_FAIL.items()],
    ids=list(MUST_FAIL),
)
def test_invalid_risk_file_is_rejected(stem: str, location: str, fragment: str) -> None:
    with pytest.raises(ConfigError) as excinfo:
        load_risk_config(INVALID_DIR / f"{stem}.yaml")

    issues = excinfo.value.issues
    assert any(i.location == location and fragment in i.message for i in issues), issues


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("risk_per_trade_pct", 0.5),  # exactly the cap
        ("risk_per_trade_pct", 0.01),
        ("daily_cap_pct", 2),  # an integer is a valid number for a float field
        ("flatten_when_daily_buffer_below_pct", 0),
        ("flatten_when_daily_buffer_below_pct", 99.9),
        ("signal_max_drift_frac_of_stop", 1.0),
        ("self_request_budget_per_day", 1),
        ("self_request_budget_per_day", 300),
        ("max_positions_total", 1),
    ],
)
def test_boundary_value_is_accepted(field: str, value: object) -> None:
    data = _valid_data()
    data[field] = value

    assert getattr(RiskConfig.model_validate(data), field) == value


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("risk_per_trade_pct", 0.5000001),
        ("risk_per_trade_pct", 0),
        ("risk_per_trade_pct", -0.1),
        ("risk_per_trade_pct", float("nan")),
        ("risk_per_trade_pct", float("inf")),
        ("daily_cap_pct", 0),
        ("daily_cap_pct", 100),
        ("weekly_cap_pct", 0),
        ("weekly_cap_pct", 100.0),
        ("flatten_when_daily_buffer_below_pct", -1),
        ("flatten_when_daily_buffer_below_pct", 100),
        ("max_consecutive_losses", 0),
        ("max_consecutive_losses", True),
        ("max_positions_total", 0),
        ("max_positions_total", "2"),
        ("max_positions_per_strategy", 0),
        ("max_positions_per_strategy", 2),
        ("self_request_budget_per_day", 0),
        ("self_request_budget_per_day", 301),
        ("trail_min_step_ticks", 0),
        ("signal_validity_seconds", 0),
        ("signal_validity_seconds", 60.0),
        ("signal_max_drift_frac_of_stop", 0),
        ("signal_max_drift_frac_of_stop", 1.01),
    ],
)
def test_out_of_range_or_mistyped_value_is_rejected(field: str, value: object) -> None:
    data = _valid_data()
    data[field] = value

    with pytest.raises(ValidationError) as excinfo:
        RiskConfig.model_validate(data)

    assert [error["loc"] for error in excinfo.value.errors()] == [(field,)]


def test_risk_config_is_immutable() -> None:
    risk = RiskConfig.model_validate(_valid_data())

    with pytest.raises(ValidationError):
        risk.risk_per_trade_pct = 0.5
