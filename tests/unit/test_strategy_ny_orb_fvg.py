"""NY Open ORB + FVG/IFVG port (`propbot.strategies.ny_orb_fvg`) against the Pine script.

Every expected number below was derived by hand-executing `pine/ny_open_fvg_strategy.pine`
on the bar sequence of the test (comments show the arithmetic), not by running the port.
Bars run through the real backtest engine. Unless a test says otherwise: MES grid (tick
0.25, point value 5), no costs, initial capital 100 000, Pine defaults (risk 0.5 % of the
initial capital = 500, FVG Buffer stop with a 2-tick buffer = 0.5, rr 2, swing trail).

The canonical long day (1m bars from Monday 2026-03-02 09:30 New York):

    idx 0-4  09:30-09:34  opening range: high 5004, low 4999
    idx 5    09:35        close 5005.5 > 5004 -> bias +1
    idx 6    09:36        low 5005 > high[2] 5003 -> bullish FVG [5003, 5005], form bar 6
    idx 7    09:37        low 5005.5: no touch
    idx 8    09:38        low 5004.75 <= 5005 -> signal, close 5006
                          est stop 5003 - 0.5 = 5002.5, dist 3.5, qty floor(500 / 17.5) = 28,
                          est TP 5006 + 2 x 3.5 = 5013
    idx 9    09:39        fill at the open 5006.5: stop 5002.5, TP 5006.5 + 2 x 4 = 5014.5
    idx 10   09:40        high 5015 trades through 5014.5 -> target at 5014.5, +8 x 28 x 5 = 1120
"""

import dataclasses
import datetime as dt
from collections.abc import Sequence
from dataclasses import dataclass, field
from typing import Final
from zoneinfo import ZoneInfo

import pytest
from pydantic import ValidationError

from propbot.backtest.engine import run_backtest
from propbot.backtest.model import EngineResult, ExitReason, FillModel
from propbot.core.bars import Bar, BarSeries, DataSource
from propbot.core.instrument import Instrument
from propbot.data.sessions import session_last_flags
from propbot.signals.schema import STRATEGY_NAMES
from propbot.strategies.base import Action, CloseAll, Entry, Exit, FillEvent, Side, StrategyContext
from propbot.strategies.ny_orb_fvg import (
    BEARISH,
    BULLISH,
    DEAD,
    FRESH,
    INVERTED,
    PINE_FILE,
    STRATEGY_ID,
    Fvg,
    NyOrbFvg,
    NyOrbFvgParams,
)
from support.engine import cfd, make_config
from support.pine import pine_defaults, pine_inputs

NY: Final = ZoneInfo("America/New_York")
MONDAY: Final = dt.date(2026, 3, 2)
TUESDAY: Final = dt.date(2026, 3, 3)
PREVIOUS_FRIDAY: Final = dt.date(2026, 2, 27)

type OHLC = tuple[float, float, float, float]

# ---- the canonical long day (see the module docstring) --------------------------------
OPENING_RANGE: Final[list[OHLC]] = [
    (5000.0, 5002.0, 4999.0, 5001.0),  # 0 09:30
    (5001.0, 5003.0, 5000.0, 5002.0),  # 1
    (5002.0, 5004.0, 5001.0, 5003.0),  # 2
    (5003.0, 5004.0, 5001.0, 5002.0),  # 3
    (5002.0, 5003.0, 5000.0, 5001.0),  # 4 09:34
]
LONG_SETUP: Final[list[OHLC]] = [
    *OPENING_RANGE,
    (5001.0, 5006.0, 5001.0, 5005.5),  # 5 09:35 bias +1
    (5005.5, 5010.0, 5005.0, 5009.5),  # 6 09:36 bullish FVG [5003, 5005]
    (5009.5, 5009.5, 5005.5, 5008.0),  # 7 09:37 no touch
]
FLAT: Final[OHLC] = (5008.0, 5008.0, 5008.0, 5008.0)  # forms no gap and touches nothing
TOUCH: Final[OHLC] = (5008.0, 5008.0, 5004.75, 5006.0)
FILL_BAR: Final[OHLC] = (5006.5, 5008.0, 5006.0, 5007.5)
TP_BAR: Final[OHLC] = (5007.5, 5015.0, 5007.0, 5014.0)
CANONICAL: Final[list[OHLC]] = [*LONG_SETUP, TOUCH, FILL_BAR, TP_BAR]

# ---- the mirrored short day: OR as above -----------------------------------------------
SHORT_SETUP: Final[list[OHLC]] = [
    *OPENING_RANGE,
    (5001.0, 5001.0, 4997.0, 4997.5),  # 5 close < 4999 -> bias -1
    (4997.5, 4998.0, 4993.0, 4993.5),  # 6 high 4998 < low[2] 5000 -> bearish FVG [4998, 5000]
    (4993.5, 4997.5, 4993.5, 4995.0),  # 7 high 4997.5: no touch
]
SHORT_TOUCH: Final[OHLC] = (4995.0, 4998.25, 4995.0, 4997.0)
SHORT_FILL_BAR: Final[OHLC] = (4996.5, 4997.0, 4995.0, 4995.5)


# ---- helpers ---------------------------------------------------------------------------


def ny_ms(day: dt.date, hour: int, minute: int) -> int:
    return (
        int(dt.datetime(day.year, day.month, day.day, hour, minute, tzinfo=NY).timestamp()) * 1000
    )


def build(*segments: tuple[int, Sequence[OHLC]], tf_seconds: int = 60) -> BarSeries:
    """Bars of consecutive `tf_seconds` steps per segment; segments start where they say."""
    bars = [
        Bar(ts_open_ms=start + i * tf_seconds * 1000, open=o, high=h, low=lo, close=c, volume=1.0)
        for start, rows in segments
        for i, (o, h, lo, c) in enumerate(rows)
    ]
    source = DataSource(name="test", format="generic", sha256="", synthetic=True, bar_tz="UTC")
    return BarSeries(symbol="TEST", tf_seconds=tf_seconds, bars=tuple(bars), source=source)


def day(rows: Sequence[OHLC], when: dt.date = MONDAY, hour: int = 9, minute: int = 30) -> BarSeries:
    return build((ny_ms(when, hour, minute), rows))


@dataclass(frozen=True)
class Snap:
    """The Pine state after one bar."""

    or_high: float | None
    or_low: float | None
    or_ready: bool
    bias: int
    traded_today: bool
    fvgs: tuple[Fvg, ...]
    in_dir: int
    in_initial_stop: float | None
    in_trail_stop: float | None
    in_take_profit: float | None
    halted: bool
    halted_dd: bool
    halted_streak: bool
    day_base_eq: float | None


