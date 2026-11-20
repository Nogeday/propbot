"""Inline SVG charts: geometry for known inputs, degenerate inputs, escaping, CSP-safe markup."""

import math
import re
import xml.etree.ElementTree as ET

import pytest
from hypothesis import given, settings
from hypothesis import strategies as st

from propbot.dashboard import charts
from propbot.dashboard.charts import Group, HBar, Marker, Threshold

SVG = "{http://www.w3.org/2000/svg}"
HOSTILE = '<script>alert("x")</script>&\'"'


def parse(svg: str) -> ET.Element:
    return ET.fromstring(svg)


def elements(root: ET.Element, tag: str) -> list[ET.Element]:
    return list(root.iter(SVG + tag))


def css(element: ET.Element) -> set[str]:
    return set((element.get("class") or "").split())


def bars(root: ET.Element, cls: str = "c-bar-a") -> list[ET.Element]:
    """Data marks only: legend swatches carry `c-key` as well."""
    return [r for r in elements(root, "rect") if cls in css(r) and "c-key" not in css(r)]


def texts(root: ET.Element) -> list[str]:
    return ["".join(t.itertext()) for t in elements(root, "text")]


def assert_safe(svg: str) -> ET.Element:
    """Valid XML, an svg root with a viewBox, no style / script / handler, finite coordinates."""
    root = parse(svg)
    assert root.tag == SVG + "svg"
    assert root.get("viewBox")
    for element in root.iter():
        assert element.tag not in (SVG + "script", SVG + "style", SVG + "foreignObject")
        for name, value in element.attrib.items():
            assert name != "style"
            assert not name.startswith("on")
            assert "javascript:" not in value.lower()
            assert "nan" not in value.lower() or name == "class"
            assert "inf" not in value.lower() or name == "class"
    assert "<script" not in svg
    return root


def f(element: ET.Element, name: str) -> float:
    return float(element.get(name) or "nan")


# --- helpers -------------------------------------------------------------------------------


def test_scale_maps_and_clamps_and_survives_a_zero_span() -> None:
    assert charts.scale(5.0, 0.0, 10.0, 100.0, 200.0) == 150.0
    assert charts.scale(-5.0, 0.0, 10.0, 100.0, 200.0) == 100.0
    assert charts.scale(50.0, 0.0, 10.0, 100.0, 200.0) == 200.0
    assert charts.scale(1.0, 3.0, 3.0, 100.0, 200.0) == 100.0
    assert charts.scale(math.nan, 0.0, 1.0, 7.0, 9.0) == 7.0
    assert charts.scale(1.0, -1e308, 1e308, 7.0, 9.0) == 7.0  # span overflows to inf


def test_finite_rejects_nan_inf_none_and_bool() -> None:
    assert charts.finite(1.5) == 1.5
    for value in (None, math.nan, math.inf, -math.inf, True):
        assert charts.finite(value) is None


def test_compact_number_is_turkish_and_bounded_for_extremes() -> None:
    assert charts.compact_number(1234.5, 1) == "1.234,5"
    assert charts.compact_number(None) == "—"
    assert charts.compact_number(math.inf) == "—"
    extreme = charts.compact_number(1e300)
    assert len(extreme) < 12
    assert "e+300" in extreme


def test_clip_shortens_long_labels_with_an_ellipsis() -> None:
    assert charts.clip("abcdef", 4) == "abc…"
    assert charts.clip("ab", 4) == "ab"
    assert charts.clip("a  b\nc", 10) == "a b c"


# --- horizontal bars -----------------------------------------------------------------------


def test_hbar_widths_are_proportional_and_values_are_written() -> None:
    svg = charts.hbar_chart(
        [HBar("Yarım", 0.5, "%50,0"), HBar("Tam", 1.0, "%100,0"), HBar("Sıfır", 0.0, "%0,0")],
        title="Olasılıklar",
        axis_label="olasılık",
    )
    root = assert_safe(svg)
    widths = [f(rect, "width") for rect in bars(root)]
    assert widths[1] == pytest.approx(2 * widths[0], abs=0.2)
    assert widths[2] == 0.0
    assert {"%50,0", "%100,0", "%0,0", "Yarım", "olasılık"} <= set(texts(root))
    assert root.find(SVG + "title") is not None


