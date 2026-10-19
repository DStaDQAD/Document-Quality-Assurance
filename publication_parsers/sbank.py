"""Survei Perbankan: 'Tabel1'..'Tabel4' and the discontinued 'Tabel 5 (disc)'.

Indonesian labels sit in columns 1–2 (Tabel1, Tabel 5) or 1–3 (Tabel2–4: period kind, group,
item), English copies right after them, years over Roman quarters ('I'..'IV', 'III*') from the
next column on. Tabel3 and Tabel4 stack two or three blocks — realisation, quarterly estimate,
whole-year estimate — each with its own header rows; numbers may be stored as text. Units:
saldo bersih tertimbang (Tabel1, Tabel3, Tabel 5 — their titles say '(SBT, %)', and the SBT in
the unit is what lets "diprakirakan tetap tumbuh" be read as a positive balance), priorities
(Tabel2, 1 = first) and interest rates in percent (Tabel4).
"""
import re

from publication_parsers._common import SheetSpec, parse_with_specs
from table_model import TableData

SPECS = (
    (r"Tabel ?1|Tabel ?5.*", SheetSpec(label_cols=(1, 2), unit="SBT, %")),
    (r"Tabel ?2", SheetSpec(label_cols=(1, 2, 3), unit="peringkat")),
    (r"Tabel ?3", SheetSpec(label_cols=(1, 2, 3), unit="SBT, %")),
    (r"Tabel ?4", SheetSpec(label_cols=(1, 2, 3), unit="%")),
)
SHEET_PATTERNS = tuple(pattern for pattern, _ in SPECS)

# "Penghimpunan DPK … diprakirakan tetap tumbuh": Tabel3 is the deposits forecast, and its rows
# never say DPK — the whole is 'Gabungan > Total'. Kept to that sheet: on Tabel1 'TOTAL' is new
# loans.
TERM_ALIASES_BY_SHEET = (
    (r"Tabel ?3$", {
        "dana pihak ketiga (dpk)": "Gabungan > Total",
        "dana pihak ketiga": "Gabungan > Total",
        "dpk": "Gabungan > Total",
    }),
)

# The period kind and 'Gabungan' (all banks) head every row of Tabel2–4: layout, not a group, so
# "instrumen deposito" is 'Prakiraan per Triwulan > Gabungan > Deposito' itself.
LAYOUT_LEVELS = frozenset({
    "realisasi per triwulan", "prakiraan per triwulan", "prakiraan selama setahun", "gabungan",
})


def parse(data: bytes, sheet_name: str) -> TableData:
    table = parse_with_specs(data, sheet_name, SPECS)
    for pattern, aliases in TERM_ALIASES_BY_SHEET:
        if re.match(pattern, sheet_name.strip()):
            table.term_aliases = dict(aliases)
    table.layout_levels = LAYOUT_LEVELS
    return table
