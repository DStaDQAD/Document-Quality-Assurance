"""Publication notes: each publication's conventions, shown to the extractor for that publication
only, behind PUBLICATION_NOTES (docs/specs/publication-notes.md). The notes guide; nothing reads
them to refuse or abstain."""
import asyncio
import re
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock, patch

import pytest
import yaml
from langchain_core.runnables import RunnableLambda

import publication_parsers
from publication_parsers import PARSERS, notes_enabled, notes_for
from structured_extractor import _EXTRACTION_PROMPT, extract_structured_facts_async

ROOT = Path(__file__).resolve().parents[1]
SECTIONS = {"conventions", "phrases", "periods", "units_signs"}
NARRATIVE = "[== Halaman 1 ==]\nPMI-BI triwulan I 2026 sebesar 52,03%, berada pada fase ekspansi.\n"


def _capturing_llm(seen):
    def _respond(prompt_value):
        seen.append(prompt_value.to_messages())
        return SimpleNamespace(facts=[])

    llm = Mock()
    llm.with_structured_output = Mock(return_value=RunnableLambda(_respond))
    return llm


def test_without_notes_the_prompt_is_todays():
    plain = _EXTRACTION_PROMPT.format_messages(row_labels_block="- Total", narrative_text="teks")
    empty = _EXTRACTION_PROMPT.format_messages(
        row_labels_block="- Total", narrative_text="teks", publication_block="")

    assert [m.content for m in plain] == [m.content for m in empty]
    assert plain[1].content.startswith("REFERENCE METRIC LIST")


def test_the_notes_reach_every_chunk_and_leave_the_system_prompt_alone():
    seen, plain = [], []
    two_pages = ("[== Halaman 1 ==]\nPMI-BI pada triwulan I 2026 tercatat sebesar 52,03%.\n"
                 "[== Halaman 2 ==]\nVolume Produksi pada triwulan I 2026 tercatat sebesar 54,07%.\n")
    asyncio.run(extract_structured_facts_async(
        two_pages, ["PMI - BI"], _capturing_llm(seen), max_chars_per_chunk=50,
        publication_notes="PUBLIKASI: PMI-BI\n- fase ekspansi = indeks di atas 50.",
    ))
    asyncio.run(extract_structured_facts_async(two_pages, ["PMI - BI"], _capturing_llm(plain),
                                               max_chars_per_chunk=50))

    assert len(seen) == 2
    assert all("fase ekspansi = indeks di atas 50" in messages[1].content for messages in seen)
    assert seen[0][0].content == plain[0][0].content
    assert not any("PUBLIKASI:" in messages[1].content for messages in plain)


def test_notes_for_gives_the_chosen_publication_only():
    pmi, skdu = notes_for("pmi"), notes_for("skdu")

    assert pmi.startswith("PUBLIKASI: ") and "PMI" in pmi.splitlines()[0]
    assert "SKDU" not in pmi.splitlines()[0] and "PMI" not in skdu.splitlines()[0]
    assert notes_for("") == "" and notes_for("tidak-ada") == ""


def test_the_parsers_term_aliases_are_listed_as_phrases():
    pmi = notes_for("pmi")

    for phrase, row in PARSERS["pmi"].TERM_ALIASES.items():
        assert phrase in pmi and row in pmi


@pytest.mark.parametrize("publication", sorted(PARSERS))
def test_every_publication_has_valid_notes(publication):
    path = publication_parsers.notes_path(publication)
    data = yaml.safe_load(path.read_text(encoding="utf-8"))

    assert data["publication"] == publication
    assert isinstance(data["title"], str) and data["title"]
    assert set(data) - {"publication", "title"} <= SECTIONS
    entries = [entry for section in SECTIONS for entry in data.get(section) or []]
    assert entries and all(isinstance(entry, str) and entry.strip() for entry in entries)
    words = sum(len(entry.split()) for entry in entries)
    assert words <= publication_parsers.NOTES_MAX_WORDS, f"{publication}: {words} words"


def test_a_broken_notes_file_says_so_in_indonesian(tmp_path, monkeypatch):
    bad = tmp_path / "pmi.notes.yaml"
    bad.write_text("publication: pmi\ntitle: PMI\nsaran: [x]\n", encoding="utf-8")
    monkeypatch.setattr(publication_parsers, "notes_path", lambda publication: bad)

    with pytest.raises(ValueError, match="bagian tidak dikenal"):
        notes_for("pmi")


@pytest.mark.parametrize("value, on", [("1", True), ("on", True), ("", False), ("0", False)])
def test_the_flag_reads_publication_notes(monkeypatch, value, on):
    monkeypatch.setenv("PUBLICATION_NOTES", value)

    assert notes_enabled() is on


@pytest.mark.parametrize("flag, expected", [("1", True), ("", False)])
@patch("paired_verifier.extract_structured_facts_async")
@patch("paired_verifier.parse_bi_table")
def test_verify_paired_passes_the_notes_only_when_the_flag_is_on(
    mock_parse, mock_extract, monkeypatch, flag, expected
):
    from excel_parser_bi import BITableData
    from paired_verifier import verify_paired

    monkeypatch.setenv("PUBLICATION_NOTES", flag)
    table = BITableData(title="PMI", unit="%, Indeks", row_labels=["PMI - BI"])
    table._data[("PMI - BI", 2026, "Q1")] = 52.03
    mock_parse.return_value = table
    mock_extract.return_value = []

    asyncio.run(verify_paired(narrative_text=NARRATIVE, excel_sources=[(b"x", "Lain", "x.xlsx")],
                              llm=Mock(), publication="pmi"))

    notes = mock_extract.call_args.kwargs.get("publication_notes", "")
    assert bool(notes) is expected
    if expected:
        assert notes == notes_for("pmi")


def test_only_the_prompt_builder_reads_the_notes():
    # The notes guide the extractor; verdict code must never depend on them.
    allowed = {"publication_parsers/__init__.py", "paired_verifier.py"}
    reads = re.compile(r"\bnotes_for\(|\.notes\.yaml\"")
    readers = {path.relative_to(ROOT).as_posix()
               for path in [*ROOT.glob("*.py"), *(ROOT / "publication_parsers").glob("*.py")]
               if reads.search(path.read_text(encoding="utf-8"))}

    assert readers <= allowed
    verifier = (ROOT / "paired_verifier.py").read_text(encoding="utf-8")
    assert len(re.findall(r"\bnotes_for\(", verifier)) == 1
