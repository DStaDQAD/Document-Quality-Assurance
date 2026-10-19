"""Prompt Manufacturing Index - BI: 'T1 …' (components) and 'T2 …' (sub-sectors).

T1: labels in column 1, years in row 4 over 'I'..'IV' in row 5. T2: Indonesian labels in
column 2 (column 3 is English, column 4 a longer Indonesian name). BI renamed the first sheet
between editions ('T1 - Komponen PMI' → 'T1 PMI'), hence the patterns. Values are diffusion
indices (50 = no change) that the report prints in percent ("52,03%"), hence the sheet's own
unit '%, Indeks': the verifier compares percent claims only with an index that carries the %.
Each sheet ends at its forecast quarter ('I*'); the Tw IV-2025 T2 carried three template columns
after it (two repeating 2025's Q2 and Q3), hence end_at_forecast.
"""
from publication_parsers._common import (
    PublicationParseError, SheetSpec, parse_with_specs, sheet_matches,
)
from table_model import TableData

SPECS = (
    (r"T1\b.*", SheetSpec(label_cols=(1,), unit="%, Indeks", end_at_forecast=True)),
    (r"T2\b.*", SheetSpec(label_cols=(2,), unit="%, Indeks", end_at_forecast=True)),
)
SHEET_PATTERNS = tuple(pattern for pattern, _ in SPECS)

# The internal release workbook ('Tabel PMI 17 Lapangan Usaha Tw II/III-2026.xlsx' on SharePoint)
# lays T2 out without the short Indonesian column: English in column 3, the Indonesian name in
# column 4. Read with SPECS it had no labels at all, so every sub-sector claim was left unchecked.
INTERNAL_T2 = ((r"T2\b.*", SheetSpec(label_cols=(4,), unit="%, Indeks", end_at_forecast=True)),)


def parse(data: bytes, sheet_name: str) -> TableData:
    try:
        return parse_with_specs(data, sheet_name, SPECS)
    except PublicationParseError:
        if not sheet_matches(r"T2\b.*", sheet_name):
            raise
        return parse_with_specs(data, sheet_name, INTERNAL_T2)
