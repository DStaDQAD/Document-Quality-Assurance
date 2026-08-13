"""Tests for the `mode` / `run_typo_check` params on the paired endpoints.

What these pin down is the plumbing contract, not the pipeline: which uploads each mode
accepts, that the table-transcription pass runs only when asked, that its stage reaches the
stream, and that the typo pass can be switched off.
"""

import json
from unittest.mock import Mock, patch

from fastapi.testclient import TestClient

import main
from schemas import PairedVerificationResponse, TypoCheckResponse

client = TestClient(main.app)

_PDF_ONLY = [("pdf_file", ("report.pdf", b"%PDF-1.4 fake", "application/pdf"))]
_PDF_AND_EXCEL = _PDF_ONLY + [
    ("excel_file", ("TABEL1_1.xls", b"xls-bytes", "application/vnd.ms-excel"))
]


def _fact_response(**overrides):
    base = dict(
        pdf_filename="report.pdf",
        excel_filenames=[], excel_sheets=[], excel_units=[],
        total_facts=0, entailed_count=0, refuted_count=0, inconclusive_count=0,
        results=[],
    )
    base.update(overrides)
    return PairedVerificationResponse(**base)


def _typo_response():
    return TypoCheckResponse(
        total_issues=0, ejaan_count=0, tidak_baku_count=0, grammar_count=0,
        summary="Tidak ditemukan isu.", issues=[],
    )


def _read_events(response):
    return [json.loads(line) for line in response.text.splitlines() if line.strip()]


# ---------------------------------------------------------------------------
# Mode validation — a 400 while the status is still negotiable
# ---------------------------------------------------------------------------

def test_default_mode_still_requires_an_excel_file():
    response = client.post("/api/verify-paired", files=_PDF_ONLY)
    assert response.status_code == 400
    assert "Excel" in response.json()["detail"]


def test_both_mode_requires_an_excel_file():
    response = client.post("/api/verify-paired?mode=both", files=_PDF_ONLY)
    assert response.status_code == 400


def test_unknown_mode_is_rejected():
    response = client.post("/api/verify-paired?mode=telepathy", files=_PDF_AND_EXCEL)
    assert response.status_code == 400
    assert "telepathy" in response.json()["detail"]


def test_stream_endpoint_rejects_a_bad_mode_with_a_real_http_status():
    # The stream cannot report a 400 once its 200 has been flushed, so validation must happen
    # before the generator starts.
    response = client.post("/api/verify-paired-stream?mode=internal&mode=nope", files=_PDF_ONLY)
    assert response.status_code == 400


# ---------------------------------------------------------------------------
# Internal mode
# ---------------------------------------------------------------------------

@patch("main.check_typos")
@patch("main.verify_paired")
@patch("main.extract_tables_from_pdf")
@patch("main.extract_narrative_text")
@patch("main.get_vision_llm")
def test_internal_mode_needs_no_excel_and_transcribes_the_pdf_tables(
    mock_vision, mock_narrative, mock_tables, mock_verify, mock_typos
):
    mock_vision.return_value = Mock()
    mock_narrative.return_value = "[== Halaman 1 ==]\nM2 tumbuh 9,7% (yoy)."
    mock_tables.return_value = [Mock(), Mock()]
    mock_verify.return_value = _fact_response(mode="internal", total_facts=1, entailed_count=1)
    mock_typos.return_value = _typo_response()

    response = client.post("/api/verify-paired?mode=internal", files=_PDF_ONLY)

    assert response.status_code == 200
    mock_tables.assert_called_once()
    # The transcribed tables reach verify_paired, and so does the mode.
    kwargs = mock_verify.call_args.kwargs
    assert len(kwargs["pdf_tables"]) == 2
    assert kwargs["mode"] == "internal"
    assert kwargs["excel_sources"] == []


@patch("main.check_typos")
@patch("main.verify_paired")
@patch("main.extract_tables_from_pdf")
@patch("main.extract_narrative_text")
@patch("main.get_vision_llm")
def test_excel_mode_never_transcribes_pdf_tables(
    mock_vision, mock_narrative, mock_tables, mock_verify, mock_typos
):
    mock_vision.return_value = Mock()
    mock_narrative.return_value = "[== Halaman 1 ==]\nteks"
    mock_verify.return_value = _fact_response()
    mock_typos.return_value = _typo_response()

    response = client.post("/api/verify-paired", files=_PDF_AND_EXCEL)

    assert response.status_code == 200
    mock_tables.assert_not_called()
    assert mock_verify.call_args.kwargs["pdf_tables"] == []


