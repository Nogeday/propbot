"""Telegram message texts of the signal bot (SPEC §5.4, §5.5). Turkish, plain text.

Pure builders: no I/O and no clock reads (instants arrive as UTC epoch milliseconds). Every
number and time is printed through `propbot.notify.format`.

The trader executes by hand on another instrument than the one TradingView charts, so levels
are given twice: as TradingView prices (to recognise the setup) and as point offsets from the
trader's OWN entry price (what is typed into the terminal). Builders tolerate missing optional
fields: an exit alert may carry no level at all.
"""

import datetime as dt
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from types import MappingProxyType
from typing import Final

from propbot.notify.format import (
    ISTANBUL,
    NEW_YORK,
    fmt_clock,
    fmt_date,
    fmt_duration,
    fmt_lots,
    fmt_money,
    fmt_percent,
    fmt_price,
    fmt_signed_points,
)
from propbot.risk.config import RiskConfig
from propbot.risk.sizing import SizeSuggestion, value_per_point_per_lot
from propbot.risk.venue import RULES_MAX_AGE_DAYS, RulesFreshness, RulesState, VenueProfile
from propbot.signals.schema import STRATEGY_NAMES, Action, OrderType, Side
from propbot.store.db import SignalRow

RULES_UNVERIFIED_BANNER: Final = (
    "[KURALLAR DOĞRULANMADI — boyut ve bütçe doğrulanmamış kurallara göre]"
)
ORPHAN_NOTE: Final = "(bu bot giriş sinyalini görmedi)"
ORPHAN_STOP_NOTE: Final = f"{ORPHAN_NOTE} Yalnızca mevcut stop'undan DAHA SIKIysa uygula."
UNDELIVERED_ENTRY_NOTE: Final = (
    "(giriş mesajı sana ulaşmamış olabilir; pozisyon açmadıysan yok say)"
)

# Telegram refuses texts above 4096 characters; the reminder stays well below.
EXPIRY_REMINDER_MAX_CHARS: Final = 3500

_UNKNOWN: Final = "?"
_NO_FILL_ALERTS_LINE: Final = (
    "TradingView'da stop/TP dolunca mesaj gelmez; pozisyonun hâlâ açıksa seans sonunda "
    "kendin kapat."
)
# Extra line of the entry message, per strategy (asian_sweep has none).
_STRATEGY_NOTES: Final[Mapping[str, str]] = MappingProxyType(
    {
        "ny_orb_fvg": "trailing stop güncellemeleri ayrı mesajla gelir.",
        "ict_silver_bullet": "limit dolmazsa İPTAL mesajı gelir.",
        "mr_bb_kc_rsi2": "TP dinamiktir (BB ortası, her bar değişir); TradingView'dan izle.",
        "mtf_trend_cont": (
            "TP = TP1 (kısmi çıkış seviyesi; kapatılacak pay stratejinin scale-out girdisidir, "
            "TradingView'dan oku); kalan kısım için stop güncellemeleri gelir."
        ),
    }
)
_ACTION_NAMES: Final[Mapping[str, str]] = MappingProxyType(
    {
        Action.ENTRY.value: "giriş",
        Action.STOP_UPDATE.value: "stop güncelleme",
        Action.EXIT.value: "çıkış",
        Action.CANCEL.value: "iptal",
        Action.HEARTBEAT.value: "heartbeat",
    }
)
_MAX_NAME_CHARS: Final = 64
_MAX_FIELD_CHARS: Final = 40
_PROCESSING_ERROR_ADVICE: Final = (
    "TradingView Alert Log'dan bu alarmı elle kontrol et; açık pozisyonun varsa stop/çıkışı "
    "TradingView'dan izle.\n"
    "Ayrıntı sunucu logunda: signal_processing_failed."
)


@dataclass(frozen=True, slots=True, kw_only=True)
class TradeLink:
    """How a follow-up alert (stop update, exit, cancel) relates to the entry that was sent."""

    entry_id: int | None = None  # None: this bot never saw (or cannot match) the entry
    entry_undelivered: bool = False  # the entry message provably did not reach the user in time
    late_seconds: float | None = None  # set when the alert is older than the validity limit


# --------------------------------------------------------------------------------------------
# Small formatting helpers (all tolerate missing values)
# --------------------------------------------------------------------------------------------


def _strategy_name(strategy_id: str) -> str:
    return STRATEGY_NAMES.get(strategy_id, strategy_id)


