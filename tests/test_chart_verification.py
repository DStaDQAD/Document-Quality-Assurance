"""Chart labels checked against the tables, and charts as a fallback source for the prose.

Two directions, both measured on SK-Juni-2026:
  - chart vs table: every label printed on a chart is a checkable fact (check_chart_labels);
  - narrative vs chart: a chart answers a claim only when no table can, and otherwise can only
    raise a "chart" conflict.
The series a chart names ('IKK', 'Rp1 - 2 juta') are placed on the tables' row names by
_chart_row_names before anything is compared.
"""

import asyncio
from unittest.mock import Mock, patch

import pytest

from excel_parser_bi import BITableData
from paired_verifier import (
    _chart_row_fit,
    _chart_row_names,
    _chart_source,
    _settle_by_cited_chart,
    _evaluate_fact,
    _ExcelSource,
    check_chart_labels,
    summarize_chart,
    verify_paired,
)
from pdf_chart_extraction import ChartPoint, ChartReading
from structured_extractor import ExtractedFact, PeriodPoint

_IKK_RP1 = "Indeks Keyakinan Konsumen (IKK) > Pengeluaran Rp1 - 2 juta"
_IKK_RP5 = "Indeks Keyakinan Konsumen (IKK) > Pengeluaran >Rp5 juta"
_IKE_RP1 = "Indeks Kondisi Ekonomi (IKE) > Pengeluaran Rp1 - 2 juta"


def _table(data, title="Tabel 2 Indeks Keyakinan Konsumen per Kelompok Pengeluaran"):
    table = BITableData(title=title, unit="", row_labels=[])
    for (label, year, month), value in data.items():
        if label not in table.row_labels:
            table.row_labels.append(label)
        table._data[(label, year, month)] = value
    return table


def _appendix():
    """Tabel 2 of the appendix, as the PDF source a chart is checked against."""
    return _ExcelSource(
        table=_table({
            (_IKK_RP1, 2026, "Apr"): 114.4, (_IKK_RP1, 2026, "May"): 113.0,
            (_IKK_RP1, 2026, "Jun"): 108.9,
            (_IKK_RP5, 2026, "Apr"): 128.2, (_IKK_RP5, 2026, "May"): 124.6,
            (_IKK_RP5, 2026, "Jun"): 121.4,
            (_IKE_RP1, 2026, "May"): 104.8, (_IKE_RP1, 2026, "Jun"): 101.3,
        }),
        filename="SK-Juni-2026.pdf", sheet="Hal. 9 · Tabel 2", origin="pdf",
    )


def _grafik2(*points):
    return ChartReading(page_number=2, caption="Grafik 2", title="IKK per Kelompok Pengeluaran",
                        indicator="IKK", unit="Indeks", points=list(points))


def _pt(series, month, value, kind="level", year=2026):
    return ChartPoint(series=series, year=year, month=month, value=value,
                      raw=str(value).replace(".", ","), kind=kind)


# ---------------------------------------------------------------------------
# Placing a chart's series on the tables' rows
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("parts,row", [
    (["IKK", "Rp1 - 2 juta"], _IKK_RP1),
    (["IKK", "> Rp5 juta"], _IKK_RP5),
    (["Indeks Keyakinan Konsumen (IKK)", "MEDAN"], "5. Medan > Indeks Keyakinan Konsumen (IKK)"),
    (["Rasio Konsumsi", "Rp1 - 2 juta"], "Rp 1 - 2 juta > Konsumsi"),
    (["Konsumsi"], "Total > Konsumsi"),
    (["IKK", "51 - 60 tahun"], "Indeks Keyakinan Konsumen (IKK) > Usia 51-60 th"),
])
def test_a_chart_series_fits_the_row_the_tables_use_for_it(parts, row):
    assert _chart_row_fit(parts, row) is not None


