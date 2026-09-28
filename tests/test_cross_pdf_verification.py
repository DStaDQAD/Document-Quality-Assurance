"""Cross-PDF verification: one report's narrative checked against the tables of the OTHER PDFs
uploaded in the same run ("Konsistensi antar-file").

The other PDFs are a fallback, never a rival: the report's own tables and the uploaded Excel
decide the verdict whenever they can, and another PDF only answers when they cannot — a July
report quoting a June figure its own tables no longer print. When both can answer and disagree,
that is reported as a "cross_pdf" conflict (often a revised figure), not as a different verdict.
"""

import asyncio
import json
from unittest.mock import Mock, patch

import pytest
from fastapi.testclient import TestClient

import main
from excel_parser_bi import BITableData
from paired_verifier import _ExcelSource, _evaluate_fact, verify_paired
from pdf_table_extraction import PdfTable, _PdfTableOut, _assemble_grid
from schemas import PairedVerificationResponse, TypoCheckResponse
from structured_extractor import ExtractedFact, PeriodPoint


def _make_table(unit="triliun Rp", data=None):
    table = BITableData(title="Uang Beredar (M2)", unit=unit, row_labels=[])
    for (label, year, month), value in (data or {}).items():
        if label not in table.row_labels:
            table.row_labels.append(label)
        table._data[(label, year, month)] = value
    return table


def _own(table, page=7):
    return _ExcelSource(table=table, filename="M2-Juli-2026.pdf",
                        sheet=f"Hal. {page} · Lampiran 1", origin="pdf")


def _excel(table):
    return _ExcelSource(table=table, filename="TABEL1_1.xls", sheet="I.1")


def _other(table, page=7):
    return _ExcelSource(table=table, filename="M2-Juni-2026.pdf",
                        sheet=f"Hal. {page} · Lampiran 1", origin="pdf_other")


def _fact(**overrides):
    base = dict(
        operation="value",
        periods=[PeriodPoint(metric_label="Total", year=2026, month="Jun")],
        claimed_value=10355.1, unit="triliun Rp", context_quote="quote", page_number=1,
    )
    base.update(overrides)
    return ExtractedFact(**base)


# ---------------------------------------------------------------------------
# _evaluate_fact: the other PDFs are a fallback
# ---------------------------------------------------------------------------

def test_the_reports_own_table_decides_even_when_another_pdf_disagrees():
    own = _make_table(data={("Total", 2026, "Jun"): 10355.1})
    other = _make_table(data={("Total", 2026, "Jun"): 10301.0})
    # The other PDF is listed FIRST on purpose: the priority must not depend on source order.
    result = _evaluate_fact(_fact(), [_other(other), _own(own)])

    assert result.verdict == "Entailed"
    assert result.matched_excel_source == "M2-Juli-2026.pdf / Hal. 7 · Lampiran 1"
    assert result.source_conflict == "cross_pdf"
    assert [sv.origin for sv in result.source_values] == ["pdf", "pdf_other"]
    assert "PDF lain" in result.reasoning


def test_excel_also_outranks_another_pdf():
    excel = _make_table(data={("Total", 2026, "Jun"): 10355.1})
    other = _make_table(data={("Total", 2026, "Jun"): 10301.0})
    result = _evaluate_fact(_fact(), [_other(other), _excel(excel)])

    assert result.matched_excel_source == "TABEL1_1.xls / I.1"
    assert result.source_conflict == "cross_pdf"


def test_another_pdf_answers_when_the_reports_own_tables_lack_the_period():
    # The July report's Lampiran starts at July; the June figure its narrative quotes lives in
    # the June report. Without the fallback this is "Tidak Cukup Data".
    own = _make_table(data={("Total", 2026, "Jul"): 10420.0})
    other = _make_table(data={("Total", 2026, "Jun"): 10355.1})
    result = _evaluate_fact(_fact(), [_own(own), _other(other)])

    assert result.verdict == "Entailed"
    assert result.matched_excel_source == "M2-Juni-2026.pdf / Hal. 7 · Lampiran 1"
    assert result.source_conflict is None


