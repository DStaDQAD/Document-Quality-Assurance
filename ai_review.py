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

import asyncio
import json
import logging
import os
import re
from collections import Counter
from dataclasses import dataclass, field
from pathlib import Path
from typing import Dict, List, Literal, Optional, Tuple

from langchain_core.language_models import BaseChatModel
from langchain_core.prompts import ChatPromptTemplate
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
# Batches asked at the same time (see ask_reviews).
MAX_PARALLEL_BATCHES = 8

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
    ("Migas > Impor"). judge_review asks it only of a NEW Tidak Sesuai — the answer such a row
    produces — and not of a Sesuai, whose reproduced number is proof enough. Words are compared whole or one derivational prefix apart
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


# Operations the engine computes from a fixed number of cells. The model sometimes cites more
# (two numerators over a total for a "ratio"), which the engine cannot take: such an answer is
# refused here rather than left to raise inside _evaluate_fact.
_EXACT_CELLS = {"value": 1, "ratio": 2, "diff": 2}


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


def second_look_enabled() -> bool:
    """AI_REVIEW_SECOND_LOOK=1/on/true/yes: the model is told reports are usually right and looks
    for another reasonable reading before agreeing with a Tidak Sesuai, and the engine accepts
    the sign the sentence states. Off unless set."""
    return os.getenv("AI_REVIEW_SECOND_LOOK", "").strip().lower() in ("1", "on", "true", "yes")


# A fall word shortly before the claimed number: "turun 4,1%", "terkontraksi sebesar 0,1%".
_FALL_BEFORE = re.compile(
    r"\b(turun|menurun|penurunan|terkontraksi|kontraksi|minus|negatif)\b[^.;\d]{0,30}$", re.IGNORECASE)


def _said_negative(result: FactVerificationResult) -> bool:
    """The sentence prints the claimed (positive) number right after a word that makes it negative."""
    value = result.claimed_value
    if value is None or value <= 0:
        return False
    text = f"{value:.4f}".rstrip("0").rstrip(".").replace(".", ",")
    quote = result.context_quote or ""
    return any(_FALL_BEFORE.search(quote[:m.start()])
               for m in re.finditer(r"(?<![\d,.])" + re.escape(text) + r"(?!\d)", quote))


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
            "verdict": "Inconclusive", "reason_code": "ditinjau_ai", "computed_value": None, "delta": None,
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
    operation = review.operation or result.operation
    if len(keyed) != _EXACT_CELLS.get(operation, len(keyed)):
        return unchanged
    engine = _evaluate_on_cells(result, operation, src, keyed)
    if (engine.verdict == "Refuted" and review.verdict == "sesuai" and second_look_enabled()
            and _said_negative(result)):
        # "Juli 2025 yang turun 4,1% (mtm)": the sentence signs the number, the extractor did not.
        signed = _evaluate_on_cells(result.model_copy(update={"claimed_value": -result.claimed_value}),
                                    operation, src, keyed)
        if signed.verdict == "Entailed":
            engine = signed
    if engine.verdict == "Inconclusive" or (engine.verdict == "Entailed") != (review.verdict == "sesuai"):
        return unchanged
    cells = _cells_note(src, keyed)
    accepted = engine.model_copy(update={
        "resolved_via": "ai_review",
        "reasoning": f"{engine.reasoning} | Ditinjau AI: sel {cells} — {review.explanation}",
    })
    if engine.verdict == "Entailed":
        # The engine reproduced the claimed number from the cited cells: that is the proof, and
        # the subject guard is not asked. Run on 2026-10-12 it rejected 49 such answers, every
        # one correct, over abbreviations (KFLN for the 'Kewajiban' row).
        return accepted, "to_entailed"
    if result.verdict == "Refuted":
        return unchanged
    rows = [label for label, _s, _v in keyed]
    if use_guard and not subject_is_covered(review.subject, result.context_quote, rows, src):
        return unchanged
    if new_refuted == "verdict":
        return accepted, "to_refuted"
    return result.model_copy(update={
        "resolved_via": "ai_review_flag",
        "reasoning": (f"{result.reasoning} | Ditinjau AI: kemungkinan tidak sesuai — sel {cells} "
                      f"memberi {engine.computed_value}, klaim {result.claimed_value}; perlu ditinjau"),
    }), "flag"


class ReviewBatch(BaseModel):
    reviews: List[ClaimReview]


