"""TradingView alert schema: strict decoding, every validation rule, and value-free errors."""

import json
import traceback
from collections.abc import Callable
from typing import Any

import pytest
from pydantic import ValidationError

from propbot.signals.schema import (
    BAR_TIME_MAX_MS,
    BAR_TIME_MIN_MS,
    HEARTBEAT_STRATEGY_ID,
    SCHEMA_VERSION,
    SETUPS,
    STRATEGY_NAMES,
    UNKNOWN_KEY_FIELD,
    Action,
    AlertError,
    AlertErrorCode,
    OrderType,
    Side,
    TvAlert,
    canonical_json,
    decode_json_object,
    pop_token,
    validate_alert,
)
from support import alerts
from support.alerts import BAR_TIME_MS, SYMBOL, TOKEN, alert_bytes, alert_payload, pine_payloads

# Not acceptable in any field: no symbol, reason or enum value looks like this.
SECRET = "hunter2/SECRET do-not-leak"
MAX_BODY_BYTES = 4096

STRATEGY_KEYS = [
    "v",
    "token",
    "strategy_id",
    "symbol",
    "tf",
    "bar_time",
    "action",
    "side",
    "setup",
    "order_type",
    "est_entry",
    "stop",
    "tp",
    "stop_dist_pts",
    "rr",
]
HEARTBEAT_KEYS = ["v", "token", "strategy_id", "symbol", "tf", "bar_time", "action"]
PINE = pine_payloads()

Builder = Callable[..., dict[str, object]]
ENTRY_BUILDERS: list[Builder] = [
    alerts.ny_orb_fvg_entry,
    alerts.asian_sweep_entry,
    alerts.ict_silver_bullet_entry,
    alerts.mr_bb_kc_rsi2_entry,
    alerts.mtf_trend_cont_entry,
]
STOP_UPDATE_BUILDERS: list[Builder] = [
    alerts.ny_orb_fvg_stop_update,
    alerts.mtf_trend_cont_stop_update,
]
NON_HEARTBEAT_BUILDERS: list[Builder] = [
    *ENTRY_BUILDERS,
    *STOP_UPDATE_BUILDERS,
    alerts.ny_orb_fvg_exit,
    alerts.asian_sweep_exit,
    alerts.ict_silver_bullet_exit,
    alerts.mr_bb_kc_rsi2_exit,
    alerts.mtf_trend_cont_exit,
    alerts.ict_silver_bullet_cancel,
]


def untokened(
    payload: dict[str, object] | None = None, /, **overrides: object
) -> dict[str, object]:
    """A payload as the receiver hands it to `validate_alert`: token popped, overrides applied."""
    body = alert_payload() if payload is None else dict(payload)
    body.pop("token", None)
    body.update(overrides)
    return body


def without(payload: dict[str, object], *keys: str) -> dict[str, object]:
    return {key: value for key, value in payload.items() if key not in keys}


def schema_error(obj: dict[str, object]) -> AlertError:
    with pytest.raises(AlertError) as excinfo:
        validate_alert(obj)
    assert excinfo.value.code is AlertErrorCode.BAD_SCHEMA
    return excinfo.value


def rejected_fields(obj: dict[str, object]) -> tuple[str, ...]:
    return schema_error(obj).fields


def json_error(raw: bytes) -> AlertError:
    with pytest.raises(AlertError) as excinfo:
        decode_json_object(raw)
    assert excinfo.value.code is AlertErrorCode.BAD_JSON
    assert excinfo.value.fields == ()
    return excinfo.value


# ───────────────────────────── constants ─────────────────────────────


def test_constants_match_spec_5_1() -> None:
    assert SCHEMA_VERSION == 1
    assert HEARTBEAT_STRATEGY_ID == "heartbeat"
    assert dict(STRATEGY_NAMES) == {
        "ny_orb_fvg": "NY ORB FVG",
        "asian_sweep": "Asian Sweep",
        "ict_silver_bullet": "ICT Silver Bullet",
        "mr_bb_kc_rsi2": "Mean Reversion BB/KC",
        "mtf_trend_cont": "MTF Trend",
    }
    assert HEARTBEAT_STRATEGY_ID not in STRATEGY_NAMES
    assert frozenset({"FVG", "IFVG", "SWEEP_FVG", "SB_FVG", "BB_KC_RSI2", "MTF_PULLBACK"}) == SETUPS
    assert [action.value for action in Action] == [
        "entry",
        "stop_update",
        "exit",
        "cancel",
        "heartbeat",
    ]
    assert [side.value for side in Side] == ["long", "short"]
    assert [order_type.value for order_type in OrderType] == ["market", "limit"]
    assert [code.value for code in AlertErrorCode] == ["bad_json", "bad_schema"]


def test_strategy_names_are_read_only() -> None:
    names: Any = STRATEGY_NAMES
    with pytest.raises(TypeError):
        names["rogue"] = "Rogue"


def test_bar_time_window_is_2020_to_2100_utc() -> None:
    assert BAR_TIME_MIN_MS == 1_577_836_800_000  # 2020-01-01T00:00:00Z
    assert BAR_TIME_MAX_MS == 4_102_444_800_000  # 2100-01-01T00:00:00Z


# ───────────────────────────── real Pine payloads ─────────────────────────────


def test_reference_payload_is_the_spec_example() -> None:
    assert alert_payload() == {
        "v": 1,
        "token": TOKEN,
        "strategy_id": "ny_orb_fvg",
        "symbol": "CME_MINI:MES1!",
        "tf": "1",
        "bar_time": 1784647800000,
        "action": "entry",
        "side": "long",
        "setup": "FVG",
        "order_type": "market",
        "est_entry": 5123.25,
        "stop": 5110.75,
        "tp": 5148.25,
        "stop_dist_pts": 12.5,
        "rr": 2,
    }