@patch("main.check_typos")
@patch("main.verify_paired")
@patch("main.extract_tables_from_pdf")
@patch("main.extract_narrative_text")
@patch("main.get_vision_llm")
def test_internal_mode_runs_on_the_text_layer_alone_without_a_vision_model(
    mock_vision, mock_narrative, mock_tables, mock_verify, mock_typos
):
    # A digital report is read by the native table reader, which needs no model at all — so a
    # missing key is not a reason to refuse the run.
    mock_vision.side_effect = RuntimeError("no key configured")
    mock_narrative.return_value = "[== Halaman 1 ==]\nteks"
    mock_tables.return_value = [Mock()]
    mock_verify.return_value = _fact_response(mode="internal")
    mock_typos.return_value = _typo_response()

    response = client.post("/api/verify-paired?mode=internal", files=_PDF_ONLY)

    assert response.status_code == 200
    assert mock_tables.call_args.args[1] is None       # no vision model was passed
    assert len(mock_verify.call_args.kwargs["pdf_tables"]) == 1


@patch("main.check_typos")
@patch("main.verify_paired")
@patch("main.extract_tables_from_pdf")
@patch("main.extract_narrative_text")
@patch("main.get_vision_llm")
def test_internal_mode_fails_clearly_when_nothing_is_readable_without_vision(
    mock_vision, mock_narrative, mock_tables, mock_verify, mock_typos
):
    # A scanned report has no text layer, so without a vision model there is no reference at
    # all — that is the one case where the missing key is worth reporting.
    mock_vision.side_effect = RuntimeError("no key configured")
    mock_narrative.return_value = "[== Halaman 1 ==]\nteks"
    mock_tables.return_value = []

    response = client.post("/api/verify-paired?mode=internal", files=_PDF_ONLY)

    assert response.status_code == 400
    assert "GOOGLE_API_KEY" in response.json()["detail"]
    mock_verify.assert_not_called()


@patch("main.check_typos")
@patch("main.verify_paired")
@patch("main.extract_tables_from_pdf")
@patch("main.extract_narrative_text")
@patch("main.get_vision_llm")
def test_stream_reports_the_tables_stage_in_internal_mode(
    mock_vision, mock_narrative, mock_tables, mock_verify, mock_typos
):
    mock_vision.return_value = Mock()
    mock_narrative.return_value = "[== Halaman 1 ==]\nteks"
    mock_tables.return_value = []
    mock_verify.return_value = _fact_response(mode="internal")
    mock_typos.return_value = _typo_response()

    response = client.post("/api/verify-paired-stream?mode=internal", files=_PDF_ONLY)

    assert response.status_code == 200
    events = _read_events(response)
    stages = [e["stage"] for e in events if e["type"] == "stage"]
    assert "tables" in stages
    assert events[-1]["type"] == "result"


# ---------------------------------------------------------------------------
# run_typo_check
# ---------------------------------------------------------------------------

@patch("main.check_typos")
@patch("main.verify_paired")
@patch("main.extract_narrative_text")
@patch("main.get_vision_llm")
def test_typo_check_can_be_switched_off(
    mock_vision, mock_narrative, mock_verify, mock_typos
):
    mock_vision.side_effect = RuntimeError("no key configured")
    mock_narrative.return_value = "[== Halaman 1 ==]\nteks"
    mock_verify.return_value = _fact_response()

    response = client.post(
        "/api/verify-paired?run_typo_check=false", files=_PDF_AND_EXCEL
    )

    assert response.status_code == 200
    mock_typos.assert_not_called()
    assert response.json()["typo_check"] is None


@patch("main.check_typos")
@patch("main.verify_paired")
@patch("main.extract_narrative_text")
@patch("main.get_vision_llm")
def test_typo_check_runs_by_default(mock_vision, mock_narrative, mock_verify, mock_typos):
    mock_vision.side_effect = RuntimeError("no key configured")
    mock_narrative.return_value = "[== Halaman 1 ==]\nteks"
    mock_verify.return_value = _fact_response()
    mock_typos.return_value = _typo_response()

    response = client.post("/api/verify-paired", files=_PDF_AND_EXCEL)

    assert response.status_code == 200
    mock_typos.assert_called_once()
    assert response.json()["typo_check"] is not None
