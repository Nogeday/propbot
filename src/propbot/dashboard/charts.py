"""Inline SVG charts of the analysis screen (ADR-0009 item 9): small pure functions.

Why server-rendered SVG: the panel's CSP allows no inline script and no style attribute,
and the panel loads nothing from outside, so a chart is plain markup the server writes.
Geometry uses SVG presentation attributes only; colours come from the `.c-*` classes in
`/assets/app.css`, which have light and dark values. Every piece of text is html-escaped
(labels come from report files, which can be edited by hand). Every number is checked for
finiteness before it becomes a coordinate and every scale is guarded against a zero span,
so a degenerate artifact draws an empty or flat chart instead of raising.

Colour never carries the meaning alone: values are written next to the marks, every mark
has a `<title>` tooltip, and the analysis page shows the same numbers as tables.
"""

import html
import itertools
import math
import re
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from typing import Final

from propbot.backtest.report import tr_number

Formatter = Callable[[float], str]

WIDTH: Final = 640
FONT: Final = 12
SMALL: Final = 11
LABEL_CHARS: Final = 30
VALUE_CHARS: Final = 14
# Upper bounds on what one chart draws: a hand-edited report must not produce a page of
# megabytes. Callers bound their inputs too; these are the last line.
MAX_BARS: Final = 40
MAX_GROUPS: Final = 60
MAX_SERIES: Final = 3
MAX_BINS: Final = 200
MAX_POINTS: Final = 2000
MAX_MARKERS: Final = 6
VALUE_LABEL_LIMIT: Final = 24  # above this many bars, values live in tooltips and the table
GROUP_FILL: Final = 0.8  # share of a group's width the bars use; the rest separates groups
MIN_BIN_PX: Final = 0.5  # a histogram bin narrower than this would vanish when drawn
SERIES_CLASSES: Final = ("c-bar-a", "c-bar-b", "c-bar-c")


@dataclass(frozen=True, slots=True)
class Threshold:
    """A limit that applies to one bar only (e.g. the G4 floor of one probability)."""

    value: float
    label: str


@dataclass(frozen=True, slots=True)
class HBar:
    label: str
    value: float | None
    text: str
    threshold: Threshold | None = None


@dataclass(frozen=True, slots=True)
class Marker:
    """A vertical reference line; `limit` draws it as a rule line (e.g. the firm's max loss)."""

    value: float
    label: str
    limit: bool = False


@dataclass(frozen=True, slots=True)
class Group:
    """One x position of a grouped bar chart: one value (and its text) per series."""

    label: str
    values: tuple[float | None, ...]
    texts: tuple[str, ...]


def default_format(value: float) -> str:
    return compact_number(value, 2)


def compact_number(value: float | None, digits: int = 2) -> str:
    """Turkish number; scientific beyond 1e9 so an extreme value cannot break the layout."""
    number = finite(value)
    if number is None:
        return "—"
    if abs(number) >= 1e9:
        return f"{number:.2e}".replace(".", ",")
    return tr_number(number, digits)


def finite(value: float | None) -> float | None:
    if value is None or isinstance(value, bool):
        return None
    try:
        number = float(value)
    except (TypeError, ValueError, OverflowError):
        return None
    return number if math.isfinite(number) else None


# Characters XML 1.0 forbids (C0 controls except tab/newline/return, surrogates, U+FFFE/F):
# a hand-edited label holding one would make the inline SVG ill-formed.
_NOT_XML: Final = re.compile("[\x00-\x08\x0b\x0c\x0e-\x1f\ud800-\udfff\ufffe\uffff]")


def esc(value: object) -> str:
    return html.escape(_NOT_XML.sub("\ufffd", str(value)), quote=True)


def clip(text: str, limit: int) -> str:
    text = " ".join(str(text).split())
    if limit < 2:
        return text[:1]
    return text if len(text) <= limit else text[: limit - 1] + "…"


