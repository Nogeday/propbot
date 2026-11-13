"""TvAlertsConfig: TradingView webhook rules, receiver limits, watch windows and alert expiry."""

import copy
import datetime as dt
import ipaddress
from ipaddress import IPv4Address, IPv4Network, IPv6Network
from pathlib import Path
from typing import Any

import pytest
from pydantic import ValidationError

from propbot.risk import ConfigError
from propbot.risk.loader import read_yaml_mapping
from propbot.signals.tvconfig import (
    MAX_ALERT_NAME_LENGTH,
    MAX_BODY_BYTES,
    MAX_EXPIRY_REMINDER_DAYS,
    MAX_HEARTBEAT_SILENCE_MINUTES,
    MAX_RATE_LIMIT_PER_MINUTE,
    MAX_TRUSTED_PROXY_ADDRESSES,
    MIN_BODY_BYTES,
    TV_ALERTS_FILE_NAME,
    AlertExpiry,
    HeartbeatConfig,
    ReceiverConfig,
    TvAlertsConfig,
    WatchWindow,
    WebhookRules,
    Weekday,
    load_tv_alerts_config,
)
from support.alerts import alert_bytes
from support.paths import CONFIG_DIR, FIXTURES

REAL = CONFIG_DIR / TV_ALERTS_FILE_NAME
TV_FIXTURES = FIXTURES / "tv_alerts"
VALID = TV_FIXTURES / "valid" / "full.yaml"
EDGE_VALUES = TV_FIXTURES / "valid" / "edge_values.yaml"
INVALID_DIR = TV_FIXTURES / "invalid"

# TradingView's webhook source addresses as the user copied them from the official
# "About webhooks" page on 2026-11-13 (SPEC §1).
TRADINGVIEW_IPS = ("52.89.214.238", "34.212.75.30", "54.218.53.128", "52.32.178.7")

NOT_GLOBAL = "internetten gelen (global) bir adres olmalı"
NOT_AN_IPV4 = "noktalı dörtlü tek bir IPv4 adresi olmalı"
NOT_PRIVATE = "yalnızca loopback ya da özel ağ adresi olabilir"

# fixture stem -> (issue location, fragment of the Turkish message)
MUST_FAIL: dict[str, tuple[str, str]] = {
    # file-level rules
    "empty": ("", "eşlemesi olmalı"),
    "not_a_mapping": ("", "eşlemesi olmalı"),
    "syntax_error": ("", "YAML okunamadı (satır"),
    "duplicate_key": ("", "yinelenen anahtar 'source_ips' (satır 6)"),
    "unknown_key": ("webhook_url", "bilinmeyen anahtar"),
    "unknown_nested_key": ("receiver.bind_host", "bilinmeyen anahtar"),
    "missing_block": ("heartbeat", "zorunlu alan eksik"),
    "missing_key": ("receiver.max_body_bytes", "zorunlu alan eksik"),
    "alerts_key_missing": ("alerts", "zorunlu alan eksik"),
    "block_not_a_mapping": ("receiver", "eşlemesi olmalı"),
    # webhook
    "verified_date_null": ("webhook.rules_verified_at", "YYYY-AA-GG biçiminde"),
    "verified_date_bad_format": ("webhook.rules_verified_at", "okunan: '13.11.2026'"),
    "verified_date_with_time": ("webhook.rules_verified_at", "saat içermemeli"),
    "source_ips_empty": ("webhook.source_ips", "en az bir öğe içermeli"),
    "source_ips_not_a_list": ("webhook.source_ips", "liste olmalı; okunan: '52.89.214.238'"),
    "source_ip_duplicate": ("webhook.source_ips", "yinelenen öğe: 52.89.214.238"),
    "source_ip_loopback": ("webhook.source_ips.4", f"{NOT_GLOBAL}"),
    "source_ip_private": ("webhook.source_ips.4", f"{NOT_GLOBAL}"),
    "source_ip_reserved": ("webhook.source_ips.4", f"{NOT_GLOBAL}"),
    "source_ip_multicast": ("webhook.source_ips.4", f"{NOT_GLOBAL}"),
    "source_ip_ipv6": ("webhook.source_ips.4", f"{NOT_AN_IPV4}"),
    "source_ip_with_port": ("webhook.source_ips.4", "okunan: '52.89.214.238:443'"),
    "source_ip_leading_zero": ("webhook.source_ips.4", "okunan: '052.89.214.238'"),
    "source_ip_cidr": ("webhook.source_ips.4", f"{NOT_AN_IPV4}"),
    "source_ip_octet_out_of_range": ("webhook.source_ips.4", f"{NOT_AN_IPV4}"),
    "source_ip_number": ("webhook.source_ips.4", "metin olmalı; okunan: 52.89"),
    "allowed_ports_empty": ("webhook.allowed_ports", "en az bir öğe içermeli"),
    "allowed_port_out_of_range": ("webhook.allowed_ports.1", "en fazla 65535 olabilir"),
    "allowed_port_duplicate": ("webhook.allowed_ports", "yinelenen öğe: 443"),
    "allowed_port_quoted": ("webhook.allowed_ports.1", "tam sayı olmalı; okunan: '443'"),
    "response_timeout_zero": ("webhook.response_timeout_seconds", "0.0 değerinden büyük olmalı"),
    "response_timeout_quoted": ("webhook.response_timeout_seconds", "sayı olmalı"),
    "mtls_true": ("webhook.mtls_supported", "false olmalı: TradingView istemci sertifikası"),
    "mtls_quoted": ("webhook.mtls_supported", "true ya da false olmalı; okunan: 'false'"),
    # receiver
    "trusted_proxies_empty": ("receiver.trusted_proxies", "en az bir öğe içermeli"),
    "trusted_proxy_public": ("receiver.trusted_proxies.1", f"{NOT_PRIVATE}"),
    "trusted_proxy_any": ("receiver.trusted_proxies.0", "okunan: '0.0.0.0/0'"),
    "trusted_proxy_too_broad": ("receiver.trusted_proxies.1", "en fazla 256 adres kapsayabilir"),
    "trusted_proxy_host_bits": ("receiver.trusted_proxies.0", "host bitleri sıfır olmalı"),
    "trusted_proxy_netmask_form": (
        "receiver.trusted_proxies.0",
        "kanonik biçimde yazılmalı: 192.168.1.0/24",
    ),
    "trusted_proxy_zone_id": ("receiver.trusted_proxies.1", "bölge kimliği"),
    "trusted_proxy_hostname": ("receiver.trusted_proxies.0", "IP adresi ya da CIDR ağı olmalı"),
    "trusted_proxy_duplicate": ("receiver.trusted_proxies", "yinelenen öğe: 127.0.0.1/32"),
    "client_ip_header_invalid": (
        "receiver.client_ip_header",
        "yalnızca X-Forwarded-For desteklenir",
    ),
    "rate_limit_above_cap": ("receiver.rate_limit_per_minute", "en fazla 30 olabilir; okunan: 31"),
    "rate_limit_zero": ("receiver.rate_limit_per_minute", "en az 1 olmalı"),
    "rate_limit_float": ("receiver.rate_limit_per_minute", "tam sayı olmalı"),
    "max_body_above_cap": ("receiver.max_body_bytes", "en fazla 4096 olabilir; okunan: 8192"),
    "max_body_below_min": ("receiver.max_body_bytes", "en az 1024 olmalı; okunan: 512"),
    # heartbeat
    "silence_above_cap": ("heartbeat.max_silence_minutes", "en fazla 75 olabilir; okunan: 90"),
    "silence_zero": ("heartbeat.max_silence_minutes", "en az 1 olmalı"),
    "watch_windows_empty": ("heartbeat.watch_windows", "en az bir öğe içermeli"),
    "window_days_empty": ("heartbeat.watch_windows.0.days", "en az bir öğe içermeli"),
    "window_day_unknown": (
        "heartbeat.watch_windows.0.days.0",
        "izin verilenler: 'mon', 'tue', 'wed', 'thu', 'fri', 'sat', 'sun'; okunan: 'monday'",
    ),
    "window_day_duplicate": ("heartbeat.watch_windows.0.days", "yinelenen öğe: mon"),
    "window_time_unquoted": ("heartbeat.watch_windows.0.end", "okunan: 960"),
    "window_time_out_of_range": ("heartbeat.watch_windows.0.end", "okunan: '24:00'"),
    "window_start_after_end": (
        "heartbeat.watch_windows.0",
        "start (18:00) end değerinden (02:00) önce olmalı",
    ),
    "window_start_equals_end": (
        "heartbeat.watch_windows.0",
        "start (09:30) end değerinden (09:30) önce olmalı",
    ),
    "window_key_missing": ("heartbeat.watch_windows.0.end", "zorunlu alan eksik"),
    "window_unknown_key": ("heartbeat.watch_windows.0.tz", "bilinmeyen anahtar"),
    # alerts
    "alerts_null": ("alerts", "liste olmalı"),
    "alert_name_duplicate": ("alerts", "yinelenen öğe: heartbeat MES1! 1m"),
    "alert_name_empty": ("alerts.0.name", "boş olamaz"),
    "alert_name_too_long": ("alerts.0.name", "en fazla 64 karakter olabilir; okunan: 65"),
    "alert_name_multiline": ("alerts.0.name", "kontrol karakteri olamaz"),
    "alert_expiry_key_missing": ("alerts.1.expires_on", "zorunlu alan eksik"),
    "alert_expiry_bad_date": ("alerts.0.expires_on", "okunan: '12.01.2027'"),
    "alert_expiry_with_time": ("alerts.0.expires_on", "saat içermemeli"),
    "alert_unknown_key": ("alerts.0.created_on", "bilinmeyen anahtar"),
    "alert_not_a_mapping": ("alerts.1", "eşlemesi olmalı"),
    "reminder_days_above_cap": ("expiry_reminder_days", "en fazla 30 olabilir; okunan: 31"),
    "reminder_days_zero": ("expiry_reminder_days", "en az 1 olmalı"),
    # aggregation (the full list is pinned in its own test)
    "several_problems": ("webhook.mtls_supported", "false olmalı"),
}

