"""Independent Pine trace of `pine/mean_reversion_bb_kc_rsi2_strategy.pine` vs the port.

Every expectation here comes from executing the script by hand, block by block, on a small
bar sequence (the trace is in each test's comment). The script's per-bar order is:

    1. INDICATORS            (L194-214)  basis, bbDev, KC, rsi2, emaTr, atrVal
    2. TIME / DATE / DAY     (L217-243)  curMin, inWindow, atOrPastEnd, entryTimeOK, isNewDay
    3. DAILY BREAKER         (L248-263)  dayStartEquity, dayDDPct >= maxDailyLoss, tradesToday
    4. ENTRY CONDITIONS      (L277-298)  setups, longSetup[1] confirm, tradingAllowed
    5. TRADE MANAGEMENT      (L303-320)  finalize inStop from the fill, re-issue the bracket
    6. FORCED EXITS          (L328-331)  one reason, close_all(immediately = true)
    7. RESET WHEN FLAT       (L334-337)
    8. ENTRY                 (L342-365)  qty from atrMult * atrVal, bracket from the close

The reference formulas below are written for this file (Pine reference manual), independent
of `propbot.strategies.indicators`. Bars: 5-minute, Monday 2026-03-02 New York unless the
test says otherwise; each chained bar opens at the previous close with a 0.25 wick each side.
"""

import itertools
import math
from collections.abc import Sequence
from dataclasses import dataclass

import pytest

from propbot.backtest.engine import run_backtest
from propbot.backtest.model import EngineResult, ExitReason, FillModel
from propbot.core.bars import Bar, BarSeries, DataSource
from propbot.core.instrument import Instrument
from propbot.strategies.base import Action, Entry, Exit, FillEvent, Side, StrategyContext
from propbot.strategies.mean_reversion import MeanReversion, MeanReversionParams
from support.engine import MINUTE_MS, OHLC, START_MS, cfd, make_config, mes

P = FillModel.PESSIMISTIC
TV = FillModel.TV_OHLC
TF = 300
BAR_MS = 5 * MINUTE_MS
DAY_MS = 24 * 60 * MINUTE_MS
DAY1 = START_MS - 140 * MINUTE_MS  # 07:10 New York: bar 34 opens at 10:00
SETUP, CONFIRM, FILL = 34, 35, 36


# -- reference formulas -------------------------------------------------------------------


def ref_sma(xs: Sequence[float], n: int) -> float:
    return sum(xs[-n:]) / n


def ref_pstdev(xs: Sequence[float], n: int) -> float:
    w = xs[-n:]
    m = sum(w) / n
    return math.sqrt(sum((x - m) ** 2 for x in w) / n)


def ref_rma(xs: Sequence[float], n: int) -> float:
    v = sum(xs[:n]) / n
    for x in xs[n:]:
        v = (x + (n - 1) * v) / n
    return v


def ref_atr(rows: Sequence[OHLC], n: int) -> float:
    # ta.atr = ta.rma(ta.tr(true), n): the first bar's true range is high - low.
    trs = [rows[0][1] - rows[0][2]]
    trs += [
        max(h - lo, abs(h - prev[3]), abs(lo - prev[3]))
        for (_, h, lo, _), prev in zip(rows[1:], rows, strict=False)
    ]
    return ref_rma(trs, n)


def ref_rsi2(xs: Sequence[float]) -> float:
    ch = [b - a for a, b in itertools.pairwise(xs)]
    up = ref_rma([max(c, 0.0) for c in ch], 2)
    down = ref_rma([max(-c, 0.0) for c in ch], 2)
    return 100.0 - 100.0 / (1.0 + up / down)


def closes_of(rows: Sequence[OHLC]) -> list[float]:
    return [r[3] for r in rows]


# -- bars ---------------------------------------------------------------------------------


def chain(rows: list[OHLC], closes: Sequence[float]) -> list[OHLC]:
    prev = rows[-1][3] if rows else closes[0]
    for c in closes:
        rows.append((prev, max(prev, c) + 0.25, min(prev, c) - 0.25, c))
        prev = c
    return rows


