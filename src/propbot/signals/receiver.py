"""TradingView webhook receiver (SPEC §5.2, §5.3): validate, store, answer 200 at once.

Checks run in a fixed order and the first failure answers:

    1. client IP   resolved from the TCP peer or, behind a trusted proxy, from its header
    2. body size   refused from Content-Length, else while streaming
    3. JSON        exactly one object, nothing ambiguous
    4. token       constant-time comparison with the shared secret
    5. rate limit  counted over token-valid requests only
    6. schema      `TvAlert`
    7. insert      UNIQUE (strategy_id, symbol, bar_time, action) makes duplicates harmless

The source addresses belong to TradingView, not to one user: every TradingView customer
can reach step 2. That is why the rate limit sits behind the token (a stranger must not
be able to spend it and get our own alerts answered 429) and why the work before the
token is bounded: at most `max_body_bytes` read and parsed, no thread hop, no database.
The only database write an unauthenticated request can cause is one throttled alarm row
per hour, because a wrong token after a rotation would otherwise fail silently.

The token never leaves this function: it is popped from the decoded body, compared, and
dropped; rejections log an error code and the client address, never the body.

Processing (size suggestion, budget, Telegram) happens later in `signals.processor`; this
module only decides whether an alert is authentic and well-formed, and stores it.
"""

import asyncio
import hmac
from collections import deque
from collections.abc import AsyncIterator, Callable, Mapping, Sequence
from dataclasses import dataclass, field
from enum import StrEnum
from ipaddress import IPv4Address, IPv6Address, ip_address
from typing import Final

import structlog
from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse
from starlette.types import Lifespan

from propbot.core.clock import Clock
from propbot.notify.format import fmt_duration
from propbot.signals.schema import (
    STRATEGY_NAMES,
    AlertError,
    TvAlert,
    canonical_json,
    decode_json_object,
    pop_token,
    validate_alert,
)
from propbot.signals.tvconfig import ReceiverConfig, TvAlertsConfig
from propbot.store.db import NewSignal, Store, StoreError

WEBHOOK_PATH: Final = "/tv/webhook"
HEALTH_PATH: Final = "/healthz"

_MILLIS_PER_SECOND: Final = 1000
_MINUTE_MS: Final = 60 * _MILLIS_PER_SECOND
_RATE_WINDOW_MS: Final = _MINUTE_MS
# One log line per rejection code in this interval; the rest are counted and reported with it.
_LOG_THROTTLE_MS: Final = 10 * _MILLIS_PER_SECOND
# An alert this much ahead of its computed bar close points at a slow server clock (or a bar
# cut short by the session end).
_EARLY_TOLERANCE_MS: Final = 2 * _MILLIS_PER_SECOND

# Alarm codes (throttle keys in the store) and how often each may reach Telegram.
_BAD_SCHEMA_ALARM: Final = ("bad_schema", 10 * _MINUTE_MS)
_RATE_LIMIT_ALARM: Final = ("rate_limited", 10 * _MINUTE_MS)
_EARLY_ALARM: Final = ("clock_early", 60 * _MINUTE_MS)
_UNAUTHENTICATED_ALARM: Final = ("webhook_unauthenticated", 60 * _MINUTE_MS)
# When that alarm cannot be enqueued (the store still throttles it after a restart, or the
# store failed), the collected counts are kept and offered again after this long.
_UNAUTHENTICATED_RETRY_MS: Final = 5 * _MINUTE_MS

type IPAddress = IPv4Address | IPv6Address

_log = structlog.get_logger(__name__)


class RejectCode(StrEnum):
    IP_NOT_ALLOWED = "ip_not_allowed"
    RATE_LIMITED = "rate_limited"
    BODY_TOO_LARGE = "body_too_large"
    BAD_JSON = "bad_json"
    BAD_TOKEN = "bad_token"
    BAD_SCHEMA = "bad_schema"
    STORE_ERROR = "store_error"


# code -> (HTTP status, value of "error" in the answer). The answer names the class of the
# problem only: it never says which field or which check exactly.
_REJECTIONS: Final[Mapping[RejectCode, tuple[int, str]]] = {
    RejectCode.IP_NOT_ALLOWED: (403, "forbidden"),
    RejectCode.RATE_LIMITED: (429, "rate_limited"),
    RejectCode.BODY_TOO_LARGE: (413, "too_large"),
    RejectCode.BAD_JSON: (400, "bad_json"),
    RejectCode.BAD_TOKEN: (401, "unauthorized"),
    RejectCode.BAD_SCHEMA: (422, "bad_schema"),
    RejectCode.STORE_ERROR: (503, "unavailable"),
}


