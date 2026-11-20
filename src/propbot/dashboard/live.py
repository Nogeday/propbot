"""View model of the live account and signal screen (ADR-0009 item 5, SPEC §6.8).

Everything the screen shows is computed here from the store, the venue profile and the
risk config, into a tree of frozen dataclasses that `pages` renders and `/api/live`
returns as JSON. Two rules shape it:

- Honesty: the bot sends no orders before Faz 3 and knows no fills, balance or P/L. Each
  number therefore carries the label of where it comes from (default from the profile,
  entered by hand, risk ledger, declaration); a number that cannot be computed says so
  instead of guessing.
- The database is a state, not a crash: a missing file (never created here), a refused
  schema and a busy lock each become a `DbStatus` the page shows. Everything is read in
  ONE read-only snapshot (`Store.read()`, `read_snapshot`) that takes no write lock, so a
  refresh never makes `serve` wait (ADR-0009); the view is built outside it.
"""

import datetime as dt
import json
import math
import sqlite3
import threading
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from enum import StrEnum
from pathlib import Path
from typing import Final

import structlog

from propbot.core.clock import utc_from_ms
from propbot.core.propday import prop_day
from propbot.notify.format import ISTANBUL, fmt_clock, fmt_date, fmt_money, fmt_price
from propbot.risk.config import RiskConfig
from propbot.risk.rules import daily_budget_amount
from propbot.risk.venue import DailyLossBasis, MaxLossType, VenueProfile
from propbot.signals.heartbeat import DOWNTIME_THRESHOLD_MS
from propbot.signals.processor import BUDGET_EPSILON, REFERENCE_LOOKBACK_MS
from propbot.signals.report import OUTCOME_LABELS
from propbot.signals.schema import STRATEGY_NAMES, Action
from propbot.store.db import (
    BudgetRow,
    OutboxStatus,
    Outcome,
    ReadTx,
    ReleaseReason,
    SignalRow,
    StateKey,
    Store,
    StoreError,
)

_SECOND_MS: Final = 1000
_DAY_MS: Final = 24 * 60 * 60 * _SECOND_MS
# The alert-line monitor writes LAST_ALIVE_MS every 30 s; three missed ticks is the same
# limit the service itself uses to tell a restart from downtime.
LIVENESS_LIMIT_MS: Final = DOWNTIME_THRESHOLD_MS
RECENT_SIGNAL_COUNT: Final = 25
SIGNAL_LOOKBACK_MS: Final = 7 * _DAY_MS
# A follow-up is matched to an entry at most this far back (the processor's own window).
FOLLOW_UP_WINDOW_MS: Final = REFERENCE_LOOKBACK_MS
MODE_TEXT: Final = "signal — emir gönderilmez"
_FLAG_CLEAR: Final = "0"
_SNAPSHOT_VERSION: Final = 1

DB_MISSING_TEXT: Final = "veritabanı yok — propbot serve hiç çalışmamış olabilir"
DB_EMPTY_TEXT: Final = (
    "veritabanı dosyası boş — propbot serve henüz şemayı yazmamış olabilir; panel dosyaya dokunmaz"
)
DB_BUSY_TEXT: Final = "veritabanı şu an kilitli (başka bir süreç yazıyor); sonraki yenilemede"
NOT_FILLS_NOTE: Final = (
    "Bunlar SİNYAL kayıtlarıdır, dolum değildir: bot emir göndermez ve işlemlerini bilmez. "
    "İletilen bir giriş, aynı strateji ve sembolden çıkış/iptal sinyali gelene kadar "
    '"açık" sayılır.'
)
LEDGER_SOURCE: Final = (
    "botun risk defteri: bugün iletilen girişlerin TAM riski (en kötü durum); ölçülmüş kayıp değil"
)
DECLARED_SOURCE: Final = "beyan: elle girilen gün başı bakiyesi ve güncel özsermaye"
_FOLLOW_UP_ACTIONS: Final = frozenset({Action.EXIT.value, Action.CANCEL.value})
_ACTION_LABELS: Final[Mapping[str, str]] = {
    Action.ENTRY.value: "giriş",
    Action.STOP_UPDATE.value: "stop güncelleme",
    Action.EXIT.value: "çıkış",
    Action.CANCEL.value: "iptal",
}
_SIDE_LABELS: Final[Mapping[str, str]] = {"long": "LONG", "short": "SHORT"}
_DELIVERY_ORDER: Final = (
    OutboxStatus.FAILED,
    OutboxStatus.EXPIRED,
    OutboxStatus.PENDING,
    OutboxStatus.SENT,
)
_DELIVERY_LABELS: Final[Mapping[OutboxStatus, str]] = {
    OutboxStatus.SENT: "gönderildi",
    OutboxStatus.PENDING: "bekliyor",
    OutboxStatus.EXPIRED: "süresi doldu",
    OutboxStatus.FAILED: "başarısız",
}
_CLOSING_OUTCOMES: Final = frozenset({Outcome.FORWARDED, Outcome.NO_ACTIVE_TRADE})
# The screen is re-read every few seconds, so what one refresh reads is bounded: at most
# `MAX_SIGNAL_ROWS` signals of the window (newest first, through an index), and per-row
# lookups only for the rows shown.
MAX_SIGNAL_ROWS: Final = 1000
# Derived trades shown (and looked up in the ledger): the newest forwarded entries.
MAX_TRADE_ITEMS: Final = 50