def _side_label(side: str | None) -> str:
    return _UNKNOWN if not side else side.upper()


def _price(value: float | None) -> str:
    return _UNKNOWN if value is None else fmt_price(value)


def _points(value: float | None) -> str:
    return _UNKNOWN if value is None else fmt_signed_points(value)


def _difference(level: float | None, base: float | None) -> float | None:
    return None if level is None or base is None else level - base


def _plain_amount(amount: float) -> str:
    """An amount without its currency sign, for a formula the user evaluates: 250.0 -> 250."""
    return fmt_money(amount, "USD").removeprefix("$")


def _venue_target(venue: VenueProfile) -> str:
    return f"{venue.venue.upper()} {venue.symbol}'te"


def _bar_times(bar_time_ms: int) -> str:
    return f"bar {fmt_clock(bar_time_ms, NEW_YORK)} NY / {fmt_clock(bar_time_ms, ISTANBUL)} TR"


def _id_line(row: SignalRow, entry_id: int | None = None) -> str:
    reference = "" if entry_id is None else f" (giriş id {entry_id})"
    return f"id {row.id}{reference} · {_bar_times(row.bar_time_ms)}"


def _entry_header(tag: str, row: SignalRow) -> str:
    parts = [_strategy_name(row.strategy_id), row.setup, _side_label(row.side), row.symbol]
    return f"{tag} " + " · ".join(part for part in parts if part)


def _follow_up_header(tag: str, row: SignalRow, link: TradeLink) -> str:
    late = "" if link.late_seconds is None else f"[GEÇ GELDİ +{fmt_duration(link.late_seconds)}] "
    name = _strategy_name(row.strategy_id)
    return f"{late}{tag} {name} · {_side_label(row.side)} · {row.symbol}"


def _follow_up(
    tag: str, row: SignalRow, link: TradeLink, body: Sequence[str], *, orphan_note: str
) -> str:
    lines = [_follow_up_header(tag, row, link), *body]
    if link.entry_id is None:
        lines.append(orphan_note)
    elif link.entry_undelivered:
        lines.append(UNDELIVERED_ENTRY_NOTE)
    lines.append(_id_line(row, link.entry_id))
    return "\n".join(lines)


def _stop_remark(side: str | None, offset: float | None) -> str:
    """Suffix for a stop expressed as an offset from the trader's own entry price."""
    if offset is None:
        return ""
    if offset == 0:
        return " (başabaş)"
    locked = (side == Side.LONG.value and offset > 0) or (side == Side.SHORT.value and offset < 0)
    return " (kâr kilitli)" if locked else ""


# --------------------------------------------------------------------------------------------
# Entry signals
# --------------------------------------------------------------------------------------------


def _size_line(
    row: SignalRow,
    *,
    venue: VenueProfile,
    size: SizeSuggestion,
    risk_amount: float,
    stop_points: float | None,
    budget_used_after: float,
    budget_cap: float,
) -> str:
    currency = venue.account_currency
    risk_text = (
        f"{fmt_money(risk_amount, currency)} "
        f"({fmt_percent(risk_amount / venue.initial_balance * 100.0)})"
    )
    budget_text = (
        f"günlük bütçe {fmt_money(budget_used_after, currency)}/"
        f"{fmt_money(budget_cap, currency)} (bu sinyal dahil)"
    )
    if size.lots is None:
        return (
            "Önerilen boyut: hesaplanamadı (venue profilinde contract.* boş) · "
            f"en fazla risk {risk_text} → lot = {_plain_amount(risk_amount)} ÷ "
            f"({_price(stop_points)} × 1 lotun 1 puanlık değeri), AŞAĞI yuvarla · {budget_text}"
        )
    nominal_text = ""
    point_value = value_per_point_per_lot(venue.contract)
    if point_value is not None and row.est_entry is not None:
        nominal = size.lots * row.est_entry * point_value
        # One decimal on purpose: the ratio only has to make an absurd size visible.
        nominal_text = (
            f" (≈ {fmt_money(nominal, currency)} nominal, "
            f"{nominal / venue.initial_balance:.1f}× hesap)"
        )
    return (
        f"Önerilen boyut: {fmt_lots(size.lots)} lot{nominal_text} · risk ≈ {risk_text} · "
        f"{budget_text}"
    )


