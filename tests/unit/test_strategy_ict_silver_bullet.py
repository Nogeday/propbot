"""ICT Silver Bullet port against `pine/ict_silver_bullet_strategy.pine`.

Every expected number below comes from executing the Pine script by hand on the bar
sequence (pivots, FVGs, the phase machine, f_qty and f_target), not from the port. The
base scenario (1-minute bars, Monday 2026-03-02, New York, pivotLen = 1, MES: tick 0.25,
point value 5, risk 0.5 % of 100 000 = 500 per trade):

    bar  1  09:54  pivot low 4985 (confirmed at bar 2)
    bar  4  09:57  pivot high 5010 (confirmed at bar 5)
    bar  5  09:58  pivot low 5000 (confirmed at bar 6)
    bar  7  10:00  window opens; high 5011 > lastPH 5010: SWEEP, short, mssLevel 5000
    bar  8  10:01  close 4997 < 5000: MSS
    bar  9  10:02  bear FVG [4998, 5002]: ARM short limit CE 5000, stop 5011.25,
                   risk 11.25, qty floor(500 / 56.25) = 8, TP 5000 - 2 x 11.25 = 4977.5
    bar 10  10:03  high 5001 trades one tick through 5000: FILL at 5000
    bar 11  10:04  low 4977 trades through 4977.25: TARGET at 4977.5, +900
"""

import datetime as dt
from collections.abc import Sequence
from dataclasses import dataclass, field
from typing import Any, get_args

import pytest
from pydantic import ValidationError

from propbot.backtest.engine import run_backtest
from propbot.backtest.model import EngineConfig, EngineResult, ExitReason, FillModel
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
from propbot.strategies.ict_silver_bullet import (
    LIQUIDITY_POOL_SIZE,
    PINE_FILE,
    PYRAMIDING,
    STRATEGY_ID,
    IctSilverBullet,
    IctSilverBulletParams,
)
from support.engine import MINUTE_MS, OHLC, START_MS, cfd, make_config
from support.pine import pine_defaults, pine_inputs

P = FillModel.PESSIMISTIC
TV = FillModel.TV_OHLC
BOTH = pytest.mark.parametrize("model", [P, TV])
DAY_MS = 24 * 60 * MINUTE_MS
# 09:53 New York: bar 7 opens at 10:00, the first bar of the AM window.
BASE_START = START_MS + 23 * MINUTE_MS

SWEEP, MSS, ARM, FILL, TP_BAR = 7, 8, 9, 10, 11

BASE: list[OHLC] = [
    (4990, 4991, 4988, 4989),  # 0 09:53
    (4989, 4990, 4985, 4986),  # 1 09:54  pivot low 4985
    (4986, 4995, 4986, 4994),  # 2 09:55
    (5005, 5006, 5004, 5005),  # 3 09:56  (bull gap over bar 1: before the window)
    (5005, 5010, 5004, 5006),  # 4 09:57  pivot high 5010
    (5006, 5007, 5000, 5002),  # 5 09:58  pivot low 5000
    (5002, 5004, 5001, 5003),  # 6 09:59
    (5003, 5011, 5002, 5004),  # 7 10:00  sweep
    (5004, 5005, 4996, 4997),  # 8 10:01  MSS
    (4997, 4998, 4990, 4992),  # 9 10:02  bear FVG, arm
    (4992, 5001, 4991, 4999),  # 10 10:03  fill
    (4999, 4999.5, 4977, 4978),  # 11 10:04  target
    (4978, 4979, 4977, 4978),  # 12 10:05
]
NO_FILL_10 = (4992, 4995, 4990, 4993)  # stays below 5000.25
NO_FILL_11 = (4993, 4996, 4991, 4994)
HOLD_11 = (4999, 5000, 4995, 4996)  # neither stop nor target: the position stays open
STOP_11 = (4999, 5012, 4998, 5010)  # stop 5011.25 hit: -11.25 x 8 x 5 = -450

# Pine inputs that change orders -> the Params field that carries them.
PORTED: dict[str, str] = {
    "tz": "tz",
    "enableAM": "enable_am",
    "amStartH": "am_start_h",
    "amStartM": "am_start_m",
    "amEndH": "am_end_h",
    "amEndM": "am_end_m",
    "enablePM": "enable_pm",
    "pmStartH": "pm_start_h",
    "pmStartM": "pm_start_m",
    "pmEndH": "pm_end_h",
    "pmEndM": "pm_end_m",
    "pivotLen": "pivot_len",
    "maxBarsSweepMSS": "max_bars_sweep_mss",
    "maxBarsMSSFvg": "max_bars_mss_fvg",
    "allowLong": "allow_long",
    "allowShort": "allow_short",
    "minFvgTicks": "min_fvg_ticks",
    "entryFill": "entry_fill",
    "maxBarsToFill": "max_bars_to_fill",
    "riskPct": "risk_pct",
    "riskBase": "risk_base",
    "fixedBal": "fixed_bal",
    "tickOffset": "tick_offset",
    "roundToInt": "round_to_int",
    "tpMode": "tp_mode",
    "rr": "rr",
    "minRRliq": "min_rr_liq",
    "liqBuf": "liq_buf",
    "useDailyDD": "use_daily_dd",
    "maxDailyDDPct": "max_daily_dd_pct",
    "flattenBreach": "flatten_breach",
    "maxConsec": "max_consec",
    "resetConsecDay": "reset_consec_day",
    "maxTrWindow": "max_tr_window",
    "maxTrDay": "max_tr_day",
    "useOverallDD": "use_overall_dd",
    "maxOverallPct": "max_overall_pct",
    "closeAtDayEnd": "close_at_day_end",
    "dayEndH": "day_end_h",
    "dayEndM": "day_end_m",
    "startDate": "start_date",
    "endDate": "end_date",
    "tradeMon": "trade_mon",
    "tradeTue": "trade_tue",
    "tradeWed": "trade_wed",
    "tradeThu": "trade_thu",
    "tradeFri": "trade_fri",
}
# Inputs that only drive visuals, the dashboard or the alert JSON.
NOT_PORTED: frozenset[str] = frozenset(
    {
        "oosStart",
        "showDateViz",
        "showDash",
        "tableScope",
        "dashPos",
        "dashSize",
        "showVis",
        "boxProj",
        "alertToken",
    }
)

