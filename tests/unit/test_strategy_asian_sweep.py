"""The Asian sweep port (`propbot.strategies.asian_sweep`) against hand-executed Pine.

Every expected number below was derived by walking `pine/asian_sweep_reversal_strategy.pine`
by hand over the bars of the test, block by block in the script's order. Most tests use
compact windows so a whole cycle fits in a few 1-minute bars:

    Asian  [23:58, 00:01)  New York, wraps midnight (bars 1-3 from DAY1)
    Active [00:02, 00:20)

The instrument is MES-like (tick 0.25, point value 5, whole contracts), costs are zero, the
initial capital is 100 000 and the risk is 0.5 % of it: 500 per trade, so the size is
100 / stop distance contracts.
"""

import datetime as dt
import re
import typing
from collections.abc import Sequence
from typing import Any
from zoneinfo import ZoneInfo

import pytest
from pydantic import ValidationError

from propbot.backtest.engine import run_backtest
from propbot.backtest.model import EngineResult, ExitReason, FillModel
from propbot.core.bars import Bar, BarSeries, DataSource
from propbot.signals.schema import STRATEGY_NAMES
from propbot.strategies.asian_sweep import (
    PINE_FILE,
    STRATEGY_ID,
    AsianSweep,
    AsianSweepParams,
)
from propbot.strategies.base import Action, CloseAll, Entry, Exit, FillEvent, Side, StrategyContext
from support.engine import cfd, make_config, mes
from support.pine import PINE_DIR, pine_defaults

NY = ZoneInfo("America/New_York")
type OHLC = tuple[float, float, float, float]

BOTH_MODELS = pytest.mark.parametrize("fill_model", [FillModel.PESSIMISTIC, FillModel.TV_OHLC])

# Pine input -> AsianSweepParams field.
PORTED: dict[str, str] = {
    "tz": "tz",
    "asianStartHour": "asian_start_hour",
    "asianStartMin": "asian_start_min",
    "asianEndHour": "asian_end_hour",
    "asianEndMin": "asian_end_min",
    "activeStartHour": "active_start_hour",
    "activeStartMin": "active_start_min",
    "activeEndHour": "active_end_hour",
    "activeEndMin": "active_end_min",
    "closeAtEnd": "close_at_end",
    "startDate": "start_date",
    "endDate": "end_date",
    "tradeMon": "trade_mon",
    "tradeTue": "trade_tue",
    "tradeWed": "trade_wed",
    "tradeThu": "trade_thu",
    "tradeFri": "trade_fri",
    "maxBars": "max_bars",
    "minFvgTicks": "min_fvg_ticks",
    "allowLong": "allow_long",
    "allowShort": "allow_short",
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
}
# Visuals, the stats table, the OOS marker (drives only visuals and the table) and the alert token.
NOT_PORTED: frozenset[str] = frozenset(
    {
        "oosStart",
        "showDateViz",
        "showTable",
        "tableScope",
        "tablePos",
        "tableSize",
        "showRange",
        "rangeProj",
        "showZones",
        "boxProj",
        "alertToken",
    }
)

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

# Bar 0 opens Monday 2026-03-02 23:57 New York (EST): bars 1-3 are the Asian window
# (23:58, 23:59, 00:00 Tuesday), bar 4 (00:01) ends it, bar 5 (00:02) is the first active bar.
DAY1 = dt.datetime(2026, 3, 2, 23, 57, tzinfo=NY)
DAY2 = dt.datetime(2026, 3, 3, 23, 57, tzinfo=NY)

# The short setup: range High 102 / Low 98; bar 5 sweeps the High (first active bar, also a
# cross); bar 7 is a bearish FVG [101.25, 101.5] closing below the High; bar 8 touches it.
SHORT_ROWS: tuple[OHLC, ...] = (
    (100.0, 100.5, 99.5, 100.0),  # 0 23:57 outside both windows
    (100.0, 101.0, 99.0, 100.0),  # 1 23:58 Asian start
    (100.0, 102.0, 99.5, 101.0),  # 2 23:59
    (101.0, 101.5, 98.0, 99.0),  # 3 00:00 (Tuesday, still Asian: the window wraps)
    (101.0, 101.5, 100.5, 101.5),  # 4 00:01 Asian end -> range ready
    (101.5, 103.0, 101.5, 102.5),  # 5 00:02 sweep of the High
    (102.5, 102.75, 100.5, 101.0),  # 6
    (101.0, 101.25, 100.0, 100.25),  # 7 bearish FVG: high 101.25 < low[2] 101.5 -> arm short
    (100.25, 101.5, 100.0, 100.75),  # 8 touch: high >= 101.25 -> signal
    (100.5, 100.75, 99.5, 99.75),  # 9 fill at the open, 100.5
    (99.75, 100.0, 97.25, 97.5),  # 10 target 97.5 (low 97.25 is one tick through)
)