SOURCE_IPS = ("webhook", "source_ips")
TRUSTED_PROXIES = ("receiver", "trusted_proxies")
WINDOW = ("heartbeat", "watch_windows", 0)
ALERTS = ("alerts",)

KeyPath = tuple[str | int, ...]


def _valid_data() -> dict[Any, Any]:
    return read_yaml_mapping(VALID)


def _with(path: KeyPath, value: object) -> dict[Any, Any]:
    """The valid fixture with the value at `path` replaced."""
    data = _valid_data()
    target: Any = data
    for key in path[:-1]:
        target = target[key]
    target[path[-1]] = value
    return data


def _without(path: KeyPath) -> dict[Any, Any]:
    """The valid fixture with the key at `path` removed."""
    data = _valid_data()
    target: Any = data
    for key in path[:-1]:
        target = target[key]
    del target[path[-1]]
    return data


def _errors(data: dict[Any, Any]) -> list[tuple[KeyPath, str]]:
    """(location, error type) of every validation error for `data`."""
    with pytest.raises(ValidationError) as excinfo:
        TvAlertsConfig.model_validate(data)
    return [(error["loc"], error["type"]) for error in excinfo.value.errors()]


def _leaf_paths(node: object, prefix: KeyPath = ()) -> list[KeyPath]:
    """Every key path of the valid fixture: mappings, list items that are mappings, and leaves."""
    paths: list[KeyPath] = []
    if isinstance(node, dict):
        for key, value in node.items():
            paths.append((*prefix, key))
            paths.extend(_leaf_paths(value, (*prefix, key)))
    elif isinstance(node, list):
        for index, value in enumerate(node):
            if isinstance(value, dict):
                paths.extend(_leaf_paths(value, (*prefix, index)))
    return paths


ALL_KEY_PATHS = _leaf_paths(_valid_data())
NULLABLE_PATHS = {("alerts", 1, "expires_on"), ("alerts", 0, "expires_on")}


def _path_id(path: KeyPath) -> str:
    return ".".join(str(part) for part in path)


# --------------------------------------------------------------------------- the real file


def test_file_name() -> None:
    assert TV_ALERTS_FILE_NAME == "tv_alerts.yaml"
    assert REAL.is_file()


