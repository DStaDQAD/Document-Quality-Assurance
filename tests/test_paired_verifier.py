import asyncio
from unittest.mock import Mock, patch

import pytest

from cell_pointer import _BatchCellPointers, _CellPointer
from excel_parser_bi import BITableData
from langchain_core.runnables import RunnableLambda
from paired_verifier import (
    _ExcelSource,
    _build_table_suggestions,
    _deduplicate_facts,
    _evaluate_fact,
    _parse_scale_unit,
    _parse_table_with_fallback,
    _pointer_pass,
    _unit_factor,
    verify_paired,
)
from schemas import FactVerificationResult
from structured_extractor import ExtractedFact, PeriodPoint


def _make_table(title="Uang Beredar (M2)", unit="triliun Rp", data=None):
    """data: {(row_label, year, month): value}"""
    table = BITableData(title=title, unit=unit, row_labels=[])
    for (label, year, month), value in (data or {}).items():
        if label not in table.row_labels:
            table.row_labels.append(label)
        table._data[(label, year, month)] = value
    return table


def _make_source(table, filename="TABEL1_1.xls", sheet="I.1"):
    return _ExcelSource(table=table, filename=filename, sheet=sheet)


def _make_period(**overrides):
    base = dict(metric_label="Total", year=2026, month="Apr")
    base.update(overrides)
    return PeriodPoint(**base)


def _make_fact(**overrides):
    periods = overrides.pop("periods", None) or [_make_period()]
    base = dict(
        operation="value",
        periods=periods,
        claimed_value=10355.1,
        unit="triliun Rp",
        context_quote="quote",
        page_number=1,
    )
    base.update(overrides)
    return ExtractedFact(**base)


# ---------------------------------------------------------------------------
# _unit_factor
# ---------------------------------------------------------------------------

def test_unit_factor_returns_none_for_unknown_pair():
    assert _unit_factor("foo", "bar") is None


def test_unit_factor_is_case_and_whitespace_insensitive():
    assert _unit_factor(" Triliun Rp ", "Miliar Rp") == 1000.0


def test_unit_factor_identical_units_need_no_conversion():
    assert _unit_factor("juta Rp", "juta Rp") == 1.0


def test_unit_factor_derives_ratio_for_unlisted_same_currency_pair():
    # ('ribu Rp', 'miliar Rp') is not in the explicit table — the scale-word fallback
    # must derive pdf * factor = excel, i.e. 1e3 / 1e9.
    assert _unit_factor("ribu Rp", "miliar Rp") == pytest.approx(1e-6)
    assert _unit_factor("Rp", "juta Rp") == pytest.approx(1e-6)


def test_unit_factor_does_not_cross_currencies():
    assert _unit_factor("juta Rp", "juta USD") is None


@pytest.mark.parametrize(
    "unit,expected",
    [
        ("juta Rp", (1e6, "rp")),
        ("Rp", (1.0, "rp")),
        ("miliar dolar AS", (1e9, "usd")),
        ("miliar", (1e9, None)),
        ("persen", None),
        ("persen_yoy", None),
        ("%", None),
        ("unit", None),
    ],
)
def test_parse_scale_unit(unit, expected):
    assert _parse_scale_unit(unit) == expected


# ---------------------------------------------------------------------------
# _evaluate_fact — operation="value"
# ---------------------------------------------------------------------------

def test_evaluate_value_entailed_within_tolerance():
    table = _make_table(data={("Total", 2026, "Apr"): 10355.1})
    fact = _make_fact()

    result = _evaluate_fact(fact, [_make_source(table)])

    assert result.verdict == "Entailed"
    assert result.periods[0].metric_label == "Total"
    assert result.delta == 0.0


def test_evaluate_value_refuted_outside_tolerance():
    table = _make_table(data={("Total", 2026, "Apr"): 10000.0})
    fact = _make_fact(claimed_value=10355.1)

    result = _evaluate_fact(fact, [_make_source(table)])

    assert result.verdict == "Refuted"
    assert result.delta == pytest.approx(355.1)


def test_evaluate_value_converts_units_between_pdf_and_excel():
    table = _make_table(unit="miliar Rp", data={("Total", 2026, "Apr"): 10355100.0})
    fact = _make_fact(unit="triliun Rp", claimed_value=10355.1)

    result = _evaluate_fact(fact, [_make_source(table)])

    assert result.verdict == "Entailed"
    assert result.computed_value == pytest.approx(10355.1)


def test_evaluate_value_inconclusive_when_metric_not_found():
    table = _make_table(data={("Other", 2026, "Apr"): 1.0})
    fact = _make_fact(periods=[_make_period(metric_label="Nonexistent Metric")])

    result = _evaluate_fact(fact, [_make_source(table)])

    assert result.verdict == "Inconclusive"
    assert "Nonexistent Metric" in result.reasoning


def test_evaluate_value_inconclusive_when_units_incompatible():
    table = _make_table(unit="juta USD", data={("Total", 2026, "Apr"): 100.0})
    fact = _make_fact(unit="triliun Rp")

    result = _evaluate_fact(fact, [_make_source(table)])

    assert result.verdict == "Inconclusive"
    assert "not supported" in result.reasoning


