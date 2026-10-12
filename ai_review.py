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
from typing import Dict, List, Literal, Optional, Tuple

from pydantic import BaseModel, Field

from excel_parser_bi import BITableData
from paired_verifier import _evaluate_fact_safely, _ExcelSource, _period_ordinal
from schemas import FactVerificationResult
from structured_extractor import ExtractedFact, PeriodPoint
from table_model import TableData, _label_words, _same_root, _sig_words

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


# Words that say HOW a series moved or is measured, never WHICH series it is. A subject made
# only of these ("pertumbuhan tahunan") puts no constraint on the row, which is intended: the
# engine still has to reproduce the number.
_GENERIC_SUBJECT_WORDS = TableData._TITLE_STOP_WORDS | frozenset({
    "nilai", "sebesar", "tumbuh", "tahunan", "yoy", "mtm", "qtq", "persen", "triliun", "miliar",
    "juta", "usd", "dolar", "rupiah", "meningkat", "menurun", "naik", "turun", "kenaikan",
    "penurunan", "bulan", "triwulan", "tahun", "periode", "sebelumnya", "level", "indeks",
    "januari", "februari", "maret", "april", "mei", "juni", "juli", "agustus", "september",
    "oktober", "november", "desember",
})


def subject_is_covered(subject: str, quote: str, rows: List[str], src: _ExcelSource) -> bool:
    """True when the subject is the sentence's own words and every significant one of them is
    in a cited row (its 'Parent > ' path included), the table title or the sheet name.

    This is the deterministic backstop for the one mistake the spike showed the model makes
    despite being told not to: answering a breakdown ("impor minyak") with its total row
    ("Migas > Impor"). Words are compared whole or one derivational prefix apart
    (_same_root), so 'kepemilikan' meets 'Pemilikan' but 'usaha' never meets 'Perusahaan'.
    An abbreviation the table spells out differently (KPR) fails the guard — the verdict then
    simply stays as the code left it.
    """
    if not subject.strip() or _norm(subject) not in _norm(quote):
        return False
    words = [w for w in _sig_words(subject) if w not in _GENERIC_SUBJECT_WORDS and not w.isdigit()]
    known = _label_words(" ".join(rows) + " " + src.table.title + " " + src.sheet)
    return all(any(_same_root(w, k) for k in known) for w in words)


Operation = Literal[
    "value", "yoy_growth", "average", "sum", "diff", "ratio",
    "is_increasing", "is_decreasing", "is_stable", "above_threshold", "below_threshold",
]


class ClaimReview(BaseModel):
    # Field order matters: a structured answer is generated top to bottom, so the model has to
    # write down the sentence's subject and check its words against the row BEFORE it commits
    # to a match kind, and the match kind before the verdict.
    claim_no: int
    first_words: str = Field(description="5 kata pertama kalimat klaim, untuk memastikan nomor tidak tertukar")
    subject: str = Field(description="Frasa yang menyebut APA yang diukur, disalin PERSIS dari kalimat")
    word_check: str = Field(description="Tiap kata penting subject: ada/tidak di baris, induknya, atau judul tabel")
    match: Literal["persis", "total_atau_induk", "mirip", "tidak_ada"]
    verdict: Literal["sesuai", "tidak_sesuai", "tidak_ada_di_tabel"]
    operation: Optional[Operation] = None
    cells: List[CitedCell] = Field(
        default_factory=list,
        description="Sel yang dipakai; yoy_growth: [tahun lalu, sekarang]; diff/tren: urut waktu; ratio: [pembilang, penyebut]",
    )
    explanation: str = Field(description="Alasan singkat, Bahasa Indonesia, maks 2 kalimat")


def _same_claim(review: ClaimReview, quote: str) -> bool:
    """The answer is about this sentence: its first words open the sentence too."""
    said = review.first_words.lower().split()[:2]
    return not said or said == (quote or "").lower().split()[:2]


def _header(suffix: Tuple) -> str:
    return suffix[0] if len(suffix) == 1 else f"{suffix[1]} {suffix[0]}"