@dataclass(eq=False)
class Recorder:
    """Wraps the port: records every action batch and the state after each bar."""

    inner: NyOrbFvg
    actions: dict[int, list[Action]] = field(default_factory=dict)
    fill_actions: list[tuple[FillEvent, list[Action]]] = field(default_factory=list)
    snaps: list[Snap] = field(default_factory=list)

    @property
    def strategy_id(self) -> str:
        return self.inner.strategy_id

    @property
    def pyramiding(self) -> int:
        return self.inner.pyramiding

    def on_bar(self, bar: Bar, ctx: StrategyContext) -> Sequence[Action]:
        acts = list(self.inner.on_bar(bar, ctx))
        self.actions[ctx.bar_index] = acts
        s = self.inner
        self.snaps.append(
            Snap(
                or_high=s.or_high,
                or_low=s.or_low,
                or_ready=s.or_ready,
                bias=s.bias,
                traded_today=s.traded_today,
                fvgs=tuple(dataclasses.replace(f) for f in s.fvgs),
                in_dir=s.in_dir,
                in_initial_stop=s.in_initial_stop,
                in_trail_stop=s.in_trail_stop,
                in_take_profit=s.in_take_profit,
                halted=s.halted,
                halted_dd=s.halted_dd,
                halted_streak=s.halted_streak,
                day_base_eq=s.day_base_eq,
            )
        )
        return acts

    def on_fill(self, fill: FillEvent, ctx: StrategyContext) -> Sequence[Action]:
        acts = list(self.inner.on_fill(fill, ctx))
        self.fill_actions.append((fill, acts))
        return acts

    def entries(self) -> list[tuple[int, Entry]]:
        return [(i, a) for i, batch in self.actions.items() for a in batch if isinstance(a, Entry)]

    def exits_at(self, index: int) -> list[Exit]:
        return [a for a in self.actions.get(index, []) if isinstance(a, Exit)]


def run_ny(
    series: BarSeries,
    params: NyOrbFvgParams | None = None,
    *,
    fill_model: FillModel = FillModel.PESSIMISTIC,
    instrument: Instrument | None = None,
    session_last: Sequence[bool] | None = None,
) -> tuple[EngineResult, Recorder]:
    recorder = Recorder(NyOrbFvg(params))
    flags = (
        session_last
        if session_last is not None
        else session_last_flags(series.bars, series.tf_seconds)
    )
    config = make_config(instrument=instrument, fill_model=fill_model)
    return run_backtest(series, recorder, config, session_last=flags), recorder


def params(**changes: object) -> NyOrbFvgParams:
    return NyOrbFvgParams.model_validate(changes)


# ---- a) parameters vs the Pine inputs --------------------------------------------------

PORTED: Final = {
    "tz": "tz",
    "orStartHour": "or_start_hour",
    "orStartMin": "or_start_min",
    "orMinutes": "or_minutes",
    "sessEndHour": "sess_end_hour",
    "sessEndMin": "sess_end_min",
    "closeAtEnd": "close_at_end",
    "startDate": "start_date",
    "endDate": "end_date",
    "tradeMon": "trade_mon",
    "tradeTue": "trade_tue",
    "tradeWed": "trade_wed",
    "tradeThu": "trade_thu",
    "tradeFri": "trade_fri",
    "breakConfirm": "break_confirm",
    "allowLong": "allow_long",
    "allowShort": "allow_short",
    "useFVG": "use_fvg",
    "useIFVG": "use_ifvg",
    "minFvgTicks": "min_fvg_ticks",
    "riskPct": "risk_pct",
    "riskBase": "risk_base",
    "fixedBal": "fixed_bal",
    "rr": "rr",
    "stopMethod": "stop_method",
    "slBuf": "sl_buf",
    "atrLenInit": "atr_len_init",
    "atrMultInit": "atr_mult_init",
    "qtyRound": "qty_round",
    "useBreakers": "use_breakers",
    "dailyDDPct": "daily_dd_pct",
    "flattenBreach": "flatten_breach",
    "maxConsecLoss": "max_consec_loss",
    "useTrail": "use_trail",
    "trailMode": "trail_mode",
    "swingLen": "swing_len",
    "atrLenT": "atr_len_t",
    "atrMultT": "atr_mult_t",
    "trailPct": "trail_pct",
    "trailActR": "trail_act_r",
    "trailMinStepTicks": "trail_min_step_ticks",
}
# Visual, table or alert-only inputs: they place no order.
NOT_PORTED: Final = {
    "oosStart",
    "showDateViz",
    "showTable",
    "tableScope",
    "tablePos",
    "tableSize",
    "showAllFVG",
    "boxProj",
    "fvgProj",
    "alertToken",
}


def test_module_surface_names_the_script_and_a_known_strategy_id() -> None:
    assert STRATEGY_ID == "ny_orb_fvg"
    assert STRATEGY_ID in STRATEGY_NAMES
    assert PINE_FILE == "ny_open_fvg_strategy.pine"
    strategy = NyOrbFvg()
    assert strategy.strategy_id == STRATEGY_ID
    assert strategy.pyramiding == 0  # strategy(pyramiding = 0)


def test_every_pine_input_is_either_ported_or_deliberately_left_out() -> None:
    assert set(PORTED) | NOT_PORTED == set(pine_inputs(PINE_FILE))
    assert not set(PORTED) & NOT_PORTED
    assert set(PORTED.values()) == set(NyOrbFvgParams.model_fields)


def test_param_defaults_equal_the_pine_defaults() -> None:
    defaults = pine_defaults(PINE_FILE)
    model = NyOrbFvgParams()
    for pine_name, field_name in PORTED.items():
        assert getattr(model, field_name) == defaults[pine_name], pine_name


def test_date_defaults_are_utc_datetimes() -> None:
    model = NyOrbFvgParams()
    assert model.start_date == dt.datetime(2024, 1, 1, tzinfo=dt.UTC)
    assert model.end_date == dt.datetime(2030, 1, 1, tzinfo=dt.UTC)
    assert model.start_date.utcoffset() == dt.timedelta(0)


def test_params_convert_an_aware_date_to_utc_and_refuse_a_naive_one() -> None:
    plus3 = dt.timezone(dt.timedelta(hours=3))
    model = params(start_date=dt.datetime(2026, 1, 1, 3, 0, tzinfo=plus3))
    assert model.start_date == dt.datetime(2026, 1, 1, tzinfo=dt.UTC)
    assert model.start_date.tzinfo is dt.UTC
    with pytest.raises(ValidationError):
        params(start_date=dt.datetime(2026, 1, 1))


