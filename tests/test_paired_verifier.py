import asyncio
import logging
from unittest.mock import Mock, patch

import pytest

from cell_pointer import _MultiCellPointer, _MultiSourcePointers, _SheetUnit
from excel_parser_bi import BITableData
from langchain_core.runnables import RunnableLambda
from paired_verifier import (
    _ExcelSource,
    _attach_named_members,
    _build_table_suggestions,
    _deduplicate_facts,
    _evaluate_fact,
    _evaluate_fact_safely,
    _printed_signed,
    _growth_tolerance,
    _numeric_verdict,
    _parse_scale_unit,
    _parse_table_with_fallback,
    _pointer_pass,
    _semester_periods,
    _unit_factor,
    verify_paired,
)
from publication_parsers import PublicationParseError
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

def test_unit_factor_reads_an_english_sdds_unit():
    # Tabel Cadev ORA (SDDS template, DSTA SharePoint 2026-10): the right row was matched, but
    # 'Millions of US$' parsed as no unit at all, so 146.320 (juta USD) was compared as 0,0001
    # miliar against the report's 146,3 — a false Tidak Sesuai.
    assert _unit_factor("miliar dolar AS", "Millions of US$") == 1000.0
    assert _unit_factor("juta dolar AS", "Thousands of US$") == 1000.0
    assert _unit_factor("triliun Rp", "Billions of Rupiah") == 1000.0


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


@pytest.mark.parametrize(
    "quote,claimed,cell,verdict",
    [
        ("tumbuh 10,8%", 10.8, 10.84, "Entailed"),
        ("tumbuh 10,8%", 10.8, 10.86, "Refuted"),
        # Excel shows 10,85 as 10,9 (half up); a flat 0,05 called it equal to 10,8.
        ("tumbuh 10,8%", 10.8, 10.85, "Refuted"),
        # A whole number is rounded as a whole number — 0,05 refuted it over 0,1.
        ("tercatat Rp9.388 triliun", 9388.0, 9387.9, "Entailed"),
        ("tercatat Rp9.388 triliun", 9388.0, 9387.4, "Refuted"),
        ("tercatat Rp10.253,7 triliun", 10253.7, 10253.66, "Entailed"),
        ("tercatat Rp10.253,7 triliun", 10253.7, 10253.76, "Refuted"),
        # Two decimals are held to two decimals — 0,05 let 5,16 pass as 5,12.
        ("sebesar 5,12%", 5.12, 5.16, "Refuted"),
        ("sebesar 5,12%", 5.12, 5.1249, "Entailed"),
        # '10,0' prints a decimal the float 10.0 hides: 10,4 must not round to 10.
        ("rasio 10,0%", 10.0, 10.4, "Refuted"),
        # More decimals than the cell holds: the table cannot settle the extra digit.
        ("BI-Rate 4,75%", 4.75, 4.8, "Entailed"),
        # Not printed in the sentence: the old tolerance stands.
        ("quote", 10.8, 10.84, "Entailed"),
    ],
)
def test_value_claim_is_judged_by_rounding_the_cell_as_printed(quote, claimed, cell, verdict):
    table = _make_table(unit="triliun Rp", data={("Total", 2026, "Apr"): cell})
    fact = _make_fact(claimed_value=claimed, context_quote=quote)

    assert _evaluate_fact(fact, [_make_source(table)]).verdict == verdict


def test_value_claim_rounding_is_shown_in_the_reasoning():
    table = _make_table(unit="triliun Rp", data={("Total", 2026, "Apr"): 9387.9})
    fact = _make_fact(claimed_value=9388.0, context_quote="tercatat Rp9.388 triliun")

    result = _evaluate_fact(fact, [_make_source(table)])

    assert "dibulatkan ke 0 desimal seperti di laporan = 9388 → sama dengan klaim" in result.reasoning


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


def _signed_level_case(quote, claimed, table_value, label="Transaksi Berjalan"):
    """A value claim against a balance-of-payments style table, where deficits are negative."""
    table = _make_table(
        title="Neraca Pembayaran Indonesia", unit="miliar dolar AS",
        data={(label, 2026, "Q2"): table_value},
    )
    fact = _make_fact(
        periods=[_make_period(metric_label=label, month="Q2")],
        claimed_value=claimed, unit="miliar dolar AS", context_quote=quote,
    )
    return _evaluate_fact(fact, [_make_source(table)])


def test_a_deficit_printed_as_a_positive_number_matches_the_negative_table_value():
    # NPI: "defisit transaksi berjalan ... tercatat sebesar 12,5 miliar dolar AS" — the report
    # prints the size of the deficit; SEKI Tabel V.1 stores the balance, −12,49.
    result = _signed_level_case(
        "defisit transaksi berjalan pada triwulan II 2026 tercatat sebesar 12,5 miliar dolar AS",
        12.5, -12.4874,
    )

    assert result.verdict == "Entailed"
    assert result.computed_value == pytest.approx(-12.4874)
    assert "defisit" in result.reasoning


def test_net_liabilities_printed_positive_match_a_negative_net_position():
    # PII: "kewajiban neto sebesar 197,4 miliar dolar AS" against the net IIP, −197,43.
    result = _signed_level_case(
        "PII Indonesia mencatat kewajiban neto sebesar 197,4 miliar dolar AS",
        197.4, -197.431, label="Posisi Investasi Internasional, bersih",
    )

    assert result.verdict == "Entailed"


def test_the_same_number_with_the_opposite_sign_stays_wrong_without_a_word_that_explains_it():
    result = _signed_level_case(
        "transaksi berjalan pada triwulan II 2026 tercatat sebesar 12,5 miliar dolar AS",
        12.5, -12.4874,
    )

    assert result.verdict == "Refuted"


def test_a_deficit_word_does_not_excuse_a_positive_table_value():
    # The word explains a positive printed number against a NEGATIVE stored balance — not a
    # negative claim against a positive cell, which is a genuine disagreement.
    result = _signed_level_case(
        "defisit transaksi berjalan tercatat sebesar 12,5 miliar dolar AS", -12.5, 12.4874,
    )

    assert result.verdict == "Refuted"


def _index_case(operation, claimed, unit, table_unit, data, label="TOTAL", month="Q2", periods=None):
    table = _make_table(title="Indeks Harga Properti Residensial", unit=table_unit, data=data)
    fact = _make_fact(
        operation=operation,
        periods=periods or [_make_period(metric_label=label, month=month)],
        claimed_value=claimed, unit=unit,
    )
    return _evaluate_fact(fact, [_make_source(table)])


def test_a_percent_claim_is_not_compared_with_index_levels():
    # SHPR: "harga rumah tipe menengah tumbuh sebesar 0,40% (qtq)" was checked against the
    # index LEVEL 114,01 and reported Tidak Sesuai — a growth rate and a level are not the same
    # quantity, so the honest answer from this sheet is "not enough data".
    result = _index_case(
        "value", 0.40, "persen", "Indeks (2018=100)", {("MENENGAH", 2026, "Q2"): 114.01},
        label="MENENGAH",
    )

    assert result.verdict == "Inconclusive"
    assert "indeks" in result.reasoning.lower()


def test_a_yoy_growth_claim_is_still_computed_from_index_levels():
    result = _index_case(
        "yoy_growth", 0.69, "persen_yoy", "Indeks (2018=100)",
        {("TOTAL", 2026, "Q2"): 110.89, ("TOTAL", 2025, "Q2"): 110.13},
    )

    assert result.verdict == "Entailed"


def test_a_percent_claim_still_compares_with_an_index_that_is_read_in_percent():
    # PMI-BI is a diffusion index printed as "52,03%"; its sheet says '(%, Indeks)'.
    result = _index_case(
        "value", 52.03, "persen", "%, Indeks", {("PMI - BI", 2026, "Q1"): 52.03125},
        label="PMI - BI", month="Q1",
    )

    assert result.verdict == "Entailed"


def test_an_index_claim_without_a_unit_still_compares_with_index_levels():
    result = _index_case(
        "value", 118.5, None, "Indeks", {("IKK", 2026, "Aug"): 118.5}, label="IKK", month="Aug",
    )

    assert result.verdict == "Entailed"


def test_a_deficit_word_does_not_excuse_a_different_number():
    result = _signed_level_case(
        "defisit transaksi berjalan tercatat sebesar 12,5 miliar dolar AS", 12.5, -9.1,
    )

    assert result.verdict == "Refuted"


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


def _devisa_case(claimed_value):
    # Cadangan devisa, SEKI 5.9: "…pada akhir Mei 2026 tercatat 144,9 miliar dolar AS, lebih
    # rendah dibandingkan dengan posisi akhir April 2026 sebesar 146,2 miliar dolar AS."
    table = _make_table(unit="Juta USD", data={
        ("Total", 2026, "Apr"): 146201.96, ("Total", 2026, "May"): 144898.4,
    })
    fact = _make_fact(
        operation="diff", claimed_value=claimed_value, unit="miliar dolar AS",
        periods=[_make_period(month="Apr"), _make_period(month="May")],
    )
    return _evaluate_fact(fact, [_make_source(table)])


def test_diff_whose_claimed_number_is_really_a_level_is_checked_as_a_value():
    # The sentence states two levels and a direction, never a difference. The extractor read the
    # comparison as a subtraction and put April's level in claimed_value, so 146,2 was checked
    # against a difference of -1,3 and Refuted — a report matching the table to three digits.
    result = _devisa_case(146.2)

    assert result.verdict == "Entailed"
    assert result.operation == "value"
    assert result.computed_value == pytest.approx(146.202)
    assert len(result.periods) == 1 and result.periods[0].month == "Apr"
    assert "ditandai sebagai selisih" in result.reasoning


def test_diff_that_really_states_its_difference_is_never_rerouted():
    # 144,8984 - 146,202 = -1,3036. The reinterpretation is only consulted once the diff check
    # has failed, so a genuine diff claim keeps both its operation and its verdict.
    result = _devisa_case(-1.3036)

    assert result.verdict == "Entailed"
    assert result.operation == "diff"


def test_a_wrong_diff_matching_no_level_stays_refuted():
    # The signal the recovery must not swallow: a claimed difference that is simply wrong.
    result = _devisa_case(-5.0)

    assert result.verdict == "Refuted"
    assert result.operation == "diff"


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


def _npi_exports_table():
    return _make_table(
        title="Neraca Pembayaran Indonesia : Ringkasan", unit="Juta USD",
        data={
            ("Nonmigas > Ekspor", 2026, "Q2"): 70459.06, ("Nonmigas > Ekspor", 2025, "Q2"): 64165.87,
            ("Nonmigas > Impor", 2026, "Q2"): -58322.94, ("Nonmigas > Impor", 2025, "Q2"): -48487.59,
        },
    )


def _yoy_fact(label, claimed):
    return _make_fact(
        operation="yoy_growth", claimed_value=claimed, unit="persen_yoy",
        periods=[_make_period(metric_label=label, month="Q2")],
    )


@pytest.mark.parametrize("label, missing", [
    # NPI E2E: "ekspor nonmigas ke Tiongkok tumbuh 18,7% (yoy)" was answered by the all-country
    # 'Nonmigas > Ekspor' row (9,8%) and reported Tidak Sesuai — Tabel V.1 has no country rows.
    ("Ekspor nonmigas ke Tiongkok", "tiongkok"),
    # ... and "impor barang konsumsi" by the all-goods import row.
    ("impor nonmigas barang konsumsi", "konsumsi"),
])
def test_a_breakdown_the_table_does_not_hold_is_not_answered_by_its_parent_row(label, missing):
    result = _evaluate_fact(_yoy_fact(label, 18.7), [_make_source(_npi_exports_table())])

    assert result.verdict == "Inconclusive"
    assert missing in result.reasoning.lower()


def test_a_claim_the_table_does_hold_is_still_answered():
    result = _evaluate_fact(_yoy_fact("Ekspor nonmigas", 9.8), [_make_source(_npi_exports_table())])

    assert result.verdict == "Entailed"


def test_a_word_the_table_spells_with_another_prefix_counts_as_present():
    # "kredit kepemilikan rumah" in the prose, "Kredit Pemilikan Rumah" in the sheet.
    table = _make_table(
        title="Perkembangan Kredit", unit="triliun Rp",
        data={("Kredit Pemilikan Rumah", 2026, "Q2"): 110.0, ("Kredit Pemilikan Rumah", 2025, "Q2"): 100.0},
    )

    result = _evaluate_fact(_yoy_fact("kredit kepemilikan rumah", 10.0), [_make_source(table)])

    assert result.verdict == "Entailed"


def _skdu_sheet(title, value, label="PENGADAAN LISTRIK"):
    return _make_table(title=title, unit="%", data={(label, 2026, "Q2"): value})


def _capacity_fact(label="PENGADAAN LISTRIK"):
    return _make_fact(
        periods=[_make_period(metric_label=label, month="Q2")], claimed_value=80.54, unit="persen",
    )


def test_a_full_tie_goes_to_the_source_whose_value_the_claim_states():
    # SKDU E2E: "LU Pengadaan Listrik tercatat stabil (80,54%)" — capacity utilisation, Tabel 2 —
    # was answered by Tabel 1's SBT row of the same name (0,57) because the three sheets tie on
    # every ranking key and Tabel 1 was uploaded first.
    sources = [
        _make_source(_skdu_sheet("Tabel 1. Kegiatan Usaha", 0.57), sheet="T1"),
        _make_source(_skdu_sheet("Tabel 2. Kapasitas Produksi Terpakai", 80.54), sheet="T2"),
        _make_source(_skdu_sheet("Tabel 4. Penggunaan Tenaga Kerja", 0.01), sheet="T4"),
    ]

    result = _evaluate_fact(_capacity_fact(), sources)

    assert result.verdict == "Entailed"
    assert result.matched_excel_source == "TABEL1_1.xls / T2"


def test_a_looser_match_that_happens_to_agree_does_not_take_over():
    sources = [
        _make_source(_skdu_sheet("Tabel 1. Kegiatan Usaha", 0.57), sheet="T1"),
        _make_source(
            _skdu_sheet("Tabel 2. Kapasitas Produksi Terpakai", 80.54, label="PENGADAAN LISTRIK DAN GAS"),
            sheet="T2",
        ),
    ]

    result = _evaluate_fact(_capacity_fact(), sources)

    assert result.verdict == "Refuted"
    assert result.matched_excel_source == "TABEL1_1.xls / T1"


def test_a_tie_goes_to_the_source_whose_title_names_the_claim_not_one_with_a_lookalike_row():
    # SULNI E2E: "ULN pemerintah ... Jasa Kesehatan (22,0% dari total ULN pemerintah)" against
    # the private-debt sector table (uploaded first) and the government one. The private table
    # also "covers" pemerintah — through its 'Administrasi Pemerintah, ...' SECTOR row — so both
    # scored the same and upload order handed the claim to the wrong table.
    swasta = _make_table(
        title="Posisi Utang Luar Negeri (ULN) Swasta Menurut Sektor Ekonomi", unit="Juta USD",
        data={
            ("Jasa Kesehatan dan Kegiatan Sosial", 2026, "Jul"): 269.6,
            ("Administrasi Pemerintah, Pertahanan, dan Jaminan Sosial Wajib", 2026, "Jul"): 0.0,
            ("TOTAL", 2026, "Jul"): 194476.9,
        },
    )
    pemerintah = _make_table(
        title="Posisi Utang Luar Negeri (ULN) Pemerintah Menurut Sektor Ekonomi", unit="Juta USD",
        data={
            ("Jasa Kesehatan dan Kegiatan Sosial", 2026, "Jul"): 48110.0,
            ("Administrasi Pemerintah, Pertahanan, dan Jaminan Sosial Wajib", 2026, "Jul"): 45200.0,
            ("TOTAL", 2026, "Jul"): 218391.1,
        },
    )
    fact = _make_fact(
        operation="ratio",
        periods=[
            _make_period(metric_label="Jasa Kesehatan dan Kegiatan Sosial", month="Jul"),
            _make_period(metric_label="total ULN pemerintah", month="Jul"),
        ],
        claimed_value=22.0,
        unit="persen",
    )

    result = _evaluate_fact(fact, [
        _make_source(swasta, filename="TABEL_SWASTA.xlsx", sheet="Tbl III.2"),
        _make_source(pemerintah, filename="TABEL_PEMERINTAH.xlsx", sheet="Tbl II.2"),
    ])

    assert result.verdict == "Entailed"
    assert result.matched_excel_source == "TABEL_PEMERINTAH.xlsx / Tbl II.2"


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


def test_evaluate_is_stable_entailed_for_a_small_move_relative_to_the_level():
    # The observed false positive: "proporsi cicilan sebesar 10,0%, relatif stabil dibandingkan
    # bulan sebelumnya sebesar 10,2%" was Refuted, because the stability band was MATCH_TOLERANCE
    # (0,05) — half a PRINTING unit, which asks whether two numbers are the same number, not
    # whether a series held flat. 0,2pp on a ratio of 10 is what "relatif stabil" means.
    table = _make_table(unit="", data={
        ("Total > Cicilan pinjaman", 2026, "May"): 10.2,
        ("Total > Cicilan pinjaman", 2026, "Jun"): 10.0,
    })
    fact = _make_fact(
        operation="is_stable", claimed_value=None, unit=None,
        periods=[
            _make_period(metric_label="Total > Cicilan pinjaman", year=2026, month="May"),
            _make_period(metric_label="Total > Cicilan pinjaman", year=2026, month="Jun"),
        ],
    )

    result = _evaluate_fact(fact, [_make_source(table)])

    assert result.verdict == "Entailed"


def test_evaluate_is_stable_refuted_when_the_move_is_large_for_the_level():
    # The band is proportional, so it must not bless the same 0,4 absolute move on a series an
    # order of magnitude smaller — that is a swing of more than half the level.
    table = _make_table(unit="", data={
        ("Total", 2026, "May"): 0.3, ("Total", 2026, "Jun"): 0.7,
    })
    fact = _make_fact(
        operation="is_stable", claimed_value=None, unit=None,
        periods=[_make_period(month="May"), _make_period(month="Jun")],
    )

    result = _evaluate_fact(fact, [_make_source(table)])

    assert result.verdict == "Refuted"


def test_evaluate_is_stable_refuted_when_the_series_actually_moved():
    # The signal the widened band must not suppress: 10,0 → 13,1 is a real change.
    table = _make_table(unit="", data={
        ("Total", 2026, "May"): 10.0, ("Total", 2026, "Jun"): 13.1,
    })
    fact = _make_fact(
        operation="is_stable", claimed_value=None, unit=None,
        periods=[_make_period(month="May"), _make_period(month="Jun")],
    )

    result = _evaluate_fact(fact, [_make_source(table)])

    assert result.verdict == "Refuted"


def test_evaluate_is_increasing_autocompletes_previous_quarter():
    # "SBT KMK meningkat" in a Tw II report states only Q2; the baseline Q1 is pulled from
    # the table so the QoQ trend is checkable without the LLM guessing the earlier period.
    table = _make_table(data={
        ("Kredit Modal Kerja", 2026, "Q1"): 36.4,
        ("Kredit Modal Kerja", 2026, "Q2"): 94.34,
    })
    fact = _make_fact(
        operation="is_increasing",
        periods=[_make_period(metric_label="Kredit Modal Kerja", year=2026, month="Q2")],
        claimed_value=None, unit=None,
    )

    result = _evaluate_fact(fact, [_make_source(table)])

    assert result.verdict == "Entailed"
    assert len(result.periods) == 2  # Q1 auto-added ahead of the stated Q2


def test_evaluate_is_decreasing_autocompletes_and_refutes_when_actually_up():
    table = _make_table(data={
        ("KMK", 2026, "Q1"): 36.4, ("KMK", 2026, "Q2"): 94.34,
    })
    fact = _make_fact(
        operation="is_decreasing",
        periods=[_make_period(metric_label="KMK", year=2026, month="Q2")],
        claimed_value=None, unit=None,
    )

    result = _evaluate_fact(fact, [_make_source(table)])

    assert result.verdict == "Refuted"  # it rose, so a decline claim is refuted


def test_evaluate_is_increasing_inconclusive_when_periods_mix_metrics():
    # "SBT meningkat pada KMK, KI, KK" mis-bundled as one trend over THREE metrics at the
    # same quarter: monotonicity across unrelated series wrongly Refuted before — must now
    # be Inconclusive (the claim should have been split into one trend per metric).
    table = _make_table(data={
        ("Kredit Modal Kerja", 2026, "Q2"): 94.34,
        ("Kredit Investasi", 2026, "Q2"): 93.51,
        ("Kredit Konsumsi", 2026, "Q2"): 83.67,
    })
    fact = _make_fact(
        operation="is_increasing",
        periods=[
            _make_period(metric_label="Kredit Modal Kerja", year=2026, month="Q2"),
            _make_period(metric_label="Kredit Investasi", year=2026, month="Q2"),
            _make_period(metric_label="Kredit Konsumsi", year=2026, month="Q2"),
        ],
        claimed_value=None, unit=None,
    )

    result = _evaluate_fact(fact, [_make_source(table)])

    assert result.verdict == "Inconclusive"
    assert "satu metrik" in result.reasoning


def test_evaluate_is_increasing_inconclusive_when_single_time_point():
    # One data point cannot be a trend — previously the empty monotonicity check made it a
    # vacuous Entailed.
    table = _make_table(data={("Total", 2026, "Q2"): 94.34})
    fact = _make_fact(
        operation="is_increasing",
        periods=[_make_period(metric_label="Total", year=2026, month="Q2")],
        claimed_value=None, unit=None,
    )

    result = _evaluate_fact(fact, [_make_source(table)])

    assert result.verdict == "Inconclusive"


# ---------------------------------------------------------------------------
# _evaluate_fact — operation="above_threshold" / "below_threshold"
# ---------------------------------------------------------------------------

def _threshold_table(value):
    return _make_table(title="PMI", unit="%, Indeks", data={("PMI - BI", 2026, "Q2"): value})


def _threshold_fact(operation, bound):
    return _make_fact(
        operation=operation,
        periods=[_make_period(metric_label="PMI - BI", year=2026, month="Q2")],
        claimed_value=bound, unit="persen",
    )


def test_evaluate_above_threshold_entailed_when_value_exceeds_bound():
    result = _evaluate_fact(_threshold_fact("above_threshold", 50.0), [_make_source(_threshold_table(51.43))])

    assert result.verdict == "Entailed"
    assert result.computed_value == 51.43
    assert result.periods[0].excel_value == 51.43


def test_evaluate_above_threshold_refuted_when_value_below_bound():
    result = _evaluate_fact(_threshold_fact("above_threshold", 50.0), [_make_source(_threshold_table(48.0))])

    assert result.verdict == "Refuted"


def test_evaluate_above_threshold_refuted_at_exactly_the_bound():
    # A PMI index of exactly 50 is neutral — neither expansion nor contraction.
    result = _evaluate_fact(_threshold_fact("above_threshold", 50.0), [_make_source(_threshold_table(50.0))])

    assert result.verdict == "Refuted"


def test_evaluate_below_threshold_entailed_when_value_under_bound():
    result = _evaluate_fact(_threshold_fact("below_threshold", 50.0), [_make_source(_threshold_table(47.2))])

    assert result.verdict == "Entailed"


def test_evaluate_threshold_inconclusive_without_a_bound():
    fact = _threshold_fact("above_threshold", None)
    result = _evaluate_fact(fact, [_make_source(_threshold_table(51.43))])

    assert result.verdict == "Inconclusive"
    assert "ambang" in result.reasoning


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


@patch("paired_verifier.extract_structured_facts_async")
@patch("paired_verifier._parse_table_with_fallback")
def test_verify_paired_hands_the_publication_to_the_table_parsers(mock_parse, mock_extract_facts):
    mock_parse.return_value = (_make_table(data={("TOTAL", 2026, "Jul"): 1.0}), "sulni")
    mock_extract_facts.return_value = []

    response = asyncio.run(
        verify_paired(
            narrative_text="[== Halaman 1 ==]\n" + "x" * 250,
            excel_sources=[(b"xlsx-bytes", "TabI.1", "TABEL_INDONESIA.xlsx")],
            llm=Mock(),
            publication="sulni",
        )
    )

    assert mock_parse.call_args.kwargs["publication"] == "sulni"
    assert response.excel_parsers == ["sulni"]


def test_verify_paired_reads_each_grid_through_the_cached_loader():
    import paired_verifier
    from publication_parsers import load_grid

    assert paired_verifier._load_grid is load_grid


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


def test_evaluate_value_dimensionless_nonempty_unit_compares_raw():
    # PMI is an index in % ; a 'persen' claim must compare raw against a '(%, Indeks)'
    # sheet unit (no scale to convert), not fail unit conversion.
    table = _make_table(title="PMI", unit="%, Indeks", data={("PMI - BI", 2026, "Q2"): 51.43})
    fact = _make_fact(
        periods=[_make_period(metric_label="PMI - BI", year=2026, month="Q2")],
        claimed_value=51.43, unit="persen",
    )

    result = _evaluate_fact(fact, [_make_source(table)])

    assert result.verdict == "Entailed"
    assert "not supported" not in (result.reasoning or "")


