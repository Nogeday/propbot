"""Independent Pine-vs-Python audit of the Asian sweep port: hand-executed Pine traces.

Each test pins one behaviour of `pine/asian_sweep_reversal_strategy.pine` that the port's own
tests leave open. The trace in each test walks the script by hand, block by block in its own
per-bar order (Pine line numbers in brackets):

     1. time, windows, `asianStartBar` / `asianEndBar` / `activeFirst`, entry-time gate,
        date range, day filter                                                  [180-209]
     2. cycle reset on the first Asian bar (in* vars only when flat)           [212-226]
     3. range build, range finalize, `canTrade`                                 [229-248]
     4. `ta.atr`, risk base                                                     [251-257]
     5. breakers: baseline on the Asian start, fold closed trades, evaluate    [306-333]
     6. arming (short first, long second)                                       [338-369]
     7. sweep detection                                                         [372-380]
     8. entry: invalidation, then the touch; Entry + bracket on the signal bar  [384-434]
     9. management: the fill bar's close re-prices the bracket once            [438-456]
    10. forced flats, one reason per bar                                        [463-467]
    11. reset of the in* vars once a trade has closed                           [472-477]

Compact windows: Asian [23:58, 00:01) New York (wraps midnight), active [00:02, 00:20).
MES-like instrument (tick 0.25, point value 5, whole contracts), zero costs, initial capital
100 000, risk 0.5 % = 500 per trade.
"""

import datetime as dt
from collections.abc import Sequence
from typing import Any
from zoneinfo import ZoneInfo

import pytest

from propbot.backtest.engine import run_backtest
from propbot.backtest.model import EngineResult, ExitReason, FillModel
from propbot.core.bars import Bar, BarSeries, DataSource
from propbot.strategies.asian_sweep import AsianSweep, AsianSweepParams
from propbot.strategies.base import Action, CloseAll, Entry, Exit, FillEvent, Side, StrategyContext
from support.engine import make_config, mes

NY = ZoneInfo("America/New_York")
type OHLC = tuple[float, float, float, float]

BOTH_MODELS = pytest.mark.parametrize("fill_model", [FillModel.PESSIMISTIC, FillModel.TV_OHLC])

COMPACT: dict[str, Any] = {
    "asian_start_hour": 23,
    "asian_start_min": 58,
    "asian_end_hour": 0,
    "asian_end_min": 1,
    "active_start_hour": 0,
    "active_start_min": 2,
    "active_end_hour": 0,
    "active_end_min": 20,
}

DAY1 = dt.datetime(2026, 3, 2, 23, 57, tzinfo=NY)  # Monday
DAY2 = dt.datetime(2026, 3, 3, 23, 57, tzinfo=NY)  # Tuesday

# Base short cycle (range 102 / 98): sweep of the High on the first active bar 5, bearish FVG
# [101.25, 101.5] on bar 7, touch on bar 8 (signal: 80 contracts, stop 102, target 98.25),
# fill at 100.5 on bar 9 (re-priced: stop 102, target 97.5), target on bar 10.
SHORT_ROWS: tuple[OHLC, ...] = (
    (100.0, 100.5, 99.5, 100.0),  # 0 23:57
    (100.0, 101.0, 99.0, 100.0),  # 1 23:58 Asian start
    (100.0, 102.0, 99.5, 101.0),  # 2 23:59
    (101.0, 101.5, 98.0, 99.0),  # 3 00:00
    (101.0, 101.5, 100.5, 101.5),  # 4 00:01 Asian end
    (101.5, 103.0, 101.5, 102.5),  # 5 00:02 first active bar, sweep
    (102.5, 102.75, 100.5, 101.0),  # 6
    (101.0, 101.25, 100.0, 100.25),  # 7 bearish FVG -> arm short
    (100.25, 101.5, 100.0, 100.75),  # 8 touch -> signal
    (100.5, 100.75, 99.5, 99.75),  # 9 fill 100.5
    (99.75, 100.0, 97.25, 97.5),  # 10 target 97.5
)


def _ms(moment: dt.datetime) -> int:
    return int(moment.timestamp() * 1000)


def stamped(start: dt.datetime, rows: Sequence[OHLC]) -> list[tuple[int, OHLC]]:
    first = _ms(start)
    return [(first + i * 60_000, row) for i, row in enumerate(rows)]


def series_of(bars: Sequence[tuple[int, OHLC]]) -> BarSeries:
    source = DataSource(name="trace", format="generic", sha256="", synthetic=True, bar_tz="UTC")
    return BarSeries(
        symbol="TEST",
        tf_seconds=60,
        bars=tuple(
            Bar(ts_open_ms=ts, open=o, high=h, low=lo, close=c, volume=1.0)
            for ts, (o, h, lo, c) in bars
        ),
        source=source,
    )


