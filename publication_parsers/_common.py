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
from collections import Counter, OrderedDict
from dataclasses import dataclass
from typing import Dict, List, Optional, Sequence, Tuple

from excel_parser_bi import parse_bi_table
from table_model import QUAL_SEP, TableData
from table_parser_generic import (
    _bare_period_token,
    _parse_period,
    load_workbook_grids,
    parse_generic_table,
)

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
# '1.' / '2.1' / '2.1.1.' (numbers with a dot — a bare '3' starts '3 bulan yang akan datang'),
# '-' (dash items). A cell that is only a number is a row number.
_LEADING_NUMBERING = re.compile(
    r"(?:[IVX]+\.(?:\d+\.)*(?=\s|$)|[A-Za-z]\.(?=\s|$)|\d+(?:\.\d+)+\.?(?=\s|$)|\d+\.(?=\s|$)"
    r"|\d+$|-+)\s*"
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
      "sections"   a label row without figures heads the rows below it;
      "outline"    the label's outline marker, whatever column it is in: each marker style
                   ('I.', 'A.', '1.', 'a.', '1)', 'a)', '(1)') opens a level the first time it
                   appears under the rows above and is a sibling when it appears again; '-'
                   items sit under the last marked row; an unmarked row starts over at the top.
    bands: the row above the year row splits the columns into bands ('TRIWULANAN (QTQ)' vs
      'TAHUNAN (YOY)'); the band becomes the outermost label level.
    unit: replaces the unit read from the title block.
    end_at_forecast: the sheet's last real period is its first starred one ('I*' = angka
      perkiraan); later columns are template leftovers and are dropped. Not for SEKI, whose
      stars mark several provisional quarters in a row.
    sum_missing_leaves: leaf names ('Penerimaan', 'Pembayaran') a parent row should have but
      does not print, while every child that has figures does: the parent's row is added as
      their sum (NPI 'Pendapatan investasi langsung' = modal ekuitas + bunga).
    """
    label_cols: Tuple[int, ...]
    header: str = "two_rows"
    hierarchy: str = "flat"
    bands: bool = False
    unit: Optional[str] = None
    end_at_forecast: bool = False
    sum_missing_leaves: Tuple[str, ...] = ()


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


_PERIOD_ORDER = ["Jan", "Feb", "Mar", "Apr", "May", "Jun", "Jul", "Aug", "Sep", "Oct", "Nov",
                 "Dec", "Q1", "Q2", "Q3", "Q4"]


def _year_runs(
    head: List, below: List, band_row: List, first_col: int, width: int
) -> List[List[Tuple[int, Optional[int], str, str]]]:
    """Period columns grouped into runs of one year: [(column, written year, period, band)].

    A run ends where the periods start over (Q4 → Q1, Dec → Jan), the band changes, or a
    column is not a period at all (annual column, 'ITEMS', 'Perubahan (Poin)').
    """
    runs: List[List[Tuple[int, Optional[int], str, str]]] = []
    run: List[Tuple[int, Optional[int], str, str]] = []
    band = ""
    for c in range(first_col, width):
        if not _blank(_cell(band_row, c)):
            band = clean_label(_cell(band_row, c))
        written = _cell(head, c)
        year = None if _blank(written) else parse_year(written)
        period = parse_period(_cell(below, c))
        if period is None or (not _blank(written) and year is None):
            if run:
                runs.append(run)
                run = []
            continue
        if run and (
            _PERIOD_ORDER.index(period) <= _PERIOD_ORDER.index(run[-1][2]) or band != run[-1][3]
        ):
            runs.append(run)
            run = []
        run.append((c, year, period, band))
    if run:
        runs.append(run)
    return runs


def _period_columns(
    grid: List[List], spec: SheetSpec, header_row: int, first_col: int
) -> Dict[int, Tuple[str, int, str]]:
    """{column: (band, year, period)} for every monthly/quarterly column, leftmost copy only.

    Two-row headers write a year's number once (on its first column, on its last — SEKI's NPI
    marks Q4 — or merged over all of them), so each run of one year's periods takes the year
    written anywhere inside it; a run with none follows the year before.
    """
    head = grid[header_row]
    below = grid[header_row + 1] if spec.header == "two_rows" else []
    band_row = grid[header_row - 1] if spec.bands and header_row > 0 else []
    width = max(len(head), len(below), len(band_row))
    keyed: List[Tuple[int, Tuple[str, int, str]]] = []
    if spec.header == "combined":
        band = ""
        for c in range(first_col, width):
            if not _blank(_cell(band_row, c)):
                band = clean_label(_cell(band_row, c))
            parsed = parse_year_period(_cell(head, c))
            if parsed is not None:
                keyed.append((c, (band, parsed[0], parsed[1])))
    else:
        previous: Optional[int] = None
        for run in _year_runs(head, below, band_row, first_col, width):
            written = [year for _, year, _, _ in run if year is not None]
            year = written[0] if written else (previous + 1 if previous is not None else None)
            for c, own_year, period, band in run:
                if own_year is not None:
                    year = own_year  # several years written inside one run: each holds onward
                if year is not None:
                    keyed.append((c, (band, year, period)))
            previous = year
    if spec.end_at_forecast:
        period_row = below if spec.header == "two_rows" else head
        starred = [c for c, _ in keyed if str(_cell(period_row, c) or "").strip().endswith("*")]
        if starred:
            keyed = [(c, key) for c, key in keyed if c <= starred[0]]
    columns: Dict[int, Tuple[str, int, str]] = {}
    seen = set()
    for c, key in keyed:
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


_AGGREGATE_LABELS = frozenset({"total", "jumlah"})


def _first_label_col(spec: SheetSpec, row: List) -> int:
    return next(i for i, c in enumerate(spec.label_cols) if not _blank(_cell(row, c)))


def _own_numbering(raw: List, own: int) -> int:
    """Depth of the row's own number ('2.1.1.'), in its label cell or a numbering-only cell
    just before it; 0 for unnumbered rows and 'a.' / '-' items."""
    depth = numbering_depth(raw[own])
    if not depth and own > 0 and not clean_label(raw[own - 1]):
        depth = numbering_depth(raw[own - 1])
    return depth


def _depth(spec: SheetSpec, r: int, row: List, raw: List, own: int, has_data: bool,
           indents: Optional[Dict[int, int]],
           last_numbered: Optional[Tuple[int, int]] = None) -> int:
    if spec.hierarchy == "indent":
        return (indents or {}).get(r, 0)
    first_col = _first_label_col(spec, row)
    if spec.hierarchy == "numbering":
        # '2.1.1.' says its own depth. An 'a.' / '-' item or an unnumbered row sits one level
        # below the last numbered row of its group, deeper still by how many columns further
        # right it starts (SULNI Tbl II.6: '-  Bilateral' under '1.1', '     ODA' under it).
        # A TOTAL row closes the group.
        depth = _own_numbering(raw, own)
        if depth:
            return depth
        if clean_label(raw[own]).lower() in _AGGREGATE_LABELS:
            return 0
        if last_numbered is not None:
            numbered_depth, numbered_col = last_numbered
            return numbered_depth + 1 + max(first_col - numbered_col, 0)
        return first_col + 1
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


# Outline markers, most specific first. A roman numeral and a capital letter look alike ('I.'
# after 'H.' is a letter); _outline_step decides with the levels already open.
_OUTLINE_MARKER = re.compile(
    r"\s*(?:(?P<paren>\(\d+\))|(?P<numclose>\d+\))|(?P<letclose>[A-Za-z]\))"
    r"|(?P<num>\d+\.)(?!\d)|(?P<roman>[IVXLC]+\.)(?=\s)|(?P<upper>[A-Z]\.)(?=\s)"
    r"|(?P<lower>[a-z]\.)(?=\s)|(?P<dash>-+))\s*"
)

def _outline_marker(raw) -> Tuple[Optional[str], str]:
    """(marker style, marker text) of a label cell; (None, '') for an unmarked one."""
    if not isinstance(raw, str):
        return None, ""
    match = _OUTLINE_MARKER.match(raw)
    if not match:
        return None, ""
    style = match.lastgroup
    text = match.group(style)
    if style == "letclose":
        style = "upperclose" if text[0].isupper() else "lowerclose"
    return style, text


def _outline_label(raw) -> str:
    """The label without its outline marker ('1) Pendapatan' -> 'Pendapatan')."""
    style, _ = _outline_marker(raw)
    if style is not None and isinstance(raw, str):
        raw = raw[_OUTLINE_MARKER.match(raw).end():]
    return clean_label(raw)


def _outline_step(levels: List[Tuple[str, str]], raw) -> int:
    """The row's depth under the open `levels` [(style, marker)], which it updates in place."""
    style, text = _outline_marker(raw)
    if levels and levels[-1][0] == "dash" and style != "dash":
        levels.pop()  # '-' items are leaves: the next marked row is not their child
    if style is None:
        levels[:] = [("none", "")]
        return 0
    if style == "roman" and len(text) == 2:
        # 'I.' after 'H.' continues the letters; so does a lone 'C.' / 'I.' while letters are
        # open and no roman level is.
        open_styles = {s: m for s, m in levels}
        after = open_styles.get("upper")
        if after is not None and (ord(text[0]) == ord(after[0]) + 1 or "roman" not in open_styles):
            style = "upper"
    for depth, (open_style, _) in enumerate(levels):
        if open_style == style:
            del levels[depth:]
            levels.append((style, text))
            return depth
    levels.append((style, text))
    return len(levels) - 1


