"""Paired PDF + Excel verification pipeline.

Given one PDF report and one or more Excel statistical tables (BI format), verifies every
quantitative claim in the PDF narrative against the authoritative values in the Excel sources.

Claims are represented as an OPERATION over one or more (metric, year, month) data points
(see structured_extractor.py for the full operation list: value, yoy_growth, average, sum,
diff, ratio, is_increasing, is_decreasing, is_stable) rather than a fixed claim-type enum, so
claims more complex than a single point-in-time value are supported without a schema change per
pattern. No SQL is generated — every data point is a direct dict lookup into the parsed Excel
tables; the operation itself (average, sum, diff, ratio, monotonic-trend check, unit conversion,
YoY growth) is computed in plain Python, never by an LLM, to avoid hallucination risk in the
comparison step.

Lookup cascade: parsing runs bi → generic → LLM structure-mapping (_parse_table_with_fallback);
claims that still resolve against no parsed table get a tier-4 CELL-POINTER pass (_pointer_pass /
cell_pointer.py): the LLM points at grid coordinates and code reads the values, so even sheets
whose structure defeats every parser stay verifiable ("pointer-only" sources) — and the LLM still
never supplies a number, only a location that is reported in the result's provenance.
"""

import logging
import re
from dataclasses import dataclass
from decimal import Decimal
from typing import Any, Callable, Dict, List, Optional, Tuple

from langchain_core.language_models import BaseChatModel

from cell_pointer import (
    PointQuery,
    build_point_queries,
    metric_could_match,
    pointer_column_matches,
    pointer_is_plausible,
    read_grid_cell,
    resolve_pointers_multi,
)
from excel_parser_bi import BITableData, parse_bi_table
from pdf_chart_extraction import ChartReading
from pdf_table_extraction import PdfTable
from table_model import QUAL_SEP, _expand_report_terms, _same_root, _sig_words, label_match_score
from publication_parsers import load_grid as _load_grid
from publication_parsers import parse_for_publication
from table_parser_generic import (
    _MONTH_ABBREVS,
    parse_generic_grid,
    parse_generic_table,
)
from table_parser_llm import parse_grid_with_llm, parse_table_with_llm
from schemas import (
    ChartCheck,
    FactVerificationResult,
    PairedVerificationResponse,
    PeriodResult,
    SourceValue,
    TableSuggestion,
)
from structured_extractor import (
    ExtractedFact,
    PeriodPoint,
    extract_structured_facts,
    extract_structured_facts_async,
)

logger = logging.getLogger("fact-checker")

# ── Progress reporting ────────────────────────────────────────────────────────
# A callback receiving one stage event dict per pipeline step, so a streaming caller (see
# main.py's /api/verify-paired-stream) can tell the user which step is running during the
# ~40s wait instead of showing an opaque spinner. Events look like:
#   {"type": "stage", "stage": "excel", "status": "running", "current": 1, "total": 2,
#    "detail": "TABEL1_1.xls / I.1"}
# `status` is "running" or "done". Called synchronously on the event loop thread, so an
# implementation must never block or await. None (the default) disables reporting.
ProgressCb = Optional[Callable[[Dict[str, Any]], None]]

# ── Rounding tolerance ────────────────────────────────────────────────────────
# Values in the PDF are printed to 1 decimal place (e.g. 10.415,9 triliun or 10,8%).
# Half of one rounding unit = 0.05 in either scale.
# False positives are more dangerous than false negatives → use strict tolerance.
MATCH_TOLERANCE = 0.05

# Operations whose comparison is a level value in fact.unit's scale, needing unit conversion
# against the matched Excel source's unit. The rest (yoy_growth, ratio, trend checks) either
# compare percentages directly or cancel/ignore units entirely.
_LEVEL_OPS = {"value", "average", "sum", "diff"}

# Operations that are only meaningful along a time axis: yoy needs the prior-year point,
# trend checks need chronological ordering. A claim whose data points are categorical
# (col_label instead of year/month) can never satisfy them.
_TEMPORAL_ONLY_OPS = {"yoy_growth", "is_increasing", "is_decreasing", "is_stable"}

# Monotonic-trend operations: ONE metric followed across several time points. They are
# invalid when the extractor bundles several DIFFERENT metrics into one fact (a
# cross-metric comparison mislabelled as a trend) or gives fewer than two distinct time
# points (nothing to trend) — see the guard in _evaluate_fact.
_TREND_OPS = {"is_increasing", "is_decreasing", "is_stable"}

# Half-width of the "relatif stabil" window, as a fraction of the level being tracked.
# is_stable used MATCH_TOLERANCE, which is half a PRINTING unit — the right question for "does
# 10.415,9 in the PDF match the sheet", the wrong one for "did this series hold roughly flat".
# It refuted every stability claim whose two numbers were not near-identical: a debt-to-income
# ratio written as "10,0%, relatif stabil dibandingkan proporsi bulan sebelumnya sebesar 10,2%"
# came back Tidak Sesuai over 0,2pp. Judging the move against the level fixes that without
# blessing real swings — 0,2 on a ratio of 10 is stable, 0,2 on a ratio of 0,5 is not.
# 5% rather than the first 2,5%: SK-Agustus-2026 calls the same ratio "relatif stabil" at
# 10,5% -> 10,0%, a 4,9% move, and the user settled that BI's own usage is the yardstick.
_STABLE_RELATIVE_BAND = 0.05

# Narrative markers of an UNNAMED subset: "peningkatan IKK terjadi di beberapa kota",
# "sebagian besar kota mencatat penurunan IEK", "IPDG berada pada level optimis pada sebagian
# besar kelompok pengeluaran". No single row corresponds to such a subset, so a claim about it
# cannot be settled by reading one — whichever row is read answers a different question, and
# the answer comes back Refuted on a sentence that is in fact true. The extractor is told to
# skip these (rule 9), and this is the deterministic backstop for when it does not. It
# deliberately does NOT fire on the per-member facts split out of the same sentence
# ("… terutama di Makassar, Banten, dan Medan") — see _quote_names_qualifier.
_SUBSET_PHRASE_RE = re.compile(
    r"\b(?:beberapa|sejumlah|banyak|sebagian(?:\s+besar)?)\s+(?:\w+\s+)?"
    r"(?:kota|daerah|wilayah|provinsi|kelompok|responden|komponen)\b"
    r"|\b(?:kota|daerah|wilayah|kelompok|golongan|tingkat\s+pendidikan)\s+"
    r"(?:\w+\s+)?lain(?:nya)?\b",
    re.IGNORECASE,
)

# Threshold operations: ONE metric at ONE period compared to a bound (claimed_value is the
# threshold, e.g. a PMI diffusion index "berada pada fase ekspansi (>50)"). Dimensionless —
# no unit conversion — so they stay out of _LEVEL_OPS.
_THRESHOLD_OPS = {"above_threshold", "below_threshold"}

# Source origins that answer a claim only when the primary sources (the report's own tables and
# the uploaded Excel) cannot — see _evaluate_fact.
_FALLBACK_ORIGINS = ("pdf_other", "chart")

# Row labels advertised to the fact extractor per source (see _source_desc). Internal mode can
# add a dozen sources at once, and every label is printed into every extraction chunk's prompt.
_MAX_LABELS_PER_SOURCE = 60


# ---------------------------------------------------------------------------
# Reference source container
#
# Named _ExcelSource because Excel sheets were the only kind. It now also carries tables
# transcribed out of the PDF itself (origin="pdf"); everything below this line treats the two
# identically on purpose — a source is a parsed table plus, optionally, its raw grid.
# ---------------------------------------------------------------------------

@dataclass
class _ExcelSource:
    table: BITableData
    filename: str
    sheet: str
    # Raw cell grid (merged-fill applied) for the tier-4 cell-pointer pass; None when
    # the bytes could not be loaded as a grid (then the pointer pass skips this source).
    grid: Optional[List[List]] = None
    # True when every parser failed but the grid loaded: the table is empty and claims
    # against this source can only be resolved by the cell-pointer pass.
    pointer_only: bool = False
    # "excel" for an uploaded workbook sheet, "pdf" for a table transcribed from the report
    # itself, "pdf_other" for a table from another PDF uploaded in the same run, "chart" for the
    # labels printed on one of the report's charts. Branched on for labelling a conflict, and to
    # keep "pdf_other" and "chart" fallbacks (see _evaluate_fact).
    origin: str = "excel"

    @property
    def label(self) -> str:
        return f"{self.filename} / {self.sheet}"


@dataclass
class _Candidate:
    """One source that resolved every data point a claim references."""
    score: float                          # mean label-match quality — see _resolution_score
    # Mean share of the claim's scope words this source accounts for (row label + table title).
    # Ranked ABOVE `score` — see _coverage_score.
    coverage: float
    src: _ExcelSource
    resolved: List[Tuple[str, float]]
    factor: float                         # divide raw values by this to reach the claim's unit
    # 0 when the matched row's qualification matches the claim's, 1 otherwise — see
    # _qualification_rank. Ranked between coverage and score.
    qualification: int = 0
    # False when the source declared no scale of its own and only the claim's scale was
    # applied. Such a value is fine for a verdict but cannot be compared against another
    # source's (see _attach_source_comparison).
    unit_comparable: bool = True
    # `coverage` counting only the title and the matched rows — breaks coverage ties in favour
    # of the table whose own name says what the claim says (see TableData.query_coverage).
    direct_coverage: float = 0.0


# ---------------------------------------------------------------------------
# Unit conversion
# ---------------------------------------------------------------------------

_UNIT_FACTORS: Dict[Tuple[str, str], float] = {
    # (pdf_unit_normalised, excel_unit_normalised) -> factor such that pdf * factor = excel
    # Rupiah
    ("triliun rp", "miliar rp"): 1_000.0,
    ("miliar rp",  "miliar rp"): 1.0,
    ("triliun rp", "triliun rp"): 1.0,
    ("miliar rp",  "triliun rp"): 0.001,
    # USD — LLM may say "miliar USD", "miliar dolar AS", "miliar dolar", or "billion USD"
    ("miliar usd",      "juta usd"): 1_000.0,
    ("miliar dolar as", "juta usd"): 1_000.0,
    ("miliar dolar",    "juta usd"): 1_000.0,
    ("billion usd",     "juta usd"): 1_000.0,
    ("juta usd",        "juta usd"): 1.0,
    ("miliar usd",      "miliar usd"): 1.0,
    ("miliar dolar as", "miliar dolar as"): 1.0,
    ("juta usd",        "miliar usd"): 0.001,
    ("juta usd",        "miliar dolar as"): 0.001,
}


# Decimal scale words and currency tokens for units the explicit _UNIT_FACTORS table does not
# list. Parsing these keeps unit handling deterministic for arbitrary sources (the generic
# parser can encounter any wording) without enumerating every pair by hand.
_SCALE_WORDS: Dict[str, float] = {
    "ribu": 1e3, "thousand": 1e3,
    "juta": 1e6, "million": 1e6,
    "miliar": 1e9, "milyar": 1e9, "billion": 1e9,
    "triliun": 1e12, "trillion": 1e12,
}
_CURRENCY_TOKENS: Dict[str, str] = {
    "rp": "rp", "rupiah": "rp", "idr": "rp",
    "usd": "usd", "dolar": "usd", "dollar": "usd",
}


def _parse_scale_unit(unit: Optional[str]) -> Optional[Tuple[float, Optional[str]]]:
    """Parse a level unit into (decimal scale, currency token or None).

    'juta Rp' -> (1e6, 'rp') | 'Rp' -> (1.0, 'rp') | 'miliar' -> (1e9, None).
    Returns None for percentages, for no unit at all (an index claim), and for units with
    neither a scale word nor a currency ('unit', 'buah'), where scaling would be meaningless.
    """
    if not unit or "%" in unit:
        return None
    words = re.findall(r"[a-z]+", unit.lower())
    if "persen" in words:
        return None
    scale, currency, saw_scale = 1.0, None, False
    # Walk the words, consuming one when it is a unit term and two or three when only their
    # concatenation is. BI's PDFs break words on a stray space, so Tabel 7 of
    # sample_data/M2-Juli-2026.pdf is captioned '(t riliun Rp)'. Read word by word that yields
    # 't', 'riliun', 'rp' — no scale term at all — and the unit silently degraded to plain
    # rupiah, making _unit_factor('triliun Rp', 't riliun Rp') return 1e12 instead of 1,0. Same
    # rejoining rule as table_model._label_words, and just as narrow: a join is only taken when
    # the result is a term we already know.
    i = 0
    while i < len(words):
        for span in (1, 2, 3):
            if i + span > len(words):
                break
            token = "".join(words[i:i + span])
            if token in _SCALE_WORDS:
                scale *= _SCALE_WORDS[token]
                saw_scale = True
                break
            if token in _CURRENCY_TOKENS:
                if currency is None:
                    currency = _CURRENCY_TOKENS[token]
                break
        else:
            span = 1
        i += span
    if not saw_scale and currency is None:
        return None
    return scale, currency


def _unit_factor(pdf_unit: Optional[str], excel_unit: Optional[str]) -> Optional[float]:
    """Return the multiplier to convert a PDF absolute value to Excel units, or None if unknown."""
    if not pdf_unit or not excel_unit:
        return None
    key = (pdf_unit.lower().strip(), excel_unit.lower().strip())
    factor = _UNIT_FACTORS.get(key)
    if factor is not None:
        return factor
    # Identical units (after normalisation) never need conversion, whatever they are —
    # keeps the explicit table for CROSS-unit pairs only.
    if key[0] == key[1]:
        return 1.0
    # General fallback: both units parse to a decimal scale of the SAME currency
    # ('ribu Rp' vs 'miliar Rp' -> 1e3/1e9). pdf * factor = excel, hence the ratio.
    pdf_parsed, excel_parsed = _parse_scale_unit(key[0]), _parse_scale_unit(key[1])
    if pdf_parsed and excel_parsed and pdf_parsed[1] and pdf_parsed[1] == excel_parsed[1]:
        return pdf_parsed[0] / excel_parsed[0]
    return None


# Trailing parenthetical of a categorical column name, where such tables usually declare
# the column's unit: 'Harga (Rp)' -> 'Rp', 'Omzet (juta Rp)' -> 'juta Rp'.
_COL_UNIT_RE = re.compile(r"\(([^)]+)\)\s*$")


def _col_unit(col_label: str) -> Optional[str]:
    m = _COL_UNIT_RE.search(col_label)
    return m.group(1).strip() if m else None


# ---------------------------------------------------------------------------
# Period resolution against a single Excel source
# ---------------------------------------------------------------------------

def _try_resolve(
    periods: List[PeriodPoint], src: _ExcelSource
) -> Tuple[List[Tuple[str, float]], List[PeriodPoint], List[Optional[str]]]:
    """Look up every period in one Excel source.

    Returns (resolved, missing, col_units): resolved has (matched_label, raw_value) for periods
    found in this source (in the same order as `periods`); missing has the PeriodPoint objects
    that weren't found; col_units has, per resolved categorical point, the unit declared in the
    matched column's trailing parenthetical ('Harga (Rp)' -> 'Rp'), or None. A fact's periods
    must ALL resolve from the SAME source (kept unit-consistent) - callers should treat a
    non-empty `missing` as "this source can't be used for this fact".

    Each point only resolves against a source of the matching axis kind: temporal points
    (year+month) against temporal tables, categorical points (col_label) against categorical
    tables. A mismatched source simply counts the point as missing so the next source is tried.
    """
    resolved: List[Tuple[str, float]] = []
    missing: List[PeriodPoint] = []
    col_units: List[Optional[str]] = []
    for p in periods:
        if p.col_label is not None:
            if src.table.axis_type != "categorical":
                missing.append(p)
                continue
            row, col, raw = src.table.lookup_cell_fuzzy(p.metric_label, p.col_label)
            if raw is None:
                missing.append(p)
            else:
                resolved.append((f"{row} — {col}", raw))
                col_units.append(_col_unit(col))
        else:
            if src.table.axis_type != "temporal":
                missing.append(p)
                continue
            label, raw = src.table.lookup_fuzzy(p.metric_label, p.year, p.month)
            if raw is None:
                missing.append(p)
            else:
                resolved.append((label, raw))
                col_units.append(None)
    return resolved, missing, col_units


def _resolution_score(
    periods: List[PeriodPoint], resolved: List[Tuple[str, float]]
) -> float:
    """Mean label_match_score across a fact's data points — how well one source's rows
    answer the metric names the claim actually asked for."""
    scores = [
        label_match_score(p.metric_label, label)
        for p, (label, _) in zip(periods, resolved)
    ]
    return sum(scores) / len(scores) if scores else 0.0


def _qualification_rank(periods: List[PeriodPoint], resolved: List[Tuple[str, float]]) -> int:
    """0 when the matched rows are qualified exactly as the claim is, 1 otherwise.

    A parent-qualified row ('Kredit Investasi > Konstruksi') carries its parent's words, which
    inflates the plain word overlap `_resolution_score` measures: a claim about "kredit
    konstruksi" — the property-credit component in Tabel 7 — scored 0,8 against that SECTOR row
    of Lampiran 4 and only 0,67 against the plain 'Konstruksi' row that answers it. Comparing
    like with like settles that without touching the score itself, which the survey workbooks
    rely on to keep a national claim off a per-city row.

    Ranked below coverage, so a claim whose scope words point at one table ("DPK korporasi" ->
    the DPK table, whose rows are all qualified) still goes there.
    """
    mismatches = sum(
        1 for p, (label, _value) in zip(periods, resolved)
        if _is_qualified(label) != _is_qualified(p.metric_label)
    )
    return 1 if mismatches else 0


def _is_qualified(label: Optional[str]) -> bool:
    """Whether a row name names a SECTION of its series rather than the series itself.

    A 'Total' parent does not: 'Total > Tabungan' is the whole table's savings row, which is
    exactly what a claim naming no section means — the same reading table_model's leaf tier
    already applies when it prefers an aggregate parent. Counting it as qualified made the two
    sheets of SK-Juni-2026 that carry a savings row rank the wrong way round, and "saving to
    income ratio … 17,0%" was answered by Tabel 8's 'Tabungan/deposito' (44,5) and refuted.
    """
    if QUAL_SEP not in (label or ""):
        return False
    return not BITableData._is_aggregate(label.rsplit(QUAL_SEP, 1)[0])


def _coverage_score(
    periods: List[PeriodPoint], resolved: List[Tuple[str, float]], src: "_ExcelSource",
    broken_out: bool = True,
) -> float:
    """Mean TableData.query_coverage across a fact's data points.

    Ranks ABOVE _resolution_score: a source that leaves one of the claim's scope words
    unaccounted for ('UMKM', 'DPK') is answering a different question, however well the row
    name it found happens to read. See TableData.query_coverage for the cases.
    """
    scores = [
        src.table.query_coverage(p.metric_label, label, broken_out=broken_out)
        for p, (label, _) in zip(periods, resolved)
    ]
    return sum(scores) / len(scores) if scores else 0.0


def _build_periods(
    fact_periods: List[PeriodPoint], resolved: List[Tuple[str, float]], values: List[float]
) -> List[PeriodResult]:
    return [
        PeriodResult(
            metric_label=resolved[i][0], year=p.year, month=p.month,
            col_label=p.col_label, excel_value=round(values[i], 4),
        )
        for i, p in enumerate(fact_periods)
    ]


