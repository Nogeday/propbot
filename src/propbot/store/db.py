"""SQLite store of the signal bot: signal queue, daily budget ledger, Telegram outbox, state.

One process, one connection (`check_same_thread=False`, `isolation_level=None`), guarded by a
`threading.RLock`. Every read and write happens inside `Store.transaction()`, which runs
`BEGIN IMMEDIATE` and commits on success or rolls back on any exception, so a signal's outcome,
its budget entry and its outbox rows are written together or not at all (exactly-once
processing). PRAGMAs: `journal_mode=WAL`, `synchronous=FULL`, `foreign_keys=ON`,
`busy_timeout=5000`.

This is the bottom layer: standard library only. Wire values of the alert schema (action, side,
order type) are stored as the plain text the caller hands over; enums defined here are checked
by the database and again when a row is read back (unknown text fails closed with `StoreError`).

Units: every `*_ms` column is INTEGER UTC epoch milliseconds; prices and money are REAL;
`prop_day` is TEXT `YYYY-MM-DD`; booleans are INTEGER 0/1. Nothing secret is stored here: the
webhook token is stripped before `payload_json` is built, and callers redact `last_error`.

Schema version 1 (all tables STRICT; no migrations yet, another version refuses to open):

    table           column           type     notes
    --------------  ---------------  -------  ---------------------------------------------------
    meta            key              TEXT     PRIMARY KEY; holds `schema_version`
                    value            TEXT     NOT NULL
    signals         id               INTEGER  PRIMARY KEY AUTOINCREMENT (queue order, never reused)
                    received_at_ms   INTEGER  NOT NULL
                    strategy_id      TEXT     NOT NULL
                    symbol           TEXT     NOT NULL
                    tf               TEXT     NOT NULL
                    bar_time_ms      INTEGER  NOT NULL; bar open
                    bar_close_ms     INTEGER  NOT NULL
                    action           TEXT     NOT NULL
                    side             TEXT     nullable
                    setup            TEXT     NOT NULL ("" when the alert has none)
                    order_type       TEXT     nullable
                    est_entry        REAL     nullable
                    stop             REAL     nullable
                    tp               REAL     nullable
                    rr               REAL     nullable
                    reason           TEXT     nullable
                    payload_json     TEXT     NOT NULL; canonical alert JSON without the token
                    status           TEXT     NOT NULL; SignalStatus
                    outcome          TEXT     Outcome; NULL exactly while queued
                    processed_at_ms  INTEGER  NULL exactly while queued
                    UNIQUE (strategy_id, symbol, bar_time_ms, action)   -- CLAUDE.md invariant 5
    budget_entries  signal_id        INTEGER  PRIMARY KEY, REFERENCES signals(id)
                    prop_day         TEXT     NOT NULL; YYYY-MM-DD
                    risk_amount      REAL     NOT NULL, >= 0; account currency
                    counted          INTEGER  NOT NULL; 0/1
                    release_reason   TEXT     ReleaseReason; NULL exactly while counted
                    updated_at_ms    INTEGER  NOT NULL
    outbox          id               INTEGER  PRIMARY KEY AUTOINCREMENT (FIFO order)
                    created_at_ms    INTEGER  NOT NULL
                    kind             TEXT     NOT NULL; OutboxKind
                    signal_id        INTEGER  nullable, REFERENCES signals(id)
                    text             TEXT     NOT NULL
                    expires_at_ms    INTEGER  nullable
                    expired_text     TEXT     nullable
                    status           TEXT     NOT NULL; OutboxStatus
                    attempts         INTEGER  NOT NULL, >= 0; failed delivery attempts so far
                    next_attempt_ms  INTEGER  NOT NULL
                    sent_at_ms       INTEGER  set exactly when status is `sent`
                    closed_at_ms     INTEGER  when the row left `pending` (audit; not in OutboxRow)
                    last_error       TEXT     nullable; at most MAX_ERROR_CHARS characters
                    delivery_uncertain INTEGER NOT NULL; 0/1, sticky (see OutboxRow)
    state           key              TEXT     PRIMARY KEY; StateKey values and "alarm:<code>"
                    value            TEXT     NOT NULL
                    updated_at_ms    INTEGER  NOT NULL

    indexes: signals(status, id) · signals(received_at_ms) · budget_entries(prop_day, signal_id)
             · outbox(status, id) · outbox(signal_id)
"""

import datetime as dt
import math
import sqlite3
import threading
import warnings
from collections.abc import Iterator, Mapping, Sequence
from contextlib import AbstractContextManager, contextmanager, suppress
from dataclasses import dataclass
from enum import StrEnum
from pathlib import Path
from types import TracebackType
from typing import Final, Self

SCHEMA_VERSION: Final = 1

MEMORY_PATH: Final = ":memory:"
BUSY_TIMEOUT_MS: Final = 5000
ALARM_STATE_PREFIX: Final = "alarm:"
MAX_ERROR_CHARS: Final = 500
"""`last_error` is cut to this length: it is a diagnostic hint, not a log."""

# STRICT tables need SQLite 3.37; RETURNING needs 3.35.
_MIN_SQLITE_VERSION: Final = (3, 37, 0)
# `signals.action` of the heartbeat indicator (wire value; the alert schema lives a layer above).
_HEARTBEAT_ACTION: Final = "heartbeat"
_SCHEMA_VERSION_KEY: Final = "schema_version"
_SYNCHRONOUS_FULL: Final = 2


class StoreError(Exception):
    """The store refused an operation or SQLite failed. Callers treat it as fail-closed."""


class SignalStatus(StrEnum):
    QUEUED = "queued"
    DONE = "done"


class Outcome(StrEnum):
    FORWARDED = "forwarded"
    STALE = "stale"
    SUPPRESSED_PAUSED = "suppressed_paused"
    SUPPRESSED_KILLED = "suppressed_killed"
    SKIP_RISK_TOO_WIDE = "skip_risk_too_wide"
    SKIP_BUDGET = "skip_budget"
    FILTERED_MIN_STEP = "filtered_min_step"
    REJECTED_WIDEN = "rejected_widen"
    NO_ACTIVE_TRADE = "no_active_trade"
    HEARTBEAT = "heartbeat"
    # An entry that arrived before its bar closed: the two clocks disagree, validity is unknown.
    SKIP_CLOCK = "skip_clock"
    # An update older than one that was already sent to the user (out-of-order delivery).
    SUPERSEDED = "superseded"
    # Processing raised: the signal is set aside so it cannot block the queue (alarm sent).
    ERROR = "error"


