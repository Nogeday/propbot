"""Panel security: Host / Origin / Referer / CSRF / body checks and headers on every answer."""

import asyncio
import datetime as dt
from collections.abc import Iterator
from pathlib import Path

import pytest
from fastapi.testclient import TestClient
from starlette.types import Message, Receive, Scope, Send

from propbot.dashboard.actions import commands_factory
from propbot.dashboard.app import DashboardDeps, create_app
from propbot.dashboard.live import StoreSource
from propbot.dashboard.security import (
    CSP,
    CSRF_FIELD,
    MAX_FORM_BYTES,
    SECURITY_HEADERS,
    SecurityMiddleware,
    allowed_hosts,
    csrf_matches,
    host_allowed,
    new_csrf_token,
    origin_allowed,
    parse_form,
)
from propbot.risk.loader import load_risk_config, load_venue_profile
from propbot.settings import Settings
from propbot.signals.tvconfig import load_tv_alerts_config
from propbot.store.db import StateKey, Store
from support import tg
from support.alerts import BAR_TIME_MS, TOKEN
from support.paths import CONFIG_FIXTURES, FIXTURES

PORT = 8787
BASE = f"http://127.0.0.1:{PORT}"
ORIGIN = BASE
PIN = "246813"
NOW_MS = BAR_TIME_MS + 61_500
CSRF = "fixed-test-csrf-token_0123456789abcdefghijkl"
RISK = load_risk_config(CONFIG_FIXTURES / "valid" / "risk.yaml")
VENUE = load_venue_profile(CONFIG_FIXTURES / "valid" / "venues" / "sample_2step.yaml").model_copy(
    update={"rules_verified_at": dt.date(2026, 7, 9)}
)
TV = load_tv_alerts_config(FIXTURES / "tv_alerts" / "valid" / "full.yaml")
SETTINGS = Settings(
    telegram_bot_token=tg.BOT_TOKEN,
    telegram_allowed_chat_id=tg.CHAT_ID,
    tv_webhook_token=TOKEN,
    propbot_pin=PIN,
)
GET_ROUTES = ("/", "/parca/canli", "/api/live", "/analiz", "/assets/app.css", "/assets/app.js")
ACTION_ROUTES = (
    "/eylem/duraklat",
    "/eylem/devam",
    "/eylem/kill",
    "/eylem/kill-kaldir",
    "/eylem/ozet",
    "/eylem/bakiye",
)


@pytest.fixture
def store() -> Iterator[Store]:
    with Store(":memory:") as opened:
        yield opened


def make_app(store: Store) -> TestClient:
    deps = DashboardDeps(
        source=StoreSource(None, store=store),
        risk=RISK,
        venue=VENUE,
        venue_name="sample_2step",
        commands=commands_factory(
            settings=SETTINGS,
            tv=TV,
            risk=RISK,
            venue=VENUE,
            venue_name="sample_2step",
            clock=lambda: NOW_MS,
            started_ms=NOW_MS,
        ),
        unavailable_reason=None,
        clock=lambda: NOW_MS,
        reports_dir=Path("reports"),
        port=PORT,
        csrf_token=CSRF,
    )
    return TestClient(create_app(deps), base_url=BASE)


@pytest.fixture
def client(store: Store) -> TestClient:
    return make_app(store)


def paused(store: Store) -> str | None:
    with store.transaction() as tx:
        return tx.get_state(StateKey.PAUSED)


def assert_secured(headers: object) -> None:
    for name, value in SECURITY_HEADERS.items():
        assert headers[name] == value, name  # type: ignore[index]


def post_pause(client: TestClient, **headers: str) -> object:
    return client.post(
        "/eylem/duraklat",
        data={CSRF_FIELD: CSRF, "onay": "1"},
        headers={"Origin": ORIGIN, **headers},
    )


# --- pure checks ------------------------------------------------------------------------------


def test_allowed_hosts_are_the_three_loopback_names_with_the_port() -> None:
    assert allowed_hosts(PORT) == {"127.0.0.1:8787", "localhost:8787", "[::1]:8787"}


