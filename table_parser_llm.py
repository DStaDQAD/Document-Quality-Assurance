"""Tier 3 of the parsing cascade: LLM structure-mapping for tables the heuristics can't read.

When both the BI parser (tier 1) and the generic heuristic parser (tier 2) fail, this module
shows the LLM a textual SNAPSHOT of the sheet grid (cell coordinates + values) and asks it to
return a mapping SPEC: which row is the header, which column holds row labels, and what each
data column means (a calendar period or an attribute name). The spec is then VALIDATED against
the grid and every value is extracted deterministically by code.

The project-wide hallucination-avoidance principle holds: the LLM never passes a numeric VALUE
through — a wrong spec can only make the parse fail validation or bind a column to the wrong
meaning (visible in the reasoning strings), never invent a number.

Layout fingerprint cache: the structural signature of a sheet (pattern of text/numeric/empty
cells in the top rows) keys a cache of validated specs, so repeat uploads of the same layout
(e.g. the monthly refresh of one report) parse deterministically without another LLM call.
The cache is in-process only — a restart re-derives specs on first sight.
"""

import hashlib
import logging
from typing import Dict, List, Literal, Optional

from langchain_core.language_models import BaseChatModel
from langchain_core.prompts import ChatPromptTemplate
from pydantic import BaseModel, Field

from table_model import TableData
from table_parser_generic import _MONTH_ABBREVS, _is_empty, _is_number, _load_grid

logger = logging.getLogger("fact-checker")

_SNAPSHOT_ROWS = 20   # rows shown to the LLM
_SNAPSHOT_COLS = 30   # columns shown to the LLM
_CELL_TEXT_MAX = 40   # truncate long cell strings in the snapshot


# ---------------------------------------------------------------------------
# Mapping-spec schema (LLM output — structure only, never data values)
# ---------------------------------------------------------------------------

class _ColumnSpec(BaseModel):
    col: int = Field(..., description="0-based column index in the sheet.")
    kind: Literal["period", "attribute"] = Field(
        ...,
        description=(
            "'period' when the column holds values of one calendar month (fill year+month); "
            "'attribute' when it holds a non-time attribute such as a price or stock count "
            "(fill name). Do NOT list columns that hold neither (e.g. notes)."
        ),
    )
    year: Optional[int] = Field(None, description="Period columns only: the year, e.g. 2026.")
    month: Optional[str] = Field(
        None,
        description="Period columns only: 3-letter English month abbreviation (Jan..Dec).",
    )
    name: Optional[str] = Field(
        None, description="Attribute columns only: the column's name as written in the sheet."
    )


class _TableSpec(BaseModel):
    """Structural map of one sheet. All indices are 0-based."""
    header_row: int = Field(..., description="0-based index of the (last) header row.")
    label_col: int = Field(..., description="0-based index of the column holding row labels.")
    data_start_row: int = Field(..., description="0-based index of the first data row.")
    axis_type: Literal["temporal", "categorical"] = Field(
        ...,
        description="'temporal' when the data columns are calendar periods, else 'categorical'.",
    )
    title: Optional[str] = Field(None, description="The table's title text, if visible.")
    unit: Optional[str] = Field(
        None, description="The measurement unit annotation, e.g. 'Miliar Rp', if visible."
    )
    columns: List[_ColumnSpec] = Field(..., description="Every DATA column, in sheet order.")


_SYSTEM_PROMPT = """\
You are a precise spreadsheet-structure mapper. You are given a textual snapshot of a
spreadsheet grid: one line per row, each cell shown as [c]=value with its 0-based column
index c. Long cells are truncated; empty cells are omitted.

Your task is to return the table's STRUCTURE as a mapping spec — NOT its data:
  - header_row     : the 0-based row index of the header row (the last one, if stacked)
  - label_col      : the 0-based column index whose cells name each data row
  - data_start_row : the 0-based row index where data rows begin
  - axis_type      : 'temporal' if the data columns are calendar months, else 'categorical'
  - title / unit   : the table title and measurement-unit annotation if visible (else null)
  - columns        : EVERY data column with its meaning:
        period columns    -> kind='period', with year (e.g. 2026) and month as a 3-letter
                             English abbreviation (Jan Feb Mar Apr May Jun Jul Aug Sep Oct
                             Nov Dec). Indonesian months: Mei=May, Agu/Agustus=Aug, Okt=Oct,
                             Des=Dec, Peb/Feb=Feb, Mar=Mar, Jan=Jan, Jun=Jun, Jul=Jul.
        attribute columns -> kind='attribute', with name copied from the sheet.

Rules:
1. NEVER copy numeric data values into the spec — only structure (indices, period identities,
   attribute names).
2. Skip columns that hold row numbers, notes, or free text: list only real data columns.
3. If year headers appear on a different row than month headers, combine them per column.
4. label_col must not be listed in columns.
"""

