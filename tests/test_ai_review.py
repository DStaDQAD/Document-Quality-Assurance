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
    assert final.reason_code == "ditinjau_ai"


def test_not_in_table_leaves_an_inconclusive_claim_alone():
    review = _review(match="tidak_ada", verdict="tidak_ada_di_tabel", cells=[])
    original = _result()
    assert judge_review(original, review, _CODES) == (original, "unchanged")


def test_a_match_label_does_not_decide_alone_the_cells_do():
    # Substance before form: cells that reproduce the number on the sentence's own row are proof
    # whatever match kind the model wrote. A row the subject does not name is still refused.
    original = _result(verdict="Refuted")
    for match in ("total_atau_induk", "mirip"):
        assert judge_review(original, _review(match=match), _CODES)[1] == "to_entailed"
    other_row = _review(match="mirip", subject="Kredit modal kerja",
                        cells=[CitedCell(sheet="S1", row="Kredit Konsumsi", period="Jun 2026", value=55.0)])
    konsumsi = _result(verdict="Refuted", claimed_value=0.055)
    assert judge_review(konsumsi, other_row, _CODES) == (konsumsi, "unchanged")


_PII = _table(title="Posisi Investasi Internasional Indonesia", unit="Juta USD", data={
    ("Aset", 2025, "Q3"): 545541.73, ("Aset", 2025, "Q4"): 558496.49,
    ("Aset > Investasi Portofolio", 2024, "Q4"): 37763.42, ("Aset > Investasi Portofolio", 2025, "Q4"): 40571.18,
    ("Kewajiban > Investasi Portofolio", 2024, "Q4"): 278576.70,
    ("Kewajiban > Investasi Portofolio", 2025, "Q4"): 293454.72,
})
_PII.label_aliases = {"afln indonesia": "Aset"}


def test_the_right_cells_under_a_not_in_table_label_are_accepted_through_an_alias():
    # ctrl_1022 PII sp: "Posisi AFLN Indonesia meningkat" — the model cited Aset Q3 -> Q4 and wrote
    # "AFLN adalah padanan untuk 'Aset'", but answered tidak_ada_di_tabel ("AFLN: tidak ada").
    quote = "Posisi AFLN Indonesia meningkat terutama didorong oleh kenaikan cadangan devisa."
    result = _result(operation="is_increasing", claimed_value=None, unit=None, quote=quote, label="AFLN Indonesia")
    review = _review(first_words="Posisi AFLN Indonesia meningkat terutama", subject="AFLN Indonesia",
                     match="total_atau_induk", verdict="tidak_ada_di_tabel", operation="is_increasing",
                     cells=[CitedCell(sheet="S1", row="Aset", period="Q3 2025", value=545541.73),
                            CitedCell(sheet="S1", row="Aset", period="Q4 2025", value=558496.49)])
    codes = {"S1": _source(_PII, sheet="5.39")}

    assert judge_review(result, review, codes)[1] == "to_entailed"
    no_alias = _table(title="Posisi Investasi Internasional Indonesia", unit="Juta USD", data={
        ("Aset", 2025, "Q3"): 545541.73, ("Aset", 2025, "Q4"): 558496.49})
    assert judge_review(result, review, {"S1": _source(no_alias, sheet="5.39")}) == (result, "unchanged")


def _net_change_review(formula="(c4-c3)-(c2-c1)", claim_number=12.1):
    return _review(
        first_words="Perkembangan ini terutama dikontribusikan oleh", subject="kewajiban neto investasi portofolio",
        match="persis", verdict="sesuai", operation="diff", formula=formula, formula_unit="tabel",
        claim_number=claim_number,
        cells=[CitedCell(sheet="S1", row="Aset > Investasi Portofolio", period="Q4 2024", value=37763.42),
               CitedCell(sheet="S1", row="Aset > Investasi Portofolio", period="Q4 2025", value=40571.18),
               CitedCell(sheet="S1", row="Kewajiban > Investasi Portofolio", period="Q4 2024", value=278576.70),
               CitedCell(sheet="S1", row="Kewajiban > Investasi Portofolio", period="Q4 2025", value=293454.72)])


