"""TradingView alert payload: strict decoding and validation (SPEC §5.1, schema v1).

The body is hostile until proven otherwise: `decode_json_object` accepts exactly one JSON
object and nothing ambiguous (no duplicate keys, no NaN/Infinity, no number that overflows,
no text that cannot be stored as UTF-8), `pop_token` hands the shared secret to the caller,
and `validate_alert` checks the rest against `TvAlert`.

Nothing raised here carries a value from the request: `AlertError` holds an error code and
the NAMES of the offending fields only, and it is never chained to the parser's or
pydantic's own exception (those keep the rejected input, which may contain the token).
"""

import json
import math
import re
from collections.abc import Iterable, Mapping
from enum import StrEnum
from types import MappingProxyType
from typing import Annotated, Final, Literal, Self

from pydantic import (
    AfterValidator,
    BaseModel,
    BeforeValidator,
    ConfigDict,
    Field,
    ValidationError,
    model_validator,
)
from pydantic_core import PydanticCustomError

from propbot.core.timeframe import TimeframeError, timeframe_seconds

SCHEMA_VERSION: Final = 1
HEARTBEAT_STRATEGY_ID: Final = "heartbeat"

# strategy_id -> display name used in Telegram messages.
STRATEGY_NAMES: Final[Mapping[str, str]] = MappingProxyType(
    {
        "ny_orb_fvg": "NY ORB FVG",
        "asian_sweep": "Asian Sweep",
        "ict_silver_bullet": "ICT Silver Bullet",
        "mr_bb_kc_rsi2": "Mean Reversion BB/KC",
        "mtf_trend_cont": "MTF Trend",
    }
)
SETUPS: Final = frozenset({"FVG", "IFVG", "SWEEP_FVG", "SB_FVG", "BB_KC_RSI2", "MTF_PULLBACK"})

# bar_time sanity window (epoch ms, UTC): 2020-01-01 inclusive to 2100-01-01 exclusive.
# It catches seconds sent instead of milliseconds and other unit mistakes.
BAR_TIME_MIN_MS: Final = 1_577_836_800_000
BAR_TIME_MAX_MS: Final = 4_102_444_800_000

# Reported in `AlertError.fields` in place of the name of a key the schema does not know:
# an unknown key name is attacker-chosen text and is never repeated.
UNKNOWN_KEY_FIELD: Final = "unknown_key"

_SYMBOL: Final = re.compile(r"[A-Za-z0-9_.:!-]{1,64}")
_REASON: Final = re.compile(r"[a-z_]{1,32}")
_MILLIS_PER_SECOND: Final = 1000
# Type of the pydantic error raised by the cross-field rules; its ctx names the fields.
_RULE_ERROR: Final = "alert_rule"
_RULE_FIELDS_SEPARATOR: Final = ","


class Action(StrEnum):
    ENTRY = "entry"
    STOP_UPDATE = "stop_update"
    EXIT = "exit"
    CANCEL = "cancel"
    HEARTBEAT = "heartbeat"


class Side(StrEnum):
    LONG = "long"
    SHORT = "short"


class OrderType(StrEnum):
    MARKET = "market"
    LIMIT = "limit"


class AlertErrorCode(StrEnum):
    BAD_JSON = "bad_json"
    BAD_SCHEMA = "bad_schema"


# Validators raise value-free errors: the messages are internal (callers see only
# `AlertError.code` and `.fields`), and none of them repeats the rejected value.


def _require_plain_int(value: object) -> object:
    # pydantic's Literal[1] also matches True and 1.0 (they compare equal to 1).
    if type(value) is not int:
        raise PydanticCustomError("alert_version_type", "schema version must be an integer")
    return value


def _check_symbol(value: str) -> str:
    if _SYMBOL.fullmatch(value) is None:
        raise PydanticCustomError("alert_symbol", "symbol has unsupported characters or length")
    return value


def _check_timeframe(value: str) -> str:
    try:
        timeframe_seconds(value)
    except TimeframeError:
        raise PydanticCustomError("alert_timeframe", "unsupported timeframe") from None
    return value


def _check_setup(value: str) -> str:
    if value and value not in SETUPS:
        raise PydanticCustomError("alert_setup", "unknown setup")
    return value


def _check_reason(value: str) -> str:
    if _REASON.fullmatch(value) is None:
        raise PydanticCustomError("alert_reason", "reason has unsupported characters or length")
    return value


_Price = Annotated[float, Field(gt=0)]