def test_real_config_loads_within_the_caps() -> None:
    # Dates and the alert list change as the user maintains the file; only invariants are pinned.
    config = load_tv_alerts_config(REAL)

    assert config.webhook.mtls_supported is False
    assert config.webhook.response_timeout_seconds > 0
    assert 1 <= config.receiver.rate_limit_per_minute <= MAX_RATE_LIMIT_PER_MINUTE
    assert MIN_BODY_BYTES <= config.receiver.max_body_bytes <= MAX_BODY_BYTES
    assert 1 <= config.heartbeat.max_silence_minutes <= MAX_HEARTBEAT_SILENCE_MINUTES
    assert config.heartbeat.watch_windows
    assert 1 <= config.expiry_reminder_days <= MAX_EXPIRY_REMINDER_DAYS


def test_real_config_lists_exactly_the_four_tradingview_addresses() -> None:
    # A change here means TradingView changed its published list: update the YAML from the
    # official page together with deploy/Caddyfile and the firewall rules, then this tuple.
    webhook = load_tv_alerts_config(REAL).webhook

    assert set(webhook.source_ips) == set(TRADINGVIEW_IPS)
    assert len(webhook.source_ips) == 4
    assert webhook.source_addresses == {IPv4Address(text) for text in TRADINGVIEW_IPS}


def test_real_config_admits_no_private_or_loopback_source() -> None:
    for address in load_tv_alerts_config(REAL).webhook.source_addresses:
        assert address.is_global, address
        assert not address.is_private, address
        assert not address.is_loopback, address
        assert not address.is_link_local, address
        assert not address.is_multicast, address
        assert not address.is_reserved, address
        assert not address.is_unspecified, address


def test_real_config_trusts_only_narrow_local_proxies() -> None:
    receiver = load_tv_alerts_config(REAL).receiver

    assert receiver.trusted_networks
    for network in receiver.trusted_networks:
        assert network.is_loopback or network.is_private, network
        assert network.num_addresses <= MAX_TRUSTED_PROXY_ADDRESSES, network
    # The header a proxy writes must never be trusted from a TradingView (or any public) peer.
    for address in load_tv_alerts_config(REAL).webhook.source_addresses:
        assert not any(address in network for network in receiver.trusted_networks)


def test_real_config_matches_the_spec_defaults() -> None:
    # SPEC §1 (ports 80/443, 3 s), §5.2-§5.5 (4 KB, 30/min, 75 min, reminder 7 days before).
    config = load_tv_alerts_config(REAL)

    assert config.webhook.allowed_ports == (80, 443)
    assert config.webhook.response_timeout_seconds == 3
    assert config.receiver.trusted_proxies == ("127.0.0.1", "::1")
    assert config.receiver.client_ip_header == "X-Forwarded-For"
    assert config.receiver.rate_limit_per_minute == 30
    assert config.receiver.max_body_bytes == 4096
    assert config.heartbeat.max_silence_minutes == 75
    assert config.expiry_reminder_days == 7
    (window,) = config.heartbeat.watch_windows
    assert window.weekdays == {0, 1, 2, 3, 4}
    assert (window.start_time, window.end_time) == (dt.time(3, 0), dt.time(16, 0))


# --------------------------------------------------------------------------- valid fixtures


def test_valid_fixture_loads_with_exact_values() -> None:
    config = load_tv_alerts_config(VALID)

    assert config.model_dump() == {
        "webhook": {
            "rules_verified_at": dt.date(2026, 11, 1),
            "source_ips": TRADINGVIEW_IPS,
            "allowed_ports": (80, 443),
            "response_timeout_seconds": 3.0,
            "mtls_supported": False,
        },
        "receiver": {
            "trusted_proxies": ("127.0.0.1", "::1"),
            "client_ip_header": "X-Forwarded-For",
            "rate_limit_per_minute": 30,
            "max_body_bytes": 4096,
        },
        "heartbeat": {
            "max_silence_minutes": 75,
            "watch_windows": (
                {"days": ("mon", "tue", "wed", "thu", "fri"), "start": "03:00", "end": "16:00"},
            ),
        },
        "alerts": (
            {"name": "heartbeat MES1! 1m", "expires_on": dt.date(2027, 1, 12)},
            {"name": "ny_orb_fvg MES1! 1m", "expires_on": None},
        ),
        "expiry_reminder_days": 7,
    }


def test_valid_fixture_exposes_typed_blocks_and_parsed_values() -> None:
    config = load_tv_alerts_config(VALID)

    assert isinstance(config.webhook, WebhookRules)
    assert isinstance(config.receiver, ReceiverConfig)
    assert isinstance(config.heartbeat, HeartbeatConfig)
    assert all(isinstance(window, WatchWindow) for window in config.heartbeat.watch_windows)
    assert all(isinstance(alert, AlertExpiry) for alert in config.alerts)
    assert config.webhook.source_addresses == frozenset(
        IPv4Address(text) for text in TRADINGVIEW_IPS
    )
    assert config.receiver.trusted_networks == (
        IPv4Network("127.0.0.1/32"),
        IPv6Network("::1/128"),
    )


def test_edge_values_fixture_loads() -> None:
    config = load_tv_alerts_config(EDGE_VALUES)

    assert config.webhook.rules_verified_at == dt.date(2026, 11, 13)  # quoted in the file
    assert config.webhook.source_addresses == {IPv4Address("52.89.214.238")}
    assert config.webhook.response_timeout_seconds == 2.5
    assert config.receiver.trusted_networks == (
        IPv4Network("192.168.10.0/24"),
        IPv6Network("fd00::/120"),
        IPv4Network("10.1.2.3/32"),
        IPv4Network("172.31.255.255/32"),
    )
    assert config.receiver.client_ip_header == "x-forwarded-for"  # case-insensitive
    assert config.receiver.rate_limit_per_minute == 1
    assert config.receiver.max_body_bytes == MIN_BODY_BYTES
    assert config.heartbeat.max_silence_minutes == 1
    sunday, rest = config.heartbeat.watch_windows
    assert sunday.weekdays == {6}
    assert (sunday.start_time, sunday.end_time) == (dt.time(18, 0), dt.time(23, 59))
    assert rest.weekdays == {0, 1, 2, 3, 4, 5}
    assert (rest.start_time, rest.end_time) == (dt.time(0, 0), dt.time(17, 0))
    assert config.alerts == ()
    assert config.expiry_reminder_days == MAX_EXPIRY_REMINDER_DAYS