def scale(value: float, lo: float, hi: float, start: float, end: float) -> float:
    """Map `value` from [lo, hi] onto [start, end], clamped; a zero or bad span gives `start`."""
    span = hi - lo
    if not (math.isfinite(span) and span > 0.0) or not math.isfinite(value):
        return start
    ratio = (value - lo) / span
    if not math.isfinite(ratio):
        return start
    ratio = min(1.0, max(0.0, ratio))
    return start + ratio * (end - start)


def _c(value: float) -> str:
    return f"{value:.1f}"


def _frame(width: float, height: float, title: str, body: str, css: str = "chart") -> str:
    return (
        f'<svg class="{css}" xmlns="http://www.w3.org/2000/svg" viewBox="0 0 {_c(width)} '
        f'{_c(height)}" role="img" preserveAspectRatio="xMidYMid meet" font-size="{FONT}">'
        f"<title>{esc(title)}</title>{body}</svg>"
    )


def _text(
    x: float,
    y: float,
    content: str,
    *,
    anchor: str = "start",
    css: str = "c-text",
    size: int | None = None,
) -> str:
    size_attr = f' font-size="{size}"' if size is not None else ""
    return (
        f'<text x="{_c(x)}" y="{_c(y)}" text-anchor="{anchor}" class="{css}"{size_attr}>'
        f"{esc(content)}</text>"
    )


def _rect(x: float, y: float, w: float, h: float, css: str, tip: str = "") -> str:
    tooltip = f"<title>{esc(tip)}</title>" if tip else ""
    return (
        f'<rect x="{_c(x)}" y="{_c(y)}" width="{_c(max(0.0, w))}" height="{_c(max(0.0, h))}" '
        f'class="{css}">{tooltip}</rect>'
    )


def _line(x1: float, y1: float, x2: float, y2: float, css: str, *, dashed: bool = False) -> str:
    dash = ' stroke-dasharray="5 3"' if dashed else ""
    return f'<line x1="{_c(x1)}" y1="{_c(y1)}" x2="{_c(x2)}" y2="{_c(y2)}" class="{css}"{dash}/>'


def _anchor_for(x: float, left: float, right: float) -> str:
    """Labels near the right edge grow leftwards so they never leave the viewBox."""
    return "end" if x > (left + right) / 2.0 else "start"


def _y_label(mid_y: float, label: str) -> str:
    """The y axis title, rotated along the axis (a presentation attribute, not a style)."""
    return (
        f'<text x="14" y="{_c(mid_y)}" text-anchor="middle" class="c-muted" '
        f'font-size="{SMALL}" transform="rotate(-90 14 {_c(mid_y)})">{esc(clip(label, 40))}</text>'
    )


def empty_chart(title: str, message: str) -> str:
    body = _text(WIDTH / 2.0, 34.0, message, anchor="middle", css="c-muted")
    return _frame(WIDTH, 56.0, title, body)


# --- horizontal bars ------------------------------------------------------------------------