def test_hbar_threshold_tick_sits_at_its_value_on_its_own_row() -> None:
    svg = charts.hbar_chart(
        [HBar("A", 0.4, "%40", Threshold(0.6, "G4 eşiği ≥ %60")), HBar("B", 0.2, "%20")],
        title="t",
        axis_label="x",
    )
    root = assert_safe(svg)
    a_bar = bars(root)[0]
    zero_x, full = f(a_bar, "x"), f(a_bar, "width") / 0.4
    limits = [line for line in elements(root, "line") if "c-limit" in css(line)]
    assert len(limits) == 1
    assert f(limits[0], "x1") == pytest.approx(zero_x + 0.6 * full, abs=0.2)
    # The tick spans the first row only.
    assert f(limits[0], "y2") < f(bars(root)[1], "y")
    assert "G4 eşiği ≥ %60" in texts(root)


def test_hbar_missing_value_draws_no_bar_and_values_outside_the_scale_are_clamped() -> None:
    svg = charts.hbar_chart(
        [HBar("yok", None, ""), HBar("büyük", 5.0, "%500"), HBar("nan", math.nan, "?")],
        title="t",
        axis_label="x",
    )
    root = assert_safe(svg)
    drawn = bars(root)
    assert len(drawn) == 1
    track = next(r for r in elements(root, "rect") if "c-track" in css(r))
    assert f(drawn[0], "width") == pytest.approx(f(track, "width"))
    assert "—" in texts(root)


def test_hbar_empty_and_bad_scale() -> None:
    assert "gösterilecek değer yok" in charts.hbar_chart([], title="t", axis_label="x")
    root = assert_safe(
        charts.hbar_chart([HBar("a", 0.5, "x")], title="t", axis_label="x", lo=1.0, hi=1.0)
    )
    assert bars(root)


def test_hbar_number_of_rows_is_bounded() -> None:
    many = [HBar(f"r{i}", 0.5, "x") for i in range(500)]
    root = assert_safe(charts.hbar_chart(many, title="t", axis_label="x"))
    assert len(bars(root)) == charts.MAX_BARS


# --- grouped bars --------------------------------------------------------------------------


def test_grouped_bars_go_up_and_down_from_the_zero_line() -> None:
    svg = charts.grouped_bar_chart(
        [Group("K1", (1.0, -1.0), ("+1R", "-1R")), Group("K2", (2.0, 0.5), ("+2R", "+0,5R"))],
        title="IS/OOS",
        series=("IS", "OOS"),
        y_label="ortalama R",
    )
    root = assert_safe(svg)
    a, b = bars(root, "c-bar-a"), bars(root, "c-bar-b")
    assert len(a) == 2
    assert len(b) == 2
    zero = next(line for line in elements(root, "line") if "c-zero" in css(line))
    zero_y = f(zero, "y1")
    # +1 ends at the zero line from above, -1 starts at it and goes down.
    assert f(a[0], "y") + f(a[0], "height") == pytest.approx(zero_y, abs=0.2)
    assert f(b[0], "y") == pytest.approx(zero_y, abs=0.2)
    assert f(a[0], "height") == pytest.approx(f(b[0], "height"), abs=0.2)
    assert f(a[1], "height") == pytest.approx(2 * f(a[0], "height"), abs=0.2)
    # A legend names both series (identity is never colour alone).
    assert len([r for r in elements(root, "rect") if "c-key" in css(r)]) == 2
    assert {"IS", "OOS", "K1", "K2"} <= set(texts(root))
    # Paired series keep their colour below zero.
    assert "c-neg" not in css(b[0])


