"""Telegram message texts: one golden text per template variant (SPEC §5.4, contract §6.1)."""

import datetime as dt
import math
from collections.abc import Mapping
from typing import Any

import pytest
from hypothesis import given
from hypothesis import strategies as st

from propbot.core.clock import ms_from_datetime
from propbot.notify import messages
from propbot.notify.messages import TradeLink
from propbot.risk import RiskConfig, VenueProfile
from propbot.risk.loader import read_yaml_mapping
from propbot.risk.sizing import SizeSkip, SizeSuggestion, suggest_size
from propbot.risk.venue import rules_freshness
from propbot.store.db import Outcome, SignalRow, SignalStatus
from support.paths import CONFIG_FIXTURES

RISK_FILE = CONFIG_FIXTURES / "valid" / "risk.yaml"
VENUE_FILE = CONFIG_FIXTURES / "valid" / "venues" / "sample_2step.yaml"

# Index CFD: one lot earns 1.0 per point, sizes in hundredths of a lot.
INDEX_CFD = {"tick_size": 0.01, "tick_value_per_lot": 0.01, "min_lot": 0.01, "lot_step": 0.01}
NO_CONTRACT = dict.fromkeys(INDEX_CFD)


def utc(text: str) -> int:
    return ms_from_datetime(dt.datetime.fromisoformat(text).replace(tzinfo=dt.UTC))


# Monday 2026-11-16, 09:44 New York (EST) = 17:44 Istanbul: the bar OPEN of the examples.
BAR_OPEN_MS = utc("2026-11-16 14:44")
BAR_CLOSE_MS = BAR_OPEN_MS + 60_000
VALID_UNTIL_MS = BAR_CLOSE_MS + 60_000


def risk_config() -> RiskConfig:
    return RiskConfig.model_validate(read_yaml_mapping(RISK_FILE))


def venue_profile(
    contract: Mapping[str, float | None] | None = None, **overrides: object
) -> VenueProfile:
    data = read_yaml_mapping(VENUE_FILE)
    data.update({"venue": "ftmo", "symbol": "US500.cash", "rules_verified_at": "2026-11-13"})
    data["contract"] = dict(INDEX_CFD if contract is None else contract)
    data.update(overrides)
    return VenueProfile.model_validate(data)


RISK = risk_config()
VENUE = venue_profile()
VENUE_UNKNOWN = venue_profile(NO_CONTRACT)


def row(**overrides: Any) -> SignalRow:
    """The reference alert as a stored row: ny_orb_fvg long FVG entry, id 123."""
    values: dict[str, Any] = {
        "id": 123,
        "received_at_ms": BAR_CLOSE_MS + 1_000,
        "strategy_id": "ny_orb_fvg",
        "symbol": "CME_MINI:MES1!",
        "tf": "1",
        "bar_time_ms": BAR_OPEN_MS,
        "bar_close_ms": BAR_CLOSE_MS,
        "action": "entry",
        "side": "long",
        "setup": "FVG",
        "order_type": "market",
        "est_entry": 5123.25,
        "stop": 5110.75,
        "tp": 5148.25,
        "rr": 2.0,
        "reason": None,
        "payload_json": "{}",
        "status": SignalStatus.QUEUED,
        "outcome": None,
        "processed_at_ms": None,
    }
    values.update(overrides)
    return SignalRow(**values)


SHORT_LEVELS = {"side": "short", "stop": 5135.75, "tp": 5098.25}


def entry_text(
    signal: SignalRow,
    *,
    venue: VenueProfile = VENUE,
    budget_used_before: float = 250.0,
    rules_unverified: bool = False,
) -> str:
    assert signal.est_entry is not None
    assert signal.stop is not None
    size = suggest_size(stop_points=abs(signal.est_entry - signal.stop), risk=RISK, venue=venue)
    risk_amount = size.risk_budget if size.risk_at_lots is None else size.risk_at_lots
    return messages.entry_signal(
        signal,
        risk=RISK,
        venue=venue,
        size=size,
        risk_amount=risk_amount,
        budget_used_after=budget_used_before + risk_amount,
        budget_cap=1000.0,
        valid_until_ms=VALID_UNTIL_MS,
        rules_unverified=rules_unverified,
    )


# --- entry signal -------------------------------------------------------------------------

