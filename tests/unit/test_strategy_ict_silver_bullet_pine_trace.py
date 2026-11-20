"""Independent Pine-vs-Python audit traces for `pine/ict_silver_bullet_strategy.pine`.

Every expected value below comes from executing the Pine script by hand on the bars, line
by line (the line numbers in the comments are the script's). The per-bar order of the
script that these traces walk:

     1. time gates (210-238): curMin from the bar's OPEN time, inAM/inPM half-open
        [start, end), newWindow vs nz(windowId[1], 0), entryTimeOK = inWindow and not
        session.islastbar and curMin + barMin < winEnd
     2. pivots and FIFO pools of 25 (286-297)
     3. dayStartEq / peakEq init (300-303); newDay = change of dayofmonth(time, tz)
        (305-316); newWindow resets (318-325)
     4. in-window FVG trackers, strict `low > high[2]` / `high < low[2]` (328-339)
     5. eligibility (342-343); sweep, short first, strict `high > lastPH` (346-364)
     6. phase 1 (367-417): running sweep extreme, void `> maxBarsSweepMSS`, MSS by strict
        close, void `> maxBarsMSSFvg`, fvgOK needs `fvgBar >= sweepBar`, arm
     7. justEntered by trade count (422); cancel, strict `close > stop` (425-437)
     8. fill detection (440-445); exit re-issued while in a position (448-451)
     9. consec-loss guard (454-463); flat after a trade -> idle (466-469)
    10. daily DD (478-493), overall DD (495-510), EOD / End of range (515-518); one forced
        close per bar (flatSent)

Base bars (1-minute, Monday 2026-03-02 New York, pivotLen 1, MES tick 0.25, point value
5, risk 0.5 % x 100 000 = 500): pivot high 5010 known at bar 5, pivot low 5000 at bar 6;
bar 7 sweeps 5010 (short, mssLevel 5000); bar 8 closes 4997 < 5000 (MSS); bar 9 leaves the
bear FVG [4998, 5002] -> limit 5000, stop 5011 + 1 tick = 5011.25, risk 11.25,
qty floor(500 / 56.25) = 8, TP 5000 - 2 x 11.25 = 4977.5.
"""

import datetime as dt
from collections import deque
from collections.abc import Sequence
from dataclasses import dataclass, field
from typing import Any
from zoneinfo import ZoneInfo

from propbot.backtest.engine import run_backtest
from propbot.backtest.model import EngineConfig, EngineResult, FillModel
from propbot.core.bars import Bar, BarSeries, DataSource
from propbot.strategies.base import (
    Action,
    Cancel,
    CloseAll,
    Entry,
    Exit,
    FillEvent,
    Side,
    StrategyContext,
)
from propbot.strategies.ict_silver_bullet import IctSilverBullet, IctSilverBulletParams
from support.engine import make_config

NY = ZoneInfo("America/New_York")
type Row = tuple[float, float, float, float]

BASE: list[Row] = [
    (4990, 4991, 4988, 4989),  # 0
    (4989, 4990, 4985, 4986),  # 1  pivot low 4985 (known at bar 2)
    (4986, 4995, 4986, 4994),  # 2
    (5005, 5006, 5004, 5005),  # 3
    (5005, 5010, 5004, 5006),  # 4  pivot high 5010 (known at bar 5)
    (5006, 5007, 5000, 5002),  # 5  pivot low 5000 (known at bar 6)
    (5002, 5004, 5001, 5003),  # 6
    (5003, 5011, 5002, 5004),  # 7  sweep
    (5004, 5005, 4996, 4997),  # 8  MSS
    (4997, 4998, 4990, 4992),  # 9  bear FVG [4998, 5002] -> arm
    (4992, 5001, 4991, 4999),  # 10 fill at 5000
    (4999, 4999.5, 4977, 4978),  # 11 target 4977.5
    (4978, 4979, 4977, 4978),  # 12
]
NO_FILL: Row = (4992, 4995, 4990, 4993)  # never trades up to 5000.25
WOULD_FILL: Row = (4994, 5003, 4994, 5002)
HOLD: Row = (4999, 5000, 4995, 4996)  # neither the stop nor the target
QUIET: Row = (5010, 5011, 5009, 5010)

