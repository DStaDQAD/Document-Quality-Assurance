"""Read the data labels printed on a PDF report's charts.

Why this exists: a BI report states most of its numbers three times — in a sentence, in a chart
("Grafik 2 IKK per Kelompok Pengeluaran", with the value printed over each bar) and in an
appendix table. The narrative and the tables were already checked; the charts were read by no
one, so a chart left un-updated from last month, or a bar labelled with the neighbouring group's
value, went straight to print. `paired_verifier` turns every label read here into a checkable
fact against the report's own tables, and offers each chart as a fallback source for the prose.

Why vision: the charts in these reports are vector drawings whose labels are outlined glyphs,
not text — SK-Juni-2026 has no chart number anywhere in its text layer. Only the CAPTIONS are
text, which is what locates each chart: the region from a "Grafik N" caption down to the next
line of text is rendered on its own, about three times sharper than a whole-page render, so 5-6pt
labels stay legible.

What the model is and is not trusted with. It copies a printed label and says which bar/point
and which period it belongs to; it never estimates a value from a bar's height or a line's
position, and a label it cannot place is skipped. A misplaced label is still possible, which is
why paired_verifier reports a chart label that matches the NEIGHBOURING period as exactly that,
rather than as a plain mismatch.
"""

import asyncio
import base64
import hashlib
import io
import logging
import re
import time
from collections import Counter, OrderedDict
from dataclasses import dataclass, field
from typing import Callable, Dict, List, Literal, Optional, Tuple

from langchain_core.exceptions import OutputParserException
from langchain_core.language_models import BaseChatModel
from langchain_core.messages import HumanMessage
from pydantic import BaseModel, Field

from pdf_extraction import (
    VISION_RENDER_MAX_PX,
    call_vision_with_retry,
    plan_vision_concurrency,
    vision_provider_flags,
)
from pdf_table_extraction import _coerce_cell, _document_number_format, _page_lines
from table_model import QUAL_SEP
from table_parser_generic import _MONTH_ABBREVS, _bare_period_token

logger = logging.getLogger("fact-checker")

# Bump when the prompt or the region logic changes: it is part of the cache key.
_PROMPT_VERSION = "2"
_MAX_CACHE_ENTRIES = 8

# A chart caption, matched on the line with every space removed: the text layer spells it
# 'G rafi k 2' (zero-width spaces inside the word).
_CHART_CAPTION_RE = re.compile(r'^(Grafik|Gambar)(\d+)', re.IGNORECASE)
# Lines this close to a caption line (pt) are the rest of its caption box, not the next text.
_CAPTION_BAND_PT = 25.0
# Room kept above the highest caption line, for the coloured caption box around it.
_CAPTION_ABOVE_PT = 16.0
# Side margins trimmed off every region (pt) — outside the text block there is nothing to read.
_SIDE_MARGIN_PT = 30.0
# How far left of its caption text a chart's caption box (and the chart below it) begins.
_CAPTION_LEFT_PT = 18.0


@dataclass
class ChartPoint:
    """One printed data label, placed."""
    series: str
    year: int
    month: str          # canonical period token: 'Jan'..'Dec' or 'Q1'..'Q4'
    value: float
    raw: str            # as printed, e.g. '121,4'
    kind: str = "level"   # "level" | "change"


@dataclass
class ChartReading:
    """The labels read off one chart."""
    page_number: int
    caption: str        # 'Grafik 2'
    title: str          # 'IKK per Kelompok Pengeluaran'
    indicator: str      # 'Indeks Keyakinan Konsumen (IKK)', or '' when each series is one
    unit: str
    points: List[ChartPoint] = field(default_factory=list)

    @property
    def label(self) -> str:
        """Display name used as a source's 'sheet' — e.g. 'Hal. 2 · Grafik 2 IKK per …'."""
        name = " ".join(p for p in (self.caption, self.title) if p).strip() or "Grafik"
        return f"Hal. {self.page_number} · {name[:60]}"

    def metric_label(self, series: str) -> str:
        """The row a point is about: 'indicator > series', or whichever of the two is given."""
        indicator, series = self.indicator.strip(), series.strip()
        if not indicator:
            return series
        if not series or series.lower() == indicator.lower():
            return indicator
        return f"{indicator}{QUAL_SEP}{series}"


