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
