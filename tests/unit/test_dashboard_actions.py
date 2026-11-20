"""Panel actions: the same state as the Telegram commands, notices in the outbox, validation."""

import datetime as dt
import logging
from collections.abc import Iterator
from pathlib import Path

import pytest
from structlog.testing import capture_logs

from propbot.dashboard.actions import (
    MAX_SUMMARY_CHARS,
    NOT_KILLED_TEXT,
    ActionDeps,
    ActionKind,
    ActionResult,
    Actions,
    commands_factory,
    control_state,
    parse_amount,
    strip_process_lines,
)
from propbot.dashboard.live import (
    DB_OK,
    DbState,
    DbStatus,
    StoreSource,
    parse_snapshot,
)
from propbot.notify.commands import PIN_LOCK_MS, PIN_MAX_FAILURES, CommandDeps, Commands
from propbot.risk.loader import load_risk_config, load_venue_profile
from propbot.settings import Settings
from propbot.signals.tvconfig import load_tv_alerts_config
from propbot.store.db import STANDBY_ON, OutboxKind, OutboxRow, StateKey, Store
from support import tg
from support.alerts import BAR_TIME_MS, TOKEN
from support.paths import CONFIG_FIXTURES, FIXTURES

PIN = "246813"
WRONG_PIN = "135790"
MINUTE_MS = 60_000
NOW_MS = BAR_TIME_MS + MINUTE_MS + 1_500  # 2026-07-21 15:31:01.5 UTC = 18:31 TR
STARTED_MS = NOW_MS - 60 * MINUTE_MS
VENUE_NAME = "sample_2step"
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
CONFIRMED = {"onay": "1"}


class FakeClock:
    def __init__(self, now_ms: int = NOW_MS) -> None:
        self.now_ms = now_ms

    def __call__(self) -> int:
        return self.now_ms


class World:
    """The panel's actions on one store, and Telegram's commands on a twin store."""

    def __init__(self) -> None:
        self.clock = FakeClock()
        self.store = Store(":memory:")
        self.twin = Store(":memory:")
        factory = commands_factory(
            settings=SETTINGS,
            tv=TV,
            risk=RISK,
            venue=VENUE,
            venue_name=VENUE_NAME,
            clock=self.clock,
            started_ms=STARTED_MS,
        )
        self.actions = Actions(
            ActionDeps(
                source=StoreSource(None, store=self.store),
                commands=factory,
                unavailable_reason="",
                venue=VENUE,
                clock=self.clock,
            )
        )
        self.telegram = Commands(
            CommandDeps(
                store=self.twin,
                risk=RISK,
                venue=VENUE,
                venue_name=VENUE_NAME,
                tv=TV,
                pin=PIN,
                clock=self.clock,
                started_ms=STARTED_MS,
                reject_counts=dict,
            )
        )
        # The telegram side of the shared lock test: Commands on the PANEL's store.
        self.shared = Commands(
            CommandDeps(
                store=self.store,
                risk=RISK,
                venue=VENUE,
                venue_name=VENUE_NAME,
                tv=TV,
                pin=PIN,
                clock=self.clock,
                started_ms=STARTED_MS,
                reject_counts=dict,
            )
        )

    def act(self, kind: ActionKind, **form: str) -> ActionResult:
        return self.actions.run(kind, form)

    def say(self, text: str) -> str:
        return self.telegram.handle(text, sent_at_ms=self.clock.now_ms).text

    def close(self) -> None:
        self.store.close()
        self.twin.close()


def flags(store: Store) -> dict[str, str | None]:
    """The control state: everything but the panel's own snapshot."""
    with store.transaction() as tx:
        return {
            key.value: tx.get_state(key) for key in StateKey if key is not StateKey.MANUAL_ACCOUNT
        }


def notices(store: Store) -> list[OutboxRow]:
    with store.transaction() as tx:
        rows = tx.messages_created_between(0, 2**62)
    return [row for row in rows if row.kind is OutboxKind.NOTICE]


@pytest.fixture
def world() -> Iterator[World]:
    current = World()
    yield current
    current.close()


# --- same state as the Telegram commands ----------------------------------------------------