# ---------------------------------------------------------------------------
# LLM output schema — strings only, as in pdf_table_extraction: _coerce_cell is the single
# place a printed label becomes a number.
# ---------------------------------------------------------------------------

class _ChartPointOut(BaseModel):
    series: str = Field("", description="Kelompok/kategori atau nama seri pemilik label, persis tercetak.")
    year: str = Field("", description="Tahun posisi label pada sumbu waktu, mis. '2026'.")
    period: str = Field("", description="Bulan/triwulan posisi label, persis tercetak, mis. '6' atau 'Jun'.")
    value: str = Field(
        "", description="Angka label persis seperti tercetak, mis. '121,4'; '' bila tanpa label."
    )
    kind: Literal["level", "change"] = Field(
        "level", description="'change' bila label itu perubahan (mis. 'Δ -8,9'), selain itu 'level'."
    )


class _ChartOut(BaseModel):
    caption: str = Field("", description="Nomor grafik persis tercetak, mis. 'Grafik 2'.")
    title: str = Field("", description="Judul grafik persis tercetak.")
    indicator: str = Field(
        "", description="Besaran yang diukur, nama lengkap bila tercetak; kosong bila tiap seri "
                        "adalah indikator tersendiri.",
    )
    unit: str = Field("", description="Satuan sumbu, mis. 'Indeks' atau '% thdp pendapatan'.")
    points: List[_ChartPointOut] = Field(default_factory=list)


class _PageCharts(BaseModel):
    charts: List[_ChartOut] = Field(default_factory=list)


_CHART_VISION_PROMPT = """\
Gambar berikut adalah SATU grafik (atau peta) dari laporan statistik Bank Indonesia.
Periode laporan: {period}.

Tugasmu: salin LABEL ANGKA yang tercetak pada grafik ini, dan untuk setiap label tentukan
dengan cermat batang/titik mana pemiliknya.

Isi:
- caption: nomor grafik persis tercetak, mis. "Grafik 2" atau "Gambar 1".
- title: judul grafik persis tercetak.
- indicator: besaran yang diukur grafik, dengan nama lengkap bila tercetak pada judul, legenda,
  atau sumbu (mis. "Indeks Keyakinan Konsumen (IKK)", "Indeks Penghasilan Saat Ini", "Rasio
  Konsumsi"). Bila setiap garis/legenda adalah indikator yang berbeda (mis. IKK, IKE, dan IEK
  dalam satu grafik), kosongkan indicator dan tulis nama indikator itu pada series.
- unit: satuan sumbu, mis. "Indeks" atau "% thdp pendapatan".
- points: daftar tanda data (lihat cara menulisnya di bawah), masing-masing:
  - series: kelompok/kategori, nama garis/legenda, atau nama wilayah, persis tercetak
    (mis. "Rp1 - 2 juta", "20 - 30 tahun", "Indeks Keyakinan Konsumen (IKK)", "MEDAN").
  - year: tahun posisi tanda pada sumbu waktu, mis. "2026".
  - period: bulan/triwulan posisi tanda pada sumbu waktu, persis tercetak, mis. "6".
  - value: angka label PERSIS seperti tercetak, mis. "121,4" (koma desimal tetap koma), atau ""
    bila tanda itu tidak berlabel.
  - kind: "change" bila label itu perubahan (mis. "(Δ -8,9)"), selain itu "level".

CARA MENULIS points:
A. GRAFIK BATANG: tulis SETIAP batang, kelompok demi kelompok, batang demi batang dari kiri ke
   kanan — termasuk batang yang TIDAK berlabel (value ""). Periode setiap batang dibaca dari
   angka sumbu tepat di bawah batang itu (mis. "4 5 6" di atas "2026" = April, Mei, Juni 2026).
   Grafik sering hanya memberi label pada SEBAGIAN batang (mis. dua batang terakhir saja). Untuk
   menentukan pemilik label, bandingkan posisi horizontal TENGAH angka label dengan TENGAH setiap
   batang: label milik batang yang tengahnya paling dekat, dan angka sumbu di bawah batang itulah
   periodenya. Jangan berasumsi label pertama milik batang pertama.
B. GRAFIK GARIS: tulis hanya titik yang berlabel. Label milik titik data terdekat pada garis
   yang WARNANYA sama dengan label; periodenya adalah posisi titik itu pada sumbu waktu (hitung
   tik bulannya). Legenda menentukan nama seri untuk setiap warna.
C. PETA atau grafik tanpa sumbu waktu: satu tanda per label, periodenya periode laporan.

ATURAN YANG TIDAK BOLEH DILANGGAR:
1. Salin HANYA angka yang benar-benar tercetak sebagai label data. JANGAN menaksir nilai dari
   tinggi batang, posisi titik, atau bentuk garis.
2. Bila kamu tidak yakin label itu milik batang/titik yang mana — kelompoknya, serinya, atau
   periodenya — JANGAN tulis label itu. Label yang dilewati lebih baik daripada salah tempat.
3. JANGAN menyalin angka skala sumbu (80, 100, 120, …) atau nomor grafik.

Jika gambar tidak memuat grafik, kembalikan daftar grafik yang KOSONG.
"""