SHORT_ENTRY = Entry("Short", Side.SHORT, 8.0, limit=5000.0)
SHORT_EXIT = Exit("Exit S", "Short", stop=5011.25, limit=4977.5)
CANCEL_ALL = [Cancel("Long"), Cancel("Short"), Cancel("Exit L"), Cancel("Exit S")]


def ny_ms(day: int, hour: int, minute: int) -> int:
    """Open time of a bar at a New York wall-clock time in March 2026."""
    moment = dt.datetime(2026, 3, day, hour, minute, tzinfo=NY)
    return int(moment.timestamp() * 1000)


def series(rows: Sequence[Row], times: Sequence[int], *, tf_seconds: int = 60) -> BarSeries:
    bars = tuple(
        Bar(ts_open_ms=t, open=o, high=h, low=lo, close=c, volume=1.0)
        for t, (o, h, lo, c) in zip(times, rows, strict=True)
    )
    source = DataSource(name="trace", format="generic", sha256="", synthetic=True, bar_tz="UTC")
    return BarSeries(symbol="TEST", tf_seconds=tf_seconds, bars=bars, source=source)


def every(start_ms: int, count: int, step_minutes: int = 1) -> list[int]:
    return [start_ms + i * step_minutes * 60_000 for i in range(count)]


@dataclass(eq=False)
class Trace:
    inner: IctSilverBullet
    actions: dict[int, list[Action]] = field(default_factory=dict)
    states: list[dict[str, Any]] = field(default_factory=list)

    @property
    def strategy_id(self) -> str:
        return self.inner.strategy_id

    @property
    def pyramiding(self) -> int:
        return self.inner.pyramiding

    def on_bar(self, bar: Bar, ctx: StrategyContext) -> Sequence[Action]:
        out = list(self.inner.on_bar(bar, ctx))
        self.actions[ctx.bar_index] = out
        self.states.append(dict(vars(self.inner)))
        return out

    def on_fill(self, fill: FillEvent, ctx: StrategyContext) -> Sequence[Action]:
        return list(self.inner.on_fill(fill, ctx))


def go(
    rows: Sequence[Row] | BarSeries,
    *,
    model: FillModel = FillModel.PESSIMISTIC,
    session_last: Sequence[bool] | None = None,
    config: EngineConfig | None = None,
    **params: Any,
) -> tuple[EngineResult, Trace]:
    params.setdefault("pivot_len", 1)
    data = rows if isinstance(rows, BarSeries) else series(rows, every(ny_ms(2, 9, 53), len(rows)))
    if session_last is None:
        session_last = [i == len(data) - 1 for i in range(len(data))]
    trace = Trace(IctSilverBullet(IctSilverBulletParams(**params)))
    result = run_backtest(
        data, trace, config or make_config(fill_model=model), session_last=session_last
    )
    return result, trace


def entries(trace: Trace) -> list[Entry]:
    return [a for acts in trace.actions.values() for a in acts if isinstance(a, Entry)]


# -- 1) time gates on 5-minute bars ---------------------------------------------------------


def test_five_minute_gate_arms_at_14_50_and_expires_the_limit_on_the_14_55_bar() -> None:
    # 5-minute bars from 14:05 (PM window [840, 900)), barMin = 300 / 60 = 5 (line 230).
    # bar 9 = 14:50: entryTimeOK = 890 + 5 < 900 -> arms the base bracket.
    # bar 10 = 14:55: 895 + 5 < 900 is false -> expired (line 428) -> cancel all four.
    # bar 11 = 15:00 would trade through 5000.25: the order is gone, no trade.
    rows = [*BASE[:10], NO_FILL, WOULD_FILL, QUIET]
    data = series(rows, every(ny_ms(2, 14, 5), len(rows), 5), tf_seconds=300)
    result, trace = go(data)
    assert trace.actions[9] == [SHORT_ENTRY, SHORT_EXIT]
    assert trace.actions[10] == CANCEL_ALL
    assert trace.states[10]["phase"] == 0
    assert result.trades == ()


