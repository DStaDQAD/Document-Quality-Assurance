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
from table_parser_generic import (
    _MONTH_ABBREVS,
    _QUARTER_CANON,
    _is_empty,
    _is_number,
    _load_grid,
)

# Valid period tokens for a column spec: months plus quarter tokens.
_PERIOD_TOKENS = frozenset(_MONTH_ABBREVS) | frozenset(_QUARTER_CANON)

logger = logging.getLogger("fact-checker")

# _grid_snapshot defaults — the cell pointer passes its own caps (cell_pointer.py).
_SNAPSHOT_ROWS = 20
_SNAPSHOT_COLS = 30
_CELL_TEXT_MAX = 40

# What the structure mapper sees (_structure_snapshot). A BI sheet runs years to the right, so
# its first 30 columns are long-gone periods: on the 2026-10-13 spike the old 20 x 30 window
# found 0 of 28 golden values. Every row is shown, with the label columns on the left and the
# newest periods on the right; the middle is elided.
_STRUCTURE_ROWS = 300
_STRUCTURE_LEFT_COLS = 8
_STRUCTURE_RIGHT_COLS = 40
_STRUCTURE_CELL_TEXT_MAX = 60


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
        description=(
            "Period columns only: 3-letter English month abbreviation (Jan..Dec), or a "
            "quarter token Q1..Q4 when the column is a calendar quarter."
        ),
    )
    name: Optional[str] = Field(
        None, description="Attribute columns only: the column's name as written in the sheet."
    )


class _TableSpec(BaseModel):
    """Structural map of one sheet. All indices are 0-based."""
    label_cols: List[int] = Field(
        ...,
        description=(
            "Columns holding row names, left to right. A row's name is its RIGHTMOST non-empty "
            "cell among them; a cell further left is its group."
        ),
    )
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
You are a precise spreadsheet-structure mapper for statistical tables. You are given a textual
snapshot of a spreadsheet grid: one line per row, each cell shown as [c]=value with its 0-based
column index c. Long cells are truncated; empty cells are omitted. On a wide sheet only the
leftmost and the rightmost (newest) columns are shown; '…' marks the skipped middle.

Your task is to return the table's STRUCTURE as a mapping spec — NOT its data:
  - label_cols     : the columns whose cells name the rows, left to right. Use MORE THAN ONE
                     when names are spread over several columns: a total in column 0 with its
                     components indented in column 1, or a group column next to a detail
                     column. A row's name is its RIGHTMOST non-empty cell among label_cols; a
                     cell further left is read as its group. Leave out an English-translation
                     column when an Indonesian one names the same rows.
  - data_start_row : the 0-based row index where data rows begin
  - axis_type      : 'temporal' if the data columns are calendar periods, else 'categorical'
  - title / unit   : the table title and measurement-unit annotation if visible (else null)
  - columns        : every SHOWN data column with its meaning:
        period columns    -> kind='period', with year (e.g. 2026) and month as a 3-letter
                             English abbreviation (Jan Feb Mar Apr May Jun Jul Aug Sep Oct
                             Nov Dec), or Q1..Q4 for a quarter. Indonesian months: Mei=May,
                             Agu/Agustus=Aug, Okt=Oct, Des=Dec, Peb/Feb=Feb.
        attribute columns -> kind='attribute', with name copied from the sheet.

Rules:
1. NEVER copy numeric data values into the spec — only structure (indices, period identities,
   attribute names).
2. Skip columns that hold row numbers, units, notes, dates as text, or free text.
3. A column's period need not be written in its own header. If year headers sit on a different
   row than month headers, combine them per column. Quarter headers 'I','II','III','IV' (or
   Tw/Triwulan/Kuartal forms) under a year row are periods: month='Q1'..'Q4'. Relative headers
   ("Latest Data", "Latest Data -1", "t", "t-1", "Data Terakhir") are periods too: read the
   date written elsewhere (e.g. a "Date of Latest Observation" cell holding "September/26")
   and map "Latest Data" to September 2026 and "Latest Data -1" to the month before.
4. No column may be both in label_cols and in columns.
"""

_HUMAN_TEMPLATE = """\
Sheet has {n_rows} rows and {n_cols} columns.