LONG_ENTRY = (
    "[SİNYAL] NY ORB FVG · FVG · LONG · CME_MINI:MES1!\n"
    "Referans giriş ≈ 5123.25 (TradingView bar kapanışı)\n"
    "Stop 5110.75 (−12.50 puan) · TP 5148.25 (+25.00 puan, 2.00R)\n"
    "FTMO US500.cash'te PİYASA emri; SL/TP'yi emirle BİRLİKTE gir: giriş anındaki fiyata göre "
    "stop −12.50 puan, TP +25.00 puan\n"
    "Önerilen boyut: 20.00 lot (≈ $102465 nominal, 1.0× hesap) · risk ≈ $250 (%0.25) · "
    "günlük bütçe $500/$1000 (bu sinyal dahil)\n"
    "Geçerlilik: 17:46:00 TR'ye kadar ve TradingView'da (CME_MINI:MES1!) fiyat "
    "5120.125–5126.375 aralığında — değilse ATLA\n"
    "TradingView'da stop/TP dolunca mesaj gelmez; pozisyonun hâlâ açıksa seans sonunda "
    "kendin kapat.\n"
    "Not: trailing stop güncellemeleri ayrı mesajla gelir.\n"
    "id 123 · bar 09:44 NY / 17:44 TR"
)


def test_long_market_entry_matches_the_contract_layout() -> None:
    assert entry_text(row()) == LONG_ENTRY


def test_short_entry_mirrors_every_sign() -> None:
    assert entry_text(row(**SHORT_LEVELS)) == (
        "[SİNYAL] NY ORB FVG · FVG · SHORT · CME_MINI:MES1!\n"
        "Referans giriş ≈ 5123.25 (TradingView bar kapanışı)\n"
        "Stop 5135.75 (+12.50 puan) · TP 5098.25 (−25.00 puan, 2.00R)\n"
        "FTMO US500.cash'te PİYASA emri; SL/TP'yi emirle BİRLİKTE gir: giriş anındaki fiyata "
        "göre stop +12.50 puan, TP −25.00 puan\n"
        "Önerilen boyut: 20.00 lot (≈ $102465 nominal, 1.0× hesap) · risk ≈ $250 (%0.25) · "
        "günlük bütçe $500/$1000 (bu sinyal dahil)\n"
        "Geçerlilik: 17:46:00 TR'ye kadar ve TradingView'da (CME_MINI:MES1!) fiyat "
        "5120.125–5126.375 aralığında — değilse ATLA\n"
        "TradingView'da stop/TP dolunca mesaj gelmez; pozisyonun hâlâ açıksa seans sonunda "
        "kendin kapat.\n"
        "Not: trailing stop güncellemeleri ayrı mesajla gelir.\n"
        "id 123 · bar 09:44 NY / 17:44 TR"
    )


def test_limit_entry_names_the_level_and_has_no_price_band() -> None:
    signal = row(
        strategy_id="ict_silver_bullet",
        setup="SB_FVG",
        side="short",
        order_type="limit",
        est_entry=5123.25,
        stop=5128.25,
        tp=5113.25,
    )

    assert entry_text(signal, budget_used_before=0.0) == (
        "[SİNYAL] ICT Silver Bullet · SB_FVG · SHORT · CME_MINI:MES1!\n"
        "Limit giriş 5123.25 (TradingView seviyesi; FTMO fiyatı baz farkı kadar kayabilir — "
        "seviyeyi iki grafik arasındaki farkla çevir)\n"
        "Stop 5128.25 (+5.00 puan) · TP 5113.25 (−10.00 puan, 2.00R)\n"
        "FTMO US500.cash'te LİMİT emri; SL/TP'yi emirle BİRLİKTE gir: limit fiyatına göre "
        "stop +5.00 puan, TP −10.00 puan\n"
        "Önerilen boyut: 50.00 lot (≈ $256162.50 nominal, 2.6× hesap) · risk ≈ $250 "
        "(%0.25) · günlük bütçe $250/$1000 (bu sinyal dahil)\n"
        "Emri 17:46:00 TR'ye kadar gir; İPTAL mesajı gelene kadar bekler\n"
        "TradingView'da stop/TP dolunca mesaj gelmez; pozisyonun hâlâ açıksa seans sonunda "
        "kendin kapat.\n"
        "Not: limit dolmazsa İPTAL mesajı gelir.\n"
        "id 123 · bar 09:44 NY / 17:44 TR"
    )


def test_entry_without_a_target_says_so_and_gives_no_target_offset() -> None:
    text = entry_text(row(tp=None))

    assert text.splitlines()[2] == "Stop 5110.75 (−12.50 puan) · TP yok"
    assert text.splitlines()[3] == (
        "FTMO US500.cash'te PİYASA emri; SL'yi emirle BİRLİKTE gir: giriş anındaki fiyata göre "
        "stop −12.50 puan, TP yok"
    )
    # Everything else is the ordinary message.
    assert [line for i, line in enumerate(text.splitlines()) if i not in (2, 3)] == [
        line for i, line in enumerate(LONG_ENTRY.splitlines()) if i not in (2, 3)
    ]


