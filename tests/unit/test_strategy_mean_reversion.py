"""Mean Reversion BB/KC + RSI(2) port against `pine/mean_reversion_bb_kc_rsi2_strategy.pine`.

Every expected number below comes from executing the Pine script by hand on the bar
sequence: the indicator values from the plain textbook formulas in `_ref_*` (written for
these tests, independent of `propbot.strategies.indicators`), the orders and levels from
the script's blocks. The main scenario (5-minute bars, Monday 2026-03-02, New York):

    bars  0- 9  07:10-07:55  closes 90, 91, ... 99 (keeps the 200 EMA below the trade)
    bars 10-33  08:00-09:55  closes alternating 100 / 101
    bar  34     10:00        SETUP: close 93 < BB lower 95.87, RSI(2) 7.41 < 10, no squeeze
    bar  35     10:05        CONFIRM: close 97 > BB lower 95.38, 97 >= EMA200 92.63
    bar  36     10:10        the market entry fills at the open, 97.5
    bar  37     10:15        the target (the basis at bar 36's close, 99.85) fills

Each bar opens at the previous close, high = max(open, close) + 0.25 and low =
min(open, close) - 0.25, unless the row is written out.
"""

import itertools
import math
import re
from collections.abc import Sequence
from typing import get_args

import pytest
from pydantic import ValidationError

from propbot.backtest.engine import run_backtest
from propbot.backtest.model import EngineResult, ExitReason, FillModel, TradeRecord
from propbot.core.bars import Bar, BarSeries, DataSource
from propbot.core.instrument import Instrument
from propbot.signals.schema import STRATEGY_NAMES
from propbot.strategies.base import (
    Action,
    CloseAll,
    Entry,
    Exit,
    FillEvent,
    Side,
    StrategyContext,
)
from propbot.strategies.mean_reversion import (
    PINE_FILE,
    STRATEGY_ID,
    MeanReversion,
    MeanReversionParams,
)
from support.engine import MINUTE_MS, OHLC, START_MS, cfd, make_config, mes
from support.paths import REPO_ROOT
from support.pine import pine_defaults

P = FillModel.PESSIMISTIC
TV = FillModel.TV_OHLC
BOTH = pytest.mark.parametrize("model", [P, TV])
TF = 300
BAR_MS = 5 * MINUTE_MS
# 07:10 New York: bar 34 (the setup) opens at 10:00.
SCENARIO_START = START_MS - 140 * MINUTE_MS
SETUP = 34
CONFIRM = 35
FILL = 36

# Pine inputs that change orders -> the Params field that carries them.
PORTED: dict[str, str] = {
    "bbLen": "bb_len",
    "bbMult": "bb_mult",
    "kcLen": "kc_len",
    "kcMult": "kc_mult",
    "rsiLen": "rsi_len",
    "rsiOB": "rsi_ob",
    "rsiOS": "rsi_os",
    "emaLen": "ema_len",
    "atrLen": "atr_len",
    "atrMult": "atr_mult",
    "useTrendFilter": "use_trend_filter",
    "useKcFilter": "use_kc_filter",
    "requireStrong": "require_strong",
    "allowLong": "allow_long",
    "allowShort": "allow_short",
    "riskPct": "risk_pct",
    "riskBase": "risk_base",
    "fixedBal": "fixed_bal",
    "maxDailyLoss": "max_daily_loss",
    "closeOnHalt": "close_on_halt",
    "maxTradesDay": "max_trades_day",
    "qtyRound": "qty_round",
    "tz": "tz",
    "useTimeFilter": "use_time_filter",
    "startHour": "start_hour",
    "startMin": "start_min",
    "endHour": "end_hour",
    "endMin": "end_min",
    "closeAtEnd": "close_at_end",
    "tradeMon": "trade_mon",
    "tradeTue": "trade_tue",
    "tradeWed": "trade_wed",
    "tradeThu": "trade_thu",
    "tradeFri": "trade_fri",
    "startYear": "start_year",
    "startMonth": "start_month",
    "startDay": "start_day",
    "endYear": "end_year",
    "endMonth": "end_month",
    "endDay": "end_day",
}
# Visuals, the stats table, the OOS marker and the alert token: no effect on orders.
NOT_PORTED: frozenset[str] = frozenset(
    {
        "oosYear",
        "oosMonth",
        "oosDay",
        "showDateViz",
        "showTable",
        "tableScope",
        "tablePos",
        "tableSize",
        "showBands",
        "showKc",
        "showEma",
        "showSetupBg",
        "showLevels",
        "alertToken",
    }
)