# ---------------------------------------------------------------------------
# Locating charts
# ---------------------------------------------------------------------------

def _compact(text: str) -> str:
    return re.sub(r"\s+", "", text)


def _caption_xs(pdf_bytes: bytes) -> Dict[int, List[Tuple[float, float]]]:
    """1-based page -> [(left x, top y)] of every 'Grafik N'/'Gambar N' spelled in the text layer.

    Read glyph by glyph because the visual lines carry no x, and a caption row that holds two
    charts side by side has to be split between them.
    """
    import pypdfium2 as pdfium

    found: Dict[int, List[Tuple[float, float]]] = {}
    doc = pdfium.PdfDocument(pdf_bytes)
    try:
        for index in range(len(doc)):
            page = doc[index]
            textpage = page.get_textpage()
            glyphs: List[Tuple[str, float, float]] = []
            for i in range(textpage.count_chars()):
                char = textpage.get_text_range(i, 1)
                if char.strip():
                    left, _, _, top = textpage.get_charbox(i)
                    glyphs.append((char, left, top))
            text = "".join(g[0] for g in glyphs)
            hits = [(glyphs[m.start()][1], glyphs[m.start()][2])
                    for m in re.finditer(r"(?:Grafik|Gambar)\d+", text, re.IGNORECASE)]
            if hits:
                found[index + 1] = hits
            textpage.close()
            page.close()
    finally:
        doc.close()
    return found


def chart_regions(pdf_bytes: bytes) -> Dict[int, List[Tuple[float, float, float, float]]]:
    """1-based page -> [(left, bottom, right, top)] in PDF points, one box per chart.

    Vertically a box runs from just above its caption box down to the first line of text below
    it — the chart itself has no text layer. Horizontally, charts set side by side under one
    caption row ('Grafik 2 … Grafik 3 …') are split at the next caption's x, so each chart is
    read on its own at the full resolution budget.
    """
    xs = _caption_xs(pdf_bytes)
    regions: Dict[int, List[Tuple[float, float, float, float]]] = {}
    for page_number, (_reading, visual) in _page_lines(pdf_bytes).items():
        lines = sorted(visual, key=lambda line: -line[0])     # top of the page first
        boxes: List[Tuple[float, float, float, float]] = []
        consumed_below: Optional[float] = None
        for y, text in lines:
            if consumed_below is not None and y >= consumed_below:
                continue
            if not _CHART_CAPTION_RE.match(_compact(text)):
                continue
            band = [ly for ly, _ in lines if abs(ly - y) <= _CAPTION_BAND_PT]
            top = max(band) + _CAPTION_ABOVE_PT
            below = [ly for ly, _ in lines if ly < min(band)]
            bottom = max(below) if below else 0.0
            consumed_below = min(band)
            lefts = sorted(x for x, ty in xs.get(page_number, [])
                           if min(band) - 2 <= ty <= max(band) + 2)
            if len(lefts) < 2:
                boxes.append((_SIDE_MARGIN_PT, bottom, -1.0, top))
                continue
            edges = [x - _CAPTION_LEFT_PT for x in lefts] + [-1.0]
            for left, right in zip(edges, edges[1:]):
                boxes.append((left, bottom, right, top))
        if boxes:
            regions[page_number] = boxes
    return regions


