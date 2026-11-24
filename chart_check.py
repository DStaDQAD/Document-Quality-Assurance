"""The chart check (cek grafik), inside strict: every chart is read by the model (its series mapped to
table rows, its printed labels listed), each label is a strict value claim, and the drawing is judged
against the tables' series. See docs/superpowers/specs/2026-11-23-chart-check-design.md.

Two model calls per chart: `read_chart` (the picture + the tables' rows, no values) and
`judge_drawing` (the picture + the mapped series' data). The labels are judged by the program.
"""
import asyncio
import base64
import logging
import re
from typing import Callable, Dict, List, Literal, Optional, Tuple

from langchain_core.language_models import BaseChatModel
from pydantic import BaseModel, ValidationError, field_validator

from paired_verifier import _period_ordinal

from chart_images import ChartImage, ScannedPage
from chart_vision import NO_IMAGE, ChartUnreadable, ask_chart, locate_charts
from copilot_chat import extract_json
from schemas import ChartCheck, FactVerificationResult
from strict.claims import Cell, Claim
from strict.evaluate import evaluate_claim
from strict.tables import ANNUAL, TableView, _fold, find_column, find_row, parse_period, period_header, table_block

logger = logging.getLogger("fact-checker")


def _text_or_empty(value):
    """The agent writes null for a missing name or title (a chart with no legend); read it as ""."""
    return "" if value is None else str(value)


class SeriesRef(BaseModel):
    name: str = ""                    # as in the legend; for a chart with no legend, what it measures
    table: Optional[str] = None       # "S1"; None when no table holds it
    row: Optional[str] = None         # exact row name from TABLES
    growth: bool = False              # the chart draws the yoy growth of a row the table holds as levels
    unit: Optional[str] = None        # this series' unit when it has its own axis ("Indeks" left, "%" right)

    _name = field_validator("name", mode="before")(_text_or_empty)

    @field_validator("table", mode="before")
    @classmethod
    def _table(cls, value):
        # A code the model wrote as a number (1) is the code it stands for ("S1").
        return f"S{value}" if isinstance(value, int) and not isinstance(value, bool) else value

    @field_validator("growth", mode="before")
    @classmethod
    def _growth(cls, value):
        # null is "no"; "false" / "no" as text are parsed by pydantic, never bool("false") == True.
        return False if value is None else value


class ChartLabel(BaseModel):
    text: str                         # as printed, "12,97"
    series: str = ""                  # a SeriesRef.name
    period: str = ""                  # as the tables write it: "Q2 2026", "Jun 2026", "Tahun 2025"

    _strings = field_validator("series", "period", mode="before")(_text_or_empty)

    @field_validator("text", mode="before")
    @classmethod
    def _text(cls, value):
        # A label the model sent as a JSON number (1.287) is a decimal point, not thousands: written
        # back the way the report prints it ("1,287") so parse_label and the decimals read it right.
        if isinstance(value, (int, float)) and not isinstance(value, bool):
            return repr(value).replace(".", ",")
        return _text_or_empty(value)


class ChartRead(BaseModel):
    title: str = ""
    unit: Optional[str] = None        # the labels' / axis's unit, "miliar dolar AS", "%"
    last_period: Optional[str] = None
    series: List[SeriesRef] = []
    labels: List[ChartLabel] = []

    _title = field_validator("title", mode="before")(_text_or_empty)