@pytest.mark.parametrize(
    ("host", "ok"),
    [
        ("127.0.0.1:8787", True),
        ("localhost:8787", True),
        ("[::1]:8787", True),
        ("127.0.0.1", False),
        ("127.0.0.1:8788", False),
        ("evil.example:8787", False),
        ("localhost.:8787", False),
        ("LOCALHOST:8787", False),
        ("127.0.0.1:8787 ", False),
        ("", False),
        (None, False),
    ],
)
def test_host_allowed(host: str | None, ok: bool) -> None:
    assert host_allowed(host, PORT) is ok


@pytest.mark.parametrize(
    ("origin", "referer", "ok"),
    [
        (ORIGIN, None, True),
        ("http://localhost:8787", None, True),
        ("http://[::1]:8787", None, True),
        ("http://evil.example", None, False),
        ("https://127.0.0.1:8787", None, False),
        ("http://127.0.0.1:9999", None, False),
        ("null", None, False),
        (ORIGIN, "http://evil.example/x", True),  # Origin decides when present
        (None, None, True),  # the token alone decides
        (None, f"{BASE}/", True),
        (None, "http://evil.example/page", False),
        (None, "http://127.0.0.1:8787.evil.example/", False),
        (None, "not a url", False),
    ],
)
def test_origin_allowed(origin: str | None, referer: str | None, ok: bool) -> None:
    assert origin_allowed(origin, referer, PORT) is ok


def test_csrf_tokens_are_random_and_compared_exactly() -> None:
    first, second = new_csrf_token(), new_csrf_token()

    assert first != second
    assert len(first) >= 43
    assert csrf_matches(first, first)
    assert not csrf_matches(first, second)
    assert not csrf_matches(first, None)
    assert not csrf_matches(first, "")
    assert not csrf_matches("", "")
    assert not csrf_matches(first, first + "x")


@pytest.mark.parametrize(
    ("body", "expected"),
    [
        (b"a=1&b=x+y", {"a": "1", "b": "x y"}),
        (b"", {}),
        (b"a=1&a=2", None),  # repeated field
        (b"\xff=1", None),
        (b"a", None),
        (b"&".join(b"f%d=1" % index for index in range(50)), None),
    ],
)
def test_parse_form(body: bytes, expected: dict[str, str] | None) -> None:
    assert parse_form(body) == expected


# --- the app ----------------------------------------------------------------------------------


@pytest.mark.parametrize("path", GET_ROUTES)
def test_every_get_route_answers_with_every_security_header(client: TestClient, path: str) -> None:
    response = client.get(path)

    assert response.status_code == 200
    assert_secured(response.headers)
    assert response.headers["content-security-policy"] == CSP
    assert "set-cookie" not in response.headers


@pytest.mark.parametrize("path", ["/docs", "/redoc", "/openapi.json", "/nope", "/favicon.ico"])
def test_unknown_and_docs_routes_are_404_with_headers(client: TestClient, path: str) -> None:
    response = client.get(path)

    assert response.status_code == 404
    assert response.text == "Bu adreste bir sayfa yok."
    assert_secured(response.headers)


@pytest.mark.parametrize("path", ACTION_ROUTES)
def test_get_on_an_action_route_is_refused_and_changes_nothing(
    client: TestClient, store: Store, path: str
) -> None:
    response = client.get(path, params={CSRF_FIELD: CSRF, "onay": "1", "pin": PIN})

    assert response.status_code == 405
    assert_secured(response.headers)
    assert paused(store) is None


@pytest.mark.parametrize(
    "host", ["evil.example", "127.0.0.1:9999", "localhost", "192.168.1.5:8787"]
)
def test_foreign_host_is_refused_on_every_route(store: Store, host: str) -> None:
    client = make_app(store)
    for path in GET_ROUTES:
        response = client.get(path, headers={"Host": host})
        assert response.status_code == 421
        assert "Host başlığı" in response.text
        assert_secured(response.headers)
    response = client.post(
        "/eylem/duraklat",
        data={CSRF_FIELD: CSRF, "onay": "1"},
        headers={"Host": host, "Origin": f"http://{host}"},
    )
    assert response.status_code == 421
    assert paused(store) is None


