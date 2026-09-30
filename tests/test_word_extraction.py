"""word_extraction: a Word report's narrative and the tables it prints as EMF pictures."""

from word_extraction import EmfText, emf_table_lines, emf_text_runs
from word_fixtures import SNIPPET_RUNS, emf_bytes


# --- EMF text records -----------------------------------------------------------------------

def test_emf_text_runs_reads_every_text_record_with_its_position():
    runs = emf_text_runs(emf_bytes([(10, 20, "Giro"), (90, 20, "3.130,8")]))
    assert runs == [EmfText(10, 20, "Giro"), EmfText(90, 20, "3.130,8")]


def test_emf_text_runs_ignores_what_is_not_an_emf():
    assert emf_text_runs(b"\x89PNG\r\n\x1a\n" + bytes(100)) == []
    assert emf_text_runs(b"") == []


def test_emf_text_runs_survives_a_truncated_file():
    data = emf_bytes([(10, 20, "Giro"), (90, 20, "3.130,8")])
    assert emf_text_runs(data[:-30]) == [EmfText(10, 20, "Giro")]


# --- rows -----------------------------------------------------------------------------------

def _texts(lines):
    return [text for _, text in lines]


def test_lines_run_top_to_bottom_with_the_label_before_its_values():
    lines = emf_table_lines(emf_text_runs(emf_bytes(SNIPPET_RUNS)))
    assert lines[0][0] > lines[-1][0]          # PDF convention: larger y is higher
    assert "Uang Beredar Luas (M2) 10.355,7 10.253,7 9,7 9,2" in _texts(lines)


def test_a_label_that_wraps_just_above_its_values_joins_them():
    texts = _texts(emf_table_lines(emf_text_runs(emf_bytes(SNIPPET_RUNS))))
    assert "Uang Kartal di Luar Bank Umum dan BPR 1.206,1 1.186,3 10,8 15,7" in texts


def test_a_label_split_around_its_values_reads_in_order():
    # Tabel 9's last row: "Surat Berharga … Sektor" / values / "Swasta**", 13 units apart
    # against a row pitch of ~37.
    runs = [(100, 34, "Mar"), (160, 34, "Apr*"), (240, 34, "Mar'26"),
            (0, 77, "Giro"), (100, 77, "5,5"), (160, 77, "7,6"), (240, 77, "39,2"),
            (0, 114, "Uang Kartal"), (100, 114, "1,0"), (160, 114, "2,0"), (240, 114, "3,0"),
            (0, 138, "Surat Berharga Sektor"), (100, 151, "26,7"), (160, 151, "36,1"),
            (240, 151, "35,0"), (0, 164, "Swasta**")]
    texts = _texts(emf_table_lines(emf_text_runs(emf_bytes(runs))))
    assert "Surat Berharga Sektor Swasta** 26,7 36,1 35,0" in texts


def test_header_lines_are_never_merged_into_a_data_row():
    texts = _texts(emf_table_lines(emf_text_runs(emf_bytes(SNIPPET_RUNS))))
    assert "2026 % (yoy)" in texts and "Komponen Uang Beredar" in texts
    assert "Mar Apr* Mar'26 Apr'26*" in texts


def test_without_a_period_header_rows_are_only_grouped_by_height():
    runs = [(0, 10, "a"), (50, 10, "1,0"), (0, 11, "b")]
    assert _texts(emf_table_lines(emf_text_runs(emf_bytes(runs)))) == ["a 1,0", "b"]


# ===========================================================================================
# The whole document
# ===========================================================================================

import io
import zipfile
from pathlib import Path

import pytest

from structured_extractor import _filter_narrative
from word_extraction import is_legacy_word_document, is_word_document, read_word_document
from word_fixtures import docx_bytes, para, picture

_CAPTION = "Tabel 1.  Uang Beredar dan Komponennya (triliun Rp)"
_PNG = b"\x89PNG\r\n\x1a\n"


def _row(table, label):
    return next(r for r in table.grid if r and r[0] == label)


