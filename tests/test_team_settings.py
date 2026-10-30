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


def test_notes_are_the_teams_conventions_only():
    assert team_settings.notes("spe") == []          # nothing is copied from the YAML any more
    team_settings.add_note("spe", '"melambat" berarti yoy turun.', by="Ani")
    assert [n["text"] for n in team_settings.notes("spe")] == ['"melambat" berarti yoy turun.']


@pytest.fixture
def seed_rows():
    """Two rows an older version copied from the SPE YAML: one untouched, one the team edited."""
    from sqlalchemy import insert
    unedited = team_settings.notes_sections("spe")["conventions"][1]
    edited = "Diubah tim: \"turun X%\" berarti -X, juga untuk qtq."
    now = team_settings._now()
    with team_settings._engine().begin() as conn:
        conn.execute(insert(team_settings.notes_seeded).values(publication="spe", seeded_at=now))
        for position, text, editor in ((0, unedited, None), (1, edited, "Budi")):
            conn.execute(insert(team_settings.notes_table).values(
                publication="spe", position=position, text=text, seeded=True, created_by="catatan awal",
                created_at=now, updated_by=editor, updated_at=now if editor else None))
    return unedited, edited


def test_an_edited_seed_row_is_a_team_convention_and_an_unedited_one_is_not(seed_rows):
    unedited, edited = seed_rows
    block = team_settings.for_check("spe")[1]
    assert [n["text"] for n in team_settings.notes("spe")] == [edited]
    assert edited in block
    assert block.count(unedited) == 1          # once, from the YAML; the old copy is not added again


def test_the_word_limit_counts_the_teams_notes_only(monkeypatch):
    monkeypatch.setattr(team_settings, "NOTES_MAX_WORDS", 5)     # far below any YAML's length
    team_settings.add_note("npi", "Satu kalimat pendek.", by="Ani")
    with pytest.raises(SettingsError):
        team_settings.add_note("npi", "dua tiga empat", by="Ani")  # 3 + 3 > 5


def test_a_team_alias_is_edited_and_logged():
    added = team_settings.add_alias("pii", "kfln", "Kewajiban", by="Ani")
    edited = team_settings.update_alias("pii", added["id"], "KFLN  Indonesia", "Kewajiban", by="Budi")
    assert edited["phrase"] == "kfln indonesia"
    assert team_settings.team_aliases("pii") == {"kfln indonesia": "Kewajiban"}
    assert (team_settings.changes("pii")[0]["action"], team_settings.changes("pii")[0]["after"]) == (
        "ubah", "kfln indonesia → Kewajiban")


def test_editing_an_alias_onto_another_ones_phrase_or_a_missing_one_is_refused():
    first = team_settings.add_alias("pii", "kfln", "Kewajiban", by="Ani")
    second = team_settings.add_alias("pii", "afln", "Aset", by="Ani")
    with pytest.raises(SettingsError, match="sudah ada"):
        team_settings.update_alias("pii", second["id"], "kfln", "Aset", by="Ani")
    with pytest.raises(SettingsError, match="tidak ditemukan"):
        team_settings.update_alias("pii", first["id"] + 99, "x", "Aset", by="Ani")


def test_builtin_view_shows_exactly_the_phrases_the_prompt_carries():
    # The tab shows what the AI reads (spec anti-goal): every YAML phrase line, and the parser's
    # TERM_ALIASES grouped by row. Matcher-only aliases (label_aliases) are not notes.
    from publication_parsers import PARSERS
    pii = team_settings.builtin_view("pii")
    shown = {p for g in pii["phrases"] if "row" in g for p in g["phrases"]}
    assert shown == set(PARSERS["pii"].TERM_ALIASES)
    spe = team_settings.builtin_view("spe")
    assert [g["text"] for g in spe["phrases"]] == team_settings.notes_sections("spe")["phrases"]
    assert spe["units_signs"] == ["Indeks tanpa satuan; pertumbuhan dalam persen."]
    block = team_settings.for_check("pii")[1]
    assert all(f'"{p}" → baris' in block for p in shown)


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


def test_reset_empties_the_team_lists_seeds_nothing_and_shows_the_publication():
    team_settings.add_alias("npi", "wisman total", "Ke dalam negeri", by="Ani")
    team_settings.add_note("npi", "catatan tim", by="Ani")
    team_settings.set_hidden("npi", True, by="Ani")
    team_settings.reset("npi", by="Budi")
    assert team_settings.team_aliases("npi") == {}
    assert team_settings.notes("npi") == []
    assert team_settings.builtin_view("npi")["conventions"]           # the built-ins still show
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


def test_for_check_gives_the_whole_block_with_the_team_additions():
    team_settings.add_alias("spe", "penjualan riil", "INDEKS TOTAL", by="Ani")
    team_settings.add_note("spe", '"melambat" berarti yoy turun.', by="Ani")
    aliases, block = team_settings.for_check("spe")
    assert aliases == {"penjualan riil": "INDEKS TOTAL"}
    assert "Indeks tanpa satuan; pertumbuhan dalam persen." in block      # built-in
    assert '"penjualan riil" → baris \'INDEKS TOTAL\'' in block             # team phrase
    assert '- "melambat" berarti yoy turun.' in block                       # team convention
    assert team_settings.for_check(None) == ({}, "")


