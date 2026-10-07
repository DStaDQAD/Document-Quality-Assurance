"""Survei Perbankan: 'Tabel1'..'Tabel4' and the discontinued 'Tabel 5 (disc)'.

Indonesian labels sit in columns 1–2 (Tabel1, Tabel 5) or 1–3 (Tabel2–4: period kind, group,
item), English copies right after them, years over Roman quarters ('I'..'IV', 'III*') from the
next column on. Tabel3 and Tabel4 stack two or three blocks — realisation, quarterly estimate,
whole-year estimate — each with its own header rows; numbers may be stored as text. Units:
saldo bersih tertimbang (%) except Tabel2, which ranks priorities (1 = first).
"""
from publication_parsers._common import SheetSpec, parse_with_specs
from table_model import TableData

SPECS = (
    (r"Tabel ?1|Tabel ?5.*", SheetSpec(label_cols=(1, 2), unit="%")),
    (r"Tabel ?2", SheetSpec(label_cols=(1, 2, 3), unit="peringkat")),
    (r"Tabel ?[34]", SheetSpec(label_cols=(1, 2, 3), unit="%")),
)
SHEET_PATTERNS = tuple(pattern for pattern, _ in SPECS)


def parse(data: bytes, sheet_name: str) -> TableData:
    return parse_with_specs(data, sheet_name, SPECS)