# -- reference formulas (Pine v5 reference manual), for the expected values ----------------


def _ref_sma(xs: Sequence[float], n: int) -> float:
    return sum(xs[-n:]) / n


def _ref_pstdev(xs: Sequence[float], n: int) -> float:
    window = xs[-n:]
    mean = sum(window) / n
    return math.sqrt(sum((x - mean) ** 2 for x in window) / n)


def _ref_ema(xs: Sequence[float], n: int) -> float:
    alpha = 2.0 / (n + 1)
    value = xs[0]
    for x in xs[1:]:
        value = alpha * x + (1 - alpha) * value
    return value


def _ref_rma(xs: Sequence[float], n: int) -> float:
    value = sum(xs[:n]) / n
    for x in xs[n:]:
        value = (x + (n - 1) * value) / n
    return value


def _ref_atr(rows: Sequence[OHLC], n: int) -> float:
    trs = [rows[0][1] - rows[0][2]]
    for (_, h, lo, _c), prev in zip(rows[1:], rows, strict=False):
        trs.append(max(h - lo, abs(h - prev[3]), abs(lo - prev[3])))
    return _ref_rma(trs, n)


def _ref_rsi(xs: Sequence[float], n: int) -> float:
    changes = [b - a for a, b in itertools.pairwise(xs)]
    up = _ref_rma([max(c, 0.0) for c in changes], n)
    down = _ref_rma([max(-c, 0.0) for c in changes], n)
    return 100.0 - 100.0 / (1.0 + up / down)


def _closes(rows: Sequence[OHLC]) -> list[float]:
    return [row[3] for row in rows]


# -- bar builders ---------------------------------------------------------------------------


def chain(rows: list[OHLC], closes: Sequence[float]) -> list[OHLC]:
    """Append bars that open at the previous close, with a quarter-point wick each side."""
    prev = rows[-1][3] if rows else closes[0]
    for close in closes:
        rows.append((prev, max(prev, close) + 0.25, min(prev, close) - 0.25, close))
        prev = close
    return rows


def alternating(count: int) -> list[float]:
    return [100.0 if i % 2 == 0 else 101.0 for i in range(count)]


def warm_up(*, lead: bool = True) -> list[OHLC]:
    """Bars 0-33: the ramp (keeps EMA200 low) and 24 alternating bars."""
    closes = [90.0 + i for i in range(10)] if lead else [100.0] * 10
    return chain([], closes + alternating(24))


def long_signal(rows: list[OHLC], *, confirm: OHLC = (93.0, 97.25, 92.75, 97.0)) -> list[OHLC]:
    rows.append((rows[-1][3], rows[-1][3] + 0.25, 92.75, 93.0))  # setup
    rows.append(confirm)
    return rows


def main_rows(*, fill_bar: OHLC = (97.5, 98.0, 97.25, 98.0)) -> list[OHLC]:
    rows = long_signal(warm_up())
    rows.append(fill_bar)
    rows.append((98.0, 101.0, 97.75, 100.5))  # trades through the target
    return rows


def mirrored(rows: Sequence[OHLC]) -> list[OHLC]:
    """The same bars upside down around 100: a long scenario becomes a short one."""
    return [(200.0 - o, 200.0 - lo, 200.0 - h, 200.0 - c) for o, h, lo, c in rows]


def series_of(
    rows: Sequence[OHLC], *, start_ms: int = SCENARIO_START, times: Sequence[int] | None = None
) -> BarSeries:
    stamps = times if times is not None else [start_ms + i * BAR_MS for i in range(len(rows))]
    bars = tuple(
        Bar(ts_open_ms=t, open=o, high=h, low=lo, close=c, volume=1.0)
        for t, (o, h, lo, c) in zip(stamps, rows, strict=True)
    )
    source = DataSource(name="test", format="generic", sha256="", synthetic=True, bar_tz="UTC")
    return BarSeries(symbol="TEST", tf_seconds=TF, bars=bars, source=source)


class Recorder:
    """Runs the port and keeps what it returned per bar and per fill."""

    def __init__(self, params: MeanReversionParams | None = None) -> None:
        self.inner = MeanReversion(params)
        self.bar_actions: dict[int, list[Action]] = {}
        self.fill_actions: list[tuple[FillEvent, list[Action]]] = []

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
        actions = list(self.inner.on_fill(fill, ctx))
        self.fill_actions.append((fill, actions))
        return actions

    def entries(self) -> dict[int, Entry]:
        return {
            index: action
            for index, actions in self.bar_actions.items()
            for action in actions
            if isinstance(action, Entry)
        }