@pytest.mark.parametrize(
    ("steps", "commands"),
    [
        ([(ActionKind.PAUSE, CONFIRMED)], ["/duraklat"]),
        ([(ActionKind.PAUSE, CONFIRMED), (ActionKind.RESUME, CONFIRMED)], ["/duraklat", "/devam"]),
        ([(ActionKind.KILL, {"pin": PIN})], [f"/kill {PIN}"]),
        (
            [(ActionKind.KILL, {"pin": PIN}), (ActionKind.UNKILL, {"pin": PIN})],
            [f"/kill {PIN}", f"/devam {PIN}"],
        ),
        (
            [(ActionKind.PAUSE, CONFIRMED), (ActionKind.KILL, {"pin": PIN})],
            ["/duraklat", f"/kill {PIN}"],
        ),
        ([(ActionKind.KILL, {"pin": WRONG_PIN})], [f"/kill {WRONG_PIN}"]),
        (
            [(ActionKind.KILL, {"pin": PIN}), (ActionKind.RESUME, CONFIRMED)],
            [f"/kill {PIN}", "/devam"],
        ),
    ],
)
def test_actions_leave_the_same_state_and_reply_as_the_telegram_commands(
    world: World, steps: list[tuple[ActionKind, dict[str, str]]], commands: list[str]
) -> None:
    replies = [world.act(kind, **form) for kind, form in steps]
    telegram = [world.say(text) for text in commands]

    assert flags(world.store) == flags(world.twin)
    assert [result.reply for result in replies] == telegram
    # Done only when the flags say so: a wrong PIN, or /devam while KILL holds, is refused.
    refused = {(ActionKind.KILL, WRONG_PIN), (ActionKind.RESUME, "after-kill")}
    for index, ((kind, form), result) in enumerate(zip(steps, replies, strict=True)):
        after_kill = any(step[0] is ActionKind.KILL for step in steps[:index])
        key = (kind, form.get("pin", "after-kill" if after_kill else ""))
        assert result.status == (409 if key in refused else 200), (kind, result.reply)


def test_pause_and_resume_need_the_local_confirmation(world: World) -> None:
    refused = world.act(ActionKind.PAUSE)
    other = world.act(ActionKind.PAUSE, onay="yes")

    assert refused.status == other.status == 400
    assert "onay" in refused.reply.lower()
    assert flags(world.store)["paused"] is None
    assert notices(world.store) == []


def test_lifting_a_kill_that_is_not_set_changes_nothing_and_checks_no_pin(world: World) -> None:
    world.act(ActionKind.PAUSE, **CONFIRMED)

    result = world.act(ActionKind.UNKILL, pin=WRONG_PIN)

    assert result.status == 409
    assert result.reply == NOT_KILLED_TEXT
    # `/devam <PIN>` without a KILL would have lifted the pause; the panel does not.
    assert flags(world.store)["paused"] == "1"
    assert flags(world.store)["pin_fail_count"] is None


def test_wrong_pin_five_times_locks_pin_entry_for_telegram_too(world: World) -> None:
    results = [world.act(ActionKind.KILL, pin=WRONG_PIN) for _ in range(PIN_MAX_FAILURES)]

    assert "Kalan deneme: 4" in results[0].reply
    assert "PIN girişi kilitli" in results[-1].reply
    # Telegram, on the same store, is locked now: even the right PIN does not pass.
    reply = world.shared.handle(f"/kill {PIN}", sent_at_ms=world.clock.now_ms).text
    assert "PIN girişi kilitli" in reply
    assert flags(world.store)["killed"] is None
    # And the panel itself refuses the right PIN while the lock lasts.
    assert "PIN girişi kilitli" in world.act(ActionKind.KILL, pin=PIN).reply
    world.clock.now_ms += PIN_LOCK_MS + 1
    assert world.act(ActionKind.KILL, pin=PIN).reply.startswith("KILL etkin")


def test_a_lock_set_from_telegram_also_stops_the_panel(world: World) -> None:
    for _ in range(PIN_MAX_FAILURES):
        world.shared.handle(f"/kill {WRONG_PIN}", sent_at_ms=world.clock.now_ms)

    result = world.act(ActionKind.UNKILL, pin=PIN)

    assert result.status == 409  # nothing to lift: KILL never got set
    with world.store.transaction() as tx:
        tx.set_state(StateKey.KILLED, "1", NOW_MS)
    assert "PIN girişi kilitli" in world.act(ActionKind.UNKILL, pin=PIN).reply


def test_pin_lock_state_disables_the_pin_controls() -> None:
    state = control_state(unavailable_reason=None, db=DB_OK, pin_locked_until_ms=NOW_MS + 60_000)

    assert state.enabled
    assert not state.pin_enabled
    assert "18:32" in state.pin_reason


