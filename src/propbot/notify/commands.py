"""Telegram commands of the signal bot (SPEC §5.4): the whole command logic, library-free.

`Commands.handle` takes the raw text of one message and returns the reply. It is plain
synchronous code that opens its own store transactions (one per command), so every rule
is testable without python-telegram-bot; `notify.telegram` is the thin adapter that
checks the chat, calls `handle` in a worker thread and sends the reply.

Nothing here places, changes or cancels an order: the mode is `signal`, and `/mod` can
only explain why the other modes are locked (CLAUDE.md invariant 1).

PIN hygiene (CLAUDE.md invariant 6):
- whether the user's message must be deleted is decided first, from the raw text alone,
  and every answer keeps that decision, refusals and internal errors included;
- a reply never repeats an argument, and the PIN is cut out of every reply as a last
  line of defence (`/atladim <PIN>` would otherwise print it back as an id);
- nothing but the command name is logged.

Age rule: commands typed while the bot was down are processed after a restart. The ones
that only reduce risk (`/duraklat`, `/kill <PIN>`) are applied whatever their age;
the ones that give risk back (`/devam`, `/atladim`, `/aldim`) are refused when the
message is more than a minute old. Read-only commands are always answered.
"""

import datetime as dt
import hmac
import re
import statistics
from collections import Counter
from collections.abc import Callable, Mapping
from dataclasses import dataclass, field
from typing import Final

import structlog

from propbot.core.clock import Clock, ms_from_datetime, utc_from_ms
from propbot.core.propday import prop_day, prop_day_start
from propbot.notify.format import (
    ISTANBUL,
    NEW_YORK,
    fmt_clock,
    fmt_date,
    fmt_duration,
    fmt_money,
    fmt_percent,
)
from propbot.risk.config import RiskConfig
from propbot.risk.modes import Mode, mode_refusal
from propbot.risk.rules import daily_budget_amount
from propbot.risk.sizing import risk_budget_amount
from propbot.risk.venue import (
    RULES_MAX_AGE_DAYS,
    RulesState,
    UnresolvedKind,
    VenueProfile,
    rules_freshness,
    unresolved_fields,
)
from propbot.signals.schema import STRATEGY_NAMES, Action
from propbot.signals.tvconfig import TvAlertsConfig
from propbot.signals.watch import watch_window_open
from propbot.store.db import (
    BudgetRow,
    LatencySample,
    OutboxKind,
    OutboxStatus,
    Outcome,
    ReleaseReason,
    SignalRow,
    StateKey,
    Store,
    StoreError,
    Tx,
)

_SECOND_MS: Final = 1000
_MINUTE_MS: Final = 60 * _SECOND_MS
_ONE_DAY: Final = dt.timedelta(days=1)

# A command older than this was typed before a restart or delivered late.
STALE_COMMAND_MS: Final = 60 * _SECOND_MS
PIN_MAX_FAILURES: Final = 5
PIN_FAILURE_WINDOW_MS: Final = 15 * _MINUTE_MS
PIN_LOCK_MS: Final = 15 * _MINUTE_MS
# `/risk` lists at most this many entries, so the reply stays far below Telegram's 4096.
MAX_RISK_ENTRIES: Final = 15
LATENCY_SAMPLE_COUNT: Final = 50

_LATENCY_LOOKBACK_MS: Final = 7 * 24 * 60 * _MINUTE_MS
# The server clock more than this behind Telegram's is certain: a message cannot arrive
# before it was sent (Telegram stamps whole seconds).
_CLOCK_BEHIND_TOLERANCE_MS: Final = 2 * _SECOND_MS

_PIN_FORMAT: Final = re.compile(r"[0-9]{6,12}")
_ID_FORMAT: Final = re.compile(r"[0-9]{1,15}")
_REDACTED: Final = "***"
# A run of digits long enough to be a PIN (PROPBOT_PIN is 6 to 12 digits).
_PIN_LIKE: Final = re.compile(r"[0-9]{6,}")
_FLAG_SET: Final = "1"
_FLAG_CLEAR: Final = "0"
# Commands are ASCII, but a Turkish keyboard easily produces the dotless i or the breve g
# in /atladim, /saglik, /yardim: such letters are folded to ASCII before the lookup.
_TURKISH_TO_ASCII: Final = str.maketrans("ıİğĞüÜşŞöÖçÇ", "iIgGuUsSoOcC")
# Messages that start like one of these and carry anything more may contain the PIN.
_PIN_COMMANDS: Final = ("kill", "devam")
_STRATEGY_ACTIONS: Final = tuple(
    action.value for action in Action if action is not Action.HEARTBEAT
)

