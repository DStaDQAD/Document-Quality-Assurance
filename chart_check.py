"""The chart check (cek grafik), inside strict: every chart is read by the model (its series mapped to
table rows, its printed labels listed), each label is a strict value claim, and the drawing is judged
against the tables' series. See docs/superpowers/specs/2026-11-23-chart-check-design.md.

Two model calls per chart: `read_chart` (the picture + the tables' rows, no values) and
`judge_drawing` (the picture + the mapped series' data). The labels are judged by the program.
"""
import logging
from typing import List, Optional

from langchain_core.language_models import BaseChatModel
from pydantic import BaseModel, ValidationError

from chart_images import ChartImage
from chart_vision import NO_IMAGE, ChartUnreadable, ask_chart
from copilot_chat import extract_json
from strict.tables import TableView, table_block

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
