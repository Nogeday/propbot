"""Webhook receiver: every validation step of SPEC §5.2 and its error path, in order."""

import datetime as dt
import json
from collections.abc import Iterator
from ipaddress import IPv4Address, IPv6Address

import pytest
import structlog
from fastapi.testclient import TestClient
from httpx2 import Response
from structlog.testing import capture_logs

from propbot.signals.receiver import (
    HEALTH_PATH,
    WEBHOOK_PATH,
    ReceiverDeps,
    RejectCode,
    RejectStats,
    SlidingWindowLimiter,
    create_app,
    parse_ip_strict,
    resolve_client_ip,
)
from propbot.signals.tvconfig import TvAlertsConfig
from propbot.store.db import OutboxKind, SignalStatus, Store, StoreError
from support import alerts
from support.alerts import BAR_TIME_MS, TOKEN

TV_IP = "52.89.214.238"
OTHER_TV_IPS = ("34.212.75.30", "54.218.53.128", "52.32.178.7")
STRANGER = "203.0.113.9"
LOOPBACK = "127.0.0.1"
VALIDITY_SECONDS = 60
BAR_CLOSE_MS = BAR_TIME_MS + 60_000
# 1.5 s after the one-minute bar closed: a fresh alert.
NOW_MS = BAR_CLOSE_MS + 1_500
HOUR_MS = 60 * 60_000


def tv_config(**receiver_overrides: object) -> TvAlertsConfig:
    receiver: dict[str, object] = {
        "trusted_proxies": ["127.0.0.1", "::1"],
        "client_ip_header": "X-Forwarded-For",
        "rate_limit_per_minute": 30,
        "max_body_bytes": 4096,
    }
    receiver.update(receiver_overrides)
    return TvAlertsConfig.model_validate(
        {
            "webhook": {
                "rules_verified_at": dt.date(2026, 11, 13),
                "source_ips": [TV_IP, *OTHER_TV_IPS],
                "allowed_ports": [80, 443],
                "response_timeout_seconds": 3,
                "mtls_supported": False,
            },
            "receiver": receiver,
            "heartbeat": {
                "max_silence_minutes": 75,
                "watch_windows": [{"days": ["mon"], "start": "03:00", "end": "16:00"}],
            },
            "alerts": [],
            "expiry_reminder_days": 7,
        }
    )


class FakeClock:
    """Returns `now_ms` and then advances by `step_ms` (0 keeps time frozen)."""

    def __init__(self, now_ms: int = NOW_MS) -> None:
        self.now_ms = now_ms
        self.step_ms = 0

    def __call__(self) -> int:
        value = self.now_ms
        self.now_ms += self.step_ms
        return value


class Harness:
    """A receiver app on an in-memory store, with the callbacks recorded."""

    def __init__(self, tv: TvAlertsConfig | None = None, clock: FakeClock | None = None) -> None:
        self.store = Store(":memory:")
        self.clock = clock or FakeClock()
        self.stats = RejectStats()
        self.tv = tv or tv_config()
        self.queued: list[int] = []
        self.messages: list[int] = []
        self.deps = ReceiverDeps(
            store=self.store,
            webhook_token=TOKEN,
            tv=self.tv,
            validity_seconds=VALIDITY_SECONDS,
            clock=self.clock,
            on_queued=lambda: self.queued.append(1),
            on_messages=lambda: self.messages.append(1),
            stats=self.stats,
        )
        self.app = create_app(self.deps)

    def request(
        self,
        method: str,
        path: str,
        *,
        peer: str = TV_IP,
        headers: dict[str, str] | None = None,
        content: bytes | Iterator[bytes] | None = None,
    ) -> Response:
        with TestClient(self.app, client=(peer, 50000)) as client:
            return client.request(method, path, headers=headers, content=content)

    def post(
        self,
        content: bytes | Iterator[bytes] | None = None,
        *,
        peer: str = TV_IP,
        headers: dict[str, str] | None = None,
    ) -> Response:
        body = alerts.alert_bytes() if content is None else content
        return self.request("POST", WEBHOOK_PATH, peer=peer, headers=headers, content=body)

    def via_proxy(self, forwarded_for: str, content: bytes | None = None) -> Response:
        return self.post(content, peer=LOOPBACK, headers={"X-Forwarded-For": forwarded_for})

    def rejected(self, code: RejectCode) -> int:
        return self.stats.snapshot()[code.value]

    def signal_count(self) -> int:
        with self.store.transaction() as tx:
            return len(tx.signals_received_between(0, 2**62))

    def outbox_total(self) -> int:
        with self.store.transaction() as tx:
            return sum(tx.outbox_counts().values())

    def drain_outbox(self) -> list[str]:
        """Texts of every pending outbox row, oldest first (rows are marked sent on the way)."""
        texts: list[str] = []
        with self.store.transaction() as tx:
            while (message := tx.next_pending_message()) is not None:
                assert message.kind is OutboxKind.ALARM
                texts.append(message.text)
                tx.mark_message_sent(message.id, self.clock.now_ms)
        return texts


