"""Schema of `config/backtest.yaml`: instruments, costs, samples, Go/No-Go thresholds.

Fail-closed like the other config files (ADR-0003): unknown keys, wrong types and
out-of-range values are load errors. Two rules are specific to this file:

- An instrument profile may hold `null` for a value that still has to be read from the
  trading terminal (VERIFY). Such a profile loads, but it cannot be turned into an
  `Instrument` or a `CostModel`: a backtest on guessed contract values would look like
  evidence and be none.
- The Go/No-Go thresholds of SPEC §9 are floors in code. The file can make a gate
  stricter, never looser, so an edit cannot quietly lower the bar for a paid challenge.
"""

import datetime as dt
import itertools
import re
from collections.abc import Mapping
from pathlib import Path
from types import MappingProxyType
from typing import Annotated, Final, Self
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from pydantic import AfterValidator, Field, ValidationError, model_validator
from pydantic_core import PydanticCustomError

from propbot.backtest.model import CostModel, SpreadWindow
from propbot.core.instrument import Instrument, InstrumentKind
from propbot.risk.config import StrictModel
from propbot.risk.errors import ConfigError, ConfigIssue, issues_from_validation_error
from propbot.risk.loader import read_yaml_mapping

BACKTEST_FILE_NAME: Final = "backtest.yaml"

# SPEC §9. The YAML may only be stricter than these.
MIN_DATA_YEARS: Final = 2.0
REQUIRED_TF_SECONDS: Final = 60
MIN_OOS_T_STAT: Final = 2.0
MIN_OOS_TO_IS_RATIO: Final = 0.5
MIN_P_PASS_ALL_PHASES: Final = 0.60
MAX_P_BREACH: Final = 0.20
MIN_PARITY_RATE: Final = 0.98
# Engineering floors of the Go/No-Go reporter (not SPEC §9 values; they await the user's
# confirmation). Same rule: the YAML may only be stricter.
# The t >= 2 floor reads the t-statistic against the normal distribution; below about 30
# trades that reading breaks down, so a smaller OOS or IS sample cannot support G2 or G3.
MIN_OOS_TRADES: Final = 30
# G1 sanity floor: the regular New York session alone is 6.5 h x ~252 days = ~1,638 h a
# year, so a file below this cannot hold the required years of 1-minute data, whatever
# its first and last timestamps say (two bars two years apart are not two years of data).
MIN_BAR_HOURS_PER_YEAR: Final = 1_500.0
# A Monte Carlo run smaller than these is too coarse to judge a paid challenge on;
# 10,000 paths is the simulator's default (docs/monte-carlo.md).
MIN_MC_PATHS: Final = 5_000
MIN_MC_TRADES: Final = 100
# An artifact older than this (days, relative to the report date) cannot support a PASS:
# the numbers may describe code, data or rules that have changed since. A MAXIMUM: the
# YAML may only make it smaller.
MAX_ARTIFACT_AGE_DAYS: Final = 30
# G6: a match rate needs enough compared trades to mean anything. With n compared trades
# and none differing, the 95 % one-sided lower bound on the true match rate is about
# 1 - 3/n ("rule of three"): 94 % at 50 trades (the least that tolerates one mismatch at
# 98 %), 97 % at 100, so 100 is the smallest count whose bound is near the 98 % floor.
MIN_PARITY_TRADES: Final = 100

_HHMM: Final = re.compile(r"([01]\d|2[0-3]):([0-5]\d)")
_ISO_DATE: Final = re.compile(r"\d{4}-\d{2}-\d{2}")
_INSTRUMENT_NAME: Final = re.compile(r"[A-Za-z0-9_.:!-]{1,64}")

NonNegative = Annotated[float, Field(ge=0)]
Positive = Annotated[float, Field(gt=0)]


class BacktestConfigError(Exception):
    """The configuration cannot be used for this run. The message is user-facing (Turkish)."""


def _check_zone(value: str) -> str:
    try:
        ZoneInfo(value)
    except (ZoneInfoNotFoundError, ValueError) as exc:
        raise PydanticCustomError("zone", "bilinmeyen saat dilimi (IANA adı olmalı)") from exc
    return value


def _check_hhmm(value: str) -> str:
    if _HHMM.fullmatch(value) is None:
        raise PydanticCustomError("hhmm", "saat SS:DD biçiminde olmalı (ör. 09:30)")
    return value


def _check_iso_date(value: str) -> str:
    if _ISO_DATE.fullmatch(value) is not None:
        try:
            dt.date.fromisoformat(value)
        except ValueError:
            pass
        else:
            return value
    raise PydanticCustomError("date_format", "tarih YYYY-AA-GG biçiminde olmalı")


