"""Transcribe the data tables printed INSIDE a PDF report into grids.

Why this exists: a BI report carries snippet tables on its narrative pages (Tabel 2, Tabel 3…)
and the full series in its Lampiran pages. Those are an independent reference for the report's
own claims — one that cannot be out of sync with the report the way a separately-uploaded Excel
workbook can. `paired_verifier.verify_paired` consumes the grids produced here exactly like it
consumes Excel sheets (see `_ExcelSource(origin="pdf")`).

Why VISION and not the text layer, for every page, unconditionally:
  - Reports are routinely scanned. On one sample (`M2-April-2026 (1).pdf`) 10 pages carry 337
    characters of text layer in total — there is nothing to parse.
  - Even with a good text layer, PDFium emits table rows with the column headers detached from
    the body rows, and interleaves two Lampiran tables that share a page. Reconstructing the
    row/column geometry from that stream is guesswork; a rendered page shows the model the
    actual grid.

So the model reads the LAYOUT off the image. It does not get to supply the numbers whenever
they can be obtained otherwise: on a page that has a text layer, every transcribed value is
replaced by the one PDFium reads out of the file, and a row that cannot be matched up is
emptied rather than trusted (see _verify_against_text_layer). That keeps this module inside the
project's standing rule — the LLM points, code reads the value — which is the same division of
labour cell_pointer.py uses.

Measured on sample_data/M2-April-2026.pdf before that verification existed, against the text
layer of its two single-table pages: Lampiran 1 (29x15) came back ~98% correct, but Lampiran 6
(46x18) only ~73%, and its errors were not misread digits — whole value ROWS were attached to
the wrong label, which no structural check can see because the result stays perfectly
well-formed. One misread digit is enough to flip a verdict: 5.224,9 read as 5.274,9 turned a
correct "M1 tumbuh 15,3%" into Tidak Sesuai.

THE REMAINING CAVEAT: a scanned page has no text layer to check against, so there the numbers
really are the model's. Such tables are reported as "pdf-*-unverified" so a reviewer can tell
them apart. What contains them is self-consistency — a report states most series twice, snippet
AND Lampiran, so a bad cell tends to surface as a reported CONFLICT between two internal
sources rather than a silently wrong verdict.
"""

import asyncio
import difflib
import hashlib
import logging
import os
import re
from collections import OrderedDict
from dataclasses import dataclass, field
from typing import Callable, Dict, List, Optional, Tuple

from langchain_core.language_models import BaseChatModel
from langchain_core.messages import HumanMessage
from pydantic import BaseModel, Field

from pdf_extraction import (
    _extract_pages_raw,
    call_vision_with_retry,
    plan_vision_concurrency,
    render_pages_to_b64,
    vision_provider_flags,
)
from structured_extractor import _parse_indonesian_number
from table_parser_generic import _MONTH_ABBREVS, _QUARTER_CANON, _bare_period_token

logger = logging.getLogger("fact-checker")

# Pages per vision call. 1 by default and rarely worth raising: a Lampiran page is ~40 rows x
# ~14 columns = ~560 cells, and batching two of those risks the output being truncated mid-table.
# That is the worst failure mode here — a dropped row is invisible downstream, whereas a failed
# call is logged and skipped.
_TABLE_PAGES_PER_CALL = int(os.getenv("PDF_TABLE_PAGES_PER_CALL", "1"))

# Bump when the prompt, the assembly logic or the verification pass changes: it is part of the
# cache key, so stale transcriptions from a previous version can never be served.
_PROMPT_VERSION = "2"

_MAX_CACHE_ENTRIES = 8


@dataclass
class PdfTable:
    """One data table transcribed off one PDF page."""
    page_number: int                       # 1-based
    caption: str                           # "Lampiran 1. Tabel Uang Beredar dan Faktor-Faktornya"
    unit: str                              # "Triliun Rp" | "%, yoy" | ""
    grid: List[List] = field(default_factory=list)   # assembled; numeric cells are int/float
    index_on_page: int = 0
    # True when this table's page had a text layer, so every value the grid still carries was
    # read out of the file by code rather than off the image by the model. False for a scanned
    # page — worth surfacing, since only then are the numbers the model's own.
    verified: bool = False

    @property
    def label(self) -> str:
        """Display name used as the source's 'sheet' — e.g. 'Hal. 7 · Lampiran 1. Tabel …'."""
        caption = self.caption.strip() or f"Tabel {self.index_on_page + 1}"
        return f"Hal. {self.page_number} · {caption[:60]}"


