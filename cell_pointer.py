"""Tier 4 of the lookup cascade: LLM CELL-POINTER resolution for unresolved claims.

When a claim's (metric, period) cannot be resolved against any parsed TableData —
including sheets whose structure defeats every parser — the LLM is shown a
coordinate-labelled snapshot of the raw grid plus a numbered list of queries, and asked
to answer each with the COORDINATE (row, col) of the data cell. Code then reads
grid[row][col].

The project-wide hallucination-avoidance invariant holds: the LLM only ever emits
coordinates, a found flag, and (optionally) the sheet's unit ANNOTATION TEXT — the same
kind of structural metadata as _TableSpec.unit in table_parser_llm. A wrong pointer can
bind a claim to the wrong cell (visible in the result's provenance: the cell reference
is reported), but it can never invent a number: `read_grid_cell` is the only place a
pointer becomes a value, and it reads the grid.

Batching mirrors typo_checker._escalate_to_llm: ONE structured-output call per sheet
carrying every pending query, results keyed back by query_index; any failure is
swallowed and the claims simply stay unresolved.
"""

import logging
from dataclasses import dataclass
from typing import Dict, List, Optional, Tuple

from langchain_core.language_models import BaseChatModel
from langchain_core.prompts import ChatPromptTemplate
from pydantic import BaseModel, Field

from table_parser_generic import _is_number
from table_parser_llm import _grid_snapshot

logger = logging.getLogger("fact-checker")

# Snapshot caps for the pointer prompt. Much larger than tier 3's structure-mapping
# window (20x30): the answer cells — the NEWEST periods a report cites — sit at the far
# right of wide time-series sheets, so a tight column cap would amputate exactly the
# region the queries target.
_POINTER_MAX_ROWS = 120
_POINTER_MAX_COLS = 100
_POINTER_CELL_TEXT_MAX = 30

_ROMAN = {"Q1": "I", "Q2": "II", "Q3": "III", "Q4": "IV"}


# ---------------------------------------------------------------------------
# LLM output schema — coordinates only, never values
# ---------------------------------------------------------------------------

class _CellPointer(BaseModel):
    query_index: int = Field(..., description="Index of the query this pointer answers.")
    found: bool = Field(
        ..., description="False when the requested cell is absent or ambiguous."
    )
    row: Optional[int] = Field(
        None, description="0-based row index of the DATA cell, as shown in the snapshot."
    )
    col: Optional[int] = Field(
        None, description="0-based column index of the DATA cell, as shown in the snapshot."
    )


class _BatchCellPointers(BaseModel):
    """One pointer per query. Coordinates only — data values are read by code."""
    sheet_unit: Optional[str] = Field(
        None,
        description=(
            "The sheet's measurement-unit annotation text if visible (e.g. 'Miliar Rp'), "
            "else null. Text only, never a number."
        ),
    )
    pointers: List[_CellPointer] = Field(default_factory=list)


# ---------------------------------------------------------------------------
# Queries
# ---------------------------------------------------------------------------

@dataclass
class PointQuery:
    """One cell to locate: the exact TableData key it will be injected under + a
    human-readable description shown to the LLM."""
    fact_index: int
    data_key: Tuple  # (metric_label, year, month) or (metric_label, col_label)
    desc: str


def _period_desc(year: int, month: str) -> str:
    if month in _ROMAN:
        return f"Triwulan {_ROMAN[month]} ({month}) {year}"
    return f"{month} {year}"


def build_point_queries(facts, candidate_indices: List[int]) -> List[PointQuery]:
    """Enumerate every cell the candidate facts need, one PointQuery per cell.

    For yoy_growth facts the prior-year same-period point is synthesized too, covering
    the internal lookup _compute_yoy_growth performs. Facts mixing temporal and
    categorical points are skipped — a single source has one axis kind, so they are
    inherently unresolvable.
    """
    queries: List[PointQuery] = []
    for fi in candidate_indices:
        fact = facts[fi]
        kinds = {p.col_label is not None for p in fact.periods}
        if len(kinds) > 1:
            continue
        fact_queries: List[PointQuery] = []
        valid = True
        for p in fact.periods:
            if p.col_label is not None:
                fact_queries.append(PointQuery(
                    fact_index=fi,
                    data_key=(p.metric_label, p.col_label),
                    desc=f"baris='{p.metric_label}', atribut='{p.col_label}'",
                ))
            elif p.year is not None and p.month is not None:
                fact_queries.append(PointQuery(
                    fact_index=fi,
                    data_key=(p.metric_label, p.year, p.month),
                    desc=f"metrik='{p.metric_label}', periode='{_period_desc(p.year, p.month)}'",
                ))
            else:
                valid = False
                break
        if not valid:
            continue
        if fact.operation == "yoy_growth":
            for p in fact.periods:
                if p.col_label is None and p.year is not None and p.month is not None:
                    fact_queries.append(PointQuery(
                        fact_index=fi,
                        data_key=(p.metric_label, p.year - 1, p.month),
                        desc=(
                            f"metrik='{p.metric_label}', "
                            f"periode='{_period_desc(p.year - 1, p.month)}' "
                            "(pembanding tahun sebelumnya)"
                        ),
                    ))
        queries.extend(fact_queries)
    return queries