def test_evaluate_value_uses_first_matching_source_in_order():
    table_without_match = _make_table(data={})
    table_with_match = _make_table(data={("Total", 2026, "Apr"): 10355.1})
    fact = _make_fact()

    result = _evaluate_fact(
        fact,
        [_make_source(table_without_match, filename="a.xls"), _make_source(table_with_match, filename="b.xls")],
    )

    assert result.verdict == "Entailed"
    assert result.matched_excel_source == "b.xls / I.1"


# ---------------------------------------------------------------------------
# _evaluate_fact — operation="yoy_growth"
# ---------------------------------------------------------------------------

def test_evaluate_yoy_growth_entailed():
    table = _make_table(data={("Total", 2026, "Apr"): 110.0, ("Total", 2025, "Apr"): 100.0})
    fact = _make_fact(operation="yoy_growth", claimed_value=10.0, unit="persen_yoy")

    result = _evaluate_fact(fact, [_make_source(table)])

    assert result.verdict == "Entailed"
    assert result.computed_value == pytest.approx(10.0)
    assert len(result.periods) == 2


def test_evaluate_yoy_growth_inconclusive_when_prior_year_missing():
    table = _make_table(data={("Total", 2026, "Apr"): 110.0})
    fact = _make_fact(operation="yoy_growth", claimed_value=10.0)

    result = _evaluate_fact(fact, [_make_source(table)])

    assert result.verdict == "Inconclusive"
    assert "yoy denominator" in result.reasoning


def test_evaluate_yoy_growth_inconclusive_when_prior_year_zero():
    table = _make_table(data={("Total", 2026, "Apr"): 110.0, ("Total", 2025, "Apr"): 0.0})
    fact = _make_fact(operation="yoy_growth", claimed_value=10.0)

    result = _evaluate_fact(fact, [_make_source(table)])

    assert result.verdict == "Inconclusive"
    assert "undefined" in result.reasoning


def test_evaluate_yoy_growth_inconclusive_when_metric_not_found_in_any_source():
    table = _make_table(data={})
    fact = _make_fact(
        operation="yoy_growth", periods=[_make_period(metric_label="Nonexistent Metric")], claimed_value=10.0
    )

    result = _evaluate_fact(fact, [_make_source(table)])

    assert result.verdict == "Inconclusive"
    assert "Nonexistent Metric" in result.reasoning


# ---------------------------------------------------------------------------
# _evaluate_fact — operation="average" / "sum"
# ---------------------------------------------------------------------------

def _range_periods(months, metric="Total", year=2026):
    return [_make_period(metric_label=metric, year=year, month=m) for m in months]


def test_evaluate_average_entailed():
    table = _make_table(data={
        ("Total", 2026, "Jan"): 100.0, ("Total", 2026, "Feb"): 105.0,
        ("Total", 2026, "Mar"): 98.0, ("Total", 2026, "Apr"): 110.0,
    })
    fact = _make_fact(operation="average", periods=_range_periods(["Jan", "Feb", "Mar", "Apr"]), claimed_value=103.25)

    result = _evaluate_fact(fact, [_make_source(table)])

    assert result.verdict == "Entailed"
    assert result.computed_value == pytest.approx(103.25)
    assert len(result.periods) == 4


def test_evaluate_average_refuted():
    table = _make_table(data={
        ("Total", 2026, "Jan"): 100.0, ("Total", 2026, "Feb"): 105.0,
        ("Total", 2026, "Mar"): 98.0, ("Total", 2026, "Apr"): 110.0,
    })
    fact = _make_fact(operation="average", periods=_range_periods(["Jan", "Feb", "Mar", "Apr"]), claimed_value=110.0)

    result = _evaluate_fact(fact, [_make_source(table)])

    assert result.verdict == "Refuted"


def test_evaluate_average_inconclusive_when_one_month_missing():
    table = _make_table(data={
        ("Total", 2026, "Jan"): 100.0, ("Total", 2026, "Feb"): 105.0, ("Total", 2026, "Mar"): 98.0,
    })
    fact = _make_fact(operation="average", periods=_range_periods(["Jan", "Feb", "Mar", "Apr"]), claimed_value=103.25)

    result = _evaluate_fact(fact, [_make_source(table)])

    assert result.verdict == "Inconclusive"
    assert "Total Apr 2026" in result.reasoning


def test_evaluate_sum_entailed():
    table = _make_table(data={("Total", 2026, "Jan"): 100.0, ("Total", 2026, "Feb"): 100.0})
    fact = _make_fact(operation="sum", periods=_range_periods(["Jan", "Feb"]), claimed_value=200.0)

    result = _evaluate_fact(fact, [_make_source(table)])

    assert result.verdict == "Entailed"
    assert result.computed_value == pytest.approx(200.0)


# ---------------------------------------------------------------------------
# _evaluate_fact — operation="diff"
# ---------------------------------------------------------------------------

def test_evaluate_diff_entailed_later_minus_earlier():
    table = _make_table(data={("Total", 2026, "Jan"): 100.0, ("Total", 2026, "Apr"): 150.0})
    fact = _make_fact(operation="diff", periods=_range_periods(["Jan", "Apr"]), claimed_value=50.0)

    result = _evaluate_fact(fact, [_make_source(table)])

    assert result.verdict == "Entailed"
    assert result.computed_value == pytest.approx(50.0)


