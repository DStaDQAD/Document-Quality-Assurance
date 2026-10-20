"""Cadangan Devisa: SEKI Tabel V.9 (sheet '5.9'), read by the proven SEKI reader.

The sheet's total row is called 'Total'; the press release calls it "cadangan devisa", which
otherwise found the component 'Cadangan Devisa Lainnya' first (Agustus 2026: "posisi cadangan
devisa … meningkat" was judged on 125.345 -> 125.377, not 145.344 -> 146.508). The row is
renamed to what the report calls it.
"""
from publication_parsers._common import parse_with_bi_reader
from table_model import TableData

SHEET_PATTERNS = (r"5\.9",)

TOTAL_ROW = "Cadangan Devisa"


def parse(data: bytes, sheet_name: str) -> TableData:
    table = parse_with_bi_reader(data, sheet_name)
    if "Total" in table.row_labels and TOTAL_ROW not in table.row_labels:
        table.row_labels = [TOTAL_ROW if label == "Total" else label for label in table.row_labels]
        table._data = {((TOTAL_ROW, *key[1:]) if key[0] == "Total" else key): value
                       for key, value in table._data.items()}
    return table