@pytest.mark.parametrize("parts,row", [
    (["IKK", "Rp1 - 2 juta"], "Indeks Keyakinan Konsumen (IKK) > Pengeluaran Rp2,1 - 3 juta"),
    (["IKK", "> 60 tahun"], "Indeks Keyakinan Konsumen (IKK) > Usia 51-60 th"),
    (["IKK", "51 - 60 tahun"], "Indeks Keyakinan Konsumen (IKK) > Usia >60 th"),
    (["IKK", "Rp1 - 2 juta"], _IKE_RP1),
    (["Konsumsi"], "Rp 1 - 2 juta > Konsumsi"),       # a group level the chart never named
])
def test_a_chart_series_does_not_fit_a_neighbouring_row(parts, row):
    assert _chart_row_fit(parts, row) is None


def test_the_row_that_fits_with_fewest_words_to_spare_wins():
    # "Indeks Ketersediaan Lapangan Kerja" is inside both IKLK and IEKLK; the plainer one is it.
    source = _ExcelSource(table=_table({
        ("Indeks Ketersediaan Lapangan Kerja (IKLK) > SMA", 2026, "Jun"): 98.3,
        ("Indeks Ekspektasi Ketersediaan Lapangan Kerja (IEKLK) > SMA", 2026, "Jun"): 121.1,
    }), filename="x", sheet="Tabel 4")
    reading = ChartReading(page_number=3, caption="Grafik 7", title="…", unit="Indeks",
                           indicator="Indeks Ketersediaan Lapangan Kerja",
                           points=[_pt("SMA", "Jun", 98.3)])
    names = _chart_row_names([reading], [source])
    assert names[("Indeks Ketersediaan Lapangan Kerja", "SMA")] == (
        "Indeks Ketersediaan Lapangan Kerja (IKLK) > SMA")


# ---------------------------------------------------------------------------
# Chart vs table
# ---------------------------------------------------------------------------

def _check(*points):
    return [result for _, result in check_chart_labels([_grafik2(*points)], [_appendix()])]


def test_a_label_that_matches_the_table_is_sesuai():
    [result] = _check(_pt("Rp1 - 2 juta", "Jun", 108.9))
    assert result.verdict == "Entailed"
    assert result.checked_item == "chart"
    assert result.periods[0].metric_label == _IKK_RP1
    assert result.context_quote.startswith("Grafik 2 · IKK per Kelompok Pengeluaran")


def test_a_wrong_number_on_a_chart_is_tidak_sesuai():
    [result] = _check(_pt("Rp1 - 2 juta", "Jun", 118.9))
    assert result.verdict == "Refuted"
    assert result.computed_value == 108.9


def test_a_right_number_read_one_bar_off_is_matched_and_says_so():
    # The measured failure: the model put June's 108,9 on the May bar.
    [result] = _check(_pt("Rp1 - 2 juta", "May", 108.9))
    assert result.verdict == "Entailed"
    assert "Jun 2026" in result.reasoning and "Mei 2026" in result.reasoning
    assert result.periods[0].month == "Jun"


def test_a_number_from_another_group_is_not_excused_as_a_misplaced_label():
    # 121,4 is the >Rp5 juta group's June value, not Rp1 - 2 juta's in any month.
    [result] = _check(_pt("Rp1 - 2 juta", "Jun", 121.4))
    assert result.verdict == "Refuted"


def test_a_series_no_table_carries_is_tidak_cukup_data():
    [result] = _check(_pt("Rp9 - 10 juta", "Jun", 100.0))
    assert result.verdict == "Inconclusive"
    assert "tidak dapat dipastikan" in result.reasoning


def test_a_change_label_is_checked_as_the_difference_from_the_month_before():
    reading = ChartReading(page_number=14, caption="Gambar 1", title="IKK Regional", unit="Indeks",
                           indicator="Indeks Keyakinan Konsumen (IKK)",
                           points=[_pt("MEDAN", "Jun", -8.9, kind="change")])
    medan = "5. Medan > Indeks Keyakinan Konsumen (IKK)"
    source = _ExcelSource(table=_table({(medan, 2026, "May"): 99.4, (medan, 2026, "Jun"): 90.5}),
                          filename="x", sheet="Tabel 6", origin="pdf")
    [(fact, result)] = check_chart_labels([reading], [source])
    assert fact.operation == "diff"
    assert result.verdict == "Entailed"


