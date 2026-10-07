"""Cadangan Devisa: SEKI Tabel V.9 (sheet '5.9'), read by the proven SEKI reader."""
from publication_parsers._common import parse_with_bi_reader
from table_model import TableData

SHEET_PATTERNS = (r"5\.9",)


def parse(data: bytes, sheet_name: str) -> TableData:
    return parse_with_bi_reader(data, sheet_name)