# Display only, not a rule: a bar turns red when half of its allowance is used up. The rule
# values themselves (targets, limits, the bot's budget) come from the profiles.
WARN_USED_FRACTION: Final = 0.5

_log = structlog.get_logger(__name__)


# --------------------------------------------------------------------------------------------
# Database access: the file is opened lazily and never created
# --------------------------------------------------------------------------------------------


class DbState(StrEnum):
    OK = "ok"
    MISSING = "missing"
    EMPTY = "empty"
    BUSY = "busy"
    ERROR = "error"


@dataclass(frozen=True, slots=True)
class DbStatus:
    state: DbState
    message: str

    @property
    def ok(self) -> bool:
        return self.state is DbState.OK


DB_OK: Final = DbStatus(DbState.OK, "veritabanı: tamam")


def _is_busy(error: StoreError) -> bool:
    text = str(error).lower()
    return "locked" in text or "busy" in text


def classify_store_error(error: StoreError) -> DbStatus:
    """A store failure as the screen states it (the text of a StoreError holds no values)."""
    if _is_busy(error):
        return DbStatus(DbState.BUSY, DB_BUSY_TEXT)
    return DbStatus(DbState.ERROR, f"veritabanı açılamıyor/okunamıyor: {error}")


class StoreSource:
    """The panel's handle on `data/propbot.db`.

    The file is opened on first use and only if it already exists and is not empty, with
    `create=False`: creating it or writing a schema is `serve`'s job, and the open takes no
    write lock. A failure is retried on the next call: `serve` may start, or the lock may
    clear, while the panel is open. Tests hand in an open `Store`.
    """

    def __init__(self, path: Path | None, *, store: Store | None = None) -> None:
        self._path = path
        self._store = store
        self._owned = False
        self._identity: tuple[int, int] | None = None
        self._lock = threading.Lock()

    @property
    def path(self) -> Path | None:
        return self._path

    def _file_identity(self) -> tuple[int, int] | None:
        if self._path is None:
            return None
        try:
            stat = self._path.stat()
        except OSError:
            return None
        return stat.st_dev, stat.st_ino

    def _drop_if_replaced(self) -> None:
        """Forget an open file that is no longer the one at the path.

        The runbook's answer to a schema mismatch is to rename the old file aside and let
        `serve` create a new one. An open connection follows the renamed file, so without
        this the panel would show the old state and write a KILL where `serve` never reads.
        """
        if self._store is None or not self._owned:
            return
        if self._file_identity() == self._identity:
            return
        store, self._store, self._owned, self._identity = self._store, None, False, None
        try:
            store.close()
        except StoreError as error:
            _log.warning("dashboard_db_close_failed", error_type=type(error).__name__)

    def get(self) -> Store | DbStatus:
        with self._lock:
            self._drop_if_replaced()
            if self._store is not None:
                return self._store
            path = self._path
            if path is None or not path.exists():
                return DbStatus(DbState.MISSING, DB_MISSING_TEXT)
            try:
                if path.is_file() and path.stat().st_size == 0:
                    return DbStatus(DbState.EMPTY, DB_EMPTY_TEXT)
            except OSError as error:
                return DbStatus(DbState.ERROR, f"veritabanı okunamıyor ({type(error).__name__})")
            identity = self._file_identity()
            try:
                self._store = Store(path, create=False)
            except StoreError as error:
                return classify_store_error(error)
            except sqlite3.Error as error:
                return DbStatus(DbState.ERROR, f"veritabanı açılamıyor ({type(error).__name__})")
            self._owned = True
            # Taken before opening: a file swapped in between is caught on the next call.
            self._identity = identity
            return self._store

    def close(self) -> None:
        with self._lock:
            store, self._store = self._store, None
            if store is not None and self._owned:
                store.close()
            self._owned = False
            self._identity = None