# The long setup: the short one mirrored around 100 (p -> 200 - p).
LONG_ROWS: tuple[OHLC, ...] = tuple(
    (200.0 - o, 200.0 - lo, 200.0 - h, 200.0 - c) for o, h, lo, c in SHORT_ROWS
)


def _ms(moment: dt.datetime) -> int:
    return int(moment.timestamp() * 1000)


def series_of(stamped: Sequence[tuple[int, OHLC]], *, tf_seconds: int = 60) -> BarSeries:
    """A series with explicit open times (gaps allowed)."""
    bars = tuple(
        Bar(ts_open_ms=ts, open=o, high=h, low=lo, close=c, volume=1.0)
        for ts, (o, h, lo, c) in stamped
    )
    source = DataSource(name="test", format="generic", sha256="", synthetic=True, bar_tz="UTC")
    return BarSeries(symbol="TEST", tf_seconds=tf_seconds, bars=bars, source=source)


def stamped_from(
    start: dt.datetime, rows: Sequence[OHLC], *, tf_seconds: int = 60
) -> list[tuple[int, OHLC]]:
    first = _ms(start)
    return [(first + i * tf_seconds * 1000, row) for i, row in enumerate(rows)]


class Recorder:
    """Drives an AsianSweep and records what it returned and its state after each bar."""

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


def run_series(
    series: BarSeries,
    *,
    fill_model: FillModel = FillModel.PESSIMISTIC,
    session_last: Sequence[bool] | None = None,
    compact: bool = True,
    **overrides: Any,
) -> tuple[EngineResult, Recorder]:
    params = AsianSweepParams(**({**COMPACT, **overrides} if compact else overrides))
    recorder = Recorder(AsianSweep(params))
    flags = (
        list(session_last)
        if session_last is not None
        else [i == len(series) - 1 for i in range(len(series))]
    )
    config = make_config(instrument=mes(), fill_model=fill_model)
    return run_backtest(series, recorder, config, session_last=flags), recorder


def run_rows(
    rows: Sequence[OHLC], *, start: dt.datetime = DAY1, **kwargs: Any
) -> tuple[EngineResult, Recorder]:
    """Consecutive 1-minute bars from `start` (default: DAY1, Monday 23:57)."""
    return run_series(series_of(stamped_from(start, rows)), **kwargs)


def short_bracket(qty: float, stop: float, target: float) -> list[Action]:
    return [Entry("Short", Side.SHORT, qty, comment="Short"), Exit("Exit S", "Short", stop, target)]


# -- a) parameters mirror the Pine inputs --------------------------------------------------


def test_every_pine_input_is_either_ported_or_deliberately_left_out() -> None:
    names = set(pine_defaults(PINE_FILE))

    assert set(PORTED) | NOT_PORTED == names
    assert not set(PORTED) & NOT_PORTED
    assert set(PORTED.values()) == set(AsianSweepParams.model_fields)


def test_every_ported_default_equals_the_pine_default() -> None:
    defaults = pine_defaults(PINE_FILE)
    params = AsianSweepParams()

    for pine_name, field in PORTED.items():
        value = getattr(params, field)
        assert value == defaults[pine_name], pine_name
        assert type(value) is type(defaults[pine_name]), pine_name


def _pine_options(pine_name: str) -> tuple[str, ...]:
    source = (PINE_DIR / PINE_FILE).read_text(encoding="utf-8")
    line = next(ln for ln in source.splitlines() if ln.startswith(f"{pine_name} "))
    match = re.search(r"options\s*=\s*\[([^\]]*)\]", line)
    assert match is not None
    return tuple(re.findall(r'"([^"]*)"', match.group(1)))


@pytest.mark.parametrize(
    ("pine_name", "field"),
    [("riskBase", "risk_base"), ("stopMethod", "stop_method"), ("qtyRound", "qty_round")],
)
def test_option_inputs_accept_exactly_the_pine_option_strings(pine_name: str, field: str) -> None:
    annotation = AsianSweepParams.model_fields[field].annotation
    literal = annotation.__value__ if hasattr(annotation, "__value__") else annotation

    assert typing.get_args(literal) == _pine_options(pine_name)