class TvAlert(BaseModel):
    """One validated TradingView alert, without the token.

    Strict: no type coercion (a JSON integer is accepted where a float is expected, a
    boolean or a numeric string never is), no unknown keys, no NaN/Infinity, immutable.
    The enum fields are the only lax ones, so that they accept their wire strings.
    """

    model_config = ConfigDict(
        strict=True,
        extra="forbid",
        frozen=True,
        allow_inf_nan=False,
        hide_input_in_errors=True,
    )

    v: Annotated[Literal[1], BeforeValidator(_require_plain_int)]
    strategy_id: str
    symbol: Annotated[str, AfterValidator(_check_symbol)]
    tf: Annotated[str, AfterValidator(_check_timeframe)]
    # Bar OPEN, epoch ms.
    bar_time: int = Field(ge=BAR_TIME_MIN_MS, lt=BAR_TIME_MAX_MS)
    action: Action = Field(strict=False)
    side: Side | None = Field(default=None, strict=False)
    setup: Annotated[str, AfterValidator(_check_setup)] = ""
    order_type: OrderType | None = Field(default=None, strict=False)
    est_entry: _Price | None = None
    stop: _Price | None = None
    tp: _Price | None = None
    # Informational only, never trusted: use `stop_distance`.
    stop_dist_pts: Annotated[float, Field(ge=0)] | None = None
    # Informational only. Zero is accepted: Pine prints the computed ratio with two decimals
    # ("#.##"), so a target one tick away from the entry arrives as 0.
    rr: Annotated[float, Field(ge=0)] | None = None
    # Optional v1 extension: why a forced exit happened.
    reason: Annotated[str, AfterValidator(_check_reason)] | None = None

    @model_validator(mode="after")
    def _check_action_rules(self) -> Self:
        offending = _rule_violations(self)
        if offending:
            raise PydanticCustomError(
                _RULE_ERROR,
                "fields do not fit the action: {fields}",
                {"fields": _RULE_FIELDS_SEPARATOR.join(sorted(offending))},
            )
        return self

    @property
    def bar_seconds(self) -> int:
        """Bar length in seconds."""
        return timeframe_seconds(self.tf)

    @property
    def bar_close_ms(self) -> int:
        """Nominal bar close, epoch ms (the alert fires at this instant)."""
        return self.bar_time + self.bar_seconds * _MILLIS_PER_SECOND

    @property
    def stop_distance(self) -> float | None:
        """Entry-to-stop distance in price points, computed here rather than read from the alert."""
        if self.est_entry is None or self.stop is None:
            return None
        return abs(self.est_entry - self.stop)


_ALERT_FIELD_NAMES: Final = frozenset(TvAlert.model_fields)
# A heartbeat carries no trade data at all.
_HEARTBEAT_ABSENT: Final = (
    "side",
    "order_type",
    "est_entry",
    "stop",
    "tp",
    "stop_dist_pts",
    "rr",
    "reason",
)


def _rule_violations(alert: TvAlert) -> set[str]:
    """Names of the fields that break a cross-field rule (empty when the alert is consistent)."""
    is_heartbeat = alert.action is Action.HEARTBEAT
    if is_heartbeat != (alert.strategy_id == HEARTBEAT_STRATEGY_ID):
        return {"action", "strategy_id"}
    if is_heartbeat:
        offending = {name for name in _HEARTBEAT_ABSENT if getattr(alert, name) is not None}
        if alert.setup:
            offending.add("setup")
        return offending

    offending = set()
    if alert.strategy_id not in STRATEGY_NAMES:
        offending.add("strategy_id")
    if alert.side is None:
        offending.add("side")
    if alert.action is Action.ENTRY:
        if not alert.setup:
            offending.add("setup")
        if alert.order_type is None:
            offending.add("order_type")
        offending.update(_missing_levels(alert))
        offending.update(_entry_geometry_violations(alert))
    elif alert.action is Action.STOP_UPDATE:
        # No geometry rule here: a trailed stop may sit at or beyond the entry price.
        offending.update(_missing_levels(alert))
    return offending


def _missing_levels(alert: TvAlert) -> set[str]:
    return {name for name in ("est_entry", "stop") if getattr(alert, name) is None}


def _entry_geometry_violations(alert: TvAlert) -> set[str]:
    """Stop on the loss side of the entry, target (when given) on the profit side."""
    entry, stop, tp, side = alert.est_entry, alert.stop, alert.tp, alert.side
    if entry is None or stop is None:
        return set()
    offending: set[str] = set()
    if stop == entry:
        offending.add("stop")
    if side is Side.LONG:
        if stop > entry:
            offending.add("stop")
        if tp is not None and tp <= entry:
            offending.add("tp")
    elif side is Side.SHORT:
        if stop < entry:
            offending.add("stop")
        if tp is not None and tp >= entry:
            offending.add("tp")
    return offending


