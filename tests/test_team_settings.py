"""Pengaturan: the team's shared settings per publication (docs/specs/settings-tab.md).

Hide/show, team aliases on top of the parsers' built-in ones, notes for the Tinjauan AI seeded
once from the YAML notes, a change log, and a reset. Stored next to Riwayat (conftest points it
at a temporary SQLite file)."""
import pytest

import team_settings
from team_settings import SettingsError, UnknownPublication


def test_every_publication_starts_visible_and_can_be_hidden_and_shown():
    assert team_settings.hidden_publications() == set()
    team_settings.set_hidden("ppkom", True, by="Ani")
    assert team_settings.hidden_publications() == {"ppkom"}
    team_settings.set_hidden("ppkom", False, by="Ani")
    assert team_settings.hidden_publications() == set()


def test_an_unknown_publication_is_refused():
    with pytest.raises(UnknownPublication):
        team_settings.set_hidden("koran", True, by="Ani")
    with pytest.raises(UnknownPublication):
        team_settings.add_alias("koran", "x", "y", by="Ani")


def test_built_in_aliases_are_listed_from_the_parsers():
    builtin = dict(team_settings.builtin_aliases("pii"))
    assert builtin["afln indonesia"] == "Aset"
    assert ("ipr", "INDEKS TOTAL") in team_settings.builtin_aliases("spe")
    # SKDU keeps its aliases per sheet; they are listed too.
    assert any(phrase == "akses kredit" for phrase, _row in team_settings.builtin_aliases("skdu"))


def test_a_team_alias_is_added_normalised_and_removed():
    added = team_settings.add_alias("pii", "  KFLN   Jangka Panjang ", "Kewajiban", by="Ani")
    assert added["phrase"] == "kfln jangka panjang" and added["row"] == "Kewajiban"
    assert team_settings.team_aliases("pii") == {"kfln jangka panjang": "Kewajiban"}
    team_settings.delete_alias("pii", added["id"], by="Budi")
    assert team_settings.team_aliases("pii") == {}


def test_a_team_alias_over_a_built_in_one_says_so():
    added = team_settings.add_alias("pii", "AFLN Indonesia", "Aset > Investasi Langsung", by="Ani")
    assert "bawaan" in " ".join(added["warnings"])


def test_a_row_never_seen_is_allowed_with_a_warning():
    team_settings.record_known_rows("pii", ["Aset", "Kewajiban"])
    seen = team_settings.add_alias("pii", "afln", "Aset", by="Ani")
    unseen = team_settings.add_alias("pii", "kfln total", "Kewajiban Total", by="Ani")
    assert seen["warnings"] == []
    assert "belum pernah terlihat" in " ".join(unseen["warnings"])
    assert team_settings.known_rows("pii") == ["Aset", "Kewajiban"]


@pytest.mark.parametrize("phrase, row", [("", "Aset"), ("afln", ""), ("   ", "Aset")])
def test_an_empty_alias_is_refused(phrase, row):
    with pytest.raises(SettingsError):
        team_settings.add_alias("pii", phrase, row, by="Ani")


def test_the_editor_name_is_required():
    with pytest.raises(SettingsError):
        team_settings.set_hidden("pii", True, by="  ")


def test_notes_are_seeded_once_from_the_yaml_without_its_phrases():
    notes = team_settings.notes("spe")
    texts = [n["text"] for n in notes]
    assert notes and all(n["seeded"] for n in notes)
    assert any("turun X%" in t for t in texts)                      # a convention
    assert not any(t.startswith('"IPR", "penjualan eceran"') for t in texts)   # phrases stay out
    for note in notes:
        team_settings.delete_note("spe", note["id"], by="Ani")
    # Removing every note does not bring the seed back.
    assert team_settings.notes("spe") == []


def test_notes_are_added_edited_reordered_and_removed():
    for note in team_settings.notes("cadangan-devisa"):
        team_settings.delete_note("cadangan-devisa", note["id"], by="Ani")
    first = team_settings.add_note("cadangan-devisa", '"secara tahunan" → tabel %yoy', by="Ani")
    second = team_settings.add_note("cadangan-devisa", "Posisi akhir bulan.", by="Budi")
    team_settings.update_note("cadangan-devisa", first["id"], '"secara tahunan" → yoy', by="Budi")
    team_settings.reorder_notes("cadangan-devisa", [second["id"], first["id"]], by="Ani")
    assert [n["text"] for n in team_settings.notes("cadangan-devisa")] == [
        "Posisi akhir bulan.", '"secara tahunan" → yoy']
    team_settings.delete_note("cadangan-devisa", second["id"], by="Ani")
    assert [n["text"] for n in team_settings.notes("cadangan-devisa")] == ['"secara tahunan" → yoy']


def test_notes_over_the_word_limit_are_refused():
    with pytest.raises(SettingsError):
        team_settings.add_note("pmi", "kata " * (team_settings.NOTES_MAX_WORDS + 1), by="Ani")