def test_reference_payload_for_a_short_mirrors_the_levels() -> None:
    payload = alert_payload(side="short", tf="5")

    assert payload == alerts.ny_orb_fvg_entry("short") | {"tf": "5"}
    assert (payload["est_entry"], payload["stop"], payload["tp"]) == (5123.25, 5135.75, 5098.25)
    assert validate_alert(untokened(payload)).side is Side.SHORT


def test_reference_payload_overrides_are_plain_key_replacement() -> None:
    payload = alert_payload(stop=1.0, side=None, extra="x")

    assert payload == alerts.ny_orb_fvg_entry() | {"stop": 1.0, "side": None, "extra": "x"}
    assert payload["stop_dist_pts"] == 12.5


@pytest.mark.parametrize("name", sorted(PINE))
def test_pine_payload_has_the_documented_keys(name: str) -> None:
    expected = HEARTBEAT_KEYS if name.startswith("heartbeat/") else STRATEGY_KEYS
    assert list(PINE[name]) == expected


@pytest.mark.parametrize("name", sorted(PINE))
def test_every_pine_payload_passes_the_whole_pipeline(name: str) -> None:
    raw = alert_bytes(PINE[name])
    assert len(raw) <= MAX_BODY_BYTES

    obj = decode_json_object(raw)
    assert pop_token(obj) == TOKEN
    assert "token" not in obj
    alert = validate_alert(obj)

    strategy_id, action, *_ = name.split("/")
    assert alert.v == 1
    assert alert.strategy_id == strategy_id
    assert alert.action is Action(action)
    assert alert.symbol == SYMBOL
    assert alert.reason is None
    if alert.action is Action.HEARTBEAT:
        assert alert.side is None
        assert alert.setup == ""
    else:
        assert alert.strategy_id in STRATEGY_NAMES
        assert alert.side is Side(name.split("/")[2])


def test_raw_pine_text_with_trailing_zeros_and_nulls_decodes() -> None:
    text = alerts.pine_alert_text(
        strategy_id="ny_orb_fvg",
        action="stop_update",
        side="long",
        setup="",
        order_type="market",
        est_entry=5123.5,
        stop=5120.0,
        tp=None,
        rr="2",
    )
    assert '"stop":5120.00,"tp":null,"stop_dist_pts":3.50,"rr":2}' in text

    obj = decode_json_object(text.encode())
    assert pop_token(obj) == TOKEN
    alert = validate_alert(obj)

    assert alert.stop == 5120.0
    assert alert.tp is None
    assert alert.stop_dist_pts == 3.5
    assert alert.rr == 2.0


def test_entry_fields_are_typed() -> None:
    alert = validate_alert(untokened())

    assert alert.action is Action.ENTRY
    assert alert.side is Side.LONG
    assert alert.order_type is OrderType.MARKET
    assert alert.setup == "FVG"
    assert alert.bar_time == BAR_TIME_MS
    assert (alert.est_entry, alert.stop, alert.tp) == (5123.25, 5110.75, 5148.25)
    # Pine prints rr = 2.0 as the JSON integer 2.
    assert isinstance(alert.rr, float)
    assert alert.rr == 2.0


def test_limit_entry_and_cancel_of_silver_bullet() -> None:
    entry = validate_alert(untokened(alerts.ict_silver_bullet_entry("short")))
    cancel = validate_alert(untokened(alerts.ict_silver_bullet_cancel("short")))

    assert entry.order_type is OrderType.LIMIT
    assert entry.setup == "SB_FVG"
    assert entry.side is Side.SHORT
    assert entry.stop is not None
    assert entry.est_entry is not None
    assert entry.tp is not None
    assert entry.tp < entry.est_entry < entry.stop
    assert cancel.action is Action.CANCEL
    assert (cancel.est_entry, cancel.stop, cancel.tp) == (entry.est_entry, entry.stop, entry.tp)


@pytest.mark.parametrize(
    "builder",
    [
        alerts.ny_orb_fvg_exit,
        alerts.asian_sweep_exit,
        alerts.ict_silver_bullet_exit,
        alerts.mr_bb_kc_rsi2_exit,
        alerts.mtf_trend_cont_exit,
    ],
)
def test_forced_exit_has_a_reference_price_and_no_levels(builder: Builder) -> None:
    alert = validate_alert(untokened(builder("short")))

    assert alert.action is Action.EXIT
    assert alert.side is Side.SHORT
    assert alert.est_entry is not None
    assert alert.stop is None
    assert alert.tp is None
    assert alert.stop_dist_pts is None
    assert alert.stop_distance is None


def test_breakeven_stop_update_has_stop_at_entry_and_no_target() -> None:
    alert = validate_alert(untokened(alerts.mtf_trend_cont_stop_update("long", tp1_filled=True)))

    assert alert.action is Action.STOP_UPDATE
    assert alert.stop == alert.est_entry
    assert alert.stop_dist_pts == 0.0
    assert alert.stop_distance == 0.0
    assert alert.tp is None


@pytest.mark.parametrize("side", ["long", "short"])
def test_computed_rr_that_rounds_to_zero_is_accepted(side: str) -> None:
    # mr_bb_kc_rsi2 clamps the target to one tick from the close; "#.##" then prints 0.
    payload = alerts.mr_bb_kc_rsi2_entry(side, target_one_tick=True)
    assert payload["rr"] == 0

    alert = validate_alert(untokened(payload))

    assert alert.rr == 0.0
    assert alert.tp is not None