def test_unknown_contract_gives_the_formula_instead_of_a_lot_size() -> None:
    text = entry_text(row(), venue=VENUE_UNKNOWN)

    assert text.splitlines()[4] == (
        "Önerilen boyut: hesaplanamadı (venue profilinde contract.* boş) · en fazla risk $250 "
        "(%0.25) → lot = 250 ÷ (12.50 × 1 lotun 1 puanlık değeri), AŞAĞI yuvarla · "
        "günlük bütçe $500/$1000 (bu sinyal dahil)"
    )
    assert [line for i, line in enumerate(text.splitlines()) if i != 4] == [
        line for i, line in enumerate(LONG_ENTRY.splitlines()) if i != 4
    ]


def test_the_reward_multiple_comes_from_the_levels_not_from_the_alert() -> None:
    # Mean reversion prints rr with two decimals; a target one tick away arrives as 0.
    signal = row(
        strategy_id="mr_bb_kc_rsi2",
        setup="BB_KC_RSI2",
        est_entry=5101.5,
        stop=5093.0,
        tp=5109.25,
        rr=0.0,
    )

    assert entry_text(signal).splitlines()[2] == (
        "Stop 5093.00 (−8.50 puan) · TP 5109.25 (+7.75 puan, 0.91R)"
    )
    assert entry_text(row(rr=None)).splitlines()[2] == LONG_ENTRY.splitlines()[2]


@pytest.mark.parametrize(
    ("strategy_id", "note"),
    [
        ("ny_orb_fvg", "Not: trailing stop güncellemeleri ayrı mesajla gelir."),
        ("ict_silver_bullet", "Not: limit dolmazsa İPTAL mesajı gelir."),
        (
            "mr_bb_kc_rsi2",
            "Not: TP dinamiktir (BB ortası, her bar değişir); TradingView'dan izle.",
        ),
        (
            "mtf_trend_cont",
            "Not: TP = TP1 (kısmi çıkış seviyesi; kapatılacak pay stratejinin scale-out "
            "girdisidir, TradingView'dan oku); kalan kısım için stop güncellemeleri gelir.",
        ),
        ("asian_sweep", None),
    ],
)
def test_each_strategy_has_its_own_note_line(strategy_id: str, note: str | None) -> None:
    lines = entry_text(row(strategy_id=strategy_id)).splitlines()

    assert lines[-1] == "id 123 · bar 09:44 NY / 17:44 TR"
    assert lines[-3 if note else -2] == (
        "TradingView'da stop/TP dolunca mesaj gelmez; pozisyonun hâlâ açıksa seans sonunda "
        "kendin kapat."
    )
    if note is None:
        assert not any(line.startswith("Not:") for line in lines)
        assert len(lines) == 8
    else:
        assert lines[-2] == note
        assert len(lines) == 9


def test_unverified_rules_put_a_banner_on_top() -> None:
    text = entry_text(row(), rules_unverified=True)

    assert text == (
        "[KURALLAR DOĞRULANMADI — boyut ve bütçe doğrulanmamış kurallara göre]\n" + LONG_ENTRY
    )


def test_other_account_currencies_are_written_after_the_amount() -> None:
    text = entry_text(row(), venue=venue_profile(account_currency="EUR"))

    assert text.splitlines()[4] == (
        "Önerilen boyut: 20.00 lot (≈ 102465 EUR nominal, 1.0× hesap) · risk ≈ 250 EUR "
        "(%0.25) · günlük bütçe 500 EUR/1000 EUR (bu sinyal dahil)"
    )


def test_expired_notice_names_the_entry_and_the_deadline() -> None:
    assert messages.entry_expired_notice(row(), valid_until_ms=VALID_UNTIL_MS) == (
        "[İLETİLEMEDİ — ATLA] id 123 · NY ORB FVG LONG: sinyal Telegram'a zamanında ulaşmadı "
        "(geçerlilik 17:46:00 TR'de bitti). Bütçeden düşüldü."
    )


# --- skipped entries ----------------------------------------------------------------------


def test_stale_notice_says_how_late_and_the_limit() -> None:
    assert messages.entry_stale_notice(row(), delay_seconds=200.4, validity_seconds=60) == (
        "[BAYAT SİNYAL — ATLA] NY ORB FVG · FVG · LONG · CME_MINI:MES1!\n"
        "Sinyal bar kapanışından 3 dk 20 sn sonra işlenebildi (geçerlilik sınırı 1 dk). "
        "Fiyat artık referans değil: bu girişi ALMA.\n"
        "id 123 · bar 09:44 NY / 17:44 TR"
    )


def test_clock_skip_notice_points_at_the_server_clock() -> None:
    assert messages.entry_clock_skip_notice(row(), early_seconds=5.0) == (
        "[SİNYAL — ATLA] NY ORB FVG · FVG · LONG · CME_MINI:MES1!\n"
        "Sunucu saati TradingView ile uyuşmuyor (alert bar kapanışından 5 sn önce geldi); "
        "geçerlilik hesaplanamıyor. /saglik ile saati kontrol et.\n"
        "id 123 · bar 09:44 NY / 17:44 TR"
    )


