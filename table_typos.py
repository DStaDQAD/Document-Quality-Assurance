"""Spelling in the document's own tables: captions, column headers, row names and notes, never numbers.

The narrative check never sees them: a Word report's tables are EMF pictures, and table rows are
stripped from a PDF's narrative. So a misspelt row name such as "Kompensansi" was found only by
hand. Each table's words go through the same typo checker as the narrative (dictionary, tidak-baku
list, the model for the doubtful ones), one call for all tables.

- Word: what each picture table prints (WordDocument.table_texts, 'Tabel 3' → its text), cut into
  label segments at the numbers.
- PDF: the tables the native text-layer reader rebuilds (no model); their text cells.
"""
import re
from dataclasses import dataclass
from typing import Dict, Iterable, List, Optional

from langchain_core.language_models import BaseChatModel

from pdf_table_extraction import PdfTable
from schemas import TypoCheckResponse, TypoIssue
from typo_checker import check_typos

_NUMBERED = re.compile(r"^\s*((?:Tabel|Lampiran|Table|Appendix)\s+[0-9IVX]+[a-z]?)\b", re.IGNORECASE)
_HAS_LETTER = re.compile(r"[A-Za-zÀ-ÿ]")
# A number cell as a table prints it: 100,0  -3,8  (49,8)  12,3%  1.234,56*  −0,1
_NUMBER_TOKEN = re.compile(r"^[(\-–−+]?[\d.,]+%?\)?\**$")
# Cells are kept one per line, with a mark between them: two neighbouring cells that start with the
# same word ("Rupiah" over "Rupiah dan Valas") must not read as an unhyphenated reduplication.
_CELL_BREAK = " |\n"
_BLOCK_BREAK = " |\n\n"
# A capitalised word mid-line counts as a proper noun to the typo checker and is skipped, while in a
# table nearly every name is capitalised ("Kompensansi Tenaga Kerja"). Title-case words are lowered
# before the check (same length, so offsets hold) and the issue shows the word as printed. ALLCAPS
# abbreviations (ULN, SBT, BPM6) stay as they are and skipped; month abbreviations (Agu, Des) are blanked.
_TITLE_WORD = re.compile(r"\b[A-ZÀ-Þ][a-zà-ÿ]+\b")
_MONTHS = {"jan", "feb", "mar", "apr", "mei", "may", "jun", "jul", "agu", "agt", "ags", "aug", "sep", "sept",
           "okt", "oct", "nov", "des", "dec", "peb", "nop"}


@dataclass
class TableWords:
    """One table's words: where it is ('Tabel 3', its page for a PDF) and its label cells."""
    location: str
    page: Optional[int]
    cells: List[str]


def location_of(caption: str) -> str:
    """'Tabel 3' / 'Lampiran 2' from a caption; else its first words."""
    caption = (caption or "").strip()
    match = _NUMBERED.match(caption)
    if match:
        return " ".join(match.group(1).split())
    words = caption.split()
    return "Tabel: " + (" ".join(words[:4]) + ("…" if len(words) > 4 else "")) if words else "Tabel"


def _unique(cells: Iterable[str]) -> List[str]:
    seen, out = set(), []
    for cell in cells:
        text = " ".join(str(cell).split())
        if text and _HAS_LETTER.search(text) and text not in seen:
            seen.add(text)
            out.append(text)
    return out


def pdf_table_words(tables: Iterable[PdfTable]) -> List[TableWords]:
    """A PDF's tables: caption and every text cell (headers, row names), each once."""
    return [TableWords(location_of(t.caption), t.page_number or None,
                       _unique([t.caption, *(c for row in t.grid for c in row if isinstance(c, str))]))
            for t in tables]


