"""Independent Pine-vs-Python audit of the NY ORB + FVG port: hand-executed Pine traces.

Each test pins one behaviour of `pine/ny_open_fvg_strategy.pine` that the port's own tests
leave open. The trace in each test walks the script by hand, block by block in its own
per-bar order (Pine line numbers in brackets):

     1. time, OR window, session end, entry-time gate, date range, day filter  [189-210]
     2. daily reset on a NY day change (in* vars only when flat)              [213-232]
     3. build the OR, finalize it, `canScan`                                   [235-245]
     4. `ta.atr` x2, `ta.lowest` / `ta.highest`, risk base                     [248-258]
     5. breakers: baseline on a day change, fold closed trades, evaluate       [305-332]
     6. breakout -> bias (up tested first)                                     [335-341]
     7. FVG detection (also on the bias bar), FIFO cap 60                      [344-373]
     8. inversions / deaths, before the entry check                            [376-398]
     9. entry: newest first, skip dead and non-zones, break on the first touch [403-473]
    10. management: the fill bar's close finalizes the bracket, then trails    [477-516]
    11. forced flats, one reason per bar (EOD before DD before end date)       [523-526]
    12. reset of the in* vars once a trade has closed                          [531-537]

MES grid (tick 0.25, point value 5, whole contracts), zero costs, initial capital 100 000,
Pine defaults otherwise: risk 0.5 % = 500 per trade, FVG Buffer stop with 2 ticks = 0.5.
"""

import dataclasses
import datetime as dt
from collections.abc import Sequence
from dataclasses import dataclass, field
from typing import Any, Final
from zoneinfo import ZoneInfo

import pytest

from propbot.backtest.engine import run_backtest
from propbot.backtest.model import EngineResult, ExitReason, FillModel
from propbot.core.bars import Bar, BarSeries, DataSource
from propbot.data.sessions import session_last_flags
from propbot.strategies.base import Action, CloseAll, Entry, Exit, FillEvent, Side, StrategyContext
from propbot.strategies.ny_orb_fvg import (
    BEARISH,
    BULLISH,
    FRESH,
    INVERTED,
    Fvg,
    NyOrbFvg,
    NyOrbFvgParams,
)
from support.engine import make_config, mes

NY: Final = ZoneInfo("America/New_York")
MONDAY: Final = dt.date(2026, 3, 2)
TUESDAY: Final = dt.date(2026, 3, 3)
PREVIOUS_FRIDAY: Final = dt.date(2026, 2, 27)

BOTH_MODELS = pytest.mark.parametrize("fill_model", [FillModel.PESSIMISTIC, FillModel.TV_OHLC])

type OHLC = tuple[float, float, float, float]

# OR [09:30, 09:35): high 5004, low 4999.
OPENING_RANGE: Final[list[OHLC]] = [
    (5000.0, 5002.0, 4999.0, 5001.0),  # 0 09:30
    (5001.0, 5003.0, 5000.0, 5002.0),  # 1
    (5002.0, 5004.0, 5001.0, 5003.0),  # 2
    (5003.0, 5004.0, 5001.0, 5002.0),  # 3
    (5002.0, 5003.0, 5000.0, 5001.0),  # 4 09:34
]
# 5: close 5005.5 > 5004 -> bias +1; 6: low 5005 > high[2] 5003 -> bullish FVG [5003, 5005].
LONG_SETUP: Final[list[OHLC]] = [
    *OPENING_RANGE,
    (5001.0, 5006.0, 5001.0, 5005.5),  # 5 09:35
    (5005.5, 5010.0, 5005.0, 5009.5),  # 6 09:36
    (5009.5, 5009.5, 5005.5, 5008.0),  # 7 09:37 low 5005.5 > top: no touch
]
# 8: low 5004.75 <= 5005 -> signal at close 5006: stop 5002.5, dist 3.5, qty 28, TP 5013.
TOUCH: Final[OHLC] = (5008.0, 5008.0, 5004.75, 5006.0)
FILL_BAR: Final[OHLC] = (5006.5, 5008.0, 5006.0, 5007.5)  # 9: fill at the open 5006.5
TP_BAR: Final[OHLC] = (5007.5, 5015.0, 5007.0, 5014.0)
FLAT: Final[OHLC] = (5008.0, 5008.0, 5008.0, 5008.0)