def test_risk_too_wide_notice_shows_the_risk_of_the_minimum_lot() -> None:
    venue = venue_profile(INDEX_CFD | {"min_lot": 25.0})
    size = suggest_size(stop_points=12.5, risk=RISK, venue=venue)
    assert size.skip is SizeSkip.RISK_TOO_WIDE

    text = messages.entry_risk_too_wide_notice(row(), venue=venue, size=size, stop_points=12.5)

    assert text == (
        "[SİNYAL — ATLA] NY ORB FVG · FVG · LONG · CME_MINI:MES1!\n"
        "Stop çok geniş (12.50 puan): en küçük lot (25.00) bile ≈ $312.50 risk taşır; "
        "işlem başı sınır $250. Bu girişi ALMA.\n"
        "id 123 · bar 09:44 NY / 17:44 TR"
    )


def test_risk_too_wide_notice_survives_an_unknown_contract() -> None:
    size = SizeSuggestion(250.0, None, None, SizeSkip.RISK_TOO_WIDE)

    text = messages.entry_risk_too_wide_notice(
        row(), venue=VENUE_UNKNOWN, size=size, stop_points=12.5
    )

    assert text.splitlines()[1] == (
        "Stop çok geniş (12.50 puan): en küçük lot bile işlem başı risk sınırını ($250) "
        "aşıyor. Bu girişi ALMA."
    )


def test_budget_skip_notice_shows_the_sum_and_the_release_command() -> None:
    text = messages.entry_budget_skip_notice(
        row(), venue=VENUE, budget_used=900.0, risk_amount=249.5, budget_cap=1000.0
    )

    assert text == (
        "[SİNYAL — ATLA] NY ORB FVG · FVG · LONG · CME_MINI:MES1!\n"
        "Günlük risk bütçesi yetmiyor: kullanılan $900 + bu sinyal $249.50 > sınır $1000. "
        "Bu girişi ALMA.\n"
        "Bugün almadığın bir giriş bütçede sayılıyorsa serbest bırak: /atladim <id>\n"
        "id 123 · bar 09:44 NY / 17:44 TR"
    )


# --- stop update ---------------------------------------------------------------------------

# 10:02 New York = 18:02 Istanbul.
UPDATE_BAR_MS = utc("2026-11-16 15:02")


def stop_update(**overrides: Any) -> SignalRow:
    values: dict[str, Any] = {
        "id": 124,
        "action": "stop_update",
        "setup": "",
        "bar_time_ms": UPDATE_BAR_MS,
        "bar_close_ms": UPDATE_BAR_MS + 60_000,
        "received_at_ms": UPDATE_BAR_MS + 61_000,
        "est_entry": 5123.5,
        "stop": 5120.0,
        "tp": 5149.0,
    }
    values.update(overrides)
    return row(**values)


LINKED = TradeLink(entry_id=123)


def test_stop_update_matches_the_contract_layout() -> None:
    text = messages.stop_update_signal(
        stop_update(), venue=VENUE, link=LINKED, current_offset=-12.5
    )

    assert text == (
        "[STOP GÜNCELLE] NY ORB FVG · LONG · CME_MINI:MES1!\n"
        "Stop: kendi giriş fiyatına göre −12.50 → −3.50 puan (TradingView seviyesi 5120.00)\n"
        "FTMO US500.cash'te uygula: stop = kendi giriş fiyatın −3.50 puan\n"
        "id 124 (giriş id 123) · bar 10:02 NY / 18:02 TR"
    )


@pytest.mark.parametrize(
    ("side", "stop", "expected"),
    [
        ("long", 5125.5, "stop = kendi giriş fiyatın +2.00 puan (kâr kilitli)"),
        ("short", 5121.5, "stop = kendi giriş fiyatın −2.00 puan (kâr kilitli)"),
        ("long", 5123.5, "stop = kendi giriş fiyatın +0.00 puan (başabaş)"),
        ("short", 5123.5, "stop = kendi giriş fiyatın +0.00 puan (başabaş)"),
        ("long", 5121.5, "stop = kendi giriş fiyatın −2.00 puan"),
        ("short", 5125.5, "stop = kendi giriş fiyatın +2.00 puan"),
    ],
)
def test_stop_update_marks_a_stop_on_the_profit_side(side: str, stop: float, expected: str) -> None:
    text = messages.stop_update_signal(
        stop_update(side=side, stop=stop, tp=None), venue=VENUE, link=LINKED, current_offset=None
    )

    assert text.splitlines()[2] == f"FTMO US500.cash'te uygula: {expected}"


