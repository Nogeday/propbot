"""Venue (prop firm) profile schema: `config/venues/*.yaml`.

Firm rule values live only in the YAML profile, never in code (CLAUDE.md invariant 10).
`null` is accepted only for fields that are still to be verified on the firm's
official pages or that are read from the trading terminal at runtime; those are
reported by `unresolved_fields`.
"""

import datetime as dt
import re
from dataclasses import dataclass
from enum import StrEnum
from typing import Annotated, Final, Self
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from pydantic import AfterValidator, BeforeValidator, Field, model_validator
from pydantic_core import PydanticCustomError

from propbot.risk.config import Percent, PositiveInt, StrictModel

# CLAUDE.md invariant 1 / SPEC §8.1: rules older than this lock every mode except signal/paper.
RULES_MAX_AGE_DAYS: Final = 30
# Slack between the date the user writes (local) and the UTC date the bot compares against.
RULES_FUTURE_TOLERANCE_DAYS: Final = 1

_HHMM = re.compile(r"([01]\d|2[0-3]):[0-5]\d")
_ISO_DATE = re.compile(r"\d{4}-\d{2}-\d{2}")


class Platform(StrEnum):
    MT5 = "mt5"


class MaxLossType(StrEnum):
    STATIC = "static"
    TRAILING_EOD = "trailing_eod"


class DailyLossBasis(StrEnum):
    MAX_BALANCE_EQUITY_AT_RESET = "max_balance_equity_at_reset"
    BALANCE_AT_RESET = "balance_at_reset"


def _check_zone(value: str) -> str:
    try:
        ZoneInfo(value)
    except (ZoneInfoNotFoundError, ValueError) as exc:
        raise PydanticCustomError(
            "unknown_time_zone", "bilinmeyen saat dilimi: {value}", {"value": repr(value)}
        ) from exc
    return value


def _require_text_time(value: object) -> object:
    # Unquoted 22:30 is a sexagesimal integer (1350) in YAML 1.1, so insist on a quoted string.
    if not isinstance(value, str):
        raise PydanticCustomError(
            "reset_time_not_text",
            'tırnak içinde "HH:MM" olmalı (YAML tırnaksız 22:30 gibi değerleri sayıya çevirir);'
            " okunan: {value}",
            {"value": repr(value)},
        )
    return value


def _check_hhmm(value: str) -> str:
    if _HHMM.fullmatch(value) is None:
        raise PydanticCustomError(
            "reset_time_format",
            '"HH:MM" biçiminde olmalı (00:00-23:59); okunan: {value}',
            {"value": repr(value)},
        )
    return value


def _parse_plain_date(value: object) -> object:
    # YAML yields a date for an unquoted 2026-11-01 and a string for a quoted one; accept both.
    if isinstance(value, dt.datetime):
        raise PydanticCustomError(
            "date_with_time", "yalnızca tarih olmalı (YYYY-AA-GG), saat içermemeli"
        )
    if isinstance(value, dt.date):
        return value
    if isinstance(value, str) and _ISO_DATE.fullmatch(value) is not None:
        try:
            return dt.date.fromisoformat(value)
        except ValueError:
            pass
    raise PydanticCustomError(
        "date_format",
        "YYYY-AA-GG biçiminde geçerli bir tarih olmalı; okunan: {value}",
        {"value": repr(value)},
    )


NonEmptyStr = Annotated[str, Field(min_length=1)]
ZoneName = Annotated[str, AfterValidator(_check_zone)]
ResetTime = Annotated[str, BeforeValidator(_require_text_time), AfterValidator(_check_hhmm)]
PlainDate = Annotated[dt.date, BeforeValidator(_parse_plain_date)]
PositiveFloat = Annotated[float, Field(gt=0)]


class ProfitTargets(StrictModel):
    """Profit target per phase, % of initial balance. `phase2` is null for single-phase programs."""

    phase1: Percent
    phase2: Percent | None


class DailyReset(StrictModel):
    """When the firm's trading day rolls over."""

    tz: ZoneName
    time: ResetTime

    @property
    def zone(self) -> ZoneInfo:
        return ZoneInfo(self.tz)

    @property
    def time_of_day(self) -> dt.time:
        return dt.time.fromisoformat(self.time)


