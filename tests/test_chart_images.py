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
from unittest.mock import Mock

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


def test_a_caption_whose_pictures_never_come_is_dropped_at_the_next_table():
    doc = docx_bytes(para("Grafik 1. A") + para("Tabel 1. Uang Beredar") + picture("rId1") + para("Grafik 2. B")
                     + picture("rId2"), {"image1.png": _png(), "image2.png": _png()})

    assert [c.caption for c in word_charts(doc)] == ["Grafik 2"]


def _pdf_fakes(monkeypatch, regions, captions, texts, pages=3):
    monkeypatch.setattr(chart_images, "_page_texts", lambda pdf: texts)
    monkeypatch.setattr(chart_images, "_page_count", lambda pdf: pages)
    monkeypatch.setattr(chart_images, "_render_page", lambda pdf, page: _png(160, 220))
    monkeypatch.setattr(chart_images, "_caption_lines", lambda pdf: captions)


def test_a_pdf_page_with_a_chart_caption_goes_to_the_model_whole(monkeypatch):
    # NPI Tw III-2026 R0.1: the text-layer locator cut most charts to their axis and legend, so a
    # page that names a chart is sent whole and the model gives each chart's box (locate_charts).
    _pdf_fakes(monkeypatch, {}, {3: ["Grafik 4 Ekspor Nonmigas"]}, {1: "x" * 50, 2: "x" * 50, 3: "x" * 50})

    charts, pages = pdf_charts(b"%PDF")

    assert charts == [] and [p.page_number for p in pages] == [3]
    assert Image.open(io.BytesIO(pages[0].png)).size == (160, 220)


def test_a_pdf_page_without_a_text_layer_is_a_scanned_page(monkeypatch):
    _pdf_fakes(monkeypatch, {}, {}, {1: "x" * 50, 2: "  ", 3: "x" * 50})

    charts, scanned = pdf_charts(b"%PDF")

    assert charts == [] and [p.page_number for p in scanned] == [2]
    assert Image.open(io.BytesIO(scanned[0].png)).size == (160, 220)


def test_the_list_of_charts_page_gives_no_charts(monkeypatch):
    # NPI Tw III-2026 page 5, "DAFTAR GRAFIK": every entry came back as a box under its caption.
    _pdf_fakes(monkeypatch, {5: [(28.5, 394.8, 300, 469.6)], 7: [(50, 300, 550, 600)]},
               {5: ["Grafik 1 Neraca Pembayaran ..... 7"], 7: ["Grafik 1 Neraca Pembayaran"]},
               {**{p: "x" * 50 for p in range(1, 9)}, 5: "DAFTAR GRAFIK Grafik 1 Neraca Pembayaran ..... 7"}, pages=8)

    charts, pages = pdf_charts(b"%PDF")

    assert charts == [] and [p.page_number for p in pages] == [7]


def test_the_title_lines_after_a_caption_are_its_title():
    # SKDU Tw III-2026: "Grafik 9" / "Perkembangan Tenaga Kerja" / "LU Pertanian, Kehutanan, dan Perikanan" / picture.
    doc = docx_bytes(para("Grafik 9") + para("Perkembangan Tenaga Kerja") + para("LU Pertanian, Kehutanan, dan Perikanan")
                     + picture("rId1"), {"image1.png": _png()})

    [chart] = word_charts(doc)

    assert (chart.caption, chart.title) == ("Grafik 9", "Perkembangan Tenaga Kerja LU Pertanian, Kehutanan, dan Perikanan")


def test_charts_side_by_side_take_their_pictures_in_caption_order():
    # SKDU Tw III-2026: "Grafik 3" "Kondisi Likuiditas" "Grafik 4" "Kondisi Rentabilitas", then both pictures.
    first, second = _png(colour=(1, 2, 3)), _png(colour=(4, 5, 6))
    doc = docx_bytes(para("Grafik 3") + para("Kondisi Likuiditas") + para("Grafik 4") + para("Kondisi Rentabilitas")
                     + picture("rId1") + picture("rId2"), {"image1.png": first, "image2.png": second})

    charts = word_charts(doc)

    assert [(c.caption, c.title, c.png) for c in charts] == [("Grafik 3", "Kondisi Likuiditas", first),
                                                             ("Grafik 4", "Kondisi Rentabilitas", second)]


# --- final review (2026-11-24) --------------------------------------------------------------------

def test_pdf_pages_are_counted_and_rendered_holding_the_pdfium_lock(monkeypatch):
    # Two threads inside PDFium at once killed Render with no traceback (pdf_extraction._PDFIUM_LOCK).
    import threading
    import pypdfium2
    import pdf_extraction

    held = []

    class _Lock:
        def __init__(self):
            self.depth = 0

        def __enter__(self):
            self.depth += 1

        def __exit__(self, *exc):
            self.depth -= 1

    lock = _Lock()
    monkeypatch.setattr(pdf_extraction, "_PDFIUM_LOCK", lock)

    class _Page:
        def get_size(self):
            held.append(lock.depth)
            return 100, 200

        def render(self, scale):
            return Mock(to_pil=lambda: Image.new("RGB", (10, 20)))

    class _Doc:
        def __init__(self, data):
            held.append(lock.depth)

        def __len__(self):
            return 1

        def __getitem__(self, i):
            return _Page()

        def close(self):
            pass

    monkeypatch.setattr(pypdfium2, "PdfDocument", _Doc)

    chart_images._page_count(b"%PDF")
    chart_images._render_page(b"%PDF", 1)

    assert held and all(depth > 0 for depth in held)


