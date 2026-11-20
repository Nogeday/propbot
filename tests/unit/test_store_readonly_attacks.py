"""Attacks on the store's read-only path (`Store.read`, `Store(create=False)`, `recent_signals`)
and on the panel refresh that uses it: real threads and second connections, interrupted and
abandoned reads, closed stores, side files, read-only file systems, the bounded read against
a naive query, and the refresh against the slicing reader it replaced."""

import dataclasses
import functools
import gc
import os
import shutil
import sqlite3
import stat
import subprocess
import sys
import tempfile
import threading
import time
from collections.abc import Callable, Iterator
from contextlib import closing
from pathlib import Path
from typing import Any

import pytest
from hypothesis import HealthCheck, given, settings
from hypothesis import strategies as st

from propbot.dashboard import live
from propbot.dashboard.live import (
    MAX_SIGNAL_ROWS,
    SIGNAL_LOOKBACK_MS,
    StoreSource,
    load_view,
    view_json,
)
from propbot.risk.loader import load_risk_config, load_venue_profile
from propbot.store import db
from propbot.store.db import (
    BUSY_TIMEOUT_MS,
    SCHEMA_VERSION,
    NewSignal,
    Outcome,
    ReadTx,
    SignalRow,
    Store,
    StoreError,
)
from support import tg
from support.alerts import BAR_TIME_MS
from support.paths import CONFIG_FIXTURES

MINUTE_MS = 60_000
HOUR_MS = 60 * MINUTE_MS
DAY_MS = 24 * HOUR_MS
NOW_MS = BAR_TIME_MS + 2 * HOUR_MS
RISK = load_risk_config(CONFIG_FIXTURES / "valid" / "risk.yaml")
VENUE = load_venue_profile(CONFIG_FIXTURES / "valid" / "venues" / "sample_2step.yaml")
SET_STATE = "INSERT OR REPLACE INTO state (key, value, updated_at_ms) VALUES (?, ?, 1)"


class Boom(Exception):
    pass


@pytest.fixture
def db_path(tmp_path: Path) -> Path:
    return tmp_path / "propbot.db"


@pytest.fixture
def serve(db_path: Path) -> Iterator[Store]:
    """The writing store, as `serve` opens it."""
    with Store(db_path) as opened:
        yield opened


@pytest.fixture
def panel(serve: Store, db_path: Path) -> Iterator[Store]:
    """A second connection to the same file, as the panel opens it."""
    with Store(db_path, create=False) as opened:
        yield opened


def signal(index: int, *, received_at_ms: int = NOW_MS, **overrides: Any) -> NewSignal:
    """A distinct signal per `index` (the idempotency key differs by bar time)."""
    bar = BAR_TIME_MS + index * MINUTE_MS
    return dataclasses.replace(
        tg.ENTRY_SIGNAL,
        received_at_ms=received_at_ms,
        bar_time_ms=bar,
        bar_close_ms=bar + MINUTE_MS,
        **overrides,
    )


def insert(store: Store, index: int, **overrides: Any) -> int:
    with store.transaction() as tx:
        signal_id = tx.insert_signal(signal(index, **overrides))
    assert signal_id is not None
    return signal_id


def connection(path: Path) -> sqlite3.Connection:
    """Another process's connection: autocommit, and it never waits for a lock."""
    return sqlite3.connect(path, isolation_level=None, timeout=0)


def checkpoint_truncate(path: Path) -> tuple[int, int, int]:
    """TRUNCATE checkpoint from another connection: busy = 1 while any read is still open."""
    with closing(connection(path)) as other:
        row = other.execute("PRAGMA wal_checkpoint(TRUNCATE)").fetchone()
    return int(row[0]), int(row[1]), int(row[2])


def raw(store: Store) -> sqlite3.Connection:
    conn = store._conn
    assert conn is not None
    return conn


def assert_clean(store: Store) -> None:
    """The connection is neither query-only nor inside a transaction, and writes."""
    conn = raw(store)
    assert conn.execute("PRAGMA query_only").fetchone()[0] == 0
    assert not conn.in_transaction
    assert store._active is None
    with store.transaction() as tx:
        tx.set_state("after", "1", NOW_MS)
        assert tx.get_state("after") == "1"


