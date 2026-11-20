"""Control actions of the local panel (ADR-0009 items 6-8).

Pause, resume, KILL and lifting a KILL are the Telegram commands themselves: the panel
builds the command text and hands it to `notify.commands.Commands.handle`, so there is one
rule set (PIN check and the shared PIN lock in the state table, the age rule, standby).
What the panel adds is local: a ticked confirmation box for pause/resume, the manual
balance snapshot, and a Telegram NOTICE in the outbox for every action, which the running
`propbot serve` delivers to the one whitelisted chat (the panel never talks to Telegram).

The PIN travels only in the POST body; it is put into the command text and nowhere else.
`Commands` never repeats a long number of its input, and nothing here logs or returns it.
"""

import re
import threading
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from enum import StrEnum
from typing import Final

import structlog

from propbot.core.clock import Clock
from propbot.dashboard.live import (
    DbStatus,
    ManualSnapshot,
    StoreSource,
    classify_store_error,
    flag_set,
    service_status,
    snapshot_text,
)
from propbot.notify.commands import CommandDeps, Commands
from propbot.notify.format import ISTANBUL, fmt_clock, fmt_date, fmt_money
from propbot.risk.config import RiskConfig
from propbot.risk.venue import VenueProfile
from propbot.settings import Settings
from propbot.signals.tvconfig import TvAlertsConfig
from propbot.store.db import OutboxKind, StateKey, Store, StoreError

CONFIRM_FIELD: Final = "onay"
PIN_FIELD: Final = "pin"
BALANCE_FIELD: Final = "bakiye"
EQUITY_FIELD: Final = "ozsermaye"
DAY_START_FIELD: Final = "gun_basi"
# A typed balance above this many times the initial balance is a typo, not an account.
MAX_BALANCE_FACTOR: Final = 100.0
# The summary notice stays well below Telegram's 4096 characters.
MAX_SUMMARY_CHARS: Final = 3500
NOTICE_HEADER: Final = "[PANEL]"
# Digits with at most two decimals, a point or a comma: "104000", "104000.5", "104000,50".
_AMOUNT: Final = re.compile(r"[0-9]{1,12}(?:[.,][0-9]{1,2})?")
# Lines of /durum and /saglik that describe the process answering, not the bot: from the
# panel they would describe the panel (uptime, its empty webhook counters, no Telegram stamp).
_PROCESS_LINES: Final = (
    "Çalışma süresi",
    "Reddedilen webhook",
    "Yetkisiz sohbet",
    "Saat farkı",
)

_log = structlog.get_logger(__name__)


class ActionKind(StrEnum):
    PAUSE = "duraklat"
    RESUME = "devam"
    KILL = "kill"
    UNKILL = "kill-kaldir"
    SUMMARY = "ozet"
    BALANCE = "bakiye"


ACTION_TITLES: Final[Mapping[ActionKind, str]] = {
    ActionKind.PAUSE: "Duraklat",
    ActionKind.RESUME: "Devam Et",
    ActionKind.KILL: "Acil Kapat (KILL)",
    ActionKind.UNKILL: "KILL'i kaldır",
    ActionKind.SUMMARY: "Telegram Durum/Test Özeti Gönder",
    ActionKind.BALANCE: "Bakiye kaydet",
}
KILL_MEANING: Final = (
    "Faz 1–2'de bot emir göndermez: KILL yalnızca yeni giriş sinyallerini keser; "
    "terminalindeki açık pozisyonu KAPATMAZ. Pozisyonun varsa platformdan elle kapat."
)
CONFIRM_MISSING_TEXT: Final = "Yerel onay kutusu işaretlenmedi: hiçbir şey değiştirilmedi."
NOT_KILLED_TEXT: Final = "KILL etkin değil: kaldırılacak bir şey yok; değişiklik yapılmadı."
UNVERIFIED_TEXT: Final = (
    "Komut işlendi ama sonucu veritabanından okunamadı: durumu canlı ekrandan kontrol et."
)
STANDBY_BALANCE_TEXT: Final = (
    "Bu veritabanının servisi BEKLEMEDE (aynı bot token'ıyla başka bir süreç komutları alıyor): "
    "bakiye kaydedilmedi. Değeri asıl çalışan botun panelinden gir."
)