_HUMAN_TEMPLATE = """\
Sheet has {n_rows} rows and {n_cols} columns. Snapshot of the first {shown_rows} rows:

{snapshot}
"""

_MAPPING_PROMPT = ChatPromptTemplate.from_messages([
    ("system", _SYSTEM_PROMPT),
    ("human", _HUMAN_TEMPLATE),
])


# ---------------------------------------------------------------------------
# Snapshot + fingerprint
# ---------------------------------------------------------------------------

def _grid_snapshot(grid: List[List]) -> str:
    lines = []
    for r, row in enumerate(grid[:_SNAPSHOT_ROWS]):
        cells = []
        for c, v in enumerate(row[:_SNAPSHOT_COLS]):
            if _is_empty(v):
                continue
            text = str(v)
            if len(text) > _CELL_TEXT_MAX:
                text = text[:_CELL_TEXT_MAX] + "…"
            cells.append(f"[{c}]={text!r}")
        lines.append(f"row {r}: " + (" ".join(cells) if cells else "(empty)"))
    return "\n".join(lines)


def _layout_fingerprint(grid: List[List]) -> str:
    """Structural signature: the text/number/empty pattern of the top rows + column count.

    Data VALUES are deliberately excluded so the same layout with refreshed numbers
    (monthly update of one report) hits the cache.
    """
    sig_rows = []
    for row in grid[:_SNAPSHOT_ROWS]:
        sig_rows.append("".join(
            "n" if _is_number(v) else ("t" if not _is_empty(v) else ".")
            for v in row[:_SNAPSHOT_COLS]
        ))
    n_cols = max((len(r) for r in grid), default=0)
    return hashlib.sha256(("|".join(sig_rows) + f"#{n_cols}").encode()).hexdigest()


# Validated specs per layout fingerprint (in-process; see module docstring).
_SPEC_CACHE: Dict[str, _TableSpec] = {}


# ---------------------------------------------------------------------------
# Spec validation (grid-dependent — a cached spec is re-validated per file)
# ---------------------------------------------------------------------------

def _validate_spec(spec: _TableSpec, grid: List[List]) -> None:
    """Raise ValueError when the spec cannot be a correct map of this grid."""
    n_rows = len(grid)
    n_cols = max((len(r) for r in grid), default=0)

    if not (0 <= spec.header_row < spec.data_start_row <= n_rows - 1):
        raise ValueError(
            f"Spec row indices out of order/range: header_row={spec.header_row}, "
            f"data_start_row={spec.data_start_row}, sheet has {n_rows} rows."
        )
    if not (0 <= spec.label_col < n_cols):
        raise ValueError(f"label_col={spec.label_col} outside the sheet's {n_cols} columns.")
    if not spec.columns:
        raise ValueError("Spec lists no data columns.")

    seen_cols = set()
    for col_spec in spec.columns:
        if not (0 <= col_spec.col < n_cols):
            raise ValueError(f"Column index {col_spec.col} outside the sheet's {n_cols} columns.")
        if col_spec.col == spec.label_col or col_spec.col in seen_cols:
            raise ValueError(f"Column index {col_spec.col} duplicated or equal to label_col.")
        seen_cols.add(col_spec.col)
        if col_spec.kind == "period":
            if spec.axis_type != "temporal":
                raise ValueError("Period column in a categorical spec.")
            if col_spec.year is None or not (1990 <= col_spec.year <= 2100):
                raise ValueError(f"Period column {col_spec.col} has invalid year {col_spec.year}.")
            if col_spec.month not in _MONTH_ABBREVS:
                raise ValueError(f"Period column {col_spec.col} has invalid month {col_spec.month!r}.")
        else:
            if spec.axis_type != "categorical":
                raise ValueError("Attribute column in a temporal spec.")
            if not (col_spec.name or "").strip():
                raise ValueError(f"Attribute column {col_spec.col} has no name.")

    # Plausibility against the actual data region: the label column must name at least one
    # row and the mapped columns must hold at least one numeric value.
    data_rows = grid[spec.data_start_row:]
    has_label = any(
        spec.label_col < len(row) and not _is_empty(row[spec.label_col]) for row in data_rows
    )
    if not has_label:
        raise ValueError("Label column is empty in the data region.")
    has_value = any(
        c < len(row) and _is_number(row[c])
        for row in data_rows for c in seen_cols
    )
    if not has_value:
        raise ValueError("Mapped data columns hold no numeric values in the data region.")


