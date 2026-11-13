"""Signal processor: decide what the trader is told about each queued alert (SPEC §5.2, §8.1).

One transaction takes the oldest queued signal and writes its outcome, its budget entry and
its outbox rows together, so a signal is processed exactly once. Nothing here places,
changes or cancels an order: the only output is text for the one whitelisted chat.

The decision is a pure function (`decide`: signal + context -> `Decision`), `process_one`
reads the context from the store and applies the decision.

Entries, first match wins:

    0. arrived more than 2 s before its bar close   SKIP_CLOCK (clock doubt = no new entry)
    1. KILLED                                       SUPPRESSED_KILLED (silent)
    2. PAUSED                                       SUPPRESSED_PAUSED (silent)
    3. older than the validity limit                STALE
    4. minimum lot risks more than the budget       SKIP_RISK_TOO_WIDE
    5. daily budget cannot take it                  SKIP_BUDGET
    6. otherwise                                    FORWARDED, risk counted in the ledger

Stop updates, exits and cancels reduce risk and are never held back by pause or kill. They
are matched to the latest entry of the same strategy and symbol; a stop update is judged in
the trader's frame (offset = stop - entry price), because the trader holds the position on
another instrument at another fill price. A stop is never widened (CLAUDE.md invariant 2).
"""

import asyncio
import datetime as dt
from collections.abc import Callable
from dataclasses import dataclass
from decimal import Decimal
from typing import Final

import structlog

from propbot.core.clock import Clock, utc_from_ms
from propbot.core.propday import prop_day
from propbot.notify import messages
from propbot.notify.messages import TradeLink
from propbot.risk.config import RiskConfig
from propbot.risk.rules import daily_budget_amount
from propbot.risk.sizing import SizeSkip, suggest_size
from propbot.risk.venue import VenueProfile, rules_freshness
from propbot.signals.schema import BAR_TIME_MAX_MS, Action, Side
from propbot.store.db import (
    BudgetRow,
    OutboxKind,
    Outcome,
    ReleaseReason,
    SignalRow,
    SignalStatus,
    StateKey,
    Store,
    Tx,
)

_MILLIS_PER_SECOND: Final = 1000
_MINUTE_MS: Final = 60 * _MILLIS_PER_SECOND
_DAY_MS: Final = 24 * 60 * _MINUTE_MS

# An entry this much ahead of its computed bar close means the two clocks disagree.
EARLY_TOLERANCE_MS: Final = 2 * _MILLIS_PER_SECOND
# Follow-ups are matched to an entry at most this far back.
REFERENCE_LOOKBACK_MS: Final = _DAY_MS
# Float slack of the budget comparison (account currency).
BUDGET_EPSILON: Final = 1e-9

RULES_STALE_ALARM: Final = ("rules_stale", _DAY_MS)
SIDE_MISMATCH_ALARM: Final = ("side_mismatch", 10 * _MINUTE_MS)

_FLAG_CLEAR: Final = "0"
# "After the reference" has no upper bound: an out-of-order alert is compared with the newest
# state that was already sent.
_NO_UPPER_BOUND_MS: Final = BAR_TIME_MAX_MS - 1

_log = structlog.get_logger(__name__)


class SignalDataError(ValueError):
    """A stored signal lacks something its action needs (the schema should have refused it)."""


@dataclass(frozen=True, slots=True)
class ProcessorConfig:
    risk: RiskConfig
    venue: VenueProfile


@dataclass(frozen=True, slots=True, kw_only=True)
class DecisionContext:
    """Everything a decision depends on besides the signal and the config (plain data)."""

    now_ms: int
    # Entries only.
    killed: bool = False
    paused: bool = False
    budget_used: float = 0.0  # counted risk of the current prop day
    # Stop updates, exits and cancels only.
    reference: SignalRow | None = None  # latest entry of the strategy + symbol, any outcome
    reference_budget: BudgetRow | None = None
    exit_after_reference: bool = False  # a forwarded exit exists after the reference
    # Latest forwarded stop update after the reference (without one: of the last 24 h).
    last_stop_update: SignalRow | None = None


@dataclass(frozen=True, slots=True)
class OutboxDraft:
    text: str
    kind: OutboxKind = OutboxKind.SIGNAL
    expires_at_ms: int | None = None
    expired_text: str | None = None


@dataclass(frozen=True, slots=True)
class AlarmDraft:
    code: str
    text: str
    throttle_ms: int


