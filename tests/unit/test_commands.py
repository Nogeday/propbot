"""Telegram commands: every command and reply, PIN hygiene and lock, age rule, budget ledger."""

import ast
import dataclasses
import datetime as dt
import hmac
from collections.abc import Iterator
from pathlib import Path

import pytest
import structlog
from hypothesis import assume, given, settings
from hypothesis import strategies as st
from structlog.testing import capture_logs

from propbot.notify import commands as commands_module
from propbot.notify.commands import (
    MAX_RISK_ENTRIES,
    PIN_FAILURE_WINDOW_MS,
    PIN_LOCK_MS,
    PIN_MAX_FAILURES,
    STALE_COMMAND_MS,
    CommandDeps,
    Commands,
    Reply,
)
from propbot.risk.loader import load_risk_config, load_venue_profile
from propbot.risk.venue import VenueProfile
from propbot.signals.tvconfig import TvAlertsConfig, load_tv_alerts_config
from propbot.store.db import OutboxKind, Outcome, ReleaseReason, StateKey, Store
from support import tg
from support.alerts import BAR_TIME_MS
from support.paths import CONFIG_FIXTURES, FIXTURES

PIN = "246813"
WRONG_PIN = "135790"
MINUTE_MS = 60_000
# 2026-07-21 15:31:01.5 UTC = 11:31 New York = 18:31 Istanbul = 17:31 Prague (a Tuesday),
# 1.5 s after the reference bar closed.
NOW_MS = BAR_TIME_MS + MINUTE_MS + 1_500
TODAY = dt.date(2026, 7, 21)
STARTED_MS = NOW_MS - (2 * 60 + 5) * MINUTE_MS
VENUE_NAME = "sample_2step"

RISK = load_risk_config(CONFIG_FIXTURES / "valid" / "risk.yaml")
# The fixture's dates lie after the reference bar; move them so the rules are 12 days old.
VENUE = load_venue_profile(CONFIG_FIXTURES / "valid" / "venues" / "sample_2step.yaml").model_copy(
    update={"rules_verified_at": dt.date(2026, 7, 9)}
)
_TV_FIXTURE = load_tv_alerts_config(FIXTURES / "tv_alerts" / "valid" / "full.yaml")
TV = _TV_FIXTURE.model_copy(
    update={
        "webhook": _TV_FIXTURE.webhook.model_copy(update={"rules_verified_at": dt.date(2026, 7, 1)})
    }
)

HELP = (
    "PropBot komutları (mod: signal — emir gönderilmez)\n"
    "/durum — mod, durum, kuyruk, bugünkü sinyaller\n"
    "/pnl — bugünkü girişler ve sayılan risk\n"
    "/risk — risk ayarları ve günlük bütçe defteri\n"
    "/saglik — sağlık, gecikme, saat\n"
    "/duraklat — giriş sinyallerini durdur (stop/çıkış/iptal gelmeye devam eder)\n"
    "/devam — yeniden başlat (KILL sonrası: /devam <PIN>)\n"
    "/kill <PIN> — giriş sinyallerini kes; yalnızca /devam <PIN> açar\n"
    "/atladim [id] — girişi almadım: riskini günlük bütçeden düş\n"
    "/aldim <id> — /atladim'ı geri al\n"
    "/mod — çalışma modu\n"
    "/yardim — bu liste"
)
ENTRY_ONLY = "Stop güncelleme / çıkış / iptal mesajları gelmeye devam eder."
KILL_REPLY = (
    "KILL etkin: giriş sinyalleri kesildi. Açık pozisyonun ya da bekleyen emrin varsa platformda "
    f"ELLE kapat (bot emir göndermez). {ENTRY_ONLY} Yeniden açmak için yalnızca: /devam <PIN>"
)
PAUSE_REPLY = f"Duraklatıldı: yeni GİRİŞ sinyalleri iletilmez. {ENTRY_ONLY} Açmak için: /devam"


def wrong_pin_reply(remaining: int, tail: str = "PIN'siz acil durdurma: /duraklat") -> str:
    return (
        f"PIN hatalı. Kalan deneme: {remaining} (15 dk içinde 5 hata PIN girişini 15 dk "
        f"kilitler). {tail}"
    )


class FakeClock:
    def __init__(self, now_ms: int = NOW_MS) -> None:
        self.now_ms = now_ms

    def __call__(self) -> int:
        return self.now_ms


class World:
    """Commands on an in-memory store with a clock the test moves."""

    def __init__(
        self, *, venue: VenueProfile = VENUE, tv: TvAlertsConfig = TV, now_ms: int = NOW_MS
    ) -> None:
        self.store = Store(":memory:")
        self.clock = FakeClock(now_ms)
        self.rejects: dict[str, int] = {"bad_json": 0, "bad_token": 0, "ip_not_allowed": 0}
        self.deps = CommandDeps(
            store=self.store,
            risk=RISK,
            venue=venue,
            venue_name=VENUE_NAME,
            tv=tv,
            pin=PIN,
            clock=self.clock,
            started_ms=STARTED_MS,
            reject_counts=lambda: self.rejects,
        )
        self.commands = Commands(self.deps)

    def say(self, text: str, *, age_ms: int = 0) -> Reply:
        """Handle a message that Telegram stamped `age_ms` before the current time."""
        return self.commands.handle(text, sent_at_ms=self.clock.now_ms - age_ms)

    def text(self, message: str, *, age_ms: int = 0) -> str:
        return self.say(message, age_ms=age_ms).text

    def flag(self, key: StateKey) -> str | None:
        with self.store.transaction() as tx:
            return tx.get_state(key)

    def state(self) -> dict[str, str | None]:
        with self.store.transaction() as tx:
            return {key.value: tx.get_state(key) for key in StateKey}

    def entry(self, *, delivered: bool = True, offset: int = 0, **overrides: object) -> int:
        """A forwarded entry of today on the bar `offset` minutes after the reference bar."""
        fields: dict[str, object] = {
            "bar_time_ms": BAR_TIME_MS + offset * MINUTE_MS,
            "bar_close_ms": BAR_TIME_MS + (offset + 1) * MINUTE_MS,
            "prop_day": TODAY,
        }
        fields.update(overrides)
        signal_id, _outbox_id = tg.forwarded_entry(
            self.store,
            now_ms=self.clock.now_ms,
            delivered=delivered,
            **fields,  # type: ignore[arg-type]
        )
        return signal_id

    def release(self, signal_id: int, reason: ReleaseReason) -> None:
        with self.store.transaction() as tx:
            tx.set_budget_counted(
                signal_id=signal_id, counted=False, reason=reason, now_ms=self.clock.now_ms
            )

    def counted(self, signal_id: int) -> tuple[bool, ReleaseReason | None]:
        with self.store.transaction() as tx:
            entry = tx.get_budget_entry(signal_id)
        assert entry is not None
        return entry.counted, entry.release_reason

    def used(self, day: dt.date = TODAY) -> float:
        with self.store.transaction() as tx:
            return tx.budget_used(day)


@pytest.fixture
def make() -> Iterator[type[World]]:
    """World factory; every store it hands out is closed afterwards."""
    created: list[World] = []

    class Tracked(World):
        def __init__(self, **kwargs: object) -> None:
            super().__init__(**kwargs)  # type: ignore[arg-type]
            created.append(self)

    structlog.reset_defaults()
    yield Tracked
    for world in created:
        world.store.close()