# ───────────────────────────── derived properties ─────────────────────────────


@pytest.mark.parametrize(
    ("tf", "seconds"),
    [("1", 60), ("5", 300), ("15", 900), ("240", 14_400), ("1440", 86_400), ("30S", 30)],
)
def test_bar_seconds_and_bar_close(tf: str, seconds: int) -> None:
    alert = validate_alert(untokened(tf=tf))

    assert alert.bar_seconds == seconds
    assert alert.bar_close_ms == BAR_TIME_MS + seconds * 1000


def test_stop_distance_is_computed_not_read_from_the_alert() -> None:
    alert = validate_alert(untokened(stop_dist_pts=0.25))

    assert alert.stop_dist_pts == 0.25
    assert alert.stop_distance == 12.5


def test_stop_distance_is_absolute_for_shorts() -> None:
    alert = validate_alert(untokened(alerts.ny_orb_fvg_entry("short")))

    assert alert.stop_distance == 12.5


@pytest.mark.parametrize("missing", ["est_entry", "stop"])
def test_stop_distance_is_none_without_both_levels(missing: str) -> None:
    alert = validate_alert(untokened(alerts.ny_orb_fvg_exit(), stop=5100.0) | {missing: None})

    assert alert.stop_distance is None


def test_alert_is_immutable() -> None:
    alert = validate_alert(untokened())

    with pytest.raises(ValidationError):
        alert.stop = 1.0


# ───────────────────────────── canonical_json ─────────────────────────────


@pytest.mark.parametrize("name", sorted(PINE))
def test_canonical_json_round_trips(name: str) -> None:
    alert = validate_alert(untokened(PINE[name]))

    text = canonical_json(alert)
    decoded = json.loads(text)

    assert validate_alert(decoded) == alert
    assert decode_json_object(text.encode()) == decoded
    assert canonical_json(validate_alert(decoded)) == text


def test_canonical_json_is_sorted_compact_complete_and_tokenless() -> None:
    text = canonical_json(validate_alert(untokened()))

    assert text == (
        '{"action":"entry","bar_time":1784647800000,"est_entry":5123.25,"order_type":"market",'
        '"reason":null,"rr":2.0,"setup":"FVG","side":"long","stop":5110.75,"stop_dist_pts":12.5,'
        '"strategy_id":"ny_orb_fvg","symbol":"CME_MINI:MES1!","tf":"1","tp":5148.25,"v":1}'
    )
    assert sorted(json.loads(text)) == sorted(TvAlert.model_fields)
    assert "token" not in text
    assert TOKEN not in text
    assert text.isascii()


def test_canonical_json_ignores_input_key_order_and_number_spelling() -> None:
    payload = untokened()
    shuffled = dict(reversed(list(payload.items()))) | {"rr": 2.0, "stop_dist_pts": 12.50}

    assert canonical_json(validate_alert(shuffled)) == canonical_json(validate_alert(payload))


def test_canonical_json_of_a_heartbeat_keeps_every_key() -> None:
    text = canonical_json(validate_alert(untokened(alerts.heartbeat_payload())))

    assert json.loads(text) == {
        "action": "heartbeat",
        "bar_time": BAR_TIME_MS,
        "est_entry": None,
        "order_type": None,
        "reason": None,
        "rr": None,
        "setup": "",
        "side": None,
        "stop": None,
        "stop_dist_pts": None,
        "strategy_id": "heartbeat",
        "symbol": SYMBOL,
        "tf": "15",
        "tp": None,
        "v": 1,
    }


# ───────────────────────────── pop_token ─────────────────────────────


def test_pop_token_returns_and_removes_a_string_token() -> None:
    obj = alert_payload()

    assert pop_token(obj) == TOKEN
    assert "token" not in obj
    assert pop_token(obj) is None


@pytest.mark.parametrize("value", [None, 123, 1.5, True, ["x"], {"token": "x"}])
def test_pop_token_removes_a_non_string_token_and_returns_none(value: object) -> None:
    obj = alert_payload(token=value)

    assert pop_token(obj) is None
    assert "token" not in obj


def test_pop_token_keeps_an_empty_string_for_the_caller_to_refuse() -> None:
    assert pop_token(alert_payload(token="")) == ""


def test_a_token_left_in_the_object_is_a_schema_error() -> None:
    error = schema_error(alert_payload(token=SECRET))

    assert error.fields == (UNKNOWN_KEY_FIELD,)
    assert SECRET not in str(error)


# ───────────────────────────── field types (strict) ─────────────────────────────

NOT_A_NUMBER: list[object] = ["5123.25", "", True, False, [5123.25], {"value": 5123.25}]
NOT_TEXT: list[object] = [1, 1.5, True, None, ["1"], {}]


@pytest.mark.parametrize("value", [True, False, 1.0, "1", 0, 2, -1, None, [1]])
def test_version_must_be_the_integer_1(value: object) -> None:
    assert rejected_fields(untokened(v=value)) == ("v",)


@pytest.mark.parametrize("value", ["1784647800000", 1784647800000.0, 1.5e12, True, None, []])
def test_bar_time_must_be_an_integer(value: object) -> None:
    assert rejected_fields(untokened(bar_time=value)) == ("bar_time",)


@pytest.mark.parametrize("field", ["est_entry", "stop", "tp", "stop_dist_pts", "rr"])
@pytest.mark.parametrize("value", NOT_A_NUMBER)
def test_number_fields_refuse_strings_booleans_and_containers(field: str, value: object) -> None:
    assert rejected_fields(untokened(**{field: value})) == (field,)


