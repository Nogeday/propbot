"""VenueProfile: enums, ranges, reset time, null reporting and the 30-day staleness rule."""

import datetime as dt
from typing import Any

import pytest
from pydantic import ValidationError

from propbot.risk import (
    RULES_MAX_AGE_DAYS,
    ConfigError,
    DailyLossBasis,
    MaxLossType,
    Platform,
    RulesState,
    UnresolvedField,
    UnresolvedKind,
    VenueProfile,
    load_venue_profile,
    rules_freshness,
    unresolved_fields,
)
from propbot.risk import venue as venue_module
from propbot.risk.loader import read_yaml_mapping
from propbot.risk.venue import MAX_TV_SYMBOLS
from propbot.signals import schema as alert_schema
from support.paths import CONFIG_DIR, CONFIG_FIXTURES

REAL = CONFIG_DIR / "venues" / "ftmo_2step_100k.yaml"
VALID = CONFIG_FIXTURES / "valid" / "venues" / "sample_2step.yaml"
UNVERIFIED = CONFIG_FIXTURES / "valid_unverified" / "venues" / "sample_unverified.yaml"
TODAY = dt.date(2026, 11, 13)
INVALID_DIR = CONFIG_FIXTURES / "invalid" / "venue"

# fixture stem -> (issue location, fragment of the Turkish message)
MUST_FAIL: dict[str, tuple[str, str]] = {
    "platform_unknown": ("platform", "izin verilenler: 'mt5'; okunan: 'mt4'"),
    "max_loss_type_unknown": ("max_loss_type", "izin verilenler: 'static', 'trailing_eod'"),
    "daily_loss_basis_unknown": (
        "daily_loss_basis",
        "izin verilenler: 'max_balance_equity_at_reset', 'balance_at_reset'",
    ),
    "daily_limit_not_below_max_loss": (
        "",
        "daily_loss_limit_pct (10.0) max_loss_pct değerinden (10.0) küçük olmalı",
    ),
    "reset_tz_unknown": ("daily_reset.tz", "bilinmeyen saat dilimi: 'Europe/Prag'"),
    "reset_time_unquoted": ("daily_reset.time", "okunan: 1350"),
    "reset_time_out_of_range": ("daily_reset.time", "okunan: '24:00'"),
    "verified_date_with_time": ("rules_verified_at", "saat içermemeli"),
    "verified_date_bad_format": ("rules_verified_at", "YYYY-AA-GG biçiminde"),
    "min_trading_days_negative": ("min_trading_days", "en az 0 olmalı"),
    "null_in_required_field": ("max_loss_pct", "boş (null) bırakılamaz"),
    "nullable_key_missing": ("min_trading_days", "zorunlu alan eksik"),
    "server_tz_unknown": ("server_tz", "bilinmeyen saat dilimi: 'Mars/Olympus'"),
    "phase_target_out_of_range": ("profit_target_pct.phase1", "100.0 değerinden küçük olmalı"),
    "currency_not_iso": ("account_currency", "biçim uymuyor"),
    "contract_negative": ("contract.tick_size", "0.0 değerinden büyük olmalı"),
    "unknown_key": ("leverage", "bilinmeyen anahtar"),
    "signal_section_missing": ("signal", "zorunlu alan eksik"),
    "tv_symbols_empty": ("signal.tv_symbols", "en az bir sembol içermeli"),
    "tv_symbols_duplicate": ("signal.tv_symbols", "yinelenen sembol: CME_MINI:MES1!"),
    "tv_symbol_with_space": ("signal.tv_symbols.1", "okunan: 'CME_MINI: ES1!'"),
    "tv_symbols_not_a_list": ("signal.tv_symbols", "liste olmalı"),
    "min_stop_points_zero": ("signal.min_stop_points", "0.0 değerinden büyük olmalı"),
    "min_stop_points_null": ("signal.min_stop_points", "boş (null) bırakılamaz"),
}


def _valid_data() -> dict[Any, Any]:
    return read_yaml_mapping(VALID)


def _valid_profile() -> VenueProfile:
    return load_venue_profile(VALID)


def test_real_venue_profile_loads() -> None:
    # Values in the real profile change as the user verifies them; only invariants are pinned.
    venue = load_venue_profile(REAL)

    assert isinstance(venue.platform, Platform)
    assert isinstance(venue.max_loss_type, MaxLossType)
    assert isinstance(venue.daily_loss_basis, DailyLossBasis)
    assert venue.daily_loss_limit_pct < venue.max_loss_pct
    assert venue.daily_reset.zone.key == venue.daily_reset.tz
    assert isinstance(venue.daily_reset.time_of_day, dt.time)


