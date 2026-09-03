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

import asyncio
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
    resolve_pointers,
)
from excel_parser_bi import BITableData, parse_bi_table
from pdf_table_extraction import PdfTable
from table_model import QUAL_SEP, _sig_words, label_match_score
from table_parser_generic import (
    _MONTH_ABBREVS,
    _load_grid,
    parse_generic_grid,
    parse_generic_table,
)
from table_parser_llm import parse_grid_with_llm, parse_table_with_llm
from schemas import (
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
_STABLE_RELATIVE_BAND = 0.025

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
    # itself. Only two things branch on it: labelling a conflict internal-vs-cross, and
    # suppressing the BI-workbook table suggestions in internal mode.
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


def _parse_scale_unit(unit: str) -> Optional[Tuple[float, Optional[str]]]:
    """Parse a level unit into (decimal scale, currency token or None).

    'juta Rp' -> (1e6, 'rp') | 'Rp' -> (1.0, 'rp') | 'miliar' -> (1e9, None).
    Returns None for percentages and units with neither a scale word nor a currency
    ('unit', 'buah'), where scaling would be meaningless.
    """
    if "%" in unit:
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
        if (QUAL_SEP in (label or "")) != (QUAL_SEP in (p.metric_label or ""))
    )
    return 1 if mismatches else 0