def test_five_minute_gate_refuses_to_arm_on_the_windows_last_bar() -> None:
    # The same bars 5 minutes later: bar 9 = 14:55 fails 895 + 5 < 900, so fvgOK is false
    # (line 392) and the hunt stays in phase 1; bar 10 = 15:00 is outside the window, so
    # neither the phase-1 block (line 367) nor the cancel block (phase 2 only) runs.
    rows = [*BASE[:10], NO_FILL, WOULD_FILL, QUIET]
    data = series(rows, every(ny_ms(2, 14, 10), len(rows), 5), tf_seconds=300)
    result, trace = go(data)
    assert [trace.states[i]["phase"] for i in (7, 8, 9, 10, 11)] == [1, 1, 1, 1, 1]
    assert entries(trace) == []
    assert result.trades == ()


# -- 4) / 6) an FVG older than the sweep is not armed off -----------------------------------


def test_a_gap_formed_before_the_sweep_bar_is_ignored_and_a_later_gap_arms() -> None:
    # Window 09:55-11:00 (bar 2 opens it). bar 6 high 5003.5 < low[2] = bar 4 low 5004:
    # bear FVG [5003.5, 5004] at bar 6 (line 336). bar 7 sweeps 5010, bar 8 is the MSS,
    # but bearFvgBar 6 < sweepBar 7 (line 392): no order on bar 8. bar 9 (high 5002 is not
    # < low[2] = 5002) and bars 10-11 leave no gap. bar 12 high 4979 < bar 10 low 4991:
    # bear FVG [4979, 4991], 12 - mssBar 8 = 4 <= 5 -> arm: CE 4985, sweep extreme 5011
    # (max high of bars 7-12), stop 5011.25, risk 26.25, qty floor(500 / 131.25) = 3,
    # TP 4985 - 52.5 = 4932.5.
    rows = list(BASE)
    rows[6] = (5002, 5003.5, 5001, 5003)
    rows[9] = (4997, 5002, 4990, 4992)
    rows.append((4978, 4979, 4977, 4978))  # 13: keeps bar 12 off the session's last bar
    _, trace = go(rows, am_start_h=9, am_start_m=55)
    state = trace.states[8]
    gap = (state["bear_fvg_bar"], state["bear_fvg_top"], state["bear_fvg_bot"])
    assert gap == (6, 5004, 5003.5)
    assert (state["phase"], state["sweep_bar"], state["mss_done"]) == (1, 7, True)
    assert trace.actions[8] == trace.actions[9] == trace.actions[10] == trace.actions[11] == []
    assert trace.actions[12] == [
        Entry("Short", Side.SHORT, 3.0, limit=4985.0),
        Exit("Exit S", "Short", stop=5011.25, limit=4932.5),
    ]


# -- strict comparisons ----------------------------------------------------------------------


def test_a_high_equal_to_the_pivot_is_not_a_sweep() -> None:
    # line 347: high > lastPH; 5010 > 5010 is false (and low 5002 < 5000 is false).
    rows = list(BASE)
    rows[7] = (5003, 5010, 5002, 5004)
    _, trace = go(rows)
    assert trace.states[7]["phase"] == 0


def test_a_close_equal_to_the_mss_level_is_not_an_mss() -> None:
    # line 378: close < mssLevel; 5000 < 5000 is false.
    rows = list(BASE)
    rows[8] = (5004, 5005, 4996, 5000)
    _, trace = go(rows)
    assert (trace.states[8]["phase"], trace.states[8]["mss_done"]) == (1, False)