class OutboxKind(StrEnum):
    SIGNAL = "signal"
    ALARM = "alarm"
    NOTICE = "notice"


class OutboxStatus(StrEnum):
    PENDING = "pending"
    SENT = "sent"
    EXPIRED = "expired"
    FAILED = "failed"


class ReleaseReason(StrEnum):
    SKIPPED = "skipped"
    UNDELIVERED = "undelivered"


class StateKey(StrEnum):
    """Well-known keys of the `state` table. Flags hold "1" when set; absent or "0" otherwise."""

    PAUSED = "paused"
    KILLED = "killed"
    PIN_FAIL_COUNT = "pin_fail_count"
    PIN_WINDOW_START_MS = "pin_window_start_ms"
    PIN_LOCKED_UNTIL_MS = "pin_locked_until_ms"
    HEARTBEAT_ALARM_MS = "heartbeat_alarm_ms"
    HEARTBEAT_SILENT = "heartbeat_silent"
    EXPIRY_REMINDER_DAY = "expiry_reminder_day"
    LAST_ALIVE_MS = "last_alive_ms"  # written by the monitor on every tick (downtime detection)


@dataclass(frozen=True, slots=True, kw_only=True)
class NewSignal:
    """A validated alert, ready to be queued."""

    received_at_ms: int
    strategy_id: str
    symbol: str
    tf: str
    bar_time_ms: int
    bar_close_ms: int
    action: str
    side: str | None
    setup: str
    order_type: str | None
    est_entry: float | None
    stop: float | None
    tp: float | None
    rr: float | None
    reason: str | None
    payload_json: str


@dataclass(frozen=True, slots=True, kw_only=True)
class SignalRow:
    """A stored signal: every `NewSignal` field plus its id and processing state."""

    id: int
    received_at_ms: int
    strategy_id: str
    symbol: str
    tf: str
    bar_time_ms: int
    bar_close_ms: int
    action: str
    side: str | None
    setup: str
    order_type: str | None
    est_entry: float | None
    stop: float | None
    tp: float | None
    rr: float | None
    reason: str | None
    payload_json: str
    status: SignalStatus
    outcome: Outcome | None
    processed_at_ms: int | None


@dataclass(frozen=True, slots=True, kw_only=True)
class BudgetRow:
    signal_id: int
    prop_day: dt.date
    risk_amount: float
    counted: bool
    release_reason: ReleaseReason | None
    updated_at_ms: int


@dataclass(frozen=True, slots=True, kw_only=True)
class OutboxRow:
    id: int
    created_at_ms: int
    kind: OutboxKind
    signal_id: int | None
    text: str
    expires_at_ms: int | None
    expired_text: str | None
    status: OutboxStatus
    attempts: int
    next_attempt_ms: int
    sent_at_ms: int | None
    last_error: str | None
    # True once a send may have reached Telegram without us learning the answer (a timeout
    # after the request left, or a restart while the row was pending): "not marked sent" is
    # then no proof that the user did not get the message.
    delivery_uncertain: bool


@dataclass(frozen=True, slots=True, kw_only=True)
class LatencySample:
    signal_id: int
    strategy_id: str
    action: str
    bar_close_ms: int
    received_at_ms: int
    sent_at_ms: int | None
    """Earliest `sent_at_ms` of the signal's outbox rows, if any was sent."""


# --------------------------------------------------------------------------------------------
# SQL: connection setup and schema
# --------------------------------------------------------------------------------------------


def _sql_values(enum_type: type[StrEnum]) -> str:
    """SQL literal list of an enum's values, for CHECK constraints (values are code constants)."""
    return ", ".join(f"'{member.value}'" for member in enum_type)


_SQL_PRAGMA_JOURNAL_MODE_WAL: Final = "PRAGMA journal_mode = WAL"
_SQL_PRAGMA_SYNCHRONOUS_FULL: Final = "PRAGMA synchronous = FULL"
_SQL_PRAGMA_FOREIGN_KEYS_ON: Final = "PRAGMA foreign_keys = ON"
_SQL_PRAGMA_BUSY_TIMEOUT: Final = f"PRAGMA busy_timeout = {BUSY_TIMEOUT_MS}"
_SQL_READ_SYNCHRONOUS: Final = "PRAGMA synchronous"
_SQL_READ_FOREIGN_KEYS: Final = "PRAGMA foreign_keys"
_SQL_READ_BUSY_TIMEOUT: Final = "PRAGMA busy_timeout"

_SQL_BEGIN: Final = "BEGIN IMMEDIATE"
_SQL_COMMIT: Final = "COMMIT"
_SQL_ROLLBACK: Final = "ROLLBACK"

_SQL_LIST_TABLES: Final = (
    "SELECT name FROM sqlite_master WHERE type = 'table' AND name NOT LIKE 'sqlite~_%' ESCAPE '~'"
)
_SQL_READ_META: Final = "SELECT value FROM meta WHERE key = ?"
_SQL_WRITE_META: Final = "INSERT INTO meta (key, value) VALUES (?, ?)"

_SQL_CREATE_META: Final = """
CREATE TABLE meta (
    key   TEXT PRIMARY KEY,
    value TEXT NOT NULL
) STRICT
"""

_SQL_CREATE_SIGNALS: Final = f"""
CREATE TABLE signals (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    received_at_ms  INTEGER NOT NULL,
    strategy_id     TEXT NOT NULL,
    symbol          TEXT NOT NULL,
    tf              TEXT NOT NULL,
    bar_time_ms     INTEGER NOT NULL,
    bar_close_ms    INTEGER NOT NULL,
    action          TEXT NOT NULL,
    side            TEXT,
    setup           TEXT NOT NULL,
    order_type      TEXT,
    est_entry       REAL,
    stop            REAL,
    tp              REAL,
    rr              REAL,
    reason          TEXT,
    payload_json    TEXT NOT NULL,
    status          TEXT NOT NULL DEFAULT '{SignalStatus.QUEUED.value}'
                    CHECK (status IN ({_sql_values(SignalStatus)})),
    outcome         TEXT CHECK (outcome IN ({_sql_values(Outcome)})),
    processed_at_ms INTEGER,
    UNIQUE (strategy_id, symbol, bar_time_ms, action),
    CHECK ((status = '{SignalStatus.QUEUED.value}') = (outcome IS NULL)),
    CHECK ((status = '{SignalStatus.QUEUED.value}') = (processed_at_ms IS NULL))
) STRICT
"""

