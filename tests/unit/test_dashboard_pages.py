"""Rendered HTML: no inline script or style, every hostile value escaped, honest wording."""

import datetime as dt
from collections.abc import Iterator
from html.parser import HTMLParser
from pathlib import Path

import pytest

from propbot.dashboard import pages
from propbot.dashboard.actions import KILL_MEANING, ActionKind, ActionResult, control_state
from propbot.dashboard.analysis import render_analysis
from propbot.dashboard.live import DB_OK, DbState, DbStatus, LiveView, StoreSource, load_view
from propbot.risk.loader import load_risk_config, load_venue_profile
from propbot.store.db import Store
from support import tg
from support.alerts import BAR_TIME_MS
from support.paths import CONFIG_FIXTURES, REPO_ROOT

NOW_MS = BAR_TIME_MS + 2 * 60 * 60_000
TODAY = dt.date(2026, 7, 21)
RISK = load_risk_config(CONFIG_FIXTURES / "valid" / "risk.yaml")
VENUE = load_venue_profile(CONFIG_FIXTURES / "valid" / "venues" / "sample_2step.yaml")
CSRF = "csrf-token-value_0123456789abcdefghijklmnopqrstu"
HOSTILE = '<script>alert("x")</script>'
HOSTILE_ATTR = "\"><img src=x onerror=alert(1)>'"


class Collector(HTMLParser):
    """Every start tag with its attributes, and the text content."""

    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.tags: list[tuple[str, dict[str, str | None]]] = []
        self.text: list[str] = []

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        self.tags.append((tag, dict(attrs)))

    def handle_startendtag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        self.tags.append((tag, dict(attrs)))

    def handle_data(self, data: str) -> None:
        self.text.append(data)


def parse(document: str) -> Collector:
    collector = Collector()
    collector.feed(document)
    collector.close()
    return collector


def assert_no_inline_code(document: str) -> None:
    collector = parse(document)
    for tag, attrs in collector.tags:
        assert tag != "style"
        assert "style" not in attrs
        assert not any(name.startswith("on") for name in attrs), (tag, attrs)
        if tag == "script":
            assert attrs.get("src"), "inline <script> is blocked by the CSP"
        for value in attrs.values():
            assert not (value or "").strip().lower().startswith("javascript:")


@pytest.fixture
def store() -> Iterator[Store]:
    with Store(":memory:") as opened:
        yield opened


def view_of(store: Store) -> LiveView:
    return load_view(
        StoreSource(None, store=store),
        venue=VENUE,
        venue_name="sample_2step",
        risk=RISK,
        now_ms=NOW_MS,
    )


def full_page(store: Store) -> str:
    view = view_of(store)
    controls = control_state(unavailable_reason=None, db=view.db, pin_locked_until_ms=None)
    return pages.live_page(view, controls, CSRF)


def test_live_page_is_turkish_and_has_no_inline_code(store: Store) -> None:
    tg.forwarded_entry(store, prop_day=TODAY, now_ms=NOW_MS)
    document = full_page(store)

    assert document.startswith("<!doctype html>")
    assert '<html lang="tr">' in document
    assert_no_inline_code(document)
    assert_no_inline_code(pages.live_fragment(view_of(store)))


def test_hostile_signal_fields_are_escaped_everywhere(store: Store) -> None:
    tg.add_signal(store, symbol=HOSTILE, strategy_id=HOSTILE_ATTR, setup=HOSTILE)
    tg.forwarded_entry(
        store,
        prop_day=TODAY,
        now_ms=NOW_MS,
        symbol=HOSTILE_ATTR,
        strategy_id=HOSTILE,
        bar_time_ms=BAR_TIME_MS + 60_000,
    )

    for document in (full_page(store), pages.live_fragment(view_of(store))):
        collector = parse(document)
        assert "<script>alert" not in document
        assert "<img" not in document
        assert [tag for tag, _ in collector.tags].count("img") == 0
        assert "&lt;script&gt;alert(&quot;x&quot;)&lt;/script&gt;" in document
        text = "".join(collector.text)
        assert HOSTILE in text  # shown to the user as text, exactly
        assert HOSTILE_ATTR in text
        assert_no_inline_code(document)


