"""Offline Telegram for tests: a `BaseRequest` fake, update builders, small store scenarios.

Nothing here opens a socket. `FakeRequest` stands where python-telegram-bot's HTTP layer
would be, so the real `Bot` / `Application` code runs: parameter serialisation, error
mapping (HTTP status -> `telegram.error` class) and update parsing are the library's own.

Typical use::

    fake = FakeRequest()
    service = TelegramService(bot_token=BOT_TOKEN, allowed_chat_id=CHAT_ID, ...,
                              request=fake, get_updates_request=fake, polling=False)
    fake.script("sendMessage", TimedOut())                 # next sendMessage raises
    fake.script("sendMessage", retry_after_response(7))    # the one after answers HTTP 429
    await service.application.initialize()
    await service.application.process_update(
        Update.de_json(private_update("/durum", sent_at_ms=NOW_MS), service.application.bot))
    await service.send_due()
    assert fake.sent_texts() == [...]
    assert fake.chat_ids() == {CHAT_ID}

Things to know:
- Create the fake, the service and everything asyncio inside ONE `asyncio.run(...)`.
- `retry_after_response` makes python-telegram-bot build a `RetryAfter`, which emits a
  `PTBDeprecationWarning` unless the environment has `PTB_TIMEDELTA=true`; under pytest's
  `filterwarnings = error` set it with `monkeypatch.setenv("PTB_TIMEDELTA", "true")`.
- With polling enabled, `getUpdates` blocks until `push_update` supplies an update (a
  long poll that never times out); a call with `timeout=0` returns an empty list at once.
"""

import asyncio
import dataclasses
import itertools
import json
from collections import deque
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from typing import Any, Final

from telegram.request import BaseRequest, RequestData

from propbot.store.db import NewSignal, OutboxKind, Outcome, Store
from support.alerts import BAR_TIME_MS, SYMBOL

# Shaped like real values (TELEGRAM_BOT_TOKEN ^[0-9]+:[A-Za-z0-9_-]{20,}$); not secrets.
BOT_ID: Final = 7000000001
BOT_TOKEN: Final = f"{BOT_ID}:AAtest-token_0123456789_abcdefghijklmn"
BOT_USERNAME: Final = "propbot_test_bot"
CHAT_ID: Final = 4242  # the whitelisted private chat (= the user's id)
OTHER_USER_ID: Final = 5151
GROUP_ID: Final = -1001234567890
FIRST_MESSAGE_ID: Final = 1000  # ids the fake gives to messages the bot sends

type Response = tuple[int, bytes]
"""HTTP status and body, as `BaseRequest.do_request` returns them."""
type Hook = Callable[["Call"], Awaitable[Response | None]]
type Scripted = BaseException | Response | Hook


@dataclass(frozen=True, slots=True)
class Call:
    """One request the bot made: Bot API method name and its JSON parameters."""

    endpoint: str
    params: dict[str, Any]


def ok(result: object) -> Response:
    """A successful Bot API answer carrying `result`."""
    return 200, json.dumps({"ok": True, "result": result}).encode()


def error_response(status: int, description: str = "error", **parameters: object) -> Response:
    """A Bot API error. The library maps the status: 400 BadRequest, 401/404 InvalidToken,
    403 Forbidden, 409 Conflict, anything else NetworkError."""
    body: dict[str, object] = {"ok": False, "error_code": status, "description": description}
    if parameters:
        body["parameters"] = parameters
    return status, json.dumps(body).encode()


def retry_after_response(seconds: int) -> Response:
    """Flood control (HTTP 429): the library raises `RetryAfter` (see the module docstring)."""
    return error_response(429, f"Too Many Requests: retry after {seconds}", retry_after=seconds)