def alternating(count: int) -> list[float]:
    return [100.0 if i % 2 == 0 else 101.0 for i in range(count)]


def signal(rows: list[OHLC]) -> list[OHLC]:
    rows.append((rows[-1][3], rows[-1][3] + 0.25, 92.75, 93.0))  # setup: close 93
    rows.append((93.0, 97.25, 92.75, 97.0))  # confirm: close 97
    return rows


def base_rows(fill_bar: OHLC = (97.5, 98.0, 97.25, 98.0)) -> list[OHLC]:
    """Bars 0-9 ramp 90..99, 10-33 alternate 100/101, 34 setup, 35 confirm, 36 fill, 37 exit."""
    rows = signal(chain([], [90.0 + i for i in range(10)] + alternating(24)))
    rows.append(fill_bar)
    rows.append((98.0, 101.0, 97.75, 100.5))
    return rows


def second_signal(rows: list[OHLC]) -> list[OHLC]:
    """20 quiet bars, then the same setup / confirm again (bars 58 / 59), fill, exit."""
    chain(rows, alternating(20))
    signal(rows)
    rows.append((97.5, 98.0, 97.25, 98.0))
    rows.append((98.0, 101.0, 97.75, 100.5))
    return rows


def series(rows: Sequence[OHLC], times: Sequence[int] | None = None) -> BarSeries:
    stamps = times if times is not None else [DAY1 + i * BAR_MS for i in range(len(rows))]
    bars = tuple(
        Bar(ts_open_ms=t, open=o, high=h, low=lo, close=c, volume=1.0)
        for t, (o, h, lo, c) in zip(stamps, rows, strict=True)
    )
    source = DataSource(name="trace", format="generic", sha256="", synthetic=True, bar_tz="UTC")
    return BarSeries(symbol="TEST", tf_seconds=TF, bars=bars, source=source)


@dataclass(frozen=True, slots=True)
class State:
    trades_today: int
    in_dir: int
    in_stop: float | None
    atr_stored: float | None
    daily_halt: bool
    day_start_equity: float | None


class Tracer:
    """Runs the port; records the actions and the `var` state after every bar."""

    def __init__(self, params: MeanReversionParams | None = None) -> None:
        self.inner = MeanReversion(params)
        self.actions: list[list[Action]] = []
        self.states: list[State] = []
        self.fills: list[tuple[FillEvent, list[Action]]] = []

    @property
    def strategy_id(self) -> str:
        return self.inner.strategy_id

    @property
    def pyramiding(self) -> int:
        return self.inner.pyramiding

    def on_bar(self, bar: Bar, ctx: StrategyContext) -> Sequence[Action]:
        out = list(self.inner.on_bar(bar, ctx))
        s = self.inner
        self.actions.append(out)
        self.states.append(
            State(
                s.trades_today, s.in_dir, s.in_stop, s.atr_stored, s.daily_halt, s.day_start_equity
            )
        )
        return out

    def on_fill(self, fill: FillEvent, ctx: StrategyContext) -> Sequence[Action]:
        out = list(self.inner.on_fill(fill, ctx))
        self.fills.append((fill, out))
        return out

    def entry_bars(self) -> list[int]:
        return [i for i, acts in enumerate(self.actions) if any(isinstance(a, Entry) for a in acts)]


def trace(
    rows: Sequence[OHLC] | BarSeries,
    params: MeanReversionParams | None = None,
    *,
    model: FillModel = P,
    session_last: Sequence[bool] | None = None,
    instrument: Instrument | None = None,
) -> tuple[EngineResult, Tracer]:
    s = rows if isinstance(rows, BarSeries) else series(rows)
    flags = session_last if session_last is not None else [i == len(s) - 1 for i in range(len(s))]
    tracer = Tracer(params)
    config = make_config(instrument=instrument or mes(), fill_model=model)
    return run_backtest(s, tracer, config, session_last=flags), tracer


# -- the full per-bar trace of one trade -------------------------------------------------


