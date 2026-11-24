"""The charts of a report as pictures, for the chart check (chart_check.py).

- Word: the first picture after a "Grafik N" caption. PNG/JPEG as stored; EMF (Excel's charts in
  SKDU and NPI) drawn by emf_render, its texts kept exactly (emf_boxes) for the printed labels.
- PDF: every page that names a "Grafik N", and every page without a text layer (scanned), as a
  whole page for the model to locate its charts (chart_vision.locate_charts).
"""
import io
import logging
import re
import xml.etree.ElementTree as ET
import zipfile
from dataclasses import dataclass
from typing import Dict, List, Optional, Tuple

from emf_render import render_emf
from pdf_extraction import pdfium_exclusive
from pdf_table_extraction import _page_lines
from word_extraction import (
    _ANY_CAPTION_RE, _ANY_CHART_CAPTION_RE, _TABLE_CAPTION_RE, _W, EmfText, _is_contents_entry, _relationships, _walk,
    emf_text_boxes,
)

logger = logging.getLogger("fact-checker")

_CAPTION = re.compile(r"^\s*(Grafik\s*\d+)\s*[.:]?\s*(.*)$", re.IGNORECASE)
_MIMES = {"png": "image/png", "jpg": "image/jpeg", "jpeg": "image/jpeg"}
_MAX_PX = 1600
_SCANNED_BELOW_CHARS = 20          # a page whose text layer holds fewer characters is a picture
_TITLE_LINES = 2                    # title lines under a chart caption, at most
_TITLE_CHARS = 150                 # a longer line is prose, not a title
_CONTENTS_PAGE = re.compile(r"\bDAFTAR\s+(?:GRAFIK|ISI|TABEL)\b", re.IGNORECASE)


@dataclass
class ChartImage:
    caption: str                       # "Grafik 3", or "Grafik hal. 3 (1)"
    title: str                         # the caption's text after the number, may be ""
    page_number: Optional[int]         # PDF page; None for Word
    png: bytes                         # always a picture
    mime: str = "image/png"
    emf_boxes: Optional[List[EmfText]] = None   # an EMF chart's exact texts


@dataclass
class ScannedPage:
    page_number: int
    png: bytes                         # the whole page


def _split_caption(text: str) -> Tuple[str, str]:
    match = _CAPTION.match(text)
    caption = " ".join(match.group(1).split()) if match else text.strip()
    return caption, (match.group(2).strip() if match else "")


def word_charts(docx: bytes) -> List[ChartImage]:
    """Each "Grafik N" caption with its picture, the caption printed above the picture (SKDU, UB) or
    below it (NPI Tw II-2026: picture, then "Grafik 4", then its title).

    Captions wait in order for the pictures after them: charts set side by side print both captions
    (each with its title lines) and then both pictures (SKDU Tw III-2026 Grafik 3, 4). A picture no
    caption is waiting for waits in turn for the caption under it. A table caption or a paragraph of
    prose ends the waiting on both sides, so one caption without a picture never shifts the pictures
    of the charts after it. The pictures under a "Tabel N" caption are the table's. A native Word
    chart takes its caption's place and gives no picture. A sentence that starts "Grafik 2
    menunjukkan …" and the list of charts are not captions."""
    with zipfile.ZipFile(io.BytesIO(docx)) as z:
        body = ET.fromstring(z.read("word/document.xml")).find(_W + "body")
        paragraphs: list = []
        if body is not None:
            _walk(body, paragraphs)
        rels = _relationships(z)
        names = set(z.namelist())
        found: List[dict] = []          # each caption: its title lines, picture rId, native chart
        loose: List[str] = []           # pictures waiting for the caption under them
        in_table = False                # pictures now belong to a "Tabel N" caption

        def waiting():
            return [entry for entry in found if entry["rid"] is None and not entry["native"]]

        def end_waiting():
            nonlocal loose
            found[:] = [entry for entry in found if entry["rid"] is not None or entry["native"]]
            loose = []
            for entry in found:
                entry["closed"] = True

        for index, p in enumerate(paragraphs):
            text = (p.text or "").strip()
            short = len(text) <= _TITLE_CHARS
            if _ANY_CHART_CAPTION_RE.match(text) and short and not _is_contents_entry(paragraphs, index):
                caption, title = _split_caption(text)
                for entry in found:
                    if entry["rid"] is not None:
                        entry["closed"] = True
                found.append({"caption": caption, "lines": [title] if title else [], "native": False,
                              "rid": loose.pop(0) if loose else None, "closed": False})
                in_table = False
            elif _TABLE_CAPTION_RE.match(text) or _ANY_CAPTION_RE.match(text):
                end_waiting()                           # "Tabel 3", "Lampiran 1": the pictures under it are its
                in_table = True
            elif _ANY_CHART_CAPTION_RE.match(text) or not short:
                end_waiting()                           # prose, a sentence about a chart, a list entry
                in_table = False
            elif text and found and not found[-1]["closed"] and len(found[-1]["lines"]) < (
                    1 if found[-1]["rid"] is not None else _TITLE_LINES):
                # The title printed under its caption, one or two lines ("Perkembangan Tenaga Kerja" /
                # "LU Pertanian, Kehutanan, dan Perikanan"); a line after its picture ("Sumber: …") is
                # not. A caption under its picture has one title line: the next is the text's heading.
                found[-1]["lines"].append(text)
            for _ in p.charts:
                open_ = waiting()
                if open_:
                    open_[0].update(native=True, closed=True)
                    logger.info("%s: grafik bawaan Word, tidak ada gambar untuk dibaca", open_[0]["caption"])
            for rid in p.pictures:
                open_ = waiting()
                if open_:
                    open_[0].update(rid=rid, closed=True)
                elif not in_table:
                    loose.append(rid)
                    for entry in found:
                        entry["closed"] = True
        charts: List[ChartImage] = []
        for entry in found:
            path = rels.get(entry["rid"] or "", "")
            if entry["native"] or path not in names:
                continue
            caption, title = entry["caption"], " ".join(entry["lines"])
            data, ext = z.read(path), path.rsplit(".", 1)[-1].lower()
            if ext in _MIMES:
                charts.append(ChartImage(caption, title, None, data, _MIMES[ext]))
            elif ext == "emf":
                png = render_emf(data, max_px=_MAX_PX)
                if png is None:
                    logger.warning("%s: EMF tidak bisa digambar", caption)
                    continue
                charts.append(ChartImage(caption, title, None, png, "image/png", emf_text_boxes(data)))
    return charts