def _point_desc(p: PeriodPoint) -> str:
    """Short human-readable identity of a data point for reasoning strings."""
    return p.col_label if p.col_label is not None else f"{p.month} {p.year}"


_QUARTER_ORDINAL = {"Q1": 1, "Q2": 2, "Q3": 3, "Q4": 4}


def _period_ordinal(token: str) -> int:
    """Chronological rank of a period token so months and quarters sort correctly
    (available_periods sorts tokens as plain strings, which misorders month abbrevs)."""
    if token in _QUARTER_ORDINAL:
        return _QUARTER_ORDINAL[token]
    try:
        return _MONTH_ABBREVS.index(token) + 1
    except ValueError:
        return 0


def _previous_period(
    table: BITableData, label: str, year: int, month: str
) -> Optional[Tuple[Tuple[int, str], float]]:
    """The period immediately before (year, month) in this metric's own series, with its
    value — used to complete a single-point trend claim. None when there is no earlier
    period or it has no value."""
    avail = table.available_periods(label)
    ordered = sorted(avail, key=lambda ym: (ym[0], _period_ordinal(ym[1])))
    key = (year, month)
    if key not in ordered:
        return None
    i = ordered.index(key)
    if i == 0:
        return None
    py, pm = ordered[i - 1]
    _, val = table.lookup_fuzzy(label, py, pm)
    return ((py, pm), val) if val is not None else None


def _stable_band(a: float, b: float) -> float:
    """Largest move between two consecutive points still readable as "relatif stabil".

    Proportional to the pair's own magnitude, floored at MATCH_TOLERANCE so a series hovering
    near zero (SBT balances, growth rates that cross sign) still admits two readings that are
    identical to the precision they were printed at, instead of getting a band of nearly zero.
    """
    return max(MATCH_TOLERANCE, _STABLE_RELATIVE_BAND * (abs(a) + abs(b)) / 2)


def _numeric_verdict(
    claimed: float, computed: float, tolerance: float = MATCH_TOLERANCE
) -> Tuple[float, str]:
    delta = round(abs(claimed - computed), 4)
    return delta, ("Entailed" if delta <= tolerance else "Refuted")


def _printing_step(value: float) -> float:
    """The last decimal place `value` was printed to, e.g. 0.1 for 7,6 and 1.0 for 12."""
    exponent = Decimal(str(round(value, 6))).as_tuple().exponent
    return float(Decimal(1).scaleb(exponent)) if exponent < 0 else 1.0


def _growth_tolerance(current: float, prior: float) -> float:
    """Tolerance for a yoy computed from two PRINTED levels, widened by their rounding.

    A table states 7,6 and 7,5; the true values lie anywhere within half a printed step of
    those, so the growth they imply is not 1,33% but 1,33% give or take 1,3pp — and the
    report's own 1,5% sits comfortably inside that. Comparing against the bare 0,05 tolerance
    reported Tidak Sesuai on a figure the document had every right to print.

    The band is ADDED to the ordinary tolerance rather than compared against it: the claim's
    own rounding (what MATCH_TOLERANCE stands for) and the levels' rounding are independent
    sources of slack, and taking the larger threw the smaller away. On M2-Juni-2026 the report
    printed 37,4% (yoy) while its own levels 799,0 / 581,3 imply 37,4505% — Δ = 0,0505, refuted
    by half a thousandth of a point, because the 0,0204 the levels carry was discarded for
    being smaller than 0,05.

    The band shrinks to nothing as the base grows (on 9.387,9 it is 0,001pp), so this stays a
    rounding allowance rather than a blanket loosening. A directly-printed growth cell is
    unaffected — there is no division.
    """
    if not prior:
        return MATCH_TOLERANCE
    half = _printing_step(prior) / 2
    band = 100 * (half / abs(prior)) * (1 + abs(current / prior))
    return round(MATCH_TOLERANCE + band, 4)


def _make_result(
    fact: ExtractedFact,
    periods: List[PeriodResult],
    matched_source: Optional[str],
    claimed_value: Optional[float],
    claimed_unit: Optional[str],
    computed_value: Optional[float],
    computed_unit: Optional[str],
    delta: Optional[float],
    verdict: str,
    reasoning: str,
) -> FactVerificationResult:
    return FactVerificationResult(
        operation=fact.operation,
        metric_label=fact.display_label,
        matched_excel_source=matched_source,
        periods=periods,
        claimed_value=claimed_value,
        claimed_unit=claimed_unit,
        computed_value=computed_value,
        computed_unit=computed_unit,
        delta=delta,
        verdict=verdict,
        reasoning=reasoning,
        context_quote=fact.context_quote,
        page_number=fact.page_number,
    )


# ---------------------------------------------------------------------------
# Per-operation computation (all arithmetic here is plain Python, never LLM output)
# ---------------------------------------------------------------------------

def _is_growth_series(unit: Optional[str]) -> bool:
    """True when a table's cells ARE year-on-year growth figures, e.g. unit '%, yoy'.

    A BI report states the same series twice under identical row labels: Lampiran 1 in
    'Triliun Rp' and Lampiran 2 in '%, yoy'. Growth computed over the second is growth OF a
    growth — 15,7% claimed against a computed 56,3% on the M2 report — which is not a
    disagreement with the claim but an operation that should never have run.
    """
    return bool(unit and re.search(r'\byoy\b', unit, re.IGNORECASE))


_GROWTH_KIND_RE = re.compile(r"\b(yoy|mtm|qtq|ctc)\b", re.IGNORECASE)


def _growth_kind(unit: Optional[str], label: str) -> Optional[str]:
    """Which growth a matched cell holds — 'yoy', 'mtm', 'qtq', 'ctc' — or None for a level.

    The row label wins when it names one (SHPR Tabel 3 carries a 'TRIWULANAN (QTQ)' and a
    'TAHUNAN (YOY)' band under one unit '%, qtq & yoy'); otherwise the table unit must name
    exactly one kind. A mixed unit with no band in the label is 'mixed' — not knowably yoy.
    """
    in_label = {k.lower() for k in _GROWTH_KIND_RE.findall(label or "")}
    if len(in_label) == 1:
        return in_label.pop()
    in_unit = {k.lower() for k in _GROWTH_KIND_RE.findall(unit or "")}
    if not in_unit:
        return None
    return in_unit.pop() if len(in_unit) == 1 else "mixed"


def _not_a_yoy_cell(fact: ExtractedFact, label: str, src: "_ExcelSource") -> Optional[str]:
    """The growth kind of a cell a yoy claim landed on, when it is NOT a yoy figure.

    SPE Tabel 3 is in '%, mtm': computing yoy over it gave growth of a growth, and reading it
    would answer a yoy question with an mtm figure. Either way the cell does not hold the answer.
    """
    kind = _growth_kind(src.table.unit, label)
    return kind if kind not in (None, "yoy") else None


def _claimed_rate_kind(fact: ExtractedFact) -> Optional[str]:
    """The growth kind a percent claim states — the '(mtm)' / '(qtq)' / '(yoy)' after its number.

    One sentence often carries two ('tumbuh 1,1% (yoy) dan terkontraksi sebesar 0,1% (mtm)'), so
    the tag that follows the claimed number wins; without the number in the quote, a quote with a
    single kind decides. None when the quote names no kind.
    """
    quote = fact.context_quote or ""
    tags = [(m.start(), m.group(1).lower()) for m in _GROWTH_KIND_RE.finditer(quote)]
    if not tags:
        return None
    if fact.claimed_value is not None:
        text = f"{abs(fact.claimed_value):.4f}".rstrip("0").rstrip(".")
        whole, _, frac = text.partition(".")
        pattern = re.escape(whole) + (r"\s*,\s*" + re.escape(frac) if frac else "") + r"(?![\d,])"
        number = re.search(r"(?<![\d,.])" + pattern, quote)
        if number:
            following = [kind for pos, kind in tags if pos >= number.end()]
            if following:
                return following[0]
    kinds = {kind for _, kind in tags}
    return kinds.pop() if len(kinds) == 1 else None


# Survey topics a report names in front of a sector: "SBT harga jual Perdagangan", "SBT
# investasi Industri Pengolahan", "prakiraan tingkat inflasi …" (SKDU Tabel 5-7). Such a word
# absent from the table means the claim is about another table. Any other front word stays
# allowed: it usually spells out or describes the row ("Kredit Pemilikan Rumah (KPR)" for
# 'KPR/KPA', "harga rumah tipe menengah" for 'Menengah', "LU …", "defisit neraca …").
_FRONT_TOPIC_WORDS = frozenset({
    "harga", "jual", "inflasi", "investasi", "upah", "margin", "kapasitas", "produksi",
    "tenaga", "kerja",
})


def _breakdown_the_table_lacks(
    fact: ExtractedFact, resolved: List[Tuple[str, float]], src: "_ExcelSource"
) -> List[str]:
    """Words that narrow the claim to a breakdown this table does not hold, else [].

    "Ekspor nonmigas ke Tiongkok" against SEKI Tabel V.1 (no country rows) was answered by the
    all-country 'Nonmigas > Ekspor' row and reported Tidak Sesuai. A word counts when the table
    never mentions it (TableData.words_absent) AND it comes after the last claim word the
    matched row accounts for — the same reading as _is_narrower_than_the_claim: a trailing word
    narrows the series ('… ke Tiongkok', 'impor barang konsumsi'), while one in the middle only
    rewords it ('Uang Beredar Luas (M2)' for a row 'Uang Beredar (M2)').
    """
    narrowing: List[str] = []
    unit_words = {t.lower() for t in re.findall(r"\w+", src.table.unit or "")}
    for point, (label, _) in zip(fact.periods, resolved):
        absent = src.table.words_absent(point.metric_label) - unit_words
        if not absent:
            continue
        claim_tokens = [t.lower() for t in re.findall(r"\w+", point.metric_label)]
        row_tokens = {t.lower() for t in re.findall(r"\w+", label)}
        covered = [
            i for i, t in enumerate(claim_tokens)
            if any(_same_root(t, r) for r in row_tokens)
        ]
        first = covered[0] if covered else len(claim_tokens)
        last = covered[-1] if covered else -1
        narrowing += [
            t for i, t in enumerate(claim_tokens)
            if t in absent and t not in narrowing
            and (i > last or (i < first and t in _FRONT_TOPIC_WORDS))
        ]
    return narrowing


# "kewajiban neto sebesar 229,6", "aset neto 37,0": the number that follows is a NET position.
_NET_POSITION = re.compile(r"\b(kewajiban|aset)\s+(neto|bersih)\b", re.IGNORECASE)
# A gross side named after the net position takes the figures that follow it.
_GROSS_SIDE = re.compile(r"\b(KFLN|AFLN|finansial\s+luar\s+negeri|bruto)\b", re.IGNORECASE)
_QUOTE_NUMBER = re.compile(r"\d[\d.]*(?:,\d+)?")
_BALANCE_SIDES = ("aset", "kewajiban", "afln", "kfln")


def _is_a_net_figure(fact: ExtractedFact) -> bool:
    """True when the sentence presents the claimed number as a net position.

    The number counts as net when, within its sentence, the nearest earlier mention of a side
    is 'kewajiban/aset neto' ("kewajiban neto sebesar 229,6 …, lebih rendah dibandingkan dengan
    237,7"), not a gross one ("… dari penurunan posisi KFLN … menjadi 389,7").
    """
    if fact.claimed_value is None:
        return False
    quote = fact.context_quote or ""
    for m in _QUOTE_NUMBER.finditer(quote):
        try:
            if abs(_id_number(m.group(0).rstrip(".")) - abs(fact.claimed_value)) > 1e-9:
                continue
        except ValueError:
            continue
        before = re.split(r"[.;]\s", quote[:m.start()])[-1]
        nets = list(_NET_POSITION.finditer(before))
        if nets and not _GROSS_SIDE.search(before[nets[-1].end():]):
            return True
    return False


def _gross_row_for_a_net_number(fact: ExtractedFact, resolved: List[Tuple[str, float]]) -> Optional[str]:
    """The matched row, when the claimed number is a net position but the row is one gross side.

    PII prints "investasi portofolio mencatat kewajiban neto sebesar 229,6" for each component,
    while SEKI V.39 holds only 'Aset > Investasi Portofolio' and 'Kewajiban > Investasi
    Portofolio' (and a top-level 'Aset Lainnya'). The claim's 'kewajiban' matched the liability
    row (389,7) and was reported Tidak Sesuai. See _is_a_net_figure for which numbers count.
    """
    if not _is_a_net_figure(fact):
        return None
    for label, _ in resolved:
        words = label.split(">")[0].strip().lower().split()
        if words and words[0] in _BALANCE_SIDES:
            return label
    return None


def _is_percent_unit(unit: Optional[str]) -> bool:
    return bool(unit and (unit.strip().lower().startswith("persen") or "%" in unit))


def _is_plain_index(unit: Optional[str]) -> bool:
    """True for an index read as a level ('Indeks', 'Indeks (2018=100)'), not one printed in
    percent ('(%, Indeks)' — PMI-BI's diffusion index, reported as '52,03%')."""
    return bool(unit and re.search(r'\b(indeks|index)\b', unit, re.IGNORECASE) and "%" not in unit)


def _is_narrower_than_the_claim(
    fact: ExtractedFact, resolved: List[Tuple[str, float]]
) -> bool:
    """True when a matched row adds a word the claim never used, making it a subset of it.

    Only consulted once a better-named source has already been dropped for holding no data, so
    the question is narrow: may THIS row answer in its place?

    Where the extra word SITS is what tells the two cases apart, and BI's labels are consistent
    about it — they read "Category Subject Qualifier":

      claim 'Kredit'      vs row 'Kredit Properti'        -> 'properti' trails: a breakdown
      claim 'Giro Rupiah' vs row 'Simpanan Giro Rupiah'   -> 'simpanan' leads: the family it is in

    So a word added AFTER the claim's own terms narrows the series and disqualifies the row,
    while one added before it only names the section the report files it under. Answering a
    credit claim from the property breakdown is answering a different question; answering
    "giro rupiah" from 'Simpanan Giro Rupiah' is the same series under Lampiran 2's wording.
    """
    for point, (label, _value) in zip(fact.periods, resolved):
        claim_words = _sig_words(point.metric_label or "")
        words = _rejoined_words(label, claim_words)
        matched = [i for i, w in enumerate(words) if w in claim_words]
        if not matched:
            continue
        trailing = [w for w in words[max(matched) + 1:]
                    if len(w) > 2 and w not in claim_words]
        if trailing:
            return True
    return False


# What a BI report's sentence — and a table's title — is about. Matched on the text and on a
# copy with the whitespace squeezed out, since PDF text breaks words mid-way ('k redit').
_SENTENCE_SUBJECTS = {
    "kredit": re.compile(r"kredit|pembiayaan", re.IGNORECASE),
    "dpk": re.compile(r"danapihakketiga|dpk|simpananmasyarakat", re.IGNORECASE),
    "m2": re.compile(r"uangberedar|m2\b", re.IGNORECASE),
    "m0": re.compile(r"uangprimer|m0\b", re.IGNORECASE),
}


def _subjects(text: str) -> set:
    squeezed = re.sub(r"\s+", "", text or "")
    return {name for name, pattern in _SENTENCE_SUBJECTS.items() if pattern.search(squeezed)}


def _source_is_about_the_sentence(fact: ExtractedFact, src: "_ExcelSource") -> bool:
    """False when a claim named only 'Total' is being answered by a table about something else.

    The extractor names a claim after the row it expects to hit, and for a report's headline
    figures that row is just 'Total'. The name then carries no subject at all, every table's
    'Total' covers it equally, and whichever table came first answered: on the M2 report for
    Agustus 2026, "Penyaluran kredit … tercatat sebesar Rp9.019,3 triliun atau tumbuh 13,3%
    (yoy)" was checked against Tabel 4 — Penghimpunan Dana Pihak Ketiga — and refuted with
    DPK's 9.732,8 and 7,7% while three credit tables printed exactly the claimed figures.

    So when every point's name is an aggregate word, the SENTENCE says what the total is of:
    a table whose title is about another subject may not answer. A sentence naming several
    subjects, or a title naming none, rules nothing out.
    """
    names = [p.metric_label or "" for p in fact.periods]
    if not names or not all(
        re.sub(r"[^a-z]", "", n.lower()) in ("total", "jumlah") for n in names
    ):
        return True
    wanted = _subjects(fact.context_quote)
    covered = _subjects(src.table.title)
    return not wanted or not covered or bool(wanted & covered)


def _answers_one_group_of_it(
    fact: ExtractedFact, resolved: List[Tuple[str, float]]
) -> bool:
    """True when a claim about a whole series resolved to ONE group of its breakdown.

    SK-Agustus-2026's appendix lost the national IEKLK row of Tabel 1, and "IEKLK tercatat
    125,7" was answered by Tabel 4's 'Indeks Ekspektasi Ketersediaan Lapangan Kerja (IEKLK) >
    SMA' — the high-school respondents, 125,0 — and refuted. The row contains every word of the
    claim, which is all the containment tiers ask; what gives it away is its last level, which
    names a group the claim never mentioned. Unlike _is_narrower_than_the_claim this holds
    whether or not a better source was there: a group's figure is never the whole's. A claim
    that is itself qualified, or a row whose last level the claim does name ('Total >
    Korporasi' for "DPK korporasi"), is not affected.
    """
    for point, (label, _value) in zip(fact.periods, resolved):
        if QUAL_SEP in (point.metric_label or "") or QUAL_SEP not in label:
            continue
        # In the tables' vocabulary: "average propensity to consume ratio" names the 'Konsumsi'
        # level of 'Total > Konsumsi' though no word of it appears there (see _REPORT_TERMS).
        claim_words = _sig_words(_expand_report_terms(point.metric_label or ""))
        if not claim_words:
            continue
        # Any level the claim does not name is a group of it — at the end ('IEKLK > SMA') or at
        # the front, where the per-city table files each index under its city ('Medan > IKK').
        # An aggregate level ('Total > …') names no group and is let through.
        for level in label.split(QUAL_SEP):
            words = _sig_words(level) - _UNNAMED_PARTS
            if words and not (words & claim_words):
                return True
    return False


def _rejoined_words(label: str, claim_words: set) -> List[str]:
    """The label's words, with the PDF's mid-word splits put back together where the claim says
    how. 'Kredit Konsum si (KK)' reads as ['kredit', 'konsumsi', 'kk'] for a claim about Kredit
    Konsumsi — without this the stray 'konsum' looks like a word the row adds, and the row that
    answers the claim exactly gets rejected as a narrower series.
    """
    raw = re.findall(r"\w+", (label or "").lower())
    out: List[str] = []
    i = 0
    while i < len(raw):
        if raw[i] in claim_words:
            out.append(raw[i])
            i += 1
            continue
        joined = next(
            ("".join(raw[i:i + span]) for span in (2, 3)
             if i + span <= len(raw) and "".join(raw[i:i + span]) in claim_words),
            None,
        )
        if joined:
            out.append(joined)
            i += len(joined) and next(
                span for span in (2, 3)
                if i + span <= len(raw) and "".join(raw[i:i + span]) == joined
            )
            continue
        out.append(raw[i])
        i += 1
    return out


