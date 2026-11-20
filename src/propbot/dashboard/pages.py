"""Server-rendered HTML of the panel: Turkish, no template engine, no inline script or style.

Every dynamic value goes through `esc` (html.escape with quotes): signal fields come from
outside (a TradingView alert) and a report file may hold anything. Pages are built from
small functions returning strings. The CSP forbids inline `<script>` and `style`
attributes, so looks come from classes in `/assets/app.css` only; bars are `<meter>`
elements with the value written next to them, and status badges always carry text.
"""

import html
from collections.abc import Iterable, Sequence
from typing import Final

from propbot.dashboard.actions import (
    BALANCE_FIELD,
    CONFIRM_FIELD,
    DAY_START_FIELD,
    EQUITY_FIELD,
    KILL_MEANING,
    PIN_FIELD,
    ActionKind,
    ActionResult,
    ControlState,
)
from propbot.dashboard.assets import CSS_PATH, JS_PATH
from propbot.dashboard.live import (
    BotState,
    DbState,
    Gauge,
    LiveView,
    SignalItem,
    TradeItem,
)
from propbot.dashboard.security import CSRF_FIELD

LIVE_PATH: Final = "/"
ANALYSIS_PATH: Final = "/analiz"
FRAGMENT_PATH: Final = "/parca/canli"
ACTION_PREFIX: Final = "/eylem/"

_BOT_TONES: Final = {
    BotState.KILLED: "bad",
    BotState.STANDBY: "warn",
    BotState.PAUSED: "warn",
    BotState.ACTIVE: "ok",
}
_DB_TONES: Final = {
    DbState.OK: "ok",
    DbState.MISSING: "warn",
    DbState.EMPTY: "warn",
    DbState.BUSY: "warn",
    DbState.ERROR: "bad",
}


def esc(value: object) -> str:
    return html.escape(str(value), quote=True)


def badge(text: str, tone: str) -> str:
    return f'<span class="badge {esc(tone)}">{esc(text)}</span>'


def layout(*, title: str, active: str, body: str) -> str:
    """The one page frame: head, navigation, main content."""
    nav = "".join(
        f'<a href="{esc(path)}"'
        + (' aria-current="page"' if key == active else "")
        + f">{esc(label)}</a>"
        for key, path, label in (
            ("canli", LIVE_PATH, "Canlı"),
            ("analiz", ANALYSIS_PATH, "Analiz"),
        )
    )
    return (
        "<!doctype html>\n"
        '<html lang="tr"><head><meta charset="utf-8">'
        '<meta name="viewport" content="width=device-width, initial-scale=1">'
        f"<title>{esc(title)} · PropBot paneli</title>"
        '<link rel="icon" href="data:,">'
        f'<link rel="stylesheet" href="{esc(CSS_PATH)}">'
        f'<script src="{esc(JS_PATH)}" defer></script>'
        "</head><body>"
        '<header class="top"><span class="brand">PropBot paneli</span>'
        f'<nav aria-label="Ana menü">{nav}</nav>'
        '<span class="mode">mod: signal — emir gönderilmez</span></header>'
        f"<main>{body}</main>"
        "<footer>Yalnızca bu bilgisayardan (loopback) erişilir · panel hiçbir yapay zekâ ya da "
        "dış servis çağırmaz</footer>"
        "</body></html>"
    )


def gauge_html(gauge: Gauge) -> str:
    classes = "gauge warn" if gauge.warn else "gauge"
    if gauge.fraction is None:
        bar = badge("hesaplanamıyor", "warn")
    else:
        percent = f"{gauge.fraction * 100:.0f}"
        bar = (
            f'<meter min="0" max="1" value="{gauge.fraction:.4f}" '
            f'aria-label="{esc(gauge.title)}">%{percent}</meter>'
        )
    return (
        f'<div class="{classes}"><div class="title">{esc(gauge.title)}</div>{bar}'
        f'<div class="value">{esc(gauge.value_text)}</div>'
        f'<div class="source">Kaynak: {esc(gauge.source)}</div></div>'
    )