_SQL_CREATE_BUDGET_ENTRIES: Final = f"""
CREATE TABLE budget_entries (
    signal_id      INTEGER PRIMARY KEY REFERENCES signals (id),
    prop_day       TEXT NOT NULL
                   CHECK (prop_day GLOB '[0-9][0-9][0-9][0-9]-[0-9][0-9]-[0-9][0-9]'),
    risk_amount    REAL NOT NULL CHECK (risk_amount >= 0),
    counted        INTEGER NOT NULL CHECK (counted IN (0, 1)),
    release_reason TEXT CHECK (release_reason IN ({_sql_values(ReleaseReason)})),
    updated_at_ms  INTEGER NOT NULL,
    CHECK ((counted = 1) = (release_reason IS NULL))
) STRICT
"""

_SQL_CREATE_OUTBOX: Final = f"""
CREATE TABLE outbox (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    created_at_ms   INTEGER NOT NULL,
    kind            TEXT NOT NULL CHECK (kind IN ({_sql_values(OutboxKind)})),
    signal_id       INTEGER REFERENCES signals (id),
    text            TEXT NOT NULL,
    expires_at_ms   INTEGER,
    expired_text    TEXT,
    status          TEXT NOT NULL DEFAULT '{OutboxStatus.PENDING.value}'
                    CHECK (status IN ({_sql_values(OutboxStatus)})),
    attempts        INTEGER NOT NULL DEFAULT 0 CHECK (attempts >= 0),
    next_attempt_ms INTEGER NOT NULL,
    sent_at_ms      INTEGER,
    closed_at_ms    INTEGER,
    last_error      TEXT,
    delivery_uncertain INTEGER NOT NULL DEFAULT 0 CHECK (delivery_uncertain IN (0, 1)),
    CHECK ((status = '{OutboxStatus.SENT.value}') = (sent_at_ms IS NOT NULL)),
    CHECK ((status = '{OutboxStatus.PENDING.value}') = (closed_at_ms IS NULL))
) STRICT
"""

_SQL_CREATE_STATE: Final = """
CREATE TABLE state (
    key           TEXT PRIMARY KEY,
    value         TEXT NOT NULL,
    updated_at_ms INTEGER NOT NULL
) STRICT
"""

_SQL_CREATE_INDEXES: Final = (
    "CREATE INDEX signals_status_id ON signals (status, id)",
    "CREATE INDEX signals_received_at ON signals (received_at_ms)",
    "CREATE INDEX budget_entries_prop_day ON budget_entries (prop_day, signal_id)",
    "CREATE INDEX outbox_status_id ON outbox (status, id)",
    "CREATE INDEX outbox_signal_id ON outbox (signal_id)",
)

_SQL_SCHEMA: Final = (
    _SQL_CREATE_META,
    _SQL_CREATE_SIGNALS,
    _SQL_CREATE_BUDGET_ENTRIES,
    _SQL_CREATE_OUTBOX,
    _SQL_CREATE_STATE,
    *_SQL_CREATE_INDEXES,
)
_TABLES: Final = frozenset({"meta", "signals", "budget_entries", "outbox", "state"})

# --------------------------------------------------------------------------------------------
# SQL: signals
# --------------------------------------------------------------------------------------------

_SIGNAL_COLUMNS: Final = (
    "id, received_at_ms, strategy_id, symbol, tf, bar_time_ms, bar_close_ms, action, side, "
    "setup, order_type, est_entry, stop, tp, rr, reason, payload_json, status, outcome, "
    "processed_at_ms"
)

# The duplicate check is a separate SELECT inside the same write transaction, not
# `ON CONFLICT DO NOTHING`: with AUTOINCREMENT a skipped conflict would still burn an id.
_SQL_FIND_SIGNAL_BY_KEY: Final = """
SELECT id FROM signals
WHERE strategy_id = :strategy_id AND symbol = :symbol AND bar_time_ms = :bar_time_ms
  AND action = :action
"""
_SQL_INSERT_SIGNAL: Final = """
INSERT INTO signals (
    received_at_ms, strategy_id, symbol, tf, bar_time_ms, bar_close_ms, action, side, setup,
    order_type, est_entry, stop, tp, rr, reason, payload_json
) VALUES (
    :received_at_ms, :strategy_id, :symbol, :tf, :bar_time_ms, :bar_close_ms, :action, :side,
    :setup, :order_type, :est_entry, :stop, :tp, :rr, :reason, :payload_json
)
RETURNING id
"""
_SQL_NEXT_QUEUED_SIGNAL: Final = f"""
SELECT {_SIGNAL_COLUMNS} FROM signals
WHERE status = '{SignalStatus.QUEUED.value}' ORDER BY id LIMIT 1
"""
_SQL_COMPLETE_SIGNAL: Final = f"""
UPDATE signals SET status = '{SignalStatus.DONE.value}', outcome = ?, processed_at_ms = ?
WHERE id = ? AND status = '{SignalStatus.QUEUED.value}'
"""
_SQL_GET_SIGNAL: Final = f"SELECT {_SIGNAL_COLUMNS} FROM signals WHERE id = ?"
_SQL_LATEST_SIGNAL: Final = f"""
SELECT {_SIGNAL_COLUMNS} FROM signals
WHERE strategy_id = ? AND symbol = ? AND action = ? AND bar_time_ms BETWEEN ? AND ?
ORDER BY bar_time_ms DESC LIMIT 1
"""
_SQL_LATEST_SIGNAL_WITH_OUTCOME: Final = f"""
SELECT {_SIGNAL_COLUMNS} FROM signals
WHERE strategy_id = ? AND symbol = ? AND action = ? AND bar_time_ms BETWEEN ? AND ?
  AND outcome = ?
ORDER BY bar_time_ms DESC LIMIT 1
"""
_SQL_SIGNALS_RECEIVED_BETWEEN: Final = f"""
SELECT {_SIGNAL_COLUMNS} FROM signals
WHERE received_at_ms >= ? AND received_at_ms < ? ORDER BY id
"""
_SQL_LAST_RECEIVED: Final = "SELECT MAX(received_at_ms) FROM signals"
_SQL_LAST_RECEIVED_FOR_ACTION: Final = "SELECT MAX(received_at_ms) FROM signals WHERE action = ?"
_SQL_QUEUE_DEPTH: Final = (
    f"SELECT COUNT(*) FROM signals WHERE status = '{SignalStatus.QUEUED.value}'"
)
_SQL_LATENCY_SAMPLES: Final = """
SELECT s.id AS signal_id, s.strategy_id, s.action, s.bar_close_ms, s.received_at_ms,
       (SELECT MIN(o.sent_at_ms) FROM outbox AS o WHERE o.signal_id = s.id) AS sent_at_ms
FROM signals AS s
WHERE s.action <> ? AND s.received_at_ms >= ?
ORDER BY s.id
"""