def entry_signal(
    row: SignalRow,
    *,
    risk: RiskConfig,
    venue: VenueProfile,
    size: SizeSuggestion,
    risk_amount: float,
    budget_used_after: float,
    budget_cap: float,
    valid_until_ms: int,
    rules_unverified: bool = False,
) -> str:
    """The [SİNYAL] message of a forwarded entry.

    `risk_amount` is what the ledger counts for this entry; `budget_used_after` includes it.
    The reward multiple is computed from the levels, never read from the alert's `rr`.
    """
    entry, stop, tp = row.est_entry, row.stop, row.tp
    stop_offset = _difference(stop, entry)
    tp_offset = _difference(tp, entry)
    stop_points = None if stop_offset is None else abs(stop_offset)
    is_limit = row.order_type == OrderType.LIMIT.value
    venue_name = venue.venue.upper()
    valid_until = fmt_clock(valid_until_ms, ISTANBUL, seconds=True)

    if is_limit:
        entry_line = (
            f"Limit giriş {_price(entry)} (TradingView seviyesi; {venue_name} fiyatı baz farkı "
            "kadar kayabilir — seviyeyi iki grafik arasındaki farkla çevir)"
        )
    else:
        entry_line = f"Referans giriş ≈ {_price(entry)} (TradingView bar kapanışı)"

    stop_text = f"Stop {_price(stop)} ({_points(stop_offset)} puan)"
    if tp is None:
        levels_line = f"{stop_text} · TP yok"
        bracket = "SL'yi emirle BİRLİKTE gir"
        offsets = f"stop {_points(stop_offset)} puan, TP yok"
    else:
        multiple = ""
        if tp_offset is not None and stop_points:
            multiple = f", {fmt_price(round(abs(tp_offset) / stop_points, 2))}R"
        levels_line = f"{stop_text} · TP {fmt_price(tp)} ({_points(tp_offset)} puan{multiple})"
        bracket = "SL/TP'yi emirle BİRLİKTE gir"
        offsets = f"stop {_points(stop_offset)} puan, TP {_points(tp_offset)} puan"
    order = "LİMİT" if is_limit else "PİYASA"
    basis = "limit fiyatına göre" if is_limit else "giriş anındaki fiyata göre"
    order_line = f"{_venue_target(venue)} {order} emri; {bracket}: {basis} {offsets}"

    if is_limit:
        validity_line = f"Emri {valid_until} TR'ye kadar gir; İPTAL mesajı gelene kadar bekler"
    elif entry is not None and stop_points is not None:
        drift = risk.signal_max_drift_frac_of_stop * stop_points
        validity_line = (
            f"Geçerlilik: {valid_until} TR'ye kadar ve TradingView'da ({row.symbol}) fiyat "
            f"{fmt_price(entry - drift)}–{fmt_price(entry + drift)} aralığında — değilse ATLA"
        )
    else:
        validity_line = f"Geçerlilik: {valid_until} TR'ye kadar — sonrasında ATLA"

    lines = [RULES_UNVERIFIED_BANNER] if rules_unverified else []
    lines += [
        _entry_header("[SİNYAL]", row),
        entry_line,
        levels_line,
        order_line,
        _size_line(
            row,
            venue=venue,
            size=size,
            risk_amount=risk_amount,
            stop_points=stop_points,
            budget_used_after=budget_used_after,
            budget_cap=budget_cap,
        ),
        validity_line,
        _NO_FILL_ALERTS_LINE,
    ]
    note = _STRATEGY_NOTES.get(row.strategy_id)
    if note is not None:
        lines.append(f"Not: {note}")
    lines.append(_id_line(row))
    return "\n".join(lines)


def entry_expired_notice(row: SignalRow, *, valid_until_ms: int) -> str:
    """Sent instead of an entry signal that could not be delivered before its deadline."""
    return (
        f"[İLETİLEMEDİ — ATLA] id {row.id} · {_strategy_name(row.strategy_id)} "
        f"{_side_label(row.side)}: sinyal Telegram'a zamanında ulaşmadı (geçerlilik "
        f"{fmt_clock(valid_until_ms, ISTANBUL, seconds=True)} TR'de bitti). Bütçeden düşüldü."
    )


