"""`propbot` command line. All output is user-facing and therefore Turkish.

Exit codes: 0 success, 1 invalid config or input (or a service that stopped on a failure),
2 usage error (argparse).
"""

import argparse
import datetime as dt
import sys
from collections.abc import Sequence
from pathlib import Path
from typing import TextIO
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from propbot.backtest.montecarlo import (
    DEFAULT_DD_DAYS,
    DEFAULT_MAX_DAYS,
    DEFAULT_PATHS,
    DEFAULT_SEED,
    ChallengeRules,
    MonteCarloParams,
    RiskStep,
    SimulationInputError,
    simulate,
    summarize,
)
from propbot.backtest.report import ReportContext, render_report, tr_int, tr_level
from propbot.backtest.trades import (
    IntratradeModel,
    TradeListError,
    build_day_blocks,
    load_trades_csv,
    summarize_trades,
)
from propbot.risk.config import DAILY_CAP_MAX_FRACTION_OF_DAILY_LOSS_LIMIT, MAX_RISK_PER_TRADE_PCT
from propbot.risk.errors import ConfigError, ConfigIssue
from propbot.risk.loader import (
    RISK_FILE_NAME,
    VENUES_DIR_NAME,
    LoadedConfig,
    load_config_dir,
    load_risk_config,
    load_venue_profile,
)
from propbot.risk.modes import Mode, mode_refusal
from propbot.risk.rules import (
    cross_check,
    daily_budget_amount,
    daily_budget_pct,
    daily_cap_ceiling_pct,
)
from propbot.risk.venue import (
    RULES_MAX_AGE_DAYS,
    RulesState,
    UnresolvedKind,
    VenueProfile,
    rules_freshness,
    unresolved_fields,
)
from propbot.settings import SettingsError, load_settings
from propbot.signals.report import ReportError, build_report, day_start_ms, signals_csv
from propbot.signals.tvconfig import TV_ALERTS_FILE_NAME, TvAlertsConfig, load_tv_alerts_config
from propbot.store.db import Store, StoreError

EXIT_OK = 0
EXIT_INVALID = 1
_ISTANBUL = ZoneInfo("Europe/Istanbul")
DEFAULT_DB_PATH = "data/propbot.db"
_LOG_LEVELS = ("DEBUG", "INFO", "WARNING", "ERROR", "CRITICAL")
_WEEKDAY_LABELS = ("Pzt", "Sal", "Çar", "Per", "Cum", "Cmt", "Paz")


def main(argv: Sequence[str] | None = None) -> int:
    parser = _build_parser()
    args = parser.parse_args(argv)
    try:
        if args.command == "config":
            return run_config_check(Path(args.config_dir), _utc_today(), sys.stdout)
        if args.command == "serve":
            return _run_serve(args, _utc_today(), sys.stdout)
        if args.command == "signals":
            return _run_signals_report(args, sys.stdout)
        return _run_montecarlo(args, _utc_today(), sys.stdout)
    except ConfigError as exc:
        print(f"HATA: config geçersiz ({len(exc.issues)} sorun)", file=sys.stderr)
        for issue in exc.issues:
            print(f"  - {issue}", file=sys.stderr)
    except SettingsError as exc:
        print(f"HATA: sırlar okunamadı ({len(exc.issues)} sorun)", file=sys.stderr)
        for text in exc.issues:
            print(f"  - {text}", file=sys.stderr)
    except (TradeListError, SimulationInputError, ReportError, StoreError, ServeError) as exc:
        print(f"HATA: {exc}", file=sys.stderr)
    return EXIT_INVALID


class ServeError(Exception):
    """`propbot serve` cannot start as asked. The message is user-facing (Turkish)."""