def test_single_series_negative_bar_is_marked_and_titled() -> None:
    root = assert_safe(
        charts.grouped_bar_chart(
            [Group("1×", (0.2,), ("+0,2R",)), Group("2×", (-0.1,), ("-0,1R",))],
            title="maliyet",
            series=("ortalama R",),
            y_label="R",
        )
    )
    drawn = bars(root)
    assert "c-neg" not in css(drawn[0])
    assert "c-neg" in css(drawn[1])
    tooltip = "".join(drawn[1].itertext())
    assert "2×" in tooltip
    assert "-0,1R" in tooltip


def test_grouped_bars_share_a_given_range_so_small_multiples_compare() -> None:
    small = charts.grouped_bar_chart(
        [Group("a", (0.5,), ("x",))], title="t", series=("s",), y_label="y", y_range=(-1.0, 1.0)
    )
    big = charts.grouped_bar_chart(
        [Group("a", (1.0,), ("x",))], title="t", series=("s",), y_label="y", y_range=(-1.0, 1.0)
    )
    h_small = f(bars(assert_safe(small))[0], "height")
    h_big = f(bars(assert_safe(big))[0], "height")
    assert h_big == pytest.approx(2 * h_small, abs=0.2)


@pytest.mark.parametrize(
    "groups",
    [
        [],
        [Group("hepsi sıfır", (0.0, 0.0), ("0", "0"))],
        [Group("eksik", (None, None), ("", ""))],
        [Group("kısa", (), ())],
        [Group("aşırı", (1e300, -1e300), ("a", "b"))],
        [Group("nan", (math.nan, math.inf), ("a", "b"))],
    ],
)
def test_grouped_bars_degenerate_inputs_render(groups: list[Group]) -> None:
    assert_safe(charts.grouped_bar_chart(groups, title="t", series=("a", "b"), y_label="y"))


def test_grouped_bars_bound_groups_and_hide_crowded_value_labels() -> None:
    many = [Group(f"K{i}", (1.0, 2.0), ("+1R", "+2R")) for i in range(300)]
    root = assert_safe(charts.grouped_bar_chart(many, title="t", series=("IS", "OOS"), y_label="y"))
    assert len(bars(root, "c-bar-a")) == charts.MAX_GROUPS
    assert "+1R" not in texts(root)  # values stay in tooltips and the table


# --- histogram -----------------------------------------------------------------------------


def test_histogram_bins_and_marker_positions() -> None:
    svg = charts.histogram_chart(
        [0.0, 1.0, 2.0, 3.0, 4.0],
        [1, 4, 2, 0],
        title="DD",
        x_label="maks. düşüş",
        y_label="yol",
        markers=[Marker(2.0, "medyan %2"), Marker(8.0, "firma sınırı %8", limit=True)],
    )
    root = assert_safe(svg)
    drawn = bars(root)
    assert len(drawn) == 4
    heights = [f(rect, "height") for rect in drawn]
    assert heights[1] == pytest.approx(4 * heights[0], abs=0.2)
    assert heights[3] == 0.0
    # The x range grows to the limit at 8, so bin edges sit at 0..4 of a 0..8 axis.
    left = f(drawn[0], "x")
    unit = (f(drawn[1], "x") - left) / 1.0
    median = next(line for line in elements(root, "line") if "c-marker" in css(line))
    limit = next(line for line in elements(root, "line") if "c-limit" in css(line))
    assert f(median, "x1") == pytest.approx(left + 2 * unit, abs=0.3)
    assert f(limit, "x1") == pytest.approx(left + 8 * unit, abs=0.3)
    assert median.get("stroke-dasharray")
    assert not limit.get("stroke-dasharray")
    assert {"medyan %2", "firma sınırı %8"} <= set(texts(root))
    # Each marker label has its own row: no two share a y coordinate.
    label_rows = [
        t.get("y") for t in elements(root, "text") if "".join(t.itertext()).startswith(("m", "f"))
    ]
    assert len(set(label_rows)) == len(label_rows)


