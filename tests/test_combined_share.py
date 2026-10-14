"""B3: a share held by SEVERAL rows together — "pangsa sektor A, B dan C mencapai 80,6% dari total".

Either the extractor lists one numerator point per row (ratio with 3+ points: all but the last
are summed), or it writes one numerator label naming the rows; then the rows are read off the
table. A sector the label names but the table lacks makes the claim uncheckable — summing the
rest would understate it and refute a true sentence. Synthetic sector table.
"""

from excel_parser_bi import BITableData
from paired_verifier import _ExcelSource, _evaluate_fact
from structured_extractor import ExtractedFact, PeriodPoint

ROWS = {"Industri Kayu": 30.0, "Jasa Angkutan dan Gudang": 20.0, "Pertanian & Kebun": 10.0,
        "Jasa Lainnya": 40.0, "TOTAL": 100.0}


def _source():
    table = BITableData(title="Posisi Utang Perusahaan Menurut Sektor", unit="Juta USD", row_labels=[])
    for label, value in ROWS.items():
        table.row_labels.append(label)
        table._data[(label, 2026, "Jul")] = value
    return _ExcelSource(table=table, filename="t.xlsx", sheet="T")


def _ratio(labels, claimed):
    return ExtractedFact("ratio", [PeriodPoint(l, 2026, "Jul") for l in labels], claimed, "persen",
                         "Utang terutama berasal dari sektor tersebut dengan pangsa mencapai x% dari total.")


def test_several_numerator_points_are_summed_over_the_last_point():
    result = _evaluate_fact(_ratio(["Industri Kayu", "Pertanian & Kebun", "TOTAL"], 40.0), [_source()])
    assert result.verdict == "Entailed" and result.computed_value == 40.0


def test_a_wrong_combined_share_is_refuted():
    assert _evaluate_fact(_ratio(["Industri Kayu", "Pertanian & Kebun", "TOTAL"], 55.0), [_source()]).verdict == "Refuted"


def test_one_numerator_label_naming_several_rows_is_split_on_the_table():
    fact = _ratio(["Sektor Industri Kayu, Jasa Angkutan dan Gudang, serta Pertanian dan Kebun", "TOTAL"], 60.0)
    result = _evaluate_fact(fact, [_source()])
    assert result.verdict == "Entailed" and result.computed_value == 60.0


def test_a_named_sector_the_table_lacks_leaves_the_claim_unchecked():
    fact = _ratio(["Sektor Industri Kayu, serta Perikanan Laut", "TOTAL"], 30.0)
    assert _evaluate_fact(fact, [_source()]).verdict == "Inconclusive"


def test_the_table_whose_title_fits_the_sentence_answers_a_combined_share():
    """The same sector rows sit in a table of ALL debt and in one of private debt; the sentence
    is about private debt ('utang swasta'), so the private table answers."""
    def table(title, scale):
        t = BITableData(title=title, unit="Juta USD", row_labels=[])
        for label, value in ROWS.items():
            t.row_labels.append(label)
            t._data[(label, 2026, "Jul")] = value * scale if label != "TOTAL" else 100.0 * scale * 2
        return t
    every = _ExcelSource(table=table("Posisi Utang Menurut Sektor", 3.0), filename="a.xlsx", sheet="A")
    private = _ExcelSource(table=_source().table, filename="b.xlsx", sheet="B")
    private.table.title = "Posisi Utang Swasta Menurut Sektor"
    fact = ExtractedFact("ratio", [PeriodPoint("Sektor Industri Kayu, serta Pertanian dan Kebun", 2026, "Jul"),
                                   PeriodPoint("total utang swasta", 2026, "Jul")], 40.0, "persen",
                         "Posisi utang swasta terutama berasal dari sektor tersebut dengan pangsa 40,0%.")
    result = _evaluate_fact(fact, [every, private])
    assert result.verdict == "Entailed" and "b.xlsx" in result.matched_excel_source
