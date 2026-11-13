"""TradingView alert payloads as the Pine scripts build them (`f_alert` in `pine/*.pine`).

`pine_alert_text` repeats Pine's string concatenation: same key order, prices printed with
`str.tostring(x, format.mintick)`, `na` printed as `null`. Every builder parses that text,
so the dictionaries are what `json.loads` yields for a real webhook body, token included.

One builder per strategy/action shape. The Master portfolio script sends the `ny_orb_fvg`
and `asian_sweep` shapes unchanged (same `f_alert`, the strategy id is an argument).

Keyword overrides replace (or add) top-level keys AFTER the payload is built and recompute
nothing: `alert_payload(stop=1.0)` keeps the original `stop_dist_pts`. Tests that need a
key removed delete it from the returned dictionary.
"""

import json
from collections.abc import Mapping
from decimal import ROUND_HALF_UP, Decimal
from typing import Final

# Shaped like a real TV_WEBHOOK_TOKEN (^[A-Za-z0-9_-]{32,128}$); a test value, not a secret.
TOKEN: Final = "tv_test_token_0123456789_ABCDEFGHIJKLMNOPQRSTUV"
SYMBOL: Final = "CME_MINI:MES1!"
TF: Final = "1"
# 2026-07-21 15:30:00 UTC = 11:30 New York = 18:30 Istanbul (bar OPEN).
BAR_TIME_MS: Final = 1_784_647_800_000
MINTICK: Final = 0.25
HEARTBEAT_TF: Final = "15"

LONG: Final = "long"
SHORT: Final = "short"

_NULL: Final = "null"


def f_num(value: float | None, mintick: float = MINTICK) -> str:
    """Pine `f_num`: `na` -> null, else `str.tostring(x, format.mintick)`.

    `format.mintick` rounds to the nearest tick (ties up) and keeps the tick's decimals,
    trailing zeros included: 5120 with a 0.25 tick prints as 5120.00.
    """
    if value is None:
        return _NULL
    tick = Decimal(str(mintick))
    ticks = (Decimal(str(value)) / tick).quantize(Decimal(1), rounding=ROUND_HALF_UP)
    return str((ticks * tick).quantize(tick))


def input_rr(value: float) -> str:
    """Pine `str.tostring(rr)` for an input value: 2.0 -> "2", 1.5 -> "1.5"."""
    return format(Decimal(str(value)).normalize(), "f")


def computed_rr(est_entry: float | None, stop: float | None, tp: float | None) -> str:
    """Pine `rrOut` of ict_silver_bullet / mr_bb_kc_rsi2, printed with "#.##" (or null)."""
    if est_entry is None or stop is None or tp is None or est_entry == stop:
        return _NULL
    ratio = Decimal(str(abs(tp - est_entry) / abs(est_entry - stop)))
    return format(ratio.quantize(Decimal("0.01"), rounding=ROUND_HALF_UP).normalize(), "f")


def pine_alert_text(
    *,
    strategy_id: str,
    action: str,
    side: str,
    setup: str,
    order_type: str,
    est_entry: float | None,
    stop: float | None,
    tp: float | None,
    rr: str,
    token: str = TOKEN,
    symbol: str = SYMBOL,
    tf: str = TF,
    bar_time: int = BAR_TIME_MS,
    mintick: float = MINTICK,
) -> str:
    """The exact string a strategy's `f_alert` hands to `alert()`. `rr` is already text."""
    stop_dist = None if est_entry is None or stop is None else abs(est_entry - stop)
    text = '{"v":1,"token":"' + token + '","strategy_id":"' + strategy_id + '"'
    text += ',"symbol":"' + symbol + '","tf":"' + tf + '","bar_time":' + str(bar_time)
    text += ',"action":"' + action + '","side":"' + side + '","setup":"' + setup + '"'
    text += ',"order_type":"' + order_type + '"'
    text += ',"est_entry":' + f_num(est_entry, mintick) + ',"stop":' + f_num(stop, mintick)
    text += ',"tp":' + f_num(tp, mintick)
    text += ',"stop_dist_pts":' + f_num(stop_dist, mintick) + ',"rr":' + rr + "}"
    return text