_NET_QUOTE = ("Perkembangan ini terutama dikontribusikan oleh meningkatnya kewajiban neto investasi "
              "portofolio sebesar 12,1 miliar dolar AS.")


def test_a_formula_over_four_cells_proves_a_change_in_net_liabilities():
    # (293.454,72 − 40.571,18) − (278.576,70 − 37.763,42) = 12.070,3 juta = 12,1 miliar.
    result = _result(verdict="Inconclusive", operation="diff", claimed_value=12.1, unit="miliar USD",
                     quote=_NET_QUOTE, label="kewajiban neto investasi portofolio")
    final, outcome = judge_review(result, _net_change_review(), {"S1": _source(_PII, sheet="5.39")})
    assert (outcome, final.verdict, final.computed_value) == ("to_entailed", "Entailed", 12.0703)
    assert "rumus (c4-c3)-(c2-c1)" in final.reasoning


def test_a_formula_the_other_way_round_does_not_pass_on_its_size():
    result = _result(verdict="Refuted", operation="diff", claimed_value=12.1, unit="miliar USD",
                     quote=_NET_QUOTE, label="kewajiban neto investasi portofolio")
    review = _net_change_review(formula="(c2-c1)-(c4-c3)")
    assert judge_review(result, review, {"S1": _source(_PII, sheet="5.39")}) == (result, "unchanged")


def test_a_formula_that_misses_the_number_flags_a_tidak_cukup_data():
    result = _result(verdict="Inconclusive", operation="diff", claimed_value=15.0, unit="miliar USD",
                     quote=_NET_QUOTE.replace("12,1", "15,0"), label="kewajiban neto investasi portofolio")
    final, outcome = judge_review(result, _net_change_review(claim_number=15.0),
                                  {"S1": _source(_PII, sheet="5.39")})
    assert (outcome, final.verdict) == ("flag", "Inconclusive")


def test_a_formula_result_is_not_widened_by_how_the_sentence_prints_it():
    # PII Tw IV-2025 s046: "sebesar 14 juta dolar AS" against 803,20 -> 817,10 = 13,90. A change
    # printed without decimals is not widened (owner's rule: "23 juta" vs 22,78 is Tidak Sesuai).
    pii = _table(title="Posisi Investasi Internasional Indonesia", unit="Juta USD", data={
        ("Aset > Derivatif Finansial", 2025, "Q3"): 803.2041, ("Aset > Derivatif Finansial", 2025, "Q4"): 817.1022})
    quote = "Kenaikan posisi aset derivatif finansial sebesar 14 juta dolar AS."
    result = _result(verdict="Refuted", operation="diff", claimed_value=14.0, unit="juta USD", quote=quote,
                     label="aset derivatif finansial")
    review = _review(first_words="Kenaikan posisi aset derivatif finansial", subject="aset derivatif finansial",
                     operation="diff", formula="c2-c1", formula_unit="tabel", claim_number=14,
                     cells=[CitedCell(sheet="S1", row="Aset > Derivatif Finansial", period="Q3 2025", value=803.2041),
                            CitedCell(sheet="S1", row="Aset > Derivatif Finansial", period="Q4 2025", value=817.1022)])
    assert judge_review(result, review, {"S1": _source(pii, sheet="5.39")}) == (result, "unchanged")


def test_a_formula_that_is_not_allowed_is_ignored():
    result = _result(verdict="Inconclusive", operation="diff", claimed_value=12.1, unit="miliar USD",
                     quote=_NET_QUOTE, label="kewajiban neto investasi portofolio")
    review = _net_change_review(formula="__import__('os').getcwd()")
    assert judge_review(result, review, {"S1": _source(_PII, sheet="5.39")})[1] != "to_entailed"