# ---------------------------------------------------------------------------
# Narrative vs chart
# ---------------------------------------------------------------------------

def _claim(label, month, value):
    return ExtractedFact(operation="value", periods=[PeriodPoint(label, 2026, month)],
                         claimed_value=value, unit=None, context_quote="…", page_number=2)


def _chart_src(*points, sources=None):
    reading = _grafik2(*points)
    sources = sources or [_appendix()]
    names = _chart_row_names([reading], sources)
    return _chart_source(reading, "SK-Juni-2026.pdf", names, sources)


def test_a_chart_answers_a_claim_no_table_can():
    # The appendix here has no >Rp5 juta row for the IKE; the chart source is the only answer.
    appendix = _appendix()
    chart = _chart_src(_pt("Rp1 - 2 juta", "Jun", 108.9), sources=[appendix])
    appendix.table._data.pop((_IKK_RP1, 2026, "Jun"))

    result = _evaluate_fact(_claim(_IKK_RP1, "Jun", 108.9), [appendix, chart])
    assert result.verdict == "Entailed"
    assert "Grafik 2" in result.matched_excel_source


def test_the_table_decides_and_a_disagreeing_chart_is_reported_as_a_chart_conflict():
    appendix = _appendix()
    chart = _chart_src(_pt("Rp1 - 2 juta", "Jun", 118.9), sources=[appendix])

    result = _evaluate_fact(_claim(_IKK_RP1, "Jun", 108.9), [appendix, chart])
    assert result.verdict == "Entailed"
    assert "Tabel 2" in result.matched_excel_source
    assert result.source_conflict == "chart"


# ---------------------------------------------------------------------------
# verify_paired
# ---------------------------------------------------------------------------

def test_verify_paired_appends_the_chart_checks_and_keeps_them_out_of_the_extractor_prompt():
    claim = _claim(_IKK_RP5, "Jun", 121.4)
    extractor = Mock(side_effect=lambda *a, **k: asyncio.sleep(0, result=[claim]))
    appendix = _appendix()
    pdf_table = Mock()

    with patch("paired_verifier.extract_structured_facts_async", new=extractor), \
         patch("paired_verifier._pdf_table_source", return_value=(appendix, "pdf-generic")):
        response = asyncio.run(verify_paired(
            narrative_text="[== Halaman 2 ==]\nIKK >Rp5 juta 121,4.", excel_sources=[],
            llm=Mock(), pdf_tables=[pdf_table], mode="internal",
            chart_readings=[_grafik2(_pt("Rp1 - 2 juta", "Jun", 108.9),
                                     _pt("> Rp5 juta", "Jun", 121.4))],
            chart_pages_unread=[5],
        ))

    # The claims and the charts are reported apart: the counts are the narrative's alone.
    assert [r.checked_item for r in response.results] == ["narrative"]
    assert response.total_facts == 1 and response.entailed_count == 1
    [chart] = response.chart_checks
    assert (chart.caption, chart.verdict, chart.label_count, chart.issues) == ("Grafik 2", "Entailed", 2, [])
    assert response.chart_label_count == 2
    assert response.chart_pages_unread == [5]
    assert "chart" in response.excel_parsers
    # The extractor was offered the tables' row names only — never the chart's.
    offered = extractor.call_args.args[1]
    assert _IKK_RP1 in offered and not any("IKK >" == l[:5] for l in offered)


def test_a_chart_index_is_not_placed_on_a_different_index_that_spells_it_out():
    # The appendix lost IKLK's row; IEKLK contains every IKLK word plus 'Ekspektasi'.
    assert _chart_row_fit(
        ["Indeks Ketersediaan Lapangan Kerja", "41 - 50 tahun"],
        "Indeks Ekspektasi Ketersediaan Lapangan Kerja (IEKLK) > Usia 41-50 th",
    ) is None
    assert _chart_row_fit(
        ["Indeks Pembelian Barang Tahan Lama/Durable Goods", "> 60 tahun"],
        "Indeks Pembelian Barang Tahan Lama (Durable Goods) (IPDG) > Usia >60 th",
    ) is not None


