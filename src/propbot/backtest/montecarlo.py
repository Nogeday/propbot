"""Monte Carlo prop-challenge simulator (SPEC §6.5; modelling decisions in ADR-0002).

Units: every balance, limit and target is in % of the initial balance. A trade's
result is `r * risk_pct`; the risk base is the initial balance and never compounds
(SPEC §8.1).

Model in short: each simulated market day draws one day from the trade list with
replacement and replays its entry/exit events in time order. After every event the
lowest possible equity is `balance + worst floating result of the open trades`; a
breach is checked against that bound, a profit target only while flat.
"""

import math
from collections.abc import Callable
from dataclasses import dataclass
from enum import IntEnum
from typing import Final, Self

import numpy as np
import numpy.typing as npt

from propbot.backtest.trades import EVENT_ENTRY, EVENT_EXIT, EVENT_PAD, DayBlocks
from propbot.risk.config import MAX_RISK_PER_TRADE_PCT
from propbot.risk.venue import MaxLossType, VenueProfile

DEFAULT_PATHS: Final = 10_000
DEFAULT_MAX_DAYS: Final = 1_000
DEFAULT_DD_DAYS: Final = 250
DEFAULT_SEED: Final = 20261113

FloatArray = npt.NDArray[np.float64]
IntArray = npt.NDArray[np.intp]
DaySampler = Callable[[int], IntArray]


class SimulationInputError(Exception):
    """The simulation cannot run with these inputs. The message is user-facing (Turkish)."""


class PathStatus(IntEnum):
    RUNNING = 0  # horizon reached before any outcome
    WAITING = 1  # target reached; still completing the minimum trading days at the horizon
    PASSED = 2  # every phase passed
    BREACHED = 3


class BreachReason(IntEnum):
    NONE = 0
    DAILY_LOSS = 1
    MAX_LOSS = 2


@dataclass(frozen=True, slots=True)
class ChallengeRules:
    """The firm rules the simulator applies. Values come only from a venue profile."""

    targets_pct: tuple[float, ...]
    daily_loss_limit_pct: float
    max_loss_pct: float
    min_trading_days: int
    min_trading_days_assumed: bool = False

    def __post_init__(self) -> None:
        if not self.targets_pct or any(target <= 0.0 for target in self.targets_pct):
            raise SimulationInputError("her fazın kâr hedefi sıfırdan büyük olmalı")
        if self.daily_loss_limit_pct <= 0.0 or self.max_loss_pct <= 0.0:
            raise SimulationInputError("kayıp limitleri sıfırdan büyük olmalı")
        if self.min_trading_days < 0:
            raise SimulationInputError("minimum işlem günü negatif olamaz")

    @classmethod
    def from_venue(cls, venue: VenueProfile, *, assume_min_trading_days: int | None = None) -> Self:
        """Build the rules from a venue profile.

        `min_trading_days` is never invented: when the profile leaves it null the
        caller must state an assumption, which the report then labels as such.
        """
        if venue.max_loss_type is not MaxLossType.STATIC:
            raise SimulationInputError(
                f"max_loss_type '{venue.max_loss_type.value}' desteklenmiyor: venue profilinde "
                "gün sonu trailing formülü tanımlı değil (yalnızca 'static')"
            )
        if venue.min_trading_days is None:
            if assume_min_trading_days is None:
                raise SimulationInputError(
                    "min_trading_days venue profilinde boş (VERIFY). Değeri firmanın resmi "
                    "sayfasından doğrulayıp profile yaz ya da --assume-min-trading-days N ile "
                    "açık bir varsayım ver"
                )
            min_days, assumed = assume_min_trading_days, True
        else:
            if assume_min_trading_days not in (None, venue.min_trading_days):
                raise SimulationInputError(
                    f"venue profilinde min_trading_days = {venue.min_trading_days}; farklı bir "
                    f"varsayım ({assume_min_trading_days}) verilemez"
                )
            min_days, assumed = venue.min_trading_days, False

        targets = [venue.profit_target_pct.phase1]
        if venue.profit_target_pct.phase2 is not None:
            targets.append(venue.profit_target_pct.phase2)
        return cls(
            targets_pct=tuple(targets),
            daily_loss_limit_pct=venue.daily_loss_limit_pct,
            max_loss_pct=venue.max_loss_pct,
            min_trading_days=min_days,
            min_trading_days_assumed=assumed,
        )


