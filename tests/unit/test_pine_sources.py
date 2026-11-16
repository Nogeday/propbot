"""Static checks on `pine/*.pine`: what the scripts send must be what the receiver knows.

Pine cannot be compiled or run here (the user does that in TradingView, see
`docs/tv-verification-checklist.md`). These tests only read the source text and pin the
few facts the Python side depends on: the strategy ids each script sends, that no two
scripts share one, and that the webhook token input is hidden from the chart.
"""

import re

import pytest

from propbot.signals.schema import HEARTBEAT_STRATEGY_ID, STRATEGY_NAMES
from support.paths import REPO_ROOT

PINE_DIR = REPO_ROOT / "pine"

# file name -> strategy ids its alerts carry
SENT_IDS: dict[str, set[str]] = {
    "ny_open_fvg_strategy.pine": {"ny_orb_fvg"},
    "asian_sweep_reversal_strategy.pine": {"asian_sweep"},
    "ict_silver_bullet_strategy.pine": {"ict_silver_bullet"},
    "mean_reversion_bb_kc_rsi2_strategy.pine": {"mr_bb_kc_rsi2"},
    "mtf_trend_continuation_strategy.pine": {"mtf_trend_cont"},
    # User decision 47: the Master script's legs have ids of their own.
    "master_prop_firm_portfolio.pine": {"master_ny_orb_fvg", "master_asian_sweep"},
    "heartbeat_indicator.pine": {HEARTBEAT_STRATEGY_ID},
}

# `"strategy_id":"ny_orb_fvg"` written into the JSON text of `f_alert`.
_LITERAL_ID = re.compile(r'"strategy_id":"([a-z0-9_]+)"')
# `f_alert("master_ny_orb_fvg", ...)`: the Master script passes the id as the first argument.
_ARGUMENT_ID = re.compile(r'\bf_alert\("([a-z0-9_]+)",\s*"(?:entry|stop_update|exit|cancel)"')
_TOKEN_INPUT = re.compile(r"^alertToken\s*=\s*input\.string\(.*$", re.MULTILINE)


def source(name: str) -> str:
    return (PINE_DIR / name).read_text(encoding="utf-8")


def code_lines(name: str) -> str:
    """The script without its `//` comment lines (ids quoted in comments are not sent)."""
    return "\n".join(
        line for line in source(name).splitlines() if not line.lstrip().startswith("//")
    )


def sent_ids(name: str) -> set[str]:
    code = code_lines(name)
    return set(_LITERAL_ID.findall(code)) | set(_ARGUMENT_ID.findall(code))


def test_every_pine_file_is_listed() -> None:
    assert {path.name for path in PINE_DIR.glob("*.pine")} == set(SENT_IDS)


@pytest.mark.parametrize("name", sorted(SENT_IDS))
def test_each_script_sends_exactly_its_own_strategy_ids(name: str) -> None:
    assert sent_ids(name) == SENT_IDS[name]


def test_no_two_scripts_send_the_same_strategy_id() -> None:
    # One id from two scripts on the same symbol is one stream to the receiver: the second
    # alert is dropped as a duplicate and follow-ups are tied to the wrong entry.
    seen: dict[str, str] = {}
    for name in sorted(SENT_IDS):
        for strategy_id in sent_ids(name):
            assert strategy_id not in seen, (strategy_id, name, seen[strategy_id])
            seen[strategy_id] = name


def test_the_receiver_knows_every_id_a_script_sends_and_no_other() -> None:
    sent = set().union(*(sent_ids(name) for name in SENT_IDS))

    assert sent == set(STRATEGY_NAMES) | {HEARTBEAT_STRATEGY_ID}


@pytest.mark.parametrize("name", sorted(SENT_IDS))
def test_the_webhook_token_input_is_hidden_from_the_chart(name: str) -> None:
    # User decision 45: with the default display the token is printed next to the script's
    # name on the chart, so a screenshot or a shared chart would give it away.
    (declaration,) = _TOKEN_INPUT.findall(source(name))

    assert "display = display.none" in declaration
    assert '"Webhook token"' in declaration
    # The default stays empty: a token is never written into the source.
    assert re.match(r'alertToken\s*=\s*input\.string\(""\s*,', declaration)


@pytest.mark.parametrize("name", sorted(SENT_IDS))
def test_the_token_reaches_the_alert_text_only_through_that_input(name: str) -> None:
    code = code_lines(name)

    assert code.count('"token":"\' + alertToken + \'"') == 1
    assert len(re.findall(r"\balertToken\b", code)) == 2  # its declaration and that one use


@pytest.mark.parametrize("name", sorted(SENT_IDS))
def test_every_script_stays_on_pine_v5(name: str) -> None:
    assert source(name).splitlines()[0] == "//@version=5"