@pytest.mark.parametrize("field", ["est_entry", "stop", "tp", "stop_dist_pts", "rr"])
@pytest.mark.parametrize("value", [float("nan"), float("inf"), float("-inf"), 10**400])
def test_number_fields_refuse_non_finite_and_overflowing_values(field: str, value: object) -> None:
    assert rejected_fields(untokened(**{field: value})) == (field,)


def test_json_integers_are_accepted_for_number_fields() -> None:
    alert = validate_alert(untokened(est_entry=5123, stop=5110, tp=5149, stop_dist_pts=13, rr=2))

    assert (alert.est_entry, alert.stop, alert.tp) == (5123.0, 5110.0, 5149.0)
    assert (alert.stop_dist_pts, alert.rr) == (13.0, 2.0)
    assert all(
        isinstance(value, float)
        for value in (alert.est_entry, alert.stop, alert.tp, alert.stop_dist_pts, alert.rr)
    )


@pytest.mark.parametrize("field", ["strategy_id", "symbol", "tf", "setup"])
@pytest.mark.parametrize("value", NOT_TEXT)
def test_text_fields_refuse_everything_but_strings(field: str, value: object) -> None:
    assert rejected_fields(untokened(**{field: value})) == (field,)


@pytest.mark.parametrize("value", [1, True, [], {}, "", "Session End", "session-end", "a" * 33])
def test_reason_must_be_a_short_lowercase_word(value: object) -> None:
    assert rejected_fields(untokened(alerts.ny_orb_fvg_exit(), reason=value)) == ("reason",)


@pytest.mark.parametrize("value", ["session_end", "daily_loss", "end_of_range", "a", "a" * 32])
def test_reason_extension_is_accepted(value: str) -> None:
    alert = validate_alert(untokened(alerts.ny_orb_fvg_exit(), reason=value))

    assert alert.reason == value


@pytest.mark.parametrize("value", ["ENTRY", "Entry", "buy", "close", "", " entry", 1, True, None])
def test_action_must_be_a_wire_string(value: object) -> None:
    assert rejected_fields(untokened(action=value)) == ("action",)


@pytest.mark.parametrize("value", ["LONG", "Long", "buy", "sell", "", "long ", 1, True, ["long"]])
def test_side_must_be_a_wire_string(value: object) -> None:
    assert rejected_fields(untokened(side=value)) == ("side",)


@pytest.mark.parametrize("value", ["MARKET", "stop", "stop_limit", "", 1, True])
def test_order_type_must_be_a_wire_string(value: object) -> None:
    assert rejected_fields(untokened(order_type=value)) == ("order_type",)


@pytest.mark.parametrize("value", ["fvg", "FVG ", "ORB", "SWEEP", "X" * 500])
def test_setup_must_be_a_known_setup(value: str) -> None:
    assert rejected_fields(untokened(setup=value)) == ("setup",)


@pytest.mark.parametrize("setup", sorted(SETUPS))
def test_every_known_setup_is_accepted(setup: str) -> None:
    assert validate_alert(untokened(setup=setup)).setup == setup


# ───────────────────────────── field ranges and formats ─────────────────────────────


@pytest.mark.parametrize("value", [BAR_TIME_MIN_MS, BAR_TIME_MS, BAR_TIME_MAX_MS - 1])
def test_bar_time_inside_the_window(value: int) -> None:
    assert validate_alert(untokened(bar_time=value)).bar_time == value


@pytest.mark.parametrize(
    "value",
    [
        BAR_TIME_MIN_MS - 1,
        BAR_TIME_MAX_MS,
        BAR_TIME_MS // 1000,  # seconds instead of milliseconds
        BAR_TIME_MS * 1000,  # microseconds
        0,
        -BAR_TIME_MS,
        10**400,
        -(10**400),
    ],
)
def test_bar_time_outside_the_window(value: int) -> None:
    assert rejected_fields(untokened(bar_time=value)) == ("bar_time",)


@pytest.mark.parametrize("field", ["est_entry", "stop", "tp"])
@pytest.mark.parametrize("value", [0, 0.0, -0.0, -0.25, -5123.25])
def test_prices_must_be_positive(field: str, value: float) -> None:
    assert rejected_fields(untokened(**{field: value})) == (field,)


@pytest.mark.parametrize("field", ["stop_dist_pts", "rr"])
def test_stop_dist_and_rr_may_be_zero_but_not_negative(field: str) -> None:
    assert getattr(validate_alert(untokened(**{field: 0})), field) == 0.0
    assert rejected_fields(untokened(**{field: -0.01})) == (field,)


@pytest.mark.parametrize(
    "symbol",
    [
        "CME_MINI:MES1!",
        "FTMO:US500.cash",
        "OANDA:SPX500USD",
        "BINANCE:BTC-USDT.P",
        "A",
        "A" * 64,
    ],
)
def test_symbol_accepts_ticker_ids(symbol: str) -> None:
    assert validate_alert(untokened(symbol=symbol)).symbol == symbol


@pytest.mark.parametrize(
    "symbol",
    [
        "",
        "A" * 65,
        "CME MINI:MES1!",
        "MES1!\n",
        "\nMES1!",
        "ES/NQ",
        'MES1!"',
        "MES1!\\",
        "MES<1>",
        "MES1!;DROP",
        "MES₁",
        "MES\x00",
        '={"session":"extended","symbol":"CME_MINI:MES1!"}',
    ],
)
def test_symbol_refuses_other_characters_and_lengths(symbol: str) -> None:
    assert rejected_fields(untokened(symbol=symbol)) == ("symbol",)


