"""A whole check through the strict path, handed over by verify_paired only when STRICT_EXTRACTION
is on and the check is an Excel-only one; otherwise verify_paired runs exactly as before."""
import asyncio
from unittest.mock import AsyncMock, Mock, patch

import pytest
from langchain_core.runnables import RunnableLambda

from excel_parser_bi import BITableData
from strict.claims import CellRef, StrictClaim, StrictClaims

NARRATIVE = "[== Halaman 1 ==]\nPosisi AFLN Indonesia pada triwulan IV 2025 tercatat 558,5 miliar dolar AS.\n"


def _table():
    table = BITableData(title="Posisi Investasi Internasional", unit="Juta USD", row_labels=["Aset", "Kewajiban"])
    table._data[("Aset", 2025, "Q4")] = 558496.4949
    table._data[("Kewajiban", 2025, "Q4")] = 831121.5700
    return table


def _llm(seen):
    claim = StrictClaim(anchor_quote="tercatat 558,5 miliar dolar AS", subject="Posisi AFLN Indonesia",
                        operation="value", claimed_value_raw="558,5", unit="miliar USD",
                        cells=[CellRef(table="S1", row="Aset", period="Q4 2025")])

    def _respond(prompt_value):
        seen.append(prompt_value.to_messages())
        return StrictClaims(claims=[claim])

    llm = Mock()
    llm.with_structured_output = Mock(return_value=RunnableLambda(_respond))
    return llm


def _verify(**kw):
    from paired_verifier import verify_paired
    args = dict(narrative_text=NARRATIVE, excel_sources=[(b"x", "5.39", "t.xls")])
    args.update(kw)
    return asyncio.run(verify_paired(**args))


@patch("paired_verifier.extract_structured_facts_async", new_callable=AsyncMock, return_value=[])
@patch("paired_verifier.parse_bi_table")
def test_off_verify_paired_is_unchanged(mock_parse, mock_extract, monkeypatch):
    monkeypatch.delenv("STRICT_EXTRACTION", raising=False)
    mock_parse.return_value = _table()

    response = _verify(llm=_llm([]))

    assert mock_extract.called and response.strict_claims == []


@patch("paired_verifier.extract_structured_facts_async", new_callable=AsyncMock, return_value=[])
@patch("paired_verifier.parse_bi_table")
def test_on_a_check_goes_strict(mock_parse, mock_extract, monkeypatch):
    monkeypatch.setenv("STRICT_EXTRACTION", "1")
    mock_parse.return_value = _table()

    response = _verify(llm=_llm([]))

    assert not mock_extract.called
    [result] = response.results
    assert (result.verdict, result.resolved_via) == ("Entailed", "strict")
    assert response.entailed_count == 1 and response.total_facts == 1
    assert len(response.strict_claims) == 1 and response.strict_claims[0]["cells"][0]["row"] == "Aset"


@patch("paired_verifier.extract_structured_facts_async", new_callable=AsyncMock, return_value=[])
@patch("paired_verifier.parse_bi_table")
def test_on_and_both_mode_goes_strict_on_the_excel_tables(mock_parse, mock_extract, monkeypatch):
    # Render 2026-10-27: the team checks the press release and the report together (mode "both",
    # the other document as a reference), and strict was skipped for every such check.
    from pdf_table_extraction import PdfTable
    monkeypatch.setenv("STRICT_EXTRACTION", "1")
    mock_parse.return_value = _table()
    reference = ("Laporan.docx", [PdfTable(page_number=1, caption="Tabel 1", unit="", grid=[["", "Q3 2025", "Q4 2025"], ["Aset lain", 1.0, 2.0]])])

    response = _verify(llm=_llm([]), mode="both", reference_tables=[reference])

    assert not mock_extract.called
    assert response.mode == "both" and response.results[0].resolved_via == "strict"