def entry_stale_notice(row: SignalRow, *, delay_seconds: float, validity_seconds: float) -> str:
    """An entry processed after its validity ended: tell the user to skip it."""
    return "\n".join(
        [
            _entry_header("[BAYAT SİNYAL — ATLA]", row),
            f"Sinyal bar kapanışından {fmt_duration(delay_seconds)} sonra işlenebildi "
            f"(geçerlilik sınırı {fmt_duration(validity_seconds)}). Fiyat artık referans değil: "
            "bu girişi ALMA.",
            _id_line(row),
        ]
    )


def entry_clock_skip_notice(row: SignalRow, *, early_seconds: float) -> str:
    """An entry that arrived before its bar closed: the clocks disagree, so nothing is valid."""
    return "\n".join(
        [
            _entry_header("[SİNYAL — ATLA]", row),
            "Sunucu saati TradingView ile uyuşmuyor (alert bar kapanışından "
            f"{fmt_duration(early_seconds)} önce geldi); geçerlilik hesaplanamıyor. "
            "/saglik ile saati kontrol et.",
            _id_line(row),
        ]
    )


def entry_risk_too_wide_notice(
    row: SignalRow, *, venue: VenueProfile, size: SizeSuggestion, stop_points: float
) -> str:
    """Even the minimum lot would risk more than the per-trade budget."""
    currency = venue.account_currency
    budget = fmt_money(size.risk_budget, currency)
    min_lot = venue.contract.min_lot
    point_value = value_per_point_per_lot(venue.contract)
    if min_lot is None or point_value is None:
        reason = f"en küçük lot bile işlem başı risk sınırını ({budget}) aşıyor"
    else:
        at_min_lot = fmt_money(min_lot * stop_points * point_value, currency)
        reason = (
            f"en küçük lot ({fmt_lots(min_lot)}) bile ≈ {at_min_lot} risk taşır; "
            f"işlem başı sınır {budget}"
        )
    return "\n".join(
        [
            _entry_header("[SİNYAL — ATLA]", row),
            f"Stop çok geniş ({fmt_price(stop_points)} puan): {reason}. Bu girişi ALMA.",
            _id_line(row),
        ]
    )


def entry_budget_skip_notice(
    row: SignalRow,
    *,
    venue: VenueProfile,
    budget_used: float,
    risk_amount: float,
    budget_cap: float,
) -> str:
    """The daily budget cannot take this entry."""
    currency = venue.account_currency
    return "\n".join(
        [
            _entry_header("[SİNYAL — ATLA]", row),
            f"Günlük risk bütçesi yetmiyor: kullanılan {fmt_money(budget_used, currency)} + "
            f"bu sinyal {fmt_money(risk_amount, currency)} > sınır "
            f"{fmt_money(budget_cap, currency)}. Bu girişi ALMA.",
            "Bugün almadığın bir giriş bütçede sayılıyorsa serbest bırak: /atladim <id>",
            _id_line(row),
        ]
    )


# --------------------------------------------------------------------------------------------
# Follow-ups of an open trade: stop update, exit, cancel
# --------------------------------------------------------------------------------------------


def stop_update_signal(
    row: SignalRow, *, venue: VenueProfile, link: TradeLink, current_offset: float | None
) -> str:
    """The [STOP GÜNCELLE] message. Offsets are measured from the trader's own entry price.

    `current_offset` is the stop the user was last told to hold (None when unknown).
    """
    new_offset = _difference(row.stop, row.est_entry)
    change = "" if current_offset is None else f"{fmt_signed_points(current_offset)} → "
    body = [
        f"Stop: kendi giriş fiyatına göre {change}{_points(new_offset)} puan "
        f"(TradingView seviyesi {_price(row.stop)})",
        f"{_venue_target(venue)} uygula: stop = kendi giriş fiyatın {_points(new_offset)} puan"
        f"{_stop_remark(row.side, new_offset)}",
    ]
    orphan_note = ORPHAN_STOP_NOTE if current_offset is None else ORPHAN_NOTE
    return _follow_up("[STOP GÜNCELLE]", row, link, body, orphan_note=orphan_note)


def stop_widen_warning(
    row: SignalRow, *, venue: VenueProfile, link: TradeLink, current_offset: float
) -> str:
    """The [UYARI] message for a stop update that would widen the trader's stop (never applied)."""
    new_offset = _difference(row.stop, row.est_entry)
    current = fmt_signed_points(current_offset)
    body = [
        f"Strateji stopu GENİŞLETMEK istiyor (kendi giriş fiyatına göre {current} → "
        f"{_points(new_offset)} puan; TradingView seviyesi {_price(row.stop)}). UYGULAMA: açık "
        "pozisyonun stopu genişletilmez.",
        f"{_venue_target(venue)} stopunu KORU: stop = kendi giriş fiyatın {current} puan"
        f"{_stop_remark(row.side, current_offset)}",
    ]
    return _follow_up("[UYARI]", row, link, body, orphan_note=ORPHAN_NOTE)