def hbar_chart(
    bars: Sequence[HBar],
    *,
    title: str,
    axis_label: str,
    lo: float = 0.0,
    hi: float = 1.0,
    tick_format: Formatter = default_format,
) -> str:
    """Labelled horizontal bars on one shared scale, the value written at each bar's tip.

    A bar's own threshold is drawn as a tick across that bar's row only, with its label
    under the bar, so a limit never looks as if it applied to the other rows.
    """
    rows = list(bars[:MAX_BARS])
    if not rows:
        return empty_chart(title, "gösterilecek değer yok")
    lo_v, hi_v = finite(lo), finite(hi)
    if lo_v is None or hi_v is None or not hi_v > lo_v:
        lo_v, hi_v = 0.0, 1.0
    label_w, x0, x1 = 200.0, 210.0, WIDTH - 86.0
    bar_h = 14.0
    y = 8.0
    parts: list[str] = []
    for bar in rows:
        row_h = 26.0 + (16.0 if bar.threshold is not None else 0.0)
        mid = y + bar_h / 2.0 + 4.0
        parts.append(_text(label_w, mid, clip(bar.label, LABEL_CHARS), anchor="end", css="c-text"))
        parts.append(_rect(x0, y, x1 - x0, bar_h, "c-track"))
        value = finite(bar.value)
        text = clip(bar.text, VALUE_CHARS)
        if value is None:
            parts.append(_text(x0 + 4.0, mid, text or "—", css="c-muted"))
        else:
            end = scale(value, lo_v, hi_v, x0, x1)
            parts.append(_rect(x0, y, end - x0, bar_h, "c-bar-a", f"{bar.label}: {bar.text}"))
            parts.append(_text(x1 + 6.0, mid, text, css="c-text"))
        if bar.threshold is not None:
            limit = finite(bar.threshold.value)
            if limit is not None:
                tx = scale(limit, lo_v, hi_v, x0, x1)
                parts.append(_line(tx, y - 3.0, tx, y + bar_h + 3.0, "c-limit"))
                parts.append(
                    _text(
                        tx,
                        y + bar_h + 14.0,
                        clip(bar.threshold.label, 40),
                        anchor=_anchor_for(tx, x0, x1),
                        css="c-muted",
                        size=SMALL,
                    )
                )
        y += row_h
    axis_y = y + 2.0
    parts.append(_line(x0, axis_y, x1, axis_y, "c-axis"))
    for tick in (lo_v, (lo_v + hi_v) / 2.0, hi_v):
        tx = scale(tick, lo_v, hi_v, x0, x1)
        anchor = "start" if tx <= x0 else ("end" if tx >= x1 else "middle")
        parts.append(_line(tx, axis_y, tx, axis_y + 4.0, "c-axis"))
        parts.append(
            _text(tx, axis_y + 16.0, tick_format(tick), anchor=anchor, css="c-muted", size=SMALL)
        )
    parts.append(_text((x0 + x1) / 2.0, axis_y + 32.0, axis_label, anchor="middle", css="c-muted"))
    return _frame(WIDTH, axis_y + 40.0, title, "".join(parts))


# --- grouped (paired) bars -------------------------------------------------------------------