_CITIES = ("BANDUNG", "MEDAN", "PADANG", "SURABAYA")
_SHPR = _table(title="Pertumbuhan IHPR per Kota", unit="%", data={
    **{(f"TAHUNAN (YOY) > {c} > TOTAL", 2025, "Q3"): q3 for c, q3 in zip(_CITIES, (2.0, 1.5, 1.0, 0.5))},
    **{(f"TAHUNAN (YOY) > {c} > TOTAL", 2025, "Q4"): q4 for c, q4 in zip(_CITIES, (1.8, 1.2, 0.7, 0.9))},
})
_COUNT_QUOTE = "Secara spasial, dari 4 kota yang disurvei, 3 kota diantaranya mengalami perlambatan pertumbuhan IHPR."


def _count_review(cities):
    cells = []
    for c in cities:
        for q, v in (("Q3 2025", _SHPR._data[(f"TAHUNAN (YOY) > {c} > TOTAL", 2025, "Q3")]),
                     ("Q4 2025", _SHPR._data[(f"TAHUNAN (YOY) > {c} > TOTAL", 2025, "Q4")])):
            cells.append(CitedCell(sheet="S1", row=f"TAHUNAN (YOY) > {c} > TOTAL", period=q, value=v))
    formula = "+".join(f"(c{2 * i + 2}<c{2 * i + 1})" for i in range(len(cities)))
    return _review(first_words="Secara spasial, dari 4 kota", subject="pertumbuhan IHPR", match="persis",
                   verdict="sesuai", operation="is_decreasing", formula=formula, formula_unit="jumlah",
                   claim_number=3, cells=cells)


def test_a_count_over_every_city_is_proved_by_its_formula():
    # SHPR Tw IV-2025: "dari 18 kota yang disurvei, 12 kota diantaranya mengalami perlambatan
    # pertumbuhan" came back is_decreasing on the national index and was refuted.
    result = _result(verdict="Refuted", operation="is_decreasing", claimed_value=None, unit=None,
                     quote=_COUNT_QUOTE, label="IHPR")
    final, outcome = judge_review(result, _count_review(_CITIES), {"S1": _source(_SHPR, sheet="TABEL 3")})
    assert (outcome, final.verdict) == ("to_entailed", "Entailed")


def test_a_count_that_leaves_out_a_city_is_not_used():
    # Three slowed of the three cited proves nothing about the fourth.
    result = _result(verdict="Refuted", operation="is_decreasing", claimed_value=None, unit=None,
                     quote=_COUNT_QUOTE, label="IHPR")
    review = _count_review(("BANDUNG", "MEDAN", "PADANG"))
    assert judge_review(result, review, {"S1": _source(_SHPR, sheet="TABEL 3")}) == (result, "unchanged")


def test_a_condition_formula_proves_an_eased_contraction():
    uln = _table(title="Posisi Utang Luar Negeri (ULN) Indonesia", unit="Juta USD", data={
        ("Swasta", 2025, "Mar"): 195160.0, ("Swasta", 2025, "Jun"): 195733.6,
        ("Swasta", 2026, "Mar"): 192525.57, ("Swasta", 2026, "Jun"): 194616.52})
    quote = "Perkembangan tersebut disebabkan oleh peningkatan ULN publik, di tengah kontraksi pertumbuhan ULN swasta yang lebih rendah."
    result = _result(verdict="Refuted", operation="is_decreasing", claimed_value=None, unit=None,
                     quote=quote, label="ULN swasta")
    review = _review(first_words="Perkembangan tersebut disebabkan oleh peningkatan", subject="ULN swasta",
                     operation="is_decreasing", formula="c4/c1-1 < 0 and abs(c4/c1-1) < abs(c3/c2-1)",
                     cells=[CitedCell(sheet="S1", row="Swasta", period="Jun 2025", value=195733.6),
                            CitedCell(sheet="S1", row="Swasta", period="Mar 2025", value=195160.0),
                            CitedCell(sheet="S1", row="Swasta", period="Mar 2026", value=192525.57),
                            CitedCell(sheet="S1", row="Swasta", period="Jun 2026", value=194616.52)])
    final, outcome = judge_review(result, review, {"S1": _source(uln, sheet="TabI.1")})
    assert (outcome, final.verdict) == ("to_entailed", "Entailed")


