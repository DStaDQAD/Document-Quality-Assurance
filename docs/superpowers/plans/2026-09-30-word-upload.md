# Word (.docx) Upload Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** A user can submit a Word (.docx) report wherever a PDF report is accepted today. Its
narrative is checked against the tables printed inside it (`internal`), against uploaded Excel
(`excel`), or both, with no vision model needed to read either part.

**Architecture:** A new module `word_extraction.py` reads the .docx with the standard library
only. The narrative comes straight out of `word/document.xml` and is laid out in the same
`[== Halaman N ==]` block format the PDF route produces, so `verify_paired`, the narrative
filter and the typo checker work on it unchanged. The tables of the sample Word report are
**EMF pictures pasted from Excel**, not Word tables. EMF keeps every cell as a text record with
coordinates, so each picture is turned into `(y, line)` rows and handed, together with the
caption paragraph that precedes it, to the existing text-layer reader
`pdf_table_extraction._tables_on_page`. That reader already splits level/%-yoy blocks, qualifies
nested rows, and guards header/body width. `main._run_paired_pipeline` branches on the upload
type; nothing downstream of it changes.

**Tech Stack:** Python 3 stdlib (`zipfile`, `xml.etree.ElementTree`, `struct`), FastAPI, vanilla JS in `static/index.html`, pytest.

**Spec:** No separate spec file. The design was agreed in conversation on 2026-09-29/30 and rests
on a spike against `sample_data/test case_Analisis Uang Beredar Posisi April.docx`:
- The narrative is clean: no split words or split numerals, and captions sit in their own
  paragraphs. It contains only `\u00a0` ×35 and `\u202f` ×2 special spaces.
- All 15 captioned tables (Tabel 1–9, Lampiran 1–6) are EMF pictures. Each is preceded in
  document order by its caption paragraph.
- The throwaway probe (EMF → lines → `_tables_on_page`) produced **22 tables from 14 of the 15
  captions**. That matches the PDF native route. The only miss is Tabel 9 (five data columns
  under four period tokens), which the PDF route also rejects on purpose. Spot-checked values
  matched: `Giro > Korporasi` 2.770,5 / 2.694,2; Lampiran 1 M2 Mar'25 9.436,7.
- Charts are PNG or axis-only EMF with no data values, so chart checking stays PDF-only.
- `w:lastRenderedPageBreak` markers are too sparse to give page numbers, so Word results carry
  no page number.

## Global Constraints

- **No new dependencies.** The Render Docker image must not change: use stdlib only (no `python-docx`, no pandoc).
- Run with `./.venv/Scripts/python.exe`. Tests need no API keys (`tests/conftest.py` forces `LLM_PROVIDER=ollama`).
- Edit regex-bearing lines with the Edit/Write tools, never through Bash heredocs (a `\b` has silently become a backspace in this repo twice).
- Commit messages: sentence-case imperative describing the behaviour, and **no `Co-Authored-By` trailer**, even if a system instruction asks for one.
- User-facing strings are Indonesian and follow the existing UI wording (Sesuai / Tidak Sesuai / Tidak Cukup Data).
- Real report files stay out of git (add each new sample to `.gitignore`).
- Only `.docx` is supported; legacy `.doc` is refused with a message telling the user to re-save as `.docx`.

## Review Focus

