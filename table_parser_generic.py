"""Heuristic parser for arbitrary 'clean' tables — Tier 2 of the parsing cascade.

parse_bi_table (Tier 1) handles the known BI wide time-series layout. This module handles
everything else that still has a detectable single-row header structure:

  - Time-series tables whose period headers are COMBINED in one row ("Apr 2026", "2026M04",
    "Apr-26", real Excel date cells) instead of BI's split year-row + month-row.
  - Non-time-series (categorical) tables such as item lists / inventories / budgets:
    a header row of attribute names ("Nama Barang | Harga | Stok") over data rows.

Detection strategy (all deterministic — no LLM, values never leave code):
  1. Header row  : first row (within the top 20) with >= 2 non-empty cells where >= 80% of
                   the non-empty cells are text or period-parseable, and at least one of the
                   next 6 rows carries a numeric cell.
  2. Label column: leftmost column whose cells below the header are >= 60% non-numeric text.
                   Columns left of it (e.g. numeric row-index columns) are ignored.
  3. Axis kind   : if >= 60% of the data-column headers (min 2) parse as (year, month)
                   periods, the table is TEMPORAL and is stored under (label, year, month)
                   keys; otherwise it is CATEGORICAL and stored under (label, col_name) keys.
  4. Title/unit  : non-empty rows above the header; a row fully wrapped in parentheses is the
                   unit, the first other row is the title.

Known limitations (deliberate Tier-2 scope — messier layouts belong to the future LLM
structure-mapping tier):
  - Only numeric cell values are stored; text attributes (e.g. 'Kategori') are not
    verifiable and are skipped.
  - The leftmost text column is assumed to be the row key. If an ID/code column precedes
    the human-readable name column, rows are keyed by the code.
  - Duplicate row labels / column headers keep the first occurrence.
  - Split two-row period headers (bare year row + month row) are NOT handled here — that is
    exactly the BI layout, which Tier 1 already covers.
"""

import io
import re
from datetime import date, datetime
from typing import Dict, List, Optional, Tuple

from table_model import TableData

_MONTH_ABBREVS = ["Jan", "Feb", "Mar", "Apr", "May", "Jun",
                  "Jul", "Aug", "Sep", "Oct", "Nov", "Dec"]

# Indonesian + English month names/abbreviations -> canonical 3-letter English abbreviation
# (the same canonical form structured_extractor emits and TableData keys on).
_MONTH_NAMES: Dict[str, str] = {
    "jan": "Jan", "januari": "Jan", "january": "Jan",
    "feb": "Feb", "februari": "Feb", "february": "Feb", "peb": "Feb",
    "mar": "Mar", "mrt": "Mar", "maret": "Mar", "march": "Mar",
    "apr": "Apr", "april": "Apr",
    "may": "May", "mei": "May",
    "jun": "Jun", "juni": "Jun", "june": "Jun",
    "jul": "Jul", "juli": "Jul", "july": "Jul",
    "aug": "Aug", "agu": "Aug", "ags": "Aug", "agt": "Aug", "agustus": "Aug", "august": "Aug",
    "sep": "Sep", "sept": "Sep", "september": "Sep",
    "oct": "Oct", "okt": "Oct", "oktober": "Oct", "october": "Oct",
    "nov": "Nov", "nop": "Nov", "november": "Nov",
    "dec": "Dec", "des": "Dec", "desember": "Dec", "december": "Dec",
}

_MAX_HEADER_SCAN = 20   # rows from the top considered as header candidates
_DATA_PROBE_ROWS = 6    # rows below a header candidate probed for numeric data
_HEADERISH_MIN_FRAC = 0.8
_LABEL_TEXT_MIN_FRAC = 0.6
_PERIOD_MIN_FRAC = 0.6

_NAME_YEAR_RE = re.compile(r"^([A-Za-z]+)[\s.\-/']*(\d{2}|\d{4})$")
_YEAR_NAME_RE = re.compile(r"^(\d{4})[\s.\-/']*([A-Za-z]+)$")
_YEAR_M_NUM_RE = re.compile(r"^(\d{4})[Mm\-/](\d{1,2})$")
_NUM_YEAR_RE = re.compile(r"^(\d{1,2})[\-/](\d{4})$")