_SNAPSHOT_FIELDS = (
    "phase",
    "dir",
    "mss_done",
    "sweep_bar",
    "mss_bar",
    "sweep_extreme",
    "mss_level",
    "bull_fvg_top",
    "bull_fvg_bot",
    "bull_fvg_bar",
    "bear_fvg_top",
    "bear_fvg_bot",
    "bear_fvg_bar",
    "last_ph",
    "last_pl",
    "trades_today",
    "trades_window",
    "consec_losses",
    "halted_today",
    "halted_perma",
)


@dataclass(eq=False)
class Recorder:
    """Runs the port and records what it returned and its state after every bar."""

    inner: IctSilverBullet
    actions: dict[int, list[Action]] = field(default_factory=dict)
    fill_actions: list[tuple[FillEvent, list[Action]]] = field(default_factory=list)
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
        self.states.append({name: getattr(self.inner, name) for name in _SNAPSHOT_FIELDS})
        return out

    def on_fill(self, fill: FillEvent, ctx: StrategyContext) -> Sequence[Action]:
        out = list(self.inner.on_fill(fill, ctx))
        self.fill_actions.append((fill, out))
        return out


def series_of(
    rows: Sequence[OHLC], *, start_ms: int = BASE_START, times: Sequence[int] | None = None
) -> BarSeries:
    """1-minute bars from `start_ms`, or at the explicit open times `times`."""
    stamps = times if times is not None else [start_ms + i * MINUTE_MS for i in range(len(rows))]
    bars = tuple(
        Bar(ts_open_ms=t, open=o, high=h, low=lo, close=c, volume=1.0)
        for t, (o, h, lo, c) in zip(stamps, rows, strict=True)
    )
    source = DataSource(name="test", format="generic", sha256="", synthetic=True, bar_tz="UTC")
    return BarSeries(symbol="TEST", tf_seconds=60, bars=bars, source=source)


def go(
    rows: Sequence[OHLC] | BarSeries,
    model: FillModel = P,
    *,
    session_last: Sequence[bool] | None = None,
    config: EngineConfig | None = None,
    **params: Any,
) -> tuple[EngineResult, Recorder]:
    params.setdefault("pivot_len", 1)
    series = rows if isinstance(rows, BarSeries) else series_of(rows)
    if session_last is None:
        session_last = [i == len(series) - 1 for i in range(len(series))]
    recorder = Recorder(IctSilverBullet(IctSilverBulletParams(**params)))
    result = run_backtest(
        series, recorder, config or make_config(fill_model=model), session_last=session_last
    )
    return result, recorder


def mirror(rows: Sequence[OHLC], axis: float = 10_000.0) -> list[OHLC]:
    """The same bars upside down: a short setup becomes the matching long setup."""
    return [(axis - o, axis - lo, axis - h, axis - c) for o, h, lo, c in rows]


def with_rows(base: Sequence[OHLC], replacements: dict[int, OHLC]) -> list[OHLC]:
    out = list(base)
    for index, row in replacements.items():
        if index == len(out):
            out.append(row)
        else:
            out[index] = row
    return out


CANCEL_ALL = [Cancel("Long"), Cancel("Short"), Cancel("Exit L"), Cancel("Exit S")]
SHORT_BRACKET = [
    Entry("Short", Side.SHORT, 8.0, limit=5000.0),
    Exit("Exit S", "Short", stop=5011.25, limit=4977.5),
]


# -- module surface and defaults --------------------------------------------------------


def test_module_surface_names_the_pine_script_and_a_known_strategy_id() -> None:
    strategy = IctSilverBullet()
    assert STRATEGY_ID in STRATEGY_NAMES
    assert strategy.strategy_id == STRATEGY_ID
    assert PINE_FILE == "ict_silver_bullet_strategy.pine"
    assert strategy.pyramiding == PYRAMIDING == 0  # strategy(pyramiding = 0)


def test_every_pine_input_is_either_ported_or_deliberately_left_out() -> None:
    names = set(pine_inputs(PINE_FILE))
    assert set(PORTED) | NOT_PORTED == names
    assert not set(PORTED) & NOT_PORTED
    assert set(PORTED.values()) == set(IctSilverBulletParams.model_fields)


def test_param_defaults_equal_the_pine_defaults() -> None:
    defaults = pine_defaults(PINE_FILE)
    params = IctSilverBulletParams()
    for pine_name, field_name in PORTED.items():
        value = getattr(params, field_name)
        assert value == defaults[pine_name], pine_name
        assert type(value) is type(defaults[pine_name]), pine_name


def test_option_literals_are_the_pine_option_strings_verbatim() -> None:
    fields = IctSilverBulletParams.model_fields
    entry_fill = fields["entry_fill"].annotation
    risk_base = fields["risk_base"].annotation
    tp_mode = fields["tp_mode"].annotation
    assert entry_fill is not None
    assert risk_base is not None
    assert tp_mode is not None
    assert get_args(entry_fill.__value__) == (
        "Proximal (first touch)",
        "50% (Consequent Encroachment)",
        "Distal (full gap fill)",
    )
    assert get_args(risk_base.__value__) == ("Initial Capital", "Account Equity", "Fixed Balance")
    assert get_args(tp_mode.__value__) == ("Fixed R:R", "Next opposing liquidity")