@pytest.fixture
def make() -> Iterator[type[Harness]]:
    """Harness factory; every store it hands out is closed afterwards."""
    created: list[Harness] = []

    class Tracked(Harness):
        def __init__(
            self, tv: TvAlertsConfig | None = None, clock: FakeClock | None = None
        ) -> None:
            super().__init__(tv, clock)
            created.append(self)

    structlog.reset_defaults()
    yield Tracked
    for harness in created:
        harness.store.close()


def body_of(**overrides: object) -> bytes:
    return alerts.alert_bytes(**overrides)


def padded(size: int) -> bytes:
    """A valid alert padded with spaces to exactly `size` bytes."""
    body = body_of()
    return body + b" " * (size - len(body))


def without_token() -> bytes:
    payload = alerts.alert_payload()
    del payload["token"]
    return json.dumps(payload).encode()


# --- client IP resolution -----------------------------------------------------------------


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        ("52.89.214.238", IPv4Address("52.89.214.238")),
        ("::1", IPv6Address("::1")),
        ("::ffff:52.89.214.238", IPv4Address("52.89.214.238")),
        ("2001:db8::1", IPv6Address("2001:db8::1")),
    ],
)
def test_parse_ip_accepts_plain_literals(text: str, expected: object) -> None:
    assert parse_ip_strict(text) == expected


@pytest.mark.parametrize(
    "text",
    [
        "",
        " 52.89.214.238",
        "52.89.214.238 ",
        "52.89.214.238:443",
        "[::1]",
        "[::1]:443",
        "fe80::1%eth0",
        "052.089.214.238",
        "52.89.214",
        "52.89.214.238.1",
        "52.89.214.256",
        "localhost",
        "testclient",
        "0x34.89.214.238",
        "52.89.214.238\n",
        "unknown",
    ],
)
def test_parse_ip_refuses_everything_else(text: str) -> None:
    assert parse_ip_strict(text) is None


def test_untrusted_peer_is_the_client_and_its_header_is_ignored() -> None:
    receiver = tv_config().receiver

    assert resolve_client_ip(STRANGER, [TV_IP], receiver) == IPv4Address(STRANGER)
    assert resolve_client_ip(TV_IP, [], receiver) == IPv4Address(TV_IP)
    assert resolve_client_ip(TV_IP, ["garbage"], receiver) == IPv4Address(TV_IP)


@pytest.mark.parametrize(
    ("header_values", "expected"),
    [
        ([TV_IP], TV_IP),
        ([f"{STRANGER}, {TV_IP}"], TV_IP),  # the proxy appended the peer it saw
        ([f"{TV_IP}, {STRANGER}"], STRANGER),  # a forged first entry does not count
        ([f"{TV_IP},{STRANGER}"], STRANGER),
        ([STRANGER, TV_IP], TV_IP),  # repeated header lines are one list
        ([f"\t{TV_IP} "], TV_IP),
        (["::ffff:52.89.214.238"], TV_IP),
    ],
)
def test_behind_a_trusted_proxy_the_right_most_entry_is_the_client(
    header_values: list[str], expected: str
) -> None:
    receiver = tv_config().receiver

    for proxy in ("127.0.0.1", "::1", "::ffff:127.0.0.1"):
        assert resolve_client_ip(proxy, header_values, receiver) == IPv4Address(expected)