@patch("paired_verifier.extract_structured_facts_async", new_callable=AsyncMock, return_value=[])
@patch("paired_verifier.parse_bi_table")
def test_on_but_internal_mode_stays_old(mock_parse, mock_extract, monkeypatch):
    monkeypatch.setenv("STRICT_EXTRACTION", "1")
    mock_parse.return_value = _table()

    response = _verify(llm=_llm([]), mode="internal")

    assert mock_extract.called and response.strict_claims == []


SETTINGS_BLOCK = ("PUBLIKASI: PII\nFrasa yang biasa dipakai → baris tabel:\n"
                  "- \"posisi afln indonesia\" → baris 'Aset'\n\n")


@patch("paired_verifier.team_settings.for_check", return_value=({"posisi afln indonesia": "Aset"}, SETTINGS_BLOCK))
@patch("paired_verifier.parse_bi_table")
def test_the_settings_block_reaches_the_extraction_prompt(mock_parse, _for_check, monkeypatch):
    monkeypatch.setenv("STRICT_EXTRACTION", "1")
    mock_parse.return_value = _table()
    seen = []

    _verify(llm=_llm(seen), publication="pii")

    assert "- \"posisi afln indonesia\" → baris 'Aset'" in seen[0][1].content
    assert "PADANAN TIM" not in seen[0][1].content


@patch("ai_review.review_claims", new_callable=AsyncMock, side_effect=RuntimeError("down"))
@patch("paired_verifier.parse_bi_table")
def test_a_review_failure_keeps_the_verdicts(mock_parse, _review, monkeypatch):
    monkeypatch.setenv("STRICT_EXTRACTION", "1")
    mock_parse.return_value = _table()

    response = _verify(llm=_llm([]), review_llm=Mock())

    assert [r.verdict for r in response.results] == ["Entailed"]


@pytest.mark.parametrize("value, on", [("1", True), (" ON ", True), ("yes", True), ("", False), ("0", False)])
def test_the_flag(monkeypatch, value, on):
    from strict.pipeline import enabled
    monkeypatch.setenv("STRICT_EXTRACTION", value)

    assert enabled() is on


def test_charts_or_a_category_sheet_keep_the_old_path():
    from paired_verifier import _ExcelSource
    from strict.pipeline import applies

    series = _ExcelSource(table=_table(), filename="t.xls", sheet="5.39")
    items = BITableData(title="Barang", unit="Rp", row_labels=["Laptop"])
    items.axis_type, items.col_labels = "categorical", ["Harga"]
    items._data[("Laptop", "Harga")] = 7.5
    category = _ExcelSource(table=items, filename="b.xlsx", sheet="Barang")

    assert applies([series], "excel", has_charts=False)
    assert not applies([series], "excel", has_charts=True)
    assert not applies([series, category], "excel", has_charts=False)


def test_strict_needs_an_uploaded_workbook_not_only_document_tables():
    from paired_verifier import _ExcelSource
    from strict.pipeline import applies

    own = _ExcelSource(table=_table(), filename="Laporan.docx", sheet="Lampiran 1", origin="pdf")

    assert not applies([own], "both", has_charts=False)
    assert applies([_ExcelSource(table=_table(), filename="t.xls", sheet="5.39"), own], "both", has_charts=False)


# --- two stages: the workbooks first, the documents' tables only for what they lack ----------------

TWO_SENTENCES = ("[== Halaman 1 ==]\nPosisi AFLN Indonesia pada triwulan IV 2025 tercatat 558,5 miliar dolar AS. "
                 "Kewajiban neto tercatat 272,6 miliar dolar AS.\n")


def _staged_llm(seen, excel_answer, document_answer):
    def _respond(prompt_value):
        messages = prompt_value.to_messages()
        seen.append(messages)
        return StrictClaims(claims=document_answer if "HANYA KALIMAT INI" in messages[1].content else excel_answer)

    llm = Mock()
    llm.with_structured_output = Mock(return_value=RunnableLambda(_respond))
    return llm