_OUTCOME_LABELS: Final[Mapping[Outcome, str]] = {
    Outcome.FORWARDED: "iletildi",
    Outcome.STALE: "bayat",
    Outcome.SUPPRESSED_PAUSED: "duraklatmada bastırıldı",
    Outcome.SUPPRESSED_KILLED: "KILL'de bastırıldı",
    Outcome.SKIP_RISK_TOO_WIDE: "stop çok geniş",
    Outcome.SKIP_BUDGET: "bütçe dolu",
    Outcome.FILTERED_MIN_STEP: "küçük stop adımı",
    Outcome.REJECTED_WIDEN: "stop genişletme reddi",
    Outcome.NO_ACTIVE_TRADE: "açık işlem yok",
    Outcome.HEARTBEAT: "heartbeat",
    Outcome.SKIP_CLOCK: "saat uyuşmazlığı",
    Outcome.SUPERSEDED: "eskimiş güncelleme",
    Outcome.ERROR: "işleme hatası",
}
_QUEUED_LABEL: Final = "kuyrukta"

_HELP: Final = "\n".join(
    (
        "PropBot komutları (mod: signal — emir gönderilmez)",
        "/durum — mod, durum, kuyruk, bugünkü sinyaller",
        "/pnl — bugünkü girişler ve sayılan risk",
        "/risk — risk ayarları ve günlük bütçe defteri",
        "/saglik — sağlık, gecikme, saat",
        "/duraklat — giriş sinyallerini durdur (stop/çıkış/iptal gelmeye devam eder)",
        "/devam — yeniden başlat (KILL sonrası: /devam <PIN>)",
        "/kill <PIN> — giriş sinyallerini kes; yalnızca /devam <PIN> açar",
        "/atladim [id] — girişi almadım: riskini günlük bütçeden düş",
        "/aldim <id> — /atladim'ı geri al",
        "/mod — çalışma modu",
        "/yardim — bu liste",
    )
)
_UNKNOWN_PREFIX: Final = "Bilinmeyen komut.\n"
_LONG_NUMBER_REPLY: Final = (
    "Mesajındaki uzun sayı PIN olabileceği için mesajın siliniyor. PIN yalnızca /kill <PIN> ya "
    "da /devam <PIN> içinde kullanılır."
)
_KILL_USAGE: Final = (
    "Kullanım: /kill <PIN> — giriş sinyallerini keser; yalnızca /devam <PIN> yeniden açar. "
    "PIN içeren mesajın silinir. PIN'siz acil durdurma: /duraklat"
)
_ATLADIM_USAGE: Final = (
    "Kullanım: /atladim [id] — id, sinyal mesajının son satırındaki numaradır; id yazmazsan "
    "bugün sana ulaşan son giriş alınır."
)
_ALDIM_USAGE: Final = (
    "Kullanım: /aldim <id> — id, /atladim ile serbest bırakılan girişin numarasıdır."
)
_ENTRY_ONLY_NOTE: Final = "Stop güncelleme / çıkış / iptal mesajları gelmeye devam eder."

_log = structlog.get_logger(__name__)


@dataclass(frozen=True, slots=True, kw_only=True)
class CommandDeps:
    """Everything the commands need from the rest of the process."""

    store: Store
    risk: RiskConfig
    venue: VenueProfile
    venue_name: str  # file stem of the venue profile, e.g. "ftmo_2step_100k"
    tv: TvAlertsConfig
    pin: str = field(repr=False)  # never printed with the object
    clock: Clock
    started_ms: int
    reject_counts: Callable[[], Mapping[str, int]]  # webhook rejections since start, by code


@dataclass(frozen=True, slots=True)
class Reply:
    text: str
    delete_request: bool = False
    """Delete the user's message: it may hold the PIN."""


@dataclass(frozen=True, slots=True)
class _Call:
    """One command as the handlers see it. `age_ms` is server time minus Telegram's stamp."""

    args: tuple[str, ...]
    now_ms: int
    age_ms: int


@dataclass(frozen=True, slots=True)
class _Command:
    run: Callable[[_Call], str]
    # True for commands that give risk back: they are refused when the message is old.
    needs_fresh: bool = False


@dataclass(frozen=True, slots=True)
class _PinCheck:
    ok: bool
    refusal: str  # user-facing reason when not ok


def _split_command(text: str) -> tuple[str | None, tuple[str, ...]]:
    """Return (command name, arguments); the name is None when the text is not a command.

    The name is lower-cased ASCII without the slash and without a "@botname" suffix.
    """
    parts = text.split()
    if not parts or not parts[0].startswith("/"):
        return None, ()
    name = parts[0][1:].partition("@")[0].translate(_TURKISH_TO_ASCII).lower()
    return name, tuple(parts[1:])