# ---------------------------------------------------------------------------
# _evaluate_fact — operation="ratio"
# ---------------------------------------------------------------------------

def test_evaluate_ratio_entailed_as_percentage():
    table = _make_table(data={("Kredit", 2026, "Apr"): 850.0, ("DPK", 2026, "Apr"): 1000.0})
    fact = _make_fact(
        operation="ratio",
        periods=[_make_period(metric_label="Kredit"), _make_period(metric_label="DPK")],
        claimed_value=85.0,
        unit="persen",
    )

    result = _evaluate_fact(fact, [_make_source(table)])

    assert result.verdict == "Entailed"
    assert result.computed_value == pytest.approx(85.0)


def test_evaluate_ratio_inconclusive_when_denominator_zero():
    table = _make_table(data={("Kredit", 2026, "Apr"): 850.0, ("DPK", 2026, "Apr"): 0.0})
    fact = _make_fact(
        operation="ratio",
        periods=[_make_period(metric_label="Kredit"), _make_period(metric_label="DPK")],
        claimed_value=85.0,
        unit="persen",
    )

    result = _evaluate_fact(fact, [_make_source(table)])

    assert result.verdict == "Inconclusive"
    assert "tidak terdefinisi" in result.reasoning


# ---------------------------------------------------------------------------
# _evaluate_fact — operation="is_increasing" / "is_decreasing" / "is_stable"
# ---------------------------------------------------------------------------

def test_evaluate_is_increasing_entailed_when_strictly_increasing():
    table = _make_table(data={
        ("Total", 2026, "Jan"): 100.0, ("Total", 2026, "Feb"): 105.0, ("Total", 2026, "Mar"): 110.0,
    })
    fact = _make_fact(
        operation="is_increasing", periods=_range_periods(["Jan", "Feb", "Mar"]),
        claimed_value=None, unit=None,
    )

    result = _evaluate_fact(fact, [_make_source(table)])

    assert result.verdict == "Entailed"
    assert result.claimed_value is None


def test_evaluate_is_decreasing_refuted_when_actually_increasing():
    table = _make_table(data={
        ("Total", 2026, "Jan"): 100.0, ("Total", 2026, "Feb"): 105.0, ("Total", 2026, "Mar"): 110.0,
    })
    fact = _make_fact(
        operation="is_decreasing", periods=_range_periods(["Jan", "Feb", "Mar"]),
        claimed_value=None, unit=None,
    )

    result = _evaluate_fact(fact, [_make_source(table)])

    assert result.verdict == "Refuted"


def test_evaluate_is_stable_entailed_when_within_tolerance():
    table = _make_table(data={
        ("Total", 2026, "Jan"): 100.0, ("Total", 2026, "Feb"): 100.02, ("Total", 2026, "Mar"): 100.0,
    })
    fact = _make_fact(
        operation="is_stable", periods=_range_periods(["Jan", "Feb", "Mar"]),
        claimed_value=None, unit=None,
    )

    result = _evaluate_fact(fact, [_make_source(table)])

    assert result.verdict == "Entailed"


# ---------------------------------------------------------------------------
# _deduplicate_facts
# ---------------------------------------------------------------------------

def test_deduplicate_facts_keeps_first_occurrence_for_same_key():
    f1 = _make_fact(context_quote="first")
    f2 = _make_fact(context_quote="second")  # same (operation, periods)
    f3 = _make_fact(periods=[_make_period(month="May")], context_quote="third")

    result = _deduplicate_facts([f1, f2, f3])

    assert len(result) == 2
    assert result[0].context_quote == "first"
    assert result[1].periods[0].month == "May"


# ---------------------------------------------------------------------------
# _build_table_suggestions
# ---------------------------------------------------------------------------

def _make_result(metric_label, verdict="Inconclusive", matched_source=None):
    return FactVerificationResult(
        operation="yoy_growth",
        metric_label=metric_label,
        matched_excel_source=matched_source,
        verdict=verdict,
        reasoning="",
        context_quote="q",
    )


def test_build_table_suggestions_groups_unfound_metrics_by_family():
    results = [
        _make_result("M0 adjusted"),
        _make_result("uang kartal yang diedarkan"),
        _make_result("Penghimpunan DPK"),
        _make_result("Kredit Modal Kerja (KMK)"),
        _make_result("kredit kendaraan bermotor"),
        _make_result("metrik tak dikenal sama sekali"),  # no family → no suggestion
    ]

    suggestions = _build_table_suggestions(results)

    by_table = {s.table: s.metrics for s in suggestions}
    assert len(by_table) == 3
    m0 = next(t for t in by_table if "Uang Primer" in t)
    dpk = next(t for t in by_table if "DPK" in t)
    kredit = next(t for t in by_table if "Kredit" in t)
    assert by_table[m0] == ["M0 adjusted", "uang kartal yang diedarkan"]
    assert by_table[dpk] == ["Penghimpunan DPK"]
    assert by_table[kredit] == ["Kredit Modal Kerja (KMK)", "kredit kendaraan bermotor"]