# 5: close 4997.5 < 4999 -> bias -1; 6: high 4998 < low[2] 5000 -> bearish FVG [4998, 5000].
SHORT_SETUP: Final[list[OHLC]] = [
    *OPENING_RANGE,
    (5001.0, 5001.0, 4997.0, 4997.5),  # 5
    (4997.5, 4998.0, 4993.0, 4993.5),  # 6
    (4993.5, 4997.5, 4993.5, 4995.0),  # 7 high 4997.5 < bottom: no touch
]
# 8: high 4998.25 >= 4998 -> signal at close 4997: stop 5000.5, qty 28; 9: fill at 4996.5.
SHORT_TOUCH: Final[OHLC] = (4995.0, 4998.25, 4995.0, 4997.0)
SHORT_FILL_BAR: Final[OHLC] = (4996.5, 4997.0, 4995.0, 4995.5)


def ny_ms(day: dt.date, hour: int, minute: int) -> int:
    moment = dt.datetime(day.year, day.month, day.day, hour, minute, tzinfo=NY)
    return int(moment.timestamp()) * 1000


def build(*segments: tuple[int, Sequence[OHLC]], tf_seconds: int = 60) -> BarSeries:
    bars = [
        Bar(ts_open_ms=start + i * tf_seconds * 1000, open=o, high=h, low=lo, close=c, volume=1.0)
        for start, rows in segments
        for i, (o, h, lo, c) in enumerate(rows)
    ]
    source = DataSource(name="trace", format="generic", sha256="", synthetic=True, bar_tz="UTC")
    return BarSeries(symbol="TRACE", tf_seconds=tf_seconds, bars=tuple(bars), source=source)


def monday(rows: Sequence[OHLC], hour: int = 9, minute: int = 30) -> BarSeries:
    return build((ny_ms(MONDAY, hour, minute), rows))


@dataclass(eq=False)
class Tracer:
    """Delegates to the port; keeps each bar's actions and a copy of the Pine state."""

    inner: NyOrbFvg
    actions: list[list[Action]] = field(default_factory=list)
    fill_actions: list[tuple[FillEvent, list[Action]]] = field(default_factory=list)
    state: list[dict[str, Any]] = field(default_factory=list)

    @property
    def strategy_id(self) -> str:
        return self.inner.strategy_id

    @property
    def pyramiding(self) -> int:
        return self.inner.pyramiding

    def on_bar(self, bar: Bar, ctx: StrategyContext) -> Sequence[Action]:
        acts = list(self.inner.on_bar(bar, ctx))
        self.actions.append(acts)
        s = self.inner
        self.state.append(
            {
                "or_high": s.or_high,
                "or_low": s.or_low,
                "or_ready": s.or_ready,
                "bias": s.bias,
                "fvgs": tuple(dataclasses.replace(f) for f in s.fvgs),
                "in_dir": s.in_dir,
                "in_trail_stop": s.in_trail_stop,
                "in_take_profit": s.in_take_profit,
                "halted_dd": s.halted_dd,
            }
        )
        return acts

    def on_fill(self, fill: FillEvent, ctx: StrategyContext) -> Sequence[Action]:
        acts = list(self.inner.on_fill(fill, ctx))
        self.fill_actions.append((fill, acts))
        return acts

    def entries(self) -> list[tuple[int, Entry]]:
        return [
            (i, a) for i, batch in enumerate(self.actions) for a in batch if isinstance(a, Entry)
        ]

    def exits_at(self, index: int) -> list[Exit]:
        return [a for a in self.actions[index] if isinstance(a, Exit)]


def trace(
    series: BarSeries,
    fill_model: FillModel = FillModel.PESSIMISTIC,
    **changes: object,
) -> tuple[EngineResult, Tracer]:
    tracer = Tracer(NyOrbFvg(NyOrbFvgParams.model_validate(changes)))
    config = make_config(instrument=mes(), fill_model=fill_model)
    flags = session_last_flags(series.bars, series.tf_seconds)
    return run_backtest(series, tracer, config, session_last=flags), tracer


# ---- 6. breakout -> bias [335-341] -----------------------------------------------------


