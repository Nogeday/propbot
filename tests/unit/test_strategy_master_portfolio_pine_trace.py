"""Independent Pine-vs-Python audit of the Master portfolio port: hand-executed Pine traces.

Each test pins one behaviour of `pine/master_prop_firm_portfolio.pine` that the port's own
tests leave open. The traces walk the script by hand in its per-bar order (Pine line
numbers in brackets):

     1. time, newDay, S1 / S2 session math, entry-time gates                    [265-303]
     2. S1 daily reset, S2 cycle reset                                          [307-329]
     3. build the OR and the Asian range, finalize them                         [332-344]
     4. ta.atr x3, ta.lowest / ta.highest, risk base                            [354-366]
     5. open-trade scan by entry id, per-leg reset with the 1-bar guard         [398-426]
     6. fill-bar re-pricing per leg from its own fill (ATR na-ness of THIS bar) [434-456]
     7. breakers: day baseline, closed-trade fold, evaluate, forced-flat flag   [461-491]
     8. S1: bias, FVG detection, inversions, gates, entry (newest eligible zone
        first; `break` only after a touched zone), trailing, session-end close  [498-660]
     9. S2: arming, sweep detection, invalidation, gates, entry, exit, window
        end close                                                               [663-774]
    10. portfolio CloseAll (daily-loss breach / after the End date)             [779-785]

MES grid (tick 0.25, point value 5, whole contracts), zero costs, initial capital 100 000,
risk 0.5 % = 500 per trade, FVG Buffer stop 2 ticks = 0.5.
"""

import datetime as dt
from collections.abc import Sequence
from typing import Any, Final
from zoneinfo import ZoneInfo

import pytest

from propbot.backtest.engine import run_backtest
from propbot.backtest.model import EngineResult, ExitReason, FillModel, TradeRecord
from propbot.core.bars import Bar, BarSeries, DataSource
from propbot.data.sessions import session_last_flags
from propbot.strategies.base import (
    Action,
    Close,
    CloseAll,
    Entry,
    Exit,
    FillEvent,
    Side,
    StrategyContext,
)
from propbot.strategies.master_portfolio import MasterPortfolio, MasterPortfolioParams
from support.engine import make_config, mes

NY: Final = ZoneInfo("America/New_York")
BOTH_MODELS = pytest.mark.parametrize("fill_model", [FillModel.PESSIMISTIC, FillModel.TV_OHLC])

type OHLC = tuple[float, float, float, float]

MONDAY_0929: Final = dt.datetime(2026, 3, 2, 9, 29, tzinfo=NY)
MONDAY_2357: Final = dt.datetime(2026, 3, 2, 23, 57, tzinfo=NY)

# OR [09:30, 09:31), S2 off.
S1_ONLY: Final[dict[str, Any]] = {"s2_enable": False, "s1_or_minutes": 1}
# Asian [23:58, 00:01), active [00:02, 00:20), S1 off.
S2_ONLY: Final[dict[str, Any]] = {
    "s1_enable": False,
    "s2_asian_start_hour": 23,
    "s2_asian_start_min": 58,
    "s2_asian_end_hour": 0,
    "s2_asian_end_min": 1,
    "s2_active_start_hour": 0,
    "s2_active_start_min": 2,
    "s2_active_end_hour": 0,
    "s2_active_end_min": 20,
}

# The S1 long of the port's own tests (independently re-traced here): OR 101 / 99 (bar 1);
# bar 2 bias long + bull FVG [100.5, 100.75]; bar 3 bull FVG [101, 101.25]; bar 4 touches the
# newest one: est 101.25, stop 101 - 0.5 = 100.5, risk 0.75, qty floor(500 / 3.75) = 133;
# bar 5 fills at its open 101.5.
S1_LONG_ROWS: Final[tuple[OHLC, ...]] = (
    (100.0, 100.5, 99.5, 100.0),  # 0 09:29
    (100.0, 101.0, 99.0, 100.5),  # 1 09:30 OR
    (100.75, 101.75, 100.75, 101.5),  # 2 09:31
    (101.5, 102.0, 101.25, 101.75),  # 3 09:32
    (101.75, 101.75, 101.0, 101.25),  # 4 09:33 signal
    (101.5, 101.75, 101.25, 101.5),  # 5 09:34 fill 101.5
    (101.5, 103.75, 101.5, 103.5),  # 6 09:35
)
# The S2 short of the port's own tests: range 102 / 98, sweep of the High on bar 5 (the first
# active bar), bear FVG [101.25, 101.5] arms on bar 7, bar 8 touches: est 100.75, stop
# 101.5 + 0.5 = 102, qty floor(500 / 6.25) = 80; bar 9 fills at 100.5, target 97.5.
S2_SHORT_ROWS: Final[tuple[OHLC, ...]] = (
    (100.0, 100.5, 99.5, 100.0),  # 0 23:57
    (100.0, 101.0, 99.0, 100.0),  # 1 23:58 Asian start
    (100.0, 102.0, 99.5, 101.0),  # 2 23:59
    (101.0, 101.5, 98.0, 99.0),  # 3 00:00 (Tuesday)
    (101.0, 101.5, 100.5, 101.5),  # 4 00:01 Asian end
    (101.5, 103.0, 101.5, 102.5),  # 5 00:02 first active bar
    (102.5, 102.75, 100.5, 101.0),  # 6
    (101.0, 101.25, 100.0, 100.25),  # 7 arm
    (100.25, 101.5, 100.0, 100.75),  # 8 signal
    (100.5, 100.75, 99.5, 99.75),  # 9 fill 100.5
    (99.75, 100.0, 97.25, 97.5),  # 10 target 97.5
)