def test_build_table_suggestions_skips_non_inconclusive_and_sourced_results():
    results = [
        _make_result("Kredit Modal Kerja (KMK)", verdict="Entailed"),
        # Inconclusive but a source WAS found (e.g. unit mismatch) → data isn't missing.
        _make_result("penyaluran kredit", matched_source="TABEL1_1.xls / I.1"),
    ]

    assert _build_table_suggestions(results) == []


def test_build_table_suggestions_deduplicates_repeated_metrics():
    results = [_make_result("Penghimpunan DPK"), _make_result("Penghimpunan DPK")]

    suggestions = _build_table_suggestions(results)

    assert len(suggestions) == 1
    assert suggestions[0].metrics == ["Penghimpunan DPK"]


# ---------------------------------------------------------------------------
# verify_paired (end-to-end orchestration, deps mocked)
# ---------------------------------------------------------------------------

# Note: PDF extraction / vision fallback is no longer verify_paired's concern - it now takes
# already-extracted narrative_text (see pdf_extraction.extract_narrative_text, tested in
# tests/test_pdf_extraction.py) so that fact-verification and typo_checker.check_typos can share
# one extraction pass instead of each re-running it.

@patch("paired_verifier.extract_structured_facts_async")
@patch("paired_verifier.parse_bi_table")
def test_verify_paired_end_to_end_produces_entailed_verdict(mock_parse, mock_extract_facts):
    table = _make_table(data={("Total", 2026, "Apr"): 10355.1})
    mock_parse.return_value = table
    mock_extract_facts.return_value = [_make_fact()]

    response = asyncio.run(
        verify_paired(
            narrative_text="[== Halaman 1 ==]\n" + "x" * 250,
            excel_sources=[(b"xls-bytes", "I.1", "TABEL1_1.xls")],
            llm=Mock(),
        )
    )

    assert response.total_facts == 1
    assert response.entailed_count == 1
    assert response.results[0].verdict == "Entailed"
    assert response.excel_filenames == ["TABEL1_1.xls"]
    mock_parse.assert_called_once_with(b"xls-bytes", "I.1")


@patch("paired_verifier.extract_structured_facts_async")
@patch("paired_verifier.parse_bi_table")
def test_verify_paired_returns_no_facts_when_narrative_has_nothing_extractable(mock_parse, mock_extract_facts):
    mock_parse.return_value = _make_table(data={})
    mock_extract_facts.return_value = []

    response = asyncio.run(
        verify_paired(
            narrative_text="[== Halaman 1 ==]\n" + "y" * 250,
            excel_sources=[(b"xls-bytes", "I.1", "TABEL1_1.xls")],
            llm=Mock(),
        )
    )

    assert response.total_facts == 0
    assert response.results == []


@patch("paired_verifier.extract_structured_facts_async")
@patch("paired_verifier._load_grid")
@patch("paired_verifier.parse_table_with_llm")
@patch("paired_verifier.parse_generic_table")
@patch("paired_verifier.parse_bi_table")
def test_verify_paired_keeps_unparseable_sheet_as_pointer_only_source(
    mock_bi, mock_generic, mock_llm_parse, mock_load, mock_extract_facts
):
    mock_bi.side_effect = ValueError("no year header")
    mock_generic.side_effect = ValueError("no structure")
    mock_llm_parse.side_effect = ValueError("spec rejected")
    mock_load.return_value = [["Metrik", 2026], ["KPR", 40.63]]
    mock_extract_facts.return_value = []

    response = asyncio.run(
        verify_paired(
            narrative_text="[== Halaman 1 ==]\n" + "z" * 250,
            excel_sources=[(b"weird-bytes", "S", "aneh.xlsx")],
            llm=Mock(),
        )
    )

    assert response.excel_parsers == ["pointer-only"]
    assert response.total_facts == 0


@patch("paired_verifier.extract_structured_facts_async")
@patch("paired_verifier._load_grid")
@patch("paired_verifier.parse_table_with_llm")
@patch("paired_verifier.parse_generic_table")
@patch("paired_verifier.parse_bi_table")
def test_verify_paired_still_raises_when_grid_also_unloadable(
    mock_bi, mock_generic, mock_llm_parse, mock_load, mock_extract_facts
):
    mock_bi.side_effect = ValueError("no year header")
    mock_generic.side_effect = ValueError("no structure")
    mock_llm_parse.side_effect = ValueError("spec rejected")
    mock_load.side_effect = ValueError("Unrecognized file format")
    mock_extract_facts.return_value = []

    with pytest.raises(ValueError, match="Tabel tidak dapat diparsing"):
        asyncio.run(
            verify_paired(
                narrative_text="[== Halaman 1 ==]\n" + "z" * 250,
                excel_sources=[(b"garbage", "S", "rusak.bin")],
                llm=Mock(),
            )
        )


# ---------------------------------------------------------------------------
# Categorical (non-time-series) sources
# ---------------------------------------------------------------------------

def _make_cat_table(title="Daftar Barang", unit="", data=None):
    """data: {(row_label, col_label): value}"""
    table = BITableData(title=title, unit=unit, row_labels=[], axis_type="categorical")
    for (row, col), value in (data or {}).items():
        if row not in table.row_labels:
            table.row_labels.append(row)
        if col not in table.col_labels:
            table.col_labels.append(col)
        table._data[(row, col)] = value
    return table