Zone = Annotated[str, AfterValidator(_check_zone)]
ClockTime = Annotated[str, AfterValidator(_check_hhmm)]
IsoDate = Annotated[str, AfterValidator(_check_iso_date)]


def _minute_of_day(value: str) -> int:
    hours, minutes = value.split(":")
    return int(hours) * 60 + int(minutes)


class SpreadWindowConfig(StrictModel):
    start: ClockTime  # inclusive, wall clock of `session.tz`
    end: ClockTime  # exclusive; may wrap midnight
    spread_points: NonNegative

    @model_validator(mode="after")
    def _not_empty(self) -> Self:
        if self.start == self.end:
            raise PydanticCustomError("spread_window", "başlangıç ve bitiş aynı olamaz")
        return self


class InstrumentCosts(StrictModel):
    commission_per_unit_side: NonNegative | None
    slippage_ticks: NonNegative | None
    spread_points: NonNegative | None
    spread_windows: tuple[SpreadWindowConfig, ...] = Field(strict=False)


class InstrumentProfile(StrictModel):
    kind: InstrumentKind = Field(strict=False)
    tick_size: Positive | None
    point_value: Positive | None
    qty_step: Positive | None
    min_qty: Positive | None
    costs: InstrumentCosts

    def unresolved_fields(self) -> tuple[str, ...]:
        """Dotted names of the values that are still `null` (to be read from the terminal)."""
        missing = [
            name
            for name in ("tick_size", "point_value", "qty_step", "min_qty")
            if getattr(self, name) is None
        ]
        missing.extend(
            f"costs.{name}"
            for name in ("commission_per_unit_side", "slippage_ticks", "spread_points")
            if getattr(self.costs, name) is None
        )
        return tuple(missing)


class SessionConfig(StrictModel):
    tz: Zone
    min_gap_minutes: int = Field(ge=1, le=24 * 60)


class SamplesConfig(StrictModel):
    oos_start: IsoDate

    @property
    def oos_start_utc(self) -> dt.datetime:
        """Midnight UTC of the date, as the Pine inputs write it ("01 Jan 2025 00:00 +0000")."""
        return dt.datetime.combine(dt.date.fromisoformat(self.oos_start), dt.time(), tzinfo=dt.UTC)


class WalkForwardSettings(StrictModel):
    is_days: int = Field(ge=1)
    oos_days: int = Field(ge=1)
    step_days: int = Field(ge=1)

    @model_validator(mode="after")
    def _no_overlap(self) -> Self:
        if self.step_days < self.oos_days:
            raise PydanticCustomError(
                "walk_forward",
                "step_days, oos_days'ten küçük olamaz (bir işlem iki OOS penceresinde sayılırdı)",
            )
        return self


class GoNoGoCriteria(StrictModel):
    min_data_years: float = Field(ge=MIN_DATA_YEARS)
    required_tf_seconds: int = Field(ge=1, le=REQUIRED_TF_SECONDS)
    min_oos_t_stat: float = Field(ge=MIN_OOS_T_STAT)
    min_oos_to_is_ratio: float = Field(ge=MIN_OOS_TO_IS_RATIO)
    min_p_pass_all_phases: float = Field(ge=MIN_P_PASS_ALL_PHASES, le=1)
    max_p_breach: float = Field(ge=0, le=MAX_P_BREACH)
    min_parity_rate: float = Field(ge=MIN_PARITY_RATE, le=1)
    # Engineering floors (see the constants above). The defaults ARE the floors, so a
    # caller that does not name them gets exactly the code's values, never a looser one.
    min_oos_trades: int = Field(default=MIN_OOS_TRADES, ge=MIN_OOS_TRADES)
    min_bar_hours_per_year: float = Field(
        default=MIN_BAR_HOURS_PER_YEAR, ge=MIN_BAR_HOURS_PER_YEAR, le=24 * 366
    )
    min_mc_paths: int = Field(default=MIN_MC_PATHS, ge=MIN_MC_PATHS)
    min_mc_trades: int = Field(default=MIN_MC_TRADES, ge=MIN_MC_TRADES)
    max_artifact_age_days: int = Field(
        default=MAX_ARTIFACT_AGE_DAYS, ge=0, le=MAX_ARTIFACT_AGE_DAYS
    )
    min_parity_trades: int = Field(default=MIN_PARITY_TRADES, ge=MIN_PARITY_TRADES)


