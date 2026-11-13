"""SQLite store: schema, transactions, signal queue, budget ledger, outbox, state, threads."""

import ast
import dataclasses
import datetime as dt
import gc
import sqlite3
import sys
import threading
from collections.abc import Callable, Iterator
from contextlib import closing
from enum import StrEnum
from pathlib import Path

import pytest

from propbot.store import db
from propbot.store.db import (
    ALARM_STATE_PREFIX,
    BUSY_TIMEOUT_MS,
    MAX_ERROR_CHARS,
    SCHEMA_VERSION,
    BudgetRow,
    LatencySample,
    NewSignal,
    OutboxKind,
    OutboxRow,
    OutboxStatus,
    Outcome,
    ReleaseReason,
    SignalRow,
    SignalStatus,
    StateKey,
    Store,
    StoreError,
    Tx,
)

BAR_MS = 1_784_647_800_000  # bar open of the example alert in the Faz 1 contract
MINUTE_MS = 60_000
DAY = dt.date(2026, 11, 13)
NEXT_DAY = dt.date(2026, 11, 14)
NOW_MS = BAR_MS + 61_000

ENTRY = NewSignal(
    received_at_ms=NOW_MS,
    strategy_id="ny_orb_fvg",
    symbol="CME_MINI:MES1!",
    tf="1",
    bar_time_ms=BAR_MS,
    bar_close_ms=BAR_MS + MINUTE_MS,
    action="entry",
    side="long",
    setup="FVG",
    order_type="market",
    est_entry=5123.25,
    stop=5110.75,
    tp=5148.25,
    rr=2.0,
    reason=None,
    payload_json='{"action":"entry","v":1}',
)
HEARTBEAT = NewSignal(
    received_at_ms=NOW_MS,
    strategy_id="heartbeat",
    symbol="CME_MINI:MES1!",
    tf="15",
    bar_time_ms=BAR_MS,
    bar_close_ms=BAR_MS + 15 * MINUTE_MS,
    action="heartbeat",
    side=None,
    setup="",
    order_type=None,
    est_entry=None,
    stop=None,
    tp=None,
    rr=None,
    reason=None,
    payload_json='{"action":"heartbeat","v":1}',
)
EXIT = dataclasses.replace(
    ENTRY,
    action="exit",
    setup="",
    order_type="market",
    stop=None,
    tp=None,
    rr=None,
    reason="session_end",
    payload_json='{"action":"exit","v":1}',
)


@pytest.fixture
def store() -> Iterator[Store]:
    with Store(":memory:") as opened:
        yield opened


@pytest.fixture
def db_path(tmp_path: Path) -> Path:
    return tmp_path / "propbot.db"


@pytest.fixture
def file_store(db_path: Path) -> Iterator[Store]:
    with Store(db_path) as opened:
        yield opened


def bar(offset: int) -> int:
    """Open time of the bar `offset` minutes after the reference bar."""
    return BAR_MS + offset * MINUTE_MS


def insert(store: Store, signal: NewSignal = ENTRY, **overrides: object) -> int:
    """Insert a signal in its own transaction; it must not be a duplicate."""
    with store.transaction() as tx:
        signal_id = tx.insert_signal(dataclasses.replace(signal, **overrides))
    assert signal_id is not None
    return signal_id


def insert_bars(store: Store, count: int, **overrides: object) -> list[int]:
    """Insert `count` signals on consecutive bars, received in bar order."""
    return [
        insert(store, bar_time_ms=bar(i), received_at_ms=NOW_MS + i * MINUTE_MS, **overrides)
        for i in range(count)
    ]


def enqueue(store: Store, text: str = "mesaj", **kwargs: object) -> int:
    with store.transaction() as tx:
        return tx.enqueue_message(
            kind=kwargs.pop("kind", OutboxKind.NOTICE),  # type: ignore[arg-type]
            text=text,
            now_ms=kwargs.pop("now_ms", NOW_MS),  # type: ignore[arg-type]
            **kwargs,  # type: ignore[arg-type]
        )


def raw_execute(path: Path, sql: str, *, ignore_checks: bool = False) -> list[tuple[object, ...]]:
    """Run one statement on a second, plain connection (the way another process would)."""
    with closing(sqlite3.connect(path, isolation_level=None)) as raw:
        if ignore_checks:
            raw.execute("PRAGMA ignore_check_constraints = ON")
        return raw.execute(sql).fetchall()


# --------------------------------------------------------------------------------------------
# Contract surface
# --------------------------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("enum_type", "values"),
    [
        (SignalStatus, ["queued", "done"]),
        (
            Outcome,
            [
                "forwarded",
                "stale",
                "suppressed_paused",
                "suppressed_killed",
                "skip_risk_too_wide",
                "skip_budget",
                "filtered_min_step",
                "rejected_widen",
                "no_active_trade",
                "heartbeat",
                "skip_clock",
                "superseded",
                "error",
            ],
        ),
        (OutboxKind, ["signal", "alarm", "notice"]),
        (OutboxStatus, ["pending", "sent", "expired", "failed"]),
        (ReleaseReason, ["skipped", "undelivered"]),
        (
            StateKey,
            [
                "paused",
                "killed",
                "pin_fail_count",
                "pin_window_start_ms",
                "pin_locked_until_ms",
                "heartbeat_alarm_ms",
                "heartbeat_silent",
                "expiry_reminder_day",
                "last_alive_ms",
                "restarts_unreported",
            ],
        ),
    ],
)
def test_enum_values_are_the_stored_text(enum_type: type[StrEnum], values: list[str]) -> None:
    # These strings are persisted: changing one needs a schema migration.
    assert [member.value for member in enum_type] == values


def test_schema_version_is_one() -> None:
    assert SCHEMA_VERSION == 1


def test_state_keys_cannot_collide_with_alarm_throttle_keys() -> None:
    assert not any(key.value.startswith(ALARM_STATE_PREFIX) for key in StateKey)


def test_signal_row_has_every_new_signal_field_plus_processing_state() -> None:
    new_fields = {field.name for field in dataclasses.fields(NewSignal)}
    row_fields = {field.name for field in dataclasses.fields(SignalRow)}

    assert row_fields - new_fields == {"id", "status", "outcome", "processed_at_ms"}
    assert new_fields <= row_fields


@pytest.mark.parametrize("row_type", [NewSignal, SignalRow, BudgetRow, OutboxRow, LatencySample])
def test_row_types_are_frozen_slotted_and_keyword_only(row_type: type) -> None:
    params = row_type.__dataclass_params__  # type: ignore[attr-defined]

    assert params.frozen
    assert hasattr(row_type, "__slots__")
    assert all(field.kw_only for field in dataclasses.fields(row_type))


def test_module_is_the_bottom_layer() -> None:
    # Standard library and propbot.core only: no pydantic, nothing from propbot.signals.
    tree = ast.parse(Path(db.__file__).read_text(encoding="utf-8"))
    imported: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            imported.update(alias.name for alias in node.names)
        elif isinstance(node, ast.ImportFrom):
            assert node.level == 0
            assert node.module is not None
            imported.add(node.module)

    foreign = {
        name
        for name in imported
        if name.split(".")[0] not in sys.stdlib_module_names
        and not (name == "propbot.core" or name.startswith("propbot.core."))
    }
    assert foreign == set()


# --------------------------------------------------------------------------------------------
# Opening and the schema
# --------------------------------------------------------------------------------------------


def test_new_file_gets_wal_strict_tables_and_the_schema_version(
    file_store: Store, db_path: Path
) -> None:
    assert db_path.is_file()
    assert raw_execute(db_path, "PRAGMA journal_mode") == [("wal",)]
    assert raw_execute(db_path, "SELECT key, value FROM meta") == [("schema_version", "1")]

    tables = raw_execute(
        db_path, "SELECT name, strict FROM pragma_table_list WHERE schema = 'main' AND wr = 0"
    )
    ours = {name: strict for name, strict in tables if not str(name).startswith("sqlite_")}
    assert ours == {"meta": 1, "signals": 1, "budget_entries": 1, "outbox": 1, "state": 1}


def test_connection_pragmas_are_in_force(file_store: Store) -> None:
    conn = file_store._conn  # the PRAGMAs are per connection: only this one can report them
    assert conn is not None

    assert conn.execute("PRAGMA journal_mode").fetchone()[0] == "wal"
    assert conn.execute("PRAGMA synchronous").fetchone()[0] == 2  # FULL
    assert conn.execute("PRAGMA foreign_keys").fetchone()[0] == 1
    assert conn.execute("PRAGMA busy_timeout").fetchone()[0] == BUSY_TIMEOUT_MS == 5000
    assert conn.isolation_level is None


def test_idempotency_key_is_a_unique_constraint_of_the_signals_table(
    file_store: Store, db_path: Path
) -> None:
    unique_indexes = raw_execute(
        db_path, "SELECT name FROM pragma_index_list('signals') WHERE \"unique\" = 1"
    )
    columns = {
        tuple(row[0] for row in raw_execute(db_path, f"SELECT name FROM pragma_index_info('{n}')"))
        for (n,) in unique_indexes
    }

    assert ("strategy_id", "symbol", "bar_time_ms", "action") in columns


def test_budget_and_outbox_reference_signals(file_store: Store, db_path: Path) -> None:
    for table in ("budget_entries", "outbox"):
        keys = raw_execute(
            db_path, f'SELECT "table", "from", "to" FROM pragma_foreign_key_list(\'{table}\')'
        )
        assert keys == [("signals", "signal_id", "id")]


def test_memory_database_is_allowed() -> None:
    with Store(":memory:") as memory:
        signal_id = insert(memory)
        with memory.transaction() as tx:
            assert tx.get_signal(signal_id) is not None


def test_path_may_be_given_as_text(db_path: Path) -> None:
    with Store(str(db_path)) as opened:
        insert(opened)

    assert db_path.is_file()


def test_everything_survives_a_reopen(db_path: Path) -> None:
    with Store(db_path) as first, first.transaction() as tx:
        signal_id = tx.insert_signal(ENTRY)
        assert signal_id is not None
        tx.complete_signal(signal_id, Outcome.FORWARDED, NOW_MS + 5)
        tx.add_budget_entry(signal_id=signal_id, prop_day=DAY, risk_amount=250.0, now_ms=NOW_MS)
        message_id = tx.enqueue_message(
            kind=OutboxKind.SIGNAL,
            text="[SİNYAL] …",
            now_ms=NOW_MS,
            signal_id=signal_id,
            expires_at_ms=NOW_MS + 120_000,
            expired_text="[İLETİLEMEDİ — ATLA] …",
        )
        tx.set_state(StateKey.PAUSED, "1", NOW_MS)

    with Store(db_path) as second, second.transaction() as tx:
        signal = tx.get_signal(signal_id)
        assert signal is not None
        assert signal.outcome is Outcome.FORWARDED
        assert signal.processed_at_ms == NOW_MS + 5
        assert tx.budget_used(DAY) == 250.0
        message = tx.get_message(message_id)
        assert message is not None
        assert message.text == "[SİNYAL] …"
        assert message.expired_text == "[İLETİLEMEDİ — ATLA] …"
        assert tx.get_state(StateKey.PAUSED) == "1"
        # Ids keep counting: a reopened queue never reuses one.
        assert tx.insert_signal(dataclasses.replace(ENTRY, bar_time_ms=bar(1))) == signal_id + 1


