"""Telegram service: chat whitelist, command adapter, outbox sender, start-up and fatal paths.

Everything runs offline on `support.tg.FakeRequest`; the real python-telegram-bot code sits
between the service and the fake. Most tests drive the sender with `send_due()` and feed
updates with `application.process_update`; a few start `run()` to cover the loop itself.
"""

import asyncio
import datetime as dt
import inspect
import io
import traceback
from collections.abc import Awaitable, Callable
from typing import Any, cast

import httpx
import pytest
import structlog
from structlog.testing import capture_logs
from telegram import Update
from telegram.error import (
    BadRequest,
    Conflict,
    Forbidden,
    InvalidToken,
    NetworkError,
    RetryAfter,
    TelegramError,
    TimedOut,
)
from telegram.ext import Application, ExtBot
from telegram.warnings import PTBDeprecationWarning

from propbot.core.clock import boot_clock_ms
from propbot.logging_setup import configure_logging, reset_logging
from propbot.notify import telegram as telegram_module
from propbot.notify.commands import CommandDeps, Commands, Reply
from propbot.notify.telegram import (
    ResilientBot,
    TelegramFatalError,
    TelegramService,
    code_entity_spans,
    fit_text,
)
from propbot.risk.loader import load_risk_config, load_venue_profile
from propbot.signals.tvconfig import load_tv_alerts_config
from propbot.store.db import (
    OutboxKind,
    OutboxRow,
    OutboxStatus,
    ReleaseReason,
    StateKey,
    Store,
    StoreError,
)
from support import tg
from support.alerts import BAR_TIME_MS
from support.paths import CONFIG_FIXTURES, FIXTURES
from support.tg import BOT_TOKEN, CHAT_ID, FakeRequest

PIN = "246813"
MINUTE_MS = 60_000
VALIDITY_SECONDS = 60
# 2026-07-21 15:31:01.5 UTC, 1.5 s after the reference bar closed (prop day 2026-07-21).
NOW_MS = BAR_TIME_MS + MINUTE_MS + 1_500
TODAY = dt.date(2026, 7, 21)
# Deadline of an entry on the reference bar: bar close + validity.
ENTRY_DEADLINE_MS = BAR_TIME_MS + 2 * MINUTE_MS

RISK = load_risk_config(CONFIG_FIXTURES / "valid" / "risk.yaml")
VENUE = load_venue_profile(CONFIG_FIXTURES / "valid" / "venues" / "sample_2step.yaml").model_copy(
    update={"rules_verified_at": dt.date(2026, 7, 9)}
)
TV = load_tv_alerts_config(FIXTURES / "tv_alerts" / "valid" / "full.yaml")

CONFLICT_ALARM = (
    "[ALARM] Aynı bot token'ıyla başka bir süreç güncelleme çekiyor. İki kopya çalışıyorsa "
    "birini durdur; değilse token sızmış olabilir: BotFather → /revoke, .env'i güncelle, "
    "PIN'i değiştir.\n"
    "Bu sırada gönderdiğin bir komut öbür sürece gitmiş olabilir: /durum ile kontrol et, "
    "gerekirse komutu yeniden gönder."
)
# What a copy sends when it goes on standby for one that was already polling (user decision 53).
STANDBY_ALARM = (
    "[ALARM] Bu PropBot kopyası (vps-1) BEKLEMEDE: aynı bot token'ıyla başka bir süreç "
    "güncelleme çekiyor; bu kopya ona yol verdi. Artık komut almıyor ve GİRİŞ sinyali "
    "iletmiyor; stop/çıkış/iptal mesajlarını iletmeye devam ediyor.\n"
    "Hangisi çalışmalıysa öbürünü durdur, sonra bu kopyayı yeniden başlat (sudo systemctl "
    "restart propbot): yaklaşık bir dakika sonra ETKİN bildirimi gelir. Deneme kopyası "
    "için BotFather'dan ayrı bir bot aç. Başka kopyan yoksa token sızmış olabilir: "
    "BotFather → /revoke, .env'i güncelle, PIN'i değiştir.\n"
    "Az önce gönderdiğin bir komut yanıtsız kaldıysa ya da yanıtında BEKLEMEDE yazıyorsa "
    "yeniden gönder."
)
STANDBY_AGAIN = (
    "[ALARM] Bu PropBot kopyası (vps-1) yeniden başlatıldı, ama aynı bot token'ıyla başka bir "
    "süreç hâlâ güncelleme çekiyor: yine BEKLEMEDE. Komut almıyor ve GİRİŞ sinyali iletmiyor; "
    "stop/çıkış/iptal mesajlarını iletmeye devam ediyor.\n"
    "Önce öbür süreci durdur, sonra bu kopyayı yeniden başlat. Bu uyarı 5 dakikada en çok bir "
    "kez gelir: ondan sonraki bir denemede ETKİN bildirimi gelmediyse kopya yine beklemededir."
)
ACTIVE_AGAIN = (
    "[BİLGİ] Bu PropBot kopyası (vps-1) yeniden ETKİN: bir dakikadır başka dinleyici yok. "
    "Giriş sinyalleri yeniden iletiliyor ve komutlar bu kopyaya geliyor.\n"
    "Duraklatma ve KILL durumu kopyalar arasında aktarılmaz: /durum ile kontrol et."
)
STANDBY_REFUSAL = (
    "Bu kopya BEKLEMEDE (aynı bot token'ıyla başka bir süreç komutları alıyor): komutun "
    "UYGULANMADI. Yeniden gönder; öbür süreç yanıtlar."
)
STANDBY_NOTE = (
    "\n[BEKLEMEDE] Bu yanıt bekleme durumundaki kopyadan geldi: komut aynı bot token'ıyla "
    "çalışan öbür sürece ULAŞMADI. Yeniden gönder."
)
CONFLICT_409 = tg.error_response(409, "Conflict: terminated by other getUpdates request")
DELETE_FAILED = "PIN içeren mesajı silemedim — elle sil ve PROPBOT_PIN'i değiştir."
FATAL_TOKEN = (
    "Telegram bot token'ı reddedildi. TELEGRAM_BOT_TOKEN değerini BotFather'daki güncel "
    "token ile karşılaştır; servis durduruluyor."
)
FATAL_POLLING = (
    "Telegram güncelleme döngüsü beklenmedik biçimde durdu; servis durduruluyor "
    "(yeniden başlatılmalı)."
)


def uncertain_notice(signal_id: int, why: str = "geçerliliği doldu") -> str:
    return (
        f"[İLETİM BELİRSİZ] id {signal_id}: giriş sinyali sana ulaşmış da olabilir, ulaşmamış "
        f"da ({why}). Gördüysen ve girdiysen bir şey yapma; görmediysen ya da girmediysen: "
        f"/atladim {signal_id}"
    )


class FakeClock:
    def __init__(self, now_ms: int = NOW_MS) -> None:
        self.now_ms = now_ms

    def __call__(self) -> int:
        return self.now_ms


class Harness:
    """A service on an in-memory store and the offline transport. Build it inside the loop."""

    def __init__(
        self,
        *,
        polling: bool = False,
        commands: Commands | None = None,
        store: Store | None = None,
        arrival_hold_ms: int = 0,
        contest_hold_ms: int = 0,
    ) -> None:
        # A store handed in belongs to the test (it outlives this harness, as a database
        # file outlives a process).
        self._owns_store = store is None
        self.store = Store(":memory:") if store is None else store
        self.clock = FakeClock()
        self.fake = FakeRequest()
        self.commands = commands or Commands(
            CommandDeps(
                store=self.store,
                risk=RISK,
                venue=VENUE,
                venue_name="sample_2step",
                tv=TV,
                pin=PIN,
                clock=self.clock,
                started_ms=NOW_MS - 5 * MINUTE_MS,
                reject_counts=dict,
            )
        )
        self.service = TelegramService(
            bot_token=BOT_TOKEN,
            allowed_chat_id=CHAT_ID,
            commands=self.commands,
            store=self.store,
            clock=self.clock,
            validity_seconds=VALIDITY_SECONDS,
            request=self.fake,
            get_updates_request=self.fake,
            polling=polling,
            instance="vps-1",
            # One fake clock for timestamps and for durations; commands are not held back
            # after an arrival unless a test asks for it (the fake clock does not move).
            uptime=self.clock,
            arrival_hold_ms=arrival_hold_ms,
            contest_hold_ms=contest_hold_ms,
        )

    @property
    def app(self) -> Application[Any, Any, Any, Any, Any, Any]:
        return self.service.application

    async def close(self) -> None:
        if self.app.running:
            await self.app.stop()
        await self.app.shutdown()
        await self.app.bot.shutdown()
        if self._owns_store:
            self.store.close()

    # -- driving -----------------------------------------------------------------------------

    async def first_step(self) -> None:
        """Run the sender's first step on an empty outbox (it flags older rows as uncertain)."""
        assert await self.service.send_due() is False

    async def feed(self, update: dict[str, Any]) -> None:
        await self.app.initialize()
        await self.app.process_update(Update.de_json(update, self.app.bot))

    async def say(self, text: str, *, age_ms: int = 0) -> None:
        await self.feed(tg.private_update(text, sent_at_ms=self.clock.now_ms - age_ms))

    async def drain(self, limit: int = 20) -> int:
        """Call `send_due` until it reports nothing to do; return the number of busy steps."""
        steps = 0
        while await self.service.send_due():
            steps += 1
            assert steps < limit
        return steps

    # -- arranging and reading the store -----------------------------------------------------

    def enqueue(self, text: str, *, kind: OutboxKind = OutboxKind.NOTICE, **kwargs: Any) -> int:
        with self.store.transaction() as tx:
            return tx.enqueue_message(kind=kind, text=text, now_ms=self.clock.now_ms, **kwargs)

    def entry(self, *, offset: int = 0, **overrides: Any) -> tuple[int, int]:
        """A forwarded entry on the bar `offset` minutes after the reference bar (pending)."""
        fields: dict[str, Any] = {
            "bar_time_ms": BAR_TIME_MS + offset * MINUTE_MS,
            "bar_close_ms": BAR_TIME_MS + (offset + 1) * MINUTE_MS,
            "prop_day": TODAY,
        }
        fields.update(overrides)
        return tg.forwarded_entry(self.store, now_ms=self.clock.now_ms, **fields)

    def row(self, outbox_id: int) -> OutboxRow:
        with self.store.transaction() as tx:
            row = tx.get_message(outbox_id)
        assert row is not None
        return row

    def counted(self, signal_id: int) -> tuple[bool, ReleaseReason | None]:
        with self.store.transaction() as tx:
            entry = tx.get_budget_entry(signal_id)
        assert entry is not None
        return entry.counted, entry.release_reason

    def pending_texts(self) -> list[str]:
        """Texts still waiting in the outbox, oldest first."""
        with self.store.transaction() as tx:
            rows = tx.messages_created_between(0, 2**62)
        return [row.text for row in rows if row.status is OutboxStatus.PENDING]

    def outbox(self) -> list[tuple[OutboxKind, OutboxStatus, str]]:
        with self.store.transaction() as tx:
            rows = tx.messages_created_between(0, 2**62)
        return [(row.kind, row.status, row.text) for row in rows]

    def flag(self, key: StateKey) -> str | None:
        with self.store.transaction() as tx:
            return tx.get_state(key)


def drive[T](scenario: Callable[[Harness], Awaitable[T]], **kwargs: Any) -> T:
    """Run `scenario` in a fresh event loop on a fresh harness; always shut both down."""
    structlog.reset_defaults()

    async def main() -> T:
        harness = Harness(**kwargs)
        try:
            return await scenario(harness)
        finally:
            await harness.close()

    return asyncio.run(main())


async def until(predicate: Callable[[], bool], timeout: float = 5.0) -> None:
    """Wait (really) until `predicate` holds; only the tests that start `run()` need this."""
    async with asyncio.timeout(timeout):
        while not predicate():
            await asyncio.sleep(0.005)


def caused_by[E: BaseException](error: E, cause: BaseException) -> E:
    error.__cause__ = cause
    return error


# --- pure helpers -------------------------------------------------------------------------


def test_domain_like_tokens_become_code_spans() -> None:
    text = "FTMO US500.cash'te PİYASA emri"
    assert code_entity_spans(text) == [(5, 10)]
    assert text[5:15] == "US500.cash"


@pytest.mark.parametrize(
    "text",
    [
        "Stop 5110.75 (−12.50 puan) · TP 5148.25 (+25.00 puan, 2.00R)",
        "risk ≈ $250 (%0.25) · 20.00 lot · 1.0× hesap · $249.50",
        "Geçerlilik: 17:46:00 TR'ye kadar · 2026-07-21 · CME_MINI:MES1!",
        "rules_verified_at · tick_size · v1.2.3 · x.y · a.b1 · 5120.125–5126.375",
        ".env · /saglik · /durum · id 123 · bar 09:44 NY / 17:44 TR",
        "/atladimlar · /aldim_x · /aldim9 · atladim · aldim 12",
        "/devamli · /devam_x · /devam2 · devam <PIN> · sinyaller gelmeye devam eder",
        "Cümle bitti. Yenisi başladı.",
        "",
    ],
)
def test_prices_times_and_plain_words_are_not_marked(text: str) -> None:
    assert code_entity_spans(text) == []


def test_a_command_that_carries_an_id_becomes_one_code_span() -> None:
    # Telegram turns "/atladim" into a link that SENDS the bare command when tapped; without
    # its id the command would act on another entry. As code, a tap copies "/atladim 126".
    text = "bütçeyi serbest bırak: /atladim 126\nYanlışsa: /aldim 124 · /saglik · US500.cash"

    spans = code_entity_spans(text)

    assert [text[offset : offset + length] for offset, length in spans] == [
        "/atladim 126",
        "/aldim 124",
        "US500.cash",
    ]


@pytest.mark.parametrize(
    ("text", "tokens"),
    [
        ("/atladim", ["/atladim"]),
        ("/atladim [id] — girişi almadım", ["/atladim [id]"]),
        ("Kullanım: /aldim <id> — id, /atladim ile serbest bırakılan", ["/aldim <id>", "/atladim"]),
        ("/aldim <id> — /atladim'ı geri al", ["/aldim <id>", "/atladim"]),
        ("almadıysan /atladim ile serbest bırak: /atladim <id>", ["/atladim", "/atladim <id>"]),
        ("/atladim 126\n/aldim 126.", ["/atladim 126", "/aldim 126"]),
        ("/atladim  126 · /atladim <ID> · /atladim [1]", ["/atladim", "/atladim", "/atladim"]),
    ],
)
def test_the_ledger_commands_are_never_left_tappable(text: str, tokens: list[str]) -> None:
    # Telegram links a command and SENDS it bare on a tap: a tap on the help line
    # "/atladim [id]" would release the last entry's risk. A code span is copied instead.
    assert [text[start : start + length] for start, length in code_entity_spans(text)] == tokens


@pytest.mark.parametrize(
    ("text", "tokens"),
    [
        ("/devam", ["/devam"]),
        ("Açmak için: /devam", ["/devam"]),
        ("açmak için /devam)", ["/devam"]),
        ("/devam: komut eski, yok sayıldı", ["/devam"]),
        ("/devam'ı elle yaz · /devamı", ["/devam", "/devam"]),
        ("KILL (açmak için /devam <PIN>)", ["/devam <PIN>"]),
        ("/devam — yeniden başlat (KILL sonrası: /devam <PIN>)", ["/devam", "/devam <PIN>"]),
        ("/kill <PIN> — yalnızca /devam <PIN> açar", ["/devam <PIN>"]),
        ("/devam 123456", ["/devam 123456"]),
        ("/devam <pin> · /devam <Pin> · /devam  <PIN>", ["/devam <pin>", "/devam", "/devam"]),
    ],
)
def test_devam_is_never_left_tappable(text: str, tokens: list[str]) -> None:
    # User decision 51: a tap on a linked "/devam" would lift a pause that was set on
    # purpose. Every mention goes out as a code span, with its "<PIN>" placeholder if any.
    assert [text[start : start + length] for start, length in code_entity_spans(text)] == tokens


@pytest.mark.parametrize(
    ("text", "tokens"),
    [
        ("US500.cash", ["US500.cash"]),
        ("docs/runbook.md §7", ["runbook.md"]),
        (
            "a.b.c.example.com ve sub-domain.example-site.org.",
            ["a.b.c.example.com", "sub-domain.example-site.org"],
        ),
        ("(FTMO.com)", ["FTMO.com"]),
        ("US500.cash,GER40.cash", ["US500.cash", "GER40.cash"]),
        ("BotFather → /revoke, .env'i güncelle; t.me/x", ["t.me"]),
    ],
)
def test_every_domain_like_token_is_found(text: str, tokens: list[str]) -> None:
    assert [text[start : start + length] for start, length in code_entity_spans(text)] == tokens


def test_offsets_are_utf16_units_turkish_letters_one_emoji_two() -> None:
    text = "ğüş İı 😀🚀 US500.cash'te ve 𝔘 FTMO.com"
    #       "ğüş İı " = 7 units, two emoji = 4, space = 1 -> 12
    #       "US500.cash" 10, "'te ve " 7, the Fraktur letter (astral) 2, space 1 -> 32
    assert code_entity_spans(text) == [(12, 10), (32, 8)]
    encoded = text.encode("utf-16-le")
    for (offset, length), token in zip(
        code_entity_spans(text), ["US500.cash", "FTMO.com"], strict=True
    ):
        assert encoded[2 * offset : 2 * (offset + length)].decode("utf-16-le") == token
    # Counting characters instead would be off by one per astral character before the token.
    assert text.index("US500.cash") == 10
    assert text.index("FTMO.com") == 29


def test_fit_text_leaves_normal_messages_alone() -> None:
    text = "ğ" * 4096
    assert fit_text(text) is text
    assert fit_text("") == ""


def test_fit_text_cuts_an_over_long_message_and_marks_the_cut() -> None:
    cut = fit_text("a" * 4097)
    assert cut == "a" * (4096 - 21) + "\n[… mesaj kısaltıldı]"
    assert len(cut) == 4096


def test_fit_text_counts_utf16_units_and_never_splits_a_character() -> None:
    cut = fit_text("xx" + "😀" * 2100)  # 4202 units; 4075 are left beside the mark
    assert cut.endswith("😀\n[… mesaj kısaltıldı]")
    assert len(cut.encode("utf-16-le")) // 2 == 4095  # one unit short: a whole emoji fits or not
    assert cut.encode("utf-16-le").decode("utf-16-le") == cut


@pytest.mark.parametrize(
    ("failures", "expected_ms"),
    [(0, 1_000), (1, 2_000), (2, 4_000), (3, 8_000), (4, 16_000), (5, 30_000), (6, 30_000),
     (500, 30_000), (-1, 1_000)],
)  # fmt: skip
def test_backoff_doubles_up_to_thirty_seconds(failures: int, expected_ms: int) -> None:
    assert telegram_module._backoff_ms(failures) == expected_ms