AFLN = StrictClaim(anchor_quote="tercatat 558,5 miliar dolar AS", subject="Posisi AFLN", operation="value",
                   claimed_value_raw="558,5", unit="miliar USD", cells=[CellRef(table="S1", row="Aset", period="Q4 2025")])
NETO_ABSENT = StrictClaim(anchor_quote="tercatat 272,6 miliar dolar AS", subject="Kewajiban neto", operation="value",
                          not_in_tables=True, reason="tidak ada di workbook")
NETO_IN_DOC = StrictClaim(anchor_quote="tercatat 272,6 miliar dolar AS", subject="Kewajiban neto", operation="value",
                          claimed_value_raw="272,6", unit="miliar USD",
                          cells=[CellRef(table="S2", row="Kewajiban neto", period="Q4 2025")])
AFLN_IN_DOC = StrictClaim(anchor_quote="tercatat 558,5 miliar dolar AS", subject="Posisi AFLN", operation="value",
                          claimed_value_raw="558,5", unit="miliar USD",
                          cells=[CellRef(table="S2", row="Kewajiban neto", period="Q4 2025")])


def _reference():
    from pdf_table_extraction import PdfTable
    grid = [["", "Q3 2025", "Q4 2025"], ["Kewajiban neto", 245.1, 272.6]]
    return ("Siaran Pers PII.docx", [PdfTable(page_number=1, caption="Tabel 1. PII", unit="Miliar USD", grid=grid)])


@patch("paired_verifier.parse_bi_table")
def test_a_figure_the_workbooks_lack_is_read_from_the_documents_tables(mock_parse, monkeypatch):
    monkeypatch.setenv("STRICT_EXTRACTION", "1")
    mock_parse.return_value = _table()
    seen = []

    response = _verify(narrative_text=TWO_SENTENCES, mode="both", reference_tables=[_reference()],
                       llm=_staged_llm(seen, [AFLN, NETO_ABSENT], [NETO_IN_DOC]))

    assert len(seen) == 2
    first, second = seen[0][1].content, seen[1][1].content
    assert "tabel di dokumen" not in first                       # stage 1: the workbooks only
    assert "tabel di dokumen lain (Siaran Pers PII.docx)" in second and "Posisi Investasi" not in second
    assert "Kewajiban neto tercatat 272,6 miliar dolar AS." in second
    verdicts = sorted((r.verdict, r.matched_excel_source.split(" / ")[0]) for r in response.results)
    assert verdicts == [("Entailed", "Siaran Pers PII.docx"), ("Entailed", "t.xls")]


@patch("paired_verifier.parse_bi_table")
def test_a_figure_the_workbooks_answer_never_goes_to_the_documents(mock_parse, monkeypatch):
    # Option B as first built let the model check M2 against the report's own table although SEKI
    # held it: a typo in that table, copied into the narrative, would then pass.
    monkeypatch.setenv("STRICT_EXTRACTION", "1")
    mock_parse.return_value = _table()
    seen = []

    response = _verify(narrative_text=TWO_SENTENCES, mode="both", reference_tables=[_reference()],
                       llm=_staged_llm(seen, [AFLN, NETO_ABSENT], [NETO_IN_DOC, AFLN_IN_DOC]))

    afln = [r for r in response.results if r.claimed_value == 558.5]
    assert len(afln) == 1 and afln[0].matched_excel_source.startswith("t.xls")


@patch("paired_verifier.parse_bi_table")
def test_no_second_stage_when_the_workbooks_answer_everything(mock_parse, monkeypatch):
    monkeypatch.setenv("STRICT_EXTRACTION", "1")
    mock_parse.return_value = _table()
    seen = []

    _verify(narrative_text=TWO_SENTENCES, mode="both", reference_tables=[_reference()],
            llm=_staged_llm(seen, [AFLN], [NETO_IN_DOC]))

    assert len(seen) == 1