@pytest.mark.parametrize(
    "header_values",
    [
        [],
        [""],
        [f"{TV_IP},"],
        [f"{TV_IP}, "],
        [f"{TV_IP}:443"],
        ["unknown"],
        [f"for={TV_IP}"],
        [f"{TV_IP} {TV_IP}"],
    ],
)
def test_behind_a_trusted_proxy_a_missing_or_broken_header_resolves_to_nothing(
    header_values: list[str],
) -> None:
    assert resolve_client_ip(LOOPBACK, header_values, tv_config().receiver) is None


@pytest.mark.parametrize("peer", [None, "", "testclient", "52.89.214.238:1234"])
def test_an_unparsable_peer_resolves_to_nothing(peer: str | None) -> None:
    assert resolve_client_ip(peer, [TV_IP], tv_config().receiver) is None


def test_a_private_proxy_range_is_trusted_when_configured() -> None:
    receiver = tv_config(trusted_proxies=["10.0.0.0/24"]).receiver

    assert resolve_client_ip("10.0.0.7", [TV_IP], receiver) == IPv4Address(TV_IP)
    # Loopback is no longer a trusted proxy: it is just a peer, and not an allowed one.
    assert resolve_client_ip(LOOPBACK, [TV_IP], receiver) == IPv4Address(LOOPBACK)


# --- 1. source IP -------------------------------------------------------------------------


@pytest.mark.parametrize("peer", [TV_IP, *OTHER_TV_IPS])
def test_each_tradingview_address_is_admitted_directly(make: type[Harness], peer: str) -> None:
    harness = make()

    response = harness.post(peer=peer)

    assert response.status_code == 200
    assert response.json() == {"status": "queued"}


def test_a_stranger_is_refused_before_anything_is_read(make: type[Harness]) -> None:
    harness = make()

    response = harness.post(peer=STRANGER)

    assert response.status_code == 403
    assert response.json() == {"error": "forbidden"}
    assert harness.rejected(RejectCode.IP_NOT_ALLOWED) == 1
    assert harness.signal_count() == 0
    assert harness.queued == []


def test_a_forged_header_from_a_stranger_does_not_help(make: type[Harness]) -> None:
    harness = make()

    response = harness.post(peer=STRANGER, headers={"X-Forwarded-For": TV_IP})

    assert response.status_code == 403
    assert harness.signal_count() == 0


def test_through_the_local_proxy_the_forwarded_address_decides(make: type[Harness]) -> None:
    harness = make()

    assert harness.via_proxy(TV_IP).status_code == 200
    assert harness.via_proxy(STRANGER, body_of(action="exit")).status_code == 403
    assert harness.via_proxy(f"{TV_IP}, {STRANGER}", body_of(action="exit")).status_code == 403
    assert harness.via_proxy(f"{STRANGER}, {TV_IP}", body_of(action="exit")).status_code == 200


def test_through_the_local_proxy_a_missing_header_is_refused(make: type[Harness]) -> None:
    harness = make()

    response = harness.post(peer=LOOPBACK)

    assert response.status_code == 403
    assert harness.rejected(RejectCode.IP_NOT_ALLOWED) == 1


def test_headers_other_than_x_forwarded_for_are_ignored(make: type[Harness]) -> None:
    harness = make()

    response = harness.post(peer=LOOPBACK, headers={"CF-Connecting-IP": TV_IP, "X-Real-IP": TV_IP})

    assert response.status_code == 403


def test_a_peer_that_is_not_an_address_is_refused(make: type[Harness]) -> None:
    harness = make()

    assert harness.post(peer="testclient").status_code == 403


# --- 2. body size -------------------------------------------------------------------------


def test_a_body_of_exactly_the_limit_is_read(make: type[Harness]) -> None:
    harness = make()

    assert harness.post(padded(4096)).status_code == 200


def test_a_declared_length_above_the_limit_is_refused(make: type[Harness]) -> None:
    harness = make()

    response = harness.post(padded(4097))

    assert response.status_code == 413
    assert response.json() == {"error": "too_large"}
    assert harness.rejected(RejectCode.BODY_TOO_LARGE) == 1
    assert harness.signal_count() == 0


def test_a_chunked_body_above_the_limit_is_cut_off(make: type[Harness]) -> None:
    harness = make()

    def chunks() -> Iterator[bytes]:
        yield body_of()
        for _ in range(8):
            yield b" " * 1024

    response = harness.post(chunks())

    assert response.status_code == 413
    assert harness.signal_count() == 0


