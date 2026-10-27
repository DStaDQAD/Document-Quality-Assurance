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

import copy
import logging
import re
from dataclasses import dataclass
from decimal import ROUND_HALF_UP, Decimal
from typing import Any, Callable, Dict, List, Optional, Tuple

import yaml
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
from publication_parsers import notes_enabled, notes_for, parse_for_publication, skipped_sections
import team_settings
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
    attach_section_headings,
    drop_sections,
    extract_structured_facts,
    extract_structured_facts_async,
    extraction_llms,
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
    # How many words of the table's title (and TableData.subject_words) the SENTENCE uses. Breaks
    # a full tie for a claim WITHOUT a number, which used to go to the first source: SKDU's Tabel 1, 4, 5 and 7 all have a 'PERTANIAN, KEHUTANAN DAN
    # PERIKANAN' row, and "tingkat penggunaan tenaga kerja LU Pertanian … dari -0,23% menjadi
    # -0,10%" went to Tabel 1 (1,74 -> 0,39) because it came first, not to Tabel 4.
    sentence_title: int = 0


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
# English plurals and 'US$' are how SDDS-template sheets write units ("Millions of US$", Tabel
# Cadev ORA); '$' is not a word character, so 'US$' reaches the parser as 'us'.
_SCALE_WORDS: Dict[str, float] = {
    "ribu": 1e3, "ribuan": 1e3, "thousand": 1e3, "thousands": 1e3,
    "juta": 1e6, "million": 1e6, "millions": 1e6,
    "miliar": 1e9, "milyar": 1e9, "billion": 1e9, "billions": 1e9,
    "triliun": 1e12, "trillion": 1e12, "trillions": 1e12,
}
_CURRENCY_TOKENS: Dict[str, str] = {
    "rp": "rp", "rupiah": "rp", "idr": "rp",
    "usd": "usd", "dolar": "usd", "dollar": "usd", "us": "usd",
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
                # Out of words to join: step over just this one. Leaving `span` at the length
                # that ran out skipped the next word too — 'of' swallowed 'us' in 'Millions of
                # US$', and the unit lost its currency.
                span = 1
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
    # The same counted thing in two scales: '4,0 juta orang' against 'ribuan orang'. Both sides
    # become full numbers; the nouns only have to name the same KIND of quantity, so "2,0 juta
    # wisnas" (NPI Tw II-2026) is people like 'ribuan orang', while barrels or tonnes never are.
    if pdf_parsed and excel_parsed and not pdf_parsed[1] and not excel_parsed[1]:
        nouns = [set(re.findall(r"[a-z]+", k)) - set(_SCALE_WORDS) for k in key]
        if nouns[0] and nouns[1] and _quantity_kind(nouns[0]) == _quantity_kind(nouns[1]):
            return pdf_parsed[0] / excel_parsed[0]
    return None


# Nouns that name a measured quantity. Any other noun after a scale word ('juta wisnas', 'ribu
# kunjungan', 'ribuan orang') counts things, and counts compare with each other.
_MEASURED_NOUNS: Dict[str, str] = {
    "barel": "volume minyak", "barrel": "volume minyak", "bph": "volume minyak",
    "ton": "berat", "kg": "berat", "kilogram": "berat",
    "liter": "volume", "kl": "volume", "kiloliter": "volume",
    "bulan": "waktu", "hari": "waktu", "tahun": "waktu", "minggu": "waktu",
}


def _quantity_kind(nouns: set) -> str:
    kinds = {_MEASURED_NOUNS[n] for n in nouns if n in _MEASURED_NOUNS}
    return "/".join(sorted(kinds)) or "jumlah"


# Trailing parenthetical of a categorical column name, where such tables usually declare
# the column's unit: 'Harga (Rp)' -> 'Rp', 'Omzet (juta Rp)' -> 'juta Rp'.
_COL_UNIT_RE = re.compile(r"\(([^)]+)\)\s*$")


# A row that states its own unit, unlike the rest of its table: NPI's 'Transaksi Berjalan
# (% PDB)', 'Jumlah pelawat (ribuan orang)', 'Harga … (USD/barel)' under a 'Juta USD' title.
_ROW_UNIT_WORDS = re.compile(r"%|persen|orang|barel|ton\b|bulan|USD|Rp", re.IGNORECASE)


def _row_unit(labels: List[str], table: Optional[BITableData] = None) -> Optional[str]:
    """The unit every matched row declares in its own name, if they agree on one."""
    units = set()
    for label in labels:
        known = getattr(table, "row_units", {}).get(label) if table is not None else None
        if known:
            units.add(known)
            continue
        for level in reversed(label.split(QUAL_SEP.strip())):
            # 'Dalam Bulan Impor dan Pembayaran ULN Pemerintah' is counted in months.
            lead = re.match(r"\s*dalam\s+(bulan|persen)\b", level, re.IGNORECASE)
            unit = lead.group(1).lower() if lead else _col_unit(level.strip())
            if unit and _ROW_UNIT_WORDS.search(unit):
                units.add(unit)
                break
        else:
            return None
    return units.pop() if len(units) == 1 else None


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


# S1 / S2: semester columns (publication_parsers._common.SEMESTER_OF_HEADER).
_QUARTER_ORDINAL = {"Q1": 1, "Q2": 2, "Q3": 3, "Q4": 4, "S1": 1, "S2": 2}


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


# The period a sentence compares with: "lebih tinggi dibandingkan SBT pada triwulan III 2025",
# "lebih rendah dari semester II 2025", "dibandingkan dengan triwulan yang sama pada tahun
# sebelumnya". "dari" counts only after a direction word: "bersumber dari" names no baseline.
_UP_WORDS = r"lebih\s+(?:tinggi|besar|baik)|meningkat|naik"
_DOWN_WORDS = r"lebih\s+(?:rendah|kecil|buruk)|menurun|turun|melambat"
_COMPARED_PERIOD = re.compile(
    r"(?:\b(?P<dir>" + _UP_WORDS + "|" + _DOWN_WORDS + r")\s+(?:dari|dibandingkan)|\bdibandingkan)\b"
    r"[^.;]{0,60}?\b(?P<kind>triwulan|semester)\s+(?P<q>IV|III|II|I)\s+(?P<y>\d{4})\b",
    re.IGNORECASE)
_SAME_QUARTER_LAST_YEAR = re.compile(
    r"\bdibandingkan\b[^.;]{0,60}?\b(triwulan|periode)\s+yang\s+sama\s+(pada\s+)?tahun\s+"
    r"(sebelumnya|lalu)\b", re.IGNORECASE)
_SEMESTER_WORD = {"i": "S1", "ii": "S2"}


# "secara tahunan": the sentence's own baseline is the same period a year before — unless it also
# names another period to compare with, which then decides.
_YEAR_ON_YEAR_ONLY = re.compile(r"\bsecara\s+tahunan\b", re.IGNORECASE)
_COMPARES_WITH_A_PERIOD = re.compile(r"\b(dibandingkan|dibanding|sebelumnya|lalu)\b", re.IGNORECASE)


def _stated_baseline(
    quote: str, table: BITableData, label: str, year: int, month: str, operation: str = ""
) -> Optional[Tuple[Tuple[int, str], float]]:
    """The earlier quarter or semester the sentence itself names as the baseline, with its value —
    used in place of the previous period to complete a single-point trend claim.

    SBank Tw II-2026: "giro … lebih tinggi dibandingkan SBT pada triwulan III 2025" (50,82 ->
    76,38) arrived as Q3 2026 alone, and against Q2 2026 (80,36) it read as a fall. SKDU Tw
    III-2026: "Margin usaha pada semester II 2026 diprakirakan lebih tinggi dibandingkan semester I
    2026, namun lebih rendah dari semester II 2025" names two baselines; the rise is against the
    first, the fall against the second, so the one whose direction word agrees with the claim wins.
    None when the sentence names no such period, names a later one, or the table has no value there.
    """
    if re.fullmatch(r"Q[1-4]", month or ""):
        kind, words = "triwulan", _QUARTER_WORD
    elif re.fullmatch(r"S[12]", month or ""):
        kind, words = "semester", _SEMESTER_WORD
    elif (_YEAR_ON_YEAR_ONLY.search(quote or "") and not _COMPARES_WITH_A_PERIOD.search(quote or "")
          and _growth_kind(table.unit, label) is None):   # a level or index table, not a growth one
        # SPE: "peningkatan penjualan secara tahunan pada Kelompok … Suku Cadang dan Aksesori" is
        # August against August a year before (yoy +3,9%), not against July (148,9 -> 141,5).
        _, val = table.lookup_fuzzy(label, year - 1, month)
        return ((year - 1, month), val) if val is not None else None
    else:
        return None
    named = [m for m in _COMPARED_PERIOD.finditer(quote or "")
             if m.group("kind").lower() == kind and m.group("q").lower() in words]
    if named:
        said = {"is_increasing": _UP_WORDS, "is_decreasing": _DOWN_WORDS}.get(operation)
        agreeing = [m for m in named
                    if said and m.group("dir") and re.fullmatch(said, m.group("dir"), re.IGNORECASE)]
        match = (agreeing or named)[0]
        key = (int(match.group("y")), words[match.group("q").lower()])
    elif kind == "triwulan" and _SAME_QUARTER_LAST_YEAR.search(quote or ""):
        key = (year - 1, month)
    else:
        return None
    if (key[0], _period_ordinal(key[1])) >= (year, _period_ordinal(month)):
        return None
    _, val = table.lookup_fuzzy(label, *key)
    return (key, val) if val is not None else None


_SEMESTER_IN_SENTENCE = re.compile(r"\bsemester\s+(?P<s>II|I)\s+(?P<y>\d{4})\b", re.IGNORECASE)


def _semester_periods(fact: ExtractedFact, sources: List[_ExcelSource]) -> Optional[ExtractedFact]:
    """The claim with its quarters read as semesters, when a source keeps semesters (SKDU Tabel
    7b-10) and the sentence speaks of semesters.

    "Margin usaha pada semester II 2026 …" came back as Q2 2026 against a table with only S1/S2
    columns: a point moves when the sentence names that very semester and year. "SB upah sebesar
    9,24% … dibandingkan … semester I 2026" came back as Q3 and Q4 2026, the two quarters of the
    second half: a metric given exactly the two quarters of one half becomes that semester, when the
    sentence speaks of semesters and never of a quarter ("triwulan").
    """
    quote = fact.context_quote or ""
    if not any(src.table.axis_type == "temporal"
               and any(len(key) == 3 and str(key[2]).startswith("S") for key in src.table._data)
               for src in sources):
        return None
    if not re.search(r"\bsemester", quote, re.IGNORECASE):
        return _semester_only_rows(fact, sources, quote)
    said = {(int(m.group("y")), _SEMESTER_WORD[m.group("s").lower()])
            for m in _SEMESTER_IN_SENTENCE.finditer(quote)}
    by_metric: Dict[str, List[PeriodPoint]] = {}
    for p in fact.periods:
        by_metric.setdefault(p.metric_label, []).append(p)
    moved, changed = [], False
    for metric, points in by_metric.items():
        months = tuple(sorted(p.month or "" for p in points))
        years = {p.year for p in points}
        half = {("Q1", "Q2"): "S1", ("Q3", "Q4"): "S2"}.get(months)
        if (half and len(points) == 2 and len(years) == 1 and all(p.col_label is None for p in points)
                and not re.search(r"\btriwulan\b", quote, re.IGNORECASE)):
            moved.append(PeriodPoint(metric, points[0].year, half))
            changed = True
            continue
        for p in points:
            semester = {"Q1": "S1", "Q2": "S2"}.get(p.month or "")
            if p.col_label is None and semester and (p.year, semester) in said:
                moved.append(PeriodPoint(p.metric_label, p.year, semester))
                changed = True
            else:
                moved.append(p)
    if not changed:
        return None
    out = copy.copy(fact)
    out.periods = moved
    return out


def _semester_only_rows(
    fact: ExtractedFact, sources: List[_ExcelSource], quote: str
) -> Optional[ExtractedFact]:
    """Q1/Q2 read as S1/S2 for a metric no source keeps per quarter but one keeps per semester.

    SKDU Tw III-2026: "upah pegawai level di bawah mandor/supervisor sebesar Rp3,87 juta per bulan"
    follows a sentence naming semester II 2026, and came back as Q2 2026; Tabel 9 has that row
    only for S1/S2 (S2 2026 = 3.865.177). The extractor writes semester II as Q2, and a row with no
    quarterly figures leaves nothing a quarter could mean. Not when the sentence says "triwulan".
    """
    if re.search(r"\btriwulan\b", quote, re.IGNORECASE):
        return None
    moved, changed = [], False
    for p in fact.periods:
        semester = {"Q1": "S1", "Q2": "S2"}.get(p.month or "")
        if p.col_label is None and semester and _only_per_semester(p.metric_label, p.year, p.month,
                                                                  semester, sources):
            moved.append(PeriodPoint(p.metric_label, p.year, semester))
            changed = True
        else:
            moved.append(p)
    if not changed:
        return None
    out = copy.copy(fact)
    out.periods = moved
    return out


def _only_per_semester(metric: str, year: int, quarter: str, semester: str,
                       sources: List[_ExcelSource]) -> bool:
    """True when some source has the metric for the semester and none has it for the quarter."""
    temporal = [src for src in sources if src.table.axis_type == "temporal"]
    if any(src.table.lookup_fuzzy(metric, year, quarter)[1] is not None for src in temporal):
        return False
    return any(src.table.lookup_fuzzy(metric, year, semester)[1] is not None for src in temporal)


# A number printed as a net balance: "SBT sebesar 0,07%", "(SBT 0,48%)", "SB -0,36%".
_BALANCE_NUMBER = re.compile(
    r"\b(?:SBT|SB|saldo\s+bersih(?:\s+tertimbang)?)\b[^\d\-.;,]{0,40}?(-?\d+(?:,\d+)?)\s*%"
    r"(?!\s*\(?\s*yoy)", re.IGNORECASE)


def _balance_read_as_growth(fact: ExtractedFact) -> Optional[ExtractedFact]:
    """The claim as a plain value, when its yoy growth is a number the sentence prints as a balance
    ("SBT sebesar 0,07%", "SBT triwulan sebelumnya sebesar 0,14%").

    SKDU Tw III-2026: "penggunaan tenaga kerja terindikasi tetap tumbuh, dengan SBT sebesar 0,07%"
    came back as a yoy growth of 0,07%, and the engine computed the balance's own growth (133%) and
    refuted it; "LU Pertambangan dan Penggalian (SBT 0,48%)" likewise. A balance is a level. Only
    when the claimed number itself follows "SBT"/"SB"/"saldo bersih" and is not marked "(yoy)".
    """
    if fact.operation != "yoy_growth" or fact.claimed_value is None:
        return None
    for match in _BALANCE_NUMBER.finditer(fact.context_quote or ""):
        if abs(float(match.group(1).replace(",", ".")) - fact.claimed_value) < 1e-9:
            out = copy.copy(fact)
            out.operation = "value"
            out.unit = "persen"
            out.periods = fact.periods[-1:]
            return out
    return None


# The narrowest "relatif stabil" on a net-balance (SBT) series, in percentage points. A balance runs
# from -100 to 100 and often sits near zero, where the relative band shrinks to nothing: SKDU
# Tw II-2026 called Industri Pengolahan's labour use "relatif stabil" at -0,47 -> -0,54 and its
# activity at 1,46 -> 1,34, and both were Tidak Sesuai on a band of 0,05. The reports' own
# "stabil" moves on these tables run 0,01-0,12 points.
_STABLE_NET_BALANCE_FLOOR = 0.25


def _stable_band(a: float, b: float, net_balance: bool = False) -> float:
    """Largest move between two consecutive points still readable as "relatif stabil".

    Proportional to the pair's own magnitude, floored at MATCH_TOLERANCE so a series hovering
    near zero (growth rates that cross sign) still admits two readings that are identical to
    the precision they were printed at, instead of getting a band of nearly zero — and at
    _STABLE_NET_BALANCE_FLOOR on a net-balance series.
    """
    floor = _STABLE_NET_BALANCE_FLOOR if net_balance else MATCH_TOLERANCE
    return max(floor, _STABLE_RELATIVE_BAND * (abs(a) + abs(b)) / 2)


def _printed_in(value: Optional[float], quote: Optional[str]) -> bool:
    """Whether the sentence prints this number ('8,2' / '8.2'), sign aside."""
    if value is None or not quote:
        return True
    text = f"{abs(value):.4f}".rstrip("0").rstrip(".")
    whole, _, frac = text.partition(".")
    pattern = re.escape(whole) + (r"[,.]" + re.escape(frac) if frac else "") + r"(?![\d])"
    return re.search(r"(?<![\d,.])" + pattern, quote) is not None


def _printed_signed(value: Optional[float], quote: Optional[str]) -> bool:
    """Whether the sentence prints this number with its sign: '-0,13' for -0.13, and '0,07' not
    preceded by a minus for 0.07. Rounded to the two decimals survey tables print."""
    if value is None or not quote:
        return False
    whole, _, frac = f"{abs(value):.2f}".partition(".")
    number = re.escape(whole) + r"[,.]" + re.escape(frac) + r"(?![\d])"
    minus = r"[-−–]\s?"
    if value < 0:
        return re.search(r"(?<![\d,.])" + minus + number, quote) is not None
    return re.search(r"(?<![\d,.\-−–])(?<![-−–]\s)" + number, quote) is not None


def _numeric_verdict(
    claimed: float, computed: float, tolerance: float = MATCH_TOLERANCE
) -> Tuple[float, str]:
    delta = round(abs(claimed - computed), 4)
    return delta, ("Entailed" if delta <= tolerance else "Refuted")


# A number as a report prints it: '10.415,9', '7,6', '9.388', '12' (or '10,415.9').
_PRINTED_NUMBER = re.compile(r"(?<![\d,.])\d+(?:[.,]\d+)*")


def _readings(token: str) -> List[Tuple[float, int]]:
    """Every (value, decimals) a printed number can mean: Indonesian '9.388' = 9388 with no
    decimals, English '9.388' = 9,388 with three."""
    out = []
    for thousands, point in ((".", ","), (",", ".")):
        whole, _, frac = token.partition(point)
        groups = whole.split(thousands)
        if (frac and not frac.isdigit()) or not all(g.isdigit() for g in groups) or (
                len(groups) > 1 and any(len(g) != 3 for g in groups[1:])):
            continue
        out.append((float("".join(groups) + ("." + frac if frac else "")), len(frac)))
    return out


def _printed_decimals(value: float, quote: Optional[str]) -> Optional[int]:
    """How many decimals the sentence prints `value` with ('10,0' -> 1, '9.388' -> 0).

    Read from the text because the float cannot tell '10,0' from '10'. None when the number is
    not in the sentence or is printed twice with different precision.
    """
    found = {decimals for m in _PRINTED_NUMBER.finditer(quote or "")
             for number, decimals in _readings(m.group(0)) if abs(number - abs(value)) < 1e-9}
    return found.pop() if len(found) == 1 else None


def _decimals_held(value: float) -> int:
    """Decimals a table value carries: 4.8 -> 1, 9387.8952 -> 4, 10.0 -> 0."""
    return max(0, -Decimal(repr(round(value, 6))).normalize().as_tuple().exponent)


def _value_verdict(claimed: float, table_value: float, quote: Optional[str]) -> Tuple[float, str, str]:
    """A number read straight off the table, judged by rounding it as the report printed it.

    "10,8" is Sesuai when the cell rounds to 10,8 (10,84) and not when it rounds to 10,9
    (10,86); "Rp9.388 triliun" is Sesuai against 9.387,9, which a flat 0,05 refuted; "5,12%"
    is not against 5,16, which 0,05 let through. Half-up on the decimal digits, as Excel shows
    them — Python's round() turns 2,675 into 2,67.

    Falls back to MATCH_TOLERANCE when the sentence does not show how the number was printed,
    or prints more decimals than the table holds (4,75 against a cell of 4,8): the table
    cannot settle the extra digit, so rounding would refute what it cannot see. Derived
    numbers (sums, differences, computed growth) never come here; they carry two roundings.
    Returns (delta, verdict, the reasoning tail).
    """
    delta, verdict = _numeric_verdict(claimed, round(table_value, 4))
    decimals = _printed_decimals(claimed, quote)
    if decimals is None or decimals > _decimals_held(table_value):
        return delta, verdict, (f"Δ = {delta} → {'within' if verdict == 'Entailed' else 'exceeds'} "
                                f"tolerance {MATCH_TOLERANCE}")
    step = Decimal(1).scaleb(-decimals)
    shown = Decimal(repr(round(table_value, 10))).quantize(step, rounding=ROUND_HALF_UP)
    same = shown == Decimal(repr(claimed)).quantize(step, rounding=ROUND_HALF_UP)
    shown_text = f"{shown:f}".replace(".", ",")
    return delta, ("Entailed" if same else "Refuted"), (
        f"dibulatkan ke {decimals} desimal seperti di laporan = {shown_text} → "
        f"{'sama dengan' if same else 'berbeda dari'} klaim")


def _printing_step(value: float) -> float:
    """The last decimal place `value` was printed to, e.g. 0.1 for 7,6 and 1.0 for 12."""
    exponent = Decimal(str(round(value, 6))).as_tuple().exponent
    return float(Decimal(1).scaleb(exponent)) if exponent < 0 else 1.0


def _growth_tolerance(current: float, prior: float, claimed: Optional[float] = None,
                      quote: Optional[str] = None) -> float:
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

    The claim's own rounding is half its last printed digit when the sentence shows it, never
    more than MATCH_TOLERANCE: SHPR Tw IV-2025 "Palembang … 1,06% (yoy)" against 108,56 / 107,40
    = 1,08% passed on 0,05 + 0,009, though a figure printed to two decimals allows 0,005.
    """
    decimals = _printed_decimals(claimed, quote) if claimed is not None else None
    own = MATCH_TOLERANCE if decimals is None else min(MATCH_TOLERANCE, 0.5 * 10 ** -decimals)
    if not prior:
        return own
    half = _printing_step(prior) / 2
    band = 100 * (half / abs(prior)) * (1 + abs(current / prior))
    return round(own + band, 4)


class _Why(str):
    """A Tidak Cukup Data explanation that also carries its schemas.ReasonCode.

    The text keeps flowing through the engine as a plain string (best_reason, f-strings), and
    _make_result reads the code off it, so each site states its reason once, next to the words.
    """

    code: Optional[str] = None


def _why(code: str, text: str) -> _Why:
    why = _Why(text)
    why.code = code
    return why


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
    reason_code: Optional[str] = None,
) -> FactVerificationResult:
    if verdict == "Inconclusive":
        reason_code = reason_code or getattr(reasoning, "code", None)
    else:
        reason_code = None
    return FactVerificationResult(
        reason_code=reason_code,
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


def _in_the_claims_band(fact: ExtractedFact, src: "_ExcelSource"):
    """The claim resolved inside the band of `src` that holds the growth kind the claim states, or
    None. SHPR Tabel 3 repeats every city under 'TRIWULANAN (QTQ)' and 'TAHUNAN (YOY)'; "Padang …
    terkontraksi sebesar 0,26% (qtq)" found the yoy band's Padang (0,63), which was then refused as
    the wrong kind, and the claim — off by 0,01 from the qtq band's -0,25 — went unchecked."""
    kind = _claimed_rate_kind(fact)
    # Only a table whose unit itself says its cells are several growth kinds side by side ('%,
    # qtq & yoy'); NPI's 'Id' has a yoy growth section under a 'Juta USD' unit, and its rows there
    # are not the bands of one series.
    if not kind or len({k.lower() for k in _GROWTH_KIND_RE.findall(src.table.unit or "")}) < 2:
        return None
    band = next((level for level in dict.fromkeys(row.split(QUAL_SEP)[0] for row in src.table.row_labels)
                 if kind in {k.lower() for k in _GROWTH_KIND_RE.findall(level)}), None)
    if band is None:
        return None
    points = [p if (p.metric_label or "").upper().startswith(band.upper())
              else PeriodPoint(band + QUAL_SEP + (p.metric_label or ""), p.year, p.month, p.col_label)
              for p in fact.periods]
    resolved, missing, col_units = _try_resolve(points, src)
    if missing or not all(label.startswith(band) for label, _ in resolved):
        return None
    return resolved, missing, col_units


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


# Words that say which way a flow went, not which breakdown it is: "aliran dana asing ke pasar
# saham domestik penjualan neto" names the equity-liability row; 'penjualan neto' is its sign.
_FLOW_DIRECTION_WORDS = frozenset({
    "penjualan", "pembelian", "neto", "bersih", "arus", "masuk", "keluar", "net", "inflow",
    "outflow",
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
        absent = src.table.words_absent(point.metric_label) - unit_words - _FLOW_DIRECTION_WORDS
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
_SPLIT_DIGITS = re.compile(r"(?<=\d) (?=\d)|(?<=\d,) (?=\d)")


def _is_a_net_figure(fact: ExtractedFact) -> bool:
    """True when the sentence presents the claimed number as a net position.

    The number counts as net when, within its sentence, the nearest earlier mention of a side
    is 'kewajiban/aset neto' ("kewajiban neto sebesar 229,6 …, lebih rendah dibandingkan dengan
    237,7"), not a gross one ("… dari penurunan posisi KFLN … menjadi 389,7").
    """
    if fact.claimed_value is None:
        return False
    # A PDF text layer splits numbers ("kewajiban neto sebesar 1 49,1", "turun 19, 8").
    quote = _SPLIT_DIGITS.sub("", fact.context_quote or "")
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


def _net_row_for(label: str, table: BITableData) -> Optional[str]:
    """The net row (TableData.net_rows) a gross row stands for: the component under an 'Aset' /
    'Kewajiban' side, or a row the parser maps directly (PII's 'Di Indonesia (PMA)', one side of
    the directional memorandum, whose net row is 'Investasi langsung berdasarkan arah investasi')."""
    if not table.net_rows:
        return None
    side, _, component = label.partition(QUAL_SEP)
    if component and side.strip().lower() in _BALANCE_SIDES:
        net = table.net_rows.get(component.strip().lower())
        if net:
            return net
    return table.net_rows.get(label.strip().lower())


def _net_row_instead(
    fact: ExtractedFact, resolved: List[Tuple[str, float]], src: _ExcelSource
) -> Optional[List[Tuple[str, float]]]:
    """The component's net row, when a net number landed on one gross side of it.

    PII Tw II-2026: "investasi portofolio mencatat kewajiban neto sebesar 229,6" resolved to
    'Aset > Investasi Portofolio' (43,5); the figure is Aset − Kewajiban (−229,62), which the
    parser derives (TableData.net_rows). Only for a number the sentence presents as net
    (_is_a_net_figure) and a row that is one side of a component the table has a net row for;
    anything else keeps its row and meets _gross_row_for_a_net_number as before.
    """
    if not src.table.net_rows or not _is_a_net_figure(fact):
        return None
    out = []
    for point, (label, _value) in zip(fact.periods, resolved):
        net = _net_row_for(label, src.table)
        if net is None:
            return None
        value = src.table._data.get((net, point.year, point.month))
        if value is None:
            return None
        out.append((net, value))
    return out


def _all_net_rows(fact: ExtractedFact, sources: List[_ExcelSource]) -> bool:
    """Every row of the claim is a net row (TableData.net_rows) — put there by
    _net_position_threshold, which requires the sentence to name each one."""
    nets = {row for src in sources for row in src.table.net_rows.values()}
    return bool(nets) and all(p.metric_label in nets for p in fact.periods)


def _net_position_threshold(
    fact: ExtractedFact, sources: List[_ExcelSource]
) -> Optional[ExtractedFact]:
    """A sign claim on a net position, asked of the component's net row, on the sentence's side.

    PII Tw II-2026: "posisi investasi portofolio mencatat kewajiban neto sebesar 229,6" also came
    back as 'Investasi Portofolio' below 0. Read as a group claim it was judged on the sentence's
    'kewajiban' — 'Kewajiban > Investasi Portofolio', 273,1 > 0 — and reported Tidak Sesuai. What
    is below 0 is Aset − Kewajiban (TableData.net_rows). And "sebagian besar komponen … mencatat
    kewajiban neto, kecuali investasi lainnya dan cadangan devisa mencatat aset neto" came back as
    those two below 0, a majority claim: the net phrase after the claim's own names ("aset neto")
    says which side, and the named rows are judged on it, not as a majority.

    Only a threshold at 0 whose sentence states a net side before naming any gross one
    (KFLN/AFLN/bruto), and only a claim whose every row has a net row; the labels become those
    rows' exact keys and any group_scope is dropped (the sentence names the rows itself).
    """
    if fact.operation not in _THRESHOLD_OPS or (fact.claimed_value or 0) != 0:
        return None
    quote = fact.context_quote or ""
    net = _NET_POSITION.search(quote)
    if net is None:
        return None
    sentence_start = max((m.end() for m in re.finditer(r"[.;]\s", quote[:net.start()])), default=0)
    if _GROSS_SIDE.search(quote[sentence_start:net.end()]):
        return None
    lowered = quote.lower()
    named_at = [lowered.find(p.metric_label.split(QUAL_SEP)[-1].strip().lower()) for p in fact.periods]
    if not all(at >= 0 for at in named_at):
        return None  # a row the extractor picked for an unnamed "sebagian besar komponen"
    after = _NET_POSITION.search(quote, max(named_at))
    if after is not None and not re.search(r"[.;]\s", quote[max(named_at):after.start()]):
        net = after
    operation = "above_threshold" if net.group(1).lower() == "aset" else "below_threshold"
    for src in sources:
        if not src.table.net_rows:
            continue
        labels = []
        for point in fact.periods:
            label, _value = src.table.lookup_fuzzy(point.metric_label, point.year, point.month)
            row = _net_row_for(label or "", src.table)
            if row is None or src.table._data.get((row, point.year, point.month)) is None:
                break
            labels.append(row)
        else:
            moved = copy.copy(fact)
            moved.periods = [PeriodPoint(row, p.year, p.month) for p, row in zip(fact.periods, labels)]
            moved.operation = operation
            moved.group_scope = None
            return moved
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
    fact: ExtractedFact, resolved: List[Tuple[str, float]], table: Optional[BITableData] = None
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
        if table is not None and table.term_aliases:
            # A publication's fixed alias names the row it means ("investasi langsung asing" ->
            # 'Investasi Langsung > Kewajiban'); its levels are named by the claim, not groups.
            targets = [row for pattern, row in table._alias_patterns()
                       if pattern.search(point.metric_label or "")]
            if any(label == t or label.endswith(QUAL_SEP + t) for t in targets):
                continue
            claim_words |= _sig_words(table._normalise_query(point.metric_label or ""))
        if not claim_words:
            continue
        # Any level the claim does not name is a group of it — at the end ('IEKLK > SMA') or at
        # the front, where the per-city table files each index under its city ('Medan > IKK').
        # An aggregate level ('Total > …') names no group and is let through.
        layout = getattr(table, "layout_levels", None) or frozenset()
        for level in label.split(QUAL_SEP):
            # A balance ('Saldo Bersih (% Baik - % Buruk)') measures the whole group above it: it
            # is not one group of it (SKDU's "likuiditas membaik", see _balance_row_instead).
            if level.strip().lower() in layout or level.strip().lower().startswith("saldo bersih"):
                continue
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
    point = fact.periods[0]
    matched_label = resolved[0][0]
    if _is_growth_series(src.table.unit) or "yoy" in matched_label.lower():
        return True
    _prior_label, prior = src.table.lookup_fuzzy(matched_label, point.year - 1, point.month)
    return prior is not None or _published_yoy(fact, src) is not None


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
            "persen_yoy", None, "Inconclusive", reason_code="jenis",
            reasoning=(
                f"Excel [{matched_source}] ({matched_label}) berisi pertumbuhan {other_kind} "
                f"('{src.table.unit}'), bukan yoy — tidak menjawab klaim {fact.claimed_value}% yoy."
            ),
        )

    if _is_growth_series(src.table.unit) or "yoy" in matched_label.lower():
        # The cell already holds the answer, so read it instead of computing one (the same
        # division of labour every other lookup follows). No prior-year column is needed,
        # which also stops such a table from being ruled out for lacking one. A level table
        # may carry its own yoy rows too (NPI: 'Pertumbuhan (%, yoy) > Total Ekspor (fob)').
        computed = round(curr_raw, 4)
        delta, verdict, rule = _value_verdict(fact.claimed_value, curr_raw, fact.context_quote)
        return _make_result(
            fact, [current_period], matched_source,
            fact.claimed_value, "persen_yoy", computed, "persen_yoy", delta, verdict,
            reasoning=(
                f"PDF: {fact.claimed_value}% yoy | "
                f"Excel [{matched_source}] ({matched_label}): {computed}% yoy "
                f"(dibaca langsung; tabel ini sudah dalam satuan '{src.table.unit}') | {rule}"
            ),
        )

    prior_label, prior_raw = src.table.lookup_fuzzy(matched_label, prior_year, p.month)

    if prior_raw is None:
        published = _published_yoy_result(fact, src, f"'{matched_label}' tidak punya angka {prior_year}")
        if published is not None:
            return published
        return _make_result(
            fact, [current_period], matched_source, fact.claimed_value, "persen_yoy", None, "persen_yoy", None,
            "Inconclusive", reason_code="periode",
            reasoning=(
                f"No data found in Excel [{matched_source}] for metric '{matched_label}' "
                f"at {p.month} {prior_year} (needed for yoy denominator)."
            ),
        )
    if prior_raw == 0:
        return _make_result(
            fact, [current_period], matched_source, fact.claimed_value, "persen_yoy", None, "persen_yoy", None,
            "Inconclusive", reason_code="jenis",
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
            "Inconclusive", reason_code="jenis",
            reasoning=(
                f"Nilai '{matched_label}' pada {p.month} {prior_year} di [{matched_source}] "
                f"({prior_raw}) sama persis dengan angka pertumbuhan yang diklaim — hampir pasti "
                f"sel '%, yoy', bukan level tahun lalu. Pembanding tahun lalu tidak tersedia."
            ),
        )

    # A row stored negative — a debit such as imports in SEKI V.1, or a deficit — grows when its
    # SIZE grows: imports -48.487 -> -58.323 are up 20,3%, which the signed formula read as -20,3%.
    size_note = ""
    if curr_raw < 0 and prior_raw < 0:
        computed = round((abs(curr_raw) - abs(prior_raw)) / abs(prior_raw) * 100, 4)
        size_note = ", dihitung dari besarnya karena baris ini bernilai negatif"
    else:
        computed = round((curr_raw - prior_raw) / abs(prior_raw) * 100, 4)
    tolerance = _growth_tolerance(curr_raw, prior_raw, fact.claimed_value, fact.context_quote)
    delta, verdict = _numeric_verdict(fact.claimed_value, computed, tolerance)
    periods = [current_period, PeriodResult(metric_label=prior_label, year=prior_year, month=p.month, excel_value=round(prior_raw, 4))]
    if verdict == "Refuted":
        published = _published_yoy_result(fact, src, f"dihitung dari '{matched_label}': {computed}%")
        if published is not None and published.verdict == "Entailed":
            return published
    return _make_result(
        fact, periods, matched_source, fact.claimed_value, "persen_yoy", computed, "persen_yoy", delta, verdict,
        reasoning=(
            f"PDF: {fact.claimed_value}% yoy | "
            f"Excel [{matched_source}] ({matched_label}): {computed}% yoy "
            f"({curr_raw:.2f} vs {prior_raw:.2f} {src.table.unit}{size_note}) | "
            f"Δ = {delta}% → {'within' if verdict == 'Entailed' else 'exceeds'} tolerance {tolerance}%"
        ),
    )


def _published_yoy(fact: ExtractedFact, src: _ExcelSource) -> Optional[Tuple[str, float]]:
    """The table's own '%, yoy' row for the claim's metric at its period, if it prints one.

    NPI's memorandum prints 'Pertumbuhan (%, yoy) > Total Ekspor (fob) > Nonmigas' (8,49) next
    to the BPM6 rows; "ekspor nonmigas tumbuh 8,5% (yoy)" is that row, while growth computed
    from the general-merchandise row gives 9,81. Every word of the claim's metric must be in the
    row's name (the yoy part aside), and the closest such row wins.
    """
    point = fact.periods[0]
    wanted = _sig_words(point.metric_label) - {"yoy", "pertumbuhan"}
    if not wanted or point.year is None or point.month is None:
        return None
    best: Optional[Tuple[int, str, float]] = None
    for row in src.table.row_labels:
        parts = [part for part in row.split(QUAL_SEP.strip()) if "yoy" not in part.lower()]
        if len(parts) == len(row.split(QUAL_SEP.strip())):
            continue  # not a yoy row
        words = _sig_words(" ".join(parts))
        if not wanted <= words:
            continue
        value = src.table.lookup(row, point.year, point.month)
        if value is None:
            continue
        extra = len(words - wanted)
        if best is None or extra < best[0]:
            best = (extra, row, value)
    return (best[1], best[2]) if best else None


def _published_yoy_result(
    fact: ExtractedFact, src: _ExcelSource, note: str
) -> Optional[FactVerificationResult]:
    """The yoy claim judged on the table's published growth row (see _published_yoy)."""
    published = _published_yoy(fact, src)
    if published is None:
        return None
    row, value = published
    p = fact.periods[0]
    delta, verdict, rule = _value_verdict(fact.claimed_value, value, fact.context_quote)
    value = round(value, 4)
    return _make_result(
        fact, [PeriodResult(metric_label=row, year=p.year, month=p.month, excel_value=value)],
        src.label, fact.claimed_value, "persen_yoy", value, "persen_yoy", delta, verdict,
        reasoning=(
            f"PDF: {fact.claimed_value}% yoy | Excel [{src.label}] ({row}): {value}% yoy, dibaca "
            f"langsung dari baris pertumbuhan yang diterbitkan tabel ({note}) | {rule}"
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
            fact, periods, matched_source, fact.claimed_value, fact.unit, None, fact.unit, None, "Inconclusive", reason_code="jenis",
            reasoning=f"Nilai penyebut '{label_b}' bernilai nol; rasio tidak terdefinisi.",
        )
    if _GDP_SHARE_SUFFIX.search(label_b) and not _gdp_share_row(label_a, label_b):
        # SEKI V.1 prints a (% PDB) row only for the current account; dividing the capital
        # account by it gave -881%. Another account's share of GDP is no denominator.
        return _make_result(
            fact, periods, matched_source, fact.claimed_value, fact.unit, None, fact.unit, None, "Inconclusive", reason_code="di_luar_tabel",
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
# The same verbs further on in the sentence, past the figures the report prints in between:
# "defisit transaksi berjalan … sebesar 12,5 miliar dolar AS (3,3% dari PDB), melebar
# dibandingkan …". Only the diff check uses it, and only on a balance negative throughout.
_DEFICIT_TREND_LATER_IN_SENTENCE = re.compile(
    r"\b" + _DEFICIT_NOUNS + r"\b(?:[^.;]|(?<=\d)\.(?=\d)){0,160}?"
    r"\b(?P<verb>meningkat|naik|melebar|membesar|bertambah|menurun|turun|menyempit|mengecil|berkurang"
    r"|lebih\s+(tinggi|rendah|besar|kecil|lebar|sempit))\b",
    re.IGNORECASE,
)
# The same, noun first: "peningkatan defisit neraca jasa", "penyempitan defisit TB", and the
# verb made a noun with -nya: PII Tw IV-2025 "meningkatnya kewajiban neto investasi langsung
# sebesar 21,5" (−182,4 → −203,9).
_DEFICIT_TREND_NOUN_FIRST = re.compile(
    r"\b(?P<verb>peningkatan|kenaikan|pelebaran|penambahan|penurunan|penyempitan|pengurangan"
    r"|(?:meningkat|naik|melebar|membesar|bertambah|menurun|turun|menyempit|mengecil|berkurang)nya)\s+"
    + _DEFICIT_NOUNS + r"\b",
    re.IGNORECASE,
)
# A claim whose metric name starts with the deficit: "defisit neraca perdagangan migas", or with
# a net outflow: "net outflow kewajiban investasi lainnya".
_DEFICIT_CLAIM_NAME = re.compile(
    r"\s*(defisit|net\s+outflow|arus\s+keluar\s+neto|aliran\s+keluar\s+neto)\b", re.IGNORECASE)

# Words that say which way a value moved. "Ekspansi"/"kontraksi" are not among them: they name a
# level (PMI index above/below 50), and "perbaikan meskipun masih berada pada fase kontraksi" is
# exactly where the extractor took the level for the direction.
_SAID_UP = re.compile(
    r"\b(lebih\s+tinggi|meningkat|peningkatan|naik|kenaikan|membaik|perbaikan|terakselerasi|"
    # "penurunan penjualan Kelompok Barang Lainnya di tengah pertumbuhan penjualan Kelompok
    # Peralatan …" (SPE Juli 2026) words both ways; read as "turun" alone, the growing groups
    # were refuted. Not "tumbuh": SBank's "tetap tumbuh" is a level (TableData.level_phrases).
    r"akselerasi|menguat|penguatan|pertumbuhan)\b", re.IGNORECASE)
_SAID_DOWN = re.compile(
    r"\b(lebih\s+rendah|menurun|penurunan|turun|melambat|perlambatan|memburuk|melemah|pelemahan)\b",
    re.IGNORECASE)
_NEGATED = re.compile(r"\b(tidak|belum|bukan|tanpa)\s+(\w+\s+)?$", re.IGNORECASE)


def _direction_said_for(fact: ExtractedFact, quote: str) -> Optional[str]:
    """'is_increasing' / 'is_decreasing' when the WHOLE sentence words one direction only and
    names the claim's metric; None when any word points the other way or is negated.

    The whole sentence, not the part after the metric: SK's "IKK mengalami peningkatan tertinggi
    di Pontianak, Medan, dan Padang, sementara penurunan IKK terbesar …" puts the direction BEFORE
    the city, so reading from 'Pontianak' on found only "penurunan" and refuted 11 true claims.
    """
    words = [w for w in _sig_words(fact.display_label or "") if len(w) > 3]
    if not any(w in quote.lower() for w in words):
        return None
    found = set()
    for pattern, direction in ((_SAID_UP, "is_increasing"), (_SAID_DOWN, "is_decreasing")):
        for match in pattern.finditer(quote):
            if _NEGATED.search(quote[:match.start()]):
                return None
            found.add(direction)
    return found.pop() if len(found) == 1 else None


def _publication_level_claim(quote: str, table: BITableData) -> Optional[Tuple[str, float]]:
    """("above" | "below", threshold) when the sentence states a level its publication defines
    (TableData.level_phrases) and names no direction at all — "diprakirakan tetap berada pada fase
    ekspansi" is PMI > 50, not a rise."""
    phrases = getattr(table, "level_phrases", None) or {}
    if _SAID_UP.search(quote) or _SAID_DOWN.search(quote):
        return None
    for pattern, level in phrases.items():
        if re.search(pattern, quote, re.IGNORECASE):
            return level
    return None
# The words among them that say the size went DOWN; every other one says it went up.
_SIZE_DOWN = re.compile(
    r"menurun|turun|menyempit|mengecil|berkurang|lebih\s+(rendah|kecil|sempit)"
    r"|penurunan|penyempitan|pengurangan",
    re.IGNORECASE,
)


# A net-balance survey table (SKDU, Survei Perbankan): a positive SBT/SB means the activity
# grew, so the report's words describe the LEVEL, not a move against the previous quarter.
_NET_BALANCE_UNIT = re.compile(r"saldo\s+bersih|\bSBT\b|\bSB\b|net\s+balance", re.IGNORECASE)
# "tetap tumbuh", "seluruh sub-LU tercatat tumbuh": the balance is positive.
_GROWTH_LEVEL_WORDS = re.compile(r"\b(tetap\s+tumbuh|tercatat\s+tumbuh|tumbuh\s+positif)\b",
                                 re.IGNORECASE)
# "kinerja ... terjaga", "tetap kuatnya investasi", "likuiditas masih dalam kondisi baik", "akses
# kredit tetap mudah": still positive, judged as is_stable. Not "lebih baik" — that is the
# answer a share of respondents gave, a row of its own.
_HELD_LEVEL_WORDS = re.compile(
    r"\b(terjaga\w*|tetap\s+kuat\w*|(?:tetap|masih)(?:\s+dalam\s+kondisi)?\s+(?:baik|mudah)"
    r"|dalam\s+kondisi\s+baik)\b",
    re.IGNORECASE,
)
_MOVE_WORDS = re.compile(
    r"\b(meningkat|naik|peningkatan|kenaikan|menurun|turun|penurunan|lebih\s+(tinggi|rendah))\b",
    re.IGNORECASE,
)
# "bersumber dari LU Pertanian (SBT 3,50%)": the LU is a SOURCE of the total's rise.
_CONTRIBUTION_WORDS = re.compile(r"\b(bersumber\s+dari|ditopang\s+oleh|didorong\s+oleh)\b",
                                 re.IGNORECASE)
_CITED_BALANCE = re.compile(r"\((?:SBT|SB)\s*-?\d[\d.,]*\s*%?\)", re.IGNORECASE)


def _words(text: str) -> set:
    return {w for w in re.findall(r"[a-z]+", text.lower()) if len(w) > 2 and w != "dan"}


def _cited_as_contributor(quote: str, row: str) -> bool:
    """True when `row` is one of the "X (SBT a%)" items after a contribution verb."""
    verb = _CONTRIBUTION_WORDS.search(quote)
    if not verb:
        return False
    leaf = _words(row.rsplit(QUAL_SEP, 1)[-1])
    if not leaf:
        return False
    start = verb.end()
    for cite in _CITED_BALANCE.finditer(quote, verb.end()):
        item = _words(quote[start:cite.start()])
        start = cite.end()
        if len(leaf & item) / len(leaf) >= 0.6:
            return True
    return False


def _net_balance_level_claim(fact: ExtractedFact, row: str, unit: str) -> Optional[str]:
    """Why a trend claim on a net-balance row is really about its level being positive, or None.

    SKDU Tw II-2026: "kegiatan usaha LU Perdagangan ... tetap tumbuh" (SBT 1,24 -> 1,04),
    "kinerja usaha LU Industri Pengolahan ... terjaga" (1,46 -> 1,34) and "peningkatan harga jual
    ... bersumber dari LU Pertanian (SBT 3,50%)" (4,07 -> 3,50) were all checked as a rise or a
    flat line and refuted, though each says the balance is positive — which it is.
    """
    # The balance can be one row of a '% Responden' table: SKDU Tabel 3 'Likuiditas > Saldo
    # Bersih (% Baik - % Buruk)' (17,05 -> 15,03, "masih dalam kondisi baik").
    if fact.operation not in ("is_increasing", "is_stable") or not (
            _NET_BALANCE_UNIT.search(unit or "") or _NET_BALANCE_UNIT.search(row)):
        return None
    quote = fact.context_quote or ""
    if _cited_as_contributor(quote, row):
        return "disebut sebagai sumber/penopang dengan nilai SBT-nya"
    # "Kegiatan usaha diprakirakan meningkat bersumber dari LU A (SBT a%), LU B (SBT b%)": the
    # sentence breaks the growth into its sources, so the total's 'meningkat' is growth — a
    # positive balance (11,75) — not a rise over the quarter before (12,97).
    verb = _CONTRIBUTION_WORDS.search(quote)
    if (fact.operation == "is_increasing" and BITableData._is_aggregate(row) and verb
            and _CITED_BALANCE.search(quote, verb.end())):
        return "pertumbuhan yang dirinci menurut sumbernya"
    if fact.operation == "is_stable" and _HELD_LEVEL_WORDS.search(quote):
        return f"'{_HELD_LEVEL_WORDS.search(quote).group(0)}' pada saldo bersih"
    if fact.operation in ("is_increasing", "is_stable"):
        # Per clause: "tetap tumbuh dan diprakirakan meningkat pada triwulan III" holds a level
        # word for the report's quarter and a real rise for the next one. The extractor files
        # "tetap tumbuh" as a rise in one run and as a flat line in the next (SKDU 2026-10-16:
        # 0,28 -> 0,14 and 1,74 -> 0,79 refuted as is_stable); either way it means positive.
        latest = fact.periods[-1].month if fact.periods else None
        for clause in _CLAUSE_SPLIT.split(quote):
            growth = _GROWTH_LEVEL_WORDS.search(clause)
            if not growth or _MOVE_WORDS.search(clause):
                continue
            said = {_QUARTER_WORD[q.lower()] for q in _CLAUSE_QUARTER.findall(clause)}
            if not said or latest in said:
                return f"'{growth.group(0)}' pada saldo bersih"
        # A sentence that compares with nothing can only be about the level. Listing the ways to
        # say "grew" never ends — SBank Tw I-2026's "didukung oleh perkiraan pertumbuhan seluruh
        # kategori DPK yaitu … Deposito (72,70%)" (73,81 -> 72,70) was refuted as a rise — while
        # the words that compare are a short, closed list.
        if not _COMPARES.search(quote) and not _SAID_NEGATIVE.search(quote):
            said = {_QUARTER_WORD[q.lower()] for q in _CLAUSE_QUARTER.findall(quote)}
            if not said or latest in said:
                return "kalimat tidak membandingkan dengan periode lain"
    return None


# Words that set a value against another period or another value. Any of them in the sentence
# keeps a trend claim on a net balance a trend.
_COMPARES = re.compile(
    r"\b(dibandingkan|dibanding|sebelumnya|lalu|menjadi|stabil|tidak\s+berubah|sama"
    r"|lebih\s+(tinggi|rendah|besar|kecil|baik|buruk)|meningkat|peningkatan|naik|kenaikan|menurun"
    r"|penurunan|turun|membaik|perbaikan|memburuk|melambat|perlambatan|terakselerasi|akselerasi"
    r"|menguat|penguatan|melemah|pelemahan|tertinggi|terendah)\b",
    re.IGNORECASE)
# A level said to be below zero: not read as a positive balance.
_SAID_NEGATIVE = re.compile(r"\b(negatif|minus|kontraksi|terkontraksi|berkurang)\b", re.IGNORECASE)


_CLAUSE_SPLIT = re.compile(r"[,;]|\b(?:dan|serta|namun|sementara|meski(?:pun)?)\b", re.IGNORECASE)
_CLAUSE_QUARTER = re.compile(r"\btriwulan\s+(IV|III|II|I)\b", re.IGNORECASE)
_QUARTER_WORD = {"i": "Q1", "ii": "Q2", "iii": "Q3", "iv": "Q4"}


def _compute_trend(fact: ExtractedFact, resolved: List[Tuple[str, float]], src: _ExcelSource) -> FactVerificationResult:
    matched_source = src.label
    used_periods = list(fact.periods)
    labelled = list(resolved)  # [(matched_label, value)]

    # Auto-complete a single-point trend: reports often state only the current period
    # ("SBT meningkat" in a Tw II 2026 report) and leave the QoQ baseline implicit. Pull
    # the metric's immediately-preceding period from the table itself so the claim is
    # checkable without relying on the LLM to guess the earlier period.
    baseline_guessed = False
    if len(labelled) == 1:
        p0 = used_periods[0]
        if p0.col_label is None and p0.year is not None and p0.month is not None:
            prev = _stated_baseline(fact.context_quote or "", src.table, labelled[0][0],
                                    p0.year, p0.month, fact.operation)
            if prev is None:
                prev = _previous_period(src.table, labelled[0][0], p0.year, p0.month)
                baseline_guessed = prev is not None
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
            fact, periods, matched_source, None, None, None, src.table.unit, None, "Inconclusive", reason_code="periode",
            reasoning=(
                f"Klaim tren '{fact.operation}' hanya menyebut satu periode dan tabel tidak "
                f"punya periode sebelumnya untuk '{labelled[0][0]}' sebagai pembanding."
            ),
        )

    # "Dari sisi volume, impor minyak mentah menurun dari 36 juta barel …" says nothing about the
    # dollar row the label found; its direction there is a coincidence either way.
    if _VOLUME_TREND.search(fact.context_quote or "") and _MONEY_UNIT.search(src.table.unit or "") \
            and not _VOLUME_TREND.search(labelled[-1][0]):
        return _make_result(
            fact, periods, matched_source, None, None, None, src.table.unit, None, "Inconclusive", reason_code="jenis",
            reasoning=(
                f"Kalimat menyebut tren volume, sedangkan '{labelled[-1][0]}' di "
                f"[{matched_source}] bernilai uang ('{src.table.unit}') — tidak menjawab klaim ini."
            ),
        )

    stated = _stated_endpoints_missed(fact.context_quote or "", values, src.table.unit)
    if stated:
        return _make_result(
            fact, periods, matched_source, None, None, None, src.table.unit, None, "Inconclusive", reason_code="tidak_ditemukan",
            reasoning=(
                f"Kalimat menyebut {stated}, sedangkan '{labelled[-1][0]}' di [{matched_source}] "
                f"bernilai {round(abs(values[0]), 4)} → {round(abs(values[-1]), 4)} "
                f"{src.table.unit} — baris ini bukan metrik yang dimaksud."
            ),
        )

    # "defisit … meningkat" on a balance stored negative (−5,1 → −8,2) means the deficit grew, which
    # the signed numbers read as a fall. Judge such a claim on the size of the balance.
    size_note = ""
    operation = fact.operation
    quote = fact.context_quote or ""
    deficit = (
        _DEFICIT_TREND.search(quote) or _DEFICIT_TREND_NOUN_FIRST.search(quote)
        or _DEFICIT_TREND_LATER_IN_SENTENCE.search(quote)
    )
    # The claim itself named after the deficit — "defisit neraca perdagangan migas" — is about the
    # deficit's size even when its sentence puts no verb after it (NPI Tw II-2026: "… disertai
    # dengan defisit neraca perdagangan migas." came back is_increasing on −5.087 → −10.180, and
    # the signed reading refuted a deficit that had doubled).
    named_deficit = _DEFICIT_CLAIM_NAME.match(fact.display_label or "")
    # A balance that turned from surplus/inflow to deficit/outflow grew as a deficit: NPI Tw I-2026
    # "net outflow pada sisi kewajiban setelah … net inflow" is +1.896 -> -1.054. Only for a claim
    # named after the deficit, whose subject is unambiguous.
    turned = bool(named_deficit) and len(values) > 1 and values[0] >= 0 > values[-1]
    if (all(v < 0 for v in values) and (deficit or named_deficit)) or turned:
        values = [-v for v in values]
        size_note = " (dinilai pada besarnya defisit/kewajiban neto)"
        # The extractor reads "defisit … melebar" as the balance falling in one run and the
        # deficit growing in the next; the verb itself says which way the size moved. With no
        # verb, the extractor's direction stands: it named the deficit, so it meant the deficit.
        if deficit and operation in ("is_increasing", "is_decreasing"):
            grew = _SIZE_DOWN.search(deficit.group("verb")) is None
            operation = "is_increasing" if grew else "is_decreasing"
    elif all(v < 0 for v in values) and _is_debit_row(labelled[-1][0]):
        # "impor gas … meningkat" on -682 -> -1.529: imports grew.
        values = [-v for v in values]
        size_note = " (dinilai pada besarnya; baris debit dicatat negatif)"

    direction_note = ""
    if not size_note and operation in ("is_increasing", "is_decreasing"):
        said = _direction_said_for(fact, quote)
        if said and said != operation:
            # PMI Tw III-2026: "Kecepatan Penerimaan Barang Input … menunjukkan perbaikan meskipun
            # masih berada pada fase kontraksi …, lebih tinggi dibandingkan 47,46%" came back
            # is_decreasing — the extractor read "kontraksi" as the direction. The sentence is the
            # claim: its own direction is judged. A wrong direction BY THE WRITER still refutes,
            # because then the sentence and the data disagree.
            operation = said
            direction_note = " (arah dibaca dari kalimat: " + (
                "naik" if said == "is_increasing" else "turun") + ")"

    band_note = ""
    publication_level = None if size_note or direction_note else _publication_level_claim(
        quote, src.table
    )
    level_reason = None if size_note or publication_level else _net_balance_level_claim(
        fact, labelled[-1][0], src.table.unit
    )
    if publication_level:
        side, threshold = publication_level
        ok = values[-1] > threshold if side == "above" else values[-1] < threshold
        band_note = f" (dinilai sebagai level {'>' if side == 'above' else '<'} {threshold:g}, bukan perubahan)"
    elif level_reason:
        # The level at the claim's own (latest) period must be positive (= tumbuh / ekspansi).
        ok = values[-1] > 0
        band_note = f" (dinilai sebagai SBT positif: {level_reason})"
    elif operation == "is_increasing":
        ok = all(values[i + 1] >= values[i] for i in range(len(values) - 1))
    elif operation == "is_decreasing":
        ok = all(values[i + 1] <= values[i] for i in range(len(values) - 1))
    else:  # is_stable — see _stable_band
        net_balance = bool(_NET_BALANCE_UNIT.search(src.table.unit or "")
                           or _NET_BALANCE_UNIT.search(labelled[-1][0]))
        bands = [_stable_band(values[i], values[i + 1], net_balance)
                 for i in range(len(values) - 1)]
        ok = all(
            abs(values[i + 1] - values[i]) <= bands[i] for i in range(len(values) - 1)
        )
        band_note = f" (ambang stabil ±{round(max(bands), 4)})"

    band_note += direction_note
    if not ok and operation == "is_stable" and not level_reason and not publication_level \
            and _says_held_not_unchanged(quote):
        return _make_result(
            fact, periods, matched_source, None, None, None, src.table.unit, None, "Inconclusive", reason_code="klaim_tidak_lengkap",
            reasoning=(
                f"Kalimat menyebut '{fact.display_label}' terjaga, yaitu masih pada tingkat yang "
                "aman, bukan tidak berubah; tabel tidak memuat batas aman itu untuk dinilai."
            ),
        )

    if not ok and baseline_guessed and not (size_note or band_note) and operation in ("is_increasing", "is_decreasing"):
        other = _year_on_year_reading(src.table, labelled[-1][0], used_periods[-1], values[-1], operation, quote)
        if other:
            return _make_result(
                fact, periods, matched_source, None, None, None, src.table.unit, None, "Inconclusive",
                reason_code="klaim_tidak_lengkap",
                reasoning=(
                    f"Kalimat tidak menyebut pembandingnya: dibanding {used_periods[0].month} "
                    f"{used_periods[0].year} '{labelled[-1][0]}' {round(values[0], 4)} → {round(values[-1], 4)} "
                    f"tidak sesuai, tetapi {other} sesuai; perlu dicek pembanding yang dimaksud."
                ),
            )

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


# Words that say what a trend compares with. Without any, "IPR diprakirakan meningkat" may mean the
# month before or the same month a year before.
_TREND_BASIS = re.compile(
    r"\b(dibandingkan|dibanding|sebelumnya|lalu|secara\s+(?:bulanan|tahunan|triwulanan)"
    r"|mtm|yoy|qtq|ytd|bulanan|tahunan|triwulanan)\b", re.IGNORECASE)


def _year_on_year_reading(table, label: str, point: PeriodPoint, value: float, operation: str,
                          quote: str) -> Optional[str]:
    """"dibanding <same month a year before> …" when that reading of an unbased trend holds.

    SPE Juni 2026: "Pada Juli 2026, IPR diprakirakan meningkat." was checked against June (225,0 →
    224,4) and refuted, while the release reads it year on year (Juli 2025 222,3 → 224,4) and the
    next sentence says "secara tahunan". A sentence that names no basis is not refuted when the
    other natural reading holds; None when it names one, or neither reading holds."""
    if _TREND_BASIS.search(quote) or point.year is None or not point.month:
        return None
    _, before = table.lookup_fuzzy(label, point.year - 1, point.month)
    if before is None:
        return None
    holds = value > before if operation == "is_increasing" else value < before
    return f"dibanding {point.month} {point.year - 1} ({round(before, 4)} → {round(value, 4)})" if holds else None


# "naik dari 0,5 miliar dolar AS menjadi 0,7 miliar dolar AS": a trend that prints both ends.
_FROM_TO = re.compile(
    r"\bdari\s+(?P<a>\d[\d.]*(?:,\d+)?)\s*(?:(?:ribu|juta|miliar|triliun)\s+(?:dolar\s+AS|rupiah|Rp|USD)\s+)?"
    r"menjadi\s+(?P<b>\d[\d.]*(?:,\d+)?)\s*(?P<unit>(?:ribu|juta|miliar|triliun)\s+(?:dolar\s+AS|rupiah|Rp|USD))",
    re.IGNORECASE,
)


def _stated_endpoints_missed(quote: str, values: List[float], table_unit: Optional[str]) -> Optional[str]:
    """The sentence's own "dari A menjadi B", when neither end is anywhere near the row's values.

    "Ekspor NG naik dari 0,5 miliar dolar AS menjadi 0,7" was answered by 'Gas > Ekspor', 1.590,85
    -> 1.590,55 juta: total gas, of which NG is one part. Only the direction was compared, so the
    claim came back Tidak Sesuai although the numbers show it is another metric. Both ends must
    miss by more than rounding can explain (10% and 0,15 in the sentence's unit); a row that
    matches either end is kept. Magnitudes are compared, as BPM6 stores the debit side negative.
    """
    m = _FROM_TO.search(quote)
    if not m or len(values) < 2:
        return None
    factor = _unit_factor(m.group("unit"), table_unit)
    if not factor:
        return None
    try:
        ends = (_id_number(m.group("a")), _id_number(m.group("b")))
    except ValueError:
        return None
    rows = (abs(values[0]) / factor, abs(values[-1]) / factor)
    if all(abs(c - r) > max(0.15, 0.1 * c) for c, r in zip(ends, rows)):
        return f"{m.group('a')} → {m.group('b')} {m.group('unit')}"
    return None


# A trend stated in physical units, and table units that are money.
_VOLUME_TREND = re.compile(r"\b(volume|barel|barrel|MMBTU|ton)\b", re.IGNORECASE)
_MONEY_UNIT = re.compile(r"\b(USD|dolar|dollar|Rp|rupiah)\b", re.IGNORECASE)
# A claimed quantity counted in months, times or years ("5,3 bulan impor", "3 kali").
_COUNTED_UNIT = re.compile(r"\b(bulan|kali|tahun|hari)\b", re.IGNORECASE)

# Words that DO say a figure stayed where it was.
_UNCHANGED_WORDS = re.compile(
    r"\b(stabil\w*|relatif\s+sama|tidak\s+(banyak\s+)?berubah|datar|tidak\s+jauh\s+berbeda)\b",
    re.IGNORECASE,
)


def _says_held_not_unchanged(quote: str) -> bool:
    """"Rasio defisit … tetap terjaga, meski lebih tinggi …": a level kept safe, not a flat one.

    NPI Tw II-2026 had this sentence extracted as is_stable and refuted on -0,97 -> -3,34,
    although the sentence itself says the ratio rose. Only consulted for a stability claim the
    numbers do not support, so a "terjaga" claim that is also flat keeps its Sesuai.
    """
    return bool(_HELD_LEVEL_WORDS.search(quote)) and not _UNCHANGED_WORDS.search(quote)


# Words that speak of a figure's sign, which is what a bound of zero tests.
_SIGN_WORDS = re.compile(
    r"\b(negatif|positif|(ter)?kontraksi|ekspansi\w*|defisit|surplus|di\s+bawah|di\s+atas|minus|nol"
    r"|tumbuh|pertumbuhan|optimis|pesimis|net\s+(inflow|outflow)|inflow|outflow|arus\s+(masuk|keluar)"
    # PII: "cadangan devisa dan investasi lainnya mencatat aset neto" says both are above zero.
    r"|(aset|kewajiban)\s+neto)\b",
    re.IGNORECASE,
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
    # A bound counted in months or times is no bound on money: "di atas standar kecukupan
    # internasional sekitar 3 bulan impor" was judged on the reserves themselves (146.320 juta USD
    # > 3) and came back Sesuai (Cadev ORA, Oktober 2026).
    row_unit = getattr(src.table, "row_units", {}).get(label) or src.table.unit or ""
    if (_COUNTED_UNIT.search(fact.unit or "") and _MONEY_UNIT.search(row_unit)
            and not _COUNTED_UNIT.search(label)):
        return _make_result(
            fact, periods, matched_source, fact.claimed_value, fact.unit, None, src.table.unit, None,
            "Inconclusive", reason_code="jenis",
            reasoning=(f"Ambang klaim dalam {fact.unit}, sedangkan '{label}' di [{matched_source}] bernilai "
                       f"uang ({src.table.unit}); keduanya tidak bisa dibandingkan."),
        )
    threshold = fact.claimed_value
    if threshold is None and (_NET_BALANCE_UNIT.search(src.table.unit or "")
                              or _NET_BALANCE_UNIT.search(label)):
        # A net balance's line is zero: "SBT Deposito positif, menunjukkan pertumbuhan" names no
        # bound. The Tinjauan AI answers such claims as above_threshold on the cited cell, and
        # without a bound the engine could not reproduce it (SBank Tw I/II-2026, 4 answers).
        threshold = 0.0
    if threshold is None:
        return _make_result(
            fact, periods, matched_source, None, fact.unit, value, fact.unit, None, "Inconclusive", reason_code="klaim_tidak_lengkap",
            reasoning="Klaim ambang tanpa nilai ambang yang jelas; tidak dapat dinilai.",
        )
    if threshold == 0 and not _SIGN_WORDS.search(fact.context_quote or ""):
        # PII Tw I-2026: "posisi cadangan devisa yang menurun terutama bersumber dari posisi
        # cadangan devisa lainnya sebesar 9,1" came back as 'Cadangan Devisa Lainnya' below 0 —
        # a fall read as a sign, refuted on a level of 126,8. A zero bound needs the sentence to
        # speak of a sign at all.
        return _make_result(
            fact, periods, matched_source, threshold, fact.unit, value, fact.unit, None, "Inconclusive", reason_code="klaim_tidak_lengkap",
            reasoning="Klaim ambang 0, tetapi kalimatnya tidak menyebut tanda (positif/negatif, "
                      "ekspansi/kontraksi, surplus/defisit); tidak dapat dinilai.",
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


_RISE_WORDS = re.compile(r"\b(meningkat|naik|kenaikan|peningkatan|bertambah|penambahan)\b", re.IGNORECASE)
_FALL_WORDS = re.compile(r"\b(menurun|turun|penurunan|berkurang|pengurangan)\b", re.IGNORECASE)


def _reinterpret_value_as_change(
    fact: ExtractedFact, label: str, factor: float, table_value: float, src: _ExcelSource
) -> Optional[FactVerificationResult]:
    """A refuted 'value' whose number is the row's change from the period before, in a sentence
    about a rise or fall in that direction — re-checked as a 'diff'.

    PII Tw I-2026: "posisi cadangan devisa yang menurun terutama bersumber dari posisi cadangan
    devisa lainnya sebesar 9,1 miliar dolar AS" — 9,1 is how far 'Cadangan Devisa Lainnya' fell
    (135,9 -> 126,8), not its level, and was refuted against 126,8. Only consulted after the
    value check failed; a change in the other direction, or none matching, leaves it Refuted.
    """
    point = fact.periods[0] if fact.periods else None
    quote = fact.context_quote or ""
    rises, falls = bool(_RISE_WORDS.search(quote)), bool(_FALL_WORDS.search(quote))
    if (point is None or point.col_label is not None or point.year is None or not point.month
            or fact.claimed_value is None or not (rises or falls)):
        return None
    if point.month in _MONTH_ABBREVS:
        names = _MONTH_ABBREVS
    elif re.fullmatch(r"Q[1-4]", point.month):
        names = ("Q1", "Q2", "Q3", "Q4")
    else:
        return None
    year, index = divmod(point.year * len(names) + names.index(point.month) - 1, len(names))
    prior_raw = src.table._data.get((label, year, names[index]))
    if prior_raw is None:
        return None
    prior = prior_raw / factor
    change = round(table_value - prior, 4)
    if (change < 0 and not falls) or (change > 0 and not rises):
        return None
    # Held to MATCH_TOLERANCE like any stated change, and to the last printed digit when the
    # sentence prints more (0,22 -> 0,01). A change printed without decimals gets no wider bound:
    # the user's call (2026-10-21) — "23 juta" for a fall of 22,78 is Tidak Sesuai.
    printed = _printed_decimals(fact.claimed_value, quote)
    tolerance = min(MATCH_TOLERANCE, 10 ** -printed) if printed is not None else 2 * MATCH_TOLERANCE
    delta, verdict = _numeric_verdict(abs(fact.claimed_value), abs(change), tolerance)
    if verdict != "Entailed":
        return None
    prior_point = PeriodResult(metric_label=label, year=year, month=names[index], excel_value=round(prior, 4))
    current = PeriodResult(metric_label=label, year=point.year, month=point.month, excel_value=round(table_value, 4))
    result = _make_result(
        fact, [prior_point, current], src.label, fact.claimed_value, fact.unit, change, fact.unit, delta,
        "Entailed",
        reasoning=(
            f"PDF: {fact.claimed_value} {fact.unit} | Klaim ini ditandai sebagai nilai, tetapi kalimatnya "
            f"menyebut perubahan dan angkanya cocok dengan perubahan '{label}' dari {names[index]} {year} "
            f"({round(prior, 4)}) ke {point.month} {point.year} ({round(table_value, 4)}) = {change} "
            f"{fact.unit} — dinilai sebagai selisih. | Δ = {delta} → within tolerance {tolerance}"
        ),
    )
    return result.model_copy(update={"operation": "diff"})


# Words that make a report print the SIZE of a negative balance as a positive number: "defisit
# 12,5 miliar dolar AS", "kewajiban neto 197,4", "arus keluar neto 2,2", "kontraksi 0,1".
_NEGATIVE_BALANCE_WORDS = re.compile(
    r"\b(defisit|kewajiban\s+neto|net\s+liabilit\w*|arus\s+keluar\s+neto|net\s+outflow"
    # BPM6 records residents' net purchases abroad (an asset increase) negative.
    r"|pembelian\s+neto|penjualan\s+neto"
    r"|(ter)?kontraksi)\b",
    re.IGNORECASE,
)


def _fall_before_rate(fact: ExtractedFact) -> Optional[re.Match]:
    """"turun 4,1%": a fall word right before the claimed number, printed as a rate. SPE Juni 2026
    "lebih baik dibandingkan Juli 2025 yang turun 4,1% (mtm)" came back +4,1 against the −4,1 of
    Tabel 3 and was refuted. Only a rate: "turun 4,1 poin" or "turun sebesar 4,1 triliun" is a
    change, not a negative level."""
    text = f"{abs(fact.claimed_value):.4f}".rstrip("0").rstrip(".")
    whole, _, frac = text.partition(".")
    number = re.escape(whole) + (r"\s*,\s*" + re.escape(frac) if frac else "")
    return re.search(r"\b(?:menurun|turun|terkoreksi)\s+(?:sebesar\s+)?" + number + r"\s*%",
                     fact.context_quote or "", re.IGNORECASE)


# BPM6 tables (NPI) store the debit side negative: 'Impor', 'Pembayaran'. A report prints
# "impor minyak … sebesar 11,6 miliar dolar AS" and "impor gas … meningkat" about their size.
_DEBIT_ROW = re.compile(r"^(impor|pembayaran|debit)\b", re.IGNORECASE)


def _is_debit_row(label: str) -> bool:
    return bool(_DEBIT_ROW.match(label.split(QUAL_SEP.strip())[-1].strip()))


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
    word = _NEGATIVE_BALANCE_WORDS.search(fact.context_quote or "") or _fall_before_rate(fact)
    if word is None and all(_is_debit_row(label) for label, _ in resolved):
        word = re.search(r".+", resolved[0][0].split(QUAL_SEP.strip())[-1].strip())
    if word is None:
        return None
    # A sum over several rows carries each row's rounding; only a single cell is rounded.
    delta, verdict, rule = _value_verdict(
        fact.claimed_value, -computed, fact.context_quote if fact.operation == "value" else None)
    if verdict != "Entailed":
        return None
    return _make_result(
        fact, periods, src.label, fact.claimed_value, fact.unit, round(computed, 4), fact.unit, delta,
        "Entailed",
        reasoning=(
            f"PDF: {fact.claimed_value} {fact.unit} ('{word.group(0)}') | "
            f"Excel [{src.label}] ({resolved[0][0]}): {round(computed, 4)} {fact.unit} | "
            f"Laporan menulis besarnya {word.group(0).lower()} sebagai angka positif; tabel "
            f"mencatat saldonya negatif. Besaran: {rule}"
        ),
    )


# "transaksi modal dan finansial" — two rows named at once, the second sharing the first's head
# words ('Transaksi Modal' + 'Transaksi Finansial').
_JOINED_LABEL = re.compile(r"(?P<left>[^,]+?)\s+dan\s+(?P<right>[^,]+)", re.IGNORECASE)


def _row_leaf(label: str) -> str:
    return " ".join(label.split(QUAL_SEP.strip())[-1].split()).casefold()


def _joined_in_sentence(label: str, quote: str) -> Optional[List[str]]:
    """['Transaksi Modal', 'Transaksi Finansial'] when the sentence writes the label's last word
    joined to another under the same head ("transaksi modal dan finansial"); else None. Not when
    the sentence gives each its own figure ("ekspor barang dan jasa masing-masing sebesar …")."""
    words = label.split()
    if len(words) < 2 or QUAL_SEP.strip() in label or re.search(r"\bmasing", quote, re.IGNORECASE):
        return None
    head, tail = words[:-1], words[-1]
    pattern = (r"\b" + r"\s+".join(map(re.escape, head)) + r"\s+(\w+)\s+dan\s+"
               + re.escape(tail) + r"\b")
    match = re.search(pattern, quote, re.IGNORECASE)
    if not match:
        return None
    return [" ".join(head + [match.group(1)]), " ".join(words)]


def _joined_rows_as_sum(fact: ExtractedFact, sources: List[_ExcelSource]) -> Optional[ExtractedFact]:
    """A level claimed for 'X dan Y' where the table has rows X and Y but none for both: their sum.

    NPI: "transaksi modal dan finansial mencatat defisit 4,8 miliar dolar AS" has no row of its
    own; lookup_fuzzy matched 'Transaksi Modal' alone (4 juta) and refuted a true claim, while
    the same sentence's Tw II figure came out of the extractor as a sum and was confirmed.
    Only for amounts: a percentage named for two rows is not their sum. Both halves must be
    rows of the same table under exactly those names, so nothing near-miss gets added up.
    """
    if fact.operation != "value" or fact.periods[0].col_label:
        return None
    if "%" in (fact.unit or "") or "persen" in (fact.unit or "").lower():
        return None
    points = fact.periods
    if len(points) > 1:
        # "transaksi modal dan finansial … 12,0 miliar" extracted as one value over the two
        # rows: one number for several rows at one period can only be their total. (It used to
        # crash the value check, which reads one row.)
        same_period = len({(q.year, q.month) for q in points}) == 1
        distinct = len({q.metric_label.strip().lower() for q in points}) == len(points)
        if same_period and distinct and all(q.col_label is None for q in points):
            joined = copy.copy(fact)
            joined.operation = "sum"
            return joined
        return None
    point = fact.periods[0]
    match = _JOINED_LABEL.fullmatch(" ".join(point.metric_label.split()))
    if match:
        left, right = match.group("left").split(), match.group("right").split()
        if len(right) < len(left):
            right = left[: len(left) - len(right)] + right
        halves = [" ".join(left), " ".join(right)]
        whole = " ".join(point.metric_label.split()).casefold()
    else:
        # NPI Tw I-2026: "transaksi modal dan finansial … setelah pada triwulan sebelumnya
        # mencatat surplus 9,0" came back as 'Transaksi Finansial' alone (8,79); the sentence
        # still names both rows (0,17 + 8,79 = 9,0).
        halves = _joined_in_sentence(point.metric_label, fact.context_quote or "")
        if halves is None:
            return None
        whole = f"{halves[0]} dan {halves[1].split()[-1]}".casefold()
    for src in sources:
        table = src.table
        if table.axis_type != "temporal" or "%" in (table.unit or ""):
            continue
        if any(_row_leaf(row) == whole for row in table.row_labels):
            return None  # the table has a row for both together
        rows = [table.lookup_fuzzy(half, point.year, point.month)[0] for half in halves]
        if all(rows) and rows[0] != rows[1] and all(
            _row_leaf(row) == half.casefold() for row, half in zip(rows, halves)
        ):
            joined = copy.copy(fact)
            joined.operation = "sum"
            joined.periods = [PeriodPoint(half, point.year, point.month) for half in halves]
            return joined
    return None


# A sentence carrying several periods' figures: "… masing-masing tercatat sebesar 149,9 dan 177,9,
# lebih tinggi dibandingkan … periode sebelumnya yang masing-masing tercatat sebesar 146,5 dan 166,0".
_SEVERAL_PERIODS_RE = re.compile(r"\b(sebelumnya|masing-masing|masing\s+masing)\b", re.IGNORECASE)
_NEIGHBOURS = {"month": 6, "quarter": 2}


_MONTH_WORD_ID = {"Jan": "januari", "Feb": "februari", "Mar": "maret", "Apr": "april", "May": "mei",
                  "Jun": "juni", "Jul": "juli", "Aug": "agustus", "Sep": "september", "Oct": "oktober",
                  "Nov": "november", "Dec": "desember"}
_QUARTER_ROMAN = {"Q1": "I", "Q2": "II", "Q3": "III", "Q4": "IV"}


def _period_printed_beside(quote: str, value: float, year: int, month: str) -> bool:
    """True when the sentence prints (year, month) within a few words of the claimed number."""
    if month in _MONTH_WORD_ID:
        period = re.compile(r"\b" + _MONTH_WORD_ID[month] + r"\s+" + str(year) + r"\b", re.IGNORECASE)
    elif month in _QUARTER_ROMAN:
        period = re.compile(r"\b(?:triwulan|tw\.?)\s+" + _QUARTER_ROMAN[month] + r"\s+" + str(year) + r"\b",
                            re.IGNORECASE)
    else:
        return False
    for match in _PRINTED_NUMBER.finditer(quote):
        if not any(abs(n - abs(value)) < 1e-9 for n, _ in _readings(match.group(0))):
            continue
        if period.search(quote[max(0, match.start() - 60):match.end() + 45]):
            return True
    return False


def _value_at_a_neighbouring_period(
    fact: ExtractedFact, label: str, factor: float, src: "_ExcelSource"
) -> Optional[str]:
    """'<Mon YYYY>' when a refuted number is the same row's figure a few periods away, in a sentence
    that lists several periods' figures — the extractor put it on the wrong one.

    SPE Agustus 2026: "IEH November 2026 dan Februari 2027 … 149,9 dan 177,9, lebih tinggi
    dibandingkan … periode sebelumnya … 146,5 dan 166,0": 166,0 is January 2027's, filed under
    November 2026 (149,9) and refuted. A stale number in a report would also land here — it then
    comes back as a question for the checker, not as a confirmed error.
    """
    point = fact.periods[0] if fact.periods else None
    quote = fact.context_quote or ""
    if (point is None or point.col_label is not None or point.year is None or not point.month
            or fact.claimed_value is None):
        return None
    # Only where the sentence lists several periods' figures, or the quote is just the number
    # ("dan 166,0") — and never where it prints the claim's own period beside the number: NPI's
    # "0,7 miliar dolar AS pada triwulan II 2026" is dated by its author, and 0,7 also being Q4
    # 2025's figure is a coincidence, not a sign of misreading.
    if not (_SEVERAL_PERIODS_RE.search(quote) or len(quote.strip()) < 40):
        return None
    if _period_printed_beside(quote, fact.claimed_value, point.year, point.month):
        return None
    if point.month in _MONTH_ABBREVS:
        names, span = _MONTH_ABBREVS, _NEIGHBOURS["month"]
    elif re.fullmatch(r"Q[1-4]", point.month):
        names, span = ("Q1", "Q2", "Q3", "Q4"), _NEIGHBOURS["quarter"]
    else:
        return None
    here = point.year * len(names) + names.index(point.month)
    for step in range(1, span + 1):
        for index in (here - step, here + step):
            year, month = divmod(index, len(names))
            raw = src.table._data.get((label, year, names[month]))
            if raw is None:
                continue
            _d, verdict, _rule = _value_verdict(fact.claimed_value, raw / factor, fact.context_quote)
            if verdict == "Entailed":
                return f"{names[month]} {year}"
    return None


def _compute_operation(
    fact: ExtractedFact, resolved: List[Tuple[str, float]], factor: float, src: _ExcelSource
) -> FactVerificationResult:
    op = fact.operation
    matched_source = src.label

    if op in _THRESHOLD_OPS:
        return _compute_threshold(fact, resolved, src)

    if op == "value":
        table_value = resolved[0][1] / factor
        computed = round(table_value, 4)
        # One number is one cell; a value the extractor gave two periods ("semester II" as Q3 and
        # Q4) is read at the first, as before, without indexing past the one value.
        periods = _build_periods(fact.periods[:1], resolved[:1], [table_value])
        if fact.claimed_value is None:
            # A Tinjauan AI answer "value" for a claim with no number of its own ("tetap tumbuh"):
            # nothing to compare, and the subtraction used to crash.
            return _make_result(
                fact, periods, matched_source, None, fact.unit, computed, src.table.unit, None,
                "Inconclusive", reason_code="klaim_tidak_lengkap", reasoning="Klaim nilai tanpa angka yang disebut; tidak dapat dinilai.",
            )
        delta, verdict, rule = _value_verdict(fact.claimed_value, table_value, fact.context_quote)
        if verdict == "Refuted":
            recovered = _reinterpret_signed_level(fact, resolved, periods, table_value, src)
            if recovered is not None:
                return recovered
            elsewhere = _value_at_a_neighbouring_period(fact, resolved[0][0], factor, src)
            if elsewhere is not None:
                return _make_result(
                    fact, periods, matched_source, fact.claimed_value, fact.unit, computed, fact.unit,
                    None, "Inconclusive", reason_code="periode",
                    reasoning=(
                        f"PDF: {fact.claimed_value} {fact.unit} | Excel [{matched_source}] "
                        f"({resolved[0][0]}) {fact.periods[0].month} {fact.periods[0].year}: {computed} | "
                        f"angka yang diklaim sama dengan {elsewhere} pada baris yang sama — kalimat ini "
                        f"menyebut beberapa periode, kemungkinan periodenya tertukar; perlu dicek manual."
                    ),
                )
        return _make_result(
            fact, periods, matched_source, fact.claimed_value, fact.unit, computed, fact.unit, delta, verdict,
            reasoning=(
                f"PDF: {fact.claimed_value} {fact.unit} | "
                f"Excel [{matched_source}] ({resolved[0][0]}): {computed} {fact.unit} | {rule}"
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
        diff_points = list(fact.periods)
        if len(resolved) == 1:
            # "Pada akhir triwulan IV 2025, kewajiban neto PII naik sebesar 10,8" arrives with Q4
            # alone, and Q4 − Q4 refuted it. A change stated for one period runs from the one before.
            p0 = diff_points[0]
            prev = (_previous_period(src.table, resolved[0][0], p0.year, p0.month)
                    if p0.col_label is None and p0.year is not None and p0.month else None)
            if prev is None:
                return _make_result(
                    fact, [], matched_source, fact.claimed_value, fact.unit, None, fact.unit, None,
                    "Inconclusive", reason_code="periode",
                    reasoning=(f"Klaim perubahan hanya menyebut satu periode dan tabel tidak punya periode "
                               f"sebelumnya untuk '{resolved[0][0]}' sebagai pembanding."))
            (py, pm), pv = prev
            resolved = [(resolved[0][0], pv), resolved[0]]
            diff_points = [PeriodPoint(metric_label=resolved[0][0], year=py, month=pm), p0]
        converted = [raw / factor for (_, raw) in resolved]
        computed = round(converted[-1] - converted[0], 4)
        periods = _build_periods(diff_points, resolved, converted)
        # "defisit 0,9 … dibandingkan … 9,1" read as a change of 8,2 that the sentence never
        # prints: both ends are rounded, so the change carries both roundings (8,26 here).
        diff_tolerance = (MATCH_TOLERANCE if _printed_in(fact.claimed_value, fact.context_quote)
                          else 2 * MATCH_TOLERANCE)
        delta, verdict = _numeric_verdict(fact.claimed_value, computed, diff_tolerance)
        if verdict == "Refuted":
            recovered = _reinterpret_diff_as_value(fact, resolved, converted, computed, src)
            if recovered is not None:
                return recovered
            quote = fact.context_quote or ""
            if all(v < 0 for v in converted) and (
                _DEFICIT_TREND.search(quote) or _DEFICIT_TREND_NOUN_FIRST.search(quote)
                or _DEFICIT_TREND_LATER_IN_SENTENCE.search(quote)
            ):
                # "Kewajiban neto PII ... menurun sebesar 25,6" on -223,0 -> -197,4: the change
                # the report states is in the size of the net liabilities, not in the balance.
                size_change = round(converted[0] - converted[-1], 4)
                size_delta, size_verdict = _numeric_verdict(
                    fact.claimed_value, size_change, diff_tolerance)
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

    if op == "ratio" and len(resolved) == 1:
        # A ratio given as ONE point is a row that holds the ratio itself ('Rasio Utang terhadap
        # PDB'): compare it as a value. Unpacking it as numerator and denominator crashed.
        as_value = ExtractedFact(
            operation="value", periods=fact.periods, claimed_value=fact.claimed_value, unit=fact.unit,
            context_quote=fact.context_quote, page_number=fact.page_number,
        )
        return _compute_operation(as_value, resolved, factor, src).model_copy(update={"operation": "ratio"})

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
        reasoning = _why("tidak_ditemukan", f"Data tidak ditemukan di sumber Excel manapun untuk: {missing_str}.")
    else:
        reasoning = _why("tidak_ditemukan",
                         f"Tidak ada sumber Excel yang cocok untuk operasi '{fact.operation}' pada '{fact.display_label}'.")
    return _make_result(
        fact, [], None, fact.claimed_value, fact.unit, None, fact.unit, None, "Inconclusive", reasoning=reasoning,
    )


# ---------------------------------------------------------------------------
# Per-fact evaluation (multi-source): tries each source in order, first source with ALL
# periods resolved AND a compatible unit (if the operation needs one) wins.
# ---------------------------------------------------------------------------

def _without_own_names(quote: str, fact: ExtractedFact) -> str:
    """The sentence with the claim's own row names cut out, for the subset-phrase test.

    SPE Agustus 2026: "kenaikan penjualan pada Kelompok Barang Lainnya, Kelompok Perlengkapan
    Rumah Tangga Lainnya, serta Kelompok Barang Budaya dan Rekreasi" names three groups; but
    'Kelompok Barang Lainnya' reads as the hedge "kelompok … lainnya" (the other groups), and
    every claim of the sentence was refused as unnamed."""
    for label in {p.metric_label for p in fact.periods if p.metric_label}:
        leaf = label.rsplit(QUAL_SEP, 1)[-1].strip()
        if len(leaf) > 3:
            quote = re.sub(re.escape(leaf), " ", quote, flags=re.IGNORECASE)
    return quote


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
    if _quote_bounds_group(metric_label.rsplit(QUAL_SEP, 1)[1], quote):
        return True
    # City tables put the group first ('2. Bandung > Indeks Ekspektasi Konsumen (IEK)'): SK Juli
    # 2026 "penurunan indeks pada sejumlah kota, terutama di Bandung, Pontianak, dan Samarinda"
    # names its cities, and was left Tidak Cukup Data as an unnamed subset. A first level counts
    # only when it is a name, not the series ('Indeks …', 'Saldo Bersih …').
    words, figures = _label_tokens(metric_label.split(QUAL_SEP, 1)[0])
    return (bool(words) and not figures and not words & _SERIES_WORDS
            and words <= _label_tokens(quote)[0])


# Words of a series' own name, never of a group's: see _quote_names_qualifier.
_SERIES_WORDS = frozenset({"indeks", "index", "saldo", "bersih", "total", "jumlah", "nilai"})

# A group noun and the words after it: "Kelompok Makanan, Minuman, dan Tembakau", "Subkelompok
# Sandang", "kota Medan".
_MEMBER_AFTER_NOUN_RE = re.compile(
    r"\b(?:sub)?(?:kelompok|kota|daerah|wilayah|provinsi)\s+((?:\S+\s*){1,6})", re.IGNORECASE)


def _member_named(metric_label: str, quote: str) -> bool:
    """True when a flat breakdown row (no qualifier) is named as a member in the sentence: its
    words follow a group noun. SPE Juni 2026: "kelompok lainnya mencatatkan perbaikan …, yaitu
    pada Kelompok Makanan, Minuman, dan Tembakau (…) serta Subkelompok Sandang" lists the members
    of its "lainnya", and both were refused as an unnamed subset."""
    if not metric_label or QUAL_SEP in metric_label:
        return False
    words = _label_tokens(re.sub(r"^o/w\s+", "", metric_label, flags=re.IGNORECASE))[0]
    if not words:
        return False
    for m in _MEMBER_AFTER_NOUN_RE.finditer(quote):
        # The name starts right after the noun: "… serta Kelompok Dari sisi harga, tekanan inflasi"
        # (two sentences run together) does not name 'tekanan inflasi' as a group.
        after = re.findall(r"[a-z0-9]+", m.group(1).lower())
        if after and after[0] in words and words <= _label_tokens(m.group(1))[0]:
            return True
    return False


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


# A survey's answer categories, not groups: SKDU Tabel 8 splits each LU into 'Naik' / 'Tetap' /
# 'Turun' / 'Saldo Bersih', and "kegiatan usaha LU Perdagangan tetap terjaga" read as a claim
# about the row 'Perdagangan ... > Tetap'.
_SURVEY_ANSWER_WORDS = frozenset({
    "naik", "tetap", "turun", "baik", "buruk", "normal", "cukup", "sama", "mudah", "sulit",
    "lebih", "meningkat", "menurun", "stabil",
})


def _balance_row_instead(
    fact: ExtractedFact, resolved: List[Tuple[str, float]], src: _ExcelSource
) -> Optional[List[Tuple[str, float]]]:
    """The group's balance row, when a trend or level claim landed on one answer's share of it.

    SKDU Tw III-2026: "kondisi keuangan dunia usaha secara umum membaik pada aspek Likuiditas dan
    Rentabilitas" came back on 'Likuiditas', which found '… - Likuiditas > Baik' (the share that
    answered "baik") and was then refused as one group. What "membaik" is about is the group's
    'Saldo Bersih (% Baik - % Buruk)' (15,03 -> 15,56). Only when the claim never names the answer
    and the group has exactly one balance row; a claim about the share itself ("persentase
    responden yang menjawab lebih baik") names it and keeps its row.
    """
    if fact.operation not in _TREND_OPS | _THRESHOLD_OPS:
        return None
    claim_words = _sig_words(" ".join(p.metric_label or "" for p in fact.periods))
    out = []
    for point, (label, _value) in zip(fact.periods, resolved):
        if QUAL_SEP not in label:
            return None
        parent, leaf = label.rsplit(QUAL_SEP, 1)
        leaf_words = _sig_words(leaf)
        if not leaf_words or not leaf_words <= _SURVEY_ANSWER_WORDS or leaf_words & claim_words:
            return None
        balance = [row for row in src.table.row_labels
                   if row.startswith(parent + QUAL_SEP)
                   and row.rsplit(QUAL_SEP, 1)[-1].strip().lower().startswith("saldo bersih")]
        if len(balance) != 1:
            return None
        key = ((balance[0], point.col_label) if point.col_label is not None
               else (balance[0], point.year, point.month))
        value = src.table._data.get(key)
        if value is None:
            return None
        out.append((balance[0], value))
    return out


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
        if words and not figures and words <= _SURVEY_ANSWER_WORDS:
            return False
        return bool(words | set(figures)) and words <= quote_words and set(figures) <= quote_figures

    wanted = {(p.year, p.month) for p in fact.periods if p.year is not None}
    for src in sources:
        if src.origin == "chart" or not src.table.row_labels:
            continue
        # A table with no column for the claim's period cannot hold its groups: SKDU's
        # semester margin table ('… > Margin Minimum yang Tidak Mengganggu Kegiatan Usaha')
        # was taken as the LU breakdown of a quarterly "kegiatan usaha meningkat" claim.
        if wanted and not any((k[1], k[2]) in wanted for k in src.table._data if len(k) == 3):
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
            # A group whose name is part of the series' own name ('PERTANIAN, KEHUTANAN DAN
            # PERIKANAN > Perikanan') is "named" by every sentence about the whole series.
            series_tokens = set().union(*(_label_tokens(level)[0]
                                          for level, own in zip(levels, is_series) if own))
            if any(_label_tokens(level)[0] <= series_tokens for level in others):
                continue
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


# ---------------------------------------------------------------------------
# Quantified group claims: "seluruh", "kecuali", "sebagian besar", "lainnya"
# ---------------------------------------------------------------------------

_SCOPE_TEXT = {"all": "seluruh kelompok", "majority": "sebagian besar kelompok",
               "rest": "kelompok lainnya"}
# A level of a row name that totals its siblings rather than being one of them.
_AGGREGATE_GROUP_RE = re.compile(
    r"^(?:total|jumlah|keseluruhan|nasional|rata-?rata|semua|seluruh)\b", re.IGNORECASE
)


def _levels(label: str) -> List[str]:
    return [level.strip() for level in label.split(QUAL_SEP)]


def _group_family(labels: List[str], src: _ExcelSource) -> Optional[Tuple[List[str], str]]:
    """Every row of `src` in the same breakdown as `labels`, and the shared series name.

    The breakdown is the one level in which the given rows differ ('Penjualan > Cabang Utara'
    vs '... > Cabang Selatan' differ in the last; 'Cabang Utara > Penjualan' vs 'Cabang Selatan
    > Penjualan' in the first). Its members are the table's rows that agree with them on every
    other level — the siblings — minus rows that total the others ('Total', 'Jumlah'). Needs at
    least two different rows to tell which level is the group; None when it cannot.
    """
    split = [_levels(label) for label in dict.fromkeys(labels)]
    # A breakdown is a level BELOW a series ('Penjualan > Cabang Utara'); whole series side by
    # side ('Omzet', 'Biaya') are different indicators, not groups of one.
    if len(split) < 2 or len({len(s) for s in split}) != 1 or len(split[0]) < 2:
        return None
    differing = [i for i in range(len(split[0])) if len({s[i] for s in split}) > 1]
    if len(differing) != 1:
        return None
    pos = differing[0]
    fixed = split[0][:pos] + split[0][pos + 1:]
    # One series can carry two breakdowns side by side ('Penjualan > Wilayah Utara' and
    # 'Penjualan > Usia 20-30 th'). A word every listed group shares ('wilayah') names the
    # dimension, and the family keeps to it.
    shared = frozenset.intersection(*(_label_tokens(s[pos])[0] for s in split))
    family = []
    for row in src.table.row_labels:
        levels = _levels(row)
        if (len(levels) == len(split[0]) and levels[:pos] + levels[pos + 1:] == fixed
                and not _AGGREGATE_GROUP_RE.match(levels[pos])
                and shared <= _label_tokens(levels[pos])[0]):
            family.append(row)
    return list(dict.fromkeys(family)), QUAL_SEP.join(fixed)


# Words of a quantified sentence that say nothing about WHICH breakdown it is about.
_SCOPE_FILLER_WORDS = frozenset(
    "kelompok seluruh semua sebagian besar mayoritas lainnya lain indeks pada di dan yang "
    "level optimis pesimis meningkat menurun naik turun peningkatan penurunan terjadi tetap "
    "berada tercatat sebesar dengan untuk dari secara".split()
)


# The dimension a RANGE group belongs to, by its unit: a rupiah band is an expenditure/income
# group, a band of years an age group. Lets "kelompok pengeluaran" find rows named only
# 'Rp 1 - 2 juta'.
_SPAN_DIMENSION_WORDS = {"rp": {"pengeluaran", "pendapatan", "penghasilan"}, "age": {"usia", "umur"}}


def _group_dimension_words(level: str) -> set:
    words = set(_label_tokens(level)[0])
    span = _GROUP_SPAN_RE.search(level)
    if span:
        words |= _SPAN_DIMENSION_WORDS.get(_span(span)[0], set())
    return words


def _family_from_sentence(fact: ExtractedFact, sources: List[_ExcelSource]):
    """(family, series, source) for a quantified fact that lists only its indicator.

    "Keyakinan konsumen tetap optimis pada seluruh kelompok pengeluaran" can come back as one
    fact on 'Indeks Keyakinan Konsumen (IKK)' with no group at all. The breakdown is then the
    one whose group names share a word with the sentence ('pengeluaran'), among the rows that
    put a group level next to that indicator.
    """
    series_labels = list(dict.fromkeys(p.metric_label for p in fact.periods if QUAL_SEP not in p.metric_label))
    if len(series_labels) != 1:
        return None
    quote_words = _label_tokens(fact.context_quote or "")[0] - _SCOPE_FILLER_WORDS
    best = None
    for src in sources:
        if src.origin == "chart" or not src.table.row_labels:
            continue
        series_words = _sig_words(src.table._normalise_query(series_labels[0]))
        if not series_words:
            continue
        by_position: Dict[int, List[str]] = {}
        for row in src.table.row_labels:
            levels = _levels(row)
            if len(levels) != 2:
                continue
            for i, level in enumerate(levels):
                other = levels[1 - i]
                if (series_words <= _sig_words(level) and not _AGGREGATE_GROUP_RE.match(other)
                        and (_group_dimension_words(other) - series_words) & quote_words):
                    by_position.setdefault(1 - i, []).append(row)
        for rows in by_position.values():
            found = _group_family(rows, src)
            if found and (best is None or len(found[0]) > len(best[0])):
                best = (found[0], found[1], src)
    return best


def _series_of(label: str) -> str:
    return _normalize_quote(label.rsplit(QUAL_SEP, 1)[0] if QUAL_SEP in label else label)


def _drop_group_stand_ins(facts: List[ExtractedFact]) -> List[ExtractedFact]:
    """Drop the model's stand-ins for a quantified claim of the same sentence (A4).

    SK Sept 2026: "optimis pada kelompok usia 20-30 tahun, sedangkan kelompok usia lainnya
    pesimis" came back with a 'rest' fact for the other ages AND a below_threshold fact on
    'IKLK > SMA' — an education group the sentence never names, put there to stand for the same
    'lainnya'. Kept, it shows as Tidak Cukup Data beside a claim that is fully checked. A fact
    is a stand-in when its group is not named in its sentence (see _quote_names_qualifier) and
    the sentence has a quantified fact of the same operation on the same series; a fact whose
    group the sentence does name is always kept.
    """
    scoped = {}
    for f in facts:
        if f.group_scope and f.operation in _TREND_OPS | _THRESHOLD_OPS:
            key = (_normalize_quote(f.context_quote), f.operation)
            scoped.setdefault(key, set()).update(_series_of(p.metric_label) for p in f.periods)
    kept = []
    for f in facts:
        series = scoped.get((_normalize_quote(f.context_quote), f.operation))
        quote = f.context_quote or ""
        if (series and not f.group_scope and all(QUAL_SEP in p.metric_label for p in f.periods)
                and not any(_quote_names_qualifier(p.metric_label, quote) for p in f.periods)
                and {_series_of(p.metric_label) for p in f.periods} <= series):
            logger.info("Dropping stand-in fact %r for a quantified claim of its sentence",
                        f.display_label)
            continue
        kept.append(f)
    return kept


# A subject that is nothing but the word for the groups: "seluruh LU terindikasi melambat".
_GROUP_WORD = r"(?:sub-?\s?LU|LU|lapangan\s+usaha|sektor)"
_MEMBER_PREFIX = re.compile(r"^\s*" + _GROUP_WORD + r"\s+(?=\S)", re.IGNORECASE)
_SAYS_BALANCE = re.compile(r"\b(SB|SBT|saldo\s+bersih)\b", re.IGNORECASE)


def _attach_named_members(facts: List[ExtractedFact]) -> None:
    """Turn "seluruh LU …" whose subject the extractor gave as the bare group word into a claim on
    every group, pointed at the groups the same sentence names.

    SKDU Tw III-2026: "Berdasarkan LU, seluruh LU terindikasi melambat dibandingkan semester I
    2026, terutama … LU Industri Pengolahan (SB 9,18%), … LU Jasa Lainnya (SB 7,32%)" came back as
    a trend on 'LU' with no group scope, while the named sectors came back as facts of their own.
    Those names (without the 'LU' prefix, and as the balance row when the sentence speaks of SB)
    show _judge_group_scope which breakdown "seluruh LU" ranges over.
    """
    for fact in facts:
        labels = {p.metric_label.strip() for p in fact.periods}
        if (fact.group_scope or fact.operation not in _TREND_OPS | _THRESHOLD_OPS or len(labels) != 1
                or not re.fullmatch(_GROUP_WORD, next(iter(labels)), re.IGNORECASE)):
            continue
        quote = fact.context_quote or ""
        if not re.search(r"\bseluruh\s+" + _GROUP_WORD + r"\b", quote, re.IGNORECASE):
            continue
        balance = " > Saldo Bersih" if _SAYS_BALANCE.search(quote) else ""
        members = list(dict.fromkeys(
            _MEMBER_PREFIX.sub("", p.metric_label).strip() + balance
            for other in facts if other is not fact and other.context_quote == quote
            for p in other.periods if _MEMBER_PREFIX.match(p.metric_label)
        ))
        if not members:
            continue
        times = list(dict.fromkeys((p.year, p.month, p.col_label) for p in fact.periods))
        fact.group_scope = "all"
        fact.periods = [PeriodPoint(member, y, m, c) for member in members for (y, m, c) in times]


def _attach_group_exclusions(facts: List[ExtractedFact]) -> None:
    """Give each quantified fact the groups its own sentence names separately.

    "Naik di seluruh cabang, kecuali Cabang Barat yang turun" arrives as an "all" fact and a
    named fact for Cabang Barat with the opposite direction; "naik di Cabang Utara, sedangkan
    cabang lainnya turun" as a named fact and a "rest" fact. The named groups are not part of
    the quantified set. For "all"/"majority" only a named fact claiming something DIFFERENT is
    an exception — "naik di sebagian besar cabang, terutama di Cabang Utara" names a member of
    the majority, not an exception to it. "rest" is, by definition, every group not named.
    """
    for fact in facts:
        fact.group_exclude = []
    for fact in facts:
        if not fact.group_scope:
            continue
        quote = _normalize_quote(fact.context_quote)
        excluded: List[str] = []
        for other in facts:
            if (other is fact or other.group_scope or _normalize_quote(other.context_quote) != quote
                    or other.operation not in _TREND_OPS | _THRESHOLD_OPS):
                continue
            if fact.group_scope != "rest" and (other.operation, other.claimed_value) == (
                fact.operation, fact.claimed_value
            ):
                continue
            excluded.extend(p.metric_label for p in other.periods)
        if fact.group_scope == "rest" and not excluded:
            excluded = _named_in_previous_sentence(fact, facts)
        fact.group_exclude = list(dict.fromkeys(excluded))


def _named_in_previous_sentence(fact: ExtractedFact, facts: List[ExtractedFact]) -> List[str]:
    """The rows the sentence before a "lainnya" names, when its own names none.

    SK Juli 2026: "Penurunan indeks terjadi pada kelompok pengeluaran Rp2,1-3 juta dan Rp3,1-4
    juta … Sementara itu, kelompok lainnya mengalami peningkatan indeks." — the rest fact was
    judged on all five groups, the two that fell among them, and refuted. Facts come in document
    order, so the sentence before is the last other quote ahead of this one's first fact."""
    quote = _normalize_quote(fact.context_quote)
    previous = None
    for other in facts:
        other_quote = _normalize_quote(other.context_quote)
        if other_quote == quote:
            break
        previous = other_quote
    if previous is None:
        return []
    return [p.metric_label for other in facts
            if _normalize_quote(other.context_quote) == previous and not other.group_scope
            for p in other.periods if QUAL_SEP in (p.metric_label or "")]


def _normalize_quote(quote: Optional[str]) -> str:
    return re.sub(r"\s+", " ", quote or "").strip().lower()


def _member_line(row: str, result: Optional[FactVerificationResult]) -> str:
    """'row: sesuai — Aug 2026=100.0, Sep 2026=101.0' — the numbers from the member's own
    trend/threshold reasoning ('… | Excel [src]: numbers | verdict'), without the source."""
    if result is None:
        return f"{row}: tidak ada data"
    parts = result.reasoning.split(" | ")
    numbers = re.sub(r"^Excel \[[^\]]*\]:\s*", "", parts[1]) if len(parts) >= 3 else result.reasoning
    return f"{row}: {VERDICT_TEXT.get(result.verdict, result.verdict)} — {numbers}"


_KECUALI_RE = re.compile(r"\bkecuali\b(?P<clause>[^.;]*)", re.IGNORECASE)


def _level_named_in(level: str, text: str) -> bool:
    """True when `text` names this group: every word and figure of it ('Cabang Barat',
    'Akademi/Diploma'), its own range ('kelompok Rp3,1-4 juta' for 'Pengeluaran Rp3,1 - 4
    juta'), or a range drawn around it ('kelompok usia 20-40 tahun' for 'Usia 31-40 th')."""
    words, figures = _label_tokens(level)
    text_words, text_figures = _label_tokens(text)
    if (words or figures) and words <= text_words and set(figures) <= set(text_figures):
        return True
    own = _GROUP_SPAN_RE.search(level)
    if own is None:
        return False
    kind, low, high = _span(own)
    if any(_span(m) == (kind, low, high) for m in _GROUP_SPAN_RE.finditer(text)):
        return True
    return any(k == kind and lo <= low and high <= hi
               for k, lo, hi in (_span(m) for m in _QUOTE_SPAN_RE.finditer(text)))


def _named_in_the_sentence(fact: ExtractedFact, family: List[str], pos: int) -> List[str]:
    """Rows the sentence itself sets apart from the quantified set: the groups after 'kecuali'
    for "all", every group it names before 'lainnya' for "rest". A backstop for the separate
    facts _attach_group_exclusions links — those can be missing, or carry a different quote."""
    quote = fact.context_quote or ""
    if fact.group_scope == "all":
        text = " ".join(m.group("clause") for m in _KECUALI_RE.finditer(quote))
    elif fact.group_scope == "rest":
        text = re.split(r"\blain(?:nya)?\b", quote, flags=re.IGNORECASE)[0]
    else:
        return []
    if not text.strip():
        return []
    return [row for row in family if _level_named_in(_levels(row)[pos], text)]


def _acronyms(text: str) -> set:
    return set(re.findall(r"\(([A-Z]{2,})\)", text))


def _single_rows_named(fact: ExtractedFact) -> Optional[List[str]]:
    """The listed rows, when they are whole series (no breakdown level) that the sentence names
    — "seluruh komponen, yaitu IPSI, IPDG dan IKLK" lists its members rather than quantifying
    over a breakdown. A row counts as named by all its words or by its abbreviation."""
    rows = list(dict.fromkeys(p.metric_label for p in fact.periods))
    if not rows or any(QUAL_SEP in row for row in rows):
        return None
    quote = fact.context_quote or ""
    quote_words = _sig_words(quote)
    for row in rows:
        if not (_sig_words(row) <= quote_words or _acronyms(row) & _acronyms(quote)):
            return None
    return rows


def _judge_rows(fact: ExtractedFact, rows: List[str], src: _ExcelSource):
    """Each row's own trend/threshold, computed on that row of `src`: [(row, result or None)]."""
    times = list(dict.fromkeys((p.year, p.month, p.col_label) for p in fact.periods))
    out = []
    for row in rows:
        sub = ExtractedFact(
            operation=fact.operation,
            periods=[PeriodPoint(row, y, m, c) for (y, m, c) in times],
            claimed_value=fact.claimed_value, unit=fact.unit,
            context_quote=fact.context_quote, page_number=fact.page_number,
        )
        resolved, missing, _ = _try_resolve(sub.periods, src)
        out.append((row, None if missing else _compute_operation(sub, resolved, 1.0, src)))
    return out


def _named_rows_result(fact: ExtractedFact, rows: List[str], src: _ExcelSource) -> FactVerificationResult:
    """Named groups/series, each judged on its own row: holds only if it holds for every one."""
    verdicts = _judge_rows(fact, rows, src)
    if any(r is not None and r.verdict == "Refuted" for _, r in verdicts):
        overall = "Refuted"
    elif all(r is not None and r.verdict == "Entailed" for _, r in verdicts):
        overall = "Entailed"
    else:
        overall = "Inconclusive"
    lines = " | ".join(_member_line(row, r) for row, r in verdicts)
    reasoning = f"Klaim untuk kelompok yang disebut kalimat, diperiksa per baris [{src.label}]. | {lines}"
    head = next((r for _, r in verdicts if r is not None and r.verdict == overall), None)
    if head is None:
        return _make_result(fact, [], src.label, None, None, None, None, None, overall, reasoning=reasoning)
    return head.model_copy(update={"verdict": overall, "metric_label": fact.display_label,
                                   "reasoning": reasoning})


def _bundled_siblings(fact: ExtractedFact, sources: List[_ExcelSource]):
    """(rows, source) when a trend lists several rows of ONE breakdown — named groups the
    extractor put in one fact ('naik di usia 20-30 dan 31-40 tahun') — else None."""
    first = fact.periods[0]
    labels = list(dict.fromkeys(p.metric_label for p in fact.periods))
    found_in = []
    for src in sources:
        if src.origin == "chart" or not src.table.row_labels:
            continue
        resolved, missing, _ = _try_resolve(
            [PeriodPoint(label, first.year, first.month, first.col_label) for label in labels], src
        )
        if missing:
            continue
        rows = list(dict.fromkeys(label for label, _ in resolved))
        found = _group_family(rows, src) if len(rows) == len(labels) else None
        if found and all(row in found[0] for row in rows):
            found_in.append((rows, src))
    if found_in:
        # SKDU repeats its sub-sectors in Tabel 1, 4, 5 and 7: "Perbaikan penggunaan tenaga kerja
        # didorong oleh beberapa sub-LU …" is Tabel 4's, whatever came first. A tie keeps order.
        cited = _cited_table(fact)
        return max(found_in, key=lambda found: (_is_cited(found[1], cited),
                                                _title_words_in_sentence(fact, found[1])))
    return _components_of_one_total(fact, labels, sources)


def _components_of_one_total(fact: ExtractedFact, labels: List[str], sources: List[_ExcelSource]):
    """(rows, source) when the named metrics are top-level components of ONE table's TOTAL.

    SKDU writes its sectors as the table's own rows, not as 'Series > Sector':
    "Meningkatnya kegiatan usaha tersebut didorong oleh kenaikan kinerja ... LU Pertanian, LU
    Konstruksi, ..." names four rows of Tabel 1 above a 'TOTAL' row, yet was refused as four
    unrelated metrics. The table has to be unambiguous — Tabel 1, 2, 4, 5, 6 and 7 all list
    the same sectors — so only a table whose title the sentence names counts, and only when
    exactly one does.
    """
    first = fact.periods[0]
    quote_words = _sig_words(fact.context_quote or "")
    picked = []
    for src in sources:
        if src.origin == "chart" or not src.table.row_labels:
            continue
        if not any(BITableData._is_aggregate(row) for row in src.table.row_labels):
            continue
        resolved, missing, _ = _try_resolve(
            [PeriodPoint(label, first.year, first.month, first.col_label) for label in labels], src
        )
        rows = list(dict.fromkeys(label for label, _ in resolved))
        if missing or len(rows) != len(labels) or any(QUAL_SEP in row for row in rows):
            continue
        subject = _sig_words(src.table.title or "") | _sig_words(" ".join(src.table.subject_words))
        if subject & quote_words:
            picked.append((rows, src))
    return picked[0] if len(picked) == 1 else None


def _group_level_index(family: List[str]) -> int:
    """Which level of a family's row names is the group (the one that varies)."""
    split = [_levels(row) for row in family]
    for i in range(len(split[0])):
        if len({level[i] for level in split}) > 1:
            return i
    return len(split[0]) - 1


# "meningkat pada sebagian besar kelompok pengeluaran", "mayoritas kota": a majority of a breakdown.
_MAJORITY_OF_GROUPS_RE = re.compile(
    r"\b(?:sebagian\s+besar|mayoritas)\s+(?:\w+\s+){0,2}?"
    r"(?:kelompok|kota|daerah|wilayah|provinsi|komponen|sektor|responden|sub-?LU|LU|lapangan\s+usaha)\b",
    re.IGNORECASE,
)


def _majority_the_sentence_states(fact: ExtractedFact) -> Optional[ExtractedFact]:
    """A claim on several groups that its sentence says holds for MOST of them, marked as such.

    SK Agustus 2026: "keyakinan konsumen … meningkat pada sebagian besar kelompok pengeluaran"
    came back with all five expenditure groups as one rising claim and no group_scope, so the one
    group that fell (Rp4,1-5 juta, 120,4 -> 118,9) refuted it, although four of five rose. The
    extractor is told to mark it 'majority' (structured_extractor rule 9); this is the backstop.
    """
    if fact.group_scope or fact.operation not in _TREND_OPS | _THRESHOLD_OPS:
        return None
    if len({(p.metric_label or "").strip().lower() for p in fact.periods}) < 2:
        return None
    if not _MAJORITY_OF_GROUPS_RE.search(fact.context_quote or ""):
        return None
    marked = copy.copy(fact)
    marked.group_scope = "majority"
    return marked


def _judge_group_scope(fact: ExtractedFact, sources: List[_ExcelSource]) -> FactVerificationResult:
    return _judge_group_scope_members(fact, sources).model_copy(update={"group_scope": fact.group_scope})


def _judge_group_scope_members(
    fact: ExtractedFact, sources: List[_ExcelSource]
) -> FactVerificationResult:
    """Check a quantified group claim on every group of its breakdown, read off the table.

    The extractor's list of groups is only a pointer to the breakdown; the members are the
    table's sibling rows (see _group_family), so a group the model forgot still counts. Each
    member is computed directly on its own row in the same table — the subset guard in
    _evaluate_fact exists for claims nobody resolved into groups, which this one now is.
      all / rest : holds when it holds for every member; one counter-example refutes it.
      majority   : holds for more than half of the members; refuted when it cannot.
    """
    first = fact.periods[0]
    best = None
    for src in sources:
        if src.origin == "chart" or not src.table.row_labels:
            continue
        named = [_try_resolve([PeriodPoint(p.metric_label, first.year, first.month, first.col_label)], src)[0]
                 for p in fact.periods]
        named_rows = list(dict.fromkeys(r[0][0] for r in named if r))
        excluded = _try_resolve(
            [PeriodPoint(label, first.year, first.month, first.col_label) for label in fact.group_exclude],
            src,
        )[0]
        excluded_rows = list(dict.fromkeys(label for label, _ in excluded))
        found = _group_family(named_rows + excluded_rows, src)
        if found is None:
            continue
        rank = (_is_cited(src, _cited_table(fact)), len(named_rows))
        if best is None or rank > best[0]:
            best = (rank, src, found, excluded_rows)
    if best is None:
        # A table that is one breakdown with no parent row (PMI's T2 sub-sectors), named by the
        # sentence ("sebagian besar Sub-LU") or by two or more of its rows in the claim (PMI Tw
        # III-2026: the quote was only "berada pada fase ekspansi", the rows all 14 sub-sectors).
        for src in sources:
            group = getattr(src.table, "whole_table_group", None)
            if src.origin == "chart" or not group:
                continue
            in_table = {r[0][0] for r in (
                _try_resolve([PeriodPoint(p.metric_label, first.year, first.month, first.col_label)], src)[0]
                for p in fact.periods) if r}
            if len(in_table) < 2 and not re.search(group[0], fact.context_quote or "", re.IGNORECASE):
                continue
            family = [row for row in src.table.row_labels if not _AGGREGATE_GROUP_RE.match(row)]
            excluded = _try_resolve(
                [PeriodPoint(label, first.year, first.month, first.col_label) for label in fact.group_exclude],
                src,
            )[0]
            best = (0, src, (family, group[1]), list(dict.fromkeys(label for label, _ in excluded)))
            break
    if best is None:
        from_sentence = _family_from_sentence(fact, sources)
        if from_sentence:
            family, series, src = from_sentence
            excluded = _try_resolve(
                [PeriodPoint(label, first.year, first.month, first.col_label) for label in fact.group_exclude],
                src,
            )[0]
            best = (0, src, (family, series), list(dict.fromkeys(label for label, _ in excluded)))
    if best is None:
        named = _single_rows_named(fact)
        for src in sources if named else []:
            if src.origin != "chart" and not _try_resolve(
                [PeriodPoint(row, first.year, first.month, first.col_label) for row in named], src
            )[1]:
                return _named_rows_result(fact, named, src)
    if best is None:
        return _make_result(
            fact, [], None, None, None, None, None, None, "Inconclusive", reason_code="tidak_ditemukan",
            reasoning=(
                f"Klaim berlaku untuk {_SCOPE_TEXT[fact.group_scope]}, tetapi kelompoknya tidak "
                "bisa dipastikan dari tabel (perlu minimal dua baris kelompok yang sama)."
            ),
        )
    _, src, (family, series), excluded_rows = best
    excluded_rows = list(dict.fromkeys(
        excluded_rows + _named_in_the_sentence(fact, family, _group_level_index(family))
    ))
    members = [row for row in family if row not in excluded_rows]
    if not members:
        return _make_result(
            fact, [], src.label, None, None, None, None, None, "Inconclusive", reason_code="klaim_tidak_lengkap",
            reasoning=f"Tidak ada kelompok '{series}' tersisa setelah kelompok yang disebut terpisah.",
        )

    verdicts = _judge_rows(fact, members, src)

    n = len(verdicts)
    ok = sum(1 for _, r in verdicts if r is not None and r.verdict == "Entailed")
    bad = sum(1 for _, r in verdicts if r is not None and r.verdict == "Refuted")
    if fact.group_scope == "majority":
        overall = ("Entailed" if 2 * ok > n else "Refuted" if 2 * (n - bad) <= n else "Inconclusive")
    else:
        overall = "Refuted" if bad else "Entailed" if ok == n else "Inconclusive"

    scope = _SCOPE_TEXT[fact.group_scope]
    if fact.group_scope == "all" and excluded_rows:
        scope += " kecuali " + ", ".join(_levels(r)[-1] if _levels(r)[-1] not in series else r
                                         for r in excluded_rows)
    lines = " | ".join(_member_line(row, r) for row, r in verdicts)
    tally = f"{ok} dari {n} kelompok sesuai"
    head = next((r for _, r in verdicts if r is not None and r.verdict == overall), None) or next(
        (r for _, r in verdicts if r is not None), None
    )
    reasoning = (f"Klaim untuk {scope} '{series}' — kelompok diambil dari tabel [{src.label}]; "
                 f"{tally}. | {lines}")
    if head is None:
        return _make_result(fact, [], src.label, None, None, None, None, None, overall, reasoning=reasoning)
    return head.model_copy(update={
        "verdict": overall,
        "metric_label": f"{series} ({scope})",
        "reasoning": reasoning,
        "computed_value": None,
        "delta": None,
    })


# Words of a combined-share numerator that join or frame the rows rather than name one.
_SHARE_FRAME_WORDS = frozenset("sektor serta dan atau pangsa total posisi terutama berasal dari".split())


def _row_words(text: str) -> set:
    return _sig_words(text.replace("&", " dan ")) - _SHARE_FRAME_WORDS


def _rows_named_in(text: str, src: _ExcelSource, exclude: str) -> Optional[List[str]]:
    """The rows of `src` that one numerator label names together — 'Sektor Industri Pengolahan,
    Jasa Keuangan dan Asuransi, serta Pertambangan dan Penggalian' — or None unless at least two
    are named AND every word of the label is accounted for by them, the table's title or the
    denominator. A word left over is a row the table lacks, and summing without it would
    understate the share."""
    wanted = _row_words(text)
    found = []
    for row in src.table.row_labels:
        words = _row_words(row)
        if row != exclude and words and words <= wanted:
            found.append((row, words))
    # A row whose words are all part of another named row is that row's fragment, not a sector.
    found = [(r, w) for r, w in found if not any(w < other for _, other in found)]
    covered = set().union(*(w for _, w in found)) if found else set()
    leftover = wanted - covered - _row_words(src.table.title or "") - _row_words(exclude)
    if len(found) < 2 or leftover:
        return None
    return [r for r, _ in found]


def _combined_share(fact: ExtractedFact, sources: List[_ExcelSource]) -> Optional[FactVerificationResult]:
    """A share held by several rows together, judged as their sum over the last point (B3).

    Either the extractor listed one numerator point per row (3+ points) or one numerator label
    names the rows (see _rows_named_in). None when the claim is an ordinary two-point ratio, or a
    ratio the extractor gave as ONE point (SULNI Okt 2026: 'Rasio Utang terhadap PDB' alone, a row
    holding the ratio itself) — that has no numerator to split, and indexing one crashed.
    """
    if fact.claimed_value is None or len(fact.periods) < 2:
        return None
    denominator = fact.periods[-1]
    numerators = fact.periods[:-1]
    # The same sector rows can sit in a table of ALL debt and in one of private debt; the
    # sentence's own words ('ULN swasta') say which table it is about. Best title fit first,
    # upload order on a tie.
    claim_words = _sig_words(" ".join([fact.context_quote or ""] + [p.metric_label for p in fact.periods]))
    sources = sorted(sources, key=lambda src: -len(_sig_words(src.table.title or "") & claim_words))
    for src in sources:
        if src.origin == "chart" or not src.table.row_labels:
            continue
        den = _try_resolve([denominator], src)[0]
        if not den:
            continue
        den_label, den_value = den[0]
        if len(numerators) >= 2:
            resolved, missing, _ = _try_resolve(numerators, src)
            if missing:
                continue
            # Rows of ONE group the sentence never names are not the sectors' own: SULNI Tbl III.6
            # files them under 'Perusahaan Induk', and "Sektor Industri Pengolahan, Jasa Keuangan
            # … pangsa 80,5% dari total ULN swasta" summed the parent companies' 62,4%.
            probe = copy.copy(fact)
            probe.periods = list(numerators)
            if _answers_one_group_of_it(probe, resolved, src.table):
                continue
            rows = [label for label, _ in resolved]
        else:
            # Several rows that account for every word of the label beat a looser match of the
            # whole label to one of them ('Sektor Industri Kayu, serta Pertanian' also contains
            # the row 'Industri Kayu'); only without such a split is it an ordinary ratio.
            rows = _rows_named_in(numerators[0].metric_label, src, den_label)
            if not rows:
                if _try_resolve(numerators, src)[0]:
                    return None
                continue
            resolved = _try_resolve([PeriodPoint(r, denominator.year, denominator.month, denominator.col_label)
                                     for r in rows], src)[0]
            if len(resolved) != len(rows):
                continue
        if den_value == 0 or len(set(rows)) != len(rows):
            continue
        total = sum(value for _, value in resolved)
        computed = round(total / den_value * 100, 4)
        delta, verdict = _numeric_verdict(fact.claimed_value, computed)
        periods = [PeriodResult(metric_label=label, year=denominator.year, month=denominator.month,
                                excel_value=round(value, 4)) for label, value in resolved + [den[0]]]
        parts = " + ".join(f"{label}={round(value, 4)}" for label, value in resolved)
        return _make_result(
            fact, periods, src.label, fact.claimed_value, fact.unit, computed, fact.unit, delta, verdict,
            reasoning=(
                f"PDF: pangsa gabungan {fact.claimed_value} {fact.unit} | Excel [{src.label}]: "
                f"({parts}) / {den_label}={round(den_value, 4)} = {computed} persen | "
                f"Δ = {delta} → {'within' if verdict == 'Entailed' else 'exceeds'} tolerance {MATCH_TOLERANCE}"
            ),
        )
    return None


# B4: a superlative over a CHANGE ("peningkatan tertinggi di A, B dan C") ranks the named groups.
_SUPERLATIVE_RE = re.compile(r"\b(?:tertinggi|terbesar|terdalam|terkuat|paling\s+\w+)\b", re.IGNORECASE)
_RISE_WORD_RE = re.compile(r"\b(?:peningkatan|kenaikan|meningkat|naik|menguat|penguatan)\b", re.IGNORECASE)
_FALL_WORD_RE = re.compile(r"\b(?:penurunan|menurun|turun|melemah|pelemahan)\b", re.IGNORECASE)
_CLAUSE_SPLIT_RE = re.compile(r"\s*(?:;|\bsementara(?:\s+itu)?\b|\bsedangkan\b|\bnamun\b)\s*",
                              re.IGNORECASE)


def _attach_rankings(facts: List[ExtractedFact]) -> None:
    """Link the named-group trend facts one superlative ranks together.

    "IKE mengalami penurunan terbesar di Mataram, Bandung, dan Ambon, sementara peningkatan
    tertinggi di Pontianak, Medan, dan Makassar" says more than that each city fell or rose:
    those three are the largest moves. A clause (split at 'sementara' / 'sedangkan' / ';', not
    at the commas between names) counts when it has a superlative AND the fact's own direction
    word — 'indeks tertinggi di X' ranks a level, not a change, and is left alone.
    """
    for fact in facts:
        fact.rank_group = []
    clusters: Dict[Tuple[str, str, int], List[ExtractedFact]] = {}
    for fact in facts:
        labels = {p.metric_label for p in fact.periods}
        if (fact.group_scope or fact.operation not in ("is_increasing", "is_decreasing")
                or len(labels) != 1 or QUAL_SEP not in next(iter(labels))):
            continue
        label = next(iter(labels))
        direction = _RISE_WORD_RE if fact.operation == "is_increasing" else _FALL_WORD_RE
        for i, clause in enumerate(_CLAUSE_SPLIT_RE.split(fact.context_quote or "")):
            if any(_level_named_in(level, clause) for level in _levels(label)[1:]) or (
                    _level_named_in(_levels(label)[0], clause) and len(_levels(label)) > 1
                    and not _level_named_in(_levels(label)[-1], clause)):
                if _SUPERLATIVE_RE.search(clause) and direction.search(clause):
                    key = (_normalize_quote(fact.context_quote), fact.operation, i)
                    clusters.setdefault(key, []).append((fact, clause))
                break
    for members in clusters.values():
        labels = list(dict.fromkeys(f.periods[0].metric_label for f, _ in members))
        for fact, clause in members:
            fact.rank_group = labels
            fact.rank_clause = clause


def _check_ranking(fact: ExtractedFact, result: FactVerificationResult,
                   sources: List[_ExcelSource]) -> FactVerificationResult:
    """A direction that held is also checked as a ranking: the named groups must be the
    largest moves of their breakdown — in points or in percent, either reading (a report may
    rank by either, and refuting on the one it did not use would be a false Tidak Sesuai)."""
    src = next((s for s in sources if s.label == result.matched_excel_source), None)
    if result.verdict != "Entailed" or src is None:
        return result
    point = max(fact.periods, key=lambda p: (p.year or 0, _period_ordinal(p.month or "")))
    named = _try_resolve([PeriodPoint(l, point.year, point.month) for l in fact.rank_group], src)[0]
    rows = list(dict.fromkeys(label for label, _ in named))
    found = _group_family(rows, src) if len(rows) >= 2 else None
    if found is None and len(rows) == 1:
        own = _try_resolve([PeriodPoint(fact.periods[0].metric_label, point.year, point.month)], src)[0]
        siblings = _bundled_family_of(own[0][0], src) if own else None
        found = (siblings, "") if siblings else None
    if found is None or len(found[0]) <= len(rows):
        return result
    # How many the sentence ranks is what it NAMES, not how many facts the extractor made: a
    # city whose fact went missing still takes one of the top places.
    pos = _group_level_index(found[0])
    named_in_clause = [r for r in found[0] if _level_named_in(_levels(r)[pos], fact.rank_clause or "")]
    k_named = max(len(rows), len(named_in_clause))
    if k_named >= len(found[0]):
        return result
    sign = 1 if fact.operation == "is_increasing" else -1
    moves = {}
    for row in found[0]:
        now = src.table.lookup(row, point.year, point.month)
        before = _previous_period(src.table, row, point.year, point.month)
        if now is None or before is None or before[1] is None:
            continue
        moves[row] = (sign * (now - before[1]), sign * (now - before[1]) / abs(before[1]) if before[1] else None)
    own = _try_resolve([PeriodPoint(fact.periods[0].metric_label, point.year, point.month)], src)[0]
    own_row = own[0][0] if own else None
    if own_row not in moves or any(row not in moves for row in rows):
        return result
    k = k_named

    def top(index):
        ranked = sorted((m[index] for m in moves.values() if m[index] is not None), reverse=True)
        if len(ranked) < k:
            return None
        bar = ranked[k - 1] - 1e-9
        return {row for row, m in moves.items() if m[index] is not None and m[index] >= bar}

    for index in (0, 1):
        leaders = top(index)
        if leaders is not None and own_row in leaders:
            return result
    by_points = sorted(moves, key=lambda r: -moves[r][0])[:k]
    word = "kenaikan" if sign > 0 else "penurunan"
    return result.model_copy(update={
        "verdict": "Refuted",
        "reasoning": (
            f"{result.reasoning} | Kalimat menyebut {own_row} di antara {k} {word} terbesar, "
            f"tetapi {k} {word} terbesar di [{src.label}] adalah {', '.join(by_points)}."
        ),
    })


def _bundled_family_of(row: str, src: _ExcelSource) -> Optional[List[str]]:
    """The breakdown one row belongs to, when its first level is the group (city tables
    'Kota > Indeks'). Used only when a superlative names a single group."""
    levels = _levels(row)
    if len(levels) != 2:
        return None
    family = [r for r in src.table.row_labels
              if len(_levels(r)) == 2 and _levels(r)[1] == levels[1]
              and not _AGGREGATE_GROUP_RE.match(_levels(r)[0])]
    return family if len(family) > 2 else None


_QUARTER_OF_END_MONTH = {"Mar": "Q1", "Jun": "Q2", "Sep": "Q3", "Dec": "Q4"}
_MONTH_NAME_ID = {"Mar": "Maret", "Jun": "Juni", "Sep": "September", "Dec": "Desember"}


def _quarter_end_position(fact: ExtractedFact, sources: List[_ExcelSource]) -> Optional[ExtractedFact]:
    """A position "pada akhir Juni" read from a table that holds it only per quarter: Q2.

    NPI: "Posisi cadangan devisa pada akhir Juni 2026 … 5,4 bulan impor dan pembayaran ULN
    pemerintah" against a quarterly row (Q2 2026 = 5,41) was left Tidak Cukup Data. Only for a
    sentence that says "akhir <bulan>" for a quarter-end month (or "akhir <tahun>" for December),
    and only when the matched row has no monthly figures at all — a monthly table still answers
    with its own June.

    A table of positions (TableData.positions, PII) needs no such words: "Untuk keseluruhan tahun
    2025, kewajiban neto PII naik 26,9 miliar dolar AS (11,0% yoy) dibandingkan akhir 2024" came
    back Dec 2024 → Dec 2025, and every full-year claim of the Tw IV-2025 report was left
    Tidak Cukup Data although the Q4 columns hold them.
    """
    quote = fact.context_quote or ""
    points = fact.periods
    if not points or any(p.col_label or p.month not in _QUARTER_OF_END_MONTH for p in points):
        return None
    # One end named is enough: "naik 0,8 miliar dolar AS dari akhir tahun 2024" dates only the start.
    said_end = any(re.search(r"\bakhir\s+(?:" + _MONTH_NAME_ID[p.month]
                             + (r"|(?:tahun\s+)?" + str(p.year) if p.month == "Dec" else "")
                             + r")\b", quote, re.IGNORECASE)
                   for p in points)
    for src in sources:
        if src.table.axis_type != "temporal" or not (said_end or src.table.positions):
            continue
        periods = [src.table.available_periods(p.metric_label) for p in points]
        if all(ps and all(m.startswith("Q") for _, m in ps) for ps in periods):
            moved = copy.copy(fact)
            moved.periods = [PeriodPoint(p.metric_label, p.year, _QUARTER_OF_END_MONTH[p.month])
                             for p in points]
            return moved
    return None


_END_MONTH_OF_QUARTER = {quarter: month for month, quarter in _QUARTER_OF_END_MONTH.items()}
# A quantity accumulated over the quarter, which its last month does not hold.
_OVER_THE_QUARTER = re.compile(
    r"\b(selama|sepanjang|rata-rata|rerata|kumulatif|total)\s+(?:\w+\s+){0,2}?triwulan\b", re.IGNORECASE)


def _quarter_as_end_month(fact: ExtractedFact, sources: List[_ExcelSource]) -> Optional[ExtractedFact]:
    """A position "pada triwulan II" read from tables that hold it only per month: June.

    SULNI Agustus 2026, a quarterly release: "Posisi ULN Indonesia pada triwulan II 2026 tercatat
    sebesar 453,4 miliar dolar AS" against monthly tables came back Q2 2026, which no sheet has, and
    every claim of the release was left Tidak Cukup Data. A position at a quarter is the position at
    its last month. Only when no source has the quarter for the metric (a quarterly table answers
    the quarter itself), the metric's row is monthly, and the sentence does not speak of a total or
    an average over the quarter ("selama triwulan II"), which the last month does not hold.
    """
    points = fact.periods
    if (not points or fact.operation in ("sum", "average")
            or not any(p.col_label is None and p.month in _END_MONTH_OF_QUARTER for p in points)
            or _OVER_THE_QUARTER.search(fact.context_quote or "")):
        return None
    temporal = [src for src in sources if src.table.axis_type == "temporal"]
    moved = []
    for p in points:
        if p.col_label is not None or p.month not in _END_MONTH_OF_QUARTER:
            moved.append(p)
            continue
        if any(src.table.lookup_fuzzy(p.metric_label, p.year, p.month)[1] is not None for src in temporal):
            return None
        month = _END_MONTH_OF_QUARTER[p.month]
        if not any(src.table.lookup_fuzzy(p.metric_label, p.year, month)[1] is not None for src in temporal):
            return None
        moved.append(PeriodPoint(p.metric_label, p.year, month))
    # A one-quarter trend ("peningkatan ULN publik" in a triwulan II release) compares with the
    # quarter before: its last month, not the month before June that _previous_period would pick.
    if fact.operation in _TREND_OPS and len(points) == 1 and moved[0] is not points[0]:
        quarter = int(points[0].month[1])
        year, prior = (points[0].year, quarter - 1) if quarter > 1 else (points[0].year - 1, 4)
        prior_month = _END_MONTH_OF_QUARTER[f"Q{prior}"]
        if any(src.table.lookup_fuzzy(points[0].metric_label, year, prior_month)[1] is not None
               for src in temporal):
            moved.insert(0, PeriodPoint(points[0].metric_label, year, prior_month))
    out = copy.copy(fact)
    out.periods = moved
    return out


# "kontraksi pertumbuhan ULN swasta yang lebih rendah": a negative growth rate that got smaller
# (eased) or bigger (deepened) — about the rate, not the level.
_CONTRACTION_OF_GROWTH = re.compile(
    r"\bkontraksi\s+(?P<span>(?:pertumbuhan\s+)?(?:\S+\s+){0,4}?)(?:yang\s+)?"
    r"(?P<how>lebih\s+(?:rendah|kecil|terbatas|dalam|besar|tinggi)|semakin\s+dalam|mereda|tertahan)\b",
    re.IGNORECASE)


def _contraction_of_growth(fact: ExtractedFact, sources: List[_ExcelSource]) -> Optional[FactVerificationResult]:
    """A trend on a contraction of GROWTH, judged on the yoy rate at both ends.

    SULNI Agustus 2026: "peningkatan ULN publik … di tengah kontraksi pertumbuhan ULN swasta yang
    lebih rendah" came back is_decreasing on the private debt level (192,5 → 194,6 thousand juta)
    and was refuted. The sentence says the yoy rate is still negative but smaller: −1,35% in Q1,
    −0,57% in Q2. Only when the contraction names the claim's own row and the claim has both ends.
    """
    match = _CONTRACTION_OF_GROWTH.search(fact.context_quote or "")
    if match is None or len(fact.periods) != 2 or any(p.col_label or p.year is None for p in fact.periods):
        return None
    leaf = fact.periods[-1].metric_label.rsplit(QUAL_SEP, 1)[-1]
    if not _sig_words(leaf) & _sig_words(match.group("span")):
        return None
    deepened = re.search(r"dalam|besar|tinggi", match.group("how"), re.IGNORECASE) is not None
    for src in sources:
        if src.table.axis_type != "temporal":
            continue
        rates, labels = [], []
        for p in fact.periods:
            label, now = src.table.lookup_fuzzy(p.metric_label, p.year, p.month)
            _, before = src.table.lookup_fuzzy(p.metric_label, p.year - 1, p.month)
            if now is None or not before:
                break
            rates.append(round((now / before - 1) * 100, 2))
            labels.append(label)
        if len(rates) != 2:
            continue
        holds = rates[-1] < 0 and (abs(rates[-1]) > abs(rates[0]) if deepened else abs(rates[-1]) < abs(rates[0]))
        periods = [PeriodResult(metric_label=labels[i], year=p.year, month=p.month, col_label=None,
                                excel_value=rates[i]) for i, p in enumerate(fact.periods)]
        return _make_result(
            fact, periods, src.label, None, None, rates[-1], "persen_yoy", None,
            "Entailed" if holds else "Refuted",
            reasoning=(
                f"Kalimat menyebut kontraksi pertumbuhan '{leaf}' yang {match.group('how').lower()}: "
                f"dinilai pada pertumbuhan yoy, bukan level | Excel [{src.label}]: "
                f"{_point_desc(fact.periods[0])} {rates[0]}% → {_point_desc(fact.periods[-1])} {rates[-1]}% (yoy)"
            ),
        )
    return None


def _only_quarterly_reason(missing: List[PeriodPoint], sources: List[_ExcelSource]) -> Optional[str]:
    """Why a MONTHLY point found nothing, when a source holds that series only per quarter (B5).

    "Rasio ULN terhadap PDB 30,7% pada Juli 2026" against TabI.7, whose ratio row is quarterly
    (Q2-2026 = 30,63): "data tidak ditemukan" sent the reader looking for a row that is there.
    """
    for point in missing:
        if point.year is None or not point.month or point.month.startswith("Q"):
            continue
        for src in sources:
            if src.table.axis_type != "temporal" or not src.table.row_labels:
                continue
            periods = src.table.available_periods(point.metric_label)
            if periods and all(month.startswith("Q") for _, month in periods):
                year, quarter = max(periods, key=lambda p: (p[0], _period_ordinal(p[1])))
                row = src.table._resolve_label(point.metric_label)
                return _why("periode", (
                    f"Tabel [{src.label}] memuat '{row}' hanya per triwulan (terakhir {quarter} "
                    f"{year}), sedangkan klaim menyebut {point.month} {point.year}; angka bulanan "
                    "tidak tersedia untuk dibandingkan."
                ))
    return None


def _evaluate_fact(fact: ExtractedFact, sources: List[_ExcelSource]) -> FactVerificationResult:
    # A time-only operation over categorical data points can never be computed — fail fast
    # with an explanation instead of scanning sources for data that cannot qualify.
    if fact.operation in _TEMPORAL_ONLY_OPS and any(p.col_label is not None for p in fact.periods):
        return _make_result(
            fact, [], None, fact.claimed_value, fact.unit, None, fact.unit, None, "Inconclusive", reason_code="jenis",
            reasoning=(
                f"Operasi '{fact.operation}' memerlukan data deret waktu (tahun/bulan), "
                f"tetapi klaim ini merujuk atribut non-waktu ('{fact.display_label}')."
            ),
        )

    # "semester II 2026" the extractor wrote as Q2 2026, against a table of semesters.
    fact = _semester_periods(fact, sources) or fact
    # "dengan SBT sebesar 0,07%" the extractor wrote as a yoy growth of 0,07%.
    fact = _balance_read_as_growth(fact) or fact
    # "posisi ULN pada triwulan II 2026" against tables that hold it only per month: June.
    fact = _quarter_as_end_month(fact, sources) or fact

    # A superlative ranks the named groups as well as giving their direction (B4).
    if fact.rank_group:
        plain = ExtractedFact(fact.operation, fact.periods, fact.claimed_value, fact.unit,
                              fact.context_quote, fact.page_number, group_scope=fact.group_scope)
        return _check_ranking(fact, _evaluate_fact(plain, sources), sources)

    # A share of several rows together ("pangsa sektor A, B dan C") — see _combined_share.
    if fact.operation == "ratio" and fact.periods:
        combined = _combined_share(fact, sources)
        if combined is not None:
            return combined

    if fact.operation in _TREND_OPS:
        eased = _contraction_of_growth(fact, sources)
        if eased is not None:
            return eased

    # A quantified group claim ("seluruh", "sebagian besar", "lainnya") the extractor marked as
    # such is judged on every group of its breakdown — see _judge_group_scope.
    fact = _net_position_threshold(fact, sources) or _majority_the_sentence_states(fact) or fact
    if fact.group_scope and fact.operation in _TREND_OPS | _THRESHOLD_OPS:
        return _judge_group_scope(fact, sources)

    # A claim attributed to an unnamed subset cannot be settled by reading any single row.
    # Thresholds need this as much as trends do: "IPDG berada pada level optimis pada sebagian
    # besar kelompok pengeluaran" was pinned to the one expenditure group of five that sat below
    # 100 and Refuted — for being exactly the exception the sentence allows for. Only reject when
    # NO data point names its own member, so the per-member facts split out of the same sentence
    # still verify normally (see _SUBSET_PHRASE_RE and _quote_names_qualifier).
    if fact.operation in _TREND_OPS | _THRESHOLD_OPS:
        quote = fact.context_quote or ""
        if _SUBSET_PHRASE_RE.search(_without_own_names(quote, fact)) and not any(
            _quote_names_qualifier(p.metric_label, quote) or _member_named(p.metric_label, quote)
            for p in fact.periods
        ) and not _all_net_rows(fact, sources):
            return _make_result(
                fact, [], None, None, None, None, None, None, "Inconclusive", reason_code="klaim_tidak_lengkap",
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
        # Several rows of ONE breakdown are named groups bundled into one fact: each group's
        # own trend is the claim. Unrelated series stay refused below.
        bundled = _bundled_siblings(fact, sources) if len(distinct_metrics) > 1 else None
        if bundled:
            return _named_rows_result(fact, *bundled)
        if len(distinct_metrics) > 1:
            return _make_result(
                fact, [], None, None, None, None, None, None, "Inconclusive", reason_code="klaim_tidak_lengkap",
                reasoning=(
                    f"Klaim tren '{fact.operation}' harus mengikuti satu metrik, tetapi klaim ini "
                    f"mencakup {len(distinct_metrics)} metrik berbeda — seharusnya dipecah menjadi "
                    "satu tren per metrik."
                ),
            )

    joined = _joined_rows_as_sum(fact, sources)
    if joined is not None:
        fact = joined
    fact = _quarter_end_position(fact, sources) or fact

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
        resolved, missing, col_units = _in_the_claims_band(fact, src) or _try_resolve(fact.periods, src)
        if missing:
            if best_missing is None or len(missing) < len(best_missing):
                best_missing = missing
            continue
        resolved = _balance_row_instead(fact, resolved, src) or resolved
        resolved = _net_row_instead(fact, resolved, src) or resolved
        other_kind = (
            _not_a_yoy_cell(fact, resolved[0][0], src) if fact.operation == "yoy_growth" else None
        )
        if other_kind:
            if best_reason is None:
                best_reason = _why("jenis", (
                    f"Excel [{src.label}] ({resolved[0][0]}) berisi pertumbuhan {other_kind} "
                    f"('{src.table.unit}'), bukan yoy — tidak menjawab klaim yoy ini."
                ))
            continue
        if not _can_answer(fact, resolved, src):
            if best_reason is None:
                point = fact.periods[0]
                best_reason = _why("periode", (
                    f"No data found in Excel [{src.label}] for metric '{resolved[0][0]}' "
                    f"at {point.month} {point.year - 1} (needed for yoy denominator)."
                ))
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
            else:
                row_unit = _row_unit([label for label, _ in resolved], src.table)
                if row_unit and "/" in row_unit and "/" not in (fact.unit or ""):
                    # A price row ('USD/barel') cannot answer an amount ('0,1 miliar dolar AS').
                    if best_reason is None:
                        best_reason = _why("jenis", (
                            f"Baris '{resolved[0][0]}' di [{src.label}] bersatuan '{row_unit}', "
                            f"tidak bisa menjawab klaim dalam '{fact.unit}'."
                        ))
                    continue
                excel_unit = row_unit or excel_unit
            # A '%, yoy' table cannot answer "berapa triliun Rp". Without this the fallback
            # below normalises the CLAIM's scale only, so Tabel 1's growth half answered a
            # level claim about uang kartal with 15,7 / 1e12 = 0,0 triliun Rp and reported it
            # as a Refuted second opinion. Gated on the claim carrying a real numeric scale,
            # so a dimensionless 'persen'/index claim still reaches the fallback (the PMI case
            # that branch documents).
            if _is_growth_series(excel_unit) and _parse_scale_unit(fact.unit) is not None:
                if best_reason is None:
                    best_reason = _why("jenis", (
                        f"Sumber [{src.label}] bersatuan '{excel_unit}' (pertumbuhan), "
                        f"tidak bisa menjawab klaim nilai dalam '{fact.unit}'."
                    ))
                continue
            # The mirror case: an index sheet holds LEVELS, so a percent claim ('harga rumah
            # tipe menengah tumbuh 0,40% (qtq)') compared with them reads 0,40 against 114,01.
            # The dimensionless fallback below exists for PMI, whose sheet says '(%, Indeks)'
            # and whose report prints the index itself as '52,03%' — so only an index without
            # a % is turned away.
            if _is_percent_unit(fact.unit) and _is_plain_index(excel_unit):
                if best_reason is None:
                    best_reason = _why("jenis", (
                        f"Sumber [{src.label}] berisi level indeks ('{excel_unit}'); klaim dalam "
                        f"persen (pertumbuhan) tidak bisa dibandingkan langsung dengan level."
                    ))
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
                    best_reason = _why("jenis", (
                        f"Unit conversion from '{fact.unit}' to '{excel_unit}' "
                        f"([{src.label}]) is not supported. Cannot compare."
                    ))
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
                best_reason = _why("tidak_ditemukan", (
                    f"Sumber [{src.label}] membahas pokok lain dari kalimat klaim "
                    f"('{fact.display_label}' saja tidak menyebut pokoknya)."
                ))
            continue
        if _answers_one_group_of_it(fact, resolved, src.table):
            if best_reason is None:
                best_reason = _why("di_luar_tabel", (
                    f"Sumber [{src.label}] hanya memuat rinciannya per kelompok "
                    f"('{resolved[0][0]}'), bukan angka '{fact.display_label}' itu sendiri."
                ))
            continue
        # A rate the sentence marks '(mtm)' is not answered by a '%, yoy' cell, and vice versa:
        # SPE Tabel 2 (yoy) answered "terkontraksi sebesar 0,1% (mtm)" with 1,1.
        if fact.operation == "value" and _is_percent_unit(fact.unit):
            claim_kind = _claimed_rate_kind(fact)
            cell_kind = _growth_kind(src.table.unit, resolved[0][0])
            if claim_kind and cell_kind not in (None, "mixed", claim_kind):
                if best_reason is None:
                    best_reason = _why("jenis", (
                        f"Excel [{src.label}] ({resolved[0][0]}) berisi pertumbuhan {cell_kind} "
                        f"('{src.table.unit}'), sedangkan klaim menyebut {claim_kind}."
                    ))
                continue
        gross = _gross_row_for_a_net_number(fact, resolved)
        if gross:
            if best_reason is None:
                best_reason = _why("di_luar_tabel", (
                    f"Klaim menyebut posisi neto, sedangkan baris '{gross}' di [{src.label}] "
                    f"hanya memuat satu sisi (aset atau kewajiban) secara bruto."
                ))
            continue
        absent = _breakdown_the_table_lacks(fact, resolved, src)
        if absent:
            if best_reason is None:
                best_reason = _why("di_luar_tabel", (
                    f"Sumber [{src.label}] tidak memuat '{', '.join(absent)}' — "
                    f"rincian '{fact.display_label}' tidak ada di tabel ini."
                ))
            continue
        coverage = _coverage_score(fact.periods, resolved, src)
        if coverage <= 0.0:
            # Nothing this source names has anything to do with what the claim asked about —
            # see TableData.query_coverage. Answering anyway is the confident-wrong-number case.
            if best_reason is None:
                best_reason = _why("tidak_ditemukan", (
                    f"Sumber [{src.label}] tidak membahas '{fact.display_label}'."
                ))
            continue
        candidates.append(_Candidate(
            score=_resolution_score(fact.periods, resolved),
            coverage=coverage,
            direct_coverage=_coverage_score(fact.periods, resolved, src, broken_out=False),
            sentence_title=_title_words_in_sentence(fact, src),
            qualification=_qualification_rank(fact.periods, resolved),
            src=src, resolved=resolved, factor=factor, unit_comparable=unit_comparable,
        ))

    if not candidates:
        if best_reason is None and best_missing:
            best_reason = _only_quarterly_reason(best_missing, sources)
        return _inconclusive_result(fact, best_missing, best_reason)

    # Coverage first (a tie goes to the source whose title and matched rows carry the claim's
    # words themselves), then label quality. Stable sort, so ties keep source order as before.
    candidates.sort(key=_rank_key)
    # The table the sentence itself cites goes first: SKDU's "seluruh LU terindikasi melambat
    # … (Lampiran Tabel 8)" is the wages table, though Tabel 7b, 9 and 10 have the same sectors
    # and semesters. Only when that table could answer at all.
    cited = _cited_table(fact)
    if cited and any(_is_cited(c.src, cited) for c in candidates):
        candidates.sort(key=lambda c: not _is_cited(c.src, cited))
    elif cited and any(_is_cited(src, cited) for src in sources):
        # The cited table is there but cannot answer: another table's same-named row is not what
        # the author pointed at. SKDU Tw III-2026: "LU Industri Pengolahan (SB 9,18%) … (Lampiran
        # Tabel 8)" came back for Q3, which the semester-only wages table lacks, and Tabel 1's
        # activity balance (1,53) refuted it.
        named = next(src for src in sources if _is_cited(src, cited))
        return _make_result(
            fact, [], None, fact.claimed_value, fact.unit, None, None, None, "Inconclusive",
            reasoning=_why("periode", (f"Kalimat merujuk Tabel {cited} ([{named.label}]), tetapi tabel itu tidak "
                       f"memuat '{fact.display_label}' untuk periode klaim; tabel lain tidak dipakai.")),
        )
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
    # Without a number of its own (a trend, or a threshold, whose number is the bound and not a
    # cell), the sentence's words are the only evidence: among fully tied tables that
    # hold the SAME row, the one whose title the sentence uses most ("penggunaan tenaga kerja LU
    # Pertanian … perbaikan" is Tabel 4, not Tabel 1, though both have the row). Only the same
    # row: SULNI's "ULN publik" ties with Tabel II.1's 'Bank Sentral', a different series. A tie
    # on the title too keeps upload order.
    head_rows = [label for label, _ in head_cand.resolved]
    numberless = fact.claimed_value is None or fact.operation in _THRESHOLD_OPS
    if numberless and head_result.verdict != "Inconclusive":
        tied = [index for index, (cand, result) in enumerate(evaluated)
                if result.verdict != "Inconclusive" and _rank_key(cand) == _rank_key(head_cand)
                and [label for label, _ in cand.resolved] == head_rows
                and (cand.src.origin in _FALLBACK_ORIGINS)
                == (head_cand.src.origin in _FALLBACK_ORIGINS)]
        # A tied table whose own figure for the claim's period the sentence prints, sign and all,
        # is the one it reads from: "sub-LU Industri Tekstil dan Pakaian Jadi (SBT -0,13%) …
        # terindikasi turun" (SKDU Tw II-2026) is Tabel 4's -0,13, not Tabel 1's 0,09 —
        # whichever title the sentence's words lean to. Only when exactly one table qualifies.
        printing = [index for index in tied
                    if _printed_signed(evaluated[index][0].resolved[-1][1], fact.context_quote)]
        best = max(tied, key=lambda index: evaluated[index][0].sentence_title, default=head_index)
        if len(printing) == 1:
            best = printing[0]
        if best != head_index and (len(printing) == 1
                                   or evaluated[best][0].sentence_title > head_cand.sentence_title):
            head_index = best
            head_cand, head_result = evaluated[head_index]
    if head_result.verdict == "Refuted" and fact.claimed_value is not None:
        for index, (cand, result) in enumerate(evaluated):
            if (result.verdict == "Entailed" and _rank_key(cand) == _rank_key(head_cand)
                    and (cand.src.origin in _FALLBACK_ORIGINS)
                    == (head_cand.src.origin in _FALLBACK_ORIGINS)):
                head_index = index
                break
    # Last, and only for the source that answers: a level no tied table confirms may be the row's
    # change that the sentence describes. Earlier, it would outrank a tied table that prints the
    # level itself (SKDU's "SBT 0,22%" is Tabel 4's level, and Tabel 2's capacity rose by 0,20).
    head_cand, head_result = evaluated[head_index]
    if head_result.verdict == "Refuted" and fact.operation == "value":
        label, raw = head_cand.resolved[0]
        recovered = _reinterpret_value_as_change(fact, label, head_cand.factor, raw / head_cand.factor,
                                                 head_cand.src)
        if recovered is not None:
            evaluated[head_index] = (head_cand, recovered)
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


# "(Lampiran Tabel 8)", "Tabel 2": the author's own pointer to the table a sentence reads.
_CITED_TABLE = re.compile(r"\b(?:lampiran\s+)?tabel\s+(\d+)\b", re.IGNORECASE)


def _cited_table(fact: ExtractedFact) -> Optional[str]:
    """The table number the sentence cites, when it cites exactly one."""
    cited = set(_CITED_TABLE.findall(fact.context_quote or ""))
    return cited.pop() if len(cited) == 1 else None


def _is_cited(src: "_ExcelSource", number: Optional[str]) -> bool:
    """True when `src` is the table the sentence cites: its title opens with 'Tabel <number>'."""
    return bool(number) and bool(re.match(r"\s*tabel\s*" + number + r"\b", src.table.title or "",
                                          re.IGNORECASE))


def _rank_key(c: _Candidate) -> Tuple[float, float, int, float]:
    """How _evaluate_fact orders candidate sources: coverage, title wording, qualification, label."""
    return (-c.coverage, -c.direct_coverage, c.qualification, -round(c.score, 9))


# Words that carry no subject: every table title has "Tabel", most sentences "dan", and a unit
# or growth tag ("(%, yoy)") says how a table measures, not what — a level sentence that adds
# "atau tumbuh 8,9% (yoy)" is still about the level.
_TITLE_FILLER = {"tabel", "table", "dan", "and", "the", "per", "atas", "dari", "yang",
                 "yoy", "mtm", "qtq", "ytd", "persen", "juta", "miliar", "triliun", "usd"}


def _title_words_in_sentence(fact: ExtractedFact, src: "_ExcelSource") -> int:
    """How many of the table's own subject words — its title, its TableData.subject_words — the
    claim's sentence uses (see _Candidate.sentence_title)."""
    title = _sig_words(src.table.title or "") | _sig_words(" ".join(src.table.subject_words))
    words = {w for w in title - _TITLE_FILLER if w.isalpha()}
    # A chart caption run into the sentence names ITS series, not the claim's (see _CAPTION_RE).
    return len(words & _sig_words(_without_captions(fact.context_quote or "")))


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
    # Two Excel sources are not two readings of one figure: the conflict check exists to set a
    # report's own tables against the workbook ("PDF vs Excel") or against each other. Between
    # sheets, a shared row name is another series — SKDU's 'REAL ESTAT' is business activity in
    # Tabel 1 and employment in Tabel 4, SULNI's 'Total' is all debt in TabI.1 and government
    # debt in Tbl II.1 — and every such claim was flagged "PDF vs Excel berbeda" (100+ per SKDU
    # report) over a verdict that was right.
    if head.src.origin == "excel" and other.src.origin == "excel":
        return False
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
# Words that only describe how a RATE moves: an index does not "melambat". With a growth tag in
# the sentence they make a trend about the growth series (SPE: "penjualan di Medan … tetap tumbuh
# meski melambat, … 0,6% (mtm)" is the monthly growth 1,3 -> 0,6, not the index 381,1 -> 383,5).
_RATE_MOVE_RE = re.compile(r"\b(melambat|perlambatan|terakselerasi|akselerasi)\b", re.IGNORECASE)
_GROWTH_TAG_RE = re.compile(r"\b(yoy|mtm|qtq)\b", re.IGNORECASE)
# A yoy figure quoted in the same sentence, which is what makes 'pertumbuhan' there a
# comparison of RATES rather than a passing mention.
_YOY_IN_QUOTE_RE = re.compile(r"\byoy\b", re.IGNORECASE)


# A chart or table caption the PDF text layer ran into the sentence: "Grafik 1 Pertumbuhan Tahunan
# Indeks Permintaan Properti Komersial Kategori Jual (%, yoy)". Its 'pertumbuhan' and '(%, yoy)'
# are the chart's, and made PPKom Tw I-2026's "penurunan permintaan pada segmen convention hall
# di Jakarta" (index 85,1 -> 70,2) a claim about the growth rate (-26,0 -> -2,7, "rising").
_CAPTION_RE = re.compile(r"(?<!\()\b(?:Grafik|Tabel)\s+\d+[a-z]?\s+[^.()]*?\((?:%|Indeks|persen)[^)]*\)",
                         re.IGNORECASE)


def _without_captions(quote: str) -> str:
    return _CAPTION_RE.sub(" ", quote)


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
    quote = _without_captions(fact.context_quote or "")
    if _RATE_MOVE_RE.search(quote) and _GROWTH_TAG_RE.search(quote):
        return True
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
    # The kind the sentence tags its figures with picks among growth tables: SPE has a yoy and
    # an mtm table of the same cities, and "… melambat, … 0,6% (mtm)" is the mtm one.
    kinds = {k.lower() for k in _GROWTH_TAG_RE.findall(_without_captions(fact.context_quote or ""))}
    tagged = [c for c in candidates
              if len(kinds) == 1 and _growth_kind(c.src.table.unit, "") in kinds]
    growth = tagged or [c for c in candidates if _is_growth_series(c.src.table.unit)]
    if not growth:
        return candidates
    return growth + [c for c in candidates if c not in growth]


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
        # A pointer reads the cells it is pointed at; a quantified claim needs every group's.
        and not facts[i].group_scope
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

# A sentence that points back at the one before it ("Hal ini tecermin dari ...", "Hal tersebut
# ...") does not say whose figure it gives.
_ANAPHORIC_OPENING = re.compile(r"^\s*(?:hal\s+(?:ini|tersebut)|kondisi\s+(?:ini|tersebut))\b",
                                re.IGNORECASE)


def _unnamed_total_guard(fact: ExtractedFact, result: FactVerificationResult) -> FactVerificationResult:
    """A refutation that rests on the table's TOTAL standing in for a sentence with no subject.

    SKDU: "... tenaga kerja di LU Pertanian ... diprakirakan meningkat. Hal ini tecermin dari
    SBT jumlah tenaga kerja sebesar 0,06% (Grafik 13)." The second sentence names no sector,
    so 'jumlah tenaga kerja' fell back to Tabel 4's TOTAL (0,95) and was refuted — the 0,06 is
    Pertanian's. A match still confirms; a mismatch only says the total is not what was meant.
    """
    if (result.verdict != "Refuted" or fact.operation not in _LEVEL_OPS
            or not _ANAPHORIC_OPENING.match(fact.context_quote or "")
            or any(re.sub(r"\W", "", (p.metric_label or "").lower()) in ("total", "jumlah")
                   for p in fact.periods)
            or not result.periods
            or not all(BITableData._is_aggregate(p.metric_label) for p in result.periods)):
        return result
    return result.model_copy(update={
        "verdict": "Inconclusive", "reason_code": "klaim_tidak_lengkap",
        "reasoning": (
            "Kalimat ini merujuk ke kalimat sebelumnya tanpa menyebut rinciannya, dan angkanya "
            f"tidak sama dengan baris TOTAL — kemungkinan angka ini milik rincian yang dibahas "
            f"kalimat sebelumnya. | {result.reasoning}"
        ),
    })


def _section_guard(
    fact: ExtractedFact, result: FactVerificationResult, sources: List[_ExcelSource]
) -> FactVerificationResult:
    """A verdict from a table that does not hold what the claim's report section is about.

    The publication parser lists those sections (TableData.uncovered_sections). UB Agustus 2026:
    under 'PERKEMBANGAN DANA PIHAK KETIGA (DPK)', "simpanan berjangka tumbuh 4,7%" is DPK's
    (the report's Tabel 3), but it was judged on M2's 'Simpanan Berjangka' row of SEKI I.1A and
    refuted. A match is no proof either — the right number on the wrong row is a coincidence —
    so the verdict, either way, becomes Tidak Cukup Data. The row itself naming the section's
    subject keeps the verdict: then the table does hold it after all.
    """
    if result.verdict == "Inconclusive" or not getattr(fact, "section_headings", None):
        return result
    src = next((s for s in sources if s.label == result.matched_excel_source), None)
    uncovered = getattr(src.table, "uncovered_sections", None) if src is not None else None
    if not uncovered:
        return result
    rows = " ".join(p.metric_label for p in result.periods)
    for heading in fact.section_headings:
        for pattern, subject in uncovered.items():
            if re.search(pattern, heading, re.IGNORECASE) and not re.search(pattern, rows, re.IGNORECASE):
                return result.model_copy(update={
                    "verdict": "Inconclusive", "reason_code": "di_luar_tabel", "computed_value": None, "delta": None,
                    "resolved_via": "section_guard",
                    "reasoning": (
                        f"Kalimat ini ada di bagian '{heading}', yang membahas {subject}; "
                        f"tabel {src.sheet} tidak memuat data {subject}, jadi baris "
                        f"'{rows}' bukan data yang dimaksud. | {result.reasoning}"
                    ),
                })
    return result


def _absent_item_guard(
    fact: ExtractedFact, result: FactVerificationResult, sources: List[_ExcelSource]
) -> FactVerificationResult:
    """A verdict on a total row for a breakdown the table never splits out.

    The publication parser lists those breakdowns (TableData.absent_items). NPI Tw II-2026:
    "impor minyak mentah 3,8 miliar" was refuted on 'Minyak > Impor' (11,6 — crude oil plus
    refined products), which the workbook does not split. The claim's own name decides; a matched
    row that names the item itself ('Harga rata-rata ekspor minyak mentah') still answers.
    """
    if result.verdict == "Inconclusive":
        return result
    src = next((s for s in sources if s.label == result.matched_excel_source), None)
    absent = getattr(src.table, "absent_items", None) if src is not None else None
    if not absent:
        return result
    name = fact.display_label or ""
    rows = " ".join(p.metric_label for p in result.periods)
    for pattern, item in absent.items():
        if re.search(pattern, name, re.IGNORECASE) and not re.search(pattern, rows, re.IGNORECASE):
            return result.model_copy(update={
                "verdict": "Inconclusive", "reason_code": "di_luar_tabel", "computed_value": None, "delta": None,
                "resolved_via": "absent_item_guard",
                "reasoning": (
                    f"Klaim ini tentang {item}, yang tidak dirinci di tabel {src.sheet}; baris "
                    f"'{rows}' adalah totalnya, bukan data yang dimaksud. | {result.reasoning}"
                ),
            })
    return result


_NO_NUMBER_OPS = {"above_threshold", "below_threshold", "is_increasing", "is_decreasing", "is_stable"}


def _number_phrase_guard(
    fact: ExtractedFact, result: FactVerificationResult, sources: List[_ExcelSource]
) -> FactVerificationResult:
    """A row that only answers a number the sentence follows with certain words, used for another.

    The publication parser lists such rows (TableData.number_phrases). NPI Tw II-2026: "setara
    dengan pembiayaan 5,6 bulan impor atau 5,4 bulan impor dan pembayaran utang luar negeri
    pemerintah" — the extractor put 5,6 on 'Dalam Bulan Impor dan Pembayaran ULN Pemerintah'
    (5,41), the row of the 5,4. The words printed after the claim's own number decide. Not for a
    threshold or a trend, which state no figure of the row (5,41 months of imports and debt
    payments does show reserves above "3 bulan impor").
    """
    if result.verdict == "Inconclusive" or fact.operation in _NO_NUMBER_OPS or fact.claimed_value is None:
        return result
    src = next((s for s in sources if s.label == result.matched_excel_source), None)
    phrases = getattr(src.table, "number_phrases", None) if src is not None else None
    if not phrases:
        return result
    quote = fact.context_quote or ""
    for row in dict.fromkeys(p.metric_label for p in result.periods):
        if row not in phrases:
            continue
        afters = [quote[m.end():m.end() + 80] for m in _PRINTED_NUMBER.finditer(quote)
                  if any(abs(n - abs(fact.claimed_value)) < 1e-9 for n, _ in _readings(m.group(0)))]
        if not afters or any(re.match(r"\W*" + phrases[row], a, re.IGNORECASE) for a in afters):
            continue
        said = " ".join(afters[0].split()[:4])
        return result.model_copy(update={
            "verdict": "Inconclusive", "reason_code": "jenis", "computed_value": None, "delta": None,
            "resolved_via": "number_phrase_guard",
            "reasoning": (
                f"Angka {fact.claimed_value:g} di kalimat diikuti '{said}…', bukan ukuran baris "
                f"'{row}'; tabel {src.sheet} tidak memuat ukuran itu. | {result.reasoning}"
            ),
        })
    return result


def _child_named_in_sentence(
    fact: ExtractedFact, result: FactVerificationResult, sources: List[_ExcelSource]
) -> FactVerificationResult:
    """The verdict on the matched row's child when the sentence names that child.

    The publication parser lists the words (TableData.child_keywords). NPI Tw II-2026: "Di pasar
    surat utang, sisi kewajiban sektor swasta mencatat net inflow sebesar 1,5 miliar dolar AS"
    was refuted on the private-sector portfolio liabilities (−1,61, shares plus debt) while its
    'Surat utang' child holds 1,53. Only when exactly one listed child is named, and exactly one
    such child sits under the row.
    """
    if result.matched_excel_source is None:
        return result
    src = next((s for s in sources if s.label == result.matched_excel_source), None)
    keywords = getattr(src.table, "child_keywords", None) if src is not None else None
    rows = list(dict.fromkeys(p.metric_label for p in result.periods))
    if not keywords or len(rows) != 1:
        return result
    named = {child for pattern, child in keywords.items()
             if re.search(pattern, fact.context_quote or "", re.IGNORECASE)}
    if len(named) != 1:
        return result
    child = named.pop()
    row_levels = [level.strip().lower() for level in rows[0].split(QUAL_SEP.strip())]
    if row_levels[-1] == child.lower():
        return result

    def _under_row(label: str) -> bool:
        levels = [level.strip().lower() for level in label.split(QUAL_SEP.strip())]
        parent = levels[:-1]
        return levels[-1] == child.lower() and bool(parent) and row_levels[-len(parent):] == parent

    children = [label for label in src.table.row_labels if _under_row(label)]
    if len(children) != 1:
        return result
    moved = ExtractedFact(
        operation=fact.operation,
        periods=[PeriodPoint(metric_label=children[0], year=p.year, month=p.month, col_label=p.col_label)
                 for p in fact.periods],
        claimed_value=fact.claimed_value, unit=fact.unit, context_quote=fact.context_quote,
        page_number=fact.page_number, group_scope=fact.group_scope,
    )
    on_child = _evaluate_fact(moved, [src])
    if on_child.verdict == "Inconclusive":
        return result
    return on_child.model_copy(update={
        "metric_label": result.metric_label,
        "reasoning": f"Kalimat menyebut '{child}'; dinilai pada baris '{children[0]}'. | {on_child.reasoning}",
    })


def _publication_guards(
    fact: ExtractedFact, result: FactVerificationResult, sources: List[_ExcelSource]
) -> FactVerificationResult:
    """The checks a publication parser configures (report sections, absent breakdowns, number
    phrases). Run after the code path and again after the cell pointer and the Tinjauan AI, which
    can put a claim on the same wrong row (UB "uang kartal yang diedarkan" was accepted on M2's
    currency row by the review)."""
    result = _section_guard(fact, result, sources)
    result = _absent_item_guard(fact, result, sources)
    result = _months_of_imports_guard(fact, result)
    return _number_phrase_guard(fact, result, sources)


_MONTHS_OF_IMPORTS = re.compile(r"\s*bulan\s+impor\b", re.IGNORECASE)


def _months_of_imports_guard(fact: ExtractedFact, result: FactVerificationResult) -> FactVerificationResult:
    """A number the sentence counts in months of imports, answered by a row that is not.

    Cadangan Devisa Agustus 2026: "berada di atas standar kecukupan internasional sekitar 3 bulan
    impor" came back as 'Cadangan Devisa' above 3 and was Sesuai on 125.377 juta USD. Only a row
    that names the measure ('Dalam Bulan Impor', NPI's V.9 has it) can answer such a number.
    """
    if result.verdict == "Inconclusive" or not fact.claimed_value:
        return result
    quote = fact.context_quote or ""
    in_months = False
    for m in _QUOTE_NUMBER.finditer(quote):
        try:
            same = abs(_id_number(m.group(0).rstrip(".")) - abs(fact.claimed_value)) <= 1e-9
        except ValueError:
            continue
        if same and _MONTHS_OF_IMPORTS.match(quote, m.end()):
            in_months = True
            break
    if not in_months or any("bulan" in p.metric_label.lower() for p in result.periods):
        return result
    rows = ", ".join(dict.fromkeys(p.metric_label for p in result.periods))
    return result.model_copy(update={
        "verdict": "Inconclusive", "reason_code": "jenis", "computed_value": None, "delta": None,
        "resolved_via": "months_of_imports_guard",
        "reasoning": (
            f"Angka klaim dinyatakan dalam bulan impor, sedangkan baris '{rows}' bukan ukuran "
            f"bulan impor. | {result.reasoning}"
        ),
    })


def _publication_notes(publication: str) -> str:
    """The chosen publication's conventions for the extractor when PUBLICATION_NOTES is on, else "".
    Guidance only: a notes file that does not load is logged and the check runs without it."""
    if not notes_enabled():
        return ""
    try:
        return notes_for(publication)
    except (OSError, ValueError, yaml.YAMLError) as exc:
        logger.warning("Publication notes for '%s' not used: %s", publication, exc)
        return ""


def _evaluate_fact_safely(fact: ExtractedFact, sources: List[_ExcelSource]) -> FactVerificationResult:
    """_evaluate_fact, with any failure contained to the one claim that caused it.

    Claims arrive in whatever shape the extractor gave them, and a shape the comparison code
    did not anticipate used to raise straight out of verify_paired: on SK-Juni-2026 a threshold
    claim carrying two periods ended the whole request with "list index out of range", and the
    user saw no verdict for any of its seventy claims. One claim the code cannot judge is a
    claim without a verdict — Inconclusive, with the reason logged — not a failed report.
    """
    try:
        result = _unnamed_total_guard(fact, _evaluate_fact(fact, sources))
        result = _child_named_in_sentence(fact, result, sources)
        return _publication_guards(fact, result, sources)
    except Exception as exc:
        logger.exception("Could not evaluate fact %r (%s)", fact.display_label, fact.operation)
        return _make_result(
            fact, [], None, fact.claimed_value, fact.unit, None, fact.unit, None, "Inconclusive", reason_code="lainnya",
            reasoning=f"Klaim ini gagal dinilai oleh program ({type(exc).__name__}: {exc}).",
        )


def _deduplicate_facts(facts: List[ExtractedFact]) -> List[ExtractedFact]:
    """Remove duplicate (operation, periods) entries — keep first occurrence."""
    seen = set()
    unique = []
    for f in facts:
        key = (f.operation, f.group_scope,
               tuple((p.metric_label, p.year, p.month, p.col_label) for p in f.periods))
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
                    "verdict": "Inconclusive", "reason_code": "tidak_ditemukan",
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


def _with_reason_codes(results: List[FactVerificationResult]) -> List[FactVerificationResult]:
    """Every Tidak Cukup Data leaves the engine saying why; a path that forgot is 'lainnya'
    (and logged, so the missing code can be added where that reason is written)."""
    out = []
    for r in results:
        if r.verdict == "Inconclusive" and r.reason_code is None:
            logger.warning("Tidak Cukup Data without a reason code: %r — %s", r.metric_label, r.reasoning[:120])
            r = r.model_copy(update={"reason_code": "lainnya"})
        out.append(r)
    return out


def _apply_team_aliases(sources: List[_ExcelSource], team_aliases: Dict[str, str]) -> None:
    """The team's aliases (whole metric name only) on every table that has the row they name. A
    team alias over a parser's alias of the same phrase wins."""
    if not team_aliases:
        return
    for src in sources:
        rows = set(src.table.row_labels)
        picked = {phrase: row for phrase, row in team_aliases.items() if row in rows}
        if picked:
            src.table.label_aliases = {**getattr(src.table, "label_aliases", {}), **picked}


def _remember_rows(publication: str, sources: List[_ExcelSource]) -> None:
    rows = [label for src in sources if not src.pointer_only for label in src.table.row_labels]
    if not publication or not rows:
        return
    try:
        team_settings.record_known_rows(publication, rows)
    except Exception:
        logger.warning("Nama baris untuk saran padanan tidak tersimpan", exc_info=True)


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
    review_llm: Optional[BaseChatModel] = None,
    review_new_refuted: str = "note",
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
        review_llm:     Model for the Tinjauan AI stage (ai_review.py); None skips the stage, so
                        the output is exactly the code path's. Thinking-enabled on Gemini.
        review_new_refuted: "note" (default) flags a Tidak Sesuai the AI found on a Tidak Cukup
                        Data claim; "verdict" lets it stand. See ai_review.judge_review.

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

    # Step 1a: the team's settings for this publication (Pengaturan, team_settings): its aliases
    # join the parser's own on each table; its notes go to the Tinjauan AI (step 3c). The row
    # names are kept for the alias form's suggestions. None of it can stop the check.
    team_aliases, team_notes = team_settings.for_check(publication)
    _apply_team_aliases(parsed_sources, team_aliases)
    _remember_rows(publication, parsed_sources)

    # STRICT_EXTRACTION: a check against Excel time-series tables ("excel" or "both") goes to the
    # strict path, where the extractor names the cells and nothing below (matching, guards, cell
    # pointer, the document's own tables) runs. Imported here: strict builds
    # on this module.
    from strict import pipeline as strict_pipeline
    if strict_pipeline.enabled() and strict_pipeline.applies(parsed_sources, mode, bool(chart_readings)):
        return await strict_pipeline.run_check(
            narrative_text, parsed_sources, llm, pdf_filename=pdf_filename, vision_llm=vision_llm,
            publication=publication, team_aliases=team_aliases, team_notes=team_notes,
            review_llm=review_llm, review_new_refuted=review_new_refuted, progress_cb=progress_cb,
            on_extract_gap=on_extract_gap, excel_parsers=excel_parsers, mode=mode,
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

    # Every row goes to the extractor, which shows each chunk the ones it is about
    # (structured_extractor._labels_for_chunk, capped per source so the prompt does not grow
    # with the page count). all_row_labels below is already deduplicated.
    source_labels_for_extractor = _merge_sources_sharing_rows([
        (_source_desc(src), src.table.row_labels) for src in parsed_sources
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
    extraction_primary, extraction_fallback = extraction_llms(llm, vision_llm)

    def _on_chunk_progress(done: int, total: int) -> None:
        emit("extract", "running", current=done, total=total, detail=f"{total} bagian teks")

    # Sections the publication marks as holding no claims (SBank's METODOLOGI) are never shown
    # to the extractor: their figures ("SBT > 0 berarti lebih ketat") only look like claims.
    raw_facts = await extract_structured_facts_async(
        drop_sections(narrative_text, skipped_sections(publication)),
        all_row_labels,
        extraction_primary,
        fallback_llm=extraction_fallback,
        source_labels=source_labels_for_extractor,
        on_progress=_on_chunk_progress,
        on_chunk_failed=on_extract_gap,
        publication_notes=_publication_notes(publication),
    )
    facts = _deduplicate_facts(raw_facts)
    logger.info("%d unique facts after deduplication (was %d)", len(facts), len(raw_facts))
    attach_section_headings(facts, narrative_text)
    facts = _drop_group_stand_ins(facts)
    _attach_named_members(facts)
    _attach_group_exclusions(facts)
    _attach_rankings(facts)
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

    # Step 3c: Tinjauan AI — a second look at what the code left Tidak Sesuai / Tidak Cukup
    # Data. Imported here because ai_review builds on this module. Any failure keeps the code's
    # verdicts: the stage can only ever add to a finished check, never break one.
    n_reviewed = n_flagged = 0
    if review_llm is not None:
        from ai_review import review_candidates, review_claims
        n_candidates = len(review_candidates(results))
        if n_candidates:
            emit("compare", "running", detail=f"tinjauan AI: {n_candidates} klaim")
            try:
                results, review_stats = await review_claims(
                    results, table_sources, review_llm, new_refuted=review_new_refuted,
                    team_notes=team_notes,
                )
                n_reviewed, n_flagged = review_stats.changed, review_stats.flagged
            except Exception:
                logger.warning("Tinjauan AI gagal; vonis kode dipertahankan", exc_info=True)

    # The publication's own checks once more, over verdicts the cell pointer or the review reached.
    results = [
        _publication_guards(fact, result, table_sources) for fact, result in zip(facts, results)
    ] + results[len(facts):]
    results = _with_reason_codes(results)

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
    if n_reviewed:
        compare_detail += f" · {n_reviewed} diubah tinjauan AI"
    if n_flagged:
        compare_detail += f" · {n_flagged} perlu ditinjau"
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
