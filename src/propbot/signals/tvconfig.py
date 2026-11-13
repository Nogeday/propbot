"""TradingView alert-line config: schema and loader for `config/tv_alerts.yaml`.

The file carries what TradingView publishes about its webhooks (source addresses, ports,
response deadline), the receiver's own limits, the heartbeat watch windows and the expiry
dates of the user's alerts. Same fail-closed rules as the risk config (ADR-0003): strict
types, unknown / missing / duplicate key = load error, every problem reported at once in
Turkish. The caps below hold no matter what the YAML says: a value above a cap is a load
error, never silently clamped.
"""

import datetime as dt
import ipaddress
from collections.abc import Callable, Hashable
from enum import StrEnum
from ipaddress import IPv4Address, IPv4Network, IPv6Network
from pathlib import Path
from typing import Annotated, Final, Self

from pydantic import AfterValidator, BeforeValidator, Field, ValidationError, model_validator
from pydantic_core import PydanticCustomError

from propbot.risk.config import StrictModel
from propbot.risk.errors import ConfigError, issues_from_validation_error
from propbot.risk.loader import read_yaml_mapping

# `ResetTime` is the generic quoted-"HH:MM" validator; its messages do not mention the reset.
from propbot.risk.venue import PlainDate, ResetTime

TV_ALERTS_FILE_NAME: Final = "tv_alerts.yaml"

# SPEC §5.3: at most 30 webhook requests per minute.
MAX_RATE_LIMIT_PER_MINUTE: Final = 30
# SPEC §5.2: request body at most 4 KB. The floor keeps every schema-valid alert deliverable:
# strategy alerts are about 300-500 bytes while a heartbeat is under 200, so a smaller limit
# would drop the signals and still look alive to the silence alarm.
MIN_BODY_BYTES: Final = 1024
MAX_BODY_BYTES: Final = 4096
# SPEC §5.5: 75 minutes of silence inside a watch window raises the alarm.
MAX_HEARTBEAT_SILENCE_MINUTES: Final = 75
MAX_EXPIRY_REMINDER_DAYS: Final = 30
# A broad trusted range would let any host inside it forge the client-IP header.
MAX_TRUSTED_PROXY_ADDRESSES: Final = 256
# The one client-IP header every supported front end writes itself: Caddy and tailscaled
# overwrite it, Cloudflare appends the connecting address, so its right-most entry is always
# the proxy's own observation. Any other header could pass through one of them untouched.
SUPPORTED_CLIENT_IP_HEADER: Final = "X-Forwarded-For"
# Alert names are printed in Telegram reminders: one short line each.
MAX_ALERT_NAME_LENGTH: Final = 64

type IPNetwork = IPv4Network | IPv6Network

_TRUSTED_PROXY_RANGES_V4: Final = (
    IPv4Network("127.0.0.0/8"),
    IPv4Network("10.0.0.0/8"),
    IPv4Network("172.16.0.0/12"),
    IPv4Network("192.168.0.0/16"),
)
_TRUSTED_PROXY_RANGES_V6: Final = (IPv6Network("::1/128"), IPv6Network("fc00::/7"))
_TRUSTED_PROXY_RANGES_TEXT: Final = ", ".join(
    str(network) for network in (*_TRUSTED_PROXY_RANGES_V4, *_TRUSTED_PROXY_RANGES_V6)
)


class Weekday(StrEnum):
    """Day names used in `watch_windows.days`; definition order is Monday first."""

    MON = "mon"
    TUE = "tue"
    WED = "wed"
    THU = "thu"
    FRI = "fri"
    SAT = "sat"
    SUN = "sun"


# Same numbering as `datetime.date.weekday()`: Monday = 0.
_WEEKDAY_INDEX: Final = {day: index for index, day in enumerate(Weekday)}


def _list_to_tuple(value: object) -> object:
    # A YAML sequence is a list. Lax tuple validation would also take sets and generators.
    if not isinstance(value, list | tuple):
        raise PydanticCustomError(
            "not_a_list", "liste olmalı; okunan: {value}", {"value": repr(value)}
        )
    return tuple(value)


def _non_empty[T](items: tuple[T, ...]) -> tuple[T, ...]:
    if not items:
        raise PydanticCustomError("list_empty", "en az bir öğe içermeli")
    return items


def _unique_by[T](key: Callable[[T], Hashable]) -> Callable[[tuple[T, ...]], tuple[T, ...]]:
    """Build a validator that rejects two items with the same `key`."""

    def check(items: tuple[T, ...]) -> tuple[T, ...]:
        seen: set[Hashable] = set()
        repeated: list[str] = []
        for item in items:
            marker = key(item)
            if marker in seen and str(marker) not in repeated:
                repeated.append(str(marker))
            seen.add(marker)
        if repeated:
            raise PydanticCustomError(
                "list_duplicate", "yinelenen öğe: {values}", {"values": ", ".join(repeated)}
            )
        return items

    return check


def _identity[T](item: T) -> T:
    return item