def grouped_bar_chart(
    groups: Sequence[Group],
    *,
    title: str,
    series: Sequence[str],
    y_label: str,
    y_range: tuple[float, float] | None = None,
    fmt: Formatter = default_format,
    width: float = WIDTH,
    height: float = 240.0,
) -> str:
    """Vertical bars around a zero line: one bar per series inside each group.

    With one series, bars below zero also carry the `c-neg` class (and the value text keeps
    its minus sign); with several series the colour stays the series' identity and the
    position under the zero line carries the sign.
    """
    names = [clip(name, 24) for name in series[:MAX_SERIES]] or ["değer"]
    shown = list(groups[:MAX_GROUPS])
    if not shown:
        return empty_chart(title, "gösterilecek değer yok")
    n_series = len(names)
    values = [
        [finite(g.values[i]) if i < len(g.values) else None for i in range(n_series)] for g in shown
    ]
    present = [v for row in values for v in row if v is not None]
    lo = min([0.0, *present])
    hi = max([0.0, *present])
    if y_range is not None:
        extra = [v for v in (finite(y_range[0]), finite(y_range[1])) if v is not None]
        lo, hi = min([lo, *extra]), max([hi, *extra])
    if not hi > lo or not math.isfinite(hi - lo):
        lo, hi = (lo, lo + 1.0) if hi == lo and math.isfinite(lo) else (0.0, 1.0)
    legend_h = 22.0 if n_series > 1 else 6.0
    left, right, top, bottom = 64.0, width - 10.0, legend_h + 14.0, height - 48.0
    parts: list[str] = []
    if n_series > 1:
        lx = left
        for index, name in enumerate(names):
            parts.append(_rect(lx, 6.0, 10.0, 10.0, SERIES_CLASSES[index] + " c-key"))
            parts.append(_text(lx + 14.0, 15.0, name, css="c-text", size=SMALL))
            lx += 24.0 + 7.0 * len(name)
    zero_y = scale(0.0, lo, hi, bottom, top)
    for tick in sorted({lo, 0.0, hi}):
        ty = scale(tick, lo, hi, bottom, top)
        parts.append(_line(left, ty, right, ty, "c-zero" if tick == 0.0 else "c-grid"))
        parts.append(
            _text(left - 6.0, ty + 4.0, fmt(tick), anchor="end", css="c-muted", size=SMALL)
        )
    mid_y = (top + bottom) / 2.0
    parts.append(_y_label(mid_y, y_label))
    group_w = (right - left) / len(shown)
    bar_w = max(1.0, min(28.0, group_w * GROUP_FILL / n_series - 2.0))
    # A value label needs about 50 px at font size 10; narrower slots keep the value in the
    # bar's tooltip and the page's table instead of overlapping the neighbour's label.
    with_values = len(shown) * n_series <= VALUE_LABEL_LIMIT and group_w / n_series >= 50.0
    label_every = max(1, math.ceil(len(shown) / 16))
    label_chars = max(3, int(group_w * label_every / 7.0))
    for gi, (group, row) in enumerate(zip(shown, values, strict=True)):
        g_left = left + gi * group_w
        span = n_series * (bar_w + 2.0) - 2.0
        start = g_left + (group_w - span) / 2.0
        for si, value in enumerate(row):
            x = start + si * (bar_w + 2.0)
            text = group.texts[si] if si < len(group.texts) else ""
            tip = f"{group.label} · {names[si]}: {text or '—'}"
            if value is None:
                if with_values:
                    parts.append(
                        _text(
                            x + bar_w / 2.0,
                            zero_y - 4.0,
                            "yok",
                            anchor="middle",
                            css="c-muted",
                            size=10,
                        )
                    )
                continue
            vy = scale(value, lo, hi, bottom, top)
            css = SERIES_CLASSES[si]
            if n_series == 1 and value < 0.0:
                css += " c-neg"
            parts.append(_rect(x, min(vy, zero_y), bar_w, abs(zero_y - vy), css, tip))
            if with_values:
                ty = vy - 4.0 if value >= 0.0 else vy + 12.0
                parts.append(
                    _text(
                        x + bar_w / 2.0, ty, clip(text, 9), anchor="middle", css="c-text", size=10
                    )
                )
        if gi % label_every == 0:
            parts.append(
                _text(
                    g_left + group_w / 2.0,
                    bottom + 22.0,
                    clip(group.label, label_chars),
                    anchor="middle",
                    css="c-muted",
                    size=SMALL,
                )
            )
    return _frame(width, height, title, "".join(parts))


# --- histogram -------------------------------------------------------------------------------