def run_mr(
    rows: Sequence[OHLC] | BarSeries,
    params: MeanReversionParams | None = None,
    *,
    model: FillModel = P,
    session_last: Sequence[bool] | None = None,
    instrument: Instrument | None = None,
    start_ms: int = SCENARIO_START,
) -> tuple[EngineResult, Recorder]:
    series = rows if isinstance(rows, BarSeries) else series_of(rows, start_ms=start_ms)
    if session_last is None:
        session_last = [i == len(series) - 1 for i in range(len(series))]
    recorder = Recorder(params)
    config = make_config(instrument=instrument or mes(), fill_model=model)
    return run_backtest(series, recorder, config, session_last=session_last), recorder


def only(result: EngineResult) -> TradeRecord:
    assert len(result.trades) == 1, result.trades
    return result.trades[0]


def atr14_at(rows: Sequence[OHLC], index: int) -> float:
    return _ref_atr(rows[: index + 1], 14)


def basis_at(rows: Sequence[OHLC], index: int) -> float:
    return _ref_sma(_closes(rows[: index + 1]), 20)


# -- parameters -----------------------------------------------------------------------------


def test_every_pine_input_is_either_ported_or_deliberately_left_out() -> None:
    names = set(pine_defaults(PINE_FILE))
    assert set(PORTED) | NOT_PORTED == names
    assert not set(PORTED) & NOT_PORTED
    assert set(PORTED.values()) == set(MeanReversionParams.model_fields)


def test_param_defaults_equal_the_pine_defaults() -> None:
    defaults = pine_defaults(PINE_FILE)
    params = MeanReversionParams()
    for pine_name, field in PORTED.items():
        value = getattr(params, field)
        assert value == defaults[pine_name], pine_name
        assert type(value) is type(defaults[pine_name]), pine_name


def test_option_inputs_are_the_verbatim_pine_option_lists() -> None:
    source = (REPO_ROOT / "pine" / PINE_FILE).read_text(encoding="utf-8")

    def options(name: str) -> tuple[str, ...]:
        line = next(line for line in source.splitlines() if line.startswith(f"{name} "))
        match = re.search(r"options = \[([^\]]*)\]", line)
        assert match is not None
        return tuple(re.findall(r'"([^"]*)"', match.group(1)))

    risk_base = MeanReversionParams.model_fields["risk_base"].annotation
    qty_round = MeanReversionParams.model_fields["qty_round"].annotation
    assert get_args(risk_base.__value__) == options("riskBase")  # type: ignore[union-attr]
    assert get_args(qty_round.__value__) == options("qtyRound")  # type: ignore[union-attr]


@pytest.mark.parametrize(
    "bad",
    [
        {"bb_len": 0},
        {"bb_mult": 0.05},
        {"rsi_ob": 49.0},
        {"rsi_ob": 100.5},
        {"rsi_os": 50.5},
        {"rsi_os": -1.0},
        {"atr_mult": 0.0},
        {"risk_pct": 0.0},
        {"max_trades_day": 0},
        {"start_hour": 24},
        {"end_min": 60},
        {"start_month": 13},
        {"end_day": 32},
        {"risk_base": "Equity"},
        {"qty_round": "auto"},
        {"tz": "Mars/Olympus_Mons"},
        {"use_kc_filter": 1},
        {"unknown": 1},
    ],
)
def test_params_refuse_what_the_pine_dialog_refuses(bad: dict[str, object]) -> None:
    with pytest.raises(ValidationError):
        MeanReversionParams.model_validate(bad)


def test_module_surface() -> None:
    strategy = MeanReversion()
    assert STRATEGY_ID in STRATEGY_NAMES
    assert strategy.strategy_id == STRATEGY_ID
    assert strategy.pyramiding == 0
    assert strategy.params == MeanReversionParams()


def test_two_instances_share_no_state() -> None:
    first, second = MeanReversion(), MeanReversion()
    run_backtest(series_of(main_rows()), first, make_config(), session_last=[False] * 37 + [True])
    assert first.trades_today == 1
    assert second.trades_today == 0
    assert second.day_start_equity is None


# -- indicators and the entry pattern ------------------------------------------------------


