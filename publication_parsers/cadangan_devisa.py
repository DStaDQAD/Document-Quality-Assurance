"""Cadangan Devisa: SEKI Tabel V.9 (sheet '5.9'), read by the proven SEKI reader, and the team's
own "Tabel Cadev ORA" (SDDS Official Reserve Assets, one sheet called 'sheet').

V.9: the sheet's total row is called 'Total'; the press release calls it "cadangan devisa", which
otherwise found the component 'Cadangan Devisa Lainnya' first (Agustus 2026: "posisi cadangan
devisa … meningkat" was judged on 125.345 -> 125.377, not 145.344 -> 146.508). The row is
renamed to what the report calls it.

ORA: rows 'Official Reserve Assets' > 'Foreign currency reserves', 'IMF reserve position …',
'SDRs', 'Gold', 'Other reserve assets', in Millions of US$, with the month in a cell of each row
("September/26", once "Auguts/26") and two value columns, 'Latest Data' (that month) and 'Latest
Data -1' (the month before). No generic reader finds a period header there, so a check with the
ORA table alone (the team's usual source, Oktober 2026) came back all Tidak Cukup Data.
"""
import re
from typing import List, Optional, Tuple

from publication_parsers._common import PublicationParseError, load_grid, parse_with_bi_reader
from table_model import TableData

SHEET_PATTERNS = (r"5\.9", r"sheet\d*", r"tabel sp.*")

TOTAL_ROW = "Cadangan Devisa"
ORA_TOTAL = "official reserve assets"
ORA_UNIT = "Juta USD"
# The release's names for the components, beside the SDDS ones.
TERM_ALIASES = {
    "cadangan devisa valas": "Foreign currency reserves",
    "aset valas": "Foreign currency reserves",
    "emas": "Gold",
    "sdr": "SDRs",
}

_MONTHS = ("jan", "feb", "mar", "apr", "may", "jun", "jul", "aug", "sep", "oct", "nov", "dec")
_MONTH_ID = {"mei": "may", "agu": "aug", "okt": "oct", "des": "dec"}
_MONTH_ABBR = ("Jan", "Feb", "Mar", "Apr", "May", "Jun", "Jul", "Aug", "Sep", "Oct", "Nov", "Dec")


def parse(data: bytes, sheet_name: str) -> TableData:
    if re.fullmatch(r"5\.9", sheet_name.strip()):
        return _parse_seki(data, sheet_name)
    if re.fullmatch(r"tabel sp.*", sheet_name.strip(), re.IGNORECASE):
        return _parse_indimon(load_grid(data, sheet_name))
    return _parse_ora(load_grid(data, sheet_name))


INDIMON_UNIT = "Miliar Rp"


def _parse_indimon(grid: List[List]) -> TableData:
    """The team's "Tabel Indimon" (PERKEMBANGAN BESARAN MONETER, sheet 'Tabel SP (indonesian
    version)'): monetary aggregates in billion rupiah by month, dated by a row of date cells, and
    one memorandum row, 'Cadangan Devisa (konsep IRFCL)' > 'Dalam Juta USD', which is the reserves.

    The SEKI reader took its first data column for the labels (rows named '930537.86…') and the
    check reported nothing wrong (Oktober 2026). Here: the date row gives each column its month,
    the 'Items' column the names, and the memorandum row becomes 'Cadangan Devisa' in Juta USD.
    """
    import datetime as _dt
    date_row = next((r for r, row in enumerate(grid)
                     if sum(isinstance(c, (_dt.date, _dt.datetime)) for c in row) >= 3), None)
    if date_row is None:
        raise PublicationParseError("bukan tabel Indimon: tidak ada baris tanggal")
    columns = {}
    for c, cell in enumerate(grid[date_row]):
        if isinstance(cell, (_dt.date, _dt.datetime)):
            key = (cell.year, _MONTH_ABBR[cell.month - 1])
            if key not in columns.values():
                columns[c] = key
    label_col = min(columns) - 1
    table = TableData(title="Perkembangan Besaran Moneter (Indimon)", unit=INDIMON_UNIT, row_labels=[])
    previous_label = ""
    for row in grid[date_row + 1:]:
        label = re.sub(r"\s+\d\)\s*$", "", _text(row[label_col] if label_col < len(row) else ""))
        numbers = {c: row[c] for c in columns if c < len(row) and isinstance(row[c], (int, float))}
        if not label:
            continue
        if not numbers:
            previous_label = label
            continue
        if label.lower().startswith("dalam juta usd") and "cadangan devisa" in previous_label.lower():
            label = TOTAL_ROW
            table.row_units[label] = "Juta USD"
        if label in table.row_labels:
            continue
        table.row_labels.append(label)
        for c, value in numbers.items():
            table._data[(label, *columns[c])] = float(value)
    if TOTAL_ROW not in table.row_labels:
        raise PublicationParseError("bukan tabel Indimon: baris cadangan devisa tidak ditemukan")
    return table