# ---------------------------------------------------------------------------
# LLM output schema — strings only. No number crosses this boundary as a number;
# _coerce_cell below is the single place a printed cell becomes a Python value.
# ---------------------------------------------------------------------------

class _PdfTableOut(BaseModel):
    caption: Optional[str] = Field(
        None, description="Judul tabel persis seperti tercetak, mis. 'Lampiran 2. Pertumbuhan …'."
    )
    unit: Optional[str] = Field(
        None, description="Anotasi unit dalam tanda kurung, mis. 'Triliun Rp' atau '%, yoy'."
    )
    header_rows: List[List[str]] = Field(
        default_factory=list,
        description=(
            "Baris header kolom, 1-2 baris. Bila baris tahun berada di atas baris bulan/triwulan, "
            "kirim keduanya: baris tahun lebih dahulu."
        ),
    )
    rows: List[List[str]] = Field(
        default_factory=list,
        description="Baris data. Sel pertama setiap baris adalah label baris.",
    )


class _PageTables(BaseModel):
    tables: List[_PdfTableOut] = Field(default_factory=list)


_TABLE_VISION_PROMPT = """\
Gambar berikut adalah satu halaman dari laporan statistik Bank Indonesia.

Tugasmu: transkripsi SETIAP TABEL DATA di halaman ini, sel per sel, apa adanya.

ATURAN YANG TIDAK BOLEH DILANGGAR:
1. Salin angka PERSIS seperti tercetak. Pertahankan titik ribuan dan koma desimal Indonesia
   ("10.415,9" tetap "10.415,9"), pertahankan tanda kurung akuntansi untuk nilai negatif
   ("(49,8)" tetap "(49,8)"), pertahankan tanda "%" bila tercetak.
2. JANGAN menghitung, membulatkan, mengubah format, menerjemahkan, atau menebak apa pun.
   Sel yang kosong di halaman ditulis sebagai "" (string kosong) — jangan diisi.
3. Setiap baris data harus punya JUMLAH SEL YANG SAMA dengan baris header. Sel pertama adalah
   label baris, ditulis lengkap beserta penanda catatan kaki ("*", "**") seperti tercetak.
4. Bila header waktu tersusun dua tingkat (baris tahun di atas baris bulan/triwulan), kirim
   DUA baris header: baris tahun lebih dahulu, lalu baris bulan/triwulan. Baris bulan/triwulan
   harus LENGKAP dan berurutan sesuai cetakan — satu token untuk setiap kolom data, tanpa ada
   yang dilewat. Pada baris tahun, tulis setiap tahun di kolom pertama rentangnya.
5. Satu halaman BISA memuat lebih dari satu tabel (mis. "Lampiran 4." dan "Lampiran 5." pada
   halaman yang sama). Keluarkan satu entri terpisah per tabel, berurutan dari atas ke bawah.
6. Salin judul tabel ("Tabel 2. …" / "Lampiran 1. …") dan anotasi unit ("(Triliun Rp)",
   "(%, yoy)") persis seperti tercetak.

YANG DIABAIKAN:
- Paragraf narasi dan kalimat biasa.
- Grafik/chart — label data pada grafik BUKAN tabel.
- Header/footer halaman, nomor halaman, nama departemen.
- Baris "Keterangan:"/catatan kaki di bawah tabel.

Jika halaman ini tidak memuat tabel data sama sekali, kembalikan daftar tabel yang KOSONG.
"""


# ---------------------------------------------------------------------------
# Cell coercion
# ---------------------------------------------------------------------------

# A cell that is a number. Deliberately stricter than pdf_extraction._CELL_NUMERIC_RE, which
# allows an UNBALANCED paren and would therefore turn an enumeration cell like '1)' into 1.0.
# Requires a leading digit, so '-' / 'n.a.' / 'Jan' stay strings.
_NUM_CELL_RE = re.compile(r'^-?\d[\d.,]*%?$|^\(-?\d[\d.,]*%?\)$')