def test_scenario_indicator_values_qualify_bar_34_as_setup_and_bar_35_as_confirmation() -> None:
    """Sanity of the hand execution itself (Pine formulas on the scenario's closes)."""
    rows = main_rows()
    closes = _closes(rows)
    setup = closes[: SETUP + 1]
    bb_lower = _ref_sma(setup, 20) - 2.5 * _ref_pstdev(setup, 20)
    bb_upper = _ref_sma(setup, 20) + 2.5 * _ref_pstdev(setup, 20)
    kc_upper = _ref_ema(setup, 20) + 1.5 * _ref_atr(rows[: SETUP + 1], 20)
    assert bb_lower == pytest.approx(95.8725153419)
    assert _ref_rsi(setup, 2) == pytest.approx(7.4074082904)
    assert closes[SETUP] < bb_lower
    assert bb_upper > kc_upper  # BB outside KC: no squeeze
    confirm = closes[: CONFIRM + 1]
    assert closes[CONFIRM] > _ref_sma(confirm, 20) - 2.5 * _ref_pstdev(confirm, 20)
    assert closes[CONFIRM] >= _ref_ema(confirm, 200) == pytest.approx(92.6320316769)


@BOTH
def test_long_trade_with_the_pine_defaults(model: FillModel) -> None:
    rows = main_rows()
    result, rec = run_mr(rows, model=model)

    dist = 2.0 * atr14_at(rows, CONFIRM)  # ATR(14) of the SIGNAL bar
    assert dist == pytest.approx(4.3291634231)
    qty = math.floor(100_000 * 0.005 / (dist * 5.0))  # 500 / 21.65 = 23.1
    assert qty == 23
    assert rec.bar_actions[CONFIRM] == [
        Entry("Long", Side.LONG, 23.0, comment="MR Long"),
        Exit("Exit L", "Long", stop=97.0 - dist, limit=max(basis_at(rows, CONFIRM), 97.25)),
    ]
    assert basis_at(rows, CONFIRM) == pytest.approx(99.95)
    assert list(rec.entries()) == [CONFIRM]

    trade = only(result)
    assert trade.entry_id == "Long"
    assert trade.setup == "MR Long"
    assert trade.side is Side.LONG
    assert trade.qty == 23.0
    assert trade.entry_bar_index == FILL
    assert trade.entry_price == 97.5  # the next bar's open, not the confirmation close
    assert trade.initial_stop == pytest.approx(97.5 - dist)  # re-priced from the fill
    assert trade.initial_stop == pytest.approx(93.1708365769)
    target = max(basis_at(rows, FILL), 97.5 + 0.25)  # the live basis at bar 36's close
    assert target == pytest.approx(99.85)
    assert trade.reason is ExitReason.TARGET
    assert trade.exit_id == "Exit L"
    assert trade.exit_bar_index == FILL + 1
    assert trade.exit_price == pytest.approx(target)
    assert trade.pnl == pytest.approx((99.85 - 97.5) * 23 * 5.0)


@BOTH
def test_short_trade_mirrors_the_long_one(model: FillModel) -> None:
    rows = mirrored(main_rows())
    result, rec = run_mr(rows, model=model)

    dist = 2.0 * atr14_at(rows, CONFIRM)
    assert dist == pytest.approx(4.3291634231)
    basis = basis_at(rows, CONFIRM)
    assert basis == pytest.approx(100.05)
    assert rec.bar_actions[CONFIRM] == [
        Entry("Short", Side.SHORT, 23.0, comment="MR Short"),
        Exit("Exit S", "Short", stop=103.0 + dist, limit=min(basis, 103.0 - 0.25)),
    ]
    trade = only(result)
    assert trade.side is Side.SHORT
    assert trade.entry_price == 102.5
    assert trade.initial_stop == pytest.approx(102.5 + dist)
    assert trade.reason is ExitReason.TARGET
    assert trade.exit_id == "Exit S"
    assert trade.exit_price == pytest.approx(min(basis_at(rows, FILL), 102.25))
    assert trade.exit_price == pytest.approx(100.15)


def test_no_signal_while_the_bands_are_still_na() -> None:
    """The same drop and recovery 10 bars after the start: BB(20) / ATR(20) are na."""
    rows = chain([], [90.0, 91.0, 92.0, *alternating(7)])
    rows = long_signal(rows)
    rows.append((97.5, 98.0, 97.25, 98.0))
    # The setup bar (index 10) opens at 10:00.
    result, rec = run_mr(
        rows, MeanReversionParams(use_trend_filter=False), start_ms=START_MS + 30 * MINUTE_MS
    )
    assert _ref_rsi(_closes(rows[:11]), 2) < 10.0  # RSI is already valid: only BB is na
    assert rec.entries() == {}
    assert result.trades == ()


def test_trend_filter_blocks_a_long_below_the_200_ema() -> None:
    rows = [*long_signal(warm_up(lead=False)), (97.5, 98.0, 97.25, 98.0)]
    assert rows[CONFIRM][3] < _ref_ema(_closes(rows[: CONFIRM + 1]), 200)
    _, rec = run_mr(rows)
    assert rec.entries() == {}
    _, rec = run_mr(rows, MeanReversionParams(use_trend_filter=False))
    assert list(rec.entries()) == [CONFIRM]


