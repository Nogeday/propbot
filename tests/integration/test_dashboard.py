"""The panel end to end: real uvicorn on an ephemeral loopback port, real file database.

Walks the main flows over a real socket (page, CSRF token, pause, KILL with PIN, lifting
it, balance entry, notices in the outbox) and checks the read-only start (no database,
no secrets) through the same app.
"""

import datetime as dt
import http.client
import json
import logging
import re
import threading
from collections.abc import Iterator
from pathlib import Path
from urllib.parse import urlencode

import pytest
from fastapi.testclient import TestClient
from structlog.testing import capture_logs

from propbot.dashboard.actions import commands_factory
from propbot.dashboard.app import DashboardDeps, create_app
from propbot.dashboard.command import build_server, serve
from propbot.dashboard.live import StoreSource
from propbot.risk.loader import load_risk_config, load_venue_profile
from propbot.settings import Settings
from propbot.signals.service import bind_listener
from propbot.signals.tvconfig import load_tv_alerts_config
from propbot.store.db import OutboxKind, StateKey, Store
from support import tg
from support.alerts import BAR_TIME_MS, TOKEN
from support.paths import CONFIG_FIXTURES, FIXTURES

PIN = "246813"
NOW_MS = BAR_TIME_MS + 61_500
TODAY = dt.date(2026, 7, 21)
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
_CSRF_INPUT = re.compile(r'name="csrf" value="([^"]+)"')


def deps_for(source: StoreSource, port: int, *, secrets: bool = True) -> DashboardDeps:
    factory = commands_factory(
        settings=SETTINGS,
        tv=TV,
        risk=RISK,
        venue=VENUE,
        venue_name="sample_2step",
        clock=lambda: NOW_MS,
        started_ms=NOW_MS,
    )
    return DashboardDeps(
        source=source,
        risk=RISK,
        venue=VENUE,
        venue_name="sample_2step",
        commands=factory if secrets else None,
        unavailable_reason=None if secrets else "sırlar okunamadı (PROPBOT_PIN: tanımlı değil)",
        clock=lambda: NOW_MS,
        reports_dir=Path("reports"),
        port=port,
        csrf_token="integration-csrf-token_0123456789abcdefghijk",
    )


class Panel:
    """A running panel and a plain HTTP client for it."""

    def __init__(self, db_path: Path) -> None:
        self.listener = bind_listener("127.0.0.1", 0)
        self.port: int = self.listener.getsockname()[1]
        self.source = StoreSource(db_path)
        self.server = build_server(create_app(deps_for(self.source, self.port)))
        self.thread = threading.Thread(target=serve, args=(self.server, self.listener), daemon=True)
        self.thread.start()

    def request(
        self, method: str, path: str, *, form: dict[str, str] | None = None, host: str | None = None
    ) -> tuple[int, dict[str, str], str]:
        # The listener is already listening: a request made before uvicorn accepts waits in
        # the backlog, so no polling for readiness is needed.
        conn = http.client.HTTPConnection("127.0.0.1", self.port, timeout=10)
        try:
            headers = {"Host": host or f"127.0.0.1:{self.port}"}
            body = None
            if form is not None:
                body = urlencode(form)
                headers["Content-Type"] = "application/x-www-form-urlencoded"
                headers["Origin"] = f"http://127.0.0.1:{self.port}"
            conn.request(method, path, body=body, headers=headers)
            response = conn.getresponse()
            return (
                response.status,
                {name.lower(): value for name, value in response.getheaders()},
                response.read().decode("utf-8"),
            )
        finally:
            conn.close()

    def stop(self) -> None:
        self.server.should_exit = True
        self.thread.join(timeout=15)
        self.listener.close()
        self.source.close()


@pytest.fixture
def db_path(tmp_path: Path) -> Path:
    path = tmp_path / "propbot.db"
    with Store(path) as store:  # `serve` created it
        tg.forwarded_entry(store, prop_day=TODAY, now_ms=NOW_MS, symbol='<b>"MES"</b>')
        with store.transaction() as tx:
            tx.set_state(StateKey.LAST_ALIVE_MS, str(NOW_MS - 20_000), NOW_MS)
    return path


@pytest.fixture
def panel(db_path: Path) -> Iterator[Panel]:
    running = Panel(db_path)
    yield running
    running.stop()
    assert not running.thread.is_alive()