class FakeRequest(BaseRequest):
    """Records every call and answers from a per-endpoint script, else with a default.

    Scripted outcomes are consumed in order, one per call of that endpoint:
    - an exception instance is raised (a `TelegramError` reaches the caller as it is; any
      other exception is wrapped by the library in `NetworkError`),
    - a `Response` (`ok`, `error_response`, `retry_after_response`) is returned,
    - an async callable gets the `Call`; it may wait (to hold a request open) and returns
      a `Response`, or None for the default answer.
    """

    def __init__(self) -> None:
        self.calls: list[Call] = []
        self.now_s = BAR_TIME_MS // 1000  # `date` of the messages the fake "sends"
        self._scripts: dict[str, deque[Scripted]] = {}
        self._updates: asyncio.Queue[dict[str, Any]] = asyncio.Queue()
        self._message_ids = itertools.count(FIRST_MESSAGE_ID)

    # -- arranging ---------------------------------------------------------------------------

    def script(self, endpoint: str, *outcomes: Scripted) -> None:
        """Queue outcomes for the next calls of `endpoint` (e.g. "sendMessage")."""
        self._scripts.setdefault(endpoint, deque()).extend(outcomes)

    def push_update(self, update: dict[str, Any]) -> None:
        """Deliver an update through long polling (the service must run with polling)."""
        self._updates.put_nowait(update)

    # -- asserting ---------------------------------------------------------------------------

    def calls_to(self, endpoint: str) -> list[dict[str, Any]]:
        return [call.params for call in self.calls if call.endpoint == endpoint]

    def sent_texts(self) -> list[str]:
        """Text of every `sendMessage` attempt, in order (failed attempts included)."""
        return [params["text"] for params in self.calls_to("sendMessage")]

    def chat_ids(self) -> set[int]:
        """Every `chat_id` the bot addressed, whatever the method."""
        return {call.params["chat_id"] for call in self.calls if "chat_id" in call.params}

    # -- BaseRequest -------------------------------------------------------------------------

    @property
    def read_timeout(self) -> float | None:
        return 1.0

    async def initialize(self) -> None:
        return None

    async def shutdown(self) -> None:
        return None

    async def do_request(
        self,
        url: str,
        method: str,
        request_data: RequestData | None = None,
        read_timeout: object = None,
        write_timeout: object = None,
        connect_timeout: object = None,
        pool_timeout: object = None,
    ) -> Response:
        endpoint = url.rsplit("/", 1)[-1]
        # Round trip through JSON: plain dicts, lists, strings and numbers, as on the wire.
        params: dict[str, Any] = (
            {} if request_data is None else json.loads(json.dumps(request_data.parameters))
        )
        call = Call(endpoint, params)
        self.calls.append(call)
        scripted = self._scripts.get(endpoint)
        if scripted:
            outcome = scripted.popleft()
            if isinstance(outcome, BaseException):
                raise outcome
            if isinstance(outcome, tuple):
                return outcome
            answer = await outcome(call)
            if answer is not None:
                return answer
        return ok(await self._default_result(call))

    async def _default_result(self, call: Call) -> object:
        if call.endpoint == "getMe":
            return {"id": BOT_ID, "is_bot": True, "first_name": "PropBot", "username": BOT_USERNAME}
        if call.endpoint == "sendMessage":
            return {
                "message_id": next(self._message_ids),
                "date": self.now_s,
                "chat": {"id": call.params["chat_id"], "type": "private"},
                "text": call.params["text"],
            }
        if call.endpoint == "getUpdates":
            if not call.params.get("timeout"):
                return []
            return [await self._updates.get()]
        return True  # deleteMessage, deleteWebhook, ...


# --------------------------------------------------------------------------------------------
# Incoming updates (plain dicts, as the Bot API sends them)
# --------------------------------------------------------------------------------------------

_update_ids = itertools.count(1)
_incoming_message_ids = itertools.count(1)


def message_update(
    text: str,
    *,
    sent_at_ms: int,
    chat_id: int = CHAT_ID,
    chat_type: str = "private",
    user_id: int | None = None,
    message_id: int | None = None,
) -> dict[str, Any]:
    """A `message` update. `user_id` defaults to `chat_id` (true for every private chat).

    Telegram stamps whole seconds: `sent_at_ms` is truncated to the second.
    """
    return {
        "update_id": next(_update_ids),
        "message": {
            "message_id": next(_incoming_message_ids) if message_id is None else message_id,
            "date": sent_at_ms // 1000,
            "chat": {"id": chat_id, "type": chat_type},
            "from": {
                "id": chat_id if user_id is None else user_id,
                "is_bot": False,
                "first_name": "Test",
            },
            "text": text,
        },
    }