# ---------------------------------------------------------------------------
# Deterministic extraction from a validated spec
# ---------------------------------------------------------------------------

def _extract(spec: _TableSpec, grid: List[List]) -> TableData:
    col_keys: Dict[int, tuple] = {}
    col_labels: List[str] = []
    for col_spec in spec.columns:
        if col_spec.kind == "period":
            col_keys[col_spec.col] = (col_spec.year, col_spec.month)
        else:
            name = col_spec.name.strip()
            if name not in col_labels:
                col_labels.append(name)
                col_keys[col_spec.col] = (name,)

    table_data: Dict[tuple, float] = {}
    row_labels: List[str] = []
    for row in grid[spec.data_start_row:]:
        if spec.label_col >= len(row) or _is_empty(row[spec.label_col]):
            continue
        label = str(row[spec.label_col]).strip()
        stored = False
        for c, key_part in col_keys.items():
            if c < len(row) and _is_number(row[c]):
                table_data.setdefault((label, *key_part), float(row[c]))
                stored = True
        if stored and label not in row_labels:
            row_labels.append(label)

    if not row_labels or not table_data:
        raise ValueError("Spec extracted no labelled numeric rows from the sheet.")

    return TableData(
        title=(spec.title or "").strip(),
        unit=(spec.unit or "").strip(),
        row_labels=row_labels,
        col_labels=col_labels,
        axis_type=spec.axis_type,
        _data=table_data,
    )


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------

def parse_table_with_llm(data: bytes, sheet_name: str, llm: BaseChatModel) -> TableData:
    """Parse a sheet by asking the LLM for a structure spec, then extracting values in code.

    Raises ValueError when the LLM's spec fails validation against the grid (or the LLM call
    itself fails) — callers treat this like any other parser failure.
    """
    grid = _load_grid(data, sheet_name)
    fingerprint = _layout_fingerprint(grid)

    spec = _SPEC_CACHE.get(fingerprint)
    if spec is not None:
        try:
            _validate_spec(spec, grid)
            logger.info("LLM table spec served from layout cache (%s…)", fingerprint[:12])
            return _extract(spec, grid)
        except ValueError:
            # Same fingerprint, incompatible grid (rare) — drop and re-derive.
            _SPEC_CACHE.pop(fingerprint, None)
            spec = None

    chain = _MAPPING_PROMPT | llm.with_structured_output(_TableSpec)
    n_cols = max((len(r) for r in grid), default=0)
    try:
        spec = chain.invoke({
            "n_rows": len(grid),
            "n_cols": n_cols,
            "shown_rows": min(_SNAPSHOT_ROWS, len(grid)),
            "snapshot": _grid_snapshot(grid),
        })
    except Exception as exc:
        raise ValueError(f"LLM structure mapping failed: {exc}") from exc

    _validate_spec(spec, grid)
    result = _extract(spec, grid)
    _SPEC_CACHE[fingerprint] = spec
    logger.info(
        "LLM table spec derived and cached (%s…): axis=%s, %d columns",
        fingerprint[:12], spec.axis_type, len(spec.columns),
    )
    return result