@pytest.mark.parametrize(
    ("name", "value"),
    [
        ("or_start_hour", 24),
        ("or_start_min", 60),
        ("or_minutes", 0),
        ("or_minutes", 61),
        ("sess_end_hour", -1),
        ("sess_end_min", 60),
        ("min_fvg_ticks", -1),
        ("risk_pct", 0.05),
        ("fixed_bal", 0.5),
        ("rr", 0.0),
        ("sl_buf", -1),
        ("atr_len_init", 0),
        ("atr_mult_init", 0.0),
        ("daily_dd_pct", 0.0),
        ("max_consec_loss", 0),
        ("swing_len", 0),
        ("atr_len_t", 0),
        ("atr_mult_t", 0.05),
        ("trail_pct", 0.001),
        ("trail_act_r", -0.1),
        ("trail_min_step_ticks", 0),
        ("break_confirm", "close"),
        ("risk_base", "Equity"),
        ("stop_method", "ATR"),
        ("qty_round", "Whole"),
        ("trail_mode", "Swing"),
        ("tz", "Mars/Olympus"),
    ],
)
def test_params_refuse_values_the_pine_dialog_refuses(name: str, value: object) -> None:
    with pytest.raises(ValidationError):
        params(**{name: value})


def test_params_are_strict_frozen_and_closed() -> None:
    with pytest.raises(ValidationError):
        params(sl_buf="2")
    with pytest.raises(ValidationError):
        params(unknown=1)
    model = NyOrbFvgParams()
    with pytest.raises(ValidationError):
        model.rr = 3.0  # type: ignore[misc]


def test_two_instances_share_no_state() -> None:
    _, used = run_ny(day(CANONICAL))
    fresh = NyOrbFvg()
    assert used.inner.fvgs
    assert used.inner.or_ready
    assert fresh.fvgs == []
    assert not fresh.or_ready
    assert fresh.fvgs is not used.inner.fvgs


# ---- b) the canonical day --------------------------------------------------------------


@pytest.mark.parametrize("fill_model", list(FillModel))
def test_canonical_long_day_enters_on_the_touch_and_exits_at_the_rr_target(
    fill_model: FillModel,
) -> None:
    result, recorder = run_ny(day(CANONICAL), fill_model=fill_model)

    assert recorder.actions[8] == [
        Entry("Long", Side.LONG, 28.0, comment="Long"),
        Exit("Exit L", from_entry="Long", stop=5002.5, limit=5013.0),
    ]
    # Fill bar: the bracket from the real fill 5006.5 (risk 4, target 5006.5 + 8).
    assert recorder.exits_at(9) == [Exit("Exit L", from_entry="Long", stop=5002.5, limit=5014.5)]
    (trade,) = result.trades
    assert (trade.entry_id, trade.exit_id, trade.setup) == ("Long", "Exit L", "Long")
    assert (trade.entry_bar_index, trade.exit_bar_index) == (9, 10)
    assert (trade.entry_price, trade.exit_price, trade.qty) == (5006.5, 5014.5, 28.0)
    assert trade.reason is ExitReason.TARGET
    assert trade.initial_stop == 5002.5
    assert trade.pnl == pytest.approx(1120.0)


@pytest.mark.parametrize("fill_model", list(FillModel))
def test_canonical_short_day_mirrors_the_long_one(fill_model: FillModel) -> None:
    rows = [*SHORT_SETUP, SHORT_TOUCH, SHORT_FILL_BAR, (4995.5, 4996.0, 4988.0, 4988.5)]
    result, recorder = run_ny(day(rows), fill_model=fill_model)

    # est stop 5000 + 0.5 = 5000.5, dist 3.5, qty 28, est TP 4997 - 7 = 4990
    assert recorder.actions[8] == [
        Entry("Short", Side.SHORT, 28.0, comment="Short"),
        Exit("Exit S", from_entry="Short", stop=5000.5, limit=4990.0),
    ]
    # fill 4996.5: stop max(5000.5, 4996.75) = 5000.5, TP 4996.5 - 2 x 4 = 4988.5
    assert recorder.exits_at(9) == [Exit("Exit S", from_entry="Short", stop=5000.5, limit=4988.5)]
    (trade,) = result.trades
    assert trade.side is Side.SHORT
    assert (trade.entry_price, trade.exit_price) == (4996.5, 4988.5)
    assert trade.reason is ExitReason.TARGET
    assert trade.pnl == pytest.approx(1120.0)


# ---- opening range and bias ------------------------------------------------------------


def test_opening_range_covers_bars_opening_in_or_start_to_or_end_and_is_ready_at_or_end() -> None:
    rows = [(5000.0, 5050.0, 4950.0, 5000.0), *OPENING_RANGE, (5001.0, 5003.0, 5000.0, 5002.0)]
    _, recorder = run_ny(day(rows, minute=29))  # 09:29 .. 09:35

    assert recorder.snaps[0].or_high is None  # 09:29 is outside [09:30, 09:35)
    assert (recorder.snaps[5].or_high, recorder.snaps[5].or_low) == (5004.0, 4999.0)
    assert not recorder.snaps[5].or_ready  # 09:34
    assert recorder.snaps[6].or_ready  # 09:35 = orEnd
    assert (recorder.snaps[6].or_high, recorder.snaps[6].or_low) == (5004.0, 4999.0)


def test_opening_range_on_five_minute_bars_is_the_0930_bar() -> None:
    rows = [(5000.0, 5004.0, 4999.0, 5001.0), (5001.0, 5003.0, 5000.0, 5002.0)]
    _, recorder = run_ny(build((ny_ms(MONDAY, 9, 30), rows), tf_seconds=300))
    assert (recorder.snaps[0].or_high, recorder.snaps[0].or_low) == (5004.0, 4999.0)
    assert not recorder.snaps[0].or_ready
    assert recorder.snaps[1].or_ready
    assert recorder.snaps[1].or_high == 5004.0  # the 09:35 bar is not part of the range


def test_bias_by_close_needs_a_close_beyond_the_range_and_wick_does_not() -> None:
    rows = [*OPENING_RANGE, (5001.0, 5005.0, 5001.0, 5003.5)]  # high > 5004, close inside
    _, by_close = run_ny(day(rows))
    _, by_wick = run_ny(day(rows), params(break_confirm="Wick"))
    assert by_close.snaps[5].bias == 0
    assert by_wick.snaps[5].bias == 1


def test_short_bias_by_close_and_by_wick() -> None:
    _, by_close = run_ny(day(SHORT_SETUP))
    assert [s.bias for s in by_close.snaps[4:6]] == [0, -1]
    rows = [*OPENING_RANGE, (5001.0, 5001.0, 4998.75, 4999.5)]  # low < 4999, close inside
    _, close_mode = run_ny(day(rows))
    _, wick_mode = run_ny(day(rows), params(break_confirm="Wick"))
    assert close_mode.snaps[5].bias == 0
    assert wick_mode.snaps[5].bias == -1


def test_bias_is_fixed_for_the_day_once_set() -> None:
    rows = [*LONG_SETUP, (5008.0, 5008.0, 4990.0, 4990.0)]  # closes below the OR low later
    _, recorder = run_ny(day(rows))
    assert recorder.snaps[-1].bias == 1