@dataclass(frozen=True, slots=True)
class RiskStep:
    """While the balance is `loss_pct` or more below the start, risk is scaled by `multiplier`."""

    loss_pct: float
    multiplier: float


@dataclass(frozen=True, slots=True)
class MonteCarloParams:
    risk_pct: float
    n_paths: int = DEFAULT_PATHS
    max_days: int = DEFAULT_MAX_DAYS
    dd_days: int = DEFAULT_DD_DAYS
    seed: int = DEFAULT_SEED
    skip_prob: float = 0.0
    # Experimental comparison option, not a bot rule: it can only reduce risk.
    risk_steps: tuple[RiskStep, ...] = ()

    def __post_init__(self) -> None:
        if not (math.isfinite(self.risk_pct) and 0.0 < self.risk_pct <= MAX_RISK_PER_TRADE_PCT):
            raise SimulationInputError(
                f"risk_pct 0 ile {MAX_RISK_PER_TRADE_PCT} arasında olmalı (SPEC §8.1 kod tavanı); "
                f"verilen: {self.risk_pct}"
            )
        if self.n_paths < 1 or self.max_days < 1 or self.dd_days < 1:
            raise SimulationInputError("yol sayısı ve gün ufukları en az 1 olmalı")
        if self.seed < 0:
            raise SimulationInputError("tohum negatif olamaz")
        if not (math.isfinite(self.skip_prob) and 0.0 <= self.skip_prob <= 1.0):
            raise SimulationInputError("işlem atlama olasılığı 0 ile 1 arasında olmalı")
        previous = RiskStep(loss_pct=0.0, multiplier=1.0)
        for step in self.risk_steps:
            if not (math.isfinite(step.loss_pct) and step.loss_pct > previous.loss_pct):
                raise SimulationInputError("risk kademesi eşikleri pozitif ve artan sırada olmalı")
            if not (0.0 < step.multiplier <= previous.multiplier):
                raise SimulationInputError(
                    "risk kademesi çarpanları (0, 1] aralığında ve artmayan sırada olmalı: "
                    "kademe riski yalnızca küçültebilir (değişmez kural 4)"
                )
            previous = step


@dataclass(frozen=True, slots=True)
class PathOutcomes:
    """Raw result per simulated path."""

    n_phases: int
    status: npt.NDArray[np.int8]  # PathStatus
    phase_days: npt.NDArray[
        np.int32
    ]  # (n_paths, n_phases): market days a passed phase took, else -1
    breach_phase: IntArray  # 0-based phase of the breach, -1 if none
    breach_reason: npt.NDArray[np.int8]  # BreachReason
    max_drawdown_pct: FloatArray  # without rule stops, over `dd_days`

    @property
    def n_paths(self) -> int:
        return int(self.status.shape[0])