def test_trend_filter_blocks_a_short_above_the_200_ema() -> None:
    rows = mirrored([*long_signal(warm_up(lead=False)), (97.5, 98.0, 97.25, 98.0)])
    _, rec = run_mr(rows)
    assert rec.entries() == {}
    _, rec = run_mr(rows, MeanReversionParams(use_trend_filter=False))
    assert [e.id for e in rec.entries().values()] == ["Short"]


def test_squeeze_filter_rejects_a_setup_while_the_bb_is_inside_the_kc() -> None:
    """Long wicks widen the ATR (KC) but not the closes (BB): a squeeze."""
    rows: list[OHLC] = []
    for o, h, lo, c in warm_up():
        rows.append((o, h + 5.0, lo - 5.0, c))
    rows = [*long_signal(rows), (97.5, 98.0, 97.25, 98.0)]
    setup = _closes(rows[: SETUP + 1])
    bb_upper = _ref_sma(setup, 20) + 2.5 * _ref_pstdev(setup, 20)
    bb_lower = _ref_sma(setup, 20) - 2.5 * _ref_pstdev(setup, 20)
    kc_range = 1.5 * _ref_atr(rows[: SETUP + 1], 20)
    assert bb_upper < _ref_ema(setup, 20) + kc_range
    assert bb_lower > _ref_ema(setup, 20) - kc_range
    _, rec = run_mr(rows)
    assert rec.entries() == {}
    _, rec = run_mr(rows, MeanReversionParams(use_kc_filter=False))
    assert list(rec.entries()) == [CONFIRM]


def test_confirmation_must_be_the_very_next_bar() -> None:
    rows = warm_up()
    rows.append((101.0, 101.25, 92.75, 93.0))  # setup
    rows.append((93.0, 93.75, 92.75, 93.5))  # still outside, RSI 16.7: neither setup nor confirm
    rows.append((93.5, 97.25, 93.25, 97.0))  # back inside, but one bar late
    rows.append((97.0, 97.5, 96.75, 97.25))
    assert _ref_rsi(_closes(rows[: SETUP + 2]), 2) > 10.0
    _, rec = run_mr(rows)
    assert rec.entries() == {}


@pytest.mark.parametrize(
    ("confirm", "strong"),
    [
        ((93.0, 97.25, 92.75, 97.0), True),  # up candle, closes in the upper 94 %
        ((98.0, 98.25, 92.75, 97.0), False),  # down candle
        ((93.0, 102.0, 92.75, 97.0), False),  # up candle closing in the lower half
    ],
)
def test_require_strong_wants_a_rejecting_candle(confirm: OHLC, strong: bool) -> None:
    rows = [*long_signal(warm_up(), confirm=confirm), (97.5, 98.0, 97.25, 98.0)]
    _, rec = run_mr(rows, MeanReversionParams(require_strong=True))
    assert (list(rec.entries()) == [CONFIRM]) is strong
    _, rec = run_mr(rows)
    assert list(rec.entries()) == [CONFIRM]


def test_allow_long_off_drops_the_long_setup() -> None:
    _, rec = run_mr(main_rows(), MeanReversionParams(allow_long=False))
    assert rec.entries() == {}
    _, rec = run_mr(mirrored(main_rows()), MeanReversionParams(allow_short=False))
    assert rec.entries() == {}


# -- entry permission ----------------------------------------------------------------------


@pytest.mark.parametrize(
    ("shift_minutes", "enters"),
    [
        (-5, True),  # confirmation 10:00: the window opens (the setup at 09:55 needs no window)
        (-10, False),  # confirmation 09:55: before the window
        (330, True),  # confirmation 15:35: the fill bar opens 15:40 < 15:45
        (335, False),  # confirmation 15:40: the fill bar would open at 15:45 (entry-time gate)
        (340, False),  # confirmation 15:45: outside the window
    ],
)
def test_time_window_and_entry_time_gate(shift_minutes: int, enters: bool) -> None:
    rows = main_rows()
    _, rec = run_mr(rows, start_ms=SCENARIO_START + shift_minutes * MINUTE_MS)
    assert (list(rec.entries()) == [CONFIRM]) is enters


def test_time_filter_off_allows_any_time() -> None:
    _, rec = run_mr(
        main_rows(),
        MeanReversionParams(use_time_filter=False),
        start_ms=SCENARIO_START - 300 * MINUTE_MS,  # confirmation at 05:05
    )
    assert list(rec.entries()) == [CONFIRM]