def pine_heartbeat_text(
    *, token: str = TOKEN, symbol: str = SYMBOL, tf: str = HEARTBEAT_TF, bar_time: int = BAR_TIME_MS
) -> str:
    """The heartbeat indicator's alert: the seven identifying keys and nothing else."""
    text = '{"v":1,"token":"' + token + '","strategy_id":"heartbeat"'
    text += ',"symbol":"' + symbol + '","tf":"' + tf + '","bar_time":' + str(bar_time)
    text += ',"action":"heartbeat"}'
    return text


def _parsed(text: str, overrides: Mapping[str, object]) -> dict[str, object]:
    payload: dict[str, object] = json.loads(text)
    payload.update(overrides)
    return payload


def _levels(side: str, entry: float, risk: float, reward: float) -> tuple[float, float]:
    """(stop, target) around `entry`: stop on the loss side, target on the profit side."""
    if side == LONG:
        return entry - risk, entry + reward
    return entry + risk, entry - reward


# ───────────── ny_orb_fvg (also Master S1): market entry, trailing stop, forced exit ─────────────


def ny_orb_fvg_entry(
    side: str = LONG, setup: str = "FVG", **overrides: object
) -> dict[str, object]:
    """Signal-bar close as est_entry; fixed target at rr x risk. `setup` is FVG or IFVG."""
    close, rr = 5123.25, 2.0
    stop, tp = _levels(side, close, 12.5, rr * 12.5)
    text = pine_alert_text(
        strategy_id="ny_orb_fvg",
        action="entry",
        side=side,
        setup=setup,
        order_type="market",
        est_entry=close,
        stop=stop,
        tp=tp,
        rr=input_rr(rr),
    )
    return _parsed(text, overrides)


def ny_orb_fvg_stop_update(side: str = LONG, **overrides: object) -> dict[str, object]:
    """Trail step: est_entry = average fill, stop = NEW stop (loss side here), tp unchanged."""
    fill, rr = 5123.5, 2.0
    _, tp = _levels(side, fill, 12.75, rr * 12.75)
    new_stop, _ = _levels(side, fill, 3.5, 0.0)
    text = pine_alert_text(
        strategy_id="ny_orb_fvg",
        action="stop_update",
        side=side,
        setup="",
        order_type="market",
        est_entry=fill,
        stop=new_stop,
        tp=tp,
        rr=input_rr(rr),
        bar_time=BAR_TIME_MS + 18 * 60_000,
    )
    return _parsed(text, overrides)


def ny_orb_fvg_exit(side: str = LONG, **overrides: object) -> dict[str, object]:
    """Forced flatten: est_entry = bar close, stop/tp/stop_dist_pts null, rr = the input."""
    text = pine_alert_text(
        strategy_id="ny_orb_fvg",
        action="exit",
        side=side,
        setup="",
        order_type="market",
        est_entry=5131.5,
        stop=None,
        tp=None,
        rr=input_rr(2.0),
        bar_time=BAR_TIME_MS + 269 * 60_000,
    )
    return _parsed(text, overrides)


# ───────────── asian_sweep (also Master S2): market entry, forced exit ─────────────


def asian_sweep_entry(side: str = LONG, **overrides: object) -> dict[str, object]:
    close, rr = 5098.0, 2.0
    stop, tp = _levels(side, close, 9.25, rr * 9.25)
    text = pine_alert_text(
        strategy_id="asian_sweep",
        action="entry",
        side=side,
        setup="SWEEP_FVG",
        order_type="market",
        est_entry=close,
        stop=stop,
        tp=tp,
        rr=input_rr(rr),
    )
    return _parsed(text, overrides)


def asian_sweep_exit(side: str = LONG, **overrides: object) -> dict[str, object]:
    text = pine_alert_text(
        strategy_id="asian_sweep",
        action="exit",
        side=side,
        setup="",
        order_type="market",
        est_entry=5104.75,
        stop=None,
        tp=None,
        rr=input_rr(2.0),
        bar_time=BAR_TIME_MS + 120 * 60_000,
    )
    return _parsed(text, overrides)


# ───────────── ict_silver_bullet: LIMIT entry, cancel, forced exit; rr computed ─────────────


def _silver_bullet_levels(side: str) -> tuple[float, float, float]:
    # The limit is the FVG midpoint, which need not sit on the tick grid: f_num rounds it.
    limit = 5123.125
    stop, tp = _levels(side, limit, 5.5, 11.0)
    return limit, stop, tp