def _coerce_cell(text) -> object:
    """Printed cell text -> int | float | str | None.

    Coercion is mandatory, not cosmetic: every consumer of a grid gates on
    `table_parser_generic._is_number`, which is an `isinstance(v, (int, float))` check. A grid
    of strings parses to an empty table and makes every cell pointer read None.

    Integers come back as `int` rather than `float` on purpose — `_is_year_cell` accepts both,
    but `parse_generic_table` renders categorical column headers with `str(cell)`, where a
    float would produce the column label "2026.0".
    """
    if text is None:
        return None
    s = str(text).strip()
    if not s:
        return None
    if not _NUM_CELL_RE.match(s):
        return s
    value = _parse_indonesian_number(s)
    if value is None:
        return s
    if float(value).is_integer() and ',' not in s:
        return int(value)
    return value


# A trailing parenthetical on a table caption, e.g. 'Tabel 9. Uang Primer (triliun Rp)'.
_CAPTION_UNIT_RE = re.compile(r'\(([^()]{1,40})\)\s*$')

# Words that make a parenthetical a UNIT rather than an aside ('(M2)', '(yoy)', '(2)').
_UNIT_WORDS = re.compile(
    r'\b(rp|rupiah|usd|persen|indeks|index|ribu|juta|miliar|milyar|triliun|trilyun|'
    r'orang|unit|ton|barel|%)\b|%', re.IGNORECASE
)


def _unit_from_caption(caption: str) -> str:
    """Pull the unit out of a caption's trailing parenthetical, or return "".

    BI captions print the unit inline — 'Lampiran 3. Tabel Dana Pihak Ketiga (Triliun Rp)' —
    and the model then often leaves the `unit` field empty because it already copied it into
    the caption. An empty unit is not a harmless gap: for a level claim it sends
    paired_verifier down its no-declared-unit fallback, which normalises the CLAIM's scale
    ('triliun Rp' -> 1e12) against a table already written in trillions, computing ~0.0 and
    reporting a false Refuted against a perfectly good number. Measured on three tables of
    sample_data/M2-April-2026.pdf.
    """
    match = _CAPTION_UNIT_RE.search(caption or "")
    if not match:
        return ""
    inner = match.group(1).strip()
    return inner if _UNIT_WORDS.search(inner) else ""


# ---------------------------------------------------------------------------
# Grid assembly
# ---------------------------------------------------------------------------

def _period_ordinal(cell) -> Optional[int]:
    """1-12 for a month cell, 1-4 for a quarter cell, else None."""
    token = _bare_period_token(cell)
    if token is None:
        return None
    if token in _QUARTER_CANON:
        return _QUARTER_CANON.index(token) + 1
    return _MONTH_ABBREVS.index(token) + 1


def _reconstruct_year_row(year_row: List, period_row: List) -> List:
    """Re-derive which year each period column belongs to, from the PERIOD sequence.

    Measured on the real M2 report, this is the one thing the vision model gets wrong often
    enough to matter — and wrong in the worst possible way, since a mis-dated column is a
    plausible-looking number filed under the wrong month:

      Lampiran 1  year row: [·, 2025, 2026, ·, ·, …]   (each year written once, and 2026
                            landing on 'Mei' — a 2025 column)
      Lampiran 2  year row: [·, 2025 ×12, 2026 ×3]     (12 evenly spread years, so Jan/Feb/Mar
                            2026 come out labelled 2025)

    The PERIOD row, by contrast, is copied correctly every time — it is a plain row of labels
    with no spanning to reason about. So the periods are the source of truth: they run in
    calendar order, and each wrap (Des -> Jan, or Q4 -> Q1) starts the next year. The year row
    is used only for the SET of years involved, never for their positions.

    Rewrites the row only when the reconstruction is unambiguous: the number of year blocks the
    period sequence implies must equal the number of distinct years written in the row. Anything
    else is left exactly as transcribed, and the parser cascade decides whether it is usable —
    better a table that fails to parse than one that parses into wrongly dated values.
    """
    ordinals = [(i, _period_ordinal(v)) for i, v in enumerate(period_row)]
    period_cols = [(i, o) for i, o in ordinals if o is not None]
    if len(period_cols) < 2:
        return year_row

    years = sorted({v for v in year_row if isinstance(v, int) and 1990 <= v <= 2100})
    if not years:
        return year_row

    # Walk the period columns in order; a non-increasing period starts a new year block.
    blocks: List[int] = []
    block = 0
    previous: Optional[int] = None
    for _, ordinal in period_cols:
        if previous is not None and ordinal <= previous:
            block += 1
        blocks.append(block)
        previous = ordinal

    if block + 1 != len(years):
        logger.info(
            "Leaving the transcribed year row alone: periods imply %d year block(s) but %d "
            "distinct year(s) were written (%s).", block + 1, len(years), years,
        )
        return year_row

    out: List = [None] * len(year_row)
    for (col, _), block_index in zip(period_cols, blocks):
        while col >= len(out):
            out.append(None)
        out[col] = years[block_index]
    return out