def _check_factors(value: tuple[float, ...]) -> tuple[float, ...]:
    if not value or value[0] != 1.0:
        raise PydanticCustomError("cost_factors", "ilk çarpan 1.0 olmalı (temel maliyet)")
    if any(later <= earlier for earlier, later in itertools.pairwise(value)):
        raise PydanticCustomError("cost_factors", "çarpanlar kesin artan sırada olmalı")
    return value


def _check_instruments(
    value: Mapping[str, InstrumentProfile],
) -> Mapping[str, InstrumentProfile]:
    if not value:
        raise PydanticCustomError("instruments", "en az bir enstrüman profili gerekli")
    for name in value:
        if _INSTRUMENT_NAME.fullmatch(name) is None:
            raise PydanticCustomError(
                "instruments",
                "enstrüman adı 1-64 karakter olmalı (harf, rakam ve _ . : ! -)",
            )
    return MappingProxyType(dict(value))


class BacktestConfig(StrictModel):
    default_instrument: str
    instruments: Annotated[Mapping[str, InstrumentProfile], AfterValidator(_check_instruments)]
    session: SessionConfig
    samples: SamplesConfig
    walk_forward: WalkForwardSettings
    cost_factors: Annotated[
        tuple[Annotated[float, Field(gt=0, le=10)], ...],
        Field(strict=False, max_length=8),
        AfterValidator(_check_factors),
    ]
    go_no_go: GoNoGoCriteria

    @model_validator(mode="after")
    def _default_is_listed(self) -> Self:
        if self.default_instrument not in self.instruments:
            raise PydanticCustomError(
                "default_instrument", "default_instrument, instruments altında tanımlı olmalı"
            )
        return self

    def profile(self, name: str | None) -> tuple[str, InstrumentProfile]:
        """The named profile (the default one for None), refusing one with open VERIFY values."""
        chosen = self.default_instrument if name is None else name
        profile = self.instruments.get(chosen)
        if profile is None:
            known = ", ".join(sorted(self.instruments))
            raise BacktestConfigError(
                f"enstrüman profili yok: {chosen} (config/{BACKTEST_FILE_NAME} içinde tanımlılar: "
                f"{known})"
            )
        missing = profile.unresolved_fields()
        if missing:
            raise BacktestConfigError(
                f"{chosen} profili kullanılamıyor: şu değerler boş (VERIFY — işlem terminalinden "
                f"okunup config/{BACKTEST_FILE_NAME} dosyasına yazılmalı): {', '.join(missing)}"
            )
        return chosen, profile

    def instrument(self, name: str | None = None) -> Instrument:
        chosen, profile = self.profile(name)
        # `profile` refused every None above; the asserts tell the type checker so.
        assert profile.tick_size is not None
        assert profile.point_value is not None
        assert profile.qty_step is not None
        assert profile.min_qty is not None
        return Instrument(
            symbol=chosen,
            kind=profile.kind,
            tick_size=profile.tick_size,
            point_value=profile.point_value,
            qty_step=profile.qty_step,
            min_qty=profile.min_qty,
        )

    def cost_model(self, name: str | None = None) -> CostModel:
        _chosen, profile = self.profile(name)
        costs = profile.costs
        assert costs.commission_per_unit_side is not None
        assert costs.slippage_ticks is not None
        assert costs.spread_points is not None
        return CostModel(
            commission_per_unit_side=costs.commission_per_unit_side,
            slippage_ticks=costs.slippage_ticks,
            spread_points=costs.spread_points,
            spread_windows=tuple(
                SpreadWindow(
                    start_minute=_minute_of_day(window.start),
                    end_minute=_minute_of_day(window.end),
                    spread_points=window.spread_points,
                )
                for window in costs.spread_windows
            ),
            spread_tz=self.session.tz,
        )


def load_backtest_config(path: Path) -> BacktestConfig:
    """Read and validate the file; every problem is reported at once as a `ConfigError`."""
    data = read_yaml_mapping(path)
    try:
        return BacktestConfig.model_validate(data)
    except ValidationError as exc:
        raise ConfigError(issues_from_validation_error(exc, str(path))) from exc


def unresolved_instruments(config: BacktestConfig) -> list[ConfigIssue]:
    """One note per instrument profile that still has VERIFY values (for `config check`)."""
    return [
        ConfigIssue(
            f"config/{BACKTEST_FILE_NAME}",
            f"instruments.{name}",
            "boş (VERIFY) değerler: " + ", ".join(profile.unresolved_fields()),
        )
        for name, profile in sorted(config.instruments.items())
        if profile.unresolved_fields()
    ]