def _table(headers: Sequence[str], rows: Iterable[Sequence[str]], empty: str) -> str:
    body = "".join(
        "<tr>" + "".join(f"<td>{esc(cell)}</td>" for cell in row) + "</tr>" for row in rows
    )
    if not body:
        return f'<p class="muted">{esc(empty)}</p>'
    head = "".join(f'<th scope="col">{esc(cell)}</th>' for cell in headers)
    return (
        f'<div class="table-wrap"><table><thead><tr>{head}</tr></thead>'
        f"<tbody>{body}</tbody></table></div>"
    )


def _signal_rows(items: Sequence[SignalItem]) -> list[list[str]]:
    return [
        [str(i.id), i.time_text, i.strategy, i.symbol, i.action, i.side, i.outcome, i.delivery]
        for i in items
    ]


def _trade_rows(items: Sequence[TradeItem]) -> list[list[str]]:
    return [
        [
            str(t.signal_id),
            t.opened_text,
            t.strategy,
            t.symbol,
            t.side,
            t.levels,
            t.state,
            t.closed_text,
            t.ledger_mark,
        ]
        for t in items
    ]


_TRADE_HEADERS: Final = (
    "id",
    "Giriş sinyali",
    "Strateji",
    "Sembol",
    "Yön",
    "Seviyeler",
    "Durum",
    "Kapanış",
    "Bütçe defteri",
)


def _status_card(view: LiveView) -> str:
    parts = [f"<p>Veritabanı: {badge(view.db.message, _DB_TONES[view.db.state])}</p>"]
    if view.bot is not None:
        also = f" (ayrıca: {', '.join(view.bot.also)})" if view.bot.also else ""
        parts.append(
            f"<p>Bot durumu: {badge(view.bot.label, _BOT_TONES[view.bot.state])} "
            f"{esc(view.bot.detail)}{esc(also)}</p>"
        )
    if view.service is not None:
        tone = "ok" if view.service.alive else "bad"
        parts.append(
            f"<p>Servis (propbot serve): {badge(view.service.label, tone)} "
            f"{esc(view.service.detail)}</p>"
        )
    parts.append(f"<p>Telegram giden kutusu: {esc(view.outbox_text)}</p>")
    parts.append(
        f'<p class="muted">Mod: {esc(view.mode_text)} · Venue: {esc(view.venue_name)} '
        f"({esc(view.venue_title)}) · prop günü {esc(view.prop_day)} · {esc(view.now_text)}</p>"
    )
    return (
        '<section class="card" aria-labelledby="h-durum"><h2 id="h-durum">Durum</h2>'
        + "".join(parts)
        + "</section>"
    )


def _account_card(view: LiveView) -> str:
    account = view.account
    tone = "warn" if account.stale else ("info" if account.manual else "warn")
    label = (
        "güncel değil" if account.stale else ("elle girildi" if account.manual else "varsayılan")
    )
    parts = [
        f"<p>Bakiye: <strong>{esc(account.balance_text)}</strong> · özsermaye "
        f"<strong>{esc(account.equity_text)}</strong> {badge(label, tone)}</p>",
        f'<p class="source">Kaynak: {esc(account.source)}</p>',
    ]
    if account.note:
        parts.append(f'<p class="banner warn">{esc(account.note)}</p>')
    parts.extend(gauge_html(gauge) for gauge in account.targets)
    parts.append(gauge_html(account.total_loss))
    return (
        '<section class="card" aria-labelledby="h-hesap"><h2 id="h-hesap">Hesap</h2>'
        + "".join(parts)
        + "</section>"
    )


def _daily_card(view: LiveView) -> str:
    account = view.account
    parts = [
        "<h3>(a) Botun risk defteri</h3>",
        *(gauge_html(gauge) for gauge in account.ledger),
        "<h3>(b) Beyan edilen günlük kayıp</h3>",
    ]
    if account.declared_daily is None:
        parts.append(f'<p class="muted">{esc(account.declared_daily_note)}</p>')
    else:
        parts.append(gauge_html(account.declared_daily))
    return (
        '<section class="card" aria-labelledby="h-gunluk"><h2 id="h-gunluk">Günlük tampon</h2>'
        + "".join(parts)
        + "</section>"
    )