@dataclass(frozen=True, slots=True)
class ActionResult:
    kind: ActionKind
    title: str
    status: int
    """HTTP status of the result page: 200 done, 400 bad input, 409 refused, 503 busy
    (nothing changed), 500 carried out but the outcome could not be read back."""
    reply: str
    """What was done or why not; for commands, the reply Telegram would have sent."""
    notice: str
    """What happened to the Telegram notice (and whether a service is there to send it)."""


CommandsFactory = Callable[[Store], Commands]
"""Builds the Telegram command set on the store once it is open (the PIN stays inside)."""


@dataclass(frozen=True, slots=True, kw_only=True)
class ActionDeps:
    source: StoreSource
    commands: CommandsFactory | None
    unavailable_reason: str
    """Why `commands` is None (secrets or tv_alerts.yaml could not be loaded)."""
    venue: VenueProfile
    clock: Clock


def _stamp(epoch_ms: int) -> str:
    return f"{fmt_date(epoch_ms, ISTANBUL)} {fmt_clock(epoch_ms, ISTANBUL, seconds=True)} TR"


def parse_amount(text: str | None) -> float | None:
    """A typed money amount, or None when it is not one (no exponent, no grouping, no sign)."""
    if text is None:
        return None
    cleaned = text.strip()
    if _AMOUNT.fullmatch(cleaned) is None:
        return None
    return float(cleaned.replace(",", "."))


def strip_process_lines(text: str) -> str:
    return "\n".join(line for line in text.splitlines() if not line.startswith(_PROCESS_LINES))