def test_a_wick_bar_breaking_both_sides_sets_the_long_bias_because_up_is_tested_first() -> None:
    # Bar 5 (Wick mode): high 5005 > 5004 (brokeUp) and low 4998 < 4999 (brokeDn).
    # [338] `if brokeUp and allowLong` wins; with allowLong off [340] gives -1.
    # Close mode: close 5001 is inside [4999, 5004] -> no bias.
    rows = [*OPENING_RANGE, (5001.0, 5005.0, 4998.0, 5001.0)]
    _, wick = trace(monday(rows), break_confirm="Wick")
    _, wick_no_long = trace(monday(rows), break_confirm="Wick", allow_long=False)
    _, close = trace(monday(rows))
    assert wick.state[5]["bias"] == 1
    assert wick_no_long.state[5]["bias"] == -1
    assert close.state[5]["bias"] == 0


# ---- 7. detection runs after the bias on the same bar [344-347] ------------------------


def test_the_bias_bar_itself_can_form_a_gap() -> None:
    # Bar 5: close 5006.5 > 5004 -> bias +1 [339]; then [347] low 5004.5 > high[2] (bar 3)
    # 5004 -> bullish FVG [5004, 5004.5], form bar 5, in the same bar.
    rows = [*OPENING_RANGE, (5004.5, 5007.0, 5004.5, 5006.5)]
    _, tracer = trace(monday(rows))
    assert tracer.state[5]["bias"] == 1
    assert tracer.state[5]["fvgs"] == (Fvg(5004.5, 5004.0, BULLISH, FRESH, 5, -1),)


# ---- 1-3. the chart's first bar and a partial opening range [213, 235-241] -------------


def test_a_chart_starting_inside_the_window_builds_a_partial_opening_range() -> None:
    # First bar 09:33: ta.change(...) is na -> no newDay, but isInOR is true [196, 235]:
    # OR = bars 09:33-09:34 -> high max(5004, 5003) = 5004, low min(5001, 5000) = 5000.
    # 09:35: orReady (curMin 575 >= orEnd 575, <= sessEnd 959) [240].
    rows = [*OPENING_RANGE[3:], (5001.0, 5003.0, 5000.5, 5002.0)]
    _, tracer = trace(monday(rows, minute=33))
    assert (tracer.state[1]["or_high"], tracer.state[1]["or_low"]) == (5004.0, 5000.0)
    assert not tracer.state[1]["or_ready"]
    assert tracer.state[2]["or_ready"]


# ---- 9. entry scan [403-439] -----------------------------------------------------------


def test_newer_zones_that_are_not_long_zones_are_skipped_and_an_older_fresh_gap_is_taken() -> None:
    rows = [
        *LONG_SETUP,  # gap 1 bullish [5003, 5005], form bar 6
        (5008.0, 5012.0, 5008.0, 5011.5),  # 8
        # 9: low 5011 > high[2] (bar 7) 5009.5 -> gap 2 bullish [5009.5, 5011], form bar 9
        (5011.5, 5014.0, 5011.0, 5013.5),
        # 10: [385] close 5009 < 5009.5 inverts gap 2 (flip 10) BEFORE the entry check;
        #     gap 1 is not touched (low 5008.5 > 5005).
        (5013.5, 5013.5, 5008.5, 5009.0),
        # 11: [357] high 5009.5 < low[2] (bar 9) 5011 -> gap 3 bearish [5009.5, 5011] (fresh).
        #     Entry, newest first [405]: gap 3 bearish fresh and gap 2 bullish inverted are
        #     no long zones (no break [411]); gap 1 is fresh, 11 > 6, low 5004.5 <= 5005.
        #     close 5006: stop 5003 - 0.5 = 5002.5, dist 3.5, qty floor(500 / 17.5) = 28,
        #     TP 5006 + 7 = 5013.
        (5009.0, 5009.5, 5004.5, 5006.0),
        FLAT,  # 12: the signal bar must not be the session's last bar [204]
    ]
    _, tracer = trace(monday(rows))
    assert [(f.kind, f.state) for f in tracer.state[11]["fvgs"]] == [
        (BULLISH, FRESH),
        (BULLISH, INVERTED),
        (BEARISH, FRESH),
    ]
    assert tracer.entries() == [(11, Entry("Long", Side.LONG, 28.0, comment="Long"))]
    assert tracer.exits_at(11) == [Exit("Exit L", from_entry="Long", stop=5002.5, limit=5013.0)]


# ---- 10. fill-bar finalize with the ATR method [418-429, 480-485] ----------------------