class NewsRule(StrictModel):
    applies_to: Annotated[tuple[NonEmptyStr, ...], Field(strict=False)]
    window_minutes: int = Field(ge=0)


class ContractSpec(StrictModel):
    """Symbol properties: null in the profile, read from the terminal at runtime (SPEC §8.3)."""

    tick_size: PositiveFloat | None
    tick_value_per_lot: PositiveFloat | None
    min_lot: PositiveFloat | None
    lot_step: PositiveFloat | None


class VenueProfile(StrictModel):
    """Every key is required; nullable keys must still be present so a deleted line is noticed."""

    venue: NonEmptyStr
    program: NonEmptyStr
    rules_verified_at: PlainDate | None
    platform: Annotated[Platform, Field(strict=False)]
    symbol: NonEmptyStr
    initial_balance: PositiveFloat
    account_currency: str = Field(pattern=r"^[A-Z]{3}$")
    profit_target_pct: ProfitTargets
    daily_loss_limit_pct: Percent
    max_loss_pct: Percent
    max_loss_type: Annotated[MaxLossType, Field(strict=False)]
    daily_reset: DailyReset
    daily_loss_basis: Annotated[DailyLossBasis, Field(strict=False)]
    min_trading_days: Annotated[int, Field(ge=0)] | None
    news_rule: NewsRule
    automation_allowed: bool
    firm_request_threshold_per_day: PositiveInt
    server_tz: ZoneName | None
    contract: ContractSpec

    @model_validator(mode="after")
    def _daily_limit_below_max_loss(self) -> Self:
        if not self.daily_loss_limit_pct < self.max_loss_pct:
            raise PydanticCustomError(
                "daily_limit_not_below_max_loss",
                "daily_loss_limit_pct ({daily}) max_loss_pct değerinden ({total}) küçük olmalı",
                {"daily": self.daily_loss_limit_pct, "total": self.max_loss_pct},
            )
        return self


class UnresolvedKind(StrEnum):
    VERIFY = "verify"  # must be checked on the firm's official pages by the user
    RUNTIME = "runtime"  # read from the trading terminal when the bridge connects


@dataclass(frozen=True, slots=True)
class UnresolvedField:
    path: str
    kind: UnresolvedKind


_VERIFY_FIELDS: Final = ("rules_verified_at", "min_trading_days", "server_tz")
_RUNTIME_CONTRACT_FIELDS: Final = ("tick_size", "tick_value_per_lot", "min_lot", "lot_step")


def unresolved_fields(venue: VenueProfile) -> tuple[UnresolvedField, ...]:
    """List the profile fields that are still null, in file order."""
    found = [
        UnresolvedField(name, UnresolvedKind.VERIFY)
        for name in _VERIFY_FIELDS
        if getattr(venue, name) is None
    ]
    found += [
        UnresolvedField(f"contract.{name}", UnresolvedKind.RUNTIME)
        for name in _RUNTIME_CONTRACT_FIELDS
        if getattr(venue.contract, name) is None
    ]
    return tuple(found)


class RulesState(StrEnum):
    MISSING = "missing"  # rules_verified_at is null
    STALE = "stale"  # verified more than RULES_MAX_AGE_DAYS ago
    FUTURE = "future"  # date lies in the future: a typo must not unlock anything
    FRESH = "fresh"


@dataclass(frozen=True, slots=True)
class RulesFreshness:
    state: RulesState
    age_days: int | None

    @property
    def order_modes_locked(self) -> bool:
        """True unless the rules are fresh: only `signal` and `paper` may run (SPEC §8.1)."""
        return self.state is not RulesState.FRESH


def rules_freshness(venue: VenueProfile, today: dt.date) -> RulesFreshness:
    """Classify `rules_verified_at` against `today` (the caller passes the UTC date)."""
    verified = venue.rules_verified_at
    if verified is None:
        return RulesFreshness(RulesState.MISSING, None)
    age_days = (today - verified).days
    if age_days < -RULES_FUTURE_TOLERANCE_DAYS:
        return RulesFreshness(RulesState.FUTURE, age_days)
    if age_days > RULES_MAX_AGE_DAYS:
        return RulesFreshness(RulesState.STALE, age_days)
    return RulesFreshness(RulesState.FRESH, max(age_days, 0))