def test_missing_parent_directory_is_refused(tmp_path: Path) -> None:
    target = tmp_path / "missing" / "propbot.db"

    with pytest.raises(StoreError, match="veritabanı klasörü yok"):
        Store(target)

    assert not target.parent.exists()


def test_directory_as_path_is_refused(tmp_path: Path) -> None:
    with pytest.raises(StoreError, match="bir klasör"):
        Store(tmp_path)


def test_empty_path_is_refused() -> None:
    # sqlite3 would silently open a private temporary database.
    with pytest.raises(StoreError, match="boş olamaz"):
        Store("")


def test_file_that_is_not_a_database_is_refused(db_path: Path) -> None:
    content = b"this is not an sqlite database, just text. " * 50
    db_path.write_bytes(content)

    with pytest.raises(StoreError, match="veritabanı hatası"):
        Store(db_path)

    assert db_path.read_bytes() == content


def test_foreign_sqlite_database_is_refused_and_left_untouched(db_path: Path) -> None:
    raw_execute(db_path, "CREATE TABLE notes (body TEXT)")

    with pytest.raises(StoreError, match="PropBot veritabanı değil"):
        Store(db_path)

    assert raw_execute(db_path, "SELECT name FROM sqlite_master") == [("notes",)]
    assert raw_execute(db_path, "PRAGMA journal_mode") == [("delete",)]


@pytest.mark.parametrize("stored", ["2", "0", "01", "1.0", " 1", ""])
def test_other_schema_version_is_refused(db_path: Path, stored: str) -> None:
    Store(db_path).close()
    raw_execute(db_path, f"UPDATE meta SET value = '{stored}' WHERE key = 'schema_version'")

    with pytest.raises(StoreError, match="şema sürümü uyuşmuyor: beklenen 1"):
        Store(db_path)


def test_missing_schema_version_is_refused(db_path: Path) -> None:
    Store(db_path).close()
    raw_execute(db_path, "DELETE FROM meta")

    with pytest.raises(StoreError, match="şema sürümü uyuşmuyor"):
        Store(db_path)


@pytest.mark.parametrize(
    ("change", "named"),
    [
        ("DROP TABLE state", "state"),
        ("CREATE TABLE extra (x INTEGER)", "extra"),
    ],
)
def test_unexpected_table_set_is_refused(db_path: Path, change: str, named: str) -> None:
    Store(db_path).close()
    raw_execute(db_path, change)

    with pytest.raises(StoreError, match=f"tabloları şemayla uyuşmuyor: {named}"):
        Store(db_path)