def test_the_code_caps_match_the_spec() -> None:
    # SPEC §5.2 (body <= 4 KB), §5.3 (<= 30 requests/minute), §5.5 (75 minutes of silence).
    # A change here needs an ADR, not a test edit.
    assert MAX_BODY_BYTES == 4096
    assert MAX_RATE_LIMIT_PER_MINUTE == 30
    assert MAX_HEARTBEAT_SILENCE_MINUTES == 75
    assert MAX_EXPIRY_REMINDER_DAYS == 30
    assert MAX_TRUSTED_PROXY_ADDRESSES == 256
    assert MAX_ALERT_NAME_LENGTH == 64


def test_the_body_floor_leaves_room_for_every_real_alert() -> None:
    # A strategy alert is larger than a heartbeat: a limit between the two would drop the
    # signals while the alarm line still looks alive. The floor must clear the largest alert.
    longest = alert_bytes(token="t" * 128, symbol="S" * 64, reason="r" * 32)

    assert len(alert_bytes()) > 256
    assert len(longest) <= MIN_BODY_BYTES // 2


# --------------------------------------------------------------------------- invalid fixtures


def test_every_must_fail_fixture_is_listed() -> None:
    assert {path.stem for path in INVALID_DIR.glob("*.yaml")} == set(MUST_FAIL)


@pytest.mark.parametrize(
    ("stem", "location", "fragment"),
    [(stem, *expected) for stem, expected in MUST_FAIL.items()],
    ids=list(MUST_FAIL),
)
def test_invalid_file_is_rejected(stem: str, location: str, fragment: str) -> None:
    path = INVALID_DIR / f"{stem}.yaml"

    with pytest.raises(ConfigError) as excinfo:
        load_tv_alerts_config(path)

    issues = excinfo.value.issues
    assert any(i.location == location and fragment in i.message for i in issues), issues
    assert all(i.source == str(path) for i in issues)


def test_all_problems_are_reported_in_one_error() -> None:
    with pytest.raises(ConfigError) as excinfo:
        load_tv_alerts_config(INVALID_DIR / "several_problems.yaml")

    assert [i.location for i in excinfo.value.issues] == [
        "webhook.source_ips.4",
        "webhook.mtls_supported",
        "receiver.rate_limit_per_minute",
        "heartbeat.watch_windows.0",
        "expiry_reminder_days",
    ]
    assert str(excinfo.value).count("\n") == 4  # one line per issue


def test_missing_file_is_reported_not_raised(tmp_path: Path) -> None:
    with pytest.raises(ConfigError) as excinfo:
        load_tv_alerts_config(tmp_path / TV_ALERTS_FILE_NAME)

    assert [i.message for i in excinfo.value.issues] == ["dosya bulunamadı"]


def test_yaml_tags_cannot_construct_python_objects(tmp_path: Path) -> None:
    path = tmp_path / TV_ALERTS_FILE_NAME
    text = VALID.read_text(encoding="utf-8").replace(
        "alerts:\n", "alerts: !!python/object/apply:os.getcwd []\nold_alerts:\n"
    )
    assert "!!python" in text
    path.write_text(text, encoding="utf-8")

    with pytest.raises(ConfigError) as excinfo:
        load_tv_alerts_config(path)

    assert "YAML okunamadı" in excinfo.value.issues[0].message


# --------------------------------------------------------------------------- generic strictness


@pytest.mark.parametrize("path", ALL_KEY_PATHS, ids=_path_id)
def test_every_key_is_required(path: KeyPath) -> None:
    assert _errors(_without(path)) == [(path, "missing")]


@pytest.mark.parametrize(
    "path", [p for p in ALL_KEY_PATHS if p not in NULLABLE_PATHS], ids=_path_id
)
def test_null_is_rejected_everywhere_but_expires_on(path: KeyPath) -> None:
    locations = [location for location, _kind in _errors(_with(path, None))]

    assert locations == [path]


@pytest.mark.parametrize(
    "path",
    [(), ("webhook",), ("receiver",), ("heartbeat",), WINDOW, ("alerts", 0)],
    ids=_path_id,
)
def test_unknown_key_is_rejected_at_every_level(path: KeyPath) -> None:
    extra = (*path, "surprise")

    assert _errors(_with(extra, 1)) == [(extra, "extra_forbidden")]


def test_models_are_immutable() -> None:
    config = load_tv_alerts_config(VALID)

    with pytest.raises(ValidationError):
        config.expiry_reminder_days = 1  # type: ignore[misc]
    with pytest.raises(ValidationError):
        config.receiver.max_body_bytes = 1  # type: ignore[misc]


@pytest.mark.parametrize(
    ("path", "value"),
    [
        (("webhook", "response_timeout_seconds"), "3"),
        (("webhook", "response_timeout_seconds"), True),
        (("webhook", "mtls_supported"), 0),
        (("webhook", "mtls_supported"), "false"),
        (("webhook", "allowed_ports", 0), "80"),
        (("webhook", "allowed_ports", 0), 80.0),
        (("webhook", "allowed_ports", 0), True),
        (("receiver", "client_ip_header"), 5),
        (("receiver", "rate_limit_per_minute"), "30"),
        (("receiver", "rate_limit_per_minute"), 30.0),
        (("receiver", "rate_limit_per_minute"), True),
        (("receiver", "max_body_bytes"), "4096"),
        (("receiver", "max_body_bytes"), 4096.0),
        (("heartbeat", "max_silence_minutes"), "75"),
        (("heartbeat", "max_silence_minutes"), 75.0),
        (("alerts", 0, "name"), 7),
        (("expiry_reminder_days",), "7"),
        (("expiry_reminder_days",), 7.0),
        (("expiry_reminder_days",), True),
    ],
)
def test_mistyped_value_is_rejected(path: KeyPath, value: object) -> None:
    locations = [location for location, _kind in _errors(_with(path, value))]

    assert locations == [path]


@pytest.mark.parametrize(
    "path",
    [
        SOURCE_IPS,
        ("webhook", "allowed_ports"),
        TRUSTED_PROXIES,
        ("heartbeat", "watch_windows"),
        (*WINDOW, "days"),
        ALERTS,
    ],
    ids=_path_id,
)
@pytest.mark.parametrize(
    "value", ["52.89.214.238", 443, {"mon": None}, {"mon"}, frozenset({"mon"}), None, True]
)
def test_list_fields_accept_only_sequences(path: KeyPath, value: object) -> None:
    assert _errors(_with(path, value)) == [(path, "not_a_list")]


def test_list_fields_reject_generators() -> None:
    lazy = (text for text in TRADINGVIEW_IPS)

    assert _errors(_with(SOURCE_IPS, lazy)) == [(SOURCE_IPS, "not_a_list")]