SYSTEM = """Kamu memeriksa ulang klaim angka dari laporan statistik Bank Indonesia terhadap tabel
sumbernya. Program sudah memeriksa klaim-klaim ini dan hasilnya Tidak Sesuai atau Tidak Cukup
Data. Tugasmu: apakah tabel benar-benar memuat data yang dimaksud kalimat, dan jika ya, sel mana.

Untuk SETIAP klaim, isi kolom berurutan:
1. subject: frasa yang menyebut APA yang diukur, disalin PERSIS dari kalimat (mis. "impor minyak
   mentah", "kredit modal kerja", "IKK"). Jangan diganti dengan nama baris tabel.
2. Pilih baris yang paling cocok, lalu isi word_check: setiap kata penting subject, dan apakah
   kata itu ada di label baris, induk barisnya (bagian sebelum " > "), atau judul tabel.
   Contoh: "impor: ada; minyak: tidak ada; mentah: tidak ada".
3. match:
   - persis: baris itu mengukur hal yang sama dengan subject (semua kata penting ada, atau
     singkatan resminya, mis. KPR = Kredit Pemilikan Rumah).
   - total_atau_induk: baris itu total atau induk dari yang dimaksud. Contoh: klaim "impor
     minyak", baris "Migas > Impor"; klaim "remitansi pekerja migran", baris "Pendapatan Sekunder".
   - mirip: topik serupa tapi ukurannya lain. Contoh: sisi aset saja untuk posisi neto; tabel
     pertumbuhan yoy untuk laju mtm/qtq; nilai nominal untuk klaim riil.
   - tidak_ada: tidak ada baris yang relevan.
4. verdict: "sesuai" atau "tidak_sesuai" HANYA boleh bila match = persis. Untuk match lain,
   jawab "tidak_ada_di_tabel". JIKA RAGU, jawab "tidak_ada_di_tabel" — itu jawaban yang aman.
5. operation dan cells: sebutkan SEMUA sel yang dipakai (kode sheet, label baris, header kolom,
   nilai) persis seperti tertulis di tabel. Sel kosong tidak boleh dipakai.
   value = angka langsung · yoy_growth = sel tahun lalu lalu sel sekarang · diff = lama lalu baru ·
   sum · average · ratio = pembilang lalu penyebut (persen) · is_increasing / is_decreasing /
   is_stable = sel urut waktu · above_threshold / below_threshold = sel yang dibandingkan dengan
   angka ambang di klaim.
6. explanation: alasan singkat, maksimal 2 kalimat.

Catatan tentang tabel:
- Laporan menulis besarnya defisit atau kewajiban neto sebagai angka positif; tabel menyimpannya
  negatif. Impor (debit) juga disimpan negatif. "Menurun", "menyempit", "melebar" untuk defisit
  atau kewajiban neto bicara tentang BESARNYA: dari −223.049 ke −197.431 berarti kewajiban neto
  menurun (sesuai), walaupun angka di tabel naik.
- Pada Saldo Bersih Tertimbang (SBT), "tumbuh", "meningkat" atau "ekspansi" TANPA pembanding
  periode lain berarti SBT bernilai positif; "terkontraksi" atau "turun" tanpa pembanding berarti
  SBT negatif. Hanya bila kalimat membandingkan dengan periode lain ("lebih tinggi dari triwulan
  sebelumnya") itu perbandingan dua sel.
- Hal yang sama berlaku untuk Saldo Bersih (SB) di tabel "% responden": "tetap tumbuh", "tetap
  baik", "masih dalam kondisi baik", "tetap mudah", "tetap kuat" atau "terjaga" berarti saldo
  bersihnya POSITIF, bukan bahwa angkanya tidak berubah. SB likuiditas 17,05 → 15,03 dengan
  kalimat "likuiditas masih dalam kondisi baik" itu sesuai (15,03 positif), walaupun angkanya
  turun. Kutip sel periode yang dibicarakan kalimat (dan periode sebelumnya bila operasinya tren).
- Kolom ekspektasi "X bulan yang akan datang" pada bulan survei B adalah ekspektasi untuk bulan
  B+X: nilai "3 bulan yang akan datang" di kolom Juli 2026 adalah ekspektasi Oktober 2026.
- Satuan klaim (juta/miliar/triliun) boleh berbeda dari satuan tabel; program yang mengonversi.
  Salin nilai sel apa adanya.
- Laporan membulatkan ke 1-2 desimal; selisih karena pembulatan dianggap sesuai.
- claim_no sama dengan nomor klaim, dan first_words adalah 5 kata pertama kalimatnya."""