def live_fragment(view: LiveView) -> str:
    """The part of the live screen that the script refreshes (no forms in here)."""
    banner = ""
    if not view.db.ok:
        tone = "bad" if view.db.state is DbState.ERROR else "warn"
        banner = f'<p class="banner {tone}" role="status">{esc(view.db.message)}</p>'
    signals = _table(
        ("id", "Alındı", "Strateji", "Sembol", "Tür", "Yön", "Sonuç", "Telegram"),
        _signal_rows(view.signals),
        "Gösterilecek sinyal yok (son 7 gün).",
    )
    return (
        banner
        + '<div class="grid">'
        + _status_card(view)
        + _account_card(view)
        + _daily_card(view)
        + "</div>"
        + '<section class="card" aria-labelledby="h-sinyal"><h2 id="h-sinyal">Son sinyaller</h2>'
        + signals
        + "</section>"
        + '<section class="card" aria-labelledby="h-islem">'
        + '<h2 id="h-islem">Sinyal kayıtlarından türetilen işlemler (dolum değil)</h2>'
        + f'<p class="muted">{esc(view.trades_note)}</p>'
        + "<h3>Açık</h3>"
        + _table(_TRADE_HEADERS, _trade_rows(view.open_trades), "Açık sinyal kaydı yok.")
        + "<h3>Kapalı / bilinmiyor</h3>"
        + _table(_TRADE_HEADERS, _trade_rows(view.closed_trades), "Kapalı sinyal kaydı yok.")
        + "</section>"
    )


def _form(
    kind: ActionKind,
    *,
    csrf: str,
    legend: str,
    fields: str,
    button: str,
    disabled: bool,
    danger: bool = False,
    note: str = "",
) -> str:
    button_class = ' class="danger"' if danger else ""
    disabled_attr = " disabled" if disabled else ""
    note_html = f'<p class="muted">{esc(note)}</p>' if note else ""
    return (
        f'<form method="post" action="{esc(ACTION_PREFIX + kind.value)}">'
        f"<fieldset{disabled_attr}><legend>{esc(legend)}</legend>"
        f'<input type="hidden" name="{CSRF_FIELD}" value="{esc(csrf)}">'
        f"{fields}{note_html}"
        f'<button type="submit"{button_class}>{esc(button)}</button>'
        "</fieldset></form>"
    )


def _confirm(field_id: str, text: str) -> str:
    return (
        f'<label class="check" for="{esc(field_id)}">'
        f'<input type="checkbox" id="{esc(field_id)}" name="{CONFIRM_FIELD}" value="1" required>'
        f"{esc(text)}</label>"
    )


def _pin(field_id: str) -> str:
    # No value attribute ever: the PIN is never written back into a page.
    return (
        f'<label for="{esc(field_id)}">PIN (6-12 rakam)</label>'
        f'<input type="password" id="{esc(field_id)}" name="{PIN_FIELD}" inputmode="numeric" '
        'autocomplete="off" pattern="[0-9]{6,12}" required>'
    )


def _amount(field_id: str, name: str, label: str, *, required: bool) -> str:
    required_attr = " required" if required else ""
    return (
        f'<label for="{esc(field_id)}">{esc(label)}</label>'
        f'<input type="text" id="{esc(field_id)}" name="{esc(name)}" inputmode="decimal" '
        f'autocomplete="off"{required_attr}>'
    )