def test_evaluate_value_dimensionless_nonempty_unit_still_refutes_wrong_number():
    table = _make_table(title="PMI", unit="%, Indeks", data={("PMI - BI", 2026, "Q2"): 51.43})
    fact = _make_fact(
        periods=[_make_period(metric_label="PMI - BI", year=2026, month="Q2")],
        claimed_value=60.0, unit="persen",
    )

    result = _evaluate_fact(fact, [_make_source(table)])

    assert result.verdict == "Refuted"


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
    batch = _MultiSourcePointers(pointers=[
        _MultiCellPointer(source_index=0, query_index=0, found=True, row=1, col=2)
    ])

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
    batch = _MultiSourcePointers(pointers=[
        _MultiCellPointer(source_index=0, query_index=0, found=True, row=9, col=9)
    ])

    new, n = asyncio.run(_pointer_pass([fact], results, [src], _pointer_llm(batch)))

    assert n == 0
    assert new[0].verdict == "Inconclusive"
    assert new[0].resolved_via is None


def test_pointer_pass_rejects_pointer_at_row_unrelated_to_metric():
    # Regression: an ILS claim (metric absent from the sheet) must not bind to the SBT
    # TOTAL row just because the pointer aimed there — it stays Inconclusive.
    grid = [
        ["Menurut Penggunaan", "Kredit Modal Kerja", 92.11],
        ["TOTAL", "TOTAL", 93.08],
    ]
    src = _pointer_only_source(grid)
    fact = _make_fact(
        periods=[_make_period(metric_label="Indeks Lending Standard (ILS)", year=2026, month="Q2")],
        claimed_value=1.03, unit="",
    )
    results = [_evaluate_fact(fact, [src])]
    batch = _MultiSourcePointers(pointers=[
        _MultiCellPointer(source_index=0, query_index=0, found=True, row=1, col=2)
    ])

    new, n = asyncio.run(_pointer_pass([fact], results, [src], _pointer_llm(batch)))

    assert n == 0
    assert new[0].verdict == "Inconclusive"
    assert new[0].resolved_via is None