@pytest.fixture
def world(make: type[World]) -> World:
    return make()


# --- shape of the module ------------------------------------------------------------------


def test_command_logic_does_not_import_the_telegram_library() -> None:
    tree = ast.parse(Path(commands_module.__file__).read_text(encoding="utf-8"))
    imported = {
        (node.module or "") if isinstance(node, ast.ImportFrom) else alias.name
        for node in ast.walk(tree)
        if isinstance(node, ast.Import | ast.ImportFrom)
        for alias in node.names
    }
    assert not any(name.split(".")[0] == "telegram" for name in imported)


def test_deps_are_frozen_keyword_only_and_hide_the_pin(world: World) -> None:
    assert PIN not in repr(world.deps)
    with pytest.raises(dataclasses.FrozenInstanceError):
        world.deps.pin = "000000"  # type: ignore[misc]
    with pytest.raises(TypeError):
        CommandDeps(world.store)  # type: ignore[call-arg]


def test_reply_defaults_to_keeping_the_users_message() -> None:
    assert Reply("x") == Reply(text="x", delete_request=False)
    with pytest.raises(dataclasses.FrozenInstanceError):
        Reply("x").text = "y"  # type: ignore[misc]


@pytest.mark.parametrize("pin", ["", "12345", "1234567890123", "12345a", "١٢٣٤٥٦", " 246813"])
def test_a_malformed_pin_is_refused_at_construction_without_printing_it(
    world: World, pin: str
) -> None:
    with pytest.raises(ValueError, match="pin must be 6 to 12 digits") as caught:
        Commands(dataclasses.replace(world.deps, pin=pin))
    assert pin == "" or pin not in str(caught.value)


# --- help and unknown input ---------------------------------------------------------------


@pytest.mark.parametrize("message", ["/yardim", "/start", "/YARDIM", "/yardım", "/yardim@propbot"])
def test_help_lists_every_command(world: World, message: str) -> None:
    assert world.say(message) == Reply(HELP)


@pytest.mark.parametrize("message", ["/foo", "/", "/durumm", "/kil"])
def test_unknown_command_gets_the_help_list(world: World, message: str) -> None:
    assert world.say(message) == Reply("Bilinmeyen komut.\n" + HELP)


@pytest.mark.parametrize("message", ["selam", "", "   ", "durum", "x /durum"])
def test_text_that_is_not_a_command_gets_the_help_list(world: World, message: str) -> None:
    assert world.say(message) == Reply(HELP)


def test_help_names_every_dispatched_command_except_start(world: World) -> None:
    names = set(world.commands._commands) - {"start"}
    assert names == {
        "durum",
        "pnl",
        "risk",
        "saglik",
        "duraklat",
        "devam",
        "kill",
        "atladim",
        "aldim",
        "mod",
        "yardim",
    }
    for name in names:
        assert f"/{name}" in HELP


@pytest.mark.parametrize(
    "message",
    ["/DURAKLAT", "/Duraklat", "/duraklat@propbot_test_bot", "/DURAKLAT@Bot", "  /duraklat  "],
)
def test_commands_are_case_insensitive_and_accept_a_bot_suffix(world: World, message: str) -> None:
    assert world.text(message) == PAUSE_REPLY
    assert world.flag(StateKey.PAUSED) == "1"


@pytest.mark.parametrize(
    ("typed", "expected_start"),
    [
        ("/atladım", "Bugün (prop günü 2026-07-21) sana ulaşmış bir giriş sinyali yok."),
        ("/ATLADIM", "Bugün (prop günü 2026-07-21) sana ulaşmış bir giriş sinyali yok."),
        ("/aldım", "Kullanım: /aldim <id>"),
        ("/sağlık", "[SAĞLIK]"),
        ("/SAĞLIK", "[SAĞLIK]"),
        ("/YARDİM", "PropBot komutları"),
    ],
)
def test_turkish_keyboard_spellings_reach_the_ascii_command(
    world: World, typed: str, expected_start: str
) -> None:
    assert world.text(typed).startswith(expected_start)


# --- /mod ---------------------------------------------------------------------------------


def test_mod_shows_signal(world: World) -> None:
    assert world.say("/mod") == Reply(
        "Mod: signal — yalnızca sinyal mesajı gönderilir, emir gönderilmez. "
        "Diğer modlar (paper, approve, auto) bu sürümde yok."
    )


@pytest.mark.parametrize(
    ("message", "expected"),
    [
        ("/mod signal", "Mod zaten signal; değişiklik yok."),
        (
            "/mod paper",
            "Mod değişmedi (signal). 'paper' modu bu sürümde yok: yalnızca 'signal' "
            "(emir gönderilmez) çalışır.",
        ),
        (
            "/mod APPROVE",
            "Mod değişmedi (signal). 'approve' modu bu sürümde yok: yalnızca 'signal' "
            "(emir gönderilmez) çalışır.",
        ),
        (
            "/mod auto",
            "Mod değişmedi (signal). 'auto' modu bu sürümde yok: yalnızca 'signal' "
            "(emir gönderilmez) çalışır; auto için ayrıca gerekir: config live.enabled, "
            "--i-accept-live-risk, Telegram PIN onayı ve PASS veren Go/No-Go raporu.",
        ),
        (
            "/mod turbo",
            "Bilinmeyen mod; geçerli modlar: signal, paper, approve, auto. Mod değişmedi: signal.",
        ),
        (
            "/mod auto now",
            "Bilinmeyen mod; geçerli modlar: signal, paper, approve, auto. Mod değişmedi: signal.",
        ),
    ],
)
def test_mod_never_changes_anything(world: World, message: str, expected: str) -> None:
    before = world.state()
    assert world.say(message) == Reply(expected)
    assert world.state() == before


def test_mod_refusal_names_stale_rules(make: type[World]) -> None:
    stale = make(venue=VENUE.model_copy(update={"rules_verified_at": dt.date(2026, 5, 1)}))
    assert stale.text("/mod approve") == (
        "Mod değişmedi (signal). 'approve' modu bu sürümde yok: yalnızca 'signal' (emir "
        "gönderilmez) çalışır; venue kuralları taze değil (rules_verified_at boş, ileri tarihli "
        "ya da 30 günden eski)."
    )


# --- /durum -------------------------------------------------------------------------------


def test_durum_on_an_empty_database(world: World) -> None:
    assert world.say("/durum") == Reply(
        "[DURUM] 18:31:01 TR\n"
        "Mod: signal (emir gönderilmez) · Durum: aktif\n"
        "Venue: sample_2step · SAMPLEFIRM US500.sample\n"
        "Sinyal kuyruğu: 0 · Telegram kutusu: 0 bekliyor, 0 başarısız\n"
        "Bugünkü sinyaller (prop günü 2026-07-21): yok\n"
        "Son sinyal alert'i: yok\n"
        "Son heartbeat: yok\n"
        "Çalışma süresi: 2 sa 5 dk"
    )


