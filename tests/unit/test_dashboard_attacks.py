"""Attacks on the local panel (ADR-0009): a hostile page in the user's browser, another host,
an alert sender who holds the webhook token, a careless maintainer.

Each test is one attack or one honesty check that failed against the first version of the
panel or guards a property the threat model depends on. Requests go through the real app
(TestClient) and, where header handling matters, through a real uvicorn socket.
"""

import ast
import asyncio
import dataclasses
import datetime as dt
import html
import logging
import socket
import threading
from collections.abc import Callable, Iterator
from html.parser import HTMLParser
from pathlib import Path
from typing import Any

import pytest
from fastapi.routing import APIRoute
from fastapi.testclient import TestClient
from hypothesis import given, settings
from hypothesis import strategies as st
from structlog.testing import capture_logs

from propbot.core.clock import ms_from_datetime
from propbot.dashboard import live
from propbot.dashboard.actions import (
    STANDBY_BALANCE_TEXT,
    ActionDeps,
    ActionKind,
    Actions,
    commands_factory,
)
from propbot.dashboard.app import DashboardDeps, create_app
from propbot.dashboard.command import build_server, serve
from propbot.dashboard.live import (
    MAX_SIGNAL_ROWS,
    MAX_TRADE_ITEMS,
    RECENT_SIGNAL_COUNT,
    BotState,
    DbState,
    ManualSnapshot,
    StoreSource,
    account_view,
    load_view,
    snapshot_text,
)
from propbot.dashboard.security import CSRF_FIELD, SECURITY_HEADERS
from propbot.notify.commands import PIN_MAX_FAILURES, CommandDeps, Commands
from propbot.risk.loader import load_risk_config, load_venue_profile
from propbot.risk.venue import DailyLossBasis, MaxLossType, ProfitTargets, VenueProfile
from propbot.settings import Settings
from propbot.signals.service import bind_listener
from propbot.signals.tvconfig import load_tv_alerts_config
from propbot.store.db import (
    STANDBY_ON,
    OutboxKind,
    Outcome,
    ReadTx,
    SignalRow,
    SignalStatus,
    StateKey,
    Store,
    StoreError,
    Tx,
)
from support import tg
from support.alerts import BAR_TIME_MS, TOKEN
from support.paths import CONFIG_FIXTURES, FIXTURES, REPO_ROOT

PORT = 8787
BASE = f"http://127.0.0.1:{PORT}"
PIN = "246813"
WRONG_PIN = "135790"
MINUTE_MS = 60_000
HOUR_MS = 60 * MINUTE_MS
NOW_MS = BAR_TIME_MS + MINUTE_MS + 1_500  # 2026-07-21 15:31:01.5 UTC
TODAY = dt.date(2026, 7, 21)
CSRF = "attack-test-csrf-token_0123456789abcdefghijk"
RISK = load_risk_config(CONFIG_FIXTURES / "valid" / "risk.yaml")
VENUE = load_venue_profile(CONFIG_FIXTURES / "valid" / "venues" / "sample_2step.yaml").model_copy(
    update={"rules_verified_at": dt.date(2026, 7, 9)}
)
OTHER_VENUE = load_venue_profile(
    CONFIG_FIXTURES / "valid_unverified" / "venues" / "sample_unverified.yaml"
)
TV = load_tv_alerts_config(FIXTURES / "tv_alerts" / "valid" / "full.yaml")
SETTINGS = Settings(
    telegram_bot_token=tg.BOT_TOKEN,
    telegram_allowed_chat_id=tg.CHAT_ID,
    tv_webhook_token=TOKEN,
    propbot_pin=PIN,
)
ACTION_PATHS = tuple(f"/eylem/{kind.value}" for kind in ActionKind)
HOSTILE = '"><img src=x onerror=alert(1)><script>alert(2)</script></textarea>'


def factory(clock: Callable[[], int] = lambda: NOW_MS) -> Callable[[Store], Commands]:
    return commands_factory(
        settings=SETTINGS,
        tv=TV,
        risk=RISK,
        venue=VENUE,
        venue_name="sample_2step",
        clock=clock,
        started_ms=NOW_MS,
    )


def deps(source: StoreSource, *, port: int = PORT, read_only: bool = False) -> DashboardDeps:
    return DashboardDeps(
        source=source,
        risk=RISK,
        venue=VENUE,
        venue_name="sample_2step",
        commands=None if read_only else factory(),
        unavailable_reason="sırlar okunamadı (PROPBOT_PIN)" if read_only else None,
        clock=lambda: NOW_MS,
        reports_dir=REPO_ROOT / "reports-does-not-exist",
        port=port,
        csrf_token=CSRF,
    )


def client_for(store: Store, *, read_only: bool = False) -> TestClient:
    return TestClient(
        create_app(deps(StoreSource(None, store=store), read_only=read_only)), base_url=BASE
    )


@pytest.fixture
def store() -> Iterator[Store]:
    with Store(":memory:") as opened:
        yield opened