class Actions:
    """Carries out one panel action at a time (`run` is called from a worker thread)."""

    def __init__(self, deps: ActionDeps) -> None:
        self._deps = deps
        self._lock = threading.Lock()
        self._commands: tuple[Store, Commands] | None = None

    def _commands_for(self, store: Store) -> Commands | None:
        factory = self._deps.commands
        if factory is None:
            return None
        with self._lock:
            # One Commands per store, as in `serve`: it keeps no state of its own that a new
            # instance would lose (the PIN counter and lock live in the state table).
            if self._commands is None or self._commands[0] is not store:
                self._commands = (store, factory(store))
            return self._commands[1]

    def run(self, kind: ActionKind, form: Mapping[str, str]) -> ActionResult:
        title = ACTION_TITLES[kind]
        if self._deps.commands is None:
            return ActionResult(
                kind, title, 409, f"Kontroller kapalı: {self._deps.unavailable_reason}", ""
            )
        store = self._deps.source.get()
        if isinstance(store, DbStatus):
            return ActionResult(kind, title, 409, f"Kontroller kapalı: {store.message}", "")
        commands = self._commands_for(store)
        if commands is None:
            return ActionResult(
                kind, title, 409, f"Kontroller kapalı: {self._deps.unavailable_reason}", ""
            )
        try:
            result = self._run(kind, title, form, commands, store)
        except StoreError as error:
            status = classify_store_error(error)
            result = ActionResult(
                kind, title, 503, f"{status.message}. Hiçbir şey değiştirilmedi; yeniden dene.", ""
            )
        _log.info("panel_action", action=kind.value, status=result.status)
        return result

    def _run(
        self,
        kind: ActionKind,
        title: str,
        form: Mapping[str, str],
        commands: Commands,
        store: Store,
    ) -> ActionResult:
        confirmed = form.get(CONFIRM_FIELD) == "1"
        if kind is ActionKind.BALANCE:
            return self._balance(title, form, store, confirmed=confirmed)
        if kind is ActionKind.SUMMARY:
            return self._summary(title, commands, store)
        if kind in (ActionKind.PAUSE, ActionKind.RESUME) and not confirmed:
            return ActionResult(kind, title, 400, CONFIRM_MISSING_TEXT, "")
        pin = form.get(PIN_FIELD, "")
        if kind is ActionKind.PAUSE:
            text = "/duraklat"
        elif kind is ActionKind.RESUME:
            text = "/devam"
        elif kind is ActionKind.KILL:
            text = f"/kill {pin}" if pin else "/kill"
        else:
            with store.transaction() as tx:
                killed = flag_set(tx.get_state(StateKey.KILLED))
            if not killed:
                # `/devam <PIN>` without a KILL would only lift a pause, unasked.
                return ActionResult(kind, title, 409, NOT_KILLED_TEXT, "")
            text = f"/devam {pin}" if pin else "/devam"
        reply = self._handle(commands, store, text)
        # The outcome is read back from the flags, not inferred from the reply: a wrong PIN,
        # a lock or a refusal on standby is answered with text, and the result page must not
        # call such an answer done (a KILL believed set while it is not is the worst case).
        done = _took_effect(kind, store)
        notice = self._notify(store, f"{title}\n{reply}")
        if done is None:
            return ActionResult(kind, title, 500, f"{reply}\n{UNVERIFIED_TEXT}", notice)
        return ActionResult(kind, title, 200 if done else 409, reply, notice)

    def _handle(self, commands: Commands, store: Store, text: str) -> str:
        # The same standby mark the processor and Telegram read: on a copy that is on
        # standby only /kill and /duraklat are carried out (Commands decides).
        with store.transaction() as tx:
            standby = flag_set(tx.get_state(StateKey.STANDBY))
        return commands.handle(text, sent_at_ms=self._deps.clock(), standby=standby).text

    def _notify(self, store: Store, body: str) -> str:
        now_ms = self._deps.clock()
        text = f"{NOTICE_HEADER} {_stamp(now_ms)} · {body}"
        try:
            with store.transaction() as tx:
                tx.enqueue_message(kind=OutboxKind.NOTICE, text=text, now_ms=now_ms)
        except StoreError as error:
            _log.error("panel_notice_failed", error_type=type(error).__name__)
            return "Telegram bildirimi giden kutusuna YAZILAMADI (veritabanı hatası)."
        return self._delivery_line(store)

    def _delivery_line(self, store: Store) -> str:
        try:
            with store.transaction() as tx:
                last_alive = tx.get_state(StateKey.LAST_ALIVE_MS)
        except StoreError:
            last_alive = None
        service = service_status(last_alive, self._deps.clock())
        if service.alive:
            return (
                "Telegram bildirimi giden kutusuna yazıldı; servis çalışıyor, birkaç saniye "
                "içinde gönderir."
            )
        return (
            "Telegram bildirimi giden kutusuna yazıldı, ama servis ÇALIŞMIYOR "
            f"({service.detail}). Bildirimin süresi dolmaz: propbot serve açılınca gönderilir."
        )

    def _summary(self, title: str, commands: Commands, store: Store) -> ActionResult:
        kind = ActionKind.SUMMARY
        with store.transaction() as tx:
            standby = flag_set(tx.get_state(StateKey.STANDBY))
        sent_at_ms = self._deps.clock()
        status = commands.handle("/durum", sent_at_ms=sent_at_ms, standby=standby).text
        if standby:
            # The copy on standby must not report as the bot (Commands refused /durum).
            return ActionResult(kind, title, 409, status, "Bildirim yazılmadı.")
        health = commands.handle("/saglik", sent_at_ms=sent_at_ms, standby=standby).text
        status = strip_process_lines(status)
        health = strip_process_lines(health)
        body = f"Durum/Test özeti (panelden istendi)\n{status}"
        if len(body) + len(health) + 2 <= MAX_SUMMARY_CHARS:
            body += f"\n\n{health}"
        notice = self._notify(store, body)
        return ActionResult(kind, title, 200, body, notice)

    def _balance(
        self, title: str, form: Mapping[str, str], store: Store, *, confirmed: bool
    ) -> ActionResult:
        kind = ActionKind.BALANCE
        if not confirmed:
            return ActionResult(kind, title, 400, CONFIRM_MISSING_TEXT, "")
        venue = self._deps.venue
        upper = venue.initial_balance * MAX_BALANCE_FACTOR
        money = venue.account_currency
        values: dict[str, float | None] = {}
        problems: list[str] = []
        for field, label, required in (
            (BALANCE_FIELD, "Bakiye", True),
            (EQUITY_FIELD, "Özsermaye", False),
            (DAY_START_FIELD, "Gün başı bakiyesi", False),
        ):
            raw = form.get(field, "").strip()
            if not raw:
                if required:
                    problems.append(f"{label} boş bırakılamaz.")
                values[field] = None
                continue
            amount = parse_amount(raw)
            if amount is None:
                problems.append(
                    f"{label} bir tutar olmalı (yalnızca rakam, en çok iki ondalık; ör. 100250,50)."
                )
            elif not 0 < amount <= upper:
                problems.append(f"{label} 0'dan büyük ve en çok {fmt_money(upper, money)} olmalı.")
            values[field] = amount
        balance = values[BALANCE_FIELD]
        if problems or balance is None:
            return ActionResult(kind, title, 400, "Kaydedilmedi:\n" + "\n".join(problems), "")
        equity = values[EQUITY_FIELD]
        now_ms = self._deps.clock()
        snapshot = ManualSnapshot(
            balance=balance,
            equity=balance if equity is None else equity,
            day_start_balance=values[DAY_START_FIELD],
            entered_at_ms=now_ms,
        )
        day_start = snapshot.day_start_balance
        summary = (
            f"bakiye {fmt_money(snapshot.balance, money)} · özsermaye "
            f"{fmt_money(snapshot.equity, money)}"
            + ("" if equity is not None else " (girilmedi, bakiye kabul edildi)")
            + (
                " · gün başı bakiyesi girilmedi"
                if day_start is None
                else f" · gün başı {fmt_money(day_start, money)}"
            )
        )
        notice_text = (
            f"{NOTICE_HEADER} {_stamp(now_ms)} · Bakiye elle girildi (beyan; ölçülmüş değil): "
            f"{summary}"
        )
        # The snapshot and its notice are written together or not at all; a copy on standby
        # takes nothing, as it takes no Telegram command beyond /kill and /duraklat.
        with store.transaction() as tx:
            standby = flag_set(tx.get_state(StateKey.STANDBY))
            if not standby:
                tx.set_state(StateKey.MANUAL_ACCOUNT, snapshot_text(snapshot), now_ms)
                tx.enqueue_message(kind=OutboxKind.NOTICE, text=notice_text, now_ms=now_ms)
        if standby:
            return ActionResult(kind, title, 409, STANDBY_BALANCE_TEXT, "Bildirim yazılmadı.")
        reply = f'Kaydedildi: {summary}. Prop günü değişince "güncel değil" olarak işaretlenir.'
        return ActionResult(kind, title, 200, reply, self._delivery_line(store))