def test_the_prompt_lists_the_publications_names_for_rows():
    lines = ai_review.alias_lines({"S1": _source(_PII, sheet="5.39")})
    assert "\"afln indonesia\" = baris 'Aset' (S1)" in lines


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


_NPI_MIGAS = {"S1": _source(_table(title="Neraca Pembayaran Indonesia", unit="Juta USD",
                                   data={("Migas > Impor", 2026, "Q2"): 9000.0}), sheet="I.1")}


def test_the_guard_blocks_a_mismatch_found_on_a_total_row_for_a_breakdown_claim():
    # A new Tidak Sesuai is the answer a wrong row produces most easily ("impor minyak" read off
    # "Migas > Impor" gives a number that differs), so it must name the claim's own subject.
    original = _result(verdict="Inconclusive", claimed_value=4.0, unit="miliar USD",
                       quote="Impor minyak mentah tercatat 4,0 miliar USD.", label="impor minyak mentah")
    review = _review(first_words="Impor minyak mentah tercatat 4,0", subject="Impor minyak mentah",
                     verdict="tidak_sesuai",
                     cells=[CitedCell(sheet="S1", row="Migas > Impor", period="Q2 2026", value=9000.0)])
    assert judge_review(original, review, _NPI_MIGAS) == (original, "unchanged")
    assert judge_review(original, review, _NPI_MIGAS, use_guard=False)[1] == "flag"


def test_the_guard_does_not_block_a_number_the_engine_reproduces():
    # Measured 2026-10-12: the guard threw away 49 correct Sesuai over abbreviations (KFLN for the
    # 'Kewajiban' row, "ULN publik" for 'Pemerintah dan Bank Sentral'). An exact match of the
    # claimed number from the cited cells is the proof; the guard adds nothing there.
    original = _result(verdict="Inconclusive", claimed_value=9.0, unit="miliar USD",
                       quote="Impor KFLN tercatat 9,0 miliar USD.", label="impor KFLN")
    review = _review(first_words="Impor KFLN tercatat 9,0", subject="Impor KFLN",
                     cells=[CitedCell(sheet="S1", row="Migas > Impor", period="Q2 2026", value=9000.0)])
    assert judge_review(original, review, _NPI_MIGAS)[1] == "to_entailed"


def test_a_swapped_claim_number_is_ignored():
    original = _result(verdict="Refuted")
    review = _review(first_words="Penjualan eceran pada Juni 2026")
    assert judge_review(original, review, _CODES) == (original, "unchanged")


import asyncio

from ai_review import ReviewBatch, review_candidates, review_claims


def _llm_answering(answer_for_batch, calls):
    def _respond(prompt_value):
        calls.append(prompt_value.to_string())
        return answer_for_batch(len(calls))

    llm = Mock()
    llm.with_structured_output = Mock(return_value=RunnableLambda(_respond))
    return llm


def test_review_candidates_are_the_narrative_claims_the_code_did_not_confirm():
    results = [
        _result(verdict="Entailed"), _result(verdict="Refuted"), _result(),
        _result().model_copy(update={"checked_item": "chart"}),
    ]
    assert review_candidates(results) == [1, 2]


def test_review_candidates_skip_a_claim_the_section_guard_set_aside():
    # The table holds no data for the claim's report section, so any cell the model cites is
    # on the wrong row (UB Agustus 2026: DPK's simpanan berjangka vs M2's).
    results = [_result(), _result().model_copy(update={"resolved_via": "section_guard"}),
               _result().model_copy(update={"resolved_via": "absent_item_guard"})]
    assert review_candidates(results) == [0]