def run_asgi(app: object, headers: list[tuple[bytes, bytes]]) -> list[Message]:
    sent: list[Message] = []
    scope: Scope = {
        "type": "http",
        "asgi": {"version": "3.0"},
        "http_version": "1.1",
        "method": "GET",
        "scheme": "http",
        "path": "/",
        "raw_path": b"/",
        "query_string": b"",
        "root_path": "",
        "headers": headers,
        "client": ("127.0.0.1", 5000),
        "server": ("127.0.0.1", PORT),
    }

    async def receive() -> Message:
        return {"type": "http.request", "body": b"", "more_body": False}

    async def send(message: Message) -> None:
        sent.append(message)

    async def main(asgi: object) -> None:
        assert callable(asgi)
        await asgi(scope, receive, send)

    asyncio.run(main(app))
    return sent


async def _never_called(scope: Scope, receive: Receive, send: Send) -> None:
    raise AssertionError("the app must not be reached")


@pytest.mark.parametrize(
    "headers",
    [[], [(b"host", b"127.0.0.1:8787"), (b"host", b"127.0.0.1:8787")]],
)
def test_missing_or_repeated_host_header_is_a_400(headers: list[tuple[bytes, bytes]]) -> None:
    sent = run_asgi(SecurityMiddleware(_never_called, port=PORT), headers)

    assert sent[0]["status"] == 400
    names = {name.decode() for name, _ in sent[0]["headers"]}
    assert set(SECURITY_HEADERS) <= names


async def _broken(scope: Scope, receive: Receive, send: Send) -> None:
    raise RuntimeError(f"boom {PIN}")


def test_an_exception_in_the_app_is_a_500_with_headers_and_no_detail() -> None:
    sent = run_asgi(SecurityMiddleware(_broken, port=PORT), [(b"host", b"127.0.0.1:8787")])

    assert sent[0]["status"] == 500
    assert set(SECURITY_HEADERS) <= {name.decode() for name, _ in sent[0]["headers"]}
    body = b"".join(message.get("body", b"") for message in sent[1:])
    assert PIN.encode() not in body


@pytest.mark.parametrize(
    "headers",
    [
        {"Origin": "http://evil.example"},
        {"Origin": "null"},
        {"Origin": "http://127.0.0.1:9999"},
        {"Referer": "http://evil.example/form.html"},
    ],
)
def test_foreign_origin_or_referer_is_refused(
    client: TestClient, store: Store, headers: dict[str, str]
) -> None:
    response = client.post("/eylem/duraklat", data={CSRF_FIELD: CSRF, "onay": "1"}, headers=headers)

    assert response.status_code == 403
    assert "başka bir siteden" in response.text
    assert_secured(response.headers)
    assert paused(store) is None


@pytest.mark.parametrize("token", [None, "", "wrong", CSRF + "x", CSRF.upper()])
def test_missing_or_wrong_csrf_token_is_refused(
    client: TestClient, store: Store, token: str | None
) -> None:
    data = {"onay": "1"} if token is None else {CSRF_FIELD: token, "onay": "1"}

    response = client.post("/eylem/duraklat", data=data, headers={"Origin": ORIGIN})

    assert response.status_code == 403
    assert "belirteci" in response.text
    assert paused(store) is None


def test_csrf_token_in_the_query_string_does_not_count(client: TestClient, store: Store) -> None:
    response = client.post(
        "/eylem/duraklat", params={CSRF_FIELD: CSRF}, data={"onay": "1"}, headers={"Origin": ORIGIN}
    )

    assert response.status_code == 403
    assert paused(store) is None


def test_oversized_body_is_refused_before_it_is_read(client: TestClient, store: Store) -> None:
    body = f"{CSRF_FIELD}={CSRF}&onay=1&pad=" + "x" * MAX_FORM_BYTES

    response = client.post(
        "/eylem/duraklat",
        content=body.encode(),
        headers={"Origin": ORIGIN, "Content-Type": "application/x-www-form-urlencoded"},
    )

    assert response.status_code == 413
    assert_secured(response.headers)
    assert paused(store) is None


