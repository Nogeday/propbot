"""Turkish text report for the Monte Carlo prop simulator (user-facing output)."""

import math
from dataclasses import dataclass
from typing import Final

from propbot.backtest.montecarlo import (
    BreachReason,
    ChallengeRules,
    DurationStats,
    MonteCarloParams,
    MonteCarloSummary,
    standard_error,
)
from propbot.backtest.trades import IntratradeModel, TradeStats
from propbot.risk.venue import RULES_MAX_AGE_DAYS, RulesFreshness, RulesState, VenueProfile

# SPEC §9 G2: a strategy needs a t-statistic of at least 2.0 out of sample.
G2_MIN_T_STAT: Final = 2.0
_Z_95: Final = 1.96
_WIDTH: Final = 78
_SWAP_SEPARATORS = str.maketrans({",": ".", ".": ","})

_REASON_LABEL: Final = {
    BreachReason.DAILY_LOSS: "günlük kayıp",
    BreachReason.MAX_LOSS: "toplam kayıp",
}
_INTRATRADE_LABEL: Final = {
    IntratradeModel.STOP: "stop (yüzen zarar stop'a kadar; kötümser)",
    IntratradeModel.CLOSED: "closed (yalnızca kapanan sonuçlar; iyimser)",
}


def tr_number(value: float, digits: int = 1) -> str:
    """Format with a decimal comma and dots as thousands separators: 1234.5 -> 1.234,5."""
    return f"{value:,.{digits}f}".translate(_SWAP_SEPARATORS)


def tr_int(value: int) -> str:
    return f"{value:,d}".replace(",", ".")


def tr_percent(fraction: float, digits: int = 1) -> str:
    """Format a probability the Turkish way: 0.623 -> %62,3."""
    return "%" + tr_number(100.0 * fraction, digits)


def tr_plain(value: float) -> str:
    """Format without padding zeros: 0.5 -> 0,5 and 1.0 -> 1."""
    return f"{value:.4f}".rstrip("0").rstrip(".").replace(".", ",")


def tr_level(percent: float) -> str:
    """Format a value that is already a percentage, without padding zeros: 0.25 -> %0,25."""
    return "%" + tr_plain(percent)


def tr_r(value: float, digits: int = 3) -> str:
    return f"{value:+.{digits}f}".replace(".", ",") + "R"


@dataclass(frozen=True, slots=True)
class ReportContext:
    """Everything the report states about its inputs and assumptions."""

    trades_path: str
    venue_path: str
    venue: VenueProfile
    rules: ChallengeRules
    params: MonteCarloParams
    cost_r: float
    r_is_net: bool
    intratrade: IntratradeModel
    stats: TradeStats
    trades_crossing_reset: int
    freshness: RulesFreshness


def render_report(context: ReportContext, summary: MonteCarloSummary) -> str:
    lines = [
        "PropBot · Monte Carlo prop simülasyonu",
        "=" * _WIDTH,
        *_input_lines(context),
        "",
        *_warning_lines(context),
        *_result_lines(summary),
        "",
        *_duration_lines(summary),
        "",
        *_drawdown_lines(context, summary),
        "",
        *_note_lines(summary),
    ]
    return "\n".join(lines) + "\n"


