"""SKDU: 'T1 Kegiatan Usaha' .. 'T10 Margin Usaha' (plus 'T7b Investasi Semesteran').

The generic reader already reads every sheet of this workbook correctly (audit 2026-10-07), so
this parser wraps it: what it adds is coverage, a shape check, the Indonesian half of each
sheet's bilingual unit cell, and the golden-number tests.
"""
from publication_parsers._common import parse_with_generic_reader
from table_model import TableData

SHEET_PATTERNS = (r"T\d+b? .+",)


def parse(data: bytes, sheet_name: str) -> TableData:
    return parse_with_generic_reader(data, sheet_name)