def test_a_label_filed_under_the_wrong_line_of_the_chart_is_left_for_a_person():
    # SK-Juni-2026 Grafik 4: the model swapped the red IKLK and green IPDG lines.
    iklk = "Indeks Ketersediaan Lapangan Kerja (IKLK)"
    ipdg = "Indeks Pembelian Barang Tahan Lama (Durable Goods) (IPDG)"
    tabel1 = _ExcelSource(table=_table({
        (iklk, 2026, "May"): 105.0, (iklk, 2026, "Jun"): 101.8,
        (ipdg, 2026, "May"): 108.3, (ipdg, 2026, "Jun"): 105.9,
    }, title="Tabel 1 Indeks Keyakinan Konsumen"), filename="SK", sheet="Hal. 8 · Tabel 1", origin="pdf")
    reading = ChartReading(page_number=2, caption="Grafik 4", title="Perkembangan IKE", indicator="",
                           unit="Indeks", points=[
                               _pt("Indeks Ketersediaan Lapangan Kerja (IKLK)", "Jun", 105.9),
                               _pt("Indeks Pembelian Barang Tahan Lama (IPDG)", "Jun", 101.8),
                               _pt("Indeks Pembelian Barang Tahan Lama (IPDG)", "May", 999.0),
                           ])
    results = [r for _, r in check_chart_labels([reading], [tabel1])]

    assert [r.verdict for r in results] == ["Inconclusive", "Inconclusive", "Refuted"]
    assert "Durable Goods" in results[0].reasoning and "tertukar" in results[0].reasoning



def _summary(*values):
    """Grafik 2 with one label per value; 108.9 matches the table, 118.9 does not, 100.0 is a
    series no table carries."""
    points = [_pt("Rp9 - 10 juta" if v == 100.0 else "Rp1 - 2 juta", "Jun", v) for v in values]
    reading = _grafik2(*points)
    return summarize_chart(reading, check_chart_labels([reading], [_appendix()]))


def test_a_chart_takes_the_verdict_most_of_its_labels_got():
    chart = _summary(108.9, 108.9, 118.9)
    assert chart.verdict == "Entailed"
    assert (chart.entailed_count, chart.refuted_count) == (2, 1)
    # Only the label that failed is carried, for the reader to see.
    assert [r.claimed_value for r in chart.issues] == [118.9]


def test_a_tied_chart_takes_the_worse_verdict():
    assert _summary(108.9, 118.9).verdict == "Refuted"
    assert _summary(108.9, 100.0).verdict == "Inconclusive"


# ---------------------------------------------------------------------------
# SK-Agustus-2026 regressions (not chart-specific, found on the same report)
# ---------------------------------------------------------------------------

def test_a_rise_named_newest_period_first_is_still_a_rise():
    ikk = "Indeks Keyakinan Konsumen (IKK)"
    source = _ExcelSource(table=_table({(ikk, 2026, "Jul"): 116.8, (ikk, 2026, "Aug"): 118.5}),
                          filename="SK", sheet="Hal. 8 · Tabel 1", origin="pdf")
    fact = ExtractedFact(operation="is_increasing",
                         periods=[PeriodPoint(ikk, 2026, "Aug"), PeriodPoint(ikk, 2026, "Jul")],
                         claimed_value=None, unit=None, context_quote="meningkat dibandingkan Juli")
    assert _evaluate_fact(fact, [source]).verdict == "Entailed"


