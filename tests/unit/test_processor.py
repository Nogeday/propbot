"""Signal processor: the decision table (contract §6.1), its store work and the queue task."""

import asyncio
import dataclasses
import datetime as dt
import re
from collections.abc import Callable, Coroutine, Iterator, Mapping
from decimal import Decimal
from typing import Any, cast
from zoneinfo import ZoneInfo

import pytest
import structlog
from structlog.testing import capture_logs

from propbot.core.clock import ms_from_datetime
from propbot.notify import messages
from propbot.risk import RiskConfig, VenueProfile
from propbot.risk.loader import read_yaml_mapping
from propbot.signals import processor
from propbot.signals.processor import (
    Decision,
    DecisionContext,
    ProcessorConfig,
    SignalDataError,
    decide,
    process_one,
    run_processor,
)
from propbot.signals.schema import canonical_json, validate_alert
from propbot.store.db import (
    BudgetRow,
    NewSignal,
    OutboxKind,
    OutboxRow,
    Outcome,
    ReleaseReason,
    SignalRow,
    SignalStatus,
    StateKey,
    Store,
    StoreError,
    Tx,
)
from support import alerts
from support.alerts import BAR_TIME_MS, LONG, SHORT
from support.paths import CONFIG_FIXTURES

RISK_FILE = CONFIG_FIXTURES / "valid" / "risk.yaml"
VENUE_FILE = CONFIG_FIXTURES / "valid" / "venues" / "sample_2step.yaml"

# Index CFD: one lot earns 1.0 per point, sizes in hundredths of a lot, tick 0.01.
INDEX_CFD: dict[str, float | None] = {
    "tick_size": 0.01,
    "tick_value_per_lot": 0.01,
    "min_lot": 0.01,
    "lot_step": 0.01,
}
NO_CONTRACT: dict[str, float | None] = dict.fromkeys(INDEX_CFD)

MINUTE_MS = 60_000
HOUR_MS = 60 * MINUTE_MS
DAY_MS = 24 * HOUR_MS
# The reference entry bar: opens 2026-07-21 15:30 UTC (11:30 New York, 18:30 Istanbul).
ENTRY_CLOSE_MS = BAR_TIME_MS + MINUTE_MS
VALIDITY_MS = 60_000
VALID_UNTIL_MS = ENTRY_CLOSE_MS + VALIDITY_MS
# 1.5 s after the entry bar closed: a fresh alert, received 1 s after the close.
NOW_MS = ENTRY_CLOSE_MS + 1_500
PRAGUE = ZoneInfo("Europe/Prague")


def config(
    contract: Mapping[str, float | None] = INDEX_CFD,
    *,
    rules_verified_at: str | None = "2026-07-20",
    **risk_overrides: object,
) -> ProcessorConfig:
    """Fixture risk + venue (validated), renamed to the venue of the contract's examples."""
    risk = read_yaml_mapping(RISK_FILE)
    risk.update(risk_overrides)
    venue = read_yaml_mapping(VENUE_FILE)
    venue.update(
        {
            "venue": "ftmo",
            "symbol": "US500.cash",
            "rules_verified_at": rules_verified_at,
            "contract": dict(contract),
        }
    )
    return ProcessorConfig(RiskConfig.model_validate(risk), VenueProfile.model_validate(venue))


CONFIG = config()
UNKNOWN = config(NO_CONTRACT)  # what config/venues/*.yaml holds today: contract.* all null
TOO_WIDE = config(INDEX_CFD | {"min_lot": 25.0})  # a 12.5 point stop allows 20 lots at most