def test_a_chunked_body_within_the_limit_is_accepted(make: type[Harness]) -> None:
    harness = make()
    body = body_of()

    response = harness.post(iter([body[:40], body[40:]]))

    assert response.status_code == 200


def test_the_body_limit_follows_the_config(make: type[Harness]) -> None:
    harness = make(tv_config(max_body_bytes=1024))

    assert harness.post(padded(1024)).status_code == 200
    assert harness.post(padded(1025)).status_code == 413


# --- 3. JSON ------------------------------------------------------------------------------


@pytest.mark.parametrize(
    "body",
    [
        b"",
        b"not json",
        b"[]",
        b'"text"',
        b"null",
        b'{"v":1,"v":1}',
        b'{"rr":NaN}',
        b'{"rr":1e400}',
        b"\xff\xfe",
        b'\xef\xbb\xbf{"v":1}',
        b'{"v":1',
    ],
)
def test_a_body_that_is_not_one_json_object_is_refused(make: type[Harness], body: bytes) -> None:
    harness = make()

    response = harness.post(body)

    assert response.status_code == 400
    assert response.json() == {"error": "bad_json"}
    assert harness.rejected(RejectCode.BAD_JSON) == 1
    assert harness.signal_count() == 0


@pytest.mark.parametrize(
    "content_type", ["application/json", "text/plain", "text/plain; charset=utf-8"]
)
def test_the_content_type_does_not_matter(make: type[Harness], content_type: str) -> None:
    harness = make()

    response = harness.post(headers={"Content-Type": content_type})

    assert response.status_code == 200


# --- 4. token -----------------------------------------------------------------------------


@pytest.mark.parametrize(
    "body",
    [
        without_token(),
        body_of(token=""),
        body_of(token=TOKEN[:-1]),
        body_of(token=TOKEN + "x"),
        body_of(token=TOKEN.swapcase()),
        body_of(token=None),
        body_of(token=12345),
        body_of(token=[TOKEN]),
        body_of(token="ş" * 40),
    ],
)
def test_a_missing_or_wrong_token_is_refused(make: type[Harness], body: bytes) -> None:
    harness = make()

    response = harness.post(body)

    assert response.status_code == 401
    assert response.json() == {"error": "unauthorized"}
    assert harness.rejected(RejectCode.BAD_TOKEN) == 1
    assert harness.signal_count() == 0
    assert harness.queued == []


def test_the_token_is_checked_before_the_schema_and_the_rate_limit(make: type[Harness]) -> None:
    harness = make(tv_config(rate_limit_per_minute=1))
    harness.post()  # uses up the rate limit

    response = harness.post(body_of(token="wrong", strategy_id="nonsense", tf="1D"))

    assert response.status_code == 401
    assert harness.rejected(RejectCode.BAD_SCHEMA) == 0
    assert harness.rejected(RejectCode.RATE_LIMITED) == 0
    assert all("Geçersiz alert gövdesi" not in text for text in harness.drain_outbox())


def test_an_app_without_a_token_cannot_be_built(make: type[Harness]) -> None:
    harness = make()

    with pytest.raises(ValueError, match="webhook_token"):
        create_app(
            ReceiverDeps(
                store=harness.store,
                webhook_token="",
                tv=harness.tv,
                validity_seconds=VALIDITY_SECONDS,
                clock=harness.clock,
                on_queued=lambda: None,
                on_messages=lambda: None,
                stats=harness.stats,
            )
        )


# --- 5. rate limit (token-valid requests only) --------------------------------------------


def test_limiter_slides_with_the_clock() -> None:
    limiter = SlidingWindowLimiter(limit=2, window_ms=1000)

    assert limiter.allow(0)
    assert limiter.allow(999)
    assert not limiter.allow(999)
    assert limiter.allow(1000)  # the call at 0 left the window
    assert not limiter.allow(1500)
    assert limiter.allow(1999)  # the call at 999 left the window


def test_limiter_does_not_count_refused_calls() -> None:
    limiter = SlidingWindowLimiter(limit=1, window_ms=1000)

    assert limiter.allow(0)
    assert not any(limiter.allow(now) for now in range(1, 1000, 100))
    assert limiter.allow(1000)