@pytest.fixture
def client(store: Store) -> TestClient:
    return client_for(store)


def post(client: TestClient, path: str, **form: str) -> Any:
    return client.post(path, data={CSRF_FIELD: CSRF, **form}, headers={"Origin": BASE})


def flag(store: Store, key: StateKey) -> str | None:
    with store.transaction() as tx:
        return tx.get_state(key)


def set_flag(store: Store, key: StateKey, value: str = "1") -> None:
    with store.transaction() as tx:
        tx.set_state(key, value, NOW_MS)


def notice_texts(store: Store) -> list[str]:
    with store.transaction() as tx:
        rows = tx.messages_created_between(0, 2**62)
    return [row.text for row in rows if row.kind is OutboxKind.NOTICE]


def telegram(store: Store) -> Commands:
    return Commands(
        CommandDeps(
            store=store,
            risk=RISK,
            venue=VENUE,
            venue_name="sample_2step",
            tv=TV,
            pin=PIN,
            clock=lambda: NOW_MS,
            started_ms=NOW_MS,
            reject_counts=dict,
        )
    )


# --- PIN, KILL and the outcome the page reports -----------------------------------------------


@pytest.mark.parametrize(
    "pin",
    [WRONG_PIN, "", "abc123", "1" * 1500, "２４６８１３", f" {PIN}x", f"{PIN}\n/devam {PIN}"],
)
def test_a_kill_that_did_not_take_is_never_reported_as_done(
    client: TestClient, store: Store, pin: str
) -> None:
    response = post(client, "/eylem/kill", pin=pin)

    assert flag(store, StateKey.KILLED) is None
    assert response.status_code == 409
    assert "yapılmadı" in response.text
    assert "yapıldı<" not in response.text
    assert PIN not in response.text


def test_resume_while_killed_is_reported_as_not_done(client: TestClient, store: Store) -> None:
    set_flag(store, StateKey.KILLED)

    response = post(client, "/eylem/devam", onay="1")

    assert response.status_code == 409
    assert "PIN gerekir" in response.text
    assert flag(store, StateKey.KILLED) == "1"


@pytest.mark.parametrize("path", ACTION_PATHS)
@pytest.mark.parametrize(
    "form",
    [
        {},
        {"onay": "1"},
        {"pin": WRONG_PIN, "onay": "1"},
        {"pin": "", "onay": "1", "bakiye": "100000"},
    ],
)
def test_no_route_lifts_a_kill_without_the_pin(
    client: TestClient, store: Store, path: str, form: dict[str, str]
) -> None:
    set_flag(store, StateKey.KILLED)
    set_flag(store, StateKey.PAUSED)

    post(client, path, **form)
    client.post(
        f"{path}?pin={PIN}&kind=kill-kaldir",
        data={CSRF_FIELD: CSRF, **form},
        headers={"Origin": BASE},
    )
    client.get(f"{path}?pin={PIN}")

    assert flag(store, StateKey.KILLED) == "1"


def test_only_the_lift_route_with_the_pin_lifts_a_kill(client: TestClient, store: Store) -> None:
    set_flag(store, StateKey.KILLED)

    response = post(client, "/eylem/kill-kaldir", pin=PIN)

    assert response.status_code == 200
    assert flag(store, StateKey.KILLED) is None
    assert PIN not in response.text


def test_a_lock_set_by_telegram_holds_on_the_panel_and_disables_its_pin_forms(
    client: TestClient, store: Store
) -> None:
    set_flag(store, StateKey.KILLED)
    commands = telegram(store)
    for _ in range(PIN_MAX_FAILURES):
        commands.handle(f"/devam {WRONG_PIN}", sent_at_ms=NOW_MS)

    response = post(client, "/eylem/kill-kaldir", pin=PIN)
    page = client.get("/").text

    assert response.status_code == 409
    assert "PIN girişi kilitli" in response.text
    assert flag(store, StateKey.KILLED) == "1"
    assert "PIN girişi kilitli" in page
    assert page.count("<fieldset disabled>") == 2  # KILL and lifting it


def test_a_lock_set_by_the_panel_holds_on_telegram(client: TestClient, store: Store) -> None:
    for _ in range(PIN_MAX_FAILURES):
        post(client, "/eylem/kill", pin=WRONG_PIN)

    reply = telegram(store).handle(f"/kill {PIN}", sent_at_ms=NOW_MS).text

    assert "PIN girişi kilitli" in reply
    assert flag(store, StateKey.KILLED) is None