# --- PDF ---------------------------------------------------------------------------------------

@pdfium_exclusive
def _page_count(pdf: bytes) -> int:
    import pypdfium2 as pdfium
    document = pdfium.PdfDocument(pdf)
    try:
        return len(document)
    finally:
        document.close()


def _page_texts(pdf: bytes) -> Dict[int, str]:
    """1-based page → its text layer joined (pages with no text layer are absent or empty)."""
    return {page: " ".join(text for _, text in reading) for page, (reading, _visual) in _page_lines(pdf).items()}


def _caption_lines(pdf: bytes) -> Dict[int, List[str]]:
    """1-based page → its "Grafik N …" lines in reading order."""
    return {page: [text for _, text in reading if _ANY_CHART_CAPTION_RE.match(text.strip())]
            for page, (reading, _visual) in _page_lines(pdf).items()}


@pdfium_exclusive
def _render_page(pdf: bytes, page_number: int) -> bytes:
    import pypdfium2 as pdfium
    document = pdfium.PdfDocument(pdf)
    try:
        page = document[page_number - 1]
        width, height = page.get_size()
        image = page.render(scale=_MAX_PX / max(width, height)).to_pil().convert("RGB")
        out = io.BytesIO()
        image.save(out, format="PNG", optimize=True)
        return out.getvalue()
    finally:
        document.close()


def pdf_charts(pdf: bytes) -> Tuple[List[ChartImage], List[ScannedPage]]:
    """A PDF's charts are located by the model on whole pages (chart_vision.locate_charts): the pages
    whose text layer names a "Grafik N", and the scanned pages (no text layer). The first list is
    kept for the interface and is empty.

    The text-layer locator (pdf_chart_extraction.chart_regions) cut most charts of NPI Tw III-2026
    R0.1 to their axis and legend, which the model then rightly judged unreadable (measurement
    2026-11-24); a whole page and the model's own box do not."""
    texts = _page_texts(pdf)
    captions = _caption_lines(pdf)
    # The list of charts (and the table of contents) names every chart with its page: no chart there.
    contents = {page for page, text in texts.items() if _CONTENTS_PAGE.search(text or "")}
    pages = [page for page in range(1, _page_count(pdf) + 1)
             if (captions.get(page) and page not in contents)
             or len((texts.get(page) or "").strip()) < _SCANNED_BELOW_CHARS]
    return [], [ScannedPage(page, _render_page(pdf, page)) for page in pages]