@pytest.mark.parametrize(
    ("field", "bad"),
    [
        ("asian_start_hour", 24),
        ("asian_end_min", 60),
        ("active_start_hour", -1),
        ("max_bars", 0),
        ("min_fvg_ticks", -1),
        ("risk_pct", 0.05),
        ("fixed_bal", 0.5),
        ("rr", 0.05),
        ("sl_buf", -1),
        ("atr_len_init", 0),
        ("atr_mult_init", 0.05),
        ("daily_dd_pct", 0.05),
        ("max_consec_loss", 0),
        ("stop_method", "Swing"),
        ("tz", "Mars/Olympus"),
        ("start_date", dt.datetime(2024, 1, 1)),  # naive: refused
    ],
)
def test_values_outside_the_pine_minval_maxval_or_options_are_refused(field: str, bad: Any) -> None:
    with pytest.raises(ValidationError):
        AsianSweepParams(**{field: bad})


def test_module_surface_matches_the_registry() -> None:
    strategy = AsianSweep()

    assert STRATEGY_ID in STRATEGY_NAMES
    assert strategy.strategy_id == STRATEGY_ID
    assert strategy.pyramiding == 0  # strategy(pyramiding = 0)
    assert AsianSweep().params == AsianSweepParams()


# -- b) the cycle, the windows and the in-bar order -----------------------------------------


@BOTH_MODELS
def test_short_setup_end_to_end(fill_model: FillModel) -> None:
    result, rec = run_rows(SHORT_ROWS, fill_model=fill_model)

    # The touch bar sends the market entry with its bracket: stop = armTop 101.5 + 2 ticks,
    # size = 500 / ((102 - 100.75) * 5) = 80, target = 100.75 - 2 * 1.25.
    assert rec.entries() == {8: short_bracket(80.0, 102.0, 98.25)}
    # The arming bar 7 already reaches armBot (high 101.25) but entries need a later bar.
    assert rec.actions[7] == []
    # Re-priced from the fill 100.5: the FVG stop stays 102, target = 100.5 - 2 * 1.5.
    assert rec.actions[9] == [Exit("Exit S", "Short", 102.0, 97.5)]
    (trade,) = result.trades
    assert trade.side is Side.SHORT
    assert trade.qty == 80.0
    assert (trade.entry_bar_index, trade.entry_price) == (9, 100.5)
    assert (trade.exit_bar_index, trade.exit_price) == (10, 97.5)
    assert trade.reason is ExitReason.TARGET
    assert trade.initial_stop == 102.0
    assert trade.pnl == pytest.approx(3.0 * 80 * 5)
    assert trade.setup == "Short"


def test_the_asian_window_wraps_midnight_and_is_final_on_its_first_bar_after() -> None:
    _, rec = run_rows(SHORT_ROWS)
    s = rec.states

    # Bar 3 opens at 00:00 Tuesday and still belongs to the range (its low 98 is the Low).
    assert (s[3]["asian_high"], s[3]["asian_low"], s[3]["asian_ready"]) == (102.0, 98.0, False)
    assert s[4]["asian_ready"] is True
    assert (s[5]["sweep_hi_bar"], s[5]["sweep_lo_bar"]) == (5, -1)
    armed = (s[7]["arm_dir"], s[7]["arm_top"], s[7]["arm_bot"], s[7]["arm_bar"])
    assert armed == (-1, 101.5, 101.25, 7)
    assert s[7]["sweep_hi_bar"] == -1  # the sweep is consumed by the arming
    # Management vars are reset on the bar the trade is seen closed.
    assert (s[9]["in_dir"], s[9]["in_initial_stop"], s[9]["in_take_profit"]) == (-1, 102.0, 97.5)
    assert (s[10]["in_dir"], s[10]["in_take_profit"]) == (0, None)


def test_bars_after_the_asian_window_do_not_extend_the_range() -> None:
    rows = list(SHORT_ROWS)
    rows[4] = (101.0, 101.75, 100.5, 101.5)  # 00:01: above nothing, below the 102 High

    _, rec = run_rows(rows)

    assert rec.states[4]["asian_high"] == 102.0
    rows[4] = (101.0, 105.0, 100.5, 101.5)  # 00:01 trades far above the range
    _, rec = run_rows(rows)
    assert rec.states[4]["asian_high"] == 102.0
    assert rec.states[4]["asian_ready"] is True