def simulate(
    blocks: DayBlocks,
    rules: ChallengeRules,
    params: MonteCarloParams,
    *,
    day_sampler: DaySampler | None = None,
) -> PathOutcomes:
    """Run the challenge on `params.n_paths` resampled paths.

    `day_sampler(step)` may replace the random day draw with a scripted one (tests):
    it returns the source-day index of every path for that market day.
    """
    if params.risk_steps and params.risk_steps[-1].loss_pct >= rules.max_loss_pct:
        raise SimulationInputError(
            f"risk kademesi eşikleri max_loss_pct ({rules.max_loss_pct}) değerinden küçük olmalı"
        )

    n = params.n_paths
    max_trades = blocks.max_trades
    n_events = int(blocks.event_kind.shape[1])
    n_phases = len(rules.targets_pct)
    targets = np.asarray(rules.targets_pct, dtype=np.float64)
    max_floor = -rules.max_loss_pct
    risk = params.risk_pct
    scaled = bool(params.risk_steps)
    step_losses = np.asarray([step.loss_pct for step in params.risk_steps], dtype=np.float64)
    step_multipliers = np.asarray(
        [1.0, *(step.multiplier for step in params.risk_steps)], dtype=np.float64
    )

    # Independent streams: changing skip_prob never changes which days are drawn.
    day_seed, skip_seed = np.random.SeedSequence(params.seed).spawn(2)
    day_rng = np.random.default_rng(day_seed)
    skip_rng = np.random.default_rng(skip_seed)
    path = np.arange(n, dtype=np.intp)

    # Challenge track.
    balance = np.zeros(n, dtype=np.float64)
    phase = np.zeros(n, dtype=np.intp)
    status = np.full(n, PathStatus.RUNNING, dtype=np.int8)
    breach_reason = np.full(n, BreachReason.NONE, dtype=np.int8)
    breach_phase = np.full(n, -1, dtype=np.intp)
    days_in_phase = np.zeros(n, dtype=np.int32)
    trading_days = np.zeros(n, dtype=np.int32)
    wait_left = np.zeros(n, dtype=np.int32)
    phase_days = np.full((n, n_phases), -1, dtype=np.int32)
    open_mae = np.zeros(n, dtype=np.float64)
    n_open = np.zeros(n, dtype=np.int32)
    traded = np.zeros(n, dtype=np.bool_)
    reached_target = np.zeros(n, dtype=np.bool_)
    live = np.zeros(n, dtype=np.bool_)
    daily_floor = np.zeros(n, dtype=np.float64)
    trade_risk = np.zeros((n, max_trades), dtype=np.float64)

    # Drawdown track: the same resampled days, no rule stops, no phase reset.
    free_balance = np.zeros(n, dtype=np.float64)
    free_peak = np.zeros(n, dtype=np.float64)
    free_max_dd = np.zeros(n, dtype=np.float64)
    free_open_mae = np.zeros(n, dtype=np.float64)
    free_n_open = np.zeros(n, dtype=np.int32)
    free_trade_risk = np.zeros((n, max_trades), dtype=np.float64)

    def finish_phase(done: npt.NDArray[np.bool_]) -> None:
        if not done.any():
            return
        phase_days[path[done], phase[done]] = days_in_phase[done]
        last = done & (phase == n_phases - 1)
        more = done & ~last
        status[last] = PathStatus.PASSED
        status[more] = PathStatus.RUNNING
        phase[more] += 1
        # The next phase is a fresh account at the initial balance.
        balance[more] = 0.0
        days_in_phase[more] = 0
        trading_days[more] = 0

    for step in range(max(params.max_days, params.dd_days)):
        undecided = (status == PathStatus.RUNNING) | (status == PathStatus.WAITING)
        challenge_on = step < params.max_days and bool(undecided.any())
        drawdown_on = step < params.dd_days
        if not challenge_on and not drawdown_on:
            break

        # Draws cover every path whatever its state, so the stream is outcome-independent.
        if day_sampler is not None:
            day = np.asarray(day_sampler(step), dtype=np.intp)
            if day.shape != (n,):
                raise ValueError("day_sampler must return one source-day index per path")
        else:
            day = day_rng.integers(0, blocks.n_days, size=n, dtype=np.intp)
        keep = (
            skip_rng.random((n, max_trades)) >= params.skip_prob if params.skip_prob > 0 else None
        )

        if challenge_on:
            waiting = status == PathStatus.WAITING
            if waiting.any():
                # Remaining minimum days are completed with risk-free micro trades.
                days_in_phase[waiting] += 1
                wait_left[waiting] -= 1
                finish_phase(waiting & (wait_left == 0))
            live = (status == PathStatus.RUNNING) & ~waiting
            days_in_phase[live] += 1
            # Positions are flat at the reset, so balance == equity for either daily-loss basis.
            np.subtract(balance, rules.daily_loss_limit_pct, out=daily_floor)
            open_mae[:] = 0.0
            n_open[:] = 0
            traded[:] = False
            reached_target[:] = False
        if drawdown_on:
            free_open_mae[:] = 0.0
            free_n_open[:] = 0

        kinds = blocks.event_kind[day]
        slots = blocks.event_trade[day]
        for event in range(n_events):
            kind = kinds[:, event]
            present = kind != EVENT_PAD
            if not present.any():
                break
            slot = slots[:, event]
            if keep is not None:
                present &= keep[path, slot]
            is_entry = present & (kind == EVENT_ENTRY)
            is_exit = present & (kind == EVENT_EXIT)
            result = blocks.r[day, slot]
            worst = blocks.mae[day, slot]

            if challenge_on and live.any():
                entering = is_entry & live
                exiting = is_exit & live
                event_risk: FloatArray | float = risk
                if scaled:
                    multiplier = step_multipliers[
                        np.searchsorted(step_losses, -balance, side="right")
                    ]
                    trade_risk[path[entering], slot[entering]] = risk * multiplier[entering]
                    event_risk = trade_risk[path, slot]
                floating = worst * event_risk
                open_mae += np.where(entering, floating, 0.0) - np.where(exiting, floating, 0.0)
                n_open += entering.astype(np.int32) - exiting.astype(np.int32)
                balance += np.where(exiting, result * event_risk, 0.0)
                open_mae[n_open == 0] = 0.0  # no rounding residue while flat
                traded |= entering

                low = balance + open_mae
                touched = entering | exiting
                below_daily = touched & (low <= daily_floor)
                below_max = touched & (low <= max_floor)
                breached = below_daily | below_max
                if breached.any():
                    # Falling equity crosses the higher floor first.
                    daily_first = below_daily & (~below_max | (daily_floor >= max_floor))
                    status[breached] = PathStatus.BREACHED
                    breach_reason[breached] = np.where(
                        daily_first[breached], BreachReason.DAILY_LOSS, BreachReason.MAX_LOSS
                    )
                    breach_phase[breached] = phase[breached]
                    live &= ~breached
                reached = exiting & live & (n_open == 0) & (balance >= targets[phase])
                if reached.any():
                    reached_target |= reached
                    live &= ~reached  # stop trading for the day once the target is banked

            if drawdown_on:
                free_risk: FloatArray | float = risk
                if scaled:
                    free_multiplier = step_multipliers[
                        np.searchsorted(step_losses, -free_balance, side="right")
                    ]
                    free_trade_risk[path[is_entry], slot[is_entry]] = (
                        risk * free_multiplier[is_entry]
                    )
                    free_risk = free_trade_risk[path, slot]
                free_floating = worst * free_risk
                free_open_mae += np.where(is_entry, free_floating, 0.0) - np.where(
                    is_exit, free_floating, 0.0
                )
                free_n_open += is_entry.astype(np.int32) - is_exit.astype(np.int32)
                free_balance += np.where(is_exit, result * free_risk, 0.0)
                free_open_mae[free_n_open == 0] = 0.0
                np.maximum(free_peak, free_balance, out=free_peak)
                np.maximum(free_max_dd, free_peak - (free_balance + free_open_mae), out=free_max_dd)

        if challenge_on:
            trading_days[traded] += 1
            # A path stops trading once it banks the target, so it cannot have breached since;
            # the status check keeps that invariant explicit.
            banked = reached_target & (status == PathStatus.RUNNING)
            if banked.any():
                need = np.maximum(0, rules.min_trading_days - trading_days)
                must_wait = banked & (need > 0)
                status[must_wait] = PathStatus.WAITING
                wait_left[must_wait] = need[must_wait]
                finish_phase(banked & (need == 0))

    return PathOutcomes(
        n_phases=n_phases,
        status=status,
        phase_days=phase_days,
        breach_phase=breach_phase,
        breach_reason=breach_reason,
        max_drawdown_pct=free_max_dd,
    )


