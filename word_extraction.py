"""Read a Word (.docx) report: its narrative, and the tables it prints as pictures.

BI's Word reports do not hold their tables as Word tables. Every Tabel and Lampiran is an EMF
picture pasted from Excel, preceded by its caption paragraph. EMF is a vector format and keeps
each cell as a text record with coordinates, so the numbers are read out of the file by code, as
exactly as the PDF text layer and with none of its damage: no zero-width spaces inside words, no
narrative column interleaved with the table. The picture's text is rebuilt into (y, line) rows and
handed to the same reader the PDF route uses (pdf_table_extraction._tables_on_page), so a Word
table is split, qualified and guarded exactly like a PDF one.

Word has no fixed pages. The narrative is still cut into `[== Halaman N ==]` blocks — at explicit
page breaks and at every appendix caption — because the narrative filter works per block and
drops appendix blocks by their title. Those block numbers are not page numbers; main clears the
page numbers of a Word document's results.

Standard library only: the deployment image carries no Word toolkit.
"""

import io
import logging
import re
import statistics
import struct
import zipfile
import xml.etree.ElementTree as ET
from dataclasses import dataclass, field
from typing import Dict, List, Optional, Tuple

from pdf_table_extraction import _NUM_CELL_RE, PdfTable, _line_periods, _tables_on_page
from structured_extractor import detect_number_format

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# EMF: text records → table lines
# ---------------------------------------------------------------------------

_EMR_HEADER = 1
_EMR_EXTTEXTOUTW = 84
_EMF_SIGNATURE = b" EMF"
_EXTTEXTOUT_FIXED_BYTES = 76

# A label line within this fraction of the row pitch of a value row belongs to that row: a wrapped
# label sits 1-13 units from its values against a pitch of ~37 in the April report's tables,
# while a section band ("a.l:" headings) sits a whole pitch away and stays its own line.
_ATTACH_FRACTION = 0.5
_MIN_ROW_NUMBERS = 2


@dataclass(frozen=True)
class EmfText:
    x: int
    y: int
    text: str


def emf_text_runs(data: bytes) -> List[EmfText]:
    """Every EMR_EXTTEXTOUTW record's text and reference point, in file order."""
    if (len(data) < 44 or struct.unpack_from("<I", data, 0)[0] != _EMR_HEADER
            or data[40:44] != _EMF_SIGNATURE):
        return []
    runs: List[EmfText] = []
    offset = 0
    while offset + 8 <= len(data):
        record_type, size = struct.unpack_from("<II", data, offset)
        if size < 8 or offset + size > len(data):
            break
        if record_type == _EMR_EXTTEXTOUTW and size >= _EXTTEXTOUT_FIXED_BYTES:
            x, y = struct.unpack_from("<ii", data, offset + 36)
            n_chars, off_string = struct.unpack_from("<II", data, offset + 44)
            start = offset + off_string
            end = start + 2 * n_chars
            if end <= offset + size:
                text = data[start:end].decode("utf-16-le", "replace").strip()
                if text:
                    runs.append(EmfText(x, y, text))
        offset += size
    return runs


def _is_number(text: str) -> bool:
    return bool(_NUM_CELL_RE.match(text))


def _line_text(runs: List[EmfText]) -> str:
    return " ".join(r.text for r in sorted(runs, key=lambda r: r.x))


def emf_table_lines(runs: List[EmfText]) -> List[Tuple[float, str]]:
    """(y, line) per visual row, top first, y negated to the PDF convention (larger = higher)."""
    by_y: Dict[int, List[EmfText]] = {}
    for run in runs:
        by_y.setdefault(run.y, []).append(run)
    ys = sorted(by_y)                      # EMF y grows downward

    header_y = next((y for y in ys if _line_periods(_line_text(by_y[y])) is not None), None)
    value_rows = [y for y in ys if header_y is not None and y > header_y
                  and sum(_is_number(r.text) for r in by_y[y]) >= _MIN_ROW_NUMBERS]
    gaps = [b - a for a, b in zip(value_rows, value_rows[1:])]
    pitch = statistics.median(gaps) if gaps else 0

    owner = {y: y for y in ys}
    if value_rows and pitch:
        value_set = set(value_rows)
        for y in ys:
            if y <= header_y or y in value_set:
                continue
            nearest = min(value_rows, key=lambda row: abs(row - y))
            if abs(nearest - y) <= _ATTACH_FRACTION * pitch:
                owner[y] = nearest

    groups: Dict[int, List[EmfText]] = {}
    for y in ys:
        groups.setdefault(owner[y], []).extend(by_y[y])

    lines: List[Tuple[float, str]] = []
    for y in sorted(groups):
        by_x = sorted(groups[y], key=lambda r: r.x)
        n_values = 0
        for run in reversed(by_x):
            if not _is_number(run.text):
                break
            n_values += 1
        values = by_x[len(by_x) - n_values:]
        # A label split over lines reads top to bottom, then left to right.
        label = sorted(by_x[:len(by_x) - n_values], key=lambda r: (r.y, r.x))
        lines.append((-float(y), " ".join(r.text for r in label + values)))
    return lines


