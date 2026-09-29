"""Reading the data labels printed on a report's charts (pdf_chart_extraction).

The model's output is strings only; what is pinned here is what code does with them — which
labels become placed points and which are dropped — plus the region finding that decides what
the model is shown at all. No test calls a real model.
"""

import asyncio
from pathlib import Path
from unittest.mock import AsyncMock, Mock, patch

import pytest

from pdf_chart_extraction import (
    ChartReading,
    _ChartOut,
    _ChartPointOut,
    _CHART_CACHE,
    _PageCharts,
    _period_token,
    _to_reading,
    chart_regions,
    extract_charts_from_pdf,
    report_period,
)

_SK = Path(__file__).resolve().parent.parent / "sample_data" / "SK-Juni-2026.pdf"
_FALLBACK = (2026, "Jun")


@pytest.fixture(autouse=True)
def _clear_cache():
    _CHART_CACHE.clear()
    yield
    _CHART_CACHE.clear()


def _chart(points, **overrides):
    base = dict(caption="Grafik 2", title="IKK per Kelompok Pengeluaran", indicator="IKK",
                unit="(Indeks)", points=points)
    base.update(overrides)
    return _ChartOut(**base)


def _point(series="Rp1 - 2 juta", year="2026", period="6", value="108,9", kind="level"):
    return _ChartPointOut(series=series, year=year, period=period, value=value, kind=kind)


# ---------------------------------------------------------------------------
# From strings to placed points
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("raw,token", [
    ("6", "Jun"), ("12", "Dec"), ("Jun", "Jun"), ("Juni", "Jun"), ("Mei", "May"), ("13", None),
])
def test_period_token_reads_axis_month_numbers_and_names(raw, token):
    assert _period_token(raw) == token


def test_a_labelled_bar_becomes_a_placed_point():
    reading = _to_reading(_chart([_point()]), 2, "id", _FALLBACK)

    point = reading.points[0]
    assert (point.series, point.year, point.month, point.value, point.raw) == (
        "Rp1 - 2 juta", 2026, "Jun", 108.9, "108,9")
    assert reading.unit == "Indeks"
    assert reading.metric_label(point.series) == "IKK > Rp1 - 2 juta"


def test_unlabelled_bars_are_listed_only_to_place_the_labelled_ones():
    reading = _to_reading(_chart([_point(period="4", value=""), _point(period="5", value="113,0")]),
                          2, "id", _FALLBACK)
    assert [(p.month, p.value) for p in reading.points] == [("May", 113.0)]


def test_a_ditto_mark_for_a_series_name_drops_the_label():
    reading = _to_reading(_chart([_point(series='"', value="101,6"), _point()]), 3, "id", _FALLBACK)
    assert [p.series for p in reading.points] == ["Rp1 - 2 juta"]


def test_a_map_label_takes_the_report_period_and_a_change_label_its_kind():
    reading = _to_reading(_chart(
        [_point(series="MEDAN", year="", period="", value="90,5"),
         _point(series="MEDAN", year="", period="", value="(Δ -8,9)", kind="change")],
        caption="Gambar 1", indicator="Indeks Keyakinan Konsumen (IKK)",
    ), 14, "id", _FALLBACK)

    assert [(p.year, p.month, p.value, p.kind) for p in reading.points] == [
        (2026, "Jun", 90.5, "level"), (2026, "Jun", -8.9, "change")]


def test_a_breakdown_chart_takes_its_indicator_from_the_title():
    reading = _to_reading(_chart(
        [_point()], indicator="", title="Indeks Ekspektasi Penghasilan per Kelompok Pengeluaran",
    ), 5, "id", _FALLBACK)
    assert reading.indicator == "Indeks Ekspektasi Penghasilan"


def test_a_trend_chart_keeps_its_series_as_the_indicators():
    reading = _to_reading(_chart(
        [_point(series="Indeks Keyakinan Konsumen (IKK)", value="117,8")],
        indicator="", title="Perkembangan Indeks Keyakinan Konsumen",
    ), 1, "id", _FALLBACK)
    assert reading.indicator == ""
    assert reading.metric_label("Indeks Keyakinan Konsumen (IKK)") == "Indeks Keyakinan Konsumen (IKK)"


def test_an_axis_note_is_not_part_of_the_series_name():
    reading = _to_reading(_chart([_point(series="Konsumsi (sb. kanan)", value="73,0")], indicator=""),
                          6, "id", _FALLBACK)
    assert reading.points[0].series == "Konsumsi"


def test_a_chart_with_nothing_placeable_is_no_reading():
    assert _to_reading(_chart([_point(value="")]), 2, "id", _FALLBACK) is None


# ---------------------------------------------------------------------------
# The pass
# ---------------------------------------------------------------------------

def test_no_vision_model_means_no_chart_pass():
    assert asyncio.run(extract_charts_from_pdf(b"%PDF-fake", None)) == []


def _llm(*returns):
    structured = Mock()
    structured.ainvoke = AsyncMock(side_effect=list(returns))
    llm = Mock()
    llm.with_structured_output = Mock(return_value=structured)
    llm.model = "gemini-2.5-flash"
    type(llm).__name__ = "ChatGoogleGenerativeAI"
    return llm


def _patched_regions(pages):
    return [
        patch("pdf_chart_extraction.chart_regions", return_value={p: [(0, 0, -1, 100)] for p in pages}),
        patch("pdf_chart_extraction._render_regions", return_value=[(p, "png") for p in pages]),
        patch("pdf_chart_extraction.report_period", return_value="Juni 2026"),
        patch("pdf_chart_extraction._document_number_format", return_value="id"),
    ]