ATR14_AT_CONFIRM = 2.1645817115399932  # rma(tr, 14) at bar 35
BASIS_AT_CONFIRM = 99.95  # sma(close, 20) at bar 35
BASIS_AT_FILL = 99.85  # sma(close, 20) at bar 36


def test_reference_values_of_the_trace() -> None:
    rows = base_rows()
    c = closes_of(rows)
    assert ref_atr(rows[: CONFIRM + 1], 14) == pytest.approx(ATR14_AT_CONFIRM, abs=1e-12)
    assert ref_sma(c[: CONFIRM + 1], 20) == pytest.approx(BASIS_AT_CONFIRM)
    assert ref_sma(c[: FILL + 1], 20) == pytest.approx(BASIS_AT_FILL)
    # bar 34: close 93 < lower 95.8725, rsi2 7.41 < 10; BB upper 104.43 > KC upper 101.96.
    setup = c[: SETUP + 1]
    assert c[SETUP] < ref_sma(setup, 20) - 2.5 * ref_pstdev(setup, 20)
    assert ref_rsi2(setup) < 10.0
    # bar 33 is no setup, bar 35 is no setup (close 97 is inside): exactly one confirm.
    assert ref_rsi2(c[:SETUP]) > 10.0
    assert c[CONFIRM] > ref_sma(c[: CONFIRM + 1], 20) - 2.5 * ref_pstdev(c[: CONFIRM + 1], 20)


@pytest.mark.parametrize("model", [P, TV])
def test_per_bar_trace_of_one_long_trade(model: FillModel) -> None:
    """Hand execution, bar by bar:

    bars 0-34  no position, longSetup[1] false               -> no order; tradesToday 0
    bar 35     goLong: qty = floor(500 / (2*2.16458*5)) = 23; atrStored := 2.16458;
               entry "Long" 23 "MR Long"; exit "Exit L" stop 97 - 4.32916 = 92.67084,
               limit max(99.95, 97.25) = 99.95; tradesToday 1     (L342-353)
    bar 36     fills at the open 97.5; position > 0 and na(inStop): inDir 1,
               inStop := 97.5 - 4.32916 = 93.17084 (L303-305); exit re-issued with
               limit max(99.85, 97.75) = 99.85 (L313-315)
    bar 37     high 101 trades 1 tick through 99.85 -> target fill; flat at the close:
               RESET inDir 0, inStop na, atrStored na (L334-337); no new order (cap 1)
    """
    rows = base_rows()
    result, t = trace(rows, model=model)
    dist = 2.0 * ATR14_AT_CONFIRM
    stop = 97.5 - dist

    for i in range(CONFIRM):
        assert t.actions[i] == [], i
        assert t.states[i].trades_today == 0
        assert t.states[i].atr_stored is None
    assert t.actions[CONFIRM] == [
        Entry("Long", Side.LONG, 23.0, comment="MR Long"),
        Exit("Exit L", "Long", stop=pytest.approx(97.0 - dist), limit=pytest.approx(99.95)),  # type: ignore[arg-type]
    ]
    assert t.states[CONFIRM] == State(
        1,
        0,
        None,
        pytest.approx(ATR14_AT_CONFIRM),  # type: ignore[arg-type]
        False,
        100_000.0,
    )
    assert t.actions[FILL] == [
        Exit("Exit L", "Long", stop=pytest.approx(stop), limit=pytest.approx(BASIS_AT_FILL))  # type: ignore[arg-type]
    ]
    assert t.states[FILL].in_dir == 1
    assert t.states[FILL].in_stop == pytest.approx(93.17083657692)
    assert t.actions[FILL + 1] == []
    assert t.states[FILL + 1] == State(1, 0, None, None, False, 100_000.0)

    (trade,) = result.trades
    assert (trade.entry_price, trade.qty, trade.reason) == (97.5, 23.0, ExitReason.TARGET)
    assert trade.exit_price == pytest.approx(BASIS_AT_FILL)
    assert trade.initial_stop == pytest.approx(stop)