def test_the_first_asian_bar_starts_a_new_cycle_and_restores_the_daily_trade_right() -> None:
    # Day 1: the short trade, then a second sweep + arming + touch the same cycle (bars 11-15).
    day1 = [*SHORT_ROWS, *SHORT_ROWS[4:9]]
    stamped = stamped_from(DAY1, day1) + stamped_from(DAY2, SHORT_ROWS)

    result, rec = run_series(series_of(stamped))
    s = rec.states

    assert [t.entry_bar_index for t in result.trades] == [9, 16 + 9]
    # The second day-1 setup arms (bar 14) and touches (bar 15) but one trade per day.
    assert (s[14]["arm_dir"], s[14]["arm_bar"]) == (-1, 14)
    assert 15 not in rec.entries()
    assert s[15]["arm_dir"] == -1
    assert s[15]["traded_today"] is True
    # Day 2's first Asian bar (index 17, 23:58 Tuesday) resets the cycle.
    assert s[17]["traded_today"] is False
    assert (s[17]["arm_dir"], s[17]["sweep_hi_bar"], s[17]["sweep_lo_bar"]) == (0, -1, -1)
    assert (s[17]["asian_high"], s[17]["asian_low"], s[17]["asian_ready"]) == (101.0, 99.0, False)
    assert rec.entries()[16 + 8] == short_bracket(80.0, 102.0, 98.25)


def test_long_setup_with_a_sweep_by_crossing_on_a_later_active_bar() -> None:
    # Active from 00:01: bar 4 ends the Asian window AND is the first active bar; the range is
    # final before canTrade is evaluated. Bar 5 sweeps the Low by crossing (low[1] 98.5 >= 98).
    result, rec = run_rows(LONG_ROWS, active_start_min=1)

    assert rec.states[4]["asian_ready"] is True
    assert rec.states[4]["sweep_lo_bar"] == -1
    assert rec.states[5]["sweep_lo_bar"] == 5
    assert (rec.states[7]["arm_dir"], rec.states[7]["arm_top"], rec.states[7]["arm_bot"]) == (
        1,
        98.75,
        98.5,
    )
    # stop = armBot 98.5 - 2 ticks = 98; size = 500 / (1.25 * 5); target = 99.25 + 2 * 1.25.
    assert rec.entries() == {
        8: [Entry("Long", Side.LONG, 80.0, comment="Long"), Exit("Exit L", "Long", 98.0, 101.75)]
    }
    assert rec.actions[9] == [Exit("Exit L", "Long", 98.0, 102.5)]
    (trade,) = result.trades
    assert (trade.entry_price, trade.exit_price, trade.reason) == (99.5, 102.5, ExitReason.TARGET)


def test_a_bar_above_the_high_is_a_sweep_on_the_first_active_bar_only() -> None:
    # Base: bar 6 (high 102.75 after 103) is no cross, so the sweep stays at bar 5.
    _, base = run_rows(SHORT_ROWS)
    assert base.states[6]["sweep_hi_bar"] == 5

    # Active from 00:03: bar 5 is not seen; bar 6 is the first active bar and sweeps.
    result, rec = run_rows(SHORT_ROWS, active_start_min=3)

    assert rec.states[5]["sweep_hi_bar"] == -1
    assert rec.states[6]["sweep_hi_bar"] == 6
    assert rec.entries() == {8: short_bracket(80.0, 102.0, 98.25)}
    assert len(result.trades) == 1


def test_arming_runs_before_sweep_detection_on_the_same_bar() -> None:
    rows = [
        *SHORT_ROWS[:4],
        (103.0, 104.0, 103.0, 103.5),  # 4 00:01 not active: no sweep recorded
        (102.0, 102.0, 101.0, 101.5),  # 5 first active bar, high == 102: no sweep
        (101.5, 102.5, 101.0, 101.5),  # 6 crosses 102 AND is a bearish FVG below low[2] 103
    ]

    _, rec = run_rows(rows)

    # The sweep of bar 6 is recorded only after the arming check: bar 6 cannot arm on it.
    assert rec.states[6]["sweep_hi_bar"] == 6
    assert rec.states[6]["arm_dir"] == 0