def test_risky_note_wording_is_saved_with_a_warning():
    added = team_settings.add_note("pmi", "Rasio terhadap PDB tidak ada di tabel.", by="Ani")
    assert added["warnings"] and "tidak ada di" in added["warnings"][0]


def test_reset_removes_team_aliases_restores_the_seed_and_shows_the_publication():
    seeded = len(team_settings.notes("npi"))
    team_settings.add_alias("npi", "wisman total", "Ke dalam negeri", by="Ani")
    team_settings.add_note("npi", "catatan tim", by="Ani")
    team_settings.set_hidden("npi", True, by="Ani")
    team_settings.reset("npi", by="Budi")
    assert team_settings.team_aliases("npi") == {}
    assert len(team_settings.notes("npi")) == seeded
    assert "npi" not in team_settings.hidden_publications()


def test_every_change_is_logged_newest_first():
    team_settings.set_hidden("sk", True, by="Ani")
    alias = team_settings.add_alias("sk", "ikk nasional", "Indeks Keyakinan Konsumen (IKK)", by="Budi")
    team_settings.delete_alias("sk", alias["id"], by="Citra")
    log = team_settings.changes("sk")
    assert [(c["by"], c["kind"], c["action"]) for c in log] == [
        ("Citra", "alias", "hapus"), ("Budi", "alias", "tambah"), ("Ani", "visibility", "sembunyikan")]
    assert "ikk nasional" in log[0]["before"]
    assert team_settings.changes("pii") == []


def test_for_check_gives_the_team_aliases_and_notes_text():
    for note in team_settings.notes("sulni"):
        team_settings.delete_note("sulni", note["id"], by="Ani")
    team_settings.add_alias("sulni", "ULN swasta total", "Swasta", by="Ani")
    team_settings.add_note("sulni", "Kontraksi pertumbuhan = laju yoy negatif.", by="Ani")
    aliases, notes = team_settings.for_check("sulni")
    assert aliases == {"uln swasta total": "Swasta"}
    assert notes == "- Kontraksi pertumbuhan = laju yoy negatif."
    assert team_settings.for_check(None) == ({}, "")


def test_for_check_never_raises(monkeypatch):
    def broken():
        raise RuntimeError("database down")
    monkeypatch.setattr(team_settings, "_engine", broken)
    assert team_settings.for_check("pii") == ({}, "")


# --- what a check does with them -------------------------------------------------------------

import asyncio
from unittest.mock import Mock

from langchain_core.runnables import RunnableLambda

import ai_review
from excel_parser_bi import BITableData
from paired_verifier import _apply_team_aliases, _ExcelSource


def _pii_source(label_aliases=None):
    table = BITableData(title="Posisi Investasi Internasional", unit="Juta USD", row_labels=["Aset", "Kewajiban"])
    table._data = {("Aset", 2025, "Q4"): 558496.5, ("Kewajiban", 2025, "Q4"): 831121.6}
    table.label_aliases = dict(label_aliases or {})
    return _ExcelSource(table=table, filename="TABEL5_39.xls", sheet="5.39")


def test_a_team_alias_resolves_the_whole_metric_name_and_leaves_a_longer_one_alone():
    src = _pii_source()
    _apply_team_aliases([src], {"total afln": "Aset", "afln bank": "Baris Yang Tidak Ada"})
    assert src.table.lookup_fuzzy("Total AFLN", 2025, "Q4") == ("Aset", 558496.5)
    assert src.table.label_aliases == {"total afln": "Aset"}          # the missing row is ignored
    assert src.table._normalise_query("total afln sektor publik") != "Aset"


def test_a_team_alias_wins_over_the_parsers_alias_of_the_same_phrase():
    src = _pii_source({"afln indonesia": "Aset"})
    _apply_team_aliases([src], {"afln indonesia": "Kewajiban"})
    assert src.table.label_aliases["afln indonesia"] == "Kewajiban"


def _capturing_review_llm(seen):
    def answer(prompt_value):
        seen.append(prompt_value.to_messages()[1].content)
        return ai_review.ReviewBatch(reviews=[])
    llm = Mock()
    llm.with_structured_output = Mock(return_value=RunnableLambda(answer))
    return llm


def _one_claim():
    from schemas import FactVerificationResult
    return [FactVerificationResult(operation="value", metric_label="Aset", claimed_value=558.5,
                                   claimed_unit="miliar USD", verdict="Inconclusive", reasoning="-",
                                   context_quote="Posisi AFLN 558,5 miliar dolar AS.", page_number=1)]


def test_the_teams_notes_reach_the_review_prompt_only_when_there_are_some():
    seen = []
    asyncio.run(ai_review.review_claims(_one_claim(), [_pii_source()], _capturing_review_llm(seen),
                                        team_notes="- KFLN = sisi Kewajiban."))
    assert "CATATAN TIM UNTUK PUBLIKASI INI" in seen[0] and "KFLN = sisi Kewajiban." in seen[0]
    seen.clear()
    asyncio.run(ai_review.review_claims(_one_claim(), [_pii_source()], _capturing_review_llm(seen)))
    assert "CATATAN TIM" not in seen[0]
