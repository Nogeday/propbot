"""The whole Faz 1 path in one process: webhook in, Telegram out (SPEC §5, ADR-0004).

The real pieces run together: FastAPI app with its lifespan, receiver, SQLite store,
processor, alert-line monitor and the python-telegram-bot application. Only the two ends
are replaced: Starlette's test client stands in for TradingView (no socket) and an
offline `BaseRequest` fake stands in for Telegram's HTTP API.
"""

import dataclasses
import datetime as dt
import json
import time
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from pathlib import Path

import pytest
import structlog
import uvicorn
from fastapi.testclient import TestClient
from structlog.testing import capture_logs

from propbot.risk import load_config_dir
from propbot.settings import Settings
from propbot.signals.receiver import WEBHOOK_PATH
from propbot.signals.service import (
    Service,
    ServiceError,
    ServiceParts,
    is_loopback_host,
    run_service,
    uvicorn_config,
)
from propbot.signals.tvconfig import load_tv_alerts_config
from propbot.store.db import OutboxStatus, Outcome, SignalStatus, StateKey, Store
from support import alerts, tg
from support.paths import CONFIG_FIXTURES, FIXTURES

TV_IP = "52.89.214.238"
STRANGER = "203.0.113.9"
PIN = "246810"
BAR_CLOSE_MS = alerts.BAR_TIME_MS + 60_000
NOW_MS = BAR_CLOSE_MS + 1_500  # 1.5 s after the bar closed: fresh
WAIT_SECONDS = 10.0

_CONFIG = load_config_dir(CONFIG_FIXTURES / "valid")
RISK = _CONFIG.risk
# Rules verified shortly before the test date, so entries carry no "unverified rules" banner.
VENUE = _CONFIG.venues["sample_2step"].model_copy(
    update={"rules_verified_at": dt.date(2026, 7, 10)}
)
TV = load_tv_alerts_config(FIXTURES / "tv_alerts" / "valid" / "full.yaml")


class FakeClock:
    def __init__(self, now_ms: int = NOW_MS) -> None:
        self.now_ms = now_ms

    def __call__(self) -> int:
        return self.now_ms


def settings() -> Settings:
    return Settings(
        telegram_bot_token=tg.BOT_TOKEN,
        telegram_allowed_chat_id=tg.CHAT_ID,
        tv_webhook_token=alerts.TOKEN,
        propbot_pin=PIN,
    )


def wait_until(condition: Callable[[], bool], what: str) -> None:
    deadline = time.monotonic() + WAIT_SECONDS
    while not condition():
        if time.monotonic() > deadline:
            raise AssertionError(f"timed out waiting for: {what}")
        time.sleep(0.01)


@dataclasses.dataclass
class Running:
    service: Service
    client: TestClient
    telegram: tg.FakeRequest
    store: Store
    clock: FakeClock
    failures: list[int]

    def post(self, body: bytes) -> dict[str, str]:
        response = self.client.post(WEBHOOK_PATH, content=body)
        assert response.status_code == 200, response.text
        answer: dict[str, str] = response.json()
        return answer

    def say(self, text: str) -> None:
        """Deliver a Telegram message from the whitelisted user through the polling path."""
        self.push(tg.private_update(text, sent_at_ms=self.clock.now_ms))

    def push(self, update: dict[str, object]) -> None:
        portal = self.client.portal
        assert portal is not None
        portal.call(self.telegram.push_update, update)

    def texts(self) -> list[str]:
        return self.telegram.sent_texts()

    def wait_for_text(self, fragment: str) -> str:
        wait_until(lambda: any(fragment in text for text in self.texts()), repr(fragment))
        return next(text for text in self.texts() if fragment in text)

    def wait_for_outcome(self, signal_id: int, outcome: Outcome) -> None:
        def done() -> bool:
            with self.store.transaction() as tx:
                row = tx.get_signal(signal_id)
            return row is not None and row.outcome is outcome

        wait_until(done, f"signal {signal_id} -> {outcome.value}")

    def state(self, key: StateKey) -> str | None:
        with self.store.transaction() as tx:
            return tx.get_state(key)


@pytest.fixture(autouse=True)
def default_structlog() -> None:
    """Start from structlog's defaults so `capture_logs` sees every event of the test."""
    structlog.reset_defaults()


@contextmanager
def running(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    *,
    telegram: tg.FakeRequest | None = None,
    prepare: Callable[[Store], None] | None = None,
) -> Iterator[Running]:
    """Start the service on a fresh database file and stop it cleanly afterwards."""
    monkeypatch.setattr("propbot.notify.telegram._IDLE_POLL_SECONDS", 0.02)
    fake = telegram or tg.FakeRequest()
    clock = FakeClock()
    failures: list[int] = []
    with Store(tmp_path / "propbot.db") as store:
        if prepare is not None:
            prepare(store)
        service = Service(
            ServiceParts(
                settings=settings(),
                risk=RISK,
                venue=VENUE,
                venue_name="sample_2step",
                tv=TV,
                store=store,
                clock=clock,
                telegram_request=fake,
                telegram_updates_request=fake,
            ),
            on_failure=lambda: failures.append(1),
        )
        with TestClient(service.app, client=(TV_IP, 50000)) as client:
            yield Running(service, client, fake, store, clock, failures)