def _ms(moment: dt.datetime) -> int:
    return int(moment.timestamp() * 1000)


def stamped(start: dt.datetime, rows: Sequence[OHLC]) -> list[tuple[int, OHLC]]:
    first = _ms(start)
    return [(first + i * 60_000, row) for i, row in enumerate(rows)]


def series_of(bars: Sequence[tuple[int, OHLC]]) -> BarSeries:
    built = tuple(
        Bar(ts_open_ms=ts, open=o, high=h, low=lo, close=c, volume=1.0)
        for ts, (o, h, lo, c) in bars
    )
    source = DataSource(name="trace", format="generic", sha256="", synthetic=True, bar_tz="UTC")
    return BarSeries(symbol="TEST", tf_seconds=60, bars=built, source=source)


class Recorder:
    """Keeps every batch the port returned and a snapshot of its breaker state per bar."""

    def __init__(self, inner: MasterPortfolio) -> None:
        self.inner = inner
        self.bars: dict[int, list[Action]] = {}
        self.consec: dict[int, int] = {}
        self.halted: dict[int, bool] = {}
        self.fills: list[tuple[FillEvent, list[Action]]] = []

    @property
    def strategy_id(self) -> str:
        return self.inner.strategy_id

    @property
    def pyramiding(self) -> int:
        return self.inner.pyramiding

    def on_bar(self, bar: Bar, ctx: StrategyContext) -> Sequence[Action]:
        out = list(self.inner.on_bar(bar, ctx))
        self.bars[ctx.bar_index] = out
        self.consec[ctx.bar_index] = self.inner.g_consec_loss
        self.halted[ctx.bar_index] = self.inner.halted
        return out

    def on_fill(self, fill: FillEvent, ctx: StrategyContext) -> Sequence[Action]:
        out = list(self.inner.on_fill(fill, ctx))
        self.fills.append((fill, out))
        return out

    def entries(self) -> list[tuple[int, Entry]]:
        return [
            (i, a) for i, batch in sorted(self.bars.items()) for a in batch if isinstance(a, Entry)
        ]


def run(
    bars: Sequence[tuple[int, OHLC]],
    fill_model: FillModel = FillModel.PESSIMISTIC,
    **params: Any,
) -> tuple[EngineResult, Recorder]:
    series = series_of(bars)
    recorder = Recorder(MasterPortfolio(MasterPortfolioParams(**params)))
    flags = session_last_flags(series.bars, series.tf_seconds)
    config = make_config(instrument=mes(), fill_model=fill_model)
    return run_backtest(series, recorder, config, session_last=flags), recorder


def only(trades: Sequence[TradeRecord]) -> TradeRecord:
    assert len(trades) == 1, trades
    return trades[0]


# -- S1 -------------------------------------------------------------------------------------------


