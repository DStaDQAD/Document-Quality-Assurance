"""The uploaded tables as the strict extractor sees them: a code per table, its shape, its rows.

Never the values: with the numbers in view the model can hunt for the row whose value matches
the printed number, which hides exactly the report errors the check exists to find.
"""
import re
from dataclasses import dataclass
from typing import List, Optional, Tuple

from paired_verifier import _ExcelSource, _period_ordinal
from structured_extractor import LABELS_PER_SOURCE, _labels_for_chunk
from table_parser_generic import _MONTH_ABBREVS

_QUARTERS = ("Q1", "Q2", "Q3", "Q4")
_SEMESTERS = ("S1", "S2")
_ROMAN = {"I": 1, "II": 2, "III": 3, "IV": 4}
# Indonesian and English month names and their abbreviations, lower case, to the table's token.
_MONTH_NAMES = {
    "jan": "Jan", "januari": "Jan", "january": "Jan",
    "feb": "Feb", "februari": "Feb", "february": "Feb",
    "mar": "Mar", "maret": "Mar", "march": "Mar",
    "apr": "Apr", "april": "Apr",
    "mei": "May", "may": "May",
    "jun": "Jun", "juni": "Jun", "june": "Jun",
    "jul": "Jul", "juli": "Jul", "july": "Jul",
    "agu": "Aug", "agt": "Aug", "ags": "Aug", "aug": "Aug", "agustus": "Aug", "august": "Aug",
    "sep": "Sep", "sept": "Sep", "september": "Sep",
    "okt": "Oct", "oct": "Oct", "oktober": "Oct", "october": "Oct",
    "nov": "Nov", "nopember": "Nov", "november": "Nov",
    "des": "Dec", "dec": "Dec", "desember": "Dec", "december": "Dec",
}
_QUARTER_TOKEN = re.compile(r"^q([1-4])[\s\-./]*(\d{4})$")
_SEMESTER_TOKEN = re.compile(r"^s([12])[\s\-./]*(\d{4})$")
_ROMAN_PERIOD = re.compile(r"^(triwulan|tw|semester|smt)\.?\s*(iv|iii|ii|i)[\s\-./]*(\d{4})$")
_MONTH_PERIOD = re.compile(r"^([a-z]+)\.?[\s\-./']*(\d{4})$")


@dataclass
class TableView:
    """One uploaded time-series table under the code the extractor cites it by."""
    code: str
    source: _ExcelSource

    @property
    def table(self):
        return self.source.table

    @property
    def shape(self) -> str:
        """'per triwulan, Q1 2015 s.d. Q4 2025, satuan Juta USD'."""
        periods = sorted({key[1:] for key in self.table._data if len(key) == 3 and isinstance(key[1], int)},
                         key=lambda p: (p[0], _period_ordinal(p[1])))
        if not periods:
            return ""
        tokens = {month for _, month in periods}
        if tokens <= set(_QUARTERS):
            axis = "per triwulan"
        elif tokens <= set(_MONTH_ABBREVS):
            axis = "per bulan"
        elif tokens <= set(_SEMESTERS):
            axis = "per semester"
        else:
            axis = "periode campuran"
        (first_year, first), (last_year, last) = periods[0], periods[-1]
        shape = f"{axis}, {first} {first_year} s.d. {last} {last_year}"
        return f"{shape}, satuan {self.table.unit}" if self.table.unit else shape


def build_views(sources: List[_ExcelSource]) -> List[TableView]:
    """The uploaded Excel time-series tables that hold data, coded S1, S2, … in upload order."""
    usable = [s for s in sources
              if s.origin == "excel" and not s.pointer_only and s.table.axis_type == "temporal"
              and s.table._data]
    return [TableView(code=f"S{i}", source=s) for i, s in enumerate(usable, 1)]


def period_header(year: int, month: str) -> str:
    """The period as the extractor is shown it and cites it: 'Q4 2025', 'Jun 2026'."""
    return f"{month} {year}"


def parse_period(text: str) -> Optional[Tuple[int, str]]:
    """A period however the model writes it ('Triwulan IV 2025', 'Desember 2025', 'Q4 2025')
    as the table's key (2025, 'Q4') / (2025, 'Dec'); None when it names no single period."""
    t = " ".join((text or "").lower().split())
    match = _QUARTER_TOKEN.match(t)
    if match:
        return int(match.group(2)), f"Q{match.group(1)}"
    match = _SEMESTER_TOKEN.match(t)
    if match:
        return int(match.group(2)), f"S{match.group(1)}"
    match = _ROMAN_PERIOD.match(t)
    if match:
        number, year = _ROMAN[match.group(2).upper()], int(match.group(3))
        if match.group(1) in ("semester", "smt"):
            return (year, f"S{number}") if number <= 2 else None
        return year, f"Q{number}"
    match = _MONTH_PERIOD.match(t)
    if match and match.group(1) in _MONTH_NAMES:
        return int(match.group(2)), _MONTH_NAMES[match.group(1)]
    return None


def _fold(text: str) -> str:
    return " ".join((text or "").split()).lower()


def find_row(view: TableView, text: str) -> Optional[str]:
    """The row whose name is `text` up to case and spacing. Never a word match: a name that is
    not one of the table's rows finds nothing."""
    wanted = _fold(text)
    return next((row for row in view.table.row_labels if _fold(row) == wanted), None)


def table_block(views: List[TableView], chunk: str) -> str:
    """The TABLES section of one chunk's prompt: per table its code, title and shape, the rows
    whose unit differs from the table's, and its row names (the ones the chunk is about when a
    table has more than LABELS_PER_SOURCE). No values."""
    headers = {}
    groups = []
    for view in views:
        header = f"[{view.code}: {view.table.title} — {view.shape}]"
        headers[header] = view
        groups.append((header, list(view.table.row_labels)))
    parts: List[str] = []
    for header, rows in _labels_for_chunk(groups, chunk, LABELS_PER_SOURCE) or []:
        parts.append(header)
        for row, unit in (headers[header].table.row_units or {}).items():
            parts.append(f"  Satuan khusus: '{row}' = {unit}")
        parts.extend(f"  - {row}" for row in rows)
    return "\n".join(parts)