def _cells_note(src: _ExcelSource, keyed: List[Tuple[str, Tuple, float]]) -> str:
    return "; ".join(f"{src.sheet} · {label} · {_header(suffix)} = {_fmt(v)}" for label, suffix, v in keyed)


def _evaluate_on_cells(
    result: FactVerificationResult, operation: str, src: _ExcelSource,
    keyed: List[Tuple[str, Tuple, float]],
) -> FactVerificationResult:
    """The claim rebuilt from the cited cells and judged by the ordinary engine, on a copy of
    the sheet holding only the cited rows — so units, thresholds, tolerances and the deficit
    rules apply exactly as in the code path, and no other row can be picked."""
    table = src.table
    rows = list(dict.fromkeys(label for label, _s, _v in keyed))
    mini = BITableData(title=table.title, unit=table.unit, row_labels=rows,
                       col_labels=list(table.col_labels), axis_type=table.axis_type)
    mini._data = {k: v for k, v in table._data.items() if k[0] in rows}
    points = keyed
    if operation == "yoy_growth" and table.axis_type != "categorical":
        # The engine looks the prior year up itself; it wants the current period only.
        points = [max(keyed, key=lambda c: (c[1][0], _period_ordinal(c[1][1])))]
    periods = [
        PeriodPoint(metric_label=label, col_label=suffix[0]) if len(suffix) == 1
        else PeriodPoint(metric_label=label, year=suffix[0], month=suffix[1])
        for label, suffix, _v in points
    ]
    unit = "persen" if result.claimed_unit == "persen_yoy" else result.claimed_unit
    fact = ExtractedFact(
        operation=operation, periods=periods, claimed_value=result.claimed_value, unit=unit,
        context_quote=result.context_quote, page_number=result.page_number,
    )
    patched = _ExcelSource(table=mini, filename=src.filename, sheet=src.sheet, origin=src.origin)
    return _evaluate_fact_safely(fact, [patched])


def judge_review(
    result: FactVerificationResult,
    review: ClaimReview,
    codes: Dict[str, _ExcelSource],
    *,
    new_refuted: str = "note",
    use_guard: bool = True,
) -> Tuple[FactVerificationResult, str]:
    """The claim after the model's answer, and what happened to it (see module docstring)."""
    unchanged = (result, "unchanged")
    if not _same_claim(review, result.context_quote):
        return unchanged
    if review.verdict == "tidak_ada_di_tabel":
        if result.verdict != "Refuted":
            return unchanged
        return result.model_copy(update={
            "verdict": "Inconclusive", "computed_value": None, "delta": None,
            "resolved_via": "ai_review",
            "reasoning": (f"{result.reasoning} | Ditinjau AI: tabel tidak memuat data yang dimaksud "
                          f"kalimat ini — {review.explanation}"),
        }), "to_inconclusive"
    if review.match != "persis":
        return unchanged
    read = read_cells(review.cells, codes)
    if read is None:
        return unchanged
    src, keyed = read
    rows = [label for label, _s, _v in keyed]
    if use_guard and not subject_is_covered(review.subject, result.context_quote, rows, src):
        return unchanged
    engine = _evaluate_on_cells(result, review.operation or result.operation, src, keyed)
    if engine.verdict == "Inconclusive" or (engine.verdict == "Entailed") != (review.verdict == "sesuai"):
        return unchanged
    cells = _cells_note(src, keyed)
    accepted = engine.model_copy(update={
        "resolved_via": "ai_review",
        "reasoning": f"{engine.reasoning} | Ditinjau AI: sel {cells} — {review.explanation}",
    })
    if engine.verdict == "Entailed":
        return accepted, "to_entailed"
    if result.verdict == "Refuted":
        return unchanged
    if new_refuted == "verdict":
        return accepted, "to_refuted"
    return result.model_copy(update={
        "resolved_via": "ai_review_flag",
        "reasoning": (f"{result.reasoning} | Ditinjau AI: kemungkinan tidak sesuai — sel {cells} "
                      f"memberi {engine.computed_value}, klaim {result.claimed_value}; perlu ditinjau"),
    }), "flag"