def new_signal(payload: Mapping[str, object], *, received_at_ms: int | None = None) -> NewSignal:
    """A webhook payload as the receiver stores it (received 1 s after the bar close)."""
    body = dict(payload)
    body.pop("token", None)
    alert = validate_alert(body)
    return NewSignal(
        received_at_ms=alert.bar_close_ms + 1_000 if received_at_ms is None else received_at_ms,
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


def stored(
    payload: Mapping[str, object],
    *,
    signal_id: int = 1,
    received_at_ms: int | None = None,
    outcome: Outcome | None = None,
) -> SignalRow:
    """A payload as a row read back from the store, without a store."""
    signal = new_signal(payload, received_at_ms=received_at_ms)
    return SignalRow(
        id=signal_id,
        status=SignalStatus.QUEUED if outcome is None else SignalStatus.DONE,
        outcome=outcome,
        processed_at_ms=None if outcome is None else signal.received_at_ms + 500,
        **dataclasses.asdict(signal),
    )


def at(now_ms: int = NOW_MS, **fields: Any) -> DecisionContext:
    return DecisionContext(now_ms=now_ms, **fields)


def text_of(decision: Decision) -> str:
    assert len(decision.messages) == 1
    assert decision.messages[0].kind is OutboxKind.SIGNAL
    return decision.messages[0].text


def assert_silent(decision: Decision, outcome: Outcome) -> None:
    assert decision == Decision(outcome)
    assert decision.messages == ()
    assert decision.alarms == ()
    assert decision.budget_risk is None


# --- store helpers ------------------------------------------------------------------------


@pytest.fixture
def store() -> Iterator[Store]:
    structlog.reset_defaults()
    with Store(":memory:") as opened:
        yield opened


def queue(store: Store, payload: Mapping[str, object], *, received_at_ms: int | None = None) -> int:
    with store.transaction() as tx:
        signal_id = tx.insert_signal(new_signal(payload, received_at_ms=received_at_ms))
    assert signal_id is not None
    return signal_id


def process(store: Store, cfg: ProcessorConfig = CONFIG, now_ms: int | None = None) -> SignalRow:
    """Process the head of the queue; by default 500 ms after it was received."""
    with store.transaction() as tx:
        head = tx.next_queued_signal()
        assert head is not None
        row = process_one(tx, cfg, head.received_at_ms + 500 if now_ms is None else now_ms)
    assert row is not None
    assert row.id == head.id
    assert row.status is SignalStatus.DONE
    return row


def handle(
    store: Store,
    payload: Mapping[str, object],
    cfg: ProcessorConfig = CONFIG,
    *,
    now_ms: int | None = None,
) -> SignalRow:
    queue(store, payload)
    return process(store, cfg, now_ms)


def outbox(store: Store) -> list[OutboxRow]:
    with store.transaction() as tx:
        return tx.messages_created_between(0, 2**62)


def texts(store: Store, signal_id: int) -> list[str]:
    with store.transaction() as tx:
        return [message.text for message in tx.messages_for_signal(signal_id)]


def budget(store: Store, signal_id: int) -> BudgetRow | None:
    with store.transaction() as tx:
        return tx.get_budget_entry(signal_id)


def set_state(store: Store, key: StateKey, value: str) -> None:
    with store.transaction() as tx:
        tx.set_state(key, value, NOW_MS)


def release(store: Store, signal_id: int, reason: ReleaseReason) -> None:
    with store.transaction() as tx:
        tx.set_budget_counted(signal_id=signal_id, counted=False, reason=reason, now_ms=NOW_MS)


def trail(
    side: str = LONG, *, minute: int, offset: float, fill: float = 5123.5
) -> dict[str, object]:
    """A ny_orb_fvg stop update `minute` minutes after the entry bar: stop = fill + offset."""
    # Decimal arithmetic, as TradingView prints prices: `fill + offset` in binary floating
    # point would hand the alert a stop such as 5111.110000000001.
    stop = float(Decimal(str(fill)) + Decimal(str(offset)))
    return alerts.ny_orb_fvg_stop_update(
        side, bar_time=BAR_TIME_MS + minute * MINUTE_MS, est_entry=fill, stop=stop
    )


ENTRY = stored(alerts.ny_orb_fvg_entry())
SHORT_ENTRY = stored(alerts.ny_orb_fvg_entry(SHORT))
FORWARDED_ENTRY = stored(alerts.ny_orb_fvg_entry(), outcome=Outcome.FORWARDED)
FORWARDED_SHORT_ENTRY = stored(alerts.ny_orb_fvg_entry(SHORT), outcome=Outcome.FORWARDED)
COUNTED = BudgetRow(
    signal_id=1,
    prop_day=dt.date(2026, 7, 21),
    risk_amount=250.0,
    counted=True,
    release_reason=None,
    updated_at_ms=NOW_MS,
)


def released(reason: ReleaseReason) -> BudgetRow:
    return dataclasses.replace(COUNTED, counted=False, release_reason=reason)


# ============================================================================================
# The decision as a pure function
# ============================================================================================

# --- heartbeat and malformed rows ---------------------------------------------------------


def test_heartbeat_is_recorded_without_a_message() -> None:
    signal = stored(alerts.heartbeat_payload())

    assert_silent(decide(signal, at(signal.received_at_ms + 500), CONFIG), Outcome.HEARTBEAT)
    assert_silent(
        decide(signal, at(signal.received_at_ms + DAY_MS, killed=True), CONFIG), Outcome.HEARTBEAT
    )


@pytest.mark.parametrize(
    "broken",
    [
        dataclasses.replace(ENTRY, action="bogus"),
        dataclasses.replace(ENTRY, stop=None),
        dataclasses.replace(ENTRY, est_entry=None),
        dataclasses.replace(stored(alerts.ny_orb_fvg_exit()), side=None),
        dataclasses.replace(stored(alerts.ny_orb_fvg_exit()), side="both"),
        dataclasses.replace(stored(alerts.ny_orb_fvg_stop_update()), stop=None),
    ],
)
def test_a_row_no_valid_alert_could_produce_is_an_error(broken: SignalRow) -> None:
    context = at(broken.received_at_ms + 500, reference=FORWARDED_ENTRY, reference_budget=COUNTED)

    with pytest.raises(SignalDataError):
        decide(broken, context, CONFIG)


def test_an_entry_whose_stop_equals_its_entry_is_an_error() -> None:
    with pytest.raises(ValueError, match="stop_points"):
        decide(dataclasses.replace(ENTRY, stop=ENTRY.est_entry), at(), CONFIG)


# --- entry: rule 6, forwarded -------------------------------------------------------------

ENTRY_TEXT = (
    "[SİNYAL] NY ORB FVG · FVG · LONG · CME_MINI:MES1!\n"
    "Referans giriş ≈ 5123.25 (TradingView bar kapanışı)\n"
    "Stop 5110.75 (−12.50 puan) · TP 5148.25 (+25.00 puan, 2.00R)\n"
    "FTMO US500.cash'te PİYASA emri; SL/TP'yi emirle BİRLİKTE gir: giriş anındaki fiyata göre "
    "stop −12.50 puan, TP +25.00 puan\n"
    "Önerilen boyut: 20.00 lot (≈ $102465 nominal, 1.0× hesap) · risk ≈ $250 (%0.25) · "
    "günlük bütçe $500/$1000 (bu sinyal dahil)\n"
    "Geçerlilik: 18:32:00 TR'ye kadar ve TradingView'da (CME_MINI:MES1!) fiyat "
    "5120.125–5126.375 aralığında — değilse ATLA\n"
    "TradingView'da stop/TP dolunca mesaj gelmez; pozisyonun hâlâ açıksa seans sonunda "
    "kendin kapat.\n"
    "Not: trailing stop güncellemeleri ayrı mesajla gelir.\n"
    "id 1 · bar 11:30 NY / 18:30 TR"
)
EXPIRED_TEXT = (
    "[İLETİLEMEDİ — ATLA] id 1 · NY ORB FVG LONG: sinyal Telegram'a zamanında ulaşmadı "
    "(geçerlilik 18:32:00 TR'de bitti). Bütçeden düşüldü."
)


def test_fresh_entry_is_forwarded_with_its_risk_deadline_and_expiry_text() -> None:
    decision = decide(ENTRY, at(budget_used=250.0), CONFIG)

    assert decision.outcome is Outcome.FORWARDED
    assert decision.budget_risk == 250.0
    assert decision.alarms == ()
    assert text_of(decision) == ENTRY_TEXT
    assert decision.messages[0].expires_at_ms == VALID_UNTIL_MS
    assert decision.messages[0].expired_text == EXPIRED_TEXT


def test_short_entry_is_forwarded_with_mirrored_levels() -> None:
    decision = decide(SHORT_ENTRY, at(), CONFIG)

    assert decision.outcome is Outcome.FORWARDED
    assert decision.budget_risk == 250.0
    assert text_of(decision).splitlines()[2] == (
        "Stop 5135.75 (+12.50 puan) · TP 5098.25 (−25.00 puan, 2.00R)"
    )


def test_the_counted_risk_is_the_risk_of_the_floored_lot_size() -> None:
    # 250 / 9.25 = 27.027 lots -> 27.02 lots -> 249.935 at risk, not the whole 250.
    decision = decide(stored(alerts.asian_sweep_entry()), at(), CONFIG)

    assert decision.outcome is Outcome.FORWARDED
    assert decision.budget_risk == pytest.approx(27.02 * 9.25)
    assert "Önerilen boyut: 27.02 lot" in text_of(decision)


# --- entry: rule 0, clock doubt -----------------------------------------------------------


def test_entry_that_arrived_before_its_bar_closed_is_skipped() -> None:
    early = stored(alerts.ny_orb_fvg_entry(), received_at_ms=ENTRY_CLOSE_MS - 5_000)

    decision = decide(early, at(ENTRY_CLOSE_MS - 4_500), CONFIG)

    assert decision.outcome is Outcome.SKIP_CLOCK
    assert decision.budget_risk is None
    assert decision.alarms == ()
    assert decision.messages[0].expires_at_ms is None
    assert text_of(decision) == (
        "[SİNYAL — ATLA] NY ORB FVG · FVG · LONG · CME_MINI:MES1!\n"
        "Sunucu saati TradingView ile uyuşmuyor (alert bar kapanışından 5 sn önce geldi); "
        "geçerlilik hesaplanamıyor. /saglik ile saati kontrol et.\n"
        "id 1 · bar 11:30 NY / 18:30 TR"
    )


def test_two_seconds_early_is_still_tolerated() -> None:
    at_tolerance = stored(alerts.ny_orb_fvg_entry(), received_at_ms=ENTRY_CLOSE_MS - 2_000)
    beyond = stored(alerts.ny_orb_fvg_entry(), received_at_ms=ENTRY_CLOSE_MS - 2_001)

    assert decide(at_tolerance, at(ENTRY_CLOSE_MS), CONFIG).outcome is Outcome.FORWARDED
    assert decide(beyond, at(ENTRY_CLOSE_MS), CONFIG).outcome is Outcome.SKIP_CLOCK


def test_a_slightly_early_entry_is_still_valid_until_bar_close_plus_validity() -> None:
    # Unlike follow-ups, an entry's validity never counts from its (earlier) arrival.
    early = stored(alerts.ny_orb_fvg_entry(), received_at_ms=ENTRY_CLOSE_MS - 1_500)

    decision = decide(early, at(VALID_UNTIL_MS), CONFIG)

    assert decision.outcome is Outcome.FORWARDED
    assert decision.messages[0].expires_at_ms == VALID_UNTIL_MS
    assert decide(early, at(VALID_UNTIL_MS + 1), CONFIG).outcome is Outcome.STALE


# --- entry: rules 1 and 2, kill and pause -------------------------------------------------


def test_killed_entry_is_suppressed_silently() -> None:
    assert_silent(decide(ENTRY, at(killed=True), CONFIG), Outcome.SUPPRESSED_KILLED)


def test_paused_entry_is_suppressed_silently() -> None:
    assert_silent(decide(ENTRY, at(paused=True), CONFIG), Outcome.SUPPRESSED_PAUSED)


def test_kill_wins_over_pause() -> None:
    assert_silent(decide(ENTRY, at(killed=True, paused=True), CONFIG), Outcome.SUPPRESSED_KILLED)


# --- entry: rule 3, stale -----------------------------------------------------------------


def test_entry_past_its_validity_is_stale() -> None:
    decision = decide(ENTRY, at(VALID_UNTIL_MS + 140_000), CONFIG)

    assert decision.outcome is Outcome.STALE
    assert decision.budget_risk is None
    assert decision.messages[0].expires_at_ms is None
    assert text_of(decision) == (
        "[BAYAT SİNYAL — ATLA] NY ORB FVG · FVG · LONG · CME_MINI:MES1!\n"
        "Sinyal bar kapanışından 3 dk 20 sn sonra işlenebildi (geçerlilik sınırı 1 dk). "
        "Fiyat artık referans değil: bu girişi ALMA.\n"
        "id 1 · bar 11:30 NY / 18:30 TR"
    )


def test_validity_ends_after_the_last_valid_millisecond() -> None:
    assert decide(ENTRY, at(VALID_UNTIL_MS), CONFIG).outcome is Outcome.FORWARDED
    assert decide(ENTRY, at(VALID_UNTIL_MS + 1), CONFIG).outcome is Outcome.STALE


def test_entry_validity_counts_from_the_computed_bar_close_not_from_arrival() -> None:
    # Received late but processed at once: the bar close decides, not the arrival time.
    late_arrival = stored(alerts.ny_orb_fvg_entry(), received_at_ms=VALID_UNTIL_MS + 1)

    assert decide(late_arrival, at(VALID_UNTIL_MS + 2), CONFIG).outcome is Outcome.STALE


def test_validity_follows_the_risk_config() -> None:
    longer = config(signal_validity_seconds=90)

    decision = decide(ENTRY, at(ENTRY_CLOSE_MS + 90_000), longer)

    assert decision.outcome is Outcome.FORWARDED
    assert decision.messages[0].expires_at_ms == ENTRY_CLOSE_MS + 90_000
    assert decide(ENTRY, at(ENTRY_CLOSE_MS + 90_001), longer).outcome is Outcome.STALE


# --- entry: rule 4, risk too wide ---------------------------------------------------------


def test_entry_whose_minimum_lot_exceeds_the_risk_budget_is_skipped() -> None:
    decision = decide(ENTRY, at(), TOO_WIDE)

    assert decision.outcome is Outcome.SKIP_RISK_TOO_WIDE
    assert decision.budget_risk is None
    assert decision.messages[0].expires_at_ms is None
    assert text_of(decision) == (
        "[SİNYAL — ATLA] NY ORB FVG · FVG · LONG · CME_MINI:MES1!\n"
        "Stop çok geniş (12.50 puan): en küçük lot (25.00) bile ≈ $312.50 risk taşır; "
        "işlem başı sınır $250. Bu girişi ALMA.\n"
        "id 1 · bar 11:30 NY / 18:30 TR"
    )


# --- entry: rule 5, daily budget ----------------------------------------------------------


def test_entry_that_fills_the_budget_exactly_is_forwarded() -> None:
    decision = decide(ENTRY, at(budget_used=750.0), CONFIG)

    assert decision.outcome is Outcome.FORWARDED
    assert "günlük bütçe $1000/$1000 (bu sinyal dahil)" in text_of(decision)


def test_entry_one_cent_above_the_budget_is_skipped() -> None:
    decision = decide(ENTRY, at(budget_used=750.01), CONFIG)

    assert decision.outcome is Outcome.SKIP_BUDGET
    assert decision.budget_risk is None
    assert decision.messages[0].expires_at_ms is None
    assert text_of(decision) == (
        "[SİNYAL — ATLA] NY ORB FVG · FVG · LONG · CME_MINI:MES1!\n"
        "Günlük risk bütçesi yetmiyor: kullanılan $750.01 + bu sinyal $250 > sınır $1000. "
        "Bu girişi ALMA.\n"
        "Bugün almadığın bir giriş bütçede sayılıyorsa serbest bırak: /atladim <id>\n"
        "id 1 · bar 11:30 NY / 18:30 TR"
    )


def test_float_noise_in_the_ledger_sum_does_not_skip_an_entry_at_the_cap() -> None:
    # 0.1 + 0.2 style noise: three counted risks that sum to 750 only up to the last bit.
    noisy = 750.0 + 5e-10

    assert decide(ENTRY, at(budget_used=noisy), CONFIG).outcome is Outcome.FORWARDED


def test_the_budget_cap_is_bounded_by_half_the_firm_daily_loss_limit() -> None:
    # daily_cap_pct 3.0 would be $3000; the venue allows 0.5 x 5 % = $2500.
    wide = config(daily_cap_pct=3.0)

    assert decide(ENTRY, at(budget_used=2250.0), wide).outcome is Outcome.FORWARDED
    assert decide(ENTRY, at(budget_used=2250.01), wide).outcome is Outcome.SKIP_BUDGET


# --- entry: contract unknown (the normal path while contract.* is null) ---------------------


def test_unknown_contract_counts_the_whole_risk_budget_and_gives_the_formula() -> None:
    decision = decide(ENTRY, at(budget_used=250.0), UNKNOWN)

    assert decision.outcome is Outcome.FORWARDED
    assert decision.budget_risk == 250.0
    assert decision.messages[0].expires_at_ms == VALID_UNTIL_MS
    assert decision.messages[0].expired_text == EXPIRED_TEXT
    assert text_of(decision).splitlines()[4] == (
        "Önerilen boyut: hesaplanamadı (venue profilinde contract.* boş) · en fazla risk $250 "
        "(%0.25) → lot = 250 ÷ (12.50 × 1 lotun 1 puanlık değeri), AŞAĞI yuvarla · "
        "günlük bütçe $500/$1000 (bu sinyal dahil)"
    )
    assert [line for i, line in enumerate(text_of(decision).splitlines()) if i != 4] == [
        line for i, line in enumerate(ENTRY_TEXT.splitlines()) if i != 4
    ]


@pytest.mark.parametrize("missing", sorted(INDEX_CFD))
def test_one_missing_contract_field_is_enough_for_the_unknown_path(missing: str) -> None:
    partial = config(INDEX_CFD | {missing: None})

    decision = decide(stored(alerts.asian_sweep_entry()), at(), partial)

    assert decision.outcome is Outcome.FORWARDED
    assert decision.budget_risk == 250.0  # not the 249.935 of a floored lot size
    assert "Önerilen boyut: hesaplanamadı" in text_of(decision)


def test_unknown_contract_never_reports_a_stop_as_too_wide() -> None:
    # Nothing is known about the lot: the stop distance cannot be judged, the user sizes it.
    wide_stop = stored(alerts.mr_bb_kc_rsi2_entry(target_one_tick=True))

    assert decide(wide_stop, at(), UNKNOWN).outcome is Outcome.FORWARDED


def test_unknown_contract_budget_boundary() -> None:
    assert decide(ENTRY, at(budget_used=750.0), UNKNOWN).outcome is Outcome.FORWARDED
    assert decide(ENTRY, at(budget_used=750.01), UNKNOWN).outcome is Outcome.SKIP_BUDGET


# --- entry: precedence --------------------------------------------------------------------

EARLY_ENTRY = stored(alerts.ny_orb_fvg_entry(), received_at_ms=ENTRY_CLOSE_MS - 3_000)
LATE_MS = VALID_UNTIL_MS + 1


@pytest.mark.parametrize(
    ("signal", "context", "cfg", "expected"),
    [
        (EARLY_ENTRY, at(killed=True), CONFIG, Outcome.SKIP_CLOCK),
        (EARLY_ENTRY, at(paused=True), CONFIG, Outcome.SKIP_CLOCK),
        (EARLY_ENTRY, at(budget_used=1000.0), TOO_WIDE, Outcome.SKIP_CLOCK),
        (ENTRY, at(LATE_MS, killed=True), CONFIG, Outcome.SUPPRESSED_KILLED),
        (ENTRY, at(LATE_MS, paused=True), CONFIG, Outcome.SUPPRESSED_PAUSED),
        (ENTRY, at(killed=True), TOO_WIDE, Outcome.SUPPRESSED_KILLED),
        (ENTRY, at(paused=True), TOO_WIDE, Outcome.SUPPRESSED_PAUSED),
        (ENTRY, at(paused=True, budget_used=1000.0), CONFIG, Outcome.SUPPRESSED_PAUSED),
        (ENTRY, at(LATE_MS), TOO_WIDE, Outcome.STALE),
        (ENTRY, at(LATE_MS, budget_used=1000.0), CONFIG, Outcome.STALE),
        (ENTRY, at(budget_used=1000.0), TOO_WIDE, Outcome.SKIP_RISK_TOO_WIDE),
    ],
    ids=[
        "early+killed",
        "early+paused",
        "early+too_wide+budget",
        "killed+late",
        "paused+late",
        "killed+too_wide",
        "paused+too_wide",
        "paused+budget",
        "late+too_wide",
        "late+budget",
        "too_wide+budget",
    ],
)
def test_entry_rules_apply_in_order(
    signal: SignalRow, context: DecisionContext, cfg: ProcessorConfig, expected: Outcome
) -> None:
    assert decide(signal, context, cfg).outcome is expected


# --- entry: unverified venue rules --------------------------------------------------------


@pytest.mark.parametrize("verified", [None, "2026-06-20", "2026-07-23"])
def test_unverified_rules_add_the_banner_and_a_daily_alarm(verified: str | None) -> None:
    cfg = config(rules_verified_at=verified)

    decision = decide(ENTRY, at(budget_used=250.0), cfg)

    assert decision.outcome is Outcome.FORWARDED
    assert decision.budget_risk == 250.0
    assert text_of(decision) == (
        "[KURALLAR DOĞRULANMADI — boyut ve bütçe doğrulanmamış kurallara göre]\n" + ENTRY_TEXT
    )
    (alarm,) = decision.alarms
    assert (alarm.code, alarm.throttle_ms) == ("rules_stale", DAY_MS)
    assert alarm.text.startswith("[ALARM] FTMO kuralları güncel doğrulanmamış: ")
    assert "rules_verified_at tarihini güncelle" in alarm.text


@pytest.mark.parametrize("verified", ["2026-06-21", "2026-07-21", "2026-07-22"])
def test_fresh_rules_add_nothing(verified: str) -> None:
    decision = decide(ENTRY, at(budget_used=250.0), config(rules_verified_at=verified))

    assert text_of(decision) == ENTRY_TEXT
    assert decision.alarms == ()


def test_skipped_entries_carry_no_rules_alarm() -> None:
    cfg = config(rules_verified_at=None)

    assert decide(ENTRY, at(LATE_MS), cfg).alarms == ()
    assert decide(ENTRY, at(budget_used=1000.0), cfg).alarms == ()


# --- follow-ups: which trade they belong to -------------------------------------------------

STOP_UPDATE = stored(alerts.ny_orb_fvg_stop_update(), signal_id=2)
EXIT = stored(alerts.ny_orb_fvg_exit(), signal_id=3)
FOLLOW_UPS = [STOP_UPDATE, EXIT, stored(alerts.ict_silver_bullet_cancel(), signal_id=4)]
FOLLOW_UP_IDS = ["stop_update", "exit", "cancel"]


def follow_up(signal: SignalRow, **fields: Any) -> DecisionContext:
    """Context of a follow-up processed 500 ms after it arrived, after a forwarded long entry."""
    values: dict[str, Any] = {
        "now_ms": signal.received_at_ms + 500,
        "reference": FORWARDED_ENTRY,
        "reference_budget": COUNTED,
    }
    values.update(fields)
    return DecisionContext(**values)


NOT_FORWARDED = [None, *(outcome for outcome in Outcome if outcome is not Outcome.FORWARDED)]


@pytest.mark.parametrize("signal", FOLLOW_UPS, ids=FOLLOW_UP_IDS)
@pytest.mark.parametrize("outcome", NOT_FORWARDED)
def test_follow_up_of_an_entry_that_was_not_forwarded_is_dropped(
    signal: SignalRow, outcome: Outcome | None
) -> None:
    reference = dataclasses.replace(FORWARDED_ENTRY, outcome=outcome)

    decision = decide(signal, follow_up(signal, reference=reference, reference_budget=None), CONFIG)

    assert_silent(decision, Outcome.NO_ACTIVE_TRADE)


@pytest.mark.parametrize("signal", FOLLOW_UPS, ids=FOLLOW_UP_IDS)
def test_follow_up_after_a_forwarded_exit_is_dropped(signal: SignalRow) -> None:
    decision = decide(signal, follow_up(signal, exit_after_reference=True), CONFIG)

    assert_silent(decision, Outcome.NO_ACTIVE_TRADE)


@pytest.mark.parametrize("signal", FOLLOW_UPS, ids=FOLLOW_UP_IDS)
def test_follow_up_of_an_entry_the_user_skipped_is_dropped(signal: SignalRow) -> None:
    context = follow_up(signal, reference_budget=released(ReleaseReason.SKIPPED))

    assert_silent(decide(signal, context, CONFIG), Outcome.NO_ACTIVE_TRADE)


@pytest.mark.parametrize("signal", FOLLOW_UPS, ids=FOLLOW_UP_IDS)
def test_follow_up_of_an_undelivered_entry_is_forwarded_with_a_note(signal: SignalRow) -> None:
    reference = dataclasses.replace(FORWARDED_ENTRY, side=signal.side)
    context = follow_up(
        signal, reference=reference, reference_budget=released(ReleaseReason.UNDELIVERED)
    )

    decision = decide(signal, context, CONFIG)

    assert decision.outcome is Outcome.FORWARDED
    assert decision.budget_risk is None
    assert text_of(decision).splitlines()[-2] == (
        "(giriş mesajı sana ulaşmamış olabilir; pozisyon açmadıysan yok say)"
    )
    assert f"id {signal.id} (giriş id 1) · " in text_of(decision).splitlines()[-1]
    assert "/atladim" not in text_of(decision)  # nothing is counted any more


@pytest.mark.parametrize("signal", FOLLOW_UPS, ids=FOLLOW_UP_IDS)
def test_follow_up_without_any_entry_is_forwarded_with_the_orphan_note(signal: SignalRow) -> None:
    decision = decide(signal, at(signal.received_at_ms + 500), CONFIG)

    assert decision.outcome is Outcome.FORWARDED
    assert decision.messages[0].expires_at_ms is None
    assert decision.messages[0].expired_text is None
    assert text_of(decision).splitlines()[-2].startswith("(bu bot giriş sinyalini görmedi)")
    assert text_of(decision).splitlines()[-1].startswith(f"id {signal.id} · bar ")
    assert "/atladim" not in text_of(decision)


@pytest.mark.parametrize("signal", FOLLOW_UPS, ids=FOLLOW_UP_IDS)
def test_follow_ups_ignore_pause_and_kill(signal: SignalRow) -> None:
    reference = dataclasses.replace(FORWARDED_ENTRY, side=signal.side)
    context = follow_up(signal, reference=reference, killed=True, paused=True)

    assert decide(signal, context, CONFIG).outcome is Outcome.FORWARDED


def test_exit_is_forwarded() -> None:
    decision = decide(EXIT, follow_up(EXIT), CONFIG)

    assert decision.outcome is Outcome.FORWARDED
    assert decision.messages[0].expires_at_ms is None
    assert text_of(decision) == (
        "[ÇIKIŞ] NY ORB FVG · LONG · CME_MINI:MES1!\n"
        "Strateji pozisyonu kapattı (seans sonu / günlük zarar kesicisi / test aralığı sonu).\n"
        "FTMO US500.cash'te pozisyon açıksa PİYASADAN KAPAT.\n"
        "id 3 (giriş id 1) · bar 15:59 NY / 22:59 TR"
    )


def test_cancel_is_forwarded_with_the_release_command_of_its_entry() -> None:
    entry = stored(alerts.ict_silver_bullet_entry(SHORT), signal_id=126, outcome=Outcome.FORWARDED)
    cancel = stored(alerts.ict_silver_bullet_cancel(SHORT), signal_id=127)

    decision = decide(cancel, follow_up(cancel, reference=entry), CONFIG)

    assert decision.outcome is Outcome.FORWARDED
    assert decision.budget_risk is None  # the ledger is released by the user, never by a guess
    assert text_of(decision) == (
        "[İPTAL] ICT Silver Bullet · SHORT · CME_MINI:MES1!\n"
        "Bekleyen limit emir geri çekildi (seviye 5123.25).\n"
        "FTMO US500.cash'te emir dolmadıysa İPTAL ET ve bütçeyi serbest bırak: /atladim 126\n"
        "Emir dolduysa: TradingView'da bu pozisyon YOK, başka mesaj gelmeyecek — stop/TP ile "
        "yönet ve seans sonunda kendin kapat.\n"
        "id 127 (giriş id 126) · bar 11:40 NY / 18:40 TR"
    )


@pytest.mark.parametrize(
    "signal",
    [stored(alerts.ny_orb_fvg_exit(SHORT), signal_id=3), FOLLOW_UPS[2]],
    ids=["exit", "cancel"],
)
def test_exit_or_cancel_for_the_other_side_is_forwarded_but_not_tied_to_the_entry(
    signal: SignalRow,
) -> None:
    other_side = FORWARDED_SHORT_ENTRY if signal.side == LONG else FORWARDED_ENTRY

    decision = decide(signal, follow_up(signal, reference=other_side), CONFIG)

    assert decision.outcome is Outcome.FORWARDED
    assert decision.alarms == ()
    assert text_of(decision).splitlines()[-2] == "(bu bot giriş sinyalini görmedi)"
    assert "giriş id" not in text_of(decision)
    assert "/atladim" not in text_of(decision)  # releasing the other trade would be wrong


# --- follow-ups: late ---------------------------------------------------------------------


def test_follow_up_within_the_validity_has_no_prefix() -> None:
    context = follow_up(EXIT, now_ms=EXIT.bar_close_ms + VALIDITY_MS)

    assert text_of(decide(EXIT, context, CONFIG)).startswith("[ÇIKIŞ] ")


def test_late_follow_up_is_still_forwarded_with_its_age() -> None:
    context = follow_up(EXIT, now_ms=EXIT.bar_close_ms + 200_000)

    decision = decide(EXIT, context, CONFIG)

    assert decision.outcome is Outcome.FORWARDED
    assert text_of(decision).splitlines()[0] == (
        "[GEÇ GELDİ +3 dk 20 sn] [ÇIKIŞ] NY ORB FVG · LONG · CME_MINI:MES1!"
    )


def test_follow_up_on_a_session_shortened_bar_ages_from_its_arrival() -> None:
    # A 5-minute bar cut after 1 minute by the session end: the alert arrives 4 minutes
    # before the computed bar close.
    payload = alerts.mtf_trend_cont_exit()
    close_ms = new_signal(payload).bar_close_ms
    arrived_ms = close_ms - 4 * MINUTE_MS
    shortened = stored(payload, signal_id=9, received_at_ms=arrived_ms)

    on_time = decide(shortened, at(arrived_ms + VALIDITY_MS), CONFIG)
    late = decide(shortened, at(arrived_ms + VALIDITY_MS + 1), CONFIG)

    assert text_of(on_time).startswith("[ÇIKIŞ] ")
    assert text_of(late).startswith("[GEÇ GELDİ +1 dk] [ÇIKIŞ] ")


def test_late_stop_update_and_warning_are_prefixed_too() -> None:
    update = stored(trail(minute=18, offset=-3.5), signal_id=2)
    widening = stored(trail(minute=18, offset=-13.25), signal_id=2)
    late_ms = update.bar_close_ms + 200_000

    assert text_of(decide(update, follow_up(update, now_ms=late_ms), CONFIG)).startswith(
        "[GEÇ GELDİ +3 dk 20 sn] [STOP GÜNCELLE] "
    )
    assert text_of(decide(widening, follow_up(widening, now_ms=late_ms), CONFIG)).startswith(
        "[GEÇ GELDİ +3 dk 20 sn] [UYARI] "
    )


# --- stop update: the trader's frame --------------------------------------------------------


def test_stop_update_is_forwarded_as_offsets_from_the_traders_own_entry() -> None:
    decision = decide(STOP_UPDATE, follow_up(STOP_UPDATE), CONFIG)

    assert decision.outcome is Outcome.FORWARDED
    assert decision.messages[0].expires_at_ms is None
    assert text_of(decision) == (
        "[STOP GÜNCELLE] NY ORB FVG · LONG · CME_MINI:MES1!\n"
        "Stop: kendi giriş fiyatına göre −12.50 → −3.50 puan (TradingView seviyesi 5120.00)\n"
        "FTMO US500.cash'te uygula: stop = kendi giriş fiyatın −3.50 puan\n"
        "id 2 (giriş id 1) · bar 11:48 NY / 18:48 TR"
    )


def test_worked_case_a_higher_tradingview_stop_can_still_widen_the_traders_stop() -> None:
    # Entry alert: close 5123.25, stop 5110.75 -> the trader holds a stop 12.50 below the
    # own fill. TradingView filled at 5125.25 and trails to 5112.00: a HIGHER price level,
    # but 13.25 below TradingView's fill. Applied to the trader's fill that is 0.75 wider.
    update = stored(alerts.ny_orb_fvg_stop_update(est_entry=5125.25, stop=5112.0), signal_id=2)
    assert update.stop is not None
    assert FORWARDED_ENTRY.stop is not None
    assert update.stop > FORWARDED_ENTRY.stop

    decision = decide(update, follow_up(update), CONFIG)

    assert decision.outcome is Outcome.REJECTED_WIDEN
    assert decision.alarms == ()
    assert decision.messages[0].expires_at_ms is None
    assert text_of(decision) == (
        "[UYARI] NY ORB FVG · LONG · CME_MINI:MES1!\n"
        "Strateji stopu GENİŞLETMEK istiyor (kendi giriş fiyatına göre −12.50 → −13.25 puan; "
        "TradingView seviyesi 5112.00). UYGULAMA: açık pozisyonun stopu genişletilmez.\n"
        "FTMO US500.cash'te stopunu KORU: stop = kendi giriş fiyatın −12.50 puan\n"
        "id 2 (giriş id 1) · bar 11:48 NY / 18:48 TR"
    )


def test_a_lower_tradingview_stop_can_still_tighten_the_traders_stop() -> None:
    # The mirror image: TradingView filled BELOW the signal close, so a stop below the
    # entry alert's stop level is still closer to the fill.
    update = stored(alerts.ny_orb_fvg_stop_update(est_entry=5120.25, stop=5110.25), signal_id=2)

    decision = decide(update, follow_up(update), CONFIG)

    assert decision.outcome is Outcome.FORWARDED
    assert "−12.50 → −10.00 puan (TradingView seviyesi 5110.25)" in text_of(decision)


@pytest.mark.parametrize(
    ("side", "offset", "expected"),
    [
        (LONG, -12.5, Outcome.FILTERED_MIN_STEP),  # equal
        (LONG, -12.47, Outcome.FILTERED_MIN_STEP),  # 3 ticks tighter
        (LONG, -12.46, Outcome.FORWARDED),  # exactly trail_min_step_ticks = 4 ticks
        (LONG, -12.51, Outcome.REJECTED_WIDEN),  # one tick wider
        (LONG, 2.0, Outcome.FORWARDED),  # beyond the entry: profit locked
        (SHORT, 12.5, Outcome.FILTERED_MIN_STEP),
        (SHORT, 12.47, Outcome.FILTERED_MIN_STEP),
        (SHORT, 12.46, Outcome.FORWARDED),
        (SHORT, 12.51, Outcome.REJECTED_WIDEN),
        (SHORT, -2.0, Outcome.FORWARDED),
    ],
)
def test_stop_update_steps_with_a_known_tick_size(
    side: str, offset: float, expected: Outcome
) -> None:
    reference = FORWARDED_ENTRY if side == LONG else FORWARDED_SHORT_ENTRY
    # A fill off the 0.25 grid: binary floats cannot represent these differences exactly.
    update = stored(trail(side, minute=18, offset=offset, fill=5123.57), signal_id=2)

    decision = decide(update, follow_up(update, reference=reference), CONFIG)

    assert decision.outcome is expected
    if expected is Outcome.FILTERED_MIN_STEP:
        assert_silent(decision, expected)


@pytest.mark.parametrize(
    ("side", "offset", "expected"),
    [
        (LONG, -12.5, Outcome.FILTERED_MIN_STEP),  # no improvement is never forwarded
        (LONG, -12.49, Outcome.FORWARDED),  # any improvement is, while the tick is unknown
        (LONG, -12.51, Outcome.REJECTED_WIDEN),
        (SHORT, 12.5, Outcome.FILTERED_MIN_STEP),
        (SHORT, 12.49, Outcome.FORWARDED),
        (SHORT, 12.51, Outcome.REJECTED_WIDEN),
    ],
)
def test_stop_update_steps_without_a_tick_size(side: str, offset: float, expected: Outcome) -> None:
    reference = FORWARDED_ENTRY if side == LONG else FORWARDED_SHORT_ENTRY
    update = stored(trail(side, minute=18, offset=offset, fill=5123.57), signal_id=2)

    assert decide(update, follow_up(update, reference=reference), UNKNOWN).outcome is expected


def test_min_step_follows_the_risk_config_and_the_tick_size() -> None:
    quarter_tick = config(INDEX_CFD | {"tick_size": 0.25, "tick_value_per_lot": 0.25})
    almost = stored(trail(minute=18, offset=-11.75), signal_id=2)  # 3 ticks of 0.25
    enough = stored(trail(minute=18, offset=-11.5), signal_id=2)  # 4 ticks

    assert decide(almost, follow_up(almost), quarter_tick).outcome is Outcome.FILTERED_MIN_STEP
    assert decide(enough, follow_up(enough), quarter_tick).outcome is Outcome.FORWARDED
    one_tick = config(
        INDEX_CFD | {"tick_size": 0.25, "tick_value_per_lot": 0.25}, trail_min_step_ticks=1
    )
    assert decide(almost, follow_up(almost), one_tick).outcome is Outcome.FORWARDED


def test_short_widening_warning_keeps_the_positive_offset() -> None:
    update = stored(trail(SHORT, minute=18, offset=13.25), signal_id=2)

    decision = decide(update, follow_up(update, reference=FORWARDED_SHORT_ENTRY), CONFIG)

    assert decision.outcome is Outcome.REJECTED_WIDEN
    assert text_of(decision).splitlines()[2] == (
        "FTMO US500.cash'te stopunu KORU: stop = kendi giriş fiyatın +12.50 puan"
    )


def test_stop_update_is_compared_with_the_last_one_that_was_sent() -> None:
    last = stored(trail(minute=18, offset=-3.5), signal_id=2, outcome=Outcome.FORWARDED)
    tighter = stored(trail(minute=20, offset=-2.0, fill=5125.0), signal_id=3)
    wider = stored(trail(minute=20, offset=-5.0, fill=5125.0), signal_id=3)
    same = stored(trail(minute=20, offset=-3.5, fill=5125.0), signal_id=3)

    forwarded = decide(tighter, follow_up(tighter, last_stop_update=last), CONFIG)
    rejected = decide(wider, follow_up(wider, last_stop_update=last), CONFIG)
    filtered = decide(same, follow_up(same, last_stop_update=last), CONFIG)

    assert forwarded.outcome is Outcome.FORWARDED
    assert "−3.50 → −2.00 puan (TradingView seviyesi 5123.00)" in text_of(forwarded)
    assert rejected.outcome is Outcome.REJECTED_WIDEN
    assert "stop = kendi giriş fiyatın −3.50 puan" in text_of(rejected)
    assert_silent(filtered, Outcome.FILTERED_MIN_STEP)


def test_stop_update_older_than_one_already_sent_is_superseded() -> None:
    newer = stored(trail(minute=18, offset=-3.5), signal_id=2, outcome=Outcome.FORWARDED)
    older = stored(trail(minute=10, offset=-1.0), signal_id=3)

    decision = decide(older, follow_up(older, last_stop_update=newer), CONFIG)

    assert_silent(decision, Outcome.SUPERSEDED)


def test_a_newer_update_of_another_trade_supersedes_whatever_its_side() -> None:
    # The entry of a newer short trade is not this update's reference (its bar is later),
    # but its stop update was already sent: the long trade is over.
    newer_short = stored(
        trail(SHORT, minute=40, offset=3.5), signal_id=5, outcome=Outcome.FORWARDED
    )
    late_long = stored(trail(minute=18, offset=-3.5), signal_id=6)

    decision = decide(late_long, follow_up(late_long, last_stop_update=newer_short), CONFIG)

    assert_silent(decision, Outcome.SUPERSEDED)


def test_an_older_update_of_the_other_side_is_not_the_stop_to_compare_with() -> None:
    # A short update that was forwarded as an orphan before this long entry was stored.
    stray_short = stored(trail(SHORT, minute=5, offset=3.5), signal_id=5, outcome=Outcome.FORWARDED)
    update = stored(trail(minute=18, offset=-3.5), signal_id=6)

    decision = decide(update, follow_up(update, last_stop_update=stray_short), CONFIG)

    assert decision.outcome is Outcome.FORWARDED
    assert "−12.50 → −3.50 puan" in text_of(decision)  # compared with the entry's own stop


def test_stop_update_for_the_other_side_is_dropped_with_an_alarm() -> None:
    update = stored(alerts.ny_orb_fvg_stop_update(SHORT), signal_id=2)

    decision = decide(update, follow_up(update), CONFIG)

    assert decision.outcome is Outcome.NO_ACTIVE_TRADE
    assert decision.messages == ()
    (alarm,) = decision.alarms
    assert (alarm.code, alarm.throttle_ms) == ("side_mismatch", 10 * MINUTE_MS)
    assert alarm.text == messages.side_mismatch_alarm(update, FORWARDED_ENTRY)
    assert "SHORT yönlü stop güncellemesi geldi (id 2)" in alarm.text


def test_dead_trade_wins_over_the_side_check() -> None:
    update = stored(alerts.ny_orb_fvg_stop_update(SHORT), signal_id=2)

    decision = decide(update, follow_up(update, exit_after_reference=True), CONFIG)

    assert_silent(decision, Outcome.NO_ACTIVE_TRADE)


@pytest.mark.parametrize("side", [LONG, SHORT])
def test_breakeven_move_without_a_target_is_forwarded(side: str) -> None:
    # MTF after TP1: stop == est_entry, tp null.
    entry = stored(alerts.mtf_trend_cont_entry(side), outcome=Outcome.FORWARDED)
    update = stored(alerts.mtf_trend_cont_stop_update(side, tp1_filled=True), signal_id=2)
    assert update.stop == update.est_entry
    assert update.tp is None

    decision = decide(update, follow_up(update, reference=entry), CONFIG)

    current = "−10.00" if side == LONG else "+10.00"
    assert decision.outcome is Outcome.FORWARDED
    assert text_of(decision).splitlines()[1:3] == [
        f"Stop: kendi giriş fiyatına göre {current} → +0.00 puan (TradingView seviyesi 5140.50)",
        "FTMO US500.cash'te uygula: stop = kendi giriş fiyatın +0.00 puan (başabaş)",
    ]


def test_stop_beyond_the_entry_is_marked_as_locked_profit() -> None:
    update = stored(trail(minute=18, offset=2.0), signal_id=2)

    decision = decide(update, follow_up(update), CONFIG)

    assert text_of(decision).splitlines()[2] == (
        "FTMO US500.cash'te uygula: stop = kendi giriş fiyatın +2.00 puan (kâr kilitli)"
    )


# --- stop update: no entry seen -------------------------------------------------------------


def test_orphan_stop_update_without_history_leaves_the_comparison_to_the_user() -> None:
    decision = decide(STOP_UPDATE, at(STOP_UPDATE.received_at_ms + 500), CONFIG)

    assert decision.outcome is Outcome.FORWARDED
    assert text_of(decision) == (
        "[STOP GÜNCELLE] NY ORB FVG · LONG · CME_MINI:MES1!\n"
        "Stop: kendi giriş fiyatına göre −3.50 puan (TradingView seviyesi 5120.00)\n"
        "FTMO US500.cash'te uygula: stop = kendi giriş fiyatın −3.50 puan\n"
        "(bu bot giriş sinyalini görmedi) Yalnızca mevcut stop'undan DAHA SIKIysa uygula.\n"
        "id 2 · bar 11:48 NY / 18:48 TR"
    )


def test_orphan_stop_update_is_compared_with_an_earlier_one_of_the_same_side() -> None:
    last = stored(trail(minute=18, offset=-3.5), signal_id=2, outcome=Outcome.FORWARDED)
    tighter = stored(trail(minute=20, offset=-2.0), signal_id=3)
    wider = stored(trail(minute=20, offset=-5.0), signal_id=3)
    older = stored(trail(minute=10, offset=-1.0), signal_id=3)

    def orphan(signal: SignalRow) -> Decision:
        return decide(signal, at(signal.received_at_ms + 500, last_stop_update=last), CONFIG)

    assert orphan(tighter).outcome is Outcome.FORWARDED
    assert text_of(orphan(tighter)).splitlines()[1:] == [
        "Stop: kendi giriş fiyatına göre −3.50 → −2.00 puan (TradingView seviyesi 5121.50)",
        "FTMO US500.cash'te uygula: stop = kendi giriş fiyatın −2.00 puan",
        "(bu bot giriş sinyalini görmedi)",
        "id 3 · bar 11:50 NY / 18:50 TR",
    ]
    assert orphan(wider).outcome is Outcome.REJECTED_WIDEN
    assert text_of(orphan(wider)).splitlines()[-2] == "(bu bot giriş sinyalini görmedi)"
    assert_silent(orphan(older), Outcome.SUPERSEDED)


def test_orphan_stop_update_ignores_an_earlier_one_of_the_other_side() -> None:
    other = stored(trail(SHORT, minute=25, offset=3.5), signal_id=2, outcome=Outcome.FORWARDED)
    update = stored(trail(LONG, minute=20, offset=-2.0), signal_id=3)

    decision = decide(update, at(update.received_at_ms + 500, last_stop_update=other), CONFIG)

    assert decision.outcome is Outcome.FORWARDED
    assert "Yalnızca mevcut stop'undan DAHA SIKIysa uygula." in text_of(decision)


def test_offsets_are_exact_decimals() -> None:
    update = stored(trail(minute=18, offset=-12.46, fill=5123.57), signal_id=2)

    # 5111.11 - 5123.57 is -12.460000000000036 in binary floating point.
    assert processor._stop_offset(update) == Decimal("-12.46")


# ============================================================================================
# process_one: context from the store, everything written in one transaction
# ============================================================================================


def test_an_empty_queue_yields_none(store: Store) -> None:
    with store.transaction() as tx:
        assert process_one(tx, CONFIG, NOW_MS) is None

    assert outbox(store) == []


def test_forwarded_entry_writes_outcome_ledger_and_outbox_together(store: Store) -> None:
    signal_id = queue(store, alerts.ny_orb_fvg_entry())

    row = process(store, CONFIG, NOW_MS)

    assert (row.id, row.outcome, row.processed_at_ms) == (signal_id, Outcome.FORWARDED, NOW_MS)
    assert budget(store, signal_id) == BudgetRow(
        signal_id=signal_id,
        prop_day=dt.date(2026, 7, 21),
        risk_amount=250.0,
        counted=True,
        release_reason=None,
        updated_at_ms=NOW_MS,
    )
    (message,) = outbox(store)
    assert message.kind is OutboxKind.SIGNAL
    assert message.signal_id == signal_id
    assert message.created_at_ms == NOW_MS
    assert message.text == ENTRY_TEXT.replace("$500/$1000", "$250/$1000")
    assert message.expires_at_ms == VALID_UNTIL_MS
    assert message.expired_text == EXPIRED_TEXT


def test_signals_are_processed_in_arrival_order(store: Store) -> None:
    first = queue(store, alerts.asian_sweep_entry())
    second = queue(store, alerts.ny_orb_fvg_entry())

    assert [process(store).id, process(store).id] == [first, second]
    with store.transaction() as tx:
        assert process_one(tx, CONFIG, NOW_MS) is None


@pytest.mark.parametrize(
    ("state", "expected"),
    [
        ({StateKey.KILLED: "1"}, Outcome.SUPPRESSED_KILLED),
        ({StateKey.PAUSED: "1"}, Outcome.SUPPRESSED_PAUSED),
        ({StateKey.KILLED: "1", StateKey.PAUSED: "1"}, Outcome.SUPPRESSED_KILLED),
        ({StateKey.KILLED: "0", StateKey.PAUSED: "0"}, Outcome.FORWARDED),
        # An unreadable switch fails closed: no new entry.
        ({StateKey.KILLED: "yes"}, Outcome.SUPPRESSED_KILLED),
        ({StateKey.PAUSED: ""}, Outcome.SUPPRESSED_PAUSED),
    ],
)
def test_entry_reads_kill_and_pause_from_the_store(
    store: Store, state: dict[StateKey, str], expected: Outcome
) -> None:
    for key, value in state.items():
        set_state(store, key, value)

    row = handle(store, alerts.ny_orb_fvg_entry())

    assert row.outcome is expected
    if expected is not Outcome.FORWARDED:
        assert outbox(store) == []
        assert budget(store, row.id) is None


@pytest.mark.parametrize(
    ("cfg", "now_ms", "received_at_ms", "expected", "tag"),
    [
        (CONFIG, LATE_MS, None, Outcome.STALE, "[BAYAT SİNYAL — ATLA]"),
        (CONFIG, ENTRY_CLOSE_MS, ENTRY_CLOSE_MS - 2_001, Outcome.SKIP_CLOCK, "[SİNYAL — ATLA]"),
        (TOO_WIDE, NOW_MS, None, Outcome.SKIP_RISK_TOO_WIDE, "[SİNYAL — ATLA]"),
    ],
)
def test_skipped_entry_tells_the_user_and_counts_nothing(
    store: Store,
    cfg: ProcessorConfig,
    now_ms: int,
    received_at_ms: int | None,
    expected: Outcome,
    tag: str,
) -> None:
    signal_id = queue(store, alerts.ny_orb_fvg_entry(), received_at_ms=received_at_ms)

    row = process(store, cfg, now_ms)

    assert row.outcome is expected
    assert budget(store, signal_id) is None
    (message,) = outbox(store)
    assert message.kind is OutboxKind.SIGNAL
    assert message.signal_id == signal_id
    assert message.text.startswith(tag)
    assert message.expires_at_ms is None
    assert message.expired_text is None


def entry_at(minute: int, side: str = LONG) -> dict[str, object]:
    return alerts.ny_orb_fvg_entry(side, bar_time=BAR_TIME_MS + minute * MINUTE_MS)


@pytest.mark.parametrize("cfg", [CONFIG, UNKNOWN], ids=["contract_known", "contract_unknown"])
def test_daily_budget_fills_up_and_is_freed_by_a_release(
    store: Store, cfg: ProcessorConfig
) -> None:
    day = dt.date(2026, 7, 21)
    outcomes = [handle(store, entry_at(minute), cfg).outcome for minute in range(5)]

    # 4 x $250 = the $1000 cap exactly; the fifth does not fit.
    assert outcomes == [Outcome.FORWARDED] * 4 + [Outcome.SKIP_BUDGET]
    assert budget(store, 5) is None
    assert texts(store, 4)[0].count("günlük bütçe $1000/$1000 (bu sinyal dahil)") == 1
    assert "kullanılan $1000 + bu sinyal $250 > sınır $1000" in texts(store, 5)[0]
    with store.transaction() as tx:
        assert tx.budget_used(day) == 1000.0

    release(store, 2, ReleaseReason.SKIPPED)  # what /atladim 2 does

    assert handle(store, entry_at(6), cfg).outcome is Outcome.FORWARDED
    assert handle(store, entry_at(7), cfg).outcome is Outcome.SKIP_BUDGET
    with store.transaction() as tx:
        assert tx.budget_used(day) == 1000.0


def test_exit_and_cancel_do_not_free_the_budget(store: Store) -> None:
    handle(store, alerts.ny_orb_fvg_entry())
    handle(store, alerts.ny_orb_fvg_exit())
    handle(store, alerts.ict_silver_bullet_entry())
    handle(store, alerts.ict_silver_bullet_cancel())

    with store.transaction() as tx:
        assert [
            (row.signal_id, row.counted) for row in tx.budget_entries(dt.date(2026, 7, 21))
        ] == [
            (1, True),
            (3, True),
        ]


def test_unverified_rules_alarm_once_a_day_and_mark_every_signal(store: Store) -> None:
    cfg = config(rules_verified_at=None)

    first = handle(store, entry_at(0), cfg)
    second = handle(store, entry_at(1), cfg)
    next_day = queue(store, entry_at(2), received_at_ms=NOW_MS + DAY_MS)
    with store.transaction() as tx:
        third = process_one(tx, cfg, NOW_MS + DAY_MS)

    assert first.outcome is second.outcome is Outcome.FORWARDED
    assert third is not None
    assert third.outcome is Outcome.STALE
    rows = outbox(store)
    assert [(row.kind, row.signal_id) for row in rows] == [
        (OutboxKind.SIGNAL, 1),  # the signal goes out before the alarm about it
        (OutboxKind.ALARM, None),
        (OutboxKind.SIGNAL, 2),
        (OutboxKind.SIGNAL, next_day),
    ]
    assert rows[0].text.startswith("[KURALLAR DOĞRULANMADI — ")
    assert rows[2].text.startswith("[KURALLAR DOĞRULANMADI — ")
    assert rows[1].text.startswith("[ALARM] FTMO kuralları güncel doğrulanmamış: ")
    assert rows[1].expires_at_ms is None

    # A day later the alarm may be repeated.
    handle(store, entry_at(DAY_MS // MINUTE_MS + 5), cfg)
    assert [row.kind for row in outbox(store)][-2:] == [OutboxKind.SIGNAL, OutboxKind.ALARM]


# --- process_one: follow-ups against stored trades ------------------------------------------


@pytest.mark.parametrize(
    ("switches", "entry_outcome"),
    [
        ([StateKey.PAUSED], Outcome.SUPPRESSED_PAUSED),
        ([StateKey.KILLED], Outcome.SUPPRESSED_KILLED),
        ([StateKey.PAUSED, StateKey.KILLED], Outcome.SUPPRESSED_KILLED),
    ],
    ids=["paused", "killed", "paused+killed"],
)
def test_follow_ups_reach_the_user_while_paused_and_killed(
    store: Store, switches: list[StateKey], entry_outcome: Outcome
) -> None:
    handle(store, alerts.ny_orb_fvg_entry())
    handle(store, alerts.ict_silver_bullet_entry())
    for switch in switches:
        set_state(store, switch, "1")

    update = handle(store, alerts.ny_orb_fvg_stop_update())
    cancel = handle(store, alerts.ict_silver_bullet_cancel())
    flatten = handle(store, alerts.ny_orb_fvg_exit())
    entry = handle(store, alerts.asian_sweep_entry())

    assert [update.outcome, cancel.outcome, flatten.outcome] == [Outcome.FORWARDED] * 3
    assert entry.outcome is entry_outcome
    assert texts(store, update.id)[0].startswith("[STOP GÜNCELLE] ")
    assert texts(store, cancel.id)[0].startswith("[İPTAL] ")
    assert texts(store, flatten.id)[0].startswith("[ÇIKIŞ] ")
    assert texts(store, entry.id) == []
    assert all(row.expires_at_ms is None for row in outbox(store)[2:])


def test_stop_updates_follow_the_last_one_sent_and_drop_out_of_order_ones(store: Store) -> None:
    handle(store, alerts.ny_orb_fvg_entry())

    first = handle(store, trail(minute=18, offset=-3.5))
    out_of_order = handle(store, trail(minute=10, offset=-6.0))
    wider = handle(store, trail(minute=20, offset=-5.0))
    equal = handle(store, trail(minute=21, offset=-3.5))
    small = handle(store, trail(minute=22, offset=-3.47))
    tighter = handle(store, trail(minute=23, offset=-2.0))

    assert [row.outcome for row in (first, out_of_order, wider, equal, small, tighter)] == [
        Outcome.FORWARDED,
        Outcome.SUPERSEDED,
        Outcome.REJECTED_WIDEN,
        Outcome.FILTERED_MIN_STEP,
        Outcome.FILTERED_MIN_STEP,
        Outcome.FORWARDED,
    ]
    assert "−12.50 → −3.50 puan" in texts(store, first.id)[0]
    assert texts(store, out_of_order.id) == []
    assert "stop = kendi giriş fiyatın −3.50 puan" in texts(store, wider.id)[0]
    assert texts(store, equal.id) == texts(store, small.id) == []
    # The rejected and the filtered updates never became the stop to compare with.
    assert "−3.50 → −2.00 puan" in texts(store, tighter.id)[0]
    assert "(giriş id 1)" in texts(store, tighter.id)[0]


def test_worked_case_through_the_store(store: Store) -> None:
    handle(store, alerts.ny_orb_fvg_entry())

    row = handle(store, alerts.ny_orb_fvg_stop_update(est_entry=5125.25, stop=5112.0))

    assert row.outcome is Outcome.REJECTED_WIDEN
    assert "Strateji stopu GENİŞLETMEK istiyor" in texts(store, row.id)[0]
    assert "stopunu KORU: stop = kendi giriş fiyatın −12.50 puan" in texts(store, row.id)[0]


def test_nothing_follows_a_forwarded_exit_until_the_next_entry(store: Store) -> None:
    handle(store, alerts.ny_orb_fvg_entry())
    flatten = handle(store, alerts.ny_orb_fvg_exit())  # 269 minutes after the entry bar

    late_update = handle(store, trail(minute=270, offset=-1.0))
    earlier_update = handle(store, trail(minute=100, offset=-1.0))
    second_exit = handle(store, alerts.ny_orb_fvg_exit(bar_time=BAR_TIME_MS + 271 * MINUTE_MS))

    assert flatten.outcome is Outcome.FORWARDED
    assert [late_update.outcome, earlier_update.outcome, second_exit.outcome] == [
        Outcome.NO_ACTIVE_TRADE
    ] * 3
    assert len(outbox(store)) == 2

    # A new entry is a new trade: its follow-ups are judged against it, not the old exit.
    entry = handle(store, entry_at(300, SHORT))
    update = handle(store, trail(SHORT, minute=310, offset=3.5))
    assert (entry.outcome, update.outcome) == (Outcome.FORWARDED, Outcome.FORWARDED)
    assert f"(giriş id {entry.id})" in texts(store, update.id)[0]


def test_follow_up_of_a_suppressed_entry_is_dropped(store: Store) -> None:
    set_state(store, StateKey.PAUSED, "1")
    entry = handle(store, alerts.ny_orb_fvg_entry())

    update = handle(store, alerts.ny_orb_fvg_stop_update())
    flatten = handle(store, alerts.ny_orb_fvg_exit())

    assert entry.outcome is Outcome.SUPPRESSED_PAUSED
    assert (update.outcome, flatten.outcome) == (Outcome.NO_ACTIVE_TRADE,) * 2
    assert outbox(store) == []


def test_follow_up_that_overtook_its_entry_is_dropped(store: Store) -> None:
    # The stop update was stored first; its entry is still queued (no outcome yet).
    update_id = queue(store, alerts.ny_orb_fvg_stop_update())
    queue(store, alerts.ny_orb_fvg_entry())
    now_ms = new_signal(alerts.ny_orb_fvg_stop_update()).received_at_ms + 500

    update = process(store, CONFIG, now_ms)
    entry = process(store, CONFIG, now_ms)

    assert (update.id, update.outcome) == (update_id, Outcome.NO_ACTIVE_TRADE)
    assert entry.outcome is Outcome.STALE  # processed 18 minutes after its bar closed
    assert len(outbox(store)) == 1  # the stale notice of the entry


def test_released_entries_decide_what_follows(store: Store) -> None:
    handle(store, alerts.ny_orb_fvg_entry())
    handle(store, alerts.ict_silver_bullet_entry())
    release(store, 1, ReleaseReason.SKIPPED)
    release(store, 2, ReleaseReason.UNDELIVERED)

    skipped = handle(store, alerts.ny_orb_fvg_stop_update())
    undelivered = handle(store, alerts.ict_silver_bullet_cancel())

    assert skipped.outcome is Outcome.NO_ACTIVE_TRADE
    assert texts(store, skipped.id) == []
    assert undelivered.outcome is Outcome.FORWARDED
    (text,) = texts(store, undelivered.id)
    assert text.splitlines()[2] == "FTMO US500.cash'te emir dolmadıysa İPTAL ET."
    assert text.splitlines()[-2] == (
        "(giriş mesajı sana ulaşmamış olabilir; pozisyon açmadıysan yok say)"
    )

    # Counted again (/aldim): the trade is live again for its follow-ups.
    with store.transaction() as tx:
        tx.set_budget_counted(signal_id=1, counted=True, reason=None, now_ms=NOW_MS)
    assert handle(store, alerts.ny_orb_fvg_exit()).outcome is Outcome.FORWARDED


def test_reference_is_the_latest_entry_of_the_same_strategy_and_symbol(store: Store) -> None:
    handle(store, alerts.asian_sweep_entry())  # another strategy, same bar
    handle(store, alerts.ny_orb_fvg_entry(symbol="CME_MINI:NQ1!"))  # another symbol
    first = handle(store, entry_at(0))
    second = handle(store, entry_at(30))
    handle(store, entry_at(400))  # after the follow-up's bar: not its entry

    flatten = handle(store, alerts.ny_orb_fvg_exit())  # bar 269 minutes after the first entry

    assert (first.outcome, second.outcome) == (Outcome.FORWARDED, Outcome.FORWARDED)
    assert f"id {flatten.id} (giriş id {second.id}) · " in texts(store, flatten.id)[0]


def test_reference_lookback_is_24_hours_inclusive(store: Store) -> None:
    handle(store, alerts.ny_orb_fvg_entry())

    inside = handle(store, alerts.ny_orb_fvg_exit(bar_time=BAR_TIME_MS + DAY_MS))
    assert "(giriş id 1)" in texts(store, inside.id)[0]


def test_follow_up_more_than_24_hours_after_the_entry_is_an_orphan(store: Store) -> None:
    handle(store, alerts.ny_orb_fvg_entry())

    outside = handle(store, alerts.ny_orb_fvg_exit(bar_time=BAR_TIME_MS + DAY_MS + MINUTE_MS))

    assert outside.outcome is Outcome.FORWARDED
    assert texts(store, outside.id)[0].splitlines()[-2:] == [
        "(bu bot giriş sinyalini görmedi)",
        f"id {outside.id} · bar 11:31 NY / 18:31 TR",
    ]


def test_orphan_stop_updates_are_compared_with_each_other(store: Store) -> None:
    first = handle(store, trail(minute=18, offset=-3.5))
    wider = handle(store, trail(minute=19, offset=-4.0))
    older = handle(store, trail(minute=5, offset=-1.0))
    other_side = handle(store, trail(SHORT, minute=20, offset=6.0))
    tighter = handle(store, trail(minute=21, offset=-2.0))

    assert [row.outcome for row in (first, wider, older, other_side, tighter)] == [
        Outcome.FORWARDED,
        Outcome.REJECTED_WIDEN,
        Outcome.SUPERSEDED,
        Outcome.FORWARDED,
        # The latest update sent is now the short one: nothing of this side to compare with.
        Outcome.FORWARDED,
    ]
    assert "Yalnızca mevcut stop'undan DAHA SIKIysa uygula." in texts(store, first.id)[0]
    assert "stop = kendi giriş fiyatın −3.50 puan" in texts(store, wider.id)[0]
    assert "Yalnızca mevcut stop'undan DAHA SIKIysa uygula." in texts(store, other_side.id)[0]
    assert "Yalnızca mevcut stop'undan DAHA SIKIysa uygula." in texts(store, tighter.id)[0]


def test_side_mismatch_alarm_is_throttled_to_ten_minutes(store: Store) -> None:
    handle(store, alerts.ny_orb_fvg_entry())

    outcomes = [
        handle(store, trail(SHORT, minute=minute, offset=3.5)).outcome
        for minute in (18, 20, 27, 28)
    ]

    assert outcomes == [Outcome.NO_ACTIVE_TRADE] * 4
    rows = outbox(store)[1:]
    # Alarms at minute 18 and, ten minutes later, at minute 28; none for 20 and 27.
    assert [(row.kind, row.signal_id) for row in rows] == [(OutboxKind.ALARM, None)] * 2
    assert "(id 2), ama son giriş (id 1) LONG yönlüydü" in rows[0].text
    assert "(id 5), ama son giriş (id 1) LONG yönlüydü" in rows[1].text


# --- process_one: every payload shape the Pine scripts can send ----------------------------


def _sequences() -> list[list[str]]:
    """Each real payload shape once, in the order its strategy would send it."""
    sequences = [["heartbeat/heartbeat"]]
    for side in (LONG, SHORT):
        sequences += [
            [
                f"ny_orb_fvg/entry/{side}/FVG",
                f"ny_orb_fvg/stop_update/{side}",
                f"ny_orb_fvg/exit/{side}",
            ],
            [f"ny_orb_fvg/entry/{side}/IFVG"],
            [f"asian_sweep/entry/{side}", f"asian_sweep/exit/{side}"],
            [
                f"ict_silver_bullet/entry/{side}",
                f"ict_silver_bullet/cancel/{side}",
                f"ict_silver_bullet/exit/{side}",
            ],
            [f"mr_bb_kc_rsi2/entry/{side}", f"mr_bb_kc_rsi2/exit/{side}"],
            [f"mr_bb_kc_rsi2/entry/{side}/target_one_tick"],
            [
                f"mtf_trend_cont/entry/{side}",
                f"mtf_trend_cont/stop_update/{side}/before_tp1",
                f"mtf_trend_cont/stop_update/{side}/breakeven",
                f"mtf_trend_cont/exit/{side}",
            ],
        ]
    return sequences


SEQUENCES = _sequences()


def expected_outcome(shape: str) -> Outcome:
    if shape == "heartbeat/heartbeat":
        return Outcome.HEARTBEAT
    if shape.endswith("/before_tp1"):
        # The stop re-priced from the real fill: entry alert 10.00 from the signal close,
        # update 10.50 from TradingView's fill. In the trader's frame that is wider.
        return Outcome.REJECTED_WIDEN
    return Outcome.FORWARDED


def test_the_sequences_cover_every_real_payload_shape() -> None:
    shapes = [shape for sequence in SEQUENCES for shape in sequence]

    assert len(shapes) == len(set(shapes)) == 33
    assert set(shapes) == set(alerts.pine_payloads())


@pytest.mark.parametrize("cfg", [CONFIG, UNKNOWN], ids=["contract_known", "contract_unknown"])
@pytest.mark.parametrize("sequence", SEQUENCES, ids=[sequence[0] for sequence in SEQUENCES])
def test_every_pine_payload_shape_is_processed(
    store: Store, sequence: list[str], cfg: ProcessorConfig
) -> None:
    payloads = alerts.pine_payloads()

    for shape in sequence:
        row = handle(store, payloads[shape], cfg)

        assert row.outcome is expected_outcome(shape), shape
        sent = texts(store, row.id)
        assert len(sent) == (0 if row.outcome is Outcome.HEARTBEAT else 1), shape
        for text in sent:
            assert re.search(r"None|\bnan\b|\binf\b|\?", text) is None, text
            assert text.splitlines()[-1].startswith(f"id {row.id} "), shape
    with store.transaction() as tx:
        assert tx.queue_depth() == 0
        entries = tx.budget_entries(dt.date(2026, 7, 21))
    assert len(entries) == (0 if sequence == ["heartbeat/heartbeat"] else 1)
    assert all(row.kind is OutboxKind.SIGNAL for row in outbox(store))


# --- process_one: prop day ----------------------------------------------------------------


@pytest.mark.parametrize(
    ("prague_day", "midnight_utc"),
    [
        ("2026-03-09", "2026-03-08 23:00"),  # US already on summer time, Prague on CET
        ("2026-03-20", "2026-03-19 23:00"),
        ("2026-03-29", "2026-03-28 23:00"),  # the day Prague changes (at 02:00): still CET
        ("2026-03-30", "2026-03-29 22:00"),  # first midnight on CEST, after a 23-hour day
        ("2026-10-25", "2026-10-24 22:00"),  # the day Prague changes back (at 03:00)
        ("2026-10-26", "2026-10-25 23:00"),  # first midnight on CET, after a 25-hour day
        ("2026-10-29", "2026-10-28 23:00"),  # Prague on CET, US still on summer time
        ("2026-11-01", "2026-10-31 23:00"),
        ("2026-11-02", "2026-11-01 23:00"),
    ],
)
def test_budget_rolls_over_at_prague_midnight(
    store: Store, prague_day: str, midnight_utc: str
) -> None:
    day = dt.date.fromisoformat(prague_day)
    midnight_ms = ms_from_datetime(dt.datetime.combine(day, dt.time(0, 0), tzinfo=PRAGUE))
    assert midnight_ms == ms_from_datetime(
        dt.datetime.fromisoformat(midnight_utc).replace(tzinfo=dt.UTC)
    )
    cfg = config(rules_verified_at=(day - dt.timedelta(days=2)).isoformat())

    def entry_closing_at(close_ms: int) -> dict[str, object]:
        return alerts.ny_orb_fvg_entry(bar_time=close_ms - MINUTE_MS)

    before = [
        handle(store, entry_closing_at(midnight_ms - minutes * MINUTE_MS), cfg)
        for minutes in (9, 8, 7, 6)
    ]
    # Processed in the last millisecond of the old prop day: the day's budget is spent.
    queue(store, entry_closing_at(midnight_ms - MINUTE_MS))
    last_of_day = process(store, cfg, midnight_ms - 1)
    # Processed in the first millisecond of the new prop day.
    queue(store, entry_closing_at(midnight_ms), received_at_ms=midnight_ms)
    first_of_day = process(store, cfg, midnight_ms)

    assert [row.outcome for row in before] == [Outcome.FORWARDED] * 4
    assert last_of_day.outcome is Outcome.SKIP_BUDGET
    assert first_of_day.outcome is Outcome.FORWARDED
    previous_day = day - dt.timedelta(days=1)
    with store.transaction() as tx:
        assert [row.signal_id for row in tx.budget_entries(previous_day)] == [1, 2, 3, 4]
        assert [row.signal_id for row in tx.budget_entries(day)] == [first_of_day.id]
        assert tx.budget_used(previous_day) == 1000.0
        assert tx.budget_used(day) == 250.0
    assert "günlük bütçe $250/$1000 (bu sinyal dahil)" in texts(store, first_of_day.id)[0]


# --- process_one: failure -----------------------------------------------------------------


class FailingTx:
    """A transaction whose `method` raises; every other call reaches the real one."""

    def __init__(self, tx: Tx, method: str) -> None:
        self._tx = tx
        self._method = method

    def __getattr__(self, name: str) -> Any:
        if name == self._method:
            raise RuntimeError("injected failure")
        return getattr(self._tx, name)


def snapshot(store: Store) -> tuple[int, int, int, list[Outcome | None]]:
    with store.transaction() as tx:
        signals = tx.signals_received_between(0, 2**62)
        return (
            tx.queue_depth(),
            len(tx.messages_created_between(0, 2**62)),
            len(tx.budget_entries(dt.date(2026, 7, 21))),
            [signal.outcome for signal in signals],
        )


@pytest.mark.parametrize(
    "method",
    ["budget_used", "complete_signal", "add_budget_entry", "enqueue_message", "get_signal"],
)
def test_an_exception_inside_process_one_leaves_nothing_behind(store: Store, method: str) -> None:
    queue(store, alerts.ny_orb_fvg_entry())

    with pytest.raises(RuntimeError, match="injected failure"), store.transaction() as tx:
        process_one(cast(Tx, FailingTx(tx, method)), CONFIG, NOW_MS)

    assert snapshot(store) == (1, 0, 0, [None])
    # The signal is still at the head of the queue and can be processed.
    assert process(store, CONFIG, NOW_MS).outcome is Outcome.FORWARDED
    assert snapshot(store) == (0, 1, 1, [Outcome.FORWARDED])


def test_a_rules_alarm_failure_rolls_the_signal_back_too(store: Store) -> None:
    queue(store, alerts.ny_orb_fvg_entry())

    with pytest.raises(RuntimeError, match="injected failure"), store.transaction() as tx:
        process_one(
            cast(Tx, FailingTx(tx, "enqueue_alarm")), config(rules_verified_at=None), NOW_MS
        )

    assert snapshot(store) == (1, 0, 0, [None])


# ============================================================================================
# run_processor: the queue task
# ============================================================================================


class FakeClock:
    def __init__(self, now_ms: int = NOW_MS) -> None:
        self.now_ms = now_ms

    def __call__(self) -> int:
        return self.now_ms


class Runner:
    """`run_processor` as a task, with its events and the `on_messages` calls recorded."""

    def __init__(
        self,
        store: Store,
        *,
        cfg: ProcessorConfig = CONFIG,
        clock: Callable[[], int] | None = None,
        idle_poll_seconds: float = 60.0,
    ) -> None:
        self.store = store
        self.wake = asyncio.Event()
        self.stop = asyncio.Event()
        self.message_calls = 0
        self.task = asyncio.create_task(
            run_processor(
                store=store,
                config=cfg,
                clock=clock or FakeClock(),
                wake=self.wake,
                on_messages=self._on_messages,
                stop=self.stop,
                idle_poll_seconds=idle_poll_seconds,
            )
        )

    def _on_messages(self) -> None:
        self.message_calls += 1

    async def until(self, condition: Callable[[], bool], timeout: float = 5.0) -> None:
        deadline = asyncio.get_running_loop().time() + timeout
        while not condition():
            if self.task.done():
                self.task.result()  # re-raises what ended the task
                raise AssertionError("the processor stopped before the condition was met")
            assert asyncio.get_running_loop().time() < deadline, "condition not reached in time"
            await asyncio.sleep(0.002)

    async def until_drained(self) -> None:
        await self.until(lambda: snapshot(self.store)[0] == 0)

    async def finish(self, timeout: float = 2.0) -> None:
        self.stop.set()
        await asyncio.wait_for(self.task, timeout=timeout)


def run(scenario: Callable[[], Coroutine[Any, Any, None]]) -> None:
    asyncio.run(scenario())


def test_run_processor_drains_the_queue_wakes_up_and_stops(store: Store) -> None:
    for minute in range(3):
        queue(store, entry_at(minute))
    queue(store, alerts.heartbeat_payload())

    async def scenario() -> None:
        runner = Runner(store, clock=FakeClock(BAR_TIME_MS + 3 * MINUTE_MS + 500))
        await runner.until_drained()
        # Three entries enqueued a message each; the heartbeat enqueued nothing.
        await runner.until(lambda: runner.message_calls == 3)
        assert snapshot(store)[3] == [
            Outcome.STALE,
            Outcome.STALE,
            Outcome.FORWARDED,
            Outcome.HEARTBEAT,
        ]

        # Idle now (the poll is a minute away): only `wake` can make it look again.
        await asyncio.sleep(0.05)
        assert not runner.task.done()
        queue(store, alerts.ny_orb_fvg_exit(bar_time=BAR_TIME_MS + 3 * MINUTE_MS))
        runner.wake.set()
        await runner.until_drained()
        await runner.until(lambda: runner.message_calls == 4)
        # The wake-up was consumed: the task is waiting again, not spinning.
        await runner.until(lambda: not runner.wake.is_set())

        # `stop` ends the idle wait at once.
        started = asyncio.get_running_loop().time()
        await runner.finish()
        assert asyncio.get_running_loop().time() - started < 1.0

    run(scenario)
    assert snapshot(store)[0] == 0


def test_run_processor_polls_when_nobody_wakes_it(store: Store) -> None:
    async def scenario() -> None:
        runner = Runner(store, idle_poll_seconds=0.01)
        await asyncio.sleep(0.03)
        queue(store, alerts.ny_orb_fvg_entry())  # no wake
        await runner.until_drained()
        await runner.finish()

    run(scenario)
    assert snapshot(store) == (0, 1, 1, [Outcome.FORWARDED])


def test_run_processor_reads_the_clock_for_every_signal(store: Store) -> None:
    queue(store, alerts.ny_orb_fvg_entry())
    queue(store, alerts.asian_sweep_entry())
    clock = FakeClock()

    def ticking() -> int:
        clock.now_ms += 100
        return clock.now_ms

    async def scenario() -> None:
        runner = Runner(store, clock=ticking)
        await runner.until_drained()
        await runner.finish()

    run(scenario)
    with store.transaction() as tx:
        first, second = tx.signals_received_between(0, 2**62)
    assert (first.processed_at_ms, second.processed_at_ms) == (NOW_MS + 100, NOW_MS + 200)


def test_run_processor_does_nothing_once_stop_is_set(store: Store) -> None:
    queue(store, alerts.ny_orb_fvg_entry())

    async def scenario() -> None:
        runner = Runner(store)
        runner.stop.set()
        await asyncio.wait_for(runner.task, timeout=2.0)
        assert runner.message_calls == 0

    run(scenario)
    assert snapshot(store) == (1, 0, 0, [None])


def poison_entry() -> NewSignal:
    """An entry without a stop: the schema refuses it, the store does not."""
    return dataclasses.replace(new_signal(alerts.asian_sweep_entry()), stop=None)


def test_a_poisoned_signal_is_set_aside_and_the_queue_moves_on(store: Store) -> None:
    with store.transaction() as tx:
        poisoned_id = tx.insert_signal(poison_entry())
    assert poisoned_id == 1
    queue(store, alerts.ny_orb_fvg_entry())

    async def scenario() -> None:
        runner = Runner(store)
        await runner.until_drained()
        await runner.until(lambda: runner.message_calls == 2)
        await runner.finish()

    with capture_logs() as logs:
        run(scenario)

    assert snapshot(store) == (0, 2, 1, [Outcome.ERROR, Outcome.FORWARDED])
    alarm, signal = outbox(store)
    with store.transaction() as tx:
        poisoned = tx.get_signal(1)
    assert poisoned is not None
    assert poisoned.processed_at_ms == NOW_MS
    assert (alarm.kind, alarm.signal_id, alarm.expires_at_ms) == (OutboxKind.ALARM, None, None)
    assert alarm.text.startswith(
        "[ALARM] Bir TradingView alert'i işlenemedi ve İLETİLMEDİ (id 1 · Asian Sweep · giriş "
        "· LONG · CME_MINI:MES1! · bar 11:30 NY / 18:30 TR).\n"
    )
    assert (signal.kind, signal.signal_id) == (OutboxKind.SIGNAL, 2)
    assert budget(store, 1) is None
    failures = [entry for entry in logs if entry["event"] == "signal_processing_failed"]
    assert failures == [
        {
            "event": "signal_processing_failed",
            "log_level": "error",
            "signal_id": 1,
            "strategy_id": "asian_sweep",
            "action": "entry",
            "error_type": "SignalDataError",
        }
    ]
    processed = [entry for entry in logs if entry["event"] == "signal_processed"]
    assert processed == [
        {
            "event": "signal_processed",
            "log_level": "info",
            "signal_id": 2,
            "strategy_id": "ny_orb_fvg",
            "action": "entry",
            "outcome": "forwarded",
            "messages": 1,
        }
    ]


def test_a_failure_after_the_first_write_is_rolled_back_before_the_quarantine(store: Store) -> None:
    # A ledger row that should not exist makes `add_budget_entry` fail AFTER the outcome
    # was written: the whole transaction must be undone before the signal is set aside.
    signal_id = queue(store, alerts.ny_orb_fvg_entry())
    with store.transaction() as tx:
        tx.add_budget_entry(
            signal_id=signal_id, prop_day=dt.date(2026, 7, 21), risk_amount=1.0, now_ms=NOW_MS
        )

    async def scenario() -> None:
        runner = Runner(store)
        await runner.until_drained()
        await runner.finish()

    with capture_logs() as logs:
        run(scenario)

    assert snapshot(store) == (0, 1, 1, [Outcome.ERROR])
    (alarm,) = outbox(store)
    assert alarm.kind is OutboxKind.ALARM
    assert alarm.text.startswith("[ALARM] Bir TradingView alert'i işlenemedi")
    assert [entry["error_type"] for entry in logs if entry["log_level"] == "error"] == [
        "StoreError"
    ]


def test_unknown_actions_are_quarantined_one_by_one(store: Store) -> None:
    with store.transaction() as tx:
        for minute in range(3):
            payload = alerts.ny_orb_fvg_exit(bar_time=BAR_TIME_MS + minute * MINUTE_MS)
            tx.insert_signal(dataclasses.replace(new_signal(payload), action="flatten"))

    async def scenario() -> None:
        runner = Runner(store)
        await runner.until_drained()
        await runner.until(lambda: runner.message_calls == 3)
        await runner.finish()

    run(scenario)
    assert snapshot(store) == (0, 3, 0, [Outcome.ERROR] * 3)
    assert all(row.kind is OutboxKind.ALARM for row in outbox(store))


def test_a_failing_quarantine_ends_the_task(store: Store, monkeypatch: pytest.MonkeyPatch) -> None:
    with store.transaction() as tx:
        tx.insert_signal(poison_entry())

    def broken_quarantine(_store: Store, _signal_id: int, _now_ms: int) -> bool:
        raise StoreError("veritabanı hatası")

    monkeypatch.setattr(processor, "_quarantine", broken_quarantine)

    async def scenario() -> None:
        runner = Runner(store)
        with pytest.raises(StoreError, match="veritabanı hatası"):
            await asyncio.wait_for(runner.task, timeout=2.0)
        assert runner.message_calls == 0

    run(scenario)
    assert snapshot(store) == (1, 0, 0, [None])


def test_a_store_that_cannot_be_read_ends_the_task() -> None:
    closed = Store(":memory:")
    closed.close()

    async def scenario() -> None:
        runner = Runner(closed)
        with pytest.raises(StoreError, match="veritabanı kapalı"):
            await asyncio.wait_for(runner.task, timeout=2.0)

    run(scenario)


def test_quarantine_refuses_a_signal_that_is_no_longer_queued(store: Store) -> None:
    signal_id = queue(store, alerts.ny_orb_fvg_entry())
    process(store, CONFIG, NOW_MS)

    assert processor._quarantine(store, signal_id, NOW_MS) is False
    assert processor._quarantine(store, 99, NOW_MS) is False
    assert snapshot(store) == (0, 1, 1, [Outcome.FORWARDED])


def test_the_alarm_text_of_a_quarantined_signal_is_the_message_builder(store: Store) -> None:
    with store.transaction() as tx:
        tx.insert_signal(poison_entry())
        queued = tx.get_signal(1)
    assert queued is not None

    assert processor._quarantine(store, 1, NOW_MS) is True

    (alarm,) = outbox(store)
    assert alarm.text == messages.processing_error_alarm(queued)
    assert snapshot(store) == (0, 1, 0, [Outcome.ERROR])