def histogram_chart(
    edges: Sequence[float],
    counts: Sequence[int],
    *,
    title: str,
    x_label: str,
    y_label: str,
    markers: Sequence[Marker] = (),
    fmt: Formatter = default_format,
) -> str:
    """Bins as touching columns; each marker is a vertical line with its own label row on top.

    The x range grows to include every marker (e.g. a max-loss line beyond the worst path),
    so a limit is never silently dropped off the edge.
    """
    clean_edges = [finite(edge) for edge in edges]
    if (
        len(clean_edges) < 2
        or len(counts) != len(clean_edges) - 1
        or any(edge is None for edge in clean_edges)
    ):
        return empty_chart(title, "histogram verisi geçersiz")
    xs = [edge for edge in clean_edges if edge is not None]
    if any(b <= a for a, b in itertools.pairwise(xs)):
        return empty_chart(title, "histogram kenarları artan sırada değil")
    if len(counts) > MAX_BINS:
        return empty_chart(title, f"histogram çok fazla kutu içeriyor ({len(counts)})")
    heights = [
        max(0, int(c)) if isinstance(c, int) and not isinstance(c, bool) else 0 for c in counts
    ]
    shown_markers = [
        (value, m) for m in markers[:MAX_MARKERS] if (value := finite(m.value)) is not None
    ]
    lo = min([xs[0], *(v for v, _ in shown_markers)])
    hi = max([xs[-1], *(v for v, _ in shown_markers)])
    if not (hi > lo and math.isfinite(hi - lo)):
        return empty_chart(title, "histogram aralığı çizilemiyor")
    left, right = 56.0, WIDTH - 12.0
    top = 10.0 + 15.0 * len(shown_markers)
    bottom = top + 150.0
    peak = max(heights)
    parts: list[str] = []
    parts.append(_line(left, bottom, right, bottom, "c-axis"))
    for index, count in enumerate(heights):
        x_a = scale(xs[index], lo, hi, left, right)
        x_b = scale(xs[index + 1], lo, hi, left, right)
        y = scale(float(count), 0.0, float(peak), bottom, top) if peak > 0 else bottom
        tip = f"{fmt(xs[index])} – {fmt(xs[index + 1])}: {count} yol"
        parts.append(_rect(x_a, y, max(MIN_BIN_PX, x_b - x_a - 1.0), bottom - y, "c-bar-a", tip))
    if peak == 0:
        parts.append(
            _text(
                (left + right) / 2.0,
                (top + bottom) / 2.0,
                "tüm kutular boş (0 yol)",
                anchor="middle",
                css="c-muted",
            )
        )
    for row, (value, marker) in enumerate(shown_markers):
        mx = scale(value, lo, hi, left, right)
        label_y = 20.0 + 15.0 * row
        css = "c-limit" if marker.limit else "c-marker"
        parts.append(_line(mx, label_y + 3.0, mx, bottom, css, dashed=not marker.limit))
        parts.append(
            _text(
                mx + (4.0 if _anchor_for(mx, left, right) == "start" else -4.0),
                label_y,
                clip(marker.label, 48),
                anchor=_anchor_for(mx, left, right),
                css="c-text",
                size=SMALL,
            )
        )
    for tick in (lo, (lo + hi) / 2.0, hi):
        tx = scale(tick, lo, hi, left, right)
        anchor = "start" if tx <= left else ("end" if tx >= right else "middle")
        parts.append(_text(tx, bottom + 15.0, fmt(tick), anchor=anchor, css="c-muted", size=SMALL))
    parts.append(_text(left - 6.0, top + 4.0, str(peak), anchor="end", css="c-muted", size=SMALL))
    parts.append(_text(left - 6.0, bottom, "0", anchor="end", css="c-muted", size=SMALL))
    parts.append(
        _text(
            (left + right) / 2.0,
            bottom + 31.0,
            clip(x_label, 90),
            anchor="middle",
            css="c-muted",
            size=SMALL,
        )
    )
    parts.append(_y_label((top + bottom) / 2.0, y_label))
    return _frame(WIDTH, bottom + 40.0, title, "".join(parts))


# --- line ------------------------------------------------------------------------------------