class Recorder:
    """Records what the port returned on every bar and fill, and its state after each bar."""

    def __init__(self, inner: AsianSweep) -> None:
        self.inner = inner
        self.actions: dict[int, list[Action]] = {}
        self.fills: list[tuple[FillEvent, list[Action]]] = []
        self.states: list[dict[str, Any]] = []

    @property
    def strategy_id(self) -> str:
        return self.inner.strategy_id

    @property
    def pyramiding(self) -> int:
        return self.inner.pyramiding

    def on_bar(self, bar: Bar, ctx: StrategyContext) -> Sequence[Action]:
        out = list(self.inner.on_bar(bar, ctx))
        self.actions[ctx.bar_index] = out
        self.states.append(
            {k: v for k, v in vars(self.inner).items() if not k.startswith("_") and k != "params"}
        )
        return out

    def on_fill(self, fill: FillEvent, ctx: StrategyContext) -> Sequence[Action]:
        out = list(self.inner.on_fill(fill, ctx))
        self.fills.append((fill, out))
        return out

    def entries(self) -> dict[int, list[Action]]:
        return {
            i: acts for i, acts in self.actions.items() if any(isinstance(a, Entry) for a in acts)
        }


def run(
    bars: Sequence[tuple[int, OHLC]],
    *,
    fill_model: FillModel = FillModel.PESSIMISTIC,
    session_last: Sequence[bool] | None = None,
    **overrides: Any,
) -> tuple[EngineResult, Recorder]:
    series = series_of(bars)
    recorder = Recorder(AsianSweep(AsianSweepParams(**{**COMPACT, **overrides})))
    flags = (
        list(session_last)
        if session_last is not None
        else [i == len(series) - 1 for i in range(len(series))]
    )
    config = make_config(instrument=mes(), fill_model=fill_model)
    return run_backtest(series, recorder, config, session_last=flags), recorder


def short_bracket(qty: float, stop: float, target: float) -> list[Action]:
    return [Entry("Short", Side.SHORT, qty, comment="Short"), Exit("Exit S", "Short", stop, target)]


# -- sweep detection [373]: `high[1] <= asianHigh` is inclusive -------------------------------


def test_a_previous_high_exactly_at_the_asian_high_still_lets_the_next_bar_cross() -> None:
    # Active from 00:01: bar 4 is the first active bar, high 102 == asianHigh -> `high >
    # asianHigh` false, no sweep [374]. Bar 5: high 103 > 102 and high[1] 102 <= 102 ->
    # crossedUp, sweepHiBar = 5. Bar 7 arms (high 101.25 < low[2] 101.5, close < 102), bar 8
    # touches: stop max(101.5 + 0.5, 100.75 + 0.25) = 102, qty floor(500 / 6.25) = 80,
    # target 100.75 - 2 * 1.25 = 98.25. With a strict `<` the sweep would never be seen.
    rows = list(SHORT_ROWS)
    rows[4] = (101.0, 102.0, 100.5, 101.5)

    _, rec = run(stamped(DAY1, rows), active_start_min=1)

    assert rec.states[4]["sweep_hi_bar"] == -1
    assert rec.states[5]["sweep_hi_bar"] == 5
    assert rec.entries() == {8: short_bracket(80.0, 102.0, 98.25)}


# -- date range [205]: `time >= startDate and time <= endDate`, both inclusive --------------


@pytest.mark.parametrize(("offset_min", "trades"), [(0, 1), (1, 0)])
def test_the_start_date_is_inclusive_and_gates_the_sweep_too(offset_min: int, trades: int) -> None:
    # start == bar 5's open: bar 5 is in range, the sweep is recorded, the setup trades.
    # start one minute later: bar 5 is outside (canTrade false, no sweep); bar 6 is in range
    # but is not the first active bar and does not cross (high[1] 103 > 102): no setup.
    start = (DAY1 + dt.timedelta(minutes=5 + offset_min)).astimezone(dt.UTC)

    result, rec = run(stamped(DAY1, SHORT_ROWS), start_date=start)

    assert len(result.trades) == trades
    assert rec.states[5]["sweep_hi_bar"] == (5 if trades else -1)


# -- breakers [323-328]: the daily base is capped at the initial capital ------------------


