"""Charts of a document as pictures: Word pictures after a "Grafik N" caption (EMF rendered, its
texts kept), PDF regions under their captions, and scanned PDF pages."""
from word_extraction import emf_text_boxes
from word_fixtures import emf_bytes


def test_a_chart_emf_gives_each_text_the_centre_of_its_bounds():
    data = emf_bytes([(0, 0, "12,97"), (0, 0, "Tw.II")], bounds=[(2814, 436, 2984, 526), (2050, 1060, 2150, 1118)])

    assert [(b.x, b.y, b.text) for b in emf_text_boxes(data)] == [(2899, 481, "12,97"), (2100, 1089, "Tw.II")]


def test_without_bounds_the_reference_point_is_kept():
    assert [(b.x, b.y) for b in emf_text_boxes(emf_bytes([(120, 5, "2026")]))] == [(120, 5)]


# --- the charts of a document -------------------------------------------------------------------

import base64
import io

from PIL import Image

import chart_images
from chart_images import ChartImage, ScannedPage, pdf_charts, word_charts
from word_fixtures import docx_bytes, emf_chart, emf_pen, emf_polyline16, emf_select, para, picture


def _png(width=40, height=20, colour=(200, 0, 0)):
    out = io.BytesIO()
    Image.new("RGB", (width, height), colour).save(out, format="PNG")
    return out.getvalue()


def _chart_emf():
    return emf_chart([emf_pen(1, (255, 0, 0)), emf_select(1), emf_polyline16([(10, 10), (390, 190)])]
                     ) [:-20] + emf_bytes([(0, 0, "12,97"), (0, 0, "Tw.II")],
                                          bounds=[(100, 40, 160, 60), (200, 170, 240, 190)])[88:]


def test_a_png_chart_after_its_caption_is_found_with_its_title():
    png = _png()
    doc = docx_bytes(para("Grafik 2. Pertumbuhan M1 (yoy)") + picture("rId1"), {"image1.png": png})

    [chart] = word_charts(doc)

    assert (chart.caption, chart.title, chart.page_number, chart.png, chart.mime) == (
        "Grafik 2", "Pertumbuhan M1 (yoy)", None, png, "image/png")
    assert chart.emf_boxes is None


def test_an_emf_chart_is_rendered_and_keeps_its_texts():
    doc = docx_bytes(para("Grafik 1. Kegiatan Usaha") + picture("rId1"), {"image1.emf": _chart_emf()})

    [chart] = word_charts(doc)

    assert chart.png.startswith(b"\x89PNG") and chart.mime == "image/png"
    assert [b.text for b in chart.emf_boxes] == ["12,97", "Tw.II"]


def test_a_caption_with_no_picture_after_it_gives_no_chart():
    assert word_charts(docx_bytes(para("Grafik 3. Tanpa gambar") + para("Teks biasa."))) == []


def test_a_table_caption_is_not_a_chart():
    doc = docx_bytes(para("Tabel 1. Uang Beredar") + picture("rId1"), {"image1.png": _png()})

    assert word_charts(doc) == []


def test_the_next_caption_closes_the_one_before():
    doc = docx_bytes(para("Grafik 1. A") + para("Grafik 2. B") + picture("rId1"), {"image1.png": _png()})

    assert [c.caption for c in word_charts(doc)] == ["Grafik 2"]


def _pdf_fakes(monkeypatch, regions, captions, texts, pages=3):
    monkeypatch.setattr(chart_images, "chart_regions", lambda pdf: regions)
    monkeypatch.setattr(chart_images, "_render_regions",
                        lambda pdf, wanted, max_px: [(p, base64.b64encode(_png()).decode()) for p in sorted(wanted)
                                                     for _ in wanted[p]])
    monkeypatch.setattr(chart_images, "_page_texts", lambda pdf: texts)
    monkeypatch.setattr(chart_images, "_page_count", lambda pdf: pages)
    monkeypatch.setattr(chart_images, "_render_page", lambda pdf, page: _png(160, 220))
    monkeypatch.setattr(chart_images, "_caption_lines", lambda pdf: captions)


def test_a_pdf_chart_is_a_rendered_region_named_by_its_caption(monkeypatch):
    _pdf_fakes(monkeypatch, {3: [(50, 300, 550, 600)]}, {3: ["Grafik 4 Ekspor Nonmigas"]},
               {1: "x" * 50, 2: "x" * 50, 3: "x" * 50})

    charts, scanned = pdf_charts(b"%PDF")

    [chart] = charts
    assert (chart.caption, chart.title, chart.page_number) == ("Grafik 4", "Ekspor Nonmigas", 3)
    assert chart.png == _png() and scanned == []


def test_pdf_regions_without_a_matching_caption_are_named_by_page(monkeypatch):
    _pdf_fakes(monkeypatch, {3: [(50, 600, 300, 800), (50, 200, 300, 400)]}, {3: []},
               {1: "x" * 50, 2: "x" * 50, 3: "x" * 50})

    charts, _ = pdf_charts(b"%PDF")

    assert [c.caption for c in charts] == ["Grafik hal. 3 (1)", "Grafik hal. 3 (2)"]


def test_a_pdf_page_without_a_text_layer_is_a_scanned_page(monkeypatch):
    _pdf_fakes(monkeypatch, {}, {}, {1: "x" * 50, 2: "  ", 3: "x" * 50})

    charts, scanned = pdf_charts(b"%PDF")

    assert charts == [] and [p.page_number for p in scanned] == [2]
    assert Image.open(io.BytesIO(scanned[0].png)).size == (160, 220)


def test_a_zero_width_region_is_skipped(monkeypatch):
    # NPI Tw III-2026 R0.1 (PDF) page 5 is the list of charts: each of its lines came back as a box
    # of no width (left == right), which pypdfium2 refuses to crop.
    _pdf_fakes(monkeypatch, {5: [(28.5, 394.8, 28.5, 469.6)], 7: [(50, 300, 550, 600)]},
               {5: ["Grafik 1 Neraca ... 7"], 7: ["Grafik 1 Neraca Pembayaran"]}, {p: "x" * 50 for p in range(1, 9)}, pages=8)

    charts, _ = pdf_charts(b"%PDF")

    assert [(c.caption, c.page_number) for c in charts] == [("Grafik 1", 7)]


def test_the_list_of_charts_page_gives_no_charts(monkeypatch):
    # NPI Tw III-2026 page 5, "DAFTAR GRAFIK": every entry came back as a box under its caption.
    _pdf_fakes(monkeypatch, {5: [(28.5, 394.8, 300, 469.6)], 7: [(50, 300, 550, 600)]},
               {5: ["Grafik 1 Neraca Pembayaran ..... 7"], 7: ["Grafik 1 Neraca Pembayaran"]},
               {**{p: "x" * 50 for p in range(1, 9)}, 5: "DAFTAR GRAFIK Grafik 1 Neraca Pembayaran ..... 7"}, pages=8)

    charts, _ = pdf_charts(b"%PDF")

    assert [(c.caption, c.page_number) for c in charts] == [("Grafik 1", 7)]