def test_real_venue_profile_reports_exactly_its_null_fields() -> None:
    raw = read_yaml_mapping(REAL)
    null_top_level = {key for key, value in raw.items() if value is None}
    null_contract = {f"contract.{key}" for key, value in raw["contract"].items() if value is None}

    reported = {field.path for field in unresolved_fields(load_venue_profile(REAL))}

    assert reported == null_top_level | null_contract


def test_unverified_profile_reports_its_null_fields_in_file_order() -> None:
    assert unresolved_fields(load_venue_profile(UNVERIFIED)) == (
        UnresolvedField("rules_verified_at", UnresolvedKind.VERIFY),
        UnresolvedField("min_trading_days", UnresolvedKind.VERIFY),
        UnresolvedField("server_tz", UnresolvedKind.VERIFY),
        UnresolvedField("contract.tick_size", UnresolvedKind.RUNTIME),
        UnresolvedField("contract.tick_value_per_lot", UnresolvedKind.RUNTIME),
        UnresolvedField("contract.min_lot", UnresolvedKind.RUNTIME),
        UnresolvedField("contract.lot_step", UnresolvedKind.RUNTIME),
    )


def test_unverified_profile_keeps_order_modes_locked() -> None:
    freshness = rules_freshness(load_venue_profile(UNVERIFIED), TODAY)

    assert freshness.state is RulesState.MISSING
    assert freshness.order_modes_locked


def test_fully_resolved_profile_reports_nothing() -> None:
    venue = _valid_profile()

    assert unresolved_fields(venue) == ()
    assert venue.rules_verified_at == dt.date(2026, 11, 1)
    assert venue.min_trading_days == 4
    assert venue.news_rule.applies_to == ("funded_standard",)


def test_every_must_fail_fixture_is_listed() -> None:
    assert {path.stem for path in INVALID_DIR.glob("*.yaml")} == set(MUST_FAIL)


@pytest.mark.parametrize(
    ("stem", "location", "fragment"),
    [(stem, *expected) for stem, expected in MUST_FAIL.items()],
    ids=list(MUST_FAIL),
)
def test_invalid_venue_file_is_rejected(stem: str, location: str, fragment: str) -> None:
    with pytest.raises(ConfigError) as excinfo:
        load_venue_profile(INVALID_DIR / f"{stem}.yaml")

    issues = excinfo.value.issues
    assert any(i.location == location and fragment in i.message for i in issues), issues


# --- the bot's own entry filters: signal.tv_symbols and signal.min_stop_points ---------------


def test_real_profile_carries_a_usable_signal_filter() -> None:
    venue = load_venue_profile(REAL)

    assert venue.signal.tv_symbols
    assert len(set(venue.signal.tv_symbols)) == len(venue.signal.tv_symbols)
    # Every listed symbol must be one the webhook receiver lets in.
    assert all(alert_schema._SYMBOL.fullmatch(symbol) for symbol in venue.signal.tv_symbols)
    # User decision 33 (2026-11-16); changing it needs a written reason (docs/adr/0006).
    assert venue.signal.min_stop_points == 1.5


def test_tv_symbols_have_the_shape_the_alert_schema_accepts() -> None:
    # Two layers, one rule: a listed symbol the receiver refuses could never match.
    assert venue_module._TV_SYMBOL.pattern == alert_schema._SYMBOL.pattern


def test_tv_symbols_keep_their_order_and_spelling() -> None:
    data = _valid_data()
    data["signal"]["tv_symbols"] = ["US500.cash", "OANDA:SPX500USD", "CME_MINI:MES1!"]

    venue = VenueProfile.model_validate(data)

    assert venue.signal.tv_symbols == ("US500.cash", "OANDA:SPX500USD", "CME_MINI:MES1!")


@pytest.mark.parametrize(
    "symbol", ["", "CME_MINI:MES 1!", "CME_MINI/MES1!", "MES1!\n", "A" * 65, "ÇME:MES1!", "a,b"]
)
def test_a_malformed_tv_symbol_is_rejected(symbol: str) -> None:
    data = _valid_data()
    data["signal"]["tv_symbols"] = ["CME_MINI:MES1!", symbol]

    with pytest.raises(ValidationError, match="TradingView sembolü olmalı"):
        VenueProfile.model_validate(data)


@pytest.mark.parametrize("symbol", [None, 500, ["CME_MINI:MES1!"]])
def test_a_tv_symbol_that_is_not_text_is_rejected(symbol: object) -> None:
    data = _valid_data()
    data["signal"]["tv_symbols"] = [symbol]

    with pytest.raises(ValidationError):
        VenueProfile.model_validate(data)