def test_no_entry_on_the_sessions_last_bar() -> None:
    rows = main_rows()
    flags = [False] * len(rows)
    flags[CONFIRM] = True
    flags[-1] = True
    _, rec = run_mr(rows, session_last=flags)
    assert rec.entries() == {}


def test_disabled_weekday_blocks_entries() -> None:
    _, rec = run_mr(main_rows(), MeanReversionParams(trade_mon=False))  # 2026-03-02 is a Monday
    assert rec.entries() == {}
    _, rec = run_mr(main_rows(), MeanReversionParams(trade_tue=False))
    assert list(rec.entries()) == [CONFIRM]


def test_weekend_bars_never_enter() -> None:
    saturday = SCENARIO_START + 5 * 24 * 60 * MINUTE_MS
    _, rec = run_mr(main_rows(), start_ms=saturday)
    assert rec.entries() == {}


@pytest.mark.parametrize(
    ("dates", "enters"),
    [
        ({"start_year": 2026, "start_month": 3, "start_day": 2}, True),
        ({"start_year": 2026, "start_month": 3, "start_day": 3}, False),
        ({"end_year": 2026, "end_month": 3, "end_day": 2}, True),
        ({"end_year": 2026, "end_month": 3, "end_day": 1}, False),
        # 31 February rolls over to 3 March, as Pine's timestamp() does.
        ({"end_year": 2026, "end_month": 2, "end_day": 31}, True),
        ({"end_year": 2026, "end_month": 2, "end_day": 29}, False),  # = 1 March
    ],
)
def test_backtest_date_range(dates: dict[str, int], enters: bool) -> None:
    _, rec = run_mr(main_rows(), MeanReversionParams.model_validate(dates))
    assert (list(rec.entries()) == [CONFIRM]) is enters


def double_signal_rows() -> list[OHLC]:
    """The main trade, 20 quiet bars, then the same setup again (bars 58 / 59)."""
    rows = main_rows()
    chain(rows, alternating(20))
    rows = long_signal(rows)
    rows.append((97.5, 98.0, 97.25, 98.0))
    rows.append((98.0, 101.0, 97.75, 100.5))
    return rows


@pytest.mark.parametrize(("max_trades", "signals"), [(1, [CONFIRM]), (2, [CONFIRM, 59])])
def test_trades_per_day_cap(max_trades: int, signals: list[int]) -> None:
    rows = double_signal_rows()
    assert _ref_rsi(_closes(rows[:59]), 2) < 10.0  # bar 58 is a setup again
    result, rec = run_mr(rows, MeanReversionParams(max_trades_day=max_trades))
    assert list(rec.entries()) == signals
    assert len(result.trades) == len(signals)


def test_trade_counter_resets_on_the_next_new_york_day() -> None:
    rows = double_signal_rows()
    # Day 1: bars 0-37 from 07:10; day 2 (Tuesday): bars 38- from 08:20, so bar 58 is 10:00.
    day2 = START_MS + 24 * 60 * MINUTE_MS - 70 * MINUTE_MS
    times = [SCENARIO_START + i * BAR_MS for i in range(38)]
    times += [day2 + i * BAR_MS for i in range(len(rows) - 38)]
    flags = [False] * len(rows)
    flags[37] = True
    flags[-1] = True
    result, rec = run_mr(series_of(rows, times=times), session_last=flags)
    assert list(rec.entries()) == [CONFIRM, 59]
    assert len(result.trades) == 2


def test_quantity_below_one_contract_skips_the_trade_and_keeps_the_state() -> None:
    params = MeanReversionParams(risk_base="Fixed Balance", fixed_bal=10.0)  # 0.05 $ of risk
    result, rec = run_mr(main_rows(), params)
    assert rec.entries() == {}
    assert result.trades == ()
    assert rec.inner.trades_today == 0
    assert rec.inner.atr_stored is None


def test_fixed_balance_risk_base() -> None:
    rows = main_rows()
    _, rec = run_mr(rows, MeanReversionParams(risk_base="Fixed Balance", fixed_bal=10_000.0))
    dist = 2.0 * atr14_at(rows, CONFIRM)
    assert rec.entries()[CONFIRM].qty == math.floor(50.0 / (dist * 5.0)) == 2