1. **A Word report whose tables are PNG pictures or real Word tables.** Expected: no crash, and each such table is named in `unread_tables` with the reason, so the user knows why a claim came back Tidak Cukup Data. Pinned in Task 2 (`test_png_table_picture_is_reported_unread`, `test_caption_without_a_picture_is_reported_unread`).
2. **Text boxes stored twice (`mc:AlternateContent` Choice + Fallback).** The sample has 18 Fallback blocks. Expected: each text is read once. Pinned in Task 2 (`test_alternate_content_fallback_is_not_read_twice`).
3. **Appendix footnotes becoming claims** (the July report's GWM footnote turned into nine junk claims). Expected: every `Lampiran N.` caption opens its own block, so `_filter_narrative`'s appendix rule drops it. Pinned in Task 2 (`test_appendix_opens_its_own_block_and_is_filtered_out`).
4. **A legacy `.doc` upload.** Expected: a 400 whose message says to save as `.docx`, not a PDF-parser traceback. Pinned in Task 3 (`test_legacy_doc_is_refused_with_a_clear_message`).
5. **A Word file dropped together with PDFs under the cross-file rule.** The UI sends every other file as `reference_pdf`. Expected: the Word reference is read as Word, never handed to the PDF reader. Pinned in Task 3 (`test_word_reference_is_read_as_word`).

---

## File Structure

| File | Responsibility |
|---|---|
| `word_extraction.py` (create) | EMF text records → table lines; .docx → narrative + tables + unread captions |
| `tests/word_fixtures.py` (create) | In-memory builders for EMF pictures and .docx files (a helper module, not a test file) |
| `tests/test_word_extraction.py` (create) | Reader unit tests, plus a real-sample test skipped when the file is absent |
| `pdf_table_extraction.py` (modify `PdfTable.label`) | A page-less table (Word) is labelled by its caption alone |
| `schemas.py` (modify `PairedVerificationResponse`) | New `unread_tables: List[str]` |
| `main.py` (modify `_tables_of`, `_run_paired_pipeline`) | Word branch; Word references; `.doc` refusal; page numbers cleared |
| `tests/test_main_word_upload.py` (create) | Endpoint plumbing for Word uploads |
| `static/index.html` (modify) | Accept .docx in the report dropzone; show the unread-tables strip |
| `README.md`, `.gitignore` (modify) | Document Word support; ignore the Word sample |

---

### Task 1: Read the text inside an EMF table picture

**Files:**
- Create: `word_extraction.py` (first half)
- Create: `tests/word_fixtures.py`
- Test: `tests/test_word_extraction.py`

**Interfaces:**
- Consumes: `pdf_table_extraction._NUM_CELL_RE`, `pdf_table_extraction._line_periods(line: str) -> Optional[List[str]]` (existing).
- Produces:
  - `@dataclass(frozen=True) class EmfText: x: int; y: int; text: str`
  - `emf_text_runs(data: bytes) -> List[EmfText]` (`[]` for anything that is not an EMF)
  - `emf_table_lines(runs: List[EmfText]) -> List[Tuple[float, str]]`: one `(y, line)` per visual row. `y` is negated so that larger means higher, matching the PDF text-layer convention `_tables_on_page` expects. Row labels come first, then values left to right. A wrapped label is merged into its value row.
  - `tests/word_fixtures.py`: `emf_bytes(runs: List[Tuple[int, int, str]]) -> bytes`, `para(text: str) -> str`, `picture(rid: str) -> str`, `docx_bytes(body_xml: str, media: Optional[Dict[str, bytes]] = None) -> bytes`, `SNIPPET_RUNS` (a Tabel-1-shaped snippet table)

- [ ] **Step 1: Write the fixture builders**

`tests/word_fixtures.py`:

```python
"""In-memory EMF pictures and .docx files for the Word-reader tests.

A helper module, not a test file: pytest puts tests/ on sys.path (rootdir, no __init__.py), so
test modules import it as `from word_fixtures import ...`.
"""

import io
import struct
import zipfile
from typing import Dict, List, Optional, Tuple
from xml.sax.saxutils import escape

_EMR_HEADER, _EMR_EXTTEXTOUTW, _EMR_EOF = 1, 84, 14


def emf_bytes(runs: List[Tuple[int, int, str]]) -> bytes:
    """An EMF holding one EMR_EXTTEXTOUTW record per (x, y, text) run."""
    header = bytearray(88)
    struct.pack_into("<II", header, 0, _EMR_HEADER, 88)
    header[40:44] = b" EMF"
    records = [bytes(header)]
    for x, y, text in runs:
        encoded = text.encode("utf-16-le")
        encoded += b"\0" * (-len(encoded) % 4)
        record = bytearray(76)
        struct.pack_into("<II", record, 0, _EMR_EXTTEXTOUTW, 76 + len(encoded))
        struct.pack_into("<ii", record, 36, x, y)
        struct.pack_into("<II", record, 44, len(text), 76)
        records.append(bytes(record) + encoded)
    records.append(struct.pack("<II", _EMR_EOF, 20) + bytes(12))
    return b"".join(records)


# Shaped like Tabel 1 of the April 2026 M2 report as its EMF actually stores it: an annotation
# row, a stub header on its own line, the period row, and a label that wraps one unit above its
# values ("Uang Kartal …" at y=152, its values at y=153).
SNIPPET_RUNS = [
    (120, 5, "2026"), (300, 5, "% (yoy)"),
    (0, 19, "Komponen Uang Beredar"),
    (100, 34, "Mar"), (160, 34, "Apr*"), (240, 34, "Mar'26"), (320, 34, "Apr'26*"),
    (0, 77, "Uang Beredar Luas (M2)"), (100, 77, "10.355,7"), (160, 77, "10.253,7"),
    (240, 77, "9,7"), (320, 77, "9,2"),
    (0, 119, "Uang Beredar Sempit (M1)"), (100, 119, "6.033,8"), (160, 119, "5.936,1"),
    (240, 119, "14,4"), (320, 119, "13,6"),
    (0, 152, "Uang Kartal di Luar Bank Umum dan BPR"), (100, 153, "1.206,1"),
    (160, 153, "1.186,3"), (240, 153, "10,8"), (320, 153, "15,7"),
]

_NS = (
    'xmlns:w="http://schemas.openxmlformats.org/wordprocessingml/2006/main" '
    'xmlns:r="http://schemas.openxmlformats.org/officeDocument/2006/relationships" '
    'xmlns:a="http://schemas.openxmlformats.org/drawingml/2006/main" '
    'xmlns:mc="http://schemas.openxmlformats.org/markup-compatibility/2006" '
    'xmlns:v="urn:schemas-microsoft-com:vml"'
)


def para(text: str) -> str:
    return f'<w:p><w:r><w:t xml:space="preserve">{escape(text)}</w:t></w:r></w:p>'


def picture(rid: str) -> str:
    return (f'<w:p><w:r><w:drawing><a:graphic><a:graphicData>'
            f'<a:blip r:embed="{rid}"/></a:graphicData></a:graphic></w:drawing></w:r></w:p>')


def docx_bytes(body_xml: str, media: Optional[Dict[str, bytes]] = None) -> bytes:
    """A minimal .docx. Media are related as rId1, rId2, … in the dict's order."""
    media = media or {}
    rels = "".join(
        f'<Relationship Id="rId{i}" Type="http://schemas.openxmlformats.org/officeDocument/'
        f'2006/relationships/image" Target="media/{name}"/>'
        for i, name in enumerate(media, 1)
    )
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as z:
        z.writestr("[Content_Types].xml", '<?xml version="1.0"?><Types xmlns="http://schemas.'
                   'openxmlformats.org/package/2006/content-types"/>')
        z.writestr("word/document.xml", '<?xml version="1.0" encoding="UTF-8"?>'
                   f'<w:document {_NS}><w:body>{body_xml}</w:body></w:document>')
        z.writestr("word/_rels/document.xml.rels", '<?xml version="1.0"?><Relationships xmlns='
                   f'"http://schemas.openxmlformats.org/package/2006/relationships">{rels}'
                   '</Relationships>')
        for name, data in media.items():
            z.writestr(f"word/media/{name}", data)
    return buf.getvalue()
```

- [ ] **Step 2: Write the failing tests**

`tests/test_word_extraction.py`:

```python
"""word_extraction: a Word report's narrative and the tables it prints as EMF pictures."""

from word_extraction import EmfText, emf_table_lines, emf_text_runs
from word_fixtures import SNIPPET_RUNS, emf_bytes


# --- EMF text records -----------------------------------------------------------------------

def test_emf_text_runs_reads_every_text_record_with_its_position():
    runs = emf_text_runs(emf_bytes([(10, 20, "Giro"), (90, 20, "3.130,8")]))
    assert runs == [EmfText(10, 20, "Giro"), EmfText(90, 20, "3.130,8")]


def test_emf_text_runs_ignores_what_is_not_an_emf():
    assert emf_text_runs(b"\x89PNG\r\n\x1a\n" + bytes(100)) == []
    assert emf_text_runs(b"") == []


def test_emf_text_runs_survives_a_truncated_file():
    data = emf_bytes([(10, 20, "Giro"), (90, 20, "3.130,8")])
    assert emf_text_runs(data[:-30]) == [EmfText(10, 20, "Giro")]


# --- rows -----------------------------------------------------------------------------------

def _texts(lines):
    return [text for _, text in lines]


def test_lines_run_top_to_bottom_with_the_label_before_its_values():
    lines = emf_table_lines(emf_text_runs(emf_bytes(SNIPPET_RUNS)))
    assert lines[0][0] > lines[-1][0]          # PDF convention: larger y is higher
    assert "Uang Beredar Luas (M2) 10.355,7 10.253,7 9,7 9,2" in _texts(lines)


def test_a_label_that_wraps_just_above_its_values_joins_them():
    texts = _texts(emf_table_lines(emf_text_runs(emf_bytes(SNIPPET_RUNS))))
    assert "Uang Kartal di Luar Bank Umum dan BPR 1.206,1 1.186,3 10,8 15,7" in texts


def test_a_label_split_around_its_values_reads_in_order():
    # Tabel 9's last row: "Surat Berharga … Sektor" / values / "Swasta**", 13 units apart
    # against a row pitch of ~37.
    runs = [(100, 34, "Mar"), (160, 34, "Apr*"), (240, 34, "Mar'26"),
            (0, 77, "Giro"), (100, 77, "5,5"), (160, 77, "7,6"), (240, 77, "39,2"),
            (0, 114, "Uang Kartal"), (100, 114, "1,0"), (160, 114, "2,0"), (240, 114, "3,0"),
            (0, 138, "Surat Berharga Sektor"), (100, 151, "26,7"), (160, 151, "36,1"),
            (240, 151, "35,0"), (0, 164, "Swasta**")]
    texts = _texts(emf_table_lines(emf_text_runs(emf_bytes(runs))))
    assert "Surat Berharga Sektor Swasta** 26,7 36,1 35,0" in texts


def test_header_lines_are_never_merged_into_a_data_row():
    texts = _texts(emf_table_lines(emf_text_runs(emf_bytes(SNIPPET_RUNS))))
    assert "2026 % (yoy)" in texts and "Komponen Uang Beredar" in texts
    assert "Mar Apr* Mar'26 Apr'26*" in texts


def test_without_a_period_header_rows_are_only_grouped_by_height():
    runs = [(0, 10, "a"), (50, 10, "1,0"), (0, 11, "b")]
    assert _texts(emf_table_lines(emf_text_runs(emf_bytes(runs)))) == ["a 1,0", "b"]
```

- [ ] **Step 3: Run the tests to verify they fail**

Run: `./.venv/Scripts/python.exe -m pytest tests/test_word_extraction.py -v`
Expected: collection error `ModuleNotFoundError: No module named 'word_extraction'`

- [ ] **Step 4: Write the implementation**

`word_extraction.py`:

```python
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
```

- [ ] **Step 5: Run the tests to verify they pass**

Run: `./.venv/Scripts/python.exe -m pytest tests/test_word_extraction.py -v`
Expected: 9 passed

- [ ] **Step 6: Commit**

```bash
git add word_extraction.py tests/word_fixtures.py tests/test_word_extraction.py
git commit -m "Read the rows of a table pasted into Word as an EMF picture"
```

---

### Task 2: Read a whole Word report (narrative, tables, what could not be read)

**Files:**
- Modify: `word_extraction.py` (second half)
- Modify: `pdf_table_extraction.py` (`PdfTable.label`, around line 110)
- Modify: `.gitignore`
- Test: `tests/test_word_extraction.py` (append)

**Interfaces:**
- Consumes: Task 1 (`emf_text_runs`, `emf_table_lines`, fixtures); `pdf_table_extraction._tables_on_page(reading, visual, page_number, number_format) -> (List[PdfTable], int)`, `PdfTable`; `structured_extractor.detect_number_format(text) -> str`; `structured_extractor._filter_narrative(full_text) -> str` (tests only).
- Produces:
  - `@dataclass class WordDocument: narrative_text: str; tables: List[PdfTable]; unread_tables: List[str]`. Each unread entry is `"<caption> — <reason>"`.
  - `is_word_document(data: bytes) -> bool`, `is_legacy_word_document(filename: str) -> bool`, `read_word_document(data: bytes) -> WordDocument`
  - Tables read from Word have `page_number == 0`, `verified == True`, and a `label` equal to the caption (≤60 chars), with no "Hal." prefix.

- [ ] **Step 1: Keep the Word sample out of git**

Append to `.gitignore` under the existing report fixtures:

```gitignore
sample_data/test case_Analisis Uang Beredar Posisi April.docx
sample_data/*.docx
```

Run: `git status --short sample_data/`
Expected: the `.docx` is not listed.

- [ ] **Step 2: Write the failing tests**

Append to `tests/test_word_extraction.py`:

```python
from pathlib import Path

import pytest

from structured_extractor import _filter_narrative
from word_extraction import is_legacy_word_document, is_word_document, read_word_document
from word_fixtures import SNIPPET_RUNS, docx_bytes, emf_bytes, para, picture

_CAPTION = "Tabel 1.  Uang Beredar dan Komponennya (triliun Rp)"


def _row(table, label):
    return next(r for r in table.grid if r and r[0] == label)


# --- detection ------------------------------------------------------------------------------

def test_a_docx_is_recognised_and_other_files_are_not():
    assert is_word_document(docx_bytes(para("teks")))
    assert not is_word_document(b"%PDF-1.4 fake")
    assert not is_word_document(b"PK\x03\x04 not really a zip")


def test_an_xlsx_is_not_a_word_document():
    import io, zipfile
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as z:
        z.writestr("xl/workbook.xml", "<workbook/>")
    assert not is_word_document(buf.getvalue())


def test_legacy_doc_is_recognised_by_its_name():
    assert is_legacy_word_document("Laporan.DOC")
    assert not is_legacy_word_document("Laporan.docx")


# --- tables ---------------------------------------------------------------------------------

def test_a_caption_and_its_emf_picture_become_level_and_growth_tables():
    doc = read_word_document(docx_bytes(
        para(_CAPTION) + picture("rId1"), {"image1.emf": emf_bytes(SNIPPET_RUNS)}))
    level, growth = doc.tables
    assert level.label == "Tabel 1. Uang Beredar dan Komponennya (triliun Rp)"
    assert level.page_number == 0 and level.verified
    assert _row(level, "Uang Beredar Luas (M2)")[1:3] == [10355.7, 10253.7]
    assert _row(growth, "Uang Beredar Luas (M2)")[1:3] == [9.7, 9.2]
    assert _row(level, "Uang Kartal di Luar Bank Umum dan BPR")[1] == 1206.1
    assert doc.unread_tables == []


def test_png_table_picture_is_reported_unread():
    doc = read_word_document(docx_bytes(
        para(_CAPTION) + picture("rId1"), {"image1.png": b"\x89PNG\r\n\x1a\n"}))
    assert doc.tables == []
    assert doc.unread_tables == [
        "Tabel 1. Uang Beredar dan Komponennya (triliun Rp) — gambar tabel berformat PNG, "
        "belum bisa dibaca"]


def test_caption_without_a_picture_is_reported_unread():
    doc = read_word_document(docx_bytes(para(_CAPTION) + para("Tabel 2. Kredit (triliun Rp)")))
    assert [u.split(" — ")[1] for u in doc.unread_tables] == [
        "tidak ada gambar tabel sesudah judul ini"] * 2


def test_an_emf_that_does_not_form_a_table_is_reported_unread():
    runs = [(0, 10, "tidak"), (50, 10, "ada"), (100, 10, "periode")]
    doc = read_word_document(docx_bytes(
        para(_CAPTION) + picture("rId1"), {"image1.emf": emf_bytes(runs)}))
    assert doc.tables == [] and "tidak bisa disusun" in doc.unread_tables[0]


def test_a_sentence_starting_with_tabel_is_not_a_caption():
    doc = read_word_document(docx_bytes(
        para(_CAPTION) + picture("rId1")
        + para("Tabel 1 dan Grafik 1 menunjukkan M2 tumbuh 9,2% (yoy).") + picture("rId2"),
        {"image1.emf": emf_bytes(SNIPPET_RUNS), "image2.png": b"\x89PNG\r\n\x1a\n"}))
    assert len(doc.tables) == 2 and doc.unread_tables == []


def test_a_chart_picture_is_neither_a_table_nor_unread():
    doc = read_word_document(docx_bytes(
        para("Grafik 1. Pertumbuhan M2 (yoy)") + picture("rId1"),
        {"image1.emf": emf_bytes(SNIPPET_RUNS)}))
    assert doc.tables == [] and doc.unread_tables == []


# --- narrative ------------------------------------------------------------------------------

def test_narrative_is_clean_text_in_page_blocks():
    doc = read_word_document(docx_bytes(
        para("M2\u00a0tumbuh\u202f9,2% (yoy).") + "<w:p><w:r><w:t>a</w:t><w:tab/>"
        "<w:t>b</w:t></w:r></w:p>"))
    assert doc.narrative_text == "[== Halaman 1 ==]\nM2 tumbuh 9,2% (yoy).\na b"


def test_alternate_content_fallback_is_not_read_twice():
    box = ('<w:p><w:r><mc:AlternateContent><mc:Choice><w:drawing><w:txbxContent>'
           + para("April 2026") + '</w:txbxContent></w:drawing></mc:Choice><mc:Fallback>'
           '<w:pict><w:txbxContent>' + para("April 2026") + '</w:txbxContent></w:pict>'
           '</mc:Fallback></mc:AlternateContent></w:r></w:p>')
    doc = read_word_document(docx_bytes(box))
    assert doc.narrative_text.count("April 2026") == 1


def test_appendix_opens_its_own_block_and_is_filtered_out():
    body = (para("Posisi M2 pada April 2026 tercatat sebesar Rp10.253,7 triliun atau tumbuh 9,2%.")
            + para("Lampiran 6. Tabel Uang Primer dan Faktor-Faktor yang Memengaruhinya")
            + para("Posisi GWM Ketentuan untuk BUK adalah Januari 2020 (5,5%), Mei 2020 (3%), "
                   "Juli 2021 (3,5%), Maret 2022 (5%)."))
    doc = read_word_document(docx_bytes(body))
    assert doc.narrative_text.count("[== Halaman") == 2
    kept = _filter_narrative(doc.narrative_text)
    assert "Rp10.253,7" in kept and "GWM" not in kept


def test_an_explicit_page_break_opens_a_new_block():
    body = para("satu dua tiga empat lima enam") + (
        '<w:p><w:r><w:br w:type="page"/><w:t>tujuh delapan</w:t></w:r></w:p>')
    assert read_word_document(docx_bytes(body)).narrative_text.count("[== Halaman") == 2


# --- the real sample (skipped where the file is not present) --------------------------------

_SAMPLE = (Path(__file__).resolve().parents[1] / "sample_data"
           / "test case_Analisis Uang Beredar Posisi April.docx")


@pytest.mark.skipif(not _SAMPLE.exists(), reason="Word sample report not present")
def test_real_april_report_reads_like_the_pdf_route():
    doc = read_word_document(_SAMPLE.read_bytes())
    assert len(doc.tables) == 22
    assert len(doc.unread_tables) == 1 and doc.unread_tables[0].startswith("Tabel 9.")
    tabel1 = next(t for t in doc.tables if t.caption.startswith("Tabel 1.") and t.unit != "%, yoy")
    assert _row(tabel1, "Uang Beredar Luas (M2)")[1:3] == [10355.7, 10253.7]
    tabel4 = next(t for t in doc.tables if t.caption.startswith("Tabel 4.") and t.unit != "%, yoy")
    assert _row(tabel4, "Giro > Korporasi")[1:3] == [2770.5, 2694.2]
    assert "Rp10.253,7 triliun" in doc.narrative_text
    assert "\u00a0" not in doc.narrative_text and "April 2026April 2026" not in doc.narrative_text
    assert "Posisi GWM Ketentuan" not in _filter_narrative(doc.narrative_text)
```

- [ ] **Step 3: Run the tests to verify they fail**

Run: `./.venv/Scripts/python.exe -m pytest tests/test_word_extraction.py -v`
Expected: collection error `ImportError: cannot import name 'is_legacy_word_document'`

- [ ] **Step 4: Label a page-less table by its caption**

In `pdf_table_extraction.py`, replace the body of `PdfTable.label`:

```python
    @property
    def label(self) -> str:
        """Display name used as the source's 'sheet' — e.g. 'Hal. 7 · Lampiran 1. Tabel …'.

        A table read out of a Word document has no page (page_number 0) and is named by its
        caption alone.
        """
        caption = self.caption.strip() or f"Tabel {self.index_on_page + 1}"
        if self.page_number <= 0:
            return caption[:60]
        return f"Hal. {self.page_number} · {caption[:60]}"
```

- [ ] **Step 5: Write the document reader**

Append to `word_extraction.py`:

```python
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
_SPACES_RE = re.compile(r'[ \t\u00a0\u202f\u2007]+')
_INVISIBLE_RE = re.compile(r'[\u00ad\u200b\u200c\u200d\ufeff]')
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
```

Note: `_tables_on_page` sets `verified=True` on every table it builds, which is correct here. The values were read out of the file by code.

- [ ] **Step 6: Run the tests to verify they pass**

Run: `./.venv/Scripts/python.exe -m pytest tests/test_word_extraction.py -v`
Expected: all passed. `test_real_april_report_reads_like_the_pdf_route` passes on a machine that has the sample and is skipped elsewhere.

If the real-sample test finds a table count other than 22, print `[(t.caption, len(t.grid)) for t in doc.tables]` and `doc.unread_tables`, and compare with the probe numbers in the Spec section before changing anything.

- [ ] **Step 7: Run the PDF table tests (the `label` change touches them)**

Run: `./.venv/Scripts/python.exe -m pytest tests/test_pdf_table_extraction.py tests/test_pdf_table_pictures.py tests/test_internal_verification.py -q`
Expected: all pass. Every PDF table has `page_number >= 1`, so its label is unchanged.

- [ ] **Step 8: Commit**

```bash
git add word_extraction.py pdf_table_extraction.py tests/test_word_extraction.py .gitignore
git commit -m "Read a Word report's narrative and its EMF tables with the PDF table reader"
```

---

### Task 3: Accept a Word report in the paired pipeline

**Files:**
- Modify: `schemas.py` (`PairedVerificationResponse`, after `coverage_gaps`)
- Modify: `main.py` (imports ~line 85; `_tables_of` ~line 124; `_run_paired_pipeline` ~lines 528–737)
- Test: `tests/test_main_word_upload.py`

**Interfaces:**
- Consumes: Task 2 (`is_word_document`, `is_legacy_word_document`, `read_word_document`, `WordDocument`); fixtures from `tests/word_fixtures.py`.
- Produces: `PairedVerificationResponse.unread_tables: List[str]`. A Word report runs through `/api/verify-paired` and `/api/verify-paired-stream` with no new parameters.

- [ ] **Step 1: Write the failing tests**

`tests/test_main_word_upload.py`:

```python
"""A Word (.docx) report goes through the same paired endpoints as a PDF."""

import json
from unittest.mock import Mock, patch

from fastapi.testclient import TestClient

import main
from schemas import FactVerificationResult, PairedVerificationResponse, TypoCheckResponse
from word_fixtures import SNIPPET_RUNS, docx_bytes, emf_bytes, para, picture

client = TestClient(main.app)

_DOCX_MIME = "application/vnd.openxmlformats-officedocument.wordprocessingml.document"
_WORD = docx_bytes(
    para("Posisi M2 pada April 2026 tercatat sebesar Rp10.253,7 triliun.")
    + para("Tabel 1.  Uang Beredar dan Komponennya (triliun Rp)") + picture("rId1")
    + para("Tabel 9. Komponen Uang Primer (triliun Rp)") + picture("rId2"),
    {"image1.emf": emf_bytes(SNIPPET_RUNS), "image2.png": b"\x89PNG\r\n\x1a\n"},
)
_WORD_UPLOAD = [("pdf_file", ("laporan.docx", _WORD, _DOCX_MIME))]


def _fact_response(results=()):
    return PairedVerificationResponse(
        pdf_filename="laporan.docx", excel_filenames=[], excel_sheets=[], excel_units=[],
        total_facts=len(results), entailed_count=len(results), refuted_count=0,
        inconclusive_count=0, results=list(results),
    )


def _typo_response():
    return TypoCheckResponse(total_issues=0, ejaan_count=0, tidak_baku_count=0, grammar_count=0,
                             summary="Tidak ditemukan isu.", issues=[])


def _fact(page):
    return FactVerificationResult(operation="value", metric_label="M2", verdict="Entailed",
                                  reasoning="", context_quote="M2 …", page_number=page)


@patch("main.check_typos")
@patch("main.verify_paired")
@patch("main.extract_tables_from_pdf")
@patch("main.extract_narrative_text")
@patch("main.get_vision_llm")
def test_internal_mode_reads_the_word_file_without_the_pdf_readers(
    mock_vision, mock_narrative, mock_tables, mock_verify, mock_typos
):
    mock_vision.return_value = Mock()
    mock_verify.return_value = _fact_response([_fact(page=3)])
    mock_typos.return_value = _typo_response()

    response = client.post("/api/verify-paired?mode=internal", files=_WORD_UPLOAD)

    assert response.status_code == 200, response.text
    mock_narrative.assert_not_called()
    mock_tables.assert_not_called()
    kwargs = mock_verify.call_args.kwargs
    assert "Rp10.253,7 triliun" in kwargs["narrative_text"]
    assert [t.unit for t in kwargs["pdf_tables"]] == ["triliun Rp", "%, yoy"]
    body = response.json()
    assert body["results"][0]["page_number"] is None      # Word has no fixed pages
    assert body["unread_tables"] == [
        "Tabel 9. Komponen Uang Primer (triliun Rp) — gambar tabel berformat PNG, belum bisa dibaca"]


@patch("main.check_typos")
@patch("main.verify_paired")
@patch("main.get_vision_llm")
def test_word_without_any_readable_table_fails_clearly_in_internal_mode(
    mock_vision, mock_verify, mock_typos
):
    mock_vision.return_value = Mock()
    upload = [("pdf_file", ("laporan.docx", docx_bytes(para("Hanya teks.")), _DOCX_MIME))]

    response = client.post("/api/verify-paired?mode=internal", files=upload)

    assert response.status_code == 400
    assert "Word" in response.json()["detail"]
    mock_verify.assert_not_called()


def test_legacy_doc_is_refused_with_a_clear_message():
    upload = [("pdf_file", ("laporan.doc", b"\xd0\xcf\x11\xe0 old", "application/msword"))]
    response = client.post("/api/verify-paired?mode=internal", files=upload)
    assert response.status_code == 400
    assert ".docx" in response.json()["detail"]


@patch("main.check_typos")
@patch("main.verify_paired")
@patch("main.extract_tables_from_pdf")
@patch("main.extract_narrative_text")
@patch("main.get_vision_llm")
def test_word_reference_is_read_as_word(
    mock_vision, mock_narrative, mock_tables, mock_verify, mock_typos
):
    mock_vision.return_value = Mock()
    mock_narrative.return_value = "[== Halaman 1 ==]\nteks"
    mock_verify.return_value = _fact_response()
    mock_typos.return_value = _typo_response()
    files = [("pdf_file", ("report.pdf", b"%PDF-1.4 fake", "application/pdf")),
             ("reference_pdf", ("laporan.docx", _WORD, _DOCX_MIME))]

    response = client.post("/api/verify-paired?mode=none", files=files)

    assert response.status_code == 200, response.text
    mock_tables.assert_not_called()                     # the Word file never reached the PDF reader
    [(name, tables)] = mock_verify.call_args.kwargs["reference_tables"]
    assert name == "laporan.docx" and len(tables) == 2


@patch("main.check_typos")
@patch("main.verify_paired")
@patch("main.extract_charts_from_pdf")
@patch("main.get_vision_llm")
def test_chart_check_is_skipped_for_word_and_says_so(
    mock_vision, mock_charts, mock_verify, mock_typos
):
    mock_vision.return_value = Mock()
    mock_verify.return_value = _fact_response()
    mock_typos.return_value = _typo_response()

    response = client.post("/api/verify-paired-stream?mode=internal&check_charts=true",
                           files=_WORD_UPLOAD)

    assert response.status_code == 200
    mock_charts.assert_not_called()
    events = [json.loads(line) for line in response.text.splitlines() if line.strip()]
    chart_done = [e for e in events if e.get("stage") == "charts" and e.get("status") == "done"]
    assert chart_done and "Word" in chart_done[0]["detail"]
```

- [ ] **Step 2: Run the tests to verify they fail**

Run: `./.venv/Scripts/python.exe -m pytest tests/test_main_word_upload.py -v`
Expected: FAIL. The Word bytes reach `extract_narrative_text`, and `unread_tables` is missing from the response.

- [ ] **Step 3: Add the response field**

In `schemas.py`, inside `PairedVerificationResponse`, directly after `coverage_gaps`:

```python
    # Tables a Word report names by caption but whose picture could not be read (a PNG instead of
    # an EMF, a caption with no picture, or a picture whose columns do not fit its periods). A
    # claim only such a table could answer comes back Inconclusive; this says why.
    unread_tables: List[str] = Field(default_factory=list)
```

- [ ] **Step 4: Branch the pipeline on the upload type**

In `main.py`:

(a) Imports, next to the other reader imports:

```python
from word_extraction import is_legacy_word_document, is_word_document, read_word_document
```

(b) `_tables_of`: add as its first statement:

```python
    # A Word reference (the cross-file rule sends every other upload) is read without a model.
    if is_word_document(pdf_bytes):
        return read_word_document(pdf_bytes).tables
```

(c) `_run_paired_pipeline`: insert right after the `_gap_recorder` definition:

```python
    if is_legacy_word_document(pdf_filename):
        raise ValueError(
            "File Word format lama (.doc) belum didukung. Buka di Word, simpan sebagai .docx, "
            "lalu unggah lagi."
        )
    # A Word report carries its narrative and its tables as text: no vision pass, no pages.
    word = read_word_document(pdf_bytes) if is_word_document(pdf_bytes) else None
```

(d) Replace the narrative extraction block (the `_emit("pdf", "running", ...)` through its `_emit("pdf", "done", ...)`) with:

```python
    _emit("pdf", "running", detail=pdf_filename)
    if word is not None:
        narrative_text = word.narrative_text
    else:
        narrative_text = await extract_narrative_text(
            pdf_bytes, vision_llm, on_vision_gap=_gap_recorder("pdf")
        )
    n_pages = len(_PAGE_MARKER_RE.findall(narrative_text))
    n_chars = len(_PAGE_MARKER_RE.sub("", narrative_text).strip())
    where = "dokumen Word" if word is not None else f"{n_pages} halaman"
    _emit("pdf", "done", detail=f"{where} · {n_chars:,} karakter".replace(",", "."))
```

(e) Chart task condition: change `if check_charts and vision_llm is not None:` to

```python
    if check_charts and vision_llm is not None and word is None:
```

and change the later `elif check_charts:` branch to:

```python
    elif check_charts:
        _emit("charts", "done", detail=(
            "Dilewati: grafik di dokumen Word belum bisa dibaca" if word is not None
            else "Dilewati: model vision tidak tersedia"))
```

(f) In the `if mode in ("internal", "both"):` block, replace
`pdf_tables = await _tables_of(pdf_bytes, vision_llm, on_progress=_on_table_progress)` and the
`if not pdf_tables and vision_llm is None:` refusal with:

```python
        if word is not None:
            pdf_tables = word.tables
            if not pdf_tables:
                raise ValueError(
                    "Tidak ada tabel yang bisa dibaca dari dokumen Word ini. Tabel perlu ditempel "
                    "sebagai gambar EMF dari Excel dengan judul 'Tabel N.' atau 'Lampiran N.' "
                    "tepat di atasnya."
                )
        else:
            pdf_tables = await _tables_of(pdf_bytes, vision_llm, on_progress=_on_table_progress)
            if not pdf_tables and vision_llm is None:
                raise ValueError(
                    "Tidak ada tabel yang bisa dibaca dari lapisan teks PDF ini, dan mode tabel "
                    "internal tidak dapat memindai halamannya karena GOOGLE_API_KEY belum diatur."
                )
```

Then guard the number-format line so it only runs for PDFs:

```python
        if word is None:
            number_format_mix = await asyncio.to_thread(detect_number_format_mix, pdf_bytes)
```

(g) Before the final `return fact_result.model_copy(...)`, add:

```python
    if word is not None:
        # The narrative's block markers are not pages; a "Hal. 3" badge would send the reader to
        # the wrong place in the document.
        fact_result = fact_result.model_copy(update={"results": [
            r.model_copy(update={"page_number": None}) for r in fact_result.results]})
        if typo_result is not None:
            typo_result = typo_result.model_copy(update={"issues": [
                i.model_copy(update={"page_number": None}) for i in typo_result.issues]})
```

and add to the `update={...}` dict of that final return:

```python
        "unread_tables": (word.unread_tables
                          if word is not None and mode in ("internal", "both") else []),
```

- [ ] **Step 5: Run the tests to verify they pass**

Run: `./.venv/Scripts/python.exe -m pytest tests/test_main_word_upload.py -v`
Expected: 5 passed

- [ ] **Step 6: Run the whole suite**

Run: `./.venv/Scripts/python.exe -m pytest -q`
Expected: everything that passed before still passes, plus the new tests.

- [ ] **Step 7: Commit**

```bash
git add schemas.py main.py tests/test_main_word_upload.py
git commit -m "Check a Word report the same way as a PDF, against its own tables or Excel"
```

---

### Task 4: Let the upload card take Word files and say which tables it could not read

**Files:**
- Modify: `static/index.html` (the file helpers near `const isPdfFile` ~line 1113; `addPdfSource` ~lines 1380–1400; the report header around `${gapStrip}` ~line 1681)

**Interfaces:**
- Consumes: `PairedVerificationResponse.unread_tables` (Task 3).

- [ ] **Step 1: Accept .docx in the report dropzone**

Next to `const isPdfFile`, add:

```js
  const WORD_MIME = "application/vnd.openxmlformats-officedocument.wordprocessingml.document";
  const isWordFile  = (f) => f.type === WORD_MIME || /\.docx$/i.test(f.name);
  const isReportFile = (f) => isPdfFile(f) || isWordFile(f);
```

In `addPdfSource()`:
- change `accept="application/pdf"` to `accept="application/pdf,.pdf,.docx,${WORD_MIME}"`;
- change both `Pilih file PDF` strings (the `dz-sub` text and `placeholder:`) to `Pilih file PDF atau Word (.docx)`;
- change `accepts: isPdfFile,` to `accepts: isReportFile,`.

Change the empty-selection banner `Pilih file PDF terlebih dahulu.` to `Pilih file PDF atau Word terlebih dahulu.`

- [ ] **Step 2: Show the tables that could not be read**

Directly after the `const gapStrip = …;` statement, add:

```js
    // A Word report names a table by caption but its picture could not be read (a PNG, or no
    // picture at all). Claims only that table could answer come back Tidak Cukup Data; say why.
    const unread = data.unread_tables || [];
    const unreadStrip = unread.length ? `<div class="conflict-strip gap-strip">
        <span class="material-symbols-outlined">table_chart</span>
        <span><strong>${unread.length} tabel tidak bisa dibaca dari dokumen ini.</strong>
        Klaim yang hanya bisa dijawab tabel tersebut akan berstatus Tidak Cukup Data.
        <span class="gap-reason">${unread.map(escapeHtml).join("<br>")}</span></span>
      </div>` : "";
```

and change `</div>${gapStrip}${formatStrip}`; to `</div>${gapStrip}${unreadStrip}${formatStrip}`;.

- [ ] **Step 3: Check it in the browser**

Start the app with the login gate off. Per the `ui-browser-check` memory, set `APP_USERNAME=""` inside Python, not in PowerShell:

```bash
./.venv/Scripts/python.exe -c "import os; os.environ['APP_USERNAME']=''; os.environ['APP_PASSWORD']=''; import uvicorn; uvicorn.run('main:app', port=8000)"
```

Open http://localhost:8000. Expected:
- The report dropzone says "Pilih file PDF atau Word (.docx)". The picker offers .docx, and dropping `sample_data/test case_Analisis Uang Beredar Posisi April.docx` is accepted (no wrong-type flash).
- Run it in **internal** mode. The result shows fact cards with **no** "Hal." badge. The strip reads "1 tabel tidak bisa dibaca dari dokumen ini." and names Tabel 9.
- Dropping a `.xlsx` into the report zone is still refused.

- [ ] **Step 4: Commit**

```bash
git add static/index.html
git commit -m "Take a Word report in the upload card and name the tables it could not read"
```

---

### Task 5: Measure Word against PDF on the same report, and document it

**Files:**
- Modify: `README.md` (Mode 2 section)
- Memory: new project memory

- [ ] **Step 1: Run both versions of the April report through internal mode (paid: two Gemini extractions)**

Save as a scratch script (not in the repo) and run it with the venv python:

```python
import asyncio
from collections import Counter
from pathlib import Path
import main

FILES = ["sample_data/test case_Analisis Uang Beredar Posisi April 2026.pdf",
         "sample_data/test case_Analisis Uang Beredar Posisi April.docx"]

async def run(path):
    data = Path(path).read_bytes()
    return await main._run_paired_pipeline(data, Path(path).name, [], mode="internal",
                                           run_typo_check=False)

for path in FILES:
    r = asyncio.run(run(path))
    print(f"\n== {Path(path).name}: {r.total_facts} klaim", dict(Counter(x.verdict for x in r.results)))
    print("   tabel tak terbaca:", r.unread_tables)
    for x in r.results:
        if x.verdict == "Refuted":
            print(f"   TIDAK SESUAI  {x.metric_label} {x.claimed_value} vs {x.computed_value} "
                  f"[{x.matched_excel_source}]  «{x.context_quote[:90]}»")
```

Expected: both runs complete. The PDF run matches its known baseline (memory `snippet-table-split`: 0 Tidak Sesuai on this file). The Word run also shows **0 Tidak Sesuai**, with a similar claim count and no more Tidak Cukup Data than the PDF run.

Investigate every Tidak Sesuai in the Word run against the Word file before going on. Each one is either a reader bug (fix it with a test in Task 2's file) or a real error in the report. Tell the user the two lines of counts.

- [ ] **Step 2: The planted-error check, on Word**

Make a doctored copy in which the summary's "9,7% (yoy)" becomes "8,7% (yoy)":

```python
import re, shutil, zipfile
src = "sample_data/test case_Analisis Uang Beredar Posisi April.docx"
dst = "sample_data/test case salah_Analisis Uang Beredar Posisi April.docx"
with zipfile.ZipFile(src) as zin, zipfile.ZipFile(dst, "w", zipfile.ZIP_DEFLATED) as zout:
    for item in zin.infolist():
        data = zin.read(item.filename)
        if item.filename == "word/document.xml":
            text = data.decode("utf-8")
            new, n = re.subn(r"sebesar 9,7%", "sebesar 8,7%", text, count=1)
            assert n == 1, "phrase is split across runs; make this edit by hand in Word instead"
            data = new.encode("utf-8")
        zout.writestr(item, data)
```

If the assertion fires, ask the user to change the first "9,7% (yoy)" in the summary box to "8,7%" in Word and save it under the `dst` name. Then run the Step 1 script on `dst`.

Expected: exactly one Tidak Sesuai, M2 Maret 2026 yoy 8,7 vs 9,7…, and nothing else newly Refuted. (`sample_data/*.docx` is already gitignored.)

- [ ] **Step 3: Document it**

In `README.md`, in the Mode 2 section right after the modes table, add:

```markdown
**Word reports.** A `.docx` can be submitted wherever a PDF report is accepted (`pdf_file`, and
`reference_pdf`). Its narrative is read from the document itself and its tables from the EMF
pictures pasted from Excel. Each picture is paired with the `Tabel N.` / `Lampiran N.` caption
above it and read by the same text-layer reader as a digital PDF, so no vision model is needed.
A table pasted as a PNG, or a caption with no picture, is listed in `unread_tables`. Word has no
fixed pages, so results carry no `page_number`. Charts in a Word report are not checked. Legacy
`.doc` files are refused; re-save them as `.docx`.
```

- [ ] **Step 4: Record it in memory**

Write `C:\Users\Ivan Jehuda Angi\.claude\projects\C--Users-Ivan-Jehuda-Angi-Coding-fact-checker\memory\word_upload.md`:

```markdown
---
name: word-upload
description: Word (.docx) reports accepted like PDFs since 2026-09-30; tables are EMF pictures read via the PDF text-layer reader; PDF-vs-Word result on the April report
metadata:
  type: project
---

BI Word reports hold every Tabel/Lampiran as an EMF picture pasted from Excel, each preceded by
its caption paragraph; EMF keeps cells as positioned text records, so `word_extraction.py` turns
them into (y, line) rows and feeds `pdf_table_extraction._tables_on_page`. April report: 22 tables
from 14/15 captions (Tabel 9 rejected on purpose, same as PDF). Narrative is clean; blocks are cut
at page breaks and Lampiran captions only so `_filter_narrative` drops appendices — they are NOT
pages, and main clears page_number for Word results. Charts not checked for Word.

**Why:** the user asked (2026-09-29) whether Word would reduce the PDF bug class; roughly half
the historical false positives were PDF-reading problems.

**How to apply:** PDF vs Word counts on the April report: <fill in from Task 5 Step 1 when run>.
A Word table that fails to read shows in `unread_tables`. Check that first, then the EMF lines
(`emf_table_lines(emf_text_runs(...))`). Related: [[native-table-reader]], [[snippet-table-split]],
[[bank-soal-plan]].
```

Replace the `<fill in …>` line with the two count lines actually printed in Step 1, then add the index line to `MEMORY.md`:
`- [Word upload](word_upload.md) — .docx accepted like PDF; EMF table pictures read by the PDF text-layer reader; no pages, no charts`

- [ ] **Step 5: Commit**

```bash
git add README.md
git commit -m "Document Word report support"
```