def _can_answer(
    fact: ExtractedFact, resolved: List[Tuple[str, float]], src: _ExcelSource
) -> bool:
    """Whether this source holds what the OPERATION needs, not just the points it names.

    Resolving a claim only checks the periods it mentions, so a table that carries the current
    month but nothing to compare it against still wins the source ranking on the strength of its
    row name — and then answers "no data", while a table that could have answered was never
    consulted. Tabel 1 of sample_data/M2-Juli-2026.pdf is exactly that: two columns, Jun and
    Jul, no growth block and no year-ago column, but its row is spelled 'Uang Beredar Luas (M2)'
    — the claim's wording exactly, where Tabel 2 and Lampiran 2 both say 'Uang Beredar (M2)'. So
    "M2 tumbuh 8,3% (yoy)" came back Tidak Cukup Data against a report that prints 8,3 twice.

    This does not loosen matching. A source dropped here would have returned Inconclusive
    anyway; the only thing that changes is which source gets to be the answer, and
    `_coverage_score` still keeps a claim inside the tables that cover its scope words.
    """
    if fact.operation != "yoy_growth" or not resolved:
        return True
    # Same two ways of getting a growth figure that _compute_yoy_growth uses, in the same order.
    if _is_growth_series(src.table.unit):
        return True
    point = fact.periods[0]
    matched_label = resolved[0][0]
    _prior_label, prior = src.table.lookup_fuzzy(matched_label, point.year - 1, point.month)
    return prior is not None


def _compute_yoy_growth(fact: ExtractedFact, resolved: List[Tuple[str, float]], src: _ExcelSource) -> FactVerificationResult:
    p = fact.periods[0]
    matched_label, curr_raw = resolved[0]
    matched_source = src.label
    prior_year = p.year - 1
    current_period = PeriodResult(metric_label=matched_label, year=p.year, month=p.month, excel_value=round(curr_raw, 4))

    other_kind = _not_a_yoy_cell(fact, matched_label, src)
    if other_kind:
        return _make_result(
            fact, [current_period], matched_source, fact.claimed_value, "persen_yoy", None,
            "persen_yoy", None, "Inconclusive",
            reasoning=(
                f"Excel [{matched_source}] ({matched_label}) berisi pertumbuhan {other_kind} "
                f"('{src.table.unit}'), bukan yoy — tidak menjawab klaim {fact.claimed_value}% yoy."
            ),
        )

    if _is_growth_series(src.table.unit):
        # The cell already holds the answer, so read it instead of computing one (the same
        # division of labour every other lookup follows). No prior-year column is needed,
        # which also stops such a table from being ruled out for lacking one.
        computed = round(curr_raw, 4)
        delta, verdict = _numeric_verdict(fact.claimed_value, computed)
        return _make_result(
            fact, [current_period], matched_source,
            fact.claimed_value, "persen_yoy", computed, "persen_yoy", delta, verdict,
            reasoning=(
                f"PDF: {fact.claimed_value}% yoy | "
                f"Excel [{matched_source}] ({matched_label}): {computed}% yoy "
                f"(dibaca langsung; tabel ini sudah dalam satuan '{src.table.unit}') | "
                f"Δ = {delta}% → {'within' if verdict == 'Entailed' else 'exceeds'} "
                f"tolerance {MATCH_TOLERANCE}%"
            ),
        )

    prior_label, prior_raw = src.table.lookup_fuzzy(matched_label, prior_year, p.month)

    if prior_raw is None:
        return _make_result(
            fact, [current_period], matched_source, fact.claimed_value, "persen_yoy", None, "persen_yoy", None,
            "Inconclusive",
            reasoning=(
                f"No data found in Excel [{matched_source}] for metric '{matched_label}' "
                f"at {p.month} {prior_year} (needed for yoy denominator)."
            ),
        )
    if prior_raw == 0:
        return _make_result(
            fact, [current_period], matched_source, fact.claimed_value, "persen_yoy", None, "persen_yoy", None,
            "Inconclusive",
            reasoning=f"Prior-year value for '{matched_label}' at {p.month} {prior_year} is zero; yoy undefined.",
        )
    # A "prior-year level" that IS the claimed growth is the growth cell, read as a level. BI
    # snippet tables print both under one caption, and whenever a lookup or a cell pointer
    # crossed that boundary the result was the same absurd shape: 2.232,2 over 14,3 reported as
    # 15.509% against a claim of 14,3%. Cheap backstop for the cases the structural guards
    # (_split_unit_blocks, pointer_column_matches) do not catch — a genuine level that happens
    # to equal its own growth rate to four decimals does not occur in these series.
    if fact.claimed_value is not None and round(prior_raw, 4) == round(fact.claimed_value, 4):
        return _make_result(
            fact, [current_period], matched_source, fact.claimed_value, "persen_yoy", None, "persen_yoy", None,
            "Inconclusive",
            reasoning=(
                f"Nilai '{matched_label}' pada {p.month} {prior_year} di [{matched_source}] "
                f"({prior_raw}) sama persis dengan angka pertumbuhan yang diklaim — hampir pasti "
                f"sel '%, yoy', bukan level tahun lalu. Pembanding tahun lalu tidak tersedia."
            ),
        )

    computed = round((curr_raw - prior_raw) / abs(prior_raw) * 100, 4)
    tolerance = _growth_tolerance(curr_raw, prior_raw)
    delta, verdict = _numeric_verdict(fact.claimed_value, computed, tolerance)
    periods = [current_period, PeriodResult(metric_label=prior_label, year=prior_year, month=p.month, excel_value=round(prior_raw, 4))]
    return _make_result(
        fact, periods, matched_source, fact.claimed_value, "persen_yoy", computed, "persen_yoy", delta, verdict,
        reasoning=(
            f"PDF: {fact.claimed_value}% yoy | "
            f"Excel [{matched_source}] ({matched_label}): {computed}% yoy "
            f"({curr_raw:.2f} vs {prior_raw:.2f} {src.table.unit}) | "
            f"Δ = {delta}% → {'within' if verdict == 'Entailed' else 'exceeds'} tolerance {tolerance}%"
        ),
    )


_GDP_SHARE_SUFFIX = re.compile(r"\s*\(\s*%\s*(dari\s+|of\s+)?(PDB|GDP)\s*\)\s*$", re.IGNORECASE)


def _gdp_share_row(numerator: str, denominator: str) -> bool:
    """True when `denominator` is the "(% PDB)" row of the numerator's own metric."""
    m = _GDP_SHARE_SUFFIX.search(denominator)
    if not m:
        return False
    base = denominator[:m.start()].strip().lower()
    return bool(base) and base in numerator.lower()


def _compute_ratio(fact: ExtractedFact, resolved: List[Tuple[str, float]], src: _ExcelSource) -> FactVerificationResult:
    (label_a, raw_a), (label_b, raw_b) = resolved
    matched_source = src.label
    p_a, p_b = fact.periods
    periods = [
        PeriodResult(metric_label=label_a, year=p_a.year, month=p_a.month, excel_value=round(raw_a, 4)),
        PeriodResult(metric_label=label_b, year=p_b.year, month=p_b.month, excel_value=round(raw_b, 4)),
    ]
    if raw_b == 0:
        return _make_result(
            fact, periods, matched_source, fact.claimed_value, fact.unit, None, fact.unit, None, "Inconclusive",
            reasoning=f"Nilai penyebut '{label_b}' bernilai nol; rasio tidak terdefinisi.",
        )
    if _GDP_SHARE_SUFFIX.search(label_b) and not _gdp_share_row(label_a, label_b):
        # SEKI V.1 prints a (% PDB) row only for the current account; dividing the capital
        # account by it gave -881%. Another account's share of GDP is no denominator.
        return _make_result(
            fact, periods, matched_source, fact.claimed_value, fact.unit, None, fact.unit, None, "Inconclusive",
            reasoning=(
                f"Baris '{label_b}' adalah rasio terhadap PDB untuk metrik lain, bukan untuk "
                f"'{label_a}'; tabel tidak memuat rasio '{label_a}' terhadap PDB."
            ),
        )
    share = _gdp_share_row(label_a, label_b)
    if share:
        # The denominator row already IS the claim's share of GDP (SEKI V.1 "Transaksi Berjalan
        # (% PDB)"); dividing the balance by it is meaningless. Read the row as the answer.
        computed = round(raw_b, 4)
        delta, verdict = _numeric_verdict(fact.claimed_value, computed)
        word = _NEGATIVE_BALANCE_WORDS.search(fact.context_quote or "")
        size = ""
        if verdict != "Entailed" and word and computed < 0 and (fact.claimed_value or 0) > 0:
            delta, verdict = _numeric_verdict(fact.claimed_value, -computed)
            size = f" Laporan menulis besarnya {word.group(0).lower()}; tabel mencatat rasionya negatif."
        return _make_result(
            fact, periods, matched_source, fact.claimed_value, fact.unit, computed, fact.unit, delta, verdict,
            reasoning=(
                f"PDF: {fact.claimed_value} {fact.unit} dari PDB | "
                f"Excel [{matched_source}]: baris '{label_b}' = {computed} | "
                f"Rasio terhadap PDB dibaca langsung dari baris itu.{size} "
                f"Δ = {delta} → {'within' if verdict == 'Entailed' else 'exceeds'} tolerance {MATCH_TOLERANCE}"
            ),
        )
    computed = raw_a / raw_b
    if fact.unit and "persen" in fact.unit.lower():
        computed *= 100
    computed = round(computed, 4)
    delta, verdict = _numeric_verdict(fact.claimed_value, computed)
    return _make_result(
        fact, periods, matched_source, fact.claimed_value, fact.unit, computed, fact.unit, delta, verdict,
        reasoning=(
            f"PDF: rasio = {fact.claimed_value} {fact.unit} | "
            f"Excel [{matched_source}]: {label_a}={round(raw_a, 4)} / {label_b}={round(raw_b, 4)} = {computed} {fact.unit} | "
            f"Δ = {delta} → {'within' if verdict == 'Entailed' else 'exceeds'} tolerance {MATCH_TOLERANCE}"
        ),
    )


# A negative balance named as the SUBJECT of the trend verb: "defisit transaksi berjalan meningkat",
# "kewajiban neto PII menurun". The noun must come before the verb in the same clause, so "transaksi
# berjalan membaik, dari defisit 8,2" stays a claim about the signed balance. "kontraksi" is left
# out: it names a negative growth RATE, where "kontraksi menurun" is ambiguous.
_DEFICIT_NOUNS = r"(defisit|kewajiban\s+neto|net\s+liabilit\w*|arus\s+keluar\s+neto|net\s+outflow)"
_DEFICIT_TREND = re.compile(
    r"\b" + _DEFICIT_NOUNS + r"\b[^,.;]{0,80}?"
    r"\b(?P<verb>meningkat|naik|melebar|membesar|bertambah|menurun|turun|menyempit|mengecil|berkurang"
    r"|lebih\s+(tinggi|rendah|besar|kecil|lebar|sempit))\b",
    re.IGNORECASE,
)
# The same, noun first: "peningkatan defisit neraca jasa", "penyempitan defisit TB".
_DEFICIT_TREND_NOUN_FIRST = re.compile(
    r"\b(?P<verb>peningkatan|kenaikan|pelebaran|penambahan|penurunan|penyempitan|pengurangan)\s+"
    + _DEFICIT_NOUNS + r"\b",
    re.IGNORECASE,
)
# The words among them that say the size went DOWN; every other one says it went up.
_SIZE_DOWN = re.compile(
    r"menurun|turun|menyempit|mengecil|berkurang|lebih\s+(rendah|kecil|sempit)"
    r"|penurunan|penyempitan|pengurangan",
    re.IGNORECASE,
)


def _compute_trend(fact: ExtractedFact, resolved: List[Tuple[str, float]], src: _ExcelSource) -> FactVerificationResult:
    matched_source = src.label
    used_periods = list(fact.periods)
    labelled = list(resolved)  # [(matched_label, value)]

    # Auto-complete a single-point trend: reports often state only the current period
    # ("SBT meningkat" in a Tw II 2026 report) and leave the QoQ baseline implicit. Pull
    # the metric's immediately-preceding period from the table itself so the claim is
    # checkable without relying on the LLM to guess the earlier period.
    if len(labelled) == 1:
        p0 = used_periods[0]
        if p0.col_label is None and p0.year is not None and p0.month is not None:
            prev = _previous_period(src.table, labelled[0][0], p0.year, p0.month)
            if prev is not None:
                (py, pm), pv = prev
                labelled = [(labelled[0][0], pv), labelled[0]]
                used_periods = [PeriodPoint(metric_label=labelled[0][0], year=py, month=pm), p0]

    # A trend runs forward in time, whatever order the claim named its periods in: "meningkat
    # dibandingkan dengan Juli 2026" in an August report arrives as [Agustus, Juli], and read in
    # that order SK-Agustus-2026's IKK, 116,8 -> 118,5, came back as a fall. Only reordered when
    # every point is dated; a categorical claim keeps the order it was given.
    if all(p.year is not None and p.month is not None for p in used_periods):
        order = sorted(range(len(used_periods)),
                       key=lambda i: (used_periods[i].year, _period_ordinal(used_periods[i].month)))
        used_periods = [used_periods[i] for i in order]
        labelled = [labelled[i] for i in order]

    values = [raw for (_, raw) in labelled]
    periods = _build_periods(used_periods, labelled, values)

    if len(values) < 2:
        return _make_result(
            fact, periods, matched_source, None, None, None, src.table.unit, None, "Inconclusive",
            reasoning=(
                f"Klaim tren '{fact.operation}' hanya menyebut satu periode dan tabel tidak "
                f"punya periode sebelumnya untuk '{labelled[0][0]}' sebagai pembanding."
            ),
        )

    # "defisit … meningkat" on a balance stored negative (−5,1 → −8,2) means the deficit grew, which
    # the signed numbers read as a fall. Judge such a claim on the size of the balance.
    size_note = ""
    operation = fact.operation
    deficit = _DEFICIT_TREND.search(fact.context_quote or "") or _DEFICIT_TREND_NOUN_FIRST.search(
        fact.context_quote or ""
    )
    if all(v < 0 for v in values) and deficit:
        values = [-v for v in values]
        size_note = " (dinilai pada besarnya defisit/kewajiban neto)"
        # The extractor reads "defisit … melebar" as the balance falling in one run and the
        # deficit growing in the next; the verb itself says which way the size moved.
        if operation in ("is_increasing", "is_decreasing"):
            grew = _SIZE_DOWN.search(deficit.group("verb")) is None
            operation = "is_increasing" if grew else "is_decreasing"

    band_note = ""
    if operation == "is_increasing":
        ok = all(values[i + 1] >= values[i] for i in range(len(values) - 1))
    elif operation == "is_decreasing":
        ok = all(values[i + 1] <= values[i] for i in range(len(values) - 1))
    else:  # is_stable — see _stable_band
        bands = [_stable_band(values[i], values[i + 1]) for i in range(len(values) - 1)]
        ok = all(
            abs(values[i + 1] - values[i]) <= bands[i] for i in range(len(values) - 1)
        )
        band_note = f" (ambang stabil ±{round(max(bands), 4)})"

    verdict = "Entailed" if ok else "Refuted"
    breakdown = ", ".join(f"{p.month} {p.year}={round(v, 4)}" for p, (_, v) in zip(used_periods, labelled))
    return _make_result(
        fact, periods, matched_source, None, None, None, src.table.unit, None, verdict,
        reasoning=(
            f"Klaim tren '{fact.operation}' untuk '{fact.display_label}' | "
            f"Excel [{matched_source}]: {breakdown}{band_note}{size_note} | "
            f"{'sesuai' if ok else 'tidak sesuai'} dengan klaim"
        ),
    )


def _compute_threshold(
    fact: ExtractedFact, resolved: List[Tuple[str, float]], src: _ExcelSource
) -> FactVerificationResult:
    """Verify a value-above/below-a-bound claim (claimed_value = the threshold).

    Dimensionless comparison: the metric's value at the period is checked directly against
    the threshold with a strict inequality (a PMI index of exactly 50 is neither expansion
    nor contraction). No unit conversion — the bound is an index/percent level.

    The extractor is asked for exactly one point but sometimes gives several ("IKK Mei dan Juni
    berada di level optimis"). Every point must then clear the bound, and the one reported as
    the computed value is the first that fails — or the first point when all pass. Reading only
    the first point used to leave `_build_periods` one value short, and the IndexError took the
    whole report down with it.
    """
    values = [round(raw, 4) for _, raw in resolved]
    label, value = resolved[0][0], values[0]
    matched_source = src.label
    periods = _build_periods(fact.periods, resolved, values)
    threshold = fact.claimed_value
    if threshold is None:
        return _make_result(
            fact, periods, matched_source, None, fact.unit, value, fact.unit, None, "Inconclusive",
            reasoning="Klaim ambang tanpa nilai ambang yang jelas; tidak dapat dinilai.",
        )
    if fact.operation == "above_threshold":
        passes, arah = (lambda v: v > threshold), "di atas"
    else:
        passes, arah = (lambda v: v < threshold), "di bawah"
    failing = [i for i, v in enumerate(values) if not passes(v)]
    ok = not failing
    if failing:
        label, value = resolved[failing[0]][0], values[failing[0]]
    verdict = "Entailed" if ok else "Refuted"
    if len(values) > 1:
        breakdown = ", ".join(
            f"{_point_desc(p)}={v}" for p, v in zip(fact.periods, values)
        )
    else:
        breakdown = f"{label} = {value}"
    return _make_result(
        fact, periods, matched_source, threshold, fact.unit, value, fact.unit, None, verdict,
        reasoning=(
            f"Klaim: {label} {arah} ambang {threshold} | "
            f"Excel [{matched_source}]: {breakdown} | "
            f"{'sesuai' if ok else 'tidak sesuai'} dengan klaim"
        ),
    )


def _reinterpret_diff_as_value(
    fact: ExtractedFact,
    resolved: List[Tuple[str, float]],
    converted: List[float],
    computed_diff: float,
    src: _ExcelSource,
) -> Optional[FactVerificationResult]:
    """A 'diff' whose claimed number is really one of the endpoint LEVELS, re-checked as a value.

    "Posisi cadangan devisa … pada akhir Mei 2026 tercatat 144,9 miliar dolar AS, lebih rendah
    dibandingkan dengan posisi akhir April 2026 sebesar 146,2 miliar dolar AS" states two levels
    and a direction — it states no difference at all. The extractor read the comparison as a
    subtraction and put April's level in claimed_value, so 146,2 was checked against a difference
    of -1,3 and Refuted: a sentence that agrees with the reference table to three digits was
    reported as wrong.

    The mislabelling is unambiguous from the numbers alone — the claimed "difference" misses the
    real one by 147,5 while matching an endpoint to 0,002 — and this is only consulted after the
    diff check has already failed, so a claim that genuinely states its difference is never
    rerouted. Returns None when no endpoint matches, leaving the Refuted verdict to stand.
    """
    if fact.claimed_value is None:
        return None
    match = next(
        (i for i, v in enumerate(converted) if abs(fact.claimed_value - v) <= MATCH_TOLERANCE),
        None,
    )
    if match is None:
        return None

    label = resolved[match][0]
    point = fact.periods[match]
    value = round(converted[match], 4)
    delta = round(abs(fact.claimed_value - value), 4)
    result = _make_result(
        fact,
        [PeriodResult(
            metric_label=label, year=point.year, month=point.month,
            col_label=point.col_label, excel_value=value,
        )],
        src.label, fact.claimed_value, fact.unit, value, fact.unit, delta, "Entailed",
        reasoning=(
            f"PDF: {fact.claimed_value} {fact.unit} | "
            f"Klaim ini ditandai sebagai selisih, tetapi nilainya cocok dengan level "
            f"{_point_desc(point)} ({value} {fact.unit}) dan bukan dengan selisih antarperiode "
            f"({computed_diff} {fact.unit}) — dinilai sebagai klaim nilai. | "
            f"Excel [{src.label}] ({label}): {value} {fact.unit} | "
            f"Δ = {delta} → within tolerance {MATCH_TOLERANCE}"
        ),
    )
    # The operation is corrected too, so the reported claim type matches what was actually checked.
    return result.model_copy(update={"operation": "value"})