PROMPT = ChatPromptTemplate.from_messages([
    ("system", SYSTEM),
    ("human", "TABEL:\n{tables}\n\nKLAIM YANG DIPERIKSA:\n{claims}\n\nBeri satu review per klaim, urut sesuai nomor."),
])

# AI_REVIEW_SECOND_LOOK (second_look_enabled): the owner's prior — a BI report is nearly always
# right — so a program Tidak Sesuai earns a search for another reasonable reading first. The engine
# still judges every cited cell, so a real error stays Tidak Sesuai.
SECOND_LOOK = """

Latar penting: laporan Bank Indonesia hampir selalu benar — biasanya 0 sampai 2 angka keliru per
dokumen. Untuk klaim yang hasil programnya Tidak Sesuai, jangan langsung setuju. Cari dulu bacaan
lain yang wajar dari kalimat itu:
- periode lain yang dimaksud kalimat: triwulan sebelumnya, setahun penuh ("sepanjang 2025",
  dibandingkan akhir tahun lalu), bulan yang sama tahun lalu, bulan target ekspektasi;
- tanda: "turun 4,1%" atau "terkontraksi 0,1%" berarti −4,1 / −0,1;
- laju pertumbuhan versus level ("kontraksi pertumbuhan yang lebih rendah" bicara tentang laju);
- baris lain yang lebih tepat: sisi neto versus bruto, kelompok yang disebut kalimat sebelumnya,
  tabel lain dengan nama baris yang sama.
Jika ada bacaan wajar dengan sel yang cocok, jawab "sesuai" dengan sel-sel itu. Jawab
"tidak_sesuai" hanya bila tidak ada bacaan wajar yang cocok — kesalahan sungguhan memang ada dan
harus tetap dilaporkan."""

PROMPT_SECOND_LOOK = ChatPromptTemplate.from_messages([
    ("system", SYSTEM + SECOND_LOOK),
    ("human", "TABEL:\n{tables}\n\nKLAIM YANG DIPERIKSA:\n{claims}\n\nBeri satu review per klaim, urut sesuai nomor."),
])

_VERDICT_ID = {"Refuted": "Tidak Sesuai", "Inconclusive": "Tidak Cukup Data", "Entailed": "Sesuai"}


def review_candidates(results: List[FactVerificationResult]) -> List[int]:
    """Narrative claims the code did not confirm. Chart labels are left to the chart checks.

    A quantified group claim ("seluruh", "sebagian besar", "lainnya") is not reviewed: the
    review re-runs a claim on the rows the model cites, and for a claim about every group that
    is a subset the model chose — it could leave out exactly the group that refutes it.

    Nor is a claim the section or absent-item guard set aside (paired_verifier._section_guard,
    _absent_item_guard): the table holds no such data, so any row the model cites is the wrong one.
    """
    return [
        i for i, r in enumerate(results)
        if r.verdict in ("Refuted", "Inconclusive") and r.checked_item == "narrative"
        and not r.group_scope and r.resolved_via not in _GUARDED
    ]


_GUARDED = ("section_guard", "absent_item_guard", "number_phrase_guard")


def _claim_line(n: int, r: FactVerificationResult) -> str:
    quote = " ".join((r.context_quote or "").split())[:400]
    value = "-" if r.claimed_value is None else f"{r.claimed_value} {r.claimed_unit or ''}".strip()
    line = (f'{n}. Kalimat: "{quote}" | metrik menurut ekstraktor: {r.metric_label} | '
            f"angka diperiksa: {value} | operasi ekstraktor: {r.operation}")
    if second_look_enabled():
        line += f" | hasil program: {_VERDICT_ID.get(r.verdict, r.verdict)}"
    return line


