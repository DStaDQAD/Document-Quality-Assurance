"""B5: say WHY a monthly claim cannot be checked when the table holds the series only quarterly.

"Rasio utang terhadap PDB 30,7% pada Juli" against a table whose ratio row is quarterly: the
verdict stays Tidak Cukup Data, but "data tidak ditemukan" sent the reader looking for a row that
is right there. Synthetic table.
"""

from excel_parser_bi import BITableData
from paired_verifier import _ExcelSource, _evaluate_fact
from structured_extractor import ExtractedFact, PeriodPoint


def test_a_monthly_claim_on_a_quarterly_row_says_the_table_is_quarterly():
    table = BITableData(title="Indikator utang", unit="%", row_labels=["Rasio Utang terhadap PDB"])
    table._data[("Rasio Utang terhadap PDB", 2026, "Q1")] = 30.1
    table._data[("Rasio Utang terhadap PDB", 2026, "Q2")] = 30.6
    fact = ExtractedFact("value", [PeriodPoint("Rasio utang terhadap PDB", 2026, "Jul")], 30.7, "persen",
                         "rasio utang terhadap PDB tercatat sebesar 30,7% pada Juli 2026")
    result = _evaluate_fact(fact, [_ExcelSource(table=table, filename="t.xlsx", sheet="T")])
    assert result.verdict == "Inconclusive"
    assert "triwulan" in result.reasoning and "Q2 2026" in result.reasoning