OVERWRITE_ROWS: tuple[OHLC, ...] = (
    *SHORT_ROWS[:4],
    (100.0, 100.5, 99.5, 100.0),  # 4 00:01
    (98.5, 98.5, 97.0, 97.5),  # 5 first active bar: sweep of the Low
    (97.5, 98.5, 97.25, 98.25),  # 6
    (98.75, 99.5, 98.75, 99.25),  # 7 bullish FVG [98.5, 98.75] -> arm long
    (99.25, 102.5, 99.25, 102.25),  # 8 crosses the High -> sweep_hi_bar 8
    (102.25, 103.0, 101.5, 102.0),  # 9
    (99.0, 99.0, 98.5, 98.75),  # 10 bearish FVG [99, 99.25] -> arm short; low touches long zone
    (98.75, 99.25, 98.5, 99.0),  # 11 touches the short zone
    (99.0, 99.25, 98.75, 99.0),  # 12 fill bar (the touch bar must not be the session's last)
)


@BOTH_MODELS
def test_a_new_arming_overwrites_the_old_one_before_the_entry_check(fill_model: FillModel) -> None:
    _, rec = run_rows(OVERWRITE_ROWS, fill_model=fill_model)

    assert (rec.states[7]["arm_dir"], rec.states[7]["arm_bar"]) == (1, 7)
    # Bar 10 touches the old long zone (low 98.5 <= 98.75), but arming runs first and replaces
    # it with the short; its touch needs a later bar. Entry-first would have bought here.
    assert rec.actions[10] == []
    assert (rec.states[10]["arm_dir"], rec.states[10]["arm_top"]) == (-1, 99.25)
    # stop = 99.25 + 0.5; size = floor(500 / (0.75 * 5)) = 133; target = 99 - 2 * 0.75.
    assert rec.entries() == {11: short_bracket(133.0, 99.75, 97.5)}


@pytest.mark.parametrize(("max_bars", "trades"), [(1, 0), (2, 1)])
def test_the_fvg_must_form_within_max_bars_of_the_sweep(max_bars: int, trades: int) -> None:
    # Sweep at bar 5, FVG at bar 7: two bars later.
    result, rec = run_rows(SHORT_ROWS, max_bars=max_bars)

    assert len(result.trades) == trades
    assert rec.states[7]["arm_dir"] == (-1 if trades else 0)


@pytest.mark.parametrize(
    ("fvg_bar", "armed"),
    [
        ((102.5, 102.75, 102.25, 102.5), False),  # closes above the High
        ((102.5, 102.75, 101.75, 102.0), False),  # closes AT the High: not below it
        ((102.5, 102.75, 101.5, 101.75), True),
    ],
)
def test_the_short_fvg_must_close_back_below_the_high(fvg_bar: OHLC, armed: bool) -> None:
    rows = [
        *SHORT_ROWS[:5],
        (103.0, 105.0, 103.0, 104.5),  # 5 sweep, low 103
        (104.5, 104.75, 103.0, 103.25),  # 6
        fvg_bar,  # 7 high 102.75 < low[2] 103
    ]

    _, rec = run_rows(rows)

    assert rec.states[7]["arm_dir"] == (-1 if armed else 0)


@pytest.mark.parametrize(("min_ticks", "armed"), [(0, True), (1, True), (2, False)])
def test_the_fvg_size_filter_compares_in_ticks(min_ticks: int, armed: bool) -> None:
    # The FVG of bar 7 is exactly one tick (101.5 - 101.25).
    _, rec = run_rows(SHORT_ROWS[:8], min_fvg_ticks=min_ticks)

    assert rec.states[7]["arm_dir"] == (-1 if armed else 0)


@pytest.mark.parametrize(("allow_short", "trades"), [(True, 1), (False, 0)])
def test_allow_short_switches_the_short_side(allow_short: bool, trades: int) -> None:
    result, _ = run_rows(SHORT_ROWS, allow_short=allow_short)

    assert len(result.trades) == trades


def test_invalidation_runs_before_the_touch_check() -> None:
    rows = [
        *SHORT_ROWS[:8],
        (100.25, 102.0, 100.0, 101.75),  # 8 touches 101.25 but closes above armTop 101.5
        SHORT_ROWS[8],  # 9 a clean touch: too late, the setup is dead
        SHORT_ROWS[9],
    ]

    result, rec = run_rows(rows)

    assert rec.states[8]["arm_dir"] == 0
    assert rec.entries() == {}
    assert result.trades == ()


def test_a_size_that_rounds_to_zero_keeps_the_arming_and_retries_next_bar() -> None:
    rows = [
        *SHORT_ROWS[:9],
        (100.75, 101.5, 100.75, 101.0),  # 9 touches again, closer to the stop
        (101.0, 101.25, 100.75, 101.0),  # 10 fill
    ]

    # Risk 0.5 % of a fixed 1000 = 5. Bar 8: 5 / (1.25 * 5) = 0.8 -> 0: nothing is sent and
    # the arming stays. Bar 9: 5 / ((102 - 101) * 5) = 1 contract, target 101 - 2 * 1.
    _, rec = run_rows(rows, risk_base="Fixed Balance", fixed_bal=1000.0)

    assert rec.actions[8] == []
    assert (rec.states[8]["arm_dir"], rec.states[8]["traded_today"]) == (-1, False)
    assert rec.entries() == {9: short_bracket(1.0, 102.0, 99.0)}