# --- the path ------------------------------------------------------------------------------


def test_an_entry_alert_becomes_one_telegram_message(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    with running(tmp_path, monkeypatch) as bot:
        assert bot.post(alerts.alert_bytes()) == {"status": "queued"}
        message = bot.wait_for_text("[SİNYAL]")
        assert bot.post(alerts.alert_bytes()) == {"status": "duplicate"}
        bot.say("/durum")
        status = bot.wait_for_text("signal")

        lines = message.splitlines()
        assert lines[0] == f"[SİNYAL] NY ORB FVG · FVG · LONG · {alerts.SYMBOL}"
        assert "SAMPLEFIRM US500.sample'te PİYASA emri; SL/TP'yi emirle BİRLİKTE gir" in message
        assert "risk ≈ $250 (%0.25) · günlük bütçe $250/$1000 (bu sinyal dahil)" in message
        assert lines[-1].startswith("id 1 · bar ")
        assert "KURALLAR DOĞRULANMADI" not in message
        assert "KILL" not in status or "aktif" in status
        with bot.store.transaction() as tx:
            row = tx.get_signal(1)
            used = tx.budget_used(dt.date(2026, 7, 21))
            counts = tx.outbox_counts()
        assert row is not None
        assert (row.status, row.outcome) == (SignalStatus.DONE, Outcome.FORWARDED)
        assert used == 250.0
        assert counts[OutboxStatus.PENDING] == 0
        assert alerts.TOKEN not in row.payload_json
    # One signal, one message: the duplicate produced nothing.
    assert sum("[SİNYAL]" in text for text in bot.texts()) == 1
    assert bot.telegram.chat_ids() == {tg.CHAT_ID}
    assert bot.service.failure is None
    assert bot.failures == []


def test_a_trade_is_followed_from_entry_to_exit(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    with running(tmp_path, monkeypatch) as bot:
        bot.post(json.dumps(alerts.ny_orb_fvg_entry("long")).encode())
        bot.wait_for_text("[SİNYAL]")
        for build, marker in (
            (alerts.ny_orb_fvg_stop_update, "[STOP GÜNCELLE]"),
            (alerts.ny_orb_fvg_exit, "[ÇIKIŞ]"),
        ):
            payload = build("long")
            bar_time = payload["bar_time"]
            assert isinstance(bar_time, int)
            bot.clock.now_ms = bar_time + 60_000 + 1_000
            assert bot.post(json.dumps(payload).encode()) == {"status": "queued"}
            bot.wait_for_text(marker)

    order = [text.split("]")[0] + "]" for text in bot.texts()]
    assert order == ["[SİNYAL]", "[STOP GÜNCELLE]", "[ÇIKIŞ]"]
    assert "(giriş id 1)" in bot.texts()[1]
    assert "PİYASADAN KAPAT" in bot.texts()[2]


def test_signals_queued_before_a_restart_are_processed_on_start(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    def leave_a_queued_signal(store: Store) -> None:
        tg.add_signal(store, outcome=None)

    with running(tmp_path, monkeypatch, prepare=leave_a_queued_signal) as bot:
        bot.wait_for_text("[SİNYAL]")
        with bot.store.transaction() as tx:
            assert tx.queue_depth() == 0


def test_a_late_entry_is_reported_as_stale_and_not_counted(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    with running(tmp_path, monkeypatch) as bot:
        bot.clock.now_ms = BAR_CLOSE_MS + 5 * 60_000

        assert bot.post(alerts.alert_bytes()) == {"status": "stale"}
        message = bot.wait_for_text("[BAYAT SİNYAL — ATLA]")

        assert "[SİNYAL]" not in message
        with bot.store.transaction() as tx:
            assert tx.budget_used(dt.date(2026, 7, 21)) == 0.0


# --- commands through the real polling path -------------------------------------------------


def test_kill_stops_entries_but_not_exits(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    with running(tmp_path, monkeypatch) as bot:
        bot.post(alerts.alert_bytes())
        bot.wait_for_text("[SİNYAL]")
        bot.say(f"/kill {PIN}")
        wait_until(lambda: bot.state(StateKey.KILLED) == "1", "KILLED flag")
        wait_until(lambda: bool(bot.telegram.calls_to("deleteMessage")), "PIN message deleted")

        second_entry = alerts.alert_payload(strategy_id="asian_sweep", setup="SWEEP_FVG")
        bot.post(json.dumps(second_entry).encode())
        bot.wait_for_outcome(2, Outcome.SUPPRESSED_KILLED)
        exit_payload = alerts.ny_orb_fvg_exit("long")
        bar_time = exit_payload["bar_time"]
        assert isinstance(bar_time, int)
        bot.clock.now_ms = bar_time + 60_000 + 1_000
        bot.post(json.dumps(exit_payload).encode())
        bot.wait_for_text("[ÇIKIŞ]")

    assert sum("[SİNYAL]" in text for text in bot.texts()) == 1
    assert all(PIN not in text for text in bot.texts())


def test_other_chats_get_no_answer(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    with running(tmp_path, monkeypatch) as bot:
        bot.push(tg.other_user_update("/durum", sent_at_ms=NOW_MS))
        bot.push(tg.group_update(f"/kill {PIN}", sent_at_ms=NOW_MS, chat_id=tg.CHAT_ID))
        bot.say("/saglik")
        bot.wait_for_text("TR")

        assert bot.state(StateKey.KILLED) is None

    assert bot.telegram.chat_ids() == {tg.CHAT_ID}
    assert len(bot.texts()) == 1  # only the answer to the whitelisted user's /saglik


def test_strangers_reach_neither_the_queue_nor_telegram(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    with running(tmp_path, monkeypatch) as bot:
        # No `with`: entering a second client would run the app's lifespan a second time.
        outsider = TestClient(bot.service.app, client=(STRANGER, 40000))
        answer = outsider.post(WEBHOOK_PATH, content=alerts.alert_bytes())
        bot.say("/durum")
        bot.wait_for_text("signal")

        assert answer.status_code == 403
        with bot.store.transaction() as tx:
            assert tx.signals_received_between(0, 2**62) == []
    assert all("[SİNYAL]" not in text for text in bot.texts())


# --- supervision ---------------------------------------------------------------------------


def test_a_dying_task_is_reported_as_a_failure_without_leaking_the_token(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    rejected = tg.FakeRequest()
    rejected.script("getMe", tg.error_response(401, "Unauthorized"))

    with capture_logs() as logs, running(tmp_path, monkeypatch, telegram=rejected) as bot:
        wait_until(lambda: bot.service.failure is not None, "task failure")
        wait_until(lambda: bot.failures == [1], "on_failure callback")
        # The receiver still stores alerts while the process is on its way down.
        assert bot.post(alerts.alert_bytes()) == {"status": "queued"}

    assert bot.service.failure == "telegram"
    died = [entry for entry in logs if entry["event"] == "service_task_died"]
    assert [(entry["task"], entry["error_type"]) for entry in died] == [
        ("telegram", "TelegramFatalError")
    ]
    assert tg.BOT_TOKEN not in json.dumps(logs, default=str)


def test_a_clean_stop_is_not_a_failure(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    with capture_logs() as logs, running(tmp_path, monkeypatch) as bot:
        bot.say("/durum")
        bot.wait_for_text("signal")

    assert bot.service.failure is None
    assert bot.failures == []
    events = [entry["event"] for entry in logs]
    assert "service_started" in events
    assert events[-1] == "service_stopped"
    stopped = logs[-1]
    assert stopped["cancelled"] == []  # every task ended by itself within the grace period


# --- server configuration ------------------------------------------------------------------


@pytest.mark.parametrize("host", ["127.0.0.1", "::1", "127.0.0.53"])
def test_loopback_hosts_are_accepted(host: str) -> None:
    assert is_loopback_host(host)


@pytest.mark.parametrize(
    "host", ["0.0.0.0", "::", "192.168.1.10", "10.0.0.5", "localhost", "example.org", "", "[::1]"]
)
def test_every_other_host_is_refused(host: str) -> None:
    assert not is_loopback_host(host)


def test_run_service_refuses_to_listen_on_a_public_interface(tmp_path: Path) -> None:
    with Store(tmp_path / "propbot.db") as store:
        parts = ServiceParts(
            settings=settings(),
            risk=RISK,
            venue=VENUE,
            venue_name="sample_2step",
            tv=TV,
            store=store,
        )

        with pytest.raises(ServiceError, match="yalnızca loopback"):
            run_service(parts, host="0.0.0.0", port=8000)


def test_uvicorn_leaves_the_peer_address_and_the_logging_alone() -> None:
    async def app(_scope: object, _receive: object, _send: object) -> None:
        return None

    config = uvicorn_config(app, host="127.0.0.1", port=8123)

    assert isinstance(config, uvicorn.Config)
    assert (config.host, config.port) == ("127.0.0.1", 8123)
    # uvicorn would otherwise rewrite the peer from X-Forwarded-For when it is 127.0.0.1.
    assert config.proxy_headers is False
    assert config.server_header is False
    assert config.access_log is False
    assert config.log_config is None
    assert config.lifespan == "on"


def test_secrets_are_not_part_of_the_parts_repr(tmp_path: Path) -> None:
    with Store(tmp_path / "propbot.db") as store:
        parts = ServiceParts(
            settings=settings(),
            risk=RISK,
            venue=VENUE,
            venue_name="sample_2step",
            tv=TV,
            store=store,
        )

    text = repr(parts)
    assert tg.BOT_TOKEN not in text
    assert alerts.TOKEN not in text
    assert PIN not in text
