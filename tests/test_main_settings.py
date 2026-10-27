"""/api/settings: the Pengaturan tab's endpoints over team_settings (docs/specs/settings-tab.md)."""
from fastapi.testclient import TestClient

import main
import team_settings

client = TestClient(main.app)


def test_get_settings_lists_all_thirteen_with_aliases_notes_and_visibility():
    body = client.get("/api/settings").json()
    keys = [p["key"] for p in body["publications"]]
    assert keys == list(team_settings.PUBLICATIONS)
    pii = next(p for p in body["publications"] if p["key"] == "pii")
    assert pii["hidden"] is False
    assert any(g.get("row") == "Utang Luar Negeri (ULN)" and "utang luar negeri" in g["phrases"]
               for g in pii["builtin"]["phrases"])
    assert pii["builtin"]["conventions"] and "builtin_aliases" not in pii
    assert pii["team_aliases"] == [] and pii["notes"] == []      # the team's additions only
    assert body["notes_max_words"] == team_settings.NOTES_MAX_WORDS


def test_a_team_phrase_is_edited_and_a_missing_one_is_422():
    alias = client.post("/api/settings/spe/aliases", json={"phrase": "penjualan riil", "row": "INDEKS TOTAL", "by": "Ani"}).json()
    edited = client.put(f"/api/settings/spe/aliases/{alias['id']}",
                        json={"phrase": "Penjualan Riil Total", "row": "INDEKS TOTAL", "by": "Budi"})
    assert edited.status_code == 200 and edited.json()["phrase"] == "penjualan riil total"
    spe = next(p for p in client.get("/api/settings").json()["publications"] if p["key"] == "spe")
    assert [a["phrase"] for a in spe["team_aliases"]] == ["penjualan riil total"]
    missing = client.put(f"/api/settings/spe/aliases/{alias['id'] + 99}",
                         json={"phrase": "x", "row": "INDEKS TOTAL", "by": "Budi"})
    assert missing.status_code == 422 and "tidak ditemukan" in missing.json()["detail"]


def test_hide_add_alias_note_reorder_delete_and_reset_round_trip():
    assert client.put("/api/settings/ppkom/visibility", json={"hidden": True, "by": "Ani"}).status_code == 200
    alias = client.post("/api/settings/pii/aliases", json={"phrase": "Total AFLN", "row": "Aset", "by": "Ani"}).json()
    assert alias["phrase"] == "total afln" and alias["warnings"] == []
    note = client.post("/api/settings/pii/notes", json={"text": "KFLN = sisi Kewajiban.", "by": "Budi"}).json()
    edited = client.put(f"/api/settings/pii/notes/{note['id']}", json={"text": "KFLN = Kewajiban.", "by": "Budi"})
    assert edited.json()["text"] == "KFLN = Kewajiban."
    ids = [n["id"] for n in client.get("/api/settings").json()["publications"][3]["notes"]]
    assert client.put("/api/settings/pii/notes/order", json={"ids": ids[::-1], "by": "Ani"}).status_code == 200
    assert client.delete(f"/api/settings/pii/aliases/{alias['id']}?by=Ani").status_code == 200
    assert client.delete(f"/api/settings/pii/notes/{note['id']}?by=Ani").status_code == 200
    assert client.post("/api/settings/ppkom/reset", json={"by": "Citra"}).status_code == 200

    body = client.get("/api/settings").json()
    assert not any(p["hidden"] for p in body["publications"])
    log = client.get("/api/settings/changes?publication=pii").json()["changes"]
    assert [c["action"] for c in log][:2] == ["hapus", "hapus"] and log[0]["by"] == "Ani"


def test_an_unknown_publication_is_404_and_bad_values_are_422_in_indonesian():
    assert client.put("/api/settings/koran/visibility", json={"hidden": True, "by": "Ani"}).status_code == 404
    empty = client.post("/api/settings/pii/aliases", json={"phrase": "", "row": "Aset", "by": "Ani"})
    assert empty.status_code == 422 and "wajib diisi" in empty.json()["detail"]
    nameless = client.put("/api/settings/pii/visibility", json={"hidden": True, "by": ""})
    assert nameless.status_code == 422 and "nama" in nameless.json()["detail"]
    long_note = client.post("/api/settings/pii/notes", json={"text": "kata " * 700, "by": "Ani"})
    assert long_note.status_code == 422


def test_a_database_failure_is_a_503_not_a_crash(monkeypatch):
    def broken(*_a, **_k):
        raise RuntimeError("database down")
    monkeypatch.setattr(team_settings, "hidden_publications", broken)
    response = client.get("/api/settings")
    assert response.status_code == 503 and "Pengaturan" in response.json()["detail"]


def test_upload_hints_are_listed_and_edited():
    body = client.get("/api/settings").json()
    cadev = next(p for p in body["publications"] if p["key"] == "cadangan-devisa")
    assert "Tabel Cadev ORA" in cadev["upload_hints"]["excel"]
    saved = client.put("/api/settings/cadangan-devisa/upload-hints",
                       json={"press": "SP Cadev.docx", "report": "", "excel": "ORA.xlsx", "by": "Ani"})
    assert saved.status_code == 200 and saved.json()["excel"] == "ORA.xlsx"
    body = client.get("/api/settings").json()
    cadev = next(p for p in body["publications"] if p["key"] == "cadangan-devisa")
    assert cadev["upload_hints"] == {"press": "SP Cadev.docx", "report": "", "excel": "ORA.xlsx"}
    assert client.put("/api/settings/cadangan-devisa/upload-hints",
                      json={"press": "x", "report": "", "excel": "", "by": ""}).status_code == 422