def _block_title(rows: List[List], label_cols: Sequence[int]) -> str:
    """A stacked table's own name: the last title line above its header ('BARANG')."""
    title = ""
    for row in rows:
        for c in label_cols:
            value = _cell(row, c)
            if not isinstance(value, str) or not value.strip():
                continue
            text = " ".join(value.split())
            if _UNIT_CELL.fullmatch(text) or _TABLE_NUMBER.fullmatch(text) or _FOOTER.match(text):
                continue
            title = clean_label(text)
    return title


# A unit a row states for itself: '(ribuan orang)', '(% PDB)', '(USD/barel)'.
_ROW_UNIT_IN_LABEL = re.compile(
    r"\(([^()]*(?:%|persen|orang|barel|ton\b|USD|Rp)[^()]*)\)", re.IGNORECASE
)


def _summed_missing_leaves(
    entries: List[Tuple[List[str], Dict[Tuple[int, str], float]]], leaves: Sequence[str]
) -> List[Tuple[List[str], Dict[Tuple[int, str], float]]]:
    """Rows for the leaves a parent leaves out, summed from its children (see SheetSpec)."""
    if not leaves:
        return []
    by_path = {tuple(path): values for path, values in entries}
    added = []
    for parent in list(by_path):
        children = [p for p in by_path if len(p) == len(parent) + 1 and p[:-1] == parent
                    and p[-1] not in leaves]
        if len(children) < 2:
            continue
        for leaf in leaves:
            if parent + (leaf,) in by_path:
                continue
            parts = [by_path.get(child + (leaf,)) for child in children]
            if any(part is None for part in parts):
                continue
            periods = set.intersection(*(set(part) for part in parts))
            if periods:
                added.append((list(parent) + [leaf],
                              {k: round(sum(part[k] for part in parts), 6) for k in periods}))
    return added


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
    last_numbered: Optional[Tuple[int, int]] = None  # (depth, first label column)
    levels: List[Tuple[str, str]] = []  # open outline levels
    # A footer ('Catatan:', '*) Angka sementara') closes a block. A sheet may stack another
    # table below it (NPI's internal workbook: TABEL 1..8 on one sheet); its rows are read
    # under that table's own name, since its first rows repeat names from the tables above.
    footer_at: Optional[int] = None
    prefix: List[str] = []
    for r in range(header_rows[0], len(grid)):
        if r in blocks:
            columns, stack, group, last_numbered, levels = blocks[r], [], None, None, []
            if footer_at is not None:
                block_title = _block_title(grid[footer_at:r], spec.label_cols)
                prefix = [block_title] if block_title else []
                footer_at = None
        if r in header_like or footer_at is not None:
            continue
        row = grid[r]
        raw: List = []
        for c in spec.label_cols:
            value = _cell(row, c)
            if not _blank(value) and not (raw and value == raw[-1]):
                raw.append(value)
        label = _outline_label if spec.hierarchy == "outline" else clean_label
        texts = [label(v) for v in raw]
        if not any(texts):
            continue
        if isinstance(raw[0], str) and _FOOTER.match(raw[0]):
            if not any(h > r for h in header_rows):
                break
            footer_at = r
            continue
        if tuple(clean_label(_cell(row, c)) for c in spec.label_cols) in headings:
            continue
        own = max(i for i, t in enumerate(texts) if t)
        ancestors = tuple(t for t in texts[:own] if t)
        by_band: Dict[str, Dict[Tuple[int, str], float]] = {}
        for c, (band, year, period) in columns.items():
            value = _number(_cell(row, c))
            if value is not None:
                by_band.setdefault(band, {})[(year, period)] = value
        if ancestors != group:
            stack, group, last_numbered = [], ancestors, None
        if spec.hierarchy == "outline":
            depth = _outline_step(levels, raw[own])
        else:
            depth = _depth(spec, r, row, raw, own, bool(by_band), indents, last_numbered)
        if spec.hierarchy == "numbering" and _own_numbering(raw, own):
            last_numbered = (depth, _first_label_col(spec, row))
        while stack and stack[-1][0] >= depth:
            stack.pop()
        path = prefix + list(ancestors) + [name for _, name in stack] + [texts[own]]
        stack.append((depth, texts[own]))
        for band, values in by_band.items():
            entries.append(([band] + path if band else path, values))
    if not entries:
        raise PublicationParseError("tidak ada baris berisi angka di bawah baris tahun")
    entries += _summed_missing_leaves(entries, spec.sum_missing_leaves)

    table = TableData(title=title, unit=spec.unit or unit, row_labels=[])
    for name, (path, _) in zip(_shortest_unique_names([p for p, _ in entries]), entries):
        for level in reversed(path):
            match = _ROW_UNIT_IN_LABEL.search(level)
            if match:
                table.row_units[name] = match.group(1).strip()
                break
    for name, (_, values) in zip(_shortest_unique_names([p for p, _ in entries]), entries):
        if name in table.row_labels:
            # Two rows the hierarchy could not tell apart. Keeping the first would answer the
            # second's claims with the first's figures, silently — the generic cascade instead.
            raise PublicationParseError(f"label baris berulang tanpa induk pembeda: '{name}'")
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
    _close_letter_spacing(table)
    if _is_semester_series(table):
        table._data = {
            (label, year, SEMESTER_OF_HEADER[period]): value
            for (label, year, period), value in table._data.items()
        }
    return table


