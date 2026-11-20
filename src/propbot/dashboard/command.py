"""`propbot dashboard`: arguments, start-up checks and the uvicorn run loop (ADR-0009).

Order of the checks: the host first (a non-loopback address is refused, there is no
override), then the config, then the secrets and `tv_alerts.yaml` (their absence makes the
panel read-only, not unusable), then the port (a busy port is an error before anything
runs). The database is never created here: `StoreSource` opens it once it exists.
"""

import argparse
import asyncio
import socket
from contextlib import suppress
from dataclasses import dataclass
from pathlib import Path
from typing import Final, TextIO

import uvicorn
from fastapi import FastAPI

from propbot.core.clock import Clock, system_clock_ms
from propbot.dashboard.actions import CommandsFactory, commands_factory
from propbot.dashboard.app import DashboardDeps, create_app
from propbot.dashboard.live import StoreSource
from propbot.dashboard.security import new_csrf_token
from propbot.risk.errors import ConfigError
from propbot.risk.loader import LoadedConfig, load_config_dir
from propbot.risk.venue import VenueProfile
from propbot.settings import Settings, SettingsError, load_settings
from propbot.signals.service import ServiceError, bind_listener, is_loopback_host
from propbot.signals.tvconfig import TV_ALERTS_FILE_NAME, TvAlertsConfig, load_tv_alerts_config

DEFAULT_HOST: Final = "127.0.0.1"
DEFAULT_PORT: Final = 8787
EXIT_OK: Final = 0
EXIT_FAILED: Final = 1
_GRACE_SECONDS: Final = 5


class DashboardError(Exception):
    """The panel cannot start as asked. The message is user-facing (Turkish)."""


def refuse_host(host: str) -> None:
    if not is_loopback_host(host):
        raise DashboardError(
            f"--host {host!r} reddedildi: panel yalnızca loopback adresinde (127.0.0.1 ya da ::1) "
            "açılır ve kimlik doğrulaması yoktur. Başka bir bilgisayardan bakmak için SSH tüneli "
            f"kullan: ssh -L {DEFAULT_PORT}:127.0.0.1:{DEFAULT_PORT} <kullanıcı>@<sunucu>"
        )


def pick_venue(config: LoadedConfig, name: str | None) -> tuple[str, VenueProfile]:
    names = ", ".join(sorted(config.venues))
    if name is None:
        if len(config.venues) != 1:
            raise DashboardError(f"birden çok venue profili var; --venue ile seç: {names}")
        return next(iter(config.venues.items()))
    venue = config.venues.get(name)
    if venue is None:
        raise DashboardError(f"venue profili bulunamadı: {name!r}; mevcut profiller: {names}")
    return name, venue


@dataclass(frozen=True, slots=True)
class _Controls:
    settings: Settings | None
    tv: TvAlertsConfig | None
    reason: str | None


def _load_controls(args: argparse.Namespace, config_dir: Path) -> _Controls:
    """Secrets and tv_alerts.yaml; a problem turns the panel read-only and is named."""
    reasons: list[str] = []
    settings: Settings | None = None
    tv: TvAlertsConfig | None = None
    try:
        settings = load_settings(None if args.no_env_file else Path(args.env_file))
    except SettingsError as error:
        # The issues name keys and files, never values (settings.py).
        reasons.append("sırlar okunamadı (" + "; ".join(error.issues) + ")")
    try:
        tv = load_tv_alerts_config(config_dir / TV_ALERTS_FILE_NAME)
    except ConfigError as error:
        reasons.append(f"{TV_ALERTS_FILE_NAME} okunamadı ({len(error.issues)} sorun)")
    return _Controls(settings, tv, "; ".join(reasons) if reasons else None)


def _host_text(host: str) -> str:
    return f"[{host}]" if ":" in host else host


def build_server(app: FastAPI) -> uvicorn.Server:
    config = uvicorn.Config(
        app,
        # Nothing of a request is logged by uvicorn: no access log (it would print query
        # strings), and our redacting logging setup instead of uvicorn's own.
        access_log=False,
        log_config=None,
        proxy_headers=False,
        server_header=False,
        lifespan="off",
        timeout_graceful_shutdown=_GRACE_SECONDS,
    )
    return uvicorn.Server(config)


def serve(server: uvicorn.Server, listener: socket.socket) -> None:
    """Run until SIGINT/SIGTERM (uvicorn's own handlers, main thread) or `should_exit`."""
    with suppress(KeyboardInterrupt):
        asyncio.run(server.serve(sockets=[listener]))


def run_dashboard(
    args: argparse.Namespace, out: TextIO, err: TextIO, *, clock: Clock = system_clock_ms
) -> int:
    """Check, bind, print the banner and serve. Returns the exit code."""
    try:
        refuse_host(args.host)
        config_dir = Path(args.config_dir)
        config = load_config_dir(config_dir)
        venue_name, venue = pick_venue(config, args.venue)
    except DashboardError as error:
        print(f"HATA: {error}", file=err)
        return EXIT_FAILED

    controls = _load_controls(args, config_dir)
    try:
        listener = bind_listener(args.host, args.port)
    except ServiceError:
        print(
            f"HATA: {_host_text(args.host)}:{args.port} dinlenemiyor; port başka bir süreçte "
            "olabilir (ikinci bir panel?). Başka port için: --port <numara> (ss -ltnp ile "
            "kontrol et)",
            file=err,
        )
        return EXIT_FAILED

    # Imported here: only this command needs the logging setup.
    from propbot.logging_setup import configure_logging
    from propbot.signals.service import install_exception_logging

    settings = controls.settings
    secrets = (
        []
        if settings is None
        else [
            settings.telegram_bot_token.get_secret_value(),
            settings.tv_webhook_token.get_secret_value(),
            settings.propbot_pin.get_secret_value(),
        ]
    )
    configure_logging(secrets=secrets, level=args.log_level)
    install_exception_logging()

    port = listener.getsockname()[1]
    factory: CommandsFactory | None = None
    if settings is not None and controls.tv is not None:
        factory = commands_factory(
            settings=settings,
            tv=controls.tv,
            risk=config.risk,
            venue=venue,
            venue_name=venue_name,
            clock=clock,
            started_ms=clock(),
        )
    source = StoreSource(Path(args.db))
    app = create_app(
        DashboardDeps(
            source=source,
            risk=config.risk,
            venue=venue,
            venue_name=venue_name,
            commands=factory,
            unavailable_reason=controls.reason,
            clock=clock,
            reports_dir=Path(args.reports_dir),
            port=port,
            csrf_token=new_csrf_token(),
        )
    )
    url = f"http://{_host_text(args.host)}:{port}/"
    print(
        f"PropBot paneli · {url} · mod: signal (emir gönderilmez) · durdurmak için Ctrl+C", file=out
    )
    print(f"  venue: {venue_name} · veritabanı: {args.db} · raporlar: {args.reports_dir}", file=out)
    if controls.reason is None:
        print(
            "  kontroller: açık (Duraklat/Devam/KILL Telegram komutlarıyla aynı kuralda)", file=out
        )
    else:
        print(f"  kontroller: KAPALI, panel salt-okunur — {controls.reason}", file=out)
    if not Path(args.db).exists():
        print("  veritabanı henüz yok: panel onu oluşturmaz; propbot serve oluşturur", file=out)
    out.flush()
    try:
        serve(build_server(app), listener)
    finally:
        listener.close()
        source.close()
    return EXIT_OK
