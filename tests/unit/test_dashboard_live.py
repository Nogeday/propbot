"""Live view model: status, liveness, account arithmetic, prop-day staleness, signals, trades,
and every database problem as a state rather than a crash."""

import datetime as dt
import sqlite3
from collections.abc import Iterator
from pathlib import Path

import pytest

from propbot.core.clock import ms_from_datetime
from propbot.dashboard import live
from propbot.dashboard.live import (
    LIVENESS_LIMIT_MS,
    RECENT_SIGNAL_COUNT,
    BotState,
    DbState,
    LiveView,
    ManualSnapshot,
    StoreSource,
    bot_status,
    flag_set,
    load_view,
    parse_snapshot,
    service_status,
    snapshot_text,
    view_json,
)
from propbot.notify.commands import CommandDeps, Commands
from propbot.risk.loader import load_risk_config, load_venue_profile
from propbot.risk.venue import MaxLossType, VenueProfile
from propbot.signals.tvconfig import load_tv_alerts_config
from propbot.store.db import Outcome, ReleaseReason, StateKey, Store
from support import tg
from support.alerts import BAR_TIME_MS
from support.paths import CONFIG_FIXTURES, FIXTURES

MINUTE_MS = 60_000
HOUR_MS = 60 * MINUTE_MS
DAY_MS = 24 * HOUR_MS
NOW_MS = BAR_TIME_MS + 2 * HOUR_MS  # 2026-07-21 17:30 UTC = 19:30 Prague = 20:30 TR
TODAY = dt.date(2026, 7, 21)
RISK = load_risk_config(CONFIG_FIXTURES / "valid" / "risk.yaml")
VENUE = load_venue_profile(CONFIG_FIXTURES / "valid" / "venues" / "sample_2step.yaml")
TV = load_tv_alerts_config(FIXTURES / "tv_alerts" / "valid" / "full.yaml")
PRAGUE = VENUE.daily_reset.zone


def view_of(store: Store, *, now_ms: int = NOW_MS, venue: VenueProfile = VENUE) -> LiveView:
    return load_view(
        StoreSource(None, store=store),
        venue=venue,
        venue_name="sample_2step",
        risk=RISK,
        now_ms=now_ms,
    )


@pytest.fixture
def store() -> Iterator[Store]:
    with Store(":memory:") as opened:
        yield opened


def set_state(store: Store, key: StateKey, value: str) -> None:
    with store.transaction() as tx:
        tx.set_state(key, value, NOW_MS)


def prague_ms(year: int, month: int, day: int, hour: int, minute: int) -> int:
    return ms_from_datetime(dt.datetime(year, month, day, hour, minute, tzinfo=PRAGUE))


# --- bot status and liveness -----------------------------------------------------------------


@pytest.mark.parametrize("killed", [False, True])
@pytest.mark.parametrize("paused", [False, True])
@pytest.mark.parametrize("standby", [False, True])
def test_bot_status_order_and_every_set_flag_is_named(
    killed: bool, paused: bool, standby: bool
) -> None:
    status = bot_status(killed=killed, paused=paused, standby=standby)

    expected = (
        BotState.KILLED
        if killed
        else BotState.STANDBY
        if standby
        else BotState.PAUSED
        if paused
        else BotState.ACTIVE
    )
    assert status.state is expected
    shown = {status.label, *status.also}
    assert ("KILL" in shown) == killed
    assert ("DURAKLATILDI" in shown) == paused
    assert ("BEKLEMEDE" in shown) == standby


@pytest.mark.parametrize("value", [None, "0", "1", "2", "", "garbage"])
def test_flag_rule_is_the_one_durum_applies(store: Store, value: str | None) -> None:
    if value is not None:
        set_state(store, StateKey.KILLED, value)
    commands = Commands(
        CommandDeps(
            store=store,
            risk=RISK,
            venue=VENUE,
            venue_name="sample_2step",
            tv=TV,
            pin="246813",
            clock=lambda: NOW_MS,
            started_ms=NOW_MS,
            reject_counts=dict,
        )
    )

    durum = commands.handle("/durum", sent_at_ms=NOW_MS).text
    view = view_of(store)

    assert flag_set(value) == ("Durum: KILL" in durum)
    assert view.bot is not None
    assert (view.bot.state is BotState.KILLED) == flag_set(value)