def test_for_check_falls_back_to_the_built_ins_and_never_raises(monkeypatch):
    def broken():
        raise RuntimeError("database down")
    monkeypatch.setattr(team_settings, "_engine", broken)
    aliases, block = team_settings.for_check("spe")
    assert aliases == {} and "Indeks tanpa satuan" in block


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
    assert "CATATAN PUBLIKASI INI" in seen[0] and "KFLN = sisi Kewajiban." in seen[0]
    seen.clear()
    asyncio.run(ai_review.review_claims(_one_claim(), [_pii_source()], _capturing_review_llm(seen)))
    assert "CATATAN PUBLIKASI" not in seen[0]


# --- upload hints: example file names under each upload area ---------------------------------

def test_every_publication_has_default_upload_hints_taken_from_sharepoint():
    for key in team_settings.PUBLICATIONS:
        hints = team_settings.upload_hints(key)
        assert set(hints) == {"press", "report", "excel"} and hints["excel"]
    assert "Tabel Cadev ORA" in team_settings.upload_hints("cadangan-devisa")["excel"]
    assert "Indimon" in team_settings.upload_hints("cadangan-devisa")["excel"]
    assert team_settings.upload_hints("ppkom")["press"] == ""          # report only
    assert team_settings.upload_hints("uang-primer-m0")["report"] == ""  # press release only


def test_upload_hints_are_edited_logged_and_reset():
    team_settings.set_upload_hints("pii", press="Siaran Pers PIII Tw.<I-IV> 2026.docx", report="",
                                   excel="TABEL5_39.xls", by="Ani")
    assert team_settings.upload_hints("pii") == {
        "press": "Siaran Pers PIII Tw.<I-IV> 2026.docx", "report": "", "excel": "TABEL5_39.xls"}
    log = team_settings.changes("pii")
    assert (log[0]["kind"], log[0]["action"], log[0]["by"]) == ("hint", "ubah", "Ani")
    team_settings.reset("pii", by="Budi")
    assert team_settings.upload_hints("pii") == team_settings.UPLOAD_HINT_DEFAULTS["pii"]


def test_an_overlong_upload_hint_is_refused():
    with pytest.raises(SettingsError):
        team_settings.set_upload_hints("pii", press="x" * 400, report="", excel="", by="Ani")


# --- which documents each publication checks (Pengaturan › Format nama dokumen) -------------------

def test_documents_start_as_the_publication_has_them():
    assert team_settings.documents("uang-beredar") == {"press": True, "report": True, "excel": True}
    assert team_settings.documents("uang-primer-m0") == {"press": True, "report": False, "excel": True}
    assert team_settings.documents("ppkom") == {"press": False, "report": True, "excel": True}


def test_a_document_is_hidden_shown_logged_and_reset():
    team_settings.set_document("sk", "report", False, by="Ani")
    team_settings.set_document("uang-primer-m0", "report", True, by="Ani")
    assert team_settings.documents("sk")["report"] is False
    assert team_settings.documents("uang-primer-m0")["report"] is True
    assert [(c["kind"], c["action"], c["after"]) for c in team_settings.changes("sk")][0] == (
        "document", "sembunyikan", "Laporan")
    team_settings.reset("sk", by="Budi")
    assert team_settings.documents("sk")["report"] is True


def test_press_release_and_report_cannot_both_be_hidden():
    team_settings.set_document("npi", "press", False, by="Ani")
    with pytest.raises(SettingsError, match="dua-duanya"):
        team_settings.set_document("npi", "report", False, by="Ani")
    with pytest.raises(SettingsError, match="dua-duanya"):
        team_settings.set_document("ppkom", "report", False, by="Ani")   # its press is hidden from the start
    with pytest.raises(SettingsError):
        team_settings.set_document("npi", "lampiran", False, by="Ani")


# --- the Pengaturan tab's whole read ---------------------------------------------------------

def test_the_snapshot_reads_what_the_per_publication_readers_read_in_one_connection():
    from sqlalchemy import event

    team_settings.set_hidden("ppkom", True, by="Ani")
    team_settings.add_alias("pii", "total afln", "Aset", by="Ani")
    team_settings.add_alias("pii", "kfln bruto", "Kewajiban", by="Ani")
    team_settings.add_note("pii", "KFLN = Kewajiban.", by="Budi")
    team_settings.add_note("npi", "Defisit ditulis tanpa minus.", by="Budi")
    team_settings.record_known_rows("pii", ["Kewajiban", "Aset"])
    team_settings.set_upload_hints("sk", "SP SK.docx", "", "SK.xlsx", by="Ani")
    team_settings.set_document("spe", "excel", False, by="Ani")

    engine = team_settings._engine()
    checkouts = []
    listener = lambda *_a: checkouts.append(1)
    event.listen(engine.pool, "checkout", listener)
    try:
        snap = team_settings.snapshot()
    finally:
        event.remove(engine.pool, "checkout", listener)

    assert len(checkouts) == 1
    assert list(snap) == list(team_settings.PUBLICATIONS)
    hidden = team_settings.hidden_publications()
    for key in team_settings.PUBLICATIONS:
        assert snap[key] == {"hidden": key in hidden,
                             "builtin": team_settings.builtin_view(key),
                             "team_aliases": team_settings.list_team_aliases(key),
                             "notes": team_settings.notes(key),
                             "known_rows": team_settings.known_rows(key),
                             "upload_hints": team_settings.upload_hints(key),
                             "documents": team_settings.documents(key)}, key
    assert snap["ppkom"]["hidden"] and [a["phrase"] for a in snap["pii"]["team_aliases"]] == [
        "total afln", "kfln bruto"]
    assert snap["pii"]["known_rows"] == ["Aset", "Kewajiban"] and snap["spe"]["documents"]["excel"] is False