def private_update(text: str, *, sent_at_ms: int, message_id: int | None = None) -> dict[str, Any]:
    """A message from the whitelisted user in their private chat with the bot."""
    return message_update(text, sent_at_ms=sent_at_ms, message_id=message_id)


def other_user_update(
    text: str, *, sent_at_ms: int, user_id: int = OTHER_USER_ID
) -> dict[str, Any]:
    """A message from somebody else's private chat."""
    return message_update(text, sent_at_ms=sent_at_ms, chat_id=user_id)


def group_update(
    text: str, *, sent_at_ms: int, chat_id: int = GROUP_ID, user_id: int = CHAT_ID
) -> dict[str, Any]:
    """A group message, by default written by the whitelisted user.

    Pass `chat_id=CHAT_ID` for the "right id, wrong chat type" case.
    """
    return message_update(
        text, sent_at_ms=sent_at_ms, chat_id=chat_id, chat_type="group", user_id=user_id
    )


# --------------------------------------------------------------------------------------------
# Store scenarios: what the processor leaves behind for a forwarded entry
# --------------------------------------------------------------------------------------------

MINUTE_MS: Final = 60_000

ENTRY_SIGNAL: Final = NewSignal(
    received_at_ms=BAR_TIME_MS + MINUTE_MS + 1_500,
    strategy_id="ny_orb_fvg",
    symbol=SYMBOL,
    tf="1",
    bar_time_ms=BAR_TIME_MS,
    bar_close_ms=BAR_TIME_MS + MINUTE_MS,
    action="entry",
    side="long",
    setup="FVG",
    order_type="market",
    est_entry=5123.25,
    stop=5110.75,
    tp=5148.25,
    rr=2.0,
    reason=None,
    payload_json='{"action":"entry","v":1}',
)


def add_signal(
    store: Store, *, outcome: Outcome | None = Outcome.FORWARDED, **overrides: Any
) -> int:
    """Insert `ENTRY_SIGNAL` with field overrides and complete it; return the signal id.

    `outcome=None` leaves it queued. The idempotency key must be new: vary `bar_time_ms`
    (or `strategy_id` / `action`).
    """
    signal = dataclasses.replace(ENTRY_SIGNAL, **overrides)
    with store.transaction() as tx:
        signal_id = tx.insert_signal(signal)
        assert signal_id is not None, "duplicate idempotency key"
        if outcome is not None:
            tx.complete_signal(signal_id, outcome, signal.received_at_ms)
    return signal_id


def forwarded_entry(
    store: Store,
    *,
    prop_day: Any,
    now_ms: int,
    risk_amount: float = 250.0,
    expires_at_ms: int | None = None,
    text: str | None = None,
    delivered: bool = False,
    **overrides: Any,
) -> tuple[int, int]:
    """A forwarded entry as the processor writes it: signal + budget row + SIGNAL message.

    Returns `(signal_id, outbox_id)`. The message is pending unless `delivered=True`
    (then it is marked sent at `now_ms`). `expires_at_ms` defaults to one minute after the
    bar close; the expiry text names the signal id.
    """
    signal_id = add_signal(store, **overrides)
    with store.transaction() as tx:
        signal = tx.get_signal(signal_id)
        assert signal is not None
        tx.add_budget_entry(
            signal_id=signal_id, prop_day=prop_day, risk_amount=risk_amount, now_ms=now_ms
        )
        outbox_id = tx.enqueue_message(
            kind=OutboxKind.SIGNAL,
            text=f"[SİNYAL] id {signal_id}" if text is None else text,
            now_ms=now_ms,
            signal_id=signal_id,
            expires_at_ms=(
                signal.bar_close_ms + MINUTE_MS if expires_at_ms is None else expires_at_ms
            ),
            expired_text=f"[İLETİLEMEDİ — ATLA] id {signal_id}",
        )
        if delivered:
            tx.mark_message_sent(outbox_id, now_ms)
    return signal_id, outbox_id