@dataclass(frozen=True, slots=True)
class DurationStats:
    """Market days needed to pass, over the paths that passed."""

    n: int
    mean: float
    p25: float
    median: float
    p75: float
    p90: float
    p95: float


@dataclass(frozen=True, slots=True)
class DrawdownStats:
    """Maximum drawdown per path (% of initial balance); ES is the mean of the worst tail."""

    median: float
    q95: float
    q99: float
    es95: float
    es99: float


@dataclass(frozen=True, slots=True)
class BreachShare:
    phase: int  # 1-based
    reason: BreachReason
    probability: float


@dataclass(frozen=True, slots=True)
class MonteCarloSummary:
    n_paths: int
    p_pass_phase: tuple[float, ...]  # P(phase i passed); the last entry is P(all phases passed)
    p_breach: float
    breach_shares: tuple[BreachShare, ...]
    p_undecided: float
    time_to_pass: tuple[DurationStats | None, ...]  # cumulative days up to and including phase i
    drawdown: DrawdownStats

    @property
    def p_pass_all(self) -> float:
        return self.p_pass_phase[-1]


def summarize(outcomes: PathOutcomes) -> MonteCarloSummary:
    n = outcomes.n_paths
    passed = outcomes.phase_days >= 0
    cumulative_days = np.cumsum(np.where(passed, outcomes.phase_days, 0), axis=1)
    time_to_pass = tuple(
        _duration_stats(cumulative_days[passed[:, index], index])
        for index in range(outcomes.n_phases)
    )
    breached = outcomes.status == PathStatus.BREACHED
    shares = tuple(
        BreachShare(
            phase=index + 1,
            reason=reason,
            probability=float(
                (
                    breached & (outcomes.breach_phase == index) & (outcomes.breach_reason == reason)
                ).sum()
                / n
            ),
        )
        for index in range(outcomes.n_phases)
        for reason in (BreachReason.DAILY_LOSS, BreachReason.MAX_LOSS)
    )
    undecided = (outcomes.status == PathStatus.RUNNING) | (outcomes.status == PathStatus.WAITING)
    return MonteCarloSummary(
        n_paths=n,
        p_pass_phase=tuple(float(passed[:, index].mean()) for index in range(outcomes.n_phases)),
        p_breach=float(breached.mean()),
        breach_shares=shares,
        p_undecided=float(undecided.mean()),
        time_to_pass=time_to_pass,
        drawdown=_drawdown_stats(outcomes.max_drawdown_pct),
    )


def _duration_stats(days: npt.NDArray[np.int32]) -> DurationStats | None:
    if days.size == 0:
        return None
    p25, median, p75, p90, p95 = (float(q) for q in np.quantile(days, [0.25, 0.5, 0.75, 0.9, 0.95]))
    return DurationStats(
        n=int(days.size), mean=float(days.mean()), p25=p25, median=median, p75=p75, p90=p90, p95=p95
    )


def _drawdown_stats(max_dd: FloatArray) -> DrawdownStats:
    median, q95, q99 = (float(q) for q in np.quantile(max_dd, [0.5, 0.95, 0.99]))
    return DrawdownStats(
        median=median,
        q95=q95,
        q99=q99,
        es95=float(max_dd[max_dd >= q95].mean()),
        es99=float(max_dd[max_dd >= q99].mean()),
    )


def standard_error(probability: float, n_paths: int) -> float:
    """Monte Carlo standard error of an estimated probability."""
    return math.sqrt(probability * (1.0 - probability) / n_paths)