def exit_signal(row: SignalRow, *, venue: VenueProfile, link: TradeLink) -> str:
    """The [ÇIKIŞ] message of a forced flatten."""
    body = [
        "Strateji pozisyonu kapattı (seans sonu / günlük zarar kesicisi / test aralığı sonu).",
        f"{_venue_target(venue)} pozisyon açıksa PİYASADAN KAPAT.",
    ]
    return _follow_up("[ÇIKIŞ]", row, link, body, orphan_note=ORPHAN_NOTE)


def cancel_signal(
    row: SignalRow, *, venue: VenueProfile, link: TradeLink, release_hint: bool
) -> str:
    """The [İPTAL] message of a withdrawn limit entry.

    `release_hint`: the entry still counts in the daily budget, so the user is told how to
    release it (the bot never releases budget on its own guess that the order was not filled).
    """
    level = "" if row.est_entry is None else f" (seviye {fmt_price(row.est_entry)})"
    release = "."
    if release_hint and link.entry_id is not None:
        release = f" ve bütçeyi serbest bırak: /atladim {link.entry_id}"
    body = [
        f"Bekleyen limit emir geri çekildi{level}.",
        f"{_venue_target(venue)} emir dolmadıysa İPTAL ET{release}",
        "Emir dolduysa: TradingView'da bu pozisyon YOK, başka mesaj gelmeyecek — stop/TP ile "
        "yönet ve seans sonunda kendin kapat.",
    ]
    return _follow_up("[İPTAL]", row, link, body, orphan_note=ORPHAN_NOTE)


# --------------------------------------------------------------------------------------------
# Alarms raised while processing
# --------------------------------------------------------------------------------------------


def rules_stale_alarm(venue: VenueProfile, freshness: RulesFreshness) -> str:
    """Venue rules are not freshly verified: sizes and budgets rest on unverified numbers."""
    if freshness.state is RulesState.MISSING or venue.rules_verified_at is None:
        state = "rules_verified_at boş"
    elif freshness.state is RulesState.FUTURE:
        state = f"rules_verified_at ileri tarihli ({venue.rules_verified_at.isoformat()})"
    else:
        age = "" if freshness.age_days is None else f"{freshness.age_days} gün önce, "
        state = (
            f"son doğrulama {venue.rules_verified_at.isoformat()} "
            f"({age}sınır {RULES_MAX_AGE_DAYS} gün)"
        )
    return (
        f"[ALARM] {venue.venue.upper()} kuralları güncel doğrulanmamış: {state}.\n"
        "Sinyaller iletiliyor, ama önerilen boyut ve günlük bütçe doğrulanmamış kurallara göre "
        "hesaplanıyor.\n"
        "Kuralları firmanın resmi sayfasından yeniden doğrula ve venue profilindeki "
        "rules_verified_at tarihini güncelle. Bu uyarı en fazla günde bir gelir."
    )


def side_mismatch_alarm(row: SignalRow, reference: SignalRow) -> str:
    """A stop update for the other side than the entry this bot forwarded."""
    return (
        f"[ALARM] {_strategy_name(row.strategy_id)} · {row.symbol}: {_side_label(row.side)} "
        f"yönlü stop güncellemesi geldi (id {row.id}), ama son giriş (id {reference.id}) "
        f"{_side_label(reference.side)} yönlüydü. Güncelleme İLETİLMEDİ.\n"
        "Bot bir giriş sinyalini kaçırmış olabilir: TradingView'da stratejinin açık pozisyonunu "
        "ve Alert Log'u kontrol et; pozisyonun varsa stopunu TradingView'dan izle."
    )


def _safe_text(value: object, limit: int = _MAX_FIELD_CHARS) -> str:
    """Printable, bounded text of anything; never raises."""
    try:
        text = str(value)[:limit]
    except Exception:
        return _UNKNOWN
    cleaned = "".join(char if char.isprintable() else _UNKNOWN for char in text)
    return cleaned or _UNKNOWN


def _attempt(build: Callable[[], str | None]) -> str | None:
    try:
        return build()
    except Exception:
        return None


