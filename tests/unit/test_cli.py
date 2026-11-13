"""`propbot` command line: config check and the Monte Carlo report, with their exit codes."""

import datetime as dt
import io
from importlib.metadata import entry_points
from pathlib import Path

import pytest

from propbot.backtest.montecarlo import RiskStep, SimulationInputError
from propbot.cli import main, parse_risk_steps, run_config_check
from support.paths import CONFIG_DIR, CONFIG_FIXTURES, FIXTURES
from support.synthetic import synthetic_trades_csv

SYNTHETIC = FIXTURES / "trades" / "synthetic_trades.csv"
REAL_VENUE = CONFIG_DIR / "venues" / "ftmo_2step_100k.yaml"
SAMPLE_VENUE = CONFIG_FIXTURES / "valid" / "venues" / "sample_2step.yaml"
SAMPLE_RISK = CONFIG_FIXTURES / "valid" / "risk.yaml"
TODAY = dt.date(2026, 11, 13)

# A small, fast run on the fully resolved sample venue.
QUICK = [
    "montecarlo",
    "--trades",
    str(SYNTHETIC),
    "--venue",
    str(SAMPLE_VENUE),
    "--risk-config",
    str(SAMPLE_RISK),
    "--paths",
    "300",
    "--max-days",
    "200",
    "--dd-days",
    "60",
]


def check(config_dir: Path, today: dt.date = TODAY) -> tuple[int, str]:
    out = io.StringIO()
    return run_config_check(config_dir, today, out), out.getvalue()


# --- wiring -----------------------------------------------------------------------------


def test_console_script_points_at_the_cli() -> None:
    scripts = {script.name: script.value for script in entry_points(group="console_scripts")}

    assert scripts["propbot"] == "propbot.cli:main"


def test_synthetic_fixture_matches_its_generator() -> None:
    assert SYNTHETIC.read_text(encoding="utf-8") == synthetic_trades_csv()


def test_a_command_is_required(capsys: pytest.CaptureFixture[str]) -> None:
    with pytest.raises(SystemExit) as excinfo:
        main([])

    assert excinfo.value.code == 2
    assert "KOMUT" in capsys.readouterr().err


# --- config check -----------------------------------------------------------------------


def test_config_check_accepts_the_real_config(capsys: pytest.CaptureFixture[str]) -> None:
    code = main(["config", "check", "--config-dir", str(CONFIG_DIR)])

    out = capsys.readouterr().out
    assert code == 0
    assert "risk.yaml: GEÇERLİ" in out
    assert "venues/ftmo_2step_100k.yaml: GEÇERLİ" in out
    assert "Çapraz kurallar (SPEC §8.1): GEÇERLİ" in out
    assert out.rstrip().endswith("Sonuç: GEÇERLİ")


def test_config_check_reports_budget_lock_and_null_fields_of_the_real_profile() -> None:
    code, out = check(CONFIG_DIR)

    assert code == 0
    assert "risk_per_trade_pct 0.25 ≤ 0.5 (kod tavanı)" in out
    assert "daily_cap_pct 1.0 ≤ 0.5 × daily_loss_limit_pct 5.0 = 2.5" in out
    assert "günlük bütçe: %1 × 100.000 USD = 1.000 USD" in out
    assert "[ftmo_2step_100k] boş → signal/paper dışındaki modlar KİLİTLİ" in out
    assert (
        "VERIFY (resmi sayfadan doğrulanacak): rules_verified_at, min_trading_days, server_tz"
        in out
    )
    assert "RUNTIME (çalışma anında terminalden okunur): contract.tick_size" in out


def test_config_check_on_a_fully_resolved_profile() -> None:
    code, out = check(CONFIG_FIXTURES / "valid")

    assert code == 0
    assert "[sample_2step] 2026-11-01: taze (12 gün; sınır 30)" in out
    assert "Boş alanlar" not in out
    assert "KİLİTLİ" not in out


def test_config_check_flags_stale_rules_but_stays_valid() -> None:
    code, out = check(CONFIG_FIXTURES / "valid", today=dt.date(2026, 12, 2))

    assert code == 0
    assert "2026-11-01: BAYAT (31 gün; sınır 30) → signal/paper dışındaki modlar KİLİTLİ" in out


def test_config_check_rejects_a_verification_date_in_the_future() -> None:
    code, out = check(CONFIG_FIXTURES / "valid", today=dt.date(2026, 10, 1))

    assert code == 1
    assert "2026-11-01: İLERİ TARİHLİ (bugün 2026-10-01)" in out
    assert out.rstrip().endswith("Sonuç: GEÇERSİZ (rules_verified_at ileri tarihli)")