# --- detection ------------------------------------------------------------------------------

def test_a_docx_is_recognised_and_other_files_are_not():
    assert is_word_document(docx_bytes(para("teks")))
    assert not is_word_document(b"%PDF-1.4 fake")
    assert not is_word_document(b"PK\x03\x04 not really a zip")


def test_an_xlsx_is_not_a_word_document():
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as z:
        z.writestr("xl/workbook.xml", "<workbook/>")
    assert not is_word_document(buf.getvalue())


def test_legacy_doc_is_recognised_by_its_name():
    assert is_legacy_word_document("Laporan.DOC")
    assert not is_legacy_word_document("Laporan.docx")


# --- tables ---------------------------------------------------------------------------------

def test_a_caption_and_its_emf_picture_become_level_and_growth_tables():
    doc = read_word_document(docx_bytes(
        para(_CAPTION) + picture("rId1"), {"image1.emf": emf_bytes(SNIPPET_RUNS)}))
    level, growth = doc.tables
    assert level.label == "Tabel 1. Uang Beredar dan Komponennya (triliun Rp)"
    assert level.page_number == 0 and level.verified
    assert _row(level, "Uang Beredar Luas (M2)")[1:3] == [10355.7, 10253.7]
    assert _row(growth, "Uang Beredar Luas (M2)")[1:3] == [9.7, 9.2]
    assert _row(level, "Uang Kartal di Luar Bank Umum dan BPR")[1] == 1206.1
    assert doc.unread_tables == []


def test_png_table_picture_is_reported_unread():
    doc = read_word_document(docx_bytes(para(_CAPTION) + picture("rId1"), {"image1.png": _PNG}))
    assert doc.tables == []
    assert doc.unread_tables == [
        "Tabel 1. Uang Beredar dan Komponennya (triliun Rp) — gambar tabel berformat PNG, "
        "belum bisa dibaca"]


def test_caption_without_a_picture_is_reported_unread():
    doc = read_word_document(docx_bytes(para(_CAPTION) + para("Tabel 2. Kredit (triliun Rp)")))
    assert [u.split(" — ")[1] for u in doc.unread_tables] == [
        "tidak ada gambar tabel sesudah judul ini"] * 2


def test_an_emf_that_does_not_form_a_table_is_reported_unread():
    runs = [(0, 10, "tidak"), (50, 10, "ada"), (100, 10, "periode")]
    doc = read_word_document(docx_bytes(
        para(_CAPTION) + picture("rId1"), {"image1.emf": emf_bytes(runs)}))
    assert doc.tables == [] and "tidak bisa disusun" in doc.unread_tables[0]


def test_a_sentence_starting_with_tabel_is_not_a_caption():
    doc = read_word_document(docx_bytes(
        para(_CAPTION) + picture("rId1")
        + para("Tabel 1 dan Grafik 1 menunjukkan M2 tumbuh 9,2% (yoy).") + picture("rId2"),
        {"image1.emf": emf_bytes(SNIPPET_RUNS), "image2.png": _PNG}))
    assert len(doc.tables) == 2 and doc.unread_tables == []


def test_a_chart_picture_is_neither_a_table_nor_unread():
    doc = read_word_document(docx_bytes(
        para("Grafik 1. Pertumbuhan M2 (yoy)") + picture("rId1"),
        {"image1.emf": emf_bytes(SNIPPET_RUNS)}))
    assert doc.tables == [] and doc.unread_tables == []


# --- narrative ------------------------------------------------------------------------------

def test_narrative_is_clean_text_in_page_blocks():
    doc = read_word_document(docx_bytes(
        para("M2\u00a0tumbuh\u202f9,2% (yoy).") + "<w:p><w:r><w:t>a</w:t><w:tab/>"
        "<w:t>b</w:t></w:r></w:p>"))
    assert doc.narrative_text == "[== Halaman 1 ==]\nM2 tumbuh 9,2% (yoy).\na b"