def _must_delete(text: str) -> bool:
    """Decide from the SHAPE of the raw text alone whether the message may carry a PIN.

    Never by comparing with the real PIN. What the bot does with a message (deletes it or
    not, which reply it sends) is visible to whoever writes in the chat; if that depended
    on a match, digit strings could be tested against the PIN without ever touching the
    failure counter or the lock.

    True when the text holds a run of six or more digits anywhere (a PIN is 6 to 12
    digits), or when it starts like `/kill` or `/devam` and holds anything beyond the bare
    command (a wrong, mistyped or glued-on PIN too).
    """
    if _PIN_LIKE.search(text) is not None:
        return True
    name, args = _split_command(text)
    if name is None:
        return False
    return any(name.startswith(command) and (name != command or args) for command in _PIN_COMMANDS)


def _scrub_echoed_numbers(reply: str, text: str) -> str:
    """Replace in `reply` every long digit run that came from the user's own `text`."""
    for number in set(_PIN_LIKE.findall(text)):
        reply = reply.replace(number, _REDACTED)
    return reply


def _flag(tx: Tx, key: StateKey) -> bool:
    # Absent or "0" is clear. Any other text counts as set: an unreadable flag fails closed.
    return tx.get_state(key) not in (None, _FLAG_CLEAR)


def _int_state(tx: Tx, key: StateKey) -> int | None:
    """Stored integer, None when absent. Unreadable text raises ValueError."""
    value = tx.get_state(key)
    return None if value is None else int(value)


def _entry_label(signal_id: int, signal: SignalRow | None) -> str:
    """Name an entry as `id 124 · Asian Sweep · LONG · bar 09:44 NY` (the bar's open)."""
    if signal is None:
        return f"id {signal_id}"
    name = STRATEGY_NAMES.get(signal.strategy_id, signal.strategy_id)
    side = "?" if signal.side is None else signal.side.upper()
    return f"id {signal_id} · {name} · {side} · bar {fmt_clock(signal.bar_time_ms, NEW_YORK)} NY"


def _release_label(row: BudgetRow) -> str:
    if row.counted:
        return "sayıldı"
    return "iletilemedi" if row.release_reason is ReleaseReason.UNDELIVERED else "atlandı"


def _parse_id(args: tuple[str, ...]) -> int | None:
    if len(args) != 1 or _ID_FORMAT.fullmatch(args[0]) is None:
        return None
    return int(args[0])


def _seconds(milliseconds: float) -> str:
    return f"{milliseconds / _SECOND_MS:.1f} sn"


def _late_note(call: _Call) -> str:
    if call.age_ms <= STALE_COMMAND_MS:
        return ""
    return f"\n(Komut {fmt_duration(call.age_ms / _SECOND_MS)} gecikmeyle işlendi.)"


