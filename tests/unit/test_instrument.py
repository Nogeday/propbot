"""Instrument numbers and size rounding (`propbot.core.instrument`)."""

import pytest

from propbot.core.instrument import Instrument, InstrumentKind


def mes(qty_step: float = 1.0, min_qty: float = 1.0) -> Instrument:
    return Instrument(
        symbol="MES",
        kind=InstrumentKind.FUTURES,
        tick_size=0.25,
        point_value=5.0,
        qty_step=qty_step,
        min_qty=min_qty,
    )


def test_kinds_have_stable_values() -> None:
    assert InstrumentKind("futures") is InstrumentKind.FUTURES
    assert InstrumentKind("cfd") is InstrumentKind.CFD


@pytest.mark.parametrize("field", ["tick_size", "point_value", "qty_step", "min_qty"])
@pytest.mark.parametrize("value", [0.0, -1.0, float("nan"), float("inf")])
def test_numbers_must_be_positive_and_finite(field: str, value: float) -> None:
    values = {"tick_size": 0.25, "point_value": 5.0, "qty_step": 1.0, "min_qty": 1.0}
    values[field] = value
    with pytest.raises(ValueError, match=field):
        Instrument(symbol="X", kind=InstrumentKind.CFD, **values)


@pytest.mark.parametrize(
    ("qty", "expected"), [(1.0, 1.0), (1.99, 1.0), (2.0, 2.0), (7.5, 7.0), (0.99, 0.0)]
)
def test_whole_contracts_are_floored_never_rounded_up(qty: float, expected: float) -> None:
    assert mes().floor_qty(qty) == expected


def test_lot_step_survives_float_noise() -> None:
    lots = mes(qty_step=0.01, min_qty=0.01)
    assert lots.floor_qty(0.30) == pytest.approx(0.30)
    assert lots.floor_qty(0.30) <= 0.30 + 1e-12
    assert lots.floor_qty(0.009) == 0.0


def test_size_below_the_minimum_is_skipped() -> None:
    assert mes(qty_step=0.1, min_qty=0.5).floor_qty(0.45) == 0.0
    assert mes(qty_step=0.1, min_qty=0.5).floor_qty(0.5) == pytest.approx(0.5)


@pytest.mark.parametrize("qty", [0.0, -1.0, float("nan"), float("inf"), -float("inf")])
def test_unusable_sizes_floor_to_zero(qty: float) -> None:
    assert mes().floor_qty(qty) == 0.0


@pytest.mark.parametrize(
    ("price", "expected"), [(5000.1, 5000.0), (5000.13, 5000.25), (5000.375, 5000.5)]
)
def test_round_to_tick_picks_the_nearest_grid_price(price: float, expected: float) -> None:
    assert mes().round_to_tick(price) == pytest.approx(expected)