def test_list_fields_accept_tuples_too() -> None:
    config = TvAlertsConfig.model_validate(_with(SOURCE_IPS, TRADINGVIEW_IPS))

    assert config.webhook.source_ips == TRADINGVIEW_IPS


# --------------------------------------------------------------------------- numeric ranges


@pytest.mark.parametrize(
    ("path", "value", "accepted"),
    [
        (("webhook", "response_timeout_seconds"), 0.001, True),
        (("webhook", "response_timeout_seconds"), 2.5, True),
        (("webhook", "response_timeout_seconds"), 3, True),
        (("webhook", "response_timeout_seconds"), 0, False),
        (("webhook", "response_timeout_seconds"), -3, False),
        (("webhook", "response_timeout_seconds"), float("inf"), False),
        (("webhook", "response_timeout_seconds"), float("nan"), False),
        (("webhook", "allowed_ports", 0), 1, True),
        (("webhook", "allowed_ports", 0), 65535, True),
        (("webhook", "allowed_ports", 0), 0, False),
        (("webhook", "allowed_ports", 0), -443, False),
        (("webhook", "allowed_ports", 0), 65536, False),
        (("receiver", "rate_limit_per_minute"), 1, True),
        (("receiver", "rate_limit_per_minute"), MAX_RATE_LIMIT_PER_MINUTE, True),
        (("receiver", "rate_limit_per_minute"), 0, False),
        (("receiver", "rate_limit_per_minute"), -1, False),
        (("receiver", "rate_limit_per_minute"), MAX_RATE_LIMIT_PER_MINUTE + 1, False),
        (("receiver", "max_body_bytes"), MIN_BODY_BYTES, True),
        (("receiver", "max_body_bytes"), MAX_BODY_BYTES, True),
        (("receiver", "max_body_bytes"), MIN_BODY_BYTES - 1, False),
        (("receiver", "max_body_bytes"), 256, False),
        (("receiver", "max_body_bytes"), 0, False),
        (("receiver", "max_body_bytes"), MAX_BODY_BYTES + 1, False),
        (("receiver", "max_body_bytes"), 1_000_000, False),
        (("heartbeat", "max_silence_minutes"), 1, True),
        (("heartbeat", "max_silence_minutes"), MAX_HEARTBEAT_SILENCE_MINUTES, True),
        (("heartbeat", "max_silence_minutes"), 0, False),
        (("heartbeat", "max_silence_minutes"), MAX_HEARTBEAT_SILENCE_MINUTES + 1, False),
        (("heartbeat", "max_silence_minutes"), 1440, False),
        (("expiry_reminder_days",), 1, True),
        (("expiry_reminder_days",), MAX_EXPIRY_REMINDER_DAYS, True),
        (("expiry_reminder_days",), 0, False),
        (("expiry_reminder_days",), MAX_EXPIRY_REMINDER_DAYS + 1, False),
    ],
)
def test_numeric_ranges_and_caps(path: KeyPath, value: float, accepted: bool) -> None:
    data = _with(path, value)

    if accepted:
        TvAlertsConfig.model_validate(data)
    else:
        assert [location for location, _kind in _errors(data)] == [path]


# --------------------------------------------------------------------------- webhook


@pytest.mark.parametrize("text", [*TRADINGVIEW_IPS, "1.1.1.1", "8.8.8.8", "223.255.255.254"])
def test_public_ipv4_source_address_is_accepted(text: str) -> None:
    webhook = TvAlertsConfig.model_validate(_with(SOURCE_IPS, [text])).webhook

    assert webhook.source_ips == (text,)
    assert webhook.source_addresses == frozenset({IPv4Address(text)})


@pytest.mark.parametrize(
    "text",
    [
        "052.89.214.238",  # leading zero (octal in some parsers)
        "52.089.214.238",
        "52.89.214.0238",
        " 52.89.214.238",
        "52.89.214.238 ",
        "52.89. 214.238",
        "52.89.214.238\n",
        "52.89.214.238\x00",
        "52.89.214.238:443",
        "52.89.214.238/32",
        "52.89.214.0/24",
        "52.89.214",
        "52.89.214.238.1",
        "52.89.214.256",
        "52.89.214.-1",
        "52.89.214.+1",
        "0x34.89.214.238",
        "878302958",  # the same address as one integer
        "５２.89.214.238",  # full-width digits
        "٥٢.89.214.238",  # Arabic-Indic digits
        "tradingview.com",
        "",
        "::1",
        "2001:db8::1",
        "::ffff:52.89.214.238",
        "[52.89.214.238]",
        "52.89.214.238%eth0",
    ],
)
def test_source_address_must_be_a_plain_dotted_quad(text: str) -> None:
    assert _errors(_with(SOURCE_IPS, [text])) == [((*SOURCE_IPS, 0), "source_ip_format")]


@pytest.mark.parametrize(
    "text",
    [
        "127.0.0.1",  # loopback: would admit every local process
        "127.255.255.254",
        "10.0.0.1",  # RFC 1918
        "172.16.0.1",
        "172.31.255.254",
        "192.168.1.1",
        "169.254.169.254",  # link-local
        "100.64.0.1",  # carrier-grade NAT (also Tailscale addresses)
        "0.0.0.0",
        "255.255.255.255",
        "224.0.0.1",  # multicast
        "239.255.255.250",
        "240.0.0.1",  # reserved
        "192.0.2.1",  # documentation ranges
        "198.51.100.1",
        "203.0.113.1",
        "198.18.0.1",  # benchmarking
    ],
)
def test_source_address_must_be_global(text: str) -> None:
    assert _errors(_with(SOURCE_IPS, [*TRADINGVIEW_IPS, text])) == [
        ((*SOURCE_IPS, 4), "source_ip_not_global")
    ]


@pytest.mark.parametrize(
    "value",
    [
        878302958,
        52.89,
        None,
        True,
        b"52.89.214.238",
        IPv4Address("52.89.214.238"),
        ["52.89.214.238"],
    ],
)
def test_source_address_must_be_text(value: object) -> None:
    # `ipaddress` itself would accept an integer or packed bytes.
    assert _errors(_with(SOURCE_IPS, [value])) == [((*SOURCE_IPS, 0), "string_type")]