# A word typed with a space between its letters for emphasis: SKDU 'T7 Investasi Triwulanan'
# labels its total 'T O T A L', which nothing recognised as the total row, so "kegiatan investasi
# … tetap kuat" found no row although the sheet holds it (5,46 -> 5,33).
_LETTER_SPACED = re.compile(r"\b[A-Za-z](?: [A-Za-z]){2,}\b")


def _close_letter_spacing(table: TableData) -> None:
    def close(label: str) -> str:
        return _LETTER_SPACED.sub(lambda m: m.group(0).replace(" ", ""), label)

    renamed = {label: close(label) for label in table.row_labels}
    if all(old == new for old, new in renamed.items()):
        return
    table.row_labels = [renamed[label] for label in table.row_labels]
    table._data = {(renamed.get(key[0], key[0]),) + key[1:]: value for key, value in table._data.items()}
    table.row_units = {renamed.get(label, label): unit for label, unit in table.row_units.items()}


# A semester sheet heads its columns 'I' / 'II' under each year, exactly as a quarterly sheet
# heads 'I' .. 'IV', so the reader files them as Q1 / Q2. SKDU 'T7b Investasi Semesteran' then
# answered "realisasi investasi meningkat pada triwulan II 2026" with semester I -> semester II
# (a forecast) and refuted a true claim. S1 / S2 keep semester figures off quarter claims.
SEMESTER_OF_HEADER = {"Q1": "S1", "Q2": "S2"}


def _is_semester_series(table: TableData) -> bool:
    """Every year has only periods I and II, and at least two years have both."""
    by_year: Dict[int, set] = {}
    for _, year, period in table._data:
        by_year.setdefault(year, set()).add(period)
    full_years = sum(1 for periods in by_year.values() if periods == {"Q1", "Q2"})
    return full_years >= 2 and all(periods <= {"Q1", "Q2"} for periods in by_year.values())