def test_alternate_content_fallback_is_not_read_twice():
    box = ('<w:p><w:r><mc:AlternateContent><mc:Choice><w:drawing><w:txbxContent>'
           + para("April 2026") + '</w:txbxContent></w:drawing></mc:Choice><mc:Fallback>'
           '<w:pict><w:txbxContent>' + para("April 2026") + '</w:txbxContent></w:pict>'
           '</mc:Fallback></mc:AlternateContent></w:r></w:p>')
    doc = read_word_document(docx_bytes(box))
    assert doc.narrative_text.count("April 2026") == 1


def test_appendix_opens_its_own_block_and_is_filtered_out():
    body = (para("Posisi M2 pada April 2026 tercatat sebesar Rp10.253,7 triliun atau tumbuh 9,2%.")
            + para("Lampiran 6. Tabel Uang Primer dan Faktor-Faktor yang Memengaruhinya")
            + para("Posisi GWM Ketentuan untuk BUK adalah Januari 2020 (5,5%), Mei 2020 (3%), "
                   "Juli 2021 (3,5%), Maret 2022 (5%)."))
    doc = read_word_document(docx_bytes(body))
    assert doc.narrative_text.count("[== Halaman") == 2
    kept = _filter_narrative(doc.narrative_text)
    assert "Rp10.253,7" in kept and "GWM" not in kept


def test_an_explicit_page_break_opens_a_new_block():
    body = para("satu dua tiga empat lima enam") + (
        '<w:p><w:r><w:br w:type="page"/><w:t>tujuh delapan</w:t></w:r></w:p>')
    assert read_word_document(docx_bytes(body)).narrative_text.count("[== Halaman") == 2


# --- the real sample (skipped where the file is not present) --------------------------------

_SAMPLE = (Path(__file__).resolve().parents[1] / "sample_data"
           / "test case_Analisis Uang Beredar Posisi April.docx")


@pytest.mark.skipif(not _SAMPLE.exists(), reason="Word sample report not present")
def test_real_april_report_reads_like_the_pdf_route():
    doc = read_word_document(_SAMPLE.read_bytes())
    assert len(doc.tables) == 22
    assert len(doc.unread_tables) == 1 and doc.unread_tables[0].startswith("Tabel 9.")
    tabel1 = next(t for t in doc.tables if t.caption.startswith("Tabel 1.") and t.unit != "%, yoy")
    assert _row(tabel1, "Uang Beredar Luas (M2)")[1:3] == [10355.7, 10253.7]
    tabel4 = next(t for t in doc.tables if t.caption.startswith("Tabel 4.") and t.unit != "%, yoy")
    assert _row(tabel4, "Giro > Korporasi")[1:3] == [2770.5, 2694.2]
    assert "Rp10.253,7 triliun" in doc.narrative_text
    assert "\u00a0" not in doc.narrative_text and "April 2026April 2026" not in doc.narrative_text
    assert "Posisi GWM Ketentuan" not in _filter_narrative(doc.narrative_text)


def test_a_real_word_table_after_a_caption_is_reported_as_such():
    word_table = ('<w:tbl><w:tr><w:tc>' + para("Uang Beredar") + '</w:tc><w:tc>' + para("10.253,7")
                  + '</w:tc></w:tr></w:tbl>')
    doc = read_word_document(docx_bytes(
        para(_CAPTION) + word_table + picture("rId1"), {"image1.png": _PNG}))
    assert doc.unread_tables == [
        "Tabel 1. Uang Beredar dan Komponennya (triliun Rp) — tabel Word (bukan gambar), "
        "belum bisa dibaca"]


def test_a_layout_table_holding_the_picture_does_not_close_the_caption():
    layout = '<w:tbl><w:tr><w:tc>' + picture("rId1") + '</w:tc></w:tr></w:tbl>'
    doc = read_word_document(docx_bytes(para(_CAPTION) + layout,
                                        {"image1.emf": emf_bytes(SNIPPET_RUNS)}))
    assert len(doc.tables) == 2 and doc.unread_tables == []