@pytest.mark.parametrize(
    ("name", "value"),
    [
        ("pivot_len", 0),
        ("am_start_h", 24),
        ("am_end_m", 60),
        ("risk_pct", 0.0),
        ("rr", 0.05),
        ("min_rr_liq", 0.0),
        ("max_tr_day", 0),
        ("max_daily_dd_pct", 0.05),
        ("tick_offset", -1),
        ("entry_fill", "Middle"),
        ("tz", "Mars/Olympus"),
        ("start_date", dt.datetime(2024, 1, 1)),  # naive: refused
    ],
)
def test_params_refuse_values_outside_the_pine_constraints(name: str, value: object) -> None:
    with pytest.raises(ValidationError):
        IctSilverBulletParams(**{name: value})


def test_date_params_are_stored_in_utc() -> None:
    plus3 = dt.timezone(dt.timedelta(hours=3))
    params = IctSilverBulletParams(end_date=dt.datetime(2030, 1, 1, 3, 0, tzinfo=plus3))
    assert params.end_date == dt.datetime(2030, 1, 1, tzinfo=dt.UTC)
    assert params.end_date.tzinfo is dt.UTC


def test_two_instances_share_no_state() -> None:
    a, b = IctSilverBullet(), IctSilverBullet()
    assert a.ph_arr is not b.ph_arr
    assert a.pl_arr is not b.pl_arr


# -- base scenario ------------------------------------------------------------------------


@BOTH
def test_short_setup_sweep_mss_fvg_arms_a_limit_with_bracket_and_hits_the_target(
    model: FillModel,
) -> None:
    result, rec = go(BASE, model)
    assert rec.actions[ARM] == SHORT_BRACKET
    (trade,) = result.trades
    assert trade.side is Side.SHORT
    assert trade.entry_id == "Short"
    assert trade.exit_id == "Exit S"
    assert trade.qty == 8.0
    assert (trade.entry_bar_index, trade.entry_price) == (FILL, 5000.0)
    assert trade.initial_stop == 5011.25
    assert (trade.exit_bar_index, trade.exit_price) == (TP_BAR, 4977.5)
    assert trade.reason is ExitReason.TARGET
    assert trade.pnl == pytest.approx(900.0)


@BOTH
def test_long_setup_is_the_mirror_of_the_short_one(model: FillModel) -> None:
    result, rec = go(mirror(BASE), model)
    assert rec.actions[ARM] == [
        Entry("Long", Side.LONG, 8.0, limit=5000.0),
        Exit("Exit L", "Long", stop=4988.75, limit=5022.5),
    ]
    assert rec.states[SWEEP]["dir"] == 1
    (trade,) = result.trades
    assert (trade.side, trade.entry_price, trade.initial_stop) == (Side.LONG, 5000.0, 4988.75)
    assert (trade.exit_price, trade.reason) == (5022.5, ExitReason.TARGET)
    assert trade.pnl == pytest.approx(900.0)


def test_phase_machine_runs_idle_swept_armed_in_trade_idle() -> None:
    _, rec = go(BASE)
    phases = [state["phase"] for state in rec.states]
    assert phases == [0, 0, 0, 0, 0, 0, 0, 1, 1, 2, 3, 0, 0]
    sweep = rec.states[SWEEP]
    assert (sweep["dir"], sweep["sweep_bar"], sweep["sweep_extreme"], sweep["mss_level"]) == (
        -1,
        SWEEP,
        5011.0,
        5000.0,
    )
    assert (rec.states[SWEEP]["mss_done"], rec.states[MSS]["mss_done"]) == (False, True)
    assert rec.states[MSS]["mss_bar"] == MSS
    assert rec.states[FILL]["trades_window"] == rec.states[FILL]["trades_today"] == 1


def test_pivots_confirm_after_pivot_len_bars_and_feed_last_ph_last_pl() -> None:
    _, rec = go(BASE)
    last_pl = [state["last_pl"] for state in rec.states]
    last_ph = [state["last_ph"] for state in rec.states]
    # pivot low 4985 at bar 1 -> known at bar 2; pivot low 5000 at bar 5 -> at bar 6;
    # 4990 at bar 9 -> at bar 10.
    assert last_pl[:7] == [None, None, 4985, 4985, 4985, 4985, 5000]
    assert last_pl[10] == 4990
    # pivot high 5010 at bar 4 -> at bar 5; 5011 (the sweep bar) -> at bar 8.
    assert last_ph[:9] == [None] * 5 + [5010, 5010, 5010, 5011]


def test_pivot_len_two_confirms_two_bars_later() -> None:
    _, rec = go(ONE_BAR_SETUP, pivot_len=2)
    last_ph = [state["last_ph"] for state in rec.states]
    last_pl = [state["last_pl"] for state in rec.states]
    # pivot high 5010 at bar 2 -> known at bar 4; pivot low 5000 at bar 4 -> at bar 6.
    assert last_ph[:5] == [None, None, None, None, 5010]
    assert last_pl[:7] == [None] * 6 + [5000]


def test_liquidity_pools_keep_the_latest_25_pivots_fifo() -> None:
    # Outside the windows (06:00): a zigzag with a peak on every even bar and a trough on
    # every odd bar; with pivotLen 1 each is a pivot, confirmed one bar later.
    rows: list[OHLC] = []
    for i in range(64):
        if i % 2 == 0:
            high = 5000.0 + i
            rows.append((high - 1, high, high - 2, high - 1))
        else:
            low = 4900.0 - i
            rows.append((low + 1, low + 2, low, low + 1))
    strategy = IctSilverBullet(IctSilverBulletParams(pivot_len=1))
    series = series_of(rows, start_ms=START_MS - 210 * MINUTE_MS)
    run_backtest(series, strategy, make_config(), session_last=[False] * 63 + [True])
    peaks = [5000.0 + i for i in range(2, 63, 2)]  # bar 0 and bar 63 are not confirmed
    troughs = [4900.0 - i for i in range(1, 63, 2)]
    assert list(strategy.ph_arr) == peaks[-LIQUIDITY_POOL_SIZE:]
    assert list(strategy.pl_arr) == troughs[-LIQUIDITY_POOL_SIZE:]
    assert strategy.last_ph == peaks[-1]
    assert strategy.last_pl == troughs[-1]