# Words that make a report print the SIZE of a negative balance as a positive number: "defisit
# 12,5 miliar dolar AS", "kewajiban neto 197,4", "arus keluar neto 2,2", "kontraksi 0,1".
_NEGATIVE_BALANCE_WORDS = re.compile(
    r"\b(defisit|kewajiban\s+neto|net\s+liabilit\w*|arus\s+keluar\s+neto|net\s+outflow"
    r"|(ter)?kontraksi)\b",
    re.IGNORECASE,
)


def _reinterpret_signed_level(
    fact: ExtractedFact,
    resolved: List[Tuple[str, float]],
    periods: List[PeriodResult],
    computed: float,
    src: _ExcelSource,
) -> Optional[FactVerificationResult]:
    """A value claim that prints the size of a negative balance, re-checked against its magnitude.

    NPI: "defisit transaksi berjalan … tercatat sebesar 12,5 miliar dolar AS" — SEKI Tabel V.1
    stores the balance, −12,49, so the claim was Refuted although report and table agree. PII
    does the same with "kewajiban neto 197,4" against a net position of −197,43.

    Only consulted after the value check has failed, and only when all three hold: the sentence
    names a negative balance (_NEGATIVE_BALANCE_WORDS), the table value is negative, and the
    claimed number matches its magnitude. A negative claim against a positive cell, or a number
    that matches nothing, keeps its Refuted verdict. Returns None in those cases.
    """
    if fact.claimed_value is None or computed >= 0 or fact.claimed_value <= 0:
        return None
    word = _NEGATIVE_BALANCE_WORDS.search(fact.context_quote or "")
    if word is None:
        return None
    delta, verdict = _numeric_verdict(fact.claimed_value, -computed)
    if verdict != "Entailed":
        return None
    return _make_result(
        fact, periods, src.label, fact.claimed_value, fact.unit, computed, fact.unit, delta,
        "Entailed",
        reasoning=(
            f"PDF: {fact.claimed_value} {fact.unit} ('{word.group(0)}') | "
            f"Excel [{src.label}] ({resolved[0][0]}): {computed} {fact.unit} | "
            f"Laporan menulis besarnya {word.group(0).lower()} sebagai angka positif; tabel "
            f"mencatat saldonya negatif. Δ besaran = {delta} → within tolerance {MATCH_TOLERANCE}"
        ),
    )


def _compute_operation(
    fact: ExtractedFact, resolved: List[Tuple[str, float]], factor: float, src: _ExcelSource
) -> FactVerificationResult:
    op = fact.operation
    matched_source = src.label

    if op in _THRESHOLD_OPS:
        return _compute_threshold(fact, resolved, src)

    if op == "value":
        computed = round(resolved[0][1] / factor, 4)
        periods = _build_periods(fact.periods, resolved, [resolved[0][1] / factor])
        delta, verdict = _numeric_verdict(fact.claimed_value, computed)
        if verdict == "Refuted":
            recovered = _reinterpret_signed_level(fact, resolved, periods, computed, src)
            if recovered is not None:
                return recovered
        return _make_result(
            fact, periods, matched_source, fact.claimed_value, fact.unit, computed, fact.unit, delta, verdict,
            reasoning=(
                f"PDF: {fact.claimed_value} {fact.unit} | "
                f"Excel [{matched_source}] ({resolved[0][0]}): {computed} {fact.unit} | "
                f"Δ = {delta} → {'within' if verdict == 'Entailed' else 'exceeds'} tolerance {MATCH_TOLERANCE}"
            ),
        )

    if op == "yoy_growth":
        return _compute_yoy_growth(fact, resolved, src)

    if op in ("average", "sum"):
        converted = [raw / factor for (_, raw) in resolved]
        computed = round(sum(converted) / len(converted), 4) if op == "average" else round(sum(converted), 4)
        periods = _build_periods(fact.periods, resolved, converted)
        delta, verdict = _numeric_verdict(fact.claimed_value, computed)
        if verdict == "Refuted" and op == "sum":
            # "transaksi modal dan finansial ... mengalami defisit sebesar 4,8" over two rows
            # that add up to -4,76: the same printed-size rule as a single level.
            recovered = _reinterpret_signed_level(fact, resolved, periods, computed, src)
            if recovered is not None:
                return recovered
        label = "rata-rata" if op == "average" else "total"
        breakdown = ", ".join(f"{_point_desc(p)}={round(v, 4)}" for p, v in zip(fact.periods, converted))
        return _make_result(
            fact, periods, matched_source, fact.claimed_value, fact.unit, computed, fact.unit, delta, verdict,
            reasoning=(
                f"PDF: {label} = {fact.claimed_value} {fact.unit} | "
                f"Excel [{matched_source}] ({label} dari {breakdown}) = {computed} {fact.unit} | "
                f"Δ = {delta} → {'within' if verdict == 'Entailed' else 'exceeds'} tolerance {MATCH_TOLERANCE}"
            ),
        )

    if op == "diff":
        converted = [raw / factor for (_, raw) in resolved]
        computed = round(converted[-1] - converted[0], 4)
        periods = _build_periods(fact.periods, resolved, converted)
        delta, verdict = _numeric_verdict(fact.claimed_value, computed)
        if verdict == "Refuted":
            recovered = _reinterpret_diff_as_value(fact, resolved, converted, computed, src)
            if recovered is not None:
                return recovered
            quote = fact.context_quote or ""
            if all(v < 0 for v in converted) and (
                _DEFICIT_TREND.search(quote) or _DEFICIT_TREND_NOUN_FIRST.search(quote)
            ):
                # "Kewajiban neto PII ... menurun sebesar 25,6" on -223,0 -> -197,4: the change
                # the report states is in the size of the net liabilities, not in the balance.
                size_change = round(converted[0] - converted[-1], 4)
                size_delta, size_verdict = _numeric_verdict(fact.claimed_value, size_change)
                if size_verdict == "Entailed":
                    return _make_result(
                        fact, periods, matched_source, fact.claimed_value, fact.unit, size_change,
                        fact.unit, size_delta, "Entailed",
                        reasoning=(
                            f"PDF: perubahan = {fact.claimed_value} {fact.unit} | "
                            f"Excel [{matched_source}]: besarnya {round(-converted[0], 4)} → "
                            f"{round(-converted[-1], 4)} = {size_change} {fact.unit} "
                            f"(dinilai pada besarnya defisit/kewajiban neto) | "
                            f"Δ = {size_delta} → within tolerance {MATCH_TOLERANCE}"
                        ),
                    )
        return _make_result(
            fact, periods, matched_source, fact.claimed_value, fact.unit, computed, fact.unit, delta, verdict,
            reasoning=(
                f"PDF: selisih = {fact.claimed_value} {fact.unit} | "
                f"Excel [{matched_source}]: {round(converted[-1], 4)} - {round(converted[0], 4)} = {computed} {fact.unit} | "
                f"Δ = {delta} → {'within' if verdict == 'Entailed' else 'exceeds'} tolerance {MATCH_TOLERANCE}"
            ),
        )

    if op == "ratio":
        return _compute_ratio(fact, resolved, src)

    return _compute_trend(fact, resolved, src)  # is_increasing / is_decreasing / is_stable


def _inconclusive_result(
    fact: ExtractedFact, best_missing: Optional[List[PeriodPoint]], best_reason: Optional[str]
) -> FactVerificationResult:
    if best_reason:
        reasoning = best_reason
    elif best_missing:
        missing_str = ", ".join(
            f"{p.metric_label} ({p.col_label})" if p.col_label is not None
            else f"{p.metric_label} {p.month} {p.year}"
            for p in best_missing
        )
        reasoning = f"Data tidak ditemukan di sumber Excel manapun untuk: {missing_str}."
    else:
        reasoning = f"Tidak ada sumber Excel yang cocok untuk operasi '{fact.operation}' pada '{fact.display_label}'."
    return _make_result(
        fact, [], None, fact.claimed_value, fact.unit, None, fact.unit, None, "Inconclusive", reasoning=reasoning,
    )


# ---------------------------------------------------------------------------
# Per-fact evaluation (multi-source): tries each source in order, first source with ALL
# periods resolved AND a compatible unit (if the operation needs one) wins.
# ---------------------------------------------------------------------------

def _quote_names_qualifier(metric_label: str, quote: str) -> bool:
    """True when a qualified metric's leaf ('… > Makassar') is actually named in the sentence.

    Separates the two ways a claim ends up carrying a breakdown member. When the sentence names
    it ("… terutama di Makassar, Banten, dan Medan"), the qualifier is the AUTHOR'S and the
    claim really is about that row. When the sentence only hedges ("pada sebagian besar kelompok
    pengeluaran"), the qualifier is the EXTRACTOR'S — it picked one group to stand in for a
    statement about most of them — and reading that row settles a question nobody asked.

    Requiring every significant word of the leaf, not just one, is what makes the distinction
    work: "Pengeluaran Rp2,1 - 3 juta" shares 'pengeluaran' with a sentence about expenditure
    groups in general, and matching on that alone would wave the invented qualifier through.
    """
    if QUAL_SEP not in metric_label:
        return False
    leaf_words = _sig_words(metric_label.rsplit(QUAL_SEP, 1)[1])
    if bool(leaf_words) and leaf_words <= _sig_words(quote):
        return True
    return _quote_bounds_group(metric_label.rsplit(QUAL_SEP, 1)[1], quote)


def _id_number(text: str) -> float:
    return float(text.replace(".", "").replace(",", "."))


# A group's own bounds, from its row name: 'Pengeluaran Rp4,1 - 5 juta', '>Rp5 juta',
# 'Usia 20-30 th', 'Usia >60 th'. The unit word keeps expenditure and age apart.
_GROUP_SPAN_RE = re.compile(
    r"Rp\s*(?P<lo>\d+(?:,\d+)?)\s*-\s*(?P<hi>\d+(?:,\d+)?)\s*juta"
    r"|>\s*Rp\s*(?P<above>\d+(?:,\d+)?)\s*juta"
    r"|(?P<alo>\d+)\s*-\s*(?P<ahi>\d+)\s*(?:th|tahun)\b"
    r"|>\s*(?P<aabove>\d+)\s*(?:th|tahun)\b",
    re.IGNORECASE,
)
# A range the SENTENCE draws: "di atas Rp3,1 juta", "di bawah Rp2 juta", "usia 20-40 tahun",
# "usia >41 tahun".
_QUOTE_SPAN_RE = re.compile(
    r"(?:di\s+atas|lebih\s+dari|>)\s*Rp\s*(?P<above>\d+(?:,\d+)?)\s*juta"
    r"|(?:di\s+bawah|kurang\s+dari|<)\s*Rp\s*(?P<below>\d+(?:,\d+)?)\s*juta"
    r"|usia\s+(?P<alo>\d+)\s*-\s*(?P<ahi>\d+)\s*tahun"
    r"|usia\s+(?:di\s+atas\s+|lebih\s+dari\s+|>\s*)(?P<aabove>\d+)\s*tahun",
    re.IGNORECASE,
)


def _span(match) -> Tuple[str, float, float]:
    """(kind, low, high) of a _GROUP_SPAN_RE or _QUOTE_SPAN_RE match."""
    g = match.groupdict()
    inf = float("inf")
    if g.get("lo"):
        return "rp", _id_number(g["lo"]), _id_number(g["hi"])
    if g.get("above"):
        return "rp", _id_number(g["above"]), inf
    if g.get("below"):
        return "rp", 0.0, _id_number(g["below"])
    if g.get("alo"):
        return "age", float(g["alo"]), float(g["ahi"])
    return "age", float(g["aabove"]), inf


def _quote_bounds_group(group: str, quote: str) -> bool:
    """True when the sentence names this group by a RANGE rather than by its row name.

    SK-Agustus-2026: "IPDG berada pada level optimis untuk kelompok pengeluaran di atas Rp3,1
    juta, sementara kelompok lainnya berada pada level pesimis". 'Rp4,1 - 5 juta' is nowhere in
    the words, yet the author has said exactly which groups: every group inside the range, and —
    since the sentence goes on about the others — every group outside it. Both are checkable
    rows, not an extractor's stand-in for "most groups".
    """
    own = _GROUP_SPAN_RE.search(group)
    if own is None:
        return False
    kind, low, high = _span(own)
    ranges = [_span(m) for m in _QUOTE_SPAN_RE.finditer(quote)]
    ranges = [(lo, hi) for k, lo, hi in ranges if k == kind]
    if not ranges:
        return False
    if any(lo <= low and high <= hi for lo, hi in ranges):
        return True
    outside_all = all(high <= lo or low >= hi for lo, hi in ranges)
    return outside_all and re.search(r"\blain(?:nya)?\b", quote, re.IGNORECASE) is not None


def _groups_the_sentence_names(
    fact: ExtractedFact, sources: List[_ExcelSource]
) -> List[str]:
    """Rows of the claim's own series for the groups its sentence names, when the claim names
    none itself.

    SK-Agustus-2026: "persepsi responden terhadap ketersediaan lapangan pekerjaan saat ini
    meningkat pada responden berpendidikan SMA dan akademi/diploma" came out as one IKLK trend
    with no group, and was checked against the national IKLK — right by coincidence (it rose
    too), about the wrong series. The sentence is about the SMA and Akademi/Diploma rows.

    A row counts when one of its levels IS the claim's series (as the table spells it: every
    word of the rewritten name, so IEKLK does not pass for IKLK) and every other level is named
    in the sentence. Taken from the first table that has any, so the groups come from one
    breakdown.
    """
    if any(QUAL_SEP in (p.metric_label or "") for p in fact.periods):
        return []
    names = {p.metric_label for p in fact.periods}
    if len(names) != 1:
        return []
    quote_words, quote_figures = _label_tokens(fact.context_quote or "")
    quote_figures = set(quote_figures)

    def named_in_quote(level: str) -> bool:
        # Words AND figures: 'Usia 20-30 th' and 'Usia 31-40 th' differ only in their figures.
        words, figures = _label_tokens(level)
        return bool(words | set(figures)) and words <= quote_words and set(figures) <= quote_figures

    for src in sources:
        if src.origin == "chart" or not src.table.row_labels:
            continue
        series_words = _sig_words(src.table._normalise_query(next(iter(names))))
        if not series_words:
            continue
        rows = []
        for label in src.table.row_labels:
            levels = [level.strip() for level in label.split(QUAL_SEP)]
            if len(levels) < 2:
                continue
            is_series = [series_words <= _sig_words(level) for level in levels]
            others = [level for level, own in zip(levels, is_series) if not own]
            if any(is_series) and others and all(named_in_quote(level) for level in others):
                rows.append(label)
        if rows:
            return rows
    return []


def _judge_per_group(
    fact: ExtractedFact, rows: List[str], sources: List[_ExcelSource]
) -> FactVerificationResult:
    """The claim checked on each named group's row; it holds only if it holds for every one."""
    verdicts = []
    for row in rows:
        sub = ExtractedFact(
            operation=fact.operation,
            periods=[PeriodPoint(row, p.year, p.month, p.col_label) for p in fact.periods],
            claimed_value=fact.claimed_value, unit=fact.unit,
            context_quote=fact.context_quote, page_number=fact.page_number,
        )
        verdicts.append((row, _evaluate_fact(sub, sources)))
    if any(r.verdict == "Refuted" for _, r in verdicts):
        overall = "Refuted"
    elif any(r.verdict == "Inconclusive" for _, r in verdicts):
        overall = "Inconclusive"
    else:
        overall = "Entailed"
    head = next((r for _, r in verdicts if r.verdict == overall), verdicts[0][1])
    lines = " | ".join(
        f"{row}: {VERDICT_TEXT.get(r.verdict, r.verdict)} — {r.reasoning.split(' | ')[-1]}"
        for row, r in verdicts
    )
    return head.model_copy(update={
        "verdict": overall,
        "metric_label": fact.display_label,
        "reasoning": (
            f"Kalimat ini tentang kelompok yang disebutnya, bukan '{fact.display_label}' secara "
            f"keseluruhan; diperiksa per kelompok. | {lines}"
        ),
    })


VERDICT_TEXT = {"Entailed": "sesuai", "Refuted": "tidak sesuai", "Inconclusive": "tidak cukup data"}