@dataclass(frozen=True, slots=True)
class Decision:
    """What happens to one signal: its outcome and everything that is written with it."""

    outcome: Outcome
    messages: tuple[OutboxDraft, ...] = ()
    alarms: tuple[AlarmDraft, ...] = ()
    budget_risk: float | None = None  # counted in the daily ledger when set


# --------------------------------------------------------------------------------------------
# Pure decision
# --------------------------------------------------------------------------------------------


def _exact(value: float | None, name: str) -> Decimal:
    """A stored price as the exact decimal it prints as (binary floats cannot compare ticks)."""
    if value is None:
        raise SignalDataError(f"signal has no {name}")
    return Decimal(str(value))


def _stop_offset(row: SignalRow) -> Decimal:
    """Signed distance from the row's entry price to its stop: the trader's frame."""
    return _exact(row.stop, "stop") - _exact(row.est_entry, "est_entry")


def _side(row: SignalRow) -> Side:
    try:
        return Side(row.side or "")
    except ValueError:
        raise SignalDataError("signal has no usable side") from None


def _late_seconds(signal: SignalRow, now_ms: int, validity_ms: int) -> float | None:
    """Age of a follow-up beyond its bar close, or None while it is within the validity limit.

    A bar cut short by the session end closes before its computed close, hence the `min`.
    """
    effective_close_ms = min(signal.bar_close_ms, signal.received_at_ms)
    if now_ms > effective_close_ms + validity_ms:
        return (now_ms - effective_close_ms) / _MILLIS_PER_SECOND
    return None


def _decide_entry(signal: SignalRow, context: DecisionContext, config: ProcessorConfig) -> Decision:
    risk, venue = config.risk, config.venue
    now_ms = context.now_ms
    validity_ms = risk.signal_validity_seconds * _MILLIS_PER_SECOND

    early_ms = signal.bar_close_ms - signal.received_at_ms
    if early_ms > EARLY_TOLERANCE_MS:
        text = messages.entry_clock_skip_notice(signal, early_seconds=early_ms / _MILLIS_PER_SECOND)
        return Decision(Outcome.SKIP_CLOCK, (OutboxDraft(text),))
    if context.killed:
        return Decision(Outcome.SUPPRESSED_KILLED)
    if context.paused:
        return Decision(Outcome.SUPPRESSED_PAUSED)

    # Entries never fire on session-shortened bars: the computed bar close is the truth.
    valid_until_ms = signal.bar_close_ms + validity_ms
    if now_ms > valid_until_ms:
        text = messages.entry_stale_notice(
            signal,
            delay_seconds=(now_ms - signal.bar_close_ms) / _MILLIS_PER_SECOND,
            validity_seconds=risk.signal_validity_seconds,
        )
        return Decision(Outcome.STALE, (OutboxDraft(text),))

    stop_points = float(abs(_stop_offset(signal)))
    size = suggest_size(stop_points=stop_points, risk=risk, venue=venue)
    if size.skip is SizeSkip.RISK_TOO_WIDE:
        text = messages.entry_risk_too_wide_notice(
            signal, venue=venue, size=size, stop_points=stop_points
        )
        return Decision(Outcome.SKIP_RISK_TOO_WIDE, (OutboxDraft(text),))
    if size.skip is SizeSkip.CONTRACT_UNKNOWN:
        # No lot size can be suggested: the whole per-trade budget is assumed to be at risk.
        risk_new = size.risk_budget
    elif size.skip is None and size.lots is not None and size.risk_at_lots is not None:
        risk_new = size.risk_at_lots
    else:
        raise SignalDataError("size suggestion is inconsistent")

    cap = daily_budget_amount(risk, venue)
    if context.budget_used + risk_new > cap + BUDGET_EPSILON:
        text = messages.entry_budget_skip_notice(
            signal,
            venue=venue,
            budget_used=context.budget_used,
            risk_amount=risk_new,
            budget_cap=cap,
        )
        return Decision(Outcome.SKIP_BUDGET, (OutboxDraft(text),))

    freshness = rules_freshness(venue, utc_from_ms(now_ms).date())
    rules_unverified = freshness.order_modes_locked
    text = messages.entry_signal(
        signal,
        risk=risk,
        venue=venue,
        size=size,
        risk_amount=risk_new,
        budget_used_after=context.budget_used + risk_new,
        budget_cap=cap,
        valid_until_ms=valid_until_ms,
        rules_unverified=rules_unverified,
    )
    draft = OutboxDraft(
        text,
        expires_at_ms=valid_until_ms,
        expired_text=messages.entry_expired_notice(signal, valid_until_ms=valid_until_ms),
    )
    alarms: tuple[AlarmDraft, ...] = ()
    if rules_unverified:
        code, throttle_ms = RULES_STALE_ALARM
        alarms = (AlarmDraft(code, messages.rules_stale_alarm(venue, freshness), throttle_ms),)
    return Decision(Outcome.FORWARDED, (draft,), alarms, budget_risk=risk_new)