@pytest.mark.parametrize(
    ("tf_seconds", "touch", "entered"),
    [
        (60, (10, 58), True),  # fill 10:59, inside [03:00, 11:00)
        (60, (10, 59), False),  # fill 11:00, outside
        (300, (10, 50), True),  # fill 10:55
        (300, (10, 55), False),  # fill 11:00
    ],
)
def test_the_fill_bar_must_open_inside_the_default_active_window(
    tf_seconds: int, touch: tuple[int, int], entered: bool
) -> None:
    step = dt.timedelta(seconds=tf_seconds)
    monday = dt.datetime(2026, 3, 2, 20, 0, tzinfo=NY)
    touch_at = dt.datetime(2026, 3, 3, *touch, tzinfo=NY)
    stamped = [
        (_ms(monday - step), SHORT_ROWS[0]),
        (_ms(monday), SHORT_ROWS[1]),  # Asian start (default 20:00)
        (_ms(monday + step), (100.0, 102.0, 98.0, 101.0)),  # range 102 / 98
        (_ms(dt.datetime(2026, 3, 3, 0, 0, tzinfo=NY)), SHORT_ROWS[4]),  # 00:00 ends it
        *(
            (_ms(touch_at + (i - 3) * step), SHORT_ROWS[5 + i])
            for i in range(5)  # sweep, -, FVG, touch, next bar
        ),
        (_ms(touch_at + 2 * step), (99.75, 100.0, 99.0, 99.5)),
    ]

    result, rec = run_series(series_of(stamped, tf_seconds=tf_seconds), compact=False)

    if entered:
        assert rec.entries() == {7: short_bracket(80.0, 102.0, 98.25)}
        # Default windows: the bar after the active window closes the trade at its close.
        (trade,) = result.trades
        assert (trade.entry_price, trade.exit_price) == (100.5, 99.5)
        assert (trade.reason, trade.comment) == (ExitReason.CLOSE, "Session end")
    else:
        assert rec.entries() == {}
        assert rec.states[7]["arm_dir"] == -1  # armed, only the gate said no


@pytest.mark.parametrize(("active_end_min", "entered"), [(6, False), (7, True)])
def test_no_entry_when_the_fill_bar_is_the_first_bar_after_the_window(
    active_end_min: int, entered: bool
) -> None:
    # The touch bar 8 opens at 00:05; its fill bar opens at 00:06.
    _, rec = run_rows(SHORT_ROWS, active_end_min=active_end_min)

    assert (8 in rec.entries()) is entered


@pytest.mark.parametrize(
    ("trade_mon", "trade_tue", "trades"), [(True, True, 1), (False, True, 1), (True, False, 0)]
)
def test_the_weekday_filter_reads_the_active_bar_day(
    trade_mon: bool, trade_tue: bool, trades: int
) -> None:
    # The Asian bars span Monday night; the active bars are on Tuesday.
    result, _ = run_rows(SHORT_ROWS, trade_mon=trade_mon, trade_tue=trade_tue)

    assert len(result.trades) == trades


# -- b) exits, re-pricing, forced flats, breakers, date range -------------------------------


@BOTH_MODELS
def test_close_when_the_active_window_ends(fill_model: FillModel) -> None:
    rows = [*SHORT_ROWS[:10], (99.75, 100.0, 99.0, 99.25)]  # bar 10 opens 00:07

    result, rec = run_rows(rows, fill_model=fill_model, active_end_min=7)

    assert rec.actions[10] == [Exit("Exit S", "Short", 102.0, 97.5), CloseAll("Session end")]
    (trade,) = result.trades
    assert (trade.exit_bar_index, trade.exit_price) == (10, 99.25)
    assert (trade.reason, trade.comment) == (ExitReason.CLOSE, "Session end")


def test_close_at_end_off_keeps_the_trade_past_the_window() -> None:
    rows = [*SHORT_ROWS[:10], (99.75, 100.0, 99.0, 99.25)]

    result, _ = run_rows(rows, active_end_min=7, close_at_end=False)

    (trade,) = result.trades
    assert trade.reason is ExitReason.END_OF_DATA