def _source_ip_format_error(value: str) -> PydanticCustomError:
    return PydanticCustomError(
        "source_ip_format",
        "noktalı dörtlü tek bir IPv4 adresi olmalı (port, CIDR, boşluk ya da baştaki sıfır"
        " olmadan); okunan: {value}",
        {"value": repr(value)},
    )


def _check_source_ip(value: str) -> str:
    try:
        address = IPv4Address(value)
    except ValueError as exc:
        raise _source_ip_format_error(value) from exc
    # `IPv4Address` refuses leading zeros, whitespace, ports and prefixes (the tests pin that);
    # the round trip keeps the rule even if a later Python version became more lenient.
    if str(address) != value:
        raise _source_ip_format_error(value)
    # `is_global` alone is still true for multicast addresses.
    if not address.is_global or address.is_multicast or address.is_reserved:
        raise PydanticCustomError(
            "source_ip_not_global",
            "internetten gelen (global) bir adres olmalı; özel, loopback, link-local, multicast"
            " ve ayrılmış adresler kaynak IP olarak yazılamaz; okunan: {value}",
            {"value": repr(value)},
        )
    return value


def _is_loopback_or_private(network: IPNetwork) -> bool:
    if isinstance(network, IPv4Network):
        return any(network.subnet_of(allowed) for allowed in _TRUSTED_PROXY_RANGES_V4)
    return any(network.subnet_of(allowed) for allowed in _TRUSTED_PROXY_RANGES_V6)


def _parse_trusted_proxy(value: str) -> IPNetwork:
    if "%" in value:
        # `ipaddress` accepts scoped IPv6 literals ("::1%eth0"); a peer address never has one.
        raise PydanticCustomError(
            "trusted_proxy_zone_id",
            "IPv6 bölge kimliği (%...) kullanılamaz; okunan: {value}",
            {"value": repr(value)},
        )
    try:
        interface = ipaddress.ip_interface(value)
    except ValueError as exc:
        raise PydanticCustomError(
            "trusted_proxy_format",
            "IP adresi ya da CIDR ağı olmalı (ör. 127.0.0.1, ::1, 192.168.1.0/24); okunan: {value}",
            {"value": repr(value)},
        ) from exc
    network = interface.network
    if interface.ip != network.network_address:
        raise PydanticCustomError(
            "trusted_proxy_host_bits",
            "CIDR ağ adresiyle yazılmalı (host bitleri sıfır olmalı): {expected}; okunan: {value}",
            {"expected": str(network), "value": repr(value)},
        )
    # Netmask / hostmask suffixes, "/024" and expanded IPv6 spellings parse too; one spelling only.
    canonical = str(network) if "/" in value else str(network.network_address)
    if value != canonical:
        raise PydanticCustomError(
            "trusted_proxy_not_canonical",
            "kanonik biçimde yazılmalı: {expected}; okunan: {value}",
            {"expected": canonical, "value": repr(value)},
        )
    if not _is_loopback_or_private(network):
        raise PydanticCustomError(
            "trusted_proxy_not_private",
            "yalnızca loopback ya da özel ağ adresi olabilir ({allowed}); okunan: {value}",
            {"allowed": _TRUSTED_PROXY_RANGES_TEXT, "value": repr(value)},
        )
    if network.num_addresses > MAX_TRUSTED_PROXY_ADDRESSES:
        raise PydanticCustomError(
            "trusted_proxy_too_broad",
            "en fazla {limit} adres kapsayabilir (geniş bir güvenilen aralık, istemci IP"
            " başlığının taklit edilmesine izin verir); okunan: {value}",
            {"limit": MAX_TRUSTED_PROXY_ADDRESSES, "value": repr(value)},
        )
    return network


def _check_trusted_proxy(value: str) -> str:
    _parse_trusted_proxy(value)
    return value


def _check_mtls(value: bool) -> bool:
    if value:
        raise PydanticCustomError(
            "mtls_not_supported",
            "false olmalı: TradingView istemci sertifikası sunmuyor (mTLS yok);"
            " güvenlik kaynak IP + token ile sağlanır",
        )
    return value


def _check_alert_name(value: str) -> str:
    if not value:
        raise PydanticCustomError("alert_name_empty", "boş olamaz")
    if value != value.strip() or not value.isprintable():
        raise PydanticCustomError(
            "alert_name_characters",
            "başında/sonunda boşluk, satır sonu ya da kontrol karakteri olamaz; okunan: {value}",
            {"value": repr(value)},
        )
    if len(value) > MAX_ALERT_NAME_LENGTH:
        raise PydanticCustomError(
            "alert_name_too_long",
            "en fazla {limit} karakter olabilir; okunan: {length} karakter",
            {"limit": MAX_ALERT_NAME_LENGTH, "length": len(value)},
        )
    return value


SourceIp = Annotated[str, AfterValidator(_check_source_ip)]
TrustedProxy = Annotated[str, AfterValidator(_check_trusted_proxy)]
Port = Annotated[int, Field(ge=1, le=65535)]
AlertName = Annotated[str, AfterValidator(_check_alert_name)]