def test_a_whole_index_is_not_answered_by_one_group_of_its_breakdown():
    # The national IEKLK row was lost from Tabel 1; Tabel 4 only has it per education level.
    ieklk = "Indeks Ekspektasi Ketersediaan Lapangan Kerja (IEKLK)"
    tabel4 = _ExcelSource(table=_table({
        (f"{ieklk} > SMA", 2026, "Aug"): 125.0, (f"{ieklk} > Sarjana", 2026, "Aug"): 128.8,
    }, title="Tabel 4 Indeks Keyakinan Konsumen per Tingkat Pendidikan"),
        filename="SK", sheet="Hal. 11 · Tabel 4", origin="pdf")

    result = _evaluate_fact(_claim(ieklk, "Aug", 125.7), [tabel4])
    assert result.verdict == "Inconclusive"
    assert "per kelompok" in result.reasoning
    # A claim that names the group is still answered by it.
    assert _evaluate_fact(_claim(f"{ieklk} > SMA", "Aug", 125.0), [tabel4]).verdict == "Entailed"


def test_half_a_point_on_a_ten_percent_ratio_is_relatively_stable():
    # SK-Agustus-2026: "cicilan/utang ... sebesar 10,0%, relatif stabil dibandingkan ... 10,5%".
    row = "Total > Cicilan pinjaman"
    source = _ExcelSource(table=_table({(row, 2026, "Jul"): 10.5, (row, 2026, "Aug"): 10.0}),
                          filename="SK", sheet="Hal. 12 · Tabel 5", origin="pdf")
    stable = ExtractedFact(operation="is_stable",
                           periods=[PeriodPoint(row, 2026, "Aug"), PeriodPoint(row, 2026, "Jul")],
                           claimed_value=None, unit=None, context_quote="relatif stabil")
    assert _evaluate_fact(stable, [source]).verdict == "Entailed"
    source.table._data[(row, 2026, "Aug")] = 9.8          # a 7% move is not stable
    assert _evaluate_fact(stable, [source]).verdict == "Refuted"


def _ipsi_case(claimed, quote):
    """SK-Agustus-2026: the IPSI sentence citing Grafik 5, extracted as the IKE row."""
    ipsi = "Indeks Penghasilan Saat Ini (IPSI) > Pengeluaran >Rp5 juta"
    ike = "Indeks Kondisi Ekonomi (IKE) > Pengeluaran >Rp5 juta"
    tabel2 = _ExcelSource(table=_table({(ipsi, 2026, "Aug"): 125.9, (ike, 2026, "Aug"): 116.4}),
                          filename="SK", sheet="Hal. 9 · Tabel 2", origin="pdf")
    grafik5 = ChartReading(page_number=3, caption="Grafik 5",
                           title="Indeks Penghasilan Saat Ini per Kelompok Pengeluaran",
                           indicator="Indeks Penghasilan Saat Ini", unit="Indeks",
                           points=[_pt("> Rp5 juta", "Aug", 125.9)])
    fact = ExtractedFact(operation="value", periods=[PeriodPoint(ike, 2026, "Aug")],
                         claimed_value=claimed, unit=None, context_quote=quote)
    names = _chart_row_names([grafik5], [tabel2])
    result = _evaluate_fact(fact, [tabel2])
    return _settle_by_cited_chart(fact, result, [grafik5], names, [tabel2])


def test_a_figure_filed_under_the_wrong_index_is_judged_by_the_chart_its_sentence_cites():
    result = _ipsi_case(125.9, "Indeks tertinggi … >Rp5 juta, yaitu sebesar 125,9 (Grafik 5).")
    assert result.verdict == "Entailed"
    assert "Grafik 5" in result.reasoning and "IPSI" in result.reasoning


def test_the_cited_chart_does_not_rescue_a_figure_it_does_not_print_or_an_uncited_one():
    assert _ipsi_case(124.9, "… sebesar 124,9 (Grafik 5).").verdict == "Refuted"
    assert _ipsi_case(125.9, "… sebesar 125,9.").verdict == "Refuted"
    assert _ipsi_case(125.9, "… sebesar 125,9 (Grafik 6).").verdict == "Refuted"