def _make_cat_period(**overrides):
    base = dict(metric_label="Laptop ASUS", col_label="Harga")
    base.update(overrides)
    return PeriodPoint(**base)


def test_evaluate_value_categorical_entailed():
    table = _make_cat_table(data={("Laptop ASUS", "Harga"): 7_500_000.0})
    fact = _make_fact(periods=[_make_cat_period()], claimed_value=7_500_000.0, unit="Rp")

    result = _evaluate_fact(fact, [_make_source(table, filename="barang.xlsx", sheet="S")])

    assert result.verdict == "Entailed"
    assert result.periods[0].col_label == "Harga"
    assert result.periods[0].year is None
    assert result.matched_excel_source == "barang.xlsx / S"


def test_evaluate_value_categorical_refuted_outside_tolerance():
    table = _make_cat_table(data={("Laptop ASUS", "Harga"): 7_500_000.0})
    fact = _make_fact(periods=[_make_cat_period()], claimed_value=8_000_000.0, unit="Rp")

    result = _evaluate_fact(fact, [_make_source(table)])

    assert result.verdict == "Refuted"


def test_evaluate_categorical_without_declared_source_unit_compares_raw_numbers():
    # Categorical tables usually declare no table-wide unit (it lives in column names like
    # 'Harga (Rp)') — the level-value comparison must not die on unit conversion.
    table = _make_cat_table(unit="", data={("Laptop ASUS", "Harga"): 7_500_000.0})
    fact = _make_fact(periods=[_make_cat_period()], claimed_value=7_500_000.0, unit="Rp")

    result = _evaluate_fact(fact, [_make_source(table)])

    assert result.verdict == "Entailed"


def test_evaluate_categorical_scaled_claim_against_unitless_source():
    # PDF says 'Rp7,5 juta' (claimed 7.5, unit 'juta Rp'); the source declares no unit and
    # stores base rupiah (7 500 000). The claim's own scale word must bridge the gap —
    # previously this produced a false Refuted (7.5 vs 7 500 000 raw).
    table = _make_cat_table(unit="", data={("Laptop ASUS", "Harga"): 7_500_000.0})
    fact = _make_fact(periods=[_make_cat_period()], claimed_value=7.5, unit="juta Rp")

    result = _evaluate_fact(fact, [_make_source(table)])

    assert result.verdict == "Entailed"
    assert result.computed_value == pytest.approx(7.5)


def test_evaluate_categorical_scaled_claim_still_refutes_wrong_numbers():
    table = _make_cat_table(unit="", data={("Laptop ASUS", "Harga"): 7_500_000.0})
    fact = _make_fact(periods=[_make_cat_period()], claimed_value=75.0, unit="juta Rp")

    result = _evaluate_fact(fact, [_make_source(table)])

    assert result.verdict == "Refuted"
    assert result.computed_value == pytest.approx(7.5)


def test_evaluate_categorical_uses_unit_declared_in_column_name():
    # 'Harga (juta Rp)' declares the column's unit — a claim in base 'Rp' must be
    # converted with a real unit factor, not compared raw.
    table = _make_cat_table(unit="", data={("Laptop ASUS", "Harga (juta Rp)"): 7.5})
    fact = _make_fact(
        periods=[_make_cat_period(col_label="Harga")], claimed_value=7_500_000.0, unit="Rp",
    )

    result = _evaluate_fact(fact, [_make_source(table)])

    assert result.verdict == "Entailed"
    assert result.computed_value == pytest.approx(7_500_000.0)


def test_evaluate_categorical_non_currency_unit_compares_raw():
    # 'Stok 10 unit' — no currency, no scale word: raw comparison is the only sane option.
    table = _make_cat_table(unit="", data={("Laptop ASUS", "Stok"): 10.0})
    fact = _make_fact(
        periods=[_make_cat_period(col_label="Stok")], claimed_value=10.0, unit="unit",
    )

    result = _evaluate_fact(fact, [_make_source(table)])

    assert result.verdict == "Entailed"


def test_evaluate_ratio_between_two_categorical_points():
    table = _make_cat_table(data={
        ("Laptop ASUS", "Harga"): 7_500_000.0,
        ("Mouse Logitech", "Harga"): 250_000.0,
    })
    fact = _make_fact(
        operation="ratio",
        periods=[
            _make_cat_period(metric_label="Laptop ASUS"),
            _make_cat_period(metric_label="Mouse Logitech"),
        ],
        claimed_value=30.0,
        unit=None,
    )

    result = _evaluate_fact(fact, [_make_source(table)])

    assert result.verdict == "Entailed"
    assert result.computed_value == pytest.approx(30.0)


def test_evaluate_temporal_only_operation_on_categorical_points_is_inconclusive():
    table = _make_cat_table(data={("Laptop ASUS", "Harga"): 7_500_000.0})
    fact = _make_fact(operation="yoy_growth", periods=[_make_cat_period()], claimed_value=10.0)

    result = _evaluate_fact(fact, [_make_source(table)])

    assert result.verdict == "Inconclusive"
    assert "deret waktu" in result.reasoning


