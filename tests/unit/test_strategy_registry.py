"""The strategy registry and `config/strategies.yaml`."""

import datetime as dt
from pathlib import Path

import pytest

from propbot.risk.errors import ConfigError
from propbot.signals.schema import STRATEGY_NAMES
from propbot.strategies.config import load_strategy_overrides
from propbot.strategies.registry import STRATEGIES, StrategyError, StrategySpec, strategy_spec
from support.paths import REPO_ROOT
from support.pine import PINE_DIR

REAL_FILE = REPO_ROOT / "config" / "strategies.yaml"
MASTER = "master_portfolio"


def write(tmp_path: Path, text: str) -> Path:
    path = tmp_path / "strategies.yaml"
    path.write_text(text, encoding="utf-8")
    return path


def issues(tmp_path: Path, text: str) -> str:
    with pytest.raises(ConfigError) as caught:
        load_strategy_overrides(write(tmp_path, text))
    return str(caught.value)


def test_every_alert_id_belongs_to_exactly_one_registered_port() -> None:
    # The ids the Pine scripts send (and the receiver knows) are the ids trade records
    # carry: each must come from exactly one port, and no port may record another id.
    legs = [leg for spec in STRATEGIES.values() for leg in spec.leg_ids]

    assert sorted(legs) == sorted(STRATEGY_NAMES)
    assert len(legs) == len(set(legs))


def test_single_strategy_ports_record_their_own_id_and_the_master_its_two_legs() -> None:
    for strategy_id, spec in STRATEGIES.items():
        if strategy_id == MASTER:
            assert spec.leg_ids == ("master_ny_orb_fvg", "master_asian_sweep")
            assert strategy_id not in STRATEGY_NAMES
        else:
            assert spec.leg_ids == (strategy_id,)


def test_every_port_mirrors_one_pine_script_and_every_strategy_script_has_a_port() -> None:
    mirrored = sorted(spec.pine_file for spec in STRATEGIES.values())
    scripts = sorted(
        path.name for path in PINE_DIR.glob("*.pine") if path.name != "heartbeat_indicator.pine"
    )

    assert mirrored == scripts


@pytest.mark.parametrize("strategy_id", sorted(STRATEGIES))
def test_a_port_builds_from_its_defaults_and_reports_the_registered_id(strategy_id: str) -> None:
    spec = strategy_spec(strategy_id)

    first, second = spec.build(), spec.build()

    assert first.strategy_id == strategy_id
    assert first.pyramiding >= 0
    assert first is not second  # instances hold state: one per run
    assert spec.params() == spec.params_model()


# The Pine default of every script's risk input; the account is planned at
# config/risk.yaml's value instead (user decision of 2026-11-19, open finding 82).
PINE_RISK_PCT = 0.5
RISK_PARAMS = {
    "ny_orb_fvg": "risk_pct",
    "asian_sweep": "risk_pct",
    "ict_silver_bullet": "risk_pct",
    "mr_bb_kc_rsi2": "risk_pct",
    "mtf_trend_cont": "risk_percent",  # the MTF script calls it riskPercent
    MASTER: "risk_pct",
}


def test_every_port_names_its_risk_input_and_the_ports_default_is_the_pine_default() -> None:
    assert {sid: spec.risk_param for sid, spec in STRATEGIES.items()} == RISK_PARAMS
    for strategy_id, spec in STRATEGIES.items():
        field = spec.params_model.model_fields[spec.risk_param]
        # The port itself is untouched by the rule: its default stays the script's.
        assert field.default == PINE_RISK_PCT, strategy_id
        assert spec.pine_risk_pct == PINE_RISK_PCT
        assert spec.params().model_dump()[spec.risk_param] == PINE_RISK_PCT
        # No port has both names: the registry's name is the only risk input.
        others = {"risk_pct", "risk_percent"} - {spec.risk_param}
        assert not others & set(spec.params_model.model_fields), strategy_id


def test_only_the_master_has_a_second_risk_input_the_planned_risk_leaves_alone() -> None:
    caps = {sid: spec.risk_cap_param for sid, spec in STRATEGIES.items() if spec.risk_cap_param}

    assert caps == {MASTER: "max_concurrent_risk_pct"}
    assert STRATEGIES[MASTER].params().model_dump()["max_concurrent_risk_pct"] == 1.0


@pytest.mark.parametrize("strategy_id", sorted(STRATEGIES))
def test_the_planned_risk_replaces_the_risk_input_and_nothing_else(strategy_id: str) -> None:
    spec = strategy_spec(strategy_id)
    defaults = spec.params().model_dump()
    # Any other input of the script (the ports share no name but the risk base).
    name, value = next((k, v) for k, v in defaults.items() if isinstance(v, bool))
    override = {name: not value}

    planned = spec.params(spec.with_planned_risk(override, 0.25)).model_dump()

    assert planned == {**defaults, name: not value, spec.risk_param: 0.25}
    assert spec.with_planned_risk(None, None) == {}
    assert spec.with_planned_risk(override, None) == override
    # The risk input itself is never an override, with or without a planned risk.
    for planned_risk in (None, 0.25):
        with pytest.raises(StrategyError) as caught:
            spec.with_planned_risk({spec.risk_param: 0.25}, planned_risk)
        assert spec.risk_param in str(caught.value)
        assert "config/risk.yaml" in str(caught.value)