def test_review_claims_batches_claims_and_maps_answers_back():
    results = [_result(verdict="Entailed"), _result(verdict="Refuted"), _result(), _result()]
    calls = []
    llm = _llm_answering(lambda n: ReviewBatch(reviews=[_review(claim_no=1)]), calls)

    reviewed, stats = asyncio.run(review_claims(results, [_source(_KREDIT)], llm, batch_size=2))

    # 3 candidates, 2 per batch → 2 calls; each reply answers only its claim 1 (indices 1 and
    # 3), so index 2 is asked once more on its own → a third call.
    assert len(calls) == 3
    assert all("### S1 — sheet 'Tabel 1'" in c for c in calls)
    assert "1. Kalimat: \"Kredit modal kerja pada Juni 2026" in calls[0]
    assert [r.verdict for r in reviewed] == ["Entailed", "Entailed", "Entailed", "Entailed"]
    assert (stats.candidates, stats.answered, stats.changed) == (3, 3, 3)


def test_a_failed_batch_keeps_the_code_verdicts():
    def _raise(_prompt_value):
        raise RuntimeError("quota")

    llm = Mock()
    llm.with_structured_output = Mock(return_value=RunnableLambda(_raise))
    results = [_result(verdict="Refuted")]
    reviewed, stats = asyncio.run(review_claims(results, [_source(_KREDIT)], llm))
    assert reviewed == results
    assert stats.answered == 0


def test_review_claims_without_reviewable_sources_makes_no_call():
    llm = Mock()
    results = [_result(verdict="Refuted")]
    reviewed, stats = asyncio.run(review_claims(results, [_source(_KREDIT, origin="chart")], llm))
    assert reviewed == results
    llm.with_structured_output.assert_not_called()


def test_batches_are_asked_in_parallel():
    # NPI sends ~9 batches; asked one after another they took 216 s (2026-10-12).
    import threading
    import time

    lock, state = threading.Lock(), {"now": 0, "peak": 0}

    def _respond(_prompt_value):
        with lock:
            state["now"] += 1
            state["peak"] = max(state["peak"], state["now"])
        time.sleep(0.2)
        with lock:
            state["now"] -= 1
        return ReviewBatch(reviews=[])

    llm = Mock()
    llm.with_structured_output = Mock(return_value=RunnableLambda(_respond))
    results = [_result() for _ in range(3)]
    asyncio.run(review_claims(results, [_source(_KREDIT)], llm, batch_size=1))
    assert state["peak"] > 1


def test_claims_a_batch_left_unanswered_are_asked_once_more():
    # NPI 2026-10-12: one batch came back with 6 answers for 30 claims.
    calls = []

    def _answer(n):
        return ReviewBatch(reviews=[_review(claim_no=1)])   # only ever answers the first claim

    llm = _llm_answering(_answer, calls)
    results = [_result(verdict="Refuted"), _result(verdict="Refuted")]
    reviewed, stats = asyncio.run(review_claims(results, [_source(_KREDIT)], llm, batch_size=2))
    assert len(calls) == 2                      # the batch, then the one claim it skipped
    assert stats.answered == 2
    assert [r.verdict for r in reviewed] == ["Entailed", "Entailed"]


def test_unanswered_claims_are_not_asked_a_third_time():
    calls = []
    llm = _llm_answering(lambda n: ReviewBatch(reviews=[]), calls)
    asyncio.run(review_claims([_result(verdict="Refuted")], [_source(_KREDIT)], llm))
    assert len(calls) == 2


def test_the_prompt_spells_out_the_readings_that_split_the_two_runs():
    # 2026-10-12, t1024a vs t1024b: the model read "kewajiban neto menurun" off −223 → −197 as a
    # rise, "sub-LU tumbuh (SBT 0,59%)" as a rise on the quarter, and could not always place
    # "IEP Oktober" in the July column of "3 bulan yang akan datang".
    from ai_review import SYSTEM
    assert "−223.049 ke −197.431" in SYSTEM
    assert "SBT" in SYSTEM and "bernilai positif" in SYSTEM
    assert "3 bulan yang akan datang" in SYSTEM and "Oktober" in SYSTEM


