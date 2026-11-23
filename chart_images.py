"""The charts of a report as pictures, for the chart check (chart_check.py).

- Word: the first picture after a "Grafik N" caption. PNG/JPEG as stored; EMF (Excel's charts in
  SKDU and NPI) drawn by emf_render, its texts kept exactly (emf_boxes) for the printed labels.
- PDF with a text layer: the regions pdf_chart_extraction.chart_regions finds under the captions,
  rendered.
- PDF pages without a text layer (scanned): the whole page, for the model to locate its charts
  (chart_vision.locate_charts).
"""
import base64
import io
import logging
import re
import xml.etree.ElementTree as ET
import zipfile
from dataclasses import dataclass
from typing import Dict, List, Optional, Tuple

from emf_render import render_emf
from pdf_chart_extraction import _render_regions, chart_regions
from pdf_table_extraction import _page_lines
from word_extraction import (
    _ANY_CHART_CAPTION_RE, _TABLE_CAPTION_RE, _W, EmfText, _relationships, _walk, emf_text_boxes,
)

logger = logging.getLogger("fact-checker")

_CAPTION = re.compile(r"^\s*(Grafik\s*\d+)\s*[.:]?\s*(.*)$", re.IGNORECASE)
_MIMES = {"png": "image/png", "jpg": "image/jpeg", "jpeg": "image/jpeg"}
_MAX_PX = 1600
_SCANNED_BELOW_CHARS = 20          # a page whose text layer holds fewer characters is a picture
_MIN_BOX_PT = 20                   # a chart box narrower or lower than this is not a chart
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
        pending: Optional[Tuple[str, str]] = None
        for p in paragraphs:
            if _ANY_CHART_CAPTION_RE.match(p.text or ""):
                pending = _split_caption(p.text)
            elif _TABLE_CAPTION_RE.match(p.text or ""):
                pending = None
            for rid in p.pictures:
                if pending is None:
                    continue
                path = rels.get(rid, "")
                caption, title = pending
                pending = None
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
    """The charts of a PDF's text-layer pages (rendered regions, named by the page's "Grafik N"
    captions when there are as many of them as regions, else by page), and its scanned pages."""
    regions = chart_regions(pdf)
    texts = _page_texts(pdf)
    # The list of charts (and the table of contents) names every chart with its page: NPI Tw III-2026
    # page 5 came back as one box per entry. Such a page has no chart.
    contents = {page for page, text in texts.items() if _CONTENTS_PAGE.search(text or "")}
    # A box with no width or height is not a chart (left == right on such a list, which pypdfium2
    # refuses to crop). A right edge of -1 means "to the right margin" (chart_regions) and is kept.
    usable = {page: [b for b in boxes if (b[2] < 0 or b[2] - b[0] >= _MIN_BOX_PT) and b[3] - b[1] >= _MIN_BOX_PT]
              for page, boxes in regions.items() if page not in contents}
    ordered = {page: sorted(boxes, key=lambda b: (-b[3], b[0])) for page, boxes in usable.items() if boxes}
    rendered = _render_regions(pdf, ordered, _MAX_PX) if ordered else []
    captions = _caption_lines(pdf)
    charts: List[ChartImage] = []
    seen: Dict[int, int] = {}
    for page, png_b64 in rendered:
        index = seen.get(page, 0)
        seen[page] = index + 1
        names = captions.get(page, [])
        if len(names) == len(ordered.get(page, [])):
            caption, title = _split_caption(names[index])
        else:
            caption, title = f"Grafik hal. {page} ({index + 1})", ""
        charts.append(ChartImage(caption, title, page, base64.b64decode(png_b64)))
    scanned = [ScannedPage(page, _render_page(pdf, page)) for page in range(1, _page_count(pdf) + 1)
               if len((texts.get(page) or "").strip()) < _SCANNED_BELOW_CHARS]
    return charts, scanned