# A pixel lighter than this is paper.
_PAPER_LUMA = 235
_INK_PAD_PT = 4.0
# A blank band this tall (pt) ends the chart: below it is page art, not the chart. Only once
# the chart has had room to be drawn — a map sits well below its caption box, with a gap wider
# than this between them, and no chart in these reports is shorter than _MIN_CHART_PT.
_BLANK_GAP_PT = 20.0
_MIN_CHART_PT = 100


def _ink_box(page, box: Tuple[float, float, float, float]) -> Tuple[float, float, float, float]:
    """The box shrunk to the chart drawn at its top, so the resolution budget goes to the chart.

    A chart with no text below it runs to the page footer: on SK-Juni-2026 that left the bottom
    half of Grafik 20's image blank but for the page's decorative wave. The chart is the inked
    block that starts at the top of the box and ends at the first tall blank band. Measured on a
    cheap 1-px-per-point render; the original box is kept when nothing is drawn in it.
    """
    import numpy as np

    left, bottom, right, top = box
    width, height = page.get_size()
    image = page.render(
        scale=1.0, crop=(left, bottom, width - right, height - top)
    ).to_pil().convert("L")
    ink = np.asarray(image) < _PAPER_LUMA
    rows = ink.any(axis=1)
    if not rows.any():
        return box
    first = int(np.argmax(rows))
    last = first
    blank = 0
    for y in range(first, len(rows)):
        if rows[y]:
            last, blank = y, 0
        else:
            blank += 1
            if blank >= _BLANK_GAP_PT and y - first >= _MIN_CHART_PT:
                break
    cols = ink[first:last + 1].any(axis=0)
    x0 = int(np.argmax(cols))
    x1 = len(cols) - int(np.argmax(cols[::-1]))
    return (
        max(left, left + x0 - _INK_PAD_PT),
        max(bottom, top - (last + 1) - _INK_PAD_PT),
        min(right, left + x1 + _INK_PAD_PT),
        min(top, top - first + _INK_PAD_PT),
    )


def _render_regions(
    pdf_bytes: bytes, regions: Dict[int, List[Tuple[float, float, float, float]]], max_px: int
) -> List[Tuple[int, str]]:
    """[(page_number, base64 PNG)] — each box rendered so its long side is max_px.

    A right edge of -1 means "to the right margin".
    """
    import pypdfium2 as pdfium

    out: List[Tuple[int, str]] = []
    doc = pdfium.PdfDocument(pdf_bytes)
    try:
        for page_number in sorted(regions):
            page = doc[page_number - 1]
            try:
                width, height = page.get_size()
                for left, bottom, right, top in regions[page_number]:
                    right = width - _SIDE_MARGIN_PT if right < 0 else right
                    left, top = max(left, 0.0), min(top, height)
                    left, bottom, right, top = _ink_box(page, (left, bottom, right, top))
                    scale = max_px / max(right - left, top - bottom, 1.0)
                    # crop = margins, in points, off the (left, bottom, right, top) page edges.
                    bitmap = page.render(
                        scale=scale, crop=(left, bottom, width - right, height - top),
                    )
                    buffer = io.BytesIO()
                    bitmap.to_pil().save(buffer, format="PNG")
                    out.append((page_number, base64.b64encode(buffer.getvalue()).decode("ascii")))
            finally:
                page.close()
    finally:
        doc.close()
    return out


# The report's own period, for charts that have no time axis: the '<Bulan> <Tahun>' the text
# layer names most often ('Juni 2026' is written a dozen times in SK-Juni-2026).
_ID_MONTHS = ("januari februari maret april mei juni juli agustus september oktober "
              "november desember").split()
_MONTH_YEAR_RE = re.compile(r"\b(" + "|".join(_ID_MONTHS) + r")\s+((?:19|20)\d\d)\b", re.IGNORECASE)


def report_period(pdf_bytes: bytes) -> str:
    """'Juni 2026', or '' when the text layer names no month and year."""
    counts: Counter = Counter()
    for reading, _visual in _page_lines(pdf_bytes).values():
        for _, text in reading:
            for month, year in _MONTH_YEAR_RE.findall(text):
                counts[f"{month.capitalize()} {year}"] += 1
    return counts.most_common(1)[0][0] if counts else ""


# ---------------------------------------------------------------------------
# From the model's strings to placed numbers
# ---------------------------------------------------------------------------

