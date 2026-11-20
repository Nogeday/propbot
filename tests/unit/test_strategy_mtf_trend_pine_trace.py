"""Independent Pine trace of `propbot.strategies.mtf_trend` (parity audit).

Each test pins a behaviour of `pine/mtf_trend_continuation_strategy.pine` that the port's
own tests leave open. The expected values come from stepping the script by hand; the
trace is in the comments, with the Pine line numbers it rests on.

Shared set-up (same reduced inputs as the port's unit tests, so the arithmetic is short):
- 5-minute bars from 09:30 New York (or another start, given per test); `htfTf = "10"`,
  HTF EMA lengths 1 / 2: a chart bar's trend is the last CLOSED 10-minute bucket's close
  against the previous bucket's slow EMA (L168-173).
- `ltfFastLen = ltfSlowLen = 1`: the value-area band is the close, so every bar is inside.
- Volume filter off, `swingLen = 3`, ATR trailing with length 1 and multiplier 1
  (trail = close -/+ true range), `riskPercent = 0.1` of 100 000 = 100 $, MES point value 5.

Base scenario rows 0..5 (HTF buckets (0,1) (2,3) (4,5) ...):
- bucket (0,1) closes 5000: fast = slow = 5000 from bar 2 (neither bull nor bear)
- bucket (2,3) closes 5004: fast 5004 > slow 2/3*5004 + 1/3*5000 = 5002.667 -> bull, bars 4-5
- bar 5 triggers (5006.75 > open 5002, > high[1] 5004.5): swingLow(3) of bars 3..5 = 5001,
  stopLvl = min(5001 - 1, 5006.75 - 0.25) = 5000, stopDist 6.75, qty = floor(100/33.75) = 2
  (even -> scaled), estTP1 = 5006.75 + 1.5 * 6.75 = 5016.875 (L367-393)
- bucket (4,5) closes 5006.75: slow = 2/3*5006.75 + 1/3*5002.667 = 5005.389 -> bull, bars 6-7
"""

from collections.abc import Sequence
from dataclasses import dataclass, field
from typing import Any

import pytest

from propbot.backtest.engine import run_backtest
from propbot.backtest.model import EngineResult, ExitReason, FillModel
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
from propbot.strategies.mtf_trend import MtfTrend, MtfTrendParams
from support.engine import FREE, MINUTE_MS, START_MS, make_config, mes

FIVE_MIN_MS = 5 * MINUTE_MS
HOUR_MS = 60 * MINUTE_MS
BOTH_MODELS = pytest.mark.parametrize("fill_model", list(FillModel))
ALL_CANCELS = [Cancel(order_id) for order_id in ("TP1", "Init", "Run", "RunB", "Stop")]

SMALL: dict[str, Any] = {
    "htf_tf": "10",
    "htf_fast_len": 1,
    "htf_slow_len": 2,
    "ltf_fast_len": 1,
    "ltf_slow_len": 1,
    "use_vol_filter": False,
    "swing_len": 3,
    "risk_percent": 0.1,
    "trail_type": "ATR Trailing",
    "atr_trail_len": 1,
    "atr_trail_mult": 1.0,
}

BASE: list[tuple[float, float, float, float]] = [
    (5000, 5001, 4999, 5000),  # 0
    (5000, 5001, 4999, 5000),  # 1
    (5000, 5003, 4999, 5002),  # 2
    (5002, 5005, 5001, 5004),  # 3
    (5004, 5004.5, 5001, 5002),  # 4
    (5002, 5007, 5001.5, 5006.75),  # 5  signal: Long 2, stop 5000, estTP1 5016.875
]


def mirror(rows: Sequence[tuple[float, float, float, float]]) -> list[tuple[float, ...]]:
    """x -> 10000 - x: every long setup becomes its short twin."""
    return [(10000 - o, 10000 - lo, 10000 - h, 10000 - c) for o, h, lo, c in rows]