# -- FVG tracking ---------------------------------------------------------------------------


def test_fvgs_are_tracked_only_on_in_window_bars() -> None:
    _, rec = go(BASE)
    # Bar 3 (09:56) gaps over bar 1 (low 5004 > high 4990) but opens before the window.
    assert all(state["bull_fvg_bar"] is None for state in rec.states)
    assert rec.states[ARM]["bear_fvg_bar"] == ARM
    assert (rec.states[ARM]["bear_fvg_top"], rec.states[ARM]["bear_fvg_bot"]) == (5002, 4998)
    # The same bars with a window opening at 09:55 do track it.
    _, early = go(BASE, am_start_h=9, am_start_m=55)
    state = early.states[3]
    assert (state["bull_fvg_bar"], state["bull_fvg_top"], state["bull_fvg_bot"]) == (3, 5004, 4990)


def test_only_the_latest_gap_per_direction_is_kept() -> None:
    _, rec = go(BASE)
    # Bar 12 high 4979 < low[2] = bar 10 low 4991: a second bear gap replaces the first.
    state = rec.states[12]
    assert (state["bear_fvg_bar"], state["bear_fvg_top"], state["bear_fvg_bot"]) == (
        12,
        4991,
        4979,
    )


def test_min_fvg_ticks_filters_small_gaps() -> None:
    # The arming gap is 4 points = 16 ticks.
    _, small = go(BASE, min_fvg_ticks=16)
    _, big = go(BASE, min_fvg_ticks=17)
    assert small.states[ARM]["bear_fvg_bar"] == ARM
    assert big.states[ARM]["bear_fvg_bar"] is None
    assert not any(isinstance(a, Entry) for acts in big.actions.values() for a in acts)


def test_a_new_window_invalidates_the_gaps_of_the_previous_window() -> None:
    # AM 10:00-10:05 and PM 10:05-10:10: bar 12 (10:05) opens a new window.
    rows = with_rows(BASE, {12: (4978, 4992, 4977, 4990), 13: (4990, 4991, 4989, 4990)})
    _, rec = go(
        rows, am_end_h=10, am_end_m=5, pm_start_h=10, pm_start_m=5, pm_end_h=10, pm_end_m=10
    )
    assert rec.states[11]["bear_fvg_bar"] == ARM
    state = rec.states[12]
    assert state["bear_fvg_bar"] is None
    assert state["bear_fvg_top"] == 5002  # Pine resets only the bar index
    assert state["trades_window"] == 0
    assert state["trades_today"] == 1


# -- phase machine resets, gates, counters ---------------------------------------------


def test_a_new_day_resets_the_hunt_when_flat() -> None:
    day1 = BASE[: MSS + 1]  # ends in phase 1 (swept, MSS done, no FVG yet)
    day2: list[OHLC] = [(4997, 4998, 4996, 4997), (4997, 4998, 4996, 4997)]
    times = [BASE_START + i * MINUTE_MS for i in range(len(day1))]
    times += [BASE_START + DAY_MS + i * MINUTE_MS for i in range(len(day2))]
    last = [False] * len(times)
    last[len(day1) - 1] = last[-1] = True
    _, rec = go(series_of(day1 + day2, times=times), session_last=last)
    assert rec.states[MSS]["phase"] == 1
    assert rec.states[MSS + 1]["phase"] == 0
    assert rec.states[MSS + 1]["dir"] == 0
    assert rec.states[MSS + 1]["mss_done"] is False


def test_a_new_window_resets_an_unfinished_hunt() -> None:
    no_mss = (5004, 5006, 5003, 5005)
    rows = BASE[:8] + [no_mss] * 4 + [(5005, 5006, 5004, 5005), (5005, 5006, 5004, 5005)]
    rows[7] = BASE[7]
    _, rec = go(
        rows, am_end_h=10, am_end_m=5, pm_start_h=10, pm_start_m=5, pm_end_h=10, pm_end_m=10
    )
    assert [s["phase"] for s in rec.states[7:13]] == [1, 1, 1, 1, 1, 0]


def test_short_is_tried_first_when_a_bar_sweeps_both_pivots() -> None:
    rows = with_rows(BASE, {SWEEP: (5003, 5011, 4999, 5004)})  # high > 5010 and low < 5000
    _, rec = go(rows)
    assert rec.states[SWEEP]["dir"] == -1
    _, longs_only = go(rows, allow_short=False)
    assert longs_only.states[SWEEP]["dir"] == 1
    assert longs_only.states[SWEEP]["sweep_extreme"] == 4999
    assert longs_only.states[SWEEP]["mss_level"] == 5010


@pytest.mark.parametrize(
    ("params", "session_last_at"),
    [
        ({"am_end_h": 10, "am_end_m": 1}, None),  # 600 + 1 < 601 is false: next bar is outside
        ({}, SWEEP),  # the session's last bar
        ({"trade_mon": False}, None),  # Monday filtered out
        ({"start_date": dt.datetime(2026, 3, 3, tzinfo=dt.UTC)}, None),
        ({"enable_am": False}, None),
    ],
)
def test_no_hunt_starts_when_a_gate_is_closed(
    params: dict[str, Any], session_last_at: int | None
) -> None:
    last = None
    if session_last_at is not None:
        last = [i in (session_last_at, len(BASE) - 1) for i in range(len(BASE))]
    result, rec = go(BASE, session_last=last, **params)
    assert rec.states[SWEEP]["phase"] == 0
    assert result.trades == ()