def test_stop_update_of_an_unseen_entry_asks_the_user_to_compare() -> None:
    text = messages.stop_update_signal(
        stop_update(), venue=VENUE, link=TradeLink(), current_offset=None
    )

    assert text == (
        "[STOP GÜNCELLE] NY ORB FVG · LONG · CME_MINI:MES1!\n"
        "Stop: kendi giriş fiyatına göre −3.50 puan (TradingView seviyesi 5120.00)\n"
        "FTMO US500.cash'te uygula: stop = kendi giriş fiyatın −3.50 puan\n"
        "(bu bot giriş sinyalini görmedi) Yalnızca mevcut stop'undan DAHA SIKIysa uygula.\n"
        "id 124 · bar 10:02 NY / 18:02 TR"
    )


def test_stop_update_of_an_unseen_entry_with_a_known_previous_update() -> None:
    text = messages.stop_update_signal(
        stop_update(), venue=VENUE, link=TradeLink(), current_offset=-6.0
    )

    assert text.splitlines()[1:] == [
        "Stop: kendi giriş fiyatına göre −6.00 → −3.50 puan (TradingView seviyesi 5120.00)",
        "FTMO US500.cash'te uygula: stop = kendi giriş fiyatın −3.50 puan",
        "(bu bot giriş sinyalini görmedi)",
        "id 124 · bar 10:02 NY / 18:02 TR",
    ]


def test_follow_up_after_an_undelivered_entry_carries_the_note() -> None:
    link = TradeLink(entry_id=123, entry_undelivered=True)

    text = messages.stop_update_signal(stop_update(), venue=VENUE, link=link, current_offset=-12.5)

    assert text.splitlines()[-2:] == [
        "(giriş mesajı sana ulaşmamış olabilir; pozisyon açmadıysan yok say)",
        "id 124 (giriş id 123) · bar 10:02 NY / 18:02 TR",
    ]


def test_late_follow_up_is_prefixed_with_its_age() -> None:
    link = TradeLink(entry_id=123, late_seconds=200.0)

    text = messages.stop_update_signal(stop_update(), venue=VENUE, link=link, current_offset=-12.5)

    assert text.splitlines()[0] == (
        "[GEÇ GELDİ +3 dk 20 sn] [STOP GÜNCELLE] NY ORB FVG · LONG · CME_MINI:MES1!"
    )


def test_widening_warning_tells_the_user_to_keep_the_current_stop() -> None:
    signal = stop_update(est_entry=5125.25, stop=5112.0)

    text = messages.stop_widen_warning(signal, venue=VENUE, link=LINKED, current_offset=-12.5)

    assert text == (
        "[UYARI] NY ORB FVG · LONG · CME_MINI:MES1!\n"
        "Strateji stopu GENİŞLETMEK istiyor (kendi giriş fiyatına göre −12.50 → −13.25 puan; "
        "TradingView seviyesi 5112.00). UYGULAMA: açık pozisyonun stopu genişletilmez.\n"
        "FTMO US500.cash'te stopunu KORU: stop = kendi giriş fiyatın −12.50 puan\n"
        "id 124 (giriş id 123) · bar 10:02 NY / 18:02 TR"
    )


# --- exit and cancel -----------------------------------------------------------------------

# 15:59 New York = 23:59 Istanbul.
EXIT_BAR_MS = utc("2026-11-16 20:59")


def exit_row(**overrides: Any) -> SignalRow:
    values: dict[str, Any] = {
        "id": 125,
        "action": "exit",
        "setup": "",
        "bar_time_ms": EXIT_BAR_MS,
        "bar_close_ms": EXIT_BAR_MS + 60_000,
        "received_at_ms": EXIT_BAR_MS + 61_000,
        "est_entry": 5131.5,
        "stop": None,
        "tp": None,
    }
    values.update(overrides)
    return row(**values)


def test_exit_matches_the_contract_layout() -> None:
    assert messages.exit_signal(exit_row(), venue=VENUE, link=LINKED) == (
        "[ÇIKIŞ] NY ORB FVG · LONG · CME_MINI:MES1!\n"
        "Strateji pozisyonu kapattı (seans sonu / günlük zarar kesicisi / test aralığı sonu).\n"
        "FTMO US500.cash'te pozisyon açıksa PİYASADAN KAPAT.\n"
        "id 125 (giriş id 123) · bar 15:59 NY / 23:59 TR"
    )


def test_exit_of_an_unseen_entry_carries_the_orphan_note() -> None:
    text = messages.exit_signal(exit_row(), venue=VENUE, link=TradeLink(late_seconds=75.0))

    assert text == (
        "[GEÇ GELDİ +1 dk 15 sn] [ÇIKIŞ] NY ORB FVG · LONG · CME_MINI:MES1!\n"
        "Strateji pozisyonu kapattı (seans sonu / günlük zarar kesicisi / test aralığı sonu).\n"
        "FTMO US500.cash'te pozisyon açıksa PİYASADAN KAPAT.\n"
        "(bu bot giriş sinyalini görmedi)\n"
        "id 125 · bar 15:59 NY / 23:59 TR"
    )


