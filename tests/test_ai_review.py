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


from ai_review import ClaimReview, judge_review
from schemas import FactVerificationResult


def _result(verdict="Inconclusive", operation="value", claimed_value=0.11, unit="triliun Rp",
            quote="Kredit modal kerja pada Juni 2026 tercatat Rp0,11 triliun.", label="kredit modal kerja"):
    return FactVerificationResult(
        operation=operation, metric_label=label, claimed_value=claimed_value, claimed_unit=unit,
        verdict=verdict, reasoning="kode: tidak cocok", context_quote=quote, page_number=2,
    )


def _review(**overrides):
    base = dict(
        claim_no=1, first_words="Kredit modal kerja pada Juni", subject="Kredit modal kerja",
        word_check="kredit: ada; modal: ada; kerja: ada", match="persis", verdict="sesuai",
        operation="value",
        cells=[CitedCell(sheet="S1", row="Kredit Modal Kerja", period="Jun 2026", value=110.0)],
        explanation="Baris kredit modal kerja Juni 2026.",
    )
    base.update(overrides)
    return ClaimReview(**base)


_CODES = {"S1": _source(_KREDIT)}


def test_matching_cells_turn_a_refuted_claim_entailed():
    # 110 Miliar Rp = 0,11 triliun Rp: the engine does the unit conversion, not the model
    final, outcome = judge_review(_result(verdict="Refuted"), _review(), _CODES)
    assert outcome == "to_entailed"
    assert final.verdict == "Entailed"
    assert final.resolved_via == "ai_review"
    assert "Ditinjau AI: sel Tabel 1 · Kredit Modal Kerja · Jun 2026 = 110.0" in final.reasoning
    assert final.page_number == 2


def test_a_yoy_claim_is_recomputed_by_the_engine_from_the_cited_row():
    result = _result(operation="yoy_growth", claimed_value=10.0, unit="persen",
                     quote="Kredit modal kerja tumbuh 10,0% (yoy) pada Juni 2026.")
    review = _review(first_words="Kredit modal kerja tumbuh 10,0%", operation="yoy_growth", cells=[
        CitedCell(sheet="S1", row="Kredit Modal Kerja", period="Jun 2025", value=100.0),
        CitedCell(sheet="S1", row="Kredit Modal Kerja", period="Jun 2026", value=110.0),
    ])
    final, outcome = judge_review(result, review, _CODES)
    assert (outcome, final.verdict, final.computed_value) == ("to_entailed", "Entailed", 10.0)


def test_a_threshold_claim_is_judged_by_the_engine():
    pmi = _table(title="Prompt Manufacturing Index", unit="Indeks",
                 data={("PMI-BI", 2025, "Q2"): 49.0, ("PMI-BI", 2026, "Q2"): 51.3})
    result = _result(operation="above_threshold", claimed_value=50.0, unit=None,
                     quote="PMI-BI triwulan II 2026 berada di atas 50.", label="PMI-BI")
    review = _review(first_words="PMI-BI triwulan II 2026 berada", subject="PMI-BI",
                     operation="above_threshold",
                     cells=[CitedCell(sheet="S1", row="PMI-BI", period="Q2 2026", value=51.3)])
    final, outcome = judge_review(result, review, {"S1": _source(pmi, sheet="T1")})
    assert (outcome, final.verdict) == ("to_entailed", "Entailed")


def test_not_in_table_turns_a_refuted_claim_inconclusive():
    review = _review(match="tidak_ada", verdict="tidak_ada_di_tabel", cells=[],
                     explanation="Tabel tidak memuat kredit modal kerja bank BUMN.")
    final, outcome = judge_review(_result(verdict="Refuted"), review, _CODES)
    assert (outcome, final.verdict, final.resolved_via) == ("to_inconclusive", "Inconclusive", "ai_review")
    assert final.computed_value is None
    assert "tabel tidak memuat data yang dimaksud" in final.reasoning


def test_not_in_table_leaves_an_inconclusive_claim_alone():
    review = _review(match="tidak_ada", verdict="tidak_ada_di_tabel", cells=[])
    original = _result()
    assert judge_review(original, review, _CODES) == (original, "unchanged")


def test_only_a_persis_match_may_change_a_verdict():
    original = _result(verdict="Refuted")
    for match in ("total_atau_induk", "mirip"):
        assert judge_review(original, _review(match=match), _CODES) == (original, "unchanged")


def test_a_number_the_engine_cannot_reproduce_keeps_the_verdict():
    original = _result(verdict="Refuted", claimed_value=0.2)  # cell gives 0,11
    assert judge_review(original, _review(verdict="sesuai"), _CODES) == (original, "unchanged")


def test_a_new_refuted_is_only_flagged_by_default():
    original = _result(claimed_value=0.2)
    final, outcome = judge_review(original, _review(verdict="tidak_sesuai"), _CODES)
    assert (outcome, final.verdict, final.resolved_via) == ("flag", "Inconclusive", "ai_review_flag")
    assert "perlu ditinjau" in final.reasoning


def test_a_new_refuted_becomes_a_verdict_when_configured():
    final, outcome = judge_review(_result(claimed_value=0.2), _review(verdict="tidak_sesuai"),
                                  _CODES, new_refuted="verdict")
    assert (outcome, final.verdict, final.resolved_via) == ("to_refuted", "Refuted", "ai_review")


def test_a_refuted_claim_the_model_also_refutes_stays_as_it_was():
    original = _result(verdict="Refuted", claimed_value=0.2)
    assert judge_review(original, _review(verdict="tidak_sesuai"), _CODES) == (original, "unchanged")


def test_the_guard_blocks_a_total_row_for_a_breakdown_claim():
    npi = _table(title="Neraca Pembayaran Indonesia", unit="Juta USD",
                 data={("Migas > Impor", 2026, "Q2"): 9000.0})
    original = _result(verdict="Inconclusive", claimed_value=9.0, unit="miliar USD",
                       quote="Impor minyak mentah tercatat 9,0 miliar USD.", label="impor minyak mentah")
    review = _review(first_words="Impor minyak mentah tercatat 9,0", subject="Impor minyak mentah",
                     cells=[CitedCell(sheet="S1", row="Migas > Impor", period="Q2 2026", value=9000.0)])
    assert judge_review(original, review, {"S1": _source(npi, sheet="I.1")}) == (original, "unchanged")


def test_a_swapped_claim_number_is_ignored():
    original = _result(verdict="Refuted")
    review = _review(first_words="Penjualan eceran pada Juni 2026")
    assert judge_review(original, review, _CODES) == (original, "unchanged")