def test_controls_carry_the_csrf_token_and_never_a_pin_value(store: Store) -> None:
    collector = parse(full_page(store))

    forms = [attrs for tag, attrs in collector.tags if tag == "form"]
    assert {attrs["method"] for attrs in forms} == {"post"}
    assert {attrs["action"] for attrs in forms} == {f"/eylem/{kind.value}" for kind in ActionKind}
    hidden = [
        attrs for tag, attrs in collector.tags if tag == "input" and attrs.get("type") == "hidden"
    ]
    assert len(hidden) == len(ActionKind)
    assert all(attrs == {"type": "hidden", "name": "csrf", "value": CSRF} for attrs in hidden)
    pins = [attrs for tag, attrs in collector.tags if tag == "input" and attrs.get("name") == "pin"]
    assert len(pins) == 2
    assert all(attrs["type"] == "password" and "value" not in attrs for attrs in pins)
    # Every input has a label.
    labelled = {attrs.get("for") for tag, attrs in collector.tags if tag == "label"}
    visible = [
        attrs["id"]
        for tag, attrs in collector.tags
        if tag == "input" and attrs.get("type") != "hidden"
    ]
    assert set(visible) <= labelled


def test_kill_button_says_what_kill_does_not_do(store: Store) -> None:
    document = full_page(store)

    assert KILL_MEANING in "".join(parse(document).text)
    assert "KAPATMAZ" in KILL_MEANING
    assert "emir göndermez" in KILL_MEANING


def test_forms_stay_outside_the_refreshed_fragment(store: Store) -> None:
    fragment = pages.live_fragment(view_of(store))

    assert "<form" not in fragment
    assert "<input" not in fragment


def test_read_only_panel_renders_every_control_disabled(store: Store) -> None:
    view = view_of(store)
    controls = control_state(
        unavailable_reason="sırlar okunamadı", db=view.db, pin_locked_until_ms=None
    )

    collector = parse(pages.live_page(view, controls, CSRF))

    fieldsets = [attrs for tag, attrs in collector.tags if tag == "fieldset"]
    assert len(fieldsets) == len(ActionKind)
    assert all("disabled" in attrs for attrs in fieldsets)
    assert "Panel salt-okunur: sırlar okunamadı" in "".join(collector.text)


def test_pin_lock_disables_only_the_pin_forms(store: Store) -> None:
    view = view_of(store)
    controls = control_state(unavailable_reason=None, db=DB_OK, pin_locked_until_ms=NOW_MS + 60_000)

    document = pages.live_page(view, controls, CSRF)
    collector = parse(document)

    fieldsets = [attrs for tag, attrs in collector.tags if tag == "fieldset"]
    assert sum("disabled" in attrs for attrs in fieldsets) == 2
    assert "PIN girişi kilitli" in document


def test_database_problem_is_shown_as_text_with_a_badge(tmp_path: Path) -> None:
    view = load_view(
        StoreSource(tmp_path / "none.db"), venue=VENUE, venue_name="x", risk=RISK, now_ms=NOW_MS
    )

    fragment = pages.live_fragment(view)

    assert view.db.state is DbState.MISSING
    assert "veritabanı yok — propbot serve hiç çalışmamış olabilir" in fragment
    assert 'class="banner warn"' in fragment


def test_status_badges_carry_text(store: Store) -> None:
    collector = parse(pages.live_fragment(view_of(store)))

    badges = [
        index
        for index, (tag, attrs) in enumerate(collector.tags)
        if "badge" in (attrs.get("class") or "")
    ]
    assert badges
    assert "ETKİN" in "".join(collector.text)


def test_result_page_escapes_the_reply() -> None:
    result = ActionResult(ActionKind.PAUSE, "Duraklat", 200, f"cevap {HOSTILE}", "bildirim")

    document = pages.result_page(result)

    assert "<script>alert" not in document
    assert HOSTILE in "".join(parse(document).text)
    assert_no_inline_code(document)


def test_analysis_page_wraps_the_stub_in_the_layout() -> None:
    document = pages.analysis_page(render_analysis(REPO_ROOT / "reports" / HOSTILE))

    assert 'aria-current="page">Analiz<' in document
    assert "<script>alert" not in document
    assert_no_inline_code(document)


def test_message_page_escapes() -> None:
    assert "<script>alert" not in pages.message_page(HOSTILE, HOSTILE)


def test_db_status_ok_constant() -> None:
    assert DbStatus(DbState.OK, "x").ok