@BOTH_MODELS
def test_atr_method_with_na_atr_on_the_signal_bar_re_applies_the_fvg_distance_to_the_fill(
    fill_model: FillModel,
) -> None:
    # atrLenInit 10: ta.atr(10) is na on bar 8 (the 9th bar) and has a value on bar 9.
    # Signal bar 8 [418]: na ATR -> estStop = 5003 - 0.5 = 5002.5; inAtrDist = 5006 - 5002.5
    #   = 3.5 [429]; qty 28, est TP 5013.
    # Fill bar 9 [482]: "ATR Based" and not na(atrInit) -> rawStop = avg - inAtrDist
    #   = 5006.5 - 3.5 = 5003 (NOT the structural 5002.5); TP 5006.5 + 2 x 3.5 = 5013.5.
    # Bar 10: high 5015 trades through 5013.5 -> target; +7 x 28 x 5 = 980.
    rows = [*LONG_SETUP, TOUCH, FILL_BAR, TP_BAR]
    result, tracer = trace(monday(rows), fill_model, stop_method="ATR Based", atr_len_init=10)
    assert tracer.exits_at(8) == [Exit("Exit L", from_entry="Long", stop=5002.5, limit=5013.0)]
    assert tracer.exits_at(9) == [Exit("Exit L", from_entry="Long", stop=5003.0, limit=5013.5)]
    (trade,) = result.trades
    assert trade.initial_stop == 5003.0
    assert (trade.exit_price, trade.reason) == (5013.5, ExitReason.TARGET)
    assert trade.pnl == pytest.approx(980.0)


# ---- 10. trailing [487-494, 507-514] ---------------------------------------------------


def test_the_trail_starts_at_exactly_one_r_from_the_real_fill() -> None:
    # Fill 5006.5, initial stop 5002.5, risk 4 (rr 10: TP 5046.5).
    # Bar 10 close 5010.5: profit 4 >= 1 x 4 [492]. lowest(low, 5) over bars 6..10 =
    # min(5005, 5005.5, 5004.75, 5005.5, 5007) = 5004.75 -> cand 5004.25; +1.75 >= 1.0.
    rows = [
        *LONG_SETUP,
        TOUCH,
        (5006.5, 5008.0, 5005.5, 5007.5),  # 9 fill
        (5007.5, 5011.0, 5007.0, 5010.5),  # 10
    ]
    _, tracer = trace(monday(rows), rr=10.0)
    assert tracer.exits_at(9) == [Exit("Exit L", from_entry="Long", stop=5002.5, limit=5046.5)]
    assert tracer.exits_at(10) == [Exit("Exit L", from_entry="Long", stop=5004.25, limit=5046.5)]


def test_short_swing_trail_uses_the_highest_high_plus_the_buffer() -> None:
    # Fill 4996.5, stop 5000.5, risk 4, rr 10: TP 4996.5 - 40 = 4956.5.
    # Bar 10 close 4992.5: profit 4 >= 4 [512]. highest(high, 5) over bars 6..10 =
    # max(4998, 4997.5, 4998.25, 4997, 4996) = 4998.25 -> cand 4998.75; 5000.5 - 4998.75
    # = 1.75 >= 1.0 -> the stop moves down to 4998.75.
    rows = [*SHORT_SETUP, SHORT_TOUCH, SHORT_FILL_BAR, (4995.5, 4996.0, 4991.0, 4992.5)]
    _, tracer = trace(monday(rows), rr=10.0)
    assert tracer.exits_at(9) == [Exit("Exit S", from_entry="Short", stop=5000.5, limit=4956.5)]
    assert tracer.exits_at(10) == [Exit("Exit S", from_entry="Short", stop=4998.75, limit=4956.5)]


# ---- 11. forced-flat reason order [523] ------------------------------------------------


@pytest.mark.parametrize(
    ("close_at_end", "reason"), [(True, "EOD close"), (False, "Daily DD halt")]
)
def test_session_end_is_the_reason_when_a_daily_breach_happens_on_the_same_bar(
    close_at_end: bool, reason: str
) -> None:
    # Friday bar 0 makes Monday 09:30 a day change: baseline equity 100 000 [306].
    # Monday: setup 09:30-09:37, flat bars, touch 15:57 (curMin 957 + 1 < 959), fill 15:58
    # at 5006.5, 15:59 closes 5002.75 (stop 5002.5 untouched): floating
    # (5002.75 - 5006.5) x 28 x 5 = -525 <= -0.5 % x 100 000 -> haltedDD [327].
    # [523]: (closeAtEnd and atSessionEnd) is tested first -> "EOD close"; without
    # closeAtEnd the breach gives "Daily DD halt". Either way the flat fills at 5002.75.
    filler = [FLAT] * (15 * 60 + 57 - (9 * 60 + 38))
    sag = (5007.5, 5008.0, 5002.75, 5002.75)
    series = build(
        (ny_ms(PREVIOUS_FRIDAY, 9, 30), [FLAT]),
        (ny_ms(MONDAY, 9, 30), [*LONG_SETUP, *filler, TOUCH, FILL_BAR, sag]),
    )
    result, tracer = trace(series, daily_dd_pct=0.5, close_at_end=close_at_end)
    assert tracer.state[-1]["halted_dd"]
    assert tracer.actions[-1][-1] == CloseAll(comment=reason)
    (trade,) = result.trades
    assert (trade.reason, trade.comment, trade.exit_price) == (ExitReason.CLOSE, reason, 5002.75)