def test_durum_counts_todays_signals_by_outcome(world: World) -> None:
    world.entry(delivered=True)
    world.entry(delivered=False, offset=1, strategy_id="asian_sweep")
    tg.add_signal(world.store, outcome=Outcome.STALE, bar_time_ms=BAR_TIME_MS + 2 * MINUTE_MS)
    tg.add_signal(
        world.store,
        outcome=Outcome.NO_ACTIVE_TRADE,
        action="exit",
        received_at_ms=NOW_MS - 2 * MINUTE_MS,
    )
    tg.add_signal(world.store, outcome=None, bar_time_ms=BAR_TIME_MS + 3 * MINUTE_MS)
    tg.add_signal(
        world.store,
        outcome=Outcome.HEARTBEAT,
        strategy_id="heartbeat",
        action="heartbeat",
        received_at_ms=NOW_MS - 5 * MINUTE_MS,
    )
    # Received before the prop day began (Prague midnight): not one of today's signals.
    tg.add_signal(
        world.store,
        outcome=Outcome.SKIP_BUDGET,
        bar_time_ms=BAR_TIME_MS - 24 * 60 * MINUTE_MS,
        received_at_ms=NOW_MS - 24 * 60 * MINUTE_MS,
    )
    with world.store.transaction() as tx:
        failed = tx.enqueue_message(kind=OutboxKind.ALARM, text="x", now_ms=NOW_MS)
        tx.mark_message_failed(failed, error="BadRequest", now_ms=NOW_MS)

    assert world.text("/durum") == (
        "[DURUM] 18:31:01 TR\n"
        "Mod: signal (emir gönderilmez) · Durum: aktif\n"
        "Venue: sample_2step · SAMPLEFIRM US500.sample\n"
        "Sinyal kuyruğu: 1 · Telegram kutusu: 1 bekliyor, 1 başarısız\n"
        "Bugünkü sinyaller (prop günü 2026-07-21): iletildi 2 · bayat 1 · açık işlem yok 1 "
        "· kuyrukta 1\n"
        "Son sinyal alert'i: 18:31 TR (0 sn önce)\n"
        "Son heartbeat: 18:26 TR (5 dk önce)\n"
        "Çalışma süresi: 2 sa 5 dk"
    )


def test_durum_dates_an_alert_of_another_day(world: World) -> None:
    tg.add_signal(world.store, received_at_ms=NOW_MS - 26 * 60 * MINUTE_MS)
    assert "Son sinyal alert'i: 2026-07-20 16:31 TR (1 gün 2 sa önce)\n" in world.text("/durum")


def test_durum_shows_pause_and_kill(world: World) -> None:
    world.say("/duraklat")
    assert "Durum: duraklatıldı (giriş sinyalleri iletilmiyor; açmak için /devam)\n" in world.text(
        "/durum"
    )
    world.say(f"/kill {PIN}")
    assert "Durum: KILL (giriş sinyalleri kesildi; açmak için /devam <PIN>)\n" in world.text(
        "/durum"
    )


def test_every_outcome_has_a_turkish_label() -> None:
    assert set(commands_module._OUTCOME_LABELS) == set(Outcome)


# --- /pnl ---------------------------------------------------------------------------------


def test_pnl_says_there_is_no_realized_pnl_in_this_phase(world: World) -> None:
    assert world.say("/pnl") == Reply(
        "[K/Z] Faz 1'de emir gönderilmez; gerçekleşen kâr/zarar tutulmaz (işlemlerini kendi "
        "platformundan izle).\n"
        "Bugün (prop günü 2026-07-21): 0 giriş iletildi · sayılan risk $0/$1000"
    )


def test_pnl_shows_todays_entries_and_counted_risk(world: World) -> None:
    first = world.entry()
    world.entry(offset=1)
    world.entry(offset=2, prop_day=dt.date(2026, 7, 20))  # another prop day
    world.release(first, ReleaseReason.SKIPPED)
    assert world.text("/pnl").splitlines()[1] == (
        "Bugün (prop günü 2026-07-21): 2 giriş iletildi (1 tanesi serbest bırakıldı) · "
        "sayılan risk $250/$1000"
    )


# --- /risk --------------------------------------------------------------------------------


def test_risk_without_entries(world: World) -> None:
    assert world.say("/risk") == Reply(
        "[RİSK] sample_2step · SAMPLEFIRM US500.sample\n"
        "İşlem başı risk: %0.25 = $250\n"
        "Günlük bütçe: $0/$1000 kullanıldı — ihtiyatlı defter: iletilen her giriş prop günü "
        "bitene kadar sayılır; almadıysan /atladim ile serbest bırak\n"
        "Bugünkü girişler (prop günü 2026-07-21): yok\n"
        "Venue kuralları: taze (12 gün önce doğrulandı)\n"
        "Boş contract alanları: yok"
    )


def test_risk_lists_todays_entries_with_their_ledger_state(world: World) -> None:
    world.entry()
    skipped = world.entry(offset=1, strategy_id="asian_sweep", side="short")
    undelivered = world.entry(offset=2, strategy_id="ict_silver_bullet", risk_amount=249.5)
    world.entry(offset=3, prop_day=dt.date(2026, 7, 20))
    world.release(skipped, ReleaseReason.SKIPPED)
    world.release(undelivered, ReleaseReason.UNDELIVERED)
    assert world.text("/risk") == (
        "[RİSK] sample_2step · SAMPLEFIRM US500.sample\n"
        "İşlem başı risk: %0.25 = $250\n"
        "Günlük bütçe: $250/$1000 kullanıldı — ihtiyatlı defter: iletilen her giriş prop günü "
        "bitene kadar sayılır; almadıysan /atladim ile serbest bırak\n"
        "Bugünkü girişler (prop günü 2026-07-21):\n"
        "id 1 · NY ORB FVG · LONG · bar 11:30 NY · $250 · sayıldı\n"
        "id 2 · Asian Sweep · SHORT · bar 11:31 NY · $250 · atlandı\n"
        "id 3 · ICT Silver Bullet · LONG · bar 11:32 NY · $249.50 · iletilemedi\n"
        "Venue kuralları: taze (12 gün önce doğrulandı)\n"
        "Boş contract alanları: yok"
    )


def test_risk_shows_at_most_fifteen_entries_and_stays_short(world: World) -> None:
    for offset in range(MAX_RISK_ENTRIES + 2):
        world.entry(offset=offset, risk_amount=50.0, strategy_id="mr_bb_kc_rsi2")
    reply = world.text("/risk")
    lines = reply.splitlines()
    entry_lines = [line for line in lines if line.startswith("id ")]
    assert len(entry_lines) == MAX_RISK_ENTRIES == 15
    assert entry_lines[0].startswith("id 3 · ")
    assert entry_lines[-1].startswith("id 17 · ")
    assert lines[4] == "(önceki 2 giriş gösterilmiyor)"
    assert len(reply.encode("utf-16-le")) // 2 < 4096 // 2
    assert "Günlük bütçe: $850/$1000 kullanıldı" in reply


@pytest.mark.parametrize(
    ("verified", "expected"),
    [
        (
            dt.date(2026, 6, 20),
            "Venue kuralları: ESKİ (31 gün önce doğrulandı; sınır 30 gün) — resmi sayfadan "
            "doğrula ve rules_verified_at'i güncelle",
        ),
        (dt.date(2026, 6, 21), "Venue kuralları: taze (30 gün önce doğrulandı)"),
        (None, "Venue kuralları: DOĞRULANMADI (rules_verified_at boş) — resmi sayfadan doğrula"),
        (
            dt.date(2026, 7, 23),
            "Venue kuralları: GEÇERSİZ (rules_verified_at ileri tarihli) — tarihi düzelt",
        ),
    ],
)
def test_risk_and_saglik_report_rules_freshness(
    make: type[World], verified: dt.date | None, expected: str
) -> None:
    world = make(venue=VENUE.model_copy(update={"rules_verified_at": verified}))
    assert expected in world.text("/risk").splitlines()
    assert expected in world.text("/saglik").splitlines()