def _coverage_score(
    periods: List[PeriodPoint], resolved: List[Tuple[str, float]], src: "_ExcelSource"
) -> float:
    """Mean TableData.query_coverage across a fact's data points.

    Ranks ABOVE _resolution_score: a source that leaves one of the claim's scope words
    unaccounted for ('UMKM', 'DPK') is answering a different question, however well the row
    name it found happens to read. See TableData.query_coverage for the cases.
    """
    scores = [
        src.table.query_coverage(p.metric_label, label)
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

    The band shrinks to nothing as the base grows (on 2.153,6 it is 0,005pp), so this changes
    nothing for the ordinary case and only stops the checker from claiming precision the
    source never had. A directly-printed growth cell is unaffected — there is no division.
    """
    if not prior:
        return MATCH_TOLERANCE
    half = _printing_step(prior) / 2
    band = 100 * (half / abs(prior)) * (1 + abs(current / prior))
    return max(MATCH_TOLERANCE, round(band, 4))


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

    band_note = ""
    if fact.operation == "is_increasing":
        ok = all(values[i + 1] >= values[i] for i in range(len(values) - 1))
    elif fact.operation == "is_decreasing":
        ok = all(values[i + 1] <= values[i] for i in range(len(values) - 1))
    else:  # is_stable — see _stable_band
        bands = [_stable_band(values[i], values[i + 1]) for i in range(len(values) - 1)]
        ok = all(
            abs(values[i + 1] - values[i]) <= bands[i] for i in range(len(values) - 1)
        )
        band_note = f" (ambang stabil ±{round(max(bands), 4)})"

    verdict = "Entailed" if ok else "Refuted"
    breakdown = ", ".join(f"{p.month} {p.year}={round(v, 4)}" for p, v in zip(used_periods, values))
    return _make_result(
        fact, periods, matched_source, None, None, None, src.table.unit, None, verdict,
        reasoning=(
            f"Klaim tren '{fact.operation}' untuk '{fact.display_label}' | "
            f"Excel [{matched_source}]: {breakdown}{band_note} | "
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
    """
    label, value = resolved[0]
    value = round(value, 4)
    matched_source = src.label
    periods = _build_periods(fact.periods, resolved, [value])
    threshold = fact.claimed_value
    if threshold is None:
        return _make_result(
            fact, periods, matched_source, None, fact.unit, value, fact.unit, None, "Inconclusive",
            reasoning="Klaim ambang tanpa nilai ambang yang jelas; tidak dapat dinilai.",
        )
    if fact.operation == "above_threshold":
        ok, arah = value > threshold, "di atas"
    else:
        ok, arah = value < threshold, "di bawah"
    verdict = "Entailed" if ok else "Refuted"
    return _make_result(
        fact, periods, matched_source, threshold, fact.unit, value, fact.unit, None, verdict,
        reasoning=(
            f"Klaim: {label} {arah} ambang {threshold} | "
            f"Excel [{matched_source}]: {label} = {value} | "
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
    return bool(leaf_words) and leaf_words <= _sig_words(quote)


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
            qualification=_qualification_rank(fact.periods, resolved),
            src=src, resolved=resolved, factor=factor, unit_comparable=unit_comparable,
        ))

    if not candidates:
        return _inconclusive_result(fact, best_missing, best_reason)

    # Coverage first, then label quality. Stable sort, so ties keep source order as before.
    candidates.sort(key=lambda c: (-c.coverage, c.qualification, -c.score))
    candidates = _prefer_growth_source_for_a_growth_trend(fact, candidates)
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

    return _attach_source_comparison(evaluated, head_index, displaced_source)


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
    says 1.195,6, and that 9,3 T gap is a real thing for a reader to know about. Only the
    implausible pairings are silenced, and only across universes — two credit tables that
    disagree are reported however far apart they are.
    """
    head_subject = head.src.table.table_subject()
    other_subject = other.src.table.table_subject()
    if head_subject is None or other_subject is None or head_subject == other_subject:
        return True
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
            conflict = "internal" if head.origin == other.origin == "pdf" else "cross"
            break

    reasoning = result.reasoning + note

    if conflict is None:
        return result.model_copy(update={"source_values": values, "reasoning": reasoning})

    def _fmt(sv: SourceValue) -> str:
        value = "tidak terhitung" if sv.computed_value is None else f"{sv.computed_value}"
        return f"[{sv.source}]: {value} ({sv.verdict})"

    note = (
        f" | KONFLIK SUMBER: {_fmt(head)} vs {_fmt(conflicting)} — "
        + ("dua tabel di dalam PDF saling bertentangan"
           if conflict == "internal" else "tabel di PDF dan sumber Excel tidak sinkron")
    )
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
    excel_bytes: bytes, sheet_name: str, llm: Optional[BaseChatModel] = None
) -> Tuple[BITableData, str]:
    """Return (table, parser_name) — three-tier cascade: BI → generic heuristic → LLM mapping.

    The BI parser stays the primary path so known SEKI files keep their exact current
    behaviour. Its result is accepted only when it actually extracted data; a structurally
    successful but EMPTY parse (or a ValueError) falls through to the generic parser. When
    that also fails and an llm is available, the LLM structure-mapping parser (tier 3) gets
    a shot — it only maps structure; values are still extracted by code. As the last resort,
    any BI result we did get is returned even when empty (claims then come back Inconclusive
    instead of the whole request failing); only when every tier raised is the combined error
    surfaced.
    """
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

async def _resolve_source_pointers(
    kept: List[Tuple[int, PointQuery]],
    grid: List[List],
    llm: BaseChatModel,
    fallback_llm: Optional[BaseChatModel] = None,
) -> Tuple[Dict[int, Tuple[int, int]], Optional[str]]:
    """resolve_pointers for one source's surviving queries, keyed back to global indices.

    resolve_pointers numbers the queries it is given from zero; the caller tracks them by
    their position in the full list, so the two numbering schemes are translated here.
    An empty selection short-circuits without any LLM call.
    """
    if not kept:
        return {}, None
    local, sheet_unit = await resolve_pointers([q for _, q in kept], grid, llm, fallback_llm)
    return {kept[i][0]: coord for i, coord in local.items()}, sheet_unit


async def _pointer_pass(
    facts: List[ExtractedFact],
    results: List[FactVerificationResult],
    sources: List[_ExcelSource],
    llm: BaseChatModel,
    fallback_llm: Optional[BaseChatModel] = None,
) -> Tuple[List[FactVerificationResult], int]:
    """Re-resolve fully-unresolved claims via LLM cell pointers.

    Candidates are results that stayed Inconclusive without matching any source (the
    same predicate _build_table_suggestions uses). One batched pointer call per
    grid-bearing source; a fact is accepted from the first source (upload order) where
    EVERY needed cell — including the synthesized prior-year point for yoy_growth —
    yields a numeric value via read_grid_cell. The values are injected into a fresh
    minimal TableData under the exact keys the fact asks for, and the ordinary
    _evaluate_fact machinery computes the verdict; the LLM never supplies a number.
    A wrong pointer is therefore visible (cell refs are appended to reasoning and
    resolved_via='pointer' is set) but can never invent data.
    """
    candidate_idx = [
        i for i, r in enumerate(results)
        if r.verdict == "Inconclusive" and r.matched_excel_source is None
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

    resolutions = await asyncio.gather(*[
        _resolve_source_pointers(kept, src.grid, llm, fallback_llm)
        for src, kept in zip(grid_sources, per_source_queries)
    ])

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
            res = _evaluate_fact(facts[fi], [patched])
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


async def verify_paired(
    narrative_text: str,
    excel_sources: List[Tuple[bytes, str, str]],
    llm: BaseChatModel,
    pdf_filename: str = "report.pdf",
    vision_llm: Optional[BaseChatModel] = None,
    progress_cb: ProgressCb = None,
    pdf_tables: Optional[List[PdfTable]] = None,
    mode: str = "excel",
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
        mode:           "excel" | "internal" | "both". Metadata plus two behaviour switches:
                        table-family suggestions are pointless in "internal" mode, and the
                        response echoes the mode back so the UI can caveat LLM-read references.

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
            table, parser_used = _parse_table_with_fallback(excel_bytes, sheet_name, llm=llm)
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
        parsed_sources.append(_ExcelSource(
            table=table,
            filename=pdf_filename,
            sheet=table_from_pdf.label,
            grid=table_from_pdf.grid,
            pointer_only=pointer_only,
            origin="pdf",
        ))
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

    # Per-source label groups with table title context (used by the LLM to understand
    # what generic rows like 'Total' represent in each table). Categorical sources also
    # advertise their attribute columns so the LLM can fill col_label with a real name.
    def _source_desc(src: _ExcelSource) -> str:
        origin = src.sheet if src.origin == "pdf" else src.filename
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
    )
    facts = _deduplicate_facts(raw_facts)
    logger.info("%d unique facts after deduplication (was %d)", len(facts), len(raw_facts))
    emit("extract", "done", detail=f"{len(facts)} klaim ditemukan")

    excel_filenames = [src.filename for src in parsed_sources]
    excel_sheets = [src.sheet for src in parsed_sources]
    excel_units = [src.table.unit for src in parsed_sources]

    if not facts:
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
    results: List[FactVerificationResult] = [_evaluate_fact(fact, parsed_sources) for fact in facts]

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
            facts, results, parsed_sources,
            llm=vision_llm or llm,
            fallback_llm=llm if vision_llm is not None else None,
        )

    entailed = sum(1 for r in results if r.verdict == "Entailed")
    refuted = sum(1 for r in results if r.verdict == "Refuted")
    inconclusive = sum(1 for r in results if r.verdict == "Inconclusive")
    conflicts = sum(1 for r in results if r.source_conflict is not None)
    compare_detail = (
        f"{entailed} sesuai · {refuted} tidak sesuai · {inconclusive} tidak dapat dipastikan"
    )
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
        conflict_count=conflicts,
        total_facts=len(results),
        entailed_count=entailed,
        refuted_count=refuted,
        inconclusive_count=inconclusive,
        results=results,
        # The BI table-family hints tell the user which WORKBOOK to upload — noise in internal
        # mode, where they deliberately opted out of uploading one.
        table_suggestions=[] if mode == "internal" else _build_table_suggestions(results),
    )