async def ask_reviews(
    results: List[FactVerificationResult],
    indices: List[int],
    codes: Dict[str, _ExcelSource],
    llm: BaseChatModel,
    batch_size: int = BATCH_SIZE,
) -> Dict[int, ClaimReview]:
    """The model's answer per claim index. A batch that fails is logged and left unanswered."""
    tables = "\n\n".join(table_markdown(code, src) for code, src in codes.items())
    chain = (PROMPT_SECOND_LOOK if second_look_enabled() else PROMPT) | llm.with_structured_output(ReviewBatch)
    # Batches are independent, so they are asked together: one after another, NPI's nine took
    # 216 s on 2026-10-12. The cap keeps a long report from bursting the provider's rate limit.
    limit = asyncio.Semaphore(MAX_PARALLEL_BATCHES)

    async def _ask(batch_no: int, batch: List[int]):
        claims = "\n".join(_claim_line(n, results[i]) for n, i in enumerate(batch, 1))
        async with limit:
            try:
                return batch, await chain.ainvoke({"tables": tables, "claims": claims})
            except Exception:
                logger.warning("Tinjauan AI: batch %d gagal; klaimnya tetap dengan vonis kode",
                               batch_no, exc_info=True)
                return batch, None

    async def _round(pending: List[int]) -> None:
        batches = [pending[s:s + batch_size] for s in range(0, len(pending), batch_size)]
        replies = await asyncio.gather(*(_ask(n, b) for n, b in enumerate(batches, 1)))
        for batch, reply in replies:
            if reply is None:
                continue
            for review in reply.reviews:
                if 1 <= review.claim_no <= len(batch):
                    answers.setdefault(batch[review.claim_no - 1], review)

    answers: Dict[int, ClaimReview] = {}
    await _round(indices)
    # A reply can stop short (NPI 2026-10-12: 6 answers for a batch of 30) or a batch can fail;
    # those claims are asked once more, on their own. Twice is enough: what is still unanswered
    # keeps the code's verdict.
    unanswered = [i for i in indices if i not in answers]
    if unanswered:
        await _round(unanswered)
    return answers


@dataclass
class ReviewStats:
    candidates: int = 0
    answered: int = 0
    outcomes: Dict[str, int] = field(default_factory=dict)

    @property
    def changed(self) -> int:
        return sum(n for kind, n in self.outcomes.items() if kind.startswith("to_"))

    @property
    def flagged(self) -> int:
        return self.outcomes.get("flag", 0)


async def review_claims(
    results: List[FactVerificationResult],
    sources: List[_ExcelSource],
    llm: BaseChatModel,
    *,
    new_refuted: str = "note",
    use_guard: bool = True,
    batch_size: int = BATCH_SIZE,
) -> Tuple[List[FactVerificationResult], ReviewStats]:
    """Every unconfirmed narrative claim, after the model's second look and code's judgement."""
    codes = reviewable_sources(sources)
    indices = review_candidates(results)
    stats = ReviewStats(candidates=len(indices))
    if not codes or not indices:
        return list(results), stats
    answers = await ask_reviews(results, indices, codes, llm, batch_size)
    stats.answered = len(answers)
    reviewed = list(results)
    counts: Counter = Counter()
    outcomes: Dict[int, str] = {}
    for i, review in answers.items():
        reviewed[i], outcome = judge_review(
            results[i], review, codes, new_refuted=new_refuted, use_guard=use_guard
        )
        counts[outcome] += 1
        outcomes[i] = outcome
    stats.outcomes = dict(counts)
    raw_log = os.getenv("AI_REVIEW_RAW_LOG", "").strip()
    if raw_log:
        _write_raw_answers(Path(raw_log), results, reviewed, indices, answers, outcomes)
    return reviewed, stats


def _write_raw_answers(
    path: Path,
    before: List[FactVerificationResult],
    after: List[FactVerificationResult],
    indices: List[int],
    answers: Dict[int, ClaimReview],
    outcomes: Dict[int, str],
) -> None:
    """The model's raw answer per reviewed claim, appended as JSON lines — for diagnosing a
    review that went wrong, never shown to the user (the response and Riwayat carry only the
    verdict). Only written when AI_REVIEW_RAW_LOG names a file; a failure is logged, not raised."""
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        with path.open("a", encoding="utf-8") as fh:
            for i in indices:
                review = answers.get(i)
                fh.write(json.dumps({
                    "index": i,
                    "metric_label": before[i].metric_label,
                    "operation": before[i].operation,
                    "claimed_value": before[i].claimed_value,
                    "context_quote": before[i].context_quote,
                    "verdict_before": before[i].verdict,
                    "verdict_after": after[i].verdict,
                    "outcome": outcomes.get(i, "unanswered"),
                    "review": review.model_dump() if review else None,
                }, ensure_ascii=False) + "\n")
    except OSError:
        logger.warning("Tinjauan AI: jawaban mentah tidak bisa ditulis ke %s", path, exc_info=True)