def test_histogram_all_zero_counts_says_so() -> None:
    root = assert_safe(
        charts.histogram_chart([0.0, 1.0, 2.0], [0, 0], title="t", x_label="x", y_label="y")
    )
    assert all(f(rect, "height") == 0.0 for rect in bars(root))
    assert "tüm kutular boş (0 yol)" in texts(root)


@pytest.mark.parametrize(
    ("edges", "counts", "message"),
    [
        ([0.0], [], "geçersiz"),
        ([0.0, 1.0], [1, 2], "geçersiz"),
        ([0.0, math.nan], [1], "geçersiz"),
        ([1.0, 0.0], [1], "artan"),
        (list(range(500)), [1] * 499, "çok fazla"),
    ],
)
def test_histogram_bad_inputs_render_a_message(
    edges: list[float], counts: list[int], message: str
) -> None:
    svg = charts.histogram_chart(edges, counts, title="t", x_label="x", y_label="y")
    assert_safe(svg)
    assert message in svg


def test_histogram_ignores_non_finite_markers_and_bounds_their_number() -> None:
    markers = [Marker(math.inf, "inf")] + [Marker(float(i), f"m{i}") for i in range(20)]
    root = assert_safe(
        charts.histogram_chart(
            [0.0, 1.0], [3], title="t", x_label="x", y_label="y", markers=markers
        )
    )
    drawn = [line for line in elements(root, "line") if "c-marker" in css(line)]
    assert len(drawn) == charts.MAX_MARKERS - 1  # the inf marker is one of the six, dropped


# --- line ----------------------------------------------------------------------------------


def test_line_points_and_the_oos_marker() -> None:
    svg = charts.line_chart(
        [(0.0, 0.0), (10.0, 2.0), (20.0, -1.0)],
        title="R",
        x_label="tarih",
        y_label="R",
        x_texts=("01.01.2026", "20.01.2026"),
        marker=Marker(10.0, "OOS başlangıcı"),
    )
    root = assert_safe(svg)
    poly = elements(root, "polyline")[0]
    coords = [tuple(map(float, p.split(","))) for p in (poly.get("points") or "").split()]
    assert len(coords) == 3
    xs = [x for x, _ in coords]
    marker = next(line for line in elements(root, "line") if "c-marker" in css(line))
    assert f(marker, "x1") == pytest.approx(xs[1], abs=0.2)
    assert coords[1][1] < coords[0][1] < coords[2][1]  # SVG y grows downwards
    assert {"OOS başlangıcı", "01.01.2026", "20.01.2026", "tarih"} <= set(texts(root))


def test_line_marker_outside_the_range_is_said_in_words() -> None:
    root = assert_safe(
        charts.line_chart(
            [(0.0, 1.0), (1.0, 2.0)],
            title="t",
            x_label="x",
            y_label="y",
            marker=Marker(99.0, "OOS"),
        )
    )
    assert not [line for line in elements(root, "line") if "c-marker" in css(line)]
    assert any("aralığı dışında" in text for text in texts(root))


@pytest.mark.parametrize(
    "points",
    [
        [],
        [(5.0, 1.0)],
        [(1.0, 0.0), (1.0, 0.0)],
        [(0.0, 1e300), (1.0, -1e300)],
        [(math.nan, 1.0), (1.0, math.inf)],
    ],
)
def test_line_degenerate_inputs_render(points: list[tuple[float, float]]) -> None:
    assert_safe(charts.line_chart(points, title="t", x_label="x", y_label="y"))


def test_line_downsamples_very_long_curves_and_keeps_the_last_point() -> None:
    points = [(float(i), float(i % 7)) for i in range(10_001)]
    root = assert_safe(charts.line_chart(points, title="t", x_label="x", y_label="y"))
    coords = (elements(root, "polyline")[0].get("points") or "").split()
    assert len(coords) <= charts.MAX_POINTS + 1
    assert coords[-1].startswith(f"{charts.WIDTH - 70.0:.1f},")  # the last point is the end


# --- cell bar ------------------------------------------------------------------------------