def test_a_high_equal_to_low_two_bars_back_is_not_a_gap() -> None:
    # line 336: high < low[2]; bar 9 high 5002 vs bar 7 low 5002. The window opened at bar
    # 7 (newWindow cleared bearFvgBar, line 320), so no bear gap is known at bar 9.
    rows = list(BASE)
    rows[9] = (4997, 5002, 4990, 4992)
    _, trace = go(rows)
    assert trace.states[9]["bear_fvg_bar"] is None
    assert trace.actions[9] == []


def test_a_close_exactly_at_the_stop_does_not_cancel_the_armed_limit() -> None:
    # line 426: close > stopLevel; 5011.25 > 5011.25 is false and the gate is open
    # (10:03), 10 - 9 = 1 <= 20: the order stays (phase 2). 100 ticks through: no fill.
    rows = list(BASE)
    rows[10] = (4992, 5011.25, 4991, 5011.25)
    rows[11] = (5011.25, 5011.5, 5010, 5011.5)  # 5011.5 > 5011.25: cancelled here
    _, trace = go(rows, config=make_config(limit_through_ticks=100))
    assert trace.actions[10] == []
    assert trace.states[10]["phase"] == 2
    assert trace.actions[11] == CANCEL_ALL


# -- bar counters ------------------------------------------------------------------------


def test_counter_limits_are_inclusive_mss_one_bar_after_sweep_and_fvg_one_bar_after_mss() -> None:
    # line 372: (8 - 7) > 1 is false -> no void; line 387: (9 - 8) > 1 is false -> arm.
    _, trace = go(BASE, max_bars_sweep_mss=1, max_bars_mss_fvg=1)
    assert trace.actions[9] == [SHORT_ENTRY, SHORT_EXIT]


# -- 3) new day is the New York calendar day, not the session open -------------------------


def test_new_day_resets_at_new_york_midnight_not_at_the_18_00_session_open() -> None:
    # Monday: the base trade is stopped at bar 11 (stop 5011.25: -11.25 x 8 x 5 = -450),
    # maxConsec 1 -> consecLosses 1, haltedToday (lines 457-463). Monday 18:00 and 23:59:
    # dayofmonth is still 2 -> no reset. Tuesday 00:00: dayofmonth 3 -> newDay (line 305):
    # dayStartEq := equity 99 550, consecLosses 0, haltedToday false, tradesToday 0.
    monday = list(BASE)
    monday[11] = (4999, 5012, 4998, 5010)
    monday[12] = QUIET
    times = every(ny_ms(2, 9, 53), len(monday))
    times += [ny_ms(2, 18, 0), ny_ms(2, 23, 59), ny_ms(3, 0, 0), ny_ms(3, 0, 1)]
    rows = monday + [QUIET] * 4
    last = [i in (12, len(rows) - 1) for i in range(len(rows))]
    result, trace = go(series(rows, times), session_last=last, max_consec=1)
    (trade,) = result.trades
    assert trade.pnl == -450.0
    for index in (13, 14):  # 18:00 and 23:59 Monday
        state = trace.states[index]
        assert (state["consec_losses"], state["halted_today"], state["trades_today"]) == (
            1,
            True,
            1,
        )
        assert state["day_start_eq"] == 100_000.0
    tuesday = trace.states[15]
    assert (tuesday["consec_losses"], tuesday["halted_today"], tuesday["trades_today"]) == (
        0,
        False,
        0,
    )
    assert tuesday["day_start_eq"] == 99_550.0
    assert tuesday["halt_reason"] == ""


# -- 3) a new window with an open trade -----------------------------------------------------