def _evaluate_fact(fact: ExtractedFact, sources: List[_ExcelSource]) -> FactVerificationResult:
    # A time-only operation over categorical data points can never be computed — fail fast
    # with an explanation instead of scanning sources for data that cannot qualify.
    if fact.operation in _TEMPORAL_ONLY_OPS and any(p.col_label is not None for p in fact.periods):
        return _make_result(
            fact, [], None, fact.claimed_value, fact.unit, None, fact.unit, None, "Inconclusive",
            reasoning=(
                f"Operasi '{fact.operation}' memerlukan data deret waktu (tahun/bulan), "
                f"tetapi klaim ini merujuk atribut non-waktu ('{fact.display_label}')."
            ),
        )

    # A claim attributed to an unnamed subset cannot be settled by reading any single row.
    # Thresholds need this as much as trends do: "IPDG berada pada level optimis pada sebagian
    # besar kelompok pengeluaran" was pinned to the one expenditure group of five that sat below
    # 100 and Refuted — for being exactly the exception the sentence allows for. Only reject when
    # NO data point names its own member, so the per-member facts split out of the same sentence
    # still verify normally (see _SUBSET_PHRASE_RE and _quote_names_qualifier).
    if fact.operation in _TREND_OPS | _THRESHOLD_OPS:
        quote = fact.context_quote or ""
        if _SUBSET_PHRASE_RE.search(quote) and not any(
            _quote_names_qualifier(p.metric_label, quote) for p in fact.periods
        ):
            return _make_result(
                fact, [], None, None, None, None, None, None, "Inconclusive",
                reasoning=(
                    f"Klaim ini hanya berlaku untuk sebagian kelompok/kota tanpa menyebut "
                    f"yang mana, sedangkan '{fact.display_label}' harus diperiksa sebagai satu "
                    "baris tertentu — memeriksanya akan menilai hal yang berbeda dari yang "
                    "diklaim."
                ),
            )

    # A trend or threshold the sentence states for named groups is checked on THOSE groups,
    # whatever name the extractor gave it — see _groups_the_sentence_names.
    if fact.operation in _TREND_OPS | _THRESHOLD_OPS:
        group_rows = _groups_the_sentence_names(fact, sources)
        if group_rows:
            return _judge_per_group(fact, group_rows, sources)

    # A trend must follow ONE metric. When the extractor bundles several DIFFERENT metrics
    # into one fact (e.g. "SBT meningkat pada KMK, KI, dan KK" — three metrics at the same
    # quarter), monotonicity would be checked across unrelated series and wrongly Refuted;
    # that is the extractor mis-structuring the claim, so return Inconclusive (it should
    # have been one trend per metric). A single-period trend is NOT rejected here — the
    # baseline is auto-completed from the table in _compute_trend.
    if fact.operation in _TREND_OPS:
        distinct_metrics = {p.metric_label.strip().lower() for p in fact.periods}
        if len(distinct_metrics) > 1:
            return _make_result(
                fact, [], None, None, None, None, None, None, "Inconclusive",
                reasoning=(
                    f"Klaim tren '{fact.operation}' harus mengikuti satu metrik, tetapi klaim ini "
                    f"mencakup {len(distinct_metrics)} metrik berbeda — seharusnya dipecah menjadi "
                    "satu tren per metrik."
                ),
            )

    needs_unit = fact.operation in _LEVEL_OPS
    best_missing: Optional[List[PeriodPoint]] = None
    best_reason: Optional[str] = None
    # Rows that named the claim but held none of the data the operation needs — see _can_answer
    # and _is_narrower_than_the_claim.
    dropped_for_lack_of_data: List[str] = []
    displaced_source: Optional[str] = None
    # One _Candidate per source that resolved every data point — see _resolution_score. The
    # best-scoring one produces the verdict; the rest become source_values and can raise a
    # conflict (see _attach_source_comparison).
    candidates: List[_Candidate] = []

    for src in sources:
        resolved, missing, col_units = _try_resolve(fact.periods, src)
        if missing:
            if best_missing is None or len(missing) < len(best_missing):
                best_missing = missing
            continue
        other_kind = (
            _not_a_yoy_cell(fact, resolved[0][0], src) if fact.operation == "yoy_growth" else None
        )
        if other_kind:
            if best_reason is None:
                best_reason = (
                    f"Excel [{src.label}] ({resolved[0][0]}) berisi pertumbuhan {other_kind} "
                    f"('{src.table.unit}'), bukan yoy — tidak menjawab klaim yoy ini."
                )
            continue
        if not _can_answer(fact, resolved, src):
            if best_reason is None:
                point = fact.periods[0]
                best_reason = (
                    f"No data found in Excel [{src.label}] for metric '{resolved[0][0]}' "
                    f"at {point.month} {point.year - 1} (needed for yoy denominator)."
                )
            dropped_for_lack_of_data.append(resolved[0][0])
            if displaced_source is None:
                displaced_source = src.label
            continue

        factor = 1.0
        unit_comparable = True
        if needs_unit:
            # Categorical tables usually declare their unit in the COLUMN name
            # ('Harga (Rp)') rather than a table-wide unit row — prefer the matched
            # column's declared unit when there is one.
            excel_unit = src.table.unit
            if src.table.axis_type == "categorical":
                excel_unit = next((u for u in col_units if u), None) or src.table.unit
            # A '%, yoy' table cannot answer "berapa triliun Rp". Without this the fallback
            # below normalises the CLAIM's scale only, so Tabel 1's growth half answered a
            # level claim about uang kartal with 15,7 / 1e12 = 0,0 triliun Rp and reported it
            # as a Refuted second opinion. Gated on the claim carrying a real numeric scale,
            # so a dimensionless 'persen'/index claim still reaches the fallback (the PMI case
            # that branch documents).
            if _is_growth_series(excel_unit) and _parse_scale_unit(fact.unit) is not None:
                if best_reason is None:
                    best_reason = (
                        f"Sumber [{src.label}] bersatuan '{excel_unit}' (pertumbuhan), "
                        f"tidak bisa menjawab klaim nilai dalam '{fact.unit}'."
                    )
                continue
            # The mirror case: an index sheet holds LEVELS, so a percent claim ('harga rumah
            # tipe menengah tumbuh 0,40% (qtq)') compared with them reads 0,40 against 114,01.
            # The dimensionless fallback below exists for PMI, whose sheet says '(%, Indeks)'
            # and whose report prints the index itself as '52,03%' — so only an index without
            # a % is turned away.
            if _is_percent_unit(fact.unit) and _is_plain_index(excel_unit):
                if best_reason is None:
                    best_reason = (
                        f"Sumber [{src.label}] berisi level indeks ('{excel_unit}'); klaim dalam "
                        f"persen (pertumbuhan) tidak bisa dibandingkan langsung dengan level."
                    )
                continue
            factor = _unit_factor(fact.unit, excel_unit)
            if factor is None and (not excel_unit or _parse_scale_unit(excel_unit) is None):
                # The Excel column carries no numeric SCALE to convert TO: either no unit
                # at all (BI survey workbooks), or a dimensionless one like 'persen' or
                # '(%, Indeks)' (PMI is an index in %). There is nothing to convert, so
                # normalise only the CLAIM's own scale word and compare raw — a claim of
                # 7,5 'juta Rp' becomes 7 500 000, while a 'persen'/index claim (no scale)
                # compares directly. Without this a PMI '51,43%' claim failed conversion
                # against a '(%, Indeks)' sheet even though the value sat right there.
                parsed = _parse_scale_unit(fact.unit) if fact.unit else None
                factor = parsed[0] if parsed else 1.0
                # Only the CLAIM's scale was applied — this source never confirmed a scale of
                # its own, so its number is not on a footing where it can corroborate or
                # contradict another source's. Excluded from conflict comparison (but not
                # from producing a verdict): a BI report carries the same series in
                # 'Triliun Rp' and in percent under identical row labels, and comparing
                # across those two would flag a conflict on nearly every claim.
                unit_comparable = parsed is None
            if factor is None:
                if best_reason is None:
                    best_reason = (
                        f"Unit conversion from '{fact.unit}' to '{excel_unit}' "
                        f"([{src.label}]) is not supported. Cannot compare."
                    )
                continue

        # Do NOT stop at the first source that resolves: the fuzzy tiers resolve a
        # breakdown claim against a coarser aggregate row just as readily as against the
        # real breakdown row, so whichever sheet the user happened to upload first would
        # win. Keep scanning and keep the closest label match; ties keep the earlier
        # source, so single-source behaviour is unchanged.
        # A source only stands in for one that named the claim better and had no data if it is
        # not a NARROWER series. "Kredit" also matches a 'Kredit Properti' row, and answering a
        # credit claim from the property breakdown is answering a different question — the guard
        # test_a_looser_label_match_may_not_supply_the_answer pins down. A row that adds no word
        # the claim did not use is the same series worded differently ('Uang Beredar (M2)' for a
        # claim about 'Uang Beredar Luas (M2)'), and may stand in.
        if dropped_for_lack_of_data and _is_narrower_than_the_claim(fact, resolved):
            continue
        if not _source_is_about_the_sentence(fact, src):
            if best_reason is None:
                best_reason = (
                    f"Sumber [{src.label}] membahas pokok lain dari kalimat klaim "
                    f"('{fact.display_label}' saja tidak menyebut pokoknya)."
                )
            continue
        if _answers_one_group_of_it(fact, resolved):
            if best_reason is None:
                best_reason = (
                    f"Sumber [{src.label}] hanya memuat rinciannya per kelompok "
                    f"('{resolved[0][0]}'), bukan angka '{fact.display_label}' itu sendiri."
                )
            continue
        # A rate the sentence marks '(mtm)' is not answered by a '%, yoy' cell, and vice versa:
        # SPE Tabel 2 (yoy) answered "terkontraksi sebesar 0,1% (mtm)" with 1,1.
        if fact.operation == "value" and _is_percent_unit(fact.unit):
            claim_kind = _claimed_rate_kind(fact)
            cell_kind = _growth_kind(src.table.unit, resolved[0][0])
            if claim_kind and cell_kind not in (None, "mixed", claim_kind):
                if best_reason is None:
                    best_reason = (
                        f"Excel [{src.label}] ({resolved[0][0]}) berisi pertumbuhan {cell_kind} "
                        f"('{src.table.unit}'), sedangkan klaim menyebut {claim_kind}."
                    )
                continue
        gross = _gross_row_for_a_net_number(fact, resolved)
        if gross:
            if best_reason is None:
                best_reason = (
                    f"Klaim menyebut posisi neto, sedangkan baris '{gross}' di [{src.label}] "
                    f"hanya memuat satu sisi (aset atau kewajiban) secara bruto."
                )
            continue
        absent = _breakdown_the_table_lacks(fact, resolved, src)
        if absent:
            if best_reason is None:
                best_reason = (
                    f"Sumber [{src.label}] tidak memuat '{', '.join(absent)}' — "
                    f"rincian '{fact.display_label}' tidak ada di tabel ini."
                )
            continue
        coverage = _coverage_score(fact.periods, resolved, src)
        if coverage <= 0.0:
            # Nothing this source names has anything to do with what the claim asked about —
            # see TableData.query_coverage. Answering anyway is the confident-wrong-number case.
            if best_reason is None:
                best_reason = (
                    f"Sumber [{src.label}] tidak membahas '{fact.display_label}'."
                )
            continue
        candidates.append(_Candidate(
            score=_resolution_score(fact.periods, resolved),
            coverage=coverage,
            direct_coverage=_coverage_score(fact.periods, resolved, src, broken_out=False),
            qualification=_qualification_rank(fact.periods, resolved),
            src=src, resolved=resolved, factor=factor, unit_comparable=unit_comparable,
        ))

    if not candidates:
        return _inconclusive_result(fact, best_missing, best_reason)

    # Coverage first (a tie goes to the source whose title and matched rows carry the claim's
    # words themselves), then label quality. Stable sort, so ties keep source order as before.
    candidates.sort(key=_rank_key)
    candidates = _prefer_growth_source_for_a_growth_trend(fact, candidates)
    # Another uploaded PDF is a fallback, never a rival: the report's own tables and the Excel
    # the user chose decide whenever they can answer at all. So is a chart — its labels are read
    # off a drawing, and it only prints a few of the months a table holds. Stable, so each group
    # keeps the ranking above.
    primary = [c for c in candidates if c.src.origin not in _FALLBACK_ORIGINS]
    fallback = [c for c in candidates if c.src.origin in _FALLBACK_ORIGINS]
    candidates = primary + fallback
    # Every candidate is computed once — plain arithmetic over values already looked up. The
    # results are reused for the source comparison, so this costs no more than before.
    evaluated = [(c, _compute_operation(fact, c.resolved, c.factor, c.src)) for c in candidates]

    # The best label match does not always CARRY the answer. Resolving a claim only checks the
    # periods it names, while a yoy_growth also needs the year-ago column, so a snippet table
    # with four columns wins the label match and then cannot compute — and the Lampiran that
    # could was never consulted. Measured on the M2 report: 44 of 89 claims came back "no data"
    # while another table in the same PDF held the figure.
    #
    # So a source that reaches a verdict outranks one that does not, but ONLY among equally good
    # label matches. A looser match is a weaker claim to be the same series (a "Kredit" claim
    # fuzzy-matches a "Kredit Properti" breakdown row just as readily), and answering from one of
    # those would trade an honest "not enough data" for a confident wrong number.
    head_index = 0
    for index, (cand, result) in enumerate(evaluated):
        if _match_quality(cand) < _match_quality(evaluated[0][0]) - 1e-9:
            break
        if result.verdict != "Inconclusive":
            head_index = index
            break
    # A FULL tie — same coverage, title wording, qualification and label quality — leaves the
    # ranking with no basis at all, and upload order decided. SKDU names its sector rows the
    # same in every sheet, so "Pengadaan Listrik tercatat stabil (80,54%)" (capacity, Tabel 2)
    # was answered by Tabel 1's SBT row (0,57). Among fully tied sources the claim's own number
    # is the evidence of which series the sentence means; any other tied value still shows up
    # in the source comparison. A claim without a number (a trend) carries no such evidence.
    head_cand, head_result = evaluated[head_index]
    if head_result.verdict == "Refuted" and fact.claimed_value is not None:
        for index, (cand, result) in enumerate(evaluated):
            if (result.verdict == "Entailed" and _rank_key(cand) == _rank_key(head_cand)
                    and (cand.src.origin in _FALLBACK_ORIGINS)
                    == (head_cand.src.origin in _FALLBACK_ORIGINS)):
                head_index = index
                break
    # The loop above stops at the first looser match, and with the fallback group sorted last
    # that can be before any other PDF was looked at. When the own sources reached no verdict,
    # another PDF may still answer — held to the same bar: at least as close a label match as
    # the best own source, so it never trades "not enough data" for a different series.
    if evaluated[head_index][1].verdict == "Inconclusive" and primary and fallback:
        bar = _match_quality(evaluated[0][0]) - 1e-9
        for index in range(len(primary), len(evaluated)):
            cand, result = evaluated[index]
            if result.verdict != "Inconclusive" and _match_quality(cand) >= bar:
                head_index = index
                break

    return _attach_source_comparison(evaluated, head_index, displaced_source)


def _rank_key(c: _Candidate) -> Tuple[float, float, int, float]:
    """How _evaluate_fact orders candidate sources: coverage, title wording, qualification, label."""
    return (-c.coverage, -c.direct_coverage, c.qualification, -round(c.score, 9))


def _units_comparable(a: Optional[str], b: Optional[str]) -> bool:
    """True when two sources' declared units describe the same kind of quantity.

    The guard that keeps conflict detection honest. A BI report states the same series, under
    the SAME row labels, in a levels table ('Triliun Rp') and in a growth table ('%, yoy'), and
    a yoy_growth computed off each of those is 14,2 vs 142,9 — an artefact of applying growth to
    an already-growth series, not a disagreement between the tables.

    An unknown (blank) unit is treated as comparable only with another blank one: with nothing
    to check, staying quiet beats inventing a contradiction.
    """
    left, right = (a or "").strip().lower(), (b or "").strip().lower()
    if left == right:
        return True
    if not left or not right:
        return False
    return _unit_factor(left, right) is not None


# Beyond this relative gap, two same-named rows are different series rather than two readings
# of one. Measured over every row name shared between this report's M0 and M2 tables: the pairs
# that ARE the same series compiled differently sit at 0,8% (uang kartal, 1.186,3 vs 1.195,6)
# and 1,5% (aktiva luar negeri bersih), while the pairs that merely share a name start at 63,7%
# and run past 100% (opposite signs). Anywhere in that gap works; 25% keeps a wide margin both
# ways. Re-measure if a BI report of another kind starts carrying an M0 table.
#
# Those figures are LEVELS, and the separation they rest on does not exist between growth rates:
# 4,8% and 4,6% are two different aggregates one percent apart. _may_contradict therefore never
# consults this band for a growth series.
_DIFFERENT_SERIES_GAP = 0.25


def _same_series_plausible(a: float, b: float) -> bool:
    """Whether two sources' raw values could be readings of the SAME series.

    Opposite signs, or a gap wider than _DIFFERENT_SERIES_GAP, means they cannot be: a series
    that is +838,0 in one table and -246,7 in another is two different quantities sharing a row
    name, not a discrepancy worth reporting to the reader.
    """
    if a == 0 and b == 0:
        return True
    if a * b < 0:
        return False
    scale = max(abs(a), abs(b))
    return scale == 0 or abs(a - b) / scale <= _DIFFERENT_SERIES_GAP


def _may_contradict(head: "_Candidate", other: "_Candidate") -> bool:
    """Whether `other` is entitled to contradict `head`, given what each table is ABOUT.

    Two tables from different statistical universes (see TableData.table_subject) routinely
    print rows with identical names for different quantities — BI's own balance sheet versus
    the whole monetary system's. Reporting those as "tabel internal tidak konsisten" was noise
    on every claim about a determinant of M2.

    They are still allowed to disagree when their numbers are close enough to be the same
    series measured on a different basis: Lampiran 1 says uang kartal is 1.186,3 and Lampiran 6
    says 1.195,6, and that 9,3 T gap is a real thing for a reader to know about. That leniency
    is for LEVELS only — see the growth-series guard below. Only the implausible pairings are
    silenced, and only across universes — two credit tables that disagree are reported however
    far apart they are.
    """
    head_subject = head.src.table.table_subject()
    other_subject = other.src.table.table_subject()
    if head_subject is None or other_subject is None or head_subject == other_subject:
        return True
    # _DIFFERENT_SERIES_GAP was measured on levels, where two readings of one series sit within
    # 1,5% and two rows that merely share a name start at 63,7%. Growth rates carry no such
    # separation: DPK's simpanan berjangka grew 4,8% and M2's narrower one 4,6%, four percent
    # apart and nothing alike. On a growth table the value gap proves nothing, so a
    # cross-universe pair is simply not comparable and the leniency below must not be reached.
    if _is_growth_series(head.src.table.unit) or _is_growth_series(other.src.table.unit):
        logger.info(
            "Not reporting a conflict between [%s] (%s) and [%s] (%s): growth rates from "
            "different statistical universes.",
            head.src.label, head_subject, other.src.label, other_subject,
        )
        return False
    raw_head = head.resolved[0][1] if head.resolved else None
    raw_other = other.resolved[0][1] if other.resolved else None
    if raw_head is None or raw_other is None:
        return True
    if _same_series_plausible(raw_head, raw_other):
        return True
    logger.info(
        "Not reporting a conflict between [%s] (%s) and [%s] (%s): %s vs %s cannot be the "
        "same series.",
        head.src.label, head_subject, other.src.label, other_subject, raw_head, raw_other,
    )
    return False


# A metric named as the GROWTH of something rather than the thing itself.
_GROWTH_METRIC_RE = re.compile(r"\bpertumbuhan\b", re.IGNORECASE)
# A yoy figure quoted in the same sentence, which is what makes 'pertumbuhan' there a
# comparison of RATES rather than a passing mention.
_YOY_IN_QUOTE_RE = re.compile(r"\byoy\b", re.IGNORECASE)


def _is_about_a_growth_rate(fact: ExtractedFact) -> bool:
    """Whether a trend claim is about how fast something grew rather than how big it is.

    Two signals, either of which settles it. The metric can say so outright ("pertumbuhan
    giro"), or the sentence can compare against a previous period's GROWTH while quoting a yoy
    figure — "tabungan dan simpanan berjangka meningkat dibandingkan pertumbuhan pada bulan
    sebelumnya masing-masing sebesar 8,9% (yoy) dan 4,6% (yoy)". Both readings of that sentence
    are true statements about different quantities, and only one of them is the claim.

    Requiring the word 'pertumbuhan' and not merely 'tumbuh' keeps this off the report's
    commonest shape, "Posisi M2 ... tercatat Rp10.253,7 triliun, atau tumbuh 9,2% (yoy)", which
    states a level and its growth side by side rather than comparing two growth rates.
    """
    if any(_GROWTH_METRIC_RE.search(p.metric_label or "") for p in fact.periods):
        return True
    quote = fact.context_quote or ""
    return bool(_GROWTH_METRIC_RE.search(quote) and _YOY_IN_QUOTE_RE.search(quote))


def _prefer_growth_source_for_a_growth_trend(
    fact: ExtractedFact, candidates: List["_Candidate"]
) -> List["_Candidate"]:
    """Put '%, yoy' sources first when a trend claim is about a growth RATE, not a level.

    "pertumbuhan giro meningkat sebesar 10,5% (yoy) dari 10,2% (yoy)" says the growth rate rose.
    Checked against the levels table it asks a different question — giro fell from Rp3.087,8 to
    Rp3.055,6 triliun over those two months, both facts true at once — and the claim came back
    Tidak Sesuai. The report prints both quantities under one caption and they are now separate
    sources (see pdf_table_extraction._split_unit_blocks), so the right one can simply be chosen.

    Only reorders, never discards: if no growth-series source resolved the claim, the levels one
    still answers it exactly as before.
    """
    if fact.operation not in _TREND_OPS:
        return candidates
    if not _is_about_a_growth_rate(fact):
        return candidates
    growth = [c for c in candidates if _is_growth_series(c.src.table.unit)]
    if not growth:
        return candidates
    return growth + [c for c in candidates if not _is_growth_series(c.src.table.unit)]