def _decide_stop_update(
    signal: SignalRow,
    side: Side,
    context: DecisionContext,
    config: ProcessorConfig,
    *,
    undelivered: bool,
    late_seconds: float | None,
) -> Decision:
    venue = config.venue
    reference = context.reference
    if reference is not None and reference.side != signal.side:
        code, throttle_ms = SIDE_MISMATCH_ALARM
        alarm = AlarmDraft(code, messages.side_mismatch_alarm(signal, reference), throttle_ms)
        return Decision(Outcome.NO_ACTIVE_TRADE, alarms=(alarm,))

    last = context.last_stop_update
    if last is not None:
        same_trade = last.side == signal.side
        # A newer update was already sent. With a known entry that settles it whatever the
        # side (a newer trade has taken over); without one, only the same side is this trade.
        if last.bar_time_ms > signal.bar_time_ms and (reference is not None or same_trade):
            return Decision(Outcome.SUPERSEDED)
        if not same_trade:
            last = None  # another trade's stop: nothing to compare offsets with

    link = TradeLink(
        entry_id=None if reference is None else reference.id,
        entry_undelivered=undelivered,
        late_seconds=late_seconds,
    )
    new_offset = _stop_offset(signal)
    if last is not None:
        current_offset = _stop_offset(last)
    elif reference is not None:
        current_offset = _stop_offset(reference)
    else:
        # Nothing to compare with: the user decides against the stop they really hold.
        text = messages.stop_update_signal(signal, venue=venue, link=link, current_offset=None)
        return Decision(Outcome.FORWARDED, (OutboxDraft(text),))

    gain = new_offset - current_offset if side is Side.LONG else current_offset - new_offset
    if gain < 0:
        text = messages.stop_widen_warning(
            signal, venue=venue, link=link, current_offset=float(current_offset)
        )
        return Decision(Outcome.REJECTED_WIDEN, (OutboxDraft(text),))
    if gain == 0:
        return Decision(Outcome.FILTERED_MIN_STEP)
    tick_size = venue.contract.tick_size
    if tick_size is not None:
        # CLAUDE.md invariant 8: a trail step below `trail_min_step_ticks` is not passed on.
        min_step = config.risk.trail_min_step_ticks * _exact(tick_size, "tick_size")
        if gain < min_step:
            return Decision(Outcome.FILTERED_MIN_STEP)
    text = messages.stop_update_signal(
        signal, venue=venue, link=link, current_offset=float(current_offset)
    )
    return Decision(Outcome.FORWARDED, (OutboxDraft(text),))


def _decide_follow_up(
    signal: SignalRow, action: Action, context: DecisionContext, config: ProcessorConfig
) -> Decision:
    side = _side(signal)
    venue = config.venue
    reference = context.reference
    budget = context.reference_budget
    if reference is not None:
        if reference.outcome is not Outcome.FORWARDED:
            return Decision(Outcome.NO_ACTIVE_TRADE)
        if context.exit_after_reference:
            return Decision(Outcome.NO_ACTIVE_TRADE)
        if budget is not None and budget.release_reason is ReleaseReason.SKIPPED:
            return Decision(Outcome.NO_ACTIVE_TRADE)
    undelivered = budget is not None and budget.release_reason is ReleaseReason.UNDELIVERED
    validity_ms = config.risk.signal_validity_seconds * _MILLIS_PER_SECOND
    late_seconds = _late_seconds(signal, context.now_ms, validity_ms)

    if action is Action.STOP_UPDATE:
        return _decide_stop_update(
            signal, side, context, config, undelivered=undelivered, late_seconds=late_seconds
        )

    # An exit or cancel for the other side is still forwarded (it can only reduce risk), but
    # it is not tied to an entry it does not belong to.
    matched = reference is not None and reference.side == signal.side
    link = TradeLink(
        entry_id=reference.id if reference is not None and matched else None,
        entry_undelivered=undelivered and matched,
        late_seconds=late_seconds,
    )
    if action is Action.EXIT:
        text = messages.exit_signal(signal, venue=venue, link=link)
    else:
        release_hint = matched and budget is not None and budget.counted
        text = messages.cancel_signal(signal, venue=venue, link=link, release_hint=release_hint)
    return Decision(Outcome.FORWARDED, (OutboxDraft(text),))