@pytest.mark.parametrize(
    ("stored", "alive", "label"),
    [
        (None, False, "çalışmıyor"),
        (str(NOW_MS - 30_000), True, "çalışıyor"),
        (str(NOW_MS - LIVENESS_LIMIT_MS), True, "çalışıyor"),
        (str(NOW_MS - LIVENESS_LIMIT_MS - 1), False, "çalışmıyor"),
        (str(NOW_MS + LIVENESS_LIMIT_MS + 1), False, "bilinmiyor"),
        ("not-a-number", False, "bilinmiyor"),
    ],
)
def test_service_liveness_from_last_alive(stored: str | None, alive: bool, label: str) -> None:
    status = service_status(stored, NOW_MS)

    assert (status.alive, status.label) == (alive, label)


def test_liveness_limit_is_three_missed_monitor_ticks() -> None:
    assert LIVENESS_LIMIT_MS == 3 * 30_000


# --- account arithmetic -----------------------------------------------------------------------


def test_account_numbers_from_a_manual_snapshot_and_the_ledger(store: Store) -> None:
    snapshot = ManualSnapshot(
        balance=104000.0,
        equity=103000.0,
        day_start_balance=104500.0,
        entered_at_ms=NOW_MS - HOUR_MS,
    )
    set_state(store, StateKey.MANUAL_ACCOUNT, snapshot_text(snapshot))
    first, _ = tg.forwarded_entry(store, prop_day=TODAY, now_ms=NOW_MS, risk_amount=250.0)
    tg.forwarded_entry(
        store, prop_day=TODAY, now_ms=NOW_MS, risk_amount=250.0, bar_time_ms=BAR_TIME_MS + MINUTE_MS
    )
    with store.transaction() as tx:
        tx.set_budget_counted(
            signal_id=first, counted=False, reason=ReleaseReason.SKIPPED, now_ms=NOW_MS
        )

    account = view_of(store).account

    assert account.manual
    assert not account.stale
    assert account.source == "elle girildi · 2026-07-21 19:30 TR"
    assert (account.balance_text, account.equity_text) == ("$104000", "$103000")
    phase1, phase2 = account.targets
    assert phase1.title == "Faz 1 kâr hedefi (%10 = $10000)"
    assert (phase1.fraction, phase1.value_text) == (0.4, "$4000 / $10000 (%40.0)")
    assert (phase2.fraction, phase2.value_text) == (0.8, "$4000 / $5000 (%80.0)")
    assert account.total_loss.fraction == 1.0
    assert account.total_loss.value_text == "kalan $13000 / $10000 (%130.0) · taban $90000"
    budget, firm = account.ledger
    assert (budget.fraction, budget.value_text) == (0.25, "$250 / $1000 (%25.0) · 2 giriş")
    assert (firm.fraction, firm.value_text) == (0.05, "$250 / $5000 (%5.0)")
    assert account.declared_daily is not None
    assert account.declared_daily.fraction == 0.3
    assert account.declared_daily.value_text == "kayıp $1500 / $5000 (%30.0) · kalan $3500"
    assert account.declared_daily.source.startswith("beyan")


def test_default_balance_is_labelled_and_no_declared_loss_is_invented(store: Store) -> None:
    account = view_of(store).account

    assert not account.manual
    assert account.source.startswith("varsayılan")
    assert account.balance_text == "$100000"
    assert [gauge.fraction for gauge in account.targets] == [0.0, 0.0]
    assert account.declared_daily is None
    assert "gün başı bakiyesi girilmedi" in account.declared_daily_note


def test_losses_show_negative_progress_and_a_shrinking_buffer(store: Store) -> None:
    snapshot = ManualSnapshot(
        balance=95000.0, equity=94000.0, day_start_balance=None, entered_at_ms=NOW_MS
    )
    set_state(store, StateKey.MANUAL_ACCOUNT, snapshot_text(snapshot))

    account = view_of(store).account

    assert account.targets[0].fraction == 0.0
    assert account.targets[0].value_text == "−$5000 / $10000 (%-50.0)"
    assert account.total_loss.fraction == pytest.approx(0.4)
    assert account.total_loss.warn