def test_a_new_window_during_an_open_trade_resets_the_window_count_but_not_the_trade() -> None:
    # AM 10:00-10:05, PM 10:05-10:10. Bar 12 (10:05) opens the PM window with the short
    # still open: tradesWindow := 0 and the FVG bar indexes are cleared (lines 319-321),
    # but position_size != 0 keeps phase 3 (line 322); the exit is re-issued (line 451).
    rows = list(BASE)
    rows[11] = HOLD
    rows[12] = HOLD
    rows.append(HOLD)
    _, trace = go(
        rows, am_end_h=10, am_end_m=5, pm_start_h=10, pm_start_m=5, pm_end_h=10, pm_end_m=10
    )
    state = trace.states[12]
    assert (state["phase"], state["dir"], state["trades_window"], state["trades_today"]) == (
        3,
        -1,
        0,
        1,
    )
    assert state["bear_fvg_bar"] is None
    assert trace.actions[12] == [SHORT_EXIT]


# -- 10) one forced close per bar and the flatten reason order -----------------------------


def test_eod_flat_takes_precedence_over_end_of_range() -> None:
    # line 515: the EOD test is the first branch of the ternary. Bar 11 (10:04) is both
    # past flatMin 10:04 and past endDate 10:03 New York.
    rows = list(BASE)
    rows[11] = HOLD
    end = dt.datetime(2026, 3, 2, 10, 3, tzinfo=NY)
    _, trace = go(rows, day_end_h=10, day_end_m=4, end_date=end)
    assert trace.actions[11] == [SHORT_EXIT, CloseAll("EOD flat")]


def test_daily_overall_and_eod_on_one_bar_send_a_single_close_named_by_the_daily_halt() -> None:
    # Bar 11 closes 5003: equity 100 000 - 3 x 8 x 5 = 99 880. Daily: -0.12 % <= -0.1 %
    # -> close_all "Daily DD halt", flatSent, cancel Long/Short; position_size is still the
    # bar-close value, so the exits stay (line 487). Overall: peak 100 000, -0.12 % <= -0.1 %
    # -> haltedPerma; flatSent blocks a second close; cancel Long/Short again (504-505).
    # EOD (flatMin 10:04): flatSent blocks it (line 516).
    rows = list(BASE)
    rows[11] = (4999, 5004, 4998, 5003)
    result, trace = go(rows, max_daily_dd_pct=0.1, max_overall_pct=0.1, day_end_h=10, day_end_m=4)
    assert trace.actions[11] == [
        SHORT_EXIT,
        CloseAll("Daily DD halt"),
        Cancel("Long"),
        Cancel("Short"),
        Cancel("Long"),
        Cancel("Short"),
    ]
    state = trace.states[11]
    assert (state["halted_today"], state["halted_perma"], state["halt_reason"]) == (
        True,
        True,
        "Overall DD limit",
    )
    (trade,) = result.trades
    assert (trade.exit_bar_index, trade.exit_price, trade.comment) == (11, 5003.0, "Daily DD halt")


# -- f_target: nearest pool, strict side, inclusive min-R ---------------------------------


def test_liquidity_target_takes_the_nearest_pool_strictly_beyond_the_entry() -> None:
    # lines 257-270: short -> the HIGHEST low strictly below the entry, + liqBuf ticks;
    # long -> the LOWEST high strictly above, - liqBuf ticks; kept when the distance is
    # >= minRRliq x risk (inclusive), else the fixed rr target.
    strategy = IctSilverBullet(IctSilverBulletParams(tp_mode="Next opposing liquidity"))
    strategy.pl_arr = deque([4970.0, 4985.0, 4980.0, 5000.0, 5003.0], maxlen=25)
    strategy.ph_arr = deque([5030.0, 5015.0, 5020.0, 5000.0, 4997.0], maxlen=25)
    target = strategy._f_target
    assert target(-1, 5000.0, 10.0, 0.25) == 4985.5  # 4985 + 2 ticks, 14.5 >= 10
    assert target(1, 5000.0, 10.0, 0.25) == 5014.5  # 5015 - 2 ticks
    assert target(-1, 5000.0, 14.5, 0.25) == 4985.5  # 14.5 >= 14.5: inclusive
    assert target(-1, 5000.0, 14.75, 0.25) == 5000.0 - 2 * 14.75  # fallback to 2R