READ_PROMPT = """Terlampir gambar sebuah grafik dari laporan statistik Bank Indonesia ({caption}{title}).
Jika kamu tidak menerima atau tidak bisa melihat gambar, jawab persis '{no_image}'.

TABLES di bawah adalah tabel Excel yang diunggah: kode, judul, bentuk periode, dan nama baris. Nilainya
sengaja tidak ditampilkan.

Tugas:
1. title: judul grafik bila ada. unit: satuan angka pada grafik (mis. 'miliar dolar AS', '%'). last_period:
   periode terakhir yang tergambar, ditulis seperti TABLES menulis periode ('Q3 2026', 'Sep 2026', 'Tahun 2025').
2. series: setiap seri di legenda. name disalin persis dari legenda; bila grafik tidak punya legenda (satu
   seri), name = apa yang diukur grafik menurut judul atau sumbunya. table dan row: kode tabel dan nama baris
   yang disalin PERSIS dari TABLES dan memuat data seri itu; null bila tidak ada tabel yang memuatnya. Jangan
   memilih baris yang hanya mirip. Bila seri grafik adalah pertumbuhan (% yoy, % qtq, % mtm) dan TABLES sudah
   punya baris pertumbuhan itu (mis. di bawah 'TAHUNAN (YOY)', 'TRIWULANAN (QTQ)', 'Pertumbuhan (%, yoy)', atau
   tabel berjudul pertumbuhan), pilih baris itu dan jangan hitung dari level: growth = false. growth: true
   HANYA bila tabel tidak punya baris pertumbuhannya dan baris yang ada berisi level (nilai, bukan persen
   pertumbuhan); program yang menghitung pertumbuhannya. unit: satuan seri ini bila berbeda dari unit grafik,
   misalnya grafik dua sumbu dengan sumbu kiri 'Indeks' dan sumbu kanan '%'; null bila sama.
3. labels: setiap angka data yang TERCETAK pada grafik (bukan angka sumbu): text persis seperti tercetak,
   series = name seri pemiliknya (sama persis dengan salah satu name di series), period = periodenya
   ditulis seperti TABLES menulis periode.
{emf_block}
Jawab HANYA JSON:
{{"title": "...", "unit": "...", "last_period": "...",
 "series": [{{"name": "...", "table": "S1", "row": "...", "growth": false, "unit": null}}],
 "labels": [{{"text": "...", "series": "...", "period": "..."}}]}}

TABLES:
{tables}
"""

_EMF_BLOCK = """
Teks yang tercetak pada grafik ini, dengan posisinya (x ke kanan, y ke bawah). Salin angka label PERSIS dari
daftar ini:
{lines}
"""


def _prompt(chart: ChartImage, views: List[TableView]) -> str:
    texts = [b.text for b in chart.emf_boxes or []]
    emf_block = _EMF_BLOCK.format(lines="\n".join(f"x={b.x} y={b.y} teks={b.text!r}" for b in chart.emf_boxes)) \
        if chart.emf_boxes else ""
    chunk = " ".join([chart.title] + texts)
    return READ_PROMPT.format(caption=chart.caption, title=f": {chart.title}" if chart.title else "",
                              no_image=NO_IMAGE, emf_block=emf_block, tables=table_block(views, chunk))


async def read_chart(chart: ChartImage, views: List[TableView], llm: BaseChatModel) -> ChartRead:
    """The chart as the model reads it, its series mapped to the tables' rows. ChartUnreadable when
    the model saw no picture or its answer is not the JSON asked for."""
    reply = await ask_chart(llm, _prompt(chart, views), chart.png, chart.mime)
    try:
        return ChartRead.model_validate_json(extract_json(reply))
    except (ValidationError, ValueError) as e:
        raise ChartUnreadable(f"{chart.caption}: jawaban bukan JSON grafik ({e.__class__.__name__})")


# --- labels as strict claims -----------------------------------------------------------------------

_LABEL = re.compile(r"^[\d.,]+$")
_THOUSANDS_DOT = re.compile(r"^\d{1,3}(\.\d{3})+$")


def parse_label(text: str) -> Optional[float]:
    """A number as a chart prints it: "12,97", "-1,5", "(1,2)" (negative), "22.6%", "10,77*",
    "1.287" (thousands); None when the text is not a number."""
    t = (text or "").strip().replace("−", "-").replace("–", "-").rstrip("*").strip()
    negative = False
    if t.startswith("(") and t.endswith(")"):
        negative, t = True, t[1:-1].strip()
    t = t.rstrip("%").strip()
    if t.startswith("-"):
        negative, t = not negative, t[1:].strip()
    if not t or not _LABEL.match(t):
        return None
    if "," in t and "." in t:                           # both: the last one is the decimal mark
        t = t.replace(".", "").replace(",", ".") if t.rfind(",") > t.rfind(".") else t.replace(",", "")
    elif "," in t:
        t = t.replace(",", ".")
    elif "." in t and _THOUSANDS_DOT.match(t):
        t = t.replace(".", "")
    try:
        value = float(t)
    except ValueError:
        return None
    return -value if negative else value