def test_a_cell_count_the_operation_cannot_take_is_refused_without_an_engine_error(caplog):
    # 2026-10-12 SULNI/PII: the model cited three cells for a ratio (two numerators over a total);
    # _compute_ratio raised, the error was contained, and every such answer logged a traceback.
    review = _review(operation="ratio", cells=[
        CitedCell(sheet="S1", row="Kredit Modal Kerja", period="Jun 2026", value=110.0),
        CitedCell(sheet="S1", row="Kredit Konsumsi", period="Jun 2026", value=55.0),
        CitedCell(sheet="S1", row="Kredit Modal Kerja", period="Jun 2025", value=100.0),
    ])
    original = _result(verdict="Refuted", operation="ratio", claimed_value=66.7, unit="persen")
    with caplog.at_level("ERROR"):
        assert judge_review(original, review, _CODES) == (original, "unchanged")
    assert not [r for r in caplog.records if r.levelname == "ERROR"]


def test_raw_answers_go_to_the_log_file_only_when_one_is_named(tmp_path, monkeypatch):
    # For diagnosing a review that went wrong; the verdicts themselves are unchanged and the
    # raw answer is never put in the result the user sees.
    import json

    results = [_result(verdict="Entailed"), _result(verdict="Refuted"), _result()]
    llm = _llm_answering(lambda n: ReviewBatch(reviews=[_review(claim_no=1)]), [])

    monkeypatch.delenv("AI_REVIEW_RAW_LOG", raising=False)
    plain, _ = asyncio.run(review_claims(results, [_source(_KREDIT)], llm, batch_size=1))

    log = tmp_path / "raw" / "doc.jsonl"
    monkeypatch.setenv("AI_REVIEW_RAW_LOG", str(log))
    logged, _ = asyncio.run(review_claims(results, [_source(_KREDIT)], llm, batch_size=1))

    assert [r.model_dump() for r in logged] == [r.model_dump() for r in plain]
    lines = [json.loads(line) for line in log.read_text(encoding="utf-8").splitlines()]
    assert [line["index"] for line in lines] == [1, 2]
    assert lines[0]["verdict_before"] == "Refuted" and lines[0]["outcome"] == "to_entailed"
    assert lines[0]["review"]["claim_no"] == 1


_DPK = _table(title="Tabel3", unit="SBT, %",
              data={("Gabungan > Deposito", 2026, "Q1"): 73.81, ("Gabungan > Deposito", 2026, "Q2"): 72.70})
_GROWTH_QUOTE = ("Hal ini terutama didukung oleh perkiraan pertumbuhan seluruh kategori DPK yaitu "
                 "Tabungan (82,47%), Giro (SBT 80,36%), dan Deposito (72,70%).")


def test_an_above_zero_answer_on_a_net_balance_is_accepted_without_a_stated_bound():
    # SBank Tw I-2026: the AI read "pertumbuhan" as a positive balance and answered above_threshold
    # on 72,70, but the claim (is_increasing) had no number, so the engine had no bound to check.
    result = _result(verdict="Refuted", operation="is_increasing", claimed_value=None, unit=None,
                     quote=_GROWTH_QUOTE, label="Deposito")
    review = _review(first_words="Hal ini terutama didukung", subject="Deposito",
                     operation="above_threshold",
                     cells=[CitedCell(sheet="S1", row="Gabungan > Deposito", period="Q2 2026", value=72.70)])

    final, outcome = judge_review(result, review, {"S1": _source(_DPK, sheet="Tabel3")})

    assert (outcome, final.verdict) == ("to_entailed", "Entailed")