def line_chart(
    points: Sequence[tuple[float, float]],
    *,
    title: str,
    x_label: str,
    y_label: str,
    x_texts: tuple[str, str] = ("", ""),
    marker: Marker | None = None,
    fmt: Formatter = default_format,
) -> str:
    """One line with a zero line, the last value written at its end and an optional vertical
    marker (e.g. the OOS boundary). A marker outside the x range is said in words."""
    clean = sorted(
        (x, y)
        for x, y in ((finite(px), finite(py)) for px, py in points)
        if x is not None and y is not None
    )
    if not clean:
        return empty_chart(title, "çizilecek nokta yok")
    if len(clean) > MAX_POINTS:
        stride = math.ceil(len(clean) / MAX_POINTS)
        clean = clean[::stride] + ([clean[-1]] if (len(clean) - 1) % stride else [])
    x_lo, x_hi = clean[0][0], clean[-1][0]
    if not (x_hi > x_lo and math.isfinite(x_hi - x_lo)):
        x_lo, x_hi = x_lo - 1.0, x_lo + 1.0
    ys = [y for _, y in clean]
    y_lo, y_hi = min(0.0, *ys), max(0.0, *ys)
    if not (y_hi > y_lo and math.isfinite(y_hi - y_lo)):
        y_lo, y_hi = (y_lo - 1.0, y_lo + 1.0) if y_hi == y_lo else (0.0, 1.0)
    left, right, top, bottom = 64.0, WIDTH - 70.0, 26.0, 196.0
    parts: list[str] = []
    for tick in sorted({y_lo, 0.0, y_hi}):
        ty = scale(tick, y_lo, y_hi, bottom, top)
        parts.append(_line(left, ty, right, ty, "c-zero" if tick == 0.0 else "c-grid"))
        parts.append(
            _text(left - 6.0, ty + 4.0, fmt(tick), anchor="end", css="c-muted", size=SMALL)
        )
    coords = [
        (scale(x, x_lo, x_hi, left, right), scale(y, y_lo, y_hi, bottom, top)) for x, y in clean
    ]
    if len(coords) == 1:
        cx, cy = coords[0]
        parts.append(f'<circle cx="{_c(cx)}" cy="{_c(cy)}" r="4" class="c-dot"/>')
    else:
        path = " ".join(f"{_c(x)},{_c(y)}" for x, y in coords)
        parts.append(f'<polyline points="{path}" class="c-line" fill="none"/>')
    end_x, end_y = coords[-1]
    parts.append(
        f'<circle cx="{_c(end_x)}" cy="{_c(end_y)}" r="4" class="c-dot">'
        f"<title>{esc(fmt(clean[-1][1]))}</title></circle>"
    )
    parts.append(
        _text(end_x + 8.0, end_y + 4.0, clip(fmt(clean[-1][1]), 10), css="c-text", size=SMALL)
    )
    if marker is not None:
        value = finite(marker.value)
        if value is not None and x_lo <= value <= x_hi:
            mx = scale(value, x_lo, x_hi, left, right)
            parts.append(_line(mx, top - 8.0, mx, bottom, "c-marker", dashed=True))
            anchor = _anchor_for(mx, left, right)
            parts.append(
                _text(
                    mx + (4.0 if anchor == "start" else -4.0),
                    top - 10.0,
                    clip(marker.label, 48),
                    anchor=anchor,
                    css="c-text",
                    size=SMALL,
                )
            )
        else:
            parts.append(
                _text(
                    left,
                    12.0,
                    clip(f"{marker.label}: grafiğin aralığı dışında", 70),
                    css="c-muted",
                    size=SMALL,
                )
            )
    parts.append(_text(left, bottom + 15.0, clip(x_texts[0], 24), css="c-muted", size=SMALL))
    parts.append(
        _text(right, bottom + 15.0, clip(x_texts[1], 24), anchor="end", css="c-muted", size=SMALL)
    )
    parts.append(
        _text(
            (left + right) / 2.0,
            bottom + 31.0,
            clip(x_label, 60),
            anchor="middle",
            css="c-muted",
            size=SMALL,
        )
    )
    mid_y = (top + bottom) / 2.0
    parts.append(_y_label(mid_y, y_label))
    return _frame(WIDTH, bottom + 40.0, title, "".join(parts))


# --- a bar inside a table cell ---------------------------------------------------------------


def cell_bar(value: float | None, max_abs: float | None, *, signed: bool = True) -> str:
    """A tiny decorative bar for a table cell; the cell's text carries the value itself."""
    width, height = 60.0, 10.0
    number, extent = finite(value), finite(max_abs)
    body = ""
    if number is not None and extent is not None and extent > 0.0:
        if signed:
            mid = width / 2.0
            end = scale(number, -extent, extent, 0.0, width)
            css = "c-bar-a" if number >= 0.0 else "c-bar-a c-neg"
            body = _rect(min(mid, end), 1.0, abs(end - mid), height - 2.0, css)
            body += _line(mid, 0.0, mid, height, "c-zero")
        else:
            end = scale(number, 0.0, extent, 0.0, width)
            body = _rect(0.0, 1.0, end, height - 2.0, "c-bar-a")
    return (
        f'<svg class="cellbar" xmlns="http://www.w3.org/2000/svg" viewBox="0 0 60 10" '
        f'aria-hidden="true" focusable="false">{body}</svg>'
    )