def test_every_chart_region_is_read_and_placed():
    llm = _llm(_PageCharts(charts=[_chart([_point()])]),
               _PageCharts(charts=[_chart([_point(value="121,4", series="> Rp5 juta")],
                                          caption="Grafik 3")]))
    patches = _patched_regions([2, 3])
    for p in patches:
        p.start()
    try:
        readings = asyncio.run(extract_charts_from_pdf(b"%PDF-fake", llm))
    finally:
        for p in patches:
            p.stop()

    assert [(r.page_number, r.caption) for r in readings] == [(2, "Grafik 2"), (3, "Grafik 3")]


def test_a_region_whose_call_fails_is_reported_unread_and_the_rest_come_back():
    llm = _llm(RuntimeError("boom"), _PageCharts(charts=[_chart([_point()])]))
    unread = []
    patches = _patched_regions([2, 5])
    for p in patches:
        p.start()
    try:
        readings = asyncio.run(extract_charts_from_pdf(b"%PDF-fake", llm, on_unread=unread.extend))
    finally:
        for p in patches:
            p.stop()

    assert len(readings) == 1
    assert unread in ([2], [5])


# ---------------------------------------------------------------------------
# Locating charts on the real report
# ---------------------------------------------------------------------------

@pytest.mark.skipif(not _SK.exists(), reason="SK-Juni-2026 sample not present")
def test_every_chart_of_the_survey_report_gets_its_own_box():
    regions = chart_regions(_SK.read_bytes())

    # Grafik 1-20 plus Gambar 1: 21 charts. Page 2 holds Grafik 2 and 3 side by side (split at
    # Grafik 3's caption) and Grafik 4 across the page.
    assert sum(len(boxes) for boxes in regions.values()) == 21
    assert len(regions[2]) == 3
    left, right = regions[2][0], regions[2][1]
    assert left[2] == right[0]                   # the side-by-side pair meets at one edge
    assert 8 not in regions                      # the appendix pages have no charts


@pytest.mark.skipif(not _SK.exists(), reason="SK-Juni-2026 sample not present")
def test_the_report_period_is_the_month_the_text_names_most():
    assert report_period(_SK.read_bytes()) == "Juni 2026"


def test_a_chart_reported_twice_for_one_region_is_one_reading():
    llm = _llm(_PageCharts(charts=[
        _chart([_point()]),
        _chart([_point(), _point(value="113,0", period="5")], caption="", title=""),
    ]))
    patches = _patched_regions([3])
    for p in patches:
        p.start()
    try:
        readings = asyncio.run(extract_charts_from_pdf(b"%PDF-fake", llm))
    finally:
        for p in patches:
            p.stop()

    [reading] = readings
    assert reading.caption == "Grafik 2"
    assert [(p.month, p.value) for p in reading.points] == [("Jun", 108.9), ("May", 113.0)]


_M2_AUG = Path(__file__).resolve().parent.parent / "sample_data" / "Analisis-Perkembangan-Uang Beredar-(M2)-Agustus-2026.pdf"


@pytest.mark.skipif(not _M2_AUG.exists(), reason="M2 Agustus 2026 sample not present")
def test_a_chart_beside_a_text_column_is_cropped_to_its_own_column():
    # The M2 report's charts have live text (axis ticks, legends) and share their rows with the
    # narrative's right-hand column: each region must reach past the chart's own labels, and
    # stop short of the paragraph beside it.
    regions = chart_regions(_M2_AUG.read_bytes())
    grafik6 = regions[5][-1]
    left, bottom, right, top = grafik6
    assert top - bottom > 150                   # the whole chart, not just its caption
    assert 250 < right < 315                    # the paragraph column starts at ~312 pt


def test_a_prose_column_is_found_by_the_left_edge_its_lines_share():
    from pdf_chart_extraction import _prose_column_edge

    def line(text, x, y):
        return [(ch, x + i * 5, x + i * 5 + 4, y + 4, y) for i, ch in enumerate(text) if ch.strip()]

    glyphs = []
    for n, y in enumerate((500, 488, 476, 464)):
        glyphs += line("9,5 2020 2021", 40, y)                                    # chart text
        glyphs += line("sebelumnya suku bunga simpanan berjangka mengalami kenaikan", 312, y)
    assert _prose_column_edge(glyphs, 30, 400, 520) == 306
    # A chart with only its own text beside it is left full width.
    chart_only = [g for g in glyphs if g[1] < 300]
    assert _prose_column_edge(chart_only, 30, 400, 520) is None


def test_every_reading_carries_a_small_picture_of_its_chart():
    import base64
    import io

    from PIL import Image

    buffer = io.BytesIO()
    Image.new("RGB", (1536, 1000), "white").save(buffer, format="PNG")
    png = base64.b64encode(buffer.getvalue()).decode("ascii")
    llm = _llm(_PageCharts(charts=[_chart([_point()])]))
    patches = _patched_regions([2])
    for p in patches:
        p.start()
    try:
        with patch("pdf_chart_extraction._render_regions", return_value=[(2, png)]):
            [reading] = asyncio.run(extract_charts_from_pdf(b"%PDF-fake", llm))
    finally:
        for p in patches:
            p.stop()

    assert reading.thumbnail.startswith("data:image/jpeg;base64,")
    thumb = Image.open(io.BytesIO(base64.b64decode(reading.thumbnail.split(",", 1)[1])))
    assert max(thumb.size) <= 720