def test_oversized_chunked_body_is_refused(client: TestClient, store: Store) -> None:
    def chunks() -> Iterator[bytes]:
        yield f"{CSRF_FIELD}={CSRF}&onay=1&pad=".encode()
        for _ in range(10):
            yield b"x" * 512

    response = client.post(
        "/eylem/duraklat",
        content=chunks(),
        headers={"Origin": ORIGIN, "Content-Type": "application/x-www-form-urlencoded"},
    )

    assert response.status_code == 413
    assert paused(store) is None


@pytest.mark.parametrize(
    "content_type", ["application/json", "multipart/form-data; boundary=x", "text/plain"]
)
def test_only_urlencoded_forms_are_accepted(
    client: TestClient, store: Store, content_type: str
) -> None:
    response = client.post(
        "/eylem/duraklat",
        content=f"{CSRF_FIELD}={CSRF}&onay=1".encode(),
        headers={"Origin": ORIGIN, "Content-Type": content_type},
    )

    assert response.status_code == 415
    assert paused(store) is None


def test_repeated_form_field_is_refused(client: TestClient, store: Store) -> None:
    response = client.post(
        "/eylem/duraklat",
        content=f"{CSRF_FIELD}={CSRF}&onay=1&onay=0".encode(),
        headers={"Origin": ORIGIN, "Content-Type": "application/x-www-form-urlencoded"},
    )

    assert response.status_code == 400
    assert paused(store) is None


def test_a_correct_post_from_the_panel_is_carried_out_with_headers(
    client: TestClient, store: Store
) -> None:
    response = post_pause(client)

    assert response.status_code == 200  # type: ignore[attr-defined]
    assert_secured(response.headers)  # type: ignore[attr-defined]
    assert paused(store) == "1"


def test_post_without_origin_and_referer_is_decided_by_the_token(
    client: TestClient, store: Store
) -> None:
    response = client.post("/eylem/duraklat", data={CSRF_FIELD: CSRF, "onay": "1"})

    assert response.status_code == 200
    assert paused(store) == "1"


def test_pin_never_appears_in_any_response(client: TestClient, store: Store) -> None:
    bodies = [
        client.post(
            "/eylem/kill", data={CSRF_FIELD: CSRF, "pin": pin}, headers={"Origin": ORIGIN}
        ).text
        for pin in ("135790", PIN, f"x{PIN}x")
    ]
    bodies += [client.get(path).text for path in GET_ROUTES]
    bodies.append(
        client.post(
            "/eylem/kill-kaldir", data={CSRF_FIELD: CSRF, "pin": PIN}, headers={"Origin": ORIGIN}
        ).text
    )

    assert all(PIN not in body for body in bodies)
    assert all("135790" not in body for body in bodies)


def test_query_parameters_cannot_change_the_action(client: TestClient, store: Store) -> None:
    response = client.post(
        "/eylem/duraklat",
        params={"kind": "kill", "pin": PIN},
        data={CSRF_FIELD: CSRF, "onay": "1"},
        headers={"Origin": ORIGIN},
    )

    assert response.status_code == 200
    assert paused(store) == "1"
    with store.transaction() as tx:
        assert tx.get_state(StateKey.KILLED) is None


def test_websocket_scopes_are_closed_without_reaching_the_app() -> None:
    sent: list[Message] = []

    async def receive() -> Message:
        return {"type": "websocket.connect"}

    async def send(message: Message) -> None:
        sent.append(message)

    scope: Scope = {"type": "websocket", "headers": [(b"host", b"evil.example")], "path": "/"}
    asyncio.run(SecurityMiddleware(_never_called, port=PORT)(scope, receive, send))

    assert sent == [{"type": "websocket.close", "code": 1008}]


def test_the_referrer_policy_lets_the_panels_own_forms_keep_their_origin() -> None:
    # Found in a real browser (Chromium): under "no-referrer" a same-origin form post is
    # sent with `Origin: null`, which `origin_allowed` rightly treats as foreign, so every
    # panel action was refused with 403. "same-origin" keeps the real Origin for the
    # panel's own requests and still sends no referrer to any other site.
    assert SECURITY_HEADERS["referrer-policy"] == "same-origin"
    assert not origin_allowed("null", None, 8787)
    assert origin_allowed("http://127.0.0.1:8787", None, 8787)
