"""Loading a config directory: strict YAML, cross-file rules (SPEC §8.1), aggregated errors."""

from pathlib import Path

import pytest

from propbot.risk import (
    ConfigError,
    cross_check,
    daily_budget_amount,
    daily_budget_pct,
    daily_cap_ceiling_pct,
    load_config_dir,
    load_risk_config,
    load_venue_profile,
)
from propbot.risk.loader import read_yaml_mapping
from support.paths import CONFIG_DIR, CONFIG_FIXTURES

VALID_DIR = CONFIG_FIXTURES / "valid"
CROSS_DIR = CONFIG_FIXTURES / "invalid" / "cross"

# fixture directory -> (file the issue points at, issue location, fragment of the message)
MUST_FAIL: dict[str, tuple[str, str, str]] = {
    "daily_cap_above_half_limit": ("risk.yaml", "daily_cap_pct", "tavanı aşıyor"),
    "request_budget_above_firm_threshold": (
        "risk.yaml",
        "self_request_budget_per_day",
        "firm_request_threshold_per_day (200)",
    ),
    "no_venues": ("venues", "", "venue profili yok"),
    "two_broken_files": ("risk.yaml", "risk_per_trade_pct", "en fazla 0.5"),
}


def test_real_config_dir_loads_and_is_consistent() -> None:
    config = load_config_dir(CONFIG_DIR)

    assert "ftmo_2step_100k" in config.venues
    for venue in config.venues.values():
        assert config.risk.daily_cap_pct <= daily_cap_ceiling_pct(venue)
        assert daily_budget_pct(config.risk, venue) <= 0.5 * venue.daily_loss_limit_pct


def test_valid_fixture_dir_loads() -> None:
    config = load_config_dir(VALID_DIR)
    venue = config.venues["sample_2step"]

    assert list(config.venues) == ["sample_2step"]
    assert daily_cap_ceiling_pct(venue) == 2.5
    assert daily_budget_pct(config.risk, venue) == 1.0
    assert daily_budget_amount(config.risk, venue) == 1000.0


def test_every_cross_fixture_is_listed() -> None:
    assert {path.name for path in CROSS_DIR.iterdir() if path.is_dir()} == set(MUST_FAIL)


@pytest.mark.parametrize(
    ("name", "file_name", "location", "fragment"),
    [(name, *expected) for name, expected in MUST_FAIL.items()],
    ids=list(MUST_FAIL),
)
def test_inconsistent_config_dir_is_rejected(
    name: str, file_name: str, location: str, fragment: str
) -> None:
    with pytest.raises(ConfigError) as excinfo:
        load_config_dir(CROSS_DIR / name)

    issues = excinfo.value.issues
    assert any(
        Path(i.source).name == file_name and i.location == location and fragment in i.message
        for i in issues
    ), issues


def test_all_broken_files_are_reported_in_one_error() -> None:
    with pytest.raises(ConfigError) as excinfo:
        load_config_dir(CROSS_DIR / "two_broken_files")

    reported = {(Path(i.source).name, i.location) for i in excinfo.value.issues}
    assert reported == {("risk.yaml", "risk_per_trade_pct"), ("sample_2step.yaml", "platform")}
    assert str(excinfo.value).count("\n") == 1  # one line per issue


def test_missing_directory_reports_both_gaps(tmp_path: Path) -> None:
    with pytest.raises(ConfigError) as excinfo:
        load_config_dir(tmp_path)

    messages = [i.message for i in excinfo.value.issues]
    assert messages == ["dosya bulunamadı", "venue profili yok (*.yaml)"]


@pytest.mark.parametrize(
    ("daily_cap_pct", "consistent"),
    [(1.0, True), (2.5, True), (2.5000001, False), (3.0, False)],
)
def test_daily_cap_may_not_exceed_half_the_daily_loss_limit(
    daily_cap_pct: float, consistent: bool
) -> None:
    risk = load_risk_config(VALID_DIR / "risk.yaml").model_copy(
        update={"daily_cap_pct": daily_cap_pct}
    )
    venue = load_venue_profile(VALID_DIR / "venues" / "sample_2step.yaml")  # daily limit 5.0

    issues = cross_check(risk, venue, risk_source="risk.yaml", venue_source="venue.yaml")

    assert (issues == []) is consistent
    if not consistent:
        assert [i.location for i in issues] == ["daily_cap_pct"]


def test_daily_budget_stays_capped_even_if_the_cross_check_is_skipped() -> None:
    # Second layer of SPEC §8.1: budget = min(daily_cap_pct, 0.5 x daily_loss_limit_pct).
    risk = load_risk_config(VALID_DIR / "risk.yaml").model_copy(update={"daily_cap_pct": 4.0})
    venue = load_venue_profile(VALID_DIR / "venues" / "sample_2step.yaml")

    assert daily_budget_pct(risk, venue) == 2.5
    assert daily_budget_amount(risk, venue) == 2500.0


def test_request_budget_may_equal_the_firm_threshold() -> None:
    risk = load_risk_config(VALID_DIR / "risk.yaml")  # 300 requests per day
    venue = load_venue_profile(VALID_DIR / "venues" / "sample_2step.yaml")

    at_threshold = venue.model_copy(update={"firm_request_threshold_per_day": 300})
    below = venue.model_copy(update={"firm_request_threshold_per_day": 299})

    assert cross_check(risk, at_threshold, risk_source="r", venue_source="v") == []
    assert [i.location for i in cross_check(risk, below, risk_source="r", venue_source="v")] == [
        "self_request_budget_per_day"
    ]


def test_loaded_venues_mapping_is_read_only() -> None:
    config = load_config_dir(VALID_DIR)

    with pytest.raises(TypeError):
        config.venues["other"] = config.venues["sample_2step"]  # type: ignore[index]


def test_duplicate_key_inside_a_nested_mapping_is_rejected(tmp_path: Path) -> None:
    path = tmp_path / "venue.yaml"
    path.write_text("contract:\n  tick_size: 0.25\n  tick_size: 0.5\n", encoding="utf-8")

    with pytest.raises(ConfigError) as excinfo:
        read_yaml_mapping(path)

    assert excinfo.value.issues[0].message == "yinelenen anahtar 'tick_size' (satır 3)"


def test_yaml_tags_cannot_construct_python_objects(tmp_path: Path) -> None:
    path = tmp_path / "risk.yaml"
    path.write_text("risk_per_trade_pct: !!python/object/apply:os.getcwd []\n", encoding="utf-8")

    with pytest.raises(ConfigError) as excinfo:
        read_yaml_mapping(path)

    assert "YAML okunamadı" in excinfo.value.issues[0].message


def test_non_utf8_file_is_reported_not_raised(tmp_path: Path) -> None:
    path = tmp_path / "risk.yaml"
    path.write_bytes(b"risk_per_trade_pct: \xff\xfe\n")

    with pytest.raises(ConfigError) as excinfo:
        read_yaml_mapping(path)

    assert excinfo.value.issues[0].message.startswith("dosya okunamadı")
