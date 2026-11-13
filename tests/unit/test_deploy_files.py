"""Deployment files must stay consistent with the config they mirror (SPEC §5.3, §5.6)."""

import re

import yaml

from support.paths import CONFIG_DIR, REPO_ROOT

DEPLOY = REPO_ROOT / "deploy"
RUNBOOK = REPO_ROOT / "docs" / "runbook.md"
IPV4 = re.compile(r"\b(?:\d{1,3}\.){3}\d{1,3}\b")
LOOPBACK = "127.0.0.1"


def tradingview_ips() -> set[str]:
    data = yaml.safe_load((CONFIG_DIR / "tv_alerts.yaml").read_text(encoding="utf-8"))
    return set(data["webhook"]["source_ips"])


def addresses_on_line_starting_with(text: str, prefix: str) -> set[str]:
    lines = [line for line in text.splitlines() if line.strip().startswith(prefix)]
    assert len(lines) == 1, f"expected exactly one line starting with {prefix!r}"
    return set(IPV4.findall(lines[0]))


def test_caddyfile_admits_exactly_the_configured_tradingview_addresses() -> None:
    caddyfile = (DEPLOY / "Caddyfile").read_text(encoding="utf-8")

    assert addresses_on_line_starting_with(caddyfile, "remote_ip") == tradingview_ips()


def test_caddyfile_forwards_only_the_webhook_route_to_loopback() -> None:
    directives = [
        line.strip()
        for line in (DEPLOY / "Caddyfile").read_text(encoding="utf-8").splitlines()
        if line.strip() and not line.strip().startswith("#")
    ]

    assert "method POST" in directives
    assert "path /tv/webhook" in directives
    assert [line for line in directives if line.startswith("reverse_proxy")] == [
        f"reverse_proxy {LOOPBACK}:8000"
    ]
    assert "abort" in directives  # everything else is dropped
    assert "max_size 4KiB" in directives  # same cap as receiver.max_body_bytes (4096)


def test_runbook_firewall_rule_lists_the_same_addresses() -> None:
    runbook = RUNBOOK.read_text(encoding="utf-8")

    assert addresses_on_line_starting_with(runbook, "for ip in") == tradingview_ips()


def test_runbook_smoke_test_uses_a_configured_address() -> None:
    runbook = RUNBOOK.read_text(encoding="utf-8")
    forwarded = re.findall(r"X-Forwarded-For: ((?:\d{1,3}\.){3}\d{1,3})", runbook)

    assert forwarded
    assert set(forwarded) <= tradingview_ips()


def test_systemd_unit_runs_signal_mode_and_restarts() -> None:
    unit = (DEPLOY / "propbot.service").read_text(encoding="utf-8")
    exec_lines = [line for line in unit.splitlines() if line.startswith("ExecStart=")]

    assert len(exec_lines) == 1
    assert "propbot serve --mode signal" in exec_lines[0]
    for other_mode in ("paper", "approve", "auto"):
        assert other_mode not in exec_lines[0]
    assert "Restart=always" in unit.splitlines()
    assert "UMask=0077" in unit.splitlines()
    assert "EnvironmentFile" not in unit  # secrets are read by the app from a 0600 .env