def test_the_daily_loss_base_is_capped_at_the_initial_capital_after_a_winning_day() -> None:
    # Day 1 (bars 0-10): the base short wins 3 * 80 * 5 = 1200 at the target.
    # Day 2 starts on bar 11 (23:57); bar 12 is its Asian start: dayStartEquity = 101 200.
    # dayBaseEq = min(101 200, 100 000) = 100 000 -> cap = 0.1 % = 100 (uncapped: 101.2).
    # Day 2 repeats the setup: signal on bar 19, fill 100.5 on bar 20, which closes at 100.75:
    # equity 101 200 - 0.25 * 80 * 5 = 101 100 -> dailyEqPnL = -100 <= -100 -> haltedDD,
    # flatten at bar 20's close [464]. Uncapped, -100 > -101.2 and the trade would run on.
    day2 = [*SHORT_ROWS[:9], (100.5, 101.0, 100.25, 100.75), SHORT_ROWS[10]]
    bars = stamped(DAY1, SHORT_ROWS) + stamped(DAY2, day2)

    result, rec = run(bars, daily_dd_pct=0.1)

    assert rec.states[12]["day_start_equity"] == pytest.approx(101_200.0)
    assert rec.actions[20] == [Exit("Exit S", "Short", 102.0, 97.5), CloseAll("Daily DD halt")]
    assert rec.states[20]["halted_dd"] is True
    first, second = result.trades
    assert first.pnl == pytest.approx(1200.0)
    assert (second.exit_bar_index, second.exit_price, second.comment) == (
        20,
        100.75,
        "Daily DD halt",
    )


# -- forced flats [464]: one reason per bar, "Session end" first -----------------------------


def test_session_end_wins_over_daily_dd_when_both_hold_on_one_bar() -> None:
    # Bar 9 is the session's last bar inside the window (activeExit) AND its close breaches the
    # 0.1 % cap (-100, see above): the ternary picks "Session end".
    rows = [*SHORT_ROWS[:9], (100.5, 101.0, 100.25, 100.75), SHORT_ROWS[10]]
    flags = [i in (9, len(rows) - 1) for i in range(len(rows))]

    result, rec = run(stamped(DAY1, rows), daily_dd_pct=0.1, session_last=flags)

    assert rec.states[9]["halted_dd"] is True
    assert rec.actions[9] == [Exit("Exit S", "Short", 102.0, 97.5), CloseAll("Session end")]
    (trade,) = result.trades
    assert (trade.exit_bar_index, trade.comment) == (9, "Session end")


# -- management [449-456]: the clamp `max(rawStop, avg + mintick)` on a gapped fill --------


@pytest.mark.parametrize(
    ("fill_model", "exit_price", "reason", "initial_stop"),
    [
        # Pine (tv_ohlc): the signal-bar bracket (stop 102) is in force when bar 9 opens at
        # 102.25 above it: stopped at the open, before the fill bar's close could re-price.
        (FillModel.TV_OHLC, 102.25, ExitReason.STOP, 102.0),
        # Pessimistic re-prices at the fill: stop = max(102, 102.25 + 0.25) = 102.5,
        # target = 102.25 - 2 * 0.25 = 101.75, reached by bar 9's low 101.5 (one tick through).
        (FillModel.PESSIMISTIC, 101.75, ExitReason.TARGET, 102.5),
    ],
)
def test_a_fill_beyond_the_fvg_stop_is_clamped_one_tick_to_the_loss_side(
    fill_model: FillModel, exit_price: float, reason: ExitReason, initial_stop: float
) -> None:
    rows = [*SHORT_ROWS[:9], (102.25, 102.25, 101.5, 101.5), SHORT_ROWS[10]]

    result, rec = run(stamped(DAY1, rows), fill_model=fill_model)

    assert rec.entries() == {8: short_bracket(80.0, 102.0, 98.25)}
    if fill_model is FillModel.PESSIMISTIC:
        ((fill, out),) = rec.fills
        assert fill.price == 102.25
        assert out == [Exit("Exit S", "Short", 102.5, 101.75)]
    first = result.trades[0]
    assert (first.exit_bar_index, first.exit_price, first.reason) == (9, exit_price, reason)
    assert first.initial_stop == initial_stop


# -- [393] vs [442]: ATR na on the signal bar but known on the fill bar ----------------------