class RejectStats:
    """Rejection counters since process start (in memory: rejections never touch the database)."""

    def __init__(self) -> None:
        self._counts: dict[RejectCode, int] = dict.fromkeys(RejectCode, 0)
        self._last_ms: dict[RejectCode, int] = {}

    def record(self, code: RejectCode, now_ms: int) -> None:
        self._counts[code] += 1
        self._last_ms[code] = now_ms

    def snapshot(self) -> dict[str, int]:
        """Count per code value; every code is present."""
        return {code.value: count for code, count in self._counts.items()}

    def last_ms(self, code: RejectCode) -> int | None:
        return self._last_ms.get(code)


class SlidingWindowLimiter:
    """At most `limit` accepted calls in any `window_ms` (SPEC §5.3: 30 requests per minute)."""

    def __init__(self, limit: int, window_ms: int = _RATE_WINDOW_MS) -> None:
        if limit < 1 or window_ms < 1:
            raise ValueError("limit and window_ms must be positive")
        self._limit = limit
        self._window_ms = window_ms
        self._accepted: deque[int] = deque()

    def allow(self, now_ms: int) -> bool:
        # Entries stamped after `now_ms` (the clock stepped back) stay and keep counting.
        while self._accepted and now_ms - self._accepted[0] >= self._window_ms:
            self._accepted.popleft()
        if len(self._accepted) >= self._limit:
            return False
        self._accepted.append(now_ms)
        return True


class _LogThrottle:
    """Lets one log line per key through each interval and counts what it held back."""

    def __init__(self, interval_ms: int) -> None:
        self._interval_ms = interval_ms
        self._last_ms: dict[str, int] = {}
        self._suppressed: dict[str, int] = {}

    def due(self, key: str, now_ms: int) -> int | None:
        """Return how many lines were held back since the last one, or None to hold this one."""
        last = self._last_ms.get(key)
        if last is not None and 0 <= now_ms - last < self._interval_ms:
            self._suppressed[key] = self._suppressed.get(key, 0) + 1
            return None
        self._last_ms[key] = now_ms
        return self._suppressed.pop(key, 0)


class _AlarmGate:
    """Collects unauthenticated rejections and decides when the user is told about them.

    At most one alarm per interval, whatever the request rate, and it lives in memory so a
    flood costs one database transaction per interval. Counts are never lost: what arrives
    while the gate is closed goes out with the next alarm, and ANY later request from an
    allow-listed address (also a valid one, such as the hourly heartbeat) triggers that
    alarm once the interval has passed. A stale token on one of our own alerts therefore
    cannot hide behind somebody else's junk request that happened to open the gate first.
    """

    def __init__(self, interval_ms: int, retry_ms: int) -> None:
        self._interval_ms = interval_ms
        self._retry_ms = retry_ms
        self._closed_until_ms: int | None = None
        self._counts: dict[RejectCode, int] = {}

    def record(self, code: RejectCode) -> None:
        self._counts[code] = self._counts.get(code, 0) + 1

    def take_due(self, now_ms: int) -> dict[RejectCode, int] | None:
        """Hand over the pending counts when an alarm may go out now, else None."""
        if not self._counts:
            return None
        closed_until = self._closed_until_ms
        # A deadline further away than one interval means the clock stepped back: open.
        if closed_until is not None and 0 < closed_until - now_ms <= self._interval_ms:
            return None
        counts, self._counts = self._counts, {}
        return counts

    def sent(self, now_ms: int) -> None:
        """The alarm went out: stay closed for one interval."""
        self._closed_until_ms = now_ms + self._interval_ms

    def put_back(self, counts: Mapping[RejectCode, int], now_ms: int) -> None:
        """The alarm could not be enqueued (store throttle or failure): keep the counts."""
        for code, count in counts.items():
            self._counts[code] = self._counts.get(code, 0) + count
        self._closed_until_ms = now_ms + self._retry_ms