# --- (1) the write path is what it was ----------------------------------------------------------


def test_the_write_path_constants_are_unchanged() -> None:
    assert SCHEMA_VERSION == 3
    assert BUSY_TIMEOUT_MS == 5000
    assert db._SQL_BEGIN == "BEGIN IMMEDIATE"
    assert db._SQL_PRAGMA_JOURNAL_MODE_WAL == "PRAGMA journal_mode = WAL"
    assert db._SQL_PRAGMA_SYNCHRONOUS_FULL == "PRAGMA synchronous = FULL"
    assert db._SQL_PRAGMA_FOREIGN_KEYS_ON == "PRAGMA foreign_keys = ON"
    assert db._SQL_PRAGMA_BUSY_TIMEOUT == "PRAGMA busy_timeout = 5000"


@pytest.mark.parametrize("create", [True, False])
def test_both_opens_configure_the_connection_as_serve_needs_it(
    serve: Store, db_path: Path, create: bool
) -> None:
    with Store(db_path, create=create) as opened:
        with opened.read() as reader:
            assert reader.queue_depth() == 0
        conn = raw(opened)
        assert conn.execute("PRAGMA journal_mode").fetchone()[0] == "wal"
        assert conn.execute("PRAGMA synchronous").fetchone()[0] == 2
        assert conn.execute("PRAGMA foreign_keys").fetchone()[0] == 1
        assert conn.execute("PRAGMA busy_timeout").fetchone()[0] == BUSY_TIMEOUT_MS
        assert conn.execute("PRAGMA query_only").fetchone()[0] == 0
        assert conn.isolation_level is None


def test_a_write_after_a_read_takes_the_write_lock_as_before(panel: Store, db_path: Path) -> None:
    # The most dangerous regression: a read leaving its state behind in the next write.
    with panel.read() as reader:
        assert reader.queue_depth() == 0
    with closing(connection(db_path)) as other, panel.transaction() as tx:
        assert tx.insert_signal(signal(0)) == 1
        # BEGIN IMMEDIATE, not a deferred read upgraded later: the lock is held already.
        with pytest.raises(sqlite3.OperationalError, match="locked"):
            other.execute("BEGIN IMMEDIATE")
    with panel.read() as reader:
        assert reader.queue_depth() == 1


# --- (2) concurrency: one connection, many threads ----------------------------------------------


def test_many_reader_threads_and_one_writer_see_consistent_snapshots(serve: Store) -> None:
    # The writer adds a signal and the count of signals in one transaction; a read that saw
    # one without the other would have mixed two moments.
    writes = 60
    errors: list[BaseException] = []
    seen: list[int] = []
    done = threading.Event()

    def writer() -> None:
        try:
            for index in range(writes):
                with serve.transaction() as tx:
                    tx.insert_signal(signal(index))
                    tx.set_state("count", str(index + 1), NOW_MS)
        except BaseException as exc:
            errors.append(exc)
        finally:
            done.set()

    def reader() -> None:
        try:
            while not done.is_set():
                with serve.read() as tx:
                    rows = tx.recent_signals(10_000)
                    count = tx.get_state("count")
                    assert len(rows) == (0 if count is None else int(count))
                    assert tx.queue_depth() == len(rows)
                    seen.append(len(rows))
        except BaseException as exc:
            errors.append(exc)

    threads = [threading.Thread(target=reader) for _ in range(6)]
    threads.append(threading.Thread(target=writer))
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(timeout=60)
        assert not thread.is_alive()

    assert errors == []
    assert seen
    assert_clean(serve)
    with serve.read() as tx:
        assert tx.queue_depth() == writes