def _assemble_grid(table: _PdfTableOut) -> List[List]:
    """Lay a transcribed table out as a grid the existing Excel parsers already understand.

    Row 0 is the caption alone and row 1 the parenthesised unit alone, because
    `table_parser_generic._title_and_unit` reads exactly that shape (a fully-parenthesised row
    is the unit, the first other row is the title) and `_find_header_row` skips any row with
    fewer than 2 non-empty cells — so neither can be mistaken for the header.
    """
    header_rows = [[_coerce_cell(c) for c in r] for r in table.header_rows]
    # A year row stacked over a period row: re-derive the years from the periods (see
    # _reconstruct_year_row). Body rows are never touched — carrying a value sideways there
    # would invent data.
    if len(header_rows) >= 2:
        header_rows[-2] = _reconstruct_year_row(header_rows[-2], header_rows[-1])
    body_rows = [[_coerce_cell(c) for c in r] for r in table.rows]

    grid: List[List] = []
    caption = (table.caption or "").strip()
    if caption:
        grid.append([caption])
    unit = (table.unit or "").strip().strip("()") or _unit_from_caption(caption)
    if unit:
        grid.append([f"({unit})"])
    grid.extend(header_rows)
    grid.extend(body_rows)

    width = max((len(r) for r in grid), default=0)
    return [r + [None] * (width - len(r)) for r in grid]


def _is_usable(table: _PdfTableOut, page_number: int) -> bool:
    """Reject a transcription that cannot be a faithful table before it becomes a source.

    Cheap structural guards, not semantic ones — they catch the shapes a garbled or truncated
    transcription actually takes.
    """
    caption = (table.caption or "").strip() or "(tanpa judul)"
    if not table.header_rows or len(table.rows) < 2:
        logger.info(
            "Dropping transcribed table on page %d (%s): %d header row(s), %d body row(s)",
            page_number, caption, len(table.header_rows), len(table.rows),
        )
        return False
    if not any(_coerce_cell(c) is not None and not isinstance(_coerce_cell(c), str)
               for row in table.rows for c in row):
        logger.info(
            "Dropping transcribed table on page %d (%s): no numeric cell in the body",
            page_number, caption,
        )
        return False
    # A body row identical to the header row is the fingerprint of two stacked tables
    # transcribed as one — the second table's header landed in the first table's body.
    header_sig = [str(c).strip() for c in table.header_rows[-1]]
    if any([str(c).strip() for c in row] == header_sig for row in table.rows):
        logger.info(
            "Dropping transcribed table on page %d (%s): a body row repeats the header row",
            page_number, caption,
        )
        return False
    return True


# ---------------------------------------------------------------------------
# Text-layer verification: where the file states the numbers, the file wins
# ---------------------------------------------------------------------------

# A text-layer line needs a run of at least this many numeric tokens at its end to be read as a
# table row. 3 is the floor prose never reaches: numbers in a sentence are separated by words,
# so its longest trailing run is 1-2 (the same reasoning as pdf_extraction._trailing_numeric_run).
_MIN_ROW_CELLS = 3


def _norm_label(text) -> str:
    """Collapse a row label to comparable form: letters and digits only, lowercased.

    PDFium reproduces BI labels with stray spacing around hyphens ('Faktor -Faktor') and the
    model does not, so anything but alphanumerics has to go before the two can be compared.
    """
    return re.sub(r'[^a-z0-9]', '', str(text).lower())


