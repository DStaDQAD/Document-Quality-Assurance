"""PII: SEKI Tabel V.39 (Posisi Investasi Internasional, quarterly, .xls).

Same layout as NPI: labels in column 2 nested by indent ('Aset' > 'Investasi Langsung' >
'Modal Ekuitas'), years over 'Q1'..'Q4', annual columns for the early years (skipped).
"""
from publication_parsers._common import SheetSpec, parse_with_specs
from table_model import TableData

SPECS = ((r"5\.39", SheetSpec(label_cols=(2,), hierarchy="indent")),)
SHEET_PATTERNS = tuple(pattern for pattern, _ in SPECS)


def parse(data: bytes, sheet_name: str) -> TableData:
    return parse_with_specs(data, sheet_name, SPECS)
