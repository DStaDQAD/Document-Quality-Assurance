# Per-Publication Parsers Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Every time-series sheet of the 12 BI publications is read without AI by a parser made for that publication, proven by the figures BI printed in the matching release.

**Architecture:** A new package `publication_parsers/` holds one module per publication plus `_common.py` (header-cell and label helpers, a cached workbook loader, and one time-series sheet engine driven by a per-sheet `SheetSpec`). UB/M0/Cadev wrap the proven SEKI reader, SK/SKDU wrap the generic reader, the other seven declare `SheetSpec`s for the engine. `paired_verifier._parse_table_with_fallback` tries the parser of the publication the checker picked first and falls back to today's cascade on any failure; `main` threads `publication` through `verify_paired`. Golden-number tests (`tests/fixtures/publications/<pub>/<period>/golden.json`) prove each parser against the release; the BI files themselves stay out of git.

**Tech Stack:** Python 3.13 (`.venv`), xlrd (formatting_info for .xls indents), openpyxl, pytest, pdfplumber (reading release text while writing golden files), FastAPI TestClient for the end-to-end runs with Gemini 2.5 Flash.

**Spec:** `docs/superpowers/specs/2026-10-07-publication-parsers-design.md`

## Global Constraints

- Publication keys are exactly `check_history.PUBLICATIONS`: `uang-beredar`, `uang-primer-m0`, `npi`, `pii`, `cadangan-devisa`, `sulni`, `sk`, `spe`, `skdu`, `pmi`, `sbank`, `shpr`.
- One module per publication in `publication_parsers/`; each exposes `SHEET_PATTERNS` (regexes matched with `re.fullmatch` against the stripped sheet name, case-insensitive) and `parse(data: bytes, sheet_name: str) -> TableData`, raising `PublicationParseError` on a layout it does not expect.
- The publication parser's result is reported under the publication key (e.g. `excel_parsers: ["sulni"]`).
- A publication parser must never make a check worse than today: any exception from it → `logger.warning` and the old cascade (bi → generic → llm → pointer-only) runs unchanged. No publication, unknown publication, or uncovered sheet → old cascade, publication parser not called.
- Bilingual labels keep the Indonesian part only; no aliases are added to `TableData`.
- Duplicate labels get the shortest ancestor prefix that makes them unique (`Barang > Ekspor`); unique labels stay bare.
- Sheet selection stays manual; nothing in the UI changes.
- BI files (`.xls`, `.xlsx`, `.pdf`, `.zip`) under `tests/fixtures/publications/` are never committed; `golden.json`, `README.md` and `show_table.py` there are.
- Golden tests skip an edition whose Excel is missing (CI has none); they must pass locally before merging.
- Fixture folders are named by the data period the release discusses: `YYYY-MM` or `YYYY-Qn`.
- Commit messages: one imperative sentence in the repo's style (see `git log`), **no** `Co-Authored-By` trailer.
- Always run Python through `.venv/Scripts/python` (the global `python` lacks xlrd and friends).
- Edit regex literals with the Edit/Write tools, never through Bash heredocs/sed (backslashes get mangled — this has silently broken regexes in this repo twice).
- `.gitignore` currently carries an uncommitted user line `presentation/`. Do not commit that line; stage only your own rule (Task 5 shows how).

## Review Focus