def test_source_addresses_must_be_unique_and_present() -> None:
    doubled = [*TRADINGVIEW_IPS, TRADINGVIEW_IPS[1]]

    assert _errors(_with(SOURCE_IPS, doubled)) == [(SOURCE_IPS, "list_duplicate")]
    assert _errors(_with(SOURCE_IPS, [])) == [(SOURCE_IPS, "list_empty")]


def test_every_bad_source_address_is_reported_with_its_index() -> None:
    mixed = ["52.89.214.238", "127.0.0.1", "34.212.75.30:443", 7]

    assert _errors(_with(SOURCE_IPS, mixed)) == [
        ((*SOURCE_IPS, 1), "source_ip_not_global"),
        ((*SOURCE_IPS, 2), "source_ip_format"),
        ((*SOURCE_IPS, 3), "string_type"),
    ]


@pytest.mark.parametrize(
    ("ports", "kind"),
    [([], "list_empty"), ([443, 443], "list_duplicate"), ([80, 443, 80], "list_duplicate")],
)
def test_allowed_ports_must_be_unique_and_present(ports: list[int], kind: str) -> None:
    path = ("webhook", "allowed_ports")

    assert _errors(_with(path, ports)) == [(path, kind)]


def test_mtls_must_stay_false() -> None:
    path = ("webhook", "mtls_supported")

    assert TvAlertsConfig.model_validate(_with(path, False)).webhook.mtls_supported is False
    assert _errors(_with(path, True)) == [(path, "mtls_not_supported")]


@pytest.mark.parametrize("value", ["2026-11-13", dt.date(2026, 11, 13)])
def test_rules_verified_at_accepts_a_plain_date(value: object) -> None:
    config = TvAlertsConfig.model_validate(_with(("webhook", "rules_verified_at"), value))

    assert config.webhook.rules_verified_at == dt.date(2026, 11, 13)


@pytest.mark.parametrize(
    "bad",
    ["2026-02-30", "13.11.2026", "2026-11-13T00:00:00", 20261113, dt.datetime(2026, 11, 13), None],
)
def test_rules_verified_at_rejects_non_dates(bad: object) -> None:
    path = ("webhook", "rules_verified_at")

    assert [location for location, _kind in _errors(_with(path, bad))] == [path]


# --------------------------------------------------------------------------- receiver


@pytest.mark.parametrize(
    ("text", "network"),
    [
        ("127.0.0.1", "127.0.0.1/32"),
        ("127.0.0.1/32", "127.0.0.1/32"),
        ("127.0.0.0/24", "127.0.0.0/24"),
        ("::1", "::1/128"),
        ("::1/128", "::1/128"),
        ("10.0.0.5", "10.0.0.5/32"),
        ("10.20.30.0/24", "10.20.30.0/24"),
        ("172.16.0.0/24", "172.16.0.0/24"),
        ("172.31.255.0/24", "172.31.255.0/24"),
        ("192.168.1.0/24", "192.168.1.0/24"),
        ("192.168.1.128/25", "192.168.1.128/25"),
        ("fd00::1", "fd00::1/128"),
        ("fd12:3456:789a::/120", "fd12:3456:789a::/120"),
    ],
)
def test_loopback_or_private_proxy_is_accepted(text: str, network: str) -> None:
    receiver = TvAlertsConfig.model_validate(_with(TRUSTED_PROXIES, [text])).receiver

    assert receiver.trusted_proxies == (text,)
    assert receiver.trusted_networks == (ipaddress.ip_network(network),)


@pytest.mark.parametrize(
    ("text", "kind"),
    [
        # anything that is not loopback / RFC 1918 / IPv6 unique-local
        ("8.8.8.8", "trusted_proxy_not_private"),
        ("52.89.214.238", "trusted_proxy_not_private"),
        ("0.0.0.0", "trusted_proxy_not_private"),
        ("0.0.0.0/0", "trusted_proxy_not_private"),
        ("::", "trusted_proxy_not_private"),
        ("::/0", "trusted_proxy_not_private"),
        ("172.32.0.1", "trusted_proxy_not_private"),
        ("172.15.255.255", "trusted_proxy_not_private"),
        ("192.169.0.1", "trusted_proxy_not_private"),
        ("169.254.1.1", "trusted_proxy_not_private"),
        ("100.64.0.1", "trusted_proxy_not_private"),
        ("fe80::1", "trusted_proxy_not_private"),
        ("2001:db8::1", "trusted_proxy_not_private"),
        ("::ffff:7f00:1", "trusted_proxy_not_private"),  # IPv4-mapped loopback
        ("126.0.0.0/7", "trusted_proxy_not_private"),  # only half of it is loopback
        # a broad range would let any host inside it forge the header
        ("127.0.0.0/8", "trusted_proxy_too_broad"),
        ("10.0.0.0/8", "trusted_proxy_too_broad"),
        ("172.16.0.0/12", "trusted_proxy_too_broad"),
        ("192.168.0.0/16", "trusted_proxy_too_broad"),
        ("192.168.0.0/23", "trusted_proxy_too_broad"),
        ("fc00::/7", "trusted_proxy_too_broad"),
        ("fd00::/119", "trusted_proxy_too_broad"),
        # host bits set
        ("192.168.1.5/24", "trusted_proxy_host_bits"),
        ("127.0.0.1/8", "trusted_proxy_host_bits"),
        ("fd00::1/120", "trusted_proxy_host_bits"),
        # one spelling only
        ("192.168.1.0/255.255.255.0", "trusted_proxy_not_canonical"),
        ("192.168.1.0/0.0.0.255", "trusted_proxy_not_canonical"),
        ("192.168.1.0/024", "trusted_proxy_not_canonical"),
        ("0:0:0:0:0:0:0:1", "trusted_proxy_not_canonical"),
        ("FD00::1", "trusted_proxy_not_canonical"),
        ("::ffff:127.0.0.1", "trusted_proxy_not_canonical"),
        # IPv6 zone ids
        ("::1%lo", "trusted_proxy_zone_id"),
        ("fe80::1%eth0", "trusted_proxy_zone_id"),
        ("fd00::%1/120", "trusted_proxy_zone_id"),
        ("127.0.0.1%lo", "trusted_proxy_zone_id"),
        # not an address at all
        ("localhost", "trusted_proxy_format"),
        ("", "trusted_proxy_format"),
        ("127.1", "trusted_proxy_format"),
        ("0127.0.0.1", "trusted_proxy_format"),
        ("127.0.0.01", "trusted_proxy_format"),
        (" 127.0.0.1", "trusted_proxy_format"),
        ("127.0.0.1 ", "trusted_proxy_format"),
        ("127.0.0.1\n", "trusted_proxy_format"),
        ("127.0.0.1:8000", "trusted_proxy_format"),
        ("[::1]", "trusted_proxy_format"),
        ("[::1]:8000", "trusted_proxy_format"),
        ("192.168.1.0/ 24", "trusted_proxy_format"),
        ("192.168.1.0/+24", "trusted_proxy_format"),
        ("192.168.1.0/24/1", "trusted_proxy_format"),
        ("192.168.1.0/33", "trusted_proxy_format"),
        ("192.168.1.0/", "trusted_proxy_format"),
        ("2130706433", "trusted_proxy_format"),
    ],
)
def test_trusted_proxy_is_rejected(text: str, kind: str) -> None:
    assert _errors(_with(TRUSTED_PROXIES, ["127.0.0.1", text])) == [((*TRUSTED_PROXIES, 1), kind)]