def test_temporal_fact_does_not_resolve_against_categorical_source():
    table = _make_cat_table(data={("Total", "Harga"): 100.0})
    fact = _make_fact()  # temporal: Total, Apr 2026

    result = _evaluate_fact(fact, [_make_source(table)])

    assert result.verdict == "Inconclusive"
    assert "tidak ditemukan" in result.reasoning.lower()


def test_categorical_fact_skips_temporal_source_and_matches_categorical_one():
    temporal = _make_table(data={("Total", 2026, "Apr"): 10355.1})
    categorical = _make_cat_table(data={("Laptop ASUS", "Harga"): 7_500_000.0})
    fact = _make_fact(periods=[_make_cat_period()], claimed_value=7_500_000.0, unit="Rp")

    result = _evaluate_fact(
        fact,
        [_make_source(temporal, filename="a.xls"), _make_source(categorical, filename="b.xlsx")],
    )

    assert result.verdict == "Entailed"
    assert result.matched_excel_source == "b.xlsx / I.1"


# ---------------------------------------------------------------------------
# Quarterly (survey) tables — quarter tokens ride the month slot: (year, "Q2")
# ---------------------------------------------------------------------------

def _make_quarterly_table():
    return _make_table(
        title="Tabel 1. Penyaluran Kredit Baru",
        unit="",  # BI survey workbooks declare no unit row
        data={
            ("Kredit Modal Kerja", 2012, "Q2"): 92.11,
            ("Kredit Modal Kerja", 2013, "Q2"): 70.47,
        },
    )


def test_evaluate_value_on_quarterly_table_entailed():
    fact = _make_fact(
        periods=[_make_period(metric_label="Kredit Modal Kerja", year=2013, month="Q2")],
        claimed_value=70.47,
        unit="persen",
    )

    result = _evaluate_fact(fact, [_make_source(_make_quarterly_table())])

    assert result.verdict == "Entailed"
    assert result.periods[0].excel_value == 70.47


def test_evaluate_yoy_growth_on_quarterly_table_uses_prior_year_same_quarter():
    # (70.47 - 92.11) / 92.11 * 100 = -23.49...
    fact = _make_fact(
        operation="yoy_growth",
        periods=[_make_period(metric_label="Kredit Modal Kerja", year=2013, month="Q2")],
        claimed_value=-23.49,
        unit="persen_yoy",
    )

    result = _evaluate_fact(fact, [_make_source(_make_quarterly_table())])

    assert result.verdict == "Entailed"
    assert result.computed_value == pytest.approx(-23.49, abs=0.01)


def test_evaluate_temporal_unitless_source_compares_raw_instead_of_unit_inconclusive():
    # A temporal source with an empty unit (survey workbook) must fall back to raw
    # comparison — not fail with "Unit conversion not supported".
    fact = _make_fact(
        periods=[_make_period(metric_label="Kredit Modal Kerja", year=2013, month="Q2")],
        claimed_value=70.47,
        unit="persen",
    )

    result = _evaluate_fact(fact, [_make_source(_make_quarterly_table())])

    assert result.verdict == "Entailed"
    assert "not supported" not in (result.reasoning or "")


# ---------------------------------------------------------------------------
# Tier-4 cell-pointer pass (_pointer_pass) — LLM points, code reads
# ---------------------------------------------------------------------------

def _pointer_llm(batch, call_log=None):
    def _respond(prompt_value):
        if call_log is not None:
            call_log.append(prompt_value)
        return batch

    llm = Mock()
    llm.with_structured_output = Mock(return_value=RunnableLambda(_respond))
    return llm


def _pointer_llm_raising():
    def _raise(_prompt_value):
        raise RuntimeError("pointer boom")

    llm = Mock()
    llm.with_structured_output = Mock(return_value=RunnableLambda(_raise))
    return llm


def _pointer_only_source(grid):
    return _ExcelSource(
        table=BITableData(title="", unit="", row_labels=[]),
        filename="aneh.xlsx", sheet="S", grid=grid, pointer_only=True,
    )


_POINTER_GRID = [["Metrik", "x", "y"], ["KPR/KPA", 38.0, 40.63], ["Lain", "na", 99.9]]


def _kpr_fact(**overrides):
    base = dict(
        periods=[_make_period(metric_label="KPR/KPA", year=2026, month="Q2")],
        claimed_value=40.63,
        unit="persen",
    )
    base.update(overrides)
    return _make_fact(**base)


def test_pointer_pass_resolves_value_claim_with_provenance():
    src = _pointer_only_source(_POINTER_GRID)
    fact = _kpr_fact()
    results = [_evaluate_fact(fact, [src])]
    assert results[0].verdict == "Inconclusive"
    batch = _BatchCellPointers(pointers=[_CellPointer(query_index=0, found=True, row=1, col=2)])

    new, n = asyncio.run(_pointer_pass([fact], results, [src], _pointer_llm(batch)))

    assert n == 1
    assert new[0].verdict == "Entailed"
    assert new[0].resolved_via == "pointer"
    assert "R1K2" in new[0].reasoning
    assert new[0].matched_excel_source == "aneh.xlsx / S"