@BOTH_MODELS
def test_s1_newest_zone_that_is_not_a_long_zone_is_skipped_and_an_older_one_is_traded(
    fill_model: FillModel,
) -> None:
    # [567-597] The scan runs newest -> oldest; `break` sits inside the touched-zone branch,
    # so a zone that is not a long zone (a FRESH BEARISH gap under a long bias) is passed over.
    #   bar 2 (09:31) close 101.5 > OR high 101 -> bias +1; low 100.75 > high[2] 100.5 ->
    #         bull A [100.5, 100.75], form 2 (no touch: 2 > 2 is false)
    #   bar 3 low 101.0 > high[1] 101.0 is false: no gap; A untouched (101 > 100.75)
    #   bar 4 high 100.5 < low[2] 100.75 -> bear C [100.5, 100.75], form 4; no inversion
    #         (A: close 100.5 < bot 100.5 false; C: close 100.5 > top 100.75 false).
    #         Scan: C is bear + fresh -> not a long zone; A: 4 > 2 and low 100.25 <= 100.75
    #         -> est 100.5, stop min(100.5 - 0.5, 100.25) = 100.0, risk 0.5,
    #         qty floor(500 / 2.5) = 200, target 100.5 + 2 x 0.5 = 101.5
    #   bar 5 fills at its open 100.75 -> stop min(100.0, 100.5) = 100.0 (structural),
    #         target 100.75 + 2 x 0.75 = 102.25
    #   bar 6 high 102.5 is one tick through 102.25 -> target fill.
    rows = (
        (100.0, 100.5, 99.5, 100.0),  # 0 09:29
        (100.0, 101.0, 99.0, 100.5),  # 1 09:30 OR 101 / 99
        (100.75, 101.75, 100.75, 101.5),  # 2 09:31
        (101.5, 102.5, 101.0, 102.25),  # 3 09:32
        (100.5, 100.5, 100.25, 100.5),  # 4 09:33 signal
        (100.75, 101.0, 100.5, 100.75),  # 5 09:34 fill
        (100.75, 102.5, 100.75, 102.25),  # 6 09:35 target
    )

    result, rec = run(stamped(MONDAY_0929, rows), fill_model, **S1_ONLY)

    assert rec.bars[4] == [
        Entry("S1 Long", Side.LONG, 200.0, comment="S1 Long", strategy_id="master_ny_orb_fvg"),
        Exit("S1 ExitL", "S1 Long", stop=100.0, limit=101.5),
    ]
    assert rec.bars[5] == [Exit("S1 ExitL", "S1 Long", stop=100.0, limit=102.25)]
    trade = only(result.trades)
    assert (trade.entry_price, trade.initial_stop) == (100.75, 100.0)
    assert (trade.exit_bar_index, trade.exit_price, trade.reason) == (6, 102.25, ExitReason.TARGET)


@BOTH_MODELS
def test_s1_atr_stop_on_the_fill_bar_reads_atr_na_ness_of_the_fill_bar_not_the_signal_bar(
    fill_model: FillModel,
) -> None:
    # [578, 587, 437] ATR Based with s1_atrLenInit = 6: ta.atr is na until the 6th bar.
    #   bar 4 (5th bar) signal: ATR na -> estStop = FVG buffer = 101 - 0.5 = 100.5,
    #         s1_inAtrDist = 101.25 - 100.5 = 0.75, qty 133, target 101.25 + 1.5 = 102.75
    #   bar 5 (6th bar) fill 101.5: `not na(s1_atrInit)` is now TRUE, so the raw stop is
    #         the distance re-applied: 101.5 - 0.75 = 100.75; min(100.75, 101.25) = 100.75;
    #         target 101.5 + 2 x 0.75 = 103.0 (the FVG level would have given 100.5 / 103.5)
    #   bar 6 high 103.75 is through 103.0 -> target fill at 103.0.
    params = {**S1_ONLY, "s1_stop_method": "ATR Based", "s1_atr_len_init": 6}

    result, rec = run(stamped(MONDAY_0929, S1_LONG_ROWS), fill_model, **params)

    assert rec.bars[4] == [
        Entry("S1 Long", Side.LONG, 133.0, comment="S1 Long", strategy_id="master_ny_orb_fvg"),
        Exit("S1 ExitL", "S1 Long", stop=100.5, limit=102.75),
    ]
    repriced = Exit("S1 ExitL", "S1 Long", stop=100.75, limit=103.0)
    if fill_model is FillModel.PESSIMISTIC:
        assert rec.fills[0][1] == [repriced]
    assert rec.bars[5] == [repriced]
    trade = only(result.trades)
    assert trade.initial_stop == 100.75
    assert (trade.exit_bar_index, trade.exit_price, trade.reason) == (6, 103.0, ExitReason.TARGET)


def test_s1_session_end_close_fires_on_the_first_bar_at_or_after_a_missing_end_bar() -> None:
    # [279, 658-660] s1_atSessionEnd = curMin >= s1_sessEnd: with sessEnd 09:37 and no 09:37
    # bar, the 09:38 bar (index 8) closes the leg at its close; 09:39 sees the leg gone.
    flat = (101.5, 101.75, 101.25, 101.5)
    bars = stamped(MONDAY_0929, (*S1_LONG_ROWS[:6], flat, flat)) + stamped(
        MONDAY_0929 + dt.timedelta(minutes=9), (flat, flat)
    )
    params = {**S1_ONLY, "s1_sess_end_hour": 9, "s1_sess_end_min": 37}

    result, rec = run(bars, **params)

    assert rec.bars[7][-1] == Exit("S1 ExitL", "S1 Long", stop=100.5, limit=103.5)  # 09:36
    assert rec.bars[8][-1] == Close("S1 Long", "S1 EOD")  # 09:38
    assert rec.bars[9] == []
    trade = only(result.trades)
    assert (trade.exit_bar_index, trade.exit_price, trade.comment) == (8, 101.5, "S1 EOD")