def test_a_bound_is_still_needed_off_a_net_balance():
    kredit = _table(unit="Miliar Rp", data={("Kredit Modal Kerja", 2026, "Jun"): 110.0})
    result = _result(verdict="Refuted", operation="is_increasing", claimed_value=None, unit=None)
    review = _review(operation="above_threshold")

    final, outcome = judge_review(result, review, {"S1": _source(kredit)})

    assert outcome == "unchanged"


def test_a_value_answer_for_a_claim_without_a_number_does_not_crash():
    result = _result(verdict="Refuted", operation="is_increasing", claimed_value=None, unit=None,
                     quote=_GROWTH_QUOTE, label="Deposito")
    review = _review(first_words="Hal ini terutama didukung", subject="Deposito", operation="value",
                     cells=[CitedCell(sheet="S1", row="Gabungan > Deposito", period="Q2 2026", value=72.70)])

    final, outcome = judge_review(result, review, {"S1": _source(_DPK, sheet="Tabel3")})

    assert outcome == "unchanged"


# AI_REVIEW_SECOND_LOOK: reports are usually right, so a Tidak Sesuai earns a search for another
# reasonable reading first; and the engine accepts the sign the sentence states ("turun 4,1%").
_SPE = _table(title="Pertumbuhan Indeks Penjualan Riil", unit="%, mtm",
              data={("INDEKS TOTAL", 2025, "Jul"): -4.1, ("INDEKS TOTAL", 2026, "Jul"): 0.7})
_SPE_QUOTE = "Meski demikian, kinerja penjualan pada Juli 2026 tersebut lebih baik dibandingkan Juli 2025 yang turun 4,1% (mtm)."


def _spe_claim(quote=_SPE_QUOTE):
    return _result(verdict="Refuted", claimed_value=4.1, unit="persen", quote=quote, label="penjualan")


def _spe_review(quote=_SPE_QUOTE):
    return _review(first_words=" ".join(quote.split()[:5]), subject="kinerja penjualan",
                   word_check="penjualan: ada (judul)",
                   cells=[CitedCell(sheet="S1", row="INDEKS TOTAL", period="Jul 2025", value=-4.1)])


def test_second_look_accepts_the_sign_the_sentence_states(monkeypatch):
    # SPE Juni 2026: the review cited Juli 2025's -4,1 and said sesuai; the engine, rebuilding the
    # claim as +4,1, could not reproduce it and the false Tidak Sesuai stood.
    # The engine itself now reads "turun 4,1%" as −4,1 (paired_verifier._fall_before_rate), so the
    # cited cell is accepted with or without the second look.
    codes = {"S1": _source(_SPE)}
    for flag in ("", "1"):
        monkeypatch.setenv("AI_REVIEW_SECOND_LOOK", flag)
        final, outcome = judge_review(_spe_claim(), _spe_review(), codes)
        assert outcome == "to_entailed" and final.verdict == "Entailed"


def test_second_look_leaves_the_sign_alone_when_the_sentence_says_a_rise(monkeypatch):
    monkeypatch.setenv("AI_REVIEW_SECOND_LOOK", "1")
    quote = "Meski demikian, kinerja penjualan pada Juli 2025 naik 4,1% (mtm)."
    final, outcome = judge_review(_spe_claim(quote), _spe_review(quote), {"S1": _source(_SPE)})
    assert outcome == "unchanged" and final.verdict == "Refuted"


def test_second_look_tells_the_model_reports_are_usually_right(monkeypatch):
    results = [_result(verdict="Refuted")]
    for flag, expected in (("", False), ("1", True)):
        monkeypatch.setenv("AI_REVIEW_SECOND_LOOK", flag)
        calls = []
        llm = _llm_answering(lambda n: ReviewBatch(reviews=[_review(claim_no=1)]), calls)
        asyncio.run(review_claims(results, [_source(_KREDIT)], llm))
        assert ("hampir selalu benar" in calls[0]) is expected
        assert ("hasil program: Tidak Sesuai" in calls[0]) is expected
