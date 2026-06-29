from unittest.mock import Mock, patch

from fastapi.testclient import TestClient

import main
from excel_ingestion import IngestSummary

client = TestClient(main.app)


@patch("main.ingest_bytes")
@patch("main.get_llm")
def test_upload_excel_source_endpoint_wires_bytes_into_ingestion(mock_get_llm, mock_ingest_bytes):
    mock_get_llm.return_value = Mock()
    mock_ingest_bytes.return_value = IngestSummary(
        n_sheets=1, n_facts=16, auto_aggregate=1, auto_not_aggregate=7, llm_escalated=0, defaulted=0
    )

    response = client.post(
        "/api/upload-excel-source",
        files={"file": ("penjualan_2024.xlsx", b"fake xlsx bytes", "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet")},
    )

    assert response.status_code == 200
    body = response.json()
    assert body == {
        "filename": "penjualan_2024.xlsx",
        "n_sheets": 1,
        "n_facts": 16,
        "auto_aggregate": 1,
        "auto_not_aggregate": 7,
        "llm_escalated": 0,
        "defaulted": 0,
    }
    mock_ingest_bytes.assert_called_once_with(b"fake xlsx bytes", "penjualan_2024.xlsx", llm=mock_get_llm.return_value)


@patch("main.ingest_bytes")
def test_upload_excel_source_endpoint_returns_400_on_unreadable_excel(mock_ingest_bytes):
    mock_ingest_bytes.side_effect = Exception("not a valid xlsx")

    response = client.post(
        "/api/upload-excel-source",
        files={"file": ("broken.xlsx", b"not really an excel file", "application/octet-stream")},
    )

    assert response.status_code == 400
    assert "not a valid xlsx" in response.json()["detail"]