def test_limiter_stays_closed_when_the_clock_steps_back() -> None:
    limiter = SlidingWindowLimiter(limit=1, window_ms=1000)

    assert limiter.allow(5000)
    assert not limiter.allow(1000)


@pytest.mark.parametrize(("limit", "window_ms"), [(0, 1000), (-1, 1000), (1, 0)])
def test_limiter_refuses_nonsense_settings(limit: int, window_ms: int) -> None:
    with pytest.raises(ValueError, match="positive"):
        SlidingWindowLimiter(limit=limit, window_ms=window_ms)


def test_requests_above_the_limit_get_429_until_the_window_moves(make: type[Harness]) -> None:
    harness = make(tv_config(rate_limit_per_minute=2))

    first = harness.post(body_of(action="entry"))
    second = harness.post(body_of(action="stop_update"))
    third = harness.post(body_of(action="exit"))
    fourth = harness.post(body_of(action="cancel"))
    harness.clock.now_ms += 60_000
    later = harness.post(body_of(action="exit"))

    assert [first.status_code, second.status_code] == [200, 200]
    assert third.status_code == 429
    assert third.json() == {"error": "rate_limited"}
    assert fourth.status_code == 429
    assert harness.rejected(RejectCode.RATE_LIMITED) == 2
    assert later.status_code == 200
    assert harness.signal_count() == 3
    # Our own alerts are looping: the user is told, once per throttle interval.
    alarms = harness.drain_outbox()
    assert len(alarms) == 1
    assert "dakikada 2 istekten fazlasını" in alarms[0]
    assert harness.messages == [1]


def test_strangers_cannot_use_up_the_rate_limit(make: type[Harness]) -> None:
    harness = make(tv_config(rate_limit_per_minute=1))

    for _ in range(5):
        assert harness.post(peer=STRANGER).status_code == 403

    assert harness.post().status_code == 200


def test_other_tradingview_customers_cannot_use_up_the_rate_limit(make: type[Harness]) -> None:
    # Every TradingView customer sends from the same four addresses. Requests that do not
    # know the token must never make one of our own alerts answer 429.
    harness = make(tv_config(rate_limit_per_minute=1))
    junk = [body_of(token="not-the-token"), b"{", padded(5000)]

    statuses = {harness.post(junk[index % 3]).status_code for index in range(99)}

    assert statuses == {401, 400, 413}
    assert harness.post().status_code == 200
    assert harness.rejected(RejectCode.RATE_LIMITED) == 0


# --- 6. schema ----------------------------------------------------------------------------


def test_a_bad_schema_with_a_valid_token_is_refused_and_raises_one_alarm(
    make: type[Harness],
) -> None:
    harness = make()

    response = harness.post(body_of(tf="1D", stop=-5))
    again = harness.post(body_of(tf="W"))

    assert response.status_code == 422
    assert response.json() == {"error": "bad_schema"}
    assert again.status_code == 422
    assert harness.rejected(RejectCode.BAD_SCHEMA) == 2
    assert harness.signal_count() == 0
    alarms = harness.drain_outbox()
    assert len(alarms) == 1  # the second alarm was throttled
    assert "Geçersiz alert gövdesi" in alarms[0]
    assert "stop" in alarms[0]
    assert "tf" in alarms[0]
    assert harness.messages == [1]  # the sender was woken once
    assert harness.queued == []


def test_the_bad_schema_alarm_names_fields_but_never_values(make: type[Harness]) -> None:
    harness = make()
    secret_looking = "S3cr3t-Value-That-Must-Not-Travel"

    harness.post(body_of(symbol=secret_looking + " ", extra_key=secret_looking))

    (alarm,) = harness.drain_outbox()
    assert secret_looking not in alarm
    assert "extra_key" not in alarm
    assert "symbol" in alarm
    assert "unknown_key" in alarm


def test_the_bad_schema_alarm_is_sent_again_after_the_throttle(make: type[Harness]) -> None:
    harness = make()

    harness.post(body_of(tf="1D"))
    harness.clock.now_ms += 10 * 60_000
    harness.post(body_of(tf="1D"))

    assert len(harness.drain_outbox()) == 2
    assert harness.messages == [1, 1]


# --- 7. insert: queued, duplicate, stale, early -------------------------------------------


