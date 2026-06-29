from unittest.mock import patch

from fastapi.testclient import TestClient

import main
from schemas import VerifyDocumentResponse

client = TestClient(main.app)


@patch("main.verify_document")
@patch("main.extract_text_from_pdf")
def test_verify_document_pdf_endpoint_wires_extraction_into_orchestrator(mock_extract_text, mock_verify_document):
    mock_extract_text.return_value = "Inflasi Q1 2023 sebesar 5.47%."
    mock_verify_document.return_value = VerifyDocumentResponse(
        total_claims=1,
        entailed_count=1,
        refuted_count=0,
        inconclusive_count=0,
        error_count=0,
        summary="1 claim(s) extracted: 1 entailed, 0 refuted, 0 inconclusive, 0 could not be verified due to a pipeline error.",
        results=[],
    )

    response = client.post(
        "/api/verify-document-pdf",
        files={"file": ("doc.pdf", b"%PDF-1.4 fake bytes", "application/pdf")},
    )

    assert response.status_code == 200
    assert response.json()["total_claims"] == 1
    mock_extract_text.assert_called_once_with(b"%PDF-1.4 fake bytes")
    mock_verify_document.assert_called_once_with("Inflasi Q1 2023 sebesar 5.47%.")


@patch("main.extract_text_from_pdf")
def test_verify_document_pdf_endpoint_returns_400_on_unreadable_pdf(mock_extract_text):
    mock_extract_text.side_effect = ValueError("No extractable text found in the PDF.")

    response = client.post(
        "/api/verify-document-pdf",
        files={"file": ("doc.pdf", b"not a real pdf", "application/pdf")},
    )

    assert response.status_code == 400
    assert "No extractable text" in response.json()["detail"]


@patch("main.verify_document")
@patch("main.extract_text_from_pdf")
def test_verify_document_pdf_endpoint_returns_502_when_pipeline_fails(mock_extract_text, mock_verify_document):
    mock_extract_text.return_value = "some text"
    mock_verify_document.side_effect = Exception("LLM exploded")

    response = client.post(
        "/api/verify-document-pdf",
        files={"file": ("doc.pdf", b"%PDF-1.4", "application/pdf")},
    )

    assert response.status_code == 502