1. **A new edition shifts the layout** (BI adds a column or renames a header) — the check must still finish with today's behaviour, not crash. Test: Task 4 `test_cascade_falls_back_when_the_publication_parser_fails` (both `PublicationParseError` and an unexpected `IndexError`).
2. **The checker picks a covered workbook's non-time-series sheet** (SULNI `Tbl II.7` payment schedule) — it must go to the old cascade, not the engine. Test: Task 6 `test_sulni_covers_only_its_time_series_sheets`.
3. **The checker picks the wrong publication for the uploaded file** (publication `npi`, sheet `I.1`) — the sheet is not covered, so the old cascade reads it. Tests: Task 4 `test_covers` row `("cadangan-devisa", "I.1", False)` and `test_an_uncovered_sheet_is_left_to_the_generic_cascade`; Task 7 row `("npi", "I.1", False)`.
4. **Sheet names typed or exported slightly differently** (`Tabel1` vs `Tabel 1`, trailing space, other case, PMI's renamed `T1 - Komponen PMI`) — still covered. Tests: Task 4 `(" i.1a ", True)`, Task 10 `test_sbank_covers_its_sheets_however_they_are_spaced`, Task 12 `test_pmi_covers_both_sheet_namings`.
5. **One check reads several sheets of one big workbook** (SULNI: ~9 s per workbook load) — the workbook must be opened once, and the cell-pointer grid must not reopen it. Tests: Task 2 `test_load_grid_opens_each_workbook_once_and_hands_out_copies`, Task 4 `test_verify_paired_reads_each_grid_through_the_cached_loader`.

---

## File map

| File | Task | Responsibility |
|---|---|---|
| `table_parser_generic.py` (modify) | 1 | `load_workbook_grids(data)`: every sheet's grid from one workbook load |
| `publication_parsers/_common.py` (create) | 2, 3, 4 | header cells, labels, cached loader, indents (2); sheet engine (3); reader wrappers (4) |
| `publication_parsers/__init__.py` (create) | 4, 6–12 | registry `PARSERS`, `covers`, `parse_for_publication`, `load_grid` re-export |
| `publication_parsers/uang_beredar.py`, `uang_primer.py`, `cadangan_devisa.py` | 4 | SEKI reader wrappers |
| `publication_parsers/sulni.py` | 6 | engine specs |
| `publication_parsers/npi.py`, `pii.py` | 7 | engine specs (indent) |
| `publication_parsers/sk.py`, `skdu.py` | 8 | generic reader wrappers |
| `publication_parsers/spe.py`, `sbank.py`, `shpr.py`, `pmi.py` | 9–12 | engine specs |
| `paired_verifier.py` (modify) | 4 | publication-first dispatch, `publication` param, cached grid loader |
| `main.py` (modify) | 4 | pass `publication` to `verify_paired` |
| `tests/test_publication_parsers_common.py` (create) | 2, 3 | synthetic-grid tests (CI) |
| `tests/test_publication_parsers_registry.py` (create) | 4, 6–12 | coverage + wrapper tests (CI) |
| `tests/test_publication_golden.py` (create) | 5 | golden-number harness (local) |
| `tests/fixtures/publications/README.md`, `show_table.py`, `*/*/golden.json` | 5–12 | fixture docs, inspection helper, golden numbers |
| `docs/publication-parsers-e2e.md` (create) | 13 | end-to-end results |

---

### Task 1: Read every sheet of a workbook in one load

**Files:**
- Modify: `table_parser_generic.py:217-281` (`_load_grid`)
- Test: `tests/test_table_parser_generic.py`

**Interfaces:**
- Consumes: nothing new.
- Produces: `table_parser_generic.load_workbook_grids(data: bytes) -> Dict[str, List[List]]` — every sheet's grid (same cells, merged fill and %-scaling as `_load_grid`), keyed by sheet name in workbook order; raises `ValueError("Unrecognized file format…")` for non-workbook bytes. `_load_grid(data, sheet_name)` keeps its exact behaviour and messages.

- [ ] **Step 1: Write the failing tests**

Add to `tests/test_table_parser_generic.py` — extend the import block with `load_workbook_grids`, then append below the existing `_load_grid` tests:

```python
def test_load_workbook_grids_reads_every_sheet_in_one_go_like_load_grid():
    wb = Workbook()
    first = wb.active
    first.title = "Data"
    first["A1"] = "Judul"
    first.merge_cells("A1:C1")
    first["B2"] = 0.25
    first["B2"].number_format = "0.0%"
    wb.create_sheet("Catatan")["A1"] = 7
    data = _save(wb)

    grids = load_workbook_grids(data)

    assert list(grids) == ["Data", "Catatan"]
    assert grids["Data"] == _load_grid(data, "Data")
    assert grids["Catatan"] == _load_grid(data, "Catatan")
    assert grids["Data"][0][:3] == ["Judul"] * 3
    assert grids["Data"][1][1] == pytest.approx(25.0)


def test_load_workbook_grids_rejects_bytes_that_are_not_a_workbook():
    with pytest.raises(ValueError, match="Unrecognized file format"):
        load_workbook_grids(b"not a workbook")
```

- [ ] **Step 2: Run the tests to verify they fail**

Run: `.venv/Scripts/python -m pytest tests/test_table_parser_generic.py -k load_workbook_grids -v`
Expected: collection error — `ImportError: cannot import name 'load_workbook_grids'`.

- [ ] **Step 3: Split `_load_grid` into open/convert helpers and add `load_workbook_grids`**

In `table_parser_generic.py`, replace the whole `_load_grid` function (from `def _load_grid(data: bytes, sheet_name: str) -> List[List]:` to its final `raise ValueError("Unrecognized file format: expected .xls or .xlsx bytes.")`) with:

```python
_XLS_MAGIC = b'\xd0\xcf\x11\xe0\xa1\xb1\x1a\xe1'


def _open_xls(data: bytes):
    import xlrd

    # formatting_info exposes merged_cells; some .xls files can't provide it —
    # fall back to a plain open and skip the merged fill (same pattern as
    # excel_parser_bi._read_xls).
    try:
        return xlrd.open_workbook(file_contents=data, formatting_info=True)
    except Exception:
        return xlrd.open_workbook(file_contents=data)


def _xls_sheet_grid(wb, ws) -> List[List]:
    import xlrd

    grid: List[List] = []
    for r in range(ws.nrows):
        row = []
        for c in range(ws.ncols):
            v = ws.cell_value(r, c)
            if ws.cell_type(r, c) == xlrd.XL_CELL_DATE:
                v = xlrd.xldate.xldate_as_datetime(v, wb.datemode)
            row.append(v)
        grid.append(row)
    _apply_merged_fill(grid, list(getattr(ws, "merged_cells", []) or []))
    return grid


def _open_xlsx(data: bytes):
    import openpyxl

    # Not read_only: merged_cells.ranges is unavailable in read-only mode, and the
    # workbooks handled here are small enough to load fully.
    return openpyxl.load_workbook(io.BytesIO(data), data_only=True)


def _xlsx_sheet_grid(ws) -> List[List]:
    grid = []
    for row in ws.iter_rows():
        out = []
        for cell in row:
            v = cell.value
            # Excel scales %-formatted cells by 100 for DISPLAY only (0.0577
            # shown as "5.77%"). Narrative claims quote the displayed number,
            # so store the display scale. Quoted literals ('0"%"') don't scale
            # in Excel and are excluded. (.xls path: xlrd format lookup is
            # impractical, so this correction is xlsx-only.)
            if isinstance(v, (int, float)) and not isinstance(v, bool):
                fmt = re.sub(r'"[^"]*"', "", cell.number_format or "")
                if "%" in fmt:
                    v = v * 100
            out.append(v)
        grid.append(out)
    _apply_merged_fill(grid, [
        (m.min_row - 1, m.max_row, m.min_col - 1, m.max_col)
        for m in ws.merged_cells.ranges
    ])
    return grid


def _load_grid(data: bytes, sheet_name: str) -> List[List]:
    if data[:8] == _XLS_MAGIC:
        import xlrd

        wb = _open_xls(data)
        try:
            ws = wb.sheet_by_name(sheet_name)
        except xlrd.XLRDError:
            raise ValueError(f"Sheet '{sheet_name}' not found. Available: {wb.sheet_names()}")
        return _xls_sheet_grid(wb, ws)
    elif data[:4] == b'PK\x03\x04':
        wb = _open_xlsx(data)
        if sheet_name not in wb.sheetnames:
            raise ValueError(f"Sheet '{sheet_name}' not found. Available: {wb.sheetnames}")
        try:
            return _xlsx_sheet_grid(wb[sheet_name])
        finally:
            wb.close()
    else:
        raise ValueError("Unrecognized file format: expected .xls or .xlsx bytes.")


def load_workbook_grids(data: bytes) -> Dict[str, List[List]]:
    """Every sheet's grid from ONE workbook load, keyed by sheet name in workbook order.

    Opening a large .xlsx is the slow part (openpyxl must load all of it to see merged ranges;
    about 9 s for a SULNI workbook) while converting a loaded sheet is cheap, so a caller that
    needs several sheets of one workbook — publication_parsers.load_grid — takes them at once.
    """
    if data[:8] == _XLS_MAGIC:
        wb = _open_xls(data)
        return {ws.name: _xls_sheet_grid(wb, ws) for ws in wb.sheets()}
    elif data[:4] == b'PK\x03\x04':
        wb = _open_xlsx(data)
        try:
            return {ws.title: _xlsx_sheet_grid(ws) for ws in wb.worksheets}
        finally:
            wb.close()
    else:
        raise ValueError("Unrecognized file format: expected .xls or .xlsx bytes.")
```

`Dict` is already imported (`from typing import Dict, List, Optional, Tuple`).

- [ ] **Step 4: Run the generic parser tests**

Run: `.venv/Scripts/python -m pytest tests/test_table_parser_generic.py -v`
Expected: all PASS, including the two new tests and the existing `_load_grid` merged-fill / percent tests.

- [ ] **Step 5: Commit**

```bash
git add table_parser_generic.py tests/test_table_parser_generic.py
git commit -m "Read every sheet of a workbook from one load"
```

---

### Task 2: `_common` header cells, labels and the cached loader

**Files:**
- Create: `publication_parsers/__init__.py` (one-line docstring for now; Task 4 fills it)
- Create: `publication_parsers/_common.py`
- Test: `tests/test_publication_parsers_common.py`

**Interfaces:**
- Consumes: `table_parser_generic.load_workbook_grids` (Task 1), `table_parser_generic._bare_period_token`, `table_parser_generic._parse_period`.
- Produces (all in `publication_parsers._common`):
  - `class PublicationParseError(ValueError)`
  - `parse_year(value) -> Optional[int]`
  - `parse_period(value) -> Optional[str]` — `'Jan'..'Dec'` or `'Q1'..'Q4'`
  - `parse_year_period(value) -> Optional[Tuple[int, str]]`
  - `clean_label(raw) -> str`
  - `numbering_depth(raw) -> int`
  - `load_grid(data: bytes, sheet_name: str) -> List[List]` — raises `ValueError("Sheet '…' not found. Available: […]")`
  - `load_label_indents(data: bytes, sheet_name: str, col: int) -> Dict[int, int]` — .xls only, else `PublicationParseError`
  - module attribute `_WORKBOOK_CACHE` (tests clear it)

- [ ] **Step 1: Write the failing tests**

Create `tests/test_publication_parsers_common.py`:

```python
"""publication_parsers._common: header cells, label cleaning and the sheet engine on synthetic grids.

These run in CI (no BI files needed). The real workbooks are covered by test_publication_golden.py.
"""
import datetime

import pytest

from publication_parsers import _common
from publication_parsers._common import (
    PublicationParseError,
    clean_label,
    load_grid,
    load_label_indents,
    numbering_depth,
    parse_period,
    parse_year,
    parse_year_period,
)


# ---------------------------------------------------------------------------
# Header cells
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("raw, expected", [
    (2026, 2026), (2026.0, 2026), ("2026", 2026), ("2026*", 2026), ("2010.0", 2010),
    (datetime.datetime(2007, 1, 1), 2007),
    ("ITEMS", None), ("Perubahan (Poin)", None), (51.49, None), (12, None), (True, None), (None, None),
])
def test_parse_year(raw, expected):
    assert parse_year(raw) == expected


@pytest.mark.parametrize("raw, expected", [
    ("Jan", "Jan"), ("Mei", "May"), ("Agt*", "Aug"), ("Agu", "Aug"), ("Jul**", "Jul"), ("Des", "Dec"),
    ("QI", "Q1"), ("QIII", "Q3"), ("QIV", "Q4"), ("Q2", "Q2"), ("III*", "Q3"), ("I", "Q1"), ("Tw II", "Q2"),
    ("2007", None), (2007, None), ("Perubahan (Poin)", None), ("", None), (None, None),
])
def test_parse_period(raw, expected):
    assert parse_period(raw) == expected


@pytest.mark.parametrize("raw, expected", [
    ("Q1-2013", (2013, "Q1")), ("Q2-2026**", (2026, "Q2")), ("2009", None), (2009, None), (None, None),
])
def test_parse_year_period(raw, expected):
    assert parse_year_period(raw) == expected


# ---------------------------------------------------------------------------
# Labels
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("raw, expected", [
    ("Pemerintah / Government ", "Pemerintah"),
    ("Pemerintah dan Bank Sentral/ Government and Central Bank", "Pemerintah dan Bank Sentral"),
    ("2.1.1. Bank / Bank", "Bank"),
    ("  2.2   Bukan Lembaga Keuangan / Nonfinancial Corporations", "Bukan Lembaga Keuangan"),
    ("I. Transaksi Berjalan", "Transaksi Berjalan"),
    ("III. Transaksi Finansial \ufffd", "Transaksi Finansial"),
    ("V.1. NERACA PEMBAYARAN INDONESIA", "NERACA PEMBAYARAN INDONESIA"),
    ("A. Barang", "Barang"),
    ("a. Nonmigas", "Nonmigas"),
    ("- Ekspor, fob", "Ekspor, fob"),
    ("A.D.B", "A.D.B"),
    ("I.B.R.D", "I.B.R.D"),
    ("KPR/KPA", "KPR/KPA"),
    ("PMI - BI", "PMI - BI"),
    ("DSR Tier-1", "DSR Tier-1"),
    ("TOTAL (1+2) ", "TOTAL"),
    ("TOTAL ( 1 + 2 )", "TOTAL"),
    ("Total (I + II + III)", "Total"),
    ("Semarang **", "Semarang"),
    ("Pinjaman yang Diberikan 2)", "Pinjaman yang Diberikan"),
    ("Aset", "Aset"),
    ("1.", ""), ("  2.1 ", ""), ("a.", ""), ("-", ""), (3, ""), (None, ""),
])
def test_clean_label(raw, expected):
    assert clean_label(raw) == expected


@pytest.mark.parametrize("raw, expected", [
    ("1. Pemerintah", 1), ("1.1", 2), ("2.1.1. Bank", 3), ("  2.2   Bukan", 2), (7, 1),
    ("TOTAL (1+2)", 0), ("a.", 0), ("- Bilateral", 0), (None, 0),
])
def test_numbering_depth(raw, expected):
    assert numbering_depth(raw) == expected


# ---------------------------------------------------------------------------
# Loading
# ---------------------------------------------------------------------------

def test_load_grid_opens_each_workbook_once_and_hands_out_copies(monkeypatch):
    opened = []

    def fake_load(data):
        opened.append(data)
        return {"A": [[1, 2]], "B": [[3]]}

    monkeypatch.setattr(_common, "load_workbook_grids", fake_load)
    _common._WORKBOOK_CACHE.clear()

    first = load_grid(b"book-1", "A")
    first[0][0] = "edited by a caller"

    assert load_grid(b"book-1", "A") == [[1, 2]]
    assert load_grid(b"book-1", "B") == [[3]]
    assert opened == [b"book-1"]


def test_load_grid_names_the_available_sheets_when_one_is_missing(monkeypatch):
    monkeypatch.setattr(_common, "load_workbook_grids", lambda data: {"A": []})
    _common._WORKBOOK_CACHE.clear()

    with pytest.raises(ValueError, match=r"Sheet 'Z' not found\. Available: \['A'\]"):
        load_grid(b"book-2", "Z")


def test_label_indents_are_only_read_from_xls_files():
    with pytest.raises(PublicationParseError, match=r"\.xls"):
        load_label_indents(b"PK\x03\x04 an xlsx file", "5.1", 2)
```

- [ ] **Step 2: Run the tests to verify they fail**

Run: `.venv/Scripts/python -m pytest tests/test_publication_parsers_common.py -v`
Expected: collection error — `ModuleNotFoundError: No module named 'publication_parsers'`.

- [ ] **Step 3: Create the package and `_common.py`**

Create `publication_parsers/__init__.py`:

```python
"""Per-publication Excel parsers — one module per BI publication (registry added below)."""
```

Create `publication_parsers/_common.py`:

```python
"""Shared building blocks for the per-publication parsers.

Most BI publication workbooks are the same kind of sheet underneath: a block of label columns
on the left, a header made of a year row over a period row (or one 'Q1-2026' row), and one
column per month or quarter — with annual columns, preliminary-figure stars, an English mirror
on the right and a footnote block at the bottom. parse_series_sheet reads that shape from a
SheetSpec saying where the labels are and how the row hierarchy is encoded; each publication
module only declares its specs. Sheets the older readers already handle (SEKI monthly tables,
the survey workbooks) are wrapped instead of re-implemented.
"""
from __future__ import annotations

import datetime as _dt
import hashlib
import re
from collections import OrderedDict
from typing import Dict, List, Optional, Tuple

from table_parser_generic import _bare_period_token, _parse_period, load_workbook_grids

_XLS_MAGIC = b"\xd0\xcf\x11\xe0\xa1\xb1\x1a\xe1"


class PublicationParseError(ValueError):
    """A sheet this publication's parser covers does not have the layout the parser expects."""


# ---------------------------------------------------------------------------
# Header cells and labels
# ---------------------------------------------------------------------------

_YEAR_TEXT = re.compile(r"(\d{4})(?:\.0)?\**")
_ROMAN_QUARTER = {"I": "Q1", "II": "Q2", "III": "Q3", "IV": "Q4"}


def parse_year(value) -> Optional[int]:
    """A header cell's year (2026, 2026.0, '2026', '2026*', a date cell), else None."""
    if value is None or isinstance(value, bool):
        return None
    if isinstance(value, (_dt.datetime, _dt.date)):
        year = value.year
    elif isinstance(value, (int, float)):
        if not float(value).is_integer():
            return None
        year = int(value)
    elif isinstance(value, str):
        match = _YEAR_TEXT.fullmatch(value.strip())
        if not match:
            return None
        year = int(match.group(1))
    else:
        return None
    return year if 1950 <= year <= 2100 else None


def parse_period(value) -> Optional[str]:
    """A period-row cell as 'Jan'..'Dec' or 'Q1'..'Q4' (Indonesian/English, stars dropped)."""
    if not isinstance(value, str):
        return None
    text = value.strip().rstrip("*").strip()
    match = re.fullmatch(r"Q(I{1,3}|IV)", text, re.IGNORECASE)
    if match:
        return _ROMAN_QUARTER[match.group(1).upper()]
    return _bare_period_token(text) if text else None


def parse_year_period(value) -> Optional[Tuple[int, str]]:
    """A combined header cell ('Q1-2013', 'Q2-2026**', 'Jul 2026') as (year, period)."""
    if not isinstance(value, str):
        return None
    return _parse_period(value.strip().rstrip("*").strip())


# Outline markers at the start of a label: 'I.' / 'V.1.' (roman, optionally with a number),
# 'A.' / 'a.' (a single letter — never the first letter of an acronym such as 'A.D.B'),
# '1.' / '2.1.1.' (numbers), '-' (dash items).
_LEADING_NUMBERING = re.compile(
    r"(?:[IVX]+\.(?:\d+\.)*(?=\s|$)|[A-Za-z]\.(?=\s|$)|\d+(?:\.\d+)*\.?(?=\s|$)|-+)\s*"
)
_FORMULA_SUFFIX = re.compile(r"\s*\(\s*(?:[\dIVX]+\s*[+-]\s*)+[\dIVX]+\s*\)$")
_BILINGUAL_SPLIT = re.compile(r"\s+/\s*|\s*/\s+")
_FOOTNOTE_SUFFIX = re.compile(r"\s+\d[)\]]$")
# Stars mark preliminary figures; ¹²³ and U+FFFD are footnote superscripts (U+FFFD where the
# file lost them).
_TRAILING_MARKS = re.compile(r"[\s*\u00b9\u00b2\u00b3\ufffd]+$")


def clean_label(raw) -> str:
    """The Indonesian name in a label cell: numbering, English half, formula refs and stars off.

    'I. Transaksi Berjalan' -> 'Transaksi Berjalan', '2.1.1. Bank / Bank' -> 'Bank',
    'TOTAL (1+2)' -> 'TOTAL'. A cell holding only numbering ('1.', a bare row number) -> ''.
    """
    if raw is None or isinstance(raw, (bool, int, float)):
        return ""
    text = _BILINGUAL_SPLIT.split(" ".join(str(raw).split()))[0]
    while True:
        match = _LEADING_NUMBERING.match(text)
        if not match:
            break
        text = text[match.end():]
    text = _TRAILING_MARKS.sub("", text)
    return _FOOTNOTE_SUFFIX.sub("", _FORMULA_SUFFIX.sub("", text)).strip()


_NUMBERING_TOKEN = re.compile(r"(\d+(?:\.\d+)*)\.?(?=\s|$)")


def numbering_depth(raw) -> int:
    """How deep a label's own numbering goes: '1.' -> 1, '1.1' -> 2, '2.1.1. Bank' -> 3."""
    if isinstance(raw, bool):
        return 0
    if isinstance(raw, (int, float)):
        return 1
    if not isinstance(raw, str):
        return 0
    match = _NUMBERING_TOKEN.match(" ".join(raw.split()))
    return len(match.group(1).split(".")) if match else 0


# ---------------------------------------------------------------------------
# Reading the workbook
# ---------------------------------------------------------------------------

_WORKBOOK_CACHE: "OrderedDict[str, Dict[str, List[List]]]" = OrderedDict()
_WORKBOOK_CACHE_SIZE = 4


def load_grid(data: bytes, sheet_name: str) -> List[List]:
    """The sheet's cell values with merged ranges filled; each workbook is opened once.

    openpyxl has to load a whole .xlsx to see its merged ranges — about 9 seconds for a SULNI
    workbook — while turning a loaded sheet into a grid is cheap. So the first request for a
    workbook converts every sheet, and later sheets of that workbook (and the cell-pointer grid
    paired_verifier reads after parsing) come from memory. Rows are copied on the way out so a
    caller that edits its grid cannot change the cached one.
    """
    digest = hashlib.sha1(data).hexdigest()
    sheets = _WORKBOOK_CACHE.get(digest)
    if sheets is None:
        sheets = load_workbook_grids(data)
        _WORKBOOK_CACHE[digest] = sheets
        if len(_WORKBOOK_CACHE) > _WORKBOOK_CACHE_SIZE:
            _WORKBOOK_CACHE.popitem(last=False)
    else:
        _WORKBOOK_CACHE.move_to_end(digest)
    if sheet_name not in sheets:
        raise ValueError(f"Sheet '{sheet_name}' not found. Available: {list(sheets)}")
    return [list(row) for row in sheets[sheet_name]]


def load_label_indents(data: bytes, sheet_name: str, col: int) -> Dict[int, int]:
    """Row -> indent level of the label cell in `col` (.xls only: where SEKI keeps hierarchy)."""
    if data[:8] != _XLS_MAGIC:
        raise PublicationParseError("indentasi label hanya terbaca dari file .xls")
    import xlrd

    try:
        wb = xlrd.open_workbook(file_contents=data, formatting_info=True)
        ws = wb.sheet_by_name(sheet_name)
    except Exception as exc:
        raise PublicationParseError(f"indentasi label tidak terbaca: {exc}") from exc
    if col >= ws.ncols:
        return {}
    return {
        r: int(wb.xf_list[ws.cell_xf_index(r, col)].alignment.indent_level or 0)
        for r in range(ws.nrows)
    }
```

- [ ] **Step 4: Run the tests to verify they pass**

Run: `.venv/Scripts/python -m pytest tests/test_publication_parsers_common.py -v`
Expected: all PASS.

- [ ] **Step 5: Commit**

```bash
git add publication_parsers/__init__.py publication_parsers/_common.py tests/test_publication_parsers_common.py
git commit -m "Add the shared header, label and workbook helpers for publication parsers"
```

---

### Task 3: The time-series sheet engine

**Files:**
- Modify: `publication_parsers/_common.py` (append; extend imports)
- Test: `tests/test_publication_parsers_common.py` (append)

**Interfaces:**
- Consumes: Task 2 helpers.
- Produces (in `publication_parsers._common`):
  - `@dataclass(frozen=True) class SheetSpec: label_cols: Tuple[int, ...]; header: str = "two_rows"; hierarchy: str = "flat"; bands: bool = False; unit: Optional[str] = None` — `header` ∈ `"two_rows" | "combined"`, `hierarchy` ∈ `"flat" | "indent" | "numbering" | "first_col" | "sections"`.
  - `parse_series_sheet(grid: List[List], spec: SheetSpec, indents: Optional[Dict[int, int]] = None) -> TableData` — temporal `TableData`; raises `PublicationParseError` (`"…tahun/periode…"` when no header, `"…berisi angka…"` when no data rows).
  - `sheet_matches(pattern: str, sheet_name: str) -> bool`
  - `spec_for(sheet_name: str, specs: Sequence[Tuple[str, SheetSpec]]) -> SheetSpec` — raises `PublicationParseError` when no pattern matches.
  - `parse_with_specs(data: bytes, sheet_name: str, specs: Sequence[Tuple[str, SheetSpec]]) -> TableData` — loads the grid through `load_grid`, indents through `load_label_indents(…, spec.label_cols[-1])` when `hierarchy == "indent"`.

- [ ] **Step 1: Write the failing tests**

In `tests/test_publication_parsers_common.py`, add `SheetSpec` and `parse_series_sheet` to the `from publication_parsers._common import (...)` block, then append:

```python
# ---------------------------------------------------------------------------
# The sheet engine
# ---------------------------------------------------------------------------

def test_monthly_sheet_skips_annual_columns_reads_sparse_years_and_ignores_the_mirror():
    grid = [
        [None, "Tabel I.1", None, None, None, None, None, None, None, None],
        [None, None, "Posisi Utang Luar Negeri / External Debt", None, None, None, None, None, None, None],
        [None, None, None, None, None, None, None, "(Juta USD / Million of USD)", None, None],
        [None, None, None, 2013, 2025, None, 2026, "ITEMS", 2026, None],
        [None, None, None, 2013, "Jul", "Agu*", "Jul**", "ITEMS", "Jul", None],
        [None, "1.", "Pemerintah / Government", 100.0, 110.0, 111.0, 120.0, "Government", 999.0, None],
        [None, "2.", "Swasta / Private", 50.0, 60.0, 61.0, 59.0, "Private", 999.0, None],
        [None, "TOTAL (1+2)", None, 150.0, 170.0, 172.0, 179.0, "Total", 999.0, None],
        [None, "Keterangan: * angka sementara", None, None, None, None, None, None, None, None],
    ]

    table = parse_series_sheet(grid, SheetSpec(label_cols=(1, 2), hierarchy="numbering"))

    assert table.title == "Posisi Utang Luar Negeri"
    assert table.unit == "Juta USD"
    assert table.row_labels == ["Pemerintah", "Swasta", "TOTAL"]
    assert table.lookup("Swasta", 2026, "Jul") == 59.0     # the mirror's 999 never wins
    assert table.lookup("Swasta", 2025, "Aug") == 61.0     # year carried over a blank cell
    assert not any(year == 2013 for _, year, _ in table._data)  # annual column skipped


def test_duplicate_labels_take_their_parents_from_the_indent_hierarchy():
    grid = [
        ["NERACA PEMBAYARAN", None, None, None],
        ["(Juta USD)", None, None, None],
        [None, "KETERANGAN", 2026, 2026],
        [None, "KETERANGAN", "Q1", "Q2*"],
        [None, "I. Transaksi Berjalan", 1.0, 2.0],
        [None, "A. Barang", 3.0, 4.0],
        [None, "- Ekspor", 5.0, 6.0],
        [None, "B. Jasa", 7.0, 8.0],
        [None, "- Ekspor", 9.0, 10.0],
    ]
    indents = {4: 0, 5: 1, 6: 2, 7: 1, 8: 2}

    table = parse_series_sheet(grid, SheetSpec(label_cols=(1,), hierarchy="indent"), indents)

    assert table.row_labels == [
        "Transaksi Berjalan", "Barang", "Barang > Ekspor", "Jasa", "Jasa > Ekspor",
    ]
    assert table.lookup("Jasa > Ekspor", 2026, "Q2") == 10.0
    assert table.unit == "Juta USD"


def test_a_label_still_ambiguous_under_its_parent_takes_the_grandparent_too():
    short, long_ = "1. Utang Jangka Pendek / Short-term", "2. Utang Jangka Panjang / Long-term"
    grid = [
        [None, None, None, 2026, 2026],
        [None, None, None, "Jun", "Jul"],
        [None, short, "1. Pemerintah dan Bank Sentral", 1.0, 2.0],
        [None, short, "1.1 Pemerintah", 3.0, 4.0],
        [None, short, "Total", 5.0, 6.0],
        [None, long_, "1. Pemerintah dan Bank Sentral", 7.0, 8.0],
        [None, long_, "1.1 Pemerintah", 9.0, 10.0],
        [None, long_, "Total", 11.0, 12.0],
    ]

    table = parse_series_sheet(
        grid, SheetSpec(label_cols=(1, 2), hierarchy="numbering", unit="Juta USD")
    )

    assert table.row_labels == [
        "Utang Jangka Pendek > Pemerintah dan Bank Sentral",
        "Utang Jangka Pendek > Pemerintah dan Bank Sentral > Pemerintah",
        "Utang Jangka Pendek > Total",
        "Utang Jangka Panjang > Pemerintah dan Bank Sentral",
        "Utang Jangka Panjang > Pemerintah dan Bank Sentral > Pemerintah",
        "Utang Jangka Panjang > Total",
    ]
    assert table.lookup("Utang Jangka Panjang > Pemerintah dan Bank Sentral > Pemerintah", 2026, "Jul") == 10.0


def test_letter_and_dash_items_nest_by_the_column_they_start_in():
    grid = [
        [None, None, None, None, 2026],
        [None, None, None, None, "Jul"],
        [None, "1.", "Pemerintah / Government", None, 10.0],
        [None, None, "a.", "Pinjaman / Loan", 4.0],
        [None, None, None, "-  Bilateral", 1.0],
        [None, "2.", "Bank Sentral / Central Bank", None, 6.0],
        [None, None, "a.", "Pinjaman / Loan", 0.0],
        [None, None, None, "-  Bilateral", 0.0],
    ]

    table = parse_series_sheet(
        grid, SheetSpec(label_cols=(1, 2, 3), hierarchy="numbering", unit="Juta USD")
    )

    assert table.row_labels == [
        "Pemerintah", "Pemerintah > Pinjaman", "Pemerintah > Pinjaman > Bilateral",
        "Bank Sentral", "Bank Sentral > Pinjaman", "Bank Sentral > Pinjaman > Bilateral",
    ]


def test_section_rows_head_their_block_and_bands_split_the_columns():
    grid = [
        [None, None, None, None, "TRIWULANAN (QTQ)", None, "TAHUNAN (YOY)", None],
        [None, "No.", "KOTA", "TIPE", 2026, 2026, 2026, 2026],
        [None, None, None, None, "Q1", "Q2", "Q1", "Q2"],
        [None, None, "BANDUNG", None, None, None, None, None],
        [None, 1, "  KECIL", "KECIL", 0.1, 0.2, 1.1, 1.2],
        [None, None, "TOTAL", "TOTAL", 0.3, 0.4, 1.3, 1.4],
        [None, None, "DENPASAR", None, None, None, None, None],
        [None, 2, "  KECIL", "KECIL", 0.5, 0.6, 1.5, 1.6],
    ]

    table = parse_series_sheet(
        grid, SheetSpec(label_cols=(2,), hierarchy="sections", bands=True, unit="%")
    )

    assert table.lookup("TAHUNAN (YOY) > DENPASAR > KECIL", 2026, "Q2") == 1.6
    assert table.lookup("TRIWULANAN (QTQ) > BANDUNG > TOTAL", 2026, "Q1") == 0.3
    assert "BANDUNG" not in table.row_labels  # a section heads rows; it holds no figures


def test_combined_header_nests_by_the_first_label_column():
    rasio, rasio_pem = (
        "- Rasio Pembayaran Utang / Debt Service Ratio",
        "- Rasio Pembayaran Utang Pemerintah / Government DSR",
    )
    grid = [
        [None, None, None, "(Dalam persen / In percent)", None, None],
        [None, None, None, "2009", "Q1-2013", "Q2-2026**"],
        [None, None, None, "2009", "Q1-2013", "Q2-2026**"],
        [None, rasio, rasio, 30.0, 25.0, 0.0],
        [None, None, "- DSR Tier-1", 20.0, 15.0, 13.7],
        [None, rasio_pem, rasio_pem, 10.0, 9.0, 0.0],
        [None, None, "- DSR Tier-1", 5.0, 4.0, 14.4],
    ]

    table = parse_series_sheet(
        grid, SheetSpec(label_cols=(1, 2), header="combined", hierarchy="first_col")
    )

    assert table.unit == "Dalam persen"
    assert table.lookup("Rasio Pembayaran Utang Pemerintah > DSR Tier-1", 2026, "Q2") == 14.4
    assert table.lookup("Rasio Pembayaran Utang > DSR Tier-1", 2013, "Q1") == 15.0
    assert not any(year == 2009 for _, year, _ in table._data)


def test_stacked_blocks_each_use_their_own_header_and_numbers_stored_as_text_are_read():
    grid = [
        [None, "Tabel 4. Suku Bunga", None, None, None, None, None],
        [None, "Periode", "Jenis Valuta", None, "2025", "2026", None],
        [None, "Periode", "Jenis Valuta", None, "IV", "I", None],
        [None, "Realisasi per Triwulan", "Rupiah", None, "5.70", "5.61", None],
        [None, "Periode", "Jenis Valuta", "Period", None, "2026", "2026"],
        [None, "Periode", "Jenis Valuta", "Period", None, "I", "II*"],
        [None, "Prakiraan per Triwulan", "Rupiah", "Estimation", None, "na", "5.40"],
        [None, "**) Sejak periode survei triwulan II 2021 ...", None, None, None, None, None],
    ]

    table = parse_series_sheet(grid, SheetSpec(label_cols=(1, 2), unit="%"))

    assert table.row_labels == ["Realisasi per Triwulan > Rupiah", "Prakiraan per Triwulan > Rupiah"]
    assert table.lookup("Realisasi per Triwulan > Rupiah", 2025, "Q4") == 5.70
    assert table.lookup("Prakiraan per Triwulan > Rupiah", 2026, "Q2") == 5.40
    assert table.lookup("Prakiraan per Triwulan > Rupiah", 2026, "Q1") is None  # 'na'


def test_a_sheet_without_a_period_header_is_rejected():
    grid = [["Daftar Seri SBN", None, None], [None, "FR0100", 5.0], [None, "FR0101", 6.0]]

    with pytest.raises(PublicationParseError, match="tahun/periode"):
        parse_series_sheet(grid, SheetSpec(label_cols=(1,)))


def test_a_header_without_any_figures_below_it_is_rejected():
    grid = [[None, None, 2026, 2026], [None, None, "Jan", "Feb"], [None, "Catatan", None, None]]

    with pytest.raises(PublicationParseError, match="berisi angka"):
        parse_series_sheet(grid, SheetSpec(label_cols=(1,)))
```

- [ ] **Step 2: Run the tests to verify they fail**

Run: `.venv/Scripts/python -m pytest tests/test_publication_parsers_common.py -v`
Expected: collection error — `ImportError: cannot import name 'SheetSpec'`.

- [ ] **Step 3: Append the engine to `_common.py`**

Change the imports at the top of `publication_parsers/_common.py` to:

```python
import datetime as _dt
import hashlib
import re
from collections import Counter, OrderedDict
from dataclasses import dataclass
from typing import Dict, List, Optional, Sequence, Tuple

from table_model import QUAL_SEP, TableData
from table_parser_generic import _bare_period_token, _parse_period, load_workbook_grids
```

Append to the end of the file:

```python
# ---------------------------------------------------------------------------
# The time-series sheet engine
# ---------------------------------------------------------------------------

@dataclass(frozen=True)
class SheetSpec:
    """Where a sheet keeps its labels and how its rows nest.

    label_cols: the Indonesian label columns, left to right (English mirror columns left out).
      A text repeated across them by a merge counts once; the earlier texts name the row's group.
    header: "two_rows" (a year row over a period row) or "combined" (one 'Q1-2026' row).
    hierarchy: how a row finds its parent inside its group —
      "flat"       no nesting beyond the label columns themselves;
      "indent"     the label cell's indent level (SEKI .xls tables);
      "numbering"  the label's own numbering ('2.1.1.' sits under '2.1');
      "first_col"  the label column the text starts in (a later column is a child);
      "sections"   a label row without figures heads the rows below it.
    bands: the row above the year row splits the columns into bands ('TRIWULANAN (QTQ)' vs
      'TAHUNAN (YOY)'); the band becomes the outermost label level.
    unit: replaces the unit read from the title block.
    """
    label_cols: Tuple[int, ...]
    header: str = "two_rows"
    hierarchy: str = "flat"
    bands: bool = False
    unit: Optional[str] = None


_FOOTER = re.compile(r"\s*(\*|(keterangan|catatan|sumber|note|source)\s*:)", re.IGNORECASE)
_NUMBER_TEXT = re.compile(r"-?\d+(?:\.\d+)?(?:[eE][-+]?\d+)?")
_UNIT_CELL = re.compile(r"\((.*)\)")
_TABLE_NUMBER = re.compile(r"(tabel|table)\s+[\w.]+", re.IGNORECASE)


def _cell(row: Sequence, c: int):
    return row[c] if c < len(row) else None


def _blank(value) -> bool:
    return value is None or (isinstance(value, str) and not value.strip())


def _number(value) -> Optional[float]:
    if isinstance(value, bool):
        return None
    if isinstance(value, (int, float)):
        return float(value)
    if isinstance(value, str) and _NUMBER_TEXT.fullmatch(value.strip()):
        return float(value.strip())
    return None


def _header_rows(grid: List[List], spec: SheetSpec, first_col: int) -> List[int]:
    """Every row that heads a block of figures (a sheet may stack several blocks)."""
    rows = []
    for r, row in enumerate(grid):
        if spec.header == "combined":
            hit = sum(1 for c in range(first_col, len(row)) if parse_year_period(row[c])) >= 2
        else:
            below = grid[r + 1] if r + 1 < len(grid) else []
            hit = any(parse_year(row[c]) and parse_period(_cell(below, c))
                      for c in range(first_col, len(row)))
        if hit:
            rows.append(r)
    if not rows:
        raise PublicationParseError("baris tahun/periode tidak ditemukan")
    return rows


def _period_columns(
    grid: List[List], spec: SheetSpec, header_row: int, first_col: int
) -> Dict[int, Tuple[str, int, str]]:
    """{column: (band, year, period)} for every monthly/quarterly column, leftmost copy only."""
    head = grid[header_row]
    below = grid[header_row + 1] if spec.header == "two_rows" else []
    band_row = grid[header_row - 1] if spec.bands and header_row > 0 else []
    columns: Dict[int, Tuple[str, int, str]] = {}
    seen = set()
    year: Optional[int] = None
    band = ""
    for c in range(first_col, max(len(head), len(below), len(band_row))):
        if not _blank(_cell(band_row, c)):
            band = clean_label(_cell(band_row, c))
        if spec.header == "combined":
            parsed = parse_year_period(_cell(head, c))
            if parsed is None:
                continue
            col_year, period = parsed
        else:
            if not _blank(_cell(head, c)):
                year = parse_year(_cell(head, c))  # non-year text ('ITEMS') ends the run
            col_year, period = year, parse_period(_cell(below, c))
            if col_year is None or period is None:
                continue  # annual column, mirror heading, 'Perubahan' block
        key = (band, col_year, period)
        if key in seen:
            continue  # the English mirror repeats periods already read: leftmost wins
        seen.add(key)
        columns[c] = key
    return columns


def _title_and_unit(rows: List[List]) -> Tuple[str, str]:
    title = unit = ""
    for row in rows:
        for value in row:
            if not isinstance(value, str) or not value.strip():
                continue
            text = " ".join(value.split())
            match = _UNIT_CELL.fullmatch(text)
            if match:
                unit = unit or match.group(1).split(" / ")[0].strip(" ()")
            elif not title and not _TABLE_NUMBER.fullmatch(text):
                title = clean_label(text)
    return title, unit


def _depth(spec: SheetSpec, r: int, row: List, raw: List, own: int, has_data: bool,
           indents: Optional[Dict[int, int]]) -> int:
    if spec.hierarchy == "indent":
        return (indents or {}).get(r, 0)
    first_col = next(i for i, c in enumerate(spec.label_cols) if not _blank(_cell(row, c)))
    if spec.hierarchy == "numbering":
        # '2.1.1.' says its own depth; 'a.' / '-' items and unnumbered rows nest by the
        # column they start in, one level below a '1.' that starts in the same column.
        depth = numbering_depth(raw[own])
        if not depth and own > 0 and not clean_label(raw[own - 1]):
            depth = numbering_depth(raw[own - 1])
        return depth or first_col + 1
    if spec.hierarchy == "first_col":
        return first_col
    if spec.hierarchy == "sections":
        return 1 if has_data else 0
    return 0


def _shortest_unique_names(paths: Sequence[Sequence[str]]) -> List[str]:
    """Each row's name: its own label, lengthened with ancestors only while it stays ambiguous."""
    names = [p[-1] for p in paths]
    for depth in range(2, max(len(p) for p in paths) + 1):
        counts = Counter(names)
        if all(n == 1 for n in counts.values()):
            break
        names = [
            QUAL_SEP.join(p[-depth:]) if counts[name] > 1 else name
            for p, name in zip(paths, names)
        ]
    return names


def parse_series_sheet(
    grid: List[List], spec: SheetSpec, indents: Optional[Dict[int, int]] = None
) -> TableData:
    """Read one time-series sheet laid out as `spec` describes into a temporal TableData."""
    first_col = max(spec.label_cols) + 1
    header_rows = _header_rows(grid, spec, first_col)
    blocks = {r: _period_columns(grid, spec, r, first_col) for r in header_rows}
    header_like = set(header_rows)
    if spec.header == "two_rows":
        header_like |= {r + 1 for r in header_rows}
    # Column headings ('Periode', 'Jenis Valuta', ...) repeat in the label cells of every
    # header row a block has, including extra ones such as 'Prakiraan 2026' under the periods.
    headings = {
        tuple(clean_label(_cell(grid[r], c)) for c in spec.label_cols)
        for r in header_like if r < len(grid)
    }
    title, unit = _title_and_unit(grid[:header_rows[0]])

    entries: List[Tuple[List[str], Dict[Tuple[int, str], float]]] = []
    columns: Dict[int, Tuple[str, int, str]] = {}
    stack: List[Tuple[int, str]] = []
    group: Optional[Tuple[str, ...]] = None
    for r in range(header_rows[0], len(grid)):
        if r in blocks:
            columns, stack, group = blocks[r], [], None
        if r in header_like:
            continue
        row = grid[r]
        raw: List = []
        for c in spec.label_cols:
            value = _cell(row, c)
            if not _blank(value) and not (raw and value == raw[-1]):
                raw.append(value)
        texts = [clean_label(v) for v in raw]
        if not any(texts):
            continue
        if isinstance(raw[0], str) and _FOOTER.match(raw[0]):
            break
        if tuple(clean_label(_cell(row, c)) for c in spec.label_cols) in headings:
            continue
        own = max(i for i, t in enumerate(texts) if t)
        ancestors = tuple(t for t in texts[:own] if t)
        by_band: Dict[str, Dict[Tuple[int, str], float]] = {}
        for c, (band, year, period) in columns.items():
            value = _number(_cell(row, c))
            if value is not None:
                by_band.setdefault(band, {})[(year, period)] = value
        depth = _depth(spec, r, row, raw, own, bool(by_band), indents)
        if ancestors != group:
            stack, group = [], ancestors
        while stack and stack[-1][0] >= depth:
            stack.pop()
        path = list(ancestors) + [label for _, label in stack] + [texts[own]]
        stack.append((depth, texts[own]))
        for band, values in by_band.items():
            entries.append(([band] + path if band else path, values))
    if not entries:
        raise PublicationParseError("tidak ada baris berisi angka di bawah baris tahun")

    table = TableData(title=title, unit=spec.unit or unit, row_labels=[])
    for name, (_, values) in zip(_shortest_unique_names([p for p, _ in entries]), entries):
        if name in table.row_labels:
            continue  # identical path twice in one sheet: first occurrence wins
        table.row_labels.append(name)
        for (year, period), value in values.items():
            table._data[(name, year, period)] = value
    return table


# ---------------------------------------------------------------------------
# Glue for the publication modules
# ---------------------------------------------------------------------------

def sheet_matches(pattern: str, sheet_name: str) -> bool:
    return re.fullmatch(pattern, sheet_name.strip(), re.IGNORECASE) is not None


def spec_for(sheet_name: str, specs: Sequence[Tuple[str, SheetSpec]]) -> SheetSpec:
    for pattern, spec in specs:
        if sheet_matches(pattern, sheet_name):
            return spec
    raise PublicationParseError(f"sheet '{sheet_name}' tidak dikenal parser ini")


def parse_with_specs(
    data: bytes, sheet_name: str, specs: Sequence[Tuple[str, SheetSpec]]
) -> TableData:
    spec = spec_for(sheet_name, specs)
    indents = (
        load_label_indents(data, sheet_name, spec.label_cols[-1])
        if spec.hierarchy == "indent" else None
    )
    return parse_series_sheet(load_grid(data, sheet_name), spec, indents)
```

- [ ] **Step 4: Run the tests to verify they pass**

Run: `.venv/Scripts/python -m pytest tests/test_publication_parsers_common.py -v`
Expected: all PASS (the Task 2 cases and the nine engine tests).

- [ ] **Step 5: Commit**

```bash
git add publication_parsers/_common.py tests/test_publication_parsers_common.py
git commit -m "Add the time-series sheet engine the publication parsers share"
```

---

### Task 4: Registry, SEKI wrappers and publication-first dispatch

**Files:**
- Modify: `publication_parsers/__init__.py`, `publication_parsers/_common.py` (append wrappers)
- Create: `publication_parsers/uang_beredar.py`, `publication_parsers/uang_primer.py`, `publication_parsers/cadangan_devisa.py`
- Modify: `paired_verifier.py:43-48` (imports), `paired_verifier.py:1813-1852` (`_parse_table_with_fallback`), `paired_verifier.py:2575-2641` (`verify_paired`)
- Modify: `main.py:791-803` (`verify_paired(...)` call)
- Test: `tests/test_publication_parsers_registry.py` (create), `tests/test_paired_verifier.py`, `tests/test_main_paired_endpoint.py`

**Interfaces:**
- Consumes: Task 3 `sheet_matches`; `excel_parser_bi.parse_bi_table`; `table_parser_generic.parse_generic_table`.
- Produces:
  - `publication_parsers.PARSERS: Dict[str, ModuleType]`
  - `publication_parsers.covers(publication: str, sheet_name: str) -> bool`
  - `publication_parsers.parse_for_publication(publication: str, data: bytes, sheet_name: str) -> Optional[TableData]` — `None` when not covered; may raise anything the module raises.
  - `publication_parsers.load_grid`, `publication_parsers.PublicationParseError` (re-exports)
  - `_common.parse_with_bi_reader(data, sheet_name) -> TableData`, `_common.parse_with_generic_reader(data, sheet_name, unit: str = "") -> TableData`
  - `paired_verifier._parse_table_with_fallback(excel_bytes, sheet_name, llm=None, publication: str = "")`
  - `paired_verifier.verify_paired(..., publication: str = "")`

- [ ] **Step 1: Write the failing registry tests**

Create `tests/test_publication_parsers_registry.py`:

```python
"""Which sheets each publication parser claims, and how the reader wrappers report failure."""
from unittest.mock import patch

import pytest

from publication_parsers import PublicationParseError, covers, parse_for_publication
from table_model import TableData


@pytest.mark.parametrize("publication, sheet, expected", [
    ("uang-beredar", "I.1", True),
    ("uang-beredar", "I.1A", True),
    ("uang-beredar", " i.1a ", True),
    ("uang-beredar", "I.10", False),
    ("uang-beredar", "Th 1985-1992", False),
    ("uang-primer-m0", "I.2", True),
    ("uang-primer-m0", "Th 2010-2021", False),
    ("cadangan-devisa", "5.9", True),
    ("cadangan-devisa", "5.90", False),
    ("cadangan-devisa", "I.1", False),
    ("", "I.1", False),
    ("tidak-ada", "I.1", False),
])
def test_covers(publication, sheet, expected):
    assert covers(publication, sheet) is expected


def test_an_uncovered_sheet_is_left_to_the_generic_cascade():
    assert parse_for_publication("cadangan-devisa", b"never read", "I.1") is None


def test_seki_wrapper_reports_an_unreadable_file_as_a_publication_error():
    with pytest.raises(PublicationParseError, match="SEKI"):
        parse_for_publication("uang-beredar", b"not an excel file", "I.1")


@patch("publication_parsers._common.parse_bi_table")
def test_seki_wrapper_rejects_a_table_whose_hierarchy_collapsed(mock_bi):
    mock_bi.return_value = TableData(
        title="t", unit="Miliar Rp", row_labels=["Rupiah", "Rupiah"],
        _data={("Rupiah", 2026, "Jul"): 1.0},
    )

    with pytest.raises(PublicationParseError, match="berulang"):
        parse_for_publication("uang-beredar", b"bytes", "I.1")


@patch("publication_parsers._common.parse_bi_table")
def test_seki_wrapper_rejects_a_table_without_figures(mock_bi):
    mock_bi.return_value = TableData(title="t", unit="Miliar Rp", row_labels=["M2"])

    with pytest.raises(PublicationParseError, match="angka"):
        parse_for_publication("uang-primer-m0", b"bytes", "I.2")
```

- [ ] **Step 2: Write the failing dispatch tests**

In `tests/test_paired_verifier.py`, add `import logging` at the top, add `from publication_parsers import PublicationParseError` after the `from excel_parser_bi import BITableData` line, then append below `test_cascade_falls_back_to_generic_when_bi_parse_is_empty` (the cascade section, ~line 1345):

```python
@patch("paired_verifier.parse_bi_table")
@patch("paired_verifier.parse_for_publication")
def test_cascade_uses_the_publication_parser_first(mock_pub, mock_bi):
    mock_pub.return_value = _make_table(data={("TOTAL", 2026, "Jul"): 454759.8})

    table, parser = _parse_table_with_fallback(b"bytes", "TabI.1", publication="sulni")

    assert parser == "sulni"
    assert table.lookup("TOTAL", 2026, "Jul") == 454759.8
    mock_pub.assert_called_once_with("sulni", b"bytes", "TabI.1")
    mock_bi.assert_not_called()


@pytest.mark.parametrize("failure", [PublicationParseError("kolom bergeser"), IndexError("bug")])
@patch("paired_verifier.parse_bi_table")
@patch("paired_verifier.parse_for_publication")
def test_cascade_falls_back_when_the_publication_parser_fails(mock_pub, mock_bi, failure, caplog):
    mock_pub.side_effect = failure
    mock_bi.return_value = _make_table(data={("Total", 2026, "Jul"): 1.0})

    with caplog.at_level(logging.WARNING):
        table, parser = _parse_table_with_fallback(b"bytes", "TabI.1", publication="sulni")

    assert parser == "bi"
    assert "sulni" in caplog.text


@patch("paired_verifier.parse_bi_table")
@patch("paired_verifier.parse_for_publication")
def test_cascade_reads_an_uncovered_sheet_the_old_way(mock_pub, mock_bi):
    mock_pub.return_value = None
    mock_bi.return_value = _make_table(data={("Total", 2026, "Jul"): 1.0})

    _, parser = _parse_table_with_fallback(b"bytes", "Tbl II.7", publication="sulni")

    assert parser == "bi"


@patch("paired_verifier.parse_bi_table")
@patch("paired_verifier.parse_for_publication")
def test_cascade_without_a_publication_never_asks_a_publication_parser(mock_pub, mock_bi):
    mock_bi.return_value = _make_table(data={("Total", 2026, "Apr"): 1.0})

    _parse_table_with_fallback(b"bytes", "I.1")

    mock_pub.assert_not_called()
```

And append below `test_verify_paired_still_raises_when_grid_also_unloadable` (~line 760):

```python
@patch("paired_verifier.extract_structured_facts_async")
@patch("paired_verifier._parse_table_with_fallback")
def test_verify_paired_hands_the_publication_to_the_table_parsers(mock_parse, mock_extract_facts):
    mock_parse.return_value = (_make_table(data={("TOTAL", 2026, "Jul"): 1.0}), "sulni")
    mock_extract_facts.return_value = []

    response = asyncio.run(
        verify_paired(
            narrative_text="[== Halaman 1 ==]\n" + "x" * 250,
            excel_sources=[(b"xlsx-bytes", "TabI.1", "TABEL_INDONESIA.xlsx")],
            llm=Mock(),
            publication="sulni",
        )
    )

    assert mock_parse.call_args.kwargs["publication"] == "sulni"
    assert response.excel_parsers == ["sulni"]


def test_verify_paired_reads_each_grid_through_the_cached_loader():
    import paired_verifier
    from publication_parsers import load_grid

    assert paired_verifier._load_grid is load_grid
```

- [ ] **Step 3: Write the failing endpoint test**

Append to `tests/test_main_paired_endpoint.py`:

```python
@patch("main.verify_paired")
@patch("main.extract_narrative_text")
@patch("main.get_vision_llm")
def test_verify_paired_endpoint_passes_the_publication_to_the_table_parsers(
    mock_get_vision_llm, mock_extract_narrative_text, mock_verify_paired
):
    mock_get_vision_llm.side_effect = RuntimeError("no key configured")
    mock_extract_narrative_text.return_value = "[== Halaman 1 ==]\nULN tumbuh 4,9% (yoy)."
    mock_verify_paired.return_value = _fact_response()

    client.post(
        "/api/verify-paired",
        params={"run_typo_check": "false", "publication": "sulni"},
        data={"sheet_names": "TabI.1"},
        files=[
            ("pdf_file", ("report.pdf", b"%PDF-1.4 fake", "application/pdf")),
            ("excel_file", ("TABEL_INDONESIA.xlsx", b"xlsx-bytes", "application/vnd.ms-excel")),
        ],
    )

    assert mock_verify_paired.call_args.kwargs["publication"] == "sulni"
```

- [ ] **Step 4: Run the new tests to verify they fail**

Run: `.venv/Scripts/python -m pytest tests/test_publication_parsers_registry.py tests/test_paired_verifier.py tests/test_main_paired_endpoint.py -v -k "covers or uncovered or wrapper or publication or cached_loader"`
Expected: FAIL/ERROR — `ImportError: cannot import name 'PublicationParseError' from 'publication_parsers'`.

- [ ] **Step 5: Add the reader wrappers to `_common.py`**

In the import block of `publication_parsers/_common.py`, add (above `from table_model import …`):

```python
from excel_parser_bi import parse_bi_table
```

and change the `table_parser_generic` import to:

```python
from table_parser_generic import (
    _bare_period_token,
    _parse_period,
    load_workbook_grids,
    parse_generic_table,
)
```

Append to the end of the file:

```python
def parse_with_bi_reader(data: bytes, sheet_name: str) -> TableData:
    """SEKI monthly tables: the existing BI reader, accepted only when its result is whole."""
    try:
        table = parse_bi_table(data, sheet_name)
    except ValueError as exc:
        raise PublicationParseError(f"pembaca tabel SEKI: {exc}") from exc
    if not table._data:
        raise PublicationParseError("pembaca tabel SEKI tidak menemukan angka")
    if len(set(table.row_labels)) < len(table.row_labels):
        raise PublicationParseError("label baris berulang tanpa induk; hierarki tidak terbaca")
    return table


def parse_with_generic_reader(data: bytes, sheet_name: str, unit: str = "") -> TableData:
    """Survey workbooks the generic reader already handles (SK, SKDU), checked for shape.

    The generic reader keeps both halves of a bilingual unit cell ('Rp)  / (IDR'); only the
    Indonesian half is kept, and `unit` fills in for a sheet that states none.
    """
    try:
        table = parse_generic_table(data, sheet_name)
    except ValueError as exc:
        raise PublicationParseError(f"pembaca tabel survei: {exc}") from exc
    if table.axis_type != "temporal" or not table._data:
        raise PublicationParseError("tabel tidak terbaca sebagai deret waktu")
    table.unit = _BILINGUAL_SPLIT.split(table.unit)[0].strip(" ()") or unit
    return table
```

- [ ] **Step 6: Create the three SEKI modules**

`publication_parsers/uang_beredar.py`:

```python
"""Uang Beredar (M2): SEKI Tabel I.1 and I.1.A (reklasifikasi), read by the proven SEKI reader.

The historical sheets of TABEL1_1.xls ('Th 1985-1992' ...) are not covered.
"""
from publication_parsers._common import parse_with_bi_reader
from table_model import TableData

SHEET_PATTERNS = (r"I\.1A?",)


def parse(data: bytes, sheet_name: str) -> TableData:
    return parse_with_bi_reader(data, sheet_name)
```

`publication_parsers/uang_primer.py`:

```python
"""Uang Primer (M0): SEKI Tabel I.2, read by the proven SEKI reader."""
from publication_parsers._common import parse_with_bi_reader
from table_model import TableData

SHEET_PATTERNS = (r"I\.2",)


def parse(data: bytes, sheet_name: str) -> TableData:
    return parse_with_bi_reader(data, sheet_name)
```

`publication_parsers/cadangan_devisa.py`:

```python
"""Cadangan Devisa: SEKI Tabel V.9 (sheet '5.9'), read by the proven SEKI reader."""
from publication_parsers._common import parse_with_bi_reader
from table_model import TableData

SHEET_PATTERNS = (r"5\.9",)


def parse(data: bytes, sheet_name: str) -> TableData:
    return parse_with_bi_reader(data, sheet_name)
```

- [ ] **Step 7: Write the registry**

Replace `publication_parsers/__init__.py` with:

```python
"""Per-publication Excel parsers — one module per BI publication.

Each module declares SHEET_PATTERNS (regexes matched against the whole, stripped sheet name,
case-insensitive) and parse(data, sheet_name) -> TableData, raising PublicationParseError when
a covered sheet does not have the layout it expects. paired_verifier tries the parser of the
publication the checker picked before its generic cascade (see _parse_table_with_fallback).
Design: docs/superpowers/specs/2026-10-07-publication-parsers-design.md
"""
from types import ModuleType
from typing import Dict, Optional

from publication_parsers import cadangan_devisa, uang_beredar, uang_primer
from publication_parsers._common import PublicationParseError, load_grid, sheet_matches
from table_model import TableData

PARSERS: Dict[str, ModuleType] = {
    "uang-beredar": uang_beredar,
    "uang-primer-m0": uang_primer,
    "cadangan-devisa": cadangan_devisa,
}

__all__ = ["PARSERS", "PublicationParseError", "covers", "load_grid", "parse_for_publication"]


def covers(publication: str, sheet_name: str) -> bool:
    """True when `publication` has a parser and it claims this sheet."""
    module = PARSERS.get(publication or "")
    return module is not None and any(
        sheet_matches(pattern, sheet_name) for pattern in module.SHEET_PATTERNS
    )


def parse_for_publication(publication: str, data: bytes, sheet_name: str) -> Optional[TableData]:
    """The publication's own reading of the sheet; None when it has no parser for this sheet."""
    if not covers(publication, sheet_name):
        return None
    return PARSERS[publication].parse(data, sheet_name)
```

- [ ] **Step 8: Run the registry tests**

Run: `.venv/Scripts/python -m pytest tests/test_publication_parsers_registry.py -v`
Expected: all PASS.

- [ ] **Step 9: Dispatch in `paired_verifier.py`**

Change the import block (lines 43-48) from:

```python
from table_parser_generic import (
    _MONTH_ABBREVS,
    _load_grid,
    parse_generic_grid,
    parse_generic_table,
)
```

to:

```python
from publication_parsers import load_grid as _load_grid
from publication_parsers import parse_for_publication
from table_parser_generic import (
    _MONTH_ABBREVS,
    parse_generic_grid,
    parse_generic_table,
)
```

(`_load_grid` keeps its name so the existing tests that patch `paired_verifier._load_grid` keep working; it now reads each workbook once — see `publication_parsers._common.load_grid`.)

Replace the signature and first lines of `_parse_table_with_fallback` — from `def _parse_table_with_fallback(` through the line `    bi_error: Optional[Exception] = None` — with:

```python
def _parse_table_with_fallback(
    excel_bytes: bytes,
    sheet_name: str,
    llm: Optional[BaseChatModel] = None,
    publication: str = "",
) -> Tuple[BITableData, str]:
    """Return (table, parser_name) — publication parser, then BI → generic → LLM mapping.

    When the checker picked a publication whose own parser covers this sheet
    (publication_parsers), that parser goes first and is reported under the publication's key
    (e.g. "sulni"). If it rejects the sheet — or breaks — the cascade below runs exactly as it
    would without a publication, so a layout change in a new edition degrades to today's
    behaviour instead of failing the check.

    The BI parser stays the primary generic path so known SEKI files keep their exact current
    behaviour. Its result is accepted only when it actually extracted data; a structurally
    successful but EMPTY parse (or a ValueError) falls through to the generic parser. When
    that also fails and an llm is available, the LLM structure-mapping parser (tier 3) gets
    a shot — it only maps structure; values are still extracted by code. As the last resort,
    any BI result we did get is returned even when empty (claims then come back Inconclusive
    instead of the whole request failing); only when every tier raised is the combined error
    surfaced.
    """
    if publication:
        try:
            table = parse_for_publication(publication, excel_bytes, sheet_name)
        except Exception as exc:  # a parser fault must never fail the check
            logger.warning(
                "Publication parser '%s' failed on sheet '%s' (%s) — using the generic cascade.",
                publication, sheet_name, exc,
            )
            table = None
        if table is not None:
            return table, publication

    bi_table = None
    bi_error: Optional[Exception] = None
```

In `verify_paired`, add the parameter after `chart_pages_unread`:

```python
    chart_pages_unread: Optional[List[int]] = None,
    publication: str = "",
) -> PairedVerificationResponse:
```

add to its docstring, after the `chart_pages_unread:` entry:

```python
        publication:    The publication type the checker picked ("sulni", "npi", ...). Its own
                        parser reads the sheets it covers before the generic cascade (see
                        _parse_table_with_fallback); empty means the generic cascade only.
```

and change the parse call (~line 2641) to:

```python
            table, parser_used = _parse_table_with_fallback(
                excel_bytes, sheet_name, llm=llm, publication=publication
            )
```

- [ ] **Step 10: Pass the publication from `main.py`**

In `_run_paired_pipeline`, in the `verify_paired(` call (~line 791), add after `chart_pages_unread=chart_pages_unread,`:

```python
            publication=publication,
```

- [ ] **Step 11: Run the whole suite**

Run: `.venv/Scripts/python -m pytest -q`
Expected: all PASS (the new dispatch, threading, endpoint and registry tests included; nothing else changes behaviour because `publication` defaults to `""`).

- [ ] **Step 12: Commit**

```bash
git add publication_parsers tests/test_publication_parsers_registry.py tests/test_paired_verifier.py tests/test_main_paired_endpoint.py paired_verifier.py main.py
git commit -m "Try the picked publication's own parser first, falling back to the generic cascade"
```

---

### Task 5: Golden-number harness and the SEKI golden files

**Files:**
- Modify: `.gitignore` (stage only the new rule)
- Create: `tests/fixtures/publications/README.md`, `tests/fixtures/publications/show_table.py`
- Create: `tests/test_publication_golden.py`
- Create: `tests/fixtures/publications/uang-beredar/2026-07/golden.json`, `tests/fixtures/publications/uang-primer-m0/2026-07/golden.json`, `tests/fixtures/publications/cadangan-devisa/2026-08/golden.json`
- Move (untracked files, plain `mv`): `tests/fixtures/publications/uang-primer-m0/2026-09` → `tests/fixtures/publications/uang-primer-m0/2026-07`

**Interfaces:**
- Consumes: `publication_parsers.parse_for_publication` (Task 4).
- Produces: golden.json schema used by Tasks 6–12:

```json
{
  "publication": "<key>",
  "data_period": "YYYY-MM | YYYY-Qn",
  "release": "<what the figures were read from>",
  "sheets": {"<workbook file>": {"<sheet>": {"year": 2026, "period": "Jul"}}},
  "values": [
    {"workbook": "<file>", "sheet": "<sheet>", "label": "<exact TableData row label>",
     "query": "<optional narrative phrase that must land on label>",
     "year": 2026, "period": "Jul", "kind": "value | yoy", "scale": 0.001,
     "decimals": 1, "expected": 454.8, "quote": "<release sentence>"}
  ]
}
```

`sheets` lists every covered sheet of the edition with its latest period; `scale` is optional (default 1); `kind: "yoy"` compares `(v[y]/v[y-1] - 1) * 100`; a value matches when `|got - expected| <= 0.5 * 10**-decimals`.

- [ ] **Step 1: Keep BI files out of git**

Append to the working-tree `.gitignore` (with the Edit tool):

```gitignore

# BI publication samples sit next to their golden.json but are never committed
tests/fixtures/publications/**/*.xls
tests/fixtures/publications/**/*.xlsx
tests/fixtures/publications/**/*.pdf
tests/fixtures/publications/**/*.zip
```

Then check what else is uncommitted in `.gitignore`: `git diff .gitignore`. If the diff shows only your rule, `git add .gitignore`. If it also shows lines you did not write (the user's `presentation/`), stage only your rule:

```bash
SCRATCH="$(mktemp -d)"
git show HEAD:.gitignore > "$SCRATCH/gitignore"
printf '\n# BI publication samples sit next to their golden.json but are never committed\ntests/fixtures/publications/**/*.xls\ntests/fixtures/publications/**/*.xlsx\ntests/fixtures/publications/**/*.pdf\ntests/fixtures/publications/**/*.zip\n' >> "$SCRATCH/gitignore"
git update-index --cacheinfo 100644,"$(git hash-object -w "$SCRATCH/gitignore")",.gitignore
git diff --cached .gitignore   # must show only the five new lines
```

Verify: `git status --short tests/fixtures/publications | head` lists only folders/`golden.json`-to-be, and `git check-ignore -q "tests/fixtures/publications/sulni/2026-07/siaran_pers.pdf" && echo ignored` prints `ignored`.

- [ ] **Step 2: Name the M0 folder by the period its release discusses**

The M0 figures come from the July 2026 Uang Beredar report, so:

```bash
mv tests/fixtures/publications/uang-primer-m0/2026-09 tests/fixtures/publications/uang-primer-m0/2026-07
```

- [ ] **Step 3: Write the README and the inspection helper**

Create `tests/fixtures/publications/README.md`:

````markdown
# Sampel publikasi BI untuk tes angka emas

Satu folder per publikasi (kunci sama dengan `check_history.PUBLICATIONS`), lalu satu folder per
periode data yang dibahas rilisnya (`2026-07`, `2026-Q2`). Isinya:

- file Excel acuan dengan nama aslinya,
- `siaran_pers.pdf` dan/atau `laporan.pdf`,
- `golden.json` — angka yang dicetak BI dan baris tabel tempat angka itu berada.

File BI (`.xls`, `.xlsx`, `.pdf`, `.zip`) **tidak di-commit** (lihat `.gitignore`); hanya
`golden.json`, berkas ini, dan `show_table.py`. Tes `tests/test_publication_golden.py` melewati
(skip) edisi yang Excel-nya tidak ada — di CI semuanya dilewati — jadi jalankan di lokal sebelum
merge perubahan parser:

    .venv/Scripts/python -m pytest tests/test_publication_golden.py -v

Melihat apa yang dibaca parser dari satu sheet (berguna saat menulis `golden.json`):

    .venv/Scripts/python tests/fixtures/publications/show_table.py <publikasi> <file Excel> <sheet> <tahun> <periode>

Membaca teks rilis:

    .venv/Scripts/python -c "import pdfplumber,sys; print('\n'.join((p.extract_text() or '') for p in pdfplumber.open(sys.argv[1]).pages[:3]))" <file.pdf>

## Sumber (bi.go.id)

| Publikasi | Folder | Excel | Rilis |
|---|---|---|---|
| uang-beredar | 2026-07 | `/SEKI/tabel/TABEL1_1.xls`, `/SEKI/tabel/TABEL1_1_1.xls` | laporan Uang Beredar Juli 2026 |
| uang-primer-m0 | 2026-07 | `/SEKI/tabel/TABEL1_2.xls` (unduhan September 2026) | paragraf M0 di laporan Uang Beredar Juli 2026 |
| cadangan-devisa | 2026-08 | `/SEKI/tabel/TABEL5_9.xls` | siaran pers Cadangan Devisa Agustus 2026 |
| npi | 2026-Q2 | `/SEKI/tabel/TABEL5_1.xls` | laporan NPI Triwulan II 2026 |
| pii | 2026-Q2 | `/SEKI/tabel/TABEL5_39.xls` | laporan PII Triwulan II 2026 |
| sulni | 2026-07 | `/en/statistik/ekonomi-keuangan/sulni/Documents/SULNI-September-2026.zip` | siaran pers ULN Juli 2026 (No.28/188/DKom) |
| sk | 2026-08 | `/id/publikasi/laporan/Documents/Data-Series-SK-Agustus-2026.zip` | laporan Survei Konsumen Agustus 2026 |
| spe | 2026-07 | `/id/publikasi/laporan/Documents/Data-Series-SPE-Juli-2026.zip` | laporan Survei Penjualan Eceran Juli 2026 |
| skdu | 2026-Q2 | `/id/publikasi/laporan/Documents/Data-Series-SKDU-Triwulan-II-2026.zip` | laporan SKDU Triwulan II 2026 |
| sbank | 2026-Q2 | `/id/publikasi/laporan/Documents/Data-Series-Survei-Perbankan-Tw-II-2026.zip` | laporan Survei Perbankan Triwulan II 2026 |
| shpr | 2026-Q2 | `/id/publikasi/laporan/Documents/SHPR_Tw_II_2026.zip` | laporan SHPR Triwulan II 2026 |
| pmi | 2026-Q1 | `/id/publikasi/laporan/Documents/PMI-Triwulan-I-2026.zip` | laporan Prompt Manufacturing Index Triwulan I 2026 |

Zip generik tanpa periode (`/id/publikasi/laporan/Documents/SK.zip` dst.) berisi edisi lama;
pakai zip yang bertanggal. bi.go.id menolak curl/headless untuk halaman daftar; unduh lewat peramban.
````

Create `tests/fixtures/publications/show_table.py`:

```python
"""Print what a publication parser reads from one sheet — for writing golden.json by hand.

    .venv/Scripts/python tests/fixtures/publications/show_table.py sulni \
        "tests/fixtures/publications/sulni/2026-07/TABEL_INDONESIA Sep26_value.xlsx" TabI.1 2026 Jul
"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[3]))

from publication_parsers import parse_for_publication  # noqa: E402

_ORDER = ["Jan", "Feb", "Mar", "Apr", "May", "Jun", "Jul", "Aug", "Sep", "Oct", "Nov", "Dec",
          "Q1", "Q2", "Q3", "Q4"]

publication, path, sheet, year, period = sys.argv[1:6]
table = parse_for_publication(publication, Path(path).read_bytes(), sheet)
if table is None:
    sys.exit(f"sheet {sheet!r} tidak dicakup parser {publication}")
latest = max(((y, p) for _, y, p in table._data), key=lambda yp: (yp[0], _ORDER.index(yp[1])))
print(f"{table.title} | satuan: {table.unit} | {len(table.row_labels)} baris | terakhir: {latest}")
year = int(year)
for label in table.row_labels:
    now, before = table.lookup(label, year, period), table.lookup(label, year - 1, period)
    yoy = f"{(now / before - 1) * 100:8.2f}" if now is not None and before else "       -"
    print(f"{now!s:>20}  yoy {yoy}  {label}")
```

- [ ] **Step 4: Write the harness (failing until the golden files exist)**

Create `tests/test_publication_golden.py`:

```python
"""Golden numbers: each publication parser reproduces the figures BI printed in its own release.

Fixtures live in tests/fixtures/publications/<publication>/<data period>/. The BI files (Excel,
PDF) are gitignored — only golden.json is committed — so an edition whose Excel is missing is
skipped. Run these locally before merging any parser change (see that folder's README.md).
"""
import json
from functools import lru_cache
from pathlib import Path

import pytest

from publication_parsers import parse_for_publication
from table_model import TableData

FIXTURES = Path(__file__).parent / "fixtures" / "publications"
_ORDER = ["Jan", "Feb", "Mar", "Apr", "May", "Jun", "Jul", "Aug", "Sep", "Oct", "Nov", "Dec",
          "Q1", "Q2", "Q3", "Q4"]


def _goldens():
    return [
        (path.parent, json.loads(path.read_text(encoding="utf-8")))
        for path in sorted(FIXTURES.glob("*/*/golden.json"))
    ]


def _sheet_cases():
    for folder, golden in _goldens():
        for workbook, sheets in golden["sheets"].items():
            for sheet, latest in sheets.items():
                yield pytest.param(
                    folder, golden["publication"], workbook, sheet, latest,
                    id=f"{golden['publication']}/{folder.name}/{sheet}",
                )


def _value_cases():
    for folder, golden in _goldens():
        for i, value in enumerate(golden["values"]):
            yield pytest.param(
                folder, golden["publication"], value,
                id=f"{golden['publication']}/{folder.name}/{i}-{value['sheet']}-{value['label']}",
            )


@lru_cache(maxsize=None)
def _parsed(folder: Path, publication: str, workbook: str, sheet: str) -> TableData:
    path = folder / workbook
    if not path.exists():
        pytest.skip(f"{path.name} tidak ada di lokal (file BI di-gitignore; lihat README folder ini)")
    table = parse_for_publication(publication, path.read_bytes(), sheet)
    assert table is not None, f"sheet '{sheet}' tidak dicakup parser {publication}"
    return table


def test_an_edition_without_its_excel_is_skipped_not_failed(tmp_path):
    with pytest.raises(pytest.skip.Exception):
        _parsed(tmp_path, "uang-beredar", "TABEL1_1.xls", "I.1")


@pytest.mark.parametrize("folder, publication, workbook, sheet, latest", list(_sheet_cases()))
def test_each_sheet_is_read_by_its_publication_parser(folder, publication, workbook, sheet, latest):
    table = _parsed(folder, publication, workbook, sheet)

    assert table.row_labels and table._data
    assert table.unit.strip()
    last = max(((y, p) for _, y, p in table._data), key=lambda yp: (yp[0], _ORDER.index(yp[1])))
    assert last == (latest["year"], latest["period"])


@pytest.mark.parametrize("folder, publication, value", list(_value_cases()))
def test_each_golden_figure_matches_the_release(folder, publication, value):
    table = _parsed(folder, publication, value["workbook"], value["sheet"])
    year, period = value["year"], value["period"]

    current = table.lookup(value["label"], year, period)
    assert current is not None, (
        f"tidak ada sel {value['label']!r} {period} {year}; label: {table.row_labels}"
    )
    if value["kind"] == "yoy":
        previous = table.lookup(value["label"], year - 1, period)
        assert previous, f"tidak ada sel {value['label']!r} {period} {year - 1}"
        got = (current / previous - 1) * 100
    else:
        got = current * value.get("scale", 1)
    assert abs(got - value["expected"]) <= 0.5 * 10 ** -value["decimals"] + 1e-9, (
        f"tabel {got:.4f} vs rilis {value['expected']} — {value['quote']}"
    )
    if value.get("query"):
        matched, _ = table.lookup_fuzzy(value["query"], year, period)
        assert matched == value["label"], f"{value['query']!r} mendarat di {matched!r}"
```

Run: `.venv/Scripts/python -m pytest tests/test_publication_golden.py -v`
Expected: only `test_an_edition_without_its_excel_is_skipped_not_failed` runs and PASSES; the two parametrized tests report "got empty parameter set" (skipped) because no golden.json exists yet.

- [ ] **Step 5: Write the three SEKI golden files**

These figures were checked against the fixtures while writing this plan. `tests/fixtures/publications/uang-beredar/2026-07/golden.json`:

```json
{
  "publication": "uang-beredar",
  "data_period": "2026-07",
  "release": "Laporan Uang Beredar (M2) dan Faktor yang Memengaruhi, Juli 2026 (laporan.pdf)",
  "sheets": {
    "TABEL1_1.xls": {"I.1": {"year": 2026, "period": "Jul"}},
    "TABEL1_1_1.xls": {"I.1A": {"year": 2026, "period": "Jul"}}
  },
  "values": [
    {"workbook": "TABEL1_1_1.xls", "sheet": "I.1A", "label": "Uang Beredar Luas(M2)", "query": "M2",
     "year": 2026, "period": "Jul", "kind": "value", "scale": 0.001, "decimals": 1, "expected": 10371.1,
     "quote": "Posisi M2 pada Juli 2026 tercatat sebesar Rp10.371,1 triliun"},
    {"workbook": "TABEL1_1_1.xls", "sheet": "I.1A", "label": "Uang Beredar Luas(M2)",
     "year": 2026, "period": "Jul", "kind": "yoy", "decimals": 1, "expected": 8.3,
     "quote": "atau tumbuh sebesar 8,3% (yoy)"},
    {"workbook": "TABEL1_1_1.xls", "sheet": "I.1A", "label": "Uang Beredar Luas(M2)",
     "year": 2026, "period": "Jun", "kind": "yoy", "decimals": 1, "expected": 8.7,
     "quote": "melanjutkan pertumbuhan pada Juni 2026 sebesar 8,7% (yoy)"},
    {"workbook": "TABEL1_1_1.xls", "sheet": "I.1A", "label": "Uang Beredar Luas(M2)",
     "year": 2026, "period": "Jun", "kind": "value", "scale": 0.001, "decimals": 1, "expected": 10432.0,
     "quote": "Tabel 1: Uang Beredar Luas (M2) Jun 10,432.0"},
    {"workbook": "TABEL1_1_1.xls", "sheet": "I.1A", "label": "Uang Beredar Sempit (M1)", "query": "M1",
     "year": 2026, "period": "Jul", "kind": "value", "scale": 0.001, "decimals": 1, "expected": 5908.7,
     "quote": "tercatat Rp5.908,7 triliun"},
    {"workbook": "TABEL1_1_1.xls", "sheet": "I.1A", "label": "Uang Beredar Sempit (M1)",
     "year": 2026, "period": "Jul", "kind": "yoy", "decimals": 1, "expected": 10.0,
     "quote": "pertumbuhan uang beredar sempit (M1) sebesar 10,0% (yoy)"},
    {"workbook": "TABEL1_1_1.xls", "sheet": "I.1A", "label": "Uang Kuasi", "query": "uang kuasi",
     "year": 2026, "period": "Jul", "kind": "value", "scale": 0.001, "decimals": 1, "expected": 4375.9,
     "quote": "Tabel 1: Uang Kuasi Jul* 4,375.9"},
    {"workbook": "TABEL1_1_1.xls", "sheet": "I.1A", "label": "Uang Kuasi",
     "year": 2026, "period": "Jul", "kind": "yoy", "decimals": 1, "expected": 5.6,
     "quote": "dan uang kuasi sebesar 5,6% (yoy)"},
    {"workbook": "TABEL1_1_1.xls", "sheet": "I.1A", "label": "Giro Rupiah", "query": "giro rupiah",
     "year": 2026, "period": "Jul", "kind": "yoy", "decimals": 1, "expected": 10.9,
     "quote": "pertumbuhan giro rupiah sebesar 10,9% (yoy)"},
    {"workbook": "TABEL1_1_1.xls", "sheet": "I.1A", "label": "Uang Kartal di Luar Bank Umum dan BPR",
     "query": "uang kartal", "year": 2026, "period": "Jul", "kind": "value", "scale": 0.001,
     "decimals": 1, "expected": 1206.0,
     "quote": "Tabel 1: Uang Kartal di Luar Bank Umum dan BPR Jul* 1,206.0"},
    {"workbook": "TABEL1_1.xls", "sheet": "I.1", "label": "Uang Beredar Luas(M2)",
     "year": 2026, "period": "Jul", "kind": "value", "scale": 0.001, "decimals": 1, "expected": 10371.1,
     "quote": "Posisi M2 pada Juli 2026 tercatat sebesar Rp10.371,1 triliun"},
    {"workbook": "TABEL1_1.xls", "sheet": "I.1", "label": "Tagihan Bersih kepada Pemerintah Pusat",
     "query": "tagihan bersih kepada pemerintah pusat",
     "year": 2026, "period": "Jul", "kind": "yoy", "decimals": 1, "expected": 4.6,
     "quote": "tagihan bersih kepada pemerintah pusat tumbuh sebesar 4,6% (yoy)"},
    {"workbook": "TABEL1_1.xls", "sheet": "I.1", "label": "Tagihan Bersih kepada Pemerintah Pusat",
     "year": 2026, "period": "Jun", "kind": "yoy", "decimals": 1, "expected": 10.1,
     "quote": "melanjutkan pertumbuhan sebesar 10,1% (yoy) pada Juni 2026"}
  ]
}
```

`tests/fixtures/publications/uang-primer-m0/2026-07/golden.json`:

```json
{
  "publication": "uang-primer-m0",
  "data_period": "2026-07",
  "release": "Laporan Uang Beredar Juli 2026, paragraf Uang Primer (fixtures uang-beredar/2026-07/laporan.pdf); TABEL1_2.xls diunduh September 2026",
  "sheets": {
    "TABEL1_2.xls": {"I.2": {"year": 2026, "period": "Sep"}}
  },
  "values": [
    {"workbook": "TABEL1_2.xls", "sheet": "I.2", "label": "Uang Primer Adjusted 1)",
     "query": "uang primer adjusted", "year": 2026, "period": "Jul", "kind": "value",
     "scale": 0.001, "decimals": 1, "expected": 2254.5,
     "quote": "sehingga tercatat sebesar Rp2.254,5 triliun"},
    {"workbook": "TABEL1_2.xls", "sheet": "I.2", "label": "Uang Primer Adjusted 1)",
     "year": 2026, "period": "Jul", "kind": "yoy", "decimals": 1, "expected": 17.1,
     "quote": "Uang Primer (M0) adjusted pada Juli 2026 tumbuh 17,1% (yoy)"},
    {"workbook": "TABEL1_2.xls", "sheet": "I.2", "label": "Uang Primer Adjusted 1)",
     "year": 2026, "period": "Jun", "kind": "yoy", "decimals": 1, "expected": 13.8,
     "quote": "pertumbuhan bulan Juni 2026 sebesar 13,8% (yoy)"},
    {"workbook": "TABEL1_2.xls", "sheet": "I.2", "label": "Giro Bank Umum di BI Adjusted 2)",
     "query": "giro bank umum di BI adjusted", "year": 2026, "period": "Jul", "kind": "yoy",
     "decimals": 1, "expected": 17.1,
     "quote": "pertumbuhan giro bank umum di Bank Indonesia adjusted sebesar 17,1% (yoy)"},
    {"workbook": "TABEL1_2.xls", "sheet": "I.2", "label": "Uang Kartal Yang Diedarkan",
     "year": 2026, "period": "Jul", "kind": "yoy", "decimals": 1, "expected": 15.1,
     "quote": "uang kartal yang diedarkan sebesar 15,1% (yoy)"}
  ]
}
```

`tests/fixtures/publications/cadangan-devisa/2026-08/golden.json`:

```json
{
  "publication": "cadangan-devisa",
  "data_period": "2026-08",
  "release": "Siaran Pers No.28/181/DKom, Cadangan Devisa Agustus 2026 (siaran_pers.pdf)",
  "sheets": {
    "TABEL5_9.xls": {"5.9": {"year": 2026, "period": "Aug"}}
  },
  "values": [
    {"workbook": "TABEL5_9.xls", "sheet": "5.9", "label": "Total", "query": "posisi cadangan devisa",
     "year": 2026, "period": "Aug", "kind": "value", "scale": 0.001, "decimals": 1, "expected": 146.5,
     "quote": "Posisi cadangan devisa Indonesia pada akhir Agustus 2026 tetap terjaga sebesar 146,5 miliar dolar AS"},
    {"workbook": "TABEL5_9.xls", "sheet": "5.9", "label": "Total",
     "year": 2026, "period": "Jul", "kind": "value", "scale": 0.001, "decimals": 1, "expected": 145.3,
     "quote": "posisi akhir Juli 2026 sebesar 145,3 miliar dolar AS"}
  ]
}
```

- [ ] **Step 6: Run the golden tests**

Run: `.venv/Scripts/python -m pytest tests/test_publication_golden.py -v`
Expected: all PASS (4 sheet cases, 20 value cases, the skip test). If only a `query` assertion fails, the label matcher missed — not the parser. Drop that figure's `query` field and write the phrase down for the "Tindak lanjut" list of `docs/publication-parsers-e2e.md` (Task 13); matcher changes are out of scope here.

- [ ] **Step 7: Commit**

```bash
git add tests/test_publication_golden.py tests/fixtures/publications/README.md tests/fixtures/publications/show_table.py tests/fixtures/publications/uang-beredar/2026-07/golden.json tests/fixtures/publications/uang-primer-m0/2026-07/golden.json tests/fixtures/publications/cadangan-devisa/2026-08/golden.json
git commit -m "Prove the SEKI publication parsers against the figures in their releases"
```

(`.gitignore` is already staged from Step 1; it goes into this commit. Run `git show --stat HEAD` and confirm no `.xls/.xlsx/.pdf` was committed.)

---

### Task 6: SULNI

**Files:**
- Create: `publication_parsers/sulni.py`
- Modify: `publication_parsers/__init__.py` (import + `PARSERS["sulni"]`)
- Create: `tests/fixtures/publications/sulni/2026-07/golden.json`
- Test: `tests/test_publication_parsers_registry.py`

**Interfaces:**
- Consumes: `_common.SheetSpec`, `_common.parse_with_specs`.
- Produces: `PARSERS["sulni"]`.

- [ ] **Step 1: Write the failing coverage test**

Append to `tests/test_publication_parsers_registry.py`:

```python
@pytest.mark.parametrize("sheet, expected", [
    ("TabI.1", True), ("TabI.7", True), ("Tbl II.1", True), ("Tbl II.6", True),
    ("Tbl II.7", False), ("Tbl II.8", False),
    ("Tbl III.1", True), ("Tbl III.10", True), ("Tbl III.11", False),
])
def test_sulni_covers_only_its_time_series_sheets(sheet, expected):
    assert covers("sulni", sheet) is expected
```

Run: `.venv/Scripts/python -m pytest tests/test_publication_parsers_registry.py -k sulni -v`
Expected: FAIL (`covers("sulni", "TabI.1")` is False).

- [ ] **Step 2: Write the module and register it**

`publication_parsers/sulni.py`:

```python
"""SULNI: the three workbooks of the Statistik Utang Luar Negeri Indonesia zip.

TABEL_INDONESIA (TabI.1–I.7), TABEL_PEMERINTAH (Tbl II.1–II.6) and TABEL_SWASTA (Tbl III.1–III.10)
share one layout: labels across columns 1–5 carrying their own numbering ('2.1.1. Bank / Bank',
'a.', '-'), years in row 5 written only at each year's first column, months in row 6 with
'*' / '**' marks, and annual columns for the early years (skipped). TabI.5/I.6 put the maturity
group in columns 1–4 and the borrower in column 5. TabI.7 (indicator ratios) is quarterly with
one combined header row ('Q2-2026**') and nests by the column its label starts in.
Tbl II.7 (payment schedule) and Tbl II.8 (SBN series) are not time series: not covered.
"""
from publication_parsers._common import SheetSpec, parse_with_specs
from table_model import TableData

_LABELS = (1, 2, 3, 4, 5)

SPECS = (
    (r"TabI\.7", SheetSpec(label_cols=_LABELS, header="combined", hierarchy="first_col")),
    (r"TabI\.[1-6]|Tbl II\.[1-6]|Tbl III\.(?:[1-9]|10)",
     SheetSpec(label_cols=_LABELS, hierarchy="numbering")),
)
SHEET_PATTERNS = tuple(pattern for pattern, _ in SPECS)


def parse(data: bytes, sheet_name: str) -> TableData:
    return parse_with_specs(data, sheet_name, SPECS)
```

In `publication_parsers/__init__.py` change the import to `from publication_parsers import cadangan_devisa, sulni, uang_beredar, uang_primer` and add `"sulni": sulni,` to `PARSERS`.

Run: `.venv/Scripts/python -m pytest tests/test_publication_parsers_registry.py -v`
Expected: PASS.

- [ ] **Step 3: Look at what the parser reads**

Run:

```bash
.venv/Scripts/python tests/fixtures/publications/show_table.py sulni "tests/fixtures/publications/sulni/2026-07/TABEL_INDONESIA Sep26_value.xlsx" TabI.1 2026 Jul
```

Expected (values rounded here):

```
Posisi Utang Luar Negeri Menurut Kelompok Peminjam | satuan: Juta USD | 9 baris | terakhir: (2026, 'Jul')
  260282.9 ... Pemerintah dan Bank Sentral
  218391.1 ... Pemerintah
   41891.8 ... Bank Sentral
  194476.9 ... Swasta
   38670.4 ... Lembaga Keuangan
   32563.6 ... Bank
    6106.8 ... LKBB
  155806.5 ... Bukan Lembaga Keuangan
  454759.8 ... TOTAL
```

(The first SULNI workbook load takes ~9 s; the rest of the sheets of that workbook come from memory.)

- [ ] **Step 4: Write the golden file**

`tests/fixtures/publications/sulni/2026-07/golden.json` (latest periods and figures were checked against the fixtures while writing this plan):

```json
{
  "publication": "sulni",
  "data_period": "2026-07",
  "release": "Siaran Pers No.28/188/DKom, 15 September 2026, ULN Juli 2026 (siaran_pers.pdf); zip SULNI edisi September 2026",
  "sheets": {
    "TABEL_INDONESIA Sep26_value.xlsx": {
      "TabI.1": {"year": 2026, "period": "Jul"},
      "TabI.2": {"year": 2026, "period": "Jul"},
      "TabI.3": {"year": 2026, "period": "Jul"},
      "TabI.4": {"year": 2026, "period": "Jul"},
      "TabI.5": {"year": 2026, "period": "Jul"},
      "TabI.6": {"year": 2026, "period": "Jul"},
      "TabI.7": {"year": 2026, "period": "Q2"}
    },
    "TABEL_PEMERINTAH Sep26_value.xlsx": {
      "Tbl II.1": {"year": 2026, "period": "Jul"},
      "Tbl II.2": {"year": 2026, "period": "Jul"},
      "Tbl II.3": {"year": 2026, "period": "Jul"},
      "Tbl II.4": {"year": 2026, "period": "Jul"},
      "Tbl II.5": {"year": 2026, "period": "Jul"},
      "Tbl II.6": {"year": 2026, "period": "Jul"}
    },
    "TABEL_SWASTA Sep26_value.xlsx": {
      "Tbl III.1": {"year": 2026, "period": "Jul"},
      "Tbl III.2": {"year": 2026, "period": "Jul"},
      "Tbl III.3": {"year": 2026, "period": "Jul"},
      "Tbl III.4": {"year": 2026, "period": "Jul"},
      "Tbl III.5": {"year": 2026, "period": "Jul"},
      "Tbl III.6": {"year": 2026, "period": "Jul"},
      "Tbl III.7": {"year": 2026, "period": "Jul"},
      "Tbl III.8": {"year": 2026, "period": "Jul"},
      "Tbl III.9": {"year": 2026, "period": "Jul"},
      "Tbl III.10": {"year": 2026, "period": "Jul"}
    }
  },
  "values": [
    {"workbook": "TABEL_INDONESIA Sep26_value.xlsx", "sheet": "TabI.1", "label": "TOTAL",
     "year": 2026, "period": "Jul", "kind": "value", "scale": 0.001, "decimals": 1, "expected": 454.8,
     "quote": "Posisi ULN Indonesia pada Juli 2026 tercatat sebesar 454,8 miliar dolar AS"},
    {"workbook": "TABEL_INDONESIA Sep26_value.xlsx", "sheet": "TabI.1", "label": "TOTAL",
     "year": 2026, "period": "Jun", "kind": "value", "scale": 0.001, "decimals": 1, "expected": 454.5,
     "quote": "posisi pada Juni 2026 sebesar 454,5 miliar dolar AS"},
    {"workbook": "TABEL_INDONESIA Sep26_value.xlsx", "sheet": "TabI.1", "label": "TOTAL",
     "year": 2026, "period": "Jul", "kind": "yoy", "decimals": 1, "expected": 4.9,
     "quote": "ULN Indonesia mencatat pertumbuhan sebesar 4,9 % (yoy)"},
    {"workbook": "TABEL_INDONESIA Sep26_value.xlsx", "sheet": "TabI.1", "label": "Pemerintah",
     "query": "ULN pemerintah", "year": 2026, "period": "Jul", "kind": "value", "scale": 0.001,
     "decimals": 1, "expected": 218.4,
     "quote": "Posisi ULN pemerintah pada Juli 2026 tercatat 218,4 miliar dolar AS"},
    {"workbook": "TABEL_INDONESIA Sep26_value.xlsx", "sheet": "TabI.1", "label": "Pemerintah",
     "year": 2026, "period": "Jul", "kind": "yoy", "decimals": 1, "expected": 3.2,
     "quote": "atau tumbuh sebesar 3,2% (yoy)"},
    {"workbook": "TABEL_INDONESIA Sep26_value.xlsx", "sheet": "TabI.1", "label": "Swasta",
     "query": "ULN swasta", "year": 2026, "period": "Jul", "kind": "value", "scale": 0.001,
     "decimals": 1, "expected": 194.5,
     "quote": "Posisi ULN swasta pada Juli 2026 tercatat sebesar 194,5 miliar dolar AS"},
    {"workbook": "TABEL_INDONESIA Sep26_value.xlsx", "sheet": "TabI.1", "label": "Swasta",
     "year": 2026, "period": "Jul", "kind": "yoy", "decimals": 1, "expected": -1.2,
     "quote": "atau mengalami kontraksi sebesar 1,2% (yoy)"},
    {"workbook": "TABEL_INDONESIA Sep26_value.xlsx", "sheet": "TabI.1", "label": "Bukan Lembaga Keuangan",
     "query": "perusahaan bukan lembaga keuangan", "year": 2026, "period": "Jul", "kind": "yoy",
     "decimals": 1, "expected": -1.4,
     "quote": "bukan lembaga keuangan (nonfinancial corporations) ... kontraksi sebesar 1,4% (yoy)"},
    {"workbook": "TABEL_INDONESIA Sep26_value.xlsx", "sheet": "TabI.1", "label": "Lembaga Keuangan",
     "query": "lembaga keuangan", "year": 2026, "period": "Jul", "kind": "yoy",
     "decimals": 1, "expected": -0.3,
     "quote": "lembaga keuangan (financial corporations) ... 0,3% (yoy)"}
  ]
}
```

- [ ] **Step 5: Run the golden tests**

Run: `.venv/Scripts/python -m pytest tests/test_publication_golden.py -v -k sulni`
Expected: all PASS (23 sheet cases, 9 value cases). The run takes ~30 s (three workbook loads).

- [ ] **Step 6: Commit**

```bash
git add publication_parsers/sulni.py publication_parsers/__init__.py tests/test_publication_parsers_registry.py tests/fixtures/publications/sulni/2026-07/golden.json
git commit -m "Read every SULNI time-series sheet with its own parser"
```

---

### Tasks 7–12: the remaining publications

Each of these tasks has the same shape: write the coverage test, write the module (code given), register it, look at what it reads with `show_table.py`, write `golden.json` from the release, run the golden tests, commit. The module code and sheet specs below were tried on the fixtures while writing this plan; the golden figures are read from the release during the task.

**How to write a golden file (used by Tasks 7–12):**

1. Print the release text: `.venv/Scripts/python -c "import pdfplumber,sys; print('\n'.join((p.extract_text() or '') for p in pdfplumber.open(sys.argv[1]).pages[:3]))" tests/fixtures/publications/<pub>/<period>/laporan.pdf`
2. Pick **5–15 figures that the sheets hold**: the headline level and its growth, the previous period's figure when the release quotes it, and at least two components. Skip figures that are not in any covered sheet (ratios to GDP from another source, sums of several rows) — they are out of scope.
3. For each figure find its row with `show_table.py <pub> <file> <sheet> <year> <period>`; copy the label **exactly** as printed. Use `kind: "yoy"` when the release states a yoy growth that the sheet does not hold as its own row; use `kind: "value"` when the sheet holds the figure itself (growth tables such as SPE Tabel 2 or SHPR Tabel 3 hold growth as values).
4. `sheets` lists **every** sheet the module covers in that workbook, with the latest period `show_table.py` prints on its first line.
5. Add `query` only for figures whose release wording names the row plainly ("ULN swasta", "transaksi berjalan").
6. If a figure does not match: a wrong row/period in your golden entry → fix the entry; the parser reading a wrong cell → write a synthetic test reproducing the layout in `tests/test_publication_parsers_common.py` first, then fix `_common.py`, then rerun **all** golden tests (`.venv/Scripts/python -m pytest tests/test_publication_golden.py -v`) since every engine publication shares the code.

---

### Task 7: NPI and PII

**Files:**
- Create: `publication_parsers/npi.py`, `publication_parsers/pii.py`
- Modify: `publication_parsers/__init__.py`
- Create: `tests/fixtures/publications/npi/2026-Q2/golden.json`, `tests/fixtures/publications/pii/2026-Q2/golden.json`
- Test: `tests/test_publication_parsers_registry.py`

**Interfaces:**
- Consumes: `_common.SheetSpec(hierarchy="indent")`, `_common.parse_with_specs` (reads indents from the label column with xlrd).
- Produces: `PARSERS["npi"]`, `PARSERS["pii"]`.

- [ ] **Step 1: Write the failing coverage test**

```python
@pytest.mark.parametrize("publication, sheet, expected", [
    ("npi", "5.1", True), ("npi", "Th 2004-2010", False), ("npi", "I.1", False),
    ("pii", "5.39", True), ("pii", "2001-2012", False), ("pii", "5.3", False),
])
def test_npi_and_pii_cover_their_seki_sheets(publication, sheet, expected):
    assert covers(publication, sheet) is expected
```

Run: `.venv/Scripts/python -m pytest tests/test_publication_parsers_registry.py -k npi_and_pii -v` → FAIL.

- [ ] **Step 2: Write the modules and register them**

`publication_parsers/npi.py`:

```python
"""NPI: SEKI Tabel V.1 (Neraca Pembayaran Indonesia, quarterly, .xls).

Labels in column 2 nest by the label cell's indent ('I. Transaksi Berjalan' > 'A. Barang' >
'- Ekspor, fob'); years in row 4 over 'Q1'..'Q4' in row 5; an English mirror on the right.
The SEKI monthly reader cannot read it (no month row), hence the engine.
"""
from publication_parsers._common import SheetSpec, parse_with_specs
from table_model import TableData

SPECS = ((r"5\.1", SheetSpec(label_cols=(2,), hierarchy="indent")),)
SHEET_PATTERNS = tuple(pattern for pattern, _ in SPECS)


def parse(data: bytes, sheet_name: str) -> TableData:
    return parse_with_specs(data, sheet_name, SPECS)
```

`publication_parsers/pii.py`:

```python
"""PII: SEKI Tabel V.39 (Posisi Investasi Internasional, quarterly, .xls).

Same layout as NPI: labels in column 2 nested by indent ('Aset' > 'Investasi Langsung' >
'Modal Ekuitas'), years over 'Q1'..'Q4', annual columns for the early years (skipped).
"""
from publication_parsers._common import SheetSpec, parse_with_specs
from table_model import TableData

SPECS = ((r"5\.39", SheetSpec(label_cols=(2,), hierarchy="indent")),)
SHEET_PATTERNS = tuple(pattern for pattern, _ in SPECS)


def parse(data: bytes, sheet_name: str) -> TableData:
    return parse_with_specs(data, sheet_name, SPECS)
```

Register both in `publication_parsers/__init__.py` (`"npi": npi, "pii": pii`; keep the module import list alphabetical). Run the registry tests → PASS.

- [ ] **Step 3: Look at what they read**

```bash
.venv/Scripts/python tests/fixtures/publications/show_table.py npi tests/fixtures/publications/npi/2026-Q2/TABEL5_1.xls 5.1 2026 Q2
.venv/Scripts/python tests/fixtures/publications/show_table.py pii tests/fixtures/publications/pii/2026-Q2/TABEL5_39.xls 5.39 2026 Q2
```

Expected: NPI `Juta USD`, 57 rows, latest `(2026, 'Q2')`, labels such as `Transaksi Berjalan`, `Barang`, `Nonmigas > Ekspor`, `Jasa - jasa > Impor`, `Pendapatan Primer`, `Transaksi Finansial`, `Investasi Portofolio > Kewajiban`, `Investasi Portofolio > Kewajiban > Sektor publik`, `Neraca Keseluruhan`, `Posisi Cadangan Devisa`, `Transaksi Berjalan (% PDB)`; Q2 2026 `Transaksi Berjalan` = −12487.42. PII `Juta USD`, 39 rows, latest `(2026, 'Q2')`, `Aset` 571539.3, `Kewajiban` 768970.3, `Posisi Investasi Internasional, bersih` −197431.0, `Aset > Investasi Langsung`, `Kewajiban > Investasi Portofolio`.

- [ ] **Step 4: Write both golden files** following "How to write a golden file". NPI figures to look for in `npi/2026-Q2/laporan.pdf`: current-account balance (USD bn and % of GDP — the `Transaksi Berjalan (% PDB)` row holds the latter), financial-account balance, overall balance (`Neraca Keseluruhan`), reserve position, goods/services/primary-income balances. PII figures in `pii/2026-Q2/laporan.pdf`: net IIP (`Posisi Investasi Internasional, bersih`), foreign financial assets (`Aset`), liabilities (`Kewajiban`) and their main components. Release figures are in USD bn → `"scale": 0.001` on the `Juta USD` rows.

- [ ] **Step 5: Run** `.venv/Scripts/python -m pytest tests/test_publication_golden.py -v -k "npi or pii"` → all PASS.

- [ ] **Step 6: Commit**

```bash
git add publication_parsers/npi.py publication_parsers/pii.py publication_parsers/__init__.py tests/test_publication_parsers_registry.py tests/fixtures/publications/npi/2026-Q2/golden.json tests/fixtures/publications/pii/2026-Q2/golden.json
git commit -m "Read the quarterly NPI and PII tables through their indent hierarchy"
```

---

### Task 8: SK and SKDU

**Files:**
- Create: `publication_parsers/sk.py`, `publication_parsers/skdu.py`
- Modify: `publication_parsers/__init__.py`
- Create: `tests/fixtures/publications/sk/2026-08/golden.json`, `tests/fixtures/publications/skdu/2026-Q2/golden.json`
- Test: `tests/test_publication_parsers_registry.py`

**Interfaces:**
- Consumes: `_common.parse_with_generic_reader(data, sheet_name, unit="")`, `_common.sheet_matches`.
- Produces: `PARSERS["sk"]`, `PARSERS["skdu"]`.

- [ ] **Step 1: Write the failing tests**

Append to `tests/test_publication_parsers_registry.py`:

```python
@pytest.mark.parametrize("publication, sheet, expected", [
    ("sk", "Tabel 1", True), ("sk", "Tabel 9", True), ("sk", "Tabel 10", False),
    ("skdu", "T1 Kegiatan Usaha", True), ("skdu", "T7b Investasi Semesteran", True),
    ("skdu", "T10 Margin Usaha", True), ("skdu", "Tabel 1", False),
])
def test_sk_and_skdu_cover_their_survey_sheets(publication, sheet, expected):
    assert covers(publication, sheet) is expected


@patch("publication_parsers._common.parse_generic_table")
def test_survey_wrapper_keeps_the_indonesian_half_of_the_unit(mock_generic):
    mock_generic.return_value = TableData(
        title="t", unit="Rp)  / (IDR", row_labels=["Mandor"], _data={("Mandor", 2026, "Q1"): 1.0},
    )

    assert parse_for_publication("skdu", b"bytes", "T9 Upah Rata-rata").unit == "Rp"


@patch("publication_parsers._common.parse_generic_table")
def test_survey_wrapper_fills_in_the_unit_a_consumer_survey_sheet_leaves_out(mock_generic):
    mock_generic.return_value = TableData(
        title="t", unit="", row_labels=["IKK"], _data={("IKK", 2026, "Aug"): 120.0},
    )

    assert parse_for_publication("sk", b"bytes", "Tabel 1").unit == "Indeks"
    assert parse_for_publication("sk", b"bytes", "Tabel 5").unit == "%"


@patch("publication_parsers._common.parse_generic_table")
def test_survey_wrapper_rejects_a_table_that_is_not_a_time_series(mock_generic):
    mock_generic.return_value = TableData(
        title="t", unit="", row_labels=["IKK"], axis_type="categorical",
        _data={("IKK", "Nilai"): 1.0},
    )

    with pytest.raises(PublicationParseError, match="deret waktu"):
        parse_for_publication("sk", b"bytes", "Tabel 1")
```

Run: `.venv/Scripts/python -m pytest tests/test_publication_parsers_registry.py -k "sk_and_skdu or survey_wrapper" -v` → FAIL.

- [ ] **Step 2: Write the modules and register them**

`publication_parsers/sk.py`:

```python
"""Survei Konsumen: 'Tabel 1'..'Tabel 9' of the monthly data-series workbook.

The generic reader already reads every sheet of this workbook correctly (audit 2026-10-07), so
this parser wraps it: what it adds is coverage, a shape check, the unit (the sheets state none)
and the golden-number tests. Tabel 5 (income allocation) and Tabel 8 (savings choices) are
shares in %, the rest are indices. Tabel 9 was discontinued in March 2020.
"""
from publication_parsers._common import parse_with_generic_reader, sheet_matches
from table_model import TableData

SHEET_PATTERNS = (r"Tabel [1-9]",)
_UNITS = ((r"Tabel [58]", "%"), (r"Tabel [1-9]", "Indeks"))


def parse(data: bytes, sheet_name: str) -> TableData:
    unit = next(unit for pattern, unit in _UNITS if sheet_matches(pattern, sheet_name))
    return parse_with_generic_reader(data, sheet_name, unit=unit)
```

`publication_parsers/skdu.py`:

```python
"""SKDU: 'T1 Kegiatan Usaha' .. 'T10 Margin Usaha' (plus 'T7b Investasi Semesteran').

The generic reader already reads every sheet of this workbook correctly (audit 2026-10-07), so
this parser wraps it: what it adds is coverage, a shape check, the Indonesian half of each
sheet's bilingual unit cell, and the golden-number tests.
"""
from publication_parsers._common import parse_with_generic_reader
from table_model import TableData

SHEET_PATTERNS = (r"T\d+b? .+",)


def parse(data: bytes, sheet_name: str) -> TableData:
    return parse_with_generic_reader(data, sheet_name)
```

Register both. Run the registry tests → PASS.

- [ ] **Step 3: Look at what they read** — `show_table.py sk "tests/fixtures/publications/sk/2026-08/Tabel Series Survei Konsumen Agustus 2026.xlsx" "Tabel 1" 2026 Aug` and `show_table.py skdu "tests/fixtures/publications/skdu/2026-Q2/Tabel SKDU 17 Lapangan Usaha Tw II-2026.xlsx" "T1 Kegiatan Usaha" 2026 Q2`, then every other covered sheet. Expected (checked while writing this plan): SK Tabel 1–8 latest `(2026, 'Aug')`, Tabel 9 `(2020, 'Mar')`; Tabel 1 labels start `Indeks Keyakinan Konsumen (IKK)`, `Indeks Kondisi Ekonomi Saat Ini (IKE)`; Tabel 6 labels look like `1. Jakarta > Indeks Keyakinan Konsumen (IKK)` (the generic reader keeps the city number — fine). SKDU latest: T1, T4, T5, T7 `(2026, 'Q3')`; T2, T3, T6, T7b `(2026, 'Q2')`; T8, T9, T10 `(2026, 'Q1')`; units after the wrapper: `% Saldo Bersih Tertimbang - SBT`, `%`, `% Responden`, `Rp`, …

- [ ] **Step 4: Write both golden files.** SK figures in `sk/2026-08/laporan.pdf`: Indeks Keyakinan Konsumen (IKK), Indeks Kondisi Ekonomi Saat Ini (IKE), Indeks Ekspektasi Konsumen (IEK), and their sub-indices. SKDU figures in `skdu/2026-Q2/laporan.pdf`: saldo bersih tertimbang (SBT) kegiatan usaha for the quarter and the next-quarter expectation, capacity utilisation, and two sector SBTs.

- [ ] **Step 5: Run** `.venv/Scripts/python -m pytest tests/test_publication_golden.py -v -k sk` (matches both `sk/…` and `skdu/…` ids) → all PASS.

- [ ] **Step 6: Commit**

```bash
git add publication_parsers/sk.py publication_parsers/skdu.py publication_parsers/__init__.py tests/test_publication_parsers_registry.py tests/fixtures/publications/sk/2026-08/golden.json tests/fixtures/publications/skdu/2026-Q2/golden.json
git commit -m "Give the consumer and business surveys their own parsers over the generic reader"
```

---

### Task 9: SPE

**Files:**
- Create: `publication_parsers/spe.py`
- Modify: `publication_parsers/__init__.py`
- Create: `tests/fixtures/publications/spe/2026-07/golden.json`
- Test: `tests/test_publication_parsers_registry.py`

**Interfaces:**
- Consumes: `_common.SheetSpec(hierarchy="sections")`, `_common.parse_with_specs`.
- Produces: `PARSERS["spe"]`.

- [ ] **Step 1: Write the failing coverage test**

```python
@pytest.mark.parametrize("sheet, expected", [
    ("Tabel 1", True), ("Tabel 4", True), ("Tabel 9", True), ("Tabel 10", False), ("Tabel1", False),
])
def test_spe_covers_its_nine_tables(sheet, expected):
    assert covers("spe", sheet) is expected
```

Run it → FAIL.

- [ ] **Step 2: Write the module and register it**

`publication_parsers/spe.py`:

```python
"""Survei Penjualan Eceran: 'Tabel 1'..'Tabel 9' of the monthly data-series workbook.

Labels in column 0 ('DESKRIPSI' / 'KOTA'), years in row 3 over months (Indonesian: 'Mei',
'Agt*') or quarters in row 4; a 'Perubahan (Poin)' block and an English mirror on the right.
Tabel 9 groups '- 3 bulan yang akan datang' rows under 'Ekspektasi Penjualan' /
'Ekspektasi Harga Umum' section rows, hence "sections". The sheets carry no unit cell:
index tables are 'Indeks', growth tables (yoy, mtm, qtq) are '%'.
"""
from publication_parsers._common import SheetSpec, parse_with_specs
from table_model import TableData

SPECS = (
    (r"Tabel [159]", SheetSpec(label_cols=(0,), hierarchy="sections", unit="Indeks")),
    (r"Tabel [2-46-8]", SheetSpec(label_cols=(0,), hierarchy="sections", unit="%")),
)
SHEET_PATTERNS = tuple(pattern for pattern, _ in SPECS)


def parse(data: bytes, sheet_name: str) -> TableData:
    return parse_with_specs(data, sheet_name, SPECS)
```

Register it. Run the registry tests → PASS.

- [ ] **Step 3: Look at what it reads** — `show_table.py spe "tests/fixtures/publications/spe/2026-07/Tabel Series SPE -  Juli 2026.xlsx" "Tabel 1" 2026 Jul` (note the two spaces in the file name) and Tabel 2–9. Expected: Tabel 1–3 and 5–7 latest `(2026, 'Aug')` (the next-month estimate column), Tabel 4 and 8 `(2026, 'Q3')`, Tabel 9 `(2026, 'Jul')`; Tabel 1 labels `Suku Cadang dan Aksesori` … `o/w Sandang`, `INDEKS TOTAL`; Tabel 5 cities `Jakarta` … `Denpasar`, `INDEKS TOTAL`; Tabel 9 `Ekspektasi Penjualan > 3 bulan yang akan datang` style labels.

- [ ] **Step 4: Write the golden file.** Figures in `spe/2026-07/laporan.pdf`: Indeks Penjualan Riil (IPR) total for July (Tabel 1 `INDEKS TOTAL`) and its yoy growth (Tabel 2 holds yoy growth as a value), the August estimate, mtm growth (Tabel 3), two groups (e.g. `Makanan, Minuman, & Tembakau`), and the IEP/IEH expectation indices (Tabel 9).

- [ ] **Step 5: Run** `.venv/Scripts/python -m pytest tests/test_publication_golden.py -v -k spe` → all PASS.

- [ ] **Step 6: Commit**

```bash
git add publication_parsers/spe.py publication_parsers/__init__.py tests/test_publication_parsers_registry.py tests/fixtures/publications/spe/2026-07/golden.json
git commit -m "Read the retail sales survey tables with their own parser"
```

---

### Task 10: SBank

**Files:**
- Create: `publication_parsers/sbank.py`
- Modify: `publication_parsers/__init__.py`
- Create: `tests/fixtures/publications/sbank/2026-Q2/golden.json`
- Test: `tests/test_publication_parsers_registry.py`

**Interfaces:**
- Consumes: `_common.SheetSpec`, `_common.parse_with_specs` (stacked blocks, numbers stored as text).
- Produces: `PARSERS["sbank"]`.

- [ ] **Step 1: Write the failing coverage test**

```python
@pytest.mark.parametrize("sheet, expected", [
    ("Tabel1", True), ("Tabel 1", True), ("tabel4", True), ("Tabel 5 (disc)", True), ("Tabel6", False),
])
def test_sbank_covers_its_sheets_however_they_are_spaced(sheet, expected):
    assert covers("sbank", sheet) is expected
```

Run it → FAIL.

- [ ] **Step 2: Write the module and register it**

`publication_parsers/sbank.py`:

```python
"""Survei Perbankan: 'Tabel1'..'Tabel4' and the discontinued 'Tabel 5 (disc)'.

Indonesian labels sit in columns 1–2 (Tabel1, Tabel 5) or 1–3 (Tabel2–4: period kind, group,
item), English copies right after them, years over Roman quarters ('I'..'IV', 'III*') from the
next column on. Tabel3 and Tabel4 stack two or three blocks — realisation, quarterly estimate,
whole-year estimate — each with its own header rows; numbers may be stored as text. Units:
saldo bersih tertimbang (%) except Tabel2, which ranks priorities (1 = first).
"""
from publication_parsers._common import SheetSpec, parse_with_specs
from table_model import TableData

SPECS = (
    (r"Tabel ?1|Tabel ?5.*", SheetSpec(label_cols=(1, 2), unit="%")),
    (r"Tabel ?2", SheetSpec(label_cols=(1, 2, 3), unit="peringkat")),
    (r"Tabel ?[34]", SheetSpec(label_cols=(1, 2, 3), unit="%")),
)
SHEET_PATTERNS = tuple(pattern for pattern, _ in SPECS)


def parse(data: bytes, sheet_name: str) -> TableData:
    return parse_with_specs(data, sheet_name, SPECS)
```

Register it. Run the registry tests → PASS.

- [ ] **Step 3: Look at what it reads** — file `tests/fixtures/publications/sbank/2026-Q2/Data Series Survei Perbankan_ Triwulan II 2_026_.xlsx`. Expected latest periods: Tabel1–4 `(2026, 'Q3')`, `Tabel 5 (disc)` `(2021, 'Q1')`. Tabel1 `TOTAL` Q2 2026 = 93.08, Q3 2026 = 84.65; Tabel3 `Prakiraan per Triwulan > Gabungan > Total` Q3 2026 = 87.52; Tabel4 labels like `Prakiraan per Triwulan > Rupiah > Kredit Modal Kerja`.

- [ ] **Step 4: Write the golden file.** Figures in `sbank/2026-Q2/laporan.pdf`: SBT penyaluran kredit baru for Q2 (Tabel1 `TOTAL`) and the Q3 expectation, two loan types (`Kredit Modal Kerja`, `Kredit Investasi`), DPK growth expectation (Tabel3), and an interest-rate expectation (Tabel4).

- [ ] **Step 5: Run** `.venv/Scripts/python -m pytest tests/test_publication_golden.py -v -k sbank` → all PASS.

- [ ] **Step 6: Commit**

```bash
git add publication_parsers/sbank.py publication_parsers/__init__.py tests/test_publication_parsers_registry.py tests/fixtures/publications/sbank/2026-Q2/golden.json
git commit -m "Read the banking survey tables, stacked blocks included, with their own parser"
```

---

### Task 11: SHPR

**Files:**
- Create: `publication_parsers/shpr.py`
- Modify: `publication_parsers/__init__.py`
- Create: `tests/fixtures/publications/shpr/2026-Q2/golden.json`
- Test: `tests/test_publication_parsers_registry.py`

**Interfaces:**
- Consumes: `_common.SheetSpec(hierarchy="sections", bands=True)`, `_common.parse_with_specs`.
- Produces: `PARSERS["shpr"]`.

- [ ] **Step 1: Write the failing coverage test**

```python
@pytest.mark.parametrize("sheet, expected", [
    ("TABEL 1", True), ("Tabel 2", True), ("TABEL 3", True), ("TABEL 4", False),
])
def test_shpr_covers_its_three_tables(sheet, expected):
    assert covers("shpr", sheet) is expected
```

Run it → FAIL.

- [ ] **Step 2: Write the module and register it**

`publication_parsers/shpr.py`:

```python
"""Survei Harga Properti Residensial: 'TABEL 1' (national index), 'TABEL 2' (index per city),
'TABEL 3' (growth per city).

TABEL 1/2: labels in column 2 under section rows without figures ('TIPE BANGUNAN', '1 BANDUNG');
years in row 5 over 'QI'..'QIV' / 'Q2' in row 6; English labels on the right. TABEL 3: labels in
column 3 under city section rows, and the columns split into a 'TRIWULANAN (QTQ)' band and a
'TAHUNAN (YOY)' band that repeat the same quarters — the band becomes the outermost label.
"""
from publication_parsers._common import SheetSpec, parse_with_specs
from table_model import TableData

SPECS = (
    (r"TABEL [12]", SheetSpec(label_cols=(2,), hierarchy="sections", unit="Indeks (2018=100)")),
    (r"TABEL 3", SheetSpec(label_cols=(3,), hierarchy="sections", bands=True, unit="%")),
)
SHEET_PATTERNS = tuple(pattern for pattern, _ in SPECS)


def parse(data: bytes, sheet_name: str) -> TableData:
    return parse_with_specs(data, sheet_name, SPECS)
```

Register it. Run the registry tests → PASS.

- [ ] **Step 3: Look at what it reads** — `show_table.py shpr tests/fixtures/publications/shpr/2026-Q2/SHPR_Tw_II_2026.xlsx "TABEL 1" 2026 Q2` and TABEL 2–3. Expected: all three latest `(2026, 'Q2')`; TABEL 1 `KECIL` 113.89, `MENENGAH` 114.01, `BESAR` 108.41, `TOTAL` 110.89; TABEL 2 `BANDUNG > KECIL` 117.99; TABEL 3 `TRIWULANAN (QTQ) > BANDUNG > KECIL` 0.25, `TAHUNAN (YOY) > BANDUNG > KECIL` 0.95.

- [ ] **Step 4: Write the golden file.** Figures in `shpr/2026-Q2/laporan.pdf`: national IHPR yoy growth for Q2 (TABEL 1 `TOTAL`, `kind: "yoy"`), the previous quarter's growth, growth by house type (`KECIL`, `MENENGAH`, `BESAR`), and one or two city growths quoted in the report (TABEL 3 band labels, `kind: "value"`).

- [ ] **Step 5: Run** `.venv/Scripts/python -m pytest tests/test_publication_golden.py -v -k shpr` → all PASS.

- [ ] **Step 6: Commit**

```bash
git add publication_parsers/shpr.py publication_parsers/__init__.py tests/test_publication_parsers_registry.py tests/fixtures/publications/shpr/2026-Q2/golden.json
git commit -m "Read the residential property price tables, city sections and growth bands included"
```

---

### Task 12: PMI, and every publication has a parser

**Files:**
- Create: `publication_parsers/pmi.py`
- Modify: `publication_parsers/__init__.py`
- Create: `tests/fixtures/publications/pmi/2026-Q1/golden.json`
- Test: `tests/test_publication_parsers_registry.py`

**Interfaces:**
- Consumes: `_common.SheetSpec`, `_common.parse_with_specs`; `check_history.PUBLICATIONS`.
- Produces: `PARSERS["pmi"]`; `PARSERS` keys == `check_history.PUBLICATIONS`.

- [ ] **Step 1: Write the failing tests**

```python
@pytest.mark.parametrize("sheet, expected", [
    ("T1 PMI", True), ("T1 - Komponen PMI", True), ("T2", True), ("T2 Sublapangan", True),
    ("T10", False), ("T3", False),
])
def test_pmi_covers_both_sheet_namings(sheet, expected):
    assert covers("pmi", sheet) is expected


def test_every_publication_in_the_ui_has_its_own_parser():
    import check_history
    from publication_parsers import PARSERS

    assert sorted(PARSERS) == sorted(check_history.PUBLICATIONS)
```

Run: `.venv/Scripts/python -m pytest tests/test_publication_parsers_registry.py -k "pmi or every_publication" -v` → FAIL.

- [ ] **Step 2: Write the module and register it**

`publication_parsers/pmi.py`:

```python
"""Prompt Manufacturing Index - BI: 'T1 …' (components) and 'T2 …' (sub-sectors).

T1: labels in column 1, years in row 4 over 'I'..'IV' in row 5. T2: Indonesian labels in
column 2 (column 3 is English, column 4 a longer Indonesian name). BI renamed the first sheet
between editions ('T1 - Komponen PMI' → 'T1 PMI'), hence the patterns. Values are diffusion
indices (50 = no change).
"""
from publication_parsers._common import SheetSpec, parse_with_specs
from table_model import TableData

SPECS = (
    (r"T1\b.*", SheetSpec(label_cols=(1,), unit="Indeks")),
    (r"T2\b.*", SheetSpec(label_cols=(2,), unit="Indeks")),
)
SHEET_PATTERNS = tuple(pattern for pattern, _ in SPECS)


def parse(data: bytes, sheet_name: str) -> TableData:
    return parse_with_specs(data, sheet_name, SPECS)
```

Register it. Run the registry tests → PASS (including `test_every_publication_in_the_ui_has_its_own_parser`).

- [ ] **Step 3: Look at what it reads** — `show_table.py pmi tests/fixtures/publications/pmi/2026-Q1/PMI-Triwulan-I-2026.xlsx "T1 PMI" 2026 Q1` and `T2`. Expected latest `(2026, 'Q2')` for both (the next-quarter expectation column); T1 labels `Volume Produksi`, `Volume Total Pesanan`, `Kecepatan Penerimaan Barang Input`, `Volume Persediaan Barang Jadi`, `Jumlah Tenaga Kerja`, `PMI - BI`.

- [ ] **Step 4: Write the golden file.** Figures in `pmi/2026-Q1/laporan.pdf`: PMI-BI for Q1 2026 and the previous quarter, the Q2 expectation, two components, two sub-sectors.

- [ ] **Step 5: Run the whole suite**

Run: `.venv/Scripts/python -m pytest -q` then `.venv/Scripts/python -m pytest tests/test_publication_golden.py -v`
Expected: everything PASSES; the golden run covers all 12 publications with no skips locally.

- [ ] **Step 6: Commit**

```bash
git add publication_parsers/pmi.py publication_parsers/__init__.py tests/test_publication_parsers_registry.py tests/fixtures/publications/pmi/2026-Q1/golden.json
git commit -m "Read the PMI-BI tables, so every publication now has its own parser"
```

---

### Task 13: One real check per publication

**Files:**
- Create: `docs/publication-parsers-e2e.md`
- Modify: `docs/superpowers/specs/2026-10-07-publication-parsers-design.md` (status line only)
- Scratch only (not committed): `<scratchpad>/e2e_run.py`

**Interfaces:**
- Consumes: the whole branch; `main.app`; Gemini via `.env` (`LLM_PROVIDER=google`).
- Produces: the E2E record.

This step spends real Gemini credit (about Rp700 per check, 12 checks). The user approved this in the spec.

- [ ] **Step 1: Write the runner in the scratchpad**

```python
"""One real check per publication through /api/verify-paired, in-process, history kept aside.

usage: e2e_run.py <scratch dir> <publication> <release pdf> <workbook>::<sheet> [...]
"""
import json
import os
import sys
from pathlib import Path

os.environ["APP_USERNAME"] = ""            # login gate off — must be set in Python, not the shell
os.environ["HISTORY_DATABASE_URL"] = ""    # never write to the production history
REPO = Path(r"C:/Users/Ivan Jehuda Angi/Coding/fact-checker")
sys.path.insert(0, str(REPO))
os.chdir(REPO)

import check_history  # noqa: E402

check_history.LOCAL_DB_PATH = Path(sys.argv[1]) / "history_e2e.db"

from fastapi.testclient import TestClient  # noqa: E402

import main  # noqa: E402

publication, pdf_path, *pairs = sys.argv[2:]
files = [("pdf_file", (Path(pdf_path).name, Path(pdf_path).read_bytes(), "application/pdf"))]
sheets = []
for pair in pairs:
    workbook, sheet = pair.split("::")
    files.append(("excel_file", (Path(workbook).name, Path(workbook).read_bytes(),
                                 "application/octet-stream")))
    sheets.append(sheet)

response = TestClient(main.app).post(
    "/api/verify-paired",
    params={"run_typo_check": "false", "check_charts": "false", "publication": publication},
    data={"sheet_names": ",".join(sheets)},
    files=files,
    timeout=900,
)
body = response.json()
out = Path(sys.argv[1]) / f"e2e_{publication}.json"
out.write_text(json.dumps(body, ensure_ascii=False, indent=1), encoding="utf-8")
print(response.status_code, "parsers:", body.get("excel_parsers"))
print("facts:", body.get("total_facts"), "sesuai:", body.get("entailed_count"),
      "tidak sesuai:", body.get("refuted_count"), "tidak cukup data:", body.get("inconclusive_count"))
for r in body.get("results", []):
    if r.get("verdict") != "Entailed":
        print("-", r.get("verdict"), "|", (r.get("claim") or "")[:120], "|", r.get("explanation", "")[:160])
```

- [ ] **Step 2: Run it once per publication** (release PDF + the sheets that hold its figures — the golden `sheets` of that edition minus ones the release never touches). Example for SULNI:

```bash
S="<scratchpad>"; F=tests/fixtures/publications/sulni/2026-07
.venv/Scripts/python "$S/e2e_run.py" "$S" sulni "$F/siaran_pers.pdf" "$F/TABEL_INDONESIA Sep26_value.xlsx::TabI.1" "$F/TABEL_SWASTA Sep26_value.xlsx::Tbl III.2" "$F/TABEL_PEMERINTAH Sep26_value.xlsx::Tbl II.2"
```

Expected for every publication: `excel_parsers` lists the publication key for every sheet (never `generic`, `llm` or `pointer-only`).

- [ ] **Step 3: Check every non-Sesuai claim by hand** against the sheet (`show_table.py`) and the release. Pass per publication when: no false Tidak Sesuai, and no claim whose figure is in a covered sheet ended as Tidak Cukup Data because of parsing. A claim the pipeline cannot express (e.g. the SULNI "four sectors = 80,6%" combined share) is expected to stay Tidak Cukup Data and is listed, not fixed.

- [ ] **Step 4: Write `docs/publication-parsers-e2e.md`**

```markdown
# Uji ujung-ke-ujung parser per publikasi (2026-10-xx)

Gemini 2.5 Flash, cek grafik dan cek ejaan mati, riwayat lokal terpisah. Sumber: folder
`tests/fixtures/publications/<publikasi>/<periode>/`.

| Publikasi | Rilis | Sheet | Parser | Klaim | Sesuai | Tidak Sesuai | Tidak Cukup Data | Lulus |
|---|---|---|---|---|---|---|---|---|
| sulni | siaran pers ULN Juli 2026 | TabI.1, Tbl III.2, Tbl II.2 | sulni ×3 | … | … | … | … | ya/tidak |

## Catatan per publikasi

### sulni
- Tidak Cukup Data yang diharapkan: … (alasan)
- Tidak Sesuai: … (dicek manual: benar/salah)

## Tindak lanjut
- Frasa narasi yang tidak mendarat di label yang benar (pencocok label, bukan parser): …
```

Fill one row and one notes section per publication with the actual numbers from Step 2–3; compare SULNI with the 2026-10-07 baseline (18 claims, 10 Tidak Cukup Data, 1 false Tidak Sesuai, 272 s).

- [ ] **Step 5: Update the spec status line** to `Status: diimplementasikan 2026-10-xx di branch publication-parsers; hasil uji ujung-ke-ujung: docs/publication-parsers-e2e.md.`

- [ ] **Step 6: Commit**

```bash
git add docs/publication-parsers-e2e.md docs/superpowers/specs/2026-10-07-publication-parsers-design.md
git commit -m "Record one real check per publication with its own parser"
```

Merging to `main` and pushing happen only when the user asks.