def test_close_from_another_thread_waits_for_the_open_read(serve: Store) -> None:
    inside = threading.Event()
    release = threading.Event()
    closed = threading.Event()
    errors: list[BaseException] = []

    def closer() -> None:
        try:
            serve.close()
            closed.set()
        except BaseException as exc:
            errors.append(exc)

    thread = threading.Thread(target=closer)
    with serve.read() as reader:
        inside.set()
        thread.start()
        assert not closed.wait(timeout=0.05)
        assert reader.queue_depth() == 0  # still usable: close has not run
        release.set()
    thread.join(timeout=10)

    assert errors == []
    assert closed.is_set()
    with pytest.raises(StoreError, match="veritabanı kapalı"):
        serve.read().__enter__()
    with pytest.raises(StoreError, match="veritabanı kapalı"):
        serve.transaction().__enter__()


# --- (2) concurrency: two connections (panel and serve) -----------------------------------------


def test_a_read_open_while_this_thread_writes_through_the_other_connection(
    serve: Store, panel: Store
) -> None:
    insert(serve, 0)
    with panel.read() as reader:
        assert reader.queue_depth() == 1
        # serve's writer neither waits for the panel's open read nor fails because of it.
        raw(serve).execute("PRAGMA busy_timeout = 0")
        insert(serve, 1)
        assert reader.queue_depth() == 1  # the snapshot is kept
    with panel.read() as reader:
        assert reader.queue_depth() == 2
    assert_clean(panel)


def test_a_write_open_while_this_thread_reads_through_the_other_connection(
    serve: Store, panel: Store
) -> None:
    raw(panel).execute("PRAGMA busy_timeout = 0")
    with serve.transaction() as tx:
        tx.insert_signal(signal(0))
        with panel.read() as reader:  # would raise "locked" at once if it had to wait
            assert reader.queue_depth() == 0
    with panel.read() as reader:
        assert reader.queue_depth() == 1


def test_the_panel_writes_fresh_after_a_read_that_saw_an_older_moment(
    serve: Store, panel: Store
) -> None:
    # A stale snapshot handed to a writer would make this write fail (SQLITE_BUSY_SNAPSHOT)
    # or decide on old data; the write transaction starts its own, current snapshot.
    with panel.read() as reader:
        assert reader.get_state("k") is None
        raw(serve).execute(SET_STATE, ("k", "serve"))
    with panel.transaction() as tx:
        assert tx.get_state("k") == "serve"
        tx.set_state("k", "panel", NOW_MS)
    with serve.read() as reader:
        assert reader.get_state("k") == "panel"