@pytest.mark.parametrize("model", [P, TV])
def test_a_favourable_gap_fill_re_prices_the_stop_wider_from_the_fill(model: FillModel) -> None:
    """L305: inStop := position_avg_price - atrMult * atrStored, with no clamp to the estimate.

    The fill bar opens at 96.5, half a point under the confirmation close 97: the estimate
    92.67084 (bar 35) becomes 96.5 - 4.32916 = 92.17084, a stop half a point further away.
    The quantity stays 23 (it was solved at the signal bar).
    """
    rows = base_rows(fill_bar=(96.5, 98.0, 96.25, 98.0))
    result, t = trace(rows, model=model)
    stop = 96.5 - 2.0 * ATR14_AT_CONFIRM
    assert stop == pytest.approx(92.17083657692)
    assert t.states[FILL].in_stop == pytest.approx(stop)
    (trade,) = result.trades
    assert trade.entry_price == 96.5
    assert trade.qty == 23.0
    assert trade.initial_stop == pytest.approx(stop)


# -- na handling ----------------------------------------------------------------------------


def squeeze_rows() -> list[OHLC]:
    """The base rows with 5-point wicks: KC(20) is wider than BB, a squeeze at bar 34."""
    plain = chain([], [90.0 + i for i in range(10)] + alternating(24))
    rows = signal([(o, h + 5.0, lo - 5.0, c) for o, h, lo, c in plain])
    rows.append((97.5, 98.0, 97.25, 98.0))
    return rows


def test_an_na_keltner_channel_is_no_squeeze() -> None:
    """L207: isSqueeze = bbUpper < kcUpper and bbLower > kcLower. With kcLen 40 the ATR(40)
    is na on bar 34 (35 bars), the comparisons are false, so volExpansion is true and the
    setup stands even though the same bars ARE a squeeze once KC has a value (kcLen 20)."""
    rows = squeeze_rows()
    setup = closes_of(rows[: SETUP + 1])
    kc_range = 1.5 * ref_atr(rows[: SETUP + 1], 20)
    ema20 = setup[0]
    for x in setup[1:]:
        ema20 = 2.0 / 21 * x + (1 - 2.0 / 21) * ema20
    assert ref_sma(setup, 20) + 2.5 * ref_pstdev(setup, 20) < ema20 + kc_range
    assert ref_sma(setup, 20) - 2.5 * ref_pstdev(setup, 20) > ema20 - kc_range

    _, t = trace(rows)
    assert t.entry_bars() == []
    _, t = trace(rows, MeanReversionParams(kc_len=40))
    assert t.entry_bars() == [CONFIRM]


def test_an_na_atr_sizes_zero_and_skips_without_touching_state() -> None:
    """L176: _perUnit > 0 is false for na, _raw 0, qtyL 0 -> the `if qtyL > 0` body (atrStored,
    orders, tradesToday += 1) does not run. ATR(40) is na on bar 35 (36 bars)."""
    _, t = trace(base_rows(), MeanReversionParams(atr_len=40))
    assert t.entry_bars() == []
    assert t.states[CONFIRM].trades_today == 0
    assert t.states[CONFIRM].atr_stored is None


# -- the daily breaker ----------------------------------------------------------------------


@pytest.mark.parametrize(("fill_close", "halts"), [(96.5, True), (96.75, False)])
def test_daily_breaker_compares_open_equity_with_greater_or_equal(
    fill_close: float, halts: bool
) -> None:
    """L252-258 at bar 36's close. Fixed balance 88 000: qty = floor(440 / 21.6458) = 20, so
    one point under the fill 97.5 is 100 $ of OPEN loss: dayDDPct = (100000 - 99900) /
    100000 * 100 = 0.1 >= 0.1 -> halt and flatten "Daily loss halt" (L328). 0.75 point =
    0.075 % -> no halt; the position runs to the target."""
    params = MeanReversionParams(risk_base="Fixed Balance", fixed_bal=88_000.0, max_daily_loss=0.1)
    rows = base_rows(fill_bar=(97.5, 97.75, 96.25, fill_close))
    result, t = trace(rows, params)
    assert t.actions[CONFIRM][0] == Entry("Long", Side.LONG, 20.0, comment="MR Long")
    assert t.states[FILL].daily_halt is halts
    (trade,) = result.trades
    if halts:
        assert (trade.reason, trade.comment, trade.exit_bar_index) == (
            ExitReason.CLOSE,
            "Daily loss halt",
            FILL,
        )
        assert trade.exit_price == 96.5
    else:
        assert trade.reason is ExitReason.TARGET


