"""Survei Properti Komersial (PPKom): 'Tabel 1'..'Tabel 18' of the quarterly workbook.

Three measures — price (Tabel 1-6), supply (7-12) and demand (13-18) — each as an index level,
quarterly growth (%, qtq) and annual growth (%, yoy); odd tables hold the national figures and
Jakarta, Bodebek, Banten, Bandung, Makassar, even tables Medan, Semarang, Surabaya, Balikpapan,
Denpasar, Palembang. The odd tables of one measure repeat the same row names ('Sewa', 'Jual',
'Perkantoran', …), so the unit is what tells a growth claim which of them answers it.

Labels in column 2, years over Roman quarters, an English mirror on the right — the generic reader
reads them. A city's row ('6', 'MEDAN') carries the city's own figures and heads its property
types below it, which the reader files under the city's number ('6 > Sewa'); here they are named
after the city ('MEDAN > Sewa', the city row itself 'MEDAN').
"""
import re

from publication_parsers._common import parse_with_generic_reader
from table_model import QUAL_SEP, TableData

SHEET_PATTERNS = (r"Tabel \d+",)

_QTQ = {3, 4, 9, 10, 15, 16}
_YOY = {5, 6, 11, 12, 17, 18}


def parse(data: bytes, sheet_name: str) -> TableData:
    table = parse_with_generic_reader(data, sheet_name)
    number = int(re.search(r"\d+", sheet_name).group())
    table.unit = "%, qtq" if number in _QTQ else "%, yoy" if number in _YOY else "Indeks"
    _name_cities(table)
    return table


def _name_cities(table: TableData) -> None:
    """'6 > MEDAN' -> 'MEDAN', '6 > Sewa' -> 'MEDAN > Sewa': the first row under a number is the
    city. Rows without a number (the national block of the odd tables) keep their names."""
    cities = {}
    for label in table.row_labels:
        head, sep, rest = label.partition(QUAL_SEP)
        if sep and head.strip().isdigit() and head.strip() not in cities:
            cities[head.strip()] = rest.strip()
    if not cities:
        return
    renamed = {}
    for label in table.row_labels:
        head, sep, rest = label.partition(QUAL_SEP)
        city = cities.get(head.strip()) if sep else None
        if city is None:
            continue
        renamed[label] = city if rest.strip() == city else f"{city}{QUAL_SEP}{rest.strip()}"
    table.row_labels = [renamed.get(label, label) for label in table.row_labels]
    table._data = {(renamed.get(key[0], key[0]),) + key[1:]: value for key, value in table._data.items()}