def test_risk_names_the_contract_fields_that_are_still_null(make: type[World]) -> None:
    contract = VENUE.contract.model_copy(update={"tick_size": None, "lot_step": None})
    world = make(venue=VENUE.model_copy(update={"contract": contract}))
    assert world.text("/risk").splitlines()[-1] == (
        "Boş contract alanları: tick_size, lot_step — boyut önerisi hesaplanamaz"
    )


# --- /saglik ------------------------------------------------------------------------------


def test_saglik_full_report(world: World) -> None:
    world.entry(delivered=True)  # received 1.5 s after the close, sent at the same instant
    # The bar before: received 2.5 s after its close, its message still pending.
    world.entry(delivered=False, offset=-1, received_at_ms=BAR_TIME_MS + 2_500)
    tg.add_signal(
        world.store,
        outcome=Outcome.HEARTBEAT,
        strategy_id="heartbeat",
        action="heartbeat",
        received_at_ms=NOW_MS - 5 * MINUTE_MS,
    )
    world.rejects.update(bad_token=2, bad_json=1)
    world.commands.note_unauthorized()
    world.commands.note_unauthorized()
    assert world.say("/saglik", age_ms=1_000) == Reply(
        "[SAĞLIK] 18:31:01 TR\n"
        "Çalışma süresi: 2 sa 5 dk\n"
        "Veritabanı: tamam\n"
        "Sinyal kuyruğu: 0\n"
        "Telegram kutusu: 1 bekliyor · 1 gönderildi · 0 süresi doldu · 0 başarısız\n"
        "Son sinyal alert'i: 18:31 TR (0 sn önce)\n"
        "Son heartbeat: 18:26 TR (5 dk önce)\n"
        "Gecikme medyanı (son 2 sinyal): bar kapanışı → alım 2.0 sn · "
        "bar kapanışı → Telegram 1.5 sn (1 mesaj)\n"
        "İzleme penceresi: açık (alarm hattının sessizliği izleniyor)\n"
        "Reddedilen webhook istekleri: bad_json 1 · bad_token 2\n"
        "Yetkisiz sohbetlerden mesaj: 2\n"
        "Saat farkı (sunucu − Telegram mesaj saati): +1.0 sn — kaba tahmin, ±2 sn (mesajın "
        "yolda geçen süresini içerir)\n"
        "Venue kuralları: taze (12 gün önce doğrulandı)\n"
        "TradingView webhook kuralları: 2026-07-01 tarihinde doğrulandı (20 gün önce)"
    )


def test_saglik_on_an_empty_database_outside_the_watch_window(make: type[World]) -> None:
    # 2026-07-25 is a Saturday: the fixture watches Monday to Friday only.
    world = make(now_ms=NOW_MS + 4 * 24 * 60 * MINUTE_MS)
    lines = world.text("/saglik").splitlines()
    assert lines[5:11] == [
        "Son sinyal alert'i: yok",
        "Son heartbeat: yok",
        "Gecikme medyanı: ölçüm yok",
        "İzleme penceresi: kapalı",
        "Reddedilen webhook istekleri: yok",
        "Yetkisiz sohbetlerden mesaj: 0",
    ]


def test_saglik_median_uses_the_last_fifty_samples_and_reports_missing_sends(
    world: World,
) -> None:
    # 60 signals, each received (index) ms after its bar closed; none sent to Telegram.
    for index in range(60):
        close_ms = BAR_TIME_MS + (index + 1) * MINUTE_MS
        tg.add_signal(
            world.store,
            bar_time_ms=close_ms - MINUTE_MS,
            bar_close_ms=close_ms,
            received_at_ms=close_ms + index * 100,
        )
    world.clock.now_ms = BAR_TIME_MS + 62 * MINUTE_MS
    # Samples 10..59 -> latencies 1000..5900 ms -> median 3450 ms.
    assert (
        "Gecikme medyanı (son 50 sinyal): bar kapanışı → alım 3.5 sn · bar kapanışı → Telegram "
        "ölçüm yok"
    ) in world.text("/saglik").splitlines()


def test_saglik_warns_when_the_server_clock_is_behind_telegram(world: World) -> None:
    line = (
        "Saat farkı (sunucu − Telegram mesaj saati): −5.0 sn — kaba tahmin, ±2 sn (mesajın "
        "yolda geçen süresini içerir). UYARI: sunucu saati geride görünüyor; chronyc tracking "
        "ile kontrol et"
    )
    assert line in world.text("/saglik", age_ms=-5_000).splitlines()
    assert "UYARI" not in world.text("/saglik", age_ms=-2_000)


def test_saglik_marks_future_dated_webhook_rules(make: type[World]) -> None:
    world = make(tv=_TV_FIXTURE)  # the fixture says 2026-11-01
    assert world.text("/saglik").splitlines()[-1] == (
        "TradingView webhook kuralları: 2026-11-01 tarihinde doğrulandı (İLERİ TARİHLİ: düzelt)"
    )


def test_saglik_still_answers_when_the_database_fails(world: World) -> None:
    world.store.close()
    with capture_logs() as logs:
        reply = world.say("/saglik")
    lines = reply.text.splitlines()
    assert lines[:3] == [
        "[SAĞLIK] 18:31:01 TR",
        "Çalışma süresi: 2 sa 5 dk",
        "Veritabanı: HATA (StoreError) — sinyaller işlenemiyor olabilir; log'a bak",
    ]
    assert lines[3] == "İzleme penceresi: açık (alarm hattının sessizliği izleniyor)"
    assert lines[-1].startswith("TradingView webhook kuralları: 2026-07-01")
    assert [entry["event"] for entry in logs if entry["log_level"] == "error"] == [
        "telegram_health_store_failed"
    ]


# --- /duraklat and /devam -----------------------------------------------------------------


def test_duraklat_sets_the_pause_flag_and_is_idempotent(world: World) -> None:
    assert world.say("/duraklat") == Reply(PAUSE_REPLY)
    assert world.flag(StateKey.PAUSED) == "1"
    assert world.text("/duraklat") == (
        f"Zaten duraklatılmış: yeni GİRİŞ sinyalleri iletilmez. {ENTRY_ONLY} Açmak için: /devam"
    )
    assert world.flag(StateKey.PAUSED) == "1"
    assert world.flag(StateKey.KILLED) is None


def test_duraklat_is_applied_whatever_its_age_and_states_the_delay(world: World) -> None:
    assert world.text("/duraklat", age_ms=STALE_COMMAND_MS) == PAUSE_REPLY  # exactly 60 s: fresh
    world.say("/devam")
    assert world.text("/duraklat", age_ms=5 * MINUTE_MS) == (
        PAUSE_REPLY + "\n(Komut 5 dk gecikmeyle işlendi.)"
    )
    assert world.flag(StateKey.PAUSED) == "1"


def test_duraklat_while_killed_points_at_the_pin(world: World) -> None:
    world.say(f"/kill {PIN}")
    assert world.text("/duraklat") == (
        f"Duraklatıldı: yeni GİRİŞ sinyalleri iletilmez. {ENTRY_ONLY} "
        "KILL de etkin; açmak için: /devam <PIN>"
    )


def test_devam_clears_the_pause(world: World) -> None:
    world.say("/duraklat")
    assert world.say("/devam") == Reply("Devam: giriş sinyalleri yeniden iletilecek.")
    assert world.flag(StateKey.PAUSED) is None
    assert world.say("/devam") == Reply("Zaten aktif: giriş sinyalleri iletiliyor.")