@BOTH_MODELS
def test_close_on_the_sessions_last_bar_inside_the_window(fill_model: FillModel) -> None:
    flags = [i == 9 for i in range(len(SHORT_ROWS))]
    flags[-1] = True

    result, _ = run_rows(SHORT_ROWS, fill_model=fill_model, session_last=flags)

    (trade,) = result.trades
    assert (trade.exit_bar_index, trade.exit_price) == (9, 99.75)
    assert (trade.reason, trade.comment) == (ExitReason.CLOSE, "Session end")


def test_no_entry_on_the_sessions_last_bar() -> None:
    flags = [i in (8, len(SHORT_ROWS) - 1) for i in range(len(SHORT_ROWS))]

    result, rec = run_rows(SHORT_ROWS, session_last=flags)

    assert rec.entries() == {}
    assert result.trades == ()


@BOTH_MODELS
def test_fill_bar_repricing_gives_the_same_levels_in_both_models(fill_model: FillModel) -> None:
    _, rec = run_rows(SHORT_ROWS, fill_model=fill_model)

    if fill_model is FillModel.PESSIMISTIC:
        # Pessimistic: re-priced at the fill instant; the fill bar's close re-issues it.
        ((fill, out),) = rec.fills
        assert (fill.price, fill.bar_index) == (100.5, 9)
        assert out == [Exit("Exit S", "Short", 102.0, 97.5)]
    else:
        assert rec.fills == []
    assert rec.actions[9] == [Exit("Exit S", "Short", 102.0, 97.5)]


@pytest.mark.parametrize(
    ("fill_model", "exit_bar", "exit_price"),
    [(FillModel.TV_OHLC, 9, 98.25), (FillModel.PESSIMISTIC, 10, 97.5)],
)
def test_only_the_timing_of_the_repricing_differs_between_models(
    fill_model: FillModel, exit_bar: int, exit_price: float
) -> None:
    rows = list(SHORT_ROWS)
    rows[9] = (100.5, 100.75, 98.0, 98.5)  # fill bar trades 98.0: 1 tick through 98.25

    result, _ = run_rows(rows, fill_model=fill_model)

    # tv_ohlc: the signal-bar target 98.25 still works inside the fill bar (Pine). Pessimistic:
    # on_fill moved it to 97.5 at the fill, reached only on bar 10.
    (trade,) = result.trades
    assert (trade.exit_bar_index, trade.exit_price) == (exit_bar, exit_price)
    assert trade.reason is ExitReason.TARGET
    assert trade.initial_stop == 102.0


@BOTH_MODELS
def test_atr_stop_is_a_distance_reapplied_to_the_fill(fill_model: FillModel) -> None:
    # ATR(1) on bar 8 = its true range: max(1.5, |101.5 - 100.25|, |100 - 100.25|) = 1.5.
    # Distance 1.5 * 1.5 = 2.25: est stop 100.75 + 2.25 = 103, size floor(500 / 11.25) = 44,
    # target 100.75 - 4.5. From the fill 100.5: stop 102.75, target 96.
    result, rec = run_rows(
        SHORT_ROWS, fill_model=fill_model, stop_method="ATR Based", atr_len_init=1
    )

    assert rec.entries() == {8: short_bracket(44.0, 103.0, 96.25)}
    assert rec.actions[9] == [Exit("Exit S", "Short", 102.75, 96.0)]
    (trade,) = result.trades
    assert trade.initial_stop == 102.75
    # Bar 10 is the session's last bar inside the window: closed at its close 97.5.
    assert (trade.exit_price, trade.comment) == (97.5, "Session end")
    assert trade.pnl == pytest.approx(3.0 * 44 * 5)


def test_atr_method_falls_back_to_the_fvg_stop_while_atr_is_na() -> None:
    # ATR(14) is na on bars 8 and 9 (fewer than 14 bars): the FVG levels of the base case.
    _, rec = run_rows(SHORT_ROWS, stop_method="ATR Based")

    assert rec.entries() == {8: short_bracket(80.0, 102.0, 98.25)}
    assert rec.actions[9] == [Exit("Exit S", "Short", 102.0, 97.5)]


DD_ROWS: tuple[OHLC, ...] = (
    *SHORT_ROWS[:9],
    (100.5, 101.0, 100.25, 100.75),  # 9 fill at 100.5, closes 0.25 against: -0.25 * 80 * 5 = -100
    SHORT_ROWS[10],
)


