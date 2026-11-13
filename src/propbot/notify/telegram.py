"""Telegram side of the signal bot (SPEC §5.4): one whitelisted private chat.

Two jobs share one python-telegram-bot `Application`:

- commands in: long polling; a message is handled only when it comes from the whitelisted
  private chat (CLAUDE.md invariant 7). The text goes to `notify.commands.Commands` in a
  worker thread; this module only checks the chat, sends the reply and deletes messages
  that may hold the PIN.
- outbox out: the store's FIFO outbox is delivered at least once. `send_due` is exactly
  one sender step: plan in one transaction, talk to Telegram outside any transaction,
  record the result in a second one. `run` is that step in a loop plus a wake event.

The bot only ever sends to `allowed_chat_id`; no other chat id exists in this module.
Nothing here places, changes or cancels an order.

Secrets (CLAUDE.md invariant 6): python-telegram-bot's `InvalidToken` quotes the bot
token, its `Bot` prints the token in its repr and httpx errors may carry the request URL.
So exception TEXT never leaves this module: logs and `outbox.last_error` get class names
only, and a rejected token becomes `TelegramFatalError` with a constant message and no
cause. Update texts are never logged (they may hold the PIN).

Delivery doubt: when a send fails after the request may have left (read timeout, 5xx,
connection lost), the row is marked `delivery_uncertain`. An entry signal that later
expires or is dropped in that state keeps its budget row counted and the user is asked,
because "not marked sent" is no proof that the signal was not seen.

All methods must be called from the event loop thread that runs `run` / `send_due`.
"""

import asyncio
import datetime as dt
import math
import re
import warnings
from contextlib import suppress
from dataclasses import dataclass
from enum import StrEnum
from typing import Any, Final

import structlog
from telegram import LinkPreviewOptions, MessageEntity, Update
from telegram.constants import ChatType, MessageLimit
from telegram.error import (
    BadRequest,
    Conflict,
    InvalidToken,
    NetworkError,
    RetryAfter,
    TelegramError,
)
from telegram.ext import Application, ApplicationBuilder, ContextTypes, TypeHandler
from telegram.request import BaseRequest
from telegram.warnings import PTBDeprecationWarning

from propbot.core.clock import Clock, ms_from_datetime
from propbot.notify.commands import Commands
from propbot.notify.format import fmt_duration
from propbot.store.db import (
    OutboxKind,
    OutboxRow,
    ReleaseReason,
    StateKey,
    Store,
    StoreError,
    Tx,
)

_SECOND_MS: Final = 1000
_MINUTE_MS: Final = 60 * _SECOND_MS

# The run loop looks at the outbox at least this often (a wake-up makes it look at once).
_IDLE_POLL_SECONDS: Final = 1.0
# How long a clean stop of the polling task may take before the task is cancelled.
_POLLING_STOP_GRACE_SECONDS: Final = 0.5
# Retry delays after a network failure: 1, 2, 4, 8, 16, then 30 seconds.
_MAX_BACKOFF_MS: Final = 30 * _SECOND_MS
_MAX_BACKOFF_EXPONENT: Final = 5
_LOG_THROTTLE_MS: Final = 10 * _SECOND_MS
_CONFLICT_ALARM: Final = ("telegram_conflict", 60 * _MINUTE_MS)
_ALLOWED_UPDATES: Final = ("message",)
# Name python-telegram-bot gives the task that long-polls (telegram/ext/_updater.py).
_POLLING_TASK_NAME: Final = "Updater:start_polling:polling_task"

# Telegram turns anything that looks like a domain ("US500.cash") into a link.
_DOMAIN_LIKE: Final = re.compile(r"[A-Za-z0-9-]+(?:\.[A-Za-z0-9-]+)*\.[A-Za-z]{2,}")
# A command with an id, e.g. "/atladim 126". Telegram makes "/atladim" a link that SENDS the
# bare command when tapped; without its id that command would act on another entry. As
# `code` the whole "/atladim 126" is copied instead.
_COMMAND_WITH_ID: Final = re.compile(r"/[a-z_]+ [0-9]+")
_CODE_TOKEN: Final = re.compile(f"{_COMMAND_WITH_ID.pattern}|{_DOMAIN_LIKE.pattern}")
# Exceptions (httpx / httpcore) proving that the HTTP request was never written: the
# connection could not be opened, or no pooled connection became free.
_NEVER_SENT_ERRORS: Final = frozenset({"ConnectError", "ConnectTimeout", "PoolTimeout"})
_HTTP_LIBRARIES: Final = frozenset({"httpx", "httpcore"})