@pytest.mark.parametrize("value", [2130706433, None, True, b"\x7f\x00\x00\x01", ["127.0.0.1"]])
def test_trusted_proxy_must_be_text(value: object) -> None:
    assert _errors(_with(TRUSTED_PROXIES, [value])) == [((*TRUSTED_PROXIES, 0), "string_type")]


def test_trusted_proxy_size_limit_is_256_addresses() -> None:
    widest_v4, widest_v6 = "10.0.0.0/24", "fd00::/120"
    receiver = TvAlertsConfig.model_validate(
        _with(TRUSTED_PROXIES, [widest_v4, widest_v6])
    ).receiver

    assert [network.num_addresses for network in receiver.trusted_networks] == [256, 256]


@pytest.mark.parametrize(
    "proxies",
    [
        ["127.0.0.1", "127.0.0.1"],
        ["127.0.0.1", "127.0.0.1/32"],
        ["::1", "127.0.0.1", "::1/128"],
    ],
)
def test_trusted_proxies_must_be_unique_whatever_the_spelling(proxies: list[str]) -> None:
    assert _errors(_with(TRUSTED_PROXIES, proxies)) == [(TRUSTED_PROXIES, "list_duplicate")]


def test_trusted_proxies_may_not_be_empty() -> None:
    assert _errors(_with(TRUSTED_PROXIES, [])) == [(TRUSTED_PROXIES, "list_empty")]


def test_trusted_networks_answer_membership_for_both_families() -> None:
    receiver = TvAlertsConfig.model_validate(
        _with(TRUSTED_PROXIES, ["127.0.0.1", "::1", "192.168.10.0/24"])
    ).receiver

    def trusted(peer: str) -> bool:
        address = ipaddress.ip_address(peer)
        return any(address in network for network in receiver.trusted_networks)

    assert trusted("127.0.0.1")
    assert trusted("::1")
    assert trusted("192.168.10.77")
    assert not trusted("127.0.0.2")
    assert not trusted("192.168.11.1")
    assert not trusted("52.89.214.238")
    assert not trusted("::ffff:127.0.0.1")


@pytest.mark.parametrize("name", ["X-Forwarded-For", "x-forwarded-for", "X-FORWARDED-FOR"])
def test_client_ip_header_accepts_only_x_forwarded_for_in_any_case(name: str) -> None:
    data = _with(("receiver", "client_ip_header"), name)

    assert TvAlertsConfig.model_validate(data).receiver.client_ip_header == name


@pytest.mark.parametrize(
    "name",
    [
        "",
        "H" * 65,
        "X Forwarded For",
        "X-Forwarded-For:",
        "X-Forwarded-For\n",
        "X-Forwarded-For\r\nX-Evil: 1",
        " X-Forwarded-For",
        "X_Forwarded_For",
        "X-İstemci-IP",
        # Real headers of real proxies, refused on purpose: not every supported front end
        # overwrites them, so a client could forge one through the other tunnel.
        "X-Real-IP",
        "CF-Connecting-IP",
        "Fly-Client-IP",
        "Forwarded",
    ],
)
def test_client_ip_header_rejects_anything_else(name: str) -> None:
    path = ("receiver", "client_ip_header")

    assert _errors(_with(path, name)) == [(path, "client_ip_header_unsupported")]


# --------------------------------------------------------------------------- heartbeat


def test_weekday_numbers_follow_datetime() -> None:
    assert [day.value for day in Weekday] == ["mon", "tue", "wed", "thu", "fri", "sat", "sun"]
    monday = dt.date(2026, 11, 9)
    assert monday.weekday() == 0

    for offset, day in enumerate(Weekday):
        window = WatchWindow.model_validate({"days": [day.value], "start": "03:00", "end": "16:00"})
        assert window.weekdays == {(monday + dt.timedelta(days=offset)).weekday()}


def test_whole_week_and_day_order_do_not_matter() -> None:
    days = ["sun", "sat", "fri", "thu", "wed", "tue", "mon"]
    window = WatchWindow.model_validate({"days": days, "start": "00:00", "end": "23:59"})

    assert window.days == tuple(days)
    assert window.weekdays == frozenset(range(7))


@pytest.mark.parametrize(
    "day", ["monday", "Mon", "MON", "mo", "", " mon", "pzt", "0", 0, 1, True, None, ["mon"]]
)
def test_day_must_be_one_of_the_seven_names(day: object) -> None:
    path = (*WINDOW, "days")

    assert _errors(_with(path, ["tue", day])) == [((*path, 1), "enum")]


@pytest.mark.parametrize(
    ("days", "kind"),
    [
        ([], "list_empty"),
        (["mon", "mon"], "list_duplicate"),
        (["fri", "sat", "fri"], "list_duplicate"),
    ],
)
def test_days_must_be_unique_and_present(days: list[str], kind: str) -> None:
    path = (*WINDOW, "days")

    assert _errors(_with(path, days)) == [(path, kind)]


@pytest.mark.parametrize("key", ["start", "end"])
@pytest.mark.parametrize(
    "bad", ["24:00", "00:60", "7:5", "0300", "03:00:00", " 03:00", "03:00 ", "3pm", "", 0, 180, 960]
)
def test_window_time_must_be_quoted_hh_mm(key: str, bad: object) -> None:
    path = (*WINDOW, key)

    assert [location for location, _kind in _errors(_with(path, bad))] == [path]