# ---------------------------------------------------------------------------
# .docx: narrative, pictures, captions
# ---------------------------------------------------------------------------

_W = "{http://schemas.openxmlformats.org/wordprocessingml/2006/main}"
_R = "{http://schemas.openxmlformats.org/officeDocument/2006/relationships}"
_A = "{http://schemas.openxmlformats.org/drawingml/2006/main}"
_V = "{urn:schemas-microsoft-com:vml}"
_MC = "{http://schemas.openxmlformats.org/markup-compatibility/2006}"
_PKG_REL = "{http://schemas.openxmlformats.org/package/2006/relationships}"

# Stricter than pdf_table_extraction._CAPTION_RE: in Word every paragraph is a candidate, and a
# sentence such as "Tabel 2 menunjukkan …" must not open a caption. A real caption numbers the
# table and closes the number with a period ("Tabel 1.  Uang Beredar …").
_TABLE_CAPTION_RE = re.compile(r'^(?:Tabel|Lampiran)\s*[IVX\d]+\.\s', re.IGNORECASE)
_CHART_CAPTION_RE = re.compile(r'^Grafik\s*\d+\.', re.IGNORECASE)
_APPENDIX_CAPTION_RE = re.compile(r'^Lampiran\s*[IVX\d]+[.\s]', re.IGNORECASE)
_SPACES_RE = re.compile(r'[ \t   ]+')
_INVISIBLE_RE = re.compile(r'[­​‌‍﻿]')
# Above every line of a picture, so _tables_on_page finds the caption over the header.
_CAPTION_Y = 1e9


@dataclass
class WordDocument:
    narrative_text: str
    tables: List[PdfTable] = field(default_factory=list)
    unread_tables: List[str] = field(default_factory=list)   # "<caption> — <reason>"


@dataclass
class _Paragraph:
    text: str
    pictures: List[str]      # relationship ids, in order
    page_break: bool


def is_word_document(data: bytes) -> bool:
    if not data.startswith(b"PK\x03\x04"):
        return False
    try:
        with zipfile.ZipFile(io.BytesIO(data)) as z:
            return "word/document.xml" in z.namelist()
    except zipfile.BadZipFile:
        return False


def is_legacy_word_document(filename: str) -> bool:
    return (filename or "").lower().endswith(".doc")


def _clean(text: str) -> str:
    return _SPACES_RE.sub(" ", _INVISIBLE_RE.sub("", text)).strip()


def _read_paragraph(p: ET.Element, out: List[_Paragraph]) -> None:
    """One paragraph, then the paragraphs of any text box it carries, each exactly once."""
    texts: List[str] = []
    pictures: List[str] = []
    boxes: List[ET.Element] = []
    page_break = False

    def visit(element: ET.Element) -> None:
        nonlocal page_break
        for child in element:
            tag = child.tag
            if tag == _MC + "Fallback":        # the same content again, for older readers
                continue
            if tag == _W + "txbxContent":      # a text box: its own paragraphs, read after this one
                boxes.append(child)
                continue
            if tag == _W + "t" and child.text:
                texts.append(child.text)
            elif tag == _W + "tab":
                texts.append(" ")
            elif tag == _W + "br":
                if child.get(_W + "type") == "page":
                    page_break = True
                else:
                    texts.append(" ")
            elif tag == _W + "noBreakHyphen":
                texts.append("-")
            elif tag == _W + "pageBreakBefore" and child.get(_W + "val") not in ("0", "false"):
                page_break = True
            elif tag == _A + "blip" and child.get(_R + "embed"):
                pictures.append(child.get(_R + "embed"))
            elif tag == _V + "imagedata" and child.get(_R + "id"):
                pictures.append(child.get(_R + "id"))
            visit(child)

    visit(p)
    out.append(_Paragraph(_clean("".join(texts)), pictures, page_break))
    for box in boxes:
        _walk(box, out)