def parse_ip_strict(text: str) -> IPAddress | None:
    """Parse one IP address literal; anything else (port, brackets, zone id, spaces) is None.

    An IPv4-mapped IPv6 address (`::ffff:1.2.3.4`) is returned as the IPv4 address it wraps,
    so it compares equal to the same address written as IPv4.
    """
    try:
        address = ip_address(text)
    except ValueError:
        return None
    if isinstance(address, IPv6Address):
        if address.scope_id is not None:
            return None
        if address.ipv4_mapped is not None:
            return address.ipv4_mapped
    return address


def resolve_client_ip(
    peer: str | None, header_values: Sequence[str], receiver: ReceiverConfig
) -> IPAddress | None:
    """Return the address the request really came from, or None when it cannot be known.

    If the TCP peer is a trusted proxy, the proxy's client-IP header is REQUIRED and its
    right-most comma-separated entry is the client (each proxy appends the peer it saw, so
    only the last entry was written by the proxy we trust). Otherwise the peer itself is
    the client and the header, which anybody can send, is ignored.
    """
    if peer is None:
        return None
    peer_address = parse_ip_strict(peer)
    if peer_address is None:
        return None
    if not any(peer_address in network for network in receiver.trusted_networks):
        return peer_address
    entries = [entry.strip(" \t") for value in header_values for entry in value.split(",")]
    if not entries:
        return None
    return parse_ip_strict(entries[-1])


@dataclass(frozen=True, slots=True, kw_only=True)
class ReceiverDeps:
    """Everything the receiver needs from the rest of the process."""

    store: Store
    webhook_token: str = field(repr=False)  # never printed with the object
    tv: TvAlertsConfig
    validity_seconds: int  # risk.signal_validity_seconds
    clock: Clock
    on_queued: Callable[[], None]  # a signal was stored: wake the processor
    on_messages: Callable[[], None]  # an alarm was put in the outbox: wake the sender
    stats: RejectStats