@pytest.mark.parametrize(
    ("start", "end", "accepted"),
    [
        ("03:00", "16:00", True),
        ("00:00", "00:01", True),
        ("00:00", "23:59", True),
        ("09:59", "10:00", True),
        ("09:30", "09:30", False),
        ("16:00", "03:00", False),
        ("18:00", "02:00", False),  # no wrap past midnight: write two windows
        ("23:59", "00:00", False),
        ("10:00", "09:59", False),
    ],
)
def test_window_start_must_be_before_end(start: str, end: str, accepted: bool) -> None:
    data = _with(WINDOW, {"days": ["mon"], "start": start, "end": end})

    if accepted:
        (window,) = TvAlertsConfig.model_validate(data).heartbeat.watch_windows
        assert window.start_time == dt.time.fromisoformat(start)
        assert window.end_time == dt.time.fromisoformat(end)
        assert window.start_time < window.end_time
    else:
        assert _errors(data) == [(WINDOW, "watch_window_order")]


def test_window_times_are_naive_wall_clock_values() -> None:
    (window,) = load_tv_alerts_config(VALID).heartbeat.watch_windows

    assert window.start_time == dt.time(3, 0)
    assert window.end_time == dt.time(16, 0)
    assert window.start_time.tzinfo is None  # the monitor applies America/New_York


def test_at_least_one_watch_window_is_required() -> None:
    path = ("heartbeat", "watch_windows")

    assert _errors(_with(path, [])) == [(path, "list_empty")]


def test_several_windows_keep_their_order_and_may_overlap() -> None:
    windows = [
        {"days": ["sun"], "start": "18:00", "end": "23:59"},
        {"days": ["mon", "tue"], "start": "00:00", "end": "17:00"},
        {"days": ["mon"], "start": "09:30", "end": "16:00"},
    ]
    heartbeat = TvAlertsConfig.model_validate(_with(("heartbeat", "watch_windows"), windows))

    parsed = heartbeat.heartbeat.watch_windows
    assert [sorted(window.weekdays) for window in parsed] == [[6], [0, 1], [0]]
    assert [window.start for window in parsed] == ["18:00", "00:00", "09:30"]


def test_every_bad_window_is_reported_with_its_index() -> None:
    windows = [
        {"days": ["mon"], "start": "03:00", "end": "16:00"},
        {"days": ["funday"], "start": "03:00", "end": "16:00"},
        {"days": ["tue"], "start": "16:00", "end": "03:00"},
        "mon 03:00-16:00",
    ]
    path = ("heartbeat", "watch_windows")

    assert _errors(_with(path, windows)) == [
        ((*path, 1, "days", 0), "enum"),
        ((*path, 2), "watch_window_order"),
        ((*path, 3), "model_type"),
    ]


# --------------------------------------------------------------------------- alerts


def test_alert_list_may_be_empty() -> None:
    assert TvAlertsConfig.model_validate(_with(ALERTS, [])).alerts == ()


@pytest.mark.parametrize(
    ("value", "expected"),
    [
        (dt.date(2027, 1, 10), dt.date(2027, 1, 10)),
        ("2027-01-10", dt.date(2027, 1, 10)),
        (dt.date(2020, 1, 1), dt.date(2020, 1, 1)),  # already expired: the reminder says so
        (None, None),
    ],
)
def test_alert_expiry_is_a_plain_date_or_null(value: object, expected: dt.date | None) -> None:
    data = _with(ALERTS, [{"name": "heartbeat", "expires_on": value}])

    assert TvAlertsConfig.model_validate(data).alerts == (
        AlertExpiry(name="heartbeat", expires_on=expected),
    )


@pytest.mark.parametrize(
    "bad", ["2027-02-30", "10.01.2027", "never", "", 20270110, dt.datetime(2027, 1, 10, 12), True]
)
def test_alert_expiry_rejects_non_dates(bad: object) -> None:
    path = ("alerts", 0, "expires_on")

    assert [location for location, _kind in _errors(_with(path, bad))] == [path]


@pytest.mark.parametrize(
    "name",
    ["heartbeat MES1! 1m", "x", "NY ORB — CME_MINI:MES1! (1 dk)", "Çıkış alarmı", "n" * 64],
)
def test_alert_name_accepts_a_short_single_line(name: str) -> None:
    data = _with(("alerts", 0, "name"), name)

    assert TvAlertsConfig.model_validate(data).alerts[0].name == name


@pytest.mark.parametrize(
    ("name", "kind"),
    [
        ("", "alert_name_empty"),
        (" ", "alert_name_characters"),
        (" heartbeat", "alert_name_characters"),
        ("heartbeat ", "alert_name_characters"),
        ("heart\nbeat", "alert_name_characters"),
        ("heart\tbeat", "alert_name_characters"),
        ("heartbeat\x00", "alert_name_characters"),
        ("heart‮beat", "alert_name_characters"),  # right-to-left override
        ("n" * 65, "alert_name_too_long"),
    ],
)
def test_alert_name_is_rejected(name: str, kind: str) -> None:
    path = ("alerts", 0, "name")

    assert _errors(_with(path, name)) == [(path, kind)]


def test_alert_names_must_be_unique() -> None:
    alerts = [
        {"name": "heartbeat", "expires_on": None},
        {"name": "master", "expires_on": dt.date(2027, 1, 10)},
        {"name": "heartbeat", "expires_on": dt.date(2027, 1, 10)},
    ]

    assert _errors(_with(ALERTS, alerts)) == [(ALERTS, "list_duplicate")]


def test_alert_names_differing_only_in_case_are_distinct() -> None:
    alerts = [{"name": "Heartbeat", "expires_on": None}, {"name": "heartbeat", "expires_on": None}]

    assert len(TvAlertsConfig.model_validate(_with(ALERTS, alerts)).alerts) == 2


# --------------------------------------------------------------------------- fixtures stay honest


def test_fixture_helpers_do_not_mutate_the_valid_fixture() -> None:
    before = copy.deepcopy(_valid_data())
    _with(SOURCE_IPS, [])
    _without(("alerts",))

    assert _valid_data() == before


@pytest.mark.parametrize("path", sorted(INVALID_DIR.glob("*.yaml")), ids=lambda p: p.stem)
def test_invalid_fixture_says_why_it_must_fail(path: Path) -> None:
    assert path.read_text(encoding="utf-8").startswith("# MUST FAIL: ")