@pytest.mark.parametrize(
    ("cause", "never_left"),
    [
        (httpx.ConnectError("refused"), True),
        (httpx.ConnectTimeout("slow handshake"), True),
        (httpx.PoolTimeout("pool exhausted"), True),
        (httpx.ReadTimeout("no answer"), False),
        (httpx.WriteTimeout("stalled"), False),
        (httpx.ReadError("reset"), False),
        (httpx.WriteError("reset"), False),
        (httpx.RemoteProtocolError("closed"), False),
        (httpx.ProxyError("proxy"), False),
        (ConnectionRefusedError("os level"), False),
        (None, False),
    ],
)
def test_only_connect_and_pool_errors_prove_the_request_never_left(
    cause: BaseException | None, never_left: bool
) -> None:
    error = TimedOut()
    error.__cause__ = cause
    assert telegram_module._never_left(error) is never_left


def test_never_left_walks_the_whole_chain_and_ignores_look_alikes() -> None:
    class ConnectError(Exception):  # same name, but not httpx's
        pass

    assert telegram_module._never_left(caused_by(NetworkError("x"), ConnectError())) is False
    deep = caused_by(NetworkError("x"), caused_by(RuntimeError("y"), httpx.ConnectError("z")))
    assert telegram_module._never_left(deep) is True
    looped = NetworkError("x")
    looped.__cause__ = looped
    assert telegram_module._never_left(looped) is False