@pytest.mark.parametrize(
    ("kind", "form", "carried_out"),
    [
        (ActionKind.KILL, {"pin": PIN}, True),
        (ActionKind.PAUSE, CONFIRMED, True),
        (ActionKind.RESUME, CONFIRMED, False),
        (ActionKind.SUMMARY, {}, False),
    ],
)
def test_on_a_standby_copy_only_kill_and_pause_are_carried_out(
    world: World, kind: ActionKind, form: dict[str, str], carried_out: bool
) -> None:
    for store in (world.store, world.twin):
        with store.transaction() as tx:
            tx.set_state(StateKey.STANDBY, STANDBY_ON, NOW_MS)
            tx.set_state(StateKey.PAUSED, "1", NOW_MS)
    command = {
        ActionKind.KILL: f"/kill {PIN}",
        ActionKind.PAUSE: "/duraklat",
        ActionKind.RESUME: "/devam",
        ActionKind.SUMMARY: "/durum",
    }[kind]

    result = world.act(kind, **form)
    telegram = world.telegram.handle(command, sent_at_ms=NOW_MS, standby=True).text

    assert flags(world.store) == flags(world.twin)
    if kind is not ActionKind.SUMMARY:
        assert result.reply == telegram
    if carried_out:
        assert "[BEKLEMEDE]" in result.reply
    else:
        assert "UYGULANMADI" in result.reply


# --- notices ---------------------------------------------------------------------------------


def test_every_action_writes_a_panel_notice_without_the_pin(world: World) -> None:
    world.act(ActionKind.PAUSE, **CONFIRMED)
    world.act(ActionKind.KILL, pin=PIN)
    world.act(ActionKind.UNKILL, pin=WRONG_PIN)

    rows = notices(world.store)
    assert len(rows) == 3
    assert all(row.text.startswith("[PANEL] 2026-07-21 18:31:01 TR · ") for row in rows)
    assert "Duraklat\nDuraklatıldı" in rows[0].text
    assert "Acil Kapat (KILL)\nKILL etkin" in rows[1].text
    assert "PIN hatalı" in rows[2].text
    # A notice never expires: it waits for `serve` however long it is down.
    assert all(row.expires_at_ms is None for row in rows)
    assert all(PIN not in row.text and WRONG_PIN not in row.text for row in rows)


def test_result_says_whether_the_service_will_deliver_the_notice(world: World) -> None:
    down = world.act(ActionKind.PAUSE, **CONFIRMED)
    with world.store.transaction() as tx:
        tx.set_state(StateKey.LAST_ALIVE_MS, str(NOW_MS - 30_000), NOW_MS)
    up = world.act(ActionKind.PAUSE, **CONFIRMED)

    assert "servis ÇALIŞMIYOR" in down.notice
    assert "süresi dolmaz" in down.notice
    assert "servis çalışıyor" in up.notice


def test_summary_notice_carries_status_and_health_without_process_lines(world: World) -> None:
    result = world.act(ActionKind.SUMMARY)

    rows = notices(world.store)
    assert result.status == 200
    assert len(rows) == 1
    text = rows[0].text
    assert text.startswith("[PANEL] 2026-07-21 18:31:01 TR · Durum/Test özeti (panelden istendi)")
    assert "[DURUM]" in text
    assert "[SAĞLIK]" in text
    for line in ("Çalışma süresi", "Reddedilen webhook", "Yetkisiz sohbet", "Saat farkı"):
        assert line not in text
    assert len(text) <= MAX_SUMMARY_CHARS + 100


def test_strip_process_lines_keeps_everything_else() -> None:
    text = "[DURUM] x\nÇalışma süresi: 1 sa\nVeritabanı: tamam\nSaat farkı (...): +0.0 sn"

    assert strip_process_lines(text) == "[DURUM] x\nVeritabanı: tamam"


# --- manual balance ---------------------------------------------------------------------------


def test_balance_snapshot_and_its_notice_are_stored_together(world: World) -> None:
    result = world.act(
        ActionKind.BALANCE, bakiye="104000,50", ozsermaye="103500", gun_basi="104500", onay="1"
    )

    assert result.status == 200
    with world.store.transaction() as tx:
        text = tx.get_state(StateKey.MANUAL_ACCOUNT)
    assert text is not None
    snapshot = parse_snapshot(text)
    assert (snapshot.balance, snapshot.equity, snapshot.day_start_balance) == (
        104000.5,
        103500.0,
        104500.0,
    )
    assert snapshot.entered_at_ms == NOW_MS
    rows = notices(world.store)
    assert len(rows) == 1
    assert "Bakiye elle girildi (beyan; ölçülmüş değil)" in rows[0].text
    assert "$104000.50" in rows[0].text


