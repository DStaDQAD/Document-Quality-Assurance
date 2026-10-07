"""Survei Penjualan Eceran: 'Tabel 1'..'Tabel 9' of the monthly data-series workbook.

Labels in column 0 ('DESKRIPSI' / 'KOTA'), years in row 3 over months (Indonesian: 'Mei',
'Agt*') or quarters in row 4; a 'Perubahan (Poin)' block and an English mirror on the right.
Tabel 9 groups '- 3 bulan yang akan datang' rows under 'Ekspektasi Penjualan' /
'Ekspektasi Harga Umum' section rows, hence "sections". The sheets carry no unit cell:
index tables are 'Indeks', growth tables (yoy, mtm, qtq) are '%'.
"""
from publication_parsers._common import SheetSpec, parse_with_specs
from table_model import TableData

SPECS = (
    (r"Tabel [159]", SheetSpec(label_cols=(0,), hierarchy="sections", unit="Indeks")),
    (r"Tabel [2-46-8]", SheetSpec(label_cols=(0,), hierarchy="sections", unit="%")),
)
SHEET_PATTERNS = tuple(pattern for pattern, _ in SPECS)


def parse(data: bytes, sheet_name: str) -> TableData:
    return parse_with_specs(data, sheet_name, SPECS)