def _utc_today() -> dt.date:
    return dt.datetime.now(dt.UTC).date()


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="propbot",
        description="PropBot: config doğrulama, sinyal botu (emir yok) ve Monte Carlo prop "
        "simülasyonu.",
    )
    commands = parser.add_subparsers(dest="command", required=True, metavar="KOMUT")

    config = commands.add_parser("config", help="config dosyalarını doğrula")
    config_commands = config.add_subparsers(dest="config_command", required=True, metavar="ALT")
    check = config_commands.add_parser(
        "check",
        help="risk.yaml, venues/*.yaml ve tv_alerts.yaml şemasını, çapraz kuralları ve boş "
        "alanları raporla",
    )
    check.add_argument("--config-dir", default="config", help="config klasörü (varsayılan: config)")

    serve = commands.add_parser(
        "serve",
        help="sinyal botunu çalıştır: TradingView webhook → Telegram (emir gönderilmez)",
        description="TradingView alert'lerini doğrular, boyut önerisi ekler ve tek bir Telegram "
        "sohbetine iletir (SPEC §5). Emir göndermez. Kurulum: docs/runbook.md.",
    )
    serve.add_argument(
        "--mode",
        default=Mode.SIGNAL.value,
        choices=[mode.value for mode in Mode],
        help="çalışma modu; bu sürümde yalnızca signal (varsayılan) çalışır",
    )
    serve.add_argument(
        "--venue", help="venue profili adı (config/venues/<ad>.yaml); tek profil varsa gerekmez"
    )
    serve.add_argument("--config-dir", default="config", help="config klasörü (varsayılan: config)")
    serve.add_argument("--db", default=DEFAULT_DB_PATH, help=f"veritabanı ({DEFAULT_DB_PATH})")
    env = serve.add_mutually_exclusive_group()
    env.add_argument("--env-file", default=".env", help="sırların dosyası (chmod 600; .env)")
    env.add_argument(
        "--no-env-file", action="store_true", help="sırları yalnızca ortam değişkenlerinden oku"
    )
    serve.add_argument(
        "--host", default="127.0.0.1", help="dinlenecek loopback adresi (127.0.0.1 ya da ::1)"
    )
    serve.add_argument("--port", type=int, default=8000, help="dinlenecek port (8000)")
    serve.add_argument("--log-level", default="INFO", choices=_LOG_LEVELS, help="günlük seviyesi")

    signals = commands.add_parser("signals", help="alınan sinyallerin kaydı")
    signals_commands = signals.add_subparsers(dest="signals_command", required=True, metavar="ALT")
    report = signals_commands.add_parser(
        "report",
        help="alınan / iletilen sinyaller ve gecikme raporu (SPEC §5.7 kabul kanıtı)",
        description="Veritabanındaki sinyalleri gün gün sayar, gecikme medyanını verir. "
        "Tarihler Türkiye saatiyle takvim günleridir.",
    )
    report.add_argument("--db", default=DEFAULT_DB_PATH, help=f"veritabanı ({DEFAULT_DB_PATH})")
    report.add_argument("--since", help="ilk gün, YYYY-AA-GG (varsayılan: kayıtların başı)")
    report.add_argument("--until", help="son gün, YYYY-AA-GG, dahil (varsayılan: bugün)")
    report.add_argument("--csv", help="sinyal başına bir satırlık CSV dosyası yaz")

    mc = commands.add_parser(
        "montecarlo",
        help="işlem listesinden prop sınavı geçme / ihlal olasılıklarını simüle et",
        description="R cinsinden işlem listesini gün bazında yeniden örnekler (SPEC §6.5). "
        "CSV biçimi: docs/monte-carlo.md.",
    )
    mc.add_argument("--trades", required=True, help="işlem listesi CSV (entry_time,exit_time,r)")
    mc.add_argument("--venue", required=True, help="venue profili (config/venues/*.yaml)")
    mc.add_argument("--risk-config", default="config/risk.yaml", help="risk.yaml yolu")
    mc.add_argument(
        "--risk-pct",
        type=float,
        help=f"işlem başı risk, başlangıç bakiyesinin yüzdesi (varsayılan: risk.yaml; "
        f"tavan {MAX_RISK_PER_TRADE_PCT})",
    )
    cost = mc.add_mutually_exclusive_group()
    cost.add_argument(
        "--cost-r", type=float, default=0.0, help="her işlemden düşülecek maliyet, R cinsinden"
    )
    cost.add_argument(
        "--r-is-net", action="store_true", help="R değerleri maliyet dahil (maliyetsiz uyarısı yok)"
    )
    mc.add_argument("--skip-prob", type=float, default=0.0, help="her işlemin atlanma olasılığı")
    mc.add_argument(
        "--assume-min-trading-days",
        type=int,
        help="profilde min_trading_days boşken kullanılacak VARSAYIM (raporda belirtilir)",
    )
    mc.add_argument(
        "--intratrade",
        choices=[model.value for model in IntratradeModel],
        default=IntratradeModel.STOP.value,
        help="işlem içi yüzen zarar: stop = stop'a kadar (kötümser, varsayılan); "
        "closed = yalnızca kapanışlar",
    )
    mc.add_argument(
        "--dd-risk-steps",
        help="DENEYSEL: zarar eşiği:risk çarpanı çiftleri, ör. 3:0.5,6:0.25 "
        "(bakiye %%3 aşağıdayken risk ×0,5); yalnızca küçültür",
    )
    mc.add_argument("--paths", type=int, default=DEFAULT_PATHS, help="yol sayısı")
    mc.add_argument("--seed", type=int, default=DEFAULT_SEED, help="rastgele tohum")
    mc.add_argument(
        "--max-days", type=int, default=DEFAULT_MAX_DAYS, help="sınav ufku (piyasa günü)"
    )
    mc.add_argument(
        "--dd-days", type=int, default=DEFAULT_DD_DAYS, help="maks. düşüş penceresi (piyasa günü)"
    )
    mc.add_argument(
        "--trades-tz", help="CSV'deki ofsetsiz zamanların saat dilimi (ör. America/New_York)"
    )
    return parser