def test_devam_with_a_needless_argument_still_asks_for_deletion(world: World) -> None:
    world.say("/duraklat")
    with capture_logs() as logs:
        reply = world.say(f"/devam {WRONG_PIN}")
    # Not killed: no PIN is needed, nothing is compared or counted.
    assert reply == Reply("Devam: giriş sinyalleri yeniden iletilecek.", delete_request=True)
    assert world.flag(StateKey.PIN_FAIL_COUNT) is None
    assert WRONG_PIN not in repr(logs)


def test_devam_after_kill_needs_the_pin(world: World) -> None:
    world.say("/duraklat")
    world.say(f"/kill {PIN}")
    assert world.say("/devam") == Reply("KILL etkin: yeniden açmak için PIN gerekir → /devam <PIN>")
    assert world.say(f"/devam {WRONG_PIN}") == Reply(
        wrong_pin_reply(4, "KILL etkin kalıyor."), delete_request=True
    )
    assert (world.flag(StateKey.KILLED), world.flag(StateKey.PAUSED)) == ("1", "1")

    assert world.say(f"/devam {PIN}") == Reply(
        "KILL kaldırıldı: giriş sinyalleri yeniden iletilecek.", delete_request=True
    )
    assert (world.flag(StateKey.KILLED), world.flag(StateKey.PAUSED)) == (None, None)


# --- /kill and the PIN --------------------------------------------------------------------


def test_kill_without_argument_explains_itself(world: World) -> None:
    assert world.say("/kill") == Reply(
        "Kullanım: /kill <PIN> — giriş sinyallerini keser; yalnızca /devam <PIN> yeniden açar. "
        "PIN içeren mesajın silinir. PIN'siz acil durdurma: /duraklat"
    )
    assert world.state() == dict.fromkeys((key.value for key in StateKey), None)


def test_kill_with_the_correct_pin_sets_the_flag(world: World) -> None:
    assert world.say(f"/kill {PIN}") == Reply(KILL_REPLY, delete_request=True)
    assert world.flag(StateKey.KILLED) == "1"
    assert world.flag(StateKey.PAUSED) is None
    assert world.say(f"/kill {PIN}") == Reply(
        KILL_REPLY.replace("KILL etkin:", "KILL zaten etkindi:"), delete_request=True
    )


def test_wrong_pin_is_counted_and_changes_nothing_else(world: World) -> None:
    assert world.say(f"/kill {WRONG_PIN}") == Reply(wrong_pin_reply(4), delete_request=True)
    assert world.flag(StateKey.KILLED) is None
    assert world.flag(StateKey.PIN_FAIL_COUNT) == "1"
    assert world.flag(StateKey.PIN_WINDOW_START_MS) == str(NOW_MS)


@pytest.mark.parametrize(
    "argument",
    ["12345", "1234567890123", "24681x", "abc", f"{PIN} x", f"x {PIN}", "２４６８１３", f"{PIN}."],
)
def test_malformed_argument_is_a_counted_failure_without_a_comparison(
    world: World, argument: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    compared: list[object] = []

    def always_equal(*args: object) -> bool:
        compared.append(args)
        return True

    monkeypatch.setattr(hmac, "compare_digest", always_equal)
    reply = world.say(f"/kill {argument}")
    assert reply == Reply(wrong_pin_reply(4), delete_request=True)
    assert compared == []
    assert world.flag(StateKey.KILLED) is None
    assert world.flag(StateKey.PIN_FAIL_COUNT) == "1"


def test_the_pin_is_compared_in_constant_time(
    world: World, monkeypatch: pytest.MonkeyPatch
) -> None:
    compared: list[tuple[bytes, bytes]] = []
    real = hmac.compare_digest

    def spy(left: bytes, right: bytes) -> bool:
        compared.append((left, right))
        return real(left, right)

    monkeypatch.setattr(hmac, "compare_digest", spy)
    world.say(f"/kill {WRONG_PIN}")
    world.say(f"/kill {PIN}")
    assert compared == [(WRONG_PIN.encode(), PIN.encode()), (PIN.encode(), PIN.encode())]


def test_five_failures_lock_pin_entry_even_for_the_correct_pin(world: World) -> None:
    for remaining in (4, 3, 2, 1):
        assert world.text(f"/kill {WRONG_PIN}") == wrong_pin_reply(remaining)
        world.clock.now_ms += MINUTE_MS
    assert PIN_MAX_FAILURES == 5
    assert world.say(f"/kill {WRONG_PIN}") == Reply(
        "PIN hatalı. PIN girişi kilitli (çok fazla hatalı deneme); 15 dk sonra yeniden dene. "
        "PIN'siz acil durdurma: /duraklat",
        delete_request=True,
    )
    assert world.flag(StateKey.PIN_LOCKED_UNTIL_MS) == str(world.clock.now_ms + PIN_LOCK_MS)

    world.clock.now_ms += 5 * MINUTE_MS
    assert world.say(f"/kill {PIN}") == Reply(
        "PIN girişi kilitli (çok fazla hatalı deneme); 10 dk sonra yeniden dene. "
        "PIN'siz acil durdurma: /duraklat",
        delete_request=True,
    )
    assert world.flag(StateKey.KILLED) is None
    # The lock does not stop the PIN-free emergency brake.
    assert world.text("/duraklat") == PAUSE_REPLY


def test_the_lock_also_covers_devam_and_survives_a_restart(world: World) -> None:
    world.say(f"/kill {PIN}")
    for _ in range(PIN_MAX_FAILURES):
        world.say(f"/devam {WRONG_PIN}")
    restarted = Commands(world.deps)  # a new process reads the same store
    assert restarted.handle(f"/devam {PIN}", sent_at_ms=NOW_MS) == Reply(
        "PIN girişi kilitli (çok fazla hatalı deneme); 15 dk sonra yeniden dene. "
        "KILL etkin kalıyor.",
        delete_request=True,
    )
    assert world.flag(StateKey.KILLED) == "1"


def test_the_lock_expires_after_fifteen_minutes(world: World) -> None:
    for _ in range(PIN_MAX_FAILURES):
        world.say(f"/kill {WRONG_PIN}")
    world.clock.now_ms += PIN_LOCK_MS - 1
    assert world.text(f"/kill {PIN}").startswith("PIN girişi kilitli")
    world.clock.now_ms += 1
    assert world.text(f"/kill {PIN}") == KILL_REPLY
    assert world.flag(StateKey.PIN_LOCKED_UNTIL_MS) is None
    assert world.flag(StateKey.PIN_FAIL_COUNT) is None


def test_a_correct_pin_resets_the_failure_counter(world: World) -> None:
    for _ in range(PIN_MAX_FAILURES - 1):
        world.say(f"/kill {WRONG_PIN}")
    assert world.text(f"/kill {PIN}") == KILL_REPLY
    assert world.flag(StateKey.PIN_FAIL_COUNT) is None
    assert world.flag(StateKey.PIN_WINDOW_START_MS) is None
    for remaining in (4, 3, 2, 1):
        assert world.text(f"/kill {WRONG_PIN}") == wrong_pin_reply(remaining)
    assert world.flag(StateKey.PIN_LOCKED_UNTIL_MS) is None


def test_failures_older_than_the_window_do_not_count(world: World) -> None:
    for _ in range(PIN_MAX_FAILURES - 1):
        world.say(f"/kill {WRONG_PIN}")
    world.clock.now_ms += PIN_FAILURE_WINDOW_MS
    assert world.text(f"/kill {WRONG_PIN}").startswith("PIN hatalı. PIN girişi kilitli")

    fresh = Commands(world.deps)
    world.clock.now_ms += PIN_LOCK_MS
    for _ in range(PIN_MAX_FAILURES - 1):
        fresh.handle(f"/kill {WRONG_PIN}", sent_at_ms=world.clock.now_ms)
    world.clock.now_ms += PIN_FAILURE_WINDOW_MS + 1
    assert world.text(f"/kill {WRONG_PIN}") == wrong_pin_reply(4)
    assert world.flag(StateKey.PIN_WINDOW_START_MS) == str(world.clock.now_ms)


def test_an_unreadable_lock_fails_closed(world: World) -> None:
    with world.store.transaction() as tx:
        tx.set_state(StateKey.PIN_LOCKED_UNTIL_MS, "soon", NOW_MS)
    assert world.text(f"/kill {PIN}").startswith(
        "PIN girişi kilitli (çok fazla hatalı deneme); 15 dk sonra yeniden dene."
    )
    assert world.flag(StateKey.KILLED) is None
    assert world.flag(StateKey.PIN_LOCKED_UNTIL_MS) == str(NOW_MS + PIN_LOCK_MS)


def test_a_clock_jump_cannot_lock_pin_entry_for_longer_than_one_period(world: World) -> None:
    with world.store.transaction() as tx:
        tx.set_state(StateKey.PIN_LOCKED_UNTIL_MS, str(NOW_MS + 365 * 24 * 60 * MINUTE_MS), NOW_MS)
    assert "15 dk sonra yeniden dene" in world.text(f"/kill {PIN}")


def test_unreadable_failure_counters_start_a_new_window(world: World) -> None:
    with world.store.transaction() as tx:
        tx.set_state(StateKey.PIN_FAIL_COUNT, "many", NOW_MS)
        tx.set_state(StateKey.PIN_WINDOW_START_MS, str(NOW_MS), NOW_MS)
    assert world.text(f"/kill {WRONG_PIN}") == wrong_pin_reply(4)
    assert world.flag(StateKey.PIN_FAIL_COUNT) == "1"


def test_a_stale_kill_is_still_applied_and_still_deleted(world: World) -> None:
    reply = world.say(f"/kill {PIN}", age_ms=8 * MINUTE_MS + 20_000)
    assert reply == Reply(
        KILL_REPLY + "\n(Komut 8 dk 20 sn gecikmeyle işlendi.)", delete_request=True
    )
    assert world.flag(StateKey.KILLED) == "1"


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        (f"/kill {PIN}", True),
        (f"/kill {WRONG_PIN}", True),
        ("/kill x", True),
        ("/KILL x", True),
        ("/kill@propbot x", True),
        ("  /kill   x  ", True),
        (f"/kill{PIN[:5]}9", True),  # glued-on wrong PIN
        ("/kill:1", True),
        ("/killx", True),
        ("/devam x", True),
        ("/devam 1 2 3", True),
        ("/Devam@bot x", True),
        ("/kill", False),
        ("/kill@propbot", False),
        ("/devam", False),
        ("  /devam  ", False),
        ("/duraklat x", False),
        ("/atladim 12", False),
        ("selam", False),
        ("", False),
        (PIN, True),
        (f"pinim {PIN} idi", True),
        (f"/durum {PIN}", True),
        (f"/atladim {PIN}", True),
        (f"x{PIN}y", True),
        (f"/mod\n{PIN}", True),
        # Decided by shape, never by a match with the real PIN (that would be an oracle):
        # any run of six or more digits is treated as a possible PIN.
        (WRONG_PIN, True),
        ("id 1234567 nedir", True),
        ("12345", False),
        ("12 345 678", False),
    ],
)
def test_delete_request_is_decided_from_the_raw_text(
    world: World, text: str, expected: bool
) -> None:
    assert world.say(text).delete_request is expected
    # Staleness does not change the decision.
    assert world.say(text, age_ms=10 * MINUTE_MS).delete_request is expected