# --------------------------------------------------------------------------------------------
# SQL: daily budget ledger
# --------------------------------------------------------------------------------------------

_BUDGET_COLUMNS: Final = "signal_id, prop_day, risk_amount, counted, release_reason, updated_at_ms"

_SQL_ADD_BUDGET_ENTRY: Final = """
INSERT INTO budget_entries (signal_id, prop_day, risk_amount, counted, release_reason,
                            updated_at_ms)
VALUES (?, ?, ?, 1, NULL, ?)
"""
_SQL_GET_BUDGET_ENTRY: Final = f"SELECT {_BUDGET_COLUMNS} FROM budget_entries WHERE signal_id = ?"
_SQL_BUDGET_ENTRIES: Final = (
    f"SELECT {_BUDGET_COLUMNS} FROM budget_entries WHERE prop_day = ? ORDER BY signal_id"
)
_SQL_BUDGET_USED: Final = """
SELECT COALESCE(SUM(risk_amount), 0.0) FROM budget_entries WHERE prop_day = ? AND counted = 1
"""
_SQL_SET_BUDGET_COUNTED: Final = """
UPDATE budget_entries SET counted = ?, release_reason = ?, updated_at_ms = ? WHERE signal_id = ?
"""

# --------------------------------------------------------------------------------------------
# SQL: outbox
# --------------------------------------------------------------------------------------------

_OUTBOX_COLUMNS: Final = (
    "id, created_at_ms, kind, signal_id, text, expires_at_ms, expired_text, status, attempts, "
    "next_attempt_ms, sent_at_ms, last_error, delivery_uncertain"
)

_SQL_ENQUEUE_MESSAGE: Final = """
INSERT INTO outbox (created_at_ms, kind, signal_id, text, expires_at_ms, expired_text,
                    next_attempt_ms)
VALUES (?, ?, ?, ?, ?, ?, ?)
RETURNING id
"""
_SQL_NEXT_PENDING_MESSAGE: Final = f"""
SELECT {_OUTBOX_COLUMNS} FROM outbox
WHERE status = '{OutboxStatus.PENDING.value}' ORDER BY id LIMIT 1
"""
_SQL_GET_MESSAGE: Final = f"SELECT {_OUTBOX_COLUMNS} FROM outbox WHERE id = ?"
_SQL_MESSAGES_FOR_SIGNAL: Final = (
    f"SELECT {_OUTBOX_COLUMNS} FROM outbox WHERE signal_id = ? ORDER BY id"
)
_SQL_MARK_MESSAGE_RETRY: Final = f"""
UPDATE outbox
SET attempts = attempts + 1, next_attempt_ms = ?, last_error = ?,
    delivery_uncertain = MAX(delivery_uncertain, ?)
WHERE id = ? AND status = '{OutboxStatus.PENDING.value}'
"""
# Rows with a deadline are the entry signals: only for them does "delivered or not" matter.
_SQL_FLAG_PENDING_DEADLINES_UNCERTAIN: Final = f"""
UPDATE outbox SET delivery_uncertain = 1
WHERE status = '{OutboxStatus.PENDING.value}' AND expires_at_ms IS NOT NULL
  AND delivery_uncertain = 0
"""
_SQL_PENDING_EXPIRED_MESSAGES: Final = f"""
SELECT {_OUTBOX_COLUMNS} FROM outbox
WHERE status = '{OutboxStatus.PENDING.value}' AND expires_at_ms IS NOT NULL AND expires_at_ms < ?
ORDER BY id
"""
_SQL_MESSAGES_CREATED_BETWEEN: Final = f"""
SELECT {_OUTBOX_COLUMNS} FROM outbox
WHERE created_at_ms >= ? AND created_at_ms < ? ORDER BY id
"""
# Leaves `pending` for good. A NULL error keeps the last retry error as history.
_SQL_CLOSE_MESSAGE: Final = f"""
UPDATE outbox
SET status = ?, sent_at_ms = ?, closed_at_ms = ?, last_error = COALESCE(?, last_error)
WHERE id = ? AND status = '{OutboxStatus.PENDING.value}'
"""
_SQL_OUTBOX_COUNTS: Final = "SELECT status, COUNT(*) FROM outbox GROUP BY status"

# --------------------------------------------------------------------------------------------
# SQL: state
# --------------------------------------------------------------------------------------------

_SQL_GET_STATE: Final = "SELECT value FROM state WHERE key = ?"
_SQL_SET_STATE: Final = """
INSERT INTO state (key, value, updated_at_ms) VALUES (?, ?, ?)
ON CONFLICT (key) DO UPDATE SET value = excluded.value, updated_at_ms = excluded.updated_at_ms
"""
_SQL_DELETE_STATE: Final = "DELETE FROM state WHERE key = ?"

# --------------------------------------------------------------------------------------------
# Row mapping (fail closed on anything the schema should have made impossible)
# --------------------------------------------------------------------------------------------

_Params = Sequence[object] | Mapping[str, object]


def _store_error(exc: Exception) -> StoreError:
    # SQLite messages name tables, columns and constraints; they never contain row values.
    return StoreError(f"veritabanı hatası ({type(exc).__name__}): {exc}")


def _enum[E: StrEnum](enum_type: type[E], value: object, column: str) -> E:
    if isinstance(value, str):
        try:
            return enum_type(value)
        except ValueError:
            pass
    raise StoreError(f"{column} sütununda bilinmeyen değer: {str(value)[:40]!r}")


def _optional_enum[E: StrEnum](enum_type: type[E], value: object, column: str) -> E | None:
    return None if value is None else _enum(enum_type, value, column)


def _flag(value: object, column: str) -> bool:
    if isinstance(value, int) and value in (0, 1):
        return value == 1
    raise StoreError(f"{column} sütununda 0/1 dışında bir değer var: {str(value)[:40]!r}")