def _normalize_year(raw: str) -> Optional[int]:
    year = int(raw)
    if len(raw) == 2:
        year += 2000 if year < 70 else 1900
    return year if 1990 <= year <= 2100 else None


def _parse_period(value) -> Optional[Tuple[int, str]]:
    """Return (year, month_abbrev) when a header cell denotes one calendar month, else None."""
    if isinstance(value, (datetime, date)):
        if 1990 <= value.year <= 2100:
            return value.year, _MONTH_ABBREVS[value.month - 1]
        return None
    if not isinstance(value, str):
        return None
    text = re.sub(r"\s+", " ", value).strip().rstrip("*").strip()
    if not text:
        return None

    m = _NAME_YEAR_RE.match(text)
    if m:
        month = _MONTH_NAMES.get(m.group(1).lower())
        year = _normalize_year(m.group(2))
        if month and year:
            return year, month
        return None
    m = _YEAR_NAME_RE.match(text)
    if m:
        month = _MONTH_NAMES.get(m.group(2).lower())
        year = _normalize_year(m.group(1))
        if month and year:
            return year, month
        return None
    m = _YEAR_M_NUM_RE.match(text)
    if m:
        year, month_num = _normalize_year(m.group(1)), int(m.group(2))
        if year and 1 <= month_num <= 12:
            return year, _MONTH_ABBREVS[month_num - 1]
        return None
    m = _NUM_YEAR_RE.match(text)
    if m:
        month_num, year = int(m.group(1)), _normalize_year(m.group(2))
        if year and 1 <= month_num <= 12:
            return year, _MONTH_ABBREVS[month_num - 1]
    return None


# ---------------------------------------------------------------------------
# Grid loading (.xls via xlrd, .xlsx via openpyxl) — values only, dates as datetime
# ---------------------------------------------------------------------------

def _load_grid(data: bytes, sheet_name: str) -> List[List]:
    if data[:8] == b'\xd0\xcf\x11\xe0\xa1\xb1\x1a\xe1':
        import xlrd

        wb = xlrd.open_workbook(file_contents=data)
        try:
            ws = wb.sheet_by_name(sheet_name)
        except xlrd.XLRDError:
            raise ValueError(f"Sheet '{sheet_name}' not found. Available: {wb.sheet_names()}")
        grid: List[List] = []
        for r in range(ws.nrows):
            row = []
            for c in range(ws.ncols):
                v = ws.cell_value(r, c)
                if ws.cell_type(r, c) == xlrd.XL_CELL_DATE:
                    v = xlrd.xldate.xldate_as_datetime(v, wb.datemode)
                row.append(v)
            grid.append(row)
        return grid
    elif data[:4] == b'PK\x03\x04':
        import openpyxl

        wb = openpyxl.load_workbook(io.BytesIO(data), read_only=True, data_only=True)
        if sheet_name not in wb.sheetnames:
            raise ValueError(f"Sheet '{sheet_name}' not found. Available: {wb.sheetnames}")
        try:
            return [list(row) for row in wb[sheet_name].iter_rows(values_only=True)]
        finally:
            wb.close()
    else:
        raise ValueError("Unrecognized file format: expected .xls or .xlsx bytes.")


# ---------------------------------------------------------------------------
# Structure detection
# ---------------------------------------------------------------------------

def _is_empty(v) -> bool:
    return v is None or (isinstance(v, str) and not v.strip())


def _is_number(v) -> bool:
    return isinstance(v, (int, float)) and not isinstance(v, bool)