def test_config_check_lists_every_issue_of_a_broken_config(
    capsys: pytest.CaptureFixture[str],
) -> None:
    broken = CONFIG_FIXTURES / "invalid" / "cross" / "two_broken_files"

    code = main(["config", "check", "--config-dir", str(broken)])

    out = capsys.readouterr().out
    assert code == 1
    assert "HATA: 2 sorun bulundu" in out
    assert "risk.yaml: risk_per_trade_pct: en fazla 0.5 olabilir; okunan: 0.6" in out
    assert "sample_2step.yaml: platform: geçersiz değer" in out
    assert "GEÇERLİ" not in out


def test_config_check_rejects_a_cross_file_violation() -> None:
    code, out = check(CONFIG_FIXTURES / "invalid" / "cross" / "daily_cap_above_half_limit")

    assert code == 1
    assert "daily_cap_pct: 3.0 değeri" in out
    assert "tavanı aşıyor" in out


# --- montecarlo -------------------------------------------------------------------------


def test_montecarlo_prints_a_complete_report(capsys: pytest.CaptureFixture[str]) -> None:
    code = main([*QUICK, "--cost-r", "0.05"])

    captured = capsys.readouterr()
    assert code == 0
    assert captured.err == ""
    for heading in (
        "PropBot · Monte Carlo prop simülasyonu",
        "Girdi       :",
        "Venue       : samplefirm / 2-step",
        "Kurallar    : hedef %10 → %5",
        "Varsayımlar : risk %0,25 / işlem",
        "SONUÇLAR",
        "Faz 1'i geçme",
        "İki fazı da geçme",
        "İhlal",
        "Faz 2 · toplam kayıp",
        "Sonuçlanmadı (ufuk doldu)",
        "GEÇME SÜRESİ",
        "MAKS. DÜŞÜŞ (kural durdurması olmadan, 60 piyasa günü;",
        "beklenen kuyruk kaybı (ES)",
        "NOTLAR",
    ):
        assert heading in captured.out, heading
    assert "260 işlem" in captured.out
    assert "300 yol · ufuk 200 piyasa günü" in captured.out


def test_montecarlo_is_reproducible_and_seed_dependent(capsys: pytest.CaptureFixture[str]) -> None:
    main([*QUICK, "--cost-r", "0.05", "--seed", "1"])
    first = capsys.readouterr().out
    main([*QUICK, "--cost-r", "0.05", "--seed", "1"])
    again = capsys.readouterr().out
    main([*QUICK, "--cost-r", "0.05", "--seed", "2"])
    other = capsys.readouterr().out

    assert first == again
    assert first != other


def test_montecarlo_uses_the_risk_from_risk_yaml_unless_overridden(
    capsys: pytest.CaptureFixture[str],
) -> None:
    main([*QUICK, "--r-is-net"])
    default = capsys.readouterr().out
    main([*QUICK, "--r-is-net", "--risk-pct", "0.5"])
    overridden = capsys.readouterr().out

    assert "risk %0,25 / işlem" in default
    assert "risk %0,5 / işlem" in overridden
    assert "R değerleri maliyet dahil (kullanıcı beyanı)" in default


def test_montecarlo_warns_about_a_run_without_costs(capsys: pytest.CaptureFixture[str]) -> None:
    code = main(QUICK)

    assert code == 0
    assert "MALİYETSİZ KOŞU" in capsys.readouterr().out


def test_montecarlo_refuses_a_null_min_trading_days_without_an_assumption(
    capsys: pytest.CaptureFixture[str],
) -> None:
    arguments = ["montecarlo", "--trades", str(SYNTHETIC), "--venue", str(REAL_VENUE)]
    arguments += ["--risk-config", str(CONFIG_DIR / "risk.yaml"), "--paths", "200"]
    arguments += ["--max-days", "100", "--dd-days", "50", "--cost-r", "0.05"]

    refused = main(arguments)
    refusal = capsys.readouterr()
    assumed = main([*arguments, "--assume-min-trading-days", "4"])
    report = capsys.readouterr().out

    if "min. işlem günü 4 (VARSAYIM)" not in report:
        pytest.skip("the real profile now carries a verified min_trading_days")
    assert refused == 1
    assert refusal.out == ""
    assert "HATA: min_trading_days venue profilinde boş (VERIFY)" in refusal.err
    assert assumed == 0
    assert "Prop kuralları DOĞRULANMADI" in report
    assert "VARSAYIMDIR" in report


def test_montecarlo_shows_experimental_risk_steps(capsys: pytest.CaptureFixture[str]) -> None:
    code = main([*QUICK, "--cost-r", "0.05", "--dd-risk-steps", "3:0.5,6:0.25"])

    out = capsys.readouterr().out
    assert code == 0
    assert "risk kademesi (DENEYSEL): %3 zararda ×0,5, %6 zararda ×0,25" in out