# 10:20 New York = 18:20 Istanbul.
CANCEL_BAR_MS = utc("2026-11-16 15:20")


def cancel_row(**overrides: Any) -> SignalRow:
    values: dict[str, Any] = {
        "id": 127,
        "strategy_id": "ict_silver_bullet",
        "action": "cancel",
        "side": "short",
        "setup": "SB_FVG",
        "order_type": "limit",
        "bar_time_ms": CANCEL_BAR_MS,
        "bar_close_ms": CANCEL_BAR_MS + 60_000,
        "received_at_ms": CANCEL_BAR_MS + 61_000,
        "est_entry": 5123.25,
        "stop": 5128.75,
        "tp": 5112.25,
    }
    values.update(overrides)
    return row(**values)


def test_cancel_matches_the_contract_layout() -> None:
    text = messages.cancel_signal(
        cancel_row(), venue=VENUE, link=TradeLink(entry_id=126), release_hint=True
    )

    assert text == (
        "[İPTAL] ICT Silver Bullet · SHORT · CME_MINI:MES1!\n"
        "Bekleyen limit emir geri çekildi (seviye 5123.25).\n"
        "FTMO US500.cash'te emir dolmadıysa İPTAL ET ve bütçeyi serbest bırak: /atladim 126\n"
        "Emir dolduysa: TradingView'da bu pozisyon YOK, başka mesaj gelmeyecek — stop/TP ile "
        "yönet ve seans sonunda kendin kapat.\n"
        "id 127 (giriş id 126) · bar 10:20 NY / 18:20 TR"
    )


def test_cancel_without_a_counted_entry_offers_no_release_command() -> None:
    released = messages.cancel_signal(
        cancel_row(), venue=VENUE, link=TradeLink(entry_id=126), release_hint=False
    )
    orphan = messages.cancel_signal(
        cancel_row(est_entry=None), venue=VENUE, link=TradeLink(), release_hint=True
    )

    assert released.splitlines()[2] == "FTMO US500.cash'te emir dolmadıysa İPTAL ET."
    assert "/atladim" not in released
    assert orphan.splitlines()[:3] == [
        "[İPTAL] ICT Silver Bullet · SHORT · CME_MINI:MES1!",
        "Bekleyen limit emir geri çekildi.",
        "FTMO US500.cash'te emir dolmadıysa İPTAL ET.",
    ]
    assert orphan.splitlines()[-2:] == [
        "(bu bot giriş sinyalini görmedi)",
        "id 127 · bar 10:20 NY / 18:20 TR",
    ]


def test_every_builder_tolerates_a_row_without_optional_fields() -> None:
    bare = row(side=None, setup="", order_type=None, est_entry=None, stop=None, tp=None, rr=None)
    size = SizeSuggestion(250.0, None, None, SizeSkip.CONTRACT_UNKNOWN)
    link = TradeLink()

    texts = [
        messages.entry_signal(
            bare,
            risk=RISK,
            venue=VENUE_UNKNOWN,
            size=size,
            risk_amount=250.0,
            budget_used_after=250.0,
            budget_cap=1000.0,
            valid_until_ms=VALID_UNTIL_MS,
        ),
        messages.entry_expired_notice(bare, valid_until_ms=VALID_UNTIL_MS),
        messages.entry_stale_notice(bare, delay_seconds=1.0, validity_seconds=60),
        messages.entry_clock_skip_notice(bare, early_seconds=3.0),
        messages.entry_risk_too_wide_notice(bare, venue=VENUE, size=size, stop_points=1.0),
        messages.entry_budget_skip_notice(
            bare, venue=VENUE, budget_used=0.0, risk_amount=1.0, budget_cap=1.0
        ),
        messages.stop_update_signal(bare, venue=VENUE, link=link, current_offset=None),
        messages.stop_widen_warning(bare, venue=VENUE, link=link, current_offset=-1.0),
        messages.exit_signal(bare, venue=VENUE, link=link),
        messages.cancel_signal(bare, venue=VENUE, link=link, release_hint=True),
        messages.side_mismatch_alarm(bare, bare),
        messages.processing_error_alarm(bare),
    ]

    assert all(text and "None" not in text for text in texts)
    assert texts[0].splitlines()[:4] == [
        "[SİNYAL] NY ORB FVG · ? · CME_MINI:MES1!",
        "Referans giriş ≈ ? (TradingView bar kapanışı)",
        "Stop ? (? puan) · TP yok",
        "FTMO US500.cash'te PİYASA emri; SL'yi emirle BİRLİKTE gir: giriş anındaki fiyata göre "
        "stop ? puan, TP yok",
    ]
    assert texts[0].splitlines()[5] == "Geçerlilik: 17:46:00 TR'ye kadar — sonrasında ATLA"


# --- alarms raised while processing --------------------------------------------------------