def _period_token(raw: str) -> Optional[str]:
    """'6' -> 'Jun', 'Jun'/'Juni' -> 'Jun', 'Q2'/'II' -> 'Q2'; None when it names no period."""
    text = (raw or "").strip()
    if re.fullmatch(r"\d{1,2}", text):
        number = int(text)
        return _MONTH_ABBREVS[number - 1] if 1 <= number <= 12 else None
    return _bare_period_token(text)


def _year(raw: str) -> Optional[int]:
    match = re.search(r"(?:19|20)\d\d", raw or "")
    return int(match.group(0)) if match else None


# "(sb. kanan)" / "(sb. Kiri)": which axis a series is drawn against, not part of its name.
_AXIS_NOTE_RE = re.compile(r"\(\s*sb\.?\s*(?:kanan|kiri)\s*\)", re.IGNORECASE)
# "Indeks Penghasilan Saat Ini per Kelompok Usia": the indicator is what comes before "per".
_BREAKDOWN_TITLE_RE = re.compile(r"^(.+?)\s+per\s+\S", re.IGNORECASE)


def _indicator(out: _ChartOut) -> str:
    """The indicator the chart measures; from its title when the model left it blank.

    A breakdown chart names its indicator only in its title ("Indeks Ekspektasi Penghasilan per
    Kelompok Pengeluaran"), and without it a label is just 'Rp1 - 2 juta' — a row that nine
    indices of the same appendix table share. A title without "per" names a trend chart whose
    series ARE the indicators, and is left alone.
    """
    indicator = (out.indicator or "").strip()
    if indicator:
        return indicator
    match = _BREAKDOWN_TITLE_RE.match(re.sub(r"\s+", " ", out.title or "").strip())
    return match.group(1).strip() if match else ""


def _to_reading(out: _ChartOut, page_number: int, number_format: str,
                fallback: Tuple[Optional[int], Optional[str]]) -> Optional[ChartReading]:
    """A ChartReading from one transcribed chart, keeping only the points it can place."""
    points: List[ChartPoint] = []
    dropped = 0
    for p in out.points:
        if not p.value.strip():
            continue    # an unlabelled bar, listed only so the labelled ones sit in place
        value = _coerce_cell(p.value.replace("Δ", "").strip(" ()"), number_format)
        year = _year(p.year) or fallback[0]
        month = _period_token(p.period) or (fallback[1] if not p.period.strip() else None)
        if not isinstance(value, (int, float)) or year is None or month is None:
            dropped += 1
            continue
        series = re.sub(r"\s+", " ", _AXIS_NOTE_RE.sub("", p.series)).strip()
        if p.series.strip() and not re.search(r"\w", series):
            # A ditto mark ('"') or stray punctuation for a name: whichever group it stands
            # for, the label cannot be tied to it.
            dropped += 1
            continue
        points.append(ChartPoint(series=series, year=year, month=month,
                                 value=float(value), raw=p.value.strip(), kind=p.kind))
    if dropped:
        logger.info("%s on page %d: %d label(s) could not be placed and were skipped.",
                    out.caption or "A chart", page_number, dropped)
    if not points:
        return None
    return ChartReading(
        page_number=page_number,
        caption=(out.caption or "").strip(),
        title=(out.title or "").strip(),
        indicator=_indicator(out),
        unit=(out.unit or "").strip().strip("()"),
        points=points,
    )


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------

_CHART_CACHE: "OrderedDict[str, List[ChartReading]]" = OrderedDict()

# Output budget for one chart. Every bar of a 5 x 3 bar chart listed in full is ~2k tokens; the
# cap exists for the other case — a run of newlines inside one string field (seen on
# SK-Juni-2026) that otherwise continues to the model's own limit and holds the whole pass for
# minutes before failing to parse anyway.
_CHART_MAX_OUTPUT_TOKENS = 8192


def _capped(vision_llm: BaseChatModel) -> BaseChatModel:
    """The same model with the chart output budget, where the model class has such a setting."""
    fields = getattr(type(vision_llm), "model_fields", None)
    if isinstance(fields, dict) and "max_output_tokens" in fields:
        return vision_llm.model_copy(update={"max_output_tokens": _CHART_MAX_OUTPUT_TOKENS})
    return vision_llm


def _cache_key(pdf_bytes: bytes, model_name: str) -> str:
    h = hashlib.sha256(pdf_bytes)
    h.update(f"|charts|{_PROMPT_VERSION}|{model_name}|{VISION_RENDER_MAX_PX}".encode())
    return h.hexdigest()