def run_config_check(config_dir: Path, today: dt.date, out: TextIO) -> int:
    """Validate a config directory and print what is valid, capped, locked and still null."""
    print(f"Config doğrulaması: {config_dir}", file=out)
    try:
        config = load_config_dir(config_dir)
    except ConfigError as exc:
        print(f"HATA: {len(exc.issues)} sorun bulundu", file=out)
        for issue in exc.issues:
            print(f"  - {issue}", file=out)
        return EXIT_INVALID

    risk = config.risk
    print(f"  {RISK_FILE_NAME}: GEÇERLİ", file=out)
    for name in config.venues:
        print(f"  {VENUES_DIR_NAME}/{name}.yaml: GEÇERLİ", file=out)

    print("Çapraz kurallar (SPEC §8.1): GEÇERLİ", file=out)
    print(
        f"  risk_per_trade_pct {risk.risk_per_trade_pct} ≤ {MAX_RISK_PER_TRADE_PCT} (kod tavanı)",
        file=out,
    )
    future_dates = 0
    for name, venue in config.venues.items():
        print(
            f"  [{name}] daily_cap_pct {risk.daily_cap_pct} ≤ "
            f"{DAILY_CAP_MAX_FRACTION_OF_DAILY_LOSS_LIMIT} × daily_loss_limit_pct "
            f"{venue.daily_loss_limit_pct} = {daily_cap_ceiling_pct(venue)}",
            file=out,
        )
        print(
            f"  [{name}] daily_loss_limit_pct {venue.daily_loss_limit_pct} < "
            f"max_loss_pct {venue.max_loss_pct}",
            file=out,
        )
        print(
            f"  [{name}] günlük bütçe: {tr_level(daily_budget_pct(risk, venue))} × "
            f"{tr_int(round(venue.initial_balance))} {venue.account_currency} = "
            f"{tr_int(round(daily_budget_amount(risk, venue)))} {venue.account_currency}",
            file=out,
        )

    print("Kural tazeliği (rules_verified_at):", file=out)
    for name, venue in config.venues.items():
        print(f"  [{name}] {_freshness_line(venue, today)}", file=out)
        if rules_freshness(venue, today).state is RulesState.FUTURE:
            future_dates += 1

    unresolved_any = False
    for name, venue in config.venues.items():
        unresolved = unresolved_fields(venue)
        if not unresolved:
            continue
        if not unresolved_any:
            print("Boş alanlar:", file=out)
            unresolved_any = True
        verify = [field.path for field in unresolved if field.kind is UnresolvedKind.VERIFY]
        runtime = [field.path for field in unresolved if field.kind is UnresolvedKind.RUNTIME]
        if verify:
            print(f"  [{name}] VERIFY (resmi sayfadan doğrulanacak): {', '.join(verify)}", file=out)
        if runtime:
            print(
                f"  [{name}] RUNTIME (çalışma anında terminalden okunur): {', '.join(runtime)}",
                file=out,
            )

    tv_valid = _check_tv_alerts(config_dir, today, out)

    if future_dates:
        print("Sonuç: GEÇERSİZ (rules_verified_at ileri tarihli)", file=out)
        return EXIT_INVALID
    if not tv_valid:
        print(f"Sonuç: GEÇERSİZ ({TV_ALERTS_FILE_NAME})", file=out)
        return EXIT_INVALID
    print("Sonuç: GEÇERLİ", file=out)
    return EXIT_OK