# How similar two normalised labels must be to be the same row. Below this, a pair like
# 'kreditinvestasi' / 'kreditkonsumsi' (~0.55) must not be treated as a match.
_LABEL_SIM = 0.85
# Shortest label for which one being a prefix of the other is evidence rather than coincidence.
_PREFIX_MIN = 12


def _labels_match(transcribed: str, printed: str) -> bool:
    """Whether a transcribed row label could be a garbled rendering of a printed one.

    Exact equality is not enough, because a BI table prints long labels into columns too narrow
    to hold them and the page is VISUALLY truncated — the model copies what it can see
    ('Industri Pengolahan dan sejenisny.', 'Perdagangan, Hotel, dan Restorar') while the text
    layer carries the whole string. Both a clean truncation and a misread final character have
    to be tolerated, or those rows lose their values for no good reason.

    Only ever consulted for labels that have NO exact counterpart (see _align_rows). On its own
    this test is too generous to be safe: 'Giro Bank Umum di BI' is a legitimate prefix of the
    separate row 'Giro Bank Umum di BI Adjusted 2)', and pairing those two put one row's numbers
    under the other's name — measured on page 10 of the M2 report.
    """
    if transcribed == printed:
        return True
    if len(transcribed) >= _PREFIX_MIN and printed.startswith(transcribed):
        return True
    if len(printed) >= _PREFIX_MIN and transcribed.startswith(printed):
        return True
    return difflib.SequenceMatcher(None, transcribed, printed).ratio() >= _LABEL_SIM


def _align_rows(transcribed: List[str], printed: List[str]) -> Dict[int, int]:
    """Map transcribed row index -> text-layer row index, preserving printed order.

    A longest-common-subsequence alignment rather than a label lookup: a Lampiran repeats
    sub-labels ('Pertambangan dan Penggalian' once per credit type, 'Rupiah' and 'Valas' under
    several parents), so order is the only thing that tells two same-named rows apart. Keeping
    the alignment monotonic is also what stops a fuzzy label match from pairing rows that sit in
    different parts of the table.

    Fuzzy matching is a LAST RESORT, permitted only between two labels that each have no exact
    counterpart on the other side. When a label appears verbatim in both sequences there is
    nothing to guess about, and guessing anyway is what mispaired 'Giro Bank Umum di BI Adjusted
    2)' with 'Giro Bank Umum di BI'.
    """
    transcribed_set, printed_set = set(transcribed), set(printed)

    def match(a: str, b: str) -> bool:
        if a == b:
            return True
        if a in printed_set or b in transcribed_set:
            return False
        return _labels_match(a, b)

    n, m = len(transcribed), len(printed)
    # best[i][j] = size of the best alignment of transcribed[i:] against printed[j:]
    best = [[0] * (m + 1) for _ in range(n + 1)]
    for i in range(n - 1, -1, -1):
        for j in range(m - 1, -1, -1):
            if match(transcribed[i], printed[j]):
                best[i][j] = 1 + best[i + 1][j + 1]
            else:
                best[i][j] = max(best[i + 1][j], best[i][j + 1])

    pairs: Dict[int, int] = {}
    i = j = 0
    while i < n and j < m:
        if match(transcribed[i], printed[j]) and best[i][j] == 1 + best[i + 1][j + 1]:
            pairs[i] = j
            i += 1
            j += 1
        elif best[i + 1][j] >= best[i][j + 1]:
            i += 1
        else:
            j += 1
    return pairs


def _text_layer_rows(page_text: str) -> List[Tuple[str, List[float]]]:
    """The (label, values) pairs a page's text layer states, in printed order.

    A row is a line ending in a run of >= _MIN_ROW_CELLS numeric cells; everything before that
    run is the label. Returns [] for a page with no text layer, which is the scanned case.
    """
    rows: List[Tuple[str, List[float]]] = []
    for line in page_text.split("\n"):
        tokens = line.split()
        run = 0
        for token in reversed(tokens):
            if not _NUM_CELL_RE.match(token):
                break
            run += 1
        if run < _MIN_ROW_CELLS or run == len(tokens):
            continue
        label = " ".join(tokens[:len(tokens) - run]).strip()
        values = [_parse_indonesian_number(t) for t in tokens[-run:]]
        if not label or any(v is None for v in values):
            continue
        rows.append((label, values))
    return rows