# ---- 12. the management reset after a close, on the signal bar [427-433, 531-537] ------


@BOTH_MODELS
def test_an_overnight_trade_stopped_on_a_signal_bar_leaves_the_new_trade_unmanaged(
    fill_model: FillModel,
) -> None:
    # closeAtEnd off. Monday: the long fills at 5006.5 on bar 9 (stop 5002.5, TP 5014.5) and
    # is carried overnight (no Tuesday bar trails it: closes stay below 5010.5 = +1R).
    # Tuesday (bars 10..20): OR 5006 / 5003; bar 15 close 5007 -> bias +1; bar 16 low 5006.5
    # > high[2] (bar 14) 5005.5 -> FVG [5005.5, 5006.5].
    # Bar 18: low 5002 stops Monday's trade at 5002.5 (-560; 2 % limit of 99 790 not hit).
    #   Flat at the close, so [403] enters: close 5006.25, stop 5005.5 - 0.5 = 5005,
    #   dist 1.25, qty 500 / 6.25 = 80, est TP 5006.25 + 2.5 = 5008.75 [427-433].
    #   Then [531]: flat, closedtrades 1 > closedtrades[1] 0, inDir 1 -> inDir := 0.
    # Bar 19: fill at 5006.5; [477] inDir 0 -> no finalize (it would give TP 5009.5).
    # Bar 20: high 5009.25 trades through the signal-bar TP 5008.75 -> target there.
    tuesday = [
        (5005.0, 5006.0, 5003.0, 5005.0),  # 10 09:30
        (5005.0, 5006.0, 5003.5, 5004.5),  # 11
        (5004.5, 5005.5, 5003.0, 5004.0),  # 12
        (5004.0, 5005.0, 5003.25, 5004.5),  # 13
        (5004.5, 5005.5, 5003.5, 5005.0),  # 14 09:34
        (5005.0, 5007.5, 5004.75, 5007.0),  # 15 bias +1
        (5007.0, 5010.0, 5006.5, 5009.5),  # 16 FVG [5005.5, 5006.5]
        (5009.5, 5009.5, 5007.0, 5008.0),  # 17 no touch
        (5008.0, 5008.0, 5002.0, 5006.25),  # 18 old stop + new signal
        (5006.5, 5007.0, 5006.0, 5006.5),  # 19 fill
        (5006.5, 5009.25, 5006.25, 5009.0),  # 20 target
    ]
    series = build(
        (ny_ms(MONDAY, 9, 30), [*LONG_SETUP, TOUCH, FILL_BAR]),
        (ny_ms(TUESDAY, 9, 30), tuesday),
    )
    result, tracer = trace(series, fill_model, close_at_end=False)
    assert [i for i, _ in tracer.entries()] == [8, 18]
    assert tracer.exits_at(18) == [Exit("Exit L", from_entry="Long", stop=5005.0, limit=5008.75)]
    assert tracer.state[18]["in_dir"] == 0
    assert tracer.exits_at(19) == []
    # Pessimistic: on_fill at bar 19 does not re-price either (tv_ohlc never calls it).
    assert [acts for fill, acts in tracer.fill_actions if fill.bar_index == 19] in ([], [[]])
    first, second = result.trades
    assert (first.exit_bar_index, first.exit_price, first.pnl) == (
        18,
        5002.5,
        pytest.approx(-560.0),
    )
    assert (second.entry_price, second.qty, second.initial_stop) == (5006.5, 80.0, 5005.0)
    assert (second.exit_bar_index, second.exit_price, second.reason) == (
        20,
        5008.75,
        ExitReason.TARGET,
    )