def test_a_bare_pin_message_is_deleted_and_changes_nothing(world: World) -> None:
    assert world.say(PIN) == Reply(
        "Mesajındaki uzun sayı PIN olabileceği için mesajın siliniyor. PIN yalnızca /kill <PIN> "
        "ya da /devam <PIN> içinde kullanılır.",
        delete_request=True,
    )
    assert world.state() == dict.fromkeys((key.value for key in StateKey), None)


def test_refusals_keep_the_delete_request(world: World) -> None:
    world.say(f"/kill {PIN}")
    stale = world.say(f"/devam {PIN}", age_ms=STALE_COMMAND_MS + 1)
    assert stale.delete_request is True
    assert stale.text.startswith("/devam: komut eski, yok sayıldı")
    for _ in range(PIN_MAX_FAILURES):
        world.say(f"/devam {WRONG_PIN}")
    locked = world.say(f"/devam {PIN}")
    assert (locked.delete_request, locked.text.startswith("PIN girişi kilitli")) == (True, True)
    world.store.close()
    failed = world.say(f"/kill {PIN}")
    assert (failed.delete_request, failed.text.startswith("[HATA]")) == (True, True)


def test_the_pin_never_appears_in_a_reply_or_a_log_line(world: World) -> None:
    world.entry()
    messages = [
        PIN,
        f"/kill {PIN}",
        f"/kill {PIN} {PIN}",
        f"/kill {WRONG_PIN}",
        f"/devam {PIN}",
        f"/devam {PIN}",
        f"/atladim {PIN}",
        f"/aldim {PIN}",
        f"/mod {PIN}",
        f"/durum {PIN}",
        f"/risk {PIN}",
        f"/saglik {PIN}",
        f"/pnl {PIN}",
        f"/duraklat {PIN}",
        f"/yardim {PIN}",
        f"/{PIN}",
        f"/kill{PIN}",
        f"selam {PIN}",
    ]
    with capture_logs() as logs:
        replies = [world.say(message) for message in messages]
        replies += [world.say(message, age_ms=5 * MINUTE_MS) for message in messages]
        world.store.close()
        replies += [world.say(message) for message in messages]
    assert logs  # commands are logged, by name only
    assert PIN not in repr(logs)
    assert WRONG_PIN not in repr(logs)
    for reply in replies:
        assert PIN not in reply.text
        assert reply.delete_request is True


def test_what_the_bot_shows_never_depends_on_a_match_with_the_pin(make: type[World]) -> None:
    # Otherwise digit strings could be tested against the PIN through the bot's visible
    # behaviour (delete or not, which reply) without touching the failure counter.
    for template in ("{}", "x{}y", "/atladim {}", "/aldim {}", "/durum {}", "/mod {}", "/risk\n{}"):
        with_pin = make().say(template.format(PIN))
        with_guess = make().say(template.format(WRONG_PIN))

        assert with_guess.delete_request is with_pin.delete_request is True
        assert with_guess.text == with_pin.text
        assert PIN not in with_pin.text
        assert WRONG_PIN not in with_guess.text


