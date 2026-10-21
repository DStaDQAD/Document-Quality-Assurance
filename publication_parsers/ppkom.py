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

# The index's own name, which the extractor puts in front of a segment and city: "Indeks Harga
# Properti Komersial kategori sewa > convention hall > Jakarta" found the city's total 'JAKARTA'
# (Tw I-2026), while "convention hall > Jakarta" finds 'JAKARTA > Convention Hall'. Dropped only
# when a city follows: before a national row ("Indeks Harga Properti Komersial > Jual") the name
# is what keeps the claim off the city tables' 'MEDAN > Jual' (Tw II-2026).
CITIES = ("jakarta", "bodebek", "banten", "bandung", "makassar", "medan", "semarang", "surabaya",
          "balikpapan", "denpasar", "palembang")
MEASURES = ("harga", "pasokan", "permintaan")
# Matches no row: what a claim about another measure's index becomes on this sheet. Without the
# name the eighteen sheets are alike, and "pengembangan hotel … di Jakarta" (supply) was answered
# by Tabel 5 (price growth).
NO_ROW = "∅"


def _index_name(measure: str) -> str:
    return r"^\s*indeks\s+" + measure + r"\s+properti\s+komersial"


def _rewrites(own: str) -> tuple:
    """This sheet's index name dropped before a city; another measure's index matches nothing."""
    city = r"(?=.*(?<![a-z])(?:" + "|".join(CITIES) + r")(?![a-z]))"
    rewrites = [(re.compile(_index_name(own) + r"(?:\s+kategori\s+(?:jual|sewa))?\s*>\s*" + city,
                            re.IGNORECASE), "")]
    for other in MEASURES:
        if other != own:
            rewrites.append((re.compile(_index_name(other) + r"(?![a-z]).*$", re.IGNORECASE | re.DOTALL),
                             NO_ROW))
    return tuple(rewrites)

# A city's 'Jual' or 'Sewa' row without figures ('-', Denpasar) reads as a heading, and the
# segments below it as its children: 'DENPASAR > Jual > Hotel'. Segments sit beside them.
_CATEGORY = re.compile(r"^(?P<city>[^>]+?) > (?:Jual|Sewa) > (?P<segment>.+)$")


def parse(data: bytes, sheet_name: str) -> TableData:
    table = parse_with_generic_reader(data, sheet_name)
    # The Indonesian title only: the reader joins it with its English mirror, and Tabel 5's came
    # back as the pair repeated forty times (2.498 characters) — in every extraction prompt.
    table.title = re.split(r"\s+(?=Tab(?:el|le)\s*\d+\s*\.)", (table.title or "").strip())[0]
    number = int(re.search(r"\d+", sheet_name).group())
    table.unit = "%, qtq" if number in _QTQ else "%, yoy" if number in _YOY else "Indeks"
    _name_cities(table)
    _unnest_segments(table)
    measure = "harga" if number <= 6 else "pasokan" if number <= 12 else "permintaan"
    table.query_rewrites = _rewrites(measure)
    # The national rows by the index's full name, whole claims only: "Indeks Harga Properti
    # Komersial kategori jual" found 'JAKARTA > Jual'. Only this sheet's own measure.
    index = f"indeks {measure} properti komersial"
    aliases = {f"{index} kategori jual": "Jual", f"{index} kategori sewa": "Sewa"}
    table.label_aliases = {name: row for name, row in aliases.items() if row in table.row_labels}
    return table


def _unnest_segments(table: TableData) -> None:
    """'DENPASAR > Jual > Hotel' -> 'DENPASAR > Hotel' (see _CATEGORY)."""
    renamed = {}
    for label in table.row_labels:
        m = _CATEGORY.match(label)
        if m:
            flat = f"{m['city']}{QUAL_SEP}{m['segment']}"
            if flat not in table.row_labels:
                renamed[label] = flat
    if renamed:
        table.row_labels = [renamed.get(label, label) for label in table.row_labels]
        table._data = {(renamed.get(key[0], key[0]),) + key[1:]: value for key, value in table._data.items()}


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