# --------------------------------------------------------------------------------------------
# Manually entered account snapshot (state key MANUAL_ACCOUNT, JSON)
# --------------------------------------------------------------------------------------------


@dataclass(frozen=True, slots=True, kw_only=True)
class ManualSnapshot:
    balance: float
    equity: float
    day_start_balance: float | None
    entered_at_ms: int


def snapshot_text(snapshot: ManualSnapshot) -> str:
    return json.dumps(
        {
            "v": _SNAPSHOT_VERSION,
            "balance": snapshot.balance,
            "equity": snapshot.equity,
            "day_start_balance": snapshot.day_start_balance,
            "entered_at_ms": snapshot.entered_at_ms,
        },
        allow_nan=False,
        sort_keys=True,
    )


def _positive_finite(value: object) -> float:
    if isinstance(value, bool) or not isinstance(value, int | float):
        raise ValueError("not a number")
    number = float(value)
    if not math.isfinite(number) or number <= 0:
        raise ValueError("not a positive finite number")
    return number


def parse_snapshot(text: str) -> ManualSnapshot:
    """Read the stored snapshot; ValueError for anything the panel did not write."""
    data = json.loads(text)
    if not isinstance(data, dict) or data.get("v") != _SNAPSHOT_VERSION:
        raise ValueError("unknown snapshot format")
    entered = data.get("entered_at_ms")
    if isinstance(entered, bool) or not isinstance(entered, int):
        raise ValueError("entered_at_ms is not an integer")
    day_start = data.get("day_start_balance")
    return ManualSnapshot(
        balance=_positive_finite(data.get("balance")),
        equity=_positive_finite(data.get("equity")),
        day_start_balance=None if day_start is None else _positive_finite(day_start),
        entered_at_ms=entered,
    )


# --------------------------------------------------------------------------------------------
# One read transaction
# --------------------------------------------------------------------------------------------


@dataclass(frozen=True, slots=True, kw_only=True)
class StoreSnapshot:
    """Raw rows read in one transaction; nothing here is formatted yet."""

    paused: bool
    killed: bool
    standby: bool
    last_alive_text: str | None
    pin_locked_until_text: str | None
    manual_text: str | None
    budget_used: float
    budget_entries: tuple[BudgetRow, ...]
    signals: tuple[SignalRow, ...]
    signals_truncated: bool
    """Older signals of the lookback window were not read (bounded refresh)."""
    delivery: Mapping[int, OutboxStatus]
    ledger: Mapping[int, BudgetRow]
    outbox: Mapping[OutboxStatus, int]


def flag_set(value: str | None) -> bool:
    """The flag rule of commands and processor: absent or "0" is clear, anything else is set.

    Unreadable text counts as set, so a broken kill switch fails closed on this screen too.
    """
    return value is not None and value != _FLAG_CLEAR


def current_prop_day(venue: VenueProfile, now_ms: int) -> dt.date:
    reset = venue.daily_reset
    return prop_day(utc_from_ms(now_ms), reset.zone, reset.time_of_day)


def _worst(statuses: Sequence[OutboxStatus]) -> OutboxStatus | None:
    return next((status for status in _DELIVERY_ORDER if status in statuses), None)


def _recent_signals(tx: ReadTx, now_ms: int) -> tuple[tuple[SignalRow, ...], bool]:
    """The window's newest `MAX_SIGNAL_ROWS` signals, oldest first.

    Returns the rows and whether older rows of the window were left unread. Every signal
    newer than the oldest one returned is there too: a follow-up is never missing next to
    its entry. One row more than shown is read, only to know whether the cut happened.
    """
    newest_first = tx.recent_signals(
        MAX_SIGNAL_ROWS + 1, since_ms=now_ms - SIGNAL_LOOKBACK_MS, before_ms=now_ms + _DAY_MS
    )
    kept = newest_first[:MAX_SIGNAL_ROWS]
    return tuple(reversed(kept)), len(newest_first) > MAX_SIGNAL_ROWS


def _forwarded_entries(signals: Sequence[SignalRow]) -> list[SignalRow]:
    """The newest forwarded entries, newest first, at most `MAX_TRADE_ITEMS`."""
    entries = [
        s for s in signals if s.action == Action.ENTRY.value and s.outcome is Outcome.FORWARDED
    ]
    return entries[::-1][:MAX_TRADE_ITEMS]


def _shown_signals(signals: Sequence[SignalRow]) -> list[SignalRow]:
    """The newest non-heartbeat signals, newest first, at most `RECENT_SIGNAL_COUNT`."""
    shown = [s for s in signals if s.action != Action.HEARTBEAT.value]
    return shown[::-1][:RECENT_SIGNAL_COUNT]