def word_table_words(table_texts: Dict[str, str]) -> List[TableWords]:
    """A Word report's picture tables: their printed text cut into label segments at the numbers
    ('A, General Marchandise 99,2 100,0 …' → 'A, General Marchandise')."""
    out = []
    for caption, text in (table_texts or {}).items():
        segments, current = [], []
        for token in text.split():
            if _NUMBER_TOKEN.match(token):
                segments.append(" ".join(current))
                current = []
            else:
                current.append(token)
        segments.append(" ".join(current))
        out.append(TableWords(location_of(caption), None, _unique([caption, *segments])))
    return out


def _lowered(match: "re.Match") -> str:
    """A title-case word lowered; a month abbreviation (a period header) blanked out, same length."""
    word = match.group(0)
    return " " * len(word) if word.lower() in _MONTHS else word.lower()


# Two capitalised copies of one word side by side are two cells of a picture table read as one line
# ("Kelompok Barang Barang Konsumsi", "Total Total"), not an unhyphenated reduplication: the second
# keeps its capital, which the typo checker's reduplication rule requires to be lower case.
# A lookahead, so that "Kelompok Barang Barang" still pairs the two copies after a non-matching pair.
_REPEATED_TITLE = re.compile(r"\b([A-ZÀ-Þ][a-zà-ÿ]+)(?=(\s+)([A-ZÀ-Þ][a-zà-ÿ]+)\b)")


def _for_checking(printed: str) -> str:
    """The text the typo checker reads: title-case words lowered and months blanked (_lowered), a
    repeated title-case word left as printed. Same length as `printed`, so offsets hold."""
    text = list(_TITLE_WORD.sub(_lowered, printed))
    for match in _REPEATED_TITLE.finditer(printed):
        if match.group(1).lower() == match.group(3).lower():
            text[match.start(3):match.end(3)] = match.group(3)
    return "".join(text)


def check_table_typos(tables: Iterable[TableWords], llm: Optional[BaseChatModel]) -> List[TypoIssue]:
    """Typo issues in the tables' words, each with its table's location (and page, for a PDF). A word
    misspelt the same way twice in one table is reported once."""
    blocks = [t for t in tables if t.cells]
    if not blocks:
        return []
    texts = [_CELL_BREAK.join(t.cells) for t in blocks]
    starts, offset = [], 0
    for text in texts:
        starts.append(offset)
        offset += len(text) + len(_BLOCK_BREAK)
    printed = _BLOCK_BREAK.join(texts)
    result = check_typos(_for_checking(printed), llm)
    issues, seen = [], set()
    for issue in result.issues:
        i = max(n for n, start in enumerate(starts) if start <= issue.start)
        table = blocks[i]
        key = (table.location, issue.word.lower(), issue.suggestion.lower())
        if key in seen:
            continue
        seen.add(key)
        word = printed[issue.start:issue.end]
        suggestion = issue.suggestion
        if word[:1].isupper() and suggestion[:1].islower():
            suggestion = suggestion[:1].upper() + suggestion[1:]   # "Kompensansi" → "Kompensasi"
        issues.append(issue.model_copy(update={
            "word": word, "suggestion": suggestion,
            "start": issue.start - starts[i], "end": issue.end - starts[i],
            "location": table.location, "page_number": table.page,
        }))
    return issues


def merge_typos(narrative: TypoCheckResponse, table_issues: List[TypoIssue]) -> TypoCheckResponse:
    """The narrative's typo response with the tables' issues after it, counts and summary redone."""
    issues = list(narrative.issues) + list(table_issues)
    counts = {c: sum(1 for x in issues if x.category == c) for c in ("ejaan", "tidak_baku", "grammar")}
    total = len(issues)
    summary = (f"Ditemukan {total} isu: {counts['ejaan']} ejaan, {counts['tidak_baku']} kata tidak baku, "
               f"{counts['grammar']} tata bahasa" + (f" ({len(table_issues)} di tabel dokumen)." if table_issues else ".")
               if total else "Tidak ditemukan isu ejaan atau tata bahasa.")
    return TypoCheckResponse(total_issues=total, ejaan_count=counts["ejaan"], tidak_baku_count=counts["tidak_baku"],
                             grammar_count=counts["grammar"], summary=summary, issues=issues)