def test_a_breakout_in_a_disabled_direction_sets_no_bias_and_scanning_goes_on() -> None:
    rows = [
        *OPENING_RANGE,
        (5001.0, 5006.0, 5001.0, 5005.5),  # 5 close above: longs are off
        (5005.5, 5005.5, 4997.0, 4998.0),  # 6 close below 4999 -> bias -1
    ]
    _, recorder = run_ny(day(rows), params(allow_long=False))
    assert recorder.snaps[5].bias == 0
    assert recorder.snaps[5].fvgs == ()  # no detection without a bias
    assert recorder.snaps[6].bias == -1


# ---- FVG detection, FIFO and the state machine -----------------------------------------


def test_bullish_fvg_is_recorded_with_high2_as_bottom_and_low_as_top() -> None:
    _, recorder = run_ny(day(LONG_SETUP))
    assert recorder.snaps[6].fvgs == (Fvg(5005.0, 5003.0, BULLISH, FRESH, 6, -1),)


def test_bearish_fvg_is_recorded_with_high_as_bottom_and_low2_as_top() -> None:
    _, recorder = run_ny(day(SHORT_SETUP))
    assert recorder.snaps[6].fvgs == (Fvg(5000.0, 4998.0, BEARISH, FRESH, 6, -1),)


@pytest.mark.parametrize(("setup", "ticks", "kept"), [
    (LONG_SETUP, 8, 1), (LONG_SETUP, 9, 0), (SHORT_SETUP, 8, 1), (SHORT_SETUP, 9, 0),
])  # fmt: skip
def test_min_fvg_ticks_filters_smaller_gaps(setup: list[OHLC], ticks: int, kept: int) -> None:
    # Both gaps are 2 points = 8 ticks.
    _, recorder = run_ny(day(setup), params(min_fvg_ticks=ticks))
    assert len(recorder.snaps[-1].fvgs) == kept


def test_tracked_gaps_are_capped_at_60_oldest_first() -> None:
    # A staircase: low_k = 5007 + 2k > high_(k-2) = low_k - 3, so every bar forms a gap,
    # and no later bar comes back down to an older top.
    stairs = [(5007.0 + 2 * k, 5008.0 + 2 * k, 5007.0 + 2 * k, 5008.0 + 2 * k) for k in range(70)]
    rows = [*OPENING_RANGE, (5001.0, 5006.0, 5001.0, 5005.5), *stairs]
    _, recorder = run_ny(day(rows))

    assert max(len(s.fvgs) for s in recorder.snaps) == 60
    final = recorder.snaps[-1].fvgs
    assert len(final) == 60
    assert (final[0].form_bar, final[-1].form_bar) == (16, 75)  # gaps of bars 6..15 dropped
    assert recorder.entries() == []


def test_bullish_gap_goes_fresh_inverted_dead_before_the_entry_check() -> None:
    rows = [
        *LONG_SETUP,
        (5008.0, 5008.0, 5002.0, 5002.5),  # 8 close < 5003: inverted, although low <= top
        (5002.5, 5005.5, 5002.5, 5005.25),  # 9 close > 5005: dead
    ]
    _, recorder = run_ny(day(rows))
    assert recorder.snaps[8].fvgs[0].state == INVERTED
    assert recorder.snaps[8].fvgs[0].flip_bar == 8
    assert recorder.entries() == []  # the touch on bar 8 met an already inverted gap
    assert recorder.snaps[9].fvgs[0].state == DEAD


def test_bearish_gap_goes_fresh_inverted_dead() -> None:
    rows = [
        *SHORT_SETUP,
        (4995.0, 5001.0, 4995.0, 5000.5),  # 8 close > 5000: inverted (high >= bot too)
        (5000.5, 5000.5, 4997.0, 4997.5),  # 9 close < 4998: dead
    ]
    _, recorder = run_ny(day(rows))
    assert recorder.snaps[8].fvgs[0].state == INVERTED
    assert recorder.snaps[8].fvgs[0].flip_bar == 8
    assert recorder.entries() == []
    assert recorder.snaps[9].fvgs[0].state == DEAD


def test_short_ifvg_entry_waits_for_the_bar_after_the_flip() -> None:
    rows = [
        *OPENING_RANGE,
        (5001.0, 5001.0, 4997.0, 4997.5),  # 5 bias -1
        (5004.25, 5007.0, 5004.0, 5006.5),  # 6 gaps up; low 5004 > high[2] 5003: FVG [5003, 5004]
        (5006.5, 5006.5, 5000.5, 5001.0),  # 7 close < 5003: inverted (flip bar 7), high >= 5003
        (5001.0, 5004.0, 5000.75, 5002.0),  # 8 high 5004 >= 5003: short IFVG entry
        (5002.0, 5002.5, 5001.5, 5002.0),  # 9 the fill bar
    ]
    _, recorder = run_ny(day(rows))
    assert recorder.snaps[7].fvgs[0].state == INVERTED
    assert 7 not in dict(recorder.entries())  # the flip bar itself is no activation
    # est stop 5004 + 0.5 = 5004.5, dist 2.5, qty 500 / 12.5 = 40, est TP 5002 - 5 = 4997
    assert recorder.actions[8] == [
        Entry("Short", Side.SHORT, 40.0, comment="Short"),
        Exit("Exit S", from_entry="Short", stop=5004.5, limit=4997.0),
    ]


def test_no_long_entry_on_the_form_bar_even_though_its_low_is_the_top() -> None:
    _, recorder = run_ny(day(LONG_SETUP))
    # Bar 6 formed the gap with low == top (a touch); bar 7 does not reach it.
    assert recorder.entries() == []


def test_use_fvg_off_ignores_fresh_gaps() -> None:
    _, recorder = run_ny(day(CANONICAL), params(use_fvg=False))
    assert recorder.entries() == []


def test_use_ifvg_off_ignores_inverted_gaps() -> None:
    rows = [
        *OPENING_RANGE,
        (5001.0, 5001.0, 4997.0, 4997.5),
        (5004.25, 5007.0, 5004.0, 5006.5),
        (5006.5, 5006.5, 5000.5, 5001.0),
        (5001.0, 5004.0, 5000.75, 5002.0),
        (5002.0, 5002.5, 5001.5, 5002.0),
    ]
    _, recorder = run_ny(day(rows), params(use_ifvg=False))
    assert recorder.entries() == []


def test_the_newest_touched_gap_is_taken_first() -> None:
    rows = [
        *LONG_SETUP,  # gap 1 [5003, 5005], form bar 6
        (5008.0, 5012.0, 5008.0, 5011.5),  # 8
        (5011.5, 5014.0, 5011.0, 5013.5),  # 9 low 5011 > high[2] 5009.5: gap 2 [5009.5, 5011]
        (5013.5, 5013.5, 5004.75, 5010.0),  # 10 touches both, closes above both bottoms
        (5010.0, 5010.5, 5009.75, 5010.25),  # 11 the fill bar
    ]
    _, recorder = run_ny(day(rows))
    assert len(recorder.snaps[10].fvgs) == 2
    # Gap 2: stop 5009.5 - 0.5 = 5009, dist 1, qty 500 / 5 = 100, est TP 5010 + 2 = 5012.
    assert recorder.actions[10] == [
        Entry("Long", Side.LONG, 100.0, comment="Long"),
        Exit("Exit L", from_entry="Long", stop=5009.0, limit=5012.0),
    ]