def controls_html(controls: ControlState, csrf: str) -> str:
    """The control forms; outside the refreshed fragment so typing is never interrupted."""
    off = not controls.enabled
    pin_off = off or not controls.pin_enabled
    head = []
    if controls.reason:
        head.append(f'<p class="banner warn" role="status">{esc(controls.reason)}</p>')
    if controls.pin_reason:
        head.append(f'<p class="banner warn" role="status">{esc(controls.pin_reason)}</p>')
    forms = [
        _form(
            ActionKind.PAUSE,
            csrf=csrf,
            legend="Duraklat",
            fields=_confirm("onay-duraklat", "Yerel onay: giriş sinyallerini durdur"),
            button="Duraklat",
            disabled=off,
            note="Stop güncelleme / çıkış / iptal mesajları gelmeye devam eder.",
        ),
        _form(
            ActionKind.RESUME,
            csrf=csrf,
            legend="Devam Et",
            fields=_confirm("onay-devam", "Yerel onay: giriş sinyallerini yeniden ilet"),
            button="Devam Et",
            disabled=off,
            note="KILL etkinse işe yaramaz: KILL yalnızca PIN ile kaldırılır.",
        ),
        _form(
            ActionKind.KILL,
            csrf=csrf,
            legend="Acil Kapat (KILL)",
            fields=_pin("pin-kill"),
            button="Acil Kapat (KILL)",
            disabled=pin_off,
            danger=True,
            note=KILL_MEANING,
        ),
        _form(
            ActionKind.UNKILL,
            csrf=csrf,
            legend="KILL'i kaldır",
            fields=_pin("pin-devam"),
            button="KILL'i kaldır",
            disabled=pin_off,
            note="Telegram'daki /devam <PIN> ile aynıdır; PIN kilidi ortaktır.",
        ),
        _form(
            ActionKind.SUMMARY,
            csrf=csrf,
            legend="Telegram Durum/Test Özeti Gönder",
            fields="",
            button="Özeti gönder",
            disabled=off,
            note="Çalışan propbot serve, özeti tek yetkili sohbete gönderir.",
        ),
        _form(
            ActionKind.BALANCE,
            csrf=csrf,
            legend="Bakiye kaydet (elle, beyan)",
            fields=(
                _amount("f-bakiye", BALANCE_FIELD, "Güncel bakiye", required=True)
                + _amount(
                    "f-ozsermaye", EQUITY_FIELD, "Güncel özsermaye (boşsa bakiye)", required=False
                )
                + _amount(
                    "f-gunbasi", DAY_START_FIELD, "Gün başı bakiyesi (isteğe bağlı)", required=False
                )
                + _confirm("onay-bakiye", "Yerel onay: bu değerleri kaydet")
            ),
            button="Bakiye kaydet",
            disabled=off,
            note="Bot gerçek bakiyeyi bilmez; bu değer senin beyanındır ve prop günü değişince "
            '"güncel değil" olur.',
        ),
    ]
    return (
        '<section class="card" aria-labelledby="h-kontrol"><h2 id="h-kontrol">Yerel kontrol</h2>'
        + "".join(head)
        + '<div class="grid">'
        + "".join(forms)
        + "</div></section>"
    )


def live_page(view: LiveView, controls: ControlState, csrf: str) -> str:
    body = (
        '<p id="conn" role="status" hidden></p>'
        f'<div id="live" data-src="{esc(FRAGMENT_PATH)}">'
        + live_fragment(view)
        + "</div>"
        + controls_html(controls, csrf)
    )
    return layout(title="Canlı Hesap & Sinyal", active="canli", body=body)


def analysis_page(inner: str) -> str:
    """`inner` is HTML produced (and escaped) by `dashboard.analysis`."""
    return layout(title="Analiz & Simülasyon", active="analiz", body=inner)


def result_page(result: ActionResult) -> str:
    tone = "ok" if result.status == 200 else ("bad" if result.status == 500 else "warn")
    outcome = {200: "yapıldı", 500: "sonucu bilinmiyor"}.get(result.status, "yapılmadı")
    notice = f"<p>{esc(result.notice)}</p>" if result.notice else ""
    body = (
        '<section class="card" aria-labelledby="h-sonuc">'
        f'<h1 id="h-sonuc">{esc(result.title)} {badge(outcome, tone)}</h1>'
        f'<pre class="reply">{esc(result.reply)}</pre>'
        f"{notice}"
        f'<p><a href="{esc(LIVE_PATH)}">Canlı ekrana dön</a></p>'
        "</section>"
    )
    return layout(title=result.title, active="canli", body=body)


def message_page(title: str, text: str) -> str:
    body = f'<section class="card"><h1>{esc(title)}</h1><p>{esc(text)}</p></section>'
    return layout(title=title, active="", body=body)
