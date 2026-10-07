"""Uang Beredar (M2): SEKI Tabel I.1 and I.1.A (reklasifikasi), read by the proven SEKI reader.

The historical sheets of TABEL1_1.xls ('Th 1985-1992' ...) are not covered.
"""
from publication_parsers._common import parse_with_bi_reader
from table_model import TableData

SHEET_PATTERNS = (r"I\.1A?",)


def parse(data: bytes, sheet_name: str) -> TableData:
    return parse_with_bi_reader(data, sheet_name)
