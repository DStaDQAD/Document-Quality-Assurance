"""Prompt Manufacturing Index - BI: 'T1 …' (components) and 'T2 …' (sub-sectors).

T1: labels in column 1, years in row 4 over 'I'..'IV' in row 5. T2: Indonesian labels in
column 2 (column 3 is English, column 4 a longer Indonesian name). BI renamed the first sheet
between editions ('T1 - Komponen PMI' → 'T1 PMI'), hence the patterns. Values are diffusion
indices (50 = no change).
"""
from publication_parsers._common import SheetSpec, parse_with_specs
from table_model import TableData

SPECS = (
    (r"T1\b.*", SheetSpec(label_cols=(1,), unit="Indeks")),
    (r"T2\b.*", SheetSpec(label_cols=(2,), unit="Indeks")),
)
SHEET_PATTERNS = tuple(pattern for pattern, _ in SPECS)


def parse(data: bytes, sheet_name: str) -> TableData:
    return parse_with_specs(data, sheet_name, SPECS)