def test_fractional_size_on_a_cfd() -> None:
    rows = main_rows()
    instrument = cfd(tick_size=0.25, point_value=1.0)
    _, rec = run_mr(rows, instrument=instrument)
    dist = 2.0 * atr14_at(rows, CONFIRM)
    expected = math.floor(500.0 / dist * 100.0) / 100.0  # Auto = fractional off futures
    assert rec.entries()[CONFIRM].qty == pytest.approx(expected)
    assert expected == pytest.approx(115.49)
    _, rec = run_mr(rows, MeanReversionParams(qty_round="Whole units"), instrument=instrument)
    assert rec.entries()[CONFIRM].qty == 115.0


# -- trade management -----------------------------------------------------------------------


@BOTH
def test_target_follows_the_live_basis_and_the_stop_stays_fixed(model: FillModel) -> None:
    rows = main_rows()[:-1]
    chain(rows, [98.25, 98.0, 98.5, 98.25])  # below every target
    rows.append((98.25, 98.5, 98.0, 98.25))
    _, rec = run_mr(rows, model=model)
    stop = 97.5 - 2.0 * atr14_at(rows, CONFIRM)
    for index in range(FILL, len(rows) - 1):
        exits = [a for a in rec.bar_actions[index] if isinstance(a, Exit)]
        expected_limit = max(basis_at(rows, index), 97.75)
        assert exits == [Exit("Exit L", "Long", stop=pytest.approx(stop), limit=expected_limit)]  # type: ignore[arg-type]
        assert exits[0].limit == pytest.approx(basis_at(rows, index))


def test_target_is_clamped_one_tick_above_the_fill() -> None:
    """Gap up: the fill at 100.25 is above the basis (99.95), so the target is 100.5."""
    rows = main_rows(fill_bar=(100.25, 100.5, 100.0, 100.25))
    rows[-1] = (100.25, 100.5, 100.0, 100.25)
    result, rec = run_mr(rows)
    stop = 100.25 - 2.0 * atr14_at(rows, CONFIRM)
    ((fill, fill_actions),) = rec.fill_actions
    assert fill.price == 100.25
    assert fill_actions == [Exit("Exit L", "Long", stop=stop, limit=100.5)]
    assert basis_at(rows, FILL) < 100.5
    assert rec.bar_actions[FILL] == [Exit("Exit L", "Long", stop=stop, limit=100.5)]
    trade = only(result)
    assert trade.reason is ExitReason.CLOSE  # the last bar ends the session


def test_tv_model_exits_at_the_fill_when_the_estimated_target_is_gapped() -> None:
    """Pine: the signal bar's bracket (target 99.95) works in the fill bar; a fill at 100.25
    is past it, so TradingView closes the trade at the fill bar's open (mirrored here)."""
    rows = main_rows(fill_bar=(100.25, 100.5, 100.0, 100.25))
    result, rec = run_mr(rows, model=TV)
    assert rec.fill_actions == []
    trade = only(result)
    assert trade.reason is ExitReason.TARGET
    assert trade.exit_bar_index == FILL
    assert trade.exit_price == 100.25


def test_both_fill_models_finalize_the_same_stop_only_at_different_times() -> None:
    rows = main_rows()
    stop = 97.5 - 2.0 * atr14_at(rows, CONFIRM)
    pess, rec_p = run_mr(rows, model=P)
    tv, rec_tv = run_mr(rows, model=TV)

    # Pessimistic: at the fill instant, the target from the latest basis (bar 35's).
    ((fill, actions),) = rec_p.fill_actions
    assert (fill.entry_id, fill.price, fill.bar_index) == ("Long", 97.5, FILL)
    assert actions == [Exit("Exit L", "Long", stop=stop, limit=max(basis_at(rows, CONFIRM), 97.75))]
    # TradingView model: no on_fill; the fill bar's close finalizes the same stop.
    assert rec_tv.fill_actions == []
    assert rec_tv.bar_actions[FILL] == [
        Exit("Exit L", "Long", stop=stop, limit=max(basis_at(rows, FILL), 97.75))
    ]
    assert rec_p.bar_actions[FILL] == rec_tv.bar_actions[FILL]
    assert only(pess).initial_stop == only(tv).initial_stop == stop
    assert only(pess) == only(tv)


@BOTH
def test_stop_fills_at_the_fixed_atr_stop(model: FillModel) -> None:
    rows = main_rows()[:-1]
    rows.append((98.0, 98.25, 92.0, 92.5))
    result, _ = run_mr(rows, model=model)
    trade = only(result)
    stop = 97.5 - 2.0 * atr14_at(rows, CONFIRM)
    assert trade.reason is ExitReason.STOP
    assert trade.exit_price == pytest.approx(stop)
    assert trade.pnl == pytest.approx((stop - 97.5) * 23 * 5.0)


# -- forced exits ---------------------------------------------------------------------------