def _date(value: object, column: str) -> dt.date:
    if isinstance(value, str):
        try:
            day = dt.date.fromisoformat(value)
        except ValueError:
            day = None
        # `fromisoformat` also accepts basic and week formats; only YYYY-MM-DD is ours.
        if day is not None and day.isoformat() == value:
            return day
    raise StoreError(f"{column} sütununda geçersiz tarih: {str(value)[:40]!r}")


def _day_text(day: dt.date) -> str:
    if isinstance(day, dt.datetime) or not isinstance(day, dt.date):
        raise StoreError("prop_day saat içermeyen bir tarih (datetime.date) olmalı")
    return day.isoformat()


def _require_finite(name: str, value: float | None) -> None:
    # SQLite stores NaN as NULL, keeps infinities and converts numeric-looking text: none of
    # these may reach a price or money column.
    if value is None:
        return
    if isinstance(value, bool) or not isinstance(value, int | float):
        raise StoreError(f"{name} sayı olmalı")
    if not math.isfinite(value):
        raise StoreError(f"{name} sonlu bir sayı olmalı")


def _signal_row(row: sqlite3.Row) -> SignalRow:
    return SignalRow(
        id=row["id"],
        received_at_ms=row["received_at_ms"],
        strategy_id=row["strategy_id"],
        symbol=row["symbol"],
        tf=row["tf"],
        bar_time_ms=row["bar_time_ms"],
        bar_close_ms=row["bar_close_ms"],
        action=row["action"],
        side=row["side"],
        setup=row["setup"],
        order_type=row["order_type"],
        est_entry=row["est_entry"],
        stop=row["stop"],
        tp=row["tp"],
        rr=row["rr"],
        reason=row["reason"],
        payload_json=row["payload_json"],
        status=_enum(SignalStatus, row["status"], "signals.status"),
        outcome=_optional_enum(Outcome, row["outcome"], "signals.outcome"),
        processed_at_ms=row["processed_at_ms"],
    )


def _budget_row(row: sqlite3.Row) -> BudgetRow:
    return BudgetRow(
        signal_id=row["signal_id"],
        prop_day=_date(row["prop_day"], "budget_entries.prop_day"),
        risk_amount=row["risk_amount"],
        counted=_flag(row["counted"], "budget_entries.counted"),
        release_reason=_optional_enum(
            ReleaseReason, row["release_reason"], "budget_entries.release_reason"
        ),
        updated_at_ms=row["updated_at_ms"],
    )


def _outbox_row(row: sqlite3.Row) -> OutboxRow:
    return OutboxRow(
        id=row["id"],
        created_at_ms=row["created_at_ms"],
        kind=_enum(OutboxKind, row["kind"], "outbox.kind"),
        signal_id=row["signal_id"],
        text=row["text"],
        expires_at_ms=row["expires_at_ms"],
        expired_text=row["expired_text"],
        status=_enum(OutboxStatus, row["status"], "outbox.status"),
        attempts=row["attempts"],
        next_attempt_ms=row["next_attempt_ms"],
        sent_at_ms=row["sent_at_ms"],
        last_error=row["last_error"],
        delivery_uncertain=bool(row["delivery_uncertain"]),
    )


def _latency_sample(row: sqlite3.Row) -> LatencySample:
    return LatencySample(
        signal_id=row["signal_id"],
        strategy_id=row["strategy_id"],
        action=row["action"],
        bar_close_ms=row["bar_close_ms"],
        received_at_ms=row["received_at_ms"],
        sent_at_ms=row["sent_at_ms"],
    )


# --------------------------------------------------------------------------------------------
# Transaction
# --------------------------------------------------------------------------------------------

# Handed to `Tx` by `Store.transaction()` only: nobody else can build a working `Tx`.
_TX_KEY: Final = object()


