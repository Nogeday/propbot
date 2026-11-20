"""The Pine input reader the strategy ports' default tests rely on (`tests/support/pine.py`)."""

import datetime as dt

import pytest

from support.pine import PINE_DIR, PineInputError, _default, pine_defaults, pine_inputs

STRATEGY_FILES = sorted(
    path.name for path in PINE_DIR.glob("*.pine") if path.name != "heartbeat_indicator.pine"
)


@pytest.mark.parametrize("name", [*STRATEGY_FILES, "heartbeat_indicator.pine"])
def test_every_input_line_of_every_script_is_understood(name: str) -> None:
    source = (PINE_DIR / name).read_text(encoding="utf-8")
    declared = sum(
        1 for line in source.splitlines() if "input." in line and not line.lstrip().startswith("//")
    )

    assert len(pine_inputs(name)) == declared > 0


def test_defaults_of_the_reference_script_are_read_with_their_types() -> None:
    defaults = pine_defaults("ny_open_fvg_strategy.pine")

    assert defaults["tz"] == "America/New_York"
    assert defaults["orStartHour"] == 9
    assert defaults["orStartMin"] == 30
    assert defaults["orMinutes"] == 5
    assert defaults["sessEndHour"] == 15
    assert defaults["sessEndMin"] == 59
    assert defaults["closeAtEnd"] is True
    assert defaults["riskPct"] == 0.5
    assert defaults["rr"] == 2.0
    assert defaults["breakConfirm"] == "Close"
    assert defaults["stopMethod"] == "FVG Buffer"
    assert defaults["showAllFVG"] is False
    assert defaults["alertToken"] == ""
    assert defaults["startDate"] == dt.datetime(2024, 1, 1, tzinfo=dt.UTC)
    assert defaults["oosStart"] == dt.datetime(2025, 1, 1, tzinfo=dt.UTC)
    assert defaults["endDate"] == dt.datetime(2030, 1, 1, tzinfo=dt.UTC)
    assert type(defaults["orMinutes"]) is int
    assert type(defaults["riskPct"]) is float


def test_the_title_is_the_second_argument() -> None:
    inputs = pine_inputs("ny_open_fvg_strategy.pine")

    assert inputs["riskPct"].title == "Risk Per Trade (%)"
    assert inputs["riskPct"].kind == "float"
    assert inputs["startDate"].title == "Start date"


def test_the_timeframe_input_of_the_mtf_script_is_read() -> None:
    assert pine_defaults("mtf_trend_continuation_strategy.pine")["htfTf"] == "60"


@pytest.mark.parametrize(
    ("kind", "args"),
    [
        ("bool", 'maybe, "x")'),
        ("int", '1.5, "x")'),
        ("int", 'someVar, "x")'),
        ("float", 'a + 1, "x")'),
        ("string", "someVar)"),
        ("time", '1700000000000, "x")'),
        ("source", 'close, "x")'),
    ],
)
def test_an_unknown_form_is_refused_rather_than_guessed(kind: str, args: str) -> None:
    with pytest.raises(PineInputError):
        _default(kind, args, "test:1")


def test_a_negative_number_and_a_single_argument_are_read() -> None:
    assert _default("int", "-3)", "test:1")[0] == -3
    assert _default("float", '-0.25, "x")', "test:1")[0] == -0.25
    assert _default("bool", "false)", "test:1")[0] is False