def test_a_valid_alert_is_stored_without_its_token_and_wakes_the_processor(
    make: type[Harness],
) -> None:
    harness = make()

    response = harness.post()

    assert response.status_code == 200
    assert response.json() == {"status": "queued"}
    assert harness.queued == [1]
    assert harness.messages == []
    with harness.store.transaction() as tx:
        row = tx.next_queued_signal()
    assert row is not None
    assert row.status is SignalStatus.QUEUED
    assert row.received_at_ms == NOW_MS
    assert (row.strategy_id, row.symbol, row.tf) == ("ny_orb_fvg", alerts.SYMBOL, "1")
    assert (row.bar_time_ms, row.bar_close_ms) == (BAR_TIME_MS, BAR_CLOSE_MS)
    assert (row.action, row.side, row.order_type) == ("entry", "long", "market")
    stored = json.loads(row.payload_json)
    assert "token" not in stored
    assert TOKEN not in row.payload_json
    assert stored["est_entry"] == row.est_entry


@pytest.mark.parametrize("name", sorted(alerts.pine_payloads()))
def test_every_shape_the_pine_scripts_emit_is_accepted(make: type[Harness], name: str) -> None:
    payload = alerts.pine_payloads()[name]
    seconds = 60 * int(str(payload["tf"]))
    bar_time = payload["bar_time"]
    assert isinstance(bar_time, int)
    harness = make(clock=FakeClock(bar_time + seconds * 1000 + 800))

    response = harness.post(json.dumps(payload).encode())

    assert response.status_code == 200
    assert response.json() == {"status": "queued"}
    assert harness.signal_count() == 1
    assert harness.outbox_total() == 0


def test_the_same_alert_twice_is_stored_once(make: type[Harness]) -> None:
    harness = make()

    first = harness.post()
    second = harness.post()
    different_content_same_key = harness.post(body_of(stop=5100.0))

    assert first.json() == {"status": "queued"}
    assert second.status_code == 200
    assert second.json() == {"status": "duplicate"}
    assert different_content_same_key.json() == {"status": "duplicate"}
    assert harness.signal_count() == 1
    assert harness.queued == [1]


def test_other_actions_on_the_same_bar_are_separate_signals(make: type[Harness]) -> None:
    harness = make()

    assert harness.post(body_of(action="entry")).json() == {"status": "queued"}
    assert harness.post(body_of(action="exit")).json() == {"status": "queued"}
    assert harness.post(body_of(symbol="CME_MINI:ES1!")).json() == {"status": "queued"}
    assert harness.signal_count() == 3


@pytest.mark.parametrize(
    ("late_ms", "status"),
    [(0, "queued"), (60_000, "queued"), (60_001, "stale"), (600_000, "stale")],
)
def test_staleness_is_measured_from_the_bar_close(
    make: type[Harness], late_ms: int, status: str
) -> None:
    harness = make(clock=FakeClock(BAR_CLOSE_MS + late_ms))

    response = harness.post()

    assert response.status_code == 200
    assert response.json() == {"status": status}
    assert harness.signal_count() == 1  # a stale alert is stored: the processor reports it
    assert harness.queued == [1]


def test_the_bar_close_follows_the_timeframe(make: type[Harness]) -> None:
    fifteen_minutes = 15 * 60_000
    fresh = make(clock=FakeClock(BAR_TIME_MS + fifteen_minutes + 60_000))
    stale = make(clock=FakeClock(BAR_TIME_MS + fifteen_minutes + 60_001))

    assert fresh.post(body_of(tf="15")).json() == {"status": "queued"}
    assert stale.post(body_of(tf="15")).json() == {"status": "stale"}


@pytest.mark.parametrize(("early_ms", "alarm"), [(0, False), (2_000, False), (2_001, True)])
def test_an_alert_ahead_of_its_bar_close_raises_a_clock_alarm(
    make: type[Harness], early_ms: int, alarm: bool
) -> None:
    harness = make(clock=FakeClock(BAR_CLOSE_MS - early_ms))

    response = harness.post()

    assert response.json() == {"status": "queued"}
    alarms = harness.drain_outbox()
    assert len(alarms) == (1 if alarm else 0)
    assert harness.messages == ([1] if alarm else [])
    for text in alarms:
        assert "ÖNCE geldi" in text
        assert "chronyc tracking" in text