def _match_quality(cand: "_Candidate") -> float:
    """One number for 'how well does this source answer the claim', for the equal-quality tests.

    Coverage dominates label quality: a source that drops one of the claim's scope words is
    answering a different question, so it must not be promoted for reaching a verdict, nor
    reported as a second reading that contradicts the winner.
    """
    return cand.coverage - cand.qualification / 100.0 + cand.score / 1000.0


def _source_value(cand: "_Candidate", result: FactVerificationResult) -> SourceValue:
    return SourceValue(
        source=cand.src.label,
        origin=cand.src.origin,
        matched_label=result.periods[0].metric_label if result.periods else None,
        computed_value=result.computed_value,
        computed_unit=result.computed_unit,
        verdict=result.verdict,
    )


def _attach_source_comparison(
    evaluated: List[Tuple["_Candidate", FactVerificationResult]],
    head_index: int,
    displaced: Optional[str] = None,
) -> FactVerificationResult:
    """The verdict of the chosen source, plus what every other resolving source said.

    A conflict says the SOURCES disagree, not that the claim is wrong, so the headline verdict is
    left alone. `head_index` is the source that produced it — usually the best label match, but
    see _evaluate_fact for the one case where a lower-ranked source is preferred.

    Only sources that matched the metric AS PRECISELY as the winner are reported at all. A looser
    match is a different series, not a second reading of the same one: a claim about "Kredit"
    fuzzy-matches a "Kredit Properti" breakdown row, and "IPDG > Pengeluaran Rp2,1 - 3 juta"
    resolves against the respondent-profile sheet's bare "Rp2,1 - 3 juta" row — printing that
    sheet's 17,9 (a share of respondents) beside the index's 99,0 invites the reader to doubt a
    number that was never in question. This is the same score test the conflict loop needs, so
    hiding these costs no signal: a source too loose to contradict the winner had nothing to add.
    """
    head_cand_for_filter = evaluated[head_index][0]
    head_score = _match_quality(head_cand_for_filter)
    per_source = [evaluated[head_index]] + [
        pair for index, pair in enumerate(evaluated)
        if index != head_index and _match_quality(pair[0]) >= head_score - 1e-9
        # A source measuring a different quantity is not a second reading of this claim, so it
        # is not shown either — printing Lampiran 6's 56,1% (Bank Indonesia's net claims on
        # government) beside Lampiran 1's 38,6% (the monetary system's) invites the reader to
        # doubt a number that was never in question. Same reasoning as the score filter above.
        and _may_contradict(head_cand_for_filter, pair[0])
        # A chart is compared with the tables on the Cek Grafik tab, label by label, where a
        # misread position is recognised as one. Beside a claim it only ever repeated that
        # comparison without the recognition: SK-Agustus-2026's IKLK claims, right against
        # Tabel 1, were flagged "Grafik berbeda dengan tabel" because the model had read Grafik
        # 4's 104,1 and 101,1 one month apart.
        and pair[0].src.origin != "chart"
    ]
    result = per_source[0][1]
    values = [_source_value(cand, res) for cand, res in per_source]

    # When the answer did not come from the closest label match, say so — the reader is entitled
    # to know the headline number was taken from a table that matched the metric less exactly,
    # whether that source was outranked here or dropped earlier for holding no usable data.
    passed_over = evaluated[0][0].src.label if head_index != 0 else displaced
    note = (
        f" | Sumber dengan kecocokan label terbaik "
        f"([{passed_over}]) tidak memuat data yang dibutuhkan; "
        f"nilai diambil dari [{per_source[0][0].src.label}]."
    ) if passed_over else ""

    if len(per_source) == 1:
        return result.model_copy(update={"reasoning": result.reasoning + note}) if note else result

    head_cand, head = per_source[0][0], values[0]
    conflict: Optional[str] = None
    conflicting: Optional[SourceValue] = None
    for (cand, _), other in zip(per_source[1:], values[1:]):
        # A source that could not reach a verdict has no opinion to contradict — it is missing
        # data, not a disagreement. Without this, every claim whose best-matching table happens
        # to lack the period reads as a conflict with whichever table does have it.
        if "Inconclusive" in (head.verdict, other.verdict):
            continue
        # Every source that got this far already ties the winner's label score — see the
        # filter above, which is what keeps a looser match from contradicting a better one.
        # Both sources must measure the same kind of quantity, and both must have had a real
        # unit basis for the conversion (see unit_comparable in _evaluate_fact).
        if not (head_cand.unit_comparable and cand.unit_comparable):
            continue
        if not _units_comparable(head_cand.src.table.unit, cand.src.table.unit):
            continue
        # Same row name, different statistical universe — see _may_contradict.
        if not _may_contradict(head_cand, cand):
            continue
        if head.computed_value is not None and other.computed_value is not None:
            differs = abs(head.computed_value - other.computed_value) > MATCH_TOLERANCE
        else:
            # Trend operations compute no value; their sources disagree when their verdicts do.
            differs = head.verdict != other.verdict
        if differs:
            conflicting = other
            if "pdf_other" in (head.origin, other.origin):
                conflict = "cross_pdf"
            elif head.origin == other.origin == "pdf":
                conflict = "internal"
            else:
                conflict = "cross"
            break

    reasoning = result.reasoning + note

    if conflict is None:
        return result.model_copy(update={"source_values": values, "reasoning": reasoning})

    def _fmt(sv: SourceValue) -> str:
        value = "tidak terhitung" if sv.computed_value is None else f"{sv.computed_value}"
        return f"[{sv.source}]: {value} ({sv.verdict})"

    explanation = {
        "internal": "dua tabel di dalam PDF saling bertentangan",
        "cross": "tabel di PDF dan sumber Excel tidak sinkron",
        "cross_pdf": "tabel di PDF lain memuat angka berbeda (bisa jadi angka revisi)",
    }[conflict]
    note = f" | KONFLIK SUMBER: {_fmt(head)} vs {_fmt(conflicting)} — {explanation}"
    return result.model_copy(update={
        "source_values": values,
        "source_conflict": conflict,
        "reasoning": reasoning + note,
    })


# ---------------------------------------------------------------------------
# Table-family suggestions for Inconclusive claims
# ---------------------------------------------------------------------------

# Known BI statistical-table families and the metric keywords that point at them. A BI M2
# report cites series from ~4 different SEKI tables; users typically upload only I.1 and then
# see the rest come back Inconclusive with no hint of WHICH table would cover them. Keyword
# matching is on the extracted metric label (lowercased substring), deterministic on purpose.
_TABLE_FAMILY_HINTS: List[Tuple[str, Tuple[str, ...]]] = [
    (
        "Uang Primer / M0 (SEKI Tabel 1.2)",
        ("m0", "uang primer", "uang kartal yang diedarkan", "uyd", "giro bank umum"),
    ),
    (
        "Posisi Simpanan Masyarakat / DPK (mis. TABEL1_19 — SEKI Tabel 1.19)",
        ("dpk", "dana pihak ketiga", "deposito", "tabungan masyarakat"),
    ),
    (
        "Posisi Kredit Bank Umum & BPR (SEKI Tabel 1.5 dst — KMK/KI/KK per jenis penggunaan)",
        (
            "kredit", "kmk", "kredit modal kerja", "kredit investasi", "kredit konsumsi",
            "kepemilikan rumah", "kendaraan bermotor", "multiguna", "debitur",
        ),
    ),
]


def _build_table_suggestions(results: List[FactVerificationResult]) -> List[TableSuggestion]:
    """Map Inconclusive claims to the BI table family that likely carries their data.

    Only claims that resolved against NO source at all (matched_excel_source is None) are
    considered — an Inconclusive caused by e.g. a unit mismatch already found its table.
    """
    metrics_by_family: Dict[str, List[str]] = {}
    for r in results:
        if r.verdict != "Inconclusive" or r.matched_excel_source is not None:
            continue
        label_lower = r.metric_label.lower()
        for family, keywords in _TABLE_FAMILY_HINTS:
            if any(kw in label_lower for kw in keywords):
                bucket = metrics_by_family.setdefault(family, [])
                if r.metric_label not in bucket:
                    bucket.append(r.metric_label)
                break  # first matching family wins; families are ordered specific-first

    return [
        TableSuggestion(table=family, metrics=metrics)
        for family, metrics in metrics_by_family.items()
    ]


# ---------------------------------------------------------------------------
# Parser cascade: deterministic BI layout first, generic heuristic parser as fallback
# ---------------------------------------------------------------------------

def _bi_parse_collapsed(table: BITableData) -> bool:
    """True when the BI reader emitted the SAME row label more than once.

    parse_bi_table reads the label from one fixed column and disambiguates repeats via the
    label cell's indent. When a sheet encodes its row hierarchy in a DIFFERENT column
    instead (the per-city consumer-survey sheet puts '1. Jakarta' left of the metric name),
    the indent trick finds no parent, every city repeats the same six metric labels, and
    first-occurrence-wins silently keeps only the first city's numbers under a label that
    reads national. Duplicate labels are that failure's fingerprint, so treat the parse as
    unusable and let the generic parser — which composes labels from the whole label block —
    have it. Nothing is lost if that also fails: the cascade returns this table as its last
    resort anyway.
    """
    return len(set(table.row_labels)) < len(table.row_labels)


def _parse_table_with_fallback(
    excel_bytes: bytes,
    sheet_name: str,
    llm: Optional[BaseChatModel] = None,
    publication: str = "",
) -> Tuple[BITableData, str]:
    """Return (table, parser_name) — publication parser, then BI → generic → LLM mapping.

    When the checker picked a publication whose own parser covers this sheet
    (publication_parsers), that parser goes first and is reported under the publication's key
    (e.g. "sulni"). If it rejects the sheet — or breaks — the cascade below runs exactly as it
    would without a publication, so a layout change in a new edition degrades to today's
    behaviour instead of failing the check.

    The BI parser stays the primary generic path so known SEKI files keep their exact current
    behaviour. Its result is accepted only when it actually extracted data; a structurally
    successful but EMPTY parse (or a ValueError) falls through to the generic parser. When
    that also fails and an llm is available, the LLM structure-mapping parser (tier 3) gets
    a shot — it only maps structure; values are still extracted by code. As the last resort,
    any BI result we did get is returned even when empty (claims then come back Inconclusive
    instead of the whole request failing); only when every tier raised is the combined error
    surfaced.
    """
    if publication:
        try:
            table = parse_for_publication(publication, excel_bytes, sheet_name)
        except Exception as exc:  # a parser fault must never fail the check
            logger.warning(
                "Publication parser '%s' failed on sheet '%s' (%s) — using the generic cascade.",
                publication, sheet_name, exc,
            )
            table = None
        if table is not None:
            return table, publication

    bi_table = None
    bi_error: Optional[Exception] = None
    try:
        bi_table = parse_bi_table(excel_bytes, sheet_name)
        if bi_table.row_labels and bi_table._data and not _bi_parse_collapsed(bi_table):
            return bi_table, "bi"
    except ValueError as exc:
        bi_error = exc

    try:
        return parse_generic_table(excel_bytes, sheet_name), "generic"
    except ValueError as generic_error:
        llm_error: Optional[Exception] = None
        if llm is not None:
            try:
                return parse_table_with_llm(excel_bytes, sheet_name, llm), "llm"
            except ValueError as exc:
                llm_error = exc
                logger.warning("LLM structure-mapping parser failed: %s", exc)
        if bi_table is not None:
            return bi_table, "bi"
        raise ValueError(
            f"Tabel tidak dapat diparsing. Parser BI: {bi_error}. "
            f"Parser generik: {generic_error}."
            + (f" Parser LLM: {llm_error}." if llm_error is not None else "")
        ) from generic_error


def _parse_grid_with_fallback(
    grid: List[List], llm: Optional[BaseChatModel] = None
) -> Tuple[BITableData, str]:
    """Return (table, parser_name) for an in-memory grid — generic heuristic → LLM mapping.

    Used for grids that did not come from a spreadsheet (tables transcribed out of a PDF).
    Tier 1 (parse_bi_table) is deliberately absent: it disambiguates repeated row labels by
    reading the label cell's INDENT from the workbook's styling, which a transcribed grid does
    not have — running it would produce a collapsed table (see _bi_parse_collapsed) rather than
    an honest failure. Raises ValueError when both available tiers fail; the caller degrades the
    source to pointer-only, which is always possible here since the grid exists by construction.
    """
    try:
        return parse_generic_grid(grid), "generic"
    except ValueError as generic_error:
        if llm is not None:
            try:
                return parse_grid_with_llm(grid, llm), "llm"
            except ValueError as llm_error:
                logger.warning("LLM structure-mapping parser failed on PDF grid: %s", llm_error)
                raise ValueError(
                    f"Parser generik: {generic_error}. Parser LLM: {llm_error}."
                ) from generic_error
        raise


# ---------------------------------------------------------------------------
# Tier-4 cell-pointer pass: claims no source could resolve get one more chance —
# the LLM points at grid coordinates, code reads the values (see cell_pointer.py).
# ---------------------------------------------------------------------------

async def _pointer_pass(
    facts: List[ExtractedFact],
    results: List[FactVerificationResult],
    sources: List[_ExcelSource],
    llm: BaseChatModel,
    fallback_llm: Optional[BaseChatModel] = None,
) -> Tuple[List[FactVerificationResult], int]:
    """Re-resolve fully-unresolved claims via LLM cell pointers.

    Candidates are results that stayed Inconclusive without matching any source (the
    same predicate _build_table_suggestions uses). ONE batched pointer call covers every
    grid-bearing source; a fact is accepted from the first source (upload order) where
    EVERY needed cell — including the synthesized prior-year point for yoy_growth —
    yields a numeric value via read_grid_cell. The values are injected into a fresh
    minimal TableData under the exact keys the fact asks for, and the ordinary
    _evaluate_fact machinery computes the verdict; the LLM never supplies a number.
    A wrong pointer is therefore visible (cell refs are appended to reasoning and
    resolved_via='pointer' is set) but can never invent data.
    """
    # A net position per component ("kewajiban neto sebesar 229,6") has no cell of its own in
    # SEKI V.39; a pointer can only land on a gross side (see _gross_row_for_a_net_number).
    candidate_idx = [
        i for i, r in enumerate(results)
        if r.verdict == "Inconclusive" and r.matched_excel_source is None
        and not _is_a_net_figure(facts[i])
    ]
    grid_sources = [s for s in sources if s.grid]
    if not candidate_idx or not grid_sources:
        return results, 0
    queries = build_point_queries(facts, candidate_idx)
    if not queries:
        return results, 0

    # Only ask a sheet about metrics it could plausibly hold. pointer_is_plausible rejects
    # a coordinate whose row shares no term with the metric, so a query failing
    # metric_could_match here is one whose answer would be thrown away — and a sheet with
    # no surviving query is a whole LLM call (a full grid snapshot) not worth making.
    per_source_queries: List[List[Tuple[int, PointQuery]]] = [
        [
            (qi, q) for qi, q in enumerate(queries)
            if metric_could_match(src.grid, q.data_key[0], src.table.title)
        ]
        for src in grid_sources
    ]
    for src, kept in zip(grid_sources, per_source_queries):
        if len(kept) < len(queries):
            logger.info(
                "Cell pointer: %s/%s asked about %d of %d queries (rest cannot match this sheet)",
                src.filename, src.sheet, len(kept), len(queries),
            )

    # One call for all of them. The snapshots are the only part that differs per source; the
    # system prompt and the query list are identical, and sending those once per source was
    # most of what this tier cost (measured 2026-09-14: 22 calls, 45k chars of repeated query
    # text and 45k of repeated prompt against 29k of actual snapshot).
    resolutions = await resolve_pointers_multi(
        [
            (f"{src.filename} / {src.sheet}", src.grid, [qi for qi, _q in kept])
            for src, kept in zip(grid_sources, per_source_queries)
        ],
        queries,
        llm,
        fallback_llm,
    )

    new_results = list(results)
    n_resolved = 0
    for fi in candidate_idx:
        fact_queries = [(qi, q) for qi, q in enumerate(queries) if q.fact_index == fi]
        if not fact_queries:
            continue  # mixed-axis or otherwise unqueryable fact
        for src, (pointers, sheet_unit) in zip(grid_sources, resolutions):
            cells = []
            for qi, q in fact_queries:
                coord = pointers.get(qi)
                value = read_grid_cell(src.grid, *coord) if coord else None
                # Guard: the pointed row must actually relate to the queried metric, so a
                # pointer that grabbed an unrelated cell (e.g. a generic TOTAL for a metric
                # absent from the sheet) is rejected instead of yielding a wrong verdict.
                if value is None or not pointer_is_plausible(
                    src.grid, coord[0], q.data_key[0], src.table.title
                ):
                    cells = None
                    break
                # The same guard for the other axis: a temporal query names a period, and the
                # column it lands in has to be headed by that period. Without this the pointer
                # answered "April 2025" with a "% yoy April 2026" cell — see
                # cell_pointer.pointer_column_matches for what that cost.
                if len(q.data_key) == 3 and not pointer_column_matches(
                    src.grid, coord[0], coord[1], q.data_key[1], q.data_key[2]
                ):
                    cells = None
                    break
                cells.append((q, coord[0], coord[1], value))
            if cells is None:
                continue

            axis = "categorical" if len(fact_queries[0][1].data_key) == 2 else "temporal"
            # A parsed table's own unit stays authoritative; only a pointer-only source
            # (no parse at all) trusts the LLM-reported unit ANNOTATION text, so that
            # _unit_factor can reconcile e.g. a 'triliun Rp' claim with a 'Miliar Rp'
            # sheet instead of comparing raw.
            unit = src.table.unit if not src.pointer_only else (sheet_unit or "")
            mini = BITableData(title=src.table.title, unit=unit, row_labels=[], axis_type=axis)
            for q, _r, _c, value in cells:
                mini._data.setdefault(q.data_key, value)
                if q.data_key[0] not in mini.row_labels:
                    mini.row_labels.append(q.data_key[0])
                if axis == "categorical" and q.data_key[1] not in mini.col_labels:
                    mini.col_labels.append(q.data_key[1])

            patched = _ExcelSource(table=mini, filename=src.filename, sheet=src.sheet)
            res = _evaluate_fact_safely(facts[fi], [patched])
            if res.verdict == "Inconclusive":
                continue
            refs = "; ".join(f"{q.desc} → R{r}K{c}" for q, r, c, _v in cells)
            new_results[fi] = res.model_copy(update={
                "resolved_via": "pointer",
                "reasoning": f"{res.reasoning} | Sel ditunjuk AI: {refs}",
            })
            n_resolved += 1
            break
    return new_results, n_resolved


# ---------------------------------------------------------------------------
# Orchestrator
# ---------------------------------------------------------------------------