def _find_header_row(grid: List[List]) -> int:
    for r in range(min(_MAX_HEADER_SCAN, len(grid))):
        nonempty = [v for v in grid[r] if not _is_empty(v)]
        if len(nonempty) < 2:
            continue
        headerish = [
            v for v in nonempty
            if (isinstance(v, str) and not _is_number(v)) or _parse_period(v) is not None
        ]
        if len(headerish) / len(nonempty) < _HEADERISH_MIN_FRAC:
            continue
        has_data_below = any(
            any(_is_number(v) for v in grid[rr])
            for rr in range(r + 1, min(r + 1 + _DATA_PROBE_ROWS, len(grid)))
        )
        if has_data_below:
            return r
    raise ValueError(
        "Tidak dapat mendeteksi baris header tabel: tidak ada baris teks dengan "
        "data numerik di bawahnya dalam 20 baris pertama."
    )


def _find_label_col(grid: List[List], header_row: int) -> int:
    n_cols = max((len(row) for row in grid), default=0)
    for c in range(n_cols):
        cells = [row[c] for row in grid[header_row + 1:] if c < len(row) and not _is_empty(row[c])]
        if not cells:
            continue
        text_cells = [v for v in cells if isinstance(v, str)]
        if len(text_cells) / len(cells) >= _LABEL_TEXT_MIN_FRAC:
            return c
    raise ValueError(
        "Tidak dapat mendeteksi kolom label: tidak ada kolom yang mayoritas berisi teks."
    )


def _title_and_unit(grid: List[List], header_row: int) -> Tuple[str, str]:
    title, unit = "", ""
    for r in range(header_row):
        row_texts = [str(v).strip() for v in grid[r] if not _is_empty(v)]
        if not row_texts:
            continue
        joined = " ".join(row_texts)
        m = re.fullmatch(r"\((.+)\)", joined)
        if m and not unit:
            unit = m.group(1).strip()
        elif not title:
            title = joined
    return title, unit


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------

def parse_generic_table(data: bytes, sheet_name: str) -> TableData:
    """Parse an arbitrary single-header-row table from .xls/.xlsx bytes into a TableData.

    Returns a TEMPORAL table (lookup by year/month) when the column headers read as
    calendar periods, otherwise a CATEGORICAL table (lookup by attribute/column name).
    Raises ValueError when no plausible table structure is found.
    """
    grid = _load_grid(data, sheet_name)
    header_row = _find_header_row(grid)
    label_col = _find_label_col(grid, header_row)
    title, unit = _title_and_unit(grid, header_row)

    header = grid[header_row]
    data_cols = [
        c for c in range(label_col + 1, len(header)) if not _is_empty(header[c])
    ]
    if not data_cols:
        raise ValueError("Tidak ada kolom data di kanan kolom label.")

    col_periods = {c: _parse_period(header[c]) for c in data_cols}
    n_period_cols = sum(1 for p in col_periods.values() if p is not None)
    temporal = n_period_cols >= 2 and n_period_cols / len(data_cols) >= _PERIOD_MIN_FRAC

    col_keys: Dict[int, Tuple] = {}
    col_labels: List[str] = []
    if temporal:
        col_keys = {c: p for c, p in col_periods.items() if p is not None}
    else:
        for c in data_cols:
            name = str(header[c]).strip()
            if name and name not in col_labels:
                col_labels.append(name)
                col_keys[c] = (name,)

    table_data: Dict[Tuple, float] = {}
    row_labels: List[str] = []
    for row in grid[header_row + 1:]:
        if label_col >= len(row) or _is_empty(row[label_col]):
            continue
        label = str(row[label_col]).strip()
        stored = False
        for c, key_part in col_keys.items():
            if c < len(row) and _is_number(row[c]):
                table_data.setdefault((label, *key_part), float(row[c]))
                stored = True
        if stored and label not in row_labels:
            row_labels.append(label)

    if not row_labels or not table_data:
        raise ValueError(
            "Tabel terdeteksi tetapi tidak ada baris berlabel dengan nilai numerik."
        )

    return TableData(
        title=title,
        unit=unit,
        row_labels=row_labels,
        col_labels=col_labels,
        axis_type="temporal" if temporal else "categorical",
        _data=table_data,
    )
