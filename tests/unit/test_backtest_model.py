"""Shared backtest types: cost model, spread windows, engine config, grouping by entry."""

import math

import pytest

from propbot.backtest.model import (
    CostModel,
    EngineConfig,
    ExitReason,
    SpreadWindow,
    TradeRecord,
    group_by_entry,
)
from propbot.strategies.base import Side
from support.engine import mes


def _costs(**overrides: object) -> CostModel:
    values: dict[str, object] = {"commission_per_unit_side": 0.62, "slippage_ticks": 1.0}
    values.update(overrides)
    return CostModel(**values)  # type: ignore[arg-type]


def test_scaled_multiplies_every_cost() -> None:
    costs = _costs(
        spread_points=0.4,
        spread_windows=(SpreadWindow(570, 600, 1.0), SpreadWindow(960, 965, 0.8)),
    )

    scaled = costs.scaled(1.5)

    assert scaled.commission_per_unit_side == pytest.approx(0.93)
    assert scaled.slippage_ticks == 1.5
    assert scaled.spread_points == pytest.approx(0.6)
    assert [w.spread_points for w in scaled.spread_windows] == pytest.approx([1.5, 1.2])
    assert [(w.start_minute, w.end_minute) for w in scaled.spread_windows] == [
        (570, 600),
        (960, 965),
    ]
    assert scaled.spread_tz == costs.spread_tz
    assert costs.scaled(1.0) == costs


@pytest.mark.parametrize("factor", [-0.5, math.nan, math.inf])
def test_scaled_rejects_a_bad_factor(factor: float) -> None:
    with pytest.raises(ValueError, match="factor"):
        _costs().scaled(factor)


@pytest.mark.parametrize("field", ["commission_per_unit_side", "slippage_ticks", "spread_points"])
@pytest.mark.parametrize("value", [-0.01, math.nan, math.inf])
def test_cost_model_rejects_negative_or_non_finite_costs(field: str, value: float) -> None:
    with pytest.raises(ValueError, match=field):
        _costs(**{field: value})


def test_spread_at_uses_the_first_matching_window_else_the_base_spread() -> None:
    costs = _costs(
        spread_points=0.5,
        spread_windows=(SpreadWindow(570, 575, 2.0), SpreadWindow(560, 600, 1.0)),
    )
    assert costs.spread_at(569) == 1.0
    assert costs.spread_at(570) == 2.0
    assert costs.spread_at(574) == 2.0
    assert costs.spread_at(575) == 1.0  # the end is exclusive
    assert costs.spread_at(600) == 0.5
    assert costs.spread_at(0) == 0.5


def test_spread_window_may_wrap_midnight() -> None:
    window = SpreadWindow(start_minute=1430, end_minute=10, spread_points=3.0)
    assert [window.contains(m) for m in (1429, 1430, 1439, 0, 9, 10)] == [
        False,
        True,
        True,
        True,
        True,
        False,
    ]
    assert _costs(spread_windows=(window,)).spread_at(5) == 3.0


@pytest.mark.parametrize(
    ("start", "end", "spread", "message"),
    [
        (-1, 10, 1.0, "start_minute"),
        (0, 1440, 1.0, "end_minute"),
        (100, 100, 1.0, "empty"),
        (100, 200, -1.0, "spread_points"),
    ],
)
def test_spread_window_validation(start: int, end: int, spread: float, message: str) -> None:
    with pytest.raises(ValueError, match=message):
        SpreadWindow(start, end, spread)


def test_is_free_only_when_no_cost_anywhere() -> None:
    free = CostModel(commission_per_unit_side=0.0, slippage_ticks=0.0)
    assert free.is_free
    assert not _costs().is_free
    assert not CostModel(0.0, 0.0, spread_points=0.1).is_free
    assert not CostModel(0.0, 0.0, spread_windows=(SpreadWindow(570, 575, 0.5),)).is_free
    assert CostModel(0.0, 0.0, spread_windows=(SpreadWindow(570, 575, 0.0),)).is_free


@pytest.mark.parametrize("capital", [0.0, -1.0, math.nan, math.inf])
def test_engine_config_requires_positive_finite_capital(capital: float) -> None:
    with pytest.raises(ValueError, match="initial_capital"):
        EngineConfig(instrument=mes(), costs=_costs(), initial_capital=capital)


def test_engine_config_rejects_negative_through_ticks() -> None:
    with pytest.raises(ValueError, match="limit_through_ticks"):
        EngineConfig(instrument=mes(), costs=_costs(), limit_through_ticks=-1)
    config = EngineConfig(instrument=mes(), costs=_costs(), limit_through_ticks=0)
    assert config.limit_through_ticks == 0


def _row(
    seq: int,
    *,
    pnl: float,
    risk: float,
    mae_r: float,
    qty: float = 1.0,
    reason: ExitReason = ExitReason.STOP,
    exit_ts: int = 10,
) -> TradeRecord:
    return TradeRecord(
        seq=seq,
        strategy_id="s",
        entry_id=f"e{seq}",
        exit_id="x",
        reason=reason,
        setup="FVG",
        comment="",
        side=Side.LONG,
        qty=qty,
        entry_bar_index=0,
        exit_bar_index=1,
        entry_ts_ms=1,
        exit_ts_ms=exit_ts,
        entry_price=100.0,
        exit_price=101.0,
        gross_pnl=pnl,
        commission=0.0,
        pnl=pnl,
        initial_stop=99.0,
        risk_amount=risk,
        r=pnl / risk,
        mae_r=mae_r,
    )


def test_group_by_entry_adds_rows_of_one_entry_in_seq_order() -> None:
    rows = [
        _row(1, pnl=-10.0, risk=10.0, mae_r=-1.0),
        _row(0, pnl=30.0, risk=20.0, mae_r=-0.5, reason=ExitReason.TARGET, exit_ts=20),
        _row(0, pnl=0.0, risk=20.0, mae_r=-0.25, exit_ts=30),
    ]

    first, second = group_by_entry(rows)

    assert (first.seq, first.qty, first.pnl, first.risk_amount) == (0, 2.0, 30.0, 40.0)
    assert first.r == 0.75
    # Size-weighted: (-0.5 * 20 + -0.25 * 20) / 40.
    assert first.mae_r == -0.375
    assert first.reasons == (ExitReason.TARGET, ExitReason.STOP)
    assert (first.entry_ts_ms, first.exit_ts_ms, first.setup) == (1, 30, "FVG")
    assert first.complete
    assert (second.seq, second.r, second.mae_r) == (1, -1.0, -1.0)


def test_group_by_entry_clamps_mae_and_flags_end_of_data() -> None:
    rows = [
        _row(0, pnl=-5.0, risk=10.0, mae_r=0.0),
        _row(0, pnl=1.0, risk=10.0, mae_r=0.0, reason=ExitReason.END_OF_DATA),
    ]
    (entry,) = group_by_entry(rows)
    assert entry.mae_r == -0.2  # never above the result
    assert not entry.complete
    assert group_by_entry([]) == ()
