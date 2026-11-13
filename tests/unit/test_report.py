"""Turkish Monte Carlo report: number formats, every section, and when each warning appears."""

import dataclasses
import datetime as dt
import math

import pytest

from propbot.backtest.montecarlo import (
    BreachReason,
    BreachShare,
    ChallengeRules,
    DrawdownStats,
    DurationStats,
    MonteCarloParams,
    MonteCarloSummary,
    RiskStep,
)
from propbot.backtest.report import (
    ReportContext,
    render_report,
    tr_int,
    tr_level,
    tr_number,
    tr_percent,
    tr_plain,
    tr_r,
)
from propbot.backtest.trades import IntratradeModel, TradeStats
from propbot.risk import RulesFreshness, RulesState, load_venue_profile
from support.paths import CONFIG_FIXTURES

VENUE = load_venue_profile(CONFIG_FIXTURES / "valid" / "venues" / "sample_2step.yaml")

STATS = TradeStats(
    n_trades=412,
    n_days=260,
    n_days_with_trades=198,
    first_day=dt.date(2025, 1, 2),
    last_day=dt.date(2025, 12, 31),
    mean_r=0.186,
    std_r=1.42,
    win_rate=0.403,
    t_stat=2.66,
)

SUMMARY = MonteCarloSummary(
    n_paths=10_000,
    p_pass_phase=(0.713, 0.589),
    p_breach=0.124,
    breach_shares=(
        BreachShare(1, BreachReason.DAILY_LOSS, 0.0),
        BreachShare(1, BreachReason.MAX_LOSS, 0.098),
        BreachShare(2, BreachReason.DAILY_LOSS, 0.0),
        BreachShare(2, BreachReason.MAX_LOSS, 0.026),
    ),
    p_undecided=0.287,
    time_to_pass=(
        DurationStats(n=7130, mean=158.3, p25=98, median=142, p75=201, p90=288, p95=341),
        DurationStats(n=5890, mean=240.0, p25=160, median=221, p75=300, p90=410, p95=480),
    ),
    drawdown=DrawdownStats(median=4.1, q95=7.9, q99=10.3, es95=9.2, es99=11.6),
)


def context(**overrides: object) -> ReportContext:
    """A context that triggers no warning; tests override one field at a time."""
    base = ReportContext(
        trades_path="trades.csv",
        venue_path="venue.yaml",
        venue=VENUE,
        rules=ChallengeRules.from_venue(VENUE),
        params=MonteCarloParams(risk_pct=0.25),
        cost_r=0.05,
        r_is_net=False,
        intratrade=IntratradeModel.STOP,
        stats=STATS,
        trades_crossing_reset=0,
        freshness=RulesFreshness(RulesState.FRESH, 12),
    )
    return dataclasses.replace(base, **overrides)  # type: ignore[arg-type]


def lines_of(report: str, needle: str) -> list[str]:
    return [line for line in report.splitlines() if needle in line]


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        (tr_number(1234.5), "1.234,5"),
        (tr_number(0.004, 2), "0,00"),
        (tr_int(10_000), "10.000"),
        (tr_int(999), "999"),
        (tr_percent(0.623), "%62,3"),
        (tr_percent(1.0, 0), "%100"),
        (tr_level(0.25), "%0,25"),
        (tr_level(10.0), "%10"),
        (tr_level(2.5), "%2,5"),
        (tr_plain(1.0), "1"),
        (tr_plain(0.125), "0,125"),
        (tr_r(0.186), "+0,186R"),
        (tr_r(-1.0, 1), "-1,0R"),
    ],
)
def test_turkish_number_formats(text: str, expected: str) -> None:
    assert text == expected


def test_report_states_inputs_rules_and_assumptions() -> None:
    report = render_report(context(), SUMMARY)

    assert report.startswith("PropBot · Monte Carlo prop simülasyonu\n")
    assert "Girdi       : trades.csv" in report
    assert "412 işlem · 2025-01-02 → 2025-12-31 · 260 piyasa günü (198'inde işlem var)" in report
    assert "ortalama +0,186R · std 1,42R · kazanma %40,3 · t = 2,66" in report
    assert "Venue       : samplefirm / 2-step · 100.000 USD · venue.yaml" in report
    assert "hedef %10 → %5 · günlük kayıp %5 (reset 00:00 Europe/Prague)" in report
    assert "toplam kayıp %10 (static) · min. işlem günü 4\n" in report
    assert (
        "risk %0,25 / işlem (başlangıç bakiyesi üzerinden, sabit) · maliyet 0,05R / işlem" in report
    )
    assert "işlem atlama olasılığı %0 · işlem içi model: stop" in report
    assert "10.000 yol · ufuk 1.000 piyasa günü · tohum 20261113" in report


def test_report_without_any_issue_has_no_warning_section() -> None:
    report = render_report(context(), SUMMARY)

    assert "UYARILAR" not in report
    assert " ! " not in report


def test_report_lists_probabilities_with_their_confidence_margin() -> None:
    report = render_report(context(), SUMMARY)

    # 1.96 x sqrt(p (1 - p) / 10 000), in percentage points.
    assert lines_of(report, "Faz 1'i geçme")[0].split() == ["Faz", "1'i", "geçme", "%71,3", "±0,9"]
    assert lines_of(report, "İki fazı da geçme")[0].split()[-2:] == ["%58,9", "±1,0"]
    assert lines_of(report, "  İhlal")[0].split() == ["İhlal", "%12,4", "±0,6"]
    assert lines_of(report, "Sonuçlanmadı")[0].split()[-2:] == ["%28,7", "±0,9"]
    assert lines_of(report, "Faz 1 · günlük kayıp")[0].split()[-1] == "%0,0"
    assert lines_of(report, "Faz 1 · toplam kayıp")[0].split()[-1] == "%9,8"
    assert lines_of(report, "Faz 2 · günlük kayıp")[0].split()[-1] == "%0,0"
    assert lines_of(report, "Faz 2 · toplam kayıp")[0].split()[-1] == "%2,6"