def _claim(label: ChartLabel, read: ChartRead, views: Dict[str, TableView], caption: str) -> Claim:
    quote = f"{caption}: {label.series} {label.period} = {label.text}"
    ref = next((s for s in read.series if _fold(s.name) == _fold(label.series)), None)
    # The label alone says how it is printed: "Grafik 2 … = 2,0" must not read the caption's "2".
    # A series on its own axis has its own unit (SHPR: index on the left, % on the right).
    base = dict(operation="value", subject=label.series, context_quote=quote, printed=label.text,
                claimed_value=parse_label(label.text), unit=(ref.unit if ref and ref.unit else read.unit))
    if ref is None or not ref.table or not ref.row:
        return Claim(cells=[], not_in_tables=True, reason="seri grafik ini tidak ada di tabel yang diunggah", **base)
    view = views.get(ref.table.strip().upper())
    row = find_row(view, ref.row) if view else None
    if view is not None and view.is_categorical:
        cell = Cell(code=ref.table, row_text=ref.row, period_text=label.period, row=row,
                    column=find_column(view, label.period))
    elif ref.growth:
        # Growth of a row held as levels: a yoy claim on the same period a year earlier, then this one.
        key = parse_period(label.period)
        earlier = (key[0] - 1, key[1]) if key else None
        cells = [Cell(code=ref.table, row_text=ref.row, period_text=f"{label.period} (tahun sebelumnya)", row=row,
                      key=earlier),
                 Cell(code=ref.table, row_text=ref.row, period_text=label.period, row=row, key=key)]
        return Claim(cells=cells, **{**base, "operation": "yoy_growth"})
    else:
        cell = Cell(code=ref.table, row_text=ref.row, period_text=label.period, row=row, key=parse_period(label.period))
    return Claim(cells=[cell], **base)


def _number(value: float) -> str:
    return f"{round(value, 2):g}".replace(".", ",")


def _ordinal(period: str) -> int:
    """A period's place within its year; a year's annual value comes after its months and quarters."""
    return 13 if period == ANNUAL else _period_ordinal(period)


def series_rows(read: ChartRead, views: List[TableView]) -> List[str]:
    """Each mapped series with the table row it was compared with, for the card: a chart drawn on
    another definition (c.i.f. against a fob row, giro rupiah + valas against giro rupiah) shows
    as such instead of as an error in the report."""
    by_code = {v.code: v for v in views}
    out = []
    for ref in read.series:
        view = by_code.get((ref.table or "").strip().upper())
        row = find_row(view, ref.row) if view and ref.row else None
        if row is None:
            continue
        text = f"{ref.name}: {row} ({view.source.filename}, sheet {view.source.sheet})"
        out.append(text + (", pertumbuhan yoy dihitung dari level" if ref.growth else ""))
    return out


def series_data(read: ChartRead, views: List[TableView], max_points: int = 40) -> Dict[str, List[Tuple[str, float]]]:
    """Each mapped series' values from its table row: the last `max_points` periods up to the chart's
    last period (a category table: its columns in order). Series no table holds are left out."""
    by_code = {v.code: v for v in views}
    last = parse_period(read.last_period) if read.last_period else None
    out: Dict[str, List[Tuple[str, float]]] = {}
    for ref in read.series:
        view = by_code.get((ref.table or "").strip().upper())
        row = find_row(view, ref.row) if view and ref.row else None
        if row is None:
            continue
        if view.is_categorical:
            points = [(col, view.table._data[(row, col)]) for col in view.table.col_labels if (row, col) in view.table._data]
        elif last is not None and last[1] == ANNUAL:
            # A chart of years (SKDU "Realisasi Inflasi Tahun 2018 … 2025"): the table's annual values.
            keyed = sorted(((year, ANNUAL), v) for (r, year), v in (view.table.annual or {}).items() if r == row)
        else:
            keyed = sorted((((k[1], k[2]), v) for k, v in view.table._data.items() if len(k) == 3 and k[0] == row),
                           key=lambda kv: (kv[0][0], _ordinal(kv[0][1])))
        if not view.is_categorical:
            if ref.growth:
                # The chart draws growth (% yoy) of a row the table holds as levels: computed here.
                levels = dict(keyed)
                keyed = [((year, period), (value / levels[(year - 1, period)] - 1) * 100)
                         for (year, period), value in keyed if levels.get((year - 1, period))]
            if last is not None:
                stop = (last[0], _ordinal(last[1]))
                keyed = [kv for kv in keyed if (kv[0][0], _ordinal(kv[0][1])) <= stop]
            points = [(period_header(year, period), value) for (year, period), value in keyed]
        if points:
            out[ref.name] = [(p, round(v, 2)) for p, v in points[-max_points:]]
    return out