class Commands:
    """The command set of one chat. `handle` may be called from any one thread at a time."""

    def __init__(self, deps: CommandDeps) -> None:
        if _PIN_FORMAT.fullmatch(deps.pin) is None:
            # An empty PIN would be "contained" in every message; the value is never printed.
            raise ValueError("pin must be 6 to 12 digits")
        self._deps: Final = deps
        self._pin: Final = deps.pin.encode("ascii")
        self._unauthorized = 0
        self._commands: Final[Mapping[str, _Command]] = {
            "durum": _Command(self._status),
            "pnl": _Command(self._pnl),
            "risk": _Command(self._risk),
            "saglik": _Command(self._health),
            "mod": _Command(self._mode),
            "yardim": _Command(self._help),
            "start": _Command(self._help),
            "duraklat": _Command(self._pause),
            "kill": _Command(self._kill),
            "devam": _Command(self._resume, needs_fresh=True),
            "atladim": _Command(self._skipped, needs_fresh=True),
            "aldim": _Command(self._taken, needs_fresh=True),
        }

    def note_unauthorized(self) -> None:
        """Count a message from a chat that is not the whitelisted one (shown by /saglik)."""
        self._unauthorized += 1

    def handle(self, text: str, *, sent_at_ms: int) -> Reply:
        """Answer one message. Never raises: a failure becomes a reply and changes nothing."""
        pin = self._deps.pin
        # First, from the raw text alone: nothing below may lose this decision.
        delete_request = _must_delete(text)
        name, args = _split_command(text)
        known = name if name in self._commands else None
        try:
            body = self._dispatch(name, args, sent_at_ms, pin_like=delete_request)
        except Exception as error:
            # Every command is one transaction, so whatever failed has been rolled back.
            _log.error("telegram_command_failed", command=known, error_type=type(error).__name__)
            body = (
                f"[HATA] Komut işlenemedi ({type(error).__name__}); hiçbir şey değiştirilmedi. "
                "/saglik ile kontrol et; sürerse log'a bak."
            )
        # No long number of the user's own text is repeated back (it may be a PIN or a
        # guess at one); the exact-match replacement is only a last line of defence.
        body = _scrub_echoed_numbers(body, text).replace(pin, _REDACTED)
        return Reply(text=body, delete_request=delete_request)

    def _dispatch(
        self, name: str | None, args: tuple[str, ...], sent_at_ms: int, *, pin_like: bool
    ) -> str:
        if name is None:
            return _LONG_NUMBER_REPLY if pin_like else _HELP
        command = self._commands.get(name)
        if command is None:
            _log.info("telegram_command", command=None)
            return _UNKNOWN_PREFIX + _HELP
        now_ms = self._deps.clock()
        call = _Call(args=args, now_ms=now_ms, age_ms=now_ms - sent_at_ms)
        refusal = _freshness_refusal(name, call) if command.needs_fresh else None
        _log.info("telegram_command", command=name, age_ms=call.age_ms, refused=refusal is not None)
        return command.run(call) if refusal is None else refusal

    # -- shared pieces -----------------------------------------------------------------------

    def _prop_day(self, now_ms: int) -> dt.date:
        reset = self._deps.venue.daily_reset
        return prop_day(utc_from_ms(now_ms), reset.zone, reset.time_of_day)

    def _day_bounds_ms(self, day: dt.date) -> tuple[int, int]:
        reset = self._deps.venue.daily_reset
        start = prop_day_start(day, reset.zone, reset.time_of_day)
        end = prop_day_start(day + _ONE_DAY, reset.zone, reset.time_of_day)
        return ms_from_datetime(start), ms_from_datetime(end)

    def _money(self, amount: float) -> str:
        return fmt_money(amount, self._deps.venue.account_currency)

    def _budget_text(self, used: float) -> str:
        cap = daily_budget_amount(self._deps.risk, self._deps.venue)
        return f"{self._money(used)}/{self._money(cap)}"

    def _rules_text(self, now_ms: int) -> str:
        freshness = rules_freshness(self._deps.venue, utc_from_ms(now_ms).date())
        if freshness.state is RulesState.FRESH:
            return f"taze ({freshness.age_days} gün önce doğrulandı)"
        if freshness.state is RulesState.STALE:
            return (
                f"ESKİ ({freshness.age_days} gün önce doğrulandı; sınır {RULES_MAX_AGE_DAYS} gün)"
                " — resmi sayfadan doğrula ve rules_verified_at'i güncelle"
            )
        if freshness.state is RulesState.FUTURE:
            return "GEÇERSİZ (rules_verified_at ileri tarihli) — tarihi düzelt"
        return "DOĞRULANMADI (rules_verified_at boş) — resmi sayfadan doğrula"

    def _when(self, moment_ms: int | None, now_ms: int) -> str:
        """Format as `18:31 TR (2 dk önce)`; the date is added when it is not today's."""
        if moment_ms is None:
            return "yok"
        clock = fmt_clock(moment_ms, ISTANBUL)
        if fmt_date(moment_ms, ISTANBUL) != fmt_date(now_ms, ISTANBUL):
            clock = f"{fmt_date(moment_ms, ISTANBUL)} {clock}"
        return f"{clock} TR ({fmt_duration((now_ms - moment_ms) / _SECOND_MS)} önce)"

    def _budget_line(self, tx: Tx, entry: BudgetRow, today: dt.date) -> str:
        if entry.prop_day != today:
            return f"(Prop günü {entry.prop_day.isoformat()}: bugünün bütçesini etkilemez.)"
        used = tx.budget_used(today)
        line = f"Günlük bütçe şimdi {self._budget_text(used)}."
        if used > daily_budget_amount(self._deps.risk, self._deps.venue) + 1e-9:
            line += " DİKKAT: günlük bütçe aşıldı; yeni giriş sinyali iletilmez."
        return line

    def _check_pin(self, tx: Tx, args: tuple[str, ...], now_ms: int) -> _PinCheck:
        """Compare the argument with the PIN and keep the failure counter (inside `tx`).

        Five failures within 15 minutes lock PIN entry for 15 minutes. The state lives in
        the store, so a restart does not reset it. An argument that is not 6-12 digits is a
        counted failure without a comparison.
        """
        try:
            locked_until = _int_state(tx, StateKey.PIN_LOCKED_UNTIL_MS)
        except ValueError:
            locked_until = now_ms + PIN_LOCK_MS  # unreadable lock: fail closed
            tx.set_state(StateKey.PIN_LOCKED_UNTIL_MS, str(locked_until), now_ms)
        if locked_until is not None:
            # A clock that jumped must not lock PIN entry for longer than one lock period.
            remaining_ms = min(locked_until, now_ms + PIN_LOCK_MS) - now_ms
            if remaining_ms > 0:
                return _PinCheck(False, _pin_locked_text(remaining_ms))
            self._clear_pin_state(tx)

        candidate = args[0] if len(args) == 1 else None
        if (
            candidate is not None
            and _PIN_FORMAT.fullmatch(candidate) is not None
            and hmac.compare_digest(candidate.encode("ascii"), self._pin)
        ):
            self._clear_pin_state(tx)
            return _PinCheck(True, "")

        try:
            window_start = _int_state(tx, StateKey.PIN_WINDOW_START_MS)
            failures = _int_state(tx, StateKey.PIN_FAIL_COUNT)
        except ValueError:
            window_start, failures = None, None
        in_window = (
            window_start is not None
            and failures is not None
            and 0 <= now_ms - window_start <= PIN_FAILURE_WINDOW_MS
        )
        if window_start is None or failures is None or not in_window:
            window_start, failures = now_ms, 0
        failures += 1
        if failures >= PIN_MAX_FAILURES:
            self._clear_pin_state(tx)
            tx.set_state(StateKey.PIN_LOCKED_UNTIL_MS, str(now_ms + PIN_LOCK_MS), now_ms)
            _log.warning("telegram_pin_locked", lock_ms=PIN_LOCK_MS)
            return _PinCheck(False, "PIN hatalı. " + _pin_locked_text(PIN_LOCK_MS))
        tx.set_state(StateKey.PIN_WINDOW_START_MS, str(window_start), now_ms)
        tx.set_state(StateKey.PIN_FAIL_COUNT, str(failures), now_ms)
        _log.warning("telegram_pin_rejected", failures=failures)
        window_minutes = PIN_FAILURE_WINDOW_MS // _MINUTE_MS
        return _PinCheck(
            False,
            f"PIN hatalı. Kalan deneme: {PIN_MAX_FAILURES - failures} "
            f"({window_minutes} dk içinde {PIN_MAX_FAILURES} hata PIN girişini "
            f"{PIN_LOCK_MS // _MINUTE_MS} dk kilitler).",
        )

    @staticmethod
    def _clear_pin_state(tx: Tx) -> None:
        for key in (
            StateKey.PIN_LOCKED_UNTIL_MS,
            StateKey.PIN_WINDOW_START_MS,
            StateKey.PIN_FAIL_COUNT,
        ):
            tx.delete_state(key)

    # -- read-only commands ------------------------------------------------------------------

    def _help(self, _call: _Call) -> str:
        return _HELP

    def _mode(self, call: _Call) -> str:
        if not call.args:
            return (
                "Mod: signal — yalnızca sinyal mesajı gönderilir, emir gönderilmez. "
                "Diğer modlar (paper, approve, auto) bu sürümde yok."
            )
        try:
            mode = Mode(call.args[0].lower()) if len(call.args) == 1 else None
        except ValueError:
            mode = None
        if mode is None:
            valid = ", ".join(member.value for member in Mode)
            return f"Bilinmeyen mod; geçerli modlar: {valid}. Mod değişmedi: signal."
        refusal = mode_refusal(mode, self._deps.venue, utc_from_ms(call.now_ms).date())
        if refusal is None:
            return "Mod zaten signal; değişiklik yok."
        return f"Mod değişmedi (signal). {refusal}."

    def _status(self, call: _Call) -> str:
        deps = self._deps
        now_ms = call.now_ms
        day = self._prop_day(now_ms)
        start_ms, end_ms = self._day_bounds_ms(day)
        with deps.store.transaction() as tx:
            paused = _flag(tx, StateKey.PAUSED)
            killed = _flag(tx, StateKey.KILLED)
            depth = tx.queue_depth()
            outbox = tx.outbox_counts()
            signals = tx.signals_received_between(start_ms, end_ms)
            last_alert_ms = _last_strategy_alert_ms(tx)
            last_heartbeat_ms = tx.last_received_ms(action=Action.HEARTBEAT.value)

        if killed:
            state = "KILL (giriş sinyalleri kesildi; açmak için /devam <PIN>)"
        elif paused:
            state = "duraklatıldı (giriş sinyalleri iletilmiyor; açmak için /devam)"
        else:
            state = "aktif"
        counts = Counter(
            signal.outcome for signal in signals if signal.action != Action.HEARTBEAT.value
        )
        parts = [
            f"{_OUTCOME_LABELS[outcome]} {counts[outcome]}"
            for outcome in Outcome
            if counts[outcome]
        ]
        if counts[None]:
            parts.append(f"{_QUEUED_LABEL} {counts[None]}")
        venue = deps.venue
        return "\n".join(
            (
                f"[DURUM] {fmt_clock(now_ms, ISTANBUL, seconds=True)} TR",
                f"Mod: signal (emir gönderilmez) · Durum: {state}",
                f"Venue: {deps.venue_name} · {venue.venue.upper()} {venue.symbol}",
                f"Sinyal kuyruğu: {depth} · Telegram kutusu: "
                f"{outbox[OutboxStatus.PENDING]} bekliyor, {outbox[OutboxStatus.FAILED]} başarısız",
                f"Bugünkü sinyaller (prop günü {day.isoformat()}): "
                + (" · ".join(parts) if parts else "yok"),
                f"Son sinyal alert'i: {self._when(last_alert_ms, now_ms)}",
                f"Son heartbeat: {self._when(last_heartbeat_ms, now_ms)}",
                f"Çalışma süresi: {fmt_duration((now_ms - deps.started_ms) / _SECOND_MS)}",
            )
        )

    def _pnl(self, call: _Call) -> str:
        day = self._prop_day(call.now_ms)
        with self._deps.store.transaction() as tx:
            entries = tx.budget_entries(day)
            used = tx.budget_used(day)
        released = sum(1 for entry in entries if not entry.counted)
        released_note = f" ({released} tanesi serbest bırakıldı)" if released else ""
        return (
            "[K/Z] Faz 1'de emir gönderilmez; gerçekleşen kâr/zarar tutulmaz "
            "(işlemlerini kendi platformundan izle).\n"
            f"Bugün (prop günü {day.isoformat()}): {len(entries)} giriş iletildi{released_note} · "
            f"sayılan risk {self._budget_text(used)}"
        )

    def _risk(self, call: _Call) -> str:
        deps = self._deps
        venue = deps.venue
        day = self._prop_day(call.now_ms)
        with deps.store.transaction() as tx:
            entries = tx.budget_entries(day)
            used = tx.budget_used(day)
            shown = entries[-MAX_RISK_ENTRIES:]
            entry_lines = [
                f"{_entry_label(entry.signal_id, tx.get_signal(entry.signal_id))} · "
                f"{self._money(entry.risk_amount)} · {_release_label(entry)}"
                for entry in shown
            ]
        lines = [
            f"[RİSK] {deps.venue_name} · {venue.venue.upper()} {venue.symbol}",
            f"İşlem başı risk: {fmt_percent(deps.risk.risk_per_trade_pct)} = "
            f"{self._money(risk_budget_amount(deps.risk, venue))}",
            f"Günlük bütçe: {self._budget_text(used)} kullanıldı — ihtiyatlı defter: iletilen her "
            "giriş prop günü bitene kadar sayılır; almadıysan /atladim ile serbest bırak",
        ]
        if entries:
            lines.append(f"Bugünkü girişler (prop günü {day.isoformat()}):")
            hidden = len(entries) - len(shown)
            if hidden:
                lines.append(f"(önceki {hidden} giriş gösterilmiyor)")
            lines.extend(entry_lines)
        else:
            lines.append(f"Bugünkü girişler (prop günü {day.isoformat()}): yok")
        lines.append(f"Venue kuralları: {self._rules_text(call.now_ms)}")
        missing = [
            item.path.removeprefix("contract.")
            for item in unresolved_fields(venue)
            if item.kind is UnresolvedKind.RUNTIME
        ]
        lines.append(
            "Boş contract alanları: " + ", ".join(missing) + " — boyut önerisi hesaplanamaz"
            if missing
            else "Boş contract alanları: yok"
        )
        return "\n".join(lines)

    def _health(self, call: _Call) -> str:
        deps = self._deps
        now_ms = call.now_ms
        lines = [
            f"[SAĞLIK] {fmt_clock(now_ms, ISTANBUL, seconds=True)} TR",
            f"Çalışma süresi: {fmt_duration((now_ms - deps.started_ms) / _SECOND_MS)}",
        ]
        try:
            with deps.store.transaction() as tx:
                depth = tx.queue_depth()
                outbox = tx.outbox_counts()
                last_alert_ms = _last_strategy_alert_ms(tx)
                last_heartbeat_ms = tx.last_received_ms(action=Action.HEARTBEAT.value)
                samples = tx.latency_samples(now_ms - _LATENCY_LOOKBACK_MS)[-LATENCY_SAMPLE_COUNT:]
        except StoreError as error:
            _log.error("telegram_health_store_failed", error_type=type(error).__name__)
            lines.append(
                f"Veritabanı: HATA ({type(error).__name__}) — sinyaller işlenemiyor olabilir; "
                "log'a bak"
            )
        else:
            lines += [
                "Veritabanı: tamam",
                f"Sinyal kuyruğu: {depth}",
                f"Telegram kutusu: {outbox[OutboxStatus.PENDING]} bekliyor · "
                f"{outbox[OutboxStatus.SENT]} gönderildi · "
                f"{outbox[OutboxStatus.EXPIRED]} süresi doldu · "
                f"{outbox[OutboxStatus.FAILED]} başarısız",
                f"Son sinyal alert'i: {self._when(last_alert_ms, now_ms)}",
                f"Son heartbeat: {self._when(last_heartbeat_ms, now_ms)}",
                _latency_line(samples),
            ]
        window_open = watch_window_open(deps.tv.heartbeat, now_ms)
        lines.append(
            "İzleme penceresi: açık (alarm hattının sessizliği izleniyor)"
            if window_open
            else "İzleme penceresi: kapalı"
        )
        rejected = [
            f"{code} {count}" for code, count in sorted(deps.reject_counts().items()) if count
        ]
        lines.append(
            "Reddedilen webhook istekleri: " + (" · ".join(rejected) if rejected else "yok")
        )
        lines.append(f"Yetkisiz sohbetlerden mesaj: {self._unauthorized}")
        lines.append(_clock_line(call.age_ms))
        lines.append(f"Venue kuralları: {self._rules_text(now_ms)}")
        verified = deps.tv.webhook.rules_verified_at
        verified_age = (utc_from_ms(now_ms).date() - verified).days
        age_note = f"{verified_age} gün önce" if verified_age >= 0 else "İLERİ TARİHLİ: düzelt"
        lines.append(
            f"TradingView webhook kuralları: {verified.isoformat()} tarihinde doğrulandı "
            f"({age_note})"
        )
        return "\n".join(lines)

    # -- commands that only reduce risk (applied whatever their age) -------------------------

    def _pause(self, call: _Call) -> str:
        with self._deps.store.transaction() as tx:
            already = _flag(tx, StateKey.PAUSED)
            killed = _flag(tx, StateKey.KILLED)
            tx.set_state(StateKey.PAUSED, _FLAG_SET, call.now_ms)
        head = "Zaten duraklatılmış" if already else "Duraklatıldı"
        resume = "KILL de etkin; açmak için: /devam <PIN>" if killed else "Açmak için: /devam"
        text = f"{head}: yeni GİRİŞ sinyalleri iletilmez. {_ENTRY_ONLY_NOTE} {resume}"
        return text + _late_note(call)

    def _kill(self, call: _Call) -> str:
        if not call.args:
            return _KILL_USAGE
        with self._deps.store.transaction() as tx:
            check = self._check_pin(tx, call.args, call.now_ms)
            if not check.ok:
                return check.refusal + " PIN'siz acil durdurma: /duraklat"
            already = _flag(tx, StateKey.KILLED)
            tx.set_state(StateKey.KILLED, _FLAG_SET, call.now_ms)
        _log.warning("telegram_kill_set", already=already)
        head = "KILL zaten etkindi" if already else "KILL etkin"
        return (
            f"{head}: giriş sinyalleri kesildi. Açık pozisyonun ya da bekleyen emrin varsa "
            f"platformda ELLE kapat (bot emir göndermez). {_ENTRY_ONLY_NOTE} "
            "Yeniden açmak için yalnızca: /devam <PIN>" + _late_note(call)
        )

    # -- commands that give risk back (refused when the message is old) ----------------------

    def _resume(self, call: _Call) -> str:
        with self._deps.store.transaction() as tx:
            killed = _flag(tx, StateKey.KILLED)
            paused = _flag(tx, StateKey.PAUSED)
            if killed:
                if not call.args:
                    return "KILL etkin: yeniden açmak için PIN gerekir → /devam <PIN>"
                check = self._check_pin(tx, call.args, call.now_ms)
                if not check.ok:
                    return check.refusal + " KILL etkin kalıyor."
                tx.delete_state(StateKey.KILLED)
            tx.delete_state(StateKey.PAUSED)
        if killed:
            _log.warning("telegram_kill_cleared")
            return "KILL kaldırıldı: giriş sinyalleri yeniden iletilecek."
        if paused:
            return "Devam: giriş sinyalleri yeniden iletilecek."
        return "Zaten aktif: giriş sinyalleri iletiliyor."

    def _skipped(self, call: _Call) -> str:
        """`/atladim [id]`: the entry was not taken; release its risk from the ledger."""
        if call.args and _parse_id(call.args) is None:
            return _ATLADIM_USAGE
        signal_id = _parse_id(call.args) if call.args else None
        today = self._prop_day(call.now_ms)
        with self._deps.store.transaction() as tx:
            if signal_id is None:
                entry = _latest_delivered_entry(tx, today)
                if entry is None:
                    return (
                        f"Bugün (prop günü {today.isoformat()}) sana ulaşmış bir giriş sinyali "
                        "yok. Belirli bir giriş için: /atladim <id>"
                    )
            else:
                entry = tx.get_budget_entry(signal_id)
                if entry is None:
                    return _no_ledger_text(signal_id)
            label = _entry_label(entry.signal_id, tx.get_signal(entry.signal_id))
            if not entry.counted:
                return (
                    f"{label}: zaten serbest ({_release_label(entry)}); değişiklik yok. "
                    f"Geri almak için: /aldim {entry.signal_id}"
                )
            tx.set_budget_counted(
                signal_id=entry.signal_id,
                counted=False,
                reason=ReleaseReason.SKIPPED,
                now_ms=call.now_ms,
            )
            budget_line = self._budget_line(tx, entry, today)
        _log.info("telegram_entry_released", signal_id=entry.signal_id)
        return (
            f"{label}: {self._money(entry.risk_amount)} serbest bırakıldı; bu girişin stop/çıkış "
            f"mesajları artık gelmeyecek. Yanlışsa: /aldim {entry.signal_id}\n{budget_line}"
        )

    def _taken(self, call: _Call) -> str:
        """`/aldim <id>`: undo a release (`/atladim` or non-delivery); the risk counts again."""
        signal_id = _parse_id(call.args)
        if signal_id is None:
            return _ALDIM_USAGE
        today = self._prop_day(call.now_ms)
        with self._deps.store.transaction() as tx:
            entry = tx.get_budget_entry(signal_id)
            if entry is None:
                return _no_ledger_text(signal_id)
            label = _entry_label(signal_id, tx.get_signal(signal_id))
            if entry.counted:
                return f"{label}: zaten bütçede sayılıyor; değişiklik yok."
            tx.set_budget_counted(
                signal_id=signal_id, counted=True, reason=None, now_ms=call.now_ms
            )
            budget_line = self._budget_line(tx, entry, today)
        _log.info("telegram_entry_recounted", signal_id=signal_id)
        return (
            f"{label}: {self._money(entry.risk_amount)} yeniden bütçede sayılıyor; bu girişin "
            f"stop/çıkış mesajları yeniden iletilecek.\n{budget_line}"
        )


