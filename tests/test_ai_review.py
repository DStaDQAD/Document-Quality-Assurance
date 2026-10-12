"""Tinjauan AI: the model names cells, code checks them and re-runs the engine."""

from unittest.mock import Mock

from langchain_core.runnables import RunnableLambda

import ai_review
from ai_review import CitedCell, read_cells, reviewable_sources, table_markdown
from excel_parser_bi import BITableData
from paired_verifier import _ExcelSource


def _table(title="Kredit Perbankan", unit="Miliar Rp", data=None):
    """data: {(row_label, year, month): value}"""
    table = BITableData(title=title, unit=unit, row_labels=[])
    for (label, year, month), value in (data or {}).items():
        if label not in table.row_labels:
            table.row_labels.append(label)
        table._data[(label, year, month)] = value
    return table


def _source(table, sheet="Tabel 1", filename="kredit.xlsx", origin="excel"):
    return _ExcelSource(table=table, filename=filename, sheet=sheet, origin=origin)


_KREDIT = _table(data={
    ("Kredit Modal Kerja", 2024, "Jun"): 90.0,
    ("Kredit Modal Kerja", 2025, "Jun"): 100.0,
    ("Kredit Modal Kerja", 2026, "Jun"): 110.0,
    ("Kredit Konsumsi", 2025, "Jun"): 50.0,
    ("Kredit Konsumsi", 2026, "Jun"): 55.0,
    ("Kredit Lama", 2024, "Jun"): 7.0,
})


def test_table_markdown_shows_every_row_but_only_the_last_two_years():
    md = table_markdown("S1", _source(_KREDIT))
    assert "### S1 — sheet 'Tabel 1': Kredit Perbankan (satuan: Miliar Rp)" in md
    assert "| Baris | Jun 2025 | Jun 2026 |" in md
    assert "| Kredit Modal Kerja | 100.0 | 110.0 |" in md
    assert "2024" not in md


def test_table_markdown_drops_rows_empty_in_the_shown_years():
    assert "Kredit Lama" not in table_markdown("S1", _source(_KREDIT))


def test_reviewable_sources_skips_charts_other_pdfs_and_empty_tables():
    keep = _source(_KREDIT)
    pdf = _source(_KREDIT, origin="pdf", sheet="Hal. 3")
    chart = _source(_KREDIT, origin="chart")
    other = _source(_KREDIT, origin="pdf_other")
    empty = _source(_table(data={}))
    pointer_only = _ExcelSource(table=_KREDIT, filename="x.xls", sheet="I.1", pointer_only=True)
    assert reviewable_sources([chart, keep, other, empty, pdf, pointer_only]) == {"S1": keep, "S2": pdf}


def test_read_cells_returns_label_key_and_value():
    codes = {"S1": _source(_KREDIT)}
    cited = [CitedCell(sheet="s1", row="kredit modal kerja", period="Jun 2026", value=110.0)]
    src, keyed = read_cells(cited, codes)
    assert src is codes["S1"]
    assert keyed == [("Kredit Modal Kerja", (2026, "Jun"), 110.0)]


def test_read_cells_rejects_a_value_that_is_not_in_the_cell():
    cited = [CitedCell(sheet="S1", row="Kredit Modal Kerja", period="Jun 2026", value=111.0)]
    assert read_cells(cited, {"S1": _source(_KREDIT)}) is None


def test_read_cells_rejects_a_row_or_column_that_does_not_exist():
    codes = {"S1": _source(_KREDIT)}
    assert read_cells([CitedCell(sheet="S1", row="Kredit Investasi", period="Jun 2026", value=1.0)], codes) is None
    assert read_cells([CitedCell(sheet="S1", row="Kredit Modal Kerja", period="Jul 2026", value=1.0)], codes) is None


def test_read_cells_rejects_cells_from_two_sheets_or_none_at_all():
    codes = {"S1": _source(_KREDIT), "S2": _source(_KREDIT, sheet="Tabel 2")}
    cited = [
        CitedCell(sheet="S1", row="Kredit Modal Kerja", period="Jun 2025", value=100.0),
        CitedCell(sheet="S2", row="Kredit Modal Kerja", period="Jun 2026", value=110.0),
    ]
    assert read_cells(cited, codes) is None
    assert read_cells([], codes) is None


from ai_review import subject_is_covered

_NPI = _table(title="Neraca Pembayaran Indonesia", unit="Juta USD", data={
    ("Migas > Impor", 2026, "Q2"): -9000.0,
    ("Kredit Pemilikan Rumah", 2026, "Q2"): 1.0,
})


def test_the_subject_must_be_copied_from_the_sentence():
    quote = "Impor minyak mentah turun pada triwulan II 2026."
    assert not subject_is_covered("impor migas", quote, ["Migas > Impor"], _source(_NPI))


def test_a_subject_word_missing_from_row_and_title_fails():
    quote = "Impor minyak mentah turun pada triwulan II 2026."
    assert not subject_is_covered("Impor minyak mentah", quote, ["Migas > Impor"], _source(_NPI))


def test_subject_words_found_in_the_row_path_or_title_pass():
    quote = "Impor migas turun pada triwulan II 2026."
    assert subject_is_covered("Impor migas", quote, ["Migas > Impor"], _source(_NPI))
    quote = "Neraca pembayaran impor migas turun."
    assert subject_is_covered("Neraca pembayaran impor migas", quote, ["Migas > Impor"], _source(_NPI))


def test_a_derivational_prefix_counts_as_the_same_word():
    quote = "Kredit kepemilikan rumah tumbuh 5%."
    assert subject_is_covered("Kredit kepemilikan rumah", quote, ["Kredit Pemilikan Rumah"], _source(_NPI))


def test_generic_words_and_numbers_are_ignored():
    quote = "Pertumbuhan kredit modal kerja tahunan 2026 tercatat 10%."
    assert subject_is_covered(
        "Pertumbuhan kredit modal kerja tahunan 2026", quote, ["Kredit Modal Kerja"], _source(_KREDIT)
    )