def test_trailing_max_loss_is_not_computed(store: Store) -> None:
    venue = VENUE.model_copy(update={"max_loss_type": MaxLossType.TRAILING_EOD})

    gauge = view_of(store, venue=venue).account.total_loss

    assert gauge.fraction is None
    assert gauge.value_text.startswith("hesaplanamıyor")


def test_single_phase_program_shows_one_target(store: Store) -> None:
    venue = VENUE.model_copy(
        update={"profit_target_pct": VENUE.profit_target_pct.model_copy(update={"phase2": None})}
    )

    assert len(view_of(store, venue=venue).account.targets) == 1


def test_unreadable_snapshot_falls_back_to_the_default_and_says_so(store: Store) -> None:
    set_state(store, StateKey.MANUAL_ACCOUNT, '{"v": 1, "balance": "lots"}')

    account = view_of(store).account

    assert not account.manual
    assert "okunamadı" in account.note


@pytest.mark.parametrize(
    "text",
    [
        "[]",
        '{"v": 2, "balance": 1, "equity": 1, "day_start_balance": null, "entered_at_ms": 1}',
        '{"v": 1, "balance": -1, "equity": 1, "day_start_balance": null, "entered_at_ms": 1}',
        '{"v": 1, "balance": true, "equity": 1, "day_start_balance": null, "entered_at_ms": 1}',
        '{"v": 1, "balance": 1, "equity": 1, "day_start_balance": null, "entered_at_ms": 1.5}',
        '{"v": 1, "balance": NaN, "equity": 1, "day_start_balance": null, "entered_at_ms": 1}',
    ],
)
def test_parse_snapshot_refuses_what_the_panel_did_not_write(text: str) -> None:
    with pytest.raises(ValueError):  # noqa: PT011
        parse_snapshot(text)


# --- prop-day staleness (Prague reset, DST) ---------------------------------------------------


@pytest.mark.parametrize(
    ("entered_ms", "now_ms", "stale"),
    [
        # Autumn: 2026-10-25 lasts 25 hours in Prague (CEST -> CET at 03:00).
        (prague_ms(2026, 10, 25, 0, 30), prague_ms(2026, 10, 25, 23, 59), False),
        (prague_ms(2026, 10, 25, 0, 30), prague_ms(2026, 10, 26, 0, 0), True),
        # 24 hours after the reset is still the same prop day on that date.
        (prague_ms(2026, 10, 25, 0, 30), prague_ms(2026, 10, 25, 0, 0) + DAY_MS, False),
        # Spring: 2026-03-29 lasts 23 hours (CET -> CEST at 02:00).
        (prague_ms(2026, 3, 29, 0, 30), prague_ms(2026, 3, 29, 23, 59), False),
        (prague_ms(2026, 3, 29, 0, 30), prague_ms(2026, 3, 30, 0, 0), True),
        (prague_ms(2026, 3, 29, 0, 30), prague_ms(2026, 3, 29, 0, 0) + 23 * HOUR_MS, True),
        # One minute before the reset vs. the reset itself.
        (prague_ms(2026, 7, 21, 23, 59), prague_ms(2026, 7, 22, 0, 0), True),
    ],
)
def test_manual_values_go_stale_when_the_prop_day_changes(
    store: Store, entered_ms: int, now_ms: int, stale: bool
) -> None:
    snapshot = ManualSnapshot(
        balance=101000.0, equity=101000.0, day_start_balance=100500.0, entered_at_ms=entered_ms
    )
    set_state(store, StateKey.MANUAL_ACCOUNT, snapshot_text(snapshot))

    account = view_of(store, now_ms=now_ms).account

    assert account.stale is stale
    assert ("güncel değil" in account.source) is stale
    # A stale declaration is not shown as today's daily loss.
    assert (account.declared_daily is None) is stale