def read_snapshot(tx: ReadTx, *, venue: VenueProfile, now_ms: int) -> StoreSnapshot:
    """Everything the screen needs, inside the caller's read-only snapshot.

    Per-row lookups are made only for the rows the screen shows, never for the whole window.
    """
    day = current_prop_day(venue, now_ms)
    signals, truncated = _recent_signals(tx, now_ms)
    delivery: dict[int, OutboxStatus] = {}
    for signal in _shown_signals(signals):
        worst = _worst([message.status for message in tx.messages_for_signal(signal.id)])
        if worst is not None:
            delivery[signal.id] = worst
    ledger: dict[int, BudgetRow] = {}
    for entry in _forwarded_entries(signals):
        row = tx.get_budget_entry(entry.id)
        if row is not None:
            ledger[entry.id] = row
    outbox = tx.outbox_counts()
    return StoreSnapshot(
        paused=flag_set(tx.get_state(StateKey.PAUSED)),
        killed=flag_set(tx.get_state(StateKey.KILLED)),
        standby=flag_set(tx.get_state(StateKey.STANDBY)),
        last_alive_text=tx.get_state(StateKey.LAST_ALIVE_MS),
        pin_locked_until_text=tx.get_state(StateKey.PIN_LOCKED_UNTIL_MS),
        manual_text=tx.get_state(StateKey.MANUAL_ACCOUNT),
        budget_used=tx.budget_used(day),
        budget_entries=tuple(tx.budget_entries(day)),
        signals=signals,
        signals_truncated=truncated,
        delivery=delivery,
        ledger=ledger,
        outbox=outbox,
    )


# --------------------------------------------------------------------------------------------
# View tree
# --------------------------------------------------------------------------------------------


class BotState(StrEnum):
    KILLED = "killed"
    STANDBY = "standby"
    PAUSED = "paused"
    ACTIVE = "active"


BOT_STATE_LABELS: Final[Mapping[BotState, str]] = {
    BotState.KILLED: "KILL",
    BotState.STANDBY: "BEKLEMEDE",
    BotState.PAUSED: "DURAKLATILDI",
    BotState.ACTIVE: "ETKİN",
}
_BOT_STATE_DETAILS: Final[Mapping[BotState, str]] = {
    BotState.KILLED: "giriş sinyalleri kesildi; yalnızca PIN ile açılır",
    BotState.STANDBY: (
        "bu veritabanının servisi beklemede: aynı bot token'ıyla başka bir süreç komutları "
        "alıyor; giriş sinyali iletilmez"
    ),
    BotState.PAUSED: "giriş sinyalleri iletilmiyor (stop/çıkış/iptal gelmeye devam eder)",
    BotState.ACTIVE: "giriş sinyalleri iletiliyor",
}


@dataclass(frozen=True, slots=True)
class BotStatus:
    state: BotState
    label: str
    detail: str
    also: tuple[str, ...]
    """Other flags that are set besides the one the badge shows."""


def bot_status(*, killed: bool, paused: bool, standby: bool) -> BotStatus:
    """KILL > BEKLEMEDE > DURAKLATILDI > ETKİN (ADR-0009 item 5), with the flag rule of /durum.

    /durum names the same flags; it lists a pause before a standby, the badge here follows
    the ADR's order and lists every other set flag next to it, so no flag is hidden.
    """
    set_flags = [
        state
        for state, is_set in (
            (BotState.KILLED, killed),
            (BotState.STANDBY, standby),
            (BotState.PAUSED, paused),
        )
        if is_set
    ]
    state = set_flags[0] if set_flags else BotState.ACTIVE
    also = tuple(BOT_STATE_LABELS[other] for other in set_flags[1:])
    return BotStatus(state, BOT_STATE_LABELS[state], _BOT_STATE_DETAILS[state], also)


@dataclass(frozen=True, slots=True)
class ServiceStatus:
    alive: bool
    label: str
    detail: str


def service_status(last_alive_text: str | None, now_ms: int) -> ServiceStatus:
    if last_alive_text is None:
        return ServiceStatus(False, "çalışmıyor", "servis bu veritabanında hiç canlılık yazmamış")
    try:
        last_ms = int(last_alive_text)
    except ValueError:
        return ServiceStatus(False, "bilinmiyor", "canlılık kaydı okunamadı")
    age_ms = now_ms - last_ms
    when = _stamp(last_ms)
    if age_ms < -LIVENESS_LIMIT_MS:
        return ServiceStatus(False, "bilinmiyor", f"canlılık kaydı ileri tarihli ({when}); saat?")
    if age_ms > LIVENESS_LIMIT_MS:
        return ServiceStatus(
            False,
            "çalışmıyor",
            f"son canlılık {when} — Telegram bildirimleri servis açılana kadar kuyrukta bekler",
        )
    return ServiceStatus(True, "çalışıyor", f"son canlılık {when}")


