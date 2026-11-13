"""`propbot` command line, Faz 1 parts: tv_alerts in config check, `serve` refusals, the report."""

import datetime as dt
import io
import shutil
from pathlib import Path

import pytest

from propbot.cli import main, run_config_check
from propbot.signals.report import CSV_HEADER
from propbot.store.db import NewSignal, Outcome, Store
from support.paths import CONFIG_DIR, CONFIG_FIXTURES, FIXTURES

VALID_DIR = CONFIG_FIXTURES / "valid"
TV_FIXTURES = FIXTURES / "tv_alerts"
TODAY = dt.date(2026, 11, 13)


def config_dir_with(tmp_path: Path, tv_alerts: Path | str | None) -> Path:
    """A copy of the valid config fixture, optionally with a tv_alerts.yaml next to it."""
    target = tmp_path / "config"
    shutil.copytree(VALID_DIR, target)
    if isinstance(tv_alerts, Path):
        shutil.copyfile(tv_alerts, target / "tv_alerts.yaml")
    elif tv_alerts is not None:
        (target / "tv_alerts.yaml").write_text(tv_alerts, encoding="utf-8")
    return target


def check(config_dir: Path) -> tuple[int, str]:
    out = io.StringIO()
    return run_config_check(config_dir, TODAY, out), out.getvalue()


# --- config check -------------------------------------------------------------------------


def test_config_check_reports_the_real_alert_line_config() -> None:
    code, out = check(CONFIG_DIR)

    assert code == 0
    assert "tv_alerts.yaml: GEÇERLİ" in out
    assert (
        "TradingView kaynak IP'leri: 4 adet · kurallar 2026-11-13 tarihinde karşılaştırıldı" in out
    )
    assert "dakikada en fazla 30 istek · gövde en fazla 4096 bayt" in out
    assert "istemci IP başlığı X-Forwarded-For" in out
    assert (
        "izlenen pencereler (New York): Pzt,Sal,Çar,Per,Cum 03:00–16:00 · sessizlik sınırı 75 dk"
        in out
    )
    assert "alarm bitiş tarihleri: kayıt yok → bitiş hatırlatması gönderilmez" in out
    assert out.rstrip().endswith("Sonuç: GEÇERLİ")


def test_config_check_without_the_alert_line_file_is_still_valid() -> None:
    code, out = check(VALID_DIR)

    assert code == 0
    assert "tv_alerts.yaml: yok (yalnızca `propbot serve` için gerekir)" in out
    assert out.rstrip().endswith("Sonuç: GEÇERLİ")


def test_config_check_lists_alert_expiry_dates(tmp_path: Path) -> None:
    code, out = check(config_dir_with(tmp_path, TV_FIXTURES / "valid" / "full.yaml"))

    assert code == 0
    assert "alarm 'heartbeat MES1! 1m': 2027-01-12 (60 gün kaldı)" in out
    assert "alarm 'ny_orb_fvg MES1! 1m': bitiş tarihi yok" in out


def test_config_check_marks_an_expired_alert(tmp_path: Path) -> None:
    config_dir = config_dir_with(tmp_path, TV_FIXTURES / "valid" / "full.yaml")

    out = io.StringIO()
    code = run_config_check(config_dir, dt.date(2027, 1, 15), out)

    assert code == 0
    assert "alarm 'heartbeat MES1! 1m': 2027-01-12 (3 gün önce DOLDU)" in out.getvalue()


def test_config_check_fails_on_a_broken_alert_line_file(tmp_path: Path) -> None:
    broken = TV_FIXTURES / "invalid" / "source_ip_loopback.yaml"

    code, out = check(config_dir_with(tmp_path, broken))

    assert code == 1
    assert "tv_alerts.yaml: HATA, 1 sorun bulundu" in out
    assert "webhook.source_ips" in out
    assert out.rstrip().endswith("Sonuç: GEÇERSİZ (tv_alerts.yaml)")