def decide(signal: SignalRow, context: DecisionContext, config: ProcessorConfig) -> Decision:
    """Decide one signal from plain data. Pure: no clock, no store, no I/O.

    Raises `SignalDataError` (a ValueError) for a row no valid alert could have produced.
    """
    try:
        action = Action(signal.action)
    except ValueError:
        raise SignalDataError("signal has an unknown action") from None
    if action is Action.HEARTBEAT:
        return Decision(Outcome.HEARTBEAT)
    if action is Action.ENTRY:
        return _decide_entry(signal, context, config)
    return _decide_follow_up(signal, action, context, config)


# --------------------------------------------------------------------------------------------
# Store work: context in, decision out, all inside the caller's transaction
# --------------------------------------------------------------------------------------------


def current_prop_day(config: ProcessorConfig, now_ms: int) -> dt.date:
    reset = config.venue.daily_reset
    return prop_day(utc_from_ms(now_ms), reset.zone, reset.time_of_day)


def _flag_set(tx: Tx, key: StateKey) -> bool:
    # Anything but absent / "0" counts as set: an unreadable kill switch must not let entries pass.
    value = tx.get_state(key)
    return value is not None and value != _FLAG_CLEAR


def load_context(
    tx: Tx, signal: SignalRow, config: ProcessorConfig, now_ms: int
) -> DecisionContext:
    """Read from the store what `decide` needs for this signal."""
    if signal.action == Action.ENTRY.value:
        return DecisionContext(
            now_ms=now_ms,
            killed=_flag_set(tx, StateKey.KILLED),
            paused=_flag_set(tx, StateKey.PAUSED),
            budget_used=tx.budget_used(current_prop_day(config, now_ms)),
        )
    if signal.action not in (Action.STOP_UPDATE.value, Action.EXIT.value, Action.CANCEL.value):
        return DecisionContext(now_ms=now_ms)

    lookback_start_ms = signal.bar_time_ms - REFERENCE_LOOKBACK_MS
    reference = tx.latest_signal(
        strategy_id=signal.strategy_id,
        symbol=signal.symbol,
        action=Action.ENTRY.value,
        min_bar_time_ms=lookback_start_ms,
        max_bar_time_ms=signal.bar_time_ms,
    )
    # The lookups below filter on FORWARDED, so they never match the signal being processed.
    exit_after_reference = reference is not None and (
        tx.latest_signal(
            strategy_id=signal.strategy_id,
            symbol=signal.symbol,
            action=Action.EXIT.value,
            min_bar_time_ms=reference.bar_time_ms,
            max_bar_time_ms=_NO_UPPER_BOUND_MS,
            outcome=Outcome.FORWARDED,
        )
        is not None
    )
    last_stop_update = None
    if signal.action == Action.STOP_UPDATE.value:
        last_stop_update = tx.latest_signal(
            strategy_id=signal.strategy_id,
            symbol=signal.symbol,
            action=Action.STOP_UPDATE.value,
            min_bar_time_ms=lookback_start_ms if reference is None else reference.bar_time_ms,
            max_bar_time_ms=_NO_UPPER_BOUND_MS,
            outcome=Outcome.FORWARDED,
        )
    return DecisionContext(
        now_ms=now_ms,
        reference=reference,
        reference_budget=None if reference is None else tx.get_budget_entry(reference.id),
        exit_after_reference=exit_after_reference,
        last_stop_update=last_stop_update,
    )


def _apply(
    tx: Tx, signal: SignalRow, decision: Decision, config: ProcessorConfig, now_ms: int
) -> int:
    """Write a decision; return the number of outbox rows it enqueued."""
    tx.complete_signal(signal.id, decision.outcome, now_ms)
    if decision.budget_risk is not None:
        tx.add_budget_entry(
            signal_id=signal.id,
            prop_day=current_prop_day(config, now_ms),
            risk_amount=decision.budget_risk,
            now_ms=now_ms,
        )
    enqueued = 0
    # Signal texts first: an alarm raised with them must not delay them in the FIFO.
    for draft in decision.messages:
        tx.enqueue_message(
            kind=draft.kind,
            text=draft.text,
            now_ms=now_ms,
            signal_id=signal.id,
            expires_at_ms=draft.expires_at_ms,
            expired_text=draft.expired_text,
        )
        enqueued += 1
    for alarm in decision.alarms:
        if tx.enqueue_alarm(
            code=alarm.code, text=alarm.text, now_ms=now_ms, throttle_ms=alarm.throttle_ms
        ):
            enqueued += 1
    return enqueued