@dataclass(frozen=True, slots=True)
class Gauge:
    """One bar on the screen: a fraction for the bar and the words that say what it is."""

    title: str
    fraction: float | None
    """0..1 for the bar; None when the value cannot be computed."""
    value_text: str
    source: str
    warn: bool = False


@dataclass(frozen=True, slots=True)
class AccountView:
    balance_text: str
    equity_text: str
    source: str
    manual: bool
    stale: bool
    note: str
    targets: tuple[Gauge, ...]
    total_loss: Gauge
    ledger: tuple[Gauge, ...]
    declared_daily: Gauge | None
    declared_daily_note: str


@dataclass(frozen=True, slots=True)
class SignalItem:
    id: int
    time_text: str
    strategy: str
    symbol: str
    action: str
    side: str
    outcome: str
    delivery: str


@dataclass(frozen=True, slots=True)
class TradeItem:
    signal_id: int
    opened_text: str
    strategy: str
    symbol: str
    side: str
    levels: str
    state: str
    closed_text: str
    ledger_mark: str


@dataclass(frozen=True, slots=True)
class LiveView:
    now_text: str
    venue_name: str
    venue_title: str
    mode_text: str
    prop_day: str
    db: DbStatus
    bot: BotStatus | None
    service: ServiceStatus | None
    outbox_text: str
    account: AccountView
    signals: tuple[SignalItem, ...]
    open_trades: tuple[TradeItem, ...]
    closed_trades: tuple[TradeItem, ...]
    trades_note: str
    pin_locked_until_ms: int | None


def _stamp(epoch_ms: int) -> str:
    return f"{fmt_date(epoch_ms, ISTANBUL)} {fmt_clock(epoch_ms, ISTANBUL)} TR"


def _ratio_text(fraction: float) -> str:
    return f"%{fraction * 100:.1f}"


def _signed_money(amount: float, currency: str) -> str:
    return fmt_money(amount, currency) if amount >= 0 else "−" + fmt_money(-amount, currency)


def _clamp(fraction: float) -> float:
    return min(1.0, max(0.0, fraction))


def _target_gauges(venue: VenueProfile, balance: float, source: str) -> tuple[Gauge, ...]:
    money = venue.account_currency
    initial = venue.initial_balance
    gained = balance - initial
    gauges = []
    phases = (("Faz 1", venue.profit_target_pct.phase1), ("Faz 2", venue.profit_target_pct.phase2))
    for name, pct in phases:
        if pct is None:
            continue
        target = initial * pct / 100.0
        fraction = gained / target
        gauges.append(
            Gauge(
                title=f"{name} kâr hedefi (%{pct:g} = {fmt_money(target, money)})",
                fraction=_clamp(fraction),
                value_text=(
                    f"{_signed_money(gained, money)} / {fmt_money(target, money)} "
                    f"({_ratio_text(fraction)})"
                ),
                source=source,
            )
        )
    return tuple(gauges)


def _total_loss_gauge(venue: VenueProfile, balance: float, equity: float, source: str) -> Gauge:
    money = venue.account_currency
    title = f"Toplam kayıp tamponu (%{venue.max_loss_pct:g}, {venue.max_loss_type.value})"
    if venue.max_loss_type is not MaxLossType.STATIC:
        return Gauge(
            title=title,
            fraction=None,
            value_text="hesaplanamıyor (bu kayıp türünün formülü profilde yok)",
            source=source,
            warn=True,
        )
    allowance = venue.initial_balance * venue.max_loss_pct / 100.0
    floor = venue.initial_balance - allowance
    # The lower of the two: an open loss counts against the floor before it is booked.
    basis = min(balance, equity)
    remaining = basis - floor
    fraction = remaining / allowance
    return Gauge(
        title=title,
        fraction=_clamp(fraction),
        value_text=(
            f"kalan {_signed_money(remaining, money)} / {fmt_money(allowance, money)} "
            f"({_ratio_text(fraction)}) · taban {fmt_money(floor, money)}"
        ),
        source=f"{source} · bakiye ve özsermayeden düşük olanı",
        warn=fraction < 1 - WARN_USED_FRACTION,
    )