def test_no_arming_when_the_next_bar_falls_outside_the_window() -> None:
    # Window 10:00-10:03: the FVG bar (10:02) passes 602 + 1 < 603? No -> no order.
    result, rec = go(BASE, am_end_h=10, am_end_m=3)
    assert rec.states[ARM]["phase"] == 1
    assert not any(isinstance(a, Entry) for acts in rec.actions.values() for a in acts)
    assert result.trades == ()


def _two_window_rows(sweep_again: OHLC) -> list[OHLC]:
    return with_rows(BASE, {12: sweep_again, 13: (5029, 5030, 5028, 5029)})


@pytest.mark.parametrize(
    ("max_tr_window", "max_tr_day", "expected_phase"),
    [(1, 1, 0), (1, 2, 1), (2, 1, 0)],
)
def test_trade_counters_per_window_and_per_day_gate_the_next_hunt_across_windows(
    max_tr_window: int, max_tr_day: int, expected_phase: int
) -> None:
    # After the AM trade a PM bar (10:05) sweeps lastPH 5001 (bar 10's high, known at bar 11).
    rows = _two_window_rows((4978, 5030, 4977, 5029))
    _, rec = go(
        rows,
        am_end_h=10,
        am_end_m=5,
        pm_start_h=10,
        pm_start_m=5,
        pm_end_h=10,
        pm_end_m=15,
        max_tr_window=max_tr_window,
        max_tr_day=max_tr_day,
    )
    assert rec.states[11]["last_ph"] == 5001
    assert rec.states[12]["phase"] == expected_phase


@pytest.mark.parametrize(
    ("max_tr_window", "expected_phase"),
    [(1, 0), (2, 1)],
)
def test_trade_counter_per_window_gates_a_second_hunt_in_the_same_window(
    max_tr_window: int, expected_phase: int
) -> None:
    rows = _two_window_rows((4978, 5030, 4977, 5029))
    _, rec = go(rows, max_tr_window=max_tr_window, max_tr_day=2)
    assert rec.states[12]["phase"] == expected_phase


# -- sweep, MSS, FVG ----------------------------------------------------------------------


def test_mss_by_close_on_the_sweep_bar_and_sweep_extreme_tracks_every_phase_one_bar() -> None:
    rows = with_rows(
        BASE,
        {
            SWEEP: (5003, 5011, 4999.5, 4999.75),  # sweeps 5010 and closes below 5000
            MSS: (4999.75, 5012, 4996, 4997),  # a further poke to 5012
        },
    )
    _, rec = go(rows)
    assert rec.states[SWEEP]["mss_done"] is True
    assert rec.states[SWEEP]["mss_bar"] == SWEEP
    assert rec.states[MSS]["sweep_extreme"] == 5012
    # bear FVG [4998, 4999.5] -> CE 4998.75; stop 5012.25; risk 13.5 -> floor(500 / 67.5) = 7
    assert rec.actions[ARM] == [
        Entry("Short", Side.SHORT, 7.0, limit=4998.75),
        Exit("Exit S", "Short", stop=5012.25, limit=4998.75 - 27.0),
    ]


def test_a_wick_through_the_mss_level_is_not_an_mss() -> None:
    rows = with_rows(BASE, {MSS: (5004, 5005, 4996, 5001)})  # low 4996 < 5000, close 5001
    _, rec = go(rows)
    assert rec.states[MSS]["mss_done"] is False


def test_the_hunt_is_voided_when_the_mss_comes_too_late() -> None:
    late = with_rows(BASE, {MSS: (5004, 5005, 5001, 5002), ARM: (5002, 5003, 5001, 5002)})
    _, rec = go(late, max_bars_sweep_mss=1)
    assert [rec.states[i]["phase"] for i in (SWEEP, MSS, ARM)] == [1, 1, 0]
    assert rec.states[ARM]["dir"] == 0
    _, patient = go(late)
    assert rec.states[MSS]["phase"] == patient.states[ARM]["phase"] == 1


def test_the_hunt_is_voided_when_the_fvg_comes_too_late() -> None:
    late = with_rows(BASE, {ARM: (4997, 5003, 4995, 4996), FILL: (4996, 4999, 4994, 4995)})
    _, rec = go(late, max_bars_mss_fvg=1)
    assert rec.states[ARM]["phase"] == 1
    assert (rec.states[FILL]["phase"], rec.states[FILL]["mss_done"]) == (0, False)
    _, patient = go(late)
    assert patient.states[FILL]["phase"] == 1


@pytest.mark.parametrize(
    ("entry_fill", "expected"),
    [
        # short: proximal = gap bottom, distal = gap top
        ("Proximal (first touch)", (4998.0, 7.0, 5011.25, 4998.0 - 26.5)),
        ("50% (Consequent Encroachment)", (5000.0, 8.0, 5011.25, 4977.5)),
        ("Distal (full gap fill)", (5002.0, 10.0, 5011.25, 5002.0 - 18.5)),
    ],
)
def test_entry_level_modes(entry_fill: str, expected: tuple[float, float, float, float]) -> None:
    _, rec = go(BASE, entry_fill=entry_fill)
    entry, exit_ = rec.actions[ARM]
    assert isinstance(entry, Entry)
    assert isinstance(exit_, Exit)
    assert (entry.limit, entry.qty, exit_.stop, exit_.limit) == expected


def test_tick_offset_moves_the_stop_beyond_the_sweep_extreme() -> None:
    _, rec = go(BASE, tick_offset=4)  # stop 5012, risk 12 -> floor(500 / 60) = 8, TP 4976
    assert rec.actions[ARM] == [
        Entry("Short", Side.SHORT, 8.0, limit=5000.0),
        Exit("Exit S", "Short", stop=5012.0, limit=4976.0),
    ]