def _took_effect(kind: ActionKind, store: Store) -> bool | None:
    """Whether the flags now say what the action asked for; None when they cannot be read."""
    try:
        with store.transaction() as tx:
            killed = flag_set(tx.get_state(StateKey.KILLED))
            paused = flag_set(tx.get_state(StateKey.PAUSED))
    except StoreError as error:
        _log.error("panel_outcome_unreadable", error_type=type(error).__name__)
        return None
    if kind is ActionKind.KILL:
        return killed
    if kind is ActionKind.PAUSE:
        return paused
    if kind is ActionKind.RESUME:
        return not killed and not paused
    return not killed


@dataclass(frozen=True, slots=True)
class ControlState:
    """What the control forms show; the same conditions are enforced again in `Actions.run`."""

    enabled: bool
    reason: str
    pin_enabled: bool
    pin_reason: str


def control_state(
    *, unavailable_reason: str | None, db: DbStatus, pin_locked_until_ms: int | None
) -> ControlState:
    if unavailable_reason is not None:
        return ControlState(False, f"Panel salt-okunur: {unavailable_reason}", False, "")
    if not db.ok:
        return ControlState(False, f"Panel salt-okunur: {db.message}", False, "")
    if pin_locked_until_ms is not None:
        until = fmt_clock(pin_locked_until_ms, ISTANBUL)
        return ControlState(
            True, "", False, f"PIN girişi kilitli (çok fazla hatalı deneme): {until} TR'ye kadar."
        )
    return ControlState(True, "", True, "")


def commands_factory(
    *,
    settings: Settings,
    tv: TvAlertsConfig,
    risk: RiskConfig,
    venue: VenueProfile,
    venue_name: str,
    clock: Clock,
    started_ms: int,
) -> CommandsFactory:
    """The Telegram command set as `serve` builds it, minus the webhook counters.

    The panel has no webhook, so it has no rejection counters: the /saglik line that shows
    them is dropped from the summary (`strip_process_lines`) instead of claiming zero.
    """

    def build(store: Store) -> Commands:
        return Commands(
            CommandDeps(
                store=store,
                risk=risk,
                venue=venue,
                venue_name=venue_name,
                tv=tv,
                pin=settings.propbot_pin.get_secret_value(),
                clock=clock,
                started_ms=started_ms,
                reject_counts=dict,
            )
        )

    return build