def _evaluate_fact_safely(fact: ExtractedFact, sources: List[_ExcelSource]) -> FactVerificationResult:
    """_evaluate_fact, with any failure contained to the one claim that caused it.

    Claims arrive in whatever shape the extractor gave them, and a shape the comparison code
    did not anticipate used to raise straight out of verify_paired: on SK-Juni-2026 a threshold
    claim carrying two periods ended the whole request with "list index out of range", and the
    user saw no verdict for any of its seventy claims. One claim the code cannot judge is a
    claim without a verdict — Inconclusive, with the reason logged — not a failed report.
    """
    try:
        return _evaluate_fact(fact, sources)
    except Exception as exc:
        logger.exception("Could not evaluate fact %r (%s)", fact.display_label, fact.operation)
        return _make_result(
            fact, [], None, fact.claimed_value, fact.unit, None, fact.unit, None, "Inconclusive",
            reasoning=f"Klaim ini gagal dinilai oleh program ({type(exc).__name__}: {exc}).",
        )


def _deduplicate_facts(facts: List[ExtractedFact]) -> List[ExtractedFact]:
    """Remove duplicate (operation, periods) entries — keep first occurrence."""
    seen = set()
    unique = []
    for f in facts:
        key = (f.operation, tuple((p.metric_label, p.year, p.month, p.col_label) for p in f.periods))
        if key not in seen:
            seen.add(key)
            unique.append(f)
    return unique


def _merge_sources_sharing_rows(
    sources: List[Tuple[str, List[str]]]
) -> List[Tuple[str, List[str]]]:
    """Collapse sources whose row lists are identical into one entry naming both tables.

    Splitting a snippet table into its level half and its '%, yoy' half doubles the source
    count, and the two halves carry exactly the same row names — so the extraction prompt
    listed every row twice, in every chunk. On the April report that is 22 sources for 12
    distinct row lists, and 11.262 characters of prompt where 8.179 say the same thing.

    The titles are kept, both of them: `_build_row_labels_block` shows them so the model can
    tell what 'Total' totals in a given table, and losing that would trade prompt size for the
    ambiguity that costs verdicts. Order is preserved so the first table to advertise a row list
    still leads.
    """
    order: List[Tuple[str, ...]] = []
    titles: Dict[Tuple[str, ...], List[str]] = {}
    for desc, labels in sources:
        key = tuple(labels)
        if key not in titles:
            titles[key] = []
            order.append(key)
        titles[key].append(desc)
    return [("; ".join(titles[key]), list(key)) for key in order]


def _pdf_table_source(
    table_from_pdf: PdfTable, filename: str, origin: str, llm: Optional[BaseChatModel],
) -> Tuple[_ExcelSource, str]:
    """One transcribed PDF table as a reference source, plus the name of the parser that read it."""
    # "verified" means the values came out of the PDF's text layer rather than off the
    # rendered image (see pdf_table_extraction._verify_against_text_layer). Surfaced in the
    # parser name because it is the difference between a code-read number and a model-read
    # one, which a reviewer weighing a verdict needs to know.
    suffix = "" if table_from_pdf.verified else "-unverified"
    try:
        table, parser_used = _parse_grid_with_fallback(table_from_pdf.grid, llm=llm)
        parser_used = f"pdf-{parser_used}{suffix}"
        pointer_only = False
    except ValueError as parse_error:
        # The grid exists by construction, so a parse failure always degrades to
        # pointer-only rather than dropping the table.
        logger.warning(
            "No parser understood the PDF table '%s' (%s) — keeping as pointer-only source.",
            table_from_pdf.label, parse_error,
        )
        table = BITableData(title=table_from_pdf.caption, unit=table_from_pdf.unit, row_labels=[])
        parser_used = f"pdf-pointer-only{suffix}"
        pointer_only = True
    # The transcription carries the printed unit annotation; trust it over a parser that
    # inferred nothing (an empty unit blocks every level-claim unit conversion).
    if not table.unit and table_from_pdf.unit:
        table.unit = table_from_pdf.unit
    source = _ExcelSource(
        table=table,
        filename=filename,
        sheet=table_from_pdf.label,
        grid=table_from_pdf.grid,
        pointer_only=pointer_only,
        origin=origin,
    )
    return source, parser_used


# ---------------------------------------------------------------------------
# Charts — each printed data label is checked like a claim, against the tables
# ---------------------------------------------------------------------------

# How a month is written back to an Indonesian reader.
_MONTH_ID = {"May": "Mei", "Aug": "Agu", "Oct": "Okt", "Dec": "Des"}


def _shift_period(year: int, token: str, step: int) -> Optional[Tuple[int, str]]:
    """The calendar period `step` months (or quarters) away, or None for an unknown token."""
    if token in _QUARTER_ORDINAL:
        index = year * 4 + _QUARTER_ORDINAL[token] - 1 + step
        return index // 4, f"Q{index % 4 + 1}"
    if token in _MONTH_ABBREVS:
        index = year * 12 + _MONTH_ABBREVS.index(token) + step
        return index // 12, _MONTH_ABBREVS[index % 12]
    return None


# Words a chart title or axis adds that a table row does not repeat: Grafik 19 is "Rasio
# Konsumsi per Kelompok Pengeluaran", its table row just 'Rp 1 - 2 juta > Konsumsi'.
_CHART_FILLER_WORDS = frozenset({"indeks", "rasio", "perkembangan", "komposisi"})
# The numbering a workbook puts in front of a section ('5. Medan', 'B1.'), which no chart prints.
_ENUMERATION_RE = re.compile(r"^\s*(?:[A-Za-z]\d{0,2}|\d{1,2})[.)]\s+")
# Words a table row adds to say WHICH breakdown a group belongs to: 'Pengeluaran Rp1 - 2 juta',
# 'Usia 20-30 th' for a chart's 'Rp1 - 2 juta' and '20 - 30 tahun'.
_DIMENSION_WORDS = frozenset({"pengeluaran", "usia", "kelompok", "pendidikan", "tingkat"})
# A qualifier a table puts above rows that a chart never names: 'Total > Konsumsi'.
_UNNAMED_PARTS = frozenset({"total", "jumlah"})


def _label_tokens(text: str) -> Tuple[frozenset, Tuple[str, ...]]:
    """(words, figures in order) of a label. 'tahun' is read as the tables' 'th', and '>' is a
    figure: '> 60 tahun' and '51 - 60 tahun' share every word and must still differ."""
    text = _ENUMERATION_RE.sub("", text)
    text = re.sub(r"\btahun\b", "th", text.lower())
    tokens = re.findall(r"[a-z]+|\d+|>", text)
    words = frozenset(t for t in tokens if t.isalpha() and t not in _CHART_FILLER_WORDS)
    figures = tuple(t for t in tokens if not t.isalpha())
    return words, figures


def _part_fit(query: str, row_part: str) -> Optional[int]:
    """How many words the row part has beyond the query part, or None when it is not the same
    thing: every query word must be there, the figures must be the same, in order, and the row
    may add only what names the SAME thing — its abbreviation or a parenthetical in brackets,
    or the dimension word ('Pengeluaran', 'Usia'). A chart naming only the abbreviation ('IKK')
    is identified by it, whatever else the row spells out.

    Without the last rule the row that fit with fewest words to spare won even when it was a
    different index: SK-Juni-2026's appendix lost its 'IKLK > Usia 41-50 th' row, and Grafik
    8's IKLK labels were checked against 'Indeks Ekspektasi Ketersediaan Lapangan Kerja
    (IEKLK) > …' — two false mismatches of 24 and 28 points."""
    query_words, query_figures = _label_tokens(query)
    row_words, row_figures = _label_tokens(row_part)
    if not query_words and not query_figures:
        return None
    if query_figures != row_figures or not query_words <= row_words:
        return None
    extra = row_words - query_words
    bracketed = frozenset().union(*(
        _label_tokens(inner)[0] for inner in re.findall(r"\(([^()]*)\)", row_part)
    ))
    names_by_abbreviation = bool(query_words) and query_words <= bracketed
    if not names_by_abbreviation and extra - bracketed - _DIMENSION_WORDS:
        return None
    return len(extra)


def _chart_row_fit(parts: List[str], row_label: str) -> Optional[int]:
    """How loosely a row label fits a chart's (indicator, series), or None when it does not.

    Each named part must fit a different level of the row label, in either order — the map puts
    the city under the index ('IKK', 'Medan') where Tabel 6 files the index under the city.
    Every level of the row must be accounted for, except an aggregate qualifier ('Total').
    """
    import itertools

    row_parts = [p.strip() for p in row_label.split(QUAL_SEP)]
    best: Optional[int] = None
    for placing in itertools.permutations(range(len(row_parts)), len(parts)):
        fits = [_part_fit(q, row_parts[i]) for q, i in zip(parts, placing)]
        if any(f is None for f in fits):
            continue
        rest = [row_parts[i] for i in range(len(row_parts)) if i not in placing]
        if any(_label_tokens(r)[0] - _UNNAMED_PARTS or _label_tokens(r)[1] for r in rest):
            continue
        score = sum(fits)
        best = score if best is None else min(best, score)
    return best


def _chart_row_names(
    readings: List[ChartReading], sources: List[_ExcelSource]
) -> Dict[Tuple[str, str], str]:
    """(indicator, series) -> the row name the tables use for it, where exactly one fits best.

    The generic label matcher answers a narrative claim, whose metric the extractor already named
    after a row it was shown. A chart's names are its own — 'IKK' and 'Rp1 - 2 juta' for the row
    'Indeks Keyakinan Konsumen (IKK) > Pengeluaran Rp1 - 2 juta' — and that matcher, rightly
    strict, finds nothing for them. So they are placed here, deterministically: the row that
    fits with the fewest words to spare, and only when no other row name ties it. A chart
    series with no such row stays under its own name and comes back Tidak Cukup Data.
    """
    labels = list(dict.fromkeys(
        label for src in sources if src.origin != "chart" for label in src.table.row_labels
    ))
    names: Dict[Tuple[str, str], str] = {}
    for reading in readings:
        for point in reading.points:
            key = (reading.indicator, point.series)
            if key in names:
                continue
            parts = [p for p in (reading.indicator, point.series) if p.strip()]
            if len(parts) == 2 and parts[0].strip().lower() == parts[1].strip().lower():
                parts = parts[:1]
            fits = [(fit, label) for label in labels
                    if (fit := _chart_row_fit(parts, label)) is not None]
            if not fits:
                continue
            best = min(fit for fit, _ in fits)
            winners = {label for fit, label in fits if fit == best}
            if len(winners) == 1:
                names[key] = winners.pop()
    return names


def _chart_source(
    reading: ChartReading,
    filename: str,
    row_names: Optional[Dict[Tuple[str, str], str]] = None,
    table_sources: Optional[List[_ExcelSource]] = None,
    checks: Optional[List[Tuple[ExtractedFact, FactVerificationResult]]] = None,
) -> _ExcelSource:
    """A chart's level labels as a sparse source, keyed by the tables' own row names where
    _chart_row_names found them — so a narrative claim resolves against the chart exactly as it
    does against the table, and the two can be compared.

    Two labels the model placed on the same series and period with different numbers cannot
    both be right, and nothing says which is — so neither is kept.

    The unit is the one the tables give those rows, when they agree on one: the axis says
    "Indeks" where the appendix declares nothing, and _units_comparable would otherwise keep a
    chart that contradicts its table from ever being reported.
    """
    row_names = row_names or {}
    data: Dict[Tuple, float] = {}
    clashing: set = set()
    for label, year, month, value in (
        _settled_levels(checks) if checks is not None else _read_levels(reading, row_names)
    ):
        key = (label, year, month)
        point_value = value
        if key in data and abs(data[key] - point_value) > MATCH_TOLERANCE:
            clashing.add(key)
        data.setdefault(key, point_value)
    for key in clashing:
        del data[key]
    labels = list(dict.fromkeys(key[0] for key in data))
    row_units = {src.table.unit for src in (table_sources or []) if src.origin != "chart"
                 for label in labels if label in src.table.row_labels}
    table = BITableData(
        title=" ".join(p for p in (reading.caption, reading.title) if p),
        unit=row_units.pop() if len(row_units) == 1 else reading.unit,
        row_labels=labels,
    )
    table._data.update(data)
    return _ExcelSource(table=table, filename=filename, sheet=reading.label, origin="chart")


def _read_levels(reading: ChartReading, row_names: Dict[Tuple[str, str], str]):
    """(row, year, month, value) for every level label, where the model placed it."""
    for point in reading.points:
        if point.kind == "level":
            label = (row_names.get((reading.indicator, point.series))
                     or reading.metric_label(point.series))
            yield label, point.year, point.month, point.value


def _settled_levels(checks: List[Tuple[ExtractedFact, FactVerificationResult]]):
    """(row, year, month, value) for the level labels a chart can be trusted with.

    A label the tables confirmed goes where they confirmed it — the settled period, not the one
    the model read (see _settle_chart_placement). A label no table could speak to (its series
    has no row) stays where it was read: that is what the chart is a fallback FOR. A label the
    tables contradict, or that matched another line of the chart, is left out.
    """
    for fact, result in checks:
        if fact.operation != "value":
            continue
        if result.verdict == "Entailed" and result.periods:
            p = result.periods[0]
            yield p.metric_label, p.year, p.month, fact.claimed_value
        elif result.verdict == "Inconclusive" and result.matched_excel_source is None:
            p = fact.periods[0]
            yield p.metric_label, p.year, p.month, fact.claimed_value


def chart_label_facts(
    reading: ChartReading, row_names: Optional[Dict[Tuple[str, str], str]] = None
) -> List[ExtractedFact]:
    """One checkable fact per printed label: the value it shows, for the series and period it
    sits on. A change label ('Δ -8,9' on a map) is the difference from the period before.
    Named after the tables' row wherever _chart_row_names placed the series."""
    row_names = row_names or {}
    facts: List[ExtractedFact] = []
    for point in reading.points:
        label = row_names.get((reading.indicator, point.series)) or reading.metric_label(point.series)
        when = f"{_MONTH_ID.get(point.month, point.month)} {point.year}"
        quote = (f"{reading.caption} · {reading.title}: "
                 f"{point.series or reading.indicator}, {when} = {point.raw}")
        if point.kind == "change":
            previous = _shift_period(point.year, point.month, -1)
            if previous is None:
                continue
            facts.append(ExtractedFact(
                operation="diff",
                periods=[PeriodPoint(label, *previous), PeriodPoint(label, point.year, point.month)],
                claimed_value=point.value, unit=reading.unit or None,
                context_quote=quote + " (perubahan dari bulan sebelumnya)",
                page_number=reading.page_number,
            ))
        else:
            facts.append(ExtractedFact(
                operation="value",
                periods=[PeriodPoint(label, point.year, point.month)],
                claimed_value=point.value, unit=reading.unit or None,
                context_quote=quote, page_number=reading.page_number,
            ))
    return facts


def _settle_chart_placement(
    fact: ExtractedFact, result: FactVerificationResult, sources: List[_ExcelSource]
) -> FactVerificationResult:
    """Check a chart label's NUMBER when the model misplaced it by one bar or point.

    The number on a chart is read reliably; WHICH bar or point it belongs to is the model's
    judgement, and it is the part it gets wrong. Measured on SK-Juni-2026 against its workbook:
    the bar charts label only the last two of each group's three bars, and the model put the
    two labels on bars one and two (or one and three) in 59 of 233 labels — each off by exactly
    one month, every number itself right. Reported as mismatches, those are 59 false alarms
    about a report with no error in it.

    So a label that misses its own period but equals the SAME row one period either side is
    counted as matching the table, and the reasoning says exactly that, naming both periods.
    What this gives up, deliberately: a chart that was not updated and shows every value one
    month late reads the same way, and is not caught. A wrong number, a number from another
    group or series, and a number from further away still are.
    """
    if result.verdict != "Refuted" or fact.operation != "value" or not result.periods:
        return result
    source = next((s for s in sources if s.label == result.matched_excel_source), None)
    point = fact.periods[0]
    if source is None or point.year is None or point.month is None:
        return result
    row = result.periods[0].metric_label
    for step in (-1, 1):
        shifted = _shift_period(point.year, point.month, step)
        if shifted is None:
            continue
        value = source.table.lookup(row, *shifted)
        if value is None or abs(value - fact.claimed_value) > MATCH_TOLERANCE:
            continue
        read_at = f"{_MONTH_ID.get(point.month, point.month)} {point.year}"
        matched_at = f"{_MONTH_ID.get(shifted[1], shifted[1])} {shifted[0]}"
        return result.model_copy(update={
            "verdict": "Entailed",
            "computed_value": round(value, 4),
            "delta": round(abs(value - fact.claimed_value), 4),
            "periods": [result.periods[0].model_copy(update={
                "year": shifted[0], "month": shifted[1], "excel_value": value,
            })],
            "reasoning": (
                f"Angka grafik {fact.claimed_value} sama dengan nilai tabel "
                f"[{source.label}] ({row}) untuk {matched_at}. Model membaca label ini pada "
                f"posisi {read_at}; letak label pada gambar tidak dapat dipastikan, jadi yang "
                f"diverifikasi adalah angkanya. (Grafik yang belum diperbarui satu bulan tidak "
                f"tertangkap dengan cara ini.)"
            ),
        })
    return result


def _settle_chart_series(
    fact: ExtractedFact,
    result: FactVerificationResult,
    reading: ChartReading,
    row_names: Dict[Tuple[str, str], str],
    sources: List[_ExcelSource],
) -> FactVerificationResult:
    """A label that fails its own series but is ANOTHER series of the same chart: undecided.

    On a line chart the model tells series apart by colour, and gets it wrong: on SK-Juni-2026's
    Grafik 4 it filed the green IPDG labels (105,9 · 108,3 · 105,9) under the red IKLK line and
    the red ones under the green, and a chart with nothing wrong in it came back with six Tidak
    Sesuai. A chart whose AUTHOR swapped the legend looks exactly the same, so the label is
    neither passed nor failed: it comes back Tidak Cukup Data, naming the series it matches, for
    a person to look at. Same period or one either side, as in _settle_chart_placement.
    """
    if result.verdict != "Refuted" or fact.operation != "value":
        return result
    own = fact.periods[0].metric_label
    point = fact.periods[0]
    others = {row_names.get((reading.indicator, p.series)) for p in reading.points} - {None, own}
    for row in sorted(others):
        for step in (0, -1, 1):
            when = _shift_period(point.year, point.month, step) if step else (point.year, point.month)
            if when is None:
                continue
            for src in sources:
                if src.origin == "chart" or row not in src.table.row_labels:
                    continue
                value = src.table.lookup(row, *when)
                if value is None or abs(value - fact.claimed_value) > MATCH_TOLERANCE:
                    continue
                matched_at = f"{_MONTH_ID.get(when[1], when[1])} {when[0]}"
                return result.model_copy(update={
                    "verdict": "Inconclusive",
                    "reasoning": (
                        f"Angka grafik {fact.claimed_value} tidak cocok dengan seri yang dibaca "
                        f"model ({own}), tetapi sama dengan seri lain di grafik yang sama: "
                        f"{row} {matched_at} di [{src.label}]. Kemungkinan warna garis/legenda "
                        f"tertukar saat dibaca — atau legenda grafiknya memang tertukar. "
                        f"Periksa grafik ini secara manual. | {result.reasoning}"
                    ),
                })
    return result


_CITED_CHART_RE = re.compile(r"\bGrafik\s*(\d+)", re.IGNORECASE)