def test_a_registry_entry_cannot_name_a_risk_input_its_script_does_not_have() -> None:
    real = strategy_spec("ny_orb_fvg")

    for changes in ({"risk_param": "risk_percent"}, {"risk_cap_param": "no_such_cap"}):
        fields = {
            "strategy_id": real.strategy_id,
            "pine_file": real.pine_file,
            "params_model": real.params_model,
            "strategy_class": real.strategy_class,
            "leg_ids": real.leg_ids,
            "risk_param": real.risk_param,
            **changes,
        }
        with pytest.raises(ValueError, match="has no field"):
            StrategySpec(**fields)  # type: ignore[arg-type]


def test_an_override_is_applied_and_an_unknown_or_bad_one_is_refused() -> None:
    spec = strategy_spec("ny_orb_fvg")

    assert spec.params({"rr": 3.0}).model_dump()["rr"] == 3.0
    for overrides in ({"no_such_input": 1}, {"rr": "3"}, {"rr": -1.0}):
        with pytest.raises(StrategyError) as caught:
            spec.build(overrides)
        assert "ny_orb_fvg" in str(caught.value)
        assert next(iter(overrides)) in str(caught.value)


def test_an_unknown_strategy_id_names_the_known_ones() -> None:
    with pytest.raises(StrategyError) as caught:
        strategy_spec("ny_orb")

    assert "ny_orb_fvg" in str(caught.value)
    assert MASTER in str(caught.value)


def test_the_real_file_lists_every_port_and_changes_nothing() -> None:
    overrides = load_strategy_overrides(REAL_FILE)

    assert sorted(overrides) == sorted(STRATEGIES)
    assert all(dict(values) == {} for values in overrides.values())


def test_a_strategy_the_file_does_not_mention_runs_on_the_pine_defaults(tmp_path: Path) -> None:
    overrides = load_strategy_overrides(write(tmp_path, "strategies:\n  asian_sweep: {rr: 2.5}\n"))

    assert dict(overrides["asian_sweep"]) == {"rr": 2.5}
    assert dict(overrides["ny_orb_fvg"]) == {}
    assert sorted(overrides) == sorted(STRATEGIES)


def test_an_empty_entry_means_defaults(tmp_path: Path) -> None:
    overrides = load_strategy_overrides(write(tmp_path, "strategies:\n  asian_sweep:\n"))

    assert dict(overrides["asian_sweep"]) == {}


def test_a_date_is_written_as_a_yaml_timestamp_with_an_offset(tmp_path: Path) -> None:
    path = write(tmp_path, "strategies:\n  ny_orb_fvg:\n    start_date: 2025-06-01T00:00:00Z\n")

    params = strategy_spec("ny_orb_fvg").params(load_strategy_overrides(path)["ny_orb_fvg"])

    assert params.model_dump()["start_date"] == dt.datetime(2025, 6, 1, tzinfo=dt.UTC)


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        ("strategies:\n  ny_orb: {}\n", "strategies.ny_orb"),
        ("strategies:\n  ny_orb_fvg: {no_such_input: 1}\n", "strategies.ny_orb_fvg.no_such_input"),
        ("strategies:\n  ny_orb_fvg: {rr: -1}\n", "strategies.ny_orb_fvg.rr"),
        ('strategies:\n  ny_orb_fvg: {rr: "2"}\n', "strategies.ny_orb_fvg.rr"),
        ("strategies:\n  ny_orb_fvg: [1, 2]\n", "strategies.ny_orb_fvg"),
        ("strategies:\n  ny_orb_fvg: {1: 2}\n", "strategies.ny_orb_fvg"),
        ("strategies: []\n", "strategies"),
        ("other: {}\n", "other"),
        ("strategies: {}\nextra: 1\n", "extra"),
        (
            "strategies:\n  ny_orb_fvg: {start_date: 2025-06-01}\n",
            "strategies.ny_orb_fvg.start_date",
        ),
        # The risk input has one source, config/risk.yaml (2026-11-19): refused here
        # under each script's own name for it.
        ("strategies:\n  ny_orb_fvg: {risk_pct: 0.5}\n", "strategies.ny_orb_fvg.risk_pct"),
        (
            "strategies:\n  mtf_trend_cont: {risk_percent: 0.25}\n",
            "strategies.mtf_trend_cont.risk_percent",
        ),
        (
            "strategies:\n  master_portfolio: {risk_pct: 0.25}\n",
            "strategies.master_portfolio.risk_pct",
        ),
    ],
)
def test_every_kind_of_mistake_is_a_load_error_that_names_its_place(
    tmp_path: Path, text: str, expected: str
) -> None:
    assert expected in issues(tmp_path, text)


def test_all_mistakes_are_reported_at_once(tmp_path: Path) -> None:
    text = "strategies:\n  ny_orb: {}\n  asian_sweep: {rr: -1}\n  mtf_trend_cont: {zzz: 1}\n"

    report = issues(tmp_path, text)

    assert "strategies.ny_orb" in report
    assert "strategies.asian_sweep.rr" in report
    assert "strategies.mtf_trend_cont.zzz" in report