def state(db_path: Path) -> dict[str, str | None]:
    with Store(db_path) as store, store.transaction() as tx:
        return {key.value: tx.get_state(key) for key in StateKey}


def test_main_flows_over_a_real_socket(
    panel: Panel, db_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    caplog.set_level(logging.DEBUG)
    with capture_logs() as logs:
        status, headers, page = panel.request("GET", "/")
        assert status == 200
        assert headers["content-security-policy"].startswith("default-src 'none'")
        assert headers["x-frame-options"] == "DENY"
        assert "&lt;b&gt;&quot;MES&quot;&lt;/b&gt;" in page
        assert "<b>" not in page
        match = _CSRF_INPUT.search(page)
        assert match is not None
        csrf = match.group(1)

        assert panel.request("GET", "/", host="evil.example")[0] == 421
        assert panel.request("GET", "/", host=f"rebind.example:{panel.port}")[0] == 421
        assert panel.request("GET", "/eylem/duraklat")[0] == 405
        assert panel.request("POST", "/eylem/duraklat", form={"onay": "1"})[0] == 403

        status, _, body = panel.request("POST", "/eylem/duraklat", form={"csrf": csrf, "onay": "1"})
        assert status == 200
        assert "Duraklatıldı" in body
        assert "servis çalışıyor" in body
        assert state(db_path)["paused"] == "1"

        status, _, body = panel.request("POST", "/eylem/kill", form={"csrf": csrf, "pin": PIN})
        assert status == 200
        assert "KILL etkin" in body
        assert state(db_path)["killed"] == "1"

        status, _, body = panel.request("GET", "/parca/canli")
        assert status == 200
        assert "KILL" in body
        assert "<form" not in body

        status, _, body = panel.request(
            "POST", "/eylem/kill-kaldir", form={"csrf": csrf, "pin": PIN}
        )
        assert "KILL kaldırıldı" in body
        flags = state(db_path)
        assert flags["killed"] is None
        assert flags["paused"] is None

        status, _, body = panel.request(
            "POST",
            "/eylem/bakiye",
            form={"csrf": csrf, "bakiye": "104000", "gun_basi": "104500", "onay": "1"},
        )
        assert status == 200
        status, _, live = panel.request("GET", "/api/live")
        assert json.loads(live)["account"]["manual"] is True

        responses = [page, body, live]

    with Store(db_path) as store, store.transaction() as tx:
        rows = tx.messages_created_between(0, 2**62)
    panel_notices = [row.text for row in rows if row.kind is OutboxKind.NOTICE]
    assert len(panel_notices) == 4
    assert all(text.startswith("[PANEL] ") for text in panel_notices)
    leaks = [*responses, *panel_notices, *(repr(entry) for entry in logs)]
    leaks += [record.getMessage() for record in caplog.records]
    assert all(PIN not in text for text in leaks)


def test_read_only_start_without_database_and_secrets(tmp_path: Path) -> None:
    db_path = tmp_path / "propbot.db"
    source = StoreSource(db_path)
    client = TestClient(
        create_app(deps_for(source, 8787, secrets=False)), base_url="http://localhost:8787"
    )
    page = client.get("/").text
    csrf = _CSRF_INPUT.search(page)
    assert csrf is not None

    response = client.post(
        "/eylem/kill",
        data={"csrf": csrf.group(1), "pin": PIN},
        headers={"Origin": "http://localhost:8787"},
    )

    assert "veritabanı yok" in page
    assert "Panel salt-okunur: sırlar okunamadı" in page
    assert page.count("<fieldset disabled>") == 6
    assert response.status_code == 409
    assert PIN not in response.text
    assert client.get("/analiz").status_code == 200
    assert not db_path.exists()
    source.close()


def test_panel_picks_up_a_database_created_after_it_started(tmp_path: Path) -> None:
    db_path = tmp_path / "propbot.db"
    source = StoreSource(db_path)
    client = TestClient(create_app(deps_for(source, 8787)), base_url="http://[::1]:8787")

    before = client.get("/api/live").json()
    Store(db_path).close()
    after = client.get("/api/live").json()
    source.close()

    assert before["db"]["state"] == "missing"
    assert after["db"]["state"] == "ok"