def test_retry_after_seconds_reads_both_representations(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("PTB_TIMEDELTA", raising=False)
    with pytest.warns(PTBDeprecationWarning):
        error = RetryAfter(7)  # the library itself warns when it builds the error
    assert telegram_module._retry_after_seconds(error) == 7.0  # reading it must not warn again
    monkeypatch.setenv("PTB_TIMEDELTA", "true")
    assert telegram_module._retry_after_seconds(RetryAfter(dt.timedelta(seconds=2.5))) == 2.5
    assert telegram_module._retry_after_seconds(RetryAfter(dt.timedelta(seconds=-3))) == 0.0


# --- construction -------------------------------------------------------------------------


@pytest.mark.parametrize("chat_id", [0, -1, tg.GROUP_ID])
def test_only_a_private_chat_id_is_accepted(chat_id: int) -> None:
    async def scenario(h: Harness) -> None:
        with pytest.raises(ValueError, match="private chat"):
            TelegramService(
                bot_token=BOT_TOKEN,
                allowed_chat_id=chat_id,
                commands=h.commands,
                store=h.store,
                clock=h.clock,
                validity_seconds=VALIDITY_SECONDS,
                request=FakeRequest(),
                get_updates_request=FakeRequest(),
            )

    drive(scenario)


def test_validity_must_be_positive() -> None:
    async def scenario(h: Harness) -> None:
        with pytest.raises(ValueError, match="validity_seconds"):
            TelegramService(
                bot_token=BOT_TOKEN,
                allowed_chat_id=CHAT_ID,
                commands=h.commands,
                store=h.store,
                clock=h.clock,
                validity_seconds=0,
                request=FakeRequest(),
                get_updates_request=FakeRequest(),
            )

    drive(scenario)


def test_an_empty_token_is_fatal_without_a_cause() -> None:
    async def scenario(h: Harness) -> TelegramFatalError:
        with pytest.raises(TelegramFatalError) as caught:
            TelegramService(
                bot_token="",
                allowed_chat_id=CHAT_ID,
                commands=h.commands,
                store=h.store,
                clock=h.clock,
                validity_seconds=VALIDITY_SECONDS,
                request=FakeRequest(),
                get_updates_request=FakeRequest(),
            )
        return caught.value

    error = drive(scenario)
    assert str(error) == FATAL_TOKEN
    assert error.__cause__ is None
    assert error.__context__ is None


def test_the_service_exposes_the_application_and_keeps_the_token_out_of_its_repr() -> None:
    async def scenario(h: Harness) -> None:
        assert isinstance(h.service.application, Application)
        assert BOT_TOKEN not in repr(h.service)
        assert BOT_TOKEN not in repr(vars(h.service).keys())

    drive(scenario)


def test_a_service_built_before_the_event_loop_exists_works_inside_it() -> None:
    """`propbot serve` builds the service first and starts the loop afterwards."""
    structlog.reset_defaults()
    with Store(":memory:") as store:
        clock = FakeClock()
        fake = FakeRequest()
        commands = Commands(
            CommandDeps(
                store=store,
                risk=RISK,
                venue=VENUE,
                venue_name="sample_2step",
                tv=TV,
                pin=PIN,
                clock=clock,
                started_ms=NOW_MS,
                reject_counts=dict,
            )
        )
        service = TelegramService(
            bot_token=BOT_TOKEN,
            allowed_chat_id=CHAT_ID,
            commands=commands,
            store=store,
            clock=clock,
            validity_seconds=VALIDITY_SECONDS,
            request=fake,
            get_updates_request=fake,
        )
        with store.transaction() as tx:
            tx.enqueue_message(kind=OutboxKind.ALARM, text="alarm", now_ms=NOW_MS)

        async def main() -> None:
            stop = asyncio.Event()
            task = asyncio.create_task(service.run(stop))
            fake.push_update(tg.private_update("/mod", sent_at_ms=NOW_MS))
            await until(lambda: len(fake.sent_texts()) == 2)
            stop.set()
            await asyncio.wait_for(task, 5)

        asyncio.run(main())
        assert fake.sent_texts()[0] == "alarm"
        assert fake.sent_texts()[1].startswith("Mod: signal")


def test_a_run_that_is_stopped_before_it_starts_touches_nothing() -> None:
    async def scenario(h: Harness) -> None:
        stop = asyncio.Event()
        stop.set()
        await h.service.run(stop)
        assert h.fake.calls == []

    drive(scenario, polling=True)


# --- commands through the adapter ---------------------------------------------------------


def test_a_command_from_the_owner_is_answered_in_plain_text() -> None:
    async def scenario(h: Harness) -> None:
        expected = h.commands.handle("/durum", sent_at_ms=NOW_MS).text
        await h.say("/durum")
        assert [call.endpoint for call in h.fake.calls] == ["getMe", "sendMessage"]
        (params,) = h.fake.calls_to("sendMessage")
        offset = len("[DURUM] 18:31:01 TR\nMod: signal (emir gönderilmez) · Durum: aktif\n") + len(
            "Venue: sample_2step · SAMPLEFIRM "
        )
        assert params == {
            "chat_id": CHAT_ID,
            "text": expected,
            "entities": [{"type": "code", "offset": offset, "length": len("US500.sample")}],
            "link_preview_options": {"is_disabled": True},
        }
        assert "parse_mode" not in params

    drive(scenario)


@pytest.mark.parametrize(
    ("command", "marked"),
    [
        (
            "/yardim",
            ["/devam", "/devam <PIN>", "/devam <PIN>", "/atladim [id]", "/aldim <id>", "/atladim"],
        ),
        ("/aldim", ["/aldim <id>", "/atladim"]),
        ("/atladim abc", ["/atladim [id]"]),
        ("/kill", ["/devam <PIN>"]),
        ("/duraklat", ["/devam"]),
    ],
)
def test_help_and_usage_replies_leave_no_ledger_command_tappable(
    command: str, marked: list[str]
) -> None:
    # A tap on a linked "/atladim" in the help would send it bare: the last entry's risk
    # would leave the budget; a tap on "/devam" would lift a pause. Every mention goes out
    # as a code span instead.
    async def scenario(h: Harness) -> None:
        await h.say(command)
        (params,) = h.fake.calls_to("sendMessage")
        encoded = params["text"].encode("utf-16-le")
        spans = [
            encoded[2 * entity["offset"] : 2 * (entity["offset"] + entity["length"])].decode(
                "utf-16-le"
            )
            for entity in params["entities"]
        ]
        assert all(entity["type"] == "code" for entity in params["entities"])
        assert spans == marked
        mentions = sum(params["text"].count(name) for name in ("/atladim", "/aldim", "/devam"))
        assert mentions == len(marked)

    drive(scenario)


def test_pause_and_status_replies_carry_devam_as_code() -> None:
    # User decision 51: the two replies a paused user reads most are the ones where a slip
    # of the thumb on a linked /devam would silently lift the pause.
    async def scenario(h: Harness) -> None:
        await h.say("/duraklat")
        await h.say("/durum")
        replies = h.fake.calls_to("sendMessage")
        assert len(replies) == 2
        for params in replies:
            assert params["text"].count("/devam") == 1
            encoded = params["text"].encode("utf-16-le")
            marked = [
                encoded[2 * entity["offset"] : 2 * (entity["offset"] + entity["length"])].decode(
                    "utf-16-le"
                )
                for entity in params["entities"]
                if entity["type"] == "code"
            ]
            assert "/devam" in marked

    drive(scenario)


def test_a_reply_without_domain_like_tokens_carries_no_entities() -> None:
    async def scenario(h: Harness) -> None:
        await h.say("/mod")
        (params,) = h.fake.calls_to("sendMessage")
        assert set(params) == {"chat_id", "text", "link_preview_options"}

    drive(scenario)


def test_the_telegram_timestamp_reaches_the_age_rule() -> None:
    async def scenario(h: Harness) -> None:
        await h.say("/duraklat")
        await h.say("/devam", age_ms=2 * MINUTE_MS)
        assert h.fake.sent_texts()[-1] == (
            "/devam: komut eski, yok sayıldı (gönderileli 2 dk olmuş). "
            "Hâlâ geçerliyse yeniden gönder."
        )
        assert h.flag(StateKey.PAUSED) == "1"
        await h.say("/devam", age_ms=59_000)
        assert h.flag(StateKey.PAUSED) is None

    drive(scenario)


@pytest.mark.parametrize("age_ms", [0, 10 * MINUTE_MS])
def test_kill_with_pin_is_applied_and_the_message_deleted_whatever_its_age(age_ms: int) -> None:
    async def scenario(h: Harness) -> None:
        update = tg.private_update(
            f"/kill {PIN}", sent_at_ms=h.clock.now_ms - age_ms, message_id=77
        )
        with capture_logs() as logs:
            await h.feed(update)
        assert h.flag(StateKey.KILLED) == "1"
        # The message with the PIN goes first, then the answer.
        assert [call.endpoint for call in h.fake.calls] == ["getMe", "deleteMessage", "sendMessage"]
        assert h.fake.calls_to("deleteMessage") == [{"chat_id": CHAT_ID, "message_id": 77}]
        (reply,) = h.fake.sent_texts()
        assert reply.startswith("KILL etkin: giriş sinyalleri kesildi.")
        assert ("gecikmeyle işlendi" in reply) is (age_ms > 0)
        assert PIN not in repr(h.fake.calls)
        assert PIN not in repr(logs)

    drive(scenario)


def test_a_bare_pin_message_is_deleted() -> None:
    async def scenario(h: Harness) -> None:
        await h.feed(tg.private_update(PIN, sent_at_ms=NOW_MS, message_id=5))
        assert h.fake.calls_to("deleteMessage") == [{"chat_id": CHAT_ID, "message_id": 5}]
        assert h.fake.sent_texts() == [
            "Mesajındaki uzun sayı PIN olabileceği için mesajın siliniyor. PIN yalnızca "
            "/kill <PIN> ya da /devam <PIN> içinde kullanılır."
        ]

    drive(scenario)


def test_a_caption_is_treated_like_text() -> None:
    async def scenario(h: Harness) -> None:
        update = tg.private_update("", sent_at_ms=NOW_MS, message_id=9)
        del update["message"]["text"]
        update["message"]["caption"] = f"/kill {PIN}"
        await h.feed(update)
        assert h.fake.calls_to("deleteMessage") == [{"chat_id": CHAT_ID, "message_id": 9}]
        assert h.flag(StateKey.KILLED) == "1"

    drive(scenario)


def test_a_message_without_text_gets_the_help_list() -> None:
    async def scenario(h: Harness) -> None:
        update = tg.private_update("", sent_at_ms=NOW_MS)
        del update["message"]["text"]
        await h.feed(update)
        assert h.fake.sent_texts() == [h.commands.handle("", sent_at_ms=NOW_MS).text]
        assert h.fake.calls_to("deleteMessage") == []

    drive(scenario)


def test_when_the_pin_message_cannot_be_deleted_the_user_is_told() -> None:
    async def scenario(h: Harness) -> None:
        h.fake.script("deleteMessage", tg.error_response(400, "message can't be deleted"))
        with capture_logs() as logs:
            await h.say(f"/kill {PIN}")
        texts = h.fake.sent_texts()
        assert len(texts) == 2
        assert texts[0].startswith("KILL etkin")
        assert texts[1] == DELETE_FAILED
        assert h.flag(StateKey.KILLED) == "1"
        assert {
            "event": "telegram_delete_failed",
            "error_type": "BadRequest",
            "log_level": "error",
        } in logs
        assert PIN not in repr(logs)

    drive(scenario)


def test_no_delete_and_no_warning_for_ordinary_commands() -> None:
    async def scenario(h: Harness) -> None:
        for text in ("/durum", "/kill", "/devam", "/atladim 1", "selam"):
            await h.say(text)
        assert h.fake.calls_to("deleteMessage") == []
        assert DELETE_FAILED not in h.fake.sent_texts()
        assert len(h.fake.sent_texts()) == 5

    drive(scenario)


# --- chat whitelist -----------------------------------------------------------------------


def _foreign_updates() -> list[dict[str, Any]]:
    forged = tg.message_update("/duraklat", sent_at_ms=NOW_MS, user_id=tg.OTHER_USER_ID)
    edited = tg.other_user_update("/duraklat", sent_at_ms=NOW_MS)
    edited["edited_message"] = edited.pop("message")
    channel = tg.message_update("/duraklat", sent_at_ms=NOW_MS, chat_type="channel")
    channel["channel_post"] = channel.pop("message")
    anonymous = tg.private_update("/duraklat", sent_at_ms=NOW_MS)
    del anonymous["message"]["from"]
    return [
        tg.other_user_update("/duraklat", sent_at_ms=NOW_MS),
        tg.other_user_update(f"/kill {PIN}", sent_at_ms=NOW_MS),
        tg.group_update("/duraklat", sent_at_ms=NOW_MS),  # a real group, written by the owner
        tg.group_update(f"/kill {PIN}", sent_at_ms=NOW_MS, chat_id=CHAT_ID),  # right id, group
        tg.message_update("/duraklat", sent_at_ms=NOW_MS, chat_type="supergroup"),
        forged,  # the owner's chat id, but another sender
        edited,
        channel,
        anonymous,
        {"update_id": 999_001},  # an update kind without any chat
    ]


@pytest.mark.parametrize("index", range(10))
def test_updates_from_anywhere_else_are_ignored_and_counted(index: int) -> None:
    async def scenario(h: Harness) -> None:
        update = _foreign_updates()[index]
        with capture_logs() as logs:
            await h.feed(update)
        assert [call.endpoint for call in h.fake.calls] == ["getMe"]  # no reply, no delete
        assert h.flag(StateKey.PAUSED) is None
        assert h.flag(StateKey.KILLED) is None
        assert h.outbox() == []
        assert (
            "Yetkisiz sohbetlerden mesaj: 1" in h.commands.handle("/saglik", sent_at_ms=NOW_MS).text
        )
        (line,) = [entry for entry in logs if entry["event"] == "telegram_unauthorized_chat"]
        assert set(line) == {"event", "chat_id", "suppressed", "log_level"}
        assert "duraklat" not in repr(logs)
        assert PIN not in repr(logs)

    drive(scenario)


def test_the_unauthorized_log_is_throttled_and_reports_what_it_held_back() -> None:
    async def scenario(h: Harness) -> None:
        with capture_logs() as logs:
            for _ in range(3):
                await h.feed(tg.other_user_update("x", sent_at_ms=NOW_MS))
                h.clock.now_ms += 3_000
            h.clock.now_ms += 1_000  # 10 s after the first line
            await h.feed(tg.group_update("x", sent_at_ms=NOW_MS))
        lines = [entry for entry in logs if entry["event"] == "telegram_unauthorized_chat"]
        assert lines == [
            {
                "event": "telegram_unauthorized_chat",
                "chat_id": tg.OTHER_USER_ID,
                "suppressed": 0,
                "log_level": "warning",
            },
            {
                "event": "telegram_unauthorized_chat",
                "chat_id": tg.GROUP_ID,
                "suppressed": 2,
                "log_level": "warning",
            },
        ]
        assert (
            "Yetkisiz sohbetlerden mesaj: 4" in h.commands.handle("/saglik", sent_at_ms=NOW_MS).text
        )

    drive(scenario)


def test_an_edit_in_the_owners_chat_is_ignored_without_being_counted() -> None:
    async def scenario(h: Harness) -> None:
        edited = tg.private_update(f"/kill {PIN}", sent_at_ms=NOW_MS)
        edited["edited_message"] = edited.pop("message")
        await h.feed(edited)
        assert [call.endpoint for call in h.fake.calls] == ["getMe"]
        assert h.flag(StateKey.KILLED) is None
        assert (
            "Yetkisiz sohbetlerden mesaj: 0" in h.commands.handle("/saglik", sent_at_ms=NOW_MS).text
        )

    drive(scenario)


def test_the_bot_only_ever_addresses_the_whitelisted_chat(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("PTB_TIMEDELTA", "true")

    async def scenario(h: Harness) -> None:
        await h.first_step()
        for update in _foreign_updates():
            await h.feed(update)
        await h.say("/durum")
        await h.say(f"/kill {PIN}")
        h.entry()
        h.enqueue("stop güncelle", kind=OutboxKind.SIGNAL)
        h.enqueue("alarm", kind=OutboxKind.ALARM)
        # A group that "migrated" must not make the bot follow it to another chat.
        h.fake.script(
            "sendMessage",
            tg.error_response(400, "migrated", migrate_to_chat_id=tg.GROUP_ID),
            tg.retry_after_response(1),
            TimedOut(),
        )
        for _ in range(8):
            h.service.wake_sender()
            await h.service.send_due()
            h.clock.now_ms += 2_000
        assert h.fake.chat_ids() == {CHAT_ID}
        assert len(h.fake.calls_to("sendMessage")) >= 5
        assert all(params["chat_id"] == CHAT_ID for params in h.fake.calls_to("sendMessage"))

    drive(scenario)


# --- replies when Telegram is unreachable -------------------------------------------------


def test_a_reply_that_times_out_is_queued_and_delivered_by_the_sender() -> None:
    async def scenario(h: Harness) -> None:
        h.fake.script("sendMessage", TimedOut())
        with capture_logs() as logs:
            await h.say("/duraklat")
        assert h.flag(StateKey.PAUSED) == "1"  # the command itself was applied
        (queued,) = h.outbox()
        assert queued[:2] == (OutboxKind.NOTICE, OutboxStatus.PENDING)
        assert queued[2].startswith("Duraklatıldı")
        assert {
            "event": "telegram_reply_queued",
            "error_type": "TimedOut",
            "log_level": "warning",
        } in logs
        assert await h.service.send_due() is True
        assert h.fake.sent_texts() == [queued[2], queued[2]]
        assert h.outbox()[0][1] is OutboxStatus.SENT

    drive(scenario)


def test_a_flood_limited_reply_is_queued_and_holds_the_sender_back(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("PTB_TIMEDELTA", "true")

    async def scenario(h: Harness) -> None:
        h.fake.script("sendMessage", tg.retry_after_response(5))
        await h.say("/mod")
        await h.say("/yardim")  # while the deadline runs: queued without another request
        assert len(h.fake.calls_to("sendMessage")) == 1
        assert len(h.pending_texts()) == 2
        h.service.wake_sender()
        assert await h.service.send_due() is False
        h.clock.now_ms += 5_000
        assert await h.drain() == 2
        assert h.fake.sent_texts()[1].startswith("Mod: signal")
        assert h.fake.sent_texts()[2].startswith("PropBot komutları")

    drive(scenario)


@pytest.mark.parametrize("status", [400, 403])
def test_a_reply_telegram_rejects_is_logged_and_dropped(status: int) -> None:
    async def scenario(h: Harness) -> None:
        h.fake.script("sendMessage", tg.error_response(status, "nope"))
        with capture_logs() as logs:
            await h.say("/mod")
        assert h.outbox() == []
        assert [entry["event"] for entry in logs if entry["log_level"] == "error"] == [
            "telegram_reply_failed"
        ]

    drive(scenario)


def test_a_failing_handler_is_logged_by_exception_type_only() -> None:
    class Broken:
        def handle(self, text: str, *, sent_at_ms: int, standby: bool = False) -> Reply:
            raise RuntimeError(f"içerik: {text}")

        def note_unauthorized(self) -> None:
            raise AssertionError("not expected")

    async def scenario(h: Harness) -> None:
        with capture_logs() as logs:
            await h.say("/durum gizli-metin")
        assert [call.endpoint for call in h.fake.calls] == ["getMe"]
        assert logs == [
            {
                "event": "telegram_handler_failed",
                "error_type": "RuntimeError",
                "log_level": "error",
            }
        ]

    drive(scenario, commands=cast(Commands, Broken()))


class _Scripted:
    """Stands in for `Commands`: answers every message the same way (or raises)."""

    def __init__(self, outcome: Reply | Exception) -> None:
        self.outcome = outcome

    def handle(self, text: str, *, sent_at_ms: int, standby: bool = False) -> Reply:
        if isinstance(self.outcome, Exception):
            raise self.outcome
        return self.outcome

    def note_unauthorized(self) -> None:
        raise AssertionError("not expected")


def test_a_reply_is_lost_only_when_telegram_and_the_database_both_fail() -> None:
    async def scenario(h: Harness) -> None:
        await h.app.initialize()
        h.fake.script("sendMessage", TimedOut())
        h.store.close()
        with capture_logs() as logs:
            await h.say("/durum")
        assert logs == [
            {"event": "telegram_reply_lost", "error_type": "StoreError", "log_level": "error"}
        ]

    drive(scenario, commands=cast(Commands, _Scripted(Reply("yanıt"))))


def test_an_invalid_token_raised_inside_a_handler_is_fatal_and_not_quoted() -> None:
    async def scenario(h: Harness) -> TelegramFatalError:
        with capture_logs() as logs:
            await h.say("/durum")
        assert logs == [
            {
                "event": "telegram_handler_failed",
                "error_type": "InvalidToken",
                "log_level": "error",
            }
        ]
        with pytest.raises(TelegramFatalError) as caught:
            await h.service.send_due()
        return caught.value

    broken = _Scripted(InvalidToken(f"The token `{BOT_TOKEN}` was rejected by the server."))
    _assert_token_free(drive(scenario, commands=cast(Commands, broken)))


# --- sender: order, wake, retries ---------------------------------------------------------


def test_nothing_to_send_means_nothing_done() -> None:
    async def scenario(h: Harness) -> None:
        assert await h.service.send_due() is False
        assert await h.service.send_due() is False
        assert [call.endpoint for call in h.fake.calls] == ["getMe"]

    drive(scenario)


def test_messages_leave_in_fifo_order_one_per_step() -> None:
    async def scenario(h: Harness) -> None:
        ids = [
            h.enqueue("bir", kind=OutboxKind.ALARM),
            h.enqueue("iki", kind=OutboxKind.SIGNAL),
            h.enqueue("üç US500.cash"),
        ]
        with capture_logs() as logs:
            for sent in (1, 2, 3):
                h.clock.now_ms += 10
                assert await h.service.send_due() is True
                assert len(h.fake.sent_texts()) == sent
            assert await h.service.send_due() is False
        assert h.fake.sent_texts() == ["bir", "iki", "üç US500.cash"]
        assert [h.row(i).status for i in ids] == [OutboxStatus.SENT] * 3
        assert [h.row(i).sent_at_ms for i in ids] == [NOW_MS + 10, NOW_MS + 20, NOW_MS + 30]
        assert h.fake.calls_to("sendMessage")[2]["entities"] == [
            {"type": "code", "offset": 3, "length": 10}
        ]
        assert all(
            params["link_preview_options"] == {"is_disabled": True}
            for params in h.fake.calls_to("sendMessage")
        )
        sent_logs = [entry for entry in logs if entry["event"] == "telegram_message_sent"]
        assert [entry["outbox_id"] for entry in sent_logs] == ids
        assert "bir" not in repr(logs)

    drive(scenario)


def test_a_failed_head_blocks_the_queue_until_it_is_due_again() -> None:
    async def scenario(h: Harness) -> None:
        first = h.enqueue("bir")
        h.enqueue("iki")
        h.fake.script("sendMessage", TimedOut())
        assert await h.service.send_due() is True
        row = h.row(first)
        assert (row.status, row.attempts, row.last_error) == (OutboxStatus.PENDING, 1, "TimedOut")
        assert row.next_attempt_ms == NOW_MS + 1_000
        assert row.delivery_uncertain is True

        h.clock.now_ms += 999
        assert await h.service.send_due() is False  # FIFO: "iki" does not overtake
        h.clock.now_ms += 1
        assert await h.drain() == 2
        assert h.fake.sent_texts() == ["bir", "bir", "iki"]

    drive(scenario)


def test_a_wake_up_retries_at_once() -> None:
    async def scenario(h: Harness) -> None:
        h.enqueue("bir")
        h.fake.script("sendMessage", NetworkError("down"))
        assert await h.service.send_due() is True
        assert await h.service.send_due() is False  # in back-off
        h.service.wake_sender()  # a new row was enqueued
        assert await h.service.send_due() is True
        assert h.fake.sent_texts() == ["bir", "bir"]
        assert await h.service.send_due() is False  # the wake-up is used up

    drive(scenario)


@pytest.mark.parametrize("opt_in", [True, False])
def test_flood_control_is_obeyed_and_a_wake_up_does_not_bypass_it(
    monkeypatch: pytest.MonkeyPatch, opt_in: bool
) -> None:
    if opt_in:
        monkeypatch.setenv("PTB_TIMEDELTA", "true")
    else:
        monkeypatch.delenv("PTB_TIMEDELTA", raising=False)

    async def scenario(h: Harness) -> None:
        first = h.enqueue("bir")
        h.enqueue("iki")
        h.fake.script("sendMessage", tg.retry_after_response(7))
        h.clock.now_ms += 5
        assert await h.service.send_due() is True
        row = h.row(first)
        assert (row.status, row.attempts, row.last_error) == (OutboxStatus.PENDING, 1, "RetryAfter")
        assert row.next_attempt_ms == NOW_MS + 5 + 7_000  # the server's time
        assert row.delivery_uncertain is False  # Telegram answered: it was not delivered

        h.clock.now_ms += 6_999
        h.service.wake_sender()
        assert await h.service.send_due() is False
        assert len(h.fake.calls_to("sendMessage")) == 1
        h.clock.now_ms += 1
        assert await h.drain() == 2
        assert h.fake.sent_texts() == ["bir", "bir", "iki"]

    if opt_in:
        drive(scenario)
    else:
        # Without the opt-in the library warns while it builds the error; behaviour is the same.
        with pytest.warns(PTBDeprecationWarning):
            drive(scenario)


def test_backoff_grows_to_thirty_seconds_and_stays_there() -> None:
    async def scenario(h: Harness) -> None:
        outbox_id = h.enqueue("bir")
        delays = []
        for attempt in range(1, 9):
            h.fake.script("sendMessage", TimedOut())
            assert await h.service.send_due() is True
            row = h.row(outbox_id)
            assert row.attempts == attempt
            delays.append(row.next_attempt_ms - h.clock.now_ms)
            h.clock.now_ms = row.next_attempt_ms
        assert delays == [1_000, 2_000, 4_000, 8_000, 16_000, 30_000, 30_000, 30_000]
        assert await h.service.send_due() is True
        assert h.row(outbox_id).status is OutboxStatus.SENT

    drive(scenario)


def _transport_failures() -> list[tuple[BaseException | tg.Response, str, bool]]:
    """(scripted outcome, expected last_error, expected delivery_uncertain)."""
    token_url = f"https://api.telegram.org/bot{BOT_TOKEN}/sendMessage"
    return [
        (TimedOut(), "TimedOut", True),
        (caused_by(TimedOut(), httpx.ReadTimeout("no answer")), "TimedOut", True),
        (caused_by(TimedOut(), httpx.WriteTimeout("stalled")), "TimedOut", True),
        (caused_by(TimedOut(), httpx.ConnectTimeout("handshake")), "TimedOut", False),
        (caused_by(TimedOut("Pool timeout"), httpx.PoolTimeout("busy")), "TimedOut", False),
        (
            caused_by(NetworkError("httpx.ConnectError: x"), httpx.ConnectError("x")),
            "NetworkError",
            False,
        ),
        (caused_by(NetworkError("httpx.ReadError: x"), httpx.ReadError("x")), "NetworkError", True),
        (
            caused_by(NetworkError("httpx.RemoteProtocolError: x"), httpx.RemoteProtocolError("x")),
            "NetworkError",
            True,
        ),
        (tg.error_response(502, "Bad Gateway"), "NetworkError", True),
        (tg.error_response(500, "Internal Server Error"), "NetworkError", True),
        # Not a Telegram error at all: the library wraps it, message and URL included.
        (RuntimeError(f"boom {token_url}"), "NetworkError", True),
        (httpx.ConnectError(f"refused {token_url}"), "NetworkError", False),
    ]


@pytest.mark.parametrize("index", range(12))
def test_transient_failures_are_retried_and_classified(index: int) -> None:
    outcome, error_name, uncertain = _transport_failures()[index]

    async def scenario(h: Harness) -> None:
        await h.first_step()  # the row below is created after start-up: not flagged
        signal_id, outbox_id = h.entry(expires_at_ms=NOW_MS + 10 * MINUTE_MS)
        h.fake.script("sendMessage", outcome)
        with capture_logs() as logs:
            assert await h.service.send_due() is True
        row = h.row(outbox_id)
        assert row.status is OutboxStatus.PENDING
        assert row.attempts == 1
        assert row.last_error == error_name  # the class name only
        assert row.delivery_uncertain is uncertain
        assert row.next_attempt_ms == NOW_MS + 1_000
        assert h.counted(signal_id) == (True, None)
        (line,) = [entry for entry in logs if entry["event"] == "telegram_send_retry"]
        assert (line["error_type"], line["uncertain"]) == (error_name, uncertain)
        assert BOT_TOKEN not in repr(logs)
        assert BOT_TOKEN not in repr(row)

    drive(scenario)


def test_uncertainty_is_sticky() -> None:
    async def scenario(h: Harness) -> None:
        await h.first_step()
        _signal_id, outbox_id = h.entry(expires_at_ms=NOW_MS + 10 * MINUTE_MS)
        h.fake.script(
            "sendMessage",
            caused_by(NetworkError("x"), httpx.ConnectError("x")),
            TimedOut(),
            caused_by(NetworkError("x"), httpx.ConnectError("x")),
        )
        seen = []
        for _ in range(3):
            assert await h.service.send_due() is True
            row = h.row(outbox_id)
            seen.append(row.delivery_uncertain)
            h.clock.now_ms = row.next_attempt_ms
        assert seen == [False, True, True]

    drive(scenario)


REFUSED = "GÖNDERİLEMEDİ ve atlandı"
MAY_HAVE_ARRIVED = "sana ULAŞMAMIŞ OLABİLİR"


def _permanent_failures() -> list[tuple[BaseException | tg.Response, str, str]]:
    """Outcome of sendMessage, the class it is recorded as, and what the user is told."""
    return [
        (tg.error_response(400, "Bad Request: message text is empty"), "BadRequest", REFUSED),
        (tg.error_response(403, "Forbidden: bot was blocked by the user"), "Forbidden", REFUSED),
        (
            tg.error_response(400, "migrated", migrate_to_chat_id=tg.GROUP_ID),
            "ChatMigrated",
            REFUSED,
        ),
        (tg.error_response(409, "Conflict"), "Conflict", REFUSED),
        (BadRequest("chat not found"), "BadRequest", REFUSED),
        (Forbidden("kicked"), "Forbidden", REFUSED),
        # Not an answer of the Bot API: nobody knows whether the message arrived.
        (TelegramError(f"odd {BOT_TOKEN}"), "TelegramError", MAY_HAVE_ARRIVED),
        ((200, b"<html>gateway</html>"), "TelegramError", MAY_HAVE_ARRIVED),
    ]


@pytest.mark.parametrize("index", range(8))
def test_a_permanent_failure_is_set_aside_and_the_queue_moves_on(index: int) -> None:
    outcome, error_name, fate = _permanent_failures()[index]

    async def scenario(h: Harness) -> None:
        bad = h.enqueue("bozuk", kind=OutboxKind.SIGNAL)
        good = h.enqueue("sağlam")
        h.fake.script("sendMessage", outcome)
        with capture_logs() as logs:
            assert await h.service.send_due() is True  # "bozuk" is refused for good
            assert await h.service.send_due() is True  # "sağlam" goes out
            assert await h.service.send_due() is True  # the alarm about the dropped message
            assert await h.service.send_due() is False
        row = h.row(bad)
        assert (row.status, row.last_error, row.attempts) == (OutboxStatus.FAILED, error_name, 0)
        assert h.row(good).status is OutboxStatus.SENT
        assert h.fake.sent_texts() == [
            "bozuk",
            "sağlam",
            f"[ALARM] Telegram bir mesaj için kalıcı hata verdi ({error_name}); mesaj "
            f"{fate}.\nTradingView Alert Log'undan ne olduğunu kontrol et.",
        ]
        assert row.delivery_uncertain is (fate == MAY_HAVE_ARRIVED)
        assert {
            "event": "telegram_send_failed",
            "error_type": error_name,
            "outbox_id": bad,
            "kind": "signal",
            "signal_id": None,
            "log_level": "error",
        } in logs
        assert BOT_TOKEN not in repr(logs)
        assert h.fake.chat_ids() == {CHAT_ID}

    drive(scenario)


def test_a_row_that_breaks_the_sender_itself_is_quarantined(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    real = telegram_module.code_entity_spans

    def explode(text: str) -> list[tuple[int, int]]:
        if "zehir" in text:
            raise ValueError(f"cannot handle {text!r}")
        return real(text)

    monkeypatch.setattr(telegram_module, "code_entity_spans", explode)

    async def scenario(h: Harness) -> None:
        poisoned = h.enqueue("zehir")
        good = h.enqueue("sağlam")
        with capture_logs() as logs:
            assert await h.drain() == 3  # the poisoned row, the good one, the alarm
        row = h.row(poisoned)
        assert (row.status, row.last_error) == (OutboxStatus.FAILED, "ValueError")
        assert h.row(good).status is OutboxStatus.SENT
        sent = h.fake.sent_texts()
        assert sent[0] == "sağlam"
        assert sent[1].startswith("[ALARM] Telegram bir mesaj için kalıcı hata verdi (ValueError)")
        assert "zehir" not in repr(logs) + sent[1]

    drive(scenario)


def test_an_entry_that_breaks_before_anything_is_sent_is_released_not_doubted(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # A failure of our own code while the message is prepared proves that nothing left.
    real = telegram_module.code_entity_spans

    def explode(text: str) -> list[tuple[int, int]]:
        if text.startswith("[SİNYAL]"):
            raise ValueError("cannot prepare this text")
        return real(text)

    monkeypatch.setattr(telegram_module, "code_entity_spans", explode)

    async def scenario(h: Harness) -> None:
        await h.first_step()
        signal_id, message_id = tg.forwarded_entry(h.store, prop_day=TODAY, now_ms=NOW_MS)

        assert await h.drain() == 2  # the entry is set aside, then the alarm goes out

        row = h.row(message_id)
        assert (row.status, row.last_error, row.delivery_uncertain) == (
            OutboxStatus.FAILED,
            "ValueError",
            False,
        )
        assert h.counted(signal_id) == (False, ReleaseReason.UNDELIVERED)
        assert h.fake.sent_texts() == [
            f"[ALARM] Telegram sinyal id {signal_id} mesajı için kalıcı hata verdi (ValueError); "
            "mesaj GÖNDERİLEMEDİ ve atlandı.\n"
            "TradingView Alert Log'undan ne olduğunu kontrol et. Bu bir giriş sinyaliydi: ATLA "
            "say; bütçeden düşüldü."
        ]

    drive(scenario)


def test_an_entry_that_telegram_refuses_is_released_and_reported() -> None:
    async def scenario(h: Harness) -> None:
        await h.first_step()
        signal_id, message_id = tg.forwarded_entry(h.store, prop_day=TODAY, now_ms=NOW_MS)
        h.fake.script("sendMessage", BadRequest("message is too long"))

        assert await h.drain() == 2  # the refused entry, then the alarm

        assert h.row(message_id).status is OutboxStatus.FAILED
        with h.store.transaction() as tx:
            entry = tx.get_budget_entry(signal_id)
            used = tx.budget_used(TODAY)
        assert entry is not None
        assert (entry.counted, entry.release_reason) == (False, ReleaseReason.UNDELIVERED)
        assert used == 0.0
        assert h.fake.sent_texts()[-1] == (
            f"[ALARM] Telegram sinyal id {signal_id} mesajı için kalıcı hata verdi (BadRequest); "
            "mesaj GÖNDERİLEMEDİ ve atlandı.\n"
            "TradingView Alert Log'undan ne olduğunu kontrol et. Bu bir giriş sinyaliydi: ATLA "
            "say; bütçeden düşüldü."
        )

    drive(scenario)


def test_an_entry_that_may_have_arrived_is_not_released_by_a_later_refusal() -> None:
    # The first attempt timed out after the request left: the user may have the signal.
    # "Telegram refused the retry" is then no proof that they do not, and a release would
    # also silence that trade's stop and exit messages.
    async def scenario(h: Harness) -> None:
        await h.first_step()
        signal_id, message_id = h.entry(expires_at_ms=NOW_MS + 10 * MINUTE_MS)
        h.fake.script("sendMessage", TimedOut(), BadRequest("message is too long"))

        assert await h.service.send_due() is True
        assert h.row(message_id).delivery_uncertain is True
        h.clock.now_ms = h.row(message_id).next_attempt_ms
        assert await h.drain() == 2  # the refused retry, then the notice

        assert h.row(message_id).status is OutboxStatus.FAILED
        assert h.counted(signal_id) == (True, None)
        assert h.fake.sent_texts()[-1] == uncertain_notice(
            signal_id, "gönderim kalıcı hatayla bitti: BadRequest"
        )
        assert not any("GÖNDERİLEMEDİ" in text for text in h.fake.sent_texts())

    drive(scenario)


@pytest.mark.parametrize(
    "outcome",
    [
        TelegramError("Invalid server response"),
        (200, b"not json"),
        # Accepted by Telegram, but the library cannot build a Message from the answer.
        tg.ok({"sent": "yes, but this is no message object"}),
    ],
    ids=["plain_telegram_error", "answer_is_not_json", "answer_is_not_a_message"],
)
def test_an_entry_whose_send_ended_in_an_unreadable_way_is_not_released(
    outcome: tg.Scripted,
) -> None:
    async def scenario(h: Harness) -> None:
        await h.first_step()
        signal_id, message_id = h.entry(expires_at_ms=NOW_MS + 10 * MINUTE_MS)
        h.fake.script("sendMessage", outcome)

        assert await h.drain() == 2

        row = h.row(message_id)
        assert (row.status, row.delivery_uncertain) == (OutboxStatus.FAILED, True)
        assert h.counted(signal_id) == (True, None)
        assert h.fake.sent_texts()[-1].startswith(f"[İLETİM BELİRSİZ] id {signal_id}: ")
        assert h.fake.sent_texts()[-1].endswith(f"/atladim {signal_id}")

    drive(scenario)


def test_dropped_messages_raise_one_alarm_per_throttle_interval() -> None:
    async def scenario(h: Harness) -> None:
        for text in ("bir", "iki", "üç"):
            h.enqueue(text)
            h.fake.script("sendMessage", Forbidden("bot was blocked by the user"))
            await h.drain()
        alarms = [text for text in h.fake.sent_texts() if text.startswith("[ALARM]")]
        assert len(alarms) == 1

        h.clock.now_ms += 10 * MINUTE_MS
        h.enqueue("dört")
        h.fake.script("sendMessage", Forbidden("bot was blocked by the user"))
        await h.drain()
        alarms = [text for text in h.fake.sent_texts() if text.startswith("[ALARM]")]
        assert len(alarms) == 2

    drive(scenario)


def test_a_refused_alarm_raises_no_alarm_about_itself() -> None:
    async def scenario(h: Harness) -> None:
        alarm = h.enqueue("[ALARM] bir şey", kind=OutboxKind.ALARM)
        h.fake.script("sendMessage", Forbidden("bot was blocked by the user"))

        assert await h.drain() == 1

        assert h.row(alarm).status is OutboxStatus.FAILED
        with h.store.transaction() as tx:
            assert tx.outbox_counts()[OutboxStatus.PENDING] == 0
        assert h.fake.sent_texts() == ["[ALARM] bir şey"]

    drive(scenario)


def test_two_concurrent_steps_do_not_send_a_message_twice() -> None:
    async def scenario(h: Harness) -> None:
        h.enqueue("bir")
        results = await asyncio.gather(h.service.send_due(), h.service.send_due())
        assert sorted(results) == [False, True]
        assert h.fake.sent_texts() == ["bir"]

    drive(scenario)


def test_no_transaction_is_held_while_talking_to_telegram() -> None:
    async def scenario(h: Harness) -> None:
        outbox_id = h.enqueue("bir")
        seen: list[OutboxStatus] = []

        def read_from_another_thread() -> OutboxStatus:
            with h.store.transaction() as tx:
                row = tx.get_message(outbox_id)
            assert row is not None
            return row.status

        async def in_flight(_call: tg.Call) -> None:
            # Would dead-lock (and time out) if the sender still held its transaction.
            seen.append(await asyncio.wait_for(asyncio.to_thread(read_from_another_thread), 5))

        h.fake.script("sendMessage", in_flight)
        assert await h.service.send_due() is True
        assert seen == [OutboxStatus.PENDING]
        assert h.row(outbox_id).status is OutboxStatus.SENT

    drive(scenario)


def test_a_store_failure_ends_the_step_with_store_error() -> None:
    async def scenario(h: Harness) -> None:
        h.enqueue("bir")

        async def close_the_store(_call: tg.Call) -> None:
            h.store.close()

        h.fake.script("sendMessage", close_the_store)
        with pytest.raises(StoreError):
            await h.service.send_due()
        with pytest.raises(StoreError):
            await h.service.send_due()

    drive(scenario)


# --- sender: late prefix ------------------------------------------------------------------


def test_a_signal_without_deadline_that_leaves_late_says_so() -> None:
    async def scenario(h: Harness) -> None:
        h.enqueue("[STOP GÜNCELLE] NY ORB FVG", kind=OutboxKind.SIGNAL)
        h.enqueue("[ÇIKIŞ] NY ORB FVG", kind=OutboxKind.SIGNAL)
        h.clock.now_ms += VALIDITY_SECONDS * 1000  # exactly the validity: not late yet
        assert await h.service.send_due() is True
        h.clock.now_ms += 1 + 3 * MINUTE_MS + 20_000
        assert await h.service.send_due() is True
        assert h.fake.sent_texts() == [
            "[STOP GÜNCELLE] NY ORB FVG",
            "[GEÇ İLETİLDİ +4 dk 20 sn] [ÇIKIŞ] NY ORB FVG",
        ]
        # The stored text is untouched.
        assert [text for _kind, _status, text in h.outbox()] == [
            "[STOP GÜNCELLE] NY ORB FVG",
            "[ÇIKIŞ] NY ORB FVG",
        ]

    drive(scenario)


def test_only_signals_without_deadline_get_the_late_prefix() -> None:
    async def scenario(h: Harness) -> None:
        await h.first_step()
        h.enqueue("alarm", kind=OutboxKind.ALARM)
        h.enqueue("not", kind=OutboxKind.NOTICE)
        h.entry(expires_at_ms=NOW_MS + 60 * MINUTE_MS, text="[SİNYAL] giriş")
        h.clock.now_ms += 30 * MINUTE_MS
        assert await h.drain() == 3
        assert h.fake.sent_texts() == ["alarm", "not", "[SİNYAL] giriş"]

    drive(scenario)


# --- sender: expiry -----------------------------------------------------------------------


def test_an_entry_proven_undelivered_expires_releases_its_budget_and_tells_the_user() -> None:
    async def scenario(h: Harness) -> None:
        await h.first_step()
        signal_id, outbox_id = h.entry()
        assert h.row(outbox_id).expires_at_ms == ENTRY_DEADLINE_MS
        # Telegram is unreachable (the request provably never leaves) until the deadline passes.
        h.fake.script("sendMessage", caused_by(NetworkError("x"), httpx.ConnectError("x")))
        assert await h.service.send_due() is True
        h.clock.now_ms = ENTRY_DEADLINE_MS + 1

        with capture_logs() as logs:
            assert await h.service.send_due() is True
        row = h.row(outbox_id)
        assert (row.status, row.delivery_uncertain) == (OutboxStatus.EXPIRED, False)
        assert h.counted(signal_id) == (False, ReleaseReason.UNDELIVERED)
        assert h.fake.sent_texts() == ["[SİNYAL] id 1", "[İLETİLEMEDİ — ATLA] id 1"]
        assert h.outbox()[1] == (
            OutboxKind.NOTICE,
            OutboxStatus.SENT,  # the same step delivered the notice it had just queued
            "[İLETİLEMEDİ — ATLA] id 1",
        )
        assert {
            "event": "telegram_message_expired",
            "outbox_id": outbox_id,
            "signal_id": signal_id,
            "uncertain": False,
            "attempts": 1,
            "why": "expired",
            "log_level": "warning",
        } in logs
        assert await h.service.send_due() is False

    drive(scenario)


def test_an_entry_that_may_have_been_delivered_keeps_its_budget_and_asks_the_user() -> None:
    async def scenario(h: Harness) -> None:
        await h.first_step()
        signal_id, outbox_id = h.entry()
        h.fake.script("sendMessage", TimedOut())  # the request left; no answer came back
        assert await h.service.send_due() is True
        assert h.row(outbox_id).delivery_uncertain is True
        h.clock.now_ms = ENTRY_DEADLINE_MS + 1

        assert await h.service.send_due() is True
        assert h.row(outbox_id).status is OutboxStatus.EXPIRED
        assert h.counted(signal_id) == (True, None)
        assert h.fake.sent_texts()[-1] == uncertain_notice(signal_id)
        assert h.fake.sent_texts()[-1] == (
            "[İLETİM BELİRSİZ] id 1: giriş sinyali sana ulaşmış da olabilir, ulaşmamış da "
            "(geçerliliği doldu). Gördüysen ve girdiysen bir şey yapma; görmediysen ya da "
            "girmediysen: /atladim 1"
        )
        assert "[İLETİLEMEDİ" not in repr(h.outbox())

    drive(scenario)


def test_the_deadline_itself_is_still_in_time() -> None:
    async def scenario(h: Harness) -> None:
        await h.first_step()
        signal_id, outbox_id = h.entry()
        h.clock.now_ms = ENTRY_DEADLINE_MS
        assert await h.service.send_due() is True
        assert h.row(outbox_id).status is OutboxStatus.SENT
        assert h.counted(signal_id) == (True, None)

    drive(scenario)


def test_an_entry_expires_even_when_it_is_not_the_head_of_the_queue() -> None:
    async def scenario(h: Harness) -> None:
        await h.first_step()
        head = h.enqueue("öndeki mesaj")
        first, first_message = h.entry()
        second, second_message = h.entry(offset=1)  # deadline one minute later
        for _ in range(5):  # the head keeps failing: 1 + 2 + 4 + 8 + 16 = 31 s pass
            h.fake.script("sendMessage", caused_by(NetworkError("x"), httpx.ConnectError("x")))
            assert await h.service.send_due() is True
            h.clock.now_ms = h.row(head).next_attempt_ms
        h.fake.script("sendMessage", caused_by(NetworkError("x"), httpx.ConnectError("x")))
        assert await h.service.send_due() is True
        assert h.row(head).next_attempt_ms > ENTRY_DEADLINE_MS + 1

        h.clock.now_ms = ENTRY_DEADLINE_MS + 1
        sends_before = len(h.fake.calls_to("sendMessage"))
        assert await h.service.send_due() is True  # expired the first entry; the head is not due
        assert len(h.fake.calls_to("sendMessage")) == sends_before
        assert h.row(first_message).status is OutboxStatus.EXPIRED
        assert h.row(second_message).status is OutboxStatus.PENDING
        assert h.counted(first) == (False, ReleaseReason.UNDELIVERED)
        assert h.counted(second) == (True, None)
        assert h.pending_texts() == ["öndeki mesaj", "[SİNYAL] id 2", "[İLETİLEMEDİ — ATLA] id 1"]
        assert await h.service.send_due() is False

    drive(scenario)


def test_every_overdue_entry_is_expired_in_one_step() -> None:
    async def scenario(h: Harness) -> None:
        await h.first_step()
        entries = [h.entry(offset=offset) for offset in range(3)]
        h.enqueue("sonraki")
        h.clock.now_ms = ENTRY_DEADLINE_MS + 5 * MINUTE_MS
        assert await h.service.send_due() is True
        for signal_id, outbox_id in entries:
            assert h.row(outbox_id).status is OutboxStatus.EXPIRED
            assert h.counted(signal_id) == (False, ReleaseReason.UNDELIVERED)
        # The same step went on to the next message; the notices follow in order.
        assert h.fake.sent_texts() == ["sonraki"]
        assert await h.drain() == 3
        assert h.fake.sent_texts()[1:] == [f"[İLETİLEMEDİ — ATLA] id {i}" for i in (1, 2, 3)]

    drive(scenario)


def test_an_expired_entry_whose_budget_was_already_released_stays_as_it_is() -> None:
    async def scenario(h: Harness) -> None:
        await h.first_step()
        signal_id, outbox_id = h.entry()
        with h.store.transaction() as tx:
            tx.set_budget_counted(
                signal_id=signal_id, counted=False, reason=ReleaseReason.SKIPPED, now_ms=NOW_MS
            )
        h.clock.now_ms = ENTRY_DEADLINE_MS + 1
        assert await h.service.send_due() is True
        assert h.row(outbox_id).status is OutboxStatus.EXPIRED
        assert h.counted(signal_id) == (False, ReleaseReason.SKIPPED)

    drive(scenario)


def test_a_deadline_row_without_signal_or_expiry_text_just_expires() -> None:
    async def scenario(h: Harness) -> None:
        await h.first_step()
        outbox_id = h.enqueue("süreli", expires_at_ms=NOW_MS + 1_000)
        h.clock.now_ms += 1_001
        assert await h.service.send_due() is True
        assert h.outbox() == [(OutboxKind.NOTICE, OutboxStatus.EXPIRED, "süreli")]
        assert h.row(outbox_id).status is OutboxStatus.EXPIRED
        assert h.fake.sent_texts() == []

    drive(scenario)


# --- sender: start-up ---------------------------------------------------------------------


def test_the_first_step_flags_waiting_entries_as_possibly_delivered() -> None:
    async def scenario(h: Harness) -> None:
        # Left behind by the previous process, which may have died right after sending them.
        signal_id, entry_message = h.entry()
        plain = h.enqueue("stop güncelle", kind=OutboxKind.SIGNAL)
        h.clock.now_ms = ENTRY_DEADLINE_MS + 1
        with capture_logs() as logs:
            assert await h.service.send_due() is True
        assert {
            "event": "telegram_pending_entries_uncertain",
            "count": 1,
            "log_level": "warning",
        } in logs
        row = h.row(entry_message)
        assert (row.status, row.delivery_uncertain) == (OutboxStatus.EXPIRED, True)
        assert h.row(plain).delivery_uncertain is False  # only rows with a deadline matter
        assert h.counted(signal_id) == (True, None)
        assert await h.drain() == 1
        assert h.fake.sent_texts() == ["stop güncelle", uncertain_notice(signal_id)]

    drive(scenario)


def test_the_flagging_happens_once() -> None:
    async def scenario(h: Harness) -> None:
        _old, old_message = h.entry(expires_at_ms=NOW_MS + 60 * MINUTE_MS)
        assert await h.service.send_due() is True
        assert h.row(old_message).delivery_uncertain is True
        _new, new_message = h.entry(offset=1, expires_at_ms=NOW_MS + 60 * MINUTE_MS)
        assert await h.service.send_due() is True
        row = h.row(new_message)
        assert (row.status, row.delivery_uncertain) == (OutboxStatus.SENT, False)

    drive(scenario)


def test_while_telegram_is_unreachable_at_start_up_nothing_blocks_and_entries_still_expire() -> (
    None
):
    async def scenario(h: Harness) -> None:
        down = caused_by(NetworkError("httpx.ConnectError"), httpx.ConnectError("no route"))
        h.fake.script("getMe", down, down, TimedOut())
        signal_id, outbox_id = h.entry()
        h.enqueue("alarm", kind=OutboxKind.ALARM)

        with capture_logs() as logs:
            assert await h.service.send_due() is True  # flagged the waiting entry; no send
            assert await h.service.send_due() is False  # connect back-off: no second getMe yet
            assert len(h.fake.calls) == 1
            h.clock.now_ms += 1_000
            assert await h.service.send_due() is False
            assert len(h.fake.calls) == 2
            h.clock.now_ms += 1_999
            assert await h.service.send_due() is False
            assert len(h.fake.calls) == 2  # second failure: wait 2 s
            h.clock.now_ms = ENTRY_DEADLINE_MS + 1
            assert await h.service.send_due() is True  # third getMe failed; the entry expired
        assert [call.endpoint for call in h.fake.calls] == ["getMe"] * 3
        assert h.row(outbox_id).status is OutboxStatus.EXPIRED
        assert h.counted(signal_id) == (True, None)  # it predates this process: uncertain
        failures = [entry for entry in logs if entry["event"] == "telegram_connect_failed"]
        assert [(entry["error_type"], entry["retry_in_ms"]) for entry in failures] == [
            ("NetworkError", 1_000),
            ("NetworkError", 2_000),
            ("TimedOut", 4_000),
        ]

        h.clock.now_ms += 4_000
        assert await h.drain() == 2
        assert h.fake.sent_texts() == ["alarm", uncertain_notice(signal_id)]

    drive(scenario)


# --- sender: pause and kill ---------------------------------------------------------------


@pytest.mark.parametrize("command", ["/duraklat", f"/kill {PIN}"])
def test_waiting_entries_are_dropped_after_pause_or_kill(command: str) -> None:
    async def scenario(h: Harness) -> None:
        await h.first_step()
        h.fake.script("sendMessage", caused_by(NetworkError("x"), httpx.ConnectError("x")))
        first, first_message = h.entry()
        second, second_message = h.entry(offset=1)
        stop_update = h.enqueue("[STOP GÜNCELLE]", kind=OutboxKind.SIGNAL)
        assert await h.service.send_due() is True  # one failed attempt, provably not sent
        await h.say(command)
        reply = h.fake.sent_texts()[-1]

        with capture_logs() as logs:
            assert await h.service.send_due() is True
        for signal_id, outbox_id in ((first, first_message), (second, second_message)):
            assert h.row(outbox_id).status is OutboxStatus.EXPIRED
            assert h.counted(signal_id) == (False, ReleaseReason.UNDELIVERED)
        # No notice about the dropped entries; what reduces risk is still delivered.
        assert h.row(stop_update).status is OutboxStatus.SENT
        assert h.fake.sent_texts()[-2:] == [reply, "[STOP GÜNCELLE]"]
        assert [status for _kind, status, _text in h.outbox()] == [
            OutboxStatus.EXPIRED,
            OutboxStatus.EXPIRED,
            OutboxStatus.SENT,
        ]
        dropped = [entry for entry in logs if entry["event"] == "telegram_entry_dropped"]
        assert [entry["signal_id"] for entry in dropped] == [first, second]
        assert await h.service.send_due() is False

    drive(scenario)


def test_entries_flow_again_after_devam() -> None:
    async def scenario(h: Harness) -> None:
        await h.first_step()
        await h.say("/duraklat")
        await h.say("/devam")
        signal_id, outbox_id = h.entry()
        assert await h.service.send_due() is True
        assert h.row(outbox_id).status is OutboxStatus.SENT
        assert h.counted(signal_id) == (True, None)

    drive(scenario)


def test_an_entry_behind_another_message_is_dropped_when_it_reaches_the_head() -> None:
    async def scenario(h: Harness) -> None:
        await h.first_step()
        h.enqueue("öndeki")
        signal_id, outbox_id = h.entry()
        await h.say("/duraklat")
        assert await h.service.send_due() is True  # sends "öndeki"; the entry is not the head yet
        assert h.row(outbox_id).status is OutboxStatus.PENDING
        assert await h.service.send_due() is True
        assert h.row(outbox_id).status is OutboxStatus.EXPIRED
        assert h.counted(signal_id) == (False, ReleaseReason.UNDELIVERED)
        assert "[SİNYAL] id 1" not in h.fake.sent_texts()

    drive(scenario)


def test_a_possibly_delivered_entry_dropped_by_pause_keeps_its_budget_and_asks() -> None:
    async def scenario(h: Harness) -> None:
        await h.first_step()
        signal_id, outbox_id = h.entry()
        h.fake.script("sendMessage", TimedOut())
        assert await h.service.send_due() is True
        await h.say("/duraklat")
        assert await h.service.send_due() is True
        assert h.row(outbox_id).status is OutboxStatus.EXPIRED
        assert h.counted(signal_id) == (True, None)
        # The same step went on to the notice it had just queued.
        assert h.fake.sent_texts()[-1] == uncertain_notice(
            signal_id, "duraklatma/KILL nedeniyle yeniden gönderilmedi"
        )
        assert h.fake.sent_texts()[-1] == (
            "[İLETİM BELİRSİZ] id 1: giriş sinyali sana ulaşmış da olabilir, ulaşmamış da "
            "(duraklatma/KILL nedeniyle yeniden gönderilmedi). Gördüysen ve girdiysen bir şey "
            "yapma; görmediysen ya da girmediysen: /atladim 1"
        )
        assert await h.service.send_due() is False

    drive(scenario)


def test_an_unreadable_pause_flag_counts_as_set() -> None:
    async def scenario(h: Harness) -> None:
        await h.first_step()
        with h.store.transaction() as tx:
            tx.set_state(StateKey.PAUSED, "yes", NOW_MS)
        _signal_id, outbox_id = h.entry()
        assert await h.service.send_due() is True
        assert h.row(outbox_id).status is OutboxStatus.EXPIRED
        with h.store.transaction() as tx:
            tx.set_state(StateKey.PAUSED, "0", NOW_MS)
        _other, other_message = h.entry(offset=1)
        assert await h.service.send_due() is True
        assert h.row(other_message).status is OutboxStatus.SENT

    drive(scenario)


# --- fatal: the token ---------------------------------------------------------------------


def _assert_token_free(error: BaseException, text: str = FATAL_TOKEN) -> None:
    assert type(error) is TelegramFatalError
    assert str(error) == text
    assert error.__cause__ is None
    assert error.__context__ is None
    rendered = "".join(traceback.format_exception(error)) + repr(error) + repr(error.args)
    assert BOT_TOKEN not in rendered
    assert "InvalidToken" not in rendered


@pytest.mark.parametrize("status", [401, 404])
def test_a_token_rejected_at_start_up_is_fatal_and_never_quoted(status: int) -> None:
    async def scenario(h: Harness) -> TelegramFatalError:
        h.fake.script("getMe", tg.error_response(status, "Unauthorized"))
        h.enqueue("bir")
        with capture_logs() as logs, pytest.raises(TelegramFatalError) as caught:
            await h.service.send_due()
        assert BOT_TOKEN not in repr(logs)
        with pytest.raises(TelegramFatalError):  # and it stays fatal
            await h.service.send_due()
        assert h.fake.sent_texts() == []
        return caught.value

    _assert_token_free(drive(scenario))


def test_the_library_error_would_have_quoted_the_token() -> None:
    """Guards the reason for the conversion: if this stops being true the tests above still hold."""

    async def scenario(h: Harness) -> str:
        h.fake.script("getMe", tg.error_response(401, "Unauthorized"))
        with pytest.raises(InvalidToken) as caught:
            await h.app.initialize()
        return str(caught.value)

    assert BOT_TOKEN in drive(scenario)


def test_a_token_rejected_while_sending_is_fatal_and_leaves_the_row_pending() -> None:
    async def scenario(h: Harness) -> TelegramFatalError:
        outbox_id = h.enqueue("bir")
        h.fake.script("sendMessage", tg.error_response(401, "Unauthorized"))
        with capture_logs() as logs, pytest.raises(TelegramFatalError) as caught:
            await h.service.send_due()
        row = h.row(outbox_id)
        assert (row.status, row.attempts, row.last_error) == (OutboxStatus.PENDING, 0, None)
        assert BOT_TOKEN not in repr(logs)
        return caught.value

    _assert_token_free(drive(scenario))


@pytest.mark.parametrize("endpoint", ["sendMessage", "deleteMessage"])
def test_a_token_rejected_while_answering_a_command_stops_the_sender(endpoint: str) -> None:
    async def scenario(h: Harness) -> TelegramFatalError:
        h.fake.script(endpoint, tg.error_response(401, "Unauthorized"))
        await h.say(f"/kill {PIN}")  # the handler itself must not raise into the library
        assert h.flag(StateKey.KILLED) == "1"
        with pytest.raises(TelegramFatalError) as caught:
            await h.service.send_due()
        return caught.value

    _assert_token_free(drive(scenario))


def test_run_ends_with_the_fatal_error() -> None:
    async def scenario(h: Harness) -> TelegramFatalError:
        h.fake.script("getMe", tg.error_response(401, "Unauthorized"))
        with capture_logs() as logs, pytest.raises(TelegramFatalError) as caught:
            await asyncio.wait_for(h.service.run(asyncio.Event()), 5)
        assert BOT_TOKEN not in repr(logs)
        return caught.value

    _assert_token_free(drive(scenario))


def test_the_token_never_reaches_last_error_or_a_log_line() -> None:
    url = f"https://api.telegram.org/bot{BOT_TOKEN}/sendMessage"

    async def scenario(h: Harness) -> None:
        ids = [h.enqueue("bir"), h.enqueue("iki"), h.enqueue("üç")]
        h.fake.script(
            "sendMessage",
            RuntimeError(f"connection to {url} failed"),
            NetworkError(f"httpx.ReadError: {url}"),
            BadRequest(f"bad {url}"),
            TelegramError(f"odd {url}"),
            ValueError(url),
        )
        h.fake.script("deleteMessage", TelegramError(f"cannot delete at {url}"))
        with capture_logs() as logs:
            for _ in range(12):
                h.service.wake_sender()
                await h.service.send_due()
            await h.say(f"/kill {PIN}")
            h.service._on_polling_error(NetworkError(f"poll {url}"))
            h.service._on_polling_error(Conflict(f"conflict {url}"))
            await asyncio.gather(*h.service._background)
        assert logs
        assert BOT_TOKEN not in repr(logs)
        assert PIN not in repr(logs)
        errors = {h.row(outbox_id).last_error for outbox_id in ids}
        assert errors == {"NetworkError", "BadRequest", "TelegramError"}
        with h.store.transaction() as tx:
            rows = tx.messages_created_between(0, 2**62)
        assert BOT_TOKEN not in repr(rows)

    drive(scenario)


# --- polling callbacks --------------------------------------------------------------------


def test_a_polling_conflict_raises_one_alarm_per_hour() -> None:
    async def scenario(h: Harness) -> None:
        with capture_logs() as logs:
            for _ in range(3):
                h.service._on_polling_error(Conflict("terminated by other getUpdates request"))
                await asyncio.gather(*h.service._background)
                h.clock.now_ms += 20 * MINUTE_MS
        assert h.outbox() == [(OutboxKind.ALARM, OutboxStatus.PENDING, CONFLICT_ALARM)]
        h.clock.now_ms += 1  # 60 min + 1 ms after the first one
        h.service._on_polling_error(Conflict("again"))
        await asyncio.gather(*h.service._background)
        assert [text for _kind, _status, text in h.outbox()] == [CONFLICT_ALARM, CONFLICT_ALARM]
        assert await h.service.send_due() is True  # the alarm woke the sender
        assert h.fake.sent_texts() == [CONFLICT_ALARM]
        events = [entry["event"] for entry in logs]
        assert events.count("telegram_polling_error") == 3
        assert {entry["error_type"] for entry in logs} == {"Conflict"}

    drive(scenario)


def test_the_conflict_alarm_is_retried_when_the_store_failed() -> None:
    async def scenario(h: Harness) -> None:
        real_store = h.service._store
        broken = Store(":memory:")
        broken.close()
        h.service._store = broken  # type: ignore[misc]
        with capture_logs() as logs:
            h.service._on_polling_error(Conflict("x"))
            await asyncio.gather(*h.service._background)
        failed = [entry for entry in logs if entry["event"] == "telegram_alarm_failed"]
        assert [(entry["code"], entry["error_type"]) for entry in failed] == [
            ("telegram_conflict", "StoreError")
        ]
        h.service._store = real_store  # type: ignore[misc]
        h.service._on_polling_error(Conflict("x"))
        await asyncio.gather(*h.service._background)
        assert [text for _kind, _status, text in h.outbox()] == [CONFLICT_ALARM]

    drive(scenario)


# --- updates python-telegram-bot cannot build ---------------------------------------------

NEAR_PIN = "246814"  # the PIN with its last digit mistyped: not a secret the redactor knows
STRANGER_TEXT = "merhaba, ben bir yabancıyım"
STUCK_ALARM = (
    "[ALARM] Telegram güncellemeleri okunamıyor: komutların (/duraklat ve /kill dahil) şu an "
    "İŞLENMEYEBİLİR; sinyal mesajları gelmeye devam eder. Sinyalleri durdurman gerekirse "
    "sunucuda: sudo systemctl stop propbot. Ayrıntı: journalctl -u propbot"
)


def skipped_alarm(count: int) -> str:
    return (
        f"[ALARM] Telegram'dan gelen {count} mesaj okunamadı ve atlandı (kütüphanenin "
        "tanımadığı bir mesaj biçimi). Az önce bir komut yazdıysan ve yanıt gelmediyse "
        "yeniden gönder."
    )


def unbuildable_update(text: str = f"/kill {NEAR_PIN}") -> dict[str, Any]:
    """A message whose chat has no type: python-telegram-bot raises while building it."""
    update = tg.private_update(text, sent_at_ms=NOW_MS)
    del update["message"]["chat"]["type"]
    return update


class Unparsed:
    """Records what `ResilientBot` reports and how long it asked to sleep."""

    def __init__(self) -> None:
        self.reports: list[tuple[int, bool]] = []
        self.sleeps: list[float] = []

    def __call__(self, skipped: int, stuck: bool) -> None:
        self.reports.append((skipped, stuck))

    async def sleep(self, seconds: float) -> None:
        self.sleeps.append(seconds)


def with_bot[T](scenario: Callable[[ResilientBot, FakeRequest, Unparsed], Awaitable[T]]) -> T:
    async def main() -> T:
        fake, seen = FakeRequest(), Unparsed()
        bot = ResilientBot(
            BOT_TOKEN, on_unparsed=seen, request=fake, get_updates_request=fake, sleep=seen.sleep
        )
        await bot.initialize()
        try:
            return await scenario(bot, fake, seen)
        finally:
            await bot.shutdown()

    return asyncio.run(main())


def test_the_bot_reports_each_answer_and_whether_it_was_a_conflict() -> None:
    # What the service's presence rules rest on: a result (empty or not) and a batch the
    # library cannot build are answers; a 409 is an answer of its own kind; a network
    # failure is no answer at all.
    async def main() -> list[bool]:
        fake, seen = FakeRequest(), Unparsed()
        answers: list[bool] = []
        bot = ResilientBot(
            BOT_TOKEN,
            on_unparsed=seen,
            on_answer=answers.append,
            request=fake,
            get_updates_request=fake,
            sleep=seen.sleep,
        )
        await bot.initialize()
        try:
            fake.script(
                "getUpdates",
                tg.ok([]),
                tg.ok([tg.private_update("/mod", sent_at_ms=NOW_MS)]),
                tg.ok([unbuildable_update()]),  # the poll that cannot be built ...
                tg.ok([unbuildable_update()]),  # ... and its salvage request
                NetworkError("no route"),
                tg.error_response(409, "Conflict: terminated by other getUpdates request"),
            )
            await bot.get_updates(timeout=10)
            await bot.get_updates(timeout=10)
            await bot.get_updates(timeout=10)
            with pytest.raises(NetworkError):
                await bot.get_updates(timeout=10)
            with pytest.raises(Conflict):
                await bot.get_updates(timeout=10)
        finally:
            await bot.shutdown()
        return answers

    assert asyncio.run(main()) == [False, False, False, True]


def test_the_update_used_as_unbuildable_really_breaks_the_library() -> None:
    with pytest.raises(TypeError, match="type"):
        Update.de_json(unbuildable_update(), None)


def test_an_unbuildable_update_is_skipped_and_the_updates_around_it_are_delivered() -> None:
    async def scenario(bot: ResilientBot, fake: FakeRequest, seen: Unparsed) -> None:
        first = tg.private_update("/duraklat", sent_at_ms=NOW_MS)
        bad = unbuildable_update()
        last = tg.private_update("/saglik", sent_at_ms=NOW_MS)
        batch = tg.ok([first, bad, last])
        fake.script("getUpdates", batch, batch)

        updates = await bot.get_updates(offset=0, timeout=10, allowed_updates=["message"])

        assert [update.update_id for update in updates] == [first["update_id"], last["update_id"]]
        assert [
            update.effective_message.text for update in updates if update.effective_message
        ] == [
            "/duraklat",
            "/saglik",
        ]
        assert seen.reports == [(1, False)]
        assert seen.sleeps == []  # worked around: no reason to slow down
        long_poll, salvage = fake.calls_to("getUpdates")
        assert long_poll == {"offset": 0, "timeout": 10, "allowed_updates": ["message"]}
        assert salvage == {
            "offset": 0,
            "limit": 100,
            "timeout": 0,
            "allowed_updates": ["message"],
        }

    with_bot(scenario)


def test_a_skipped_update_at_the_end_of_a_batch_is_confirmed_by_the_next_poll() -> None:
    # The updater confirms up to the last update it was GIVEN; the skipped one comes after.
    async def scenario(bot: ResilientBot, fake: FakeRequest, seen: Unparsed) -> None:
        good = tg.private_update("/durum", sent_at_ms=NOW_MS)
        bad = unbuildable_update()
        batch = tg.ok([good, bad])
        down = tg.error_response(502, "Bad Gateway")
        fake.script("getUpdates", batch, batch, down, tg.ok([]), tg.ok([]))

        (update,) = await bot.get_updates(offset=0, timeout=10)
        assert update.update_id == good["update_id"]
        updater_offset = good["update_id"] + 1  # what the library's updater asks for next
        after_bad = bad["update_id"] + 1

        with pytest.raises(NetworkError):  # the confirming poll fails: nothing is confirmed
            await bot.get_updates(offset=updater_offset, timeout=10)
        await bot.get_updates(offset=updater_offset, timeout=10)  # so the next one confirms
        await bot.get_updates(offset=updater_offset, timeout=10)  # done: the library's offset

        assert [params["offset"] for params in fake.calls_to("getUpdates")[2:]] == [
            after_bad,
            after_bad,
            updater_offset,
        ]
        assert seen.reports == [(1, False)]

    with_bot(scenario)


@pytest.mark.parametrize("offset", [None, 0])
def test_the_first_poll_after_a_salvage_confirms_even_without_an_offset(
    offset: int | None,
) -> None:
    async def scenario(bot: ResilientBot, fake: FakeRequest, seen: Unparsed) -> None:
        bad = unbuildable_update()
        fake.script("getUpdates", tg.ok([bad]), tg.ok([bad]), tg.ok([]))

        assert await bot.get_updates(offset=offset, timeout=10) == ()
        await bot.get_updates(offset=offset, timeout=10)

        assert fake.calls_to("getUpdates")[2]["offset"] == bad["update_id"] + 1

    with_bot(scenario)


def test_an_offset_that_is_already_further_on_is_left_alone() -> None:
    async def scenario(bot: ResilientBot, fake: FakeRequest, seen: Unparsed) -> None:
        bad = unbuildable_update()
        fake.script("getUpdates", tg.ok([bad]), tg.ok([bad]), tg.ok([]))
        await bot.get_updates(offset=0, timeout=10)

        await bot.get_updates(offset=bad["update_id"] + 6, timeout=10)

        assert fake.calls_to("getUpdates")[2]["offset"] == bad["update_id"] + 6

    with_bot(scenario)


def test_a_callback_that_raises_does_not_break_polling() -> None:
    async def main() -> None:
        fake = FakeRequest()

        def broken(_skipped: int, _stuck: bool) -> None:
            raise RuntimeError(f"callback saw /kill {NEAR_PIN}")

        bot = ResilientBot(BOT_TOKEN, on_unparsed=broken, request=fake, get_updates_request=fake)
        good, bad = tg.private_update("/durum", sent_at_ms=NOW_MS), unbuildable_update()
        fake.script("getUpdates", tg.ok([good, bad]), tg.ok([good, bad]))
        with capture_logs() as logs:
            (update,) = await bot.get_updates(offset=0, timeout=10)
        assert update.update_id == good["update_id"]
        assert [(entry["event"], entry["error_type"]) for entry in logs] == [
            ("telegram_unparsed_callback_failed", "RuntimeError")
        ]
        assert NEAR_PIN not in repr(logs)

    structlog.reset_defaults()
    asyncio.run(main())


def test_a_batch_of_nothing_but_unbuildable_updates_is_confirmed_too() -> None:
    async def scenario(bot: ResilientBot, fake: FakeRequest, seen: Unparsed) -> None:
        bad = [unbuildable_update(), unbuildable_update(STRANGER_TEXT)]
        fake.script("getUpdates", tg.ok(bad), tg.ok(bad), tg.ok([]))

        assert await bot.get_updates(offset=0, timeout=10) == ()
        assert seen.reports == [(2, False)]
        assert seen.sleeps == []

        await bot.get_updates(offset=0, timeout=10)
        assert fake.calls_to("getUpdates")[2]["offset"] == bad[1]["update_id"] + 1

    with_bot(scenario)


def test_nothing_of_an_unbuildable_batch_reaches_the_log() -> None:
    stream = io.StringIO()

    async def scenario(bot: ResilientBot, fake: FakeRequest, seen: Unparsed) -> None:
        batch = tg.ok(
            [
                tg.private_update(f"/kill {PIN}", sent_at_ms=NOW_MS),
                unbuildable_update(f"/kill {NEAR_PIN}"),
                tg.other_user_update(STRANGER_TEXT, sent_at_ms=NOW_MS),
            ]
        )
        fake.script("getUpdates", batch, batch)
        configure_logging(secrets=[BOT_TOKEN, PIN], level="DEBUG", stream=stream)
        try:
            assert len(await bot.get_updates(offset=0, timeout=10)) == 2
        finally:
            reset_logging()

    with_bot(scenario)

    logged = stream.getvalue()
    # The library does log its failure, but as a template: no update, no exception text.
    assert '"event": "Error while parsing updates! Received data was %r"' in logged
    for fragment in (PIN, NEAR_PIN, "/kill", STRANGER_TEXT, "yabancı", "missing 1 required"):
        assert fragment not in logged


@pytest.mark.parametrize(
    ("salvaged", "report", "delivered"),
    [
        (tg.ok({"unexpected": "shape"}), (0, True), 0),  # not a list of updates
        (tg.ok(True), (0, True), 0),
        (tg.ok([{"message": "no update_id here"}]), (1, True), 0),  # cannot be confirmed
        (tg.ok(["not even an object"]), (1, True), 0),
        (tg.ok([]), (0, True), 0),  # the failure is gone but nothing explains it
    ],
)
def test_a_failure_that_cannot_be_worked_around_slows_polling_down(
    salvaged: tg.Response, report: tuple[int, bool], delivered: int
) -> None:
    async def scenario(bot: ResilientBot, fake: FakeRequest, seen: Unparsed) -> None:
        fake.script("getUpdates", tg.ok([unbuildable_update()]), salvaged)

        assert len(await bot.get_updates(offset=0, timeout=10)) == delivered

        assert seen.reports == [report]
        assert seen.sleeps == [1.0]

    with_bot(scenario)


def test_polling_backs_off_up_to_thirty_seconds_and_recovers() -> None:
    async def scenario(bot: ResilientBot, fake: FakeRequest, seen: Unparsed) -> None:
        for _ in range(7):
            fake.script("getUpdates", tg.ok([unbuildable_update()]), tg.ok(True))
            assert await bot.get_updates(timeout=10) == ()
        assert seen.sleeps == [1.0, 2.0, 4.0, 8.0, 16.0, 30.0, 30.0]

        fake.script("getUpdates", tg.ok([]))  # a poll that works
        await bot.get_updates(timeout=10)
        fake.script("getUpdates", tg.ok([unbuildable_update()]), tg.ok(True))
        await bot.get_updates(timeout=10)
        assert seen.sleeps[-1] == 1.0  # counted from the start again

    with_bot(scenario)


def test_a_poll_that_fails_although_every_update_builds_still_delivers_them(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    async def broken(*_args: object, **_kwargs: object) -> tuple[Update, ...]:
        raise ValueError(f"library bug while handling /kill {NEAR_PIN}")

    monkeypatch.setattr(ExtBot, "get_updates", broken)

    async def scenario(bot: ResilientBot, fake: FakeRequest, seen: Unparsed) -> None:
        update = tg.private_update("/duraklat", sent_at_ms=NOW_MS)
        fake.script("getUpdates", tg.ok([update]))

        (delivered,) = await bot.get_updates(offset=0, timeout=10)

        assert delivered.update_id == update["update_id"]
        # Nothing skipped, and the next poll confirms the batch: it cannot come back.
        assert seen.reports == [(0, False)]
        assert seen.sleeps == []

    with_bot(scenario)


def test_updates_in_hand_are_never_held_back_by_the_back_off() -> None:
    # A stop during the wait would lose them: the updater's next call confirms them unhandled.
    async def scenario(bot: ResilientBot, fake: FakeRequest, seen: Unparsed) -> None:
        good = tg.private_update("/duraklat", sent_at_ms=NOW_MS)
        nameless = unbuildable_update()
        del nameless["update_id"]  # cannot be confirmed: it will come back
        fake.script("getUpdates", tg.ok([good, nameless]), tg.ok([good, nameless]))

        (delivered,) = await bot.get_updates(offset=0, timeout=10)

        assert delivered.update_id == good["update_id"]
        assert seen.reports == [(1, True)]
        assert seen.sleeps == []  # handed out at once

        # It comes back alone: now there is nothing to hand out, and polling slows down.
        fake.script("getUpdates", tg.ok([nameless]), tg.ok([nameless]))
        assert await bot.get_updates(offset=good["update_id"] + 1, timeout=10) == ()
        assert seen.sleeps == [1.0]

    with_bot(scenario)


@pytest.mark.parametrize("timeout", [0, dt.timedelta(seconds=0), None])
def test_a_short_poll_never_waits_even_when_polling_is_stuck(
    timeout: int | dt.timedelta | None,
) -> None:
    # The updater's last call at shutdown is a short poll: a back-off there (up to 30 s)
    # outlasts the service's shutdown grace.
    async def scenario(bot: ResilientBot, fake: FakeRequest, seen: Unparsed) -> None:
        for _ in range(4):
            fake.script("getUpdates", tg.ok([unbuildable_update()]), tg.ok(True))
            assert await bot.get_updates(offset=0, timeout=timeout) == ()

        assert seen.reports == [(0, True)] * 4
        assert seen.sleeps == []

    with_bot(scenario)


def test_a_stuck_service_still_stops_at_once() -> None:
    # The state whose alarm tells the user to stop the service.
    async def scenario(h: Harness) -> None:
        async def never_a_list(call: tg.Call) -> tg.Response:
            if call.params.get("timeout"):
                return tg.ok([unbuildable_update()])  # the long poll: a batch that fails
            return tg.ok(True)  # the salvage and the clean-up poll: nothing usable

        h.fake.script("getUpdates", *([never_a_list] * 50))
        stop = asyncio.Event()
        task = asyncio.create_task(h.service.run(stop))
        await until(lambda: STUCK_ALARM in h.fake.sent_texts())  # polling is backing off now
        with capture_logs() as logs:
            stop.set()
            h.service.wake_sender()
            # Well below the 2 s the clean-up poll would wait if it backed off as well.
            await asyncio.wait_for(task, 1.5)
        assert "telegram_stopped" in [entry["event"] for entry in logs]
        assert h.fake.sent_texts().count(STUCK_ALARM) == 1

    drive(scenario, polling=True)


@pytest.mark.parametrize("failing_call", [1, 2], ids=["the_poll", "the_salvage_request"])
def test_telegram_errors_are_left_to_the_updater(failing_call: int) -> None:
    async def scenario(bot: ResilientBot, fake: FakeRequest, seen: Unparsed) -> None:
        outcomes: list[tg.Scripted] = [tg.ok([unbuildable_update()])] * (failing_call - 1)
        fake.script("getUpdates", *outcomes, tg.error_response(409, "Conflict"))

        with pytest.raises(Conflict):
            await bot.get_updates(offset=0, timeout=10)

        assert len(fake.calls_to("getUpdates")) == failing_call
        assert seen.reports == []
        assert seen.sleeps == []

    with_bot(scenario)


def test_commands_around_an_unbuildable_update_are_handled_and_the_user_is_told() -> None:
    # Before: the updater asked for the same batch for ever, no command was handled.
    async def scenario(h: Harness) -> None:
        pause = tg.private_update("/duraklat", sent_at_ms=NOW_MS)
        bad = unbuildable_update()
        mode = tg.private_update("/mod", sent_at_ms=NOW_MS)
        batch = tg.ok([pause, bad, mode])
        h.fake.script("getUpdates", batch, batch)
        stop = asyncio.Event()
        task = asyncio.create_task(h.service.run(stop))
        try:
            await until(lambda: len(h.fake.sent_texts()) == 3)
            await until(lambda: len(h.fake.calls_to("getUpdates")) == 3)
            await asyncio.sleep(0.05)  # a spinning loop would add calls here
            polls = h.fake.calls_to("getUpdates")
            assert len(polls) == 3  # the poll that failed, the salvage, one waiting long poll
            assert polls[2]["offset"] == mode["update_id"] + 1
            assert h.flag(StateKey.PAUSED) == "1"
            texts = h.fake.sent_texts()
            assert skipped_alarm(1) in texts
            assert sum(text.startswith("Mod: signal") for text in texts) == 1
            assert NEAR_PIN not in repr(texts)
        finally:
            stop.set()
            h.service.wake_sender()
            await asyncio.wait_for(task, 5)
        assert h.fake.chat_ids() == {CHAT_ID}

    drive(scenario, polling=True)


def test_unbuildable_updates_raise_one_alarm_of_each_kind_per_hour() -> None:
    async def scenario(h: Harness) -> None:
        with capture_logs() as logs:
            # Minutes 0, 15, 30 and 45: the first of each kind raises an alarm.
            for report in [(2, False), (1, False), (0, True), (1, True)]:
                h.service._on_unparsed_updates(*report)
                await asyncio.gather(*h.service._background)
                h.clock.now_ms += 15 * MINUTE_MS
        assert h.outbox() == [
            (OutboxKind.ALARM, OutboxStatus.PENDING, skipped_alarm(2)),
            (OutboxKind.ALARM, OutboxStatus.PENDING, STUCK_ALARM),
        ]
        h.clock.now_ms += 30 * MINUTE_MS  # minute 90: an hour after the stuck alarm
        h.service._on_unparsed_updates(3, True)
        await asyncio.gather(*h.service._background)
        assert [text for _kind, _status, text in h.outbox()][2:] == [skipped_alarm(3), STUCK_ALARM]
        unparsed = [entry for entry in logs if entry["event"] == "telegram_updates_unparsed"]
        assert [(entry["skipped"], entry["stuck"]) for entry in unparsed] == [
            (2, False),
            (1, False),
            (0, True),
            (1, True),
        ]

    drive(scenario)


def test_other_polling_errors_are_only_logged_and_throttled() -> None:
    async def scenario(h: Harness) -> None:
        with capture_logs() as logs:
            for _ in range(4):
                h.service._on_polling_error(NetworkError("bad gateway"))
            h.clock.now_ms += 10_000
            h.service._on_polling_error(Forbidden("?"))
        assert not h.service._background
        assert h.outbox() == []
        assert logs == [
            {
                "event": "telegram_polling_error",
                "error_type": "NetworkError",
                "suppressed": 0,
                "log_level": "warning",
            },
            {
                "event": "telegram_polling_error",
                "error_type": "Forbidden",
                "suppressed": 3,
                "log_level": "warning",
            },
        ]
        assert await h.service.send_due() is False  # none of it is fatal

    drive(scenario)


def test_an_invalid_token_reported_by_polling_is_fatal() -> None:
    async def scenario(h: Harness) -> TelegramFatalError:
        h.service._on_polling_error(InvalidToken(f"The token `{BOT_TOKEN}` was rejected"))
        with pytest.raises(TelegramFatalError) as caught:
            await h.service.send_due()
        return caught.value

    _assert_token_free(drive(scenario))


# --- run(): the loop itself ---------------------------------------------------------------


def test_run_polls_answers_sends_and_stops_cleanly() -> None:
    async def scenario(h: Harness) -> None:
        stop = asyncio.Event()
        task = asyncio.create_task(h.service.run(stop))
        try:
            # A command arrives through long polling and is answered.
            h.fake.push_update(tg.private_update("/mod", sent_at_ms=NOW_MS))
            await until(lambda: len(h.fake.sent_texts()) == 1)
            assert h.fake.sent_texts()[0].startswith("Mod: signal")
            # A foreign chat gets nothing.
            h.fake.push_update(tg.other_user_update("/mod", sent_at_ms=NOW_MS))
            # An outbox row is delivered after a wake-up.
            outbox_id = h.enqueue("alarm", kind=OutboxKind.ALARM)
            h.service.wake_sender()
            await until(lambda: h.row(outbox_id).status is OutboxStatus.SENT)
            assert h.app.running
        finally:
            stop.set()
            h.service.wake_sender()
            await asyncio.wait_for(task, 5)
        assert h.fake.sent_texts() == [h.fake.sent_texts()[0], "alarm"]
        assert h.fake.chat_ids() == {CHAT_ID}
        assert not h.app.running
        polls = h.fake.calls_to("getUpdates")
        assert polls
        assert all(params["allowed_updates"] == ["message"] for params in polls)
        # Pending updates are never dropped: old commands are judged by the age rule instead.
        assert h.fake.calls_to("deleteWebhook") == [{"drop_pending_updates": False}]

    drive(scenario, polling=True)


def test_a_service_that_is_already_stopping_does_not_start_polling() -> None:
    # E.g. a copy that is shut down right after it started: polling would take updates away
    # from the bot that keeps running (and give it a Conflict).
    async def scenario(h: Harness) -> None:
        stop = asyncio.Event()

        async def stop_while_connecting(_call: tg.Call) -> None:
            stop.set()

        h.fake.script("getMe", stop_while_connecting)

        await asyncio.wait_for(h.service.run(stop), 5)

        assert [call.endpoint for call in h.fake.calls] == ["getMe"]

    drive(scenario, polling=True)


def test_run_without_polling_only_sends() -> None:
    async def scenario(h: Harness) -> None:
        stop = asyncio.Event()
        task = asyncio.create_task(h.service.run(stop))
        outbox_id = h.enqueue("bir")
        h.service.wake_sender()
        await until(lambda: h.row(outbox_id).status is OutboxStatus.SENT)
        # Updates can still be fed by hand while the loop runs.
        await h.say("/mod")
        stop.set()
        h.service.wake_sender()
        await asyncio.wait_for(task, 5)
        assert {call.endpoint for call in h.fake.calls} == {"getMe", "sendMessage"}

    drive(scenario)


def test_the_idle_poll_finds_rows_nobody_announced(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(telegram_module, "_IDLE_POLL_SECONDS", 0.01)

    async def scenario(h: Harness) -> None:
        stop = asyncio.Event()
        task = asyncio.create_task(h.service.run(stop))
        await until(lambda: len(h.fake.calls) >= 1)
        outbox_id = h.enqueue("sessiz")  # no wake_sender()
        await until(lambda: h.row(outbox_id).status is OutboxStatus.SENT)
        stop.set()
        await asyncio.wait_for(task, 5)

    drive(scenario)


def test_a_wake_up_cuts_the_idle_wait_short(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(telegram_module, "_IDLE_POLL_SECONDS", 3_600.0)

    async def scenario(h: Harness) -> None:
        stop = asyncio.Event()
        task = asyncio.create_task(h.service.run(stop))
        await until(lambda: len(h.fake.calls) >= 1)
        await asyncio.sleep(0.05)  # let the loop reach its (hour-long) idle wait
        outbox_id = h.enqueue("uyandır")
        h.service.wake_sender()
        await until(lambda: h.row(outbox_id).status is OutboxStatus.SENT)
        stop.set()  # `stop` alone must end the wait too
        await asyncio.wait_for(task, 5)

    drive(scenario)


def test_run_keeps_retrying_while_telegram_is_down_at_start_up(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(telegram_module, "_IDLE_POLL_SECONDS", 0.01)

    async def scenario(h: Harness) -> None:
        down = caused_by(NetworkError("httpx.ConnectError"), httpx.ConnectError("no route"))
        h.fake.script("getMe", down, down)
        h.fake.script("deleteWebhook", down)
        outbox_id = h.enqueue("bekleyen")
        stop = asyncio.Event()
        task = asyncio.create_task(h.service.run(stop))
        await until(lambda: len(h.fake.calls_to("getMe")) == 1)
        assert not task.done()  # a dead network does not end the task
        for _ in range(8):  # move the clock past each back-off
            h.clock.now_ms += 30_000
            await asyncio.sleep(0.03)
        await until(lambda: h.row(outbox_id).status is OutboxStatus.SENT)
        h.clock.now_ms += 30_000
        await until(lambda: len(h.fake.calls_to("getUpdates")) >= 1)  # polling started at last
        assert len(h.fake.calls_to("deleteWebhook")) == 2
        stop.set()
        await asyncio.wait_for(task, 5)

    drive(scenario, polling=True)


async def polling_for(h: Harness, stop: asyncio.Event) -> asyncio.Task[None]:
    """Start `run` and wait until the first long poll is open (it then waits for an update)."""
    task = asyncio.create_task(h.service.run(stop))
    await until(lambda: len(h.fake.calls_to("getUpdates")) >= 1)
    return task


async def answered(h: Harness, command: str = "/mod") -> None:
    """Let Telegram answer the open long poll (with a command) and wait for the reply."""
    replies = len(h.fake.calls_to("sendMessage"))
    h.fake.push_update(tg.private_update(command, sent_at_ms=h.clock.now_ms))
    await until(lambda: len(h.fake.calls_to("sendMessage")) > replies)


async def one_conflict(h: Harness) -> None:
    """Through the real polling loop: a command is answered, then one poll meets a 409."""
    polls = len(h.fake.calls_to("getUpdates"))
    h.fake.script("getUpdates", CONFLICT_409)
    await answered(h)
    await until(lambda: len(h.fake.calls_to("getUpdates")) >= polls + 2, timeout=10)


async def loop_turn(h: Harness) -> None:
    """Let `run` go round once more (it idles for a second when nothing wakes it)."""
    h.service.wake_sender()
    await asyncio.sleep(0.2)


def arrive(h: Harness) -> None:
    """Telegram answers a getUpdates of this copy (what `ResilientBot` reports)."""
    h.service._note_answer()


def conflict(h: Harness) -> None:
    """One 409 as the service sees it: the answer is noted, then the updater's callback."""
    h.service._note_answer(True)
    h.service._on_polling_error(Conflict("terminated by other getUpdates request"))


# --- two processes with one token: who yields (user decision 53) ----------------------------


def test_presence_is_timed_on_the_boot_clock_unless_a_test_says_otherwise() -> None:
    # Not on the wall clock: a time correction must not turn a newcomer into the incumbent
    # (or the other way round), and a laptop's sleep must count as silence.
    assert inspect.signature(TelegramService).parameters["uptime"].default is boot_clock_ms


def test_the_numbers_of_the_arrival_rule() -> None:
    # Literal values: a window of 0 or of 60 ms would switch the rule off without a sound,
    # ten minutes would make a long-running service yield to a newcomer.
    assert telegram_module.ARRIVAL_WINDOW_MS == 60_000
    assert telegram_module.STANDBY_CONFLICTS == 2
    assert telegram_module.ARRIVAL_HOLD_MS == 3_000
    assert telegram_module.CONTEST_HOLD_MS == 10_000


@pytest.mark.parametrize(
    ("first_at_ms", "second_at_ms"),
    [(0, 0), (1_000, 3_500), (0, 60_000), (59_999, 60_000), (60_000, 60_000)],
)
def test_two_conflicts_within_a_minute_of_the_arrival_mean_standby(
    first_at_ms: int, second_at_ms: int
) -> None:
    async def scenario(h: Harness) -> None:
        arrived = h.clock.now_ms
        arrive(h)
        h.clock.now_ms = arrived + first_at_ms
        conflict(h)
        assert not h.service._standby_due
        h.clock.now_ms = arrived + second_at_ms
        conflict(h)
        assert h.service._standby_due
        await asyncio.gather(*h.service._background)
        assert h.outbox() == []  # the incumbent's alarm is not raised by the newcomer

    drive(scenario)


@pytest.mark.parametrize("gap_ms", [1_500, 20_000, 60_000])
def test_a_conflict_after_the_first_minute_is_the_other_process_arriving(gap_ms: int) -> None:
    # Telegram keeps answering this copy (a long poll returns every few seconds): it has
    # been here all along, so a Conflict means someone else has just come.
    async def scenario(h: Harness) -> None:
        arrived = h.clock.now_ms
        arrive(h)
        while h.clock.now_ms + gap_ms <= arrived + 60_000:
            h.clock.now_ms += gap_ms
            arrive(h)
        h.clock.now_ms = max(h.clock.now_ms, arrived + 60_001 - gap_ms) + gap_ms
        assert h.clock.now_ms > arrived + 60_000
        for _ in range(5):
            conflict(h)
            h.clock.now_ms += 1_000
        await asyncio.gather(*h.service._background)
        assert not h.service._standby_due
        assert h.outbox() == [(OutboxKind.ALARM, OutboxStatus.PENDING, CONFLICT_ALARM)]

    drive(scenario)


def test_a_single_conflict_after_the_arrival_is_tolerated_in_silence() -> None:
    # A getUpdates typed by hand, or a request of the previous process that Telegram had
    # not dropped yet: another poller would repeat itself.
    async def scenario(h: Harness) -> None:
        arrive(h)
        with capture_logs() as logs:
            conflict(h)
            h.clock.now_ms += 70_000  # the window passes; Telegram kept answering
            h.service._last_answer_ms = h.clock.now_ms - 5_000
            conflict(h)  # now as the incumbent: counted afresh, this one raises the alarm
        await asyncio.gather(*h.service._background)
        assert not h.service._standby_due
        assert [text for _kind, _status, text in h.outbox()] == [CONFLICT_ALARM]
        events = [entry["event"] for entry in logs]
        assert events.count("telegram_conflict_on_arrival") == 1

    drive(scenario)


def test_a_copy_back_from_a_long_silence_is_the_one_arriving() -> None:
    # It polled for ten minutes, then its network was gone (or the laptop slept) for two.
    # Another copy may have started meanwhile and seen no Conflict: the one that returns
    # yields, although its process is the older one.
    async def scenario(h: Harness) -> None:
        arrive(h)
        for _ in range(60):
            h.clock.now_ms += 10_000
            arrive(h)
        h.clock.now_ms += 120_000
        conflict(h)
        h.clock.now_ms += 2_500
        conflict(h)
        assert h.service._standby_due

    drive(scenario)


def test_a_copy_that_was_never_answered_is_a_newcomer_however_long_it_tried() -> None:
    async def scenario(h: Harness) -> None:
        for _ in range(4):
            h.service._on_polling_error(NetworkError("no route"))
            h.clock.now_ms += 30_000
        conflict(h)  # the first answer it ever got
        h.clock.now_ms += 2_500
        conflict(h)
        assert h.service._standby_due

    drive(scenario)


def test_conflicts_of_an_earlier_arrival_do_not_count_for_the_next() -> None:
    async def scenario(h: Harness) -> None:
        arrive(h)
        conflict(h)
        h.clock.now_ms += 10 * MINUTE_MS  # silent, then back
        conflict(h)
        assert not h.service._standby_due  # one Conflict of THIS arrival
        conflict(h)
        assert h.service._standby_due

    drive(scenario)


def test_nothing_but_conflicts_for_two_minutes_puts_the_copy_that_was_first_on_standby() -> None:
    # The other process does not yield (a foreign program with the token, a webhook): the
    # commands are shared at random, so this copy cannot count on receiving /kill either.
    async def scenario(h: Harness) -> None:
        arrive(h)
        for _ in range(10):  # five minutes of results, half a minute apart
            h.clock.now_ms += 30_000
            arrive(h)
        h.clock.now_ms += 1_000  # the last result was a second ago; now every poll is a Conflict
        first = h.clock.now_ms
        for after_ms in (0, 7_000, 31_000, 61_000, 91_000, 120_000):
            h.clock.now_ms = first + after_ms
            conflict(h)
            assert not h.service._standby_due, after_ms
        h.clock.now_ms = first + 120_001  # more than two minutes after the FIRST Conflict
        conflict(h)
        assert h.service._standby_due
        await asyncio.gather(*h.service._background)
        assert [text for _kind, _status, text in h.outbox()] == [CONFLICT_ALARM]

    drive(scenario)


def test_the_real_polling_loop_tells_conflicts_from_results() -> None:
    # Through `ResilientBot` and the library's retry loop: results for a minute and a half
    # (the copy has been there), then only Conflicts, 50 s apart on the clock. The fourth
    # one comes 150 s after the first.
    async def scenario(h: Harness) -> None:
        async def result(_call: tg.Call) -> tg.Response:
            h.clock.now_ms += 30_000
            return tg.ok([])

        async def conflicting(_call: tg.Call) -> tg.Response:
            h.clock.now_ms += 50_000
            return CONFLICT_409

        h.fake.script("getUpdates", *([result] * 3), *([conflicting] * 4))
        stop = asyncio.Event()
        task = asyncio.create_task(h.service.run(stop))
        try:
            await until(lambda: CONFLICT_ALARM in h.fake.sent_texts(), timeout=15)
            assert h.flag(StateKey.STANDBY) is None  # an alarm first: it was here first
            await until(lambda: STANDBY_ALARM in h.fake.sent_texts(), timeout=15)
            assert h.flag(StateKey.STANDBY) == "1"
            assert len(h.fake.calls_to("getUpdates")) == 8  # 3 + 4 and the library's last one
        finally:
            stop.set()
            await asyncio.wait_for(task, 5)

    drive(scenario, polling=True)


def test_a_result_between_conflicts_starts_the_two_minutes_again() -> None:
    # Telegram still hands this copy its updates now and then: it keeps the bot (and the
    # alarm), as long as it does not go two minutes on Conflicts alone.
    async def scenario(h: Harness) -> None:
        arrive(h)
        for _ in range(10):
            h.clock.now_ms += 30_000
            arrive(h)
        for _ in range(6):
            h.clock.now_ms += 50_000
            conflict(h)
            h.clock.now_ms += 50_000
            conflict(h)
            h.clock.now_ms += 5_000
            arrive(h)  # a result
        assert not h.service._standby_due

    drive(scenario)


def test_the_two_minutes_of_conflicts_are_a_literal_number() -> None:
    assert telegram_module.PERSISTENT_CONFLICT_MS == 120_000


def test_an_empty_long_poll_is_an_answer_and_keeps_the_copy_that_was_first() -> None:
    # A quiet bot: every ten seconds the long poll returns nothing. That is still Telegram
    # answering, and the only thing that tells a long-running copy from one that returns.
    async def scenario(h: Harness) -> None:
        async def nothing(_call: tg.Call) -> tg.Response:
            h.clock.now_ms += 10_000
            return tg.ok([])

        h.fake.script("getUpdates", *([nothing] * 9), CONFLICT_409, CONFLICT_409, CONFLICT_409)
        stop = asyncio.Event()
        task = asyncio.create_task(h.service.run(stop))
        try:
            await until(lambda: CONFLICT_ALARM in h.fake.sent_texts(), timeout=15)
            await until(lambda: len(h.fake.calls_to("getUpdates")) >= 13, timeout=15)
            assert not h.service._standby
            assert h.flag(StateKey.STANDBY) is None
        finally:
            stop.set()
            await asyncio.wait_for(task, 5)
        assert STANDBY_ALARM not in h.fake.sent_texts()

    drive(scenario, polling=True)


@pytest.mark.parametrize("mark", ["1", "2", "yes"])
def test_empty_long_polls_alone_lift_an_inherited_standby(mark: str) -> None:
    # Whatever the earlier process left: on standby, restarted once already ("2": a second
    # restart during the minute), or text nobody can read. None of them may stay for ever.
    async def scenario(h: Harness) -> None:
        with h.store.transaction() as tx:
            tx.set_state(StateKey.STANDBY, mark, h.clock.now_ms)

        async def nothing(_call: tg.Call) -> tg.Response:
            h.clock.now_ms += 10_000
            return tg.ok([])

        h.fake.script("getUpdates", *([nothing] * 8))
        stop = asyncio.Event()
        task = asyncio.create_task(h.service.run(stop))
        try:
            await until(lambda: ACTIVE_AGAIN in h.fake.sent_texts(), timeout=10)
            assert h.flag(StateKey.STANDBY) is None
        finally:
            stop.set()
            await asyncio.wait_for(task, 5)

    drive(scenario, polling=True)


def test_a_batch_the_library_cannot_build_is_an_answer_too() -> None:
    # The first thing Telegram says is a batch with an update the library rejects. The copy
    # has arrived all the same: a command salvaged from that batch is held like any other
    # command fetched right after an arrival (it may have been meant for another process).
    async def scenario(h: Harness) -> None:
        batch = [unbuildable_update(), tg.private_update("/duraklat", sent_at_ms=h.clock.now_ms)]
        h.fake.script("getUpdates", tg.ok(batch), tg.ok(batch))
        stop = asyncio.Event()
        task = asyncio.create_task(h.service.run(stop))
        try:
            await until(lambda: len(h.fake.calls_to("getUpdates")) >= 3)
            h.clock.now_ms += 2_999
            await asyncio.sleep(0.3)
            assert h.flag(StateKey.PAUSED) is None  # held: three seconds have not passed
            h.clock.now_ms += 1
            await until(lambda: h.flag(StateKey.PAUSED) == "1")
        finally:
            stop.set()
            await asyncio.wait_for(task, 5)

    drive(scenario, polling=True, arrival_hold_ms=3_000, contest_hold_ms=10_000)


def test_a_conflict_reported_without_any_answer_is_the_incumbents_alarm() -> None:
    # The callback alone (as older tests drive it): no arrival is known, nothing to yield.
    async def scenario(h: Harness) -> None:
        for _ in range(3):
            h.service._on_polling_error(Conflict("x"))
        await asyncio.gather(*h.service._background)
        assert not h.service._standby_due
        assert [text for _kind, _status, text in h.outbox()] == [CONFLICT_ALARM]

    drive(scenario)


# --- standby through the real polling loop ---------------------------------------------------


def test_another_poller_at_the_start_puts_this_copy_on_standby_and_it_stays_up() -> None:
    async def scenario(h: Harness) -> None:
        h.fake.script("getUpdates", CONFLICT_409, CONFLICT_409)
        stop = asyncio.Event()
        with capture_logs() as logs:
            task = asyncio.create_task(h.service.run(stop))
            try:
                await until(lambda: STANDBY_ALARM in h.fake.sent_texts(), timeout=10)
                assert h.flag(StateKey.STANDBY) == "1"
                # Two long polls met a Conflict; the library's own stop sent one last short
                # request, which confirms nothing (offset unchanged). No poll after that.
                polls = [call["timeout"] for call in h.fake.calls_to("getUpdates")]
                assert polls == [10, 10, 0]
                assert {call["offset"] for call in h.fake.calls_to("getUpdates")} == {0}
                # Still up: the sender delivers what the processor writes (an exit, say).
                later = h.enqueue("[ÇIKIŞ] bekleme sırasında gelen çıkış", kind=OutboxKind.SIGNAL)
                h.service.wake_sender()
                await until(lambda: h.row(later).status is OutboxStatus.SENT)
                await asyncio.sleep(1.2)  # longer than the library's retry sleep
                assert not task.done()
                assert h.app.running
                assert len(h.fake.calls_to("getUpdates")) == 3
            finally:
                stop.set()
                await asyncio.wait_for(task, 5)
        assert h.outbox()[0] == (OutboxKind.ALARM, OutboxStatus.SENT, STANDBY_ALARM)
        assert h.fake.sent_texts().count(STANDBY_ALARM) == 1
        events = [entry["event"] for entry in logs]
        assert events.count("telegram_standby") == 1
        assert "telegram_polling_stopped" not in events  # not mistaken for a dead poller
        assert BOT_TOKEN not in repr(logs)
        assert "terminated by other" not in repr(logs)  # the library's text is not logged

    drive(scenario, polling=True)


def test_one_conflict_at_the_start_changes_nothing() -> None:
    async def scenario(h: Harness) -> None:
        h.fake.script("getUpdates", CONFLICT_409)
        stop = asyncio.Event()
        task = asyncio.create_task(h.service.run(stop))
        try:
            await until(lambda: len(h.fake.calls_to("getUpdates")) >= 2, timeout=10)
            await answered(h)
            # The command is carried out as usual (no standby note, no refusal).
            (reply,) = h.fake.sent_texts()
            assert reply.startswith("Mod: signal")
            assert h.flag(StateKey.STANDBY) is None
            assert not task.done()
        finally:
            stop.set()
            await asyncio.wait_for(task, 5)
        assert STANDBY_ALARM not in h.fake.sent_texts()
        assert CONFLICT_ALARM not in h.fake.sent_texts()

    drive(scenario, polling=True)


def test_a_conflict_long_after_the_arrival_reaches_the_user_and_polling_goes_on() -> None:
    # This copy has been answering for more than a minute: the OTHER one is the newcomer.
    async def scenario(h: Harness) -> None:
        stop = asyncio.Event()
        task = await polling_for(h, stop)
        try:
            for _ in range(3):  # Telegram keeps answering: 0 s, 30 s, 60 s, 90 s
                await answered(h)
                h.clock.now_ms += 30_000
            h.fake.script("getUpdates", CONFLICT_409, CONFLICT_409, CONFLICT_409)
            await answered(h)
            await until(lambda: CONFLICT_ALARM in h.fake.sent_texts(), timeout=10)
            await until(lambda: len(h.fake.calls_to("getUpdates")) >= 8, timeout=10)
            assert not task.done()
            assert h.flag(StateKey.STANDBY) is None
            # Still the bot's poller: a command typed afterwards is answered.
            await answered(h)
        finally:
            stop.set()
            await asyncio.wait_for(task, 5)
        assert STANDBY_ALARM not in h.fake.sent_texts()

    drive(scenario, polling=True)


# --- standby: commands that reached the wrong copy -------------------------------------------


def test_a_command_fetched_before_the_conflicts_is_not_carried_out_by_the_newcomer() -> None:
    # The update was waiting at Telegram for whoever polls next, and that was this copy.
    # It is held until the other process has had time to show itself.
    async def scenario(h: Harness) -> None:
        h.fake.push_update(tg.private_update("/devam", sent_at_ms=h.clock.now_ms))
        with h.store.transaction() as tx:
            tx.set_state(StateKey.PAUSED, "1", h.clock.now_ms)
        stop = asyncio.Event()
        task = asyncio.create_task(h.service.run(stop))
        try:
            await until(lambda: len(h.fake.calls_to("getUpdates")) >= 2)
            await asyncio.sleep(0.3)
            assert h.fake.sent_texts() == []  # held: three seconds have not passed
            h.fake.script("getUpdates", CONFLICT_409, CONFLICT_409)
            h.fake.push_update(tg.private_update("/mod", sent_at_ms=h.clock.now_ms))
            await until(lambda: STANDBY_ALARM in h.fake.sent_texts(), timeout=10)
            await until(lambda: h.fake.sent_texts().count(STANDBY_REFUSAL) == 2)
        finally:
            stop.set()
            await asyncio.wait_for(task, 5)
        assert h.flag(StateKey.PAUSED) == "1"  # /devam did not lift the pause
        assert sorted(h.fake.sent_texts()) == sorted([STANDBY_REFUSAL] * 2 + [STANDBY_ALARM])

    drive(scenario, polling=True, arrival_hold_ms=3_000, contest_hold_ms=10_000)


def test_kill_that_reached_the_copy_on_standby_is_carried_out_there_and_says_so() -> None:
    # /kill and /duraklat can only take risk away: never refused. The reply says that the
    # process that forwards the signals did not get the command.
    async def scenario(h: Harness) -> None:
        h.fake.push_update(tg.private_update(f"/kill {PIN}", sent_at_ms=h.clock.now_ms))
        stop = asyncio.Event()
        task = asyncio.create_task(h.service.run(stop))
        try:
            await until(lambda: len(h.fake.calls_to("getUpdates")) >= 2)
            h.fake.script("getUpdates", CONFLICT_409, CONFLICT_409)
            h.fake.push_update(tg.private_update("/duraklat", sent_at_ms=h.clock.now_ms))
            await until(lambda: len(h.fake.sent_texts()) >= 3, timeout=10)
        finally:
            stop.set()
            await asyncio.wait_for(task, 5)
        assert h.flag(StateKey.KILLED) == "1"
        assert h.flag(StateKey.PAUSED) == "1"
        replies = [text for text in h.fake.sent_texts() if text != STANDBY_ALARM]
        assert len(replies) == 2
        assert replies[0].startswith("KILL etkin: giriş sinyalleri kesildi.")
        assert replies[1].startswith("Duraklatıldı: yeni GİRİŞ sinyalleri iletilmez.")
        assert all(reply.endswith(STANDBY_NOTE) for reply in replies)
        assert len(h.fake.calls_to("deleteMessage")) == 1  # the message with the PIN
        assert PIN not in repr(h.fake.sent_texts())

    drive(scenario, polling=True, arrival_hold_ms=3_000, contest_hold_ms=10_000)


def test_a_command_is_held_for_three_seconds_after_an_arrival_and_then_carried_out() -> None:
    async def scenario(h: Harness) -> None:
        h.fake.push_update(tg.private_update("/duraklat", sent_at_ms=h.clock.now_ms))
        stop = asyncio.Event()
        task = asyncio.create_task(h.service.run(stop))
        try:
            await until(lambda: len(h.fake.calls_to("getUpdates")) >= 2)
            h.clock.now_ms += 2_999
            await asyncio.sleep(0.3)
            assert h.fake.sent_texts() == []
            assert h.flag(StateKey.PAUSED) is None
            h.clock.now_ms += 1
            await until(lambda: len(h.fake.sent_texts()) == 1)
            assert h.flag(StateKey.PAUSED) == "1"
            assert STANDBY_NOTE not in h.fake.sent_texts()[0]
            # Later commands are not held: the arrival is over.
            await answered(h)
        finally:
            stop.set()
            await asyncio.wait_for(task, 5)

    drive(scenario, polling=True, arrival_hold_ms=3_000, contest_hold_ms=10_000)


def test_after_one_conflict_a_command_waits_ten_seconds_for_the_second() -> None:
    async def scenario(h: Harness) -> None:
        h.fake.script("getUpdates", CONFLICT_409)
        stop = asyncio.Event()
        task = asyncio.create_task(h.service.run(stop))
        try:
            await until(lambda: len(h.fake.calls_to("getUpdates")) >= 2, timeout=10)
            h.fake.push_update(tg.private_update("/duraklat", sent_at_ms=h.clock.now_ms))
            await until(lambda: len(h.fake.calls_to("getUpdates")) >= 3)
            h.clock.now_ms += 9_999  # well past the three seconds of the arrival
            await asyncio.sleep(0.3)
            assert h.fake.sent_texts() == []
            h.clock.now_ms += 1
            await until(lambda: len(h.fake.sent_texts()) == 1)
            assert h.fake.sent_texts()[0].startswith("Duraklatıldı")
            assert STANDBY_NOTE not in h.fake.sent_texts()[0]  # the Conflict was a stray one
        finally:
            stop.set()
            await asyncio.wait_for(task, 5)

    drive(scenario, polling=True, arrival_hold_ms=3_000, contest_hold_ms=10_000)


def test_a_held_command_does_not_keep_the_service_from_stopping() -> None:
    async def scenario(h: Harness) -> None:
        h.fake.push_update(tg.private_update("/duraklat", sent_at_ms=h.clock.now_ms))
        stop = asyncio.Event()
        task = asyncio.create_task(h.service.run(stop))
        await until(lambda: len(h.fake.calls_to("getUpdates")) >= 2)
        stop.set()
        await asyncio.wait_for(task, 5)
        # Answered on the way out, not lost; and carried out as usual: no Conflict was seen.
        (reply,) = h.fake.sent_texts()
        assert reply.startswith("Duraklatıldı")
        assert STANDBY_NOTE not in reply
        assert h.flag(StateKey.PAUSED) == "1"

    drive(scenario, polling=True, arrival_hold_ms=3_000, contest_hold_ms=10_000)


def test_a_command_held_after_a_conflict_is_answered_as_from_standby_when_the_copy_stops() -> None:
    # One Conflict was seen and the second has not come yet: the copy does not know whether
    # it holds the bot. Stopped in that state, it must not answer "KILL etkin" as if it did.
    async def scenario(h: Harness) -> None:
        h.fake.script("getUpdates", CONFLICT_409)
        stop = asyncio.Event()
        task = asyncio.create_task(h.service.run(stop))
        await until(lambda: len(h.fake.calls_to("getUpdates")) >= 2, timeout=10)
        h.fake.push_update(tg.private_update(f"/kill {PIN}", sent_at_ms=h.clock.now_ms))
        await until(lambda: len(h.fake.calls_to("getUpdates")) >= 3)
        h.fake.push_update(tg.private_update("/devam", sent_at_ms=h.clock.now_ms))
        await until(lambda: len(h.fake.calls_to("getUpdates")) >= 4)
        with h.store.transaction() as tx:
            tx.set_state(StateKey.PAUSED, "1", h.clock.now_ms)
        assert h.fake.sent_texts() == []  # both held
        stop.set()
        await asyncio.wait_for(task, 5)
        kill_reply, resume_reply = h.fake.sent_texts()
        assert kill_reply.startswith("KILL etkin: giriş sinyalleri kesildi.")
        assert kill_reply.endswith(STANDBY_NOTE)
        assert resume_reply == STANDBY_REFUSAL
        assert h.flag(StateKey.KILLED) == "1"
        assert h.flag(StateKey.PAUSED) == "1"  # /devam lifted nothing

    drive(scenario, polling=True, arrival_hold_ms=3_000, contest_hold_ms=10_000)


@pytest.mark.parametrize(
    ("since_conflict_ms", "as_from_standby"),
    [(0, True), (5_000, True), (9_999, True), (10_000, False), (10 * MINUTE_MS, False)],
)
def test_a_stopping_copy_answers_as_from_standby_only_while_a_conflict_is_unexplained(
    since_conflict_ms: int, as_from_standby: bool
) -> None:
    # One Conflict keeps a command back for ten seconds. Stopped within them, the copy does
    # not know whether it holds the bot; afterwards the Conflict was a stray one, and a
    # command still waiting at shutdown is carried out as usual, for the rest of the process.
    async def scenario(h: Harness) -> None:
        arrive(h)
        h.clock.now_ms += 1_000
        conflict(h)
        h.clock.now_ms += since_conflict_ms
        h.service._stopping = True
        assert await h.service._settled() is as_from_standby

    drive(scenario, arrival_hold_ms=3_000, contest_hold_ms=10_000)


def test_a_stopping_copy_that_saw_no_conflict_answers_as_usual() -> None:
    async def scenario(h: Harness) -> None:
        arrive(h)
        h.service._stopping = True
        assert await h.service._settled() is False

    drive(scenario, arrival_hold_ms=3_000, contest_hold_ms=10_000)


# --- standby: entries, and the way back ------------------------------------------------------


@pytest.mark.parametrize("mark", ["1", "2", "yes"])
def test_a_pending_entry_signal_is_dropped_while_the_store_says_standby(mark: str) -> None:
    # Not the user's choice (unlike a pause): the drop is said, and with its real reason.
    # Also while the copy is leaving standby ("2") and for a mark nobody can read: an entry
    # may wait behind another message across a restart.
    async def scenario(h: Harness) -> None:
        await h.first_step()
        signal_id, entry_id = h.entry()
        follow_up = h.enqueue("[STOP GÜNCELLE] açık işlem", kind=OutboxKind.SIGNAL)
        with h.store.transaction() as tx:
            tx.set_state(StateKey.STANDBY, mark, h.clock.now_ms)
        await h.drain()
        assert h.row(entry_id).status is OutboxStatus.EXPIRED
        assert h.counted(signal_id) == (False, ReleaseReason.UNDELIVERED)
        assert h.row(follow_up).status is OutboxStatus.SENT
        assert h.fake.sent_texts() == [
            "[STOP GÜNCELLE] açık işlem",
            f"[İLETİLEMEDİ — ATLA] id {signal_id}: giriş sinyali gönderilmedi (bu kopya "
            "BEKLEMEDE). Bütçeden düşüldü.",
        ]

    drive(scenario)


def test_an_entry_that_may_have_arrived_is_asked_about_with_the_standby_reason() -> None:
    # The copy returned from an outage with a send that had timed out, then met the other
    # poller. Neither a pause nor a kill is set, and /atladim cannot reach a copy on standby.
    async def scenario(h: Harness) -> None:
        signal_id, entry_id = h.entry()
        with h.store.transaction() as tx:
            tx.set_state(StateKey.STANDBY, "1", h.clock.now_ms)
        await h.drain()  # the first step flags what an earlier process left pending
        assert h.row(entry_id).status is OutboxStatus.EXPIRED
        assert h.counted(signal_id) == (True, None)
        (notice,) = h.fake.sent_texts()
        assert notice == (
            f"[İLETİM BELİRSİZ] id {signal_id}: giriş sinyali sana ulaşmış da olabilir, "
            "ulaşmamış da (bu kopya BEKLEMEDE olduğu için yeniden gönderilmedi). Gördüysen ve "
            "girdiysen bir şey yapma. Riski bu kopyanın günlük bütçesinde sayılmaya devam "
            "ediyor; görmediysen ya da girmediysen, kopya yeniden ETKİN olduktan sonra: "
            f"/atladim {signal_id}"
        )
        assert "duraklatma" not in notice
        spans = code_entity_spans(notice)
        assert [notice[start : start + length] for start, length in spans] == [
            f"/atladim {signal_id}"
        ]

    drive(scenario)


@pytest.mark.parametrize("own_choice", [StateKey.PAUSED, StateKey.KILLED])
def test_a_pause_or_kill_explains_a_dropped_entry_even_on_standby(own_choice: StateKey) -> None:
    async def scenario(h: Harness) -> None:
        await h.first_step()
        signal_id, entry_id = h.entry()
        with h.store.transaction() as tx:
            tx.set_state(StateKey.STANDBY, "1", h.clock.now_ms)
            tx.set_state(own_choice, "1", h.clock.now_ms)
        await h.drain()
        assert h.row(entry_id).status is OutboxStatus.EXPIRED
        assert h.counted(signal_id) == (False, ReleaseReason.UNDELIVERED)
        assert h.fake.sent_texts() == []  # the user's own choice: silent, as before

    drive(scenario)


def test_after_a_restart_the_copy_says_active_once_it_was_alone_for_a_minute() -> None:
    # The process that went on standby was restarted (the user stopped the other copy).
    async def scenario(h: Harness) -> None:
        with h.store.transaction() as tx:
            tx.set_state(StateKey.STANDBY, "1", h.clock.now_ms)
        stop = asyncio.Event()
        task = await polling_for(h, stop)
        try:
            await answered(h)  # commands work at once; entries stay off for now
            h.clock.now_ms += 60_000
            await answered(h)
            await loop_turn(h)
            assert h.flag(StateKey.STANDBY) == "2"  # polling again, not yet alone for a minute
            assert ACTIVE_AGAIN not in h.fake.sent_texts()
            h.clock.now_ms += 1
            await until(lambda: ACTIVE_AGAIN in h.fake.sent_texts())
            assert h.flag(StateKey.STANDBY) is None
            h.clock.now_ms += 10 * MINUTE_MS
            await answered(h)
        finally:
            stop.set()
            await asyncio.wait_for(task, 5)
        assert h.fake.sent_texts().count(ACTIVE_AGAIN) == 1
        assert (OutboxKind.NOTICE, OutboxStatus.SENT, ACTIVE_AGAIN) in h.outbox()

    drive(scenario, polling=True)


def test_a_stray_conflict_restarts_the_minute_before_active() -> None:
    async def scenario(h: Harness) -> None:
        with h.store.transaction() as tx:
            tx.set_state(StateKey.STANDBY, "1", h.clock.now_ms)
        stop = asyncio.Event()
        task = await polling_for(h, stop)
        try:
            await answered(h)
            h.clock.now_ms += 30_000
            h.fake.script("getUpdates", CONFLICT_409)  # one Conflict, half a minute in
            await answered(h)
            await until(lambda: len(h.fake.calls_to("getUpdates")) >= 4, timeout=10)
            h.clock.now_ms += 45_000  # 75 s after the arrival, 45 s after the Conflict
            await answered(h)
            await loop_turn(h)
            assert h.flag(StateKey.STANDBY) == "2"
            h.clock.now_ms += 15_001  # a minute and a millisecond after the Conflict
            await answered(h)
            await until(lambda: ACTIVE_AGAIN in h.fake.sent_texts())
            assert h.flag(StateKey.STANDBY) is None
        finally:
            stop.set()
            await asyncio.wait_for(task, 5)

    drive(scenario, polling=True)


def test_a_late_conflict_also_restarts_the_minute_before_active() -> None:
    # Conflicts at 30 s and at 70 s after the arrival (the second one is "the other process
    # arriving" for the alarm): "a minute without another listener" starts at the last one.
    async def scenario(h: Harness) -> None:
        with h.store.transaction() as tx:
            tx.set_state(StateKey.STANDBY, "1", h.clock.now_ms)
        stop = asyncio.Event()
        task = await polling_for(h, stop)
        try:
            await answered(h)
            for step_ms in (30_000, 40_000):
                h.clock.now_ms += step_ms
                await one_conflict(h)
            h.clock.now_ms += 21_000  # 91 s after the arrival, 21 s after the last Conflict
            await answered(h)
            await loop_turn(h)
            assert h.flag(StateKey.STANDBY) == "2"
            assert ACTIVE_AGAIN not in h.fake.sent_texts()
            h.clock.now_ms += 39_001  # a minute and a millisecond after the last Conflict
            await answered(h)
            await until(lambda: ACTIVE_AGAIN in h.fake.sent_texts())
        finally:
            stop.set()
            await asyncio.wait_for(task, 5)

    drive(scenario, polling=True)


def test_a_conflict_and_then_silence_do_not_add_up_to_a_minute_alone() -> None:
    async def scenario(h: Harness) -> None:
        with h.store.transaction() as tx:
            tx.set_state(StateKey.STANDBY, "1", h.clock.now_ms)
        stop = asyncio.Event()
        task = await polling_for(h, stop)
        try:
            await answered(h)
            await one_conflict(h)
            h.clock.now_ms += 10 * MINUTE_MS  # nothing from Telegram in all that time
            await answered(h)  # the first answer back is a new arrival, not the end of a minute
            await loop_turn(h)
            assert h.flag(StateKey.STANDBY) == "2"
            assert ACTIVE_AGAIN not in h.fake.sent_texts()
            h.clock.now_ms += 60_000
            await answered(h)
            await loop_turn(h)
            assert ACTIVE_AGAIN not in h.fake.sent_texts()
            h.clock.now_ms += 1
            await until(lambda: ACTIVE_AGAIN in h.fake.sent_texts())
        finally:
            stop.set()
            await asyncio.wait_for(task, 5)

    drive(scenario, polling=True)


def test_a_copy_that_telegram_stopped_answering_does_not_call_itself_active() -> None:
    # Arrived, then the network went away: a minute passes on the clock, but nothing is
    # known about who polls now. The minute starts again with the next answer.
    async def scenario(h: Harness) -> None:
        with h.store.transaction() as tx:
            tx.set_state(StateKey.STANDBY, "1", h.clock.now_ms)
        stop = asyncio.Event()
        task = await polling_for(h, stop)
        try:
            await answered(h)
            h.clock.now_ms += 10 * MINUTE_MS  # the long poll hangs: no answer in all that time
            await loop_turn(h)
            assert h.flag(StateKey.STANDBY) == "2"
            assert ACTIVE_AGAIN not in h.fake.sent_texts()
            await answered(h)  # back: a new arrival
            h.clock.now_ms += 60_000
            await answered(h)
            await loop_turn(h)
            assert h.flag(StateKey.STANDBY) == "2"
            h.clock.now_ms += 1
            await until(lambda: ACTIVE_AGAIN in h.fake.sent_texts())
        finally:
            stop.set()
            await asyncio.wait_for(task, 5)

    drive(scenario, polling=True)


def test_a_restart_into_the_same_conflict_says_so_at_most_every_five_minutes() -> None:
    # The user restarted the copy to end the standby and waits for "ETKİN": when the other
    # process is still there they are told that it did not work. A process that keeps
    # crashing comes back every few seconds: the store throttles the alarm across restarts.
    sent: list[list[str]] = []
    starts_at_ms = [0, 10_000, 20_000, 200_000, 310_000, 320_000, 611_000]

    async def one_start(h: Harness) -> None:
        h.clock.now_ms = NOW_MS + starts_at_ms[len(sent)]
        h.fake.script("getUpdates", CONFLICT_409, CONFLICT_409)
        stop = asyncio.Event()
        task = asyncio.create_task(h.service.run(stop))
        try:
            await until(lambda: h.service._standby, timeout=10)
            await h.drain()
            assert h.flag(StateKey.STANDBY) == "1"
        finally:
            stop.set()
            await asyncio.wait_for(task, 5)
        sent.append(h.fake.sent_texts())

    with Store(":memory:") as store:
        for _ in starts_at_ms:
            drive(one_start, polling=True, store=store)

    assert sent == [
        [STANDBY_ALARM],  # the first time: the standby itself
        [STANDBY_AGAIN],  # 10 s later, restarted: still there
        [],  # 20 s
        [],  # 200 s: less than five minutes after the last "again"
        [STANDBY_AGAIN],  # 310 s: five minutes after the one at 10 s
        [],
        [STANDBY_AGAIN],  # 611 s
    ]
    assert telegram_module.STANDBY_AGAIN_ALARM == ("telegram_standby_again", 300_000)
    assert telegram_module.standby_again_alarm_text("vps-1") == STANDBY_AGAIN
    assert code_entity_spans(STANDBY_AGAIN) == []


def test_a_copy_that_was_active_again_announces_a_new_standby_in_full() -> None:
    # Not a restart into the old standby: the copy had said ETKİN in between.
    async def scenario(h: Harness) -> None:
        with h.store.transaction() as tx:
            tx.set_state(StateKey.STANDBY, "1", h.clock.now_ms)
        stop = asyncio.Event()
        task = await polling_for(h, stop)
        try:
            await answered(h)
            h.clock.now_ms += 60_000
            await answered(h)
            h.clock.now_ms += 1
            await until(lambda: ACTIVE_AGAIN in h.fake.sent_texts())
            h.clock.now_ms += 10 * MINUTE_MS  # silent, then back: a new arrival
            h.fake.script("getUpdates", CONFLICT_409, CONFLICT_409)
            h.fake.push_update(tg.private_update("/mod", sent_at_ms=h.clock.now_ms))
            await until(lambda: STANDBY_ALARM in h.fake.sent_texts(), timeout=10)
            assert h.flag(StateKey.STANDBY) == "1"
        finally:
            stop.set()
            await asyncio.wait_for(task, 5)
        assert STANDBY_AGAIN not in h.fake.sent_texts()

    drive(scenario, polling=True)


def test_an_arrival_that_stays_unchallenged_says_nothing_when_there_was_no_standby() -> None:
    async def scenario(h: Harness) -> None:
        stop = asyncio.Event()
        task = await polling_for(h, stop)
        try:
            await answered(h)
            h.clock.now_ms += 5 * MINUTE_MS
            await answered(h)
            await asyncio.sleep(0.2)
        finally:
            stop.set()
            await asyncio.wait_for(task, 5)
        assert ACTIVE_AGAIN not in h.fake.sent_texts()
        assert h.flag(StateKey.STANDBY) is None

    drive(scenario, polling=True)


def test_the_standby_texts_name_the_copy_and_leave_nothing_to_tap_by_mistake() -> None:
    assert telegram_module.standby_alarm_text("vps-1") == STANDBY_ALARM
    assert telegram_module.active_again_text("vps-1") == ACTIVE_AGAIN
    assert telegram_module.standby_alarm_text("").startswith(
        "[ALARM] Bu PropBot kopyası BEKLEMEDE: "
    )
    assert telegram_module.active_again_text("").startswith(
        "[BİLGİ] Bu PropBot kopyası yeniden ETKİN: "
    )
    # No /devam, /atladim or /aldim; /durum only reads and BotFather's /revoke is not ours.
    for text in (STANDBY_ALARM, ACTIVE_AGAIN, STANDBY_REFUSAL, STANDBY_NOTE):
        assert code_entity_spans(text) == []


def test_polling_that_dies_of_a_rejected_token_ends_run_with_the_fatal_error() -> None:
    async def scenario(h: Harness) -> TelegramFatalError:
        h.fake.script("getUpdates", tg.error_response(401, "Unauthorized"))
        with capture_logs() as logs, pytest.raises(TelegramFatalError) as caught:
            await asyncio.wait_for(h.service.run(asyncio.Event()), 5)
        assert BOT_TOKEN not in repr(logs)
        assert "telegram_polling_stopped" in [entry["event"] for entry in logs]
        assert not h.app.running
        return caught.value

    _assert_token_free(drive(scenario, polling=True))


def test_a_token_rejected_while_polling_starts_is_fatal() -> None:
    async def scenario(h: Harness) -> TelegramFatalError:
        h.fake.script("deleteWebhook", tg.error_response(401, "Unauthorized"))
        with capture_logs() as logs, pytest.raises(TelegramFatalError) as caught:
            await asyncio.wait_for(h.service.run(asyncio.Event()), 5)
        assert BOT_TOKEN not in repr(logs)
        assert h.fake.calls_to("getUpdates") == []
        return caught.value

    _assert_token_free(drive(scenario, polling=True))


def test_run_goes_on_when_the_polling_task_cannot_be_found(monkeypatch: pytest.MonkeyPatch) -> None:
    # A future library version may rename its task: polling works, only the watch is missing.
    monkeypatch.setattr(telegram_module, "_POLLING_TASK_NAME", "renamed")

    async def scenario(h: Harness) -> None:
        stop = asyncio.Event()
        with capture_logs() as logs:
            task = asyncio.create_task(h.service.run(stop))
            h.fake.push_update(tg.private_update("/mod", sent_at_ms=NOW_MS))
            await until(lambda: len(h.fake.sent_texts()) == 1)
            stop.set()
            await asyncio.wait_for(task, 5)
        assert "telegram_polling_task_not_found" in [entry["event"] for entry in logs]

    drive(scenario, polling=True)


def test_an_alarm_being_written_at_shutdown_is_not_lost() -> None:
    async def scenario(h: Harness) -> None:
        stop = asyncio.Event()
        task = asyncio.create_task(h.service.run(stop))
        await until(lambda: h.app.running)
        h.service._on_polling_error(Conflict("terminated by other getUpdates request"))
        stop.set()
        await asyncio.wait_for(task, 5)
        assert not h.service._background
        assert [text for _kind, _status, text in h.outbox()] == [CONFLICT_ALARM]

    drive(scenario)


def test_polling_that_stops_for_no_reason_is_fatal_too() -> None:
    async def scenario(h: Harness) -> None:
        stop = asyncio.Event()
        task = asyncio.create_task(h.service.run(stop))
        await until(lambda: h.service._polling_task is not None)
        assert h.service._polling_task is not None
        h.service._polling_task.cancel()
        h.service.wake_sender()
        with pytest.raises(TelegramFatalError) as caught:
            await asyncio.wait_for(task, 5)
        assert str(caught.value) == FATAL_POLLING
        assert caught.value.__cause__ is None
        assert caught.value.__context__ is None

    drive(scenario, polling=True)


def test_run_ends_with_store_error_when_the_database_fails() -> None:
    async def scenario(h: Harness) -> None:
        stop = asyncio.Event()
        task = asyncio.create_task(h.service.run(stop))
        await until(lambda: len(h.fake.calls) >= 1)
        h.store.close()
        h.service.wake_sender()
        with pytest.raises(StoreError):
            await asyncio.wait_for(task, 5)
        assert not h.app.running

    drive(scenario)


def test_a_cancelled_run_still_shuts_down() -> None:
    async def scenario(h: Harness) -> None:
        task = asyncio.create_task(h.service.run(asyncio.Event()))
        await until(lambda: h.app.running)
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
        assert not h.app.running
        assert h.app.updater is not None
        assert not h.app.updater.running

    drive(scenario, polling=True)