@settings(max_examples=150, deadline=None)
@given(
    text=st.one_of(
        st.text(alphabet=st.sampled_from("0123456789 /abdeiklmnrstuv@\n"), max_size=40),
        st.from_regex(r"/(durum|risk|pnl|mod|saglik|atladim|aldim|duraklat|yardim)( [0-9]{1,12})?"),
        st.from_regex(r"[a-z ]{0,6}[0-9]{6,12}[a-z ]{0,6}"),
    )
)
def test_replies_are_the_same_for_any_two_pins_outside_the_two_pin_commands(text: str) -> None:
    first_word = text.split()[0].lower() if text.split() else ""
    # /kill X and /devam X are the only places where a PIN is checked, and there every
    # wrong one is counted towards the lock.
    assume(not first_word.startswith(("/kill", "/devam")))
    replies = []
    for pin in (PIN, "975310642"):
        world = World()
        try:
            commands = Commands(dataclasses.replace(world.deps, pin=pin))
            replies.append(commands.handle(text, sent_at_ms=NOW_MS))
        finally:
            world.store.close()

    assert replies[0] == replies[1]


def test_an_id_that_equals_the_pin_is_not_echoed(world: World) -> None:
    assert world.say(f"/atladim {PIN}") == Reply(
        "id *** için bütçe kaydı yok (iletilmiş bir giriş sinyali değil).", delete_request=True
    )


def test_a_failing_store_becomes_a_reply_and_an_error_log(world: World) -> None:
    world.store.close()
    with capture_logs() as logs:
        reply = world.say("/duraklat")
    assert reply == Reply(
        "[HATA] Komut işlenemedi (StoreError); hiçbir şey değiştirilmedi. /saglik ile kontrol "
        "et; sürerse log'a bak."
    )
    assert {
        "event": "telegram_command_failed",
        "command": "duraklat",
        "error_type": "StoreError",
        "log_level": "error",
    } in logs


def test_commands_are_logged_by_name_only(world: World) -> None:
    with capture_logs() as logs:
        world.say("/atladim 12 gizli-metin", age_ms=2 * MINUTE_MS)
        world.say("/bilinmeyen gizli-metin")
    assert logs == [
        {
            "event": "telegram_command",
            "command": "atladim",
            "age_ms": 2 * MINUTE_MS,
            "refused": True,
            "log_level": "info",
        },
        {"event": "telegram_command", "command": None, "log_level": "info"},
    ]


# --- age rule -----------------------------------------------------------------------------


@pytest.mark.parametrize("message", ["/devam", "/atladim", "/atladim 1", "/aldim 1"])
def test_commands_that_give_risk_back_are_refused_when_old(world: World, message: str) -> None:
    signal_id = world.entry()
    world.release(signal_id, ReleaseReason.SKIPPED) if message.startswith("/aldim") else None
    world.say("/duraklat")
    before = (world.state(), world.counted(signal_id))
    name = message.split()[0]

    reply = world.say(message, age_ms=STALE_COMMAND_MS + 1_000)
    assert reply == Reply(
        f"{name}: komut eski, yok sayıldı (gönderileli 1 dk 1 sn olmuş). "
        "Hâlâ geçerliyse yeniden gönder."
    )
    assert (world.state(), world.counted(signal_id)) == before

    # Exactly at the limit the command is still applied.
    world.say(message, age_ms=STALE_COMMAND_MS)
    assert (world.state(), world.counted(signal_id)) != before


@pytest.mark.parametrize("message", ["/devam", "/atladim", "/aldim 1"])
def test_commands_that_give_risk_back_are_refused_when_the_clock_is_in_doubt(
    world: World, message: str
) -> None:
    world.entry()
    world.say("/duraklat")
    before = world.state()
    name = message.split()[0]
    # Telegram stamped the message two minutes AFTER the server's "now": the server is behind.
    assert world.say(message, age_ms=-2 * MINUTE_MS) == Reply(
        f"{name}: komut yok sayıldı — mesajın saati sunucu saatinden 2 dk ileride, sunucu saati "
        "kaymış olabilir. /saglik ile kontrol et."
    )
    assert world.state() == before
    assert world.used() == 250.0


def test_a_stale_devam_with_pin_does_not_touch_the_pin_counter(world: World) -> None:
    world.say(f"/kill {PIN}")
    for _ in range(PIN_MAX_FAILURES + 1):
        world.say(f"/devam {WRONG_PIN}", age_ms=5 * MINUTE_MS)
    assert world.flag(StateKey.PIN_FAIL_COUNT) is None
    assert world.flag(StateKey.PIN_LOCKED_UNTIL_MS) is None


@pytest.mark.parametrize(
    "message", ["/durum", "/pnl", "/risk", "/saglik", "/mod", "/mod auto", "/yardim", "/start"]
)
def test_read_only_commands_are_answered_whatever_their_age(world: World, message: str) -> None:
    fresh = world.text(message)
    old = world.text(message, age_ms=60 * MINUTE_MS)
    assert "komut eski" not in old
    assert old.splitlines()[0] == fresh.splitlines()[0]


# --- /atladim -----------------------------------------------------------------------------


def test_atladim_releases_the_latest_delivered_entry_of_today(world: World) -> None:
    world.entry(delivered=True)
    latest = world.entry(delivered=True, offset=13, strategy_id="asian_sweep")
    assert world.say("/atladim") == Reply(
        "id 2 · Asian Sweep · LONG · bar 11:43 NY: $250 serbest bırakıldı; bu girişin stop/çıkış "
        "mesajları artık gelmeyecek. Yanlışsa: /aldim 2\n"
        "Günlük bütçe şimdi $250/$1000."
    )
    assert world.counted(latest) == (False, ReleaseReason.SKIPPED)
    assert world.counted(1) == (True, None)
    assert world.used() == 250.0


def test_atladim_never_picks_an_entry_whose_message_is_still_pending(world: World) -> None:
    delivered = world.entry(delivered=True)
    pending = world.entry(delivered=False, offset=1)
    assert world.text("/atladim").startswith("id 1 · NY ORB FVG · LONG · bar 11:30 NY: $250")
    assert world.counted(delivered) == (False, ReleaseReason.SKIPPED)
    assert world.counted(pending) == (True, None)


def test_atladim_ignores_messages_that_are_not_the_delivered_signal(world: World) -> None:
    older = world.entry(delivered=True)
    newer = world.entry(delivered=False, offset=1)
    with world.store.transaction() as tx:
        # A sent notice about the newer entry, and its signal message expired unsent.
        notice = tx.enqueue_message(
            kind=OutboxKind.NOTICE, text="not", now_ms=NOW_MS, signal_id=newer
        )
        tx.mark_message_sent(notice, NOW_MS)
        (signal_message,) = (
            row for row in tx.messages_for_signal(newer) if row.kind is OutboxKind.SIGNAL
        )
        tx.mark_message_expired(signal_message.id, NOW_MS)
    world.say("/atladim")
    assert world.counted(older) == (False, ReleaseReason.SKIPPED)
    assert world.counted(newer) == (True, None)


def test_atladim_without_a_delivered_entry_changes_nothing(world: World) -> None:
    pending = world.entry(delivered=False)
    assert world.say("/atladim") == Reply(
        "Bugün (prop günü 2026-07-21) sana ulaşmış bir giriş sinyali yok. "
        "Belirli bir giriş için: /atladim <id>"
    )
    assert world.counted(pending) == (True, None)