def _ledger_gauges(
    venue: VenueProfile, risk: RiskConfig, used: float | None, entries: int
) -> tuple[Gauge, ...]:
    money = venue.account_currency
    budget = daily_budget_amount(risk, venue)
    firm = venue.initial_balance * venue.daily_loss_limit_pct / 100.0
    if used is None:
        return tuple(
            Gauge(
                title=title, fraction=None, value_text="veritabanı okunamadı", source=LEDGER_SOURCE
            )
            for title in ("Botun günlük bütçesi", "Firmanın günlük limiti")
        )
    count = f" · {entries} giriş"
    return (
        Gauge(
            title=f"Botun günlük bütçesi ({fmt_money(budget, money)})",
            fraction=_clamp(used / budget),
            value_text=(
                f"{fmt_money(used, money)} / {fmt_money(budget, money)} "
                f"({_ratio_text(used / budget)}){count}"
            ),
            source=LEDGER_SOURCE,
            warn=used > budget + BUDGET_EPSILON,
        ),
        Gauge(
            title=f"Firmanın günlük limiti (%{venue.daily_loss_limit_pct:g} = "
            f"{fmt_money(firm, money)})",
            fraction=_clamp(used / firm),
            value_text=(
                f"{fmt_money(used, money)} / {fmt_money(firm, money)} ({_ratio_text(used / firm)})"
            ),
            source=LEDGER_SOURCE,
            warn=used / firm >= WARN_USED_FRACTION,
        ),
    )


def _declared_daily(
    venue: VenueProfile, snapshot: ManualSnapshot | None, stale: bool
) -> tuple[Gauge | None, str]:
    if snapshot is None or snapshot.day_start_balance is None:
        return None, "gün başı bakiyesi girilmedi: beyan edilen günlük kayıp hesaplanmıyor"
    if stale:
        return None, "elle girilen değerler önceki prop gününden: bugün için yeniden gir"
    money = venue.account_currency
    limit = venue.initial_balance * venue.daily_loss_limit_pct / 100.0
    loss = snapshot.day_start_balance - snapshot.equity
    remaining = limit - loss
    loss_text = _signed_money(loss, money)
    note = ""
    remaining_label = "kalan"
    if venue.daily_loss_basis is DailyLossBasis.MAX_BALANCE_EQUITY_AT_RESET:
        # The equity at the reset was not entered, so the firm's floor may be higher than
        # the one computed from the balance: the remaining buffer is an upper bound.
        remaining_label = "kalan en çok"
        note = (
            " · firma tabanı reset anındaki özsermayeye de bağlı: o bakiyeden yüksekse kalan "
            "tampon daha azdır"
        )
    return (
        Gauge(
            title=f"Beyan edilen günlük kayıp (limit %{venue.daily_loss_limit_pct:g} = "
            f"{fmt_money(limit, money)})",
            fraction=_clamp(loss / limit),
            value_text=(
                f"kayıp {loss_text} / {fmt_money(limit, money)} ({_ratio_text(loss / limit)}) · "
                f"{remaining_label} {_signed_money(remaining, money)}"
            ),
            source=DECLARED_SOURCE + note,
            warn=loss / limit >= WARN_USED_FRACTION,
        ),
        "",
    )


def account_view(
    *,
    venue: VenueProfile,
    risk: RiskConfig,
    manual_text: str | None,
    budget_used: float | None,
    budget_entries: int,
    now_ms: int,
) -> AccountView:
    money = venue.account_currency
    snapshot: ManualSnapshot | None = None
    note = ""
    if manual_text is not None:
        try:
            snapshot = parse_snapshot(manual_text)
        except ValueError:
            note = "elle girilen bakiye kaydı okunamadı; varsayılan gösteriliyor — yeniden gir"
    if snapshot is None:
        balance = equity = venue.initial_balance
        source = "varsayılan (venue profili initial_balance; gerçek bakiye bilinmiyor)"
        stale = False
    else:
        balance, equity = snapshot.balance, snapshot.equity
        source = f"elle girildi · {_stamp(snapshot.entered_at_ms)}"
        stale = current_prop_day(venue, snapshot.entered_at_ms) != current_prop_day(venue, now_ms)
        if stale:
            source += " · güncel değil (prop günü değişti)"
    declared, declared_note = _declared_daily(venue, snapshot, stale)
    return AccountView(
        balance_text=fmt_money(balance, money),
        equity_text=fmt_money(equity, money),
        source=source,
        manual=snapshot is not None,
        stale=stale,
        note=note,
        targets=_target_gauges(venue, balance, source),
        total_loss=_total_loss_gauge(venue, balance, equity, source),
        ledger=_ledger_gauges(venue, risk, budget_used, budget_entries),
        declared_daily=declared,
        declared_daily_note=declared_note,
    )


def _strategy_name(strategy_id: str) -> str:
    return STRATEGY_NAMES.get(strategy_id, strategy_id)