def _failed_side(row: SignalRow) -> str | None:
    return _safe_text(row.side.upper()) if row.side else None


def processing_error_alarm(row: SignalRow) -> str:
    """Alarm for a signal that could not be processed (it was set aside, nothing was sent).

    Last line of defence: it runs after processing itself failed, so it must return a
    storable text whatever the row holds (wrong types, missing values, hostile text).
    """
    parts = [
        _attempt(lambda: f"id {_safe_text(row.id)}"),
        _attempt(lambda: _safe_text(STRATEGY_NAMES.get(row.strategy_id, row.strategy_id))),
        _attempt(lambda: _safe_text(_ACTION_NAMES.get(row.action, row.action))),
        _attempt(lambda: _failed_side(row)),
        _attempt(lambda: _safe_text(row.symbol)),
        _attempt(lambda: _bar_times(row.bar_time_ms)),
    ]
    described = " · ".join(part for part in parts if part)
    detail = f" ({described})" if described else ""
    return (
        f"[ALARM] Bir TradingView alert'i işlenemedi ve İLETİLMEDİ{detail}.\n"
        f"{_PROCESSING_ERROR_ADVICE}"
    )


# --------------------------------------------------------------------------------------------
# Alert-line monitor
# --------------------------------------------------------------------------------------------


def heartbeat_silence_alarm(silent_seconds: float, limit_minutes: int) -> str:
    return (
        f"[ALARM] TradingView alarm hattı sessiz: son {fmt_duration(silent_seconds)} içinde "
        f"hiçbir alert gelmedi (sınır {limit_minutes} dk).\n"
        "Sinyaller de GELMİYOR olabilir: TradingView'da alarmların aktif olduğunu (heartbeat "
        "göstergesi dahil) ve Alert Log'u kontrol et; açık pozisyonun varsa stop/çıkışı "
        "TradingView'dan izle.\n"
        "Tatil ya da erken kapanış günüyse yok say."
    )


def heartbeat_recovered_notice() -> str:
    return "[BİLGİ] TradingView alarm hattı geri geldi: yeniden alert alınıyor."


def downtime_alarm(down_from_ms: int, down_until_ms: int) -> str:
    """The bot was not running between two instants that overlap a watch window."""
    start = fmt_clock(down_from_ms, ISTANBUL)
    end = fmt_clock(down_until_ms, ISTANBUL)
    start_day = fmt_date(down_from_ms, ISTANBUL)
    end_day = fmt_date(down_until_ms, ISTANBUL)
    # Clock times alone are ambiguous once the gap crosses midnight.
    span = f"{start}–{end}" if start_day == end_day else f"{start_day} {start} – {end_day} {end}"
    return (
        f"[ALARM] Bot {span} TR arasında kapalıydı; bu aralıktaki TradingView alarmları "
        "alınmadı. Açık pozisyonun varsa stop/çıkışı TradingView'dan kontrol et."
    )


def _expiry_line(name: str, expires_on: dt.date, days_left: int) -> str:
    if days_left < 0:
        status = f"{-days_left} gün önce BİTTİ"
    elif days_left == 0:
        status = "BUGÜN bitiyor"
    else:
        status = f"{days_left} gün kaldı"
    return f"• {_safe_text(name, _MAX_NAME_CHARS)} — {expires_on.isoformat()} ({status})"


def alert_expiry_reminder(items: Sequence[tuple[str, dt.date, int]]) -> str:
    """Reminder listing alerts as (name, expires_on, days_left); negative days = expired.

    Stays below `EXPIRY_REMINDER_MAX_CHARS`: what does not fit is counted in a last line.
    """
    header = "[HATIRLATMA] TradingView alarmlarının süresi doluyor:"
    footer = (
        "Süresi biten alarm sinyal GÖNDERMEZ: TradingView'da alarmı yeniden kur ve "
        "config/tv_alerts.yaml'daki expires_on tarihini güncelle."
    )
    lines = [_expiry_line(name, expires_on, days_left) for name, expires_on, days_left in items]

    def render(shown: int) -> str:
        body = lines[:shown]
        if shown < len(lines):
            body.append(f"… ve {len(lines) - shown} alarm daha")
        return "\n".join([header, *body, footer])

    shown = len(lines)
    while shown > 0 and len(render(shown)) >= EXPIRY_REMINDER_MAX_CHARS:
        shown -= 1
    return render(shown)