class SeriesJudgement(BaseModel):
    name: str
    fits: Literal["ya", "tidak", "ragu"]
    off_periods: List[str] = []
    note: str = ""


class DrawingJudgement(BaseModel):
    last_period_same: Optional[bool] = None
    series: List[SeriesJudgement] = []
    verdict: Literal["masuk akal", "perlu dicek"]
    reason: str = ""


JUDGE_PROMPT = """Kamu memeriksa sebuah grafik dari laporan statistik Bank Indonesia ({caption}{title}). Terlampir
gambar grafiknya. Jika kamu tidak menerima atau tidak bisa melihat gambar, jawab persis '{no_image}'.
Di bawah ini DATA dari tabel Excel resmi untuk seri yang disebut. Tugasmu BUKAN mengukur tinggi persis, tetapi
menilai apakah grafik MASUK AKAL untuk data itu:
- apakah arah naik/turun dan pola umum setiap seri di grafik mengikuti datanya;
- apakah ada periode yang jelas menyimpang (mis. data naik tajam tetapi grafik turun);
- apakah periode terakhir yang tergambar sama dengan periode terakhir data;
- apakah ada seri yang tertukar. Seri yang tidak ada di DATA tidak dinilai.
Selisih kecil yang wajar karena skala gambar bukan masalah.
Jawab HANYA JSON:
{{"last_period_same": true, "series": [{{"name": "...", "fits": "ya" | "tidak" | "ragu", "off_periods": ["..."],
  "note": "..."}}], "verdict": "masuk akal" | "perlu dicek", "reason": "satu kalimat"}}

DATA:
{data}
"""


async def judge_drawing(chart: ChartImage, data: Dict[str, List[Tuple[str, float]]],
                        llm: BaseChatModel) -> Optional[DrawingJudgement]:
    """The model's judgement of the drawing against the series' data; None (no call) without data."""
    if not data:
        return None
    lines = "\n".join(f"SERI '{name}': " + "; ".join(f"{p} = {_number(v)}" for p, v in points)
                      for name, points in data.items())
    prompt = JUDGE_PROMPT.format(caption=chart.caption, title=f": {chart.title}" if chart.title else "",
                                 no_image=NO_IMAGE, data=lines)
    reply = await ask_chart(llm, prompt, chart.png, chart.mime)
    try:
        return DrawingJudgement.model_validate_json(extract_json(reply))
    except (ValidationError, ValueError) as e:
        raise ChartUnreadable(f"{chart.caption}: penilaian gambar bukan JSON ({e.__class__.__name__})")


def _numeric(read: ChartRead) -> ChartRead:
    """The read with only the labels that are numbers (a map's city names are not labels)."""
    return read.model_copy(update={"labels": [l for l in read.labels if parse_label(l.text) is not None]})


def label_results(read: ChartRead, views: List[TableView], caption: str) -> List[FactVerificationResult]:
    """One strict verdict per printed label, judged on the cell of its series' row at its period."""
    read = _numeric(read)
    by_code = {v.code: v for v in views}
    out = []
    for label in read.labels:
        result = evaluate_claim(_claim(label, read, by_code, caption), views)
        out.append(result.model_copy(update={"checked_item": "chart"}))
    return out


# --- a label that does not fit, read once more --------------------------------------------------------