def _check_tv_alerts(config_dir: Path, today: dt.date, out: TextIO) -> bool:
    """Report on `tv_alerts.yaml`; a missing file is fine here (only `serve` needs it)."""
    path = config_dir / TV_ALERTS_FILE_NAME
    if not path.exists():
        print(f"{TV_ALERTS_FILE_NAME}: yok (yalnızca `propbot serve` için gerekir)", file=out)
        return True
    try:
        tv = load_tv_alerts_config(path)
    except ConfigError as exc:
        print(f"{TV_ALERTS_FILE_NAME}: HATA, {len(exc.issues)} sorun bulundu", file=out)
        for issue in exc.issues:
            print(f"  - {issue}", file=out)
        return False
    print(f"{TV_ALERTS_FILE_NAME}: GEÇERLİ", file=out)
    for line in _tv_alerts_lines(tv, today):
        print(f"  {line}", file=out)
    return True


def _tv_alerts_lines(tv: TvAlertsConfig, today: dt.date) -> list[str]:
    age_days = (today - tv.webhook.rules_verified_at).days
    lines = [
        f"TradingView kaynak IP'leri: {len(tv.webhook.source_ips)} adet · kurallar "
        f"{tv.webhook.rules_verified_at} tarihinde karşılaştırıldı ({age_days} gün önce); her "
        "kurulumda resmi sayfayla yeniden karşılaştır",
        f"alıcı: dakikada en fazla {tv.receiver.rate_limit_per_minute} istek · gövde en fazla "
        f"{tv.receiver.max_body_bytes} bayt · istemci IP başlığı {tv.receiver.client_ip_header}",
        f"izlenen pencereler (New York): {_windows_text(tv)} · sessizlik sınırı "
        f"{tv.heartbeat.max_silence_minutes} dk",
    ]
    if not tv.alerts:
        lines.append("alarm bitiş tarihleri: kayıt yok → bitiş hatırlatması gönderilmez")
    for alert in tv.alerts:
        if alert.expires_on is None:
            lines.append(f"alarm '{alert.name}': bitiş tarihi yok")
            continue
        days_left = (alert.expires_on - today).days
        state = f"{days_left} gün kaldı" if days_left >= 0 else f"{-days_left} gün önce DOLDU"
        lines.append(f"alarm '{alert.name}': {alert.expires_on} ({state})")
    return lines


def _windows_text(tv: TvAlertsConfig) -> str:
    parts = []
    for window in tv.heartbeat.watch_windows:
        days = ",".join(_WEEKDAY_LABELS[index] for index in sorted(window.weekdays))
        parts.append(f"{days} {window.start}–{window.end}")
    return "; ".join(parts)


def _run_serve(args: argparse.Namespace, today: dt.date, out: TextIO) -> int:
    """Check everything that can be checked before the first request, then serve."""
    config_dir = Path(args.config_dir)
    config = load_config_dir(config_dir)
    venue_name, venue = _pick_venue(config, args.venue)
    tv = load_tv_alerts_config(config_dir / TV_ALERTS_FILE_NAME)

    refusal = mode_refusal(Mode(args.mode), venue, today)
    if refusal is not None:
        raise ServeError(f"mod kilitli: {refusal}")

    # Imported here: the web and Telegram stack is only needed by this command.
    import structlog

    from propbot.logging_setup import configure_logging
    from propbot.signals.service import (
        ServiceError,
        ServiceParts,
        install_exception_logging,
        is_loopback_host,
        run_service,
    )

    if not is_loopback_host(args.host):
        raise ServeError(
            f"--host {args.host!r} reddedildi: alıcı yalnızca loopback adresini (127.0.0.1 ya da "
            "::1) dinler; dışarıya TLS vekili ya da tünel açar (docs/runbook.md §7)"
        )
    settings = load_settings(None if args.no_env_file else Path(args.env_file))
    db_path = Path(args.db)
    db_path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)

    for line in _serve_banner(args, venue_name, venue, tv, today):
        print(line, file=out)
    out.flush()
    configure_logging(
        secrets=[
            settings.telegram_bot_token.get_secret_value(),
            settings.tv_webhook_token.get_secret_value(),
            settings.propbot_pin.get_secret_value(),
        ],
        level=args.log_level,
    )
    install_exception_logging()
    with Store(db_path) as store:
        parts = ServiceParts(
            settings=settings,
            risk=config.risk,
            venue=venue,
            venue_name=venue_name,
            tv=tv,
            store=store,
        )
        try:
            return run_service(parts, host=args.host, port=args.port)
        except ServiceError as exc:
            raise ServeError(str(exc)) from None
        except Exception as exc:
            # A raw traceback may quote a secret (python-telegram-bot's InvalidToken prints
            # the bot token): everything goes through the redacting logger instead.
            structlog.get_logger("propbot.cli").critical(
                "service_fatal", error_type=type(exc).__name__, exc_info=exc
            )
            raise ServeError(
                "servis beklenmeyen bir hatayla durdu; ayrıntı günlükte (service_fatal)"
            ) from None