@pytest.mark.parametrize(
    ("verified", "today", "expected"),
    [
        (None, "2026-11-16", "rules_verified_at boş"),
        ("2026-10-01", "2026-11-16", "son doğrulama 2026-10-01 (46 gün önce, sınır 30 gün)"),
        ("2026-12-24", "2026-11-16", "rules_verified_at ileri tarihli (2026-12-24)"),
    ],
)
def test_rules_stale_alarm_explains_what_is_wrong(
    verified: str | None, today: str, expected: str
) -> None:
    venue = venue_profile(rules_verified_at=verified)
    freshness = rules_freshness(venue, dt.date.fromisoformat(today))
    assert freshness.order_modes_locked

    assert messages.rules_stale_alarm(venue, freshness) == (
        f"[ALARM] FTMO kuralları güncel doğrulanmamış: {expected}.\n"
        "Sinyaller iletiliyor, ama önerilen boyut ve günlük bütçe doğrulanmamış kurallara "
        "göre hesaplanıyor.\n"
        "Kuralları firmanın resmi sayfasından yeniden doğrula ve venue profilindeki "
        "rules_verified_at tarihini güncelle. Bu uyarı en fazla günde bir gelir."
    )


def test_side_mismatch_alarm_names_both_signals() -> None:
    text = messages.side_mismatch_alarm(stop_update(side="short"), row())

    assert text == (
        "[ALARM] NY ORB FVG · CME_MINI:MES1!: SHORT yönlü stop güncellemesi geldi (id 124), "
        "ama son giriş (id 123) LONG yönlüydü. Güncelleme İLETİLMEDİ.\n"
        "Bot bir giriş sinyalini kaçırmış olabilir: TradingView'da stratejinin açık "
        "pozisyonunu ve Alert Log'u kontrol et; pozisyonun varsa stopunu TradingView'dan izle."
    )


PROCESSING_ERROR_ADVICE = (
    "TradingView Alert Log'dan bu alarmı elle kontrol et; açık pozisyonun varsa stop/çıkışı "
    "TradingView'dan izle.\n"
    "Ayrıntı sunucu logunda: signal_processing_failed."
)


def test_processing_error_alarm_identifies_the_signal() -> None:
    assert messages.processing_error_alarm(row(outcome=Outcome.ERROR)) == (
        "[ALARM] Bir TradingView alert'i işlenemedi ve İLETİLMEDİ (id 123 · NY ORB FVG · "
        "giriş · LONG · CME_MINI:MES1! · bar 09:44 NY / 17:44 TR).\n" + PROCESSING_ERROR_ADVICE
    )


class Unprintable:
    def __str__(self) -> str:
        raise RuntimeError("no text")


NASTY_ROWS: dict[str, dict[str, Any]] = {
    "all_none": dict.fromkeys(SignalRow.__slots__),
    "wrong_types": {
        "id": "abc",
        "strategy_id": 7,
        "action": ["entry"],
        "side": 3.5,
        "symbol": {"a": 1},
        "bar_time_ms": "yesterday",
    },
    "huge_and_non_finite": {
        "id": 10**40,
        "bar_time_ms": 10**30,
        "est_entry": math.nan,
        "stop": math.inf,
    },
    "negative_time": {"bar_time_ms": -(10**18)},
    "control_characters": {
        "strategy_id": "x\n[SİNYAL] sahte\x00\x1b[31m",
        "symbol": "𐏿‮",
        "side": "lo\nng",
        "action": "\t",
    },
    "very_long": {"strategy_id": "s" * 100_000, "symbol": "y" * 100_000, "action": "a" * 5000},
    "unprintable_objects": {
        "id": Unprintable(),
        "strategy_id": Unprintable(),
        "action": Unprintable(),
        "side": Unprintable(),
        "symbol": Unprintable(),
        "bar_time_ms": Unprintable(),
    },
    "empty_text": {"strategy_id": "", "symbol": "", "action": "", "side": ""},
}


def assert_storable_alarm(text: str) -> None:
    assert text.startswith("[ALARM] Bir TradingView alert'i işlenemedi ve İLETİLMEDİ")
    assert text.endswith(PROCESSING_ERROR_ADVICE)
    assert len(text) < 700
    # One header line plus the two advice lines: row content cannot add lines or escapes.
    assert len(text.splitlines()) == 3
    assert all(char.isprintable() or char == "\n" for char in text)
    text.encode("utf-8")  # SQLite refuses text that is not valid UTF-8


@pytest.mark.parametrize("name", sorted(NASTY_ROWS))
def test_processing_error_alarm_cannot_be_broken_by_row_content(name: str) -> None:
    assert_storable_alarm(messages.processing_error_alarm(row(**NASTY_ROWS[name])))


