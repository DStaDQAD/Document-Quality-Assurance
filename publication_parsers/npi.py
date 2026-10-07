"""NPI: SEKI Tabel V.1 (Neraca Pembayaran Indonesia, quarterly, .xls).

Labels in column 2 nest by the label cell's indent ('I. Transaksi Berjalan' > 'A. Barang' >
'- Ekspor, fob'); years in row 4 over 'Q1'..'Q4' in row 5; an English mirror on the right.
The SEKI monthly reader cannot read it (no month row), hence the engine.
"""
from publication_parsers._common import SheetSpec, parse_with_specs
from table_model import TableData

SPECS = ((r"5\.1", SheetSpec(label_cols=(2,), hierarchy="indent")),)
SHEET_PATTERNS = tuple(pattern for pattern, _ in SPECS)


def parse(data: bytes, sheet_name: str) -> TableData:
    return parse_with_specs(data, sheet_name, SPECS)