# ---- stops, targets and the fill bar ---------------------------------------------------


@pytest.mark.parametrize("fill_model", list(FillModel))
def test_atr_stop_distance_is_captured_on_the_signal_bar_and_re_applied_to_the_fill(
    fill_model: FillModel,
) -> None:
    stop_bar = (5007.5, 5008.0, 4999.0, 5000.0)
    rows = [*LONG_SETUP, TOUCH, FILL_BAR, stop_bar]
    p = params(stop_method="ATR Based", atr_len_init=1, atr_mult_init=2.0)
    result, recorder = run_ny(day(rows), p, fill_model=fill_model)

    # ATR(1) on the signal bar = its true range max(5008 - 5004.75, |5004.75 - 5008|) = 3.25
    # dist 6.5: est stop 5006 - 6.5 = 4999.5, qty floor(500 / 32.5) = 15, est TP 5019.
    assert recorder.actions[8] == [
        Entry("Long", Side.LONG, 15.0, comment="Long"),
        Exit("Exit L", from_entry="Long", stop=4999.5, limit=5019.0),
    ]
    # fill 5006.5: stop 5006.5 - 6.5 = 5000, TP 5006.5 + 13 = 5019.5
    assert recorder.exits_at(9) == [Exit("Exit L", from_entry="Long", stop=5000.0, limit=5019.5)]
    (trade,) = result.trades
    assert trade.initial_stop == 5000.0
    assert (trade.exit_price, trade.reason) == (5000.0, ExitReason.STOP)
    assert trade.pnl == pytest.approx(-6.5 * 15 * 5)


def test_fvg_buffer_stop_is_the_structural_level_whatever_the_fill() -> None:
    stop_bar = (5007.5, 5008.0, 4999.0, 5000.0)
    result, _ = run_ny(day([*LONG_SETUP, TOUCH, FILL_BAR, stop_bar]))
    (trade,) = result.trades
    assert trade.initial_stop == 5002.5
    assert (trade.exit_price, trade.reason) == (5002.5, ExitReason.STOP)


def test_atr_method_falls_back_to_the_fvg_level_while_atr_is_na() -> None:
    # ATR(50) has no value within these 12 bars: Pine's `not na(atrInit)` fails.
    result, recorder = run_ny(day(CANONICAL), params(stop_method="ATR Based", atr_len_init=50))
    assert recorder.actions[8][1] == Exit("Exit L", from_entry="Long", stop=5002.5, limit=5013.0)
    assert result.trades[0].initial_stop == 5002.5


def test_signal_stop_is_forced_one_tick_below_the_close() -> None:
    # slBuf 0 and a signal close exactly at the gap bottom: 5003 -> min(5003, 5002.75)
    touch_at_bottom = (5008.0, 5008.0, 5003.0, 5003.0)
    fill_bar = (5003.0, 5003.25, 5003.0, 5003.25)
    _, recorder = run_ny(day([*LONG_SETUP, touch_at_bottom, fill_bar]), params(sl_buf=0))
    # dist 0.25: qty 500 / 1.25 = 400, est TP 5003 + 0.5
    assert recorder.actions[8] == [
        Entry("Long", Side.LONG, 400.0, comment="Long"),
        Exit("Exit L", from_entry="Long", stop=5002.75, limit=5003.5),
    ]


def test_fill_stop_is_forced_one_tick_below_a_fill_that_gapped_onto_the_fvg_stop() -> None:
    gap_fill_bar = (5002.5, 5003.5, 5002.5, 5003.0)
    result, recorder = run_ny(day([*LONG_SETUP, TOUCH, gap_fill_bar]))
    # fill 5002.5: stop min(5002.5, 5002.25) = 5002.25, TP 5002.5 + 2 x 0.25 = 5003
    ((fill, fill_exits),) = recorder.fill_actions
    assert fill.price == 5002.5
    assert fill_exits == [Exit("Exit L", from_entry="Long", stop=5002.25, limit=5003.0)]
    (trade,) = result.trades
    assert trade.initial_stop == 5002.25
    assert (trade.exit_price, trade.reason) == (5003.0, ExitReason.TARGET)


def test_tv_ohlc_fill_bar_is_protected_by_the_signal_bracket_until_its_close() -> None:
    # Pine's own timing: the gapped open sits on the signal-time stop 5002.5, which is in
    # force until the fill bar closes, so the trade is stopped at the open.
    gap_fill_bar = (5002.5, 5003.5, 5002.5, 5003.0)
    result, recorder = run_ny(day([*LONG_SETUP, TOUCH, gap_fill_bar]), fill_model=FillModel.TV_OHLC)
    assert recorder.fill_actions == []  # the engine does not call on_fill in tv_ohlc
    (trade,) = result.trades
    assert (trade.entry_price, trade.exit_price, trade.reason) == (5002.5, 5002.5, ExitReason.STOP)


def test_both_fill_models_derive_the_same_levels_and_differ_only_in_timing() -> None:
    # The fill bar reaches 5013.5: through the signal-time target 5013 but not through the
    # re-priced 5014.5.
    fill_bar = (5006.5, 5013.5, 5006.0, 5007.5)
    rows = [*LONG_SETUP, TOUCH, fill_bar, TP_BAR]
    pessimistic, p_rec = run_ny(day(rows))
    tv, _tv_rec = run_ny(day(rows), fill_model=FillModel.TV_OHLC)

    expected = Exit("Exit L", from_entry="Long", stop=5002.5, limit=5014.5)
    assert p_rec.fill_actions[0][1] == [expected]  # at the fill instant
    assert p_rec.exits_at(9) == [expected]  # and again at the fill bar's close
    # tv_ohlc: the signal bracket worked during the fill bar (open nearer the low: O-L-H-C).
    (tv_trade,) = tv.trades
    assert (tv_trade.exit_bar_index, tv_trade.exit_price) == (9, 5013.0)
    (p_trade,) = pessimistic.trades
    assert (p_trade.exit_bar_index, p_trade.exit_price) == (10, 5014.5)


@pytest.mark.parametrize("stop_method", ["FVG Buffer", "ATR Based"])
def test_on_fill_and_the_fill_bar_close_compute_identical_brackets(stop_method: str) -> None:
    quiet_fill_bar = (5006.5, 5007.0, 5006.25, 5006.75)
    rows = [*LONG_SETUP, TOUCH, quiet_fill_bar, FLAT]
    p = params(stop_method=stop_method, atr_len_init=1, atr_mult_init=2.0)
    _, p_rec = run_ny(day(rows), p)
    _, tv_rec = run_ny(day(rows), p, fill_model=FillModel.TV_OHLC)
    assert p_rec.fill_actions[0][1] == tv_rec.exits_at(9)
    assert p_rec.exits_at(9) == tv_rec.exits_at(9)
    assert p_rec.snaps[9].in_initial_stop == tv_rec.snaps[9].in_initial_stop


