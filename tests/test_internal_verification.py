"""Internal verification: claims checked against the tables printed inside the same PDF.

Covers the two pieces that mode adds to the pipeline — PDF-transcribed tables used as
reference sources, and multi-source conflict detection (which also serves "both" mode's
Excel-vs-PDF comparison).
"""

import asyncio
from unittest.mock import Mock, patch

import pytest

from excel_parser_bi import BITableData
from paired_verifier import _ExcelSource, _evaluate_fact, verify_paired
from pdf_table_extraction import PdfTable, _PdfTableOut, _assemble_grid
from structured_extractor import ExtractedFact, PeriodPoint


def _make_table(title="Uang Beredar (M2)", unit="triliun Rp", data=None):
    table = BITableData(title=title, unit=unit, row_labels=[])
    for (label, year, month), value in (data or {}).items():
        if label not in table.row_labels:
            table.row_labels.append(label)
        table._data[(label, year, month)] = value
    return table


def _excel_source(table, filename="TABEL1_1.xls", sheet="I.1"):
    return _ExcelSource(table=table, filename=filename, sheet=sheet)


def _pdf_source(table, page=7, caption="Lampiran 1. Uang Beredar"):
    return _ExcelSource(
        table=table, filename="M2-April-2026.pdf",
        sheet=f"Hal. {page} · {caption}", origin="pdf",
    )


def _make_period(**overrides):
    base = dict(metric_label="Total", year=2026, month="Apr")
    base.update(overrides)
    return PeriodPoint(**base)


def _make_fact(**overrides):
    periods = overrides.pop("periods", None) or [_make_period()]
    base = dict(
        operation="value", periods=periods, claimed_value=10355.1,
        unit="triliun Rp", context_quote="quote", page_number=1,
    )
    base.update(overrides)
    return ExtractedFact(**base)


def _facts_returning(facts):
    """Patch for extract_structured_facts_async — an awaitable yielding `facts`."""
    return patch(
        "paired_verifier.extract_structured_facts_async",
        new=Mock(side_effect=lambda *a, **k: asyncio.sleep(0, result=list(facts))),
    )


def _internal_pdf_table(page=7, caption="Lampiran 1. Uang Beredar", apr="10.355,1", unit="(Triliun Rp)"):
    out = _PdfTableOut(
        caption=caption, unit=unit,
        header_rows=[["", "2026", "2026"], ["", "Mar", "Apr"]],
        rows=[["Total", "9.900,0", apr], ["Uang Kuasi", "4.100,0", "4.200,0"]],
    )
    return PdfTable(
        page_number=page, caption=caption, unit=(unit or "").strip("()"),
        grid=_assemble_grid(out),
    )


# ---------------------------------------------------------------------------
# Conflict detection in _evaluate_fact
# ---------------------------------------------------------------------------

def test_two_pdf_sources_that_disagree_are_flagged_as_an_internal_conflict():
    # The snippet table on page 2 and the Lampiran on page 7 give different numbers for the
    # same series: the report contradicts itself.
    snippet = _make_table(data={("Total", 2026, "Apr"): 10355.1})
    lampiran = _make_table(data={("Total", 2026, "Apr"): 10999.9})
    result = _evaluate_fact(
        _make_fact(),
        [_pdf_source(snippet, page=2, caption="Tabel 2. Uang Beredar"), _pdf_source(lampiran)],
    )

    assert result.source_conflict == "internal"
    assert len(result.source_values) == 2
    assert [sv.origin for sv in result.source_values] == ["pdf", "pdf"]
    assert result.source_values[0].computed_value == pytest.approx(10355.1)
    assert result.source_values[1].computed_value == pytest.approx(10999.9)
    # The headline verdict still comes from the best-matching source: a conflict says the
    # SOURCES disagree, not that the claim is wrong.
    assert result.verdict == "Entailed"
    assert "KONFLIK SUMBER" in result.reasoning


def test_excel_and_pdf_sources_that_disagree_are_flagged_as_a_cross_conflict():
    excel = _make_table(data={("Total", 2026, "Apr"): 10355.1})
    from_pdf = _make_table(data={("Total", 2026, "Apr"): 10999.9})
    result = _evaluate_fact(_make_fact(), [_excel_source(excel), _pdf_source(from_pdf)])

    assert result.source_conflict == "cross"
    assert [sv.origin for sv in result.source_values] == ["excel", "pdf"]
    # Excel is listed first on purpose (verify_paired step 1b), so its value stays the
    # headline and today's numbers do not move when internal mode is switched on.
    assert result.matched_excel_source == "TABEL1_1.xls / I.1"