def _side_text(side: str | None) -> str:
    return "—" if side is None else _SIDE_LABELS.get(side, side)


def _signal_items(snapshot: StoreSnapshot) -> tuple[SignalItem, ...]:
    items = []
    for signal in _shown_signals(snapshot.signals):
        delivery = snapshot.delivery.get(signal.id)
        items.append(
            SignalItem(
                id=signal.id,
                time_text=_stamp(signal.received_at_ms),
                strategy=_strategy_name(signal.strategy_id),
                symbol=signal.symbol,
                action=_ACTION_LABELS.get(signal.action, signal.action),
                side=_side_text(signal.side),
                outcome="kuyrukta" if signal.outcome is None else OUTCOME_LABELS[signal.outcome],
                delivery="mesaj yok" if delivery is None else _DELIVERY_LABELS[delivery],
            )
        )
    return tuple(items)


def _ledger_mark(entry: BudgetRow | None) -> str:
    if entry is None:
        return "bütçe kaydı yok"
    if entry.counted:
        return "bütçede sayılıyor"
    if entry.release_reason is ReleaseReason.SKIPPED:
        return "/atladim ile serbest"
    return "iletilemedi, serbest"


def _levels_text(signal: SignalRow) -> str:
    parts = []
    for label, value in (("giriş", signal.est_entry), ("stop", signal.stop), ("TP", signal.tp)):
        if value is not None:
            parts.append(f"{label} {fmt_price(value)}")
    return " · ".join(parts) if parts else "—"


def _closing_signal(entry: SignalRow, same: Sequence[SignalRow]) -> SignalRow | None:
    """The first exit/cancel that the processor would have matched to this entry.

    The processor ties a follow-up to the latest entry of the same strategy and symbol
    whose bar lies at most `FOLLOW_UP_WINDOW_MS` before the follow-up's bar. `same` holds
    the signals of the entry's strategy and symbol, sorted by (bar time, id).
    """
    newer_entry_bars = [
        other.bar_time_ms
        for other in same
        if other.action == Action.ENTRY.value and other.bar_time_ms > entry.bar_time_ms
    ]
    # A follow-up at or after the next entry's bar belongs to that entry.
    blocked_from = min(newer_entry_bars, default=None)
    latest = entry.bar_time_ms + FOLLOW_UP_WINDOW_MS
    for follow in same:
        if follow.id == entry.id or follow.bar_time_ms < entry.bar_time_ms:
            continue
        if follow.bar_time_ms > latest:
            break
        if blocked_from is not None and follow.bar_time_ms >= blocked_from:
            break
        if follow.action in _FOLLOW_UP_ACTIONS and follow.outcome in _CLOSING_OUTCOMES:
            return follow
    return None


def _by_key(signals: Sequence[SignalRow]) -> dict[tuple[str, str], list[SignalRow]]:
    groups: dict[tuple[str, str], list[SignalRow]] = {}
    for signal in signals:
        groups.setdefault((signal.strategy_id, signal.symbol), []).append(signal)
    for group in groups.values():
        group.sort(key=lambda s: (s.bar_time_ms, s.id))
    return groups


def _trade_items(
    snapshot: StoreSnapshot, now_ms: int
) -> tuple[tuple[TradeItem, ...], tuple[TradeItem, ...]]:
    open_items: list[TradeItem] = []
    closed_items: list[TradeItem] = []
    groups = _by_key(snapshot.signals)
    for entry in _forwarded_entries(snapshot.signals):
        closing = _closing_signal(entry, groups[(entry.strategy_id, entry.symbol)])
        mark = _ledger_mark(snapshot.ledger.get(entry.id))
        if closing is not None:
            state = "kapalı"
            closed_text = (
                f"{_ACTION_LABELS.get(closing.action, closing.action)} sinyali · "
                f"{_stamp(closing.received_at_ms)}"
            )
        elif now_ms - entry.bar_time_ms > FOLLOW_UP_WINDOW_MS:
            state = "bilinmiyor"
            closed_text = "takip penceresi doldu: işlemi platformdan kontrol et"
        else:
            state = "açık"
            closed_text = "—"
        item = TradeItem(
            signal_id=entry.id,
            opened_text=_stamp(entry.received_at_ms),
            strategy=_strategy_name(entry.strategy_id),
            symbol=entry.symbol,
            side=_side_text(entry.side),
            levels=_levels_text(entry),
            state=state,
            closed_text=closed_text,
            ledger_mark=mark,
        )
        (open_items if state == "açık" else closed_items).append(item)
    return tuple(open_items), tuple(closed_items)


