"""Survei Penjualan Eceran: 'Tabel 1'..'Tabel 9' of the monthly data-series workbook.

Labels in column 0 ('DESKRIPSI' / 'KOTA'), years in row 3 over months (Indonesian: 'Mei',
'Agt*') or quarters in row 4; a 'Perubahan (Poin)' block and an English mirror on the right.
Tabel 9 groups '- 3 bulan yang akan datang' rows under 'Ekspektasi Penjualan' /
'Ekspektasi Harga Umum' section rows, hence "sections". The sheets carry no unit cell, so the
unit says what the cells are: index tables 'Indeks', the yoy growth tables (2, 6; quarterly 4,
8) '%, yoy' and the mtm ones (3, 7) '%, mtm' — the verifier reads a yoy table's cell as the
answer to a yoy claim and keeps a yoy claim off an mtm table.
"""
import re

from publication_parsers._common import SheetSpec, parse_with_specs
from table_model import QUAL_SEP, TableData

SPECS = (
    (r"Tabel [159]", SheetSpec(label_cols=(0,), hierarchy="sections", unit="Indeks")),
    (r"Tabel [2468]", SheetSpec(label_cols=(0,), hierarchy="sections", unit="%, yoy")),
    (r"Tabel [37]", SheetSpec(label_cols=(0,), hierarchy="sections", unit="%, mtm")),
)
SHEET_PATTERNS = tuple(pattern for pattern, _ in SPECS)

_MONTHS = ("Jan", "Feb", "Mar", "Apr", "May", "Jun", "Jul", "Aug", "Sep", "Oct", "Nov", "Dec")

# The report's names for Tabel 9's indices, which the sheet titles 'Ekspektasi Penjualan' /
# 'Ekspektasi Harga Umum'; "tekanan inflasi … diprakirakan meningkat" is the price index.
EXPECTATION_ALIASES = {
    "indeks ekspektasi penjualan (iep)": "Ekspektasi Penjualan",
    "indeks ekspektasi penjualan": "Ekspektasi Penjualan",
    "iep": "Ekspektasi Penjualan",
    "indeks ekspektasi harga umum (ieh)": "Ekspektasi Harga Umum",
    "indeks ekspektasi harga umum": "Ekspektasi Harga Umum",
    "ieh": "Ekspektasi Harga Umum",
    "tekanan inflasi": "Ekspektasi Harga Umum",
}

# The report's names for the whole index in Tabel 1-4, whole claims only (TableData.label_aliases):
# SPE Juni 2026 "Pada Juni 2026, IPR tercatat sebesar 225,0" found no row 'IPR'. Not as a term
# alias: "penjualan eceran Kelompok Sandang" is a group, not the total.
TOTAL_ALIASES = {name: "INDEKS TOTAL" for name in (
    "ipr", "indeks penjualan riil", "indeks penjualan riil (ipr)", "ipr penjualan eceran",
    "penjualan eceran", "kinerja penjualan eceran", "kinerja penjualan", "penjualan",
)}

# The report's names for the city rows of Tabel 5-8.
CITY_ALIASES = {"semarang (termasuk purwokerto)": "Semarang"}


def parse(data: bytes, sheet_name: str) -> TableData:
    table = parse_with_specs(data, sheet_name, SPECS)
    if re.match(r"Tabel 9\b", sheet_name.strip()):
        _add_target_month_rows(table)
        table.term_aliases = dict(EXPECTATION_ALIASES)
        # "Ekspektasi Harga Umum > 6 bulan yang akan datang" for February 2027 is the target-month
        # row: the horizon only says which survey printed it. An exact survey-month key still
        # answers first (TableData.lookup_fuzzy), so the sheet's own rows read as before.
        table.measure_levels = frozenset(
            label.split(QUAL_SEP, 1)[1].strip().lower() for label in table.row_labels
            if QUAL_SEP in label and re.search(r"\d+\s*bulan", label))
    elif re.match(r"Tabel [1-4]\b", sheet_name.strip()) and "INDEKS TOTAL" in table.row_labels:
        table.label_aliases = dict(TOTAL_ALIASES)
    elif re.match(r"Tabel [5-8]\b", sheet_name.strip()) and "Semarang" in table.row_labels:
        table.term_aliases = dict(CITY_ALIASES)
    return table


def _add_target_month_rows(table: TableData) -> None:
    """Each expectation index once more, keyed by the month it is FOR.

    The sheet keys 'Ekspektasi Penjualan > 3 bulan yang akan datang' by the survey month; the
    report names the month ahead: "IEP November 2026 … 148,2" is the August survey's 3-month
    figure, "IEP Februari 2027 … 143,8" its 6-month one. A target month reachable from two
    surveys takes the later survey (the shorter horizon). The original rows stay as they are.
    """
    merged = {}
    for label in list(table.row_labels):
        if QUAL_SEP not in label:
            continue
        section, leaf = label.split(QUAL_SEP, 1)
        horizon = re.search(r"(\d+)\s*bulan", leaf)
        if not horizon:
            continue
        months_ahead = int(horizon.group(1))
        for key, value in table._data.items():
            if len(key) != 3 or key[0] != label or key[2] not in _MONTHS:
                continue
            index = key[1] * 12 + _MONTHS.index(key[2]) + months_ahead
            target = (index // 12, _MONTHS[index % 12])
            slot = merged.setdefault(section.strip(), {})
            if target not in slot or months_ahead < slot[target][0]:
                slot[target] = (months_ahead, value)
    for section, values in merged.items():
        if section in table.row_labels:
            continue
        table.row_labels.append(section)
        for (year, month), (_ahead, value) in values.items():
            table._data[(section, year, month)] = value