async def extract_charts_from_pdf(
    pdf_bytes: bytes,
    vision_llm: Optional[BaseChatModel],
    on_progress: Optional[Callable[[int, int], None]] = None,
    on_unread: Optional[Callable[[List[int]], None]] = None,
) -> List[ChartReading]:
    """Every chart's printed labels, in page order. Never raises for a readable PDF.

    A PDF whose text layer names no chart caption (a scan, or a report without charts) costs no
    vision call and returns []; so does `vision_llm=None`. A region whose call fails contributes
    nothing — the other charts still come back — and its page is reported through `on_unread`,
    so the reader is told which charts went unchecked rather than seeing fewer of them.
    """
    if vision_llm is None:
        return []
    model_name = getattr(vision_llm, "model", None) or getattr(vision_llm, "model_name", "")
    key = _cache_key(pdf_bytes, str(model_name))
    if key in _CHART_CACHE:
        _CHART_CACHE.move_to_end(key)
        if on_progress is not None:
            on_progress(1, 1)
        return list(_CHART_CACHE[key])

    try:
        regions = await asyncio.to_thread(chart_regions, pdf_bytes)
    except Exception:
        logger.exception("Could not locate charts; skipping the chart pass")
        return []
    if not regions:
        return []
    number_format = await asyncio.to_thread(_document_number_format, pdf_bytes)
    period = await asyncio.to_thread(report_period, pdf_bytes)
    fallback = (_year(period), _period_token(period.split()[0]) if period else None)
    images = await asyncio.to_thread(_render_regions, pdf_bytes, regions, VISION_RENDER_MAX_PX)
    logger.info("Reading %d chart region(s) on %d page(s)", len(images), len(regions))

    is_groq, is_gemini = vision_provider_flags(vision_llm)
    semaphore, max_retries = plan_vision_concurrency(is_groq, is_gemini, len(images))
    structured = _capped(vision_llm).with_structured_output(_PageCharts)
    prompt = _CHART_VISION_PROMPT.format(period=period or "tidak diketahui")
    done = 0
    lock = asyncio.Lock()
    unread: set = set()

    async def _read(page_number: int, b64: str) -> List[ChartReading]:
        async def _call() -> List[ChartReading]:
            message = [HumanMessage(content=[
                {"type": "image_url", "image_url": {"url": f"data:image/png;base64,{b64}"}},
                {"type": "text", "text": prompt},
            ])]
            try:
                result = await structured.ainvoke(message)
            except OutputParserException:
                # The model occasionally runs away inside one string field (a series name of
                # nothing but newlines) and the structure fails to parse. Asked again, it
                # usually does not; asked a third time it is not worth the wait.
                logger.info("Chart labels on page %d did not parse; asking once more.",
                            page_number)
                result = await structured.ainvoke(message)
            readings = [_to_reading(c, page_number, number_format, fallback)
                        for c in result.charts]
            return [r for r in readings if r is not None]

        started = time.monotonic()
        readings = await call_vision_with_retry(
            _call, semaphore=semaphore, max_retries=max_retries,
            label=f"chart labels, page {page_number}", on_give_up=lambda: None,
        )
        logger.info("Chart region on page %d: %s in %.1fs", page_number,
                    "unread" if readings is None else f"{len(readings)} chart(s)",
                    time.monotonic() - started)
        if readings is None:
            unread.add(page_number)
            readings = []
        nonlocal done
        async with lock:
            done += 1
            if on_progress is not None:
                on_progress(done, len(images))
        return readings

    per_region = await asyncio.gather(*[_read(page, b64) for page, b64 in images])
    readings = [r for region in per_region for r in region]
    logger.info(
        "%d chart(s) read, %d label(s): %s", len(readings), sum(len(r.points) for r in readings),
        ", ".join(r.label for r in readings) or "none",
    )
    if unread:
        # Not cached: a failed call is worth making again on the next run.
        if on_unread is not None:
            on_unread(sorted(unread))
        return readings
    _CHART_CACHE[key] = list(readings)
    _CHART_CACHE.move_to_end(key)
    while len(_CHART_CACHE) > _MAX_CACHE_ENTRIES:
        _CHART_CACHE.popitem(last=False)
    return readings