def _pick_venue(config: LoadedConfig, name: str | None) -> tuple[str, VenueProfile]:
    names = ", ".join(sorted(config.venues))
    if name is None:
        if len(config.venues) != 1:
            raise ServeError(f"birden çok venue profili var; --venue ile seç: {names}")
        return next(iter(config.venues.items()))
    venue = config.venues.get(name)
    if venue is None:
        raise ServeError(f"venue profili bulunamadı: {name!r}; mevcut profiller: {names}")
    return name, venue


def _serve_banner(
    args: argparse.Namespace,
    venue_name: str,
    venue: VenueProfile,
    tv: TvAlertsConfig,
    today: dt.date,
) -> list[str]:
    risk_missing = [field.path for field in unresolved_fields(venue) if "contract." in field.path]
    lines = [
        "PropBot sinyal botu · mod: signal (emir GÖNDERİLMEZ; sinyaller Telegram'a iletilir)",
        f"  venue: {venue_name} ({venue.venue.upper()} · {venue.symbol}) · kurallar: "
        f"{_freshness_line(venue, today)}",
        f"  alıcı: http://{_host_text(args.host)}:{args.port}/tv/webhook (TLS vekili ya da tünel "
        "arkasında)",
        f"  TradingView kaynak IP'leri: {len(tv.webhook.source_ips)} adet · izlenen pencereler "
        f"(New York): {_windows_text(tv)}",
        f"  veritabanı: {args.db}",
    ]
    if risk_missing:
        lines.append(
            f"  UYARI: {', '.join(risk_missing)} boş → mesajlarda lot yerine 'boyut hesaplanamadı' "
            "yazacak (docs/runbook.md §3.3)"
        )
    return lines


def _host_text(host: str) -> str:
    return f"[{host}]" if ":" in host else host


def _run_signals_report(args: argparse.Namespace, out: TextIO) -> int:
    db_path = Path(args.db)
    if not db_path.is_file():
        raise ReportError(f"veritabanı bulunamadı: {db_path}")
    start_ms = 0 if args.since is None else day_start_ms(_parse_day(args.since, "--since"))
    last_day = (
        dt.datetime.now(dt.UTC).astimezone(_ISTANBUL).date()
        if args.until is None
        else _parse_day(args.until, "--until")
    )
    end_ms = day_start_ms(last_day + dt.timedelta(days=1))
    if end_ms <= start_ms:
        raise ReportError("--until, --since tarihinden önce olamaz")
    with Store(db_path) as store:
        out.write(build_report(store, start_ms=start_ms, end_ms=end_ms, source=str(db_path)))
        if args.csv is not None:
            csv_path = Path(args.csv)
            try:
                csv_path.parent.mkdir(parents=True, exist_ok=True)
                csv_path.write_text(
                    signals_csv(store, start_ms=start_ms, end_ms=end_ms), encoding="utf-8"
                )
            except OSError as exc:
                raise ReportError(f"CSV yazılamadı ({csv_path}): {type(exc).__name__}") from exc
            print(f"CSV yazıldı: {csv_path}", file=out)
    return EXIT_OK