# --- signals and derived trades ---------------------------------------------------------------


def test_last_signals_newest_first_and_capped(store: Store) -> None:
    for index in range(RECENT_SIGNAL_COUNT + 5):
        tg.add_signal(store, bar_time_ms=BAR_TIME_MS + index * MINUTE_MS)
    tg.add_signal(
        store, action="heartbeat", side=None, bar_time_ms=BAR_TIME_MS, outcome=Outcome.HEARTBEAT
    )

    signals = view_of(store).signals

    assert len(signals) == RECENT_SIGNAL_COUNT
    assert [item.id for item in signals] == sorted((item.id for item in signals), reverse=True)
    assert signals[0].id == RECENT_SIGNAL_COUNT + 5
    assert signals[0].strategy == "NY ORB FVG"
    assert signals[0].outcome == "iletildi"
    assert signals[0].delivery == "mesaj yok"
    assert signals[0].time_text == "2026-07-21 18:31 TR"


def test_hostile_signal_text_reaches_the_view_unchanged(store: Store) -> None:
    hostile = '<script>alert("x")</script>'
    tg.add_signal(store, symbol=hostile, strategy_id='"><img src=x onerror=1>', setup=hostile)

    item = view_of(store).signals[0]

    assert item.symbol == hostile  # escaping is the page's job, tested there
    assert item.strategy == '"><img src=x onerror=1>'


def test_trades_are_derived_from_signals_and_the_ledger(store: Store) -> None:
    closed, _ = tg.forwarded_entry(store, prop_day=TODAY, now_ms=NOW_MS, delivered=True)
    tg.add_signal(store, action="exit", bar_time_ms=BAR_TIME_MS + 10 * MINUTE_MS)
    still_open, _ = tg.forwarded_entry(
        store, prop_day=TODAY, now_ms=NOW_MS, strategy_id="asian_sweep", bar_time_ms=BAR_TIME_MS
    )
    skipped, _ = tg.forwarded_entry(
        store,
        prop_day=TODAY,
        now_ms=NOW_MS,
        strategy_id="mtf_trend_cont",
        bar_time_ms=BAR_TIME_MS + 5 * MINUTE_MS,
    )
    with store.transaction() as tx:
        tx.set_budget_counted(
            signal_id=skipped, counted=False, reason=ReleaseReason.SKIPPED, now_ms=NOW_MS
        )
    old, _ = tg.forwarded_entry(
        store,
        prop_day=TODAY - dt.timedelta(days=2),
        now_ms=NOW_MS,
        strategy_id="mr_bb_kc_rsi2",
        bar_time_ms=BAR_TIME_MS - 2 * DAY_MS,
        received_at_ms=BAR_TIME_MS - 2 * DAY_MS + MINUTE_MS,
    )
    tg.add_signal(
        store,
        strategy_id="ict_silver_bullet",
        bar_time_ms=BAR_TIME_MS,
        outcome=Outcome.SKIP_BUDGET,
    )
    # An exit of another strategy closes nothing of these.
    tg.add_signal(
        store, action="exit", strategy_id="ict_silver_bullet", bar_time_ms=BAR_TIME_MS + MINUTE_MS
    )

    view = view_of(store)

    open_ids = {item.signal_id: item for item in view.open_trades}
    closed_ids = {item.signal_id: item for item in view.closed_trades}
    assert set(open_ids) == {still_open, skipped}
    assert set(closed_ids) == {closed, old}
    assert closed_ids[closed].state == "kapalı"
    assert closed_ids[closed].closed_text.startswith("çıkış sinyali")
    assert closed_ids[old].state == "bilinmiyor"
    assert open_ids[skipped].ledger_mark == "/atladim ile serbest"
    assert open_ids[still_open].ledger_mark == "bütçede sayılıyor"
    assert open_ids[still_open].levels == "giriş 5123.25 · stop 5110.75 · TP 5148.25"
    assert "dolum değildir" in view.trades_note