# One bar does it all (pivotLen 2): bar 7 sweeps 5010, closes below 5000 and leaves a bear
# gap under bar 5's low 5021.
ONE_BAR_SETUP: list[OHLC] = [
    (5004, 5005, 5003, 5004),  # 0 09:53
    (5004, 5006, 5002, 5005),  # 1 09:54
    (5005, 5010, 5004, 5006),  # 2 09:55  pivot high 5010 (known at bar 4)
    (5006, 5007, 5001, 5002),  # 3 09:56
    (5002, 5003, 5000, 5001),  # 4 09:57  pivot low 5000 (known at bar 6)
    (5021, 5023, 5021, 5022),  # 5 09:58
    (5022, 5026, 5018, 5019),  # 6 09:59
    (5019, 5020, 4998, 4999),  # 7 10:00  sweep + MSS + bear FVG [5020, 5021]
    (4999, 5000, 4998, 4999),  # 8 10:01
]


def test_sweep_mss_and_fvg_can_complete_on_one_bar() -> None:
    _, rec = go(ONE_BAR_SETUP, pivot_len=2, entry_fill="Proximal (first touch)")
    # entry 5020 (gap bottom), stop 5020.25, risk 0.25 -> floor(500 / 1.25) = 400, TP 5019.5
    assert rec.actions[7] == [
        Entry("Short", Side.SHORT, 400.0, limit=5020.0),
        Exit("Exit S", "Short", stop=5020.25, limit=5019.5),
    ]
    # The default CE level 5020.5 sits above that stop: risk <= 0, no order, still hunting.
    _, ce = go(ONE_BAR_SETUP, pivot_len=2)
    assert ce.actions[7] == []
    assert (ce.states[7]["phase"], ce.states[7]["mss_done"]) == (1, True)


# -- targets and sizing -------------------------------------------------------------------


@pytest.mark.parametrize("flip", [False, True])
def test_target_at_the_next_opposing_liquidity_pool(flip: bool) -> None:
    # Pivot lows below the entry 5000 at arming: [4985]; 4985 + 2 ticks = 4985.5 is
    # 14.5 >= 1 x 11.25 away -> target 4985.5; filled at bar 11 (+14.5 x 8 x 5 = 580).
    rows = mirror(BASE) if flip else BASE
    result, rec = go(rows, tp_mode="Next opposing liquidity")
    exit_ = rec.actions[ARM][1]
    assert isinstance(exit_, Exit)
    expected = 10_000.0 - 4985.5 if flip else 4985.5
    assert exit_.limit == expected
    (trade,) = result.trades
    assert (trade.exit_price, trade.reason) == (expected, ExitReason.TARGET)
    assert trade.pnl == pytest.approx(580.0)


def test_liquidity_target_falls_back_to_fixed_rr_below_min_rr() -> None:
    # 14.5 < 1.5 x 11.25 = 16.875 -> fixed 2R target 4977.5.
    _, rec = go(BASE, tp_mode="Next opposing liquidity", min_rr_liq=1.5)
    assert rec.actions[ARM] == SHORT_BRACKET


def test_liquidity_target_falls_back_when_no_pool_lies_beyond_the_entry() -> None:
    # Drops the 4985 pivot low: the only pool is 5000, which is not below the entry 5000.
    rows = series_of(BASE[3:], start_ms=BASE_START + 3 * MINUTE_MS)
    _, rec = go(rows, tp_mode="Next opposing liquidity")
    assert rec.actions[ARM - 3][1] == SHORT_BRACKET[1]


@pytest.mark.parametrize(
    ("params", "expected_qty"),
    [
        ({"risk_pct": 1.0}, 17.0),  # floor(1000 / 56.25) = 17
        ({"risk_base": "Fixed Balance", "fixed_bal": 50_000.0}, 4.0),  # floor(250 / 56.25)
        ({"risk_base": "Account Equity"}, 8.0),  # equity = 100 000 before the first trade
    ],
)
def test_risk_base_and_risk_pct_size_whole_contracts(
    params: dict[str, Any], expected_qty: float
) -> None:
    _, rec = go(BASE, **params)
    entry = rec.actions[ARM][0]
    assert isinstance(entry, Entry)
    assert entry.qty == expected_qty


@pytest.mark.parametrize(
    "params",
    [
        {"risk_pct": 0.01},  # 10 / 56.25 < 1
        {"risk_base": "Fixed Balance"},  # 10 000 x 0.5 % = 50 / 56.25 < 1
        {"risk_base": "Fixed Balance", "round_to_int": False},  # 0.89 is below MES min_qty
    ],
)
def test_no_arming_when_the_size_is_below_one_contract(params: dict[str, Any]) -> None:
    result, rec = go(BASE, **params)
    assert not any(isinstance(a, Entry) for acts in rec.actions.values() for a in acts)
    # The hunt stays in phase 1 and keeps trying (Pine leaves the phase untouched).
    assert rec.states[ARM]["phase"] == 1
    assert result.trades == ()


@pytest.mark.parametrize(("round_to_int", "expected"), [(True, 45.0), (False, 45.04)])
def test_fractional_sizing_is_floored_to_the_instrument_step(
    round_to_int: bool, expected: float
) -> None:
    # CFD: tick 0.1, point value 1 -> stop 5011.1, risk 11.1, 500 / 11.1 = 45.045...
    config = make_config(instrument=cfd())
    _, rec = go(BASE, config=config, round_to_int=round_to_int)
    entry = rec.actions[ARM][0]
    assert isinstance(entry, Entry)
    assert entry.qty == pytest.approx(expected)


