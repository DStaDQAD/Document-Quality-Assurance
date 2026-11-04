"""The paired endpoints store every finished check in the history, and never fail over it."""

import json
from unittest.mock import patch

import pytest
from fastapi.testclient import TestClient

import check_history
import main
from schemas import CheckSummary, PairedVerificationResponse

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
    items, _, _ = check_history.list_checks()
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
    items, _, _ = check_history.list_checks()
    assert [i.id for i in items] == [result["history_id"]]
    assert items[0].checker_name is None
    assert items[0].publication is None


@patch("main.verify_paired")
@patch("main.extract_narrative_text")
@patch("main.get_vision_llm")
def test_both_endpoints_store_the_publication(mock_vision, mock_extract, mock_verify):
    _mock_pipeline(mock_vision, mock_extract, mock_verify)

    client.post("/api/verify-paired",
                params={"run_typo_check": "false", "publication": "uang-beredar"},
                files=[_PDF, _XLS])
    client.post("/api/verify-paired-stream",
                params={"run_typo_check": "false", "publication": "shpr"},
                files=[_PDF, _XLS])

    items, _, _ = check_history.list_checks()
    assert sorted(i.publication for i in items) == ["shpr", "uang-beredar"]


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



@patch("main.usage_totals", side_effect=ValueError("unexpected usage metadata"))
@patch("main.verify_paired")
@patch("main.extract_narrative_text")
@patch("main.get_vision_llm")
def test_a_summary_that_cannot_be_built_still_returns_the_result(
    mock_vision, mock_extract, mock_verify, _usage, caplog
):
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
    assert check_history.list_checks() == ([], False, 0)


def _seed(filename="laporan.pdf", **overrides):
    summary = CheckSummary(filename=filename, file_kind="pdf", mode="internal", **overrides)
    return check_history.save_check(summary, {"pdf_filename": filename, "total_facts": 1})


def test_history_list_returns_summaries_without_results():
    check_id = _seed("SK-Agustus-2026.pdf", checker_name="Ivan", n_match=4)

    body = client.get("/api/history").json()

    assert body["has_more"] is False
    assert len(body["items"]) == 1
    item = body["items"][0]
    assert item["id"] == check_id
    assert item["filename"] == "SK-Agustus-2026.pdf"
    assert item["checker_name"] == "Ivan"
    assert item["n_match"] == 4
    assert item["created_at"].endswith(("Z", "+00:00"))
    assert "result" not in item and "result_gz" not in item


def test_history_list_searches_and_pages():
    for i in range(3):
        _seed(f"M2_Juli_{i}.pdf")
    _seed("lain.pdf")

    page = client.get("/api/history", params={"q": "m2_juli", "limit": 2}).json()
    rest = client.get("/api/history", params={"q": "m2_juli", "limit": 2, "offset": 2}).json()

    assert len(page["items"]) == 2 and page["has_more"] is True
    assert len(rest["items"]) == 1 and rest["has_more"] is False


@pytest.mark.parametrize("asked, used", [(0, 1), (-5, 1), (1000, 100), (20, 20)])
def test_history_list_clamps_limit(asked, used):
    with patch("main.check_history.list_checks", return_value=([], False, 0)) as mock_list:
        client.get("/api/history", params={"limit": asked, "offset": -3})
    assert mock_list.call_args.args[:2] == (used, 0)


def test_history_list_passes_the_filters_and_the_total():
    _seed("skdu.docx", publication="skdu")
    _seed("ub.docx", publication="uang-beredar")

    body = client.get("/api/history", params={"publication": "skdu", "date_from": "2026-01-01"}).json()
    assert [i["filename"] for i in body["items"]] == ["skdu.docx"] and body["total"] == 1
    # An unknown publication filters nothing; a malformed date is refused.
    assert client.get("/api/history", params={"publication": "bukan"}).json()["total"] == 2
    assert client.get("/api/history", params={"date_to": "kemarin"}).status_code == 422


def test_history_detail_returns_summary_and_result():
    check_id = _seed("SK-Agustus-2026.pdf", checker_name="Ivan")

    body = client.get(f"/api/history/{check_id}").json()

    assert body["summary"]["id"] == check_id
    assert body["summary"]["checker_name"] == "Ivan"
    assert body["result"] == {
        "pdf_filename": "SK-Agustus-2026.pdf", "total_facts": 1, "history_id": check_id,
    }


def test_history_detail_unknown_id_is_404():
    response = client.get("/api/history/not-a-real-id")
    assert response.status_code == 404
    assert "history" in response.json()["detail"].lower()


def test_history_endpoints_answer_503_when_the_database_is_down():
    with patch("main.check_history.list_checks", side_effect=RuntimeError("down")):
        assert client.get("/api/history").status_code == 503
    with patch("main.check_history.get_check", side_effect=RuntimeError("down")):
        assert client.get("/api/history/abc").status_code == 503


