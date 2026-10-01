"""The paired endpoints store every finished check in the history, and never fail over it."""

import json
from unittest.mock import patch

from fastapi.testclient import TestClient

import check_history
import main
from schemas import PairedVerificationResponse

client = TestClient(main.app)

_PDF = ("pdf_file", ("report.pdf", b"%PDF-1.4 fake", "application/pdf"))
_XLS = ("excel_file", ("TABEL1_1.xls", b"xls-bytes", "application/vnd.ms-excel"))


def _fact_response(**overrides):
    base = dict(
        pdf_filename="report.pdf",
        excel_filenames=["TABEL1_1.xls"],
        excel_sheets=["I.1"],
        excel_units=["triliun Rp"],
        total_facts=3,
        entailed_count=2,
        refuted_count=1,
        inconclusive_count=0,
        results=[],
    )
    base.update(overrides)
    return PairedVerificationResponse(**base)


def _mock_pipeline(mock_get_vision_llm, mock_extract, mock_verify):
    mock_get_vision_llm.side_effect = RuntimeError("no key configured")
    mock_extract.return_value = "[== Halaman 1 ==]\nInflasi tumbuh 9,7% (yoy)."
    mock_verify.return_value = _fact_response()


@patch("main.verify_paired")
@patch("main.extract_narrative_text")
@patch("main.get_vision_llm")
def test_verify_paired_saves_the_check_and_returns_its_id(mock_vision, mock_extract, mock_verify):
    _mock_pipeline(mock_vision, mock_extract, mock_verify)

    response = client.post(
        "/api/verify-paired",
        params={"run_typo_check": "false", "checker_name": "  Ivan  "},
        files=[_PDF, _XLS],
    )

    assert response.status_code == 200
    history_id = response.json()["history_id"]
    assert history_id
    items, _ = check_history.list_checks()
    assert len(items) == 1
    saved = items[0]
    assert saved.id == history_id
    assert saved.filename == "report.pdf"
    assert saved.file_kind == "pdf"
    assert saved.checker_name == "Ivan"
    assert saved.mode == "excel"
    assert saved.excel_files == ["TABEL1_1.xls"]
    assert (saved.n_facts, saved.n_match, saved.n_mismatch, saved.n_unverified) == (3, 2, 1, 0)
    assert saved.duration_s >= 0
    _, result = check_history.get_check(history_id)
    assert result["total_facts"] == 3
    assert result["history_id"] == history_id


@patch("main.verify_paired")
@patch("main.extract_narrative_text")
@patch("main.get_vision_llm")
def test_stream_saves_the_check_too(mock_vision, mock_extract, mock_verify):
    _mock_pipeline(mock_vision, mock_extract, mock_verify)

    response = client.post(
        "/api/verify-paired-stream", params={"run_typo_check": "false"}, files=[_PDF, _XLS]
    )

    lines = [json.loads(line) for line in response.text.splitlines() if line.strip()]
    result = next(line["data"] for line in lines if line["type"] == "result")
    items, _ = check_history.list_checks()
    assert [i.id for i in items] == [result["history_id"]]
    assert items[0].checker_name is None


@patch("main.verify_paired")
@patch("main.extract_narrative_text")
@patch("main.get_vision_llm")
def test_one_excel_file_used_for_two_sheets_is_listed_once(mock_vision, mock_extract, mock_verify):
    _mock_pipeline(mock_vision, mock_extract, mock_verify)

    client.post(
        "/api/verify-paired",
        params={"run_typo_check": "false", "sheet_names": "I.1,II.1"},
        files=[_PDF, _XLS, _XLS],
    )

    assert check_history.list_checks()[0][0].excel_files == ["TABEL1_1.xls"]


@patch("main.check_history.save_check", side_effect=RuntimeError("database down"))
@patch("main.verify_paired")
@patch("main.extract_narrative_text")
@patch("main.get_vision_llm")
def test_a_failed_save_still_returns_the_result(mock_vision, mock_extract, mock_verify, _save, caplog):
    _mock_pipeline(mock_vision, mock_extract, mock_verify)

    response = client.post(
        "/api/verify-paired", params={"run_typo_check": "false"}, files=[_PDF, _XLS]
    )

    assert response.status_code == 200
    assert response.json()["total_facts"] == 3
    assert response.json()["history_id"] is None
    assert "History save failed" in caplog.text


@patch("main.verify_paired")
@patch("main.extract_narrative_text")
@patch("main.get_vision_llm")
def test_a_failed_check_is_not_saved(mock_vision, mock_extract, mock_verify):
    _mock_pipeline(mock_vision, mock_extract, mock_verify)
    mock_verify.side_effect = Exception("boom")

    response = client.post(
        "/api/verify-paired", params={"run_typo_check": "false"}, files=[_PDF, _XLS]
    )

    assert response.status_code == 400
    assert check_history.list_checks() == ([], False)