def test_pointer_pass_non_numeric_cell_keeps_original_inconclusive():
    src = _pointer_only_source(_POINTER_GRID)
    fact = _kpr_fact()
    results = [_evaluate_fact(fact, [src])]
    batch = _MultiSourcePointers(pointers=[
        _MultiCellPointer(source_index=0, query_index=0, found=True, row=2, col=1)
    ])

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
    batch = _MultiSourcePointers(pointers=[
        _MultiCellPointer(source_index=0, query_index=0, found=True, row=1, col=2),  # 2026 Q2 = 42.0
        _MultiCellPointer(source_index=0, query_index=1, found=True, row=1, col=1),  # 2025 Q2 = 40.0
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
    batch = _MultiSourcePointers(pointers=[
        _MultiCellPointer(source_index=0, query_index=0, found=True, row=1, col=1)
    ])

    new, n = asyncio.run(_pointer_pass([fact], results, [src], _pointer_llm(batch)))

    assert n == 1
    assert new[0].verdict == "Refuted"
    assert new[0].resolved_via == "pointer"
    assert "R1K1" in new[0].reasoning


def test_pointer_pass_batches_every_fact_into_one_call():
    src = _pointer_only_source(_POINTER_GRID)
    facts = [
        _kpr_fact(),
        _kpr_fact(periods=[_make_period(metric_label="Lain", year=2026, month="Q2")],
                  claimed_value=99.9),
    ]
    results = [_evaluate_fact(f, [src]) for f in facts]
    batch = _MultiSourcePointers(pointers=[
        _MultiCellPointer(source_index=0, query_index=0, found=True, row=1, col=2),
        _MultiCellPointer(source_index=0, query_index=1, found=True, row=2, col=2),
    ])
    log = []

    new, n = asyncio.run(_pointer_pass(facts, results, [src], _pointer_llm(batch, log)))

    assert len(log) == 1  # one batched call for the whole document
    assert n == 2
    assert all(r.resolved_via == "pointer" for r in new)


def test_pointer_pass_skips_a_source_that_cannot_hold_any_queried_metric():
    # Nothing in this sheet names the metric, so every coordinate it could return would be
    # rejected by pointer_is_plausible — the call is pure cost and must not be made.
    src = _pointer_only_source([["Metrik", "x"], ["Cadangan Devisa", 1.0]])
    fact = _kpr_fact()
    results = [_evaluate_fact(fact, [src])]
    log = []
    batch = _MultiSourcePointers(pointers=[
        _MultiCellPointer(source_index=0, query_index=0, found=True, row=1, col=1)
    ])

    new, n = asyncio.run(_pointer_pass([fact], results, [src], _pointer_llm(batch, log)))

    assert log == []
    assert n == 0
    assert new == results


def test_pointer_pass_asks_each_source_only_about_metrics_it_could_hold():
    # Two sheets, one metric each: neither may be asked about the other's metric, and both
    # facts must still resolve. They now share ONE call, so that filtering shows up as each
    # source's own "asks:" line instead of as a separate call, and the query list is numbered
    # once for the whole document rather than per source.
    kpr_src = _pointer_only_source([["Metrik", "x"], ["KPR/KPA", 40.63]])
    dev_src = _pointer_only_source([["Metrik", "x"], ["Cadangan Devisa", 12.5]])
    facts = [
        _kpr_fact(),
        _kpr_fact(periods=[_make_period(metric_label="Cadangan Devisa", year=2026, month="Q2")],
                  claimed_value=12.5),
    ]
    results = [_evaluate_fact(f, [kpr_src, dev_src]) for f in facts]
    batch = _MultiSourcePointers(pointers=[
        _MultiCellPointer(source_index=0, query_index=0, found=True, row=1, col=1),
        _MultiCellPointer(source_index=1, query_index=1, found=True, row=1, col=1),
    ])
    log = []

    new, n = asyncio.run(
        _pointer_pass(facts, results, [kpr_src, dev_src], _pointer_llm(batch, log))
    )

    assert len(log) == 1
    prompt = log[0].to_string()
    assert "asks: 0\n" in prompt
    assert "asks: 1\n" in prompt
    assert n == 2
    assert [r.verdict for r in new] == ["Entailed", "Entailed"]


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

    batch = _MultiSourcePointers(pointers=[
        _MultiCellPointer(source_index=0, query_index=0, found=True, row=1, col=1)
    ])

    def _structured(schema, **_kw):
        if schema is _MultiSourcePointers:
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
    # The row names its subject, as a real sheet does: a bare 'Posisi' row for "Posisi Kredit"
    # is a breakdown the sheet never mentions (pointer_is_plausible).
    grid = [["Posisi Kredit", 8900.0]]
    src = _pointer_only_source(grid)
    fact = _make_fact(
        periods=[_make_period(metric_label="Posisi Kredit", year=2026, month="Apr")],
        claimed_value=8.9, unit="triliun Rp",
    )
    results = [_evaluate_fact(fact, [src])]
    batch = _MultiSourcePointers(
        units=[_SheetUnit(source_index=0, unit="Miliar Rp")],
        pointers=[_MultiCellPointer(source_index=0, query_index=0, found=True, row=0, col=1)],
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


@patch("paired_verifier.parse_bi_table")
@patch("paired_verifier.parse_for_publication")
def test_cascade_uses_the_publication_parser_first(mock_pub, mock_bi):
    mock_pub.return_value = _make_table(data={("TOTAL", 2026, "Jul"): 454759.8})

    table, parser = _parse_table_with_fallback(b"bytes", "TabI.1", publication="sulni")

    assert parser == "sulni"
    assert table.lookup("TOTAL", 2026, "Jul") == 454759.8
    mock_pub.assert_called_once_with("sulni", b"bytes", "TabI.1")
    mock_bi.assert_not_called()


@pytest.mark.parametrize("failure", [PublicationParseError("kolom bergeser"), IndexError("bug")])
@patch("paired_verifier.parse_bi_table")
@patch("paired_verifier.parse_for_publication")
def test_cascade_falls_back_when_the_publication_parser_fails(mock_pub, mock_bi, failure, caplog):
    mock_pub.side_effect = failure
    mock_bi.return_value = _make_table(data={("Total", 2026, "Jul"): 1.0})

    with caplog.at_level(logging.WARNING):
        table, parser = _parse_table_with_fallback(b"bytes", "TabI.1", publication="sulni")

    assert parser == "bi"
    assert "sulni" in caplog.text


@patch("paired_verifier.parse_bi_table")
@patch("paired_verifier.parse_for_publication")
def test_cascade_reads_an_uncovered_sheet_the_old_way(mock_pub, mock_bi):
    mock_pub.return_value = None
    mock_bi.return_value = _make_table(data={("Total", 2026, "Jul"): 1.0})

    _, parser = _parse_table_with_fallback(b"bytes", "Tbl II.7", publication="sulni")

    assert parser == "bi"


@patch("paired_verifier.parse_bi_table")
@patch("paired_verifier.parse_for_publication")
def test_cascade_without_a_publication_never_asks_a_publication_parser(mock_pub, mock_bi):
    mock_bi.return_value = _make_table(data={("Total", 2026, "Apr"): 1.0})

    _parse_table_with_fallback(b"bytes", "I.1")

    mock_pub.assert_not_called()


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


# ---------------------------------------------------------------------------
# Rounding: a growth computed from printed levels carries TWO uncertainties
# ---------------------------------------------------------------------------

def test_growth_tolerance_adds_the_claims_own_rounding_to_the_level_band():
    """M2-Juni-2026: the report prints 37,4% (yoy) and its own Lampiran levels (799,0 and
    581,3) imply 37,4505%. The claim is a correct rounding of that figure, but the two
    uncertainties were combined with max(): the level band is 0,0204 and never beats the
    claim's own half-step of 0,05, so the pair was judged at 0,05 and Δ = 0,0505 came back
    Tidak Sesuai by half a thousandth of a point. The two are independent, so they add."""
    tolerance = _growth_tolerance(799.0, 581.3)
    delta, verdict = _numeric_verdict(37.4, 37.4505, tolerance)

    assert verdict == "Entailed", f"delta={delta} tolerance={tolerance}"


def test_growth_tolerance_covers_the_giro_case_too():
    """Same document, same shape: 12,6% claimed against 12,6537% computed from 852,0 / 756,3."""
    tolerance = _growth_tolerance(852.0, 756.3)
    _, verdict = _numeric_verdict(12.6, 12.6537, tolerance)

    assert verdict == "Entailed"


def test_growth_tolerance_still_refuses_a_figure_rounding_cannot_explain():
    """The widened band must stay a rounding allowance, not a blanket amnesty."""
    tolerance = _growth_tolerance(799.0, 581.3)
    _, verdict = _numeric_verdict(36.9, 37.4505, tolerance)

    assert verdict == "Refuted"


def test_growth_tolerance_on_a_large_base_stays_tight():
    """The level band shrinks as the base grows, so an ordinary claim is judged as before."""
    tolerance = _growth_tolerance(10432.8, 9595.3)

    assert tolerance < 0.06


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


# ---------------------------------------------------------------------------
# Best-matching-source selection, collapsed-BI rejection, unnamed-subset guard
# ---------------------------------------------------------------------------

def test_evaluate_fact_prefers_the_breakdown_row_over_an_earlier_aggregate_source():
    # Both sources resolve the claim; the first holds only the national aggregate. Taking
    # whichever source came first checked a per-group claim against the national series.
    national = _make_source(
        _make_table(title="Indeks", unit="", data={
            ("Indeks Keyakinan Konsumen (IKK)", 2026, "Jun"): 117.8,
        }),
        sheet="Tabel 1",
    )
    per_group = _make_source(
        _make_table(title="Indeks per Kelompok", unit="", data={
            ("Indeks Keyakinan Konsumen (IKK) > Pengeluaran >Rp5 juta", 2026, "Jun"): 121.4,
        }),
        sheet="Tabel 2",
    )
    fact = _make_fact(
        claimed_value=121.4, unit=None,
        periods=[_make_period(
            metric_label="Indeks Keyakinan Konsumen (IKK) > Pengeluaran >Rp5 juta",
            year=2026, month="Jun",
        )],
    )

    result = _evaluate_fact(fact, [national, per_group])

    assert result.verdict == "Entailed"
    assert result.matched_excel_source.endswith("Tabel 2")


def test_evaluate_fact_prefers_the_national_row_over_a_per_city_source():
    per_city = _make_source(
        _make_table(title="Indeks per Kota", unit="", data={
            ("5. Medan > Indeks Kondisi Ekonomi Saat Ini (IKE)", 2026, "Jun"): 86.2,
        }),
        sheet="Tabel 6",
    )
    national = _make_source(
        _make_table(title="Indeks", unit="", data={
            ("Indeks Kondisi Ekonomi Saat Ini (IKE)", 2026, "Jun"): 109.2,
        }),
        sheet="Tabel 1",
    )
    fact = _make_fact(
        claimed_value=109.2, unit=None,
        periods=[_make_period(
            metric_label="Indeks Kondisi Ekonomi Saat Ini (IKE)", year=2026, month="Jun",
        )],
    )

    result = _evaluate_fact(fact, [per_city, national])

    assert result.verdict == "Entailed"
    assert result.matched_excel_source.endswith("Tabel 1")


def test_evaluate_fact_keeps_the_first_source_when_scores_tie():
    data = {("Total", 2026, "Apr"): 10355.1}
    first = _make_source(_make_table(data=data), sheet="I.1")
    second = _make_source(_make_table(data=data), sheet="I.2")

    result = _evaluate_fact(_make_fact(), [first, second])

    assert result.matched_excel_source.endswith("I.1")


@patch("paired_verifier.parse_generic_table")
@patch("paired_verifier.parse_bi_table")
def test_cascade_rejects_a_bi_parse_with_duplicate_row_labels(mock_bi, mock_generic):
    # Repeated labels mean the BI reader missed the column holding the row hierarchy, so
    # every group silently collapsed onto the first one's values.
    collapsed = _make_table(data={("IKK", 2026, "Jun"): 1.0})
    collapsed.row_labels.append("IKK")
    mock_bi.return_value = collapsed
    mock_generic.return_value = _make_table(data={
        ("Jakarta > IKK", 2026, "Jun"): 1.0, ("Bandung > IKK", 2026, "Jun"): 2.0,
    })

    _, parser = _parse_table_with_fallback(b"bytes", "Tabel 6")

    assert parser == "generic"


@patch("paired_verifier.parse_generic_table")
@patch("paired_verifier.parse_bi_table")
def test_cascade_keeps_collapsed_bi_result_when_generic_also_fails(mock_bi, mock_generic):
    collapsed = _make_table(data={("IKK", 2026, "Jun"): 1.0})
    collapsed.row_labels.append("IKK")
    mock_bi.return_value = collapsed
    mock_generic.side_effect = ValueError("no structure")

    _, parser = _parse_table_with_fallback(b"bytes", "Tabel 6")

    assert parser == "bi"


@pytest.mark.parametrize("quote", [
    "Sementara itu, beberapa kota lainnya mencatatkan kenaikan IEK.",
    "Secara spasial, sebagian besar kota mengalami penurunan IKE.",
    "Sedangkan pada kelompok pengeluaran lainnya, proporsi konsumsi terindikasi menurun.",
])
def test_trend_about_an_unnamed_subset_is_inconclusive_not_refuted(quote):
    source = _make_source(_make_table(title="Indeks", unit="", data={
        ("Indeks Ekspektasi Konsumen (IEK)", 2026, "May"): 129.7,
        ("Indeks Ekspektasi Konsumen (IEK)", 2026, "Jun"): 126.4,
    }))
    fact = _make_fact(
        operation="is_increasing", claimed_value=None, unit=None, context_quote=quote,
        periods=[
            _make_period(metric_label="Indeks Ekspektasi Konsumen (IEK)", year=2026, month="May"),
            _make_period(metric_label="Indeks Ekspektasi Konsumen (IEK)", year=2026, month="Jun"),
        ],
    )

    result = _evaluate_fact(fact, [source])

    assert result.verdict == "Inconclusive"


def test_trend_naming_its_member_is_still_verified_from_the_same_sentence():
    # The per-city facts split out of a 'sebagian besar kota …' sentence carry the city in
    # their metric label, so the unnamed-subset guard must not swallow them too.
    source = _make_source(_make_table(title="Indeks per Kota", unit="", data={
        ("9. Makassar > Indeks Kondisi Ekonomi Saat Ini (IKE)", 2026, "May"): 117.2,
        ("9. Makassar > Indeks Kondisi Ekonomi Saat Ini (IKE)", 2026, "Jun"): 99.3,
    }))
    label = "Indeks Kondisi Ekonomi Saat Ini (IKE) > Makassar"
    fact = _make_fact(
        operation="is_decreasing", claimed_value=None, unit=None,
        context_quote=("Secara spasial, sebagian besar kota mengalami penurunan IKE "
                       "terutama di Makassar, Banten, dan Medan."),
        periods=[
            _make_period(metric_label=label, year=2026, month="May"),
            _make_period(metric_label=label, year=2026, month="Jun"),
        ],
    )

    result = _evaluate_fact(fact, [source])

    assert result.verdict == "Entailed"


def test_a_city_named_first_in_its_row_is_named_by_the_sentence():
    # SK Juli 2026 s026: the city tables put the city first, and the guard looked only at the
    # last level ('IEK', not spelled out in full) — Bandung was left Tidak Cukup Data.
    label = "2. Bandung > Indeks Ekspektasi Konsumen (IEK)"
    source = _make_source(_make_table(title="Indeks per Kota", unit="", data={
        (label, 2026, "Jun"): 128.2, (label, 2026, "Jul"): 115.4}))
    fact = _make_fact(
        operation="is_decreasing", claimed_value=None, unit=None,
        context_quote=("Namun, penurunan indeks pada sejumlah kota, terutama di Bandung, Pontianak, "
                       "dan Samarinda menyebabkan IEK lebih rendah dibandingkan dengan bulan sebelumnya."),
        periods=[_make_period(metric_label=label, year=2026, month="Jun"),
                 _make_period(metric_label=label, year=2026, month="Jul")],
    )

    assert _evaluate_fact(fact, [source]).verdict == "Entailed"
    # A city the sentence does not name stays a stand-in.
    jakarta = "1. Jakarta > Indeks Ekspektasi Konsumen (IEK)"
    from paired_verifier import _quote_names_qualifier
    assert not _quote_names_qualifier(jakarta, fact.context_quote)


def test_a_member_named_after_its_group_noun_is_not_an_unnamed_subset():
    # SPE Juni 2026 s014: "kelompok lainnya …, yaitu pada Kelompok Makanan, Minuman, dan Tembakau"
    # lists the members of its "lainnya".
    row = "Makanan, Minuman, & Tembakau"
    source = _make_source(_make_table(title="Pertumbuhan Tahunan", unit="", data={(row, 2026, "Jun"): -3.6}))
    fact = _make_fact(
        operation="below_threshold", claimed_value=0.0, unit=None,
        context_quote=("Sementara itu, kelompok lainnya mencatatkan perbaikan meski masih berada dalam fase "
                       "kontraksi, yaitu pada Kelompok Makanan, Minuman, dan Tembakau (indeks 318,4; "
                       "terkontraksi 3,6% (yoy)) serta Subkelompok Sandang."),
        periods=[_make_period(metric_label=row, year=2026, month="Jun")],
    )

    assert _evaluate_fact(fact, [source]).verdict == "Entailed"
    # Two sentences run together: "… serta Kelompok Dari sisi harga, tekanan inflasi …" names no
    # group 'tekanan inflasi'.
    from paired_verifier import _member_named
    assert not _member_named("tekanan inflasi", "serta Kelompok Dari sisi harga, tekanan inflasi menurun")


def test_growth_of_some_groups_beside_the_fall_of_another_keeps_the_extractors_direction():
    # SPE Juli 2026: "penurunan penjualan Kelompok Barang Lainnya di tengah pertumbuhan penjualan
    # Kelompok Peralatan Informasi dan Komunikasi" was read as "turun" for the growing group.
    row = "Peralatan Informasi dan Komunikasi"
    source = _make_source(_make_table(title="Tabel 1. IPR", unit="Indeks",
                                      data={(row, 2026, "Jul"): 68.4, (row, 2026, "Aug"): 70.1}))
    fact = _make_fact(
        operation="is_increasing", claimed_value=None, unit=None,
        context_quote=("Perkembangan tersebut dipengaruhi terutama oleh penurunan penjualan Kelompok Barang "
                       "Lainnya di tengah pertumbuhan penjualan Kelompok Peralatan Informasi dan Komunikasi."),
        periods=[_make_period(metric_label=row, month="Jul"), _make_period(metric_label=row, month="Aug")],
    )

    assert _evaluate_fact(fact, [source]).verdict == "Entailed"


def _ipdg_source():
    return _make_source(_make_table(title="Indeks per Kelompok Pengeluaran", unit="", data={
        ("Indeks Pembelian Barang Tahan Lama (IPDG) > Pengeluaran Rp2,1 - 3 juta", 2026, "Jun"): 99.0,
        ("Indeks Pembelian Barang Tahan Lama (IPDG) > Pengeluaran >Rp5 juta", 2026, "Jun"): 108.8,
    }), sheet="Tabel 2")


def test_threshold_claim_about_an_unnamed_subset_is_inconclusive_not_refuted():
    # Four of the five expenditure groups sit above 100, so "sebagian besar" is TRUE. The
    # extractor pinned the claim to the one group that does not, and the threshold check
    # Refuted it for being exactly the exception the sentence allows for. The subset guard
    # used to cover trends only, and its QUAL_SEP test would have waved this through anyway
    # because the invented qualifier looks just like an author-named one.
    fact = _make_fact(
        operation="above_threshold", claimed_value=100.0, unit=None,
        context_quote=("Dari sisi pengeluaran, IPDG berada pada level optimis pada "
                       "sebagian besar kelompok pengeluaran."),
        periods=[_make_period(
            metric_label="Indeks Pembelian Barang Tahan Lama (IPDG) > Pengeluaran Rp2,1 - 3 juta",
            year=2026, month="Jun",
        )],
    )

    result = _evaluate_fact(fact, [_ipdg_source()])

    assert result.verdict == "Inconclusive"


def test_threshold_claim_naming_its_group_is_still_verified_from_a_hedged_sentence():
    # The counterpart guard: when the sentence NAMES the group, the qualifier is the author's
    # and the claim really is about that row — the widened guard must not swallow it.
    fact = _make_fact(
        operation="above_threshold", claimed_value=100.0, unit=None,
        context_quote=("IPDG optimis pada sebagian besar kelompok pengeluaran, "
                       "tertinggi pada Pengeluaran >Rp5 juta."),
        periods=[_make_period(
            metric_label="Indeks Pembelian Barang Tahan Lama (IPDG) > Pengeluaran >Rp5 juta",
            year=2026, month="Jun",
        )],
    )

    result = _evaluate_fact(fact, [_ipdg_source()])

    assert result.verdict == "Entailed"
    assert result.computed_value == pytest.approx(108.8)


def test_a_source_matching_only_the_qualifier_is_not_reported_beside_the_winner():
    # The respondent-profile sheet carries a bare 'Rp2,1 - 3 juta' row holding a SHARE of
    # respondents, which fuzzy-resolves the same claim as the index sheet. Printing its 17,9
    # next to the index's 99,0 invited doubt about a number that was never in question.
    profile = _make_source(_make_table(title="Profil Responden", unit="", data={
        ("Rp2,1 - 3 juta", 2026, "Jun"): 17.9,
    }), sheet="Tabel 7")
    fact = _make_fact(
        claimed_value=99.0, unit=None,
        periods=[_make_period(
            metric_label="Indeks Pembelian Barang Tahan Lama (IPDG) > Pengeluaran Rp2,1 - 3 juta",
            year=2026, month="Jun",
        )],
    )

    result = _evaluate_fact(fact, [_ipdg_source(), profile])

    assert result.verdict == "Entailed"
    assert result.matched_excel_source.endswith("Tabel 2")
    assert [sv.source for sv in result.source_values] == []
    assert "17.9" not in result.reasoning


# ---------------------------------------------------------------------------
# yoy_growth against a table that ALREADY holds growth figures
# ---------------------------------------------------------------------------

def test_yoy_claim_reads_a_growth_table_instead_of_computing_over_it():
    # Lampiran 2 of the M2 report is in '%, yoy'. Computing growth over it gave growth OF a
    # growth: 15,7% claimed against a computed 56,3%, a Refuted verdict on a correct sentence.
    table = _make_table(
        title="Lampiran 2. Pertumbuhan Uang Beredar", unit="%, yoy",
        data={("Uang Kartal", 2026, "Apr"): 15.7, ("Uang Kartal", 2025, "Apr"): 8.7},
    )
    fact = _make_fact(
        operation="yoy_growth", claimed_value=15.7, unit="persen",
        periods=[_make_period(metric_label="Uang Kartal")],
    )

    result = _evaluate_fact(fact, [_make_source(table)])

    assert result.verdict == "Entailed"
    assert result.computed_value == 15.7
    assert "dibaca langsung" in result.reasoning
    # The prior-year cell exists but must not have been used as a denominator.
    assert len(result.periods) == 1


def test_yoy_claim_is_not_answered_by_a_month_on_month_table():
    # SPE Tabel 3 holds mtm growth. A yoy claim computed over it gave growth OF an mtm growth
    # (97,6%) — and reading the cell would answer a yoy question with an mtm figure.
    table = _make_table(
        title="Tabel 3. Pertumbuhan Bulanan Indeks Penjualan Riil (%, mtm)", unit="%, mtm",
        data={("INDEKS TOTAL", 2026, "Jul"): -0.1, ("INDEKS TOTAL", 2025, "Jul"): -4.1},
    )
    fact = _make_fact(
        operation="yoy_growth", claimed_value=-0.1, unit="persen_yoy",
        periods=[_make_period(metric_label="INDEKS TOTAL", month="Jul")],
    )

    result = _evaluate_fact(fact, [_make_source(table)])

    assert result.verdict == "Inconclusive"
    assert "mtm" in result.reasoning


def _band_table():
    # SHPR Tabel 3: one sheet, a qtq band and a yoy band, the band named in every row label.
    return _make_table(
        title="Tabel 3. Pertumbuhan Harga Properti Residensial", unit="%, qtq & yoy",
        data={
            ("TRIWULANAN (QTQ) > BANJARMASIN > TOTAL", 2026, "Q2"): 1.28,
            ("TRIWULANAN (QTQ) > BANJARMASIN > TOTAL", 2025, "Q2"): 0.51,
            ("TAHUNAN (YOY) > BANJARMASIN > TOTAL", 2026, "Q2"): 1.29,
            ("TAHUNAN (YOY) > BANJARMASIN > TOTAL", 2025, "Q2"): 2.25,
        },
    )


def test_yoy_claim_reads_the_yoy_band_of_a_mixed_growth_table():
    fact = _make_fact(
        operation="yoy_growth", claimed_value=1.29, unit="persen_yoy",
        periods=[_make_period(metric_label="TAHUNAN (YOY) > BANJARMASIN > TOTAL", month="Q2")],
    )

    result = _evaluate_fact(fact, [_make_source(_band_table())])

    assert result.verdict == "Entailed"
    assert result.computed_value == 1.29


def test_yoy_claim_is_not_answered_by_the_qtq_band_of_a_mixed_growth_table():
    fact = _make_fact(
        operation="yoy_growth", claimed_value=1.29, unit="persen_yoy",
        periods=[_make_period(metric_label="TRIWULANAN (QTQ) > BANJARMASIN > TOTAL", month="Q2")],
    )

    result = _evaluate_fact(fact, [_make_source(_band_table())])

    assert result.verdict == "Inconclusive"
    assert "qtq" in result.reasoning


def _spe_growth_tables():
    yoy = _make_table(title="Tabel 2. Pertumbuhan Tahunan IPR (%, yoy)", unit="%, yoy",
                      data={("INDEKS TOTAL", 2026, "Jul"): 1.1})
    mtm = _make_table(title="Tabel 3. Pertumbuhan Bulanan IPR (%, mtm)", unit="%, mtm",
                      data={("INDEKS TOTAL", 2026, "Jul"): -0.1})
    return _make_source(yoy, sheet="Tabel 2"), _make_source(mtm, sheet="Tabel 3")


def _rate_fact(claimed, quote):
    return _make_fact(
        periods=[_make_period(metric_label="INDEKS TOTAL", month="Jul")],
        claimed_value=claimed, unit="persen", context_quote=quote,
    )


def test_an_mtm_rate_is_answered_by_the_mtm_table_not_the_yoy_one():
    # SPE E2E: "terkontraksi sebesar 0,1% (mtm)" was answered by Tabel 2 (yoy, 1,1).
    yoy, mtm = _spe_growth_tables()
    quote = "Pada Juli 2026, IPR tumbuh sebesar 1,1% (yoy) dan terkontraksi sebesar 0,1% (mtm)."

    result = _evaluate_fact(_rate_fact(-0.1, quote), [yoy, mtm])

    assert result.verdict == "Entailed"
    assert result.matched_excel_source == "TABEL1_1.xls / Tabel 3"


def test_an_mtm_rate_is_not_answered_by_a_yoy_table_alone():
    yoy, _ = _spe_growth_tables()

    result = _evaluate_fact(_rate_fact(-0.1, "terkontraksi sebesar 0,1% (mtm)"), [yoy])

    assert result.verdict == "Inconclusive"
    assert "mtm" in result.reasoning


def test_the_yoy_rate_in_the_same_sentence_still_reads_the_yoy_table():
    yoy, mtm = _spe_growth_tables()
    quote = "Pada Juli 2026, IPR tumbuh sebesar 1,1% (yoy) dan terkontraksi sebesar 0,1% (mtm)."

    result = _evaluate_fact(_rate_fact(1.1, quote), [mtm, yoy])

    assert result.verdict == "Entailed"
    assert result.matched_excel_source == "TABEL1_1.xls / Tabel 2"


def test_a_contraction_of_growth_that_eased_is_judged_on_the_yoy_rate():
    # SULNI Agustus 2026 s002: the private debt level ROSE (192,5 -> 194,6 ribu juta), but the
    # sentence is about its yoy rate: −1,35% in Q1, −0,57% in Q2.
    table = _make_table(title="Posisi ULN", unit="Juta USD", data={
        ("Swasta", 2025, "Mar"): 195160.0, ("Swasta", 2026, "Mar"): 192525.57,
        ("Swasta", 2025, "Jun"): 195733.6, ("Swasta", 2026, "Jun"): 194616.52})
    quote = ("Perkembangan tersebut disebabkan oleh peningkatan ULN publik, baik pemerintah maupun bank "
             "sentral, di tengah kontraksi pertumbuhan ULN swasta yang lebih rendah.")
    fact = _make_fact(operation="is_decreasing", claimed_value=None, unit=None, context_quote=quote,
                      periods=[_make_period(metric_label="Swasta", month="Mar"),
                               _make_period(metric_label="Swasta", month="Jun")])

    result = _evaluate_fact(fact, [_make_source(table)])

    assert result.verdict == "Entailed"
    assert result.computed_value == pytest.approx(-0.57)
    # The public debt of the same sentence is not the contracting row: its own trend stands.
    from paired_verifier import _contraction_of_growth
    public = _make_fact(operation="is_increasing", claimed_value=None, unit=None, context_quote=quote,
                        periods=[_make_period(metric_label="Publik", month="Mar"),
                                 _make_period(metric_label="Publik", month="Jun")])
    assert _contraction_of_growth(public, [_make_source(table)]) is None


def test_a_bound_in_months_is_not_judged_on_money():
    # Cadev ORA: "di atas standar kecukupan internasional sekitar 3 bulan impor" against the
    # reserves themselves (146.320 juta USD) came back Sesuai.
    table = _make_table(title="Official Reserve Assets", unit="Juta USD",
                        data={("Cadangan Devisa", 2026, "Sep"): 146320.43})
    fact = _make_fact(operation="above_threshold", claimed_value=3.0, unit="bulan",
                      periods=[_make_period(metric_label="Cadangan Devisa", month="Sep")],
                      context_quote="… serta berada di atas standar kecukupan internasional sekitar 3 bulan impor.")

    result = _evaluate_fact(fact, [_make_source(table)])

    assert result.verdict == "Inconclusive" and result.reason_code == "jenis"


def test_a_trend_without_a_basis_is_not_refuted_when_the_yearly_reading_holds():
    # SPE Juni 2026 s018: "Pada Juli 2026, IPR diprakirakan meningkat." Jun 225,0 -> Jul 224,4 (down
    # month on month), Jul 2025 222,3 -> 224,4 (up year on year, the release's reading).
    table = _make_table(title="Tabel 1. IPR", unit="Indeks", data={
        ("INDEKS TOTAL", 2025, "Jul"): 222.3, ("INDEKS TOTAL", 2026, "Jun"): 225.0,
        ("INDEKS TOTAL", 2026, "Jul"): 224.4})

    def trend(quote):
        return _make_fact(operation="is_increasing", claimed_value=None, unit=None, context_quote=quote,
                          periods=[_make_period(metric_label="INDEKS TOTAL", month="Jul")])

    unbased = _evaluate_fact(trend("Pada Juli 2026, IPR diprakirakan meningkat."), [_make_source(table)])
    assert unbased.verdict == "Inconclusive"
    assert "Jul 2025" in unbased.reasoning
    # A sentence that names its basis is judged on it.
    based = _evaluate_fact(trend("IPR Juli 2026 meningkat dibandingkan bulan sebelumnya."), [_make_source(table)])
    assert based.verdict == "Refuted"


def test_a_rate_said_to_fall_matches_the_negative_cell():
    # SPE Juni 2026 s008: "lebih baik dibandingkan Juli 2025 yang turun 4,1% (mtm)", Tabel 3 −4,1.
    mtm = _make_source(_make_table(title="Tabel 3. Pertumbuhan Bulanan IPR (%, mtm)", unit="%, mtm",
                                   data={("INDEKS TOTAL", 2025, "Jul"): -4.1}), sheet="Tabel 3")
    quote = "Meski demikian, kinerja penjualan pada Juli 2026 tersebut lebih baik dibandingkan Juli 2025 yang turun 4,1% (mtm)."
    fact = _make_fact(periods=[_make_period(metric_label="INDEKS TOTAL", year=2025, month="Jul")],
                      claimed_value=4.1, unit="persen", context_quote=quote)

    assert _evaluate_fact(fact, [mtm]).verdict == "Entailed"
    # Without the fall word next to the number, a positive claim still differs from −4,1.
    plain = _make_fact(periods=[_make_period(metric_label="INDEKS TOTAL", year=2025, month="Jul")],
                       claimed_value=4.1, unit="persen", context_quote="Penjualan Juli 2025 tumbuh 4,1% (mtm).")
    assert _evaluate_fact(plain, [mtm]).verdict == "Refuted"


def test_yoy_claim_against_a_levels_table_still_computes_growth():
    table = _make_table(
        unit="triliun Rp",
        data={("Uang Kartal", 2026, "Apr"): 1186.3, ("Uang Kartal", 2025, "Apr"): 1025.3},
    )
    fact = _make_fact(
        operation="yoy_growth", claimed_value=15.7, unit="persen",
        periods=[_make_period(metric_label="Uang Kartal")],
    )

    result = _evaluate_fact(fact, [_make_source(table)])

    assert result.verdict == "Entailed"
    assert result.computed_value == pytest.approx(15.7, abs=0.05)
    assert len(result.periods) == 2


# ---------------------------------------------------------------------------
# YoY guards added with the snippet-table split
# ---------------------------------------------------------------------------

def test_growth_tolerance_widens_only_where_rounding_actually_bites():
    from paired_verifier import MATCH_TOLERANCE, _growth_tolerance

    # 7,6 against 7,5: each is printed to a tenth, so the growth they imply carries more than
    # a point of slack and the report's own 1,5% is inside it.
    assert _growth_tolerance(7.6, 7.5) > 1.0
    # On a base three orders of magnitude larger the levels' own rounding is worth a thousandth
    # of a point, so the tolerance stays within a hair of the ordinary one — this must not
    # become a blanket loosening. (It is no longer EQUAL to it: the levels' band is added to the
    # claim's own rounding rather than replacing it, so that neither uncertainty is discarded.)
    assert MATCH_TOLERANCE < _growth_tolerance(10253.7, 9387.9) < MATCH_TOLERANCE + 0.01


def test_a_growth_printed_to_two_decimals_gets_its_own_rounding_only():
    # SHPR Tw IV-2025: "Palembang … 1,06% (yoy)" against 108,5555 / 107,3956 = 1,0800%. A figure
    # printed to two decimals allows 0,005 of its own: 0,02 is a real error (0,05 let it through).
    table = _make_table(title="Indeks Harga Properti Residensial", unit="Indeks (2018=100)",
                        data={("PALEMBANG > TOTAL", 2024, "Q4"): 107.3956, ("PALEMBANG > TOTAL", 2025, "Q4"): 108.5555})

    def claim(value, quote):
        return _make_fact(operation="yoy_growth", claimed_value=value, unit="persen", context_quote=quote,
                          periods=[_make_period(metric_label="PALEMBANG > TOTAL", year=2025, month="Q4")])

    assert _evaluate_fact(claim(1.06, "IHPR Palembang tumbuh 1,06% (yoy)."), [_make_source(table)]).verdict == "Refuted"
    assert _evaluate_fact(claim(1.08, "IHPR Palembang tumbuh 1,08% (yoy)."), [_make_source(table)]).verdict == "Entailed"
    # Printed to one decimal, the old allowance stands: 1,1 for 1,08.
    assert _evaluate_fact(claim(1.1, "IHPR Palembang tumbuh 1,1% (yoy)."), [_make_source(table)]).verdict == "Entailed"


def test_yoy_refuses_a_prior_year_value_equal_to_the_claimed_growth():
    from paired_verifier import _compute_yoy_growth, _ExcelSource
    from structured_extractor import ExtractedFact, PeriodPoint
    from table_model import TableData

    table = TableData(title="Tabel 9", unit="triliun Rp", row_labels=["M0"])
    table._data[("M0", 2026, "Apr")] = 2232.2
    table._data[("M0", 2025, "Apr")] = 14.3          # actually the '%, yoy' cell, mis-read
    src = _ExcelSource(table=table, filename="report.pdf", sheet="Tabel 9")
    fact = ExtractedFact(
        operation="yoy_growth", periods=[PeriodPoint(metric_label="M0", year=2026, month="Apr")],
        claimed_value=14.3, unit="persen_yoy",
        context_quote="Uang Primer (M0) adjusted ... tumbuh 14,3% (yoy)",
    )
    result = _compute_yoy_growth(fact, [("M0", 2232.2)], src)

    # 2.232,2 / 14,3 would have been reported as 15.509% yoy against a claim of 14,3%.
    assert result.verdict == "Inconclusive"
    assert "yoy" in result.reasoning.lower()


# ---------------------------------------------------------------------------
# Same row name, different statistical universe (M0 vs M2)
# ---------------------------------------------------------------------------

def _m0_m2_sources(row, m2_value, m0_value, prior_m2=None, prior_m0=None):
    """One M2 table and one M0 table carrying the SAME row name, as BI's reports do."""
    from paired_verifier import _ExcelSource
    from table_model import TableData

    out = []
    for title, value, prior in (
        ("Lampiran 1. Tabel Uang Beredar dan Faktor-Faktor yang Memengaruhinya", m2_value, prior_m2),
        ("Lampiran 6. Tabel Uang Primer dan Faktor-Faktor yang Memengaruhinya", m0_value, prior_m0),
    ):
        table = TableData(title=title, unit="Triliun Rp", row_labels=[row])
        table._data[(row, 2026, "Apr")] = value
        if prior is not None:
            table._data[(row, 2025, "Apr")] = prior
        out.append(_ExcelSource(table=table, filename="report.pdf", sheet=title[:20], origin="pdf"))
    return out


def test_m0_table_does_not_contradict_an_m2_table_about_a_different_quantity():
    from structured_extractor import ExtractedFact, PeriodPoint

    # 'Tagihan Bersih kepada Pemerintah Pusat' is +838,0 for the monetary system and -246,7 on
    # Bank Indonesia's own balance sheet. Opposite signs: one row name, two quantities.
    sources = _m0_m2_sources("Tagihan Bersih kepada Pemerintah Pusat", 838.0, -246.7,
                             prior_m2=604.4, prior_m0=-561.5)
    fact = ExtractedFact(
        operation="yoy_growth",
        periods=[PeriodPoint(metric_label="Tagihan Bersih kepada Pemerintah Pusat",
                             year=2026, month="Apr")],
        claimed_value=38.6, unit="persen_yoy", context_quote="x",
    )
    result = _evaluate_fact(fact, sources)

    assert result.verdict == "Entailed"
    assert result.source_conflict is None, "M0 and M2 measure different things here"
    # The unrelated series is not shown beside the answer either.
    assert all("Uang Primer" not in sv.source for sv in result.source_values or [])


def test_m0_table_still_contradicts_an_m2_table_when_the_numbers_are_close():
    from structured_extractor import ExtractedFact, PeriodPoint

    # Uang kartal is 1.186,3 in Lampiran 1 and 1.195,6 in Lampiran 6 — the same series compiled
    # on a different basis. A 9,3 T gap is a real thing for a reviewer to know about, so the
    # cross-universe rule must not swallow it.
    sources = _m0_m2_sources("Uang Kartal di Luar Bank Umum dan BPR", 1186.3, 1195.6)
    fact = ExtractedFact(
        operation="value",
        periods=[PeriodPoint(metric_label="Uang Kartal di Luar Bank Umum dan BPR",
                             year=2026, month="Apr")],
        claimed_value=1186.3, unit="triliun Rp", context_quote="x",
    )
    result = _evaluate_fact(fact, sources)

    assert result.verdict == "Entailed"
    assert result.source_conflict == "internal"


def test_a_growth_table_cannot_answer_a_level_claim():
    from paired_verifier import _ExcelSource
    from structured_extractor import ExtractedFact, PeriodPoint
    from table_model import TableData

    # The '%, yoy' half of a snippet table holds 15,7 for this row. Converting that into a
    # 'triliun Rp' claim produced 15,7 / 1e12 = 0,0 and reported it as a second opinion.
    growth = TableData(title="Tabel 1. Uang Beredar dan Komponennya (%, yoy)", unit="%, yoy",
                       row_labels=["Uang Kartal"])
    growth._data[("Uang Kartal", 2026, "Apr")] = 15.7
    fact = ExtractedFact(
        operation="value",
        periods=[PeriodPoint(metric_label="Uang Kartal", year=2026, month="Apr")],
        claimed_value=1186.3, unit="triliun Rp", context_quote="x",
    )
    result = _evaluate_fact(
        fact, [_ExcelSource(table=growth, filename="r.pdf", sheet="T1 yoy", origin="pdf")]
    )

    assert result.verdict == "Inconclusive"
    assert result.computed_value != 0.0


def test_a_percent_claim_still_reaches_a_percent_sheet():
    from paired_verifier import _ExcelSource
    from structured_extractor import ExtractedFact, PeriodPoint
    from table_model import TableData

    # Regression guard for the PMI case the unit fallback exists for: a 'persen' claim carries
    # no numeric scale, so the growth-table rule above must not fire on it.
    sheet = TableData(title="PMI-BI", unit="%, Indeks", row_labels=["PMI"])
    sheet._data[("PMI", 2026, "Apr")] = 51.43
    fact = ExtractedFact(
        operation="value", periods=[PeriodPoint(metric_label="PMI", year=2026, month="Apr")],
        claimed_value=51.43, unit="persen", context_quote="x",
    )
    result = _evaluate_fact(
        fact, [_ExcelSource(table=sheet, filename="r.xlsx", sheet="PMI", origin="excel")]
    )
    assert result.verdict == "Entailed"


def test_a_unit_word_split_by_a_stray_space_still_carries_its_scale():
    from paired_verifier import _parse_scale_unit, _unit_factor

    # Tabel 7 of sample_data/M2-Juli-2026.pdf is captioned '(t riliun Rp)'. Read word by word
    # that has no scale term at all, so the unit degraded to plain rupiah and a triliun claim
    # was compared against a number 1e12 too large.
    assert _parse_scale_unit("t riliun Rp") == (1e12, "rp")
    assert _unit_factor("triliun Rp", "t riliun Rp") == pytest.approx(1.0)
    assert _unit_factor("triliun Rp", "m iliar Rp") == pytest.approx(1000.0)
    # A percentage still has no scale to convert to.
    assert _parse_scale_unit("%, yoy") is None


def test_a_trend_about_growth_is_checked_against_the_growth_table():
    from paired_verifier import _ExcelSource
    from structured_extractor import ExtractedFact, PeriodPoint
    from table_model import TableData

    # "pertumbuhan giro meningkat sebesar 10,5% (yoy) dari 10,2% (yoy)" says the RATE rose. Giro's
    # level fell over those same two months (3.087,8 -> 3.055,6), so checking the claim against
    # the levels table answers a different question and calls a true sentence Tidak Sesuai.
    levels = TableData(title="Tabel 4. Penghimpunan Dana Pihak Ketiga", unit="triliun Rp",
                       row_labels=["Giro"])
    levels._data.update({("Giro", 2026, "Jun"): 3087.8, ("Giro", 2026, "Jul"): 3055.6})
    growth = TableData(title="Tabel 4. Penghimpunan Dana Pihak Ketiga (%, yoy)", unit="%, yoy",
                       row_labels=["Giro"])
    growth._data.update({("Giro", 2026, "Jun"): 10.2, ("Giro", 2026, "Jul"): 10.5})
    sources = [
        _ExcelSource(table=levels, filename="r.pdf", sheet="T4", origin="pdf"),
        _ExcelSource(table=growth, filename="r.pdf", sheet="T4 yoy", origin="pdf"),
    ]

    def trend(metric):
        fact = ExtractedFact(
            operation="is_increasing",
            periods=[PeriodPoint(metric_label=metric, year=2026, month=m) for m in ("Jun", "Jul")],
            claimed_value=None, unit=None, context_quote="x",
        )
        return _evaluate_fact(fact, sources)

    assert trend("pertumbuhan giro").verdict == "Entailed"
    # A claim about the LEVEL still reads the levels, in source order as before.
    assert trend("giro").periods[0].excel_value == pytest.approx(3087.8)


def test_a_trend_sentence_comparing_two_growth_rates_reads_the_growth_table():
    from paired_verifier import _ExcelSource
    from structured_extractor import ExtractedFact, PeriodPoint
    from table_model import TableData

    # The metric here is a bare 'tabungan' — it is the SENTENCE that says the comparison is
    # between growth rates: "meningkat dibandingkan pertumbuhan pada bulan sebelumnya ...
    # sebesar 8,9% (yoy)". Tabungan's level fell over those months, so the levels table calls a
    # true sentence Tidak Sesuai.
    levels = TableData(title="Tabel 3. DPK", unit="triliun Rp", row_labels=["Tabungan"])
    levels._data.update({("Tabungan", 2026, "Mar"): 3166.2, ("Tabungan", 2026, "Apr"): 3160.4})
    growth = TableData(title="Tabel 3. DPK (%, yoy)", unit="%, yoy", row_labels=["Tabungan"])
    growth._data.update({("Tabungan", 2026, "Mar"): 8.4, ("Tabungan", 2026, "Apr"): 8.9})
    sources = [
        _ExcelSource(table=levels, filename="r.pdf", sheet="T3", origin="pdf"),
        _ExcelSource(table=growth, filename="r.pdf", sheet="T3 yoy", origin="pdf"),
    ]

    def trend(quote):
        fact = ExtractedFact(
            operation="is_increasing",
            periods=[PeriodPoint(metric_label="tabungan", year=2026, month=m)
                     for m in ("Mar", "Apr")],
            claimed_value=None, unit=None, context_quote=quote,
        )
        return _evaluate_fact(fact, sources)

    assert trend("tabungan meningkat dibandingkan pertumbuhan pada bulan "
                 "sebelumnya sebesar 8,9% (yoy)").verdict == "Entailed"
    # 'tumbuh' alone states a level and its growth side by side; it is not a comparison of
    # rates, so the levels table still answers.
    assert trend("Posisi tabungan tercatat Rp3.160,4 triliun, atau tumbuh 8,9% "
                 "(yoy)").periods[0].excel_value == pytest.approx(3166.2)


def test_a_table_without_a_year_ago_column_does_not_block_one_that_has_it():
    from paired_verifier import _ExcelSource
    from structured_extractor import ExtractedFact, PeriodPoint
    from table_model import TableData

    # Tabel 1 of M2-Juli-2026.pdf carries only Jun and Jul, no growth block and no year-ago
    # column — but its row is spelled exactly as the claim is, so it won the ranking and then
    # answered "no data" while Lampiran 2 printed the figure.
    thin = TableData(title="Tabel 1. Uang Beredar dan Komponennya", unit="triliun Rp",
                     row_labels=["Uang Beredar Luas (M2)"])
    thin._data[("Uang Beredar Luas (M2)", 2026, "Jul")] = 10371.1
    growth = TableData(title="Lampiran 2. Pertumbuhan Uang Beredar", unit="%, yoy",
                       row_labels=["Uang Beredar (M2)"])
    growth._data[("Uang Beredar (M2)", 2026, "Jul")] = 8.3
    fact = ExtractedFact(
        operation="yoy_growth",
        periods=[PeriodPoint(metric_label="Uang Beredar Luas (M2)", year=2026, month="Jul")],
        claimed_value=8.3, unit="persen_yoy", context_quote="x",
    )
    result = _evaluate_fact(fact, [
        _ExcelSource(table=thin, filename="r.pdf", sheet="T1", origin="pdf"),
        _ExcelSource(table=growth, filename="r.pdf", sheet="L2", origin="pdf"),
    ])

    assert result.verdict == "Entailed"
    assert result.computed_value == pytest.approx(8.3)
    # The reader is told the headline number did not come from the closest label match.
    assert "kecocokan label terbaik" in result.reasoning


@pytest.mark.parametrize(
    "label,claim,narrower",
    [
        # A word AFTER the claim's own terms narrows the series.
        ("Kredit Properti", "Kredit", True),
        # A word BEFORE them only names the section the report files it under.
        ("Simpanan Giro Rupiah", "Giro Rupiah", False),
        # Dropping or rewording the claim's terms keeps the same series.
        ("Uang Beredar (M2)", "Uang Beredar Luas (M2)", False),
        # The PDF's mid-word splits must not read as words the row adds.
        ("Kredit Konsum si (KK)", "Kredit Konsumsi (KK)", False),
        ("Giro Sektor Sw asta di BI", "Giro Sektor Swasta", False),
    ],
)
def test_only_a_trailing_extra_word_makes_a_row_too_narrow_to_stand_in(label, claim, narrower):
    from paired_verifier import _is_narrower_than_the_claim
    from structured_extractor import ExtractedFact, PeriodPoint

    fact = ExtractedFact(
        operation="yoy_growth",
        periods=[PeriodPoint(metric_label=claim, year=2026, month="Jul")],
        claimed_value=1.0, unit="persen_yoy", context_quote="x",
    )
    assert _is_narrower_than_the_claim(fact, [(label, 1.0)]) is narrower


# ---------------------------------------------------------------------------
# Threshold claims over several points, and one bad fact never sinks a report
# ---------------------------------------------------------------------------

def _two_point_threshold_fact(operation="above_threshold"):
    return _make_fact(
        operation=operation, claimed_value=100.0, unit=None,
        periods=[
            _make_period(metric_label="Indeks Keyakinan Konsumen (IKK)", year=2026, month="May"),
            _make_period(metric_label="Indeks Keyakinan Konsumen (IKK)", year=2026, month="Jun"),
        ],
    )


def _ikk_table(may, jun):
    return _make_table(title="Tabel 1", unit="Indeks", data={
        ("Indeks Keyakinan Konsumen (IKK)", 2026, "May"): may,
        ("Indeks Keyakinan Konsumen (IKK)", 2026, "Jun"): jun,
    })


def test_threshold_claim_over_two_points_is_judged_on_every_point():
    # The extractor is asked for one point per threshold claim but sometimes gives two
    # ("IKK Mei dan Juni berada di level optimis"). That used to raise IndexError inside
    # _build_periods and take the whole report down with it.
    result = _evaluate_fact(_two_point_threshold_fact(), [_make_source(_ikk_table(120.9, 117.8))])

    assert result.verdict == "Entailed"
    assert [p.excel_value for p in result.periods] == [120.9, 117.8]


def test_threshold_claim_over_two_points_is_refuted_when_any_point_misses():
    result = _evaluate_fact(_two_point_threshold_fact(), [_make_source(_ikk_table(120.9, 98.0))])

    assert result.verdict == "Refuted"
    assert "98.0" in result.reasoning


@patch("paired_verifier._evaluate_fact")
@patch("paired_verifier.extract_structured_facts_async")
@patch("paired_verifier.parse_bi_table")
def test_verify_paired_turns_a_failing_fact_into_inconclusive(mock_parse, mock_extract, mock_eval):
    good = FactVerificationResult(
        operation="value", metric_label="Total", verdict="Entailed",
        reasoning="ok", context_quote="quote",
    )
    mock_parse.return_value = _make_table(data={("Total", 2026, "Apr"): 10355.1})
    mock_extract.return_value = [_make_fact(), _make_fact(periods=[_make_period(month="Mar")])]
    mock_eval.side_effect = [good, IndexError("list index out of range")]

    response = asyncio.run(
        verify_paired(
            narrative_text="[== Halaman 1 ==]\n" + "x" * 250,
            excel_sources=[(b"xls-bytes", "I.1", "TABEL1_1.xls")],
            llm=Mock(),
        )
    )

    assert response.total_facts == 2
    assert [r.verdict for r in response.results] == ["Entailed", "Inconclusive"]
    assert "gagal dinilai" in response.results[1].reasoning


def test_an_aggregate_parent_is_not_a_qualification_when_ranking_sources():
    """SK-Juni-2026 carries two rows named for savings: Tabel 5's 'Total > Tabungan' (17,0% — the
    share of income saved, which is what "saving to income ratio" means) and Tabel 8's
    'Tabungan/deposito' (44,5% — the share of respondents naming it an investment). The claim
    names no section, so _qualification_rank called Tabel 5's row the odd one out and the claim
    was answered with 44,5 and refuted. A 'Total' parent IS the unqualified series."""
    split = _make_table(title="Tabel 5", unit="persen", data={
        ("Total > Konsumsi", 2026, "Jun"): 73.0,
        ("Total > Tabungan", 2026, "Jun"): 17.0,
    })
    investment = _make_table(title="Tabel 8", unit="persen", data={
        ("Tabungan/deposito", 2026, "Jun"): 44.5,
        ("Emas/perhiasan", 2026, "Jun"): 36.5,
    })
    fact = _make_fact(
        operation="value", claimed_value=17.0, unit="persen",
        periods=[_make_period(metric_label="tabungan", year=2026, month="Jun")],
    )

    result = _evaluate_fact(fact, [
        _make_source(split, filename="SK.xlsx", sheet="Tabel 5"),
        _make_source(investment, filename="SK.xlsx", sheet="Tabel 8"),
    ])

    assert result.verdict == "Entailed"
    assert result.computed_value == 17.0


def _deficit_trend_case(quote, operation, older, newer, label="Transaksi Berjalan", claim_label=None):
    table = _make_table(
        title="Neraca Pembayaran Indonesia", unit="juta USD",
        data={(label, 2026, "Q1"): older, (label, 2026, "Q2"): newer},
    )
    fact = _make_fact(
        operation=operation, claimed_value=None, unit=None, context_quote=quote,
        periods=[
            _make_period(metric_label=claim_label or label, month="Q1"),
            _make_period(metric_label=claim_label or label, month="Q2"),
        ],
    )
    return _evaluate_fact(fact, [_make_source(table)])


def test_a_widening_deficit_is_judged_on_its_size_not_on_the_signed_balance():
    # NPI: "defisit transaksi berjalan meningkat" — the balance goes −5,1 → −8,2, which read
    # as signed numbers is a fall, yet the deficit really did grow.
    result = _deficit_trend_case(
        "defisit transaksi berjalan meningkat menjadi 8,2 miliar dolar AS", "is_increasing", -5.1, -8.2,
    )

    assert result.verdict == "Entailed"


def test_a_smaller_deficit_is_judged_on_its_size_not_on_the_signed_balance():
    result = _deficit_trend_case(
        "defisit transaksi berjalan lebih rendah dibandingkan triwulan sebelumnya", "is_decreasing", -8.2, -5.1,
    )

    assert result.verdict == "Entailed"


def test_a_deficit_that_really_shrank_still_refutes_a_claim_that_it_grew():
    result = _deficit_trend_case(
        "defisit transaksi berjalan meningkat menjadi 5,1 miliar dolar AS", "is_increasing", -8.2, -5.1,
    )

    assert result.verdict == "Refuted"


@pytest.mark.parametrize("operation, older, newer, verdict", [
    ("is_increasing", -5086.91, -10180.34, "Entailed"),
    ("is_increasing", -10180.34, -5086.91, "Refuted"),
    ("is_decreasing", -10180.34, -5086.91, "Entailed"),
])
def test_a_trend_whose_own_name_is_the_deficit_is_judged_on_its_size(operation, older, newer, verdict):
    # NPI Tw II-2026: the claim was named "defisit neraca perdagangan migas" by the extractor, but
    # its sentence ("… disertai dengan defisit neraca perdagangan migas.") has no verb after the
    # deficit, so −5.087 → −10.180 was read on the signed numbers and the widening refuted.
    table = _npi_table({("Barang Dagangan Umum > Migas", 2026, "Q1"): older,
                        ("Barang Dagangan Umum > Migas", 2026, "Q2"): newer,
                        ("Barang Dagangan Umum > Nonmigas", 2026, "Q2"): 12136.12,
                        ("Minyak > Impor", 2026, "Q2"): -11607.23})
    table.term_aliases = {"neraca perdagangan migas": "Barang Dagangan Umum > Migas"}  # as npi.py
    fact = _make_fact(
        operation=operation, claimed_value=None, unit=None,
        context_quote=("Neraca perdagangan barang mencatat surplus yang lebih rendah disertai dengan "
                       "defisit neraca perdagangan migas."),
        periods=[_make_period(metric_label="defisit neraca perdagangan migas", month="Q1"),
                 _make_period(metric_label="defisit neraca perdagangan migas", month="Q2")],
    )

    assert _evaluate_fact(fact, [_make_source(table)]).verdict == verdict


def test_a_trend_on_the_balance_itself_keeps_the_signed_reading():
    # "transaksi berjalan membaik" with no deficit word before the verb: −8,2 → −5,1 is an
    # improvement of the signed balance, exactly as written.
    result = _deficit_trend_case(
        "transaksi berjalan membaik, dari defisit 8,2 miliar dolar AS", "is_increasing", -8.2, -5.1,
    )

    assert result.verdict == "Entailed"


def _gdp_share_case(claimed, quote, ratio_row=-1.0):
    table = _make_table(
        title="Neraca Pembayaran Indonesia", unit="juta USD",
        data={("Transaksi Berjalan", 2026, "Q2"): -3270.0, ("Transaksi Berjalan (% PDB)", 2026, "Q2"): ratio_row},
    )
    fact = _make_fact(
        operation="ratio", claimed_value=claimed, unit="persen", context_quote=quote,
        periods=[
            _make_period(metric_label="Transaksi Berjalan", month="Q2"),
            _make_period(metric_label="Transaksi Berjalan (% PDB)", month="Q2"),
        ],
    )
    return _evaluate_fact(fact, [_make_source(table)])


def test_a_share_of_gdp_is_read_from_the_tables_own_percent_of_gdp_row():
    # NPI: "defisit transaksi berjalan ... (1,0% dari PDB)". SEKI V.1 already prints the share;
    # dividing the balance by that row (−3270 / −1,0) gave 327000%.
    result = _gdp_share_case(1.0, "defisit transaksi berjalan sebesar 3,3 miliar dolar AS (1,0% dari PDB)")

    assert result.verdict == "Entailed"
    assert result.computed_value == pytest.approx(-1.0)


def test_a_wrong_share_of_gdp_is_still_refuted():
    result = _gdp_share_case(1.4, "defisit transaksi berjalan sebesar 3,3 miliar dolar AS (1,4% dari PDB)")

    assert result.verdict == "Refuted"


def _accounts_table():
    return _make_table(
        title="Neraca Pembayaran Indonesia", unit="Juta USD",
        data={("Transaksi Modal", 2025, "Q4"): 172.52, ("Transaksi Finansial", 2025, "Q4"): 8794.82,
              ("Transaksi Finansial > Investasi Lainnya > Kewajiban", 2025, "Q4"): 1896.46,
              ("Transaksi Finansial > Investasi Lainnya > Kewajiban", 2026, "Q1"): -1054.07},
    )


@pytest.mark.parametrize("quote, verdict", [
    # NPI Tw I-2026: the extractor kept only 'Transaksi Finansial' (8,79); the sentence names both.
    ("transaksi modal dan finansial pada triwulan I 2026 mencatat defisit 4,9 miliar dolar AS, "
     "setelah pada triwulan sebelumnya mencatat surplus 9,0 miliar dolar AS.", "Entailed"),
    # Each its own figure: not a total.
    ("transaksi modal dan finansial masing-masing sebesar 0,2 dan 9,0 miliar dolar AS", "Refuted"),
])
def test_a_label_the_sentence_joins_to_another_row_is_their_sum(quote, verdict):
    fact = _make_fact(claimed_value=9.0, unit="miliar USD", context_quote=quote,
                      periods=[_make_period(metric_label="Transaksi Finansial", year=2025, month="Q4")])

    assert _evaluate_fact(fact, [_make_source(_accounts_table())]).verdict == verdict


def test_a_net_outflow_after_a_net_inflow_has_grown():
    # NPI Tw I-2026: "net outflow pada sisi kewajiban setelah pada triwulan sebelumnya mencatatkan
    # net inflow" — +1.896 -> -1.054, refuted as a fall.
    fact = _make_fact(
        operation="is_increasing", claimed_value=None, unit=None,
        context_quote=("disertai dengan net outflow pada sisi kewajiban setelah pada triwulan "
                       "sebelumnya mencatatkan net inflow"),
        periods=[_make_period(metric_label="net outflow kewajiban investasi lainnya", year=2025, month="Q4"),
                 _make_period(metric_label="net outflow kewajiban investasi lainnya", year=2026, month="Q1")],
    )
    table = _accounts_table()
    table.term_aliases = {"net outflow kewajiban investasi lainnya":
                          "Transaksi Finansial > Investasi Lainnya > Kewajiban"}

    assert _evaluate_fact(fact, [_make_source(table)]).verdict == "Entailed"


def test_a_flow_is_not_answered_by_the_income_on_it():
    # NPI Tw I-2026: "investasi langsung … surplus 2,0" reached 'Pendapatan investasi langsung'.
    table = _make_table(title="Neraca Pembayaran Indonesia", unit="Juta USD", data={
        ("Pendapatan investasi langsung", 2026, "Q1"): -5681.84,
        ("INVESTASI LANGSUNG > Investasi Langsung", 2026, "Q1"): 2019.62,
    })

    assert table.lookup_fuzzy("Investasi Langsung", 2026, "Q1") == (
        "INVESTASI LANGSUNG > Investasi Langsung", 2019.62)


def test_most_groups_rising_is_a_majority_claim_even_unmarked():
    # SK Agustus 2026: "meningkat pada sebagian besar kelompok pengeluaran" with all five groups
    # and no group_scope was refuted by the one group that fell.
    rows = {"IKK > Pengeluaran Rp1 - 2 juta": (106.8, 108.2), "IKK > Pengeluaran Rp2,1 - 3 juta": (110.4, 111.3),
            "IKK > Pengeluaran Rp3,1 - 4 juta": (112.2, 117.5), "IKK > Pengeluaran Rp4,1 - 5 juta": (120.4, 118.9),
            "IKK > Pengeluaran >Rp5 juta": (121.2, 123.9)}
    data = {}
    for row, (jul, aug) in rows.items():
        data[(row, 2026, "Jul")], data[(row, 2026, "Aug")] = jul, aug
    table = _make_table(title="Tabel 2. Indeks Keyakinan Konsumen", unit="Indeks", data=data)
    fact = _make_fact(
        operation="is_increasing", claimed_value=None, unit=None,
        context_quote=("keyakinan konsumen pada Agustus 2026 meningkat pada sebagian besar kelompok "
                       "pengeluaran, dengan IKK tertinggi pada responden kelompok >Rp5 juta sebesar 123,9"),
        periods=[_make_period(metric_label=row, month="Aug") for row in rows],
    )

    result = _evaluate_fact_safely(fact, [_make_source(table)])

    assert result.verdict == "Entailed"
    assert "4 dari 5" in result.reasoning


def test_a_negative_share_needs_a_deficit_word_to_match_a_positive_claim():
    result = _gdp_share_case(1.0, "transaksi berjalan sebesar 3,3 miliar dolar AS (1,0% dari PDB)")

    assert result.verdict == "Refuted"


def test_a_sum_adds_two_different_rows_of_the_same_period():
    # NPI: "transaksi modal dan finansial ... surplus 12,0 miliar dolar AS" — SEKI V.1 holds
    # the two accounts as separate rows; the extractor emits a sum over both (rule 2g).
    table = _make_table(
        title="Neraca Pembayaran Indonesia", unit="miliar USD",
        data={("Transaksi Modal", 2026, "Q2"): 0.04, ("Transaksi Finansial", 2026, "Q2"): 11.98},
    )
    fact = _make_fact(
        operation="sum", claimed_value=12.0, unit="miliar USD",
        context_quote="transaksi modal dan finansial mencatat surplus 12,0 miliar dolar AS",
        periods=[
            _make_period(metric_label="Transaksi Modal", month="Q2"),
            _make_period(metric_label="Transaksi Finansial", month="Q2"),
        ],
    )

    result = _evaluate_fact(fact, [_make_source(table)])

    assert result.verdict == "Entailed"
    assert result.computed_value == pytest.approx(12.02)



def _net_claim_case(metric_label, quote, claimed):
    table = _make_table(
        title="Posisi Investasi Internasional Indonesia", unit="miliar USD",
        data={
            ("Aset > Investasi Portofolio", 2026, "Q2"): 160.1,
            ("Kewajiban > Investasi Portofolio", 2026, "Q2"): 389.7,
        },
    )
    fact = _make_fact(
        claimed_value=claimed, unit="miliar USD", context_quote=quote,
        periods=[_make_period(metric_label=metric_label, month="Q2")],
    )
    return _evaluate_fact(fact, [_make_source(table)])


def test_a_net_position_is_not_answered_by_the_gross_liability_row():
    # PII: "investasi portofolio mencatat kewajiban neto sebesar 229,6" was matched to the gross
    # 'Kewajiban > Investasi Portofolio' (389,7) and reported Tidak Sesuai. SEKI V.39 has no net
    # row per component, so the claim cannot be checked against this table.
    result = _net_claim_case(
        "Kewajiban Investasi Portofolio",
        "posisi investasi portofolio mencatat kewajiban neto sebesar 229,6 miliar dolar AS", 229.6,
    )

    assert result.verdict == "Inconclusive"
    assert result.matched_excel_source is None


def test_a_gross_liability_claim_still_matches_the_liability_row():
    result = _net_claim_case(
        "Kewajiban Investasi Portofolio",
        "posisi KFLN investasi portofolio tercatat sebesar 389,7 miliar dolar AS", 389.7,
    )

    assert result.verdict == "Entailed"


_TRADE = "PERDAGANGAN BESAR DAN ECERAN, DAN REPARASI MOBIL DAN MOTOR"


def _skdu_t1_case(metric_label, claimed):
    table = _make_table(
        title="Tabel 1. Kegiatan Usaha  / Table 1. Business Activity",
        unit="% Saldo Bersih Tertimbang - SBT",
        data={(_TRADE, 2026, "Q2"): 1.2, ("INDUSTRI PENGOLAHAN", 2026, "Q2"): 0.77},
    )
    fact = _make_fact(
        claimed_value=claimed, unit="persen", context_quote=f"{metric_label} sebesar {claimed}%",
        periods=[_make_period(metric_label=metric_label, month="Q2")],
    )
    return _evaluate_fact(fact, [_make_source(table, filename="SKDU.xlsx", sheet="T1 Kegiatan Usaha")])


@pytest.mark.parametrize("metric_label", [
    "SBT harga jual Perdagangan Besar dan Eceran, dan Reparasi Mobil dan Motor",
    "SBT Investasi Industri Pengolahan",
    "prakiraan tingkat inflasi Perdagangan Besar dan Eceran, dan Reparasi Mobil dan Motor",
])
def test_a_topic_word_before_the_sector_that_the_table_lacks_keeps_the_claim_off_it(metric_label):
    # SKDU: claims about Tabel 5-7 (harga jual, inflasi, investasi), not uploaded in the run,
    # fell to the same sector's row in Tabel 1 Kegiatan Usaha and were reported Tidak Sesuai.
    result = _skdu_t1_case(metric_label, 5.8)

    assert result.verdict == "Inconclusive"
    assert result.matched_excel_source is None


@pytest.mark.parametrize("metric_label, claimed", [
    ("SBT kegiatan usaha Perdagangan Besar dan Eceran, dan Reparasi Mobil dan Motor", 1.2),
    ("LU Industri Pengolahan", 0.77),
    ("prakiraan SBT kegiatan usaha sub-LU Industri Pengolahan", 0.77),
    ("kinerja LU Industri Pengolahan", 0.77),
])
def test_generic_words_before_the_sector_still_reach_the_row(metric_label, claimed):
    result = _skdu_t1_case(metric_label, claimed)

    assert result.verdict == "Entailed"


def test_a_widening_deficit_holds_whichever_way_the_extractor_read_the_verb():
    # NPI e2e: "defisit neraca transaksi berjalan (TB) melebar" came back as is_decreasing (the
    # balance falls) in one run and is_increasing (the deficit grows) in another. The verb
    # decides: -3576 -> -12487 is a wider deficit either way.
    for operation in ("is_decreasing", "is_increasing"):
        result = _deficit_trend_case(
            "defisit neraca transaksi berjalan (TB) melebar terutama dipengaruhi perekonomian global",
            operation, -3576.01, -12487.42,
        )

        assert result.verdict == "Entailed", operation


def test_a_deficit_word_before_the_account_does_not_keep_the_claim_off_its_row():
    # "Defisit neraca perdagangan migas meningkat": 'defisit' and 'neraca' name the sign and the
    # kind of account, not a different series.
    result = _deficit_trend_case(
        "Defisit neraca perdagangan migas meningkat akibat kenaikan impor minyak",
        "is_increasing", -1.2, -5.1, label="Migas", claim_label="Defisit neraca migas",
    )

    assert result.verdict == "Entailed"


def test_a_two_account_sum_printed_as_a_deficit_matches_its_negative_total():
    # "transaksi modal dan finansial ... setelah pada triwulan I 2026 mengalami defisit sebesar
    # 4,8 miliar dolar AS": the two rows add up to -4,76.
    table = _make_table(
        title="Neraca Pembayaran Indonesia", unit="miliar USD",
        data={("Transaksi Modal", 2026, "Q1"): 0.0043, ("Transaksi Finansial", 2026, "Q1"): -4.7654},
    )
    fact = _make_fact(
        operation="sum", claimed_value=4.8, unit="miliar USD",
        context_quote="transaksi modal dan finansial ... setelah pada triwulan I 2026 mengalami defisit sebesar 4,8 miliar dolar AS",
        periods=[
            _make_period(metric_label="Transaksi Modal", month="Q1"),
            _make_period(metric_label="Transaksi Finansial", month="Q1"),
        ],
    )

    result = _evaluate_fact(fact, [_make_source(table)])

    assert result.verdict == "Entailed"


def test_another_accounts_share_of_gdp_is_not_a_denominator():
    # "TMF ... 12,0 miliar dolar AS (3,2% dari PDB)": SEKI V.1 has a (% PDB) row only for the
    # current account. Dividing the capital account by it gave -881%.
    table = _make_table(
        title="Neraca Pembayaran Indonesia", unit="miliar USD",
        data={("Transaksi Modal", 2026, "Q2"): 29.44, ("Transaksi Berjalan (% PDB)", 2026, "Q2"): -3.34},
    )
    fact = _make_fact(
        operation="ratio", claimed_value=3.2, unit="persen",
        context_quote="neraca transaksi modal dan finansial (TMF) mencatat surplus 12,0 miliar dolar AS (3,2% dari PDB)",
        periods=[
            _make_period(metric_label="Transaksi Modal", month="Q2"),
            _make_period(metric_label="Transaksi Berjalan (% PDB)", month="Q2"),
        ],
    )

    result = _evaluate_fact(fact, [_make_source(table)])

    assert result.verdict == "Inconclusive"


@pytest.mark.parametrize("quote, older, newer", [
    ("didorong oleh peningkatan defisit neraca jasa", -4391.13, -5893.35),
    ("pelebaran defisit neraca jasa pada triwulan II 2026", -4391.13, -5893.35),
    ("penyempitan defisit neraca jasa pada triwulan II 2026", -5893.35, -4391.13),
])
def test_a_noun_before_the_deficit_also_says_which_way_its_size_moved(quote, older, newer):
    for operation in ("is_increasing", "is_decreasing"):
        result = _deficit_trend_case(quote, operation, older, newer, label="Jasa")

        assert result.verdict == "Entailed", (quote, operation)


@pytest.mark.parametrize("quote, claimed", [
    # the earlier quarter's figure, a few words away from 'kewajiban neto'
    ("posisi investasi portofolio mencatat kewajiban neto sebesar 229,6 miliar dolar AS, lebih "
     "rendah dibandingkan dengan 237,7 miliar dolar AS pada akhir triwulan I 2026", 237.7),
    ("derivatif finansial mencatat kewajiban neto 1,4 miliar dolar AS, lebih tinggi dari "
     "kewajiban neto pada triwulan I 2026 sebesar 0,6 miliar dolar AS", 0.6),
])
def test_a_net_figure_further_along_the_sentence_is_still_a_net_figure(quote, claimed):
    result = _net_claim_case("Kewajiban Investasi Portofolio", quote, claimed)

    assert result.verdict == "Inconclusive"


def test_a_gross_figure_after_a_net_one_is_judged_as_gross():
    # The KFLN figure names its own (gross) side; the net position earlier in the sentence does
    # not carry over to it.
    result = _net_claim_case(
        "Kewajiban Investasi Portofolio",
        "kewajiban neto menurun, bersumber dari penurunan posisi KFLN investasi portofolio menjadi 389,7 miliar dolar AS",
        389.7,
    )

    assert result.verdict == "Entailed"


def _with_net_row(operation, metric_label, quote, claimed):
    # The PII parser derives 'Investasi Portofolio (neto)' = Aset − Kewajiban, outside row_labels.
    table = _make_table(
        title="Posisi Investasi Internasional Indonesia", unit="miliar USD",
        data={
            ("Aset > Investasi Portofolio", 2026, "Q2"): 43.49,
            ("Kewajiban > Investasi Portofolio", 2026, "Q2"): 273.1,
        },
    )
    table._data[("Investasi Portofolio (neto)", 2026, "Q2")] = 43.49 - 273.1
    table.net_rows = {"investasi portofolio": "Investasi Portofolio (neto)"}
    fact = _make_fact(
        operation=operation, claimed_value=claimed, unit="miliar USD", context_quote=quote,
        periods=[_make_period(metric_label=metric_label, month="Q2")],
    )
    return _evaluate_fact(fact, [_make_source(table)])


@pytest.mark.parametrize("metric_label", [
    "Investasi Portofolio", "Investasi Portofolio, Neto", "Kewajiban > Investasi Portofolio",
])
def test_a_net_figure_is_read_from_the_components_net_row(metric_label):
    # PII Tw II-2026: "investasi portofolio mencatat kewajiban neto sebesar 229,6" stayed Tidak
    # Cukup Data; the table's net row (−229,61) answers it.
    result = _with_net_row(
        "value", metric_label,
        "posisi investasi portofolio mencatat kewajiban neto sebesar 229,6 miliar dolar AS", 229.6,
    )

    assert result.verdict == "Entailed"
    assert result.periods[0].metric_label == "Investasi Portofolio (neto)"


def test_a_gross_figure_never_reaches_the_net_row():
    result = _with_net_row(
        "value", "Aset > Investasi Portofolio",
        "posisi AFLN investasi portofolio tercatat sebesar 43,5 miliar dolar AS", 43.5,
    )

    assert result.verdict == "Entailed"
    assert result.periods[0].metric_label == "Aset > Investasi Portofolio"


def _pii_components():
    table = _make_table(title="Posisi Investasi Internasional Indonesia", unit="miliar USD", data={
        ("Aset > Investasi Lainnya", 2026, "Q2"): 233.63, ("Kewajiban > Investasi Lainnya", 2026, "Q2"): 196.60,
        ("Cadangan Devisa", 2026, "Q2"): 145.59,
        ("Investasi langsung berdasarkan arah investasi", 2026, "Q1"): -168.84,
        ("Investasi langsung berdasarkan arah investasi", 2026, "Q2"): -149.08,
        ("Di Indonesia (PMA)", 2026, "Q1"): 310.78, ("Di Indonesia (PMA)", 2026, "Q2"): 294.11,
    })
    table._data[("Investasi Lainnya (neto)", 2026, "Q2")] = 233.63 - 196.60
    table.net_rows = {"investasi lainnya": "Investasi Lainnya (neto)", "cadangan devisa": "Cadangan Devisa",
                      "di indonesia (pma)": "Investasi langsung berdasarkan arah investasi"}
    return [_make_source(table)]


def test_the_named_exceptions_are_judged_on_their_own_net_side():
    # PII Tw II-2026: "Sebagian besar komponen … mencatat kewajiban neto, kecuali investasi lainnya
    # dan cadangan devisa mencatat aset neto" came back as those two below 0, a majority claim,
    # and was refuted on the gross asset rows.
    fact = _make_fact(
        operation="below_threshold", claimed_value=0.0, unit=None, group_scope="majority",
        context_quote=("Sebagian besar komponen PII pada akhir triwulan II 2026 mencatat kewajiban neto, "
                       "kecuali investasi lainnya dan cadangan devisa mencatat aset neto."),
        periods=[_make_period(metric_label="Investasi Lainnya", month="Q2"),
                 _make_period(metric_label="Cadangan Devisa", month="Q2")],
    )

    assert _evaluate_fact_safely(fact, _pii_components()).verdict == "Entailed"


@pytest.mark.parametrize("operation, claimed, year_month", [
    ("value", 149.1, [(2026, "Q2")]),
    ("diff", -19.8, [(2026, "Q1"), (2026, "Q2")]),
])
def test_a_net_figure_split_by_the_pdf_reaches_the_directional_net_row(operation, claimed, year_month):
    # The same report printed "kewajiban neto sebesar 1 49,1 … turun 19, 8" and the extractor
    # named the memorandum's 'Di Indonesia (PMA)' (294,1).
    fact = _make_fact(
        operation=operation, claimed_value=claimed, unit="miliar USD",
        context_quote=("Posisi investasi langsung pada akhir triwulan II 202 6 mencatat kewajiban neto "
                       "sebesar 1 49,1 miliar dolar AS, turun 19, 8 miliar dolar AS dari 168,8 miliar dolar AS"),
        periods=[_make_period(metric_label="Di Indonesia (PMA)", year=y, month=m) for y, m in year_month],
    )

    assert _evaluate_fact_safely(fact, _pii_components()).verdict == "Entailed"


def test_net_liabilities_as_a_sign_claim_are_checked_on_the_net_row():
    # The same sentence also came back as 'Investasi Portofolio' below 0, which was judged on the
    # sentence's 'kewajiban' — the gross 273,1 — and reported Tidak Sesuai.
    result = _with_net_row(
        "below_threshold", "Investasi Portofolio",
        "posisi investasi portofolio mencatat kewajiban neto sebesar 229,6 miliar dolar AS", 0.0,
    )

    assert result.verdict == "Entailed"


def _reserves_threshold(row, quote, claimed=3.0):
    table = _make_table(title="Posisi Cadangan Devisa", unit="Juta USD",
                        data={(row, 2026, "Aug"): 146507.61})
    fact = _make_fact(
        operation="above_threshold", claimed_value=claimed, unit=None, context_quote=quote,
        periods=[_make_period(metric_label=row, month="Aug")],
    )
    return _evaluate_fact_safely(fact, [_make_source(table, filename="TABEL5_9.xls", sheet="5.9")])


def test_a_bound_in_months_of_imports_is_not_answered_by_reserves_in_dollars():
    # Cadangan Devisa Agustus 2026: "di atas standar kecukupan internasional sekitar 3 bulan
    # impor" was Sesuai on 146.507 juta USD > 3.
    result = _reserves_threshold(
        "Cadangan Devisa",
        "setara dengan pembiayaan 5,4 bulan impor, serta berada di atas standar kecukupan "
        "internasional sekitar 3 bulan impor")

    assert result.verdict == "Inconclusive"
    assert result.resolved_via == "months_of_imports_guard"


def test_a_row_in_months_of_imports_still_answers_such_a_bound():
    result = _reserves_threshold(
        "Dalam Bulan Impor", "berada di atas standar kecukupan internasional sekitar 3 bulan impor")

    assert result.verdict == "Entailed"


def test_a_net_figure_is_not_answered_by_a_top_level_side_row():
    table = _make_table(
        title="Posisi Investasi Internasional Indonesia", unit="miliar USD",
        data={("Aset Lainnya", 2026, "Q2"): 28.42},
    )
    fact = _make_fact(
        claimed_value=9.9, unit="miliar USD",
        context_quote="instrumen lainnya mencatatkan aset neto sebesar 9,9 miliar dolar AS",
        periods=[_make_period(metric_label="aset neto instrumen lainnya", month="Q2")],
    )

    result = _evaluate_fact(fact, [_make_source(table)])

    assert result.verdict == "Inconclusive"


def test_a_change_in_net_liabilities_is_measured_on_their_size():
    # "Kewajiban neto PII ... menurun sebesar 25,6": the net position goes -223,0 -> -197,4.
    table = _make_table(
        title="Posisi Investasi Internasional Indonesia", unit="miliar USD",
        data={("Posisi Investasi Internasional, bersih", 2026, "Q1"): -223.0489,
              ("Posisi Investasi Internasional, bersih", 2026, "Q2"): -197.431},
    )
    label = "Posisi Investasi Internasional, bersih"
    fact = _make_fact(
        operation="diff", claimed_value=-25.6, unit="miliar USD",
        context_quote="Kewajiban neto PII pada akhir triwulan II 2026 tersebut menurun sebesar 25,6 miliar dolar AS",
        periods=[_make_period(metric_label=label, month="Q1"), _make_period(metric_label=label, month="Q2")],
    )

    result = _evaluate_fact(fact, [_make_source(table)])

    assert result.verdict == "Entailed"


def test_a_change_stated_for_one_period_runs_from_the_period_before():
    # PII Tw IV-2025 s063: "Pada akhir triwulan IV 2025, kewajiban neto PII naik sebesar 10,8" came
    # with Q4 alone; Q4 − Q4 = 0 refuted it. Q3 −261,80 -> Q4 −272,63: net liabilities +10,8.
    label = "Posisi Investasi Internasional, bersih"
    table = _make_table(title="Posisi Investasi Internasional Indonesia", unit="miliar USD",
                        data={(label, 2025, "Q3"): -261.8006, (label, 2025, "Q4"): -272.6251})
    fact = _make_fact(
        operation="diff", claimed_value=10.8, unit="miliar USD",
        context_quote="Pada akhir triwulan IV 2025, kewajiban neto PII naik sebesar 10,8 miliar dolar AS.",
        periods=[_make_period(metric_label=label, year=2025, month="Q4")],
    )

    assert _evaluate_fact(fact, [_make_source(table)]).verdict == "Entailed"


def test_a_rise_in_net_liabilities_said_with_nya_is_measured_on_their_size():
    # PII Tw IV-2025 s031: "meningkatnya kewajiban neto investasi langsung sebesar 21,5" on
    # −182,36 → −203,90 was refuted against the signed change (−21,54).
    label = "Investasi Langsung (neto)"
    table = _make_table(title="Posisi Investasi Internasional Indonesia", unit="miliar USD",
                        data={(label, 2024, "Q4"): -182.3646, (label, 2025, "Q4"): -203.901})
    fact = _make_fact(
        operation="diff", claimed_value=21.5, unit="miliar USD",
        context_quote=("Perkembangan ini terutama dikontribusikan oleh meningkatnya kewajiban neto investasi "
                       "langsung sebesar 21,5 miliar dolar AS."),
        periods=[_make_period(metric_label=label, year=2024, month="Q4"),
                 _make_period(metric_label=label, year=2025, month="Q4")],
    )

    assert _evaluate_fact(fact, [_make_source(table)]).verdict == "Entailed"


def test_pointer_pass_does_not_point_a_net_figure_at_a_gross_cell():
    # PII e2e: "investasi portofolio mengalami kewajiban neto tertinggi sebesar 229,6" fell to the
    # pointer, which read the gross liability cell (273,1). The sheet has no net cell per
    # component, so there is nothing a pointer could correctly point at.
    grid = [["Komponen", "Q2 2026"], ["Investasi Portofolio", 273.1], ["Lain", 1.0]]
    src = _pointer_only_source(grid)
    fact = _make_fact(
        claimed_value=229.6, unit="miliar USD",
        context_quote="Komponen investasi portofolio mengalami kewajiban neto tertinggi sebesar 229,6 miliar dolar AS",
        periods=[_make_period(metric_label="Investasi Portofolio", month="Q2")],
    )
    results = [_evaluate_fact(fact, [src])]
    batch = _MultiSourcePointers(pointers=[
        _MultiCellPointer(source_index=0, query_index=0, found=True, row=1, col=1)
    ])

    new, n = asyncio.run(_pointer_pass([fact], results, [src], _pointer_llm(batch)))

    assert n == 0
    assert new[0].verdict == "Inconclusive"


@pytest.mark.parametrize("metric_label, row, title", [
    # an abbreviation spelled out in front of the row it names (SBank e2e)
    ("Kredit Pemilikan Rumah KPR/KPA", "KPR/KPA",
     "Tabel 1. Penyaluran Kredit Baru Berdasarkan Jenis Kredit (SBT, %)"),
    # descriptive words in front of the row (SHPR e2e)
    ("pertumbuhan indeks harga rumah tipe menengah", "MENENGAH",
     "TABEL 3. PERTUMBUHAN INDEKS HARGA PROPERTI RESIDENSIAL"),
])
def test_descriptive_words_before_the_row_do_not_reject_the_table(metric_label, row, title):
    table = _make_table(title=title, unit="%", data={(row, 2026, "Q2"): 40.63, ("Lain", 2026, "Q2"): 1.0})
    fact = _make_fact(
        claimed_value=40.63, unit="persen", context_quote=f"{metric_label} sebesar 40,63%",
        periods=[_make_period(metric_label=metric_label, month="Q2")],
    )

    result = _evaluate_fact(fact, [_make_source(table)])

    assert result.verdict == "Entailed"


def test_a_value_claim_without_a_unit_is_checked_against_a_growth_table_without_crashing():
    # SPE e2e: 14 claims such as "Indeks Penjualan Riil (IPR) ... 210,6" carry no unit; meeting
    # a '%, yoy' sheet they raised TypeError in _parse_scale_unit(None) and were never checked.
    table = _make_table(
        title="Tabel 2. Pertumbuhan Indeks Penjualan Riil", unit="%, yoy",
        data={("Indeks Penjualan Riil (IPR)", 2026, "Jul"): 1.1},
    )
    fact = _make_fact(
        claimed_value=1.1, unit=None, context_quote="IPR tumbuh 1,1%",
        periods=[_make_period(metric_label="Indeks Penjualan Riil (IPR)", month="Jul")],
    )

    result = _evaluate_fact(fact, [_make_source(table)])

    assert result.verdict == "Entailed"


def _debit_yoy_case(claimed, prior=-48487.59, current=-58322.94):
    # SEKI V.1 records imports (a debit) as negative numbers.
    table = _make_table(
        title="Neraca Pembayaran Indonesia", unit="Juta USD",
        data={("Nonmigas > Impor", 2025, "Q2"): prior, ("Nonmigas > Impor", 2026, "Q2"): current},
    )
    fact = _make_fact(
        operation="yoy_growth", claimed_value=claimed, unit="persen",
        context_quote=f"impor nonmigas tumbuh {claimed}% (yoy)",
        periods=[_make_period(metric_label="Nonmigas > Impor", month="Q2")],
    )
    return _evaluate_fact(fact, [_make_source(table)])


def test_growth_of_a_row_stored_negative_is_the_growth_of_its_size():
    # NPI e2e: imports -48.487 -> -58.323 is a 20,3% rise; signed it read as -20,3%.
    result = _debit_yoy_case(20.3)

    assert result.verdict == "Entailed"
    assert result.computed_value == pytest.approx(20.2843, abs=1e-3)


def test_the_opposite_sign_on_a_negative_row_is_still_wrong():
    result = _debit_yoy_case(-20.3)

    assert result.verdict == "Refuted"


def test_a_shrinking_debit_row_reads_as_a_fall():
    result = _debit_yoy_case(-16.9, prior=-58322.94, current=-48487.59)

    assert result.verdict == "Entailed"


# ---------------------------------------------------------------------------
# Tinjauan AI stage inside verify_paired
# ---------------------------------------------------------------------------

from ai_review import ClaimReview, ReviewBatch  # noqa: E402


def _review_llm(batch):
    llm = Mock()
    llm.with_structured_output = Mock(return_value=RunnableLambda(lambda _p: batch))
    return llm


@patch("paired_verifier.extract_structured_facts_async")
@patch("paired_verifier.parse_bi_table")
def test_verify_paired_runs_the_review_stage_on_unconfirmed_claims(mock_parse, mock_extract_facts):
    # The code path refutes the claim (10.355,1 against 1,0); the AI says the table does not
    # hold what the sentence means, which turns it Tidak Cukup Data — and the counts follow.
    mock_parse.return_value = _make_table(data={("Total", 2026, "Apr"): 1.0})
    mock_extract_facts.return_value = [_make_fact()]
    review = ClaimReview(
        claim_no=1, first_words="quote", subject="quote", word_check="-", match="tidak_ada",
        verdict="tidak_ada_di_tabel", explanation="Tabel tidak memuat metrik ini.",
    )
    events = []
    response = asyncio.run(verify_paired(
        narrative_text="[== Halaman 1 ==]\n" + "x" * 250,
        excel_sources=[(b"xls-bytes", "I.1", "TABEL1_1.xls")],
        llm=Mock(), progress_cb=events.append,
        review_llm=_review_llm(ReviewBatch(reviews=[review])),
    ))
    r = response.results[0]
    assert (r.verdict, r.resolved_via) == ("Inconclusive", "ai_review")
    assert (response.refuted_count, response.inconclusive_count) == (0, 1)
    details = [e.get("detail") or "" for e in events]
    assert "tinjauan AI: 1 klaim" in details
    assert any("1 diubah tinjauan AI" in d for d in details)


@patch("paired_verifier.extract_structured_facts_async")
@patch("paired_verifier.parse_bi_table")
def test_verify_paired_without_review_llm_makes_no_review_call(mock_parse, mock_extract_facts):
    mock_parse.return_value = _make_table(data={("Total", 2026, "Apr"): 1.0})
    mock_extract_facts.return_value = [_make_fact()]
    with patch("ai_review.review_claims") as mock_review:
        asyncio.run(verify_paired(
            narrative_text="[== Halaman 1 ==]\n" + "x" * 250,
            excel_sources=[(b"xls-bytes", "I.1", "TABEL1_1.xls")], llm=Mock(),
        ))
    mock_review.assert_not_called()


@patch("paired_verifier.extract_structured_facts_async")
@patch("paired_verifier.parse_bi_table")
def test_a_failing_review_stage_keeps_the_code_verdicts(mock_parse, mock_extract_facts):
    mock_parse.return_value = _make_table(data={("Total", 2026, "Apr"): 1.0})
    mock_extract_facts.return_value = [_make_fact()]   # claims 10355,1 against 1,0 → Refuted
    with patch("ai_review.review_claims", side_effect=RuntimeError("boom")):
        response = asyncio.run(verify_paired(
            narrative_text="[== Halaman 1 ==]\n" + "x" * 250,
            excel_sources=[(b"xls-bytes", "I.1", "TABEL1_1.xls")], llm=Mock(),
            review_llm=Mock(),
        ))
    assert response.results[0].verdict == "Refuted"
    assert response.results[0].resolved_via is None


# --- SKDU Tw II-2026: SBT wording is about the level (2026-10-15) ---------------------------------

_SBT = "% Saldo Bersih Tertimbang - SBT"


def _sbt_trend(operation, row, q1, q2, quote, unit=_SBT):
    table = _make_table(title="Tabel 1. Kegiatan Usaha", unit=unit,
                        data={(row, 2026, "Q1"): q1, (row, 2026, "Q2"): q2})
    fact = _make_fact(operation=operation, claimed_value=None, unit=None, context_quote=quote,
                      periods=[_make_period(metric_label=row, month="Q1"),
                               _make_period(metric_label=row, month="Q2")])
    return _evaluate_fact(fact, [_make_source(table, filename="SKDU.xlsx", sheet="T1")])


def test_sbt_tetap_tumbuh_means_positive_not_rising():
    r = _sbt_trend("is_increasing", "PERDAGANGAN", 1.24, 1.04,
                   "Kegiatan usaha LU Perdagangan pada triwulan II 2026 terindikasi tetap tumbuh.")
    assert r.verdict == "Entailed"
    assert "SBT positif" in r.reasoning


@pytest.mark.parametrize("q2,verdict", [(0.14, "Entailed"), (-0.2, "Refuted")])
def test_sbt_tetap_tumbuh_filed_as_stable_is_still_about_the_level(q2, verdict):
    # SKDU 2026-10-16: the extractor filed "tetap tumbuh" as is_stable, and 0,28 -> 0,14 was
    # refuted as not flat although the balance stayed positive.
    r = _sbt_trend("is_stable", "TOTAL", 0.28, q2,
                   "Penggunaan tenaga kerja pada triwulan II 2026 tetap tumbuh dan diprakirakan "
                   "meningkat pada triwulan III 2026.")
    assert r.verdict == verdict


def test_sbt_terjaga_means_positive_not_flat():
    r = _sbt_trend("is_stable", "INDUSTRI PENGOLAHAN", 1.46, 1.34,
                   "Kinerja usaha LU Industri Pengolahan pada triwulan II 2026 diindikasikan terjaga.")
    assert r.verdict == "Entailed"


def test_sbt_contributor_with_cited_balance_is_judged_on_its_level():
    r = _sbt_trend("is_increasing", "PERTANIAN, KEHUTANAN DAN PERIKANAN", 4.07, 3.50,
                   "Peningkatan harga jual antara lain bersumber dari LU Industri Pengolahan "
                   "(SBT 5,16%), serta LU Pertanian, Kehutanan, dan Perikanan (SBT 3,50%).")
    assert r.verdict == "Entailed"


def test_sbt_negative_level_still_refutes_tetap_tumbuh():
    r = _sbt_trend("is_increasing", "PERDAGANGAN", 0.4, -0.2,
                   "Kegiatan usaha LU Perdagangan pada triwulan II 2026 terindikasi tetap tumbuh.")
    assert r.verdict == "Refuted"


def test_a_real_rise_claim_on_sbt_is_still_checked_as_a_rise():
    r = _sbt_trend("is_increasing", "KONSTRUKSI", 0.81, 0.12,
                   "Kegiatan usaha LU Konstruksi meningkat pada triwulan II 2026.")
    assert r.verdict == "Refuted"


def test_tetap_tumbuh_on_a_level_table_is_still_a_rise():
    r = _sbt_trend("is_increasing", "Kredit", 100.0, 90.0,
                   "Kredit pada triwulan II 2026 tetap tumbuh.", unit="triliun Rp")
    assert r.verdict == "Refuted"


def test_named_sectors_of_one_total_are_checked_one_by_one():
    # SKDU: "kenaikan kinerja ... LU Pertanian dan LU Konstruksi" names two top-level rows of
    # Tabel 1 (above its TOTAL); it was refused as two unrelated metrics.
    t1 = _make_table(title="Tabel 1. Kegiatan Usaha", unit=_SBT, data={
        ("PERTANIAN", 2026, "Q1"): 1.54, ("PERTANIAN", 2026, "Q2"): 1.74,
        ("KONSTRUKSI", 2026, "Q1"): 0.12, ("KONSTRUKSI", 2026, "Q2"): 0.81,
        ("TOTAL", 2026, "Q1"): 10.11, ("TOTAL", 2026, "Q2"): 12.97})
    t4 = _make_table(title="Tabel 4. Penggunaan Tenaga Kerja", unit=_SBT, data={
        ("PERTANIAN", 2026, "Q1"): -0.27, ("PERTANIAN", 2026, "Q2"): -0.23,
        ("KONSTRUKSI", 2026, "Q1"): 0.5, ("KONSTRUKSI", 2026, "Q2"): 0.1,
        ("TOTAL", 2026, "Q1"): 0.28, ("TOTAL", 2026, "Q2"): 0.14})
    fact = _make_fact(operation="is_increasing", claimed_value=None, unit=None,
                      context_quote="Meningkatnya kegiatan usaha didorong oleh kenaikan kinerja "
                                    "LU Pertanian dan LU Konstruksi.",
                      periods=[_make_period(metric_label="LU Pertanian", month="Q2"),
                               _make_period(metric_label="LU Konstruksi", month="Q2")])
    r = _evaluate_fact(fact, [_make_source(t4, sheet="T4"), _make_source(t1, sheet="T1")])
    assert r.verdict == "Entailed"
    assert "T1" in r.reasoning


def test_total_growth_broken_down_by_its_sources_is_a_positive_balance():
    r = _sbt_trend("is_increasing", "TOTAL", 12.97, 11.75,
                   "Kegiatan usaha diprakirakan meningkat bersumber dari LU Industri Pengolahan "
                   "(SBT 1,47%) dan LU Konstruksi (SBT 0,95%).")
    assert r.verdict == "Entailed"


def test_tetap_tumbuh_clause_is_a_level_even_when_the_next_clause_rises():
    quote = ("Penggunaan tenaga kerja pada triwulan II 2026 tetap tumbuh dan diprakirakan "
             "meningkat pada triwulan III 2026.")
    assert _sbt_trend("is_increasing", "TOTAL", 0.28, 0.14, quote).verdict == "Entailed"


def test_anaphoric_sentence_is_not_refuted_by_the_total_standing_in_for_it():
    table = _make_table(title="Tabel 4. Penggunaan Tenaga Kerja", unit=_SBT,
                        data={("PERTANIAN", 2026, "Q3"): 0.06, ("TOTAL", 2026, "Q3"): 0.95})
    fact = _make_fact(claimed_value=0.06, unit="persen",
                      context_quote="Hal ini tecermin dari SBT jumlah tenaga kerja sebesar 0,06% (Grafik 13).",
                      periods=[_make_period(metric_label="jumlah tenaga kerja", month="Q3")])
    src = [_make_source(table, filename="SKDU.xlsx", sheet="T4")]
    assert _evaluate_fact_safely(fact, src).verdict == "Inconclusive"
    fact.claimed_value = 0.95
    assert _evaluate_fact_safely(fact, src).verdict == "Entailed"


def test_a_sector_whose_name_contains_a_sub_sector_is_not_read_as_naming_it():
    t1 = _make_table(title="Tabel 1. Kegiatan Usaha", unit=_SBT, data={
        ("PERTANIAN, KEHUTANAN DAN PERIKANAN", 2026, "Q1"): 1.54,
        ("PERTANIAN, KEHUTANAN DAN PERIKANAN", 2026, "Q2"): 1.74,
        ("PERTANIAN, KEHUTANAN DAN PERIKANAN > Perikanan", 2026, "Q1"): 0.19,
        ("PERTANIAN, KEHUTANAN DAN PERIKANAN > Perikanan", 2026, "Q2"): 0.15})
    fact = _make_fact(operation="is_increasing", claimed_value=None, unit=None,
                      context_quote="Kegiatan usaha LU Pertanian, Kehutanan, dan Perikanan pada "
                                    "triwulan II 2026 diindikasikan meningkat.",
                      periods=[_make_period(metric_label="PERTANIAN, KEHUTANAN DAN PERIKANAN", month="Q1"),
                               _make_period(metric_label="PERTANIAN, KEHUTANAN DAN PERIKANAN", month="Q2")])
    r = _evaluate_fact(fact, [_make_source(t1, filename="SKDU.xlsx", sheet="T1")])
    assert r.verdict == "Entailed"
    assert r.periods[-1].excel_value == 1.74


# ---------------------------------------------------------------------------
# NPI Tw II-2026 (BPM6 signs, joined rows, published growth rows)
# ---------------------------------------------------------------------------

def _npi_table(data):
    return _make_table(title="NERACA PEMBAYARAN INDONESIA", unit="Juta USD", data=data)


def test_an_import_printed_as_a_positive_amount_matches_the_negative_debit_row():
    table = _npi_table({("Minyak > Impor", 2026, "Q2"): -11607.23})
    fact = _make_fact(
        periods=[_make_period(metric_label="Impor minyak", month="Q2")],
        claimed_value=11.6, unit="miliar dolar AS",
        context_quote="Impor minyak pada triwulan II 2026 tercatat sebesar 11,6 miliar dolar AS.",
    )

    assert _evaluate_fact(fact, [_make_source(table)]).verdict == "Entailed"


def test_rising_imports_on_a_debit_row_are_judged_on_their_size():
    table = _npi_table({("Gas > Impor", 2026, "Q1"): -682.36, ("Gas > Impor", 2026, "Q2"): -1529.31})
    quote = "Impor gas pada triwulan II 2026 meningkat dari 0,7 miliar dolar AS pada triwulan I 2026."
    rising = _make_fact(
        operation="is_increasing", claimed_value=None, unit=None, context_quote=quote,
        periods=[_make_period(metric_label="Impor gas", month="Q1"),
                 _make_period(metric_label="Impor gas", month="Q2")],
    )
    falling = _make_fact(
        operation="is_decreasing", claimed_value=None, unit=None, context_quote=quote,
        periods=[_make_period(metric_label="Impor gas", month="Q1"),
                 _make_period(metric_label="Impor gas", month="Q2")],
    )

    assert _evaluate_fact(rising, [_make_source(table)]).verdict == "Entailed"
    assert _evaluate_fact(falling, [_make_source(table)]).verdict == "Refuted"


def test_a_deficit_that_widens_later_in_the_sentence_is_a_size_change():
    table = _npi_table({("Transaksi Berjalan", 2026, "Q1"): -3576.01,
                        ("Transaksi Berjalan", 2026, "Q2"): -12487.42})
    fact = _make_fact(
        operation="diff", claimed_value=8.9, unit="miliar USD",
        periods=[_make_period(metric_label="Transaksi Berjalan", month="Q1"),
                 _make_period(metric_label="Transaksi Berjalan", month="Q2")],
        context_quote=(
            "defisit transaksi berjalan pada triwulan II 2026 tercatat sebesar 12,5 miliar dolar "
            "AS (3,3% dari PDB), melebar dibandingkan dengan defisit pada triwulan sebelumnya."
        ),
    )

    assert _evaluate_fact(fact, [_make_source(table)]).verdict == "Entailed"


def test_a_value_named_for_two_rows_is_their_sum():
    table = _npi_table({("Transaksi Modal", 2026, "Q1"): 4.28,
                        ("Transaksi Finansial", 2026, "Q1"): -4765.37})
    quote = "transaksi modal dan finansial mencatat defisit 4,8 miliar dolar AS pada triwulan I 2026"
    joined_label = _make_fact(
        periods=[_make_period(metric_label="Transaksi Modal dan Finansial", month="Q1")],
        claimed_value=-4.8, unit="miliar USD", context_quote=quote,
    )
    two_rows = _make_fact(
        periods=[_make_period(metric_label="Transaksi Modal", month="Q1"),
                 _make_period(metric_label="Transaksi Finansial", month="Q1")],
        claimed_value=4.8, unit="miliar USD", context_quote=quote,
    )

    assert _evaluate_fact(joined_label, [_make_source(table)]).verdict == "Entailed"
    assert _evaluate_fact(two_rows, [_make_source(table)]).verdict == "Entailed"


def test_a_joined_name_with_its_own_row_is_read_from_that_row():
    table = _npi_table({("Transaksi Modal", 2026, "Q1"): 4.28,
                        ("Transaksi Finansial", 2026, "Q1"): -4765.37,
                        ("Transaksi Modal dan Finansial", 2026, "Q1"): -100.0})
    fact = _make_fact(
        periods=[_make_period(metric_label="Transaksi Modal dan Finansial", month="Q1")],
        claimed_value=-4.8, unit="miliar USD", context_quote="transaksi modal dan finansial",
    )

    assert _evaluate_fact(fact, [_make_source(table)]).verdict == "Refuted"


def test_a_yoy_claim_is_answered_by_the_growth_row_the_table_publishes():
    # Computed from the BPM6 row it is 9,81; the memorandum prints 8,49.
    table = _npi_table({
        ("Nonmigas > Ekspor", 2026, "Q2"): 70459.06,
        ("Nonmigas > Ekspor", 2025, "Q2"): 64165.87,
        ("Pertumbuhan (%, yoy) > Total Ekspor (fob) > Nonmigas", 2026, "Q2"): 8.49,
    })
    fact = _make_fact(
        operation="yoy_growth", claimed_value=8.5, unit="persen_yoy",
        periods=[_make_period(metric_label="Nonmigas > Ekspor", month="Q2")],
        context_quote="Secara tahunan, ekspor nonmigas tumbuh 8,5% (yoy).",
    )

    result = _evaluate_fact(fact, [_make_source(table)])

    assert result.verdict == "Entailed"
    assert "Pertumbuhan" in result.reasoning


def test_held_not_unchanged_a_rising_ratio_that_stays_safe_is_not_refuted_as_unstable():
    table = _npi_table({("Transaksi Berjalan (% PDB)", 2026, "Q1"): -0.97,
                        ("Transaksi Berjalan (% PDB)", 2026, "Q2"): -3.34})
    quote = ("Rasio defisit transaksi berjalan terhadap PDB pada triwulan II 2026 tetap terjaga, "
             "meski lebih tinggi dibandingkan triwulan sebelumnya.")
    periods = [_make_period(metric_label="Transaksi Berjalan (% PDB)", month="Q1"),
               _make_period(metric_label="Transaksi Berjalan (% PDB)", month="Q2")]
    stable = _make_fact(operation="is_stable", claimed_value=None, unit=None,
                        context_quote=quote, periods=periods)
    rising = _make_fact(operation="is_increasing", claimed_value=None, unit=None,
                        context_quote=quote, periods=list(periods))

    assert _evaluate_fact(stable, [_make_source(table)]).verdict == "Inconclusive"
    assert _evaluate_fact(rising, [_make_source(table)]).verdict == "Entailed"


def test_a_volume_trend_is_not_judged_on_a_dollar_row():
    table = _npi_table({("Minyak > Impor", 2026, "Q1"): -7291.32,
                        ("Minyak > Impor", 2026, "Q2"): -11607.23})
    fact = _make_fact(
        operation="is_decreasing", claimed_value=None, unit=None,
        periods=[_make_period(metric_label="Impor minyak mentah", month="Q1"),
                 _make_period(metric_label="Impor minyak mentah", month="Q2")],
        context_quote="Dari sisi volume, impor minyak mentah menurun dari 36 juta barel menjadi 31 juta barel.",
    )

    assert _evaluate_fact(fact, [_make_source(table)]).verdict == "Inconclusive"


def test_a_row_that_states_its_own_unit_is_compared_in_it():
    table = _npi_table({("Transaksi Berjalan (% PDB)", 2026, "Q2"): -3.34,
                        ("Jumlah pelawat (ribuan orang) > Ke luar negeri", 2026, "Q2"): 1960.82})
    ratio = _make_fact(
        periods=[_make_period(metric_label="Transaksi Berjalan (% PDB)", month="Q2")],
        claimed_value=3.3, unit="persen",
        context_quote="defisit transaksi berjalan tercatat 12,5 miliar dolar AS (3,3% dari PDB)",
    )
    people = _make_fact(
        periods=[_make_period(metric_label="Jumlah pelawat > Ke luar negeri", month="Q2")],
        claimed_value=2.0, unit="juta orang", context_quote="kunjungan wisnas 2,0 juta orang",
    )

    assert _evaluate_fact(ratio, [_make_source(table)]).verdict == "Entailed"
    assert _evaluate_fact(people, [_make_source(table)]).verdict == "Entailed"


@pytest.mark.parametrize("claim_unit, table_unit, factor", [
    # NPI Tw II-2026: "dari 2,5 juta wisnas menjadi 2,0 juta wisnas" against 'ribuan orang'.
    ("juta wisnas", "ribuan orang", 1000.0),
    ("juta orang", "ribuan orang", 1000.0),
    ("ribu kunjungan", "juta orang", 0.001),
    # Different kinds of quantity stay apart: barrels are not people, months are not people.
    ("juta barel", "ribuan orang", None),
    ("juta orang", "ribu ton", None),
    ("ribu bulan", "ribuan orang", None),
])
def test_counted_things_compare_in_full_numbers(claim_unit, table_unit, factor):
    assert _unit_factor(claim_unit, table_unit) == factor


def test_a_position_at_the_end_of_june_is_read_from_the_second_quarter():
    table = _npi_table({("Dalam Bulan Impor dan Pembayaran ULN Pemerintah", 2026, "Q1"): 5.8,
                        ("Dalam Bulan Impor dan Pembayaran ULN Pemerintah", 2026, "Q2"): 5.41})
    fact = _make_fact(
        periods=[_make_period(metric_label="Dalam Bulan Impor dan Pembayaran ULN Pemerintah",
                              month="Jun")],
        claimed_value=5.4, unit="bulan",
        context_quote="Posisi cadangan devisa pada akhir Juni 2026 setara 5,4 bulan impor dan pembayaran ULN pemerintah.",
    )

    assert _evaluate_fact(fact, [_make_source(table)]).verdict == "Entailed"


def test_the_end_of_a_year_is_read_from_the_fourth_quarter():
    table = _npi_table({("Cadangan Devisa", 2024, "Q4"): 155719.4, ("Cadangan Devisa", 2025, "Q4"): 156471.0})
    fact = _make_fact(
        operation="diff",
        periods=[_make_period(metric_label="Cadangan Devisa", year=2024, month="Dec"),
                 _make_period(metric_label="Cadangan Devisa", year=2025, month="Dec")],
        claimed_value=0.8, unit="miliar dolar AS",
        context_quote="Posisi cadangan devisa naik 0,8 miliar dolar AS dari akhir tahun 2024.",
    )

    assert _evaluate_fact(fact, [_make_source(table)]).verdict == "Entailed"


def test_a_table_of_positions_reads_any_december_from_the_fourth_quarter():
    # PII Tw IV-2025 s030: "Untuk keseluruhan tahun 2025, kewajiban neto PII Indonesia naik 26,9
    # miliar dolar AS (11,0% yoy) dibandingkan akhir 2024" came back Dec 2024 -> Dec 2025 and was
    # left Tidak Cukup Data: the sheet is quarterly. A position at December is the Q4 one.
    cells = {("Investasi Lainnya (neto)", 2024, "Q4"): 22059.78, ("Investasi Lainnya (neto)", 2025, "Q4"): 28055.22}
    fact = _make_fact(
        operation="yoy_growth",
        periods=[_make_period(metric_label="Investasi Lainnya (neto)", year=2025, month="Dec")],
        claimed_value=27.2, unit="persen",
        context_quote="Perkembangan ini didorong oleh aset neto investasi lainnya yang naik 27,2% (yoy).",
    )
    positions = _npi_table(cells)
    positions.positions = True

    assert _evaluate_fact(fact, [_make_source(positions)]).verdict == "Entailed"
    # A table of flows: its December is not its fourth quarter, and the sentence says no "akhir".
    assert _evaluate_fact(fact, [_make_source(_npi_table(cells))]).verdict == "Inconclusive"


def test_net_purchases_abroad_printed_positive_match_the_negative_asset_row():
    table = _npi_table({("Investasi Portofolio > Aset", 2026, "Q2"): -1487.61})
    fact = _make_fact(
        periods=[_make_period(metric_label="Investasi Portofolio > Aset", month="Q2")],
        claimed_value=1.5, unit="miliar dolar AS",
        context_quote="penduduk Indonesia tercatat melakukan pembelian neto surat berharga di luar negeri sebesar 1,5 miliar dolar AS",
    )

    assert _evaluate_fact(fact, [_make_source(table)]).verdict == "Entailed"


def test_trend_whose_stated_ends_miss_the_row_is_not_judged_on_it():
    # NPI Tw II-2026: "Ekspor NG naik dari 0,5 … menjadi 0,7" was answered by total gas exports,
    # 1.590,85 -> 1.590,55 juta, and refuted on direction alone.
    from paired_verifier import _stated_endpoints_missed

    quote = "Ekspor NG naik dari 0,5 miliar dolar AS menjadi 0,7 miliar dolar AS, terutama harga."
    assert _stated_endpoints_missed(quote, [1590.85, 1590.55], "Juta USD") == "0,5 → 0,7 miliar dolar AS"
    # The right row (rounding apart), and a debit row stored negative, are kept.
    assert _stated_endpoints_missed(quote, [540.0, 660.0], "Juta USD") is None
    assert _stated_endpoints_missed(quote, [-540.0, -660.0], "Juta USD") is None
    # One end matching is enough to keep the row; no stated ends, nothing to say.
    assert _stated_endpoints_missed(quote, [500.0, 1590.0], "Juta USD") is None
    assert _stated_endpoints_missed("Ekspor NG naik.", [1590.85, 1590.55], "Juta USD") is None


@pytest.mark.parametrize("quote,q2,verdict", [
    ("Responden menilai likuiditas perusahaan masih dalam kondisi baik pada triwulan II 2026.", 15.03, "Entailed"),
    ("Responden menilai akses kredit perbankan pada triwulan II 2026 tetap mudah.", 2.45, "Entailed"),
    ("Responden menilai akses kredit perbankan pada triwulan II 2026 tetap mudah.", -1.2, "Refuted"),
])
def test_saldo_bersih_row_in_a_respondent_table_reads_baik_and_mudah_as_positive(quote, q2, verdict):
    # SKDU Tabel 3 is in '% Responden'; only its 'Saldo Bersih' rows are balances.
    row = "Likuiditas > Saldo Bersih (% Baik - % Buruk)"
    r = _sbt_trend("is_stable", row, 17.05, q2, quote, unit="% Responden")
    assert r.verdict == verdict


def test_baik_on_a_respondent_share_row_is_not_read_as_a_balance():
    r = _sbt_trend("is_stable", "Likuiditas > Baik", 23.98, 22.78,
                   "Likuiditas perusahaan masih dalam kondisi baik.", unit="% Responden")
    assert r.verdict != "Entailed"
    assert "SBT positif" not in r.reasoning


# ---------------------------------------------------------------------------
# _section_guard — a claim under a report section the table does not hold
# ---------------------------------------------------------------------------

def _ub_m2_source():
    # SEKI I.1A's M2 component, as the UB parser hands it over (uncovered_sections set).
    table = _make_table(
        title="UANG BEREDAR DAN FAKTOR-FAKTOR YANG MEMENGARUHINYA", unit="Miliar Rp",
        data={("Simpanan Berjangka (Rupiah dan Valas)", 2026, "Aug"): 3259051.1,
              ("Simpanan Berjangka (Rupiah dan Valas)", 2025, "Aug"): 3117186.4},
    )
    table.uncovered_sections = {r"\bdana pihak ketiga\b|\bdpk\b": "Dana Pihak Ketiga (DPK)"}
    return _make_source(table, filename="TABEL1_1_1.xls", sheet="I.1A")


def _simpanan_berjangka_fact(claimed, headings):
    fact = _make_fact(
        operation="yoy_growth", claimed_value=claimed, unit="persen_yoy",
        periods=[_make_period(metric_label="Simpanan Berjangka", year=2026, month="Aug")],
        context_quote=("Sementara itu, giro dan simpanan berjangka tumbuh masing-masing sebesar "
                       "9,4% (yoy) dan 4,7% (yoy)."),
    )
    fact.section_headings = headings
    return fact


@pytest.mark.parametrize("claimed", [4.7, 4.6])
def test_section_guard_sets_aside_a_dpk_claim_judged_on_an_m2_row(claimed):
    # UB Agustus 2026: DPK's simpanan berjangka (4,7%, the report's Tabel 3) was refuted on
    # M2's row (4,55%). A match would be a coincidence too, so either verdict becomes TCD.
    fact = _simpanan_berjangka_fact(claimed, ["PERKEMBANGAN DANA PIHAK KETIGA (DPK)"])

    result = _evaluate_fact_safely(fact, [_ub_m2_source()])

    assert result.verdict == "Inconclusive"
    assert result.resolved_via == "section_guard"
    assert result.computed_value is None
    assert "PERKEMBANGAN DANA PIHAK KETIGA (DPK)" in result.reasoning


def test_section_guard_leaves_a_claim_under_another_heading_alone():
    fact = _simpanan_berjangka_fact(4.6, ["KOMPONEN UANG BEREDAR"])

    assert _evaluate_fact_safely(fact, [_ub_m2_source()]).verdict == "Entailed"


def test_section_guard_needs_the_publication_to_list_the_section():
    source = _ub_m2_source()
    source.table.uncovered_sections = {}
    fact = _simpanan_berjangka_fact(4.7, ["PERKEMBANGAN DANA PIHAK KETIGA (DPK)"])

    assert _evaluate_fact_safely(fact, [source]).verdict == "Refuted"


def test_section_guard_keeps_a_row_that_names_the_section_subject():
    table = _make_table(unit="triliun Rp", data={("DPK > Giro", 2026, "Aug"): 3094.1})
    table.uncovered_sections = {r"\bdpk\b": "DPK"}
    fact = _make_fact(claimed_value=3094.1,
                      periods=[_make_period(metric_label="DPK > Giro", year=2026, month="Aug")])
    fact.section_headings = ["PERKEMBANGAN DANA PIHAK KETIGA (DPK)"]

    assert _evaluate_fact_safely(fact, [_make_source(table)]).verdict == "Entailed"


# ---------------------------------------------------------------------------
# _absent_item_guard — a breakdown the table never splits out, judged on its total
# ---------------------------------------------------------------------------

def _npi_oil_table():
    table = _npi_table({("Minyak > Impor", 2026, "Q2"): -11607.23,
                        ("Gas > Ekspor", 2026, "Q1"): 1590.85, ("Gas > Ekspor", 2026, "Q2"): 1590.55,
                        ("Harga rata-rata ekspor minyak mentah (USD/barel)", 2026, "Q2"): 102.76,
                        ("Transaksi Berjalan", 2026, "Q2"): -12487.42})
    table.absent_items = {r"\bminyak\s+mentah\b": "minyak mentah",
                          r"\bnatural\s+gas\b|\b(?-i:NG)\b": "natural gas (NG)"}  # as npi.py
    return table


def test_absent_item_guard_sets_aside_crude_oil_judged_on_all_oil():
    # NPI Tw II-2026: "impor minyak mentah 3,8 miliar" was refuted on all oil imports (11,6).
    fact = _make_fact(
        periods=[_make_period(metric_label="Impor minyak mentah", month="Q2")],
        claimed_value=3.8, unit="miliar dolar AS",
        context_quote="Impor minyak mentah pada triwulan II 2026 meningkat menjadi sebesar 3,8 miliar dolar AS.",
    )

    result = _evaluate_fact_safely(fact, [_make_source(_npi_oil_table())])

    assert result.verdict == "Inconclusive"
    assert result.resolved_via == "absent_item_guard"


def test_absent_item_guard_sets_aside_a_natural_gas_trend_judged_on_all_gas():
    fact = _make_fact(
        operation="is_increasing", claimed_value=None, unit=None,
        periods=[_make_period(metric_label="ekspor Natural Gas (NG)", month="Q1"),
                 _make_period(metric_label="ekspor Natural Gas (NG)", month="Q2")],
        context_quote="Perkembangan tersebut dipengaruhi oleh kenaikan ekspor Natural Gas (NG).",
    )

    assert _evaluate_fact_safely(fact, [_make_source(_npi_oil_table())]).verdict == "Inconclusive"


def test_absent_item_guard_keeps_a_row_that_names_the_item():
    fact = _make_fact(
        periods=[_make_period(metric_label="Harga rata-rata ekspor minyak mentah (USD/barel)", month="Q2")],
        claimed_value=102.8, unit="dolar AS/barel",
        context_quote="harga ekspor minyak mentah naik menjadi 102,8 dolar AS/barel",
    )

    assert _evaluate_fact_safely(fact, [_make_source(_npi_oil_table())]).verdict == "Entailed"


def test_absent_item_guard_leaves_the_total_itself_alone():
    fact = _make_fact(
        periods=[_make_period(metric_label="Impor minyak", month="Q2")],
        claimed_value=11.6, unit="miliar dolar AS",
        context_quote="Impor minyak pada triwulan II 2026 tercatat sebesar 11,6 miliar dolar AS.",
    )

    assert _evaluate_fact_safely(fact, [_make_source(_npi_oil_table())]).verdict == "Entailed"


# ---------------------------------------------------------------------------
# Publication checks added after NPI Tw II-2026 / UB Agustus 2026
# ---------------------------------------------------------------------------

_RESERVES_QUOTE = ("Posisi cadangan devisa pada akhir Juni 2026 tetap terjaga sebesar 145,6 miliar dolar AS, "
                   "yang setara dengan pembiayaan 5,6 bulan impor atau 5,4 bulan impor dan pembayaran utang "
                   "luar negeri pemerintah.")
_MONTHS_ROW = "Dalam Bulan Impor dan Pembayaran Utang Luar Negeri Pemerintah"


def _reserves_source():
    table = _npi_table({(_MONTHS_ROW, 2026, "Q2"): 5.41, ("Posisi Cadangan Devisa", 2026, "Q2"): 145593.58,
                        ("Transaksi Berjalan", 2026, "Q2"): -12487.42})
    table.number_phrases = {_MONTHS_ROW: r"bulan\s+impor\s+dan\s+pembayaran"}  # as npi.py
    return _make_source(table)


@pytest.mark.parametrize("operation, claimed, verdict", [
    ("value", 5.6, "Inconclusive"),         # "5,6 bulan impor atau …" is imports only: not in the workbook
    ("value", 5.4, "Entailed"),             # "5,4 bulan impor dan pembayaran …" is this row
    ("above_threshold", 3.0, "Entailed"),   # a threshold states no figure of the row
])
def test_number_phrase_guard_reads_the_words_after_the_claims_own_number(operation, claimed, verdict):
    fact = _make_fact(operation=operation, claimed_value=claimed, unit="bulan", context_quote=_RESERVES_QUOTE,
                      periods=[_make_period(metric_label=_MONTHS_ROW, month="Q2")])

    assert _evaluate_fact_safely(fact, [_reserves_source()]).verdict == verdict


def test_a_sentence_naming_the_debt_market_is_judged_on_the_debt_child_row():
    # NPI Tw II-2026: refuted on the private portfolio liabilities (shares + debt, −1,61); the
    # 'Surat utang' child holds +1,53. The chart caption's "Bursa Saham" must not count as "saham".
    table = _npi_table({
        ("Transaksi Finansial > Investasi Portofolio > Kewajiban > Sektor swasta", 2026, "Q2"): -1609.48,
        ("Kewajiban > Sektor swasta > Surat utang", 2026, "Q2"): 1525.97,
        ("Kewajiban > Sektor swasta > Modal ekuitas", 2026, "Q2"): -3135.45,
        ("Aset > Sektor swasta > Surat utang", 2026, "Q2"): -881.09,
    })
    table.child_keywords = {r"\bsurat\s+utang\b": "Surat utang", r"\bpasar\s+saham\b": "Modal ekuitas"}
    fact = _make_fact(
        claimed_value=1.5, unit="miliar USD",
        periods=[_make_period(metric_label="Investasi Portofolio > Kewajiban > Sektor swasta", month="Q2")],
        context_quote=("Perkembangan Indeks Bursa Saham Beberapa Negara ASEAN Di pasar surat utang, sisi "
                       "kewajiban sektor swasta mencatat net inflow sebesar 1,5 miliar dolar AS."),
    )

    result = _evaluate_fact_safely(fact, [_make_source(table)])

    assert result.verdict == "Entailed"
    assert result.periods[0].metric_label == "Kewajiban > Sektor swasta > Surat utang"


def test_publication_guards_also_catch_a_verdict_the_review_put_on_the_wrong_row():
    # UB Agustus 2026: the Tinjauan AI accepted M0's "uang kartal yang diedarkan 12,9%" on M2's
    # currency row. The guards run again after the review and set it aside.
    from paired_verifier import _publication_guards

    table = _make_table(title="UANG BEREDAR", unit="Miliar Rp",
                        data={("Uang Kartal di Luar Bank Umum dan BPR", 2026, "Aug"): 1222488.2})
    table.absent_items = {r"\buang\s+kartal\s+yang\s+diedarkan\b": "uang kartal yang diedarkan (komponen M0)"}
    source = _make_source(table, filename="TABEL1_1_1.xls", sheet="I.1A")
    fact = _make_fact(operation="yoy_growth", claimed_value=12.9, unit="persen_yoy",
                      periods=[_make_period(metric_label="uang kartal yang diedarkan", year=2026, month="Aug")],
                      context_quote="… dan uang kartal yang diedarkan sebesar 12,9% (yoy).")
    reviewed = FactVerificationResult(
        metric_label="Uang Kartal di Luar Bank Umum dan BPR", operation="yoy_growth", claimed_value=12.9,
        verdict="Entailed", matched_excel_source=source.label, resolved_via="ai_review",
        reasoning="Ditinjau AI", context_quote=fact.context_quote,
        periods=[{"metric_label": "Uang Kartal di Luar Bank Umum dan BPR", "year": 2026, "month": "Aug",
                  "excel_value": 1222488.2}],
    )

    assert _publication_guards(fact, reviewed, [source]).verdict == "Inconclusive"


# ---------------------------------------------------------------------------
# PMI Tw III-2026: direction read from the sentence, PMI level phrases, T2 as one group
# ---------------------------------------------------------------------------

def _pmi_components(older, newer, label="Kecepatan Penerimaan Barang Input"):
    table = _make_table(title="PMI-BI", unit="%, Indeks",
                        data={(label, 2026, "Q2"): older, (label, 2026, "Q3"): newer,
                              ("Volume Produksi", 2026, "Q3"): 54.31})
    return table


def _pmi_trend(operation, quote, table, label="Kecepatan Penerimaan Barang Input"):
    fact = _make_fact(operation=operation, claimed_value=None, unit=None, context_quote=quote,
                      periods=[_make_period(metric_label=label, month="Q2"),
                               _make_period(metric_label=label, month="Q3")])
    return _evaluate_fact(fact, [_make_source(table)])


_IMPROVED = ("Komponen Kecepatan Penerimaan Barang Input pada triwulan III 2026 menunjukkan perbaikan "
             "meskipun masih berada pada fase kontraksi (indeks < 50%), tercermin dari indeks sebesar "
             "48,68%, lebih tinggi dibandingkan 47,46% pada triwulan II 2026 (Grafik 4).")


def test_the_sentences_own_direction_beats_the_extractors():
    # The extractor read "kontraksi" as the direction and wrote is_decreasing.
    result = _pmi_trend("is_decreasing", _IMPROVED, _pmi_components(47.46, 48.68))

    assert result.verdict == "Entailed"
    assert "arah dibaca dari kalimat" in result.reasoning


def test_a_writer_who_got_the_direction_wrong_is_still_refuted():
    # The sentence says "perbaikan … lebih tinggi" but the index fell: a real error.
    assert _pmi_trend("is_decreasing", _IMPROVED, _pmi_components(48.68, 47.46)).verdict == "Refuted"


def test_a_sentence_naming_both_directions_keeps_the_extractors():
    # SK: "IKK mengalami peningkatan tertinggi di Pontianak …, sementara penurunan IKK terbesar …"
    quote = ("Secara spasial, Kecepatan Penerimaan Barang Input mengalami peningkatan tertinggi di A, "
             "sementara penurunan terbesar terjadi di B.")
    assert _pmi_trend("is_decreasing", quote, _pmi_components(48.68, 47.46)).verdict == "Entailed"


def test_a_pmi_level_phrase_is_judged_as_a_level_not_a_change():
    # "diprakirakan tetap berada pada fase ekspansi" came back is_increasing on 54,8 -> 53,69.
    table = _pmi_components(54.8, 53.69, label="Volume Persediaan Barang Jadi")
    table.level_phrases = {r"\btetap\s+(berada\s+)?(pada|di)\s+(fase|zona)\s+ekspansi\b": ("above", 50.0)}
    quote = ("Berdasarkan komponen pembentuknya, Volume Produksi, Volume Persediaan Barang Jadi, dan "
             "Volume Total Pesanan diprakirakan tetap berada pada fase ekspansi.")

    result = _pmi_trend("is_increasing", quote, table, label="Volume Persediaan Barang Jadi")

    assert result.verdict == "Entailed"
    assert "level > 50" in result.reasoning


def test_a_table_that_is_one_breakdown_counts_its_rows_for_a_majority_claim():
    rows = {"Industri Makanan dan Minuman": 55.03, "Industri Pengolahan Tembakau": 46.41,
            "Industri Tekstil dan Pakaian Jadi": 52.40, "Industri Furnitur": 50.61}
    table = _make_table(title="PMI-BI Sub-LU", unit="%, Indeks",
                        data={(name, 2026, "Q3"): value for name, value in rows.items()})
    table.whole_table_group = (r"\bsub-?\s?lu\b", "Sub-LU")
    fact = _make_fact(operation="above_threshold", claimed_value=50.0, unit="persen",
                      context_quote="berada pada fase ekspansi", group_scope="majority",
                      periods=[_make_period(metric_label=name, month="Q3") for name in rows])

    result = _evaluate_fact_safely(fact, [_make_source(table)])

    assert result.verdict == "Entailed"
    assert "3 dari 4" in result.reasoning


def test_a_ratio_given_as_one_point_is_judged_without_crashing():
    # SULNI Okt 2026: "rasio ULN … terhadap PDB … 30,9%" came back as one point on the ratio row
    # and crashed the combined-share check (IndexError), leaving "gagal dinilai".
    table = _make_table(title="TabI.7", unit="%", data={("Rasio Utang terhadap PDB", 2026, "Aug"): 30.9,
                                                       ("Total", 2026, "Aug"): 459273.0})
    fact = _make_fact(operation="ratio", claimed_value=30.9, unit="persen",
                      periods=[_make_period(metric_label="Rasio Utang terhadap PDB", month="Aug")],
                      context_quote="rasio ULN Indonesia terhadap PDB yang tercatat sebesar 30,9% pada Agustus 2026")

    result = _evaluate_fact_safely(fact, [_make_source(table)])

    assert "gagal dinilai" not in result.reasoning
    assert result.verdict == "Entailed"  # the row holds the ratio itself


def _sbank_deposits(**rows):
    """Tabel3 of Survei Perbankan: 'Prakiraan per Triwulan > Gabungan > <item>' by quarter."""
    data = {}
    for item, values in rows.items():
        for (year, quarter), value in values.items():
            data[(f"Prakiraan per Triwulan > Gabungan > {item}", year, quarter)] = value
    table = _make_table(title="Tabel3", unit="SBT, %", data=data)
    table.layout_levels = frozenset({"prakiraan per triwulan", "gabungan"})
    return table


def _one_quarter_trend(label, quote, table):
    fact = _make_fact(operation="is_increasing", claimed_value=None, unit=None, context_quote=quote,
                      periods=[_make_period(metric_label=label, month="Q3")])
    return _evaluate_fact_safely(fact, [_make_source(table)])


_SBANK_QUARTERS = {(2025, "Q3"): 0.0, (2026, "Q2"): 0.0, (2026, "Q3"): 0.0}


def test_a_trend_compares_with_the_quarter_its_sentence_names():
    # SBank Tw II-2026: "giro … lebih tinggi dibandingkan SBT pada triwulan III 2025" (50,82 ->
    # 76,38) arrived as Q3 2026 alone, and against Q2 2026 (80,36) it read as a fall.
    table = _sbank_deposits(Giro={(2025, "Q3"): 50.82, (2026, "Q2"): 80.36, (2026, "Q3"): 76.38})
    quote = ("pada triwulan III 2026, instrumen tabungan dan giro diprakirakan tumbuh lebih tinggi "
             "dengan nilai SBT masing-masing sebesar 87,08% dan 76,38%, lebih tinggi dibandingkan SBT "
             "pada triwulan III 2025.")

    result = _one_quarter_trend("instrumen giro", quote, table)

    assert result.verdict == "Entailed"
    assert "Q3 2025=50.82" in result.reasoning


def test_a_trend_against_the_same_quarter_last_year():
    table = _sbank_deposits(Giro={(2025, "Q3"): 50.82, (2026, "Q2"): 80.36, (2026, "Q3"): 76.38})
    quote = "Giro diprakirakan meningkat dibandingkan dengan triwulan yang sama pada tahun sebelumnya."

    assert _one_quarter_trend("Giro", quote, table).verdict == "Entailed"


def test_a_trend_whose_sentence_names_no_baseline_still_uses_the_previous_quarter():
    table = _sbank_deposits(Giro={(2025, "Q3"): 50.82, (2026, "Q2"): 80.36, (2026, "Q3"): 76.38})

    result = _one_quarter_trend("Giro", "Giro diprakirakan meningkat pada triwulan III 2026.", table)

    # Down from Q2 (80,36), up from Q3 2025 (50,82): with no basis named, not refuted — both
    # readings are shown for the reader to settle.
    assert result.verdict == "Inconclusive"
    assert "Q2 2026" in result.reasoning and "80.36" in result.reasoning and "Q3 2025" in result.reasoning
    falls_both_ways = _sbank_deposits(Giro={(2025, "Q3"): 90.0, (2026, "Q2"): 80.36, (2026, "Q3"): 76.38})
    assert _one_quarter_trend("Giro", "Giro diprakirakan meningkat pada triwulan III 2026.",
                              falls_both_ways).verdict == "Refuted"


def test_layout_levels_of_a_row_are_not_a_group_the_claim_left_out():
    # "instrumen deposito diprakirakan tetap tumbuh" was left Tidak Cukup Data: 'Prakiraan per
    # Triwulan' and 'Gabungan' (all banks) read as groups of deposits the claim did not name.
    table = _sbank_deposits(Deposito={(2025, "Q3"): 58.75, (2026, "Q2"): 72.7, (2026, "Q3"): 40.97},
                            Giro={(2025, "Q3"): 50.82, (2026, "Q2"): 80.36, (2026, "Q3"): 76.38})
    quote = ("Sementara itu, instrumen deposito diprakirakan tetap tumbuh dengan nilai SBT sebesar "
             "40,97%.")

    result = _one_quarter_trend("instrumen deposito", quote, table)

    assert result.verdict == "Entailed"  # a positive balance: 'tetap tumbuh'
    assert "tetap tumbuh" in result.reasoning


def test_without_layout_levels_the_group_guard_still_holds():
    table = _sbank_deposits(Deposito={(2025, "Q3"): 58.75, (2026, "Q2"): 72.7, (2026, "Q3"): 40.97},
                            Giro={(2025, "Q3"): 50.82, (2026, "Q2"): 80.36, (2026, "Q3"): 76.38})
    table.layout_levels = frozenset()

    result = _one_quarter_trend("instrumen deposito", "instrumen deposito diprakirakan tetap tumbuh.",
                                table)

    assert result.verdict == "Inconclusive"


@patch("paired_verifier.extract_structured_facts_async")
@patch("paired_verifier.parse_bi_table")
def test_verify_paired_does_not_show_the_extractor_a_skipped_section(mock_parse, mock_extract_facts):
    # SBank: "Nilai SBT > 0 berarti lebih ketat" in the METODOLOGI came back as two threshold claims.
    mock_parse.return_value = _make_table(data={})
    mock_extract_facts.return_value = []
    narrative = ("[== Halaman 1 ==]\nKredit baru tumbuh dengan SBT 93,08%." + " x" * 120
                 + "\nMETODOLOGI\nNilai SBT > 0 berarti lebih ketat.\n[== Halaman 2 ==]\nTabel 1")

    for publication, shown in (("sbank", False), ("", True)):
        asyncio.run(verify_paired(narrative_text=narrative,
                                  excel_sources=[(b"xls-bytes", "I.1", "TABEL1_1.xls")],
                                  llm=Mock(), publication=publication))
        sent = mock_extract_facts.call_args.args[0]
        assert ("berarti lebih ketat" in sent) is shown
        assert "SBT 93,08%" in sent


def test_a_net_balance_sentence_that_compares_with_nothing_is_about_the_level():
    # SBank Tw I-2026: "didukung oleh perkiraan pertumbuhan seluruh kategori DPK yaitu … Deposito
    # (72,70%)" came back is_increasing on 73,81 -> 72,70 and was refuted.
    table = _sbank_deposits(Deposito={(2026, "Q1"): 73.81, (2026, "Q2"): 72.70})
    quote = ("Hal ini terutama didukung oleh perkiraan pertumbuhan seluruh kategori DPK yaitu "
             "Tabungan (82,47%), Giro (SBT 80,36%), dan Deposito (72,70%).")
    fact = _make_fact(operation="is_increasing", claimed_value=None, unit=None, context_quote=quote,
                      periods=[_make_period(metric_label="Deposito", month="Q2")])

    result = _evaluate_fact_safely(fact, [_make_source(table)])

    assert result.verdict == "Entailed"
    assert "tidak membandingkan" in result.reasoning


def test_a_net_balance_sentence_that_compares_stays_a_trend():
    table = _sbank_deposits(Deposito={(2026, "Q1"): 73.81, (2026, "Q2"): 72.70})
    quote = "Deposito diprakirakan meningkat pada triwulan II 2026 dengan SBT 72,70%."
    fact = _make_fact(operation="is_increasing", claimed_value=None, unit=None, context_quote=quote,
                      periods=[_make_period(metric_label="Deposito", month="Q2")])

    assert _evaluate_fact_safely(fact, [_make_source(table)]).verdict == "Refuted"


def test_a_level_said_negative_is_not_read_as_positive():
    table = _sbank_deposits(Deposito={(2026, "Q1"): 3.0, (2026, "Q2"): -2.0})
    quote = "Deposito diprakirakan terkontraksi pada triwulan II 2026."
    fact = _make_fact(operation="is_increasing", claimed_value=None, unit=None, context_quote=quote,
                      periods=[_make_period(metric_label="Deposito", month="Q2")])

    assert "tidak membandingkan" not in _evaluate_fact_safely(fact, [_make_source(table)]).reasoning


def test_a_threshold_claim_on_a_growth_table_without_a_bound_stays_unjudged():
    table = _make_table(unit="%, yoy", data={("Kredit", 2026, "Jun"): 7.6, ("Dana", 2026, "Jun"): 6.0})
    fact = _make_fact(operation="above_threshold", claimed_value=None, unit=None,
                      periods=[_make_period(metric_label="Kredit", month="Jun")])

    assert _evaluate_fact_safely(fact, [_make_source(table)]).verdict == "Inconclusive"


_SECTORS = ("PERTANIAN, KEHUTANAN DAN PERIKANAN", "KONSTRUKSI", "REAL ESTAT")


def _skdu_sector_sheet(title, values, total=(1.0, 1.0), unit="% Saldo Bersih Tertimbang - SBT"):
    """An SKDU sector sheet: each sector's (Q2, Q3) 2026 balances above a TOTAL row."""
    data = {}
    for row, (q2, q3) in {**values, "TOTAL": total}.items():
        data[(row, 2026, "Q2")], data[(row, 2026, "Q3")] = q2, q3
    table = _make_table(title=title, unit=unit, data=data)
    table.measure_levels = frozenset({"saldo bersih", "sbt"})
    return table


def _skdu_sources():
    kegiatan = _skdu_sector_sheet("Tabel 1. Kegiatan Usaha / Table 1. Business Activity",
                           {s: (1.74, 0.39) for s in _SECTORS})
    kegiatan.subject_words = frozenset({"kinerja"})
    tenaga = _skdu_sector_sheet("TABEL 4. PENGGUNAAN TENAGA KERJA / TABLE 4. LABOUR UTILISATION",
                         {s: (-0.23, -0.10) for s in _SECTORS})
    return [_make_source(kegiatan, filename="SKDU.xlsx", sheet="T1 Kegiatan Usaha"),
            _make_source(tenaga, filename="SKDU.xlsx", sheet="T4 Penggunaan Tenaga Kerja")]


def test_a_level_that_restates_the_tables_measure_is_dropped_from_the_claim():
    # SKDU Tw III-2026: the extractor wrote "Pertanian, Kehutanan, dan Perikanan > Saldo Bersih"
    # (Tabel 8's row style) for a Tabel 4 row that has no such level, and found nothing.
    table = _skdu_sources()[1].table

    assert table.lookup_fuzzy("Pertanian, Kehutanan, dan Perikanan > Saldo Bersih", 2026, "Q3") == (
        "PERTANIAN, KEHUTANAN DAN PERIKANAN", -0.10)


def test_a_trend_without_a_number_goes_to_the_tied_table_its_sentence_names():
    # Both sheets hold the sector; "penggunaan tenaga kerja" is Tabel 4 (-0,23 -> -0,10), not
    # Tabel 1 (1,74 -> 0,39), which came first and refuted a true claim.
    quote = ("Sejalan dengan kegiatan usahanya, tingkat penggunaan tenaga kerja LU Pertanian, "
             "Kehutanan, dan Perikanan terindikasi mengalami perbaikan dari SBT -0,23% menjadi -0,10%.")
    fact = _make_fact(operation="is_increasing", claimed_value=None, unit=None, context_quote=quote,
                      periods=[_make_period(metric_label="Pertanian, Kehutanan, dan Perikanan > Saldo Bersih",
                                            month="Q3")])

    result = _evaluate_fact_safely(fact, _skdu_sources())

    assert result.verdict == "Entailed"
    assert "T4 Penggunaan Tenaga Kerja" in result.reasoning


def test_a_trend_goes_to_the_tied_table_whose_figure_its_sentence_prints():
    # SKDU Tw II-2026: "beberapa sub-LU yang terindikasi turun antara lain … (SBT -0,13%)" names
    # no table, and Tabel 1 (0,03 -> 0,09) refuted it; the -0,13 is Tabel 4's.
    sources = _skdu_sources()
    sources[0].table._data.update({("KONSTRUKSI", 2026, "Q2"): 0.03, ("KONSTRUKSI", 2026, "Q3"): 0.09})
    sources[1].table._data.update({("KONSTRUKSI", 2026, "Q2"): -0.09, ("KONSTRUKSI", 2026, "Q3"): -0.13})
    quote = "Namun demikian, beberapa LU yang terindikasi turun antara lain LU Konstruksi (SBT -0,13%)."
    fact = _make_fact(operation="is_decreasing", claimed_value=None, unit=None, context_quote=quote,
                      periods=[_make_period(metric_label="Konstruksi", month="Q3")])

    result = _evaluate_fact_safely(fact, sources)

    assert result.verdict == "Entailed"
    assert "T4 Penggunaan Tenaga Kerja" in result.reasoning


@pytest.mark.parametrize("value, quote, printed", [
    (-0.07, "Industri Logam Dasar (SBT -0,07%)", True),
    (0.07, "Industri Logam Dasar (SBT -0,07%)", False),
    (0.07, "LU Konstruksi (SBT 0,07%)", True),
    (-0.13, "LU Konstruksi (SBT 0,13%)", False),
])
def test_a_printed_figure_carries_its_sign(value, quote, printed):
    assert _printed_signed(value, quote) is printed


def test_a_small_move_of_a_net_balance_is_stable():
    # SKDU Tw II-2026: "tingkat penggunaan tenaga kerja LU Industri Pengolahan … relatif stabil" at
    # SBT -0,47 -> -0,54 was Tidak Sesuai on a band of 0,05.
    table = _make_table(title="Tabel 4. Penggunaan Tenaga Kerja", unit="% Saldo Bersih Tertimbang - SBT",
                        data={("INDUSTRI PENGOLAHAN", 2026, "Q1"): -0.47,
                              ("INDUSTRI PENGOLAHAN", 2026, "Q2"): -0.54})
    fact = _make_fact(operation="is_stable", claimed_value=None, unit=None,
                      context_quote="tingkat penggunaan tenaga kerja LU Industri Pengolahan terindikasi relatif stabil",
                      periods=[_make_period(metric_label="Industri Pengolahan", month="Q1"),
                               _make_period(metric_label="Industri Pengolahan", month="Q2")])

    assert _evaluate_fact(fact, [_make_source(table)]).verdict == "Entailed"


def test_a_list_of_sectors_is_read_on_the_table_the_report_calls_kinerja():
    # "Kinerja sejumlah LU tercatat meningkat, antara lain LU A, B, C": every sheet lists the
    # sectors, and no title word is in the sentence — SKDU's 'kinerja' names Tabel 1.
    sources = _skdu_sources()
    sources[0].table._data.update({(s, 2026, "Q3"): 2.0 for s in _SECTORS})
    quote = "Kinerja sejumlah LU tercatat meningkat, antara lain LU Pertanian, LU Konstruksi, serta LU Real Estat."
    fact = _make_fact(operation="is_increasing", claimed_value=None, unit=None, context_quote=quote,
                      periods=[_make_period(metric_label=s, month="Q3") for s in _SECTORS])

    result = _evaluate_fact_safely(fact, sources)

    assert result.verdict == "Entailed"
    assert "T1 Kegiatan Usaha" in result.reasoning


def _margin_table():
    table = _make_table(title="Tabel 10. Margin Usaha", unit="%", data={
        ("Total > Perkiraan Margin Usaha", 2025, "S2"): 16.70,
        ("Total > Perkiraan Margin Usaha", 2026, "S1"): 16.02,
        ("Total > Perkiraan Margin Usaha", 2026, "S2"): 16.51,
    })
    return [_make_source(table, filename="SKDU.xlsx", sheet="T10 Margin Usaha")]


_MARGIN = ("Margin usaha pada semester II 2026 diprakirakan lebih tinggi dibandingkan semester I 2026, "
           "namun lebih rendah dari semester II 2025.")


@pytest.mark.parametrize("operation, baseline", [("is_increasing", "S1 2026=16.02"),
                                                  ("is_decreasing", "S2 2025=16.7")])
def test_a_semester_written_as_a_quarter_is_read_as_the_semester_with_its_own_baseline(operation, baseline):
    # The extractor wrote "semester II 2026" as Q2 2026 against a table of semesters; the sentence
    # names two baselines, one per direction.
    fact = _make_fact(operation=operation, claimed_value=None, unit=None, context_quote=_MARGIN,
                      periods=[_make_period(metric_label="Perkiraan Margin Usaha", year=2026, month="Q2")])

    result = _evaluate_fact_safely(fact, _margin_table())

    assert result.verdict == "Entailed"
    assert baseline in result.reasoning


def test_a_quarter_the_sentence_does_not_call_a_semester_stays_a_quarter():
    fact = _make_fact(operation="is_increasing", claimed_value=None, unit=None,
                      context_quote="Margin usaha triwulan II 2026 meningkat.",
                      periods=[_make_period(metric_label="Perkiraan Margin Usaha", year=2026, month="Q2")])

    assert _evaluate_fact_safely(fact, _margin_table()).verdict == "Inconclusive"


def _wages_and_investment():
    """SKDU Tabel 8 (wages) and 7b (investment): the same sectors, semester columns."""
    def sheet(title, values):
        data = {}
        for row, (s1, s2) in values.items():
            data[(row, 2026, "S1")], data[(row, 2026, "S2")] = s1, s2
        return _make_table(title=title, unit="%, Saldo Bersih", data=data)
    upah = sheet("TABEL 8. PERKEMBANGAN UPAH / TABLE 8. WAGES/SALARIES", {
        "Industri Pengolahan > Naik": (55.0, 12.0), "Industri Pengolahan > Saldo Bersih": (52.47, 9.18),
        "Jasa Lainnya > Naik": (45.0, 9.0), "Jasa Lainnya > Saldo Bersih": (43.75, 7.32),
        "Konstruksi > Naik": (28.0, 6.0), "Konstruksi > Saldo Bersih": (26.49, 4.92),
        "TOTAL > Saldo Bersih": (39.20, 9.24),
    })
    investasi = sheet("TABEL 7b. INVESTASI SEMESTERAN", {
        "Industri Pengolahan > Saldo Bersih": (1.0, 2.0), "Jasa Lainnya > Saldo Bersih": (1.0, 2.0),
        "Konstruksi > Saldo Bersih": (1.0, 2.0),
    })
    return [_make_source(investasi, filename="SKDU.xlsx", sheet="T7b Investasi Semesteran"),
            _make_source(upah, filename="SKDU.xlsx", sheet="T8 Perkembangan Upah")]


_ALL_LU = ("Berdasarkan LU, seluruh LU terindikasi melambat dibandingkan semester I 2026, terutama pada LU "
           "yang mencatat penurunan SB tertinggi, antara lain LU Industri Pengolahan (SB 9,18%), serta LU "
           "Jasa Lainnya (SB 7,32%) (Lampiran Tabel 8).")


def test_seluruh_lu_with_the_bare_group_word_is_judged_on_every_sector_of_the_cited_table():
    # SKDU Tw III-2026: the trend came back on 'LU' at Q3 and Q4 2026 (semester II as two
    # quarters), the named sectors as facts of their own; Tabel 7b has the same sectors and
    # semesters, but the sentence cites Tabel 8.
    trend = _make_fact(operation="is_decreasing", claimed_value=None, unit=None, context_quote=_ALL_LU,
                       periods=[_make_period(metric_label="LU", month="Q3"),
                                _make_period(metric_label="LU", month="Q4")])
    named = [_make_fact(operation="value", claimed_value=v, unit="persen", context_quote=_ALL_LU,
                        periods=[_make_period(metric_label=label, month="Q3"),
                                 _make_period(metric_label=label, month="Q4")])
             for label, v in (("LU Industri Pengolahan", 9.18), ("LU Jasa Lainnya", 7.32))]
    facts = [trend, *named]

    _attach_named_members(facts)
    result = _evaluate_fact_safely(trend, _wages_and_investment())

    assert trend.group_scope == "all"
    assert result.verdict == "Entailed"
    assert "T8 Perkembangan Upah" in result.reasoning
    assert "3 dari 3" in result.reasoning  # Konstruksi too, though the sentence never names it
    assert "Saldo Bersih" in result.reasoning and "> Naik" not in result.reasoning


def test_two_quarters_of_a_half_are_read_as_that_semester():
    fact = _make_fact(operation="value", claimed_value=9.24, unit="persen",
                      context_quote="Kondisi ini terindikasi dari SB upah sebesar 9,24% pada semester ini.",
                      periods=[_make_period(metric_label="TOTAL > Saldo Bersih", month="Q3"),
                               _make_period(metric_label="TOTAL > Saldo Bersih", month="Q4")])

    result = _evaluate_fact_safely(fact, _wages_and_investment())

    assert result.verdict == "Entailed"
    assert result.periods[0].month == "S2"


def test_two_quarters_stay_quarters_when_the_sentence_speaks_of_triwulan():
    moved = _semester_periods(_make_fact(
        operation="is_increasing", claimed_value=None, unit=None,
        context_quote="Upah semester II naik dari triwulan III ke triwulan IV.",
        periods=[_make_period(metric_label="TOTAL > Saldo Bersih", month="Q3"),
                 _make_period(metric_label="TOTAL > Saldo Bersih", month="Q4")]), _wages_and_investment())

    assert moved is None


def _skdu_finance():
    """SKDU Tabel 3: answer shares and the balance under each aspect, with its section heading."""
    head = "Kondisi keuangan selama 3 bulan terakhir :"
    rows = {"Baik": (22.78, 23.42), "Normal": (69.47, 68.72), "Buruk": (7.75, 7.86),
            "Saldo Bersih (% Baik - % Buruk)": (15.03, 15.56)}
    data = {}
    for aspect in ("- Likuiditas", "- Rentabilitas"):
        for leaf, (q2, q3) in rows.items():
            label = f"{head} > {aspect} > {leaf}"
            data[(label, 2026, "Q2")], data[(label, 2026, "Q3")] = q2, q3
    table = _make_table(title="Tabel 3. Keuangan dan Akses Kredit", unit="% Responden", data=data)
    table.layout_levels = frozenset({head.lower()})
    return [_make_source(table, filename="SKDU.xlsx", sheet="T3 Keuangan dan Akses Kredit")]


def test_an_aspect_said_to_improve_is_read_on_its_balance_not_on_one_answers_share():
    # SKDU Tw III-2026 run 2: "kondisi keuangan … membaik pada aspek Likuiditas" came back on
    # 'Likuiditas', found '… > Baik' and was refused as one group of it.
    fact = _make_fact(operation="is_increasing", claimed_value=None, unit=None,
                      context_quote="Kondisi keuangan dunia usaha secara umum membaik pada aspek Likuiditas.",
                      periods=[_make_period(metric_label="Likuiditas", month="Q3")])

    result = _evaluate_fact_safely(fact, _skdu_finance())

    assert result.verdict == "Entailed"
    assert "Q2 2026=15.03, Q3 2026=15.56" in result.reasoning


def test_a_claim_about_one_answers_share_keeps_its_row():
    fact = _make_fact(operation="value", claimed_value=23.42, unit="persen",
                      context_quote="Persentase responden yang menjawab likuiditas baik sebesar 23,42%.",
                      periods=[_make_period(metric_label="Likuiditas > Baik", month="Q3")])

    assert _evaluate_fact_safely(fact, _skdu_finance()).verdict == "Entailed"


def _average_wages():
    table = _make_table(title="TABEL 9. UPAH RATA-RATA", unit="Rp", data={
        ("Total > Dibawah Mandor", 2026, "S1"): 3907771.59, ("Total > Dibawah Mandor", 2026, "S2"): 3865176.52,
    })
    quarterly = _make_table(title="Tabel 1. Kegiatan Usaha", unit="% Saldo Bersih Tertimbang - SBT",
                            data={("TOTAL", 2026, "Q2"): 12.97, ("TOTAL", 2026, "Q3"): 11.46})
    return [_make_source(quarterly, filename="SKDU.xlsx", sheet="T1 Kegiatan Usaha"),
            _make_source(table, filename="SKDU.xlsx", sheet="T9 Upah Rata-rata")]


def test_a_quarter_on_a_row_kept_only_per_semester_is_that_semester():
    # "upah pegawai level di bawah mandor/supervisor sebesar Rp3,87 juta per bulan" follows the
    # sentence naming semester II 2026 and came back as Q2 2026.
    fact = _make_fact(operation="value", claimed_value=3.87, unit="juta Rp",
                      context_quote="Sementara itu, upah pegawai level di bawah mandor/supervisor sebesar Rp3,87 juta per bulan.",
                      periods=[_make_period(metric_label="Total > Dibawah Mandor", month="Q2")])

    result = _evaluate_fact_safely(fact, _average_wages())

    assert result.verdict == "Entailed"
    assert result.periods[0].month == "S2"


def test_a_quarter_said_as_triwulan_is_not_moved_to_a_semester():
    fact = _make_fact(operation="value", claimed_value=3.87, unit="juta Rp",
                      context_quote="Upah di bawah mandor pada triwulan II 2026 sebesar Rp3,87 juta.",
                      periods=[_make_period(metric_label="Total > Dibawah Mandor", month="Q2")])

    assert _evaluate_fact_safely(fact, _average_wages()).verdict == "Inconclusive"


def _labour():
    data = {("TOTAL", 2025, "Q2"): -0.34, ("TOTAL", 2025, "Q3"): -0.21,
            ("TOTAL", 2026, "Q2"): 0.14, ("TOTAL", 2026, "Q3"): 0.07}
    table = _make_table(title="TABEL 4. PENGGUNAAN TENAGA KERJA", unit="% Saldo Bersih Tertimbang - SBT", data=data)
    return [_make_source(table, filename="SKDU.xlsx", sheet="T4 Penggunaan Tenaga Kerja")]


_LABOUR = ("Pada triwulan III 2026, penggunaan tenaga kerja terindikasi tetap tumbuh, dengan SBT sebesar "
           "0,07%, meski sedikit lebih rendah dibandingkan dengan SBT triwulan sebelumnya sebesar 0,14%.")


@pytest.mark.parametrize("value, month", [(0.07, "Q3"), (0.14, "Q2")])
def test_a_balance_the_extractor_read_as_yoy_growth_is_checked_as_a_value(value, month):
    # SKDU Tw III-2026: both balances came back as yoy growth, and the engine computed the
    # balance's own growth (133%, 141%) and refuted true sentences.
    fact = _make_fact(operation="yoy_growth", claimed_value=value, unit="persen", context_quote=_LABOUR,
                      periods=[_make_period(metric_label="penggunaan tenaga kerja", month=month)])

    result = _evaluate_fact_safely(fact, _labour())

    assert result.verdict == "Entailed"
    assert result.operation == "value"


def test_a_number_marked_yoy_stays_a_growth_claim():
    from paired_verifier import _balance_read_as_growth
    fact = _make_fact(operation="yoy_growth", claimed_value=2.61, unit="persen_yoy",
                      context_quote="Responden memprakirakan inflasi sebesar 2,61% (yoy) dengan SB 2,61% (yoy).",
                      periods=[_make_period(metric_label="Inflasi", month="Q3")])

    assert _balance_read_as_growth(fact) is None


def test_a_cited_table_that_cannot_answer_is_not_replaced_by_another():
    # "LU Jasa Lainnya (SB 7,32%) … (Lampiran Tabel 8)" at Q3: the wages table keeps semesters
    # only, and Tabel 4's 'JASA LAINNYA' (-0,04) is not what the author pointed at.
    labour = _make_table(title="TABEL 4. PENGGUNAAN TENAGA KERJA", unit="% Saldo Bersih Tertimbang - SBT",
                         data={("JASA LAINNYA", 2026, "Q3"): -0.04, ("TOTAL", 2026, "Q3"): 0.07})
    wages = _make_table(title="TABEL 8. PERKEMBANGAN UPAH", unit="%, Saldo Bersih",
                        data={("Jasa Lainnya > Saldo Bersih", 2026, "S2"): 7.32,
                              ("TOTAL > Saldo Bersih", 2026, "S2"): 9.24})
    fact = _make_fact(operation="value", claimed_value=7.32, unit="persen",
                      context_quote="… antara lain LU Jasa Lainnya (SB 7,32%) (Lampiran Tabel 8).",
                      periods=[_make_period(metric_label="LU Jasa Lainnya", month="Q3")])

    result = _evaluate_fact_safely(fact, [_make_source(labour, sheet="T4"), _make_source(wages, sheet="T8")])

    assert result.verdict == "Inconclusive"
    assert "merujuk Tabel 8" in result.reasoning


def _shpr_growth():
    """SHPR Tabel 3: every city under a quarterly and a yearly band, one unit for both."""
    data = {("TRIWULANAN (QTQ) > PADANG > TOTAL", 2026, "Q1"): 1.08,
            ("TRIWULANAN (QTQ) > PADANG > TOTAL", 2026, "Q2"): -0.25,
            ("TAHUNAN (YOY) > PADANG > TOTAL", 2026, "Q1"): 1.21,
            ("TAHUNAN (YOY) > PADANG > TOTAL", 2026, "Q2"): 0.63}
    table = _make_table(title="TABEL 3", unit="%, qtq & yoy", data=data)
    return [_make_source(table, filename="SHPR.xlsx", sheet="TABEL 3")]


_PADANG = ("IHPR di sejumlah kota tercatat tumbuh lebih rendah dibandingkan triwulan sebelumnya, terutama "
           "di Padang yang terkontraksi sebesar {}% (qtq) (Tabel 3).")


@pytest.mark.parametrize("printed, value, verdict", [("0,25", -0.25, "Entailed"), ("0,26", -0.26, "Refuted")])
def test_a_qtq_claim_is_read_in_the_quarterly_band(printed, value, verdict):
    # SHPR Tw II-2026: the claim found the yearly band's Padang (0,63), was refused as the wrong
    # kind, and the report's 0,26 — the table says -0,25 — went unchecked.
    fact = _make_fact(operation="value", claimed_value=value, unit="persen",
                      context_quote=_PADANG.format(printed),
                      periods=[_make_period(metric_label="Padang > TOTAL", month="Q2")])

    result = _evaluate_fact_safely(fact, _shpr_growth())

    assert result.verdict == verdict
    assert "TRIWULANAN (QTQ) > PADANG > TOTAL" in result.reasoning


def test_a_growth_section_under_a_level_unit_is_not_a_band():
    # NPI's 'Id' keeps a 'Pertumbuhan (%, yoy)' section under 'Juta USD'; its rows are not bands.
    from paired_verifier import _in_the_claims_band
    table = _make_table(title="NPI", unit="Juta USD", data={
        ("Pertumbuhan (%, yoy) > Ekspor", 2026, "Q2"): 5.0, ("Ekspor", 2026, "Q2"): 70000.0})
    fact = _make_fact(operation="value", claimed_value=5.0, unit="persen",
                      context_quote="Ekspor tumbuh 5,0% (yoy).",
                      periods=[_make_period(metric_label="Ekspor", month="Q2")])

    assert _in_the_claims_band(fact, _make_source(table)) is None


def test_a_combined_share_is_not_summed_from_one_group_the_sentence_never_names():
    # SULNI Okt 2026: with the denominator named only 'TOTAL', Tbl III.6 (by kind of borrower)
    # answered first and summed its 'Perusahaan Induk' sector rows (62,4%) against the claim's
    # 80,5% of all private debt, which Tbl III.2's sector rows give.
    by_borrower = _make_table(title="Tabel III.6 ULN Swasta menurut Kelompok Peminjam", unit="Juta USD", data={
        ("Perusahaan Induk > Industri Pengolahan", 2026, "Aug"): 23694.5,
        ("Perusahaan Induk > Jasa Keuangan", 2026, "Aug"): 498.8,
        ("Anak Perusahaan > Industri Pengolahan", 2026, "Aug"): 30787.3,
        ("TOTAL", 2026, "Aug"): 38291.0})
    by_sector = _make_table(title="Tabel III.2 ULN Swasta menurut Sektor Ekonomi", unit="Juta USD", data={
        ("Industri Pengolahan", 2026, "Aug"): 54481.8, ("Jasa Keuangan", 2026, "Aug"): 38995.5,
        ("Konstruksi", 2026, "Aug"): 22000.0, ("TOTAL", 2026, "Aug"): 115477.3})
    fact = _make_fact(operation="ratio", claimed_value=80.9, unit="persen",
                      context_quote="ULN swasta terutama berasal dari Sektor Industri Pengolahan dan Jasa Keuangan "
                                    "dengan pangsa mencapai 80,9% dari total ULN swasta.",
                      periods=[_make_period(metric_label=m, month="Aug")
                               for m in ("Industri Pengolahan", "Jasa Keuangan", "TOTAL")])

    result = _evaluate_fact_safely(fact, [_make_source(by_borrower, sheet="Tbl III.6"),
                                          _make_source(by_sector, sheet="Tbl III.2")])

    assert result.verdict == "Entailed"
    assert "Tbl III.2" in result.reasoning


def _spe_cities():
    index = _make_table(title="Tabel 5. Indeks Penjualan Riil Per Kota", unit="Indeks", data={
        ("Medan", 2026, "Aug"): 381.1, ("Medan", 2026, "Sep"): 383.5,
        ("Denpasar", 2026, "Aug"): 128.5, ("Denpasar", 2026, "Sep"): 128.9})
    yoy = _make_table(title="Tabel 6. Pertumbuhan Tahunan Per Kota (%, yoy)", unit="%, yoy", data={
        ("Medan", 2026, "Aug"): 21.6, ("Medan", 2026, "Sep"): 26.0,
        ("Denpasar", 2026, "Aug"): 7.9, ("Denpasar", 2026, "Sep"): 8.2})
    mtm = _make_table(title="Tabel 7. Pertumbuhan Bulanan Per Kota (%, mtm)", unit="%, mtm", data={
        ("Medan", 2026, "Aug"): 1.3, ("Medan", 2026, "Sep"): 0.6,
        ("Denpasar", 2026, "Aug"): 0.0, ("Denpasar", 2026, "Sep"): 0.3})
    return [_make_source(index, sheet="Tabel 5"), _make_source(yoy, sheet="Tabel 6"), _make_source(mtm, sheet="Tabel 7")]


_SLOWER = ("Sementara itu, penjualan di Medan dan Denpasar diprakirakan tetap tumbuh meski melambat, "
           "masing-masing sebesar 0,6% (mtm) dan 0,3% (mtm).")


@pytest.mark.parametrize("city, verdict", [("Medan", "Entailed"), ("Denpasar", "Refuted")])
def test_a_rate_said_to_slow_is_read_on_the_growth_table_of_its_tag(city, verdict):
    # SPE Agustus 2026: checked on the index, Medan "melambat" (381,1 -> 383,5) was refuted; it is
    # the monthly growth 1,3 -> 0,6. Denpasar's monthly growth rose (0,0 -> 0,3) — the report's
    # "melambat" does not hold for it.
    fact = _make_fact(operation="is_decreasing", claimed_value=None, unit=None, context_quote=_SLOWER,
                      periods=[_make_period(metric_label=f"penjualan di {city}", month="Sep")])

    result = _evaluate_fact_safely(fact, _spe_cities())

    assert result.verdict == verdict
    assert "Tabel 7" in result.reasoning


def test_a_rise_secara_tahunan_compares_with_the_same_month_a_year_before():
    table = _make_table(title="Tabel 1. Indeks Penjualan Riil Menurut Kelompok", unit="Indeks", data={
        ("Suku Cadang dan Aksesori", 2025, "Aug"): 136.2, ("Suku Cadang dan Aksesori", 2026, "Jul"): 148.9,
        ("Suku Cadang dan Aksesori", 2026, "Aug"): 141.5, ("Barang Lainnya", 2026, "Aug"): 85.2})
    fact = _make_fact(operation="is_increasing", claimed_value=None, unit=None,
                      context_quote="Kenaikan tersebut didorong oleh peningkatan penjualan secara tahunan pada "
                                    "Kelompok Suku Cadang dan Aksesori.",
                      periods=[_make_period(metric_label="Suku Cadang dan Aksesori", month="Aug")])

    result = _evaluate_fact_safely(fact, [_make_source(table)])

    assert result.verdict == "Entailed"
    assert "Aug 2025=136.2" in result.reasoning


def test_a_group_named_lainnya_is_not_the_hedge_other_groups():
    table = _make_table(title="Tabel 1. Indeks Penjualan Riil Menurut Kelompok", unit="Indeks", data={
        ("Barang Lainnya", 2026, "Aug"): 85.2, ("Barang Lainnya", 2026, "Sep"): 85.5,
        ("Barang Budaya dan Rekreasi", 2026, "Sep"): 60.2})
    fact = _make_fact(operation="is_increasing", claimed_value=None, unit=None,
                      context_quote="Peningkatan penjualan tersebut didorong oleh kenaikan penjualan pada Kelompok "
                                    "Barang Lainnya, serta Kelompok Barang Budaya dan Rekreasi.",
                      periods=[_make_period(metric_label="Barang Lainnya", month="Sep")])

    assert _evaluate_fact_safely(fact, [_make_source(table)]).verdict == "Entailed"


def _ieh():
    table = _make_table(title="Tabel 9", unit="Indeks", data={
        ("Ekspektasi Harga Umum", 2026, "Oct"): 146.5, ("Ekspektasi Harga Umum", 2026, "Nov"): 149.9,
        ("Ekspektasi Harga Umum", 2027, "Jan"): 166.0, ("Ekspektasi Harga Umum", 2027, "Feb"): 177.9})
    return [_make_source(table, sheet="Tabel 9")]


@pytest.mark.parametrize("quote", [
    "IEH November 2026 dan Februari 2027 yang masing-masing tercatat sebesar 149,9 dan 177,9, lebih tinggi "
    "dibandingkan dengan indeks pada periode sebelumnya yang masing-masing tercatat sebesar 146,5 dan 166,0.",
    "dan 166,0",
])
def test_a_number_the_extractor_put_on_the_wrong_period_is_not_refuted(quote):
    # 166,0 is January 2027's; the extractor filed it under November 2026 (149,9).
    fact = _make_fact(operation="value", claimed_value=166.0, unit=None, context_quote=quote,
                      periods=[_make_period(metric_label="Ekspektasi Harga Umum", year=2026, month="Nov")])

    result = _evaluate_fact_safely(fact, _ieh())

    assert result.verdict == "Inconclusive"
    assert "Jan 2027" in result.reasoning


def test_a_number_dated_beside_it_by_the_author_stays_refuted():
    # NPI: "0,7 miliar dolar AS pada triwulan II 2026" (table 0,77); 0,7 also being Q4 2025's
    # figure is a coincidence.
    table = _make_table(title="NPI", unit="miliar USD", data={
        ("Pendapatan investasi langsung > Penerimaan", 2025, "Q4"): 0.70,
        ("Pendapatan investasi langsung > Penerimaan", 2026, "Q1"): 0.62,
        ("Pendapatan investasi langsung > Penerimaan", 2026, "Q2"): 0.77})
    fact = _make_fact(operation="value", claimed_value=0.7, unit="miliar USD",
                      context_quote="penerimaan imbal hasil … meningkat menjadi 0,7 miliar dolar AS pada triwulan "
                                    "II 2026, dari 0,6 miliar dolar AS pada periode sebelumnya.",
                      periods=[_make_period(metric_label="Pendapatan investasi langsung > Penerimaan", month="Q2")])

    assert _evaluate_fact_safely(fact, [_make_source(table)]).verdict == "Refuted"


def test_two_excel_sheets_sharing_a_row_name_are_not_a_source_conflict():
    # SKDU: 'REAL ESTAT' is business activity in Tabel 1 (0,25 -> 0,34) and employment in Tabel 4
    # (falling); the claim was flagged "PDF vs Excel berbeda" though its verdict was right.
    activity = _make_table(title="Tabel 1. Kegiatan Usaha", unit="% Saldo Bersih Tertimbang - SBT",
                           data={("REAL ESTAT", 2026, "Q2"): 0.25, ("REAL ESTAT", 2026, "Q3"): 0.34})
    labour = _make_table(title="Tabel 4. Penggunaan Tenaga Kerja", unit="% Saldo Bersih Tertimbang - SBT",
                         data={("REAL ESTAT", 2026, "Q2"): 0.12, ("REAL ESTAT", 2026, "Q3"): 0.05})
    fact = _make_fact(operation="is_increasing", claimed_value=None, unit=None,
                      context_quote="Kinerja LU Real Estat tercatat meningkat.",
                      periods=[_make_period(metric_label="REAL ESTAT", month="Q2"),
                               _make_period(metric_label="REAL ESTAT", month="Q3")])

    result = _evaluate_fact_safely(fact, [_make_source(activity, sheet="T1"), _make_source(labour, sheet="T4")])

    assert result.verdict == "Entailed"
    assert result.source_conflict is None
    assert len(result.source_values or []) <= 1


# SULNI Agustus 2026 is a quarterly release: "Posisi ULN Indonesia pada triwulan II 2026 tercatat
# sebesar 453,4 miliar dolar AS" against monthly tables. A position at a quarter is the position at
# its last month; with no Q2 column anywhere every claim came back Tidak Cukup Data.
def _monthly_uln(rows):
    return _make_table(title="Posisi Utang Luar Negeri Indonesia", unit="Juta USD", data=rows)


def test_a_quarter_position_is_read_from_the_quarters_last_month():
    table = _monthly_uln({("Total", 2026, "Apr"): 449100.0, ("Total", 2026, "May"): 451000.0,
                          ("Total", 2026, "Jun"): 453412.0})
    fact = _make_fact(
        periods=[_make_period(metric_label="Total", month="Q2")], claimed_value=453.4,
        unit="miliar dolar AS",
        context_quote="Posisi ULN Indonesia pada triwulan II 2026 tercatat sebesar 453,4 miliar dolar AS.",
    )

    result = _evaluate_fact(fact, [_make_source(table)])

    assert result.verdict == "Entailed"
    assert [(p.year, p.month) for p in result.periods] == [(2026, "Jun")]


def test_a_quarter_growth_compares_the_last_months_a_year_apart():
    table = _monthly_uln({("Pemerintah", 2025, "Jun"): 210200.0, ("Pemerintah", 2026, "Jun"): 216300.0})
    fact = _make_fact(
        operation="yoy_growth", periods=[_make_period(metric_label="Pemerintah", month="Q2")],
        claimed_value=2.9, unit="persen_yoy",
        context_quote="Posisi ULN pemerintah pada triwulan II 2026 tercatat sebesar 216,3 miliar dolar AS, atau tumbuh 2,9% (yoy).",
    )

    assert _evaluate_fact(fact, [_make_source(table)]).verdict == "Entailed"


def test_a_quarter_trend_runs_from_one_quarter_end_to_the_next():
    table = _monthly_uln({("Swasta", 2026, "Mar"): 196000.0, ("Swasta", 2026, "Jun"): 194600.0})
    fact = _make_fact(
        operation="is_decreasing", claimed_value=None, unit=None,
        periods=[_make_period(metric_label="Swasta", month="Q1"), _make_period(metric_label="Swasta", month="Q2")],
        context_quote="Posisi ULN swasta pada triwulan II 2026 menurun dibandingkan triwulan sebelumnya.",
    )

    assert _evaluate_fact(fact, [_make_source(table)]).verdict == "Entailed"


def test_a_table_with_its_own_quarter_column_answers_the_quarter_itself():
    # TabI.7 keeps the debt-to-GDP ratio per quarter; a monthly sheet with a same-named row must
    # not answer in its place.
    quarterly = _make_table(title="Rasio ULN", unit="persen", data={("Rasio Utang terhadap PDB", 2026, "Q2"): 30.63})
    monthly = _make_table(title="Rasio ULN bulanan", unit="persen", data={("Rasio Utang terhadap PDB", 2026, "Jun"): 99.0})
    fact = _make_fact(
        periods=[_make_period(metric_label="Rasio Utang terhadap PDB", month="Q2")], claimed_value=30.6,
        unit="persen", context_quote="Rasio ULN terhadap PDB pada triwulan II 2026 sebesar 30,6%.",
    )

    result = _evaluate_fact(fact, [_make_source(monthly, sheet="M"), _make_source(quarterly, sheet="Q")])

    assert result.verdict == "Entailed"
    assert result.periods[0].month == "Q2"


def test_a_flow_over_the_quarter_is_not_read_from_its_last_month():
    # "selama triwulan II" sums April-June; June alone is not the answer.
    table = _make_table(title="Penjualan", unit="miliar Rp",
                        data={("Penjualan", 2026, "Jun"): 120.0, ("Penjualan", 2026, "May"): 110.0})
    fact = _make_fact(
        periods=[_make_period(metric_label="Penjualan", month="Q2")], claimed_value=120.0, unit="miliar Rp",
        context_quote="Penjualan selama triwulan II 2026 tercatat sebesar Rp120,0 miliar.",
    )

    assert _evaluate_fact(fact, [_make_source(table)]).verdict == "Inconclusive"


def test_a_one_quarter_trend_compares_with_the_previous_quarters_last_month():
    # "peningkatan ULN publik" in a triwulan II release: against March, not May.
    table = _monthly_uln({("Publik", 2026, "Mar"): 260000.0, ("Publik", 2026, "May"): 240000.0,
                          ("Publik", 2026, "Jun"): 258752.0})
    fact = _make_fact(
        operation="is_increasing", claimed_value=None, unit=None,
        periods=[_make_period(metric_label="Publik", month="Q2")],
        context_quote="Perkembangan tersebut disebabkan oleh peningkatan ULN publik.",
    )

    result = _evaluate_fact(fact, [_make_source(table)])

    assert [(p.year, p.month) for p in result.periods] == [(2026, "Mar"), (2026, "Jun")]
    assert result.verdict == "Refuted"
