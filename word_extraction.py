"""Read a Word (.docx) report: its narrative, and the tables it prints as pictures.

BI's Word reports do not hold their tables as Word tables. Every Tabel and Lampiran is an EMF
picture pasted from Excel, preceded by its caption paragraph. EMF is a vector format and keeps
each cell as a text record with coordinates, so the numbers are read out of the file by code, as
exactly as the PDF text layer and with none of its damage: no zero-width spaces inside words, no
narrative column interleaved with the table. The picture's text is rebuilt into (y, line) rows and
handed to the same reader the PDF route uses (pdf_table_extraction._tables_on_page), so a Word
table is split, qualified and guarded exactly like a PDF one.

Word has no fixed pages. The narrative is still cut into `[== Halaman N ==]` blocks — at explicit
page breaks and at every appendix caption — because the narrative filter works per block and
drops appendix blocks by their title. Those block numbers are not page numbers; main clears the
page numbers of a Word document's results.

Standard library only: the deployment image carries no Word toolkit.
"""

import io
import logging
import re
import statistics
import struct
import zipfile
import xml.etree.ElementTree as ET
from dataclasses import dataclass, field
from typing import Dict, List, Optional, Tuple

from pdf_table_extraction import _NUM_CELL_RE, PdfTable, _line_periods, _tables_on_page
from structured_extractor import detect_number_format

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# EMF: text records → table lines
# ---------------------------------------------------------------------------

_EMR_HEADER = 1
_EMR_EXTTEXTOUTW = 84
_EMF_SIGNATURE = b" EMF"
_EXTTEXTOUT_FIXED_BYTES = 76

# A label line within this fraction of the row pitch of a value row belongs to that row: a wrapped
# label sits 1-13 units from its values against a pitch of ~37 in the April report's tables,
# while a section band ("a.l:" headings) sits a whole pitch away and stays its own line.
_ATTACH_FRACTION = 0.5
_MIN_ROW_NUMBERS = 2


@dataclass(frozen=True)
class EmfText:
    x: int
    y: int
    text: str


def emf_text_runs(data: bytes) -> List[EmfText]:
    """Every EMR_EXTTEXTOUTW record's text and reference point, in file order."""
    if (len(data) < 44 or struct.unpack_from("<I", data, 0)[0] != _EMR_HEADER
            or data[40:44] != _EMF_SIGNATURE):
        return []
    runs: List[EmfText] = []
    offset = 0
    while offset + 8 <= len(data):
        record_type, size = struct.unpack_from("<II", data, offset)
        if size < 8 or offset + size > len(data):
            break
        if record_type == _EMR_EXTTEXTOUTW and size >= _EXTTEXTOUT_FIXED_BYTES:
            x, y = struct.unpack_from("<ii", data, offset + 36)
            n_chars, off_string = struct.unpack_from("<II", data, offset + 44)
            start = offset + off_string
            end = start + 2 * n_chars
            if end <= offset + size:
                text = data[start:end].decode("utf-16-le", "replace").strip()
                if text:
                    runs.append(EmfText(x, y, text))
        offset += size
    return runs


def _is_number(text: str) -> bool:
    return bool(_NUM_CELL_RE.match(text))


def _line_text(runs: List[EmfText]) -> str:
    return " ".join(r.text for r in sorted(runs, key=lambda r: r.x))


def emf_table_lines(runs: List[EmfText]) -> List[Tuple[float, str]]:
    """(y, line) per visual row, top first, y negated to the PDF convention (larger = higher)."""
    by_y: Dict[int, List[EmfText]] = {}
    for run in runs:
        by_y.setdefault(run.y, []).append(run)
    ys = sorted(by_y)                      # EMF y grows downward

    header_y = next((y for y in ys if _line_periods(_line_text(by_y[y])) is not None), None)
    value_rows = [y for y in ys if header_y is not None and y > header_y
                  and sum(_is_number(r.text) for r in by_y[y]) >= _MIN_ROW_NUMBERS]
    gaps = [b - a for a, b in zip(value_rows, value_rows[1:])]
    pitch = statistics.median(gaps) if gaps else 0

    owner = {y: y for y in ys}
    if value_rows and pitch:
        value_set = set(value_rows)
        for y in ys:
            if y <= header_y or y in value_set:
                continue
            nearest = min(value_rows, key=lambda row: abs(row - y))
            if abs(nearest - y) <= _ATTACH_FRACTION * pitch:
                owner[y] = nearest

    groups: Dict[int, List[EmfText]] = {}
    for y in ys:
        groups.setdefault(owner[y], []).extend(by_y[y])

    lines: List[Tuple[float, str]] = []
    for y in sorted(groups):
        by_x = sorted(groups[y], key=lambda r: r.x)
        n_values = 0
        for run in reversed(by_x):
            if not _is_number(run.text):
                break
            n_values += 1
        values = by_x[len(by_x) - n_values:]
        # A label split over lines reads top to bottom, then left to right.
        label = sorted(by_x[:len(by_x) - n_values], key=lambda r: (r.y, r.x))
        lines.append((-float(y), " ".join(r.text for r in label + values)))
    return lines