def test_the_tv_symbol_list_is_bounded() -> None:
    # The number the documents give (SPEC §8.3, ADR-0006): written out, not imported.
    assert MAX_TV_SYMBOLS == 32
    data = _valid_data()
    data["signal"]["tv_symbols"] = [f"FEED:SYM{index}" for index in range(32)]
    assert len(VenueProfile.model_validate(data).signal.tv_symbols) == 32

    data["signal"]["tv_symbols"].append("FEED:ONE_MORE")
    with pytest.raises(ValidationError, match="en çok 32 sembol"):
        VenueProfile.model_validate(data)


def test_symbols_that_differ_only_in_case_are_two_symbols() -> None:
    # The match with an alert is case-sensitive, so the list is too.
    data = _valid_data()
    data["signal"]["tv_symbols"] = ["OANDA:SPX500USD", "oanda:spx500usd"]

    assert len(VenueProfile.model_validate(data).signal.tv_symbols) == 2


@pytest.mark.parametrize("value", [0.25, 1.5, 2, 40.0])
def test_min_stop_points_accepts_any_positive_number(value: float) -> None:
    data = _valid_data()
    data["signal"]["min_stop_points"] = value

    assert VenueProfile.model_validate(data).signal.min_stop_points == value


@pytest.mark.parametrize("value", [0, -1.5, "1.5", True, float("nan"), float("inf")])
def test_min_stop_points_rejects_everything_else(value: object) -> None:
    data = _valid_data()
    data["signal"]["min_stop_points"] = value

    with pytest.raises(ValidationError):
        VenueProfile.model_validate(data)


@pytest.mark.parametrize("key", ["tv_symbols", "min_stop_points"])
def test_both_signal_keys_are_required(key: str) -> None:
    data = _valid_data()
    del data["signal"][key]

    with pytest.raises(ValidationError):
        VenueProfile.model_validate(data)


def test_the_signal_section_takes_no_unknown_key() -> None:
    data = _valid_data()
    data["signal"]["max_stop_points"] = 50

    with pytest.raises(ValidationError):
        VenueProfile.model_validate(data)


@pytest.mark.parametrize("value", [member.value for member in MaxLossType])
def test_every_max_loss_type_is_accepted(value: str) -> None:
    data = _valid_data()
    data["max_loss_type"] = value

    assert VenueProfile.model_validate(data).max_loss_type is MaxLossType(value)


@pytest.mark.parametrize("value", [member.value for member in DailyLossBasis])
def test_every_daily_loss_basis_is_accepted(value: str) -> None:
    data = _valid_data()
    data["daily_loss_basis"] = value

    assert VenueProfile.model_validate(data).daily_loss_basis is DailyLossBasis(value)


@pytest.mark.parametrize("text", ["00:00", "09:30", "23:59", "17:00"])
def test_reset_time_accepts_quoted_hh_mm(text: str) -> None:
    data = _valid_data()
    data["daily_reset"]["time"] = text

    reset = VenueProfile.model_validate(data).daily_reset
    assert reset.time_of_day == dt.time.fromisoformat(text)


@pytest.mark.parametrize(
    "bad", ["24:00", "00:60", "7:5", "0000", "00:00:00", " 00:00", "", 0, 1350]
)
def test_reset_time_rejects_anything_else(bad: object) -> None:
    data = _valid_data()
    data["daily_reset"]["time"] = bad

    with pytest.raises(ValidationError) as excinfo:
        VenueProfile.model_validate(data)

    assert [error["loc"] for error in excinfo.value.errors()] == [("daily_reset", "time")]


@pytest.mark.parametrize("zone", ["UTC", "Europe/Prague", "America/New_York", "Europe/Istanbul"])
def test_time_zone_must_resolve_with_zoneinfo(zone: str) -> None:
    data = _valid_data()
    data["daily_reset"]["tz"] = zone
    data["server_tz"] = zone

    venue = VenueProfile.model_validate(data)
    assert venue.daily_reset.zone.key == zone


@pytest.mark.parametrize("zone", ["Europe/Prag", "CEST+1", "", "/etc/localtime", "../UTC"])
def test_unknown_time_zone_is_rejected(zone: str) -> None:
    data = _valid_data()
    data["daily_reset"]["tz"] = zone

    with pytest.raises(ValidationError) as excinfo:
        VenueProfile.model_validate(data)

    assert [error["type"] for error in excinfo.value.errors()] == ["unknown_time_zone"]


def test_verified_date_accepts_quoted_iso_text() -> None:
    data = _valid_data()
    data["rules_verified_at"] = "2026-10-15"

    assert VenueProfile.model_validate(data).rules_verified_at == dt.date(2026, 10, 15)