def test_daily_halt_and_day_start_equity_reset_on_the_next_new_york_day() -> None:
    """Day 1 halts at bar 36 (L257). Day 2 (Tuesday) starts at bar 38: isNewDay ->
    dayStartEquity := equity 99 885 (100 000 - 115), dailyHalt := false, tradesToday := 0
    (L249-263), and the day-2 signal at bar 59 enters."""
    rows = second_signal(base_rows(fill_bar=(97.5, 97.75, 96.25, 96.5)))
    day2 = START_MS + DAY_MS - 70 * MINUTE_MS  # bar 58 opens 10:00 on Tuesday
    times = [DAY1 + i * BAR_MS for i in range(38)]
    times += [day2 + i * BAR_MS for i in range(len(rows) - 38)]
    flags = [i in (37, len(rows) - 1) for i in range(len(rows))]
    params = MeanReversionParams(max_daily_loss=0.1)
    result, t = trace(series(rows, times), params, session_last=flags)
    assert t.states[37].daily_halt
    assert t.states[38].daily_halt is False
    assert t.states[38].day_start_equity == pytest.approx(100_000.0 - 115.0)
    assert t.entry_bars() == [CONFIRM, 59]
    assert len(result.trades) == 2


def test_new_day_is_a_change_of_day_of_month_not_of_date() -> None:
    """L239: isNewDay = ta.change(dayofmonth(time, tz)) != 0. After a data gap from 2 March to
    2 April the day of month is unchanged, so Pine sees no new day: tradesToday stays 1 and the
    second signal is blocked by maxTradesDay. A gap to 3 April is a new day."""
    rows = second_signal(base_rows())
    flags = [i in (37, len(rows) - 1) for i in range(len(rows))]

    def times(next_day_ms: int) -> list[int]:
        out = [DAY1 + i * BAR_MS for i in range(38)]
        return out + [next_day_ms + i * BAR_MS for i in range(len(rows) - 38)]

    april_2 = DAY1 + 31 * DAY_MS + 70 * MINUTE_MS  # bar 38 at 08:20, bar 58 at 10:00
    _, t = trace(series(rows, times(april_2)), session_last=flags)
    assert t.states[38].trades_today == 1
    assert t.entry_bars() == [CONFIRM]
    _, t = trace(series(rows, times(april_2 + DAY_MS)), session_last=flags)
    assert t.entry_bars() == [CONFIRM, 59]


# -- sizing -----------------------------------------------------------------------------------


def test_account_equity_risk_base_uses_the_equity_at_the_signal_close() -> None:
    """L168: riskBaseAmt = strategy.equity on the signal bar. Trade 1 (CFD, point value 1,
    fractional) is stopped at 93.17084 on bar 37; trade 2 is sized at bar 59 from
    100 000 + pnl1, not from the initial capital."""
    rows = base_rows()
    rows[37] = (98.0, 98.25, 92.0, 92.5)  # through the stop
    rows = second_signal(rows)
    instrument = cfd(tick_size=0.25, point_value=1.0)
    params = MeanReversionParams(risk_base="Account Equity", max_trades_day=2)
    result, t = trace(rows, params, instrument=instrument)
    assert t.entry_bars() == [CONFIRM, 59]
    first, second = result.trades
    assert first.reason is ExitReason.STOP
    assert first.qty == pytest.approx(115.49)  # floor(500 / 4.32916, 0.01)
    dist2 = 2.0 * ref_atr(rows[:60], 14)
    expected = math.floor((100_000.0 + first.pnl) * 0.005 / dist2 * 100.0) / 100.0
    from_initial = math.floor(500.0 / dist2 * 100.0) / 100.0
    assert expected != from_initial
    assert second.qty == pytest.approx(expected)
