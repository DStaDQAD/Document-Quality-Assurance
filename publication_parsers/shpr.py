"""Survei Harga Properti Residensial: 'TABEL 1' (national index), 'TABEL 2' (index per city),
'TABEL 3' (growth per city).

TABEL 1/2: labels in column 2 under section rows without figures ('TIPE BANGUNAN', '1 BANDUNG');
years in row 5 over 'QI'..'QIV' / 'Q2' in row 6; English labels on the right. TABEL 3: labels in
column 3 under city section rows, and the columns split into a 'TRIWULANAN (QTQ)' band and a
'TAHUNAN (YOY)' band that repeat the same quarters — the band becomes the outermost label. Its
unit '%, qtq & yoy' marks the cells as growth figures; the band in each label tells the
verifier which kind a matched cell holds.
"""
from publication_parsers._common import SheetSpec, parse_with_specs
from table_model import TableData

SPECS = (
    (r"TABEL [12]", SheetSpec(label_cols=(2,), hierarchy="sections", unit="Indeks (2018=100)")),
    (r"TABEL 3", SheetSpec(label_cols=(3,), hierarchy="sections", bands=True, unit="%, qtq & yoy")),
)
SHEET_PATTERNS = tuple(pattern for pattern, _ in SPECS)


def parse(data: bytes, sheet_name: str) -> TableData:
    return parse_with_specs(data, sheet_name, SPECS)