@pytest.mark.parametrize(
    ("reason", "label"),
    [(ReleaseReason.SKIPPED, "atlandı"), (ReleaseReason.UNDELIVERED, "iletilemedi")],
)
def test_atladim_on_a_released_entry_says_so_and_does_not_fall_through(
    world: World, reason: ReleaseReason, label: str
) -> None:
    older = world.entry(delivered=True)
    latest = world.entry(delivered=True, offset=1)
    world.release(latest, reason)
    for message in ("/atladim", f"/atladim {latest}"):
        assert world.say(message) == Reply(
            f"id 2 · NY ORB FVG · LONG · bar 11:31 NY: zaten serbest ({label}); değişiklik yok. "
            "Geri almak için: /aldim 2"
        )
    assert world.counted(latest) == (False, reason)
    assert world.counted(older) == (True, None)


def test_atladim_with_id_takes_any_entry_that_has_a_ledger_row(world: World) -> None:
    world.entry(delivered=True)
    pending = world.entry(delivered=False, offset=1, side="short")
    assert world.say(f"/atladim {pending}") == Reply(
        "id 2 · NY ORB FVG · SHORT · bar 11:31 NY: $250 serbest bırakıldı; bu girişin stop/çıkış "
        "mesajları artık gelmeyecek. Yanlışsa: /aldim 2\n"
        "Günlük bütçe şimdi $250/$1000."
    )
    assert world.counted(pending) == (False, ReleaseReason.SKIPPED)


def test_atladim_after_the_prop_day_rolled_over(make: type[World]) -> None:
    # 21:59:59 UTC is 23:59:59 in Prague (prop day 07-21); two seconds later it is 07-22.
    before_reset = dt.datetime(2026, 7, 21, 21, 59, 59, tzinfo=dt.UTC)
    world = make(now_ms=int(before_reset.timestamp() * 1000))
    yesterday = world.entry(delivered=True)
    world.clock.now_ms += 2_000

    assert world.say("/atladim") == Reply(
        "Bugün (prop günü 2026-07-22) sana ulaşmış bir giriş sinyali yok. "
        "Belirli bir giriş için: /atladim <id>"
    )
    assert world.counted(yesterday) == (True, None)

    assert world.say(f"/atladim {yesterday}") == Reply(
        "id 1 · NY ORB FVG · LONG · bar 11:30 NY: $250 serbest bırakıldı; bu girişin stop/çıkış "
        "mesajları artık gelmeyecek. Yanlışsa: /aldim 1\n"
        "(Prop günü 2026-07-21: bugünün bütçesini etkilemez.)"
    )
    assert world.counted(yesterday) == (False, ReleaseReason.SKIPPED)


def test_atladim_one_second_before_the_rollover_still_sees_the_entry(make: type[World]) -> None:
    before_reset = dt.datetime(2026, 7, 21, 21, 59, 59, tzinfo=dt.UTC)
    world = make(now_ms=int(before_reset.timestamp() * 1000))
    world.entry(delivered=True)
    assert world.text("/atladim").endswith("Günlük bütçe şimdi $0/$1000.")


@pytest.mark.parametrize("message", ["/atladim 99", "/aldim 99"])
def test_an_id_without_a_ledger_row_is_refused(world: World, message: str) -> None:
    tg.add_signal(world.store, outcome=Outcome.STALE)  # id 1: a signal, but never forwarded
    assert world.say(message) == Reply(
        "id 99 için bütçe kaydı yok (iletilmiş bir giriş sinyali değil)."
    )
    assert world.text(message.replace("99", "1")) == (
        "id 1 için bütçe kaydı yok (iletilmiş bir giriş sinyali değil)."
    )


@pytest.mark.parametrize("message", ["/atladim x", "/atladim 1 2", "/atladim -1", "/atladim 1.0"])
def test_atladim_with_a_malformed_id_explains_the_usage(world: World, message: str) -> None:
    signal_id = world.entry()
    assert world.say(message) == Reply(
        "Kullanım: /atladim [id] — id, sinyal mesajının son satırındaki numaradır; id yazmazsan "
        "bugün sana ulaşan son giriş alınır."
    )
    assert world.counted(signal_id) == (True, None)


def test_an_entry_without_its_signal_row_is_still_named_by_id() -> None:
    # Cannot happen while the foreign key holds; the label must not fail if it ever does.
    assert commands_module._entry_label(7, None) == "id 7"


# --- /aldim -------------------------------------------------------------------------------


@pytest.mark.parametrize("reason", [ReleaseReason.SKIPPED, ReleaseReason.UNDELIVERED])
def test_aldim_counts_a_released_entry_again(world: World, reason: ReleaseReason) -> None:
    signal_id = world.entry(delivered=reason is ReleaseReason.SKIPPED)
    world.entry(delivered=True, offset=1)
    world.release(signal_id, reason)
    assert world.used() == 250.0
    assert world.say(f"/aldim {signal_id}") == Reply(
        "id 1 · NY ORB FVG · LONG · bar 11:30 NY: $250 yeniden bütçede sayılıyor; bu girişin "
        "stop/çıkış mesajları yeniden iletilecek.\n"
        "Günlük bütçe şimdi $500/$1000."
    )
    assert world.counted(signal_id) == (True, None)
    assert world.used() == 500.0


def test_aldim_on_a_counted_entry_changes_nothing(world: World) -> None:
    signal_id = world.entry()
    assert world.say(f"/aldim {signal_id}") == Reply(
        "id 1 · NY ORB FVG · LONG · bar 11:30 NY: zaten bütçede sayılıyor; değişiklik yok."
    )
    assert world.counted(signal_id) == (True, None)


def test_aldim_undoes_atladim(world: World) -> None:
    signal_id = world.entry()
    world.say("/atladim")
    assert world.used() == 0.0
    world.say(f"/aldim {signal_id}")
    assert world.used() == 250.0


def test_aldim_works_for_another_prop_day(world: World) -> None:
    signal_id = world.entry(prop_day=dt.date(2026, 7, 20))
    world.release(signal_id, ReleaseReason.SKIPPED)
    assert world.text(f"/aldim {signal_id}").splitlines()[1] == (
        "(Prop günü 2026-07-20: bugünün bütçesini etkilemez.)"
    )
    assert world.counted(signal_id) == (True, None)
    assert world.used(dt.date(2026, 7, 20)) == 250.0


def test_aldim_warns_when_the_budget_is_exceeded(world: World) -> None:
    released = world.entry(risk_amount=300.0)
    world.release(released, ReleaseReason.SKIPPED)
    for offset in (1, 2, 3):
        world.entry(offset=offset, risk_amount=300.0)
    assert world.text(f"/aldim {released}").splitlines()[1] == (
        "Günlük bütçe şimdi $1200/$1000. DİKKAT: günlük bütçe aşıldı; yeni giriş sinyali iletilmez."
    )


@pytest.mark.parametrize("message", ["/aldim", "/aldim x", "/aldim 1 2"])
def test_aldim_needs_exactly_one_id(world: World, message: str) -> None:
    signal_id = world.entry()
    world.release(signal_id, ReleaseReason.SKIPPED)
    assert world.say(message) == Reply(
        "Kullanım: /aldim <id> — id, /atladim ile serbest bırakılan girişin numarasıdır."
    )
    assert world.counted(signal_id) == (False, ReleaseReason.SKIPPED)


# --- unauthorized counter -----------------------------------------------------------------


def test_note_unauthorized_only_counts(world: World) -> None:
    before = world.state()
    for _ in range(3):
        world.commands.note_unauthorized()
    assert "Yetkisiz sohbetlerden mesaj: 3" in world.text("/saglik").splitlines()
    assert world.state() == before