def _parse_seki(data: bytes, sheet_name: str) -> TableData:
    table = parse_with_bi_reader(data, sheet_name)
    if "Total" in table.row_labels and TOTAL_ROW not in table.row_labels:
        table.row_labels = [TOTAL_ROW if label == "Total" else label for label in table.row_labels]
        table._data = {((TOTAL_ROW, *key[1:]) if key[0] == "Total" else key): value
                       for key, value in table._data.items()}
    return table


def _text(value) -> str:
    return " ".join(str(value).split()) if value is not None else ""


def _ora_month(value) -> Optional[Tuple[int, int]]:
    """'September/26' -> (2026, 9); tolerant of typos in the month ('Auguts/26') and of
    Indonesian names ('Agustus/26'): the first three letters decide."""
    match = re.match(r"\s*([A-Za-z]{3})[A-Za-z]*\s*[/\-\s]\s*(\d{2,4})\s*$", _text(value))
    if not match:
        return None
    key = match.group(1).lower()
    key = _MONTH_ID.get(key, key)
    if key not in _MONTHS:
        return None
    year = int(match.group(2))
    return (2000 + year if year < 100 else year), _MONTHS.index(key) + 1


def _parse_ora(grid: List[List]) -> TableData:
    header = next((r for r, row in enumerate(grid)
                   if any(_text(c).lower() == "latest data" for c in row)), None)
    if header is None:
        raise PublicationParseError("bukan tabel Cadev ORA: tidak ada kolom 'Latest Data'")
    cols = {_text(c).lower(): i for i, c in enumerate(grid[header])}
    latest, previous = cols.get("latest data"), cols.get("latest data -1")
    date_col = next((i for name, i in cols.items() if name.startswith("date of latest")), None)
    if latest is None or date_col is None:
        raise PublicationParseError("bukan tabel Cadev ORA: kolom data tidak lengkap")
    table = TableData(title="Official Reserve Assets (SDDS)", unit=ORA_UNIT, row_labels=[])
    total = None
    for row in grid[header + 1:]:
        cells = list(row) + [None] * (max(latest, previous or 0, date_col) + 1 - len(row))
        label = next((_text(c) for c in cells[:date_col] if _text(c) and not re.match(r"millions?\b", _text(c), re.I)), "")
        period = _ora_month(cells[date_col])
        if not label or period is None or not isinstance(cells[latest], (int, float)):
            continue
        if label.lower() == ORA_TOTAL:
            label = total = TOTAL_ROW
        elif total:
            label = f"{total} > {label}"
        (year, month) = period
        prior = (year, month - 1) if month > 1 else (year - 1, 12)
        table.row_labels.append(label)
        table._data[(label, year, _MONTH_ABBR[month - 1])] = float(cells[latest])
        if previous is not None and isinstance(cells[previous], (int, float)):
            table._data[(label, prior[0], _MONTH_ABBR[prior[1] - 1])] = float(cells[previous])
    if total is None or not table._data:
        raise PublicationParseError("bukan tabel Cadev ORA: baris 'Official Reserve Assets' tidak ditemukan")
    table.term_aliases = {phrase: f"{TOTAL_ROW} > {row}" for phrase, row in TERM_ALIASES.items()
                          if f"{TOTAL_ROW} > {row}" in table.row_labels}
    return table