def _walk(element: ET.Element, out: List[_Paragraph]) -> None:
    """Every paragraph under `element` in document order — body, table cells, content controls."""
    for child in element:
        if child.tag == _MC + "Fallback":
            continue
        if child.tag == _W + "p":
            _read_paragraph(child, out)
        else:
            _walk(child, out)


def _narrative(paragraphs: List[_Paragraph]) -> str:
    blocks: List[List[str]] = [[]]
    for p in paragraphs:
        if (p.page_break or _APPENDIX_CAPTION_RE.match(p.text)) and blocks[-1]:
            blocks.append([])
        if p.text:
            blocks[-1].append(p.text)
    return "\n".join(
        f"[== Halaman {i} ==]\n" + "\n".join(lines)
        for i, lines in enumerate((b for b in blocks if b), 1)
    )


def _relationships(z: zipfile.ZipFile) -> Dict[str, str]:
    """rId → archive path of each internal target of word/document.xml."""
    try:
        root = ET.fromstring(z.read("word/_rels/document.xml.rels"))
    except KeyError:
        return {}
    out: Dict[str, str] = {}
    for rel in root.iter(_PKG_REL + "Relationship"):
        if rel.get("TargetMode") == "External":
            continue
        target = rel.get("Target", "")
        out[rel.get("Id", "")] = target.lstrip("/") if target.startswith("/") else f"word/{target}"
    return out


def _picture_tables(
    paragraphs: List[_Paragraph], rels: Dict[str, str], z: zipfile.ZipFile
) -> Tuple[List[PdfTable], List[str]]:
    """Each table caption paired with the first picture after it, read when it is an EMF."""
    unread: List[str] = []
    readings: List[Tuple[str, List[Tuple[float, str]]]] = []
    names = set(z.namelist())
    pending: Optional[str] = None

    for p in paragraphs:
        if _TABLE_CAPTION_RE.match(p.text) or _CHART_CAPTION_RE.match(p.text):
            if pending:
                unread.append(f"{pending} — tidak ada gambar tabel sesudah judul ini")
            # A chart caption closes the table caption before it but claims no picture as a table.
            pending = p.text if _TABLE_CAPTION_RE.match(p.text) else None
        for rid in p.pictures:
            if pending is None:
                continue
            path = rels.get(rid, "")
            ext = path.rsplit(".", 1)[-1].lower() if "." in path else "?"
            if ext != "emf" or path not in names:
                unread.append(f"{pending} — gambar tabel berformat {ext.upper()}, belum bisa dibaca")
            else:
                readings.append((pending, emf_table_lines(emf_text_runs(z.read(path)))))
            pending = None
    if pending:
        unread.append(f"{pending} — tidak ada gambar tabel sesudah judul ini")

    # One convention per document, decided from all its tables' text (see detect_number_format).
    number_format = detect_number_format(
        "\n".join(text for _, lines in readings for _, text in lines))
    tables: List[PdfTable] = []
    for index, (caption, lines) in enumerate(readings):
        view = [(_CAPTION_Y, caption)] + lines
        found, _ = _tables_on_page(view, view, 0, number_format)
        if not found:
            unread.append(f"{caption} — tabel di gambar ini tidak bisa disusun "
                          "(kolom dan judul periodenya tidak cocok)")
        for table in found:
            table.index_on_page = len(tables)
            table.caption_index = index
            tables.append(table)
    return tables, unread


def read_word_document(data: bytes) -> WordDocument:
    with zipfile.ZipFile(io.BytesIO(data)) as z:
        body = ET.fromstring(z.read("word/document.xml")).find(_W + "body")
        paragraphs: List[_Paragraph] = []
        if body is not None:
            _walk(body, paragraphs)
        tables, unread = _picture_tables(paragraphs, _relationships(z), z)
    logger.info("Word document: %d paragraph(s), %d table(s) read, %d caption(s) unread.",
                len(paragraphs), len(tables), len(unread))
    return WordDocument(narrative_text=_narrative(paragraphs), tables=tables, unread_tables=unread)
