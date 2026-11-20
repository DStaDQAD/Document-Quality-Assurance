"""A Word (.docx) report goes through the same paired endpoints as a PDF."""

import json
from unittest.mock import Mock, patch

from fastapi.testclient import TestClient

import main
from schemas import FactVerificationResult, PairedVerificationResponse, TypoCheckResponse
from word_fixtures import SNIPPET_RUNS, docx_bytes, emf_bytes, para, picture

client = TestClient(main.app)

_DOCX_MIME = "application/vnd.openxmlformats-officedocument.wordprocessingml.document"
_WORD = docx_bytes(
    para("Posisi M2 pada April 2026 tercatat sebesar Rp10.253,7 triliun.")
    + para("Tabel 1.  Uang Beredar dan Komponennya (triliun Rp)") + picture("rId1")
    + para("Tabel 9. Komponen Uang Primer (triliun Rp)") + picture("rId2"),
    {"image1.emf": emf_bytes(SNIPPET_RUNS), "image2.png": b"\x89PNG\r\n\x1a\n"},
)
_WORD_UPLOAD = [("pdf_file", ("laporan.docx", _WORD, _DOCX_MIME))]


def _fact_response(results=()):
    return PairedVerificationResponse(
        pdf_filename="laporan.docx", excel_filenames=[], excel_sheets=[], excel_units=[],
        total_facts=len(results), entailed_count=len(results), refuted_count=0,
        inconclusive_count=0, results=list(results),
    )


def _typo_response():
    return TypoCheckResponse(total_issues=0, ejaan_count=0, tidak_baku_count=0, grammar_count=0,
                             summary="Tidak ditemukan isu.", issues=[])


def _fact(page):
    return FactVerificationResult(operation="value", metric_label="M2", verdict="Entailed",
                                  reasoning="", context_quote="M2 …", page_number=page)


@patch("main.check_typos")
@patch("main.verify_paired")
@patch("main.extract_tables_from_pdf")
@patch("main.extract_narrative_text")
@patch("main.get_vision_llm")
def test_internal_mode_reads_the_word_file_without_the_pdf_readers(
    mock_vision, mock_narrative, mock_tables, mock_verify, mock_typos
):
    mock_vision.return_value = Mock()
    mock_verify.return_value = _fact_response([_fact(page=3)])
    mock_typos.return_value = _typo_response()

    response = client.post("/api/verify-paired?mode=internal", files=_WORD_UPLOAD)

    assert response.status_code == 200, response.text
    mock_narrative.assert_not_called()
    mock_tables.assert_not_called()
    kwargs = mock_verify.call_args.kwargs
    assert "Rp10.253,7 triliun" in kwargs["narrative_text"]
    assert [t.unit for t in kwargs["pdf_tables"]] == ["triliun Rp", "%, yoy"]
    body = response.json()
    assert body["results"][0]["page_number"] is None      # Word has no fixed pages
    assert body["unread_tables"] == [
        "Tabel 9. Komponen Uang Primer (triliun Rp) — gambar tabel berformat PNG, belum bisa dibaca"]


@patch("main.check_typos")
@patch("main.verify_paired")
@patch("main.get_vision_llm")
def test_word_without_any_readable_table_fails_clearly_in_internal_mode(
    mock_vision, mock_verify, mock_typos
):
    mock_vision.return_value = Mock()
    upload = [("pdf_file", ("laporan.docx", docx_bytes(para("Hanya teks.")), _DOCX_MIME))]

    response = client.post("/api/verify-paired?mode=internal", files=upload)

    assert response.status_code == 400
    assert "Word" in response.json()["detail"]
    mock_verify.assert_not_called()


def test_legacy_doc_is_refused_with_a_clear_message():
    upload = [("pdf_file", ("laporan.doc", b"\xd0\xcf\x11\xe0 old", "application/msword"))]
    response = client.post("/api/verify-paired?mode=internal", files=upload)
    assert response.status_code == 400
    assert ".docx" in response.json()["detail"]


@patch("main.check_typos")
@patch("main.verify_paired")
@patch("main.extract_tables_from_pdf")
@patch("main.extract_narrative_text")
@patch("main.get_vision_llm")
def test_word_reference_is_read_as_word(
    mock_vision, mock_narrative, mock_tables, mock_verify, mock_typos
):
    mock_vision.return_value = Mock()
    mock_narrative.return_value = "[== Halaman 1 ==]\nteks"
    mock_verify.return_value = _fact_response()
    mock_typos.return_value = _typo_response()
    files = [("pdf_file", ("report.pdf", b"%PDF-1.4 fake", "application/pdf")),
             ("reference_pdf", ("laporan.docx", _WORD, _DOCX_MIME))]

    response = client.post("/api/verify-paired?mode=none", files=files)

    assert response.status_code == 200, response.text
    mock_tables.assert_not_called()                     # the Word file never reached the PDF reader
    [(name, tables)] = mock_verify.call_args.kwargs["reference_tables"]
    assert name == "laporan.docx" and len(tables) == 2


_ALL_PNG_WORD = docx_bytes(
    para("Posisi M2 pada April 2026 tercatat sebesar Rp10.253,7 triliun.")
    + para("Tabel 1.  Uang Beredar dan Komponennya (triliun Rp)") + picture("rId1"),
    {"image1.png": b"\x89PNG\r\n\x1a\n"},
)


