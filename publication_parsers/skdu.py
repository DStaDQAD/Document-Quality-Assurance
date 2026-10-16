"""SKDU: 'T1 Kegiatan Usaha' .. 'T10 Margin Usaha' (plus 'T7b Investasi Semesteran').

The generic reader already reads every sheet of this workbook correctly (audit 2026-10-07), so
this parser wraps it: what it adds is coverage, a shape check, the Indonesian half of each
sheet's bilingual unit cell, and the golden-number tests.
"""
import re

from publication_parsers._common import parse_with_generic_reader
from table_model import TableData

SHEET_PATTERNS = (r"T\d+b? .+",)

# The report's own names for a sheet's rows, by sheet. SKDU Tw II-2026: "tekanan harga pada level
# produsen terindikasi meningkat … dan diprakirakan menurun pada triwulan III" is the selling-
# price balance of Tabel 5 (15,82 -> 22,38 -> 15,94) — the respondents are the producers. Kept to
# that sheet: on every sheet 'TOTAL' would answer it with that sheet's own total.
TERM_ALIASES_BY_SHEET = (
    (r"T5\b", {
        "tekanan harga pada level produsen": "TOTAL",
        "tekanan harga di tingkat produsen": "TOTAL",
        "tekanan harga di level produsen": "TOTAL",
        "tekanan harga produsen": "TOTAL",
    }),
)


def parse(data: bytes, sheet_name: str) -> TableData:
    table = parse_with_generic_reader(data, sheet_name)
    for pattern, aliases in TERM_ALIASES_BY_SHEET:
        if re.match(pattern, sheet_name) and "TOTAL" in table.row_labels:
            table.term_aliases = dict(aliases)
    return table
