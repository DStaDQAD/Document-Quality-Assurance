"""Tinjauan AI — a second look at the claims the code left Tidak Sesuai or Tidak Cukup Data.

The code path (label matching, then arithmetic) is exact but brittle: a claim worded unlike any
row stays Tidak Cukup Data, and a breakdown matched to its total comes back Tidak Sesuai. Here a
model reads the claim sentence next to the parsed tables and names the cells that answer it. It
never supplies a verdict on its own: code checks every cited cell exists with the cited value,
rebuilds the claim from those cells and runs the ordinary engine (_evaluate_fact) on a copy of
the sheet cut down to the cited rows. What the model may change is limited by how far each kind
of answer can be proved (spike 2026-10-12; spec docs/superpowers/specs/2026-10-12-ai-review-design.md):

- → Sesuai: the engine reproduces the claimed number from the cited cells. Accepted.
- Tidak Sesuai → Tidak Cukup Data: the model says the table does not hold what the sentence is
  about. Accepted.
- Tidak Cukup Data → Tidak Sesuai: a mismatch is easy to get from the wrong row, so by default the
  verdict stays and the claim is only flagged "perlu ditinjau".
"""

import logging
from typing import Dict, List, Optional, Tuple

from pydantic import BaseModel, Field

from excel_parser_bi import BITableData
from paired_verifier import _ExcelSource, _period_ordinal

logger = logging.getLogger(__name__)

# The model sees every row but only the most recent years: a report talks about the latest
# period and the same period a year before. Cutting columns rather than rows is what keeps the
# prompt small without the row-pruning misses the cell pointer suffers from.
YEARS_SHOWN = 2
# Claims per call. The tables are re-sent with every batch, so bigger batches are cheaper.
BATCH_SIZE = 30

# Origins whose cells are real reference data. Chart labels are the report checking itself, and
# another uploaded PDF is only ever a fallback in the code path; neither is offered here.
_REVIEWABLE_ORIGINS = ("excel", "pdf")


class CitedCell(BaseModel):
    sheet: str = Field(description="Kode sheet, mis. 'S1'")
    row: str = Field(description="Label baris persis seperti di kolom 'Baris'")
    period: str = Field(description="Header kolom persis, mis. 'Jun 2026' atau 'Q2 2026'")
    value: float = Field(description="Angka di sel itu, disalin persis")


def reviewable_sources(sources: List[_ExcelSource]) -> Dict[str, _ExcelSource]:
    """The sources the model may cite, under the short codes it cites them by."""
    usable = [
        s for s in sources
        if s.origin in _REVIEWABLE_ORIGINS and not s.pointer_only and s.table._data
    ]
    return {f"S{i}": s for i, s in enumerate(usable, 1)}


def _shown_columns(table: BITableData) -> List[Tuple[str, Tuple]]:
    """(header, key suffix) of each column shown: the last YEARS_SHOWN years, or every attribute
    column of a categorical table."""
    if table.axis_type == "categorical":
        return [(c, (c,)) for c in table.col_labels]
    periods = sorted({(k[1], k[2]) for k in table._data}, key=lambda p: (p[0], _period_ordinal(p[1])))
    if not periods:
        return []
    last = periods[-1][0]
    return [(f"{m} {y}", (y, m)) for y, m in periods if y > last - YEARS_SHOWN]


def _fmt(value: float) -> str:
    return str(round(value, 4))


def _norm(text: str) -> str:
    """Case, spacing and the markdown pipe (printed as '/') do not tell two labels apart."""
    return " ".join(str(text).replace("|", "/").split()).lower()


def table_markdown(code: str, src: _ExcelSource) -> str:
    table = src.table
    cols = _shown_columns(table)
    lines = [
        f"### {code} — sheet '{src.sheet}': {table.title} (satuan: {table.unit})",
        "| Baris | " + " | ".join(h for h, _ in cols) + " |",
        "|" + "---|" * (len(cols) + 1),
    ]
    for label in table.row_labels:
        cells = [table._data.get((label, *suffix)) for _, suffix in cols]
        if all(c is None for c in cells):
            continue
        shown = " | ".join("" if c is None else _fmt(c) for c in cells)
        lines.append(f"| {label.replace('|', '/')} | {shown} |")
    return "\n".join(lines)


def read_cells(
    cited: List[CitedCell], codes: Dict[str, _ExcelSource]
) -> Optional[Tuple[_ExcelSource, List[Tuple[str, Tuple, float]]]]:
    """The cited cells as (row label, key suffix, value), or None when any of them is not a
    shown cell holding the cited value. All cells must come from one sheet: the engine judges a
    claim against one source at a time."""
    if not cited:
        return None
    sheets = {c.sheet.strip().upper() for c in cited}
    if len(sheets) != 1:
        return None
    src = codes.get(sheets.pop())
    if src is None:
        return None
    rows = {_norm(label): label for label in src.table.row_labels}
    cols = {_norm(header): suffix for header, suffix in _shown_columns(src.table)}
    keyed = []
    for cell in cited:
        label, suffix = rows.get(_norm(cell.row)), cols.get(_norm(cell.period))
        if label is None or suffix is None:
            return None
        value = src.table._data.get((label, *suffix))
        if value is None or abs(value - cell.value) > max(0.01, abs(value) * 1e-4):
            return None
        keyed.append((label, suffix, value))
    return src, keyed
