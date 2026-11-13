"""Mode locks (CLAUDE.md invariant 1): only `signal` runs in this build."""

import datetime as dt

import pytest

from propbot.risk import load_venue_profile
from propbot.risk.modes import Mode, mode_refusal
from support.paths import CONFIG_FIXTURES

TODAY = dt.date(2026, 11, 13)
VERIFIED = load_venue_profile(CONFIG_FIXTURES / "valid" / "venues" / "sample_2step.yaml")
UNVERIFIED = load_venue_profile(
    CONFIG_FIXTURES / "valid_unverified" / "venues" / "sample_unverified.yaml"
)


def test_signal_mode_always_runs() -> None:
    assert mode_refusal(Mode.SIGNAL, VERIFIED, TODAY) is None
    assert mode_refusal(Mode.SIGNAL, UNVERIFIED, TODAY) is None


@pytest.mark.parametrize("mode", [Mode.PAPER, Mode.APPROVE, Mode.AUTO])
def test_every_other_mode_is_refused_even_with_fresh_rules(mode: Mode) -> None:
    reason = mode_refusal(mode, VERIFIED, TODAY)

    assert reason is not None
    assert "yalnızca 'signal'" in reason


def test_order_modes_also_report_stale_rules() -> None:
    approve = mode_refusal(Mode.APPROVE, UNVERIFIED, TODAY)
    paper = mode_refusal(Mode.PAPER, UNVERIFIED, TODAY)

    assert approve is not None
    assert "kuralları taze değil" in approve
    assert paper is not None
    assert "kuralları taze değil" not in paper  # paper never needed fresh rules (SPEC §8.1)


def test_auto_lists_the_remaining_conditions() -> None:
    reason = mode_refusal(Mode.AUTO, VERIFIED, TODAY)

    assert reason is not None
    for fragment in ("live.enabled", "--i-accept-live-risk", "PIN", "Go/No-Go"):
        assert fragment in reason


def test_mode_values_match_the_cli_spelling() -> None:
    assert [mode.value for mode in Mode] == ["signal", "paper", "approve", "auto"]