class Tx:
    """The open transaction of a `Store`. Valid only inside its `with` block, on its thread.

    Every method is a plain synchronous call. SQLite failures surface as `StoreError`; when
    one escapes the `with` block the whole transaction is rolled back.
    """

    __slots__ = ("_conn", "_open", "_thread_id")

    def __init__(self, conn: sqlite3.Connection, key: object) -> None:
        if key is not _TX_KEY:
            raise StoreError("Tx yalnızca Store.transaction() ile açılır")
        self._conn: Final = conn
        self._open = True
        self._thread_id: Final = threading.get_ident()

    # -- plumbing ----------------------------------------------------------------------------

    def _finish(self) -> None:
        self._open = False

    def _execute(self, sql: str, params: _Params) -> sqlite3.Cursor:
        if not self._open:
            raise StoreError("transaction kapandı: Tx yalnızca kendi with bloğunda kullanılır")
        if threading.get_ident() != self._thread_id:
            raise StoreError("Tx başka bir thread'den kullanılamaz")
        try:
            return self._conn.execute(sql, params)
        except (sqlite3.Error, OverflowError) as exc:
            raise _store_error(exc) from exc

    def _one(self, sql: str, params: _Params = ()) -> sqlite3.Row | None:
        cursor = self._execute(sql, params)
        try:
            row: sqlite3.Row | None = cursor.fetchone()
        except sqlite3.Error as exc:
            raise _store_error(exc) from exc
        finally:
            # Resets the statement: an unfinished RETURNING/SELECT must not outlive the call.
            cursor.close()
        return row

    def _all(self, sql: str, params: _Params = ()) -> list[sqlite3.Row]:
        cursor = self._execute(sql, params)
        try:
            return cursor.fetchall()
        except sqlite3.Error as exc:
            raise _store_error(exc) from exc
        finally:
            cursor.close()

    def _change(self, sql: str, params: _Params) -> int:
        cursor = self._execute(sql, params)
        try:
            return cursor.rowcount
        finally:
            cursor.close()

    # -- signals -----------------------------------------------------------------------------

    def insert_signal(self, signal: NewSignal) -> int | None:
        """Queue a signal. Return its id, or None when the idempotency key already exists."""
        for name in ("est_entry", "stop", "tp", "rr"):
            _require_finite(name, getattr(signal, name))
        values: dict[str, object] = {
            "received_at_ms": signal.received_at_ms,
            "strategy_id": signal.strategy_id,
            "symbol": signal.symbol,
            "tf": signal.tf,
            "bar_time_ms": signal.bar_time_ms,
            "bar_close_ms": signal.bar_close_ms,
            "action": signal.action,
            "side": signal.side,
            "setup": signal.setup,
            "order_type": signal.order_type,
            "est_entry": signal.est_entry,
            "stop": signal.stop,
            "tp": signal.tp,
            "rr": signal.rr,
            "reason": signal.reason,
            "payload_json": signal.payload_json,
        }
        # This transaction holds the write lock, so nobody can insert between the two steps.
        if self._one(_SQL_FIND_SIGNAL_BY_KEY, values) is not None:
            return None
        row = self._one(_SQL_INSERT_SIGNAL, values)
        if row is None:
            raise StoreError("sinyal satırı eklenemedi")
        signal_id: int = row["id"]
        return signal_id

    def next_queued_signal(self) -> SignalRow | None:
        row = self._one(_SQL_NEXT_QUEUED_SIGNAL)
        return None if row is None else _signal_row(row)

    def complete_signal(self, signal_id: int, outcome: Outcome, now_ms: int) -> None:
        """Move a queued signal to `done` with its outcome. Anything else is a StoreError."""
        changed = self._change(_SQL_COMPLETE_SIGNAL, (str(outcome), now_ms, signal_id))
        if changed != 1:
            existing = self.get_signal(signal_id)
            if existing is None:
                raise StoreError(f"sinyal bulunamadı: id {signal_id}")
            raise StoreError(f"sinyal zaten işlenmiş: id {signal_id}")

    def get_signal(self, signal_id: int) -> SignalRow | None:
        row = self._one(_SQL_GET_SIGNAL, (signal_id,))
        return None if row is None else _signal_row(row)

    def latest_signal(
        self,
        *,
        strategy_id: str,
        symbol: str,
        action: str,
        min_bar_time_ms: int,
        max_bar_time_ms: int,
        outcome: Outcome | None = None,
    ) -> SignalRow | None:
        """Signal with the highest `bar_time_ms` in [min, max] (both inclusive).

        `outcome=None` matches any signal, processed or not.
        """
        key = (strategy_id, symbol, action, min_bar_time_ms, max_bar_time_ms)
        if outcome is None:
            row = self._one(_SQL_LATEST_SIGNAL, key)
        else:
            row = self._one(_SQL_LATEST_SIGNAL_WITH_OUTCOME, (*key, str(outcome)))
        return None if row is None else _signal_row(row)

    def signals_received_between(self, start_ms: int, end_ms: int) -> list[SignalRow]:
        """Signals with `start_ms <= received_at_ms < end_ms`, ordered by id."""
        rows = self._all(_SQL_SIGNALS_RECEIVED_BETWEEN, (start_ms, end_ms))
        return [_signal_row(row) for row in rows]

    def last_received_ms(self, *, action: str | None = None) -> int | None:
        if action is None:
            row = self._one(_SQL_LAST_RECEIVED)
        else:
            row = self._one(_SQL_LAST_RECEIVED_FOR_ACTION, (action,))
        last: int | None = None if row is None else row[0]
        return last

    def queue_depth(self) -> int:
        row = self._one(_SQL_QUEUE_DEPTH)
        depth: int = 0 if row is None else row[0]
        return depth

    def latency_samples(self, since_ms: int) -> list[LatencySample]:
        """Non-heartbeat signals received at or after `since_ms`, ordered by id."""
        rows = self._all(_SQL_LATENCY_SAMPLES, (_HEARTBEAT_ACTION, since_ms))
        return [_latency_sample(row) for row in rows]

    # -- daily budget ledger -----------------------------------------------------------------

    def add_budget_entry(
        self, *, signal_id: int, prop_day: dt.date, risk_amount: float, now_ms: int
    ) -> None:
        """Record the risk of a forwarded entry as counted. One entry per signal."""
        _require_finite("risk_amount", risk_amount)
        if risk_amount < 0:
            raise StoreError("risk_amount negatif olamaz")
        self._change(_SQL_ADD_BUDGET_ENTRY, (signal_id, _day_text(prop_day), risk_amount, now_ms))

    def get_budget_entry(self, signal_id: int) -> BudgetRow | None:
        row = self._one(_SQL_GET_BUDGET_ENTRY, (signal_id,))
        return None if row is None else _budget_row(row)

    def budget_entries(self, prop_day: dt.date) -> list[BudgetRow]:
        rows = self._all(_SQL_BUDGET_ENTRIES, (_day_text(prop_day),))
        return [_budget_row(row) for row in rows]

    def budget_used(self, prop_day: dt.date) -> float:
        """Sum of the counted risk of a prop day (0.0 when there is none)."""
        row = self._one(_SQL_BUDGET_USED, (_day_text(prop_day),))
        return 0.0 if row is None else float(row[0])

    def set_budget_counted(
        self, *, signal_id: int, counted: bool, reason: ReleaseReason | None, now_ms: int
    ) -> None:
        """Release an entry's risk (`counted=False` + reason) or count it again (no reason)."""
        if counted and reason is not None:
            raise StoreError("sayılan bütçe kaydında serbest bırakma nedeni olamaz")
        if not counted and reason is None:
            raise StoreError("bütçe kaydını serbest bırakmak için neden gerekir")
        reason_text = None if reason is None else str(reason)
        changed = self._change(
            _SQL_SET_BUDGET_COUNTED, (1 if counted else 0, reason_text, now_ms, signal_id)
        )
        if changed != 1:
            raise StoreError(f"bütçe kaydı bulunamadı: sinyal id {signal_id}")

    # -- outbox (FIFO) -----------------------------------------------------------------------

    def enqueue_message(
        self,
        *,
        kind: OutboxKind,
        text: str,
        now_ms: int,
        signal_id: int | None = None,
        expires_at_ms: int | None = None,
        expired_text: str | None = None,
    ) -> int:
        """Append a pending message, due immediately. Return its id."""
        row = self._one(
            _SQL_ENQUEUE_MESSAGE,
            (now_ms, str(kind), signal_id, text, expires_at_ms, expired_text, now_ms),
        )
        if row is None:
            raise StoreError("outbox satırı eklenemedi")
        message_id: int = row["id"]
        return message_id

    def enqueue_alarm(self, *, code: str, text: str, now_ms: int, throttle_ms: int) -> bool:
        """Enqueue an ALARM unless one with the same code went out less than `throttle_ms` ago.

        The last enqueue time lives in the state table under "alarm:<code>". An unreadable
        value or a clock that moved backwards never silences an alarm: it is sent.
        """
        if not code:
            raise StoreError("alarm kodu boş olamaz")
        key = ALARM_STATE_PREFIX + code
        previous = self.get_state(key)
        if previous is not None:
            try:
                elapsed = now_ms - int(previous)
            except ValueError:
                elapsed = None
            if elapsed is not None and 0 <= elapsed < throttle_ms:
                return False
        self.enqueue_message(kind=OutboxKind.ALARM, text=text, now_ms=now_ms)
        self.set_state(key, str(now_ms), now_ms)
        return True

    def next_pending_message(self) -> OutboxRow | None:
        """Head of the FIFO: lowest id still pending, whatever its `next_attempt_ms`."""
        row = self._one(_SQL_NEXT_PENDING_MESSAGE)
        return None if row is None else _outbox_row(row)

    def get_message(self, message_id: int) -> OutboxRow | None:
        row = self._one(_SQL_GET_MESSAGE, (message_id,))
        return None if row is None else _outbox_row(row)

    def messages_for_signal(self, signal_id: int) -> list[OutboxRow]:
        return [_outbox_row(row) for row in self._all(_SQL_MESSAGES_FOR_SIGNAL, (signal_id,))]

    def mark_message_sent(self, message_id: int, now_ms: int) -> None:
        self._close_message(message_id, OutboxStatus.SENT, now_ms, sent_at_ms=now_ms, error=None)

    def mark_message_retry(
        self, message_id: int, *, next_attempt_ms: int, error: str, uncertain: bool
    ) -> None:
        """Count a failed attempt and schedule the next one; the message stays pending.

        `uncertain=True` records that the request may have reached Telegram (the flag is
        sticky: a later attempt that provably never left does not clear it).
        """
        changed = self._change(
            _SQL_MARK_MESSAGE_RETRY,
            (next_attempt_ms, error[:MAX_ERROR_CHARS], int(uncertain), message_id),
        )
        self._require_pending_changed(message_id, changed)

    def flag_pending_deadlines_uncertain(self) -> int:
        """Mark every pending message that has a deadline as possibly delivered.

        Called once at start-up: the previous process may have died between a successful
        send and `mark_message_sent`. Returns the number of rows flagged.
        """
        return self._change(_SQL_FLAG_PENDING_DEADLINES_UNCERTAIN, ())

    def pending_expired_messages(self, now_ms: int) -> list[OutboxRow]:
        """Pending messages whose `expires_at_ms` lies before `now_ms`, oldest first."""
        return [_outbox_row(row) for row in self._all(_SQL_PENDING_EXPIRED_MESSAGES, (now_ms,))]

    def messages_created_between(self, start_ms: int, end_ms: int) -> list[OutboxRow]:
        """Outbox rows created in [start_ms, end_ms), by id (reports)."""
        return [
            _outbox_row(row) for row in self._all(_SQL_MESSAGES_CREATED_BETWEEN, (start_ms, end_ms))
        ]

    def mark_message_failed(self, message_id: int, *, error: str, now_ms: int) -> None:
        self._close_message(
            message_id,
            OutboxStatus.FAILED,
            now_ms,
            sent_at_ms=None,
            error=error[:MAX_ERROR_CHARS],
        )

    def mark_message_expired(self, message_id: int, now_ms: int) -> None:
        self._close_message(message_id, OutboxStatus.EXPIRED, now_ms, sent_at_ms=None, error=None)

    def outbox_counts(self) -> dict[OutboxStatus, int]:
        """Number of outbox rows per status; every status is present as a key."""
        counts = dict.fromkeys(OutboxStatus, 0)
        for row in self._all(_SQL_OUTBOX_COUNTS):
            counts[_enum(OutboxStatus, row[0], "outbox.status")] = row[1]
        return counts

    def _close_message(
        self,
        message_id: int,
        status: OutboxStatus,
        now_ms: int,
        *,
        sent_at_ms: int | None,
        error: str | None,
    ) -> None:
        changed = self._change(
            _SQL_CLOSE_MESSAGE, (status.value, sent_at_ms, now_ms, error, message_id)
        )
        self._require_pending_changed(message_id, changed)

    def _require_pending_changed(self, message_id: int, changed: int) -> None:
        if changed == 1:
            return
        existing = self.get_message(message_id)
        if existing is None:
            raise StoreError(f"mesaj bulunamadı: id {message_id}")
        raise StoreError(f"mesaj beklemede değil ({existing.status.value}): id {message_id}")

    # -- state -------------------------------------------------------------------------------

    def get_state(self, key: str) -> str | None:
        row = self._one(_SQL_GET_STATE, (str(key),))
        value: str | None = None if row is None else row[0]
        return value

    def set_state(self, key: str, value: str, now_ms: int) -> None:
        self._change(_SQL_SET_STATE, (str(key), value, now_ms))

    def delete_state(self, key: str) -> None:
        self._change(_SQL_DELETE_STATE, (str(key),))