_FLAG_CLEAR: Final = "0"
_SEND_FAILED_ALARM_CODE: Final = "send_failed"
_SEND_FAILED_ALARM_THROTTLE_MS: Final = 10 * 60 * 1000
_TRUNCATION_MARK: Final = "\n[… mesaj kısaltıldı]"

_FATAL_TOKEN: Final = (
    "Telegram bot token'ı reddedildi. TELEGRAM_BOT_TOKEN değerini BotFather'daki güncel "
    "token ile karşılaştır; servis durduruluyor."
)
_FATAL_POLLING: Final = (
    "Telegram güncelleme döngüsü beklenmedik biçimde durdu; servis durduruluyor "
    "(yeniden başlatılmalı)."
)
_CONFLICT_ALARM_TEXT: Final = (
    "[ALARM] Aynı bot token'ıyla başka bir süreç güncelleme çekiyor. İki kopya çalışıyorsa "
    "birini durdur; değilse token sızmış olabilir: BotFather → /revoke, .env'i güncelle, "
    "PIN'i değiştir."
)
_DELETE_FAILED_TEXT: Final = "PIN içeren mesajı silemedim — elle sil ve PROPBOT_PIN'i değiştir."

_log = structlog.get_logger(__name__)


def _after_permanent_failure(tx: Tx, row: OutboxRow, delivery: "_Delivery") -> None:
    """Telegram refused this message for good, so it did not arrive: do not let that be silent.

    An entry that never reached the user is released from the budget, and the user is told
    (throttled) that a message was dropped. A failed alarm raises no further alarm.
    """
    released = False
    if row.signal_id is not None and row.expires_at_ms is not None:
        entry = tx.get_budget_entry(row.signal_id)
        if entry is not None and entry.counted:
            tx.set_budget_counted(
                signal_id=row.signal_id,
                counted=False,
                reason=ReleaseReason.UNDELIVERED,
                now_ms=delivery.at_ms,
            )
            released = True
    if row.kind is OutboxKind.ALARM:
        return
    subject = "bir mesaj" if row.signal_id is None else f"sinyal id {row.signal_id} mesajı"
    text = (
        f"[ALARM] Telegram {subject} için kalıcı hata verdi ({delivery.error}); mesaj "
        "GÖNDERİLEMEDİ ve atlandı.\n"
        "TradingView Alert Log'undan ne olduğunu kontrol et."
    )
    if released:
        text += " Bu bir giriş sinyaliydi: ATLA say; bütçeden düşüldü."
    tx.enqueue_alarm(
        code=_SEND_FAILED_ALARM_CODE,
        text=text,
        now_ms=delivery.at_ms,
        throttle_ms=_SEND_FAILED_ALARM_THROTTLE_MS,
    )


class TelegramFatalError(Exception):
    """Telegram cannot be used as configured; the process must stop.

    The message is one of this module's constants: never library text, never the token.
    """


def code_entity_spans(text: str) -> list[tuple[int, int]]:
    """`(offset, length)` of every token to send as `code`, in UTF-16 code units.

    Two kinds of token: domain-like words ("US500.cash"), which Telegram would turn into
    links, and commands that carry an id ("/atladim 126"), whose tap would otherwise send
    the command without the id.

    Telegram counts entity positions in UTF-16: a character outside the Basic Multilingual
    Plane (most emoji) is two units, a Turkish letter is one. The tokens are ASCII, so a
    length is the same in characters and in units.
    """
    spans: list[tuple[int, int]] = []
    units = 0
    position = 0
    for match in _CODE_TOKEN.finditer(text):
        units += _utf16_length(text[position : match.start()])
        position = match.start()
        spans.append((units, match.end() - match.start()))
    return spans