def test_config_check_command_exits_non_zero_for_a_broken_alert_line_file(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    config_dir = config_dir_with(tmp_path, "webhook: [not, a, mapping]\n")

    code = main(["config", "check", "--config-dir", str(config_dir)])

    assert code == 1
    assert "tv_alerts.yaml: HATA" in capsys.readouterr().out


# --- serve: refusals that need no secrets and start nothing ----------------------------------


@pytest.mark.parametrize("mode", ["paper", "approve", "auto"])
def test_serve_refuses_every_mode_but_signal(mode: str, capsys: pytest.CaptureFixture[str]) -> None:
    code = main(["serve", "--mode", mode, "--config-dir", str(CONFIG_DIR), "--no-env-file"])

    captured = capsys.readouterr()
    assert code == 1
    assert captured.err.startswith("HATA: mod kilitli:")
    assert f"'{mode}' modu bu sürümde yok" in captured.err
    assert captured.out == ""  # nothing was started, no banner


def test_serve_rejects_an_unknown_mode_as_a_usage_error(capsys: pytest.CaptureFixture[str]) -> None:
    with pytest.raises(SystemExit) as excinfo:
        main(["serve", "--mode", "live"])

    assert excinfo.value.code == 2
    assert "--mode" in capsys.readouterr().err


def test_serve_needs_a_known_venue(capsys: pytest.CaptureFixture[str]) -> None:
    code = main(["serve", "--config-dir", str(CONFIG_DIR), "--venue", "topstep", "--no-env-file"])

    captured = capsys.readouterr()
    assert code == 1
    assert "venue profili bulunamadı: 'topstep'" in captured.err
    assert "ftmo_2step_100k" in captured.err


def test_serve_needs_the_alert_line_config(capsys: pytest.CaptureFixture[str]) -> None:
    code = main(["serve", "--config-dir", str(VALID_DIR), "--no-env-file"])

    captured = capsys.readouterr()
    assert code == 1
    assert "HATA: config geçersiz" in captured.err
    assert "tv_alerts.yaml: dosya bulunamadı" in captured.err


def test_serve_reports_a_broken_risk_config_before_anything_else(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    config_dir = config_dir_with(tmp_path, TV_FIXTURES / "valid" / "full.yaml")
    (config_dir / "risk.yaml").write_text("risk_per_trade_pct: 5\n", encoding="utf-8")

    code = main(["serve", "--config-dir", str(config_dir), "--no-env-file"])

    assert code == 1
    assert "HATA: config geçersiz" in capsys.readouterr().err


def test_env_file_options_exclude_each_other(capsys: pytest.CaptureFixture[str]) -> None:
    with pytest.raises(SystemExit) as excinfo:
        main(["serve", "--env-file", ".env", "--no-env-file"])

    assert excinfo.value.code == 2
    assert "--no-env-file" in capsys.readouterr().err


# --- signals report -----------------------------------------------------------------------


def database_with_one_signal(tmp_path: Path) -> Path:
    path = tmp_path / "propbot.db"
    bar_close_ms = 1_796_136_300_000  # 2026-12-01 14:45:00 UTC = 17:45 in Istanbul
    with Store(path) as store, store.transaction() as tx:
        signal_id = tx.insert_signal(
            NewSignal(
                received_at_ms=bar_close_ms + 1200,
                strategy_id="ny_orb_fvg",
                symbol="CME_MINI:MES1!",
                tf="1",
                bar_time_ms=bar_close_ms - 60_000,
                bar_close_ms=bar_close_ms,
                action="exit",
                side="long",
                setup="",
                order_type=None,
                est_entry=5123.25,
                stop=None,
                tp=None,
                rr=None,
                reason=None,
                payload_json="{}",
            )
        )
        assert signal_id is not None
        tx.complete_signal(signal_id, Outcome.FORWARDED, bar_close_ms + 1300)
    return path


def test_report_command_prints_the_period_and_writes_the_csv(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    database = database_with_one_signal(tmp_path)
    csv_path = tmp_path / "reports" / "sinyaller.csv"

    code = main(
        [
            "signals",
            "report",
            "--db",
            str(database),
            "--since",
            "2026-12-01",
            "--until",
            "2026-12-01",
            "--csv",
            str(csv_path),
        ]
    )

    out = capsys.readouterr().out
    assert code == 0
    assert out.startswith(f"Sinyal raporu · {database}\n")
    assert "Dönem (Türkiye saati): 2026-12-01 00:00 → 2026-12-02 00:00" in out
    assert "2026-12-01      1          0      0               0      0        0      0" in out
    assert out.rstrip().endswith(f"CSV yazıldı: {csv_path}")
    lines = csv_path.read_text(encoding="utf-8").splitlines()
    assert lines[0] == ",".join(CSV_HEADER)
    assert lines[1].startswith("1,2026-12-01T14:45:01.200Z,ny_orb_fvg,CME_MINI:MES1!,1,exit,long,")


def test_report_command_without_dates_covers_everything_up_to_today(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    database = database_with_one_signal(tmp_path)

    code = main(["signals", "report", "--db", str(database), "--until", "2026-12-31"])

    out = capsys.readouterr().out
    assert code == 0
    assert "Dönem (Türkiye saati): 1970-01-01 02:00 → 2027-01-01 00:00" in out
    assert "(1 alert)" in out


def test_report_command_outside_the_period_finds_nothing(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    database = database_with_one_signal(tmp_path)

    code = main(
        [
            "signals",
            "report",
            "--db",
            str(database),
            "--since",
            "2026-12-02",
            "--until",
            "2026-12-03",
        ]
    )

    assert code == 0
    assert "Bu dönemde alınmış alert yok." in capsys.readouterr().out


def test_report_command_does_not_create_a_missing_database(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    missing = tmp_path / "nowhere" / "propbot.db"

    code = main(["signals", "report", "--db", str(missing)])

    assert code == 1
    assert f"HATA: veritabanı bulunamadı: {missing}" in capsys.readouterr().err
    assert not missing.parent.exists()


@pytest.mark.parametrize(
    ("option", "value"),
    [("--since", "01.12.2026"), ("--until", "2026-13-01"), ("--since", "yesterday")],
)
def test_report_command_rejects_a_malformed_day(
    tmp_path: Path, capsys: pytest.CaptureFixture[str], option: str, value: str
) -> None:
    database = database_with_one_signal(tmp_path)

    code = main(["signals", "report", "--db", str(database), option, value])

    assert code == 1
    assert f"{option} YYYY-AA-GG biçiminde olmalı" in capsys.readouterr().err


def test_report_command_rejects_an_inverted_period(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    database = database_with_one_signal(tmp_path)

    code = main(
        [
            "signals",
            "report",
            "--db",
            str(database),
            "--since",
            "2026-12-02",
            "--until",
            "2026-12-01",
        ]
    )

    assert code == 1
    assert "--until, --since tarihinden önce olamaz" in capsys.readouterr().err


def test_report_command_refuses_a_file_that_is_not_our_database(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    impostor = tmp_path / "notes.db"
    impostor.write_text("not a database", encoding="utf-8")

    code = main(["signals", "report", "--db", str(impostor)])

    assert code == 1
    assert capsys.readouterr().err.startswith("HATA: ")
    assert impostor.read_text(encoding="utf-8") == "not a database"


# --- serve: refusals that happen after the web stack is imported -----------------------------

SECRET_VARIABLES = (
    "TELEGRAM_BOT_TOKEN",
    "TELEGRAM_ALLOWED_CHAT_ID",
    "TV_WEBHOOK_TOKEN",
    "PROPBOT_PIN",
)


@pytest.fixture
def no_secrets(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    """A process without the four variables, in a directory without a .env file."""
    for name in SECRET_VARIABLES:
        monkeypatch.delenv(name, raising=False)
    monkeypatch.chdir(tmp_path)


@pytest.mark.parametrize("host", ["0.0.0.0", "192.168.1.10", "localhost"])
def test_serve_refuses_to_listen_beyond_loopback(
    host: str, capsys: pytest.CaptureFixture[str]
) -> None:
    code = main(["serve", "--config-dir", str(CONFIG_DIR), "--no-env-file", "--host", host])

    captured = capsys.readouterr()
    assert code == 1
    assert f"HATA: --host {host!r} reddedildi" in captured.err
    assert "yalnızca loopback" in captured.err
    assert captured.out == ""


@pytest.mark.usefixtures("no_secrets")
def test_serve_without_secrets_lists_what_is_missing(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    database = tmp_path / "data" / "propbot.db"

    code = main(["serve", "--config-dir", str(CONFIG_DIR), "--no-env-file", "--db", str(database)])

    captured = capsys.readouterr()
    assert code == 1
    assert "HATA: sırlar okunamadı (4 sorun)" in captured.err
    for name in SECRET_VARIABLES:
        assert f"{name}: tanımlı değil" in captured.err
    assert captured.out == ""  # no banner: nothing was started
    assert not database.exists()


@pytest.mark.usefixtures("no_secrets")
def test_serve_refuses_an_env_file_others_can_read(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    env_file = tmp_path / "secrets.env"
    env_file.write_text("PROPBOT_PIN=246810\n", encoding="utf-8")
    env_file.chmod(0o644)

    code = main(["serve", "--config-dir", str(CONFIG_DIR), "--env-file", str(env_file)])

    captured = capsys.readouterr()
    assert code == 1
    assert "chmod 600" in captured.err
    assert "246810" not in captured.err + captured.out


@pytest.mark.usefixtures("no_secrets")
def test_serve_reports_malformed_secrets_without_echoing_them(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    env_file = tmp_path / "secrets.env"
    env_file.write_text(
        "TELEGRAM_BOT_TOKEN=123456:AAshort\n"
        "TELEGRAM_ALLOWED_CHAT_ID=-1001234567890\n"
        "TV_WEBHOOK_TOKEN=too-short-token\n"
        "PROPBOT_PIN=12ab\n",
        encoding="utf-8",
    )
    env_file.chmod(0o600)

    code = main(["serve", "--config-dir", str(CONFIG_DIR), "--env-file", str(env_file)])

    captured = capsys.readouterr()
    assert code == 1
    assert "HATA: sırlar okunamadı (4 sorun)" in captured.err
    assert "TELEGRAM_ALLOWED_CHAT_ID: pozitif olmalı" in captured.err
    for value in ("AAshort", "too-short-token", "12ab"):
        assert value not in captured.err + captured.out