@pytest.mark.parametrize("tf", ["1", "5", "15", "60", "240", "1440", "1S", "30S"])
def test_intraday_timeframes_are_accepted(tf: str) -> None:
    assert validate_alert(untokened(tf=tf)).tf == tf


@pytest.mark.parametrize(
    "tf", ["D", "1D", "W", "M", "", "0", "01", " 5", "5m", "1441", "60S", "1T"]
)
def test_other_timeframes_are_refused(tf: str) -> None:
    assert rejected_fields(untokened(tf=tf)) == ("tf",)


# ───────────────────────────── required keys, unknown keys ─────────────────────────────


@pytest.mark.parametrize("key", ["v", "strategy_id", "symbol", "tf", "bar_time", "action"])
def test_identifying_keys_are_required(key: str) -> None:
    assert rejected_fields(without(untokened(), key)) == (key,)
    assert rejected_fields(without(untokened(alerts.heartbeat_payload()), key)) == (key,)


@pytest.mark.parametrize("key", ["v", "strategy_id", "symbol", "tf", "bar_time", "action"])
def test_identifying_keys_cannot_be_null(key: str) -> None:
    assert rejected_fields(untokened(**{key: None})) == (key,)


def test_empty_object_reports_every_required_key() -> None:
    assert rejected_fields({}) == ("action", "bar_time", "strategy_id", "symbol", "tf", "v")


@pytest.mark.parametrize("key", ["qty", "Token", "TOKEN", "side ", "", "comment", "v2"])
def test_unknown_keys_are_refused_without_repeating_their_name(key: str) -> None:
    error = schema_error(untokened(**{key: 1}))

    assert error.fields == (UNKNOWN_KEY_FIELD,)
    assert str(error) == "bad_schema: unknown_key"


def test_several_problems_are_reported_together_sorted_and_unique() -> None:
    obj = untokened(tf="D", est_entry="x", stop=None, first_extra=1, second_extra=2, v=3)

    assert rejected_fields(obj) == ("est_entry", "tf", UNKNOWN_KEY_FIELD, "v")


# ───────────────────────────── cross-field rules: heartbeat ─────────────────────────────


def test_heartbeat_needs_only_the_identifying_keys() -> None:
    alert = validate_alert(untokened(alerts.heartbeat_payload()))

    assert alert.action is Action.HEARTBEAT
    assert alert.strategy_id == HEARTBEAT_STRATEGY_ID
    assert alert.bar_seconds == 900


def test_heartbeat_accepts_explicit_nulls_and_an_empty_setup() -> None:
    nulls = dict.fromkeys(
        ["side", "order_type", "est_entry", "stop", "tp", "stop_dist_pts", "rr", "reason"]
    )

    alert = validate_alert(untokened(alerts.heartbeat_payload(), setup="", **nulls))

    assert alert.action is Action.HEARTBEAT


@pytest.mark.parametrize("strategy_id", sorted(STRATEGY_NAMES))
def test_heartbeat_action_needs_the_heartbeat_strategy_id(strategy_id: str) -> None:
    obj = untokened(alerts.heartbeat_payload(), strategy_id=strategy_id)

    assert rejected_fields(obj) == ("action", "strategy_id")


@pytest.mark.parametrize("action", ["entry", "stop_update", "exit", "cancel"])
def test_heartbeat_strategy_id_needs_the_heartbeat_action(action: str) -> None:
    assert rejected_fields(untokened(strategy_id="heartbeat", action=action)) == (
        "action",
        "strategy_id",
    )


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("side", "long"),
        ("order_type", "market"),
        ("est_entry", 5123.25),
        ("stop", 5110.75),
        ("tp", 5148.25),
        ("stop_dist_pts", 0),
        ("rr", 2),
        ("reason", "session_end"),
        ("setup", "FVG"),
    ],
)
def test_heartbeat_carries_no_trade_data(field: str, value: object) -> None:
    assert rejected_fields(untokened(alerts.heartbeat_payload(), **{field: value})) == (field,)


def test_an_entry_relabelled_as_heartbeat_lists_every_trade_field() -> None:
    obj = untokened(strategy_id="heartbeat", action="heartbeat")

    assert rejected_fields(obj) == (
        "est_entry",
        "order_type",
        "rr",
        "setup",
        "side",
        "stop",
        "stop_dist_pts",
        "tp",
    )


# ───────────────────────────── cross-field rules: strategies ─────────────────────────────


@pytest.mark.parametrize("builder", NON_HEARTBEAT_BUILDERS)
@pytest.mark.parametrize(
    "strategy_id", ["", "NY_ORB_FVG", "ny_orb_fvg ", "topstep_x", "master", "x" * 300]
)
def test_strategy_id_must_be_known(builder: Builder, strategy_id: str) -> None:
    assert rejected_fields(untokened(builder(), strategy_id=strategy_id)) == ("strategy_id",)


@pytest.mark.parametrize("builder", NON_HEARTBEAT_BUILDERS)
def test_side_is_required_for_every_strategy_action(builder: Builder) -> None:
    assert rejected_fields(untokened(builder(), side=None)) == ("side",)
    assert rejected_fields(without(untokened(builder()), "side")) == ("side",)


@pytest.mark.parametrize("builder", ENTRY_BUILDERS)
@pytest.mark.parametrize("field", ["setup", "order_type", "est_entry", "stop"])
def test_entry_requires_setup_order_type_entry_and_stop(builder: Builder, field: str) -> None:
    empty = "" if field == "setup" else None

    assert rejected_fields(untokened(builder(), **{field: empty})) == (field,)
    assert rejected_fields(without(untokened(builder()), field)) == (field,)


def test_entry_without_any_level_names_both() -> None:
    assert rejected_fields(untokened(est_entry=None, stop=None, tp=None)) == ("est_entry", "stop")