def _parse_day(text: str, option: str) -> dt.date:
    try:
        return dt.date.fromisoformat(text)
    except ValueError as exc:
        raise ReportError(f"{option} YYYY-AA-GG biçiminde olmalı; okunan: {text!r}") from exc


def _freshness_line(venue: VenueProfile, today: dt.date) -> str:
    freshness = rules_freshness(venue, today)
    locked = "signal/paper dışındaki modlar KİLİTLİ"
    if freshness.state is RulesState.MISSING:
        return f"boş → {locked}"
    if freshness.state is RulesState.STALE:
        return (
            f"{venue.rules_verified_at}: BAYAT ({freshness.age_days} gün; sınır "
            f"{RULES_MAX_AGE_DAYS}) → {locked}"
        )
    if freshness.state is RulesState.FUTURE:
        return f"{venue.rules_verified_at}: İLERİ TARİHLİ (bugün {today}) → {locked}"
    return f"{venue.rules_verified_at}: taze ({freshness.age_days} gün; sınır {RULES_MAX_AGE_DAYS})"


def _run_montecarlo(args: argparse.Namespace, today: dt.date, out: TextIO) -> int:
    risk_path = Path(args.risk_config)
    venue_path = Path(args.venue)
    issues: list[ConfigIssue] = []
    risk = venue = None
    try:
        risk = load_risk_config(risk_path)
    except ConfigError as exc:
        issues.extend(exc.issues)
    try:
        venue = load_venue_profile(venue_path)
    except ConfigError as exc:
        issues.extend(exc.issues)
    if risk is not None and venue is not None:
        issues.extend(
            cross_check(risk, venue, risk_source=str(risk_path), venue_source=str(venue_path))
        )
    if issues or risk is None or venue is None:
        raise ConfigError(issues)

    risk_pct = risk.risk_per_trade_pct if args.risk_pct is None else args.risk_pct
    params = MonteCarloParams(
        risk_pct=risk_pct,
        n_paths=args.paths,
        max_days=args.max_days,
        dd_days=args.dd_days,
        seed=args.seed,
        skip_prob=args.skip_prob,
        risk_steps=parse_risk_steps(args.dd_risk_steps),
    )
    rules = ChallengeRules.from_venue(venue, assume_min_trading_days=args.assume_min_trading_days)
    intratrade = IntratradeModel(args.intratrade)
    if not args.cost_r >= 0.0:
        raise SimulationInputError(f"--cost-r negatif olamaz; verilen: {args.cost_r}")

    trades = load_trades_csv(Path(args.trades), naive_tz=_parse_zone(args.trades_tz))
    blocks = build_day_blocks(
        trades,
        zone=venue.daily_reset.zone,
        reset=venue.daily_reset.time_of_day,
        cost_r=args.cost_r,
        intratrade=intratrade,
    )
    summary = summarize(simulate(blocks, rules, params))
    context = ReportContext(
        trades_path=args.trades,
        venue_path=args.venue,
        venue=venue,
        rules=rules,
        params=params,
        cost_r=args.cost_r,
        r_is_net=args.r_is_net,
        intratrade=intratrade,
        stats=summarize_trades(blocks),
        trades_crossing_reset=blocks.trades_crossing_reset,
        freshness=rules_freshness(venue, today),
    )
    out.write(render_report(context, summary))
    return EXIT_OK


def parse_risk_steps(text: str | None) -> tuple[RiskStep, ...]:
    """Parse `loss:multiplier` pairs such as `3:0.5,6:0.25`."""
    if text is None or not text.strip():
        return ()
    steps: list[RiskStep] = []
    for part in text.split(","):
        try:
            loss_text, multiplier_text = part.split(":")
            steps.append(RiskStep(loss_pct=float(loss_text), multiplier=float(multiplier_text)))
        except ValueError as exc:
            raise SimulationInputError(
                "--dd-risk-steps biçimi 'eşik:çarpan,eşik:çarpan' olmalı (ör. 3:0.5,6:0.25); "
                f"okunan: {text!r}"
            ) from exc
    return tuple(steps)


def _parse_zone(key: str | None) -> ZoneInfo | None:
    if key is None:
        return None
    try:
        return ZoneInfo(key)
    except (ZoneInfoNotFoundError, ValueError) as exc:
        raise SimulationInputError(f"--trades-tz bilinmeyen saat dilimi: {key!r}") from exc