def test_an_exit_after_a_newer_entry_closes_only_the_newer_one(store: Store) -> None:
    first, _ = tg.forwarded_entry(store, prop_day=TODAY, now_ms=NOW_MS)
    second, _ = tg.forwarded_entry(
        store, prop_day=TODAY, now_ms=NOW_MS, bar_time_ms=BAR_TIME_MS + 5 * MINUTE_MS
    )
    tg.add_signal(store, action="cancel", bar_time_ms=BAR_TIME_MS + 6 * MINUTE_MS)

    view = view_of(store)

    assert [item.signal_id for item in view.closed_trades] == [second]
    assert [item.signal_id for item in view.open_trades] == [first]
    assert view.closed_trades[0].closed_text.startswith("iptal sinyali")


# --- database problems are states ------------------------------------------------------------


def test_missing_database_is_reported_and_not_created(tmp_path: Path) -> None:
    path = tmp_path / "propbot.db"
    view = load_view(StoreSource(path), venue=VENUE, venue_name="x", risk=RISK, now_ms=NOW_MS)

    assert view.db.state is DbState.MISSING
    assert view.db.message == "veritabanı yok — propbot serve hiç çalışmamış olabilir"
    assert view.bot is None
    assert view.signals == ()
    assert view.account.balance_text == "$100000"
    assert view.account.ledger[0].fraction is None
    assert not path.exists()
    assert list(tmp_path.iterdir()) == []


def test_empty_database_file_is_left_untouched(tmp_path: Path) -> None:
    path = tmp_path / "propbot.db"
    path.touch()

    view = load_view(StoreSource(path), venue=VENUE, venue_name="x", risk=RISK, now_ms=NOW_MS)

    assert view.db.state is DbState.EMPTY
    assert path.stat().st_size == 0


def test_schema_version_mismatch_shows_the_store_error(tmp_path: Path) -> None:
    path = tmp_path / "propbot.db"
    Store(path).close()
    with sqlite3.connect(path) as conn:
        conn.execute("UPDATE meta SET value = '2' WHERE key = 'schema_version'")
    conn.close()

    view = load_view(StoreSource(path), venue=VENUE, venue_name="x", risk=RISK, now_ms=NOW_MS)

    assert view.db.state is DbState.ERROR
    assert "şema sürümü uyuşmuyor" in view.db.message
    assert "runbook" in view.db.message


def test_a_locked_database_is_busy_and_recovers_on_the_next_refresh(tmp_path: Path) -> None:
    # Takes the store's busy timeout (5 s) once: the wait is SQLite's, not the test's. A
    # write lock no longer stops the panel's reads (next test); a connection in exclusive
    # locking mode keeps even readers out, so that is the lock used here.
    path = tmp_path / "propbot.db"
    Store(path).close()
    source = StoreSource(path)
    blocker = sqlite3.connect(path, isolation_level=None, timeout=0)
    try:
        blocker.execute("PRAGMA locking_mode = EXCLUSIVE")
        blocker.execute("BEGIN EXCLUSIVE")
        busy = load_view(source, venue=VENUE, venue_name="x", risk=RISK, now_ms=NOW_MS)
        blocker.execute("ROLLBACK")
    finally:
        blocker.close()
    again = load_view(source, venue=VENUE, venue_name="x", risk=RISK, now_ms=NOW_MS)
    source.close()

    assert busy.db.state is DbState.BUSY
    assert "kilitli" in busy.db.message
    assert again.db.ok


