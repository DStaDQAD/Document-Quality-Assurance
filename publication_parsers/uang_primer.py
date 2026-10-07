"""Uang Primer (M0): SEKI Tabel I.2, read by the proven SEKI reader."""
from publication_parsers._common import parse_with_bi_reader
from table_model import TableData

SHEET_PATTERNS = (r"I\.2",)


def parse(data: bytes, sheet_name: str) -> TableData:
    return parse_with_bi_reader(data, sheet_name)
