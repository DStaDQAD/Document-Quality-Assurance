"""Cek Terjemahan endpoints: one ID/EN document pair per check, stored in its own Riwayat table."""
from unittest.mock import AsyncMock, patch

from fastapi.testclient import TestClient

import check_history
import main
from schemas import TranslationCheckResponse
from word_fixtures import docx_bytes, para

client = TestClient(main.app)

DOCX = "application/vnd.openxmlformats-officedocument.wordprocessingml.document"
ID_DOCX = docx_bytes(para("Posisi M2 tercatat sebesar Rp10.448,1 triliun atau tumbuh 8,2% (yoy)."))
EN_DOCX = docx_bytes(para("The M2 position reached Rp10,448.1 trillion, a growth of 8.2% (yoy)."))


def _files(id_name="UB_ID.docx", en_name="UB_EN.docx", id_bytes=ID_DOCX, en_bytes=EN_DOCX):
    return [("id_file", (id_name, id_bytes, DOCX)), ("en_file", (en_name, en_bytes, DOCX))]


def _report(**overrides):
    base = dict(document="laporan", id_filename="UB_ID.docx", en_filename="UB_EN.docx", n_number=1, n_meaning=2)
    base.update(overrides)
    return TranslationCheckResponse(**base)


@patch("main.get_llm")
@patch("main.check_translation", new_callable=AsyncMock)
def test_check_translation_returns_the_report_and_saves_it(mock_check, mock_llm):
    mock_check.return_value = _report()

    response = client.post("/api/check-translation", params={"document": "laporan", "checker_name": " Ivan ",
                                                              "publication": "uang-beredar"}, files=_files())

    assert response.status_code == 200
    body = response.json()
    assert body["history_id"] and body["n_meaning"] == 2
    id_text, en_text = mock_check.call_args.args[:2]
    assert "Rp10.448,1" in id_text and "Rp10,448.1" in en_text
    assert mock_check.call_args.kwargs["document"] == "laporan"
    items, _ = check_history.list_translation_checks()
    assert [(i.id, i.checker_name, i.publication, i.n_number) for i in items] == [
        (body["history_id"], "Ivan", "uang-beredar", 1)]


@patch("main.get_llm")
@patch("main.check_translation", new_callable=AsyncMock)
def test_a_history_failure_still_returns_the_report(mock_check, mock_llm):
    mock_check.return_value = _report()
    with patch("main.check_history.save_translation_check", side_effect=RuntimeError("db down")):
        response = client.post("/api/check-translation", files=_files())

    assert response.status_code == 200 and response.json()["history_id"] is None


def test_unknown_document_kind_is_422():
    assert client.post("/api/check-translation", params={"document": "tabel"}, files=_files()).status_code == 422


def test_a_legacy_doc_is_refused_with_the_word_message():
    response = client.post("/api/check-translation", files=_files(en_name="UB_EN.doc", en_bytes=b"\xd0\xcf\x11\xe0"))
    assert response.status_code == 400 and ".docx" in response.json()["detail"]


@patch("main.get_llm")
@patch("main.check_translation", new_callable=AsyncMock)
def test_translation_history_list_and_open(mock_check, mock_llm):
    mock_check.return_value = _report()
    history_id = client.post("/api/check-translation", files=_files()).json()["history_id"]

    listed = client.get("/api/translation-history").json()
    assert [i["id"] for i in listed["items"]] == [history_id] and listed["has_more"] is False
    entry = client.get(f"/api/translation-history/{history_id}").json()
    assert entry["summary"]["en_filename"] == "UB_EN.docx" and entry["result"]["history_id"] == history_id
    assert client.get("/api/translation-history/nope").status_code == 404


def test_endpoints_sit_behind_the_login_gate(monkeypatch):
    monkeypatch.setenv("APP_USERNAME", "user@example.com")
    monkeypatch.setenv("APP_PASSWORD", "s3cret*#")
    gated = TestClient(main.app)

    assert gated.post("/api/check-translation", files=_files()).status_code == 401
    assert gated.get("/api/translation-history").status_code == 401
