"""Process wiring for `propbot serve` (ADR-0004): one event loop, one database, four tasks.

    uvicorn (FastAPI receiver)  ->  SQLite queue  ->  processor  ->  outbox  ->  Telegram
                                                         ^ alert-line monitor

The receiver answers TradingView; the processor, the alert-line monitor and the Telegram
service (polling + outbox sender) run as background tasks started by the app's lifespan.
If one of them ends while the service is running, the whole process stops with a
non-zero exit code and systemd starts it again: a bot that looks alive but no longer
forwards signals is the failure this is built to avoid. Everything durable lives in
the store, so a restart continues where the last process stopped.

Nothing here can place, change or cancel an order: the mode is `signal` (ADR-0000).
"""

import asyncio
import ipaddress
import os
import sys
import threading
from collections.abc import AsyncIterator, Callable, Mapping
from contextlib import asynccontextmanager, suppress
from dataclasses import dataclass, field
from types import TracebackType
from typing import Final

import structlog
import uvicorn
from fastapi import FastAPI
from starlette.types import ASGIApp
from telegram.request import BaseRequest

from propbot.core.clock import Clock, system_clock_ms
from propbot.notify.commands import CommandDeps, Commands
from propbot.notify.telegram import TelegramService
from propbot.risk.config import RiskConfig
from propbot.risk.venue import VenueProfile
from propbot.settings import Settings
from propbot.signals.heartbeat import run_heartbeat_monitor
from propbot.signals.processor import ProcessorConfig, run_processor
from propbot.signals.receiver import ReceiverDeps, RejectStats, create_app
from propbot.signals.tvconfig import TvAlertsConfig
from propbot.store.db import Store

EXIT_OK: Final = 0
EXIT_FAILED: Final = 1
# How long background tasks get to finish their current step when the service stops.
_SHUTDOWN_GRACE_SECONDS: Final = 10.0

_log = structlog.get_logger(__name__)


class ServiceError(Exception):
    """The service cannot start as asked. The message is user-facing (Turkish)."""


@dataclass(frozen=True, slots=True, kw_only=True)
class ServiceParts:
    """Everything a `Service` is built from. Tests replace the clock and the Telegram transport."""

    settings: Settings = field(repr=False)
    risk: RiskConfig
    venue: VenueProfile
    venue_name: str
    tv: TvAlertsConfig
    store: Store
    clock: Clock = system_clock_ms
    telegram_request: BaseRequest | None = None
    telegram_updates_request: BaseRequest | None = None
    polling: bool = True


class Service:
    """The receiver app and its background tasks.

    `app` is a complete ASGI application: entering its lifespan starts the tasks, leaving
    it stops them. `on_failure` is called once when a task ends on its own (the caller
    then shuts the server down); `failure` names the task that ended.
    """

    def __init__(self, parts: ServiceParts, *, on_failure: Callable[[], None] | None = None):
        self._parts = parts
        self._on_failure = on_failure
        self._wake_processor = asyncio.Event()
        self.failure: str | None = None
        self.stats = RejectStats()
        started_ms = parts.clock()
        self._started_ms = started_ms
        settings = parts.settings
        self.commands = Commands(
            CommandDeps(
                store=parts.store,
                risk=parts.risk,
                venue=parts.venue,
                venue_name=parts.venue_name,
                tv=parts.tv,
                pin=settings.propbot_pin.get_secret_value(),
                clock=parts.clock,
                started_ms=started_ms,
                reject_counts=self.stats.snapshot,
            )
        )
        self.telegram = TelegramService(
            bot_token=settings.telegram_bot_token.get_secret_value(),
            allowed_chat_id=settings.telegram_allowed_chat_id,
            commands=self.commands,
            store=parts.store,
            clock=parts.clock,
            validity_seconds=parts.risk.signal_validity_seconds,
            request=parts.telegram_request,
            get_updates_request=parts.telegram_updates_request,
            polling=parts.polling,
        )
        self.app = create_app(
            ReceiverDeps(
                store=parts.store,
                webhook_token=settings.tv_webhook_token.get_secret_value(),
                tv=parts.tv,
                validity_seconds=parts.risk.signal_validity_seconds,
                clock=parts.clock,
                on_queued=self._wake_processor.set,
                on_messages=self.telegram.wake_sender,
                stats=self.stats,
            ),
            lifespan=self._lifespan,
        )

    @asynccontextmanager
    async def _lifespan(self, _app: FastAPI) -> AsyncIterator[None]:
        parts = self._parts
        stop = asyncio.Event()
        tasks: dict[str, asyncio.Task[None]] = {
            "processor": asyncio.create_task(
                run_processor(
                    store=parts.store,
                    config=ProcessorConfig(risk=parts.risk, venue=parts.venue),
                    clock=parts.clock,
                    wake=self._wake_processor,
                    on_messages=self.telegram.wake_sender,
                    stop=stop,
                ),
                name="processor",
            ),
            "monitor": asyncio.create_task(
                run_heartbeat_monitor(
                    store=parts.store,
                    config=parts.tv,
                    clock=parts.clock,
                    started_ms=self._started_ms,
                    on_messages=self.telegram.wake_sender,
                    stop=stop,
                ),
                name="monitor",
            ),
            "telegram": asyncio.create_task(self.telegram.run(stop), name="telegram"),
        }
        supervisor = asyncio.create_task(self._supervise(tasks, stop), name="supervisor")
        _log.info("service_started", tasks=sorted(tasks))
        try:
            yield
        finally:
            stop.set()
            self._wake_processor.set()
            self.telegram.wake_sender()
            _done, pending = await asyncio.wait(tasks.values(), timeout=_SHUTDOWN_GRACE_SECONDS)
            for task in pending:
                task.cancel()
            supervisor.cancel()
            await asyncio.gather(*tasks.values(), supervisor, return_exceptions=True)
            _log.info("service_stopped", cancelled=sorted(task.get_name() for task in pending))

    async def _supervise(
        self, tasks: Mapping[str, asyncio.Task[None]], stop: asyncio.Event
    ) -> None:
        """Wait until a task ends; if the service was not stopping, that is a failure."""
        done, _pending = await asyncio.wait(tasks.values(), return_when=asyncio.FIRST_COMPLETED)
        if stop.is_set():
            return
        for task in done:
            error = None if task.cancelled() else task.exception()
            self.failure = task.get_name()
            # exc_info goes through the redacting formatter; nothing is printed raw.
            _log.critical(
                "service_task_died",
                task=task.get_name(),
                error_type="cancelled" if task.cancelled() else type(error).__name__,
                exc_info=error,
            )
        if self._on_failure is not None:
            self._on_failure()


