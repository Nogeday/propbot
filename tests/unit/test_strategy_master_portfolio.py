"""The Master portfolio port (`propbot.strategies.master_portfolio`) against hand-executed Pine.

Every expected number below was derived by walking `pine/master_prop_firm_portfolio.pine`
by hand over the bars of the test, block by block in the script's order (open-leg scan,
fill-bar re-pricing, breakers, S1, S2, forced flat). Compact windows keep a scenario to a
few 1-minute bars:

    S1 only     OR [09:30, 09:31), session end 15:59 unless the test says otherwise
    S2 only     Asian [23:58, 00:01), active [00:02, 00:20)  (wraps midnight)
    COMBINED    Asian [09:20, 09:23), active [09:23, 09:50), OR [09:30, 09:31), S1 end 09:50

The instrument is MES-like (tick 0.25, point value 5, whole contracts), costs are zero, the
initial capital is 100 000 and the risk 0.5 % of it: 500 per trade, i.e. a size of
floor(100 / stop distance) contracts.
"""

import datetime as dt
import typing
from collections.abc import Sequence
from typing import Any
from zoneinfo import ZoneInfo

import pytest
from pydantic import ValidationError

from propbot.backtest.engine import run_backtest
from propbot.backtest.model import EngineResult, ExitReason, FillModel, TradeRecord
from propbot.core.bars import Bar, BarSeries, DataSource
from propbot.data.sessions import session_last_flags
from propbot.signals.schema import STRATEGY_NAMES
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
from propbot.strategies.master_portfolio import (
    LEG_IDS,
    PINE_FILE,
    STRATEGY_ID,
    Fvg,
    MasterPortfolio,
    MasterPortfolioParams,
)
from support.engine import make_config, mes
from support.pine import pine_defaults

NY = ZoneInfo("America/New_York")
type OHLC = tuple[float, float, float, float]

BOTH_MODELS = pytest.mark.parametrize("fill_model", [FillModel.PESSIMISTIC, FillModel.TV_OHLC])
S1_LEG, S2_LEG = LEG_IDS

# Pine input -> MasterPortfolioParams field.
PORTED: dict[str, str] = {
    "s1_enable": "s1_enable",
    "s2_enable": "s2_enable",
    "riskPct": "risk_pct",
    "riskBase": "risk_base",
    "fixedBal": "fixed_bal",
    "rr": "rr",
    "maxConcurrentRiskPct": "max_concurrent_risk_pct",
    "tz": "tz",
    "qtyRound": "qty_round",
    "useBreakers": "use_breakers",
    "dailyDDPct": "daily_dd_pct",
    "flattenBreach": "flatten_breach",
    "maxConsecLoss": "max_consec_loss",
    "startDate": "start_date",
    "endDate": "end_date",
    "s1_orStartHour": "s1_or_start_hour",
    "s1_orStartMin": "s1_or_start_min",
    "s1_orMinutes": "s1_or_minutes",
    "s1_sessEndHour": "s1_sess_end_hour",
    "s1_sessEndMin": "s1_sess_end_min",
    "s1_closeAtEnd": "s1_close_at_end",
    "s1_breakConfirm": "s1_break_confirm",
    "s1_allowLong": "s1_allow_long",
    "s1_allowShort": "s1_allow_short",
    "s1_useFVG": "s1_use_fvg",
    "s1_useIFVG": "s1_use_ifvg",
    "s1_minFvgTicks": "s1_min_fvg_ticks",
    "s1_stopMethod": "s1_stop_method",
    "s1_slBuf": "s1_sl_buf",
    "s1_atrLenInit": "s1_atr_len_init",
    "s1_atrMultInit": "s1_atr_mult_init",
    "s1_useTrail": "s1_use_trail",
    "s1_trailMode": "s1_trail_mode",
    "s1_swingLen": "s1_swing_len",
    "s1_atrLenT": "s1_atr_len_t",
    "s1_atrMultT": "s1_atr_mult_t",
    "s1_trailPct": "s1_trail_pct",
    "s1_trailActR": "s1_trail_act_r",
    "s1_trailMinStepTicks": "s1_trail_min_step_ticks",
    "s1_tradeMon": "s1_trade_mon",
    "s1_tradeTue": "s1_trade_tue",
    "s1_tradeWed": "s1_trade_wed",
    "s1_tradeThu": "s1_trade_thu",
    "s1_tradeFri": "s1_trade_fri",
    "s2_asianStartHour": "s2_asian_start_hour",
    "s2_asianStartMin": "s2_asian_start_min",
    "s2_asianEndHour": "s2_asian_end_hour",
    "s2_asianEndMin": "s2_asian_end_min",
    "s2_activeStartHour": "s2_active_start_hour",
    "s2_activeStartMin": "s2_active_start_min",
    "s2_activeEndHour": "s2_active_end_hour",
    "s2_activeEndMin": "s2_active_end_min",
    "s2_closeAtEnd": "s2_close_at_end",
    "s2_maxBars": "s2_max_bars",
    "s2_minFvgTicks": "s2_min_fvg_ticks",
    "s2_allowLong": "s2_allow_long",
    "s2_allowShort": "s2_allow_short",
    "s2_stopMethod": "s2_stop_method",
    "s2_slBuf": "s2_sl_buf",
    "s2_atrLenInit": "s2_atr_len_init",
    "s2_atrMultInit": "s2_atr_mult_init",
    "s2_tradeMon": "s2_trade_mon",
    "s2_tradeTue": "s2_trade_tue",
    "s2_tradeWed": "s2_trade_wed",
    "s2_tradeThu": "s2_trade_thu",
    "s2_tradeFri": "s2_trade_fri",
}
# Visuals, the stats table, the OOS marker (drives only visuals and the table), the alert token.
NOT_PORTED: frozenset[str] = frozenset(
    {
        "oosStart",
        "showDateViz",
        "showTable",
        "tableScope",
        "statsSource",
        "tablePos",
        "tableSize",
        "s1_showAllFVG",
        "s1_boxProj",
        "s1_fvgProj",
        "s2_showRange",
        "s2_rangeProj",
        "s2_showZones",
        "s2_boxProj",
        "alertToken",
    }
)