def test_the_cited_chart_still_names_the_series_when_its_label_was_read_one_bar_off():
    ipsi = "Indeks Penghasilan Saat Ini (IPSI) > Pengeluaran >Rp5 juta"
    ike = "Indeks Kondisi Ekonomi (IKE) > Pengeluaran >Rp5 juta"
    tabel2 = _ExcelSource(table=_table({(ipsi, 2026, "Aug"): 125.9, (ike, 2026, "Aug"): 116.4}),
                          filename="SK", sheet="Hal. 9 · Tabel 2", origin="pdf")
    grafik5 = ChartReading(page_number=3, caption="Grafik 5", title="IPSI per Kelompok Pengeluaran",
                           indicator="Indeks Penghasilan Saat Ini", unit="Indeks",
                           points=[_pt("> Rp5 juta", "Jul", 125.9)])      # misplaced by the model
    fact = ExtractedFact(operation="value", periods=[PeriodPoint(ike, 2026, "Aug")],
                         claimed_value=125.9, unit=None, context_quote="sebesar 125,9 (Grafik 5).")
    names = _chart_row_names([grafik5], [tabel2])
    result = _settle_by_cited_chart(fact, _evaluate_fact(fact, [tabel2]), [grafik5], names, [tabel2])
    assert result.verdict == "Entailed"


def test_groups_named_by_a_range_in_the_sentence_are_checked_not_waved_off():
    # SK-Agustus-2026: "IPDG ... optimis untuk kelompok pengeluaran di atas Rp3,1 juta, sementara
    # kelompok lainnya berada pada level pesimis."
    row = "Indeks Pembelian Barang Tahan Lama (Durable Goods) (IPDG) > Pengeluaran Rp4,1 - 5 juta"
    source = _ExcelSource(table=_table({(row, 2026, "Aug"): 104.2}), filename="SK",
                          sheet="Hal. 9 · Tabel 2", origin="pdf")
    quote = ("IPDG berada pada level optimis untuk kelompok pengeluaran di atas Rp3,1 juta, "
             "sementara kelompok lainnya berada pada level pesimis.")
    fact = ExtractedFact(operation="above_threshold", periods=[PeriodPoint(row, 2026, "Aug")],
                         claimed_value=100.0, unit=None, context_quote=quote)
    assert _evaluate_fact(fact, [source]).verdict == "Entailed"

    hedged = ExtractedFact(operation="above_threshold", periods=[PeriodPoint(row, 2026, "Aug")],
                           claimed_value=100.0, unit=None,
                           context_quote="IPDG optimis pada sebagian besar kelompok pengeluaran.")
    assert _evaluate_fact(hedged, [source]).verdict == "Inconclusive"


def test_a_total_named_only_total_is_checked_against_a_table_about_the_sentence_subject():
    # M2 report, Agustus 2026: "Penyaluran kredit ... Rp9.019,3 triliun" extracted as 'Total'.
    dpk = _ExcelSource(table=_table({("Total", 2026, "Aug"): 9732.8},
                                    title="Tabel 4. Penghimpunan Dana Pihak Ketiga Berdasarkan Golongan Nasabah"),
                       filename="M2", sheet="Hal. 3 · Tabel 4", origin="pdf")
    kredit = _ExcelSource(table=_table({("Total", 2026, "Aug"): 9019.3},
                                       title="Tabel 5. Perkembangan Kredit Berdasarkan Golongan Debitur"),
                          filename="M2", sheet="Hal. 4 · Tabel 5", origin="pdf")
    fact = ExtractedFact(operation="value", periods=[PeriodPoint("Total", 2026, "Aug")],
                         claimed_value=9019.3, unit=None,
                         context_quote="Penyaluran kredit pada Agustus 2026 tercatat sebesar Rp9.019,3 triliun")
    result = _evaluate_fact(fact, [dpk, kredit])
    assert result.verdict == "Entailed"
    assert "Tabel 5" in result.matched_excel_source
    assert result.source_conflict is None


def test_a_chart_summary_carries_the_chart_picture():
    reading = _grafik2(_pt("Rp1 - 2 juta", "Jun", 108.9))
    reading.thumbnail = "data:image/jpeg;base64,AAAA"
    chart = summarize_chart(reading, check_chart_labels([reading], [_appendix()]))
    assert chart.thumbnail == "data:image/jpeg;base64,AAAA"