@pytest.mark.parametrize(
    "bad", ["2026-02-30", "15.10.2026", "2026-10-15T00:00:00", 20261015, dt.datetime(2026, 10, 15)]
)
def test_verified_date_rejects_non_dates(bad: object) -> None:
    data = _valid_data()
    data["rules_verified_at"] = bad

    with pytest.raises(ValidationError) as excinfo:
        VenueProfile.model_validate(data)

    assert [error["loc"] for error in excinfo.value.errors()] == [("rules_verified_at",)]


@pytest.mark.parametrize(
    ("daily", "total", "accepted"),
    [(5.0, 10.0, True), (9.99, 10.0, True), (10.0, 10.0, False), (10.01, 10.0, False)],
)
def test_daily_loss_limit_must_be_below_max_loss(
    daily: float, total: float, accepted: bool
) -> None:
    data = _valid_data()
    data["daily_loss_limit_pct"] = daily
    data["max_loss_pct"] = total

    if accepted:
        assert VenueProfile.model_validate(data).max_loss_pct == total
    else:
        with pytest.raises(ValidationError) as excinfo:
            VenueProfile.model_validate(data)
        assert [e["type"] for e in excinfo.value.errors()] == ["daily_limit_not_below_max_loss"]


def test_single_phase_program_may_leave_phase2_null() -> None:
    data = _valid_data()
    data["profit_target_pct"]["phase2"] = None

    assert VenueProfile.model_validate(data).profit_target_pct.phase2 is None


@pytest.mark.parametrize(
    "field",
    [
        "venue",
        "platform",
        "initial_balance",
        "profit_target_pct",
        "daily_loss_limit_pct",
        "max_loss_pct",
        "max_loss_type",
        "daily_reset",
        "daily_loss_basis",
        "automation_allowed",
        "firm_request_threshold_per_day",
        "contract",
    ],
)
def test_null_is_rejected_outside_verify_and_runtime_fields(field: str) -> None:
    data = _valid_data()
    data[field] = None

    with pytest.raises(ValidationError):
        VenueProfile.model_validate(data)


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("initial_balance", 0),
        ("initial_balance", "100000"),
        ("automation_allowed", 1),
        ("automation_allowed", "true"),
        ("firm_request_threshold_per_day", 0),
        ("min_trading_days", 4.0),
        ("min_trading_days", True),
        ("venue", ""),
        ("symbol", ""),
    ],
)
def test_mistyped_or_empty_value_is_rejected(field: str, value: object) -> None:
    data = _valid_data()
    data[field] = value

    with pytest.raises(ValidationError) as excinfo:
        VenueProfile.model_validate(data)

    assert [error["loc"] for error in excinfo.value.errors()] == [(field,)]


def test_news_rule_items_must_be_text() -> None:
    data = _valid_data()
    data["news_rule"]["applies_to"] = ["funded_standard", 2]

    with pytest.raises(ValidationError) as excinfo:
        VenueProfile.model_validate(data)

    assert [error["loc"] for error in excinfo.value.errors()] == [("news_rule", "applies_to", 1)]


@pytest.mark.parametrize(
    ("verified", "state", "age_days", "locked"),
    [
        (None, RulesState.MISSING, None, True),
        (TODAY, RulesState.FRESH, 0, False),
        (TODAY - dt.timedelta(days=1), RulesState.FRESH, 1, False),
        (TODAY - dt.timedelta(days=30), RulesState.FRESH, 30, False),
        (TODAY - dt.timedelta(days=31), RulesState.STALE, 31, True),
        (TODAY - dt.timedelta(days=400), RulesState.STALE, 400, True),
        # One day ahead is tolerated: the user's local date can lead the UTC date.
        (TODAY + dt.timedelta(days=1), RulesState.FRESH, 0, False),
        # Further ahead is a typo and must not unlock anything.
        (TODAY + dt.timedelta(days=2), RulesState.FUTURE, -2, True),
        (TODAY + dt.timedelta(days=365), RulesState.FUTURE, -365, True),
    ],
)
def test_rules_freshness(
    verified: dt.date | None, state: RulesState, age_days: int | None, locked: bool
) -> None:
    venue = _valid_profile().model_copy(update={"rules_verified_at": verified})

    freshness = rules_freshness(venue, TODAY)

    assert freshness.state is state
    assert freshness.age_days == age_days
    assert freshness.order_modes_locked is locked


def test_rules_max_age_is_thirty_days() -> None:
    # CLAUDE.md invariant 1 / SPEC §8.1.
    assert RULES_MAX_AGE_DAYS == 30