def test_cell_bar_is_signed_around_the_middle_and_hidden_from_screen_readers() -> None:
    plus = parse(charts.cell_bar(1.0, 2.0))
    assert plus.get("aria-hidden") == "true"
    rect = bars(plus)[0]
    assert f(rect, "x") == 30.0
    assert f(rect, "width") == 15.0
    minus = bars(parse(charts.cell_bar(-2.0, 2.0)))[0]
    assert f(minus, "x") == 0.0
    assert f(minus, "width") == 30.0
    assert "c-neg" in css(minus)
    assert not bars(parse(charts.cell_bar(None, 2.0)))
    assert not bars(parse(charts.cell_bar(1.0, 0.0)))


# --- escaping and CSP ----------------------------------------------------------------------


def test_every_chart_escapes_hostile_text() -> None:
    outputs = [
        charts.hbar_chart(
            [HBar(HOSTILE, 0.5, HOSTILE, Threshold(0.2, HOSTILE))],
            title=HOSTILE,
            axis_label=HOSTILE,
        ),
        charts.grouped_bar_chart(
            [Group(HOSTILE, (1.0,), (HOSTILE,))], title=HOSTILE, series=(HOSTILE,), y_label=HOSTILE
        ),
        charts.histogram_chart(
            [0.0, 1.0],
            [1],
            title=HOSTILE,
            x_label=HOSTILE,
            y_label=HOSTILE,
            markers=[Marker(0.5, HOSTILE)],
        ),
        charts.line_chart(
            [(0.0, 1.0), (1.0, 2.0)],
            title=HOSTILE,
            x_label=HOSTILE,
            y_label=HOSTILE,
            x_texts=(HOSTILE, HOSTILE),
            marker=Marker(0.5, HOSTILE),
        ),
        charts.empty_chart(HOSTILE, HOSTILE),
    ]
    for svg in outputs:
        root = assert_safe(svg)
        assert "<script>" not in svg
        assert any("<script>" in t for t in [*texts(root), "".join(root.itertext())])


def test_no_style_attribute_or_element_anywhere() -> None:
    svg = charts.histogram_chart(
        [0.0, 1.0, 2.0], [1, 2], title="t", x_label="x", y_label="y", markers=[Marker(1.0, "m")]
    )
    assert re.search(r"\sstyle=", svg) is None
    assert "<style" not in svg


@settings(deadline=None, max_examples=40)
@given(
    st.lists(
        st.tuples(
            st.text(max_size=80),
            st.one_of(st.none(), st.floats(allow_nan=True, allow_infinity=True)),
        ),
        max_size=12,
    )
)
def test_grouped_and_hbar_charts_never_raise_and_stay_valid(
    rows: list[tuple[str, float | None]],
) -> None:
    assert_safe(
        charts.grouped_bar_chart(
            [Group(label, (value, value), ("a", "b")) for label, value in rows],
            title="t",
            series=("a", "b"),
            y_label="y",
        )
    )
    assert_safe(
        charts.hbar_chart(
            [HBar(label, value, "v") for label, value in rows], title="t", axis_label="x"
        )
    )


@settings(deadline=None, max_examples=40)
@given(
    st.lists(st.floats(min_value=-1e12, max_value=1e12), min_size=2, max_size=20, unique=True),
    st.lists(st.floats(allow_nan=True, allow_infinity=True), max_size=4),
)
def test_histogram_never_raises_for_increasing_edges(
    raw_edges: list[float], marker_values: list[float]
) -> None:
    edges = sorted(raw_edges)
    counts = list(range(len(edges) - 1))
    assert_safe(
        charts.histogram_chart(
            edges,
            counts,
            title="t",
            x_label="x",
            y_label="y",
            markers=[Marker(v, "m") for v in marker_values],
        )
    )


def test_characters_xml_forbids_are_replaced_so_the_svg_stays_well_formed() -> None:
    svg = charts.hbar_chart([HBar("a\x00b\x1bc\ud800", 0.5, "x")], title="t\x0b", axis_label="x")
    root = assert_safe(svg)
    assert "a�b�c�" in texts(root)