@BOTH_MODELS
def test_floating_daily_loss_breach_flattens_at_the_bar_close(fill_model: FillModel) -> None:
    # Cap = 0.1 % of the 100 000 cycle start = 100; bar 9's close equity is exactly -100.
    result, rec = run_rows(DD_ROWS, fill_model=fill_model, daily_dd_pct=0.1)

    assert rec.actions[9] == [Exit("Exit S", "Short", 102.0, 97.5), CloseAll("Daily DD halt")]
    assert rec.states[9]["halted_dd"] is True
    (trade,) = result.trades
    assert (trade.exit_bar_index, trade.exit_price, trade.comment) == (9, 100.75, "Daily DD halt")
    assert trade.pnl == pytest.approx(-100.0)


@pytest.mark.parametrize("overrides", [{"use_breakers": False}, {"flatten_breach": False}])
def test_no_breach_flatten_when_breakers_or_flatten_are_off(overrides: dict[str, bool]) -> None:
    result, _ = run_rows(DD_ROWS, daily_dd_pct=0.1, **overrides)

    (trade,) = result.trades
    assert (trade.exit_bar_index, trade.exit_price, trade.reason) == (10, 97.5, ExitReason.TARGET)


@pytest.mark.parametrize(("max_consec_loss", "day2_trade"), [(1, False), (2, True)])
def test_a_loss_folded_into_the_new_cycle_trips_the_streak_breaker(
    max_consec_loss: int, day2_trade: bool
) -> None:
    # The day-1 short is carried (close_at_end off) and stopped at 102 by day 2's first Asian
    # bar, after that bar reset the cycle: the loss counts in day 2 (-600).
    day2 = [SHORT_ROWS[0], (101.0, 102.5, 101.0, 102.25), *SHORT_ROWS[2:]]
    stamped = stamped_from(DAY1, SHORT_ROWS[:10]) + stamped_from(DAY2, day2)

    result, rec = run_series(
        series_of(stamped), close_at_end=False, max_consec_loss=max_consec_loss
    )

    first = result.trades[0]
    assert (first.exit_bar_index, first.exit_price, first.reason) == (11, 102.0, ExitReason.STOP)
    assert rec.states[11]["consec_loss_today"] == 1
    assert rec.states[11]["halted_streak"] is (max_consec_loss == 1)
    # Day 2: range 102.5 / 98, the same setup touches on bar 10 + 8.
    assert (10 + 8 in rec.entries()) is day2_trade
    assert len(result.trades) == 1 + day2_trade


def test_end_date_closes_the_open_trade_and_start_date_blocks_entries() -> None:
    rows = [*SHORT_ROWS[:10], (99.75, 100.0, 99.0, 99.25)]
    fill_bar_open = (DAY1 + dt.timedelta(minutes=9)).astimezone(dt.UTC)

    # No session end in this data, so the end date is the only flat reason on bar 10.
    no_session_end = [False] * len(rows)
    result, rec = run_rows(rows, end_date=fill_bar_open, session_last=no_session_end)

    assert rec.actions[10] == [Exit("Exit S", "Short", 102.0, 97.5), CloseAll("End of test range")]
    (trade,) = result.trades
    assert (trade.exit_price, trade.comment) == (99.25, "End of test range")

    later = dt.datetime(2026, 3, 4, tzinfo=dt.UTC)
    result, rec = run_rows(rows, start_date=later, session_last=no_session_end)
    assert rec.entries() == {}
    assert result.trades == ()


def test_quantity_rounding_of_a_cfd() -> None:
    series = series_of(stamped_from(DAY1, SHORT_ROWS))
    flags = [i == len(series) - 1 for i in range(len(series))]
    params = AsianSweepParams(**COMPACT, fixed_bal=3333.0, risk_base="Fixed Balance")
    recorder = Recorder(AsianSweep(params))
    config = make_config(instrument=cfd(tick_size=0.25, point_value=1.0))

    run_backtest(series, recorder, config, session_last=flags)

    # 3333 * 0.5 % = 16.665 / 1.25 = 13.332 -> 13.33 (Auto on a CFD is fractional).
    ((entry, _),) = list(recorder.entries().values())
    assert isinstance(entry, Entry)
    assert entry.qty == pytest.approx(13.33)


def test_two_instances_share_no_state() -> None:
    first, rec1 = run_rows(SHORT_ROWS)
    second, rec2 = run_rows(SHORT_ROWS)

    assert first.trades == second.trades
    assert rec1.inner is not rec2.inner
    assert rec1.states == rec2.states