def _freshness_refusal(name: str, call: _Call) -> str | None:
    """Why a command that gives risk back is not applied, or None when its age is fine."""
    if call.age_ms > STALE_COMMAND_MS:
        age = fmt_duration(call.age_ms / _SECOND_MS)
        return (
            f"/{name}: komut eski, yok sayıldı (gönderileli {age} olmuş). "
            "Hâlâ geçerliyse yeniden gönder."
        )
    if call.age_ms < -STALE_COMMAND_MS:
        # The server clock is far behind Telegram's: with the clock in doubt nothing that
        # lets new entries through is applied (CLAUDE.md invariant 3).
        return (
            f"/{name}: komut yok sayıldı — mesajın saati sunucu saatinden "
            f"{fmt_duration(-call.age_ms / _SECOND_MS)} ileride, sunucu saati kaymış olabilir. "
            "/saglik ile kontrol et."
        )
    return None


def _pin_locked_text(remaining_ms: int) -> str:
    return (
        "PIN girişi kilitli (çok fazla hatalı deneme); "
        f"{fmt_duration(remaining_ms / _SECOND_MS)} sonra yeniden dene."
    )


def _no_ledger_text(signal_id: int) -> str:
    return f"id {signal_id} için bütçe kaydı yok (iletilmiş bir giriş sinyali değil)."