class WebhookRules(StrictModel):
    """What TradingView publishes about webhook delivery; re-checked on every deployment."""

    rules_verified_at: PlainDate
    source_ips: Annotated[
        tuple[SourceIp, ...],
        BeforeValidator(_list_to_tuple),
        AfterValidator(_non_empty),
        AfterValidator(_unique_by(_identity)),
    ]
    # Informational (deployment check): TradingView delivers only to these ports.
    allowed_ports: Annotated[
        tuple[Port, ...],
        BeforeValidator(_list_to_tuple),
        AfterValidator(_non_empty),
        AfterValidator(_unique_by(_identity)),
    ]
    response_timeout_seconds: float = Field(gt=0)
    mtls_supported: Annotated[bool, AfterValidator(_check_mtls)]

    @property
    def source_addresses(self) -> frozenset[IPv4Address]:
        return frozenset(IPv4Address(text) for text in self.source_ips)


def _check_client_ip_header(value: str) -> str:
    if value.lower() != SUPPORTED_CLIENT_IP_HEADER.lower():
        raise PydanticCustomError(
            "client_ip_header_unsupported",
            "yalnızca {expected} desteklenir: desteklenen her vekil (Caddy, Tailscale Funnel,"
            " Cloudflare tüneli) bu başlığı kendisi yazar; başka bir başlık vekilden olduğu gibi"
            " geçip istemci tarafından taklit edilebilir; okunan: {value}",
            {"expected": SUPPORTED_CLIENT_IP_HEADER, "value": repr(value)},
        )
    return value


class ReceiverConfig(StrictModel):
    """Limits and proxy trust of our own webhook receiver."""

    # TCP peers allowed to supply the client-IP header (the local TLS proxy).
    trusted_proxies: Annotated[
        tuple[TrustedProxy, ...],
        BeforeValidator(_list_to_tuple),
        AfterValidator(_non_empty),
        AfterValidator(_unique_by(_parse_trusted_proxy)),
    ]
    client_ip_header: Annotated[str, AfterValidator(_check_client_ip_header)]
    rate_limit_per_minute: int = Field(ge=1, le=MAX_RATE_LIMIT_PER_MINUTE)
    max_body_bytes: int = Field(ge=MIN_BODY_BYTES, le=MAX_BODY_BYTES)

    @property
    def trusted_networks(self) -> tuple[IPNetwork, ...]:
        return tuple(_parse_trusted_proxy(text) for text in self.trusted_proxies)


class WatchWindow(StrictModel):
    """One span of `America/New_York` wall time during which heartbeat silence raises an alarm."""

    days: Annotated[
        tuple[Annotated[Weekday, Field(strict=False)], ...],
        BeforeValidator(_list_to_tuple),
        AfterValidator(_non_empty),
        AfterValidator(_unique_by(_identity)),
    ]
    start: ResetTime
    end: ResetTime

    @model_validator(mode="after")
    def _start_before_end(self) -> Self:
        if not self.start_time < self.end_time:
            raise PydanticCustomError(
                "watch_window_order",
                "start ({start}) end değerinden ({end}) önce olmalı (pencere gece yarısını"
                " aşamaz; gerekiyorsa iki pencere yaz)",
                {"start": self.start, "end": self.end},
            )
        return self

    @property
    def weekdays(self) -> frozenset[int]:
        """Weekday numbers as `datetime.date.weekday()` gives them (0 = Monday)."""
        return frozenset(_WEEKDAY_INDEX[day] for day in self.days)

    @property
    def start_time(self) -> dt.time:
        return dt.time.fromisoformat(self.start)

    @property
    def end_time(self) -> dt.time:
        return dt.time.fromisoformat(self.end)


class HeartbeatConfig(StrictModel):
    max_silence_minutes: int = Field(ge=1, le=MAX_HEARTBEAT_SILENCE_MINUTES)
    watch_windows: Annotated[
        tuple[WatchWindow, ...], BeforeValidator(_list_to_tuple), AfterValidator(_non_empty)
    ]


class AlertExpiry(StrictModel):
    """A TradingView alert and the day it expires (null = no expiry on the user's plan)."""

    name: AlertName
    expires_on: PlainDate | None


def _alert_name(alert: AlertExpiry) -> str:
    return alert.name


class TvAlertsConfig(StrictModel):
    """Every key is required: defaults live in `config/tv_alerts.yaml`, not in code."""

    webhook: WebhookRules
    receiver: ReceiverConfig
    heartbeat: HeartbeatConfig
    alerts: Annotated[
        tuple[AlertExpiry, ...],
        BeforeValidator(_list_to_tuple),
        AfterValidator(_unique_by(_alert_name)),
    ]
    expiry_reminder_days: int = Field(ge=1, le=MAX_EXPIRY_REMINDER_DAYS)


def load_tv_alerts_config(path: Path) -> TvAlertsConfig:
    """Read and validate the file; any problem raises `ConfigError` with every issue found."""
    data = read_yaml_mapping(path)
    try:
        return TvAlertsConfig.model_validate(data)
    except ValidationError as exc:
        raise ConfigError(issues_from_validation_error(exc, str(path))) from exc