def test_the_clock_alarm_is_throttled_and_skipped_for_duplicates(make: type[Harness]) -> None:
    harness = make(clock=FakeClock(BAR_CLOSE_MS - 30_000))

    harness.post()
    harness.post()  # duplicate
    harness.post(body_of(action="exit"))  # early again, inside the throttle interval

    assert len(harness.drain_outbox()) == 1
    assert harness.messages == [1]


def test_a_store_failure_answers_503_without_details(make: type[Harness]) -> None:
    harness = make()
    harness.store.close()

    response = harness.post()

    assert response.status_code == 503
    assert response.json() == {"error": "unavailable"}
    assert harness.rejected(RejectCode.STORE_ERROR) == 1
    assert harness.queued == []


def test_a_store_failure_while_raising_an_alarm_does_not_change_the_answer(
    make: type[Harness],
) -> None:
    harness = make()
    harness.store.close()

    assert harness.post(body_of(tf="1D")).status_code == 422
    assert harness.post(body_of(token="nope")).status_code == 401
    assert harness.post(b"{").status_code == 400
    assert harness.messages == []


# --- alarm for unauthenticated requests from allow-listed addresses -----------------------


def test_the_first_unauthenticated_rejection_raises_an_alarm(make: type[Harness]) -> None:
    harness = make()

    harness.post(body_of(token="stale-token-after-a-rotation"))

    (alarm,) = harness.drain_outbox()
    assert "1 istek kimliği doğrulanamadan" in alarm
    assert "yanlış/eksik token: 1" in alarm
    assert "Webhook token" in alarm
    assert "stale-token-after-a-rotation" not in alarm
    assert harness.messages == [1]


def test_a_flood_of_unauthenticated_requests_costs_one_alarm_per_hour(
    make: type[Harness],
) -> None:
    harness = make()
    junk = [body_of(token="nope"), b"{", padded(5000)]

    for index in range(300):
        harness.post(junk[index % 3])
    rows_within_the_hour = harness.outbox_total()
    harness.clock.now_ms += HOUR_MS
    harness.post(b"{")

    assert rows_within_the_hour == 1
    assert harness.messages == [1, 1]
    first, second = harness.drain_outbox()
    assert "1 istek" in first
    # 299 were counted while the gate was closed, plus the one that opened it again.
    assert "300 istek" in second
    assert "yanlış/eksik token: 99" in second
    assert "bozuk JSON: 101" in second
    assert "fazla büyük gövde: 100" in second


def test_strangers_never_raise_an_alarm(make: type[Harness]) -> None:
    harness = make()

    for _ in range(20):
        harness.post(body_of(token="nope"), peer=STRANGER)
        harness.via_proxy(STRANGER, b"{")

    assert harness.outbox_total() == 0
    assert harness.messages == []


def test_rejections_write_nothing_but_the_hourly_alarm(make: type[Harness]) -> None:
    harness = make()

    harness.post(peer=STRANGER)
    harness.post(padded(5000))
    harness.post(b"{")
    harness.post(body_of(token="nope"))

    assert harness.stats.snapshot() == {
        "ip_not_allowed": 1,
        "rate_limited": 0,
        "body_too_large": 1,
        "bad_json": 1,
        "bad_token": 1,
        "bad_schema": 0,
        "store_error": 0,
    }
    assert harness.signal_count() == 0
    assert harness.outbox_total() == 1


# --- secrets and logs ---------------------------------------------------------------------


def test_the_token_reaches_neither_logs_nor_answers_nor_the_database(make: type[Harness]) -> None:
    harness = make()
    bodies = [
        body_of(),
        body_of(),  # duplicate
        body_of(tf="1D"),  # bad schema
        body_of(action="exit", token=TOKEN + "x"),  # bad token
        b'{"token":"' + TOKEN.encode() + b'"',  # bad json
    ]

    with capture_logs() as logs:
        answers = [harness.post(body) for body in bodies]
        answers.append(harness.post(peer=STRANGER))

    assert [answer.status_code for answer in answers] == [200, 200, 422, 401, 400, 403]
    assert TOKEN not in json.dumps(logs, default=str)
    assert all(TOKEN not in answer.text for answer in answers)
    with harness.store.transaction() as tx:
        rows = tx.signals_received_between(0, 2**62)
    assert all(TOKEN not in row.payload_json for row in rows)
    alarms = harness.drain_outbox()
    assert len(alarms) == 2  # bad schema + unauthenticated
    assert all(TOKEN not in text for text in alarms)