def _trades_note(snapshot: StoreSnapshot) -> str:
    note = NOT_FILLS_NOTE
    if snapshot.signals_truncated:
        note += (
            f" Kayıt çok: yalnızca en yeni {len(snapshot.signals)} sinyal incelendi; daha eski "
            "girişler burada görünmez."
        )
    entries = sum(
        1
        for s in snapshot.signals
        if s.action == Action.ENTRY.value and s.outcome is Outcome.FORWARDED
    )
    if entries > MAX_TRADE_ITEMS:
        note += f" İletilen {entries} girişin en yeni {MAX_TRADE_ITEMS} tanesi gösteriliyor."
    return note


def _pin_locked_until(text: str | None, now_ms: int) -> int | None:
    if text is None:
        return None
    try:
        until = int(text)
    except ValueError:
        return now_ms  # unreadable: Commands treats it as locked, so does the screen
    return until if until > now_ms else None


def build_view(
    *,
    venue: VenueProfile,
    venue_name: str,
    risk: RiskConfig,
    db: DbStatus,
    snapshot: StoreSnapshot | None,
    now_ms: int,
) -> LiveView:
    """Pure: the screen from what was read (`snapshot` is None when the store is unusable)."""
    day = current_prop_day(venue, now_ms)
    title = f"{venue.venue.upper()} {venue.program} · {venue.symbol}"
    if snapshot is None:
        account = account_view(
            venue=venue,
            risk=risk,
            manual_text=None,
            budget_used=None,
            budget_entries=0,
            now_ms=now_ms,
        )
        return LiveView(
            now_text=_stamp(now_ms),
            venue_name=venue_name,
            venue_title=title,
            mode_text=MODE_TEXT,
            prop_day=day.isoformat(),
            db=db,
            bot=None,
            service=None,
            outbox_text="bilinmiyor",
            account=account,
            signals=(),
            open_trades=(),
            closed_trades=(),
            trades_note=NOT_FILLS_NOTE,
            pin_locked_until_ms=None,
        )
    open_trades, closed_trades = _trade_items(snapshot, now_ms)
    outbox = snapshot.outbox
    return LiveView(
        now_text=_stamp(now_ms),
        venue_name=venue_name,
        venue_title=title,
        mode_text=MODE_TEXT,
        prop_day=day.isoformat(),
        db=db,
        bot=bot_status(killed=snapshot.killed, paused=snapshot.paused, standby=snapshot.standby),
        service=service_status(snapshot.last_alive_text, now_ms),
        outbox_text=(
            f"{outbox[OutboxStatus.PENDING]} bekliyor · {outbox[OutboxStatus.FAILED]} başarısız"
            f" · {outbox[OutboxStatus.EXPIRED]} süresi doldu"
        ),
        account=account_view(
            venue=venue,
            risk=risk,
            manual_text=snapshot.manual_text,
            budget_used=snapshot.budget_used,
            budget_entries=len(snapshot.budget_entries),
            now_ms=now_ms,
        ),
        signals=_signal_items(snapshot),
        open_trades=open_trades,
        closed_trades=closed_trades,
        trades_note=_trades_note(snapshot),
        pin_locked_until_ms=_pin_locked_until(snapshot.pin_locked_until_text, now_ms),
    )


def load_view(
    source: StoreSource,
    *,
    venue: VenueProfile,
    venue_name: str,
    risk: RiskConfig,
    now_ms: int,
) -> LiveView:
    """Open (if needed), read one snapshot, build outside it. Never raises StoreError."""
    store = source.get()
    if isinstance(store, DbStatus):
        db, snapshot = store, None
    else:
        try:
            with store.read() as tx:
                snapshot = read_snapshot(tx, venue=venue, now_ms=now_ms)
            db = DB_OK
        except StoreError as error:
            db, snapshot = classify_store_error(error), None
    return build_view(
        venue=venue, venue_name=venue_name, risk=risk, db=db, snapshot=snapshot, now_ms=now_ms
    )


def view_json(view: LiveView) -> dict[str, object]:
    """The view as plain JSON data (`/api/live`)."""
    data = _plain(view)
    assert isinstance(data, dict)
    return data


def _plain(value: object) -> object:
    if isinstance(value, StrEnum):
        return value.value
    if hasattr(value, "__dataclass_fields__"):
        names = value.__dataclass_fields__
        data: dict[str, object] = {name: _plain(getattr(value, name)) for name in names}
        if isinstance(value, DbStatus):
            data["ok"] = value.ok
        return data
    if isinstance(value, tuple | list):
        return [_plain(item) for item in value]
    if isinstance(value, Mapping):
        return {str(key): _plain(item) for key, item in value.items()}
    return value