@pytest.mark.parametrize("builder", ENTRY_BUILDERS)
@pytest.mark.parametrize("side", ["long", "short"])
def test_entry_target_and_rr_are_optional(builder: Builder, side: str) -> None:
    nulled = validate_alert(untokened(builder(side), tp=None, rr=None, stop_dist_pts=None))
    absent = validate_alert(without(untokened(builder(side)), "tp", "rr", "stop_dist_pts"))

    assert nulled == absent
    assert (absent.tp, absent.rr, absent.stop_dist_pts) == (None, None, None)
    assert absent.stop_distance is not None


@pytest.mark.parametrize("side", ["long", "short"])
def test_entry_stop_must_differ_from_entry(side: str) -> None:
    obj = untokened(alerts.ny_orb_fvg_entry(side), stop=5123.25)

    assert rejected_fields(obj) == ("stop",)


@pytest.mark.parametrize(
    ("side", "stop"),
    [("long", 5123.5), ("long", 5135.75), ("short", 5123.0), ("short", 5110.75)],
)
def test_entry_stop_must_be_on_the_loss_side(side: str, stop: float) -> None:
    obj = untokened(alerts.ny_orb_fvg_entry(side), stop=stop, tp=None)

    assert rejected_fields(obj) == ("stop",)


@pytest.mark.parametrize(
    ("side", "tp"),
    [
        ("long", 5123.25),
        ("long", 5123.0),
        ("long", 5098.25),
        ("short", 5123.25),
        ("short", 5148.25),
    ],
)
def test_entry_target_must_be_on_the_profit_side(side: str, tp: float) -> None:
    obj = untokened(alerts.ny_orb_fvg_entry(side), tp=tp)

    assert rejected_fields(obj) == ("tp",)


@pytest.mark.parametrize(("side", "wrong"), [("long", "short"), ("short", "long")])
def test_entry_with_the_levels_of_the_other_side_names_stop_and_target(
    side: str, wrong: str
) -> None:
    obj = untokened(alerts.ny_orb_fvg_entry(side), side=wrong)

    assert rejected_fields(obj) == ("stop", "tp")


@pytest.mark.parametrize(("side", "tp"), [("long", 5123.5), ("short", 5123.0)])
def test_entry_target_one_tick_away_is_enough(side: str, tp: float) -> None:
    assert validate_alert(untokened(alerts.ny_orb_fvg_entry(side), tp=tp)).tp == tp


@pytest.mark.parametrize("builder", STOP_UPDATE_BUILDERS)
@pytest.mark.parametrize("field", ["est_entry", "stop"])
def test_stop_update_requires_entry_and_stop(builder: Builder, field: str) -> None:
    assert rejected_fields(untokened(builder(), **{field: None})) == (field,)
    assert rejected_fields(without(untokened(builder()), field)) == (field,)


@pytest.mark.parametrize("side", ["long", "short"])
@pytest.mark.parametrize("stop", [5100.0, 5123.5, 5140.0])
def test_stop_update_may_put_the_stop_anywhere(side: str, stop: float) -> None:
    # Below, at and beyond the entry: whether a stop widens is the processor's decision.
    alert = validate_alert(untokened(alerts.ny_orb_fvg_stop_update(side), stop=stop))

    assert alert.stop == stop
    assert alert.stop_distance == abs(5123.5 - stop)


def test_stop_update_needs_nothing_else() -> None:
    obj = without(
        untokened(alerts.ny_orb_fvg_stop_update()),
        "setup",
        "order_type",
        "tp",
        "stop_dist_pts",
        "rr",
    )

    alert = validate_alert(obj)

    assert alert.setup == ""
    assert alert.order_type is None
    assert alert.tp is None


@pytest.mark.parametrize("action", ["exit", "cancel"])
@pytest.mark.parametrize("strategy_id", sorted(STRATEGY_NAMES))
def test_exit_and_cancel_need_only_identity_and_side(action: str, strategy_id: str) -> None:
    obj: dict[str, object] = {
        "v": 1,
        "strategy_id": strategy_id,
        "symbol": SYMBOL,
        "tf": "1",
        "bar_time": BAR_TIME_MS,
        "action": action,
        "side": "short",
    }

    alert = validate_alert(obj)

    assert alert.action is Action(action)
    assert alert.est_entry is None
    assert alert.stop_distance is None


@pytest.mark.parametrize("action", ["stop_update", "exit", "cancel"])
def test_only_entries_are_held_to_the_entry_geometry(action: str) -> None:
    # Levels of a short labelled long: refused for an entry, not judged for the other actions.
    obj = untokened(alerts.ny_orb_fvg_entry("short"), side="long", action=action)

    assert validate_alert(obj).action is Action(action)


def test_model_enforces_the_rules_when_built_directly() -> None:
    with pytest.raises(ValidationError):
        TvAlert.model_validate(untokened(stop=6000.0))
    with pytest.raises(ValidationError):
        TvAlert(
            v=1,
            strategy_id="ny_orb_fvg",
            symbol=SYMBOL,
            tf="1",
            bar_time=BAR_TIME_MS,
            action=Action.ENTRY,
        )


# ───────────────────────────── errors never carry input values ─────────────────────────────


def everything_an_error_shows(error: BaseException) -> str:
    chain = "".join(traceback.format_exception(error))
    return "\n".join([str(error), repr(error), repr(error.args), chain])