@pytest.mark.parametrize("lock", ["BEGIN IMMEDIATE", "BEGIN EXCLUSIVE"])
def test_a_refresh_succeeds_while_serve_holds_the_write_lock(tmp_path: Path, lock: str) -> None:
    # ADR-0009: the panel cannot disturb the signal path. It opens the file and reads it
    # without the write lock, so `serve` holding (or waiting for) that lock delays nothing.
    path = tmp_path / "propbot.db"
    with Store(path) as serve:
        tg.forwarded_entry(serve, prop_day=TODAY, now_ms=NOW_MS)
    source = StoreSource(path)
    writer = sqlite3.connect(path, isolation_level=None, timeout=0)
    try:
        writer.execute(lock)
        writer.execute("INSERT INTO state (key, value, updated_at_ms) VALUES ('killed', '1', 1)")
        # The first open happens under the lock too, then any wait becomes an error at once.
        store = source.get()
        assert isinstance(store, Store)
        assert store._conn is not None
        store._conn.execute("PRAGMA busy_timeout = 0")
        view = load_view(source, venue=VENUE, venue_name="x", risk=RISK, now_ms=NOW_MS)
        writer.execute("COMMIT")
    finally:
        writer.close()
    after = load_view(source, venue=VENUE, venue_name="x", risk=RISK, now_ms=NOW_MS)
    source.close()

    assert view.db.ok
    assert len(view.signals) == 1
    assert view.bot is not None
    assert view.bot.state is BotState.ACTIVE  # the uncommitted KILL is not shown
    assert after.bot is not None
    assert after.bot.state is BotState.KILLED


def test_a_refresh_leaves_the_store_writable_for_the_panel_actions(store: Store) -> None:
    view_of(store)

    set_state(store, StateKey.PAUSED, "1")

    view = view_of(store)
    assert view.bot is not None
    assert view.bot.state is BotState.PAUSED


def test_signals_beyond_the_row_limit_are_cut_and_said_so(
    store: Store, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(live, "MAX_SIGNAL_ROWS", 3)
    ids = [tg.add_signal(store, bar_time_ms=BAR_TIME_MS + index * MINUTE_MS) for index in range(5)]
    # Outside the window: received more than a day ahead, or before the lookback.
    tg.add_signal(store, bar_time_ms=BAR_TIME_MS - MINUTE_MS, received_at_ms=NOW_MS + DAY_MS)
    tg.add_signal(
        store,
        bar_time_ms=BAR_TIME_MS - 2 * MINUTE_MS,
        received_at_ms=NOW_MS - live.SIGNAL_LOOKBACK_MS - 1,
    )

    view = view_of(store)

    assert [item.id for item in view.signals] == ids[:1:-1]
    assert "yalnızca en yeni 3 sinyal incelendi" in view.trades_note


def test_all_signals_of_the_window_without_a_cut_note(
    store: Store, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(live, "MAX_SIGNAL_ROWS", 3)
    oldest = tg.add_signal(
        store,
        bar_time_ms=BAR_TIME_MS - MINUTE_MS,
        received_at_ms=NOW_MS - live.SIGNAL_LOOKBACK_MS,  # the lower bound is inclusive
    )
    ids = [tg.add_signal(store, bar_time_ms=BAR_TIME_MS + index * MINUTE_MS) for index in range(2)]

    view = view_of(store)

    assert [item.id for item in view.signals] == [*ids[::-1], oldest]
    assert "incelendi" not in view.trades_note


def test_emptied_tables_give_an_empty_screen(tmp_path: Path) -> None:
    path = tmp_path / "propbot.db"
    with Store(path) as store:
        tg.forwarded_entry(store, prop_day=TODAY, now_ms=NOW_MS)
        set_state(store, StateKey.KILLED, "1")
    with sqlite3.connect(path) as conn:
        for table in ("outbox", "budget_entries", "signals", "state"):
            conn.execute(f"DELETE FROM {table}")
    conn.close()
    source = StoreSource(path)

    view = load_view(source, venue=VENUE, venue_name="x", risk=RISK, now_ms=NOW_MS)
    source.close()

    assert view.db.ok
    assert view.bot is not None
    assert view.bot.state is BotState.ACTIVE
    assert view.signals == ()
    assert view.open_trades == ()
    assert view.closed_trades == ()
    assert view.account.ledger[0].value_text == "$0 / $1000 (%0.0) · 0 giriş"


def test_view_json_is_plain_data(store: Store) -> None:
    tg.forwarded_entry(store, prop_day=TODAY, now_ms=NOW_MS)

    data = view_json(view_of(store))

    assert data["db"] == {"state": "ok", "message": "veritabanı: tamam", "ok": True}
    assert data["bot"]["state"] == "active"  # type: ignore[index]
    assert isinstance(data["signals"], list)
    assert len(data["signals"]) == 1