@patch("main.check_typos")
@patch("main.verify_paired")
@patch("main.get_vision_llm")
def test_both_mode_falls_back_to_excel_when_no_word_table_is_readable(
    mock_vision, mock_verify, mock_typos
):
    mock_vision.return_value = Mock()
    mock_verify.return_value = _fact_response()
    mock_typos.return_value = _typo_response()
    files = [("pdf_file", ("laporan.docx", _ALL_PNG_WORD, _DOCX_MIME)),
             ("excel_file", ("TABEL1_1.xls", b"xls-bytes", "application/vnd.ms-excel"))]

    response = client.post("/api/verify-paired?mode=both", files=files)

    assert response.status_code == 200, response.text
    assert mock_verify.call_args.kwargs["pdf_tables"] == []
    assert response.json()["unread_tables"][0].startswith("Tabel 1.")


@patch("main.get_vision_llm")
def test_internal_mode_error_names_the_unread_tables(mock_vision):
    mock_vision.return_value = Mock()
    upload = [("pdf_file", ("laporan.docx", _ALL_PNG_WORD, _DOCX_MIME))]
    response = client.post("/api/verify-paired?mode=internal", files=upload)
    assert response.status_code == 400
    assert "Tabel 1." in response.json()["detail"] and "PNG" in response.json()["detail"]


@patch("main.check_typos")
@patch("main.verify_paired")
@patch("main.get_vision_llm")
def test_coverage_gaps_of_a_word_report_carry_no_page(mock_vision, mock_verify, mock_typos):
    mock_vision.return_value = Mock()
    mock_typos.return_value = _typo_response()

    async def failing_chunk(**kwargs):
        kwargs["on_extract_gap"]([1], "429 RESOURCE_EXHAUSTED")
        return _fact_response()
    mock_verify.side_effect = failing_chunk

    response = client.post("/api/verify-paired?mode=internal", files=_WORD_UPLOAD)

    assert response.status_code == 200, response.text
    assert response.json()["coverage_gaps"][0]["pages"] == []


def test_a_corrupt_docx_is_refused_as_a_word_file():
    upload = [("pdf_file", ("laporan.docx", b"PK\x03\x04 truncated", _DOCX_MIME))]
    response = client.post("/api/verify-paired?mode=internal", files=upload)
    assert response.status_code == 400
    assert "Word" in response.json()["detail"] and "PDFium" not in response.json()["detail"]


def test_a_docx_with_broken_xml_is_refused_as_a_word_file():
    import io, zipfile
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as z:
        z.writestr("word/document.xml", "<w:document><w:body>")
    upload = [("pdf_file", ("laporan.docx", buf.getvalue(), _DOCX_MIME))]
    response = client.post("/api/verify-paired?mode=internal", files=upload)
    assert response.status_code == 400
    assert "rusak" in response.json()["detail"]



def test_excel_mode_names_the_tables_whose_picture_holds_no_text():
    from word_extraction import WordDocument
    word = WordDocument(narrative_text="", unread_tables=["Tabel 9. X — tidak bisa disusun"],
                        unreadable_tables={"Tabel 3": "Tabel 3. Y — gambar PNG, isinya tidak bisa dibaca"})
    assert main._unread_tables(word, "excel") == ["Tabel 3. Y — gambar PNG, isinya tidak bisa dibaca"]
    assert main._unread_tables(word, "both") == ["Tabel 9. X — tidak bisa disusun"]
    assert main._unread_tables(None, "excel") == []


@patch("main.check_table_typos")
@patch("main.check_typos")
@patch("main.verify_paired")
@patch("main.get_vision_llm")
def test_the_word_tables_words_join_the_typo_check(mock_vision, mock_verify, mock_typos, mock_table_typos):
    from schemas import TypoIssue
    mock_vision.return_value = Mock()
    mock_verify.return_value = _fact_response()
    mock_typos.return_value = _typo_response()
    mock_table_typos.return_value = [TypoIssue(word="Kompensansi", start=0, end=11, category="ejaan",
                                               suggestion="kompensasi", explanation="", location="Tabel 1")]

    response = client.post("/api/verify-paired?mode=internal", files=_WORD_UPLOAD)

    assert response.status_code == 200, response.text
    [tables, _llm] = mock_table_typos.call_args.args
    assert [t.location for t in tables] == ["Tabel 1"]          # the EMF table's printed words
    assert "Uang Beredar Sempit (M1)" in tables[0].cells
    typo = response.json()["typo_check"]
    assert typo["total_issues"] == 1 and typo["issues"][0]["location"] == "Tabel 1"


@patch("main.check_table_typos", side_effect=RuntimeError("model down"))
@patch("main.check_typos")
@patch("main.verify_paired")
@patch("main.get_vision_llm")
def test_a_failing_table_typo_check_keeps_the_narratives(mock_vision, mock_verify, mock_typos, _mock_table_typos):
    mock_vision.return_value = Mock()
    mock_verify.return_value = _fact_response()
    mock_typos.return_value = _typo_response()

    response = client.post("/api/verify-paired?mode=internal", files=_WORD_UPLOAD)

    assert response.status_code == 200, response.text
    assert response.json()["typo_check"]["summary"] == "Tidak ditemukan isu."