def create_app(deps: ReceiverDeps, *, lifespan: Lifespan[FastAPI] | None = None) -> FastAPI:
    """Build the ASGI app: `POST /tv/webhook` and a local-only `GET /healthz`."""
    if not deps.webhook_token:
        raise ValueError("webhook_token must not be empty")
    receiver = deps.tv.receiver
    trusted_header = receiver.client_ip_header
    source_addresses = deps.tv.webhook.source_addresses
    expected_token = deps.webhook_token.encode("utf-8")
    validity_ms = deps.validity_seconds * _MILLIS_PER_SECOND
    slow_ms = deps.tv.webhook.response_timeout_seconds * _MILLIS_PER_SECOND / 2
    rate_limit = receiver.rate_limit_per_minute
    limiter = SlidingWindowLimiter(rate_limit)
    log_throttle = _LogThrottle(_LOG_THROTTLE_MS)
    unauthenticated_gate = _AlarmGate(_UNAUTHENTICATED_ALARM[1], _UNAUTHENTICATED_RETRY_MS)

    app = FastAPI(lifespan=lifespan, docs_url=None, redoc_url=None, openapi_url=None)

    def enqueue_alarm(alarm: tuple[str, int], text: str, now_ms: int) -> bool:
        code, throttle_ms = alarm
        with deps.store.transaction() as tx:
            return tx.enqueue_alarm(code=code, text=text, now_ms=now_ms, throttle_ms=throttle_ms)

    async def alarm(kind: tuple[str, int], text: str, now_ms: int) -> bool:
        """Put a throttled alarm in the outbox and say whether it went in.

        A store failure must not change the answer to TradingView.
        """
        try:
            enqueued = await asyncio.to_thread(enqueue_alarm, kind, text, now_ms)
        except StoreError as error:
            _log.error("webhook_alarm_failed", code=kind[0], error_type=type(error).__name__)
            return False
        if enqueued:
            deps.on_messages()
        return enqueued

    async def report_unauthenticated(now_ms: int) -> None:
        """Send the alarm about rejections collected so far, if one is due."""
        counts = unauthenticated_gate.take_due(now_ms)
        if counts is None:
            return
        if await alarm(_UNAUTHENTICATED_ALARM, _unauthenticated_alarm_text(counts), now_ms):
            unauthenticated_gate.sent(now_ms)
        else:
            unauthenticated_gate.put_back(counts, now_ms)

    def reject(code: RejectCode, now_ms: int, client: str | None, **detail: object) -> JSONResponse:
        deps.stats.record(code, now_ms)
        suppressed = log_throttle.due(code.value, now_ms)
        if suppressed is not None:
            _log.warning(
                "webhook_rejected",
                code=code.value,
                client_ip=client,
                suppressed=suppressed,
                **detail,
            )
        status, error = _REJECTIONS[code]
        return JSONResponse({"error": error}, status_code=status)

    async def reject_unauthenticated(code: RejectCode, now_ms: int, client: str) -> JSONResponse:
        """Reject a request from an allow-listed address that did not prove it knows the token.

        It may be our own alert with a stale token or a broken body, so the user is told,
        but rarely: at most one alarm per hour whatever the request rate.
        """
        unauthenticated_gate.record(code)
        await report_unauthenticated(now_ms)
        return reject(code, now_ms, client)

    def insert(signal: NewSignal, early_ms: int) -> tuple[int | None, bool]:
        with deps.store.transaction() as tx:
            signal_id = tx.insert_signal(signal)
            alarmed = False
            if signal_id is not None and early_ms > _EARLY_TOLERANCE_MS:
                code, throttle_ms = _EARLY_ALARM
                alarmed = tx.enqueue_alarm(
                    code=code,
                    text=_early_alarm_text(signal, early_ms),
                    now_ms=signal.received_at_ms,
                    throttle_ms=throttle_ms,
                )
            return signal_id, alarmed

    @app.post(WEBHOOK_PATH)
    async def webhook(request: Request) -> JSONResponse:
        started_ms = deps.clock()
        peer = request.client.host if request.client is not None else None
        address = resolve_client_ip(peer, request.headers.getlist(trusted_header), receiver)
        if address is None or address not in source_addresses:
            return reject(
                RejectCode.IP_NOT_ALLOWED, started_ms, None if address is None else str(address)
            )
        client = str(address)

        raw = await _read_body(request, receiver.max_body_bytes)
        if raw is None:
            return await reject_unauthenticated(RejectCode.BODY_TOO_LARGE, started_ms, client)

        try:
            body = decode_json_object(raw)
        except AlertError:
            return await reject_unauthenticated(RejectCode.BAD_JSON, started_ms, client)

        token = pop_token(body)
        if not token or not hmac.compare_digest(token.encode("utf-8"), expected_token):
            return await reject_unauthenticated(RejectCode.BAD_TOKEN, started_ms, client)

        # From here on the sender is one of our own alerts. It also carries out a report
        # about earlier rejections that had to wait for the hourly gate.
        await report_unauthenticated(started_ms)
        if not limiter.allow(started_ms):
            await alarm(_RATE_LIMIT_ALARM, _rate_limit_alarm_text(rate_limit), started_ms)
            return reject(RejectCode.RATE_LIMITED, started_ms, client)

        try:
            alert = validate_alert(body)
        except AlertError as error:
            await alarm(_BAD_SCHEMA_ALARM, _bad_schema_alarm_text(error), started_ms)
            return reject(RejectCode.BAD_SCHEMA, started_ms, client, fields=list(error.fields))

        received_at_ms = deps.clock()
        signal = _new_signal(alert, received_at_ms)
        try:
            signal_id, alarmed = await asyncio.to_thread(
                insert, signal, signal.bar_close_ms - received_at_ms
            )
        except StoreError as error:
            return reject(
                RejectCode.STORE_ERROR, started_ms, client, error_type=type(error).__name__
            )

        finished_ms = deps.clock()
        if finished_ms - started_ms >= slow_ms:
            _log.warning("webhook_slow", elapsed_ms=finished_ms - started_ms, client_ip=client)
        if signal_id is None:
            _log.info(
                "webhook_duplicate",
                strategy_id=signal.strategy_id,
                action=signal.action,
                bar_time_ms=signal.bar_time_ms,
                client_ip=client,
            )
            return JSONResponse({"status": "duplicate"})

        deps.on_queued()
        if alarmed:
            deps.on_messages()
        stale = received_at_ms > signal.bar_close_ms + validity_ms
        _log.info(
            "webhook_accepted",
            signal_id=signal_id,
            strategy_id=signal.strategy_id,
            action=signal.action,
            bar_time_ms=signal.bar_time_ms,
            latency_ms=received_at_ms - signal.bar_close_ms,
            stale=stale,
            client_ip=client,
        )
        return JSONResponse({"status": "stale" if stale else "queued"})

    @app.get(HEALTH_PATH)
    async def healthz(request: Request) -> JSONResponse:
        # Local checks only: a request that came through the proxy carries its header.
        peer = request.client.host if request.client is not None else None
        address = parse_ip_strict(peer) if peer is not None else None
        through_proxy = bool(request.headers.getlist(trusted_header))
        if address is None or not address.is_loopback or through_proxy:
            return JSONResponse({"detail": "Not Found"}, status_code=404)
        return JSONResponse({"status": "ok"})

    return app


