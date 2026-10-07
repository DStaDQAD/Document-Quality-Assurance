"""Shared building blocks for the per-publication parsers.

Most BI publication workbooks are the same kind of sheet underneath: a block of label columns
on the left, a header made of a year row over a period row (or one 'Q1-2026' row), and one
column per month or quarter — with annual columns, preliminary-figure stars, an English mirror
on the right and a footnote block at the bottom. parse_series_sheet reads that shape from a
SheetSpec saying where the labels are and how the row hierarchy is encoded; each publication
module only declares its specs. Sheets the older readers already handle (SEKI monthly tables,
the survey workbooks) are wrapped instead of re-implemented.
"""
from __future__ import annotations

import datetime as _dt
import hashlib
import re
from collections import OrderedDict
from typing import Dict, List, Optional, Tuple

from table_parser_generic import _bare_period_token, _parse_period, load_workbook_grids

_XLS_MAGIC = b"\xd0\xcf\x11\xe0\xa1\xb1\x1a\xe1"


class PublicationParseError(ValueError):
    """A sheet this publication's parser covers does not have the layout the parser expects."""


# ---------------------------------------------------------------------------
# Header cells and labels
# ---------------------------------------------------------------------------

_YEAR_TEXT = re.compile(r"(\d{4})(?:\.0)?\**")
_ROMAN_QUARTER = {"I": "Q1", "II": "Q2", "III": "Q3", "IV": "Q4"}


def parse_year(value) -> Optional[int]:
    """A header cell's year (2026, 2026.0, '2026', '2026*', a date cell), else None."""
    if value is None or isinstance(value, bool):
        return None
    if isinstance(value, (_dt.datetime, _dt.date)):
        year = value.year
    elif isinstance(value, (int, float)):
        if not float(value).is_integer():
            return None
        year = int(value)
    elif isinstance(value, str):
        match = _YEAR_TEXT.fullmatch(value.strip())
        if not match:
            return None
        year = int(match.group(1))
    else:
        return None
    return year if 1950 <= year <= 2100 else None


def parse_period(value) -> Optional[str]:
    """A period-row cell as 'Jan'..'Dec' or 'Q1'..'Q4' (Indonesian/English, stars dropped)."""
    if not isinstance(value, str):
        return None
    text = value.strip().rstrip("*").strip()
    match = re.fullmatch(r"Q(I{1,3}|IV)", text, re.IGNORECASE)
    if match:
        return _ROMAN_QUARTER[match.group(1).upper()]
    return _bare_period_token(text) if text else None


def parse_year_period(value) -> Optional[Tuple[int, str]]:
    """A combined header cell ('Q1-2013', 'Q2-2026**', 'Jul 2026') as (year, period)."""
    if not isinstance(value, str):
        return None
    return _parse_period(value.strip().rstrip("*").strip())


# Outline markers at the start of a label: 'I.' / 'V.1.' (roman, optionally with a number),
# 'A.' / 'a.' (a single letter — never the first letter of an acronym such as 'A.D.B'),
# '1.' / '2.1.1.' (numbers), '-' (dash items).
_LEADING_NUMBERING = re.compile(
    r"(?:[IVX]+\.(?:\d+\.)*(?=\s|$)|[A-Za-z]\.(?=\s|$)|\d+(?:\.\d+)*\.?(?=\s|$)|-+)\s*"
)
_FORMULA_SUFFIX = re.compile(r"\s*\(\s*(?:[\dIVX]+\s*[+-]\s*)+[\dIVX]+\s*\)$")
_BILINGUAL_SPLIT = re.compile(r"\s+/\s*|\s*/\s+")
_FOOTNOTE_SUFFIX = re.compile(r"\s+\d[)\]]$")
# Stars mark preliminary figures; ¹²³ and U+FFFD are footnote superscripts (U+FFFD where the
# file lost them).
_TRAILING_MARKS = re.compile(r"[\s*\u00b9\u00b2\u00b3\ufffd]+$")


def clean_label(raw) -> str:
    """The Indonesian name in a label cell: numbering, English half, formula refs and stars off.

    'I. Transaksi Berjalan' -> 'Transaksi Berjalan', '2.1.1. Bank / Bank' -> 'Bank',
    'TOTAL (1+2)' -> 'TOTAL'. A cell holding only numbering ('1.', a bare row number) -> ''.
    """
    if raw is None or isinstance(raw, (bool, int, float)):
        return ""
    text = _BILINGUAL_SPLIT.split(" ".join(str(raw).split()))[0]
    while True:
        match = _LEADING_NUMBERING.match(text)
        if not match:
            break
        text = text[match.end():]
    text = _TRAILING_MARKS.sub("", text)
    return _FOOTNOTE_SUFFIX.sub("", _FORMULA_SUFFIX.sub("", text)).strip()


_NUMBERING_TOKEN = re.compile(r"(\d+(?:\.\d+)*)\.?(?=\s|$)")


def numbering_depth(raw) -> int:
    """How deep a label's own numbering goes: '1.' -> 1, '1.1' -> 2, '2.1.1. Bank' -> 3."""
    if isinstance(raw, bool):
        return 0
    if isinstance(raw, (int, float)):
        return 1
    if not isinstance(raw, str):
        return 0
    match = _NUMBERING_TOKEN.match(" ".join(raw.split()))
    return len(match.group(1).split(".")) if match else 0


# ---------------------------------------------------------------------------
# Reading the workbook
# ---------------------------------------------------------------------------

_WORKBOOK_CACHE: "OrderedDict[str, Dict[str, List[List]]]" = OrderedDict()
_WORKBOOK_CACHE_SIZE = 4


def load_grid(data: bytes, sheet_name: str) -> List[List]:
    """The sheet's cell values with merged ranges filled; each workbook is opened once.

    openpyxl has to load a whole .xlsx to see its merged ranges — about 9 seconds for a SULNI
    workbook — while turning a loaded sheet into a grid is cheap. So the first request for a
    workbook converts every sheet, and later sheets of that workbook (and the cell-pointer grid
    paired_verifier reads after parsing) come from memory. Rows are copied on the way out so a
    caller that edits its grid cannot change the cached one.
    """
    digest = hashlib.sha1(data).hexdigest()
    sheets = _WORKBOOK_CACHE.get(digest)
    if sheets is None:
        sheets = load_workbook_grids(data)
        _WORKBOOK_CACHE[digest] = sheets
        if len(_WORKBOOK_CACHE) > _WORKBOOK_CACHE_SIZE:
            _WORKBOOK_CACHE.popitem(last=False)
    else:
        _WORKBOOK_CACHE.move_to_end(digest)
    if sheet_name not in sheets:
        raise ValueError(f"Sheet '{sheet_name}' not found. Available: {list(sheets)}")
    return [list(row) for row in sheets[sheet_name]]


def load_label_indents(data: bytes, sheet_name: str, col: int) -> Dict[int, int]:
    """Row -> indent level of the label cell in `col` (.xls only: where SEKI keeps hierarchy)."""
    if data[:8] != _XLS_MAGIC:
        raise PublicationParseError("indentasi label hanya terbaca dari file .xls")
    import xlrd

    try:
        wb = xlrd.open_workbook(file_contents=data, formatting_info=True)
        ws = wb.sheet_by_name(sheet_name)
    except Exception as exc:
        raise PublicationParseError(f"indentasi label tidak terbaca: {exc}") from exc
    if col >= ws.ncols:
        return {}
    return {
        r: int(wb.xf_list[ws.cell_xf_index(r, col)].alignment.indent_level or 0)
        for r in range(ws.nrows)
    }