def test_another_pdf_answers_when_the_own_table_cannot_compute_the_claim():
    # The own table names the period but not the year-ago column a yoy needs.
    own = _make_table(data={("Total", 2026, "Jun"): 10355.1})
    other = _make_table(data={("Total", 2026, "Jun"): 10355.1, ("Total", 2025, "Jun"): 9000.0})
    fact = _fact(operation="yoy_growth", claimed_value=15.1, unit="persen_yoy")
    result = _evaluate_fact(fact, [_own(own), _other(other)])

    assert result.verdict == "Entailed"
    assert result.matched_excel_source.startswith("M2-Juni-2026.pdf")
    assert result.source_conflict is None


def test_other_pdfs_alone_verify_like_any_source():
    other = _make_table(data={("Total", 2026, "Jun"): 10355.1})
    result = _evaluate_fact(_fact(), [_other(other)])

    assert result.verdict == "Entailed"
    assert result.matched_excel_source.startswith("M2-Juni-2026.pdf")


def test_agreeing_pdfs_raise_no_conflict():
    own = _make_table(data={("Total", 2026, "Jun"): 10355.1})
    other = _make_table(data={("Total", 2026, "Jun"): 10355.12})
    result = _evaluate_fact(_fact(), [_own(own), _other(other)])

    assert result.source_conflict is None
    assert len(result.source_values) == 2


# ---------------------------------------------------------------------------
# verify_paired: reference tables join the pool under their own PDF's name
# ---------------------------------------------------------------------------

def _pdf_table(jun="10.355,1"):
    out = _PdfTableOut(
        caption="Lampiran 1. Uang Beredar", unit="(Triliun Rp)",
        header_rows=[["", "2026", "2026"], ["", "Mei", "Jun"]],
        rows=[["Total", "10.100,0", jun]],
    )
    return PdfTable(page_number=7, caption="Lampiran 1. Uang Beredar", unit="Triliun Rp",
                    grid=_assemble_grid(out), verified=True)


def _facts_returning(facts):
    return patch(
        "paired_verifier.extract_structured_facts_async",
        new=Mock(side_effect=lambda *a, **k: asyncio.sleep(0, result=list(facts))),
    )


def test_reference_tables_are_named_after_their_own_pdf():
    with _facts_returning([_fact()]):
        response = asyncio.run(verify_paired(
            narrative_text="[== Halaman 1 ==]\nteks", excel_sources=[], llm=Mock(),
            pdf_filename="M2-Juli-2026.pdf", mode="none",
            reference_tables=[("M2-Juni-2026.pdf", [_pdf_table()])],
        ))

    assert response.mode == "none"
    assert response.reference_pdfs == ["M2-Juni-2026.pdf"]
    assert response.excel_filenames == ["M2-Juni-2026.pdf"]
    assert response.entailed_count == 1
    assert response.results[0].matched_excel_source.startswith("M2-Juni-2026.pdf / Hal. 7")
    # No workbook was asked for, so no hint about which one to upload.
    assert response.table_suggestions == []


def test_internal_plus_reference_reports_a_cross_pdf_conflict():
    with _facts_returning([_fact()]):
        response = asyncio.run(verify_paired(
            narrative_text="[== Halaman 1 ==]\nteks", excel_sources=[], llm=Mock(),
            pdf_filename="M2-Juli-2026.pdf", mode="internal",
            pdf_tables=[_pdf_table("10.355,1")],
            reference_tables=[("M2-Juni-2026.pdf", [_pdf_table("10.301,0")])],
        ))

    assert response.conflict_count == 1
    assert response.results[0].source_conflict == "cross_pdf"
    assert response.results[0].matched_excel_source.startswith("M2-Juli-2026.pdf")


# ---------------------------------------------------------------------------
# Endpoint plumbing
# ---------------------------------------------------------------------------

client = TestClient(main.app)

_JULI = ("pdf_file", ("M2-Juli-2026.pdf", b"%PDF-1.4 juli", "application/pdf"))
_JUNI_REF = ("reference_pdf", ("M2-Juni-2026.pdf", b"%PDF-1.4 juni", "application/pdf"))
_MEI_REF = ("reference_pdf", ("M2-Mei-2026.pdf", b"%PDF-1.4 mei", "application/pdf"))


