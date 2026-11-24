"""The chart check (cek grafik), inside strict: every chart is read by the model (its series mapped to
table rows, its printed labels listed), each label is a strict value claim, and the drawing is judged
against the tables' series. See docs/superpowers/specs/2026-11-23-chart-check-design.md.

Two model calls per chart: `read_chart` (the picture + the tables' rows, no values) and
`judge_drawing` (the picture + the mapped series' data). The labels are judged by the program.
"""
import logging
import re
from typing import Dict, List, Literal, Optional, Tuple

from langchain_core.language_models import BaseChatModel
from pydantic import BaseModel, ValidationError

from paired_verifier import _period_ordinal

from chart_images import ChartImage
from chart_vision import NO_IMAGE, ChartUnreadable, ask_chart
from copilot_chat import extract_json
from schemas import FactVerificationResult
from strict.claims import Cell, Claim
from strict.evaluate import evaluate_claim
from strict.tables import TableView, find_column, find_row, parse_period, period_header, table_block

logger = logging.getLogger("fact-checker")


class SeriesRef(BaseModel):
    name: str                         # as in the legend
    table: Optional[str] = None       # "S1"; None when no table holds it
    row: Optional[str] = None         # exact row name from TABLES


class ChartLabel(BaseModel):
    text: str                         # as printed, "12,97"
    series: str                       # a SeriesRef.name
    period: str                       # as the tables write it: "Q2 2026", "Jun 2026", "Tahun 2025"


class ChartRead(BaseModel):
    title: str = ""
    unit: Optional[str] = None        # the labels' / axis's unit, "miliar dolar AS", "%"
    last_period: Optional[str] = None
    series: List[SeriesRef] = []
    labels: List[ChartLabel] = []


READ_PROMPT = """Terlampir gambar sebuah grafik dari laporan statistik Bank Indonesia ({caption}{title}).
Jika kamu tidak menerima atau tidak bisa melihat gambar, jawab persis '{no_image}'.

TABLES di bawah adalah tabel Excel yang diunggah: kode, judul, bentuk periode, dan nama baris. Nilainya
sengaja tidak ditampilkan.

Tugas:
1. title: judul grafik bila ada. unit: satuan angka pada grafik (mis. 'miliar dolar AS', '%'). last_period:
   periode terakhir yang tergambar, ditulis seperti TABLES menulis periode ('Q3 2026', 'Sep 2026', 'Tahun 2025').
2. series: setiap seri di legenda. name disalin persis dari legenda. table dan row: kode tabel dan nama baris
   yang disalin PERSIS dari TABLES dan memuat data seri itu; null bila tidak ada tabel yang memuatnya. Jangan
   memilih baris yang hanya mirip.
3. labels: setiap angka data yang TERCETAK pada grafik (bukan angka sumbu): text persis seperti tercetak,
   series = name seri pemiliknya, period = periodenya ditulis seperti TABLES menulis periode.
{emf_block}
Jawab HANYA JSON:
{{"title": "...", "unit": "...", "last_period": "...",
 "series": [{{"name": "...", "table": "S1", "row": "..."}}],
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
    base = dict(operation="value", subject=label.series, context_quote=quote,
                claimed_value=parse_label(label.text), unit=read.unit)
    ref = next((s for s in read.series if s.name == label.series), None)
    if ref is None or not ref.table or not ref.row:
        return Claim(cells=[], not_in_tables=True, reason="seri grafik ini tidak ada di tabel yang diunggah", **base)
    view = views.get(ref.table.strip().upper())
    row = find_row(view, ref.row) if view else None
    if view is not None and view.is_categorical:
        cell = Cell(code=ref.table, row_text=ref.row, period_text=label.period, row=row,
                    column=find_column(view, label.period))
    else:
        cell = Cell(code=ref.table, row_text=ref.row, period_text=label.period, row=row, key=parse_period(label.period))
    return Claim(cells=[cell], **base)


def _number(value: float) -> str:
    return f"{round(value, 2):g}".replace(".", ",")


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
        else:
            keyed = sorted((((k[1], k[2]), v) for k, v in view.table._data.items() if len(k) == 3 and k[0] == row),
                           key=lambda kv: (kv[0][0], _period_ordinal(kv[0][1])))
            if last is not None:
                stop = (last[0], _period_ordinal(last[1]))
                keyed = [kv for kv in keyed if (kv[0][0], _period_ordinal(kv[0][1])) <= stop]
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


def label_results(read: ChartRead, views: List[TableView], caption: str) -> List[FactVerificationResult]:
    """One strict verdict per printed label, judged on the cell of its series' row at its period."""
    by_code = {v.code: v for v in views}
    out = []
    for label in read.labels:
        result = evaluate_claim(_claim(label, read, by_code, caption), views)
        out.append(result.model_copy(update={"checked_item": "chart"}))
    return out