def test_pointer_pass_out_of_range_pointer_keeps_original_inconclusive():
    src = _pointer_only_source(_POINTER_GRID)
    fact = _kpr_fact()
    results = [_evaluate_fact(fact, [src])]
    batch = _BatchCellPointers(pointers=[_CellPointer(query_index=0, found=True, row=9, col=9)])

    new, n = asyncio.run(_pointer_pass([fact], results, [src], _pointer_llm(batch)))

    assert n == 0
    assert new[0].verdict == "Inconclusive"
    assert new[0].resolved_via is None


def test_pointer_pass_non_numeric_cell_keeps_original_inconclusive():
    src = _pointer_only_source(_POINTER_GRID)
    fact = _kpr_fact()
    results = [_evaluate_fact(fact, [src])]
    batch = _BatchCellPointers(pointers=[_CellPointer(query_index=0, found=True, row=2, col=1)])

    new, n = asyncio.run(_pointer_pass([fact], results, [src], _pointer_llm(batch)))

    assert n == 0
    assert new[0].verdict == "Inconclusive"


def test_pointer_pass_llm_failure_keeps_all_originals():
    src = _pointer_only_source(_POINTER_GRID)
    fact = _kpr_fact()
    results = [_evaluate_fact(fact, [src])]

    new, n = asyncio.run(_pointer_pass([fact], results, [src], _pointer_llm_raising()))

    assert n == 0
    assert new == results


def test_pointer_pass_yoy_uses_synthesized_prior_year_cell():
    grid = [["Metrik", "x", "y"], ["KPR/KPA", 40.0, 42.0]]
    src = _pointer_only_source(grid)
    fact = _kpr_fact(operation="yoy_growth", claimed_value=5.0, unit="persen_yoy")
    results = [_evaluate_fact(fact, [src])]
    batch = _BatchCellPointers(pointers=[
        _CellPointer(query_index=0, found=True, row=1, col=2),  # 2026 Q2 = 42.0
        _CellPointer(query_index=1, found=True, row=1, col=1),  # 2025 Q2 = 40.0
    ])

    new, n = asyncio.run(_pointer_pass([fact], results, [src], _pointer_llm(batch)))

    assert n == 1
    assert new[0].verdict == "Entailed"
    assert new[0].computed_value == pytest.approx(5.0)


def test_pointer_pass_wrong_numeric_cell_yields_attributed_verdict():
    # A wrong-but-numeric pointer produces a REAL verdict by design — the guard is
    # provenance (resolved_via + cell ref), not blocking.
    src = _pointer_only_source(_POINTER_GRID)
    fact = _kpr_fact()  # claims 40.63, pointer aims at 38.0
    results = [_evaluate_fact(fact, [src])]
    batch = _BatchCellPointers(pointers=[_CellPointer(query_index=0, found=True, row=1, col=1)])

    new, n = asyncio.run(_pointer_pass([fact], results, [src], _pointer_llm(batch)))

    assert n == 1
    assert new[0].verdict == "Refuted"
    assert new[0].resolved_via == "pointer"
    assert "R1K1" in new[0].reasoning


def test_pointer_pass_batches_all_facts_into_one_call_per_source():
    src = _pointer_only_source(_POINTER_GRID)
    facts = [
        _kpr_fact(),
        _kpr_fact(periods=[_make_period(metric_label="Lain", year=2026, month="Q2")],
                  claimed_value=99.9),
    ]
    results = [_evaluate_fact(f, [src]) for f in facts]
    batch = _BatchCellPointers(pointers=[
        _CellPointer(query_index=0, found=True, row=1, col=2),
        _CellPointer(query_index=1, found=True, row=2, col=2),
    ])
    log = []

    new, n = asyncio.run(_pointer_pass(facts, results, [src], _pointer_llm(batch, log)))

    assert len(log) == 1  # one batched call for the whole source
    assert n == 2
    assert all(r.resolved_via == "pointer" for r in new)


@patch("paired_verifier.extract_structured_facts_async")
def test_verify_paired_end_to_end_pointer_resolution_on_unparseable_sheet(mock_extract_facts):
    # A real xlsx whose layout defeats every parser tier (numeric header codes — not
    # headerish for the generic tier, no year row for BI, and the scripted LLM fails
    # structure mapping) — the claim must still verify via the cell-pointer pass.
    import io as _io
    from openpyxl import Workbook

    wb = Workbook()
    ws = wb.active
    ws.title = "S"
    ws.append([None, 12345, 67890])
    ws.append(["KPR", 40.63, 39.0])
    buf = _io.BytesIO()
    wb.save(buf)

    fact = _make_fact(
        periods=[_make_period(metric_label="KPR", year=2026, month="Q2")],
        claimed_value=40.63, unit="persen",
    )
    mock_extract_facts.return_value = [fact]

    batch = _BatchCellPointers(pointers=[_CellPointer(query_index=0, found=True, row=1, col=1)])

    def _structured(schema, **_kw):
        if schema is _BatchCellPointers:
            return RunnableLambda(lambda _pv: batch)

        def _fail(_pv):
            raise RuntimeError("no structure spec")
        return RunnableLambda(_fail)  # tier-3 structure mapping fails

    llm = Mock()
    llm.with_structured_output = Mock(side_effect=_structured)

    response = asyncio.run(
        verify_paired(
            narrative_text="[== Halaman 1 ==]\n" + "k" * 250,
            excel_sources=[(buf.getvalue(), "S", "aneh.xlsx")],
            llm=llm,
        )
    )

    assert response.excel_parsers == ["pointer-only"]
    assert response.entailed_count == 1
    assert response.results[0].verdict == "Entailed"
    assert response.results[0].resolved_via == "pointer"
    assert "R1K1" in response.results[0].reasoning