@BOTH
def test_session_end_at_15_45_closes_at_that_bars_close(model: FillModel) -> None:
    rows = main_rows()
    rows[-1] = (98.0, 98.5, 97.75, 98.25)  # 15:45: no target
    rows.append((98.25, 98.5, 98.0, 98.25))
    result, rec = run_mr(rows, model=model, start_ms=SCENARIO_START + 330 * MINUTE_MS)
    assert list(rec.entries()) == [CONFIRM]
    assert CloseAll(comment="Session end") in rec.bar_actions[FILL + 1]
    trade = only(result)
    assert trade.reason is ExitReason.CLOSE
    assert trade.comment == "Session end"
    assert trade.exit_bar_index == FILL + 1
    assert trade.exit_price == 98.25


@BOTH
def test_session_last_bar_closes_the_position(model: FillModel) -> None:
    rows = main_rows()
    rows[-1] = (98.0, 98.5, 97.75, 98.25)
    rows.append((98.25, 98.5, 98.0, 98.25))
    flags = [False] * len(rows)
    flags[FILL + 1] = True
    flags[-1] = True
    result, _ = run_mr(rows, model=model, session_last=flags)
    trade = only(result)
    assert (trade.reason, trade.comment, trade.exit_bar_index) == (
        ExitReason.CLOSE,
        "Session end",
        FILL + 1,
    )


def test_close_at_end_off_keeps_the_position() -> None:
    rows = main_rows()
    rows[-1] = (98.0, 98.5, 97.75, 98.25)
    rows.append((98.25, 98.5, 98.0, 98.25))
    result, _ = run_mr(
        rows, MeanReversionParams(close_at_end=False), start_ms=SCENARIO_START + 330 * MINUTE_MS
    )
    assert only(result).reason is ExitReason.END_OF_DATA


@BOTH
def test_daily_breaker_flattens_and_blocks_the_day(model: FillModel) -> None:
    """max daily loss 0.1 % = 100 $: bar 36 closes 1 point under the fill, 23 x 5 = 115 $."""
    rows = main_rows(fill_bar=(97.5, 97.75, 96.25, 96.5))
    result, rec = run_mr(rows, MeanReversionParams(max_daily_loss=0.1), model=model)
    assert rec.bar_actions[FILL][-1] == CloseAll(comment="Daily loss halt")
    trade = only(result)
    assert (trade.reason, trade.comment, trade.exit_price) == (
        ExitReason.CLOSE,
        "Daily loss halt",
        96.5,
    )
    assert rec.inner.daily_halt


def test_daily_breaker_without_close_on_halt_only_blocks_new_entries() -> None:
    rows = double_signal_rows()
    rows[FILL] = (97.5, 97.75, 96.25, 96.5)
    params = MeanReversionParams(max_daily_loss=0.1, close_on_halt=False, max_trades_day=2)
    result, rec = run_mr(rows, params)
    assert list(rec.entries()) == [CONFIRM]  # bar 59's signal comes after the halt
    assert only(result).reason is ExitReason.TARGET


def test_end_of_range_closes_after_the_last_day() -> None:
    """Time filter off; the fill bar is 23:55 on 2 March and the next bar opens 00:00."""
    rows = main_rows()
    rows[-1] = (98.0, 98.5, 97.75, 98.25)
    rows.append((98.25, 98.5, 98.0, 98.25))
    start = SCENARIO_START + (23 * 60 + 45 - 600) * MINUTE_MS  # setup 23:45
    params = MeanReversionParams(use_time_filter=False, end_year=2026, end_month=3, end_day=2)
    result, rec = run_mr(rows, params, start_ms=start)
    assert list(rec.entries()) == [CONFIRM]
    trade = only(result)
    assert (trade.reason, trade.comment, trade.exit_bar_index) == (
        ExitReason.CLOSE,
        "End of range",
        FILL + 1,
    )


def test_one_reason_per_bar_session_end_first() -> None:
    rows = main_rows(fill_bar=(97.5, 97.75, 96.25, 96.5))
    flags = [False] * len(rows)
    flags[FILL] = True
    flags[-1] = True
    _, rec = run_mr(rows, MeanReversionParams(max_daily_loss=0.1), session_last=flags)
    closes = [a for a in rec.bar_actions[FILL] if isinstance(a, CloseAll)]
    assert closes == [CloseAll(comment="Session end")]


def test_state_resets_once_flat() -> None:
    result, rec = run_mr(main_rows())
    assert only(result).exit_bar_index == FILL + 1
    assert (rec.inner.in_dir, rec.inner.in_stop, rec.inner.atr_stored) == (0, None, None)