def ict_silver_bullet_entry(side: str = LONG, **overrides: object) -> dict[str, object]:
    """Pending limit order: est_entry = limit price; the bracket goes with it."""
    limit, stop, tp = _silver_bullet_levels(side)
    text = pine_alert_text(
        strategy_id="ict_silver_bullet",
        action="entry",
        side=side,
        setup="SB_FVG",
        order_type="limit",
        est_entry=limit,
        stop=stop,
        tp=tp,
        rr=computed_rr(limit, stop, tp),
    )
    return _parsed(text, overrides)


def ict_silver_bullet_cancel(side: str = LONG, **overrides: object) -> dict[str, object]:
    """The unfilled limit entry is withdrawn: same levels as the entry alert."""
    limit, stop, tp = _silver_bullet_levels(side)
    text = pine_alert_text(
        strategy_id="ict_silver_bullet",
        action="cancel",
        side=side,
        setup="SB_FVG",
        order_type="limit",
        est_entry=limit,
        stop=stop,
        tp=tp,
        rr=computed_rr(limit, stop, tp),
        bar_time=BAR_TIME_MS + 10 * 60_000,
    )
    return _parsed(text, overrides)


def ict_silver_bullet_exit(side: str = LONG, **overrides: object) -> dict[str, object]:
    """Forced flatten: the setup constant stays, order_type is market, rr is null."""
    text = pine_alert_text(
        strategy_id="ict_silver_bullet",
        action="exit",
        side=side,
        setup="SB_FVG",
        order_type="market",
        est_entry=5127.75,
        stop=None,
        tp=None,
        rr=computed_rr(5127.75, None, None),
        bar_time=BAR_TIME_MS + 30 * 60_000,
    )
    return _parsed(text, overrides)


# ───────────── mr_bb_kc_rsi2: market entry, ATR stop, BB-basis target, forced exit ─────────────


def mr_bb_kc_rsi2_entry(
    side: str = LONG, *, target_one_tick: bool = False, **overrides: object
) -> dict[str, object]:
    """ATR stop and basis target are off the tick grid; rr is the computed ratio ("#.##").

    `target_one_tick`: the bar closed beyond the basis, so Pine clamps the target to one
    tick from the close; with a wide ATR stop the printed ratio rounds to 0.
    """
    if target_one_tick:
        close = 21000.25
        stop, tp = _levels(side, close, 61.3, MINTICK)
    else:
        close = 5101.5
        stop, tp = _levels(side, close, 8.6, 7.87)
    text = pine_alert_text(
        strategy_id="mr_bb_kc_rsi2",
        action="entry",
        side=side,
        setup="BB_KC_RSI2",
        order_type="market",
        est_entry=close,
        stop=stop,
        tp=tp,
        rr=computed_rr(close, stop, tp),
        tf="5",
    )
    return _parsed(text, overrides)


def mr_bb_kc_rsi2_exit(side: str = LONG, **overrides: object) -> dict[str, object]:
    text = pine_alert_text(
        strategy_id="mr_bb_kc_rsi2",
        action="exit",
        side=side,
        setup="BB_KC_RSI2",
        order_type="market",
        est_entry=5106.25,
        stop=None,
        tp=None,
        rr=computed_rr(5106.25, None, None),
        tf="5",
        bar_time=BAR_TIME_MS + 265 * 60_000,
    )
    return _parsed(text, overrides)


# ───────────── mtf_trend_cont: market entry, TP1, stop updates, forced exit ─────────────


def mtf_trend_cont_entry(side: str = LONG, **overrides: object) -> dict[str, object]:
    """tp = TP1 (partial exit level); rr = the TP1 reward:risk input."""
    close, tp1_rr = 5140.0, 1.5
    stop, tp = _levels(side, close, 10.0, tp1_rr * 10.0)
    text = pine_alert_text(
        strategy_id="mtf_trend_cont",
        action="entry",
        side=side,
        setup="MTF_PULLBACK",
        order_type="market",
        est_entry=close,
        stop=stop,
        tp=tp,
        rr=input_rr(tp1_rr),
        tf="5",
    )
    return _parsed(text, overrides)