# ---------------------------------------------------------------------------
# Prompt + chain
# ---------------------------------------------------------------------------

_POINTER_SYSTEM_PROMPT = """\
You are a precise spreadsheet cell locator. You are shown a textual snapshot of a
spreadsheet grid: one line per row ("row r: ..."), each non-empty cell shown as
[c]=value with its 0-based column index c.

You receive a numbered list of QUERIES. Each query names a metric/row and either a
calendar period or an attribute/column. For each query, return the 0-based (row, col)
coordinate of the DATA CELL that holds the requested numeric value.

Rules:
1. NEVER return cell values — coordinates only. The value is read from the sheet by
   code, never from you.
2. Point at the DATA cell, not at a row-label, header, or total of something else.
3. Period vocabulary: quarters may be written I/II/III/IV, Q1..Q4, or with Tw/Triwulan/
   Kuartal prefixes; Q1=Triwulan I .. Q4=Triwulan IV. Indonesian months: Mei=May,
   Agu/Ags=Aug, Okt=Oct, Des=Dec, Peb=Feb. Year headers often sit on a separate
   (merged) row ABOVE the month/quarter row — the target column is the one under the
   requested year carrying the requested period token.
4. Return exactly one pointer per query_index; never invent extra indices.
5. Set found=false when the requested cell is absent, ambiguous, or may lie outside
   the snapshot region (a note says when the snapshot is truncated).
6. sheet_unit: when the sheet visibly declares a measurement-unit annotation (e.g.
   'Miliar Rp', '(dalam persen)'), copy that text; else null. Text only.
"""

_POINTER_HUMAN_TEMPLATE = """\
Sheet has {n_rows} rows and {n_cols} columns.{truncation_note}

SNAPSHOT:
{snapshot}

QUERIES:
{queries_block}
"""

_POINTER_PROMPT = ChatPromptTemplate.from_messages([
    ("system", _POINTER_SYSTEM_PROMPT),
    ("human", _POINTER_HUMAN_TEMPLATE),
])


def build_pointer_chain(llm: BaseChatModel):
    """Prompt | structured-output chain — separate builder so tests can mock it."""
    return _POINTER_PROMPT | llm.with_structured_output(_BatchCellPointers)


# ---------------------------------------------------------------------------
# Resolution
# ---------------------------------------------------------------------------

async def resolve_pointers(
    queries: List[PointQuery],
    grid: List[List],
    llm: BaseChatModel,
    fallback_llm: Optional[BaseChatModel] = None,
) -> Tuple[Dict[int, Tuple[int, int]], Optional[str]]:
    """One batched LLM call: {query_index: (row, col)} for every locatable query.

    Any LLM failure is swallowed (retried once on fallback_llm when given) — the
    caller keeps its original unresolved results. Pointers with found=False, unknown
    or duplicate query indices, or missing coordinates are dropped.
    """
    if not queries or not grid:
        return {}, None

    n_rows = len(grid)
    n_cols = max((len(r) for r in grid), default=0)
    truncation_note = ""
    if n_rows > _POINTER_MAX_ROWS or n_cols > _POINTER_MAX_COLS:
        truncation_note = (
            f" NOTE: the snapshot below is truncated to the first {_POINTER_MAX_ROWS} "
            f"rows x {_POINTER_MAX_COLS} columns — answers outside it must be found=false."
        )
    payload = {
        "n_rows": n_rows,
        "n_cols": n_cols,
        "truncation_note": truncation_note,
        "snapshot": _grid_snapshot(
            grid, _POINTER_MAX_ROWS, _POINTER_MAX_COLS, _POINTER_CELL_TEXT_MAX
        ),
        "queries_block": "\n".join(f"{i}. {q.desc}" for i, q in enumerate(queries)),
    }

    result = None
    for candidate in (llm, fallback_llm):
        if candidate is None:
            continue
        try:
            result = await build_pointer_chain(candidate).ainvoke(payload)
            break
        except Exception as exc:
            logger.warning("Cell-pointer LLM call failed: %s", exc)
    if result is None:
        return {}, None

    pointers: Dict[int, Tuple[int, int]] = {}
    for ptr in result.pointers:
        if not ptr.found or ptr.row is None or ptr.col is None:
            continue
        if not (0 <= ptr.query_index < len(queries)) or ptr.query_index in pointers:
            continue
        pointers[ptr.query_index] = (ptr.row, ptr.col)
    sheet_unit = (result.sheet_unit or "").strip() or None
    return pointers, sheet_unit


def read_grid_cell(grid: List[List], row: int, col: int) -> Optional[float]:
    """Read the numeric value at (row, col), or None when out of range / non-numeric.

    THE only place a pointer becomes a number — and it reads the grid, never the LLM.
    """
    if row < 0 or row >= len(grid):
        return None
    grid_row = grid[row]
    if col < 0 or col >= len(grid_row):
        return None
    v = grid_row[col]
    if _is_number(v):
        return float(v)
    return None
