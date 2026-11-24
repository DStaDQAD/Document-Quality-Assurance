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
from pdf_table_extraction import _page_lines
from word_extraction import (
    _ANY_CHART_CAPTION_RE, _TABLE_CAPTION_RE, _W, EmfText, _relationships, _walk, emf_text_boxes,
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
    """Each "Grafik N" caption with the first picture after it. A table caption, or the next chart
    caption, closes a caption that found no picture."""
    with zipfile.ZipFile(io.BytesIO(docx)) as z:
        body = ET.fromstring(z.read("word/document.xml")).find(_W + "body")
        paragraphs: list = []
        if body is not None:
            _walk(body, paragraphs)
        rels = _relationships(z)
        names = set(z.namelist())
        charts: List[ChartImage] = []
        # Captions waiting for their picture, oldest first: charts set side by side print both
        # captions (each with its title lines) and then both pictures (SKDU Tw III-2026 Grafik 3, 4).
        pending: List[List] = []                        # [caption, [title lines]]
        for p in paragraphs:
            text = (p.text or "").strip()
            if _ANY_CHART_CAPTION_RE.match(text):
                caption, title = _split_caption(text)
                pending.append([caption, [title] if title else []])
            elif _TABLE_CAPTION_RE.match(text):
                pending = []
            elif text and pending and len(pending[-1][1]) < _TITLE_LINES and len(text) <= _TITLE_CHARS:
                # The title printed under its caption, one or two lines ("Perkembangan Tenaga Kerja" /
                # "LU Pertanian, Kehutanan, dan Perikanan").
                pending[-1][1].append(text)
            for rid in p.pictures:
                if not pending:
                    continue
                path = rels.get(rid, "")
                caption, lines = pending.pop(0)
                title = " ".join(lines)
                if path not in names:
                    continue
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