def _settle_by_cited_chart(
    fact: ExtractedFact,
    result: FactVerificationResult,
    readings: List[ChartReading],
    row_names: Dict[Tuple[str, str], str],
    table_sources: List[_ExcelSource],
) -> FactVerificationResult:
    """Re-check a refuted figure against the chart its own sentence cites.

    The extractor names a claim's metric from the sentence, and a sentence that describes an
    index instead of naming it can be filed under the wrong one: SK-Agustus-2026's "keyakinan
    konsumen terhadap penghasilan saat ini … >Rp5 juta, yaitu sebesar 125,9 (Grafik 5)" came
    back as IKE (116,4) and was refuted, while Grafik 5 — IPSI per kelompok pengeluaran —
    prints 125,9 for >Rp5 juta and the appendix holds 125,9 for that row.

    So when a refuted value claim cites "Grafik N", and that chart prints the claimed figure on
    the SAME group (the claim's last level fits the chart series) at that period or one bar
    either side, and the table row that series was placed on holds the figure AT THE CLAIM'S
    PERIOD, the claim is judged against that row instead — Sesuai, with the reasoning saying which chart and row decided it. All three
    have to agree; anything less leaves the verdict as it was.
    """
    if result.verdict != "Refuted" or fact.operation != "value" or len(fact.periods) != 1:
        return result
    cited = {f"grafik{n}" for n in _CITED_CHART_RE.findall(fact.context_quote or "")}
    point = fact.periods[0]
    if not cited or point.year is None or point.month is None:
        return result
    group = (point.metric_label or "").rsplit(QUAL_SEP, 1)[-1]
    for reading in readings:
        if re.sub(r"\s+", "", reading.caption.lower()) not in cited:
            continue
        for label in reading.points:
            # The chart only says WHICH series the figure belongs to; the table, at the claim's own
            # period, decides. So a label the model placed one bar off still identifies it (see
            # _settle_chart_placement for why placement is the model's weak point).
            near = {(point.year, point.month)} | {
                shifted for step in (-1, 1)
                if (shifted := _shift_period(point.year, point.month, step)) is not None
            }
            if (label.kind != "level" or (label.year, label.month) not in near
                    or abs(label.value - fact.claimed_value) > MATCH_TOLERANCE):
                continue
            if _part_fit(label.series, group) is None and _part_fit(group, label.series) is None:
                continue
            row = row_names.get((reading.indicator, label.series))
            for src in table_sources:
                if row is None or src.origin == "chart" or row not in src.table.row_labels:
                    continue
                value = src.table.lookup(row, point.year, point.month)
                if value is None or abs(value - fact.claimed_value) > MATCH_TOLERANCE:
                    continue
                return result.model_copy(update={
                    "verdict": "Entailed",
                    "matched_excel_source": src.label,
                    "computed_value": round(value, 4),
                    "delta": round(abs(value - fact.claimed_value), 4),
                    "reasoning": (
                        f"Kalimat ini merujuk {reading.caption} ({reading.title}), yang mencetak "
                        f"{fact.claimed_value} untuk {label.series}; tabel [{src.label}] memuat "
                        f"nilai yang sama pada baris {row}. Klaim dinilai terhadap baris itu, "
                        f"bukan '{point.metric_label}' yang dipetakan dari kalimatnya. "
                        f"| Sebelumnya: {result.reasoning}"
                    ),
                })
    return result


def check_chart_labels(
    readings: List[ChartReading],
    table_sources: List[_ExcelSource],
    row_names: Optional[Dict[Tuple[str, str], str]] = None,
) -> List[Tuple[ExtractedFact, FactVerificationResult]]:
    """Every printed chart label, checked against the tables: (fact, result) per label.

    Against the tables only — never against a chart, its own included — and only once
    _chart_row_names has placed the label's series on a table row. An unplaced series is not
    handed to the generic fuzzy matcher nor to the AI cell pointer: a chart's own naming is
    exactly what those guess wrong, so it comes back Tidak Cukup Data instead.
    """
    if row_names is None:
        row_names = _chart_row_names(readings, table_sources)
    return [check for reading in readings
            for check in _check_reading(reading, table_sources, row_names)]


def _check_reading(
    reading: ChartReading,
    table_sources: List[_ExcelSource],
    row_names: Dict[Tuple[str, str], str],
) -> List[Tuple[ExtractedFact, FactVerificationResult]]:
    """check_chart_labels for one chart."""
    placed = set(row_names.values())
    checks: List[Tuple[ExtractedFact, FactVerificationResult]] = []
    for fact in chart_label_facts(reading, row_names):
        if fact.periods[0].metric_label in placed:
            result = _settle_chart_placement(
                fact, _evaluate_fact_safely(fact, table_sources), table_sources
            )
            result = _settle_chart_series(fact, result, reading, row_names, table_sources)
        else:
            result = _inconclusive_result(fact, None, (
                f"Seri grafik '{fact.periods[0].metric_label}' tidak dapat dipastikan "
                f"padanannya di tabel mana pun."
            ))
        checks.append((fact, result.model_copy(update={"checked_item": "chart"})))
    return checks


# Which verdict a split chart takes: the worse one, so one wrong label is never outvoted into a
# green card by a tie.
_VERDICT_SEVERITY = {"Refuted": 2, "Inconclusive": 1, "Entailed": 0}


def summarize_chart(
    reading: ChartReading, checks: List[Tuple[ExtractedFact, FactVerificationResult]]
) -> ChartCheck:
    """One chart's label checks as a single ChartCheck — see schemas.ChartCheck for the rule."""
    counts = {verdict: 0 for verdict in _VERDICT_SEVERITY}
    for _, result in checks:
        counts[result.verdict] += 1
    verdict = max(counts, key=lambda v: (counts[v], _VERDICT_SEVERITY[v]))
    return ChartCheck(
        page_number=reading.page_number,
        caption=reading.caption,
        title=reading.title,
        verdict=verdict,
        label_count=len(checks),
        entailed_count=counts["Entailed"],
        refuted_count=counts["Refuted"],
        inconclusive_count=counts["Inconclusive"],
        issues=[result for _, result in checks if result.verdict != "Entailed"],
        thumbnail=reading.thumbnail,
    )


async def verify_paired(
    narrative_text: str,
    excel_sources: List[Tuple[bytes, str, str]],
    llm: BaseChatModel,
    pdf_filename: str = "report.pdf",
    vision_llm: Optional[BaseChatModel] = None,
    progress_cb: ProgressCb = None,
    pdf_tables: Optional[List[PdfTable]] = None,
    mode: str = "excel",
    on_extract_gap: Optional[Callable[[List[int], Exception], None]] = None,
    reference_tables: Optional[List[Tuple[str, List[PdfTable]]]] = None,
    chart_readings: Optional[List[ChartReading]] = None,
    chart_pages_unread: Optional[List[int]] = None,
    publication: str = "",
) -> PairedVerificationResponse:
    """Verify all quantitative claims in a PDF narrative against one or more reference tables.

    Args:
        narrative_text: Already-extracted PDF narrative text (with [== Halaman N ==] page markers),
                        e.g. from pdf_extraction.extract_narrative_text(). Extraction is the
                        caller's responsibility so the same text can be reused for other checks
                        (e.g. typo_checker.check_typos) without re-running the vision LLM fallback.
        excel_sources:  List of (excel_bytes, sheet_name, filename) tuples.
                        Claims are checked against every source that can resolve them; the
                        closest label match produces the verdict (see _evaluate_fact).
        llm:            Fallback chat model (used when vision_llm is unavailable).
        pdf_filename:   Display name for the PDF (metadata only).
        vision_llm:     Vision-capable model (Gemini). When provided, used as the PRIMARY
                        model for structured fact extraction, since it handles Indonesian
                        number formats more reliably than Groq.
        progress_cb:    Optional per-stage progress callback (see ProgressCb above). None
                        disables reporting; the pipeline is otherwise identical.
        pdf_tables:     Tables transcribed from the PDF itself (pdf_table_extraction), used as
                        reference sources alongside — or instead of — the Excel ones. Transcribing
                        them is the caller's responsibility for the same reason as narrative_text.
        mode:           "excel" | "internal" | "both" | "none". Metadata plus two behaviour
                        switches: table-family suggestions are pointless without Excel in the
                        pool, and the response echoes the mode back so the UI can caveat
                        LLM-read references. "none" means only reference_tables are consulted.
        on_extract_gap: Optional (pages, error) callback for each narrative chunk the extraction
                        model failed on — see extract_structured_facts_async's on_chunk_failed.
        reference_tables: (filename, tables) per OTHER PDF uploaded in the same run. A fallback
                        only: they answer a claim the report's own tables and the Excel cannot,
                        and otherwise can only raise a "cross_pdf" conflict (see _evaluate_fact).
        chart_readings: Labels read off the report's own charts (pdf_chart_extraction). Each
                        label is checked against the tables like a claim (checked_item="chart"),
                        and each chart is a fallback source for the narrative — it answers only
                        what no table can, and otherwise can raise a "chart" conflict.
        chart_pages_unread: Pages whose charts were located but not read; echoed back.
        publication:    The publication type the checker picked ("sulni", "npi", ...). Its own
                        parser reads the sheets it covers before the generic cascade (see
                        _parse_table_with_fallback); empty means the generic cascade only.

    Returns:
        PairedVerificationResponse with per-fact verdicts.
    """
    def emit(stage: str, status: str, **extra) -> None:
        if progress_cb is not None:
            progress_cb({"type": "stage", "stage": stage, "status": status, **extra})

    # Step 1: Parse all Excel sources (BI layout → generic heuristics → LLM structure mapping)
    parsed_sources: List[_ExcelSource] = []
    excel_parsers: List[str] = []
    for i, (excel_bytes, sheet_name, filename) in enumerate(excel_sources, 1):
        logger.info("Parsing Excel sheet '%s' from '%s'", sheet_name, filename)
        emit(
            "excel", "running", current=i - 1, total=len(excel_sources),
            detail=f"{filename} / {sheet_name}",
        )
        try:
            table, parser_used = _parse_table_with_fallback(
                excel_bytes, sheet_name, llm=llm, publication=publication
            )
        except ValueError as parse_error:
            # Every parser tier failed. If the raw grid still loads, keep the source as
            # POINTER-ONLY: an empty table whose claims can only be answered by the
            # tier-4 cell-pointer pass. Unloadable bytes (bad sheet name, corrupt file)
            # keep surfacing the original parser error.
            try:
                grid = _load_grid(excel_bytes, sheet_name)
            except ValueError:
                raise parse_error
            logger.warning(
                "All parsers failed for '%s' / '%s' (%s) — keeping as pointer-only source.",
                filename, sheet_name, parse_error,
            )
            parsed_sources.append(_ExcelSource(
                table=BITableData(title="", unit="", row_labels=[]),
                filename=filename, sheet=sheet_name, grid=grid, pointer_only=True,
            ))
            excel_parsers.append("pointer-only")
            continue
        logger.info(
            "Excel parsed via %s parser (%s axis): %d rows, unit='%s'",
            parser_used, table.axis_type, len(table.row_labels), table.unit,
        )
        try:
            grid = _load_grid(excel_bytes, sheet_name)
        except ValueError:
            grid = None
        parsed_sources.append(_ExcelSource(
            table=table, filename=filename, sheet=sheet_name, grid=grid,
        ))
        excel_parsers.append(parser_used)
    if excel_sources:
        emit(
            "excel", "done",
            detail=f"{len(parsed_sources)} sumber · parser: {', '.join(excel_parsers)}",
        )

    # Step 1b: PDF-internal tables become sources too. Appended AFTER the Excel ones so that in
    # "both" mode an equal-scoring Excel sheet keeps the headline verdict (ties keep the earlier
    # source) — the PDF value then shows up as the second source_values entry instead of
    # silently changing numbers the user already trusts.
    for table_from_pdf in (pdf_tables or []):
        source, parser_used = _pdf_table_source(table_from_pdf, pdf_filename, "pdf", llm)
        parsed_sources.append(source)
        excel_parsers.append(parser_used)
    if pdf_tables:
        detail = (f"{len(pdf_tables)} tabel internal · parser: "
                  f"{', '.join(excel_parsers[-len(pdf_tables):])}")
        if vision_llm is None:
            # Without a vision model the transcriber only reads pages that carry a text layer,
            # so coverage can be partial and a claim whose table sits on an unread page comes
            # back Inconclusive. Say so here rather than let the user guess at the verdicts.
            detail += " · tanpa model vision, hanya halaman dengan lapisan teks yang dibaca"
        emit("tables", "done", detail=detail)

    # Step 1c: tables of the OTHER PDFs uploaded in the same run, last of all. Their place in
    # the list does not matter for the verdict — _evaluate_fact keeps them a fallback — but it
    # keeps the source arrays reading own-report-first.
    for reference_name, reference_pdf_tables in (reference_tables or []):
        for table_from_pdf in reference_pdf_tables:
            source, parser_used = _pdf_table_source(table_from_pdf, reference_name, "pdf_other", llm)
            parsed_sources.append(source)
            excel_parsers.append(parser_used)

    # Per-source label groups with table title context (used by the LLM to understand
    # what generic rows like 'Total' represent in each table). Categorical sources also
    # advertise their attribute columns so the LLM can fill col_label with a real name.
    def _source_desc(src: _ExcelSource) -> str:
        if src.origin == "pdf":
            origin = src.sheet
        elif src.origin == "pdf_other":
            origin = f"{src.filename} · {src.sheet}"
        else:
            origin = src.filename
        desc = f"{src.table.title} / {origin}"
        if src.pointer_only:
            desc += " — struktur tabel tidak terurai; gunakan nama metrik apa adanya"
        if src.table.axis_type == "categorical" and src.table.col_labels:
            desc += " — kolom atribut (non-waktu): " + ", ".join(src.table.col_labels)
        return desc

    # Internal mode can produce a dozen sources, and _build_row_labels_block prints every
    # advertised label into EVERY extraction chunk's prompt — cap the per-source list so the
    # prompt does not grow with the page count. all_row_labels below is already deduplicated.
    source_labels_for_extractor = _merge_sources_sharing_rows([
        (_source_desc(src), src.table.row_labels[:_MAX_LABELS_PER_SOURCE])
        for src in parsed_sources
    ])

    # Combined flat list for de-duplication (required by extract_structured_facts_async signature)
    all_row_labels: List[str] = []
    seen_labels: set = set()
    for src in parsed_sources:
        for label in src.table.row_labels:
            if label not in seen_labels:
                all_row_labels.append(label)
                seen_labels.add(label)

    # Step 1d: the report's charts. Added AFTER the extractor's label lists are built: a chart's
    # labels are the model's own composition ('IKK > Rp1 - 2 juta'), and offering them as row
    # names would steer how the narrative's claims get named.
    table_sources = list(parsed_sources)
    row_names = _chart_row_names(chart_readings or [], table_sources)
    per_chart = [(reading, _check_reading(reading, table_sources, row_names))
                 for reading in (chart_readings or [])]
    for reading, checks in per_chart:
        parsed_sources.append(
            _chart_source(reading, pdf_filename, row_names, table_sources, checks=checks)
        )
        excel_parsers.append("chart")
    chart_checks = [summarize_chart(reading, checks) for reading, checks in per_chart if checks]
    chart_facts = [fact for _, checks in per_chart for fact, _ in checks]

    # Step 2: Extract structured facts.
    logger.info("Running structured fact extraction (combined row labels from %d source(s))", len(parsed_sources))
    extraction_primary = vision_llm if vision_llm is not None else llm
    extraction_fallback = llm if vision_llm is not None else None

    def _on_chunk_progress(done: int, total: int) -> None:
        emit("extract", "running", current=done, total=total, detail=f"{total} bagian teks")

    raw_facts = await extract_structured_facts_async(
        narrative_text,
        all_row_labels,
        extraction_primary,
        fallback_llm=extraction_fallback,
        source_labels=source_labels_for_extractor,
        on_progress=_on_chunk_progress,
        on_chunk_failed=on_extract_gap,
    )
    facts = _deduplicate_facts(raw_facts)
    logger.info("%d unique facts after deduplication (was %d)", len(facts), len(raw_facts))
    emit("extract", "done", detail=f"{len(facts)} klaim ditemukan")

    excel_filenames = [src.filename for src in parsed_sources]
    excel_sheets = [src.sheet for src in parsed_sources]
    excel_units = [src.table.unit for src in parsed_sources]

    if not facts and not chart_facts:
        emit("compare", "done", detail="Tidak ada klaim untuk dibandingkan")
        return PairedVerificationResponse(
            pdf_filename=pdf_filename,
            excel_filenames=excel_filenames,
            excel_sheets=excel_sheets,
            excel_units=excel_units,
            excel_parsers=excel_parsers,
            mode=mode,
            total_facts=0,
            entailed_count=0,
            refuted_count=0,
            inconclusive_count=0,
            results=[],
        )

    # Step 3: Direct comparison (no SQL) — each fact is checked across all sources
    emit("compare", "running", detail=f"{len(facts)} klaim")
    results: List[FactVerificationResult] = [
        _evaluate_fact_safely(fact, parsed_sources) for fact in facts
    ]
    # Step 3b: tier-4 cell-pointer pass for claims no source could resolve. Vision LLM
    # (Gemini) first — big grid snapshots trip Groq's TPM limits more readily — with the
    # text LLM as fallback; any failure keeps the original Inconclusive results.
    n_pointer = 0
    n_unresolved = sum(
        1 for r in results if r.verdict == "Inconclusive" and r.matched_excel_source is None
    )
    if n_unresolved and any(s.grid for s in parsed_sources):
        emit("compare", "running", detail=f"penunjukan sel AI: {n_unresolved} klaim")
        results, n_pointer = await _pointer_pass(
            facts, results, table_sources,
            llm=vision_llm or llm,
            fallback_llm=llm if vision_llm is not None else None,
        )

    if chart_readings:
        results = [
            _settle_by_cited_chart(fact, result, chart_readings, row_names, table_sources)
            for fact, result in zip(facts, results)
        ]

    entailed = sum(1 for r in results if r.verdict == "Entailed")
    refuted = sum(1 for r in results if r.verdict == "Refuted")
    inconclusive = sum(1 for r in results if r.verdict == "Inconclusive")
    conflicts = sum(1 for r in results if r.source_conflict is not None)
    compare_detail = (
        f"{entailed} sesuai · {refuted} tidak sesuai · {inconclusive} tidak dapat dipastikan"
    )
    if chart_checks:
        n_ok = sum(1 for c in chart_checks if c.verdict == "Entailed")
        compare_detail += f" · grafik: {n_ok} dari {len(chart_checks)} sesuai"
    if n_pointer:
        compare_detail += f" · {n_pointer} via sel AI"
    if conflicts:
        compare_detail += f" · {conflicts} sumber bertentangan"
    emit("compare", "done", detail=compare_detail)

    return PairedVerificationResponse(
        pdf_filename=pdf_filename,
        excel_filenames=excel_filenames,
        excel_sheets=excel_sheets,
        excel_units=excel_units,
        excel_parsers=excel_parsers,
        mode=mode,
        reference_pdfs=[name for name, _ in (reference_tables or [])],
        conflict_count=conflicts,
        total_facts=len(results),
        entailed_count=entailed,
        refuted_count=refuted,
        inconclusive_count=inconclusive,
        results=results,
        # The BI table-family hints tell the user which WORKBOOK to upload — noise in the modes
        # where they deliberately opted out of uploading one.
        table_suggestions=(
            [] if mode in ("internal", "none") else _build_table_suggestions(results)
        ),
        chart_checks=chart_checks,
        chart_label_count=len(chart_facts),
        chart_pages_unread=sorted(chart_pages_unread or []),
    )