def is_loopback_host(host: str) -> bool:
    """True for a literal loopback address. Names such as "localhost" are not accepted."""
    try:
        return ipaddress.ip_address(host).is_loopback
    except ValueError:
        return False


def install_exception_logging() -> None:
    """Send uncaught exceptions through the (redacting) logger instead of a raw traceback.

    The interpreter's default hooks print exception text to stderr as it is, and library
    exceptions can carry a secret (python-telegram-bot's InvalidToken quotes the bot token).
    """

    def log_uncaught(
        kind: type[BaseException], error: BaseException, _traceback: TracebackType | None
    ) -> None:
        if issubclass(kind, KeyboardInterrupt):
            return
        _log.critical("uncaught_exception", error_type=kind.__name__, exc_info=error)

    def log_thread(args: threading.ExceptHookArgs) -> None:
        if args.exc_value is None or issubclass(args.exc_type, SystemExit):
            return
        thread = args.thread.name if args.thread is not None else None
        _log.critical(
            "uncaught_thread_exception",
            thread=thread,
            error_type=args.exc_type.__name__,
            exc_info=args.exc_value,
        )

    sys.excepthook = log_uncaught
    threading.excepthook = log_thread


def _log_loop_error(_loop: asyncio.AbstractEventLoop, context: dict[str, object]) -> None:
    error = context.get("exception")
    _log.error(
        "event_loop_error",
        detail=str(context.get("message", "")),
        error_type=type(error).__name__ if isinstance(error, BaseException) else None,
        exc_info=error if isinstance(error, BaseException) else None,
    )


def uvicorn_config(app: ASGIApp, *, host: str, port: int) -> uvicorn.Config:
    """Server settings: nothing but our own code interprets headers or configures logging."""
    return uvicorn.Config(
        app,
        host=host,
        port=port,
        # The receiver resolves the client address itself; uvicorn must not rewrite the peer
        # from X-Forwarded-For (its default when the peer is 127.0.0.1).
        proxy_headers=False,
        server_header=False,
        access_log=False,
        log_config=None,
        lifespan="on",
        timeout_graceful_shutdown=round(_SHUTDOWN_GRACE_SECONDS),
    )


def run_service(parts: ServiceParts, *, host: str, port: int) -> int:
    """Serve until a signal stops the process or a background task dies; return the exit code."""
    if not is_loopback_host(host):
        raise ServiceError(
            f"--host {host!r} reddedildi: alıcı yalnızca loopback adresini (127.0.0.1 ya da ::1) "
            "dinler; dışarıya TLS vekili ya da tünel açar (docs/runbook.md §7)"
        )
    servers: list[uvicorn.Server] = []

    def stop_server() -> None:
        for server in servers:
            server.should_exit = True

    # python-telegram-bot 22 warns on every RetryAfter unless this opt-in is set; it only
    # changes the type of `retry_after` (timedelta), which the sender reads either way.
    os.environ.setdefault("PTB_TIMEDELTA", "true")
    service = Service(parts, on_failure=stop_server)
    server = uvicorn.Server(uvicorn_config(service.app, host=host, port=port))
    servers.append(server)

    async def serve() -> None:
        asyncio.get_running_loop().set_exception_handler(_log_loop_error)
        await server.serve()

    with suppress(KeyboardInterrupt):
        asyncio.run(serve())
    if service.failure is not None:
        _log.critical("service_exit", reason="task_died", task=service.failure)
        return EXIT_FAILED
    return EXIT_OK