@BOTH_MODELS
def test_signal_on_the_last_bar_in_range_fills_and_is_flattened_on_the_fill_bar(
    fill_model: FillModel,
) -> None:
    # [268, 491, 779-785] endDate between 09:33 and 09:34 (New York):
    #   bar 4 (09:33): time <= endDate -> in range -> S1 signal as usual
    #   bar 5 (09:34): the entry fills at 101.5; on the close time > endDate and
    #         position_size != 0 -> g_forceFlat: S1 still re-issues its exit [641], the
    #         session-end close stands down, close_all("End of test range") at 101.5.
    end = dt.datetime(2026, 3, 2, 14, 33, 30, tzinfo=dt.UTC)

    result, rec = run(stamped(MONDAY_0929, S1_LONG_ROWS), fill_model, **S1_ONLY, end_date=end)

    assert len(rec.entries()) == 1
    assert rec.bars[5] == [
        Exit("S1 ExitL", "S1 Long", stop=100.5, limit=103.5),
        CloseAll("End of test range"),
    ]
    trade = only(result.trades)
    assert (trade.entry_bar_index, trade.exit_bar_index) == (5, 5)
    assert (trade.exit_price, trade.reason, trade.comment) == (
        101.5,
        ExitReason.CLOSE,
        "End of test range",
    )
    assert rec.bars[6] == []  # out of range: no new S1 work


def test_loss_streak_restarts_on_the_new_york_day_change() -> None:
    # [461-474] newDay zeroes g_consecLoss BEFORE the closed-trade fold.
    #   Monday: the S1 long stops out at 100.5 on bar 6 -> streak 1 (< 2: not halted).
    #   Tuesday 09:29 (bar 7): newDay -> streak 0. Bar 13: the same trade stops out -> 1.
    #   Without the day reset the streak would read 2 and halt (maxConsecLoss 2).
    loss = (101.5, 101.5, 100.25, 100.5)
    day = (*S1_LONG_ROWS[:6], loss)
    tuesday = MONDAY_0929 + dt.timedelta(days=1)

    result, rec = run(stamped(MONDAY_0929, day) + stamped(tuesday, day), **S1_ONLY)

    assert [t.exit_price for t in result.trades] == [100.5, 100.5]
    assert [t.pnl for t in result.trades] == [pytest.approx(-665.0)] * 2
    assert (rec.consec[6], rec.consec[7], rec.consec[13]) == (1, 0, 1)
    assert not any(rec.halted.values())
    assert [i for i, _ in rec.entries()] == [4, 11]


# -- S2 -------------------------------------------------------------------------------------------


@BOTH_MODELS
def test_s2_first_active_bar_marks_a_sweep_even_without_a_fresh_cross(
    fill_model: FillModel,
) -> None:
    # [699-702] crossedUp needs high[1] <= asianHigh; the first active bar also counts any
    # high above the range. Bar 4 (00:01, the Asian end bar, outside the range window) already
    # trades 102.5 > 102, so on bar 5 (00:02) crossedUp is false, `s2_activeFirst and high >
    # asianHigh` is true -> sweepHiBar 5. The rest is the short of S2_SHORT_ROWS: arm on bar 7
    # (high 101.25 < low[2] 101.5), signal on bar 8 (est 100.75, stop 102, qty 80).
    rows = list(S2_SHORT_ROWS)
    rows[4] = (101.0, 102.5, 100.5, 101.5)

    result, rec = run(stamped(MONDAY_2357, rows), fill_model, **S2_ONLY)

    assert [(i, e.id, e.qty) for i, e in rec.entries()] == [(8, "S2 Short", 80.0)]
    trade = only(result.trades)
    assert (trade.entry_price, trade.initial_stop, trade.exit_price) == (100.5, 102.0, 97.5)


@pytest.mark.parametrize(("max_bars", "entries"), [(1, 0), (2, 1)])
def test_s2_arming_window_counts_bars_from_the_sweep_inclusively(
    max_bars: int, entries: int
) -> None:
    # [668] (bar_index - s2_sweepHiBar) <= s2_maxBars: sweep on bar 5, the FVG on bar 7 is
    # 2 bars later -> armed with maxBars 2, not with 1 (no later sweep re-marks it).
    _, rec = run(stamped(MONDAY_2357, S2_SHORT_ROWS), **S2_ONLY, s2_max_bars=max_bars)

    assert len(rec.entries()) == entries