@BOTH_MODELS
def test_atr_na_at_the_signal_but_known_at_the_fill_reapplies_the_fvg_distance(
    fill_model: FillModel,
) -> None:
    # ATR(10) is first known on the 10th bar (index 9) = the fill bar.
    # Signal bar 8: atrInit na -> estStop = armTop 101.5 + 0.5 = 102, inAtrDist = 102 - 100.75
    # = 1.25 [416, 425]. Fill bar 9: atrInit known -> rawStop = avg + inAtrDist = 100.5 + 1.25
    # = 101.75 [452]; stop max(101.75, 100.75) = 101.75; target 100.5 - 2 * 1.25 = 98.0.
    # (The FVG stop would have given 102 / 97.5.) Bar 10's low 97.25 trades through 98.
    result, rec = run(
        stamped(DAY1, SHORT_ROWS), fill_model=fill_model, stop_method="ATR Based", atr_len_init=10
    )

    assert rec.entries() == {8: short_bracket(80.0, 102.0, 98.25)}
    assert rec.actions[9] == [Exit("Exit S", "Short", 101.75, 98.0)]
    (trade,) = result.trades
    assert (trade.exit_bar_index, trade.exit_price, trade.reason) == (10, 98.0, ExitReason.TARGET)
    assert trade.pnl == pytest.approx(2.5 * 80 * 5)


# -- [472-477] after [384-434]: a carried trade that closes on the next cycle's signal bar ---

# Day 2 (from bar 10, 23:57 Tuesday): range 101 / 99, sweep of the Low on bar 15, bullish FVG
# [99.5, 99.75] on bar 17, touch on bar 18 whose low 97.25 also takes the carried short's
# target 97.5 (one tick through).
CARRY_DAY2: tuple[OHLC, ...] = (
    (100.0, 100.5, 99.5, 100.0),  # 10 23:57
    (100.0, 101.0, 99.5, 100.0),  # 11 23:58 Asian start (the short is still open)
    (100.0, 101.0, 99.0, 100.0),  # 12 23:59
    (100.0, 100.5, 99.0, 99.5),  # 13 00:00 -> range 101 / 99
    (99.5, 100.0, 99.25, 99.5),  # 14 00:01 Asian end
    (99.5, 99.5, 98.5, 98.75),  # 15 00:02 first active bar, low 98.5 < 99: sweep of the Low
    (98.75, 99.0, 98.25, 98.75),  # 16
    (99.75, 100.25, 99.75, 100.0),  # 17 low 99.75 > high[2] 99.5 -> arm long [99.5, 99.75]
    (100.0, 100.25, 97.25, 99.75),  # 18 short's target fills; long touch -> signal
    (99.5, 100.0, 99.25, 99.75),  # 19 long fill at 99.5
    (99.75, 101.5, 99.5, 101.25),  # 20
)


@BOTH_MODELS
def test_a_signal_on_the_bar_a_carried_trade_closes_keeps_its_signal_bar_bracket(
    fill_model: FillModel,
) -> None:
    # Day 1 (bars 0-9, close_at_end off): the base short fills at 100.5 (stop 102, target 97.5)
    # and is carried into day 2. Bar 11 starts the cycle while it is open, so inDir stays -1.
    # Bar 18: the short's target fills intrabar; at the close the position is flat, tradedToday
    # false, not halted (a win) -> long signal: estStop = 99.5 - 0.5 = 99 (min with 99.5),
    # qty floor(500 / (0.75 * 5)) = 133, estTP = 99.75 + 2 * 0.75 = 101.25; inDir := 1.
    # Then [472]: flat, closedtrades 1 > closedtrades[1] 0, inDir != 0 -> inDir := 0 on the
    # SAME bar. Bar 19 (fill): `position_size > 0 and inDir == 1` is false, the bracket is
    # never re-priced (it would have been stop 99, target 99.5 + 2 * 0.5 = 100.5). The signal
    # bracket stays: bar 20's high 101.5 trades one tick through 101.25.
    bars = stamped(DAY1, SHORT_ROWS[:10]) + stamped(DAY2, CARRY_DAY2)

    result, rec = run(bars, fill_model=fill_model, close_at_end=False)

    assert rec.states[11]["in_dir"] == -1
    assert rec.entries() == {
        8: short_bracket(80.0, 102.0, 98.25),
        18: [Entry("Long", Side.LONG, 133.0, comment="Long"), Exit("Exit L", "Long", 99.0, 101.25)],
    }
    assert rec.states[18]["in_dir"] == 0
    assert rec.actions[19] == []
    if fill_model is FillModel.PESSIMISTIC:
        assert [out for fill, out in rec.fills if fill.entry_id == "Long"] == [[]]
    first, second = result.trades
    assert (first.exit_bar_index, first.exit_price, first.reason) == (18, 97.5, ExitReason.TARGET)
    assert (second.entry_bar_index, second.entry_price) == (19, 99.5)
    assert (second.exit_bar_index, second.exit_price, second.reason) == (
        20,
        101.25,
        ExitReason.TARGET,
    )