{snapshot}
"""

_MAPPING_PROMPT = ChatPromptTemplate.from_messages([
    ("system", _SYSTEM_PROMPT),
    ("human", _HUMAN_TEMPLATE),
])


# ---------------------------------------------------------------------------
# Snapshot + fingerprint
# ---------------------------------------------------------------------------

def _grid_snapshot(
    grid: List[List],
    max_rows: int = _SNAPSHOT_ROWS,
    max_cols: int = _SNAPSHOT_COLS,
    cell_text_max: int = _CELL_TEXT_MAX,
) -> str:
    """Textual snapshot of the grid's top-left max_rows x max_cols region.

    Defaults preserve the tier-3 structure-mapping behaviour; the cell-pointer tier
    passes larger caps because the answer cells (newest periods) sit far right.
    """
    lines = []
    for r, row in enumerate(grid[:max_rows]):
        cells = []
        for c, v in enumerate(row[:max_cols]):
            if _is_empty(v):
                continue
            text = str(v)
            if len(text) > cell_text_max:
                text = text[:cell_text_max] + "…"
            cells.append(f"[{c}]={text!r}")
        lines.append(f"row {r}: " + (" ".join(cells) if cells else "(empty)"))
    return "\n".join(lines)


def _structure_snapshot(grid: List[List]) -> str:
    """What the structure mapper sees: every row (up to _STRUCTURE_ROWS), the leftmost
    _STRUCTURE_LEFT_COLS columns (where the names are) and the rightmost _STRUCTURE_RIGHT_COLS
    (the newest periods), with '…' where columns were skipped."""
    n_cols = max((len(r) for r in grid), default=0)
    keep = sorted(set(range(min(_STRUCTURE_LEFT_COLS, n_cols)))
                  | set(range(max(0, n_cols - _STRUCTURE_RIGHT_COLS), n_cols)))
    lines = []
    for r, row in enumerate(grid[:_STRUCTURE_ROWS]):
        cells, prev = [], None
        for c in keep:
            if c >= len(row) or _is_empty(row[c]):
                continue
            if prev is not None and any(k not in keep for k in range(prev + 1, c)):
                cells.append("…")
            text = str(row[c])
            if len(text) > _STRUCTURE_CELL_TEXT_MAX:
                text = text[:_STRUCTURE_CELL_TEXT_MAX] + "…"
            cells.append(f"[{c}]={text!r}")
            prev = c
        lines.append(f"row {r}: " + (" ".join(cells) if cells else "(empty)"))
    if len(grid) > _STRUCTURE_ROWS:
        lines.append(f"… ({len(grid) - _STRUCTURE_ROWS} more rows not shown)")
    return "\n".join(lines)


def _layout_fingerprint(grid: List[List]) -> str:
    """Structural signature: the top rows' text, their pattern of numeric/empty cells, and the
    column count.

    Numbers are left out so the same layout with refreshed figures hits the cache, but TEXT is
    kept: a spec can now read a period off a date cell ("Latest Data" = the "September/26"
    written beside it), and next month's sheet, identical but for "October/26", must not be
    served September's map.
    """
    sig_rows = []
    for row in grid[:_SNAPSHOT_ROWS]:
        sig_rows.append("|".join(
            "n" if _is_number(v) else ("." if _is_empty(v) else str(v).strip())
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

    if not (0 <= spec.data_start_row <= n_rows - 1):
        raise ValueError(f"data_start_row={spec.data_start_row} outside the sheet's {n_rows} rows.")
    if not spec.label_cols:
        raise ValueError("Spec names no label column.")
    for lc in spec.label_cols:
        if not (0 <= lc < n_cols):
            raise ValueError(f"label column {lc} outside the sheet's {n_cols} columns.")
    if not spec.columns:
        raise ValueError("Spec lists no data columns.")

    seen_cols = set()
    for col_spec in spec.columns:
        if not (0 <= col_spec.col < n_cols):
            raise ValueError(f"Column index {col_spec.col} outside the sheet's {n_cols} columns.")
        if col_spec.col in spec.label_cols or col_spec.col in seen_cols:
            raise ValueError(f"Column index {col_spec.col} duplicated or also a label column.")
        seen_cols.add(col_spec.col)
        if col_spec.kind == "period":
            if spec.axis_type != "temporal":
                raise ValueError("Period column in a categorical spec.")
            if col_spec.year is None or not (1990 <= col_spec.year <= 2100):
                raise ValueError(f"Period column {col_spec.col} has invalid year {col_spec.year}.")
            if col_spec.month not in _PERIOD_TOKENS:
                raise ValueError(f"Period column {col_spec.col} has invalid month {col_spec.month!r}.")
        else:
            if spec.axis_type != "categorical":
                raise ValueError("Attribute column in a temporal spec.")
            if not (col_spec.name or "").strip():
                raise ValueError(f"Attribute column {col_spec.col} has no name.")

    # Plausibility against the actual data region: some label column must name a row and the
    # mapped columns must hold at least one numeric value.
    data_rows = grid[spec.data_start_row:]
    has_label = any(
        lc < len(row) and not _is_empty(row[lc]) for row in data_rows for lc in spec.label_cols
    )
    if not has_label:
        raise ValueError("Label columns are empty in the data region.")
    has_value = any(
        c < len(row) and _is_number(row[c])
        for row in data_rows for c in seen_cols
    )
    if not has_value:
        raise ValueError("Mapped data columns hold no numeric values in the data region.")


# ---------------------------------------------------------------------------
# Deterministic extraction from a validated spec
# ---------------------------------------------------------------------------

def _row_names(spec: _TableSpec, grid: List[List], first_data_col: int):
    """(name, group, row) of every data-region row that has a name.

    The name is the rightmost non-empty label cell and the group the one left of it (SBank:
    "Kredit Konsumsi" | "KPR/KPA"). A row with numbers but no label cell at all takes the
    leftmost text before the data (Cadev ORA: the total named in column 0 while the model
    named column 1, where the components sit).
    """
    for row in grid[spec.data_start_row:]:
        texts = [str(row[lc]).strip() for lc in spec.label_cols
                 if lc < len(row) and not _is_empty(row[lc])]
        if not texts:
            texts = [str(v).strip() for v in row[:first_data_col]
                     if not _is_empty(v) and not _is_number(v)][:1]
        if texts:
            yield texts[-1], (texts[-2] if len(texts) > 1 else None), row


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

    rows = []
    for name, group, row in _row_names(spec, grid, min(col_keys)):
        values = {key: float(row[c]) for c, key in col_keys.items()
                  if c < len(row) and _is_number(row[c])}
        rows.append((name, group, values))

    # A name used twice is qualified by its group — the label column to its left, else the
    # last row without numbers (a section heading) — as "Group > Name", the convention the
    # rest of the pipeline matches. A name STILL used twice is dropped: a row the table does
    # not name unambiguously is a missing row, never a guess.
    uses: Dict[str, int] = {}
    for name, _g, _v in rows:
        uses[name] = uses.get(name, 0) + 1
    named, section = [], None
    for name, group, values in rows:
        if not values:
            section = name
        parent = group or section
        named.append((f"{parent} > {name}" if uses[name] > 1 and parent and parent != name else name,
                      values))
    counts: Dict[str, int] = {}
    for name, values in named:
        if values:
            counts[name] = counts.get(name, 0) + 1

    table_data: Dict[tuple, float] = {}
    row_labels: List[str] = []
    for name, values in named:
        if not values or counts[name] > 1:
            continue
        for key, value in values.items():
            table_data[(name, *key)] = value
        row_labels.append(name)

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

    Thin bytes wrapper over parse_grid_with_llm.
    """
    return parse_grid_with_llm(_load_grid(data, sheet_name), llm)


def parse_grid_with_llm(grid: List[List], llm: BaseChatModel) -> TableData:
    """Parse an in-memory grid by asking the LLM for a structure spec, then extracting in code.

    Raises ValueError when the LLM's spec fails validation against the grid (or the LLM call
    itself fails) — callers treat this like any other parser failure.

    Takes a grid so non-spreadsheet sources (see `pdf_table_extraction`) reuse this tier too.
    The layout-fingerprint cache lives here rather than in the wrapper, so a recurring layout
    is mapped once regardless of which kind of source it came from.
    """
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
            "snapshot": _structure_snapshot(grid),
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