# ---- trailing --------------------------------------------------------------------------

TRAIL_FILL_BAR: Final[OHLC] = (5006.5, 5008.0, 5005.5, 5007.5)
RUN_UP: Final[list[OHLC]] = [
    (5007.5, 5011.0, 5007.0, 5010.25),  # 10 profit 3.75 < 1R (4): no trail
    (5010.25, 5012.0, 5010.0, 5011.5),  # 11
    (5011.5, 5013.0, 5011.0, 5012.5),  # 12
    (5012.5, 5014.0, 5012.0, 5013.5),  # 13
    (5013.5, 5014.0, 5013.0, 5013.75),  # 14
    (5013.75, 5014.0, 5013.5, 5013.75),  # 15
    (5013.75, 5014.0, 5009.0, 5009.5),  # 16 falls back
]
TRAIL_ROWS: Final[list[OHLC]] = [*LONG_SETUP, TOUCH, TRAIL_FILL_BAR, *RUN_UP]


def trail_stops(recorder: Recorder) -> list[float | None]:
    return [recorder.exits_at(i)[0].stop for i in range(9, 16)]


def test_swing_trail_follows_the_swing_low_in_min_steps_from_the_real_fill() -> None:
    result, recorder = run_ny(day(TRAIL_ROWS), params(rr=10.0))
    # Fill 5006.5, risk 4. Candidate = lowest(low, 5) - 0.5; min step 4 ticks = 1.0.
    #  9: no profit          10: 3.75 < 4 (from the signal close it would be 4.25 >= 3.5)
    # 11: lows 7..11 -> 5004.75 -> 5004.25 (+1.75)   12: same candidate (+0)
    # 13: lows 9..13 -> 5005.5 -> 5005.0 (+0.75 < 1)  14: 5007 -> 5006.5   15: 5010 -> 5009.5
    assert trail_stops(recorder) == [
        5002.5, 5002.5, 5004.25, 5004.25, 5004.25, 5006.5, 5009.5,
    ]  # fmt: skip
    assert all(e.limit == 5046.5 for i in range(9, 16) for e in recorder.exits_at(i))
    (trade,) = result.trades
    assert (trade.exit_price, trade.reason) == (5009.5, ExitReason.STOP)
    assert trade.initial_stop == 5002.5


def test_percent_trail_moves_only_by_at_least_the_min_step() -> None:
    result, recorder = run_ny(
        day(TRAIL_ROWS), params(rr=10.0, trail_mode="Percent step", trail_pct=0.1)
    )
    first = 5011.5 * (1 - 0.1 / 100)  # 5006.4885
    # bar 12: 5012.5 x 0.999 = 5007.4875 improves by 0.999 < 1.0: no move
    third = 5013.5 * (1 - 0.1 / 100)  # 5008.4865
    # bar 14: 5008.73625 (+0.25), bar 15: same: no move
    assert trail_stops(recorder) == [5002.5, 5002.5, first, first, third, third, third]
    # Bar 16 dips to 5009 only, above the trailed stop: the session-end close takes it.
    (trade,) = result.trades
    assert (trade.reason, trade.comment, trade.exit_price) == (
        ExitReason.CLOSE,
        "EOD close",
        5009.5,
    )


def test_atr_step_trail_uses_the_trailing_atr() -> None:
    result, recorder = run_ny(
        day(TRAIL_ROWS), params(rr=10.0, trail_mode="ATR step", atr_len_t=1, atr_mult_t=1.5)
    )
    # ATR(1) = true range: bar 11: 2 -> 5011.5 - 3 = 5008.5; 12: 2 -> 5009.5 (+1.0);
    # 13: 2 -> 5010.5; 14: 1 -> 5012.25; 15: 0.5 -> 5013.0 (+0.75 < 1: no move)
    assert trail_stops(recorder) == [5002.5, 5002.5, 5008.5, 5009.5, 5010.5, 5012.25, 5012.25]
    (trade,) = result.trades
    assert (trade.exit_price, trade.reason) == (5012.25, ExitReason.STOP)


def test_trail_off_keeps_the_initial_stop() -> None:
    _, recorder = run_ny(day(TRAIL_ROWS), params(rr=10.0, use_trail=False))
    assert trail_stops(recorder) == [5002.5] * 7


def test_short_percent_trail_moves_down() -> None:
    rows = [
        *SHORT_SETUP,
        SHORT_TOUCH,
        SHORT_FILL_BAR,  # fill 4996.5, stop 5000.5, risk 4
        (4995.5, 4996.0, 4991.0, 4991.5),  # profit 5: 4991.5 x 1.001 = 4996.4915
        (4991.5, 4997.0, 4991.0, 4996.75),  # high reaches the trailed stop
    ]
    result, recorder = run_ny(day(rows), params(rr=10.0, trail_mode="Percent step", trail_pct=0.1))
    trailed = 4991.5 * (1 + 0.1 / 100)
    assert recorder.exits_at(10) == [
        Exit("Exit S", from_entry="Short", stop=trailed, limit=4996.5 - 40.0)
    ]
    (trade,) = result.trades
    assert (trade.exit_price, trade.reason) == (trailed, ExitReason.STOP)


# ---- the entry-time gate and the session end -------------------------------------------


LAST_BAR: Final[OHLC] = (5007.5, 5008.0, 5007.0, 5007.5)


def gate_day_1m(touch_hour: int, touch_minute: int) -> BarSeries:
    """The long setup, flat bars from 09:38, the touch, then bars up to 15:59."""
    touch_min = touch_hour * 60 + touch_minute
    filler = [FLAT] * (touch_min - (9 * 60 + 38))
    tail = [FILL_BAR, LAST_BAR][-(15 * 60 + 59 - touch_min) :]
    return day([*LONG_SETUP, *filler, TOUCH, *tail])


def test_on_one_minute_bars_1557_is_the_last_signal_bar_and_the_trade_closes_at_1559() -> None:
    series = gate_day_1m(15, 57)
    result, recorder = run_ny(series)
    ((index, _entry),) = recorder.entries()
    assert series.bars[index].ts_open_ms == ny_ms(MONDAY, 15, 57)
    (trade,) = result.trades
    assert series.bars[trade.exit_bar_index].ts_open_ms == ny_ms(MONDAY, 15, 59)
    assert (trade.reason, trade.comment, trade.exit_price) == (
        ExitReason.CLOSE,
        "EOD close",
        5007.5,
    )
    assert recorder.actions[trade.exit_bar_index][-1] == CloseAll(comment="EOD close")


def test_on_one_minute_bars_a_touch_at_1558_does_not_enter() -> None:
    result, recorder = run_ny(gate_day_1m(15, 58))
    assert recorder.entries() == []
    assert result.trades == ()