def test_montecarlo_closed_model_is_labelled_optimistic(capsys: pytest.CaptureFixture[str]) -> None:
    code = main([*QUICK, "--cost-r", "0.05", "--intratrade", "closed"])

    out = capsys.readouterr().out
    assert code == 0
    assert "işlem içi model: closed" in out
    assert "YOK SAYILDI" in out


@pytest.mark.parametrize(
    ("extra", "message"),
    [
        (["--risk-pct", "0.6"], "risk_pct 0 ile 0.5 arasında olmalı"),
        (["--skip-prob", "1.5"], "işlem atlama olasılığı 0 ile 1 arasında olmalı"),
        (["--cost-r", "-0.1"], "--cost-r negatif olamaz"),
        (["--paths", "0"], "yol sayısı ve gün ufukları en az 1 olmalı"),
        (["--dd-risk-steps", "3:1.5"], "risk kademesi çarpanları"),
        (["--dd-risk-steps", "üç:yarım"], "--dd-risk-steps biçimi"),
        (["--dd-risk-steps", "10:0.5"], "max_loss_pct (10.0) değerinden küçük olmalı"),
        (["--trades-tz", "Mars/Olympus"], "--trades-tz bilinmeyen saat dilimi"),
        (["--assume-min-trading-days", "0"], "farklı bir varsayım (0) verilemez"),
    ],
)
def test_montecarlo_rejects_invalid_inputs(
    capsys: pytest.CaptureFixture[str], extra: list[str], message: str
) -> None:
    code = main([*QUICK, *extra])

    captured = capsys.readouterr()
    assert code == 1
    assert captured.out == ""
    assert captured.err.startswith("HATA: ")
    assert message in captured.err


def test_montecarlo_rejects_a_missing_trade_list(
    capsys: pytest.CaptureFixture[str], tmp_path: Path
) -> None:
    arguments = [*QUICK]
    arguments[arguments.index(str(SYNTHETIC))] = str(tmp_path / "yok.csv")

    assert main(arguments) == 1
    assert "dosya bulunamadı" in capsys.readouterr().err


def test_montecarlo_rejects_an_inconsistent_config(capsys: pytest.CaptureFixture[str]) -> None:
    broken = CONFIG_FIXTURES / "invalid" / "cross" / "daily_cap_above_half_limit"
    arguments = ["montecarlo", "--trades", str(SYNTHETIC)]
    arguments += ["--venue", str(broken / "venues" / "sample_2step.yaml")]
    arguments += ["--risk-config", str(broken / "risk.yaml")]

    code = main(arguments)

    err = capsys.readouterr().err
    assert code == 1
    assert "HATA: config geçersiz (1 sorun)" in err
    assert "daily_cap_pct" in err


def test_cost_and_net_flags_are_mutually_exclusive(capsys: pytest.CaptureFixture[str]) -> None:
    with pytest.raises(SystemExit) as excinfo:
        main([*QUICK, "--cost-r", "0.05", "--r-is-net"])

    assert excinfo.value.code == 2
    assert "--r-is-net" in capsys.readouterr().err


def test_naive_timestamps_need_trades_tz(
    capsys: pytest.CaptureFixture[str], tmp_path: Path
) -> None:
    trades = tmp_path / "naive.csv"
    trades.write_text(
        "entry_time,exit_time,r\n"
        "2026-01-05 09:36:00,2026-01-05 10:10:00,2\n"
        "2026-01-06 09:36:00,2026-01-06 10:10:00,-1\n",
        encoding="utf-8",
    )
    arguments = [*QUICK, "--r-is-net"]
    arguments[arguments.index(str(SYNTHETIC))] = str(trades)

    without_zone = main(arguments)
    error = capsys.readouterr().err
    with_zone = main([*arguments, "--trades-tz", "America/New_York"])
    report = capsys.readouterr().out

    assert without_zone == 1
    assert "saat dilimi içermiyor" in error
    assert with_zone == 0
    assert "2 işlem · 2026-01-05 → 2026-01-06" in report


# --- risk step parsing ------------------------------------------------------------------


def test_risk_steps_are_parsed_from_pairs() -> None:
    assert parse_risk_steps(None) == ()
    assert parse_risk_steps("  ") == ()
    assert parse_risk_steps("3:0.5") == (RiskStep(3.0, 0.5),)
    assert parse_risk_steps("3:0.5,6:0.25") == (RiskStep(3.0, 0.5), RiskStep(6.0, 0.25))


@pytest.mark.parametrize("text", ["3", "3:0.5:1", "3:0.5,", "a:b", "3;0.5"])
def test_malformed_risk_steps_are_rejected(text: str) -> None:
    with pytest.raises(SimulationInputError, match="--dd-risk-steps biçimi"):
        parse_risk_steps(text)