def test_sources_that_agree_within_tolerance_raise_no_conflict():
    a = _make_table(data={("Total", 2026, "Apr"): 10355.1})
    b = _make_table(data={("Total", 2026, "Apr"): 10355.13})
    result = _evaluate_fact(_make_fact(), [_pdf_source(a), _pdf_source(b, page=8)])

    assert result.source_conflict is None
    assert len(result.source_values) == 2, "both sources are still reported"


def test_sources_in_different_units_are_never_treated_as_conflicting():
    # THE false-positive guard. A BI report states the same series twice, Lampiran 1 in
    # 'Triliun Rp' and Lampiran 2 in percent, under IDENTICAL row labels. Without the unit
    # gate almost every M2 claim would be flagged (10.415,9 vs 10,8).
    levels = _make_table(unit="triliun Rp", data={("Total", 2026, "Apr"): 10355.1})
    growth = _make_table(unit="persen", data={("Total", 2026, "Apr"): 9.7})
    result = _evaluate_fact(
        _make_fact(claimed_value=10355.1, unit="triliun Rp"),
        [_pdf_source(levels), _pdf_source(growth, page=8, caption="Lampiran 2. Pertumbuhan")],
    )

    assert result.source_conflict is None
    assert result.verdict == "Entailed"


def test_a_source_that_cannot_reach_a_verdict_never_raises_a_conflict():
    # Measured on the real M2 report: the snippet table on page 1 has too few columns to
    # compute a YoY growth, while the Lampiran does. That is missing data in one table, not a
    # disagreement between two — and it was the single biggest source of false conflicts.
    thin = _make_table(unit="triliun Rp", data={("Total", 2026, "Apr"): 10355.1})
    full = _make_table(unit="triliun Rp", data={
        ("Total", 2026, "Apr"): 10355.1, ("Total", 2025, "Apr"): 9000.0,
    })
    fact = _make_fact(operation="yoy_growth", claimed_value=15.1, unit="persen_yoy")
    result = _evaluate_fact(fact, [_pdf_source(thin, page=1), _pdf_source(full)])

    assert result.verdict == "Inconclusive"
    assert result.source_conflict is None


def test_a_levels_table_and_a_growth_table_are_not_compared():
    # Both hold 'Total' at Apr 2026, but one in rupiah and one already in %-yoy. Computing YoY
    # off each gives 15,1 vs 142,9 — an artefact of the operation, not a contradiction.
    levels = _make_table(unit="triliun Rp", data={
        ("Total", 2026, "Apr"): 10355.1, ("Total", 2025, "Apr"): 9000.0,
    })
    growth = _make_table(unit="%, yoy", data={
        ("Total", 2026, "Apr"): 17.0, ("Total", 2025, "Apr"): 7.0,
    })
    fact = _make_fact(operation="yoy_growth", claimed_value=15.1, unit="persen_yoy")
    result = _evaluate_fact(fact, [_pdf_source(levels), _pdf_source(growth, page=8)])

    assert result.source_conflict is None


def test_a_looser_label_match_cannot_contradict_a_better_one():
    # Measured on the real report: a claim about "Kredit" fuzzy-matches the aggregate table's
    # "Kredit" row AND a breakdown table's "Kredit Properti" row. Those hold different numbers
    # by design — the breakdown is a component, not a competing measurement of the same series.
    aggregate = _make_table(unit="triliun Rp", data={("Kredit", 2026, "Apr"): 8759.0})
    breakdown = _make_table(unit="triliun Rp", data={("Kredit Properti", 2026, "Apr"): 1690.8})
    fact = _make_fact(
        claimed_value=8759.0, periods=[_make_period(metric_label="Kredit")],
    )
    result = _evaluate_fact(fact, [_pdf_source(aggregate), _pdf_source(breakdown, page=4)])

    assert result.verdict == "Entailed"
    assert result.source_conflict is None


def test_two_conclusive_sources_in_the_same_unit_still_conflict():
    # The signal the two guards above must not suppress.
    a = _make_table(unit="triliun Rp", data={("Total", 2026, "Apr"): 10355.1})
    b = _make_table(unit="triliun Rp", data={("Total", 2026, "Apr"): 11999.9})
    result = _evaluate_fact(_make_fact(), [_pdf_source(a), _pdf_source(b, page=8)])

    assert result.source_conflict == "internal"
    assert {sv.verdict for sv in result.source_values} == {"Entailed", "Refuted"}


def test_a_source_with_an_unknown_unit_does_not_raise_a_conflict():
    known = _make_table(unit="triliun Rp", data={("Total", 2026, "Apr"): 10355.1})
    unknown = _make_table(unit="", data={("Total", 2026, "Apr"): 11999.9})
    result = _evaluate_fact(_make_fact(), [_pdf_source(known), _pdf_source(unknown, page=8)])

    assert result.source_conflict is None
    assert len(result.source_values) == 2, "it is still reported, just not as a contradiction"


