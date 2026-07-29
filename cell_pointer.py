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
import re
from dataclasses import dataclass
from typing import Dict, List, Optional, Tuple

from langchain_core.language_models import BaseChatModel
from langchain_core.prompts import ChatPromptTemplate
from pydantic import BaseModel, Field

from table_parser_generic import _is_empty, _is_number
from table_parser_llm import _grid_snapshot

logger = logging.getLogger("fact-checker")

# Snapshot caps for the pointer prompt. Much larger than tier 3's structure-mapping
# window (20x30): the answer cells — the NEWEST periods a report cites — sit at the far
# right of wide time-series sheets, so a tight column cap would amputate exactly the
# region the queries target.
_POINTER_MAX_ROWS = 120
_POINTER_MAX_COLS = 100
_POINTER_CELL_TEXT_MAX = 30

# Relevance pruning (see _select_rows / _select_cols). Wide BI time-series sheets carry
# forty years of columns while a report only ever cites the newest one or two, so sending
# the whole grid is both the pipeline's largest prompt and — once past _POINTER_MAX_COLS —
# a slab that no longer even contains the answer.
_HEADER_ROWS = 6          # top rows always kept: they carry the year/period headers
_YEAR_BLOCK_SPAN = 13     # columns kept after a year header, covering a 12-month block
_MIN_PRUNE_GAIN = 0.6     # skip pruning unless it drops >40% of the axis — not worth the risk

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
        # The claim's own sentence carries the extra dimensions (region, city, product…)
        # that the structured (metric, period/attribute) form drops. Passing it lets the
        # LLM pick the RIGHT row when several share a label — the difference between a
        # resolvable and an ambiguous query on a long-format/relational table.
        quote = (getattr(fact, "context_quote", "") or "").strip()
        ctx = f' — konteks: "{quote[:200]}"' if quote else ""
        fact_queries: List[PointQuery] = []
        valid = True
        for p in fact.periods:
            if p.col_label is not None:
                fact_queries.append(PointQuery(
                    fact_index=fi,
                    data_key=(p.metric_label, p.col_label),
                    desc=f"baris='{p.metric_label}', atribut='{p.col_label}'{ctx}",
                ))
            elif p.year is not None and p.month is not None:
                fact_queries.append(PointQuery(
                    fact_index=fi,
                    data_key=(p.metric_label, p.year, p.month),
                    desc=f"metrik='{p.metric_label}', periode='{_period_desc(p.year, p.month)}'{ctx}",
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
                            f"(pembanding tahun sebelumnya){ctx}"
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

A query may also carry 'konteks' — the claim's original sentence. When several rows share
a label (e.g. many 'Laptop' rows in a long-format table), USE the other details in that
context (region, city, quarter, product…) to choose the ONE correct row. If the context
still doesn't pin down a single row, set found=false.

You receive a numbered list of QUERIES. Each query names a metric/row and either a
calendar period or an attribute/column. For each query, return the 0-based (row, col)
coordinate of the DATA CELL that holds the requested numeric value.

Rules:
1. NEVER return cell values — coordinates only. The value is read from the sheet by
   code, never from you.
2. Point at the DATA cell for the EXACT metric requested — never a row-label, a header,
   a column TOTAL/aggregate row, or a DIFFERENT but nearby indicator.
3. Period vocabulary: quarters may be written I/II/III/IV, Q1..Q4, or with Tw/Triwulan/
   Kuartal prefixes; Q1=Triwulan I .. Q4=Triwulan IV. Indonesian months: Mei=May,
   Agu/Ags=Aug, Okt=Oct, Des=Dec, Peb=Feb. Year headers often sit on a separate
   (merged) row ABOVE the month/quarter row — the target column is the one under the
   requested year carrying the requested period token.
4. Return exactly one pointer per query_index; never invent extra indices.
5. Set found=false when the sheet has NO row that genuinely names the requested metric,
   when the target cell is ambiguous, or when it may lie outside a truncated snapshot.
   A missing metric MUST be found=false — do NOT substitute a TOTAL/aggregate row or a
   different-but-related indicator just to return a coordinate. Guessing is worse than
   admitting the metric is absent.
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
# Relevance selection — which slice of the grid this batch of queries can possibly need
# ---------------------------------------------------------------------------

def _row_words(grid_row: List) -> set:
    """Significant words of every string cell in one row."""
    words: set = set()
    for v in grid_row:
        if isinstance(v, str):
            words |= _sig_words(v)
    return words


def _select_rows(grid: List[List], queries: List[PointQuery]) -> Optional[List[int]]:
    """Row indices worth showing, or None when every row must be kept.

    The predicate is `pointer_is_plausible`'s, quantified per row: a row that shares no
    significant word with any queried metric would have its pointer rejected after the
    call anyway, so dropping it beforehand cannot change a single verdict. When some
    metric is unguarded (no significant words of its own, e.g. 'M2') the guard accepts
    any row for it, so no row may be dropped and this returns None.
    """
    per_query_words = []
    for q in queries:
        words = _sig_words(q.data_key[0])
        if not words:
            return None  # unguarded metric — every row stays acceptable
        per_query_words.append(words)

    keep = set(range(min(_HEADER_ROWS, len(grid))))
    for r, row in enumerate(grid):
        row_words = _row_words(row)
        if any(words & row_words for words in per_query_words):
            keep.add(r)
    return sorted(keep)


def _requested_period_tokens(queries: List[PointQuery]) -> Tuple[set, set]:
    """(year strings, lowercased attribute names) the queries actually ask for.

    Months and quarters are deliberately NOT matched. A token like 'Apr' recurs in every
    year block of a time-series sheet, so keying on it selects one column per year across
    the whole history — the opposite of narrowing. The year picks the block; the
    _YEAR_BLOCK_SPAN window carries that block's months along with it.
    """
    years: set = set()
    labels: set = set()
    for q in queries:
        key = q.data_key
        if len(key) == 3:
            year = key[1]
            if isinstance(year, int):
                years.add(str(year))
        elif len(key) == 2 and isinstance(key[1], str):
            labels.add(key[1].lower())
    return years, labels


def _select_cols(
    grid: List[List], queries: List[PointQuery], keep_rows: Optional[List[int]]
) -> Optional[List[int]]:
    """Column indices worth showing, or None when every column must be kept.

    Two kinds survive: the label columns (all text, no numbers — they name the rows) and
    the columns under a requested year or attribute. A year header often labels only the
    first column of its block, so each hit also carries the following months along.

    Returns None — meaning "send everything, as before" — whenever the selection cannot
    be trusted: no header mentions a requested period, or too little would be dropped for
    the risk to be worth taking.
    """
    n_cols = max((len(r) for r in grid), default=0)
    if not n_cols:
        return None

    years, labels = _requested_period_tokens(queries)
    if not years and not labels:
        return None

    matched: set = set()
    for row in grid[:_HEADER_ROWS]:
        for c, v in enumerate(row):
            if v is None:
                continue
            text = str(v).strip().lower()
            if not text:
                continue
            if any(y in text for y in years) or text in labels:
                matched.add(c)
    if not matched:
        return None

    period_cols: set = set()
    for c in matched:
        period_cols.update(range(c, min(c + _YEAR_BLOCK_SPAN, n_cols)))

    # Label columns name the rows: across the rows on show they carry text and never a
    # number. Scanning every kept row rather than only those past the header keeps this
    # working on short sheets, where the whole grid sits inside the header window.
    label_cols: set = set()
    rows_on_show = keep_rows if keep_rows is not None else range(len(grid))
    for c in range(n_cols):
        has_text = False
        has_number = False
        for r in rows_on_show:
            if r >= len(grid) or c >= len(grid[r]):
                continue
            v = grid[r][c]
            if _is_number(v):
                has_number = True
                break
            if isinstance(v, str) and v.strip():
                has_text = True
        if has_text and not has_number:
            label_cols.add(c)

    if len(period_cols | label_cols) >= _MIN_PRUNE_GAIN * n_cols:
        return None

    # Trimming to the cap drops the LEFTMOST period columns, never the label columns: these
    # sheets run oldest-to-newest, and it is the newest period a report cites. Slicing the
    # combined set from the front instead would amputate the row labels and then the very
    # columns the queries are about.
    period_cols -= label_cols
    room = max(_POINTER_MAX_COLS - len(label_cols), 0)
    return sorted(label_cols | set(sorted(period_cols)[-room:] if room else []))


def _indexed_snapshot(
    grid: List[List],
    row_idxs: List[int],
    col_idxs: List[int],
    cell_text_max: int = _POINTER_CELL_TEXT_MAX,
) -> str:
    """Snapshot of a chosen sub-grid, printed with the cells' ORIGINAL coordinates.

    The indices in the output are what the LLM answers with and what `read_grid_cell`
    then reads out of the FULL grid, so they must never be renumbered to the position
    within the selection.
    """
    lines = []
    for r in row_idxs:
        if r >= len(grid):
            continue
        row = grid[r]
        cells = []
        for c in col_idxs:
            if c >= len(row):
                continue
            v = row[c]
            if _is_empty(v):
                continue
            text = str(v)
            if len(text) > cell_text_max:
                text = text[:cell_text_max] + "…"
            cells.append(f"[{c}]={text!r}")
        lines.append(f"row {r}: " + (" ".join(cells) if cells else "(empty)"))
    return "\n".join(lines)


def build_snapshot(grid: List[List], queries: List[PointQuery]) -> Tuple[str, str]:
    """(snapshot text, note describing what the LLM is looking at).

    Prefers a relevance-selected view of the grid; falls back to the plain top-left
    window whenever the selection would be unsafe or pointless.
    """
    n_rows = len(grid)
    n_cols = max((len(r) for r in grid), default=0)

    keep_rows = _select_rows(grid, queries)
    keep_cols = _select_cols(grid, queries, keep_rows)

    if keep_cols is not None or (keep_rows is not None and len(keep_rows) < _MIN_PRUNE_GAIN * n_rows):
        rows = (keep_rows if keep_rows is not None else list(range(n_rows)))[:_POINTER_MAX_ROWS]
        cols = (keep_cols if keep_cols is not None else list(range(n_cols)))[:_POINTER_MAX_COLS]
        note = (
            " NOTE: the snapshot shows only the rows and columns relevant to these queries, "
            "so row/column indices JUMP — always copy the index printed next to the cell. "
            "If a query's cell is not visible, answer found=false."
        )
        return _indexed_snapshot(grid, rows, cols), note

    note = ""
    if n_rows > _POINTER_MAX_ROWS or n_cols > _POINTER_MAX_COLS:
        note = (
            f" NOTE: the snapshot below is truncated to the first {_POINTER_MAX_ROWS} "
            f"rows x {_POINTER_MAX_COLS} columns — answers outside it must be found=false."
        )
    return _grid_snapshot(grid, _POINTER_MAX_ROWS, _POINTER_MAX_COLS, _POINTER_CELL_TEXT_MAX), note


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

    snapshot, truncation_note = build_snapshot(grid, queries)
    payload = {
        "n_rows": len(grid),
        "n_cols": max((len(r) for r in grid), default=0),
        "truncation_note": truncation_note,
        "snapshot": snapshot,
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


def _sig_words(text: str) -> set:
    """Lowercased words of 3+ chars — the notion of a 'significant' term for matching."""
    return {w for w in re.findall(r"\w+", text.lower()) if len(w) > 2}


def pointer_is_plausible(
    grid: List[List], row: int, metric_label: str, table_title: str = ""
) -> bool:
    """True when the pointed row (or the table title) shares a significant word with the
    requested metric.

    A plausibility guard against the LLM pointing at an unrelated row — e.g. a generic
    'TOTAL' — for a metric that is not actually in the sheet (observed: an ILS claim
    pointed at the SBT total). It intentionally rejects such over-reaches at the cost of
    also declining pointers whose only matching row is an abbreviation/synonym with no
    shared word (those degrade to Inconclusive, which is safe, not to a wrong verdict).
    Metrics with no significant word of their own (e.g. 'M2') are not guarded.
    """
    metric_words = _sig_words(metric_label)
    if not metric_words:
        return True
    context_words = _sig_words(table_title or "")
    if 0 <= row < len(grid):
        context_words |= _row_words(grid[row])
    if metric_words & context_words:
        return True
    logger.info(
        "Cell pointer rejected: row %d shares no term with metric %r", row, metric_label
    )
    return False


def metric_could_match(grid: List[List], metric_label: str, table_title: str = "") -> bool:
    """True when SOME row of this sheet could pass `pointer_is_plausible` for the metric.

    The same predicate, quantified over the whole sheet instead of one row. Asking for a
    metric this returns False for is pure waste: whatever coordinate came back, the guard
    would reject it — so the query can be dropped, and a sheet with no surviving query
    skipped entirely, without any verdict changing.
    """
    metric_words = _sig_words(metric_label)
    if not metric_words:
        return True
    if metric_words & _sig_words(table_title or ""):
        return True
    return any(metric_words & _row_words(row) for row in grid)
