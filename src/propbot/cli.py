"""`propbot` command line. All output is user-facing and therefore Turkish.

Exit codes: 0 success, 1 invalid config or input, 2 usage error (argparse).
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
    load_config_dir,
    load_risk_config,
    load_venue_profile,
)
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

EXIT_OK = 0
EXIT_INVALID = 1


def main(argv: Sequence[str] | None = None) -> int:
    parser = _build_parser()
    args = parser.parse_args(argv)
    try:
        if args.command == "config":
            return run_config_check(Path(args.config_dir), _utc_today(), sys.stdout)
        return _run_montecarlo(args, _utc_today(), sys.stdout)
    except ConfigError as exc:
        print(f"HATA: config geçersiz ({len(exc.issues)} sorun)", file=sys.stderr)
        for issue in exc.issues:
            print(f"  - {issue}", file=sys.stderr)
    except (TradeListError, SimulationInputError) as exc:
        print(f"HATA: {exc}", file=sys.stderr)
    return EXIT_INVALID


def _utc_today() -> dt.date:
    return dt.datetime.now(dt.UTC).date()


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="propbot", description="PropBot: config doğrulama ve Monte Carlo prop simülasyonu."
    )
    commands = parser.add_subparsers(dest="command", required=True, metavar="KOMUT")

    config = commands.add_parser("config", help="config dosyalarını doğrula")
    config_commands = config.add_subparsers(dest="config_command", required=True, metavar="ALT")
    check = config_commands.add_parser(
        "check",
        help="risk.yaml ve venues/*.yaml şemasını, çapraz kuralları ve boş alanları raporla",
    )
    check.add_argument("--config-dir", default="config", help="config klasörü (varsayılan: config)")

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

    if future_dates:
        print("Sonuç: GEÇERSİZ (rules_verified_at ileri tarihli)", file=out)
        return EXIT_INVALID
    print("Sonuç: GEÇERLİ", file=out)
    return EXIT_OK


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