def test_the_pin_reaches_no_page_log_or_notice_even_when_the_store_fails_mid_action(
    client: TestClient,
    store: Store,
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    caplog.set_level(logging.DEBUG)
    original = Tx.set_state

    def failing_set_state(self: Tx, key: str, value: str, now_ms: int) -> None:
        if key == StateKey.KILLED:
            raise StoreError(f"disk I/O error while writing {value}")
        original(self, key, value, now_ms)

    with capture_logs() as logs:
        monkeypatch.setattr(Tx, "set_state", failing_set_state)
        failed = post(client, "/eylem/kill", pin=PIN)
        monkeypatch.setattr(Tx, "set_state", original)
        done = post(client, "/eylem/kill", pin=PIN)
        lifted = post(client, "/eylem/kill-kaldir", pin=PIN)
        wrong = post(client, "/eylem/kill", pin=WRONG_PIN)

    assert failed.status_code == 409  # the command answered with an error and changed nothing
    assert "yapılmadı" in failed.text
    assert done.status_code == lifted.status_code == 200
    texts = [r.text for r in (failed, done, lifted, wrong)] + notice_texts(store)
    texts += [repr(entry) for entry in logs] + [r.getMessage() for r in caplog.records]
    for text in texts:
        assert PIN not in text
        assert WRONG_PIN not in text


# --- one transaction or an honest result ------------------------------------------------------


def test_a_failed_notice_write_keeps_the_kill_and_says_the_notice_is_missing(
    client: TestClient, store: Store, monkeypatch: pytest.MonkeyPatch
) -> None:
    # Commands.handle commits the KILL in its own transaction; undoing a KILL because its
    # notice could not be queued would be the unsafe direction, so the page says so instead.
    def failing_enqueue(self: Tx, **_: object) -> int:
        raise StoreError("database or disk is full")

    monkeypatch.setattr(Tx, "enqueue_message", failing_enqueue)
    response = post(client, "/eylem/kill", pin=PIN)
    monkeypatch.undo()

    assert flag(store, StateKey.KILLED) == "1"
    assert response.status_code == 200
    assert "YAZILAMADI" in response.text
    assert notice_texts(store) == []


def test_a_balance_snapshot_and_its_notice_are_both_or_none(
    client: TestClient, store: Store, monkeypatch: pytest.MonkeyPatch
) -> None:
    def failing_enqueue(self: Tx, **_: object) -> int:
        raise StoreError("database or disk is full")

    monkeypatch.setattr(Tx, "enqueue_message", failing_enqueue)
    response = post(client, "/eylem/bakiye", bakiye="104000", onay="1")
    monkeypatch.undo()

    assert response.status_code == 503
    assert "yapılmadı" in response.text
    assert flag(store, StateKey.MANUAL_ACCOUNT) is None


def test_double_submit_is_harmless(client: TestClient, store: Store) -> None:
    first = post(client, "/eylem/kill", pin=PIN)
    second = post(client, "/eylem/kill", pin=PIN)
    lifted = post(client, "/eylem/kill-kaldir", pin=PIN)
    again = post(client, "/eylem/kill-kaldir", pin=PIN)

    assert first.status_code == second.status_code == lifted.status_code == 200
    assert "KILL zaten etkindi" in second.text
    assert again.status_code == 409  # nothing left to lift; no PIN was checked
    assert flag(store, StateKey.KILLED) is None
    assert flag(store, StateKey.PIN_FAIL_COUNT) is None
    assert len(notice_texts(store)) == 3


def test_forged_forms_are_refused_server_side_when_the_panel_is_read_only(store: Store) -> None:
    client = client_for(store, read_only=True)
    form = {"pin": PIN, "onay": "1", "bakiye": "104000"}

    statuses = {path: post(client, path, **form).status_code for path in ACTION_PATHS}

    assert set(statuses.values()) == {409}
    with store.transaction() as tx:
        assert {key: tx.get_state(key) for key in StateKey} == dict.fromkeys(StateKey)
    assert notice_texts(store) == []


def test_a_standby_copy_refuses_the_balance_form(client: TestClient, store: Store) -> None:
    set_flag(store, StateKey.STANDBY, STANDBY_ON)

    response = post(client, "/eylem/bakiye", bakiye="104000", onay="1")

    assert response.status_code == 409
    assert "BEKLEMEDE" in response.text
    assert html.escape(STANDBY_BALANCE_TEXT) in response.text
    assert flag(store, StateKey.MANUAL_ACCOUNT) is None
    assert notice_texts(store) == []


# --- the database file under the panel --------------------------------------------------------


def test_a_database_file_replaced_after_start_is_followed(tmp_path: Path) -> None:
    path = tmp_path / "propbot.db"
    with Store(path) as old:
        set_flag(old, StateKey.KILLED)
    source = StoreSource(path)
    actions = Actions(
        ActionDeps(
            source=source,
            commands=factory(),
            unavailable_reason="",
            venue=VENUE,
            clock=lambda: NOW_MS,
        )
    )
    before = load_view(source, venue=VENUE, venue_name="x", risk=RISK, now_ms=NOW_MS)
    # The runbook's schema-mismatch recovery: rename the old file aside, `serve` makes a new one.
    path.rename(tmp_path / "propbot.db.old")
    Store(path).close()

    after = load_view(source, venue=VENUE, venue_name="x", risk=RISK, now_ms=NOW_MS)
    paused = actions.run(ActionKind.PAUSE, {"onay": "1"})
    source.close()

    assert before.bot is not None
    assert before.bot.state is BotState.KILLED
    assert after.bot is not None
    assert after.bot.state is BotState.ACTIVE
    assert paused.status == 200
    with Store(path) as current:
        assert flag(current, StateKey.PAUSED) == "1"
    with Store(tmp_path / "propbot.db.old") as renamed:
        assert flag(renamed, StateKey.PAUSED) is None


def test_a_database_file_removed_after_start_is_reported_missing(tmp_path: Path) -> None:
    path = tmp_path / "propbot.db"
    Store(path).close()
    source = StoreSource(path)
    assert load_view(source, venue=VENUE, venue_name="x", risk=RISK, now_ms=NOW_MS).db.ok

    path.unlink()
    view = load_view(source, venue=VENUE, venue_name="x", risk=RISK, now_ms=NOW_MS)
    source.close()

    assert view.db.state is DbState.MISSING
    assert not path.exists()


def test_page_routes_run_off_the_event_loop() -> None:
    # A busy database blocks a request for the store's busy timeout; in a worker thread
    # that delays only that request, on the event loop it would stall the whole panel.
    with Store(":memory:") as opened:
        app = create_app(deps(StoreSource(None, store=opened)))
    for route in app.routes:
        if isinstance(route, APIRoute) and "GET" in route.methods:
            assert not asyncio.iscoroutinefunction(route.endpoint), route.path


# --- stored XSS through every signal field ----------------------------------------------------


class _Markup(HTMLParser):
    def __init__(self) -> None:
        super().__init__()
        self.problems: list[str] = []

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        names = {name for name, _ in attrs}
        if tag == "script" and "src" not in names:
            self.problems.append("inline script")
        if tag in ("style", "img", "svg", "iframe", "object", "embed"):
            self.problems.append(f"<{tag}>")
        for name, value in attrs:
            if name.startswith("on") or name == "style":
                self.problems.append(f"attribute {name}")
            if value is not None and value.strip().lower().startswith("javascript:"):
                self.problems.append(f"javascript: in {name}")


def assert_clean(document: str) -> None:
    parser = _Markup()
    parser.feed(document)
    assert parser.problems == []
    assert HOSTILE not in document


def test_hostile_text_in_every_signal_field_is_escaped_on_every_page(
    client: TestClient, store: Store
) -> None:
    hostile = {
        "strategy_id": HOSTILE,
        "symbol": HOSTILE,
        "tf": HOSTILE,
        "side": HOSTILE,
        "setup": HOSTILE,
        "reason": HOSTILE,
        "order_type": HOSTILE,
        "payload_json": HOSTILE,
    }
    tg.forwarded_entry(store, prop_day=TODAY, now_ms=NOW_MS, **hostile)
    tg.add_signal(store, action=HOSTILE, bar_time_ms=BAR_TIME_MS + MINUTE_MS, **hostile)
    tg.add_signal(store, action="exit", bar_time_ms=BAR_TIME_MS + 2 * MINUTE_MS, **hostile)

    page = client.get("/")
    fragment = client.get("/parca/canli")
    data = client.get("/api/live")

    for response in (page, fragment):
        assert response.headers["content-type"].startswith("text/html")
        assert_clean(response.text)
        assert "&lt;script&gt;" in response.text
    assert data.headers["content-type"] == "application/json"
    assert HOSTILE in data.json()["signals"][0]["symbol"]  # data, never markup


def test_hostile_balance_input_is_neither_stored_nor_echoed(
    client: TestClient, store: Store
) -> None:
    response = post(client, "/eylem/bakiye", bakiye=HOSTILE, ozsermaye=HOSTILE, onay="1")

    assert response.status_code == 400
    assert_clean(response.text)
    assert "<img" not in response.text
    assert flag(store, StateKey.MANUAL_ACCOUNT) is None


# --- requests a browser on another site can make ----------------------------------------------


@pytest.mark.parametrize(
    ("content_type", "body"),
    [
        ("text/plain", f"{CSRF_FIELD}={CSRF}&onay=1"),
        ("application/json", f'{{"{CSRF_FIELD}": "{CSRF}", "onay": "1"}}'),
        (
            "multipart/form-data; boundary=x",
            f'--x\r\nContent-Disposition: form-data; name="{CSRF_FIELD}"\r\n\r\n'
            f"{CSRF}\r\n--x--\r\n",
        ),
        ("application/x-www-form-urlencoded", "onay=1"),
        ("application/x-www-form-urlencoded", f"{CSRF_FIELD}=&onay=1"),
        ("application/x-www-form-urlencoded", f"{CSRF_FIELD}=forged-{CSRF}&onay=1"),
    ],
)
@pytest.mark.parametrize("origin", [None, "http://evil.example", "null"])
def test_cross_site_posts_change_nothing(
    client: TestClient, store: Store, content_type: str, body: str, origin: str | None
) -> None:
    headers = {"Content-Type": content_type}
    if origin is not None:
        headers["Origin"] = origin
    response = client.post("/eylem/duraklat", content=body.encode(), headers=headers)

    assert response.status_code in (403, 415)
    assert flag(store, StateKey.PAUSED) is None
    for name, value in SECURITY_HEADERS.items():
        assert response.headers[name] == value


@pytest.mark.parametrize("method", ["HEAD", "OPTIONS", "PUT", "DELETE", "PATCH"])
@pytest.mark.parametrize("path", ["/", "/eylem/kill", "/eylem/duraklat"])
def test_other_methods_are_refused_without_cors(
    client: TestClient, store: Store, method: str, path: str
) -> None:
    response = client.request(
        method,
        path,
        headers={"Origin": "http://evil.example", "Access-Control-Request-Method": "POST"},
    )

    assert response.status_code == 405
    assert not any(name.lower().startswith("access-control-") for name in response.headers)
    assert (
        response.headers["content-security-policy"] == SECURITY_HEADERS["content-security-policy"]
    )
    assert flag(store, StateKey.PAUSED) is None


@pytest.mark.parametrize(
    "path",
    [
        "/<script>alert(1)</script>",
        "/assets/../../etc/passwd",
        "/assets/%2e%2e/%2e%2e/etc/passwd",
        "/assets/app.css/../../pyproject.toml",
        "/api/live/../../.env",
    ],
)
def test_unknown_paths_are_plain_404s_that_echo_nothing(client: TestClient, path: str) -> None:
    response = client.get(path)

    assert response.status_code == 404
    assert response.headers["content-type"].startswith("text/plain")
    assert "<script" not in response.text
    assert "passwd" not in response.text
    assert response.headers["x-content-type-options"] == "nosniff"


class _Server:
    """The real uvicorn server on an ephemeral loopback port."""

    def __init__(self, store: Store) -> None:
        self.listener = bind_listener("127.0.0.1", 0)
        self.port: int = self.listener.getsockname()[1]
        app = create_app(deps(StoreSource(None, store=store), port=self.port))
        self.server = build_server(app)
        self.thread = threading.Thread(target=serve, args=(self.server, self.listener), daemon=True)
        self.thread.start()

    def raw(self, request: bytes) -> tuple[int, bytes]:
        with socket.create_connection(("127.0.0.1", self.port), timeout=10) as conn:
            conn.sendall(request)
            data = b""
            while chunk := conn.recv(65536):
                data += chunk
        status_line = data.split(b"\r\n", 1)[0]
        return int(status_line.split()[1]), data

    def stop(self) -> None:
        self.server.should_exit = True
        self.thread.join(timeout=15)
        self.listener.close()


@pytest.fixture
def server(store: Store) -> Iterator[_Server]:
    running = _Server(store)
    yield running
    running.stop()


def test_odd_host_headers_over_a_real_socket(server: _Server, store: Store) -> None:
    port = str(server.port).encode()
    good = b"127.0.0.1:" + port

    def get(target: bytes, *headers: bytes, version: bytes = b"HTTP/1.1") -> int:
        lines = [b"GET " + target + b" " + version, *headers, b"Connection: close"]
        return server.raw(b"\r\n".join(lines) + b"\r\n\r\n")[0]

    assert get(b"/", b"Host: " + good) == 200
    assert get(b"/", b"Host: [::1]:" + port) == 200
    assert get(b"/", b"Host: localhost:" + port) == 200
    refused = [
        b"Host: evil.example",
        b"Host: evil.example:" + port,
        b"Host: LOCALHOST:" + port,
        b"Host: localhost.:" + port,
        b"Host: user@" + good,
        b"Host: 127.0.0.1:" + str(server.port + 1).encode(),
        b"Host: 127.0.0.1",
        b"Host: [0:0:0:0:0:0:0:1]:" + port,
        b"Host: 2130706433:" + port,
    ]
    for header in refused:
        assert get(b"/", header) == 421, header
    # Absolute-form target naming this panel does not stand in for a foreign Host.
    assert get(b"http://" + good + b"/", b"Host: evil.example") == 421
    assert get(b"/", b"Host: " + good, b"Host: evil.example") == 400
    assert get(b"/", version=b"HTTP/1.0") == 400
    assert flag(store, StateKey.PAUSED) is None


def test_a_post_with_a_foreign_host_never_reaches_the_action(server: _Server, store: Store) -> None:
    body = f"{CSRF_FIELD}={CSRF}&onay=1".encode()
    request = (
        b"POST /eylem/duraklat HTTP/1.1\r\nHost: rebind.example:"
        + str(server.port).encode()
        + b"\r\nOrigin: http://rebind.example:"
        + str(server.port).encode()
        + b"\r\nContent-Type: application/x-www-form-urlencoded\r\nContent-Length: "
        + str(len(body)).encode()
        + b"\r\nConnection: close\r\n\r\n"
        + body
    )

    status, data = server.raw(request)

    assert status == 421
    assert b"content-security-policy" in data.lower()
    assert flag(store, StateKey.PAUSED) is None


# --- honesty of the numbers -------------------------------------------------------------------


def prague_noon_ms(day: dt.date) -> int:
    return ms_from_datetime(
        dt.datetime(day.year, day.month, day.day, 12, tzinfo=VENUE.daily_reset.zone)
    )


EUR_VENUE = VENUE.model_copy(
    update={
        "initial_balance": 50_000.0,
        "account_currency": "EUR",
        "profit_target_pct": ProfitTargets(phase1=8.0, phase2=None),
        "daily_loss_limit_pct": 4.0,
        "max_loss_pct": 8.0,
    }
)


def manual(balance: float, equity: float, day_start: float | None, at_ms: int) -> str:
    return snapshot_text(
        ManualSnapshot(
            balance=balance, equity=equity, day_start_balance=day_start, entered_at_ms=at_ms
        )
    )


def view_for(
    venue: VenueProfile,
    *,
    text: str | None,
    used: float | None = 250.0,
    entries: int = 1,
    now_ms: int | None = None,
) -> live.AccountView:
    return account_view(
        venue=venue,
        risk=RISK,
        manual_text=text,
        budget_used=used,
        budget_entries=entries,
        now_ms=prague_noon_ms(TODAY) if now_ms is None else now_ms,
    )


def test_account_numbers_by_hand_for_the_sample_profile() -> None:
    # 100000 USD, targets 10 % / 5 %, daily 5 %, max 10 % static; risk daily cap 1 %.
    view = view_for(VENUE, text=manual(104_000, 103_000, 105_000, prague_noon_ms(TODAY)))

    phase1, phase2 = view.targets
    assert (phase1.fraction, phase1.value_text) == (0.4, "$4000 / $10000 (%40.0)")
    assert (phase2.fraction, phase2.value_text) == (0.8, "$4000 / $5000 (%80.0)")
    # Floor 90000; the lower of balance and equity (103000) is 13000 above it.
    assert view.total_loss.fraction == 1.0
    assert view.total_loss.value_text == "kalan $13000 / $10000 (%130.0) · taban $90000"
    bot, firm = view.ledger
    # min(1 %, 0.5 x 5 %) of 100000 = 1000; the firm's daily limit is 5000.
    assert (bot.fraction, bot.value_text) == (0.25, "$250 / $1000 (%25.0) · 1 giriş")
    assert (firm.fraction, firm.value_text) == (0.05, "$250 / $5000 (%5.0)")
    assert view.declared_daily is not None
    # Declared: 105000 at the start of the day, equity 103000 now -> 2000 of 5000.
    assert view.declared_daily.fraction == 0.4
    assert view.declared_daily.value_text == "kayıp $2000 / $5000 (%40.0) · kalan $3000"
    for gauge in (*view.ledger, view.declared_daily):
        assert "ölçülmüş" in gauge.source or "beyan" in gauge.source


def test_account_numbers_by_hand_for_a_second_profile() -> None:
    # 50000 EUR, one phase 8 %, daily 4 %, max 8 %: nothing of the first profile may show.
    view = view_for(
        EUR_VENUE, text=manual(51_000, 50_500, 52_000, prague_noon_ms(TODAY)), used=0.0, entries=0
    )

    (phase1,) = view.targets
    assert phase1.value_text == "1000 EUR / 4000 EUR (%25.0)"
    assert view.total_loss.value_text == "kalan 4500 EUR / 4000 EUR (%112.5) · taban 46000 EUR"
    bot, firm = view.ledger
    # min(1 %, 0.5 x 4 %) of 50000 = 500; firm 4 % = 2000; nothing used yet.
    assert bot.value_text == "0 EUR / 500 EUR (%0.0) · 0 giriş"
    assert firm.value_text == "0 EUR / 2000 EUR (%0.0)"
    assert view.declared_daily is not None
    assert view.declared_daily.value_text == "kayıp 1500 EUR / 2000 EUR (%75.0) · kalan 500 EUR"
    assert view.declared_daily.warn


def test_the_reset_equity_basis_shows_the_declared_buffer_as_an_upper_bound() -> None:
    assert OTHER_VENUE.daily_loss_basis is DailyLossBasis.MAX_BALANCE_EQUITY_AT_RESET
    view = view_for(OTHER_VENUE, text=manual(104_000, 103_000, 105_000, prague_noon_ms(TODAY)))

    assert view.declared_daily is not None
    assert view.declared_daily.value_text.endswith("kalan en çok $3000")
    assert "reset anındaki özsermaye" in view.declared_daily.source


def test_edge_balances_clamp_the_bar_but_not_the_words() -> None:
    above = view_for(VENUE, text=manual(112_000, 112_000, None, prague_noon_ms(TODAY)))
    below = view_for(VENUE, text=manual(91_000, 88_000, None, prague_noon_ms(TODAY)))

    assert above.targets[0].fraction == 1.0
    assert above.targets[0].value_text == "$12000 / $10000 (%120.0)"
    assert below.targets[0].fraction == 0.0
    assert below.targets[0].value_text == "−$9000 / $10000 (%-90.0)"
    assert below.total_loss.fraction == 0.0
    assert below.total_loss.warn
    assert below.total_loss.value_text == "kalan −$2000 / $10000 (%-20.0) · taban $90000"
    assert above.declared_daily is None
    assert "girilmedi" in above.declared_daily_note


def test_a_stale_snapshot_is_labelled_and_gives_no_declared_daily_loss() -> None:
    yesterday = prague_noon_ms(TODAY - dt.timedelta(days=1))
    view = view_for(VENUE, text=manual(104_000, 103_000, 105_000, yesterday))

    assert view.stale
    assert "güncel değil" in view.source
    assert all("güncel değil" in gauge.source for gauge in (*view.targets, view.total_loss))
    assert view.declared_daily is None
    assert "önceki prop gününden" in view.declared_daily_note


def test_default_balance_is_never_presented_as_measured() -> None:
    view = view_for(VENUE, text=None, used=None, entries=0)

    assert not view.manual
    assert "varsayılan" in view.source
    assert "gerçek bakiye bilinmiyor" in view.source
    assert all(gauge.fraction is None for gauge in view.ledger)
    assert view.declared_daily is None


def test_a_loss_type_without_a_formula_is_not_computed() -> None:
    trailing = VENUE.model_copy(update={"max_loss_type": MaxLossType.TRAILING_EOD})

    gauge = view_for(trailing, text=manual(95_000, 95_000, None, prague_noon_ms(TODAY))).total_loss

    assert gauge.fraction is None
    assert "hesaplanamıyor" in gauge.value_text


_RULE_LIKE_INTS = frozenset({100_000, 50_000, 10_000, 200_000})


def test_no_rule_value_is_written_into_the_panel_code() -> None:
    # Rule values come from config/venues and config/risk (CLAUDE.md invariant 10). Numbers
    # that are not obviously structural must be named module constants with a reason.
    problems: list[str] = []
    for path in sorted((REPO_ROOT / "src" / "propbot" / "dashboard").glob("*.py")):
        tree = ast.parse(path.read_text(encoding="utf-8"))
        named = set()
        for node in tree.body:
            if isinstance(node, ast.AnnAssign | ast.Assign):
                named.update(id(child) for child in ast.walk(node))
        for node in ast.walk(tree):
            if not isinstance(node, ast.Constant) or isinstance(node.value, bool):
                continue
            value = node.value
            if isinstance(value, float) and 0 < value < 1 and id(node) not in named:
                problems.append(f"{path.name}:{node.lineno} {value}")
            if isinstance(value, int | float) and value in _RULE_LIKE_INTS:
                problems.append(f"{path.name}:{node.lineno} {value}")
    assert problems == []


# --- robustness with many signals -------------------------------------------------------------


def bulk_signals(store: Store, count: int, *, step_ms: int) -> None:
    """`count` forwarded signals, one per `step_ms`, newest at NOW_MS; entries and exits mixed."""
    with store.transaction() as tx:
        for index in range(count):
            moment = NOW_MS - index * step_ms
            signal = dataclasses.replace(
                tg.ENTRY_SIGNAL,
                received_at_ms=moment,
                bar_time_ms=moment - MINUTE_MS,
                bar_close_ms=moment,
                action=("entry", "exit", "heartbeat", "stop_update")[index % 4],
            )
            signal_id = tx.insert_signal(signal)
            assert signal_id is not None
            tx.complete_signal(signal_id, Outcome.FORWARDED, moment)


def test_a_refresh_reads_a_bounded_number_of_rows_from_a_flooded_database(
    store: Store, monkeypatch: pytest.MonkeyPatch
) -> None:
    # 30 alerts a minute (the receiver's hard maximum) for a little over five hours.
    bulk_signals(store, 10_000, step_ms=2_000)
    calls: dict[str, int] = {"rows": 0, "messages": 0, "budget": 0}
    recent, messages, budget = (
        ReadTx.recent_signals,
        ReadTx.messages_for_signal,
        ReadTx.get_budget_entry,
    )

    def counted_recent(self: ReadTx, limit: int, **bounds: Any) -> list[SignalRow]:
        rows = recent(self, limit, **bounds)
        calls["rows"] += len(rows)
        return rows

    def counted_messages(self: ReadTx, signal_id: int) -> Any:
        calls["messages"] += 1
        return messages(self, signal_id)

    def counted_budget(self: ReadTx, signal_id: int) -> Any:
        calls["budget"] += 1
        return budget(self, signal_id)

    # The refresh reads through the read-only snapshot; the slicing reader is gone, so no
    # other signal query may stand in for the counted one.
    monkeypatch.setattr(ReadTx, "recent_signals", counted_recent)
    monkeypatch.setattr(ReadTx, "messages_for_signal", counted_messages)
    monkeypatch.setattr(ReadTx, "get_budget_entry", counted_budget)
    monkeypatch.setattr(ReadTx, "signals_received_between", None)
    view = load_view(
        StoreSource(None, store=store), venue=VENUE, venue_name="x", risk=RISK, now_ms=NOW_MS
    )

    # One row past the limit tells the refresh that older rows were cut.
    assert calls["rows"] <= MAX_SIGNAL_ROWS + 1
    assert calls["messages"] <= RECENT_SIGNAL_COUNT
    assert calls["budget"] <= MAX_TRADE_ITEMS
    assert len(view.signals) == RECENT_SIGNAL_COUNT
    assert len(view.open_trades) + len(view.closed_trades) == MAX_TRADE_ITEMS
    assert "yalnızca en yeni" in view.trades_note


def test_an_empty_database_renders_every_page(client: TestClient) -> None:
    for path in ("/", "/parca/canli", "/api/live", "/analiz"):
        assert client.get(path).status_code == 200


# --- the faster trade matching is the processor's rule ----------------------------------------


def _row(index: int, strategy: str, bar: int, action: str, outcome: Outcome | None) -> SignalRow:
    return SignalRow(
        id=index + 1,
        received_at_ms=NOW_MS,
        strategy_id=strategy,
        symbol="MES",
        tf="1",
        bar_time_ms=bar * MINUTE_MS,
        bar_close_ms=bar * MINUTE_MS + MINUTE_MS,
        action=action,
        side="long",
        setup="",
        order_type=None,
        est_entry=None,
        stop=None,
        tp=None,
        rr=None,
        reason=None,
        payload_json="{}",
        status=SignalStatus.DONE,
        outcome=outcome,
        processed_at_ms=NOW_MS,
    )


def _reference_closing(entry: SignalRow, signals: list[SignalRow]) -> SignalRow | None:
    """The first implementation, kept as the specification (quadratic, but plainly right)."""
    same = [
        s
        for s in signals
        if s.strategy_id == entry.strategy_id and s.symbol == entry.symbol and s.id != entry.id
    ]
    for follow in sorted(same, key=lambda s: (s.bar_time_ms, s.id)):
        if follow.action not in ("exit", "cancel") or follow.outcome not in (
            Outcome.FORWARDED,
            Outcome.NO_ACTIVE_TRADE,
        ):
            continue
        if (
            not entry.bar_time_ms
            <= follow.bar_time_ms
            <= entry.bar_time_ms + live.FOLLOW_UP_WINDOW_MS
        ):
            continue
        if not any(
            other.action == "entry" and entry.bar_time_ms < other.bar_time_ms <= follow.bar_time_ms
            for other in same
        ):
            return follow
    return None


_SIGNAL = st.tuples(
    st.sampled_from(["a", "b"]),
    st.integers(min_value=0, max_value=3000),
    st.sampled_from(["entry", "exit", "cancel", "stop_update"]),
    st.sampled_from([Outcome.FORWARDED, Outcome.NO_ACTIVE_TRADE, Outcome.SKIP_BUDGET, None]),
)


@settings(max_examples=200, deadline=None)
@given(st.lists(_SIGNAL, max_size=30))
def test_trade_matching_equals_the_reference_rule(
    specs: list[tuple[str, int, str, Outcome | None]],
) -> None:
    rows = [_row(index, *spec) for index, spec in enumerate(specs)]
    groups = live._by_key(rows)

    for entry in rows:
        if entry.action != "entry":
            continue
        expected = _reference_closing(entry, rows)
        actual = live._closing_signal(entry, groups[(entry.strategy_id, entry.symbol)])
        assert (actual.id if actual else None) == (expected.id if expected else None)


def test_a_busy_database_is_shown_as_busy_and_an_action_changes_nothing(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # What the store raises once its 5 s busy timeout runs out while `serve` writes; the
    # wait itself is SQLite's and is covered by the live-view tests.
    path = tmp_path / "propbot.db"
    Store(path).close()
    source = StoreSource(path)
    assert isinstance(source.get(), Store)

    def locked(self: Store) -> Any:
        raise StoreError("database is locked")

    # The page reads through `read`, the action writes through `transaction`.
    monkeypatch.setattr(Store, "read", locked)
    monkeypatch.setattr(Store, "transaction", locked)
    app_client = TestClient(create_app(deps(source)), base_url=BASE)
    page = app_client.get("/")
    action = post(app_client, "/eylem/duraklat", onay="1")
    monkeypatch.undo()
    source.close()

    assert page.status_code == 200
    assert "kilitli" in page.text
    assert action.status_code == 503
    assert "Hiçbir şey değiştirilmedi" in action.text
    with Store(path) as reopened:
        assert flag(reopened, StateKey.PAUSED) is None
