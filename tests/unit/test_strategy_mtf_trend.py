"""MTF Trend Continuation port (`propbot.strategies.mtf_trend`) against hand-executed Pine.

Every expected number below was derived by stepping `pine/mtf_trend_continuation_strategy.pine`
through the bar sequence by hand (comments show the arithmetic). Most tests shrink the
indicator lengths so the arithmetic stays checkable:

- `htf_tf = "10"` with 5-minute bars: two chart bars per higher-timeframe bar; with HTF EMA
  lengths 1 and 2 the trend on a chart bar is `c > (2/3) c + (1/3) slow[prev]` of the last
  CLOSED 10-minute bar, i.e. bull when that bar's close beats the previous slow EMA.
- `ltf_fast_len = ltf_slow_len = 1`: both 5-minute EMAs equal the close, so the band is the
  close itself and every bar's range intersects it (the value-area test has its own case).
- ATR trailing with length 1 and multiplier 1: the trail is `close - true range`.
- `risk_percent = 0.1` of 100 000 = 100 $ per trade; MES point value 5 -> qty = floor(20 / d).
"""

import datetime as dt
from collections.abc import Sequence
from dataclasses import dataclass, field
from typing import Any

import pytest
from pydantic import ValidationError

from propbot.backtest.engine import run_backtest
from propbot.backtest.model import CostModel, EngineResult, ExitReason, FillModel
from propbot.core.bars import Bar, BarSeries, DataSource
from propbot.signals.schema import STRATEGY_NAMES
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
from propbot.strategies.mtf_trend import PINE_FILE, STRATEGY_ID, MtfTrend, MtfTrendParams
from support.engine import FREE, MINUTE_MS, START_MS, make_config, mes
from support.pine import pine_defaults, pine_inputs

FIVE_MIN_MS = 5 * MINUTE_MS
BOTH_MODELS = pytest.mark.parametrize("fill_model", list(FillModel))

# Every Pine input that changes orders -> its Params field.
PORTED: dict[str, str] = {
    "tz": "tz",
    "htfTf": "htf_tf",
    "htfFastLen": "htf_fast_len",
    "htfSlowLen": "htf_slow_len",
    "ltfFastLen": "ltf_fast_len",
    "ltfSlowLen": "ltf_slow_len",
    "useVolFilter": "use_vol_filter",
    "volLen": "vol_len",
    "allowLong": "allow_long",
    "allowShort": "allow_short",
    "armValidityBars": "arm_validity_bars",
    "riskPercent": "risk_percent",
    "riskBase": "risk_base",
    "fixedBal": "fixed_bal",
    "swingLen": "swing_len",
    "swingBufTick": "swing_buf_tick",
    "roundUnits": "round_units",
    "tp1RR": "tp1_rr",
    "scaleOutPct": "scale_out_pct",
    "trailType": "trail_type",
    "chLen": "ch_len",
    "chMult": "ch_mult",
    "atrTrailLen": "atr_trail_len",
    "atrTrailMult": "atr_trail_mult",
    "trailMinStepTicks": "trail_min_step_ticks",
    "maxTradesDay": "max_trades_day",
    "useDailyLoss": "use_daily_loss",
    "dailyLossPct": "daily_loss_pct",
    "flatOnDailyLoss": "flat_on_daily_loss",
    "useConsec": "use_consec",
    "maxConsec": "max_consec",
    "useSession": "use_session",
    "sessStartH": "sess_start_h",
    "sessStartM": "sess_start_m",
    "sessEndH": "sess_end_h",
    "sessEndM": "sess_end_m",
    "flatAtEnd": "flat_at_end",
    "startDate": "start_date",
    "endDate": "end_date",
}
# Inputs that only drive visuals, the table, the OOS marker or the alert JSON.
NOT_PORTED: frozenset[str] = frozenset(
    {
        "oosStart",
        "showDateViz",
        "showVA",
        "showHtf",
        "showLevels",
        "showPerf",
        "tableScope",
        "tablePos",
        "tableSize",
        "alertToken",
    }
)

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

# Scenario A (scaled long, 2 contracts). HTF buckets: bars (0,1) (2,3) (4,5) ...
A_ROWS: list[tuple[float, float, float, float]] = [
    (5000, 5001, 4999, 5000),  # 0
    (5000, 5001, 4999, 5000),  # 1  HTF bar closes 5000: fast = slow = 5000 from bar 2
    (5000, 5003, 4999, 5002),  # 2
    (5002, 5005, 5001, 5004),  # 3  HTF bar closes 5004: bull from bar 4 (5004 > 5002.67)
    (5004, 5004.5, 5001, 5002),  # 4  armed, no trigger (close < open)
    (5002, 5007, 5001.5, 5006.75),  # 5  re-armed + trigger: 5006.75 > high[1] = 5004.5
    (5007, 5010, 5005, 5009),  # 6  fill bar: market entry at 5007
    (5009, 5018, 5008, 5016),  # 7  TP1 limit 5017.5 traded through (5017.75)
    (5016, 5022, 5015, 5021),  # 8  trail: 5021 - TR 7 = 5014
    (5018, 5018.25, 5017.5, 5018),  # 9  trail 5018 - TR 3.5 = 5014.5: +0.5 < 1.0, no move
    (5017, 5017.5, 5013, 5013.5),  # 10 runner stop 5014 hit
    (5013.5, 5014, 5012, 5013),  # 11
]
# Scenario B (Plan B): A with swingBufTick 0 (stop 5001, d 5.75 -> 3 contracts) and a bar 7
# that only TOUCHES the 1.5R level 5016.
B_ROWS = [*A_ROWS[:7], (5009, 5016, 5008, 5015), *A_ROWS[8:]]