def test_alert_error_sorts_deduplicates_and_hides_unknown_names() -> None:
    error = AlertError(AlertErrorCode.BAD_SCHEMA, ["tp", SECRET, "stop", "tp", "token", "other"])

    assert error.code is AlertErrorCode.BAD_SCHEMA
    assert error.fields == ("stop", "tp", UNKNOWN_KEY_FIELD)
    assert str(error) == "bad_schema: stop, tp, unknown_key"
    assert SECRET not in everything_an_error_shows(error)


def test_alert_error_without_fields() -> None:
    error = AlertError(AlertErrorCode.BAD_JSON)

    assert error.fields == ()
    assert str(error) == "bad_json"


@pytest.mark.parametrize(
    "field",
    [
        "v",
        "strategy_id",
        "symbol",
        "tf",
        "bar_time",
        "action",
        "side",
        "setup",
        "order_type",
        "est_entry",
        "stop",
        "tp",
        "stop_dist_pts",
        "rr",
        "reason",
    ],
)
def test_schema_error_never_repeats_a_value_put_in_the_wrong_field(field: str) -> None:
    assert field in TvAlert.model_fields

    error = schema_error(untokened(**{field: SECRET}))

    assert field in error.fields
    assert SECRET not in everything_an_error_shows(error)
    assert error.__cause__ is None
    assert error.__context__ is None


def test_schema_error_never_repeats_unknown_keys_or_their_values() -> None:
    error = schema_error(untokened(token=SECRET, **{SECRET: 1, "note": SECRET}))

    assert error.fields == (UNKNOWN_KEY_FIELD,)
    assert SECRET not in everything_an_error_shows(error)
    assert error.__cause__ is None
    assert error.__context__ is None


def test_cross_field_error_never_repeats_values() -> None:
    marker = 987654.25

    error = schema_error(untokened(stop=marker, tp=None))

    assert error.fields == ("stop",)
    assert "987654" not in everything_an_error_shows(error)
    assert error.__context__ is None


def test_pydantic_error_text_hides_the_input() -> None:
    with pytest.raises(ValidationError) as excinfo:
        TvAlert.model_validate(untokened(est_entry=SECRET, symbol=SECRET + " "))

    assert SECRET not in str(excinfo.value)


@pytest.mark.parametrize(
    "raw",
    [
        b'{"v":1,"token":"' + SECRET.encode() + b'","strategy_id":',
        b'{"token":"' + SECRET.encode() + b'","token":"' + SECRET.encode() + b'"}',
        b'{"token":"' + SECRET.encode() + b'","rr":NaN}',
        b'{"token":"' + SECRET.encode() + b'","rr":1e999}',
        b'{"token":"' + SECRET.encode() + b'\xff"}',
        b'["' + SECRET.encode() + b'"]',
        b'"' + SECRET.encode() + b'"',
    ],
)
def test_json_error_never_carries_the_body(raw: bytes) -> None:
    error = json_error(raw)

    assert str(error) == "bad_json"
    assert SECRET not in everything_an_error_shows(error)
    # JSONDecodeError keeps the whole document in `.doc`: it must not be attached.
    assert error.__cause__ is None
    assert error.__context__ is None


# ───────────────────────────── decode_json_object ─────────────────────────────


def test_decode_returns_the_object_with_json_types() -> None:
    raw = b' \r\n\t{"a":1,"b":2.5,"c":"x","d":null,"e":true,"f":[1,{"g":"h"}],"i":{"j":-0.0}} \n'

    assert decode_json_object(raw) == {
        "a": 1,
        "b": 2.5,
        "c": "x",
        "d": None,
        "e": True,
        "f": [1, {"g": "h"}],
        "i": {"j": -0.0},
    }


def test_decode_accepts_an_empty_object_and_escapes() -> None:
    assert decode_json_object(b"{}") == {}
    assert decode_json_object(b'{"a":"\\u00e7\\n\\"","b":"\\ud83d\\ude00"}') == {
        "a": 'ç\n"',
        "b": "\U0001f600",
    }
    assert decode_json_object('{"a":"çığ"}'.encode()) == {"a": "çığ"}


def test_decode_keeps_integers_and_floats_apart() -> None:
    obj = decode_json_object(b'{"int":2,"float":2.0,"exp":2e0,"tiny":1e-400}')

    assert [type(obj[key]) for key in ("int", "float", "exp", "tiny")] == [int, float, float, float]
    assert obj["tiny"] == 0.0