REREAD_PROMPT = """Terlampir lagi gambar grafik dari laporan statistik Bank Indonesia ({caption}{title}).
Jika kamu tidak menerima atau tidak bisa melihat gambar, jawab persis '{no_image}'.

Baca ulang dengan teliti HANYA label angka berikut: {labels}
Untuk setiap label: seri pemiliknya adalah garis atau batang tempat label itu menempel; cocokkan WARNA
angka label dengan warna seri di legenda, dan posisinya pada garis atau batang. Periodenya adalah titik
pada sumbu tepat di bawah label (hitung titik sumbu satu per satu bila labelnya rapat).
Seri yang ada (salin nama persis): {series}
Tulis period seperti contoh: {example}

Jawab HANYA JSON:
{{"labels": [{{"text": "...", "series": "...", "period": "..."}}]}}"""

_MONTH_ORDER = ["Jan", "Feb", "Mar", "Apr", "May", "Jun", "Jul", "Aug", "Sep", "Oct", "Nov", "Dec"]


class _Reread(BaseModel):
    labels: List[ChartLabel] = []


def _next_to(period: str) -> List[str]:
    """The periods just before and after `period` ("Jul 2025" → "Jun 2025", "Aug 2025")."""
    key = parse_period(period)
    if key is None:
        return []
    year, token = key
    if token in _MONTH_ORDER:
        i = _MONTH_ORDER.index(token)
        around = [(year - (i == 0), _MONTH_ORDER[i - 1]), (year + (i == 11), _MONTH_ORDER[(i + 1) % 12])]
    elif token in ("Q1", "Q2", "Q3", "Q4"):
        q = int(token[1])
        around = [(year - (q == 1), f"Q{4 if q == 1 else q - 1}"), (year + (q == 4), f"Q{1 if q == 4 else q + 1}")]
    elif token == ANNUAL:
        around = [(year - 1, ANNUAL), (year + 1, ANNUAL)]
    else:
        return []
    return [period_header(y, t) for y, t in around]


def _fits_elsewhere(label: ChartLabel, read: ChartRead, by_code: Dict[str, TableView], views: List[TableView],
                    caption: str) -> bool:
    """The label's number is exactly in the tables for another series of the chart at its period, or
    for its own series a period before or after: the model likely put it on the wrong line or point."""
    candidates = [ChartLabel(text=label.text, series=s.name, period=label.period)
                  for s in read.series if _fold(s.name) != _fold(label.series)]
    candidates += [ChartLabel(text=label.text, series=label.series, period=p) for p in _next_to(label.period)]
    return any(evaluate_claim(_claim(c, read, by_code, caption), views).verdict == "Entailed" for c in candidates)


async def reread_labels(chart: ChartImage, read: ChartRead, labels: List[ChartLabel],
                        llm: BaseChatModel) -> List[ChartLabel]:
    """The model reads `labels` once more on the picture: which series (by colour) and period each is."""
    prompt = REREAD_PROMPT.format(
        caption=chart.caption, title=f": {chart.title}" if chart.title else "", no_image=NO_IMAGE,
        labels="; ".join(label.text for label in labels),
        series="; ".join(s.name for s in read.series),
        example=", ".join(dict.fromkeys(label.period for label in labels)))
    reply = await ask_chart(llm, prompt, chart.png, chart.mime)
    try:
        return _Reread.model_validate_json(extract_json(reply)).labels
    except (ValidationError, ValueError) as e:
        raise ChartUnreadable(f"{chart.caption}: bacaan ulang bukan JSON ({e.__class__.__name__})")


async def _recheck(chart: ChartImage, read: ChartRead, results: List[FactVerificationResult],
                   views: List[TableView], llm: BaseChatModel) -> List[FactVerificationResult]:
    """Labels that came back Tidak Sesuai although their number is in the tables next to where the model
    put them are read once more. The second reading decides: Sesuai when it fits, else Tidak Sesuai."""
    by_code = {v.code: v for v in views}
    suspects = [i for i, (label, result) in enumerate(zip(read.labels, results))
                if result.verdict == "Refuted" and _fits_elsewhere(label, read, by_code, views, chart.caption)]
    if not suspects:
        return results
    try:
        again = await reread_labels(chart, read, [read.labels[i] for i in suspects], llm)
    except Exception as e:                          # the first verdicts stand
        logger.warning("%s: bacaan ulang label gagal (%s)", chart.caption, e)
        return results
    out = list(results)
    for i in suspects:
        first = read.labels[i]
        second = next((label for label in again if parse_label(label.text) == parse_label(first.text)), None)
        if second is None:
            continue
        where = (f"Dicek ulang: AI semula membaca label {first.text} di {first.series} {first.period}; "
                 f"bacaan ulang: {second.series} {second.period}.")
        result = evaluate_claim(_claim(second, read, by_code, chart.caption), views)
        if result.verdict == "Entailed":
            out[i] = result.model_copy(update={"checked_item": "chart",
                                               "reasoning": f"{where} {result.reasoning or ''}".strip()})
        else:
            out[i] = results[i].model_copy(update={
                "reasoning": f"{results[i].reasoning or ''} | {where} Tetap tidak cocok.".strip(" |")})
    return out