def _verify_against_text_layer(tables: List["PdfTable"], pages_text: List[str]) -> None:
    """Replace transcribed numbers with the ones the PDF itself states. Mutates `tables`.

    Rows are aligned as SEQUENCES, not looked up by label — see _align_rows.

    A matched row's values are taken from the text layer wholesale — that repairs a misread
    digit AND a value row attached to the wrong label, the failure mode that defeats every
    structural check.

    Two kinds of row are EMPTIED rather than patched, which makes the invariant absolute: on a
    page with a text layer, every number a grid still carries was read out of the file by code.
      - the cell COUNT disagrees. The text layer says how many values the row has but not which
        columns its gaps are in, so there is no honest way to place them.
      - no text-layer row aligned to it. On a page whose text layer we can read, a real table
        row has to be in there; failing to find it means we cannot vouch for the values, and in
        practice these are the rows whose labels the model emitted out of order — the very ones
        most likely to be carrying another row's numbers.
    An emptied row simply stops being an answer (parse_generic_grid keeps no label that has no
    value), which sends the claim to another source or to Inconclusive rather than to a wrong
    verdict.
    """
    by_page: Dict[int, List["PdfTable"]] = {}
    for table in tables:
        by_page.setdefault(table.page_number, []).append(table)

    for page_number, page_tables in by_page.items():
        page_text = pages_text[page_number - 1] if page_number <= len(pages_text) else ""
        truth = _text_layer_rows(page_text)
        if not truth:
            continue   # scanned page: nothing to check against, transcription stands as-is

        # Every body row on the page, across its tables, in printed order.
        body: List[Tuple["PdfTable", List]] = [
            (table, row)
            for table in sorted(page_tables, key=lambda t: t.index_on_page)
            for row in table.grid
            if row and isinstance(row[0], str) and any(isinstance(c, (int, float)) for c in row[1:])
        ]
        if not body:
            continue

        matched = _align_rows(
            [_norm_label(row[0]) for _, row in body],
            [_norm_label(label) for label, _ in truth],
        )

        fixed = emptied = unmatched = 0
        for index, (table, row) in enumerate(body):
            table.verified = True
            positions = [c for c, cell in enumerate(row) if c and isinstance(cell, (int, float))]
            values = truth[matched[index]][1] if index in matched else None
            if values is None or len(positions) != len(values):
                for c in positions:
                    row[c] = None
                if values is None:
                    unmatched += 1
                else:
                    emptied += 1
                continue
            for c, value in zip(positions, values):
                if abs(row[c] - value) >= 0.05:
                    fixed += 1
                row[c] = value

        logger.info(
            "Page %d verified against the text layer: %d cell(s) corrected, %d row(s) emptied "
            "(cell count disagreed), %d row(s) emptied (no matching text-layer row).",
            page_number, fixed, emptied, unmatched,
        )


# ---------------------------------------------------------------------------
# Transcription cache (in-process, same contract as table_parser_llm._SPEC_CACHE)
# ---------------------------------------------------------------------------

_TABLE_CACHE: "OrderedDict[str, List[PdfTable]]" = OrderedDict()


def _cache_key(pdf_bytes: bytes, dpi: int, model_name: str) -> str:
    h = hashlib.sha256(pdf_bytes)
    h.update(f"|{dpi}|{_PROMPT_VERSION}|{model_name}|{_TABLE_PAGES_PER_CALL}".encode())
    return h.hexdigest()


def _cache_get(key: str) -> Optional[List[PdfTable]]:
    tables = _TABLE_CACHE.get(key)
    if tables is None:
        return None
    _TABLE_CACHE.move_to_end(key)
    return list(tables)


def _cache_put(key: str, tables: List[PdfTable]) -> None:
    _TABLE_CACHE[key] = list(tables)
    _TABLE_CACHE.move_to_end(key)
    while len(_TABLE_CACHE) > _MAX_CACHE_ENTRIES:
        _TABLE_CACHE.popitem(last=False)


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------