def _last_strategy_alert_ms(tx: Tx) -> int | None:
    """When the last alert that is not a heartbeat was received."""
    stamps = [tx.last_received_ms(action=action) for action in _STRATEGY_ACTIONS]
    return max((stamp for stamp in stamps if stamp is not None), default=None)


def _latest_delivered_entry(tx: Tx, day: dt.date) -> BudgetRow | None:
    """Newest entry of the prop day whose signal message reached Telegram.

    An entry whose message is still pending (or was never delivered) is skipped: the user
    cannot mean a signal they have not seen.
    """
    for entry in reversed(tx.budget_entries(day)):
        delivered = any(
            message.kind is OutboxKind.SIGNAL and message.status is OutboxStatus.SENT
            for message in tx.messages_for_signal(entry.signal_id)
        )
        if delivered:
            return entry
    return None


def _latency_line(samples: list[LatencySample]) -> str:
    if not samples:
        return "Gecikme medyanı: ölçüm yok"
    received = statistics.median(sample.received_at_ms - sample.bar_close_ms for sample in samples)
    sent = [
        sample.sent_at_ms - sample.bar_close_ms
        for sample in samples
        if sample.sent_at_ms is not None
    ]
    telegram = f"{_seconds(statistics.median(sent))} ({len(sent)} mesaj)" if sent else "ölçüm yok"
    return (
        f"Gecikme medyanı (son {len(samples)} sinyal): bar kapanışı → alım {_seconds(received)} · "
        f"bar kapanışı → Telegram {telegram}"
    )


def _clock_line(age_ms: int) -> str:
    sign = "−" if age_ms < 0 else "+"
    line = (
        f"Saat farkı (sunucu − Telegram mesaj saati): {sign}{_seconds(abs(age_ms))} — kaba "
        "tahmin, ±2 sn (mesajın yolda geçen süresini içerir)"
    )
    if age_ms < -_CLOCK_BEHIND_TOLERANCE_MS:
        line += ". UYARI: sunucu saati geride görünüyor; chronyc tracking ile kontrol et"
    return line