def test_processing_error_alarm_survives_something_that_is_not_a_row() -> None:
    for junk in (None, 42, "text", object()):
        assert messages.processing_error_alarm(junk) == (  # type: ignore[arg-type]
            "[ALARM] Bir TradingView alert'i işlenemedi ve İLETİLMEDİ.\n" + PROCESSING_ERROR_ADVICE
        )


_anything = st.one_of(
    st.none(),
    st.booleans(),
    st.integers(),
    st.floats(allow_nan=True, allow_infinity=True),
    st.text(),
    st.binary(max_size=20),
    st.lists(st.integers(), max_size=3),
)


@given(values=st.fixed_dictionaries(dict.fromkeys(SignalRow.__slots__, _anything)))
def test_processing_error_alarm_never_raises(values: dict[str, Any]) -> None:
    assert_storable_alarm(messages.processing_error_alarm(SignalRow(**values)))


# --- alert-line monitor --------------------------------------------------------------------


def test_silence_alarm_says_how_long_and_the_limit() -> None:
    assert messages.heartbeat_silence_alarm(76 * 60 + 3.2, 75) == (
        "[ALARM] TradingView alarm hattı sessiz: son 1 sa 16 dk içinde hiçbir alert gelmedi "
        "(sınır 75 dk).\n"
        "Sinyaller de GELMİYOR olabilir: TradingView'da alarmların aktif olduğunu (heartbeat "
        "göstergesi dahil) ve Alert Log'u kontrol et; açık pozisyonun varsa stop/çıkışı "
        "TradingView'dan izle.\n"
        "Tatil ya da erken kapanış günüyse yok say."
    )


def test_recovered_notice() -> None:
    assert messages.heartbeat_recovered_notice() == (
        "[BİLGİ] TradingView alarm hattı geri geldi: yeniden alert alınıyor."
    )


def test_downtime_alarm_within_one_day_shows_clock_times_only() -> None:
    text = messages.downtime_alarm(utc("2026-11-16 14:10"), utc("2026-11-16 14:25:40"))

    assert text == (
        "[ALARM] Bot 17:10–17:25 TR arasında kapalıydı; bu aralıktaki TradingView alarmları "
        "alınmadı. Açık pozisyonun varsa stop/çıkışı TradingView'dan kontrol et."
    )


def test_downtime_alarm_across_midnight_adds_the_dates() -> None:
    # 23:50 Istanbul on Monday to 00:20 on Tuesday.
    text = messages.downtime_alarm(utc("2026-11-16 20:50"), utc("2026-11-16 21:20"))

    assert text.splitlines()[0].startswith(
        "[ALARM] Bot 2026-11-16 23:50 – 2026-11-17 00:20 TR arasında kapalıydı;"
    )


def test_expiry_reminder_lists_each_alert_with_its_state() -> None:
    items = [
        ("Asian Sweep", dt.date(2026, 11, 14), -2),
        ("NY ORB FVG", dt.date(2026, 11, 16), 0),
        ("heartbeat", dt.date(2026, 11, 17), 1),
        ("MTF Trend", dt.date(2026, 11, 23), 7),
    ]

    assert messages.alert_expiry_reminder(items) == (
        "[HATIRLATMA] TradingView alarmlarının süresi doluyor:\n"
        "• Asian Sweep — 2026-11-14 (2 gün önce BİTTİ)\n"
        "• NY ORB FVG — 2026-11-16 (BUGÜN bitiyor)\n"
        "• heartbeat — 2026-11-17 (1 gün kaldı)\n"
        "• MTF Trend — 2026-11-23 (7 gün kaldı)\n"
        "Süresi biten alarm sinyal GÖNDERMEZ: TradingView'da alarmı yeniden kur ve "
        "config/tv_alerts.yaml'daki expires_on tarihini güncelle."
    )


def test_expiry_reminder_stays_below_the_limit_and_counts_the_rest() -> None:
    items = [(f"{index:03d}-" + "n" * 60, dt.date(2026, 11, 17), 1) for index in range(120)]

    text = messages.alert_expiry_reminder(items)
    lines = text.splitlines()
    shown = [line for line in lines if line.startswith("• ")]

    assert len(text) < messages.EXPIRY_REMINDER_MAX_CHARS == 3500
    assert 0 < len(shown) < 120
    assert shown == [f"• {name} — 2026-11-17 (1 gün kaldı)" for name, _, _ in items[: len(shown)]]
    assert lines[-2] == f"… ve {120 - len(shown)} alarm daha"
    assert lines[-1].startswith("Süresi biten alarm sinyal GÖNDERMEZ")
    # One more line would have crossed the limit: the list is cut as late as possible.
    assert len(text) + len(shown[0]) + 1 >= 3500


def test_expiry_reminder_bounds_an_absurd_name() -> None:
    text = messages.alert_expiry_reminder([("x" * 10_000 + "\nsahte", dt.date(2026, 11, 17), 1)])

    assert len(text) < 400
    assert len(text.splitlines()) == 3