# -- pending order: fill, cancel ----------------------------------------------------------


@BOTH
def test_the_bracket_is_live_only_after_the_limit_fills(model: FillModel) -> None:
    # Bar 10 never reaches 5000.25 but falls to 4970 (below the 4977.5 target): nothing
    # happens, because the target belongs to an entry that has not filled.
    rows = with_rows(BASE, {FILL: (4992, 4995, 4970, 4972)})
    result, rec = go(rows, model)
    assert result.trades == ()
    assert rec.states[FILL]["phase"] == 2


@BOTH
def test_a_trade_that_fills_and_closes_inside_one_bar_is_counted(model: FillModel) -> None:
    # Pessimistic O -> L -> H (fill) -> C (target); tv_ohlc O -> H (fill) -> L (target).
    rows = with_rows(BASE, {FILL: (4992, 5001, 4976, 4977)})
    result, rec = go(rows, model)
    (trade,) = result.trades
    assert trade.entry_bar_index == trade.exit_bar_index == FILL
    assert (trade.entry_price, trade.exit_price) == (5000.0, 4977.5)
    assert rec.actions[FILL] == []  # no cancel: the fill was detected by the trade count
    state = rec.states[FILL]
    assert (state["phase"], state["trades_window"], state["trades_today"]) == (0, 1, 1)


def test_cancel_when_the_close_breaks_the_stop() -> None:
    # A limit that needs 100 ticks through cannot fill, so the close above 5011.25 cancels.
    rows = with_rows(BASE, {FILL: (4992, 5012, 4991, 5012)})
    result, rec = go(rows, config=make_config(limit_through_ticks=100))
    assert rec.actions[FILL] == CANCEL_ALL
    assert rec.states[FILL]["phase"] == 0
    assert result.trades == ()


def test_cancel_when_the_window_gate_closes_on_the_last_window_bar() -> None:
    # Window 10:00-10:05: 10:03 still passes (604 < 605), 10:04 does not (605 < 605).
    rows = with_rows(BASE, {FILL: NO_FILL_10, TP_BAR: NO_FILL_11, 12: (4994, 5003, 4994, 5002)})
    result, rec = go(rows, am_end_h=10, am_end_m=5)
    assert rec.actions[FILL] == []
    assert rec.actions[TP_BAR] == CANCEL_ALL
    assert result.trades == ()  # bar 12 would have filled the order


def test_cancel_on_the_sessions_last_bar() -> None:
    rows = with_rows(BASE, {FILL: NO_FILL_10, TP_BAR: (4993, 5003, 4993, 5002)})
    last = [i in (FILL, len(rows) - 1) for i in range(len(rows))]
    result, rec = go(rows, session_last=last)
    assert rec.actions[FILL] == CANCEL_ALL
    assert result.trades == ()


def test_cancel_after_max_bars_to_fill() -> None:
    rows = with_rows(BASE, {FILL: NO_FILL_10, TP_BAR: NO_FILL_11})
    _, rec = go(rows, max_bars_to_fill=1)
    assert rec.actions[FILL] == []  # 10 - 9 = 1 is not > 1
    assert rec.actions[TP_BAR] == CANCEL_ALL
    _, patient = go(rows)
    assert patient.actions[TP_BAR] == []


# -- the fill-bar re-pricing in both fill models -----------------------------------------


def test_both_fill_models_give_the_same_levels_only_the_timing_differs() -> None:
    p_result, p_rec = go(BASE, P)
    tv_result, tv_rec = go(BASE, TV)
    assert [(t.entry_price, t.initial_stop, t.exit_price) for t in p_result.trades] == [
        (t.entry_price, t.initial_stop, t.exit_price) for t in tv_result.trades
    ]
    # Pessimistic: on_fill re-issues the bracket at the fill instant ...
    ((fill, fill_out),) = p_rec.fill_actions
    assert (fill.entry_id, fill.price, fill.bar_index) == ("Short", 5000.0, FILL)
    assert fill_out == [SHORT_BRACKET[1]]
    # ... tv_ohlc: never called; the fill bar's on_bar issues the same bracket at its close.
    assert tv_rec.fill_actions == []
    assert tv_rec.actions[FILL] == [SHORT_BRACKET[1]]
    assert p_rec.actions[FILL] == tv_rec.actions[FILL]


def test_on_fill_returns_only_exit_actions() -> None:
    _, rec = go(BASE)
    for _, out in rec.fill_actions:
        assert out
        assert all(isinstance(action, Exit) for action in out)


# -- protections -------------------------------------------------------------------------


@BOTH
def test_a_stopped_trade_is_a_loss_at_the_stop(model: FillModel) -> None:
    result, rec = go(with_rows(BASE, {TP_BAR: STOP_11}), model)
    (trade,) = result.trades
    assert (trade.exit_price, trade.reason) == (5011.25, ExitReason.STOP)
    assert trade.pnl == pytest.approx(-450.0)
    assert rec.states[TP_BAR]["consec_losses"] == 1
    assert rec.actions[TP_BAR] == []  # a 0.45 % loss stays inside the 2 % daily limit


@BOTH
def test_daily_drawdown_halt_flattens_the_open_position(model: FillModel) -> None:
    # Bar 11 closes at 5003: open loss 3 x 8 x 5 = 120 = 0.12 % >= 0.1 % of the day start.
    rows = with_rows(BASE, {TP_BAR: (4999, 5004, 4998, 5003)})
    result, rec = go(rows, model, max_daily_dd_pct=0.1)
    assert rec.actions[TP_BAR] == [
        SHORT_BRACKET[1],
        CloseAll("Daily DD halt"),
        Cancel("Long"),
        Cancel("Short"),
    ]
    (trade,) = result.trades
    assert (trade.exit_bar_index, trade.exit_price) == (TP_BAR, 5003.0)
    assert (trade.reason, trade.comment) == (ExitReason.CLOSE, "Daily DD halt")
    assert rec.states[TP_BAR]["halted_today"] is True
    assert rec.states[12]["consec_losses"] == 1  # the closed row is seen one bar later