HOSTILE_BODIES: dict[str, bytes] = {
    "empty": b"",
    "whitespace": b" \n\t ",
    "top_level_array": b'[{"v":1}]',
    "top_level_empty_array": b"[]",
    "top_level_string": b'"entry"',
    "top_level_number": b"1",
    "top_level_float": b"1.5",
    "top_level_null": b"null",
    "top_level_true": b"true",
    "truncated": b'{"v":1,"action":"ent',
    "trailing_data": b'{"v":1} x',
    "two_objects": b'{"v":1}{"v":1}',
    "trailing_comma": b'{"v":1,}',
    "single_quotes": b"{'v':1}",
    "unquoted_key": b"{v:1}",
    "comment": b'{"v":1} // note',
    "form_encoded": b"v=1&action=entry",
    "plain_text": b"NY ORB FVG long",
    "raw_control_character": b'{"a":"\x00"}',
    "raw_newline_in_string": b'{"a":"x\ny"}',
    "duplicate_key": b'{"v":1,"v":1}',
    "duplicate_key_different_values": b'{"action":"exit","action":"entry"}',
    "duplicate_token": b'{"token":"a","token":"b"}',
    "duplicate_key_nested": b'{"a":{"b":1,"b":2}}',
    "duplicate_key_deeply_nested": b'{"a":[1,{"b":[{"c":{"d":1,"e":2,"d":3}}]}]}',
    "duplicate_key_via_escape": b'{"stop":1,"st\\u006fp":2}',
    "nan": b'{"rr":NaN}',
    "infinity": b'{"rr":Infinity}',
    "negative_infinity": b'{"rr":-Infinity}',
    "nan_nested": b'{"a":[{"b":NaN}]}',
    "float_overflow": b'{"stop":1e400}',
    "negative_float_overflow": b'{"stop":-1e400}',
    "float_overflow_nested": b'{"a":[[1e999]]}',
    "float_overflow_long_mantissa": b'{"stop":' + b"9" * 400 + b".0}",
    "invalid_utf8": b'{"symbol":"\xff\xfe"}',
    "truncated_utf8": b'{"symbol":"\xc3"}',
    "overlong_utf8": b'{"symbol":"\xc0\xaf"}',
    "utf8_encoded_surrogate": b'{"symbol":"\xed\xa0\x80"}',
    "latin1": b'{"symbol":"\xe7\xfd\xf0"}',
    "utf8_bom": b'\xef\xbb\xbf{"v":1}',
    "utf16_with_bom": '{"v":1}'.encode("utf-16"),
    "utf16_le": '{"v":1}'.encode("utf-16-le"),
    "utf32": '{"v":1}'.encode("utf-32"),
    "lone_high_surrogate": b'{"token":"\\ud800"}',
    "lone_low_surrogate": b'{"token":"\\udfff"}',
    "lone_surrogate_key": b'{"\\ud800":1}',
    "lone_surrogate_nested": b'{"a":[{"b":"x\\udc00y"}]}',
    "unclosed_arrays": b"[" * MAX_BODY_BYTES,
    "unclosed_objects": b'{"a":' * (MAX_BODY_BYTES // 5),
    "nul_bytes": b"\x00" * 64,
}


@pytest.mark.parametrize("name", sorted(HOSTILE_BODIES))
def test_decode_refuses_hostile_bodies(name: str) -> None:
    raw = HOSTILE_BODIES[name]
    assert len(raw) <= MAX_BODY_BYTES

    json_error(raw)


def decoded_or_refused(raw: bytes) -> dict[str, object] | None:
    """The decoded object, or None for a clean BAD_JSON refusal; anything else propagates."""
    try:
        return decode_json_object(raw)
    except AlertError as error:
        if error.code is not AlertErrorCode.BAD_JSON:
            raise
        return None


def deep_arrays(depth: int) -> bytes:
    return b'{"a":' + b"[" * depth + b"]" * depth + b"}"


def deep_objects(depth: int) -> bytes:
    return b'{"a":' * depth + b"1" + b"}" * depth


@pytest.mark.parametrize("raw", [deep_arrays(2040), deep_objects(680)], ids=["arrays", "objects"])
def test_nesting_that_fits_the_body_limit_never_crashes(raw: bytes) -> None:
    # Whether the parser copes with the depth or gives up, the outcome is a clean refusal.
    assert len(raw) <= MAX_BODY_BYTES
    obj = decoded_or_refused(raw)

    if obj is not None:
        assert "a" in obj
        assert UNKNOWN_KEY_FIELD in rejected_fields(obj)
        assert UNKNOWN_KEY_FIELD in rejected_fields(untokened() | obj)


@pytest.mark.parametrize(
    "raw", [deep_arrays(100_000), deep_objects(100_000)], ids=["arrays", "objects"]
)
def test_nesting_beyond_the_parser_limit_is_bad_json(raw: bytes) -> None:
    json_error(raw)


def test_integer_too_long_for_python_is_bad_json() -> None:
    json_error(b'{"bar_time":' + b"9" * 5000 + b"}")


@pytest.mark.parametrize("field", ["bar_time", "est_entry", "v"])
def test_huge_integer_inside_the_body_limit_is_refused_by_the_schema(field: str) -> None:
    raw = b'{"' + field.encode() + b'":' + b"9" * 4000 + b"}"
    assert len(raw) <= MAX_BODY_BYTES

    obj = untokened() | decode_json_object(raw)

    assert rejected_fields(obj) == (field,)


@pytest.mark.parametrize(
    ("raw", "fields"),
    [
        (b'{"stop":1e-400}', ("stop",)),  # underflows to 0.0
        (b'{"stop":1.7976931348623157e308,"tp":1e308}', ("stop",)),  # finite, wrong side
        (b'{"side":["long"]}', ("side",)),
        (b'{"est_entry":{"value":5123.25}}', ("est_entry",)),
        (b'{"bar_time":1784647800000.0}', ("bar_time",)),
        (b'{"v":1.0}', ("v",)),
        (b'{"v":true}', ("v",)),
        (b'{"symbol":"MES1!\\u0000"}', ("symbol",)),
        (b'{"strategy_id":"ny_orb_fvg\\u0000"}', ("strategy_id",)),
    ],
)
def test_odd_but_well_formed_json_is_refused_by_the_schema(
    raw: bytes, fields: tuple[str, ...]
) -> None:
    obj = untokened() | decode_json_object(raw)

    assert rejected_fields(obj) == fields


def test_decode_then_validate_end_to_end_from_bytes() -> None:
    obj = decode_json_object(alert_bytes(tf="5", bar_time=BAR_TIME_MS + 300_000))

    assert pop_token(obj) == TOKEN
    alert = validate_alert(obj)

    assert alert.bar_close_ms == BAR_TIME_MS + 600_000
    assert alert_bytes(alert_payload(), tf="5") == alert_bytes(tf="5")
    assert alert_bytes(alert_payload(tf="5")) == alert_bytes(tf="5")
    assert json.loads(alert_bytes()) == alert_payload()