def mirror(rows: Sequence[tuple[float, float, float, float]]) -> list[tuple[float, ...]]:
    """Reflect prices through 5000 (x -> 10000 - x): every long setup becomes its short twin."""
    return [(10000 - o, 10000 - lo, 10000 - h, 10000 - c) for o, h, lo, c in rows]


def series(
    rows: Sequence[Sequence[float]],
    *,
    start_ms: int = START_MS,
    volumes: Sequence[float] | None = None,
    tf_ms: int = FIVE_MIN_MS,
) -> BarSeries:
    bars = tuple(
        Bar(
            ts_open_ms=start_ms + index * tf_ms,
            open=row[0],
            high=row[1],
            low=row[2],
            close=row[3],
            volume=1.0 if volumes is None else volumes[index],
        )
        for index, row in enumerate(rows)
    )
    source = DataSource(name="test", format="generic", sha256="", synthetic=True, bar_tz="UTC")
    return BarSeries(symbol="MES", tf_seconds=tf_ms // 1000, bars=bars, source=source)


@dataclass(eq=False)
class Recorder:
    """Wraps the port and keeps every batch it returned."""

    inner: MtfTrend
    bar_actions: dict[int, list[Action]] = field(default_factory=dict)
    fill_actions: list[tuple[FillEvent, list[Action]]] = field(default_factory=list)
    init_stops: dict[int, float | None] = field(default_factory=dict)

    @property
    def strategy_id(self) -> str:
        return self.inner.strategy_id

    @property
    def pyramiding(self) -> int:
        return self.inner.pyramiding

    def on_bar(self, bar: Bar, ctx: StrategyContext) -> Sequence[Action]:
        actions = list(self.inner.on_bar(bar, ctx))
        self.bar_actions[ctx.bar_index] = actions
        self.init_stops[ctx.bar_index] = self.inner.active_init_stop
        return actions

    def on_fill(self, fill: FillEvent, ctx: StrategyContext) -> Sequence[Action]:
        actions = list(self.inner.on_fill(fill, ctx))
        self.fill_actions.append((fill, actions))
        return actions


def run_mtf(
    rows: Sequence[Sequence[float]],
    fill_model: FillModel = FillModel.PESSIMISTIC,
    *,
    start_ms: int = START_MS,
    volumes: Sequence[float] | None = None,
    session_last: Sequence[bool] | None = None,
    costs: CostModel = FREE,
    **overrides: Any,
) -> tuple[EngineResult, Recorder]:
    params = MtfTrendParams(**{**SMALL, **overrides})
    recorder = Recorder(MtfTrend(params))
    data = series(rows, start_ms=start_ms, volumes=volumes)
    flags = session_last or [index == len(rows) - 1 for index in range(len(rows))]
    result = run_backtest(
        data,
        recorder,
        make_config(instrument=mes(), costs=costs, fill_model=fill_model),
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


def summary(result: EngineResult) -> list[tuple[str, ExitReason, str, float, float, int]]:
    return [
        (t.exit_id, t.reason, t.comment, t.qty, t.exit_price, t.exit_bar_index)
        for t in result.trades
    ]


# -- module surface and defaults ------------------------------------------------------------


def test_module_surface_names_the_pine_script_and_a_known_strategy_id() -> None:
    assert STRATEGY_ID == "mtf_trend_cont"
    assert STRATEGY_ID in STRATEGY_NAMES
    assert PINE_FILE == "mtf_trend_continuation_strategy.pine"
    strategy = MtfTrend()
    assert strategy.strategy_id == STRATEGY_ID
    assert strategy.pyramiding == 0  # strategy(..., pyramiding = 0)


def test_every_pine_input_is_either_ported_or_deliberately_left_out() -> None:
    names = set(pine_inputs(PINE_FILE))

    assert set(PORTED) | NOT_PORTED == names
    assert not set(PORTED) & NOT_PORTED
    assert set(PORTED.values()) == set(MtfTrendParams.model_fields)


def test_every_ported_default_equals_the_pine_default() -> None:
    defaults = pine_defaults(PINE_FILE)
    params = MtfTrendParams()

    for pine_name, field_name in PORTED.items():
        value = getattr(params, field_name)
        assert value == defaults[pine_name], pine_name
        assert type(value) is type(defaults[pine_name]), pine_name


def test_params_enforce_the_pine_minval_maxval_and_options() -> None:
    for bad in (
        {"risk_percent": 0.001},
        {"scale_out_pct": 100.0},
        {"scale_out_pct": 0.5},
        {"sess_end_h": 24},
        {"sess_end_m": 60},
        {"swing_buf_tick": -1},
        {"arm_validity_bars": 0},
        {"trail_type": "Chandelier"},
        {"risk_base": "Equity"},
        {"htf_tf": "D"},
        {"tz": "Mars/Olympus"},
        {"start_date": dt.datetime(2024, 1, 1)},  # naive: refused
        {"unknown": 1},
    ):
        with pytest.raises(ValidationError):
            MtfTrendParams(**bad)


def test_dates_are_normalised_to_utc() -> None:
    params = MtfTrendParams(
        start_date=dt.datetime(2024, 1, 1, 5, tzinfo=dt.timezone(dt.timedelta(hours=5)))
    )

    assert params.start_date == dt.datetime(2024, 1, 1, tzinfo=dt.UTC)
    assert params.start_date.tzinfo is dt.UTC


def test_two_instances_never_share_state() -> None:
    first, second = MtfTrend(), MtfTrend()
    run_backtest(series(A_ROWS), first, make_config(), session_last=[False] * 11 + [True])

    assert second.total_entries == 0
    assert second.active_init_stop is None


def test_a_higher_timeframe_below_the_chart_timeframe_is_refused() -> None:
    with pytest.raises(ValueError, match="üst zaman dilimi"):
        run_mtf(A_ROWS, htf_tf="1")


# -- scaled mode ------------------------------------------------------------------------------


@BOTH_MODELS
def test_scaled_long_takes_tp1_then_trails_the_runner_from_breakeven(fill_model: FillModel) -> None:
    result, rec = run_mtf(A_ROWS, fill_model)

    # Signal bar 5: swingLow = lowest(low, 3) of bars 3..5 = 5001; buf = 4 ticks = 1.0
    # stopLvl = min(5001 - 1, 5006.75 - 0.25) = 5000; stopDist = 6.75
    # qty = floor(100 / (6.75 * 5)) = floor(2.96) = 2 (even -> scaled); estTP1 = 5006.75 + 10.125
    assert rec.bar_actions[5] == [
        Entry("Long", Side.LONG, 2.0, comment="Long"),
        Exit("TP1", "Long", stop=5000.0, limit=5016.875, qty_percent=50.0, comment="TP1"),
        Exit("Init", "Long", stop=5000.0, qty_percent=50.0, comment="Init stop"),
    ]
    assert list(entries(rec)) == [5]
    # Fill at 5007: activeInitStop = min(5000, 5006.75) = 5000; risk 7; TP1 = 5007 + 10.5
    final_bracket = [
        Exit("TP1", "Long", stop=5000.0, limit=5017.5, qty_percent=50.0, comment="TP1"),
        Exit("Init", "Long", stop=5000.0, qty_percent=50.0, comment="Init stop"),
    ]
    assert rec.bar_actions[6] == final_bracket
    if fill_model is FillModel.PESSIMISTIC:
        assert [actions for _, actions in rec.fill_actions] == [final_bracket]
    else:
        assert rec.fill_actions == []
    # Bar 7: 1 of 2 left < 2 * 0.95 -> TP1 filled; TR = 10, trail 5016 - 10 = 5006 -> BE 5007
    assert rec.bar_actions[7] == [
        Cancel("Init"),
        Exit("Run", "Long", stop=5007.0, comment="Runner"),
    ]
    assert rec.bar_actions[8] == [
        Cancel("Init"),
        Exit("Run", "Long", stop=5014.0, comment="Runner"),
    ]
    assert rec.bar_actions[9] == [
        Cancel("Init"),
        Exit("Run", "Long", stop=5014.0, comment="Runner"),
    ]
    # Bar 10: flat -> state reset, every exit id cancelled before any new entry
    assert rec.bar_actions[10] == [Cancel(i) for i in ("TP1", "Init", "Run", "RunB", "Stop")]
    assert summary(result) == [
        ("TP1", ExitReason.TARGET, "TP1", 1.0, 5017.5, 7),
        ("Run", ExitReason.STOP, "Runner", 1.0, 5014.0, 10),
    ]
    assert {t.entry_price for t in result.trades} == {5007.0}
    assert {t.entry_bar_index for t in result.trades} == {6}
    assert rec.init_stops[6] == 5000.0


@BOTH_MODELS
def test_scaled_short_is_the_exact_mirror(fill_model: FillModel) -> None:
    result, rec = run_mtf(mirror(A_ROWS), fill_model)

    # swingHigh = 4999 -> stopLvl = max(4999 + 1, 4993.25 + 0.25) = 5000; d = 6.75
    assert rec.bar_actions[5] == [
        Entry("Short", Side.SHORT, 2.0, comment="Short"),
        Exit("TP1", "Short", stop=5000.0, limit=4983.125, qty_percent=50.0, comment="TP1"),
        Exit("Init", "Short", stop=5000.0, qty_percent=50.0, comment="Init stop"),
    ]
    # fill 4993: activeInitStop = max(5000, 4993.25); TP1 = 4993 - 10.5 = 4982.5
    assert rec.bar_actions[6][0] == Exit(
        "TP1", "Short", stop=5000.0, limit=4982.5, qty_percent=50.0, comment="TP1"
    )
    # trail = min(BE 4993, close 4984 + TR 10) = 4993; then 4979 + 7 = 4986; then 4985.5 (+0.5)
    assert rec.bar_actions[7][1] == Exit("Run", "Short", stop=4993.0, comment="Runner")
    assert rec.bar_actions[8][1] == Exit("Run", "Short", stop=4986.0, comment="Runner")
    assert rec.bar_actions[9][1] == Exit("Run", "Short", stop=4986.0, comment="Runner")
    assert summary(result) == [
        ("TP1", ExitReason.TARGET, "TP1", 1.0, 4982.5, 7),
        ("Run", ExitReason.STOP, "Runner", 1.0, 4986.0, 10),
    ]


@BOTH_MODELS
def test_scaled_trade_stopped_before_tp1_closes_both_halves_at_the_initial_stop(
    fill_model: FillModel,
) -> None:
    rows = [*A_ROWS[:7], (5009, 5009.5, 4999, 5000), (5000, 5001, 4999.5, 5000)]
    result, rec = run_mtf(rows, fill_model)

    assert summary(result) == [
        ("TP1", ExitReason.STOP, "TP1", 1.0, 5000.0, 7),
        ("Init", ExitReason.STOP, "Init stop", 1.0, 5000.0, 7),
    ]
    assert rec.bar_actions[7] == [Cancel(i) for i in ("TP1", "Init", "Run", "RunB", "Stop")]


@BOTH_MODELS
def test_tp1_detection_uses_95_percent_of_the_entry_size(fill_model: FillModel) -> None:
    # risk 150 $ -> qty = floor(150 / 33.75) = 4 (even); scaleOutPct 25 -> TP1 takes 1 contract
    result, rec = run_mtf(A_ROWS, fill_model, risk_percent=0.15, scale_out_pct=25.0)

    assert rec.bar_actions[5][1:] == [
        Exit("TP1", "Long", stop=5000.0, limit=5016.875, qty_percent=25.0, comment="TP1"),
        Exit("Init", "Long", stop=5000.0, qty_percent=75.0, comment="Init stop"),
    ]
    # bar 7: 3 left < 4 * 0.95 = 3.8 -> runner phase for the remaining 3
    assert rec.bar_actions[7] == [
        Cancel("Init"),
        Exit("Run", "Long", stop=5007.0, comment="Runner"),
    ]
    assert summary(result) == [
        ("TP1", ExitReason.TARGET, "TP1", 1.0, 5017.5, 7),
        ("Run", ExitReason.STOP, "Runner", 3.0, 5014.0, 10),
    ]


# -- Plan B -----------------------------------------------------------------------------------


@BOTH_MODELS
def test_plan_b_odd_size_moves_the_whole_stop_once_tp1_is_touched(fill_model: FillModel) -> None:
    result, rec = run_mtf(B_ROWS, fill_model, swing_buf_tick=0)

    # stopLvl = min(5001 - 0, 5006.5) = 5001; d = 5.75; qty = floor(100 / 28.75) = 3 (odd)
    assert rec.bar_actions[5] == [
        Entry("Long", Side.LONG, 3.0, comment="Long"),
        Exit("Stop", "Long", stop=5001.0, comment="Init stop"),
    ]
    # fill 5007: init = min(5001, 5006.75) = 5001; risk 6; 1.5R = 5016 (no TP order)
    assert rec.bar_actions[6] == [Exit("Stop", "Long", stop=5001.0, comment="Init stop")]
    # bar 7 high 5016 >= 5016 (a touch is enough); TR = 8 -> trail 5007 = BE
    assert rec.bar_actions[7] == [
        Cancel("Stop"),
        Exit("RunB", "Long", stop=5007.0, comment="Runner (Plan B)"),
    ]
    assert rec.bar_actions[8][1] == Exit("RunB", "Long", stop=5014.0, comment="Runner (Plan B)")
    assert rec.bar_actions[9][1] == Exit("RunB", "Long", stop=5014.0, comment="Runner (Plan B)")
    assert summary(result) == [("RunB", ExitReason.STOP, "Runner (Plan B)", 3.0, 5014.0, 10)]
    assert result.trades[0].initial_stop == 5001.0


@BOTH_MODELS
def test_one_contract_is_plan_b_too(fill_model: FillModel) -> None:
    # risk 50 $ -> floor(50 / 33.75) = 1 contract
    result, rec = run_mtf(A_ROWS, fill_model, risk_percent=0.05)

    assert rec.bar_actions[5] == [
        Entry("Long", Side.LONG, 1.0, comment="Long"),
        Exit("Stop", "Long", stop=5000.0, comment="Init stop"),
    ]
    # 1.5R = 5017.5 reached by bar 7's high 5018 -> BE 5007, then 5014
    assert summary(result) == [("RunB", ExitReason.STOP, "Runner (Plan B)", 1.0, 5014.0, 10)]


@BOTH_MODELS
def test_plan_b_short_mirror(fill_model: FillModel) -> None:
    result, rec = run_mtf(mirror(B_ROWS), fill_model, swing_buf_tick=0)

    assert rec.bar_actions[5] == [
        Entry("Short", Side.SHORT, 3.0, comment="Short"),
        Exit("Stop", "Short", stop=4999.0, comment="Init stop"),
    ]
    # low 4984 <= 1.5R level 4984 (touch) -> BE 4993
    assert rec.bar_actions[7] == [
        Cancel("Stop"),
        Exit("RunB", "Short", stop=4993.0, comment="Runner (Plan B)"),
    ]
    assert summary(result) == [("RunB", ExitReason.STOP, "Runner (Plan B)", 3.0, 4986.0, 10)]


# -- trailing ---------------------------------------------------------------------------------


@BOTH_MODELS
def test_chandelier_trail_moves_only_in_steps_of_the_minimum(fill_model: FillModel) -> None:
    result, rec = run_mtf(A_ROWS, fill_model, trail_type="Chandelier Exit", ch_len=3, ch_mult=1.0)

    # True ranges of bars 0..9: 2, 2, 4, 4, 3.5, 5.5, 5, 10, 7, 3.5
    # ATR(3) = RMA: seed (2 + 2 + 4) / 3 on bar 2, then (tr + 2 * prev) / 3
    atr = 8 / 3
    for tr in (4, 3.5, 5.5, 5, 10):  # bars 3..7
        atr = (tr + 2 * atr) / 3
    atr7 = atr
    atr8 = (7 + 2 * atr7) / 3
    atr9 = (3.5 + 2 * atr8) / 3
    stop7 = 5018 - atr7  # highest(high, 3) of bars 5..7 = 5018; above BE 5007
    stop8 = 5022 - atr8  # +3.74 >= 1.0 -> moves
    assert 0 < (5022 - atr9) - stop8 < 1.0  # bar 9: +0.99 < 1.0 -> stays
    assert rec.bar_actions[7][1] == Exit("Run", "Long", stop=pytest.approx(stop7), comment="Runner")
    assert rec.bar_actions[8][1] == Exit("Run", "Long", stop=pytest.approx(stop8), comment="Runner")
    assert rec.bar_actions[9][1] == Exit("Run", "Long", stop=pytest.approx(stop8), comment="Runner")
    assert result.trades[1].exit_id == "Run"
    assert result.trades[1].exit_price == pytest.approx(stop8)


@BOTH_MODELS
def test_a_trail_above_the_close_fills_at_the_next_open(fill_model: FillModel) -> None:
    # Plan B (B rows), Chandelier length 3 x 0.1: on bar 7 the candidate is
    # highest 5016 - 0.1 * ATR(3) = 5015.44, above the close 5015 (wrong side of the market)
    rows = [*B_ROWS[:8], (5015, 5016, 5014, 5015.5), (5015.5, 5016, 5015, 5015.5)]
    result, rec = run_mtf(
        rows, fill_model, swing_buf_tick=0, trail_type="Chandelier Exit", ch_len=3, ch_mult=0.1
    )

    atr = 8 / 3
    for tr in (4, 3.5, 5.5, 5, 8):  # bars 3..7
        atr = (tr + 2 * atr) / 3
    stop7 = 5016 - 0.1 * atr
    assert stop7 > 5015
    assert rec.bar_actions[7][1] == Exit(
        "RunB", "Long", stop=pytest.approx(stop7), comment="Runner (Plan B)"
    )
    # Bar 8 opens at 5015, already through the stop: filled at the open
    assert summary(result) == [("RunB", ExitReason.STOP, "Runner (Plan B)", 3.0, 5015.0, 8)]


@BOTH_MODELS
def test_while_the_trail_is_warming_up_the_runner_goes_to_breakeven(fill_model: FillModel) -> None:
    # Chandelier length 30 has no value within 12 bars (Pine: math.max(be, na) = na)
    result, rec = run_mtf(A_ROWS, fill_model, trail_type="Chandelier Exit", ch_len=30)

    assert rec.bar_actions[7][1] == Exit("Run", "Long", stop=5007.0, comment="Runner")
    assert rec.bar_actions[8][1] == Exit("Run", "Long", stop=5007.0, comment="Runner")
    # never reached (lowest low after bar 7 is 5012): closed on the session's last bar
    assert summary(result)[-1] == ("", ExitReason.CLOSE, "Session end", 1.0, 5013.0, 11)


# -- fill-bar re-pricing in both fill models --------------------------------------------------


def test_both_fill_models_finalize_the_same_levels_for_the_same_fill() -> None:
    # 1 tick slippage: the fill is 5007.25 in both models
    costs = CostModel(commission_per_unit_side=0.0, slippage_ticks=1.0)
    pess, pess_rec = run_mtf(A_ROWS, FillModel.PESSIMISTIC, costs=costs)
    tv, tv_rec = run_mtf(A_ROWS, FillModel.TV_OHLC, costs=costs)

    # init = min(5000, 5007.0) = 5000; risk 7.25; TP1 = 5007.25 + 10.875 = 5018.125
    expected = [
        Exit("TP1", "Long", stop=5000.0, limit=5018.125, qty_percent=50.0, comment="TP1"),
        Exit("Init", "Long", stop=5000.0, qty_percent=50.0, comment="Init stop"),
    ]
    (fill, at_fill), *_ = pess_rec.fill_actions
    assert fill.price == 5007.25
    assert at_fill == expected  # at the fill instant
    assert tv_rec.bar_actions[6] == expected  # at the fill bar's close
    assert pess_rec.bar_actions[6] == expected
    assert pess_rec.init_stops[6] == tv_rec.init_stops[6] == 5000.0
    assert pess.trades[0].entry_price == tv.trades[0].entry_price == 5007.25


def test_only_the_timing_of_the_re_pricing_differs_between_the_fill_models() -> None:
    # The fill bar trades through the signal-time TP1 (5016.875 + 1 tick) but not the
    # re-priced one (5017.5 + 1 tick).
    rows = [*A_ROWS[:6], (5007, 5017.25, 5005, 5009), *A_ROWS[7:]]
    pess, _ = run_mtf(rows, FillModel.PESSIMISTIC)
    tv, tv_rec = run_mtf(rows, FillModel.TV_OHLC)

    # pessimistic re-prices at the fill: TP1 5017.5 fills on bar 7
    assert summary(pess)[0] == ("TP1", ExitReason.TARGET, "TP1", 1.0, 5017.5, 7)
    # tv_ohlc (the script's timing): the signal-time TP1 is live inside the fill bar
    assert summary(tv)[0] == ("TP1", ExitReason.TARGET, "TP1", 1.0, 5016.875, 6)
    # ... and at the fill bar's close the same levels are finalized, the runner goes to BE
    assert tv_rec.init_stops[6] == 5000.0
    assert tv_rec.bar_actions[6] == [
        Cancel("Init"),
        Exit("Run", "Long", stop=5007.0, comment="Runner"),
    ]


def test_the_initial_stop_is_pulled_one_tick_below_a_fill_at_the_structural_stop() -> None:
    # Bar 6 opens exactly at the structural stop 5000: init = min(5000, 5000 - 0.25)
    rows = [*A_ROWS[:6], (5000, 5003, 5000, 5002), *A_ROWS[7:]]
    result, rec = run_mtf(rows, FillModel.PESSIMISTIC)

    (fill, at_fill), *_ = rec.fill_actions
    assert fill.price == 5000.0
    # risk 0.25 -> TP1 = 5000 + 1.5 * 0.25 = 5000.375
    assert at_fill == [
        Exit("TP1", "Long", stop=4999.75, limit=5000.375, qty_percent=50.0, comment="TP1"),
        Exit("Init", "Long", stop=4999.75, qty_percent=50.0, comment="Init stop"),
    ]
    assert result.trades[0].initial_stop == 4999.75


def test_a_gap_above_the_structural_stop_keeps_it() -> None:
    rows = [*A_ROWS[:6], (5000.25, 5003, 5000.25, 5002), *A_ROWS[7:]]
    result, rec = run_mtf(rows, FillModel.PESSIMISTIC)

    # min(5000, 5000.25 - 0.25) = 5000: the structural level, exactly one tick below the fill
    assert rec.fill_actions[0][1][1] == Exit(
        "Init", "Long", stop=5000.0, qty_percent=50.0, comment="Init stop"
    )
    assert result.trades[0].initial_stop == 5000.0


# -- HTF trend, value area, volume, arming ----------------------------------------------------


@BOTH_MODELS
def test_the_hourly_trend_comes_from_the_last_closed_hour_only(fill_model: FillModel) -> None:
    # Default 60-minute HTF, EMA lengths 1 / 2. Hour buckets: bars 0-5 (09:30-09:55),
    # 6-17 (10:00-10:55), 18-29. Flat 5000 until bar 16; bar 17 closes the 10:00 hour at
    # 5004 and is itself a momentum candle. Only from bar 18 on is that hour closed:
    # fast 5004 > slow (2/3) 5004 + (1/3) 5000 -> bull.
    flat = (5000, 5000.25, 4999.75, 5000)
    rows = [
        *([flat] * 17),
        (5000, 5004.5, 4999.75, 5004),  # 17: trigger, but no trend yet (no lookahead)
        (5004, 5006, 5003.5, 5005.75),  # 18: armed + trigger (5005.75 > 5004.5)
        (5006, 5007, 5005.5, 5006.5),
        (5006.5, 5007, 5006, 5006.5),
    ]
    _, rec = run_mtf(rows, fill_model, htf_tf="60")

    # swingLow of bars 16..18 = 4999.75 -> stop 4998.75; d = 7 -> qty floor(100 / 35) = 2
    assert entries(rec) == {18: Entry("Long", Side.LONG, 2.0, comment="Long")}


@BOTH_MODELS
def test_the_bar_must_reach_into_the_value_area(fill_model: FillModel) -> None:
    # EMA(2) / EMA(3) of the closes 5000, 5000, 5002, 5004:
    #   e2 = 5003.111, e3 = 5002.5 after bar 3
    # bar 4 (close 5011): e2 = 5008.370, e3 = 5006.75 -> band top 5008.37 < low 5009: outside
    # bar 5 (close 5015.5): e2 = 5013.123, e3 = 5011.125 -> top 5013.12 < low 5013.5: outside
    rows = [
        *A_ROWS[:4],
        (5010, 5012, 5009, 5011),
        (5013.5, 5016, 5013.5, 5015.5),  # trigger: 5015.5 > high[1] = 5012
        (5015.5, 5016, 5015, 5015.5),
        (5015.5, 5016, 5015, 5015.5),
    ]
    _, outside = run_mtf(rows, fill_model, ltf_fast_len=2, ltf_slow_len=3)
    _, inside = run_mtf(rows, fill_model)  # lengths 1 / 1: the band is the close itself

    assert entries(outside) == {}
    # with the band at the close, bar 4 (5011 > high[1] = 5005) is armed and triggers
    assert list(entries(inside)) == [4]


@BOTH_MODELS
def test_the_value_area_band_of_scenario_a_is_reached(fill_model: FillModel) -> None:
    # bar 4: e2 = 5002.370, e3 = 5002.25 -> range 5001..5004.5 intersects: armed
    _, rec = run_mtf(A_ROWS, fill_model, ltf_fast_len=2, ltf_slow_len=3)

    assert list(entries(rec)) == [5]


@BOTH_MODELS
def test_the_volume_filter_needs_volume_below_its_average(fill_model: FillModel) -> None:
    # volLen 2: SMA = (v + v[1]) / 2, so v < SMA  <=>  v < v[1]
    flat_volume = [100.0] * 12
    drying = [100.0] * 5 + [90.0] + [100.0] * 6  # bar 5: 90 < 95 -> armed on the trigger bar
    _, blocked = run_mtf(A_ROWS, fill_model, use_vol_filter=True, vol_len=2, volumes=flat_volume)
    _, allowed = run_mtf(A_ROWS, fill_model, use_vol_filter=True, vol_len=2, volumes=drying)

    assert entries(blocked) == {}
    assert list(entries(allowed)) == [5]  # armed and triggered on the same bar


# Arming on bar 4 only (volume), no re-arm afterwards; HTF bull throughout.
AGE_ROWS = [
    *A_ROWS[:5],
    (5002, 5003.25, 5001.5, 5003),  # 5: no trigger (5003 < high[1] 5004.5); HTF bucket closes 5003
    (5003.5, 5004, 5002.5, 5003),  # 6: no trigger (close < open)
    (5003, 5007, 5002.75, 5006.75),  # 7: trigger (5006.75 > 5004), 3 bars after the arm
    (5007, 5008, 5006, 5007.5),
    (5007.5, 5008, 5007, 5007.5),
]
AGE_VOLUMES = [100.0, 90.0, 80.0, 70.0, 60.0, 200.0, 300.0, 400.0, 500.0, 600.0]


@BOTH_MODELS
def test_an_arm_older_than_the_validity_expires(fill_model: FillModel) -> None:
    common = {"use_vol_filter": True, "vol_len": 2, "volumes": AGE_VOLUMES}
    _, expired = run_mtf(AGE_ROWS, fill_model, arm_validity_bars=2, **common)
    _, valid = run_mtf(AGE_ROWS, fill_model, arm_validity_bars=3, **common)

    assert entries(expired) == {}  # 7 - 4 = 3 > 2
    # swingLow of bars 5..7 = 5001.5 -> stop 5000.5; d = 6.25 -> qty floor(3.2) = 3
    assert entries(valid) == {7: Entry("Long", Side.LONG, 3.0, comment="Long")}


@BOTH_MODELS
def test_a_trend_flip_disarms(fill_model: FillModel) -> None:
    rows = [
        *A_ROWS[:5],
        (5002, 5002.5, 5001, 5001.5),  # 5: HTF bar closes 5001.5 < slow 5002.67 -> bear on 6
        (5001.5, 5004, 5001.25, 5003.75),  # 6: long trigger (5003.75 > 5002.5), disarmed
        (5003.75, 5004, 5003, 5003.5),
        (5003.5, 5004, 5003, 5003.5),
    ]
    _, rec = run_mtf(
        rows,
        fill_model,
        use_vol_filter=True,
        vol_len=2,
        volumes=AGE_VOLUMES[:9],
        arm_validity_bars=10,
    )

    assert entries(rec) == {}


@BOTH_MODELS
def test_a_new_new_york_day_resets_the_arm(fill_model: FillModel) -> None:
    rows = [
        *A_ROWS[:5],  # bar 4 arms (volume 60 < 70)
        (5002, 5003.25, 5001.5, 5003),  # 5: armed again (50 < 60), no trigger
        (5002.5, 5007, 5002, 5006.75),  # 6: trigger; volume 200 does not re-arm
        (5007, 5010, 5005, 5009),
        (5009, 5010, 5008, 5009),
    ]
    volumes = [100.0, 90.0, 80.0, 70.0, 60.0, 50.0, 200.0, 300.0, 400.0]
    common: dict[str, Any] = {
        "use_vol_filter": True,
        "vol_len": 2,
        "use_session": False,
        "volumes": volumes,
    }
    hour_ms = 60 * MINUTE_MS
    # 23:30 New York: bar 6 opens at 00:00 of the next day
    across_midnight = START_MS + 14 * hour_ms
    # 23:00 is the same 10-minute bucket layout, bar 6 opens at 23:30: same day
    same_day = START_MS + 13 * hour_ms + 30 * MINUTE_MS
    _, reset = run_mtf(rows, fill_model, start_ms=across_midnight, **common)
    _, kept = run_mtf(rows, fill_model, start_ms=same_day, **common)

    assert entries(reset) == {}
    assert entries(kept) == {6: Entry("Long", Side.LONG, 2.0, comment="Long")}


@BOTH_MODELS
def test_shorts_and_longs_can_be_switched_off(fill_model: FillModel) -> None:
    _, no_longs = run_mtf(A_ROWS, fill_model, allow_long=False)
    _, no_shorts = run_mtf(mirror(A_ROWS), fill_model, allow_short=False)

    assert entries(no_longs) == {}
    assert entries(no_shorts) == {}


# -- sizing -----------------------------------------------------------------------------------


@BOTH_MODELS
def test_a_size_that_rounds_to_zero_skips_the_trade(fill_model: FillModel) -> None:
    # 10 $ / (6.75 * 5) = 0.30 -> 0 contracts
    result, rec = run_mtf(A_ROWS, fill_model, risk_percent=0.01)

    assert entries(rec) == {}
    assert result.trades == ()
    assert rec.inner.total_entries == 0


@BOTH_MODELS
def test_the_risk_base_options(fill_model: FillModel) -> None:
    # Fixed Balance 1 000 000 x 0.01 % = 100 $ -> 2 contracts; Account Equity 100 000 x 0.1 %
    _, fixed = run_mtf(
        A_ROWS, fill_model, risk_base="Fixed Balance", fixed_bal=1_000_000.0, risk_percent=0.01
    )
    _, equity = run_mtf(A_ROWS, fill_model, risk_base="Account Equity")

    assert entries(fixed)[5].qty == 2.0
    assert entries(equity)[5].qty == 2.0


@BOTH_MODELS
def test_fractional_units_are_floored_to_the_instrument_step(fill_model: FillModel) -> None:
    # roundUnits off: raw 2.96 contracts, floored to the whole-contract step of MES -> 2;
    # Plan B is decided only when roundUnits is on, so this is a scaled trade
    _, rec = run_mtf(A_ROWS, fill_model, round_units=False, risk_percent=0.05)

    # 50 / 33.75 = 1.48 -> 1 contract, but scaled (two exits) because roundUnits is off
    assert rec.bar_actions[5][0] == Entry("Long", Side.LONG, 1.0, comment="Long")
    assert [a.id for a in rec.bar_actions[5][1:] if isinstance(a, Exit)] == ["TP1", "Init"]


# -- breakers, session, date range ------------------------------------------------------------

# After A's signal on bar 5: a fill bar that closes 5 points down, a bar that hits the
# Plan B stop 5000, and a second long signal on bar 8.
BREAKER_ROWS = [
    *A_ROWS[:6],
    (5007, 5008, 5001.5, 5002),  # 6: fill 5007, close 5002
    (5002, 5006, 4999.5, 5005.5),  # 7: low 4999.5 hits the stop 5000
    (5005.5, 5010, 5005, 5009.5),  # 8: trigger (5009.5 > 5006); HTF bucket 5005.5 > 5005.39
    (5009.5, 5010, 5008, 5009),
    (5009, 5010, 5008, 5009),
]
# 200 $: qty = floor(200 / 33.75) = 5 (Plan B); daily limit 0.1 % of 100 000 = 100 $
BREAKER: dict[str, Any] = {"risk_percent": 0.2, "daily_loss_pct": 0.1, "max_trades_day": 2}


@BOTH_MODELS
def test_the_daily_loss_breaker_flattens_and_halts_the_day(fill_model: FillModel) -> None:
    result, rec = run_mtf(BREAKER_ROWS, fill_model, **BREAKER)

    # bar 6 close: equity 100 000 - 5 x 5 x 5 = 99 875; 125 >= 100 -> halt + flat
    assert rec.bar_actions[6] == [
        Exit("Stop", "Long", stop=5000.0, comment="Init stop"),
        CloseAll(comment="Daily loss halt"),
    ]
    assert summary(result) == [("", ExitReason.CLOSE, "Daily loss halt", 5.0, 5002.0, 6)]
    assert list(entries(rec)) == [5]  # the bar-8 signal is ignored


@BOTH_MODELS
def test_without_flat_on_daily_loss_the_trade_runs_but_no_new_entry(fill_model: FillModel) -> None:
    result, rec = run_mtf(BREAKER_ROWS, fill_model, flat_on_daily_loss=False, **BREAKER)

    assert summary(result) == [("Stop", ExitReason.STOP, "Init stop", 5.0, 5000.0, 7)]
    assert list(entries(rec)) == [5]


@BOTH_MODELS
def test_without_breakers_the_second_signal_is_taken(fill_model: FillModel) -> None:
    _, rec = run_mtf(BREAKER_ROWS, fill_model, use_daily_loss=False, **BREAKER)

    # bar 8: swingLow 4999.5 -> stop 4998.5; d = 11 -> floor(200 / 55) = 3
    assert entries(rec) == {
        5: Entry("Long", Side.LONG, 5.0, comment="Long"),
        8: Entry("Long", Side.LONG, 3.0, comment="Long"),
    }


@BOTH_MODELS
def test_the_loss_streak_halt(fill_model: FillModel) -> None:
    _, rec = run_mtf(
        BREAKER_ROWS, fill_model, use_daily_loss=False, use_consec=True, max_consec=1, **BREAKER
    )

    assert list(entries(rec)) == [5]  # one loss -> halted for the day


@BOTH_MODELS
def test_max_trades_per_day(fill_model: FillModel) -> None:
    _, rec = run_mtf(BREAKER_ROWS, fill_model, use_daily_loss=False, risk_percent=0.2)

    assert list(entries(rec)) == [5]  # default maxTradesDay = 1


@BOTH_MODELS
def test_open_trades_are_closed_at_the_session_end_time(fill_model: FillModel) -> None:
    # Start 15:20 New York: signal 15:45, fill 15:50, bar 7 opens at 15:55 = sessEnd
    result, rec = run_mtf(A_ROWS, fill_model, start_ms=START_MS + 350 * MINUTE_MS)

    assert rec.bar_actions[7] == [
        Cancel("Init"),
        Exit("Run", "Long", stop=5007.0, comment="Runner"),
        CloseAll(comment="Session end"),
    ]
    assert summary(result) == [
        ("TP1", ExitReason.TARGET, "TP1", 1.0, 5017.5, 7),
        ("", ExitReason.CLOSE, "Session end", 1.0, 5016.0, 7),
    ]
    assert list(entries(rec)) == [5]


@BOTH_MODELS
def test_no_entry_whose_fill_bar_would_open_at_the_session_end(fill_model: FillModel) -> None:
    # Start 15:20, session end 15:50: the trigger bar opens 15:45 -> 945 + 5 = 950 is not
    # < 950; bars 6 and 7 (also triggers) are outside the window
    _, rec = run_mtf(A_ROWS, fill_model, start_ms=START_MS + 350 * MINUTE_MS, sess_end_m=50)
    _, control = run_mtf(A_ROWS, fill_model, start_ms=START_MS + 350 * MINUTE_MS, sess_end_m=51)

    assert entries(rec) == {}
    assert list(entries(control)) == [5]  # 945 + 5 = 950 < 951


@BOTH_MODELS
def test_the_session_last_bar_closes_and_blocks_entries(fill_model: FillModel) -> None:
    last_on_7 = [index in (7, 11) for index in range(12)]
    result, rec = run_mtf(A_ROWS, fill_model, session_last=last_on_7)
    last_on_5 = [index in (5, 11) for index in range(12)]
    _, blocked = run_mtf(A_ROWS, fill_model, session_last=last_on_5)

    assert rec.bar_actions[7][-1] == CloseAll(comment="Session end")
    assert summary(result)[-1] == ("", ExitReason.CLOSE, "Session end", 1.0, 5016.0, 7)
    assert 5 not in entries(blocked)  # the fill would land in the next session


@BOTH_MODELS
def test_outside_the_date_range(fill_model: FillModel) -> None:
    bar_ms = [START_MS + index * FIVE_MIN_MS for index in range(12)]
    end_on_7 = dt.datetime.fromtimestamp(bar_ms[7] / 1000, tz=dt.UTC)
    start_on_6 = dt.datetime.fromtimestamp(bar_ms[6] / 1000, tz=dt.UTC)
    result, rec = run_mtf(A_ROWS, fill_model, end_date=end_on_7)
    _, late = run_mtf(A_ROWS, fill_model, start_date=start_on_6)

    # bar 7: time == endDate is still in range; bar 8: time > endDate -> close
    assert rec.bar_actions[8][-1] == CloseAll(comment="End of test range")
    assert summary(result)[-1] == ("", ExitReason.CLOSE, "End of test range", 1.0, 5021.0, 8)
    # the bar-5 signal is before the start date; bar 6 (time == startDate) still triggers
    assert list(entries(late)) == [6]