def test_daily_drawdown_halt_when_flat_also_cancels_the_brackets() -> None:
    rows = with_rows(BASE, {TP_BAR: STOP_11})
    _, rec = go(rows, max_daily_dd_pct=0.1)
    assert rec.actions[TP_BAR] == CANCEL_ALL
    assert rec.states[TP_BAR]["halted_today"] is True


def test_daily_drawdown_halt_does_not_flatten_when_flatten_breach_is_off() -> None:
    rows = with_rows(BASE, {TP_BAR: (4999, 5004, 4998, 5003)})
    _, rec = go(rows, max_daily_dd_pct=0.1, flatten_breach=False)
    assert rec.actions[TP_BAR] == [SHORT_BRACKET[1], Cancel("Long"), Cancel("Short")]


@pytest.mark.parametrize(("max_consec", "expected_phase"), [(1, 0), (3, 1)])
def test_consecutive_losses_halt_new_hunts_for_the_day(
    max_consec: int, expected_phase: int
) -> None:
    # After the -450 stop a later AM bar sweeps lastPH 5011 again (max 2 trades per window).
    rows = with_rows(
        BASE, {TP_BAR: STOP_11, 12: (5010, 5013, 5009, 5012), 13: (5012, 5012.5, 5011, 5012)}
    )
    _, rec = go(rows, max_consec=max_consec, max_tr_window=2, max_tr_day=2)
    assert rec.states[12]["last_ph"] == 5011
    assert rec.states[12]["halted_today"] is (max_consec == 1)
    assert rec.states[12]["phase"] == expected_phase


def _two_days(day1: Sequence[OHLC], day2: Sequence[OHLC]) -> tuple[BarSeries, list[bool]]:
    times = [BASE_START + i * MINUTE_MS for i in range(len(day1))]
    times += [BASE_START + DAY_MS + i * MINUTE_MS for i in range(len(day2))]
    last = [False] * len(times)
    last[len(day1) - 1] = last[-1] = True
    return series_of(list(day1) + list(day2), times=times), last


@pytest.mark.parametrize(("use_overall_dd", "expected_trades"), [(True, 1), (False, 2)])
def test_overall_drawdown_halt_is_permanent(use_overall_dd: bool, expected_trades: int) -> None:
    day1 = with_rows(BASE, {TP_BAR: STOP_11, 12: (5010, 5011, 5009, 5010)})
    series, last = _two_days(day1, BASE)
    result, rec = go(
        series,
        session_last=last,
        use_daily_dd=False,
        use_overall_dd=use_overall_dd,
        max_overall_pct=0.1,
    )
    assert len(result.trades) == expected_trades
    if use_overall_dd:
        assert rec.actions[TP_BAR] == CANCEL_ALL
        assert all(state["halted_perma"] for state in rec.states[TP_BAR:])
        assert rec.states[len(day1) + SWEEP]["phase"] == 0  # no hunt on the next day


def test_consecutive_losses_reset_on_a_new_day() -> None:
    day1 = with_rows(BASE, {TP_BAR: STOP_11, 12: (5010, 5011, 5009, 5010)})
    series, last = _two_days(day1, BASE)
    result, rec = go(series, session_last=last, max_consec=1)
    assert rec.states[len(day1) - 1]["halted_today"] is True
    assert rec.states[len(day1)]["consec_losses"] == 0
    assert len(result.trades) == 2


@BOTH
def test_end_of_day_flat_at_the_force_flat_time(model: FillModel) -> None:
    rows = with_rows(BASE, {TP_BAR: HOLD_11})
    result, rec = go(rows, model, day_end_h=10, day_end_m=4)
    assert rec.actions[TP_BAR] == [SHORT_BRACKET[1], CloseAll("EOD flat")]
    (trade,) = result.trades
    assert (trade.exit_bar_index, trade.exit_price, trade.comment) == (TP_BAR, 4996.0, "EOD flat")
    assert trade.pnl == pytest.approx(160.0)


def test_end_of_day_flat_at_15_55_with_the_default_inputs() -> None:
    # The base bars shifted to 13:53: the PM window (14:00) trades; the position is held
    # by quiet bars until the 15:55 bar (index 122) closes it.
    rows = BASE[:TP_BAR] + [HOLD_11] * 115
    start = BASE_START + 4 * 60 * MINUTE_MS
    result, _ = go(series_of(rows, start_ms=start))
    (trade,) = result.trades
    assert trade.exit_bar_index == 122
    assert (trade.reason, trade.comment, trade.exit_price) == (ExitReason.CLOSE, "EOD flat", 4996.0)


def test_session_last_bar_flattens_the_open_position() -> None:
    rows = with_rows(BASE, {TP_BAR: HOLD_11})
    last = [i in (TP_BAR, len(rows) - 1) for i in range(len(rows))]
    result, _ = go(rows, session_last=last)
    (trade,) = result.trades
    assert (trade.exit_bar_index, trade.comment) == (TP_BAR, "EOD flat")


def test_end_of_range_flattens_after_the_end_date() -> None:
    # endDate = 10:03 New York (15:03 UTC): bar 10 is in range, bar 11 (10:04) is past it.
    rows = with_rows(BASE, {TP_BAR: HOLD_11})
    end = dt.datetime(2026, 3, 2, 15, 3, tzinfo=dt.UTC)
    result, rec = go(rows, end_date=end)
    assert rec.actions[TP_BAR] == [SHORT_BRACKET[1], CloseAll("End of range")]
    (trade,) = result.trades
    assert trade.comment == "End of range"
