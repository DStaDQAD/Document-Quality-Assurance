"""tools/inventory: sentence export helpers and the label store (no LLM, no real BI files)."""

import json

import pytest

from tools.inventory import export
from tools.inventory.server import LABEL_FORMAT, Store, wave_of


def test_numeric_sentence_needs_a_data_number():
    assert export.is_numeric_sentence("Posisi M2 pada Agustus 2026 tercatat sebesar Rp10.448,1 triliun.")
    # only a year, or only a table number: not a data sentence
    assert not export.is_numeric_sentence("Perkembangan M2 pada Agustus 2026 dipengaruhi oleh penyaluran kredit.")
    assert not export.is_numeric_sentence("Perkembangan M2 terutama dipengaruhi oleh penyaluran kredit (Tabel 2).")
    # a table row is not a sentence
    assert not export.is_numeric_sentence("M2 10.448,1 10.320,5 8,2 8,3")


def test_rupiah_amounts_are_numbers_but_metric_names_are_not():
    texts = [n["text"] for n in export.printed_numbers("M2 dan M0 tercatat Rp10.448,1 triliun dan US$5,4 miliar pada T1.")]
    assert texts == ["10.448,1", "5,4"]


def test_printed_numbers_marks_years_references_and_footnotes():
    nums = {n["text"]: n for n in export.printed_numbers(
        "1) Giro tumbuh 9,4% pada Agustus 2026 (Lampiran 4 dan 5).")}
    assert nums["9,4"]["year_like"] is False and nums["9,4"]["reference"] is False
    assert nums["2026"]["year_like"] is True
    assert nums["4"]["reference"] and nums["5"]["reference"]
    assert nums["1"]["reference"]  # footnote marker


def test_split_sentences_keeps_decimals_and_splits_on_full_stops():
    text = "M2 tumbuh 8,2% (yoy). Kredit tumbuh sebesar 13,3%.\nDPK naik 7,1%."
    assert export.split_sentences(text) == [
        "M2 tumbuh 8,2% (yoy).", "Kredit tumbuh sebesar 13,3%.", "DPK naik 7,1%."]


def _sentence(text):
    return {"text": text, "numbers": export.printed_numbers(text), "system_claims": []}


def test_attach_claims_places_quotes_and_copies_onto_repeats():
    a = _sentence("Posisi M2 tercatat Rp10.448,1 triliun atau tumbuh 8,2% (yoy).")
    b = _sentence("Kredit tumbuh 13,3% (yoy).")
    a2 = _sentence("Posisi M2 tercatat Rp10.448,1 triliun atau tumbuh 8,2% (yoy).")
    results = [
        {"operation": "value", "claimed_value": 10448.1, "context_quote": "Posisi M2 tercatat Rp10.448,1 triliun atau tumbuh 8,2% (yoy)."},
        {"operation": "value", "claimed_value": 1.0, "context_quote": "Kalimat yang tidak ada di dokumen."},
    ]
    unplaced = export.attach_claims([a, b, a2], results)
    assert len(a["system_claims"]) == 1 and len(a2["system_claims"]) == 1
    assert b["system_claims"] == []
    assert [u["claimed_value"] for u in unplaced] == [1.0]


def test_mark_repeats_points_an_unclaimed_twin_at_the_claimed_sentence():
    a = _sentence("Posisi M2 tercatat Rp10.448,1 triliun atau tumbuh 8,2% (yoy).")
    a["system_claims"] = [{"claimed_value": 10448.1}]
    twin = _sentence("Posisi M2 tercatat sebesar Rp10.448,1 triliun, atau tumbuh 8,2% (yoy) (Tabel 1).")
    other = _sentence("Kredit tumbuh 13,3% (yoy).")
    for i, s in enumerate((a, twin, other), start=1):
        s["sid"] = f"s{i:03d}"
    export.mark_repeats([a, twin, other])
    assert twin["same_numbers_as"] == "s001"
    assert other["same_numbers_as"] is None and a["same_numbers_as"] is None


@pytest.fixture
def store(tmp_path):
    (tmp_path / "data").mkdir()
    doc = {"case": "ub_agu2026", "doc_id": "ub-sp", "publication": "uang-beredar", "needs_vision": False,
           "sentences": [{"sid": "s001", "text": "M2 tumbuh 8,2%."}]}
    (tmp_path / "data" / "ub_agu2026__ub-sp.json").write_text(json.dumps(doc), encoding="utf-8")
    return Store(tmp_path)


def test_store_saves_reloads_and_deletes_labels(store, tmp_path):
    saved = store.save_label("ub_agu2026__ub-sp", "s001", {"status": "done", "sentence_kind": "not_data", "claims": []})
    assert saved["status"] == "done" and "updated_at" in saved
    on_disk = json.loads((tmp_path / "labels" / "ub_agu2026__ub-sp.json").read_text(encoding="utf-8"))
    assert on_disk["format_version"] == LABEL_FORMAT and on_disk["labels"]["s001"]["sentence_kind"] == "not_data"
    assert store.docs()[0]["done"] == 1 and store.docs()[0]["wave"] == 1
    store.save_label("ub_agu2026__ub-sp", "s001", None)
    assert store.doc("ub_agu2026__ub-sp")["labels"]["labels"] == {}


def test_store_rejects_unknown_documents_sentences_and_path_tricks(store):
    with pytest.raises(KeyError):
        store.save_label("ub_agu2026__ub-sp", "s999", {"status": "done"})
    with pytest.raises(KeyError):
        store.doc("../../etc__passwd")
    with pytest.raises(KeyError):
        store.doc("missing__doc")


def test_waves_follow_the_owner_order():
    assert {wave_of(p) for p in ("uang-beredar", "cadangan-devisa", "sulni")} == {1}
    assert {wave_of(p) for p in ("npi", "skdu")} == {2}
    assert wave_of("ppkom") == 3