def build_series(rows: Sequence[Sequence[float]], start_ms: int) -> BarSeries:
    bars = tuple(
        Bar(
            ts_open_ms=start_ms + index * FIVE_MIN_MS,
            open=row[0],
            high=row[1],
            low=row[2],
            close=row[3],
            volume=1.0,
        )
        for index, row in enumerate(rows)
    )
    source = DataSource(name="trace", format="generic", sha256="", synthetic=True, bar_tz="UTC")
    return BarSeries(symbol="MES", tf_seconds=FIVE_MIN_MS // 1000, bars=bars, source=source)


@dataclass(eq=False)
class Recorder:
    inner: MtfTrend
    bar_actions: dict[int, list[Action]] = field(default_factory=dict)

    @property
    def strategy_id(self) -> str:
        return self.inner.strategy_id

    @property
    def pyramiding(self) -> int:
        return self.inner.pyramiding

    def on_bar(self, bar: Bar, ctx: StrategyContext) -> Sequence[Action]:
        actions = list(self.inner.on_bar(bar, ctx))
        self.bar_actions[ctx.bar_index] = actions
        return actions

    def on_fill(self, fill: FillEvent, ctx: StrategyContext) -> Sequence[Action]:
        return self.inner.on_fill(fill, ctx)


def trace(
    rows: Sequence[Sequence[float]],
    fill_model: FillModel,
    *,
    start_ms: int = START_MS,
    **overrides: Any,
) -> tuple[EngineResult, Recorder]:
    recorder = Recorder(MtfTrend(MtfTrendParams(**{**SMALL, **overrides})))
    flags = [index == len(rows) - 1 for index in range(len(rows))]
    result = run_backtest(
        build_series(rows, start_ms),
        recorder,
        make_config(instrument=mes(), costs=FREE, fill_model=fill_model),
        session_last=flags,
    )
    return result, recorder


def entries(recorder: Recorder) -> dict[int, Entry]:
    return {
        index: action
        for index, actions in recorder.bar_actions.items()
        for action in actions
        if isinstance(action, Entry)
    }


def summary(result: EngineResult) -> list[tuple[str, ExitReason, float, float, int]]:
    return [(t.exit_id, t.reason, t.qty, t.exit_price, t.exit_bar_index) for t in result.trades]


# -- a trade that ends on its own fill bar ----------------------------------------------------

# Bar 6 fills the bar-5 entry at 5007, trades down through the stop 5000 and closes as a new
# long trigger. tv_ohlc path: open 5007 is nearer the high (4.5) than the low (8) -> O-H-L-C;
# the high 5011.5 stays below estTP1 5016.875, the low hits 5000. Pessimistic: the stop first.
STOPPED_ON_FILL_BAR = [
    *BASE,
    (5007, 5011.5, 4999, 5011),  # 6
    (5011, 5013, 5010, 5012),  # 7
    (5012, 5013, 5011, 5012),  # 8
]


@BOTH_MODELS
def test_a_trade_stopped_on_its_fill_bar_resets_and_a_new_entry_follows_on_that_bar(
    fill_model: FillModel,
) -> None:
    # Bar 6 close, Pine order of blocks:
    #   L309-328: flat and closedtrades 2 > 0 -> tradeNet = 2 * (5000 - 5007) * 5 = -70: a loss
    #   L334-349: flat and (activeTP1 set [pessimistic] or pendingDir 1 and 6 > 5 [tv_ohlc])
    #             -> state reset, all five exit ids cancelled BEFORE the entry block
    #   L230-246: bull (bucket (4,5)), inside, re-armed; 5011 > open 5007 and > high[1] 5007
    #   L357:     tradesToday 1 < maxTradesDay 2, flat -> canEnterBase
    #   L368-384: swingLow of bars 4..6 = 4999 -> stopLvl = min(4998, 5010.75) = 4998;
    #             d = 13 -> qty = floor(100 / 65) = 1 -> odd -> Plan B ("Stop" only)
    result, rec = trace(STOPPED_ON_FILL_BAR, fill_model, max_trades_day=2)

    assert rec.bar_actions[6] == [
        *ALL_CANCELS,
        Entry("Long", Side.LONG, 1.0, comment="Long"),
        Exit("Stop", "Long", stop=4998.0, comment="Init stop"),
    ]
    assert summary(result)[:2] == [
        ("TP1", ExitReason.STOP, 1.0, 5000.0, 6),
        ("Init", ExitReason.STOP, 1.0, 5000.0, 6),
    ]
    # Bar 7 fills the second entry at 5011: init = min(4998, 5010.75) = 4998 (L429)
    assert rec.bar_actions[7] == [Exit("Stop", "Long", stop=4998.0, comment="Init stop")]
    assert list(entries(rec)) == [5, 6]


@BOTH_MODELS
def test_a_loss_closed_on_the_fill_bar_counts_for_the_loss_streak_halt(
    fill_model: FillModel,
) -> None:
    # Same bars; L318 consecLosses = 1 >= maxConsec 1 -> consecHaltHit (L326-327), so the
    # bar-6 trigger is refused by L356-357. The reset still cancels the exit ids.
    _, rec = trace(STOPPED_ON_FILL_BAR, fill_model, max_trades_day=2, use_consec=True, max_consec=1)

    assert rec.bar_actions[6] == ALL_CANCELS
    assert list(entries(rec)) == [5]


# -- a breakeven trade is a loss for the streak (L314-318: `tradeNet > 0` else loss) ---------

# swingBufTick 0 -> stop 5001, d 5.75, qty floor(100 / 28.75) = 3 (Plan B); fill 5007, risk 6,
# 1.5R = 5016 touched on bar 7 -> RunB at max(BE 5007, 5015 - TR 8) = 5007. Bar 8 opens 5014
# and trades down to 5006: the runner is stopped at exactly 5007 -> tradeNet = 0.
# Bucket (6,7) closes 5015: slow = 2/3*5015 + 1/3*5005.389 = 5011.796 < 5015 -> bull on 8-9.
BREAKEVEN_ROWS = [
    *BASE,
    (5007, 5010, 5005, 5009),  # 6  fill 5007
    (5009, 5016, 5008, 5015),  # 7  touches 1.5R 5016
    (5014, 5014.5, 5006, 5006.5),  # 8  RunB 5007 hit
    (5006.5, 5015, 5006, 5014.75),  # 9  trigger: 5014.75 > high[1] 5014.5
    (5014.75, 5016, 5014, 5015.5),  # 10
    (5015.5, 5016, 5015, 5015.5),  # 11
]


@BOTH_MODELS
def test_a_breakeven_trade_counts_as_a_loss_for_the_streak(fill_model: FillModel) -> None:
    common: dict[str, Any] = {"swing_buf_tick": 0, "max_trades_day": 2}
    result, halted = trace(BREAKEVEN_ROWS, fill_model, use_consec=True, max_consec=1, **common)
    _, control = trace(BREAKEVEN_ROWS, fill_model, **common)

    assert summary(result)[0] == ("RunB", ExitReason.STOP, 3.0, 5007.0, 8)
    assert result.trades[0].pnl == 0.0
    assert list(entries(halted)) == [5]
    # control: swingLow of bars 7..9 = 5006 -> stop min(5006, 5014.5) = 5006; d = 8.75
    # -> qty floor(100 / 43.75) = 2
    assert entries(control)[9] == Entry("Long", Side.LONG, 2.0, comment="Long")


# -- trail step boundary (L452 / L468 / L504 / L520: `>=` trailMinStep) -----------------------

# Scaled trade of the base scenario: fill 5007 (bar 6), TP1 5017.5 traded through on bar 7,
# runner to BE 5007 (trail 5016 - 10 = 5006 < BE), bar 8 trail 5021 - 7 = 5014.
STEP_HEAD = [
    *BASE,
    (5007, 5010, 5005, 5009),  # 6
    (5009, 5018, 5008, 5016),  # 7
    (5016, 5022, 5015, 5021),  # 8
]
STEP_TAIL = [(5017, 5017.5, 5013, 5013.5), (5013.5, 5014, 5012, 5013)]  # 10, 11


@BOTH_MODELS
@pytest.mark.parametrize(
    ("bar9", "runner9"),
    [
        # TR = max(3.5, |5020.5-5021|, |5017.5-5021|) = 3.5 -> 5018.5 - 3.5 = 5015: +1.0 = 4 ticks
        ((5019, 5020.5, 5017.5, 5018.5), 5015.0),
        # TR = max(3.25, 0.5, 3.75) = 3.75 -> 5014.75: +0.75 < 1.0 -> stays 5014
        ((5019, 5020.5, 5017.25, 5018.5), 5014.0),
    ],
)
def test_the_trail_moves_on_an_improvement_of_exactly_the_minimum_step(
    fill_model: FillModel, bar9: tuple[float, float, float, float], runner9: float
) -> None:
    rows = [*STEP_HEAD, bar9, *STEP_TAIL]
    long_result, long_rec = trace(rows, fill_model)
    short_result, short_rec = trace(mirror(rows), fill_model)

    assert long_rec.bar_actions[8][1] == Exit("Run", "Long", stop=5014.0, comment="Runner")
    assert long_rec.bar_actions[9][1] == Exit("Run", "Long", stop=runner9, comment="Runner")
    assert summary(long_result)[-1] == ("Run", ExitReason.STOP, 1.0, runner9, 10)
    short_runner9 = 10000 - runner9
    assert short_rec.bar_actions[9][1] == Exit("Run", "Short", stop=short_runner9, comment="Runner")
    assert summary(short_result)[-1] == ("Run", ExitReason.STOP, 1.0, short_runner9, 10)


# -- daily-loss breaker arithmetic (L296-301, L352-354) ---------------------------------------

# riskPercent 0.15 -> qty = floor(150 / 33.75) = 4 (scaled); dailyLossPct 0.1 % of 100 000 = 100 $
DAILY: dict[str, Any] = {"risk_percent": 0.15, "daily_loss_pct": 0.1}
DAILY_ROWS = [
    *BASE,
    (5007, 5008, 5001.5, 5002),  # 6  fill 5007, close 5002: open loss 4 * 5 * 5 = 100 $
    (5002, 5003, 5000.5, 5001),  # 7
    (5001, 5002, 5000.5, 5001),  # 8
]


@BOTH_MODELS
def test_the_daily_loss_breaker_fires_at_exactly_the_limit(fill_model: FillModel) -> None:
    # bar 6: 100 000 - 99 900 = 100 >= 100 * 1.0 -> halt + flat (L353-354, L538-541)
    result, rec = trace(DAILY_ROWS, fill_model, **DAILY)
    # close 5002.25 -> loss 4 * 4.75 * 5 = 95 < 100: no halt on bar 6
    softer = [*DAILY_ROWS[:6], (5007, 5008, 5001.5, 5002.25), *DAILY_ROWS[7:]]
    _, control = trace(softer, fill_model, **DAILY)

    assert rec.bar_actions[6][-1] == CloseAll(comment="Daily loss halt")
    assert summary(result) == [("", ExitReason.CLOSE, 4.0, 5002.0, 6)]
    assert not any(isinstance(a, CloseAll) for a in control.bar_actions[6])


@BOTH_MODELS
def test_day_start_equity_is_the_first_bar_of_the_day_close_including_open_profit(
    fill_model: FillModel,
) -> None:
    # use_session off (overnight bars). Start 23:30 New York: bar 6 (the fill bar) opens at
    # 00:00, so L296-297 takes dayStartEquity = equity at bar 6's CLOSE = 99 900 (the 100 $
    # open loss is already in it) -> loss 0 on bar 6; bar 7: 99 900 - 99 880 = 20 < 99.9.
    # Start 23:00: bar 6 opens 23:30, same day -> base 100 000 -> 100 >= 100 -> halt on bar 6.
    common: dict[str, Any] = {**DAILY, "use_session": False}
    _, new_day = trace(DAILY_ROWS, fill_model, start_ms=START_MS + 14 * HOUR_MS, **common)
    _, same_day = trace(
        DAILY_ROWS, fill_model, start_ms=START_MS + 13 * HOUR_MS + 30 * MINUTE_MS, **common
    )

    assert list(entries(new_day)) == list(entries(same_day)) == [5]
    assert not any(isinstance(a, CloseAll) for index in (6, 7) for a in new_day.bar_actions[index])
    assert same_day.bar_actions[6][-1] == CloseAll(comment="Daily loss halt")


# -- session end without the session window (L538) -------------------------------------------


@BOTH_MODELS
def test_without_the_session_window_only_the_session_last_bar_forces_flat(
    fill_model: FillModel,
) -> None:
    # Start 15:20: bar 7 opens 15:55 (= sessEnd). With useSession off, `useSession and
    # curMin >= sEnd` is false, so only session.islastbar (bar 11 here) would close; the
    # runner is stopped on bar 10 first, exactly as in the base scaled trade.
    rows = [*STEP_HEAD, (5018, 5018.25, 5017.5, 5018), *STEP_TAIL]
    result, rec = trace(rows, fill_model, start_ms=START_MS + 350 * MINUTE_MS, use_session=False)

    assert not any(isinstance(a, CloseAll) for actions in rec.bar_actions.values() for a in actions)
    assert summary(result) == [
        ("TP1", ExitReason.TARGET, 1.0, 5017.5, 7),
        ("Run", ExitReason.STOP, 1.0, 5014.0, 10),
    ]