async def _read_body(request: Request, max_bytes: int) -> bytes | None:
    """Read the body, or return None as soon as it is known to exceed `max_bytes`."""
    declared = request.headers.get("content-length")
    declared_size = int(declared) if declared and declared.isascii() and declared.isdigit() else 0
    if declared_size > max_bytes:
        return None
    chunks: list[bytes] = []
    total = 0
    stream: AsyncIterator[bytes] = request.stream()
    async for chunk in stream:
        total += len(chunk)
        if total > max_bytes:
            return None
        chunks.append(chunk)
    return b"".join(chunks)


def _new_signal(alert: TvAlert, received_at_ms: int) -> NewSignal:
    return NewSignal(
        received_at_ms=received_at_ms,
        strategy_id=alert.strategy_id,
        symbol=alert.symbol,
        tf=alert.tf,
        bar_time_ms=alert.bar_time,
        bar_close_ms=alert.bar_close_ms,
        action=alert.action.value,
        side=None if alert.side is None else alert.side.value,
        setup=alert.setup,
        order_type=None if alert.order_type is None else alert.order_type.value,
        est_entry=alert.est_entry,
        stop=alert.stop,
        tp=alert.tp,
        rr=alert.rr,
        reason=alert.reason,
        payload_json=canonical_json(alert),
    )


def _bad_schema_alarm_text(error: AlertError) -> str:
    fields = ", ".join(error.fields) if error.fields else "?"
    return (
        f"[ALARM] Geçersiz alert gövdesi reddedildi (alanlar: {fields}).\n"
        "Token doğruydu, yani alert senin alarmından geldi: TradingView'daki script sürümünü, "
        "grafiğin zaman dilimini (günlük ve üstü desteklenmez) ve Alert Log'daki mesajı "
        "kontrol et.\nBu sinyal iletilmedi."
    )


def _rate_limit_alarm_text(limit: int) -> str:
    return (
        f"[ALARM] Kendi alarmların dakikada {limit} istekten fazlasını gönderiyor; fazlası "
        "reddedildi (429) ve İLETİLMEDİ.\n"
        "Bir TradingView alarmı döngüye girmiş ya da aynı alarm birden çok kez kurulmuş "
        "olabilir: Alerts panelini ve Alert Log'u kontrol et."
    )


def _unauthenticated_alarm_text(counts: Mapping[RejectCode, int]) -> str:
    total = sum(counts.values())
    return (
        f"[ALARM] TradingView adreslerinden gelen {total} istek kimliği doğrulanamadan "
        f"reddedildi (yanlış/eksik token: {counts.get(RejectCode.BAD_TOKEN, 0)}, "
        f"bozuk JSON: {counts.get(RejectCode.BAD_JSON, 0)}, "
        f"fazla büyük gövde: {counts.get(RejectCode.BODY_TOO_LARGE, 0)}).\n"
        "Token'ı yeni değiştirdiysen ya da bir script'e yanlış yapıştırdıysan o alarmın "
        "sinyalleri GELMİYOR: TradingView'da her alarmın 'Webhook token' girdisini kontrol et "
        "ve alarmı yeniden kur.\n"
        "Bozuk JSON, grafik ayarı yüzünden sembol adının düz BORSA:SEMBOL biçiminde "
        "olmamasından kaynaklanabilir (Alert Log'daki mesaja bak).\n"
        "Hiçbiri değilse başka bir TradingView kullanıcısı adresini deniyor olabilir; "
        "sinyallerin etkilenmez. Bu uyarı en fazla saatte bir gelir."
    )


def _early_alarm_text(signal: NewSignal, early_ms: int) -> str:
    name = STRATEGY_NAMES.get(signal.strategy_id, signal.strategy_id)
    early = fmt_duration(early_ms / _MILLIS_PER_SECOND)
    return (
        f"[UYARI] {name} alert'i ({signal.tf} bar) hesaplanan bar kapanışından {early} "
        "ÖNCE geldi.\n"
        "Sunucu saati geride olabilir (chronyc tracking ile kontrol et) ya da bar seans sonunda "
        "kısaldı. Saat kaymışsa bayat sinyaller taze görünebilir."
    )