async def extract_tables_from_pdf(
    pdf_bytes: bytes,
    vision_llm: BaseChatModel,
    dpi: int = 150,
    on_progress: Optional[Callable[[int, int], None]] = None,
) -> List[PdfTable]:
    """Transcribe every data table in the PDF, in page order.

    Never raises for a readable PDF: a page whose call fails or whose transcription fails the
    structural guards contributes no tables and is logged. An empty result is a valid outcome
    (the caller then finds no internal source and every claim comes back Inconclusive).

    on_progress(done, total) is called after each page's call settles, for stage reporting.
    """
    model_name = getattr(vision_llm, "model", None) or getattr(vision_llm, "model_name", "")
    key = _cache_key(pdf_bytes, dpi, str(model_name))
    cached = _cache_get(key)
    if cached is not None:
        logger.info("PDF table transcription served from cache (%s…): %d table(s)",
                    key[:12], len(cached))
        if on_progress is not None:
            on_progress(1, 1)
        return cached

    logger.info("Rendering PDF pages at %d DPI for table transcription", dpi)
    b64_pages = await asyncio.to_thread(render_pages_to_b64, pdf_bytes, dpi)
    n_pages = len(b64_pages)
    if not n_pages:
        return []

    is_groq, is_gemini = vision_provider_flags(vision_llm)
    if not is_gemini:
        logger.warning(
            "Table transcription is running on a non-Gemini vision provider (%s). Lampiran "
            "pages are large; a small output-token cap will truncate them.",
            type(vision_llm).__name__,
        )

    batches = [
        list(range(i, min(i + _TABLE_PAGES_PER_CALL, n_pages)))
        for i in range(0, n_pages, _TABLE_PAGES_PER_CALL)
    ]
    semaphore, max_retries = plan_vision_concurrency(is_groq, is_gemini, len(batches))
    structured = vision_llm.with_structured_output(_PageTables)
    logger.info(
        "Transcribing tables from %d page(s) in %d vision call(s)", n_pages, len(batches)
    )

    done = 0
    lock = asyncio.Lock()

    async def _transcribe(idxs: List[int]) -> List[PdfTable]:
        page_nums = [i + 1 for i in idxs]

        async def _one_call() -> List[PdfTable]:
            content: list = []
            for i in idxs:
                if len(idxs) > 1:
                    content.append({"type": "text", "text": f"[== Halaman {i + 1} ==]"})
                content.append({
                    "type": "image_url",
                    "image_url": {"url": f"data:image/png;base64,{b64_pages[i]}"},
                })
            content.append({"type": "text", "text": _TABLE_VISION_PROMPT})
            result = await structured.ainvoke([HumanMessage(content=content)])
            # With one page per call (the default) every table belongs to that page. When
            # batching, the model is not asked to attribute tables to pages, so they are all
            # credited to the batch's FIRST page — approximate on purpose; page_number is
            # display/provenance metadata, never used to resolve a value.
            page_number = page_nums[0]
            tables: List[PdfTable] = []
            for out in result.tables:
                if not _is_usable(out, page_number):
                    continue
                caption = (out.caption or "").strip()
                tables.append(PdfTable(
                    page_number=page_number,
                    caption=caption,
                    unit=(out.unit or "").strip().strip("()") or _unit_from_caption(caption),
                    grid=_assemble_grid(out),
                    index_on_page=len(tables),
                ))
            return tables

        tables = await call_vision_with_retry(
            _one_call,
            semaphore=semaphore,
            max_retries=max_retries,
            label=f"table transcription, pages {page_nums}",
            on_give_up=list,
        )
        nonlocal done
        async with lock:
            done += 1
            if on_progress is not None:
                on_progress(done, len(batches))
        return tables

    per_batch = await asyncio.gather(*[_transcribe(idxs) for idxs in batches])
    tables = [t for batch in per_batch for t in batch]

    # The model has now read the layout. Wherever the file states the numbers itself, they
    # replace the transcribed ones — see _verify_against_text_layer.
    try:
        pages_text = await asyncio.to_thread(_extract_pages_raw, pdf_bytes)
        _verify_against_text_layer(tables, pages_text)
    except Exception:
        # A text layer we cannot read leaves the transcription exactly as it was; the tables
        # stay marked unverified, which is the honest outcome, not a reason to fail.
        logger.exception("Text-layer verification skipped")

    logger.info(
        "Transcribed %d table(s) from %d page(s): %s",
        len(tables), n_pages, ", ".join(t.label for t in tables) or "none",
    )
    _cache_put(key, tables)
    return tables