def test_a_sentence_that_starts_with_grafik_n_is_not_a_caption():
    doc = docx_bytes(para("Grafik 2 menunjukkan bahwa pertumbuhan kredit melambat sejalan dengan permintaan yang"
                          " tertahan di tengah ketidakpastian global yang masih tinggi pada triwulan ini.")
                     + para("Grafik 3") + para("Kredit") + picture("rId1"), {"image1.png": _png()})

    assert [(c.caption, c.title) for c in word_charts(doc)] == [("Grafik 3", "Kredit")]


def test_the_list_of_charts_queues_no_captions():
    doc = docx_bytes(para("DAFTAR GRAFIK") + para("Grafik 1 Neraca Pembayaran") + para("7")
                     + para("Grafik 2 Transaksi Berjalan") + para("8")
                     + para("Grafik 1") + para("Neraca Pembayaran") + picture("rId1"), {"image1.png": _png()})

    assert [(c.caption, c.title) for c in word_charts(doc)] == [("Grafik 1", "Neraca Pembayaran")]


def test_a_caption_over_a_native_word_chart_takes_that_chart_not_the_next_picture():
    native = ('<w:p><w:r><w:drawing><a:graphic><a:graphicData><c:chart xmlns:c="http://schemas.openxmlformats.org/'
              'drawingml/2006/chart" r:id="rId9"/></a:graphicData></a:graphic></w:drawing></w:r></w:p>')
    doc = docx_bytes(para("Grafik 1") + para("Ekspor") + native + para("Teks narasi singkat.")
                     + para("Grafik 2") + para("Impor") + picture("rId1"), {"image1.png": _png()})

    assert [(c.caption, c.title) for c in word_charts(doc)] == [("Grafik 2", "Impor")]


def test_a_caption_whose_picture_never_comes_is_dropped_at_the_next_paragraph_of_prose():
    doc = docx_bytes(para("Grafik 1") + para("Ekspor")
                     + para("Ekspor nonmigas tumbuh 9,3% (yoy) pada triwulan III 2026, lebih tinggi dibandingkan "
                            "triwulan sebelumnya, didorong oleh peningkatan ekspor produk agrikultur dan bahan bakar.")
                     + para("Grafik 2") + para("Impor") + picture("rId1"), {"image1.png": _png()})

    assert [(c.caption, c.title) for c in word_charts(doc)] == [("Grafik 2", "Impor")]


def test_a_caption_under_its_picture_takes_that_picture():
    # NPI Tw II-2026 R2: the picture, then "Grafik 4", then its title, then the text goes on.
    first, second = _png(colour=(1, 2, 3)), _png(colour=(4, 5, 6))
    prose = ("Secara nominal, ekspor nonmigas pada triwulan II 2026 tercatat sebesar 70,5 miliar dolar AS, lebih "
             "tinggi dibandingkan triwulan sebelumnya yang tercatat sebesar 66,1 miliar dolar AS.")
    doc = docx_bytes(para(prose) + picture("rId1") + para("Grafik 4") + para("Pertumbuhan Ekspor Nonmigas")
                     + para(prose) + picture("rId2") + para("Grafik 5") + para("Pertumbuhan Impor Nonmigas")
                     + para("Tabel 1"), {"image1.png": first, "image2.png": second})

    assert [(c.caption, c.title, c.png) for c in word_charts(doc)] == [
        ("Grafik 4", "Pertumbuhan Ekspor Nonmigas", first), ("Grafik 5", "Pertumbuhan Impor Nonmigas", second)]


def test_a_source_line_after_a_picture_is_not_a_title_and_keeps_the_next_caption_waiting():
    # SKDU Tw III-2026 Grafik 8, 9: both captions, the first picture, "Sumber: …", the second picture.
    first, second = _png(colour=(1, 2, 3)), _png(colour=(4, 5, 6))
    doc = docx_bytes(para("Grafik 8") + para("Kegiatan Usaha") + para("Grafik 9") + para("Tenaga Kerja")
                     + para("LU Pertanian") + picture("rId1") + para("Sumber: SKDU (BI)") + picture("rId2"),
                     {"image1.png": first, "image2.png": second})

    assert [(c.caption, c.title, c.png) for c in word_charts(doc)] == [
        ("Grafik 8", "Kegiatan Usaha", first), ("Grafik 9", "Tenaga Kerja LU Pertanian", second)]


def test_the_pictures_under_a_table_caption_are_not_taken_by_the_chart_caption_after_them():
    table, chart = _png(colour=(1, 2, 3)), _png(colour=(4, 5, 6))
    doc = docx_bytes(para("Tabel 3") + para("Uang Beredar") + picture("rId1") + para("Grafik 5") + para("Kredit")
                     + picture("rId2"), {"image1.png": table, "image2.png": chart})

    assert [(c.caption, c.png) for c in word_charts(doc)] == [("Grafik 5", chart)]


def test_a_caption_under_its_picture_has_one_title_line_and_not_the_heading_after_it():
    doc = docx_bytes(picture("rId1") + para("Grafik 2") + para("Transaksi Berjalan") + para("Neraca Perdagangan Barang")
                     + para("Neraca perdagangan barang triwulan II 2026 mencatat surplus yang lebih tinggi terutama "
                            "dipengaruhi peningkatan surplus neraca perdagangan nonmigas dan defisit migas yang turun."),
                     {"image1.png": _png()})

    assert [(c.caption, c.title) for c in word_charts(doc)] == [("Grafik 2", "Transaksi Berjalan")]