def test_report_lists_time_to_pass_in_market_days() -> None:
    report = render_report(context(), SUMMARY)

    assert "GEÇME SÜRESİ (piyasa günü; işlem olmayan günler dahil)" in report
    rows = [line.split() for line in report.splitlines() if line.startswith("  Faz 1  ")]
    assert rows[0] == ["Faz", "1", "7.130", "142", "98", "201", "288", "341", "158,3"]
    both = lines_of(report, "Faz 1 + Faz 2")[0].split()
    assert both[5:] == ["5.890", "221", "160", "300", "410", "480", "240,0"]


def test_report_lists_drawdown_quantiles_and_expected_shortfall() -> None:
    report = render_report(context(), SUMMARY)

    assert "MAKS. DÜŞÜŞ (kural durdurması olmadan, 250 piyasa günü;" in report
    assert "medyan %4,1 · %95 dilim %7,9 · %99 dilim %10,3" in report
    assert "beklenen kuyruk kaybı (ES): en kötü %5 → %9,2 · en kötü %1 → %11,6" in report


@pytest.mark.parametrize(
    ("overrides", "phrase"),
    [
        (
            {"freshness": RulesFreshness(RulesState.MISSING, None)},
            "Prop kuralları DOĞRULANMADI: rules_verified_at boş",
        ),
        (
            {"freshness": RulesFreshness(RulesState.STALE, 45)},
            "Prop kuralları BAYAT: rules_verified_at 2026-11-01 (45 gün önce; sınır 30 gün)",
        ),
        (
            {"freshness": RulesFreshness(RulesState.FUTURE, -20)},
            "rules_verified_at ileri tarihli (2026-11-01): geçersiz",
        ),
        ({"cost_r": 0.0}, "MALİYETSİZ KOŞU: yalnızca karşılaştırma içindir (SPEC §6.3)"),
        ({"intratrade": IntratradeModel.CLOSED}, "İşlem içi yüzen zarar YOK SAYILDI"),
        ({"trades_crossing_reset": 3}, "3 işlem günlük reset'i aşıyor"),
        (
            {"stats": dataclasses.replace(STATS, t_stat=1.44)},
            "Örneklem zayıf: beklentinin t-istatistiği 1,44 (SPEC §9 G2 eşiği 2,0)",
        ),
        (
            {"stats": dataclasses.replace(STATS, t_stat=math.nan)},
            "Örneklem zayıf: beklentinin t-istatistiği hesaplanamadı",
        ),
    ],
)
def test_each_condition_raises_its_warning(overrides: dict[str, object], phrase: str) -> None:
    report = render_report(context(**overrides), SUMMARY)

    warnings = [line for line in report.splitlines() if line.startswith("  ! ")]
    assert len(warnings) == 1
    assert phrase in warnings[0]
    assert "UYARILAR" in report


def test_assumed_minimum_trading_days_are_labelled_twice() -> None:
    assumed = dataclasses.replace(ChallengeRules.from_venue(VENUE), min_trading_days_assumed=True)

    report = render_report(context(rules=assumed), SUMMARY)

    assert "min. işlem günü 4 (VARSAYIM)" in report
    assert "4 değeri --assume-min-trading-days ile verilen VARSAYIMDIR" in report


def test_net_results_replace_the_cost_line_without_a_warning() -> None:
    report = render_report(context(cost_r=0.0, r_is_net=True), SUMMARY)

    assert "R değerleri maliyet dahil (kullanıcı beyanı)" in report
    assert "MALİYETSİZ" not in report


def test_risk_steps_are_shown_and_flagged_as_experimental() -> None:
    params = MonteCarloParams(risk_pct=0.25, risk_steps=(RiskStep(3.0, 0.5), RiskStep(6.0, 0.25)))

    report = render_report(context(params=params), SUMMARY)

    assert "risk kademesi (DENEYSEL): %3 zararda ×0,5, %6 zararda ×0,25" in report
    assert "Risk kademesi DENEYSELDİR: bot kuralı değildir" in report


def test_single_phase_program_has_one_pass_row() -> None:
    single = dataclasses.replace(
        SUMMARY,
        p_pass_phase=(0.713,),
        breach_shares=SUMMARY.breach_shares[:2],
        time_to_pass=SUMMARY.time_to_pass[:1],
    )

    report = render_report(context(), single)

    assert "Faz 1'i geçme (program tek fazlı)" in report
    assert "İki fazı" not in report
    assert "Faz 1 + Faz 2" not in report


def test_no_passing_path_is_stated_instead_of_empty_statistics() -> None:
    nobody = dataclasses.replace(SUMMARY, time_to_pass=(None, None), p_pass_phase=(0.0, 0.0))

    report = render_report(context(), nobody)

    assert len(lines_of(report, "(geçen yol yok)")) == 2


def test_horizon_note_only_appears_when_paths_are_undecided() -> None:
    decided = dataclasses.replace(SUMMARY, p_undecided=0.0)

    assert "--max-days büyütülürse" in render_report(context(), SUMMARY)
    assert "--max-days büyütülürse" not in render_report(context(), decided)