def test_balance_without_equity_takes_the_balance(world: World) -> None:
    world.act(ActionKind.BALANCE, bakiye="99000", onay="1")

    with world.store.transaction() as tx:
        text = tx.get_state(StateKey.MANUAL_ACCOUNT)
    assert text is not None
    snapshot = parse_snapshot(text)
    assert snapshot.equity == 99000.0
    assert snapshot.day_start_balance is None


@pytest.mark.parametrize(
    "form",
    [
        {"bakiye": "104000"},  # no confirmation
        {"bakiye": "", "onay": "1"},
        {"bakiye": "0", "onay": "1"},
        {"bakiye": "-5", "onay": "1"},
        {"bakiye": "nan", "onay": "1"},
        {"bakiye": "inf", "onay": "1"},
        {"bakiye": "1e5", "onay": "1"},
        {"bakiye": "104.000,50", "onay": "1"},
        {"bakiye": "10000001", "onay": "1"},  # above 100 x the initial balance
        {"bakiye": "104000", "ozsermaye": "abc", "onay": "1"},
        {"bakiye": "104000", "gun_basi": "0", "onay": "1"},
    ],
)
def test_invalid_balance_input_stores_nothing(world: World, form: dict[str, str]) -> None:
    result = world.actions.run(ActionKind.BALANCE, form)

    assert result.status == 400
    with world.store.transaction() as tx:
        assert tx.get_state(StateKey.MANUAL_ACCOUNT) is None
    assert notices(world.store) == []


@pytest.mark.parametrize(
    ("text", "expected"),
    [("100", 100.0), (" 100.5 ", 100.5), ("100,25", 100.25), ("1_000", None), ("+1", None)],
)
def test_parse_amount(text: str, expected: float | None) -> None:
    assert parse_amount(text) == expected


# --- refused when the panel is read-only ------------------------------------------------------


def test_no_secrets_means_every_action_is_refused(world: World) -> None:
    actions = Actions(
        ActionDeps(
            source=StoreSource(None, store=world.store),
            commands=None,
            unavailable_reason="sırlar okunamadı",
            venue=VENUE,
            clock=world.clock,
        )
    )
    for kind in ActionKind:
        result = actions.run(kind, {"onay": "1", "pin": PIN, "bakiye": "1"})
        assert result.status == 409
        assert "sırlar okunamadı" in result.reply
    assert flags(world.store)["killed"] is None
    assert notices(world.store) == []


def test_missing_database_refuses_every_action_and_creates_nothing(tmp_path: Path) -> None:
    db_path = tmp_path / "propbot.db"
    clock = FakeClock()
    source = StoreSource(db_path)
    actions = Actions(
        ActionDeps(
            source=source,
            commands=commands_factory(
                settings=SETTINGS,
                tv=TV,
                risk=RISK,
                venue=VENUE,
                venue_name=VENUE_NAME,
                clock=clock,
                started_ms=STARTED_MS,
            ),
            unavailable_reason="",
            venue=VENUE,
            clock=clock,
        )
    )

    result = actions.run(ActionKind.KILL, {"pin": PIN})

    assert result.status == 409
    assert "veritabanı yok" in result.reply
    assert not db_path.exists()


def test_control_state_is_read_only_without_secrets_or_database() -> None:
    no_secrets = control_state(unavailable_reason="x", db=DB_OK, pin_locked_until_ms=None)
    no_db = control_state(
        unavailable_reason=None,
        db=DbStatus(DbState.MISSING, "veritabanı yok"),
        pin_locked_until_ms=None,
    )

    assert not no_secrets.enabled
    assert not no_secrets.pin_enabled
    assert not no_db.enabled
    assert "veritabanı yok" in no_db.reason


# --- the PIN never leaks ----------------------------------------------------------------------


def test_pin_appears_in_no_reply_notice_log_or_repr(
    world: World, caplog: pytest.LogCaptureFixture
) -> None:
    caplog.set_level(logging.DEBUG)
    with capture_logs() as logs:
        results = [
            world.act(ActionKind.KILL, pin=PIN),
            world.act(ActionKind.UNKILL, pin=PIN),
            world.act(ActionKind.KILL, pin=WRONG_PIN),
            world.act(ActionKind.KILL, pin=f"{PIN} {PIN}"),
        ]

    texts = [repr(result) for result in results]
    texts += [row.text for row in notices(world.store)]
    texts += [repr(record) for record in logs]
    texts += [record.getMessage() for record in caplog.records]
    texts.append(repr(world.actions))
    for text in texts:
        assert PIN not in text
        assert WRONG_PIN not in text