def fit_text(text: str) -> str:
    """Cut a text that exceeds Telegram's message limit, marking the cut.

    Measured in UTF-16 units (never more lenient than a count of characters). A cut
    message still arrives; an over-long one would be rejected as a whole.
    """
    limit = int(MessageLimit.MAX_TEXT_LENGTH)
    if _utf16_length(text) <= limit:
        return text
    room = limit - _utf16_length(_TRUNCATION_MARK)
    kept: list[str] = []
    used = 0
    for character in text:
        size = _utf16_length(character)
        if used + size > room:
            break
        kept.append(character)
        used += size
    return "".join(kept) + _TRUNCATION_MARK


def _utf16_length(text: str) -> int:
    return len(text.encode("utf-16-le", "surrogatepass")) // 2


def _retry_after_seconds(error: RetryAfter) -> float:
    """Seconds Telegram asked us to wait.

    `RetryAfter.retry_after` is an int or a timedelta depending on the `PTB_TIMEDELTA`
    environment variable, and warns about the coming change in the first case.
    """
    with warnings.catch_warnings():
        warnings.simplefilter("ignore", PTBDeprecationWarning)
        value = error.retry_after
    seconds = value.total_seconds() if isinstance(value, dt.timedelta) else float(value)
    return max(0.0, seconds)


def _never_left(error: BaseException) -> bool:
    """True when the cause chain proves the request was not sent (connect or pool phase)."""
    seen: set[int] = set()
    current: BaseException | None = error
    while current is not None and id(current) not in seen:
        seen.add(id(current))
        kind = type(current)
        if kind.__name__ in _NEVER_SENT_ERRORS and kind.__module__.split(".")[0] in _HTTP_LIBRARIES:
            return True
        current = current.__cause__ or current.__context__
    return False


def _backoff_ms(failures_so_far: int) -> int:
    """`min(30 s, 2^failures s)`: 1 s after the first failure."""
    exponent = min(max(failures_so_far, 0), _MAX_BACKOFF_EXPONENT)
    return min(_MAX_BACKOFF_MS, (1 << exponent) * _SECOND_MS)


def _flag(tx: Tx, key: StateKey) -> bool:
    # Absent or "0" is clear. Any other text counts as set: an unreadable flag fails closed.
    return tx.get_state(key) not in (None, _FLAG_CLEAR)


def _uncertain_notice(signal_id: int, *, dropped: bool) -> str:
    why = "duraklatma/KILL nedeniyle yeniden gönderilmedi" if dropped else "geçerliliği doldu"
    return (
        f"[İLETİM BELİRSİZ] id {signal_id}: giriş sinyali sana ulaşmış da olabilir, ulaşmamış "
        f"da ({why}). Gördüysen ve girdiysen bir şey yapma; görmediysen ya da girmediysen: "
        f"/atladim {signal_id}"
    )


class _Throttle:
    """Lets one log line through per interval and counts what it held back."""

    def __init__(self, interval_ms: int) -> None:
        self._interval_ms = interval_ms
        self._last_ms: int | None = None
        self._suppressed = 0

    def due(self, now_ms: int) -> int | None:
        """Return how many lines were held back since the last one, or None to hold this one."""
        last = self._last_ms
        if last is not None and 0 <= now_ms - last < self._interval_ms:
            self._suppressed += 1
            return None
        self._last_ms = now_ms
        suppressed, self._suppressed = self._suppressed, 0
        return suppressed


class _Result(StrEnum):
    SENT = "sent"
    RETRY = "retry"
    FAILED = "failed"
    FATAL = "fatal"


@dataclass(frozen=True, slots=True)
class _Delivery:
    """What one send attempt came to. `error` is an exception class name, never its text."""

    result: _Result
    at_ms: int
    error: str = ""
    retry_at_ms: int = 0
    uncertain: bool = False


@dataclass(frozen=True, slots=True)
class _Plan:
    changed: bool  # the planning transaction wrote something
    to_send: OutboxRow | None