# --- one card per chart ------------------------------------------------------------------------------

_SEVERITY = {"Entailed": 0, "Inconclusive": 1, "Refuted": 2}
MAX_AT_ONCE = 4                       # charts (or PDF pages) with the model at once


def _thumbnail(png: bytes) -> Optional[str]:
    from pdf_chart_extraction import _thumbnail as thumbnail
    return thumbnail(base64.b64encode(png).decode("ascii"))


async def _card(chart: ChartImage, views: List[TableView], llm: BaseChatModel) -> ChartCheck:
    read = _numeric(await read_chart(chart, views, llm))
    results = await _recheck(chart, read, label_results(read, views, chart.caption), views, llm)
    data = series_data(read, views)
    try:
        judged = await judge_drawing(chart, data, llm)
    except ChartUnreadable:
        logger.warning("%s: penilaian gambar tidak terbaca", chart.caption, exc_info=True)
        judged = None
    counts = {verdict: sum(r.verdict == verdict for r in results) for verdict in _SEVERITY}
    if results:
        # The verdict most labels got, a tie going to the worse (Tidak Sesuai, then Tidak Cukup Data).
        verdict = max(counts, key=lambda v: (counts[v], _SEVERITY[v]))
    else:
        verdict = "Entailed" if judged is not None and judged.verdict == "masuk akal" else "Inconclusive"
    return ChartCheck(
        page_number=chart.page_number, caption=chart.caption, title=chart.title or read.title,
        verdict=verdict, label_count=len(results), entailed_count=counts["Entailed"],
        refuted_count=counts["Refuted"], inconclusive_count=counts["Inconclusive"],
        issues=[r for r in results if r.verdict != "Entailed"], thumbnail=_thumbnail(chart.png),
        drawing=judged.verdict if judged else None, drawing_note=judged.reason if judged else "",
        unchecked_series=[s.name for s in read.series if s.name not in data],
        series_rows=series_rows(read, views),
    )


async def check_charts(charts: List[ChartImage], scanned: List[ScannedPage], views: List[TableView],
                       llm: BaseChatModel,
                       on_progress: Optional[Callable[[int, int], None]] = None) -> Tuple[List[ChartCheck], List[str]]:
    """One card per chart (scanned pages located first), and the charts that could not be read.
    A chart's failure never stops the others; at most MAX_AT_ONCE are with the model at once."""
    unread: List[str] = []
    # At most MAX_AT_ONCE model calls at once for any model (Copilot's client caps itself too): a
    # report with 30 charts sent to Gemini at once runs into the API's rate limit.
    gate = asyncio.Semaphore(MAX_AT_ONCE)

    async def locate(page: ScannedPage):
        async with gate:
            return await locate_charts(page, llm)

    located = await asyncio.gather(*(locate(page) for page in scanned), return_exceptions=True)
    for page, found in zip(scanned, located):
        if isinstance(found, BaseException):
            logger.warning("Halaman %d: letak grafik tidak terbaca (%s)", page.page_number, found)
            unread.append(f"Hal. {page.page_number}")
        else:
            charts = list(charts) + found
    done = 0

    async def one(chart: ChartImage) -> Optional[ChartCheck]:
        nonlocal done
        try:
            async with gate:
                return await _card(chart, views, llm)
        except Exception as e:                          # one chart never fails the check
            logger.warning("%s tidak terbaca: %s", chart.caption, e)
            unread.append(chart.caption)
            return None
        finally:
            done += 1
            if on_progress is not None:
                on_progress(done, len(charts))

    cards = await asyncio.gather(*(one(chart) for chart in charts))
    return [card for card in cards if card is not None], unread