def mtf_trend_cont_stop_update(
    side: str = LONG, *, tp1_filled: bool = False, **overrides: object
) -> dict[str, object]:
    """Before TP1: the stop re-priced from the real fill, tp = TP1.

    After TP1 (`tp1_filled`): the runner's stop moves to breakeven, so stop == est_entry,
    stop_dist_pts is 0 and tp is null.
    """
    fill, tp1_rr = 5140.5, 1.5
    stop, tp1 = _levels(side, fill, 10.5, tp1_rr * 10.5)
    text = pine_alert_text(
        strategy_id="mtf_trend_cont",
        action="stop_update",
        side=side,
        setup="MTF_PULLBACK",
        order_type="market",
        est_entry=fill,
        stop=fill if tp1_filled else stop,
        tp=None if tp1_filled else tp1,
        rr=input_rr(tp1_rr),
        tf="5",
        bar_time=BAR_TIME_MS + (45 if tp1_filled else 5) * 60_000,
    )
    return _parsed(text, overrides)


def mtf_trend_cont_exit(side: str = LONG, **overrides: object) -> dict[str, object]:
    text = pine_alert_text(
        strategy_id="mtf_trend_cont",
        action="exit",
        side=side,
        setup="MTF_PULLBACK",
        order_type="market",
        est_entry=5152.25,
        stop=None,
        tp=None,
        rr=input_rr(1.5),
        tf="5",
        bar_time=BAR_TIME_MS + 265 * 60_000,
    )
    return _parsed(text, overrides)


# ───────────── heartbeat indicator ─────────────


def heartbeat_payload(**overrides: object) -> dict[str, object]:
    return _parsed(pine_heartbeat_text(), overrides)


# ───────────── generic entry points used across the test suite ─────────────


def alert_payload(**overrides: object) -> dict[str, object]:
    """The reference alert (ny_orb_fvg long FVG entry, SPEC §5.1 example) with overrides applied.

    `side="short"` starts from the short entry (mirrored stop and target), so the result is
    still a valid alert; every override, `side` included, is then plain key replacement.
    """
    payload = ny_orb_fvg_entry(SHORT if overrides.get("side") == SHORT else LONG)
    payload.update(overrides)
    return payload


def alert_bytes(payload: Mapping[str, object] | None = None, /, **overrides: object) -> bytes:
    """A webhook body: `payload` (default: the reference alert) with overrides, as UTF-8 JSON."""
    body = alert_payload() if payload is None else dict(payload)
    body.update(overrides)
    return json.dumps(body, separators=(",", ":")).encode("utf-8")


def pine_payloads() -> dict[str, dict[str, object]]:
    """Every payload shape the Pine scripts can send, keyed "strategy/action/side[/variant]"."""
    payloads: dict[str, dict[str, object]] = {"heartbeat/heartbeat": heartbeat_payload()}
    for side in (LONG, SHORT):
        payloads |= {
            f"ny_orb_fvg/entry/{side}/FVG": ny_orb_fvg_entry(side, "FVG"),
            f"ny_orb_fvg/entry/{side}/IFVG": ny_orb_fvg_entry(side, "IFVG"),
            f"ny_orb_fvg/stop_update/{side}": ny_orb_fvg_stop_update(side),
            f"ny_orb_fvg/exit/{side}": ny_orb_fvg_exit(side),
            f"asian_sweep/entry/{side}": asian_sweep_entry(side),
            f"asian_sweep/exit/{side}": asian_sweep_exit(side),
            f"ict_silver_bullet/entry/{side}": ict_silver_bullet_entry(side),
            f"ict_silver_bullet/cancel/{side}": ict_silver_bullet_cancel(side),
            f"ict_silver_bullet/exit/{side}": ict_silver_bullet_exit(side),
            f"mr_bb_kc_rsi2/entry/{side}": mr_bb_kc_rsi2_entry(side),
            f"mr_bb_kc_rsi2/entry/{side}/target_one_tick": mr_bb_kc_rsi2_entry(
                side, target_one_tick=True
            ),
            f"mr_bb_kc_rsi2/exit/{side}": mr_bb_kc_rsi2_exit(side),
            f"mtf_trend_cont/entry/{side}": mtf_trend_cont_entry(side),
            f"mtf_trend_cont/stop_update/{side}/before_tp1": mtf_trend_cont_stop_update(side),
            f"mtf_trend_cont/stop_update/{side}/breakeven": mtf_trend_cont_stop_update(
                side, tp1_filled=True
            ),
            f"mtf_trend_cont/exit/{side}": mtf_trend_cont_exit(side),
        }
    return payloads