def _input_lines(context: ReportContext) -> list[str]:
    stats, venue, rules, params = context.stats, context.venue, context.rules, context.params
    t_stat = "yok" if math.isnan(stats.t_stat) else tr_number(stats.t_stat, 2)
    targets = " → ".join(tr_level(target) for target in rules.targets_pct)
    reset = venue.daily_reset
    min_days = str(rules.min_trading_days) + (
        " (VARSAYIM)" if rules.min_trading_days_assumed else ""
    )
    if context.r_is_net:
        cost = "R değerleri maliyet dahil (kullanıcı beyanı)"
    else:
        cost = f"maliyet {tr_number(context.cost_r, 2)}R / işlem"
    assumptions = [
        f"risk {tr_level(params.risk_pct)} / işlem (başlangıç bakiyesi üzerinden, sabit) · {cost}",
        f"işlem atlama olasılığı {tr_percent(params.skip_prob, 0)} · "
        f"işlem içi model: {_INTRATRADE_LABEL[context.intratrade]}",
        f"{tr_int(params.n_paths)} yol · ufuk {tr_int(params.max_days)} piyasa günü · "
        f"tohum {params.seed}",
    ]
    if params.risk_steps:
        steps = ", ".join(
            f"{tr_level(step.loss_pct)} zararda ×{tr_plain(step.multiplier)}"
            for step in params.risk_steps
        )
        assumptions.append(f"risk kademesi (DENEYSEL): {steps}")
    return [
        f"Girdi       : {context.trades_path}",
        f"              {tr_int(stats.n_trades)} işlem · {stats.first_day} → {stats.last_day} · "
        f"{tr_int(stats.n_days)} piyasa günü ({tr_int(stats.n_days_with_trades)}'inde işlem var)",
        f"              ortalama {tr_r(stats.mean_r)} · std {tr_number(stats.std_r, 2)}R · "
        f"kazanma {tr_percent(stats.win_rate)} · t = {t_stat}   (maliyet sonrası)",
        f"Venue       : {venue.venue} / {venue.program} · "
        f"{tr_int(round(venue.initial_balance))} {venue.account_currency} · {context.venue_path}",
        f"Kurallar    : hedef {targets} · günlük kayıp {tr_level(rules.daily_loss_limit_pct)} "
        f"(reset {reset.time} {reset.tz})",
        f"              toplam kayıp {tr_level(rules.max_loss_pct)} "
        f"({venue.max_loss_type.value}) · min. işlem günü {min_days}",
        f"Varsayımlar : {assumptions[0]}",
        *(f"              {line}" for line in assumptions[1:]),
    ]


def _warning_lines(context: ReportContext) -> list[str]:
    warnings: list[str] = []
    freshness = context.freshness
    if freshness.state is RulesState.MISSING:
        warnings.append(
            "Prop kuralları DOĞRULANMADI: rules_verified_at boş (SPEC §8.1). Sonuçlar ön bilgidir."
        )
    elif freshness.state is RulesState.STALE:
        warnings.append(
            f"Prop kuralları BAYAT: rules_verified_at {context.venue.rules_verified_at} "
            f"({freshness.age_days} gün önce; sınır {RULES_MAX_AGE_DAYS} gün). Yeniden doğrula."
        )
    elif freshness.state is RulesState.FUTURE:
        warnings.append(
            f"rules_verified_at ileri tarihli ({context.venue.rules_verified_at}): geçersiz, "
            "kurallar doğrulanmamış sayılır."
        )
    if context.rules.min_trading_days_assumed:
        warnings.append(
            f"min_trading_days profilde boş: {context.rules.min_trading_days} değeri "
            "--assume-min-trading-days ile verilen VARSAYIMDIR; firmanın resmi sayfasından doğrula."
        )
    if context.cost_r == 0.0 and not context.r_is_net:
        warnings.append(
            "MALİYETSİZ KOŞU: yalnızca karşılaştırma içindir (SPEC §6.3). --cost-r ver ya da "
            "R değerleri maliyet dahilse --r-is-net kullan."
        )
    if context.intratrade is IntratradeModel.CLOSED:
        warnings.append(
            "İşlem içi yüzen zarar YOK SAYILDI (--intratrade closed): ihlal olasılığı iyimserdir."
        )
    if context.trades_crossing_reset:
        warnings.append(
            f"{tr_int(context.trades_crossing_reset)} işlem günlük reset'i aşıyor: bu işlemler "
            "girişin prop gününe yazıldı, günlük kayıp kuralı onlar için yaklaşıktır."
        )
    if context.params.risk_steps:
        warnings.append(
            "Risk kademesi DENEYSELDİR: bot kuralı değildir; kural olması ADR ve SPEC §8.1 "
            "değişikliği ister."
        )
    t_stat = context.stats.t_stat
    if math.isnan(t_stat) or t_stat < G2_MIN_T_STAT:
        shown = "hesaplanamadı" if math.isnan(t_stat) else tr_number(t_stat, 2)
        warnings.append(
            f"Örneklem zayıf: beklentinin t-istatistiği {shown} (SPEC §9 G2 eşiği "
            f"{tr_number(G2_MIN_T_STAT, 1)}). Olasılıklar bu listeye çok duyarlıdır."
        )
    if not warnings:
        return []
    return ["UYARILAR", *(f"  ! {warning}" for warning in warnings), ""]