class TelegramService:
    """Polling for commands plus the outbox sender, for exactly one private chat.

    `application` is the python-telegram-bot Application (tests feed updates through
    `application.process_update`). `run` owns its life cycle; `send_due` connects on
    demand, so it also works on its own.
    """

    def __init__(
        self,
        *,
        bot_token: str,
        allowed_chat_id: int,
        commands: Commands,
        store: Store,
        clock: Clock,
        validity_seconds: int,
        request: BaseRequest | None = None,
        get_updates_request: BaseRequest | None = None,
        polling: bool = True,
    ) -> None:
        if allowed_chat_id <= 0:
            # Groups and channels have negative ids: signals go to one private chat only.
            raise ValueError("allowed_chat_id must be the id of a private chat (positive)")
        if validity_seconds <= 0:
            raise ValueError("validity_seconds must be positive")
        self._chat_id: Final = allowed_chat_id
        self._commands: Final = commands
        self._store: Final = store
        self._clock: Final = clock
        self._validity_ms: Final = validity_seconds * _SECOND_MS
        self._polling: Final = polling

        builder = ApplicationBuilder().token(bot_token).job_queue(None)
        if request is not None:
            builder = builder.request(request)
        if get_updates_request is not None:
            builder = builder.get_updates_request(get_updates_request)
        application: Application[Any, Any, Any, Any, Any, Any] | None = None
        with suppress(InvalidToken):
            application = builder.build()
        if application is None:
            # Raised outside the handler: the new error must not chain to the library's.
            raise TelegramFatalError(_FATAL_TOKEN) from None
        self.application: Final = application
        application.add_handler(TypeHandler(Update, self._on_update))
        application.add_error_handler(self._on_handler_error)

        self._wake: Final = asyncio.Event()
        self._step_lock: Final = asyncio.Lock()
        self._force = False  # a wake-up asked for an immediate attempt
        self._flagged = False  # start-up flagging of pending entry signals is done
        self._retry_after_until_ms: int | None = None
        self._fatal_reason: str | None = None

        self._bot_ready = False
        self._started = False
        self._connect_failures = 0
        self._next_connect_ms = 0
        self._polling_task: asyncio.Task[Any] | None = None
        self._stopping = False

        self._conflict_alarm_ms: int | None = None
        self._background: Final[set[asyncio.Task[None]]] = set()
        self._unauthorized_log: Final = _Throttle(_LOG_THROTTLE_MS)
        self._polling_log: Final = _Throttle(_LOG_THROTTLE_MS)

    # -- public ------------------------------------------------------------------------------

    def wake_sender(self) -> None:
        """A row was put in the outbox: make the sender try now instead of at its next poll."""
        self._force = True
        self._wake.set()

    async def run(self, stop: asyncio.Event) -> None:
        """Connect (retrying while the network is down), poll and send until `stop` is set.

        Ends with `TelegramFatalError` when the token is rejected or polling died, and with
        `StoreError` when the database fails; everything else is retried or skipped.
        """
        try:
            while not stop.is_set():
                did_something = await self.send_due()
                await self._ensure_started()
                self._check_polling()
                self._raise_if_fatal()
                if not did_something:
                    await self._idle(stop)
        finally:
            self._stopping = True
            await self._shutdown()

    async def send_due(self) -> bool:
        """Perform one sender step; return whether it changed or sent anything.

        A step expires overdue entry signals, drops entry signals while paused or killed,
        and sends at most one message (the head of the FIFO, when it is due).
        """
        async with self._step_lock:
            self._raise_if_fatal()
            did_something = await self._step()
            self._raise_if_fatal()
            return did_something

    # -- sender ------------------------------------------------------------------------------

    async def _step(self) -> bool:
        forced, self._force = self._force, False
        ready = await self._ensure_bot()
        if self._fatal_reason is not None:
            return False
        now_ms = self._clock()
        held_back = self._retry_after_until_ms is not None and now_ms < self._retry_after_until_ms
        plan = await asyncio.to_thread(
            self._plan,
            now_ms,
            flag_uncertain=not self._flagged,
            may_send=ready and not held_back,
            forced=forced,
        )
        self._flagged = True
        row = plan.to_send
        if row is None:
            return plan.changed
        delivery = await self._deliver(row, now_ms)
        if delivery.result is _Result.FATAL:
            return True  # the row stays pending; `send_due` raises
        await asyncio.to_thread(self._record, row, delivery)
        return True

    def _plan(self, now_ms: int, *, flag_uncertain: bool, may_send: bool, forced: bool) -> _Plan:
        """Planning transaction: expire, drop, and pick the message to send (worker thread)."""
        changed = False
        with self._store.transaction() as tx:
            if flag_uncertain:
                # The previous process may have died between a send and `mark_message_sent`.
                flagged = tx.flag_pending_deadlines_uncertain()
                if flagged:
                    changed = True
                    _log.warning("telegram_pending_entries_uncertain", count=flagged)
            for row in tx.pending_expired_messages(now_ms):
                self._close_entry_message(tx, row, now_ms, dropped=False)
                changed = True
            entries_blocked = _flag(tx, StateKey.KILLED) or _flag(tx, StateKey.PAUSED)
            head = tx.next_pending_message()
            while head is not None and head.expires_at_ms is not None and entries_blocked:
                self._close_entry_message(tx, head, now_ms, dropped=True)
                changed = True
                head = tx.next_pending_message()
        due = head is not None and (forced or head.next_attempt_ms <= now_ms)
        return _Plan(changed=changed, to_send=head if may_send and due else None)

    @staticmethod
    def _close_entry_message(tx: Tx, row: OutboxRow, now_ms: int, *, dropped: bool) -> None:
        """Give up on a message with a deadline: it expired, or entries are paused/killed.

        Proven undelivered: the entry's risk leaves the daily budget, and an expiry tells
        the user. Possibly delivered: the risk stays counted and the user is asked.
        """
        tx.mark_message_expired(row.id, now_ms)
        signal_id = row.signal_id
        _log.warning(
            "telegram_entry_dropped" if dropped else "telegram_message_expired",
            outbox_id=row.id,
            signal_id=signal_id,
            uncertain=row.delivery_uncertain,
            attempts=row.attempts,
        )
        if row.delivery_uncertain:
            if signal_id is not None:
                tx.enqueue_message(
                    kind=OutboxKind.NOTICE,
                    text=_uncertain_notice(signal_id, dropped=dropped),
                    now_ms=now_ms,
                )
            return
        if signal_id is not None:
            entry = tx.get_budget_entry(signal_id)
            if entry is not None and entry.counted:
                tx.set_budget_counted(
                    signal_id=signal_id,
                    counted=False,
                    reason=ReleaseReason.UNDELIVERED,
                    now_ms=now_ms,
                )
        if not dropped and row.expired_text is not None:
            tx.enqueue_message(kind=OutboxKind.NOTICE, text=row.expired_text, now_ms=now_ms)

    async def _deliver(self, row: OutboxRow, now_ms: int) -> _Delivery:
        """Send one outbox row and classify the result. Raises nothing but cancellation."""
        try:
            text = row.text
            late_ms = now_ms - row.created_at_ms
            if (
                row.kind is OutboxKind.SIGNAL
                and row.expires_at_ms is None
                and late_ms > self._validity_ms
            ):
                text = f"[GEÇ İLETİLDİ +{fmt_duration(late_ms / _SECOND_MS)}] {text}"
            await self._send_text(text)
        except InvalidToken:
            self._fatal_reason = _FATAL_TOKEN
            return _Delivery(_Result.FATAL, self._clock())
        except RetryAfter as error:
            at_ms = self._clock()
            until_ms = at_ms + math.ceil(_retry_after_seconds(error) * _SECOND_MS)
            self._retry_after_until_ms = until_ms
            delivery = _Delivery(_Result.RETRY, at_ms, type(error).__name__, until_ms)
        except BadRequest as error:
            # Subclasses NetworkError, but the request itself is wrong: retrying cannot help.
            delivery = _Delivery(_Result.FAILED, self._clock(), type(error).__name__)
        except NetworkError as error:
            at_ms = self._clock()
            delivery = _Delivery(
                _Result.RETRY,
                at_ms,
                type(error).__name__,
                at_ms + _backoff_ms(row.attempts),
                uncertain=not _never_left(error),
            )
        except Exception as error:
            # Forbidden, ChatMigrated, any other TelegramError, or a bug hit by this one row:
            # set the row aside so it cannot block the queue.
            delivery = _Delivery(_Result.FAILED, self._clock(), type(error).__name__)
        else:
            delivery = _Delivery(_Result.SENT, self._clock())

        fields = {"outbox_id": row.id, "kind": row.kind.value, "signal_id": row.signal_id}
        if delivery.result is _Result.SENT:
            _log.info("telegram_message_sent", attempts=row.attempts + 1, **fields)
        elif delivery.result is _Result.RETRY:
            _log.warning(
                "telegram_send_retry",
                error_type=delivery.error,
                retry_in_ms=delivery.retry_at_ms - delivery.at_ms,
                uncertain=delivery.uncertain,
                attempts=row.attempts + 1,
                **fields,
            )
        else:
            _log.error("telegram_send_failed", error_type=delivery.error, **fields)
        return delivery

    def _record(self, row: OutboxRow, delivery: _Delivery) -> None:
        """Recording transaction (worker thread)."""
        with self._store.transaction() as tx:
            if delivery.result is _Result.SENT:
                tx.mark_message_sent(row.id, delivery.at_ms)
            elif delivery.result is _Result.RETRY:
                tx.mark_message_retry(
                    row.id,
                    next_attempt_ms=delivery.retry_at_ms,
                    error=delivery.error,
                    uncertain=delivery.uncertain,
                )
            else:
                tx.mark_message_failed(row.id, error=delivery.error, now_ms=delivery.at_ms)
                _after_permanent_failure(tx, row, delivery)

    async def _send_text(self, text: str) -> None:
        """Send plain text to the one allowed chat; domain-like tokens become `code`."""
        text = fit_text(text)
        spans = code_entity_spans(text)[: int(MessageLimit.MESSAGE_ENTITIES)]
        entities = [MessageEntity(MessageEntity.CODE, offset, length) for offset, length in spans]
        await self.application.bot.send_message(
            chat_id=self._chat_id,
            text=text,
            entities=entities or None,
            link_preview_options=LinkPreviewOptions(is_disabled=True),
        )

    # -- commands ----------------------------------------------------------------------------

    async def _on_update(self, update: Update, _context: ContextTypes.DEFAULT_TYPE) -> None:
        message = update.message
        chat = update.effective_chat
        sender = None if message is None else message.from_user
        from_owner = (
            chat is not None
            and chat.id == self._chat_id
            and chat.type == ChatType.PRIVATE
            and (message is None or (sender is not None and sender.id == self._chat_id))
        )
        if not from_owner:
            self._note_unauthorized(None if chat is None else chat.id)
            return
        if message is None:
            return  # an edit or another update kind from the owner's chat: nothing to answer

        text = message.text or message.caption or ""
        sent_at_ms = ms_from_datetime(message.date)
        reply = await asyncio.to_thread(self._commands.handle, text, sent_at_ms=sent_at_ms)
        deleted = True
        if reply.delete_request:
            deleted = await self._delete(message.message_id)
        await self._reply(reply.text)
        if not deleted:
            await self._reply(_DELETE_FAILED_TEXT)
        # Let the sender look now (a kill or pause drops waiting entry signals) without
        # forcing an attempt that is in back-off.
        self._wake.set()

    def _note_unauthorized(self, chat_id: int | None) -> None:
        self._commands.note_unauthorized()
        suppressed = self._unauthorized_log.due(self._clock())
        if suppressed is not None:
            _log.warning("telegram_unauthorized_chat", chat_id=chat_id, suppressed=suppressed)

    async def _delete(self, message_id: int) -> bool:
        try:
            await self.application.bot.delete_message(chat_id=self._chat_id, message_id=message_id)
        except InvalidToken:
            self._fatal_reason = _FATAL_TOKEN
            return False
        except TelegramError as error:
            _log.error("telegram_delete_failed", error_type=type(error).__name__)
            return False
        return True

    async def _reply(self, text: str) -> None:
        """Send a command reply directly; if Telegram is briefly unreachable, queue it instead."""
        now_ms = self._clock()
        if self._retry_after_until_ms is not None and now_ms < self._retry_after_until_ms:
            await self._queue_reply(text, "RetryAfter")
            return
        try:
            await self._send_text(text)
        except InvalidToken:
            self._fatal_reason = _FATAL_TOKEN
        except RetryAfter as error:
            delay_ms = math.ceil(_retry_after_seconds(error) * _SECOND_MS)
            self._retry_after_until_ms = self._clock() + delay_ms
            await self._queue_reply(text, type(error).__name__)
        except BadRequest as error:
            _log.error("telegram_reply_failed", error_type=type(error).__name__)
        except NetworkError as error:
            await self._queue_reply(text, type(error).__name__)
        except TelegramError as error:
            _log.error("telegram_reply_failed", error_type=type(error).__name__)

    async def _queue_reply(self, text: str, error_type: str) -> None:
        """Hand a reply to the outbox, which retries (replies never contain the PIN)."""

        def enqueue() -> None:
            with self._store.transaction() as tx:
                tx.enqueue_message(kind=OutboxKind.NOTICE, text=text, now_ms=self._clock())

        try:
            await asyncio.to_thread(enqueue)
        except StoreError as error:
            _log.error("telegram_reply_lost", error_type=type(error).__name__)
            return
        _log.warning("telegram_reply_queued", error_type=error_type)
        self._wake.set()

    async def _on_handler_error(self, _update: object, context: ContextTypes.DEFAULT_TYPE) -> None:
        error = context.error
        if isinstance(error, InvalidToken):
            self._fatal_reason = _FATAL_TOKEN
        # The type only: the text may quote the update or the token.
        _log.error("telegram_handler_failed", error_type=type(error).__name__)
        self._wake.set()

    # -- polling -----------------------------------------------------------------------------

    def _on_polling_error(self, error: TelegramError) -> None:
        """Called by python-telegram-bot for errors of `get_updates`; must not raise."""
        now_ms = self._clock()
        if isinstance(error, InvalidToken):
            self._fatal_reason = _FATAL_TOKEN
            self._wake.set()
            return
        suppressed = self._polling_log.due(now_ms)
        if suppressed is not None:
            _log.warning(
                "telegram_polling_error", error_type=type(error).__name__, suppressed=suppressed
            )
        if not isinstance(error, Conflict):
            return  # python-telegram-bot retries with its own back-off
        last = self._conflict_alarm_ms
        if last is not None and 0 <= now_ms - last < _CONFLICT_ALARM[1]:
            return
        self._conflict_alarm_ms = now_ms
        task = asyncio.get_running_loop().create_task(self._conflict_alarm(now_ms))
        self._background.add(task)
        task.add_done_callback(self._background.discard)

    async def _conflict_alarm(self, now_ms: int) -> None:
        code, throttle_ms = _CONFLICT_ALARM

        def enqueue() -> bool:
            with self._store.transaction() as tx:
                return tx.enqueue_alarm(
                    code=code, text=_CONFLICT_ALARM_TEXT, now_ms=now_ms, throttle_ms=throttle_ms
                )

        try:
            enqueued = await asyncio.to_thread(enqueue)
        except StoreError as error:
            self._conflict_alarm_ms = None  # try again at the next conflict
            _log.error("telegram_conflict_alarm_failed", error_type=type(error).__name__)
            return
        if enqueued:
            self.wake_sender()

    def _check_polling(self) -> None:
        task = self._polling_task
        if task is None or not task.done() or self._stopping:
            return
        error = None if task.cancelled() else task.exception()
        self._polling_task = None
        self._fatal_reason = _FATAL_TOKEN if isinstance(error, InvalidToken) else _FATAL_POLLING
        _log.critical(
            "telegram_polling_stopped",
            error_type="cancelled" if error is None else type(error).__name__,
        )

    # -- life cycle --------------------------------------------------------------------------

    def _raise_if_fatal(self) -> None:
        # Raised here, outside any `except` block: the error carries neither a cause nor a
        # context, so no traceback of it can show python-telegram-bot's token-quoting text.
        if self._fatal_reason is not None:
            raise TelegramFatalError(self._fatal_reason) from None

    def _connect_failed(self, event: str, error: TelegramError) -> None:
        delay_ms = _backoff_ms(self._connect_failures)
        self._connect_failures += 1
        self._next_connect_ms = self._clock() + delay_ms
        _log.warning(event, error_type=type(error).__name__, retry_in_ms=delay_ms)

    async def _ensure_bot(self) -> bool:
        """Initialize the bot (one `getMe`) if that has not worked yet; False while it has not."""
        if self._bot_ready:
            return True
        if self._clock() < self._next_connect_ms:
            return False
        try:
            await self.application.initialize()
        except InvalidToken:
            self._fatal_reason = _FATAL_TOKEN
            return False
        except TelegramError as error:
            self._connect_failed("telegram_connect_failed", error)
            return False
        self._bot_ready = True
        self._connect_failures = 0
        _log.info("telegram_ready")
        return True

    async def _ensure_started(self) -> None:
        """Start update processing and long polling once the bot is ready."""
        if self._started or not self._bot_ready or self._fatal_reason is not None:
            return
        if self._clock() < self._next_connect_ms:
            return
        application = self.application
        updater = application.updater
        try:
            if not application.running:
                await application.start()
            if self._polling and updater is not None and not updater.running:
                # Commands typed while the bot was down are processed (never dropped);
                # `Commands` refuses the ones that are unsafe when old.
                await updater.start_polling(
                    allowed_updates=_ALLOWED_UPDATES,
                    drop_pending_updates=False,
                    error_callback=self._on_polling_error,
                )
        except InvalidToken:
            self._fatal_reason = _FATAL_TOKEN
            return
        except TelegramError as error:
            self._connect_failed("telegram_polling_start_failed", error)
            return
        self._started = True
        self._connect_failures = 0
        if self._polling:
            self._polling_task = next(
                (task for task in asyncio.all_tasks() if task.get_name() == _POLLING_TASK_NAME),
                None,
            )
            if self._polling_task is None:
                _log.warning("telegram_polling_task_not_found")
            else:
                # If polling ever ends on its own, `run` must notice at once.
                self._polling_task.add_done_callback(lambda _task: self._wake.set())
        _log.info("telegram_started", polling=self._polling)

    async def _idle(self, stop: asyncio.Event) -> None:
        """Wait for a wake-up, for `stop`, or for the idle poll interval."""
        waiters = [asyncio.ensure_future(self._wake.wait()), asyncio.ensure_future(stop.wait())]
        try:
            await asyncio.wait(
                waiters, timeout=_IDLE_POLL_SECONDS, return_when=asyncio.FIRST_COMPLETED
            )
        finally:
            for waiter in waiters:
                waiter.cancel()
            await asyncio.gather(*waiters, return_exceptions=True)
        self._wake.clear()

    async def _shutdown(self) -> None:
        """Stop polling and release the connections. Never raises (it runs in a `finally`)."""
        application = self.application
        updater = application.updater
        steps = (
            ("updater_stop", updater is not None and updater.running, self._stop_updater),
            ("application_stop", application.running, application.stop),
            ("application_shutdown", True, application.shutdown),
            # `Application.shutdown` skips the bot when `initialize` never completed.
            ("bot_shutdown", True, application.bot.shutdown),
        )
        for name, wanted, step in steps:
            if not wanted:
                continue
            try:
                await step()
            except Exception as error:
                # A polling task that died of InvalidToken re-raises here: the type only.
                _log.warning(
                    "telegram_shutdown_step_failed", step=name, error_type=type(error).__name__
                )
        if self._background:
            await asyncio.gather(*self._background, return_exceptions=True)
        self._bot_ready = False
        self._started = False
        _log.info("telegram_stopped")

    async def _stop_updater(self) -> None:
        updater = self.application.updater
        if updater is None:
            return
        # `stop` cancels a request in flight at once, but after an error python-telegram-bot
        # sleeps up to 30 s between polls and `stop` waits for that sleep. Give the clean path
        # a moment, then cancel the sleeping task (nothing is in flight in that state).
        stopping = asyncio.ensure_future(updater.stop())
        done, _pending = await asyncio.wait({stopping}, timeout=_POLLING_STOP_GRACE_SECONDS)
        task = self._polling_task
        if not done and task is not None and not task.done():
            task.cancel()
        await stopping