def _fact_response(**overrides):
    base = dict(pdf_filename="M2-Juli-2026.pdf", excel_filenames=[], excel_sheets=[],
                excel_units=[], total_facts=0, entailed_count=0, refuted_count=0,
                inconclusive_count=0, results=[])
    base.update(overrides)
    return PairedVerificationResponse(**base)


def _typo_response():
    return TypoCheckResponse(total_issues=0, ejaan_count=0, tidak_baku_count=0,
                             grammar_count=0, summary="-", issues=[])


def test_mode_none_needs_another_pdf():
    response = client.post("/api/verify-paired?mode=none", files=[_JULI])
    assert response.status_code == 400
    assert "PDF lain" in response.json()["detail"]


@patch("main.check_typos")
@patch("main.verify_paired")
@patch("main.extract_tables_from_pdf")
@patch("main.extract_narrative_text")
@patch("main.get_vision_llm")
def test_each_reference_pdf_is_read_once_and_passed_under_its_name(
    mock_vision, mock_narrative, mock_tables, mock_verify, mock_typos
):
    mock_vision.return_value = Mock()
    mock_narrative.return_value = "[== Halaman 1 ==]\nteks"
    mock_tables.side_effect = lambda pdf_bytes, *a, **k: [pdf_bytes]
    mock_verify.return_value = _fact_response(mode="none")
    mock_typos.return_value = _typo_response()

    response = client.post("/api/verify-paired?mode=none", files=[_JULI, _JUNI_REF, _MEI_REF])

    assert response.status_code == 200
    kwargs = mock_verify.call_args.kwargs
    # Mode "none" never reads the report's own tables — only the other PDFs'.
    assert kwargs["pdf_tables"] == []
    assert kwargs["reference_tables"] == [
        ("M2-Juni-2026.pdf", [b"%PDF-1.4 juni"]),
        ("M2-Mei-2026.pdf", [b"%PDF-1.4 mei"]),
    ]
    assert mock_tables.call_count == 2


@patch("main.check_typos")
@patch("main.verify_paired")
@patch("main.extract_tables_from_pdf")
@patch("main.extract_narrative_text")
@patch("main.get_vision_llm")
def test_a_pdf_read_in_one_request_is_not_read_again_in_the_next(
    mock_vision, mock_narrative, mock_tables, mock_verify, mock_typos
):
    # The UI verifies the PDFs one request at a time, each sending the others as references —
    # so without a cache every PDF's tables would be transcribed once per PDF in the run.
    mock_vision.return_value = Mock()
    mock_narrative.return_value = "[== Halaman 1 ==]\nteks"
    mock_tables.side_effect = lambda pdf_bytes, *a, **k: [pdf_bytes]
    mock_verify.return_value = _fact_response(mode="internal")
    mock_typos.return_value = _typo_response()

    juni_as_report = ("pdf_file", ("M2-Juni-2026.pdf", b"%PDF-1.4 juni", "application/pdf"))
    juli_as_ref = ("reference_pdf", ("M2-Juli-2026.pdf", b"%PDF-1.4 juli", "application/pdf"))
    client.post("/api/verify-paired?mode=internal", files=[_JULI, _JUNI_REF])
    client.post("/api/verify-paired?mode=internal", files=[juni_as_report, juli_as_ref])

    assert mock_tables.call_count == 2, "two distinct PDFs, each transcribed once"


@patch("main.check_typos")
@patch("main.verify_paired")
@patch("main.extract_tables_from_pdf")
@patch("main.extract_narrative_text")
@patch("main.get_vision_llm")
def test_stream_reports_the_reference_tables_stage(
    mock_vision, mock_narrative, mock_tables, mock_verify, mock_typos
):
    mock_vision.return_value = Mock()
    mock_narrative.return_value = "[== Halaman 1 ==]\nteks"
    mock_tables.return_value = []
    mock_verify.return_value = _fact_response(mode="none")
    mock_typos.return_value = _typo_response()

    response = client.post("/api/verify-paired-stream?mode=none", files=[_JULI, _JUNI_REF])

    events = [json.loads(line) for line in response.text.splitlines() if line.strip()]
    stages = [(e["stage"], e["status"]) for e in events if e["type"] == "stage"]
    assert ("reftables", "done") in stages
    assert events[-1]["type"] == "result"