def test_history_endpoints_need_a_session_when_login_is_on(monkeypatch):
    monkeypatch.setenv("APP_USERNAME", "user@example.com")
    monkeypatch.setenv("APP_PASSWORD", "secret")
    fresh = TestClient(main.app)
    assert fresh.get("/api/history").status_code == 401
    assert fresh.get("/api/history/abc").status_code == 401


def test_ai_review_is_off_by_default(monkeypatch):
    monkeypatch.delenv("AI_REVIEW", raising=False)
    assert main._ai_review_enabled() is False


@pytest.mark.parametrize("value", ["1", "on", "ON", "true"])
def test_ai_review_turns_on_from_the_environment(monkeypatch, value):
    monkeypatch.setenv("AI_REVIEW", value)
    assert main._ai_review_enabled() is True


def test_new_refuted_policy_defaults_to_note_and_rejects_typos(monkeypatch):
    monkeypatch.delenv("AI_REVIEW_NEW_REFUTED", raising=False)
    assert main._ai_review_new_refuted() == "note"
    monkeypatch.setenv("AI_REVIEW_NEW_REFUTED", "verdict")
    assert main._ai_review_new_refuted() == "verdict"
    monkeypatch.setenv("AI_REVIEW_NEW_REFUTED", "verdik")
    assert main._ai_review_new_refuted() == "note"


@patch("main.verify_paired")
@patch("main.extract_narrative_text")
@patch("main.get_vision_llm")
def test_the_pipeline_passes_no_review_model_when_ai_review_is_off(mock_vision, mock_extract, mock_verify, monkeypatch):
    monkeypatch.delenv("AI_REVIEW", raising=False)
    _mock_pipeline(mock_vision, mock_extract, mock_verify)
    client.post("/api/verify-paired", params={"run_typo_check": "false"}, files=[_PDF, _XLS])
    assert mock_verify.call_args.kwargs["review_llm"] is None


@patch("main.get_review_llm")
@patch("main.verify_paired")
@patch("main.extract_narrative_text")
@patch("main.get_vision_llm")
def test_the_pipeline_passes_the_review_model_when_ai_review_is_on(
    mock_vision, mock_extract, mock_verify, mock_review_llm, monkeypatch
):
    monkeypatch.setenv("AI_REVIEW", "1")
    monkeypatch.setenv("AI_REVIEW_NEW_REFUTED", "verdict")
    _mock_pipeline(mock_vision, mock_extract, mock_verify)
    client.post("/api/verify-paired", params={"run_typo_check": "false"}, files=[_PDF, _XLS])
    assert mock_verify.call_args.kwargs["review_llm"] is mock_review_llm.return_value
    assert mock_verify.call_args.kwargs["review_new_refuted"] == "verdict"


def test_history_search_takes_column_filters_and_a_sort():
    _seed("b.docx", publication="skdu")
    _seed("a.docx", publication="sk")
    _seed("c.docx")

    body = client.post("/api/history/search", json={
        "columns": {"publication": {"values": ["sk", ""]}}, "sort": "filename", "descending": False}).json()
    assert [i["filename"] for i in body["items"]] == ["a.docx", "c.docx"] and body["total"] == 2
    assert client.post("/api/history/search", json={"sort": "id"}).status_code == 422
    assert client.post("/api/history/search", json={"columns": {"hasil": {"values": []}}}).status_code == 422
    assert client.post("/api/history/search", json={"limit": 500}).status_code == 422


def test_history_facets_lists_a_columns_values_with_counts():
    _seed("a.docx", publication="sk")
    _seed("b.docx", publication="sk")
    _seed("c.docx")

    body = client.post("/api/history/facets", json={"column": "publication"}).json()
    assert body == {"values": [{"value": "", "count": 1}, {"value": "sk", "count": 2}]}
    assert client.post("/api/history/facets", json={}).status_code == 422


def test_translation_history_search_and_facets():
    check_history.save_translation_check(
        main.TranslationCheckSummary(document="laporan", id_filename="ID.docx", en_filename="EN.docx"), {"pairs": []})

    body = client.post("/api/translation-history/search", json={}).json()
    assert [i["en_filename"] for i in body["items"]] == ["EN.docx"]
    facets = client.post("/api/translation-history/facets", json={"column": "mode"}).json()
    assert facets == {"values": [{"value": "laporan", "count": 1}]}


def test_history_search_and_facets_answer_503_when_the_database_is_down():
    with patch("main.check_history.search_history", side_effect=RuntimeError("down")):
        assert client.post("/api/history/search", json={}).status_code == 503
        assert client.post("/api/translation-history/search", json={}).status_code == 503
    with patch("main.check_history.history_facet", side_effect=RuntimeError("down")):
        assert client.post("/api/history/facets", json={"column": "day"}).status_code == 503