FIVE_MIN_SETUP: Final[list[OHLC]] = [
    (5000.0, 5004.0, 4999.0, 5001.0),  # 09:30 the range: 5004 / 4999
    (5001.0, 5006.0, 5001.0, 5005.5),  # 09:35 bias +1
    (5005.5, 5010.0, 5005.0, 5009.5),  # 09:40 low 5005 > 5004: FVG [5004, 5005]
    (5009.5, 5009.5, 5005.5, 5008.0),  # 09:45 no touch
]


def gate_day_5m(touch_hour: int, touch_minute: int) -> BarSeries:
    touch_min = touch_hour * 60 + touch_minute
    filler = [FLAT] * ((touch_min - (9 * 60 + 50)) // 5)
    tail = [FILL_BAR, LAST_BAR][-((16 * 60 - touch_min) // 5) :]  # up to the 16:00 bar
    return build((ny_ms(MONDAY, 9, 30), [*FIVE_MIN_SETUP, *filler, TOUCH, *tail]), tf_seconds=300)


def test_on_five_minute_bars_the_last_signal_bar_is_1550() -> None:
    # curMin + barMin < sessEnd: 950 + 5 = 955 < 959; 955 + 5 = 960 is not.
    series = gate_day_5m(15, 50)
    result, recorder = run_ny(series)
    ((index, entry),) = recorder.entries()
    assert series.bars[index].ts_open_ms == ny_ms(MONDAY, 15, 50)
    # stop 5004 - 0.5 = 5003.5, dist 2.5, qty 40
    assert entry.qty == 40.0
    (trade,) = result.trades
    # The 15:55 fill bar is before 15:59; the 16:00 bar closes the trade.
    assert series.bars[trade.exit_bar_index].ts_open_ms == ny_ms(MONDAY, 16, 0)
    assert trade.comment == "EOD close"


def test_on_five_minute_bars_a_touch_at_1555_does_not_enter() -> None:
    _, recorder = run_ny(gate_day_5m(15, 55))
    assert recorder.entries() == []


def test_half_day_session_last_bar_closes_the_trade() -> None:
    filler = [FLAT] * (12 * 60 + 59 - (9 * 60 + 40) + 1)  # 09:40 .. 12:59
    series = day([*LONG_SETUP, TOUCH, FILL_BAR, *filler])
    result, _ = run_ny(series)
    (trade,) = result.trades
    assert trade.exit_bar_index == len(series) - 1
    assert (trade.reason, trade.comment, trade.exit_price) == (
        ExitReason.CLOSE,
        "EOD close",
        5008.0,
    )


def test_no_entry_on_the_sessions_last_bar() -> None:
    filler = [FLAT] * (12 * 60 + 58 - (9 * 60 + 38) + 1)  # 09:38 .. 12:58
    series = day([*LONG_SETUP, *filler, TOUCH])  # the touch is the 12:59 half-day close
    _, recorder = run_ny(series)
    assert recorder.entries() == []


def test_close_at_end_off_carries_the_position() -> None:
    series = day([*LONG_SETUP, TOUCH, FILL_BAR, FLAT, FLAT])
    result, recorder = run_ny(series, params(close_at_end=False))
    assert not any(isinstance(a, CloseAll) for batch in recorder.actions.values() for a in batch)
    assert result.trades[0].reason is ExitReason.END_OF_DATA


# ---- one trade per day and the day reset -----------------------------------------------


def test_one_trade_per_new_york_day_and_a_fresh_day_trades_again() -> None:
    retouch = (5014.0, 5014.0, 5004.75, 5006.0)
    series = build(
        (ny_ms(MONDAY, 9, 30), [*CANONICAL, retouch, FLAT]),
        (ny_ms(TUESDAY, 9, 30), CANONICAL),
    )
    result, recorder = run_ny(series)
    assert [i for i, _ in recorder.entries()] == [8, 13 + 8]
    assert len(result.trades) == 2
    tuesday_first = recorder.snaps[13]
    assert (tuesday_first.traded_today, tuesday_first.bias, tuesday_first.fvgs) == (False, 0, ())
    assert tuesday_first.or_high == 5002.0  # the range restarted with Tuesday's first bar
    assert tuesday_first.in_dir == 0


def test_day_reset_keeps_the_management_state_of_an_open_position() -> None:
    series = build(
        (ny_ms(MONDAY, 9, 30), [*LONG_SETUP, TOUCH, FILL_BAR, FLAT]),
        (ny_ms(TUESDAY, 9, 30), [FLAT, FLAT]),
    )
    _, recorder = run_ny(series, params(close_at_end=False))
    tuesday = recorder.snaps[11]
    assert (tuesday.in_dir, tuesday.in_initial_stop, tuesday.in_take_profit) == (1, 5002.5, 5014.5)
    assert (tuesday.traded_today, tuesday.bias, tuesday.fvgs) == (False, 0, ())
    assert recorder.exits_at(11) == [Exit("Exit L", from_entry="Long", stop=5002.5, limit=5014.5)]


# ---- circuit breakers ------------------------------------------------------------------

STOPPED_OUT: Final[list[OHLC]] = [*LONG_SETUP, TOUCH, FILL_BAR, (5007.5, 5008.0, 5000.0, 5001.0)]


def with_previous_day(rows: Sequence[OHLC]) -> BarSeries:
    """Pine's breakers baseline on a day CHANGE: a bar on the Friday before makes Monday one."""
    return build((ny_ms(PREVIOUS_FRIDAY, 9, 30), [FLAT]), (ny_ms(MONDAY, 9, 30), rows))


def test_daily_loss_breaker_trips_on_a_realized_loss() -> None:
    # Loss (5002.5 - 5006.5) x 28 x 5 = -560 <= -0.5 % of 100 000.
    result, recorder = run_ny(with_previous_day(STOPPED_OUT), params(daily_dd_pct=0.5))
    assert result.trades[0].pnl == pytest.approx(-560.0)
    assert not recorder.snaps[10].halted  # the fill bar
    assert recorder.snaps[11].halted_dd
    assert recorder.snaps[11].halted


def test_daily_loss_breaker_is_inert_on_the_charts_first_day() -> None:
    # Pine finding: dayStartEquity stays na until the first day change.
    result, recorder = run_ny(day(STOPPED_OUT), params(daily_dd_pct=0.1))
    assert result.trades[0].pnl < -100.0
    assert not recorder.snaps[-1].halted_dd


def test_floating_breach_flattens_at_the_bar_close() -> None:
    sag = (5007.5, 5008.0, 5002.75, 5002.75)  # open loss 3.75 x 28 x 5 = 525 >= 500
    result, recorder = run_ny(
        with_previous_day([*LONG_SETUP, TOUCH, FILL_BAR, sag, FLAT]), params(daily_dd_pct=0.5)
    )
    (trade,) = result.trades
    assert (trade.reason, trade.comment, trade.exit_price) == (
        ExitReason.CLOSE,
        "Daily DD halt",
        5002.75,
    )
    assert recorder.actions[11][-1] == CloseAll(comment="Daily DD halt")


def test_floating_breach_without_flatten_only_halts() -> None:
    sag = (5007.5, 5008.0, 5002.75, 5002.75)
    _, recorder = run_ny(
        with_previous_day([*LONG_SETUP, TOUCH, FILL_BAR, sag, FLAT]),
        params(daily_dd_pct=0.5, flatten_breach=False),
    )
    assert recorder.snaps[11].halted
    assert not any(isinstance(a, CloseAll) for a in recorder.actions[11])


def test_breakers_off_never_halt_or_flatten() -> None:
    sag = (5007.5, 5008.0, 5002.75, 5002.75)
    _, recorder = run_ny(
        with_previous_day([*LONG_SETUP, TOUCH, FILL_BAR, sag, FLAT]),
        params(daily_dd_pct=0.5, use_breakers=False),
    )
    assert recorder.snaps[11].halted_dd
    assert not recorder.snaps[11].halted
    assert not any(isinstance(a, CloseAll) for a in recorder.actions[11])


@pytest.mark.parametrize(("first_day", "base"), [(CANONICAL, 100_000.0), (STOPPED_OUT, 99_440.0)])
def test_day_base_equity_is_the_lower_of_day_start_equity_and_initial_capital(
    first_day: list[OHLC], base: float
) -> None:
    # Monday ends +1120 (base stays 100 000) or -560 (base 99 440).
    series = build((ny_ms(MONDAY, 9, 30), first_day), (ny_ms(TUESDAY, 9, 30), [FLAT]))
    _, recorder = run_ny(series)
    assert recorder.snaps[-1].day_base_eq == pytest.approx(base)


def test_consecutive_loss_breaker_trips_and_resets_next_day() -> None:
    series = build((ny_ms(MONDAY, 9, 30), STOPPED_OUT), (ny_ms(TUESDAY, 9, 30), [FLAT]))
    _, recorder = run_ny(series, params(max_consec_loss=1))
    assert recorder.snaps[10].halted_streak
    assert recorder.snaps[10].halted
    assert not recorder.snaps[11].halted_streak


@pytest.mark.parametrize(("use_breakers", "entries"), [(True, [8]), (False, [8, 18])])
def test_a_tripped_halt_blocks_the_days_entries(use_breakers: bool, entries: list[int]) -> None:
    # Monday's long is carried overnight (bars 0-9). Tuesday's 09:30 bar sets the baseline
    # (net 0); at 09:31 the trade stops at 5002.5: realized -560 <= -500 trips the 0.5 %
    # limit before Tuesday's own setup touches its gap at 09:38 (bar 18).
    tuesday = [
        (5004.0, 5004.0, 5003.0, 5003.5),  # 10 09:30 still open
        (5003.5, 5003.5, 5001.0, 5002.0),  # 11 09:31 stopped
        *OPENING_RANGE[2:],  # range 5004 / 5000
        *LONG_SETUP[5:],  # bias, FVG [5003, 5005]
        TOUCH,
        FILL_BAR,
    ]
    series = build(
        (ny_ms(MONDAY, 9, 30), [*LONG_SETUP, TOUCH, FILL_BAR]), (ny_ms(TUESDAY, 9, 30), tuesday)
    )
    p = params(daily_dd_pct=0.5, close_at_end=False, use_breakers=use_breakers)
    result, recorder = run_ny(series, p)
    assert result.trades[0].exit_bar_index == 11
    assert result.trades[0].pnl == pytest.approx(-560.0)
    assert recorder.snaps[11].halted is use_breakers
    assert [i for i, _ in recorder.entries()] == entries


def test_end_date_closes_the_open_trade_and_stops_new_entries() -> None:
    end = dt.datetime(2026, 3, 2, 14, 40, tzinfo=dt.UTC)  # 09:40 New York
    series = day([*LONG_SETUP, TOUCH, FILL_BAR, FLAT, FLAT, FLAT])
    result, _recorder = run_ny(series, params(end_date=end))
    (trade,) = result.trades
    assert trade.exit_bar_index == 11  # 09:41 is the first bar after 09:40
    assert (trade.reason, trade.comment) == (ExitReason.CLOSE, "End of test range")


def test_start_date_blocks_entries_before_it() -> None:
    start = dt.datetime(2026, 3, 2, 14, 39, tzinfo=dt.UTC)  # 09:39 New York
    _, recorder = run_ny(day(CANONICAL), params(start_date=start))
    assert recorder.entries() == []


# ---- weekday filter, risk base and sizing ----------------------------------------------


def test_weekday_filter_blocks_a_disabled_day() -> None:
    _, off = run_ny(day(CANONICAL), params(trade_mon=False))
    _, other_off = run_ny(day(CANONICAL), params(trade_tue=False))
    assert off.entries() == []
    assert len(other_off.entries()) == 1


@pytest.mark.parametrize(
    ("risk_base", "monday_qty", "tuesday_qty"),
    [
        # 2 % of 100 000 = 2000 / 17.5 = 114.28 -> 114 (Monday +8 x 114 x 5 = +4560)
        ("Initial Capital", 114.0, 114.0),
        # Tuesday: 2 % of 104 560 = 2091.2 / 17.5 = 119.49 -> 119
        ("Account Equity", 114.0, 119.0),
        # 2 % of 10 000 = 200 / 17.5 = 11.43 -> 11
        ("Fixed Balance", 11.0, 11.0),
    ],
)
def test_risk_base_sizes_from_the_chosen_balance(
    risk_base: str, monday_qty: float, tuesday_qty: float
) -> None:
    series = build((ny_ms(MONDAY, 9, 30), CANONICAL), (ny_ms(TUESDAY, 9, 30), CANONICAL))
    _, recorder = run_ny(series, params(risk_pct=2.0, risk_base=risk_base))
    assert [entry.qty for _, entry in recorder.entries()] == [monday_qty, tuesday_qty]


@pytest.mark.parametrize(
    ("qty_round", "expected"), [("Auto", 28.57), ("Fractional", 28.57), ("Whole units", 28.0)]
)
def test_quantity_rounding_on_a_fractional_instrument(qty_round: str, expected: float) -> None:
    # 500 / 17.5 = 28.571...; the CFD steps by 0.01.
    _, recorder = run_ny(
        day(CANONICAL),
        params(qty_round=qty_round),
        instrument=cfd(tick_size=0.25, point_value=5.0),
    )
    ((_, entry),) = recorder.entries()
    assert entry.qty == pytest.approx(expected)


def test_a_size_below_one_contract_skips_the_trade_and_keeps_scanning() -> None:
    # Fixed balance 1: 0.005 / 17.5 -> 0 contracts.
    _, recorder = run_ny(day(CANONICAL), params(risk_base="Fixed Balance", fixed_bal=1.0))
    assert recorder.entries() == []
    assert not recorder.snaps[8].traded_today
