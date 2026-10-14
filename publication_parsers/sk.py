"""Survei Konsumen: 'Tabel 1'..'Tabel 9' of the monthly data-series workbook.

The generic reader already reads every sheet of this workbook correctly (audit 2026-10-07), so
this parser wraps it: what it adds is coverage, a shape check, the unit (the sheets state none)
and the golden-number tests. Tabel 5 (income allocation) and Tabel 8 (savings choices) are
shares in %, the rest are indices. Tabel 9 was discontinued in March 2020.
"""
from publication_parsers._common import parse_with_generic_reader, sheet_matches
from table_model import TableData

SHEET_PATTERNS = (r"Tabel [1-9]",)
_UNITS = ((r"Tabel [58]", "%"), (r"Tabel [1-9]", "Indeks"))


# The report's own wording for the income-use rows of Tabel 5: "proporsi pembayaran
# cicilan/utang (debt installment to income ratio)" is the 'Cicilan pinjaman' row and
# "pendapatan yang disimpan / ditabung (saving to income ratio)" the 'Tabungan' row, in every
# SK release.
TERM_ALIASES = {
    "pembayaran cicilan/utang": "cicilan pinjaman",
    "pembayaran cicilan": "cicilan pinjaman",
    "cicilan/utang": "cicilan pinjaman",
    "pendapatan konsumen yang disimpan": "tabungan",
    "pendapatan yang disimpan": "tabungan",
    "pendapatan yang ditabung": "tabungan",
}


def parse(data: bytes, sheet_name: str) -> TableData:
    unit = next(unit for pattern, unit in _UNITS if sheet_matches(pattern, sheet_name))
    table = parse_with_generic_reader(data, sheet_name, unit=unit)
    table.term_aliases = dict(TERM_ALIASES)
    return table