def test_panel_readers_and_a_serve_writer_on_threads_never_deadlock(
    serve: Store, panel: Store
) -> None:
    writes = 40
    errors: list[BaseException] = []
    done = threading.Event()

    def writer() -> None:
        try:
            for index in range(writes):
                with serve.transaction() as tx:
                    tx.insert_signal(signal(index))
                    tx.set_state("count", str(index + 1), NOW_MS)
        except BaseException as exc:
            errors.append(exc)
        finally:
            done.set()

    def reader() -> None:
        try:
            while not done.is_set():
                with panel.read() as tx:
                    count = tx.get_state("count")
                    assert len(tx.recent_signals(10_000)) == (0 if count is None else int(count))
        except BaseException as exc:
            errors.append(exc)

    def panel_action() -> None:
        try:
            while not done.is_set():
                with panel.transaction() as tx:
                    tx.set_state("panel", "1", NOW_MS)
        except BaseException as exc:
            errors.append(exc)

    threads = [threading.Thread(target=reader) for _ in range(4)]
    threads += [threading.Thread(target=panel_action), threading.Thread(target=writer)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(timeout=60)
        assert not thread.is_alive()

    assert errors == []
    assert_clean(panel)
    with panel.read() as tx:
        assert tx.queue_depth() == writes


# --- (2) a read always ends: the WAL is never pinned ---------------------------------------------


def test_a_read_that_ends_normally_releases_the_wal(
    serve: Store, panel: Store, db_path: Path
) -> None:
    insert(serve, 0)
    with panel.read() as reader:
        assert reader.queue_depth() == 1
        assert checkpoint_truncate(db_path)[0] == 1  # held while open...

    assert checkpoint_truncate(db_path) == (0, 0, 0)  # ...and released after
    assert_clean(panel)


@pytest.mark.parametrize("error", [Boom, KeyboardInterrupt, StoreError, sqlite3.OperationalError])
def test_a_read_ended_by_any_exception_releases_the_wal(
    serve: Store, panel: Store, db_path: Path, error: type[BaseException]
) -> None:
    insert(serve, 0)

    def read_and_raise() -> None:
        with panel.read() as reader:
            assert reader.queue_depth() == 1
            raise error("inside the read")

    with pytest.raises(error):
        read_and_raise()

    assert checkpoint_truncate(db_path) == (0, 0, 0)
    assert_clean(panel)


def test_a_failing_read_method_ends_the_read_too(serve: Store, panel: Store, db_path: Path) -> None:
    with pytest.raises(StoreError, match="limit"), panel.read() as reader:
        reader.recent_signals(-1)

    assert checkpoint_truncate(db_path) == (0, 0, 0)
    assert_clean(panel)


def test_an_abandoned_read_ends_when_its_context_manager_is_dropped(
    serve: Store, panel: Store, db_path: Path
) -> None:
    # `__enter__` without `__exit__`: the generator is closed when the manager is collected.
    reader = panel.read().__enter__()
    gc.collect()

    with pytest.raises(StoreError, match="transaction kapandı"):
        reader.queue_depth()
    assert checkpoint_truncate(db_path) == (0, 0, 0)
    assert_clean(panel)


class _Interrupting:
    """A connection whose `execute` raises at one statement (as a signal handler could)."""

    def __init__(self, conn: sqlite3.Connection, at: str, error: BaseException) -> None:
        self._conn = conn
        self._at = at
        self._error = error

    def execute(self, sql: str, *args: Any) -> sqlite3.Cursor:
        if sql == self._at:
            raise self._error
        return self._conn.execute(sql, *args)

    def __getattr__(self, name: str) -> Any:
        return getattr(self._conn, name)


@pytest.mark.parametrize("at", [db._SQL_QUERY_ONLY_ON, db._SQL_BEGIN_READ, db._SQL_START_READ])
@pytest.mark.parametrize(
    "error", [KeyboardInterrupt(), MemoryError(), sqlite3.OperationalError("disk I/O error")]
)
def test_an_interrupt_while_a_read_opens_leaves_the_connection_writable(
    serve: Store, panel: Store, db_path: Path, at: str, error: BaseException
) -> None:
    # Before the fix only sqlite3.Error was cleaned up: an interrupt after `query_only = ON`
    # left the shared connection read-only (every later write: "readonly database") and,
    # after BEGIN, inside the read with the WAL pinned.
    conn = raw(panel)
    panel._conn = _Interrupting(conn, at, error)  # type: ignore[assignment]
    try:
        with pytest.raises((type(error), StoreError)), panel.read():
            pytest.fail("the read must not open")
    finally:
        panel._conn = conn

    assert checkpoint_truncate(db_path)[0] == 0
    assert_clean(panel)


# --- (2) closed stores, collection, odd files --------------------------------------------------


def _read_and_forget(path: Path) -> None:
    """Open, read, and drop the store without closing it (collected when this returns)."""
    opened = Store(path, create=False)
    with opened.read() as reader:
        assert reader.queue_depth() == 0


def test_a_store_collected_after_reads_warns_and_releases_everything(db_path: Path) -> None:
    Store(db_path).close()

    with pytest.warns(ResourceWarning, match="unclosed propbot Store"):
        _read_and_forget(db_path)
    gc.collect()
    assert checkpoint_truncate(db_path) == (0, 0, 0)


def test_a_read_of_a_brand_new_database(db_path: Path) -> None:
    with Store(db_path) as fresh, fresh.read() as reader:
        assert reader.queue_depth() == 0
        assert reader.recent_signals(10) == []
        assert set(reader.outbox_counts().values()) == {0}
        assert reader.get_state("k") is None
    with Store(db_path, create=False) as opened, opened.read() as reader:
        assert reader.recent_signals(10) == []


def test_a_database_whose_side_files_are_gone_opens_and_reads(db_path: Path) -> None:
    with Store(db_path) as serve:
        insert(serve, 0)
        insert(serve, 1)
    for suffix in ("-wal", "-shm"):
        Path(f"{db_path}{suffix}").unlink(missing_ok=True)
    before = db_path.read_bytes()

    with Store(db_path, create=False) as opened:
        with opened.read() as reader:
            assert [row.id for row in reader.recent_signals(10)] == [2, 1]
        assert_clean(opened)
    with Store(db_path, create=False) as opened, opened.read() as reader:
        assert reader.get_state("after") == "1"
    assert len(before) > 0


def test_a_path_with_uri_special_characters_opens_without_create(tmp_path: Path) -> None:
    # create=False opens through a file: URI; '?', '#', '%' and non-ASCII must stay a name.
    folder = tmp_path / "veri ğüş #1 %41 ?x"
    folder.mkdir()
    path = folder / "prop?bot#.db"
    with Store(path) as serve:
        insert(serve, 0)

    with Store(path, create=False) as opened, opened.read() as reader:
        assert reader.queue_depth() == 1
    assert sorted(p.name for p in folder.iterdir()) == ["prop?bot#.db"]
    assert sorted(p.name for p in tmp_path.iterdir()) == [folder.name]


# --- (2) read-only file systems -----------------------------------------------------------------

# Run in a child process as an unprivileged user when the tests run as root: root ignores
# file modes, so chmod alone proves nothing there.
_PROBE = """
import sys
from pathlib import Path
from propbot.store.db import Store, StoreError

try:
    with Store(Path(sys.argv[1]), create=False) as store, store.read() as reader:
        print("OK", reader.queue_depth())
except StoreError as error:
    print("StoreError", error)
"""
_NOBODY = 65534


def _probe_env() -> dict[str, str]:
    return {"PYTHONPATH": str(Path(db.__file__).parents[2])}


@functools.cache
def _unprivileged_user_can_run_the_probe() -> bool:
    """Whether uid 65534 can start this interpreter and import the package from here.

    Not so when root runs the suite from a checkout only root may enter (/root/..., a
    private temp directory): the child cannot even traverse to the interpreter.
    """
    try:
        result = subprocess.run(
            [sys.executable, "-I", "-c", "import propbot.store.db"],
            capture_output=True,
            timeout=60,
            check=False,
            env=_probe_env(),
            user=_NOBODY,
            group=_NOBODY,
            extra_groups=[],
        )
    except PermissionError:
        return False
    return result.returncode == 0


def _probe(path: Path) -> str:
    kwargs: dict[str, Any] = {}
    if os.geteuid() == 0:
        if not _unprivileged_user_can_run_the_probe():
            # Said out loud instead of failing with the child's PermissionError: as root,
            # file modes are only tested through an unprivileged child, and this checkout
            # is out of its reach. Run the suite as a normal user or from a readable path.
            pytest.skip(
                "running as root from a checkout an unprivileged user cannot read: "
                "file-mode checks need an unprivileged child process"
            )
        kwargs = {"user": _NOBODY, "group": _NOBODY, "extra_groups": []}
    result = subprocess.run(
        [sys.executable, "-I", "-c", _PROBE, str(path)],
        capture_output=True,
        text=True,
        timeout=60,
        check=False,
        env=_probe_env(),
        **kwargs,
    )
    assert result.stderr == ""
    return str(result.stdout).strip()


@pytest.fixture
def shared_dir(tmp_path: Path) -> Iterator[Path]:
    """A directory the probe's user can reach (pytest's own temp root is private to root)."""
    if os.geteuid() != 0:
        yield tmp_path
        return
    base = Path(tempfile.mkdtemp(prefix="propbot-ro-"))
    try:
        base.chmod(0o755)
        yield base
    finally:
        for path in (base, *base.rglob("*")):
            path.chmod(stat.S_IRWXU)
        shutil.rmtree(base)


def _owned_database(folder: Path) -> Path:
    path = folder / "propbot.db"
    with Store(path) as serve:
        insert(serve, 0)
    if os.geteuid() == 0:
        os.chown(folder, _NOBODY, _NOBODY)
        os.chown(path, _NOBODY, _NOBODY)
    return path


def test_a_read_only_directory_fails_soft_and_creates_nothing(shared_dir: Path) -> None:
    folder = shared_dir / "data"
    folder.mkdir()
    path = _owned_database(folder)
    before = path.read_bytes()
    folder.chmod(0o555)

    answer = _probe(path)

    assert answer.startswith("StoreError veritabanı hatası")
    assert sorted(p.name for p in folder.iterdir()) == ["propbot.db"]
    assert path.read_bytes() == before


def test_a_read_only_file_is_read_and_left_unchanged(shared_dir: Path) -> None:
    folder = shared_dir / "data"
    folder.mkdir()
    path = _owned_database(folder)
    before = path.read_bytes()
    path.chmod(0o444)

    answer = _probe(path)

    # SQLite opens a write-protected file read-only: the panel can still show it. The side
    # files are what any WAL reader needs; the database itself is not touched.
    assert answer == "OK 1"
    assert path.read_bytes() == before


def test_a_missing_file_in_a_read_only_directory_is_refused_and_not_created(
    shared_dir: Path,
) -> None:
    folder = shared_dir / "data"
    folder.mkdir()
    if os.geteuid() == 0:
        os.chown(folder, _NOBODY, _NOBODY)
    folder.chmod(0o555)

    answer = _probe(folder / "propbot.db")

    assert answer.startswith("StoreError veritabanı dosyası yok")
    assert list(folder.iterdir()) == []


# --- (3) the bounded read ---------------------------------------------------------------------


def _naive(
    rows: list[SignalRow], limit: int, since_ms: int | None, before_ms: int | None
) -> list[int]:
    kept = [
        row
        for row in rows
        if (since_ms is None or row.received_at_ms >= since_ms)
        and (before_ms is None or row.received_at_ms < before_ms)
    ]
    kept.sort(key=lambda row: (row.received_at_ms, row.id), reverse=True)
    return [row.id for row in kept[:limit]]


_BOUND = st.one_of(st.none(), st.integers(min_value=-5, max_value=25))


@settings(max_examples=150, deadline=None, suppress_health_check=[HealthCheck.too_slow])
@given(
    received=st.lists(st.integers(min_value=0, max_value=20), max_size=40),
    limit=st.integers(min_value=0, max_value=50),
    since_ms=_BOUND,
    before_ms=_BOUND,
)
def test_recent_signals_equals_a_naive_query(
    received: list[int], limit: int, since_ms: int | None, before_ms: int | None
) -> None:
    with Store(":memory:") as store:
        with store.transaction() as tx:
            for index, moment in enumerate(received):
                assert tx.insert_signal(signal(index, received_at_ms=moment)) is not None
        with store.read() as reader:
            everything = reader.signals_received_between(-(2**63), 2**63 - 1)
            got = reader.recent_signals(limit, since_ms=since_ms, before_ms=before_ms)

    assert [row.id for row in got] == _naive(everything, limit, since_ms, before_ms)
    # Every row of the window newer than the oldest returned is returned too.
    if got:
        oldest = (got[-1].received_at_ms, got[-1].id)
        newer = _naive(everything, len(everything), since_ms, before_ms)
        by_id = {row.id: row for row in everything}
        assert {i for i in newer if (by_id[i].received_at_ms, i) >= oldest} == {
            row.id for row in got
        }


@pytest.mark.parametrize("limit", [2**63 - 1, 10**9])
def test_a_huge_limit_returns_everything(limit: int) -> None:
    with Store(":memory:") as store:
        for index in range(3):
            insert(store, index, received_at_ms=index)
        with store.read() as reader:
            assert [row.id for row in reader.recent_signals(limit)] == [3, 2, 1]


@pytest.mark.parametrize("limit", [2**63, 10**30])
def test_a_limit_beyond_sqlite_integers_is_a_store_error(limit: int) -> None:
    with Store(":memory:") as store, store.read() as reader, pytest.raises(StoreError):
        reader.recent_signals(limit)


@pytest.mark.parametrize(
    ("since_ms", "before_ms", "expected"),
    [
        (-(2**63), 2**63 - 1, [2, 1]),  # before is exclusive even at the top
        (-(2**63), None, [3, 2, 1]),  # the whole INTEGER range
        (2**63 - 1, None, [3]),
        (None, -(2**63), []),
        (5, 5, []),  # empty window
        (6, 5, []),  # inverted window
        (5, 6, [2]),  # exactly one millisecond
    ],
)
def test_recent_signals_bounds_at_the_edges(
    since_ms: int | None, before_ms: int | None, expected: list[int]
) -> None:
    with Store(":memory:") as store:
        for index, moment in enumerate((-(2**63), 5, 2**63 - 1)):
            insert(store, index, received_at_ms=moment)
        with store.read() as reader:
            assert [
                row.id for row in reader.recent_signals(10, since_ms=since_ms, before_ms=before_ms)
            ] == expected


@pytest.mark.parametrize("bound", ["since_ms", "before_ms"])
def test_a_bound_beyond_sqlite_integers_is_a_store_error(bound: str) -> None:
    with Store(":memory:") as store, store.read() as reader, pytest.raises(StoreError):
        reader.recent_signals(10, **{bound: 2**64})


def test_the_bounded_read_reads_limit_rows_however_large_the_table(db_path: Path) -> None:
    # The plan says "index search"; the virtual-machine step counter says how much was read.
    with Store(db_path) as store:
        with store.transaction() as tx:
            for index in range(20_000):
                tx.insert_signal(signal(index, received_at_ms=index))
        steps: list[int] = []

        def counted(limit: int, since_ms: int) -> int:
            conn = raw(store)
            counter = [0]

            def tick() -> int:
                counter[0] += 1
                return 0

            conn.set_progress_handler(tick, 100)
            try:
                with store.read() as reader:
                    assert len(reader.recent_signals(limit, since_ms=since_ms)) == min(
                        limit, 20_000 - since_ms
                    )
            finally:
                conn.set_progress_handler(None, 0)
            return counter[0]

        steps = [counted(10, 0), counted(10, 19_990), counted(1000, 0)]

    small_window, whole_table, thousand = steps
    assert whole_table <= small_window * 2 + 5
    assert thousand < 50 * (small_window + 1) + 200


# --- (4) the panel refresh -------------------------------------------------------------------


def _old_recent_signals(tx: ReadTx, now_ms: int) -> tuple[tuple[SignalRow, ...], bool]:
    """The slicing reader this change replaced, as it was (one-hour slices, newest first)."""
    slice_ms = HOUR_MS
    lower = now_ms - SIGNAL_LOOKBACK_MS
    end = now_ms + DAY_MS
    slices: list[list[SignalRow]] = []
    total = 0
    while end > lower and total < MAX_SIGNAL_ROWS:
        start = max(lower, end - slice_ms)
        rows = tx.signals_received_between(start, end)
        slices.append(rows)
        total += len(rows)
        end = start
    ordered = tuple(row for rows in reversed(slices) for row in rows)
    return ordered, end > lower


_ACTIONS = ("entry", "exit", "heartbeat", "stop_update", "cancel")
_OUTCOMES = (Outcome.FORWARDED, Outcome.NO_ACTIVE_TRADE, Outcome.SKIP_BUDGET, None)


def _fill(store: Store, specs: list[tuple[int, int, int, int]]) -> None:
    """(received offset in minutes from NOW, action, outcome, bar offset) per signal, in order."""
    with store.transaction() as tx:
        for index, (offset, action, outcome, bar) in enumerate(specs):
            moment = NOW_MS + offset * MINUTE_MS
            new = dataclasses.replace(
                tg.ENTRY_SIGNAL,
                received_at_ms=moment,
                bar_time_ms=BAR_TIME_MS + (bar * 1000 + index) * MINUTE_MS,
                bar_close_ms=BAR_TIME_MS + (bar * 1000 + index + 1) * MINUTE_MS,
                action=_ACTIONS[action],
                strategy_id=("ny_orb_fvg", "ict_silver_bullet")[index % 2],
            )
            signal_id = tx.insert_signal(new)
            assert signal_id is not None
            done = _OUTCOMES[outcome]
            if done is not None:
                tx.complete_signal(signal_id, done, moment)


def _views(store: Store, monkeypatch: pytest.MonkeyPatch) -> tuple[object, object]:
    source = StoreSource(None, store=store)
    new = view_json(load_view(source, venue=VENUE, venue_name="x", risk=RISK, now_ms=NOW_MS))
    with monkeypatch.context() as patched:
        patched.setattr(live, "_recent_signals", _old_recent_signals)
        old = view_json(load_view(source, venue=VENUE, venue_name="x", risk=RISK, now_ms=NOW_MS))
    return new, old


_SPEC = st.tuples(
    # From before the lookback to beyond the day ahead, in minutes.
    st.integers(min_value=-(8 * 24 * 60), max_value=2 * 24 * 60),
    st.integers(min_value=0, max_value=len(_ACTIONS) - 1),
    st.integers(min_value=0, max_value=len(_OUTCOMES) - 1),
    st.integers(min_value=0, max_value=3),
)


@settings(max_examples=60, deadline=None, suppress_health_check=[HealthCheck.too_slow])
@given(st.lists(_SPEC, max_size=60))
def test_the_refresh_shows_what_the_slicing_reader_showed_for_small_data(
    specs: list[tuple[int, int, int, int]],
) -> None:
    # Received in id order (one clock, as `serve` writes): the same rows in the same order.
    ordered = sorted(specs, key=lambda spec: spec[0])
    with Store(":memory:") as store:
        _fill(store, ordered)
        with pytest.MonkeyPatch.context() as monkeypatch:
            new, old = _views(store, monkeypatch)
        with store.read() as reader:
            assert live._recent_signals(reader, NOW_MS) == _old_recent_signals(reader, NOW_MS)

    assert new == old


@settings(max_examples=60, deadline=None, suppress_health_check=[HealthCheck.too_slow])
@given(st.lists(_SPEC, max_size=60))
def test_out_of_order_receive_times_give_the_same_rows(
    specs: list[tuple[int, int, int, int]],
) -> None:
    # A clock step backwards: the order inside one hour was by id and is now by receive
    # time (the documented deviation); the rows and the cut flag are the same.
    with Store(":memory:") as store:
        _fill(store, specs)
        with store.read() as reader:
            rows, cut = live._recent_signals(reader, NOW_MS)
            old_rows, old_cut = _old_recent_signals(reader, NOW_MS)

    assert cut == old_cut
    assert sorted(row.id for row in rows) == sorted(row.id for row in old_rows)
    assert [(row.received_at_ms, row.id) for row in rows] == sorted(
        (row.received_at_ms, row.id) for row in rows
    )


def _flood(path: Path, count: int) -> None:
    """`count` signals inside the lookback window, newest at NOW, mixed actions and outcomes."""
    step_ms = (SIGNAL_LOOKBACK_MS - MINUTE_MS) // count
    with Store(path) as store, store.transaction() as tx:
        for index in range(count):
            moment = NOW_MS - (count - 1 - index) * step_ms
            new = dataclasses.replace(
                tg.ENTRY_SIGNAL,
                received_at_ms=moment,
                bar_time_ms=moment - MINUTE_MS,
                bar_close_ms=moment,
                action=_ACTIONS[index % 4],
            )
            signal_id = tx.insert_signal(new)
            assert signal_id is not None
            tx.complete_signal(signal_id, Outcome.FORWARDED, moment)


def _timed(refresh: Callable[[], object], rounds: int) -> float:
    best = float("inf")
    for _ in range(rounds):
        started = time.perf_counter()
        refresh()
        best = min(best, time.perf_counter() - started)
    return best


def test_a_refresh_of_fifty_thousand_signals_stays_fast(db_path: Path) -> None:
    _flood(db_path, 50_000)
    source = StoreSource(db_path)

    def refresh() -> object:
        view = load_view(source, venue=VENUE, venue_name="x", risk=RISK, now_ms=NOW_MS)
        assert view.db.ok
        assert "yalnızca en yeni 1000 sinyal" in view.trades_note
        return view

    try:
        seconds = _timed(refresh, 5)
    finally:
        source.close()

    # Generous for a slow CI machine; the measured time is a few tens of milliseconds.
    assert seconds < 1.0, f"{seconds * 1000:.1f} ms"