def test_pointer_pass_sheet_unit_enables_scale_conversion_on_pointer_only_source():
    grid = [["Posisi", 8900.0]]
    src = _pointer_only_source(grid)
    fact = _make_fact(
        periods=[_make_period(metric_label="Posisi Kredit", year=2026, month="Apr")],
        claimed_value=8.9, unit="triliun Rp",
    )
    results = [_evaluate_fact(fact, [src])]
    batch = _BatchCellPointers(
        sheet_unit="Miliar Rp",
        pointers=[_CellPointer(query_index=0, found=True, row=0, col=1)],
    )

    new, n = asyncio.run(_pointer_pass([fact], results, [src], _pointer_llm(batch)))

    assert n == 1
    assert new[0].verdict == "Entailed"  # 8.9 triliun == 8900 miliar


# ---------------------------------------------------------------------------
# Parser cascade (_parse_table_with_fallback)
# ---------------------------------------------------------------------------

@patch("paired_verifier.parse_generic_table")
@patch("paired_verifier.parse_bi_table")
def test_cascade_prefers_bi_parser_when_it_extracts_data(mock_bi, mock_generic):
    mock_bi.return_value = _make_table(data={("Total", 2026, "Apr"): 1.0})

    table, parser = _parse_table_with_fallback(b"bytes", "I.1")

    assert parser == "bi"
    mock_generic.assert_not_called()


@patch("paired_verifier.parse_generic_table")
@patch("paired_verifier.parse_bi_table")
def test_cascade_falls_back_to_generic_when_bi_parser_raises(mock_bi, mock_generic):
    mock_bi.side_effect = ValueError("Could not auto-detect year/month header rows")
    mock_generic.return_value = _make_cat_table(data={("Laptop ASUS", "Harga"): 1.0})

    table, parser = _parse_table_with_fallback(b"bytes", "S")

    assert parser == "generic"
    assert table.axis_type == "categorical"


@patch("paired_verifier.parse_generic_table")
@patch("paired_verifier.parse_bi_table")
def test_cascade_falls_back_to_generic_when_bi_parse_is_empty(mock_bi, mock_generic):
    mock_bi.return_value = _make_table(data={})
    mock_generic.return_value = _make_cat_table(data={("Laptop ASUS", "Harga"): 1.0})

    table, parser = _parse_table_with_fallback(b"bytes", "S")

    assert parser == "generic"


@patch("paired_verifier.parse_generic_table")
@patch("paired_verifier.parse_bi_table")
def test_cascade_keeps_empty_bi_result_when_generic_also_fails(mock_bi, mock_generic):
    mock_bi.return_value = _make_table(data={})
    mock_generic.side_effect = ValueError("no structure")

    table, parser = _parse_table_with_fallback(b"bytes", "S")

    assert parser == "bi"
    assert table.row_labels == []


def test_cascade_raises_combined_error_when_both_parsers_fail():
    with pytest.raises(ValueError, match="Parser BI.*Parser generik"):
        _parse_table_with_fallback(b"not an excel file at all", "S")


@patch("paired_verifier.parse_table_with_llm")
@patch("paired_verifier.parse_generic_table")
@patch("paired_verifier.parse_bi_table")
def test_cascade_uses_llm_tier_when_deterministic_parsers_fail(mock_bi, mock_generic, mock_llm_parse):
    mock_bi.side_effect = ValueError("no year header")
    mock_generic.side_effect = ValueError("no structure")
    mock_llm_parse.return_value = _make_cat_table(data={("Laptop ASUS", "Harga"): 1.0})

    table, parser = _parse_table_with_fallback(b"bytes", "S", llm=Mock())

    assert parser == "llm"
    assert table.axis_type == "categorical"


@patch("paired_verifier.parse_table_with_llm")
@patch("paired_verifier.parse_generic_table")
@patch("paired_verifier.parse_bi_table")
def test_cascade_llm_tier_not_called_without_an_llm(mock_bi, mock_generic, mock_llm_parse):
    mock_bi.side_effect = ValueError("no year header")
    mock_generic.return_value = _make_cat_table(data={("Laptop ASUS", "Harga"): 1.0})

    _parse_table_with_fallback(b"bytes", "S")

    mock_llm_parse.assert_not_called()


@patch("paired_verifier.parse_table_with_llm")
@patch("paired_verifier.parse_generic_table")
@patch("paired_verifier.parse_bi_table")
def test_cascade_keeps_empty_bi_result_when_llm_tier_also_fails(mock_bi, mock_generic, mock_llm_parse):
    mock_bi.return_value = _make_table(data={})
    mock_generic.side_effect = ValueError("no structure")
    mock_llm_parse.side_effect = ValueError("spec rejected")

    table, parser = _parse_table_with_fallback(b"bytes", "S", llm=Mock())

    assert parser == "bi"
    assert table.row_labels == []