def test_trend_claims_conflict_when_the_per_source_verdicts_differ():
    # Trend operations compute no value, so their sources disagree when their verdicts do.
    rising = _make_table(data={("Total", 2026, "Mar"): 100.0, ("Total", 2026, "Apr"): 110.0})
    falling = _make_table(data={("Total", 2026, "Mar"): 100.0, ("Total", 2026, "Apr"): 90.0})
    fact = _make_fact(
        operation="is_increasing", claimed_value=None,
        periods=[_make_period(month="Mar"), _make_period(month="Apr")],
    )
    result = _evaluate_fact(fact, [_pdf_source(rising), _pdf_source(falling, page=8)])

    assert result.source_conflict == "internal"
    assert {sv.verdict for sv in result.source_values} == {"Entailed", "Refuted"}


def test_single_source_results_carry_no_source_comparison():
    # Regression guard: the single-source path must stay identical to before, since that is
    # every existing caller plus the whole eval harness.
    result = _evaluate_fact(
        _make_fact(), [_excel_source(_make_table(data={("Total", 2026, "Apr"): 10355.1}))]
    )

    assert result.source_values == []
    assert result.source_conflict is None
    assert "KONFLIK" not in result.reasoning


def test_unresolvable_claim_is_inconclusive_even_with_several_sources():
    a = _make_table(data={("Lain", 2026, "Apr"): 1.0})
    b = _make_table(data={("Lain", 2025, "Apr"): 2.0})
    result = _evaluate_fact(
        _make_fact(periods=[_make_period(metric_label="Metrik Yang Tidak Ada")]),
        [_pdf_source(a), _pdf_source(b, page=8)],
    )

    assert result.verdict == "Inconclusive"
    assert result.source_values == []
    assert result.source_conflict is None


# ---------------------------------------------------------------------------
# verify_paired with PDF-internal tables
# ---------------------------------------------------------------------------

def test_internal_mode_uses_pdf_tables_and_skips_table_suggestions():
    with _facts_returning([_make_fact(claimed_value=10355.1, unit="triliun Rp")]):
        response = asyncio.run(verify_paired(
            narrative_text="[== Halaman 1 ==]\nteks",
            excel_sources=[], llm=Mock(), pdf_filename="M2-April-2026.pdf",
            pdf_tables=[_internal_pdf_table()], mode="internal",
        ))

    assert response.mode == "internal"
    assert response.excel_parsers == ["pdf-generic"]
    assert response.excel_filenames == ["M2-April-2026.pdf"]
    assert response.excel_sheets[0].startswith("Hal. 7 · Lampiran 1.")
    assert response.excel_units == ["Triliun Rp"]
    assert response.entailed_count == 1
    # The BI table-family hints tell the user which workbook to upload: noise in this mode.
    assert response.table_suggestions == []


def test_internal_mode_reports_a_conflict_between_two_pdf_tables():
    tables = [
        _internal_pdf_table(page=2, caption="Tabel 2. Uang Beredar", apr="10.355,1"),
        _internal_pdf_table(page=7, caption="Lampiran 1. Uang Beredar", apr="10.999,9"),
    ]
    with _facts_returning([_make_fact(claimed_value=10355.1, unit="triliun Rp")]):
        response = asyncio.run(verify_paired(
            narrative_text="[== Halaman 1 ==]\nteks",
            excel_sources=[], llm=Mock(), pdf_filename="M2-April-2026.pdf",
            pdf_tables=tables, mode="internal",
        ))

    assert response.conflict_count == 1
    assert response.results[0].source_conflict == "internal"
    # Counts still sum to the total: a conflict is not a fourth verdict.
    assert (response.entailed_count + response.refuted_count
            + response.inconclusive_count) == response.total_facts


def test_unparseable_pdf_table_degrades_to_pointer_only_instead_of_failing():
    junk = PdfTable(page_number=3, caption="Grafik 1", unit="", grid=[["only"], ["text"]])
    with _facts_returning([]):
        response = asyncio.run(verify_paired(
            narrative_text="[== Halaman 1 ==]\nteks", excel_sources=[], llm=None,
            pdf_filename="report.pdf", pdf_tables=[junk], mode="internal",
        ))

    assert response.excel_parsers == ["pdf-pointer-only"]


def test_excel_mode_response_is_unchanged_by_the_new_fields():
    with _facts_returning([]):
        response = asyncio.run(verify_paired(
            narrative_text="[== Halaman 1 ==]\nteks", excel_sources=[], llm=Mock(),
            pdf_filename="report.pdf",
        ))

    assert response.mode == "excel"
    assert response.conflict_count == 0