class AlertError(Exception):
    """A webhook body that is not a valid alert.

    `fields` holds the names of the offending `TvAlert` fields, sorted and unique. Any
    other name collapses to `UNKNOWN_KEY_FIELD`, so neither a value nor an attacker-chosen
    key name can travel with the error into a log line or a Telegram message.
    """

    def __init__(self, code: AlertErrorCode, fields: Iterable[str] = ()) -> None:
        self.code: AlertErrorCode = code
        self.fields: tuple[str, ...] = tuple(
            sorted({name if name in _ALERT_FIELD_NAMES else UNKNOWN_KEY_FIELD for name in fields})
        )
        detail = f": {', '.join(self.fields)}" if self.fields else ""
        super().__init__(f"{code.value}{detail}")


def _unique_object(pairs: list[tuple[str, object]]) -> dict[str, object]:
    obj = dict(pairs)
    if len(obj) != len(pairs):
        raise ValueError("duplicate key")
    return obj


def _reject_constant(_name: str) -> object:
    # Called by the json module for NaN, Infinity and -Infinity.
    raise ValueError("non-finite constant")


def _finite_float(text: str) -> float:
    value = float(text)
    if not math.isfinite(value):
        # 1e400 is valid JSON syntax but overflows to infinity.
        raise ValueError("number out of range")
    return value


def _loads(raw: bytes) -> object:
    """Parse strictly; `None` for anything that is not acceptable JSON (and for JSON null)."""
    try:
        text = raw.decode("utf-8")
        obj: object = json.loads(
            text,
            object_pairs_hook=_unique_object,
            parse_constant=_reject_constant,
            parse_float=_finite_float,
        )
        # An unpaired surrogate escape ("\ud800") parses, but the resulting text can never
        # be encoded as UTF-8 again (token comparison, SQLite, logs): refuse it here.
        json.dumps(obj, ensure_ascii=False).encode("utf-8")
    except (ValueError, RecursionError):
        # ValueError covers JSONDecodeError, UnicodeDecodeError/EncodeError, the hooks above
        # and Python's limit on integer digits; RecursionError is deep nesting.
        return None
    return obj


def decode_json_object(raw: bytes) -> dict[str, object]:
    """Decode a request body into one JSON object, or raise `AlertError(BAD_JSON)`.

    Strict UTF-8 (a BOM is an error), top level must be an object, duplicate keys are
    rejected at every depth, as are NaN/Infinity, numbers that overflow a float, unpaired
    surrogates and nesting deep enough to exhaust the parser's recursion.
    """
    obj = _loads(raw)
    # Raised outside `_loads`' handler on purpose: JSONDecodeError keeps the whole document
    # (token included) in `.doc`, and it must not ride along as `__context__`.
    if not isinstance(obj, dict):
        raise AlertError(AlertErrorCode.BAD_JSON)
    return obj


def pop_token(obj: dict[str, object]) -> str | None:
    """Remove "token" from the decoded body; return it when it is a string, else `None`."""
    token = obj.pop("token", None)
    return token if isinstance(token, str) else None


def _offending_fields(exc: ValidationError) -> set[str]:
    names: set[str] = set()
    for error in exc.errors(include_url=False, include_input=False):
        location = error["loc"]
        if location:
            names.add(str(location[0]))
        elif error["type"] == _RULE_ERROR:
            names.update(str(error.get("ctx", {})["fields"]).split(_RULE_FIELDS_SEPARATOR))
    return names


def validate_alert(obj: Mapping[str, object]) -> TvAlert:
    """Validate a decoded body whose token was already popped, or raise `AlertError(BAD_SCHEMA)`.

    A "token" key that is still present is an unknown key like any other.
    """
    try:
        return TvAlert.model_validate(dict(obj))
    except ValidationError as exc:
        fields = _offending_fields(exc)
    # Raised outside the handler: ValidationError.errors() exposes the rejected values.
    raise AlertError(AlertErrorCode.BAD_SCHEMA, fields)


def canonical_json(alert: TvAlert) -> str:
    """Stable JSON text of an alert: every field, sorted keys, compact, ASCII, no token.

    `validate_alert(json.loads(canonical_json(alert)))` returns an equal alert.
    """
    return json.dumps(
        alert.model_dump(mode="json"), sort_keys=True, separators=(",", ":"), allow_nan=False
    )
