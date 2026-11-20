"""The traded instrument as the strategies and the backtest engine see it.

The three numbers every size and every order level depends on: the tick (Pine
`syminfo.mintick`), the value of one full point per unit (`syminfo.pointvalue`) and the
size step. They come from `config/backtest.yaml` for a backtest and from the trading
terminal at runtime (Faz 3); nothing here is a default for a real instrument.
"""

import math
from dataclasses import dataclass
from enum import StrEnum
from typing import Final

# Float noise allowed when flooring a size to the step (0.30 / 0.01 is 29.999999999999996).
_STEP_EPSILON: Final = 1e-9


class InstrumentKind(StrEnum):
    FUTURES = "futures"  # bars are trade prices; sizes are whole contracts
    CFD = "cfd"  # bars are bid prices; buys pay the spread


@dataclass(frozen=True, slots=True)
class Instrument:
    symbol: str
    kind: InstrumentKind
    tick_size: float  # smallest price step
    point_value: float  # account currency per 1.0 of price per 1 unit of size
    qty_step: float  # size step (1 contract, 0.01 lot, ...)
    min_qty: float  # smallest size that can be traded

    def __post_init__(self) -> None:
        for name in ("tick_size", "point_value", "qty_step", "min_qty"):
            value = getattr(self, name)
            if not (isinstance(value, float | int) and 0.0 < value < float("inf")):
                raise ValueError(f"{name} must be a positive finite number")

    def floor_qty(self, qty: float) -> float:
        """Round a size DOWN to the step; 0.0 when it is below the smallest tradable size.

        SPEC §3.1: sizes are rounded down and a trade below the minimum is skipped, never
        rounded up (rounding up would exceed the risk the size was computed from).
        """
        if not (qty == qty and qty > 0.0) or qty == float("inf"):
            return 0.0
        steps = math.floor(qty / self.qty_step + _STEP_EPSILON)
        floored = steps * self.qty_step
        return floored if floored >= self.min_qty - _STEP_EPSILON else 0.0

    def round_to_tick(self, price: float) -> float:
        """The nearest price on the tick grid."""
        return round(price / self.tick_size) * self.tick_size