def _process(
    tx: Tx, signal: SignalRow, config: ProcessorConfig, now_ms: int
) -> tuple[Outcome, int]:
    decision = decide(signal, load_context(tx, signal, config, now_ms), config)
    return decision.outcome, _apply(tx, signal, decision, config, now_ms)


def process_one(tx: Tx, config: ProcessorConfig, now_ms: int) -> SignalRow | None:
    """Process the oldest queued signal inside `tx`; return it as stored, or None when idle.

    Outcome, budget entry and outbox rows are written in the caller's transaction. An
    exception leaves the transaction for the caller to roll back (nothing is half-written).
    """
    signal = tx.next_queued_signal()
    if signal is None:
        return None
    _process(tx, signal, config, now_ms)
    return tx.get_signal(signal.id)


# --------------------------------------------------------------------------------------------
# Background task
# --------------------------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class _Step:
    processed: bool  # False: the queue was empty
    enqueued: int = 0


def _quarantine(store: Store, signal_id: int, now_ms: int) -> bool:
    """Set a signal that cannot be processed aside and tell the user. False: nothing to do."""
    with store.transaction() as tx:
        row = tx.get_signal(signal_id)
        if row is None or row.status is not SignalStatus.QUEUED:
            return False
        tx.complete_signal(row.id, Outcome.ERROR, now_ms)
        tx.enqueue_message(
            kind=OutboxKind.ALARM, text=messages.processing_error_alarm(row), now_ms=now_ms
        )
    return True


def _step(store: Store, config: ProcessorConfig, now_ms: int) -> _Step:
    """One blocking step: process the next signal in ONE transaction, or quarantine it."""
    taken: list[SignalRow] = []
    try:
        with store.transaction() as tx:
            signal = tx.next_queued_signal()
            if signal is None:
                return _Step(processed=False)
            taken.append(signal)
            outcome, enqueued = _process(tx, signal, config, now_ms)
    except Exception as error:
        if not taken:
            raise  # the queue itself cannot be read: not one signal's fault
        poisoned = taken[0]
        # The failed transaction has rolled back. If this one fails too, the task ends.
        if not _quarantine(store, poisoned.id, now_ms):
            raise
        # Exception type only: its message may quote row content.
        _log.error(
            "signal_processing_failed",
            signal_id=poisoned.id,
            strategy_id=poisoned.strategy_id,
            action=poisoned.action,
            error_type=type(error).__name__,
        )
        return _Step(processed=True, enqueued=1)
    _log.info(
        "signal_processed",
        signal_id=signal.id,
        strategy_id=signal.strategy_id,
        action=signal.action,
        outcome=outcome.value,
        messages=enqueued,
    )
    return _Step(processed=True, enqueued=enqueued)


async def _wait_for_any(events: tuple[asyncio.Event, ...], timeout: float) -> None:
    """Return when one of the events is set or the timeout passes, whichever is first."""
    waiters = [asyncio.create_task(event.wait()) for event in events]
    try:
        await asyncio.wait(waiters, timeout=timeout, return_when=asyncio.FIRST_COMPLETED)
    finally:
        for waiter in waiters:
            waiter.cancel()
        await asyncio.gather(*waiters, return_exceptions=True)


async def run_processor(
    *,
    store: Store,
    config: ProcessorConfig,
    clock: Clock,
    wake: asyncio.Event,
    on_messages: Callable[[], None],
    stop: asyncio.Event,
    idle_poll_seconds: float = 1.0,
) -> None:
    """Drain the signal queue, then sleep until `wake`, `stop` or the idle poll.

    A signal that cannot be processed is marked ERROR with an alarm and the queue moves on;
    only a store failure outside one signal's processing ends the task (the service then
    exits and is restarted). `on_messages` is called after each transaction that enqueued
    outbox rows.
    """
    while not stop.is_set():
        # Cleared before draining: a signal queued while we work sets it again.
        wake.clear()
        while not stop.is_set():
            step = await asyncio.to_thread(_step, store, config, clock())
            if step.enqueued:
                on_messages()
            if not step.processed:
                break
        if stop.is_set():
            break
        await _wait_for_any((wake, stop), idle_poll_seconds)
