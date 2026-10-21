"""Changes between two periods that the claim does not label as such, or prints without decimals.

PII Tw I-2026: "penurunan posisi aset derivatif finansial sebesar 23 juta dolar AS" on
817,1 -> 794,3 (a change printed to the unit, kept Tidak Sesuai), and "posisi cadangan devisa yang menurun terutama
bersumber dari posisi cadangan devisa lainnya sebesar 9,1 miliar dolar AS" — 9,1 is how far the
row fell, not its level. Synthetic tables with the same figures.
"""

from excel_parser_bi import BITableData
from paired_verifier import _ExcelSource, _evaluate_fact
from structured_extractor import ExtractedFact, PeriodPoint

LAINNYA = "Cadangan Devisa Lainnya"
DERIVATIF = "Aset > Derivatif Finansial"


def _source(rows, title="Posisi Investasi Internasional Indonesia", sheet="5.39"):
    table = BITableData(title=title, unit="Juta USD", row_labels=[])
    for label, (q4, q1) in rows.items():
        table.row_labels.append(label)
        table._data[(label, 2025, "Q4")] = q4
        table._data[(label, 2026, "Q1")] = q1
    return _ExcelSource(table=table, filename="t.xls", sheet=sheet)


PII = _source({LAINNYA: (135853.5, 126772.29), DERIVATIF: (817.1022, 794.3214)})


def _value(claimed, quote, label=LAINNYA):
    return ExtractedFact("value", [PeriodPoint(label, 2026, "Q1")], claimed, "miliar dolar AS", quote)


def test_a_change_printed_without_decimals_is_not_widened_to_the_unit():
    # The user's call: "23 juta" for a fall of 22,78 is Tidak Sesuai — the report prints its other
    # changes to one decimal ("0,3 miliar"), and 22,8 is what it should have said.
    quote = "serta penurunan posisi aset derivatif finansial sebesar 23 juta dolar AS."
    fact = ExtractedFact("diff", [PeriodPoint(DERIVATIF, 2025, "Q4"), PeriodPoint(DERIVATIF, 2026, "Q1")],
                         -23.0, "juta dolar AS", quote)
    assert _evaluate_fact(fact, [PII]).verdict == "Refuted"


def test_a_value_that_is_the_rows_fall_is_checked_as_a_change():
    quote = ("Berdasarkan instrumen, posisi cadangan devisa yang menurun terutama bersumber dari posisi "
             "cadangan devisa lainnya sebesar 9,1 miliar dolar AS.")
    result = _evaluate_fact(_value(9.1, quote), [PII])
    assert result.verdict == "Entailed" and result.operation == "diff"
    assert result.computed_value == -9.0812


def test_a_rise_word_does_not_confirm_a_fall():
    quote = "Posisi cadangan devisa lainnya meningkat sebesar 9,1 miliar dolar AS."
    assert _evaluate_fact(_value(9.1, quote), [PII]).verdict == "Refuted"


def test_without_a_change_word_the_level_stands():
    quote = "Posisi cadangan devisa lainnya tercatat sebesar 9,1 miliar dolar AS."
    assert _evaluate_fact(_value(9.1, quote), [PII]).verdict == "Refuted"


def test_the_change_is_held_to_the_printed_digits():
    quote = "Posisi cadangan devisa lainnya menurun sebesar 9,03 miliar dolar AS."
    assert _evaluate_fact(_value(9.03, quote), [PII]).verdict == "Refuted"


def test_a_zero_bound_the_sentence_never_states_is_not_judged():
    # The same sentence also came back as 'Cadangan Devisa Lainnya' below 0.
    quote = ("Berdasarkan instrumen, posisi cadangan devisa yang menurun terutama bersumber dari posisi "
             "cadangan devisa lainnya sebesar 9,1 miliar dolar AS.")
    fact = ExtractedFact("below_threshold", [PeriodPoint(LAINNYA, 2026, "Q1")], 0.0, None, quote)
    assert _evaluate_fact(fact, [PII]).verdict == "Inconclusive"


def test_a_zero_bound_named_by_a_sign_word_is_judged():
    quote = "Posisi cadangan devisa lainnya tercatat negatif."
    fact = ExtractedFact("below_threshold", [PeriodPoint(LAINNYA, 2026, "Q1")], 0.0, None, quote)
    assert _evaluate_fact(fact, [PII]).verdict == "Refuted"


def test_a_tied_table_that_prints_the_level_answers_before_a_change():
    """SKDU Tw III-2026: "sub-LU Industri Makanan dan Minuman (SBT 0,22%) … meningkat" is Tabel 4's
    level 0,22, though Tabel 2's capacity rose by 0,20 (76,73 -> 76,93)."""
    row = "Industri Makanan dan Minuman"
    capacity = BITableData(title="Kapasitas Produksi Terpakai", unit="%", row_labels=[row])
    capacity._data[(row, 2026, "Q2")] = 76.73
    capacity._data[(row, 2026, "Q3")] = 76.93
    workforce = BITableData(title="Penggunaan Tenaga Kerja", unit="%", row_labels=[row])
    workforce._data[(row, 2026, "Q2")] = 0.1
    workforce._data[(row, 2026, "Q3")] = 0.22
    sources = [_ExcelSource(table=capacity, filename="s.xlsx", sheet="T2"),
               _ExcelSource(table=workforce, filename="s.xlsx", sheet="T4")]
    quote = "beberapa sub-LU terindikasi meningkat, antara lain sub-LU Industri Makanan dan Minuman (SBT 0,22%)"
    fact = ExtractedFact("value", [PeriodPoint(row, 2026, "Q3")], 0.22, "persen", quote)
    result = _evaluate_fact(fact, sources)
    assert result.verdict == "Entailed" and result.operation == "value"
    assert result.computed_value == 0.22