def _result_lines(summary: MonteCarloSummary) -> list[str]:
    n = summary.n_paths

    def row(label: str, probability: float, *, with_interval: bool = True) -> str:
        text = f"  {label:<38}{tr_percent(probability):>9}"
        if with_interval:
            margin = 100.0 * _Z_95 * standard_error(probability, n)
            text += f"{'±' + tr_number(margin, 1):>10}"
        return text

    lines = [f"{'SONUÇLAR':<40}{'olasılık':>9}{'%95 GA':>10}"]
    if len(summary.p_pass_phase) == 1:
        lines.append(row("Faz 1'i geçme (program tek fazlı)", summary.p_pass_phase[0]))
    else:
        lines.append(row("Faz 1'i geçme", summary.p_pass_phase[0]))
        lines.append(row("İki fazı da geçme", summary.p_pass_all))
    lines.append(row("İhlal", summary.p_breach))
    lines += [
        row(
            f"  Faz {share.phase} · {_REASON_LABEL[share.reason]}",
            share.probability,
            with_interval=False,
        )
        for share in summary.breach_shares
    ]
    lines.append(row("Sonuçlanmadı (ufuk doldu)", summary.p_undecided))
    return lines


def _duration_lines(summary: MonteCarloSummary) -> list[str]:
    header = (
        f"  {'':<16}{'geçen yol':>10}{'medyan':>8}{'%25':>7}{'%75':>7}{'%90':>7}{'%95':>7}"
        f"{'ortalama':>10}"
    )
    lines = ["GEÇME SÜRESİ (piyasa günü; işlem olmayan günler dahil)", header]
    labels = ["Faz 1", "Faz 1 + Faz 2"]
    for label, stats in zip(labels, summary.time_to_pass, strict=False):
        lines.append(_duration_row(label, stats))
    return lines


def _duration_row(label: str, stats: DurationStats | None) -> str:
    if stats is None:
        return f"  {label:<16}{'0':>10}   (geçen yol yok)"
    cells = "".join(
        f"{tr_number(value, 0):>{width}}"
        for value, width in (
            (stats.median, 8),
            (stats.p25, 7),
            (stats.p75, 7),
            (stats.p90, 7),
            (stats.p95, 7),
        )
    )
    return f"  {label:<16}{tr_int(stats.n):>10}{cells}{tr_number(stats.mean, 1):>10}"


def _drawdown_lines(context: ReportContext, summary: MonteCarloSummary) -> list[str]:
    drawdown = summary.drawdown
    return [
        f"MAKS. DÜŞÜŞ (kural durdurması olmadan, {tr_int(context.params.dd_days)} piyasa günü; "
        "başlangıç bakiyesinin %'si)",
        f"  medyan {tr_level(round(drawdown.median, 2))} · "
        f"%95 dilim {tr_level(round(drawdown.q95, 2))} · "
        f"%99 dilim {tr_level(round(drawdown.q99, 2))}",
        f"  beklenen kuyruk kaybı (ES): en kötü %5 → {tr_level(round(drawdown.es95, 2))} · "
        f"en kötü %1 → {tr_level(round(drawdown.es99, 2))}",
    ]


def _note_lines(summary: MonteCarloSummary) -> list[str]:
    notes = [
        "Olasılıklar bu işlem listesinin gün bazında yeniden örneklenmesidir; %95 GA yalnızca",
        "  simülasyon hatasıdır, listenin kendi belirsizliğini (az işlem, rejim değişimi) içermez.",
        "Modellenmeyenler: botun kendi günlük bütçesi / haftalık tavanı / ardışık kayıp",
        "  duraklaması, lot yuvarlama, günler arası bağımlılık.",
    ]
    if summary.p_undecided > 0.0:
        notes.append("Sonuçlanmayan yollar --max-days büyütülürse geçme ya da ihlale dağılır.")
    notes.append("Ayrıntı: docs/monte-carlo.md · modelleme kararları: docs/adr/0002.")
    return ["NOTLAR", *(f"  {note}" if note.startswith("  ") else f"  - {note}" for note in notes)]