# --------------------------------------------------------------------------------------------
# Store
# --------------------------------------------------------------------------------------------


def _rollback_quietly(conn: sqlite3.Connection) -> None:
    """Roll back while another error is already on its way out (that one must win)."""
    # If the rollback itself fails the connection stays inside the transaction and the next
    # BEGIN IMMEDIATE fails: the store stays closed to writes until the process restarts.
    with suppress(sqlite3.Error):
        if conn.in_transaction:
            conn.execute(_SQL_ROLLBACK)


def _scalar(conn: sqlite3.Connection, sql: str) -> object:
    row = conn.execute(sql).fetchone()
    return None if row is None else row[0]


def _configure(conn: sqlite3.Connection) -> None:
    """Apply the per-connection PRAGMAs and verify each one took effect."""
    conn.execute(_SQL_PRAGMA_BUSY_TIMEOUT)
    conn.execute(_SQL_PRAGMA_FOREIGN_KEYS_ON)
    conn.execute(_SQL_PRAGMA_SYNCHRONOUS_FULL)
    for name, sql, expected in (
        ("busy_timeout", _SQL_READ_BUSY_TIMEOUT, BUSY_TIMEOUT_MS),
        ("foreign_keys", _SQL_READ_FOREIGN_KEYS, 1),
        ("synchronous", _SQL_READ_SYNCHRONOUS, _SYNCHRONOUS_FULL),
    ):
        actual = _scalar(conn, sql)
        if actual != expected:
            raise StoreError(f"PRAGMA {name} ayarlanamadı (beklenen {expected}, okunan {actual!r})")