S1_ONLY: dict[str, Any] = {"s2_enable": False, "s1_or_minutes": 1}
S2_ONLY: dict[str, Any] = {
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
COMBINED: dict[str, Any] = {
    "s1_or_minutes": 1,
    "s1_sess_end_hour": 9,
    "s1_sess_end_min": 50,
    "s2_asian_start_hour": 9,
    "s2_asian_start_min": 20,
    "s2_asian_end_hour": 9,
    "s2_asian_end_min": 23,
    "s2_active_start_hour": 9,
    "s2_active_start_min": 23,
    "s2_active_end_hour": 9,
    "s2_active_end_min": 50,
}

MONDAY_0929 = dt.datetime(2026, 3, 2, 9, 29, tzinfo=NY)
MONDAY_0919 = dt.datetime(2026, 3, 2, 9, 19, tzinfo=NY)
MONDAY_2357 = dt.datetime(2026, 3, 2, 23, 57, tzinfo=NY)
SUNDAY_2359 = dt.datetime(2026, 3, 1, 23, 59, tzinfo=NY)

# S1 long: OR 101 / 99 (bar 1); bar 2 closes above -> bias long and a bullish FVG
# [100.5, 100.75]; bar 3 forms [101, 101.25]; bar 4 touches it -> signal (est 101.25,
# stop 101 - 2 ticks = 100.5, 133 contracts); bar 5 fills at 101.5 -> re-priced target
# 101.5 + 2 x 1.0 = 103.5; bar 6 trades 103.75, one tick through it.
S1_LONG_ROWS: tuple[OHLC, ...] = (
    (100.0, 100.5, 99.5, 100.0),  # 0 09:29
    (100.0, 101.0, 99.0, 100.5),  # 1 09:30 opening range
    (100.75, 101.75, 100.75, 101.5),  # 2 09:31 breakout + bullish FVG [100.5, 100.75]
    (101.5, 102.0, 101.25, 101.75),  # 3 09:32 bullish FVG [101, 101.25]
    (101.75, 101.75, 101.0, 101.25),  # 4 09:33 touch -> signal
    (101.5, 101.75, 101.25, 101.5),  # 5 09:34 fill at the open, 101.5
    (101.5, 103.75, 101.5, 103.5),  # 6 09:35 target 103.5
)

# S2 short (wraps midnight): range 102 / 98; bar 5 sweeps the High on the first active bar;
# bar 7 arms a bearish FVG [101.25, 101.5]; bar 8 touches it -> signal (est 100.75, stop
# 101.5 + 2 ticks = 102, 80 contracts); bar 9 fills at 100.5 -> target 100.5 - 2 x 1.5 = 97.5.
S2_SHORT_ROWS: tuple[OHLC, ...] = (
    (100.0, 100.5, 99.5, 100.0),  # 0 23:57
    (100.0, 101.0, 99.0, 100.0),  # 1 23:58 Asian start
    (100.0, 102.0, 99.5, 101.0),  # 2 23:59
    (101.0, 101.5, 98.0, 99.0),  # 3 00:00 Tuesday (still Asian)
    (101.0, 101.5, 100.5, 101.5),  # 4 00:01 Asian end -> range ready
    (101.5, 103.0, 101.5, 102.5),  # 5 00:02 sweep of the High
    (102.5, 102.75, 100.5, 101.0),  # 6
    (101.0, 101.25, 100.0, 100.25),  # 7 bearish FVG -> arm short
    (100.25, 101.5, 100.0, 100.75),  # 8 touch -> signal
    (100.5, 100.75, 99.5, 99.75),  # 9 fill at 100.5
    (99.75, 100.0, 97.25, 97.5),  # 10 target 97.5
)

# COMBINED, S2 long: range 101 / 99 (bars 1-3); bar 4 sweeps the Low on the first active bar;
# bar 6 arms a bullish FVG [100.25, 100.5]; bar 7 touches it -> signal (est 100.5, stop
# 100.25 - 2 ticks = 99.75, 133 contracts); bar 8 fills at 100.5.
S2_LONG_PREFIX: tuple[OHLC, ...] = (
    (100.0, 100.5, 99.5, 100.0),  # 0 09:19
    (100.0, 101.0, 99.0, 100.0),  # 1 09:20 Asian start
    (100.0, 100.5, 99.5, 100.0),  # 2 09:21
    (100.0, 100.5, 99.5, 100.0),  # 3 09:22
    (100.0, 100.25, 98.5, 99.0),  # 4 09:23 Asian end + first active bar: sweep of the Low
    (99.0, 99.75, 98.75, 99.5),  # 5 09:24
    (100.5, 101.0, 100.5, 100.75),  # 6 09:25 bullish FVG [100.25, 100.5] -> arm long
)
# Then S1 long: OR 101 / 100 (bar 11), breakout + FVG [100.75, 101] (bar 12), FVG [101, 101.25]
# (bar 13), touch (bar 14: est 101.25, stop 100.5, 133 contracts), fill 101.5 (bar 15).
SAME_DIRECTION_ROWS: tuple[OHLC, ...] = (
    *S2_LONG_PREFIX,
    (100.75, 101.0, 100.25, 100.5),  # 7 09:26 S2 touch -> signal
    (100.5, 100.75, 100.25, 100.5),  # 8 09:27 S2 fill at 100.5
    (100.5, 100.75, 100.25, 100.5),  # 9 09:28
    (100.5, 100.75, 100.25, 100.5),  # 10 09:29
    (100.5, 101.0, 100.0, 100.5),  # 11 09:30 opening range
    (101.0, 101.75, 101.0, 101.5),  # 12 09:31 breakout + bullish FVG [100.75, 101]
    (101.5, 102.0, 101.25, 101.75),  # 13 09:32 bullish FVG [101, 101.25]
    (101.75, 101.75, 101.0, 101.25),  # 14 09:33 S1 touch -> signal
    (101.5, 101.75, 101.25, 101.5),  # 15 09:34 S1 fill at 101.5
    (101.5, 101.75, 101.25, 101.5),  # 16 09:35 last bar of the data
)


def _mirror(rows: Sequence[OHLC]) -> tuple[OHLC, ...]:
    """Every price p -> 200 - p: a long setup becomes the same short setup."""
    return tuple((200.0 - o, 200.0 - lo, 200.0 - h, 200.0 - c) for o, h, lo, c in rows)


def _ms(moment: dt.datetime) -> int:
    return int(moment.timestamp() * 1000)


def stamped(start: dt.datetime, rows: Sequence[OHLC]) -> list[tuple[int, OHLC]]:
    """Consecutive 1-minute bars from `start`."""
    first = _ms(start)
    return [(first + i * 60_000, row) for i, row in enumerate(rows)]


def series_of(bars: Sequence[tuple[int, OHLC]]) -> BarSeries:
    built = tuple(
        Bar(ts_open_ms=ts, open=o, high=h, low=lo, close=c, volume=1.0)
        for ts, (o, h, lo, c) in bars
    )
    source = DataSource(name="test", format="generic", sha256="", synthetic=True, bar_tz="UTC")
    return BarSeries(symbol="TEST", tf_seconds=60, bars=built, source=source)


class Recorder:
    """Wraps the port and keeps every batch of actions it returned (by bar / on fill)."""

    def __init__(self, inner: MasterPortfolio) -> None:
        self.inner = inner
        self.bars: dict[int, list[Action]] = {}
        self.contexts: dict[int, StrategyContext] = {}
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
        self.contexts[ctx.bar_index] = ctx
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


# -- module surface and parameters ----------------------------------------------------------------


def test_module_surface_names_the_portfolio_and_its_two_legs() -> None:
    assert STRATEGY_ID == "master_portfolio"
    assert STRATEGY_ID not in STRATEGY_NAMES
    assert LEG_IDS == ("master_ny_orb_fvg", "master_asian_sweep")
    assert all(leg in STRATEGY_NAMES for leg in LEG_IDS)
    assert PINE_FILE == "master_prop_firm_portfolio.pine"
    strategy = MasterPortfolio()
    assert strategy.strategy_id == STRATEGY_ID
    assert strategy.pyramiding == 2  # strategy(pyramiding = 2)


def test_every_pine_input_is_either_ported_or_deliberately_left_out() -> None:
    names = set(pine_defaults(PINE_FILE))
    assert set(PORTED) | NOT_PORTED == names
    assert not set(PORTED) & NOT_PORTED
    assert set(PORTED.values()) == set(MasterPortfolioParams.model_fields)


def test_every_ported_default_equals_the_pine_default() -> None:
    defaults = pine_defaults(PINE_FILE)
    params = MasterPortfolioParams()
    for pine_name, field in PORTED.items():
        assert getattr(params, field) == defaults[pine_name], pine_name


def test_option_inputs_accept_exactly_the_pine_option_strings() -> None:
    hints = typing.get_type_hints(MasterPortfolioParams)

    def options(field: str) -> set[str]:
        alias = hints[field]
        return set(typing.get_args(alias.__value__))

    assert options("risk_base") == {"Initial Capital", "Account Equity", "Fixed Balance"}
    assert options("qty_round") == {"Auto", "Whole units", "Fractional"}
    assert options("s1_break_confirm") == {"Close", "Wick"}
    assert options("s1_stop_method") == {"FVG Buffer", "ATR Based"}
    assert options("s2_stop_method") == {"FVG Buffer", "ATR Based"}
    assert options("s1_trail_mode") == {"Swing (1m structure)", "ATR step", "Percent step"}


@pytest.mark.parametrize(
    "bad",
    [
        {"risk_pct": 0.05},  # minval 0.1
        {"max_concurrent_risk_pct": 0.0},
        {"daily_dd_pct": 0.0},
        {"max_consec_loss": 0},
        {"s1_or_minutes": 61},  # maxval 60
        {"s1_or_start_hour": 24},
        {"s2_active_end_min": 60},
        {"s1_trail_min_step_ticks": 0},
        {"s1_trail_pct": 0.001},
        {"s1_trail_act_r": -0.1},
        {"s2_max_bars": 0},
        {"s1_trail_mode": "Swing"},
        {"risk_base": "Equity"},
        {"tz": "Mars/Olympus"},
        {"start_date": dt.datetime(2024, 1, 1)},  # naive: names no instant
        {"unknown": 1},
    ],
)
def test_params_reject_values_the_tradingview_dialog_would_not_allow(bad: dict[str, Any]) -> None:
    with pytest.raises(ValidationError):
        MasterPortfolioParams(**bad)


def test_date_inputs_are_timezone_aware_utc() -> None:
    params = MasterPortfolioParams(end_date=dt.datetime(2026, 3, 2, 9, 30, tzinfo=NY))
    assert params.end_date == dt.datetime(2026, 3, 2, 14, 30, tzinfo=dt.UTC)
    assert params.end_date.tzinfo is dt.UTC


def test_two_instances_never_share_state() -> None:
    a, b = MasterPortfolio(), MasterPortfolio()
    a.s1_fvgs.append(Fvg(1.0, 0.5, 1, 0, 0, -1))
    assert b.s1_fvgs == []
    run(stamped(MONDAY_0929, S1_LONG_ROWS), **S1_ONLY)  # another run leaves both untouched
    assert b.s1_fvgs == []
    assert b.s1_in_dir == 0


# -- S1: ORB + FVG ------------------------------------------------------------


@BOTH_MODELS
def test_s1_long_enters_on_the_fvg_touch_and_exits_at_the_target_repriced_from_the_fill(
    fill_model: FillModel,
) -> None:
    result, rec = run(stamped(MONDAY_0929, S1_LONG_ROWS), fill_model, **S1_ONLY)

    assert rec.bars[4] == [
        Entry("S1 Long", Side.LONG, 133.0, comment="S1 Long", strategy_id=S1_LEG),
        Exit("S1 ExitL", "S1 Long", stop=100.5, limit=102.75),  # signal-bar estimate
    ]
    # Fill bar: the bracket from the real fill, 101.5 (stop structural, target 101.5 + 2 x 1).
    assert rec.bars[5] == [Exit("S1 ExitL", "S1 Long", stop=100.5, limit=103.5)]
    trade = only(result.trades)
    assert trade.strategy_id == S1_LEG
    assert trade.entry_id == "S1 Long"
    assert (trade.entry_bar_index, trade.entry_price, trade.qty) == (5, 101.5, 133.0)
    assert (trade.exit_bar_index, trade.exit_price, trade.reason) == (6, 103.5, ExitReason.TARGET)
    assert trade.exit_id == "S1 ExitL"
    assert trade.initial_stop == 100.5
    assert trade.pnl == pytest.approx(2.0 * 133 * 5)


@BOTH_MODELS
def test_s1_short_is_the_mirror_of_the_long(fill_model: FillModel) -> None:
    result, rec = run(stamped(MONDAY_0929, _mirror(S1_LONG_ROWS)), fill_model, **S1_ONLY)

    assert rec.bars[4] == [
        Entry("S1 Short", Side.SHORT, 133.0, comment="S1 Short", strategy_id=S1_LEG),
        Exit("S1 ExitS", "S1 Short", stop=99.5, limit=97.25),
    ]
    assert rec.bars[5] == [Exit("S1 ExitS", "S1 Short", stop=99.5, limit=96.5)]
    trade = only(result.trades)
    assert (trade.side, trade.entry_price, trade.exit_price) == (Side.SHORT, 98.5, 96.5)
    assert trade.reason is ExitReason.TARGET
    assert trade.initial_stop == 99.5


def test_both_fill_models_give_the_same_levels_for_the_same_fill() -> None:
    _, pess = run(stamped(MONDAY_0929, S1_LONG_ROWS), FillModel.PESSIMISTIC, **S1_ONLY)
    _, tv = run(stamped(MONDAY_0929, S1_LONG_ROWS), FillModel.TV_OHLC, **S1_ONLY)

    # One shared method: on_fill (pessimistic) and the fill bar's on_bar (tv_ohlc).
    assert pess.fills[0][1] == tv.bars[5] == pess.bars[5]


def test_s1_on_fill_reprices_at_the_fill_instant_in_the_pessimistic_model() -> None:
    _, rec = run(stamped(MONDAY_0929, S1_LONG_ROWS), FillModel.PESSIMISTIC, **S1_ONLY)

    ((fill, out),) = rec.fills
    assert (fill.entry_id, fill.price) == ("S1 Long", 101.5)
    assert out == [Exit("S1 ExitL", "S1 Long", stop=100.5, limit=103.5)]


def test_s1_tv_model_never_calls_on_fill_and_reprices_on_the_fill_bar_close() -> None:
    _, rec = run(stamped(MONDAY_0929, S1_LONG_ROWS), FillModel.TV_OHLC, **S1_ONLY)

    assert rec.fills == []
    assert rec.bars[5] == [Exit("S1 ExitL", "S1 Long", stop=100.5, limit=103.5)]


def test_fill_models_differ_only_in_the_timing_of_the_repriced_target() -> None:
    # Bar 5 trades 103.0: one tick through the signal-bar target 102.75 but not through the
    # re-priced 103.5. tv_ohlc still has the estimate working inside the fill bar (Pine);
    # pessimistic re-priced at the fill. Bar 6 is the session's last bar: S1 EOD close.
    rows = (*S1_LONG_ROWS[:5], (101.5, 103.0, 101.25, 102.5), (102.5, 102.75, 102.25, 102.5))

    pess, pess_rec = run(stamped(MONDAY_0929, rows), FillModel.PESSIMISTIC, **S1_ONLY)
    tv, tv_rec = run(stamped(MONDAY_0929, rows), FillModel.TV_OHLC, **S1_ONLY)

    # tv_ohlc: the trade left the book inside its fill bar, so the script never re-priced it
    # (the scan resets the leg on that bar's close) and its first stop is the estimate.
    assert tv_rec.bars[5] == []
    assert pess_rec.fills[0][1] == [Exit("S1 ExitL", "S1 Long", stop=100.5, limit=103.5)]
    assert only(pess.trades).initial_stop == only(tv.trades).initial_stop == 100.5
    tv_trade = only(tv.trades)
    assert (tv_trade.exit_bar_index, tv_trade.exit_price) == (5, 102.75)
    assert tv_trade.reason is ExitReason.TARGET
    pess_trade = only(pess.trades)
    assert (pess_trade.exit_bar_index, pess_trade.exit_price) == (6, 102.5)
    assert (pess_trade.reason, pess_trade.comment) == (ExitReason.CLOSE, "S1 EOD")


@BOTH_MODELS
def test_s1_atr_stop_reapplies_the_signal_bar_distance_to_the_fill(fill_model: FillModel) -> None:
    # ATR(1) on bar 4 = its true range: max(0.75, |101.75 - 101.75|, |101.0 - 101.75|) = 0.75.
    # Estimate: 101.25 - 0.75 x 1.5 = 100.125 (distance 1.125, 88 contracts).
    # Fill 101.5: stop 101.5 - 1.125 = 100.375, target 101.5 + 2 x 1.125 = 103.75.
    rows = (*S1_LONG_ROWS[:6], (101.5, 101.75, 101.25, 101.5))
    params = {**S1_ONLY, "s1_stop_method": "ATR Based", "s1_atr_len_init": 1}

    result, rec = run(stamped(MONDAY_0929, rows), fill_model, **params)

    assert rec.bars[4][0] == Entry(
        "S1 Long", Side.LONG, 88.0, comment="S1 Long", strategy_id=S1_LEG
    )
    assert rec.bars[4][1] == Exit("S1 ExitL", "S1 Long", stop=100.125, limit=103.5)
    assert rec.bars[5] == [Exit("S1 ExitL", "S1 Long", stop=100.375, limit=103.75)]
    assert only(result.trades).initial_stop == 100.375


@BOTH_MODELS
def test_s1_trailing_moves_the_stop_by_swing_lows_in_steps_of_at_least_four_ticks(
    fill_model: FillModel,
) -> None:
    # rr 5 keeps the target (106.5) out of reach. From bar 6 the trade is 1 R in profit; the
    # candidate lowest(low, 5) - 2 ticks first beats the stop 100.5 by >= 4 ticks on bar 11
    # (lows 102.25..103.25 -> 101.75). Bar 12 falls through it.
    rows = (
        *S1_LONG_ROWS[:6],
        (101.5, 102.75, 101.5, 102.5),  # 6  candidate 100.25
        (102.5, 103.0, 102.25, 102.75),  # 7  100.5 (no improvement)
        (102.75, 103.25, 102.5, 103.0),  # 8  100.5
        (103.0, 103.5, 102.75, 103.25),  # 9  100.75 (1 tick)
        (103.25, 103.75, 103.0, 103.5),  # 10 101.0 (2 ticks)
        (103.5, 104.0, 103.25, 103.75),  # 11 101.75 (5 ticks) -> trail
        (103.75, 103.75, 101.5, 101.75),  # 12 stop hit at 101.75
    )
    params = {**S1_ONLY, "rr": 5.0, "s1_sess_end_hour": 9, "s1_sess_end_min": 50}

    result, rec = run(stamped(MONDAY_0929, rows), fill_model, **params)

    stops = {i: rec.bars[i][-1] for i in range(5, 12)}
    for i in range(5, 11):
        assert stops[i] == Exit("S1 ExitL", "S1 Long", stop=100.5, limit=106.5), i
    assert stops[11] == Exit("S1 ExitL", "S1 Long", stop=101.75, limit=106.5)
    trade = only(result.trades)
    assert (trade.exit_bar_index, trade.exit_price, trade.reason) == (12, 101.75, ExitReason.STOP)
    assert trade.initial_stop == 100.5


def test_s1_takes_one_trade_per_new_york_day_and_a_new_day_rearms_it() -> None:
    # Day 1: the trade hits its target on bar 6; bar 7 touches the fresh FVG again: no entry.
    day1 = (*S1_LONG_ROWS, (103.5, 103.5, 101.0, 101.25), (101.25, 101.5, 101.0, 101.25))
    tuesday = MONDAY_0929 + dt.timedelta(days=1)
    bars = stamped(MONDAY_0929, day1) + stamped(tuesday, S1_LONG_ROWS)

    result, rec = run(bars, **S1_ONLY)

    assert [i for i, _ in rec.entries()] == [4, len(day1) + 4]
    assert [t.exit_price for t in result.trades] == [103.5, 103.5]


@pytest.mark.parametrize(("end_min", "entries"), [(34, 0), (35, 1)])
def test_s1_entry_time_gate_needs_the_fill_bar_before_the_session_end(
    end_min: int, entries: int
) -> None:
    # Signal bar 09:33: the fill bar opens 09:34, which must be before the session end.
    params = {**S1_ONLY, "s1_sess_end_hour": 9, "s1_sess_end_min": end_min}

    _, rec = run(stamped(MONDAY_0929, S1_LONG_ROWS), **params)

    assert len(rec.entries()) == entries


def test_s1_no_entry_on_the_sessions_last_bar() -> None:
    _, rec = run(stamped(MONDAY_0929, S1_LONG_ROWS[:5]), **S1_ONLY)

    assert rec.entries() == []


def test_s1_closes_its_leg_at_the_session_end_bar_close() -> None:
    rows = (*S1_LONG_ROWS[:6], *[(101.5, 101.75, 101.25, 101.5)] * 4)
    params = {**S1_ONLY, "s1_sess_end_hour": 9, "s1_sess_end_min": 37}

    result, rec = run(stamped(MONDAY_0929, rows), **params)

    # 09:37 is bar 8: one Close for the leg at that bar's close; 09:38 sees the leg gone.
    assert rec.bars[8][-1] == Close("S1 Long", "S1 EOD")
    trade = only(result.trades)
    assert (trade.exit_bar_index, trade.exit_price) == (8, 101.5)
    assert (trade.reason, trade.comment, trade.exit_id) == (ExitReason.CLOSE, "S1 EOD", "")
    assert rec.bars[9] == []


def test_s1_day_filter_blocks_the_whole_day() -> None:
    _, rec = run(stamped(MONDAY_0929, S1_LONG_ROWS), **S1_ONLY, s1_trade_mon=False)

    assert rec.entries() == []


# -- S2: Asian sweep ------------------------------------------------------------------------------


@BOTH_MODELS
def test_s2_short_sweep_fvg_enters_and_exits_at_the_target_repriced_from_the_fill(
    fill_model: FillModel,
) -> None:
    result, rec = run(stamped(MONDAY_2357, S2_SHORT_ROWS), fill_model, **S2_ONLY)

    assert rec.bars[8] == [
        Entry("S2 Short", Side.SHORT, 80.0, comment="S2 Short", strategy_id=S2_LEG),
        Exit("S2 ExitS", "S2 Short", stop=102.0, limit=98.25),
    ]
    assert rec.bars[9] == [Exit("S2 ExitS", "S2 Short", stop=102.0, limit=97.5)]
    trade = only(result.trades)
    assert trade.strategy_id == S2_LEG
    assert (trade.entry_bar_index, trade.entry_price, trade.qty) == (9, 100.5, 80.0)
    assert (trade.exit_bar_index, trade.exit_price, trade.reason) == (10, 97.5, ExitReason.TARGET)
    assert trade.initial_stop == 102.0
    if fill_model is FillModel.PESSIMISTIC:
        assert rec.fills[0][1] == [Exit("S2 ExitS", "S2 Short", stop=102.0, limit=97.5)]
    else:
        assert rec.fills == []


@BOTH_MODELS
def test_s2_long_is_the_mirror_of_the_short(fill_model: FillModel) -> None:
    result, _ = run(stamped(MONDAY_2357, _mirror(S2_SHORT_ROWS)), fill_model, **S2_ONLY)

    trade = only(result.trades)
    assert (trade.side, trade.entry_id, trade.qty) == (Side.LONG, "S2 Long", 80.0)
    assert (trade.entry_price, trade.initial_stop, trade.exit_price) == (99.5, 98.0, 102.5)


def test_s2_close_through_the_gap_invalidates_the_arm() -> None:
    # Bar 8 closes 101.75 > the gap's top 101.5: the arm dies before the entry check, and
    # bar 9's touch finds nothing armed.
    rows = (
        *S2_SHORT_ROWS[:8],
        (100.25, 101.75, 100.0, 101.75),
        (101.75, 101.75, 101.0, 101.25),
        (101.25, 101.5, 101.0, 101.25),
    )
    result, rec = run(stamped(MONDAY_2357, rows), **S2_ONLY)

    assert rec.entries() == []
    assert result.trades == ()
    assert rec.inner.s2_arm_dir == 0


@pytest.mark.parametrize(("end_min", "entries"), [(6, 0), (7, 1)])
def test_s2_entry_time_gate_needs_the_fill_bar_inside_the_active_window(
    end_min: int, entries: int
) -> None:
    _, rec = run(stamped(MONDAY_2357, S2_SHORT_ROWS), **{**S2_ONLY, "s2_active_end_min": end_min})

    assert len(rec.entries()) == entries


def test_s2_closes_its_leg_at_the_first_bar_after_the_active_window() -> None:
    flat = (100.5, 100.75, 100.25, 100.5)
    rows = (*S2_SHORT_ROWS[:10], *[flat] * 15)  # bars 10..24 = 00:07..00:21

    result, rec = run(stamped(MONDAY_2357, rows), **S2_ONLY)

    assert rec.bars[23][-1] == Close("S2 Short", "S2 session end")  # 00:20
    trade = only(result.trades)
    assert (trade.exit_bar_index, trade.exit_price) == (23, 100.5)
    assert (trade.reason, trade.comment) == (ExitReason.CLOSE, "S2 session end")


def test_s2_day_filter_uses_the_active_bars_own_day() -> None:
    _, rec = run(stamped(MONDAY_2357, S2_SHORT_ROWS), **S2_ONLY, s2_trade_tue=False)

    assert rec.entries() == []


# -- the portfolio: two legs, one net position ----------------------------------------


@BOTH_MODELS
def test_both_legs_open_in_the_same_direction_and_each_is_repriced_from_its_own_fill(
    fill_model: FillModel,
) -> None:
    result, rec = run(stamped(MONDAY_0919, SAME_DIRECTION_ROWS), fill_model, **COMBINED, rr=5.0)

    assert [(i, e.id, e.qty, e.strategy_id) for i, e in rec.entries()] == [
        (7, "S2 Long", 133.0, S2_LEG),
        (14, "S1 Long", 133.0, S1_LEG),
    ]
    ctx = rec.contexts[15]
    assert [t.entry_id for t in ctx.open_trades] == ["S2 Long", "S1 Long"]
    assert ctx.position_avg_price == pytest.approx(101.0)  # the blend is NOT used
    # S1 from its own fill 101.5: target 101.5 + 5 x 1.0 = 106.5 (the blend would give 103.5).
    assert Exit("S1 ExitL", "S1 Long", stop=100.5, limit=106.5) in rec.bars[15]
    assert Exit("S2 ExitL", "S2 Long", stop=99.75, limit=104.25) in rec.bars[15]
    # Bar 16 is the session's last bar: each leg gets its own close, S1 first (script order).
    assert [a for a in rec.bars[16] if isinstance(a, Close | CloseAll)] == [
        Close("S1 Long", "S1 EOD"),
        Close("S2 Long", "S2 session end"),
    ]
    by_leg = {t.strategy_id: t for t in result.trades}
    assert set(by_leg) == {S1_LEG, S2_LEG}
    assert (by_leg[S2_LEG].entry_price, by_leg[S2_LEG].initial_stop) == (100.5, 99.75)
    assert (by_leg[S1_LEG].entry_price, by_leg[S1_LEG].initial_stop) == (101.5, 100.5)
    assert by_leg[S2_LEG].pnl == pytest.approx(1.0 * 133 * 5)
    assert by_leg[S1_LEG].comment == "S1 EOD"


def test_concurrent_risk_cap_blocks_the_second_leg() -> None:
    # (0.5 if the other leg is open) + 0.5 = 1.0 > 0.9: S1 stays out, S2 alone is fine.
    result, rec = run(
        stamped(MONDAY_0919, SAME_DIRECTION_ROWS),
        **COMBINED,
        rr=5.0,
        max_concurrent_risk_pct=0.9,
    )

    assert [e.id for _, e in rec.entries()] == ["S2 Long"]
    assert [t.strategy_id for t in result.trades] == [S2_LEG]
    assert rec.inner.s1_traded_today is False


# S2 long (stop 8 ticks below its gap: 98.25, 44 contracts) stays open while S1 breaks DOWN:
# OR 101 / 100, bar 12 closes 99.5 -> short bias, bar 13 bearish FVG [99.75, 100], bar 14
# touches it -> S1 short signal (est 99.5, stop 100.5, 100 contracts).
OPPOSITE_ROWS: tuple[OHLC, ...] = (
    *SAME_DIRECTION_ROWS[:12],
    (100.5, 100.5, 99.25, 99.5),  # 12 09:31 breakdown
    (99.5, 99.75, 99.0, 99.25),  # 13 09:32 bearish FVG
    (99.25, 99.75, 99.25, 99.5),  # 14 09:33 touch
    (99.5, 99.75, 99.25, 99.5),  # 15 09:34 last bar
)


def test_opposite_entry_is_blocked_while_the_other_leg_is_open() -> None:
    params = {**COMBINED, "rr": 5.0, "s2_sl_buf": 8}

    alone, alone_rec = run(stamped(MONDAY_0919, OPPOSITE_ROWS), **params, s2_enable=False)
    both, both_rec = run(stamped(MONDAY_0919, OPPOSITE_ROWS), **params)

    # Without S2 the short is taken ...
    assert [(i, e.id, e.qty) for i, e in alone_rec.entries()] == [(14, "S1 Short", 100.0)]
    assert only(alone.trades).entry_price == 99.5
    # ... with the S2 long open it is blocked: the net position cannot hold both.
    assert [(i, e.id, e.qty) for i, e in both_rec.entries()] == [(7, "S2 Long", 44.0)]
    s2 = only(both.trades)
    assert (s2.strategy_id, s2.initial_stop, s2.comment) == (S2_LEG, 98.25, "S2 session end")


# S2 is armed long [100.25, 100.5] from bar 6 but the price runs away; S1 breaks down from an
# OR of 102.5 / 101.75. Bar 14 is BOTH S1's short touch and S2's long touch.
CONFLICT_ROWS: tuple[OHLC, ...] = (
    *S2_LONG_PREFIX,
    (100.75, 101.5, 100.75, 101.25),  # 7 09:26
    (101.25, 102.0, 101.25, 101.75),  # 8 09:27
    (101.75, 102.25, 101.5, 102.0),  # 9 09:28
    (102.0, 102.5, 101.75, 102.25),  # 10 09:29
    (102.25, 102.5, 101.75, 102.0),  # 11 09:30 opening range 102.5 / 101.75
    (102.0, 102.0, 101.25, 101.5),  # 12 09:31 short bias
    (101.5, 101.5, 100.75, 101.0),  # 13 09:32 bearish FVG [101.5, 101.75]
    (101.0, 101.5, 100.5, 100.75),  # 14 09:33 S1 short touch AND S2 long touch
    (100.75, 101.0, 100.5, 100.75),  # 15 09:34 last bar
)


@BOTH_MODELS
def test_same_bar_conflict_goes_to_s1_which_runs_first(fill_model: FillModel) -> None:
    alone, alone_rec = run(
        stamped(MONDAY_0919, CONFLICT_ROWS), fill_model, **COMBINED, s1_enable=False
    )
    both, both_rec = run(stamped(MONDAY_0919, CONFLICT_ROWS), fill_model, **COMBINED)

    # Alone, S2 takes the long: est 100.75, stop 100.25 - 2 ticks = 99.75, 100 contracts.
    assert [(i, e.id, e.qty) for i, e in alone_rec.entries()] == [(14, "S2 Long", 100.0)]
    assert only(alone.trades).initial_stop == 99.75
    # Together, S1's short (est 100.75, stop 101.75 + 2 ticks = 102.25, 66 contracts) is
    # placed first on bar 14, and S2's opposite entry is blocked by S1's working order.
    assert [(i, e.id, e.qty) for i, e in both_rec.entries()] == [(14, "S1 Short", 66.0)]
    trade = only(both.trades)
    assert (trade.strategy_id, trade.side, trade.initial_stop) == (S1_LEG, Side.SHORT, 102.25)
    # The S2 setup is untouched: still armed, its daily trade unused.
    assert both_rec.inner.s2_arm_dir == 1
    assert both_rec.inner.s2_traded_today is False


def test_combined_loss_streak_across_engines_halts_the_other_engine() -> None:
    # S2's long stops out on bar 9 (99.75); with maxConsecLoss = 1 that halts S1 as well.
    rows = (
        *SAME_DIRECTION_ROWS[:9],
        (100.5, 100.5, 99.5, 99.75),  # 9 S2 stop 99.75
        (99.75, 100.75, 99.75, 100.5),  # 10
        *SAME_DIRECTION_ROWS[11:],
    )
    params = {**COMBINED, "rr": 5.0, "max_consec_loss": 1}

    halted, halted_rec = run(stamped(MONDAY_0919, rows), **params)
    free, free_rec = run(stamped(MONDAY_0919, rows), **params, use_breakers=False)

    s2 = only(halted.trades)
    assert (s2.exit_bar_index, s2.exit_price, s2.reason) == (9, 99.75, ExitReason.STOP)
    assert halted_rec.inner.halted is True
    assert [e.id for _, e in halted_rec.entries()] == ["S2 Long"]
    # Without the breakers S1 trades the same bars.
    assert [(i, e.id) for i, e in free_rec.entries()] == [(7, "S2 Long"), (14, "S1 Long")]
    assert [t.strategy_id for t in free.trades] == [S2_LEG, S1_LEG]


def _breach_rows() -> tuple[OHLC, ...]:
    # Bar 16 closes at 100.75: S1 floats (100.75 - 101.5) x 665 = -498.75, S2 +166.25, so the
    # day is down 332.5 >= 0.3 % of 100 000 = 300; no stop is touched.
    return (*SAME_DIRECTION_ROWS[:16], (101.5, 101.5, 100.75, 100.75))


@BOTH_MODELS
def test_daily_loss_breach_flattens_both_legs_once_and_the_leg_closes_stand_down(
    fill_model: FillModel,
) -> None:
    # A bar of the previous day starts the chart, so this NY day has a start equity.
    bars = stamped(SUNDAY_2359, [(100.0, 100.5, 99.5, 100.0)]) + stamped(
        MONDAY_0919, _breach_rows()
    )

    result, rec = run(bars, fill_model, **COMBINED, rr=5.0, daily_dd_pct=0.3)

    last = rec.bars[17]  # bar 16 of the scenario, the session's last bar
    assert [a for a in last if isinstance(a, Close | CloseAll)] == [CloseAll("Daily DD halt")]
    assert len(result.trades) == 2
    for trade in result.trades:
        assert (trade.exit_bar_index, trade.exit_price) == (17, 100.75)
        assert (trade.reason, trade.comment) == (ExitReason.CLOSE, "Daily DD halt")
    assert rec.inner.g_halted_dd is True


def test_no_daily_loss_breaker_before_the_first_new_york_day_change() -> None:
    # Pine finding (mirrored): on the chart's first day g_dayStartEquity is na, so both
    # daily P&L terms are forced to 0 and the same loss does not trip the breaker.
    result, rec = run(stamped(MONDAY_0919, _breach_rows()), **COMBINED, rr=5.0, daily_dd_pct=0.3)

    assert rec.inner.g_halted_dd is False
    assert sorted(t.comment for t in result.trades) == ["S1 EOD", "S2 session end"]


def test_end_date_flattens_every_leg_with_one_close_all() -> None:
    # 09:35 New York = 14:35 UTC is after the end date: CloseAll, no per-leg closes.
    end = dt.datetime(2026, 3, 2, 14, 34, 30, tzinfo=dt.UTC)

    result, rec = run(stamped(MONDAY_0919, SAME_DIRECTION_ROWS), **COMBINED, rr=5.0, end_date=end)

    assert [a for a in rec.bars[16] if isinstance(a, Close | CloseAll)] == [
        CloseAll("End of test range")
    ]
    assert {t.strategy_id for t in result.trades} == {S1_LEG, S2_LEG}
    assert {t.comment for t in result.trades} == {"End of test range"}
    assert len(result.trades) == 2


def test_disabled_engines_trade_nothing() -> None:
    result, rec = run(
        stamped(MONDAY_0919, SAME_DIRECTION_ROWS),
        **COMBINED,
        s1_enable=False,
        s2_enable=False,
    )

    assert rec.entries() == []
    assert result.trades == ()


def test_on_fill_ignores_a_fill_that_is_not_a_pending_leg() -> None:
    strategy = MasterPortfolio()
    fill = FillEvent(entry_id="S1 Long", side=Side.LONG, qty=1.0, price=100.0, bar_index=0, ts_ms=0)
    _, rec = run(stamped(MONDAY_0929, S1_LONG_ROWS[:2]), **S1_ONLY)
    ctx = rec.contexts[1]

    assert strategy.on_fill(fill, ctx) == ()