def test_sqlite_without_strict_tables_is_refused(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(sqlite3, "sqlite_version_info", (3, 36, 0))

    with pytest.raises(StoreError, match=r"SQLite 3\.37\.0\+ gerekli"):
        Store(":memory:")


def test_sqlite_without_thread_support_is_refused(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(sqlite3, "threadsafety", 0)

    with pytest.raises(StoreError, match="thread desteği"):
        Store(":memory:")


def test_this_sqlite_build_meets_the_requirements() -> None:
    assert sqlite3.sqlite_version_info >= (3, 37, 0)
    assert sqlite3.threadsafety > 0


# --------------------------------------------------------------------------------------------
# Closing and resource warnings
# --------------------------------------------------------------------------------------------


def test_close_is_idempotent_and_ends_transactions(db_path: Path) -> None:
    opened = Store(db_path)
    opened.close()
    opened.close()

    with pytest.raises(StoreError, match="veritabanı kapalı"):
        opened.transaction().__enter__()


def test_context_manager_closes_and_releases_the_file(db_path: Path) -> None:
    wal = db_path.with_name(db_path.name + "-wal")
    with Store(db_path) as opened:
        insert(opened)
        assert wal.exists()

    # SQLite removes the write-ahead log when the last connection closes.
    assert not wal.exists()
    with pytest.raises(StoreError, match="veritabanı kapalı"):
        opened.transaction().__enter__()


def test_context_manager_closes_when_the_block_raises(db_path: Path) -> None:
    with pytest.raises(RuntimeError, match="boom"), Store(db_path) as opened:
        raise RuntimeError("boom")

    with pytest.raises(StoreError, match="veritabanı kapalı"):
        opened.transaction().__enter__()


def test_close_inside_a_transaction_is_refused_and_the_store_keeps_working(store: Store) -> None:
    with store.transaction() as tx:
        with pytest.raises(StoreError, match="açık transaction varken"):
            store.close()
        signal_id = tx.insert_signal(ENTRY)

    with store.transaction() as tx:
        assert signal_id is not None
        assert tx.get_signal(signal_id) is not None


def test_opening_using_and_closing_leaves_no_warning(db_path: Path) -> None:
    # pytest runs with filterwarnings=error: a ResourceWarning here would fail the test.
    opened = Store(db_path)
    insert(opened)
    opened.close()
    del opened
    gc.collect()


def test_unclosed_store_warns_and_still_closes_its_connection(db_path: Path) -> None:
    wal = db_path.with_name(db_path.name + "-wal")

    def open_and_abandon() -> None:
        leaked = Store(db_path)
        insert(leaked)
        assert wal.exists()
        del leaked
        gc.collect()

    with pytest.warns(ResourceWarning, match="unclosed propbot Store"):
        open_and_abandon()

    assert not wal.exists()


def test_refused_open_leaks_no_connection(db_path: Path) -> None:
    Store(db_path).close()
    raw_execute(db_path, "UPDATE meta SET value = '2'")

    with pytest.raises(StoreError):
        Store(db_path)
    gc.collect()

    # No connection of ours is left behind holding the write-ahead log open.
    assert not db_path.with_name(db_path.name + "-wal").exists()


# --------------------------------------------------------------------------------------------
# Transactions
# --------------------------------------------------------------------------------------------


def test_transaction_commits_when_the_block_ends(file_store: Store, db_path: Path) -> None:
    with file_store.transaction() as tx:
        tx.insert_signal(ENTRY)
        # Not visible to other connections before the commit.
        assert raw_execute(db_path, "SELECT COUNT(*) FROM signals") == [(0,)]

    assert raw_execute(db_path, "SELECT COUNT(*) FROM signals") == [(1,)]


class Boom(Exception):
    pass


def insert_then_fail(store: Store, signal: NewSignal) -> None:
    with store.transaction() as tx:
        assert tx.insert_signal(signal) is not None
        raise Boom


@pytest.mark.parametrize("error", [Boom("x"), StoreError("x"), KeyboardInterrupt()])
def test_exception_rolls_back_every_write_and_propagates(
    store: Store, error: BaseException
) -> None:
    earlier = insert(store)
    written: list[int | None] = []

    def write_everything_then_fail() -> None:
        with store.transaction() as tx:
            written.append(tx.insert_signal(dataclasses.replace(ENTRY, bar_time_ms=bar(1))))
            tx.complete_signal(earlier, Outcome.FORWARDED, NOW_MS)
            tx.add_budget_entry(signal_id=earlier, prop_day=DAY, risk_amount=250.0, now_ms=NOW_MS)
            tx.enqueue_message(kind=OutboxKind.SIGNAL, text="x", now_ms=NOW_MS, signal_id=earlier)
            tx.enqueue_alarm(code="bad_schema", text="x", now_ms=NOW_MS, throttle_ms=600_000)
            tx.set_state(StateKey.KILLED, "1", NOW_MS)
            raise error

    with pytest.raises(type(error)) as caught:
        write_everything_then_fail()

    assert caught.value is error
    assert written == [earlier + 1]
    with store.transaction() as tx:
        assert tx.get_signal(earlier + 1) is None
        assert tx.queue_depth() == 1
        assert tx.next_queued_signal() == dataclasses.replace(
            _as_row(ENTRY, earlier), status=SignalStatus.QUEUED
        )
        assert tx.get_budget_entry(earlier) is None
        assert tx.budget_used(DAY) == 0.0
        assert tx.next_pending_message() is None
        assert tx.get_state(StateKey.KILLED) is None
        assert tx.get_state(ALARM_STATE_PREFIX + "bad_schema") is None


def _as_row(signal: NewSignal, signal_id: int) -> SignalRow:
    return SignalRow(
        id=signal_id,
        status=SignalStatus.QUEUED,
        outcome=None,
        processed_at_ms=None,
        **dataclasses.asdict(signal),
    )


def test_store_error_caught_inside_the_block_does_not_poison_the_transaction(store: Store) -> None:
    with store.transaction() as tx:
        first = tx.insert_signal(ENTRY)
        with pytest.raises(StoreError):
            tx.complete_signal(999, Outcome.FORWARDED, NOW_MS)
        second = tx.insert_signal(dataclasses.replace(ENTRY, bar_time_ms=bar(1)))

    with store.transaction() as tx:
        assert tx.queue_depth() == 2
        assert (first, second) == (1, 2)


def test_nested_transaction_is_refused_instead_of_deadlocking(store: Store) -> None:
    def nest() -> None:
        with store.transaction() as tx:
            tx.insert_signal(ENTRY)
            with store.transaction():
                pytest.fail("a nested transaction must not open")

    with pytest.raises(StoreError, match="iç içe transaction"):
        nest()

    # The refusal escaped the outer block, so the outer transaction was rolled back.
    with store.transaction() as tx:
        assert tx.queue_depth() == 0


def test_refused_nested_transaction_leaves_the_outer_one_intact(store: Store) -> None:
    with store.transaction() as tx:
        tx.insert_signal(ENTRY)
        with pytest.raises(StoreError, match="iç içe transaction"):
            store.transaction().__enter__()
        tx.set_state("k", "v", NOW_MS)

    with store.transaction() as tx:
        assert tx.queue_depth() == 1
        assert tx.get_state("k") == "v"


TX_CALLS: dict[str, Callable[[Tx], object]] = {
    "insert_signal": lambda tx: tx.insert_signal(ENTRY),
    "next_queued_signal": lambda tx: tx.next_queued_signal(),
    "complete_signal": lambda tx: tx.complete_signal(1, Outcome.FORWARDED, NOW_MS),
    "get_signal": lambda tx: tx.get_signal(1),
    "latest_signal": lambda tx: tx.latest_signal(
        strategy_id="ny_orb_fvg",
        symbol="CME_MINI:MES1!",
        action="entry",
        min_bar_time_ms=0,
        max_bar_time_ms=BAR_MS,
    ),
    "signals_received_between": lambda tx: tx.signals_received_between(0, NOW_MS + 1),
    "last_received_ms": lambda tx: tx.last_received_ms(),
    "queue_depth": lambda tx: tx.queue_depth(),
    "latency_samples": lambda tx: tx.latency_samples(0),
    "add_budget_entry": lambda tx: tx.add_budget_entry(
        signal_id=1, prop_day=DAY, risk_amount=1.0, now_ms=NOW_MS
    ),
    "get_budget_entry": lambda tx: tx.get_budget_entry(1),
    "budget_entries": lambda tx: tx.budget_entries(DAY),
    "budget_used": lambda tx: tx.budget_used(DAY),
    "set_budget_counted": lambda tx: tx.set_budget_counted(
        signal_id=1, counted=False, reason=ReleaseReason.SKIPPED, now_ms=NOW_MS
    ),
    "enqueue_message": lambda tx: tx.enqueue_message(
        kind=OutboxKind.NOTICE, text="x", now_ms=NOW_MS
    ),
    "enqueue_alarm": lambda tx: tx.enqueue_alarm(code="c", text="x", now_ms=NOW_MS, throttle_ms=1),
    "next_pending_message": lambda tx: tx.next_pending_message(),
    "get_message": lambda tx: tx.get_message(1),
    "messages_for_signal": lambda tx: tx.messages_for_signal(1),
    "mark_message_sent": lambda tx: tx.mark_message_sent(1, NOW_MS),
    "mark_message_retry": lambda tx: tx.mark_message_retry(
        1, next_attempt_ms=NOW_MS, error="e", uncertain=False
    ),
    "flag_pending_deadlines_uncertain": lambda tx: tx.flag_pending_deadlines_uncertain(),
    "pending_expired_messages": lambda tx: tx.pending_expired_messages(NOW_MS),
    "messages_created_between": lambda tx: tx.messages_created_between(0, NOW_MS + 1),
    "mark_message_failed": lambda tx: tx.mark_message_failed(1, error="e", now_ms=NOW_MS),
    "mark_message_expired": lambda tx: tx.mark_message_expired(1, NOW_MS),
    "outbox_counts": lambda tx: tx.outbox_counts(),
    "get_state": lambda tx: tx.get_state("k"),
    "set_state": lambda tx: tx.set_state("k", "v", NOW_MS),
    "delete_state": lambda tx: tx.delete_state("k"),
}


def test_every_public_tx_method_is_covered_by_the_call_table() -> None:
    public = {name for name in vars(Tx) if not name.startswith("_")}

    assert public == set(TX_CALLS)


@pytest.mark.parametrize("name", sorted(TX_CALLS))
def test_tx_is_dead_after_its_block(store: Store, name: str) -> None:
    with store.transaction() as tx:
        pass

    with pytest.raises(StoreError, match="transaction kapandı"):
        TX_CALLS[name](tx)

    with store.transaction() as fresh:
        assert fresh.queue_depth() == 0
        assert fresh.outbox_counts()[OutboxStatus.PENDING] == 0
        assert fresh.get_state("k") is None


def test_tx_is_dead_after_a_rolled_back_block(store: Store) -> None:
    with pytest.raises(Boom), store.transaction() as tx:
        raise Boom

    with pytest.raises(StoreError, match="transaction kapandı"):
        tx.insert_signal(ENTRY)


@pytest.mark.parametrize("name", sorted(TX_CALLS))
def test_tx_refuses_other_threads(store: Store, name: str) -> None:
    errors: list[BaseException] = []

    def intruder(tx: Tx) -> None:
        try:
            TX_CALLS[name](tx)
        except BaseException as exc:
            errors.append(exc)

    with store.transaction() as tx:
        thread = threading.Thread(target=intruder, args=(tx,))
        thread.start()
        thread.join(timeout=10)
        assert not thread.is_alive()

    assert len(errors) == 1
    assert isinstance(errors[0], StoreError)
    assert "başka bir thread" in str(errors[0])
    with store.transaction() as fresh:
        assert fresh.queue_depth() == 0
        assert fresh.get_state("k") is None


def test_tx_cannot_be_built_outside_store_transaction(file_store: Store) -> None:
    conn = file_store._conn
    assert conn is not None

    with pytest.raises(StoreError, match=r"yalnızca Store\.transaction\(\)"):
        Tx(conn, object())
    with pytest.raises(TypeError):
        Tx()  # type: ignore[call-arg]


def test_transaction_waits_for_another_process_holding_the_write_lock(
    file_store: Store, db_path: Path
) -> None:
    other_process = sqlite3.connect(db_path, isolation_level=None, check_same_thread=False)
    with closing(other_process) as other:
        other.execute("BEGIN IMMEDIATE")
        other.execute("INSERT INTO state (key, value, updated_at_ms) VALUES ('k', 'theirs', 1)")
        timer = threading.Timer(0.2, lambda: other.execute("COMMIT"))
        timer.start()
        try:
            # busy_timeout (5 s) is far longer than the 0.2 s the other writer needs.
            with file_store.transaction() as tx:
                assert tx.get_state("k") == "theirs"
        finally:
            timer.join()


def test_write_lock_held_too_long_is_a_store_error_and_the_store_recovers(
    file_store: Store, db_path: Path
) -> None:
    conn = file_store._conn
    assert conn is not None
    conn.execute("PRAGMA busy_timeout = 50")  # keep the test fast; production waits 5 s

    with closing(sqlite3.connect(db_path, isolation_level=None)) as other:
        other.execute("BEGIN IMMEDIATE")
        with pytest.raises(StoreError, match="database is locked"):
            file_store.transaction().__enter__()
        other.execute("ROLLBACK")

    assert insert(file_store) == 1


def test_failed_commit_rolls_back_and_raises_store_error(file_store: Store, db_path: Path) -> None:
    conn = file_store._conn
    assert conn is not None

    def write_a_dangling_reference() -> None:
        with file_store.transaction() as tx:
            tx.insert_signal(ENTRY)
            # Postpone the foreign-key check to COMMIT so that the COMMIT itself fails.
            conn.execute("PRAGMA defer_foreign_keys = ON")
            tx.add_budget_entry(signal_id=999, prop_day=DAY, risk_amount=1.0, now_ms=NOW_MS)

    with pytest.raises(StoreError, match="FOREIGN KEY constraint failed"):
        write_a_dangling_reference()

    assert raw_execute(db_path, "SELECT COUNT(*) FROM signals") == [(0,)]
    assert raw_execute(db_path, "SELECT COUNT(*) FROM budget_entries") == [(0,)]
    assert insert(file_store) is not None


# --------------------------------------------------------------------------------------------
# Signals
# --------------------------------------------------------------------------------------------


@pytest.mark.parametrize("signal", [ENTRY, HEARTBEAT, EXIT], ids=["entry", "heartbeat", "exit"])
def test_inserted_signal_reads_back_unchanged_and_queued(store: Store, signal: NewSignal) -> None:
    with store.transaction() as tx:
        signal_id = tx.insert_signal(signal)
        assert signal_id == 1
        row = tx.get_signal(signal_id)

    assert row == _as_row(signal, 1)
    assert row is not None
    assert row.status is SignalStatus.QUEUED
    assert row.outcome is None
    assert row.processed_at_ms is None


def test_signal_ids_increase_in_insertion_order(store: Store) -> None:
    assert insert_bars(store, 3) == [1, 2, 3]


def test_whole_numbers_in_price_columns_come_back_as_floats(store: Store) -> None:
    signal_id = insert(store, est_entry=5123, stop=5110, tp=5148, rr=2)

    with store.transaction() as tx:
        row = tx.get_signal(signal_id)

    assert row is not None
    for value in (row.est_entry, row.stop, row.tp, row.rr):
        assert type(value) is float
    assert (row.est_entry, row.stop, row.tp, row.rr) == (5123.0, 5110.0, 5148.0, 2.0)


def test_enum_members_are_accepted_for_text_fields_and_stored_as_plain_text(store: Store) -> None:
    class Action(StrEnum):
        ENTRY = "entry"

    signal_id = insert(store, action=Action.ENTRY)

    with store.transaction() as tx:
        row = tx.get_signal(signal_id)
        assert row is not None
        assert type(row.action) is str
        assert row.action == "entry"
        assert tx.last_received_ms(action=Action.ENTRY) == NOW_MS


def test_duplicate_signal_returns_none_and_keeps_the_first_row(store: Store) -> None:
    first = insert(store)
    later = dataclasses.replace(
        ENTRY, received_at_ms=NOW_MS + 5000, stop=1.0, payload_json='{"other":true}'
    )

    with store.transaction() as tx:
        assert tx.insert_signal(later) is None
        # Also within one transaction.
        other = dataclasses.replace(ENTRY, bar_time_ms=bar(1))
        second = tx.insert_signal(other)
        assert second is not None
        assert tx.insert_signal(other) is None

    with store.transaction() as tx:
        assert tx.get_signal(first) == _as_row(ENTRY, first)
        assert tx.queue_depth() == 2


def test_duplicate_is_still_refused_after_the_first_was_processed(store: Store) -> None:
    first = insert(store)
    with store.transaction() as tx:
        tx.complete_signal(first, Outcome.FORWARDED, NOW_MS)

    with store.transaction() as tx:
        assert tx.insert_signal(ENTRY) is None
        assert tx.queue_depth() == 0


@pytest.mark.parametrize(
    "overrides",
    [
        {"strategy_id": "asian_sweep"},
        {"symbol": "CME_MINI:ES1!"},
        {"bar_time_ms": BAR_MS + MINUTE_MS},
        {"action": "stop_update"},
    ],
    ids=["strategy_id", "symbol", "bar_time_ms", "action"],
)
def test_each_part_of_the_idempotency_key_distinguishes_signals(
    store: Store, overrides: dict[str, object]
) -> None:
    insert(store)

    assert insert(store, **overrides) == 2


@pytest.mark.parametrize("field", ["tf", "side", "setup", "received_at_ms", "bar_close_ms"])
def test_fields_outside_the_idempotency_key_do_not_make_a_new_signal(
    store: Store, field: str
) -> None:
    insert(store)
    changed: dict[str, object] = {
        "tf": "5",
        "side": "short",
        "setup": "IFVG",
        "received_at_ms": NOW_MS + 1,
        "bar_close_ms": BAR_MS + 5 * MINUTE_MS,
    }

    with store.transaction() as tx:
        assert tx.insert_signal(dataclasses.replace(ENTRY, **{field: changed[field]})) is None


@pytest.mark.parametrize("field", ["est_entry", "stop", "tp", "rr"])
@pytest.mark.parametrize("value", [float("nan"), float("inf"), float("-inf")])
def test_non_finite_numbers_are_refused(store: Store, field: str, value: float) -> None:
    # SQLite would store NaN as NULL: a stop that silently disappears.
    with (
        store.transaction() as tx,
        pytest.raises(StoreError, match=f"{field} sonlu bir sayı olmalı"),
    ):
        tx.insert_signal(dataclasses.replace(ENTRY, **{field: value}))

    with store.transaction() as tx:
        assert tx.queue_depth() == 0


@pytest.mark.parametrize(
    "overrides",
    [
        {"strategy_id": None},
        {"symbol": None},
        {"tf": None},
        {"action": None},
        {"setup": None},
        {"payload_json": None},
        {"received_at_ms": None},
        {"bar_time_ms": None},
        {"bar_close_ms": None},
        {"bar_time_ms": 2**63},
        {"bar_time_ms": 1.5},
        {"received_at_ms": "soon"},
        {"payload_json": {"v": 1}},
    ],
    ids=lambda overrides: "-".join(f"{k}={v!r}" for k, v in overrides.items()),
)
def test_values_the_schema_cannot_hold_are_store_errors(
    store: Store, overrides: dict[str, object]
) -> None:
    with store.transaction() as tx, pytest.raises(StoreError, match="veritabanı hatası"):
        tx.insert_signal(dataclasses.replace(ENTRY, **overrides))  # type: ignore[arg-type]

    with store.transaction() as tx:
        assert tx.queue_depth() == 0


@pytest.mark.parametrize("field", ["est_entry", "stop", "tp", "rr"])
@pytest.mark.parametrize("value", ["5110.75", "x", True, [5110.75]], ids=repr)
def test_price_columns_take_numbers_only(store: Store, field: str, value: object) -> None:
    # A STRICT REAL column would quietly convert numeric-looking text.
    with store.transaction() as tx, pytest.raises(StoreError, match=f"{field} sayı olmalı"):
        tx.insert_signal(dataclasses.replace(ENTRY, **{field: value}))  # type: ignore[arg-type]

    with store.transaction() as tx:
        assert tx.queue_depth() == 0


def test_duplicates_and_rollbacks_do_not_burn_ids(store: Store) -> None:
    assert insert(store) == 1
    with store.transaction() as tx:
        for _ in range(3):
            assert tx.insert_signal(ENTRY) is None
    with pytest.raises(Boom):
        insert_then_fail(store, dataclasses.replace(ENTRY, bar_time_ms=bar(1)))

    assert insert(store, bar_time_ms=bar(2)) == 2


def test_ids_are_never_reused(file_store: Store, db_path: Path) -> None:
    assert insert_bars(file_store, 2) == [1, 2]
    assert [enqueue(file_store), enqueue(file_store)] == [1, 2]
    # Nothing in the bot deletes rows; if someone does by hand, the ids must still not come back
    # (a later `/atladim <id>` has to mean the signal the user saw).
    raw_execute(db_path, "DELETE FROM signals WHERE id = 2")
    raw_execute(db_path, "DELETE FROM outbox WHERE id = 2")

    assert insert(file_store, bar_time_ms=bar(5)) == 3
    assert enqueue(file_store) == 3


def test_get_signal_returns_none_for_an_unknown_id(store: Store) -> None:
    with store.transaction() as tx:
        assert tx.get_signal(1) is None


def test_queue_is_first_in_first_out_and_skips_processed_signals(store: Store) -> None:
    first, second, third = insert_bars(store, 3)

    with store.transaction() as tx:
        assert tx.queue_depth() == 3
        head = tx.next_queued_signal()
        assert head is not None
        assert head.id == first
        # Reading the head does not consume it.
        again = tx.next_queued_signal()
        assert again == head
        tx.complete_signal(first, Outcome.STALE, NOW_MS)
        tx.complete_signal(third, Outcome.FORWARDED, NOW_MS)
        assert tx.queue_depth() == 1
        head = tx.next_queued_signal()
        assert head is not None
        assert head.id == second
        tx.complete_signal(second, Outcome.SKIP_BUDGET, NOW_MS)
        assert tx.queue_depth() == 0
        assert tx.next_queued_signal() is None


def test_empty_queue(store: Store) -> None:
    with store.transaction() as tx:
        assert tx.next_queued_signal() is None
        assert tx.queue_depth() == 0


@pytest.mark.parametrize("outcome", list(Outcome))
def test_complete_signal_records_outcome_and_time(store: Store, outcome: Outcome) -> None:
    signal_id = insert(store)

    with store.transaction() as tx:
        tx.complete_signal(signal_id, outcome, NOW_MS + 250)
        row = tx.get_signal(signal_id)

    assert row == dataclasses.replace(
        _as_row(ENTRY, signal_id),
        status=SignalStatus.DONE,
        outcome=outcome,
        processed_at_ms=NOW_MS + 250,
    )
    assert row is not None
    assert row.outcome is outcome


def test_completing_a_signal_twice_is_refused_and_keeps_the_first_outcome(store: Store) -> None:
    signal_id = insert(store)
    with store.transaction() as tx:
        tx.complete_signal(signal_id, Outcome.FORWARDED, NOW_MS)

    with store.transaction() as tx:
        with pytest.raises(StoreError, match=f"zaten işlenmiş: id {signal_id}"):
            tx.complete_signal(signal_id, Outcome.STALE, NOW_MS + 1)
        row = tx.get_signal(signal_id)

    assert row is not None
    assert (row.outcome, row.processed_at_ms) == (Outcome.FORWARDED, NOW_MS)


def test_completing_an_unknown_signal_is_refused(store: Store) -> None:
    with store.transaction() as tx, pytest.raises(StoreError, match="sinyal bulunamadı: id 7"):
        tx.complete_signal(7, Outcome.FORWARDED, NOW_MS)


def test_completing_with_an_unknown_outcome_text_is_refused(store: Store) -> None:
    signal_id = insert(store)

    with store.transaction() as tx:
        with pytest.raises(StoreError, match="veritabanı hatası"):
            tx.complete_signal(signal_id, "executed", NOW_MS)  # type: ignore[arg-type]
        assert tx.queue_depth() == 1


def _latest(tx: Tx, **overrides: object) -> int | None:
    query: dict[str, object] = {
        "strategy_id": "ny_orb_fvg",
        "symbol": "CME_MINI:MES1!",
        "action": "entry",
        "min_bar_time_ms": bar(0),
        "max_bar_time_ms": bar(10),
    }
    query.update(overrides)
    row = tx.latest_signal(**query)  # type: ignore[arg-type]
    return None if row is None else row.id


def test_latest_signal_picks_the_highest_bar_time_within_inclusive_bounds(store: Store) -> None:
    # Inserted out of bar order on purpose: the bar time decides, not the id.
    third = insert(store, bar_time_ms=bar(3))
    first = insert(store, bar_time_ms=bar(1))
    second = insert(store, bar_time_ms=bar(2))

    with store.transaction() as tx:
        assert _latest(tx) == third
        assert _latest(tx, max_bar_time_ms=bar(3)) == third
        assert _latest(tx, max_bar_time_ms=bar(3) - 1) == second
        assert _latest(tx, min_bar_time_ms=bar(1), max_bar_time_ms=bar(1)) == first
        assert _latest(tx, min_bar_time_ms=bar(3), max_bar_time_ms=bar(9)) == third
        assert _latest(tx, min_bar_time_ms=bar(3) + 1) is None
        assert _latest(tx, max_bar_time_ms=bar(1) - 1) is None
        assert _latest(tx, min_bar_time_ms=bar(2), max_bar_time_ms=bar(1)) is None


def test_latest_signal_matches_strategy_symbol_and_action_exactly(store: Store) -> None:
    ours = insert(store, bar_time_ms=bar(1))
    other_strategy = insert(store, bar_time_ms=bar(2), strategy_id="asian_sweep")
    other_symbol = insert(store, bar_time_ms=bar(3), symbol="CME_MINI:ES1!")
    other_action = insert(store, bar_time_ms=bar(4), action="stop_update")

    with store.transaction() as tx:
        assert _latest(tx) == ours
        assert _latest(tx, strategy_id="asian_sweep") == other_strategy
        assert _latest(tx, symbol="CME_MINI:ES1!") == other_symbol
        assert _latest(tx, action="stop_update") == other_action
        assert _latest(tx, action="exit") is None
        assert _latest(tx, strategy_id="mtf_trend_cont") is None


def test_latest_signal_outcome_filter(store: Store) -> None:
    forwarded, stale, queued = insert_bars(store, 3)
    with store.transaction() as tx:
        tx.complete_signal(forwarded, Outcome.FORWARDED, NOW_MS)
        tx.complete_signal(stale, Outcome.STALE, NOW_MS)

    with store.transaction() as tx:
        # No filter: any signal, even one that is still queued.
        assert _latest(tx) == queued
        assert _latest(tx, outcome=None) == queued
        assert _latest(tx, outcome=Outcome.FORWARDED) == forwarded
        assert _latest(tx, outcome=Outcome.STALE) == stale
        assert _latest(tx, outcome=Outcome.SKIP_BUDGET) is None
        assert _latest(tx, outcome=Outcome.FORWARDED, min_bar_time_ms=bar(1)) is None


def test_signals_received_between_is_half_open_and_ordered_by_id(store: Store) -> None:
    late = insert(store, bar_time_ms=bar(0), received_at_ms=3000)
    early = insert(store, bar_time_ms=bar(1), received_at_ms=1000)
    middle = insert(store, bar_time_ms=bar(2), received_at_ms=2000)

    def ids(start_ms: int, end_ms: int) -> list[int]:
        with store.transaction() as tx:
            return [row.id for row in tx.signals_received_between(start_ms, end_ms)]

    assert ids(0, 10_000) == [late, early, middle]
    assert ids(1000, 3000) == [early, middle]  # start inclusive, end exclusive
    assert ids(1001, 3001) == [late, middle]
    assert ids(2000, 2000) == []
    assert ids(3001, 9000) == []
    with store.transaction() as tx:
        assert tx.signals_received_between(1000, 1001) == [
            dataclasses.replace(_as_row(ENTRY, early), bar_time_ms=bar(1), received_at_ms=1000)
        ]


def test_last_received_ms(store: Store) -> None:
    with store.transaction() as tx:
        assert tx.last_received_ms() is None
        assert tx.last_received_ms(action="heartbeat") is None

    insert(store, received_at_ms=5000)
    insert(store, HEARTBEAT, received_at_ms=7000)
    insert(store, HEARTBEAT, received_at_ms=6000, bar_time_ms=bar(15))

    with store.transaction() as tx:
        assert tx.last_received_ms() == 7000
        assert tx.last_received_ms(action=None) == 7000
        assert tx.last_received_ms(action="heartbeat") == 7000
        assert tx.last_received_ms(action="entry") == 5000
        assert tx.last_received_ms(action="exit") is None


def test_latency_samples_skip_heartbeats_and_report_the_earliest_send(store: Store) -> None:
    old = insert(store, bar_time_ms=bar(0), received_at_ms=1000)
    insert(store, HEARTBEAT, received_at_ms=2000)
    sent_twice = insert(store, bar_time_ms=bar(1), received_at_ms=2000)
    unsent = insert(store, EXIT, bar_time_ms=bar(2), received_at_ms=3000)
    silent = insert(store, bar_time_ms=bar(3), received_at_ms=4000, action="stop_update")
    with store.transaction() as tx:
        late = tx.enqueue_message(
            kind=OutboxKind.SIGNAL, text="a", now_ms=2100, signal_id=sent_twice
        )
        soon = tx.enqueue_message(
            kind=OutboxKind.SIGNAL, text="b", now_ms=2100, signal_id=sent_twice
        )
        failed = tx.enqueue_message(
            kind=OutboxKind.SIGNAL, text="c", now_ms=2100, signal_id=sent_twice
        )
        tx.enqueue_message(kind=OutboxKind.SIGNAL, text="d", now_ms=3100, signal_id=unsent)
        tx.mark_message_sent(late, 2900)
        tx.mark_message_sent(soon, 2300)
        tx.mark_message_failed(failed, error="e", now_ms=2200)

    with store.transaction() as tx:
        samples = tx.latency_samples(2000)
        everything = tx.latency_samples(0)

    assert samples == [
        LatencySample(
            signal_id=sent_twice,
            strategy_id="ny_orb_fvg",
            action="entry",
            bar_close_ms=ENTRY.bar_close_ms,
            received_at_ms=2000,
            sent_at_ms=2300,
        ),
        LatencySample(
            signal_id=unsent,
            strategy_id="ny_orb_fvg",
            action="exit",
            bar_close_ms=EXIT.bar_close_ms,
            received_at_ms=3000,
            sent_at_ms=None,
        ),
        LatencySample(
            signal_id=silent,
            strategy_id="ny_orb_fvg",
            action="stop_update",
            bar_close_ms=ENTRY.bar_close_ms,
            received_at_ms=4000,
            sent_at_ms=None,
        ),
    ]
    assert [sample.signal_id for sample in everything] == [old, sent_twice, unsent, silent]
    with store.transaction() as tx:
        assert tx.latency_samples(4001) == []


# --------------------------------------------------------------------------------------------
# Daily budget ledger
# --------------------------------------------------------------------------------------------


def test_budget_entry_is_counted_and_reads_back_with_python_types(store: Store) -> None:
    signal_id = insert(store)

    with store.transaction() as tx:
        tx.add_budget_entry(signal_id=signal_id, prop_day=DAY, risk_amount=250, now_ms=NOW_MS)
        entry = tx.get_budget_entry(signal_id)

    assert entry == BudgetRow(
        signal_id=signal_id,
        prop_day=DAY,
        risk_amount=250.0,
        counted=True,
        release_reason=None,
        updated_at_ms=NOW_MS,
    )
    assert entry is not None
    assert type(entry.prop_day) is dt.date
    assert type(entry.counted) is bool
    assert type(entry.risk_amount) is float


def test_prop_day_is_stored_as_iso_text(file_store: Store, db_path: Path) -> None:
    signal_id = insert(file_store)
    with file_store.transaction() as tx:
        tx.add_budget_entry(signal_id=signal_id, prop_day=DAY, risk_amount=1.5, now_ms=NOW_MS)

    assert raw_execute(
        db_path, "SELECT prop_day, typeof(prop_day), typeof(risk_amount) FROM budget_entries"
    ) == [("2026-11-13", "text", "real")]


def test_get_budget_entry_returns_none_without_a_row(store: Store) -> None:
    signal_id = insert(store)

    with store.transaction() as tx:
        assert tx.get_budget_entry(signal_id) is None
        assert tx.get_budget_entry(99) is None


def test_second_budget_entry_for_a_signal_is_refused(store: Store) -> None:
    signal_id = insert(store)

    with store.transaction() as tx:
        tx.add_budget_entry(signal_id=signal_id, prop_day=DAY, risk_amount=250.0, now_ms=NOW_MS)
        with pytest.raises(StoreError, match="UNIQUE constraint failed"):
            tx.add_budget_entry(
                signal_id=signal_id, prop_day=NEXT_DAY, risk_amount=100.0, now_ms=NOW_MS
            )
        assert tx.budget_used(DAY) == 250.0
        assert tx.budget_used(NEXT_DAY) == 0.0


def test_budget_entry_for_an_unknown_signal_is_refused(store: Store) -> None:
    with store.transaction() as tx:
        with pytest.raises(StoreError, match="FOREIGN KEY constraint failed"):
            tx.add_budget_entry(signal_id=42, prop_day=DAY, risk_amount=250.0, now_ms=NOW_MS)
        assert tx.budget_entries(DAY) == []


@pytest.mark.parametrize(
    ("risk_amount", "message"),
    [
        (-0.01, "risk_amount negatif olamaz"),
        (float("nan"), "risk_amount sonlu bir sayı olmalı"),
        (float("inf"), "risk_amount sonlu bir sayı olmalı"),
        (float("-inf"), "risk_amount sonlu bir sayı olmalı"),
    ],
)
def test_impossible_risk_amounts_are_refused(
    store: Store, risk_amount: float, message: str
) -> None:
    signal_id = insert(store)

    with store.transaction() as tx:
        with pytest.raises(StoreError, match=message):
            tx.add_budget_entry(
                signal_id=signal_id, prop_day=DAY, risk_amount=risk_amount, now_ms=NOW_MS
            )
        assert tx.get_budget_entry(signal_id) is None


def test_zero_risk_amount_is_allowed(store: Store) -> None:
    signal_id = insert(store)

    with store.transaction() as tx:
        tx.add_budget_entry(signal_id=signal_id, prop_day=DAY, risk_amount=0.0, now_ms=NOW_MS)
        assert tx.budget_used(DAY) == 0.0
        assert len(tx.budget_entries(DAY)) == 1


@pytest.mark.parametrize(
    "not_a_day",
    [dt.datetime(2026, 11, 13, 0, 0), dt.datetime(2026, 11, 13, tzinfo=dt.UTC), "2026-11-13", None],
    ids=["naive-datetime", "aware-datetime", "text", "none"],
)
def test_prop_day_must_be_a_plain_date(store: Store, not_a_day: object) -> None:
    signal_id = insert(store)
    calls: list[Callable[[Tx], object]] = [
        lambda tx: tx.add_budget_entry(
            signal_id=signal_id,
            prop_day=not_a_day,  # type: ignore[arg-type]
            risk_amount=1.0,
            now_ms=NOW_MS,
        ),
        lambda tx: tx.budget_entries(not_a_day),  # type: ignore[arg-type]
        lambda tx: tx.budget_used(not_a_day),  # type: ignore[arg-type]
    ]

    with store.transaction() as tx:
        for call in calls:
            with pytest.raises(StoreError, match="prop_day saat içermeyen bir tarih"):
                call(tx)
        assert tx.get_budget_entry(signal_id) is None


def _ledger(store: Store) -> tuple[int, int, int]:
    """Three entries: 250 and 100.5 on DAY, 400 on NEXT_DAY. Returns their signal ids."""
    first, second, third = insert_bars(store, 3)
    with store.transaction() as tx:
        # Added out of id order: listing is by signal id, not by insertion.
        tx.add_budget_entry(signal_id=second, prop_day=DAY, risk_amount=100.5, now_ms=NOW_MS)
        tx.add_budget_entry(signal_id=third, prop_day=NEXT_DAY, risk_amount=400.0, now_ms=NOW_MS)
        tx.add_budget_entry(signal_id=first, prop_day=DAY, risk_amount=250.0, now_ms=NOW_MS)
    return first, second, third


def test_budget_entries_lists_one_prop_day_by_signal_id(store: Store) -> None:
    first, second, third = _ledger(store)

    with store.transaction() as tx:
        assert [entry.signal_id for entry in tx.budget_entries(DAY)] == [first, second]
        assert [entry.signal_id for entry in tx.budget_entries(NEXT_DAY)] == [third]
        assert tx.budget_entries(dt.date(2026, 11, 12)) == []


def test_budget_used_sums_the_counted_entries_of_one_prop_day(store: Store) -> None:
    first, second, _ = _ledger(store)

    with store.transaction() as tx:
        assert tx.budget_used(DAY) == 350.5
        assert tx.budget_used(NEXT_DAY) == 400.0
        used_on_empty_day = tx.budget_used(dt.date(2026, 11, 12))
        assert used_on_empty_day == 0.0
        assert type(used_on_empty_day) is float

        tx.set_budget_counted(
            signal_id=first, counted=False, reason=ReleaseReason.SKIPPED, now_ms=NOW_MS
        )
        assert tx.budget_used(DAY) == 100.5
        tx.set_budget_counted(
            signal_id=second, counted=False, reason=ReleaseReason.UNDELIVERED, now_ms=NOW_MS
        )
        assert tx.budget_used(DAY) == 0.0
        assert type(tx.budget_used(DAY)) is float
        # Released entries stay listed; only the sum changes.
        assert len(tx.budget_entries(DAY)) == 2
        assert tx.budget_used(NEXT_DAY) == 400.0


@pytest.mark.parametrize("reason", list(ReleaseReason))
def test_budget_entry_can_be_released_and_counted_again(
    store: Store, reason: ReleaseReason
) -> None:
    first, _, _ = _ledger(store)

    with store.transaction() as tx:
        tx.set_budget_counted(signal_id=first, counted=False, reason=reason, now_ms=NOW_MS + 10)
        released = tx.get_budget_entry(first)
        tx.set_budget_counted(signal_id=first, counted=True, reason=None, now_ms=NOW_MS + 20)
        counted = tx.get_budget_entry(first)

    assert released == BudgetRow(
        signal_id=first,
        prop_day=DAY,
        risk_amount=250.0,
        counted=False,
        release_reason=reason,
        updated_at_ms=NOW_MS + 10,
    )
    assert released is not None
    assert released.release_reason is reason
    assert counted == BudgetRow(
        signal_id=first,
        prop_day=DAY,
        risk_amount=250.0,
        counted=True,
        release_reason=None,
        updated_at_ms=NOW_MS + 20,
    )


def test_release_reason_can_change_while_released(store: Store) -> None:
    first, _, _ = _ledger(store)

    with store.transaction() as tx:
        tx.set_budget_counted(
            signal_id=first, counted=False, reason=ReleaseReason.SKIPPED, now_ms=NOW_MS
        )
        tx.set_budget_counted(
            signal_id=first, counted=False, reason=ReleaseReason.UNDELIVERED, now_ms=NOW_MS
        )
        entry = tx.get_budget_entry(first)

    assert entry is not None
    assert (entry.counted, entry.release_reason) == (False, ReleaseReason.UNDELIVERED)


@pytest.mark.parametrize(
    ("counted", "reason", "message"),
    [
        (True, ReleaseReason.SKIPPED, "serbest bırakma nedeni olamaz"),
        (True, ReleaseReason.UNDELIVERED, "serbest bırakma nedeni olamaz"),
        (False, None, "neden gerekir"),
    ],
)
def test_counted_flag_and_release_reason_must_agree(
    store: Store, counted: bool, reason: ReleaseReason | None, message: str
) -> None:
    first, _, _ = _ledger(store)

    with store.transaction() as tx:
        before = tx.get_budget_entry(first)
        with pytest.raises(StoreError, match=message):
            tx.set_budget_counted(
                signal_id=first, counted=counted, reason=reason, now_ms=NOW_MS + 99
            )
        assert tx.get_budget_entry(first) == before


def test_unknown_release_reason_text_is_refused(store: Store) -> None:
    first, _, _ = _ledger(store)

    with store.transaction() as tx:
        with pytest.raises(StoreError, match="veritabanı hatası"):
            tx.set_budget_counted(
                signal_id=first,
                counted=False,
                reason="forgot",  # type: ignore[arg-type]
                now_ms=NOW_MS,
            )
        assert tx.budget_used(DAY) == 350.5


@pytest.mark.parametrize(
    ("counted", "reason"), [(False, ReleaseReason.SKIPPED), (True, None)], ids=["release", "count"]
)
def test_changing_a_missing_budget_entry_is_refused(
    store: Store, counted: bool, reason: ReleaseReason | None
) -> None:
    signal_id = insert(store)

    with (
        store.transaction() as tx,
        pytest.raises(StoreError, match=f"bütçe kaydı bulunamadı: sinyal id {signal_id}"),
    ):
        tx.set_budget_counted(signal_id=signal_id, counted=counted, reason=reason, now_ms=NOW_MS)


# --------------------------------------------------------------------------------------------
# Outbox
# --------------------------------------------------------------------------------------------


def test_enqueued_message_is_pending_and_due_immediately(store: Store) -> None:
    with store.transaction() as tx:
        message_id = tx.enqueue_message(kind=OutboxKind.NOTICE, text="merhaba", now_ms=NOW_MS)
        row = tx.get_message(message_id)

    assert message_id == 1
    assert row == OutboxRow(
        id=1,
        created_at_ms=NOW_MS,
        kind=OutboxKind.NOTICE,
        signal_id=None,
        text="merhaba",
        expires_at_ms=None,
        expired_text=None,
        status=OutboxStatus.PENDING,
        attempts=0,
        next_attempt_ms=NOW_MS,
        sent_at_ms=None,
        last_error=None,
        delivery_uncertain=False,
    )


@pytest.mark.parametrize("kind", list(OutboxKind))
def test_message_keeps_kind_signal_and_expiry(store: Store, kind: OutboxKind) -> None:
    signal_id = insert(store)

    with store.transaction() as tx:
        message_id = tx.enqueue_message(
            kind=kind,
            text="[SİNYAL] NY ORB FVG · FVG · LONG\nStop 5110.75 (−12.50 puan)",
            now_ms=NOW_MS,
            signal_id=signal_id,
            expires_at_ms=NOW_MS + 120_000,
            expired_text="[İLETİLEMEDİ — ATLA]",
        )
        row = tx.get_message(message_id)

    assert row is not None
    assert row.kind is kind
    assert row.signal_id == signal_id
    assert row.text == "[SİNYAL] NY ORB FVG · FVG · LONG\nStop 5110.75 (−12.50 puan)"
    assert (row.expires_at_ms, row.expired_text) == (NOW_MS + 120_000, "[İLETİLEMEDİ — ATLA]")


def test_message_for_an_unknown_signal_is_refused(store: Store) -> None:
    with store.transaction() as tx:
        with pytest.raises(StoreError, match="FOREIGN KEY constraint failed"):
            tx.enqueue_message(kind=OutboxKind.SIGNAL, text="x", now_ms=NOW_MS, signal_id=5)
        assert tx.next_pending_message() is None


def test_unknown_message_kind_text_is_refused(store: Store) -> None:
    with store.transaction() as tx:
        with pytest.raises(StoreError, match="veritabanı hatası"):
            tx.enqueue_message(kind="spam", text="x", now_ms=NOW_MS)  # type: ignore[arg-type]
        assert tx.outbox_counts()[OutboxStatus.PENDING] == 0


def test_get_message_returns_none_for_an_unknown_id(store: Store) -> None:
    with store.transaction() as tx:
        assert tx.get_message(1) is None


def test_outbox_is_first_in_first_out_whatever_the_retry_time(store: Store) -> None:
    first, second, third = (enqueue(store, text) for text in ("a", "b", "c"))

    with store.transaction() as tx:
        head = tx.next_pending_message()
        assert head is not None
        assert head.id == first
        # A head waiting for its retry still blocks the queue: order is never changed.
        tx.mark_message_retry(
            first, next_attempt_ms=NOW_MS + 300_000, error="timeout", uncertain=False
        )
        head = tx.next_pending_message()
        assert head is not None
        assert (head.id, head.next_attempt_ms) == (first, NOW_MS + 300_000)

        tx.mark_message_sent(first, NOW_MS)
        tx.mark_message_failed(third, error="bad request", now_ms=NOW_MS)
        head = tx.next_pending_message()
        assert head is not None
        assert head.id == second
        tx.mark_message_expired(second, NOW_MS)
        assert tx.next_pending_message() is None


def test_empty_outbox(store: Store) -> None:
    with store.transaction() as tx:
        assert tx.next_pending_message() is None
        assert tx.outbox_counts() == dict.fromkeys(OutboxStatus, 0)
        assert tx.messages_for_signal(1) == []


def test_messages_for_signal_lists_only_that_signal_by_id(store: Store) -> None:
    first, second = insert_bars(store, 2)
    a = enqueue(store, "a", signal_id=first, kind=OutboxKind.SIGNAL)
    enqueue(store, "other", signal_id=second, kind=OutboxKind.SIGNAL)
    enqueue(store, "no signal")
    b = enqueue(store, "b", signal_id=first, kind=OutboxKind.NOTICE)
    with store.transaction() as tx:
        tx.mark_message_sent(a, NOW_MS)

    with store.transaction() as tx:
        rows = tx.messages_for_signal(first)

    assert [(row.id, row.text, row.status) for row in rows] == [
        (a, "a", OutboxStatus.SENT),
        (b, "b", OutboxStatus.PENDING),
    ]


def test_mark_sent(file_store: Store, db_path: Path) -> None:
    message_id = enqueue(file_store)

    with file_store.transaction() as tx:
        before = tx.get_message(message_id)
        tx.mark_message_sent(message_id, NOW_MS + 700)
        after = tx.get_message(message_id)

    assert before is not None
    assert after == dataclasses.replace(before, status=OutboxStatus.SENT, sent_at_ms=NOW_MS + 700)
    assert raw_execute(db_path, "SELECT closed_at_ms FROM outbox") == [(NOW_MS + 700,)]


def test_mark_retry_counts_attempts_and_keeps_the_message_pending(store: Store) -> None:
    message_id = enqueue(store)

    with store.transaction() as tx:
        before = tx.get_message(message_id)
        tx.mark_message_retry(
            message_id, next_attempt_ms=NOW_MS + 2000, error="Timed out", uncertain=False
        )
        once = tx.get_message(message_id)
        tx.mark_message_retry(
            message_id, next_attempt_ms=NOW_MS + 6000, error="Flood control", uncertain=False
        )
        twice = tx.get_message(message_id)

    assert before is not None
    assert once == dataclasses.replace(
        before, attempts=1, next_attempt_ms=NOW_MS + 2000, last_error="Timed out"
    )
    assert twice == dataclasses.replace(
        before, attempts=2, next_attempt_ms=NOW_MS + 6000, last_error="Flood control"
    )


def test_sending_after_retries_keeps_the_attempt_count_and_last_error(store: Store) -> None:
    message_id = enqueue(store)

    with store.transaction() as tx:
        tx.mark_message_retry(
            message_id, next_attempt_ms=NOW_MS + 2000, error="Timed out", uncertain=False
        )
        tx.mark_message_sent(message_id, NOW_MS + 2500)
        row = tx.get_message(message_id)

    assert row is not None
    assert (row.status, row.attempts, row.last_error, row.sent_at_ms) == (
        OutboxStatus.SENT,
        1,
        "Timed out",
        NOW_MS + 2500,
    )


def test_mark_failed(file_store: Store, db_path: Path) -> None:
    message_id = enqueue(file_store)

    with file_store.transaction() as tx:
        before = tx.get_message(message_id)
        tx.mark_message_retry(
            message_id, next_attempt_ms=NOW_MS + 2000, error="Timed out", uncertain=False
        )
        tx.mark_message_failed(message_id, error="Forbidden: bot was blocked", now_ms=NOW_MS + 9)
        after = tx.get_message(message_id)

    assert before is not None
    assert after == dataclasses.replace(
        before,
        status=OutboxStatus.FAILED,
        attempts=1,
        next_attempt_ms=NOW_MS + 2000,
        last_error="Forbidden: bot was blocked",
    )
    assert raw_execute(db_path, "SELECT sent_at_ms, closed_at_ms FROM outbox") == [
        (None, NOW_MS + 9)
    ]


def test_mark_expired(file_store: Store, db_path: Path) -> None:
    message_id = enqueue(file_store, expires_at_ms=NOW_MS + 10, expired_text="geç kaldı")

    with file_store.transaction() as tx:
        before = tx.get_message(message_id)
        tx.mark_message_expired(message_id, NOW_MS + 11)
        after = tx.get_message(message_id)

    assert before is not None
    assert after == dataclasses.replace(before, status=OutboxStatus.EXPIRED)
    assert raw_execute(db_path, "SELECT sent_at_ms, closed_at_ms FROM outbox") == [
        (None, NOW_MS + 11)
    ]


def test_stored_error_text_is_bounded(store: Store) -> None:
    retried, failed = enqueue(store), enqueue(store)
    long_error = "x" * (MAX_ERROR_CHARS - 1) + "YZ" + "tail" * 100

    with store.transaction() as tx:
        tx.mark_message_retry(retried, next_attempt_ms=NOW_MS, error=long_error, uncertain=False)
        tx.mark_message_failed(failed, error=long_error, now_ms=NOW_MS)
        errors = [tx.get_message(message_id).last_error for message_id in (retried, failed)]  # type: ignore[union-attr]

    assert errors == [long_error[:MAX_ERROR_CHARS]] * 2
    assert errors[0] is not None
    assert len(errors[0]) == MAX_ERROR_CHARS == 500
    assert errors[0].endswith("Y")


MARKS: dict[str, Callable[[Tx, int], None]] = {
    "sent": lambda tx, message_id: tx.mark_message_sent(message_id, NOW_MS + 50),
    "retry": lambda tx, message_id: tx.mark_message_retry(
        message_id, next_attempt_ms=NOW_MS + 50, error="again", uncertain=False
    ),
    "failed": lambda tx, message_id: tx.mark_message_failed(
        message_id, error="again", now_ms=NOW_MS + 50
    ),
    "expired": lambda tx, message_id: tx.mark_message_expired(message_id, NOW_MS + 50),
}
FINAL_STATUS: dict[str, OutboxStatus] = {
    "sent": OutboxStatus.SENT,
    "failed": OutboxStatus.FAILED,
    "expired": OutboxStatus.EXPIRED,
}


@pytest.mark.parametrize("second", sorted(MARKS))
@pytest.mark.parametrize("first", sorted(FINAL_STATUS))
def test_marking_a_message_that_is_not_pending_is_refused(
    store: Store, first: str, second: str
) -> None:
    message_id = enqueue(store, text="GİZLİ-METİN")
    with store.transaction() as tx:
        MARKS[first](tx, message_id)
        settled = tx.get_message(message_id)

    with store.transaction() as tx:
        with pytest.raises(StoreError) as caught:
            MARKS[second](tx, message_id)
        assert tx.get_message(message_id) == settled

    assert settled is not None
    assert settled.status is FINAL_STATUS[first]
    assert str(caught.value) == f"mesaj beklemede değil ({first}): id {message_id}"


@pytest.mark.parametrize("mark", sorted(MARKS))
def test_marking_an_unknown_message_is_refused(store: Store, mark: str) -> None:
    enqueue(store)

    with store.transaction() as tx:
        with pytest.raises(StoreError, match="mesaj bulunamadı: id 99"):
            MARKS[mark](tx, 99)
        assert tx.outbox_counts()[OutboxStatus.PENDING] == 1


def test_a_new_message_is_not_delivery_uncertain(store: Store) -> None:
    message_id = enqueue(store)

    with store.transaction() as tx:
        row = tx.get_message(message_id)

    assert row is not None
    assert row.delivery_uncertain is False


def test_an_uncertain_retry_sets_a_sticky_flag(store: Store) -> None:
    message_id = enqueue(store)
    flags: list[bool] = []

    for uncertain in (False, True, False):
        with store.transaction() as tx:
            tx.mark_message_retry(
                message_id, next_attempt_ms=NOW_MS, error="e", uncertain=uncertain
            )
            row = tx.get_message(message_id)
        assert row is not None
        flags.append(row.delivery_uncertain)

    # A later attempt that provably never left does not clear what an earlier one left open.
    assert flags == [False, True, True]


def test_startup_flags_only_pending_messages_that_have_a_deadline(store: Store) -> None:
    signal_id = insert(store)
    entry = enqueue(store, kind=OutboxKind.SIGNAL, signal_id=signal_id, expires_at_ms=NOW_MS + 60)
    notice = enqueue(store)
    sent_entry = enqueue(store, kind=OutboxKind.SIGNAL, expires_at_ms=NOW_MS + 60)
    with store.transaction() as tx:
        tx.mark_message_sent(sent_entry, NOW_MS)

    with store.transaction() as tx:
        first = tx.flag_pending_deadlines_uncertain()
        second = tx.flag_pending_deadlines_uncertain()
        flags = {
            message_id: row.delivery_uncertain
            for message_id in (entry, notice, sent_entry)
            if (row := tx.get_message(message_id)) is not None
        }

    assert (first, second) == (1, 0)
    assert flags == {entry: True, notice: False, sent_entry: False}


def test_pending_expired_messages_are_found_anywhere_in_the_queue(store: Store) -> None:
    head = enqueue(store, "stop update")  # no deadline: never expires
    expired = enqueue(store, "entry 1", kind=OutboxKind.SIGNAL, expires_at_ms=NOW_MS - 1)
    on_the_deadline = enqueue(store, "entry 2", kind=OutboxKind.SIGNAL, expires_at_ms=NOW_MS)
    also_expired = enqueue(store, "entry 3", kind=OutboxKind.SIGNAL, expires_at_ms=NOW_MS - 500)
    closed = enqueue(store, "entry 4", kind=OutboxKind.SIGNAL, expires_at_ms=NOW_MS - 1)
    with store.transaction() as tx:
        tx.mark_message_expired(closed, NOW_MS)

    with store.transaction() as tx:
        found = [row.id for row in tx.pending_expired_messages(NOW_MS)]
        later = [row.id for row in tx.pending_expired_messages(NOW_MS + 1)]
        still_head = tx.next_pending_message()

    assert found == [expired, also_expired]  # oldest first; the deadline itself still counts
    assert later == [expired, on_the_deadline, also_expired]
    assert still_head is not None
    assert still_head.id == head


def test_messages_created_between_is_half_open_and_ordered(store: Store) -> None:
    early = enqueue(store, "a", now_ms=NOW_MS - 10)
    first = enqueue(store, "b", now_ms=NOW_MS)
    second = enqueue(store, "c", kind=OutboxKind.ALARM, now_ms=NOW_MS + 5)
    at_the_end = enqueue(store, "d", now_ms=NOW_MS + 10)

    with store.transaction() as tx:
        rows = tx.messages_created_between(NOW_MS, NOW_MS + 10)
        everything = tx.messages_created_between(0, NOW_MS + 11)

    assert [row.id for row in rows] == [first, second]
    assert [row.kind for row in rows] == [OutboxKind.NOTICE, OutboxKind.ALARM]
    assert [row.id for row in everything] == [early, first, second, at_the_end]


def test_outbox_counts_has_every_status(store: Store) -> None:
    ids = [enqueue(store, str(i)) for i in range(7)]
    with store.transaction() as tx:
        tx.mark_message_sent(ids[0], NOW_MS)
        tx.mark_message_sent(ids[1], NOW_MS)
        tx.mark_message_sent(ids[2], NOW_MS)
        tx.mark_message_failed(ids[3], error="e", now_ms=NOW_MS)
        tx.mark_message_retry(ids[4], next_attempt_ms=NOW_MS, error="e", uncertain=False)

    with store.transaction() as tx:
        counts = tx.outbox_counts()

    assert counts == {
        OutboxStatus.PENDING: 3,
        OutboxStatus.SENT: 3,
        OutboxStatus.EXPIRED: 0,
        OutboxStatus.FAILED: 1,
    }
    assert list(counts) == list(OutboxStatus)


# --------------------------------------------------------------------------------------------
# Throttled alarms
# --------------------------------------------------------------------------------------------

THROTTLE_MS = 600_000


def _alarm(
    tx: Tx, now_ms: int, *, code: str = "bad_schema", throttle_ms: int = THROTTLE_MS
) -> bool:
    return tx.enqueue_alarm(
        code=code, text=f"[ALARM] {code} @{now_ms}", now_ms=now_ms, throttle_ms=throttle_ms
    )


def _alarm_texts(tx: Tx) -> list[str]:
    texts: list[str] = []
    while (message := tx.next_pending_message()) is not None:
        assert message.kind is OutboxKind.ALARM
        texts.append(message.text)
        tx.mark_message_sent(message.id, NOW_MS)
    return texts


def test_first_alarm_is_enqueued_and_remembered_in_the_state_table(store: Store) -> None:
    with store.transaction() as tx:
        assert _alarm(tx, NOW_MS) is True
        message = tx.next_pending_message()
        remembered = tx.get_state("alarm:bad_schema")

    assert message == OutboxRow(
        id=1,
        created_at_ms=NOW_MS,
        kind=OutboxKind.ALARM,
        signal_id=None,
        text=f"[ALARM] bad_schema @{NOW_MS}",
        expires_at_ms=None,
        expired_text=None,
        status=OutboxStatus.PENDING,
        attempts=0,
        next_attempt_ms=NOW_MS,
        sent_at_ms=None,
        last_error=None,
        delivery_uncertain=False,
    )
    assert remembered == str(NOW_MS)
    assert ALARM_STATE_PREFIX == "alarm:"


def test_alarm_is_throttled_until_exactly_throttle_ms_have_passed(store: Store) -> None:
    with store.transaction() as tx:
        assert _alarm(tx, NOW_MS) is True
        assert _alarm(tx, NOW_MS) is False
        assert _alarm(tx, NOW_MS + 1) is False
        assert _alarm(tx, NOW_MS + THROTTLE_MS - 1) is False
        # "Less than throttle_ms ago" is throttled; exactly throttle_ms ago is not.
        assert _alarm(tx, NOW_MS + THROTTLE_MS) is True
        assert _alarm(tx, NOW_MS + THROTTLE_MS + 1) is False

        assert _alarm_texts(tx) == [
            f"[ALARM] bad_schema @{NOW_MS}",
            f"[ALARM] bad_schema @{NOW_MS + THROTTLE_MS}",
        ]
        assert tx.get_state("alarm:bad_schema") == str(NOW_MS + THROTTLE_MS)


def test_suppressed_alarms_do_not_extend_the_throttle_window(store: Store) -> None:
    with store.transaction() as tx:
        assert _alarm(tx, NOW_MS) is True
        for minutes in range(1, 10):
            assert _alarm(tx, NOW_MS + minutes * MINUTE_MS) is False
        assert tx.get_state("alarm:bad_schema") == str(NOW_MS)
        assert _alarm(tx, NOW_MS + 10 * MINUTE_MS) is True


def test_alarm_codes_are_throttled_independently(store: Store) -> None:
    with store.transaction() as tx:
        assert _alarm(tx, NOW_MS, code="bad_schema") is True
        assert _alarm(tx, NOW_MS + 1, code="clock_early") is True
        assert _alarm(tx, NOW_MS + 2, code="side_mismatch") is True
        assert _alarm(tx, NOW_MS + 3, code="bad_schema") is False
        assert _alarm(tx, NOW_MS + 3, code="clock_early") is False

        assert len(_alarm_texts(tx)) == 3
        assert tx.get_state("alarm:clock_early") == str(NOW_MS + 1)


def test_throttle_survives_transactions_and_reopening(db_path: Path) -> None:
    with Store(db_path) as first, first.transaction() as tx:
        assert _alarm(tx, NOW_MS) is True

    with Store(db_path) as second:
        with second.transaction() as tx:
            assert _alarm(tx, NOW_MS + 1000) is False
        with second.transaction() as tx:
            assert _alarm(tx, NOW_MS + THROTTLE_MS) is True


@pytest.mark.parametrize("throttle_ms", [0, -1])
def test_alarm_without_a_throttle_is_always_enqueued(store: Store, throttle_ms: int) -> None:
    with store.transaction() as tx:
        assert _alarm(tx, NOW_MS, throttle_ms=throttle_ms) is True
        assert _alarm(tx, NOW_MS, throttle_ms=throttle_ms) is True
        assert len(_alarm_texts(tx)) == 2


def test_clock_that_moved_backwards_does_not_silence_an_alarm(store: Store) -> None:
    with store.transaction() as tx:
        assert _alarm(tx, NOW_MS) is True
        assert _alarm(tx, NOW_MS - 1) is True
        assert tx.get_state("alarm:bad_schema") == str(NOW_MS - 1)
        assert _alarm(tx, NOW_MS) is False


@pytest.mark.parametrize("corrupt", ["", "yesterday", "12.5", "0x10"])
def test_unreadable_throttle_state_does_not_silence_an_alarm(store: Store, corrupt: str) -> None:
    with store.transaction() as tx:
        tx.set_state("alarm:bad_schema", corrupt, NOW_MS)

        assert _alarm(tx, NOW_MS + 1) is True
        assert tx.get_state("alarm:bad_schema") == str(NOW_MS + 1)
        assert _alarm(tx, NOW_MS + 2) is False


def test_alarm_needs_a_code(store: Store) -> None:
    with store.transaction() as tx:
        with pytest.raises(StoreError, match="alarm kodu boş olamaz"):
            _alarm(tx, NOW_MS, code="")
        assert tx.next_pending_message() is None
        assert tx.get_state("alarm:") is None


# --------------------------------------------------------------------------------------------
# State
# --------------------------------------------------------------------------------------------


def test_state_set_get_overwrite_delete(file_store: Store, db_path: Path) -> None:
    with file_store.transaction() as tx:
        assert tx.get_state("k") is None
        tx.set_state("k", "1", NOW_MS)
        assert tx.get_state("k") == "1"
        tx.set_state("k", "", NOW_MS + 5)
        assert tx.get_state("k") == ""

    assert raw_execute(db_path, "SELECT key, value, updated_at_ms FROM state") == [
        ("k", "", NOW_MS + 5)
    ]

    with file_store.transaction() as tx:
        tx.delete_state("k")
        assert tx.get_state("k") is None
        tx.delete_state("k")  # deleting an absent key is not an error
        tx.delete_state("never-set")

    assert raw_execute(db_path, "SELECT COUNT(*) FROM state") == [(0,)]


@pytest.mark.parametrize("key", list(StateKey))
def test_state_keys_are_independent_and_readable_as_plain_text(store: Store, key: StateKey) -> None:
    with store.transaction() as tx:
        for other in StateKey:
            tx.set_state(other, f"value-of-{other.value}", NOW_MS)
        tx.set_state(key, "changed", NOW_MS + 1)
        tx.delete_state(next(other for other in StateKey if other is not key))

    with store.transaction() as tx:
        # Enum member and its plain text address the same row.
        assert tx.get_state(key) == tx.get_state(key.value) == "changed"
        assert type(tx.get_state(key)) is str
        remaining = [other for other in StateKey if tx.get_state(other) is not None]
        assert len(remaining) == len(StateKey) - 1


def test_state_value_must_be_text(store: Store) -> None:
    with store.transaction() as tx:
        with pytest.raises(StoreError, match="veritabanı hatası"):
            tx.set_state("k", None, NOW_MS)  # type: ignore[arg-type]
        assert tx.get_state("k") is None


# --------------------------------------------------------------------------------------------
# Reading back rows the schema should have made impossible (fail closed)
# --------------------------------------------------------------------------------------------


def _populated(path: Path) -> Store:
    """A store with one processed signal, its budget entry and one sent message (all id 1)."""
    populated = Store(path)
    with populated.transaction() as tx:
        signal_id = tx.insert_signal(ENTRY)
        assert signal_id == 1
        tx.complete_signal(1, Outcome.FORWARDED, NOW_MS)
        tx.add_budget_entry(signal_id=1, prop_day=DAY, risk_amount=250.0, now_ms=NOW_MS)
        tx.set_budget_counted(
            signal_id=1, counted=False, reason=ReleaseReason.SKIPPED, now_ms=NOW_MS
        )
        tx.enqueue_message(kind=OutboxKind.SIGNAL, text="x", now_ms=NOW_MS, signal_id=1)
    return populated


CORRUPTIONS: dict[str, tuple[str, str, list[str]]] = {
    # name -> (tampering statement, expected message fragment, Tx calls that must refuse)
    "signal-status": (
        "UPDATE signals SET status = 'archived'",
        "signals.status sütununda bilinmeyen değer: 'archived'",
        ["get_signal", "latest_signal", "signals_received_between"],
    ),
    "signal-outcome": (
        "UPDATE signals SET outcome = 'executed'",
        "signals.outcome sütununda bilinmeyen değer: 'executed'",
        ["get_signal", "latest_signal", "signals_received_between"],
    ),
    "queued-signal-status-case": (
        "UPDATE signals SET status = 'QUEUED', outcome = NULL, processed_at_ms = NULL",
        "signals.status sütununda bilinmeyen değer: 'QUEUED'",
        ["get_signal"],
    ),
    "outbox-kind": (
        "UPDATE outbox SET kind = 'broadcast'",
        "outbox.kind sütununda bilinmeyen değer: 'broadcast'",
        ["next_pending_message", "get_message", "messages_for_signal"],
    ),
    "outbox-status": (
        "UPDATE outbox SET status = 'queued'",
        "outbox.status sütununda bilinmeyen değer: 'queued'",
        ["get_message", "messages_for_signal", "outbox_counts"],
    ),
    "release-reason": (
        "UPDATE budget_entries SET release_reason = 'forgot'",
        "budget_entries.release_reason sütununda bilinmeyen değer: 'forgot'",
        ["get_budget_entry", "budget_entries"],
    ),
    "counted-flag": (
        "UPDATE budget_entries SET counted = 2",
        "budget_entries.counted sütununda 0/1 dışında bir değer var: '2'",
        ["get_budget_entry", "budget_entries"],
    ),
    "prop-day-impossible-date": (
        "UPDATE budget_entries SET prop_day = '2026-13-45'",
        "budget_entries.prop_day sütununda geçersiz tarih: '2026-13-45'",
        ["get_budget_entry"],
    ),
    "prop-day-week-format": (
        "UPDATE budget_entries SET prop_day = '2026-W46-5'",
        "budget_entries.prop_day sütununda geçersiz tarih: '2026-W46-5'",
        ["get_budget_entry"],
    ),
    "prop-day-basic-format": (
        "UPDATE budget_entries SET prop_day = '20261113'",
        "budget_entries.prop_day sütununda geçersiz tarih: '20261113'",
        ["get_budget_entry"],
    ),
}


@pytest.mark.parametrize("name", sorted(CORRUPTIONS))
def test_unknown_text_read_back_from_the_database_fails_closed(db_path: Path, name: str) -> None:
    statement, message, calls = CORRUPTIONS[name]
    with _populated(db_path) as populated:
        raw_execute(db_path, statement, ignore_checks=True)

        for call in calls:
            with populated.transaction() as tx, pytest.raises(StoreError) as caught:
                TX_CALLS[call](tx)
            assert message in str(caught.value), call


def test_database_constraints_reject_the_same_corruption_without_the_override(
    db_path: Path,
) -> None:
    with _populated(db_path):
        for statement, _, _ in CORRUPTIONS.values():
            if "prop_day = '2026-13-45'" in statement:
                continue  # passes the digit pattern; only the read-back check catches it
            with pytest.raises(sqlite3.IntegrityError, match="CHECK constraint failed"):
                raw_execute(db_path, statement)


@pytest.mark.parametrize(
    "statement",
    [
        "UPDATE signals SET status = 'queued'",  # queued with an outcome
        "UPDATE signals SET outcome = NULL",  # done without an outcome
        "UPDATE signals SET processed_at_ms = NULL",  # done without a time
        "UPDATE budget_entries SET counted = 1",  # counted with a release reason
        "UPDATE budget_entries SET release_reason = NULL",  # released without a reason
        "UPDATE budget_entries SET risk_amount = -1.0",
        "UPDATE outbox SET status = 'sent'",  # sent without sent_at_ms
        "UPDATE outbox SET sent_at_ms = 5",  # pending with sent_at_ms
        "UPDATE outbox SET closed_at_ms = 5",  # pending but closed
        "UPDATE outbox SET attempts = -1",
    ],
)
def test_database_rejects_inconsistent_rows(db_path: Path, statement: str) -> None:
    with (
        _populated(db_path),
        pytest.raises(sqlite3.IntegrityError, match="CHECK constraint failed"),
    ):
        raw_execute(db_path, statement)


@pytest.mark.parametrize(
    "statement",
    [
        "UPDATE signals SET bar_time_ms = 'soon'",
        "UPDATE signals SET est_entry = 'high'",
        "UPDATE budget_entries SET risk_amount = 'lots'",
        "UPDATE outbox SET attempts = 1.5",
        "UPDATE state SET updated_at_ms = 'now'",
    ],
)
def test_tables_are_strict_about_types(db_path: Path, statement: str) -> None:
    with _populated(db_path) as populated:
        with populated.transaction() as tx:
            tx.set_state("k", "v", NOW_MS)
        with pytest.raises(sqlite3.IntegrityError, match="cannot store"):
            raw_execute(db_path, statement, ignore_checks=True)


def test_rows_cannot_lose_the_signal_they_reference(db_path: Path) -> None:
    with _populated(db_path) as populated:
        conn = populated._conn
        assert conn is not None
        with pytest.raises(sqlite3.IntegrityError, match="FOREIGN KEY constraint failed"):
            conn.execute("DELETE FROM signals")


# --------------------------------------------------------------------------------------------
# Error text hygiene
# --------------------------------------------------------------------------------------------


def test_store_errors_never_quote_row_content(store: Store) -> None:
    secret = "TOKEN-9f8a7b6c5d4e"
    signal_id = insert(store, payload_json=f'{{"note":"{secret}"}}', reason=secret)
    message_id = enqueue(store, text=secret, signal_id=signal_id)
    errors: list[str] = []

    with store.transaction() as tx:
        tx.mark_message_failed(message_id, error=secret, now_ms=NOW_MS)
        tx.complete_signal(signal_id, Outcome.FORWARDED, NOW_MS)
        tx.add_budget_entry(signal_id=signal_id, prop_day=DAY, risk_amount=1.0, now_ms=NOW_MS)
        attempts: list[Callable[[], object]] = [
            lambda: tx.mark_message_sent(message_id, NOW_MS),
            lambda: tx.mark_message_retry(
                message_id, next_attempt_ms=NOW_MS, error=secret, uncertain=False
            ),
            lambda: tx.complete_signal(signal_id, Outcome.STALE, NOW_MS),
            lambda: tx.add_budget_entry(
                signal_id=signal_id, prop_day=DAY, risk_amount=1.0, now_ms=NOW_MS
            ),
            lambda: tx.insert_signal(dataclasses.replace(ENTRY, symbol=None, reason=secret)),  # type: ignore[arg-type]
            lambda: tx.enqueue_message(
                kind=OutboxKind.SIGNAL, text=secret, now_ms=NOW_MS, signal_id=77
            ),
        ]
        for attempt in attempts:
            with pytest.raises(StoreError) as caught:
                attempt()
            errors.append(str(caught.value))

    assert len(errors) == 6
    assert not any(secret in error for error in errors)


# --------------------------------------------------------------------------------------------
# Threads
# --------------------------------------------------------------------------------------------

THREADS = 8
PER_THREAD = 25


@pytest.mark.parametrize("location", ["file", "memory"])
def test_concurrent_writers_insert_everything_and_one_wins_the_duplicate(
    tmp_path: Path, location: str
) -> None:
    shared = dataclasses.replace(ENTRY, strategy_id="asian_sweep")
    start = threading.Barrier(THREADS)
    winners: list[int] = []
    losers: list[int] = []
    inserted: list[int] = []
    errors: list[BaseException] = []

    def writer(store: Store, worker: int) -> None:
        try:
            start.wait(timeout=10)
            for i in range(PER_THREAD):
                if i == PER_THREAD // 2:
                    with store.transaction() as tx:
                        shared_id = tx.insert_signal(shared)
                    (losers if shared_id is None else winners).append(worker)
                own = dataclasses.replace(
                    ENTRY, bar_time_ms=bar(worker * PER_THREAD + i), received_at_ms=NOW_MS + i
                )
                with store.transaction() as tx:
                    own_id = tx.insert_signal(own)
                    assert own_id is not None
                    # Read-your-own-write inside the transaction, as the processor does.
                    assert tx.get_signal(own_id) == _as_row(own, own_id)
                inserted.append(own_id)
        except BaseException as exc:
            errors.append(exc)

    with Store(":memory:" if location == "memory" else tmp_path / "propbot.db") as store:
        threads = [threading.Thread(target=writer, args=(store, n)) for n in range(THREADS)]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join(timeout=60)

        # No "database is locked", no ProgrammingError about threads, no failed assertion.
        assert errors == []
        assert not any(thread.is_alive() for thread in threads)
        assert len(winners) == 1
        assert len(losers) == THREADS - 1
        total = THREADS * PER_THREAD + 1
        assert len(set(inserted)) == THREADS * PER_THREAD
        with store.transaction() as tx:
            assert tx.queue_depth() == total
            rows = tx.signals_received_between(0, NOW_MS + PER_THREAD)
            assert [row.id for row in rows] == list(range(1, total + 1))
            keys = {(row.strategy_id, row.symbol, row.bar_time_ms, row.action) for row in rows}
            assert len(keys) == total
            assert sum(row.strategy_id == "asian_sweep" for row in rows) == 1


def test_transactions_of_different_threads_never_overlap(store: Store) -> None:
    inside = threading.Event()
    release = threading.Event()
    second_entered = threading.Event()
    order: list[str] = []
    errors: list[BaseException] = []

    def holder() -> None:
        try:
            with store.transaction() as tx:
                tx.set_state("owner", "holder", NOW_MS)
                inside.set()
                assert release.wait(timeout=10)
                order.append("holder-commit")
        except BaseException as exc:
            errors.append(exc)

    def waiter() -> None:
        try:
            with store.transaction() as tx:
                second_entered.set()
                order.append("waiter-enter")
                # The holder's write is committed and visible by the time we get in.
                assert tx.get_state("owner") == "holder"
        except BaseException as exc:
            errors.append(exc)

    first = threading.Thread(target=holder)
    second = threading.Thread(target=waiter)
    first.start()
    assert inside.wait(timeout=10)
    second.start()
    # The waiter must block for as long as the holder's transaction is open.
    assert not second_entered.wait(timeout=0.2)
    release.set()
    first.join(timeout=10)
    second.join(timeout=10)

    assert errors == []
    assert order == ["holder-commit", "waiter-enter"]


def test_rollback_in_one_thread_does_not_leak_into_another(store: Store) -> None:
    errors: list[BaseException] = []

    def failing() -> None:
        try:
            with store.transaction() as tx:
                tx.insert_signal(ENTRY)
                raise Boom
        except Boom:
            pass
        except BaseException as exc:
            errors.append(exc)

    thread = threading.Thread(target=failing)
    thread.start()
    thread.join(timeout=10)

    assert errors == []
    with store.transaction() as tx:
        assert tx.queue_depth() == 0
        assert tx.insert_signal(ENTRY) == 1