def test_the_token_is_not_part_of_the_deps_repr(make: type[Harness]) -> None:
    harness = make()

    assert TOKEN not in repr(harness.deps)
    assert "webhook_token" not in repr(harness.deps)


def test_rejections_log_the_code_and_the_client_address(make: type[Harness]) -> None:
    harness = make()

    with capture_logs() as logs:
        harness.post(peer=STRANGER)
        harness.post(body_of(tf="1D", stop=-5))

    events = [(entry["event"], entry["code"], entry["client_ip"]) for entry in logs]
    assert events == [
        ("webhook_rejected", "ip_not_allowed", STRANGER),
        ("webhook_rejected", "bad_schema", TV_IP),
    ]
    assert logs[1]["fields"] == ["stop", "tf"]


def test_repeated_rejections_are_logged_once_per_interval_with_a_count(
    make: type[Harness],
) -> None:
    harness = make()

    with capture_logs() as logs:
        for _ in range(5):
            harness.post(peer=STRANGER)
        harness.clock.now_ms += 10_000
        harness.post(peer=STRANGER)

    assert [entry["suppressed"] for entry in logs] == [0, 4]
    assert harness.rejected(RejectCode.IP_NOT_ALLOWED) == 6
    assert harness.stats.last_ms(RejectCode.IP_NOT_ALLOWED) == NOW_MS + 10_000
    assert harness.stats.last_ms(RejectCode.BAD_TOKEN) is None


def test_accepted_alerts_log_their_latency(make: type[Harness]) -> None:
    harness = make()

    with capture_logs() as logs:
        harness.post()
        harness.post()

    accepted, duplicate = logs
    assert accepted["event"] == "webhook_accepted"
    assert accepted["latency_ms"] == 1_500
    assert accepted["signal_id"] == 1
    assert accepted["stale"] is False
    assert duplicate["event"] == "webhook_duplicate"


def test_a_handler_slower_than_half_the_tradingview_deadline_is_reported(
    make: type[Harness],
) -> None:
    clock = FakeClock()
    clock.step_ms = 800  # three clock reads per accepted request: 0, +800, +1600 ms
    harness = make(clock=clock)

    with capture_logs() as logs:
        harness.post()

    slow = [entry for entry in logs if entry["event"] == "webhook_slow"]
    assert len(slow) == 1
    assert slow[0]["elapsed_ms"] == 1_600


def test_stats_list_every_code_from_the_start() -> None:
    assert RejectStats().snapshot() == {code.value: 0 for code in RejectCode}


# --- routes -------------------------------------------------------------------------------


def test_health_answers_only_direct_local_requests(make: type[Harness]) -> None:
    harness = make()

    local = harness.request("GET", HEALTH_PATH, peer=LOOPBACK)
    local_v6 = harness.request("GET", HEALTH_PATH, peer="::1")
    proxied = harness.request(
        "GET", HEALTH_PATH, peer=LOOPBACK, headers={"X-Forwarded-For": STRANGER}
    )
    remote = harness.request("GET", HEALTH_PATH, peer=TV_IP)

    assert (local.status_code, local.json()) == (200, {"status": "ok"})
    assert local_v6.status_code == 200
    assert proxied.status_code == 404
    assert remote.status_code == 404


@pytest.mark.parametrize("path", ["/", "/docs", "/redoc", "/openapi.json", "/tv", "/tv/webhook/x"])
def test_no_other_route_exists(make: type[Harness], path: str) -> None:
    harness = make()

    response = harness.request("GET", path)

    assert response.status_code == 404
    assert harness.signal_count() == 0


@pytest.mark.parametrize("method", ["GET", "PUT", "DELETE", "PATCH"])
def test_the_webhook_route_only_accepts_post(make: type[Harness], method: str) -> None:
    harness = make()

    assert harness.request(method, WEBHOOK_PATH).status_code == 405


def test_a_closed_store_raises_the_documented_error(make: type[Harness]) -> None:
    harness = make()
    harness.store.close()

    with pytest.raises(StoreError), harness.store.transaction():
        pass
