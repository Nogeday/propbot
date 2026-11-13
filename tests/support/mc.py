"""Builders for Monte Carlo tests: hand-made day pools, scripted day sequences, analytic values."""

import datetime as dt
from collections.abc import Sequence
from fractions import Fraction
from zoneinfo import ZoneInfo

import numpy as np

from propbot.backtest.montecarlo import ChallengeRules, DaySampler
from propbot.backtest.trades import DayBlocks, IntratradeModel, Trade, build_day_blocks
from propbot.core.propday import weekdays_between

PRAGUE = ZoneInfo("Europe/Prague")
MIDNIGHT = dt.time(0, 0)
WEEKDAYS = weekdays_between(dt.date(2026, 1, 5), dt.date(2026, 6, 30))

# One trade spec is (start_minute, end_minute, r) or (start_minute, end_minute, r, mae_r);
# minutes count from 12:00 UTC, which keeps every trade inside one Prague day.
TradeSpec = tuple[float, ...]
WIN: list[TradeSpec] = [(0, 60, 2.0)]
LOSS: list[TradeSpec] = [(0, 60, -1.0)]
EMPTY: list[TradeSpec] = []


def make_blocks(
    days: Sequence[Sequence[TradeSpec]],
    *,
    cost_r: float = 0.0,
    intratrade: IntratradeModel = IntratradeModel.STOP,
) -> DayBlocks:
    """Build a day pool where pool index i holds the trades of `days[i]`."""
    trades: list[Trade] = []
    for index, specs in enumerate(days):
        noon = dt.datetime.combine(WEEKDAYS[index], dt.time(12, 0), tzinfo=dt.UTC)
        for start, end, r, *rest in specs:
            trades.append(
                Trade(
                    entry_utc=noon + dt.timedelta(minutes=start),
                    exit_utc=noon + dt.timedelta(minutes=end),
                    r=r,
                    mae_r=rest[0] if rest else None,
                )
            )
    blocks = build_day_blocks(
        trades, zone=PRAGUE, reset=MIDNIGHT, cost_r=cost_r, intratrade=intratrade
    )
    # A leading or trailing empty day would silently drop out of the pool.
    assert blocks.n_days == len(days), "first and last day of a test pool need a trade"
    return blocks


def scripted(sequence: Sequence[int], n_paths: int = 1) -> DaySampler:
    """Day sampler that plays `sequence` (pool indices) on every path."""

    def sampler(step: int) -> np.ndarray:
        return np.full(n_paths, sequence[step], dtype=np.intp)

    return sampler


def rules(
    targets: tuple[float, ...] = (10.0, 5.0),
    *,
    daily: float = 5.0,
    total: float = 10.0,
    min_days: int = 0,
) -> ChallengeRules:
    return ChallengeRules(
        targets_pct=targets,
        daily_loss_limit_pct=daily,
        max_loss_pct=total,
        min_trading_days=min_days,
    )


def barrier_probability(lower: int, upper: int) -> float:
    """P(reach >= +upper before <= -lower) for steps +2 (prob 1/3) and -1 (prob 2/3) from 0.

    The walk is a martingale on the integers: its characteristic equation
    z^3 - 3z + 2 = (z - 1)^2 (z + 2) gives h(x) = A + B x + C (-2)^x, fixed by
    h(-lower) = 0 and h(upper) = h(upper + 1) = 1 (a +2 step can overshoot by one).
    """
    up = Fraction(-2) ** upper
    down = 1 / Fraction(-2) ** lower
    return float((3 * lower * up + 1 - down) / ((3 * (lower + upper) + 1) * up - down))


def barrier_probability_by_linear_system(lower: int, upper: int) -> float:
    """The same probability from the absorbing-chain equations, as an independent check."""
    states = list(range(-lower, upper + 2))
    size = len(states)
    matrix = np.zeros((size, size))
    target = np.zeros(size)
    for row, state in enumerate(states):
        matrix[row, row] = 1.0
        if state >= upper:
            target[row] = 1.0
        elif state > -lower:
            matrix[row, row + 2] -= 1.0 / 3.0
            matrix[row, row - 1] -= 2.0 / 3.0
    return float(np.linalg.solve(matrix, target)[states.index(0)])