def _enable_wal(conn: sqlite3.Connection, *, in_memory: bool) -> None:
    """Switch the database file to write-ahead logging (persistent; a no-op when already on)."""
    journal_mode = _scalar(conn, _SQL_PRAGMA_JOURNAL_MODE_WAL)
    # An in-memory database has no file to log to and reports "memory".
    if journal_mode != ("memory" if in_memory else "wal"):
        raise StoreError(f"journal_mode=WAL ayarlanamadı (okunan: {journal_mode!r})")


def _ensure_schema(conn: sqlite3.Connection) -> None:
    """Create the schema in an empty database; otherwise require exactly this version."""
    conn.execute(_SQL_BEGIN)
    try:
        tables = {row[0] for row in conn.execute(_SQL_LIST_TABLES).fetchall()}
        if not tables:
            for statement in _SQL_SCHEMA:
                conn.execute(statement)
            conn.execute(_SQL_WRITE_META, (_SCHEMA_VERSION_KEY, str(SCHEMA_VERSION)))
        elif "meta" not in tables:
            raise StoreError("bu dosya bir PropBot veritabanı değil (meta tablosu yok)")
        else:
            row = conn.execute(_SQL_READ_META, (_SCHEMA_VERSION_KEY,)).fetchone()
            found = None if row is None else row[0]
            if found != str(SCHEMA_VERSION):
                raise StoreError(
                    f"veritabanı şema sürümü uyuşmuyor: beklenen {SCHEMA_VERSION}, "
                    f"okunan {found!r} (geçiş/migration desteği henüz yok)"
                )
            if tables != _TABLES:
                difference = ", ".join(sorted(tables ^ _TABLES))
                raise StoreError(f"veritabanı tabloları şemayla uyuşmuyor: {difference}")
        conn.execute(_SQL_COMMIT)
    except BaseException:
        _rollback_quietly(conn)
        raise


def _connect(path: Path | str) -> sqlite3.Connection:
    if sqlite3.sqlite_version_info < _MIN_SQLITE_VERSION:
        needed = ".".join(str(part) for part in _MIN_SQLITE_VERSION)
        raise StoreError(f"SQLite {needed}+ gerekli (kurulu: {sqlite3.sqlite_version})")
    if sqlite3.threadsafety == 0:
        raise StoreError("SQLite kütüphanesi thread desteği olmadan derlenmiş")
    target = str(path)
    in_memory = target == MEMORY_PATH
    if not in_memory:
        if not target:
            raise StoreError("veritabanı yolu boş olamaz")
        file_path = Path(target)
        if not file_path.parent.is_dir():
            raise StoreError(f"veritabanı klasörü yok: {file_path.parent}")
        if file_path.is_dir():
            raise StoreError(f"veritabanı yolu bir klasör: {file_path}")
    try:
        conn = sqlite3.connect(
            target,
            timeout=BUSY_TIMEOUT_MS / 1000.0,
            isolation_level=None,  # no implicit BEGIN: transactions are opened explicitly
            check_same_thread=False,  # shared across threads, serialized by Store's lock
        )
    except sqlite3.Error as exc:
        raise _store_error(exc) from exc
    try:
        conn.row_factory = sqlite3.Row
        _configure(conn)
        # The schema check comes before WAL: a file that is refused is left exactly as it was.
        _ensure_schema(conn)
        _enable_wal(conn, in_memory=in_memory)
    except sqlite3.Error as exc:
        conn.close()
        raise _store_error(exc) from exc
    except BaseException:
        conn.close()
        raise
    return conn


class Store:
    """The bot's SQLite database. Open once per process; share between threads freely.

    `path` is a file whose parent directory exists (the file is created when missing) or
    ":memory:" (tests). Close it when done; it is also a context manager. An instance that
    is garbage-collected while still open closes its connection and emits a ResourceWarning.
    """

    def __init__(self, path: Path | str) -> None:
        self._label: Final = str(path)
        self._lock: Final = threading.RLock()
        self._active: Tx | None = None
        self._conn: sqlite3.Connection | None = None
        self._conn = _connect(path)

    def transaction(self) -> AbstractContextManager[Tx]:
        """Open the one write transaction: `with store.transaction() as tx: ...`.

        Waits for other threads' transactions, then runs BEGIN IMMEDIATE. Commits when the
        block ends normally and rolls back when it raises. Opening a second transaction on
        the thread that already holds one is a StoreError (it would otherwise silently nest).
        """
        return self._transaction()

    @contextmanager
    def _transaction(self) -> Iterator[Tx]:
        with self._lock:
            # The lock is re-entrant, so an active Tx here can only belong to this thread.
            if self._active is not None:
                raise StoreError("iç içe transaction açılamaz (aynı thread'de zaten açık)")
            conn = self._conn
            if conn is None:
                raise StoreError("veritabanı kapalı")
            try:
                conn.execute(_SQL_BEGIN)
            except sqlite3.Error as exc:
                raise _store_error(exc) from exc
            tx = Tx(conn, _TX_KEY)
            self._active = tx
            try:
                yield tx
                try:
                    conn.execute(_SQL_COMMIT)
                except sqlite3.Error as exc:
                    raise _store_error(exc) from exc
            except BaseException:
                _rollback_quietly(conn)
                raise
            finally:
                tx._finish()
                self._active = None

    def close(self) -> None:
        """Close the connection. Idempotent; refuses while this thread's transaction is open."""
        with self._lock:
            if self._active is not None:
                raise StoreError("açık transaction varken veritabanı kapatılamaz")
            conn, self._conn = self._conn, None
            if conn is not None:
                try:
                    conn.close()
                except sqlite3.Error as exc:
                    raise _store_error(exc) from exc

    def __enter__(self) -> Self:
        return self

    def __exit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        traceback: TracebackType | None,
    ) -> None:
        self.close()

    def __del__(self) -> None:
        # `__init__` may have failed before the attribute existed.
        conn: sqlite3.Connection | None = getattr(self, "_conn", None)
        if conn is None:
            return
        self._conn = None
        with suppress(sqlite3.Error):
            conn.close()
        # sqlite3 itself only warns about unclosed connections from Python 3.13 on.
        warnings.warn(
            f"unclosed propbot Store {self._label!r}", ResourceWarning, stacklevel=2, source=self
        )
